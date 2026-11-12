"""Proxy for the PS5 file service (tom-ps5-usbnet payload's /fs/* HTTP API).

The browser cannot call the console directly (CSP keeps the admin page
same-origin), so every request is streamed through this module. Only addresses
inside the configured LAN may be proxied.
"""
import ipaddress
import json
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

FILE_PORT = 80
FTP_PORT = 2120
CHUNK = 64 * 1024
BACKEND_TTL = 60
VOLUME_PATTERN = re.compile(r'^/(data|mnt/(ext|usb)\d+)(?:/|$)')


class FileServiceError(RuntimeError):
    """The file service failed; status keeps the console's HTTP code when it sent one."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


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
    # Large writes are flushed to disk before the console answers.
    UPLOAD_TIMEOUT = 300

    def __init__(self, subnet, host='', port=FILE_PORT, timeout=10, ftp_port=FTP_PORT,
                 space_port=8888):
        self.subnet = ipaddress.ip_network(subnet, strict=False)
        self.host = host
        self.port = port
        self.ftp_port = ftp_port
        self.space_port = space_port
        self.timeout = timeout
        self.last_error = None
        # Which service answers on the console: 'http' (tom-ps5-usbnet /fs/*)
        # or 'ftp' (zftpd and friends). Detected lazily, re-checked on failure.
        self.backend = None
        self.backend_at = 0.0
        self.backend_lock = threading.Lock()
        self._ftp = None

    # -- backend selection --
    def _ftp_files(self):
        from ftp_files import FtpFiles
        if self._ftp is None or self._ftp.host != self.host or self._ftp.port != self.ftp_port:
            self._ftp = FtpFiles(self.host, self.ftp_port, error=FileServiceError,
                                 space_port=self.space_port)
        return self._ftp

    def _active(self):
        """Return the FTP backend when it is the one in use, else None (HTTP)."""
        self.base()     # raises when no host is chosen
        with self.backend_lock:
            if self.backend and time.monotonic() - self.backend_at < BACKEND_TTL:
                return self._ftp_files() if self.backend == 'ftp' else None
            try:
                self._json('/fs/volumes', timeout=3)
                self.backend = 'http'
            except FileServiceError as http_error:
                if not self.ftp_port:
                    raise
                try:
                    self._ftp_files().probe()
                except FileServiceError:
                    self.backend = None
                    raise FileServiceError(
                        f'PS5（{self.host}）上没有可用的文件服务：HTTP 端口 {self.port} 和 '
                        f'FTP 端口 {self.ftp_port} 都无法连接。请在 PS5 上运行 zftpd（FTP）'
                        f'或 tom-ps5-usbnet 后重试。') from http_error
                if self.backend != 'ftp':
                    logging.info('file service on %s: using FTP port %d', self.host, self.ftp_port)
                self.backend = 'ftp'
            self.backend_at = time.monotonic()
            return self._ftp_files() if self.backend == 'ftp' else None

    def _call(self, name, *args, **kwargs):
        ftp = self._active()
        try:
            if ftp is not None:
                return getattr(ftp, name)(*args, **kwargs)
            return getattr(self, '_http_' + name)(*args, **kwargs)
        except FileServiceError as exc:
            # A connection failure may mean the console switched services:
            # detect again on the next call instead of failing for a minute.
            if exc.status is None:
                self.backend = None
            raise

    # -- addressing --
    def configure(self, host):
        host = str(ipaddress.IPv4Address(str(host)))
        if ipaddress.IPv4Address(host) not in self.subnet:
            raise ValueError('只能访问本网段内的主机')
        self.host = host
        self.last_error = None
        self.backend = None
        return self.info()

    def base(self):
        if not self.host:
            raise FileServiceError('还没有选择 PS5 主机')
        return f'http://{self.host}:{self.port}'

    def info(self):
        result = {'configured': bool(self.host), 'base': f'http://{self.host}:{self.port}' if self.host else '',
                  'host': self.host or None, 'reachable': False, 'error': None, 'backend': None}
        if not self.host:
            return result
        try:
            self.volumes()
            result['reachable'] = True
            result['backend'] = self.backend
            if self.backend == 'ftp':
                result['base'] = f'ftp://{self.host}:{self.ftp_port}'
        except FileServiceError as exc:
            result['error'] = str(exc)[:300]
        return result

    # -- low level --
    def _open(self, path, query=None, data=None, method='GET', headers=None, timeout=None):
        url = self.base() + path
        if query:
            url += ('&' if '?' in url else '?') + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, data=data, method=method,
                                         headers={'Accept': 'application/json', **(headers or {})})
        try:
            return urllib.request.urlopen(request, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read(300).decode('utf-8', 'replace')
            message = _error_text(detail) or f'HTTP {exc.code}'
            raise FileServiceError(f'PS5 文件服务拒绝了请求（HTTP {exc.code}）：{message[:200]}',
                                   status=exc.code) from exc
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            raise FileServiceError(f'无法连接 PS5 文件服务（{self.host}）：{exc}') from exc

    def _json(self, path, query=None, data=None, method='GET', timeout=None):
        with self._open(path, query, (json.dumps(data).encode() if data is not None else None), method,
                        {'Content-Type': 'application/json'} if data is not None else None,
                        timeout=timeout) as response:
            body = response.read(4 << 20)
        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise FileServiceError('PS5 文件服务返回了非 JSON 内容') from exc

    # -- public operations (validated here, executed by the active backend) --
    def volumes(self):
        return self._call('volumes')

    def list(self, path):
        _check_path(path, allow_volume=True)
        return self._call('list', path)

    def folder_size(self, path):
        _check_path(path, allow_volume=True)
        return self._call('folder_size', path)

    def job(self):
        return self._call('job')

    def transfers(self):
        return self._call('transfers')

    def stream(self, path):
        _check_path(path, allow_volume=True)
        return self._call('stream', path)

    def stream_folder(self, path):
        _check_path(path, allow_volume=True)
        return self._call('stream_folder', path)

    def mkdir(self, path):
        _check_path(path, allow_root=False)
        return self._call('mkdir', path)

    def rename(self, source, target):
        _check_path(source, allow_root=False)
        _check_path(target, allow_root=False)
        return self._call('rename', source, target)

    def delete(self, path):
        _check_path(path, allow_root=False)
        return self._call('delete', path)

    def copy(self, source, target, move=False):
        _check_path(source, allow_root=not move)
        _check_path(target, allow_root=False)
        return self._call('copy', source, target, move)

    def cancel(self):
        return self._call('cancel')

    def bench(self, path):
        _check_path(path, allow_volume=True)
        return self._call('bench', path)

    def supports_resume(self):
        """Whether an upload can continue at an offset (FTP with REST STREAM; /fs/put cannot)."""
        ftp = self._active()
        return ftp is not None and ftp.supports_resume()

    def file_size(self, path):
        """Size of a file on the console, or None when there is no such file."""
        _check_path(path, allow_root=False)
        parent, name = path.rstrip('/').rsplit('/', 1)
        for entry in self.list(parent or '/').get('entries', []):
            if entry.get('name') == name and not entry.get('dir'):
                return int(entry.get('size') or 0)
        return None

    def upload(self, directory, filename, chunks, length, overwrite=False,
               client='ps5-jb-server', offset=0):
        _check_path(directory, allow_volume=True)
        if not filename or '/' in filename or filename in ('.', '..'):
            raise ValueError('文件名不合法')
        target = _join(directory, filename)
        _check_path(target, allow_root=False)
        ftp = self._active()
        if ftp is not None:
            if offset:
                return ftp.upload(target, chunks, length, overwrite=overwrite, offset=offset)
            return ftp.upload(target, chunks, length, overwrite=overwrite)
        if offset:
            raise FileServiceError('PS5 的 HTTP 文件服务不支持续传')
        return self._http_upload(target, chunks, length, overwrite, client)

    # -- HTTP backend (tom-ps5-usbnet /fs/*) --
    def _http_volumes(self):
        return self._json('/fs/volumes')

    def _http_list(self, path):
        return normalize_entries(self._json('/fs/list', {'path': path}))

    def _http_folder_size(self, path):
        return self._json('/fs/size', {'path': path})

    def _http_job(self):
        return self._json('/fs/job')

    def _http_transfers(self):
        return self._json('/fs/transfers')

    def _http_stream(self, path):
        return self._open('/fs/get', {'path': path})

    def _http_stream_folder(self, path):
        return self._open('/fs/tar', {'path': path})

    def _http_mkdir(self, path):
        return self._json('/fs/mkdir', {'path': path}, {}, 'POST')

    def _http_rename(self, source, target):
        return self._json('/fs/rename', {'from': source, 'to': target}, {}, 'POST')

    def _http_delete(self, path):
        return self._json('/fs/delete', {'path': path}, {}, 'POST')

    def _http_copy(self, source, target, move=False):
        return self._json('/fs/move' if move else '/fs/copy',
                          {'from': source, 'to': target}, {}, 'POST')

    def _http_cancel(self):
        return self._json('/fs/cancel', None, {}, 'POST')

    def _http_bench(self, path):
        return self._json('/fs/bench', {'path': path}, {}, 'POST')

    def _http_upload(self, target, chunks, length, overwrite, client):
        query = {'path': target, 'client': client}
        if overwrite:
            query['overwrite'] = '1'
        body = _ChunkReader(chunks, int(length))
        with self._open('/fs/put', query, body, 'POST',
                        {'Content-Type': 'application/octet-stream',
                         'Content-Length': str(body.length)},
                        timeout=self.UPLOAD_TIMEOUT):
            pass
        if body.sent != body.length:
            raise ValueError('上传中断：浏览器发送的数据不完整')
        return {'ok': True, 'path': target}


def _error_text(detail):
    """Pull a readable message out of the console's JSON or text error body."""
    detail = (detail or '').strip()
    try:
        parsed = json.loads(detail)
    except ValueError:
        return detail
    if isinstance(parsed, dict):
        for key in ('error', 'message', 'detail'):
            if isinstance(parsed.get(key), str):
                return parsed[key]
    return detail


def _check_path(path, allow_volume=True, allow_root=True):
    if not isinstance(path, str) or not path.startswith('/'):
        raise ValueError('路径必须以 / 开头')
    if any(part in ('..', '.') for part in path.split('/')):
        raise ValueError('路径不允许包含 . 或 ..')
    if len(path) > 4096:
        raise ValueError('路径过长')
    if '\x00' in path:
        raise ValueError('路径包含非法字符')
    match = VOLUME_PATTERN.match(path)
    if allow_volume and not match:
        raise ValueError('只允许访问 /data、/mnt/ext*、/mnt/usb*')
    if not allow_root and match and path.rstrip('/') == match.group(0).rstrip('/'):
        raise ValueError('不能修改存储卷根目录本身')
    return path


class _ChunkReader:
    """urllib body adapter: streams request chunks without buffering the file."""

    def __init__(self, chunks, length):
        self._iterator = iter(chunks)
        self._buffer = b''
        self.length = length
        self.sent = 0

    def read(self, size=-1):
        size = CHUNK if size is None or size < 0 else size
        while len(self._buffer) < size:
            try:
                self._buffer += next(self._iterator)
            except StopIteration:
                break
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        self.sent += len(data)
        return data
