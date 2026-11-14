"""Game library: the games ShadowMount+ would find on the console.

Where to look comes from ShadowMount+'s own config, /data/shadowmount/config.ini:
every `path =` line under [scan] (or the legacy flat `scanpath=` lines) and the
scan `depth` (1 = entries of each path, 2 = one folder further). Without paths it
uses ShadowMount+'s built-in roots; without the config file at all (ShadowMount+
not installed) those roots plus [ps5] game_dirs on every volume. Only paths on
/data, /mnt/ext* and /mnt/usb* can be read; others (SMB shares, the image mounts
under /mnt/shadowmnt) are reported as skipped.

Each scan path is walked like ShadowMount+ does: a folder with a title ID in its
name or a sce_sys/param file is a game (not descended into), an .ffpkg / .exfat /
.ffpfs / .ffpfsc file is an image game, dot entries and the root's backports/
folder are ignored, and a folder that is itself another scan path is left to its
own scan. Folder sizes are computed only when asked, one at a time, and remembered
by path + modification time in <data>/library-cache.json; an image's size is in
its listing. Covers come from sce_sys/icon0.png or ShadowMount+'s icon cache
(/data/shadowmount/icon-cache), checked to be a PNG or JPEG of at most 2 MiB, and
are kept in <data>/library-icons/.

Games installed from PKGs live in /user/app, outside the file whitelist, and
are not listed here.
"""
import hashlib
import json
import logging
import os
import posixpath
import threading
import time
from pathlib import Path

from files import VOLUME_PATTERN, FileServiceError
from settings import DEFAULT_GAME_DIRS, clean_dirs
from titles import extract_id

SCAN_TTL = 60
MAX_GAMES = 1000
MAX_ICON = 2 << 20
MAX_ICONS = 1000                 # cached covers kept; the oldest go first
PNG = b'\x89PNG\r\n\x1a\n'
JPEG = b'\xff\xd8\xff'
PARAMS = ('param.json', 'param.sfo')
# Disc and digital prefixes of PS5 titles; every other known prefix is PS4.
PS5_PREFIXES = ('PPSA', 'ELAS', 'ECAS', 'ELJS', 'ECJS', 'ELJM', 'ECJM', 'ELKS', 'ECKS')

SMP_CONFIG = '/data/shadowmount/config.ini'
SMP_ICONS = '/data/shadowmount/icon-cache'
SMP_MOUNTS = '/mnt/shadowmnt'           # where ShadowMount+ mounts images; never scanned here
SMP_MAX_CONFIG = 256 << 10
SMP_MAX_PATHS = 256
SMP_DEPTHS = (1, 2)
SMP_BACKPORTS = 'backports'
# ShadowMount+'s built-in scan roots, used when config.ini names none.
SMP_DEFAULT_ROOTS = (('/data/homebrew', '/data/etaHEN/games')
                     + tuple(f'/mnt/ext{n}/{d}' for n in (0, 1) for d in ('homebrew', 'etaHEN/games'))
                     + tuple(f'/mnt/usb{n}/{d}' for n in range(8) for d in ('homebrew', 'etaHEN/games'))
                     + tuple(f'/mnt/usb{n}' for n in range(8)) + ('/mnt/ext0', '/mnt/ext1'))
IMAGE_TYPES = ('.ffpkg', '.exfat', '.ffpfs', '.ffpfsc')


class LibraryError(ValueError):
    pass


def platform(title):
    if not title:
        return ''
    if title.startswith('NPXS'):
        return ''
    return 'PS5' if title.startswith(PS5_PREFIXES) else 'PS4'


def image_type(name):
    """'ffpfsc' for 'X.FFPFSC', '' for anything ShadowMount+ does not mount."""
    lower = (name or '').lower()
    return next((kind[1:] for kind in IMAGE_TYPES if lower.endswith(kind) and len(lower) > len(kind)), '')


def _ini_value(raw):
    """A config.ini value as ShadowMount+ reads it: "quoted" with \\ escapes, else up to # or ;."""
    raw = raw.strip()
    if raw.startswith('"'):
        out, index = [], 1
        while index < len(raw) and raw[index] != '"':
            if raw[index] == '\\':
                index += 1
                if index >= len(raw) or raw[index] not in '\\"':
                    return None
            out.append(raw[index])
            index += 1
        if index >= len(raw):
            return None
        tail = raw[index + 1:].strip()
        return ''.join(out) if not tail or tail[0] in '#;' else None
    for mark in '#;':
        if mark in raw:
            raw = raw[:raw.index(mark)]
    return raw.strip()


def parse_smp_config(text):
    """{'paths': [...], 'depth': 1|2|None} from config.ini text (v2 [scan] or legacy flat keys)."""
    section, paths, depth = '', [], None
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in '#;':
            continue
        if line.startswith('['):
            if ']' in line:
                section = line[1:line.index(']')].strip().lower()
            continue
        if '=' not in line:
            continue
        key, raw = line.split('=', 1)
        key = key.strip().lower()
        if (section, key) in (('scan', 'path'), ('', 'scanpath')):
            value = _ini_value(raw)
            if value:
                paths.append(value)
        elif (section, key) in (('scan', 'depth'), ('', 'scan_depth')):
            value = _ini_value(raw) or ''
            if value.isdigit() and int(value) in SMP_DEPTHS:
                depth = int(value)
    return {'paths': paths, 'depth': depth}


def _normal_root(path):
    """A scan path the way ShadowMount+ stores it: trimmed, no trailing slash."""
    path = (path or '').strip()
    while len(path) > 1 and path.endswith('/'):
        path = path[:-1]
    return path


def _readable_root(path):
    """Whether this server may list path: a clean absolute path on /data, /mnt/ext*, /mnt/usb*."""
    return (path.startswith('/') and '\x00' not in path and posixpath.normpath(path) == path
            and not any(part in ('.', '..') for part in path.split('/')) and bool(VOLUME_PATTERN.match(path)))


def _volume_of(path):
    match = VOLUME_PATTERN.match(path)
    return match.group(0).rstrip('/') if match else ''


def _atomic_json(path, document):
    temporary = path.with_name(path.name + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(document, stream, ensure_ascii=False)
    os.replace(temporary, path)


class Library:
    def __init__(self, files, data_dir, game_dirs=DEFAULT_GAME_DIRS, titles=None, clock=time.time):
        self.files = files
        self.titles = titles              # titles.TitleCatalog, set once the server has one
        self.game_dirs = clean_dirs(list(game_dirs))
        self.clock = clock
        self.cache_path = Path(data_dir) / 'library-cache.json'
        self.icon_dir = Path(data_dir) / 'library-icons'
        self.lock = threading.Lock()
        self.size_lock = threading.Lock()     # one folder size at a time: FTP walks are slow
        self.last = None                      # (time, host, result)
        self.covers = {}                      # title -> path in ShadowMount+'s icon cache, per scan
        self.sizes = self._load_sizes()

    # ---- sizes cache ----
    def _load_sizes(self):
        try:
            data = json.loads(self.cache_path.read_text(encoding='utf-8'))
            sizes = data.get('sizes') if isinstance(data, dict) else None
            return {k: v for k, v in (sizes or {}).items() if isinstance(v, dict) and isinstance(k, str)}
        except (OSError, ValueError):
            return {}

    def _save_sizes(self):
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_json(self.cache_path, {'sizes': self.sizes})
        except OSError as exc:
            logging.warning('library: cannot write %s: %s', self.cache_path, exc)

    # ---- ShadowMount+ config ----
    def _read_text(self, path, limit):
        with self.files.stream(path) as stream:
            data = stream.read(limit + 1)
            while len(data) <= limit:
                more = stream.read(limit + 1 - len(data))
                if not more:
                    break
                data += more
        if len(data) > limit:
            raise LibraryError(f'{path} 太大')
        return data.decode('utf-8', 'replace')

    def scan_config(self, volumes):
        """Where to look: {source, file, depth, roots, skipped, error}.

        source is 'config' (paths from config.ini), 'default' (config.ini without
        usable paths: ShadowMount+'s built-in roots) or 'missing' (no config.ini).
        """
        config = {'file': SMP_CONFIG, 'depth': 1, 'error': ''}
        try:
            parsed = parse_smp_config(self._read_text(SMP_CONFIG, SMP_MAX_CONFIG))
            config['source'] = 'config'
        except FileServiceError as exc:
            if exc.status != 404:
                config['error'] = f'读不到 ShadowMount+ 配置（{str(exc)[:120]}），先按它的默认路径扫描'
            parsed = {'paths': [], 'depth': None}
            config['source'] = 'missing' if exc.status == 404 else 'default'
        except (LibraryError, ValueError) as exc:
            config['error'] = f'ShadowMount+ 配置读取失败（{str(exc)[:120]}），先按它的默认路径扫描'
            parsed = {'paths': [], 'depth': None}
            config['source'] = 'default'
        config['depth'] = parsed['depth'] or 1
        roots = []
        for path in parsed['paths']:
            path = _normal_root(path)
            if path and path not in roots and len(roots) < SMP_MAX_PATHS:
                roots.append(path)
        if not roots:
            if config['source'] == 'config':
                config['source'] = 'default'
            roots = list(SMP_DEFAULT_ROOTS)
            if config['source'] == 'missing':
                for volume in volumes:
                    for folder in self.game_dirs:
                        path = f'{volume}/{folder}'
                        if path not in roots:
                            roots.append(path)
        mounted = set(volumes)
        config['roots'] = [path for path in roots if _readable_root(path) and _volume_of(path) in mounted]
        # Roots on an absent USB drive are just not there; anything else unreadable is worth a note.
        config['skipped'] = [path for path in roots if not _readable_root(path)
                             and not (path == SMP_MOUNTS or path.startswith(SMP_MOUNTS + '/'))]
        return config

    # ---- scanning ----
    def _entries(self, path):
        try:
            return self.files.list(path).get('entries', [])
        except FileServiceError as exc:
            if exc.status == 404:
                return None
            raise

    def _has_param(self, folder):
        try:
            entries = self._entries(posixpath.join(folder, 'sce_sys'))
        except FileServiceError:
            return False
        names = {entry.get('name', '').lower() for entry in entries or ()}
        return any(name in names for name in PARAMS)

    def _candidate(self, entry, path, volume, label, root, image=''):
        name = entry.get('name', '')
        title = extract_id(name) or ''
        size = entry.get('size') if image else None
        return {'title': title, 'name': name, 'folder': name, 'path': path, 'volume': volume,
                'volume_label': label, 'scan_root': root, 'kind': 'image' if image else 'folder',
                'format': image, 'platform': platform(title), 'mtime': int(entry.get('mtime') or 0),
                'size': int(size) if isinstance(size, (int, float)) and size >= 0 else None}

    def _walk(self, root, folder, remaining, roots, context):
        """Games under folder (listed), as ShadowMount+'s scan tree walk would find them."""
        entries = self._entries(folder)
        if entries is None:
            return False
        for entry in entries:
            name = entry.get('name') or ''
            if not name or name.startswith('.') or '/' in name:
                continue
            if folder == root and name == SMP_BACKPORTS and entry.get('dir'):
                continue
            if len(context['games']) >= MAX_GAMES:
                return True
            path = posixpath.join(folder, name)
            if path in context['seen']:
                continue
            kind = '' if entry.get('dir') else image_type(name)
            if kind:
                context['seen'].add(path)
                context['games'].append(self._candidate(entry, path, context['volume'], context['label'], root, kind))
                continue
            if not entry.get('dir') or path in roots:
                continue
            if extract_id(name) or self._has_param(path):
                context['seen'].add(path)
                context['games'].append(self._candidate(entry, path, context['volume'], context['label'], root))
            elif remaining > 1:
                self._walk(root, path, remaining - 1, roots, context)
        return True

    def _icon_index(self):
        """title -> newest cover in ShadowMount+'s icon cache (<TITLE>-v3-256-<hash>.jpg)."""
        try:
            entries = self._entries(SMP_ICONS) or []
        except (FileServiceError, ValueError):
            return {}
        best = {}
        for entry in entries:
            name = entry.get('name') or ''
            title = extract_id(name.split('-', 1)[0]) if '-' in name else None
            if entry.get('dir') or not title or not name.lower().endswith(('.jpg', '.jpeg', '.png')):
                continue
            if title not in best or int(entry.get('mtime') or 0) >= best[title][0]:
                best[title] = (int(entry.get('mtime') or 0), posixpath.join(SMP_ICONS, name))
        return {title: path for title, (_, path) in best.items()}

    def scan(self, refresh=False):
        host = getattr(self.files, 'host', None)
        with self.lock:
            if (not refresh and self.last and self.last[1] == host
                    and self.clock() - self.last[0] < SCAN_TTL):
                return {**self.last[2], 'cached': True}
        volumes = self.files.volumes().get('volumes', [])
        labels = {(v.get('path') or '').rstrip('/'): v.get('label') or v.get('path') for v in volumes}
        labels.pop('', None)
        config = self.scan_config(list(labels))
        context = {'games': [], 'seen': set()}
        errors = [config['error']] if config['error'] else []
        scan_paths = []
        roots = set(config['roots'])
        for root in config['roots']:
            volume = _volume_of(root)
            context.update(volume=volume, label=labels.get(volume) or volume)
            before = len(context['games'])
            try:
                exists = self._walk(root, root, config['depth'], roots, context)
            except (FileServiceError, ValueError) as exc:
                errors.append(f'{root}：{str(exc)[:160]}')
                exists = True
            scan_paths.append({'path': root, 'volume': volume, 'volume_label': context['label'],
                               'exists': exists, 'games': len(context['games']) - before})
        games = context['games']
        self._names(games)
        for game in games:
            cached = self.sizes.get(game['path'])
            if game['size'] is None and cached and cached.get('mtime') == game['mtime']:
                game['size'] = cached.get('size')
        games.sort(key=lambda game: (game['name'].lower(), game['path']))
        covers = self._icon_index() if any(game['title'] for game in games) else {}
        result = {'host': host, 'games': games, 'errors': errors, 'scanned': self.clock(),
                  'truncated': len(games) >= MAX_GAMES, 'scan_paths': scan_paths,
                  'scan_config': {key: config[key] for key in ('source', 'file', 'depth', 'skipped')},
                  'volumes': [{'path': (v.get('path') or '').rstrip('/'), 'label': v.get('label') or v.get('path'),
                               'free': v.get('free'), 'total': v.get('total')} for v in volumes]}
        with self.lock:
            self.last = (self.clock(), host, result)
            self.covers = covers
        return {**result, 'cached': False}

    def _names(self, games):
        if self.titles is None or not games:
            return
        try:
            found = self.titles.lookup(ids=[g['title'] for g in games if g['title']],
                                       folders=[g['path'] for g in games if g['title']])
        except Exception as exc:      # names are a nicety; the list must still show
            logging.info('library: title lookup failed: %s', exc)
            return
        titles = found.get('titles') or {}
        for game in games:
            entry = titles.get(game['title'])
            if entry and entry.get('name'):
                game['name'] = entry['name']

    def known(self, path):
        """The scanned game at path (refreshing the scan once), else LibraryError."""
        if not isinstance(path, str):
            raise LibraryError('请选择一个游戏')
        path = path.rstrip('/')
        for refresh in (False, True):
            for game in self.scan(refresh=refresh)['games']:
                if game['path'] == path:
                    return game
        raise LibraryError('游戏库里没有这个游戏，请刷新后重试')

    def scan_path(self, path):
        """A readable scan path from the last scan, else LibraryError."""
        path = _normal_root(path if isinstance(path, str) else '')
        for item in self.scan()['scan_paths']:
            if item['path'] == path:
                return item
        raise LibraryError('这不是 ShadowMount+ 的扫描路径，请刷新游戏库')

    # ---- size ----
    def size(self, path):
        game = self.known(path)
        if game['kind'] == 'image' and game['size'] is not None:
            return {'path': game['path'], 'size': game['size'], 'files': 1, 'cached': True}
        cached = self.sizes.get(game['path'])
        if cached and cached.get('mtime') == game['mtime']:
            return {'path': game['path'], 'size': cached['size'], 'files': cached.get('files'), 'cached': True}
        if not self.size_lock.acquire(timeout=1):
            raise LibraryError('正在计算另一个游戏的大小，请稍候')
        try:
            result = self.files.folder_size(game['path'])
        finally:
            self.size_lock.release()
        size = int(result.get('size') or 0)
        with self.lock:
            self.sizes[game['path']] = {'mtime': game['mtime'], 'size': size, 'files': result.get('files')}
            self._save_sizes()
        return {'path': game['path'], 'size': size, 'files': result.get('files'), 'cached': False}

    # ---- icons ----
    def _read_cover(self, path):
        try:
            with self.files.stream(path) as stream:
                data = stream.read(MAX_ICON + 1)
                while len(data) <= MAX_ICON:
                    more = stream.read(MAX_ICON + 1 - len(data))
                    if not more:
                        break
                    data += more
        except FileServiceError as exc:
            if exc.status == 404:
                return None
            raise
        if len(data) > MAX_ICON:
            raise LibraryError('封面超过 2 MiB')
        if not data.startswith((PNG, JPEG)):
            raise LibraryError('封面不是 PNG 或 JPEG')
        return data

    def icon(self, path):
        """(image bytes, etag, content type) for a scanned game's cover."""
        game = self.known(path)
        key = hashlib.sha256(f'{game["path"]}\0{game["mtime"]}'.encode()).hexdigest()[:32]
        for suffix, ctype in (('.png', 'image/png'), ('.jpg', 'image/jpeg')):
            try:
                return (self.icon_dir / f'{key}{suffix}').read_bytes(), f'"{key}"', ctype
            except OSError:
                pass
        data = None
        if game['kind'] == 'folder':
            data = self._read_cover(posixpath.join(game['path'], 'sce_sys', 'icon0.png'))
        with self.lock:
            cover = self.covers.get(game['title'])
        if data is None and cover:
            data = self._read_cover(cover)
        if data is None:
            raise LibraryError('这个游戏没有封面')
        suffix, ctype = ('.png', 'image/png') if data.startswith(PNG) else ('.jpg', 'image/jpeg')
        try:
            self.icon_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            cached = self.icon_dir / f'{key}{suffix}'
            temporary = cached.with_name(cached.name + '.tmp')
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
            os.replace(temporary, cached)
            icons = sorted([*self.icon_dir.glob('*.png'), *self.icon_dir.glob('*.jpg')],
                           key=lambda item: item.stat().st_mtime)
            for old in icons[:max(0, len(icons) - MAX_ICONS)]:
                old.unlink(missing_ok=True)
        except OSError as exc:
            logging.warning('library: cannot cache icon: %s', exc)
        return data, f'"{key}"', ctype

    def forget(self):
        """Drop the scan cache (after a delete or move)."""
        with self.lock:
            self.last = None
