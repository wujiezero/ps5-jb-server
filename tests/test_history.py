import http.client
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import history as history_module
from activity import Classifier, FlowObserver, LabelsStore
from history import History

LAN = '192.0.2.0/24'
HOST_IP = '192.0.2.20'
PS5 = '192.0.2.142'
OTHER = '192.0.2.143'
NOW = 1800000000.0


def event(kind='flow', verdict='blocked', client=PS5, dst='', domain='', port=443, epoch=NOW, local=False):
    return {'epoch': epoch, 'kind': kind, 'client': client, 'dst': dst, 'port': port,
            'proto': 'dns' if kind == 'dns' else 'tcp', 'domain': domain, 'verdict': verdict, 'local': local}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'activity.db'
        self.now = [NOW]
        self.history = History(self.path, retention_days=30, clock=lambda: self.now[0])

    def tearDown(self):
        self.history.close()
        self.tmp.cleanup()

    def test_cursor_pages_newest_first(self):
        self.history.add([event(dst=f'203.0.113.{i}', epoch=NOW + i) for i in range(5)])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        page = self.history.query(limit=2)
        self.assertEqual([e['dst'] for e in page['events']], ['203.0.113.4', '203.0.113.3'])
        page = self.history.query(limit=2, before_id=page['next_before'])
        self.assertEqual([e['dst'] for e in page['events']], ['203.0.113.2', '203.0.113.1'])
        page = self.history.query(limit=2, before_id=page['next_before'])
        self.assertEqual([e['dst'] for e in page['events']], ['203.0.113.0'])
        self.assertIsNone(page['next_before'])
        self.assertEqual(self.history.query(limit=10_000)['events'][0]['dst'], '203.0.113.4', 'limit is capped')

    def test_filters(self):
        self.history.add([
            event(dst='203.0.113.1', verdict='allowed', epoch=NOW - 100),
            event(kind='dns', domain='ads_tracker.example', epoch=NOW - 50),
            event(kind='dns', domain='ads.example', client=OTHER, epoch=NOW - 40),
            event(kind='service', dst=HOST_IP, port=53, verdict='allowed', epoch=NOW - 10)])

        def domains(**kwargs):
            return [e['domain'] or e['dst'] for e in self.history.query(**kwargs)['events']]
        self.assertEqual(domains(verdict='allowed'), [HOST_IP, '203.0.113.1'])
        self.assertEqual(domains(kind='dns'), ['ads.example', 'ads_tracker.example'])
        self.assertNotIn(HOST_IP, domains(kind='external'))
        self.assertEqual(domains(client=OTHER), ['ads.example'])
        self.assertEqual(domains(text='ads_'), ['ads_tracker.example'], '_ is matched literally')
        self.assertEqual(domains(text='%'), [], '% is matched literally')
        self.assertEqual(domains(start=NOW - 60, end=NOW - 20), ['ads.example', 'ads_tracker.example'])

    def test_stats_group_by_day_target_and_client(self):
        day = 86400
        self.history.add([event(kind='dns', domain='ads.example', epoch=NOW - day) for _ in range(3)]
                         + [event(dst='203.0.113.9', epoch=NOW)]
                         + [event(kind='dns', domain='ok.example', verdict='allowed', client=OTHER, epoch=NOW)]
                         + [event(kind='service', dst=HOST_IP, port=53, verdict='allowed', epoch=NOW)]
                         + [event(kind='dns', domain='old.example', epoch=NOW - 10 * day)])
        stats = self.history.stats(7)
        self.assertEqual(stats['days'], 7)
        self.assertEqual(sum(d['blocked'] for d in stats['per_day']), 4)
        self.assertEqual(sum(d['allowed'] for d in stats['per_day']), 2)
        self.assertEqual(stats['top_blocked'][0], {'target': 'domain:ads.example', 'hits': 3,
                                                   'last': NOW - day, 'clients': 1})
        self.assertIn('ip:203.0.113.9', [t['target'] for t in stats['top_blocked']])
        self.assertEqual([t['target'] for t in stats['top_allowed']], ['domain:ok.example'], 'local services excluded')
        self.assertEqual({c['client'] for c in stats['clients']}, {PS5, OTHER})
        self.assertEqual(self.history.stats(30)['days'], 30)
        self.assertIn('domain:old.example', [t['target'] for t in self.history.stats(30)['top_blocked']])
        self.assertEqual(stats['rows'], 7)

    def test_retention_and_row_cap(self):
        self.history.max_rows = 3
        self.history.pruned = NOW          # no automatic prune during add()
        self.history.add([event(epoch=NOW - 40 * 86400, dst='203.0.113.1')]
                         + [event(epoch=NOW - 5 + i, dst=f'203.0.113.{10 + i}') for i in range(5)])
        removed = self.history.prune()
        self.assertEqual(removed, 3, 'one past retention, then the two oldest over the cap')
        self.assertEqual([e['dst'] for e in self.history.query()['events']],
                         ['203.0.113.14', '203.0.113.13', '203.0.113.12'])

    def test_prunes_at_most_hourly_on_write(self):
        calls = []
        self.history.prune = lambda: calls.append(1)
        self.history.pruned = NOW
        self.history.add([event()])
        self.now[0] += history_module.PRUNE_EVERY
        self.history.add([event()])
        self.assertEqual(len(calls), 1)

    def test_corrupt_database_is_moved_aside_and_recreated(self):
        self.history.close()
        self.path.write_bytes(b'this is not a database' * 100)
        recovered = History(self.path, clock=lambda: NOW)
        self.assertIn('损坏', recovered.error)
        self.assertEqual(len(list(Path(self.tmp.name).glob('activity.db.corrupt-*'))), 1)
        self.assertEqual(recovered.add([event()]), 1)
        recovered.close()

    def test_write_failure_is_swallowed(self):
        with self.history.lock:
            self.history.conn.close()
            self.history.conn = sqlite3.connect(':memory:', check_same_thread=False)
        with self.assertLogs(level='WARNING'):
            self.assertEqual(self.history.add([event()]), 0)
        self.assertEqual(list(Path(self.tmp.name).glob('activity.db.corrupt-*')), [], 'not mistaken for corruption')

    def test_csv_export_escapes_formulas(self):
        self.history.add([event(kind='dns', domain='=cmd.example', epoch=NOW), event(dst='203.0.113.5', epoch=NOW + 1)])
        text, cut = self.history.export_csv()
        self.assertFalse(cut)
        lines = text.strip().splitlines()
        self.assertTrue(lines[0].startswith('时间,来源'))
        self.assertIn("'=cmd.example", lines[1])
        self.assertIn('203.0.113.5', lines[2])


class ObserverHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / 'rules.json').write_text(json.dumps({'compiled': {'PlayStation 服务': ['playstation.net']}, 'exact': {}}))
        self.labels = LabelsStore(root / 'labels.json')
        self.history = History(root / 'activity.db', clock=lambda: NOW)
        self.rows = []
        self.conntrack = ''
        self.observer = FlowObserver(
            conntrack=lambda: self.conntrack, classifier=Classifier(root / 'rules.json'), labels=self.labels,
            dns_lookup=lambda ip: (None, None), is_allowed=lambda ip: ip == '203.0.113.7',
            lan=LAN, host_ip=HOST_IP, clock=lambda: NOW, dns_events=lambda: self.rows,
            domain_allowed=lambda name: name == 'store.playstation.net')
        self.observer.history = self.history

    def tearDown(self):
        self.history.close()
        self.tmp.cleanup()

    def test_poll_writes_flows_and_dns_and_warm_restores_the_ring(self):
        self.conntrack = (f'tcp      6 431999 ESTABLISHED src={PS5} dst=203.0.113.7 sport=50000 dport=443 '
                          f'src=203.0.113.7 dst={PS5} sport=443 dport=50000 [ASSURED] mark=0 use=1\n')
        self.rows = [{'seq': 2, 'client': PS5, 'name': 'ads.example', 'qtype': 'A', 'action': 'blocked'},
                     {'seq': 1, 'client': PS5, 'name': 'store.playstation.net', 'qtype': 'A', 'action': 'allowed'}]
        self.observer.poll_once()
        stored = self.history.query()['events']
        self.assertEqual(sorted(e['kind'] for e in stored), ['dns', 'dns', 'flow'])
        self.labels.set('domain:ads.example', 'Ads', '广告')
        restarted = FlowObserver(conntrack=lambda: '', classifier=self.observer.classifier, labels=self.labels,
                                 dns_lookup=lambda ip: (None, None), is_allowed=lambda ip: False,
                                 lan=LAN, host_ip=HOST_IP, clock=lambda: NOW)
        self.assertEqual(restarted.warm(self.history), 3)
        live = restarted.snapshot()['events']
        self.assertEqual([e['kind'] for e in live], [e['kind'] for e in stored], 'newest first, same order')
        ads = next(e for e in live if e['domain'] == 'ads.example')
        self.assertEqual((ads['name'], ads['category'], ads['verdict']), ('Ads', '广告', 'blocked'))
        store = self.observer.present(next(e for e in stored if e['domain'] == 'store.playstation.net'))
        self.assertEqual(store['category'], 'PlayStation 服务')
        self.assertTrue(store['allowed_now'])
        self.assertIn('id', store)

    def test_history_failure_never_breaks_polling(self):
        self.observer.history = SimpleNamespace(add=lambda events: (_ for _ in ()).throw(RuntimeError('disk')))
        self.rows = [{'seq': 1, 'client': PS5, 'name': 'ads.example', 'qtype': 'A', 'action': 'blocked'}]
        with self.assertLogs(level='ERROR'):
            self.observer.poll_once()
        self.assertEqual(len(self.observer.snapshot()['events']), 1)


class HistoryHTTPTests(unittest.TestCase):
    def setUp(self):
        from server import AdminHandler, AdminServer, AuthManager
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / 'rules.json').write_text(json.dumps({'compiled': {}, 'exact': {}}))
        self.history = History(root / 'activity.db', clock=lambda: time.time())
        self.history.add([event(kind='dns', domain='ads.example', epoch=time.time() - 5),
                          event(dst='203.0.113.9', epoch=time.time())])
        observer = FlowObserver(conntrack=lambda: '', classifier=Classifier(root / 'rules.json'),
                                labels=LabelsStore(root / 'labels.json'), dns_lookup=lambda ip: (None, None),
                                is_allowed=lambda ip: False, lan=LAN, host_ip=HOST_IP,
                                domain_allowed=lambda name: False)
        self.server = AdminServer(('127.0.0.1', 0), AdminHandler)
        self.server.auth = AuthManager(AuthManager.make_credentials('a-long-test-password'))
        self.server.app = SimpleNamespace(settings=SimpleNamespace(subnet='127.0.0.1/32', host_ip='127.0.0.1'),
                                          history=self.history, activity=observer)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.conn = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        self.conn.request('POST', '/api/login', json.dumps({'password': 'a-long-test-password'}),
                          {'Content-Type': 'application/json'})
        reply = self.conn.getresponse()
        self.cookie = reply.getheader('Set-Cookie').split(';')[0]
        self.csrf = json.loads(reply.read())['csrf']

    def tearDown(self):
        self.conn.close()
        self.server.shutdown()
        self.server.server_close()
        self.history.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        self.conn.request(method, path, body, {'Cookie': self.cookie, **(headers or {})})
        reply = self.conn.getresponse()
        return reply, reply.read()

    def test_history_stats_export_and_clear(self):
        reply, data = self.call('GET', '/api/activity/history?limit=1&kind=dns')
        self.assertEqual(reply.status, 200)
        page = json.loads(data)
        self.assertEqual(page['events'][0]['domain'], 'ads.example')
        self.assertEqual(page['events'][0]['category'], '未识别')
        self.assertEqual(self.call('GET', '/api/activity/history?before_id=x')[0].status, 400)
        reply, data = self.call('GET', '/api/activity/stats?days=30')
        stats = json.loads(data)
        self.assertEqual(stats['days'], 30)
        self.assertEqual({t['target'] for t in stats['top_blocked']}, {'domain:ads.example', 'ip:203.0.113.9'})
        self.assertIn('allowed_now', stats['top_blocked'][0])
        reply, data = self.call('GET', '/api/activity/export')
        self.assertEqual(reply.status, 200)
        self.assertTrue(data.startswith('﻿'.encode('utf-8')))
        self.assertIn('attachment; filename="ps5-activity-', reply.getheader('Content-Disposition'))
        self.assertEqual(self.call('GET', '/api/activity')[0].status, 200, 'the live route still answers')
        headers = {'Content-Type': 'application/json'}
        self.assertEqual(self.call('POST', '/api/activity/history/clear', '{}', headers)[0].status, 403)
        reply, data = self.call('POST', '/api/activity/history/clear', '{}', {**headers, 'X-CSRF-Token': self.csrf})
        self.assertEqual(json.loads(data)['removed'], 2)
        self.assertEqual(self.history.query()['events'], [])


if __name__ == '__main__':
    unittest.main()
