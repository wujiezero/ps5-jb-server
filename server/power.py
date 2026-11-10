"""PS5 power: rest-mode status, Remote Play pairing, wake-and-sign-in, standby.

Waking a PS5 properly needs a Remote Play control session after the DDP
wake-up (a bare wake falls back to rest mode after five minutes with
"RemotePlayConnectTimeout"). That protocol is implemented by the PS5Upload
engine, which runs beside this service as its own process on 127.0.0.1 and is
only ever called from here. The engine keeps the pairing keys itself (sealed
in its data directory); this service never stores or forwards them, and the
browser only learns whether a console is paired.

Status needs nothing but DDP, so it works even when the engine is not
installed. Standby is executed on the console by the PS5Upload payload.
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.request

from ps5 import ddp_search

WAKE_TIMEOUT = 150       # wake-login waits for the console to boot and sign in
POLL_INTERVAL = 2.0


class PowerError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class EngineClient:
    """Minimal JSON client for the local PS5Upload engine."""

    def __init__(self, base_url='http://127.0.0.1:19113', timeout=20):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout

    def request(self, method, path, body=None, timeout=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method,
                                         headers={'Content-Type': 'application/json',
                                                  'Accept': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read(1 << 20)
        except urllib.error.HTTPError as exc:
            raw = exc.read(4096)
            exc.close()
            raise PowerError(_engine_message(raw) or f'引擎返回 HTTP {exc.code}', status=exc.code) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise PowerError('电源控制引擎未运行：请确认 ps5upload-engine 服务已启动') from exc
        try:
            return json.loads(raw or b'{}')
        except ValueError as exc:
            raise PowerError('电源控制引擎返回了无法解析的内容') from exc

    def version(self):
        try:
            return self.request('GET', '/api/version', timeout=3)
        except PowerError:
            return None


def _engine_message(raw):
    try:
        document = json.loads(raw)
    except (ValueError, TypeError):
        return ''
    if isinstance(document, dict):
        for key in ('error', 'message', 'err'):
            if isinstance(document.get(key), str):
                return document[key]
    return ''


# Engine/console refusals rewritten for people. Matching is on substrings of
# the engine's English text, so unknown messages still pass through.
_HINTS = (
    ('rp_off', '主机未开启遥控游玩：在 PS5 设置 › 系统 › 遥控游玩 中打开「启用遥控游玩」'),
    ('remote play', '主机未开启遥控游玩，或该用户未允许遥控游玩'),
    ('80108b03', '主机拒绝配对：PIN 已过期或不正确，请重新获取'),
    ('pin', '配对 PIN 不正确或已过期：在 PS5 设置 › 系统 › 遥控游玩 › 链接装置 获取新的 8 位数字'),
    ('keys', '没有这台主机的配对信息，请先配对'),
    ('not paired', '没有这台主机的配对信息，请先配对'),
    ('connection refused', '主机上没有运行 PS5Upload 插件（端口 9120）：请先在插件库里运行 ps5upload'),
    ('9120', '主机上没有运行 PS5Upload 插件（端口 9120）：请先在插件库里运行 ps5upload'),
    ('timed out', '连接主机超时：请确认主机在线且与本服务同一网段'),
    ('standby_unavailable', '当前固件不支持由插件直接进入待机'),
)


def friendly(message):
    text = str(message or '')
    lower = text.lower()
    for needle, hint in _HINTS:
        if needle in lower:
            return f'{hint}（{text[:160]}）' if text and hint not in text else hint
    return text or '操作失败'


class PowerManager:
    def __init__(self, engine=None, validate_ip=None):
        self.engine = engine or EngineClient()
        self.validate_ip = validate_ip or (lambda ip: ip)
        self.lock = threading.Lock()
        self.jobs = {}          # ip -> wake job

    # -- reads --
    def engine_info(self):
        version = self.engine.version()
        if not version:
            return {'available': False}
        return {'available': True, 'version': str(version.get('version') or version.get('engine') or '')}

    def paired_hosts(self):
        try:
            document = self.engine.request('GET', '/api/consoles', timeout=5)
        except PowerError:
            return set()
        # {consoles: [{addr, has_wake_credential, rp_users, ...}], paired_hosts_not_listed: [host]}
        hosts = set()
        if not isinstance(document, dict):
            return hosts
        for host in document.get('paired_hosts_not_listed') or []:
            hosts.add(str(host).split(':')[0])
        for item in document.get('consoles') or []:
            if not isinstance(item, dict) or not (item.get('has_wake_credential') or item.get('rp_users')):
                continue
            for key in ('host', 'addr', 'address'):
                value = str(item.get(key) or '').split(':')[0]
                if value:
                    hosts.add(value)
        return hosts

    def status(self, ip):
        ip = self.validate_ip(ip)
        reply = ddp_search([ip], timeout=1.0).get(ip)
        engine = self.engine_info()
        with self.lock:
            job = dict(self.jobs.get(ip) or {})
        return {
            'ip': ip,
            'state': reply['state'] if reply else 'off',
            'code': reply['code'] if reply else 0,
            'name': reply['name'] if reply else '',
            'firmware': reply['firmware'] if reply else '',
            'engine': engine,
            'paired': ip in self.paired_hosts() if engine['available'] else False,
            'job': job or None,
        }

    # -- actions --
    def pair(self, ip, pin=None, account_id=None):
        ip = self.validate_ip(ip)
        body = {'host': ip, 'addr': ip}
        if pin:
            pin = str(pin).strip()
            if not (pin.isdigit() and len(pin) == 8):
                raise ValueError('PIN 是 PS5 屏幕上显示的 8 位数字')
            if not account_id:
                raise ValueError('使用 PIN 配对时需要填写 PSN 账号 ID（base64）')
            body.update(pin=pin, account_id=str(account_id).strip())
        try:
            result = self.engine.request('POST', '/api/ps5/power/pair', body, timeout=60)
        except PowerError as exc:
            raise PowerError(friendly(exc), status=exc.status) from exc
        # The engine stores the keys; only harmless facts go back to the page.
        return {'ok': True, 'nickname': str(result.get('nickname') or result.get('user_name') or ''),
                'paired': True}

    def standby(self, ip):
        ip = self.validate_ip(ip)
        try:
            self.engine.request('POST', '/api/ps5/power/control',
                                {'addr': ip, 'action': 'standby'}, timeout=20)
        except PowerError as exc:
            raise PowerError(friendly(exc), status=exc.status) from exc
        return {'ok': True}

    def wake(self, ip):
        """Start wake-and-sign-in in the background; the page polls status()."""
        ip = self.validate_ip(ip)
        with self.lock:
            job = self.jobs.get(ip)
            if job and job['state'] in ('waking', 'signing_in'):
                return dict(job)
            job = {'state': 'waking', 'started': time.time(), 'error': '', 'finished': None,
                   'message': '正在发送唤醒请求…'}
            self.jobs[ip] = job
        threading.Thread(target=self._wake, args=(ip,), daemon=True, name='ps5-wake').start()
        return dict(job)

    def _update(self, ip, **fields):
        with self.lock:
            self.jobs.setdefault(ip, {}).update(fields)

    def _wake(self, ip):
        done = threading.Event()
        result = {}

        def call():
            try:
                result['value'] = self.engine.request(
                    'POST', '/api/ps5/power/wake-login',
                    {'host': ip, 'ask_passcode': False}, timeout=WAKE_TIMEOUT)
            except PowerError as exc:
                result['error'] = exc
            finally:
                done.set()

        threading.Thread(target=call, daemon=True, name='ps5-wake-login').start()
        # While the engine works, report what the console itself says.
        while not done.wait(POLL_INTERVAL):
            reply = ddp_search([ip], timeout=0.8).get(ip)
            if reply and reply['state'] == 'on':
                self._update(ip, state='signing_in', message='主机已开机，正在登录用户…')
            elif reply and reply['state'] == 'standby':
                self._update(ip, message='主机正在启动…')
        if 'error' in result:
            message = friendly(result['error'])
            logging.warning('wake-login %s failed: %s', ip, result['error'])
            self._update(ip, state='failed', error=message, message=message, finished=time.time())
            return
        value = result.get('value') or {}
        signed_in = bool(value.get('signed_in', True))
        message = '主机已唤醒并登录用户' if signed_in else '主机已唤醒，停在用户选择/密码界面'
        self._update(ip, state='done', signed_in=signed_in, message=message, finished=time.time())
