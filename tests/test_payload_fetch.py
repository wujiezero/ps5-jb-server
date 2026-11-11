"""Plugin downloads during install: real sources, non-fatal per-item failures."""
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
FETCH = REPO / 'scripts' / 'fetch_payloads.py'
INSTALLER = REPO / 'installer' / '__main__.py'


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, data):
        self._stream = io.BytesIO(data)

    def read(self, size=-1):
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def opener_for(responses, seen):
    """Stand-in for urlopen: records URLs, returns bytes or raises a stored error."""
    def opener(request, timeout=None):
        url = getattr(request, 'full_url', request)
        seen.append((url, timeout))
        result = responses[url]
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)
    return opener


def sha256(data):
    return hashlib.sha256(data).hexdigest()


class ManifestEntryTests(unittest.TestCase):
    def test_every_payload_has_a_real_https_source(self):
        items = json.loads((REPO / 'payloads.json').read_text(encoding='utf-8'))['payloads']
        self.assertGreater(len(items), 10)
        for item in items:
            with self.subTest(item['filename']):
                self.assertTrue(item['url'].startswith('https://'),
                                'relative URLs cannot be downloaded: ' + item['url'])
                self.assertRegex(item['filename'], r'\.(elf|bin)$')
                self.assertRegex(item['checksum'], r'^[0-9a-f]{64}$')
        names = [item['filename'] for item in items]
        self.assertEqual(len(names), len(set(names)), 'duplicate filenames in manifest')


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo_dir = Path(self.temp.name) / 'repo'
        self.repo_dir.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def manifest(self, *entries):
        return {'name': 'test', 'payloads': list(entries)}

    @staticmethod
    def entry(filename, data):
        return {'filename': filename, 'url': 'https://mirror.test/' + filename,
                'checksum': sha256(data), 'version': '1.0'}

    def test_partial_failure_keeps_successes_and_reports_reason(self):
        fetch = load(FETCH, 'payload_fetch_partial')
        good, bad = b'first payload', b'second payload'
        manifest = self.manifest(self.entry('first.elf', good), self.entry('second.elf', bad))
        responses = {'https://mirror.test/first.elf': good,
                     'https://mirror.test/second.elf': urllib.error.URLError('network down')}
        seen = []
        output = io.StringIO()
        with redirect_stdout(output):
            catalog, failures = fetch.run(
                manifest, self.repo_dir, 'http://192.0.2.10:8080/payloads/',
                opener=opener_for(responses, seen))
        self.assertEqual([item['filename'] for item in catalog], ['first.elf'])
        self.assertEqual([item['url'] for item in catalog],
                         ['http://192.0.2.10:8080/payloads/first.elf'])
        self.assertEqual(len(failures), 1)
        self.assertIn('second.elf', failures[0][0])
        self.assertIn('network down', failures[0][1])
        self.assertTrue((self.repo_dir / 'payloads' / 'first.elf').is_file())
        self.assertFalse((self.repo_dir / 'payloads' / 'second.elf').exists())
        text = output.getvalue()
        self.assertIn('成功 1', text)
        self.assertIn('失败 1', text)
        catalog_disk = json.loads((self.repo_dir / 'repo.json').read_text(encoding='utf-8'))
        self.assertEqual(len(catalog_disk['payloads']), 1)

    def test_all_failures_still_write_an_empty_catalog_and_warn(self):
        fetch = load(FETCH, 'payload_fetch_empty')
        manifest = self.manifest(self.entry('only.elf', b'only'))
        responses = {'https://mirror.test/only.elf': urllib.error.URLError('offline')}
        output = io.StringIO()
        with redirect_stdout(output):
            catalog, failures = fetch.run(
                manifest, self.repo_dir, 'http://192.0.2.10:8080/payloads/',
                opener=opener_for(responses, []))
        self.assertEqual(catalog, [])
        self.assertEqual(len(failures), 1)
        on_disk = json.loads((self.repo_dir / 'repo.json').read_text(encoding='utf-8'))
        self.assertEqual(on_disk['payloads'], [])
        self.assertIn('仓库为空', output.getvalue())

    def test_checksum_mismatch_discards_the_file_and_keeps_going(self):
        fetch = load(FETCH, 'payload_fetch_checksum')
        expected, tampered = b'expected bytes', b'tampered bytes'
        manifest = self.manifest(self.entry('bad.elf', expected))
        output = io.StringIO()
        with redirect_stdout(output):
            catalog, failures = fetch.run(
                manifest, self.repo_dir, 'http://192.0.2.10:8080/payloads/',
                opener=opener_for({'https://mirror.test/bad.elf': tampered}, []))
        self.assertEqual(catalog, [])
        self.assertFalse((self.repo_dir / 'payloads' / 'bad.elf').exists())
        self.assertIn('校验和', failures[0][1])

    def test_relative_source_is_resolved_against_the_mirror_base(self):
        fetch = load(FETCH, 'payload_fetch_relative')
        data = b'legacy manifest'
        entry = {'filename': 'legacy.elf', 'url': 'legacy.elf', 'checksum': sha256(data)}
        seen = []
        with redirect_stdout(io.StringIO()):
            fetch.run(self.manifest(entry), self.repo_dir,
                      'http://192.0.2.10:8080/payloads/',
                      download_base='https://mirror.test/files/',
                      opener=opener_for({'https://mirror.test/files/legacy.elf': data}, seen))
        self.assertEqual(seen[0][0], 'https://mirror.test/files/legacy.elf')

    def test_already_verified_file_is_not_downloaded_again(self):
        fetch = load(FETCH, 'payload_fetch_cached')
        data = b'cached payload'
        manifest = self.manifest(self.entry('cached.elf', data))
        payload_dir = self.repo_dir / 'payloads'
        payload_dir.mkdir(parents=True)
        (payload_dir / 'cached.elf').write_bytes(data)
        seen = []
        with redirect_stdout(io.StringIO()):
            catalog, failures = fetch.run(
                manifest, self.repo_dir, 'http://192.0.2.10:8080/payloads/',
                opener=opener_for({}, seen))
        self.assertEqual(seen, [])
        self.assertEqual(failures, [])
        self.assertEqual(len(catalog), 1)


class InstallerDownloadStepTests(unittest.TestCase):
    def test_download_output_is_streamed_and_never_fails_the_install(self):
        installer = load(INSTALLER, 'installer_stream')
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            layout = installer.Layout(prefix=Path(tmp).resolve(), host_ip='192.0.2.10',
                                      no_manuals_host=True)
            layout.repo.mkdir(parents=True)
            (layout.repo / 'repo.json').write_text(json.dumps({'payloads': []}), encoding='utf-8')
            runner = installer.Runner(layout)
            recorded = {}

            def fake_run(argv, **kwargs):
                recorded['argv'] = argv
                recorded['kwargs'] = kwargs
                return subprocess.CompletedProcess(argv, 0, '', '')

            with mock.patch.object(installer.subprocess, 'run', side_effect=fake_run), \
                    redirect_stdout(io.StringIO()):
                installer.fetch_components(layout, runner)
        command = ' '.join(str(part) for part in recorded['argv'])
        self.assertIn('fetch_payloads.py', command)
        self.assertFalse(recorded['kwargs']['check'])
        self.assertFalse(recorded['kwargs']['capture_output'],
                         'downloader output must reach the install log')


class InstallerReportTests(unittest.TestCase):
    def layout(self, tmp, dry_run=False):
        installer = load(INSTALLER, 'installer_report')
        return installer, installer.Layout(prefix=Path(tmp).resolve(), dry_run=dry_run)

    def test_empty_catalog_reports_how_to_fix_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            installer, layout = self.layout(tmp)
            layout.repo.mkdir(parents=True)
            (layout.repo / 'repo.json').write_text(
                json.dumps({'name': 'empty', 'payloads': []}), encoding='utf-8')
            output = io.StringIO()
            with redirect_stdout(output):
                installer.report_payloads(layout, installer.Runner(layout))
            text = output.getvalue()
            self.assertIn('仓库为空', text)
            self.assertIn('fetch_payloads.py', text)

    def test_filled_catalog_reports_the_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            installer, layout = self.layout(tmp)
            layout.repo.mkdir(parents=True)
            (layout.repo / 'repo.json').write_text(
                json.dumps({'name': 'filled', 'payloads': [{'filename': 'a.elf'}]}),
                encoding='utf-8')
            output = io.StringIO()
            with redirect_stdout(output):
                installer.report_payloads(layout, installer.Runner(layout))
            self.assertIn('插件仓库：1 个插件', output.getvalue())

    def test_dry_run_does_not_warn_about_a_catalog_it_will_not_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            installer, layout = self.layout(tmp, dry_run=True)
            output = io.StringIO()
            with redirect_stdout(output):
                installer.report_payloads(layout, installer.Runner(layout))
            self.assertNotIn('仓库为空', output.getvalue())


if __name__ == '__main__':
    unittest.main()
