#!/usr/bin/env python3
"""Authenticated PS5 DNS and IPv4 gateway policy service."""
import argparse
import datetime
import gzip
import hashlib
import hmac
import http.cookies
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import urllib.parse
import signal
import ssl
import subprocess
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer

from activity import Classifier, FlowObserver, LabelsStore, make_poller
from backup import BackupError, BackupService
from selfcheck import Context as CheckContext, SelfCheck
from notify import EVENTS as NOTIFY_EVENTS, TYPES as NOTIFY_TYPES, NotifyError
from api_console import ConsoleAPI
import logbook
import netproxy
from settings import Settings, detect_interface
from titles import TitleCatalog, proxy_reader
from dns_service import DNSPolicy, LimitedThreads, diagnostic_error, start_dns
from policy import ConfigStore, Firewall, LOCAL_DOMAINS
from plugin_updates import PluginUpdates, PluginUpdateError
from version import VERSION, WEB_FILE_PATTERN, version_info
from updates import UpdateManager, UpdateError, ACTIVE as UPDATE_ACTIVE


class AuthManager:
    """Cookie sessions with sliding expiry, persisted so a restart keeps users signed in."""

    IDLE_SECONDS = 30 * 24 * 3600      # renew on use, 30 days idle
    MAX_SECONDS = 90 * 24 * 3600       # hard cap per session
    SAVE_SLACK = 3600                  # persist a renewal at most once an hour per session

    def __init__(self, credentials, session_path=None):
        self.credentials = credentials
        self.sessions = OrderedDict()
        self.attempts = OrderedDict()
        self.lock = threading.RLock()
        self.session_path = Path(session_path) if session_path else None
        self._load()

    # -- persistence (tokens are stored hashed) --
    @staticmethod
    def _token_key(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def _load(self):
        if not self.session_path:
            return
        try:
            saved = json.loads(self.session_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return
        now = time.time()
        for item in saved.get('sessions', []):
            if not isinstance(item, dict):
                continue
            key, csrf = item.get('key'), item.get('csrf')
            expires, created = item.get('expires'), item.get('created') or now
            if not (isinstance(key, str) and isinstance(csrf, str)):
                continue
            if not isinstance(expires, (int, float)) or expires <= now:
                continue
            self.sessions[key] = {'csrf': csrf, 'expires': expires, 'saved': expires,
                                  'created': float(created), 'hashed': True}

    def _save(self):
        if not self.session_path:
            return
        for value in self.sessions.values():
            value['saved'] = value['expires']
        payload = {'sessions': [
            {'key': key, 'csrf': value['csrf'], 'expires': value['expires'],
             'created': value.get('created', time.time())}
            for key, value in self.sessions.items()]}
        try:
            self.session_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.session_path.with_suffix('.tmp')
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(payload, handle)
            os.replace(tmp, self.session_path)
        except OSError:
            pass

    @staticmethod
    def make_credentials(password):
        if not isinstance(password, str) or len(password) < 10:
            raise ValueError('管理密码至少 10 个字符')
        salt = secrets.token_hex(16)
        iterations = 310000
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), iterations).hex()
        return {'salt': salt, 'iterations': iterations, 'password_hash': digest}

    def verify(self, password):
        if not isinstance(password, str) or len(password) > 256:
            return False
        c = self.credentials
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(c['salt']), c['iterations']).hex()
        return hmac.compare_digest(digest, c['password_hash'])

    def login(self, client, password):
        with self.lock:
            now = time.monotonic()
            attempts = [x for x in self.attempts.get(client, []) if x > now - 60]
            if len(attempts) >= 6:
                raise PermissionError('尝试过于频繁，请稍后再试')
            if not self.verify(password):
                attempts.append(now)
                self.attempts[client] = attempts
                if len(self.attempts) > 1024:
                    self.attempts.popitem(last=False)
                raise PermissionError('密码不正确')
            self.attempts.pop(client, None)
            wall = time.time()
            monotonic = time.monotonic()
            self.sessions = OrderedDict(
                (k, v) for k, v in self.sessions.items()
                if v['expires'] > (wall if v.get('hashed') else monotonic))
            if len(self.sessions) >= 64:
                self.sessions.popitem(last=False)
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.sessions[self._token_key(token)] = {
                'csrf': csrf, 'expires': wall + self.IDLE_SECONDS,
                'created': wall, 'hashed': True}
            self._save()
            return token, csrf

    def session(self, token):
        if not isinstance(token, str) or not token:
            return None
        key = self._token_key(token)
        with self.lock:
            found = self.sessions.get(key)
            wall = time.time()
            if not found:
                return None
            created = float(found.get('created') or wall)
            if found['expires'] <= wall or wall - created > self.MAX_SECONDS:
                self.sessions.pop(key, None)
                self._save()
                return None
            # Sliding expiry: keep active users signed in. The page polls every
            # few seconds, so only write the file when the renewal moved far
            # enough to matter (a restart loses at most SAVE_SLACK of idle time).
            found['expires'] = min(wall + self.IDLE_SECONDS, created + self.MAX_SECONDS)
            self.sessions.move_to_end(key)
            if found['expires'] - found.get('saved', 0) >= self.SAVE_SLACK:
                self._save()
            return {'csrf': found['csrf'], 'expires': found['expires']}

    def logout(self, token):
        if not isinstance(token, str) or not token:
            return
        with self.lock:
            if self.sessions.pop(self._token_key(token), None) is not None:
                self._save()


def _read_conntrack():
    result = subprocess.run(['conntrack', '-L'], capture_output=True, text=True, timeout=10)
    return result.stdout


def _allowed_destination(engine):
    """A destination counts as allowed when it is inside the active whitelist."""
    import ipaddress

    def check(dst):
        try:
            address = ipaddress.IPv4Address(dst)
        except ValueError:
            return False
        config = engine.store.snapshot()
        if any(address in ipaddress.IPv4Network(net, strict=False) for net in config['networks']):
            return True
        if dst in engine.live_leases():
            return True
        return False

    return check


def create_engine(store, host_ip, lan, firewall=None, interface=None):
    """DNS policy and firewall must agree on the LAN, or DNS refuses every client."""
    firewall = firewall or Firewall(host_ip=host_ip, lan=lan, interface=interface or 'eth0')
    return DNSPolicy(store, firewall, host_ip=host_ip, lan=lan)


def set_forwarding(enabled):
    subprocess.run(['sysctl', '-w', 'net.ipv4.ip_forward=' + ('1' if enabled else '0')],
                   capture_output=True, text=True, timeout=5, check=True)


def initialize_engine(engine, forwarder=set_forwarding):
    """Keep the diagnostic UI available if firewall initialization fails closed."""
    try:
        engine.apply_rules(engine.store.snapshot(), {})
        forwarder(True)
        engine.gateway_error = None
        return True
    except Exception as exc:
        engine.gateway_error = '网关初始化失败：' + diagnostic_error(exc)
        engine.last_error = engine.gateway_error
        try:
            forwarder(False)
        except Exception:
            logging.exception('Could not disable forwarding after initialization failure')
        logging.error('%s', engine.gateway_error)
        return False


class RepoManager:
    """Owns /srv/ps5-repo: repo.json plus payload files, written atomically.

    Earlier versions of a plugin live in versions/ (content addressed by
    SHA-256, never served to the PS5) with their records in
    versions/index.json, so an upgrade or a manual replacement can be rolled
    back. repo.json itself stays exactly what Payload Manager expects.
    """

    MAX_UPLOAD = 64 * 1024 * 1024
    HISTORY_LIMIT = 3
    NAME = '家庭离线仓库（PS5 常用插件）'
    _FILENAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(?:elf|bin)\Z', re.IGNORECASE)
    _DIGEST = re.compile(r'[0-9a-f]{64}\Z')
    _SEED_FILE = 'ps5-payload-repository'
    _FIELDS = ('name', 'version', 'category', 'author', 'description', 'source')

    def __init__(self, root, base_url='http://127.0.0.1:8080/payloads/', max_upload=None):
        if max_upload:
            self.MAX_UPLOAD = int(max_upload)
        self.root = Path(root)
        self.base_url = base_url if base_url.endswith('/') else base_url + '/'
        self.payloads = self.root / 'payloads'
        self.versions = self.root / 'versions'
        self.history_path = self.versions / 'index.json'
        self.catalog_path = self.root / 'repo.json'
        self.lock = threading.RLock()
        self.payloads.mkdir(parents=True, exist_ok=True)
        if not self.catalog_path.exists():
            self._write_catalog({'name': self.NAME, 'payloads': [{
                'id': self._SEED_FILE, 'name': '在此添加你的插件', 'filename': self._SEED_FILE,
                'url': 'https://example.invalid/repo.json', 'version': '0',
                'category': '说明',
                'description': '通过管理页面上传 ELF 或 BIN 后，本条目会被自动替换。',
            }]})

    @staticmethod
    def _check_name(filename):
        if not isinstance(filename, str) or not RepoManager._FILENAME.match(filename):
            raise ValueError('文件名只能包含字母、数字、点、下划线和连字符，且以 .elf 或 .bin 结尾')

    @staticmethod
    def entry_id(item):
        """Stable key of a plugin across version (and so filename) changes."""
        value = item.get('id')
        if isinstance(value, str) and value:
            return value
        stem = str(item.get('filename') or '').rsplit('.', 1)[0].lower()
        return re.sub(r'[^a-z0-9._-]+', '-', stem).strip('-') or 'payload'

    def _write_json(self, path, document):
        temporary = path.with_name('.' + path.name + '.tmp')
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(temporary, path)

    def _write_catalog(self, document):
        self._write_json(self.catalog_path, document)

    def catalog(self):
        with self.lock:
            document = json.loads(self.catalog_path.read_text(encoding='utf-8'))
        document.setdefault('payloads', [])
        return document

    def history(self):
        """{entry id: [records, newest first]}; unreadable means none kept."""
        with self.lock:
            try:
                document = json.loads(self.history_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return {}
        plugins = document.get('plugins') if isinstance(document, dict) else None
        if not isinstance(plugins, dict):
            return {}
        return {key: [record for record in value if isinstance(record, dict)
                      and isinstance(record.get('checksum'), str) and self._DIGEST.match(record['checksum'])]
                for key, value in plugins.items() if isinstance(value, list)}

    def _write_history(self, history):
        self.versions.mkdir(parents=True, exist_ok=True)
        self._write_json(self.history_path, {'schema': 1, 'plugins': {key: value for key, value in history.items() if value}})

    def summary(self):
        with self.lock:
            document = self.catalog()
            history = self.history()
            items, total = [], 0
            for item in document['payloads']:
                filename = item.get('filename')
                if (not isinstance(filename, str) or not filename or '/' in filename
                        or filename.startswith('.')):
                    continue
                path = self.payloads / filename
                present = path.is_file()
                size = path.stat().st_size if present else 0
                total += size
                versions = [{key: record.get(key) for key in
                             ('filename', 'version', 'checksum', 'size', 'last_update', 'archived_at')}
                            for record in history.get(self.entry_id(item), [])]
                items.append({**item, 'size': size, 'missing': not present, 'history': versions})
            return {'catalog': {'name': document.get('name', self.NAME), 'items': items},
                    'total_bytes': total}

    def _save_payload(self, filename, data):
        temporary = self.payloads / ('.' + filename + '.tmp')
        temporary.write_bytes(data)
        os.replace(temporary, self.payloads / filename)

    def limit_label(self):
        return f'{self.MAX_UPLOAD // (1024 * 1024)}MB'

    def _check_data(self, data):
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise ValueError('文件内容为空')
        if len(data) > self.MAX_UPLOAD:
            raise ValueError(f'文件超过 {self.limit_label()} 上限')

    def add(self, filename, data, name, version='', category='', author='', description='',
            replace=False):
        self._check_name(filename)
        self._check_data(data)
        with self.lock:
            target = self.payloads / filename
            digest = hashlib.sha256(bytes(data)).hexdigest()
            if replace:
                return self._replace(filename, data, digest, name=name, version=version,
                                     category=category, author=author, description=description)
            if not isinstance(name, str) or not name.strip():
                raise ValueError('插件名称不能为空')
            if target.exists():
                raise FileExistsError('同名文件已存在，可勾选「替换同名文件」或先删除原文件')
            document = self.catalog()
            entry = {
                'id': self._unique_id(document, self.entry_id({'filename': filename})),
                'name': name.strip()[:120], 'filename': filename,
                'url': self.base_url + filename,
                'version': str(version)[:32], 'last_update': datetime.date.today().isoformat(),
                'category': (category or 'Uncategorized')[:64],
                'description': str(description)[:400], 'author': str(author)[:64],
                'checksum': digest,
            }
            self._save_payload(filename, bytes(data))
            # Replace the placeholder shipped with a fresh install.
            document['payloads'] = [p for p in document['payloads']
                                    if p.get('filename') != self._SEED_FILE]
            document['payloads'].append(entry)
            self._write_catalog(document)
            return entry

    def _unique_id(self, document, key):
        """An id no other entry uses: history is keyed by it, so two plugins must
        not share one (zftpd upgraded to zftpd_1.7.elf, then zftpd.elf uploaded anew)."""
        taken = {self.entry_id(item) for item in document['payloads']
                 if item.get('filename') != self._SEED_FILE}
        candidate, number = key, 2
        while candidate in taken:
            candidate = f'{key}-{number}'
            number += 1
        return candidate

    @staticmethod
    def _find(document, filename):
        for item in document['payloads']:
            if item.get('filename') == filename:
                return item
        raise FileNotFoundError('仓库中没有这个文件')

    def _archive(self, item, history):
        """Keep the bytes currently published for item as a restorable version.

        The published file stays in place (it is still what the PS5 downloads
        until repo.json changes); only a copy goes to versions/.
        """
        source = self.payloads / item['filename']
        if not source.is_file():
            return None
        digest = item.get('checksum')
        if not (isinstance(digest, str) and self._DIGEST.match(digest)):
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.versions.mkdir(parents=True, exist_ok=True)
        stored = self.versions / digest
        if not stored.is_file():
            temporary = self.versions / ('.' + digest + '.tmp')
            shutil.copyfile(source, temporary)
            os.replace(temporary, stored)
        key = self.entry_id(item)
        item.setdefault('id', key)
        record = {'filename': item['filename'], 'version': str(item.get('version') or ''),
                  'checksum': digest, 'size': source.stat().st_size,
                  'last_update': str(item.get('last_update') or ''),
                  'archived_at': datetime.datetime.now().isoformat(timespec='seconds')}
        records = [record] + [old for old in history.get(key, []) if old.get('checksum') != digest]
        history[key] = records[:self.HISTORY_LIMIT]
        return record

    def _prune_versions(self, history):
        """Delete stored versions nothing refers to any more."""
        keep = {record['checksum'] for records in history.values() for record in records}
        if not self.versions.is_dir():
            return
        for path in self.versions.iterdir():
            if self._DIGEST.match(path.name) and path.name not in keep:
                path.unlink(missing_ok=True)

    def _publish(self, item, filename, data, digest, history, document, fields=None, version=None):
        """Point item at new bytes, archiving the current ones first."""
        previous = item['filename']
        if filename != previous:
            self._check_name(filename)
            if any(other is not item and other.get('filename') == filename
                   for other in document['payloads']):
                raise FileExistsError(f'仓库中另一个插件已使用文件名 {filename}')
        if digest != item.get('checksum'):
            self._archive(item, history)
        self._save_payload(filename, bytes(data))
        if version is not None:
            item['version'] = version
        for key, value in (fields or {}).items():
            if isinstance(value, str) and value.strip():
                item[key] = value.strip()[:400]
        item.update({'filename': filename, 'url': self.base_url + filename, 'checksum': digest,
                     'size': len(data), 'last_update': (fields or {}).get('last_update')
                     or datetime.date.today().isoformat()})
        item.setdefault('id', self.entry_id({'filename': previous}))
        # A restored version is current again, not history.
        key = self.entry_id(item)
        history[key] = [record for record in history.get(key, []) if record.get('checksum') != digest]
        self._write_catalog(document)
        self._write_history(history)
        if filename != previous:
            (self.payloads / previous).unlink(missing_ok=True)
        self._prune_versions(history)
        return item

    def _replace(self, filename, data, digest, **fields):
        """Swap the bytes of an existing entry; blank metadata keeps the old value."""
        document = self.catalog()
        try:
            item = self._find(document, filename)
        except FileNotFoundError:
            raise FileNotFoundError('仓库中没有这个文件，无法替换') from None
        fields = {key: fields.get(key) for key in ('name', 'version', 'category', 'author', 'description')}
        return self._publish(item, filename, data, digest, self.history(), document, fields)

    def upgrade(self, filename, new_filename, data, version='', last_update='', upstream=''):
        """Publish a newer build of an existing plugin, keeping the old one restorable."""
        self._check_name(filename)
        self._check_name(new_filename)
        self._check_data(data)
        with self.lock:
            document = self.catalog()
            item = self._find(document, filename)
            if upstream:
                item['upstream'] = str(upstream)[:120]
            return self._publish(item, new_filename, data, hashlib.sha256(bytes(data)).hexdigest(),
                                 self.history(), document,
                                 {'version': str(version)[:32], 'last_update': str(last_update)[:32]})

    def rollback(self, filename, checksum):
        """Make an archived version current again; the current one is archived."""
        self._check_name(filename)
        with self.lock:
            document = self.catalog()
            item = self._find(document, filename)
            history = self.history()
            record = next((old for old in history.get(self.entry_id(item), [])
                           if old.get('checksum') == checksum), None)
            stored = self.versions / str(checksum)
            if record is None or not self._DIGEST.match(str(checksum)) or not stored.is_file():
                raise FileNotFoundError('没有找到这个历史版本')
            data = stored.read_bytes()
            if hashlib.sha256(data).hexdigest() != checksum:
                raise ValueError('历史版本文件已损坏（校验和不符），无法回退')
            return self._publish(item, record['filename'], data, checksum, history, document,
                                 {'last_update': record.get('last_update') or ''},
                                 version=record.get('version') or '')

    def delete_version(self, filename, checksum):
        self._check_name(filename)
        with self.lock:
            document = self.catalog()
            item = self._find(document, filename)
            history = self.history()
            key = self.entry_id(item)
            remaining = [old for old in history.get(key, []) if old.get('checksum') != checksum]
            if len(remaining) == len(history.get(key, [])):
                raise FileNotFoundError('没有找到这个历史版本')
            history[key] = remaining
            self._write_history(history)
            self._prune_versions(history)

    def update(self, filename, **fields):
        self._check_name(filename)
        with self.lock:
            document = self.catalog()
            for item in document['payloads']:
                if item.get('filename') == filename:
                    for key in self._FIELDS:
                        if key in fields and fields[key] is not None:
                            item[key] = str(fields[key])[:400]
                    if not item.get('name'):
                        raise ValueError('插件名称不能为空')
                    self._write_catalog(document)
                    return item
            raise FileNotFoundError('仓库中没有这个文件')

    def delete(self, filename):
        self._check_name(filename)
        with self.lock:
            document = self.catalog()
            item = self._find(document, filename)
            target = self.payloads / filename
            if target.exists():
                target.unlink()
            document['payloads'] = [p for p in document['payloads'] if p is not item]
            self._write_catalog(document)
            history = self.history()
            if history.pop(self.entry_id(item), None) is not None:
                self._write_history(history)
                self._prune_versions(history)


def change_password(auth, credentials_path, current, new):
    """Verify the old password, store the new hash, and drop every session."""
    if not auth.verify(current if isinstance(current, str) else ''):
        raise PermissionError('当前密码不正确')
    if not isinstance(new, str) or len(new) < 10:
        raise ValueError('新密码至少 10 个字符')
    if len(new) > 256:
        raise ValueError('新密码过长')
    credentials = AuthManager.make_credentials(new)
    path = Path(credentials_path)
    temporary = path.with_name('.' + path.name + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(credentials, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    with auth.lock:
        auth.credentials = credentials
        auth.sessions.clear()
        # Persist the empty table too, otherwise a restart would revive the
        # sessions that the password change was meant to revoke.
        auth._save()
    return credentials


class _ConsoleMixin:
    """Console API forwarding; kept tiny so routing stays in one place."""

    def console_get(self, handler):
        api = getattr(self, 'console', None)
        if api is None:
            handler.send_body(503, {'error': '控制台接口未启用'})
            return True
        return api.handle_get(handler)

    def console_post(self, handler, route):
        api = getattr(self, 'console', None)
        if api is None:
            handler.send_body(503, {'error': '控制台接口未启用'})
            return True
        return api.handle_post(handler, route)


class Application(_ConsoleMixin):
    def __init__(self, engine, activity=None):
        self.engine = engine
        self.activity = activity
        self.started = time.monotonic()

    def allow(self, targets):
        """Add selected flows to the whitelist and apply immediately."""
        additions = self.activity.allow_targets(targets or [])
        current = self.engine.store.snapshot()
        merged = {
            'domains': sorted(set(current['domains']) | set(additions['domains'])),
            'networks': sorted(set(current['networks']) | set(additions['networks'])),
            'upstream_dns': current['upstream_dns'],
        }
        self.engine.change_config(merged)
        return self.engine.store.snapshot()

    def change(self, body=None, rollback=False):
        self.engine.change_config(body, rollback)

    # Fresh installs use ps5-jb-*, the original deployment ps5-*.
    UNIT_CANDIDATES = {
        'server': ('ps5-jb-server.service', 'ps5-manager.service'),
        'host': ('ps5-jb-host.service', 'ps5-host.service'),
        'repo': ('ps5-jb-repo.service', 'ps5-repo.service'),
    }
    UNIT_TTL = 30

    def units(self):
        """Resolve which systemd unit names exist here; cached, systemctl is slow."""
        cached = getattr(self, '_units', None)
        if cached and time.monotonic() - cached[0] < self.UNIT_TTL:
            return cached[1]
        found = {}
        for role, names in self.UNIT_CANDIDATES.items():
            found[role] = {'name': names[0].removesuffix('.service'), 'state': 'unknown'}
            try:
                check = subprocess.run(['systemctl', 'is-active', *names],
                                       capture_output=True, text=True, timeout=3)
                states = check.stdout.split()
            except (OSError, subprocess.SubprocessError):
                states = []
            # Prefer the unit that is running, then any that is known to systemd.
            ranked = sorted(zip(names, states),
                            key=lambda pair: (pair[1] != 'active',
                                              pair[1] in ('inactive', 'unknown')))
            if ranked:
                name, state = ranked[0]
                found[role] = {'name': name.removesuffix('.service'), 'state': state}
        self._units = (time.monotonic(), found)
        return found

    def service_info(self):
        settings = getattr(self, 'settings', None)
        units = self.units()
        if settings is None:
            return {'units': units}
        get = lambda name, default=None: getattr(settings, name, default)  # noqa: E731
        return {
            'units': units,
            'ports': {'dns': get('dns_port', 53), 'http': get('http_port', 80),
                      'https': get('https_port', 443), 'admin': get('admin_port', 8443),
                      'repo': get('repo_port', 8080)},
            'paths': {'data': str(get('data_dir', '')), 'repo': str(get('repo_dir', '')),
                      'config': str(get('config_path', ''))},
            'limits': {'repo_upload': getattr(getattr(self, 'repo', None), 'MAX_UPLOAD',
                                              get('max_upload', 64 << 20)),
                       'file_upload': get('max_file_upload', 0)},
            'subnet': get('subnet', ''),
        }

    def state(self):
        # Never hold the policy lock across subprocess calls: DNS answers wait on it.
        try:
            counters = self.engine.firewall.counters()
            gateway = 'error' if self.engine.gateway_error else 'active'
        except Exception as exc:
            counters = {'error': str(exc)[:200]}
            gateway = 'error'
        service = self.service_info()
        ipv6 = Path('/proc/sys/net/ipv6/conf/all/forwarding')
        try:
            ipv6_forwarding = ipv6.exists() and ipv6.read_text().strip() == '1'
        except OSError:
            ipv6_forwarding = False
        with self.engine.store.lock:
            config = self.engine.store.snapshot()
            has_previous = self.engine.store.previous is not None
        return {
            'config': config,
            # Builtin names answered by this host; the UI explains them instead of
            # duplicating the list, so policy.py stays the single source of truth.
            'local_domains': list(LOCAL_DOMAINS),
            'status': {'host_ip': self.engine.host_ip, 'dns': 'running', 'gateway': gateway,
                       'interface': getattr(self.engine.firewall, 'interface', None),
                       'host': service['units']['host']['state'],
                       'host_unit': service['units']['host']['name'],
                       'uptime': round(time.monotonic() - self.started),
                       'ipv6_forwarding': ipv6_forwarding,
                       'last_applied': self.engine.last_applied,
                       'last_error': self.engine.last_error,
                       'gateway_error': self.engine.gateway_error,
                       'has_previous': has_previous,
                       'dynamic_addresses': len(self.engine.live_leases())},
            'service': service,
            'logs': list(self.engine.logs), 'firewall': counters}


class _RequestBody:
    """The current request's body: reads stop at Content-Length.

    remaining > 0 when a response is sent means the body was not consumed, so
    the connection cannot carry another request and is closed after it.
    """

    def __init__(self, raw, length):
        self.raw, self.remaining = raw, length

    def read(self, size=-1):
        if self.remaining <= 0:
            return b''
        if size is None or size < 0 or size > self.remaining:
            size = self.remaining
        data = self.raw.read(size)
        self.remaining = self.remaining - len(data) if data else 0
        return data

    def readline(self, size=-1):
        if self.remaining <= 0:
            return b''
        if size is None or size < 0 or size > self.remaining:
            size = self.remaining
        data = self.raw.readline(size)
        self.remaining = self.remaining - len(data) if data else 0
        return data


def accepts_gzip(header):
    for part in (header or '').split(','):
        token, _, params = part.strip().partition(';')
        if token.strip().lower() in ('gzip', '*'):
            quality = params.strip().lower()
            return not (quality.startswith('q=') and quality[2:].strip() in ('0', '0.0', '0.00', '0.000'))
    return False


class AdminHandler(BaseHTTPRequestHandler):
    server_version = 'PS5LocalManager/' + VERSION
    # Keep-alive: the page polls every few seconds and should not redo TLS each time.
    protocol_version = 'HTTP/1.1'
    IDLE_TIMEOUT = 15
    # JSON bodies above this size are gzip-compressed when the browser accepts it.
    GZIP_MIN = 1024

    def setup(self):
        super().setup()
        self.connection.settimeout(self.IDLE_TIMEOUT)
        self.socket_rfile = self.rfile

    def handle_one_request(self):
        # The previous request's body wrapper must not read the next request line.
        self.rfile = self.socket_rfile
        self.connection_header = False
        super().handle_one_request()

    def finish(self):
        self.rfile = self.socket_rfile
        super().finish()

    def send_header(self, keyword, value):
        if keyword.lower() == 'connection':
            self.connection_header = True
        super().send_header(keyword, value)

    def parse_request(self):
        if not super().parse_request():
            return False
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = -1
        if self.headers.get('Transfer-Encoding') or length < 0:
            # Bodies we cannot delimit are refused by every handler; do not reuse the stream.
            self.close_connection = True
            length = 0
        self.rfile = _RequestBody(self.socket_rfile, length)
        return True

    def end_headers(self):
        if getattr(self.rfile, 'remaining', 0) > 0:
            # Answered before the body was read (401, 413, 409…): the stream is out of sync.
            self.close_connection = True
        if self.close_connection and not getattr(self, 'connection_header', False):
            self.send_header('Connection', 'close')
        super().end_headers()

    def log_message(self, fmt, *args):
        # Never log request bodies, cookies, or credentials.
        logging.getLogger('http').info('%s %s', self.client_address[0], fmt % args)

    def log_error(self, fmt, *args):
        # An idle keep-alive connection timing out is routine, not an error.
        if fmt.startswith('Request timed out'):
            logging.getLogger('http').debug('%s %s', self.client_address[0], fmt % args)
            return
        self.log_message(fmt, *args)

    def log_request(self, code='-', size='-'):
        # The page polls every few seconds; successful reads would bury the
        # records that matter in the journal and in the exported service log.
        try:
            status = int(getattr(code, 'value', code))
        except (TypeError, ValueError):
            status = 0
        level = logging.DEBUG if self.command == 'GET' and 200 <= status < 400 else logging.INFO
        logging.getLogger('http').log(level, '%s "%s" %s', self.client_address[0],
                                      self.requestline, status or code)

    def send_body(self, status, data, ctype='application/json; charset=utf-8', cookie=None,
                  cache='no-store', etag=None, encoding=None, vary=False):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        if (encoding is None and ctype.startswith('application/json') and len(data) > self.GZIP_MIN):
            vary = True
            if accepts_gzip(self.headers.get('Accept-Encoding')):
                data, encoding = gzip.compress(data, compresslevel=5), 'gzip'
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        if encoding:
            self.send_header('Content-Encoding', encoding)
        if vary:
            self.send_header('Vary', 'Accept-Encoding')
        if etag:
            self.send_header('ETag', etag)
        self.send_header('Cache-Control', cache)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(data)

    def guard(self):
        peer = ipaddress.ip_address(self.client_address[0])
        allowed = ipaddress.ip_network(self.server.app.settings.subnet, strict=False)
        if not (peer.is_loopback or peer in allowed):
            self.send_body(403, {'error': '仅允许家庭局域网管理'})
            return False
        host, port = self.server.server_address[:2]
        expected = {f'{host}:{port}'}
        if host == '0.0.0.0':
            expected = {f'{self.server.app.settings.host_ip}:{port}', f'127.0.0.1:{port}'}
        request_host = self.headers.get('Host', '')
        if request_host not in expected:
            self.send_body(403, {'error': '管理入口地址不匹配'})
            return False
        origin = self.headers.get('Origin')
        scheme = 'https' if isinstance(self.connection, ssl.SSLSocket) else 'http'
        if origin and origin != f'{scheme}://{request_host}':
            self.send_body(403, {'error': '拒绝跨站请求'})
            return False
        return True

    def token(self):
        try:
            cookies = http.cookies.SimpleCookie(self.headers.get('Cookie', ''))
            return cookies['ps5_session'].value if 'ps5_session' in cookies else ''
        except http.cookies.CookieError:
            return ''

    def require_session(self, write=False):
        session = self.server.auth.session(self.token())
        if not session:
            self.send_body(401, {'error': '请先登录'})
            return None
        if write and not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), session['csrf']):
            self.send_body(403, {'error': '操作校验失效，请刷新后重试'})
            return None
        return session

    # ---------- routing ----------
    def do_GET(self):
        if not self.guard():
            return
        route = _lookup(GET_ROUTES, GET_PREFIXES, self.path)
        if route is None:
            self.serve_static()
            return
        if route.auth:
            self.session = self.require_session(write=route.auth == 'write')
            if not self.session:
                return
        if getattr(self, route.handler)() is False:
            # A console prefix the console API does not know: same 404 as any page.
            self.serve_static()

    def do_POST(self):
        if not self.guard():
            return
        path = self.path.split('?', 1)[0]
        route = _lookup(POST_ROUTES, POST_PREFIXES, self.path)
        updater = getattr(self.server.app, 'updates', None)
        tracked = route is not None and route.tracked and not path.endswith('/cancel')
        if tracked and updater and (updater.status().get('job') or {}).get('state') in UPDATE_ACTIVE:
            if self.require_session(write=True):
                self.send_body(409, {'error': '服务正在升级，请完成后再开始新操作'})
            return
        args = ()
        if route is None or route.body == 'json':
            body = self.json_body()
            if body is None:
                return
            if route is None:
                self.send_body(404, {'error': '未知操作'})
                return
            args = (body,)
        if route.auth and not self.require_session(write=route.auth == 'write'):
            return
        tracked = tracked and updater is not None and hasattr(updater, 'begin_operation')
        if tracked:
            try:
                updater.begin_operation()
            except UpdateError as exc:
                self.send_body(409, {'error': str(exc)})
                return
        try:
            getattr(self, route.handler)(*args)
        except PermissionError as exc:
            self.send_body(403, {'error': str(exc)})
        except FileNotFoundError as exc:
            self.send_body(404, {'error': str(exc)})
        except FileExistsError as exc:
            self.send_body(409, {'error': str(exc)})
        except ValueError as exc:
            self.send_body(400, {'error': str(exc)})
        except Exception as exc:
            logging.exception('Policy apply failed')
            self.send_body(500, {'error': '应用失败：' + diagnostic_error(exc) + '。已尝试恢复上一份策略，请查看概览状态。'})
        finally:
            if tracked:
                updater.end_operation()

    def json_body(self):
        """Small JSON object request bodies; None after answering 400."""
        try:
            if self.headers.get('Transfer-Encoding'):
                raise ValueError('不支持此请求编码')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 32768:
                raise ValueError('请求大小必须在 1–32768 字节之间')
            if self.headers.get_content_type() != 'application/json':
                raise ValueError('需要 JSON 请求')
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError('配置必须是 JSON 对象')
        except (ValueError, UnicodeError, TimeoutError):
            self.send_body(400, {'error': '请求格式或大小不正确'})
            return None
        return body

    def serve_static(self):
        name = static_name(self.path)
        if name is None:
            self.send_body(404, {'error': '未找到页面'})
            return
        try:
            entry = static_cache(self.server.static_dir / name)
        except FileNotFoundError:
            self.send_body(404, {'error': '未找到页面'})
            return
        except OSError:
            self.send_body(503, {'error': '页面文件暂不可用'})
            return
        gzipped = accepts_gzip(self.headers.get('Accept-Encoding'))
        etag = entry['etag_gzip'] if gzipped else entry['etag']
        ctype = STATIC_TYPES[name.rsplit('.', 1)[1]]
        known = {tag.strip() for tag in self.headers.get('If-None-Match', '').split(',')}
        if etag in known or '*' in known:
            self.send_response(304)
            self.send_header('ETag', etag)
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Vary', 'Accept-Encoding')
            self.end_headers()
            return
        self.send_body(200, entry['gzip'] if gzipped else entry['data'], ctype, cache='no-cache',
                       etag=etag, encoding='gzip' if gzipped else None, vary=True)

    # ---------- GET handlers (session checked by the dispatcher) ----------
    def get_version(self):
        engine = getattr(self.server.app, 'engine', None)
        self.send_body(200, {**version_info(), 'ready': not bool(getattr(engine, 'gateway_error', None))})

    def get_updates(self):
        updater = getattr(self.server.app, 'updates', None)
        if updater is None:
            self.send_body(503, {'error': '更新服务未启用'})
        else:
            self.send_body(200, updater.status())

    def get_state(self):
        try:
            self.send_body(200, {**self.server.app.state(), 'csrf': self.session['csrf']})
        except Exception:
            logging.exception('Read state failed')
            self.send_body(500, {'error': '读取状态失败，请检查服务日志'})

    def get_activity(self):
        try:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            limit = max(1, min(int((query.get('limit') or ['200'])[0]), 500))
            activity = self.server.app.activity
            if activity.updated is None:
                # First read after start: do not make the user wait for the poller tick.
                activity.poll_once()
            self.send_body(200, activity.snapshot(limit))
        except Exception:
            logging.exception('Read activity failed')
            self.send_body(500, {'error': '读取网络活动失败，请检查服务日志'})

    def get_repo(self):
        try:
            summary = self.server.repo.summary()
            settings = getattr(self.server.app, 'settings', None)
            host = getattr(settings, 'host_ip', None) or self.server.server_address[0]
            port = getattr(settings, 'repo_port', None) or 8080
            plugin_updates = getattr(self.server, 'plugin_updates', None)
            self.send_body(200, {**summary, 'host': host, 'port': port,
                                 'max_upload': self.server.repo.MAX_UPLOAD,
                                 'upstream': plugin_updates.status() if plugin_updates else None})
        except Exception:
            logging.exception('Read repository failed')
            self.send_body(500, {'error': '读取插件仓库失败，请检查服务日志'})

    def get_upstream_progress(self):
        plugin_updates = getattr(self.server, 'plugin_updates', None)
        self.send_body(200, {'progress': plugin_updates.progress() if plugin_updates else None})

    def get_console(self):
        return self.server.app.console_get(self)

    def handle_titles(self):
        """GET /api/titles?ids=CUSA1,PPSA2&folder=/mnt/ext0/games/PPSA2 -> names."""
        catalog = getattr(self.server.app, 'titles', None)
        if catalog is None:
            self.send_body(200, {'titles': {}, 'pending': [], 'enabled': False})
            return
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        ids = [part for value in query.get('ids', []) for part in value.split(',') if part]
        try:
            result = catalog.lookup(ids, query.get('folder', []))
        except Exception:
            logging.exception('Title lookup failed')
            self.send_body(500, {'error': '查询游戏名称失败，请检查服务日志'})
            return
        self.send_body(200, result)

    def handle_logs(self):
        """GET /api/logs?level=&q=&after=&limit= (viewer) and /api/logs/export (bundle)."""
        parts = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parts.query)

        def first(key, default=''):
            return (query.get(key) or [default])[0]

        if parts.path == '/api/logs':
            try:
                after = max(0, int(first('after', '0')))
                limit = int(first('limit', '500'))
            except ValueError:
                self.send_body(400, {'error': '日志查询参数不正确'})
                return
            result = logbook.query(level=first('level', 'INFO'), text=first('q')[:200],
                                   after=after, limit=limit)
            path = logbook.file_path()
            self.send_body(200, {**result, 'file': str(path) if path else ''})
            return
        try:
            report = diagnostic_report(self.server)
        except Exception:
            logging.exception('Build diagnostic report failed')
            report = '（诊断摘要生成失败，见下方日志）\n'
        stored = logbook.file_text()
        body = stored or logbook.memory_text()
        source = '日志文件 ' + str(logbook.file_path()) if stored else '内存缓冲（日志文件不可用）'
        stamp = time.strftime('%Y%m%d-%H%M%S')
        text = (report + '\n==== 服务日志（' + source + '）====\n' + (body or '（暂无日志）\n'))
        self.send_body(200, {'filename': f'ps5-manager-logs-{stamp}.txt', 'text': text})

    def handle_upload(self):
        """Raw-body upload: metadata in the query string, bytes in the body."""
        if not self.require_session(write=True):
            return
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        filename = (query.get('filename') or [''])[0]
        meta = {key: (query.get(key) or [''])[0] for key in
                ('name', 'version', 'category', 'author', 'description')}
        replace = (query.get('replace') or [''])[0].lower() in ('1', 'true', 'yes')
        if self.headers.get('Transfer-Encoding'):
            self.send_body(400, {'error': '不支持分块上传'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            length = 0
        if length <= 0:
            self.send_body(400, {'error': '文件为空'})
            return
        if length > self.server.repo.MAX_UPLOAD:
            self.send_body(413, {'error': f'文件超过 {self.server.repo.limit_label()} 上限'})
            return
        data = self.rfile.read(length)
        if len(data) != length:
            self.send_body(400, {'error': '上传中断，请重试'})
            return
        try:
            entry = self.server.repo.add(filename, data, replace=replace, **meta)
        except FileExistsError as exc:
            self.send_body(409, {'error': str(exc)})
            return
        except FileNotFoundError as exc:
            self.send_body(404, {'error': str(exc)})
            return
        except ValueError as exc:
            self.send_body(400, {'error': str(exc)})
            return
        except Exception:
            logging.exception('Repository upload failed')
            self.send_body(500, {'error': '保存失败，请检查服务日志'})
            return
        logging.info('repository %s: %s (%d bytes)', 'replace' if replace else 'upload',
                     entry['filename'], length)
        self.send_body(200, {'ok': True, 'filename': entry['filename'], 'replaced': replace})

    def handle_repo_version(self, body):
        """Upstream update, rollback and archived-version removal for one plugin."""
        route = self.path.split('?', 1)[0]
        filename = body.get('filename')
        if not isinstance(filename, str):
            raise ValueError('请指定插件文件')
        if route == '/api/repo/upstream/apply':
            plugin_updates = getattr(self.server, 'plugin_updates', None)
            if plugin_updates is None:
                self.send_body(503, {'error': '插件更新检查未启用'})
                return
            try:
                result = plugin_updates.apply(filename)
            except PluginUpdateError as exc:
                self.send_body(409, {'error': str(exc)})
                return
            self.send_body(200, result)
            return
        checksum = body.get('checksum')
        if not isinstance(checksum, str):
            raise ValueError('请指定历史版本')
        if route == '/api/repo/rollback':
            item = self.server.repo.rollback(filename, checksum)
            logging.info('repository rollback: %s -> %s', filename, item['filename'])
            self.send_body(200, {'ok': True, 'filename': item['filename'],
                                 'version': item.get('version', '')})
        else:
            self.server.repo.delete_version(filename, checksum)
            self.send_body(200, {'ok': True})

    # ---------- POST handlers (JSON body; session and CSRF checked by the dispatcher) ----------
    def post_login(self, body):
        try:
            token, csrf = self.server.auth.login(self.client_address[0], body.get('password'))
            self.send_body(200, {'csrf': csrf}, cookie=f'ps5_session={token}; Path=/; Max-Age={AuthManager.IDLE_SECONDS}; HttpOnly; Secure; SameSite=Lax')
        except PermissionError as exc:
            self.send_body(401, {'error': str(exc)})

    def post_updates(self, body):
        updater = getattr(self.server.app, 'updates', None)
        if updater is None:
            self.send_body(503, {'error': '更新服务未启用'})
        elif self.path.split('?', 1)[0].endswith('/check'):
            self.send_body(200, updater.check())
        else:
            if set(body) != {'version'} or not isinstance(body.get('version'), str):
                raise UpdateError('请指定检查结果中的目标版本')
            self.send_body(200, updater.apply(body['version']))

    def post_logout(self, body):
        self.server.auth.logout(self.token())
        self.send_body(200, {'ok': True}, cookie='ps5_session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax')

    def post_password(self, body):
        change_password(self.server.auth, self.server.credentials_path,
                        body.get('current_password'), body.get('new_password'))
        self.send_body(200, {'ok': True},
                       cookie='ps5_session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax')

    def post_labels(self, body):
        self.server.app.activity.labels.set(body.get('target'),
                                            body.get('name', ''), body.get('category', ''))
        self.send_body(200, {'ok': True})

    def post_allow(self, body):
        targets = body.get('targets')
        if not isinstance(targets, list) or not targets:
            raise ValueError('请选择要放行的条目')
        if len(targets) > 200:
            raise ValueError('一次最多放行 200 项')
        self.send_body(200, {'ok': True, 'config': self.server.app.allow(targets)})

    def post_repo_update(self, body):
        self.server.repo.update(body.get('filename'),
                                **{key: body[key] for key in RepoManager._FIELDS if key in body})
        self.send_body(200, {'ok': True})

    def post_upstream_check(self, body):
        plugin_updates = getattr(self.server, 'plugin_updates', None)
        if plugin_updates is None:
            self.send_body(503, {'error': '插件更新检查未启用'})
        else:
            self.send_body(200, plugin_updates.check())

    def post_repo_delete(self, body):
        self.server.repo.delete(body.get('filename'))
        self.send_body(200, {'ok': True})

    def post_config(self, body):
        self.server.app.change(body, rollback=self.path.split('?', 1)[0] == '/api/rollback')
        self.send_body(200, {'ok': True})

    def post_console(self):
        self.server.app.console_post(self, self.path.split('?', 1)[0])

    # ---------- notifications ----------
    def _notify_parts(self):
        console = getattr(self.server.app, 'console', None)
        store = getattr(console, 'notify_store', None)
        if store is None:
            self.send_body(503, {'error': '通知功能未启用'})
        return console, store

    def get_notify(self):
        console, store = self._notify_parts()
        if store is not None:
            self.send_body(200, {'channels': store.list_public(), 'events': NOTIFY_EVENTS, 'types': NOTIFY_TYPES})

    def post_notify(self, body):
        """POST /api/notify/{save,delete,test}."""
        console, store = self._notify_parts()
        if store is None:
            return
        action = self.path.split('?', 1)[0].rsplit('/', 1)[-1]
        try:
            if action == 'save':
                channel = store.save(body.get('channel'))
                logging.info('notify channel saved: %s (%s)', channel['name'], channel['type'])
                self.send_body(200, {'channel': channel, 'channels': store.list_public()})
            elif action == 'delete':
                store.delete(str(body.get('id') or ''))
                self.send_body(200, {'channels': store.list_public()})
            else:
                console.notifier.test(str(body.get('id') or ''))
                self.send_body(200, {'ok': True})
        except NotifyError as exc:
            self.send_body(502 if action == 'test' and str(exc).startswith('发送失败') else 400, {'error': str(exc)})

    # ---------- self-check ----------
    def _selfcheck(self):
        service = getattr(self.server.app, 'selfcheck', None)
        if service is None:
            self.send_body(503, {'error': '自检未启用'})
        return service

    def get_selfcheck(self):
        """GET /api/selfcheck: the latest result kept in memory (null before the first run)."""
        service = self._selfcheck()
        if service is not None:
            self.send_body(200, {'last': service.last})

    def post_selfcheck(self, body):
        """POST /api/selfcheck: run every check now (it sends probes, so it needs CSRF)."""
        service = self._selfcheck()
        if service is None:
            return
        try:
            self.send_body(200, {'last': service.run()})
        except RuntimeError as exc:
            self.send_body(409, {'error': str(exc)})

    # ---------- backup and restore ----------
    def _backup_service(self):
        service = getattr(self.server.app, 'backup', None)
        if service is None:
            self.send_body(503, {'error': '备份功能未启用'})
        return service

    def get_backup(self):
        """GET /api/backup?payloads=0|1: an unencrypted backup (no NAS key)."""
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        payloads = (query.get('payloads') or [''])[0] in ('1', 'true')
        try:
            self._send_backup(payloads, False, '')
        except BackupError as exc:
            self.send_body(400, {'error': str(exc)})
        except Exception as exc:
            logging.exception('Backup failed')
            self.send_body(500, {'error': '备份失败：' + diagnostic_error(exc)})

    def post_backup(self, body):
        """POST /api/backup {payloads, include_key, passphrase}: the binary backup."""
        include_key = body.get('include_key') is True
        passphrase = body.get('passphrase') if include_key else ''
        self._send_backup(body.get('payloads') is True, include_key, passphrase if isinstance(passphrase, str) else '')

    def _send_backup(self, payloads, include_key, passphrase):
        service = self._backup_service()
        if service is None:
            return
        path, name = service.create(payloads=payloads, include_key=include_key, passphrase=passphrase)
        try:
            self.send_file(path, name, 'application/octet-stream' if include_key else 'application/gzip')
            logging.info('backup downloaded: %s (%d bytes, payloads=%s, key=%s)', name,
                         path.stat().st_size, payloads, include_key)
        finally:
            service.discard(path)

    def send_file(self, path, filename, ctype):
        """Send a local file as an attachment with a known length."""
        size = Path(path).stat().st_size
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(size))
        self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        with open(path, 'rb') as stream:
            shutil.copyfileobj(stream, self.wfile, 1 << 20)

    def post_backup_restore(self):
        """POST /api/backup/restore: raw backup body, X-Backup-Passphrase (URL-encoded)."""
        if not self.require_session(write=True):
            return
        service = self._backup_service()
        if service is None:
            return
        console = getattr(self.server.app, 'console', None)
        if console is not None and update_busy(console):
            self.send_body(409, {'error': '有传输、解压或复制任务在进行，完成后再恢复'})
            return
        if self.headers.get('Transfer-Encoding'):
            self.send_body(400, {'error': '不支持分块上传'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            length = 0
        limit = restore_limit(getattr(self.server, 'repo', None))
        if length <= 0:
            self.send_body(400, {'error': '备份文件为空'})
            return
        if length > limit:
            self.send_body(413, {'error': f'备份文件超过 {limit >> 20} MB 上限'})
            return
        passphrase = urllib.parse.unquote(self.headers.get('X-Backup-Passphrase', ''))
        path = service.receive(self.rfile, length)
        self.send_body(200, service.restore(path, passphrase))


def restore_limit(repo):
    """A restore upload may carry every plugin file: eight times the upload limit."""
    return (getattr(repo, 'MAX_UPLOAD', None) or RepoManager.MAX_UPLOAD) * 8


STATIC_TYPES = {'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8',
                'css': 'text/css; charset=utf-8'}
_STATIC_NAME = re.compile(WEB_FILE_PATTERN + r'\Z')


_STATIC_CACHE = {}
_STATIC_LOCK = threading.Lock()


def static_cache(path):
    """Content, gzip copy and ETags of a front-end file, recomputed when mtime or size change."""
    info = path.stat()
    key = (info.st_mtime_ns, info.st_size)
    with _STATIC_LOCK:
        cached = _STATIC_CACHE.get(path)
        if cached and cached['key'] == key:
            return cached
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()[:16]
    entry = {'key': key, 'data': data, 'gzip': gzip.compress(data, compresslevel=9, mtime=0),
             'etag': f'"{digest}"', 'etag_gzip': f'"{digest}-gz"'}
    with _STATIC_LOCK:
        _STATIC_CACHE[path] = entry
    return entry


def static_name(path):
    """File name under static_dir for a request path, or None. The pattern has no '/'."""
    name = path.split('?', 1)[0].removeprefix('/') or 'index.html'
    return name if _STATIC_NAME.match(name) else None


REPO_VERSION_ROUTES = ('/api/repo/upstream/apply', '/api/repo/rollback', '/api/repo/version/delete')


@dataclass(frozen=True)
class Route:
    """One admin endpoint.

    auth: 'read' / 'write' (session, plus CSRF for write) checked by the
    dispatcher, or None when public or the handler checks it itself.
    tracked: refused while a service upgrade runs and counted as an operation
    the upgrade waits for (paths ending in /cancel are never tracked).
    body: 'json' is parsed by the dispatcher and passed to the handler; any
    other body is read by the handler.
    exact: GET only; the route matches only without a query string.
    """
    handler: str
    auth: str | None = 'read'
    tracked: bool = False
    body: str | None = None
    exact: bool = False


CONSOLE_PREFIXES = ('/api/ps5', '/api/files', '/api/nas', '/api/transfers')

GET_ROUTES = {
    '/api/version': Route('get_version', auth=None, exact=True),
    '/api/updates': Route('get_updates', exact=True),
    '/api/state': Route('get_state', exact=True),
    '/api/repo': Route('get_repo', exact=True),
    '/api/titles': Route('handle_titles'),
    '/api/logs': Route('handle_logs'),
    '/api/logs/export': Route('handle_logs'),
    '/api/repo/upstream/progress': Route('get_upstream_progress', exact=True),
    '/api/backup': Route('get_backup'),
    '/api/selfcheck': Route('get_selfcheck', exact=True),
    '/api/notify': Route('get_notify', exact=True),
}
# Checked in order after GET_ROUTES; a prefix also matches longer paths.
GET_PREFIXES = (
    ('/api/activity', Route('get_activity')),
    (CONSOLE_PREFIXES, Route('get_console', auth=None)),
)


def _json_route(handler, tracked=False, auth='write'):
    return Route(handler, auth=auth, tracked=tracked, body='json')


POST_ROUTES = {
    '/api/login': _json_route('post_login', auth=None),
    '/api/config': _json_route('post_config'),
    '/api/rollback': _json_route('post_config'),
    '/api/logout': _json_route('post_logout'),
    '/api/password': _json_route('post_password'),
    '/api/labels': _json_route('post_labels'),
    '/api/allow': _json_route('post_allow'),
    '/api/updates/check': _json_route('post_updates'),
    '/api/updates/apply': _json_route('post_updates'),
    '/api/repo/upstream/check': _json_route('post_upstream_check'),
    '/api/repo/update': _json_route('post_repo_update', tracked=True),
    '/api/repo/delete': _json_route('post_repo_delete', tracked=True),
    **{route: _json_route('handle_repo_version', tracked=True) for route in REPO_VERSION_ROUTES},
    # Raw body; the handler checks session and CSRF itself.
    '/api/repo/upload': Route('handle_upload', auth=None, tracked=True, body='raw'),
    '/api/backup': _json_route('post_backup'),
    '/api/selfcheck': _json_route('post_selfcheck'),
    **{f'/api/notify/{action}': _json_route('post_notify') for action in ('save', 'delete', 'test')},
    '/api/backup/restore': Route('post_backup_restore', auth=None, tracked=True, body='raw'),
}
POST_PREFIXES = (
    # The console API checks session, CSRF and the body itself.
    (CONSOLE_PREFIXES, Route('post_console', auth=None, tracked=True)),
)


def _lookup(routes, prefixes, raw_path):
    path = raw_path.split('?', 1)[0]
    route = routes.get(path)
    if route is not None:
        return None if route.exact and raw_path != path else route
    for prefix, candidate in prefixes:
        if path.startswith(prefix):
            return candidate
    return None


class AdminServer(LimitedThreads, HTTPServer):
    allow_reuse_address = True
    # Idle keep-alive connections hold a slot (browsers open up to 6 per tab).
    SLOTS = 64
    def get_request(self):
        conn, address = super().get_request()
        conn.settimeout(5)
        return conn, address

    def finish_request(self, request, client_address):
        context = getattr(self, 'tls_context', None)
        if context is None:
            return super().finish_request(request, client_address)
        # This runs only after LimitedThreads admits a bounded worker. Slow
        # ClientHello senders must not monopolize the server's accept loop.
        try:
            with context.wrap_socket(request, server_side=True,
                                     do_handshake_on_connect=False) as connection:
                connection.settimeout(5)
                connection.do_handshake()
                self.RequestHandlerClass(connection, client_address, self)
        except (ssl.SSLError, OSError):
            logging.debug('TLS connection closed before request: %s', client_address[0])


def _catalog_reader(path):
    def read():
        try:
            catalog = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return {'items': []}
        return {'items': catalog.get('payloads', [])}
    return read


def diagnostic_report(server):
    """Plain-text header for an exported log: versions, layout and health, no secrets."""
    import platform
    app = server.app
    settings = getattr(app, 'settings', None)
    get = lambda name, default='': getattr(settings, name, default) if settings else default  # noqa: E731
    lines = ['==== PS5 管家 诊断摘要 ====',
             '生成时间：' + time.strftime('%Y-%m-%d %H:%M:%S %z'),
             '服务版本：' + VERSION,
             'Python：' + platform.python_version() + ' · ' + platform.platform(terse=True),
             f"管理入口：https://{get('host_ip')}:{get('admin_port')}  服务网段：{get('subnet')}",
             f"数据目录：{get('data_dir')}  仓库目录：{get('repo_dir')}"]
    try:
        status = app.state()['status']
        lines.append('运行时长：{} 秒 · DNS {} · 网关 {} · 用户手册 {}'.format(
            status.get('uptime'), status.get('dns'), status.get('gateway'), status.get('host')))
        if status.get('interface'):
            lines.append(f"网关网卡：{status['interface']}")
        for key, label in (('gateway_error', '网关错误'), ('last_error', '最近应用错误')):
            if status.get(key):
                lines.append(f'{label}：{status[key]}')
    except Exception as exc:
        lines.append(f'服务状态读取失败：{exc}')
    try:
        units = app.units()
        lines.append('系统服务：' + '，'.join(f"{item['name']}={item['state']}" for item in units.values()))
    except Exception:
        pass
    updater = getattr(app, 'updates', None)
    if updater is not None:
        try:
            status = updater.status()
            job = status.get('job') or {}
            lines.append('服务更新：最新 {} · 任务 {} {}'.format(
                status.get('latest') or '未检查', job.get('state') or '无', job.get('error') or '').rstrip())
        except Exception:
            pass
    checker = getattr(app, 'selfcheck', None)
    if checker is not None:
        try:
            lines.extend(checker.report_lines())
        except Exception:
            pass
    plugin_updates = getattr(server, 'plugin_updates', None)
    if plugin_updates is not None:
        try:
            upstream = plugin_updates.status()
            lines.append('插件上游：检查于 {} · 可更新 {} · {}'.format(
                upstream.get('checked_at') or '未检查', upstream.get('available'),
                upstream.get('error') or '无错误'))
        except Exception:
            pass
    return '\n'.join(lines) + '\n'


def update_busy(console):
    """An update restarts the service; do not interrupt in-flight console writes."""
    terminal = {'done', 'done_with_warnings', 'failed', 'fail', 'canceled', 'cancelled', 'idle'}
    if any(job.get('state') not in terminal for job in console.transfers.snapshot()):
        return True
    try:
        job = console.files.job()
        if isinstance(job, dict):
            state = job.get('state') or (job.get('job') or {}).get('state')
            if state and state not in terminal:
                return True
        transfers = console.files.transfers()
        for job in transfers.get('transfers', []) if isinstance(transfers, dict) else []:
            if job.get('state') not in terminal:
                return True
    except Exception:
        # A disconnected console with no server transfer is not an active job.
        pass
    return False


def main():
    parser = argparse.ArgumentParser()
    # Unset flags fall back to /etc/ps5-jb-server/config.toml (then env, then detection).
    parser.add_argument('--data-dir')
    parser.add_argument('--ip')
    parser.add_argument('--admin-port', type=int)
    parser.add_argument('--dns-port', type=int)
    parser.add_argument('--repo-dir')
    parser.add_argument('--lan')
    parser.add_argument('--cert')
    parser.add_argument('--key')
    parser.add_argument('--config', default='/etc/ps5-jb-server/config.toml')
    args = parser.parse_args()
    settings = Settings.load(args.config)
    data_dir = Path(args.data_dir) if args.data_dir else settings.data_dir
    repo_dir = Path(args.repo_dir) if args.repo_dir else settings.repo_dir
    host_ip = args.ip or settings.host_ip
    lan = args.lan or settings.subnet
    admin_port = args.admin_port or settings.admin_port
    dns_port = args.dns_port or settings.dns_port
    cert = args.cert or str(settings.cert_dir / 'admin.crt')
    key = args.key or str(settings.cert_dir / 'admin.key')
    # CLI wins over the file, and everything downstream must see the same values.
    settings.host_ip, settings.subnet = host_ip, lan
    settings.admin_port, settings.dns_port = admin_port, dns_port
    settings.data_dir, settings.repo_dir = data_dir, repo_dir
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logbook.install(data_dir / 'logs')
    # A proxy set for GitHub must not reach the PS5, the NAS or the local repo.
    netproxy.seal_direct()
    data = data_dir
    store = ConfigStore(data / 'config.json')
    # Resolved here, not in Settings.from_mapping, so loading settings stays side-effect free.
    configured = bool(settings.interface)
    interface = settings.interface or detect_interface(host_ip)
    settings.interface = interface
    logging.info('Gateway interface: %s (%s)', interface, 'configured' if configured else 'detected')
    engine = create_engine(store, host_ip, lan, interface=interface)
    initialize_engine(engine)
    engine.on_apply = set_forwarding
    auth = AuthManager(json.loads((data / 'credentials.json').read_text()),
                       data / 'sessions.json')
    admin = AdminServer((host_ip, admin_port), AdminHandler)
    labels = LabelsStore(data / 'labels.json')
    observer = FlowObserver(
        conntrack=_read_conntrack,
        classifier=Classifier(settings.rulesets_path),
        labels=labels,
        dns_lookup=engine.lookup_ip,
        is_allowed=_allowed_destination(engine),
        lan=lan, host_ip=host_ip,
        dns_events=lambda: list(engine.logs),
        domain_allowed=engine.domain_allowed_now)
    stop_event = threading.Event()
    threading.Thread(target=make_poller(observer), args=(stop_event,), daemon=True).start()
    admin.app, admin.auth = Application(engine, observer), auth
    admin.app.settings = settings
    admin.app.console = ConsoleAPI(settings, _catalog_reader(repo_dir / 'repo.json'),
                                   autoscan=True)
    admin.app.titles = TitleCatalog(settings.titles_path, data / 'titles-cache.json',
                                    reader=proxy_reader(admin.app.console.files))
    admin.repo = RepoManager(repo_dir,
                             base_url=f'http://{host_ip}:{settings.repo_port}/payloads/',
                             max_upload=settings.max_upload)
    admin.app.repo = admin.repo
    admin.app.backup = BackupService(data, policy=store.snapshot, apply_policy=engine.change_config,
                                     labels=labels, console=admin.app.console, repo=admin.repo,
                                     notify=admin.app.console.notify_store)
    admin.app.backup.cleanup()
    admin.app.selfcheck = SelfCheck(lambda: CheckContext(settings, engine=engine, console=admin.app.console,
                                                         repo=admin.repo, units=admin.app.units))
    admin.plugin_updates = PluginUpdates(admin.repo, data / 'plugin-upstream.json',
                                         url=settings.plugin_upstream)
    admin.app.updates = UpdateManager(data, busy=lambda: update_busy(admin.app.console))
    admin.credentials_path = data / 'credentials.json'
    admin.static_dir = settings.static_dir
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    admin.tls_context = ctx
    dns_servers = start_dns(engine, '0.0.0.0', dns_port)
    admin.app.console.notifier.watch(update_status=admin.app.updates.status,
                                     gateway_error=lambda: engine.gateway_error)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    threading.Thread(target=admin.serve_forever, daemon=True).start()
    logging.info('DNS and gateway active; admin https://%s:%d', host_ip, admin_port)
    stop.wait()
    stop_event.set()
    for server in [admin] + dns_servers:
        server.shutdown()
        server.server_close()
    # Leave deny-by-default kernel rules in place on service shutdown.


if __name__ == '__main__':
    main()
