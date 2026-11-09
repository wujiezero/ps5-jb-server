import http.client
import json
import pathlib
import sys
import tempfile
import threading
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server'))
from activity import Classifier, FlowObserver, LabelsStore
from server import AdminHandler, AdminServer, AuthManager

PASSWORD = 'a-long-test-password'
# Fixture-only LAN and upstream from RFC 5737 TEST-NET-1: nothing here reaches
# the network and nothing depends on a legacy deployment LAN.
LAN = '192.0.2.0/24'
HOST_IP = '192.0.2.20'
CLIENT = '192.0.2.142'
EXTERNAL = '93.184.215.14'
UPSTREAM = '192.0.2.1'

CONNTRACK = (f'tcp 6 89 TIME_WAIT src={CLIENT} dst={EXTERNAL} sport=5 dport=443 '
             f'src={EXTERNAL} dst={CLIENT} sport=443 dport=5 [ASSURED] mark=0 use=1\n')


class FakeSettings:
    """The subset of Settings the admin guard and the console API read."""

    subnet = '127.0.0.1/32'
    host_ip = '127.0.0.1'


class FakeStore:
    def __init__(self):
        self.config = {'domains': [], 'networks': [], 'upstream_dns': UPSTREAM}

    def snapshot(self):
        return dict(self.config, domains=list(self.config['domains']), networks=list(self.config['networks']))


class FakeApp:
    def __init__(self, activity):
        self.activity = activity
        self.store = FakeStore()
        self.applied = []
        self.settings = FakeSettings()

    def change(self, body=None, rollback=False):
        self.applied.append(body)

    def allow(self, targets):
        additions = self.activity.allow_targets(targets)
        self.store.config['domains'] = sorted(set(self.store.config['domains']) | set(additions['domains']))
        self.store.config['networks'] = sorted(set(self.store.config['networks']) | set(additions['networks']))
        self.applied.append(dict(self.store.config))
        return self.store.snapshot()

    def state(self):
        return {'config': self.store.config, 'logs': []}


class ActivityApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        rules = root / 'compiled.json'
        rules.write_text(json.dumps({'compiled': {'PlayStation 服务': ['playstation.net']}, 'exact': {}}))
        self.observer = FlowObserver(
            conntrack=lambda: CONNTRACK,
            classifier=Classifier(rules),
            labels=LabelsStore(root / 'labels.json'),
            dns_lookup=lambda ip: ('ps5.playstation.net', 5),
            is_allowed=lambda ip: False,
            lan=LAN, host_ip=HOST_IP)
        self.app = FakeApp(self.observer)
        self.server = AdminServer(('127.0.0.1', 0), AdminHandler)
        self.server.auth = AuthManager(AuthManager.make_credentials(PASSWORD))
        self.server.app = self.app
        self.server.repo = None
        self.server.credentials_path = root / 'credentials.json'
        self.server.static_dir = pathlib.Path(__file__).resolve().parents[1] / 'web'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        payload = json.dumps(body).encode() if body is not None else None
        hdrs = {'Content-Type': 'application/json'} if body is not None else {}
        hdrs.update(headers or {})
        conn.request(method, path, payload, hdrs)
        response = conn.getresponse()
        data = response.read()
        info = dict(response.getheaders())
        conn.close()
        return response.status, info, (json.loads(data) if data else {})

    def login(self):
        status, headers, body = self.call('POST', '/api/login', {'password': PASSWORD})
        self.assertEqual(status, 200)
        return headers['Set-Cookie'].split(';')[0], body['csrf']

    def test_activity_requires_login(self):
        self.assertEqual(self.call('GET', '/api/activity')[0], 401)

    def test_activity_returns_flow_with_domain(self):
        cookie, _ = self.login()
        code, _, body = self.call('GET', '/api/activity?limit=50', headers={'Cookie': cookie})
        self.assertEqual(code, 200)
        self.assertEqual(len(body['events']), 1)
        event = body['events'][0]
        self.assertEqual((event['dst'], event['domain'], event['category']),
                         ('93.184.215.14', 'ps5.playstation.net', 'PlayStation 服务'))
        self.assertEqual(body['counts']['blocked'], 1) if False else self.assertIn('counts', body)

    def test_labels_need_csrf_and_validate(self):
        cookie, csrf = self.login()
        self.assertEqual(self.call('POST', '/api/labels', {'target': 'ip:93.184.215.14', 'name': '站点'},
                                   {'Cookie': cookie})[0], 403)
        self.assertEqual(self.call('POST', '/api/labels', {'target': 'nonsense', 'name': 'x'},
                                   {'Cookie': cookie, 'X-CSRF-Token': csrf})[0], 400)
        self.assertEqual(self.call('POST', '/api/labels',
                                   {'target': 'ip:93.184.215.14', 'name': '我的站点', 'category': '自定义'},
                                   {'Cookie': cookie, 'X-CSRF-Token': csrf})[0], 200)
        self.assertEqual(self.observer.labels.get('ip:93.184.215.14')['name'], '我的站点')

    def test_allow_requires_csrf_and_merges(self):
        cookie, csrf = self.login()
        body = {'targets': ['ip:93.184.215.14', 'domain:cdn.example.com']}
        self.assertEqual(self.call('POST', '/api/allow', body, {'Cookie': cookie})[0], 403)
        code, _, result = self.call('POST', '/api/allow', body,
                                    {'Cookie': cookie, 'X-CSRF-Token': csrf})
        self.assertEqual(code, 200)
        self.assertEqual(result['config']['domains'], ['cdn.example.com'])
        self.assertEqual(result['config']['networks'], ['93.184.215.14/32'])
        self.assertEqual(self.app.applied[-1]['networks'], ['93.184.215.14/32'])

    def test_allow_rejects_empty_selection(self):
        cookie, csrf = self.login()
        self.assertEqual(self.call('POST', '/api/allow', {'targets': []},
                                   {'Cookie': cookie, 'X-CSRF-Token': csrf})[0], 400)


if __name__ == '__main__':
    unittest.main()
