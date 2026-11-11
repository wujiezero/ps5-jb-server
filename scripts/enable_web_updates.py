#!/usr/bin/env python3
"""Enroll an installed standard or legacy service for authenticated web upgrades.

Writes update-install.json and recovery unit/drop-ins for an installed service.
Install the new modules and UI, reload systemd and restart the existing admin
service once before using the web updater.
"""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
import tomllib


def checked_path(value):
    path = Path(value)
    if any(ord(char) < 32 for char in str(value)) or not path.is_absolute() or os.path.normpath(str(value)) != str(value):
        raise ValueError('路径必须是规范的绝对路径')
    if any(part.is_symlink() for part in (*path.parents, path)):
        raise ValueError('部署路径不能包含符号链接')
    return path


def deployment_descriptor(program, config, admin_unit, repo_unit=None, host_unit=None):
    program, config = checked_path(str(program)), checked_path(str(config))
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]*\.service', admin_unit or ''):
        raise ValueError('管理服务名称无效')
    if repo_unit is not None and (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]*\.service', repo_unit)
                                  or repo_unit == admin_unit):
        raise ValueError('仓库服务名称无效')
    if host_unit is not None and (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]*\.service', host_unit)
                                  or host_unit in (admin_unit, repo_unit)):
        raise ValueError('用户手册服务名称无效')
    with config.open('rb') as stream:
        raw = tomllib.load(stream)
    paths = raw.get('paths') or {}
    static = checked_path(paths.get('static', str(program / 'web')))
    data = checked_path(paths.get('data', '/var/lib/ps5-jb-server'))
    if program not in static.parents:
        raise ValueError('前端目录必须位于程序目录中')
    if data == program or program in data.parents or data in program.parents:
        raise ValueError('数据目录必须与程序目录分离')
    if config == program or program in config.parents:
        raise ValueError('配置必须位于程序目录之外')
    host = str(ipaddress.ip_address((raw.get('network') or {}).get('host_ip', '127.0.0.1')))
    port = (raw.get('ports') or {}).get('admin', 8443)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('管理端口无效')
    host = '[' + host + ']' if ':' in host else host
    return {'schema': 1, 'program_dir': str(program), 'static_dir': str(static), 'data_dir': str(data),
            'config_path': str(config), 'admin_unit': admin_unit, 'repo_unit': repo_unit, 'host_unit': host_unit,
            'health_url': f'https://{host}:{port}/api/version'}


def enroll(program, config, admin_unit, repo_unit=None, dry_run=False, host_unit=None):
    program = checked_path(str(program))
    required = ('server.py', 'version.py', 'updates.py', 'update_worker.py')
    if not all((program / name).is_file() and not (program / name).is_symlink() for name in required):
        raise ValueError('请先升级已安装的服务模块和页面：缺少 version.py、updates.py 或 update_worker.py')
    result = deployment_descriptor(program, config, admin_unit, repo_unit, host_unit)
    if not dry_run:
        descriptor = program / 'update-install.json'
        checked_path(str(descriptor))
        fd, temporary = tempfile.mkstemp(prefix='.update-install-', dir=program)
        try:
            with os.fdopen(fd, 'w') as stream:
                os.fchmod(stream.fileno(), 0o644)
                json.dump(result, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, descriptor)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return result


def recovery_units(descriptor):
    """Boot/start ordering repairs an interrupted transaction before app imports."""
    admin, repo = descriptor['admin_unit'], descriptor.get('repo_unit')
    recovery = admin.removesuffix('.service') + '-update-recovery.service'
    units = [name for name in (admin, repo) if name]
    def quoted(value):
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
    worker = Path(descriptor['program_dir']) / 'update_worker.py'
    deployment = Path(descriptor['program_dir']) / 'update-install.json'
    text = ('[Unit]\nDescription=Recover interrupted ps5-jb-server update\n'
            'After=local-fs.target\nBefore=' + ' '.join(units) + '\n\n'
            '[Service]\nType=oneshot\nExecStart=/usr/bin/python3 ' + quoted(worker) +
            ' --recover ' + quoted(deployment) + '\nTimeoutStartSec=120\n')
    result = {recovery: text}
    for unit in units:
        result[unit + '.d/update-recovery.conf'] = '[Unit]\nRequires=' + recovery + '\nAfter=' + recovery + '\n'
    # host.service Requires the manager and is stopped when it stops. Wants in
    # the reverse direction queues the host again when a verified update starts
    # the manager; host's After=manager retains the existing startup ordering.
    if descriptor.get('host_unit'):
        result[admin + '.d/update-recovery.conf'] += 'Wants=' + descriptor['host_unit'] + '\n'
    return result


def install_recovery_units(descriptor, systemd_dir=Path('/etc/systemd/system')):
    for name, text in recovery_units(descriptor).items():
        target = checked_path(str(systemd_dir / name))
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.update-unit-', dir=target.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                os.fchmod(stream.fileno(), 0o644)
                stream.write(text)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--program-dir', required=True, type=Path)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--admin-unit', required=True)
    parser.add_argument('--repo-unit')
    parser.add_argument('--host-unit', help='随管理服务启动的现有用户手册服务，如 ps5-host.service')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        program = checked_path(str(args.program_dir))
        if Path('/opt') not in program.parents or (program / 'server').exists():
            raise ValueError('仅允许 /opt 下已安装的平铺服务目录，不能登记源码目录')
        if os.geteuid() != 0 and not args.dry_run:
            raise ValueError('请用 sudo 运行登记命令')
        result = enroll(program, args.config, args.admin_unit, args.repo_unit, args.dry_run, args.host_unit)
        if not args.dry_run:
            install_recovery_units(result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print('登记及恢复服务安装完成。运行 systemctl daemon-reload 并重启原管理服务后，在设置中检查更新。' if not args.dry_run else '演练完成，未修改文件。')
        return 0
    except (ValueError, OSError) as exc:
        parser.exit(1, f'错误：{exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
