"""PS5 discovery and control (Payload Manager HTTP 8084 / elfldr TCP 9021).

Only plain HTTP is spoken, only to addresses inside the configured LAN, and
mutations carry the X-PLDMGR-Request marker that the console's web server uses
in place of an auth token.
"""
import concurrent.futures
import ipaddress
import hashlib
import json
import logging
import os
from pathlib import Path
import re
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

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


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


def _counted(data, progress, block=64 << 10):
    view = memoryview(data)
    progress(0, len(data))
    for start in range(0, len(data), block):
        chunk = view[start:start + block]
        yield bytes(chunk)
        progress(start + len(chunk), len(data))


# ---- installed plugin versions -------------------------------------------
# pldmgr keeps one local slot per plugin and drops version/platform suffixes
# when naming it: zftpd.elf and zftpd-ps5-v1.7.0.elf both live in local/zftpd.
# Its read-only /manage:check names the slot for a file name; the guess below
# is only for managers that predate that endpoint.
_PLATFORM = re.compile(r'(?<![a-z0-9])(?:ps[45]|prospero|orbis)(?![a-z0-9])')


def guess_slot_key(filename):
    stem = str(filename).rsplit('.', 1)[0].lower()
    match = re.match(r'(.+?)(?:[-_ .](?:ps[45]|v?\d)[\w.-]*)?$', stem)
    key = re.sub(r'[^a-z0-9._-]+', '-', match.group(1) if match else stem).strip('-.')
    return key or stem


def version_numbers(text):
    return tuple(int(part) for part in re.findall(r'\d+', str(text or '')))


def filename_version(filename, key):
    """Numbers in a payload file name after its slot name: zftpd-ps5-v1.6.0.elf -> (1, 6, 0)."""
    stem = str(filename).rsplit('.', 1)[0].lower()
    rest = stem[len(key):] if key and stem.startswith(key) else stem
    return version_numbers(_PLATFORM.sub(' ', rest))


def _version_text(numbers):
    return '.'.join(map(str, numbers))


def _version_key(numbers):
    """Comparable form: 1.7 and 1.7.0 are the same build, so drop trailing zeros."""
    numbers = list(numbers)
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    return tuple(numbers)


def compare_installed(item, installed, record=None, key=''):
    """How the repository build relates to the file in the console's slot.

    current  — same build (recorded checksum or identical file name)
    outdated — the repository has a newer build (versions, or the repository
               changed after this service installed the file)
    newer    — the console has a newer build than the repository
    maybe    — file names differ and no version can be read from them
    """
    filename = str(item.get('filename') or '')
    checksum = str(item.get('checksum') or '').lower()
    on_console = str(installed.get('filename') or '')
    known = bool(record) and record.get('filename') == on_console
    repo_numbers = version_numbers(item.get('version')) or filename_version(filename, key)
    if known and record.get('version'):
        console_numbers = version_numbers(record['version'])
    else:
        console_numbers = filename_version(on_console, key)
    if known and checksum and record.get('checksum') == checksum:
        state = 'current'
    elif repo_numbers and console_numbers and _version_key(repo_numbers) != _version_key(console_numbers):
        state = 'outdated' if _version_key(repo_numbers) > _version_key(console_numbers) else 'newer'
    elif known and checksum and record.get('checksum'):
        state = 'outdated'          # same or unknown version, but the repository changed since
    elif on_console == filename:
        state = 'current'
    elif repo_numbers and console_numbers:
        state = 'current'
    else:
        state = 'maybe'
    installed_version = (record.get('version') if known and record.get('version')
                         else _version_text(console_numbers))
    return {'state': state, 'confident': bool(known or (repo_numbers and console_numbers)),
            'installed': on_console, 'installed_version': str(installed_version or ''),
            'repo_version': str(item.get('version') or _version_text(repo_numbers))}


class PldmgrClient:
    def __init__(self, ip, port=PLDMGR_PORT, timeout=6):
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.base = f'http://{ip}:{port}'

    def _request(self, path, data=None, query=None, mutating=False, raw=None, progress=None):
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
            if progress is not None:
                # A sized iterable keeps Content-Length (pldmgr does not take
                # chunked bodies) while reporting how much has been sent.
                headers['Content-Length'] = str(len(raw))
                body = _counted(raw, progress)
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
            exc.close()
            raise ConsoleError(f'HTTP {exc.code}: {detail[:160]}', status=exc.code) from exc
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

    def slot_key(self, filename):
        """pldmgr's own slot name for a file; None when the manager has no resolver."""
        try:
            existing = self._request('/manage:check', query={'filename': filename})
        except ConsoleError as exc:
            if exc.status == 404:
                return None
            raise
        key = existing.get('key') if isinstance(existing, dict) else None
        if not isinstance(key, str) or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,126}', key) or '..' in key:
            raise ConsoleError('pldmgr 未返回有效的本地插件槽位')
        return key

    def upload_payload(self, filename, data, progress=None):
        # pldmgr strips version/platform suffixes when choosing a local slot:
        # zftpd.elf and zftpd-ps5-v1.7.0.elf both target local/zftpd. Its default
        # mode=new refuses an occupied slot with a generic HTTP 500. Ask its own
        # read-only resolver rather than duplicating version/name heuristics.
        mode = 'new'
        target_slot = None
        try:
            existing = self._request('/manage:check', query={'filename': filename})
        except ConsoleError as exc:
            if exc.status != 404:
                raise ConsoleError(f'无法检查主机上的插件槽位，未上传：{exc}', status=exc.status) from exc
            # Legacy servers may not implement the check endpoint. Never guess
            # replacement after a timeout, malformed reply or storage error.
        else:
            if (not isinstance(existing, dict) or existing.get('status') != 'ok'
                    or type(existing.get('folder_exists')) is not bool):
                raise ConsoleError('pldmgr 插件槽位检查失败或返回格式不正确，未上传')
            key = existing.get('key')
            if not isinstance(key, str) or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,126}', key) or '..' in key:
                raise ConsoleError('pldmgr 未返回有效的本地插件槽位，未上传')
            target_slot = 'local/' + key
            if existing['folder_exists']:
                mode = 'replace'
        result = self._request('/manage:upload', query={'filename': filename, 'mode': mode},
                               mutating=True, raw=data, progress=progress)
        return {'response': result, 'slot': target_slot, 'replaced': mode == 'replace'}

    def payload_path(self, filename, slot=None, local_only=False):
        matches = []
        for item in self.payloads():
            if not isinstance(item, dict) or item.get('filename') != filename or not item.get('path'):
                continue
            if slot is not None and item.get('slot') != slot:
                continue
            if local_only:
                # An older manager may lack /manage:check. Still never confirm
                # or run a repository copy as the result of a local upload.
                listed_slot = item.get('slot')
                if listed_slot:
                    if not str(listed_slot).startswith('local/'):
                        continue
                elif not str(item['path']).startswith('/data/pldmgr/payloads/local/'):
                    continue
            matches.append(item['path'])
        if local_only:
            return matches[0] if len(matches) == 1 else None
        return matches[0] if matches else None

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
        data_dir = getattr(config, 'data_dir', None)
        self.installs_path = Path(data_dir) / 'ps5-installs.json' if data_dir else None
        self.installs = self._read_installs()
        self.slot_keys = {}               # (ip, filename) -> pldmgr slot key
        self.legacy = set()               # hosts whose pldmgr lacks /manage:check
        self.jobs = {}                    # ip -> install / upgrade progress
        self.jobs_lock = threading.Lock()

    # -- install records and progress --
    def _read_installs(self):
        if not self.installs_path:
            return {}
        try:
            document = json.loads(self.installs_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        records = document.get('installs') if isinstance(document, dict) else None
        return {key: value for key, value in (records or {}).items()
                if isinstance(key, str) and isinstance(value, dict)}

    def _remember_install(self, ip, slot, filename, data, version):
        if not slot:
            return
        with self.lock:
            self.installs[f'{ip} {slot}'] = {
                'filename': filename, 'checksum': hashlib.sha256(bytes(data)).hexdigest(),
                'version': str(version or '')[:32], 'installed_at': time.time()}
            if not self.installs_path:
                return
            try:
                temporary = self.installs_path.with_name('.' + self.installs_path.name + '.tmp')
                temporary.write_text(json.dumps({'schema': 1, 'installs': self.installs},
                                                ensure_ascii=False) + '\n', encoding='utf-8')
                os.replace(temporary, self.installs_path)
            except OSError as exc:
                logging.warning('could not save PS5 install record: %s', exc)

    def _job(self, ip, **changes):
        with self.jobs_lock:
            if changes.get('reset'):
                self.jobs.pop(ip, None)
                return
            self.jobs[ip] = {**self.jobs.get(ip, {}), **changes, 'updated': time.time()}

    def progress(self, ip):
        ip = self.validate_ip(ip)
        with self.jobs_lock:
            job = self.jobs.get(ip)
            return dict(job) if job else None

    def _slot_key(self, ip, client, filename, deadline):
        cached = self.slot_keys.get((ip, filename))
        if cached:
            return cached
        if ip in self.legacy or time.monotonic() > deadline:
            return guess_slot_key(filename)
        try:
            key = client.slot_key(filename)
        except ConsoleError:
            return guess_slot_key(filename)
        if key is None:
            self.legacy.add(ip)
            return guess_slot_key(filename)
        if len(self.slot_keys) > 4096:
            self.slot_keys.clear()
        self.slot_keys[(ip, filename)] = key
        return key

    def _compare(self, ip, client, payloads, running):
        """{repo filename: comparison} for repository plugins present in a local slot."""
        local = {item['slot']: item for item in payloads if item.get('slot', '').startswith('local/')}
        if not local:
            return {}
        try:
            items = self._catalog()['items']
        except Exception:
            return {}
        pids = {item['name']: item.get('pid') for item in running}
        deadline = time.monotonic() + 3
        result = {}
        for item in items:
            filename = item.get('filename') if isinstance(item, dict) else None
            if not isinstance(filename, str) or not filename.lower().endswith(('.elf', '.bin')):
                continue
            key = self._slot_key(ip, client, filename, deadline)
            installed = local.get('local/' + key)
            if not installed:
                continue
            record = self.installs.get(f"{ip} {installed['slot']}")
            info = compare_installed(item, installed, record, key)
            info.update(slot=installed['slot'], running=pids.get(installed['filename']))
            result[filename] = info
        return result

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
        entry['updates'] = self._compare(entry['ip'], client, entry['payloads'], running)
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

    def install(self, ip, filename, run=False, keep_job=False, allow_elfldr=True):
        ip = self.validate_ip(ip)
        items = self._load_manifest()
        if filename not in self.manifest:
            raise ValueError('插件不在本服务仓库中')
        version = next((str(item.get('version') or '') for item in items
                        if isinstance(item, dict) and item.get('filename') == filename), '')
        try:
            return self._install(ip, filename, run, version, allow_elfldr)
        finally:
            if not keep_job:
                self._job(ip, reset=True)

    def _install(self, ip, filename, run, version, allow_elfldr=True):
        self._job(ip, filename=filename, stage='reading', done=0, total=None)
        data = self._fetch_bytes(filename) if self._fetch_bytes else self._payload_bytes(filename)
        uploaded = False
        try:
            client = self._client(ip)
            self._job(ip, stage='uploading', done=0, total=len(data))
            upload = client.upload_payload(
                filename, data, progress=lambda done, total: self._job(ip, done=done, total=total))
            uploaded = True
            self._job(ip, stage='verifying')
            path = client.payload_path(filename, slot=upload['slot'], local_only=True)
            if not path:
                raise ConsoleError('上传后未能确认新插件文件已保存，请刷新主机详情检查后重试')
            slot = upload['slot'] or next(
                (item.get('slot') for item in client.payloads()
                 if isinstance(item, dict) and item.get('path') == path), None)
            self._remember_install(ip, slot, filename, data, version)
            if run:
                self._job(ip, stage='running')
                client.run(path)
            return {'ok': True, 'installed': True, 'ran': bool(run), 'mode': 'pldmgr',
                    'path': path, 'host': self.host(ip)}
        except ConsoleError as exc:
            # elfldr cannot store a payload, it only runs it. Falling back is
            # only honest when the user asked to run, and never for an upgrade:
            # the old file would stay in the slot while we reported success.
            if not run or uploaded or exc.status is not None or not allow_elfldr:
                raise ConsoleError(f'pldmgr 安装或运行确认失败：{exc}') from exc
            if not _tcp_open(ip, self.elf_port, 0.5):
                raise ConsoleError(f'pldmgr 上传失败且 elfldr 不可用：{exc}') from exc
            ElfldrClient(ip, self.elf_port).run_url(self.manifest[filename])
            return {'ok': True, 'installed': False, 'ran': True, 'mode': 'elfldr',
                    'host': self.host(ip)}

    def upgrade(self, ip, filename, run=None):
        """Replace an older build on the console and make the new one take effect.

        A running old build is stopped and the new one started in its place;
        otherwise the file is replaced and started only when ``run`` is true.
        """
        ip = self.validate_ip(ip)
        self._job(ip, filename=filename, stage='checking', done=0, total=None)
        try:
            host = self.host(ip)
            if not host.get('pldmgr'):
                raise ConsoleError('更新需要主机运行 pldmgr（8084）')
            info = (host.get('updates') or {}).get(filename)
            if not info:
                raise ValueError('主机上没有找到这个插件的旧版本，请直接安装')
            pid = info.get('running')
            stopped = False
            if pid is not None and str(pid).isdigit():
                self._job(ip, stage='stopping', pid=int(pid))
                self._client(ip).kill(int(pid))
                stopped = True
                time.sleep(0.8)   # let pldmgr release the old process before replacing its file
            start = stopped if run is None else bool(run)
            try:
                result = self.install(ip, filename, run=start, keep_job=True, allow_elfldr=False)
            except ConsoleError as exc:
                if stopped:
                    raise ConsoleError(f'旧版本已停止，但新版本未能完成更新：{exc}。可在「运行中」确认后手动重新运行',
                                       status=exc.status) from exc
                raise
            result.update(upgraded=True, stopped=stopped, previous=info.get('installed'),
                          previous_version=info.get('installed_version'))
            return result
        finally:
            self._job(ip, reset=True)

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
