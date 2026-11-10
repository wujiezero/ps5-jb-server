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
# Programs started by the systemd units (host.py is downloaded, not ours).
ENTRY_POINTS = ('server.py', 'payload_repo.py')


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
        self.assertTrue({'ftp_files.py', 'nas.py', 'power.py', 'titles.py'} <= installed)

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

    def test_unknown_flag_fails(self):
        result = self.run_installer('--nope')
        self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
