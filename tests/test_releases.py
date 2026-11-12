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

    def test_wrong_tag_is_refused(self):
        module = self.builder()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'tag|版本'):
                module.build_release(ROOT, Path(directory), tag='v9.0.0')


if __name__ == '__main__':
    unittest.main()
