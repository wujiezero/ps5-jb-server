"""NAS sources and NAS -> PS5 transfers (no real NAS or console needed)."""
import base64
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))

import ftp_files  # noqa: E402
from api_console import ConsoleAPI  # noqa: E402
from files import FileServiceError  # noqa: E402
from nas import NasError, NasStore, TransferManager, WebDavClient, clean_path  # noqa: E402
from settings import Settings  # noqa: E402

TREE = {'/': None, '/movies': None, '/movies/a b.mkv': b'A' * 3000, '/movies/sub': None,
        '/movies/sub/c.txt': b'hello', '/readme.txt': b'readme'}


class DavHandler(BaseHTTPRequestHandler):
    """Just enough WebDAV (PROPFIND depth 0/1, GET) behind Basic auth."""

    def log_message(self, *args):
        pass

    def _auth(self):
        expected = 'Basic ' + base64.b64encode(b'user:secret').decode()
        if self.headers.get('Authorization') != expected:
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="nas"')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return False
        return True

    def _path(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        assert path.startswith('/dav')
        return path[4:] or '/'

    def do_PROPFIND(self):
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        if not self._auth():
            return
        path = self._path().rstrip('/') or '/'
        if path not in TREE:
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        names = [path]
        if self.headers.get('Depth') == '1' and TREE[path] is None:
            prefix = path.rstrip('/') + '/'
            names += [key for key in TREE if key.startswith(prefix) and '/' not in key[len(prefix):]
                      and key != path]
        parts = []
        for name in names:
            href = urllib.parse.quote('/dav' + name + ('/' if TREE[name] is None and name != '/' else ''))
            kind = '<d:resourcetype><d:collection/></d:resourcetype>' if TREE[name] is None else '<d:resourcetype/>'
            size = '' if TREE[name] is None else f'<d:getcontentlength>{len(TREE[name])}</d:getcontentlength>'
            parts.append(f'<d:response><d:href>{href}</d:href><d:propstat><d:prop>{kind}{size}'
                         '<d:getlastmodified>Tue, 10 Nov 2026 01:00:00 GMT</d:getlastmodified>'
                         '</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')
        body = ('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">' + ''.join(parts) +
                '</d:multistatus>').encode()
        self.send_response(207)
        self.send_header('Content-Type', 'application/xml')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._auth():
            return
        data = TREE.get(self._path())
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class FakeClient:
    def __init__(self, tree, delay=0.0):
        self.tree, self.delay = tree, delay

    def list(self, path):
        prefix = path.rstrip('/') + '/'
        return [{'name': key[len(prefix):], 'path': key, 'dir': value is None,
                 'size': 0 if value is None else len(value), 'mtime': 0}
                for key, value in self.tree.items()
                if key.startswith(prefix) and '/' not in key[len(prefix):] and key != path]

    def stat(self, path):
        if path not in self.tree:
            raise NasError('missing', status=404)
        value = self.tree[path]
        return {'dir': value is None, 'size': 0 if value is None else len(value), 'mtime': 0}

    def open(self, path):
        data, delay = self.tree[path], self.delay

        class Reader:
            offset = 0

            def read(self, size):
                if delay:
                    time.sleep(delay)
                chunk = data[self.offset:self.offset + min(size, 1000)]
                self.offset += len(chunk)
                return chunk

            def close(self):
                pass
        return Reader()

    def close(self):
        pass


class FakePs5:
    """In-memory stand-in for FileProxy."""

    def __init__(self):
        self.files, self.dirs = {}, {'/data'}

    def list(self, path):
        if path not in self.dirs:
            raise FileServiceError('missing', status=404)
        prefix = path.rstrip('/') + '/'
        names = {key[len(prefix):].split('/')[0] for key in list(self.files) + list(self.dirs)
                 if key.startswith(prefix)}
        return {'path': path, 'entries': [{'name': name} for name in sorted(names)]}

    def mkdir(self, path):
        if path in self.dirs:
            raise FileServiceError('exists')
        self.dirs.add(path)

    def upload(self, directory, name, chunks, length, overwrite=False):
        self.files[directory + '/' + name] = b''.join(chunks)
        return {'ok': True}

    def delete(self, path):
        self.files.pop(path, None)


def wait(manager, ident, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        job = manager.snapshot_one(ident)
        if job['state'] in ('done', 'done_with_warnings', 'failed', 'canceled'):
            return job
        time.sleep(0.02)
    raise AssertionError('transfer did not finish')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = NasStore(Path(self.tmp.name) / 'nas.json')

    def tearDown(self):
        self.tmp.cleanup()

    def test_password_never_leaves_the_server(self):
        saved = self.store.save({'protocol': 'smb', 'host': '192.0.2.9', 'share': 'video',
                                 'username': 'me', 'password': 'p@ss'})
        self.assertNotIn('password', saved)
        self.assertTrue(saved['has_password'])
        self.assertNotIn('p@ss', json.dumps(self.store.list_public()))
        mode = (Path(self.tmp.name) / 'nas.json').stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        # Editing without retyping the password keeps it.
        self.store.save({'id': saved['id'], 'protocol': 'smb', 'host': '192.0.2.9', 'share': 'video',
                         'username': 'me', 'password': ''})
        self.assertEqual(self.store.get(saved['id'])['password'], 'p@ss')
        self.assertEqual(self.store.get(saved['id'])['port'], 445)

    def test_credentials_are_encrypted_at_rest(self):
        key = Path(self.tmp.name) / 'etc' / 'nas.key'
        store = NasStore(Path(self.tmp.name) / 'data.json', key)
        first = store.save({'protocol': 'smb', 'host': '192.0.2.9', 'username': 'alice-user',
                            'password': 'S3cret-Pass-123'})
        second = store.save({'protocol': 'ftp', 'host': '192.0.2.10', 'username': 'bob-user',
                             'password': 'Other-Pass-456'})
        raw = (Path(self.tmp.name) / 'data.json').read_text()
        for secret in ('alice-user', 'S3cret-Pass-123', 'bob-user', 'Other-Pass-456'):
            self.assertNotIn(secret, raw)
        self.assertNotIn('"password"', raw)
        self.assertNotIn('"username"', raw)
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertFalse((Path(self.tmp.name) / 'nas.key').exists(), 'key stays apart from the data')
        # A fresh process with the same key reads them back.
        again = NasStore(Path(self.tmp.name) / 'data.json', key)
        self.assertEqual(again.get(first['id'])['password'], 'S3cret-Pass-123')
        self.assertEqual(again.list_public()[0]['username'], 'alice-user')
        self.assertNotIn('S3cret-Pass-123', json.dumps(again.list_public()))
        # Ciphertext is bound to its connection: swapping entries fails.
        document = json.loads(raw)
        a, b = document['connections']
        a['credentials'], b['credentials'] = b['credentials'], a['credentials']
        (Path(self.tmp.name) / 'data.json').write_text(json.dumps(document))
        swapped = NasStore(Path(self.tmp.name) / 'data.json', key)
        with self.assertRaisesRegex(NasError, '重新填写'):
            swapped.get(first['id'])
        self.assertFalse(swapped.list_public()[0]['credentials_ok'])

    def test_wrong_key_asks_to_reenter(self):
        key = Path(self.tmp.name) / 'k1'
        store = NasStore(Path(self.tmp.name) / 'data.json', key)
        ident = store.save({'protocol': 'smb', 'host': '192.0.2.9', 'username': 'u', 'password': 'p'})['id']
        other = NasStore(Path(self.tmp.name) / 'data.json', Path(self.tmp.name) / 'k2')
        with self.assertRaises(NasError):
            other.get(ident)

    def test_plaintext_file_from_older_version_is_migrated(self):
        data = Path(self.tmp.name) / 'old.json'
        data.write_text(json.dumps({'connections': [{
            'id': 'abc123', 'name': 'old', 'protocol': 'smb', 'host': '192.0.2.9', 'port': 445,
            'share': '', 'path': '/', 'username': 'legacy-user', 'password': 'legacy-pass',
            'tls': False, 'verify_tls': False, 'workgroup': ''}]}))
        store = NasStore(data, Path(self.tmp.name) / 'mig.key')
        raw = data.read_text()
        self.assertNotIn('legacy-pass', raw)
        self.assertNotIn('legacy-user', raw)
        self.assertEqual(store.get('abc123')['password'], 'legacy-pass')

    def test_validation(self):
        for raw in ({'protocol': 'nfs', 'host': 'nas'}, {'protocol': 'smb', 'host': 'http://nas/x'},
                    {'protocol': 'smb', 'host': '127.0.0.1'}, {'protocol': 'ftp', 'host': 'nas', 'port': 70000},
                    {'protocol': 'smb', 'host': 'nas', 'share': 'a/b'},
                    {'protocol': 'webdav', 'host': 'nas', 'path': '/a/../b'}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.store.validate(raw)
        self.assertEqual(clean_path('a//b/./c/'), '/a/b/c')


class WebDavTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), DavHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.config = {'protocol': 'webdav', 'host': '127.0.0.1', 'port': cls.server.server_address[1],
                      'path': '/dav', 'username': 'user', 'password': 'secret'}

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_list_stat_open(self):
        client = WebDavClient(self.config, 5)
        names = sorted((item['name'], item['dir'], item['size']) for item in client.list('/movies'))
        self.assertEqual(names, [('a b.mkv', False, 3000), ('sub', True, 0)])
        self.assertEqual(client.stat('/movies/a b.mkv')['size'], 3000)
        self.assertTrue(client.stat('/movies')['dir'])
        with client.open('/movies/sub/c.txt') as response:
            self.assertEqual(response.read(), b'hello')

    def test_wrong_password(self):
        client = WebDavClient({**self.config, 'password': 'nope'}, 5)
        with self.assertRaisesRegex(NasError, '密码'):
            client.list('/')


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = NasStore(Path(self.tmp.name) / 'nas.json')
        self.ident = self.store.save({'protocol': 'ftp', 'host': '192.0.2.9'})['id']
        self.ps5 = FakePs5()

    def tearDown(self):
        self.tmp.cleanup()

    def manager(self, delay=0.0):
        return TransferManager(self.store, self.ps5, client_factory=lambda config: FakeClient(TREE, delay))

    def test_folder_and_file_with_progress_fields(self):
        manager = self.manager()
        job = wait(manager, manager.start(self.ident, ['/movies', '/readme.txt'], '/data')['id'])
        self.assertEqual(job['state'], 'done')
        self.assertEqual(self.ps5.files['/data/movies/a b.mkv'], TREE['/movies/a b.mkv'])
        self.assertEqual(self.ps5.files['/data/movies/sub/c.txt'], b'hello')
        self.assertEqual(self.ps5.files['/data/readme.txt'], b'readme')
        self.assertEqual((job['files_done'], job['files_total']), (3, 3))
        self.assertEqual(job['bytes_done'], job['bytes_total'])
        self.assertEqual(job['percent'], 100)
        for key in ('speed', 'eta', 'elapsed', 'started', 'finished'):
            self.assertIn(key, job)

    def test_speed_follows_many_small_reads(self):
        manager = self.manager()
        job = {'bytes_done': 0, 'bytes_total': 10 ** 9, 'current_done': 0, 'speed': 0,
               'percent': 0, 'eta': None, 'elapsed': 0, 'started': 1000.0}
        samples = __import__('collections').deque(maxlen=80)
        clock = [1000.0]
        with mock.patch('nas.time.time', side_effect=lambda: clock[0]):
            for _ in range(4000):           # 16 KiB every 0.25 ms = 64 MB/s for one second
                clock[0] += 0.00025
                manager._progress(job, 16000, 0, samples)
        self.assertAlmostEqual(job['speed'] / 64e6, 1, delta=0.05)

    def test_skip_existing_then_overwrite(self):
        self.ps5.dirs.add('/data/movies')
        self.ps5.files['/data/movies/a b.mkv'] = b'old'
        manager = self.manager()
        job = wait(manager, manager.start(self.ident, ['/movies'], '/data', 'skip')['id'])
        self.assertEqual(job['files_skipped'], 1)
        self.assertEqual(self.ps5.files['/data/movies/a b.mkv'], b'old')
        job = wait(manager, manager.start(self.ident, ['/movies'], '/data', 'overwrite')['id'])
        self.assertEqual(self.ps5.files['/data/movies/a b.mkv'], TREE['/movies/a b.mkv'])
        self.assertEqual(job['files_skipped'], 0)

    def test_cancel_removes_partial_file(self):
        manager = self.manager(delay=0.05)
        ident = manager.start(self.ident, ['/movies/a b.mkv'], '/data')['id']
        time.sleep(0.05)
        manager.cancel(ident)
        job = wait(manager, ident)
        self.assertEqual(job['state'], 'canceled')
        self.assertNotIn('/data/a b.mkv', self.ps5.files)

    def test_bad_requests(self):
        manager = self.manager()
        with self.assertRaises(ValueError):
            manager.start(self.ident, [], '/data')
        with self.assertRaises(ValueError):
            manager.start(self.ident, ['/x'], '/data', 'merge')
        with self.assertRaises(NasError):
            manager.start('missing', ['/x'], '/data')

    def test_transfer_registration_is_optional_and_runs_after_folder_transfer(self):
        class Services:
            def __init__(self):
                self.calls = []

            def register(self, host, game):
                self.calls.append((host, game))
                return {'state': 'done', 'title': game['title']}

        services = Services()
        manager = TransferManager(self.store, self.ps5,
                                   client_factory=lambda config: FakeClient(TREE),
                                   game_services=services)
        ident = manager.start(self.ident, ['/movies'], '/data',
                              auto_register=True, host='192.0.2.5', title='CUSA12345')['id']
        job = wait(manager, ident)
        self.assertEqual(job['state'], 'done')
        self.assertEqual(services.calls, [('192.0.2.5', {
            'title': 'CUSA12345', 'path': '/data/movies', 'name': 'CUSA12345'} )])

    def test_registration_failure_keeps_extracted_game_as_warning(self):
        class Services:
            def register(self, host, game):
                raise RuntimeError('engine offline')

        manager = TransferManager(self.store, self.ps5,
                                   client_factory=lambda config: FakeClient(TREE),
                                   game_services=Services())
        ident = manager.start(self.ident, ['/movies'], '/data',
                              auto_register=True, host='192.0.2.5', title='CUSA12345')['id']
        job = wait(manager, ident)
        self.assertEqual(job['state'], 'done_with_warnings')
        self.assertIn('engine offline', job['registration_error'])
        self.assertIn('/data/movies/a b.mkv', self.ps5.files)

    def test_auth_failures_are_not_401_for_the_page(self):
        settings = Settings.from_mapping({'network': {'host_ip': '127.0.0.1', 'subnet': '127.0.0.0/8'},
                                          'paths': {'data': self.tmp.name}})
        api = ConsoleAPI(settings, lambda: {'items': []})
        handler = mock.Mock()
        api._fail(handler, NasError('SMB 读取目录被拒绝', status=401))
        self.assertEqual(handler.send_body.call_args[0][0], 502)


class FtpHelpersTests(unittest.TestCase):
    def test_list_fallback_parses_unix_listing(self):
        conn = mock.Mock()
        conn.mlsd.side_effect = ftp_files.ftplib.error_perm('500 Unknown command')

        def retrlines(command, callback):
            for line in ('drwxr-xr-x 1 ftp ftp 0 Nov 09 12:32 games',
                         '-rw-r--r-- 1 ftp ftp 1380 Jan 02 2026 log file.txt',
                         'lrwxrwxrwx 1 ftp ftp 4 Nov 09 12:32 link -> games'):
                callback(line)
        conn.retrlines.side_effect = retrlines
        rows = dict(ftp_files.listing(conn, '/data'))
        self.assertEqual(rows['games']['type'], 'dir')
        self.assertEqual(rows['log file.txt']['size'], '1380')
        self.assertEqual(rows['log file.txt']['modify'], '20260102000000')
        self.assertIn('link', rows)

    def test_volume_capacity_comes_from_web_file_manager(self):
        files = ftp_files.FtpFiles('192.0.2.5', 2120, space_port=8888)
        body = json.dumps({'ok': True, 'spaces': [
            {'path': '/data', 'free': 400, 'total': 1000},
            {'path': '/mnt/ext0', 'free': 10, 'total': 20}]}).encode()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = body
        conn = mock.Mock()
        with mock.patch('urllib.request.urlopen', return_value=response), \
                mock.patch.object(files, 'connect', return_value=conn), \
                mock.patch.object(files, '_entries', return_value=([{'name': 'ext0', 'path': '/mnt/ext0',
                                                                      'dir': True}], False)):
            volumes = {v['path']: (v['free'], v['total']) for v in files.volumes()['volumes']}
        self.assertEqual(volumes, {'/data': (400, 1000), '/mnt/ext0': (10, 20)})


if __name__ == '__main__':
    unittest.main()
