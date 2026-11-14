"""Small adapter to the local PS5Upload engine's v12.4.2 management API.

The engine owns AVA1 sessions and device keys. Game bytes continue to use the
existing streamed file proxy; registration only receives the final PS5 path.
"""
import os
import posixpath
import re
import time
import urllib.parse
import zipfile
from io import BytesIO

from power import PowerError

TITLE = re.compile(r'(?:CUSA|PPSA|PCAS|PCJS|PLAS|PLJS)\d{5}\Z')
_CHEAT_FORMATS = ('json', 'shn', 'mc4')
_CHEAT_EXT = re.compile(r'\.(json|shn|mc4)\Z', re.I)
_TITLE_IN_NAME = re.compile(r'(?:CUSA|PPSA|PCAS|PCJS|PLAS|PLJS)\d{5}', re.I)
_VERSION_IN_NAME = re.compile(r'(?:[_\-.]v?)(\d+(?:\.\d+){1,2})(?:[_\-.]|\Z)', re.I)


class GameServiceError(ValueError):
    pass


def _is_busy(result):
    error = str(result.get('error') or result.get('message') or result.get('reason') or '').lower()
    return result.get('busy') is True or error == 'busy' or 'busy' in error


# Any PS4 / PS5 title ID the engine can manage; NPXS system apps are refused.
APP_TITLE = re.compile(r'[A-Z]{4}\d{5}\Z')
# Save folders as the engine lists them: /user/home/<8 hex>/savedata_prospero/<TITLE> (PS5)
# or .../savedata/<TITLE> (PS4). Nothing else may be deleted through the engine.
SAVE_PATH = re.compile(r'/user/home/[0-9a-f]{8}/(?:savedata_prospero|savedata)/([A-Z]{4}\d{5})\Z')


def check_app_title(title):
    if not isinstance(title, str) or not APP_TITLE.fullmatch(title) or title.startswith('NPXS'):
        raise GameServiceError('游戏编号无效（系统应用不能卸载）')
    return title


# Engine phases -> the task card's steps (准备 → 传输 → 安装 → 确认).
PACKAGE_PHASES = {'queued': 'prepare', 'resolve': 'prepare', 'deliver': 'transfer',
                  'install': 'install', 'verify': 'verify', 'done': 'verify'}
CHEAT_ROOT = '/data/etaHEN/cheats'


def install_timeout(size):
    """How long to wait for one PKG: at least 10 minutes, else ~2 MiB/s plus 5 minutes."""
    return max(600.0, (int(size or 0) / (2 << 20)) + 300.0)


def write_cheats(files, parsed):
    """Write parse_cheat_zip() output to etaHEN's folders through FileProxy.

    Returns {'written': [destination...], 'warnings': [...]}; one failed file
    becomes a warning, the rest are still written.
    """
    written, warnings = [], list(parsed.get('warnings') or [])
    made = set()
    for item in parsed.get('files') or []:
        fmt, name = item['format'], item['name']
        if fmt not in _CHEAT_FORMATS or '/' in name or name in ('', '.', '..'):
            continue
        folder = f'{CHEAT_ROOT}/{fmt}'
        for directory in ('/data/etaHEN', CHEAT_ROOT, folder):
            if directory in made:
                continue
            try:
                files.mkdir(directory)
            except Exception:
                pass            # usually "already exists"; a real problem shows on upload
            made.add(directory)
        data = item['data']
        try:
            files.upload(folder, name, iter((data,)), len(data), overwrite=True)
            written.append(f'{folder}/{name}')
        except Exception as exc:
            warnings.append(f'写入 {folder}/{name} 失败：{str(exc)[:160]}')
    return {'written': written, 'warnings': warnings,
            'note': '需要在 etaHEN 里重新加载金手指（或重启 etaHEN）后才会生效' if written else ''}


def _validate_local_source(path, extension=None):
    if not isinstance(path, str) or not path.startswith('/') or '://' in path:
        raise GameServiceError('源文件必须是本机绝对路径')
    if '\x00' in path or '..' in path.split('/') or posixpath.normpath(path) != path:
        raise GameServiceError('源文件路径不安全')
    if extension and not path.lower().endswith(extension):
        raise GameServiceError(f'安装源必须是 {extension} 文件')


def _validate_engine_cheat_path(path):
    _validate_local_source(path)
    if not path.lower().endswith(('.json', '.xml')):
        raise GameServiceError('引擎直导只支持 JSON/XML；SHN/MC4 需要显式传输后由 etaHEN 加载')


def _normalized_member(name):
    if not isinstance(name, str) or '\x00' in name:
        return None, None
    name = name.replace('\\', '/')
    parts = [part for part in name.lstrip('/').split('/') if part not in ('', '.')]
    if '..' in parts:
        return None, None
    # Locate the etaHEN format directory anywhere in a wrapper archive.
    format_index = next((i for i, part in enumerate(parts) if part.lower() in _CHEAT_FORMATS), None)
    if format_index is not None and format_index + 1 < len(parts):
        fmt, filename = parts[format_index].lower(), parts[-1]
    else:
        filename = parts[-1] if parts else ''
        match = _CHEAT_EXT.search(filename)
        fmt = match.group(1).lower() if match else None
    if fmt not in _CHEAT_FORMATS or not filename or filename.startswith('.'):
        return None, None
    return fmt, filename


def _validate_cheat_name(filename, title=None, version=None):
    match = _TITLE_IN_NAME.search(filename)
    if not match:
        return False, '作弊文件名缺少有效游戏编号：' + filename
    found_title = match.group(0).upper()
    if title and found_title != str(title).upper():
        return False, f'作弊文件游戏编号 {found_title} 与所选 {title} 不一致：{filename}'
    if version:
        version = str(version)
        version_match = _VERSION_IN_NAME.search(filename[match.end():])
        if version_match and version_match.group(1) != version:
            return False, f'作弊文件版本 {version_match.group(1)} 与所选 {version} 不一致：{filename}'
    return True, None


def parse_cheat_files(files, title=None, version=None, max_files=256, max_bytes=16 << 20):
    """Normalize etaHEN cheat files from a mapping or iterable of pairs.

    The returned bytes are intentionally not uploaded.  Unknown wrappers and
    mismatched identities become warnings, while accepted files preserve their
    original bytes and filename.
    """
    if isinstance(files, dict):
        entries = files.items()
    else:
        entries = files
    result, warnings, total = [], [], 0
    for index, entry in enumerate(entries):
        if index >= max_files:
            warnings.append('作弊文件数量超过上限，剩余条目未读取')
            break
        try:
            name, payload = entry
        except (TypeError, ValueError):
            warnings.append('忽略格式无效的作弊文件条目')
            continue
        if not isinstance(payload, (bytes, bytearray)):
            warnings.append(f'忽略非字节作弊文件：{name}')
            continue
        if len(payload) > max_bytes or total + len(payload) > max_bytes:
            warnings.append(f'作弊文件超过大小上限，已忽略：{name}')
            continue
        fmt, filename = _normalized_member(name)
        if not fmt:
            warnings.append(f'忽略非 etaHEN 作弊文件：{name}')
            continue
        valid, warning = _validate_cheat_name(filename, title, version)
        if not valid:
            warnings.append(warning)
            continue
        total += len(payload)
        result.append({'format': fmt, 'name': filename, 'data': bytes(payload)})
    return {'files': result, 'warnings': warnings}


def parse_cheat_zip(source, title=None, version=None, max_zip_bytes=16 << 20,
                    max_member_bytes=4 << 20, max_files=256):
    """Parse bounded ZIP bytes (or a local path) using etaHEN's layout."""
    if isinstance(source, (str, os.PathLike)):
        _validate_local_source(os.fspath(source))
        with open(source, 'rb') as stream:
            source = stream.read(max_zip_bytes + 1)
    if not isinstance(source, (bytes, bytearray)) or len(source) > max_zip_bytes:
        raise GameServiceError('作弊 ZIP 超过大小上限')
    try:
        archive = zipfile.ZipFile(BytesIO(source))
    except (zipfile.BadZipFile, OSError) as exc:
        raise GameServiceError('作弊文件不是有效 ZIP') from exc
    entries, warnings = [], []
    infos = archive.infolist()
    if len(infos) > max_files:
        warnings.append('作弊文件数量超过上限，剩余条目未读取')
        infos = infos[:max_files]
    for info in infos:
        if info.is_dir() or info.file_size > max_member_bytes:
            warnings.append(f'忽略超过大小上限或目录条目：{info.filename}')
            continue
        try:
            entries.append((info.filename, archive.read(info)))
        except (OSError, RuntimeError, zipfile.BadZipFile):
            warnings.append(f'无法读取作弊文件：{info.filename}')
    result = parse_cheat_files(entries, title=title, version=version,
                               max_files=max_files, max_bytes=max_zip_bytes)
    result['warnings'] = warnings + result['warnings']
    return result


def plan_cheat_import(file_names, title, version=None, loader='etaHEN'):
    """Return an explicit FileProxy transfer plan; never claims auto-upload."""
    if str(loader).lower() != 'etahen':
        raise GameServiceError('仅支持 etaHEN 作弊加载器')
    if not TITLE.fullmatch(str(title)):
        raise GameServiceError('游戏编号无效')
    files, warnings = [], []
    for entry in file_names:
        name = entry.get('name') if isinstance(entry, dict) else entry
        fmt, filename = _normalized_member(name)
        if not fmt:
            warnings.append(f'忽略非 etaHEN 作弊文件：{name}')
            continue
        valid, warning = _validate_cheat_name(filename, title, version)
        if not valid:
            warnings.append(warning)
            continue
        files.append({'source': name, 'name': filename, 'format': fmt,
                      'destination': f'/data/etaHEN/cheats/{fmt}/{filename}'})
    warnings.append('需要通过现有 FileProxy 显式传输文件，并由调用方重新加载 etaHEN；本计划不会自动上传或刷新缓存')
    return {'loader': 'etaHEN', 'title': str(title), 'version': version,
            'files': files, 'warnings': warnings}


class GameServices:
    def __init__(self, engine, poll_interval=2.0, poll_timeout=300.0):
        self.engine = engine
        self.poll_interval = max(0.0, float(poll_interval))
        self.poll_timeout = max(0.1, float(poll_timeout))

    def _request(self, method, path, body=None, timeout=20, allow_error=False):
        try:
            result = self.engine.request(method, path, body, timeout=timeout)
        except PowerError as exc:
            raise GameServiceError(
                f'自动安装需要本机 ps5upload 引擎及 PS5 上运行的 ps5upload 插件：{exc}') from exc
        if not isinstance(result, dict):
            raise GameServiceError('ps5upload 引擎返回格式不正确')
        if not allow_error and (result.get('error') or result.get('ok') is False):
            raise GameServiceError('ps5upload：' + str(result.get('error') or '操作失败'))
        return result

    def preflight(self, host):
        query = urllib.parse.urlencode({'addr': host})
        result = self._request('GET', '/api/ps5/hw/info?' + query)
        if result.get('incomplete') or result.get('registered_unavailable'):
            raise GameServiceError('ps5upload 尚不能读取主机硬件信息，请检查 PS5 插件后重试')
        return result

    def register(self, host, game):
        title, path = game.get('title', ''), game.get('path', '')
        if not TITLE.fullmatch(title):
            raise GameServiceError('游戏编号无效，不能注册主屏幕图标')
        if (not isinstance(path, str) or not path.startswith(('/data/', '/mnt/ext', '/mnt/usb'))
                or '..' in path.split('/') or posixpath.normpath(path) != path
                or posixpath.basename(path) != title
                or not re.match(r'^/(?:data|mnt/(?:ext|usb)\d+)/', path)):
            raise GameServiceError('游戏最终路径必须是所选存储下的游戏编号目录')
        result = self._request('POST', '/api/ps5/app/register', {
            'addr': host, 'src_path': path, 'patch_drm_type': False,
        }, timeout=75)
        if result.get('title_id') != title:
            raise GameServiceError('ps5upload 注册返回的游戏编号与实际游戏不一致')
        return {'state': 'done', 'title': title, 'name': result.get('title_name') or game.get('name') or title,
                'path': path, 'used_nullfs': bool(result.get('used_nullfs'))}

    def installed(self, host):
        """Titles on the console now: [{title, name, origin, source, system}].

        origin is 'registered' (a folder game registered by ps5upload; source is its folder,
        or a ShadowMount+ image mount when image_backed), 'pkg' (installed by Sony's
        installer) or 'system'.
        """
        result = self._request('GET', '/api/ps5/apps/installed?' + urllib.parse.urlencode({'addr': host}),
                               timeout=30)
        titles = []
        for item in result.get('titles') or []:
            if not isinstance(item, dict) or not isinstance(item.get('title_id'), str):
                continue
            titles.append({'title': item['title_id'], 'name': str(item.get('title_name') or ''),
                           'origin': str(item.get('origin') or ''), 'source': str(item.get('source') or ''),
                           'image_backed': bool(item.get('image_backed')),
                           'system': bool(item.get('system')) or item['title_id'].startswith('NPXS')})
        return {'titles': titles, 'incomplete': bool(result.get('incomplete')),
                'registered_unavailable': bool(result.get('registered_unavailable'))}

    def unregister(self, host, title):
        """Remove a title's home-screen entry; for a PKG install this is Sony's uninstall."""
        check_app_title(title)
        result = self._request('POST', '/api/ps5/app/unregister', {'addr': host, 'title_id': title},
                               timeout=600, allow_error=True)
        if result.get('error') or result.get('ok') is False:
            raise GameServiceError('ps5upload 卸载失败：' + str(result.get('error') or '操作失败'))
        return {'refused': bool(result.get('sony_uninstall_refused')),
                'rc': str(result.get('sony_uninstall_rc') or ''),
                'still_present': result.get('still_present') if isinstance(result.get('still_present'), bool) else None}

    def saves(self, host, title):
        """Save folders of one title, for every user on the console."""
        check_app_title(title)
        result = self._request('GET', '/api/ps5/saves/list?' + urllib.parse.urlencode({'addr': host}), timeout=30)
        found = []
        for item in result.get('saves') or []:
            path = item.get('path') if isinstance(item, dict) else None
            match = SAVE_PATH.fullmatch(path) if isinstance(path, str) else None
            if match and match.group(1) == title and item.get('title_id') in (title, None):
                found.append({'path': path, 'size': int(item.get('size') or 0), 'user': item.get('user_id')})
        return {'saves': found, 'truncated': bool(result.get('truncated'))}

    def delete_save(self, host, path, title):
        match = SAVE_PATH.fullmatch(path) if isinstance(path, str) else None
        if not match or match.group(1) != check_app_title(title):
            raise GameServiceError('只能删除这个游戏自己的存档文件夹')
        self._request('POST', '/api/ps5/fs/delete', {'addr': host, 'path': path}, timeout=600)

    def package_install(self, host, source, timeout=None, progress=None, cancel=None):
        """Install one PKG through the engine and wait for its verdict.

        source: a local absolute path (host_file) or {'url': 'http://...'}, the
        form the NAS install uses: the console, or the engine as a fallback
        proxy, reads the package with Range requests. timeout: seconds to wait
        (default poll_timeout); progress(status) is called with every status;
        cancel (threading.Event) stops the engine job.
        """
        if isinstance(source, dict) and set(source) == {'url'}:
            url = source['url']
            if not isinstance(url, str) or not re.fullmatch(r'http://[0-9.]+:\d+/[A-Za-z0-9_./-]+', url):
                raise GameServiceError('安装源地址无效')
            wire, proxy = {'url': url}, True
        else:
            _validate_local_source(source, '.pkg')
            wire, proxy = {'host_file': source}, False
        started = self._request('POST', '/api/pkg/install', {
            'ps5_addr': host,
            'source': wire,
            'content_id': '',
            'options': {
                'delete_source_copy_after': False,
                'allow_destructive_reinstall': False,
                'force_stream': False,
                'console_path_fallback': False,
                # Lets the engine relay the URL when the console cannot reach it.
                'proxy_link': proxy,
                'insecure_tls': False,
            },
        }, timeout=75, allow_error=True)
        if _is_busy(started):
            return {'state': 'conflict', 'error_code': 'busy', 'error': '安装任务冲突：引擎正忙',
                    'job': started.get('job') or started.get('job_id')}
        if started.get('error') or started.get('ok') is False:
            return {'state': 'failed', 'error_code': 'start_failed',
                    'error': str(started.get('error') or '安装任务未能启动')}
        job = started.get('job') or started.get('job_id') or started.get('id')
        if not job:
            return {'state': 'failed', 'error_code': 'missing_job', 'error': '引擎未返回安装任务编号'}
        deadline = time.monotonic() + (self.poll_timeout if timeout is None else max(0.0, float(timeout)))
        while True:
            if cancel is not None and cancel.is_set():
                self._stop(job)
                return {'state': 'canceled', 'error_code': 'canceled', 'error': '已取消', 'job': job}
            status = self._request('GET', '/api/pkg/install/status?' + urllib.parse.urlencode({'job': job}),
                                   timeout=20, allow_error=True)
            if _is_busy(status):
                return {'state': 'conflict', 'error_code': 'busy', 'error': '安装任务冲突：引擎正忙', 'job': job}
            if status.get('error') or status.get('ok') is False:
                return {'state': 'failed', 'error_code': 'status_failed',
                        'error': str(status.get('error') or '无法读取安装状态'), 'job': job}
            phase = str(status.get('phase') or status.get('state') or '').lower()
            verdict = str(status.get('verdict') or '').lower()
            if progress is not None:
                try:
                    progress(status)
                except Exception:
                    pass
            if phase in ('failed', 'error') and _is_busy(status):
                return {'state': 'conflict', 'error_code': 'busy', 'error': '安装任务冲突：引擎正忙', 'job': job}
            if phase in ('cancelled', 'canceled'):
                return {'state': 'canceled', 'error_code': 'canceled', 'error': '安装已取消', 'job': job}
            if phase in ('done', 'failed', 'error', 'cancelled', 'canceled'):
                result = dict(status)
                result['job'] = job
                if phase == 'done' and verdict == 'installed':
                    result['state'] = 'done'
                    return result
                if verdict == 'may_not_launch':
                    result.update(state='warning', warning='安装完成但主机报告 may_not_launch，游戏可能无法启动')
                else:
                    result.update(state='failed', error=status.get('error') or
                                  f'安装未验证成功（verdict={verdict or "missing"}）')
                return result
            if time.monotonic() >= deadline:
                self._stop(job)
                return {'state': 'failed', 'error_code': 'timeout', 'error': '安装超时，已停止引擎任务', 'job': job}
            if self.poll_interval:
                if cancel is not None:
                    cancel.wait(self.poll_interval)
                else:
                    time.sleep(self.poll_interval)

    def _stop(self, job):
        try:
            self._request('POST', '/api/pkg/install/stop', {'job': job}, timeout=20, allow_error=True)
        except GameServiceError:
            pass

    def cheat_import(self, host, path):
        _validate_engine_cheat_path(path)
        result = self._request('POST', '/api/ps5/cheats/import', {'addr': host, 'path': path}, timeout=75)
        result.setdefault('state', 'done')
        return result

    import_cheat = cheat_import
