#!/usr/bin/env python3
"""Build deterministic runtime-only release artifacts, without credentials/data.

The front end is developed as several files (web/index.html lists them in load
order) but ships as the three historical ones: the scripts are joined into
app.js and the stylesheets into style.css, and index.html is rewritten to load
just those two. Update workers up to v1.9.0 accept only web/index.html,
web/app.js and web/style.css, so a package with any other web file could never
be installed from those versions by the web updater.
"""
import argparse
import ast
import gzip
import hashlib
import io
from pathlib import Path
import re
import tarfile


def source_version(root):
    tree = ast.parse((root / 'server/version.py').read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'VERSION' for t in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str) and re.fullmatch(r'\d+\.\d+\.\d+', value):
                return value
    raise ValueError('缺少有效版本号 VERSION')


def web_files(root):
    """Front-end source files: everything in web/ matching the shared file-name rule."""
    pattern = re.compile(web_file_pattern(root) + r'\Z')
    return sorted(path for path in (Path(root) / 'web').iterdir()
                  if path.is_file() and pattern.match(path.name))


BUNDLED = ('index.html', 'app.js', 'style.css')
_STYLE = re.compile(r'[ \t]*<link rel="stylesheet" href="/([a-z0-9][a-z0-9._-]{0,63}\.css)">[ \t]*\n')
_SCRIPT = re.compile(r'[ \t]*<script src="/([a-z0-9][a-z0-9._-]{0,63}\.js)" defer></script>[ \t]*\n')
STRICT = "'use strict';\n"


def _collapse(html, pattern, first, last, tag):
    """Replace the consecutive run of tags matching pattern with one tag; return (html, names)."""
    matches = list(pattern.finditer(html))
    if not matches:
        raise ValueError(f'index.html 没有引用 {first or last}')
    for before, after in zip(matches, matches[1:]):
        if before.end() != after.start():
            raise ValueError('index.html 中同类引用必须连续')
    names = [match.group(1) for match in matches]
    if len(set(names)) != len(names) or (first and names[0] != first) or (last and names[-1] != last):
        raise ValueError(f'index.html 的引用顺序不对：{names}')
    indent = re.match(r'[ \t]*', matches[0].group(0)).group(0)
    return html[:matches[0].start()] + indent + tag + '\n' + html[matches[-1].end():], names


def bundle_web(root):
    """{name: bytes} for index.html, app.js and style.css built from web/."""
    web = Path(root) / 'web'
    html = (web / 'index.html').read_text(encoding='utf-8')
    if html.count('<script') != len(_SCRIPT.findall(html)) or html.count('rel="stylesheet"') != len(_STYLE.findall(html)):
        raise ValueError('index.html 只能用 <script src="/x.js" defer> 与 <link rel="stylesheet" href="/x.css"> 引用本地文件')
    html, styles = _collapse(html, _STYLE, 'style.css', None, '<link rel="stylesheet" href="/style.css">')
    html, scripts = _collapse(html, _SCRIPT, None, 'app.js', '<script src="/app.js" defer></script>')
    unused = sorted({path.name for path in web_files(root)} - set(styles) - set(scripts) - {'index.html'})
    if unused:
        raise ValueError('web/ 下有未被 index.html 引用的文件：' + ', '.join(unused))
    parts = []
    for name in scripts:
        text = (web / name).read_text(encoding='utf-8')
        if not text.startswith(STRICT):
            raise ValueError(f'{name} 必须以 {STRICT.strip()} 开头')
        parts.append(text if len(scripts) == 1 else f'/* ── {name} ── */\n' + text[len(STRICT):])
    app = parts[0] if len(scripts) == 1 else STRICT + '\n'.join(parts)
    styles_text = [(web / name).read_text(encoding='utf-8') for name in styles]
    style = styles_text[0] if len(styles) == 1 else '\n'.join(
        f'/* ── {name} ── */\n' + text for name, text in zip(styles, styles_text))
    return {'index.html': html.encode(), 'app.js': app.encode(), 'style.css': style.encode()}


def web_file_pattern(root):
    tree = ast.parse((Path(root) / 'server/version.py').read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'WEB_FILE_PATTERN'
                                                for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError('缺少前端文件名规则 WEB_FILE_PATTERN')


def build_release(root, output, tag=None):
    root, output = Path(root), Path(output)
    version = source_version(root)
    if tag and tag != 'v' + version:
        raise ValueError(f'发布 tag 必须与版本匹配：v{version}')
    paths = sorted((root / 'server').glob('*.py'))
    if not all((root / 'web' / name).is_file() for name in BUNDLED):
        raise ValueError('安装包不完整：缺少前端文件')
    web = bundle_web(root)
    paths += [root / 'rulesets/compiled.json', root / 'titledb/titles.json', root / 'titledb/UPSTREAM_LICENSE']
    required = ('server.py', 'version.py', 'update_worker.py')
    if not all((root / 'server' / name).is_file() for name in required):
        raise ValueError('安装包不完整：缺少运行模块')
    output.mkdir(parents=True, exist_ok=True)
    name = f'ps5-jb-server-{version}.tar.gz'
    target = output / name
    prefix = f'ps5-jb-server-{version}/'
    with target.open('wb') as destination:
        with gzip.GzipFile(filename='', mode='wb', fileobj=destination, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w', format=tarfile.USTAR_FORMAT) as archive:
                entries = [(path.relative_to(root).as_posix(), path.read_bytes()) for path in paths]
                entries += [('web/' + name, data) for name, data in web.items()]
                entries.append(('VERSION', (version + '\n').encode()))
                for relative, data in sorted(entries):
                    info = tarfile.TarInfo(prefix + relative)
                    info.size, info.mode, info.mtime = len(data), 0o644, 0
                    archive.addfile(info, io.BytesIO(data))
    checksum = output / (name + '.sha256')
    checksum.write_text(hashlib.sha256(target.read_bytes()).hexdigest() + '  ' + name + '\n')
    return target, checksum


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('dist'))
    parser.add_argument('--tag')
    args = parser.parse_args()
    for path in build_release(Path(__file__).resolve().parents[1], args.output, args.tag):
        print(path)


if __name__ == '__main__':
    main()
