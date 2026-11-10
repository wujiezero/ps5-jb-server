import json
import socket
import socketserver
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from ps5 import ConsoleError, Discovery, ElfldrClient, HostManager, PldmgrClient


class FakeConfig:
    def __init__(self, subnet='127.0.0.1/32', host_ip='127.0.0.1', repo_port=8080,
                 pldmgr_port=0, elf_port=0):
        self.subnet = subnet
        self.host_ip = host_ip
        self.repo_port = repo_port
        self.pldmgr_port = pldmgr_port
        self.elf_port = elf_port
        self.ps5_control = True


class FakePldmgr(BaseHTTPRequestHandler):
    sources = [{'id': 'default', 'name': 'Official', 'url': 'https://example.invalid/repo.json'}]
    installed = ['/data/pldmgr/etaHEN_2.5B.bin']
    config = {'AUTOLOAD_ENABLED': False, 'AUTOLOAD_LIST': ''}
    killed = []
    shutdowns = 0
    installs = []
    uploads = []
    marker_missing = 0

    def log_message(self, *_args):
        pass

    def reply(self, payload, code=200, raw=False):
        body = (payload if raw else json.dumps(payload)).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain' if raw else 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == '/version':
            return self.reply('0.6.3-test', raw=True)
        if path == '/list_payloads':
            return self.reply({'self': {'id': 'pldmgr'},
                               'items': [{'slot': 'local/x', 'path': item,
                                          'filename': item.rsplit('/', 1)[-1],
                                          'kind': 'local'} for item in self.installed]})
        if path == '/get_config':
            return self.reply(dict(self.config))
        if path == '/autoload_status':
            return self.reply({'remaining': 0, 'total': 0, 'done': 0, 'current': 'DONE'})
        if path == '/processes_list':
            return self.reply([{'pid': 4242, 'name': 'etaHEN_2.5B.bin'}])
        if path == '/sources_list':
            return self.reply({'sources': list(self.sources)})
        if path == '/repository_payloads':
            return self.reply({'sources': [{'id': 'default', 'url': 'https://example.invalid/repo.json',
                                            'payloads': [{'filename': 'x.elf'}]}]})
        return self.reply({'error': 'not found'}, 404)

    def do_POST(self):
        global marker_missing
        if self.headers.get('X-PLDMGR-Request') != '1':
            FakePldmgr.marker_missing += 1
            return self.reply({'ok': False, 'message': 'missing marker'}, 403)
        split = urllib.parse.urlsplit(self.path)
        path, query = split.path, urllib.parse.parse_qs(split.query)
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b''
        ctype = self.headers.get('Content-Type', '')
        body = json.loads(raw or b'{}') if 'json' in ctype or not raw else {'raw': raw}
        if path == '/sources_add':
            url = (query.get('url') or [''])[0]
            if not url:
                return self.reply({'ok': False}, 400)
            FakePldmgr.sources.append({'id': 'added', 'name': 'Home', 'url': url})
            return self.reply({'ok': True, 'name': 'Home'})
        if path == '/repository_refresh':
            return self.reply({'ok': True})
        if path == '/repository_install':
            FakePldmgr.installs.append((query.get('filename') or [''])[0])
            return self.reply({'ok': True})
        if path.startswith('/loadpayload'):
            FakePldmgr.installs.append('run:' + path.split(':', 1)[1])
            return self.reply('OK', raw=True)
        if path == '/manage:upload':
            name = (query.get('filename') or [''])[0]
            FakePldmgr.uploads.append((name, body.get('raw', b'')))
            FakePldmgr.installed.append('/data/pldmgr/payloads/local/'
                                        + name.rsplit('.', 1)[0] + '/' + name)
            return self.reply('OK', raw=True)
        if path == '/process_kill':
            FakePldmgr.killed.append(body.get('pid'))
            return self.reply({'ok': True})
        if path == '/set_config':
            FakePldmgr.config.update(body)
            return self.reply('OK', raw=True)
        if path == '/shutdown':
            FakePldmgr.shutdowns += 1
            return self.reply('Shutting down', raw=True)
        return self.reply({'error': 'unknown'}, 404)


class FakeElfldr(socketserver.BaseRequestHandler):
    requests = []

    def handle(self):
        self.request.settimeout(2)
        data = b''
        try:
            while True:
                chunk = self.request.recv(4096)
                if not chunk:
                    break
                data += chunk
        except OSError:
            pass
        FakeElfldr.requests.append(data)
        if data.startswith(b'GET '):
            self.request.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK')


def serve(handler, cls):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler) if cls is not None else \
        socketserver.ThreadingTCPServer(('127.0.0.1', 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class ConsoleFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pld = serve(FakePldmgr, ThreadingHTTPServer)
        cls.elf = serve(FakeElfldr, None)
        cls.config = FakeConfig(pldmgr_port=cls.pld.server_address[1],
                                elf_port=cls.elf.server_address[1])
        cls.catalog = lambda: {'name': 'repo', 'items': [
            {'filename': 'etaHEN_2.5B.bin', 'name': 'etaHEN',
             'url': 'http://127.0.0.1:8080/payloads/etaHEN_2.5B.bin'},
            {'filename': 'ftpsrv_v0.21.1.elf', 'name': 'FTP',
             'url': 'http://127.0.0.1:8080/payloads/ftpsrv_v0.21.1.elf'},
        ]}
        cls.manager = HostManager(cls.config, cls.catalog,
                                  fetch_bytes=lambda name: b'\x7fELF-' + name.encode())

    @classmethod
    def tearDownClass(cls):
        for server in (cls.pld, cls.elf):
            server.shutdown()
            server.server_close()


class ClientTests(ConsoleFixture):
    def test_reads(self):
        client = PldmgrClient('127.0.0.1', self.config.pldmgr_port)
        self.assertEqual(client.version(), '0.6.3-test')
        items = client.payloads()
        self.assertEqual(items[0]['filename'], 'etaHEN_2.5B.bin')
        self.assertEqual(items[0]['path'], '/data/pldmgr/etaHEN_2.5B.bin')
        self.assertEqual(client.processes()[0]['pid'], 4242)
        self.assertEqual(client.sources()[0]['id'], 'default')

    def test_mutations_require_marker(self):
        before = FakePldmgr.marker_missing
        client = PldmgrClient('127.0.0.1', self.config.pldmgr_port)
        client.add_source('http://127.0.0.1:8080/repo.json')
        self.assertEqual(FakePldmgr.marker_missing, before, 'mutations must carry the marker')

    def test_connection_error_is_console_error(self):
        client = PldmgrClient('127.0.0.1', 1)
        with self.assertRaises(ConsoleError):
            client.version()

    def test_elfldr_url_run(self):
        FakeElfldr.requests.clear()
        ElfldrClient('127.0.0.1', self.config.elf_port).run_url('http://127.0.0.1:8080/x.elf')
        self.assertTrue(FakeElfldr.requests[0].startswith(b'GET '))
        self.assertIn(b'uri=', FakeElfldr.requests[0])


class ManagerTests(ConsoleFixture):
    def setUp(self):
        FakePldmgr.sources = [{'id': 'default', 'name': 'Official', 'url': 'https://example.invalid/repo.json'}]
        FakePldmgr.installed = ['/data/pldmgr/etaHEN_2.5B.bin']
        FakePldmgr.config = {'AUTOLOAD_ENABLED': False, 'AUTOLOAD_LIST': ''}
        FakePldmgr.killed.clear()
        FakePldmgr.installs.clear()
        FakePldmgr.uploads.clear()
        FakeElfldr.requests.clear()

    def test_scan_finds_host_and_populates(self):
        result = self.manager.scan()
        self.assertEqual(result['discovery']['scanned'], 1)
        host = result['hosts'][0]
        self.assertEqual(host['ip'], '127.0.0.1')
        self.assertTrue(host['elfldr'] and host['pldmgr'])
        self.assertEqual(host['version'], '0.6.3-test')
        self.assertEqual(host['installed'], ['etaHEN_2.5B.bin'])
        self.assertEqual(host['running'][0]['pid'], 4242)
        self.assertEqual(host['process_total'], 1)
        self.assertFalse(host['source_attached'])

    def test_attach_is_idempotent(self):
        self.manager.attach('127.0.0.1')
        host = self.manager.attach('127.0.0.1')
        self.assertTrue(host['source_attached'])
        urls = [s['url'] for s in FakePldmgr.sources]
        self.assertEqual(urls.count('http://127.0.0.1:8080/repo.json'), 1)

    def test_install_and_run_via_pldmgr(self):
        result = self.manager.install('127.0.0.1', 'etaHEN_2.5B.bin', run=True)
        self.assertEqual(result['mode'], 'pldmgr')
        self.assertEqual(FakePldmgr.uploads[0][0], 'etaHEN_2.5B.bin')
        self.assertTrue(FakePldmgr.uploads[0][1].startswith(b'\x7fELF'))
        self.assertTrue(any(str(item).startswith('run:') for item in FakePldmgr.installs))
        self.assertIn('etaHEN_2.5B.bin', result['path'])

    def test_install_falls_back_to_elfldr_when_pldmgr_fails(self):
        manager = HostManager(FakeConfig(pldmgr_port=1, elf_port=self.config.elf_port),
                              type(self).catalog,
                              fetch_bytes=lambda name: b'\x7fELF-' + name.encode())
        # Install-only must not silently run the payload through elfldr.
        with self.assertRaises(ConsoleError):
            manager.install('127.0.0.1', 'ftpsrv_v0.21.1.elf', run=False)
        result = manager.install('127.0.0.1', 'ftpsrv_v0.21.1.elf', run=True)
        self.assertEqual(result['mode'], 'elfldr')
        # The availability probe opens a connection first, so look for the GET.
        self.assertTrue(any(r.startswith(b'GET ') for r in FakeElfldr.requests))

    def test_install_unknown_payload_rejected(self):
        with self.assertRaises(ValueError):
            self.manager.install('127.0.0.1', 'not-in-catalog.elf')

    def test_kill_and_autoload(self):
        self.manager.kill('127.0.0.1', 4242)
        self.assertEqual(FakePldmgr.killed, [4242])
        self.manager.autoload('127.0.0.1', True, ['etaHEN_2.5B.bin', 'ftpsrv_v0.21.1.elf'])
        self.assertTrue(FakePldmgr.config['AUTOLOAD_ENABLED'])
        self.assertEqual(FakePldmgr.config['AUTOLOAD_LIST'], 'etaHEN_2.5B.bin,ftpsrv_v0.21.1.elf')
        host = self.manager.host('127.0.0.1')
        self.assertEqual(host['autoload']['list'], ['etaHEN_2.5B.bin', 'ftpsrv_v0.21.1.elf'])
        # Ordered items with a wait after each one, plus the start countdown.
        self.manager.autoload('127.0.0.1', True,
                              items=[{'key': 'local/kstuff', 'delay_ms': 3000},
                                     {'key': 'local/ftpsrv', 'delay_ms': 0}],
                              start_delay=8)
        self.assertEqual(FakePldmgr.config['AUTOLOAD_LIST'], 'local/kstuff,!3000,local/ftpsrv')
        self.assertEqual(FakePldmgr.config['AUTOLOAD_DELAY'], 8)
        host = self.manager.host('127.0.0.1')
        self.assertEqual([(i['key'], i['delay_ms']) for i in host['autoload']['items']],
                         [('local/kstuff', 3000), ('local/ftpsrv', 0)])
        self.assertEqual(host['autoload']['start_delay'], 8)

    def test_autoload_rejects_path_injection(self):
        with self.assertRaises(ValueError):
            self.manager.autoload('127.0.0.1', True, ['../etc/passwd'])

    def test_only_lan_addresses(self):
        with self.assertRaises(ValueError):
            self.manager.host('198.51.100.5')

    def test_shutdown(self):
        before = FakePldmgr.shutdowns
        self.manager.shutdown('127.0.0.1')
        self.assertEqual(FakePldmgr.shutdowns, before + 1)


if __name__ == '__main__':
    unittest.main()
