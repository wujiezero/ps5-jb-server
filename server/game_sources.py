"""Pure, bounded planning of game installation sources on a NAS.

This module deliberately never opens a NAS file.  It only consumes directory
listings (and, when available, stat metadata), leaving archive validation and
package/layout inspection to later stages.
"""
import posixpath
import re
from collections import deque

import archive

# NAS clients currently cap a page at 5000 entries; at that boundary the
# planner must refuse to infer uniqueness even if the client omitted a flag.
MAX_ENTRIES = 5_000
MAX_DEPTH = 32
MAX_DIRECTORIES = 1_000

_TITLE_ID = re.compile(r"(?<![A-Z])([A-Z]{4}\d{5})(?!\d)", re.I)
_PART = re.compile(r"^(?P<base>.+)\.part(?P<num>\d{1,3})\.rar$", re.I)
_RAR = re.compile(r"^(?P<base>.+)\.rar$", re.I)
_OLD_RAR = re.compile(r"^.+\.(?:r\d{2}|s\d{2})$", re.I)
_CHEAT = re.compile(r"(?:金手指|cheat|trainer)", re.I)


class SourceError(ValueError):
    """A user-fixable source selection or NAS listing error."""


def _clean(path):
    if not isinstance(path, str) or not path or not path.startswith('/'):
        raise SourceError('路径必须是 NAS 绝对路径')
    if any(ord(char) < 32 for char in path) or '\\' in path:
        raise SourceError('路径包含非法字符或不安全分隔符')
    parts = path.split('/')
    if any(part in ('.', '..') for part in parts):
        raise SourceError('路径不允许包含 . 或 ..')
    result = '/' + '/'.join(part for part in parts if part)
    return result or '/'


def _ignored(name):
    return name.startswith('._') or name == '__MACOSX'


def _entry_path(parent, entry):
    name = entry.get('name')
    if not isinstance(name, str) or not name or '/' in name or name in ('.', '..'):
        raise SourceError('NAS 列表包含非法名称')
    if _ignored(name):
        return None
    expected = posixpath.join(parent, name)
    supplied = entry.get('path')
    if supplied is not None:
        try:
            supplied = _clean(supplied)
        except SourceError:
            raise SourceError('NAS 列表返回了非法路径') from None
        if supplied != expected:
            raise SourceError('NAS 列表返回了越界路径')
    return expected


def _list(client, path):
    listing = client.list(path)
    truncated = False
    if isinstance(listing, dict):
        truncated = bool(listing.get('truncated'))
        listing = listing.get('entries', [])
    if not isinstance(listing, (list, tuple)):
        raise SourceError('NAS 列表格式不正确')
    # A capped page cannot prove that a target is unique.  Reject at the cap.
    if truncated or len(listing) >= MAX_ENTRIES:
        raise SourceError('NAS 列表被截断或达到条目上限，无法安全判断唯一游戏')
    result = []
    for raw in listing:
        if not isinstance(raw, dict):
            raise SourceError('NAS 列表格式不正确')
        path_value = _entry_path(path, raw)
        if path_value is None:
            continue
        result.append({
            'name': raw['name'],
            'path': path_value,
            'dir': bool(raw.get('dir')),
            'size': raw.get('size'),
        })
    return result


def _optional_stat(client, path):
    stat = getattr(client, 'stat', None)
    if not callable(stat):
        return None
    try:
        value = stat(path)
    except Exception as exc:  # NAS-specific errors become a stable user error.
        raise SourceError(f'找不到 NAS 路径：{path}') from exc
    if not isinstance(value, dict):
        raise SourceError('NAS stat 返回格式不正确')
    return value


def _title_hint(text):
    match = _TITLE_ID.search(text or '')
    return match.group(1).upper() if match else ''


def _base_and_group(name):
    match = _PART.match(name)
    if match:
        return match.group('base'), match.group('base').casefold()
    match = _RAR.match(name)
    if match:
        return match.group('base'), match.group('base').casefold()
    if _OLD_RAR.match(name):
        return name.rsplit('.', 1)[0], name.rsplit('.', 1)[0].casefold()
    return '', ''


def _archive_result(client, rar_entry, parent_entries, explicit_zips=(), hint_text=''):
    try:
        base, volumes = archive.volume_set(parent_entries, rar_entry['name'])
    except Exception as exc:
        raise SourceError(str(exc)) from exc
    volume_paths = [entry['path'] for entry in volumes]
    parent = posixpath.dirname(volume_paths[0])
    auto = [entry['path'] for entry in parent_entries
            if not entry['dir'] and entry['path'].lower().endswith('.zip')
            and _CHEAT.search(entry['name'])]
    cheats = sorted(set(auto).union(explicit_zips))
    return {
        'kind': 'archive',
        'path': volume_paths[0],
        'base': base,
        'cheats': cheats,
        'title_hint': (_title_hint(base) or _title_hint(volume_paths[0]) or
                       _title_hint(rar_entry['name']) or _title_hint(hint_text)),
        'volumes': volume_paths,
    }


def _scan_folder(client, root):
    queue = deque([(root, 0)])
    files = []
    directories = [root]
    scanned = 0
    while queue:
        path, depth = queue.popleft()
        scanned += 1
        if scanned > MAX_DIRECTORIES:
            raise SourceError('目录遍历达到上限，无法安全判断唯一游戏')
        entries = _list(client, path)
        for entry in entries:
            if entry['dir']:
                if depth + 1 > MAX_DEPTH:
                    raise SourceError('目录遍历超过深度上限，无法安全判断唯一游戏')
                # __MACOSX and ._ were filtered by _entry_path.
                queue.append((entry['path'], depth + 1))
                directories.append(entry['path'])
            else:
                files.append(entry)
    return files, directories


def _game_roots(files):
    by_path = {entry['path'].casefold(): entry['path'] for entry in files}
    roots = set()
    for entry in files:
        if entry['name'].casefold() != 'eboot.bin':
            continue
        root = posixpath.dirname(entry['path'])
        marker_prefix = root.rstrip('/') + '/sce_sys/'
        marker = any(path.startswith(marker_prefix.casefold()) and
                     path.rsplit('/', 1)[-1].casefold() in ('param.json', 'param.sfo', 'sfo')
                     for path in by_path)
        if marker:
            roots.add(root)
    return sorted(roots)


def _groups(files):
    result = {}
    for entry in files:
        if entry['dir']:
            continue
        base, group = _base_and_group(entry['name'])
        if group:
            key = (posixpath.dirname(entry['path']), group)
            result.setdefault(key, []).append(entry)
    return result


def _package_files(files):
    return sorted((entry for entry in files
                   if not entry['dir'] and entry['name'].casefold().endswith('.pkg')),
                  key=lambda entry: entry['path'])


def _folder_result(root, hint_text=''):
    return {
        'kind': 'folder', 'path': root, 'base': posixpath.basename(root),
        'cheats': [], 'title_hint': _title_hint(hint_text) or _title_hint(root),
    }


def _package_result(entry):
    stem = entry['name'][:-4]
    return {
        'kind': 'package', 'path': entry['path'], 'base': stem,
        'cheats': [], 'title_hint': _title_hint(entry['path']) or _title_hint(entry['name']),
    }


def plan_sources(client, paths):
    """Plan one safe source from selected NAS paths without reading file bytes."""
    if not isinstance(paths, (list, tuple)) or not paths:
        raise SourceError('请选择一个游戏文件、文件夹或归档')
    selected = []
    for path in paths:
        clean = _clean(path)
        if clean not in selected:
            selected.append(clean)
    stats = {path: _optional_stat(client, path) for path in selected}
    for path in selected:
        if _ignored(posixpath.basename(path)):
            raise SourceError('这是 macOS 元数据文件，不是可安装游戏源')
    selected_dirs = [path for path, stat in stats.items() if stat and stat.get('dir')]
    selected_files = [path for path, stat in stats.items() if stat and not stat.get('dir')]
    unknown = [path for path, stat in stats.items() if stat is None]
    if unknown:
        # Clients without stat are still supported: resolve each path by its parent listing.
        for path in unknown:
            parent, name = posixpath.dirname(path) or '/', posixpath.basename(path)
            matches = [entry for entry in _list(client, parent) if entry['name'] == name]
            if not matches:
                raise SourceError(f'找不到 NAS 路径：{path}')
            stats[path] = {'dir': matches[0]['dir'], 'size': matches[0].get('size')}
            (selected_dirs if matches[0]['dir'] else selected_files).append(path)
    if selected_dirs:
        if len(selected_dirs) != 1:
            raise SourceError('一次不能选择多个游戏文件夹')
        if selected_files:
            raise SourceError('游戏文件夹不能与其他目标混选')
        root = selected_dirs[0]
        files, _ = _scan_folder(client, root)
        groups = _groups(files)
        game_roots = _game_roots(files)
        if len(groups) > 1:
            raise SourceError('文件夹中发现多个不同 RAR 游戏目标，请只保留一个')
        if groups and game_roots:
            raise SourceError('发现多个游戏目标：同时存在 RAR 和已解压游戏目录，目标不明确')
        if groups:
            group_entries = next(iter(groups.values()))
            return _archive_result(client, group_entries[0],
                                   [entry for entry in files
                                    if posixpath.dirname(entry['path']) == posixpath.dirname(group_entries[0]['path'])],
                                   hint_text=root)
        if len(game_roots) > 1:
            raise SourceError('文件夹中发现多个已解压游戏，请一次只选择一个')
        if game_roots:
            return _folder_result(game_roots[0], root)
        packages = _package_files(files)
        if len(packages) == 1:
            return _package_result(packages[0])
        if len(packages) > 1:
            raise SourceError('文件夹中有多个 PKG，请明确选择一个')
        raise SourceError('未识别到游戏：需要 RAR、PKG 或 eboot.bin + sce_sys/param 元数据')

    rar_paths = [path for path in selected_files if path.lower().endswith(('.rar',))]
    old_paths = [path for path in selected_files if _OLD_RAR.match(posixpath.basename(path))]
    rar_paths.extend(old_paths)
    pkg_paths = [path for path in selected_files if path.lower().endswith('.pkg')]
    zip_paths = [path for path in selected_files if path.lower().endswith('.zip')]
    other_paths = [path for path in selected_files
                   if path not in rar_paths + pkg_paths + zip_paths]
    if rar_paths:
        if pkg_paths or other_paths:
            raise SourceError('RAR 只能与同组 RAR 分卷和 ZIP 金手指一起选择')
        groups = {(posixpath.dirname(path), _base_and_group(posixpath.basename(path))[1])
                  for path in rar_paths}
        if len(groups) != 1:
            raise SourceError('选择了多个不同 RAR 游戏目标')
        parent = posixpath.dirname(rar_paths[0]) or '/'
        entries = _list(client, parent)
        by_name = {entry['name']: entry for entry in entries}
        name = posixpath.basename(rar_paths[0])
        if name not in by_name:
            raise SourceError(f'找不到 NAS 路径：{rar_paths[0]}')
        return _archive_result(client, by_name[name], entries, zip_paths)
    if pkg_paths:
        if len(pkg_paths) != 1 or zip_paths:
            raise SourceError('请选择一个 PKG；多个 PKG 不能自动判断安装顺序')
        return _package_result({'path': pkg_paths[0], 'name': posixpath.basename(pkg_paths[0])})
    raise SourceError('未识别到游戏，请选择 RAR、PKG 或已解压游戏文件夹')
