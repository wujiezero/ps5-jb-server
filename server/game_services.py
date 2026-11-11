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
    error = str(result.get('error') or result.get('message') or '').lower()
    return result.get('busy') is True or error == 'busy' or 'busy' in error


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

    def package_install(self, host, source):
        _validate_local_source(source, '.pkg')
        started = self._request('POST', '/api/pkg/install', {
            'ps5_addr': host,
            'source': {'host_file': source},
            'content_id': '',
            'options': {
                'delete_source_copy_after': False,
                'allow_destructive_reinstall': False,
                'force_stream': False,
                'console_path_fallback': False,
                'proxy_link': False,
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
        deadline = time.monotonic() + self.poll_timeout
        while True:
            status = self._request('GET', '/api/pkg/install/status?' + urllib.parse.urlencode({'job': job}),
                                   timeout=20, allow_error=True)
            if _is_busy(status):
                return {'state': 'conflict', 'error_code': 'busy', 'error': '安装任务冲突：引擎正忙', 'job': job}
            if status.get('error') or status.get('ok') is False:
                return {'state': 'failed', 'error_code': 'status_failed',
                        'error': str(status.get('error') or '无法读取安装状态'), 'job': job}
            phase = str(status.get('phase') or status.get('state') or '').lower()
            verdict = str(status.get('verdict') or '').lower()
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
                return {'state': 'failed', 'error_code': 'timeout', 'error': '安装状态轮询超时', 'job': job}
            if self.poll_interval:
                time.sleep(self.poll_interval)

    def cheat_import(self, host, path):
        _validate_engine_cheat_path(path)
        result = self._request('POST', '/api/ps5/cheats/import', {'addr': host, 'path': path}, timeout=75)
        result.setdefault('state', 'done')
        return result

    import_cheat = cheat_import
