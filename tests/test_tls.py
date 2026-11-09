import http.client
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from server import AdminServer, AdminHandler, AuthManager


class FakeSettings:
    """Loopback-only bind, matching the server's own guard checks."""

    def __init__(self):
        self.subnet = '127.0.0.1/32'
        self.host_ip = '127.0.0.1'


class FakeApp:
    def __init__(self):
        self.settings = FakeSettings()

    def state(self):
        return {'config': {'domains': [], 'networks': [], 'upstream_dns': '192.0.2.1'}, 'logs': []}


class TLSAdmissionTests(unittest.TestCase):
    def test_idle_tls_clients_do_not_block_admin_accept_loop(self):
        with tempfile.TemporaryDirectory() as temp:
            cert, key = str(Path(temp) / 'cert.pem'), str(Path(temp) / 'key.pem')
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                            '-subj', '/CN=127.0.0.1', '-days', '1', '-keyout', key, '-out', cert],
                           capture_output=True, check=True, timeout=10)
            server = AdminServer(('127.0.0.1', 0), AdminHandler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            server.tls_context = context
            server.app = FakeApp()
            server.static_dir = Path(__file__).resolve().parents[1] / 'web'
            server.auth = AuthManager(AuthManager.make_credentials('a-long-test-password'))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            sockets = []
            try:
                # These clients connect but never send ClientHello. They must
                # occupy bounded workers, rather than the sole accept loop.
                for _ in range(4):
                    sockets.append(socket.create_connection(server.server_address, timeout=1))
                conn = http.client.HTTPSConnection(*server.server_address, timeout=1,
                                                   context=ssl._create_unverified_context())
                try:
                    conn.request('GET', '/api/state')
                    response = conn.getresponse()
                    response.read()
                    self.assertEqual(response.status, 401)
                finally:
                    conn.close()
            finally:
                for client in sockets:
                    client.close()
                server.shutdown()
                server.server_close()


if __name__ == '__main__': unittest.main()
