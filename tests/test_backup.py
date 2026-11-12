import hashlib
import http.client
import io
import json
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import backup
from activity import LabelsStore
from backup import BackupError, BackupService
from nas import NasStore
from policy import ConfigStore
from ps5 import Discovery
from server import AdminHandler, AdminServer, AuthManager, RepoManager

PASSPHRASE = 'correct horse battery'
POLICY = {'domains': ['example.com'], 'networks': ['192.0.2.0/24'], 'upstream_dns': '192.0.2.1'}
REPO_BASE = 'http://192.0.2.10:8080/payloads/'
# Small scrypt cost keeps the tests fast; the reader still accepts it (>= 2^14).
backup.SCRYPT_LOG2N = 14


class FakeConsole:
    def __init__(self, data):
        self.files = SimpleNamespace(host='192.0.2.5')
        self.nas = NasStore(data / 'nas.json', key_path=data / 'keys' / 'nas.key')
        self.ps5 = SimpleNamespace(discovery=Discovery('192.0.2.0/24', hosts_path=data / 'hosts.json'))
        self.remembered = None

    def set_files_host(self, host):
        self.files.host = host
        self.remembered = host


class Host:
    """One installation: data dir, policy, labels, NAS, repository."""

    def __init__(self, root, fail_policy=False):
        self.data = root / 'data'
        self.data.mkdir(parents=True)
        self.store = ConfigStore(self.data / 'config.json', initial=POLICY)
        self.labels = LabelsStore(self.data / 'labels.json')
        self.console = FakeConsole(self.data)
        self.repo = RepoManager(root / 'repo', base_url=REPO_BASE)

        def apply(raw):
            if fail_policy:
                raise RuntimeError('nft failed')
            self.store.update(raw, lambda cfg: None)
        self.service = BackupService(self.data, policy=self.store.snapshot, apply_policy=apply,
                                     labels=self.labels, console=self.console, repo=self.repo,
                                     clock=lambda: 1800000000)


def items(result):
    return {item['id']: item for item in result['items']}


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.a = Host(self.root / 'a')
        self.a.labels.set('domain:example.com', 'Example', 'Web')
        self.a.console.nas.save({'name': 'NAS', 'protocol': 'smb', 'host': '192.0.2.20', 'share': 'games',
                                 'username': 'alice', 'password': 's3cret-pw'})
        self.payload = b'\x7fELF' + b'p' * 3000
        self.a.repo.add('demo.elf', self.payload, name='Demo', version='1.0')
        self.a.repo.upgrade('demo.elf', 'demo_v2.elf', b'\x7fELF' + b'q' * 3000, version='2.0')

    def tearDown(self):
        self.tmp.cleanup()

    def restore_into(self, host, path, passphrase=''):
        upload = host.service.receive(io.BytesIO(path.read_bytes()), path.stat().st_size)
        return host.service.restore(upload, passphrase)

    def test_round_trip_restores_every_part_and_keeps_the_old_policy_as_previous(self):
        path, name = self.a.service.create()
        self.assertTrue(name.endswith('.tar.gz'))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        changed = {**POLICY, 'domains': ['other.example']}
        self.a.store.update(changed, lambda cfg: None)
        self.a.labels.set('domain:example.com', '', '')
        self.a.console.files.host = '192.0.2.99'
        result = self.restore_into(self.a, path)
        self.a.service.discard(path)
        states = {key: item['state'] for key, item in items(result).items()}
        self.assertEqual(states['policy'], 'restored')
        self.assertEqual(self.a.store.snapshot()['domains'], ['example.com'])
        self.assertEqual(self.a.store.previous['domains'], ['other.example'])
        self.assertEqual(self.a.labels.get('domain:example.com'), {'name': 'Example', 'category': 'Web'})
        self.assertEqual(self.a.console.remembered, '192.0.2.5')
        self.assertEqual(states['repo'], 'skipped', 'nothing new to add')
        self.assertEqual(result['manifest']['includes_key'], False)
        self.assertEqual(list((self.a.data / 'backup-tmp').iterdir()), [])

    def test_restore_without_key_leaves_nas_logins_unreadable_on_another_host(self):
        path, _ = self.a.service.create()
        b = Host(self.root / 'b')
        result = self.restore_into(b, path)
        self.assertIn('重新填写', items(result)['nas']['detail'])
        [conn] = b.console.nas.list_public()
        self.assertEqual((conn['host'], conn['username'], conn['has_password']), ('192.0.2.20', '', False))
        sealed = self.a.console.nas.document()['connections'][0]['credentials']
        self.assertNotIn(sealed, (b.data / 'nas.json').read_text())

    def test_encrypted_backup_restores_logins_and_key(self):
        with self.assertRaises(BackupError):
            self.a.service.create(include_key=True, passphrase='short')
        path, name = self.a.service.create(include_key=True, passphrase=PASSPHRASE)
        self.assertTrue(name.endswith('.ps5bak'))
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(backup.MAGIC))
        self.assertNotIn(b'nas.json', raw)
        self.assertNotIn(b'example.com', raw)
        b = Host(self.root / 'b')
        for wrong in ('', 'wrong passphrase!'):
            with self.subTest(passphrase=wrong):
                with self.assertRaises(BackupError):
                    self.restore_into(b, path, wrong)
        self.assertEqual(b.console.nas.list_public(), [])
        result = self.restore_into(b, path, PASSPHRASE)
        self.assertEqual(items(result)['nas']['state'], 'restored')
        conn = b.console.nas.get(b.console.nas.list_public()[0]['id'])
        self.assertEqual((conn['username'], conn['password']), ('alice', 's3cret-pw'))
        self.assertEqual((b.data / 'keys' / 'nas.key').read_bytes(), (self.a.data / 'keys' / 'nas.key').read_bytes())
        self.assertEqual((b.data / 'keys' / 'nas.key').stat().st_mode & 0o777, 0o600)

    def test_repository_merge_adds_missing_plugins_with_files_and_history(self):
        b = Host(self.root / 'b')
        b.repo.add('mine.elf', b'\x7fELF' + b'm' * 100, name='Mine')
        without, _ = self.a.service.create()
        result = self.restore_into(b, without)
        self.assertEqual(items(result)['repo']['state'], 'skipped')
        self.assertIn('没有包含插件文件', items(result)['repo']['detail'])
        path, _ = self.a.service.create(payloads=True)
        result = self.restore_into(b, path)
        self.assertEqual(items(result)['repo']['state'], 'restored')
        names = [item['filename'] for item in b.repo.catalog()['payloads']]
        self.assertEqual(names, ['mine.elf', 'demo_v2.elf'])
        self.assertEqual((b.repo.payloads / 'demo_v2.elf').read_bytes(), b'\x7fELF' + b'q' * 3000)
        entry = b.repo.catalog()['payloads'][1]
        self.assertEqual(entry['url'], REPO_BASE + 'demo_v2.elf')
        [record] = b.repo.history()[entry['id']]
        self.assertEqual((b.repo.versions / record['checksum']).read_bytes(), self.payload)
        again = self.restore_into(b, path)
        self.assertEqual(items(again)['repo']['state'], 'skipped', 'merging twice adds nothing')

    def test_failed_policy_leaves_the_current_one_and_other_parts_still_restore(self):
        path, _ = self.a.service.create()
        b = Host(self.root / 'b', fail_policy=True)
        b.store.update({**POLICY, 'domains': ['b.example']}, lambda cfg: None)
        result = items(self.restore_into(b, path))
        self.assertEqual(result['policy']['state'], 'failed')
        self.assertEqual(b.store.snapshot()['domains'], ['b.example'])
        self.assertEqual(result['labels']['state'], 'restored')

    # ---------- rejected archives ----------
    def rebuild(self, path, change):
        """Re-pack a backup after change(members: dict name -> bytes)."""
        members = {}
        with tarfile.open(path, 'r:gz') as tar:
            for member in tar:
                members[member.name] = tar.extractfile(member).read()
        change(members)
        out = path.with_name('changed.tar.gz')
        with tarfile.open(out, 'w:gz') as tar:
            for name, data in members.items():
                if isinstance(data, tarfile.TarInfo):
                    tar.addfile(data)
                    continue
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return out

    def assert_rejected(self, path, message):
        b = Host(self.root / ('b' + str(time.monotonic_ns())))
        before = b.store.snapshot()
        with self.assertRaises(BackupError) as caught:
            self.restore_into(b, path)
        self.assertIn(message, str(caught.exception))
        self.assertEqual(b.store.snapshot(), before)
        self.assertEqual(list((b.data / 'backup-tmp').iterdir()), [])

    def test_tampered_unknown_traversal_and_newer_archives_are_rejected(self):
        path, _ = self.a.service.create()

        def tamper(members):
            members['policy.json'] = json.dumps({**POLICY, 'domains': ['evil.example']}).encode()

        def unknown(members):
            members['evil.sh'] = b'#!/bin/sh'

        def traversal(members):
            members['../nas.key'] = b'x'

        def symlink(members):
            info = tarfile.TarInfo('nas.key')
            info.type, info.linkname = tarfile.SYMTYPE, '/etc/shadow'
            members['nas.key'] = info

        def newer(members):
            manifest = json.loads(members['manifest.json'])
            manifest['format'] = backup.FORMAT + 1
            members['manifest.json'] = json.dumps(manifest).encode()

        def unlisted(members):
            data = b'{}'
            members['notify.json'] = data

        for change, message in ((tamper, '校验失败'), (unknown, '不认识'), (traversal, '不认识'),
                                (symlink, '不认识'), (newer, '更新的版本'), (unlisted, '与清单不一致')):
            with self.subTest(change=change.__name__):
                self.assert_rejected(self.rebuild(path, change), message)
        garbage = path.with_name('garbage.tar.gz')
        garbage.write_bytes(b'not a tarball')
        self.assert_rejected(garbage, '不是有效的备份文件')

    def test_tampered_encrypted_backup_is_rejected(self):
        path, _ = self.a.service.create(include_key=True, passphrase=PASSPHRASE)
        data = bytearray(path.read_bytes())
        data[backup.HEADER + 10] ^= 1
        path.write_bytes(bytes(data))
        b = Host(self.root / 'b')
        with self.assertRaises(BackupError):
            self.restore_into(b, path, PASSPHRASE)
        self.assertEqual(b.console.nas.list_public(), [])

    def test_manifest_lists_every_member_with_its_digest(self):
        path, _ = self.a.service.create(payloads=True)
        with tarfile.open(path, 'r:gz') as tar:
            members = {m.name: tar.extractfile(m).read() for m in tar}
        manifest = json.loads(members.pop('manifest.json'))
        self.assertEqual(set(manifest['files']), set(members))
        for name, data in members.items():
            self.assertEqual(manifest['files'][name]['sha256'], hashlib.sha256(data).hexdigest())
        self.assertNotIn('nas.key', members)
        self.assertIn('repo/payloads/demo_v2.elf', members)
        for secret in (b'credentials.json', b'sessions.json', b's3cret-pw', b'alice'):
            self.assertFalse(any(secret in data for data in members.values()))

    def test_cleanup_removes_leftovers(self):
        (self.a.data / 'backup-tmp' / 'job-x').mkdir(parents=True)
        self.a.service.cleanup()
        self.assertFalse((self.a.data / 'backup-tmp').exists())


PASSWORD = 'a-long-test-password'


class BackupHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.host = Host(Path(self.tmp.name))
        self.host.labels.set('domain:example.com', 'Example', '')
        self.server = AdminServer(('127.0.0.1', 0), AdminHandler)
        self.server.auth = AuthManager(AuthManager.make_credentials(PASSWORD))
        self.server.repo = self.host.repo
        busy = []
        self.busy = busy
        console = self.host.console
        console.transfers = SimpleNamespace(snapshot=lambda: busy)
        console.files.job = lambda: None
        console.files.transfers = lambda: {}
        self.server.app = SimpleNamespace(settings=SimpleNamespace(subnet='127.0.0.1/32', host_ip='127.0.0.1'),
                                          backup=self.host.service, console=console)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.conn = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        self.conn.request('POST', '/api/login', json.dumps({'password': PASSWORD}),
                          {'Content-Type': 'application/json'})
        reply = self.conn.getresponse()
        self.cookie = reply.getheader('Set-Cookie').split(';')[0]
        self.csrf = json.loads(reply.read())['csrf']

    def tearDown(self):
        self.conn.close()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        self.conn.request(method, path, body, {'Cookie': self.cookie, **(headers or {})})
        reply = self.conn.getresponse()
        return reply, reply.read()

    def test_download_then_restore_over_one_connection(self):
        reply, data = self.call('GET', '/api/backup?payloads=0')
        self.assertEqual(reply.status, 200)
        self.assertIn('attachment; filename="ps5-jb-backup-', reply.getheader('Content-Disposition'))
        self.assertEqual(int(reply.getheader('Content-Length')), len(data))
        for _ in range(100):          # the server deletes it right after the last byte
            if not list((self.host.data / 'backup-tmp').iterdir()):
                break
            time.sleep(0.02)
        self.assertEqual(list((self.host.data / 'backup-tmp').iterdir()), [], 'sent file is deleted')
        self.host.labels.set('domain:example.com', '', '')
        reply, body = self.call('POST', '/api/backup/restore', data,
                                {'Content-Type': 'application/octet-stream'})
        self.assertEqual(reply.status, 403, 'CSRF token is required')
        reply, body = self.call('POST', '/api/backup/restore', data,
                                {'Content-Type': 'application/octet-stream', 'X-CSRF-Token': self.csrf})
        self.assertEqual(reply.status, 200, body)
        self.assertEqual(self.host.labels.get('domain:example.com')['name'], 'Example')

    def test_encrypted_download_uses_post_and_passphrase_header(self):
        headers = {'Content-Type': 'application/json', 'X-CSRF-Token': self.csrf}
        reply, body = self.call('POST', '/api/backup', json.dumps({'include_key': True, 'passphrase': 'short'}), headers)
        self.assertEqual(reply.status, 400)
        self.assertIn('至少', json.loads(body)['error'])
        passphrase = '中文口令也可以用的，对吧'
        reply, data = self.call('POST', '/api/backup', json.dumps({'include_key': True, 'passphrase': passphrase}),
                                headers)
        self.assertEqual(reply.status, 200)
        self.assertTrue(data.startswith(backup.MAGIC))
        upload = {'Content-Type': 'application/octet-stream', 'X-CSRF-Token': self.csrf}
        reply, body = self.call('POST', '/api/backup/restore', data, upload)
        self.assertEqual(reply.status, 400)
        self.assertIn('口令', json.loads(body)['error'])
        from urllib.parse import quote
        reply, body = self.call('POST', '/api/backup/restore', data,
                                {**upload, 'X-Backup-Passphrase': quote(passphrase)})
        self.assertEqual(reply.status, 200, body)

    def test_restore_is_refused_while_a_transfer_runs_or_when_too_large(self):
        self.busy.append({'state': 'running'})
        upload = {'Content-Type': 'application/octet-stream', 'X-CSRF-Token': self.csrf}
        reply, body = self.call('POST', '/api/backup/restore', b'x' * 10, upload)
        self.assertEqual(reply.status, 409)
        self.busy.clear()
        self.host.repo.MAX_UPLOAD = 1
        reply, body = self.call('POST', '/api/backup/restore', b'x' * 10, upload)
        self.assertEqual(reply.status, 413)


if __name__ == '__main__':
    unittest.main()
