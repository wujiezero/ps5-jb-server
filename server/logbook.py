"""Service log capture for the admin page: a live ring buffer plus rotating files.

The service has always logged to stderr (journald under systemd). Users who
report a bug rarely know journalctl, so the same records are also kept

* in memory (the last ``CAPACITY`` records) for the settings page viewer, and
* in ``<data>/logs/service.log`` (rotated, a few MB in total) so an export
  still covers what happened before the last restart.

Successful page polls are logged at DEBUG (see AdminHandler.log_request) and
never reach either store; request bodies, cookies and passwords are never
logged anywhere in the service.
"""
import collections
import itertools
import logging
import logging.handlers
import threading
import time
from pathlib import Path

CAPACITY = 3000
FILE_BYTES = 1 << 20
FILE_BACKUPS = 3
EXPORT_LIMIT = 6 << 20
LEVELS = {'DEBUG': logging.DEBUG, 'INFO': logging.INFO, 'WARNING': logging.WARNING,
          'ERROR': logging.ERROR, 'CRITICAL': logging.CRITICAL}
FORMAT = '%(asctime)s %(levelname)s [%(name)s] %(message)s'


class MemoryLog(logging.Handler):
    """Bounded, thread-safe ring of formatted records with increasing ids."""

    def __init__(self, capacity=CAPACITY):
        super().__init__(logging.INFO)
        self.records = collections.deque(maxlen=capacity)
        self.counter = itertools.count(1)
        self.guard = threading.Lock()

    def emit(self, record):
        try:
            message = record.getMessage()
            if record.exc_info:
                message += '\n' + logging.Formatter().formatException(record.exc_info)
            entry = {'time': record.created, 'level': record.levelname,
                     'source': record.name if record.name != 'root' else 'service',
                     'message': message[:8000]}
            with self.guard:
                entry['seq'] = next(self.counter)
                self.records.append(entry)
        except Exception:  # a log handler must never break the caller
            self.handleError(record)

    def query(self, level='INFO', text='', after=0, limit=500):
        threshold = LEVELS.get(str(level or 'INFO').upper(), logging.INFO)
        needle = str(text or '').strip().lower()
        with self.guard:
            records = list(self.records)
        total = len(records)
        matches = [entry for entry in records
                   if entry['seq'] > after and LEVELS.get(entry['level'], 0) >= threshold
                   and (not needle or needle in entry['message'].lower() or needle in entry['source'].lower())]
        limit = max(1, min(int(limit), CAPACITY))
        return {'entries': matches[-limit:], 'matched': len(matches), 'total': total,
                'latest': records[-1]['seq'] if records else 0, 'capacity': self.records.maxlen}


_memory = None
_file_path = None


def install(directory=None, logger=None):
    """Attach the memory ring (always) and a rotating file (when writable)."""
    global _memory, _file_path
    logger = logger or logging.getLogger()
    if _memory is None:
        _memory = MemoryLog()
        logger.addHandler(_memory)
    if directory is not None and _file_path is None:
        try:
            directory = Path(directory)
            directory.mkdir(mode=0o750, parents=True, exist_ok=True)
            path = directory / 'service.log'
            handler = logging.handlers.RotatingFileHandler(path, maxBytes=FILE_BYTES,
                                                           backupCount=FILE_BACKUPS, encoding='utf-8')
            handler.setLevel(logging.INFO)
            handler.setFormatter(logging.Formatter(FORMAT))
            logger.addHandler(handler)
            _file_path = path
        except OSError as exc:
            logging.warning('service log file unavailable, keeping logs in memory only: %s', exc)
    return _memory


def memory():
    return _memory


def file_path():
    return _file_path


def query(**kwargs):
    if _memory is None:
        return {'entries': [], 'matched': 0, 'total': 0, 'latest': 0, 'capacity': 0}
    return _memory.query(**kwargs)


def file_text(path=None, limit=EXPORT_LIMIT):
    """Rotated files oldest first, trimmed to the newest ``limit`` bytes."""
    path = Path(path) if path else _file_path
    if path is None:
        return ''
    parts = []
    for index in range(FILE_BACKUPS, 0, -1):
        parts.append(path.with_name(f'{path.name}.{index}'))
    parts.append(path)
    chunks = []
    for part in parts:
        try:
            chunks.append(part.read_bytes())
        except OSError:
            continue
    data = b''.join(chunks)
    if len(data) > limit:
        data = data[-limit:]
        data = data[data.find(b'\n') + 1:]
    return data.decode('utf-8', 'replace')


def memory_text():
    if _memory is None:
        return ''
    with _memory.guard:
        records = list(_memory.records)
    return ''.join('{} {} [{}] {}\n'.format(
        time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(entry['time'])),
        entry['level'], entry['source'], entry['message']) for entry in records)
