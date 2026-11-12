import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import notify
from nas import SecretBox
from notify import Notifier, NotifyError, NotifyStore

TOKEN = '123456789:AAHsecretTelegramTokenValue_123'
BARK_KEY = 'BarkDeviceKey12345'


class Reply:
    def read(self, size=-1):
        return b'{}'

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Stands in for default_opener: records every request, can fail on demand."""

    def __init__(self, failures=(), block=None):
        self.requests, self.failures, self.block = [], list(failures), block
        self.lock = threading.Lock()

    def __call__(self, url, use_proxy):
        outer = self

        class Opener:
            def open(self, request, timeout=None):
                if outer.block is not None:
                    outer.block.wait(5)
                with outer.lock:
                    outer.requests.append({'url': request.full_url, 'headers': dict(request.header_items()),
                                           'body': json.loads(request.data), 'proxy': use_proxy})
                    failure = outer.failures.pop(0) if outer.failures else None
                if failure is not None:
                    raise failure
                return Reply()
        return Opener()


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.box = SecretBox(root / 'nas.key')
        self.store = NotifyStore(root / 'notify.json', self.box)
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def test_secrets_are_sealed_and_never_returned(self):
        saved = self.store.save({'type': 'telegram', 'name': 'TG', 'chat_id': '-1001234', 'token': TOKEN})
        self.assertEqual(saved['has_secret'], True)
        self.assertNotIn('token', saved)
        self.assertNotIn('secret', saved)
        raw = (self.root / 'notify.json').read_text()
        self.assertNotIn(TOKEN, raw)
        self.assertNotIn(TOKEN, json.dumps(self.store.list_public()))
        self.assertEqual((self.root / 'notify.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.get(saved['id'])['token'], TOKEN)

    def test_edit_with_blank_secret_keeps_it_and_validation(self):
        saved = self.store.save({'type': 'bark', 'device_key': BARK_KEY})
        self.assertEqual(saved['server'], 'https://api.day.app')
        edited = self.store.save({'id': saved['id'], 'type': 'bark', 'name': 'Phone', 'device_key': ''})
        self.assertEqual(edited['name'], 'Phone')
        self.assertEqual(self.store.get(saved['id'])['device_key'], BARK_KEY)
        bad = [{'type': 'sms'}, {'type': 'webhook', 'url': 'ftp://x'}, {'type': 'webhook', 'url': ''},
               {'type': 'telegram', 'chat_id': 'abc', 'token': TOKEN},
               {'type': 'telegram', 'chat_id': '1', 'token': 'nope'},
               {'type': 'ntfy', 'topic': 'bad topic'},
               {'type': 'bark', 'server': 'javascript:alert(1)', 'device_key': BARK_KEY},
               {'id': 'missing', 'type': 'bark', 'device_key': BARK_KEY}]
        for raw in bad:
            with self.subTest(raw=raw):
                with self.assertRaises(NotifyError):
                    self.store.save(raw)

    def test_events_are_filtered_and_restore_without_key_drops_secrets(self):
        saved = self.store.save({'type': 'webhook', 'url': 'https://hooks.example/abc?key=s3cr3t',
                                 'events': ['transfer.failed', 'nonsense']})
        self.assertEqual(saved['events'], ['transfer.failed'])
        document = self.store.document()
        other = NotifyStore(self.root / 'other.json', SecretBox(self.root / 'other.key'))
        count, blank = other.restore(document, with_key=False)
        self.assertEqual((count, blank), (1, 1))
        self.assertEqual(other.list_public()[0]['has_secret'], False)
        self.assertNotIn('s3cr3t', (self.root / 'other.json').read_text())
        same = NotifyStore(self.root / 'notify.json', self.box)
        self.assertEqual(same.restore(document, with_key=True), (1, 0))
        self.assertEqual(same.get(saved['id'])['url'], 'https://hooks.example/abc?key=s3cr3t')


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = NotifyStore(self.root / 'notify.json', SecretBox(self.root / 'nas.key'))
        self.now = [1800000000.0]
        self.opener = FakeOpener()
        self.notifier = Notifier(self.store, opener=self.opener, clock=lambda: self.now[0],
                                 sleep=lambda s: None, state_path=self.root / 'notify-state.json')

    def tearDown(self):
        self.notifier.stop()
        self.tmp.cleanup()

    def test_request_format_for_each_channel_type(self):
        message = {'event': 'transfer.finished', 'title': '标题', 'body': '正文', 'time': 't'}
        cases = {
            'webhook': ({'type': 'webhook', 'url': 'http://192.0.2.7:8000/hook'}, 'http://192.0.2.7:8000/hook',
                        {'event': 'transfer.finished', 'title': '标题', 'body': '正文', 'time': 't', 'source': 'ps5-jb-server'}),
            'bark': ({'type': 'bark', 'server': 'https://api.day.app', 'device_key': BARK_KEY}, 'https://api.day.app/push',
                     {'device_key': BARK_KEY, 'title': '标题', 'body': '正文', 'group': 'PS5'}),
            'ntfy': ({'type': 'ntfy', 'server': 'https://ntfy.sh', 'topic': 'ps5-home', 'token': 'tk_abc'}, 'https://ntfy.sh/',
                     {'topic': 'ps5-home', 'title': '标题', 'message': '正文', 'tags': ['video_game']}),
            'telegram': ({'type': 'telegram', 'chat_id': '-100', 'token': TOKEN},
                         f'https://api.telegram.org/bot{TOKEN}/sendMessage',
                         {'chat_id': '-100', 'text': '标题\n正文', 'disable_web_page_preview': True}),
        }
        for kind, (channel, url, body) in cases.items():
            with self.subTest(kind=kind):
                request = notify.build_request(channel, message)
                self.assertEqual(request.full_url, url)
                self.assertEqual(request.get_method(), 'POST')
                self.assertEqual(json.loads(request.data), body)
        self.assertEqual(notify.build_request(cases['ntfy'][0], message).get_header('Authorization'), 'Bearer tk_abc')

    def test_subscribed_channels_get_the_event_once_a_minute(self):
        self.store.save({'type': 'webhook', 'url': 'http://192.0.2.7/a', 'events': ['transfer.finished']})
        self.store.save({'type': 'webhook', 'url': 'http://192.0.2.7/b', 'events': ['transfer.failed']})
        self.store.save({'type': 'webhook', 'url': 'http://192.0.2.7/c', 'events': ['transfer.finished'], 'enabled': False})
        self.assertEqual(self.notifier.emit('transfer.finished', '完成', key='j1'), 1)
        self.assertEqual(self.notifier.emit('transfer.finished', '完成', key='j1'), 0, 'deduplicated')
        self.now[0] += 61
        self.assertEqual(self.notifier.emit('transfer.finished', '完成', key='j1'), 1)
        self.assertTrue(wait_for(lambda: len(self.opener.requests) == 2))
        self.assertEqual({r['url'] for r in self.opener.requests}, {'http://192.0.2.7/a'})

    def test_full_queue_never_blocks_and_drops_the_oldest(self):
        gate = threading.Event()
        self.opener.block = gate
        self.store.save({'type': 'webhook', 'url': 'http://192.0.2.7/a', 'events': ['transfer.failed']})
        started = time.monotonic()
        for index in range(notify.QUEUE_SIZE + 50):
            self.notifier.emit('transfer.failed', f'失败 {index}', key=str(index))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertLessEqual(len(self.notifier.queue), notify.QUEUE_SIZE)
        self.assertGreater(self.notifier.dropped, 0)
        gate.set()

    def test_retries_network_errors_but_not_a_rejected_token_and_logs_no_secret(self):
        channel = {'type': 'telegram', 'chat_id': '1', 'token': TOKEN, 'name': 'TG', 'use_proxy': True}
        message = {'event': 'x', 'title': 't', 'body': 'b', 'time': 't'}
        self.opener.failures = [urllib.error.URLError(OSError('down')), urllib.error.URLError(OSError('down'))]
        self.notifier.deliver(channel, message)
        self.assertEqual(len(self.opener.requests), 3)
        self.opener.requests.clear()
        self.opener.failures = [urllib.error.HTTPError('u', 401, 'Unauthorized', {}, None)] * 3
        with self.assertRaises(urllib.error.HTTPError):
            self.notifier.deliver(channel, message)
        self.assertEqual(len(self.opener.requests), 1, '401 is not retried')
        saved = self.store.save({'type': 'telegram', 'chat_id': '1', 'token': TOKEN, 'events': ['gateway.error']})
        self.opener.failures = [urllib.error.URLError(OSError('down'))] * 3
        with self.assertLogs(level='WARNING') as logs:
            self.notifier.emit('gateway.error', '网关出错')
            self.assertTrue(wait_for(lambda: any('failed' in line for line in logs.output)))
        self.assertNotIn(TOKEN, '\n'.join(logs.output))
        self.opener.failures = [urllib.error.HTTPError('u', 404, 'nf', {}, None)]
        with self.assertRaises(NotifyError) as caught:
            self.notifier.test(saved['id'])
        self.assertEqual(str(caught.exception), '发送失败：HTTP 404')

    def test_job_listener_messages(self):
        self.store.save({'type': 'webhook', 'url': 'http://192.0.2.7/a', 'events': list(notify.EVENTS)})
        base = {'kind': 'nas', 'name': 'Movies', 'files_done': 3, 'bytes_done': 3 << 30,
                'bytes_total': 3 << 30, 'elapsed': 120, 'files_failed': 0}
        self.notifier.job_finished({**base, 'id': 'a', 'state': 'done'})
        self.notifier.job_finished({**base, 'id': 'b', 'state': 'failed', 'error': 'NAS 断开'})
        self.notifier.job_finished({**base, 'id': 'c', 'state': 'canceled'})
        self.notifier.job_finished({**base, 'id': 'd', 'kind': 'extract', 'state': 'done_with_warnings'})
        self.assertTrue(wait_for(lambda: len(self.opener.requests) == 3))
        bodies = sorted((r['body']['event'], r['body']['title'], r['body']['body']) for r in self.opener.requests)
        self.assertEqual(bodies[0][:2], ('extract.finished', '解压安装完成（有提醒）：Movies'))
        self.assertEqual(bodies[1], ('transfer.failed', 'NAS 传输失败：Movies', 'NAS 断开'))
        self.assertEqual(bodies[2][1], 'NAS 传输完成：Movies')
        self.assertIn('3 个文件，3.0 GB，用时 2 分 0 秒，平均 25.6 MB/s', bodies[2][2])

    def test_package_jobs_have_their_own_events(self):
        self.store.save({'type': 'webhook', 'url': 'http://192.0.2.7/a', 'events': ['package.finished', 'package.failed']})
        base = {'kind': 'package', 'name': 'CUSA12345.pkg', 'files_done': 1, 'bytes_done': 1 << 30,
                'bytes_total': 1 << 30, 'elapsed': 60}
        self.notifier.job_finished({**base, 'id': 'p1', 'state': 'done_with_warnings', 'warning': '游戏可能无法启动'})
        self.notifier.job_finished({**base, 'id': 'p2', 'state': 'failed', 'error': '引擎正忙'})
        self.notifier.job_finished({**base, 'id': 'p3', 'kind': 'nas', 'state': 'done'})
        self.notifier.job_finished({**base, 'id': 'p4', 'kind': 'backup', 'state': 'failed', 'error': 'x'})
        self.assertTrue(wait_for(lambda: len(self.opener.requests) == 2))
        bodies = sorted((r['body']['event'], r['body']['title'], r['body']['body']) for r in self.opener.requests)
        self.assertEqual(bodies[0], ('package.failed', 'PKG 安装失败：CUSA12345.pkg', '引擎正忙'))
        self.assertEqual(bodies[1][1], 'PKG 安装完成（有提醒）：CUSA12345.pkg')
        self.assertIn('可能无法启动', bodies[1][2])

    def test_backup_jobs_announce_backup_events(self):
        self.store.save({'type': 'webhook', 'url': 'http://192.0.2.7/a', 'events': ['backup.failed']})
        self.notifier.job_finished({'kind': 'backup', 'id': 'b1', 'name': 'savedata', 'state': 'failed',
                                    'error': '2 个文件没有备份成功'})
        self.assertTrue(wait_for(lambda: len(self.opener.requests) == 1))
        body = self.opener.requests[0]['body']
        self.assertEqual((body['event'], body['title']), ('backup.failed', 'PS5 备份失败：savedata'))

    def test_update_result_is_announced_once_and_old_results_stay_quiet(self):
        self.store.save({'type': 'webhook', 'url': 'http://192.0.2.7/a', 'events': ['update.finished', 'update.failed']})
        job = {'id': 'u1', 'state': 'done', 'version': '1.11.0', 'updated_at': self.now[0] - 30}
        self.assertTrue(self.notifier.check_update(job))
        self.assertFalse(self.notifier.check_update(job))
        restarted = Notifier(self.store, opener=self.opener, clock=lambda: self.now[0],
                             state_path=self.root / 'notify-state.json')
        self.assertFalse(restarted.check_update(job), 'remembered across restarts')
        self.assertFalse(self.notifier.check_update({'id': 'u0', 'state': 'failed', 'updated_at': self.now[0] - 86400}))
        self.assertFalse(self.notifier.check_update({'id': 'u2', 'state': 'installing', 'updated_at': self.now[0]}))
        self.assertTrue(wait_for(lambda: len(self.opener.requests) == 1))
        self.assertEqual(self.opener.requests[0]['body']['title'], '服务已更新到 v1.11.0')

    def test_gateway_watcher_fires_on_the_transition_only(self):
        self.store.save({'type': 'webhook', 'url': 'http://192.0.2.7/a', 'events': ['gateway.error']})
        errors = iter(['', 'nft failed', 'nft failed', '', 'nft failed'])
        values = []

        def gateway():
            value = next(errors, '')
            values.append(value)
            return value
        stop = self.notifier.watch(gateway_error=gateway, interval=0.01)
        self.assertTrue(wait_for(lambda: len(values) >= 6))
        stop.set()
        self.assertTrue(wait_for(lambda: len(self.opener.requests) >= 1))
        time.sleep(0.05)
        self.assertEqual(len(self.opener.requests), 1, 'the second error within a minute is deduplicated')


class TargetTests(unittest.TestCase):
    def test_private_targets_are_detected(self):
        self.assertTrue(notify.is_private('192.168.1.5'))
        self.assertTrue(notify.is_private('127.0.0.1'))
        self.assertFalse(notify.is_private('8.8.8.8'))
        self.assertTrue(notify.is_private('nas.lan', resolver=lambda h, p: [(0, 0, 0, '', ('192.168.1.9', 0))]))
        self.assertFalse(notify.is_private('api.telegram.org', resolver=lambda h, p: [(0, 0, 0, '', ('149.154.167.220', 0))]))
        self.assertFalse(notify.is_private('nowhere', resolver=lambda h, p: (_ for _ in ()).throw(OSError())))

    def test_redirects_may_not_change_the_scheme(self):
        handler = notify._SameScheme()
        request = urllib.request.Request('https://hooks.example/a', data=b'{}')
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(request, None, 302, 'Found', {}, 'http://hooks.example/b')


class NotifyHTTPTests(unittest.TestCase):
    def test_routes_never_return_secrets(self):
        import http.client
        from server import AdminHandler, AdminServer, AuthManager
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = NotifyStore(Path(tmp.name) / 'notify.json', SecretBox(Path(tmp.name) / 'nas.key'))
        opener = FakeOpener()
        console = SimpleNamespace(notify_store=store, notifier=Notifier(store, opener=opener))
        server = AdminServer(('127.0.0.1', 0), AdminHandler)
        server.auth = AuthManager(AuthManager.make_credentials('a-long-test-password'))
        server.app = SimpleNamespace(settings=SimpleNamespace(subnet='127.0.0.1/32', host_ip='127.0.0.1'), console=console)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection(*server.server_address, timeout=10)
        self.addCleanup(conn.close)
        conn.request('POST', '/api/login', json.dumps({'password': 'a-long-test-password'}), {'Content-Type': 'application/json'})
        reply = conn.getresponse()
        cookie, csrf = reply.getheader('Set-Cookie').split(';')[0], json.loads(reply.read())['csrf']

        def call(method, path, body=None):
            headers = {'Cookie': cookie, 'Content-Type': 'application/json', 'X-CSRF-Token': csrf}
            conn.request(method, path, json.dumps(body) if body is not None else None, headers)
            reply = conn.getresponse()
            return reply.status, reply.read().decode()
        status, text = call('POST', '/api/notify/save', {'channel': {'type': 'telegram', 'chat_id': '42', 'token': TOKEN}})
        self.assertEqual(status, 200, text)
        ident = json.loads(text)['channel']['id']
        status, text = call('GET', '/api/notify')
        self.assertEqual(status, 200)
        self.assertNotIn(TOKEN, text)
        self.assertIn('transfer.finished', json.loads(text)['events'])
        self.assertEqual(call('POST', '/api/notify/test', {'id': ident})[0], 200)
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(call('POST', '/api/notify/save', {'channel': {'type': 'sms'}})[0], 400)
        self.assertEqual(call('POST', '/api/notify/delete', {'id': ident})[0], 200)
        self.assertEqual(store.list_public(), [])


if __name__ == '__main__':
    unittest.main()
