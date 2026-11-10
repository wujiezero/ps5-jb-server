"""RARLab's UnRAR library (Debian non-free: libunrar5) through ctypes.

Used for what libarchive 3.6.2 cannot read: dictionaries above 64 MiB and
solid archives. The library only opens volumes that are real, seekable files,
so the caller points it at a read-only mount of the NAS folder. Data comes
back through the UCM_PROCESSDATA callback while RARProcessFile runs in
RAR_TEST mode: nothing is written locally.

The callback interface pushes data; the installer pulls it. A producer thread
drives the library and hands entries and data blocks over a bounded queue, so
the library waits whenever the PS5 upload is slower than decompression.
"""
import ctypes
import ctypes.util
import queue
import threading

ERAR_END_ARCHIVE = 10
RAR_OM_EXTRACT = 1
RAR_SKIP, RAR_TEST = 0, 1
UCM_CHANGEVOLUME, UCM_PROCESSDATA, UCM_NEEDPASSWORD, UCM_CHANGEVOLUMEW, UCM_NEEDPASSWORDW = range(5)
RAR_VOL_ASK = 0
RHDF_DIRECTORY = 0x20
ERRORS = {
    11: '内存不足（压缩字典太大）', 12: '数据校验失败：分卷损坏', 13: '压缩包损坏',
    14: '无法识别的压缩格式', 15: '无法打开分卷', 18: '读取分卷失败', 21: '解压库内部错误',
    22: '压缩包需要密码', 24: '密码不正确',
}

_CALLBACK = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_uint, ctypes.c_long, ctypes.c_long, ctypes.c_long)


class _Open(ctypes.Structure):
    _fields_ = [('ArcName', ctypes.c_char_p), ('ArcNameW', ctypes.c_wchar_p),
                ('OpenMode', ctypes.c_uint), ('OpenResult', ctypes.c_uint),
                ('CmtBuf', ctypes.c_char_p), ('CmtBufSize', ctypes.c_uint),
                ('CmtSize', ctypes.c_uint), ('CmtState', ctypes.c_uint), ('Flags', ctypes.c_uint),
                ('Callback', _CALLBACK), ('UserData', ctypes.c_long), ('OpFlags', ctypes.c_uint),
                ('CmtBufW', ctypes.c_wchar_p), ('Reserved', ctypes.c_uint * 25)]


class _Header(ctypes.Structure):
    _fields_ = [('ArcName', ctypes.c_char * 1024), ('ArcNameW', ctypes.c_wchar * 1024),
                ('FileName', ctypes.c_char * 1024), ('FileNameW', ctypes.c_wchar * 1024),
                ('Flags', ctypes.c_uint), ('PackSize', ctypes.c_uint), ('PackSizeHigh', ctypes.c_uint),
                ('UnpSize', ctypes.c_uint), ('UnpSizeHigh', ctypes.c_uint), ('HostOS', ctypes.c_uint),
                ('FileCRC', ctypes.c_uint), ('FileTime', ctypes.c_uint), ('UnpVer', ctypes.c_uint),
                ('Method', ctypes.c_uint), ('FileAttr', ctypes.c_uint), ('CmtBuf', ctypes.c_char_p),
                ('CmtBufSize', ctypes.c_uint), ('CmtSize', ctypes.c_uint), ('CmtState', ctypes.c_uint),
                ('DictSize', ctypes.c_uint), ('HashType', ctypes.c_uint), ('Hash', ctypes.c_char * 32),
                ('RedirType', ctypes.c_uint), ('RedirName', ctypes.c_wchar_p),
                ('RedirNameSize', ctypes.c_uint), ('DirTarget', ctypes.c_uint),
                ('MtimeLow', ctypes.c_uint), ('MtimeHigh', ctypes.c_uint),
                ('CtimeLow', ctypes.c_uint), ('CtimeHigh', ctypes.c_uint),
                ('AtimeLow', ctypes.c_uint), ('AtimeHigh', ctypes.c_uint),
                ('Reserved', ctypes.c_uint * 988)]


_LIB = None


def library():
    global _LIB
    if _LIB is None:
        name = ctypes.util.find_library('unrar') or 'libunrar.so.5'
        lib = ctypes.CDLL(name)
        lib.RAROpenArchiveEx.restype = ctypes.c_void_p
        lib.RAROpenArchiveEx.argtypes = [ctypes.POINTER(_Open)]
        lib.RARReadHeaderEx.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Header)]
        lib.RARProcessFile.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p]
        lib.RARCloseArchive.argtypes = [ctypes.c_void_p]
        _LIB = lib
    return _LIB


def available():
    try:
        library()
        return True
    except OSError:
        return False


class UnrarError(RuntimeError):
    pass


def _message(code, what=''):
    text = ERRORS.get(code, f'解压库错误 {code}')
    return f'{text}{"：" + what if what else ""}'


class Entry:
    """Looks like a libarchive entry to the installer."""

    def __init__(self, archive, pathname, is_dir, size, regular):
        self._archive = archive
        self.pathname = pathname
        self.isdir = is_dir
        self.isreg = regular
        self.size = size
        self._done = not regular

    def get_blocks(self, block_size=None):
        while not self._done:
            kind, value = self._archive._take()
            if kind == 'data':
                yield value
            elif kind == 'eof':
                self._done = True
                if value:
                    raise UnrarError(self._archive._explain(value, self.pathname))
            elif kind == 'error':
                self._done = True
                raise value
            else:
                raise UnrarError(f'解压库数据顺序异常（{kind}）')

    def _drain(self):
        for _ in self.get_blocks():
            pass


class Archive:
    """Iterate the entries of a RAR volume set; first_volume is a local path."""

    def __init__(self, first_volume, check=None):
        self.first_volume = first_volume
        self.check = check or (lambda: None)
        self.items = queue.Queue(maxsize=16)
        self.stop = threading.Event()
        self.missing_volume = ''
        self.password = False
        self.thread = None

    # -- consumer side --
    def __iter__(self):
        self.thread = threading.Thread(target=self._produce, daemon=True, name='unrar')
        self.thread.start()
        try:
            while True:
                kind, value = self._take()
                if kind == 'done':
                    return
                if kind == 'error':
                    raise value
                if kind != 'entry':
                    raise UnrarError(f'解压库数据顺序异常（{kind}）')
                entry = Entry(self, *value)
                yield entry
                entry._drain()
        finally:
            self.close()

    def _take(self):
        while True:
            self.check()
            try:
                return self.items.get(timeout=0.5)
            except queue.Empty:
                if self.thread is not None and not self.thread.is_alive() and self.items.empty():
                    return 'error', UnrarError('解压线程意外结束')

    def close(self):
        self.stop.set()
        while True:                     # unblock a producer waiting on a full queue
            try:
                self.items.get_nowait()
            except queue.Empty:
                break
        if self.thread is not None:
            self.thread.join(timeout=30)

    # -- producer side (library thread) --
    def _put(self, item):
        while not self.stop.is_set():
            try:
                self.items.put(item, timeout=0.5)
                return True
            except queue.Full:
                continue
        return False

    def _callback(self, message, user, first, second):
        if self.stop.is_set():
            return -1
        if message == UCM_PROCESSDATA:
            return 1 if self._put(('data', ctypes.string_at(first, second))) else -1
        if message in (UCM_CHANGEVOLUME, UCM_CHANGEVOLUMEW):
            if second == RAR_VOL_ASK:
                try:
                    self.missing_volume = (ctypes.wstring_at(first) if message == UCM_CHANGEVOLUMEW
                                           else ctypes.string_at(first).decode('utf-8', 'replace'))
                except (ValueError, OSError):
                    self.missing_volume = '下一个分卷'
                return -1
            return 1
        if message in (UCM_NEEDPASSWORD, UCM_NEEDPASSWORDW):
            self.password = True
        return -1

    def _produce(self):
        lib = library()
        callback = _CALLBACK(self._callback)      # keep a reference for the whole run
        data = _Open(ArcNameW=self.first_volume, OpenMode=RAR_OM_EXTRACT, Callback=callback)
        handle = lib.RAROpenArchiveEx(ctypes.byref(data))
        if not handle or data.OpenResult:
            if handle:
                lib.RARCloseArchive(handle)
            self._put(('error', UnrarError(_message(data.OpenResult or 15, '打开第一个分卷'))))
            return
        header = _Header()
        try:
            while not self.stop.is_set():
                code = lib.RARReadHeaderEx(handle, ctypes.byref(header))
                if code == ERAR_END_ARCHIVE:
                    break
                if code:
                    self._put(('error', UnrarError(self._explain(code, '读取文件头'))))
                    return
                name = header.FileNameW
                is_dir = bool(header.Flags & RHDF_DIRECTORY)
                regular = not is_dir and not header.RedirType
                size = header.UnpSize | (header.UnpSizeHigh << 32)
                if not self._put(('entry', (name, is_dir, size, regular))):
                    return
                code = lib.RARProcessFile(handle, RAR_TEST if regular else RAR_SKIP, None, None)
                if regular:
                    if not self._put(('eof', 0 if not code else code)):
                        return
                elif code:
                    self._put(('error', UnrarError(self._explain(code, name))))
                    return
                if code:
                    return
            self._put(('done', None))
        finally:
            lib.RARCloseArchive(handle)

    def _explain(self, code, what):
        if self.missing_volume:
            return f'缺少分卷 {self.missing_volume.rsplit("/", 1)[-1]}，可能下载不完整'
        if self.password:
            return '压缩包需要密码，暂不支持'
        return _message(code, what)
