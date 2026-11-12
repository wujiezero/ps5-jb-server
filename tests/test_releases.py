"""Versioned runtime release contract."""
import importlib.util
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from version import VERSION  # noqa: E402


class ReleaseTests(unittest.TestCase):
    def builder(self):
        self.assertTrue((ROOT / 'scripts/build_release.py').exists(), 'release builder is missing')
        spec = importlib.util.spec_from_file_location('build_release', ROOT / 'scripts/build_release.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_release_matches_version_and_contains_only_runtime_files(self):
        module = self.builder()
        with tempfile.TemporaryDirectory() as directory:
            artifact, checksum = module.build_release(ROOT, Path(directory), tag='v' + VERSION)
            self.assertEqual(artifact.name, f'ps5-jb-server-{VERSION}.tar.gz')
            import hashlib
            self.assertEqual(checksum.read_text(), hashlib.sha256(artifact.read_bytes()).hexdigest() + '  ' + artifact.name + '\n')
            with tarfile.open(artifact) as archive:
                names = archive.getnames()
                prefix = f'ps5-jb-server-{VERSION}/'
                self.assertIn(prefix + 'server/version.py', names)
                self.assertIn(prefix + 'server/update_worker.py', names)
                self.assertEqual(archive.extractfile(prefix + 'VERSION').read(), (VERSION + '\n').encode())
                self.assertFalse(any('credentials' in name or '/.git' in name or '/tests/' in name for name in names))
                self.assertTrue(all(item.isfile() for item in archive.getmembers()))
            first = artifact.read_bytes()
            module.build_release(ROOT, Path(directory), tag='v' + VERSION)
            self.assertEqual(first, artifact.read_bytes())

    def test_package_ships_only_the_three_legacy_web_files(self):
        """Update workers up to v1.9.0 refuse any other web/ member, so the front end is bundled."""
        import re
        module = self.builder()
        # The v1.4.0-v1.9.0 worker's whitelist, copied here on purpose: it can never change.
        legacy_runtime = {'VERSION', 'web/index.html', 'web/app.js', 'web/style.css', 'rulesets/compiled.json',
                          'titledb/titles.json', 'titledb/UPSTREAM_LICENSE'}
        legacy_required = {'server/server.py', 'server/update_worker.py', 'server/version.py',
                           'web/index.html', 'web/app.js', 'web/style.css', 'VERSION'}
        with tempfile.TemporaryDirectory() as directory:
            artifact, _ = module.build_release(ROOT, Path(directory))
            with tarfile.open(artifact) as archive:
                prefix = f'ps5-jb-server-{VERSION}/'
                members = {name[len(prefix):]: archive.extractfile(name).read() for name in archive.getnames()}
        for name in members:
            module_file = re.fullmatch(r'server/[A-Za-z_][A-Za-z0-9_]*\.py', name) and name != 'server/host.py'
            self.assertTrue(name in legacy_runtime or module_file, name)
        self.assertLessEqual(legacy_required, set(members))
        html = members['web/index.html'].decode()
        self.assertEqual(re.findall(r'<script[^>]*>', html), ['<script src="/app.js" defer>'])
        self.assertEqual(re.findall(r'<link rel="stylesheet"[^>]*>', html), ['<link rel="stylesheet" href="/style.css">'])
        source = (ROOT / 'web/index.html').read_text()
        scripts = re.findall(r'<script src="/([^"]+)" defer>', source)
        styles = re.findall(r'<link rel="stylesheet" href="/([^"]+)">', source)
        self.assertGreater(len(scripts), 1)
        self.assertEqual(html, re.sub(r'(?:  <script src="/[^"]+" defer></script>\n)+',
                                      '  <script src="/app.js" defer></script>\n',
                                      re.sub(r'(?:  <link rel="stylesheet" href="/[^"]+">\n)+',
                                             '  <link rel="stylesheet" href="/style.css">\n', source)))
        app, style = members['web/app.js'].decode(), members['web/style.css'].decode()
        self.assertTrue(app.startswith("'use strict';\n"))
        offsets = [app.index((ROOT / 'web' / name).read_text()[len("'use strict';\n"):]) for name in scripts]
        self.assertEqual(offsets, sorted(offsets))
        offsets = [style.index((ROOT / 'web' / name).read_text()) for name in styles]
        self.assertEqual(offsets, sorted(offsets))

    def test_bundled_tree_builds_byte_identical_to_the_fixed_list(self):
        """A web/ that is already index.html + app.js + style.css ships unchanged, as up to v1.9.0."""
        import gzip
        import hashlib
        import io
        module = self.builder()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'src'
            for relative in ['server/' + p.name for p in (ROOT / 'server').glob('*.py')] + [
                    'rulesets/compiled.json', 'titledb/titles.json', 'titledb/UPSTREAM_LICENSE']:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / relative).read_bytes())
            (root / 'web').mkdir()
            for name, data in module.bundle_web(ROOT).items():
                (root / 'web' / name).write_bytes(data)
            artifact, _ = module.build_release(root, Path(directory) / 'out')
            # Reference: the v1.9.0 builder's fixed file list, same deterministic tar format.
            paths = sorted((root / 'server').glob('*.py')) + [root / 'web' / n for n in ('index.html', 'app.js', 'style.css')]
            paths += [root / 'rulesets/compiled.json', root / 'titledb/titles.json', root / 'titledb/UPSTREAM_LICENSE']
            buffer = io.BytesIO()
            prefix = f'ps5-jb-server-{VERSION}/'
            with gzip.GzipFile(filename='', mode='wb', fileobj=buffer, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode='w', format=tarfile.USTAR_FORMAT) as archive:
                    entries = [(p.relative_to(root).as_posix(), p.read_bytes()) for p in paths]
                    entries.append(('VERSION', (VERSION + '\n').encode()))
                    for relative, data in sorted(entries):
                        info = tarfile.TarInfo(prefix + relative)
                        info.size, info.mode, info.mtime = len(data), 0o644, 0
                        archive.addfile(info, io.BytesIO(data))
            self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(),
                             hashlib.sha256(buffer.getvalue()).hexdigest())

    def test_web_sources_that_cannot_be_bundled_are_refused(self):
        module = self.builder()
        head = '<!doctype html>\n<head>\n'
        good = ('  <link rel="stylesheet" href="/style.css">\n  <link rel="stylesheet" href="/b.css">\n'
                '  <script src="/a.js" defer></script>\n  <script src="/app.js" defer></script>\n</head>\n')
        cases = {
            'unreferenced': (good, {'extra.js': "'use strict';\n"}),
            'style first': (good.replace('/style.css', '/x.css').replace('/b.css', '/style.css').replace('/x.css', '/b.css'), {}),
            'app last': (good.replace('/a.js', '/x.js').replace('/app.js', '/a.js').replace('/x.js', '/app.js'), {}),
            'inline': (good + '<script>alert(1)</script>\n', {}),
            'not strict': (good, {'a.js': 'var x;\n'}),
            'not adjacent': (good.replace('  <script src="/app.js"', '  <meta>\n  <script src="/app.js"'), {}),
        }
        for label, (tags, overrides) in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as directory:
                web = Path(directory) / 'web'
                web.mkdir()
                files = {'style.css': 'a{}', 'b.css': 'b{}', 'a.js': "'use strict';\nvar a;\n",
                         'app.js': "'use strict';\nbindEvents();\n", **overrides}
                for name, text in files.items():
                    (web / name).write_text(text)
                (web / 'index.html').write_text(head + tags)
                (Path(directory) / 'server').mkdir()
                (Path(directory) / 'server' / 'version.py').write_text((ROOT / 'server/version.py').read_text())
                with self.assertRaises(ValueError):
                    module.bundle_web(Path(directory))
        with tempfile.TemporaryDirectory() as directory:     # the good layout itself bundles
            web = Path(directory) / 'web'
            web.mkdir()
            for name, text in {'style.css': 'a{}', 'b.css': 'b{}', 'a.js': "'use strict';\nvar a;\n",
                               'app.js': "'use strict';\nbindEvents();\n"}.items():
                (web / name).write_text(text)
            (web / 'index.html').write_text(head + good)
            (Path(directory) / 'server').mkdir()
            (Path(directory) / 'server' / 'version.py').write_text((ROOT / 'server/version.py').read_text())
            out = module.bundle_web(Path(directory))
            self.assertEqual(out['app.js'].decode(),
                             "'use strict';\n/* ── a.js ── */\nvar a;\n\n/* ── app.js ── */\nbindEvents();\n")
            self.assertIn('<script src="/app.js" defer></script>\n</head>', out['index.html'].decode())
            self.assertEqual(out['index.html'].decode().count('<link rel="stylesheet"'), 1)

    def test_wrong_tag_is_refused(self):
        module = self.builder()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'tag|版本'):
                module.build_release(ROOT, Path(directory), tag='v9.0.0')


if __name__ == '__main__':
    unittest.main()
