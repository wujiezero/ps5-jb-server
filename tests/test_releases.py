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

    def test_three_web_files_build_is_byte_identical_to_the_fixed_list(self):
        """With only the three historical web files, the package must not change at all."""
        import gzip
        import hashlib
        import io
        import re
        module = self.builder()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'src'
            for relative in ['server/' + p.name for p in (ROOT / 'server').glob('*.py')] + [
                    'web/index.html', 'web/app.js', 'web/style.css', 'rulesets/compiled.json',
                    'titledb/titles.json', 'titledb/UPSTREAM_LICENSE']:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / relative).read_bytes())
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
            # Extra matching files ship; others in web/ do not.
            (root / 'web' / 'extra.js').write_text('//')
            (root / 'web' / 'notes.md').write_text('x')
            (root / 'web' / 'Upper.js').write_text('x')
            artifact, _ = module.build_release(root, Path(directory) / 'out2')
            with tarfile.open(artifact) as archive:
                web = sorted(n for n in archive.getnames() if '/web/' in n)
            self.assertEqual([n.rsplit('/', 1)[1] for n in web], ['app.js', 'extra.js', 'index.html', 'style.css'])
            pattern = re.compile(module.web_file_pattern(root) + r'\Z')
            self.assertTrue(all(pattern.match(n.rsplit('/', 1)[1]) for n in web))

    def test_wrong_tag_is_refused(self):
        module = self.builder()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'tag|版本'):
                module.build_release(ROOT, Path(directory), tag='v9.0.0')


if __name__ == '__main__':
    unittest.main()
