"""Game names for PS4/PS5 title IDs (CUSA12345, PPSA01234, ...).

Two sources, merged per title ID:

1. The offline catalog shipped with the repository (titledb/titles.json, built
   by scripts/build_titles.py from andshrew/PlayStation-Titles, MIT).
2. The game's own sce_sys/param.json (PS5) or param.sfo (PS4) read from the
   console when the browser shows that game folder. Results are cached on disk,
   so a folder is read once, not on every directory listing.

The offline catalog wins; the console only fills names it does not have
(homebrew, titles newer than the catalog, a missing Chinese name).
"""
import json
import logging
import os
import posixpath
import re
import struct
import threading
import time
from pathlib import Path

# Title ID prefixes: PS4/PS5 digital IDs plus disc product codes seen in dumps.
PREFIXES = ('CUSA', 'PPSA', 'PLAS', 'PCAS', 'PLJS', 'PCJS', 'PLJM', 'PCJM', 'PCKS', 'PLKS',
            'ELAS', 'ECAS', 'ELJS', 'ECJS', 'ELJM', 'ECJM', 'ELKS', 'ECKS', 'NPXS')
TITLE_ID = re.compile(r'(?<![A-Za-z0-9])(' + '|'.join(PREFIXES) + r')(\d{5})(?!\d)', re.I)
MAX_IDS = 500
MAX_FOLDERS = 64
MAX_PARAM = 512 * 1024
READ_BUDGET = 5.0         # seconds of console reads per request
MISS_TTL = 600            # do not retry a folder without a readable param file for 10 min
CACHE_LIMIT = 5000
# PS4 param.sfo TITLE_xx language codes.
SFO_ZH = ('TITLE_11', 'TITLE_10')     # Simplified, Traditional
SFO_EN = ('TITLE_01', 'TITLE_18')     # English (US), English (UK)


def extract_id(text):
    """Return the first title ID in a file name or path, upper-cased, else None."""
    match = TITLE_ID.search(text or '')
    return (match.group(1) + match.group(2)).upper() if match else None


def _clean(name):
    if not isinstance(name, str):
        return ''
    return re.sub(r'\s+', ' ', re.sub(r'[™®℠]', '', name)).strip()[:200]


def _is_chinese(name):
    return bool(re.search(r'[一-鿿]', name)) and not re.search(r'[぀-ヿ가-힯]', name)


def parse_param_json(data):
    """PS5 param.json -> (english, chinese). Missing names are empty strings."""
    try:
        document = json.loads(data.decode('utf-8-sig'))
    except (UnicodeDecodeError, ValueError, AttributeError):
        return '', ''
    params = document.get('localizedParameters') if isinstance(document, dict) else None
    if not isinstance(params, dict):
        return '', ''

    def name(language):
        entry = params.get(language)
        return _clean(entry.get('titleName')) if isinstance(entry, dict) else ''

    zh = name('zh-Hans') or name('zh-Hant') or name('zh-CN') or name('zh-TW')
    default = name(params.get('defaultLanguage') or '')
    en = name('en-US') or name('en-GB') or (default if not _is_chinese(default) else '')
    if not en:
        en = next((name(k) for k in params if k.startswith('en') and name(k)), '')
    if not zh and _is_chinese(default):
        zh = default
    return en or default, zh


def parse_param_sfo(data):
    """PS4 param.sfo -> (english, chinese)."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 20 or data[:4] != b'\x00PSF':
        return '', ''
    try:
        _, key_table, data_table, count = struct.unpack_from('<IIII', data, 4)
        values = {}
        for index in range(min(count, 512)):
            key_off, fmt, length, _, data_off = struct.unpack_from('<HHIII', data, 20 + index * 16)
            end = data.index(b'\x00', key_table + key_off)
            key = data[key_table + key_off:end].decode('ascii', 'replace')
            if key.startswith('TITLE') and fmt in (0x0004, 0x0204):
                raw = data[data_table + data_off:data_table + data_off + length]
                values[key] = _clean(raw.split(b'\x00', 1)[0].decode('utf-8', 'replace'))
    except (struct.error, ValueError):
        return '', ''
    zh = next((values[k] for k in SFO_ZH if values.get(k)), '')
    en = next((values[k] for k in SFO_EN if values.get(k)), '') or values.get('TITLE', '')
    if _is_chinese(en) and not zh:
        zh, en = en, ''
    return en, zh


def proxy_reader(files):
    """Read a small file through FileProxy (HTTP /fs/get or FTP RETR)."""
    def read(path):
        with files.stream(path) as stream:
            chunks, total = [], 0
            while total < MAX_PARAM:
                chunk = stream.read(min(64 * 1024, MAX_PARAM - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            return b''.join(chunks)
    return read


class TitleCatalog:
    def __init__(self, path, cache_path=None, reader=None, clock=time.monotonic):
        candidates = [Path(path)] if path else []
        here = Path(__file__).resolve().parent
        # Repository layout (server/ + titledb/) and the flat /opt layout.
        candidates += [here.parent / 'titledb' / 'titles.json', here / 'titles.json']
        self.path = next((p for p in candidates if p.is_file()), candidates[0] if candidates else None)
        self.cache_path = Path(cache_path) if cache_path else None
        self.reader = reader
        self.clock = clock
        self.lock = threading.Lock()
        self.read_lock = threading.Lock()
        self._titles = None
        self.meta = {}
        self.local = self._load_cache()
        self.misses = {}

    # -- offline catalog --
    def builtin(self):
        if self._titles is None:
            with self.lock:
                if self._titles is None:
                    try:
                        document = json.loads(self.path.read_text(encoding='utf-8'))
                        titles = document.get('titles') or {}
                        self.meta = {k: document.get(k) for k in ('source', 'commit', 'generated', 'count')}
                    except (OSError, ValueError, AttributeError) as exc:
                        logging.warning('title catalog unavailable (%s): %s', self.path, exc)
                        titles = {}
                    self._titles = titles if isinstance(titles, dict) else {}
        return self._titles

    def info(self):
        self.builtin()
        return {**self.meta, 'count': len(self._titles), 'local': len(self.local)}

    # -- console cache --
    def _load_cache(self):
        if not self.cache_path:
            return {}
        try:
            data = json.loads(self.cache_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        titles = data.get('titles') if isinstance(data, dict) else None
        return {k: v for k, v in (titles or {}).items()
                if isinstance(v, dict) and extract_id(k) == k}

    def _save_cache(self):
        if not self.cache_path:
            return
        temporary = self.cache_path.with_name(self.cache_path.name + '.tmp')
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps({'titles': self.local}, ensure_ascii=False), encoding='utf-8')
            os.replace(temporary, self.cache_path)
        except OSError as exc:
            logging.warning('could not write title cache %s: %s', self.cache_path, exc)

    def _read_folder(self, folder):
        """Return (english, chinese) from a game folder, raise on transport errors."""
        for name, parse in (('param.json', parse_param_json), ('param.sfo', parse_param_sfo)):
            try:
                data = self.reader(posixpath.join(folder, 'sce_sys', name))
            except Exception as exc:
                if getattr(exc, 'status', None) is None and not isinstance(exc, ValueError):
                    raise       # console unreachable: try again later
                continue        # file missing: try the next format
            en, zh = parse(data)
            if en or zh:
                return en, zh
        return '', ''

    def _scan(self, folders):
        """Read param files for folders whose title the cache does not know yet."""
        pending = []
        if not self.reader or not folders:
            return pending
        deadline = self.clock() + READ_BUDGET
        changed = False
        with self.read_lock:
            for folder in folders:
                title_id = extract_id(posixpath.basename(folder.rstrip('/')))
                if not title_id or title_id in self.local:
                    continue
                missed = self.misses.get(folder)
                if missed is not None and self.clock() - missed < MISS_TTL:
                    continue
                if self.clock() > deadline:
                    pending.append(title_id)
                    continue
                try:
                    en, zh = self._read_folder(folder)
                except Exception as exc:
                    logging.info('title lookup for %s stopped: %s', folder, exc)
                    pending.extend(extract_id(posixpath.basename(f.rstrip('/'))) or ''
                                   for f in folders[folders.index(folder):])
                    break
                if en or zh:
                    self.local[title_id] = {'en': en, 'zh': zh, 'path': folder}
                    changed = True
                else:
                    self.misses[folder] = self.clock()
            if changed:
                if len(self.local) > CACHE_LIMIT:
                    for key in list(self.local)[:len(self.local) - CACHE_LIMIT]:
                        del self.local[key]
                self._save_cache()
        return sorted({p for p in pending if p})

    # -- lookup --
    def entry(self, title_id):
        builtin = self.builtin().get(title_id) or []
        local = self.local.get(title_id) or {}
        en = (builtin[0] if builtin else '') or local.get('en', '')
        zh = (builtin[1] if len(builtin) > 1 else '') or local.get('zh', '')
        if not (en or zh):
            return None
        sources = (['builtin'] if builtin else []) + (['console'] if local else [])
        return {'name': zh or en, 'en': en, 'zh': zh, 'source': '+'.join(sources)}

    def lookup(self, ids=(), folders=()):
        wanted = []
        for raw in list(ids)[:MAX_IDS]:
            title_id = extract_id(raw)
            if title_id and title_id not in wanted:
                wanted.append(title_id)
        checked = []
        for folder in list(folders)[:MAX_FOLDERS]:
            title_id = extract_id(posixpath.basename((folder or '').rstrip('/')))
            if not title_id:
                continue
            if title_id not in wanted:
                wanted.append(title_id)
            builtin = self.builtin().get(title_id) or []
            # Only ask the console when the catalog lacks the name or its Chinese form.
            if len(builtin) < 2:
                checked.append(folder.rstrip('/'))
        pending = self._scan(checked)
        titles = {}
        for title_id in wanted:
            found = self.entry(title_id)
            if found:
                titles[title_id] = found
        # pending: folders not read this time (budget or console offline); ask again later.
        return {'titles': titles, 'pending': pending}
