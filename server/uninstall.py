"""Uninstall a game from the console and clean up what it leaves behind.

TransferManager runs these as 'uninstall' jobs. Two kinds of game:

- a game folder or image file (.ffpfsc and friends) on /data, /mnt/ext* or
  /mnt/usb* (what the library lists): its home-screen entry is removed through
  the ps5upload engine, but only when the engine says the title was registered
  from this very folder (for an image: from a ShadowMount+ image mount); a PKG
  install or a registration from another copy of the same title is left alone.
  Then the folder or file is deleted through FileProxy;
- a PKG install (origin 'pkg' in the engine's list): the engine runs Sony's
  uninstall (sceAppInstUtilAppUninstall), which removes the app and its icon.

Afterwards, optionally, the title's cheat files under /data/etaHEN/cheats and its
save folders (/user/home/<user>/savedata_prospero|savedata/<title>, through the
engine; off by default). If the home-screen step fails nothing is deleted, so a
game is never left as a dead icon without its files on purpose.
"""
import posixpath
import re
import time
import uuid

from files import FileServiceError
from game_services import CHEAT_ROOT, check_app_title
from migrate import Canceled, MigrationError, volume_root

UNINSTALL_LIMIT = 50
CHEAT_FORMATS = ('json', 'shn', 'mc4')


class UninstallError(MigrationError):
    pass


IMAGE_MOUNTS = '/mnt/shadowmnt/'


def _registered_here(entry, folder, image=False):
    source = (entry.get('source') or '').rstrip('/')
    if image:
        return bool(entry.get('image_backed')) or source.startswith(IMAGE_MOUNTS)
    return bool(source) and (source == folder or source.startswith(folder + '/'))


class UninstallJobs:
    """TransferManager mixin: 'uninstall' jobs (needs the same helpers as MigrationJobs)."""

    DELETE_WAIT = 1800.0      # how long a folder may keep disappearing after a timed-out delete

    def start_uninstall(self, path='', title='', origin='folder', delete_saves=False, delete_cheats=True,
                        host=None, name=''):
        if origin not in ('folder', 'image', 'pkg'):
            raise UninstallError('未知的游戏类型')
        if origin != 'pkg':
            if (not isinstance(path, str) or posixpath.normpath(path) != path or not volume_root(path)
                    or path == volume_root(path)):
                raise UninstallError('游戏必须在 /data、/mnt/ext*、/mnt/usb* 下')
        else:
            path = ''
        if title:
            check_app_title(title)
        elif origin == 'pkg':
            raise UninstallError('PKG 安装的游戏需要游戏编号')
        key = path or title
        with self.lock:
            for job in self.jobs.values():
                if (job.get('kind') == 'uninstall' and job.get('state') in ('queued', 'scanning', 'running')
                        and (job.get('path') or job.get('title')) == key):
                    raise UninstallError(f'{name or key} 已经在卸载队列里')
        ident = uuid.uuid4().hex[:10]
        job = {'id': ident, 'kind': 'uninstall', 'name': str(name or posixpath.basename(path) or title),
               'source': volume_root(path) if path else 'PKG', 'path': path, 'paths': [path] if path else [],
               'title': title, 'origin': origin, 'delete_saves': bool(delete_saves),
               'delete_cheats': bool(delete_cheats), 'phase': 'queued', 'state': 'queued',
               'steps': [], 'warnings': [], 'saves_deleted': 0, 'cheats_deleted': 0,
               'files_total': 0, 'files_done': 0, 'files_skipped': 0, 'files_failed': 0,
               'bytes_total': 0, 'bytes_done': 0, 'current': path, 'created': time.time(), 'started': None,
               'finished': None, 'elapsed': 0, 'speed': 0, 'eta': None, 'percent': 0, 'error': '', 'errors': [],
               'host': host or self.host or getattr(self.files, 'host', '') or ''}
        self._enqueue(job)
        return self.snapshot_one(ident)

    def _uninstall_note(self, job, field, text):
        with self.lock:
            job[field] = list(job[field]) + [text]
        self._persist(force=True)

    def _delete_folder(self, path, cancel):
        """Delete a game folder; a timed-out delete may still finish on the console."""
        try:
            self.files.delete(path)
            return
        except FileServiceError as exc:
            if exc.status is not None:
                raise
            failure = exc
        deadline = time.monotonic() + self.DELETE_WAIT
        while time.monotonic() < deadline:
            if cancel.wait(5):
                raise Canceled()
            try:
                if self._ps5_entry(path) is None:
                    return
            except FileServiceError:
                continue
        raise UninstallError(f'删除 {path} 没有完成：{str(failure)[:160]}')

    def _delete_cheats(self, job):
        title = job['title']
        pattern = re.compile(rf'(?<![A-Z0-9]){title}(?![0-9])', re.I)
        for fmt in CHEAT_FORMATS:
            folder = f'{CHEAT_ROOT}/{fmt}'
            try:
                entries = self.files.list(folder).get('entries', [])
            except FileServiceError as exc:
                if exc.status != 404:
                    self._uninstall_note(job, 'warnings', f'读不到 {folder}：{str(exc)[:120]}')
                continue
            for entry in entries:
                name = entry.get('name') or ''
                if entry.get('dir') or not pattern.search(name):
                    continue
                try:
                    self.files.delete(posixpath.join(folder, name))
                    job['cheats_deleted'] += 1
                except (FileServiceError, ValueError) as exc:
                    self._uninstall_note(job, 'warnings', f'金手指 {name} 没删掉：{str(exc)[:120]}')

    def _delete_saves(self, job, services):
        listed = services.saves(job['host'], job['title'])
        if listed['truncated']:
            self._uninstall_note(job, 'warnings', '主机上的存档太多，列表不完整，可能还有存档没删')
        for save in listed['saves']:
            try:
                services.delete_save(job['host'], save['path'], job['title'])
                job['saves_deleted'] += 1
            except Exception as exc:
                self._uninstall_note(job, 'warnings', f'存档 {save["path"]} 没删掉：{str(exc)[:120]}')

    def _execute_uninstall(self, job, cancel):
        services = getattr(self, 'game_services', None)
        path, title = job['path'], job['title']
        self._set(job, state='scanning', phase='check', started=time.time())
        present = None
        image = job['origin'] == 'image'
        if path:
            present = self._ps5_entry(path)
            if present is not None and bool(present.get('dir', True)) == image:
                raise UninstallError(f'{path} 不是{"游戏镜像文件" if image else "文件夹"}')
        installed, engine_error = None, ''
        if title and services is not None:
            try:
                installed = services.installed(job['host'])
            except Exception as exc:
                engine_error = str(exc)[:200]
        elif title:
            engine_error = '没有配置 ps5upload 引擎'
        entries = [item for item in (installed or {}).get('titles', []) if item['title'] == title]
        unregister = False
        if job['origin'] == 'pkg':
            if installed is None:
                raise UninstallError(f'卸载 PKG 安装的游戏需要 ps5upload 引擎：{engine_error}')
            if not any(item['origin'] == 'pkg' for item in entries):
                raise UninstallError(f'主机上没有以 PKG 安装的 {title}，请刷新后重试')
            unregister = True
        elif title:
            mine = [item for item in entries
                    if item['origin'] == 'registered' and _registered_here(item, path, image)]
            others = [item for item in entries if item not in mine]
            unregister = bool(mine)
            if installed is None:
                self._uninstall_note(job, 'warnings', f'主屏幕图标没有处理（{engine_error}）；'
                                                      '如果这个游戏注册过图标，引擎可用后再到 PS5 上删除')
            elif others:
                where = '、'.join(item['source'] or ('PKG 安装' if item['origin'] == 'pkg' else item['origin'])
                                 for item in others)
                self._uninstall_note(job, 'warnings', f'{title} 的主屏幕图标属于另一份游戏（{where}），保留未动')
        if path and present is None and not unregister:
            raise UninstallError(f'{path} 已经不存在，请刷新游戏库')
        if cancel.is_set():
            raise Canceled()

        self._set(job, state='running')
        if unregister:
            self._set(job, phase='icon')
            result = services.unregister(job['host'], title)
            if job['origin'] == 'pkg' and result['refused'] and result['still_present'] is not False:
                raise UninstallError(f'主机拒绝卸载 {title}（{result["rc"] or "未知代码"}），游戏仍在主机上')
            if result['refused'] or result['still_present']:
                self._uninstall_note(job, 'warnings', '主机报告这个游戏仍出现在「设置 → 存储」里，'
                                                      '可在 PS5 上再删除一次' + (f'（{result["rc"]}）' if result['rc'] else ''))
            self._uninstall_note(job, 'steps', 'icon')
        if path and present is not None:
            self._set(job, phase='files', current=path)
            self._delete_folder(path, cancel)
            self._uninstall_note(job, 'steps', 'files')
        if job['delete_cheats'] and title:
            self._set(job, phase='cheats')
            self._delete_cheats(job)
            self._uninstall_note(job, 'steps', 'cheats')
        if job['delete_saves'] and title:
            self._set(job, phase='saves')
            if services is None or installed is None:
                self._uninstall_note(job, 'warnings', f'存档没有删除：需要 ps5upload 引擎（{engine_error}）')
            else:
                try:
                    self._delete_saves(job, services)
                    self._uninstall_note(job, 'steps', 'saves')
                except Exception as exc:
                    self._uninstall_note(job, 'warnings', f'存档没有删除：{str(exc)[:160]}')
        self._set(job, state='done_with_warnings' if job['warnings'] else 'done', phase='done',
                  current='', percent=100)
