#!/usr/bin/env python3
"""Install ps5-jb-server on a Debian/Ubuntu LXC or VM.

    sudo ./install.sh                 # install or upgrade
    sudo ./install.sh --dry-run       # print every action, change nothing
    sudo ./install.sh --uninstall     # stop services and remove installed files
    ./install.sh doctor               # report current state

Everything is parameterised: --prefix moves all paths under one directory, which
is how the installer is tested without touching a real deployment.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UNIT_FILES = ('ps5-jb-server.service', 'ps5-jb-host.service', 'ps5-jb-repo.service')
NFT_FILE = '90-ps5-jb-server.nft'
SYSCTL_FILE = '90-ps5-jb-server.conf'
PORT_NAMES = {'dns': 53, 'http': 80, 'https': 443, 'admin': 8443, 'repo': 8080}


class StepError(RuntimeError):
    pass


@dataclass
class Layout:
    prefix: Path = Path('/')
    host_ip: str = ''
    subnet: str = ''
    upstream_dns: str = ''
    ports: dict = field(default_factory=lambda: dict(PORT_NAMES))
    systemd_dir: Path = Path('/etc/systemd/system')
    access_file: Path = Path('/root/ps5-jb-server-access.txt')
    dry_run: bool = False
    no_firewall: bool = False
    no_services: bool = False
    no_payloads: bool = False
    no_manuals_host: bool = False

    # derived
    opt: Path = field(init=False)
    etc: Path = field(init=False)
    data: Path = field(init=False)
    repo: Path = field(init=False)
    certs: Path = field(init=False)
    config: Path = field(init=False)
    nft: Path = field(init=False)
    nftables_conf: Path = field(init=False)
    sysctl: Path = field(init=False)

    def __post_init__(self):
        prefix = Path(self.prefix)
        self.opt = prefix / 'opt/ps5-jb-server'
        self.etc = prefix / 'etc/ps5-jb-server'
        self.data = prefix / 'var/lib/ps5-jb-server'
        self.repo = prefix / 'srv/ps5-jb-server'
        self.certs = self.etc / 'tls'
        self.config = self.etc / 'config.toml'
        self.nft = prefix / 'etc/nftables.d' / NFT_FILE
        self.nftables_conf = prefix / 'etc/nftables.conf'
        self.sysctl = prefix / 'etc/sysctl.d' / SYSCTL_FILE
        if prefix != Path('/'):
            self.systemd_dir = prefix / 'etc/systemd/system'
            self.access_file = prefix / 'root/ps5-jb-server-access.txt'


class Runner:
    def __init__(self, layout):
        self.layout = layout
        self.actions = []

    def run(self, argv, check=True, capture=True):
        self.actions.append(argv)
        line = '  $ ' + ' '.join(str(part) for part in argv)
        if self.layout.dry_run:
            print(line + '   (dry-run)')
            return subprocess.CompletedProcess(argv, 0, '', '')
        print(line)
        return subprocess.run(argv, check=check, capture_output=capture, text=True)

    def note(self, text):
        print('  · ' + text)

    def mkdir(self, path, mode=0o755):
        self.actions.append(['mkdir', str(path)])
        if self.layout.dry_run:
            print(f'  + mkdir -p {path} ({oct(mode)})')
            return
        Path(path).mkdir(parents=True, exist_ok=True)
        os.chmod(path, mode)

    def write(self, path, content, mode=0o644):
        self.actions.append(['write', str(path)])
        if self.layout.dry_run:
            print(f'  + write {path} ({len(content)} bytes, {oct(mode)})')
            return
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_text(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)

    def copy_file(self, source, target):
        self.actions.append(['copy', str(source), str(target)])
        if self.layout.dry_run:
            print(f'  + copy {source} -> {target}')
            return
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    def copy_tree(self, source, target):
        self.actions.append(['copy', str(source), str(target)])
        if self.layout.dry_run:
            print(f'  + copy {source} -> {target}')
            return
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)


# ---------------------------------------------------------------- preflight

def preflight(layout, runner):
    if os.geteuid() != 0 and not layout.dry_run and layout.prefix == Path('/'):
        raise StepError('请用 root 运行（sudo ./install.sh）')
    if not (REPO / 'server/server.py').exists():
        raise StepError('安装包不完整：缺少 server/server.py')
    if not str(layout.host_ip or ''):
        layout.host_ip = detect_ip()
    layout.subnet = layout.subnet or subnet_of(layout.host_ip)
    layout.upstream_dns = layout.upstream_dns or resolver()
    if layout.ports['admin'] == layout.ports['repo']:
        raise StepError('管理端口与仓库端口不能相同')
    if not layout.dry_run:
        for name in ('python3', 'openssl'):
            if shutil.which(name) is None:
                raise StepError(f'缺少依赖：{name}')
        check_dnspython()
        if not layout.no_firewall:
            if shutil.which('nft') is None:
                raise StepError('缺少 nftables（或用 --no-firewall 跳过网关功能）')
        if not layout.no_services and shutil.which('systemctl') is None:
            raise StepError('缺少 systemd（或用 --no-services 只安装文件）')
    busy = [name for name, port in layout.ports.items() if port_in_use(port)]
    if busy and not layout.dry_run:
        raise StepError('端口被占用：' + '、'.join(f'{name}:{layout.ports[name]}' for name in busy)
                        + '（可用 --admin-port 等改端口）')
    runner.note(f'主机地址 {layout.host_ip}，网段 {layout.subnet}，上游 DNS {layout.upstream_dns}')


def detect_ip():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(('198.51.100.1', 9))
        return probe.getsockname()[0]
    except OSError:
        return ''
    finally:
        probe.close()


def subnet_of(host_ip):
    parts = host_ip.split('.')
    return '.'.join(parts[:3]) + '.0/24' if len(parts) == 4 else ''


def resolver():
    try:
        for line in Path('/etc/resolv.conf').read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == 'nameserver':
                return parts[1]
    except OSError:
        pass
    return '1.1.1.1'


def port_in_use(port, host='0.0.0.0'):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return False
        except OSError:
            return True


def check_dnspython():
    result = subprocess.run([sys.executable, '-c', 'import dns.message'], capture_output=True)
    if result.returncode != 0:
        raise StepError('缺少 python3-dnspython：apt-get install -y python3-dnspython')
    crypto = subprocess.run([sys.executable, '-c',
                             'from cryptography.hazmat.primitives.ciphers.aead import AESGCM'],
                            capture_output=True)
    if crypto.returncode != 0:
        raise StepError('缺少 python3-cryptography（用于加密保存 NAS 账号）：'
                        'apt-get install -y python3-cryptography')
    smb = subprocess.run([sys.executable, '-c', 'import smbc'], capture_output=True)
    if smb.returncode != 0:
        print('  提示：未安装 python3-smbc，NAS 存储将无法使用 SMB（WebDAV / FTP 不受影响）；'
              '需要时执行 apt-get install -y python3-smbc')


# ---------------------------------------------------------------- steps

def install_files(layout, runner):
    print('\n[1/9] 目录与程序文件')
    for path, mode in ((layout.opt, 0o755), (layout.opt / 'web', 0o755), (layout.opt / 'rulesets', 0o755),
                       (layout.etc, 0o700), (layout.certs, 0o700), (layout.data, 0o700),
                       (layout.repo / 'payloads', 0o755)):
        runner.mkdir(path, mode)
    for name in ('server.py', 'dns_service.py', 'policy.py', 'payload_repo.py', 'activity.py',
                 'ps5.py', 'files.py', 'api_console.py', 'settings.py'):
        runner.copy_file(REPO / 'server' / name, layout.opt / name)
    for name in ('index.html', 'app.js', 'style.css'):
        runner.copy_file(REPO / 'web' / name, layout.opt / 'web' / name)
    runner.copy_file(REPO / 'rulesets' / 'compiled.json', layout.opt / 'rulesets' / 'compiled.json')


def write_config(layout, runner):
    print('\n[2/9] 配置文件')
    if layout.config.exists() and not layout.dry_run:
        runner.note(f'{layout.config} 已存在，保留现有配置（升级模式）')
        return
    runner.write(layout.config, f'''# Generated by install.sh on {time.strftime('%Y-%m-%d %H:%M:%S')}

[network]
host_ip = "{layout.host_ip}"
subnet = "{layout.subnet}"
upstream_dns = "{layout.upstream_dns}"

[ports]
dns = {layout.ports['dns']}
http = {layout.ports['http']}
https = {layout.ports['https']}
admin = {layout.ports['admin']}
repo = {layout.ports['repo']}

[ps5]
pldmgr_port = 8084
elf_port = 9021
file_port = 80
ftp_port = 2120
control = true
file_manager = true

[limits]
max_upload = 67108864

[paths]
data = "{layout.data}"
repo = "{layout.repo}"
static = "{layout.opt / 'web'}"
certs = "{layout.certs}"
credentials = "{layout.data / 'credentials.json'}"
rulesets = "{layout.opt / 'rulesets/compiled.json'}"
''', 0o644)


def make_certificates(layout, runner):
    print('\n[3/9] 自签证书')
    targets = (('admin', 'ps5-jb-server',
                f'IP:{layout.host_ip},DNS:localhost'),
               ('manuals', 'manuals.playstation.net',
                f'DNS:manuals.playstation.net,DNS:ena.net.playstation.net,'
                f'DNS:www.msftconnecttest.com,IP:{layout.host_ip}'))
    for prefix, subject, san in targets:
        key = layout.certs / f'{prefix}.key'
        cert = layout.certs / f'{prefix}.crt'
        if key.exists() and cert.exists() and not layout.dry_run:
            runner.note(f'{cert.name} 已存在，跳过')
            continue
        runner.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-sha256',
                    '-days', '3650', '-subj', f'/CN={subject}', '-addext',
                    f'subjectAltName={san}', '-keyout', str(key), '-out', str(cert)])
        if not layout.dry_run:
            os.chmod(key, 0o600)


def make_credentials(layout, runner):
    print('\n[4/9] 管理密码')
    path = layout.data / 'credentials.json'
    if path.exists() and not layout.dry_run:
        runner.note('管理密码已存在，保留（忘记密码请看 --reset-password）')
        return
    password = secrets.token_urlsafe(12)
    salt = secrets.token_hex(16)
    iterations = 310000
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), iterations).hex()
    runner.write(path, json.dumps({'salt': salt, 'iterations': iterations,
                                   'password_hash': digest}), 0o600)
    runner.write(layout.access_file,
                 f'PS5 管理页面：https://{layout.host_ip}:{layout.ports["admin"]}\n'
                 f'仅需密码登录，没有用户名。\n初始管理密码：{password}\n'
                 f'（忘记后可用 ./install.sh --reset-password 重设）\n', 0o600)
    runner.note(f'初始密码已写入 {layout.access_file}（0600）')


def make_nas_key(layout, runner):
    """AES-256 key for NAS credentials, kept beside config.toml (not in the data dir)."""
    path = layout.etc / 'nas.key'
    if path.exists() and not layout.dry_run:
        runner.note(f'{path} 已存在，保留（更换会导致已保存的 NAS 账号无法解密）')
        return
    runner.write(path, base64.b64encode(secrets.token_bytes(32)).decode() + '\n', 0o600)
    runner.note(f'已生成 NAS 凭据加密密钥 {path}（0600）')


def fetch_components(layout, runner):
    print('\n[5/9] 第三方组件')
    manifest = json.loads((REPO / 'components.json').read_text())
    if layout.no_manuals_host:
        runner.note('跳过用户手册宿主（--no-manuals-host）')
    else:
        for item in manifest['components']:
            target = layout.opt / item['filename']
            if target.exists() and sha256(target) == item['sha256'] and not layout.dry_run:
                runner.note(f'{item["filename"]} 已是最新')
                continue
            if layout.dry_run:
                print(f'  + download {item["url"]} -> {target}')
                continue
            download(item['url'], target, item['sha256'], runner)

    catalog = layout.repo / 'repo.json'
    if layout.no_payloads:
        runner.note('跳过插件下载（--no-payloads）')
    else:
        base = f'http://{layout.host_ip}:{layout.ports["repo"]}/payloads/'
        runner.run([sys.executable, str(REPO / 'scripts/fetch_payloads.py'),
                    '--repo-dir', str(layout.repo), '--base-url', base], check=False)
    if not catalog.exists() and not layout.dry_run:
        runner.write(catalog, json.dumps({'name': 'ps5-jb-server', 'payloads': []},
                                         ensure_ascii=False, indent=1) + '\n', 0o644)
        runner.note('插件仓库为空（稍后可运行 scripts/fetch_payloads.py 填充）')


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def download(url, target, expected, runner):
    print(f'  downloading {url}')
    request = urllib.request.Request(url, headers={'User-Agent': 'ps5-jb-server-installer'})
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix('.part')
    try:
        with urllib.request.urlopen(request, timeout=180) as response, tmp.open('wb') as handle:
            for chunk in iter(lambda: response.read(1 << 20), b''):
                handle.write(chunk)
        if expected and sha256(tmp) != expected:
            raise StepError(f'{target.name} 校验失败（SHA-256 不匹配）')
        os.replace(tmp, target)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def write_firewall(layout, runner):
    print('\n[6/9] 基线防火墙与内核参数')
    if layout.no_firewall:
        runner.note('已跳过（--no-firewall）：不会有网关策略，仅保留 DNS 与界面')
        return
    sys.path.insert(0, str(layout.opt))
    from policy import Firewall
    baseline = Firewall(host_ip=layout.host_ip, lan=layout.subnet, interface=interface_name()).render(
        {'domains': [], 'networks': [], 'upstream_dns': layout.upstream_dns}, {})
    runner.write(layout.nft, baseline, 0o644)
    text = layout.nftables_conf.read_text() if layout.nftables_conf.exists() else '#!/usr/sbin/nft -f\n'
    include = f'include "{layout.nft}"'.replace(str(layout.prefix), '') if layout.prefix != Path('/') \
        else f'include "{layout.nft}"'
    if include not in text:
        text = text.rstrip('\n') + '\n\n# ps5-jb-server baseline: default-deny until the manager starts\n' + include + '\n'
        runner.write(layout.nftables_conf, text, 0o644)
    runner.write(layout.sysctl, f'''# ps5-jb-server: IPv4 gateway stays closed until a valid ruleset is loaded.
net.ipv4.ip_forward=0
net.ipv4.conf.all.send_redirects=0
net.ipv4.conf.default.send_redirects=0
net.ipv4.conf.{interface_name()}.send_redirects=0
net.ipv6.conf.all.forwarding=0
''', 0o644)
    runner.run(['nft', '-c', '-f', str(layout.nftables_conf)], check=False)
    runner.run(['sysctl', '-w', 'net.ipv4.ip_forward=0'], check=False)


def interface_name():
    result = subprocess.run(['sh', '-c', "ip -4 route show default 2>/dev/null | awk '{print $5; exit}'"],
                            capture_output=True, text=True)
    name = (result.stdout or '').strip()
    return name if re.fullmatch(r'[a-zA-Z0-9_.:-]{1,15}', name or '') else 'eth0'


def install_units(layout, runner):
    print('\n[7/9] systemd 服务')
    if layout.no_services:
        runner.note('已跳过（--no-services）')
        return
    render = {
        '@OPT@': str(layout.opt), '@CONFIG@': str(layout.config),
        '@DATA@': str(layout.data), '@REPO@': str(layout.repo),
        '@HOST_IP@': layout.host_ip, '@HTTP_PORT@': str(layout.ports['http']),
        '@REPO_PORT@': str(layout.ports['repo']), '@CERTS@': str(layout.certs),
    }
    for unit in UNIT_FILES:
        text = (REPO / 'systemd' / unit).read_text()
        for key, value in render.items():
            text = text.replace(key, value)
        runner.write(layout.systemd_dir / unit, text, 0o644)
    runner.run(['systemctl', 'daemon-reload'], check=False)
    runner.run(['systemctl', 'enable', '--now', 'ps5-jb-server.service'], check=False)
    runner.run(['systemctl', 'enable', '--now', 'ps5-jb-host.service'], check=False)
    runner.run(['systemctl', 'enable', '--now', 'ps5-jb-repo.service'], check=False)


def health(layout, runner):
    print('\n[8/9] 健康检查')
    if layout.dry_run or layout.no_services:
        runner.note('跳过（dry-run 或 --no-services）')
        return True
    checks = [
        ('管理页面', f'https://{layout.host_ip}:{layout.ports["admin"]}/', 401),
        ('插件仓库', f'http://{layout.host_ip}:{layout.ports["repo"]}/repo.json', 200),
        ('用户手册', f'https://{layout.host_ip}:{layout.ports["https"]}/document/ps5/index.html', 200),
    ]
    ok = True
    for name, url, expected in checks:
        code = http_code(url)
        good = code in (expected, 200) if expected != 401 else code in (200, 401)
        print(f'  {"OK " if good else "FAIL"} {name}: HTTP {code}')
        ok = ok and good
    if not ok:
        print('  某些检查未通过，请查看 journalctl -u ps5-jb-server -n 50')
    return ok


def http_code(url):
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(url, timeout=6, context=ctx) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:
        print(f'  (请求失败: {exc})')
        return 0


def summary(layout):
    print(f'''
[9/9] 完成

  管理页面   https://{layout.host_ip}:{layout.ports['admin']}/
  用户手册   PS5 的 DNS 指向 {layout.host_ip} 后打开「用户手册」
  插件仓库   http://{layout.host_ip}:{layout.ports['repo']}/repo.json
  配置文件   {layout.config}
  初始密码   {layout.access_file}
  插件目录   {layout.repo / 'payloads'}

  PS5 设置：默认网关与首选 DNS 都填 {layout.host_ip}，本机 IP 保持自动获取。
  服务状态：systemctl status ps5-jb-server ps5-jb-host ps5-jb-repo
''')


# ---------------------------------------------------------------- commands

def cmd_install(layout):
    runner = Runner(layout)
    preflight(layout, runner)
    steps = [install_files, write_config, make_certificates, make_credentials, make_nas_key,
             fetch_components, write_firewall, install_units]
    for step in steps:
        step(layout, runner)
    health(layout, runner)
    summary(layout)


def cmd_uninstall(layout, purge=False):
    runner = Runner(layout)
    print('停止并禁用服务')
    for unit in UNIT_FILES:
        runner.run(['systemctl', 'disable', '--now', unit], check=False)
    for unit in UNIT_FILES:
        path = layout.systemd_dir / unit
        if path.exists():
            runner.run(['rm', '-f', str(path)], check=False)
    runner.run(['systemctl', 'daemon-reload'], check=False)
    if layout.nftables_conf.exists() and not layout.dry_run:
        text = layout.nftables_conf.read_text()
        include = f'include "{layout.nft}"'
        if include in text:
            runner.write(layout.nftables_conf,
                         '\n'.join(line for line in text.splitlines() if include not in line) + '\n')
    for path in (layout.nft, layout.sysctl):
        if path.exists():
            runner.run(['rm', '-f', str(path)], check=False)
    runner.run(['sysctl', '-w', 'net.ipv4.ip_forward=0'], check=False)
    if purge:
        for path in (layout.opt, layout.data, layout.repo, layout.etc,
                     layout.access_file):
            if path.exists():
                runner.run(['rm', '-rf', str(path)], check=False)
        # Drop directories we created that are now empty (prefix installs).
        if layout.prefix != Path('/'):
            for parent in ('opt', 'var/lib', 'var', 'srv', 'etc/tls', 'etc', 'root'):
                target = layout.prefix / parent
                if target.is_dir() and not any(target.iterdir()):
                    runner.run(['rmdir', str(target)], check=False)
        print('数据目录已删除（--purge）')
    else:
        print(f'保留数据：{layout.data}、{layout.repo}、{layout.etc}（--purge 可一并删除）')


def cmd_doctor(layout):
    print('ps5-jb-server doctor\n')
    print(f'  仓库副本     {REPO}')
    print(f'  安装目录     {layout.opt} {"存在" if layout.opt.exists() else "缺失"}')
    print(f'  配置文件     {layout.config} {"存在" if layout.config.exists() else "缺失"}')
    if layout.config.exists():
        sys.path.insert(0, str(REPO / 'server'))
        from settings import Settings
        settings = Settings.load(layout.config)
        print(f'  主机/网段    {settings.host_ip} / {settings.subnet}')
        print(f'  端口         dns={settings.dns_port} http={settings.http_port} '
              f'https={settings.https_port} admin={settings.admin_port} repo={settings.repo_port}')
        layout.ports = {'dns': settings.dns_port, 'http': settings.http_port,
                        'https': settings.https_port, 'admin': settings.admin_port,
                        'repo': settings.repo_port}
        print(f'  数据目录     {settings.data_dir} {"存在" if settings.data_dir.exists() else "缺失"}')
        print(f'  插件仓库     {settings.repo_dir} / repo.json '
              f'{"存在" if (settings.repo_dir / "repo.json").exists() else "缺失"}')
    for name in PORT_NAMES:
        port = layout.ports[name]
        print(f'  端口 {name:<6} {port:<6} {"占用" if port_in_use(port) else "空闲"}')
    if shutil.which('systemctl'):
        for unit in UNIT_FILES:
            state = subprocess.run(['systemctl', 'is-active', unit], capture_output=True, text=True)
            enabled = subprocess.run(['systemctl', 'is-enabled', unit], capture_output=True, text=True)
            print(f'  {unit:<26} {enabled.stdout.strip() or "未安装":<10} {state.stdout.strip() or "-"}')


def cmd_reset_password(layout):
    runner = Runner(layout)
    password = secrets.token_urlsafe(12)
    salt = secrets.token_hex(16)
    iterations = 310000
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), iterations).hex()
    runner.write(layout.data / 'credentials.json',
                 json.dumps({'salt': salt, 'iterations': iterations, 'password_hash': digest}), 0o600)
    runner.write(layout.access_file,
                 f'PS5 管理页面：https://{layout.host_ip}:{layout.ports["admin"]}\n'
                 f'仅需密码登录，没有用户名。\n管理密码：{password}\n', 0o600)
    print(f'新密码已写入 {layout.access_file}')
    if not layout.no_services:
        Runner(layout).run(['systemctl', 'restart', 'ps5-jb-server.service'], check=False)


def build_layout(args):
    layout = Layout(
        prefix=Path(args.prefix), host_ip=args.host_ip or '', subnet=args.subnet or '',
        upstream_dns=args.upstream_dns or '',
        ports={'dns': args.dns_port, 'http': args.http_port, 'https': args.https_port,
               'admin': args.admin_port, 'repo': args.repo_port},
        dry_run=args.dry_run, no_firewall=args.no_firewall, no_services=args.no_services,
        no_payloads=args.no_payloads, no_manuals_host=args.no_manuals_host,
    )
    return layout


def main(argv=None):
    parser = argparse.ArgumentParser(prog='install.sh', description='ps5-jb-server installer')
    parser.add_argument('command', nargs='?', default='install',
                        choices=['install', 'upgrade', 'uninstall', 'doctor', 'reset-password'])
    parser.add_argument('--prefix', default='/', help='install everything under this directory (testing)')
    parser.add_argument('--host-ip', default='')
    parser.add_argument('--subnet', default='')
    parser.add_argument('--upstream-dns', default='')
    parser.add_argument('--dns-port', type=int, default=PORT_NAMES['dns'])
    parser.add_argument('--http-port', type=int, default=PORT_NAMES['http'])
    parser.add_argument('--https-port', type=int, default=PORT_NAMES['https'])
    parser.add_argument('--admin-port', type=int, default=PORT_NAMES['admin'])
    parser.add_argument('--repo-port', type=int, default=PORT_NAMES['repo'])
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--no-firewall', action='store_true')
    parser.add_argument('--no-services', action='store_true')
    parser.add_argument('--no-payloads', action='store_true')
    parser.add_argument('--no-manuals-host', action='store_true')
    parser.add_argument('--purge', action='store_true')
    args = parser.parse_args(argv)

    layout = build_layout(args)
    try:
        if args.command in ('install', 'upgrade'):
            cmd_install(layout)
        elif args.command == 'uninstall':
            cmd_uninstall(layout, purge=args.purge)
        elif args.command == 'doctor':
            cmd_doctor(layout)
        elif args.command == 'reset-password':
            cmd_reset_password(layout)
    except StepError as exc:
        print(f'\n错误：{exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
