"""Move a game between the console's ShadowMount+ scan paths (internal /data <-> M.2 / USB).

TransferManager runs these as 'migrate' jobs, queued with the NAS jobs. A game is a
folder or an image file (.ffpfsc and friends). Between scan paths on the same volume
the move is a rename. Across volumes the bytes never leave the console:
FileProxy.copy() asks the file service on the PS5 to copy disk to disk
(tom-ps5-usbnet /fs/copy, or two FTP connections RETR -> STOR), and this module only
follows /fs/job. Steps:

1. check   - the source exists, the target does not, the target volume has room
             for the game plus MIGRATE_MARGIN, no other copy runs on the console;
2. copy    - into <target dir>/.migrate-<name> (a folder) or <name>.migrating (an
             image), so a half-copied game never sits where ShadowMount+ or etaHEN
             would pick it up;
3. verify  - the copy has the source's byte count (and file count for folders);
4. finish  - rename the staging copy into place (same volume: instant), then
             delete the source unless keep_source;
5. register- optionally register the home-screen icon again at the new path (title
             folders only; ShadowMount+ registers image games itself).

Any failure before step 4 removes the staging copy and leaves the source as it
was. After the rename the game is complete at the target, so a source that cannot
be deleted or a registration that fails only makes the job done_with_warnings.
"""
import collections
import posixpath
import time
import uuid

from files import FileServiceError, VOLUME_PATTERN

ACTIVE_JOB = ('copying', 'moving', 'pending', 'running', 'active')
MIGRATE_LIMIT = 50


class Canceled(Exception):
    pass


class MigrationError(ValueError):
    """A migration that cannot start or failed in a way the page should show as-is."""


def volume_root(path):
    """'/mnt/ext0' for '/mnt/ext0/games/X', None for a path outside the whitelist."""
    match = VOLUME_PATTERN.match(path or '')
    return match.group(0).rstrip('/') if match else None


def _clean(path):
    if (not isinstance(path, str) or not path.startswith('/') or '\x00' in path
            or posixpath.normpath(path) != path or path == '/'):
        raise MigrationError('PS5 路径必须是规范的绝对路径')
    return path


def _size(value):
    value = float(value or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024:
            return f'{value:.0f} {unit}' if unit == 'B' else f'{value:.1f} {unit}'
        value /= 1024
    return f'{value:.2f} TB'


def migration_target(game, target_dir):
    """Where a library game lands in target_dir (a scan path): target_dir/<its name>."""
    source = _clean(game['path'])
    target_dir = _clean(target_dir)
    if not volume_root(source) or not volume_root(target_dir):
        raise MigrationError('只能在 /data、/mnt/ext*、/mnt/usb* 上的扫描路径之间迁移')
    if posixpath.dirname(source) == target_dir:
        raise MigrationError(f'「{game.get("name") or source}」已经在 {target_dir} 里')
    if target_dir == source or target_dir.startswith(source + '/'):
        raise MigrationError('目标路径在游戏自己的文件夹里')
    return posixpath.join(target_dir, posixpath.basename(source))


class MigrationJobs:
    """TransferManager mixin: 'migrate' jobs. Needs self.files, self.lock, self.jobs,
    self._enqueue, self._set, self._persist, self._free_space, self._finish_registration."""

    MIGRATE_MARGIN = 1 << 30      # free space left on the target besides the game
    MIGRATE_POLL = 1.0            # seconds between /fs/job reads
    MIGRATE_STALL = 600.0         # seconds without progress before the copy is given up
    MIGRATE_POLL_ERRORS = 30      # consecutive failed /fs/job reads tolerated
    MIGRATE_IDLE_WAIT = 30.0      # how long to wait for someone else's copy to end

    def start_migrate(self, source, target, register=False, keep_source=False, host=None,
                      title='', name='', kind='folder'):
        if kind not in ('folder', 'image'):
            raise MigrationError('未知的游戏类型')
        source, target = _clean(source), _clean(target)
        source_root, target_root = volume_root(source), volume_root(target)
        if not source_root or not target_root:
            raise MigrationError('只能在 /data、/mnt/ext*、/mnt/usb* 之间迁移')
        if source == source_root or target == target_root:
            raise MigrationError('不能迁移整个存储卷')
        if source == target or target.startswith(source + '/'):
            raise MigrationError('目标位置就是游戏本身')
        if posixpath.basename(source) != posixpath.basename(target):
            raise MigrationError('迁移不改游戏的名字')
        with self.lock:
            for job in self.jobs.values():
                if (job.get('kind') == 'migrate' and job.get('state') in ('queued', 'scanning', 'running')
                        and (job['paths'][0] == source or job.get('target') == target)):
                    raise MigrationError(f'{posixpath.basename(source)} 已经在迁移队列里')
        ident = uuid.uuid4().hex[:10]
        parent = posixpath.dirname(target)
        base = posixpath.basename(target)
        staging = base + '.migrating' if kind == 'image' else '.migrate-' + base
        job = {'id': ident, 'kind': 'migrate', 'game_kind': kind, 'source': source_root, 'name': str(name or posixpath.basename(source)),
               'paths': [source], 'dest': parent, 'target': target, 'target_volume': target_root,
               'staging': posixpath.join(parent, staging),
               'keep_source': bool(keep_source) and source_root != target_root,
               'auto_register': bool(register) and kind == 'folder', 'registration': None,
               'registration_error': '', 'source_error': '', 'phase': 'queued', 'state': 'queued',
               'files_total': 0, 'files_done': 0, 'files_skipped': 0, 'files_failed': 0,
               'bytes_total': 0, 'bytes_done': 0, 'current': '', 'current_size': 0, 'current_done': 0,
               'created': time.time(), 'started': None, 'finished': None, 'elapsed': 0, 'speed': 0,
               'eta': None, 'percent': 0, 'error': '', 'errors': [],
               'host': host or self.host or getattr(self.files, 'host', '') or '', 'title': str(title or '')}
        self._enqueue(job)
        return self.snapshot_one(ident)

    # -- console helpers --
    def _ps5_entry(self, path):
        """The listing entry for path, None when its parent has no such name."""
        parent, name = posixpath.split(path)
        try:
            entries = self.files.list(parent).get('entries', [])
        except FileServiceError as exc:
            if exc.status == 404:
                return None
            raise
        return next((entry for entry in entries if entry.get('name') == name), None)

    def _ps5_mkdirs(self, path):
        current = volume_root(path)
        for part in [part for part in path[len(current):].split('/') if part]:
            current = posixpath.join(current, part)
            if self._ps5_entry(current) is None:
                self.files.mkdir(current)

    def _console_job(self):
        state = self.files.job()
        return state if isinstance(state, dict) else {}

    def _wait_console_idle(self, cancel):
        deadline = time.monotonic() + self.MIGRATE_IDLE_WAIT
        while str(self._console_job().get('state') or 'idle') in ACTIVE_JOB:
            if time.monotonic() >= deadline:
                raise MigrationError('PS5 上还有别的复制或移动任务在进行，等它结束后点「重试」')
            if cancel.wait(self.MIGRATE_POLL):
                raise Canceled()

    def _stop_console_copy(self):
        try:
            self.files.cancel()
        except (FileServiceError, ValueError):
            return
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if str(self._console_job().get('state') or 'idle') not in ACTIVE_JOB:
                    return
            except (FileServiceError, ValueError):
                return
            time.sleep(0.5)

    def _remove_staging(self, path):
        # Delete without looking first: some FTP servers leave dot folders out of listings.
        for attempt in range(3):
            try:
                self.files.delete(path)
                return
            except (FileServiceError, ValueError) as exc:
                if isinstance(exc, FileServiceError) and exc.status == 404:
                    return
                try:
                    if self._ps5_entry(path) is None and attempt:
                        return
                except FileServiceError:
                    pass
                time.sleep(0.5 * (attempt + 1))

    def _follow_console_copy(self, job, source, target, cancel):
        """Start the console's copy and mirror /fs/job into the job until it ends."""
        self.files.copy(source, target, move=False)
        names = {posixpath.basename(source), posixpath.basename(target)}
        started = last_change = time.monotonic()
        samples = collections.deque(maxlen=80)
        last_done, errors, seen_active = -1, 0, False
        while True:
            if cancel.is_set():
                self._stop_console_copy()
                raise Canceled()
            try:
                state = self._console_job()
                errors = 0
            except FileServiceError as exc:
                errors += 1
                if errors >= self.MIGRATE_POLL_ERRORS:
                    self._stop_console_copy()
                    raise MigrationError(f'一直读不到 PS5 上的复制进度：{str(exc)[:160]}') from exc
                cancel.wait(self.MIGRATE_POLL)
                continue
            phase = str(state.get('state') or 'idle').lower()
            foreign = state.get('name') and str(state['name']) not in names
            done = state.get('done')
            if isinstance(done, (int, float)) and not foreign:
                self._copy_progress(job, int(done), samples)
                if int(done) != last_done:
                    last_done, last_change = int(done), time.monotonic()
            if phase in ACTIVE_JOB:
                seen_active = True
                if time.monotonic() - last_change > self.MIGRATE_STALL:
                    self._stop_console_copy()
                    raise MigrationError(f'复制 {int(self.MIGRATE_STALL // 60)} 分钟没有进展，已停止')
            elif not seen_active and (foreign or phase == 'idle') and time.monotonic() - started < 15:
                pass        # the console has not published our job yet
            elif phase == 'done' or phase == 'idle':
                return      # verification decides whether the copy is complete
            elif phase in ('failed', 'error'):
                raise MigrationError('PS5 复制失败：' + (str(state.get('error') or '').strip()[:200] or '未知原因'))
            elif phase in ('canceled', 'cancelled'):
                raise MigrationError('PS5 上的复制任务被取消了')
            cancel.wait(self.MIGRATE_POLL)

    def _copy_progress(self, job, done, samples):
        now = time.time()
        with self.lock:
            total = job['bytes_total']
            done = min(done, total) if total else done
            job['bytes_done'] = done
            if not samples or now - samples[-1][0] >= 0.1:
                samples.append((now, done))
            while len(samples) > 2 and now - samples[0][0] > 5:
                samples.popleft()
            first = samples[0]
            if now - first[0] > 0.3:
                job['speed'] = max(0, round((done - first[1]) / (now - first[0])))
            job['percent'] = round(done * 100 / total, 1) if total else 0
            job['eta'] = round((total - done) / job['speed']) if job['speed'] > 0 and total >= done else None
            job['elapsed'] = round(now - job['started'], 1)
        self._persist()

    # -- the job --
    def _execute_migrate(self, job, cancel):
        source, target, staging = job['paths'][0], job['target'], job['staging']
        image = job.get('game_kind') == 'image'
        self._set(job, state='scanning', phase='check', started=time.time())
        entry = self._ps5_entry(source)
        if entry is None or bool(entry.get('dir', True)) == image:
            raise MigrationError(f'原游戏 {source} 不存在，请刷新游戏库')
        if self._ps5_entry(target) is not None:
            raise MigrationError(f'目标位置已有 {target}。如果是上次迁移已复制完成，确认游戏能运行后删掉原来的那份即可')
        if job['source'] == job['target_volume']:
            self._move_in_place(job, source, target, cancel)
            return
        self._wait_console_idle(cancel)
        if image:
            size, count = int(entry.get('size') or 0), 1
        else:
            measured = self.files.folder_size(source) or {}
            size = int(measured.get('size') or 0)
            count = measured.get('files')
            count = int(count) if isinstance(count, (int, float)) else None
        self._set(job, bytes_total=size, files_total=count or 0)
        free = self._free_space(target)
        if free is not None and free < size + self.MIGRATE_MARGIN:
            raise MigrationError(f'{job["target_volume"]} 空间不足：游戏 {_size(size)}，另需留出 '
                                 f'{_size(self.MIGRATE_MARGIN)}，目前只剩 {_size(free)}')
        if cancel.is_set():
            raise Canceled()
        self._ps5_mkdirs(job['dest'])
        self._remove_staging(staging)       # left behind by an interrupted migration
        self._set(job, state='running', phase='copy', current=source)
        try:
            self._follow_console_copy(job, source, staging, cancel)
            self._set(job, phase='verify', current=staging, speed=0, eta=None)
            if image:
                copied = self._ps5_entry(staging) or {}
                copied_size, copied_count = int(copied.get('size') or 0), None
            else:
                copied = self.files.folder_size(staging) or {}
                copied_size = int(copied.get('size') or 0)
                copied_count = copied.get('files')
            if copied_size != size or (count is not None and isinstance(copied_count, (int, float))
                                       and int(copied_count) != count):
                files = f'，{int(copied_count)} / {count} 个文件' if count is not None and isinstance(
                    copied_count, (int, float)) else ''
                raise MigrationError(f'校验没通过：复制出 {_size(copied_size)} / {_size(size)}{files}。'
                                     '原来的游戏没有改动，可以点「重试」')
            if cancel.is_set():
                raise Canceled()
            self._set(job, phase='finish', bytes_done=size, files_done=count or 0, percent=100)
            self.files.rename(staging, target)
        except BaseException:
            self._remove_staging(staging)
            raise
        if not job['keep_source']:
            try:
                self.files.delete(source)
            except (FileServiceError, ValueError) as exc:
                self._set(job, state='done_with_warnings',
                          source_error=f'游戏已在 {target}，但原来的那份没删掉（{str(exc)[:120]}），请在 PS5 文件页手动删除')
        self._migrate_done(job, target, size)

    def _move_in_place(self, job, source, target, cancel):
        """Source and target on one volume: a rename, nothing to copy."""
        if cancel.is_set():
            raise Canceled()
        self._set(job, state='running', phase='finish', current=source)
        self._ps5_mkdirs(job['dest'])
        self.files.rename(source, target)
        self._migrate_done(job, target, job.get('bytes_total') or 0)

    def _migrate_done(self, job, target, size):
        if job.get('auto_register') and getattr(self, 'game_services', None) and job.get('title'):
            self._set(job, phase='register')
            self._finish_registration(job, {'game': {'title': job['title'], 'path': target, 'name': job['name']}})
        self._set(job, state='done_with_warnings' if job.get('state') == 'done_with_warnings' else 'done',
                  phase='done', current='', percent=100, bytes_done=size)
