"""Deployment descriptors must use existing paths without changing user state."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def installer():
    spec = importlib.util.spec_from_file_location('installer_enrollment', ROOT / 'installer/__main__.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EnrollmentTests(unittest.TestCase):
    def test_standard_installer_records_actual_existing_config(self):
        module = installer()
        self.assertTrue(hasattr(module, 'write_update_descriptor'), 'installer update enrollment missing')
        with tempfile.TemporaryDirectory() as directory:
            layout = module.Layout(prefix=Path(directory).resolve(), host_ip='192.0.2.1')
            layout.etc.mkdir(parents=True)
            layout.opt.mkdir(parents=True)
            static = layout.opt / 'static'
            data = Path(directory).resolve() / 'state'
            config = f'[network]\nhost_ip="192.0.2.9"\n[ports]\nadmin=9443\n[paths]\nstatic="{static}"\ndata="{data}"\n'
            layout.config.write_text(config)
            module.write_update_descriptor(layout, module.Runner(layout))
            descriptor = json.loads((layout.opt / 'update-install.json').read_text())
            self.assertEqual(descriptor['static_dir'], str(static))
            self.assertEqual(descriptor['data_dir'], str(data))
            self.assertEqual(descriptor['health_url'], 'https://192.0.2.9:9443/api/version')
            self.assertEqual(layout.config.read_text(), config)
            self.assertEqual(descriptor['admin_unit'], 'ps5-jb-server.service')

    def test_standard_services_depend_on_recovery_before_start(self):
        module = installer()
        with tempfile.TemporaryDirectory() as directory:
            layout = module.Layout(prefix=Path(directory).resolve(), host_ip='192.0.2.1')
            layout.config.parent.mkdir(parents=True)
            layout.opt.mkdir(parents=True)
            layout.config.write_text(f'[network]\nhost_ip="192.0.2.1"\n[paths]\ndata="{layout.data}"\n')
            module.write_update_descriptor(layout, module.Runner(layout))
            from unittest import mock
            with mock.patch.object(module.Runner, 'run'):
                module.install_units(layout, module.Runner(layout))
            recovery = (layout.systemd_dir / 'ps5-jb-server-update-recovery.service').read_text()
            self.assertIn(' --recover ', recovery)
            self.assertIn('Before=ps5-jb-server.service ps5-jb-repo.service', recovery)
            for unit in ('ps5-jb-server.service', 'ps5-jb-repo.service'):
                dropin = (layout.systemd_dir / (unit + '.d/update-recovery.conf')).read_text()
                self.assertIn('Requires=ps5-jb-server-update-recovery.service', dropin)
                self.assertIn('After=ps5-jb-server-update-recovery.service', dropin)

    def test_legacy_enrollment_requires_installed_update_files_and_preserves_data(self):
        path = ROOT / 'scripts/enable_web_updates.py'
        self.assertTrue(path.exists(), 'legacy enrollment command missing')
        spec = importlib.util.spec_from_file_location('enable_updates', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            program, data = root / 'program', root / 'data'
            program.mkdir()
            config = root / 'config.toml'
            config.write_text(f'[network]\nhost_ip="192.0.2.3"\n[paths]\ndata="{data}"\nstatic="{program / "static"}"\n')
            with self.assertRaises(ValueError):
                module.enroll(program, config, 'ps5-manager.service', 'ps5-repo.service')
            for name in ('server.py', 'version.py', 'updates.py', 'update_worker.py'):
                (program / name).write_text('# installed')
            result = module.enroll(program, config, 'ps5-manager.service', 'ps5-repo.service')
            self.assertEqual(result['admin_unit'], 'ps5-manager.service')
            self.assertEqual(json.loads((program / 'update-install.json').read_text())['data_dir'], str(data))
            self.assertFalse(data.exists())
            with self.assertRaises(ValueError):
                module.enroll(program, config, '--bad.service', None)


if __name__ == '__main__':
    unittest.main()
