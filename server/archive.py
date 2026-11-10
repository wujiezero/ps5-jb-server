"""Install RAR5 game archives from a NAS straight onto the PS5, streaming.

Volumes part01..partN are read in order as one byte stream, decoded by
libarchive (Debian: python3-libarchive-c) and every entry is written to the
PS5 file service as it comes out; nothing touches local disk.

A small RAR5 header scanner reads the same bytes next to libarchive and is the
reference for what the archive holds. libarchive 3.6.2 (Debian 12) silently
drops the last entries of solid multi-volume archives, so solid archives are
refused up front and any difference between the two lists fails the job
instead of installing an incomplete game.

Files land in a hidden folder next to the target first and are moved into
place only after every entry has been written and checked, so ShadowMount+
never sees a half-extracted game and a failed patch leaves the game intact.
"""
import ctypes
import json
import posixpath
import re
import zlib

from files import FileServiceError

CHUNK = 256 * 1024
HEAD_BYTES = 256 * 1024
MAX_HEADER = 2 * 1024 * 1024
# libarchive 3.6.2 (Debian 12) refuses RAR5 dictionaries above 64 MiB
# ("Declared dictionary size is not supported"); checked 2026-11-10.
MAX_DICTIONARY = 64 * 1024 * 1024
MAX_VOLUMES = 999
INCOMPLETE = '压缩包不完整：最后一个分卷没有结束标记，可能缺少后面的分卷'
SOLID = ('这是固实（solid）压缩包，服务器上的 libarchive 处理它会漏文件；'
         '需要 unrar 解压引擎（libunrar5、rclone、FUSE），或先在电脑或 NAS 上解压后再传文件夹')


def big_dictionary(dictionary, limit):
    return (f'压缩包用了 {dictionary // (1024 * 1024)} MB 的压缩字典，libarchive 最多支持 '
            f'{limit // (1024 * 1024)} MB；需要 unrar 解压引擎（libunrar5、rclone、FUSE），'
            '或先在电脑或 NAS 上解压后再传文件夹')
SIG_RAR5 = b'Rar!\x1a\x07\x01\x00'
SIG_RAR4 = b'Rar!\x1a\x07\x00'
TITLE_ID = re.compile(r'(?<![A-Z])([A-Z]{4}\d{5})(?!\d)')
_PART = re.compile(r'^(?P<base>.+)\.part(?P<num>\d{1,3})\.rar$', re.I)
_OLD = re.compile(r'^(?P<base>.+)\.(?:r\d{2}|s\d{2})$', re.I)
_SINGLE = re.compile(r'^(?P<base>.+)\.rar$', re.I)


class ArchiveError(RuntimeError):
    pass


def available():
    try:
        import libarchive  # noqa: F401  (Debian: python3-libarchive-c)
        return True
    except (ImportError, OSError):
        return False


def is_archive_name(name):
    return not name.startswith('._') and bool(_PART.match(name) or _SINGLE.match(name) or _OLD.match(name))


# ------------------------------------------------------------- volume sets --

def volume_set(entries, name):
    """Pick the volumes that belong with `name` out of its folder listing.

    Returns (base, [entry, ...]) in volume order; raises ArchiveError with a
    message for the page when the set is not something we can stream.
    """
    if name.startswith('._'):
        raise ArchiveError('这是 macOS 生成的元数据文件（._ 开头），不是压缩包；请选择同名但不带 ._ 的分卷')
    # macOS AppleDouble files (._name) sit next to every volume on SMB/WebDAV shares.
    files = {entry['name']: entry for entry in entries
             if not entry.get('dir') and not entry['name'].startswith('._')}
    if name not in files:
        raise ArchiveError(f'找不到 {name}')
    match = _PART.match(name)
    if match:
        base = match.group('base')
        numbered = {}
        for other, entry in files.items():
            found = _PART.match(other)
            if found and found.group('base').lower() == base.lower():
                numbered.setdefault(int(found.group('num')), entry)
        if len(numbered) > MAX_VOLUMES:
            raise ArchiveError('分卷太多')
        last = max(numbered)
        missing = [number for number in range(1, last + 1) if number not in numbered]
        if missing:
            shown = '、'.join(f'part{number:0{len(match.group("num"))}d}' for number in missing[:8])
            raise ArchiveError(f'缺少分卷：{shown}' + (' 等' if len(missing) > 8 else ''))
        return base, [numbered[number] for number in range(1, last + 1)]
    old = _OLD.match(name)
    single = _SINGLE.match(name)
    base = (old or single).group('base') if (old or single) else ''
    if old or (single and any(_OLD.match(other) and _OLD.match(other).group('base').lower()
                              == base.lower() for other in files)):
        raise ArchiveError('这是旧式 .rar + .r00 分卷（RAR4），暂不支持流式解压，'
                           '请先在电脑或 NAS 上解压后再传文件夹')
    if single:
        return base, [files[name]]
    raise ArchiveError('只支持 .rar 和 .partNN.rar 分卷压缩包')


def title_guess(text):
    match = TITLE_ID.search(text or '')
    return match.group(1) if match else ''


# ------------------------------------------------------------ RAR5 headers --

class _NeedMore(Exception):
    pass


def _vint(data, pos):
    value = shift = 0
    for index in range(10):
        if pos + index >= len(data):
            raise _NeedMore()
        byte = data[pos + index]
        value |= (byte & 0x7f) << shift
        if not byte & 0x80:
            return value, pos + index + 1
        shift += 7
    raise ArchiveError('压缩包头损坏（长度字段无效）')


class Rar5Scanner:
    """Walk RAR5 block headers in a byte stream without decoding any data.

    Fed with exactly the bytes libarchive reads; keeps only header bytes in
    memory and skips data areas by count.
    """

    def __init__(self, max_dictionary=MAX_DICTIONARY, strict=True):
        """strict: refuse what libarchive cannot read (solid, big dictionary).
        Otherwise those are only recorded, for an engine that can (unrar)."""
        self.max_dictionary = max_dictionary
        self.strict = strict
        self.buffer = bytearray()
        self.skip = 0
        self.expect_signature = True
        self.signatures = 0
        self.solid = False
        self.volume_flag = False
        self.finished = False           # end-of-archive block of the last volume seen
        self.entries = {}               # name -> is_dir, first piece of each file only
        self.links = set()
        self.unpacked = 0
        self.dictionary = 0
        self.main_seen = False
        self.files_seen = 0

    def feed(self, data):
        view = memoryview(data)
        if self.skip:
            taken = min(self.skip, len(view))
            self.skip -= taken
            view = view[taken:]
        if not view:
            return
        self.buffer += view
        self._parse()

    def _parse(self):
        buffer = self.buffer
        while buffer:
            if self.finished:
                buffer.clear()          # padding or anything after the last volume
                return
            if self.expect_signature:
                if self.signatures:
                    # rar pads every volume but the last to the requested size
                    # with zero bytes after its end-of-archive block.
                    zeros = len(buffer) - len(buffer.lstrip(b'\0'))
                    if zeros:
                        del buffer[:zeros]
                        continue
                if len(buffer) < 8:
                    if not SIG_RAR5.startswith(bytes(buffer)) and not SIG_RAR4.startswith(bytes(buffer[:7])):
                        raise ArchiveError(self._not_rar())
                    return
                if bytes(buffer[:8]) == SIG_RAR5:
                    del buffer[:8]
                    self.expect_signature = False
                    self.signatures += 1
                    continue
                if bytes(buffer[:7]) == SIG_RAR4:
                    raise ArchiveError('这是 RAR4 格式的压缩包，暂不支持流式解压，请先在电脑或 NAS 上解压')
                raise ArchiveError(self._not_rar())
            try:
                size, start = _vint(buffer, 4)
            except _NeedMore:
                return
            if size > MAX_HEADER or size == 0:
                raise ArchiveError('压缩包头损坏（头部长度异常）')
            end = start + size
            if len(buffer) < end:
                return
            crc = int.from_bytes(buffer[:4], 'little')
            if zlib.crc32(buffer[4:end]) != crc:
                raise ArchiveError('压缩包头校验失败：分卷损坏，或分卷顺序不对')
            header = bytes(buffer[start:end])
            del buffer[:end]
            data_size = self._block(header)
            if data_size:
                taken = min(data_size, len(buffer))
                del buffer[:taken]
                self.skip = data_size - taken

    def _not_rar(self):
        if self.signatures:
            return f'第 {self.signatures + 1} 个分卷不是 RAR 文件，或分卷顺序不对'
        return '不是 RAR 压缩包'

    def _block(self, header):
        try:
            kind, pos = _vint(header, 0)
            flags, pos = _vint(header, pos)
            extra_size = data_size = 0
            if flags & 0x01:
                extra_size, pos = _vint(header, pos)
            if flags & 0x02:
                data_size, pos = _vint(header, pos)
            extra = header[len(header) - extra_size:] if extra_size else b''
            if kind == 1:
                self._main(header, pos)
            elif kind == 2:
                self._file(header, pos, flags, extra)
            elif kind == 4:
                raise ArchiveError('压缩包加了密码（连文件名也加密），暂不支持')
            elif kind == 5:
                end_flags, _ = _vint(header, pos)
                if end_flags & 0x01:
                    self.expect_signature = True
                else:
                    self.finished = True
            return data_size
        except _NeedMore:
            raise ArchiveError('压缩包头损坏（内容不完整）') from None

    def _main(self, header, pos):
        archive_flags, pos = _vint(header, pos)
        number = 0
        if archive_flags & 0x02:
            number, pos = _vint(header, pos)
        self.main_seen = True
        self.volume_flag = bool(archive_flags & 0x01)
        if archive_flags & 0x04:
            self.solid = True
            if self.strict:
                raise ArchiveError(SOLID)
        if self.volume_flag and number != self.signatures - 1:
            raise ArchiveError(f'分卷顺序不对：第 {self.signatures} 个文件其实是第 {number + 1} 卷')

    def _file(self, header, pos, flags, extra):
        file_flags, pos = _vint(header, pos)
        unpacked, pos = _vint(header, pos)
        _, pos = _vint(header, pos)                 # attributes
        if file_flags & 0x02:
            pos += 4                                # mtime
        if file_flags & 0x04:
            pos += 4                                # data CRC32
        info, pos = _vint(header, pos)
        _, pos = _vint(header, pos)                 # host OS
        length, pos = _vint(header, pos)
        if pos + length > len(header):
            raise _NeedMore()
        name = header[pos:pos + length].decode('utf-8', 'replace')
        is_dir = bool(file_flags & 0x01)
        records = self._records(extra)
        if 0x01 in records:
            raise ArchiveError('压缩包加了密码，暂不支持；请先在电脑或 NAS 上解压')
        if flags & 0x08:
            return                                  # continuation of a file split across volumes
        self.files_seen += 1
        if 0x05 in records:
            self.links.add(name)
        if not is_dir:
            version = info & 0x3f
            if version > 1:
                raise ArchiveError('压缩包用了无法识别的压缩算法')
            if version == 1:
                raise ArchiveError('压缩包用了 RAR 7 的超大字典算法，暂不支持')
            if info & 0x40:
                self.solid = True
                if self.strict:
                    raise ArchiveError(SOLID)
            method = (info >> 7) & 0x07
            if method:
                dictionary = (128 * 1024) << ((info >> 10) & 0x0f)
                self.dictionary = max(self.dictionary, dictionary)
                if dictionary > self.max_dictionary and self.strict:
                    raise ArchiveError(big_dictionary(dictionary, self.max_dictionary))
            if not file_flags & 0x08:
                self.unpacked += unpacked
        self.entries[name] = is_dir

    @staticmethod
    def _records(extra):
        found, pos = {}, 0
        try:
            while pos < len(extra):
                size, after = _vint(extra, pos)
                kind, _ = _vint(extra, after)
                found[kind] = True
                pos = after + size
        except (_NeedMore, ArchiveError):
            pass
        return found


SCAN_WINDOW = 64 * 1024
MAX_SCAN_REQUESTS = 20000


def prescan(read_at, volumes, max_dictionary=MAX_DICTIONARY, strict=True):
    """Walk every block header of every volume, jumping over data areas.

    read_at(path, offset, size) returns bytes, or None when the source cannot
    read at an offset; then None is returned and only the stream check runs.
    Raises ArchiveError for anything extraction would trip over later.
    """
    scanner = Rar5Scanner(max_dictionary, strict=strict)
    requests = 0
    for index, volume in enumerate(volumes):
        size, position = volume['size'], 0
        while position < size and not scanner.finished:
            if scanner.expect_signature and scanner.signatures == index + 1:
                break                   # end-of-archive block seen; the rest is padding
            data = read_at(volume['path'], position, min(SCAN_WINDOW, size - position))
            requests += 1
            if data is None or requests > MAX_SCAN_REQUESTS:
                return None
            if not data:
                raise ArchiveError(f'{volume["name"]} 读取不完整')
            scanner.feed(data)
            position += len(data) + scanner.skip
            scanner.skip = 0
        if not scanner.finished and not (scanner.expect_signature and scanner.signatures == index + 1):
            raise ArchiveError(f'{volume["name"]} 不完整：没有读到这一卷的结束标记')
    if not scanner.finished:
        raise ArchiveError(INCOMPLETE)
    return scanner


def choose_engine(scanner, engines):
    """libarchive streams straight from the NAS client; unrar needs a mount.

    engines: {'libarchive': bool, 'unrar': bool, 'memory': bytes available to a worker}.
    """
    if not scanner.solid and scanner.dictionary <= MAX_DICTIONARY and engines.get('libarchive'):
        return 'libarchive'
    if engines.get('unrar'):
        need = unrar_memory(scanner.dictionary)
        memory = engines.get('memory') or 0
        if memory and need > memory:
            raise ArchiveError(f'压缩包用了 {scanner.dictionary // (1024 * 1024)} MB 的压缩字典，解压约需 '
                               f'{need / 2 ** 30:.1f} GB 内存，服务器只有 {memory / 2 ** 30:.1f} GB')
        return 'unrar'
    if scanner.solid:
        raise ArchiveError(SOLID)
    if scanner.dictionary > MAX_DICTIONARY:
        raise ArchiveError(big_dictionary(scanner.dictionary, MAX_DICTIONARY))
    raise ArchiveError('服务器没有可用的解压库：请安装 python3-libarchive-c，或 libunrar5 + rclone + fuse3')


def unrar_memory(dictionary):
    """Memory budget for an unrar worker: the window plus room for buffers."""
    return max(dictionary, 64 * 1024 * 1024) + 768 * 1024 * 1024


def inspect(client, volumes, engines=None):
    """Check the whole volume set up front and pick the engine that can read it."""
    engines = engines if engines is not None else {'libarchive': available(), 'unrar': False}
    read_at = getattr(client, 'read_at', None)
    if read_at is not None:
        scanner = prescan(read_at, volumes, strict=False)
        if scanner is not None:
            files = sum(1 for is_dir in scanner.entries.values() if not is_dir)
            return {'format': 'RAR5', 'first_entry': next(iter(scanner.entries), ''),
                    'dictionary': scanner.dictionary, 'files': files, 'solid': scanner.solid,
                    'unpacked': scanner.unpacked, 'complete': True,
                    'engine': choose_engine(scanner, engines)}
    scanner = Rar5Scanner(strict=False)
    reader = client.open(volumes[0]['path'])
    try:
        read = 0
        while read < HEAD_BYTES:
            data = reader.read(min(CHUNK, HEAD_BYTES - read))
            if not data:
                break
            read += len(data)
            scanner.feed(data)
            if scanner.files_seen or scanner.finished:
                break
    finally:
        try:
            reader.close()
        except Exception:
            pass
    if not scanner.main_seen:
        raise ArchiveError('读不到压缩包头，文件可能损坏')
    if len(volumes) > 1 and not scanner.volume_flag:
        raise ArchiveError('第一个分卷不是多卷压缩包的开头')
    first = next(iter(scanner.entries), '')
    # Only the first file's header was seen: libarchive's own stream check
    # (strict scanner) still guards the rest of the archive.
    return {'format': 'RAR5', 'first_entry': first, 'dictionary': scanner.dictionary,
            'files': None, 'unpacked': None, 'complete': False, 'solid': scanner.solid,
            'engine': choose_engine(scanner, engines)}


# ---------------------------------------------------------------- the run --

def _clean_relative(name):
    name = name.replace('\\', '/')
    parts = [part for part in name.split('/') if part not in ('', '.')]
    if not parts or name.startswith('/') or '..' in parts or any('\x00' in part for part in parts):
        raise ArchiveError(f'压缩包里有不安全的路径：{name[:120]}')
    return '/'.join(parts)


class _VolumeStream:
    """readinto() over volume readers, feeding the scanner as bytes pass."""

    def __init__(self, client, volumes, scanner, check, on_bytes):
        self.client, self.volumes, self.scanner = client, volumes, scanner
        self.check, self.on_bytes = check, on_bytes
        self.index = 0
        self.reader = None
        self.read_in_volume = 0
        self.failure = None

    def readinto(self, buffer):
        try:
            while True:
                self.check()
                if self.reader is None:
                    if self.index >= len(self.volumes):
                        if not self.scanner.finished:
                            raise ArchiveError(INCOMPLETE)
                        return 0
                    self.reader = self.client.open(self.volumes[self.index]['path'])
                    self.read_in_volume = 0
                data = self.reader.read(len(buffer))
                if data:
                    count = len(data)
                    ctypes.memmove(buffer, data, count)
                    self.read_in_volume += count
                    self.scanner.feed(data)
                    self.on_bytes(count)
                    return count
                self._close()
                expected = self.volumes[self.index].get('size')
                if expected and self.read_in_volume != expected:
                    raise ArchiveError(f'{self.volumes[self.index]["name"]} 读取不完整')
                self.index += 1
        except BaseException as exc:    # surfaced by the caller after libarchive gives up
            self.failure = exc
            return -1

    def _close(self):
        if self.reader is not None:
            try:
                self.reader.close()
            except Exception:
                pass
            self.reader = None

    def close(self):
        self._close()


def _pathname(entry):
    """UTF-8 pathname regardless of the process locale (services run under C)."""
    from libarchive import ffi
    function = ffi.libarchive.archive_entry_pathname_utf8
    function.argtypes = [ctypes.c_void_p]
    function.restype = ctypes.c_char_p
    raw = function(entry._entry_p)
    if raw is None:
        return entry.pathname
    return raw.decode('utf-8', 'replace')


class Installer:
    """One archive -> one game folder (or merged into an existing one)."""

    def __init__(self, files, client, volumes, dest, mode, ident, hooks, engine='libarchive',
                 volume_dir=None, expected=None):
        """hooks: check() raises to stop; read(n) bytes of progress (archive bytes for
        libarchive, unpacked bytes for unrar); current(name, size, done) the entry
        being written; written() one file done.

        engine 'unrar' reads the volumes as files in volume_dir (a NAS mount) and
        checks the result against expected ({name: is_dir} from a prescan)."""
        self.files, self.client, self.volumes = files, client, volumes
        self.engine, self.volume_dir, self.expected = engine, volume_dir, expected
        self.stream = None
        self.dest, self.mode, self.ident = dest.rstrip('/') or '/', mode, ident
        self.hooks = hooks
        self.check = hooks.check
        parent = self.dest if mode == 'install' else posixpath.dirname(self.dest)
        self.staging = posixpath.join(parent, f'.extract-{ident}')
        self.made = set()
        self.param = None
        self.sfo = False            # PS4 games carry sce_sys/param.sfo instead of param.json
        self.packages = 0           # .pkg installers rather than an unpacked game

    def run(self, base):
        try:
            self._mkdir(self.staging)
            written = self._extract()
            return self._finish(base, written)
        except BaseException:
            self._remove(self.staging)
            raise

    # -- extraction --
    def _extract(self):
        if self.engine == 'unrar':
            return self._extract_unrar()
        return self._extract_libarchive()

    def _extract_unrar(self):
        import unrar
        first = posixpath.join(self.volume_dir, self.volumes[0]['name'])
        written, skipped = set(), set()
        try:
            for entry in unrar.Archive(first, self.check):
                self._place(entry, entry.pathname, written, skipped)
        except unrar.UnrarError as exc:
            raise ArchiveError(f'解压失败：{exc}') from None
        if self.expected is not None:
            missing = [name for name, is_dir in self.expected.items()
                       if name not in written and name not in skipped and not is_dir]
            if missing:
                raise ArchiveError(f'解压结果不完整：{len(missing)} 个文件没有解出来'
                                   f'（如 {missing[0][:80]}），已放弃安装')
        return written

    def _place(self, entry, name, written, skipped):
        relative = _clean_relative(name)
        if entry.isdir:
            self._mkdirs(posixpath.join(self.staging, relative))
            written.add(name)
        elif not entry.isreg:
            skipped.add(name)       # links and devices have no place on the PS5
        else:
            self._write(entry, relative)
            written.add(name)

    def _extract_libarchive(self):
        import libarchive
        scanner = Rar5Scanner()
        stream = _VolumeStream(self.client, self.volumes, scanner, self.check, self.hooks.read)
        self.stream = stream
        written, skipped = set(), set()
        try:
            # libarchive-c 2.9 has no 'rar5' name; 'all' includes it and the
            # scanner has already rejected anything that is not RAR5.
            with libarchive.stream_reader(stream, block_size=CHUNK) as archive:
                for entry in archive:
                    self._place(entry, _pathname(entry), written, skipped)
        except Exception as exc:
            if stream.failure is not None:
                raise stream.failure from None
            from libarchive.exception import ArchiveError as LibError
            if isinstance(exc, LibError):
                raise ArchiveError(f'解压失败：{_lib_message(exc)}') from None
            raise
        finally:
            stream.close()
        if stream.failure is not None:
            raise stream.failure
        if not scanner.finished:
            raise ArchiveError(INCOMPLETE)
        missing = [name for name, is_dir in scanner.entries.items()
                   if name not in written and name not in skipped and not is_dir]
        if missing:
            raise ArchiveError(f'解压结果不完整：{len(missing)} 个文件没有解出来（如 {missing[0][:80]}），已放弃安装')
        return written

    def _write(self, entry, relative):
        size = entry.size
        if size is None:
            raise ArchiveError(f'{relative} 的大小未知，无法写入 PS5')
        directory, name = posixpath.split(posixpath.join(self.staging, relative))
        self._mkdirs(directory)
        self.hooks.current(relative, size, 0)
        keep = relative.endswith('sce_sys/param.json') and relative.count('/') <= 2 and size < 1024 * 1024
        if relative.endswith('sce_sys/param.sfo') and relative.count('/') <= 2:
            self.sfo = True
        if relative.lower().endswith('.pkg'):
            self.packages += 1
        captured = []
        done = [0]

        def chunks():
            for block in entry.get_blocks(CHUNK):
                self.check()
                done[0] += len(block)
                self.hooks.current(relative, size, done[0])
                if self.engine == 'unrar':
                    self.hooks.read(len(block))     # progress in unpacked bytes
                if keep:
                    captured.append(block)
                yield block
            if done[0] != size:
                # libarchive can end an entry early without an error when the
                # stream under it failed; report the real cause.
                if self.stream is not None and self.stream.failure is not None:
                    raise self.stream.failure
                raise ArchiveError(f'{relative} 解压出的数据不完整（{done[0]} / {size} 字节）')

        self.files.upload(directory, name, chunks(), size, overwrite=True)
        self.hooks.written()
        if keep:
            try:
                self.param = (relative, json.loads(b''.join(captured).decode('utf-8-sig')))
            except (ValueError, UnicodeDecodeError):
                pass

    # -- placing the result --
    def _finish(self, base, written):
        self.hooks.current('', 0, 0)
        entries = self.files.list(self.staging)['entries']
        root = self.staging
        top = ''
        if len(entries) == 1 and entries[0].get('dir'):
            top = entries[0]['name']
            root = posixpath.join(self.staging, top)
        title = ''
        if self.param and isinstance(self.param[1], dict):
            title = title_guess(str(self.param[1].get('titleId') or ''))
        note = install_note(bool(self.param) or self.sfo, self.packages)
        if self.mode == 'merge':
            self._merge(root, self.dest)
            self._remove(self.staging)
            return {'path': self.dest, 'title': title, 'note': note}
        name = title or title_guess(top) or title_guess(base) or top or base
        name = re.sub(r'[\\/\x00]', '_', name)[:200] or f'archive-{self.ident}'
        target = posixpath.join(self.dest, name)
        existing = {entry['name'] for entry in self.files.list(self.dest)['entries']}
        if name in existing:
            name = f'{name}-{self.ident[:6]}'
            target = posixpath.join(self.dest, name)
            note = (note + '；' if note else '') + f'目标已存在同名文件夹，已改名为 {name}'
        self.files.rename(root, target)
        if root != self.staging:
            self._remove(self.staging)
        return {'path': target, 'title': title, 'note': note}

    def _merge(self, source, target):
        """Move source's content over target; same disk, so every move is a rename."""
        try:
            present = {entry['name']: bool(entry.get('dir'))
                       for entry in self.files.list(target)['entries']}
        except FileServiceError:
            self._mkdir(target)
            present = {}
        for entry in self.files.list(source)['entries']:
            self.check()
            name = entry['name']
            src, dst = posixpath.join(source, name), posixpath.join(target, name)
            if entry.get('dir'):
                if name in present and present[name]:
                    self._merge(src, dst)
                    continue
                if name in present:
                    self.files.delete(dst)
                self.files.rename(src, dst)
            else:
                if name in present:
                    self.files.delete(dst)
                self.files.rename(src, dst)

    # -- helpers --
    def _mkdir(self, path):
        try:
            self.files.mkdir(path)
        except FileServiceError:
            pass                    # usually "exists"; a real problem shows on upload
        self.made.add(path)

    def _mkdirs(self, path):
        if path in self.made:
            return
        parent = posixpath.dirname(path)
        if parent != path and parent.startswith(self.staging):
            self._mkdirs(parent)
        self._mkdir(path)

    def _remove(self, path):
        try:
            self.files.delete(path)
        except Exception:
            pass


def install_note(has_params, packages):
    """What the page should say about the result beyond its path."""
    if has_params:
        return ''
    if packages:
        return (f'压缩包里是 {packages} 个 .pkg 安装包，不是解开的游戏文件夹；'
                '需要用 PS5 上的 pkg 安装工具安装')
    return '没有找到 sce_sys/param.json 或 param.sfo，可能不是游戏文件夹'


def _lib_message(exc):
    text = str(exc)
    return text.split(' (errno=')[0][:200] if text else '未知错误'
