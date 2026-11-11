#!/usr/bin/env python3
"""Download the payloads listed in payloads.json and build /srv/.../repo.json.

Binaries are third-party projects; this script never bundles them. Each item is
fetched once, verified against its SHA-256, and skipped when already correct.

A failing payload never aborts the run: the reason is printed, the rest of the
manifest continues, and repo.json is written with whatever succeeded. The
installer treats this step as best effort, so a flaky upstream or a mirror
outage only costs the affected plugins until the command is run again.
"""
import argparse
import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

UA = {'User-Agent': 'ps5-jb-server/1.0 (+payload fetcher)'}
CATALOG = 'repo.json'


def sha256_path(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_url(item, download_base=''):
    """Absolute manifest URLs are used as-is; relative ones need a mirror base."""
    url = str(item.get('url') or '')
    if urllib.parse.urlsplit(url).scheme:
        return url
    if not download_base:
        raise ValueError('清单里的相对地址需要 --download-base，或把 url 改成绝对地址')
    return urllib.parse.urljoin(download_base.rstrip('/') + '/', url)


def download_item(item, payload_dir, download_base='', opener=None, timeout=120):
    """Store one payload, returning ('缓存'|'下载', size). Raises on any failure."""
    filename = item['filename']
    target = payload_dir / filename
    checksum = item.get('checksum') or ''
    if checksum and target.is_file() and sha256_path(target) == checksum:
        return '缓存', target.stat().st_size
    url = resolve_url(item, download_base)
    open_url = opener or urllib.request.urlopen
    request = urllib.request.Request(url, headers=UA)
    temporary = payload_dir / ('.' + filename + '.part')
    try:
        with open_url(request, timeout=timeout) as response, temporary.open('wb') as handle:
            for chunk in iter(lambda: response.read(1 << 20), b''):
                handle.write(chunk)
        if checksum and sha256_path(temporary) != checksum:
            raise ValueError('校验和不匹配（上游文件可能已更新）')
        os.replace(temporary, target)
        return '下载', target.stat().st_size
    finally:
        temporary.unlink(missing_ok=True)


def write_catalog(repo_dir, catalog, name='家庭离线仓库'):
    document = {'name': name, 'payloads': catalog}
    temporary = repo_dir / ('.' + CATALOG + '.tmp')
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=1) + '\n',
                         encoding='utf-8')
    os.replace(temporary, repo_dir / CATALOG)


def run(manifest, repo_dir, base_url, only=(), download_base='', opener=None):
    """Fetch every manifest item; per-item failures are collected, never raised."""
    repo_dir = Path(repo_dir)
    payload_dir = repo_dir / 'payloads'
    payload_dir.mkdir(parents=True, exist_ok=True)
    base = base_url.rstrip('/') + '/'
    items = manifest.get('payloads', []) if isinstance(manifest, dict) else manifest
    catalog, failures = [], []
    for item in items:
        if only and item['filename'] not in only:
            continue
        try:
            state, size = download_item(item, payload_dir, download_base, opener)
        except Exception as exc:  # noqa: BLE001 - one bad payload must not stop the rest
            reason = str(exc) or type(exc).__name__
            failures.append((item['filename'], reason))
            print(f'  ! {item["filename"]}: {reason}', file=sys.stderr)
            continue
        catalog.append({**item, 'url': base + item['filename'], 'size': size})
        print(f'  {state:4} {item["filename"]} ({size} bytes)')

    write_catalog(repo_dir, catalog)
    total = len(catalog) + len(failures)
    print(f'插件：成功 {len(catalog)} / 共 {total}，失败 {len(failures)}')
    if failures:
        print('  失败原因见上方日志；稍后重跑本脚本即可补齐。')
    if not catalog:
        print('  ! 插件仓库为空：PS5 接入本服务仓库会报 “No valid payloads found in source JSON”。')
        print(f'  ! 修复：稍后重跑 python3 {Path(__file__).resolve()} '
              f'--repo-dir {repo_dir} --base-url {base}')
    print(f'repo.json: {len(catalog)} 项 -> {repo_dir / CATALOG}')
    return catalog, failures


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', default=str(Path(__file__).resolve().parents[1] / 'payloads.json'))
    parser.add_argument('--repo-dir', required=True, help='directory holding repo.json and payloads/')
    parser.add_argument('--base-url', required=True, help='URL prefix for payload links, e.g. http://192.168.1.10:8080/payloads/')
    parser.add_argument('--download-base', default='', help='mirror base for manifest entries that use a relative url')
    parser.add_argument('--only', action='append', default=[], help='limit to these filenames')
    args = parser.parse_args(argv)
    manifest = json.loads(Path(args.manifest).read_text(encoding='utf-8'))
    run(manifest, Path(args.repo_dir), args.base_url,
        only=tuple(args.only), download_base=args.download_base)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
