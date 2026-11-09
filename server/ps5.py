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

    def remember(self, ip):
        """Prefer addresses we already saw (activity page, manual add)."""
        with self.lock:
            if ip not in self.extra:
                self.extra.insert(0, ip)

    def scan(self, extra_ips=()):
        started = time.monotonic()
        addresses = []
        for ip in list(extra_ips) + list(self.extra):
            try:
                address = str(ipaddress.IPv4Address(ip))
            except ValueError:
                continue
            if address in self.subnet and address not in addresses:
                addresses.append(address)
        addresses += [str(host) for host in self.subnet.hosts() if str(host) not in addresses]

        def probe(ip):
            return ip, {name for name, port in self.ports if _tcp_open(ip, port, self.timeout)}

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

        found = [{'ip': ip, 'elfldr': 'elfldr' in ports, 'pldmgr': 'pldmgr' in ports,
                  'loader': 'loader' in ports}
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
            return json.loads(json.dumps(self.cached))


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

    def set_autoload(self, enabled, files):
        return self.set_config({'AUTOLOAD_ENABLED': bool(enabled),
                                'AUTOLOAD_LIST': ','.join(files)})

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
        self.manifest = {item['filename']: item['url'] for item in items}
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
        hosts = []
        for host in summary['hosts']:
            entry = {'ip': host['ip'], 'online': True,
                     'elfldr': host['elfldr'], 'pldmgr': host['pldmgr'],
                     'loader': host.get('loader', False), 'version': None,
                     'source_attached': False, 'source_url': self._repo_url(),
                     'repo_payloads': None, 'installed': [], 'running': [],
                     'autoload': {'enabled': False, 'list': [], 'status': {}},
                     'last_error': None}
            if host['pldmgr'] and refresh:
                try:
                    self._populate(entry)
                except ConsoleError as exc:
                    entry['last_error'] = str(exc)[:200]
            hosts.append(entry)
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
        entry['autoload'] = {'enabled': bool(config.get('AUTOLOAD_ENABLED')),
                             'list': [f for f in str(config.get('AUTOLOAD_LIST', '')).split(',') if f],
                             'status': client.autoload_status()}
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
        entry = {'ip': ip, 'online': True, 'elfldr': _tcp_open(ip, self.elf_port, 0.5),
                 'pldmgr': _tcp_open(ip, self.pldmgr_port, 0.5), 'version': None,
                 'source_attached': False, 'source_url': self._repo_url(),
                 'repo_payloads': None, 'installed': [], 'running': [],
                 'autoload': {'enabled': False, 'list': [], 'status': {}},
                 'last_error': None}
        if entry['pldmgr']:
            try:
                self._populate(entry)
            except ConsoleError as exc:
                entry['last_error'] = str(exc)[:200]
        return entry

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
            client.run(client.payload_path(filename) or filename)
            return {'ok': True, 'mode': 'pldmgr'}
        except ConsoleError:
            ElfldrClient(ip, self.elf_port).run_url(self.manifest[filename])
            return {'ok': True, 'mode': 'elfldr'}

    def kill(self, ip, pid):
        ip = self.validate_ip(ip)
        self._client(ip).kill(pid)
        return {'ok': True, 'host': self.host(ip)}

    def autoload(self, ip, enabled, files):
        ip = self.validate_ip(ip)
        if not isinstance(files, list) or len(files) > 64:
            raise ValueError('自动加载列表不合法')
        cleaned = []
        for name in files:
            if not isinstance(name, str) or not name or '/' in name or len(name) > 128:
                raise ValueError('自动加载文件名不合法')
            cleaned.append(name)
        self._client(ip).set_autoload(enabled, cleaned)
        return {'ok': True, 'host': self.host(ip)}

    def shutdown(self, ip):
        ip = self.validate_ip(ip)
        self._client(ip).shutdown()
        return {'ok': True}
