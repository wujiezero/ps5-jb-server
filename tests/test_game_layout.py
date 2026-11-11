"""Pure game-root recognition; fixtures are local metadata bytes only."""
import importlib
import json
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))


def sfo_bytes(**values):
    keys, data, rows = bytearray(), bytearray(), []
    for key, value in values.items():
        raw = value.encode('utf-8') + b'\0'
        rows.append(struct.pack('<HHIII', len(keys), 0x0204, len(raw), len(raw), len(data)))
        keys.extend(key.encode('ascii') + b'\0')
        data.extend(raw)
    key_offset = 20 + 16 * len(rows)
    return (struct.pack('<4sIIII', b'\0PSF', 0x101, key_offset, key_offset + len(keys), len(rows))
            + b''.join(rows) + keys + data)


class ResolveGameTests(unittest.TestCase):
    def setUp(self):
        try:
            self.layout = importlib.import_module('game_layout')
        except ModuleNotFoundError:
            self.fail('game_layout.resolve_game is not implemented')

    def json_game(self, root='', **overrides):
        prefix = root + '/' if root else ''
        param = {'titleId': 'PPSA12345', 'contentVersion': '01.002.000',
                 'localizedParameters': {'defaultLanguage': 'en-US',
                                         'en-US': {'titleName': 'Test Game'}}}
        param.update(overrides)
        path = prefix + 'sce_sys/param.json'
        return [prefix + 'eboot.bin', path], {path: json.dumps(param).encode()}

    def test_deep_wrappers_with_siblings_resolve_using_ps5_metadata(self):
        root = 'Release/download/Actual Game'
        paths, metadata = self.json_game(root)
        paths += ['README.txt', 'Release/update.pkg', 'Release/download/info.txt']
        self.assertEqual(self.layout.resolve_game(paths, metadata, hint='A game release'),
                         {'root': root, 'title': 'PPSA12345', 'platform': 'ps5',
                          'version': '01.002.000', 'name': 'Test Game'})

    def test_ps4_sfo_title_version_and_name(self):
        path = 'a/b/c/sce_sys/param.sfo'
        metadata = {path: sfo_bytes(TITLE_ID='CUSA12345', APP_VER='01.09', TITLE='PS4 游戏')}
        result = self.layout.resolve_game(['a/b/c/eboot.bin', path], metadata)
        self.assertEqual(result, {'root': 'a/b/c', 'title': 'CUSA12345', 'platform': 'ps4',
                                  'version': '01.09', 'name': 'PS4 游戏'})

    def test_bare_root_is_empty_relative_path(self):
        paths, metadata = self.json_game()
        self.assertEqual(self.layout.resolve_game(paths, metadata)['root'], '')

    def test_explicit_hint_conflict_is_rejected(self):
        paths, metadata = self.json_game()
        with self.assertRaisesRegex(self.layout.GameLayoutError, '冲突'):
            self.layout.resolve_game(paths, metadata, hint='CUSA99999 release')

    def test_distinct_roots_are_ambiguous_even_with_same_title(self):
        paths1, metadata1 = self.json_game('one')
        paths2, metadata2 = self.json_game('two')
        with self.assertRaisesRegex(self.layout.GameLayoutError, '多个'):
            self.layout.resolve_game(paths1 + paths2, metadata1 | metadata2)

    def test_metadata_requires_eboot_in_the_same_root(self):
        paths, metadata = self.json_game('game')
        paths[0] = 'elsewhere/eboot.bin'
        with self.assertRaises(self.layout.NoGameError):
            self.layout.resolve_game(paths, metadata)

    def test_packages_and_plain_archives_are_not_runnable_games(self):
        for paths in (['release/game.pkg'], ['readme.txt'], ['eboot.bin']):
            with self.subTest(paths=paths), self.assertRaises(self.layout.NoGameError):
                self.layout.resolve_game(paths, {})

    def test_invalid_or_unavailable_metadata_is_a_hard_error_for_game_layout(self):
        paths, _ = self.json_game()
        for raw in (None, b'{bad json', b'[]', b'{"titleId":"../../bad"}', b'{}'):
            metadata = {} if raw is None else {paths[1]: raw}
            with self.subTest(raw=raw), self.assertRaises(self.layout.GameLayoutError):
                self.layout.resolve_game(paths, metadata, hint='PPSA12345')

    def test_truncated_sfo_fails_without_guessing_from_hint(self):
        path = 'sce_sys/param.sfo'
        raw = sfo_bytes(TITLE_ID='CUSA12345')
        for broken in (b'garbage', raw[:19], raw[:-3]):
            with self.subTest(broken=broken), self.assertRaises(self.layout.GameLayoutError):
                self.layout.resolve_game(['eboot.bin', path], {path: broken}, hint='CUSA12345')

    def test_conflicting_metadata_formats_are_rejected(self):
        paths, metadata = self.json_game()
        path = 'sce_sys/param.sfo'
        paths.append(path)
        metadata[path] = sfo_bytes(TITLE_ID='CUSA12345', APP_VER='01.00')
        with self.assertRaisesRegex(self.layout.GameLayoutError, '冲突'):
            self.layout.resolve_game(paths, metadata)

    def test_unsafe_relative_paths_are_rejected(self):
        paths, metadata = self.json_game()
        for path in ('../eboot.bin', '/absolute', 'C:\\evil', 'a\x00b'):
            with self.subTest(path=path), self.assertRaises(self.layout.GameLayoutError):
                self.layout.resolve_game(paths + [path], metadata)


if __name__ == '__main__':
    unittest.main()
