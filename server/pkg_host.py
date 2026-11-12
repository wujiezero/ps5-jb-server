"""Serve a PKG that lives on a NAS to the PS5 installer, without a local copy.

The PS5Upload engine accepts {"url": ...} as an install source; the console's
installer (or, as a fallback, the engine's own proxy) then reads the package
with HTTP Range requests. This module is that URL:

    http://<host_ip>:<pkg_port>/p/<token>.pkg

- One random token per install job, valid only while the job runs (and never
  longer than MAX_AGE); revoked as soon as the job ends.
- Only the target PS5 and this host itself (the engine probes and may proxy)
  may read it; everyone else gets 403, unknown tokens 404.
- Read only: GET and HEAD, single byte ranges, nothing else. Every request
  opens its own NAS reader at the requested offset and streams 1 MiB chunks.
- Strong ETag and Last-Modified, because the engine and the console check
  that the file does not change between ranges.

The listener starts on first use and stays up; with no published token it
answers 404 to everything.
"""
import email.utils
import logging
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHUNK = 1 << 20
MAX_AGE = 24 * 3600
MAX_CONNECTIONS = 16
_PATH = re.compile(r'/p/([A-Za-z0-9_-]{16,64})\.pkg\Z')
_RANGE = re.compile(r'bytes=(\d*)-(\d*)\Z')


class PkgHostError(RuntimeError):
    pass


def parse_range(header, size):
    """(start, end) inclusive for one Range header, None for the whole file.

    Raises ValueError for anything unsatisfiable (answered with 416).
    """
    if not header:
        return None
    match = _RANGE.fullmatch(header.strip())
    if not match or (not match.group(1) and not match.group(2)):
        raise ValueError('unsupported range')
    first, last = match.groups()
    if not first:                                  # suffix: the last N bytes
        length = int(last)
        if length <= 0:
            raise ValueError('empty suffix range')
        return max(0, size - length), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size or end < start:
        raise ValueError('range outside the file')
    return start, min(end, size - 1)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, host):
        self.host = host
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.close()
            finally:
                return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'ps5-jb-pkg'
    sys_version = ''
    timeout = 30

    def log_message(self, fmt, *args):
        logging.getLogger('pkg-host').debug('%s %s', self.client_address[0], fmt % args)

    def _empty(self, status, extra=None):
        self.send_response(status)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.send_header('Content-Length', '0')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True

    def do_HEAD(self):
        self._serve(head=True)

    def do_GET(self):
        self._serve(head=False)

    def do_POST(self):
        self._empty(405, {'Allow': 'GET, HEAD'})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_POST

    def _serve(self, head):
        host = self.server.host
        match = _PATH.fullmatch(self.path.split('?', 1)[0])
        entry = host.lookup(match.group(1)) if match else None
        if entry is None:
            self._empty(404)
            return
        if self.client_address[0] not in entry['allowed']:
            logging.warning('pkg-host: refused %s for job %s', self.client_address[0], entry['job'])
            self._empty(403)
            return
        size = entry['size']
        etag = f'"{size:x}-{int(entry["mtime"]):x}"'
        headers = {'Accept-Ranges': 'bytes', 'ETag': etag, 'Content-Type': 'application/octet-stream',
                   'Last-Modified': email.utils.formatdate(entry['mtime'] or entry['published'], usegmt=True),
                   'Cache-Control': 'no-store'}
        try:
            wanted = parse_range(self.headers.get('Range'), size)
        except ValueError:
            self._empty(416, {'Content-Range': f'bytes */{size}'})
            return
        if wanted is not None and self.headers.get('If-Range') not in (None, etag):
            wanted = None                              # a different version was asked for: whole file
        start, end = wanted if wanted is not None else (0, size - 1)
        length = end - start + 1 if size else 0
        reader = client = None
        if not head and length:
            try:
                client = host.client_factory(host.store.get(entry['nas_id']))
                reader = client.open_at(entry['path'], start) if start else client.open(entry['path'])
                if reader is None:
                    raise PkgHostError('NAS 不支持从中间读取（WebDAV 未返回 206）')
            except Exception as exc:
                logging.warning('pkg-host: NAS read for job %s failed: %s', entry['job'], exc)
                if client is not None:
                    client.close()
                self._empty(502)
                return
        self.send_response(206 if wanted is not None else 200)
        for key, value in headers.items():
            self.send_header(key, value)
        if wanted is not None:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.send_header('Content-Length', str(length))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        if head or not length:
            return
        remaining = length
        try:
            while remaining:
                chunk = reader.read(min(CHUNK, remaining))
                if not chunk:
                    logging.warning('pkg-host: NAS data ended early for job %s', entry['job'])
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
                host.count(match.group(1), len(chunk))
        except (OSError, ValueError) as exc:
            logging.info('pkg-host: transfer for job %s stopped: %s', entry['job'], exc)
        finally:
            try:
                reader.close()
            finally:
                client.close()


class PkgHost:
    def __init__(self, bind_ip, port, store, client_factory, public_ip=None, clock=time.time):
        self.bind_ip, self.port = bind_ip, int(port)
        self.public_ip = public_ip or bind_ip
        self.store, self.client_factory, self.clock = store, client_factory, clock
        self.lock = threading.Lock()
        self.tokens = {}
        self.server = None

    @property
    def enabled(self):
        return self.port > 0

    def start(self):
        with self.lock:
            if self.server is not None:
                return
            if not self.enabled:
                raise PkgHostError('PKG 安装已关闭（[ps5] pkg_port = 0）')
            try:
                self.server = _Server((self.bind_ip, self.port), _Handler, self)
            except OSError as exc:
                raise PkgHostError(f'无法监听 PKG 传输端口 {self.port}：{exc}') from exc
            threading.Thread(target=self.server.serve_forever, name='pkg-host', daemon=True).start()
            logging.info('pkg-host listening on %s:%d', self.bind_ip, self.port)

    def stop(self):
        with self.lock:
            server, self.server = self.server, None
            self.tokens.clear()
        if server is not None:
            server.shutdown()
            server.server_close()

    def publish(self, job, nas_id, path, size, mtime, allowed):
        """Make one NAS file readable; returns (token, url)."""
        self.start()
        token = secrets.token_urlsafe(18)
        with self.lock:
            self._expire()
            self.tokens[token] = {'job': job, 'nas_id': nas_id, 'path': path, 'size': int(size),
                                  'mtime': float(mtime or 0), 'published': self.clock(),
                                  'allowed': set(allowed) | {'127.0.0.1', self.public_ip}, 'served': 0}
        return token, f'http://{self.public_ip}:{self.port}/p/{token}.pkg'

    def revoke(self, token):
        with self.lock:
            entry = self.tokens.pop(token, None)
        return entry['served'] if entry else 0

    def lookup(self, token):
        with self.lock:
            self._expire()
            entry = self.tokens.get(token)
            return dict(entry) if entry else None

    def count(self, token, size):
        with self.lock:
            if token in self.tokens:
                self.tokens[token]['served'] += size

    def served(self, token):
        with self.lock:
            entry = self.tokens.get(token)
            return entry['served'] if entry else 0

    def _expire(self):
        now = self.clock()
        for token in [t for t, e in self.tokens.items() if now - e['published'] > MAX_AGE]:
            del self.tokens[token]
