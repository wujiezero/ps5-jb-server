"""Runtime settings for ps5-jb-server.

Values come from a TOML file (default /etc/ps5-jb-server/config.toml) with
environment overrides, so the installer can write one file and nothing else
needs to be edited.  Every path is overridable, which is what makes the
unprivileged test install possible.
"""
import ipaddress
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULTS = {
    'network': {'host_ip': '', 'subnet': '', 'upstream_dns': ''},
    'ports': {'dns': 53, 'http': 80, 'https': 443, 'admin': 8443, 'repo': 8080},
    'ps5': {'pldmgr_port': 8084, 'elf_port': 9021, 'file_port': 80,
            'control': True, 'file_manager': True},
    'paths': {'data': '/var/lib/ps5-jb-server', 'repo': '/srv/ps5-jb-server',
              'static': '/opt/ps5-jb-server/web', 'certs': '/etc/ps5-jb-server/tls',
              'credentials': '/var/lib/ps5-jb-server/credentials.json',
              'config': '/etc/ps5-jb-server/config.toml',
              'rulesets': '/opt/ps5-jb-server/rulesets/compiled.json'},
}


@dataclass
class Settings:
    host_ip: str
    subnet: str
    upstream_dns: str = ''
    dns_port: int = 53
    http_port: int = 80
    https_port: int = 443
    admin_port: int = 8443
    repo_port: int = 8080
    pldmgr_port: int = 8084
    elf_port: int = 9021
    file_port: int = 80
    ps5_control: bool = True
    file_manager: bool = True
    max_upload: int = 64 << 20
    data_dir: Path = Path('/var/lib/ps5-jb-server')
    repo_dir: Path = Path('/srv/ps5-jb-server')
    static_dir: Path = Path('/opt/ps5-jb-server/web')
    cert_dir: Path = Path('/etc/ps5-jb-server/tls')
    credentials_path: Path = Path('/var/lib/ps5-jb-server/credentials.json')
    config_path: Path = Path('/etc/ps5-jb-server/config.toml')
    rulesets_path: Path = Path('/opt/ps5-jb-server/rulesets/compiled.json')
    systemd_prefix: str = 'ps5-jb-server'
    extra: dict = field(default_factory=dict)

    def ensure_dirs(self):
        for path in (self.data_dir, self.repo_dir / 'payloads', self.cert_dir):
            Path(path).mkdir(parents=True, exist_ok=True)

    @classmethod
    def load(cls, path=None):
        path = Path(path or os.environ.get('PS5JB_CONFIG', DEFAULTS['paths']['config']))
        raw = {}
        if path.exists():
            raw = tomllib.loads(path.read_text())
        return cls.from_mapping(raw, path=path)

    @classmethod
    def from_mapping(cls, raw, path=None):
        def pick(section, key, env, default):
            value = os.environ.get(env)
            if value not in (None, ''):
                return value
            return (raw.get(section) or {}).get(key, default)

        host_ip = pick('network', 'host_ip', 'PS5JB_HOST_IP', '') or _detect_ip()
        subnet = pick('network', 'subnet', 'PS5JB_SUBNET', '') or _subnet_of(host_ip)
        return cls(
            host_ip=host_ip,
            subnet=subnet,
            upstream_dns=str(pick('network', 'upstream_dns', 'PS5JB_UPSTREAM_DNS', '') or _resolver()),
            dns_port=int(pick('ports', 'dns', 'PS5JB_PORT_DNS', 53)),
            http_port=int(pick('ports', 'http', 'PS5JB_PORT_HTTP', 80)),
            https_port=int(pick('ports', 'https', 'PS5JB_PORT_HTTPS', 443)),
            admin_port=int(pick('ports', 'admin', 'PS5JB_PORT_ADMIN', 8443)),
            repo_port=int(pick('ports', 'repo', 'PS5JB_PORT_REPO', 8080)),
            pldmgr_port=int(pick('ps5', 'pldmgr_port', 'PS5JB_PLDMGR_PORT', 8084)),
            elf_port=int(pick('ps5', 'elf_port', 'PS5JB_ELF_PORT', 9021)),
            file_port=int(pick('ps5', 'file_port', 'PS5JB_FILE_PORT', 80)),
            ps5_control=bool(pick('ps5', 'control', 'PS5JB_PS5_CONTROL', True)),
            file_manager=bool(pick('ps5', 'file_manager', 'PS5JB_FILE_MANAGER', True)),
            max_upload=int(pick('limits', 'max_upload', 'PS5JB_MAX_UPLOAD', 64 << 20)),
            data_dir=Path(pick('paths', 'data', 'PS5JB_DATA', DEFAULTS['paths']['data'])),
            repo_dir=Path(pick('paths', 'repo', 'PS5JB_REPO', DEFAULTS['paths']['repo'])),
            static_dir=Path(pick('paths', 'static', 'PS5JB_STATIC', DEFAULTS['paths']['static'])),
            cert_dir=Path(pick('paths', 'certs', 'PS5JB_CERTS', DEFAULTS['paths']['certs'])),
            credentials_path=Path(pick('paths', 'credentials', 'PS5JB_CREDENTIALS',
                                      str(Path(pick('paths', 'data', 'PS5JB_DATA',
                                                    DEFAULTS['paths']['data'])) / 'credentials.json'))),
            config_path=Path(path or DEFAULTS['paths']['config']),
            rulesets_path=Path(pick('paths', 'rulesets', 'PS5JB_RULESETS',
                                    DEFAULTS['paths']['rulesets'])),
        )


def _detect_ip():
    """Pick the source address of the default route (no packets are sent)."""
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(('198.51.100.1', 9))
        return probe.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        probe.close()


def _subnet_of(host_ip):
    try:
        return str(ipaddress.ip_network(f'{host_ip}/24', strict=False))
    except ValueError:
        return '127.0.0.0/8'


def _resolver():
    try:
        for line in Path('/etc/resolv.conf').read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == 'nameserver':
                return parts[1]
    except OSError:
        pass
    return '1.1.1.1'
