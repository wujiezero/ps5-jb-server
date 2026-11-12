import sys
import unittest
from pathlib import Path
from threading import RLock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import dns.message
import dns.rcode
import dns.rrset
try:
    from dns_service import DNSPolicy, authorized_answers
except ImportError:
    DNSPolicy = None
    authorized_answers = None

# Fixture-only addresses (RFC 5737 TEST-NET-1 + loopback): the gateway is
# injected, the DNS client is loopback, so no real LAN is assumed anywhere.
HOST_IP = '192.0.2.10'    # address handed out for LOCAL_DOMAINS / host answer
UPSTREAM = '192.0.2.1'    # configured upstream resolver
CLIENT = '127.0.0.1'      # loopback clients are admitted by DNSPolicy.answer


class Store:
    def __init__(self, domains=None):
        self.lock = RLock()
        self.config = {'domains': domains or [], 'networks': [], 'upstream_dns': UPSTREAM}
    def snapshot(self):
        return self.config.copy()


class Firewall:
    def __init__(self):
        self.calls = []
        self.fail = False
    def apply(self, config, leases):
        if self.fail:
            raise RuntimeError('nft failure')
        self.calls.append(dict(leases))


class DNSBehavior(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(DNSPolicy, 'DNS policy implementation is required')
        self.fw = Firewall()
        self.now = 100.0
        self.store = Store(['example.com', '*.example.org'])
        def resolve(query, upstream):
            self.assertEqual(upstream, UPSTREAM)
            response = dns.message.make_response(query)
            response.answer.append(dns.rrset.from_text(query.question[0].name, 20, 'IN', 'A', '93.184.215.14'))
            return response
        self.engine = DNSPolicy(self.store, self.fw, host_ip=HOST_IP, resolver=resolve,
                                clock=lambda: self.now)
    def query(self, name, kind='A'):
        q = dns.message.make_query(name, kind)
        return self.engine.answer(q, CLIENT)
    def test_manuals_always_local(self):
        a = self.query('manuals.playstation.net')
        self.assertEqual(a.answer[0][0].address, HOST_IP)
        self.assertEqual(self.fw.calls, [])
    def test_unknown_domain_denied(self):
        self.assertEqual(self.query('denied.example').rcode(), dns.rcode.NXDOMAIN)
    def test_exact_does_not_allow_subdomain(self):
        self.assertEqual(self.query('evil.example.com').rcode(), dns.rcode.NXDOMAIN)
    def test_wildcard_excludes_apex(self):
        self.assertEqual(self.query('example.org').rcode(), dns.rcode.NXDOMAIN)
        self.assertEqual(self.query('cdn.example.org').rcode(), dns.rcode.NOERROR)
    def test_firewall_updated_before_answer(self):
        answer = self.query('example.com')
        self.assertEqual(answer.rcode(), dns.rcode.NOERROR)
        self.assertIn('93.184.215.14', self.fw.calls[-1])
        self.assertEqual(self.fw.calls[-1]['93.184.215.14'], 20)
    def test_firewall_failure_fails_closed(self):
        self.fw.fail = True
        self.assertEqual(self.query('example.com').rcode(), dns.rcode.SERVFAIL)
    def test_lease_expires(self):
        self.query('example.com')
        self.now += 21
        self.assertEqual(self.engine.live_leases(), {})
    def test_allowed_aaaa_is_nodata(self):
        answer = self.query('example.com', 'AAAA')
        self.assertEqual(answer.rcode(), dns.rcode.NOERROR)
        self.assertEqual(answer.answer, [])
    def test_any_refused(self):
        self.assertEqual(self.query('example.com', 'ANY').rcode(), dns.rcode.REFUSED)
    def test_cache_decrements_ttl(self):
        self.query('example.com')
        self.now += 7
        self.assertLessEqual(self.query('example.com').answer[0].ttl, 13)
    def test_unrelated_queries_do_not_extend_existing_lease(self):
        self.query('example.com')
        expires = self.engine.leases['93.184.215.14']
        self.now += 0.8
        def second(query, upstream):
            response = dns.message.make_response(query)
            response.answer.append(dns.rrset.from_text(query.question[0].name, 20, 'IN', 'A', '1.1.1.1'))
            return response
        self.engine.resolver = second
        self.query('cdn.example.org')
        self.assertEqual(self.engine.leases['93.184.215.14'], expires)
    def test_lease_starts_when_upstream_reply_is_received(self):
        def slow(query, upstream):
            self.now += 2
            response = dns.message.make_response(query)
            response.answer.append(dns.rrset.from_text(query.question[0].name, 1, 'IN', 'A', '1.1.1.1'))
            return response
        self.engine.resolver = slow
        answer = self.query('example.com')
        self.assertEqual(answer.answer[0].ttl, 1)
        self.assertEqual(self.fw.calls[-1], {'1.1.1.1': 1})
        self.assertEqual(self.engine.leases['1.1.1.1'], self.now + 1)
    def test_config_failure_records_diagnostic(self):
        import tempfile
        from policy import ConfigStore
        with tempfile.TemporaryDirectory() as folder:
            store = ConfigStore(Path(folder) / 'config.json')
            engine = DNSPolicy(store, self.fw)
            self.fw.fail = True
            with self.assertRaises(RuntimeError):
                engine.change_config({'domains': ['example.com']})
            self.assertIn('nft failure', engine.last_error)
            self.assertEqual(store.snapshot()['domains'], [])
    def test_upstream_exception_fails_closed(self):
        def fail(*args): raise TimeoutError('test timeout')
        self.engine.resolver = fail
        self.assertEqual(self.query('example.com').rcode(), dns.rcode.SERVFAIL)
    def test_no_dns_amplification_to_external_client(self):
        answer = self.engine.answer(dns.message.make_query('manuals.playstation.net', 'A'), '203.0.113.3')
        self.assertEqual(answer.rcode(), dns.rcode.REFUSED)
    def test_cname_filters_unrelated_addresses(self):
        query = dns.message.make_query('example.com', 'A')
        response = dns.message.make_response(query)
        response.answer = [dns.rrset.from_text('example.com.', 15, 'IN', 'CNAME', 'cdn.other.net.'),
                           dns.rrset.from_text('cdn.other.net.', 20, 'IN', 'A', '93.184.215.14'),
                           dns.rrset.from_text('unrelated.net.', 100, 'IN', 'A', '1.2.3.4')]
        records, addresses, ttl = authorized_answers(response, query.question[0].name)
        self.assertEqual(addresses, ['93.184.215.14'])
        self.assertEqual(len(records), 2)
        self.assertEqual(ttl, 15)
    def test_private_rebinding_not_authorized(self):
        query = dns.message.make_query('example.com', 'A')
        response = dns.message.make_response(query)
        response.answer = [dns.rrset.from_text('example.com.', 20, 'IN', 'A', '127.0.0.1')]
        with self.assertRaises(ValueError):
            authorized_answers(response, query.question[0].name)


class LockFreeUpstreamTests(unittest.TestCase):
    """The upstream round trip must not hold the policy lock (A2)."""

    def setUp(self):
        import threading
        self.threading = threading
        self.fw = Firewall()
        self.store = Store(['example.com', 'slow.example', 'cached.example'])
        self.release = threading.Event()
        self.entered = threading.Event()
        self.calls = []

        def resolve(query, upstream):
            name = query.question[0].name.to_text(omit_final_dot=True)
            self.calls.append(name)
            if name == 'slow.example':
                self.entered.set()
                assert self.release.wait(5), 'test never released the upstream'
            response = dns.message.make_response(query)
            response.answer.append(dns.rrset.from_text(query.question[0].name, 60, 'IN', 'A', '93.184.215.14'))
            return response
        self.engine = DNSPolicy(self.store, self.fw, host_ip=HOST_IP, resolver=resolve)

    def tearDown(self):
        self.release.set()

    def ask(self, name, results):
        results.append(self.engine.answer(dns.message.make_query(name, 'A'), CLIENT))

    def start(self, name, results):
        thread = self.threading.Thread(target=self.ask, args=(name, results), daemon=True)
        thread.start()
        return thread

    def test_local_and_cached_names_answer_while_upstream_hangs(self):
        import time
        self.ask('cached.example', [])
        slow = []
        thread = self.start('slow.example', slow)
        self.assertTrue(self.entered.wait(2))
        started = time.monotonic()
        local = self.engine.answer(dns.message.make_query('manuals.playstation.net', 'A'), CLIENT)
        cached = self.engine.answer(dns.message.make_query('cached.example', 'A'), CLIENT)
        blocked = self.engine.answer(dns.message.make_query('nope.example', 'A'), CLIENT)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(local.answer[0][0].address, HOST_IP)
        self.assertEqual(cached.answer[0][0].address, '93.184.215.14')
        self.assertEqual(blocked.rcode(), dns.rcode.NXDOMAIN)
        self.release.set()
        thread.join(5)
        self.assertEqual(slow[0].rcode(), dns.rcode.NOERROR)

    def test_policy_change_during_upstream_wait_denies_the_answer(self):
        slow = []
        thread = self.start('slow.example', slow)
        self.assertTrue(self.entered.wait(2))
        with self.store.lock:
            self.store.config['domains'] = ['example.com']
            self.engine.generation += 1
            self.engine.cache.clear()
            self.engine.leases = {}
        self.release.set()
        thread.join(5)
        self.assertEqual(slow[0].rcode(), dns.rcode.NXDOMAIN)
        self.assertEqual(self.fw.calls, [], 'no lease may be granted for a removed name')
        self.assertNotIn('slow.example', self.engine.cache)
        self.assertEqual(self.engine.live_leases(), {})

    def test_policy_change_that_keeps_the_name_still_answers(self):
        slow = []
        thread = self.start('slow.example', slow)
        self.assertTrue(self.entered.wait(2))
        with self.store.lock:
            self.engine.generation += 1
        self.release.set()
        thread.join(5)
        self.assertEqual(slow[0].rcode(), dns.rcode.NOERROR)
        self.assertIn('93.184.215.14', self.engine.live_leases())

    def test_concurrent_queries_for_one_name_share_one_upstream_request(self):
        first, second = [], []
        one = self.start('slow.example', first)
        self.assertTrue(self.entered.wait(2))
        two = self.start('slow.example', second)
        import time
        time.sleep(0.1)  # let the second query reach the in-flight wait
        self.release.set()
        one.join(5)
        two.join(5)
        self.assertEqual(self.calls, ['slow.example'])
        self.assertEqual(first[0].answer[0][0].address, '93.184.215.14')
        self.assertEqual(second[0].answer[0][0].address, '93.184.215.14')
        self.assertEqual(self.engine.inflight, {})

    def test_waiter_queries_itself_after_the_wait_limit(self):
        self.engine.INFLIGHT_WAIT = 0.05
        first, second = [], []
        one = self.start('slow.example', first)
        self.assertTrue(self.entered.wait(2))
        two = self.start('slow.example', second)
        import time
        time.sleep(0.3)
        self.release.set()
        one.join(5)
        two.join(5)
        self.assertEqual(self.calls.count('slow.example'), 2)
        self.assertEqual(second[0].rcode(), dns.rcode.NOERROR)

    def test_upstream_error_is_servfail_and_recorded(self):
        def broken(query, upstream):
            raise TimeoutError('upstream timeout')
        self.engine.resolver = broken
        answer = self.engine.answer(dns.message.make_query('example.com', 'A'), CLIENT)
        self.assertEqual(answer.rcode(), dns.rcode.SERVFAIL)
        self.assertIn('upstream timeout', self.engine.last_error)
        self.assertEqual(self.engine.inflight, {})


class IncrementalFirewall(Firewall):
    """Records both paths; the full-table fallback stays apply()."""

    def __init__(self):
        super().__init__()
        self.added = []
        self.fail_add = False

    def add_leases(self, leases, refresh=()):
        if self.fail_add:
            raise RuntimeError('delete element: No such file or directory')
        self.added.append((dict(leases), sorted(refresh)))


class IncrementalLeaseTests(unittest.TestCase):
    """Answers write only their own addresses to nft (A3)."""

    def setUp(self):
        self.fw = IncrementalFirewall()
        self.now = 100.0
        self.ttl = 20
        self.addresses = {}
        self.store = Store(['*.example.com'])

        def resolve(query, upstream):
            name = query.question[0].name.to_text(omit_final_dot=True)
            response = dns.message.make_response(query)
            address = self.addresses.get(name, '93.184.215.14')
            response.answer.append(dns.rrset.from_text(query.question[0].name, self.ttl, 'IN', 'A', address))
            return response
        self.engine = DNSPolicy(self.store, self.fw, host_ip=HOST_IP, resolver=resolve, clock=lambda: self.now)

    def query(self, name):
        return self.engine.answer(dns.message.make_query(name, 'A'), CLIENT)

    def test_first_answer_only_adds_its_addresses(self):
        self.addresses['a.example.com'] = '1.1.1.1'
        self.query('a.example.com')
        self.addresses['b.example.com'] = '8.8.8.8'
        self.query('b.example.com')
        self.assertEqual(self.fw.added, [({'1.1.1.1': 20}, []), ({'8.8.8.8': 20}, [])])
        self.assertEqual(self.fw.calls, [], 'no full table replacement')
        self.assertEqual(set(self.engine.live_leases()), {'1.1.1.1', '8.8.8.8'})

    def test_longer_lease_on_a_live_address_is_refreshed(self):
        self.addresses['a.example.com'] = self.addresses['b.example.com'] = '1.1.1.1'
        self.query('a.example.com')
        self.now += 5
        self.query('b.example.com')
        self.assertEqual(self.fw.added[-1], ({'1.1.1.1': 20}, ['1.1.1.1']))
        self.assertEqual(self.engine.leases['1.1.1.1'], 125.0)

    def test_shorter_lease_on_a_live_address_writes_nothing(self):
        self.addresses['a.example.com'] = self.addresses['b.example.com'] = '1.1.1.1'
        self.query('a.example.com')
        self.ttl = 5
        self.query('b.example.com')
        self.assertEqual(len(self.fw.added), 1)
        self.assertEqual(self.engine.leases['1.1.1.1'], 120.0)

    def test_incremental_failure_falls_back_to_the_full_table(self):
        self.addresses['a.example.com'] = '1.1.1.1'
        self.query('a.example.com')
        self.fw.fail_add = True
        self.addresses['b.example.com'] = '8.8.8.8'
        with self.assertLogs(level='WARNING') as logs:
            answer = self.query('b.example.com')
        self.assertEqual(answer.rcode(), dns.rcode.NOERROR)
        self.assertEqual(self.fw.calls, [{'1.1.1.1': 20, '8.8.8.8': 20}])
        self.assertIn('replacing the table', '\n'.join(logs.output))

    def test_fallback_failure_is_servfail_without_a_lease(self):
        self.fw.fail_add = True
        self.fw.fail = True
        self.assertEqual(self.query('a.example.com').rcode(), dns.rcode.SERVFAIL)
        self.assertEqual(self.engine.live_leases(), {})

    def test_ten_new_names_are_ten_nft_writes(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            from types import SimpleNamespace
            return SimpleNamespace(stdout='', returncode=0)
        from policy import Firewall as Real
        self.engine.firewall = Real(host_ip='127.0.0.1', lan='127.0.0.0/8', runner=runner)
        for index in range(10):
            self.addresses[f'n{index}.example.com'] = f'1.1.1.{index + 1}'
            self.assertEqual(self.query(f'n{index}.example.com').rcode(), dns.rcode.NOERROR)
        self.assertEqual(len(calls), 10)


class TransportTests(unittest.TestCase):
    def test_real_udp_and_tcp_reply(self):
        from dns_service import start_dns
        import dns.query
        engine = DNSPolicy(Store(), Firewall(), host_ip=HOST_IP)
        servers = start_dns(engine, '127.0.0.1', 0)
        try:
            query = dns.message.make_query('manuals.playstation.net', 'A')
            udp = dns.query.udp(query, '127.0.0.1', port=servers[0].server_address[1], timeout=1)
            tcp = dns.query.tcp(query, '127.0.0.1', port=servers[1].server_address[1], timeout=1)
            self.assertEqual(udp.answer[0][0].address, HOST_IP)
            self.assertEqual(tcp.answer[0][0].address, HOST_IP)
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
