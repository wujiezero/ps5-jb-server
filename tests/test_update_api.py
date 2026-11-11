"""HTTP authentication and CSRF boundaries for service upgrades."""
import sys
from pathlib import Path
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'server'), str(Path(__file__).resolve().parent)]
from test_server import HTTPTests
from updates import UpdateError


class FakeUpdates:
    def __init__(self):
        self.applied = []
        self.checked = 0
        self.state = 'done'

    def status(self):
        return {'current': '1.4.0', 'latest': '1.5.0', 'available': True,
                'job': {'state': self.state}}

    def check(self):
        self.checked += 1
        return self.status()

    def apply(self, version):
        if version != '1.5.0':
            raise UpdateError('请先检查更新')
        self.applied.append(version)
        return self.status()


class UpdateHTTPTests(HTTPTests):
    def setUp(self):
        super().setUp()
        self.app.updates = FakeUpdates()

    def test_public_version_does_not_require_github_or_login(self):
        code, _, result = self.request('GET', '/api/version')
        self.assertEqual(code, 200)
        self.assertEqual(result, {'version': '1.4.0', 'repository': 'wujiezero/ps5-jb-server', 'ready': True})
        self.assertEqual(self.app.updates.checked, 0)

    def test_version_health_reports_gateway_initialization_failure(self):
        from types import SimpleNamespace
        self.app.engine = SimpleNamespace(gateway_error='initialization failed')
        code, _, result = self.request('GET', '/api/version')
        self.assertEqual(code, 200)
        self.assertFalse(result['ready'])
        self.assertNotIn('initialization failed', str(result))

    def test_update_status_needs_session_and_check_needs_csrf(self):
        self.assertEqual(self.request('GET', '/api/updates')[0], 401)
        cookie, csrf = self.login()
        self.assertEqual(self.request('GET', '/api/updates', cookie=cookie)[2]['current'], '1.4.0')
        self.assertEqual(self.request('POST', '/api/updates/check', {}, cookie)[0], 403)
        self.assertEqual(self.request('POST', '/api/updates/check', {}, cookie, csrf)[0], 200)
        self.assertEqual(self.app.updates.checked, 1)

    def test_upgrade_accepts_version_not_arbitrary_commands(self):
        cookie, csrf = self.login()
        code, _, _ = self.request('POST', '/api/updates/apply', {'version': '1.5.0'}, cookie, csrf)
        self.assertEqual(code, 200)
        self.assertEqual(self.app.updates.applied, ['1.5.0'])
        code, _, _ = self.request('POST', '/api/updates/apply', {'url': 'https://evil.invalid/a'}, cookie, csrf)
        self.assertEqual(code, 400)

    def test_repository_upload_refused_during_upgrade(self):
        self.app.updates.state = 'installing'
        cookie, csrf = self.login()
        code, _, body = self.request('POST', '/api/repo/upload?filename=x.elf', {}, cookie, csrf)
        self.assertEqual(code, 409)
        self.assertIn('升级', body['error'])

    def test_new_transfers_refused_during_upgrade(self):
        self.app.updates.state = 'installing'
        cookie, csrf = self.login()
        code, _, body = self.request('POST', '/api/nas/transfer', {}, cookie, csrf)
        self.assertEqual(code, 409)
        self.assertIn('升级', body['error'])


if __name__ == '__main__':
    unittest.main()
