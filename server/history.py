"""Activity history in SQLite (<data>/activity.db).

FlowObserver keeps its in-memory ring for the live page and hands every new
event here. Only facts are stored; names, categories and "allowed now" are
applied when reading, so they follow later label and whitelist changes.

One connection behind a lock (WAL, synchronous=NORMAL). Rows older than
retention_days, and anything beyond MAX_ROWS, are deleted at most once an
hour. A database that cannot be opened or turns out corrupt is renamed
aside and recreated; history problems are logged and never stop the live
page or the service.
"""
import csv
import io
import logging
import sqlite3
import threading
import time
from pathlib import Path

MAX_ROWS = 500_000
PRUNE_EVERY = 3600
MAX_PAGE = 500
MAX_EXPORT = 50_000
COLUMNS = ('ts', 'kind', 'client', 'dst', 'port', 'proto', 'domain', 'verdict', 'local')
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, kind TEXT NOT NULL, client TEXT NOT NULL DEFAULT '',
    dst TEXT NOT NULL DEFAULT '', port INTEGER NOT NULL DEFAULT 0, proto TEXT NOT NULL DEFAULT '',
    domain TEXT NOT NULL DEFAULT '', verdict TEXT NOT NULL, local INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS events_verdict_ts ON events (verdict, ts);
CREATE INDEX IF NOT EXISTS events_domain ON events (domain);
"""
# A grouping key that can be fed straight back into /api/allow.
TARGET = "CASE WHEN domain != '' THEN 'domain:' || domain ELSE 'ip:' || dst END"


def _corrupt(exc):
    text = str(exc).lower()
    return 'malformed' in text or 'not a database' in text or 'quick_check' in text


class History:
    def __init__(self, path, retention_days=30, max_rows=MAX_ROWS, clock=time.time):
        self.path = Path(path)
        self.retention = max(1, int(retention_days)) * 86400
        self.max_rows = int(max_rows)
        self.clock = clock
        self.lock = threading.Lock()
        self.conn = None
        self.pruned = 0.0
        self.error = None
        with self.lock:
            self._open()

    # ---- connection ----
    def _connect(self):
        conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5, isolation_level=None)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.executescript(SCHEMA)
        if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            conn.close()
            raise sqlite3.DatabaseError('quick_check failed')
        return conn

    def _open(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = self._connect()
            self.path.chmod(0o600)
            self.error = None
        except sqlite3.DatabaseError as exc:
            self._recover(exc)
        except OSError as exc:
            self.conn, self.error = None, f'活动历史无法打开：{exc}'
            logging.error('%s', self.error)

    def _recover(self, exc):
        """Move a broken database aside and start a new one."""
        logging.error('activity history database unusable (%s); starting a new one', exc)
        if self.conn is not None:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
        self.conn = None
        stamp = time.strftime('%Y%m%d-%H%M%S')
        for suffix in ('', '-wal', '-shm'):
            source = Path(str(self.path) + suffix)
            if source.exists():
                try:
                    source.rename(Path(f'{self.path}.corrupt-{stamp}{suffix}'))
                except OSError:
                    source.unlink(missing_ok=True)
        try:
            self.conn = self._connect()
            self.path.chmod(0o600)
            self.error = f'活动历史数据库损坏，已改名留存为 {self.path.name}.corrupt-{stamp} 并重建'
        except (sqlite3.Error, OSError) as again:
            self.conn, self.error = None, f'活动历史无法重建：{again}'
            logging.error('%s', self.error)

    def _run(self, work, default=None):
        """Run work(conn) under the lock; on corruption recover once; never raise for writes."""
        with self.lock:
            if self.conn is None:
                return default
            try:
                return work(self.conn)
            except sqlite3.DatabaseError as exc:
                if _corrupt(exc):
                    self._recover(exc)
                else:             # disk full, locked, I/O: keep the file, try again next time
                    logging.warning('activity history unavailable: %s', exc)
                return default

    def close(self):
        with self.lock:
            if self.conn is not None:
                self.conn.close()
                self.conn = None

    # ---- writing ----
    @staticmethod
    def _row(event):
        return (float(event.get('epoch') or time.time()), str(event.get('kind') or 'flow'),
                str(event.get('client') or ''), str(event.get('dst') or ''), int(event.get('port') or 0),
                str(event.get('proto') or ''), str(event.get('domain') or ''),
                'allowed' if event.get('verdict') == 'allowed' else 'blocked', 1 if event.get('local') else 0)

    def add(self, events):
        """Store new live events (FlowObserver dicts); returns how many were written."""
        rows = [self._row(event) for event in events or [] if isinstance(event, dict)]
        if not rows:
            return 0

        def work(conn):
            conn.execute('BEGIN')
            try:
                conn.executemany(f'INSERT INTO events ({", ".join(COLUMNS)}) VALUES (?,?,?,?,?,?,?,?,?)', rows)
                conn.execute('COMMIT')
            except BaseException:
                conn.execute('ROLLBACK')
                raise
            return len(rows)
        written = self._run(work, 0)
        if self.clock() - self.pruned >= PRUNE_EVERY:
            self.prune()
        return written

    def prune(self):
        self.pruned = self.clock()
        cutoff = self.clock() - self.retention

        def work(conn):
            removed = conn.execute('DELETE FROM events WHERE ts < ?', (cutoff,)).rowcount
            over = conn.execute('SELECT COUNT(*) FROM events').fetchone()[0] - self.max_rows
            if over > 0:
                removed += conn.execute('DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY id LIMIT ?)',
                                        (over,)).rowcount
            return removed
        return self._run(work, 0)

    def clear(self):
        return self._run(lambda conn: conn.execute('DELETE FROM events').rowcount, 0)

    # ---- reading ----
    @staticmethod
    def _dict(row):
        keys = ('id',) + COLUMNS
        item = dict(zip(keys, row))
        item['local'] = bool(item['local'])
        return item

    def recent(self, limit=500):
        """Newest first, for warming the live ring after a restart."""
        rows = self._run(lambda conn: conn.execute(
            f'SELECT id, {", ".join(COLUMNS)} FROM events ORDER BY id DESC LIMIT ?', (int(limit),)).fetchall(), [])
        return [self._dict(row) for row in rows]

    @staticmethod
    def _where(start=None, end=None, verdict='', kind='', client='', text=''):
        clauses, args = [], []
        if start is not None:
            clauses.append('ts >= ?')
            args.append(float(start))
        if end is not None:
            clauses.append('ts < ?')
            args.append(float(end))
        if verdict in ('allowed', 'blocked'):
            clauses.append('verdict = ?')
            args.append(verdict)
        if kind == 'external':
            clauses.append("kind != 'service'")
        elif kind in ('flow', 'dns', 'service'):
            clauses.append('kind = ?')
            args.append(kind)
        if client:
            clauses.append('client = ?')
            args.append(client)
        if text:
            pattern = '%' + text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            clauses.append("(domain LIKE ? ESCAPE '\\' OR dst LIKE ? ESCAPE '\\' OR client LIKE ? ESCAPE '\\')")
            args += [pattern] * 3
        return (' WHERE ' + ' AND '.join(clauses)) if clauses else '', args

    def query(self, start=None, end=None, verdict='', kind='', client='', text='', before_id=None, limit=200):
        """Newest first; page with before_id = the returned next_before."""
        limit = max(1, min(int(limit), MAX_PAGE))
        where, args = self._where(start, end, verdict, kind, client, text[:200])
        if before_id is not None:
            where += (' AND ' if where else ' WHERE ') + 'id < ?'
            args.append(int(before_id))
        rows = self._run(lambda conn: conn.execute(
            f'SELECT id, {", ".join(COLUMNS)} FROM events{where} ORDER BY id DESC LIMIT ?',
            args + [limit + 1]).fetchall(), [])
        items = [self._dict(row) for row in rows[:limit]]
        return {'events': items, 'next_before': items[-1]['id'] if len(rows) > limit else None}

    def stats(self, days=7):
        days = 30 if int(days) >= 30 else 7
        start = self.clock() - days * 86400

        def work(conn):
            per_day = conn.execute(
                "SELECT date(ts, 'unixepoch', 'localtime') AS day, "
                "SUM(verdict = 'allowed'), SUM(verdict = 'blocked') FROM events "
                "WHERE ts >= ? GROUP BY day ORDER BY day", (start,)).fetchall()

            def top(verdict):
                return conn.execute(
                    f"SELECT {TARGET} AS target, COUNT(*) AS hits, MAX(ts), "
                    "COUNT(DISTINCT client) FROM events WHERE ts >= ? AND verdict = ? AND kind != 'service' "
                    "GROUP BY target ORDER BY hits DESC LIMIT 20", (start, verdict)).fetchall()
            clients = conn.execute(
                "SELECT client, SUM(verdict = 'allowed'), SUM(verdict = 'blocked'), MAX(ts) FROM events "
                "WHERE ts >= ? GROUP BY client ORDER BY COUNT(*) DESC LIMIT 50", (start,)).fetchall()
            total = conn.execute('SELECT COUNT(*), MIN(ts) FROM events').fetchone()
            return per_day, top('blocked'), top('allowed'), clients, total
        result = self._run(work)
        if result is None:
            return {'days': days, 'per_day': [], 'top_blocked': [], 'top_allowed': [], 'clients': [],
                    'rows': 0, 'since': None, 'error': self.error}
        per_day, blocked, allowed, clients, total = result

        def targets(rows):
            return [{'target': row[0], 'hits': row[1], 'last': row[2], 'clients': row[3]} for row in rows]
        return {'days': days,
                'per_day': [{'day': row[0], 'allowed': row[1] or 0, 'blocked': row[2] or 0} for row in per_day],
                'top_blocked': targets(blocked), 'top_allowed': targets(allowed),
                'clients': [{'client': row[0], 'allowed': row[1] or 0, 'blocked': row[2] or 0, 'last': row[3]}
                            for row in clients],
                'rows': total[0], 'since': total[1], 'error': self.error}

    def export_csv(self, start=None, end=None):
        """CSV text (oldest first, at most MAX_EXPORT rows) and whether it was cut."""
        where, args = self._where(start, end)
        rows = self._run(lambda conn: conn.execute(
            f'SELECT id, {", ".join(COLUMNS)} FROM events{where} ORDER BY id LIMIT ?',
            args + [MAX_EXPORT + 1]).fetchall(), [])
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(['时间', '来源', '客户端', '目标 IP', '端口', '协议', '域名', '结果', '内置域名'])
        for row in rows[:MAX_EXPORT]:
            item = self._dict(row)
            when = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(item['ts']))
            # A leading = + - @ would be run as a formula by spreadsheet programs.
            cells = [when, item['kind'], item['client'], item['dst'], item['port'], item['proto'],
                     item['domain'], '允许' if item['verdict'] == 'allowed' else '未放行', '是' if item['local'] else '']
            writer.writerow(["'" + c if isinstance(c, str) and c[:1] in ('=', '+', '-', '@') else c for c in cells])
        return out.getvalue(), len(rows) > MAX_EXPORT
