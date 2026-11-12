"""Observe PS5 flows that traverse the gateway, join them with DNS and rulesets."""
import datetime
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import threading
from collections import deque

UNKNOWN = '未识别'
_TARGET_IP = re.compile(r'ip:(.+)')
_TARGET_DOMAIN = re.compile(r'domain:(.+)')
_DOMAIN = re.compile(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+')
_LABEL_MAX = 64
_CATEGORY_MAX = 48
_DEDUPE_WINDOW = 300.0


class Classifier:
    """Suffix-based classifier backed by the compiled open-source rulesets."""

    def __init__(self, path):
        document = json.loads(Path(path).read_text(encoding='utf-8'))
        self.categories_order = list(document.get('compiled', {}))
        self.suffixes = [(label, tuple(sorted(items, key=len, reverse=True)))
                         for label, items in document.get('compiled', {}).items()]
        self.exact = document.get('exact', {})

    def categories(self):
        return self.categories_order + [UNKNOWN]

    @staticmethod
    def _normalise(domain):
        if not isinstance(domain, str):
            return ''
        return domain.strip().rstrip('.').lower()

    def category(self, domain):
        name = self._normalise(domain)
        if not name:
            return UNKNOWN
        for label, items in self.exact.items():
            if name in {self._normalise(item) for item in items}:
                return label
        for label, items in self.suffixes:
            for suffix in items:
                if name == suffix or name.endswith('.' + suffix):
                    return label
        return UNKNOWN


def parse_conntrack(text):
    """Parse `conntrack -L` output into original-direction flows."""
    flows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or 'src=' not in line:
            continue
        parts = line.split()
        proto = parts[0]
        fields = {}
        for token in parts:
            if '=' in token:
                key, value = token.split('=', 1)
                fields.setdefault(key, value)
        needed = {'src', 'dst', 'sport', 'dport'}
        if not needed.issubset(fields):
            continue
        try:
            flows.append({
                'proto': proto,
                'src': str(ipaddress.IPv4Address(fields['src'])),
                'dst': str(ipaddress.IPv4Address(fields['dst'])),
                'sport': int(fields['sport']),
                'dport': int(fields['dport']),
                'replied': '[UNREPLIED]' not in line,
            })
        except (ValueError, TypeError):
            continue
    return flows


class LabelsStore:
    """User-assigned names/categories for targets, persisted atomically."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        try:
            self.data = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(self.data, dict):
                raise ValueError('labels file must hold an object')
        except FileNotFoundError:
            self.data = {}
            self._save()
        except (ValueError, OSError):
            logging.exception('labels file unreadable; starting empty')
            self.data = {}

    def _save(self):
        temporary = self.path.with_name('.' + self.path.name + '.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(self.data, stream, ensure_ascii=False, indent=1)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    @staticmethod
    def check_target(target):
        if not isinstance(target, str):
            raise ValueError('目标格式不正确')
        match = _TARGET_IP.fullmatch(target)
        if match:
            try:
                return 'ip:' + str(ipaddress.IPv4Address(match.group(1)))
            except ValueError as exc:
                raise ValueError('IP 地址不合法') from exc
        match = _TARGET_DOMAIN.fullmatch(target)
        if match:
            name = match.group(1).strip().rstrip('.').lower()
            if not _DOMAIN.fullmatch(name):
                raise ValueError('域名不合法')
            return 'domain:' + name
        raise ValueError('目标必须是 ip:地址 或 domain:域名')

    def get(self, target):
        with self.lock:
            return dict(self.data.get(target, {}))

    def set(self, target, name='', category=''):
        target = self.check_target(target)
        name = (name or '').strip()[:_LABEL_MAX]
        category = (category or '').strip()[:_CATEGORY_MAX]
        with self.lock:
            if not name and not category:
                self.data.pop(target, None)
            else:
                entry = self.data.setdefault(target, {})
                entry['name'] = name
                entry['category'] = category
            self._save()
            return self.get(target)

    def all(self):
        with self.lock:
            return {key: dict(value) for key, value in self.data.items()}

    def replace(self, mapping):
        """Swap in every label from a backup; returns (kept, dropped)."""
        if not isinstance(mapping, dict):
            raise ValueError('标签数据格式不正确')
        data, dropped = {}, 0
        for target, value in mapping.items():
            try:
                key = self.check_target(target)
            except ValueError:
                dropped += 1
                continue
            if not isinstance(value, dict):
                dropped += 1
                continue
            name = str(value.get('name') or '').strip()[:_LABEL_MAX]
            category = str(value.get('category') or '').strip()[:_CATEGORY_MAX]
            if name or category:
                data[key] = {'name': name, 'category': category}
        with self.lock:
            self.data = data
            self._save()
        return len(data), dropped


SERVICE_PORTS = {53: '本机 DNS', 853: '本机 DNS', 80: '用户手册', 443: '用户手册',
                 8080: '插件仓库', 8443: '管理页', 9021: 'elfldr', 8084: 'pldmgr'}


class FlowObserver:
    """Poll conntrack, keep a bounded ring of new outbound flows, enrich them."""

    def __init__(self, conntrack, classifier, labels, dns_lookup, is_allowed,
                 lan='127.0.0.0/8', host_ip='127.0.0.1', clock=None, max_events=500,
                 dns_events=None, domain_allowed=None):
        self.conntrack = conntrack
        self.classifier = classifier
        self.labels = labels
        self.dns_lookup = dns_lookup
        self.is_allowed = is_allowed
        self.lan = ipaddress.ip_network(lan)
        self.host_ip = host_ip
        self.clock = clock or (lambda: datetime.datetime.now().timestamp())
        self._max_events = int(max_events)
        self.lock = threading.RLock()
        self.events = deque(maxlen=self._max_events)
        self.seen = {}
        self.counts = {'allowed': 0, 'blocked': 0}
        self.updated = None
        self.last_error = None
        # Optional callable returning recent DNS log rows; they are shown next
        # to connection events because DNS is often the only traffic we see.
        self.dns_events = dns_events
        # name -> allowed under the current whitelist (DNS rows have no IP).
        self.domain_allowed = domain_allowed
        self._dns_seen = {}
        self._dns_last_seq = 0
        self.counts_by_kind = {'flow': 0, 'service': 0, 'dns': 0}

    @property
    def max_events(self):
        return self._max_events

    @max_events.setter
    def max_events(self, value):
        value = max(1, int(value))
        if value == self._max_events:
            return
        self._max_events = value
        with self.lock:
            self.events = deque(list(self.events)[:value], maxlen=value)

    def _target_of(self, flow):
        if str(flow['dst']) == self.host_ip:
            return {
                'time': datetime.datetime.fromtimestamp(self.clock()).astimezone().isoformat(timespec='seconds'),
                'epoch': round(self.clock(), 3),
                'client': flow['src'],
                'dst': flow['dst'],
                'port': flow['dport'],
                'proto': flow['proto'],
                'domain': '',
                'domain_age': None,
                'name': SERVICE_PORTS.get(int(flow['dport']), '本机服务'),
                'category': '本机服务',
                'verdict': 'allowed',
                'replied': flow['replied'],
                'target': 'service:' + str(flow['dport']),
                'kind': 'service',
                'allowed_now': True,
            }
        domain, age = self.dns_lookup(flow['dst'])
        label = self.labels.get('ip:' + flow['dst'])
        if not label and domain:
            label = self.labels.get('domain:' + domain)
        verdict = 'allowed' if self.is_allowed(flow['dst']) else 'blocked'
        category = label.get('category') or self.classifier.category(domain or '')
        if category == UNKNOWN and not domain:
            category = '直连 IP'
        return {
            'time': datetime.datetime.fromtimestamp(self.clock()).astimezone().isoformat(timespec='seconds'),
            'epoch': round(self.clock(), 3),
            'client': flow['src'],
            'dst': flow['dst'],
            'port': flow['dport'],
            'proto': flow['proto'],
            'domain': domain or '',
            'domain_age': age,
            'name': label.get('name', ''),
            'category': category,
            'verdict': verdict,
            'replied': flow['replied'],
            'target': 'ip:' + flow['dst'],
            'kind': 'flow',
            # Accurate for the poll result; snapshot() recomputes it on every read.
            'allowed_now': bool(self.is_allowed(flow['dst'])),
        }

    def poll_once(self):
        # A broken connection tracker must not hide DNS queries: they are
        # collected below either way, and the error is shown next to them.
        try:
            flows = parse_conntrack(self.conntrack())
            error = None
        except FileNotFoundError:
            flows = []
            error = ('本机缺少 conntrack 命令，网关连接无法记录（DNS 查询不受影响）；'
                     '安装：apt-get install -y conntrack')
        except Exception as exc:
            flows = []
            error = '读取网关连接失败：' + str(exc)[:200]
        if error and error != self.last_error:
            logging.warning('conntrack poll failed: %s', error)
        self.last_error = error
        now = self.clock()
        fresh = []
        with self.lock:
            self.seen = {key: stamp for key, stamp in self.seen.items() if now - stamp < _DEDUPE_WINDOW}
            for flow in flows:
                if not self._interesting(flow):
                    continue
                key = (flow['proto'], flow['src'], flow['dst'], flow['dport'])
                if key in self.seen:
                    self.seen[key] = now
                    continue
                self.seen[key] = now
                event = self._target_of(flow)
                self.events.appendleft(event)
                self.counts[event['verdict']] = self.counts.get(event['verdict'], 0) + 1
                kind = event.get('kind', 'flow')
                self.counts_by_kind[kind] = self.counts_by_kind.get(kind, 0) + 1
                fresh.append(event)
            self._collect_dns(now)
            self.updated = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
        return fresh

    # DNS log actions: allowed/local/upstream/error mean the name passed the
    # whitelist (upstream = the resolver's own NXDOMAIN etc.); blocked did not.
    _DNS_ALLOWED = ('allowed', 'answered', 'local', 'forwarded', 'upstream', 'error')
    # nodata = AAAA answered empty on purpose; it is noise, not a decision.
    _DNS_SKIP = ('nodata',)

    def _collect_dns(self, now):
        if not self.dns_events:
            return
        try:
            rows = self.dns_events() or []
        except Exception as exc:  # never let DNS logging break flow capture
            logging.debug('dns event collection failed: %s', exc)
            return
        self._dns_seen = {key: stamp for key, stamp in self._dns_seen.items()
                          if now - stamp < _DEDUPE_WINDOW}
        newest = self._dns_last_seq
        # The log is newest-first; replay oldest-first so the ring stays ordered.
        for row in reversed(list(rows)[:200]):
            if not isinstance(row, dict):
                continue
            seq = row.get('seq')
            if isinstance(seq, int):
                if seq <= self._dns_last_seq:
                    continue
                newest = max(newest, seq)
            name = str(row.get('name') or '')
            client = str(row.get('client') or '')
            action = str(row.get('action') or '')
            if not name or action in self._DNS_SKIP:
                continue
            key = (client, name, str(row.get('qtype') or ''), action)
            if key in self._dns_seen:
                self._dns_seen[key] = now
                continue
            self._dns_seen[key] = now
            verdict = 'allowed' if action in self._DNS_ALLOWED else 'blocked'
            category = self.classifier.category(name)
            event = {
                'time': str(row.get('time') or datetime.datetime.now().astimezone().isoformat(timespec='seconds')),
                'epoch': round(now, 3),
                'client': client,
                'dst': '',
                'port': 53,
                'proto': 'dns',
                'domain': name,
                'domain_age': 0,
                'name': '',
                'category': category or '未识别',
                'verdict': verdict,
                'replied': True,
                'target': 'domain:' + name,
                'kind': 'dns',
                # dns_service logs action='local' only for policy.LOCAL_DOMAINS,
                # so the UI can tell a builtin host answer from a whitelist pass.
                'local': action == 'local',
                'allowed_now': verdict == 'allowed',
            }
            self.events.appendleft(event)
            self.counts[verdict] = self.counts.get(verdict, 0) + 1
            self.counts_by_kind['dns'] = self.counts_by_kind.get('dns', 0) + 1
        self._dns_last_seq = newest

    def _interesting(self, flow):
        try:
            src = ipaddress.IPv4Address(flow['src'])
            dst = ipaddress.IPv4Address(flow['dst'])
        except ValueError:
            return False
        if src not in self.lan or flow['src'] == self.host_ip:
            return False
        if str(dst) == self.host_ip:
            # Traffic into our own services: not routed, but it shows the
            # console is actually talking to us.
            return int(flow['dport']) in SERVICE_PORTS
        if dst in self.lan or dst.is_loopback or dst.is_multicast or dst.is_unspecified:
            return False
        if str(dst) == '255.255.255.255':
            return False
        return True

    def _with_labels(self, event):
        """Labels and "allowed now" are applied at read time, so they stay current."""
        merged = dict(event)
        # verdict is historical; allowed_now tells whether the target is allowed
        # by the whitelist right now, so a row can say 已放行 / 已移出白名单.
        if merged.get('kind') == 'service':
            merged['allowed_now'] = True
        elif merged.get('dst'):
            merged['allowed_now'] = bool(self.is_allowed(merged['dst']))
        elif merged.get('kind') == 'dns' and self.domain_allowed and merged.get('domain'):
            try:
                merged['allowed_now'] = bool(self.domain_allowed(merged['domain']))
            except Exception:
                merged['allowed_now'] = bool(merged.get('allowed_now'))
        else:
            merged['allowed_now'] = bool(merged.get('allowed_now'))
        label = self.labels.get(event['target'])
        if not label and event.get('domain'):
            label = self.labels.get('domain:' + event['domain'])
        if not label:
            return merged
        if label.get('name'):
            merged['name'] = label['name']
        if label.get('category'):
            merged['category'] = label['category']
        return merged

    def snapshot(self, limit=200):
        with self.lock:
            events = [self._with_labels(item) for item in list(self.events)[:limit]]
            by_kind = dict(self.counts_by_kind)
            hints = {
                'by_kind': by_kind,
                'gateway_flows': by_kind.get('flow', 0),
                'service_flows': by_kind.get('service', 0),
                'dns_events': by_kind.get('dns', 0),
            }
            if not hints['gateway_flows'] and hints['service_flows']:
                hints['message'] = ('只看到访问本机的流量：PS5 的默认网关可能没有指向本机，'
                                    '把默认网关也改成本机地址后才会记录它的互联网访问。')
            elif not hints['gateway_flows'] and not hints['service_flows'] and not hints['dns_events']:
                hints['message'] = ('还没有看到任何流量：请确认 PS5 的主 DNS 指向本机，'
                                    '并把默认网关也设为本机地址。')
            return {
                'events': events,
                'counts': dict(self.counts),
                'tracked': len(self.seen),
                'updated': self.updated,
                'last_error': self.last_error,
                'categories': self.classifier.categories(),
                'hints': hints,
            }

    def allow_targets(self, targets):
        """Translate selected targets into whitelist additions."""
        networks, domains = [], []
        for raw in targets:
            target = LabelsStore.check_target(raw)
            if target.startswith('ip:'):
                networks.append(target[3:] + '/32')
            else:
                domains.append(target[7:])
        return {'networks': sorted(set(networks)), 'domains': sorted(set(domains))}


def make_poller(observer, interval=2.0):
    def loop(stop):
        while not stop.wait(interval):
            observer.poll_once()
    return loop
