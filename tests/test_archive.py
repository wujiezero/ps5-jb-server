"""RAR5 archives streamed from a NAS and installed on the PS5 (fake NAS and console).

Fixtures in tests/fixtures/rar are real RAR5 archives made by make.sh. The
extraction tests need libarchive (Debian: python3-libarchive-c) and are skipped
without it; the volume and header checks run everywhere.
"""
import random
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))

import archive  # noqa: E402
import extract_worker  # noqa: E402
import unrar  # noqa: E402
from files import FileServiceError  # noqa: E402
from nas import NasStore, TransferManager  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'rar'
_RNG = random.Random(7)       # same sequence as make.sh
EBOOT = bytes(_RNG.getrandbits(8) for _ in range(24000))
TEXT = ('texture block\n' * 3000).encode()
CHINESE = 'data/子目录/压缩 文件.dat'
HAS_LIB = archive.available()
HAS_UNRAR = unrar.available()
LIBARCHIVE_ONLY = {'libarchive': True, 'unrar': False}


def fixture(name):
    return (FIXTURES / name).read_bytes()


def volumes_of(prefix):
    return sorted(FIXTURES.glob(prefix + '.part*.rar'), key=lambda p: int(p.name.split('.part')[1][:-4]))


class FakeNas:
    """A NAS folder /dl holding the given files."""

    def __init__(self, files, block=4096):
        self.files, self.block = dict(files), block

    def list(self, path):
        assert path == '/dl', path
        return [{'name': name, 'path': '/dl/' + name, 'dir': False, 'size': len(data), 'mtime': 0}
                for name, data in self.files.items()]

    def open(self, path):
        data, block = self.files[path.rsplit('/', 1)[1]], self.block

        class Reader:
            offset = 0

            def read(self, size):
                chunk = data[self.offset:self.offset + min(size, block)]
                self.offset += len(chunk)
                return chunk

            def close(self):
                pass
        return Reader()

    def read_at(self, path, offset, size):
        self.reads = getattr(self, 'reads', 0) + 1
        return self.files[path.rsplit('/', 1)[1]][offset:offset + size]

    def close(self):
        pass


class StreamOnlyNas(FakeNas):
    """A source that cannot read at an offset: only the stream checks apply."""
    read_at = None


class FakePs5:
    """In-memory console file service: folders and files by absolute path."""

    subnet, host, port, ftp_port, space_port = '192.0.2.0/24', '192.0.2.89', 80, 2120, 8888

    def __init__(self, free=None):
        self.files, self.dirs, self.free = {}, {'/data', '/data/games'}, free
        self.renames = []

    def _children(self, path):
        prefix = path.rstrip('/') + '/'
        names = {}
        for key in self.dirs:
            if key.startswith(prefix) and '/' not in key[len(prefix):]:
                names[key[len(prefix):]] = True
        for key in self.files:
            if key.startswith(prefix) and '/' not in key[len(prefix):]:
                names[key[len(prefix):]] = False
        return names

    def list(self, path):
        if path not in self.dirs:
            raise FileServiceError('missing', status=404)
        return {'path': path, 'entries': [{'name': name, 'dir': is_dir}
                                          for name, is_dir in sorted(self._children(path).items())]}

    def volumes(self):
        return {'volumes': [{'path': '/data', 'free': self.free}]}

    def mkdir(self, path):
        if path in self.dirs:
            raise FileServiceError('exists')
        if path.rsplit('/', 1)[0] not in self.dirs:
            raise FileServiceError('no parent')
        self.dirs.add(path)

    def upload(self, directory, name, chunks, length, overwrite=False):
        assert directory in self.dirs, directory
        data = b''.join(chunks)
        assert len(data) == length
        self.files[directory + '/' + name] = data
        return {'ok': True}

    def delete(self, path):
        prefix = path + '/'
        self.files = {key: value for key, value in self.files.items()
                      if key != path and not key.startswith(prefix)}
        self.dirs = {key for key in self.dirs if key != path and not key.startswith(prefix)}

    def rename(self, source, target):
        self.renames.append((source, target))
        if source in self.files:
            self.files[target] = self.files.pop(source)
            return
        assert source in self.dirs and target not in self.dirs, (source, target)
        prefix = source + '/'
        self.dirs = {target + key[len(source):] if key == source or key.startswith(prefix) else key
                     for key in self.dirs}
        self.files = {(target + key[len(source):] if key.startswith(prefix) else key): value
                      for key, value in self.files.items()}

    def staging_left(self):
        return [key for key in self.dirs | set(self.files) if '/.extract-' in key]


def wait(manager, ident, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        job = manager.snapshot_one(ident)
        if job['state'] in ('done', 'failed', 'canceled'):
            return job
        time.sleep(0.02)
    raise AssertionError('job did not finish')


class VolumeSetTests(unittest.TestCase):
    def entries(self, *names):
        return [{'name': name, 'dir': False, 'size': 1, 'path': '/dl/' + name} for name in names]

    def test_numeric_order_and_missing_parts(self):
        names = [f'Game.part{i:02d}.rar' for i in range(1, 12)] + ['other.part01.rar', 'notes.txt']
        base, volumes = archive.volume_set(self.entries(*names), 'Game.part05.rar')
        self.assertEqual(base, 'Game')
        self.assertEqual([item['name'] for item in volumes], names[:11])
        with self.assertRaisesRegex(archive.ArchiveError, 'part03'):
            archive.volume_set(self.entries('G.part01.rar', 'G.part02.rar', 'G.part04.rar'), 'G.part01.rar')

    def test_macos_metadata_files_are_ignored(self):
        names = ['G.part1.rar', '._G.part1.rar', 'G.part2.rar', '._G.part2.rar']
        base, volumes = archive.volume_set(self.entries(*names), 'G.part2.rar')
        self.assertEqual([item['name'] for item in volumes], ['G.part1.rar', 'G.part2.rar'])
        with self.assertRaisesRegex(archive.ArchiveError, '元数据'):
            archive.volume_set(self.entries(*names), '._G.part1.rar')
        self.assertFalse(archive.is_archive_name('._G.part1.rar'))

    def test_single_and_old_style(self):
        self.assertEqual(len(archive.volume_set(self.entries('a.rar'), 'a.rar')[1]), 1)
        with self.assertRaisesRegex(archive.ArchiveError, 'RAR4'):
            archive.volume_set(self.entries('a.rar', 'a.r00', 'a.r01'), 'a.rar')
        with self.assertRaisesRegex(archive.ArchiveError, 'RAR4'):
            archive.volume_set(self.entries('a.rar', 'a.r00'), 'a.r00')
        with self.assertRaises(archive.ArchiveError):
            archive.volume_set(self.entries('a.zip'), 'a.zip')

    def test_title_guess(self):
        self.assertEqual(archive.title_guess('PPSA03671-app'), 'PPSA03671')
        self.assertEqual(archive.title_guess('[x] CUSA36464 v1.02'), 'CUSA36464')
        self.assertEqual(archive.title_guess('PPSA036710'), '')


class ScannerTests(unittest.TestCase):
    def scan(self, paths, block=777):
        scanner = archive.Rar5Scanner()
        for path in paths:
            data = path.read_bytes()
            for offset in range(0, len(data), block):
                scanner.feed(data[offset:offset + block])
        return scanner

    def test_lists_every_entry_across_volumes(self):
        for prefix in ('store', 'm3'):
            scanner = self.scan(volumes_of(prefix))
            self.assertTrue(scanner.finished, prefix)
            self.assertIn('PPSA03671-app/' + CHINESE, scanner.entries)
            self.assertIn('PPSA03671-app/sce_sys/param.json', scanner.entries)
            self.assertTrue(scanner.entries['PPSA03671-app/empty'])
            self.assertEqual(scanner.signatures, len(volumes_of(prefix)))
        self.assertTrue(self.scan([FIXTURES / 'single.rar']).finished)

    def test_missing_last_volume_is_not_finished(self):
        self.assertFalse(self.scan(volumes_of('store')[:-1]).finished)

    def test_wrong_order_is_reported(self):
        parts = volumes_of('store')
        with self.assertRaisesRegex(archive.ArchiveError, '顺序'):
            self.scan([parts[0], parts[2], parts[1]])

    def test_refuses_what_cannot_be_streamed(self):
        for name, pattern in (('solid.part1.rar', 'solid'), ('enc.part1.rar', '密码'),
                              ('henc.part1.rar', '密码')):
            with self.assertRaisesRegex(archive.ArchiveError, pattern):
                self.scan([FIXTURES / name])
        with self.assertRaisesRegex(archive.ArchiveError, 'RAR4'):
            archive.Rar5Scanner().feed(b'Rar!\x1a\x07\x00' + b'\0' * 20)
        with self.assertRaisesRegex(archive.ArchiveError, '不是 RAR'):
            archive.Rar5Scanner().feed(b'PK\x03\x04' + b'\0' * 20)

    def test_large_dictionary_is_refused(self):
        with self.assertRaisesRegex(archive.ArchiveError, '字典'):
            scanner = archive.Rar5Scanner(max_dictionary=64 * 1024)
            for path in volumes_of('m3'):
                scanner.feed(path.read_bytes())


class ManagerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = NasStore(Path(self.tmp.name) / 'nas.json')
        self.ident = self.store.save({'protocol': 'ftp', 'host': '192.0.2.9'})['id']
        self.ps5 = FakePs5()

    def tearDown(self):
        self.tmp.cleanup()

    def manager(self, nas_files, source=FakeNas, engines=LIBARCHIVE_ONLY, **kwargs):
        self.nas = source(nas_files)
        return TransferManager(self.store, self.ps5, client_factory=lambda config: self.nas,
                               engines=engines, **kwargs)

    def nas_files(self, prefix, rename='Game.part{}.rar'):
        return {rename.format(index): path.read_bytes()
                for index, path in enumerate(volumes_of(prefix), 1)}


class InspectTests(ManagerTestCase):
    def test_inspect_reports_volumes_and_title(self):
        manager = self.manager(self.nas_files('m3'))
        info = manager.inspect_archive(self.ident, '/dl/Game.part2.rar')
        self.assertEqual((info['parts'], info['title'], info['format']), (3, 'PPSA03671', 'RAR5'))
        self.assertEqual(info['bytes'], sum(len(v) for v in self.nas.files.values()))
        # Every header was read up front: exact file count and unpacked size.
        self.assertTrue(info['checked'])
        self.assertEqual(info['files'], 4)
        param = len(b'{"titleId":"PPSA03671","localizedParameters":{"defaultLanguage":"en-US",'
                    b'"en-US":{"titleName":"Fixture Game"}}}')
        self.assertEqual(info['unpacked'], len(EBOOT) + len(TEXT) + param)
        self.assertLess(self.nas.reads, 20)

    def test_inspect_without_range_reads_only_the_head(self):
        manager = self.manager(self.nas_files('m3'), source=StreamOnlyNas)
        info = manager.inspect_archive(self.ident, '/dl/Game.part1.rar')
        self.assertEqual((info['checked'], info['files'], info['unpacked']), (False, None, None))

    def test_prescan_finds_problems_past_the_first_volume(self):
        files = self.nas_files('store')
        files.pop(f'Game.part{len(files)}.rar')
        manager = self.manager(files)
        with self.assertRaisesRegex(ValueError, '缺少后面的分卷'):
            manager.inspect_archive(self.ident, '/dl/Game.part1.rar')
        volumes = [{'name': p.name, 'path': '/dl/' + p.name, 'size': p.stat().st_size}
                   for p in volumes_of('m3')]
        nas = FakeNas({p.name: p.read_bytes() for p in volumes_of('m3')})
        with self.assertRaisesRegex(archive.ArchiveError, '字典'):
            archive.prescan(nas.read_at, volumes, max_dictionary=64 * 1024)

    def test_inspect_refuses_solid_and_encrypted(self):
        for name in ('solid.part1.rar', 'enc.part1.rar'):
            manager = self.manager({'x.rar': fixture(name)})
            with self.assertRaises(ValueError):
                manager.inspect_archive(self.ident, '/dl/x.rar')


@unittest.skipUnless(HAS_LIB, 'needs libarchive (python3-libarchive-c)')
class InstallTests(ManagerTestCase):
    def check_game(self, root):
        files = self.ps5.files
        self.assertEqual(files[root + '/eboot.bin'], EBOOT)
        self.assertEqual(files[root + '/' + CHINESE], TEXT)
        self.assertEqual(files[root + '/data/zero.bin'], b'')
        self.assertIn(b'PPSA03671', files[root + '/sce_sys/param.json'])
        self.assertIn(root + '/empty', self.ps5.dirs)
        self.assertIn(root + '/sce_sys/trophy2', self.ps5.dirs)

    def test_install_store_and_compressed_volumes(self):
        for prefix in ('store', 'm3'):
            with self.subTest(prefix):
                self.ps5 = FakePs5()
                manager = self.manager(self.nas_files(prefix))
                job = wait(manager, manager.start_extract(self.ident, '/dl/Game.part1.rar', '/data/games')['id'])
                self.assertEqual(job['state'], 'done', job['error'])
                self.assertEqual(job['result_path'], '/data/games/PPSA03671')
                self.check_game('/data/games/PPSA03671')
                self.assertEqual(job['bytes_done'], job['bytes_total'])
                self.assertEqual((job['files_done'], job['files_total']), (4, 4))
                self.assertEqual(self.ps5.staging_left(), [])
                # Moved into place with one rename, never extracted in place.
                self.assertEqual(self.ps5.renames[-1][1], '/data/games/PPSA03671')

    def test_single_archive(self):
        manager = self.manager({'PPSA03671-app.rar': fixture('single.rar')})
        job = wait(manager, manager.start_extract(self.ident, '/dl/PPSA03671-app.rar', '/data/games')['id'])
        self.assertEqual(job['state'], 'done', job['error'])
        self.check_game('/data/games/PPSA03671')

    def test_existing_game_needs_merge(self):
        self.ps5.dirs.add('/data/games/PPSA03671')
        manager = self.manager(self.nas_files('m3'))
        with self.assertRaisesRegex(ValueError, '合并'):
            manager.start_extract(self.ident, '/dl/Game.part1.rar', '/data/games')

    def test_merge_overwrites_and_keeps_other_files(self):
        root = '/data/games/PPSA03671'
        self.ps5.dirs |= {root, root + '/sce_sys', root + '/data'}
        self.ps5.files[root + '/eboot.bin'] = b'old'
        self.ps5.files[root + '/data/save.dat'] = b'keep me'
        manager = self.manager(self.nas_files('m3'))
        job = wait(manager, manager.start_extract(self.ident, '/dl/Game.part1.rar', root, mode='merge')['id'])
        self.assertEqual(job['state'], 'done', job['error'])
        self.check_game(root)
        self.assertEqual(self.ps5.files[root + '/data/save.dat'], b'keep me')
        self.assertEqual(self.ps5.staging_left(), [])
        with self.assertRaisesRegex(ValueError, '根目录'):
            manager.start_extract(self.ident, '/dl/Game.part1.rar', '/data', mode='merge')

    def test_missing_last_volume_fails_and_cleans_up(self):
        files = self.nas_files('store')
        files.pop(f'Game.part{len(files)}.rar')
        manager = self.manager(files, source=StreamOnlyNas)
        job = wait(manager, manager.start_extract(self.ident, '/dl/Game.part1.rar', '/data/games')['id'])
        self.assertEqual(job['state'], 'failed')
        self.assertIn('最后一个分卷', job['error'])
        self.assertEqual(self.ps5.staging_left(), [])
        self.assertNotIn('/data/games/PPSA03671', self.ps5.dirs)

    def test_corrupt_volume_fails(self):
        files = self.nas_files('m3')
        data = bytearray(files['Game.part2.rar'])
        data[5000] ^= 0xff
        files['Game.part2.rar'] = bytes(data)
        manager = self.manager(files, source=StreamOnlyNas)
        job = wait(manager, manager.start_extract(self.ident, '/dl/Game.part1.rar', '/data/games')['id'])
        self.assertEqual(job['state'], 'failed')
        self.assertNotIn('内部错误', job['error'])
        self.assertEqual(self.ps5.staging_left(), [])

    def test_entries_libarchive_skips_fail_the_job(self):
        original = archive.Rar5Scanner._file

        def extra_entry(scanner, *args):
            original(scanner, *args)
            scanner.entries.setdefault('PPSA03671-app/lost.bin', False)
        manager = self.manager(self.nas_files('m3'), source=StreamOnlyNas)
        with mock.patch.object(archive.Rar5Scanner, '_file', extra_entry):
            job = wait(manager, manager.start_extract(self.ident, '/dl/Game.part1.rar', '/data/games')['id'])
        self.assertEqual(job['state'], 'failed')
        self.assertIn('不完整', job['error'])
        self.assertEqual(self.ps5.staging_left(), [])

    def test_cancel_removes_staging(self):
        manager = self.manager(self.nas_files('store'))
        self.nas.block = 64
        original = self.nas.open

        def slow_open(path):
            reader = original(path)
            read = reader.read

            def slow(size):
                time.sleep(0.002)
                return read(size)
            reader.read = slow
            return reader
        self.nas.open = slow_open
        ident = manager.start_extract(self.ident, '/dl/Game.part1.rar', '/data/games')['id']
        end = time.time() + 5
        while manager.snapshot_one(ident)['bytes_done'] == 0 and time.time() < end:
            time.sleep(0.01)
        manager.cancel(ident)
        job = wait(manager, ident)
        self.assertEqual(job['state'], 'canceled')
        self.assertEqual(self.ps5.staging_left(), [])

    def test_space_check(self):
        packed = sum(p.stat().st_size for p in volumes_of('m3'))
        self.ps5.free = packed + 10         # fits the archive, not what it unpacks to
        manager = self.manager(self.nas_files('m3'))
        with self.assertRaisesRegex(ValueError, '解压后'):
            manager.start_extract(self.ident, '/dl/Game.part1.rar', '/data/games')
        self.ps5.free = 100
        manager = self.manager(self.nas_files('m3'), source=StreamOnlyNas)
        with self.assertRaisesRegex(ValueError, '压缩包'):
            manager.start_extract(self.ident, '/dl/Game.part1.rar', '/data/games')

    def test_install_note(self):
        self.assertEqual(archive.install_note(True, 0), '')
        self.assertIn('2 个 .pkg', archive.install_note(False, 2))
        self.assertIn('param.sfo', archive.install_note(False, 0))

    def test_unsafe_paths_are_refused(self):
        for name in ('../evil', '/abs', 'a/../../b'):
            with self.assertRaises(archive.ArchiveError):
                archive._clean_relative(name)
        self.assertEqual(archive._clean_relative('a\\b/./c'), 'a/b/c')


class SyntheticInstallTests(unittest.TestCase):
    """Exercise the real upload/finish flow without a native archive decoder."""

    def setUp(self):
        self.ps5 = FakePs5()

    def install(self, contents, base='Friendly release', mode='install', engine='libarchive'):
        class Entry:
            isdir, isreg = False, True

            def __init__(self, data):
                self.data, self.size = data, len(data)

            def get_blocks(self, size):
                for offset in range(0, len(self.data), 17):
                    yield self.data[offset:offset + 17]

        class SyntheticInstaller(archive.Installer):
            def _extract(installer):
                written, skipped = set(), set()
                for name, data in contents.items():
                    installer._place(Entry(data), name, written, skipped)
                return written

        dest = '/data/games' if mode == 'install' else '/data/games/PPSA12345'
        self.installer = SyntheticInstaller(self.ps5, None, [], dest, mode, 'synthetic',
                                            mock.Mock(), engine=engine)
        return self.installer.run(base)

    def game_contents(self, root='release/deep/actual'):
        prefix = root + '/' if root else ''
        return {prefix + 'eboot.bin': b'game',
                prefix + 'sce_sys/param.json':
                    b'{"titleId":"PPSA12345","contentVersion":"01.002.000",'
                    b'"localizedParameters":{"defaultLanguage":"en-US",'
                    b'"en-US":{"titleName":"Real Game"}}}'}

    def test_deep_game_root_is_renamed_without_wrapper_or_readme(self):
        for engine in ('libarchive', 'unrar'):
            with self.subTest(engine=engine):
                self.ps5 = FakePs5()
                result = self.install(self.game_contents() | {'release/README.txt': b'notes'},
                                      engine=engine)
                self.assertEqual(result['path'], '/data/games/PPSA12345')
                self.assertEqual(self.ps5.files[result['path'] + '/eboot.bin'], b'game')
                self.assertEqual(result['game'], {'title': 'PPSA12345', 'platform': 'ps5',
                                                 'version': '01.002.000', 'name': 'Real Game',
                                                 'path': result['path']})
                self.assertEqual(set(self.ps5.files), {result['path'] + '/eboot.bin',
                                                      result['path'] + '/sce_sys/param.json'})
                self.assertEqual(self.ps5.staging_left(), [])
                self.assertEqual(self.ps5.renames[0],
                                 ('/data/games/.extract-synthetic/release/deep/actual', result['path']))

    def test_sfo_game_is_named_using_its_real_title_id(self):
        from test_game_layout import sfo_bytes
        result = self.install({'a/b/eboot.bin': b'ps4', 'a/b/sce_sys/param.sfo':
                               sfo_bytes(TITLE_ID='CUSA12345', APP_VER='01.03', TITLE='PS4 Game')})
        self.assertEqual(result['path'], '/data/games/CUSA12345')
        self.assertEqual(result['game']['platform'], 'ps4')
        self.assertEqual(result['game']['version'], '01.03')
        self.assertEqual(result['game']['name'], 'PS4 Game')

    def test_mixed_package_and_zip_are_retained_outside_the_game(self):
        contents = self.game_contents() | {'release/unknown.pkg': b'package',
                                           'release/Cheats.zip': b'zip',
                                           'release/README.txt': b'notes'}
        result = self.install(contents)
        for key, data in (('packages', b'package'), ('sidecars', b'zip')):
            self.assertIn(key, result)
            self.assertEqual(len(result[key]), 1)
            path = result[key][0]
            self.assertTrue(path.startswith('/data/games/'))
            self.assertFalse(path.startswith(result['path'] + '/'))
            self.assertEqual(self.ps5.files[path], data)
        self.assertFalse(any(path.endswith('README.txt') for path in self.ps5.files))
        self.assertEqual(self.ps5.staging_left(), [])

    def test_sidecars_inside_game_are_returned_at_their_final_paths(self):
        contents = self.game_contents('') | {'cheats/local.zip': b'zip', 'dlc.pkg': b'pkg'}
        result = self.install(contents)
        self.assertIn('sidecars', result)
        self.assertEqual(result['sidecars'], [result['path'] + '/cheats/local.zip'])
        self.assertEqual(result['packages'], [result['path'] + '/dlc.pkg'])

    def test_pkg_only_archive_is_extracted_without_claiming_a_game(self):
        result = self.install({'release/base.pkg': b'pkg', 'release/README': b'notes'})
        self.assertIn('game', result)
        self.assertIsNone(result['game'])
        self.assertEqual(result['packages'], [result['path'] + '/base.pkg'])
        self.assertEqual(self.ps5.files[result['packages'][0]], b'pkg')
        self.assertIn('.pkg', result['note'])
        self.assertEqual(self.ps5.staging_left(), [])

    def test_plain_archive_with_params_but_no_eboot_is_not_registered(self):
        result = self.install({'docs/sce_sys/param.json': b'{"titleId":"PPSA12345"}'})
        self.assertIn('game', result)
        self.assertIsNone(result['game'])
        self.assertIn('eboot.bin', result['note'])

    def test_hint_conflict_and_multiple_roots_fail_before_any_publish(self):
        for contents, base in ((self.game_contents(), 'CUSA99999 release'),
                               (self.game_contents('one') | self.game_contents('two'), 'Game')):
            with self.subTest(base=base):
                self.ps5 = FakePs5()
                with self.assertRaises(archive.ArchiveError):
                    self.install(contents, base=base)
                self.assertEqual(self.ps5.files, {})
                self.assertEqual(self.ps5.staging_left(), [])
                self.assertEqual(self.ps5.renames, [])

    def test_metadata_capture_has_a_total_budget(self):
        contents = {f'wrapper/{index}/sce_sys/param.json': b' ' * (900 * 1024)
                    for index in range(5)}
        with self.assertRaisesRegex(archive.ArchiveError, '元数据.*限制'):
            self.install(contents)
        self.assertEqual(self.ps5.staging_left(), [])

    def test_merge_patch_without_eboot_keeps_existing_game_and_other_files(self):
        root = '/data/games/PPSA12345'
        self.ps5.dirs |= {root, root + '/data'}
        self.ps5.files.update({root + '/eboot.bin': b'original', root + '/data/save': b'save'})
        result = self.install({'patch/data/updated': b'update'}, mode='merge')
        self.assertEqual(self.ps5.files[root + '/data/updated'], b'update')
        self.assertEqual(self.ps5.files[root + '/data/save'], b'save')
        self.assertEqual(self.ps5.files[root + '/eboot.bin'], b'original')
        self.assertEqual(result['path'], root)
        self.assertIn('game', result)
        self.assertIsNone(result['game'])
        self.assertEqual(self.ps5.staging_left(), [])

    def test_merge_param_only_patch_keeps_the_legacy_title_result(self):
        root = '/data/games/PPSA12345'
        self.ps5.dirs.add(root)
        result = self.install({'patch/sce_sys/param.json': b'{"titleId":"PPSA12345"}'},
                              mode='merge')
        self.assertEqual(result['title'], 'PPSA12345')
        self.assertIn('game', result)
        self.assertIsNone(result['game'])
        self.assertIn(root + '/sce_sys/param.json', self.ps5.files)

    def test_full_game_merge_also_uses_the_resolved_root(self):
        root = '/data/games/PPSA12345'
        self.ps5.dirs.add(root)
        self.ps5.files[root + '/keep'] = b'keep'
        result = self.install(self.game_contents() | {'README': b'notes'}, mode='merge')
        self.assertIn(root + '/eboot.bin', self.ps5.files)
        self.assertEqual(self.ps5.files[root + '/eboot.bin'], b'game')
        self.assertEqual(self.ps5.files[root + '/keep'], b'keep')
        self.assertEqual(result['game']['path'], root)
        self.assertEqual(self.ps5.staging_left(), [])


class EngineChoiceTests(unittest.TestCase):
    def scanner(self, solid=False, dictionary=32 << 20):
        scanner = archive.Rar5Scanner(strict=False)
        scanner.solid, scanner.dictionary = solid, dictionary
        return scanner

    def test_libarchive_when_it_can(self):
        both = {'libarchive': True, 'unrar': True, 'memory': 5 << 30}
        self.assertEqual(archive.choose_engine(self.scanner(), both), 'libarchive')
        self.assertEqual(archive.choose_engine(self.scanner(solid=True), both), 'unrar')
        self.assertEqual(archive.choose_engine(self.scanner(dictionary=4 << 30), both), 'unrar')
        self.assertEqual(archive.choose_engine(self.scanner(), {'unrar': True}), 'unrar')

    def test_refusals_explain_what_is_missing(self):
        with self.assertRaisesRegex(archive.ArchiveError, '4096 MB.*unrar'):
            archive.choose_engine(self.scanner(dictionary=4 << 30), LIBARCHIVE_ONLY)
        with self.assertRaisesRegex(archive.ArchiveError, 'solid'):
            archive.choose_engine(self.scanner(solid=True), LIBARCHIVE_ONLY)
        with self.assertRaisesRegex(archive.ArchiveError, '内存'):
            archive.choose_engine(self.scanner(dictionary=4 << 30), {'unrar': True, 'memory': 1 << 30})
        with self.assertRaisesRegex(archive.ArchiveError, '没有可用'):
            archive.choose_engine(self.scanner(), {})

    def test_inspect_reports_engine(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = NasStore(Path(tmp.name) / 'nas.json')
        ident = store.save({'protocol': 'ftp', 'host': '192.0.2.9'})['id']
        nas = StreamOnlyNas({'s.part1.rar': fixture('solid.part1.rar')})
        manager = TransferManager(store, FakePs5(), client_factory=lambda config: nas,
                                  engines={'libarchive': True, 'unrar': True, 'memory': 5 << 30})
        info = manager.inspect_archive(ident, '/dl/s.part1.rar')
        self.assertEqual((info['engine'], info['solid']), ('unrar', True))
        self.assertGreater(info['memory'], 0)


def game_folder(prefix, directory, name='Game.part{}.rar'):
    """Copy a fixture volume set into directory under release-style names."""
    volumes = []
    for index, path in enumerate(volumes_of(prefix), 1):
        target = Path(directory) / name.format(index)
        target.write_bytes(path.read_bytes())
        volumes.append({'name': target.name, 'path': '/dl/' + target.name, 'size': target.stat().st_size})
    return volumes


@unittest.skipUnless(HAS_UNRAR, 'needs libunrar (Debian: libunrar5)')
class UnrarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_every_entry_across_volumes(self):
        for prefix in ('m3', 'store'):
            volumes = game_folder(prefix, self.dir, prefix + '.part{}.rar')
            seen = {}
            for entry in unrar.Archive(str(Path(self.dir) / volumes[0]['name'])):
                seen[entry.pathname] = b''.join(entry.get_blocks()) if entry.isreg else None
            self.assertEqual(seen['PPSA03671-app/eboot.bin'], EBOOT, prefix)
            self.assertEqual(seen['PPSA03671-app/' + CHINESE], TEXT, prefix)
            self.assertIn('PPSA03671-app/empty', seen)

    def test_missing_volume_is_named(self):
        volumes = game_folder('store', self.dir)
        (Path(self.dir) / volumes[2]['name']).unlink()
        with self.assertRaisesRegex(unrar.UnrarError, 'Game.part3.rar'):
            for entry in unrar.Archive(str(Path(self.dir) / volumes[0]['name'])):
                entry._drain()

    def test_stops_when_asked(self):
        volumes = game_folder('store', self.dir)
        stop = [False]

        def check():
            if stop[0]:
                raise extract_worker.Canceled()
        archive_ = unrar.Archive(str(Path(self.dir) / volumes[0]['name']), check)
        with self.assertRaises(extract_worker.Canceled):
            for entry in archive_:
                stop[0] = True
                entry._drain()
        self.assertFalse(archive_.thread.is_alive())


class _LocalMount:
    def __init__(self, folder):
        self.folder = folder

    def __enter__(self):
        return self.folder

    def __exit__(self, *exc):
        return False


def _spec(ident, volumes, dest, mode='install', status=''):
    return {'id': ident, 'volumes': volumes, 'dest': dest, 'mode': mode, 'base': 'Game',
            'status': status, 'folder': '/dl'}


@unittest.skipUnless(HAS_UNRAR, 'needs libunrar (Debian: libunrar5)')
class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ps5 = FakePs5()

    def tearDown(self):
        self.tmp.cleanup()

    def status(self):
        return {'state': 'starting', 'bytes_done': 0, 'files_done': 0, 'current': ''}

    def test_run_installs_with_unpacked_progress(self):
        volumes = game_folder('m3', self.tmp.name)
        status = self.status()
        result = extract_worker.run(_spec('w1', volumes, '/data/games'), status, threading.Event(),
                                    self.ps5, _LocalMount(self.tmp.name))
        self.assertEqual(result['path'], '/data/games/PPSA03671')
        self.assertEqual(self.ps5.files['/data/games/PPSA03671/eboot.bin'], EBOOT)
        self.assertEqual(self.ps5.files['/data/games/PPSA03671/' + CHINESE], TEXT)
        self.assertEqual(status['bytes_done'], status['bytes_total'])
        self.assertEqual((status['files_done'], status['files_total']), (4, 4))
        self.assertEqual(self.ps5.staging_left(), [])

    def test_cancel_cleans_up(self):
        volumes = game_folder('store', self.tmp.name)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(extract_worker.Canceled):
            extract_worker.run(_spec('w2', volumes, '/data/games'), self.status(), cancel,
                               self.ps5, _LocalMount(self.tmp.name))
        self.assertEqual(self.ps5.staging_left(), [])
        self.assertNotIn('/data/games/PPSA03671', self.ps5.dirs)

    def test_rclone_remote_keeps_secrets_out_of_argv(self):
        with mock.patch.object(extract_worker, '_obscure', lambda secret: 'OBSCURED'):
            env, remote, flags = extract_worker.rclone_remote(
                {'protocol': 'webdav', 'host': '192.0.2.5', 'port': 5006, 'tls': True, 'verify_tls': False,
                 'path': '/游戏/PS5', 'username': 'player', 'password': 'hunter2'}, '/CUSA20499')
            self.assertEqual(remote, 'NAS:/游戏/PS5/CUSA20499')
            self.assertEqual(env['RCLONE_CONFIG_NAS_URL'], 'https://192.0.2.5:5006')
            self.assertEqual(env['RCLONE_CONFIG_NAS_PASS'], 'OBSCURED')
            self.assertEqual(flags, ['--no-check-certificate'])
            env, remote, _ = extract_worker.rclone_remote(
                {'protocol': 'smb', 'host': 'nas', 'port': 445, 'share': 'games', 'path': '/ps5',
                 'username': '', 'password': '', 'workgroup': ''}, '/x')
            self.assertEqual(remote, 'NAS:/games/ps5/x')
            self.assertNotIn('RCLONE_CONFIG_NAS_PASS', env)


class _ThreadRunner:
    """Stands in for systemd-run: runs the worker's run() in a thread."""

    def __init__(self, ps5, folder, fail=False):
        self.ps5, self.folder, self.fail = ps5, folder, fail
        self.started = []

    def start(self, spec_path, unit, memory):
        import json
        spec = json.loads(Path(spec_path).read_text(encoding='utf-8'))
        self.started.append((unit, memory, spec))
        cancel = threading.Event()
        runner = self

        def body():
            status = {'state': 'starting', 'bytes_done': 0, 'files_done': 0, 'files_total': 0,
                      'bytes_total': 0, 'current': '', 'current_size': 0, 'current_done': 0, 'final': False}
            if runner.fail:
                return                      # dies without a final status, like an OOM kill
            try:
                result = extract_worker.run(spec, status, cancel, runner.ps5, _LocalMount(runner.folder))
                status.update(state='done', result_path=result['path'], title=result['title'], note=result['note'])
            except extract_worker.Canceled:
                status['state'] = 'canceled'
            except Exception as exc:
                status.update(state='failed', error=str(exc))
            status['final'] = True
            extract_worker._write_json(spec['status'], status)

        thread = threading.Thread(target=body, daemon=True)
        thread.start()

        class Handle:
            def alive(self):
                return thread.is_alive()

            def stop(self):
                cancel.set()
        return Handle()


@unittest.skipUnless(HAS_UNRAR, 'needs libunrar (Debian: libunrar5)')
class UnrarJobTests(ManagerTestCase):
    def unrar_manager(self, fail=False):
        volume_dir = tempfile.mkdtemp(dir=self.tmp.name)
        game_folder('store', volume_dir)
        runner = _ThreadRunner(self.ps5, volume_dir, fail)
        manager = self.manager(self.nas_files('store'), engines={'unrar': True, 'memory': 5 << 30},
                               work_dir=Path(self.tmp.name) / 'extract', runner=runner)
        return manager, runner

    def test_job_runs_in_worker_and_mirrors_status(self):
        manager, runner = self.unrar_manager()
        job = wait(manager, manager.start_extract(self.ident, '/dl/Game.part1.rar', '/data/games')['id'])
        self.assertEqual(job['state'], 'done', job['error'])
        self.assertEqual(job['engine'], 'unrar')
        self.assertEqual(job['result_path'], '/data/games/PPSA03671')
        self.assertEqual(self.ps5.files['/data/games/PPSA03671/eboot.bin'], EBOOT)
        unit, memory, spec = runner.started[0]
        self.assertEqual(unit, f'ps5-extract-{job["id"]}')
        self.assertGreater(memory, 0)
        self.assertNotIn('password', json_text(spec))
        self.assertEqual(list((Path(self.tmp.name) / 'extract').iterdir()), [], 'spec and status removed')

    def test_worker_dying_fails_the_job(self):
        manager, _ = self.unrar_manager(fail=True)
        job = wait(manager, manager.start_extract(self.ident, '/dl/Game.part1.rar', '/data/games')['id'])
        self.assertEqual(job['state'], 'failed')
        self.assertIn('意外退出', job['error'])


class SystemdRunnerTests(unittest.TestCase):
    def test_unit_command(self):
        import nas
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return mock.Mock(returncode=0, stdout='active\n', stderr='')
        with mock.patch.object(nas.subprocess, 'run', fake_run):
            handle = nas.SystemdRunner().start('/var/lib/x/extract/j1.json', 'ps5-extract-j1', 5 << 30)
            self.assertTrue(handle.alive())
            handle.stop()
        command = calls[0]
        self.assertEqual(command[:2], ['systemd-run', '--unit=ps5-extract-j1'])
        self.assertIn(f'MemoryMax={5 << 30}', command)
        self.assertIn('KillMode=mixed', command)
        self.assertIn('Nice=10', command)
        self.assertEqual(command[-2:], [str(nas.SystemdRunner.WORKER), '/var/lib/x/extract/j1.json'])
        self.assertEqual(calls[2][:3], ['systemctl', 'stop', '--no-block'])


def json_text(document):
    import json
    return json.dumps(document, ensure_ascii=False)


if __name__ == '__main__':
    unittest.main()
