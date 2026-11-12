"""The updater only touches verified runtime files, and restores on failure."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
from unittest import mock

WORKER_PATH = Path(__file__).resolve().parents[1] / 'server' / 'update_worker.py'


def load_worker():
    if not WORKER_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location('tested_update_worker', WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = load_worker()


class UpdateWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.program = self.root / 'opt' / 'app'
        self.static = self.program / 'web'
        self.data = self.root / 'data'
        self.updates = self.data / 'updates'
        for directory in (self.program, self.static, self.updates):
            directory.mkdir(parents=True, exist_ok=True)
        self.config = self.root / 'config.toml'
        self.config.write_text('[ports]\nadmin = 8443\n')
        self.protected = {
            self.config: self.config.read_bytes(),
            self.program / 'host.py': b'# third party host\n',
            self.program / 'tls' / 'key.pem': b'private key',
            self.program / 'payload_repo_deps' / 'dependency.py': b'# dependency\n',
            self.data / 'credentials.json': b'{"secret":true}',
        }
        for path, content in self.protected.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (self.program / 'version.py').write_bytes(b'VERSION = "1.3.0"\n')
        (self.program / 'server.py').write_bytes(b'# old server\n')
        os.chmod(self.program / 'server.py', 0o640)
        (self.static / 'index.html').write_bytes(b'old index')
        self.original = {path: path.read_bytes() for path in self.program.rglob('*') if path.is_file()}
        self.archive = self.updates / 'release.tar.gz'
        self.spec = dict(schema=1, id='test-update-id', archive=str(self.archive), sha256='', version='1.4.0',
                         program_dir=str(self.program), static_dir=str(self.static),
                         data_dir=str(self.data), config_path=str(self.config),
                         admin_unit='ps5-jb-server.service', repo_unit='ps5-jb-repo.service',
                         health_url='https://127.0.0.1:8443/api/version',
                         status_path=str(self.updates / 'job.json'))
        self.events = []
        self.hooks = {'service': lambda action, unit: self.events.append((action, unit)),
                      'health': lambda url, version: self.events.append(('health', version)),
                      'verify_services': lambda spec: None}
        self.write_archive()

    def runtime_files(self):
        return {'server/server.py': b'VALUE = "new server"\n',
                'server/update_worker.py': b'# verified worker\n',
                'server/version.py': b'VERSION = "1.4.0"\n',
                'web/index.html': b'new index', 'web/app.js': b'new js',
                'web/style.css': b'new css', 'rulesets/compiled.json': b'{}',
                'titledb/titles.json': b'{}', 'titledb/UPSTREAM_LICENSE': b'license',
                'VERSION': b'1.4.0\n'}

    def write_archive(self, overrides=None, extra=None, omit=()):
        files = self.runtime_files()
        files.update(overrides or {})
        for name in omit:
            files.pop(name, None)
        with tarfile.open(self.archive, 'w:gz') as archive:
            for name, content in files.items():
                info = tarfile.TarInfo('ps5-jb-server-1.4.0/' + name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            for info, content in extra or ():
                archive.addfile(info, io.BytesIO(content) if content else None)
        self.spec['sha256'] = hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def run_worker(self, hooks=None):
        self.assertIsNotNone(worker, 'standalone update worker must be implemented')
        selected = dict(self.hooks)
        selected.update(hooks or {})
        return worker.run(self.spec, selected)

    def assert_protected(self):
        for path, content in self.protected.items():
            self.assertEqual(path.read_bytes(), content, str(path))

    def assert_unchanged(self):
        self.assertEqual({path: path.read_bytes() for path in self.program.rglob('*') if path.is_file()},
                         self.original)
        self.assert_protected()

    def test_verified_release_installs_and_keeps_backups_and_secrets(self):
        result = self.run_worker()
        self.assertEqual(result['state'], 'done', result)
        self.assertEqual((self.program / 'server.py').read_bytes(), self.runtime_files()['server/server.py'])
        self.assertEqual((self.static / 'index.html').read_bytes(), b'new index')
        self.assertEqual((self.program / 'rulesets' / 'compiled.json').read_bytes(), b'{}')
        self.assertEqual((self.program / 'titledb' / 'UPSTREAM_LICENSE').read_bytes(), b'license')
        self.assertEqual((self.program / 'VERSION').read_text(), '1.4.0\n')
        self.assert_protected()
        self.assertEqual(self.events, [('stop', self.spec['repo_unit']), ('stop', self.spec['admin_unit']),
                                       ('start', self.spec['repo_unit']), ('start', self.spec['admin_unit']),
                                       ('health', '1.4.0')])
        persisted = json.loads(Path(self.spec['status_path']).read_text())
        self.assertEqual(persisted['state'], 'done')
        self.assertEqual(persisted['version'], '1.4.0')
        self.assertEqual(persisted.get('id'), 'test-update-id')
        self.assertIsInstance(persisted['updated_at'], (float, int))
        self.assertEqual(stat.S_IMODE(Path(self.spec['status_path']).stat().st_mode), 0o600)
        backups = list((self.updates / 'backups').glob('*/program/server.py'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b'# old server\n')
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o640)

    def test_checksum_fails_before_backup_or_services(self):
        self.spec['sha256'] = '0' * 64
        result = self.run_worker()
        self.assertEqual(result['state'], 'failed')
        self.assertIn('SHA', result['error'])
        self.assertEqual(self.events, [])
        self.assertFalse((self.updates / 'backups').exists())
        self.assert_unchanged()

    def test_unsafe_tar_members_are_rejected_before_services(self):
        for name, kind in [('ps5-jb-server-1.4.0/../escape', tarfile.REGTYPE),
                           ('/absolute', tarfile.REGTYPE),
                           ('ps5-jb-server-1.4.0/server/link.py', tarfile.SYMTYPE),
                           ('ps5-jb-server-1.4.0/server/hard.py', tarfile.LNKTYPE),
                           ('ps5-jb-server-1.4.0/server/fifo', tarfile.FIFOTYPE),
                           ('ps5-jb-server-1.4.0/server/server.py', tarfile.REGTYPE),
                           ('ps5-jb-server-1.4.0/server/host.py', tarfile.REGTYPE),
                           ('ps5-jb-server-1.4.0/config.toml', tarfile.REGTYPE),
                           ('ps5-jb-server-1.4.0/docs/readme.md', tarfile.REGTYPE),
                           ('ps5-jb-server-1.4.0/server/nested/a.py', tarfile.REGTYPE)]:
            with self.subTest(name=name, kind=kind):
                info = tarfile.TarInfo(name)
                info.type = kind
                info.linkname = '../config.toml' if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ''
                self.write_archive(extra=[(info, b'')])
                result = self.run_worker()
                self.assertEqual(result['state'], 'failed', result)
                self.assertEqual(self.events, [])
                self.assert_unchanged()

    def test_required_files_versions_and_python_syntax_are_verified_without_execution(self):
        marker = self.root / 'executed'
        cases = [({'VERSION': b'1.3.9\n'}, ()),
                 ({'server/version.py': b'VERSION = "1.3.9"\n'}, ()),
                 ({'server/version.py': b'VERSION = str("1.4.0")\n'}, ()),
                 ({'server/server.py': b'def broken(:\n'}, ())]
        cases += [({}, (name,)) for name in ('server/server.py', 'server/update_worker.py',
                                            'server/version.py', 'web/index.html', 'web/app.js',
                                            'web/style.css', 'VERSION')]
        for overrides, omit in cases:
            with self.subTest(overrides=overrides, omit=omit):
                self.write_archive(overrides=overrides, omit=omit)
                self.assertEqual(self.run_worker()['state'], 'failed')
                self.assertEqual(self.events, [])
                self.assert_unchanged()
        self.write_archive(overrides={'server/server.py':
                           f'open({str(marker)!r}, "w").write("executed")\n'.encode()})
        self.assertEqual(self.run_worker()['state'], 'done')
        self.assertFalse(marker.exists())

    def test_health_failure_restores_all_files_and_removes_new_files(self):
        def fail_health(url, version):
            if version == '1.4.0':
                raise RuntimeError('wrong version')
        self.hooks['health'] = fail_health
        result = self.run_worker()
        self.assertEqual(result['state'], 'rolled_back', result)
        self.assertIn('wrong version', result['error'])
        self.assertNotIn('rollback_error', result)
        self.assert_unchanged()
        self.assertEqual(stat.S_IMODE((self.program / 'server.py').stat().st_mode), 0o640)
        self.assertEqual(self.events[-4:], [('stop', self.spec['repo_unit']),
                                           ('stop', self.spec['admin_unit']),
                                           ('start', self.spec['repo_unit']),
                                           ('start', self.spec['admin_unit'])])

    def test_partial_replace_and_service_start_failure_roll_back(self):
        for failure in ('replace', 'start'):
            with self.subTest(failure=failure):
                self.events.clear()
                fired = []
                def service(action, unit):
                    self.events.append((action, unit))
                    if failure == 'start' and action == 'start' and not fired:
                        fired.append(True)
                        raise RuntimeError('injected start failure')
                def replace(src, dst):
                    if failure == 'replace' and Path(dst).name == 'version.py' and not fired:
                        fired.append(True)
                        raise RuntimeError('injected replace failure')
                    os.replace(src, dst)
                result = self.run_worker({'service': service, 'replace': replace,
                                          'health': self.hooks['health']})
                self.assertEqual(result['state'], 'rolled_back', result)
                self.assertIn('injected', result['error'])
                self.assert_unchanged()

    def test_repository_restart_failure_is_rolled_back_and_status_keeps_job_id(self):
        fired = []
        def service(action, unit):
            self.events.append((action, unit))
            if action == 'start' and unit == self.spec['repo_unit'] and not fired:
                fired.append(True)
                raise RuntimeError('repository startup failed')
        self.hooks['service'] = service
        result = self.run_worker()
        self.assertEqual(result['state'], 'rolled_back', result)
        self.assertEqual(result.get('id'), self.spec['id'])
        self.assertIn('repository startup failed', result['error'])
        self.assertEqual(json.loads(Path(self.spec['status_path']).read_text()), result)
        self.assert_unchanged()

    def test_restore_failure_is_reported_and_old_services_restart_is_attempted(self):
        fired = []
        def replace(src, dst):
            if Path(dst) == self.program / 'server.py':
                if fired:
                    raise OSError('cannot restore')
                fired.append(True)
            os.replace(src, dst)
        self.hooks['replace'] = replace
        self.hooks['health'] = lambda *args: (_ for _ in ()).throw(RuntimeError('unhealthy'))
        result = self.run_worker()
        self.assertEqual(result['state'], 'failed', result)
        self.assertIn('cannot restore', result['rollback_error'])
        self.assertIn(('start', self.spec['admin_unit']), self.events[-2:])
        self.assert_protected()

    def test_symlink_destination_and_status_parent_are_rejected(self):
        outside = self.root / 'outside'
        outside.write_bytes(b'keep')
        (self.program / 'server.py').unlink()
        (self.program / 'server.py').symlink_to(outside)
        self.assertEqual(self.run_worker()['state'], 'failed')
        self.assertEqual(outside.read_bytes(), b'keep')
        self.assertEqual(self.events, [])
        (self.program / 'server.py').unlink()
        (self.program / 'server.py').write_bytes(b'# old server\n')
        redirect = self.root / 'redirect'
        redirect.symlink_to(self.updates, target_is_directory=True)
        self.spec['status_path'] = str(redirect / 'evil.json')
        self.assertEqual(self.run_worker()['state'], 'failed')
        self.assertFalse((self.updates / 'evil.json').exists())

    def test_invalid_spec_cannot_target_other_hosts_units_or_protected_files(self):
        for key, value in [('schema', True), ('version', 'v1.4.0'), ('version', '01.4.0'),
                           ('health_url', 'https://example.org:8443/api/version'),
                           ('health_url', 'http://127.0.0.1:8443/api/version'),
                           ('health_url', 'https://127.0.0.1:8443/api/version?redirect=1'),
                           ('admin_unit', '--root=/'), ('repo_unit', '../evil.service'),
                           ('status_path', str(self.config)), ('archive', 'relative.tar.gz'),
                           ('program_dir', str(self.data)), ('static_dir', str(self.data))]:
            with self.subTest(key=key, value=value):
                original = self.spec[key]
                self.spec[key] = value
                result = self.run_worker()
                self.assertEqual(result['state'], 'failed', result)
                self.assertEqual(self.events, [])
                self.assert_protected()
                self.spec[key] = original

    def test_lan_health_endpoint_matches_installed_admin_bind_address(self):
        self.assertIsNotNone(worker, 'standalone update worker must be implemented')
        self.spec['health_url'] = 'https://192.0.2.35:8443/api/version'
        self.assertEqual(self.run_worker()['state'], 'done')
        self.assertIn(('health', '1.4.0'), self.events)

    def test_health_connection_uses_exact_ip_host_and_does_not_follow_redirects(self):
        self.assertIsNotNone(worker, 'standalone update worker must be implemented')
        response = mock.Mock(status=200)
        response.read.return_value = b'{"version":"1.4.0","ready":true}'
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(worker.http.client, 'HTTPSConnection', return_value=connection) as connect:
            worker._health('https://192.0.2.35:8443/api/version', '1.4.0')
        self.assertEqual(connect.call_args.args, ('192.0.2.35', 8443))
        connection.request.assert_called_once_with('GET', '/api/version',
            headers={'Host': '192.0.2.35:8443', 'Accept': 'application/json'})
        connection.close.assert_called_once()

    def test_health_false_result_triggers_rollback(self):
        self.hooks['health'] = lambda url, version: version != '1.4.0'
        result = self.run_worker()
        self.assertEqual(result['state'], 'rolled_back', result)
        self.assertIn('health', result['error'])
        self.assert_unchanged()

    def test_destination_parent_file_is_rejected_before_services(self):
        (self.program / 'rulesets').write_bytes(b'not a directory')
        result = self.run_worker()
        self.assertEqual(result['state'], 'failed', result)
        self.assertEqual(self.events, [])
        self.assertEqual((self.program / 'rulesets').read_bytes(), b'not a directory')
        self.assertEqual((self.program / 'server.py').read_bytes(), b'# old server\n')

    def test_tar_directory_backslash_empty_segments_and_foreign_root_are_rejected(self):
        for name, kind in [('ps5-jb-server-1.4.0/server', tarfile.DIRTYPE),
                           ('ps5-jb-server-1.4.0/server\\\\bad.py', tarfile.REGTYPE),
                           ('ps5-jb-server-1.4.0/server//bad.py', tarfile.REGTYPE),
                           ('ps5-jb-server-1.3.9/server/bad.py', tarfile.REGTYPE)]:
            with self.subTest(name=name):
                info = tarfile.TarInfo(name)
                info.type = kind
                self.write_archive(extra=[(info, b'')])
                self.assertEqual(self.run_worker()['state'], 'failed')
                self.assertEqual(self.events, [])
                self.assert_unchanged()

    def test_states_are_persisted_before_corresponding_operations(self):
        observed = []
        def state():
            return json.loads(Path(self.spec['status_path']).read_text())['state']
        def service(action, unit):
            observed.append((action, state()))
        def replace(src, dst):
            observed.append(('replace', state()))
            os.replace(src, dst)
        def health(url, version):
            observed.append(('health', state()))
        self.assertEqual(self.run_worker(dict(service=service, replace=replace, health=health))['state'], 'done')
        self.assertTrue(all(current == 'installing' for action, current in observed if action in ('stop', 'replace')))
        self.assertTrue(all(current == 'restarting' for action, current in observed if action == 'start'))
        self.assertEqual(observed[-1], ('health', 'verifying'))

    def test_optional_repository_unit_only_restarts_admin(self):
        del self.spec['repo_unit']
        self.assertEqual(self.run_worker()['state'], 'done')
        self.assertEqual(self.events[:2], [('stop', self.spec['admin_unit']), ('start', self.spec['admin_unit'])])

    def test_resource_limits_reject_archive_before_services(self):
        self.assertIsNotNone(worker, 'standalone update worker must be implemented')
        for key, value in [('MAX_FILES', 2), ('MAX_FILE_BYTES', 4), ('MAX_TOTAL_BYTES', 10)]:
            with self.subTest(limit=key), mock.patch.object(worker, key, value):
                self.assertEqual(self.run_worker()['state'], 'failed')
                self.assertEqual(self.events, [])
                self.assert_unchanged()

    def test_cli_rejects_source_checkout_and_untrusted_spec_without_services(self):
        self.assertIsNotNone(worker, 'standalone update worker must be implemented')
        path = self.updates / 'spec.json'
        path.write_text(json.dumps(self.spec))
        os.chmod(path, 0o600)
        with mock.patch.object(worker.subprocess, 'run') as services, \
                mock.patch.object(worker.os, 'getuid', return_value=0), \
                mock.patch.object(worker, '_require_root_owned'), \
                mock.patch.object(worker, '_secure_parents'), \
                mock.patch.object(worker.sys, 'stderr', new_callable=io.StringIO) as errors:
            self.assertNotEqual(worker.main([str(path)]), 0)
            self.assertIn('flat installed program directory under /opt', errors.getvalue())
        services.assert_not_called()
        self.assert_unchanged()

    def interrupt_transaction(self):
        def interrupt(src, dst):
            os.replace(src, dst)
            raise KeyboardInterrupt('worker interrupted')
        with self.assertRaises(KeyboardInterrupt):
            self.run_worker({'replace': interrupt})

    def test_duplicate_worker_cannot_overlap_active_transaction_or_overwrite_status(self):
        import fcntl
        lock = self.updates / 'transaction.lock'
        with lock.open('w') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_worker()
        self.assertEqual(result['state'], 'failed', result)
        self.assertIn('active', result['error'])
        self.assertFalse(Path(self.spec['status_path']).exists())
        self.assertEqual(self.events, [])
        self.assert_unchanged()

    def test_flat_lowercase_web_files_are_accepted(self):
        self.write_archive(overrides={'web/extra.js': b'// split module\n', 'web/files-view.css': b'a{}',
                                      'web/help.html': b'<p>'})
        result = self.run_worker()
        self.assertEqual(result['state'], 'done', result)
        self.assertEqual((self.static / 'extra.js').read_bytes(), b'// split module\n')
        self.assertEqual((self.static / 'files-view.css').read_bytes(), b'a{}')

    def test_new_web_file_is_removed_on_rollback(self):
        self.write_archive(overrides={'web/extra.js': b'// new'})
        self.hooks['health'] = lambda url, version: (_ for _ in ()).throw(RuntimeError('bad')) \
            if version == '1.4.0' else None
        result = self.run_worker()
        self.assertEqual(result['state'], 'rolled_back', result)
        self.assertFalse((self.static / 'extra.js').exists())

    def test_web_files_outside_the_rule_are_rejected(self):
        for name in ('web/sub/x.js', 'web/x.php', 'web/.hidden.js', 'web/X.JS', 'web/x.js.map',
                     'web/' + 'a' * 65 + '.js', 'web/-x.js'):
            with self.subTest(name=name):
                self.write_archive(overrides={name: b'x'})
                result = self.run_worker()
                self.assertEqual(result['state'], 'failed', result)
                self.assertIn('unexpected runtime member', result['error'])
                self.assertEqual(self.events, [])
                self.assert_unchanged()

    def test_worker_rule_matches_the_shared_constant(self):
        import sys
        sys.path.insert(0, str(WORKER_PATH.parent))
        from version import WEB_FILE_PATTERN
        self.assertEqual(worker.WEB_FILE_PATTERN, WEB_FILE_PATTERN)

    def test_recovery_accepts_split_web_files(self):
        paths = {'program_dir': self.program, 'static_dir': self.static}
        self.assertEqual(worker._runtime_name(self.static / 'extra.js', paths), 'web/extra.js')
        with self.assertRaises(ValueError):
            worker._runtime_name(self.static / 'x.php', paths)

    def test_interrupted_transaction_persists_recovery_manifest_before_replacement(self):
        self.interrupt_transaction()
        transaction = self.updates / 'transaction.json'
        self.assertTrue(transaction.exists(), 'interrupted deployment must retain a recovery manifest')
        journal = json.loads(transaction.read_text())
        self.assertEqual(journal['phase'], 'replacing')
        self.assertEqual(journal['previous_version'], '1.3.0')
        self.assertEqual(journal['spec']['id'], self.spec['id'])
        self.assertEqual(len(journal['entries']), len(self.runtime_files()))
        self.assertEqual(stat.S_IMODE(transaction.stat().st_mode), 0o600)

    def test_live_recovery_restores_files_and_checks_old_version(self):
        self.interrupt_transaction()
        self.assertTrue(callable(getattr(worker, 'recover', None)), 'recovery API must exist')
        self.events.clear()
        result = worker.recover(self.spec, self.hooks)
        self.assertEqual(result['state'], 'rolled_back', result)
        self.assertIn(('health', '1.3.0'), self.events)
        self.assert_unchanged()
        self.assertEqual(json.loads((self.updates / 'transaction.json').read_text())['phase'], 'restored')

    def test_boot_recovery_restores_without_service_actions_and_reports_pending_health(self):
        self.interrupt_transaction()
        self.assertTrue(callable(getattr(worker, 'recover', None)), 'recovery API must exist')
        self.events.clear()
        result = worker.recover(self.spec, self.hooks, boot=True)
        self.assertEqual(result['state'], 'failed', result)
        self.assertTrue(result['restored'])
        self.assertTrue(result['recovery_pending'])
        self.assertEqual(self.events, [])
        self.assert_unchanged()

    def test_recovery_rejects_manifest_target_outside_runtime_allowlist(self):
        self.interrupt_transaction()
        transaction = self.updates / 'transaction.json'
        self.assertTrue(transaction.exists(), 'recovery manifest must exist')
        journal = json.loads(transaction.read_text())
        journal['entries'][0]['target'] = str(self.config)
        transaction.write_text(json.dumps(journal))
        self.assertTrue(callable(getattr(worker, 'recover', None)), 'recovery API must exist')
        self.events.clear()
        result = worker.recover(self.spec, self.hooks, boot=True)
        self.assertEqual(result['state'], 'failed')
        self.assertIn('target', result['error'])
        self.assertEqual(self.events, [])
        self.assert_protected()

    def test_pending_transaction_prevents_new_deployment(self):
        self.interrupt_transaction()
        self.events.clear()
        result = self.run_worker()
        self.assertEqual(result['state'], 'failed', result)
        self.assertIn('recovery', result['error'])
        self.assertEqual(self.events, [])

    def test_equal_or_older_update_is_rejected_under_lock(self):
        for old in ('1.4.0', '2.0.0'):
            with self.subTest(installed=old):
                (self.program / 'version.py').write_text(f'VERSION = {old!r}\n')
                result = self.run_worker()
                self.assertEqual(result['state'], 'failed', result)
                self.assertIn('newer', result['error'])
                self.assertEqual(self.events, [])

    def test_rollback_checks_previous_version_and_services_before_success(self):
        def health(url, version):
            self.events.append(('health', version))
            raise RuntimeError('gateway unavailable ' + version)
        result = self.run_worker({'health': health})
        self.assertEqual(result['state'], 'failed', result)
        self.assertIn('1.3.0', result['rollback_error'])
        self.assertIn(('health', '1.3.0'), self.events)
        self.assert_unchanged()

    def test_gateway_readiness_false_is_not_healthy_even_with_correct_version(self):
        response = mock.Mock(status=200)
        response.read.return_value = b'{"version":"1.4.0","ready":false}'
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(worker.http.client, 'HTTPSConnection', return_value=connection), \
                mock.patch.object(worker.time, 'monotonic', side_effect=[0, 0, 46]), \
                mock.patch.object(worker.time, 'sleep'):
            with self.assertRaises(RuntimeError):
                worker._health(self.spec['health_url'], '1.4.0')

    def test_repository_health_requires_active_units_and_successful_http(self):
        self.assertTrue(callable(getattr(worker, '_verify_services', None)), 'service health API must exist')
        response = mock.Mock(status=200)
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(worker.subprocess, 'run') as service, \
                mock.patch.object(worker.http.client, 'HTTPConnection', return_value=connection) as http:
            worker._verify_services(self.spec)
        self.assertEqual(service.call_count, 2)
        self.assertEqual(http.call_args.args[:2], ('127.0.0.1', 8080))
        self.assertEqual(connection.request.call_args.args, ('GET', '/repo.json'))
        response.read.assert_called_once_with(65536)

    def test_load_spec_binds_original_spec_path(self):
        descriptor = {key: self.spec[key] for key in worker.DESCRIPTOR_FIELDS}
        descriptor['schema'] = 1
        (self.program / 'update-install.json').write_text(json.dumps(descriptor))
        spec_path = self.updates / 'spec.json'
        spec_path.write_text(json.dumps(self.spec))
        os.chmod(spec_path, 0o600)
        with mock.patch.object(worker, 'INSTALL_ROOT', self.root / 'opt'), \
                mock.patch.object(worker.os, 'getuid', return_value=0), \
                mock.patch.object(worker, '_secure_parents'), \
                mock.patch.object(worker, '_require_root_owned'):
            loaded = worker.load_spec(spec_path)
            self.assertEqual(loaded['spec_path'], str(spec_path))
            mismatched = dict(self.spec, spec_path=str(self.updates / 'other.json'))
            spec_path.write_text(json.dumps(mismatched))
            with self.assertRaises(ValueError):
                worker.load_spec(spec_path)

    def test_cli_recover_dispatches_descriptor_and_returns_success(self):
        self.assertTrue(callable(getattr(worker, 'recover_install', None)))
        with mock.patch.object(worker, 'recover_install', return_value={'state': 'idle'}):
            self.assertEqual(worker.main(['--recover', '/opt/app/update-install.json']), 0)
        with mock.patch.object(worker, 'recover_install', return_value={'state': 'failed', 'rollback_error': 'bad'}):
            self.assertEqual(worker.main(['--recover', '/opt/app/update-install.json']), 1)

    def test_boot_recovery_skips_when_updater_holds_lock(self):
        import fcntl
        lock = self.updates / 'transaction.lock'
        with lock.open('w') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = worker.recover(self.spec, self.hooks, boot=True)
        self.assertEqual(result, {'state': 'skipped', 'active': True})
        self.assertEqual(self.events, [])

    def test_production_descriptor_requires_exact_paths_owner_and_mode(self):
        self.assertIsNotNone(worker, 'standalone update worker must be implemented')
        descriptor = {key: self.spec[key] for key in worker.DESCRIPTOR_FIELDS}
        descriptor['schema'] = 1
        path = self.program / 'update-install.json'
        path.write_text(json.dumps(descriptor))
        os.chmod(path, 0o644)
        spec_path = self.updates / 'spec.json'
        spec_path.write_text(json.dumps(self.spec))
        os.chmod(spec_path, 0o600)
        # Inject the production boundary and ownership policy for sandbox temp
        # paths (/tmp ancestors may be world-writable on Linux). File-mode and
        # symlink checks still run against the actual files.
        with mock.patch.object(worker, 'INSTALL_ROOT', self.root / 'opt'), \
                mock.patch.object(worker.os, 'getuid', return_value=0), \
                mock.patch.object(worker, '_secure_parents'):
            with mock.patch.object(worker, '_require_root_owned') as owned:
                self.assertEqual(worker.load_spec(spec_path)['version'], '1.4.0')
                self.assertGreaterEqual(owned.call_count, 2)
            bad = dict(descriptor, static_dir=str(self.root / 'other'))
            path.write_text(json.dumps(bad))
            with mock.patch.object(worker, '_require_root_owned'):
                with self.assertRaises(ValueError):
                    worker.load_spec(spec_path)
            path.write_text(json.dumps(descriptor))
            os.chmod(spec_path, 0o666)
            with mock.patch.object(worker, '_require_root_owned'):
                with self.assertRaises(ValueError):
                    worker.load_spec(spec_path)


if __name__ == '__main__':
    unittest.main()
