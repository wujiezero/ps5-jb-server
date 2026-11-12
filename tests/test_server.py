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
