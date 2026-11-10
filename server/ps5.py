"""PS5 discovery and control (Payload Manager HTTP 8084 / elfldr TCP 9021).

Only plain HTTP is spoken, only to addresses inside the configured LAN, and
mutations carry the X-PLDMGR-Request marker that the console's web server uses
in place of an auth token.
"""
import concurrent.futures
import ipaddress
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# pldmgr accepts mutations only when this header equals "1"
MARKER = {"X-PLDMGR-Request": "1"}
ELF_PORT = 9021
PLDMGR_PORT = 8084
LOADER_PORT = 9020


DDP_PORT = 9302
DDP_VERSION = '00030010'


def parse_ddp(text):
    """Parse a Device Discovery Protocol reply (HTTP-like status + header lines)."""
    lines = [line.strip() for line in str(text).replace('\r', '').split('\n') if line.strip()]
    if not lines or not lines[0].startswith('HTTP/'):
        return None
    parts = lines[0].split(' ', 2)
    try:
        code = int(parts[1])
    except (IndexError, ValueError):
        return None
    headers = {}
    for line in lines[1:]:
        key, sep, value = line.partition(':')
        if sep:
            headers[key.strip().lower()] = value.strip()
    version = headers.get('system-version', '')
    firmware = ''
    if len(version) >= 4 and version.isdigit():
        firmware = f'{int(version[:2])}.{version[2:4]}'
    state = {200: 'on', 620: 'standby'}.get(code, 'unknown')
    return {'code': code, 'state': state, 'host_type': headers.get('host-type', ''),
            'name': headers.get('host-name', ''), 'host_id': headers.get('host-id', ''),
            'firmware': firmware, 'running_app': headers.get('running-app-name', '')}


def ddp_search(targets, timeout=1.0):
    """Ask PlayStations for their power state over DDP (UDP 9302).

    Works without any payload and while the console is in rest mode, which is
    exactly when every TCP service is gone. Returns {ip: parsed reply}.
    """
    message = f'SRCH * HTTP/1.1\ndevice-discovery-protocol-version:{DDP_VERSION}\n'.encode()
    replies = {}
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return replies
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(('0.0.0.0', 0))
        for target in targets:
            try:
                sock.sendto(message, (target, DDP_PORT))
            except OSError:
                continue
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, (address, _) = sock.recvfrom(4096)
            except (socket.timeout, OSError):
                break
            parsed = parse_ddp(data.decode('utf-8', 'replace'))
            if parsed and parsed['host_type'].upper().startswith('PS'):
                replies[address] = parsed
    finally:
        sock.close()
    return replies


def _tcp_open(ip, port, timeout):
    try:
        with socket.create_connection((ip, port), timeout):
            return True
    except OSError:
        return False


class Discovery:
    """Bounded TCP scan of the LAN for elfldr / pldmgr / the boot loader."""

    def __init__(self, subnet, timeout=0.3, workers=48, cache_ttl=60,
                 elf_port=ELF_PORT, pldmgr_port=PLDMGR_PORT, loader_port=LOADER_PORT):
        self.ports = (('elfldr', elf_port), ('pldmgr', pldmgr_port), ('loader', loader_port))
        self.subnet = _bounded(ipaddress.ip_network(subnet, strict=False))
        self.timeout = timeout
        self.workers = workers
        self.cache_ttl = cache_ttl
        self.lock = threading.RLock()
        self.cached = {'hosts': [], 'last_scan': None, 'duration_ms': 0, 'scanned': 0}
        self.extra = []
        self.scanning = False
        self.scan_lock = threading.Lock()

    def remember(self, ip):
        """Prefer addresses we already saw (activity page, manual add)."""
        with self.lock:
            if ip not in self.extra:
                self.extra.insert(0, ip)

    def scan(self, extra_ips=()):
        # One sweep at a time: a manual scan waits for a background one.
        with self.scan_lock:
            self.scanning = True
            try:
                return self._scan(extra_ips)
            finally:
                self.scanning = False

    def scan_in_background(self, extra_ips=()):
        if self.scanning:
            return False
        self.scanning = True    # visible to the page before the thread starts
        threading.Thread(target=self.scan, args=(tuple(extra_ips),), daemon=True,
                         name='ps5-discovery').start()
        return True

    def _scan(self, extra_ips=()):
        started = time.monotonic()
        addresses = []
        for ip in list(extra_ips) + list(self.extra):
            try:
                address = str(ipaddress.IPv4Address(ip))
            except ValueError:
                continue
            if ipaddress.IPv4Address(address) in self.subnet and address not in addresses:
                addresses.append(address)
        addresses += [str(host) for host in self.subnet.hosts() if str(host) not in addresses]

        def probe(ip):
            return ip, {name for name, port in self.ports if _tcp_open(ip, port, self.timeout)}

        # DDP first: a broadcast finds consoles in rest mode or without payloads.
        power = ddp_search([str(self.subnet.broadcast_address)] + addresses[:32], timeout=1.2)
        power = {ip: info for ip, info in power.items()
                 if ipaddress.IPv4Address(ip) in self.subnet}

        hits = {}
        # Each worker probes all ports of one address: far fewer threads than
        # addresses x ports, which matters in small containers.
        for workers in (self.workers, 16, 4):
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, len(addresses) or 1))) as pool:
                    hits = {ip: ports for ip, ports in pool.map(probe, addresses) if ports}
                break
            except RuntimeError:
                # Thread starvation: retry with fewer workers, then serially.
                continue
        else:
            hits = {ip: ports for ip, ports in (probe(address) for address in addresses) if ports}

        for ip in power:
            hits.setdefault(ip, set())
        found = [{'ip': ip, 'elfldr': 'elfldr' in ports, 'pldmgr': 'pldmgr' in ports,
                  'loader': 'loader' in ports, 'power': power.get(ip)}
                 for ip, ports in sorted(hits.items(), key=lambda item: ipaddress.IPv4Address(item[0]))]
        result = {'hosts': found, 'last_scan': _now(), 'scanned': len(addresses),
                  'duration_ms': int((time.monotonic() - started) * 1000)}
        with self.lock:
            self.cached = dict(result)
            for host in found:
                if host['ip'] not in self.extra:
                    self.extra.append(host['ip'])
            self.extra = self.extra[:32]
        return result

    def snapshot(self):
        with self.lock:
            result = json.loads(json.dumps(self.cached))
        result['scanning'] = self.scanning
        return result


def _bounded(network, limit=1024):
    """Never sweep more than a /22: a wide subnet would take minutes."""
    if network.num_addresses <= limit:
        return network
    return ipaddress.ip_network(f'{next(iter(network.hosts()))}/24', strict=False) \
        if network.version == 4 else network


def _now():
    import datetime
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


class ConsoleError(RuntimeError):
    """The console answered, but not acceptably."""


# pldmgr autoload sequence (/data/pldmgr/autoload.txt, AUTOLOAD_LIST over HTTP):
# comma separated entries, run in order. "local/<slot>" launches a payload,
# "!<ms>" waits that long before the next entry. AUTOLOAD_DELAY is the
# countdown in seconds before the sequence starts.
MAX_ENTRY_DELAY_MS = 3600000
MAX_ENTRY_TEXT = 511
MAX_LIST_TEXT = 4000
MAX_START_DELAY = 3600


def parse_autoload(raw, slots=None):
    """Split AUTOLOAD_LIST into payload items, each with the wait that follows it."""
    slots = slots or {}
    items, lead_ms = [], 0
    for token in [part.strip() for part in str(raw or '').split(',') if part.strip()]:
        if token.startswith('!') and token[1:].isdigit():
            delay = min(int(token[1:]), MAX_ENTRY_DELAY_MS)
            if items:
                items[-1]['delay_ms'] += delay
            else:
                lead_ms += delay
            continue
        meta = slots.get(token) or {}
        items.append({'key': token, 'filename': meta.get('filename') or token.rsplit('/', 1)[-1],
                      'installed': bool(meta), 'delay_ms': 0})
    return {'items': items, 'lead_ms': lead_ms}


def serialize_autoload(items, lead_ms=0):
    """Validate an edited sequence and render it in pldmgr's format."""
    if not isinstance(items, list) or len(items) > 64:
        raise ValueError('自动加载列表最多 64 个插件')

    def delay_of(value, label):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f'{label}必须是数字')
        value = int(round(value))
        if not 0 <= value <= MAX_ENTRY_DELAY_MS:
            raise ValueError(f'{label}必须在 0–3600 秒之间')
        return value

    tokens = []
    lead = delay_of(lead_ms or 0, '开始前等待')
    if lead:
        tokens.append(f'!{lead}')
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError('自动加载条目格式不正确')
        key = item.get('key')
        if (not isinstance(key, str) or not key.strip() or key.startswith('!')
                or len(key) > MAX_ENTRY_TEXT or any(c in key for c in ',\r\n\0')
                or key.strip().startswith('/') or '..' in key.split('/')):
            raise ValueError(f'第 {index} 项插件标识不合法')
        tokens.append(key.strip())
        delay = delay_of(item.get('delay_ms', 0), f'第 {index} 项的加载后等待')
        if delay:
            tokens.append(f'!{delay}')
    text = ','.join(tokens)
    if len(text) > MAX_LIST_TEXT:
        raise ValueError('自动加载列表过长')
    return text


def autoload_view(config, status, payloads):
    """What the page needs: ordered items, delays, last run outcome per item."""
    slots = {item.get('slot'): item for item in payloads
             if isinstance(item, dict) and item.get('slot')}
    raw = str(config.get('AUTOLOAD_LIST', '') or '')
    parsed = parse_autoload(raw, slots)
    status = status if isinstance(status, dict) else {}
    outcomes = str(status.get('outcomes') or '')
    labels = {'s': 'success', 'f': 'failed', 'k': 'skipped'}
    # Outcomes belong to the run that just happened; only trust them when the
    # list has not changed since (pldmgr echoes the list it ran).
    same_run = str(status.get('list') or '') == raw
    for index, item in enumerate(parsed['items']):
        code = outcomes[index] if same_run and index < len(outcomes) else ''
        item['outcome'] = labels.get(code)
    try:
        start_delay = int(config.get('AUTOLOAD_DELAY', 5))
    except (TypeError, ValueError):
        start_delay = 5
    return {'enabled': bool(config.get('AUTOLOAD_ENABLED')),
            'start_delay': start_delay, 'lead_ms': parsed['lead_ms'],
            'items': parsed['items'], 'raw': raw,
            # Kept for older pages: the payload keys in order.
            'list': [item['key'] for item in parsed['items']],
            'status': status}


class PldmgrClient:
    def __init__(self, ip, port=PLDMGR_PORT, timeout=6):
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.base = f'http://{ip}:{port}'

    def _request(self, path, data=None, query=None, mutating=False, raw=None):
        url = self.base + path
        if query:
            url += ('&' if '?' in url else '?') + urllib.parse.urlencode(query)
        body = raw
        headers = {'Accept': 'application/json, text/plain, */*'}
        if data is not None:
            body = json.dumps(data).encode()
            headers['Content-Type'] = 'application/json'
        if raw is not None:
            headers['Content-Type'] = 'application/octet-stream'
        if mutating:
            headers.update(MARKER)
        request = urllib.request.Request(url, data=body, headers=headers,
                                         method='POST' if mutating else 'GET')
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read(1 << 20)
                code = response.status
        except urllib.error.HTTPError as exc:
            detail = exc.read(200).decode('utf-8', 'replace')
            raise ConsoleError(f'HTTP {exc.code}: {detail[:160]}') from exc
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            raise ConsoleError(str(exc)) from exc
        if code >= 400:
            raise ConsoleError(f'HTTP {code}')
        text = payload.decode('utf-8', 'replace').strip()
        try:
            return json.loads(text) if text else {}
        except json.JSONDecodeError:
            return {'raw': text}

    # ---- reads ----
    def version(self):
        result = self._request('/version')
        return str(result.get('raw', result))[:40]

    def payloads(self):
        """Real consoles answer {self, items:[{slot,path,filename,...}]}."""
        result = self._request('/list_payloads') or {}
        if isinstance(result, list):
            return result
        items = result.get('items') if isinstance(result, dict) else None
        if isinstance(items, list):
            return items
        paths = (result.get('payloads') or []) if isinstance(result, dict) else []
        return [{'path': item, 'filename': str(item).rsplit('/', 1)[-1]}
                for item in paths if isinstance(item, str)]

    def config(self):
        return self._request('/get_config') or {}

    def autoload_status(self):
        return self._request('/autoload_status') or {}

    def processes(self):
        result = self._request('/processes_list')
        if isinstance(result, list):
            return result
        return result.get('processes', []) if isinstance(result, dict) else []

    def sources(self):
        result = self._request('/sources_list')
        return result.get('sources', []) if isinstance(result, dict) else []

    def repository_sources(self):
        return self._request('/repository_payloads') or {}

    # ---- mutations ----
    def add_source(self, url):
        return self._request('/sources_add', query={'url': url}, mutating=True)

    def remove_source(self, index):
        return self._request('/sources_remove', query={'index': index}, mutating=True)

    def refresh_repository(self):
        return self._request('/repository_refresh', data={}, mutating=True)

    def install(self, filename):
        return self._request('/repository_install', query={'filename': filename}, mutating=True)

    def upload_payload(self, filename, data):
        return self._request('/manage:upload', query={'filename': filename},
                             mutating=True, raw=data)

    def payload_path(self, filename):
        for item in self.payloads():
            if item.get('filename') == filename and item.get('path'):
                return item['path']
        return None

    def run(self, path):
        return self._request('/loadpayload:' + urllib.parse.quote(path, safe='/'), mutating=True)

    def kill(self, pid):
        return self._request('/process_kill', data={'pid': int(pid)}, mutating=True)

    def set_config(self, changes):
        return self._request('/set_config', data=changes, mutating=True)

    def set_autoload(self, enabled, sequence, start_delay=None):
        changes = {'AUTOLOAD_ENABLED': bool(enabled), 'AUTOLOAD_LIST': sequence}
        if start_delay is not None:
            changes['AUTOLOAD_DELAY'] = int(start_delay)
        return self.set_config(changes)

    def shutdown(self):
        return self._request('/shutdown', data={}, mutating=True)


class ElfldrClient:
    """elfldr accepts either raw ELF bytes or a URL to fetch and run."""

    def __init__(self, ip, port=ELF_PORT, timeout=10):
        self.ip = ip
        self.port = port
        self.timeout = timeout

    def run_url(self, url):
        query = urllib.parse.urlencode({'uri': url})
        request = urllib.request.Request(f'http://{self.ip}:{self.port}?{query}')
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read(4096)
            return True
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            raise ConsoleError(str(exc)) from exc

    def run_bytes(self, data):
        try:
            with socket.create_connection((self.ip, self.port), self.timeout) as sock:
                sock.sendall(data)
                sock.shutdown(socket.SHUT_WR)
                try:
                    sock.recv(256)
                except OSError:
                    pass
            return True
        except OSError as exc:
            raise ConsoleError(str(exc)) from exc


class HostManager:
    """Glue for the admin API: discovery + per-host console state."""

    def __init__(self, config, catalog, fetch_bytes=None):
        self.config = config
        self._catalog = catalog           # callable() -> {'name','items'}
        self.discovery = Discovery(config.subnet, elf_port=config.elf_port,
                                   pldmgr_port=config.pldmgr_port)
        self.pldmgr_port = config.pldmgr_port
        self.elf_port = config.elf_port
        self.manifest = {}                # filename -> url
        self.lock = threading.RLock()
        self.state = {}
        self.last_error = None
        self._fetch_bytes = fetch_bytes

    # -- helpers --
    def _client(self, ip):
        return PldmgrClient(ip, self.pldmgr_port)

    def validate_ip(self, ip):
        try:
            address = ipaddress.IPv4Address(str(ip))
        except ValueError as exc:
            raise ValueError('IP 地址不合法') from exc
        if address not in ipaddress.ip_network(self.config.subnet, strict=False):
            raise ValueError('只能管理本网段内的主机')
        return str(address)

    def _load_manifest(self):
        items = self._catalog()['items']
        self.manifest = {item['filename']: item['url'] for item in items
                         if isinstance(item, dict) and item.get('filename') and item.get('url')}
        return items

    def _payload_bytes(self, filename, limit=64 << 20):
        url = self.manifest[filename]
        request = urllib.request.Request(url, headers={'User-Agent': 'ps5-jb-server'})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read(limit + 1)
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            raise ConsoleError(f'读取本机插件失败：{exc}') from exc
        if len(data) > limit:
            raise ConsoleError('插件超过 64MB 上限')
        return data

    def _repo_url(self):
        return f'http://{self.config.host_ip}:{self.config.repo_port}/repo.json'

    # -- public --
    def scan(self, extra_ips=()):
        result = self.discovery.scan(extra_ips)
        with self.lock:
            for host in result['hosts']:
                self.state.setdefault(host['ip'], {})
        return self.overview()

    def overview(self, refresh=True):
        summary = self.discovery.snapshot()

        def build(host):
            entry = {'ip': host['ip'], 'online': True,
                     'elfldr': host['elfldr'], 'pldmgr': host['pldmgr'],
                     'loader': host.get('loader', False), 'version': None,
                     'pldmgr_port': self.pldmgr_port, 'elf_port': self.elf_port,
                     'source_attached': False, 'source_url': self._repo_url(),
                     'repo_payloads': None, 'installed': [], 'running': [],
                     'autoload': {'enabled': False, 'list': [], 'status': {}},
                     'last_error': None}
            if not refresh:
                return entry
            # A console that went to rest mode keeps its scan entry; probe the
            # ports first so a sleeping host costs a fraction of a second
            # instead of several multi-second HTTP timeouts per page poll.
            entry['pldmgr'] = _tcp_open(host['ip'], self.pldmgr_port, 0.6)
            entry['elfldr'] = _tcp_open(host['ip'], self.elf_port, 0.6)
            entry['online'] = entry['pldmgr'] or entry['elfldr']
            entry['power'] = self.power(host['ip'], entry['online'])
            if entry['pldmgr']:
                try:
                    self._populate(entry)
                except ConsoleError as exc:
                    entry['last_error'] = str(exc)[:200]
            return entry

        found = summary['hosts']
        if len(found) > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(found))) as pool:
                hosts = list(pool.map(build, found))
        else:
            hosts = [build(host) for host in found]
        return {'discovery': {**summary, 'subnet': str(self.discovery.subnet),
                              'enabled': self.config.ps5_control},
                'hosts': hosts}

    def _populate(self, entry):
        client = self._client(entry['ip'])
        entry['version'] = client.version()
        items = client.payloads()
        entry['installed'] = [
            str(item.get('filename') or str(item.get('path', '')).rsplit('/', 1)[-1])
            for item in items if isinstance(item, dict)]
        entry['payloads'] = [
            {'slot': str(item.get('slot') or ''), 'path': str(item.get('path') or ''),
             'filename': str(item.get('filename') or str(item.get('path', '')).rsplit('/', 1)[-1])}
            for item in items if isinstance(item, dict)]
        processes = client.processes()
        installed_names = set(entry['installed'])
        running = []
        for item in processes:
            name = str(item.get('name') or item.get('path') or '')
            short = name.rsplit('/', 1)[-1]
            if short in installed_names or short.endswith(('.elf', '.bin')):
                running.append({'pid': item.get('pid') or item.get('id'), 'name': short})
        entry['running'] = running
        entry['process_total'] = len(processes)
        config = client.config()
        entry['autoload'] = autoload_view(config, client.autoload_status(), items)
        sources = client.sources()
        url = self._repo_url()
        entry['source_attached'] = any(source.get('url') == url for source in sources)
        entry['sources'] = [{'name': source.get('name'), 'url': source.get('url')}
                            for source in sources]
        repository = client.repository_sources()
        sources = repository.get('sources', []) if isinstance(repository, dict) else []
        entry['repo_cached'] = False
        for source in sources:
            if source.get('url') == url:
                entry['repo_cached'] = True
                entry['repo_payloads'] = len(source.get('payloads') or [])
                entry['repo_error'] = bool(source.get('error'))

    def attach(self, ip):
        ip = self.validate_ip(ip)
        client = self._client(ip)
        url = self._repo_url()
        if not any(source.get('url') == url for source in client.sources()):
            client.add_source(url)
            try:
                client.refresh_repository()
            except ConsoleError:
                pass
        return self.host(ip)

    def host(self, ip):
        ip = self.validate_ip(ip)
        entry = {'ip': ip, 'elfldr': _tcp_open(ip, self.elf_port, 0.5),
                 'pldmgr': _tcp_open(ip, self.pldmgr_port, 0.5), 'version': None,
                 'pldmgr_port': self.pldmgr_port, 'elf_port': self.elf_port,
                 'source_attached': False, 'source_url': self._repo_url(),
                 'repo_payloads': None, 'installed': [], 'running': [],
                 'autoload': {'enabled': False, 'list': [], 'status': {}},
                 'last_error': None}
        entry['online'] = entry['pldmgr'] or entry['elfldr']
        entry['power'] = self.power(ip, entry['online'])
        if entry['pldmgr']:
            try:
                self._populate(entry)
            except ConsoleError as exc:
                entry['last_error'] = str(exc)[:200]
        return entry

    def power(self, ip, payload_online=False):
        """on / standby / off, plus name and firmware when the console answers DDP."""
        reply = ddp_search([ip], timeout=0.8).get(ip)
        if reply:
            return {**reply, 'payload': bool(payload_online)}
        state = 'on' if payload_online else 'off'
        return {'code': 0, 'state': state, 'name': '', 'host_id': '', 'firmware': '',
                'host_type': '', 'running_app': '', 'payload': bool(payload_online)}

    def install(self, ip, filename, run=False):
        ip = self.validate_ip(ip)
        self._load_manifest()
        if filename not in self.manifest:
            raise ValueError('插件不在本服务仓库中')
        data = self._fetch_bytes(filename) if self._fetch_bytes else self._payload_bytes(filename)
        try:
            client = self._client(ip)
            client.upload_payload(filename, data)
            path = client.payload_path(filename)
            if run:
                client.run(path or filename)
            return {'ok': True, 'installed': True, 'ran': bool(run), 'mode': 'pldmgr',
                    'path': path, 'host': self.host(ip)}
        except ConsoleError as exc:
            # elfldr cannot store a payload, it only runs it. Falling back is
            # only honest when the user asked to run.
            if not run:
                raise ConsoleError(f'pldmgr 安装失败：{exc}') from exc
            if not _tcp_open(ip, self.elf_port, 0.5):
                raise ConsoleError(f'pldmgr 上传失败且 elfldr 不可用：{exc}') from exc
            ElfldrClient(ip, self.elf_port).run_url(self.manifest[filename])
            return {'ok': True, 'installed': False, 'ran': True, 'mode': 'elfldr',
                    'host': self.host(ip)}

    def run(self, ip, filename):
        ip = self.validate_ip(ip)
        self._load_manifest()
        if filename not in self.manifest:
            raise ValueError('插件不在本服务仓库中')
        try:
            client = self._client(ip)
            path = client.payload_path(filename)
            if not path:
                raise ConsoleError('主机上还没有安装这个插件')
            client.run(path)
            return {'ok': True, 'mode': 'pldmgr'}
        except ConsoleError as exc:
            if not _tcp_open(ip, self.elf_port, 0.5):
                raise ConsoleError(f'运行失败：{exc}；elfldr 也不可用') from exc
            ElfldrClient(ip, self.elf_port).run_url(self.manifest[filename])
            return {'ok': True, 'mode': 'elfldr'}

    def kill(self, ip, pid):
        ip = self.validate_ip(ip)
        if isinstance(pid, bool) or not isinstance(pid, (int, str)) or not str(pid).isdigit():
            raise ValueError('进程号不合法')
        self._client(ip).kill(int(pid))
        return {'ok': True, 'host': self.host(ip)}

    def autoload(self, ip, enabled, items=None, start_delay=None, lead_ms=0, files=None):
        """Write the ordered sequence; items = [{key, delay_ms}] in load order."""
        ip = self.validate_ip(ip)
        if isinstance(items, list) and all(isinstance(item, str) for item in items):
            files, items = items, None
        if items is None:
            # Older pages sent bare keys with no delays.
            if not isinstance(files, list):
                raise ValueError('自动加载列表不合法')
            items = [{'key': name, 'delay_ms': 0} for name in files]
        sequence = serialize_autoload(items, lead_ms)
        if start_delay is not None:
            if isinstance(start_delay, bool) or not isinstance(start_delay, (int, float)) \
                    or not 0 <= start_delay <= MAX_START_DELAY:
                raise ValueError('启动倒计时必须在 0–3600 秒之间')
            start_delay = int(start_delay)
        self._client(ip).set_autoload(enabled, sequence, start_delay)
        return {'ok': True, 'sequence': sequence, 'host': self.host(ip)}

    def shutdown(self, ip):
        ip = self.validate_ip(ip)
        self._client(ip).shutdown()
        return {'ok': True}
