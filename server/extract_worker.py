"""One unrar extraction, run in its own transient systemd unit (ps5-extract-<id>).

The admin service is sandboxed (no CAP_SYS_ADMIN, MemoryMax=256M): it may
neither mount FUSE nor hold an unrar window of several gigabytes, and a fault
in the C library must not take DNS down with it. TransferManager therefore
starts this script through systemd-run with its own memory limit. It mounts
the NAS folder read-only with rclone, runs archive.Installer with the unrar
engine, and writes its progress to a JSON status file beside its spec.
`systemctl stop` (SIGTERM) cancels; the staging folder is removed either way.

Usage: extract_worker.py <spec.json>
"""
import json
import logging
import os
import posixpath
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import archive  # noqa: E402
from files import FileProxy, FileServiceError  # noqa: E402
from nas import NasError, NasStore  # noqa: E402


class Canceled(Exception):
    pass


# ------------------------------------------------------------------ mount --

def _obscure(secret):
    # rclone wants its own reversible encoding; stdin keeps it out of argv.
    return subprocess.run(['rclone', 'obscure', '-'], input=secret, capture_output=True,
                          text=True, check=True, timeout=30).stdout.strip()


def rclone_remote(config, folder):
    """(environment, remote path, extra flags) for an rclone remote named NAS."""
    protocol = config['protocol']
    host = config['host']
    root = config.get('path') or '/'
    env, flags = {}, []
    if protocol == 'webdav':
        scheme = 'https' if config.get('tls') else 'http'
        bracketed = f'[{host}]' if ':' in host else host
        env.update(TYPE='webdav', VENDOR='other', URL=f'{scheme}://{bracketed}:{config["port"]}')
        if config.get('tls') and not config.get('verify_tls'):
            flags.append('--no-check-certificate')
    elif protocol == 'smb':
        env.update(TYPE='smb', HOST=host, PORT=str(config['port']),
                   DOMAIN=config.get('workgroup') or 'WORKGROUP')
        if config.get('share'):
            root = '/' + config['share'] + ('' if root == '/' else root)
    elif protocol == 'ftp':
        env.update(TYPE='ftp', HOST=host, PORT=str(config['port']))
        if config.get('tls'):
            env['EXPLICIT_TLS'] = 'true'
            if not config.get('verify_tls'):
                env['NO_CHECK_CERTIFICATE'] = 'true'
    else:
        raise ValueError(f'不支持的协议 {protocol}')
    if config.get('username') or protocol == 'ftp':
        env['USER'] = config.get('username') or 'anonymous'
    if config.get('password') or protocol == 'ftp':
        env['PASS'] = _obscure(config.get('password') or '')
    remote = posixpath.join(root, folder.lstrip('/'))
    return {f'RCLONE_CONFIG_NAS_{key}': value for key, value in env.items()}, 'NAS:' + remote, flags


class RcloneMount:
    """Read-only FUSE mount of one NAS folder for the life of the job."""

    def __init__(self, config, folder, mountpoint):
        self.config, self.folder, self.mountpoint = config, folder, mountpoint
        self.process = None

    def __enter__(self):
        env, remote, flags = rclone_remote(self.config, self.folder)
        os.makedirs(self.mountpoint, mode=0o700, exist_ok=True)
        self.process = subprocess.Popen(
            ['rclone', 'mount', remote, self.mountpoint, '--read-only', '--vfs-cache-mode', 'off',
             '--buffer-size', '64M', '--no-modtime', '--dir-cache-time', '24h', '--attr-timeout', '1h',
             '--log-level', 'NOTICE', *flags],
            env={**os.environ, **env}, stdin=subprocess.DEVNULL)
        deadline = time.time() + 30
        while not os.path.ismount(self.mountpoint):
            if self.process.poll() is not None:
                raise NasError('无法挂载 NAS 目录（rclone 已退出），请检查连接设置，详情见 journalctl')
            if time.time() > deadline:
                self._stop()
                raise NasError('挂载 NAS 目录超时')
            time.sleep(0.2)
        return self.mountpoint

    def __exit__(self, *exc):
        self._stop()

    def _stop(self):
        subprocess.run(['fusermount3', '-uz', self.mountpoint], capture_output=True)
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        try:
            os.rmdir(self.mountpoint)
        except OSError:
            pass


def _local_read_at(path, offset, size):
    with open(path, 'rb') as handle:
        handle.seek(offset)
        return handle.read(size)


# -------------------------------------------------------------------- run --

def run(spec, status, cancel, files, mount):
    """Do the job; status is a dict updated in place. Returns the result dict.

    files: a FileProxy-like PS5 file service; mount: a context manager whose
    __enter__ returns the local folder holding the volumes.
    """
    lock = threading.Lock()

    class Hooks:
        @staticmethod
        def check():
            if cancel.is_set():
                raise Canceled()

        @staticmethod
        def read(count):
            with lock:
                status['bytes_done'] += count

        @staticmethod
        def current(name, size, done):
            with lock:
                status.update(current=name, current_size=size, current_done=done)

        @staticmethod
        def written():
            with lock:
                status['files_done'] += 1

    with mount as folder:
        volumes = [dict(item, path=os.path.join(folder, item['name'])) for item in spec['volumes']]
        scanner = archive.prescan(_local_read_at, volumes, strict=False)
        expected = scanner.entries if scanner is not None else None
        with lock:
            status['state'] = 'running'
            if scanner is not None:
                status['files_total'] = sum(1 for is_dir in scanner.entries.values() if not is_dir)
                status['bytes_total'] = scanner.unpacked
        installer = archive.Installer(files, None, spec['volumes'], spec['dest'], spec['mode'],
                                      spec['id'], Hooks, engine='unrar', volume_dir=folder,
                                      expected=expected)
        return installer.run(spec['base'])


def _write_json(path, document):
    temporary = path + '.tmp'
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(document, stream, ensure_ascii=False)
    os.replace(temporary, path)


def main(spec_path):
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    with open(spec_path, encoding='utf-8') as stream:
        spec = json.load(stream)
    status = {'state': 'starting', 'bytes_done': 0, 'bytes_total': 0, 'files_done': 0,
              'files_total': 0, 'current': '', 'current_size': 0, 'current_done': 0,
              'error': '', 'result_path': '', 'title': '', 'note': '', 'final': False,
              'pid': os.getpid()}
    cancel = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: cancel.set())
    stop_writer = threading.Event()

    def writer():
        while not stop_writer.wait(0.5):
            _write_json(spec['status'], dict(status))

    threading.Thread(target=writer, daemon=True).start()
    try:
        store = NasStore(spec['nas_store'], key_path=spec.get('nas_key'))
        config = store.get(spec['nas_id'])
        ps5 = spec['ps5']
        files = FileProxy(ps5['subnet'], port=ps5['port'], ftp_port=ps5['ftp_port'],
                          space_port=ps5['space_port'])
        files.configure(ps5['host'])
        result = run(spec, status, cancel, files, RcloneMount(config, spec['folder'], spec['mount']))
        status.update(state='done', result_path=result['path'], title=result['title'],
                      note=result['note'], current='')
    except Canceled:
        status.update(state='canceled', current='')
    except (archive.ArchiveError, FileServiceError, NasError, ValueError, OSError) as exc:
        logging.warning('extract failed: %s', exc)
        status.update(state='failed', error=str(exc)[:300], current='')
    except Exception as exc:  # report, never leave the service guessing
        logging.exception('extract failed')
        status.update(state='failed', error=f'内部错误：{exc}'[:300], current='')
    finally:
        stop_writer.set()
        status['final'] = True
        _write_json(spec['status'], dict(status))


if __name__ == '__main__':
    main(sys.argv[1])
