#!/usr/bin/env python3
"""Download the payloads listed in payloads.json and build /srv/.../repo.json.

Binaries are third-party projects; this script never bundles them. Each item is
fetched once, verified against its SHA-256, and skipped when already correct.
"""
import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

UA = {'User-Agent': 'ps5-jb-server/1.0 (+payload fetcher)'}


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(item, target, timeout=120):
    if target.exists() and item.get('checksum') and sha256(target) == item['checksum']:
        return 'cached'
    request = urllib.request.Request(item['url'], headers=UA)
    with urllib.request.urlopen(request, timeout=timeout) as response, \
            target.open('wb') as handle:
        for chunk in iter(lambda: response.read(1 << 20), b''):
            handle.write(chunk)
    if item.get('checksum') and sha256(target) != item['checksum']:
        target.unlink(missing_ok=True)
        raise SystemExit(f'checksum mismatch: {item["filename"]}')
    return 'downloaded'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', default=str(Path(__file__).resolve().parents[1] / 'payloads.json'))
    parser.add_argument('--repo-dir', required=True, help='directory holding repo.json and payloads/')
    parser.add_argument('--base-url', required=True, help='URL prefix for payload links, e.g. http://192.168.1.10:8080/payloads/')
    parser.add_argument('--only', action='append', default=[], help='limit to these filenames')
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text())
    repo_dir = Path(args.repo_dir)
    payload_dir = repo_dir / 'payloads'
    payload_dir.mkdir(parents=True, exist_ok=True)
    base = args.base_url.rstrip('/') + '/'

    catalog = []
    for item in manifest['payloads']:
        if args.only and item['filename'] not in args.only:
            continue
        target = payload_dir / item['filename']
        try:
            state = fetch(item, target)
        except Exception as exc:
            print(f'  ! {item["filename"]}: {exc}', file=sys.stderr)
            continue
        catalog.append({**item, 'url': base + item['filename'],
                        'size': target.stat().st_size})
        print(f'  {state:10} {item["filename"]} ({target.stat().st_size} bytes)')

    repo = {'name': '家庭离线仓库', 'payloads': catalog}
    (repo_dir / 'repo.json').write_text(json.dumps(repo, ensure_ascii=False, indent=1) + '\n')
    print(f'repo.json: {len(catalog)} 项 -> {repo_dir / "repo.json"}')


if __name__ == '__main__':
    main()
