#!/usr/bin/env python3
"""Verified, rollback-capable updater, launched outside the admin service cgroup.

CLI: python3 <fixed installed update_worker.py> <absolute spec.json>
The installed root-owned update-install.json authorizes paths, units and the local
health endpoint. No module from the release is imported by this worker. Tests may
call run(spec, hooks) with sandbox paths and service/health/replace callables.
"""
import ast
from contextlib import contextmanager
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import uuid

INSTALL_ROOT = Path('/opt')
MAX_FILES = 5000
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
DESCRIPTOR_FIELDS = ('program_dir', 'static_dir', 'data_dir', 'config_path',
                     'admin_unit', 'repo_unit', 'health_url')
VERSION_RE = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z')
UNIT_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@-]*\.service\Z')
RUNTIME_FILES = frozenset(('VERSION', 'web/index.html', 'web/app.js', 'web/style.css',
                           'rulesets/compiled.json', 'titledb/titles.json',
                           'titledb/UPSTREAM_LICENSE'))
REQUIRED_FILES = frozenset(('server/server.py', 'server/update_worker.py',
                            'server/version.py', 'web/index.html', 'web/app.js',
                            'web/style.css', 'VERSION'))


def _no_symlinks(path):
    for part in (*reversed(path.parents), path):
        if part.is_symlink():
            raise ValueError(f'symlink path is forbidden: {part}')
        if part != path and part.exists() and not part.is_dir():
            raise ValueError(f'path parent is not a directory: {part}')


def _path(value):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError('paths must be absolute strings')
    path = Path(value)
    if not path.is_absolute() or os.path.normpath(value) != value or '..' in path.parts:
        raise ValueError(f'path must be absolute and normalized: {value}')
    _no_symlinks(path)
    return path


def _within(path, parent):
    return path != parent and parent in path.parents


def _validate_spec(spec):
    if not isinstance(spec, dict) or type(spec.get('schema')) is not int or spec['schema'] != 1:
        raise ValueError('unsupported update schema')
    if not isinstance(spec.get('version'), str) or not VERSION_RE.fullmatch(spec['version']):
        raise ValueError('version must be strict X.Y.Z')
    if not isinstance(spec.get('sha256'), str) or not re.fullmatch('[0-9a-fA-F]{64}', spec['sha256']):
        raise ValueError('SHA-256 must be 64 hexadecimal characters')
    paths = {key: _path(spec.get(key)) for key in ('program_dir', 'static_dir', 'data_dir',
                                                 'config_path', 'archive', 'status_path')}
    for key in ('program_dir', 'static_dir', 'data_dir'):
        if not paths[key].is_dir():
            raise ValueError(f'{key} must be an existing directory')
    program, web, data = (paths[key] for key in ('program_dir', 'static_dir', 'data_dir'))
    if web == program or _within(program, web):
        raise ValueError('static_dir must not contain program_dir')
    if any(a == b or _within(a, b) or _within(b, a) for a, b in ((program, data), (web, data))):
        raise ValueError('data_dir must be separate from runtime directories')
    updates = data / 'updates'
    for key in ('archive', 'status_path'):
        if not _within(paths[key], updates):
            raise ValueError(f'{key} must be within data_dir/updates')
    if paths['archive'] == paths['status_path'] or paths['status_path'] == paths['config_path']:
        raise ValueError('update artifacts must be separate files')
    for key in ('admin_unit', 'repo_unit'):
        unit = spec.get(key)
        if key == 'repo_unit' and unit is None:
            continue
        if not isinstance(unit, str) or not UNIT_RE.fullmatch(unit):
            raise ValueError(f'invalid {key}')
    if spec.get('repo_unit') == spec['admin_unit']:
        raise ValueError('admin and repository units must be distinct')
    url = spec.get('health_url')
    if not isinstance(url, str):
        raise ValueError('invalid health_url')
    parsed = urllib.parse.urlsplit(url)
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        raise ValueError('health_url host must be an IP literal') from None
    host = f'[{address}]' if address.version == 6 else str(address)
    if (parsed.scheme != 'https' or address.is_unspecified or address.is_multicast or not parsed.port
            or parsed.netloc != f'{host}:{parsed.port}' or parsed.path != '/api/version'
            or parsed.query or parsed.fragment):
        raise ValueError('health_url must be https://<admin IP>:<admin port>/api/version')
    return paths


def _require_root_owned(path):
    if path.stat().st_uid != 0:
        raise ValueError(f'root ownership required: {path}')


def _secure_file(path):
    _no_symlinks(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
        raise ValueError(f'unsafe writable or nonregular file: {path}')
    _require_root_owned(path)


def _secure_parents(path):
    for parent in (*reversed(path.parents), path):
        if parent.exists() and parent.is_dir():
            _require_root_owned(parent)
            if parent.stat().st_mode & 0o022:
                raise ValueError(f'unsafe writable directory: {parent}')


def _read_json(path, limit=64 * 1024):
    if path.stat().st_size > limit:
        raise ValueError('update JSON exceeds size limit')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'duplicate JSON key: {key}')
            result[key] = value
        return result
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique)


def load_spec(spec_path):
    """Authenticate a production invocation before trusting any write paths."""
    if os.getuid() != 0:
        raise ValueError('the installed update worker must run as root')
    path = _path(str(spec_path))
    _secure_file(path)
    _secure_parents(path.parent)
    spec = _read_json(path)
    paths = _validate_spec(spec)
    if not _within(paths['program_dir'], INSTALL_ROOT) or (paths['program_dir'] / 'server').exists():
        raise ValueError('updates require a flat installed program directory under /opt')
    descriptor_path = paths['program_dir'] / 'update-install.json'
    _secure_file(descriptor_path)
    descriptor = _read_json(descriptor_path)
    if not isinstance(descriptor, dict) or type(descriptor.get('schema')) is not int or descriptor['schema'] != 1:
        raise ValueError('invalid deployment descriptor schema')
    for key in DESCRIPTOR_FIELDS:
        if key not in descriptor and key != 'repo_unit':
            raise ValueError(f'deployment descriptor missing {key}')
        if spec.get(key) != descriptor.get(key):
            raise ValueError(f'deployment descriptor mismatch: {key}')
    for key in ('archive', 'status_path'):
        if key in descriptor and spec.get(key) != descriptor[key]:
            raise ValueError(f'deployment descriptor mismatch: {key}')
    updates = paths['data_dir'] / 'updates'
    if not _within(path, updates) or path in (paths['archive'], paths['status_path']):
        raise ValueError('spec must be a separate file within data_dir/updates')
    for key in ('program_dir', 'static_dir', 'data_dir', 'config_path', 'archive', 'status_path'):
        _secure_parents(paths[key] if paths[key].is_dir() else paths[key].parent)
    _secure_file(paths['archive'])
    _secure_file(paths['config_path'])
    # Read the trusted config directly; importing installed settings could execute
    # application code, and environment overrides must not change this boundary.
    import tomllib
    with paths['config_path'].open('rb') as handle:
        config = tomllib.load(handle)
    admin_port = (config.get('ports') or {}).get('admin', 8443)
    if type(admin_port) is not int or not 1 <= admin_port <= 65535:
        raise ValueError('invalid configured admin port')
    parsed = urllib.parse.urlsplit(spec['health_url'])
    admin_host = (config.get('network') or {}).get('host_ip', '127.0.0.1')
    if parsed.port != admin_port or parsed.hostname != admin_host:
        raise ValueError('health URL does not match configured admin address and port')
    declared_spec_path = spec.get('spec_path')
    if declared_spec_path is not None and _path(declared_spec_path) != path:
        raise ValueError('spec_path does not match the CLI spec file')
    spec['spec_path'] = str(path)
    return spec


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_bytes(path, payload, mode=0o600):
    _no_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.update-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _status(spec, state, **extra):
    result = {'id': spec.get('id'), 'state': state, 'version': spec.get('version'),
              'updated_at': time.time(), **extra}
    _atomic_bytes(Path(spec['status_path']), json.dumps(result, ensure_ascii=False).encode())
    return result


def _destination(name, paths):
    if name.startswith('server/'):
        return paths['program_dir'] / name.split('/')[1], Path('program') / name.split('/')[1]
    if name.startswith('web/'):
        return paths['static_dir'] / name.split('/')[1], Path('web') / name.split('/')[1]
    return paths['program_dir'] / name, Path('program') / name


def _stage(spec, paths, stage):
    """Hash and parse the same open file; tar metadata never controls extraction."""
    names, total, manifest = set(), 0, []
    with paths['archive'].open('rb') as source:
        digest = hashlib.sha256()
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
        if digest.hexdigest().lower() != spec['sha256'].lower():
            raise ValueError('archive SHA-256 mismatch')
        source.seek(0)
        with tarfile.open(fileobj=source, mode='r:gz') as archive:
            for member in archive:
                name = member.name
                parts = name.split('/')
                if (not member.isreg() or member.issparse() or '\\' in name
                        or any(part in ('', '.', '..') for part in parts)
                        or parts[0] != 'ps5-jb-server-' + spec['version']):
                    raise ValueError(f'unsafe archive member: {name}')
                relative = '/'.join(parts[1:])
                python_module = (len(parts) == 3 and parts[1] == 'server'
                                 and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*\.py', parts[2])
                                 and parts[2] != 'host.py')
                if relative not in RUNTIME_FILES and not python_module:
                    raise ValueError(f'unexpected runtime member: {name}')
                if relative in names:
                    raise ValueError(f'duplicate archive member: {name}')
                names.add(relative)
                total += member.size
                if len(names) > MAX_FILES or not 0 <= member.size <= MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                    raise ValueError('archive exceeds runtime resource limits')
                target, backup_name = _destination(relative, paths)
                _no_symlinks(target)
                if target.exists() and not target.is_file():
                    raise ValueError(f'nonregular destination: {target}')
                if target == paths['config_path']:
                    raise ValueError('runtime file overlaps configuration')
                staged = stage / PurePosixPath(relative)
                staged.parent.mkdir(parents=True, exist_ok=True)
                extracted = archive.extractfile(member)
                with extracted, staged.open('xb') as handle:
                    remaining = member.size
                    while remaining:
                        content = extracted.read(min(1024 * 1024, remaining))
                        if not content:
                            raise ValueError(f'truncated member: {name}')
                        handle.write(content)
                        remaining -= len(content)
                if python_module:
                    compile(staged.read_bytes(), relative, 'exec', dont_inherit=True)
                manifest.append((staged, target, backup_name))
    if not REQUIRED_FILES <= names:
        raise ValueError('release missing required files: ' + ', '.join(sorted(REQUIRED_FILES - names)))
    if (stage / 'VERSION').read_text(encoding='utf-8').strip() != spec['version']:
        raise ValueError('release VERSION mismatch')
    if _read_version(stage / 'server' / 'version.py') != spec['version']:
        raise ValueError('version.py must declare one matching constant VERSION')
    return manifest


def _read_version(path):
    _no_symlinks(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('version module exceeds resource limit')
    tree = ast.parse(path.read_bytes())
    versions = []
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(target, ast.Name) and target.id == 'VERSION' for target in targets):
            versions.append(node.value.value if isinstance(node.value, ast.Constant) else None)
    if len(versions) != 1 or not isinstance(versions[0], str) or not VERSION_RE.fullmatch(versions[0]):
        raise ValueError('version.py must declare one literal VERSION')
    return versions[0]


def _service(action, unit):
    subprocess.run(['systemctl', action, '--', unit], check=True, capture_output=True,
                   text=True, timeout=60)


def _health(url, version):
    """Bounded HTTPS polling of the installed IP; no redirects or proxy lookup."""
    parsed = urllib.parse.urlsplit(url)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    deadline = time.monotonic() + 45
    last_error = 'no health response'
    while time.monotonic() < deadline:
        connection = http.client.HTTPSConnection(parsed.hostname, parsed.port, context=context, timeout=3)
        try:
            connection.request('GET', '/api/version', headers={'Host': parsed.netloc, 'Accept': 'application/json'})
            response = connection.getresponse()
            payload = response.read(64 * 1024 + 1)
            if response.status != 200 or len(payload) > 64 * 1024:
                raise ValueError(f'health HTTP {response.status} or oversized response')
            body = json.loads(payload)
            if not isinstance(body, dict) or body.get('version') != version or body.get('ready') is not True:
                raise ValueError('health version mismatch or gateway not ready')
            return
        except (OSError, ValueError, http.client.HTTPException) as exc:
            last_error = str(exc)
        finally:
            connection.close()
        time.sleep(0.5)
    raise RuntimeError(f'health check failed: {last_error}')


def _verify_services(spec):
    """Require active units and an HTTP-ready repository, without proxy lookup."""
    import tomllib
    with Path(spec['config_path']).open('rb') as handle:
        config = tomllib.load(handle)
    repo_port = (config.get('ports') or {}).get('repo', 8080)
    if type(repo_port) is not int or not 1 <= repo_port <= 65535:
        raise ValueError('invalid configured repository port')
    units = [unit for unit in (spec.get('repo_unit'), spec['admin_unit']) if unit]
    host = urllib.parse.urlsplit(spec['health_url']).hostname
    deadline = time.monotonic() + 15
    last_error = 'services not ready'
    while time.monotonic() < deadline:
        connection = None
        try:
            for unit in units:
                _service('is-active', unit)
            if spec.get('repo_unit'):
                connection = http.client.HTTPConnection(host, repo_port, timeout=2)
                connection.request('GET', '/repo.json')
                response = connection.getresponse()
                if response.status != 200:
                    raise ValueError(f'repository HTTP {response.status}')
                response.read(64 * 1024)
            return
        except (OSError, ValueError, subprocess.SubprocessError, http.client.HTTPException) as exc:
            last_error = str(exc)
        finally:
            if connection:
                connection.close()
        time.sleep(0.5)
    raise RuntimeError(f'service readiness failed: {last_error}')


def _atomic_copy(source, target, replace):
    _no_symlinks(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.update-', dir=target.parent)
    os.close(fd)
    try:
        shutil.copy2(source, temporary)
        with open(temporary, 'rb') as handle:
            os.fsync(handle.fileno())
        replace(temporary, target)
        _sync_directory(target.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class DeploymentActive(ValueError):
    pass


@contextmanager
def _deployment_lock(updates):
    lock = updates / 'transaction.lock'
    _no_symlinks(lock)
    updates.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError('deployment lock must be regular')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeploymentActive('another deployment is active') from None
        yield
    finally:
        os.close(descriptor)


def _failure(spec, error, **extra):
    return {'id': spec.get('id') if isinstance(spec, dict) else None,
            'state': 'failed', 'version': spec.get('version') if isinstance(spec, dict) else None,
            'updated_at': time.time(), 'error': str(error), **extra}


def _transaction(updates):
    path = updates / 'transaction.json'
    _no_symlinks(path)
    return _read_json(path, 16 * 1024 * 1024) if path.exists() else None


def _write_transaction(updates, journal, phase):
    journal['phase'] = phase
    _atomic_bytes(updates / 'transaction.json', json.dumps(journal).encode())


def _check_health(spec, version, hooks):
    if hooks.get('health', _health)(spec['health_url'], version) is False:
        raise RuntimeError('health check failed')
    if hooks.get('verify_services', _verify_services)(spec) is False:
        raise RuntimeError('service readiness failed')


def _restore(spec, paths, journal, hooks, boot=False):
    """Keep restoring remaining files and restarting old units after one failure."""
    updates = paths['data_dir'] / 'updates'
    errors, file_errors = [], []
    service = hooks.get('service', _service)
    units = [unit for unit in (spec.get('repo_unit'), spec['admin_unit']) if unit]
    _write_transaction(updates, journal, 'replacing')
    if not boot:
        for unit in units:
            try:
                service('stop', unit)
            except Exception as exc:
                errors.append(str(exc))
    for entry in reversed(journal['entries']):
        try:
            target = Path(entry['target'])
            _no_symlinks(target)
            if entry['saved'] is not None:
                _atomic_copy(Path(entry['saved']), target, hooks.get('replace', os.replace))
            elif target.exists():
                target.unlink()
                _sync_directory(target.parent)
        except Exception as exc:
            file_errors.append(str(exc))
    errors.extend(file_errors)
    if not file_errors:
        _write_transaction(updates, journal, 'restored')
    if not boot:
        for unit in units:
            try:
                service('start', unit)
            except Exception as exc:
                errors.append(str(exc))
        try:
            _check_health(spec, journal['previous_version'], hooks)
        except Exception as exc:
            errors.append(str(exc))
    return errors


def run(spec, hooks=None):
    """Deploy under a nonblocking filesystem lock; retain interrupted recovery data.

    Hooks: service(action, unit), health(url, version), verify_services(spec), and
    replace(source, target). The CLI authenticates the descriptor via load_spec.
    """
    try:
        paths = _validate_spec(spec)
        with _deployment_lock(paths['data_dir'] / 'updates'):
            return _run_locked(spec, paths, hooks or {})
    except Exception as exc:
        # Never overwrite the status of an active or untrusted deployment.
        return _failure(spec, exc)


def _run_locked(spec, paths, hooks):
    updates = paths['data_dir'] / 'updates'
    pending = _transaction(updates)
    if pending and pending.get('phase') not in ('committed', 'restored'):
        raise ValueError('interrupted deployment requires recovery')
    previous_version = _read_version(paths['program_dir'] / 'version.py')
    if tuple(map(int, spec['version'].split('.'))) <= tuple(map(int, previous_version.split('.'))):
        raise ValueError('update version must be newer than installed version')
    stopped, journal = False, None
    try:
        _status(spec, 'validating')
        with tempfile.TemporaryDirectory(prefix='stage-', dir=updates) as stage_dir:
            manifest = _stage(spec, paths, Path(stage_dir))
            _status(spec, 'backing_up')
            backup = updates / 'backups' / (str(time.time_ns()) + '-' + uuid.uuid4().hex[:12])
            _no_symlinks(backup)
            backup.mkdir(parents=True, mode=0o700)
            entries = []
            for source, target, backup_name in manifest:
                saved = backup / backup_name if target.exists() else None
                if saved:
                    _atomic_copy(target, saved, os.replace)
                entries.append({'target': str(target), 'saved': str(saved) if saved else None})
            journal = {'schema': 1, 'spec': spec,
                       'spec_path': spec.get('spec_path'),
                       'previous_version': previous_version, 'backup': str(backup), 'entries': entries}
            _write_transaction(updates, journal, 'prepared')
            _status(spec, 'installing')
            service = hooks.get('service', _service)
            units = [unit for unit in (spec.get('repo_unit'), spec['admin_unit']) if unit]
            stopped = True
            for unit in units:
                service('stop', unit)
            _write_transaction(updates, journal, 'replacing')
            for source, target, _ in manifest:
                os.chmod(source, stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644)
                _atomic_copy(source, target, hooks.get('replace', os.replace))
            _status(spec, 'restarting')
            for unit in units:
                service('start', unit)
            _status(spec, 'verifying')
            _check_health(spec, spec['version'], hooks)
            _write_transaction(updates, journal, 'committed')
            return _status(spec, 'done')
    except Exception as exc:
        extra = {}
        if stopped:
            try:
                errors = _restore(spec, paths, journal, hooks)
            except Exception as restore_error:
                errors = [str(restore_error)]
            if errors:
                extra['rollback_error'] = '; '.join(errors)
        state = 'rolled_back' if stopped and not extra else 'failed'
        try:
            return _status(spec, state, error=str(exc), **extra)
        except Exception as status_error:
            return _failure(spec, exc, status_error=str(status_error), **extra)


def _runtime_name(target, paths):
    """Invert only the updater's runtime mapping, never trust arbitrary paths."""
    for name in RUNTIME_FILES:
        if _destination(name, paths)[0] == target:
            return name
    if target.parent == paths['program_dir'] and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*\.py', target.name) and target.name != 'host.py':
        return 'server/' + target.name
    raise ValueError(f'unsafe recovery target: {target}')


def _validate_transaction(journal, spec, paths, secure=False):
    if not isinstance(journal, dict) or journal.get('schema') != 1 or journal.get('spec') != spec:
        raise ValueError('recovery transaction does not match original spec')
    backup = _path(journal.get('backup'))
    backups = paths['data_dir'] / 'updates' / 'backups'
    if backup.parent != backups or not backup.is_dir():
        raise ValueError('invalid recovery backup directory')
    if secure:
        _secure_parents(backup)
        if backup.stat().st_uid != 0 or backup.stat().st_mode & 0o077:
            raise ValueError('unsafe recovery backup directory')
    entries, names = journal.get('entries'), set()
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FILES:
        raise ValueError('invalid recovery entries')
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('invalid recovery entry')
        target = _path(entry.get('target'))
        name = _runtime_name(target, paths)
        if name in names or target == paths['config_path']:
            raise ValueError('duplicate or protected recovery target')
        names.add(name)
        if entry.get('saved') is not None:
            saved = _path(entry['saved'])
            if saved != backup / _destination(name, paths)[1] or not saved.is_file():
                raise ValueError('invalid recovery backup file')
            if secure:
                _secure_file(saved)
                _secure_parents(saved.parent)
    if not REQUIRED_FILES <= names:
        raise ValueError('recovery transaction missing required runtime targets')
    if _read_version(backup / 'program' / 'version.py') != journal.get('previous_version'):
        raise ValueError('invalid previous version in recovery transaction')


def _recover_locked(spec, paths, hooks, boot, journal=None, secure=False):
    updates = paths['data_dir'] / 'updates'
    journal = _transaction(updates) if journal is None else journal
    if not journal or journal.get('phase') in ('committed', 'restored'):
        return {'state': 'idle', 'recovery_pending': False}
    _validate_transaction(journal, spec, paths, secure=secure)
    errors = _restore(spec, paths, journal, hooks, boot=boot)
    if errors:
        return _status(spec, 'failed', error='interrupted update recovery failed',
                       rollback_error='; '.join(errors), recovery_pending=True)
    if boot:
        return _status(spec, 'failed', error='interrupted update files restored; health verification pending',
                       restored=True, recovery_pending=True)
    return _status(spec, 'rolled_back', error='interrupted update restored', restored=True)


def recover(spec, hooks=None, boot=False):
    """Sandbox/live recovery API; boot=True only restores files, never starts units."""
    try:
        paths = _validate_spec(spec)
        with _deployment_lock(paths['data_dir'] / 'updates'):
            return _recover_locked(spec, paths, hooks or {}, boot)
    except DeploymentActive as exc:
        return {'state': 'skipped', 'active': True} if boot else _failure(spec, exc)
    except Exception as exc:
        return _failure(spec, exc)


def recover_install(descriptor_path):
    """Authenticate boot recovery from a trusted installed deployment descriptor."""
    if os.getuid() != 0:
        raise ValueError('recovery must run as root')
    descriptor_path = _path(str(descriptor_path))
    _secure_file(descriptor_path)
    _secure_parents(descriptor_path.parent)
    descriptor = _read_json(descriptor_path)
    if not isinstance(descriptor, dict) or descriptor.get('schema') != 1:
        raise ValueError('invalid recovery descriptor')
    program = _path(descriptor.get('program_dir'))
    if descriptor_path != program / 'update-install.json' or not _within(program, INSTALL_ROOT):
        raise ValueError('recovery requires installed descriptor under /opt')
    installed = program / 'update_worker.py'
    if _path(os.path.abspath(__file__)) != installed:
        raise ValueError('must invoke the fixed installed update_worker.py')
    _secure_file(installed)
    updates = _path(descriptor.get('data_dir')) / 'updates'
    _secure_parents(updates)
    try:
        with _deployment_lock(updates):
            transaction_path = updates / 'transaction.json'
            if not transaction_path.exists():
                return {'state': 'idle', 'recovery_pending': False}
            _secure_file(transaction_path)
            journal = _transaction(updates)
            if journal.get('phase') in ('committed', 'restored'):
                return {'state': 'idle', 'recovery_pending': False}
            spec_path = _path(journal.get('spec_path'))
            if not _within(spec_path, updates):
                raise ValueError('recovery spec_path is outside updates directory')
            spec = load_spec(spec_path)
            for key in DESCRIPTOR_FIELDS:
                if spec.get(key) != descriptor.get(key):
                    raise ValueError('recovery descriptor does not match original spec')
            return _recover_locked(spec, _validate_spec(spec), {}, True, journal, secure=True)
    except DeploymentActive:
        # Admin/repo Requires this boot unit. An active updater owns restoration,
        # so returning success allows its service starts to finish without a cycle.
        return {'state': 'skipped', 'active': True}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if len(argv) == 2 and argv[0] == '--recover':
            result = recover_install(argv[1])
            if result.get('rollback_error'):
                print(json.dumps(result), file=sys.stderr)
                return 1
            return 0
        if len(argv) != 1:
            raise ValueError('usage: update_worker.py <absolute spec.json> | --recover <update-install.json>')
        spec = load_spec(argv[0])
        installed = _path(spec['program_dir']) / 'update_worker.py'
        if _path(os.path.abspath(__file__)) != installed:
            raise ValueError('must invoke the fixed installed update_worker.py')
        _secure_file(installed)
        result = run(spec)
    except Exception as exc:
        print(f'update refused: {exc}', file=sys.stderr)
        return 1
    if result['state'] != 'done':
        print(json.dumps(result), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
