"""Recognize one runnable game from extracted relative paths and small metadata.

No console or archive engine is involved. A root needs both eboot.bin and its
own sce_sys/param.json or param.sfo; package names are never identity authority.
"""
import json
import posixpath
import re
import struct


class GameLayoutError(ValueError):
    """The apparent game cannot be identified safely."""


class NoGameError(GameLayoutError):
    """No eboot + parameter-file pair exists (plain files, PKGs or a patch)."""


_TITLE = re.compile(r'(?:PPSA|CUSA)\d{5}\Z')
_HINT = re.compile(r'(?<![A-Z0-9])(?:PPSA|CUSA)\d{5}(?![A-Z0-9])', re.I)
_PARAMS = ('sce_sys/param.json', 'sce_sys/param.sfo')


def _relative(path):
    path = path.replace('\\', '/')
    parts = [part for part in path.split('/') if part not in ('', '.')]
    if (not parts or path.startswith('/') or '..' in parts or '\x00' in path
            or re.match(r'^[A-Za-z]:', path)):
        raise GameLayoutError('游戏清单里有不安全的相对路径')
    return '/'.join(parts)


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _json(data):
    param = json.loads(data.decode('utf-8-sig'))
    if not isinstance(param, dict):
        raise ValueError('param.json must contain an object')
    localized = param.get('localizedParameters') or {}
    name = ''
    if isinstance(localized, dict):
        language = localized.get('defaultLanguage')
        selected = localized.get(language) if isinstance(language, str) else None
        if isinstance(selected, dict):
            name = _text(selected.get('titleName'))
    return {'title': _text(param.get('titleId')), 'platform': 'ps5',
            'version': _text(param.get('contentVersion')), 'name': name}


def _sfo(data):
    if len(data) < 20:
        raise ValueError('truncated SFO header')
    magic, version, keys, values, count = struct.unpack_from('<4sIIII', data)
    table_end = 20 + count * 16
    if (magic != b'\0PSF' or version != 0x101
            or not table_end <= keys <= values <= len(data)):
        raise ValueError('invalid SFO header')
    fields = {}
    for index in range(count):
        key_offset, fmt, length, capacity, offset = struct.unpack_from('<HHIII', data, 20 + index * 16)
        key_start, start = keys + key_offset, values + offset
        if not keys <= key_start < values:
            raise ValueError('invalid SFO key offset')
        end = data.find(b'\0', key_start, values)
        if end == -1 or length > capacity or start + capacity > len(data):
            raise ValueError('invalid SFO value bounds')
        key = data[key_start:end].decode('utf-8')
        if key in ('TITLE_ID', 'APP_VER', 'TITLE'):
            if key in fields or fmt != 0x0204 or not length:
                raise ValueError('invalid SFO text field')
            raw = data[start:start + length]
            if raw[-1:] != b'\0':
                raise ValueError('unterminated SFO text')
            fields[key] = raw[:-1].decode('utf-8').strip()
    return {'title': fields.get('TITLE_ID', ''), 'platform': 'ps4',
            'version': fields.get('APP_VER', ''), 'name': fields.get('TITLE', '')}


def resolve_game(paths, metadata, hint=''):
    """Return {root, title, platform, version, name} for one runnable game.

    `paths` contains extracted relative file paths, and `metadata` maps relative
    parameter-file paths to bytes. `root` is '' for an archive with a bare game
    root. NoGameError permits ordinary extraction/patch merging; other
    GameLayoutError failures must stop installation. Explicit title IDs in
    `hint` must agree with the metadata. Missing versions/names remain ''.
    """
    paths = {_relative(path) for path in paths}
    roots = {}
    for path in sorted(paths):
        for suffix in _PARAMS:
            if path == suffix or path.endswith('/' + suffix):
                root = path[:-len(suffix)].rstrip('/')
                eboot = posixpath.join(root, 'eboot.bin')
                if eboot in paths:
                    roots.setdefault(root, []).append(path)
    if not roots:
        raise NoGameError('没有找到同一游戏根目录下的 eboot.bin 和 sce_sys/param.json 或 param.sfo')
    if len(roots) != 1:
        raise GameLayoutError('压缩包包含多个游戏根目录，无法自动选择')
    root, params = next(iter(roots.items()))
    normalized_metadata = {_relative(path): data for path, data in metadata.items()}
    records = []
    for path in params:
        try:
            data = normalized_metadata[path]
            record = _json(data) if path.endswith('.json') else _sfo(data)
        except (KeyError, ValueError, UnicodeError, struct.error, AttributeError, TypeError, RecursionError):
            raise GameLayoutError(f'游戏元数据缺失或损坏：{path}') from None
        expected_prefix = 'PPSA' if record['platform'] == 'ps5' else 'CUSA'
        if not _TITLE.fullmatch(record['title']) or not record['title'].startswith(expected_prefix):
            raise GameLayoutError(f'游戏元数据没有有效的 title ID：{path}')
        records.append(record)
    game = records[0]
    if any(record['title'] != game['title'] or record['platform'] != game['platform']
           for record in records[1:]):
        raise GameLayoutError('游戏元数据中的 title ID 或平台冲突')
    hints = {match.upper() for match in _HINT.findall(hint)}
    if hints - {game['title']}:
        raise GameLayoutError(f'游戏元数据 {game["title"]} 与指定 title ID 冲突')
    return dict(root=root, **game)
