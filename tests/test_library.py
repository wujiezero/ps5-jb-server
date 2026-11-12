"""Game library: one-level scan of the console's storage, sizes on demand, covers."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from files import FileServiceError  # noqa: E402
from library import Library, LibraryError, platform  # noqa: E402
from settings import clean_dirs  # noqa: E402

PNG = b'\x89PNG\r\n\x1a\n' + b'icon'


class Stream(io.BytesIO):
    pass


class FakeFiles:
    """Directories as {path: [(name, is_dir, mtime)]}; files as {path: bytes}."""

    def __init__(self, tree, blobs=None, volumes=('/data', '/mnt/ext0')):
        self.tree, self.blobs = tree, blobs or {}
        self.host = '192.0.2.5'
        self.vols = volumes
        self.lists, self.sizes, self.streams = [], [], []
        self.down = False

    def volumes(self):
        if self.down:
            raise FileServiceError('无法连接 PS5 文件服务')
        return {'volumes': [{'path': v, 'label': v.split('/')[-1], 'free': 1, 'total': 2} for v in self.vols]}

    def list(self, path):
        self.lists.append(path)
        if path not in self.tree:
            raise FileServiceError('missing', status=404)
        return {'path': path, 'entries': [{'name': n, 'path': f'{path}/{n}', 'dir': d, 'size': 0, 'mtime': m}
                                          for n, d, m in self.tree[path]]}

    def folder_size(self, path):
        self.sizes.append(path)
        return {'path': path, 'size': 1234, 'files': 5, 'folders': 1}

    def stream(self, path):
        self.streams.append(path)
        if path not in self.blobs:
            raise FileServiceError('not found', status=404)
        return Stream(self.blobs[path])


TREE = {
    '/data': [('CUSA12345', True, 10), ('notes', True, 1), ('games', True, 1), ('x.txt', False, 1)],
    '/data/games': [('PPSA01234-app', True, 20), ('My Homebrew', True, 30), ('junk', True, 40),
                    ('CUSA12345', True, 50), ('loose.bin', False, 1)],
    '/data/games/My Homebrew/sce_sys': [('param.json', False, 1)],
    '/data/games/junk/sce_sys': [('icon0.png', False, 1)],
    '/mnt/ext0': [('etaHEN', True, 1)],
    '/mnt/ext0/etaHEN/games': [('ELAS00001', True, 60)],
}


class Titles:
    def __init__(self):
        self.calls = []

    def lookup(self, ids=(), folders=()):
        self.calls.append((list(ids), list(folders)))
        return {'titles': {'CUSA12345': {'name': '某游戏'}}, 'pending': []}


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = [1000.0]
        self.files = FakeFiles(TREE, {'/data/CUSA12345/sce_sys/icon0.png': PNG,
                                      '/data/games/My Homebrew/sce_sys/icon0.png': b'GIF89a',
                                      '/data/games/PPSA01234-app/sce_sys/icon0.png': PNG + b'x' * (2 << 20)})
        self.titles = Titles()
        self.library = Library(self.files, self.tmp.name, titles=self.titles, clock=lambda: self.now[0])

    def paths(self, result):
        return [game['path'] for game in result['games']]

    def test_scan_lists_one_level_over_every_volume(self):
        result = self.library.scan()
        self.assertEqual(sorted(self.paths(result)), [
            '/data/CUSA12345', '/data/games/CUSA12345', '/data/games/My Homebrew',
            '/data/games/PPSA01234-app', '/mnt/ext0/etaHEN/games/ELAS00001'])
        by_path = {game['path']: game for game in result['games']}
        self.assertEqual(by_path['/data/CUSA12345']['name'], '某游戏')
        self.assertEqual(by_path['/data/CUSA12345']['platform'], 'PS4')
        self.assertEqual(by_path['/data/games/PPSA01234-app']['platform'], 'PS5')
        self.assertEqual(by_path['/mnt/ext0/etaHEN/games/ELAS00001']['volume'], '/mnt/ext0')
        self.assertEqual(by_path['/data/games/My Homebrew']['title'], '')
        self.assertIsNone(by_path['/data/CUSA12345']['size'])
        self.assertNotIn('/data/CUSA12345/sce_sys', self.files.lists, 'title-ID folders are not opened')
        self.assertFalse([p for p in self.files.lists if p.startswith('/data/CUSA12345')])
        self.assertEqual(result['errors'], [])

    def test_scan_is_cached_for_a_minute(self):
        self.library.scan()
        count = len(self.files.lists)
        self.assertTrue(self.library.scan()['cached'])
        self.assertEqual(len(self.files.lists), count)
        self.now[0] += 61
        self.assertFalse(self.library.scan()['cached'])
        self.assertFalse(self.library.scan(refresh=True)['cached'])
        self.library.forget()
        self.assertFalse(self.library.scan()['cached'])

    def test_unreachable_console_raises_a_readable_error(self):
        self.files.down = True
        with self.assertRaisesRegex(FileServiceError, '无法连接'):
            self.library.scan()

    def test_size_is_cached_by_path_and_mtime(self):
        first = self.library.size('/data/CUSA12345')
        self.assertEqual((first['size'], first['cached']), (1234, False))
        self.assertTrue(self.library.size('/data/CUSA12345/')['cached'])
        self.assertEqual(self.files.sizes, ['/data/CUSA12345'])
        saved = json.loads((Path(self.tmp.name) / 'library-cache.json').read_text())
        self.assertEqual(saved['sizes']['/data/CUSA12345']['mtime'], 10)
        self.assertEqual({g['path']: g['size'] for g in self.library.scan(refresh=True)['games']}['/data/CUSA12345'], 1234)
        # The folder changed: the cached size no longer applies.
        TREE_CHANGED = dict(TREE, **{'/data': [('CUSA12345', True, 11)]})
        self.files.tree = TREE_CHANGED
        self.library.forget()
        self.assertFalse(self.library.size('/data/CUSA12345')['cached'])
        self.files.tree = TREE

    def test_size_and_icon_only_for_scanned_games(self):
        for path in ('/data/notes', '/etc', '/data/games/junk', None):
            with self.subTest(path=path), self.assertRaises(LibraryError):
                self.library.size(path)
        with self.assertRaises(LibraryError):
            self.library.icon('/data/notes')
        self.assertEqual(self.files.sizes, [])

    def test_icon_is_a_checked_png_and_cached(self):
        data, etag = self.library.icon('/data/CUSA12345')
        self.assertEqual(data, PNG)
        self.assertTrue(etag.startswith('"'))
        self.assertEqual(self.library.icon('/data/CUSA12345'), (data, etag))
        self.assertEqual(self.files.streams.count('/data/CUSA12345/sce_sys/icon0.png'), 1)
        icons = list((Path(self.tmp.name) / 'library-icons').glob('*.png'))
        self.assertEqual(len(icons), 1)
        self.assertEqual(icons[0].stat().st_mode & 0o777, 0o600)

    def test_icon_rejects_non_png_large_and_missing(self):
        with self.assertRaisesRegex(LibraryError, 'PNG'):
            self.library.icon('/data/games/My Homebrew')
        with self.assertRaisesRegex(LibraryError, '2 MiB'):
            self.library.icon('/data/games/PPSA01234-app')
        with self.assertRaisesRegex(LibraryError, '封面'):
            self.library.icon('/mnt/ext0/etaHEN/games/ELAS00001')

    def test_title_lookup_failure_keeps_folder_names(self):
        self.library.titles = mock.Mock(lookup=mock.Mock(side_effect=RuntimeError('offline')))
        names = {g['path']: g['name'] for g in self.library.scan()['games']}
        self.assertEqual(names['/data/CUSA12345'], 'CUSA12345')

    def test_one_broken_volume_is_reported_not_fatal(self):
        original = self.files.list

        def flaky(path):
            if path.startswith('/mnt/ext0'):
                raise FileServiceError('读取超时')
            return original(path)
        self.files.list = flaky
        result = self.library.scan()
        self.assertTrue(result['games'])
        self.assertEqual(len(result['errors']), 1)
        self.assertIn('读取超时', result['errors'][0])

    def test_platform_and_dir_settings(self):
        self.assertEqual((platform('CUSA00001'), platform('PPSA00001'), platform('NPXS20001'), platform('')),
                         ('PS4', 'PS5', '', ''))
        self.assertEqual(clean_dirs(['games', '/x/', '../etc', 'a/../b', 'ok dir', 'games']), ['games', 'x', 'ok dir'])
        self.assertEqual(clean_dirs('a,b'), ['a', 'b'])
        self.assertEqual(clean_dirs(None), ['games', 'homebrew', 'etaHEN/games'])


class LibraryApiTests(unittest.TestCase):
    """The /api/library routes on ConsoleAPI, with the library faked."""

    def setUp(self):
        import test_console_api as TC
        self.TC = TC
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from api_console import ConsoleAPI
        import test_ps5 as TP
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
        self.api.library = Library(FakeFiles(TREE, {'/data/CUSA12345/sce_sys/icon0.png': PNG}), self.tmp.name)

    def get(self, path, headers=None):
        handler = self.TC.StubHandler(path, headers=headers)
        handler.send_body = lambda status, data, **kw: setattr(handler, 'result', (status, data, kw))
        self.assertTrue(self.api.handle_get(handler))
        return handler.result

    def test_list_size_icon(self):
        status, data, _ = self.get('/api/library')
        self.assertEqual(status, 200)
        self.assertEqual(data['game_dirs'], ['games', 'homebrew', 'etaHEN/games'])
        status, data, _ = self.get('/api/library/size?path=%2Fdata%2FCUSA12345')
        self.assertEqual((status, data['size']), (200, 1234))
        status, data, extra = self.get('/api/library/icon?path=%2Fdata%2FCUSA12345')
        self.assertEqual((status, data, extra['ctype']), (200, PNG, 'image/png'))
        status, _, _ = self.get('/api/library/icon?path=%2Fdata%2FCUSA12345', {'If-None-Match': extra['etag']})
        self.assertEqual(status, 304)
        status, data, _ = self.get('/api/library/icon?path=%2Fdata%2Fgames%2FMy%20Homebrew')
        self.assertEqual(status, 404)
        status, data, _ = self.get('/api/library/size?path=%2Fetc')
        self.assertEqual(status, 400)

    def test_console_down_is_502_not_500(self):
        self.api.library.files.down = True
        status, data, _ = self.get('/api/library?refresh=1')
        self.assertEqual(status, 502)
        self.assertIn('无法连接', data['error'])

    def test_register_requires_a_title_id_folder(self):
        calls = []
        self.api.game_services = mock.Mock(register=lambda host, game: calls.append((host, game)) or {'state': 'done'})
        for path, status in (('/data/games/My Homebrew', 400), ('/data/games/PPSA01234-app', 400),
                             ('/data/CUSA12345', 200)):
            data, headers = self.TC.json_body({'path': path, 'host': '192.0.2.5'})
            handler = self.TC.StubHandler('/api/library/register', data, headers)
            self.api.handle_post(handler, '/api/library/register')
            self.assertEqual(handler.status, status, path)
        self.assertEqual(calls, [('192.0.2.5', {'title': 'CUSA12345', 'path': '/data/CUSA12345', 'name': 'CUSA12345'})])

    def test_file_manager_off_disables_the_library(self):
        self.api.file_manager = False
        handler = self.TC.StubHandler('/api/library')
        self.api.handle_get(handler)
        self.assertEqual(handler.status, 403)


if __name__ == '__main__':
    unittest.main()
