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
