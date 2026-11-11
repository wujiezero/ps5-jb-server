"""Find newer builds of repository plugins upstream and pull them in.

The upstream index is the catalog Payload Manager itself uses as its
"Official Repository" (itsPLK/ps5-payloads-mirror, payloads_v2.json): every
entry has a name, the mirrored file's URL, a version and its SHA-256. Mirror
file names follow ``<name>_<version>.<ext>``, which is how local entries that
predate this feature are matched to it.

Nothing here runs on a timer: an index is fetched only when the admin asks,
cached in the data directory, and compared against repo.json on every read.
Applying an update downloads the file, verifies the checksum from the index,
and hands it to RepoManager.upgrade, which keeps the previous build.
"""
import datetime
import hashlib
import json
import logging
import os
import re
import threading
import urllib.parse
import urllib.request
from pathlib import Path

import netproxy
from version import VERSION

DEFAULT_URL = 'https://itsplk.github.io/ps5-payloads-mirror/payloads_v2.json'
INDEX_LIMIT = 4 << 20
UA = {'User-Agent': f'ps5-jb-server/{VERSION} (+plugin updates)', 'Accept': 'application/json'}
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_FILENAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(?:elf|bin)\Z', re.IGNORECASE)


def _open(request, timeout):
    """Default opener: the upstream lives on the internet, so it may use the proxy."""
    return netproxy.opener().open(request, timeout=timeout)


class PluginUpdateError(RuntimeError):
    pass


def version_numbers(text):
    """Integers of a version string, e.g. 'v1.13-rc2' -> (1, 13, 2)."""
    return tuple(int(part) for part in re.findall(r'\d+', str(text or '')))


def local_is_newer(local, upstream):
    """True only when the numbers say so; anything uncertain is not 'newer'."""
    mine, theirs = version_numbers(local), version_numbers(upstream)
    return bool(mine and theirs) and mine > theirs


def _clean_entry(raw):
    if not isinstance(raw, dict):
        return None
    name, filename, url = raw.get('name'), raw.get('filename'), raw.get('url')
    checksum = str(raw.get('checksum') or '').lower()
    if not (isinstance(name, str) and name.strip() and isinstance(filename, str)
            and _FILENAME.match(filename) and isinstance(url, str)
            and urllib.parse.urlsplit(url).scheme == 'https' and _DIGEST.match(checksum)):
        return None
    return {'name': name.strip()[:120], 'filename': filename, 'url': url, 'checksum': checksum,
            'version': str(raw.get('version') or '')[:32],
            'last_update': str(raw.get('last_update') or '')[:32],
            'source': str(raw.get('source') or '')[:300],
            'description': str(raw.get('description') or '')[:400]}


def _follows_name(filename, name):
    """filename is <name><sep><version or platform>.<ext> (case-insensitive)."""
    lower, key = filename.lower(), name.lower()
    if not lower.startswith(key) or len(lower) <= len(key) or lower[len(key)] not in '_- .':
        return False
    rest = lower[len(key):].lstrip('_- .')
    return bool(re.match(r'(?:v?\d|ps[45]\b)', rest))


def match_upstream(item, index):
    """The upstream entry describing this repository item, or None."""
    filename = str(item.get('filename') or '')
    pinned = item.get('upstream')
    by_name = {entry['name'].lower(): entry for entry in index}
    if isinstance(pinned, str) and pinned.lower() in by_name:
        return by_name[pinned.lower()]
    checksum = str(item.get('checksum') or '').lower()
    for entry in index:
        if entry['filename'] == filename or (checksum and entry['checksum'] == checksum):
            return entry
    # Longest name first: "ftpsrv-drakmor_1.16.elf" is not an "ftpsrv" build.
    for entry in sorted(index, key=lambda entry: len(entry['name']), reverse=True):
        if _follows_name(filename, entry['name']):
            return entry
    for key in (item.get('name'), item.get('id')):
        if isinstance(key, str) and key.lower() in by_name:
            return by_name[key.lower()]
    return None


class PluginUpdates:
    def __init__(self, repo, cache_path, url=DEFAULT_URL, opener=None, timeout=20,
                 download_timeout=120):
        self.repo = repo
        self.cache_path = Path(cache_path)
        self.url = url or DEFAULT_URL
        self.opener = opener or _open
        self.timeout = timeout
        self.download_timeout = download_timeout
        self.lock = threading.Lock()
        self.applying = threading.Lock()

    # ----------------------------------------------------------- index cache

    def _cache(self):
        try:
            document = json.loads(self.cache_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        return document if isinstance(document, dict) else {}

    def _save_cache(self, document):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_name('.' + self.cache_path.name + '.tmp')
        temporary.write_text(json.dumps(document, ensure_ascii=False) + '\n', encoding='utf-8')
        os.replace(temporary, self.cache_path)

    def _fetch(self, url, limit, timeout):
        request = urllib.request.Request(url, headers=UA)
        try:
            with self.opener(request, timeout=timeout) as response:
                data = response.read(limit + 1)
        except OSError as exc:  # URLError, HTTPError, timeouts are all OSError
            reason = getattr(exc, 'reason', None) or exc
            raise PluginUpdateError(f'无法连接上游：{reason}{netproxy.hint()}') from exc
        if len(data) > limit:
            raise PluginUpdateError('上游文件超过大小上限')
        return data

    def check(self):
        """Fetch the upstream index now; a failure keeps the last good one."""
        with self.lock:
            now = datetime.datetime.now().isoformat(timespec='seconds')
            cache = self._cache()
            try:
                document = json.loads(self._fetch(self.url, INDEX_LIMIT, self.timeout))
                raw = document.get('payloads') if isinstance(document, dict) else document
                if not isinstance(raw, list):
                    raise PluginUpdateError('上游索引格式不正确')
                entries = [entry for entry in map(_clean_entry, raw) if entry]
                if not entries:
                    raise PluginUpdateError('上游索引里没有可用的插件')
            except (PluginUpdateError, ValueError) as exc:
                message = str(exc) if isinstance(exc, PluginUpdateError) else '上游索引不是有效的 JSON'
                cache.update({'error': message, 'attempted_at': now})
                self._save_cache(cache)
                logging.warning('plugin upstream check failed: %s', message)
                return self.status()
            self._save_cache({'url': self.url, 'checked_at': now, 'attempted_at': now,
                              'error': '', 'payloads': entries})
            logging.info('plugin upstream index: %d entries', len(entries))
        return self.status()

    # -------------------------------------------------------------- compare

    def status(self):
        cache = self._cache()
        index = [entry for entry in map(_clean_entry, cache.get('payloads') or []) if entry]
        items, available = {}, 0
        if index:
            for item in self.repo.catalog()['payloads']:
                filename = item.get('filename')
                if not isinstance(filename, str):
                    continue
                entry = match_upstream(item, index)
                if entry is None:
                    continue
                local_sum = str(item.get('checksum') or '').lower()
                if entry['filename'] == filename and (not local_sum or local_sum == entry['checksum']):
                    state = 'current'
                elif local_sum == entry['checksum']:
                    state = 'current'
                elif local_is_newer(item.get('version'), entry['version']):
                    state = 'local_newer'
                else:
                    state = 'available'
                    available += 1
                items[filename] = {'state': state, 'name': entry['name'], 'version': entry['version'],
                                   'filename': entry['filename'], 'last_update': entry['last_update'],
                                   'source': entry['source']}
        return {'source': cache.get('url') or self.url, 'checked_at': cache.get('checked_at'),
                'attempted_at': cache.get('attempted_at'), 'error': cache.get('error') or '',
                'indexed': len(index), 'available': available, 'items': items}

    # ---------------------------------------------------------------- apply

    def apply(self, filename):
        """Download the upstream build for one plugin and publish it."""
        if not self.applying.acquire(blocking=False):
            raise PluginUpdateError('另一个插件正在更新，请稍后再试')
        try:
            cache = self._cache()
            index = [entry for entry in map(_clean_entry, cache.get('payloads') or []) if entry]
            if not index:
                raise PluginUpdateError('请先检查上游更新')
            item = next((entry for entry in self.repo.catalog()['payloads']
                         if entry.get('filename') == filename), None)
            if item is None:
                raise FileNotFoundError('仓库中没有这个文件')
            entry = match_upstream(item, index)
            if entry is None:
                raise PluginUpdateError('上游没有这个插件')
            if str(item.get('checksum') or '').lower() == entry['checksum']:
                raise PluginUpdateError('已经是上游最新版本')
            data = self._fetch(entry['url'], self.repo.MAX_UPLOAD, self.download_timeout)
            if hashlib.sha256(data).hexdigest() != entry['checksum']:
                raise PluginUpdateError('下载的文件校验和与上游索引不符，未写入仓库（上游可能刚更新，请重新检查）')
            updated = self.repo.upgrade(filename, entry['filename'], data, version=entry['version'],
                                        last_update=entry['last_update'], upstream=entry['name'])
            logging.info('plugin updated from upstream: %s -> %s', filename, entry['filename'])
            return {'ok': True, 'filename': updated['filename'], 'previous': filename,
                    'version': updated.get('version', '')}
        finally:
            self.applying.release()
