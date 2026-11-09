#!/usr/bin/env python3
"""Authenticated PS5 DNS and IPv4 gateway policy service."""
import argparse
import datetime
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
import urllib.parse
import signal
import ssl
import subprocess
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, HTTPServer

from activity import Classifier, FlowObserver, LabelsStore, make_poller
from api_console import ConsoleAPI
from settings import Settings
from dns_service import DNSPolicy, LimitedThreads, diagnostic_error, start_dns
from policy import ConfigStore, Firewall


class AuthManager:
    def __init__(self, credentials):
        self.credentials = credentials
        self.sessions = OrderedDict()
        self.attempts = OrderedDict()
        self.lock = threading.RLock()

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
            self.sessions = OrderedDict((k, v) for k, v in self.sessions.items() if v['expires'] > now)
            if len(self.sessions) >= 64:
                self.sessions.popitem(last=False)
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.sessions[token] = {'csrf': csrf, 'expires': now + 8 * 3600}
            return token, csrf

    def session(self, token):
        with self.lock:
            found = self.sessions.get(token)
            if found and found['expires'] > time.monotonic():
                return dict(found)
            self.sessions.pop(token, None)
            return None

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)


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
    """Owns /srv/ps5-repo: repo.json plus payload files, written atomically."""

    MAX_UPLOAD = 64 * 1024 * 1024
    NAME = '家庭离线仓库（PS5 常用插件）'
    _FILENAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(?:elf|bin)\Z', re.IGNORECASE)
    _SEED_FILE = 'ps5-payload-repository'
    _FIELDS = ('name', 'version', 'category', 'author', 'description', 'source')

    def __init__(self, root, base_url='http://127.0.0.1:8080/payloads/'):
        self.root = Path(root)
        self.base_url = base_url if base_url.endswith('/') else base_url + '/'
        self.payloads = self.root / 'payloads'
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

    def _write_catalog(self, document):
        temporary = self.catalog_path.with_name('.' + self.catalog_path.name + '.tmp')
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(temporary, self.catalog_path)

    def catalog(self):
        with self.lock:
            document = json.loads(self.catalog_path.read_text(encoding='utf-8'))
        document.setdefault('payloads', [])
        return document

    def summary(self):
        with self.lock:
            document = self.catalog()
            items, total = [], 0
            for item in document['payloads']:
                path = self.payloads / item['filename']
                size = path.stat().st_size if path.is_file() else 0
                total += size
                items.append({**item, 'size': size})
            return {'catalog': {'name': document.get('name', self.NAME), 'items': items},
                    'total_bytes': total}

    def _save_payload(self, filename, data):
        temporary = self.payloads / ('.' + filename + '.tmp')
        temporary.write_bytes(data)
        os.replace(temporary, self.payloads / filename)

    def add(self, filename, data, name, version='', category='', author='', description=''):
        self._check_name(filename)
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise ValueError('文件内容为空')
        if len(data) > self.MAX_UPLOAD:
            raise ValueError('文件超过 64MB 上限')
        if not isinstance(name, str) or not name.strip():
            raise ValueError('插件名称不能为空')
        with self.lock:
            target = self.payloads / filename
            if target.exists():
                raise FileExistsError('同名文件已存在，请先删除或改名')
            digest = hashlib.sha256(bytes(data)).hexdigest()
            entry = {
                'id': re.sub(r'[^a-z0-9._-]+', '-', filename.rsplit('.', 1)[0].lower()).strip('-') or 'payload',
                'name': name.strip()[:120], 'filename': filename,
                'url': self.base_url + filename,
                'version': str(version)[:32], 'last_update': datetime.date.today().isoformat(),
                'category': (category or 'Uncategorized')[:64],
                'description': str(description)[:400], 'author': str(author)[:64],
                'checksum': digest,
            }
            self._save_payload(filename, bytes(data))
            document = self.catalog()
            # Replace the placeholder shipped with a fresh install.
            document['payloads'] = [p for p in document['payloads']
                                    if p.get('filename') != self._SEED_FILE]
            document['payloads'].append(entry)
            self._write_catalog(document)
            return entry

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
            remaining = [p for p in document['payloads'] if p.get('filename') != filename]
            if len(remaining) == len(document['payloads']):
                raise FileNotFoundError('仓库中没有这个文件')
            target = self.payloads / filename
            if target.exists():
                target.unlink()
            document['payloads'] = remaining
            self._write_catalog(document)


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

    def state(self):
        with self.engine.store.lock:
            try:
                counters = self.engine.firewall.counters()
                gateway = 'error' if self.engine.gateway_error else 'active'
            except Exception as exc:
                counters = {'error': str(exc)[:200]}
                gateway = 'error'
            check = subprocess.run(['systemctl', 'is-active', 'ps5-host.service'],
                                   capture_output=True, text=True, timeout=3)
            ipv6 = Path('/proc/sys/net/ipv6/conf/all/forwarding')
            return {
                'config': self.engine.store.snapshot(),
                'status': {'host_ip': self.engine.host_ip, 'dns': 'running', 'gateway': gateway,
                           'host': check.stdout.strip(), 'uptime': round(time.monotonic() - self.started),
                           'ipv6_forwarding': ipv6.exists() and ipv6.read_text().strip() == '1',
                           'last_applied': self.engine.last_applied,
                           'last_error': self.engine.last_error,
                           'gateway_error': self.engine.gateway_error,
                           'dynamic_addresses': len(self.engine.live_leases())},
                'logs': list(self.engine.logs), 'firewall': counters}


class AdminHandler(BaseHTTPRequestHandler):
    server_version = 'PS5LocalManager/1.0'
    protocol_version = 'HTTP/1.0'

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, fmt, *args):
        # Never log request bodies, cookies, or credentials.
        logging.info('%s %s', self.client_address[0], fmt % args)

    def send_body(self, status, data, ctype='application/json; charset=utf-8', cookie=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
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

    def do_GET(self):
        if not self.guard():
            return
        if self.path == '/api/state':
            session = self.require_session()
            if session:
                try:
                    self.send_body(200, {**self.server.app.state(), 'csrf': session['csrf']})
                except Exception:
                    logging.exception('Read state failed')
                    self.send_body(500, {'error': '读取状态失败，请检查服务日志'})
            return
        if self.path.startswith('/api/activity'):
            if not self.require_session():
                return
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
            return
        if self.path == '/api/repo':
            if not self.require_session():
                return
            try:
                summary = self.server.repo.summary()
                self.send_body(200, {**summary, 'host': self.server.server_address[0], 'port': 8080})
            except Exception:
                logging.exception('Read repository failed')
                self.send_body(500, {'error': '读取插件仓库失败，请检查服务日志'})
            return
        if self.path.startswith('/api/ps5') or self.path.startswith('/api/files'):
            if self.server.app.console_get(self):
                return
        files = {'/': ('index.html', 'text/html; charset=utf-8'),
                 '/index.html': ('index.html', 'text/html; charset=utf-8'),
                 '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                 '/style.css': ('style.css', 'text/css; charset=utf-8')}
        if self.path not in files:
            self.send_body(404, {'error': '未找到页面'})
            return
        name, ctype = files[self.path]
        try:
            self.send_body(200, (self.server.static_dir / name).read_bytes(), ctype)
        except OSError:
            self.send_body(503, {'error': '页面文件暂不可用'})

    def handle_upload(self):
        """Raw-body upload: metadata in the query string, bytes in the body."""
        if not self.require_session(write=True):
            return
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        filename = (query.get('filename') or [''])[0]
        meta = {key: (query.get(key) or [''])[0] for key in
                ('name', 'version', 'category', 'author', 'description')}
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
        if length > RepoManager.MAX_UPLOAD:
            self.send_body(413, {'error': '文件超过 64MB 上限'})
            return
        data = self.rfile.read(length)
        if len(data) != length:
            self.send_body(400, {'error': '上传中断，请重试'})
            return
        try:
            entry = self.server.repo.add(filename, data, **meta)
        except FileExistsError as exc:
            self.send_body(409, {'error': str(exc)})
            return
        except ValueError as exc:
            self.send_body(400, {'error': str(exc)})
            return
        except Exception:
            logging.exception('Repository upload failed')
            self.send_body(500, {'error': '保存失败，请检查服务日志'})
            return
        logging.info('repository upload: %s (%d bytes)', entry['filename'], length)
        self.send_body(200, {'ok': True, 'filename': entry['filename']})

    def do_POST(self):
        if not self.guard():
            return
        route = self.path.split('?', 1)[0]
        if route.startswith('/api/ps5') or route.startswith('/api/files'):
            self.server.app.console_post(self, route)
            return
        if route == '/api/repo/upload':
            self.handle_upload()
            return
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
            return
        if self.path == '/api/login':
            try:
                token, csrf = self.server.auth.login(self.client_address[0], body.get('password'))
                self.send_body(200, {'csrf': csrf}, cookie=f'ps5_session={token}; Path=/; Max-Age=28800; HttpOnly; Secure; SameSite=Strict')
            except PermissionError as exc:
                self.send_body(401, {'error': str(exc)})
            return
        if route not in ('/api/config', '/api/rollback', '/api/logout',
                         '/api/password', '/api/repo/update', '/api/repo/delete',
                         '/api/labels', '/api/allow'):
            self.send_body(404, {'error': '未知操作'})
            return
        if not self.require_session(write=True):
            return
        try:
            if route == '/api/logout':
                self.server.auth.logout(self.token())
                self.send_body(200, {'ok': True}, cookie='ps5_session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict')
            elif route == '/api/password':
                change_password(self.server.auth, self.server.credentials_path,
                                body.get('current_password'), body.get('new_password'))
                self.send_body(200, {'ok': True},
                               cookie='ps5_session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict')
            elif route == '/api/labels':
                self.server.app.activity.labels.set(body.get('target'),
                                                    body.get('name', ''), body.get('category', ''))
                self.send_body(200, {'ok': True})
            elif route == '/api/allow':
                targets = body.get('targets')
                if not isinstance(targets, list) or not targets:
                    raise ValueError('请选择要放行的条目')
                if len(targets) > 200:
                    raise ValueError('一次最多放行 200 项')
                self.send_body(200, {'ok': True, 'config': self.server.app.allow(targets)})
            elif route == '/api/repo/update':
                self.server.repo.update(body.get('filename'),
                                        **{key: body[key] for key in RepoManager._FIELDS if key in body})
                self.send_body(200, {'ok': True})
            elif route == '/api/repo/delete':
                self.server.repo.delete(body.get('filename'))
                self.send_body(200, {'ok': True})
            else:
                self.server.app.change(body, rollback=route == '/api/rollback')
                self.send_body(200, {'ok': True})
        except PermissionError as exc:
            self.send_body(403, {'error': str(exc)})
        except FileNotFoundError as exc:
            self.send_body(404, {'error': str(exc)})
        except ValueError as exc:
            self.send_body(400, {'error': str(exc)})
        except Exception as exc:
            logging.exception('Policy apply failed')
            self.send_body(500, {'error': '应用失败：' + diagnostic_error(exc) + '。已尝试恢复上一份策略，请查看概览状态。'})


class AdminServer(LimitedThreads, HTTPServer):
    allow_reuse_address = True
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
    data = data_dir
    store = ConfigStore(data / 'config.json')
    firewall = Firewall(host_ip=host_ip, lan=lan)
    engine = DNSPolicy(store, firewall, host_ip=host_ip)
    initialize_engine(engine)
    engine.on_apply = set_forwarding
    auth = AuthManager(json.loads((data / 'credentials.json').read_text()))
    admin = AdminServer((host_ip, admin_port), AdminHandler)
    labels = LabelsStore(data / 'labels.json')
    observer = FlowObserver(
        conntrack=_read_conntrack,
        classifier=Classifier(settings.rulesets_path),
        labels=labels,
        dns_lookup=engine.lookup_ip,
        is_allowed=_allowed_destination(engine),
        lan=lan, host_ip=host_ip)
    stop_event = threading.Event()
    threading.Thread(target=make_poller(observer), args=(stop_event,), daemon=True).start()
    admin.app, admin.auth = Application(engine, observer), auth
    admin.app.settings = settings
    admin.app.console = ConsoleAPI(settings, _catalog_reader(repo_dir / 'repo.json'))
    admin.repo = RepoManager(repo_dir,
                             base_url=f'http://{host_ip}:{settings.repo_port}/payloads/')
    admin.credentials_path = data / 'credentials.json'
    admin.static_dir = settings.static_dir
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    admin.tls_context = ctx
    dns_servers = start_dns(engine, '0.0.0.0', dns_port)
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
