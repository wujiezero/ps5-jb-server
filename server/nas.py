"""NAS sources (WebDAV / SMB / FTP) and NAS -> PS5 transfer jobs.

Bytes flow NAS -> this service -> PS5 file service as a stream; nothing is
written to local disk. Connection settings live in <data>/nas.json (0600);
usernames and passwords are stored only as AES-256-GCM ciphertext (see
SecretBox) and passwords never leave the server: the API only reports whether
one is set.
"""
import base64
import collections
import datetime
import email.utils
import ftplib
import ipaddress
import json
import logging
import os
import posixpath
import re
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import archive
import shutil
import subprocess
import sys
import unrar
from files import FileServiceError, VOLUME_PATTERN
from game_services import PACKAGE_PHASES, TITLE, install_timeout, parse_cheat_zip, write_cheats
from ftp_files import listing
from migrate import Canceled as _Canceled, MigrationJobs
from uninstall import UninstallJobs

PROTOCOLS = ('webdav', 'smb', 'ftp')
DEFAULT_PORTS = {'webdav': 5005, 'smb': 445, 'ftp': 21}
CHUNK = 256 * 1024
LIST_LIMIT = 5000
_HOST = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\Z')
_DAV = '{DAV:}'


class NasError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def smb_available():
    try:
        import smbc  # noqa: F401  (Debian: python3-smbc)
        return True
    except ImportError:
        return False


def clean_path(path):
    """Normalise a NAS path to '/a/b'; reject traversal and control characters."""
    if not isinstance(path, str):
        raise ValueError('路径格式不正确')
    if any(ord(c) < 32 for c in path):
        raise ValueError('路径包含非法字符')
    parts = [part for part in path.replace('\\', '/').split('/') if part not in ('', '.')]
    if '..' in parts:
        raise ValueError('路径不允许包含 ..')
    result = '/' + '/'.join(parts)
    if len(result) > 4096:
        raise ValueError('路径过长')
    return result


# ---------------------------------------------------------------- settings --

class SecretBox:
    """AES-256-GCM for NAS credentials.

    The 32-byte key lives in its own file (default /etc/ps5-jb-server/nas.key,
    0600), apart from nas.json in the data directory, so a copy or backup of
    the data directory alone does not reveal any NAS login. Each ciphertext is
    bound to its connection id (AAD), so it cannot be moved to another entry.
    Anyone with root on this host can still read both files; that is inherent
    to a service that must log in to the NAS unattended.
    """

    VERSION = 'v1'

    def __init__(self, key_path, fallback_path=None):
        self.key_path = Path(key_path) if key_path else None
        self.fallback_path = Path(fallback_path) if fallback_path else None
        self._key = None
        self.location = None

    @staticmethod
    def available():
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
            return True
        except ImportError:
            return False

    def _aead(self):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            raise NasError('服务器缺少加密组件：请执行 apt install python3-cryptography，'
                           '否则无法安全保存 NAS 账号') from exc
        return AESGCM(self.key())

    @staticmethod
    def _read(path):
        raw = path.read_bytes().strip()
        key = base64.b64decode(raw, validate=True)
        if len(key) != 32:
            raise ValueError('NAS 密钥长度不正确')
        return key

    @staticmethod
    def _create(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        # O_EXCL: never overwrite a key that would orphan stored ciphertext.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(base64.b64encode(key) + b'\n')
            stream.flush()
            os.fsync(stream.fileno())
        return key

    def key(self):
        if self._key is not None:
            return self._key
        candidates = [path for path in (self.key_path, self.fallback_path) if path]
        for path in candidates:
            if path.exists():
                self._key, self.location = self._read(path), path
                return self._key
        for path in candidates:
            try:
                self._key, self.location = self._create(path), path
                logging.info('created NAS credential key at %s', path)
                return self._key
            except FileExistsError:
                self._key, self.location = self._read(path), path
                return self._key
            except OSError:
                continue      # e.g. /etc is read-only under ProtectSystem=strict
        raise NasError('无法创建 NAS 凭据密钥文件')

    def existing(self):
        """Path of the key file in use, without creating one."""
        if self.location:
            return self.location
        return next((path for path in (self.key_path, self.fallback_path) if path and path.exists()), None)

    def export(self):
        """The key as stored on disk (base64 text), for an encrypted backup."""
        return base64.b64encode(self.key()) + b'\n'

    def install(self, raw):
        """Replace the key with one restored from a backup."""
        try:
            key = base64.b64decode(bytes(raw).strip(), validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError('备份里的 NAS 密钥格式不正确') from exc
        if len(key) != 32:
            raise ValueError('备份里的 NAS 密钥长度不正确')
        current = self.existing()
        for path in [current] if current else [p for p in (self.key_path, self.fallback_path) if p]:
            temporary = path.with_name('.' + path.name + '.tmp')
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(base64.b64encode(key) + b'\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            except OSError:
                continue
            self._key, self.location = key, path
            return path
        raise NasError('无法写入 NAS 凭据密钥文件')

    def seal(self, ident, username, password):
        if not username and not password:
            return ''
        nonce = secrets.token_bytes(12)
        plain = json.dumps({'u': username, 'p': password}, ensure_ascii=False).encode()
        sealed = self._aead().encrypt(nonce, plain, b'ps5jb-nas:' + ident.encode())
        return f'{self.VERSION}:' + base64.urlsafe_b64encode(nonce + sealed).decode()

    def open(self, ident, blob):
        if not blob:
            return '', ''
        try:
            version, payload = blob.split(':', 1)
            if version != self.VERSION:
                raise ValueError('unknown version')
            data = base64.urlsafe_b64decode(payload.encode())
            plain = self._aead().decrypt(data[:12], data[12:], b'ps5jb-nas:' + ident.encode())
            values = json.loads(plain)
            return str(values.get('u') or ''), str(values.get('p') or '')
        except NasError:
            raise
        except Exception as exc:  # wrong key, tampered file, unknown format
            raise NasError('NAS 账号无法解密（密钥文件可能被更换），请重新填写用户名和密码') from exc


class NasStore:
    FIELDS = ('name', 'protocol', 'host', 'port', 'share', 'path', 'username',
              'tls', 'verify_tls', 'workgroup', 'writable')
    # Written to disk: never the username or password themselves.
    STORED = ('id', 'name', 'protocol', 'host', 'port', 'share', 'path',
              'tls', 'verify_tls', 'workgroup', 'writable', 'credentials')

    def __init__(self, path, key_path=None):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.box = SecretBox(key_path, self.path.with_name('nas.key'))
        try:
            document = json.loads(self.path.read_text(encoding='utf-8'))
            self.items = [item for item in document.get('connections', [])
                          if isinstance(item, dict) and item.get('id')]
        except (OSError, ValueError, AttributeError):
            self.items = []
        self._migrate()

    def _migrate(self):
        """Encrypt entries written by versions that stored plain credentials."""
        plain = [item for item in self.items if 'password' in item or 'username' in item]
        if not plain:
            return
        try:
            for item in plain:
                item['credentials'] = self.box.seal(item['id'], str(item.pop('username', '') or ''),
                                                    str(item.pop('password', '') or ''))
            self._save()
            logging.info('encrypted %d stored NAS credential(s)', len(plain))
        except NasError:
            logging.exception('NAS credentials could not be encrypted')

    def _save(self):
        stored = [{key: item.get(key) for key in self.STORED if key in item} for item in self.items]
        temporary = self.path.with_name('.' + self.path.name + '.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump({'version': 2, 'connections': stored}, stream, ensure_ascii=False, indent=1)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def _credentials(self, item):
        if 'password' in item or 'username' in item:      # not migrated (no crypto available)
            return str(item.get('username') or ''), str(item.get('password') or '')
        return self.box.open(item['id'], item.get('credentials') or '')

    def public(self, item):
        result = {key: item.get(key) for key in self.FIELDS if key != 'username'}
        result['writable'] = item.get('writable') is True
        result['id'] = item['id']
        try:
            username, password = self._credentials(item)
            result['credentials_ok'] = True
        except NasError:
            username, password = '', ''
            result['credentials_ok'] = False
        result['username'] = username
        result['has_password'] = bool(password)
        return result

    def list_public(self):
        with self.lock:
            return [self.public(item) for item in self.items]

    def get(self, ident):
        """Full connection settings with decrypted credentials, for clients only."""
        with self.lock:
            for item in self.items:
                if item['id'] == ident:
                    result = {key: value for key, value in item.items() if key != 'credentials'}
                    result['username'], result['password'] = self._credentials(item)
                    return result
        raise NasError('没有这个 NAS 连接', status=404)

    def validate(self, raw, existing=None):
        if not isinstance(raw, dict):
            raise ValueError('连接配置格式不正确')
        protocol = str(raw.get('protocol') or '').lower()
        if protocol not in PROTOCOLS:
            raise ValueError('协议只能是 WebDAV、SMB 或 FTP')
        name = str(raw.get('name') or '').strip()[:40]
        host = str(raw.get('host') or '').strip()
        if host.startswith('[') and host.endswith(']'):
            host = host[1:-1]
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
            if not _HOST.match(host):
                raise ValueError('主机地址只能是 IP 或主机名，不要带 http:// 或路径')
        if address is not None and (address.is_loopback or address.is_unspecified
                                    or address.is_multicast):
            raise ValueError('不能连接回环、未指定或组播地址')
        port = raw.get('port')
        if port in (None, ''):
            port = DEFAULT_PORTS[protocol]
        try:
            port = int(port)
        except (TypeError, ValueError) as exc:
            raise ValueError('端口必须是数字') from exc
        if not 0 < port < 65536:
            raise ValueError('端口必须在 1–65535 之间')
        share = str(raw.get('share') or '').strip().strip('/')
        if '/' in share or '\\' in share or len(share) > 80:
            raise ValueError('共享名不能包含斜杠')
        username = str(raw.get('username') or '')[:128]
        workgroup = str(raw.get('workgroup') or '').strip()[:64]
        password = raw.get('password')
        if password in (None, '') and raw.get('keep_password', True) and existing:
            password = existing.get('password', '')
        password = str(password or '')[:256]
        return {
            'name': name or f'{protocol.upper()} {host}',
            'protocol': protocol, 'host': host, 'port': port,
            'share': share if protocol == 'smb' else '',
            'path': clean_path(str(raw.get('path') or '/')),
            'username': username, 'password': password,
            'tls': bool(raw.get('tls')), 'verify_tls': bool(raw.get('verify_tls', False)),
            'workgroup': workgroup,
            # PS5 -> NAS backups write here only when this is switched on.
            'writable': raw.get('writable') is True,
        }

    def save(self, raw):
        with self.lock:
            ident = raw.get('id') if isinstance(raw, dict) else None
            existing = None
            if ident:
                existing = self.get(ident)
            config = self.validate(raw, existing)
            if not existing and len(self.items) >= 20:
                raise ValueError('最多保存 20 个 NAS 连接')
            ident = ident or uuid.uuid4().hex[:12]
            item = {key: value for key, value in config.items() if key not in ('username', 'password')}
            item['id'] = ident
            item['credentials'] = self.box.seal(ident, config['username'], config['password'])
            if existing:
                self.items = [item if old['id'] == ident else old for old in self.items]
            else:
                self.items.append(item)
            self._save()
            return self.public(item)

    IDENT = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')

    def document(self):
        """What nas.json holds (credentials stay sealed), for a backup."""
        with self.lock:
            return {'version': 2, 'connections': [{key: item.get(key) for key in self.STORED if key in item}
                                                  for item in self.items]}

    def restore(self, document, key=None):
        """Replace the connections with a backup's.

        With the backup's key the sealed credentials come back as they were.
        Without it they cannot be opened here, so they are dropped: an entry
        keeps its current credentials only when this host already has it.
        Returns (connections, connections whose login must be entered again).
        """
        connections = document.get('connections') if isinstance(document, dict) else None
        if not isinstance(connections, list):
            raise ValueError('NAS 连接数据格式不正确')
        items = []
        for raw in connections[:20]:
            if not (isinstance(raw, dict) and isinstance(raw.get('id'), str) and self.IDENT.match(raw['id'])
                    and raw.get('protocol') in PROTOCOLS and isinstance(raw.get('host'), str)):
                continue
            items.append({k: raw[k] for k in self.STORED if k in raw})
        with self.lock:
            if key is not None:
                self.box.install(key)
            else:
                current = {item['id']: item.get('credentials', '') for item in self.items}
                for item in items:
                    item['credentials'] = current.get(item['id'], '')
            self.items = items
            self._save()
            blank = sum(1 for item in items if not item.get('credentials'))
        return len(items), blank

    def delete(self, ident):
        with self.lock:
            if not any(item['id'] == ident for item in self.items):
                raise NasError('没有这个 NAS 连接', status=404)
            self.items = [item for item in self.items if item['id'] != ident]
            self._save()


def client_for(config, timeout=15):
    protocol = config['protocol']
    if protocol == 'webdav':
        return WebDavClient(config, timeout)
    if protocol == 'smb':
        return SmbClient(config, timeout)
    if protocol == 'ftp':
        return FtpClient(config, timeout)
    raise ValueError('不支持的协议')


class _ChunkStream:
    """File-like view of an iterator of byte chunks (request bodies, STOR)."""

    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.buffer = b''

    def read(self, size=-1):
        size = CHUNK if size is None or size < 0 else size
        while len(self.buffer) < size:
            try:
                self.buffer += next(self.chunks)
            except StopIteration:
                break
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data


def _require_writable(config):
    if config.get('writable') is not True:
        raise NasError('这个 NAS 连接没有开启「允许写入」', status=403)


PART = '.part'


def put_file(client, path, chunks, size):
    """Write to <path>.part, check its size, then move it over path."""
    path = clean_path(path)
    part = path + PART
    client.write(part, chunks, size)
    try:
        written = client.stat(part).get('size')
    except NasError:
        written = None
    if written is not None and written != size:
        _quiet(client.delete, part)
        raise NasError(f'NAS 上的文件大小不符（{written} / {size} 字节）')
    try:
        client.stat(path)
        exists = True
    except NasError:
        exists = False
    if exists:
        client.delete(path)
    client.rename(part, path)


def _quiet(action, *args):
    try:
        action(*args)
    except Exception:
        pass


def _entry(path, name, is_dir, size=0, mtime=0):
    return {'name': name, 'path': posixpath.join(path, name) if path != '/' else '/' + name,
            'dir': bool(is_dir), 'size': 0 if is_dir else int(size or 0), 'mtime': int(mtime or 0)}


# ------------------------------------------------------------------ WebDAV --

class WebDavClient:
    PROPFIND = (b'<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:"><d:prop>'
                b'<d:resourcetype/><d:getcontentlength/><d:getlastmodified/></d:prop></d:propfind>')

    def __init__(self, config, timeout):
        self.config = config
        scheme = 'https' if config.get('tls') else 'http'
        host = config['host']
        if ':' in host:
            host = f'[{host}]'
        self.root = clean_path(config.get('path') or '/')
        self.origin = f'{scheme}://{host}:{config["port"]}'
        self.timeout = timeout
        # The NAS is on the LAN: never route it through an internet proxy.
        handlers = [urllib.request.ProxyHandler({})]
        if scheme == 'https':
            context = ssl.create_default_context()
            if not config.get('verify_tls'):
                # Home NAS boxes mostly use self-signed certificates.
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self.plain = urllib.request.build_opener(*handlers)     # no auth handlers: sees the challenge
        self.scheme = None
        if config.get('username'):
            manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            manager.add_password(None, self.origin, config['username'], config.get('password', ''))
            handlers += [urllib.request.HTTPBasicAuthHandler(manager),
                         urllib.request.HTTPDigestAuthHandler(manager)]
        self.opener = urllib.request.build_opener(*handlers)

    def _url(self, path):
        full = posixpath.join(self.root, clean_path(path).lstrip('/'))
        return self.origin + urllib.parse.quote(full)

    def _request(self, method, path, data=None, headers=None):
        request = urllib.request.Request(self._url(path), data=data, method=method,
                                         headers=headers or {})
        try:
            return self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 401:
                raise NasError('WebDAV 用户名或密码不正确', status=401) from exc
            if exc.code == 404:
                raise NasError('NAS 上找不到这个路径', status=404) from exc
            raise NasError(f'WebDAV 返回 HTTP {exc.code}') from exc
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, 'reason', exc)
            raise NasError(f'无法连接 WebDAV 服务（{self.origin}）：{reason}') from exc

    def _propfind(self, path, depth):
        with self._request('PROPFIND', path, self.PROPFIND,
                           {'Depth': str(depth), 'Content-Type': 'application/xml; charset=utf-8'}) as resp:
            body = resp.read(16 << 20)
        try:
            return ET.fromstring(body)
        except ET.ParseError as exc:
            raise NasError('WebDAV 返回的目录数据无法解析') from exc

    @staticmethod
    def _props(response):
        href = urllib.parse.unquote(urllib.parse.urlsplit(response.findtext(f'{_DAV}href') or '').path)
        is_dir, size, mtime = False, 0, 0
        for propstat in response.findall(f'{_DAV}propstat'):
            status = propstat.findtext(f'{_DAV}status') or ''
            if ' 200 ' not in status + ' ':
                continue
            prop = propstat.find(f'{_DAV}prop')
            if prop is None:
                continue
            kind = prop.find(f'{_DAV}resourcetype')
            if kind is not None and kind.find(f'{_DAV}collection') is not None:
                is_dir = True
            try:
                size = int(prop.findtext(f'{_DAV}getcontentlength') or 0)
            except ValueError:
                size = 0
            modified = prop.findtext(f'{_DAV}getlastmodified')
            if modified:
                try:
                    mtime = int(email.utils.parsedate_to_datetime(modified).timestamp())
                except (TypeError, ValueError):
                    mtime = 0
        return href, is_dir, size, mtime

    def list(self, path):
        path = clean_path(path)
        tree = self._propfind(path, 1)
        own = posixpath.join(self.root, path.lstrip('/')).rstrip('/')
        entries = []
        for response in tree.findall(f'{_DAV}response'):
            href, is_dir, size, mtime = self._props(response)
            if href.rstrip('/') == own or not href.rstrip('/'):
                continue
            name = href.rstrip('/').rsplit('/', 1)[-1]
            if name:
                entries.append(_entry(path, name, is_dir, size, mtime))
            if len(entries) >= LIST_LIMIT:
                break
        return entries

    def stat(self, path):
        tree = self._propfind(path, 0)
        response = tree.find(f'{_DAV}response')
        if response is None:
            raise NasError('NAS 上找不到这个路径', status=404)
        _, is_dir, size, mtime = self._props(response)
        return {'dir': is_dir, 'size': size, 'mtime': mtime}

    def open(self, path):
        response = self._request('GET', path)
        return response

    def open_at(self, path, offset):
        """Stream from offset, or None when the server ignores Range (no 206)."""
        response = self._request('GET', path, headers={'Range': f'bytes={int(offset)}-'})
        if response.status != 206:
            response.close()
            return None
        return response

    def read_at(self, path, offset, size):
        """Bytes [offset, offset + size) of a file, or None when Range is ignored."""
        response = self._request('GET', path, headers={'Range': f'bytes={offset}-{offset + size - 1}'})
        with response:
            if response.status != 206:
                return None
            return response.read(size)

    # -- writing (only with "writable") --
    def mkdir(self, path):
        _require_writable(self.config)
        try:
            self._request('MKCOL', path).close()
        except NasError as exc:
            # 405: it already exists (RFC 4918); check it is a folder.
            if not self.stat(path)['dir']:
                raise NasError(f'NAS 上已有同名文件，不能建文件夹：{path}') from exc

    def _auth_scheme(self):
        """'none', 'basic' or 'digest': how this NAS wants requests authenticated."""
        if self.scheme is None:
            request = urllib.request.Request(self._url('/'), data=self.PROPFIND, method='PROPFIND',
                                             headers={'Depth': '0', 'Content-Type': 'application/xml'})
            try:
                self.plain.open(request, timeout=self.timeout).close()
                self.scheme = 'none'
            except urllib.error.HTTPError as exc:
                challenge = (exc.headers.get('WWW-Authenticate') or '').lower() if exc.headers else ''
                exc.close()
                self.scheme = ('basic' if 'basic' in challenge else 'digest' if 'digest' in challenge
                               else 'none')
            except (urllib.error.URLError, OSError) as exc:
                raise NasError(f'无法连接 WebDAV 服务（{self.origin}）：{getattr(exc, "reason", exc)}') from exc
        return self.scheme

    def write(self, path, chunks, size):
        _require_writable(self.config)
        headers = {'Content-Length': str(int(size)), 'Content-Type': 'application/octet-stream'}
        # A streamed body cannot be sent twice, so it must not meet a 401 challenge.
        scheme = self._auth_scheme()
        if scheme == 'digest':
            raise NasError('这台 NAS 的 WebDAV 只接受 Digest 认证，流式写入需要 Basic 认证（建议同时开启 HTTPS）')
        if scheme == 'basic' and self.config.get('username'):
            token = f'{self.config["username"]}:{self.config.get("password", "")}'.encode()
            headers['Authorization'] = 'Basic ' + base64.b64encode(token).decode()
        self._request('PUT', path, _ChunkStream(chunks), headers).close()

    def rename(self, source, target):
        _require_writable(self.config)
        self._request('MOVE', source, headers={'Destination': self._url(target), 'Overwrite': 'T'}).close()

    def delete(self, path):
        _require_writable(self.config)
        self._request('DELETE', path).close()

    rmdir = delete

    def close(self):
        pass


# --------------------------------------------------------------------- SMB --

class SmbClient:
    # libsmbclient entry types
    SHARE, DIR, FILE = 3, 7, 8

    def __init__(self, config, timeout):
        try:
            import smbc
        except ImportError as exc:
            raise NasError('服务器缺少 SMB 支持：请在本机执行 apt install python3-smbc') from exc
        self.smbc = smbc
        self.config = config
        username, password = config.get('username', ''), config.get('password', '')
        workgroup = config.get('workgroup') or 'WORKGROUP'
        self.context = smbc.Context(auth_fn=lambda *_: (workgroup, username, password))
        try:
            self.context.timeout = int(timeout * 1000)
        except (AttributeError, TypeError):
            pass
        host = config['host']
        port = int(config.get('port') or 445)
        self.base = f'smb://{host}' + (f':{port}' if port != 445 else '')
        self.share = config.get('share') or ''
        self.root = clean_path(config.get('path') or '/')

    def _url(self, path):
        full = posixpath.join(self.root, clean_path(path).lstrip('/'))
        if self.share:
            full = '/' + self.share + (full if full != '/' else '')
        return self.base + urllib.parse.quote(full.rstrip('/') or '')

    def _wrap(self, exc, what):
        # pysmbc raises smbc.PermissionError / smbc.NoEntryError / smbc.TimedOutError ...
        kind, text = type(exc).__name__, str(exc)
        code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
        network = {110: '连接超时', 111: '连接被拒绝（端口未开放）', 112: '主机不可达',
                   113: '主机不可达', 101: '网络不可达', 2: 'NAS 上找不到这个路径'}
        if code in network and code != 2:
            return NasError(f'无法连接 SMB 服务（{self.base[6:]}）：{network[code]}')
        if code == 2:
            return NasError('NAS 上找不到这个路径', status=404)
        if code == 13:
            return NasError(f'SMB {what}被拒绝：用户名、密码或共享权限不正确', status=401)
        if kind == 'PermissionError' or 'Permission denied' in text:
            return NasError(f'SMB {what}被拒绝：用户名、密码或共享权限不正确', status=401)
        if kind in ('NoEntryError', 'NotDirectoryError') or 'No such' in text:
            return NasError('NAS 上找不到这个路径', status=404)
        if kind == 'TimedOutError' or 'timed out' in text.lower():
            return NasError(f'SMB {what}超时：请检查 NAS 地址与端口')
        return NasError(f'SMB {what}失败：{text[:200]}')

    def list(self, path):
        path = clean_path(path)
        url = self._url(path)
        try:
            entries = self.context.opendir(url).getdents()
        except Exception as exc:  # smbc raises its own errno-style exceptions
            raise self._wrap(exc, '读取目录') from exc
        result = []
        browsing_shares = not self.share and path == '/' and self.root == '/'
        for item in entries:
            name = item.name
            if name in ('.', '..'):
                continue
            if browsing_shares:
                if item.smbc_type != self.SHARE or name.endswith('$'):
                    continue
                result.append(_entry(path, name, True))
                continue
            if item.smbc_type not in (self.DIR, self.FILE):
                continue
            size = mtime = 0
            if item.smbc_type == self.FILE:
                try:
                    info = self.context.stat(url.rstrip('/') + '/' + urllib.parse.quote(name))
                    size, mtime = info[6], info[8]
                except Exception:
                    pass
            result.append(_entry(path, name, item.smbc_type == self.DIR, size, mtime))
            if len(result) >= LIST_LIMIT:
                break
        return result

    def stat(self, path):
        try:
            info = self.context.stat(self._url(path))
        except Exception as exc:
            raise self._wrap(exc, '读取') from exc
        import stat as stat_module
        return {'dir': stat_module.S_ISDIR(info[0]), 'size': info[6], 'mtime': info[8]}

    def open(self, path):
        try:
            return _SmbReader(self.context.open(self._url(path), os.O_RDONLY))
        except Exception as exc:
            raise self._wrap(exc, '打开文件') from exc

    def open_at(self, path, offset):
        try:
            handle = self.context.open(self._url(path), os.O_RDONLY)
            handle.seek(int(offset))
            return _SmbReader(handle)
        except Exception as exc:
            raise self._wrap(exc, '打开文件') from exc

    def read_at(self, path, offset, size):
        try:
            handle = self.context.open(self._url(path), os.O_RDONLY)
            try:
                handle.seek(offset)
                return handle.read(size)
            finally:
                handle.close()
        except Exception as exc:
            raise self._wrap(exc, '读取文件') from exc

    # -- writing (only with "writable") --
    def mkdir(self, path):
        _require_writable(self.config)
        try:
            self.context.mkdir(self._url(path), 0o755)
        except Exception as exc:
            try:
                if self.stat(path)['dir']:
                    return
            except NasError:
                pass
            raise self._wrap(exc, '建文件夹') from exc

    def write(self, path, chunks, size):
        _require_writable(self.config)
        try:
            handle = self.context.open(self._url(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
        except Exception as exc:
            raise self._wrap(exc, '写入') from exc
        try:
            for chunk in chunks:
                handle.write(chunk)
        except NasError:
            raise
        except Exception as exc:
            if isinstance(exc, (_Canceled, ValueError)):
                raise
            raise self._wrap(exc, '写入') from exc
        finally:
            try:
                handle.close()
            except Exception:
                pass

    def rename(self, source, target):
        _require_writable(self.config)
        try:
            self.context.rename(self._url(source), self._url(target))
        except Exception as exc:
            raise self._wrap(exc, '改名') from exc

    def delete(self, path):
        _require_writable(self.config)
        try:
            self.context.unlink(self._url(path))
        except Exception as exc:
            raise self._wrap(exc, '删除') from exc

    def rmdir(self, path):
        _require_writable(self.config)
        try:
            self.context.rmdir(self._url(path))
        except Exception as exc:
            raise self._wrap(exc, '删除文件夹') from exc

    def close(self):
        self.context = None


class _SmbReader:
    def __init__(self, handle):
        self.handle = handle

    def read(self, size=CHUNK):
        return self.handle.read(size)

    def close(self):
        try:
            self.handle.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# --------------------------------------------------------------------- FTP --

class FtpClient:
    def __init__(self, config, timeout):
        self.config = config
        self.timeout = timeout
        self.root = clean_path(config.get('path') or '/')
        self.conn = None

    def _connect(self):
        if self.conn is not None:
            return self.conn
        cls = ftplib.FTP_TLS if self.config.get('tls') else ftplib.FTP
        kwargs = {'timeout': self.timeout, 'encoding': 'utf-8'}
        if cls is ftplib.FTP_TLS:
            context = ssl.create_default_context()
            if not self.config.get('verify_tls'):
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            kwargs['context'] = context
        conn = cls(**kwargs)
        try:
            conn.connect(self.config['host'], int(self.config.get('port') or 21))
            conn.login(self.config.get('username') or 'anonymous', self.config.get('password') or '')
            if cls is ftplib.FTP_TLS:
                conn.prot_p()
            conn.voidcmd('TYPE I')
        except ftplib.error_perm as exc:
            conn.close()
            raise NasError(f'FTP 登录失败：{str(exc)[4:120]}', status=401) from exc
        except (OSError, EOFError, ftplib.Error) as exc:
            conn.close()
            raise NasError(f'无法连接 FTP 服务（{self.config["host"]}）：{exc}') from exc
        self.conn = conn
        return conn

    def _full(self, path):
        return posixpath.join(self.root, clean_path(path).lstrip('/')) or '/'

    def list(self, path):
        path = clean_path(path)
        conn = self._connect()
        full = self._full(path)
        try:
            rows = [_entry(path, name, facts.get('type') == 'dir', facts.get('size'),
                           _ftp_time(facts.get('modify')))
                    for name, facts in listing(conn, full)
                    if facts.get('type') in ('dir', 'file')]
        except ftplib.Error as exc:
            raise NasError(f'FTP 读取目录失败：{str(exc)[:160]}',
                           status=404 if str(exc).startswith('550') else None) from exc
        return rows[:LIST_LIMIT]

    def stat(self, path):
        # The parent listing is authoritative: several servers answer SIZE on a
        # folder with "213 0", which would turn a folder into an empty file.
        path = clean_path(path)
        if path == '/':
            return {'dir': True, 'size': 0, 'mtime': 0}
        parent, name = posixpath.split(path)
        for item in self.list(parent):
            if item['name'] == name:
                return {'dir': item['dir'], 'size': item['size'], 'mtime': item['mtime']}
        raise NasError('NAS 上找不到这个路径', status=404)

    def read_at(self, path, offset, size):
        # One connection per read: ending a transfer early leaves the control
        # connection with replies that servers order differently.
        reader = FtpClient(self.config, self.timeout)
        conn = reader._connect()
        try:
            data = conn.transfercmd('RETR ' + self._full(path), rest=offset)
            try:
                chunks, got = [], 0
                while got < size:
                    piece = data.recv(min(CHUNK, size - got))
                    if not piece:
                        break
                    chunks.append(piece)
                    got += len(piece)
                return b''.join(chunks)
            finally:
                data.close()
        except ftplib.Error as exc:
            raise NasError(f'FTP 读取文件失败：{str(exc)[:160]}') from exc
        finally:
            reader.close()

    def open(self, path):
        return self.open_at(path, 0)

    # -- writing (only with "writable") --
    def _command(self, what, action, *args):
        _require_writable(self.config)
        conn = self._connect()
        try:
            return action(conn, *args)
        except ftplib.Error as exc:
            raise NasError(f'FTP {what}失败：{str(exc)[:160]}',
                           status=404 if str(exc).startswith('550') else None) from exc

    def mkdir(self, path):
        _require_writable(self.config)
        try:
            self._command('建文件夹', lambda conn, full: conn.mkd(full), self._full(path))
        except NasError:
            if not self.stat(path)['dir']:      # MKD fails for an existing folder too
                raise

    def write(self, path, chunks, size):
        def store(conn, full):
            conn.storbinary('STOR ' + full, _ChunkStream(chunks), blocksize=CHUNK)
        self._command('写入', store, self._full(path))

    def rename(self, source, target):
        self._command('改名', lambda conn, a, b: conn.rename(a, b), self._full(source), self._full(target))

    def delete(self, path):
        self._command('删除', lambda conn, full: conn.delete(full), self._full(path))

    def rmdir(self, path):
        self._command('删除文件夹', lambda conn, full: conn.rmd(full), self._full(path))

    def open_at(self, path, offset):
        # A dedicated connection: the listing connection stays usable.
        reader = FtpClient(self.config, self.timeout)
        conn = reader._connect()
        try:
            data = conn.transfercmd('RETR ' + self._full(path), rest=int(offset) or None)
        except ftplib.Error as exc:
            reader.close()
            raise NasError(f'FTP 打开文件失败：{str(exc)[:160]}') from exc
        return _FtpReader(reader, data)

    def close(self):
        if self.conn is not None:
            try:
                self.conn.quit()
            except (OSError, EOFError, ftplib.Error):
                self.conn.close()
            self.conn = None


def _ftp_time(value):
    try:
        stamp = datetime.datetime.strptime(str(value)[:14], '%Y%m%d%H%M%S')
    except ValueError:
        return 0
    return int(stamp.replace(tzinfo=datetime.timezone.utc).timestamp())


class _FtpReader:
    def __init__(self, client, data):
        self.client, self.data = client, data

    def read(self, size=CHUNK):
        return self.data.recv(size)

    def close(self):
        try:
            self.data.close()
            self.client.conn.voidresp()
        except (OSError, EOFError, ftplib.Error, AttributeError):
            pass
        self.client.close()


def walk(client, root):
    """Yield (path, is_dir, size) below root, folders before their content."""
    stack = [root]
    while stack:
        current = stack.pop()
        for item in client.list(current):
            yield item['path'], item['dir'], item['size']
            if item['dir']:
                stack.append(item['path'])


# --------------------------------------------------------------- transfers --

def atomic_json(path, document, mode=0o600):
    """Write JSON next to path, fsync, then rename over it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex[:8]}.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(document, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


TERMINAL = frozenset(('done', 'done_with_warnings', 'failed', 'canceled'))
INTERRUPTED = '服务重启时中断'


class TransferManager(MigrationJobs, UninstallJobs):
    """One NAS transfer at a time; the rest wait in order."""

    KEEP = 30
    # Progress is written to the state file at most this often; state changes at once.
    PERSIST_EVERY = 5.0
    # Kinds that /api/transfers/retry can queue again (extraction needs a fresh preflight).
    RETRYABLE = ('nas', 'package', 'backup', 'migrate')
    BACKUP_MODES = ('mirror', 'snapshot')
    # Largest cheat ZIP read from the NAS into memory.
    CHEAT_ZIP_LIMIT = 16 << 20

    # Waits before the 1st, 2nd, 3rd… automatic retry of one file (seconds).
    RETRY_DELAYS = (2, 5, 15)

    def __init__(self, store, files, client_factory=client_for, work_dir=None, runner=None,
                 engines=None, game_services=None, host=None, state_path=None, retries=3,
                 pkg_host=None):
        """work_dir: where unrar jobs keep their spec and status files; runner
        starts the worker unit (SystemdRunner by default); engines overrides
        engine detection (tests); state_path keeps the job list across restarts."""
        self.store = store
        self.files = files
        self.client_factory = client_factory
        self.work_dir = Path(work_dir) if work_dir else None
        self.runner = runner or SystemdRunner()
        self._engines = engines
        # Optional ps5upload adapter.  Keeping this None preserves the legacy
        # transfer-only behavior and keeps fake engines out of old tests.
        self.game_services = game_services
        self.host = host
        # pkg_host.PkgHost: serves a NAS PKG to the console for 'package' jobs.
        self.pkg_host = pkg_host
        self.lock = threading.RLock()
        self.jobs = collections.OrderedDict()
        self.queue = collections.deque()
        self.cancels = {}
        self.worker = None
        # Automatic retries per file after a connection error; 0 disables them.
        self.retries = max(0, int(retries))
        self.state_path = Path(state_path) if state_path else None
        self._persist_lock = threading.Lock()
        self._last_persist = 0.0
        self.adopted = {}           # job id -> unit handle of an unrar worker that outlived a restart
        self.listeners = []         # callables(job copy) run once when a job ends (notifications)
        self._load()

    # -- persistence --
    def _load(self):
        if not self.state_path:
            return
        try:
            document = json.loads(self.state_path.read_text(encoding='utf-8'))
            saved = document.get('jobs') if isinstance(document, dict) else None
            if not isinstance(saved, list):
                raise ValueError('transfers.json has no job list')
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            # A damaged file must not keep the service from starting.
            logging.warning('Ignoring unreadable transfer state %s: %s', self.state_path, exc)
            return
        changed = False
        for job in saved:
            if not (isinstance(job, dict) and isinstance(job.get('id'), str) and
                    isinstance(job.get('state'), str) and isinstance(job.get('kind'), str)):
                continue
            ident = job['id']
            if job['state'] not in TERMINAL:
                changed = True
                handle = self._reattach(job)
                if handle is not None:
                    self.adopted[ident] = handle
                    self.queue.append(ident)
                else:
                    job.update(state='failed', error=INTERRUPTED, interrupted=True,
                               finished=job.get('finished') or time.time(), speed=0, eta=None)
            self.jobs[ident] = job
            self.cancels[ident] = threading.Event()
        self._trim()
        if changed:
            self._persist(force=True)
        if self.queue:
            self.worker = threading.Thread(target=self._run, daemon=True, name='nas-transfer')
            self.worker.start()

    def _reattach(self, job):
        """An unrar worker runs in its own unit and may still be going: keep following it."""
        if job['kind'] != 'extract' or job.get('engine') != 'unrar' or self.work_dir is None:
            return None
        attach = getattr(self.runner, 'attach', None)
        if attach is None or not (self.work_dir / f'{job["id"]}.json').exists():
            return None
        try:
            handle = attach(f'ps5-extract-{job["id"]}')
            return handle if handle.alive() else None
        except Exception as exc:
            logging.warning('Cannot check extraction unit for %s: %s', job['id'], exc)
            return None

    def _persist(self, force=False):
        """Write the visible job fields (never NAS credentials) to the state file."""
        if not self.state_path:
            return
        with self._persist_lock:
            with self.lock:
                now = time.monotonic()
                if not force and now - self._last_persist < self.PERSIST_EVERY:
                    return
                self._last_persist = now
                jobs = [{key: value for key, value in job.items() if key != 'samples'}
                        for job in self.jobs.values()]
            try:
                atomic_json(self.state_path, {'schema': 1, 'jobs': jobs})
            except (OSError, TypeError, ValueError) as exc:
                logging.warning('Could not save transfer state: %s', exc)

    def retry(self, ident):
        """Queue a failed or canceled transfer again; existing files are skipped (file-level resume)."""
        with self.lock:
            job = self.jobs.get(ident)
            if job is None:
                raise NasError('没有这个传输任务', status=404)
            job = dict(job)
        if job['kind'] not in self.RETRYABLE:
            raise ValueError('解压任务不支持重试：请重新在 NAS 里选择压缩包，开始前会重新检查分卷与空间')
        if job['state'] not in ('failed', 'canceled'):
            raise ValueError('只能重试失败或已取消的任务')
        if job['kind'] == 'backup':
            # Same target folder: a retried snapshot completes the same snapshot.
            created = self.start_backup(job['nas_id'], job['paths'], job['dest'], job.get('mode') or 'mirror',
                                        host=job.get('host'), target=job.get('target'))
            with self.lock:
                self.jobs[created['id']]['retry_of'] = ident
            self._persist(force=True)
            return self.snapshot_one(created['id'])
        if job['kind'] == 'migrate':
            created = self.start_migrate(job['paths'][0], job['target'], register=job.get('auto_register', False),
                                         keep_source=job.get('keep_source', False), host=job.get('host'),
                                         title=job.get('title') or '', name=job.get('name') or '')
            with self.lock:
                self.jobs[created['id']]['retry_of'] = ident
            self._persist(force=True)
            return self.snapshot_one(created['id'])
        if job['kind'] == 'package':
            created = self.start_package(job['nas_id'], job['paths'][0], host=job.get('host'),
                                         title=job.get('title') or '')
            with self.lock:
                self.jobs[created['id']]['retry_of'] = ident
            self._persist(force=True)
            return self.snapshot_one(created['id'])
        created = self.start(job['nas_id'], job['paths'], job['dest'], 'skip',
                             auto_register=job.get('auto_register', False),
                             host=job.get('host'), title=job.get('title') or '')
        with self.lock:
            self.jobs[created['id']]['retry_of'] = ident
        self._persist(force=True)
        return self.snapshot_one(created['id'])

    # -- public --
    def start(self, nas_id, paths, dest, overwrite='skip', auto_register=False,
              host=None, title=''):
        config = self.store.get(nas_id)
        if not isinstance(paths, list) or not paths or len(paths) > 200:
            raise ValueError('请选择 1–200 个要传输的文件或文件夹')
        paths = [clean_path(path) for path in paths]
        if overwrite not in ('skip', 'overwrite'):
            raise ValueError('同名文件处理方式只能是跳过或覆盖')
        if not isinstance(dest, str) or not dest.startswith('/'):
            raise ValueError('请选择 PS5 上的目标文件夹')
        self.files.list(dest)      # validates the whitelist and that the folder exists
        ident = uuid.uuid4().hex[:10]
        label = paths[0].rsplit('/', 1)[-1] or config['name']
        job = {'id': ident, 'kind': 'nas', 'source': config['name'], 'nas_id': nas_id,
               'protocol': config['protocol'], 'name': label + (f' 等 {len(paths)} 项' if len(paths) > 1 else ''),
               'paths': paths, 'dest': dest, 'overwrite': overwrite, 'state': 'queued',
               'files_total': 0, 'files_done': 0, 'files_skipped': 0, 'files_failed': 0,
               'bytes_total': 0, 'bytes_done': 0, 'current': '', 'current_size': 0,
               'current_done': 0, 'created': time.time(), 'started': None, 'finished': None,
               'elapsed': 0, 'speed': 0, 'eta': None, 'percent': 0, 'error': '', 'errors': [],
                'auto_register': bool(auto_register), 'registration_error': '',
                'registration': None, 'host': host or self.host, 'title': str(title or ''),
                'retries': 0, 'resumed': 0}
        self._enqueue(job)
        return self.snapshot_one(ident)

    def start_package(self, nas_id, path, host=None, title=''):
        """Queue a PKG install: the engine reads the file from the NAS through pkg_host."""
        if self.game_services is None or self.pkg_host is None:
            raise ValueError('PKG 安装不可用：需要本机 ps5upload 引擎')
        if not getattr(self.pkg_host, 'enabled', True):
            raise ValueError('PKG 安装已关闭（[ps5] pkg_port = 0）')
        config = self.store.get(nas_id)
        path = clean_path(path)
        if not path.lower().endswith('.pkg'):
            raise ValueError('请选择一个 .pkg 文件')
        host = host or self.host
        if not host:
            raise ValueError('请先选择 PS5 主机')
        client = self.client_factory(config)
        try:
            info = client.stat(path)
        finally:
            try:
                client.close()
            except Exception:
                pass
        if info.get('dir') or not info.get('size'):
            raise ValueError('所选 PKG 不存在或是空文件')
        ident = uuid.uuid4().hex[:10]
        name = path.rsplit('/', 1)[-1]
        job = {'id': ident, 'kind': 'package', 'source': config['name'], 'nas_id': nas_id,
               'protocol': config['protocol'], 'name': name, 'paths': [path], 'dest': '',
               'overwrite': 'skip', 'state': 'queued', 'phase': 'prepare',
               'files_total': 1, 'files_done': 0, 'files_skipped': 0, 'files_failed': 0,
               'bytes_total': int(info['size']), 'bytes_done': 0, 'current': name,
               'current_size': int(info['size']), 'current_done': 0, 'mtime': info.get('mtime') or 0,
               'created': time.time(), 'started': None, 'finished': None, 'elapsed': 0, 'speed': 0,
               'eta': None, 'percent': 0, 'error': '', 'error_code': '', 'errors': [], 'warning': '',
               'verdict': '', 'engine_job': '', 'host': host, 'title': str(title or ''),
               'retries': 0, 'resumed': 0}
        self._enqueue(job)
        return self.snapshot_one(ident)

    def _execute_package(self, job, cancel):
        self._set(job, state='running', started=time.time(), phase='prepare')
        token, url = self.pkg_host.publish(job['id'], job['nas_id'], job['paths'][0], job['bytes_total'],
                                           job.get('mtime') or 0, {job['host']})
        samples = collections.deque(maxlen=80)
        seen = [0]

        def progress(status):
            phase = str(status.get('phase') or '').lower()
            metrics = status.get('metrics') if isinstance(status.get('metrics'), dict) else {}
            served = metrics.get('served_bytes')
            if not isinstance(served, (int, float)) or served <= 0:
                served = self.pkg_host.served(token)
            served = int(min(served, job['bytes_total']))
            if served > seen[0]:
                self._progress(job, served - seen[0], served, samples)
                seen[0] = served
            self._set(job, phase=PACKAGE_PHASES.get(phase, job.get('phase') or 'prepare'),
                      engine_job=str(status.get('job') or job.get('engine_job') or ''))

        try:
            result = self.game_services.package_install(job['host'], {'url': url},
                                                        timeout=install_timeout(job['bytes_total']),
                                                        progress=progress, cancel=cancel)
        finally:
            self.pkg_host.revoke(token)
        state = result.get('state')
        self._set(job, engine_job=str(result.get('job') or job.get('engine_job') or ''),
                  verdict=str(result.get('verdict') or ''))
        if state == 'canceled' or cancel.is_set():
            raise _Canceled()
        if state == 'conflict':
            self._set(job, error_code='busy')
            raise NasError('ps5upload 引擎正忙（另一个安装任务在进行），稍后点「重试」')
        if state == 'failed':
            self._set(job, error_code=str(result.get('error_code') or 'failed'))
            reason = result.get('reason') or result.get('error') or '安装失败'
            raise NasError(f'PKG 安装失败：{str(reason)[:200]}')
        done = {'phase': 'done', 'percent': 100, 'files_done': 1, 'current_done': job['bytes_total'],
                'bytes_done': job['bytes_total']}
        if state == 'warning':
            self._set(job, state='done_with_warnings',
                      warning=result.get('warning') or '安装完成但主机报告 may_not_launch，游戏可能无法启动', **done)
        else:
            self._set(job, state='done', **done)

    def start_backup(self, nas_id, paths, dest, mode='mirror', host=None, target=None):
        """Queue a PS5 -> NAS backup of whole folders.

        mirror: always into dest, files already there with the same size are skipped;
        snapshot: into dest/<host>/<YYYYMMDD-HHMMSS>/ (or the given target on retry).
        """
        config = self.store.get(nas_id)
        _require_writable(config)
        if mode not in self.BACKUP_MODES:
            raise ValueError('备份方式只能是镜像或快照')
        if not isinstance(paths, list) or not 0 < len(paths) <= 20:
            raise ValueError('请选择 1–20 个要备份的 PS5 文件夹')
        sources = []
        for path in paths:
            if not isinstance(path, str) or not path.startswith('/') or posixpath.normpath(path) != path.rstrip('/'):
                raise ValueError('PS5 路径必须是规范的绝对路径')
            path = path.rstrip('/') or '/'
            if path == '/' or path in sources:
                raise ValueError('不能备份根目录或重复选择同一个文件夹')
            sources.append(path)
        dest = clean_path(dest or '/')
        host = host or self.host or getattr(self.files, 'host', '') or 'ps5'
        if target is None:
            target = dest
            if mode == 'snapshot':
                stamp = time.strftime('%Y%m%d-%H%M%S')
                target = posixpath.join(dest, re.sub(r'[^A-Za-z0-9_.-]', '_', str(host)), stamp)
        target = clean_path(target)
        ident = uuid.uuid4().hex[:10]
        label = sources[0].rsplit('/', 1)[-1] or sources[0]
        job = {'id': ident, 'kind': 'backup', 'source': f'PS5 {host}', 'nas_id': nas_id,
               'protocol': config['protocol'], 'nas_name': config['name'],
               'name': label + (f' 等 {len(sources)} 项' if len(sources) > 1 else ''),
               'paths': sources, 'dest': dest, 'target': target, 'mode': mode, 'state': 'queued',
               'files_total': 0, 'files_done': 0, 'files_skipped': 0, 'files_failed': 0,
               'bytes_total': 0, 'bytes_done': 0, 'current': '', 'current_size': 0, 'current_done': 0,
               'created': time.time(), 'started': None, 'finished': None, 'elapsed': 0, 'speed': 0,
               'eta': None, 'percent': 0, 'error': '', 'errors': [], 'host': host, 'retries': 0, 'resumed': 0}
        self._enqueue(job)
        return self.snapshot_one(ident)

    def _ps5_tree(self, root, cancel):
        """(folders, files) below one PS5 folder; files are (path, relative, size)."""
        folders, files, stack = [''], [], [(root, '')]
        while stack:
            if cancel.is_set():
                raise _Canceled()
            current, relative = stack.pop()
            for entry in self.files.list(current).get('entries', []):
                name = entry.get('name') or ''
                if not name or name in ('.', '..') or '/' in name:
                    continue
                child = posixpath.join(relative, name)
                if entry.get('dir'):
                    folders.append(child)
                    stack.append((entry.get('path') or posixpath.join(current, name), child))
                else:
                    files.append((entry.get('path') or posixpath.join(current, name), child,
                                  int(entry.get('size') or 0)))
        return folders, files

    def _execute_backup(self, job, cancel):
        config = self.store.get(job['nas_id'])
        _require_writable(config)
        client = self.client_factory(config)
        self._set(job, state='scanning', started=time.time())
        try:
            plan = []           # (source root name, folders, files)
            for source in job['paths']:
                folders, files = self._ps5_tree(source, cancel)
                plan.append((source.rsplit('/', 1)[-1] or 'root', folders, files))
            every = [item for _, _, files in plan for item in files]
            self._set(job, state='running', files_total=len(every), bytes_total=sum(item[2] for item in every))
            self._nas_mkdirs(client, job['target'])
            samples = collections.deque(maxlen=80)
            for top, folders, files in plan:
                base = posixpath.join(job['target'], top)
                existing = {}
                for folder in folders:
                    if cancel.is_set():
                        raise _Canceled()
                    folder_path = posixpath.join(base, folder) if folder else base
                    client.mkdir(folder_path)
                    try:
                        existing[folder_path] = {entry['name']: entry for entry in client.list(folder_path)}
                    except NasError:
                        existing[folder_path] = {}
                for source, relative, size in files:
                    if cancel.is_set():
                        raise _Canceled()
                    target = posixpath.join(base, relative)
                    known = existing.get(posixpath.dirname(target), {}).get(posixpath.basename(target))
                    if known and not known.get('dir') and int(known.get('size') or 0) == size:
                        self._set(job, files_skipped=job['files_skipped'] + 1, bytes_total=job['bytes_total'] - size)
                        continue
                    self._backup_file(job, client, source, target, size, cancel, samples)
            if job['files_failed']:
                # An incomplete backup is a failure: 「重试」 finishes it, unchanged files are skipped.
                raise NasError(f'{job["files_failed"]} 个文件没有备份成功，可以点「重试」补齐')
            self._set(job, state='done', percent=100, current='', current_done=0, current_size=0)
        finally:
            try:
                client.close()
            except Exception:
                pass

    @staticmethod
    def _nas_mkdirs(client, path):
        current = ''
        for part in [item for item in path.split('/') if item]:
            current += '/' + part
            client.mkdir(current)

    def _backup_file(self, job, client, source, target, size, cancel, samples):
        """Stream one PS5 file into <target>.part on the NAS, retrying connection errors."""
        self._set(job, current=source, current_size=size, current_done=0)
        failures = 0
        while True:
            sent = [0]
            response = None
            try:
                response = self.files.stream(source)

                def chunks():
                    while True:
                        if cancel.is_set():
                            raise _Canceled()
                        data = response.read(CHUNK)
                        if not data:
                            if sent[0] < size:
                                raise NasError(f'PS5 数据提前结束（{sent[0]} / {size} 字节）')
                            return
                        sent[0] += len(data)
                        self._progress(job, len(data), sent[0], samples)
                        yield data

                put_file(client, target, chunks(), size)
                with self.lock:
                    job['files_done'] += 1
                return
            except _Canceled:
                _quiet(client.delete, target + PART)
                raise
            except (FileServiceError, ValueError, OSError, NasError) as exc:
                with self.lock:
                    job['bytes_done'] -= sent[0]
                _quiet(client.delete, target + PART)
                if self._retryable(exc) and failures < self.retries:
                    failures += 1
                    with self.lock:
                        job['retries'] = job.get('retries', 0) + 1
                    logging.info('backup %s: %s failed (%s), retry %d/%d', job['id'], source,
                                 str(exc)[:160], failures, self.retries)
                    if cancel.wait(self.RETRY_DELAYS[min(failures, len(self.RETRY_DELAYS)) - 1]):
                        raise _Canceled() from None
                    continue
                with self.lock:
                    job['bytes_total'] -= size
                    job['files_failed'] += 1
                    if len(job['errors']) < 20:
                        job['errors'].append(f'{source}：{str(exc)[:160]}')
                    job['error'] = f'{job["files_failed"]} 个文件失败，最近：{source}'
                return
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass

    def engines(self):
        if self._engines is not None:
            return self._engines
        return {'libarchive': archive.available(),
                'unrar': self.work_dir is not None and unrar_engine_available(),
                'memory': _worker_memory()}

    def inspect_archive(self, nas_id, path):
        """Volume set, header checks and engine choice for one NAS archive, for the page."""
        engines = self.engines()
        config = self.store.get(nas_id)
        path = clean_path(path)
        folder, name = posixpath.split(path)
        client = self.client_factory(config)
        try:
            try:
                base, volumes = archive.volume_set(client.list(folder or '/'), name)
                info = archive.inspect(client, volumes, engines)
            except archive.ArchiveError as exc:
                raise ValueError(str(exc)) from None
        finally:
            try:
                client.close()
            except Exception:
                pass
        top = info['first_entry'].split('/')[0] if '/' in info['first_entry'] else ''
        return {'base': base, 'format': info['format'], 'parts': len(volumes),
                'volumes': [{'name': item['name'], 'path': item['path'], 'size': item['size']}
                            for item in volumes],
                'bytes': sum(item['size'] for item in volumes),
                'title': archive.title_guess(top) or archive.title_guess(base),
                'top': top, 'dictionary': info['dictionary'], 'source': config['name'],
                # Known only when every header could be read up front (Range / seek / REST).
                'files': info['files'], 'unpacked': info['unpacked'], 'checked': info['complete'],
                'solid': info['solid'], 'engine': info['engine'], 'folder': folder or '/',
                'memory': archive.unrar_memory(info['dictionary']) if info['engine'] == 'unrar' else 0}

    def start_extract(self, nas_id, path, dest, mode='install', auto_register=False,
                      host=None, title='', cheats=None):
        if mode not in ('install', 'merge'):
            raise ValueError('安装方式只能是新建游戏文件夹或合并到已有文件夹')
        cheats = [clean_path(item) for item in (cheats or [])]
        if len(cheats) > 10 or any(not item.lower().endswith('.zip') for item in cheats):
            raise ValueError('金手指只能是 1–10 个 ZIP 文件')
        if not isinstance(dest, str) or not dest.startswith('/'):
            raise ValueError('请选择 PS5 上的目标文件夹')
        dest = dest.rstrip('/') or '/'
        listed = self.files.list(dest)          # validates the whitelist and that it exists
        volume = VOLUME_PATTERN.match(dest)
        if mode == 'merge' and volume and dest == volume.group(0).rstrip('/'):
            raise ValueError('合并时请选中已有的游戏文件夹，而不是存储卷根目录')
        info = self.inspect_archive(nas_id, path)
        names = {entry['name'] for entry in listed.get('entries', [])}
        if mode == 'install' and info['title'] and info['title'] in names:
            raise ValueError(f'{dest} 下已经有 {info["title"]}。如果这是补丁或 DLC，'
                             f'请选择“合并到已有游戏文件夹”，目标选 {info["title"]} 文件夹')
        free = self._free_space(dest)
        need = info['unpacked'] or info['bytes']
        if free is not None and free < need:
            what = '解压后' if info['unpacked'] else '压缩包'
            raise ValueError(f'目标存储剩余 {free / 2 ** 30:.1f} GB，放不下{what} '
                             f'{need / 2 ** 30:.1f} GB 的内容')
        ident = uuid.uuid4().hex[:10]
        # Hidden folder the archive is unpacked into before the final rename.
        staging_parent = dest if mode == 'install' else posixpath.dirname(dest.rstrip('/'))
        job = {'id': ident, 'kind': 'extract', 'source': info['source'], 'nas_id': nas_id,
               'staging': posixpath.join(staging_parent, f'.extract-{ident}'),
               'protocol': self.store.get(nas_id)['protocol'],
               'name': info['base'] + (f'（{info["parts"]} 个分卷）' if info['parts'] > 1 else ''),
               'paths': [item['path'] for item in info['volumes']], 'volumes': info['volumes'],
               'base': info['base'], 'dest': dest, 'mode': mode, 'overwrite': 'overwrite',
               'state': 'queued', 'files_total': info['files'] or 0, 'files_done': 0, 'files_skipped': 0,
               # unrar reports unpacked bytes, libarchive archive bytes read.
               'files_failed': 0, 'bytes_total': (info['unpacked'] or 0) if info['engine'] == 'unrar'
               else info['bytes'], 'bytes_done': 0, 'current': '',
               'current_size': 0, 'current_done': 0, 'created': time.time(), 'started': None,
               'finished': None, 'elapsed': 0, 'speed': 0, 'eta': None, 'percent': 0,
               'error': '', 'errors': [], 'result_path': '', 'title': title or info['title'], 'note': '',
               'engine': info['engine'], 'memory': info['memory'], 'folder': info['folder'],
                'auto_register': bool(auto_register), 'registration_error': '',
                'registration': None, 'host': host or self.host,
                'cheats': cheats, 'cheat_result': None, 'cheat_error': ''}
        self._enqueue(job)
        return self.snapshot_one(ident)

    def _free_space(self, dest):
        try:
            volumes = self.files.volumes().get('volumes', [])
        except (FileServiceError, AttributeError):
            return None
        best = None
        for volume in volumes:
            root = (volume.get('path') or '').rstrip('/')
            if root and (dest == root or dest.startswith(root + '/')):
                if best is None or len(root) > len(best[0]):
                    best = (root, volume.get('free'))
        return best[1] if best and isinstance(best[1], (int, float)) else None

    def _enqueue(self, job):
        ident = job['id']
        with self.lock:
            self.jobs[ident] = job
            self.cancels[ident] = threading.Event()
            self.queue.append(ident)
            self._trim()
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._run, daemon=True, name='nas-transfer')
                self.worker.start()
        self._persist(force=True)

    def snapshot_one(self, ident):
        with self.lock:
            job = self.jobs.get(ident)
            if job is None:
                raise NasError('没有这个传输任务', status=404)
            result = {key: value for key, value in job.items() if key != 'samples'}
            if job['state'] == 'running' and job['started']:
                result['elapsed'] = round(time.time() - job['started'], 1)
            return result

    def snapshot(self):
        with self.lock:
            return [self.snapshot_one(ident) for ident in reversed(self.jobs)]

    def cancel(self, ident):
        with self.lock:
            job = self.jobs.get(ident)
            if job is None:
                raise NasError('没有这个传输任务', status=404)
            self.cancels[ident].set()
            if job['state'] == 'queued':
                job['state'] = 'canceled'
                job['finished'] = time.time()
                try:
                    self.queue.remove(ident)
                except ValueError:
                    pass
        self._persist(force=True)
        return {'ok': True}

    def clear(self):
        with self.lock:
            for ident in [key for key, job in self.jobs.items()
                          if job['state'] in TERMINAL]:
                self.jobs.pop(ident, None)
                self.cancels.pop(ident, None)
        self._persist(force=True)
        return {'ok': True}

    def _trim(self):
        finished = [key for key, job in self.jobs.items()
                    if job['state'] in TERMINAL]
        while len(self.jobs) > self.KEEP and finished:
            key = finished.pop(0)
            self.jobs.pop(key, None)
            self.cancels.pop(key, None)

    # -- worker --
    def _run(self):
        _lower_priority()
        while True:
            with self.lock:
                if not self.queue:
                    self.worker = None
                    return
                ident = self.queue.popleft()
                job = self.jobs.get(ident)
                adopted = self.adopted.pop(ident, None)
            if job is None or (job['state'] != 'queued' and adopted is None):
                continue
            try:
                if adopted is not None:
                    self._follow_unrar(job, self.cancels[ident], adopted,
                                       self.work_dir / f'{ident}.json', self.work_dir / f'{ident}.status.json')
                elif job['kind'] == 'extract' and job.get('engine') == 'unrar':
                    self._execute_unrar(job, self.cancels[ident])
                elif job['kind'] == 'extract':
                    self._execute_extract(job, self.cancels[ident])
                elif job['kind'] == 'package':
                    self._execute_package(job, self.cancels[ident])
                elif job['kind'] == 'backup':
                    self._execute_backup(job, self.cancels[ident])
                elif job['kind'] == 'migrate':
                    self._execute_migrate(job, self.cancels[ident])
                elif job['kind'] == 'uninstall':
                    self._execute_uninstall(job, self.cancels[ident])
                else:
                    self._execute(job, self.cancels[ident])
            except _Canceled:
                self._set(job, state='canceled')
            except (NasError, FileServiceError, ValueError, OSError, archive.ArchiveError) as exc:
                self._set(job, state='failed', error=str(exc)[:300])
            except Exception as exc:  # keep the queue alive whatever happens
                logging.exception('NAS transfer failed')
                self._set(job, state='failed', error=f'内部错误：{exc}'[:300])
            finally:
                self._set(job, finished=time.time(), speed=0, eta=None,
                          elapsed=round(time.time() - (job['started'] or time.time()), 1))
                self._announce(job)

    def _announce(self, job):
        with self.lock:
            snapshot = dict(job)
        for listener in list(self.listeners):
            try:
                listener(snapshot)
            except Exception:
                logging.exception('transfer listener failed')

    def _set(self, job, **fields):
        with self.lock:
            job.update(fields)
        self._persist(force='state' in fields or 'finished' in fields)

    def _execute(self, job, cancel):
        config = self.store.get(job['nas_id'])
        client = self.client_factory(config)
        self._set(job, state='scanning', started=time.time())
        try:
            plan = []       # (source, ps5 directory, name, size) or (None, ps5 dir to create)
            for source in job['paths']:
                if cancel.is_set():
                    raise _Canceled()
                info = client.stat(source)
                name = source.rstrip('/').rsplit('/', 1)[-1]
                if not info['dir']:
                    plan.append((source, job['dest'], name, info['size']))
                    continue
                top = posixpath.join(job['dest'], name)
                plan.append((None, top, '', 0))
                prefix = source.rstrip('/')
                for path, is_dir, size in walk(client, source):
                    if cancel.is_set():
                        raise _Canceled()
                    relative = path[len(prefix):].lstrip('/')
                    target = posixpath.join(top, relative)
                    if is_dir:
                        plan.append((None, target, '', 0))
                    else:
                        plan.append((path, posixpath.dirname(target), posixpath.basename(target), size))
            files = [item for item in plan if item[0] is not None]
            self._set(job, state='running', files_total=len(files),
                      bytes_total=sum(item[3] for item in files))
            samples = collections.deque(maxlen=80)
            existing = {}
            for source, directory, name, size in plan:
                if cancel.is_set():
                    raise _Canceled()
                if source is None:
                    # The folder may already exist on the PS5; its listing is
                    # read lazily before the first file goes into it.
                    self._mkdir(directory)
                    continue
                if directory not in existing:
                    try:
                        existing[directory] = {entry['name'] for entry in
                                               self.files.list(directory)['entries']}
                    except FileServiceError:
                        existing[directory] = set()
                if name in existing[directory] and job['overwrite'] == 'skip':
                    self._set(job, files_skipped=job['files_skipped'] + 1,
                              bytes_total=job['bytes_total'] - size)
                    continue
                self._copy_file(job, client, source, directory, name, size, cancel, samples)
                existing[directory].add(name)
            if job.get('auto_register') and self.game_services and job.get('title'):
                result = {'path': posixpath.join(job['dest'], job['paths'][0].rstrip('/').rsplit('/', 1)[-1]),
                          'title': job['title'], 'name': job['title']}
                self._finish_registration(job, {'game': result, **result})
            self._set(job, state='done_with_warnings' if job.get('state') == 'done_with_warnings' else 'done', percent=100, current='', current_done=0, current_size=0)
        finally:
            try:
                client.close()
            except Exception:
                pass

    @staticmethod
    def _final_state(job):
        # Registration problems downgrade a finished job; anything else is plain done.
        return 'done_with_warnings' if job.get('state') == 'done_with_warnings' else 'done'

    def _finish_registration(self, job, result):
        """Register a successfully placed runnable game, without hiding transfer success."""
        if not job.get('auto_register') or not self.game_services:
            return
        game = result.get('game')
        if not game:
            title = job.get('title') or result.get('title')
            if not title:
                return
            game = {'title': title, 'path': result.get('path'), 'name': title}
        try:
            registration = self.game_services.register(job.get('host') or self.files.host, game)
            self._set(job, registration=registration)
        except Exception as exc:
            self._set(job, state='done_with_warnings', registration_error=str(exc)[:300])

    def read_cheat_zip(self, client, path):
        """Bytes of one cheat ZIP on the NAS, refusing anything over CHEAT_ZIP_LIMIT."""
        path = clean_path(path)
        if not path.lower().endswith('.zip'):
            raise ValueError('金手指必须是 ZIP 文件')
        info = client.stat(path)
        if info.get('dir'):
            raise ValueError('所选金手指不是文件')
        if int(info.get('size') or 0) > self.CHEAT_ZIP_LIMIT:
            raise ValueError('金手指 ZIP 超过 16 MiB 上限')
        reader = client.open(path)
        try:
            data = reader.read(self.CHEAT_ZIP_LIMIT + 1)
            while len(data) <= self.CHEAT_ZIP_LIMIT:
                more = reader.read(self.CHEAT_ZIP_LIMIT + 1 - len(data))
                if not more:
                    break
                data += more
        finally:
            reader.close()
        if len(data) > self.CHEAT_ZIP_LIMIT:
            raise ValueError('金手指 ZIP 超过 16 MiB 上限')
        return data

    def import_cheats(self, client, paths, title=None, version=None):
        """Read cheat ZIPs from the NAS and write them into etaHEN's folders."""
        title = title if title and TITLE.fullmatch(str(title)) else None
        written, warnings = [], []
        for path in paths:
            parsed = parse_cheat_zip(self.read_cheat_zip(client, path), title=title, version=version)
            result = write_cheats(self.files, parsed)
            written += result['written']
            warnings += result['warnings']
        if not written:
            warnings.append('没有写入任何金手指文件')
        return {'written': written, 'warnings': warnings,
                'note': '需要在 etaHEN 里重新加载金手指（或重启 etaHEN）后才会生效' if written else ''}

    def _import_cheats(self, job, client, title):
        """Optional last step of an extraction; problems only add a warning."""
        if not job.get('cheats'):
            return
        try:
            result = self.import_cheats(client, job['cheats'], title=title)
            self._set(job, cheat_result=result)
            if not result['written']:
                raise ValueError('；'.join(result['warnings'][-3:]) or '没有可导入的金手指')
        except Exception as exc:
            self._set(job, state='done_with_warnings', cheat_error=f'金手指导入失败：{str(exc)[:200]}')

    def _execute_extract(self, job, cancel):
        config = self.store.get(job['nas_id'])
        client = self.client_factory(config)
        self._set(job, state='running', started=time.time())
        samples = collections.deque(maxlen=80)
        manager = self

        class Hooks:
            @staticmethod
            def check():
                if cancel.is_set():
                    raise _Canceled()

            @staticmethod
            def read(count):
                manager._progress(job, count, job['current_done'], samples)

            @staticmethod
            def current(name, size, done):
                manager._set(job, current=name, current_size=size, current_done=done)

            @staticmethod
            def written():
                with manager.lock:
                    job['files_done'] += 1
                    job['files_total'] = max(job['files_total'], job['files_done'])

        try:
            installer = archive.Installer(self.files, client, job['volumes'], job['dest'],
                                          job['mode'], job['id'], Hooks)
            result = installer.run(job['base'])
            self._finish_registration(job, result)
            self._import_cheats(job, client, result.get('title') or job['title'])
            self._set(job, state=self._final_state(job), percent=100, current='', current_done=0, current_size=0,
                      result_path=result['path'], title=result['title'] or job['title'],
                      note=result['note'], game=result.get('game'), packages=result.get('packages', []),
                      sidecars=result.get('sidecars', []))
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _execute_unrar(self, job, cancel):
        """Run the job in its own systemd unit and mirror its status file."""
        self.work_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        ident = job['id']
        spec_path = self.work_dir / f'{ident}.json'
        status_path = self.work_dir / f'{ident}.status.json'
        files = self.files
        spec = {'id': ident, 'nas_id': job['nas_id'], 'nas_store': str(self.store.path),
                'nas_key': str(self.store.box.key_path) if self.store.box.key_path else None,
                'folder': job['folder'], 'volumes': job['volumes'], 'base': job['base'],
                'dest': job['dest'], 'mode': job['mode'], 'status': str(status_path),
                'mount': f'/run/ps5-extract/{ident}',
                'ps5': {'subnet': str(files.subnet), 'host': files.host, 'port': files.port,
                        'ftp_port': files.ftp_port, 'space_port': files.space_port}}
        descriptor = os.open(spec_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(spec, stream, ensure_ascii=False)
        self._set(job, state='scanning', started=time.time())
        handle = self.runner.start(spec_path, f'ps5-extract-{ident}', job['memory'])
        self._follow_unrar(job, cancel, handle, spec_path, status_path)

    def _follow_unrar(self, job, cancel, handle, spec_path, status_path):
        """Mirror a running unrar unit's status file until it finishes."""
        ident = job['id']
        samples = collections.deque(maxlen=80)
        # After a restart the job already counts the bytes reported before it.
        seen = int(job.get('bytes_done') or 0)
        stopping = False
        status = None
        try:
            while True:
                status = _read_json(status_path)
                if status:
                    if status.get('state') == 'running' and job['state'] == 'scanning':
                        self._set(job, state='running', bytes_total=status.get('bytes_total') or job['bytes_total'],
                                  files_total=status.get('files_total') or job['files_total'])
                    done = int(status.get('bytes_done') or 0)
                    if done > seen:
                        self._progress(job, done - seen, int(status.get('current_done') or 0), samples)
                        seen = done
                    self._set(job, current=status.get('current') or '',
                              current_size=int(status.get('current_size') or 0),
                              files_done=int(status.get('files_done') or 0))
                    if status.get('final'):
                        break
                if cancel.is_set() and not stopping:
                    handle.stop()
                    stopping = True
                if not handle.alive():
                    status = _read_json(status_path)
                    if not status or not status.get('final'):
                        self._discard_staging(job)
                        raise archive.ArchiveError(
                            f'解压进程意外退出（可能内存不足），详见 journalctl -u ps5-extract-{ident}')
                    break
                time.sleep(0.5)
        finally:
            for path in (spec_path, status_path):
                try:
                    path.unlink()
                except OSError:
                    pass
        if status['state'] == 'canceled' or (stopping and status['state'] != 'done'):
            raise _Canceled()
        if status['state'] != 'done':
            raise archive.ArchiveError(status.get('error') or '解压失败')
        result = {'path': status.get('result_path') or '', 'title': status.get('title') or job['title'],
                  'note': status.get('note') or '', 'game': status.get('game'),
                  'packages': status.get('packages') or [], 'sidecars': status.get('sidecars') or []}
        self._finish_registration(job, result)
        if job.get('cheats'):
            client = self.client_factory(self.store.get(job['nas_id']))
            try:
                self._import_cheats(job, client, result['title'])
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        self._set(job, state=self._final_state(job), percent=100, current='', current_done=0, current_size=0,
                  result_path=result['path'], title=result['title'], note=result['note'],
                  game=result['game'], packages=result['packages'], sidecars=result['sidecars'],
                  bytes_done=job['bytes_total'])

    def _discard_staging(self, job):
        parent = job['dest'] if job['mode'] == 'install' else posixpath.dirname(job['dest'].rstrip('/'))
        try:
            self.files.delete(posixpath.join(parent, f'.extract-{job["id"]}'))
        except Exception:
            pass

    def _mkdir(self, directory):
        try:
            self.files.mkdir(directory)
        except FileServiceError:
            pass        # usually "already exists"; a real problem surfaces on upload

    @staticmethod
    def _retryable(exc):
        """Connection trouble is worth another try; bad input, missing files and conflicts are not."""
        if isinstance(exc, ValueError):
            return False
        if isinstance(exc, FileServiceError):
            return exc.status is None
        if isinstance(exc, NasError):
            return exc.status not in (401, 403, 404)
        return isinstance(exc, OSError)

    def _ps5_size(self, directory, name):
        sizer = getattr(self.files, 'file_size', None)
        if sizer is None:
            return None
        try:
            return sizer(posixpath.join(directory, name))
        except (FileServiceError, ValueError, OSError):
            return None

    def _resume_offset(self, client, directory, name, size):
        """Bytes of a partial copy on the PS5 that both ends can continue from, else 0."""
        supports = getattr(self.files, 'supports_resume', None)
        if not hasattr(client, 'open_at') or supports is None:
            return 0
        try:
            if not supports():
                return 0
        except (FileServiceError, OSError):
            return 0
        partial = self._ps5_size(directory, name)
        return partial if isinstance(partial, int) and 0 < partial < size else 0

    def _copy_file(self, job, client, source, directory, name, size, cancel, samples):
        """Copy one file, retrying connection errors and resuming a partial copy when possible."""
        self._set(job, current=source, current_size=size, current_done=0)
        offset = 0              # bytes already on the PS5 that this attempt continues from
        failures = 0
        verified = False        # a resumed copy came out the wrong size once already
        while True:
            sent = [0]
            reader = None
            try:
                if offset:
                    reader = client.open_at(source, offset)
                    if reader is None:          # the NAS ignored the range: start over
                        with self.lock:
                            job['bytes_done'] -= offset
                        offset = 0
                if reader is None:
                    reader = client.open(source)
                start = offset

                def chunks():
                    while True:
                        if cancel.is_set():
                            raise _Canceled()
                        data = reader.read(CHUNK)
                        if not data:
                            if start + sent[0] < size:
                                # A dropped connection can look like a short file.
                                raise NasError(f'NAS 数据提前结束（{start + sent[0]} / {size} 字节）')
                            return
                        sent[0] += len(data)
                        self._progress(job, len(data), start + sent[0], samples)
                        yield data

                if offset:
                    self.files.upload(directory, name, chunks(), size, overwrite=True, offset=offset)
                else:
                    self.files.upload(directory, name, chunks(), size, overwrite=True)
            except _Canceled:
                self._discard(directory, name)
                raise
            except (FileServiceError, ValueError, OSError, NasError) as exc:
                with self.lock:
                    job['bytes_done'] -= sent[0] + offset
                if self._retryable(exc) and failures < self.retries:
                    failures += 1
                    with self.lock:
                        job['retries'] = job.get('retries', 0) + 1
                    logging.info('transfer %s: %s failed (%s), retry %d/%d', job['id'], source,
                                 str(exc)[:160], failures, self.retries)
                    delay = self.RETRY_DELAYS[min(failures, len(self.RETRY_DELAYS)) - 1]
                    if cancel.wait(delay):
                        self._discard(directory, name)
                        raise _Canceled() from None
                    offset = self._resume_offset(client, directory, name, size)
                    with self.lock:
                        job['bytes_done'] += offset
                        if offset:
                            job['resumed'] = job.get('resumed', 0) + 1
                    continue
                self._discard(directory, name)
                # Remove what this file contributed, record it, keep going.
                with self.lock:
                    job['bytes_total'] -= size
                    job['files_failed'] += 1
                    if len(job['errors']) < 20:
                        job['errors'].append(f'{source}：{str(exc)[:160]}')
                    job['error'] = f'{job["files_failed"]} 个文件失败，最近：{source}'
                return
            finally:
                if reader is not None:
                    try:
                        reader.close()
                    except Exception:
                        pass
            if offset:
                # The server may have ignored REST and written from the start.
                actual = self._ps5_size(directory, name)
                if actual is not None and actual != size:
                    self._discard(directory, name)
                    with self.lock:
                        job['bytes_done'] -= size
                    if verified:
                        with self.lock:
                            job['bytes_total'] -= size
                            job['files_failed'] += 1
                            if len(job['errors']) < 20:
                                job['errors'].append(f'{source}：续传后大小不符（{actual} / {size}）')
                            job['error'] = f'{job["files_failed"]} 个文件失败，最近：{source}'
                        return
                    verified = True
                    logging.warning('transfer %s: resumed %s has %s of %d bytes, copying it again',
                                    job['id'], source, actual, size)
                    offset = 0
                    continue
            self._set(job, files_done=job['files_done'] + 1)
            return

    def _discard(self, directory, name):
        """Remove a partial file; the server may need a moment to publish it."""
        for attempt in range(3):
            try:
                self.files.delete(posixpath.join(directory, name))
                return
            except (FileServiceError, ValueError):
                time.sleep(0.5 * (attempt + 1))

    def _progress(self, job, delta, current, samples):
        now = time.time()
        with self.lock:
            job['bytes_done'] += delta
            job['current_done'] = current
            # One sample per 0.1 s: with many small reads a count-limited
            # window covered only milliseconds and the speed rarely updated.
            if not samples or now - samples[-1][0] >= 0.1:
                samples.append((now, job['bytes_done']))
            while len(samples) > 2 and now - samples[0][0] > 5:
                samples.popleft()
            first = samples[0]
            if now - first[0] > 0.3:
                job['speed'] = round((job['bytes_done'] - first[1]) / (now - first[0]))
            total = job['bytes_total']
            job['percent'] = round(job['bytes_done'] * 100 / total, 1) if total else 0
            job['eta'] = (round((total - job['bytes_done']) / job['speed'])
                          if job['speed'] > 0 and total >= job['bytes_done'] else None)
            job['elapsed'] = round(now - job['started'], 1)
        self._persist()


def unrar_engine_available():
    """libunrar5 + rclone + FUSE + systemd-run: everything an unrar worker needs."""
    return (unrar.available() and shutil.which('rclone') is not None
            and (shutil.which('fusermount3') or shutil.which('fusermount')) is not None
            and os.path.exists('/dev/fuse') and shutil.which('systemd-run') is not None)


def _worker_memory():
    """Memory an unrar worker may take: total minus room for everything else."""
    try:
        with open('/proc/meminfo', encoding='ascii') as stream:
            for line in stream:
                if line.startswith('MemTotal:'):
                    return max(0, int(line.split()[1]) * 1024 - 768 * 1024 * 1024)
    except (OSError, ValueError):
        pass
    return 0


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


class SystemdRunner:
    """Starts extract_worker.py as a transient unit with its own memory limit."""

    WORKER = Path(__file__).resolve().with_name('extract_worker.py')

    def start(self, spec_path, unit, memory):
        command = ['systemd-run', f'--unit={unit}', '--collect', '--quiet',
                   '--description=PS5 archive extraction',
                   '-p', f'MemoryMax={max(int(memory), 512 * 1024 * 1024)}',
                   '-p', 'Nice=10', '-p', 'CPUWeight=50', '-p', 'IOWeight=50',
                   # SIGTERM to the worker only: it removes the staging folder and
                   # unmounts before rclone goes; the rest is killed at the timeout.
                   '-p', 'KillMode=mixed', '-p', 'TimeoutStopSec=60',
                   sys.executable, '-I', str(self.WORKER), str(spec_path)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise NasError(f'无法启动解压进程：{(result.stderr or result.stdout).strip()[:200]}')
        return _UnitHandle(unit)

    def attach(self, unit):
        """Handle for a worker unit started before this service restarted."""
        return _UnitHandle(unit)


class _UnitHandle:
    def __init__(self, unit):
        self.unit = unit

    def alive(self):
        state = subprocess.run(['systemctl', 'is-active', self.unit], capture_output=True,
                               text=True, timeout=30).stdout.strip()
        return state in ('active', 'activating', 'deactivating', 'reloading')

    def stop(self):
        subprocess.run(['systemctl', 'stop', '--no-block', self.unit], capture_output=True, timeout=30)


def _lower_priority():
    """Transfers are background work: let DNS and the admin page go first."""
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
    except (AttributeError, OSError):
        pass


def check_connection(config):
    """List the configured folder once; returns a short summary for the page."""
    client = client_for(config, timeout=10)
    try:
        entries = client.list('/')
        result = {'ok': True, 'entries': len(entries)}
        if config.get('writable') is True:
            result['write'] = _write_check(client)
    finally:
        client.close()
    return result


def _write_check(client):
    """Create a folder, write and remove a small file in it, remove the folder."""
    folder = f'/.ps5jb-write-test-{secrets.token_hex(4)}'
    data = b'ps5-jb-server write test\n'
    try:
        client.mkdir(folder)
        put_file(client, folder + '/test.txt', [data], len(data))
        client.delete(folder + '/test.txt')
        client.rmdir(folder)
    except NasError as exc:
        _quiet(client.delete, folder + '/test.txt' + PART)
        _quiet(client.delete, folder + '/test.txt')
        _quiet(client.rmdir, folder)
        raise NasError(f'可以读取，但写入测试失败：{exc}', status=exc.status) from exc
    return 'ok'

