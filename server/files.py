"""Proxy for the PS5 file service (tom-ps5-usbnet payload's /fs/* HTTP API).

The browser cannot call the console directly (CSP keeps the admin page
same-origin), so every request is streamed through this module. Only addresses
inside the configured LAN may be proxied.
"""
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

FILE_PORT = 80
CHUNK = 64 * 1024
VOLUME_PATTERN = re.compile(r'^/(data|mnt/(ext|usb)\d+)(?:/|$)')


class FileServiceError(RuntimeError):
    pass


def normalize_entries(payload):
    """Add the derived fields the UI needs, keep the console's own fields."""
    entries = []
    for item in payload.get('entries', []) if isinstance(payload, dict) else []:
        kind = item.get('kind', 'file')
        entries.append({
            'name': item.get('name', ''),
            'path': item.get('path') or _join(payload.get('path', ''), item.get('name', '')),
            'kind': kind,
            'dir': kind == 'folder',
            'size': int(item.get('size') or 0),
            'mtime': int(item.get('mtime') or 0),
        })
    return {'path': payload.get('path', ''), 'entries': entries,
            'truncated': bool(payload.get('truncated'))}


def _join(parent, name):
    if not parent:
        return name
    return parent.rstrip('/') + '/' + name


class FileProxy:
    def __init__(self, subnet, host='', port=FILE_PORT, timeout=10):
        self.subnet = ipaddress.ip_network(subnet, strict=False)
        self.host = host
        self.port = port
        self.timeout = timeout
        self.last_error = None

    # -- addressing --
    def configure(self, host):
        host = str(ipaddress.IPv4Address(str(host)))
        if ipaddress.IPv4Address(host) not in self.subnet:
            raise ValueError('只能访问本网段内的主机')
        self.host = host
        self.last_error = None
        return self.info()

    def base(self):
        if not self.host:
            raise FileServiceError('还没有选择 PS5 主机')
        return f'http://{self.host}:{self.port}'

    def info(self):
        result = {'configured': bool(self.host), 'base': f'http://{self.host}:{self.port}' if self.host else '',
                  'host': self.host or None, 'reachable': False, 'error': None}
        if not self.host:
            return result
        try:
            self.volumes()
            result['reachable'] = True
        except FileServiceError as exc:
            result['error'] = str(exc)[:200]
        return result

    # -- low level --
    def _open(self, path, query=None, data=None, method='GET', headers=None):
        url = self.base() + path
        if query:
            url += ('&' if '?' in url else '?') + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, data=data, method=method,
                                         headers={'Accept': 'application/json', **(headers or {})})
        try:
            return urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read(300).decode('utf-8', 'replace')
            raise FileServiceError(f'HTTP {exc.code}: {detail[:200]}') from exc
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            raise FileServiceError(f'无法连接 PS5 文件服务（{self.host}）：{exc}') from exc

    def _json(self, path, query=None, data=None, method='GET'):
        with self._open(path, query, (json.dumps(data).encode() if data is not None else None), method,
                        {'Content-Type': 'application/json'} if data is not None else None) as response:
            body = response.read(4 << 20)
        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise FileServiceError('PS5 文件服务返回了非 JSON 内容') from exc

    # -- reads --
    def volumes(self):
        return self._json('/fs/volumes')

    def list(self, path):
        _check_path(path, allow_volume=True)
        return normalize_entries(self._json('/fs/list', {'path': path}))

    def folder_size(self, path):
        _check_path(path, allow_volume=True)
        return self._json('/fs/size', {'path': path})

    def job(self):
        return self._json('/fs/job')

    def transfers(self):
        return self._json('/fs/transfers')

    def stream(self, path):
        _check_path(path, allow_volume=True)
        return self._open('/fs/get', {'path': path})

    def stream_folder(self, path):
        _check_path(path, allow_volume=True)
        return self._open('/fs/tar', {'path': path})

    # -- mutations --
    def mkdir(self, path):
        _check_path(path)
        return self._json('/fs/mkdir', {'path': path}, {}, 'POST')

    def rename(self, source, target):
        _check_path(source)
        _check_path(target)
        return self._json('/fs/rename', {'from': source, 'to': target}, {}, 'POST')

    def delete(self, path):
        _check_path(path)
        return self._json('/fs/delete', {'path': path}, {}, 'POST')

    def copy(self, source, target, move=False):
        _check_path(source)
        _check_path(target)
        return self._json('/fs/move' if move else '/fs/copy',
                          {'from': source, 'to': target}, {}, 'POST')

    def cancel(self):
        return self._json('/fs/cancel', None, {}, 'POST')

    def bench(self, path):
        _check_path(path, allow_volume=True)
        return self._json('/fs/bench', {'path': path}, {}, 'POST')

    def upload(self, directory, filename, chunks, length, overwrite=False,
               client='ps5-jb-server'):
        _check_path(directory, allow_volume=True)
        if not filename or '/' in filename or filename in ('.', '..'):
            raise ValueError('文件名不合法')
        target = _join(directory, filename)
        _check_path(target)
        query = {'path': target, 'client': client}
        if overwrite:
            query['overwrite'] = '1'
        body = _ChunkReader(chunks, int(length))
        with self._open('/fs/put', query, body, 'POST',
                        {'Content-Type': 'application/octet-stream',
                         'Content-Length': str(body.length)}):
            return {'ok': True, 'path': target}


def _check_path(path, allow_volume=True):
    if not isinstance(path, str) or not path.startswith('/'):
        raise ValueError('路径必须以 / 开头')
    if any(part in ('..', '.') for part in path.split('/')):
        raise ValueError('路径不允许包含 . 或 ..')
    if len(path) > 4096:
        raise ValueError('路径过长')
    if allow_volume and not VOLUME_PATTERN.match(path):
        raise ValueError('只允许访问 /data、/mnt/ext*、/mnt/usb*')
    return path


class _ChunkReader:
    """urllib body adapter: streams request chunks without buffering the file."""

    def __init__(self, chunks, length):
        self._iterator = iter(chunks)
        self._buffer = b''
        self.length = length

    def read(self, size=-1):
        size = CHUNK if size is None or size < 0 else size
        while len(self._buffer) < size:
            try:
                self._buffer += next(self._iterator)
            except StopIteration:
                break
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data
