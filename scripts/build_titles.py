#!/usr/bin/env python3
"""Compile the PlayStation title list into titledb/titles.json (title ID -> name).

Upstream: https://github.com/andshrew/PlayStation-Titles (MIT), a PS4/PS5 title
list collected from the PlayStation Store. Each row is one region's title ID,
so Chinese names only exist on the Asia (HP/HB) title IDs; we spread them to
every title ID of the same store concept so a US/EU copy still gets a Chinese
name when the game was sold in Asia.

    python3 scripts/build_titles.py                 # download upstream main
    python3 scripts/build_titles.py --ref <sha>     # pin a commit
    python3 scripts/build_titles.py --source DIR    # use a local All_Titles.tsv (+ LICENSE)

Downloads honour HTTPS_PROXY. For a SOCKS proxy, fetch the two files with curl
first and pass --source.
"""
import argparse
import collections
import csv
import datetime
import json
import pathlib
import re
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / 'titledb' / 'titles.json'
LICENSE_OUT = ROOT / 'titledb' / 'UPSTREAM_LICENSE'
REPO = 'andshrew/PlayStation-Titles'
RAW = 'https://raw.githubusercontent.com/' + REPO + '/{ref}/{name}'
API = 'https://api.github.com/repos/' + REPO + '/commits/{ref}'

TITLE_ID = re.compile(r'^([A-Z]{4}\d{5})(?:_\d\d)?$')
CJK = re.compile(r'[㐀-䶿一-鿿豈-﫿]')
KANA = re.compile(r'[぀-ヿㇰ-ㇿｦ-ﾟ]')
HANGUL = re.compile(r'[ᄀ-ᇿ㄰-㆏가-힯]')
MARKS = re.compile(r'[™®℠]')          # TM, (R), SM
ASIA = ('HP', 'HB')                                   # Hong Kong / Asia store regions
WEST = ('UP', 'EP', 'UB', 'EB', 'IP')


def fetch(url, timeout=60):
    request = urllib.request.Request(url, headers={'User-Agent': 'ps5-jb-server-build'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def clean(name):
    return re.sub(r'\s+', ' ', MARKS.sub('', name or '')).strip()


def is_chinese(name):
    return bool(CJK.search(name)) and not KANA.search(name) and not HANGUL.search(name)


def is_latin(name):
    return not (CJK.search(name) or KANA.search(name) or HANGUL.search(name))


def pick(names):
    """Most frequent spelling wins; ties go to the shorter one (drops 'Demo' variants)."""
    counts = collections.Counter(names)
    return min(counts, key=lambda n: (-counts[n], len(n), n)) if counts else ''


def compile_titles(rows):
    """rows: dicts with titleId/conceptId/name/region. Returns {id: [en] | [en, zh]}."""
    records = {}
    concepts = collections.defaultdict(lambda: {'zh': [], 'west': [], 'latin': []})
    for row in rows:
        match = TITLE_ID.match((row.get('titleId') or '').strip().upper())
        name = clean(row.get('name'))
        if not match or not name:
            continue
        title_id, suffix = match.group(1), (row.get('titleId') or '')[9:]
        region = (row.get('region') or '').strip().upper()
        concept = (row.get('conceptId') or '').strip()
        # A title ID can appear as _00 and _01; keep _00.
        if title_id in records and suffix != '_00':
            continue
        records[title_id] = (name, region, concept)
        if concept:
            bucket = concepts[concept]
            if region in ASIA and is_chinese(name):
                bucket['zh'].append(name)
            if is_latin(name):
                bucket['latin'].append(name)
                if region in WEST:
                    bucket['west'].append(name)
    titles = {}
    for title_id in sorted(records):
        name, region, concept = records[title_id]
        bucket = concepts.get(concept) if concept else None
        own_zh = name if region in ASIA and is_chinese(name) else ''
        zh = own_zh or (pick(bucket['zh']) if bucket else '')
        if is_latin(name):
            en = name
        else:
            en = (pick(bucket['west']) or pick(bucket['latin'])) if bucket else ''
            en = en or name
        titles[title_id] = [en, zh] if zh and zh != en else [en]
    return titles


def read_rows(text):
    return list(csv.DictReader(text.splitlines(), delimiter='\t'))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    parser.add_argument('--ref', default='main', help='upstream branch or commit')
    parser.add_argument('--source', help='directory holding All_Titles.tsv (and LICENSE)')
    args = parser.parse_args()
    if args.source:
        source = pathlib.Path(args.source)
        tsv = (source / 'All_Titles.tsv').read_text(encoding='utf-8')
        license_path = source / 'LICENSE'
        license_text = license_path.read_text(encoding='utf-8') if license_path.exists() else ''
        commit = args.ref if args.ref != 'main' else 'local'
    else:
        try:
            commit = json.loads(fetch(API.format(ref=args.ref)))['sha']
        except Exception as exc:     # API rate limits should not block a build
            print('could not resolve commit, using', args.ref, '-', exc, file=sys.stderr)
            commit = args.ref
        tsv = fetch(RAW.format(ref=commit, name='All_Titles.tsv')).decode('utf-8')
        license_text = fetch(RAW.format(ref=commit, name='LICENSE')).decode('utf-8')
    titles = compile_titles(read_rows(tsv))
    if len(titles) < 1000:
        sys.exit(f'only {len(titles)} titles parsed; refusing to overwrite {OUT}')
    document = {
        'source': f'https://github.com/{REPO} (MIT, Copyright (c) 2022 andshrew)',
        'commit': commit,
        'generated': datetime.date.today().isoformat(),
        'format': 'titleId -> [english] or [english, chinese]',
        'count': len(titles),
        'titles': titles,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # One title per line keeps diffs readable when the list is refreshed.
    body = ',\n'.join(json.dumps(k) + ':' + json.dumps(v, ensure_ascii=False)
                      for k, v in titles.items())
    head = json.dumps({k: v for k, v in document.items() if k != 'titles'},
                      ensure_ascii=False, indent=1)[:-2]
    OUT.write_text(head + ',\n "titles": {\n' + body + '\n}}\n', encoding='utf-8')
    json.loads(OUT.read_text(encoding='utf-8'))      # sanity check
    if license_text:
        LICENSE_OUT.write_text(license_text, encoding='utf-8')
    with_zh = sum(1 for v in titles.values() if len(v) > 1)
    print(f'wrote {OUT} ({OUT.stat().st_size} bytes, {len(titles)} titles, {with_zh} with Chinese names)')


if __name__ == '__main__':
    main()
