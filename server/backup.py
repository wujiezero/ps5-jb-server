"""Configuration backup and restore.

A backup is a tar.gz whose manifest.json lists every member with its size and
SHA-256. Only the names in MEMBER are ever written or read back. With the NAS
key included the whole archive is sealed with a passphrase (scrypt +
AES-256-GCM); the passphrase is never stored.

Left out on purpose: admin password, sessions, TLS certificates, activity
history and the PS5Upload pairing key.

Restore applies each part on its own and reports it: the policy goes through
the normal policy change (validated, applied to nftables, the old one becomes
"previous"), labels are replaced, NAS connections are replaced (sealed logins
come back only with the key), and the plugin repository only gains entries it
does not have yet.
"""
import datetime
import hashlib
import io
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import tarfile
import tempfile
import time
from pathlib import Path

from version import VERSION

FORMAT = 1
MAGIC = b'PS5JBBK1'
HEADER = len(MAGIC) + 4 + 16 + 12      # magic, version/log2n/r/p, salt, nonce
TAG = 16
SCRYPT_LOG2N, SCRYPT_R, SCRYPT_P = 15, 8, 1
MIN_PASSPHRASE = 10
CHUNK = 1 << 20
MAX_JSON = 8 << 20
MAX_MEMBERS = 4000
# Extracted size cap: a small gzip bomb must not fill the data disk.
MAX_TOTAL = 4 << 30
PAYLOAD = r'[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(?:[Ee][Ll][Ff]|[Bb][Ii][Nn])'
MEMBER = re.compile(r'(?:manifest|policy|labels|console|hosts|notify|nas)\.json|nas\.key'
                    r'|repo/repo\.json|repo/versions/index\.json'
                    rf'|repo/payloads/{PAYLOAD}|repo/versions/[0-9a-f]{{64}}\Z')
TITLES = {'policy': '访问策略', 'labels': '活动标签', 'console': '文件主机', 'hosts': '已知主机',
          'notify': '通知渠道', 'nas': 'NAS 连接', 'repo': '插件仓库'}


class BackupError(ValueError):
    """A backup that cannot be made or read; the message is shown as is."""


def _crypto():
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        from cryptography.exceptions import InvalidTag
    except ImportError as exc:
        raise BackupError('服务器缺少加密组件（python3-cryptography），无法处理带密钥的备份') from exc
    return Cipher, algorithms, modes, Scrypt, InvalidTag


def _derive(passphrase, salt, log2n, r, p):
    *_, Scrypt, _ = _crypto()
    return Scrypt(salt=salt, length=32, n=1 << log2n, r=r, p=p).derive(passphrase.encode('utf-8'))


def check_passphrase(passphrase):
    if not isinstance(passphrase, str) or len(passphrase) < MIN_PASSPHRASE:
        raise BackupError(f'备份口令至少 {MIN_PASSPHRASE} 位')
    if len(passphrase) > 256:
        raise BackupError('备份口令太长')
    return passphrase


def encrypt_file(source, target, passphrase):
    """source -> target: header | AES-256-GCM ciphertext | tag (header is the AAD)."""
    Cipher, algorithms, modes, _, _ = _crypto()
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    header = MAGIC + bytes([1, SCRYPT_LOG2N, SCRYPT_R, SCRYPT_P]) + salt + nonce
    key = _derive(check_passphrase(passphrase), salt, SCRYPT_LOG2N, SCRYPT_R, SCRYPT_P)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header)
    with open(source, 'rb') as reader, _private(target) as writer:
        writer.write(header)
        while chunk := reader.read(CHUNK):
            writer.write(encryptor.update(chunk))
        writer.write(encryptor.finalize() + encryptor.tag)


def is_encrypted(path):
    with open(path, 'rb') as stream:
        return stream.read(len(MAGIC)) == MAGIC


def decrypt_file(source, target, passphrase):
    Cipher, algorithms, modes, _, InvalidTag = _crypto()
    size = os.path.getsize(source)
    with open(source, 'rb') as reader:
        header = reader.read(HEADER)
        if len(header) < HEADER or size < HEADER + TAG or not header.startswith(MAGIC):
            raise BackupError('备份文件已损坏')
        version, log2n, r, p = header[len(MAGIC):len(MAGIC) + 4]
        # Bounded so a crafted file cannot make scrypt take gigabytes of memory.
        if version != 1 or not 14 <= log2n <= 17 or r != 8 or p != 1:
            raise BackupError('不认识的备份加密格式，请先升级本服务')
        if not passphrase:
            raise BackupError('这个备份包含密钥，需要输入备份口令')
        salt, nonce = header[len(MAGIC) + 4:len(MAGIC) + 20], header[len(MAGIC) + 20:]
        reader.seek(size - TAG)
        tag = reader.read(TAG)
        reader.seek(HEADER)
        key = _derive(passphrase, salt, log2n, r, p)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(header)
        remaining = size - HEADER - TAG
        with _private(target) as writer:
            while remaining:
                chunk = reader.read(min(CHUNK, remaining))
                if not chunk:
                    raise BackupError('备份文件已损坏')
                remaining -= len(chunk)
                writer.write(decryptor.update(chunk))
            try:
                writer.write(decryptor.finalize())
            except InvalidTag:
                raise BackupError('备份口令不正确，或文件已损坏') from None


def _private(path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    return os.fdopen(descriptor, 'wb')


def _atomic_bytes(path, data):
    path = Path(path)
    temporary = path.with_name('.' + path.name + '.tmp')
    with _private(temporary) as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _json_bytes(document):
    return (json.dumps(document, ensure_ascii=False, indent=1, sort_keys=True) + '\n').encode('utf-8')


class BackupService:
    """Builds and restores backups; the parts it touches are passed in.

    policy:        callable returning the active policy
    apply_policy:  callable(raw) that validates and applies a policy
    labels:        LabelsStore
    console:       ConsoleAPI (file host, known hosts, NAS store)
    repo:          RepoManager
    notify:        optional object with document() / restore(document, with_key)
    """

    def __init__(self, data_dir, policy, apply_policy, labels=None, console=None, repo=None,
                 notify=None, clock=time.time):
        self.data_dir = Path(data_dir)
        self.tmp = self.data_dir / 'backup-tmp'
        self.policy, self.apply_policy = policy, apply_policy
        self.labels, self.console, self.repo, self.notify = labels, console, repo, notify
        self.clock = clock

    # ---------- housekeeping ----------
    def cleanup(self):
        """Remove leftovers of a backup or restore cut short by a restart."""
        if self.tmp.is_dir():
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _workdir(self):
        self.tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix='job-', dir=self.tmp))

    @property
    def nas(self):
        return getattr(self.console, 'nas', None)

    # ---------- create ----------
    def _parts(self, payloads, include_key):
        """[(member name, bytes or Path)] in archive order."""
        parts = [('policy.json', _json_bytes(self.policy()))]
        if self.labels is not None:
            parts.append(('labels.json', _json_bytes(self.labels.all())))
        if self.console is not None:
            parts.append(('console.json', _json_bytes({'files_host': self.console.files.host or ''})))
        hosts = self.data_dir / 'hosts.json'
        if hosts.is_file():
            parts.append(('hosts.json', hosts.read_bytes()))
        if self.notify is not None:
            parts.append(('notify.json', _json_bytes(self.notify.document())))
        if self.nas is not None:
            parts.append(('nas.json', _json_bytes(self.nas.document())))
            if include_key:
                parts.append(('nas.key', self.nas.box.export()))
        if self.repo is not None:
            with self.repo.lock:
                catalog = self.repo.catalog()
                history = self.repo.history()
                parts.append(('repo/repo.json', _json_bytes(catalog)))
                parts.append(('repo/versions/index.json', _json_bytes({'schema': 1, 'plugins': history})))
                if payloads:
                    for item in catalog['payloads']:
                        name = str(item.get('filename') or '')
                        path = self.repo.payloads / name
                        if re.fullmatch(PAYLOAD, name) and path.is_file():
                            parts.append(('repo/payloads/' + name, self._snapshot(path)))
                    for digest in sorted({r['checksum'] for records in history.values() for r in records}):
                        path = self.repo.versions / digest
                        if path.is_file():
                            parts.append(('repo/versions/' + digest, self._snapshot(path)))
        return parts

    def _snapshot(self, path):
        """Copy a repository file now, so a later upload cannot change it mid-archive."""
        work = self.tmp / 'snap'
        work.mkdir(mode=0o700, parents=True, exist_ok=True)
        copy = Path(tempfile.mkstemp(dir=work)[1])
        shutil.copyfile(path, copy)
        return copy

    def create(self, payloads=False, include_key=False, passphrase=''):
        """Write a backup under backup-tmp; returns (path, download name).

        The caller sends it and then deletes it with discard().
        """
        if include_key:
            check_passphrase(passphrase)
        work = self._workdir()
        try:
            parts = self._parts(payloads, include_key)
            files, contents = {}, set()
            archive = work / 'backup.tar.gz'
            with _private(archive) as raw, tarfile.open(fileobj=raw, mode='w:gz') as tar:
                for name, data in parts:
                    digest, size = self._add(tar, name, data)
                    files[name] = {'sha256': digest, 'size': size}
                    contents.add(name.split('/', 1)[0].split('.', 1)[0])
                manifest = {'format': FORMAT, 'service_version': VERSION,
                            'created': datetime.datetime.fromtimestamp(self.clock()).isoformat(timespec='seconds'),
                            'contents': sorted(contents), 'payloads': bool(payloads),
                            'includes_key': bool(include_key), 'files': files}
                self._add(tar, 'manifest.json', _json_bytes(manifest))
            stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(self.clock()))
            if not include_key:
                return archive, f'ps5-jb-backup-{stamp}.tar.gz'
            sealed = work / 'backup.ps5bak'
            encrypt_file(archive, sealed, passphrase)
            archive.unlink()
            return sealed, f'ps5-jb-backup-{stamp}.ps5bak'
        except Exception:
            shutil.rmtree(work, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(self.tmp / 'snap', ignore_errors=True)

    @staticmethod
    def _add(tar, name, data):
        info = tarfile.TarInfo(name)
        info.mode, info.mtime = 0o600, 0
        digest = hashlib.sha256()
        if isinstance(data, Path):
            info.size = data.stat().st_size
            with open(data, 'rb') as stream:
                while chunk := stream.read(CHUNK):
                    digest.update(chunk)
                stream.seek(0)
                tar.addfile(info, stream)
            data.unlink(missing_ok=True)
        else:
            info.size = len(data)
            digest.update(data)
            tar.addfile(info, io.BytesIO(data))
        return digest.hexdigest(), info.size

    @staticmethod
    def discard(path):
        """Remove a finished backup and its work directory."""
        shutil.rmtree(Path(path).parent, ignore_errors=True)

    # ---------- restore ----------
    def receive(self, stream, length):
        """Copy an uploaded backup to backup-tmp; returns its path."""
        work = self._workdir()
        path = work / 'upload'
        remaining = length
        with _private(path) as writer:
            while remaining:
                chunk = stream.read(min(CHUNK, remaining))
                if not chunk:
                    shutil.rmtree(work, ignore_errors=True)
                    raise BackupError('上传中断，请重试')
                remaining -= len(chunk)
                writer.write(chunk)
        return path

    def _unpack(self, path, passphrase):
        """Verify and extract into the upload's work directory; returns (manifest, folder)."""
        work = Path(path).parent
        if is_encrypted(path):
            plain = work / 'plain.tar.gz'
            decrypt_file(path, plain, passphrase)
            Path(path).unlink()
            path = plain
        folder = work / 'files'
        folder.mkdir(mode=0o700)
        found, total = {}, 0
        try:
            with tarfile.open(path, mode='r:gz') as tar:
                for count, member in enumerate(tar, 1):
                    if count > MAX_MEMBERS:
                        raise BackupError('备份里的文件太多')
                    name = member.name
                    if not member.isreg() or not MEMBER.match(name):
                        raise BackupError(f'备份里有不认识的内容：{name[:80]}')
                    if name in found:
                        raise BackupError(f'备份里有重复的文件：{name}')
                    if name.endswith('.json') and member.size > MAX_JSON:
                        raise BackupError(f'备份里的 {name} 太大')
                    total += member.size
                    if total > MAX_TOTAL:
                        raise BackupError('备份解开后太大')
                    target = folder / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    source = tar.extractfile(member)
                    with _private(target) as writer:
                        while chunk := source.read(CHUNK):
                            digest.update(chunk)
                            writer.write(chunk)
                    found[name] = {'sha256': digest.hexdigest(), 'size': member.size}
        except (tarfile.TarError, EOFError, OSError, UnicodeError) as exc:
            raise BackupError('不是有效的备份文件（tar.gz 无法读取）') from exc
        if 'manifest.json' not in found:
            raise BackupError('备份缺少清单 manifest.json')
        try:
            manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
            fmt = int(manifest.get('format'))
            listed = manifest['files']
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise BackupError('备份清单格式不正确') from exc
        if fmt > FORMAT:
            raise BackupError(f'备份来自更新的版本（{manifest.get("service_version") or "未知"}），请先升级本服务')
        del found['manifest.json']
        if not isinstance(listed, dict) or set(listed) != set(found):
            raise BackupError('备份内容与清单不一致，文件可能被修改过')
        for name, actual in found.items():
            expected = listed[name]
            if not isinstance(expected, dict) or expected.get('sha256') != actual['sha256'] \
                    or expected.get('size') != actual['size']:
                raise BackupError(f'{name} 校验失败，文件可能被修改过')
        return manifest, folder

    def restore(self, path, passphrase=''):
        """Apply an uploaded backup; returns {'manifest': ..., 'items': [...]}."""
        try:
            manifest, folder = self._unpack(path, passphrase)
            items = []
            for part, step in (('policy', self._restore_policy), ('labels', self._restore_labels),
                               ('console', self._restore_console), ('hosts', self._restore_hosts),
                               ('nas', self._restore_nas), ('notify', self._restore_notify),
                               ('repo', self._restore_repo)):
                try:
                    outcome = step(folder)
                except Exception as exc:
                    if not isinstance(exc, ValueError):
                        logging.exception('restore of %s failed', part)
                    outcome = ('failed', str(exc) or exc.__class__.__name__)
                if outcome is None:
                    continue
                items.append({'id': part, 'title': TITLES[part], 'state': outcome[0], 'detail': outcome[1]})
            logging.info('backup restored: %s', ', '.join(f"{i['id']}={i['state']}" for i in items))
            summary = {key: manifest.get(key) for key in ('service_version', 'created', 'payloads', 'includes_key')}
            return {'manifest': summary, 'items': items}
        finally:
            self.discard(path)

    @staticmethod
    def _read(folder, name):
        path = folder / name
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except ValueError as exc:
            raise ValueError(f'{name} 不是有效的 JSON') from exc

    def _restore_policy(self, folder):
        raw = self._read(folder, 'policy.json')
        if raw is None:
            return None
        if raw == self.policy():
            return 'skipped', '与当前策略相同'
        self.apply_policy(raw)
        return 'restored', '已应用；原策略成为「上一版」，可在访问策略里回退'

    def _restore_labels(self, folder):
        raw = self._read(folder, 'labels.json')
        if raw is None or self.labels is None:
            return None
        kept, dropped = self.labels.replace(raw)
        return 'restored', f'{kept} 条' + (f'，{dropped} 条格式不对已忽略' if dropped else '')

    def _restore_console(self, folder):
        raw = self._read(folder, 'console.json')
        if raw is None or self.console is None:
            return None
        host = raw.get('files_host') if isinstance(raw, dict) else None
        if not host:
            return 'skipped', '备份时没有选择文件主机'
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            return 'failed', '文件主机地址不合法'
        self.console.set_files_host(host)
        return 'restored', host

    def _restore_hosts(self, folder):
        path = folder / 'hosts.json'
        if not path.is_file() or self.console is None:
            return None
        self._read(folder, 'hosts.json')
        _atomic_bytes(self.data_dir / 'hosts.json', path.read_bytes())
        discovery = self.console.ps5.discovery
        discovery.reload_known()
        return 'restored', f'{len(discovery.known)} 台（30 天内见过的）'

    def _restore_nas(self, folder):
        raw = self._read(folder, 'nas.json')
        if raw is None or self.nas is None:
            return None
        key_path = folder / 'nas.key'
        key = key_path.read_bytes() if key_path.is_file() else None
        count, blank = self.nas.restore(raw, key=key)
        if key is not None:
            return 'restored', f'{count} 个连接，账号与密钥一起恢复'
        detail = f'{count} 个连接'
        if blank:
            detail += f'；备份不含密钥，其中 {blank} 个需要重新填写用户名和密码'
        return 'restored', detail

    def _restore_notify(self, folder):
        raw = self._read(folder, 'notify.json')
        if raw is None or self.notify is None:
            return None
        count, blank = self.notify.restore(raw, with_key=(folder / 'nas.key').is_file())
        detail = f'{count} 个渠道'
        if blank:
            detail += f'；备份不含密钥，其中 {blank} 个需要重新填写令牌'
        return 'restored', detail

    def _restore_repo(self, folder):
        catalog = self._read(folder, 'repo/repo.json')
        if catalog is None or self.repo is None:
            return None
        history = (self._read(folder, 'repo/versions/index.json') or {}).get('plugins') or {}
        items = catalog.get('payloads') if isinstance(catalog, dict) else None
        if not isinstance(items, list):
            raise ValueError('repo.json 格式不正确')
        repo = self.repo
        added, skipped = [], []
        with repo.lock:
            document = repo.catalog()
            current = repo.history()
            ids = {repo.entry_id(item) for item in document['payloads']}
            names = {item.get('filename') for item in document['payloads']}
            for item in items:
                if not isinstance(item, dict) or item.get('filename') == repo._SEED_FILE:
                    continue
                filename = str(item.get('filename') or '')
                key = repo.entry_id(item)
                if key in ids:
                    continue
                if not re.fullmatch(PAYLOAD, filename):
                    skipped.append(f'{filename[:40]}（文件名不合法）')
                    continue
                if filename in names or (repo.payloads / filename).exists():
                    skipped.append(f'{filename}（同名文件已存在）')
                    continue
                source = folder / 'repo' / 'payloads' / filename
                if not source.is_file():
                    skipped.append(f'{filename}（备份没有包含插件文件）')
                    continue
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                if item.get('checksum') and item.get('checksum') != digest:
                    skipped.append(f'{filename}（文件校验和与条目不符）')
                    continue
                temporary = repo.payloads / ('.' + filename + '.tmp')
                shutil.copyfile(source, temporary)
                os.replace(temporary, repo.payloads / filename)
                entry = {field: item[field] for field in item if isinstance(field, str)}
                entry.update({'id': key, 'filename': filename, 'url': repo.base_url + filename,
                              'checksum': digest})
                document['payloads'] = [p for p in document['payloads'] if p.get('filename') != repo._SEED_FILE]
                document['payloads'].append(entry)
                ids.add(key)
                names.add(filename)
                added.append(filename)
                records = []
                for record in history.get(key, []) if isinstance(history.get(key), list) else []:
                    checksum = record.get('checksum') if isinstance(record, dict) else None
                    if not (isinstance(checksum, str) and re.fullmatch(r'[0-9a-f]{64}', checksum)):
                        continue
                    stored = folder / 'repo' / 'versions' / checksum
                    if not stored.is_file():
                        continue
                    repo.versions.mkdir(parents=True, exist_ok=True)
                    target = repo.versions / record['checksum']
                    if not target.is_file():
                        temporary = repo.versions / ('.' + record['checksum'] + '.tmp')
                        shutil.copyfile(stored, temporary)
                        os.replace(temporary, target)
                    records.append(record)
                if records:
                    current[key] = records[:repo.HISTORY_LIMIT]
            if added:
                repo._write_catalog(document)
                repo._write_history(current)
        if not added:
            detail = '没有需要新增的插件' + (('；跳过 ' + '、'.join(skipped[:5])) if skipped else '')
            return 'skipped', detail
        detail = f'新增 {len(added)} 个插件'
        if skipped:
            detail += f'；跳过 {len(skipped)} 个：' + '、'.join(skipped[:5])
        return 'restored', detail
