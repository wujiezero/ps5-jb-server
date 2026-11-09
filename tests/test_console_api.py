import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_console import ConsoleAPI
import test_files as TF
import test_ps5 as TP


class StubHandler:
    """Minimal stand-in for AdminHandler (records the response)."""

    def __init__(self, path, body=None, headers=None):
        self.path = path
        self.rfile = io.BytesIO(body or b'')
        self.headers = headers or {}
        self.status = None
        self.payload = None
        self.stream = io.BytesIO()

        class _W:
            def __init__(self, sink):
                self.sink = sink

            def write(self, data):
                self.sink.write(data)

        self.wfile = _W(self.stream)
        self.sent_headers = {}

    def require_session(self, write=False):
        return {'csrf': 'x'}

    def send_body(self, status, data, ctype='application/json; charset=utf-8', cookie=None):
        self.status = status
        self.payload = data

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers[key.lower()] = value

    def end_headers(self):
        pass


def json_body(payload):
    data = json.dumps(payload).encode()
    return data, {'Content-Length': str(len(data)), 'Content-Type': 'application/json'}


class ConsoleAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.files_server = ThreadingHTTPServer(('127.0.0.1', 0), TF.FakeFiles)
        cls.files_server.daemon_threads = True
        threading.Thread(target=cls.files_server.serve_forever, daemon=True).start()
        cls.pld = TP.serve(TP.FakePldmgr, ThreadingHTTPServer)
        cls.elf = TP.serve(TP.FakeElfldr, None)
        settings = TP.FakeConfig(pldmgr_port=cls.pld.server_address[1],
                                 elf_port=cls.elf.server_address[1])
        settings.subnet = '127.0.0.1/32'
        settings.file_port = cls.files_server.server_address[1]
        settings.host_ip = '127.0.0.1'
        settings.repo_port = 8080
        settings.ps5_control = True
        settings.file_manager = True
        settings.max_upload = 8 << 20
        settings.data_dir = Path(cls.tmp.name)
        cls.api = ConsoleAPI(settings, lambda: {'items': [
            {'filename': 'etaHEN_2.5B.bin', 'name': 'etaHEN',
             'url': 'http://127.0.0.1:8080/payloads/etaHEN_2.5B.bin'}]})
        cls.api.files.host = '127.0.0.1'

    @classmethod
    def tearDownClass(cls):
        for server in (cls.files_server, cls.pld, cls.elf):
            server.shutdown()
            server.server_close()
        cls.tmp.cleanup()

    def test_files_info_and_volumes(self):
        handler = StubHandler('/api/files/info')
        self.assertTrue(self.api.handle_get(handler))
        self.assertEqual(handler.status, 200)
        self.assertTrue(handler.payload['reachable'])

        handler = StubHandler('/api/files/volumes')
        self.api.handle_get(handler)
        self.assertEqual(handler.payload['volumes'][0]['path'], '/data')

    def test_files_list_normalized(self):
        handler = StubHandler('/api/files/list?path=/data')
        self.api.handle_get(handler)
        self.assertEqual(handler.payload['entries'][0]['dir'], True)
        self.assertEqual(handler.payload['entries'][0]['path'], '/data/homebrew')

    def test_download_streams(self):
        handler = StubHandler('/api/files/download?path=/data/a.elf')
        self.assertTrue(self.api.handle_get(handler))
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.stream.getvalue(), b'ELF-DATA')
        self.assertIn('attachment', handler.sent_headers['content-disposition'])

    def test_mkdir_and_rename(self):
        body, headers = json_body({'path': '/data/new'})
        handler = StubHandler('/api/files/mkdir', body, headers)
        self.assertTrue(self.api.handle_post(handler, '/api/files/mkdir'))
        self.assertEqual(handler.status, 200)

        body, headers = json_body({'from': '/data/a', 'to': '/data/b'})
        handler = StubHandler('/api/files/rename', body, headers)
        self.api.handle_post(handler, '/api/files/rename')
        self.assertEqual(handler.status, 200)

    def test_upload_streams_body(self):
        payload = b'X' * 5000
        handler = StubHandler('/api/files/upload?path=/data&overwrite=true', payload,
                              {'Content-Length': str(len(payload)), 'X-Filename': 'up.elf'})
        TF.uploads.clear()
        self.assertTrue(self.api.handle_post(handler, '/api/files/upload'))
        self.assertEqual(handler.status, 200)
        self.assertEqual(TF.uploads[0], ('/data/up.elf', payload))

    def test_upload_without_length_rejected(self):
        handler = StubHandler('/api/files/upload?path=/data', b'', {'Content-Length': '0'})
        self.api.handle_post(handler, '/api/files/upload')
        self.assertEqual(handler.status, 400)

    def test_path_guard_returns_400(self):
        handler = StubHandler('/api/files/list?path=/etc')
        self.api.handle_get(handler)
        self.assertEqual(handler.status, 400)
        self.assertIn('只允许访问', handler.payload['error'])

    def test_ps5_overview_and_scan(self):
        handler = StubHandler('/api/ps5')
        self.api.handle_get(handler)
        self.assertEqual(handler.status, 200)
        self.assertIn('hosts', handler.payload)

        body, headers = json_body({})
        handler = StubHandler('/api/ps5/scan', body, headers)
        self.api.handle_post(handler, '/api/ps5/scan')
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.payload['hosts'][0]['ip'], '127.0.0.1')

    def test_ps5_install_unknown_payload(self):
        body, headers = json_body({'ip': '127.0.0.1', 'filename': 'nope.elf'})
        handler = StubHandler('/api/ps5/install', body, headers)
        self.api.handle_post(handler, '/api/ps5/install')
        self.assertEqual(handler.status, 400)

    def test_unreachable_console_is_502(self):
        self.api.files.host = '127.0.0.2'
        handler = StubHandler('/api/files/volumes')
        self.api.handle_get(handler)
        self.assertEqual(handler.status, 502)
        self.assertIn('无法连接', handler.payload['error'])
        self.api.files.host = '127.0.0.1'


if __name__ == '__main__':
    unittest.main()
