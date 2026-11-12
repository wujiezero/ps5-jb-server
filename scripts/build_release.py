#!/usr/bin/env python3
"""Build deterministic runtime-only release artifacts, without credentials/data."""
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
    """Front-end files to ship: index.html, app.js, style.css plus any matching the shared rule."""
    pattern = re.compile(web_file_pattern(root) + r'\Z')
    return sorted(path for path in (Path(root) / 'web').iterdir()
                  if path.is_file() and pattern.match(path.name))


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
    paths += web_files(root)
    if not all((root / 'web' / name).is_file() for name in ('index.html', 'app.js', 'style.css')):
        raise ValueError('安装包不完整：缺少前端文件')
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
