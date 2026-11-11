"""Regression: pldmgr v0.6.3 normalizes versioned filenames to one slot.

The real HTTP handler defaults mode=new and responds with the generic 500
'Failed to receive or save request' when the same local slot already exists.
The read-only /manage:check API selects the slot; do not guess from basenames.
"""
import json
from pathlib import Path
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'server'), str(Path(__file__).resolve().parent)]
from ps5 import ConsoleError, PldmgrClient, HostManager
from test_ps5 import FakeConfig


class UploadHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, body, status=200):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        from urllib.parse import urlsplit, parse_qs
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        self.server.calls.append(('GET', url.path, query))
        if url.path == '/manage:check':
            return self.reply(self.server.check_result, self.server.check_status)
        if url.path == '/list_payloads':
            return self.reply({'items': self.server.items})
        return self.reply({'error': 'not found'}, 404)

    def do_POST(self):
        from urllib.parse import urlsplit, parse_qs
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        data = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        self.server.calls.append(('POST', url.path, query))
        self.server.bodies.append(data)
        if self.headers.get('X-PLDMGR-Request') != '1':
            return self.reply({'error': 'missing marker'}, 403)
        if url.path != '/manage:upload':
            return self.reply({'error': 'not found'}, 404)
        mode = query.get('mode', ['new'])[0]
        if self.server.fail_write or self.server.has_existing and mode != 'replace':
            return self.reply({'error': 'Failed to receive or save request'}, 500)
        if self.server.publish:
            self.server.items = [{'slot': 'local/zftpd', 'filename': query['filename'][0],
                                  'path': '/data/pldmgr/payloads/local/zftpd/' + query['filename'][0]}]
        return self.reply({'ok': True})


class SlotUploadTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), UploadHandler)
        self.server.calls, self.server.bodies = [], []
        self.server.has_existing = True
        self.server.fail_write = False
        self.server.publish = True
        self.server.items = [{'slot': 'local/zftpd', 'filename': 'zftpd.elf',
                              'path': '/data/pldmgr/payloads/local/zftpd/zftpd.elf'}]
        self.server.check_result = {'status': 'ok', 'key': 'zftpd', 'folder_name': 'zftpd',
                                    'folder_exists': True, 'file_exists': True,
                                    'existing': {'slot': 'local/zftpd', 'filename': 'zftpd.elf'}}
        self.server.check_status = 200
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = PldmgrClient('127.0.0.1', self.server.server_port)

    def test_old_version_slot_is_replaced_without_deleting_old_file_first(self):
        data = b'\x7fELF-new-payload'
        self.client.upload_payload('zftpd-ps5-v1.7.0.elf', data)
        self.assertEqual(self.server.calls[:2], [
            ('GET', '/manage:check', {'filename': ['zftpd-ps5-v1.7.0.elf']}),
            ('POST', '/manage:upload', {'filename': ['zftpd-ps5-v1.7.0.elf'], 'mode': ['replace']}),
        ])
        self.assertEqual(self.server.bodies, [data])
        self.assertEqual(self.server.items[0]['slot'], 'local/zftpd')
        self.assertEqual(self.server.items[0]['filename'], 'zftpd-ps5-v1.7.0.elf')

    def test_empty_slot_uses_explicit_new_mode(self):
        self.server.has_existing = False
        self.server.check_result = {'status': 'ok', 'key': 'zftpd', 'folder_name': 'zftpd',
                                    'folder_exists': False, 'file_exists': False, 'existing': None}
        self.client.upload_payload('zftpd-ps5-v1.7.0.elf', b'payload')
        self.assertEqual(self.server.calls[1][2]['mode'], ['new'])

    def test_failed_slot_check_does_not_attempt_destructive_upload(self):
        self.server.check_status = 500
        self.server.check_result = {'error': 'Failed to read slot metadata'}
        with self.assertRaises(ConsoleError):
            self.client.upload_payload('zftpd-ps5-v1.7.0.elf', b'payload')
        self.assertEqual(self.server.bodies, [])

    def test_malformed_slot_check_does_not_attempt_destructive_upload(self):
        for result in ({}, {'status': 'ok', 'folder_exists': 'false'},
                       {'status': 'error', 'message': 'Payload Manager updates require the official repository'}):
            with self.subTest(result=result):
                self.server.check_result = result
                with self.assertRaises(ConsoleError):
                    self.client.upload_payload('zftpd-ps5-v1.7.0.elf', b'payload')
                self.assertEqual(self.server.bodies, [])

    def test_missing_legacy_check_endpoint_uses_only_nonreplacing_upload(self):
        self.server.check_status = 404
        self.server.has_existing = False
        self.client.upload_payload('zftpd-ps5-v1.7.0.elf', b'payload')
        self.assertEqual(self.server.calls[-1][2].get('mode'), ['new'])

    def test_disk_error_does_not_retry_or_delete_payload(self):
        self.server.fail_write = True
        with self.assertRaises(ConsoleError):
            self.client.upload_payload('zftpd-ps5-v1.7.0.elf', b'payload')
        self.assertEqual(len(self.server.bodies), 1)
        self.assertEqual(self.server.items[0]['filename'], 'zftpd.elf')

    def test_install_and_run_does_not_silently_fallback_after_disk_error(self):
        self.server.fail_write = True
        filename = 'zftpd-ps5-v1.7.0.elf'
        manager = HostManager(FakeConfig(pldmgr_port=self.server.server_port),
                              lambda: {'items': [{'filename': filename, 'url': 'http://127.0.0.1/payload'}]},
                              fetch_bytes=lambda _: b'\x7fELF-payload')
        with mock.patch('ps5._tcp_open', return_value=True), mock.patch('ps5.ElfldrClient.run_url') as fallback:
            with self.assertRaisesRegex(ConsoleError, '500'):
                manager.install('127.0.0.1', filename, run=True)
            fallback.assert_not_called()

    def test_missing_confirmation_does_not_run_old_or_external_payload(self):
        self.server.publish = False
        filename = 'zftpd-ps5-v1.7.0.elf'
        manager = HostManager(FakeConfig(pldmgr_port=self.server.server_port),
                              lambda: {'items': [{'filename': filename, 'url': 'http://127.0.0.1/payload'}]},
                              fetch_bytes=lambda _: b'\x7fELF-payload')
        with mock.patch('ps5._tcp_open', return_value=True), mock.patch('ps5.ElfldrClient.run_url') as fallback:
            with self.assertRaisesRegex(ConsoleError, '确认'):
                manager.install('127.0.0.1', filename, run=True)
            fallback.assert_not_called()

    def test_same_filename_in_repository_slot_does_not_mask_missing_local_upload(self):
        filename = 'zftpd-ps5-v1.7.0.elf'
        self.server.publish = False
        self.server.items = [{'slot': 'repo/zftpd-old', 'filename': filename,
                              'path': '/data/pldmgr/payloads/repo/zftpd-old/' + filename}]
        manager = HostManager(FakeConfig(pldmgr_port=self.server.server_port),
                              lambda: {'items': [{'filename': filename, 'url': 'http://127.0.0.1/payload'}]},
                              fetch_bytes=lambda _: b'\x7fELF-payload')
        with mock.patch.object(manager, 'host', return_value={}), self.assertRaisesRegex(ConsoleError, '确认|验证'):
            manager.install('127.0.0.1', filename)

    def test_run_uses_uploaded_local_slot_not_same_named_repository_copy(self):
        filename = 'zftpd-ps5-v1.7.0.elf'
        self.server.publish = False
        self.server.items = [
            {'slot': 'repo/zftpd-old', 'filename': filename,
             'path': '/data/pldmgr/payloads/repo/zftpd-old/' + filename},
            {'slot': 'local/zftpd', 'filename': filename,
             'path': '/data/pldmgr/payloads/local/zftpd/' + filename},
        ]
        manager = HostManager(FakeConfig(pldmgr_port=self.server.server_port),
                              lambda: {'items': [{'filename': filename, 'url': 'http://127.0.0.1/payload'}]},
                              fetch_bytes=lambda _: b'\x7fELF-payload')
        client = manager._client('127.0.0.1')
        with mock.patch.object(manager, '_client', return_value=client), \
             mock.patch.object(manager, 'host', return_value={}), mock.patch.object(client, 'run') as run:
            result = manager.install('127.0.0.1', filename, run=True)
            self.assertEqual(result['path'], '/data/pldmgr/payloads/local/zftpd/' + filename)
            run.assert_called_once_with(result['path'])

    def test_legacy_check_404_still_excludes_same_named_repository_copy(self):
        self.server.check_status = 404
        self.server.has_existing = False
        self.server.publish = False
        filename = 'zftpd-ps5-v1.7.0.elf'
        self.server.items = [
            {'slot': 'repo/zftpd-old', 'filename': filename,
             'path': '/data/pldmgr/payloads/repo/zftpd-old/' + filename},
            {'slot': 'local/zftpd', 'filename': filename,
             'path': '/data/pldmgr/payloads/local/zftpd/' + filename},
        ]
        manager = HostManager(FakeConfig(pldmgr_port=self.server.server_port),
                              lambda: {'items': [{'filename': filename, 'url': 'http://127.0.0.1/payload'}]},
                              fetch_bytes=lambda _: b'\x7fELF-payload')
        client = manager._client('127.0.0.1')
        with mock.patch.object(manager, '_client', return_value=client), \
             mock.patch.object(manager, 'host', return_value={}), mock.patch.object(client, 'run') as run:
            result = manager.install('127.0.0.1', filename, run=True)
            self.assertEqual(result['path'], '/data/pldmgr/payloads/local/zftpd/' + filename)
            run.assert_called_once_with(result['path'])

    def test_manager_requires_new_filename_to_be_visible_after_upload(self):
        self.server.publish = False
        filename = 'zftpd-ps5-v1.7.0.elf'
        manager = HostManager(FakeConfig(pldmgr_port=self.server.server_port),
                              lambda: {'items': [{'filename': filename, 'url': 'http://127.0.0.1/payload'}]},
                              fetch_bytes=lambda _: b'\x7fELF-payload')
        with mock.patch.object(manager, 'host', return_value={}), self.assertRaisesRegex(ConsoleError, '确认|验证'):
            manager.install('127.0.0.1', filename)


if __name__ == '__main__':
    unittest.main()
