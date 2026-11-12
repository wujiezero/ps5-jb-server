"""The local PS5Upload engine contract used after game placement."""
import io
import json
import sys
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from power import EngineClient

try:
    from game_services import (
        GameServices, GameServiceError, install_timeout, parse_cheat_zip, plan_cheat_import, write_cheats,
    )
except ImportError:
    GameServices = GameServiceError = install_timeout = parse_cheat_zip = plan_cheat_import = write_cheats = None


class EngineHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.calls.append(('GET', self.path, None))
        self.reply(self.server.get_response)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append(('POST', self.path, body))
        self.reply(self.server.register_response)

    def reply(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class GameServicesTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(GameServices, 'automatic game registration service is missing')
        server = ThreadingHTTPServer(('127.0.0.1', 0), EngineHandler)
        server.calls = []
        server.get_response = {'titles': [], 'incomplete': False, 'registered_unavailable': False}
        server.register_response = {'title_id': 'CUSA12345', 'title_name': 'Game', 'used_nullfs': True}
        self.server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.services = GameServices(EngineClient('http://127.0.0.1:' + str(server.server_port)))

    def test_preflight_checks_console_management_before_large_transfer(self):
        self.services.preflight('192.0.2.5')
        self.assertEqual(self.server.calls, [('GET', '/api/ps5/hw/info?addr=192.0.2.5', None)])

    def test_register_uses_final_console_path_without_modifying_drm_metadata(self):
        game = {'title': 'CUSA12345', 'path': '/mnt/ext0/games/CUSA12345'}
        result = self.services.register('192.0.2.5', game)
        self.assertEqual(result['state'], 'done')
        self.assertEqual(result['title'], 'CUSA12345')
        self.assertEqual(self.server.calls[-1], ('POST', '/api/ps5/app/register', {
            'addr': '192.0.2.5', 'src_path': game['path'], 'patch_drm_type': False}))

    def test_registration_with_wrong_identity_is_not_reported_successful(self):
        self.server.register_response['title_id'] = 'PPSA99999'
        with self.assertRaisesRegex(GameServiceError, '编号'):
            self.services.register('192.0.2.5', {'title': 'CUSA12345', 'path': '/data/homebrew/CUSA12345'})

    def test_bad_final_path_is_refused_before_engine_call(self):
        for path in ('/data/homebrew/../CUSA12345', '/system/CUSA12345', '/data/homebrew/other'):
            with self.subTest(path=path), self.assertRaises(GameServiceError):
                self.services.register('192.0.2.5', {'title': 'CUSA12345', 'path': path})
        self.assertEqual(self.server.calls, [])

    def test_engine_failure_has_actionable_plugin_requirement(self):
        services = GameServices(EngineClient('http://127.0.0.1:1', timeout=0.1))
        with self.assertRaisesRegex(GameServiceError, 'ps5upload'):
            services.preflight('192.0.2.5')

    def test_package_install_polls_until_installed_verdict(self):
        engine = FakeEngine([
            ('POST', '/api/pkg/install', {'job': 'job-1'}),
            ('GET', '/api/pkg/install/status?job=job-1', {'job': 'job-1', 'phase': 'running'}),
            ('GET', '/api/pkg/install/status?job=job-1',
             {'job': 'job-1', 'phase': 'done', 'verdict': 'installed'}),
        ])
        result = GameServices(engine, poll_interval=0).package_install('192.0.2.5', '/tmp/game.pkg')
        self.assertEqual(result['state'], 'done')
        self.assertEqual(result['verdict'], 'installed')
        self.assertEqual([item[:2] for item in engine.calls], [
            ('POST', '/api/pkg/install'),
            ('GET', '/api/pkg/install/status?job=job-1'),
            ('GET', '/api/pkg/install/status?job=job-1'),
        ])

    def test_package_install_exposes_noninstalled_terminal_verdict(self):
        for verdict, state in (('may_not_launch', 'warning'), ('failed', 'failed'), ('unverified', 'failed')):
            with self.subTest(verdict=verdict):
                engine = FakeEngine([
                    ('POST', '/api/pkg/install', {'job': 'job-1'}),
                    ('GET', '/api/pkg/install/status?job=job-1',
                     {'job': 'job-1', 'phase': 'done', 'verdict': verdict}),
                ])
                result = GameServices(engine, poll_interval=0).package_install('192.0.2.5', '/tmp/game.pkg')
                self.assertEqual(result['state'], state)
                self.assertEqual(result['verdict'], verdict)
                self.assertTrue(result.get('warning') or result.get('error'))

    def test_package_install_reports_busy_conflict(self):
        engine = FakeEngine([
            ('POST', '/api/pkg/install', {'error': 'busy', 'job': 'other'}),
        ])
        result = GameServices(engine, poll_interval=0).package_install('192.0.2.5', '/tmp/game.pkg')
        self.assertEqual(result['state'], 'conflict')
        self.assertEqual(result['error_code'], 'busy')

    def test_package_install_from_url_lets_engine_relay_and_reports_progress(self):
        engine = FakeEngine([
            ('POST', '/api/pkg/install', {'job': 'j'}),
            ('GET', '/api/pkg/install/status?job=j',
             {'job': 'j', 'phase': 'deliver', 'metrics': {'served_bytes': 10}}),
            ('GET', '/api/pkg/install/status?job=j', {'job': 'j', 'phase': 'done', 'verdict': 'installed'}),
        ])
        seen = []
        url = 'http://192.0.2.1:8090/p/abcdefghijklmnopqrstuvwx.pkg'
        result = GameServices(engine, poll_interval=0).package_install(
            '192.0.2.5', {'url': url}, progress=lambda status: seen.append(status['phase']))
        self.assertEqual(result['state'], 'done')
        body = engine.calls[0][2]
        self.assertEqual(body['source'], {'url': url})
        self.assertTrue(body['options']['proxy_link'])
        self.assertFalse(body['options']['allow_destructive_reinstall'])
        self.assertEqual(seen, ['deliver', 'done'])

    def test_package_install_refuses_odd_urls(self):
        services = GameServices(FakeEngine([]), poll_interval=0)
        for source in ({'url': 'file:///etc/passwd'}, {'url': 'http://evil.example/x.pkg'},
                       {'url': 'http://1.2.3.4:1/a b'}, {'url': 'x', 'host_file': '/a.pkg'}):
            with self.subTest(source=source), self.assertRaises(GameServiceError):
                services.package_install('192.0.2.5', source)

    def test_package_install_timeout_and_cancel_stop_the_engine_job(self):
        engine = FakeEngine([
            ('POST', '/api/pkg/install', {'job': 'j'}),
            ('GET', '/api/pkg/install/status?job=j', {'job': 'j', 'phase': 'install'}),
            ('POST', '/api/pkg/install/stop', {'ok': True}),
        ])
        result = GameServices(engine, poll_interval=0).package_install('192.0.2.5', '/tmp/game.pkg', timeout=0.0)
        self.assertEqual((result['state'], result['error_code']), ('failed', 'timeout'))
        self.assertEqual(engine.calls[-1][2], {'job': 'j'})
        cancel = threading.Event()
        cancel.set()
        engine = FakeEngine([
            ('POST', '/api/pkg/install', {'job': 'j'}),
            ('POST', '/api/pkg/install/stop', {'ok': True}),
        ])
        result = GameServices(engine, poll_interval=0).package_install('192.0.2.5', '/tmp/game.pkg', cancel=cancel)
        self.assertEqual(result['state'], 'canceled')

    def test_package_install_busy_reason_during_status_is_a_conflict(self):
        engine = FakeEngine([
            ('POST', '/api/pkg/install', {'job': 'j'}),
            ('GET', '/api/pkg/install/status?job=j', {'job': 'j', 'phase': 'failed', 'reason': 'busy'}),
        ])
        result = GameServices(engine, poll_interval=0).package_install('192.0.2.5', '/tmp/game.pkg')
        self.assertEqual(result['state'], 'conflict')

    def test_install_timeout_scales_with_size_with_a_ten_minute_floor(self):
        self.assertEqual(install_timeout(0), 600)
        self.assertEqual(install_timeout(100 << 20), 600)
        self.assertGreater(install_timeout(50 << 30), 6 * 3600)

    def test_write_cheats_creates_eta_hen_folders_and_skips_bad_members(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('cheats/json/CUSA12345_01.00.json', '{"a": 1}')
            archive.writestr('CUSA12345_01.00.shn', b'<xml/>')
            archive.writestr('../../etc/CUSA12345.mc4', b'evil')
            archive.writestr('readme.txt', b'x')
        files = FakeFiles()
        result = write_cheats(files, parse_cheat_zip(data.getvalue(), title='CUSA12345'))
        self.assertEqual(sorted(result['written']), ['/data/etaHEN/cheats/json/CUSA12345_01.00.json',
                                                     '/data/etaHEN/cheats/shn/CUSA12345_01.00.shn'])
        self.assertEqual(files.uploads['/data/etaHEN/cheats/shn/CUSA12345_01.00.shn'], b'<xml/>')
        self.assertIn('/data/etaHEN/cheats', files.dirs)
        self.assertNotIn('/etc', ' '.join(files.uploads))
        self.assertIn('重新加载', result['note'])
        self.assertTrue(any('readme.txt' in warning or '..' in warning for warning in result['warnings']))

    def test_write_cheats_keeps_going_after_one_upload_fails(self):
        files = FakeFiles(fail='a')
        parsed = {'files': [{'format': 'json', 'name': 'a', 'data': b'1'},
                            {'format': 'json', 'name': 'b', 'data': b'2'}], 'warnings': []}
        result = write_cheats(files, parsed)
        self.assertEqual(result['written'], ['/data/etaHEN/cheats/json/b'])
        self.assertTrue(any('失败' in warning for warning in result['warnings']))

    def test_cheat_zip_normalizes_eta_hen_wrapper_and_validates_identity(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('wrapper/etaHEN/cheats/json/CUSA12345.json', '{}')
            archive.writestr('wrapper/etaHEN/cheats/shn/CUSA12345_01.00.shn', b'shn')
            archive.writestr('wrapper/etaHEN/cheats/mc4/PPSA99999_01.00.mc4', b'bad')
        result = parse_cheat_zip(data.getvalue(), title='CUSA12345', version='01.00')
        self.assertEqual([item['format'] for item in result['files']], ['json', 'shn'])
        self.assertTrue(any('PPSA99999' in warning for warning in result['warnings']))

    def test_cheat_plan_routes_all_formats_and_requires_explicit_transfer(self):
        result = plan_cheat_import([
            'CUSA12345.json', 'CUSA12345_01.00.shn', 'CUSA12345_01.00.mc4',
        ], title='CUSA12345', version='01.00')
        self.assertEqual([item['destination'] for item in result['files']], [
            '/data/etaHEN/cheats/json/CUSA12345.json',
            '/data/etaHEN/cheats/shn/CUSA12345_01.00.shn',
            '/data/etaHEN/cheats/mc4/CUSA12345_01.00.mc4',
        ])
        self.assertTrue(any('传输' in warning and '加载' in warning for warning in result['warnings']))

    def test_engine_cheat_import_rejects_shn_and_mc4_direct_import(self):
        engine = FakeEngine([])
        services = GameServices(engine)
        for path in ('/tmp/CUSA12345_01.00.shn', '/tmp/CUSA12345_01.00.mc4'):
            with self.subTest(path=path), self.assertRaisesRegex(GameServiceError, 'JSON/XML'):
                services.cheat_import('192.0.2.5', path)
        self.assertEqual(engine.calls, [])


class FakeFiles:
    def __init__(self, fail=None):
        self.dirs, self.uploads, self.fail = set(), {}, fail

    def mkdir(self, path):
        if path in self.dirs:
            raise OSError('exists')
        self.dirs.add(path)

    def upload(self, directory, name, chunks, length, overwrite=False):
        if name == self.fail:
            raise OSError('disk full')
        self.uploads[f'{directory}/{name}'] = b''.join(chunks)


class FakeEngine:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, path, body=None, timeout=20):
        self.calls.append((method, path, body))
        if not self.responses:
            raise AssertionError('unexpected engine call')
        expected_method, expected_path, response = self.responses.pop(0)
        if (method, path) != (expected_method, expected_path):
            raise AssertionError(f'expected {(expected_method, expected_path)}, got {(method, path)}')
        return response


if __name__ == '__main__':
    unittest.main()
