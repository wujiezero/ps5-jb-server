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


class FlowObserver:
    """Poll conntrack, keep a bounded ring of new outbound flows, enrich them."""

    def __init__(self, conntrack, classifier, labels, dns_lookup, is_allowed,
                 lan='127.0.0.0/8', host_ip='127.0.0.1', clock=None, max_events=500):
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
            # Accurate for the poll result; snapshot() recomputes it on every read.
            'allowed_now': bool(self.is_allowed(flow['dst'])),
        }

    def poll_once(self):
        try:
            flows = parse_conntrack(self.conntrack())
        except Exception as exc:
            self.last_error = str(exc)[:200]
            logging.warning('conntrack poll failed: %s', exc)
            return []
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
                fresh.append(event)
            self.updated = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
        return fresh

    def _interesting(self, flow):
        try:
            src = ipaddress.IPv4Address(flow['src'])
            dst = ipaddress.IPv4Address(flow['dst'])
        except ValueError:
            return False
        if src not in self.lan or flow['src'] == self.host_ip:
            return False
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
        merged['allowed_now'] = bool(self.is_allowed(merged['dst']))
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
            return {
                'events': events,
                'counts': dict(self.counts),
                'tracked': len(self.seen),
                'updated': self.updated,
                'last_error': self.last_error,
                'categories': self.classifier.categories(),
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
