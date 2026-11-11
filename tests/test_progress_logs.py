"""Console plugin versions, one-click upgrade, progress reporting and the service log viewer."""
import hashlib
import io
import logging
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'server'), str(Path(__file__).resolve().parent)]
import logbook  # noqa: E402
import updates  # noqa: E402
from plugin_updates import PluginUpdates  # noqa: E402
from ps5 import HostManager, PldmgrClient, compare_installed, filename_version, guess_slot_key  # noqa: E402
from server import RepoManager  # noqa: E402
from test_pldmgr_upload import UploadHandler  # noqa: E402
from test_plugin_updates import FakeOpener, index_entry  # noqa: E402
from test_ps5 import FakeConfig  # noqa: E402


def sha(data):
    return hashlib.sha256(data).hexdigest()


class SlotNameTests(unittest.TestCase):
    def test_guess_drops_version_and_platform_suffixes(self):
        cases = {'zftpd.elf': 'zftpd', 'zftpd-ps5-v1.7.0.elf': 'zftpd', 'zftpd_v1.7.0.elf': 'zftpd',
                 'etaHEN_2.5B.bin': 'etahen', 'kstuff1.elf': 'kstuff1',
                 'ftpsrv-drakmor_1.16.elf': 'ftpsrv-drakmor',
                 'ps5-date-time-sync_v1.0.0.elf': 'ps5-date-time-sync'}
        for filename, key in cases.items():
            with self.subTest(filename=filename):
                self.assertEqual(guess_slot_key(filename), key)

    def test_version_comes_after_the_slot_name_without_platform(self):
        self.assertEqual(filename_version('zftpd-ps5-v1.6.0.elf', 'zftpd'), (1, 6, 0))
        self.assertEqual(filename_version('kstuff1.elf', 'kstuff1'), ())
        self.assertEqual(filename_version('zftpd.elf', 'zftpd'), ())


class CompareTests(unittest.TestCase):
    def item(self, filename='zftpd_v1.7.0.elf', version='v1.7.0', checksum='a' * 64):
        return {'filename': filename, 'version': version, 'checksum': checksum}

    def test_older_version_on_console_is_outdated(self):
        info = compare_installed(self.item(), {'filename': 'zftpd-ps5-v1.6.0.elf'}, key='zftpd')
        self.assertEqual((info['state'], info['installed_version'], info['repo_version']),
                         ('outdated', '1.6.0', 'v1.7.0'))
        self.assertTrue(info['confident'])

    def test_newer_build_on_console_is_not_offered_as_update(self):
        info = compare_installed(self.item(), {'filename': 'zftpd-ps5-v1.8.elf'}, key='zftpd')
        self.assertEqual(info['state'], 'newer')

    def test_unversioned_file_name_is_only_a_possibility(self):
        info = compare_installed(self.item(), {'filename': 'zftpd.elf'}, key='zftpd')
        self.assertEqual(info['state'], 'maybe')
        self.assertFalse(info['confident'])

    def test_install_record_decides_when_names_carry_no_version(self):
        item = self.item(filename='kstuff1.elf', version='', checksum='b' * 64)
        record = {'filename': 'kstuff1.elf', 'checksum': 'b' * 64, 'version': ''}
        self.assertEqual(compare_installed(item, {'filename': 'kstuff1.elf'}, record, 'kstuff1')['state'], 'current')
        record['checksum'] = 'c' * 64      # the repository build changed after we installed it
        self.assertEqual(compare_installed(item, {'filename': 'kstuff1.elf'}, record, 'kstuff1')['state'], 'outdated')

    def test_record_for_a_file_replaced_by_hand_is_ignored(self):
        record = {'filename': 'zftpd_v1.5.0.elf', 'checksum': 'a' * 64, 'version': 'v1.5.0'}
        info = compare_installed(self.item(), {'filename': 'zftpd-ps5-v1.6.0.elf'}, record, 'zftpd')
        self.assertEqual((info['state'], info['installed_version']), ('outdated', '1.6.0'))


class ConsoleUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), UploadHandler)
        self.server.calls, self.server.bodies = [], []
        self.server.has_existing, self.server.fail_write, self.server.publish = True, False, True
        self.server.items = [{'slot': 'local/zftpd', 'filename': 'zftpd-ps5-v1.6.0.elf',
                              'path': '/data/pldmgr/payloads/local/zftpd/zftpd-ps5-v1.6.0.elf'}]
        self.server.check_result = {'status': 'ok', 'key': 'zftpd', 'folder_exists': True}
        self.server.check_status = 200
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = b'\x7fELF-zftpd-1.7.0' * 5000
        self.catalog = [{'filename': 'zftpd_v1.7.0.elf', 'version': 'v1.7.0', 'checksum': sha(self.data),
                         'url': 'http://127.0.0.1/zftpd_v1.7.0.elf'},
                        {'filename': 'ps5-payload-repository', 'url': 'https://example.invalid/'}]
        config = FakeConfig(pldmgr_port=self.server.server_port)
        config.data_dir = Path(self.tmp.name)
        self.manager = HostManager(config, lambda: {'items': self.catalog}, fetch_bytes=lambda _: self.data)

    def test_upload_reports_progress_and_sends_exact_bytes(self):
        seen = []
        client = PldmgrClient('127.0.0.1', self.server.server_port)
        client.upload_payload('zftpd_v1.7.0.elf', self.data, progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(self.server.bodies[-1], self.data)
        self.assertEqual(seen[0], (0, len(self.data)))
        self.assertEqual(seen[-1], (len(self.data), len(self.data)))

    def test_compare_uses_pldmgr_slot_resolver_once(self):
        client = PldmgrClient('127.0.0.1', self.server.server_port)
        payloads = [dict(item) for item in self.server.items]
        for _ in range(3):
            updates_ = self.manager._compare('127.0.0.1', client, payloads, [{'pid': 77, 'name': 'zftpd-ps5-v1.6.0.elf'}])
        info = updates_['zftpd_v1.7.0.elf']
        self.assertEqual((info['state'], info['slot'], info['running']), ('outdated', 'local/zftpd', 77))
        checks = [call for call in self.server.calls if call[1] == '/manage:check']
        self.assertEqual(len(checks), 1, 'slot names are cached per host')
        self.assertNotIn('ps5-payload-repository', updates_)

    def test_install_records_build_so_the_slot_reads_current(self):
        with mock.patch.object(HostManager, 'host', return_value={'ip': '127.0.0.1'}):
            self.manager.install('127.0.0.1', 'zftpd_v1.7.0.elf')
        self.assertIsNone(self.manager.progress('127.0.0.1'), 'progress clears when done')
        reloaded = HostManager(self.manager.config, lambda: {'items': self.catalog})
        record = reloaded.installs['127.0.0.1 local/zftpd']
        self.assertEqual((record['filename'], record['checksum']), ('zftpd_v1.7.0.elf', sha(self.data)))
        client = PldmgrClient('127.0.0.1', self.server.server_port)
        info = reloaded._compare('127.0.0.1', client, self.server.items, [])['zftpd_v1.7.0.elf']
        self.assertEqual(info['state'], 'current')

    def test_upgrade_stops_running_old_build_then_starts_new_one(self):
        host = {'ip': '127.0.0.1', 'pldmgr': True,
                'updates': {'zftpd_v1.7.0.elf': {'state': 'outdated', 'running': 77,
                                                 'installed': 'zftpd-ps5-v1.6.0.elf', 'installed_version': '1.6.0'}}}
        stages = []
        original = self.manager._job

        def spy(ip, **changes):
            if 'stage' in changes:
                stages.append(changes['stage'])
            original(ip, **changes)

        with mock.patch.object(HostManager, 'host', return_value=host), \
                mock.patch.object(PldmgrClient, 'kill') as kill, \
                mock.patch.object(PldmgrClient, 'run') as run, \
                mock.patch('ps5.time.sleep'), mock.patch.object(self.manager, '_job', side_effect=spy):
            result = self.manager.upgrade('127.0.0.1', 'zftpd_v1.7.0.elf')
        kill.assert_called_once_with(77)
        run.assert_called_once()
        self.assertTrue(result['stopped'] and result['ran'] and result['upgraded'])
        self.assertEqual(stages, ['checking', 'stopping', 'reading', 'uploading', 'verifying', 'running'])
        self.assertIsNone(self.manager.progress('127.0.0.1'))

    def test_upgrade_of_idle_plugin_only_replaces_the_file(self):
        host = {'ip': '127.0.0.1', 'pldmgr': True,
                'updates': {'zftpd_v1.7.0.elf': {'state': 'outdated', 'running': None}}}
        with mock.patch.object(HostManager, 'host', return_value=host), \
                mock.patch.object(PldmgrClient, 'kill') as kill, mock.patch.object(PldmgrClient, 'run') as run:
            result = self.manager.upgrade('127.0.0.1', 'zftpd_v1.7.0.elf')
        kill.assert_not_called()
        run.assert_not_called()
        self.assertFalse(result['ran'])

    def test_upgrade_refuses_plugin_not_on_console(self):
        with mock.patch.object(HostManager, 'host', return_value={'ip': '127.0.0.1', 'pldmgr': True, 'updates': {}}):
            with self.assertRaises(ValueError):
                self.manager.upgrade('127.0.0.1', 'zftpd_v1.7.0.elf')
        self.assertEqual(self.server.bodies, [])


class PluginDownloadProgressTests(unittest.TestCase):
    def test_apply_reports_download_then_clears(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = RepoManager(Path(tmp.name) / 'repo')
        repo.add('ftpsrv_v0.21.elf', b'old-ftp', name='FTP Server', version='v0.21')
        new = b'new-ftp' * 40000
        opener = FakeOpener()
        entry = index_entry('ftpsrv', 'ftpsrv_v0.22.elf', new, 'v0.22')
        import json
        opener.files['https://mirror.test/payloads_v2.json'] = json.dumps({'payloads': [entry]}).encode()
        opener.files[entry['url']] = new
        plugin_updates = PluginUpdates(repo, Path(tmp.name) / 'cache.json',
                                       url='https://mirror.test/payloads_v2.json', opener=opener)
        plugin_updates.check()
        seen = []
        original = plugin_updates._report
        plugin_updates._report = lambda **changes: (seen.append(dict(changes)), original(**changes))
        plugin_updates.apply('ftpsrv_v0.21.elf')
        stages = [change['stage'] for change in seen if 'stage' in change]
        self.assertEqual(stages, ['downloading', 'verifying', 'publishing'])
        self.assertEqual(max(change.get('done') or 0 for change in seen), len(new))
        self.assertIsNone(plugin_updates.progress())


class ServiceDownloadProgressTests(unittest.TestCase):
    def test_default_fetch_writes_bytes_into_job_status(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        manager = updates.UpdateManager(tmp.name)
        manager.fetch = updates.fetch_bytes
        body = b'x' * (600 << 10)

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        opener = mock.Mock()
        opener.open.return_value = Response(body)
        with mock.patch('updates.netproxy.opener', return_value=opener):
            data = manager._download('f' * 32, '9.9.9', {'asset': 'https://github.com/x', 'size': len(body)})
        self.assertEqual(data, body)
        job = manager._read(manager.status_path)
        self.assertEqual((job['state'], job['bytes_done'], job['bytes_total']), ('downloading', len(body), len(body)))


class LogViewerTests(unittest.TestCase):
    def setUp(self):
        self.log = logging.getLogger('test-logbook')
        self.log.propagate = False
        self.log.setLevel(logging.INFO)
        self.memory = logbook.MemoryLog(capacity=5)
        self.log.addHandler(self.memory)
        self.addCleanup(self.log.removeHandler, self.memory)

    def test_query_filters_by_level_text_and_cursor(self):
        self.log.info('ps5 scan done')
        self.log.warning('NAS 连接失败')
        self.log.error('extract failed')
        result = self.memory.query(level='WARNING')
        self.assertEqual([entry['message'] for entry in result['entries']], ['NAS 连接失败', 'extract failed'])
        self.assertEqual(self.memory.query(text='nas')['matched'], 1)
        self.assertEqual(self.memory.query()['matched'], 3)
        latest = result['latest']
        self.log.error('later')
        self.assertEqual([entry['message'] for entry in self.memory.query(after=latest)['entries']], ['later'])

    def test_ring_is_bounded_and_keeps_tracebacks(self):
        for index in range(8):
            self.log.info('line %d', index)
        try:
            raise RuntimeError('boom')
        except RuntimeError:
            self.log.exception('crashed')
        result = self.memory.query()
        self.assertEqual(result['total'], 5)
        self.assertIn('RuntimeError: boom', result['entries'][-1]['message'])

    def test_file_text_joins_rotations_oldest_first(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'service.log'
        path.with_name('service.log.2').write_text('oldest\n')
        path.with_name('service.log.1').write_text('older\n')
        path.write_text('newest\n')
        self.assertEqual(logbook.file_text(path), 'oldest\nolder\nnewest\n')
        self.assertEqual(logbook.file_text(path, limit=10), 'newest\n')


if __name__ == '__main__':
    unittest.main()
