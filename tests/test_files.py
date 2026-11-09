import json
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from files import FileProxy, FileServiceError, _check_path, normalize_entries

VOLUMES = {'volumes': [{'path': '/data', 'kind': 'internal', 'format': 'ufs',
                        'total': 1000, 'free': 400}]}
LISTING = {'path': '/data', 'entries': [
    {'name': 'homebrew', 'kind': 'folder', 'size': 0, 'mtime': 1700000000},
    {'name': 'a.elf', 'kind': 'file', 'size': 12, 'mtime': 1700000001}], 'truncated': False}
uploads = []
actions = []


class FakeFiles(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def _send(self, payload, code=200, ctype='application/json'):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        split = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(split.query))
        path = query.get('path', '')
        if split.path == '/fs/volumes':
            return self._send(VOLUMES)
        if split.path == '/fs/list':
            return self._send(LISTING)
        if split.path == '/fs/get':
            return self._send(b'ELF-DATA', ctype='application/octet-stream')
        if split.path == '/fs/tar':
            return self._send(b'TARBALL', ctype='application/x-tar')
        if split.path == '/fs/job':
            return self._send({'state': 'copying', 'percent': 42})
        if split.path == '/fs/transfers':
            return self._send({'transfers': []})
        if split.path == '/fs/size':
            return self._send({'size': 999})
        return self._send({'error': 'not found'}, 404)

    def do_POST(self):
        split = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(split.query))
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length) if length else b''
        actions.append((split.path, query))
        if split.path == '/fs/put':
            uploads.append((query.get('path'), body))
            return self._send({'ok': True})
        if split.path in ('/fs/mkdir', '/fs/rename', '/fs/delete', '/fs/copy', '/fs/move',
                          '/fs/cancel', '/fs/bench'):
            return self._send({})
        return self._send({'error': 'not found'}, 404)


class ProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeFiles)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.proxy = FileProxy('127.0.0.0/8', host='127.0.0.1',
                              port=cls.server.server_address[1], timeout=3)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_info_reports_reachable(self):
        info = self.proxy.info()
        self.assertTrue(info['configured'] and info['reachable'])
        self.assertEqual(info['host'], '127.0.0.1')

    def test_list_normalizes_entries(self):
        listing = self.proxy.list('/data')
        self.assertEqual(listing['path'], '/data')
        folder = listing['entries'][0]
        self.assertTrue(folder['dir'])
        self.assertEqual(folder['path'], '/data/homebrew')
        self.assertEqual(listing['entries'][1]['size'], 12)

    def test_volumes_passthrough(self):
        self.assertEqual(self.proxy.volumes()['volumes'][0]['path'], '/data')

    def test_download_streams_bytes(self):
        with self.proxy.stream('/data/a.elf') as response:
            self.assertEqual(response.read(), b'ELF-DATA')

    def test_tar_streams_bytes(self):
        with self.proxy.stream_folder('/data') as response:
            self.assertEqual(response.read(), b'TARBALL')

    def test_mutations_reach_console(self):
        actions.clear()
        self.proxy.mkdir('/data/new')
        self.proxy.rename('/data/a', '/data/b')
        self.proxy.delete('/data/b')
        self.proxy.copy('/data/a', '/data/c')
        self.proxy.copy('/data/a', '/data/d', move=True)
        self.assertEqual([a[0] for a in actions],
                         ['/fs/mkdir', '/fs/rename', '/fs/delete', '/fs/copy', '/fs/move'])
        self.assertEqual(actions[1][1], {'from': '/data/a', 'to': '/data/b'})

    def test_upload_streams_with_size_and_overwrite(self):
        uploads.clear()
        self.proxy.upload('/data', 'x.elf', [b'abc', b'def'], length=6, overwrite=True)
        path, body = uploads[0]
        self.assertEqual(path, '/data/x.elf')
        self.assertEqual(body, b'abcdef')
        self.assertEqual(actions[-1][1].get('overwrite'), '1')

    def test_path_guard(self):
        for bad in ('relative', '/data/../etc/passwd', '/etc/passwd', '/data/./x'):
            with self.assertRaises(ValueError):
                _check_path(bad)
        self.assertEqual(_check_path('/data'), '/data')
        self.assertEqual(_check_path('/mnt/usb0/x'), '/mnt/usb0/x')

    def test_upload_rejects_bad_filename(self):
        with self.assertRaises(ValueError):
            self.proxy.upload('/data', '../x', [b''], length=0)

    def test_unreachable_is_service_error(self):
        proxy = FileProxy('127.0.0.0/8', host='127.0.0.1', port=1, timeout=1)
        with self.assertRaises(FileServiceError):
            proxy.volumes()
        self.assertFalse(proxy.info()['reachable'])

    def test_configure_rejects_foreign_subnet(self):
        with self.assertRaises(ValueError):
            self.proxy.configure('198.51.100.7')


if __name__ == '__main__':
    unittest.main()
