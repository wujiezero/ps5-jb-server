import http.client
import json
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import server
from server import AdminHandler, AdminServer, AuthManager, RepoManager

# Repository URLs are built from the base URL injected by the caller (server
# main passes it from Settings), not from a hardcoded legacy deployment host.
REPO_BASE = 'http://127.0.0.1:8080/payloads/'

PASSWORD = 'a-long-test-password'
NEW_PASSWORD = 'another-long-password'


class AuthAndRepo:
    pass


class PasswordTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(getattr(server, 'change_password', None), 'change_password is required')
        self.tmp = tempfile.TemporaryDirectory()
        self.creds = Path(self.tmp.name) / 'credentials.json'
        self.creds.write_text(json.dumps(AuthManager.make_credentials(PASSWORD)))
        self.auth = AuthManager(json.loads(self.creds.read_text()))

    def tearDown(self):
        self.tmp.cleanup()

    def test_requires_valid_current_password(self):
        with self.assertRaises(PermissionError):
            server.change_password(self.auth, self.creds, 'wrong-current', NEW_PASSWORD)
        self.assertTrue(self.auth.verify(PASSWORD))

    def test_new_password_effective_and_old_rejected(self):
        server.change_password(self.auth, self.creds, PASSWORD, NEW_PASSWORD)
        reloaded = AuthManager(json.loads(self.creds.read_text()))
        self.assertTrue(reloaded.verify(NEW_PASSWORD))
        self.assertFalse(reloaded.verify(PASSWORD))

    def test_short_password_rejected(self):
        for short in ('short', 'x' * 9):
            with self.assertRaises(ValueError):
                server.change_password(self.auth, self.creds, PASSWORD, short)
        self.assertTrue(self.auth.verify(PASSWORD))

    def test_ten_character_password_accepted(self):
        server.change_password(self.auth, self.creds, PASSWORD, 'x' * 10)
        self.assertTrue(AuthManager(json.loads(self.creds.read_text())).verify('x' * 10))

    def test_existing_sessions_invalidated(self):
        token, _ = self.auth.login('127.0.0.1', PASSWORD)
        server.change_password(self.auth, self.creds, PASSWORD, NEW_PASSWORD)
        self.assertIsNone(self.auth.session(token))


class RepoTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(getattr(server, 'RepoManager', None), 'RepoManager is required')
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = RepoManager(self.root, base_url=REPO_BASE)
        self.payload = b'\x7fELF' + b'x' * 2048

    def tearDown(self):
        self.tmp.cleanup()

    def test_starts_with_created_catalog(self):
        catalog = self.repo.catalog()
        self.assertEqual(catalog['payloads'][0]['filename'], 'ps5-payload-repository')
        self.assertEqual(catalog['payloads'][0]['url'], 'https://example.invalid/repo.json')

    def test_upload_adds_file_entry_and_checksum(self):
        entry = self.repo.add('demo.elf', self.payload, name='Demo', version='v1', category='Files')
        self.assertTrue((self.root / 'payloads' / 'demo.elf').exists())
        self.assertEqual(len(entry['checksum']), 64)
        self.assertEqual(entry['url'], REPO_BASE + 'demo.elf')
        self.assertIn('demo.elf', [p['filename'] for p in self.repo.catalog()['payloads']])

    def test_upload_rejects_unsafe_filename(self):
        for bad in ['../evil.elf', 'a/b.elf', 'x.exe', '', 'a b.elf']:
            with self.assertRaises(ValueError):
                self.repo.add(bad, self.payload, name='X')

    def test_upload_rejects_duplicate_and_oversize(self):
        self.repo.add('demo.elf', self.payload, name='Demo')
        with self.assertRaises(FileExistsError):
            self.repo.add('demo.elf', self.payload, name='Demo')
        with self.assertRaises(ValueError):
            self.repo.add('big.elf', b'x' * (RepoManager.MAX_UPLOAD + 1), name='Big')

    def test_add_replaces_seed_catalog(self):
        self.repo.add('demo.elf', self.payload, name='Demo')
        payloads = self.repo.catalog()['payloads']
        self.assertEqual([p['filename'] for p in payloads], ['demo.elf'])

    def test_delete_removes_file_and_entry(self):
        self.repo.add('demo.elf', self.payload, name='Demo')
        self.repo.delete('demo.elf')
        self.assertFalse((self.root / 'payloads' / 'demo.elf').exists())
        self.assertEqual(self.repo.catalog()['payloads'], [])
        with self.assertRaises(FileNotFoundError):
            self.repo.delete('demo.elf')

    def test_delete_refuses_traversal(self):
        with self.assertRaises(ValueError):
            self.repo.delete('../repo.json')

    def test_update_metadata_only(self):
        self.repo.add('demo.elf', self.payload, name='Demo', version='v1')
        self.repo.update('demo.elf', name='Renamed', version='v2', category='System')
        item = self.repo.catalog()['payloads'][0]
        self.assertEqual((item['name'], item['version'], item['category']), ('Renamed', 'v2', 'System'))
        with self.assertRaises(FileNotFoundError):
            self.repo.update('missing.elf', name='X')

    def test_catalog_write_is_atomic_and_keeps_name(self):
        self.repo.add('demo.elf', self.payload, name='Demo')
        text = (self.root / 'repo.json').read_text()
        self.assertIn('家庭离线仓库', text)
        leftovers = [p for p in self.root.iterdir() if p.name.startswith('.repo.json')]
        self.assertEqual(leftovers, [])


class RepoReloadTests(unittest.TestCase):
    def test_repo_server_sees_new_catalog_without_restart(self):
        import importlib
        repo_mod = importlib.import_module('payload_repo')
        with tempfile.TemporaryDirectory() as tmp:
            mgr = RepoManager(Path(tmp), base_url=REPO_BASE)
            mgr.add('demo.elf', b'\x7fELF' + b'y' * 512, name='Demo')
            srv = repo_mod.Server(('127.0.0.1', 0), tmp)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                conn = http.client.HTTPConnection(*srv.server_address, timeout=5)
                conn.request('GET', '/payloads/demo.elf')
                self.assertEqual(conn.getresponse().status, 200)
                conn.close()
                mgr.add('second.elf', b'\x7fELF' + b'z' * 512, name='Second')
                time.sleep(0.05)
                conn = http.client.HTTPConnection(*srv.server_address, timeout=5)
                conn.request('GET', '/payloads/second.elf')
                self.assertEqual(conn.getresponse().status, 200)
                conn.close()
            finally:
                srv.shutdown()
                srv.server_close()


class HTTPIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        (root / 'data').mkdir()
        (root / 'data' / 'credentials.json').write_text(json.dumps(AuthManager.make_credentials(PASSWORD)))
        self.repo = RepoManager(root / 'repo', base_url=REPO_BASE)
        self.server = AdminServer(('127.0.0.1', 0), AdminHandler)
        self.server.auth = AuthManager(json.loads((root / 'data' / 'credentials.json').read_text()))
        self.server.credentials_path = root / 'data' / 'credentials.json'
        self.server.repo = self.repo
        self.server.app = FakeApp()
        self.server.static_dir = Path(__file__).resolve().parents[1] / 'web'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, headers=None, raw=False):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        payload = body
        if body is not None and not raw:
            payload = json.dumps(body).encode()
        hdrs = {'Content-Type': 'application/octet-stream' if raw else 'application/json'}
        hdrs.update(headers or {})
        conn.request(method, path, payload, hdrs)
        response = conn.getresponse()
        data = response.read()
        info = dict(response.getheaders())
        conn.close()
        try:
            parsed = json.loads(data) if data else {}
        except ValueError:
            parsed = {}
        return response.status, info, parsed

    def login(self):
        status, headers, body = self.call('POST', '/api/login', {'password': PASSWORD})
        self.assertEqual(status, 200)
        return headers['Set-Cookie'].split(';')[0], body['csrf']

    def test_password_endpoint_requires_csrf_and_changes_login(self):
        cookie, csrf = self.login()
        status, _, _ = self.call('POST', '/api/password', {'current_password': PASSWORD, 'new_password': NEW_PASSWORD}, {'Cookie': cookie})
        self.assertEqual(status, 403)
        status, _, _ = self.call('POST', '/api/password',
                                 {'current_password': PASSWORD, 'new_password': NEW_PASSWORD},
                                 {'Cookie': cookie, 'X-CSRF-Token': csrf})
        self.assertEqual(status, 200)
        self.assertEqual(self.call('GET', '/api/state', headers={'Cookie': cookie})[0], 401)
        self.assertEqual(self.call('POST', '/api/login', {'password': NEW_PASSWORD})[0], 200)
        self.assertEqual(self.call('POST', '/api/login', {'password': PASSWORD})[0], 401)

    def test_password_rejects_wrong_current(self):
        cookie, csrf = self.login()
        status, _, body = self.call('POST', '/api/password',
                                    {'current_password': 'nope', 'new_password': NEW_PASSWORD},
                                    {'Cookie': cookie, 'X-CSRF-Token': csrf})
        self.assertEqual(status, 403)
        self.assertIn('密码', body.get('error', ''))

    def test_repo_list_upload_update_delete(self):
        cookie, csrf = self.login()
        status, _, body = self.call('GET', '/api/repo', headers={'Cookie': cookie})
        self.assertEqual(status, 200)
        self.assertEqual(body['catalog']['items'][0]['filename'], 'ps5-payload-repository')
        self.assertEqual(body['host'], '127.0.0.1')
        self.assertEqual(body['port'], 8080)
        status, _, _ = self.call('POST', '/api/repo/upload?filename=demo.elf&name=Demo&category=Files',
                                 b'\x7fELF' + b'q' * 1024,
                                 {'Cookie': cookie, 'X-CSRF-Token': csrf}, raw=True)
        self.assertEqual(status, 200)
        self.assertTrue((self.root / 'repo' / 'payloads' / 'demo.elf').exists())
        self.assertEqual(self.call('POST', '/api/repo/upload?filename=demo.elf&name=Demo',
                                   b'\x7fELF', {'Cookie': cookie, 'X-CSRF-Token': csrf}, raw=True)[0], 409)
        self.assertEqual(self.call('POST', '/api/repo/update', {'filename': 'demo.elf', 'name': 'Renamed'},
                                   {'Cookie': cookie, 'X-CSRF-Token': csrf})[0], 200)
        self.assertEqual(self.repo.catalog()['payloads'][0]['name'], 'Renamed')
        self.assertEqual(self.call('POST', '/api/repo/delete', {'filename': 'demo.elf'},
                                   {'Cookie': cookie, 'X-CSRF-Token': csrf})[0], 200)
        self.assertEqual(self.repo.catalog()['payloads'], [])

    def test_repo_requires_auth(self):
        self.assertEqual(self.call('GET', '/api/repo')[0], 401)
        self.assertEqual(self.call('POST', '/api/repo/delete', {'filename': 'x'})[0], 401)

    def test_repo_rejects_bad_upload(self):
        cookie, csrf = self.login()
        headers = {'Cookie': cookie, 'X-CSRF-Token': csrf}
        self.assertEqual(self.call('POST', '/api/repo/upload?filename=../evil.elf', b'x', headers, raw=True)[0], 400)
        self.assertEqual(self.call('POST', '/api/repo/upload',
                                   b'\x7fELF', {**headers, 'Content-Length': '99999999'}, raw=True)[0] in (400, 413), True)


class FakeSettings:
    """Loopback-only bind: the admin guard reads subnet and host_ip."""

    def __init__(self):
        self.subnet = '127.0.0.1/32'
        self.host_ip = '127.0.0.1'


class FakeApp:
    def __init__(self):
        self.settings = FakeSettings()

    def state(self):
        return {'config': {'domains': [], 'networks': [], 'upstream_dns': '192.0.2.1'}, 'logs': []}

    def change(self, body=None, rollback=False):
        pass


if __name__ == '__main__':
    unittest.main()
