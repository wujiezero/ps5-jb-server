"""Proxy selection from the environment, with a direct connection as the default."""
import http.server
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import netproxy


class _Handler(http.server.BaseHTTPRequestHandler):
    """Answers any GET with the payload its server was created with."""

    def do_GET(self):
        body = self.server.payload
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class NetProxyTests(unittest.TestCase):
    def setUp(self):
        self.direct = self.serve(b'direct')
        self.proxy = self.serve(b'proxied')

    def serve(self, payload):
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        server.payload = payload
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return f'http://127.0.0.1:{server.server_address[1]}'

    def get(self, url, env):
        """Fetch through the opener the service would build for ``env``."""
        with mock.patch.dict(os.environ, env, clear=True):
            with netproxy.opener(environ=env).open(url, timeout=5) as response:
                return response.read()

    def test_no_variable_means_a_direct_connection(self):
        self.assertEqual(netproxy.lookup({}), ('', '', ''))
        self.assertEqual(netproxy.proxies({}), {})
        self.assertEqual(netproxy.status({})['configured'], False)
        self.assertEqual(self.get(self.direct, {}), b'direct')

    def test_first_variable_wins_and_credentials_are_masked(self):
        env = {'http_proxy': 'http://127.0.0.1:1',
               'https_proxy': 'http://192.0.2.50:6152',
               'PS5JB_PROXY': 'http://user:token@192.0.2.51:6152'}
        url, name, error = netproxy.lookup(env)
        self.assertEqual((url, name, error), ('http://user:token@192.0.2.51:6152', 'PS5JB_PROXY', ''))
        state = netproxy.status(env)
        self.assertEqual((state['configured'], state['variable']), (True, 'PS5JB_PROXY'))
        self.assertEqual(state['url'], 'http://***@192.0.2.51:6152')

    def test_bare_host_and_port_gain_a_scheme(self):
        self.assertEqual(netproxy.lookup({'HTTPS_PROXY': '192.0.2.50:6152'}),
                         ('http://192.0.2.50:6152', 'HTTPS_PROXY', ''))

    def test_socks_is_reported_instead_of_silently_ignored(self):
        env = {'https_proxy': 'socks5://192.0.2.50:1080'}
        state = netproxy.status(env)
        self.assertFalse(state['configured'])
        self.assertIn('SOCKS', state['error'])
        self.assertEqual(netproxy.proxies(env), {})
        self.assertEqual(self.get(self.direct, env), b'direct')
        self.assertIn('代理配置不可用', netproxy.hint(env))

    def test_requests_use_the_proxy_unless_no_proxy_bypasses_it(self):
        proxied = {'http_proxy': self.proxy}
        self.assertEqual(self.get(self.direct, proxied), b'proxied')
        bypassed = {'http_proxy': self.proxy, 'no_proxy': '127.0.0.1'}
        self.assertEqual(self.get(self.direct, bypassed), b'direct')

    def test_seal_direct_pins_the_default_opener(self):
        env = {'http_proxy': self.proxy}
        self.addCleanup(urllib.request.install_opener, None)
        with mock.patch.dict(os.environ, env, clear=True):
            netproxy.seal_direct()
            with urllib.request.urlopen(self.direct, timeout=5) as response:
                self.assertEqual(response.read(), b'direct', 'LAN clients must stay direct')
            with netproxy.opener().open(self.direct, timeout=5) as response:
                self.assertEqual(response.read(), b'proxied', 'internet fetches still use the proxy')


if __name__ == '__main__':
    unittest.main()
