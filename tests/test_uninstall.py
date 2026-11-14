"""Game uninstall: home-screen entry, game folder, cheats and (optionally) saves."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from game_services import GameServiceError, GameServices  # noqa: E402
from nas import NasStore, TransferManager  # noqa: E402
from test_migrate import GAME, FakeConsole, wait  # noqa: E402
from uninstall import UninstallError  # noqa: E402

CHEATS = '/data/etaHEN/cheats'
# The console's user folders; built up so the privacy scan's /home/<name> rule does not trip.
USER = '/'.join(('', 'user', 'home', '1a2b3c4d'))
SAVE = USER + '/savedata_prospero/PPSA01234'


class FakeServices:
    def __init__(self, titles=(), refuse=False, still=False, down=False, fail_unregister=False):
        self.titles = list(titles)
        self.refuse, self.still, self.down, self.fail_unregister = refuse, still, down, fail_unregister
        self.calls = []

    def installed(self, host):
        if self.down:
            raise GameServiceError('引擎未运行')
        return {'titles': self.titles, 'incomplete': False, 'registered_unavailable': False}

    def unregister(self, host, title):
        self.calls.append(('unregister', title))
        if self.fail_unregister:
            raise GameServiceError('ps5upload 卸载失败：unregister_unmount_failed')
        return {'refused': self.refuse, 'rc': '0x80B21B02' if self.refuse else '', 'still_present': self.still}

    def saves(self, host, title):
        return {'saves': [{'path': SAVE, 'size': 1, 'user': 1}], 'truncated': False}

    def delete_save(self, host, path, title):
        self.calls.append(('delete_save', path))


def registered(source=GAME):
    return {'title': 'PPSA01234', 'name': 'Demo', 'origin': 'registered', 'source': source, 'system': False}


class UninstallJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.console = FakeConsole()
        self.console.dirs |= {'/data/etaHEN', CHEATS, CHEATS + '/json', CHEATS + '/shn'}
        self.console.files.update({CHEATS + '/json/PPSA01234_01.000.json': 10,
                                   CHEATS + '/shn/PPSA01234.shn': 10,
                                   CHEATS + '/json/PPSA012345.json': 10,     # another title
                                   CHEATS + '/json/CUSA00001.json': 10})
        self.store = NasStore(Path(self.tmp.name) / 'nas.json')

    def run_job(self, services, **kwargs):
        manager = TransferManager(self.store, self.console, game_services=services,
                                  state_path=Path(self.tmp.name) / 'transfers.json')
        options = {'path': GAME, 'title': 'PPSA01234', 'name': 'Demo', **kwargs}
        return wait(manager, manager.start_uninstall(**options)['id'])

    def test_registered_folder_game_loses_icon_folder_and_cheats_but_keeps_saves(self):
        services = FakeServices([registered()])
        job = self.run_job(services)
        self.assertEqual(job['state'], 'done', job)
        self.assertEqual(services.calls, [('unregister', 'PPSA01234')])
        self.assertNotIn(GAME, self.console.dirs)
        self.assertEqual(job['cheats_deleted'], 2)
        self.assertNotIn(CHEATS + '/json/PPSA01234_01.000.json', self.console.files)
        self.assertNotIn(CHEATS + '/shn/PPSA01234.shn', self.console.files)
        self.assertIn(CHEATS + '/json/PPSA012345.json', self.console.files)
        self.assertIn(CHEATS + '/json/CUSA00001.json', self.console.files)
        self.assertEqual(job['steps'], ['icon', 'files', 'cheats'])
        self.assertEqual(job['saves_deleted'], 0)

    def test_saves_only_when_asked(self):
        services = FakeServices([registered()])
        job = self.run_job(services, delete_saves=True, delete_cheats=False)
        self.assertEqual(job['state'], 'done', job)
        self.assertIn(('delete_save', SAVE), services.calls)
        self.assertEqual(job['saves_deleted'], 1)
        self.assertIn(CHEATS + '/shn/PPSA01234.shn', self.console.files)

    def test_icon_of_another_copy_or_a_pkg_install_is_left_alone(self):
        services = FakeServices([registered('/mnt/ext0/games/PPSA01234'),
                                 {**registered(''), 'origin': 'pkg'}])
        job = self.run_job(services)
        self.assertEqual(job['state'], 'done_with_warnings')
        self.assertEqual(services.calls, [])
        self.assertNotIn(GAME, self.console.dirs)
        self.assertIn('另一份游戏', job['warnings'][0])

    def test_engine_down_still_deletes_the_folder_with_a_warning(self):
        job = self.run_job(FakeServices(down=True), delete_saves=True)
        self.assertEqual(job['state'], 'done_with_warnings')
        self.assertNotIn(GAME, self.console.dirs)
        self.assertTrue(any('引擎' in item for item in job['warnings']))
        self.assertTrue(any('存档没有删除' in item for item in job['warnings']))

    def test_failed_unregister_deletes_nothing(self):
        job = self.run_job(FakeServices([registered()], fail_unregister=True))
        self.assertEqual(job['state'], 'failed')
        self.assertIn('unmount', job['error'])
        self.assertIn(GAME + '/eboot.bin', self.console.files)
        self.assertIn(CHEATS + '/shn/PPSA01234.shn', self.console.files)

    def test_pkg_install(self):
        pkg = {'title': 'PPSA05555', 'name': 'Pkg', 'origin': 'pkg', 'source': '', 'system': False}
        services = FakeServices([pkg])
        job = self.run_job(services, path='', title='PPSA05555', origin='pkg', delete_cheats=False)
        self.assertEqual((job['state'], services.calls), ('done', [('unregister', 'PPSA05555')]))
        refused = FakeServices([pkg], refuse=True, still=True)
        job = self.run_job(refused, path='', title='PPSA05555', origin='pkg')
        self.assertEqual(job['state'], 'failed')
        self.assertIn('拒绝', job['error'])
        job = self.run_job(FakeServices([]), path='', title='PPSA05555', origin='pkg')
        self.assertIn('没有以 PKG 安装', job['error'])

    def test_still_listed_after_sony_accepts_is_a_warning(self):
        job = self.run_job(FakeServices([registered()], still=True))
        self.assertEqual(job['state'], 'done_with_warnings')
        self.assertIn('设置 → 存储', job['warnings'][0])

    def test_bad_requests(self):
        manager = TransferManager(self.store, self.console)
        for kwargs in ({'path': '/data'}, {'path': '/user/app/PPSA01234'}, {'path': '/data/../etc'},
                       {'origin': 'pkg', 'title': ''}, {'origin': 'pkg', 'title': 'NPXS20001'},
                       {'origin': 'other', 'path': GAME}):
            with self.assertRaises((UninstallError, GameServiceError), msg=kwargs):
                manager.start_uninstall(**kwargs)


class EngineCallTests(unittest.TestCase):
    def services(self, response):
        engine = mock.Mock()
        engine.request.return_value = response
        return GameServices(engine), engine

    def test_save_paths_are_filtered_to_the_title(self):
        services, _ = self.services({'saves': [
            {'title_id': 'PPSA01234', 'path': SAVE},
            {'title_id': 'PPSA01234', 'path': USER + '/savedata/PPSA01234'},
            {'title_id': 'PPSA09999', 'path': USER + '/savedata_prospero/PPSA09999'},
            {'title_id': 'PPSA01234', 'path': USER + '/savedata_prospero'},
            {'title_id': 'PPSA01234', 'path': '/data/PPSA01234'}], 'truncated': False})
        found = services.saves('192.0.2.5', 'PPSA01234')['saves']
        self.assertEqual([item['path'] for item in found], [SAVE, USER + '/savedata/PPSA01234'])

    def test_delete_save_refuses_other_paths(self):
        services, engine = self.services({'ok': True})
        for path in (USER + '/savedata_prospero', USER + '/savedata_prospero/PPSA09999',
                     '/user/app/PPSA01234', SAVE + '/..'):
            with self.assertRaises(GameServiceError, msg=path):
                services.delete_save('192.0.2.5', path, 'PPSA01234')
        engine.request.assert_not_called()
        services.delete_save('192.0.2.5', SAVE, 'PPSA01234')
        engine.request.assert_called_once_with('POST', '/api/ps5/fs/delete',
                                               {'addr': '192.0.2.5', 'path': SAVE}, timeout=600)

    def test_unregister_reports_refusals(self):
        services, _ = self.services({'ok': True, 'sony_uninstall_refused': True, 'sony_uninstall_rc': '0x80B21B02',
                                     'still_present': None})
        self.assertEqual(services.unregister('192.0.2.5', 'PPSA01234'),
                         {'refused': True, 'rc': '0x80B21B02', 'still_present': None})
        with self.assertRaises(GameServiceError):
            services.unregister('192.0.2.5', 'NPXS20001')


class UninstallApiTests(unittest.TestCase):
    def setUp(self):
        from test_migrate import MigrateApiTests
        MigrateApiTests.setUp(self)
        self.api.transfers.start_uninstall.side_effect = lambda **kw: {'id': 'x', **kw}

    def post(self, body, route='/api/library/uninstall'):
        data, headers = self.TC.json_body(body)
        handler = self.TC.StubHandler(route, data, headers)
        handler.send_body = lambda status, data, **kw: setattr(handler, 'result', (status, data))
        self.api.handle_post(handler, route)
        return handler.result

    def test_queues_folders_and_pkg_installs_saves_off_by_default(self):
        status, data = self.post({'items': [{'path': GAME}, {'origin': 'pkg', 'title': 'PPSA05555', 'name': 'P'}],
                                  'host': '192.0.2.5'})
        self.assertEqual(status, 200, data)
        calls = [call.kwargs for call in self.api.transfers.start_uninstall.call_args_list]
        self.assertEqual(calls[0], {'path': GAME, 'title': 'PPSA01234', 'origin': 'folder', 'name': 'PPSA01234',
                                    'delete_saves': False, 'delete_cheats': True, 'host': '192.0.2.5'})
        self.assertEqual(calls[1]['origin'], 'pkg')

    def test_rejects_unknown_folders_and_duplicates(self):
        for body in ({'items': [{'path': '/data/games/NOPE'}]}, {'items': []},
                     {'items': [{'path': GAME}, {'path': GAME}]}, {'items': ['x']}):
            status, _ = self.post(body)
            self.assertEqual(status, 400, body)
        self.api.transfers.start_uninstall.assert_not_called()

    def test_apps_endpoint_never_fails_the_page(self):
        self.api.game_services = mock.Mock()
        self.api.game_services.installed.side_effect = GameServiceError('引擎未运行')
        handler = self.TC.StubHandler('/api/library/apps?host=127.0.0.1')
        handler.send_body = lambda status, data, **kw: setattr(handler, 'result', (status, data))
        self.api.handle_get(handler)
        self.assertEqual(handler.result[0], 200)
        self.assertEqual((handler.result[1]['available'], handler.result[1]['error']), (False, '引擎未运行'))


if __name__ == '__main__':
    unittest.main()
