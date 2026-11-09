"""Console-facing API: PS5 host control (/api/ps5*) and file manager (/api/files*).

Kept apart from server.py so the routing table there stays readable; the admin
handler only forwards paths that start with /api/ps5 or /api/files.
"""
import ipaddress
import json
import logging
import os
import urllib.parse

from files import FileServiceError, FileProxy
from ps5 import ConsoleError, HostManager

MAX_JSON = 32 * 1024
CHUNK = 64 * 1024


class ConsoleAPI:
    def __init__(self, settings, catalog):
        self.settings = settings
        self.ps5 = HostManager(settings, catalog)
        self.files = FileProxy(settings.subnet, port=settings.file_port)
        self.state_path = settings.data_dir / 'console.json'
        self._restore()
        self.enabled = settings.ps5_control
        self.file_manager = settings.file_manager

    def _restore(self):
        try:
            saved = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return
        host = saved.get('files_host')
        if host:
            try:
                self.files.host = str(ipaddress.IPv4Address(host))
            except ValueError:
                pass

    def _remember(self):
        payload = json.dumps({'files_host': self.files.host}).encode()
        tmp = self.state_path.with_suffix('.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload)
        os.replace(tmp, self.state_path)

    # ---------- helpers ----------
    def _json_body(self, handler):
        if handler.headers.get('Transfer-Encoding'):
            raise ValueError('不支持分块请求')
        length = int(handler.headers.get('Content-Length', '0') or 0)
        if not 0 < length <= MAX_JSON:
            raise ValueError('请求大小必须在 1–32768 字节之间')
        body = json.loads(handler.rfile.read(length) or b'{}')
        if not isinstance(body, dict):
            raise ValueError('请求必须是 JSON 对象')
        return body

    def _query(self, handler):
        return urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)

    def _arg(self, handler, key, default=''):
        return (self._query(handler).get(key) or [default])[0]

    def _fail(self, handler, exc):
        if isinstance(exc, (ValueError, KeyError)):
            handler.send_body(400, {'error': str(exc)})
        elif isinstance(exc, (ConsoleError, FileServiceError)):
            handler.send_body(502, {'error': str(exc)})
        else:
            logging.exception('console api failed')
            handler.send_body(500, {'error': '操作失败，请检查服务日志'})

    # ---------- GET ----------
    def handle_get(self, handler):
        path = urllib.parse.urlsplit(handler.path).path
        if not handler.require_session():
            return True
        try:
            if path == '/api/ps5':
                if not self.enabled:
                    handler.send_body(200, {'hosts': [], 'discovery': {'enabled': False}})
                    return True
                handler.send_body(200, self.ps5.overview())
                return True
            if path == '/api/files/info':
                handler.send_body(200, {**self.files.info(), 'hosts': self._known_hosts()})
                return True
            if path == '/api/files/download':
                return self._stream(handler, lambda: self.files.stream(self._arg(handler, 'path')),
                                    'download.bin', 'application/octet-stream')
            if path == '/api/files/tar':
                return self._stream(handler, lambda: self.files.stream_folder(self._arg(handler, 'path')),
                                    'folder.tar', 'application/x-tar')
            if path == '/api/files/volumes':
                handler.send_body(200, self.files.volumes())
                return True
            if path == '/api/files/list':
                handler.send_body(200, self.files.list(self._arg(handler, 'path', '/data')))
                return True
            if path == '/api/files/size':
                handler.send_body(200, self.files.folder_size(self._arg(handler, 'path', '/data')))
                return True
            if path == '/api/files/job':
                handler.send_body(200, self.files.job())
                return True
            if path == '/api/files/transfers':
                handler.send_body(200, self.files.transfers())
                return True
            return False
        except (ValueError, ConsoleError, FileServiceError) as exc:
            self._fail(handler, exc)
            return True

    def _stream(self, handler, opener, filename, ctype):
        try:
            response = opener()
        except (ValueError, FileServiceError) as exc:
            self._fail(handler, exc)
            return True
        length = response.headers.get('Content-Length')
        handler.send_response(200)
        handler.send_header('Content-Type', ctype)
        handler.send_header('Content-Disposition',
                            f'attachment; filename="{filename}"')
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        if length and length.isdigit():
            handler.send_header('Content-Length', length)
        handler.end_headers()
        try:
            with response:
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
        except OSError:
            logging.info('download interrupted by client')
        return True

    def _known_hosts(self):
        return [host['ip'] for host in self.ps5.discovery.snapshot()['hosts']]

    # ---------- POST ----------
    def handle_post(self, handler, route):
        path = route.rstrip('/')
        if path == '/api/files/upload':
            return self._upload(handler)
        if not handler.require_session(write=True):
            return True
        try:
            body = self._json_body(handler)
            if path == '/api/ps5/scan':
                handler.send_body(200, self.ps5.scan())
            elif path == '/api/ps5/attach':
                handler.send_body(200, {'ok': True, 'host': self.ps5.attach(body.get('ip'))})
            elif path == '/api/ps5/install':
                handler.send_body(200, self.ps5.install(body.get('ip'), body.get('filename'),
                                                        bool(body.get('run'))))
            elif path == '/api/ps5/run':
                handler.send_body(200, self.ps5.run(body.get('ip'), body.get('filename')))
            elif path == '/api/ps5/kill':
                handler.send_body(200, self.ps5.kill(body.get('ip'), body.get('pid')))
            elif path == '/api/ps5/autoload':
                handler.send_body(200, self.ps5.autoload(body.get('ip'), bool(body.get('enabled')),
                                                         body.get('files') or []))
            elif path == '/api/ps5/shutdown':
                handler.send_body(200, self.ps5.shutdown(body.get('ip')))
            elif path == '/api/files/configure':
                info = self.files.configure(body.get('host'))
                if info.get('configured'):
                    self._remember()
                handler.send_body(200, {**info, 'hosts': self._known_hosts()})
            elif path == '/api/files/mkdir':
                self._ok(handler, self.files.mkdir(body.get('path')))
            elif path == '/api/files/rename':
                self._ok(handler, self.files.rename(body.get('from'), body.get('to')))
            elif path == '/api/files/delete':
                self._ok(handler, self.files.delete(body.get('path')))
            elif path == '/api/files/copy':
                self._ok(handler, self.files.copy(body.get('from'), body.get('to')))
            elif path == '/api/files/move':
                self._ok(handler, self.files.copy(body.get('from'), body.get('to'), move=True))
            elif path == '/api/files/cancel':
                self._ok(handler, self.files.cancel())
            elif path == '/api/files/bench':
                handler.send_body(200, {'ok': True, 'result': self.files.bench(body.get('path'))})
            else:
                handler.send_body(404, {'error': '未知操作'})
            return True
        except (ValueError, ConsoleError, FileServiceError) as exc:
            self._fail(handler, exc)
            return True

    def _ok(self, handler, result):
        handler.send_body(200, {'ok': True, 'result': result})

    def _upload(self, handler):
        if not handler.require_session(write=True):
            return True
        directory = self._arg(handler, 'path', '/data')
        overwrite = self._arg(handler, 'overwrite', '').lower() in ('1', 'true', 'yes')
        name = urllib.parse.unquote(handler.headers.get('X-Filename', ''))
        if handler.headers.get('Transfer-Encoding'):
            handler.send_body(400, {'error': '不支持分块上传'})
            return True
        try:
            length = int(handler.headers.get('Content-Length', '0'))
        except ValueError:
            length = 0
        if length <= 0:
            handler.send_body(400, {'error': '文件为空'})
            return True
        if length > self.settings.max_upload:
            handler.send_body(413, {'error': '文件超过上限'})
            return True

        def chunks():
            remaining = length
            while remaining > 0:
                data = handler.rfile.read(min(CHUNK, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

        try:
            handler.send_body(200, self.files.upload(directory, name, chunks(), length,
                                                     overwrite=overwrite))
        except ValueError as exc:
            self._fail(handler, exc)
        except FileServiceError as exc:
            self._fail(handler, exc)
        return True
