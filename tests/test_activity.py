import json
import pathlib
import sys
import tempfile
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server'))
try:
    from activity import Classifier, FlowObserver, LabelsStore, parse_conntrack
except ImportError:
    Classifier = FlowObserver = LabelsStore = parse_conntrack = None

# Fixture-only LAN (TEST-NET-1, RFC 5737) instead of the legacy deployment
# LAN: the observer is fully injected, this test never touches the network.
LAN = '192.0.2.0/24'
HOST_IP = '192.0.2.20'   # the gateway itself, must be filtered out
CLIENT = '192.0.2.142'   # a PS5-like client inside the fixture LAN
EXTERNAL = '93.184.215.14'

CONNTRACK = f"""\
tcp      6 89 TIME_WAIT src={CLIENT} dst={EXTERNAL} sport=51234 dport=443 src={EXTERNAL} dst={CLIENT} sport=443 dport=51234 [ASSURED] mark=0 use=1
tcp      6 4 SYN_SENT src={CLIENT} dst=203.0.113.9 sport=51235 dport=80 [UNREPLIED] src=203.0.113.9 dst={CLIENT} sport=80 dport=51235 mark=0 use=1
tcp      6 100 TIME_WAIT src={CLIENT} dst={HOST_IP} sport=40000 dport=8443 src={HOST_IP} dst={CLIENT} sport=8443 dport=40000 [ASSURED] use=1
"""


class ClassifierTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(Classifier, 'classifier required')
        self.tmp = tempfile.TemporaryDirectory()
        path = pathlib.Path(self.tmp.name) / 'rules.json'
        path.write_text(json.dumps({
            'compiled': {
                'PlayStation 服务': ['playstation.net', 'playstation.com'],
                '广告与追踪': ['doubleclick.net'],
                'CDN（启发式）': ['akamai.net'],
            }, 'exact': {}}))
        self.c = Classifier(path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_suffix_match_with_boundary(self):
        self.assertEqual(self.c.category('ps5.playstation.net'), 'PlayStation 服务')
        self.assertEqual(self.c.category('playstation.net'), 'PlayStation 服务')
        self.assertEqual(self.c.category('evilplaystation.net'), '未识别')
        self.assertEqual(self.c.category('ad.doubleclick.net'), '广告与追踪')
        self.assertEqual(self.c.category('x.akamai.net'), 'CDN（启发式）')

    def test_categories_listed(self):
        self.assertIn('PlayStation 服务', self.c.categories())
        self.assertIn('未识别', self.c.categories())


class ConntrackTests(unittest.TestCase):
    def test_parses_flows(self):
        self.assertIsNotNone(parse_conntrack, 'parser required')
        flows = parse_conntrack(CONNTRACK)
        self.assertEqual(len(flows), 3)
        first = flows[0]
        self.assertEqual((first['proto'], first['src'], first['dst'], first['dport']),
                         ('tcp', CLIENT, EXTERNAL, 443))
        self.assertTrue(first['replied'])
        self.assertFalse(flows[1]['replied'])

    def test_tolerates_garbage(self):
        self.assertEqual(parse_conntrack('garbage line\n\n'), [])


class LabelTests(unittest.TestCase):
    def test_set_get_and_persist(self):
        self.assertIsNotNone(LabelsStore, 'labels store required')
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'labels.json'
            store = LabelsStore(path)
            store.set('ip:93.184.215.14', name='示例站点', category='测试')
            again = LabelsStore(path)
            self.assertEqual(again.get('ip:93.184.215.14')['name'], '示例站点')
            store.set('ip:93.184.215.14', name='', category='')
            self.assertEqual(store.get('ip:93.184.215.14'), {})

    def test_rejects_bad_target(self):
        self.assertIsNotNone(LabelsStore)
        with tempfile.TemporaryDirectory() as tmp:
            store = LabelsStore(pathlib.Path(tmp) / 'labels.json')
            for bad in ('evil', 'ip:not-an-ip', 'ip:1.2.3.4/32', 'domain:http://x', 'domain:'):
                with self.assertRaises(ValueError):
                    store.set(bad, name='x')


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(FlowObserver, 'observer required')
        self.now = 1000.0
        self.labels = tempfile.TemporaryDirectory()
        self.store = LabelsStore(pathlib.Path(self.labels.name) / 'labels.json')
        self.classifier = None
        path = pathlib.Path(self.labels.name) / 'rules.json'
        path.write_text(json.dumps({'compiled': {'PlayStation 服务': ['playstation.net']}, 'exact': {}}))
        self.classifier = Classifier(path)
        self.output = CONNTRACK
        self.observer = FlowObserver(
            conntrack=lambda: self.output,
            classifier=self.classifier,
            labels=self.store,
            dns_lookup=lambda ip: ('ps5.playstation.net', 12) if ip == '93.184.215.14' else (None, None),
            is_allowed=lambda ip: ip == EXTERNAL,
            lan=LAN, host_ip=HOST_IP, clock=lambda: self.now)

    def tearDown(self):
        self.labels.cleanup()

    def test_ignores_lan_and_self_traffic(self):
        events = self.observer.poll_once()
        self.assertEqual([e['dst'] for e in events], ['93.184.215.14', '203.0.113.9'])

    def test_event_fields_and_verdict(self):
        events = self.observer.poll_once()
        allowed, denied = events[0], events[1]
        self.assertEqual(allowed['domain'], 'ps5.playstation.net')
        self.assertEqual(allowed['category'], 'PlayStation 服务')
        self.assertEqual(allowed['verdict'], 'allowed')
        self.assertIn('allowed_now', allowed)
        self.assertEqual(denied['verdict'], 'blocked')
        self.assertEqual(denied['category'], '直连 IP')
        self.assertEqual(allowed['client'], CLIENT)

    def test_duplicate_flow_not_repeated(self):
        self.observer.poll_once()
        self.assertEqual(self.observer.poll_once(), [])
        self.now += 120
        self.output = CONNTRACK.replace('timeout', 'timeout')
        self.assertEqual(len(self.observer.poll_once()), 0)

    def test_labels_split_name_and_override_category(self):
        self.store.set('ip:93.184.215.14', name='我的站点', category='自定义')
        event = self.observer.poll_once()[0]
        self.assertEqual((event['name'], event['category']), ('我的站点', '自定义'))

    def test_labels_apply_to_existing_events_on_read(self):
        self.observer.poll_once()
        self.store.set('ip:93.184.215.14', name='读时命名', category='读时分类')
        event = [e for e in self.observer.snapshot()['events'] if e['dst'] == '93.184.215.14'][0]
        self.assertEqual((event['name'], event['category']), ('读时命名', '读时分类'))
        self.store.set('ip:93.184.215.14', name='', category='')
        event = [e for e in self.observer.snapshot()['events'] if e['dst'] == '93.184.215.14'][0]
        self.assertEqual((event['name'], event['category']), ('', 'PlayStation 服务'))

    def test_snapshot_counts_and_allow_targets(self):
        self.observer.poll_once()
        snapshot = self.observer.snapshot()
        self.assertEqual(snapshot['counts']['allowed'], 1)
        self.assertEqual(snapshot['counts']['blocked'], 1)
        targets = self.observer.allow_targets(['ip:93.184.215.14', 'domain:ps5.playstation.net'])
        self.assertEqual(targets, {'networks': ['93.184.215.14/32'], 'domains': ['ps5.playstation.net']})

    def test_allowed_now_follows_current_policy(self):
        allowed = set()
        self.observer.is_allowed = lambda ip: ip in allowed
        self.observer.poll_once()
        before = [e for e in self.observer.snapshot()['events'] if e['dst'] == '93.184.215.14'][0]
        self.assertEqual((before['verdict'], before['allowed_now']), ('blocked', False))
        allowed.add('93.184.215.14')
        after = [e for e in self.observer.snapshot()['events'] if e['dst'] == '93.184.215.14'][0]
        self.assertEqual((after['verdict'], after['allowed_now']), ('blocked', True))

    def test_event_ring_is_bounded(self):
        self.observer.max_events = 3
        for index in range(6):
            self.output = CONNTRACK.replace('93.184.215.14', f'93.184.215.{index + 1}')
            self.observer.poll_once()
            self.now += 1
        self.assertLessEqual(len(self.observer.snapshot()['events']), 3)


if __name__ == '__main__':
    unittest.main()
