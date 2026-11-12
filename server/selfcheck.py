"""Self-check: is everything the PS5 needs actually working?

run(ctx) runs every check in its own thread with its own timeout; one check
that hangs or raises only fails itself, and the whole run is bounded. Each
result is {id, title, status, detail, fix} with status ok / warn / fail / skip.
The texts never include passwords, tokens or keys (the proxy URL is masked).

Side effects (DNS queries, TLS handshakes, subprocesses, files) go through
Probes so the tests can replace them; the installer's doctor reuses the checks
that do not need the running service.
"""
import concurrent.futures
import datetime
import ipaddress
import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from policy import LOCAL_DOMAINS

STATUSES = ('ok', 'warn', 'fail', 'skip')
TOTAL_TIMEOUT = 10.0
CERT_WARN_DAYS = 30
FREE_WARN = 200 << 20
MANUALS_SNI = 'manuals.playstation.net'


class Probes:
    """Everything a check touches outside this process."""

    def __init__(self, runner=subprocess.run):
        self.runner = runner
        self._flows = None
        self._lock = threading.Lock()

    def dns_query(self, server, port, name, timeout=2.0):
        import dns.message
        import dns.query
        import dns.rdatatype
        query = dns.message.make_query(name, 'A')
        reply = dns.query.udp(query, str(server), port=int(port), timeout=timeout)
        return [item.address for rrset in reply.answer if rrset.rdtype == dns.rdatatype.A for item in rrset]

    def peer_cert(self, host, port, sni, timeout=2.5):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, int(port)), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=sni) as connection:
                return connection.getpeercert(binary_form=True)

    def http_get(self, url, timeout=2.5, limit=4 << 20):
        # The local repository is always reached directly, never through a proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as response:
            return response.read(limit)

    def read_text(self, path):
        return Path(path).read_text(encoding='utf-8', errors='replace')

    def read_bytes(self, path):
        return Path(path).read_bytes()

    def run(self, args, timeout=2.5):
        """(exit code, stdout); a missing program is exit code 127."""
        try:
            result = self.runner(args, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            return 127, ''
        return result.returncode, result.stdout

    def flows(self):
        """conntrack -L, parsed once per run (two checks use it)."""
        with self._lock:
            if self._flows is None:
                from activity import parse_conntrack
                code, out = self.run(['conntrack', '-L'], timeout=2.5)
                self._flows = parse_conntrack(out) if code == 0 else None
            return self._flows

    def disk_free(self, path):
        return shutil.disk_usage(path).free

    def writable(self, path):
        return os.access(path, os.W_OK)

    def modules(self):
        found = {}
        try:
            import archive
            found['libarchive'] = archive.available()
        except ImportError:
            found['libarchive'] = False
        try:
            import unrar
            found['unrar'] = unrar.available()
        except ImportError:
            found['unrar'] = False
        try:
            import smbc  # noqa: F401
            found['smb'] = True
        except ImportError:
            found['smb'] = False
        return found

    def engine_version(self, url):
        from power import EngineClient
        return EngineClient(url, timeout=2).version()

    def proxy(self):
        import netproxy
        return netproxy.status()


@dataclass
class Context:
    settings: object
    engine: object = None
    console: object = None
    repo: object = None
    units: object = None           # callable -> {role: {name, state}}
    probes: Probes = field(default_factory=Probes)

    def get(self, name, default=None):
        return getattr(self.settings, name, default)


@dataclass
class Check:
    id: str
    title: str
    func: object
    timeout: float = 3.0
    offline: bool = False          # usable by the installer without the running service


def result(status, detail, fix=''):
    return {'status': status, 'detail': detail, 'fix': fix}


def _worst(statuses):
    for status in ('fail', 'warn', 'skip', 'ok'):
        if status in statuses:
            return status
    return 'ok'


# ---------------------------------------------------------------- checks ---

def check_dns(ctx):
    host, port = ctx.get('host_ip'), ctx.get('dns_port', 53)
    wrong = []
    for name in LOCAL_DOMAINS:
        try:
            answers = ctx.probes.dns_query(host, port, name, timeout=2)
        except Exception as exc:
            wrong.append(f'{name}（无应答：{exc.__class__.__name__}）')
            continue
        if host not in answers:
            wrong.append(f'{name}（应答 {", ".join(answers) or "空"}）')
    if wrong:
        return result('fail', '内置域名没有解析到本机：' + '；'.join(wrong),
                      f'确认管理服务在运行，{port} 端口没有被 systemd-resolved、dnsmasq 等占用。')
    return result('ok', f'{len(LOCAL_DOMAINS)} 个内置域名都解析到本机 {host}')


def _cert_days(der_or_pem, now):
    from cryptography import x509
    if der_or_pem.lstrip().startswith(b'-----BEGIN'):
        cert = x509.load_pem_x509_certificate(der_or_pem)
    else:
        cert = x509.load_der_x509_certificate(der_or_pem)
    expires = getattr(cert, 'not_valid_after_utc', None) or cert.not_valid_after.replace(tzinfo=datetime.timezone.utc)
    try:
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        names = []
    return (expires - now).days, names


def _cert_status(days):
    if days < 0:
        return 'fail'
    return 'warn' if days < CERT_WARN_DAYS else 'ok'


def check_certificates(ctx):
    now = datetime.datetime.now(datetime.timezone.utc)
    parts, statuses, fixes = [], [], []
    certs = Path(ctx.get('cert_dir', '/etc/ps5-jb-server/tls'))
    try:
        der = ctx.probes.peer_cert(ctx.get('host_ip'), ctx.get('https_port', 443), MANUALS_SNI)
        days, names = _cert_days(der, now)
        status = _cert_status(days)
        if MANUALS_SNI not in names:
            status = 'fail'
            parts.append(f'用户手册证书不含 {MANUALS_SNI}')
        else:
            parts.append(f'用户手册证书 {days} 天后到期' if days >= 0 else '用户手册证书已过期')
        statuses.append(status)
        if status != 'ok':
            fixes.append(f'删除 {certs}/manuals.crt 与 manuals.key 后运行 sudo ./install.sh upgrade 重新签发')
    except Exception as exc:
        statuses.append('fail')
        parts.append(f'用户手册站点（{ctx.get("https_port", 443)} 端口）连不上：{exc.__class__.__name__}')
        fixes.append('检查用户手册服务 ps5-jb-host 是否在运行')
    try:
        days, _ = _cert_days(ctx.probes.read_bytes(certs / 'admin.crt'), now)
        statuses.append(_cert_status(days))
        parts.append(f'管理页证书 {days} 天后到期' if days >= 0 else '管理页证书已过期')
        if statuses[-1] != 'ok':
            fixes.append(f'删除 {certs}/admin.crt 与 admin.key 后运行 sudo ./install.sh upgrade 重新签发')
    except OSError:
        statuses.append('skip')
        parts.append('管理页证书文件读不到')
    return result(_worst(statuses), '；'.join(parts), '；'.join(fixes))


def check_repo(ctx):
    url = f'http://{ctx.get("host_ip")}:{ctx.get("repo_port", 8080)}/repo.json'
    try:
        document = json.loads(ctx.probes.http_get(url))
        count = len(document.get('payloads') or [])
    except Exception as exc:
        return result('fail', f'{url} 读不到：{exc.__class__.__name__}', '检查插件仓库服务 ps5-jb-repo 是否在运行')
    missing = []
    if ctx.repo is not None:
        missing = [item['filename'] for item in ctx.repo.summary().get('items', [])
                   if item.get('missing') and item.get('filename') != getattr(ctx.repo, '_SEED_FILE', '')]
    if missing:
        return result('warn', f'{count} 个条目，其中 {len(missing)} 个缺文件：' + '、'.join(missing[:5]),
                      '在插件仓库里重新上传或删除这些条目')
    return result('ok', f'{url} 正常，{count} 个条目')


def check_gateway(ctx):
    probes, problems, notes = ctx.probes, [], []
    try:
        if probes.read_text('/proc/sys/net/ipv4/ip_forward').strip() != '1':
            problems.append('IPv4 转发没有开启')
    except OSError:
        problems.append('读不到 IPv4 转发状态')
    code, _ = probes.run(['nft', 'list', 'table', 'inet', 'ps5guard'])
    if code != 0:
        problems.append('ps5guard 规则表不存在')
    error = getattr(ctx.engine, 'gateway_error', None)
    if error:
        problems.append(f'网关错误：{error}')
    interface = ctx.get('interface') or getattr(getattr(ctx.engine, 'firewall', None), 'interface', '')
    if interface:
        code, out = probes.run(['ip', '-4', '-o', 'addr', 'show', 'dev', interface])
        if code != 0:
            problems.append(f'网卡 {interface} 不存在')
        elif f' {ctx.get("host_ip")}/' not in out:
            problems.append(f'网卡 {interface} 不持有本机地址 {ctx.get("host_ip")}')
        else:
            notes.append(f'网卡 {interface}')
    try:
        if probes.read_text('/proc/sys/net/ipv6/conf/all/forwarding').strip() == '1':
            problems.append('IPv6 转发开着，IPv6 流量不受策略控制')
    except OSError:
        pass
    if problems:
        return result('fail', '；'.join(problems),
                      '查看「设置 → 运行日志」里的网关错误；网卡不对时在 config.toml 的 [network] interface 指定后重启服务。')
    return result('ok', '，'.join(['转发已开启', '规则表 ps5guard 已加载'] + notes))


def check_upstream(ctx):
    store = getattr(ctx.engine, 'store', None)
    upstream = store.snapshot().get('upstream_dns') if store is not None else ctx.get('upstream_dns')
    if not upstream:
        return result('skip', '没有配置上游 DNS')
    try:
        answers = ctx.probes.dns_query(upstream, 53, 'example.com', timeout=2.5)
    except Exception as exc:
        return result('fail', f'上游 {upstream} 没有应答：{exc.__class__.__name__}',
                      '在「访问策略」里换一个上游 DNS（家庭路由器或公共 DNS），并确认本机能访问它。')
    if not answers:
        return result('warn', f'上游 {upstream} 有应答但没有地址', '换一个上游 DNS 试试')
    return result('ok', f'上游 {upstream} 应答正常')


def check_conntrack(ctx):
    code, out = ctx.probes.run(['conntrack', '-C'])
    if code == 127:
        return result('fail', '没有 conntrack 命令', '安装：sudo apt install conntrack（「网络活动」和下面的 PS5 指向检查依赖它）')
    if code != 0:
        return result('fail', 'conntrack 读不到连接表（需要 root 与 nf_conntrack 模块）', '确认服务以 root 运行')
    return result('ok', f'可用，当前 {out.strip() or "0"} 条连接')


def _known_ps5(ctx):
    discovery = getattr(getattr(ctx.console, 'ps5', None), 'discovery', None)
    if discovery is None:
        return []
    hosts = list(getattr(discovery, 'known', {}) or {})
    for host in (getattr(discovery, 'cached', {}) or {}).get('hosts', []):
        if host.get('ip') and (host.get('elfldr') or host.get('pldmgr') or host.get('loader') or host.get('power')):
            hosts.append(host['ip'])
    return list(dict.fromkeys(hosts))[:16]


def check_ps5_routes(ctx):
    hosts = _known_ps5(ctx)
    if not hosts:
        return result('skip', '还没有发现过 PS5')
    asked = {str(row.get('client')) for row in list(getattr(ctx.engine, 'logs', []) or [])}
    flows = ctx.probes.flows()
    lan = ipaddress.ip_network(ctx.get('subnet') or '0.0.0.0/32', strict=False)
    outbound = set()
    for flow in flows or []:
        try:
            if ipaddress.IPv4Address(flow['dst']) not in lan:
                outbound.add(flow['src'])
        except ValueError:
            continue
    lines, statuses, fixes = [], [], set()
    host_ip = ctx.get('host_ip')
    for ip in hosts:
        dns_ok, gw_ok = ip in asked, (ip in outbound) if flows is not None else None
        if dns_ok and gw_ok is not False:
            statuses.append('ok')
            lines.append(f'{ip}：DNS 与网关都指向本机' if gw_ok else f'{ip}：DNS 指向本机')
        elif dns_ok:
            statuses.append('warn')
            lines.append(f'{ip}：DNS 指向本机，但没看到经过本机的出网连接')
            fixes.add(f'PS5 的默认网关可能没指向本机：设置 → 网络 → 设定互联网连接 → 高级设置，默认网关填 {host_ip}。')
        elif gw_ok:
            statuses.append('warn')
            lines.append(f'{ip}：有经过本机的连接，但最近没有 DNS 查询')
            fixes.add(f'PS5 的主 DNS 可能没指向本机：高级设置里主 DNS 填 {host_ip}，次 DNS 留空。')
        else:
            statuses.append('warn')
            lines.append(f'{ip}：最近没有看到它的 DNS 查询和连接')
            fixes.add('PS5 可能没开机或处于待机；开机后再检查。一直如此时按概览「PS5 网络设置」核对。')
    if flows is None:
        lines.append('（conntrack 不可用，无法判断网关）')
    return result(_worst(statuses), '；'.join(lines), ' '.join(sorted(fixes)))


def check_ipv6(ctx):
    code, out = ctx.probes.run(['ip', '-6', 'route', 'show', 'default'])
    if code == 127:
        return result('skip', '没有 ip 命令，无法判断')
    if out.strip():
        return result('warn', '本机看到 IPv6 默认路由：局域网可能有 IPv6 出网，PS5 可能绕过本机策略直接上网。',
                      '在家庭路由器上关闭 IPv6（或关闭给 PS5 所在网段的 IPv6 地址分配）。这是启发式判断，不确定时以路由器设置为准。')
    return result('ok', '没有 IPv6 默认路由')


def check_components(ctx):
    found = ctx.probes.modules()
    engine_url = ctx.get('engine_url', 'http://127.0.0.1:19113')
    try:
        found['engine'] = bool(ctx.probes.engine_version(engine_url))
    except Exception:
        found['engine'] = False
    labels = (('libarchive', '解压引擎', 'install.sh --with-extract'),
              ('unrar', 'unrar 引擎', 'install.sh --with-extract（另需 libunrar5、rclone、fuse3）'),
              ('smb', 'SMB', 'sudo apt install python3-smbc'),
              ('engine', '电源引擎', 'install.sh --with-power-engine'))
    parts = [f'{label}{"可用" if found.get(key) else "未安装"}' for key, label, _ in labels]
    fixes = [f'{label}：{how}' for key, label, how in labels if not found.get(key)]
    status = 'ok' if not fixes else 'skip'
    try:
        proxy = ctx.probes.proxy()
        if proxy.get('error'):
            status = 'warn'
            parts.append(f'代理配置不可用：{proxy["error"]}')
        else:
            parts.append(f'代理 {proxy["url"]}' if proxy.get('configured') else '代理未设置')
    except Exception:
        pass
    return result(status, '，'.join(parts), '；'.join(fixes))


def check_resources(ctx):
    data = Path(ctx.get('data_dir', '/var/lib/ps5-jb-server'))
    parts, statuses, fixes = [], [], []
    try:
        free = ctx.probes.disk_free(data)
        parts.append(f'数据目录剩余 {free / (1 << 30):.1f} GB')
        if free < FREE_WARN:
            statuses.append('warn')
            fixes.append('数据目录剩余空间不足 200 MB：清理旧的升级备份（<data>/updates/backups）或扩容')
        else:
            statuses.append('ok')
    except OSError:
        statuses.append('warn')
        parts.append('读不到数据目录剩余空间')
    logs = data / 'logs'
    if ctx.probes.writable(logs):
        statuses.append('ok')
        parts.append('日志目录可写')
    else:
        statuses.append('warn')
        parts.append('日志目录不可写')
        fixes.append(f'检查 {logs} 的权限')
    if ctx.units is not None:
        try:
            units = ctx.units()
            down = [f'{item["name"]}={item["state"]}' for item in units.values() if item.get('state') != 'active']
            if down:
                statuses.append('fail')
                parts.append('服务未运行：' + '、'.join(down))
                fixes.append('用 systemctl status <服务名> 与 journalctl -u <服务名> 查看原因')
            else:
                parts.append(f'{len(units)} 个系统服务运行中')
        except Exception:
            parts.append('读不到系统服务状态')
    return result(_worst(statuses), '，'.join(parts), '；'.join(fixes))


CHECKS = (
    Check('dns', 'DNS 劫持', check_dns),
    Check('certificates', '证书与用户手册站点', check_certificates),
    Check('repo', '插件仓库', check_repo),
    Check('gateway', '网关', check_gateway),
    Check('upstream', '上游 DNS', check_upstream),
    Check('conntrack', 'conntrack', check_conntrack, offline=True),
    Check('ps5', 'PS5 网络指向', check_ps5_routes),
    Check('ipv6', 'IPv6 绕过风险', check_ipv6, offline=True),
    Check('components', '可选组件', check_components, offline=True),
    Check('resources', '资源与服务', check_resources, offline=True),
)


def run(ctx, checks=CHECKS, total=TOTAL_TIMEOUT, clock=time.monotonic):
    """Run checks concurrently; every one returns a result, even when it hangs."""
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(checks)),
                                                 thread_name_prefix='selfcheck')
    started = clock()
    futures = [(check, pool.submit(check.func, ctx)) for check in checks]
    results = []
    try:
        for check, future in futures:
            wait = max(0.0, min(check.timeout - (clock() - started), total - (clock() - started)))
            try:
                outcome = future.result(timeout=wait)
                if not (isinstance(outcome, dict) and outcome.get('status') in STATUSES):
                    raise ValueError('检查返回格式不正确')
            except concurrent.futures.TimeoutError:
                outcome = result('fail', f'检查超时（超过 {check.timeout:g} 秒没有结果）')
            except Exception as exc:
                outcome = result('fail', f'检查出错：{exc.__class__.__name__}: {str(exc)[:160]}')
            results.append({'id': check.id, 'title': check.title, **outcome})
    finally:
        # A hung probe keeps its thread; never wait for it here.
        pool.shutdown(wait=False, cancel_futures=True)
    return results


def summary(results):
    counts = {status: sum(1 for item in results if item['status'] == status) for status in STATUSES}
    line = ' · '.join(f'{status} {count}' for status, count in counts.items() if count)
    problems = [f'{item["title"]}：{item["detail"]}' for item in results if item['status'] in ('fail', 'warn')]
    return line, problems


class SelfCheck:
    """Runs one self-check at a time and keeps the latest result in memory."""

    def __init__(self, context_factory, clock=time.time):
        self.context_factory = context_factory
        self.clock = clock
        self.lock = threading.Lock()
        self.last = None

    def run(self):
        if not self.lock.acquire(blocking=False):
            raise RuntimeError('自检正在进行，请稍候')
        try:
            results = run(self.context_factory())
            self.last = {'time': datetime.datetime.fromtimestamp(self.clock()).astimezone().isoformat(timespec='seconds'),
                         'results': results}
            return self.last
        finally:
            self.lock.release()

    def report_lines(self):
        if not self.last:
            return ['自检：本次启动后未运行']
        line, problems = summary(self.last['results'])
        return [f'自检（{self.last["time"]}）：{line}'] + [f'  {problem}' for problem in problems]
