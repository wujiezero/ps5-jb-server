"""Completion notifications: generic webhook, Bark, ntfy and Telegram.

Channels live in <data>/notify.json (0600, atomic). Tokens, device keys,
topics and webhook URLs (which often carry a key) are sealed with the NAS
SecretBox, bound to "notify:<channel id>"; the API only says has_secret.

Sending never blocks the caller: events go into a bounded queue (oldest
dropped when full) drained by one background thread. Each request has a
5 second timeout and two retries; the same event key is sent at most once a
minute. Only http/https targets, no redirect that changes the scheme. Public
targets go through netproxy (Telegram usually needs it), private addresses
always directly; a channel can turn the proxy off.

Log lines name the channel and the failure kind only, never a URL or token.
"""
import collections
import datetime
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

TYPES = {'webhook': '通用 Webhook', 'bark': 'Bark', 'ntfy': 'ntfy', 'telegram': 'Telegram'}
EVENTS = {
    'transfer.finished': 'NAS 传输完成', 'transfer.failed': 'NAS 传输失败',
    'extract.finished': '解压安装完成', 'extract.failed': '解压安装失败',
    'package.finished': 'PKG 安装完成', 'package.failed': 'PKG 安装失败',
    'backup.finished': 'PS5 备份完成', 'backup.failed': 'PS5 备份失败',
    'migrate.finished': '游戏迁移完成', 'migrate.failed': '游戏迁移失败',
    'uninstall.finished': '游戏卸载完成', 'uninstall.failed': '游戏卸载失败',
    'update.finished': '服务更新完成', 'update.failed': '服务更新失败',
    'gateway.error': '网关出错',
}
DEFAULT_EVENTS = ('transfer.finished', 'transfer.failed', 'extract.finished', 'extract.failed',
                  'update.finished', 'update.failed', 'gateway.error')
# Per type: public fields and secret fields (sealed).
FIELDS = {'webhook': ((), ('url',)), 'bark': (('server',), ('device_key',)),
          'ntfy': (('server',), ('topic', 'token')), 'telegram': (('chat_id',), ('token',))}
REQUIRED_SECRET = {'webhook': 'url', 'bark': 'device_key', 'ntfy': 'topic', 'telegram': 'token'}
DEFAULT_SERVER = {'bark': 'https://api.day.app', 'ntfy': 'https://ntfy.sh'}
TELEGRAM_API = 'https://api.telegram.org'
MAX_CHANNELS = 10
QUEUE_SIZE = 100
TIMEOUT = 5
RETRIES = 2
RETRY_DELAY = 2.0
DEDUP_SECONDS = 60
UPDATE_DONE = {'done': 'update.finished', 'failed': 'update.failed', 'rolled_back': 'update.failed'}


class NotifyError(ValueError):
    """Shown to the admin as is."""


def _check_url(value, what):
    value = str(value or '').strip()
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise NotifyError(f'{what}必须是 http:// 或 https:// 开头的地址')
    if len(value) > 500:
        raise NotifyError(f'{what}太长')
    return value.rstrip('/') if what != 'Webhook 地址' else value


def _atomic(path, document):
    path = Path(path)
    temporary = path.with_name('.' + path.name + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(document, stream, ensure_ascii=False, indent=1)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class NotifyStore:
    """Channel settings; secrets sealed with the shared SecretBox."""

    IDENT = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')

    def __init__(self, path, box):
        self.path = Path(path)
        self.box = box
        self.lock = threading.RLock()
        try:
            document = json.loads(self.path.read_text(encoding='utf-8'))
            self.items = [item for item in document.get('channels', [])
                          if isinstance(item, dict) and self.IDENT.match(str(item.get('id') or ''))
                          and item.get('type') in TYPES]
        except (OSError, ValueError, AttributeError):
            self.items = []

    def _save(self):
        _atomic(self.path, {'version': 1, 'channels': self.items})

    def _secrets(self, item):
        if not item.get('secret'):
            return {}
        username, _ = self.box.open('notify:' + item['id'], item['secret'])
        values = json.loads(username or '{}')
        return values if isinstance(values, dict) else {}

    def _seal(self, ident, values):
        values = {key: value for key, value in values.items() if value}
        return self.box.seal('notify:' + ident, json.dumps(values), '') if values else ''

    def public(self, item):
        result = {key: item.get(key) for key in ('id', 'name', 'type', 'enabled', 'events', 'use_proxy')}
        for key in FIELDS[item['type']][0]:
            result[key] = item.get(key, '')
        try:
            values = self._secrets(item)
            result['secret_ok'] = True
        except Exception:
            values, result['secret_ok'] = {}, False
        result['has_secret'] = bool(values.get(REQUIRED_SECRET[item['type']]))
        if item['type'] == 'ntfy':
            result['has_token'] = bool(values.get('token'))
        return result

    def list_public(self):
        with self.lock:
            return [self.public(item) for item in self.items]

    def channels(self):
        """Enabled channels with their secrets opened, for sending."""
        with self.lock:
            found = []
            for item in self.items:
                if not item.get('enabled', True):
                    continue
                try:
                    found.append({**item, **self._secrets(item)})
                except Exception:
                    logging.warning('notify channel %s: secret cannot be opened', item.get('name'))
            return found

    def get(self, ident):
        with self.lock:
            for item in self.items:
                if item['id'] == ident:
                    return {**item, **self._secrets(item)}
        raise NotifyError('没有这个通知渠道')

    def save(self, raw):
        if not isinstance(raw, dict):
            raise NotifyError('渠道配置格式不正确')
        kind = raw.get('type')
        if kind not in TYPES:
            raise NotifyError('渠道类型只能是 Webhook、Bark、ntfy 或 Telegram')
        with self.lock:
            ident = raw.get('id') or None
            existing = None
            if ident:
                existing = next((item for item in self.items if item['id'] == ident), None)
                if existing is None:
                    raise NotifyError('没有这个通知渠道')
            elif len(self.items) >= MAX_CHANNELS:
                raise NotifyError(f'最多 {MAX_CHANNELS} 个通知渠道')
            ident = ident or uuid.uuid4().hex[:12]
            name = str(raw.get('name') or '').strip()[:40] or TYPES[kind]
            events = [event for event in raw.get('events', DEFAULT_EVENTS) or [] if event in EVENTS]
            item = {'id': ident, 'name': name, 'type': kind, 'enabled': raw.get('enabled', True) is not False,
                    'events': sorted(set(events), key=list(EVENTS).index),
                    'use_proxy': raw.get('use_proxy', True) is not False}
            public, secret = FIELDS[kind]
            for key in public:
                value = str(raw.get(key) or '').strip()
                if key == 'server':
                    value = _check_url(value or DEFAULT_SERVER[kind], '服务器地址')
                elif key == 'chat_id':
                    if not re.fullmatch(r'-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{3,63}', value):
                        raise NotifyError('Telegram chat_id 应为数字（群组为负数）或 @频道名')
                item[key] = value
            current = {}
            if existing is not None and existing['type'] == kind:
                try:
                    current = self._secrets(existing)
                except Exception:
                    current = {}
            values = {}
            for key in secret:
                value = str(raw.get(key) or '').strip()
                if key == 'token' and kind == 'ntfy' and raw.get('clear_token') is True:
                    continue
                values[key] = value or current.get(key, '')
            required = REQUIRED_SECRET[kind]
            if not values.get(required):
                raise NotifyError({'url': '请填写 Webhook 地址', 'device_key': '请填写 Bark 设备密钥',
                                   'topic': '请填写 ntfy 主题', 'token': '请填写 Telegram 机器人令牌'}[required])
            if kind == 'webhook':
                values['url'] = _check_url(values['url'], 'Webhook 地址')
            if kind == 'telegram' and not re.fullmatch(r'\d{3,15}:[A-Za-z0-9_-]{20,100}', values['token']):
                raise NotifyError('Telegram 机器人令牌格式不正确（形如 123456:ABC…）')
            if kind == 'ntfy' and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', values['topic']):
                raise NotifyError('ntfy 主题只能包含字母、数字、下划线和连字符')
            if kind == 'bark' and not re.fullmatch(r'[A-Za-z0-9]{8,64}', values['device_key']):
                raise NotifyError('Bark 设备密钥格式不正确')
            item['secret'] = self._seal(ident, values)
            if existing is None:
                self.items.append(item)
            else:
                self.items = [item if old['id'] == ident else old for old in self.items]
            self._save()
            return self.public(item)

    def delete(self, ident):
        with self.lock:
            if not any(item['id'] == ident for item in self.items):
                raise NotifyError('没有这个通知渠道')
            self.items = [item for item in self.items if item['id'] != ident]
            self._save()

    # ---- backup ----
    def document(self):
        with self.lock:
            return {'version': 1, 'channels': [dict(item) for item in self.items]}

    def restore(self, document, with_key=False):
        """Replace channels from a backup; returns (channels, channels needing their secret again).

        Without the backup's key the sealed secrets cannot be opened here and
        are dropped (a channel this host already has keeps its own).
        """
        channels = document.get('channels') if isinstance(document, dict) else None
        if not isinstance(channels, list):
            raise ValueError('通知渠道数据格式不正确')
        items = []
        for raw in channels[:MAX_CHANNELS]:
            if not (isinstance(raw, dict) and self.IDENT.match(str(raw.get('id') or '')) and raw.get('type') in TYPES):
                continue
            public, _ = FIELDS[raw['type']]
            item = {'id': raw['id'], 'name': str(raw.get('name') or '')[:40] or TYPES[raw['type']],
                    'type': raw['type'], 'enabled': raw.get('enabled', True) is not False,
                    'events': [event for event in raw.get('events') or [] if event in EVENTS],
                    'use_proxy': raw.get('use_proxy', True) is not False,
                    'secret': str(raw.get('secret') or '')}
            for key in public:
                item[key] = str(raw.get(key) or '')
            items.append(item)
        with self.lock:
            if not with_key:
                current = {item['id']: item.get('secret', '') for item in self.items}
                for item in items:
                    item['secret'] = current.get(item['id'], '')
            self.items = items
            self._save()
            return len(items), sum(1 for item in items if not item['secret'])


# ------------------------------------------------------------- delivery ---

class _SameScheme(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only when it keeps the scheme (no https -> http)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != urllib.parse.urlsplit(req.full_url).scheme:
            raise urllib.error.HTTPError(req.full_url, code, '拒绝跨协议重定向', headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def is_private(host, resolver=socket.getaddrinfo):
    """True when every address of host is on a private / local network."""
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(info[4][0]) for info in resolver(host, None)]
        except (OSError, ValueError):
            return False
    return bool(addresses) and all(a.is_private or a.is_loopback or a.is_link_local for a in addresses)


def default_opener(url, use_proxy):
    host = urllib.parse.urlsplit(url).hostname or ''
    if use_proxy and not is_private(host):
        import netproxy
        return netproxy.opener(redirect=_SameScheme)
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _SameScheme())


def build_request(channel, message):
    """urllib Request for one channel and message {event, title, body, time}."""
    kind = channel['type']
    headers = {'Content-Type': 'application/json; charset=utf-8', 'User-Agent': 'ps5-jb-server'}
    if kind == 'webhook':
        url = channel['url']
        payload = {'event': message['event'], 'title': message['title'], 'body': message['body'],
                   'time': message['time'], 'source': 'ps5-jb-server'}
    elif kind == 'bark':
        url = channel['server'] + '/push'
        payload = {'device_key': channel['device_key'], 'title': message['title'], 'body': message['body'],
                   'group': 'PS5'}
    elif kind == 'ntfy':
        url = channel['server'] + '/'
        payload = {'topic': channel['topic'], 'title': message['title'], 'message': message['body'],
                   'tags': ['video_game']}
        if channel.get('token'):
            headers['Authorization'] = 'Bearer ' + channel['token']
    else:
        url = f'{TELEGRAM_API}/bot{channel["token"]}/sendMessage'
        payload = {'chat_id': channel['chat_id'], 'text': f'{message["title"]}\n{message["body"]}'.strip(),
                   'disable_web_page_preview': True}
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    return urllib.request.Request(url, data=data, headers=headers, method='POST')


def _failure(exc):
    """A log/UI-safe description: never the URL (it may hold a token)."""
    if isinstance(exc, urllib.error.HTTPError):
        return f'HTTP {exc.code}'
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        return f'连接失败（{reason.__class__.__name__ if not isinstance(reason, str) else "网络错误"}）'
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return '超时'
    return exc.__class__.__name__


class Notifier:
    """Queue + sender thread + watchers for update / gateway state."""

    def __init__(self, store, opener=default_opener, clock=time.time, sleep=time.sleep,
                 state_path=None):
        self.store = store
        self.opener = opener
        self.clock, self.sleep = clock, sleep
        self.queue = collections.deque(maxlen=QUEUE_SIZE)
        self.cond = threading.Condition()
        self.recent = {}
        self.thread = None
        self.stopped = False
        self.state_path = Path(state_path) if state_path else None
        self.dropped = 0

    # ---- producing ----
    def emit(self, event, title, body='', key=''):
        """Queue an event for every subscribed channel; never blocks or raises."""
        try:
            if event not in EVENTS:
                return 0
            now = self.clock()
            dedup = f'{event}:{key}'
            with self.cond:
                self.recent = {k: t for k, t in self.recent.items() if now - t < DEDUP_SECONDS}
                if dedup in self.recent:
                    return 0
                self.recent[dedup] = now
            message = {'event': event, 'title': title[:200], 'body': body[:1000],
                       'time': datetime.datetime.fromtimestamp(now).astimezone().isoformat(timespec='seconds')}
            count = 0
            for channel in self.store.channels():
                if event in channel.get('events', []):
                    with self.cond:
                        if len(self.queue) == self.queue.maxlen:
                            self.dropped += 1
                        self.queue.append((channel, message))
                        self.cond.notify()
                    count += 1
            if count:
                self._ensure_worker()
            return count
        except Exception:
            logging.exception('notify: could not queue %s', event)
            return 0

    def _ensure_worker(self):
        with self.cond:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._work, name='notify', daemon=True)
                self.thread.start()

    def _work(self):
        while True:
            with self.cond:
                while not self.queue and not self.stopped:
                    if not self.cond.wait(timeout=60):
                        if not self.queue:
                            self.thread = None
                            return
                if self.stopped:
                    return
                channel, message = self.queue.popleft()
            try:
                self.deliver(channel, message)
            except Exception as exc:
                logging.warning('notify: %s (%s) failed for %s: %s', channel.get('name'), channel.get('type'),
                                message['event'], _failure(exc))

    def deliver(self, channel, message, retries=RETRIES):
        last = None
        for attempt in range(retries + 1):
            if attempt:
                self.sleep(RETRY_DELAY * attempt)
            try:
                request = build_request(channel, message)
                with self.opener(request.full_url, channel.get('use_proxy', True)).open(request, timeout=TIMEOUT) as reply:
                    reply.read(4096)
                return
            except urllib.error.HTTPError as exc:
                last = exc
                if 400 <= exc.code < 500 and exc.code not in (408, 429):
                    break                     # a wrong token does not get better
            except Exception as exc:
                last = exc
        raise last

    def test(self, ident):
        """Send a test message now; raises NotifyError with a safe reason."""
        channel = self.store.get(ident)
        message = {'event': 'test', 'title': 'PS5 管家通知测试', 'body': '收到这条消息说明通知渠道配置正确。',
                   'time': datetime.datetime.fromtimestamp(self.clock()).astimezone().isoformat(timespec='seconds')}
        try:
            self.deliver(channel, message, retries=0)
        except Exception as exc:
            raise NotifyError(f'发送失败：{_failure(exc)}') from None

    def stop(self):
        with self.cond:
            self.stopped = True
            self.cond.notify_all()

    # ---- event sources ----
    def job_finished(self, job):
        """TransferManager listener: called once per job when it ends."""
        state, kind = job.get('state'), job.get('kind')
        if state == 'canceled' or kind not in ('nas', 'extract', 'package', 'backup', 'migrate', 'uninstall'):
            return
        prefix = {'nas': 'transfer', 'extract': 'extract', 'package': 'package', 'backup': 'backup',
                  'migrate': 'migrate', 'uninstall': 'uninstall'}[kind]
        ok = state in ('done', 'done_with_warnings')
        label = {'nas': 'NAS 传输', 'extract': '解压安装', 'package': 'PKG 安装', 'backup': 'PS5 备份',
                 'migrate': '游戏迁移', 'uninstall': '游戏卸载'}[kind] + ('完成' if ok else '失败')
        if state == 'done_with_warnings':
            label += '（有提醒）'
        name = str(job.get('name') or job.get('source') or '')
        if ok and kind == 'uninstall':
            steps = job.get('steps') or []
            parts = [text for step, text in (('icon', '已移除主屏幕图标'), ('files', '已删除游戏文件夹'))
                     if step in steps]
            if job.get('cheats_deleted'):
                parts.append(f'删除 {job["cheats_deleted"]} 个金手指文件')
            if job.get('saves_deleted'):
                parts.append(f'删除 {job["saves_deleted"]} 个存档')
            parts += [str(item) for item in (job.get('warnings') or [])[:2]]
            body = '，'.join(parts) or '已卸载'
        elif ok:
            size = _size(job.get('bytes_total') or job.get('bytes_done') or 0)
            elapsed = float(job.get('elapsed') or 0)
            speed = (job.get('bytes_done') or 0) / elapsed if elapsed > 0 else 0
            parts = [f'{job.get("files_done") or 0} 个文件', size, f'用时 {_duration(elapsed)}']
            if speed:
                parts.append(f'平均 {_size(speed)}/s')
            if job.get('files_failed'):
                parts.append(f'{job["files_failed"]} 个失败')
            for key in ('warning', 'cheat_error', 'source_error', 'registration_error'):
                if job.get(key):
                    parts.append(str(job[key]))
                    break
            body = '，'.join(parts)
        else:
            body = str(job.get('error') or '未知错误')
        self.emit(f'{prefix}.{"finished" if ok else "failed"}', f'{label}：{name}', body, key=str(job.get('id')))

    def _state(self):
        if not self.state_path:
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def check_update(self, job):
        """Notify once per upgrade job when it reaches its final state."""
        if not isinstance(job, dict) or job.get('state') not in UPDATE_DONE or not job.get('id'):
            return False
        state = self._state()
        if state.get('update_job') == job['id']:
            return False
        if self.state_path:
            _atomic(self.state_path, {**state, 'update_job': job['id']})
        try:
            stale = self.clock() - float(job.get('updated_at') or 0) > 3600
        except (TypeError, ValueError):
            stale = True
        if stale:                 # an old result seen for the first time: remember it, stay quiet
            return False
        version = job.get('version') or ''
        if job['state'] == 'done':
            self.emit('update.finished', f'服务已更新到 v{version}', '管理服务已重启并通过检查。', key=job['id'])
        else:
            what = '已回滚到原版本' if job['state'] == 'rolled_back' else '更新失败'
            self.emit('update.failed', f'服务更新失败（v{version}）', f'{what}：{job.get("error") or "未知错误"}',
                      key=job['id'])
        return True

    def watch(self, update_status=None, gateway_error=None, interval=15.0, stop=None):
        """Background loop: upgrade results and gateway errors."""
        stop = stop or threading.Event()
        previous = None

        def loop():
            nonlocal previous
            while not stop.is_set():
                try:
                    if update_status is not None:
                        self.check_update((update_status() or {}).get('job'))
                    if gateway_error is not None:
                        error = gateway_error()
                        if error and not previous:
                            self.emit('gateway.error', '网关出错', str(error), key=str(error)[:80])
                        previous = error
                except Exception:
                    logging.exception('notify watcher failed')
                stop.wait(interval)
        thread = threading.Thread(target=loop, name='notify-watch', daemon=True)
        thread.start()
        return stop


def _size(value):
    value = float(value or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024:
            return f'{value:.0f} {unit}' if unit == 'B' else f'{value:.1f} {unit}'
        value /= 1024
    return f'{value:.2f} TB'


def _duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f'{seconds} 秒'
    if seconds < 3600:
        return f'{seconds // 60} 分 {seconds % 60} 秒'
    return f'{seconds // 3600} 小时 {seconds % 3600 // 60} 分'
