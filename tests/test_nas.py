"""NAS sources and NAS -> PS5 transfers (no real NAS or console needed)."""
import base64
import json
import collections
import contextlib
import stat
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
from nas import NasError, NasStore, TransferManager, WebDavClient, clean_path, put_file  # noqa: E402
import nas  # noqa: E402
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


def join_workers(timeout=5):
    """Wait for transfer workers: a job reads as finished before its thread has
    written transfers.json and removed its work files for the last time."""
    for thread in threading.enumerate():
        if thread.name == 'nas-transfer':
            thread.join(timeout)


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
        join_workers()
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
        store.save({'protocol': 'ftp', 'host': '192.0.2.10', 'username': 'bob-user',
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
        join_workers()
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


class TransferPersistenceTests(unittest.TestCase):
    """transfers.json: jobs survive a restart; interrupted ones can be retried (B3)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = NasStore(self.root / 'nas.json', self.root / 'nas.key')
        self.ident = self.store.save({'protocol': 'ftp', 'host': '192.0.2.9', 'username': 'nas-user',
                                      'password': 'Nas-Pass-789'})['id']
        self.ps5 = FakePs5()
        self.state = self.root / 'transfers.json'

    def tearDown(self):
        join_workers()
        self.tmp.cleanup()

    def manager(self, **kwargs):
        kwargs.setdefault('client_factory', lambda config: FakeClient(TREE))
        return TransferManager(self.store, self.ps5, state_path=self.state, **kwargs)

    def write_state(self, jobs):
        self.state.write_text(json.dumps({'schema': 1, 'jobs': jobs}))

    def job(self, ident, state, kind='nas', **extra):
        return {'id': ident, 'kind': kind, 'state': state, 'nas_id': self.ident, 'paths': ['/movies'],
                'dest': '/data', 'overwrite': 'overwrite', 'name': 'movies', 'started': 1.0,
                'finished': None, 'bytes_done': 10, 'bytes_total': 100, 'files_skipped': 0,
                'files_done': 0, 'files_failed': 0, 'files_total': 0, 'error': '', 'errors': [],
                'speed': 0, 'eta': None, 'percent': 10, 'elapsed': 0, 'current': '', 'current_done': 0,
                'current_size': 0, 'title': '', 'created': 1.0, **extra}

    def test_finished_jobs_are_read_back_after_restart(self):
        first = self.manager()
        done = wait(first, first.start(self.ident, ['/readme.txt'], '/data')['id'])
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        raw = self.state.read_text()
        for secret in ('nas-user', 'Nas-Pass-789'):
            self.assertNotIn(secret, raw)
        second = self.manager()
        again = second.snapshot_one(done['id'])
        self.assertEqual(again['state'], 'done')
        self.assertEqual(again['bytes_done'], done['bytes_done'])
        self.assertNotIn('samples', json.loads(raw)['jobs'][0])

    def test_running_and_queued_jobs_become_interrupted(self):
        self.write_state([self.job('run1', 'running'), self.job('wait1', 'queued'),
                          self.job('old1', 'done'), self.job('ext1', 'running', kind='extract',
                                                             engine='libarchive', staging='/data/.extract-ext1')])
        manager = self.manager()
        for ident in ('run1', 'wait1', 'ext1'):
            job = manager.snapshot_one(ident)
            self.assertEqual(job['state'], 'failed')
            self.assertTrue(job['interrupted'])
            self.assertEqual(job['error'], '服务重启时中断')
            self.assertIsNotNone(job['finished'])
        self.assertEqual(manager.snapshot_one('ext1')['staging'], '/data/.extract-ext1')
        self.assertEqual(manager.snapshot_one('old1')['state'], 'done')
        self.assertEqual(json.loads(self.state.read_text())['jobs'][0]['state'], 'failed', 'written back')
        import server
        console = mock.Mock()
        console.transfers = manager
        console.files.job.return_value = {}
        console.files.transfers.return_value = {'transfers': []}
        self.assertFalse(server.update_busy(console), 'interrupted jobs do not block upgrades')

    def test_retry_requeues_with_skip_and_links_the_original(self):
        self.write_state([self.job('run1', 'running')])
        self.ps5.dirs.add('/data/movies')
        self.ps5.files['/data/movies/a b.mkv'] = b'already there'
        manager = self.manager()
        retried = manager.retry('run1')
        self.assertNotEqual(retried['id'], 'run1')
        self.assertEqual(retried['overwrite'], 'skip')
        self.assertEqual(retried['retry_of'], 'run1')
        job = wait(manager, retried['id'])
        self.assertEqual(job['state'], 'done')
        self.assertEqual(job['files_skipped'], 1)
        self.assertEqual(self.ps5.files['/data/movies/a b.mkv'], b'already there')
        self.assertEqual(self.ps5.files['/data/movies/sub/c.txt'], b'hello')

    def test_retry_is_refused_for_extraction_and_unfinished_jobs(self):
        self.write_state([self.job('ext1', 'running', kind='extract', engine='libarchive'),
                          self.job('ok1', 'done')])
        manager = self.manager()
        with self.assertRaisesRegex(ValueError, '解压任务不支持重试'):
            manager.retry('ext1')
        with self.assertRaisesRegex(ValueError, '失败或已取消'):
            manager.retry('ok1')
        with self.assertRaises(NasError):
            manager.retry('missing')

    def test_damaged_state_file_starts_empty(self):
        for content in ('{not json', '[]', '{"jobs": "x"}', '{"jobs": [1, {"id": 3}]}'):
            with self.subTest(content=content):
                self.state.write_text(content)
                with self.assertLogs(level='WARNING') if content in ('{not json', '[]', '{"jobs": "x"}') \
                        else contextlib.nullcontext():
                    manager = self.manager()
                self.assertEqual(manager.snapshot(), [])

    def test_progress_is_written_at_most_every_few_seconds(self):
        manager = self.manager()
        writes = []
        with mock.patch('nas.atomic_json', side_effect=lambda *args, **kw: writes.append(args)):
            job = {'id': 'x', 'bytes_done': 0, 'bytes_total': 100, 'current_done': 0, 'speed': 0,
                   'percent': 0, 'eta': None, 'elapsed': 0, 'started': time.time()}
            samples = collections.deque(maxlen=80)
            manager._last_persist = 0.0
            for _ in range(50):
                manager._progress(job, 1, 0, samples)
            self.assertEqual(len(writes), 1)
            manager._set(job, state='running')
            self.assertEqual(len(writes), 2, 'state changes are written at once')

    def test_live_unrar_unit_is_followed_after_restart(self):
        work = self.root / 'extract'
        work.mkdir()
        (work / 'unr1.json').write_text('{}')
        status = work / 'unr1.status.json'
        status.write_text(json.dumps({'state': 'running', 'bytes_done': 40, 'bytes_total': 100}))

        class Handle:
            def __init__(self):
                self.ticks = 0

            def alive(self):
                self.ticks += 1
                if self.ticks == 3:
                    status.write_text(json.dumps({'state': 'done', 'final': True, 'bytes_done': 100,
                                                  'result_path': '/data/games/PPSA1', 'title': 'PPSA1'}))
                return self.ticks < 4

            def stop(self):
                pass

        class Runner:
            def attach(self, unit):
                self.unit = unit
                return Handle()
        runner = Runner()
        self.write_state([self.job('unr1', 'running', kind='extract', engine='unrar', bytes_done=30,
                                   title='PPSA1', mode='install', dest='/data/games')])
        manager = self.manager(work_dir=work, runner=runner)
        self.assertEqual(runner.unit, 'ps5-extract-unr1')
        job = wait(manager, 'unr1')
        self.assertEqual(job['state'], 'done', job.get('error'))
        self.assertEqual(job['result_path'], '/data/games/PPSA1')
        self.assertNotIn('interrupted', job)
        self.assertFalse((work / 'unr1.json').exists())

    def test_dead_unrar_unit_is_interrupted(self):
        work = self.root / 'extract'
        work.mkdir()
        (work / 'unr2.json').write_text('{}')

        class Runner:
            def attach(self, unit):
                return mock.Mock(alive=lambda: False)
        self.write_state([self.job('unr2', 'running', kind='extract', engine='unrar')])
        manager = self.manager(work_dir=work, runner=Runner())
        self.assertTrue(manager.snapshot_one('unr2')['interrupted'])


class FlakyClient(FakeClient):
    """FakeClient whose reads break after `fail_after` bytes, `failures` times."""

    def __init__(self, tree, failures=1, fail_after=1500, error=OSError, open_error=None):
        super().__init__(tree)
        self.failures, self.fail_after, self.error, self.open_error = failures, fail_after, error, open_error
        self.opened = []

    def open(self, path):
        return self.open_at(path, 0)

    def open_at(self, path, offset):
        self.opened.append((path, offset))
        if self.open_error:
            raise self.open_error
        data, client = self.tree[path], self

        class Reader:
            position = offset

            def read(self, size):
                if client.failures and self.position >= offset + client.fail_after:
                    client.failures -= 1
                    raise client.error('connection reset by peer')
                chunk = data[self.position:self.position + min(size, 1000)]
                self.position += len(chunk)
                return chunk

            def close(self):
                pass
        return Reader()


class ResumablePs5(FakePs5):
    """FakePs5 that keeps partial uploads and can continue them (like zftpd REST + STOR)."""

    def __init__(self, resume=True, ignore_rest=False):
        super().__init__()
        self.resume, self.ignore_rest, self.calls = resume, ignore_rest, []

    def supports_resume(self):
        return self.resume

    def file_size(self, path):
        return len(self.files[path]) if path in self.files else None

    def upload(self, directory, name, chunks, length, overwrite=False, offset=0):
        path = directory + '/' + name
        self.calls.append(offset)
        kept = self.files.get(path, b'')[:offset] if offset and not self.ignore_rest else b''
        self.files[path] = kept
        for chunk in chunks:                       # a broken source leaves the partial file behind
            self.files[path] += chunk
        return {'ok': True}

    def delete(self, path):
        self.files.pop(path, None)


class RetryTests(unittest.TestCase):
    """Automatic per-file retries and REST resume (B4)."""

    BIG = {'/big.bin': bytes(range(256)) * 20}       # 5120 bytes

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = NasStore(Path(self.tmp.name) / 'nas.json')
        self.ident = self.store.save({'protocol': 'ftp', 'host': '192.0.2.9'})['id']

    def tearDown(self):
        join_workers()
        self.tmp.cleanup()

    def run_job(self, client, ps5, retries=3):
        manager = TransferManager(self.store, ps5, client_factory=lambda config: client, retries=retries)
        manager.RETRY_DELAYS = (0, 0, 0)
        return wait(manager, manager.start(self.ident, ['/big.bin'], '/data')['id'])

    def test_connection_error_is_retried_and_counted_once(self):
        ps5 = ResumablePs5(resume=False)
        job = self.run_job(FlakyClient(self.BIG), ps5)
        self.assertEqual(job['state'], 'done', job['error'])
        self.assertEqual(job['retries'], 1)
        self.assertEqual(job['files_done'], 1)
        self.assertEqual(job['bytes_done'], job['bytes_total'])
        self.assertEqual(ps5.files['/data/big.bin'], self.BIG['/big.bin'])
        self.assertEqual(ps5.calls, [0, 0], 'without resume support the file is sent again')

    def test_resume_continues_from_the_partial_file(self):
        client = FlakyClient(self.BIG, fail_after=3000)
        ps5 = ResumablePs5()
        job = self.run_job(client, ps5)
        self.assertEqual(job['state'], 'done', job['error'])
        self.assertEqual(job['resumed'], 1)
        self.assertEqual(ps5.calls, [0, 3000])
        self.assertEqual(client.opened[-1], ('/big.bin', 3000))
        self.assertEqual(ps5.files['/data/big.bin'], self.BIG['/big.bin'])
        self.assertEqual(job['bytes_done'], 5120)

    def test_size_mismatch_after_resume_copies_again_from_the_start(self):
        ps5 = ResumablePs5(ignore_rest=True)       # a server that silently ignores REST
        job = self.run_job(FlakyClient(self.BIG, fail_after=3000), ps5)
        self.assertEqual(job['state'], 'done', job['error'])
        self.assertEqual(ps5.calls, [0, 3000, 0])
        self.assertEqual(ps5.files['/data/big.bin'], self.BIG['/big.bin'])
        self.assertEqual(job['bytes_done'], job['bytes_total'])

    def test_errors_that_cannot_improve_are_not_retried(self):
        for error in (NasError('missing', status=404), NasError('denied', status=401), ValueError('bad name')):
            with self.subTest(error=error):
                ps5 = ResumablePs5()
                job = self.run_job(FlakyClient(self.BIG, open_error=error), ps5)
                self.assertEqual(job['retries'], 0)
                self.assertEqual(job['files_failed'], 1)
                self.assertNotIn('/data/big.bin', ps5.files)

    def test_retries_run_out_and_zero_disables_them(self):
        ps5 = ResumablePs5(resume=False)
        job = self.run_job(FlakyClient(self.BIG, failures=10), ps5)
        self.assertEqual((job['retries'], job['files_failed']), (3, 1))
        self.assertEqual(job['bytes_done'], 0)
        self.assertNotIn('/data/big.bin', ps5.files, 'the partial file is removed at the end')
        job = self.run_job(FlakyClient(self.BIG, failures=1), ResumablePs5(), retries=0)
        self.assertEqual((job['retries'], job['files_failed']), (0, 1))

    def test_truncated_source_stream_counts_as_a_connection_error(self):
        class Short(FakeClient):
            calls = 0

            def open(self, path):
                Short.calls += 1
                reader = super().open(path)
                if Short.calls == 1:
                    reader.read = lambda size, original=reader.read: b'' if reader.offset >= 2000 else original(size)
                return reader
        job = self.run_job(Short(self.BIG), ResumablePs5(resume=False))
        self.assertEqual(job['state'], 'done', job['error'])
        self.assertEqual(job['retries'], 1)

    def test_cancel_during_the_backoff_wait_is_immediate(self):
        ps5 = ResumablePs5(resume=False)
        manager = TransferManager(self.store, ps5, client_factory=lambda config: FlakyClient(self.BIG, failures=10))
        manager.RETRY_DELAYS = (30, 30, 30)
        ident = manager.start(self.ident, ['/big.bin'], '/data')['id']
        for _ in range(200):
            if manager.snapshot_one(ident)['retries']:
                break
            time.sleep(0.01)
        started = time.time()
        manager.cancel(ident)
        job = wait(manager, ident)
        self.assertEqual(job['state'], 'canceled')
        self.assertLess(time.time() - started, 2)
        self.assertNotIn('/data/big.bin', ps5.files)

    def test_listeners_hear_each_finished_job_once_and_cannot_break_the_queue(self):
        heard = []
        manager = TransferManager(self.store, ResumablePs5(resume=False), client_factory=lambda config: FakeClient(self.BIG))
        manager.listeners.append(lambda job: (_ for _ in ()).throw(RuntimeError('listener bug')))
        manager.listeners.append(lambda job: heard.append((job['id'], job['state'])))
        first = manager.start(self.ident, ['/big.bin'], '/data')['id']
        second = manager.start(self.ident, ['/big.bin'], '/data')['id']
        wait(manager, first)
        wait(manager, second)
        self.assertEqual(heard, [(first, 'done'), (second, 'done')])

    def test_setting_limits_and_default(self):
        self.assertEqual(Settings.from_mapping({}).transfer_retries, 3)
        self.assertEqual(Settings.from_mapping({'limits': {'transfer_retries': 0}}).transfer_retries, 0)
        self.assertEqual(Settings.from_mapping({'limits': {'transfer_retries': 99}}).transfer_retries, 10)


class FtpResumeTests(unittest.TestCase):
    def test_feat_with_rest_stream_enables_resume_once_per_server(self):
        ftp_files._RESUME.clear()
        files = ftp_files.FtpFiles('192.0.2.5', 2120)
        conn = mock.Mock()
        conn.sendcmd.return_value = '211-Extensions supported:\n SIZE\n REST STREAM\n MLSD\n211 End'
        with mock.patch.object(files, 'connect', return_value=conn) as connect:
            self.assertTrue(files.supports_resume())
            self.assertTrue(files.supports_resume())
        self.assertEqual(connect.call_count, 1)
        ftp_files._RESUME.clear()
        conn.sendcmd.return_value = '211-Features:\n SIZE\n211 End'
        with mock.patch.object(files, 'connect', return_value=conn):
            self.assertFalse(files.supports_resume())
        ftp_files._RESUME.clear()

    def test_upload_at_offset_sends_rest(self):
        files = ftp_files.FtpFiles('192.0.2.5', 2120)
        conn = mock.Mock()

        def store(command, body, blocksize, rest=None):
            store.rest = rest
            while body.read(blocksize):
                pass
        conn.storbinary.side_effect = store
        with mock.patch.object(files, 'connect', return_value=conn):
            files.upload('/data/a.bin', iter([b'x' * 40]), 100, overwrite=True, offset=60)
            self.assertEqual(store.rest, 60)
            files.upload('/data/a.bin', iter([b'x' * 100]), 100, overwrite=True)
            self.assertIsNone(store.rest)
            with self.assertRaises(ValueError):
                files.upload('/data/a.bin', iter([b'x' * 10]), 100, overwrite=True, offset=60)

    def test_http_backend_refuses_offsets(self):
        from files import FileProxy
        proxy = FileProxy('192.0.2.0/24', host='192.0.2.5')
        with mock.patch.object(proxy, '_active', return_value=None):
            self.assertFalse(proxy.supports_resume())
            with self.assertRaises(FileServiceError):
                proxy.upload('/data', 'a.bin', iter([b'x']), 2, offset=1)


def cheat_zip(entries):
    import io
    import zipfile
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return data.getvalue()


class FakePkgHost:
    enabled = True

    def __init__(self):
        self.published, self.revoked = [], []

    def publish(self, job, nas_id, path, size, mtime, allowed):
        self.published.append((job, nas_id, path, size, set(allowed)))
        return 'tok', 'http://192.0.2.1:8090/p/tok.pkg'

    def revoke(self, token):
        self.revoked.append(token)

    def served(self, token):
        return 0


class FakeInstaller:
    """GameServices stand-in: replays statuses through progress, then returns a result."""

    def __init__(self, result, statuses=(), wait_for_cancel=False):
        self.result, self.statuses, self.wait_for_cancel = result, statuses, wait_for_cancel
        self.calls = []

    def package_install(self, host, source, timeout=None, progress=None, cancel=None):
        self.calls.append((host, source, timeout))
        for status in self.statuses:
            progress(status)
        if self.wait_for_cancel:
            cancel.wait(5)
            return {'state': 'canceled', 'job': 'e1'}
        return dict(self.result)


class PackageJobTests(unittest.TestCase):
    TREE = {'/': None, '/games': None, '/games/CUSA12345.pkg': b'P' * 4096,
            '/games/CUSA12345 cheat.zip': cheat_zip({'json/CUSA12345_01.00.json': '{}',
                                                     'shn/CUSA12345_01.00.shn': '<x/>',
                                                     '../evil/CUSA12345.mc4': 'x'}),
            '/games/huge cheat.zip': b'Z' * 100}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = NasStore(Path(self.tmp.name) / 'nas.json')
        self.ident = self.store.save({'protocol': 'ftp', 'host': '192.0.2.9'})['id']
        self.ps5 = FakePs5()
        self.pkg = FakePkgHost()

    def manager(self, installer, **extra):
        return TransferManager(self.store, self.ps5, client_factory=lambda config: FakeClient(self.TREE),
                               game_services=installer, pkg_host=self.pkg, host='192.0.2.5', **extra)

    def test_installed_package_is_done_and_token_revoked(self):
        installer = FakeInstaller({'state': 'done', 'verdict': 'installed', 'job': 'e1'},
                                  statuses=[{'phase': 'deliver', 'job': 'e1', 'metrics': {'served_bytes': 2048}},
                                            {'phase': 'install', 'job': 'e1'}])
        manager = self.manager(installer)
        job = wait(manager, manager.start_package(self.ident, '/games/CUSA12345.pkg')['id'])
        self.assertEqual((job['kind'], job['state'], job['phase'], job['percent']), ('package', 'done', 'done', 100))
        self.assertEqual(job['bytes_done'], 4096)
        self.assertEqual(job['engine_job'], 'e1')
        self.assertEqual(self.pkg.published[0][2:], ('/games/CUSA12345.pkg', 4096, {'192.0.2.5'}))
        self.assertEqual(self.pkg.revoked, ['tok'])
        host, source, timeout = installer.calls[0]
        self.assertEqual((host, source), ('192.0.2.5', {'url': 'http://192.0.2.1:8090/p/tok.pkg'}))
        self.assertGreaterEqual(timeout, 600)

    def test_may_not_launch_is_done_with_warnings(self):
        manager = self.manager(FakeInstaller({'state': 'warning', 'verdict': 'may_not_launch',
                                              'warning': '可能无法启动'}))
        job = wait(manager, manager.start_package(self.ident, '/games/CUSA12345.pkg')['id'])
        self.assertEqual(job['state'], 'done_with_warnings')
        self.assertEqual(job['verdict'], 'may_not_launch')
        self.assertIn('无法启动', job['warning'])

    def test_busy_engine_fails_retryably(self):
        installer = FakeInstaller({'state': 'conflict', 'error_code': 'busy'})
        manager = self.manager(installer)
        job = wait(manager, manager.start_package(self.ident, '/games/CUSA12345.pkg')['id'])
        self.assertEqual((job['state'], job['error_code']), ('failed', 'busy'))
        self.assertIn('正忙', job['error'])
        self.assertEqual(self.pkg.revoked, ['tok'])
        installer.result = {'state': 'done', 'verdict': 'installed'}
        again = wait(manager, manager.retry(job['id'])['id'])
        self.assertEqual((again['state'], again['retry_of'], again['kind']), ('done', job['id'], 'package'))

    def test_failed_install_reports_the_engine_reason(self):
        manager = self.manager(FakeInstaller({'state': 'failed', 'error_code': 'status_failed',
                                              'reason': 'content id mismatch'}))
        job = wait(manager, manager.start_package(self.ident, '/games/CUSA12345.pkg')['id'])
        self.assertEqual(job['state'], 'failed')
        self.assertIn('content id mismatch', job['error'])

    def test_cancel_stops_the_install(self):
        manager = self.manager(FakeInstaller({}, wait_for_cancel=True))
        ident = manager.start_package(self.ident, '/games/CUSA12345.pkg')['id']
        deadline = time.time() + 5
        while manager.snapshot_one(ident)['state'] != 'running' and time.time() < deadline:
            time.sleep(0.01)
        manager.cancel(ident)
        self.assertEqual(wait(manager, ident)['state'], 'canceled')
        self.assertEqual(self.pkg.revoked, ['tok'])

    def test_bad_package_requests(self):
        manager = self.manager(FakeInstaller({}))
        for path in ('/games/CUSA12345 cheat.zip', '/games'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                manager.start_package(self.ident, path)
        with self.assertRaises(NasError):
            manager.start_package(self.ident, '/games/missing.pkg')
        with self.assertRaisesRegex(ValueError, 'ps5upload'):
            TransferManager(self.store, self.ps5).start_package(self.ident, '/games/CUSA12345.pkg')

    def test_cheat_import_writes_eta_hen_layout_and_ignores_traversal(self):
        manager = self.manager(None)
        result = manager.import_cheats(FakeClient(self.TREE), ['/games/CUSA12345 cheat.zip'], title='CUSA12345')
        self.assertEqual(sorted(result['written']), ['/data/etaHEN/cheats/json/CUSA12345_01.00.json',
                                                     '/data/etaHEN/cheats/shn/CUSA12345_01.00.shn'])
        self.assertEqual(self.ps5.files['/data/etaHEN/cheats/shn/CUSA12345_01.00.shn'], b'<x/>')
        self.assertFalse([path for path in self.ps5.files if 'evil' in path])
        self.assertIn('重新加载', result['note'])

    def test_cheat_zip_size_limit(self):
        manager = self.manager(None)
        manager.CHEAT_ZIP_LIMIT = 50
        with self.assertRaisesRegex(ValueError, '16 MiB'):
            manager.import_cheats(FakeClient(self.TREE), ['/games/huge cheat.zip'])

    def test_failed_cheat_step_only_warns(self):
        manager = self.manager(None)
        job = {'id': 'x', 'state': 'running', 'cheats': ['/games/huge cheat.zip']}
        manager._import_cheats(job, FakeClient(self.TREE), 'CUSA12345')
        self.assertEqual(job['state'], 'done_with_warnings')
        self.assertIn('金手指导入失败', job['cheat_error'])
        job = {'id': 'y', 'state': 'running', 'cheats': ['/games/CUSA12345 cheat.zip']}
        manager._import_cheats(job, FakeClient(self.TREE), 'CUSA12345')
        self.assertEqual(job['state'], 'running')
        self.assertEqual(len(job['cheat_result']['written']), 2)

    def test_extract_accepts_only_zip_cheats(self):
        manager = self.manager(None)
        with self.assertRaisesRegex(ValueError, 'ZIP'):
            manager.start_extract(self.ident, '/games/a.rar', '/data', cheats=['/games/a.txt'])


class WriteDav(BaseHTTPRequestHandler):
    """In-memory WebDAV with writes (server.tree: path -> bytes or None for folders)."""

    def log_message(self, *args):
        pass

    def _path(self, raw=None):
        path = urllib.parse.unquote(urllib.parse.urlsplit(raw or self.path).path)
        return (path[4:] or '/').rstrip('/') or '/'

    def _reply(self, code, body=b''):
        self.send_response(code)
        if code == 401:
            self.send_header('WWW-Authenticate', self.server.challenge)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        expected = 'Basic ' + base64.b64encode(b'user:secret').decode()
        self.server.log.append((self.command, self._path(), self.headers.get('Authorization') == expected))
        if self.headers.get('Authorization') != expected:
            self.close_connection = True
            self._reply(401)
            return False
        return True

    def do_PROPFIND(self):
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        if not self._auth():
            return
        tree, path = self.server.tree, self._path()
        if path not in tree:
            return self._reply(404)
        names = [path]
        if self.headers.get('Depth') == '1' and tree[path] is None:
            prefix = path.rstrip('/') + '/'
            names += [k for k in tree if k.startswith(prefix) and '/' not in k[len(prefix):] and k != path]
        parts = []
        for name in names:
            kind = '<d:resourcetype><d:collection/></d:resourcetype>' if tree[name] is None else '<d:resourcetype/>'
            size = '' if tree[name] is None else f'<d:getcontentlength>{len(tree[name])}</d:getcontentlength>'
            parts.append(f'<d:response><d:href>{urllib.parse.quote("/dav" + name)}</d:href><d:propstat><d:prop>'
                         f'{kind}{size}</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')
        self._reply(207, ('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">' + ''.join(parts)
                          + '</d:multistatus>').encode())

    def do_MKCOL(self):
        if not self._auth():
            return
        path = self._path()
        if path in self.server.tree:
            return self._reply(405)
        self.server.tree[path] = None
        self._reply(201)

    def do_PUT(self):
        if not self._auth():
            return
        self.server.tree[self._path()] = self.rfile.read(int(self.headers['Content-Length']))
        self._reply(201)

    def do_MOVE(self):
        if not self._auth():
            return
        tree, source = self.server.tree, self._path()
        target = self._path(self.headers['Destination'])
        if source not in tree:
            return self._reply(404)
        tree[target] = tree.pop(source)
        self._reply(201)

    def do_DELETE(self):
        if not self._auth():
            return
        if self.server.tree.pop(self._path(), 'missing') == 'missing':
            return self._reply(404)
        self._reply(204)


class WebDavWriteTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), WriteDav)
        self.server.tree, self.server.log, self.server.challenge = {'/': None}, [], 'Basic realm="nas"'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.config = {'protocol': 'webdav', 'host': '127.0.0.1', 'port': self.server.server_address[1],
                       'path': '/dav', 'username': 'user', 'password': 'secret', 'writable': True}

    def test_mkdir_put_file_and_overwrite(self):
        client = WebDavClient(self.config, 5)
        client.mkdir('/backup')
        client.mkdir('/backup')                              # already there: fine
        put_file(client, '/backup/a.bin', iter([b'ab', b'cd']), 4)
        self.assertEqual(self.server.tree['/backup/a.bin'], b'abcd')
        self.assertNotIn('/backup/a.bin.part', self.server.tree)
        put_file(client, '/backup/a.bin', iter([b'xyz']), 3)
        self.assertEqual(self.server.tree['/backup/a.bin'], b'xyz')
        puts = [entry for entry in self.server.log if entry[0] == 'PUT']
        self.assertTrue(all(authorized for _, _, authorized in puts), 'PUT must carry credentials up front')

    def test_digest_only_servers_are_refused_before_streaming(self):
        self.server.challenge = 'Digest realm="nas", nonce="x"'
        client = WebDavClient(self.config, 5)
        with self.assertRaisesRegex(NasError, 'Digest'):
            client.write('/a.bin', iter([b'x']), 1)

    def test_read_only_connection_refuses_every_write(self):
        client = WebDavClient({**self.config, 'writable': False}, 5)
        for action in (lambda: client.mkdir('/x'), lambda: client.write('/x', iter([b'']), 0),
                       lambda: client.rename('/a', '/b'), lambda: client.delete('/a'), lambda: client.rmdir('/a')):
            with self.assertRaisesRegex(NasError, '允许写入'):
                action()
        self.assertEqual([e for e in self.server.log if e[0] != 'PROPFIND'], [])

    def test_connection_check_does_a_write_round_trip(self):
        result = nas.check_connection(self.config)
        self.assertEqual(result['write'], 'ok')
        self.assertEqual(set(self.server.tree), {'/'}, 'test folder removed')
        self.assertNotIn('write', nas.check_connection({**self.config, 'writable': False}))


class FakeSmbModule:
    """Enough of pysmbc for the write path: files keyed by URL."""

    def __init__(self):
        self.files, self.dirs, self.calls = {}, {'smb://nas/share'}, []
        module = self

        class Handle:
            def __init__(self, url):
                self.url = url
                module.files[url] = b''

            def write(self, data):
                module.files[self.url] += data

            def close(self):
                pass

        class Context:
            def __init__(self, auth_fn=None):
                pass

            def open(self, url, flags, mode=0):
                module.calls.append(('open', url, flags))
                return Handle(url)

            def mkdir(self, url, mode):
                module.calls.append(('mkdir', url))
                if url in module.dirs:
                    raise OSError(17, 'exists')
                module.dirs.add(url)

            def stat(self, url):
                if url in module.dirs:
                    return (stat.S_IFDIR, 0, 0, 0, 0, 0, 0, 0, 0)
                if url in module.files:
                    return (stat.S_IFREG, 0, 0, 0, 0, 0, len(module.files[url]), 0, 0)
                raise OSError(2, 'No such file')

            def rename(self, source, target):
                module.calls.append(('rename', source, target))
                module.files[target] = module.files.pop(source)

            def unlink(self, url):
                module.calls.append(('unlink', url))
                module.files.pop(url)

            def rmdir(self, url):
                module.dirs.discard(url)
        self.Context = Context


class SmbWriteTests(unittest.TestCase):
    def test_write_rename_and_existing_folder(self):
        fake = FakeSmbModule()
        with mock.patch.dict(sys.modules, {'smbc': fake}):
            client = nas.SmbClient({'protocol': 'smb', 'host': 'nas', 'share': 'share', 'path': '/',
                                    'writable': True}, 5)
            client.mkdir('/b')
            client.mkdir('/b')
            put_file(client, '/b/a b.bin', iter([b'12', b'34']), 4)
            self.assertEqual(fake.files['smb://nas/share/b/a%20b.bin'], b'1234')
            self.assertIn(('rename', 'smb://nas/share/b/a%20b.bin.part', 'smb://nas/share/b/a%20b.bin'), fake.calls)
            read_only = nas.SmbClient({'protocol': 'smb', 'host': 'nas', 'share': 'share', 'path': '/'}, 5)
            with self.assertRaisesRegex(NasError, '允许写入'):
                read_only.write('/x', iter([b'']), 0)

    def test_short_part_is_removed_and_reported(self):
        fake = FakeSmbModule()
        with mock.patch.dict(sys.modules, {'smbc': fake}):
            client = nas.SmbClient({'protocol': 'smb', 'host': 'nas', 'share': 'share', 'writable': True}, 5)
            with self.assertRaisesRegex(NasError, '大小不符'):
                put_file(client, '/a.bin', iter([b'12']), 4)
        self.assertEqual(fake.files, {})


class FtpWriteTests(unittest.TestCase):
    def test_commands(self):
        client = nas.FtpClient({'protocol': 'ftp', 'host': '192.0.2.9', 'path': '/root', 'writable': True}, 5)
        conn = mock.Mock()
        stored = {}

        def store(command, body, blocksize):
            stored[command] = b''.join(iter(lambda: body.read(blocksize), b''))
        conn.storbinary.side_effect = store
        with mock.patch.object(client, '_connect', return_value=conn):
            client.mkdir('/b')
            client.write('/b/a.bin', iter([b'xy', b'z']), 3)
            client.rename('/b/a.bin.part', '/b/a.bin')
            client.delete('/b/old')
            client.rmdir('/b/empty')
        conn.mkd.assert_called_once_with('/root/b')
        self.assertEqual(stored, {'STOR /root/b/a.bin': b'xyz'})
        conn.rename.assert_called_once_with('/root/b/a.bin.part', '/root/b/a.bin')
        conn.delete.assert_called_once_with('/root/b/old')
        conn.rmd.assert_called_once_with('/root/b/empty')

    def test_read_only(self):
        client = nas.FtpClient({'protocol': 'ftp', 'host': '192.0.2.9'}, 5)
        with mock.patch.object(client, '_connect') as connect, self.assertRaisesRegex(NasError, '允许写入'):
            client.mkdir('/b')
        connect.assert_not_called()


class MemoryNas(FakeClient):
    """Writable in-memory NAS client."""

    def __init__(self, tree, fail_writes=0):
        super().__init__(tree)
        self.fail_writes = fail_writes
        self.writes = []

    def mkdir(self, path):
        if self.tree.get(path, None) is not None:
            raise NasError('file in the way')
        self.tree[path] = None

    def write(self, path, chunks, size):
        data = b''.join(chunks)
        self.writes.append(path)
        if self.fail_writes:
            self.fail_writes -= 1
            self.tree[path] = data[:1]
            raise NasError('connection reset')
        self.tree[path] = data

    def rename(self, source, target):
        self.tree[target] = self.tree.pop(source)

    def delete(self, path):
        if path not in self.tree:
            raise NasError('missing', status=404)
        del self.tree[path]

    rmdir = delete


class TreePs5:
    """FileProxy stand-in with real folder listings and streams."""

    host = '192.0.2.5'

    def __init__(self, tree, slow=False):
        self.tree, self.slow = tree, slow

    def list(self, path):
        if self.tree.get(path, b'') is not None:
            raise FileServiceError('not a folder', status=404)
        prefix = path.rstrip('/') + '/'
        return {'path': path, 'entries': [
            {'name': key[len(prefix):], 'path': key, 'dir': value is None, 'size': 0 if value is None else len(value)}
            for key, value in self.tree.items() if key.startswith(prefix) and '/' not in key[len(prefix):]]}

    def stream(self, path):
        import io
        data, slow = self.tree[path], self.slow

        class Body(io.BytesIO):
            def read(self, size=-1):
                if slow:
                    time.sleep(0.05)
                return super().read(min(size, 2) if slow else size)
        return Body(data)


PS5_TREE = {'/data/savedata': None, '/data/savedata/a.bin': b'A' * 10, '/data/savedata/sub': None,
            '/data/savedata/sub/b.bin': b'BB', '/data/savedata/empty': None}


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = NasStore(Path(self.tmp.name) / 'nas.json')
        self.ident = self.store.save({'protocol': 'ftp', 'host': '192.0.2.9', 'writable': True})['id']
        self.read_only = self.store.save({'protocol': 'ftp', 'host': '192.0.2.9'})['id']
        self.remote = MemoryNas({'/': None})
        self.ps5 = TreePs5(dict(PS5_TREE))

    def manager(self, **extra):
        manager = TransferManager(self.store, self.ps5, client_factory=lambda config: self.remote,
                                  host='192.0.2.5', **extra)
        manager.RETRY_DELAYS = (0, 0, 0)
        return manager

    def test_mirror_copies_folders_and_skips_unchanged_files_next_time(self):
        manager = self.manager()
        job = wait(manager, manager.start_backup(self.ident, ['/data/savedata'], '/ps5')['id'])
        self.assertEqual((job['kind'], job['state'], job['files_done'], job['files_total']), ('backup', 'done', 2, 2))
        self.assertEqual(self.remote.tree['/ps5/savedata/a.bin'], b'A' * 10)
        self.assertEqual(self.remote.tree['/ps5/savedata/sub/b.bin'], b'BB')
        self.assertIsNone(self.remote.tree['/ps5/savedata/empty'])
        self.assertFalse([path for path in self.remote.tree if path.endswith('.part')])
        self.ps5.tree['/data/savedata/sub/b.bin'] = b'BBB'
        self.remote.writes.clear()
        again = wait(manager, manager.start_backup(self.ident, ['/data/savedata'], '/ps5')['id'])
        self.assertEqual((again['files_skipped'], again['files_done']), (1, 1))
        self.assertEqual(self.remote.writes, ['/ps5/savedata/sub/b.bin.part'])
        self.assertEqual(self.remote.tree['/ps5/savedata/sub/b.bin'], b'BBB')

    def test_snapshot_goes_into_host_and_time_folder(self):
        manager = self.manager()
        job = wait(manager, manager.start_backup(self.ident, ['/data/savedata'], '/ps5', 'snapshot')['id'])
        self.assertRegex(job['target'], r'^/ps5/192\.0\.2\.5/\d{8}-\d{6}$')
        self.assertIn(job['target'] + '/savedata/a.bin', self.remote.tree)

    def test_network_errors_are_retried_then_recorded(self):
        self.remote.fail_writes = 1
        manager = self.manager()
        job = wait(manager, manager.start_backup(self.ident, ['/data/savedata'], '/ps5')['id'])
        self.assertEqual((job['state'], job['retries'], job['files_failed']), ('done', 1, 0))
        self.remote.fail_writes = 99
        manager.retries = 1
        job = wait(manager, manager.start_backup(self.ident, ['/data/savedata'], '/other')['id'])
        self.assertEqual((job['state'], job['files_failed']), ('failed', 2))
        self.assertIn('重试', job['error'])
        self.assertTrue(job['errors'])
        self.assertFalse([path for path in self.remote.tree if path.endswith('.part')])
        self.remote.fail_writes = 0
        again = wait(manager, manager.retry(job['id'])['id'])
        self.assertEqual((again['state'], again['target'], again['retry_of']), ('done', '/other', job['id']))

    def test_cancel_leaves_no_partial_file(self):
        self.ps5.slow = True
        manager = self.manager()
        ident = manager.start_backup(self.ident, ['/data/savedata'], '/ps5')['id']
        deadline = time.time() + 5
        while manager.snapshot_one(ident)['bytes_done'] == 0 and time.time() < deadline:
            time.sleep(0.01)
        manager.cancel(ident)
        self.assertEqual(wait(manager, ident)['state'], 'canceled')
        self.assertFalse([path for path in self.remote.tree if path.endswith('.part')])

    def test_requests_are_checked(self):
        manager = self.manager()
        with self.assertRaisesRegex(NasError, '允许写入'):
            manager.start_backup(self.read_only, ['/data/savedata'], '/ps5')
        for paths in ([], ['/'], ['data'], ['/data/../etc'], ['/a', '/a'], ['/a'] * 21):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                manager.start_backup(self.ident, paths, '/ps5')
        with self.assertRaises(ValueError):
            manager.start_backup(self.ident, ['/data/savedata'], '/ps5', 'zip')

    def test_store_keeps_the_writable_switch(self):
        listed = {item['id']: item['writable'] for item in self.store.list_public()}
        self.assertEqual(listed, {self.ident: True, self.read_only: False})
        reopened = NasStore(Path(self.tmp.name) / 'nas.json')
        self.assertTrue(reopened.get(self.ident)['writable'])
        self.assertFalse(reopened.get(self.read_only)['writable'])


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
