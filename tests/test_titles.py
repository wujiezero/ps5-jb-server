"""Game title names: ID extraction, param parsing, catalog/console merge, cache, API."""
import http.client
import importlib.util
import json
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'server'))

import titles  # noqa: E402
from files import FileServiceError  # noqa: E402
from server import AdminHandler, AdminServer, AuthManager  # noqa: E402
from titles import TitleCatalog, extract_id, parse_param_json, parse_param_sfo  # noqa: E402


def param_json(**names):
    params = {'defaultLanguage': 'en-US'}
    params.update({lang: {'titleName': name} for lang, name in names.items()})
    return json.dumps({'titleId': 'PPSA00001', 'localizedParameters': params}).encode()


def param_sfo(values):
    """Build a minimal PSF file holding utf-8 string entries."""
    keys = b''
    data = b''
    index = b''
    for key, value in values.items():
        raw = value.encode() + b'\x00'
        index += struct.pack('<HHIII', len(keys), 0x0204, len(raw), len(raw), len(data))
        keys += key.encode() + b'\x00'
        data += raw
    key_table = 20 + len(index)
    data_table = key_table + len(keys)
    header = b'\x00PSF' + struct.pack('<IIII', 0x101, key_table, data_table, len(values))
    return header + index + keys + data


class ExtractTests(unittest.TestCase):
    def test_title_ids_in_names_and_paths(self):
        cases = {
            'CUSA36464': 'CUSA36464',
            'PPSA01576.ffpfsc': 'PPSA01576',
            'PPSA03671-app': 'PPSA03671',
            'ppsa03671': 'PPSA03671',
            'UP8062-CUSA20499_00-CUPHEAD000000000-A0100-V0100.pkg': 'CUSA20499',
            '/mnt/ext0/games/PPSA03671': 'PPSA03671',
            'PLAS12345 backup': 'PLAS12345',
        }
        for text, expected in cases.items():
            self.assertEqual(extract_id(text), expected, text)

    def test_non_ids_are_ignored(self):
        for text in ('README.md', 'XCUSA12345', 'CUSA1234', 'CUSA123456', 'ABCD12345', '', None):
            self.assertIsNone(extract_id(text), text)


class ParamTests(unittest.TestCase):
    def test_param_json_prefers_simplified_chinese(self):
        data = param_json(**{'en-US': "Marvel's Wolverine", 'zh-Hans': '漫威金刚狼', 'zh-Hant': '漫威金鋼狼'})
        self.assertEqual(parse_param_json(data), ("Marvel's Wolverine", '漫威金刚狼'))

    def test_param_json_with_bom_and_only_default(self):
        data = b'\xef\xbb\xbf' + param_json(**{'en-US': 'ProsperoStore™'})
        self.assertEqual(parse_param_json(data), ('ProsperoStore', ''))

    def test_param_json_garbage(self):
        for data in (b'', b'not json', b'[]', b'{"localizedParameters": 3}'):
            self.assertEqual(parse_param_json(data), ('', ''))

    def test_param_sfo(self):
        data = param_sfo({'APP_VER': '01.00', 'TITLE': 'Cuphead', 'TITLE_11': '茶杯头'})
        self.assertEqual(parse_param_sfo(data), ('Cuphead', '茶杯头'))
        self.assertEqual(parse_param_sfo(b'\x00PSF' + b'\x00' * 4), ('', ''))
        self.assertEqual(parse_param_sfo(b'garbage'), ('', ''))


class FakeReader:
    def __init__(self, files=None, offline=False):
        self.files = files or {}
        self.offline = offline
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        if self.offline:
            raise FileServiceError('无法连接 PS5')
        if path not in self.files:
            raise FileServiceError('下载失败：not found', status=404)
        return self.files[path]


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.catalog_path = root / 'titles.json'
        self.catalog_path.write_text(json.dumps({'commit': 'abc', 'titles': {
            'PPSA01576': ['Avatar: Frontiers of Pandora'],
            'PPSA21567': ['ASTRO BOT', '宇宙机器人'],
        }}), encoding='utf-8')
        self.cache_path = root / 'cache' / 'titles-cache.json'

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, reader=None, clock=None):
        kwargs = {'clock': clock} if clock else {}
        return TitleCatalog(self.catalog_path, self.cache_path, reader=reader, **kwargs)

    def test_builtin_lookup_prefers_chinese_and_skips_unknown(self):
        result = self.make().lookup(['PPSA21567.ffpfsc', 'PPSA01576', 'PPSA99999', 'README.md'])
        self.assertEqual(result['titles']['PPSA21567'],
                         {'name': '宇宙机器人', 'en': 'ASTRO BOT', 'zh': '宇宙机器人', 'source': 'builtin'})
        self.assertEqual(result['titles']['PPSA01576']['name'], 'Avatar: Frontiers of Pandora')
        self.assertNotIn('PPSA99999', result['titles'])
        self.assertEqual(result['pending'], [])

    def test_missing_catalog_file_is_not_fatal(self):
        catalog = TitleCatalog(Path(self.tmp.name) / 'nope.json', None)
        catalog.path = Path(self.tmp.name) / 'nope.json'     # do not fall back to the repo copy
        self.assertEqual(catalog.lookup(['PPSA21567'])['titles'], {})

    def test_console_param_fills_gaps_and_is_cached(self):
        reader = FakeReader({
            '/data/homebrew/PPSA99000/sce_sys/param.json': param_json(**{'en-US': 'ProsperoStore'}),
            '/mnt/ext0/games/PPSA01576/sce_sys/param.json': param_json(**{'en-US': 'Avatar', 'zh-Hans': '阿凡达：潘多拉边境'}),
        })
        catalog = self.make(reader)
        result = catalog.lookup([], ['/data/homebrew/PPSA99000', '/mnt/ext0/games/PPSA01576/',
                                     '/mnt/ext0/games/PPSA21567', '/mnt/ext0/games/not-a-game'])
        self.assertEqual(result['titles']['PPSA99000']['name'], 'ProsperoStore')
        self.assertEqual(result['titles']['PPSA99000']['source'], 'console')
        avatar = result['titles']['PPSA01576']
        # Catalog English wins, console supplies the missing Chinese name.
        self.assertEqual((avatar['en'], avatar['zh'], avatar['source']),
                         ('Avatar: Frontiers of Pandora', '阿凡达：潘多拉边境', 'builtin+console'))
        # Complete catalog entries never touch the console.
        self.assertFalse(any('PPSA21567' in call for call in reader.calls))
        calls = len(reader.calls)
        catalog.lookup([], ['/data/homebrew/PPSA99000'])
        self.assertEqual(len(reader.calls), calls, 'cached titles are not read again')
        # Survives a restart.
        again = self.make(FakeReader(offline=True)).lookup(['PPSA99000'])
        self.assertEqual(again['titles']['PPSA99000']['name'], 'ProsperoStore')

    def test_param_sfo_fallback_and_negative_cache(self):
        reader = FakeReader({'/data/homebrew/CUSA20499/sce_sys/param.sfo': param_sfo({'TITLE': 'Cuphead'})})
        catalog = self.make(reader)
        result = catalog.lookup([], ['/data/homebrew/CUSA20499', '/data/homebrew/CUSA11111'])
        self.assertEqual(result['titles']['CUSA20499']['name'], 'Cuphead')
        self.assertNotIn('CUSA11111', result['titles'])
        calls = len(reader.calls)
        catalog.lookup([], ['/data/homebrew/CUSA11111'])
        self.assertEqual(len(reader.calls), calls, 'folders without a param file are not retried at once')

    def test_offline_console_reports_pending_without_caching_misses(self):
        reader = FakeReader(offline=True)
        catalog = self.make(reader)
        result = catalog.lookup([], ['/data/homebrew/PPSA99000', '/data/homebrew/PPSA99008'])
        self.assertEqual(result['titles'], {})
        self.assertEqual(result['pending'], ['PPSA99000', 'PPSA99008'])
        self.assertEqual(len(reader.calls), 1, 'stop after the first connection failure')
        self.assertEqual(catalog.misses, {})

    def test_time_budget_defers_remaining_folders(self):
        now = [0.0]

        def clock():
            now[0] += titles.READ_BUDGET / 2 + 0.1
            return now[0]

        reader = FakeReader({f'/data/homebrew/PPSA9900{i}/sce_sys/param.json': param_json(**{'en-US': f'App {i}'})
                             for i in range(4)})
        result = self.make(reader, clock).lookup([], [f'/data/homebrew/PPSA9900{i}' for i in range(4)])
        self.assertIn('PPSA99000', result['titles'])
        self.assertTrue(result['pending'])
        self.assertFalse(set(result['pending']) & set(result['titles']))


class FakeApp:
    def __init__(self, catalog):
        self.titles = catalog
        self.settings = type('S', (), {'subnet': '127.0.0.1/32', 'host_ip': '127.0.0.1'})()


class TitlesApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / 'titles.json'
        path.write_text(json.dumps({'titles': {'PPSA21567': ['ASTRO BOT', '宇宙机器人']}}), encoding='utf-8')
        self.reader = FakeReader({'/data/homebrew/PPSA99000/sce_sys/param.json': param_json(**{'en-US': 'ProsperoStore'})})
        self.server = AdminServer(('127.0.0.1', 0), AdminHandler)
        self.server.app = FakeApp(TitleCatalog(path, None, reader=self.reader))
        self.server.auth = AuthManager(AuthManager.make_credentials('a-long-test-password'))
        self.server.static_dir = REPO / 'web'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, cookie=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        headers = {'Content-Type': 'application/json'}
        if cookie:
            headers['Cookie'] = cookie
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        data = response.read()
        cookie_header = response.getheader('Set-Cookie')
        conn.close()
        return response.status, json.loads(data) if data else {}, cookie_header

    def test_requires_login_and_returns_names(self):
        self.assertEqual(self.request('GET', '/api/titles?ids=PPSA21567')[0], 401)
        _, _, cookie = self.request('POST', '/api/login', {'password': 'a-long-test-password'})
        cookie = cookie.split(';')[0]
        status, body, _ = self.request(
            'GET', '/api/titles?ids=PPSA21567.ffpfsc,README.md,PPSA00000'
                   '&folder=%2Fdata%2Fhomebrew%2FPPSA99000', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body['titles']['PPSA21567']['name'], '宇宙机器人')
        self.assertEqual(body['titles']['PPSA99000']['name'], 'ProsperoStore')
        self.assertNotIn('PPSA00000', body['titles'])
        status, body, _ = self.request('GET', '/api/titles', cookie=cookie)
        self.assertEqual((status, body['titles']), (200, {}))


class ShippedCatalogTests(unittest.TestCase):
    def test_repository_catalog_is_valid(self):
        document = json.loads((REPO / 'titledb' / 'titles.json').read_text(encoding='utf-8'))
        self.assertIn('andshrew/PlayStation-Titles', document['source'])
        self.assertGreater(document['count'], 10000)
        self.assertEqual(document['count'], len(document['titles']))
        for key, value in list(document['titles'].items())[:2000]:
            self.assertEqual(extract_id(key), key)
            self.assertTrue(1 <= len(value) <= 2 and all(value), (key, value))
        self.assertTrue((REPO / 'titledb' / 'UPSTREAM_LICENSE').is_file())

    def test_build_script_merges_concepts(self):
        spec = importlib.util.spec_from_file_location('build_titles', REPO / 'scripts' / 'build_titles.py')
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        rows = [
            {'titleId': 'PPSA01292_00', 'conceptId': '7', 'name': 'Sackboy™: A Big Adventure', 'region': 'HP'},
            {'titleId': 'PPSA01293_00', 'conceptId': '7', 'name': '麻布仔大冒险', 'region': 'HP'},
            {'titleId': 'PPSA01294_00', 'conceptId': '7', 'name': 'Sackboy: A Big Adventure', 'region': 'EP'},
            {'titleId': 'PPSA01295_00', 'conceptId': '7', 'name': 'リトルビッグプラネット', 'region': 'JP'},
            {'titleId': 'PPSA01296_00', 'conceptId': '8', 'name': '戦国無双', 'region': 'JP'},
            {'titleId': 'PPSA01296_01', 'conceptId': '8', 'name': 'Other', 'region': 'JP'},
            {'titleId': 'bad', 'conceptId': '', 'name': 'x', 'region': ''},
        ]
        result = build.compile_titles(rows)
        self.assertEqual(result['PPSA01292'], ['Sackboy: A Big Adventure', '麻布仔大冒险'])
        self.assertEqual(result['PPSA01293'], ['Sackboy: A Big Adventure', '麻布仔大冒险'])
        self.assertEqual(result['PPSA01295'], ['Sackboy: A Big Adventure', '麻布仔大冒险'])
        # Japanese kanji names are not treated as Chinese; the _00 row wins.
        self.assertEqual(result['PPSA01296'], ['戦国無双'])
        self.assertNotIn('bad', result)


if __name__ == '__main__':
    unittest.main()
