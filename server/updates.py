"""Stable GitHub release checks and detached, persisted service upgrades."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from version import VERSION, REPOSITORY

ACTIVE = frozenset(('queued', 'downloading', 'validating', 'backing_up', 'installing', 'restarting', 'verifying'))
MAX_ARCHIVE = 128 << 20
RELEASES = f'https://github.com/{REPOSITORY}/releases'
LATEST = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'


class UpdateError(ValueError):
    pass


def semver(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value):
        raise ValueError('版本号必须是 X.Y.Z 正式版本')
    return tuple(map(int, value.split('.')))


def newer(candidate, current):
    return semver(candidate) > semver(current)


class GithubRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != 'https' or parsed.hostname not in (
                'github.com', 'api.github.com', 'release-assets.githubusercontent.com',
                'objects.githubusercontent.com'):
            raise UpdateError('拒绝非 GitHub 的更新下载跳转')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_bytes(url, limit):
    request = urllib.request.Request(url, headers={
        'User-Agent': f'ps5-jb-server/{VERSION}', 'Accept': 'application/vnd.github+json' if url == LATEST else 'application/octet-stream',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    with urllib.request.build_opener(GithubRedirect()).open(request, timeout=30) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise UpdateError('GitHub 更新响应超过大小限制')
    return data


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False)
    os.replace(temporary, path)


class UpdateManager:
    def __init__(self, data_dir, program_dir=None, fetch=None, launcher=None, busy=None):
        self.data_dir = Path(data_dir).resolve()
        self.program_dir = Path(program_dir or Path(__file__).resolve().parent).resolve()
        self.directory = self.data_dir / 'updates'
        self.status_path = self.directory / 'status.json'
        self.release_path = self.directory / 'release.json'
        self.fetch = fetch or fetch_bytes
        self.launcher = launcher or self._launch
        self.busy = busy or (lambda: False)
        self.lock = threading.RLock()
        self.worker = None
        self.operations = 0

    @staticmethod
    def _read(path):
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _descriptor(self):
        descriptor = self._read(self.program_dir / 'update-install.json')
        if (descriptor.get('schema') != 1 or descriptor.get('program_dir') != str(self.program_dir)
                or descriptor.get('data_dir') != str(self.data_dir)):
            return None
        return descriptor

    def status(self):
        with self.lock:
            release = self._read(self.release_path)
            job = self._read(self.status_path) or None
            job = self._recover_job(job)
            descriptor = self._descriptor()
            return {'current': VERSION, 'latest': release.get('latest'),
                    'available': bool(release.get('available')) and bool(release.get('latest'))
                                 and newer(release['latest'], VERSION) and not release.get('check_error'),
                    'checked_at': release.get('checked_at'), 'release_url': release.get('release_url') or RELEASES,
                    'notes': release.get('notes') or '', 'check_error': release.get('check_error') or '',
                    'supported': descriptor is not None,
                    'unsupported_reason': '' if descriptor else '此部署尚未启用网页升级；请先用安装器登记服务路径。源码目录仅支持检查更新。',
                    'job': job}

    def _recover_job(self, job):
        # An early worker refusal or a reboot must not lock the updater forever.
        if (not job or job.get('state') not in ACTIVE or self.launcher != self._launch
                or (self.worker and self.worker.is_alive())
                or time.time() - job.get('updated_at', time.time()) < 30):
            return job
        ident = job.get('id', '')
        if not re.fullmatch(r'[0-9a-f]{32}', ident):
            return job
        try:
            observed = subprocess.run(['systemctl', 'show', '--property=ActiveState', '--value',
                                       'ps5-service-update-' + ident + '.service'],
                                      capture_output=True, text=True, timeout=3)
            state = observed.stdout.strip()
            if state in ('active', 'activating', 'reloading'):
                return job
            if state in ('inactive', 'failed') or observed.returncode:
                transaction = self._read(self.directory / 'transaction.json')
                phase = transaction.get('phase')
                if phase == 'committed':
                    job = dict(job, state='done', updated_at=time.time(), error='')
                elif phase == 'restored':
                    job = dict(job, state='rolled_back', updated_at=time.time(),
                               error='升级中断，已恢复旧程序')
                else:
                    pending_recovery = phase in ('prepared', 'replacing')
                    job = dict(job, state='installing' if pending_recovery else 'failed', updated_at=time.time(),
                               error=('升级已中断，等待恢复原程序；请运行部署的恢复服务。' if pending_recovery else
                                      '升级进程已退出或主机已重启；请查看 journalctl -u ps5-service-update-' + ident))
                atomic_json(self.status_path, job)
        except (OSError, subprocess.SubprocessError):
            pass
        return job

    def _release(self, document):
        if not isinstance(document, dict) or document.get('draft') or document.get('prerelease'):
            raise UpdateError('更新源不是正式发布版本')
        tag = document.get('tag_name', '')
        if not isinstance(tag, str) or not tag.startswith('v'):
            raise UpdateError('发布标签必须是 vX.Y.Z')
        version = tag[1:]
        semver(version)
        filename = f'ps5-jb-server-{version}.tar.gz'
        prefix = RELEASES + '/download/' + tag + '/'
        assets = document.get('assets')
        if not isinstance(assets, list):
            raise UpdateError('发布缺少更新附件')
        selected = {}
        for name in (filename, filename + '.sha256'):
            matches = [item for item in assets if isinstance(item, dict) and item.get('name') == name]
            if len(matches) != 1 or matches[0].get('browser_download_url') != prefix + name:
                raise UpdateError('发布缺少正式更新包或 SHA-256 校验附件，或附件地址不正确')
            selected[name] = matches[0]
        size = selected[filename].get('size', 0)
        if not isinstance(size, int) or not 0 < size <= MAX_ARCHIVE:
            raise UpdateError('更新包大小不正确或超过限制')
        return {'latest': version, 'available': newer(version, VERSION), 'asset': prefix + filename,
                'checksum': prefix + filename + '.sha256', 'size': size,
                'release_url': RELEASES + '/tag/' + tag, 'notes': str(document.get('body') or '')[:16000]}

    def check(self):
        with self.lock:
            # Explicit requests only; no network calls during application startup.
            try:
                result = self._release(json.loads(self.fetch(LATEST, 2 << 20)))
                result['check_error'] = ''
            except urllib.error.HTTPError as exc:
                result = {'latest': None, 'available': False,
                          'check_error': '' if exc.code == 404 else f'GitHub 检查失败（HTTP {exc.code}），请稍后重试'}
                exc.close()
            except (ValueError, OSError) as exc:
                result = {'latest': None, 'available': False, 'check_error': f'检查更新失败：{exc}'}
            result['checked_at'] = time.time()
            atomic_json(self.release_path, result)
            return self.status()

    def begin_operation(self):
        """Serialize new console writes against reserving a service upgrade."""
        with self.lock:
            if (self.status().get('job') or {}).get('state') in ACTIVE:
                raise UpdateError('服务正在升级，请完成后再开始新操作')
            self.operations += 1

    def end_operation(self):
        with self.lock:
            self.operations = max(0, self.operations - 1)

    def apply(self, version, background=True):
        with self.lock:
            status = self.status()
            if not status['supported']:
                raise UpdateError(status['unsupported_reason'])
            if status['job'] and status['job'].get('state') in ACTIVE:
                raise UpdateError('已有升级任务正在执行')
            if self._read(self.directory / 'transaction.json').get('phase') in ('prepared', 'replacing'):
                raise UpdateError('上一次升级尚未恢复，请先运行部署的恢复服务')
            release = self._read(self.release_path)
            if (not status['available'] or not release.get('available') or release.get('latest') != version
                    or release.get('check_error') or not newer(version, VERSION)):
                raise UpdateError('请先检查更新，并选择页面显示的最新版本')
            if time.time() - release.get('checked_at', 0) > 3600:
                raise UpdateError('更新检查已过期，请重新检查')
            if self.operations or self.busy():
                raise UpdateError('有传输或解压任务正在执行，请等任务结束后升级')
            ident = uuid.uuid4().hex
            atomic_json(self.status_path, {'id': ident, 'state': 'queued', 'version': version,
                                          'error': '', 'updated_at': time.time()})
            if background:
                self.worker = threading.Thread(target=self._prepare, args=(ident, release), daemon=True,
                                               name='service-update')
                self.worker.start()
            else:
                self._prepare(ident, release)
            return self.status()

    def _state(self, ident, version, state, error=''):
        atomic_json(self.status_path, {'id': ident, 'version': version, 'state': state,
                                      'error': error, 'updated_at': time.time()})

    def _prepare(self, ident, release):
        version = release['latest']
        try:
            self._state(ident, version, 'downloading')
            checksum = self.fetch(release['checksum'], 4096).decode('ascii').strip().split()
            name = f'ps5-jb-server-{version}.tar.gz'
            if (len(checksum) != 2 or not re.fullmatch(r'[a-f0-9]{64}', checksum[0])
                    or checksum[1] != name):
                raise UpdateError('SHA-256 校验文件格式不正确')
            data = self.fetch(release['asset'], MAX_ARCHIVE)
            if len(data) != release['size'] or hashlib.sha256(data).hexdigest() != checksum[0]:
                raise UpdateError('更新包 SHA-256 或长度校验失败，未修改服务')
            directory = self.directory / ident
            directory.mkdir(mode=0o700, parents=True)
            artifact = directory / name
            artifact.write_bytes(data)
            os.chmod(artifact, 0o600)
            descriptor = self._descriptor()
            if descriptor is None:
                raise UpdateError('升级部署描述已改变')
            spec = dict(descriptor, archive=str(artifact), sha256=checksum[0], version=version,
                        status_path=str(self.status_path), id=ident)
            spec_path = directory / 'spec.json'
            spec['spec_path'] = str(spec_path)
            atomic_json(spec_path, spec)
            if self.busy():
                raise UpdateError('传输任务已启动，请等任务结束后重新升级')
            self._state(ident, version, 'validating')
            self.launcher(spec_path)
        except subprocess.TimeoutExpired:
            # systemd may already own the detached worker; reconcile its exact
            # unit before accepting another job rather than declaring failure.
            self._state(ident, version, 'validating', '升级执行器启动结果暂未确认，正在等待任务状态')
        except Exception as exc:
            self._state(ident, version, 'failed', str(exc)[:500])

    def _launch(self, spec_path):
        worker = self.program_dir / 'update_worker.py'
        if not worker.is_file() or not str(self.program_dir).startswith('/opt/'):
            raise UpdateError('此目录不是可网页升级的已安装服务')
        subprocess.run(['systemd-run', '--quiet', '--collect', '--unit=ps5-service-update-' + spec_path.parent.name,
                        '--property=Type=exec', '--property=TimeoutStartSec=900',
                        '/usr/bin/python3', str(worker), str(spec_path)],
                       check=True, capture_output=True, text=True, timeout=15)
