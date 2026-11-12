"""Game library: game folders on the console's storage, one level deep.

For every volume FileProxy reports, the library lists the volume root (folders
named after a title ID) and each of [ps5] game_dirs (any folder that has a
title ID in its name or a sce_sys/param file). Nothing is read recursively:
folder sizes are computed only when asked, one at a time, and remembered by
path + modification time in <data>/library-cache.json. Covers come from
sce_sys/icon0.png, checked to be a PNG of at most 2 MiB, and are kept in
<data>/library-icons/.

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

from files import FileServiceError
from settings import DEFAULT_GAME_DIRS, clean_dirs
from titles import extract_id

SCAN_TTL = 60
MAX_GAMES = 1000
MAX_ICON = 2 << 20
MAX_ICONS = 1000                 # cached covers kept; the oldest go first
PNG = b'\x89PNG\r\n\x1a\n'
PARAMS = ('param.json', 'param.sfo')
# Disc and digital prefixes of PS5 titles; every other known prefix is PS4.
PS5_PREFIXES = ('PPSA', 'ELAS', 'ECAS', 'ELJS', 'ECJS', 'ELJM', 'ECJM', 'ELKS', 'ECKS')


class LibraryError(ValueError):
    pass


def platform(title):
    if not title:
        return ''
    if title.startswith('NPXS'):
        return ''
    return 'PS5' if title.startswith(PS5_PREFIXES) else 'PS4'


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

    # ---- scanning ----
    def _entries(self, path):
        try:
            return self.files.list(path).get('entries', [])
        except FileServiceError as exc:
            if exc.status == 404:
                return []
            raise

    def _has_param(self, folder):
        try:
            names = {entry.get('name', '').lower() for entry in self._entries(posixpath.join(folder, 'sce_sys'))}
        except FileServiceError:
            return False
        return any(name in names for name in PARAMS)

    def _candidate(self, entry, volume, label):
        title = extract_id(entry.get('name', '')) or ''
        return {'title': title, 'name': entry.get('name', ''), 'folder': entry.get('name', ''),
                'path': entry['path'].rstrip('/'), 'volume': volume, 'volume_label': label,
                'platform': platform(title), 'mtime': int(entry.get('mtime') or 0), 'size': None}

    def scan(self, refresh=False):
        host = getattr(self.files, 'host', None)
        with self.lock:
            if (not refresh and self.last and self.last[1] == host
                    and self.clock() - self.last[0] < SCAN_TTL):
                return {**self.last[2], 'cached': True}
        volumes = self.files.volumes().get('volumes', [])
        games, errors, seen = [], [], set()
        for volume in volumes:
            root = (volume.get('path') or '').rstrip('/')
            if not root:
                continue
            label = volume.get('label') or root
            try:
                for entry in self._entries(root):
                    if entry.get('dir') and extract_id(entry.get('name', '')):
                        games.append(self._candidate(entry, root, label))
                        seen.add(games[-1]['path'])
                for folder in self.game_dirs:
                    for entry in self._entries(f'{root}/{folder}'):
                        if not entry.get('dir') or entry['path'].rstrip('/') in seen:
                            continue
                        if len(games) >= MAX_GAMES:
                            break
                        if extract_id(entry.get('name', '')) or self._has_param(entry['path']):
                            games.append(self._candidate(entry, root, label))
                            seen.add(games[-1]['path'])
            except (FileServiceError, ValueError) as exc:
                errors.append(f'{label}：{str(exc)[:160]}')
        self._names(games)
        for game in games:
            cached = self.sizes.get(game['path'])
            if cached and cached.get('mtime') == game['mtime']:
                game['size'] = cached.get('size')
        games.sort(key=lambda game: (game['name'].lower(), game['path']))
        result = {'host': host, 'games': games, 'errors': errors, 'scanned': self.clock(),
                  'truncated': len(games) >= MAX_GAMES,
                  'volumes': [{'path': (v.get('path') or '').rstrip('/'), 'label': v.get('label') or v.get('path'),
                               'free': v.get('free'), 'total': v.get('total')} for v in volumes]}
        with self.lock:
            self.last = (self.clock(), host, result)
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
        raise LibraryError('游戏库里没有这个文件夹，请刷新后重试')

    # ---- size ----
    def size(self, path):
        game = self.known(path)
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
    def icon(self, path):
        """(png bytes, etag) for a scanned game's sce_sys/icon0.png."""
        game = self.known(path)
        key = hashlib.sha256(f'{game["path"]}\0{game["mtime"]}'.encode()).hexdigest()[:32]
        cached = self.icon_dir / f'{key}.png'
        try:
            return cached.read_bytes(), f'"{key}"'
        except OSError:
            pass
        try:
            with self.files.stream(posixpath.join(game['path'], 'sce_sys', 'icon0.png')) as stream:
                data = stream.read(MAX_ICON + 1)
                while len(data) <= MAX_ICON:
                    more = stream.read(MAX_ICON + 1 - len(data))
                    if not more:
                        break
                    data += more
        except FileServiceError as exc:
            if exc.status == 404:
                raise LibraryError('这个游戏没有封面') from None
            raise
        if len(data) > MAX_ICON:
            raise LibraryError('封面超过 2 MiB')
        if not data.startswith(PNG):
            raise LibraryError('封面不是 PNG')
        try:
            self.icon_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = cached.with_name(cached.name + '.tmp')
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
            os.replace(temporary, cached)
            icons = sorted(self.icon_dir.glob('*.png'), key=lambda item: item.stat().st_mtime)
            for old in icons[:max(0, len(icons) - MAX_ICONS)]:
                old.unlink(missing_ok=True)
        except OSError as exc:
            logging.warning('library: cannot cache icon: %s', exc)
        return data, f'"{key}"'

    def forget(self):
        """Drop the scan cache (after a delete or move)."""
        with self.lock:
            self.last = None
