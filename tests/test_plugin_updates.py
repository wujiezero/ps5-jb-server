"""Plugin repository: upstream update discovery, upgrade with history, rollback."""
import hashlib
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from plugin_updates import PluginUpdates, PluginUpdateError, match_upstream, local_is_newer  # noqa: E402
from server import RepoManager  # noqa: E402

BASE = 'http://192.0.2.20:8080/payloads/'
INDEX_URL = 'https://mirror.test/payloads_v2.json'


def sha(data):
    return hashlib.sha256(data).hexdigest()


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeOpener:
    def __init__(self):
        self.files = {}
        self.requests = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.requests.append(url)
        if url not in self.files:
            raise urllib.error.URLError('unreachable')
        return FakeResponse(self.files[url])


def index_entry(name, filename, data, version):
    return {'name': name, 'filename': filename, 'version': version, 'checksum': sha(data),
            'url': f'https://mirror.test/dl/{filename}', 'last_update': '2026-11-10',
            'category': 'Networking & Servers', 'source': f'https://example.test/{name}'}


class RepoHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'repo'
        self.repo = RepoManager(self.root, base_url=BASE)
        self.repo.add('ftpsrv_v0.21.elf', b'old-ftp', name='FTP Server', version='v0.21',
                      description='中文描述')

    def item(self, filename=None):
        items = self.repo.summary()['catalog']['items']
        return items[0] if filename is None else next(i for i in items if i['filename'] == filename)

    def test_upgrade_publishes_new_file_and_keeps_old_restorable(self):
        self.repo.upgrade('ftpsrv_v0.21.elf', 'ftpsrv_v0.22.elf', b'new-ftp', version='v0.22')
        item = self.item()
        self.assertEqual(item['filename'], 'ftpsrv_v0.22.elf')
        self.assertEqual(item['version'], 'v0.22')
        self.assertEqual(item['checksum'], sha(b'new-ftp'))
        self.assertEqual(item['url'], BASE + 'ftpsrv_v0.22.elf')
        self.assertEqual(item['description'], '中文描述', 'local metadata survives an upgrade')
        self.assertFalse((self.root / 'payloads/ftpsrv_v0.21.elf').exists(), 'old build is not served')
        self.assertEqual([r['version'] for r in item['history']], ['v0.21'])
        self.assertEqual((self.root / 'versions' / sha(b'old-ftp')).read_bytes(), b'old-ftp')
        # repo.json (what the PS5 reads) carries no history.
        catalog = json.loads((self.root / 'repo.json').read_text())
        self.assertNotIn('history', catalog['payloads'][0])

    def test_rollback_swaps_current_and_archived(self):
        self.repo.upgrade('ftpsrv_v0.21.elf', 'ftpsrv_v0.22.elf', b'new-ftp', version='v0.22')
        self.repo.rollback('ftpsrv_v0.22.elf', sha(b'old-ftp'))
        item = self.item()
        self.assertEqual((item['filename'], item['version']), ('ftpsrv_v0.21.elf', 'v0.21'))
        self.assertEqual((self.root / 'payloads/ftpsrv_v0.21.elf').read_bytes(), b'old-ftp')
        self.assertFalse((self.root / 'payloads/ftpsrv_v0.22.elf').exists())
        self.assertEqual([r['version'] for r in item['history']], ['v0.22'])
        # And forward again.
        self.repo.rollback('ftpsrv_v0.21.elf', sha(b'new-ftp'))
        self.assertEqual(self.item()['filename'], 'ftpsrv_v0.22.elf')

    def test_manual_replace_of_same_filename_is_also_reversible(self):
        self.repo.add('ftpsrv_v0.21.elf', b'patched', name='', replace=True)
        self.assertEqual(self.item()['history'][0]['checksum'], sha(b'old-ftp'))
        self.repo.rollback('ftpsrv_v0.21.elf', sha(b'old-ftp'))
        self.assertEqual((self.root / 'payloads/ftpsrv_v0.21.elf').read_bytes(), b'old-ftp')

    def test_history_is_bounded_and_pruned_from_disk(self):
        current = 'ftpsrv_v0.21.elf'
        for minor in range(22, 27):
            new = f'ftpsrv_v0.{minor}.elf'
            self.repo.upgrade(current, new, f'ftp-{minor}'.encode(), version=f'v0.{minor}')
            current = new
        history = self.item()['history']
        self.assertEqual(len(history), RepoManager.HISTORY_LIMIT)
        self.assertEqual(history[0]['version'], 'v0.25')
        stored = {p.name for p in (self.root / 'versions').iterdir() if p.name != 'index.json'}
        self.assertEqual(stored, {r['checksum'] for r in history})

    def test_delete_version_and_delete_plugin_clean_up(self):
        self.repo.upgrade('ftpsrv_v0.21.elf', 'ftpsrv_v0.22.elf', b'new-ftp', version='v0.22')
        self.repo.delete_version('ftpsrv_v0.22.elf', sha(b'old-ftp'))
        self.assertEqual(self.item()['history'], [])
        self.assertFalse((self.root / 'versions' / sha(b'old-ftp')).exists())
        self.repo.upgrade('ftpsrv_v0.22.elf', 'ftpsrv_v0.23.elf', b'newer', version='v0.23')
        self.repo.delete('ftpsrv_v0.23.elf')
        self.assertFalse((self.root / 'versions' / sha(b'new-ftp')).exists())
        with self.assertRaises(FileNotFoundError):
            self.repo.rollback('ftpsrv_v0.23.elf', sha(b'new-ftp'))

    def test_rollback_rejects_unknown_or_corrupt_versions(self):
        with self.assertRaises(FileNotFoundError):
            self.repo.rollback('ftpsrv_v0.21.elf', '../../etc/passwd')
        self.repo.upgrade('ftpsrv_v0.21.elf', 'ftpsrv_v0.22.elf', b'new-ftp', version='v0.22')
        (self.root / 'versions' / sha(b'old-ftp')).write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, '校验和'):
            self.repo.rollback('ftpsrv_v0.22.elf', sha(b'old-ftp'))
        self.assertEqual(self.item()['filename'], 'ftpsrv_v0.22.elf', 'nothing changed')

    def test_upgrade_refuses_a_filename_owned_by_another_plugin(self):
        self.repo.add('other.elf', b'other', name='Other')
        with self.assertRaises(FileExistsError):
            self.repo.upgrade('ftpsrv_v0.21.elf', 'other.elf', b'new', version='x')
        self.assertEqual((self.root / 'payloads/other.elf').read_bytes(), b'other')


class MatchingTests(unittest.TestCase):
    INDEX = [
        {'name': 'ftpsrv', 'filename': 'ftpsrv_v0.22.elf', 'checksum': 'a' * 64},
        {'name': 'ftpsrv-drakmor', 'filename': 'ftpsrv-drakmor_1.16-ng-stable.elf', 'checksum': 'b' * 64},
        {'name': 'zftpd', 'filename': 'zftpd_v1.7.0.elf', 'checksum': 'c' * 64},
        {'name': 'pldmgr', 'filename': 'pldmgr_v0.6.3.elf', 'checksum': 'd' * 64},
    ]

    def match(self, **item):
        entry = match_upstream(item, self.INDEX)
        return entry and entry['name']

    def test_mirror_style_and_vendor_style_filenames(self):
        self.assertEqual(self.match(filename='ftpsrv_v0.21.elf', name='FTP Server'), 'ftpsrv')
        self.assertEqual(self.match(filename='ftpsrv-drakmor_1.15.elf'), 'ftpsrv-drakmor')
        self.assertEqual(self.match(filename='zftpd-ps5-v1.6.0.elf'), 'zftpd')
        self.assertEqual(self.match(filename='Payload-Manager.elf', name='Payload Manager', id='pldmgr'), 'pldmgr')
        self.assertIsNone(self.match(filename='ftpsrv-custom.elf', name='My FTP'))
        self.assertEqual(self.match(filename='anything.elf', upstream='zftpd'), 'zftpd')

    def test_version_ordering_is_only_trusted_when_numeric(self):
        self.assertTrue(local_is_newer('v1.8.0', 'v1.7.0'))
        self.assertFalse(local_is_newer('2.5B', '2.5C'))
        self.assertFalse(local_is_newer('', 'v1'))
        self.assertFalse(local_is_newer('v1.13-rc2', 'v1.13-rc2'))


class PluginUpdatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = RepoManager(root / 'repo', base_url=BASE)
        self.repo.add('ftpsrv_v0.21.elf', b'old-ftp', name='FTP Server', version='v0.21')
        self.repo.add('zftpd_v1.8.0.elf', b'my-zftpd', name='zftpd', version='v1.8.0')
        self.repo.add('custom.elf', b'mine', name='Custom')
        self.opener = FakeOpener()
        self.new_ftp = b'new-ftp'
        self.entries = [index_entry('ftpsrv', 'ftpsrv_v0.22.elf', self.new_ftp, 'v0.22'),
                        index_entry('zftpd', 'zftpd_v1.7.0.elf', b'up-zftpd', 'v1.7.0')]
        self.publish_index()
        self.updates = PluginUpdates(self.repo, root / 'data/plugin-upstream.json',
                                     url=INDEX_URL, opener=self.opener)

    def publish_index(self):
        self.opener.files[INDEX_URL] = json.dumps({'name': 'mirror', 'payloads': self.entries}).encode()
        self.opener.files['https://mirror.test/dl/ftpsrv_v0.22.elf'] = self.new_ftp

    def test_status_before_any_check_is_empty_and_offline(self):
        status = self.updates.status()
        self.assertIsNone(status['checked_at'])
        self.assertEqual(status['items'], {})
        self.assertEqual(self.opener.requests, [], 'reading status never touches the network')

    def test_check_classifies_every_matched_plugin(self):
        status = self.updates.check()
        self.assertEqual(status['available'], 1)
        self.assertEqual(status['items']['ftpsrv_v0.21.elf']['state'], 'available')
        self.assertEqual(status['items']['ftpsrv_v0.21.elf']['version'], 'v0.22')
        self.assertEqual(status['items']['zftpd_v1.8.0.elf']['state'], 'local_newer')
        self.assertNotIn('custom.elf', status['items'])

    def test_apply_downloads_verifies_and_keeps_previous(self):
        self.updates.check()
        result = self.updates.apply('ftpsrv_v0.21.elf')
        self.assertEqual(result['filename'], 'ftpsrv_v0.22.elf')
        item = next(i for i in self.repo.summary()['catalog']['items'] if i['name'] == 'FTP Server')
        self.assertEqual(item['upstream'], 'ftpsrv')
        self.assertEqual(item['history'][0]['filename'], 'ftpsrv_v0.21.elf')
        self.assertEqual(self.updates.status()['items']['ftpsrv_v0.22.elf']['state'], 'current')
        with self.assertRaisesRegex(PluginUpdateError, '最新'):
            self.updates.apply('ftpsrv_v0.22.elf')

    def test_checksum_mismatch_changes_nothing(self):
        self.updates.check()
        self.opener.files['https://mirror.test/dl/ftpsrv_v0.22.elf'] = b'tampered'
        with self.assertRaisesRegex(PluginUpdateError, '校验和'):
            self.updates.apply('ftpsrv_v0.21.elf')
        filenames = [i['filename'] for i in self.repo.summary()['catalog']['items']]
        self.assertIn('ftpsrv_v0.21.elf', filenames)

    def test_failed_check_keeps_last_good_index_and_reports_error(self):
        self.updates.check()
        del self.opener.files[INDEX_URL]
        status = self.updates.check()
        self.assertIn('无法连接上游', status['error'])
        self.assertEqual(status['available'], 1, 'last good index still answers')

    def test_index_entries_must_be_https_with_checksums(self):
        self.entries = [{**self.entries[0], 'url': 'http://mirror.test/x.elf'},
                        {**self.entries[1], 'checksum': 'nope'}]
        self.publish_index()
        status = self.updates.check()
        self.assertIn('没有可用', status['error'])

    def test_apply_requires_a_check_first(self):
        with self.assertRaisesRegex(PluginUpdateError, '先检查'):
            self.updates.apply('ftpsrv_v0.21.elf')


if __name__ == '__main__':
    unittest.main()
