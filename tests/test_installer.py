"""Installer safety: dry-run must print a plan and touch nothing."""
import ast
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / 'installer' / '__main__.py'
# Programs started by the systemd units (host.py is downloaded, not ours), and the
# unrar worker that ps5-jb-server starts through systemd-run.
ENTRY_POINTS = ('server.py', 'payload_repo.py', 'extract_worker.py')


def local_imports(entry):
    """Every server/*.py reachable from an entry point, including function-level imports."""
    server = REPO / 'server'
    seen, todo = set(), [entry]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        tree = ast.parse((server / name).read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                modules = [node.module]
            for module in modules:
                candidate = module.split('.')[0] + '.py'
                if (server / candidate).is_file():
                    todo.append(candidate)
    return seen


def load_installer():
    spec = importlib.util.spec_from_file_location('ps5jb_installer', INSTALLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InstallerDryRunTests(unittest.TestCase):
    def run_installer(self, *args):
        return subprocess.run([sys.executable, str(INSTALLER), *args],
                              capture_output=True, text=True, timeout=120)

    def test_dry_run_plans_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / 'root'
            result = self.run_installer('--prefix', str(prefix), '--host-ip', '192.0.2.10',
                                        '--dry-run', '--no-payloads', '--no-manuals-host',
                                        '--no-firewall', '--no-services')
            self.assertEqual(result.returncode, 0, result.stderr)
            for marker in ('[1/9]', '[2/9]', '[5/9]', '[9/9]', '管理页面'):
                self.assertIn(marker, result.stdout)
            self.assertFalse(prefix.exists(), 'dry-run must not create the install prefix')

    def test_every_imported_server_module_is_installed(self):
        installed = set(load_installer().server_modules())
        for entry in ENTRY_POINTS:
            needed = local_imports(entry)
            self.assertIn('settings.py', local_imports('server.py'))
            self.assertFalse(needed - installed, f'{entry} imports modules the installer skips')
        # The modules that were once forgotten.
        self.assertTrue({'ftp_files.py', 'nas.py', 'power.py', 'titles.py', 'archive.py', 'unrar.py',
                         'extract_worker.py'} <= installed)

    def test_dry_run_copies_all_modules_and_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / 'root'
            result = self.run_installer('--prefix', str(prefix), '--host-ip', '192.0.2.10',
                                        '--dry-run', '--no-payloads', '--no-manuals-host',
                                        '--no-firewall', '--no-services')
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in sorted(local_imports('server.py') | local_imports('payload_repo.py')):
                self.assertRegex(result.stdout, rf'copy \S*/server/{name} -> \S*/{name}\n')
            self.assertRegex(result.stdout, r'copy \S*/titledb/titles\.json -> \S*/titledb/titles\.json')

    def test_dry_run_reports_no_bad_port(self):
        result = self.run_installer('--dry-run', 'doctor', '--host-ip', '192.0.2.10')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('doctor', result.stdout)
        self.assertIn('解压引擎', result.stdout)

    def test_with_extract_plans_the_packages(self):
        from unittest import mock
        installer = load_installer()
        for candidate, expected in (('  Candidate: 1:6.2.6-1+deb12u1\n', installer.EXTRACT_PACKAGES),
                                    ('  Candidate: (none)\n', tuple(p for p in installer.EXTRACT_PACKAGES
                                                                    if p != 'libunrar5'))):
            with tempfile.TemporaryDirectory() as tmp:
                layout = installer.Layout(prefix=Path(tmp), host_ip='192.0.2.10', dry_run=True, extract=True)
                runner = installer.Runner(layout)
                policy = subprocess.CompletedProcess([], 0, 'libunrar5:\n' + candidate, '')
                with mock.patch.object(installer.shutil, 'which', return_value='/usr/bin/apt-get'), \
                        mock.patch.object(installer.subprocess, 'run', return_value=policy):
                    installer.install_extract(layout, runner)
                self.assertEqual(runner.actions[-1][:4], ['apt-get', 'install', '-y', '--no-install-recommends'])
                self.assertEqual(tuple(runner.actions[-1][4:]), tuple(expected))
            layout = installer.Layout(prefix=Path(tmp), host_ip='192.0.2.10', dry_run=True)
            runner = installer.Runner(layout)
            installer.install_extract(layout, runner)
            self.assertEqual(runner.actions, [], 'nothing without --with-extract')

    def test_uninstall_removes_recovery_units(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            layout = installer.Layout(prefix=Path(tmp).resolve(), dry_run=True)
            for name in ('ps5-jb-server-update-recovery.service',
                         'ps5-jb-server.service.d/update-recovery.conf',
                         'ps5-jb-repo.service.d/update-recovery.conf'):
                path = layout.systemd_dir / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('stale')
            runner = installer.Runner(layout)
            installer.cmd_uninstall(layout, runner=runner)
            self.assertTrue(any(action and action[0:2] == ['rm', '-f']
                                and action[-1].endswith('ps5-jb-server-update-recovery.service')
                                for action in runner.actions))

    def test_unknown_flag_fails(self):
        result = self.run_installer('--nope')
        self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()


class DependencyInstallTests(unittest.TestCase):
    """install.sh brings missing system packages in instead of stopping."""

    def plan(self, missing_probes, manager='apt-get', **layout_args):
        import io
        from contextlib import redirect_stdout
        installer = load_installer()
        layout = installer.Layout(prefix=Path('/'), dry_run=True, **layout_args)
        runner = installer.Runner(layout)
        present = lambda probe: probe[1] not in missing_probes  # noqa: E731
        which = lambda name: '/usr/bin/' + name if name == manager else None  # noqa: E731
        output = io.StringIO()
        with redirect_stdout(output):
            installer.install_dependencies(layout, runner, present=present, which=which)
        return runner.actions, output.getvalue()

    def test_missing_required_packages_are_installed_with_apt(self):
        actions, _ = self.plan({'import dns.message', 'nft', 'conntrack'})
        self.assertEqual(actions, [['apt-get', 'install', '-y', '--no-install-recommends',
                                    'python3-dnspython', 'nftables', 'conntrack']])

    def test_optional_smb_is_a_separate_best_effort_install(self):
        actions, _ = self.plan({'openssl', 'import smbc'})
        self.assertEqual(actions[0][-1], 'openssl')
        self.assertEqual(actions[1][-1], 'python3-smbc')

    def test_package_names_follow_the_distribution(self):
        actions, _ = self.plan({'import dns.message', 'conntrack'}, manager='dnf')
        self.assertEqual(actions, [['dnf', 'install', '-y', 'python3-dns', 'conntrack-tools']])

    def test_no_firewall_skips_gateway_tools_and_no_deps_installs_nothing(self):
        actions, _ = self.plan({'nft', 'conntrack'}, no_firewall=True)
        self.assertEqual(actions, [])
        actions, text = self.plan({'openssl'}, no_deps=True)
        self.assertEqual(actions, [])
        self.assertIn('openssl', text)

    def test_nothing_missing_runs_nothing(self):
        actions, text = self.plan(set())
        self.assertEqual(actions, [])
        self.assertIn('依赖齐全', text)
