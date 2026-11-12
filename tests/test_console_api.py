import io
import json
import sys
import tempfile
import threading
import unittest
from unittest import mock
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

    def test_extract_plans_selected_paths_and_preflights_before_queueing(self):
        calls = []

        class Services:
            def preflight(self, host):
                calls.append(('preflight', host))

        self.api.game_services = Services()
        self.api.files.host = '192.0.2.5'
        plan = {'kind': 'archive', 'path': '/games/CUSA12345.rar', 'title_hint': 'CUSA12345'}
        with (mock.patch('api_console.plan_sources', return_value=plan) as planner,
              mock.patch.object(self.api.transfers, 'start_extract', return_value={'id': 'job'}) as start):
            body, headers = json_body({'id': 'nas-id', 'paths': ['/games/CUSA12345.rar'],
                                       'dest': '/data', 'auto_register': True})
            handler = StubHandler('/api/nas/extract', body, headers)
            self.api.handle_post(handler, '/api/nas/extract')
        self.assertEqual(handler.status, 200)
        planner.assert_called_once()
        self.assertEqual(calls, [('preflight', '192.0.2.5')])
        self.assertEqual(start.call_args.kwargs['auto_register'], True)
        self.assertEqual(start.call_args.kwargs['host'], '192.0.2.5')
        self.assertEqual(start.call_args.kwargs['title'], 'CUSA12345')
        self.api.files.host = '127.0.0.1'
        self.api.game_services = None

    def _install(self, plan, body):
        calls = []

        class Services:
            def preflight(self, host):
                calls.append(host)

        self.api.game_services = Services()
        mocks = {name: mock.patch.object(self.api.transfers, name, return_value={'id': name})
                 for name in ('start', 'start_extract', 'start_package')}
        started = {name: patcher.start() for name, patcher in mocks.items()}
        try:
            with mock.patch.object(self.api, '_plan', return_value=plan):
                data, headers = json_body(body)
                handler = StubHandler('/api/nas/install', data, headers)
                self.api.handle_post(handler, '/api/nas/install')
        finally:
            for patcher in mocks.values():
                patcher.stop()
            self.api.game_services = None
        return handler, started, calls

    def test_install_dispatches_by_source_kind(self):
        base = {'id': 'nas', 'paths': ['/g/x'], 'dest': '/data', 'host': '192.0.2.5'}
        handler, started, calls = self._install(
            {'kind': 'package', 'path': '/g/CUSA12345.pkg', 'title_hint': 'CUSA12345', 'cheats': []}, base)
        self.assertEqual((handler.status, handler.payload['kind']), (200, 'package'))
        started['start_package'].assert_called_once_with('nas', '/g/CUSA12345.pkg', host='192.0.2.5',
                                                         title='CUSA12345')
        self.assertEqual(calls, ['192.0.2.5'])

        handler, started, _ = self._install(
            {'kind': 'archive', 'path': '/g/a.part1.rar', 'title_hint': '', 'cheats': ['/g/a cheat.zip']},
            {**base, 'cheats': True})
        self.assertEqual(handler.payload['kind'], 'archive')
        self.assertEqual(started['start_extract'].call_args.kwargs['cheats'], ['/g/a cheat.zip'])

        handler, started, _ = self._install(
            {'kind': 'folder', 'path': '/g/CUSA12345', 'title_hint': 'CUSA12345', 'cheats': []}, base)
        self.assertEqual(handler.payload['kind'], 'folder')
        args = started['start'].call_args
        self.assertEqual(args.args[:3], ('nas', ['/g/CUSA12345'], '/data'))
        self.assertTrue(args.kwargs['auto_register'])

    def test_install_refuses_cheats_the_plan_did_not_find(self):
        handler, started, _ = self._install(
            {'kind': 'archive', 'path': '/g/a.rar', 'title_hint': '', 'cheats': ['/g/a cheat.zip']},
            {'id': 'nas', 'paths': ['/g/a.rar'], 'dest': '/data', 'cheats': ['/etc/other.zip']})
        self.assertEqual(handler.status, 400)
        started['start_extract'].assert_not_called()

    def test_extract_error_for_other_sources_points_to_install(self):
        with mock.patch('api_console.plan_sources', return_value={'kind': 'package', 'path': '/a.pkg'}):
            data, headers = json_body({'id': 'nope', 'paths': ['/a.pkg'], 'dest': '/data'})
            handler = StubHandler('/api/nas/extract', data, headers)
            self.api.handle_post(handler, '/api/nas/extract')
        self.assertEqual(handler.status, 400)
        self.assertIn('/api/nas/install', handler.payload['error'])
        self.assertNotIn('安装包流程', handler.payload['error'])

    def test_plan_endpoint_passes_every_selected_path(self):
        plan = {'kind': 'archive', 'path': '/g/a.rar', 'cheats': []}
        with mock.patch.object(self.api, '_plan', return_value=dict(plan)) as planner:
            handler = StubHandler('/api/nas/plan?id=n1&paths=%2Fg%2Fa.rar&paths=%2Fg%2Fb.zip')
            self.api.handle_get(handler)
        self.assertEqual(handler.status, 200)
        planner.assert_called_once_with('n1', ['/g/a.rar', '/g/b.zip'])
        self.assertIn('package', handler.payload['engine'])

    def test_cheat_upload_writes_into_eta_hen_folders(self):
        import zipfile
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('json/CUSA12345_01.00.json', '{"x": 1}')
            archive.writestr('../../CUSA12345.mc4', 'bad')
        payload = data.getvalue()
        TF.uploads.clear()
        handler = StubHandler('/api/files/cheats?title=CUSA12345', payload, {'Content-Length': str(len(payload))})
        self.assertTrue(self.api.handle_post(handler, '/api/files/cheats'))
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.payload['written'], ['/data/etaHEN/cheats/json/CUSA12345_01.00.json'])
        self.assertIn(('/data/etaHEN/cheats/json/CUSA12345_01.00.json', b'{"x": 1}'), TF.uploads)
        self.assertIn('重新加载', handler.payload['note'])

    def test_cheat_upload_limits(self):
        handler = StubHandler('/api/files/cheats', b'', {'Content-Length': str((16 << 20) + 1)})
        self.api.handle_post(handler, '/api/files/cheats')
        self.assertEqual(handler.status, 413)
        handler = StubHandler('/api/files/cheats', b'not a zip', {'Content-Length': '9'})
        self.api.handle_post(handler, '/api/files/cheats')
        self.assertEqual(handler.status, 400)

    def test_nas_cheats_route_reads_from_the_nas(self):
        result = {'written': ['/data/etaHEN/cheats/json/a.json'], 'warnings': [], 'note': 'n'}
        with (mock.patch.object(self.api.nas, 'get', return_value={'protocol': 'ftp'}),
              mock.patch('api_console.client_for') as factory,
              mock.patch.object(self.api.transfers, 'import_cheats', return_value=result) as importer):
            data, headers = json_body({'id': 'n1', 'path': '/g/a cheat.zip', 'title': 'CUSA12345'})
            handler = StubHandler('/api/nas/cheats', data, headers)
            self.api.handle_post(handler, '/api/nas/cheats')
        self.assertEqual(handler.status, 200)
        self.assertTrue(handler.payload['ok'])
        self.assertEqual(importer.call_args.args[1], ['/g/a cheat.zip'])
        factory.return_value.close.assert_called_once()

    def test_retry_route_queues_the_failed_job_again(self):
        with mock.patch.object(self.api.transfers, 'retry', return_value={'id': 'new', 'retry_of': 'old'}) as retry:
            body, headers = json_body({'id': 'old'})
            handler = StubHandler('/api/transfers/retry', body, headers)
            self.api.handle_post(handler, '/api/transfers/retry')
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.payload['job']['retry_of'], 'old')
        retry.assert_called_once_with('old')
        body, headers = json_body({'id': 'nope'})
        handler = StubHandler('/api/transfers/retry', body, headers)
        self.api.handle_post(handler, '/api/transfers/retry')
        self.assertEqual(handler.status, 404)

    def test_transfer_state_lives_in_the_data_dir(self):
        self.assertEqual(self.api.transfers.state_path, self.api.settings.data_dir / 'transfers.json')
        self.assertEqual(self.api.ps5.discovery.hosts_path, self.api.settings.data_dir / 'hosts.json')


if __name__ == '__main__':
    unittest.main()
