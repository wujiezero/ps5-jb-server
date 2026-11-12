import datetime
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import netproxy
import selfcheck
from selfcheck import Check, Context, SelfCheck

HOST = '192.0.2.10'
PASSWORD = 'a-long-test-password'


def make_cert(days, names=('manuals.playstation.net',), pem=False):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'test')])
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
               .public_key(key.public_key()).serial_number(1)
               .not_valid_before(now - datetime.timedelta(days=30))
               .not_valid_after(now + datetime.timedelta(days=days, hours=1)))
    if names:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), False)
    cert = builder.sign(key, hashes.SHA256())
    return cert.public_bytes(serialization.Encoding.PEM if pem else serialization.Encoding.DER)


class FakeProbes:
    def __init__(self, **overrides):
        self.answers = {name: [HOST] for name in selfcheck.LOCAL_DOMAINS}
        self.answers['example.com'] = ['93.184.215.14']
        self.cert = make_cert(400)
        self.admin_cert = make_cert(400, names=(), pem=True)
        self.repo = json.dumps({'payloads': [{'filename': 'a.elf'}]}).encode()
        self.files = {'/proc/sys/net/ipv4/ip_forward': '1\n', '/proc/sys/net/ipv6/conf/all/forwarding': '0\n'}
        self.commands = {('nft', 'list', 'table', 'inet', 'ps5guard'): (0, 'table inet ps5guard {}'),
                         ('ip', '-4', '-o', 'addr', 'show', 'dev', 'eth0'): (0, f'2: eth0    inet {HOST}/24 brd x'),
                         ('ip', '-6', 'route', 'show', 'default'): (0, ''),
                         ('conntrack', '-C'): (0, '42\n')}
        self.flow_list = []
        self.free = 5 << 30
        self.can_write = True
        self.found = {'libarchive': True, 'unrar': True, 'smb': True}
        self.engine = {'version': '1'}
        self.proxy_status = {'configured': False, 'url': '', 'error': ''}
        self.__dict__.update(overrides)

    def dns_query(self, server, port, name, timeout=2.0):
        value = self.answers.get(name)
        if isinstance(value, Exception):
            raise value
        return value or []

    def peer_cert(self, host, port, sni, timeout=2.5):
        if isinstance(self.cert, Exception):
            raise self.cert
        return self.cert

    def http_get(self, url, timeout=2.5, limit=0):
        if isinstance(self.repo, Exception):
            raise self.repo
        return self.repo

    def read_text(self, path):
        if str(path) not in self.files:
            raise OSError(path)
        return self.files[str(path)]

    def read_bytes(self, path):
        return self.admin_cert

    def run(self, args, timeout=2.5):
        return self.commands.get(tuple(args), (127, ''))

    def flows(self):
        return self.flow_list

    def disk_free(self, path):
        return self.free

    def writable(self, path):
        return self.can_write

    def modules(self):
        return dict(self.found)

    def engine_version(self, url):
        return self.engine

    def proxy(self):
        return self.proxy_status


def settings(**extra):
    base = dict(host_ip=HOST, subnet='192.0.2.0/24', dns_port=53, https_port=443, repo_port=8080,
                cert_dir=Path('/etc/ps5-jb-server/tls'), data_dir=Path('/var/lib/ps5-jb-server'),
                interface='eth0', upstream_dns='192.0.2.1', engine_url='http://127.0.0.1:19113')
    base.update(extra)
    return SimpleNamespace(**base)


def context(probes=None, **kwargs):
    engine = SimpleNamespace(gateway_error=None, logs=[], firewall=SimpleNamespace(interface='eth0'),
                             store=SimpleNamespace(snapshot=lambda: {'upstream_dns': '192.0.2.1'}))
    return Context(kwargs.pop('settings', settings()), engine=kwargs.pop('engine', engine),
                   probes=probes or FakeProbes(), **kwargs)


def single(check_id, ctx):
    [check] = [check for check in selfcheck.CHECKS if check.id == check_id]
    return check.func(ctx)


class CheckTests(unittest.TestCase):
    def test_everything_healthy_is_ok_or_skip(self):
        results = selfcheck.run(context())
        self.assertEqual([item['id'] for item in results], [check.id for check in selfcheck.CHECKS])
        states = {item['id']: item['status'] for item in results}
        self.assertEqual(states.pop('ps5'), 'skip', 'no PS5 known yet')
        self.assertEqual(set(states.values()), {'ok'}, results)
        for item in results:
            self.assertEqual(set(item), {'id', 'title', 'status', 'detail', 'fix'})

    def test_dns_hijack_reports_each_wrong_name(self):
        probes = FakeProbes()
        probes.answers['manuals.playstation.net'] = ['10.0.0.1']
        probes.answers['www.msftconnecttest.com'] = TimeoutError()
        outcome = single('dns', context(probes))
        self.assertEqual(outcome['status'], 'fail')
        self.assertIn('manuals.playstation.net（应答 10.0.0.1）', outcome['detail'])
        self.assertIn('TimeoutError', outcome['detail'])
        self.assertIn('53', outcome['fix'])

    def test_certificates_expiry_and_missing_name(self):
        self.assertEqual(single('certificates', context(FakeProbes(cert=make_cert(10))))['status'], 'warn')
        self.assertEqual(single('certificates', context(FakeProbes(cert=make_cert(-5))))['status'], 'fail')
        outcome = single('certificates', context(FakeProbes(cert=make_cert(400, names=('other.example',)))))
        self.assertEqual(outcome['status'], 'fail')
        self.assertIn('不含 manuals.playstation.net', outcome['detail'])
        outcome = single('certificates', context(FakeProbes(cert=ConnectionRefusedError())))
        self.assertEqual(outcome['status'], 'fail')
        self.assertIn('ps5-jb-host', outcome['fix'])
        outcome = single('certificates', context(FakeProbes(admin_cert=make_cert(5, pem=True))))
        self.assertEqual(outcome['status'], 'warn')
        self.assertIn('admin.crt', outcome['fix'])

    def test_repo_unreachable_and_missing_files(self):
        self.assertEqual(single('repo', context(FakeProbes(repo=OSError())))['status'], 'fail')
        repo = SimpleNamespace(_SEED_FILE='seed', summary=lambda: {'items': [
            {'filename': 'a.elf', 'missing': True}, {'filename': 'seed', 'missing': True}]})
        outcome = single('repo', context(repo=repo))
        self.assertEqual(outcome['status'], 'warn')
        self.assertIn('1 个缺文件：a.elf', outcome['detail'])

    def test_gateway_lists_every_problem(self):
        probes = FakeProbes()
        probes.files['/proc/sys/net/ipv4/ip_forward'] = '0\n'
        probes.files['/proc/sys/net/ipv6/conf/all/forwarding'] = '1\n'
        probes.commands[('nft', 'list', 'table', 'inet', 'ps5guard')] = (1, '')
        probes.commands[('ip', '-4', '-o', 'addr', 'show', 'dev', 'eth0')] = (0, '2: eth0 inet 192.0.2.99/24')
        engine = SimpleNamespace(gateway_error='nft: syntax error', logs=[], firewall=None, store=None)
        outcome = single('gateway', context(probes, engine=engine))
        self.assertEqual(outcome['status'], 'fail')
        for text in ('IPv4 转发没有开启', 'ps5guard', 'nft: syntax error', '不持有本机地址', 'IPv6 转发开着'):
            self.assertIn(text, outcome['detail'])

    def test_upstream_and_conntrack(self):
        probes = FakeProbes()
        probes.answers['example.com'] = OSError('timed out')
        self.assertEqual(single('upstream', context(probes))['status'], 'fail')
        probes.commands.pop(('conntrack', '-C'))
        outcome = single('conntrack', context(probes))
        self.assertEqual(outcome['status'], 'fail')
        self.assertIn('apt install conntrack', outcome['fix'])

    def test_ps5_direction_from_dns_log_and_conntrack(self):
        discovery = SimpleNamespace(known={'192.0.2.20': {}, '192.0.2.21': {}, '192.0.2.22': {}},
                                    cached={'hosts': [{'ip': '192.0.2.23', 'pldmgr': True}]})
        console = SimpleNamespace(ps5=SimpleNamespace(discovery=discovery))
        engine = SimpleNamespace(gateway_error=None, firewall=None, store=None,
                                 logs=[{'client': '192.0.2.20'}, {'client': '192.0.2.21'}])
        probes = FakeProbes(flow_list=[
            {'src': '192.0.2.20', 'dst': '203.0.113.5'},           # out through this host
            {'src': '192.0.2.21', 'dst': '192.0.2.1'},             # LAN only
            {'src': '192.0.2.23', 'dst': '203.0.113.9'}])
        outcome = single('ps5', context(probes, engine=engine, console=console))
        self.assertEqual(outcome['status'], 'warn')
        detail = outcome['detail']
        self.assertIn('192.0.2.20：DNS 与网关都指向本机', detail)
        self.assertIn('192.0.2.21：DNS 指向本机，但没看到', detail)
        self.assertIn('192.0.2.22：最近没有看到', detail)
        self.assertIn('192.0.2.23：有经过本机的连接', detail)
        self.assertIn(f'默认网关填 {HOST}', outcome['fix'])
        self.assertIn(f'主 DNS 填 {HOST}', outcome['fix'])

    def test_ipv6_route_is_a_cautious_warning(self):
        probes = FakeProbes()
        probes.commands[('ip', '-6', 'route', 'show', 'default')] = (0, 'default via fe80::1 dev eth0')
        outcome = single('ipv6', context(probes))
        self.assertEqual(outcome['status'], 'warn')
        self.assertIn('可能', outcome['detail'])

    def test_missing_components_are_skipped_with_install_hints_and_proxy_is_masked(self):
        status = netproxy.status({'HTTPS_PROXY': 'http://user:hunter2-secret@192.0.2.3:3128'})
        probes = FakeProbes(found={'libarchive': False, 'unrar': False, 'smb': True}, engine=None,
                            proxy_status=status)
        outcome = single('components', context(probes))
        self.assertEqual(outcome['status'], 'skip')
        self.assertIn('--with-extract', outcome['fix'])
        self.assertIn('--with-power-engine', outcome['fix'])
        self.assertNotIn('hunter2', json.dumps(outcome, ensure_ascii=False))
        self.assertIn('***@', outcome['detail'])

    def test_resources(self):
        units = lambda: {'server': {'name': 'ps5-jb-server', 'state': 'active'},  # noqa: E731
                         'repo': {'name': 'ps5-jb-repo', 'state': 'failed'}}
        outcome = single('resources', context(FakeProbes(free=100 << 20, can_write=False), units=units))
        self.assertEqual(outcome['status'], 'fail')
        self.assertIn('ps5-jb-repo=failed', outcome['detail'])
        self.assertIn('200 MB', outcome['fix'])


class RunTests(unittest.TestCase):
    def test_a_hung_or_broken_check_only_fails_itself(self):
        release = threading.Event()
        checks = (Check('slow', '慢', lambda ctx: release.wait(5) and None, timeout=0.2),
                  Check('boom', '炸', lambda ctx: 1 / 0),
                  Check('bad', '格式', lambda ctx: {'status': 'great'}),
                  Check('fine', '正常', lambda ctx: selfcheck.result('ok', 'x')))
        started = time.monotonic()
        results = {item['id']: item for item in selfcheck.run(context(), checks, total=2)}
        release.set()
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIn('超时', results['slow']['detail'])
        self.assertIn('ZeroDivisionError', results['boom']['detail'])
        self.assertEqual(results['bad']['status'], 'fail')
        self.assertEqual(results['fine']['status'], 'ok')

    def test_total_budget_bounds_the_run(self):
        release = threading.Event()
        checks = tuple(Check(f'c{i}', str(i), lambda ctx: release.wait(5), timeout=3) for i in range(3))
        started = time.monotonic()
        results = selfcheck.run(context(), checks, total=0.3)
        release.set()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual({item['status'] for item in results}, {'fail'})

    def test_one_run_at_a_time_and_report_lines(self):
        gate = threading.Event()
        checker = SelfCheck(lambda: context(), clock=lambda: 1800000000)
        self.assertEqual(checker.report_lines(), ['自检：本次启动后未运行'])
        original = selfcheck.run
        try:
            selfcheck.run = lambda ctx: (gate.wait(5), [{'id': 'x', 'title': '网关', 'status': 'fail',
                                                         'detail': '规则表不存在', 'fix': ''}])[1]
            worker = threading.Thread(target=checker.run)
            worker.start()
            time.sleep(0.05)
            with self.assertRaises(RuntimeError):
                checker.run()
            gate.set()
            worker.join()
        finally:
            selfcheck.run = original
        lines = checker.report_lines()
        self.assertIn('fail 1', lines[0])
        self.assertEqual(lines[1], '  网关：规则表不存在')

    def test_installer_doctor_runs_the_offline_checks(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import installer.__main__ as installer
        with tempfile.TemporaryDirectory() as tmp:
            layout = SimpleNamespace(data=Path(tmp), config=Path(tmp) / 'missing.toml')
            results = installer.offline_checks(layout, probes=FakeProbes())
        self.assertEqual([item['id'] for item in results], ['conntrack', 'ipv6', 'components', 'resources'])
        self.assertEqual({item['status'] for item in results}, {'ok'})



class SelfCheckHTTPTests(unittest.TestCase):
    def test_post_runs_with_csrf_and_get_returns_the_latest(self):
        import http.client
        from server import AdminHandler, AdminServer, AuthManager
        server = AdminServer(('127.0.0.1', 0), AdminHandler)
        server.auth = AuthManager(AuthManager.make_credentials(PASSWORD))
        checker = SelfCheck(lambda: context(), clock=lambda: 1800000000)
        server.app = SimpleNamespace(settings=SimpleNamespace(subnet='127.0.0.1/32', host_ip='127.0.0.1'),
                                     selfcheck=checker)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        conn = http.client.HTTPConnection(*server.server_address, timeout=15)
        try:
            conn.request('POST', '/api/login', json.dumps({'password': PASSWORD}), {'Content-Type': 'application/json'})
            reply = conn.getresponse()
            cookie, csrf = reply.getheader('Set-Cookie').split(';')[0], json.loads(reply.read())['csrf']

            def call(method, body=None, headers=None):
                conn.request(method, '/api/selfcheck', body, {'Cookie': cookie, **(headers or {})})
                reply = conn.getresponse()
                return reply.status, json.loads(reply.read())
            self.assertEqual(call('GET'), (200, {'last': None}))
            self.assertEqual(call('POST', '{}', {'Content-Type': 'application/json'})[0], 403)
            status, body = call('POST', '{}', {'Content-Type': 'application/json', 'X-CSRF-Token': csrf})
            self.assertEqual(status, 200)
            self.assertEqual(len(body['last']['results']), len(selfcheck.CHECKS))
            self.assertEqual(call('GET')[1], body)
        finally:
            conn.close()
            server.shutdown()
            server.server_close()

if __name__ == '__main__':
    unittest.main()
