"""DNS allowlist, bounded TTL leases, and UDP/TCP transports."""
import copy
import datetime
import ipaddress
import logging
import math
import socket
import socketserver
import struct
import threading
import time
from collections import deque

import dns.exception
import dns.flags
import dns.message
import dns.name
import dns.opcode
import dns.query
import dns.rcode
import dns.rdataclass
import dns.rdatatype
import dns.rrset
from policy import LOCAL_DOMAINS, domain_allowed, validate_config


def diagnostic_error(exc):
    # Only fixed utility errors and validated configuration can reach this text.
    detail = getattr(exc, 'stderr', None) or str(exc) or type(exc).__name__
    if isinstance(detail, bytes):
        detail = detail.decode('utf-8', 'replace')
    return ' '.join(detail.split())[:350]


def authorized_answers(response, query_name):
    """Only the queried name's CNAME chain and terminal A RRset grant access."""
    current = query_name.canonicalize()
    records, seen = [], set()
    ttl = 86400
    for _ in range(17):
        if current in seen:
            raise ValueError('DNS CNAME 循环')
        seen.add(current)
        matching = [rr for rr in response.answer if rr.name.canonicalize() == current
                    and rr.rdclass == dns.rdataclass.IN]
        aliases = [rr for rr in matching if rr.rdtype == dns.rdatatype.CNAME]
        addresses = [rr for rr in matching if rr.rdtype == dns.rdatatype.A]
        if aliases:
            if len(aliases) != 1 or len(aliases[0]) != 1 or addresses:
                raise ValueError('DNS CNAME 响应歧义')
            rr = aliases[0]
            ttl = min(ttl, rr.ttl)
            records.append(copy.deepcopy(rr))
            current = rr[0].target.canonicalize()
            continue
        ips = []
        for rr in addresses:
            ttl = min(ttl, rr.ttl)
            for entry in rr:
                ip = ipaddress.IPv4Address(entry.address)
                if not ip.is_global:
                    raise ValueError('域名解析到非公网地址，请使用显式 IP/CIDR 规则')
                ips.append(str(ip))
            records.append(copy.deepcopy(rr))
        if not ips and records:
            raise ValueError('DNS CNAME 缺少最终 A 记录')
        if ttl <= 0 and ips:
            raise ValueError('DNS 地址 TTL 为 0，无法建立有效授权')
        return records, sorted(set(ips)), max(1, ttl)
    raise ValueError('DNS CNAME 链过长')


def upstream_query(query, upstream):
    response, _ = dns.query.udp_with_fallback(query, upstream, timeout=3)
    return response


class DNSPolicy:
    def __init__(self, store, firewall, host_ip='127.0.0.1', lan='127.0.0.0/8', resolver=None, clock=None):
        self.store, self.firewall, self.host_ip = store, firewall, host_ip
        self.resolver = resolver or upstream_query
        self.clock = clock or time.monotonic
        self.leases = {}
        self.cache = {}
        # Destination IP -> (domain, last seen) so flow events can show the name.
        self.ip_domains = {}
        self.logs = deque(maxlen=200)
        self.last_applied = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.last_error = None
        self.gateway_error = None
        self.on_apply = None
        self.lan = ipaddress.ip_network(lan, strict=False)
        self.log_seq = 0
        # Bumped on every successful policy change; answers started under an
        # older generation are re-checked before they grant anything.
        self.generation = 0
        # name -> Event for upstream queries in progress (guarded by store.lock).
        self.inflight = {}

    def apply_rules(self, config, leases):
        self.firewall.apply(config, leases)
        if self.on_apply:
            self.on_apply(True)
        self.gateway_error = None

    def lookup_ip(self, ip):
        """Return (domain, age seconds) for a destination we resolved recently."""
        found = self.ip_domains.get(ip)
        if not found:
            return None, None
        return found[0], max(0, round(self.clock() - found[1], 1))

    def log(self, client, name, qtype, action, detail=''):
        # seq lets readers (the activity page) pick up only rows they have not seen.
        self.log_seq += 1
        self.logs.appendleft({'seq': self.log_seq,
                              'time': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
                              'client': client, 'name': name, 'qtype': qtype,
                              'action': action, 'detail': str(detail)[:240]})

    def live_leases(self):
        """Read-only view of unexpired leases.

        Other threads (activity poller, state reads) call this without the store
        lock, so it must never write self.leases: a stale reassignment could drop
        a lease the DNS thread has just granted. Pruning happens in answer().
        """
        now = self.clock()
        return {ip: max(1, math.ceil(expiry - now))
                for ip, expiry in list(self.leases.items()) if expiry > now}

    def domain_allowed_now(self, name):
        """Whether a DNS name would be answered under the current whitelist."""
        name = str(name or '').rstrip('.').lower()
        if not name:
            return False
        return name in LOCAL_DOMAINS or domain_allowed(name, self.store.snapshot()['domains'])

    def check_upstream(self, config):
        """The upstream resolver must not be this service or a LAN edge address."""
        upstream = ipaddress.IPv4Address(config['upstream_dns'])
        if str(upstream) == str(self.host_ip):
            raise ValueError('上游 DNS 不能是本服务地址（会形成查询回环），请填家庭路由器或公共 DNS')
        if self.lan.version == 4 and self.lan.num_addresses > 2 and upstream in (
                self.lan.network_address, self.lan.broadcast_address):
            raise ValueError('上游 DNS 不能是网段的网络地址或广播地址')

    def change_config(self, raw=None, rollback=False):
        with self.store.lock:
            old = self.store.snapshot()
            if rollback:
                previous = self.store.previous
                if previous is not None:
                    self.check_upstream(previous)
            else:
                self.check_upstream(validate_config(raw))
            try:
                if rollback:
                    self.store.rollback(lambda cfg: self.apply_rules(cfg, {}))
                else:
                    self.store.update(raw, lambda cfg: self.apply_rules(cfg, {}))
            except Exception as exc:
                self.last_error = diagnostic_error(exc)
                # ConfigStore compensates its own transaction; restore old valid leases too.
                try:
                    self.apply_rules(old, self.live_leases())
                except Exception as recovery:
                    self.gateway_error = '策略恢复失败：' + diagnostic_error(recovery)
                    self.last_error += '；' + self.gateway_error
                    if self.on_apply:
                        try:
                            self.on_apply(False)
                        except Exception:
                            logging.exception('Unable to disable forwarding')
                    logging.exception('Unable to restore previous leases')
                raise
            self.generation += 1
            self.cache.clear()
            self.leases = {}
            self.ip_domains.clear()
            self.last_applied = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self.last_error = None

    # A later query for a name already being resolved waits this long for the
    # first one to fill the cache, then asks upstream itself.
    INFLIGHT_WAIT = 4.0

    def answer(self, query, client):
        """Answer one query; the upstream round trip runs without the policy lock.

        1. Locked: snapshot policy and generation; refuse, answer local names
           and cache hits directly.
        2. Unlocked: ask upstream (one request per name at a time).
        3. Locked again: if the policy changed meanwhile, re-check the name under
           the new policy; only then grant leases, fill the cache and log.
        """
        answer = dns.message.make_response(query)
        answer.flags |= dns.flags.RA
        try:
            source = ipaddress.ip_address(client)
            if not (source.is_loopback or source in self.lan):
                answer.set_rcode(dns.rcode.REFUSED)
                return answer
        except ValueError:
            answer.set_rcode(dns.rcode.REFUSED)
            return answer
        if (query.opcode() != dns.opcode.QUERY or len(query.question) != 1
                or query.flags & dns.flags.QR or query.question[0].rdclass != dns.rdataclass.IN):
            answer.set_rcode(dns.rcode.REFUSED)
            return answer
        question = query.question[0]
        name = question.name.to_text(omit_final_dot=True).lower()
        qtype = dns.rdatatype.to_text(question.rdtype)
        with self.store.lock:
            cfg = self.store.snapshot()
            generation = self.generation
            if name not in LOCAL_DOMAINS and not domain_allowed(name, cfg['domains']):
                answer.set_rcode(dns.rcode.NXDOMAIN)
                self.log(client, name, qtype, 'blocked', '不在域名白名单')
                return answer
            if question.rdtype not in (dns.rdatatype.A, dns.rdatatype.AAAA):
                answer.set_rcode(dns.rcode.REFUSED)
                self.log(client, name, qtype, 'blocked', '仅提供 A/AAAA 查询')
                return answer
            if question.rdtype == dns.rdatatype.AAAA:
                self.log(client, name, qtype, 'nodata', 'IPv6 不提供出站授权')
                return answer
            if name in LOCAL_DOMAINS:
                answer.answer.append(dns.rrset.from_text(question.name, 30, 'IN', 'A', self.host_ip))
                self.log(client, name, qtype, 'local', self.host_ip)
                return answer
            if self._from_cache(answer, client, name, qtype):
                return answer
            waiting = self.inflight.get(name)
            if waiting is None:
                leader = self.inflight[name] = threading.Event()
        if waiting is not None:
            # Someone is already asking upstream for this name: reuse their answer.
            waiting.wait(self.INFLIGHT_WAIT)
            with self.store.lock:
                if self._from_cache(answer, client, name, qtype):
                    return answer
                cfg = self.store.snapshot()
                generation = self.generation
            leader = None
        try:
            return self._resolve(answer, client, name, qtype, question, cfg, generation)
        finally:
            if leader is not None:
                with self.store.lock:
                    if self.inflight.get(name) is leader:
                        del self.inflight[name]
                leader.set()

    def _from_cache(self, answer, client, name, qtype):
        """Caller holds the store lock. Cache entries exist only under the current policy."""
        now = self.clock()
        cached = self.cache.get(name)
        if not (cached and cached[0] > now):
            return False
        expiry, records = cached
        answer.answer = copy.deepcopy(records)
        for rr in answer.answer:
            rr.ttl = max(0, math.floor(expiry - now))
        self.log(client, name, qtype, 'allowed', 'DNS 缓存')
        return True

    def _resolve(self, answer, client, name, qtype, question, cfg, generation):
        try:
            forwarded = dns.message.make_query(question.name, 'A', use_edns=0, payload=1232)
            response = self.resolver(forwarded, cfg['upstream_dns'])
            # DNS TTL begins at receipt of the upstream response. Time spent
            # waiting upstream must not consume a lease before it is returned.
            received = self.clock()
            if response.rcode() == dns.rcode.NOERROR:
                records, ips, ttl = authorized_answers(response, question.name)
        except Exception as exc:
            with self.store.lock:
                return self._fail(answer, client, name, qtype, exc)
        with self.store.lock:
            if self.generation != generation:
                # The policy changed while upstream was answering: decide again.
                cfg = self.store.snapshot()
                if not domain_allowed(name, cfg['domains']):
                    answer.set_rcode(dns.rcode.NXDOMAIN)
                    self.log(client, name, qtype, 'blocked', '查询期间已移出域名白名单')
                    return answer
            if response.rcode() != dns.rcode.NOERROR:
                answer.set_rcode(response.rcode())
                self.log(client, name, qtype, 'upstream', dns.rcode.to_text(response.rcode()))
                return answer
            try:
                self._grant(cfg, ips, received + ttl)
            except Exception as exc:
                return self._fail(answer, client, name, qtype, exc)
            for rr in records:
                rr.ttl = min(rr.ttl, ttl)
            now = received
            if len(self.cache) >= 512:
                self.cache = {key: value for key, value in self.cache.items() if value[0] > now}
                if len(self.cache) >= 512:
                    self.cache.pop(next(iter(self.cache)))
            self.cache[name] = (now + ttl, copy.deepcopy(records))
            for ip in ips:
                self.ip_domains[ip] = (name, now)
            if len(self.ip_domains) > 4096:
                keep = sorted(self.ip_domains.items(), key=lambda kv: kv[1][1])[-2048:]
                self.ip_domains = dict(keep)
            answer.answer = records
            self.last_error = None
            self.log(client, name, qtype, 'allowed', ', '.join(ips) or '无 A 记录')
            return answer

    def _fail(self, answer, client, name, qtype, exc):
        """Caller holds the store lock."""
        answer.answer = []
        answer.set_rcode(dns.rcode.SERVFAIL)
        self.last_error = str(exc)[:240]
        self.log(client, name, qtype, 'error', self.last_error)
        logging.warning('DNS %s failed: %s', name, exc)
        return answer

    def _grant(self, cfg, ips, expiry):
        """Caller holds the store lock. Authorize ips until expiry before answering."""
        now = self.clock()
        pending = {ip: until for ip, until in self.leases.items() if until > now}
        if len(pending) + len(ips) > 2048:
            raise ValueError('动态地址授权数量超过限制')
        for ip in ips:
            pending[ip] = max(pending.get(ip, 0), expiry)
        if ips:
            # Keep absolute expiry times: rounding on unrelated queries must
            # never extend an existing authorization. Kernel timeouts may
            # outlast fractional TTL by at most one second, without drift.
            leases = {ip: max(1, math.ceil(until - self.clock()))
                      for ip, until in pending.items() if until > self.clock()}
            self.firewall.apply(cfg, leases)
            self.leases = pending


class LimitedThreads(socketserver.ThreadingMixIn):
    daemon_threads = True
    block_on_close = False
    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)
    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise
    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


def read_exact(sock, size):
    result = b''
    while len(result) < size:
        part = sock.recv(size - len(result))
        if not part:
            raise EOFError('short DNS TCP frame')
        result += part
    return result


class UDPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        if len(data) > 4096:
            return
        try:
            query = dns.message.from_wire(data)
            response = self.server.engine.answer(query, self.client_address[0])
            limit = max(512, min(query.payload or 512, 1232))
            try:
                payload = response.to_wire(max_size=limit)
            except dns.exception.TooBig:
                # dnspython 2.3 (Debian 12) has no prefer_truncation option.
                # Return TC and let clients retry the full response over TCP.
                response.answer = []
                response.authority = []
                response.additional = []
                response.flags |= dns.flags.TC
                payload = response.to_wire(max_size=limit)
            sock.sendto(payload, self.client_address)
        except (dns.exception.DNSException, ValueError, OSError):
            pass


class TCPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        try:
            length = struct.unpack('!H', read_exact(self.request, 2))[0]
            if length > 4096:
                return
            query = dns.message.from_wire(read_exact(self.request, length))
            wire = self.server.engine.answer(query, self.client_address[0]).to_wire()
            self.request.sendall(struct.pack('!H', len(wire)) + wire)
        except (dns.exception.DNSException, ValueError, OSError, EOFError):
            pass


class UDPServer(LimitedThreads, socketserver.UDPServer):
    allow_reuse_address = True


class TCPServer(LimitedThreads, socketserver.TCPServer):
    allow_reuse_address = True


def start_dns(engine, ip='0.0.0.0', port=53):
    servers = []
    try:
        for cls, handler in [(UDPServer, UDPHandler), (TCPServer, TCPHandler)]:
            server = cls((ip, port), handler)
            server.engine = engine
            threading.Thread(target=server.serve_forever, daemon=True).start()
            servers.append(server)
        return servers
    except Exception:
        for server in servers:
            server.shutdown()
            server.server_close()
        raise
