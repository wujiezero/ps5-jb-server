"""Installer safety: dry-run must print a plan and touch nothing."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / 'installer' / '__main__.py'


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

    def test_dry_run_reports_no_bad_port(self):
        result = self.run_installer('--dry-run', 'doctor', '--host-ip', '192.0.2.10')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('doctor', result.stdout)

    def test_unknown_flag_fails(self):
        result = self.run_installer('--nope')
        self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
