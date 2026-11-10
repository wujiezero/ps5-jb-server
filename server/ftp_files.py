"""FTP backend for the PS5 file page (zftpd and other FTP payloads).

The HTTP file service (tom-ps5-usbnet's /fs/* API) only runs while its USB
network adapter has a link, and then answers on the adapter's own address.
Most jailbroken consoles already autoload an FTP server, so the file page
falls back to it. Every public method returns the same shapes as the HTTP
backend in files.py, so the API layer and the page do not care which one runs.

Each operation opens its own control connection: FTP is stateful, the admin
server is threaded, and LAN round trips are cheap.
"""
import collections
import datetime
import ftplib
import json
import os
import posixpath
import re
import socket
import tarfile
import threading
import time
import urllib.request

CHUNK = 64 * 1024
LIST_LIMIT = 5000
_MOUNT = re.compile(r'(ext|usb)\d+\Z')


class FtpError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _mtime(value):
    try:
        stamp = datetime.datetime.strptime(str(value)[:14], '%Y%m%d%H%M%S')
    except ValueError:
        return 0
    return int(stamp.replace(tzinfo=datetime.timezone.utc).timestamp())


def _join(parent, name):
    return parent.rstrip('/') + '/' + name if parent != '/' else '/' + name


class FtpFiles:
    def __init__(self, host, port=2120, timeout=10, error=FtpError, space_port=8888):
        self.host = host
        self.port = int(port)
        # FTP has no free-space command; web-file-mgr (often autoloaded next to
        # zftpd) answers GET /api/space with free/total per volume.
        self.space_port = int(space_port or 0)
        self.samples = collections.deque(maxlen=40)
        self.timeout = timeout
        self.error = error           # files.FileServiceError, so callers see one type
        self.job_lock = threading.Lock()
        self.job_state = {'state': 'idle'}
        self.cancel_flag = threading.Event()

    # -- connection --
    def connect(self, timeout=None):
        conn = ftplib.FTP(timeout=timeout or self.timeout, encoding='utf-8')
        try:
            conn.connect(self.host, self.port)
            conn.login()
            conn.voidcmd('TYPE I')
        except (OSError, EOFError, ftplib.Error) as exc:
            try:
                conn.close()
            finally:
                raise self.error(f'无法连接 PS5 的 FTP 服务（{self.host}:{self.port}）：{exc}') from exc
        return conn

    def _fail(self, exc, what):
        if isinstance(exc, ftplib.error_perm):
            code = str(exc)[:3]
            status = 404 if code == '550' else None
            return self.error(f'{what}失败：{str(exc)[4:200] or exc}', status=status)
        return self.error(f'{what}失败：{exc}')

    def probe(self):
        conn = self.connect(timeout=4)
        try:
            conn.quit()
        except (OSError, EOFError, ftplib.Error):
            conn.close()
        return True

    # -- listing helpers --
    def _entries(self, conn, path, limit=LIST_LIMIT):
        result, truncated = [], False
        for name, facts in listing(conn, path):
            kind = (facts.get('type') or '').lower()
            if kind in ('cdir', 'pdir') or name in ('.', '..'):
                continue
            if len(result) >= limit:
                truncated = True
                break
            is_dir = kind == 'dir'
            try:
                size = int(facts.get('size') or 0)
            except ValueError:
                size = 0
            result.append({'name': name, 'path': _join(path, name),
                           'kind': 'folder' if is_dir else 'file', 'dir': is_dir,
                           'size': 0 if is_dir else size, 'mtime': _mtime(facts.get('modify'))})
        return result, truncated

    def _stat(self, conn, path):
        """(exists, is_dir, size) for one path, using the parent listing."""
        parent, name = posixpath.split(path.rstrip('/'))
        for item in self._entries(conn, parent or '/', limit=100000)[0]:
            if item['name'] == name:
                return True, item['dir'], item['size']
        return False, False, 0

    def _walk(self, conn, root):
        """Yield (path, is_dir, size) depth-first, directories before their content."""
        stack = [root]
        while stack:
            current = stack.pop()
            entries, _ = self._entries(conn, current, limit=100000)
            for item in entries:
                yield item['path'], item['dir'], item['size'], item['mtime']
                if item['dir']:
                    stack.append(item['path'])

    def space(self):
        """{path: (free, total)} from web-file-mgr, or {} when it is not running."""
        if not self.space_port:
            return {}
        try:
            with urllib.request.urlopen(f'http://{self.host}:{self.space_port}/api/space',
                                        timeout=2) as response:
                document = json.loads(response.read(65536))
        except (OSError, ValueError):
            return {}
        result = {}
        for item in document.get('spaces', []) if isinstance(document, dict) else []:
            try:
                path, free, total = str(item['path']), int(item['free']), int(item['total'])
            except (KeyError, TypeError, ValueError):
                continue
            if total > 0 and 0 <= free <= total:
                result[path.rstrip('/') or '/'] = (free, total)
        return result

    # -- reads --
    def volumes(self):
        space = self.space()
        conn = self.connect()
        try:
            volumes = [{'path': '/data', 'kind': 'internal', 'label': '内置存储',
                        'total': None, 'free': None}]
            try:
                mounts, _ = self._entries(conn, '/mnt', limit=200)
            except ftplib.Error:
                mounts = []
            for item in sorted(mounts, key=lambda entry: entry['name']):
                if not item['dir'] or not _MOUNT.match(item['name']):
                    continue
                if item['path'] not in space:
                    try:
                        content, _ = self._entries(conn, item['path'], limit=1)
                    except ftplib.Error:
                        continue
                    if not content:
                        continue     # empty mount point: nothing plugged in
                kind = 'usb' if item['name'].startswith('usb') else 'ext'
                volumes.append({'path': item['path'], 'kind': kind,
                                'label': ('USB 存储 ' if kind == 'usb' else '扩展存储 ') +
                                         item['name'][3:],
                                'total': None, 'free': None})
            for volume in volumes:
                if volume['path'] in space:
                    volume['free'], volume['total'] = space[volume['path']]
            return {'volumes': volumes, 'backend': 'ftp', 'space': bool(space)}
        except ftplib.Error as exc:
            raise self._fail(exc, '读取存储位置') from exc
        finally:
            _close(conn)

    def list(self, path):
        conn = self.connect()
        try:
            entries, truncated = self._entries(conn, path)
            if not entries and path.rstrip('/') not in ('', '/data', '/mnt'):
                # zftpd answers MLSD on a missing path or a file with an empty
                # list; tell those apart from a genuinely empty folder.
                exists, is_dir, _ = self._stat(conn, path)
                if not exists:
                    raise self.error('文件夹不存在', status=404)
                if not is_dir:
                    raise ValueError('这是文件，不是文件夹')
        except ftplib.Error as exc:
            raise self._fail(exc, '读取目录') from exc
        finally:
            _close(conn)
        entries.sort(key=lambda item: (not item['dir'], item['name'].lower()))
        return {'path': path, 'entries': entries, 'truncated': truncated}

    def folder_size(self, path):
        conn = self.connect()
        try:
            total = files = folders = 0
            for _, is_dir, size, _ in self._walk(conn, path):
                if is_dir:
                    folders += 1
                else:
                    files += 1
                    total += size
            return {'path': path, 'size': total, 'files': files, 'folders': folders}
        except ftplib.Error as exc:
            raise self._fail(exc, '统计目录大小') from exc
        finally:
            _close(conn)

    def job(self):
        with self.job_lock:
            return dict(self.job_state)

    def transfers(self):
        return {'transfers': []}

    def stream(self, path):
        conn = self.connect()
        try:
            try:
                size = conn.size(path)
            except ftplib.Error:
                size = None
            data = conn.transfercmd('RETR ' + path)
        except ftplib.Error as exc:
            _close(conn)
            raise self._fail(exc, '下载') from exc
        return _Download(conn, data, size)

    def stream_folder(self, path):
        reader, writer = os.pipe()
        conn = self.connect()
        base = posixpath.basename(path.rstrip('/')) or 'folder'

        def produce():
            try:
                with os.fdopen(writer, 'wb') as sink, \
                        tarfile.open(fileobj=sink, mode='w|', format=tarfile.PAX_FORMAT) as tar:
                    for item, is_dir, size, mtime in list(self._walk(conn, path)):
                        info = tarfile.TarInfo(base + item[len(path.rstrip('/')):])
                        info.mtime = mtime
                        if is_dir:
                            info.type, info.mode = tarfile.DIRTYPE, 0o755
                            tar.addfile(info)
                            continue
                        info.size, info.mode = size, 0o644
                        data = conn.transfercmd('RETR ' + item)
                        try:
                            tar.addfile(info, _Exact(data.makefile('rb'), size))
                        finally:
                            data.close()
                            conn.voidresp()
            except (OSError, EOFError, ftplib.Error):
                pass      # client went away or the console failed mid-way: stop quietly
            finally:
                _close(conn)

        threading.Thread(target=produce, daemon=True, name='ftp-tar').start()
        return _Pipe(os.fdopen(reader, 'rb'))

    # -- mutations --
    def mkdir(self, path):
        return self._simple(lambda conn: conn.mkd(path), '新建文件夹')

    def rename(self, source, target):
        return self._simple(lambda conn: conn.rename(source, target), '重命名')

    def delete(self, path):
        def run(conn):
            try:
                conn.delete(path)       # plain files: one command, no listing
                return
            except ftplib.error_perm:
                pass                    # a folder, or genuinely missing
            exists, is_dir, _ = self._stat(conn, path)
            if not exists:
                raise ftplib.error_perm('550 文件或目录不存在')
            if not is_dir:
                conn.delete(path)
                return
            items = list(self._walk(conn, path))
            for item, item_dir, _, _ in reversed(items):
                if item_dir:
                    conn.rmd(item)
                else:
                    conn.delete(item)
            conn.rmd(path)
        return self._simple(run, '删除')

    def _simple(self, action, what):
        conn = self.connect()
        try:
            action(conn)
            return {'ok': True}
        except ftplib.Error as exc:
            raise self._fail(exc, what) from exc
        finally:
            _close(conn)

    def cancel(self):
        self.cancel_flag.set()
        return {'ok': True}

    def bench(self, path):
        raise ValueError('FTP 模式不支持测速')

    def copy(self, source, target, move=False):
        """Rename in place when possible; otherwise copy in a background job."""
        with self.job_lock:
            if self.job_state.get('state') in ('copying', 'moving', 'pending'):
                raise ValueError('已有复制或移动任务在进行，请稍后再试')
        conn = self.connect()
        try:
            exists, _, _ = self._stat(conn, source)
            if not exists:
                raise self.error('源文件或目录不存在', status=404)
            if self._stat(conn, target)[0]:
                raise self.error('目标位置已有同名文件或目录', status=409)
            if move:
                try:
                    conn.rename(source, target)
                    with self.job_lock:
                        self.job_state = {'state': 'done', 'kind': 'move', 'percent': 100}
                    return {'ok': True, 'instant': True}
                except ftplib.error_perm:
                    pass   # different volume: fall back to copy + delete
        except ftplib.Error as exc:
            raise self._fail(exc, '复制') from exc
        finally:
            _close(conn)
        self.cancel_flag.clear()
        self.samples.clear()
        with self.job_lock:
            self.job_state = {'state': 'moving' if move else 'copying',
                              'kind': 'move' if move else 'copy',
                              'done': 0, 'total': None, 'name': posixpath.basename(source),
                              'started': time.time(), 'elapsed': 0, 'speed': 0}
        threading.Thread(target=self._copy_job, args=(source, target, move), daemon=True,
                         name='ftp-copy').start()
        return {'ok': True, 'instant': False}

    def _copy_job(self, source, target, move):
        reader = writer = None
        try:
            reader, writer = self.connect(), self.connect()
            exists, is_dir, size = self._stat(reader, source)
            plan = [(source, target, is_dir, size)]
            if is_dir:
                prefix = source.rstrip('/')
                plan += [(item, target + item[len(prefix):], item_dir, item_size)
                         for item, item_dir, item_size, _ in self._walk(reader, source)]
            total = sum(entry[3] for entry in plan if not entry[2])
            done = 0
            self._update(total=total)
            for src, dst, entry_dir, _ in plan:
                if self.cancel_flag.is_set():
                    self._update(state='canceled')
                    return
                if entry_dir:
                    writer.mkd(dst)
                    continue
                incoming = reader.transfercmd('RETR ' + src)
                outgoing = writer.transfercmd('STOR ' + dst)
                try:
                    while True:
                        if self.cancel_flag.is_set():
                            break
                        chunk = incoming.recv(CHUNK)
                        if not chunk:
                            break
                        outgoing.sendall(chunk)
                        done += len(chunk)
                        self._update(done=done)
                finally:
                    incoming.close()
                    outgoing.close()
                    reader.voidresp()
                    writer.voidresp()
            if self.cancel_flag.is_set():
                self._update(state='canceled')
                return
            if move:
                self._update(state='moving')
                FtpFiles.delete(self, source)
            self._update(state='done', done=total, percent=100)
        except (OSError, EOFError, ftplib.Error, RuntimeError) as exc:
            self._update(state='failed', error=str(exc)[:200])
        finally:
            for conn in (reader, writer):
                if conn is not None:
                    _close(conn)

    def _update(self, **fields):
        with self.job_lock:
            self.job_state.update(fields)
            done, total = self.job_state.get('done'), self.job_state.get('total')
            if 'percent' not in fields and isinstance(done, int) and total:
                self.job_state['percent'] = round(done * 100 / total, 1)
            now = time.time()
            started = self.job_state.get('started')
            if started:
                self.job_state['elapsed'] = round(now - started, 1)
            if isinstance(done, int):
                self.samples.append((now, done))
                while len(self.samples) > 2 and now - self.samples[0][0] > 5:
                    self.samples.popleft()
                first = self.samples[0]
                if now - first[0] > 0.2:
                    self.job_state['speed'] = round((done - first[1]) / (now - first[0]))
            if self.job_state.get('state') in ('done', 'failed', 'canceled'):
                self.job_state['speed'] = 0

    def upload(self, target, chunks, length, overwrite=False):
        conn = self.connect(timeout=300)
        try:
            if not overwrite:
                # Callers that already know the folder (batch transfers) pass
                # overwrite=True and skip this per-file parent listing.
                exists, _, _ = self._stat(conn, target)
                if exists:
                    raise self.error('同名文件已存在', status=409)
            body = _Body(chunks)
            conn.storbinary('STOR ' + target, body, blocksize=CHUNK)
            if body.sent != length:
                raise ValueError('上传中断：浏览器发送的数据不完整')
            return {'ok': True, 'path': target}
        except ftplib.Error as exc:
            raise self._fail(exc, '上传') from exc
        finally:
            _close(conn)


_LIST_LINE = re.compile(r'^([dl\-])[rwxsStT\-]{9}\S*\s+\d+\s+\S+\s+\S+\s+(\d+)\s+'
                        r'(\w{3})\s+(\d{1,2})\s+(\d{1,2}:\d{2}|\d{4})\s+(.+)$')
_MONTHS = {m: i for i, m in enumerate(('Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec').split(), 1)}


def listing(conn, path):
    """(name, facts) pairs for a folder on any FTP server.

    zftpd speaks MLSD with OPTS MLST; some servers reject OPTS but still do
    MLSD, and older ones only answer LIST in Unix ls format.
    """
    try:
        return list(conn.mlsd(path, facts=['type', 'size', 'modify']))
    except ftplib.error_perm as exc:
        if str(exc)[:3] == '550' and 'param' not in str(exc).lower():
            raise
    try:
        return list(conn.mlsd(path))
    except ftplib.error_perm as exc:
        if str(exc)[:3] not in ('500', '501', '502', '504'):
            raise
    lines = []
    conn.retrlines('LIST ' + path, lines.append)
    result = []
    now = datetime.datetime.now(datetime.timezone.utc)
    for line in lines:
        match = _LIST_LINE.match(line)
        if not match:
            continue
        kind, size, month, day, when, name = match.groups()
        if kind == 'l':
            name = name.split(' -> ')[0]
        if name in ('.', '..'):
            continue
        try:
            if ':' in when:
                hour, minute = map(int, when.split(':'))
                stamp = datetime.datetime(now.year, _MONTHS[month], int(day), hour, minute)
                if stamp.replace(tzinfo=datetime.timezone.utc) > now + datetime.timedelta(days=1):
                    stamp = stamp.replace(year=now.year - 1)
            else:
                stamp = datetime.datetime(int(when), _MONTHS[month], int(day))
            modify = stamp.strftime('%Y%m%d%H%M%S')
        except (KeyError, ValueError):
            modify = ''
        result.append((name, {'type': 'dir' if kind == 'd' else 'file', 'size': size,
                              'modify': modify}))
    return result


def _close(conn):
    try:
        conn.quit()
    except (OSError, EOFError, ftplib.Error):
        try:
            conn.close()
        except OSError:
            pass


class _Download:
    """File-like RETR stream with the urllib response surface the API layer uses."""

    def __init__(self, conn, data, size):
        self.conn, self.data = conn, data
        self.headers = {'Content-Length': str(size)} if size is not None else {}

    def read(self, size=CHUNK):
        try:
            return self.data.recv(size)
        except socket.timeout:
            return b''

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.data.close()
            self.conn.voidresp()
        except (OSError, EOFError, ftplib.Error):
            pass
        _close(self.conn)


class _Pipe:
    def __init__(self, stream):
        self.stream = stream
        self.headers = {}

    def read(self, size=CHUNK):
        return self.stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stream.close()


class _Exact:
    """Never hand tarfile more or fewer bytes than the header promised."""

    def __init__(self, stream, size):
        self.stream, self.left = stream, size

    def read(self, size=-1):
        if self.left <= 0:
            return b''
        size = self.left if size is None or size < 0 else min(size, self.left)
        data = self.stream.read(size)
        if not data:
            data = b'\0' * size     # truncated on the console: pad to keep the archive valid
        self.left -= len(data)
        return data


class _Body:
    def __init__(self, chunks):
        self.iterator, self.buffer, self.sent = iter(chunks), b'', 0

    def read(self, size=CHUNK):
        while len(self.buffer) < size:
            try:
                self.buffer += next(self.iterator)
            except StopIteration:
                break
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        self.sent += len(data)
        return data


def ftp_reachable(host, port, timeout=3):
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False

