"""Game library: ShadowMount+ scan paths, folder and image games, sizes on demand, covers."""
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
from library import SMP_CONFIG, Library, LibraryError, image_type, parse_smp_config, platform  # noqa: E402
from settings import clean_dirs  # noqa: E402

PNG = b'\x89PNG\r\n\x1a\n' + b'icon'
JPEG = b'\xff\xd8\xff\xe0' + b'jpeg'
CONFIG = b"""[config]
version = 2

[scan]
path = /data/homebrew   # internal
path = "/mnt/ext0/games/"
path = /mnt/shadowmnt
path = /mnt/smb/share
path=/mnt/usb3/games
"""


class Stream(io.BytesIO):
    pass


class FakeFiles:
    """Directories as {path: [(name, is_dir, mtime[, size])]}; files as {path: bytes}."""

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
        return {'path': path, 'entries': [{'name': item[0], 'path': f'{path}/{item[0]}', 'dir': item[1],
                                           'size': item[3] if len(item) > 3 else 0, 'mtime': item[2]}
                                          for item in self.tree[path]]}

    def folder_size(self, path):
        self.sizes.append(path)
        return {'path': path, 'size': 1234, 'files': 5, 'folders': 1}

    def stream(self, path):
        self.streams.append(path)
        if path not in self.blobs:
            raise FileServiceError('not found', status=404)
        return Stream(self.blobs[path])


TREE = {
    '/data': [('CUSA12345', True, 10), ('homebrew', True, 1), ('shadowmount', True, 1)],
    '/data/homebrew': [('PPSA01234-app', True, 20), ('My Homebrew', True, 30), ('junk', True, 40),
                       ('CUSA12345', True, 50), ('loose.bin', False, 1), ('PPSA05555.FFPFSC', False, 5, 9000),
                       ('.hidden.ffpfsc', False, 1, 1), ('backports', True, 1), ('nested', True, 1),
                       ('README.md', False, 1, 10)],
    '/data/homebrew/My Homebrew/sce_sys': [('param.json', False, 1)],
    '/data/homebrew/junk/sce_sys': [('icon0.png', False, 1)],
    '/data/homebrew/nested': [('PPSA07777', True, 3), ('deep.exfat', False, 4, 400)],
    '/data/homebrew/backports': [('PPSA08888', True, 1)],
    '/mnt/ext0': [('games', True, 1), ('PPSA00002', True, 1)],
    '/mnt/ext0/games': [('ELAS00001', True, 60), ('PPSA01476.ffpfsc', False, 70, 37000)],
    '/data/shadowmount/icon-cache': [('PPSA01476-v3-256-old.jpg', False, 1), ('PPSA01476-v3-256-new.jpg', False, 2),
                                     ('ELAS00001-v3-256-x.png', False, 1), ('notes.txt', False, 1)],
}
BLOBS = {SMP_CONFIG: CONFIG,
         '/data/homebrew/CUSA12345/sce_sys/icon0.png': PNG,
         '/data/homebrew/My Homebrew/sce_sys/icon0.png': b'GIF89a',
         '/data/homebrew/PPSA01234-app/sce_sys/icon0.png': PNG + b'x' * (2 << 20),
         '/data/shadowmount/icon-cache/PPSA01476-v3-256-new.jpg': JPEG}


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
        self.files = FakeFiles(dict(TREE), dict(BLOBS))
        self.titles = Titles()
        self.library = Library(self.files, self.tmp.name, titles=self.titles, clock=lambda: self.now[0])

    def paths(self, result):
        return sorted(game['path'] for game in result['games'])

    def config(self, text):
        self.files.blobs[SMP_CONFIG] = text.encode()
        self.library.forget()

    def test_scan_follows_the_shadowmount_config(self):
        result = self.library.scan()
        self.assertEqual(self.paths(result), [
            '/data/homebrew/CUSA12345', '/data/homebrew/My Homebrew', '/data/homebrew/PPSA01234-app',
            '/data/homebrew/PPSA05555.FFPFSC', '/mnt/ext0/games/ELAS00001', '/mnt/ext0/games/PPSA01476.ffpfsc'])
        by_path = {game['path']: game for game in result['games']}
        self.assertEqual(by_path['/data/homebrew/CUSA12345']['name'], '某游戏')
        self.assertEqual(by_path['/data/homebrew/CUSA12345']['kind'], 'folder')
        self.assertEqual(by_path['/data/homebrew/PPSA01234-app']['platform'], 'PS5')
        image = by_path['/mnt/ext0/games/PPSA01476.ffpfsc']
        self.assertEqual((image['kind'], image['format'], image['title'], image['size'], image['volume'],
                          image['scan_root']), ('image', 'ffpfsc', 'PPSA01476', 37000, '/mnt/ext0', '/mnt/ext0/games'))
        self.assertEqual(by_path['/data/homebrew/My Homebrew']['title'], '')
        self.assertIsNone(by_path['/data/homebrew/CUSA12345']['size'])
        # Title-ID folders are not opened; nothing is read below depth 1; backports/ is skipped.
        self.assertFalse([p for p in self.files.lists if p.startswith('/data/homebrew/CUSA12345')])
        self.assertNotIn('/data/homebrew/nested', self.files.lists)
        self.assertNotIn('/data/homebrew/backports', self.files.lists)
        self.assertEqual([(item['path'], item['exists'], item['games']) for item in result['scan_paths']],
                         [('/data/homebrew', True, 4), ('/mnt/ext0/games', True, 2)])
        self.assertEqual(result['scan_config'], {'source': 'config', 'file': SMP_CONFIG, 'depth': 1,
                                                 'skipped': ['/mnt/smb/share']})
        self.assertEqual(result['errors'], [])

    def test_depth_two_and_nested_scan_paths(self):
        self.config('[scan]\npath = /data/homebrew\ndepth = 2\n')
        found = self.paths(self.library.scan())
        self.assertIn('/data/homebrew/nested/PPSA07777', found)
        self.assertIn('/data/homebrew/nested/deep.exfat', found)
        self.assertNotIn('/data/homebrew/backports/PPSA08888', found)
        self.config('[scan]\npath = /data/homebrew\npath = /data/homebrew/nested\n')
        result = self.library.scan()
        nested = [game for game in result['games'] if game['path'].startswith('/data/homebrew/nested/')]
        self.assertEqual(sorted((g['path'], g['scan_root']) for g in nested), [
            ('/data/homebrew/nested/PPSA07777', '/data/homebrew/nested'),
            ('/data/homebrew/nested/deep.exfat', '/data/homebrew/nested')])

    def test_missing_or_pathless_config_falls_back_to_shadowmount_defaults(self):
        del self.files.blobs[SMP_CONFIG]
        result = self.library.scan()
        roots = [item['path'] for item in result['scan_paths']]
        self.assertEqual(result['scan_config']['source'], 'missing')
        for path in ('/data/homebrew', '/data/etaHEN/games', '/mnt/ext0', '/mnt/ext0/homebrew', '/data/games',
                     '/mnt/ext0/games'):
            self.assertIn(path, roots)
        self.assertFalse([path for path in roots if path.startswith(('/mnt/usb', '/mnt/ext1'))])
        self.assertIn('/mnt/ext0/PPSA00002', self.paths(result))
        self.assertIn('/mnt/ext0/games/PPSA01476.ffpfsc', self.paths(result))
        self.assertEqual(self.paths(result).count('/mnt/ext0/games/ELAS00001'), 1)
        self.config('[scan]\npath =\npath = ; nothing\ndepth = 9\n')
        result = self.library.scan()
        self.assertEqual((result['scan_config']['source'], result['scan_config']['depth']), ('default', 1))
        self.assertNotIn('/mnt/ext0/games', [item['path'] for item in result['scan_paths']])
        self.assertNotIn('/mnt/ext0/games/ELAS00001', self.paths(result))

    def test_unreadable_config_is_reported_and_defaults_used(self):
        original = self.files.stream

        def broken(path):
            if path == SMP_CONFIG:
                raise FileServiceError('读取超时')
            return original(path)
        self.files.stream = broken
        result = self.library.scan()
        self.assertEqual(result['scan_config']['source'], 'default')
        self.assertIn('读取超时', result['errors'][0])
        self.assertIn('/data/homebrew/CUSA12345', self.paths(result))

    def test_config_parsing(self):
        parsed = parse_smp_config('scanpath=/data/a ; old\nscan_depth=2\n[scan]\npath = "/mnt/ext0/x \\"y\\"" # q\n'
                                  'path = \n depth = 3\n[other]\npath = /nope\n')
        self.assertEqual(parsed, {'paths': ['/data/a', '/mnt/ext0/x "y"'], 'depth': 2})
        self.assertEqual(parse_smp_config('[SCAN]\nPath=/data/b\nDepth=1\n'), {'paths': ['/data/b'], 'depth': 1})
        self.assertEqual(parse_smp_config('[scan]\npath = "unterminated\n'), {'paths': [], 'depth': None})
        self.assertEqual((image_type('a.FFPFSC'), image_type('b.exfat'), image_type('.ffpkg'), image_type('c.iso')),
                         ('ffpfsc', 'exfat', '', ''))

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
        game = '/data/homebrew/CUSA12345'
        first = self.library.size(game)
        self.assertEqual((first['size'], first['cached']), (1234, False))
        self.assertTrue(self.library.size(game + '/')['cached'])
        self.assertEqual(self.files.sizes, [game])
        saved = json.loads((Path(self.tmp.name) / 'library-cache.json').read_text())
        self.assertEqual(saved['sizes'][game]['mtime'], 50)
        self.assertEqual({g['path']: g['size'] for g in self.library.scan(refresh=True)['games']}[game], 1234)
        # The folder changed: the cached size no longer applies.
        self.files.tree['/data/homebrew'] = [('CUSA12345', True, 51)]
        self.library.forget()
        self.assertFalse(self.library.size(game)['cached'])

    def test_image_size_comes_from_the_listing(self):
        result = self.library.size('/data/homebrew/PPSA05555.FFPFSC')
        self.assertEqual((result['size'], result['files']), (9000, 1))
        self.assertEqual(self.files.sizes, [])

    def test_size_and_icon_only_for_scanned_games(self):
        for path in ('/data/CUSA12345', '/etc', '/data/homebrew/junk', '/data/homebrew/README.md', None):
            with self.subTest(path=path), self.assertRaises(LibraryError):
                self.library.size(path)
        with self.assertRaises(LibraryError):
            self.library.icon('/data/homebrew/junk')
        self.assertEqual(self.files.sizes, [])

    def test_icon_is_a_checked_png_and_cached(self):
        data, etag, ctype = self.library.icon('/data/homebrew/CUSA12345')
        self.assertEqual((data, ctype), (PNG, 'image/png'))
        self.assertTrue(etag.startswith('"'))
        self.assertEqual(self.library.icon('/data/homebrew/CUSA12345'), (data, etag, ctype))
        self.assertEqual(self.files.streams.count('/data/homebrew/CUSA12345/sce_sys/icon0.png'), 1)
        icons = list((Path(self.tmp.name) / 'library-icons').glob('*.png'))
        self.assertEqual(len(icons), 1)
        self.assertEqual(icons[0].stat().st_mode & 0o777, 0o600)

    def test_image_and_iconless_folders_use_the_shadowmount_icon_cache(self):
        data, _, ctype = self.library.icon('/mnt/ext0/games/PPSA01476.ffpfsc')
        self.assertEqual((data, ctype), (JPEG, 'image/jpeg'))
        self.assertIn('/data/shadowmount/icon-cache/PPSA01476-v3-256-new.jpg', self.files.streams)
        with self.assertRaisesRegex(LibraryError, '封面'):        # listed in the cache, but the file is gone
            self.library.icon('/mnt/ext0/games/ELAS00001')
        with self.assertRaisesRegex(LibraryError, '封面'):
            self.library.icon('/data/homebrew/PPSA05555.FFPFSC')

    def test_icon_rejects_non_image_and_large(self):
        with self.assertRaisesRegex(LibraryError, 'PNG'):
            self.library.icon('/data/homebrew/My Homebrew')
        with self.assertRaisesRegex(LibraryError, '2 MiB'):
            self.library.icon('/data/homebrew/PPSA01234-app')

    def test_title_lookup_failure_keeps_folder_names(self):
        self.library.titles = mock.Mock(lookup=mock.Mock(side_effect=RuntimeError('offline')))
        names = {g['path']: g['name'] for g in self.library.scan()['games']}
        self.assertEqual(names['/data/homebrew/CUSA12345'], 'CUSA12345')

    def test_one_broken_scan_path_is_reported_not_fatal(self):
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

    def test_scan_path_lookup(self):
        self.assertEqual(self.library.scan_path('/mnt/ext0/games/')['volume'], '/mnt/ext0')
        for path in ('/mnt/ext0', '/mnt/smb/share', None):
            with self.assertRaises(LibraryError):
                self.library.scan_path(path)

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
        self.api.library = Library(FakeFiles(dict(TREE), dict(BLOBS)), self.tmp.name)

    def get(self, path, headers=None):
        handler = self.TC.StubHandler(path, headers=headers)
        handler.send_body = lambda status, data, **kw: setattr(handler, 'result', (status, data, kw))
        self.assertTrue(self.api.handle_get(handler))
        return handler.result

    def test_list_size_icon(self):
        status, data, _ = self.get('/api/library')
        self.assertEqual(status, 200)
        self.assertEqual([item['path'] for item in data['scan_paths']], ['/data/homebrew', '/mnt/ext0/games'])
        status, data, _ = self.get('/api/library/size?path=%2Fdata%2Fhomebrew%2FCUSA12345')
        self.assertEqual((status, data['size']), (200, 1234))
        status, data, extra = self.get('/api/library/icon?path=%2Fdata%2Fhomebrew%2FCUSA12345')
        self.assertEqual((status, data, extra['ctype']), (200, PNG, 'image/png'))
        status, _, _ = self.get('/api/library/icon?path=%2Fdata%2Fhomebrew%2FCUSA12345', {'If-None-Match': extra['etag']})
        self.assertEqual(status, 304)
        status, data, extra = self.get('/api/library/icon?path=%2Fmnt%2Fext0%2Fgames%2FPPSA01476.ffpfsc')
        self.assertEqual((status, extra['ctype']), (200, 'image/jpeg'))
        status, data, _ = self.get('/api/library/icon?path=%2Fdata%2Fhomebrew%2FMy%20Homebrew')
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
        for path, status in (('/data/homebrew/My Homebrew', 400), ('/data/homebrew/PPSA01234-app', 400),
                             ('/mnt/ext0/games/PPSA01476.ffpfsc', 400), ('/data/homebrew/CUSA12345', 200)):
            data, headers = self.TC.json_body({'path': path, 'host': '192.0.2.5'})
            handler = self.TC.StubHandler('/api/library/register', data, headers)
            self.api.handle_post(handler, '/api/library/register')
            self.assertEqual(handler.status, status, path)
        self.assertEqual(calls, [('192.0.2.5', {'title': 'CUSA12345', 'path': '/data/homebrew/CUSA12345',
                                                'name': 'CUSA12345'})])

    def test_file_manager_off_disables_the_library(self):
        self.api.file_manager = False
        handler = self.TC.StubHandler('/api/library')
        self.api.handle_get(handler)
        self.assertEqual(handler.status, 403)


if __name__ == '__main__':
    unittest.main()
