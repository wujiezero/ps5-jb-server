"""Console-facing API: PS5 host control (/api/ps5*) and file manager (/api/files*).

Kept apart from server.py so the routing table there stays readable; the admin
handler only forwards paths that start with /api/ps5 or /api/files.
"""
import ipaddress
import json
import logging
import os
import urllib.parse

import archive
from files import FileServiceError, FileProxy
from nas import NasError, NasStore, SecretBox, TransferManager, check_connection, client_for, \
    clean_path, smb_available, unrar_engine_available
from power import EngineClient, PowerError, PowerManager
from game_services import GameServices
from game_sources import plan_sources
from ps5 import ConsoleError, HostManager

MAX_JSON = 32 * 1024
CHUNK = 64 * 1024


class ConsoleAPI:
    def __init__(self, settings, catalog, autoscan=False):
        self.settings = settings
        self.ps5 = HostManager(settings, catalog)
        self.files = FileProxy(settings.subnet, port=settings.file_port,
                               ftp_port=getattr(settings, 'ftp_port', 2120),
                               space_port=getattr(settings, 'space_port', 8888))
        self.state_path = settings.data_dir / 'console.json'
        self._restore()
        self.enabled = settings.ps5_control
        self.file_manager = settings.file_manager
        self.nas = NasStore(settings.data_dir / 'nas.json',
                            getattr(settings, 'secret_key_path', None))
        self.engine = EngineClient(getattr(settings, 'engine_url', 'http://127.0.0.1:19113'))
        self.game_services = GameServices(self.engine)
        self.transfers = TransferManager(self.nas, self.files, work_dir=settings.data_dir / 'extract',
                                         game_services=self.game_services, host=self.files.host,
                                         state_path=settings.data_dir / 'transfers.json')
        self.power = PowerManager(self.engine, self.ps5.validate_ip)
        if autoscan and self.enabled:
            # The discovery cache is in memory; without this the host list is
            # empty after every restart until someone presses "rescan".
            extra = [self.files.host] if self.files.host else []
            self.ps5.discovery.scan_in_background(extra)

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
        if isinstance(exc, (ValueError, KeyError, TypeError)):
            handler.send_body(400, {'error': str(exc) or '请求参数不正确'})
        elif isinstance(exc, FileServiceError):
            # Keep "already exists" / "not found" distinguishable for the page.
            status = exc.status if exc.status in (404, 409) else 502
            handler.send_body(status, {'error': str(exc),
                                       **({'code': 'exists'} if status == 409 else {})})
        elif isinstance(exc, ConsoleError):
            handler.send_body(502, {'error': str(exc)})
        elif isinstance(exc, PowerError):
            handler.send_body(502, {'error': str(exc)})
        elif isinstance(exc, NasError):
            # Never 401 here: the page treats 401 as "admin session expired".
            handler.send_body(404 if exc.status == 404 else 502, {'error': str(exc)})
        else:
            logging.exception('console api failed')
            handler.send_body(500, {'error': '操作失败，请检查服务日志'})

    def _disabled(self, handler, path):
        if path.startswith(('/api/nas', '/api/transfers')) and not self.file_manager:
            handler.send_body(403, {'error': 'PS5 文件管理已在配置中关闭（[ps5] file_manager）'})
            return True
        if path.startswith('/api/ps5') and not self.enabled:
            handler.send_body(403, {'error': 'PS5 主机控制已在配置中关闭（[ps5] control）'})
            return True
        if path.startswith('/api/files') and not self.file_manager:
            handler.send_body(403, {'error': 'PS5 文件管理已在配置中关闭（[ps5] file_manager）'})
            return True
        return False

    # ---------- GET ----------
    # Answered even when [ps5] control is off ('/api/ps5' reports it disabled).
    _GET_ALWAYS = frozenset(('/api/ps5', '/api/ps5/power', '/api/ps5/progress'))
    GET_ROUTES = {
        '/api/ps5': '_get_ps5',
        '/api/ps5/power': '_get_power',
        '/api/ps5/progress': '_get_progress',
        '/api/files/info': '_get_files_info',
        '/api/files/download': '_get_download',
        '/api/files/tar': '_get_tar',
        '/api/files/volumes': '_get_volumes',
        '/api/files/list': '_get_list',
        '/api/files/size': '_get_size',
        '/api/files/job': '_get_job',
        '/api/nas': '_get_nas',
        '/api/nas/archive': '_get_nas_archive',
        '/api/nas/list': '_get_nas_list',
        '/api/transfers': '_get_transfers',
        '/api/files/transfers': '_get_file_transfers',
    }

    def handle_get(self, handler):
        """True when answered; False for an unknown path (the caller answers 404)."""
        path = urllib.parse.urlsplit(handler.path).path
        if not handler.require_session():
            return True
        try:
            name = self.GET_ROUTES.get(path)
            if path not in self._GET_ALWAYS:
                if path == '/api/files/info' and not self.file_manager:
                    handler.send_body(200, {'configured': False, 'enabled': False, 'hosts': [],
                                            'error': 'PS5 文件管理已在配置中关闭'})
                    return True
                if self._disabled(handler, path):
                    return True
            if name is None:
                return False
            result = getattr(self, name)(handler)
            return True if result is None else result
        except Exception as exc:  # every failure must still answer the browser
            self._fail(handler, exc)
            return True

    def _get_ps5(self, handler):
        if not self.enabled:
            handler.send_body(200, {'hosts': [], 'discovery': {'enabled': False}})
            return
        handler.send_body(200, self._with_power(self.ps5.overview()))

    def _get_power(self, handler):
        handler.send_body(200, self.power.status(self._arg(handler, 'ip')))

    def _get_progress(self, handler):
        handler.send_body(200, {'progress': self.ps5.progress(self._arg(handler, 'ip'))})

    def _get_files_info(self, handler):
        handler.send_body(200, {**self.files.info(), 'enabled': True, 'hosts': self._known_hosts()})

    def _get_download(self, handler):
        target = self._arg(handler, 'path')
        return self._stream(handler, lambda: self.files.stream(target),
                            _basename(target, 'download.bin'), 'application/octet-stream')

    def _get_tar(self, handler):
        target = self._arg(handler, 'path')
        return self._stream(handler, lambda: self.files.stream_folder(target),
                            _basename(target, 'folder') + '.tar', 'application/x-tar')

    def _get_volumes(self, handler):
        handler.send_body(200, self.files.volumes())

    def _get_list(self, handler):
        handler.send_body(200, self.files.list(self._arg(handler, 'path', '/data')))

    def _get_size(self, handler):
        handler.send_body(200, self.files.folder_size(self._arg(handler, 'path', '/data')))

    def _get_job(self, handler):
        handler.send_body(200, self.files.job())

    def _get_nas(self, handler):
        handler.send_body(200, {'connections': self.nas.list_public(),
                                'protocols': {'webdav': True, 'smb': smb_available(), 'ftp': True},
                                'encryption': SecretBox.available(),
                                'archive': archive.available() or unrar_engine_available(),
                                'unrar': unrar_engine_available()})

    def _get_nas_archive(self, handler):
        handler.send_body(200, self.transfers.inspect_archive(
            self._arg(handler, 'id'), self._arg(handler, 'path', '')))

    def _get_nas_list(self, handler):
        config = self.nas.get(self._arg(handler, 'id'))
        target = clean_path(self._arg(handler, 'path', '/'))
        client = client_for(config)
        try:
            entries = client.list(target)
        finally:
            client.close()
        entries.sort(key=lambda item: (not item['dir'], item['name'].lower()))
        handler.send_body(200, {'path': target, 'entries': entries})

    def _get_transfers(self, handler):
        handler.send_body(200, {'jobs': self.transfers.snapshot()})

    def _get_file_transfers(self, handler):
        handler.send_body(200, self.files.transfers())

    def _stream(self, handler, opener, filename, ctype):
        try:
            response = opener()
        except (ValueError, FileServiceError) as exc:
            self._fail(handler, exc)
            return True
        length = response.headers.get('Content-Length')
        handler.send_response(200)
        handler.send_header('Content-Type', ctype)
        handler.send_header('Content-Disposition', _disposition(filename))
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        known = bool(length and length.isdigit())
        if known:
            handler.send_header('Content-Length', length)
        else:
            # Without a length the end of the body is the end of the connection.
            handler.send_header('Connection', 'close')
        handler.end_headers()
        sent = 0
        try:
            with response:
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
                    sent += len(chunk)
        except OSError:
            logging.info('download interrupted by client')
            handler.close_connection = True
        if known and sent != int(length):
            # A short body cannot be followed by another response on this connection.
            handler.close_connection = True
        return True

    def _known_hosts(self):
        return [host['ip'] for host in self.ps5.discovery.snapshot()['hosts']]

    # ---------- POST ----------
    POST_ROUTES = {
        '/api/ps5/scan': '_post_scan',
        '/api/ps5/attach': '_post_attach',
        '/api/ps5/install': '_post_install',
        '/api/ps5/upgrade': '_post_upgrade',
        '/api/ps5/run': '_post_run',
        '/api/ps5/kill': '_post_kill',
        '/api/ps5/autoload': '_post_autoload',
        '/api/ps5/power/pair': '_post_pair',
        '/api/ps5/power/wake': '_post_wake',
        '/api/ps5/power/standby': '_post_standby',
        '/api/ps5/shutdown': '_post_shutdown',
        '/api/files/configure': '_post_configure',
        '/api/files/mkdir': '_post_mkdir',
        '/api/files/rename': '_post_rename',
        '/api/files/delete': '_post_delete',
        '/api/files/copy': '_post_copy',
        '/api/files/move': '_post_copy',
        '/api/files/cancel': '_post_files_cancel',
        '/api/nas/save': '_post_nas_save',
        '/api/nas/delete': '_post_nas_delete',
        '/api/nas/test': '_post_nas_test',
        '/api/nas/transfer': '_post_nas_transfer',
        '/api/nas/extract': '_post_nas_extract',
        '/api/transfers/cancel': '_post_transfers_cancel',
        '/api/transfers/clear': '_post_transfers_clear',
        '/api/transfers/retry': '_post_transfers_retry',
        '/api/files/bench': '_post_bench',
    }
    # Raw request bodies: the handler checks session and CSRF itself.
    RAW_POST_ROUTES = {'/api/files/upload': '_upload'}

    def handle_post(self, handler, route):
        path = route.rstrip('/')
        if path in self.RAW_POST_ROUTES:
            return getattr(self, self.RAW_POST_ROUTES[path])(handler)
        if not handler.require_session(write=True):
            return True
        if self._disabled(handler, path):
            return True
        try:
            body = self._json_body(handler)
            name = self.POST_ROUTES.get(path)
            if name is None:
                handler.send_body(404, {'error': '未知操作'})
            else:
                getattr(self, name)(handler, body, path)
            return True
        except Exception as exc:
            self._fail(handler, exc)
            return True

    def _post_scan(self, handler, body, path):
        handler.send_body(200, self.ps5.scan())

    def _post_attach(self, handler, body, path):
        handler.send_body(200, {'ok': True, 'host': self.ps5.attach(body.get('ip'))})

    def _post_install(self, handler, body, path):
        handler.send_body(200, self.ps5.install(body.get('ip'), body.get('filename'),
                                                bool(body.get('run'))))

    def _post_upgrade(self, handler, body, path):
        run = body.get('run')
        handler.send_body(200, self.ps5.upgrade(body.get('ip'), body.get('filename'),
                                                run=None if run is None else bool(run)))

    def _post_run(self, handler, body, path):
        handler.send_body(200, self.ps5.run(body.get('ip'), body.get('filename')))

    def _post_kill(self, handler, body, path):
        handler.send_body(200, self.ps5.kill(body.get('ip'), body.get('pid')))

    def _post_autoload(self, handler, body, path):
        handler.send_body(200, self.ps5.autoload(
            body.get('ip'), bool(body.get('enabled')),
            items=body.get('items'), start_delay=body.get('start_delay'),
            lead_ms=body.get('lead_ms') or 0, files=body.get('files')))

    def _post_pair(self, handler, body, path):
        handler.send_body(200, self.power.pair(body.get('ip'), body.get('pin'), body.get('account_id')))

    def _post_wake(self, handler, body, path):
        handler.send_body(200, {'ok': True, 'job': self.power.wake(body.get('ip'))})

    def _post_standby(self, handler, body, path):
        handler.send_body(200, self.power.standby(body.get('ip')))

    def _post_shutdown(self, handler, body, path):
        handler.send_body(200, self.ps5.shutdown(body.get('ip')))

    def _post_configure(self, handler, body, path):
        info = self.files.configure(body.get('host'))
        if info.get('configured'):
            self._remember()
        handler.send_body(200, {**info, 'hosts': self._known_hosts()})

    def _post_mkdir(self, handler, body, path):
        self._ok(handler, self.files.mkdir(body.get('path')))

    def _post_rename(self, handler, body, path):
        self._ok(handler, self.files.rename(body.get('from'), body.get('to')))

    def _post_delete(self, handler, body, path):
        self._ok(handler, self.files.delete(body.get('path')))

    def _post_copy(self, handler, body, path):
        result = self.files.copy(body.get('from'), body.get('to'), move=path.endswith('move'))
        instant = isinstance(result, dict) and result.get('instant') is True
        handler.send_body(200, {'ok': True, 'result': result, 'instant': instant})

    def _post_files_cancel(self, handler, body, path):
        self._ok(handler, self.files.cancel())

    def _post_nas_save(self, handler, body, path):
        handler.send_body(200, {'ok': True, 'connection': self.nas.save(body)})

    def _post_nas_delete(self, handler, body, path):
        self.nas.delete(body.get('id'))
        handler.send_body(200, {'ok': True})

    def _post_nas_test(self, handler, body, path):
        existing = self.nas.get(body['id']) if body.get('id') else None
        config = self.nas.validate(body, existing)
        handler.send_body(200, check_connection(config))

    def _post_nas_transfer(self, handler, body, path):
        host = body.get('host') or self.files.host
        auto_register = bool(body.get('auto_register', False))
        if auto_register:
            self.game_services.preflight(host)
        handler.send_body(200, {'ok': True, 'job': self.transfers.start(
            body.get('id'), body.get('paths'), body.get('dest'),
            body.get('overwrite') or 'skip', auto_register=auto_register,
            host=host, title=body.get('title') or '')})

    def _post_nas_extract(self, handler, body, path):
        auto_register = bool(body.get('auto_register', True))
        host = body.get('host') or self.files.host
        selected = body.get('paths') or ([body.get('path')] if body.get('path') else [])
        try:
            config = self.nas.get(body.get('id'))
        except NasError:
            config = None
        source_client = client_for(config) if config else None
        try:
            plan = plan_sources(source_client, selected)
        finally:
            if source_client is not None:
                source_client.close()
        if plan.get('kind') != 'archive':
            raise ValueError('所选源不是可流式解压的 RAR 分卷；PKG 请使用安装包流程')
        if auto_register:
            self.game_services.preflight(host)
        handler.send_body(200, {'ok': True, 'job': self.transfers.start_extract(
            body.get('id'), plan['path'], body.get('dest'), body.get('mode') or 'install',
            auto_register=auto_register, host=host,
            title=body.get('title') or plan.get('title_hint') or '')})

    def _post_transfers_cancel(self, handler, body, path):
        handler.send_body(200, self.transfers.cancel(body.get('id')))

    def _post_transfers_clear(self, handler, body, path):
        handler.send_body(200, self.transfers.clear())

    def _post_transfers_retry(self, handler, body, path):
        handler.send_body(200, {'ok': True, 'job': self.transfers.retry(body.get('id'))})

    def _post_bench(self, handler, body, path):
        handler.send_body(200, {'ok': True, 'result': self.files.bench(body.get('path'))})

    def _with_power(self, overview):
        """Add pairing / wake progress to each host; cheap local calls only."""
        engine = self.power.engine_info()
        paired = self.power.paired_hosts() if engine['available'] else set()
        with self.power.lock:
            jobs = {ip: dict(job) for ip, job in self.power.jobs.items()}
        for host in overview.get('hosts', []):
            power = host.setdefault('power', {})
            power['paired'] = host['ip'] in paired
            power['engine'] = engine['available']
            power['job'] = jobs.get(host['ip'])
        overview['power_engine'] = engine
        return overview

    def _ok(self, handler, result):
        handler.send_body(200, {'ok': True, 'result': result})

    def _upload(self, handler):
        if not handler.require_session(write=True):
            return True
        if self._disabled(handler, '/api/files'):
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
        limit = getattr(self.settings, 'max_file_upload', 0)
        if limit and length > limit:
            handler.send_body(413, {'error': f'文件超过上传上限（{limit // (1024 * 1024)} MB）'})
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
        except Exception as exc:
            self._fail(handler, exc)
        return True


def _basename(path, fallback):
    name = str(path or '').rstrip('/').rsplit('/', 1)[-1]
    return name or fallback


def _disposition(filename):
    """attachment with an ASCII fallback plus the exact UTF-8 name (RFC 6266/5987)."""
    ascii_name = ''.join(c if 32 <= ord(c) < 127 and c not in '"\\' else '_' for c in filename)
    quoted = urllib.parse.quote(filename, safe='')
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quoted}'
