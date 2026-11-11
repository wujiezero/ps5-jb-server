"""Official release discovery and persisted web update jobs."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
try:
    from updates import UpdateManager, UpdateError, newer
except ImportError:
    UpdateManager = UpdateError = newer = None


class UpdateTests(unittest.TestCase):
    def setUp(self):
        # The scenarios model an installed 1.4.0 meeting a 1.5.0 release,
        # independent of the version this checkout happens to carry.
        installed = mock.patch('updates.VERSION', '1.4.0')
        installed.start()
        self.addCleanup(installed.stop)
        self.assertIsNotNone(UpdateManager, 'update manager is missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.program = self.root / 'program'
        self.program.mkdir()
        self.data = self.root / 'data'
        self.payload = b'verified archive bytes'
        self.version = '1.5.0'
        self.asset = f'ps5-jb-server-{self.version}.tar.gz'
        self.base = f'https://github.com/wujiezero/ps5-jb-server/releases/download/v{self.version}/'
        self.release = {'tag_name': 'v1.5.0', 'draft': False, 'prerelease': False,
                        'html_url': 'https://github.com/wujiezero/ps5-jb-server/releases/tag/v1.5.0',
                        'body': 'Release notes', 'assets': [
                            {'name': self.asset, 'browser_download_url': self.base + self.asset, 'size': len(self.payload)},
                            {'name': self.asset + '.sha256', 'browser_download_url': self.base + self.asset + '.sha256'}]}
        self.descriptor = {'schema': 1, 'program_dir': str(self.program), 'static_dir': str(self.program / 'web'),
                           'data_dir': str(self.data), 'config_path': str(self.root / 'config.toml'),
                           'admin_unit': 'ps5-jb-server.service', 'repo_unit': 'ps5-jb-repo.service',
                           'health_url': 'https://127.0.0.1:8443/api/version'}
        (self.program / 'update-install.json').write_text(json.dumps(self.descriptor))
        self.calls = []
        self.launches = []
        self.manager = self.make_manager()

    def fetch(self, url, limit):
        self.calls.append(url)
        if url.endswith('/releases/latest'):
            return json.dumps(self.release).encode()
        if url.endswith('.sha256'):
            return (hashlib.sha256(self.payload).hexdigest() + '  ' + self.asset + '\n').encode()
        if url == self.base + self.asset:
            return self.payload
        raise AssertionError(url)

    def make_manager(self, **kwargs):
        return UpdateManager(self.data, self.program, fetch=self.fetch,
                             launcher=lambda spec: self.launches.append(spec), **kwargs)

    def test_semver_is_numeric_and_never_downgrades(self):
        self.assertTrue(newer('1.10.0', '1.9.9'))
        self.assertFalse(newer('1.4.0', '1.4.0'))
        self.assertFalse(newer('1.3.9', '1.4.0'))
        with self.assertRaises(ValueError):
            newer('1.5.0-rc1', '1.4.0')

    def test_status_needs_no_network_and_official_release_check_populates_latest(self):
        self.assertEqual(self.manager.status()['current'], '1.4.0')
        self.assertEqual(self.calls, [])
        status = self.manager.check()
        self.assertEqual((status['latest'], status['available']), ('1.5.0', True))
        self.assertEqual(status['notes'], 'Release notes')

    def test_no_releases_and_network_errors_are_different(self):
        with mock.patch.object(self.manager, 'fetch', side_effect=urllib.error.HTTPError('', 404, '', {}, None)):
            result = self.manager.check()
            self.assertIsNone(result['latest'])
            self.assertEqual(result['check_error'], '')
        with mock.patch.object(self.manager, 'fetch', side_effect=OSError('offline')):
            result = self.manager.check()
            self.assertIn('offline', result['check_error'])
            self.assertFalse(result['available'])

    def test_untrusted_asset_url_and_prerelease_cannot_be_applied(self):
        for change in ('url', 'prerelease', 'missing_checksum'):
            with self.subTest(change=change):
                original = json.loads(json.dumps(self.release))
                if change == 'url':
                    self.release['assets'][0]['browser_download_url'] = 'https://evil.example/archive'
                elif change == 'prerelease':
                    self.release['prerelease'] = True
                else:
                    self.release['assets'] = self.release['assets'][:1]
                status = self.manager.check()
                self.assertFalse(status['available'])
                self.assertTrue(status['check_error'])
                self.release = original

    def test_apply_requires_the_checked_version_and_no_active_transfers(self):
        self.manager.check()
        with self.assertRaises(UpdateError):
            self.manager.apply('1.6.0')
        busy = self.make_manager(busy=lambda: True)
        busy.check()
        with self.assertRaisesRegex(UpdateError, '传输|任务'):
            busy.apply('1.5.0')

    def test_verified_download_launches_worker_and_status_survives_restart(self):
        self.manager.check()
        self.manager.apply('1.5.0', background=False)
        self.assertEqual(len(self.launches), 1)
        spec = json.loads(Path(self.launches[0]).read_text())
        self.assertEqual(Path(spec['archive']).read_bytes(), self.payload)
        self.assertEqual(spec['admin_unit'], 'ps5-jb-server.service')
        self.assertEqual(self.make_manager().status()['job']['version'], '1.5.0')
        with self.assertRaises(UpdateError):
            self.manager.apply('1.5.0')

    def test_checksum_mismatch_never_launches(self):
        self.manager.check()
        original = self.manager.fetch
        self.manager.fetch = lambda url, limit: b'corrupt' if url == self.base + self.asset else original(url, limit)
        self.manager.apply('1.5.0', background=False)
        self.assertEqual(self.manager.status()['job']['state'], 'failed')
        self.assertEqual(self.launches, [])

    def test_terminal_transaction_recovers_status_after_worker_crash(self):
        self.manager.check()
        ident = 'a' * 32
        atomic = self.data / 'updates'
        atomic.mkdir(parents=True, exist_ok=True)
        (atomic / 'status.json').write_text(json.dumps({'id': ident, 'state': 'verifying', 'version': '1.5.0',
                                                         'updated_at': 0}))
        (atomic / 'transaction.json').write_text(json.dumps({'phase': 'committed'}))
        self.manager.launcher = self.manager._launch
        with mock.patch('updates.subprocess.run', return_value=type('R', (), {'stdout': 'inactive\n', 'returncode': 0})()):
            self.assertEqual(self.manager.status()['job']['state'], 'done')
        (atomic / 'status.json').write_text(json.dumps({'id': ident, 'state': 'verifying', 'version': '1.5.0',
                                                         'updated_at': 0}))
        (atomic / 'transaction.json').write_text(json.dumps({'phase': 'restored'}))
        with mock.patch('updates.subprocess.run', return_value=type('R', (), {'stdout': 'inactive\n', 'returncode': 0})()):
            self.assertEqual(self.manager.status()['job']['state'], 'rolled_back')

    def test_raw_console_write_blocks_upgrade_and_upgrade_blocks_new_write(self):
        self.manager.check()
        self.manager.begin_operation()
        with self.assertRaisesRegex(UpdateError, '传输|任务'):
            self.manager.apply('1.5.0')
        self.manager.end_operation()
        self.manager.apply('1.5.0', background=False)
        with self.assertRaisesRegex(UpdateError, '升级'):
            self.manager.begin_operation()

    def test_completed_update_no_longer_offers_same_version(self):
        self.manager.check()
        for installed in ('1.5.0', '1.6.0'):
            with mock.patch('updates.VERSION', installed):
                self.assertFalse(self.manager.status()['available'])
                with self.assertRaises(UpdateError):
                    self.manager.apply('1.5.0', background=False)

    def test_launcher_timeout_does_not_unlock_a_second_update(self):
        import subprocess
        self.manager.check()
        def launched_but_unconfirmed(spec):
            self.launches.append(spec)
            raise subprocess.TimeoutExpired('systemd-run', 15)
        self.manager.launcher = launched_but_unconfirmed
        self.manager.apply('1.5.0', background=False)
        self.assertEqual(self.manager.status()['job']['state'], 'validating')
        with self.assertRaises(UpdateError):
            self.manager.apply('1.5.0', background=False)
        self.assertEqual(len(self.launches), 1)

    def test_status_reports_the_environment_proxy_without_any_network_call(self):
        with mock.patch.dict(os.environ, {'https_proxy': 'http://192.0.2.50:6152'}, clear=True):
            state = self.manager.status()['proxy']
        self.assertEqual((state['configured'], state['variable'], state['url']),
                         (True, 'https_proxy', 'http://192.0.2.50:6152'))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(self.manager.status()['proxy']['configured'])

    def test_network_failure_suggests_a_proxy_only_when_none_is_configured(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(self.manager, 'fetch', side_effect=OSError('offline')):
                self.assertIn('HTTPS_PROXY', self.manager.check()['check_error'])
        with mock.patch.dict(os.environ, {'HTTPS_PROXY': 'http://192.0.2.50:6152'}, clear=True):
            with mock.patch.object(self.manager, 'fetch', side_effect=OSError('offline')):
                self.assertNotIn('HTTP', self.manager.check()['check_error'])

    def test_downloads_use_the_shared_proxy_aware_opener(self):
        import updates
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'payload'
        with mock.patch.object(updates.netproxy, 'opener', return_value=opener) as build:
            self.assertEqual(updates.fetch_bytes(
                'https://api.github.com/repos/wujiezero/ps5-jb-server/releases/latest', 64), b'payload')
        self.assertIs(build.call_args.kwargs['redirect'], updates.GithubRedirect)

    def test_source_checkout_cannot_self_update(self):
        (self.program / 'update-install.json').unlink()
        self.manager.check()
        self.assertFalse(self.manager.status()['supported'])
        with self.assertRaises(UpdateError):
            self.manager.apply('1.5.0')


if __name__ == '__main__':
    unittest.main()
