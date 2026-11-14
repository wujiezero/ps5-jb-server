"""Game migration between the console's volumes (internal /data <-> M.2 / USB)."""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from files import FileServiceError  # noqa: E402
from migrate import MigrationError, migration_target, volume_root  # noqa: E402
from nas import NasStore, TransferManager  # noqa: E402

GAME = '/data/games/PPSA01234'


class FakeConsole:
    """FileProxy stand-in: folders and files in dicts, a /fs/job-style copy job."""

    def __init__(self, free=None):
        self.host = '192.0.2.5'
        self.dirs = {'/data', '/data/games', GAME, GAME + '/sce_sys', '/mnt/ext0'}
        self.files = {GAME + '/eboot.bin': 3000, GAME + '/sce_sys/param.json': 200}
        self.free = free
        self.state = {'state': 'idle'}
        self.copy_steps = 3          # job polls before a copy finishes
        self.drop = None             # file the copy "forgets" (verification must notice)
        self.hold = threading.Event()
        self.hold.set()
        self.fail_delete = False
        self.calls = []

    # -- listing --
    def _children(self, path):
        prefix = path.rstrip('/') + '/'
        return {key[len(prefix):].split('/')[0] for key in list(self.dirs) + list(self.files)
                if key.startswith(prefix) and key != path}

    def list(self, path):
        if path not in self.dirs:
            raise FileServiceError('missing', status=404)
        entries = []
        for name in sorted(self._children(path)):
            full = path.rstrip('/') + '/' + name
            entries.append({'name': name, 'path': full, 'dir': full in self.dirs,
                            'size': self.files.get(full, 0)})
        return {'path': path, 'entries': entries}

    def volumes(self):
        return {'volumes': [{'path': '/data', 'free': self.free}, {'path': '/mnt/ext0', 'free': self.free}]}

    def folder_size(self, path):
        prefix = path + '/'
        sizes = [size for key, size in self.files.items() if key.startswith(prefix)]
        return {'path': path, 'size': sum(sizes), 'files': len(sizes)}

    # -- writes --
    def mkdir(self, path):
        self.calls.append(('mkdir', path))
        if path in self.dirs:
            raise FileServiceError('exists', status=409)
        self.dirs.add(path)

    def rename(self, source, target):
        self.calls.append(('rename', source, target))
        self._move_tree(source, target)

    def delete(self, path):
        self.calls.append(('delete', path))
        if self.fail_delete and path.startswith('/data'):
            raise FileServiceError('权限不足', status=403)
        self.dirs = {d for d in self.dirs if d != path and not d.startswith(path + '/')}
        self.files = {k: v for k, v in self.files.items() if k != path and not k.startswith(path + '/')}

    def _move_tree(self, source, target, keep=False):
        for d in [d for d in self.dirs if d == source or d.startswith(source + '/')]:
            self.dirs.add(target + d[len(source):])
            if not keep:
                self.dirs.discard(d)
        for k in [k for k in self.files if k.startswith(source + '/')]:
            if k == self.drop:
                continue
            self.files[target + k[len(source):]] = self.files[k]
            if not keep:
                del self.files[k]

    # -- console copy job --
    def copy(self, source, target, move=False):
        self.calls.append(('copy', source, target))
        if target in self.dirs:
            raise FileServiceError('exists', status=409)
        total = self.folder_size(source)['size']
        self.state = {'state': 'copying', 'kind': 'copy', 'name': source.rsplit('/', 1)[-1],
                      'done': 0, 'total': total}
        self.pending = (source, target, total, self.copy_steps)
        return {'ok': True, 'instant': False}

    def job(self):
        self.hold.wait(5)
        if self.state.get('state') == 'copying':
            source, target, total, left = self.pending
            if left <= 1:
                self._move_tree(source, target, keep=True)
                self.state = {**self.state, 'state': 'done', 'done': total, 'percent': 100}
            else:
                self.pending = (source, target, total, left - 1)
                self.state = {**self.state, 'done': total * (self.copy_steps - left + 1) // self.copy_steps}
        return dict(self.state)

    def cancel(self):
        self.calls.append(('cancel',))
        if self.state.get('state') == 'copying':
            source, target, _, _ = self.pending
            self.dirs.add(target)          # a partial copy stays behind
            self.state = {**self.state, 'state': 'canceled'}
        return {'ok': True}


def wait(manager, ident, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        job = manager.snapshot_one(ident)
        if job['state'] in ('done', 'done_with_warnings', 'failed', 'canceled'):
            for thread in threading.enumerate():
                if thread.name == 'nas-transfer':
                    thread.join(5)
            return manager.snapshot_one(ident)
        time.sleep(0.01)
    raise AssertionError('migration did not finish')


class TargetTests(unittest.TestCase):
    game = {'path': GAME, 'name': 'Demo'}

    def test_keeps_the_path_below_the_volume_by_default(self):
        self.assertEqual(migration_target(self.game, '/mnt/ext0'), '/mnt/ext0/games/PPSA01234')
        self.assertEqual(migration_target({'path': '/mnt/usb1/CUSA00001'}, '/data'), '/data/CUSA00001')

    def test_folder_choice(self):
        dirs = ('games', 'etaHEN/games')
        self.assertEqual(migration_target(self.game, '/mnt/ext0', '', dirs), '/mnt/ext0/PPSA01234')
        self.assertEqual(migration_target(self.game, '/mnt/ext0', 'etaHEN/games', dirs),
                         '/mnt/ext0/etaHEN/games/PPSA01234')
        with self.assertRaises(MigrationError):
            migration_target(self.game, '/mnt/ext0', '../etc', dirs)

    def test_rejects_same_volume_and_foreign_targets(self):
        for target in ('/data', '/etc', '/mnt/ext0/games', ''):
            with self.assertRaises(MigrationError, msg=target):
                migration_target(self.game, target)
        self.assertEqual(volume_root('/mnt/ext12/a'), '/mnt/ext12')
        self.assertIsNone(volume_root('/user/app/x'))


class MigrateJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.console = FakeConsole()
        self.store = NasStore(Path(self.tmp.name) / 'nas.json')

    def manager(self, **kwargs):
        manager = TransferManager(self.store, self.console, state_path=Path(self.tmp.name) / 'transfers.json',
                                  **kwargs)
        manager.MIGRATE_POLL = 0.01
        return manager

    def test_internal_to_external_copies_verifies_places_and_cleans_up(self):
        services = mock.Mock()
        services.register.return_value = {'state': 'done'}
        manager = self.manager(game_services=services)
        job = manager.start_migrate(GAME, '/mnt/ext0/games/PPSA01234', register=True,
                                    title='PPSA01234', name='Demo')
        job = wait(manager, job['id'])
        self.assertEqual(job['state'], 'done', job)
        self.assertEqual((job['bytes_total'], job['bytes_done'], job['files_total']), (3200, 3200, 2))
        self.assertIn('/mnt/ext0/games/PPSA01234/eboot.bin', self.console.files)
        self.assertNotIn(GAME, self.console.dirs)
        self.assertFalse(any('.migrate-' in d for d in self.console.dirs))
        # Copied into a hidden staging folder, then renamed into place.
        self.assertIn(('copy', GAME, '/mnt/ext0/games/.migrate-PPSA01234'), self.console.calls)
        self.assertIn(('rename', '/mnt/ext0/games/.migrate-PPSA01234', '/mnt/ext0/games/PPSA01234'),
                      self.console.calls)
        self.assertIn(('mkdir', '/mnt/ext0/games'), self.console.calls)
        services.register.assert_called_once_with(
            '192.0.2.5', {'title': 'PPSA01234', 'path': '/mnt/ext0/games/PPSA01234', 'name': 'Demo'})

    def test_verification_failure_keeps_the_source_and_removes_the_copy(self):
        self.console.drop = GAME + '/sce_sys/param.json'
        manager = self.manager()
        job = wait(manager, manager.start_migrate(GAME, '/mnt/ext0/games/PPSA01234')['id'])
        self.assertEqual(job['state'], 'failed')
        self.assertIn('校验没通过', job['error'])
        self.assertIn(GAME + '/sce_sys/param.json', self.console.files)
        self.assertFalse(any(d.startswith('/mnt/ext0/games/') for d in self.console.dirs))

    def test_existing_target_or_missing_space_stops_before_copying(self):
        self.console.dirs |= {'/mnt/ext0/games', '/mnt/ext0/games/PPSA01234'}
        manager = self.manager()
        job = wait(manager, manager.start_migrate(GAME, '/mnt/ext0/games/PPSA01234')['id'])
        self.assertEqual(job['state'], 'failed')
        self.assertIn('目标位置已有', job['error'])
        self.console.dirs -= {'/mnt/ext0/games/PPSA01234'}
        self.console.free = 4000      # game 3200 + 1 GiB margin does not fit
        job = wait(manager, manager.start_migrate(GAME, '/mnt/ext0/games/PPSA01234')['id'])
        self.assertIn('空间不足', job['error'])
        self.assertFalse([call for call in self.console.calls if call[0] == 'copy'])

    def test_leftover_staging_is_removed_even_when_listings_hide_dot_folders(self):
        self.console.dirs |= {'/mnt/ext0/games', '/mnt/ext0/games/.migrate-PPSA01234'}
        self.console.files['/mnt/ext0/games/.migrate-PPSA01234/old.bin'] = 7
        original = self.console.list

        def hiding(path):
            listed = original(path)
            return {**listed, 'entries': [e for e in listed['entries'] if not e['name'].startswith('.')]}
        self.console.list = hiding
        manager = self.manager()
        job = wait(manager, manager.start_migrate(GAME, '/mnt/ext0/games/PPSA01234')['id'])
        self.assertEqual(job['state'], 'done', job)
        self.assertNotIn('/mnt/ext0/games/PPSA01234/old.bin', self.console.files)
        self.assertIn(('delete', '/mnt/ext0/games/.migrate-PPSA01234'), self.console.calls)

    def test_keep_source_and_undeletable_source(self):
        manager = self.manager()
        job = wait(manager, manager.start_migrate(GAME, '/mnt/ext0/games/PPSA01234', keep_source=True)['id'])
        self.assertEqual(job['state'], 'done')
        self.assertIn(GAME + '/eboot.bin', self.console.files)
        self.console.delete('/mnt/ext0/games/PPSA01234')
        self.console.fail_delete = True
        job = wait(manager, manager.start_migrate(GAME, '/mnt/ext0/games/PPSA01234')['id'])
        self.assertEqual(job['state'], 'done_with_warnings')
        self.assertIn('原文件夹没删掉', job['source_error'])
        self.assertIn('/mnt/ext0/games/PPSA01234/eboot.bin', self.console.files)

    def test_registration_failure_is_a_warning(self):
        services = mock.Mock()
        services.register.side_effect = ValueError('引擎不可用')
        manager = self.manager(game_services=services)
        job = wait(manager, manager.start_migrate(GAME, '/mnt/ext0/games/PPSA01234', register=True,
                                                  title='PPSA01234')['id'])
        self.assertEqual(job['state'], 'done_with_warnings')
        self.assertIn('引擎不可用', job['registration_error'])

    def test_cancel_stops_the_console_copy_and_removes_the_partial_folder(self):
        self.console.copy_steps = 1000
        manager = self.manager()
        job = manager.start_migrate(GAME, '/mnt/ext0/games/PPSA01234')
        end = time.time() + 5
        while manager.snapshot_one(job['id'])['phase'] != 'copy' and time.time() < end:
            time.sleep(0.01)
        manager.cancel(job['id'])
        job = wait(manager, job['id'])
        self.assertEqual(job['state'], 'canceled')
        self.assertIn(('cancel',), self.console.calls)
        self.assertNotIn('/mnt/ext0/games/.migrate-PPSA01234', self.console.dirs)
        self.assertIn(GAME + '/eboot.bin', self.console.files)

    def test_console_copy_failure_and_retry(self):
        manager = self.manager()
        original = self.console.job

        def failing():
            state = original()
            return {**state, 'state': 'failed', 'error': 'No space left on device'}
        self.console.job = failing
        job = wait(manager, manager.start_migrate(GAME, '/mnt/ext0/games/PPSA01234')['id'])
        self.assertEqual(job['state'], 'failed')
        self.assertIn('No space left', job['error'])
        self.console.job = original
        self.console.state = {'state': 'idle'}
        retried = manager.retry(job['id'])
        self.assertEqual(retried['retry_of'], job['id'])
        self.assertEqual(wait(manager, retried['id'])['state'], 'done')

    def test_bad_requests(self):
        manager = self.manager()
        for source, target in ((GAME, '/data/other/PPSA01234'), (GAME, '/mnt/ext0/games/OTHER'),
                               ('/user/app/X', '/mnt/ext0/X'), (GAME, '/mnt/ext0')):
            with self.assertRaises(MigrationError, msg=(source, target)):
                manager.start_migrate(source, target)
        self.console.hold.clear()          # keep the first job busy
        try:
            first = manager.start_migrate(GAME, '/mnt/ext0/games/PPSA01234')
            with self.assertRaises(MigrationError):
                manager.start_migrate(GAME, '/mnt/ext0/games/PPSA01234')
        finally:
            self.console.hold.set()
        self.assertEqual(wait(manager, first['id'])['state'], 'done')


class MigrateApiTests(unittest.TestCase):
    def setUp(self):
        import test_console_api as TC
        import test_ps5 as TP
        from api_console import ConsoleAPI
        from library import Library
        self.TC = TC
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        settings = TP.FakeConfig(pldmgr_port=1, elf_port=1)
        settings.subnet = '127.0.0.1/32'
        settings.file_port = 1
        settings.host_ip = '127.0.0.1'
        settings.repo_port = 8080
        settings.max_upload = 1 << 20
        settings.ps5_control = True
        settings.file_manager = True
        settings.data_dir = Path(self.tmp.name)
        self.api = ConsoleAPI(settings, lambda: {'items': []})
        self.api.library = Library(FakeConsole(), self.tmp.name)
        self.api.transfers = mock.Mock()
        self.api.transfers.start_migrate.side_effect = lambda *a, **k: {'id': 'x', 'args': a, **k}

    def post(self, body):
        data, headers = self.TC.json_body(body)
        handler = self.TC.StubHandler('/api/library/migrate', data, headers)
        handler.send_body = lambda status, data, **kw: setattr(handler, 'result', (status, data))
        self.api.handle_post(handler, '/api/library/migrate')
        return handler.result

    def test_queues_one_job_per_game_with_registration_for_title_folders(self):
        status, data = self.post({'paths': [GAME], 'target': '/mnt/ext0', 'host': '192.0.2.5'})
        self.assertEqual(status, 200, data)
        self.api.transfers.start_migrate.assert_called_once_with(
            GAME, '/mnt/ext0/games/PPSA01234', register=True, keep_source=False, host='192.0.2.5',
            title='PPSA01234', name='PPSA01234')

    def test_rejects_unknown_games_volumes_and_folders(self):
        for body in ({'paths': ['/data/games/NOPE'], 'target': '/mnt/ext0'},
                     {'paths': [GAME], 'target': '/mnt/ext9'},
                     {'paths': [GAME], 'target': '/data'},
                     {'paths': [GAME], 'target': '/mnt/ext0', 'folder': 'secret'},
                     {'paths': [], 'target': '/mnt/ext0'},
                     {'paths': [GAME, GAME], 'target': '/mnt/ext0'}):
            status, data = self.post(body)
            self.assertEqual(status, 400, (body, data))
        self.api.transfers.start_migrate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
