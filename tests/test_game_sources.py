"""Read-only NAS source planning; no archive or game content is opened."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))

try:
    import game_sources
except ModuleNotFoundError:
    game_sources = None


class FakeClient:
    def __init__(self, files=(), directories=()):
        self.tree = {'/': True}
        self.calls = []
        for path in list(directories) + list(files):
            parts = path.strip('/').split('/')
            for index in range(1, len(parts)):
                self.tree['/' + '/'.join(parts[:index])] = True
            self.tree[path] = path in directories

    def list(self, path):
        self.calls.append(('list', path))
        prefix = path.rstrip('/') + '/'
        return [{'name': key[len(prefix):], 'path': key, 'dir': directory, 'size': 0}
                for key, directory in self.tree.items()
                if key.startswith(prefix) and key != path and '/' not in key[len(prefix):]]

    def stat(self, path):
        self.calls.append(('stat', path))
        if path not in self.tree:
            raise FileNotFoundError(path)
        return {'dir': self.tree[path], 'size': 0}

    def open(self, path):
        raise AssertionError('planner must not read file content')


class SourceTests(unittest.TestCase):
    def plan(self, client, paths):
        self.assertIsNotNone(game_sources, 'game_sources source planner is missing')
        return game_sources.plan_sources(client, paths)

    def rejects(self, client, paths, message):
        self.assertIsNotNone(game_sources, 'game_sources source planner is missing')
        with self.assertRaisesRegex(ValueError, message):
            self.plan(client, paths)

    def test_single_rar_with_no_stat_support_finds_neighbor_cheats(self):
        client = FakeClient(['/dl/Game PPSA12345.rar', '/dl/金手指.zip',
                             '/dl/CHEAT-v2.ZIP', '/dl/trainer.zip', '/dl/manual.zip'])
        client.stat = None
        result = self.plan(client, ['/dl/Game PPSA12345.rar'])
        self.assertEqual(result, {
            'kind': 'archive', 'path': '/dl/Game PPSA12345.rar', 'base': 'Game PPSA12345',
            'cheats': ['/dl/CHEAT-v2.ZIP', '/dl/trainer.zip', '/dl/金手指.zip'],
            'title_hint': 'PPSA12345', 'volumes': ['/dl/Game PPSA12345.rar']})

    def test_selected_later_volume_finds_first_and_all_volumes(self):
        client = FakeClient(['/dl/game.part03.rar', '/dl/game.part01.rar', '/dl/game.part02.rar'])
        result = self.plan(client, ['/dl/game.part03.rar'])
        self.assertEqual(result['path'], '/dl/game.part01.rar')
        self.assertEqual(result['base'], 'game')
        self.assertEqual(result['volumes'], ['/dl/game.part01.rar', '/dl/game.part02.rar',
                                              '/dl/game.part03.rar'])

    def test_multi_selected_volumes_and_explicit_zip_are_one_source(self):
        client = FakeClient(['/dl/game.part01.rar', '/dl/game.part02.rar', '/other/custom.zip'])
        result = self.plan(client, ['/dl/game.part02.rar', '/other/custom.zip',
                                    '/dl/game.part01.rar', '/dl/game.part01.rar'])
        self.assertEqual(result['kind'], 'archive')
        self.assertEqual(result['cheats'], ['/other/custom.zip'])
        self.assertEqual(len(result['volumes']), 2)

    def test_selected_rar_ignores_unselected_other_archive_group(self):
        client = FakeClient(['/dl/game.rar', '/dl/other.rar'])
        self.assertEqual(self.plan(client, ['/dl/game.rar'])['path'], '/dl/game.rar')

    def test_whole_folder_finds_rar_in_wrapper_and_cheats_beside_it(self):
        client = FakeClient(['/dl/PPSA23456/wrapper/game.part01.rar',
                             '/dl/PPSA23456/wrapper/game.part02.rar',
                             '/dl/PPSA23456/wrapper/game 金手指.zip',
                             '/dl/PPSA23456/readme.txt'])
        result = self.plan(client, ['/dl/PPSA23456'])
        self.assertEqual(result['path'], '/dl/PPSA23456/wrapper/game.part01.rar')
        self.assertEqual(result['title_hint'], 'PPSA23456')
        self.assertEqual(result['cheats'], ['/dl/PPSA23456/wrapper/game 金手指.zip'])

    def test_nested_extracted_dump_returns_actual_root_without_reading_metadata(self):
        for metadata in ('param.json', 'param.sfo'):
            with self.subTest(metadata=metadata):
                root = '/dl/CUSA01234/wrapper/dump'
                client = FakeClient([root + '/eboot.bin', root + '/sce_sys/' + metadata,
                                     '/dl/CUSA01234/update.pkg'])
                result = self.plan(client, ['/dl/CUSA01234'])
                self.assertEqual(result['kind'], 'folder')
                self.assertEqual(result['path'], root)
                self.assertEqual(result['base'], 'dump')
                self.assertEqual(result['title_hint'], 'CUSA01234')

    def test_folder_primary_content_wins_over_neighbor_packages(self):
        client = FakeClient(['/dl/game/eboot.bin', '/dl/game/sce_sys/param.json',
                             '/dl/update.pkg', '/dl/dlc.pkg'])
        self.assertEqual(self.plan(client, ['/dl'])['kind'], 'folder')

    def test_one_package_is_classified_without_execution(self):
        client = FakeClient(['/dl/wrapper/game.pkg', '/dl/readme.txt'])
        result = self.plan(client, ['/dl'])
        self.assertEqual(result['kind'], 'package')
        self.assertEqual(result['path'], '/dl/wrapper/game.pkg')
        self.assertEqual(result['base'], 'game')
        self.assertEqual(result['cheats'], [])

    def test_explicit_package_is_classified(self):
        client = FakeClient(['/dl/PPSA12345.pkg'])
        result = self.plan(client, ['/dl/PPSA12345.pkg'])
        self.assertEqual(result['kind'], 'package')
        self.assertEqual(result['title_hint'], 'PPSA12345')

    def test_multiple_packages_require_explicit_choice(self):
        self.rejects(FakeClient(['/dl/base.pkg', '/dl/update.pkg']), ['/dl'], '多个.*PKG')

    def test_missing_volume_rejected(self):
        self.rejects(FakeClient(['/dl/game.part02.rar']), ['/dl/game.part02.rar'], '缺少分卷')

    def test_different_selected_rar_groups_rejected(self):
        self.rejects(FakeClient(['/dl/a.rar', '/dl/b.rar']), ['/dl/a.rar', '/dl/b.rar'], '多个')

    def test_different_rar_groups_in_folder_rejected(self):
        self.rejects(FakeClient(['/dl/a.rar', '/dl/wrapper/b.rar']), ['/dl'], '多个')

    def test_archive_and_separate_game_root_rejected(self):
        client = FakeClient(['/dl/a.rar', '/dl/other/eboot.bin', '/dl/other/sce_sys/param.json'])
        self.rejects(client, ['/dl'], '多个')

    def test_multiple_game_roots_rejected(self):
        client = FakeClient(['/dl/a/eboot.bin', '/dl/a/sce_sys/param.json',
                             '/dl/b/eboot.bin', '/dl/b/sce_sys/param.sfo'])
        self.rejects(client, ['/dl'], '多个')

    def test_multiple_explicit_folders_rejected(self):
        self.rejects(FakeClient(directories=['/a', '/b']), ['/a', '/b'], '多个.*文件夹')

    def test_zip_and_unrecognizable_folder_rejected(self):
        client = FakeClient(['/dl/cheat.zip', '/dl/readme.txt'])
        for paths in (['/dl'], ['/dl/cheat.zip'], []):
            with self.subTest(paths=paths):
                self.rejects(client, paths, '游戏|选择')

    def test_mac_metadata_does_not_create_archive_or_game_ambiguity(self):
        client = FakeClient(['/dl/game.rar', '/dl/._other.rar',
                             '/dl/__MACOSX/other.rar', '/dl/__MACOSX/eboot.bin',
                             '/dl/__MACOSX/sce_sys/param.json', '/dl/._cheat.zip'])
        self.assertEqual(self.plan(client, ['/dl'])['path'], '/dl/game.rar')
        self.rejects(client, ['/dl/._other.rar'], '元数据')

    def test_incomplete_dump_is_not_a_game(self):
        self.rejects(FakeClient(['/dl/eboot.bin', '/dl/param.json']), ['/dl'], '游戏')

    def test_missing_selected_file_is_chinese_error(self):
        self.rejects(FakeClient(), ['/dl/missing.rar'], '找不到|不存在')

    def test_paths_reject_traversal_relative_and_controls_before_io(self):
        for path in ('../game.rar', 'relative/game.rar', '/dl/../game.rar',
                     '/dl/..\\game.rar', '/dl/evil\x00.rar'):
            with self.subTest(path=path):
                client = FakeClient()
                self.rejects(client, [path], '路径')
                self.assertEqual(client.calls, [])

    def test_listing_cannot_escape_its_directory(self):
        client = FakeClient(directories=['/dl'])
        client.list = lambda path: [{'name': 'game.rar', 'path': '/elsewhere/game.rar',
                                     'dir': False, 'size': 1}]
        self.rejects(client, ['/dl'], '路径')

    def test_entry_limit_cannot_hide_second_game(self):
        client = FakeClient(['/dl/game.rar'] + [f'/dl/file{i}' for i in range(6)])
        self.assertIsNotNone(game_sources, 'game_sources source planner is missing')
        with mock.patch.object(game_sources, 'MAX_ENTRIES', 5):
            self.rejects(client, ['/dl'], '上限|过多')

    def test_depth_limit_cannot_hide_second_game(self):
        client = FakeClient(['/dl/game.rar', '/dl/wrap/deeper/readme'])
        self.assertIsNotNone(game_sources, 'game_sources source planner is missing')
        with mock.patch.object(game_sources, 'MAX_DEPTH', 1):
            self.rejects(client, ['/dl'], '深度|上限')

    def test_directory_limit_cannot_hide_second_game(self):
        client = FakeClient(['/dl/game.rar'], directories=['/dl/a', '/dl/b'])
        self.assertIsNotNone(game_sources, 'game_sources source planner is missing')
        with mock.patch.object(game_sources, 'MAX_DIRECTORIES', 1):
            self.rejects(client, ['/dl'], '目录|上限')

    def test_known_nas_listing_cap_is_not_mistaken_for_complete_listing(self):
        client = FakeClient(['/dl/game.rar'] + [f'/dl/file{i}' for i in range(4999)])
        self.rejects(client, ['/dl'], '截断|上限')

    def test_explicit_truncated_listing_is_rejected(self):
        client = FakeClient(directories=['/dl'])
        client.list = lambda path: {'entries': [{'name': 'game.rar', 'path': '/dl/game.rar',
                                               'dir': False, 'size': 1}], 'truncated': True}
        self.rejects(client, ['/dl'], '截断|完整')


if __name__ == '__main__':
    unittest.main()
