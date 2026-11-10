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

from files import FileServiceError
from ftp_files import listing

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
              'tls', 'verify_tls', 'workgroup')
    # Written to disk: never the username or password themselves.
    STORED = ('id', 'name', 'protocol', 'host', 'port', 'share', 'path',
              'tls', 'verify_tls', 'workgroup', 'credentials')

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


def _entry(path, name, is_dir, size=0, mtime=0):
    return {'name': name, 'path': posixpath.join(path, name) if path != '/' else '/' + name,
            'dir': bool(is_dir), 'size': 0 if is_dir else int(size or 0), 'mtime': int(mtime or 0)}


# ------------------------------------------------------------------ WebDAV --

class WebDavClient:
    PROPFIND = (b'<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:"><d:prop>'
                b'<d:resourcetype/><d:getcontentlength/><d:getlastmodified/></d:prop></d:propfind>')

    def __init__(self, config, timeout):
        scheme = 'https' if config.get('tls') else 'http'
        host = config['host']
        if ':' in host:
            host = f'[{host}]'
        self.root = clean_path(config.get('path') or '/')
        self.origin = f'{scheme}://{host}:{config["port"]}'
        self.timeout = timeout
        handlers = []
        if scheme == 'https':
            context = ssl.create_default_context()
            if not config.get('verify_tls'):
                # Home NAS boxes mostly use self-signed certificates.
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=context))
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

    def open(self, path):
        # A dedicated connection: the listing connection stays usable.
        reader = FtpClient(self.config, self.timeout)
        conn = reader._connect()
        try:
            data = conn.transfercmd('RETR ' + self._full(path))
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

class _Canceled(Exception):
    pass


class TransferManager:
    """One NAS transfer at a time; the rest wait in order."""

    KEEP = 30

    def __init__(self, store, files, client_factory=client_for):
        self.store = store
        self.files = files
        self.client_factory = client_factory
        self.lock = threading.RLock()
        self.jobs = collections.OrderedDict()
        self.queue = collections.deque()
        self.cancels = {}
        self.worker = None

    # -- public --
    def start(self, nas_id, paths, dest, overwrite='skip'):
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
               'elapsed': 0, 'speed': 0, 'eta': None, 'percent': 0, 'error': '', 'errors': []}
        with self.lock:
            self.jobs[ident] = job
            self.cancels[ident] = threading.Event()
            self.queue.append(ident)
            self._trim()
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._run, daemon=True, name='nas-transfer')
                self.worker.start()
        return self.snapshot_one(ident)

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
        return {'ok': True}

    def clear(self):
        with self.lock:
            for ident in [key for key, job in self.jobs.items()
                          if job['state'] in ('done', 'failed', 'canceled')]:
                self.jobs.pop(ident, None)
                self.cancels.pop(ident, None)
        return {'ok': True}

    def _trim(self):
        finished = [key for key, job in self.jobs.items()
                    if job['state'] in ('done', 'failed', 'canceled')]
        while len(self.jobs) > self.KEEP and finished:
            key = finished.pop(0)
            self.jobs.pop(key, None)
            self.cancels.pop(key, None)

    # -- worker --
    def _run(self):
        while True:
            with self.lock:
                if not self.queue:
                    self.worker = None
                    return
                ident = self.queue.popleft()
                job = self.jobs.get(ident)
            if job is None or job['state'] != 'queued':
                continue
            try:
                self._execute(job, self.cancels[ident])
            except _Canceled:
                self._set(job, state='canceled')
            except (NasError, FileServiceError, ValueError, OSError) as exc:
                self._set(job, state='failed', error=str(exc)[:300])
            except Exception as exc:  # keep the queue alive whatever happens
                logging.exception('NAS transfer failed')
                self._set(job, state='failed', error=f'内部错误：{exc}'[:300])
            finally:
                self._set(job, finished=time.time(), speed=0, eta=None,
                          elapsed=round(time.time() - (job['started'] or time.time()), 1))

    def _set(self, job, **fields):
        with self.lock:
            job.update(fields)

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
            samples = collections.deque(maxlen=60)
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
            self._set(job, state='done', percent=100, current='', current_done=0, current_size=0)
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _mkdir(self, directory):
        try:
            self.files.mkdir(directory)
        except FileServiceError:
            pass        # usually "already exists"; a real problem surfaces on upload

    def _copy_file(self, job, client, source, directory, name, size, cancel, samples):
        self._set(job, current=source, current_size=size, current_done=0)
        reader = client.open(source)
        sent = [0]

        def chunks():
            while True:
                if cancel.is_set():
                    raise _Canceled()
                data = reader.read(CHUNK)
                if not data:
                    return
                sent[0] += len(data)
                self._progress(job, len(data), sent[0], samples)
                yield data

        try:
            self.files.upload(directory, name, chunks(), size, overwrite=True)
            self._set(job, files_done=job['files_done'] + 1)
        except _Canceled:
            self._discard(directory, name)
            raise
        except (FileServiceError, ValueError, OSError, NasError) as exc:
            self._discard(directory, name)
            # Remove what this file contributed, record it, keep going.
            with self.lock:
                job['bytes_done'] -= sent[0]
                job['bytes_total'] -= size
                job['files_failed'] += 1
                if len(job['errors']) < 20:
                    job['errors'].append(f'{source}：{str(exc)[:160]}')
                job['error'] = f'{job["files_failed"]} 个文件失败，最近：{source}'
        finally:
            try:
                reader.close()
            except Exception:
                pass

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


def check_connection(config):
    """List the configured folder once; returns a short summary for the page."""
    client = client_for(config, timeout=10)
    try:
        entries = client.list('/')
    finally:
        client.close()
    return {'ok': True, 'entries': len(entries)}

