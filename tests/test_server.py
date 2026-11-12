import http.client
import json
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
try:
    from server import AuthManager, AdminServer, AdminHandler
except ImportError:
    AuthManager = AdminServer = AdminHandler = None


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(AuthManager, 'Authenticated admin server is required')
        self.auth = AuthManager(AuthManager.make_credentials('a-long-test-password'))
    def test_password_is_hashed(self):
        creds = AuthManager.make_credentials('a-long-test-password')
        self.assertNotIn('a-long-test-password', json.dumps(creds))
        self.assertTrue(self.auth.verify('a-long-test-password'))
        self.assertFalse(self.auth.verify('wrong'))
    def test_sessions_can_be_revoked(self):
        token, csrf = self.auth.login('127.0.0.1', 'a-long-test-password')
        self.assertEqual(self.auth.session(token)['csrf'], csrf)
        self.auth.logout(token)
        self.assertIsNone(self.auth.session(token))
    def test_failed_logins_rate_limited(self):
        for _ in range(6):
            with self.assertRaises(PermissionError):
                self.auth.login('127.0.0.1', 'bad')
        with self.assertRaisesRegex(PermissionError, '稍后'):
            self.auth.login('127.0.0.1', 'a-long-test-password')


class FakeSettings:
    """Bind to loopback only: the admin guard reads these two fields."""

    def __init__(self):
        self.subnet = '127.0.0.1/32'
        self.host_ip = '127.0.0.1'


class FakeApp:
    def __init__(self):
        self.changed = []
        self.settings = FakeSettings()
    def state(self): return {'config': {'domains': [], 'networks': [], 'upstream_dns': '192.0.2.1'}, 'logs': []}
    def change(self, body=None, rollback=False): self.changed.append((body, rollback))


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(AdminServer, 'Admin server implementation is required')
        self.app = FakeApp()
        self.auth = AuthManager(AuthManager.make_credentials('a-long-test-password'))
        self.server = AdminServer(('127.0.0.1', 0), AdminHandler)
        self.server.app = self.app
        self.server.auth = self.auth
        self.server.static_dir = Path(__file__).resolve().parents[1] / 'web'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
    def tearDown(self):
        if hasattr(self, 'server'):
            self.server.shutdown()
            self.server.server_close()
    def request(self, method, path, data=None, cookie=None, csrf=None, origin=None, host=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        headers = {'Content-Type': 'application/json'}
        if cookie: headers['Cookie'] = cookie
        if csrf: headers['X-CSRF-Token'] = csrf
        if origin: headers['Origin'] = origin
        if host: headers['Host'] = host
        conn.request(method, path, json.dumps(data) if data is not None else None, headers)
        response = conn.getresponse()
        result = response.read()
        info = dict(response.getheaders())
        conn.close()
        return response.status, info, json.loads(result) if result else {}
    def login(self):
        status, headers, result = self.request('POST', '/api/login', {'password': 'a-long-test-password'})
        self.assertEqual(status, 200)
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Lax', headers['Set-Cookie'])
        return headers['Set-Cookie'].split(';')[0], result['csrf']
    def test_state_requires_authentication(self):
        self.assertEqual(self.request('GET', '/api/state')[0], 401)
    def test_authenticated_state_contains_csrf(self):
        cookie, csrf = self.login()
        code, _, body = self.request('GET', '/api/state', cookie=cookie)
        self.assertEqual(code, 200)
        self.assertEqual(body['csrf'], csrf)
    def test_write_requires_csrf(self):
        cookie, _ = self.login()
        self.assertEqual(self.request('POST', '/api/config', {}, cookie=cookie)[0], 403)
        self.assertEqual(self.app.changed, [])
    def test_valid_write_calls_apply(self):
        cookie, csrf = self.login()
        code, _, _ = self.request('POST', '/api/config', {'domains': []}, cookie, csrf)
        self.assertEqual(code, 200)
        self.assertEqual(len(self.app.changed), 1)
    def test_foreign_origin_rejected(self):
        self.assertEqual(self.request('POST', '/api/login', {'password': 'a-long-test-password'}, origin='https://evil.example')[0], 403)
    def test_dns_rebinding_host_rejected(self):
        self.assertEqual(self.request('POST', '/api/login', {'password': 'a-long-test-password'}, host='evil.example')[0], 403)
    def test_logout_revokes_cookie(self):
        cookie, csrf = self.login()
        self.assertEqual(self.request('POST', '/api/logout', {}, cookie, csrf)[0], 200)
        self.assertEqual(self.request('GET', '/api/state', cookie=cookie)[0], 401)


class StaticFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        static = Path(self.tmp.name)
        for name, body in (('index.html', '<p>'), ('app.js', '//'), ('style.css', 'a{}'),
                           ('extra.js', '// split'), ('notes.php', '<?php'), ('.hidden.js', 'x')):
            (static / name).write_text(body)
        (static / 'sub').mkdir()
        (static / 'sub' / 'x.js').write_text('x')
        self.server = AdminServer(('127.0.0.1', 0), AdminHandler)
        self.server.app = FakeApp()
        self.server.auth = AuthManager(AuthManager.make_credentials('a-long-test-password'))
        self.server.static_dir = static
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def get(self, path):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        conn.request('GET', path)
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, response.getheader('Content-Type'), body

    def test_matching_files_are_served_with_their_type(self):
        self.assertEqual(self.get('/')[2], b'<p>')
        self.assertEqual(self.get('/extra.js')[:2], (200, 'text/javascript; charset=utf-8'))
        self.assertEqual(self.get('/style.css')[1], 'text/css; charset=utf-8')
        self.assertEqual(self.get('/index.html')[1], 'text/html; charset=utf-8')

    def test_unknown_or_unmatched_names_are_404(self):
        for path in ('/missing.js', '/notes.php', '/.hidden.js', '/sub/x.js', '/../app.js',
                     '/%2e%2e/app.js', '/APP.JS', '/app.js/'):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 404)


class KeepAliveTests(unittest.TestCase):
    """HTTP/1.1: persistent connections, ETag revalidation and gzip (B2)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.static = Path(self.tmp.name)
        (self.static / 'index.html').write_text('<p>hello</p>')
        (self.static / 'app.js').write_text('// ' + 'const x = 1;\n' * 400)
        (self.static / 'style.css').write_text('a{}')
        self.app = FakeApp()
        self.server = AdminServer(('127.0.0.1', 0), AdminHandler)
        self.server.app = self.app
        self.server.auth = AuthManager(AuthManager.make_credentials('a-long-test-password'))
        self.server.static_dir = self.static
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)

    def tearDown(self):
        self.conn.close()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def send(self, method, path, body=None, headers=None):
        self.conn.request(method, path, body, headers or {})
        response = self.conn.getresponse()
        return response, response.read()

    def login_cookie(self):
        response, body = self.send('POST', '/api/login', json.dumps({'password': 'a-long-test-password'}),
                                   {'Content-Type': 'application/json'})
        return response.getheader('Set-Cookie').split(';')[0], json.loads(body)['csrf']

    def test_one_connection_serves_several_requests(self):
        self.assertEqual(self.conn.__class__.__name__, 'HTTPConnection')
        first, _ = self.send('GET', '/api/version')
        socket_before = self.conn.sock
        second, _ = self.send('GET', '/style.css')
        cookie, _ = self.login_cookie()
        third, body = self.send('GET', '/api/state', headers={'Cookie': cookie})
        self.assertEqual((first.status, second.status, third.status), (200, 200, 200))
        self.assertIs(self.conn.sock, socket_before, 'the connection must be reused')
        self.assertIsNone(third.getheader('Connection'))
        self.assertEqual(self.server.SLOTS, 64)

    def test_unread_request_body_closes_the_connection(self):
        response, _ = self.send('POST', '/api/repo/upload?filename=x.elf', b'x' * 5000,
                                {'Content-Type': 'application/octet-stream'})
        self.assertEqual(response.status, 401)
        self.assertEqual(response.getheader('Connection'), 'close')
        # A body that was fully read keeps the connection open.
        self.conn.close()
        response, _ = self.send('POST', '/api/login', json.dumps({'password': 'wrong'}),
                                {'Content-Type': 'application/json'})
        self.assertEqual(response.status, 401)
        self.assertIsNone(response.getheader('Connection'))

    def test_chunked_bodies_are_refused_and_close(self):
        self.conn.putrequest('POST', '/api/config')
        self.conn.putheader('Transfer-Encoding', 'chunked')
        self.conn.putheader('Content-Type', 'application/json')
        self.conn.endheaders()
        self.conn.send(b'2\r\n{}\r\n0\r\n\r\n')
        response = self.conn.getresponse()
        response.read()
        self.assertEqual(response.status, 400)
        self.assertEqual(response.getheader('Connection'), 'close')

    def test_static_files_revalidate_with_etag(self):
        response, body = self.send('GET', '/app.js')
        etag = response.getheader('ETag')
        self.assertRegex(etag, r'^"[0-9a-f]{16}"$')
        self.assertEqual(response.getheader('Cache-Control'), 'no-cache')
        self.assertEqual(response.getheader('Vary'), 'Accept-Encoding')
        again, rest = self.send('GET', '/app.js', headers={'If-None-Match': etag})
        self.assertEqual(again.status, 304)
        self.assertEqual(rest, b'')
        self.assertEqual(again.getheader('ETag'), etag)
        # The connection survives a 304 and a changed file gets a new tag.
        (self.static / 'app.js').write_text('// changed and longer')
        changed, body = self.send('GET', '/app.js', headers={'If-None-Match': etag})
        self.assertEqual(changed.status, 200)
        self.assertNotEqual(changed.getheader('ETag'), etag)
        self.assertEqual(body, b'// changed and longer')

    def test_static_gzip_is_smaller_and_identical_after_decompression(self):
        import gzip
        plain, raw = self.send('GET', '/app.js')
        packed, data = self.send('GET', '/app.js', headers={'Accept-Encoding': 'gzip, deflate, br'})
        self.assertIsNone(plain.getheader('Content-Encoding'))
        self.assertEqual(packed.getheader('Content-Encoding'), 'gzip')
        self.assertEqual(gzip.decompress(data), raw)
        self.assertLess(len(data) * 3, len(raw))
        self.assertNotEqual(packed.getheader('ETag'), plain.getheader('ETag'))
        refused, _ = self.send('GET', '/app.js', headers={'Accept-Encoding': 'gzip;q=0'})
        self.assertIsNone(refused.getheader('Content-Encoding'))

    def test_large_json_is_compressed_only_when_accepted(self):
        import gzip
        self.app.state = lambda: {'config': {}, 'logs': [{'n': i, 'text': 'x' * 20} for i in range(100)]}
        cookie, _ = self.login_cookie()
        plain, raw = self.send('GET', '/api/state', headers={'Cookie': cookie})
        packed, data = self.send('GET', '/api/state', headers={'Cookie': cookie, 'Accept-Encoding': 'gzip'})
        self.assertIsNone(plain.getheader('Content-Encoding'))
        self.assertEqual(plain.getheader('Vary'), 'Accept-Encoding')
        self.assertEqual(packed.getheader('Content-Encoding'), 'gzip')
        self.assertEqual(json.loads(gzip.decompress(data)), json.loads(raw))
        self.assertEqual(packed.getheader('Cache-Control'), 'no-store')
        small, body = self.send('GET', '/api/version', headers={'Accept-Encoding': 'gzip'})
        self.assertIsNone(small.getheader('Content-Encoding'), 'small JSON stays plain')
        json.loads(body)

    def test_not_modified_and_successful_reads_log_at_debug(self):
        import logging
        response, _ = self.send('GET', '/app.js')
        with self.assertLogs('http', level='DEBUG') as logs:
            self.send('GET', '/app.js', headers={'If-None-Match': response.getheader('ETag')})
        self.assertTrue(all(line.startswith('DEBUG') for line in logs.output), logs.output)
        self.assertIn('304', logs.output[-1])
        del logging


class StartupTests(unittest.TestCase):
    def test_firewall_failure_keeps_diagnostics_available_and_forwarding_off(self):
        import server
        self.assertTrue(hasattr(server, 'initialize_engine'), 'Startup needs a recoverable diagnostic path')
        class Engine:
            last_error = None
            gateway_error = None
            def apply_rules(self, config, leases): raise RuntimeError('no net admin capability')
            class store:
                @staticmethod
                def snapshot(): return {}
        calls = []
        engine = Engine()
        self.assertFalse(server.initialize_engine(engine, forwarder=lambda enabled: calls.append(enabled)))
        self.assertEqual(calls, [False])
        self.assertIn('no net admin capability', engine.gateway_error)


if __name__ == '__main__': unittest.main()


class SessionPersistenceTests(unittest.TestCase):
    """Sessions survive a service restart and renew while in use."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / 'sessions.json'
        self.credentials = AuthManager.make_credentials('correct-horse-battery')

    def tearDown(self):
        self.tmp.cleanup()

    def test_restart_keeps_session(self):
        first = AuthManager(self.credentials, self.path)
        token, csrf = first.login('127.0.0.1', 'correct-horse-battery')
        self.assertTrue(self.path.exists())
        # Stored token is hashed, never plaintext.
        self.assertNotIn(token, self.path.read_text())

        second = AuthManager(self.credentials, self.path)
        session = second.session(token)
        self.assertIsNotNone(session, 'session must survive a restart')
        self.assertEqual(session['csrf'], csrf)

    def test_logout_removes_persisted_session(self):
        auth = AuthManager(self.credentials, self.path)
        token, _ = auth.login('127.0.0.1', 'correct-horse-battery')
        auth.logout(token)
        self.assertIsNone(AuthManager(self.credentials, self.path).session(token))

    def test_expired_session_is_dropped(self):
        auth = AuthManager(self.credentials, self.path)
        token, _ = auth.login('127.0.0.1', 'correct-horse-battery')
        key = AuthManager._token_key(token)
        auth.sessions[key]['expires'] = time.time() - 1
        auth._save()
        self.assertIsNone(AuthManager(self.credentials, self.path).session(token))

    def test_sliding_expiry_renews(self):
        auth = AuthManager(self.credentials, self.path)
        token, _ = auth.login('127.0.0.1', 'correct-horse-battery')
        key = AuthManager._token_key(token)
        auth.sessions[key]['expires'] = time.time() + 10
        before = auth.sessions[key]['expires']
        auth.session(token)
        self.assertGreater(auth.sessions[key]['expires'], before + 3600)


class InterfaceTests(unittest.TestCase):
    """[network] interface: configured value first, else detected, never a stale eth0."""

    ADDR = ('1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n'
            '2: ens18    inet 192.0.2.10/24 brd 192.0.2.255 scope global ens18\\       valid_lft forever\n'
            '3: wg0    inet 10.9.0.1/24 scope global wg0\\       valid_lft forever\n')
    ROUTE = 'default via 192.0.2.1 dev enp3s0 proto dhcp src 192.0.2.10 metric 100\n'

    def runner(self, addr='', route='', fail=()):
        import subprocess
        from types import SimpleNamespace
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            kind = 'addr' if 'addr' in argv else 'route'
            if kind in fail:
                raise FileNotFoundError('ip')
            return SimpleNamespace(returncode=0, stdout=addr if kind == 'addr' else route)
        run.calls = calls
        run.subprocess = subprocess
        return run

    def test_interface_holding_host_ip_wins(self):
        from settings import detect_interface
        self.assertEqual(detect_interface('192.0.2.10', runner=self.runner(self.ADDR, self.ROUTE)), 'ens18')

    def test_default_route_when_no_interface_holds_host_ip(self):
        from settings import detect_interface
        self.assertEqual(detect_interface('192.0.2.99', runner=self.runner(self.ADDR, self.ROUTE)), 'enp3s0')
        self.assertEqual(detect_interface('192.0.2.10', runner=self.runner(fail=('addr',), route=self.ROUTE)),
                         'enp3s0')

    def test_eth0_as_last_resort(self):
        from settings import detect_interface
        self.assertEqual(detect_interface('192.0.2.10', runner=self.runner(fail=('addr', 'route'))), 'eth0')
        self.assertEqual(detect_interface('192.0.2.10', runner=self.runner('', '')), 'eth0')

    def test_detected_names_must_be_safe_for_nft(self):
        from settings import detect_interface
        hostile = '2: ev"il;    inet 192.0.2.10/24 scope global x\n'
        route = 'default via 192.0.2.1 dev bad"name proto static\n'
        self.assertEqual(detect_interface('192.0.2.10', runner=self.runner(hostile, route)), 'eth0')

    def test_configured_value_is_read_and_overrides_detection(self):
        import os
        from unittest import mock
        from settings import Settings
        settings = Settings.from_mapping({'network': {'host_ip': '192.0.2.10', 'subnet': '192.0.2.0/24',
                                                      'upstream_dns': '192.0.2.1', 'interface': 'br0'}})
        self.assertEqual(settings.interface, 'br0')
        with mock.patch.dict(os.environ, {'PS5JB_INTERFACE': 'vmbr1'}):
            self.assertEqual(Settings.from_mapping({}).interface, 'vmbr1')
        empty = Settings.from_mapping({'network': {'host_ip': '192.0.2.10', 'upstream_dns': '192.0.2.1'}})
        self.assertEqual(empty.interface, '', 'from_mapping must not run detection')

    def test_engine_renders_rules_for_the_given_interface(self):
        from policy import ConfigStore
        from server import create_engine
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / 'config.json', {'upstream_dns': '192.0.2.1'})
            engine = create_engine(store, '192.0.2.10', '192.0.2.0/24', interface='ens18')
            rules = engine.firewall.render(store.snapshot(), {})
            self.assertIn('iifname "ens18" oifname "ens18"', rules)
            self.assertIn('oifname "ens18" ip saddr', rules)
            self.assertNotIn('eth0', rules)
            with self.assertRaises(ValueError):
                create_engine(store, '192.0.2.10', '192.0.2.0/24', interface='ens18"; flush ruleset')

    def test_state_and_report_expose_the_interface(self):
        from policy import ConfigStore
        from server import Application, create_engine, diagnostic_report
        from types import SimpleNamespace

        class Counting:
            interface = 'ens18'

            def counters(self):
                return {}

            def apply(self, *_):
                pass
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / 'config.json', {'upstream_dns': '192.0.2.1'})
            app = Application(create_engine(store, '192.0.2.10', '192.0.2.0/24', firewall=Counting()))
            app.units = lambda: {role: {'name': role, 'state': 'active'} for role in ('server', 'host', 'repo')}
            self.assertEqual(app.state()['status']['interface'], 'ens18')
            self.assertIn('网关网卡：ens18', diagnostic_report(SimpleNamespace(app=app)))
