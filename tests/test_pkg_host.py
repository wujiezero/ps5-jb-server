"""PkgHost: a NAS PKG served to the console with Range, one token per job."""
import http.client
import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))

import pkg_host  # noqa: E402
from pkg_host import PkgHost, PkgHostError, parse_range  # noqa: E402

DATA = bytes(range(256)) * 40          # 10240 bytes


class Reader:
    def __init__(self, data):
        self.data, self.offset = data, 0

    def read(self, size):
        chunk = self.data[self.offset:self.offset + min(size, 700)]
        self.offset += len(chunk)
        return chunk

    def close(self):
        pass


class Client:
    opened = []

    def __init__(self, ranged=True):
        self.ranged = ranged

    def open(self, path):
        Client.opened.append((path, 0))
        return Reader(DATA)

    def open_at(self, path, offset):
        Client.opened.append((path, offset))
        return Reader(DATA[offset:]) if self.ranged else None

    def close(self):
        pass


class Store:
    def get(self, ident):
        return {'id': ident}


class ParseRangeTests(unittest.TestCase):
    def test_forms(self):
        self.assertIsNone(parse_range(None, 100))
        self.assertEqual(parse_range('bytes=0-0', 100), (0, 0))
        self.assertEqual(parse_range('bytes=10-', 100), (10, 99))
        self.assertEqual(parse_range('bytes=90-500', 100), (90, 99))
        self.assertEqual(parse_range('bytes=-10', 100), (90, 99))
        self.assertEqual(parse_range('bytes=-500', 100), (0, 99))

    def test_unsatisfiable(self):
        for header in ('bytes=100-', 'bytes=5-1', 'bytes=0-1,5-6', 'items=0-1', 'bytes=-', 'bytes=-0'):
            with self.subTest(header=header), self.assertRaises(ValueError):
                parse_range(header, 100)


class PkgHostTests(unittest.TestCase):
    def setUp(self):
        Client.opened = []
        self.now = [1000.0]
        self.ranged = True
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        self.host = PkgHost('127.0.0.1', port, Store(), lambda config: Client(self.ranged),
                            clock=lambda: self.now[0])
        self.addCleanup(self.host.stop)

    def publish(self, allowed=('192.0.2.5',)):
        return self.host.publish('job1', 'nas1', '/games/a.pkg', len(DATA), 1700000000, set(allowed))

    def request(self, method, path, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.host.port, timeout=5)
        try:
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_disabled_port_refuses_to_start(self):
        with self.assertRaises(PkgHostError):
            PkgHost('127.0.0.1', 0, Store(), Client).start()

    def test_url_is_short_and_tokenized(self):
        token, url = self.publish()
        self.assertTrue(url.startswith(f'http://127.0.0.1:{self.host.port}/p/'))
        self.assertTrue(url.endswith('.pkg'))
        self.assertLess(len(url), 127)
        self.assertIn(token, url)

    def test_full_get_and_ranges(self):
        token, _ = self.publish()
        path = f'/p/{token}.pkg'
        status, headers, body = self.request('GET', path)
        self.assertEqual((status, body), (200, DATA))
        self.assertEqual(headers['Accept-Ranges'], 'bytes')
        self.assertIn('GMT', headers['Last-Modified'])
        status, headers, body = self.request('GET', path, {'Range': 'bytes=0-0'})
        self.assertEqual((status, body), (206, DATA[:1]))
        self.assertEqual(headers['Content-Range'], f'bytes 0-0/{len(DATA)}')
        status, headers, body = self.request('GET', path, {'Range': 'bytes=5000-'})
        self.assertEqual((status, body), (206, DATA[5000:]))
        self.assertIn(('/games/a.pkg', 5000), Client.opened)
        status, headers, body = self.request('GET', path, {'Range': 'bytes=-100'})
        self.assertEqual((status, body), (206, DATA[-100:]))
        self.assertEqual(self.host.served(token), len(DATA) + 1 + len(DATA) - 5000 + 100)

    def test_head_has_length_and_no_body(self):
        token, _ = self.publish()
        status, headers, body = self.request('HEAD', f'/p/{token}.pkg')
        self.assertEqual((status, body), (200, b''))
        self.assertEqual(headers['Content-Length'], str(len(DATA)))
        self.assertEqual(Client.opened, [])

    def test_bad_range_is_416(self):
        token, _ = self.publish()
        status, headers, _ = self.request('GET', f'/p/{token}.pkg', {'Range': 'bytes=99999-'})
        self.assertEqual(status, 416)
        self.assertEqual(headers['Content-Range'], f'bytes */{len(DATA)}')

    def test_if_range_mismatch_sends_whole_file(self):
        token, _ = self.publish()
        status, headers, body = self.request('GET', f'/p/{token}.pkg', {'Range': 'bytes=10-', 'If-Range': '"old"'})
        self.assertEqual((status, body), (200, DATA))
        etag = headers['ETag']
        status, _, body = self.request('GET', f'/p/{token}.pkg', {'Range': 'bytes=10-', 'If-Range': etag})
        self.assertEqual((status, body), (206, DATA[10:]))

    def test_unknown_revoked_and_expired_tokens_are_404(self):
        token, _ = self.publish()
        self.assertEqual(self.request('GET', '/p/' + 'x' * 24 + '.pkg')[0], 404)
        self.assertEqual(self.request('GET', '/etc/passwd')[0], 404)
        self.now[0] += pkg_host.MAX_AGE + 1
        self.assertEqual(self.request('GET', f'/p/{token}.pkg')[0], 404)
        self.now[0] = 1000.0
        token, _ = self.host.publish('job2', 'nas1', '/games/a.pkg', len(DATA), 0, {'192.0.2.5'})
        self.assertEqual(self.request('HEAD', f'/p/{token}.pkg')[0], 200)
        self.host.revoke(token)
        self.assertEqual(self.request('HEAD', f'/p/{token}.pkg')[0], 404)

    def test_other_source_addresses_are_refused(self):
        token, _ = self.publish()
        with self.host.lock:
            self.host.tokens[token]['allowed'] = {'192.0.2.5'}
        self.assertEqual(self.request('GET', f'/p/{token}.pkg')[0], 403)
        self.assertEqual(Client.opened, [])

    def test_publish_always_allows_console_loopback_and_own_address(self):
        token, _ = self.publish(allowed=('192.0.2.5',))
        self.assertEqual(self.host.tokens[token]['allowed'], {'192.0.2.5', '127.0.0.1'})

    def test_writes_are_refused(self):
        token, _ = self.publish()
        self.assertEqual(self.request('POST', f'/p/{token}.pkg')[0], 405)
        self.assertEqual(self.request('DELETE', f'/p/{token}.pkg')[0], 405)

    def test_nas_without_range_support_is_502_for_offsets(self):
        self.ranged = False
        token, _ = self.publish()
        self.assertEqual(self.request('GET', f'/p/{token}.pkg', {'Range': 'bytes=100-'})[0], 502)
        self.assertEqual(self.request('GET', f'/p/{token}.pkg', {'Range': 'bytes=0-9'})[0], 206)


if __name__ == '__main__':
    unittest.main()
