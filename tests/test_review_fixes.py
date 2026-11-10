"""Regression tests for the 2026-11-10 review: wiring, persistence, files, autoload."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))

from activity import Classifier, FlowObserver, LabelsStore  # noqa: E402
from api_console import ConsoleAPI, _disposition  # noqa: E402
from dns_service import DNSPolicy  # noqa: E402
from files import FileProxy, FileServiceError, _check_path  # noqa: E402
from policy import ConfigStore  # noqa: E402
from ps5 import autoload_view, parse_autoload, serialize_autoload  # noqa: E402
from server import AuthManager, RepoManager, change_password, create_engine  # noqa: E402
from settings import Settings  # noqa: E402

RULESETS = Path(__file__).resolve().parents[1] / 'rulesets' / 'compiled.json'
REAL_LIST = ('local/shadowmountplus,!3000,local/kstuff1,!3000,local/elfldr,!3000,local/zftpd,'
             '!3000,local/web-file-mgr,!3000,local/youtube-update-blocker,!3000,'
             'local/ps5-syslang-zh-hans')


class NullFirewall:
    def __init__(self):
        self.calls = []

    def apply(self, config, leases):
        self.calls.append((config, leases))


class EngineWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ConfigStore(Path(self.tmp.name) / 'config.json',
                                 {'domains': [], 'networks': [], 'upstream_dns': '192.0.2.1'})

    def tearDown(self):
        self.tmp.cleanup()

    def test_dns_serves_the_configured_lan(self):
        engine = create_engine(self.store, '192.0.2.20', '192.0.2.0/24', firewall=NullFirewall())
        self.assertEqual(str(engine.lan), '192.0.2.0/24')

    def test_upstream_dns_cannot_be_this_service(self):
        engine = create_engine(self.store, '192.0.2.20', '192.0.2.0/24', firewall=NullFirewall())
        with self.assertRaisesRegex(ValueError, '本服务地址'):
            engine.change_config({'domains': [], 'networks': [], 'upstream_dns': '192.0.2.20'})
        with self.assertRaisesRegex(ValueError, '广播'):
            engine.change_config({'domains': [], 'networks': [], 'upstream_dns': '192.0.2.255'})
        engine.change_config({'domains': [], 'networks': [], 'upstream_dns': '192.0.2.1'})

    def test_validation_errors_are_chinese(self):
        engine = create_engine(self.store, '192.0.2.20', '192.0.2.0/24', firewall=NullFirewall())
        with self.assertRaisesRegex(ValueError, 'IPv4'):
            engine.change_config({'domains': [], 'networks': ['not-an-ip'], 'upstream_dns': '192.0.2.1'})

    def test_live_leases_is_read_only(self):
        engine = DNSPolicy(self.store, NullFirewall(), clock=lambda: 100.0)
        engine.leases = {'198.51.100.7': 50.0, '198.51.100.8': 200.0}
        self.assertEqual(engine.live_leases(), {'198.51.100.8': 100})
        self.assertIn('198.51.100.7', engine.leases, 'pruning belongs to the DNS thread')


class SessionTests(unittest.TestCase):
    def test_password_change_revokes_persisted_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / 'sessions.json'
            credentials = Path(tmp) / 'credentials.json'
            creds = AuthManager.make_credentials('first-password-1')
            credentials.write_text(json.dumps(creds))
            auth = AuthManager(creds, sessions)
            token, _ = auth.login('127.0.0.1', 'first-password-1')
            change_password(auth, credentials, 'first-password-1', 'second-password-2')
            restarted = AuthManager(json.loads(credentials.read_text()), sessions)
            self.assertIsNone(restarted.session(token))

    def test_renewal_does_not_rewrite_file_on_every_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = AuthManager(AuthManager.make_credentials('a-long-password'),
                               Path(tmp) / 'sessions.json')
            token, _ = auth.login('127.0.0.1', 'a-long-password')
            with mock.patch.object(auth, '_save') as save:
                for _ in range(5):
                    self.assertIsNotNone(auth.session(token))
            save.assert_not_called()


class RepoTests(unittest.TestCase):
    def test_replace_keeps_metadata_and_updates_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = RepoManager(tmp, base_url='http://192.0.2.20:8080/payloads/', max_upload=1024)
            repo.add('tool.elf', b'old', name='Tool', version='1')
            with self.assertRaises(FileExistsError):
                repo.add('tool.elf', b'new', name='Tool')
            entry = repo.add('tool.elf', b'newer', name='', version='2', replace=True)
            self.assertEqual(entry['name'], 'Tool')
            self.assertEqual(entry['version'], '2')
            self.assertEqual((Path(tmp) / 'payloads' / 'tool.elf').read_bytes(), b'newer')
            with self.assertRaisesRegex(ValueError, '1MB|0MB'):
                repo.add('big.elf', b'x' * 2048, name='Big')
            with self.assertRaises(FileNotFoundError):
                repo.add('absent.elf', b'x', name='', replace=True)


class FileTests(unittest.TestCase):
    def test_volume_roots_are_protected_from_mutation(self):
        for path in ('/data', '/data/', '/mnt/usb0', '/mnt/ext1'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                _check_path(path, allow_root=False)
        self.assertEqual(_check_path('/data/x', allow_root=False), '/data/x')

    def test_falls_back_to_ftp_when_http_service_is_down(self):
        proxy = FileProxy('127.0.0.0/8', host='127.0.0.1', port=1, timeout=0.5, ftp_port=2120)
        fake = mock.Mock()
        fake.list.return_value = {'path': '/data', 'entries': [], 'truncated': False}
        with mock.patch.object(proxy, '_ftp_files', return_value=fake):
            self.assertEqual(proxy.list('/data')['path'], '/data')
            self.assertEqual(proxy.backend, 'ftp')
            fake.probe.assert_called_once()

    def test_reports_both_services_when_nothing_answers(self):
        proxy = FileProxy('127.0.0.0/8', host='127.0.0.1', port=1, timeout=0.5, ftp_port=1)
        with self.assertRaisesRegex(FileServiceError, 'zftpd'):
            proxy.volumes()

    def test_download_keeps_the_real_filename(self):
        header = _disposition('存档 1.bin')
        self.assertIn("filename*=UTF-8''%E5%AD%98%E6%A1%A3%201.bin", header)

    def test_conflicts_surface_as_409(self):
        settings = Settings.from_mapping({'network': {'host_ip': '127.0.0.1', 'subnet': '127.0.0.0/8'},
                                          'paths': {'data': tempfile.mkdtemp()}})
        api = ConsoleAPI(settings, lambda: {'items': []})
        handler = mock.Mock()
        api._fail(handler, FileServiceError('exists', status=409))
        self.assertEqual(handler.send_body.call_args[0][0], 409)


class SettingsTests(unittest.TestCase):
    def test_env_false_disables_features(self):
        with mock.patch.dict('os.environ', {'PS5JB_PS5_CONTROL': 'false', 'PS5JB_FILE_MANAGER': '0',
                                            'PS5JB_HOST_IP': '192.0.2.20'}):
            settings = Settings.from_mapping({})
        self.assertFalse(settings.ps5_control)
        self.assertFalse(settings.file_manager)
        self.assertEqual(settings.max_file_upload, 0)
        self.assertEqual(settings.ftp_port, 2120)


class AutoloadTests(unittest.TestCase):
    def test_real_sequence_round_trips(self):
        parsed = parse_autoload(REAL_LIST)
        self.assertEqual(len(parsed['items']), 7)
        self.assertEqual(parsed['items'][0], {'key': 'local/shadowmountplus', 'filename': 'shadowmountplus',
                                              'installed': False, 'delay_ms': 3000})
        self.assertEqual(serialize_autoload(parsed['items'], parsed['lead_ms']), REAL_LIST)

    def test_leading_and_stacked_delays(self):
        parsed = parse_autoload('!1000,local/a,!500,!500,local/b')
        self.assertEqual(parsed['lead_ms'], 1000)
        self.assertEqual(parsed['items'][0]['delay_ms'], 1000)
        self.assertEqual(serialize_autoload(parsed['items'], parsed['lead_ms']),
                         '!1000,local/a,!1000,local/b')

    def test_rejects_bad_entries(self):
        for items in ([{'key': 'a,b'}], [{'key': '!3000'}], [{'key': '/data/x.elf'}],
                      [{'key': 'local/../x'}], [{'key': 'local/a', 'delay_ms': -1}],
                      [{'key': 'local/a', 'delay_ms': 3600001}], [{'key': 'local/a', 'delay_ms': True}]):
            with self.subTest(items=items), self.assertRaises(ValueError):
                serialize_autoload(items)

    def test_view_maps_slots_and_last_outcomes(self):
        payloads = [{'slot': 'local/kstuff1', 'filename': 'kstuff.elf'}]
        view = autoload_view({'AUTOLOAD_ENABLED': True, 'AUTOLOAD_LIST': 'local/kstuff1,!3000,local/gone',
                              'AUTOLOAD_DELAY': 7},
                             {'list': 'local/kstuff1,!3000,local/gone', 'outcomes': 'sf'}, payloads)
        self.assertEqual(view['start_delay'], 7)
        self.assertEqual([(i['filename'], i['installed'], i['outcome']) for i in view['items']],
                         [('kstuff.elf', True, 'success'), ('gone', False, 'failed')])
        stale = autoload_view({'AUTOLOAD_LIST': 'local/kstuff1'}, {'list': 'other', 'outcomes': 'f'}, payloads)
        self.assertIsNone(stale['items'][0]['outcome'])


class DiscoveryTests(unittest.TestCase):
    def test_rescan_with_known_hosts(self):
        from ps5 import Discovery
        discovery = Discovery('127.0.0.1/32', timeout=0.05)
        discovery.extra = ['127.0.0.1']          # filled by every earlier scan
        result = discovery.scan(['127.0.0.1', 'not-an-ip', '198.51.100.1'])
        self.assertEqual(result['scanned'], 1)
        self.assertFalse(discovery.snapshot()['scanning'])


class DnsActivityTests(unittest.TestCase):
    def test_dns_rows_are_read_once_and_nodata_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = []
            allowed = {'ok.example'}
            observer = FlowObserver(
                conntrack=lambda: '', classifier=Classifier(RULESETS),
                labels=LabelsStore(Path(tmp) / 'labels.json'),
                dns_lookup=lambda ip: (None, None), is_allowed=lambda ip: False,
                lan='192.0.2.0/24', host_ip='192.0.2.20', clock=lambda: 1000.0,
                dns_events=lambda: list(rows), domain_allowed=lambda name: name in allowed)
            rows[:0] = [{'seq': 1, 'client': '192.0.2.5', 'name': 'ok.example', 'qtype': 'AAAA',
                         'action': 'nodata'},
                        {'seq': 2, 'client': '192.0.2.5', 'name': 'blocked.example', 'qtype': 'A',
                         'action': 'blocked'}][::-1]
            observer.poll_once()
            observer.poll_once()
            events = observer.snapshot()['events']
            self.assertEqual([e['domain'] for e in events], ['blocked.example'])
            self.assertFalse(events[0]['allowed_now'])
            allowed.add('blocked.example')
            self.assertTrue(observer.snapshot()['events'][0]['allowed_now'])



class DdpTests(unittest.TestCase):
    def test_parse_status_replies(self):
        from ps5 import parse_ddp
        on = parse_ddp('HTTP/1.1 200 Ok\nhost-id:AABBCCDDEEFF\nhost-type:PS5\nhost-name:PS5-001\n'
                       'device-discovery-protocol-version:00030010\nsystem-version:12200005\n')
        self.assertEqual((on['state'], on['name'], on['firmware']), ('on', 'PS5-001', '12.20'))
        rest = parse_ddp('HTTP/1.1 620 Server Standby\r\nhost-type:PS5\r\nsystem-version:09600004\r\n')
        self.assertEqual((rest['state'], rest['firmware']), ('standby', '9.60'))
        self.assertIsNone(parse_ddp('garbage'))



class PowerTests(unittest.TestCase):
    def test_paired_hosts_and_friendly_errors(self):
        from power import PowerManager, friendly

        class Engine:
            def version(self):
                return {'version': '12.4.2'}

            def request(self, method, path, body=None, timeout=None):
                return {'consoles': [{'addr': '192.0.2.5:9120', 'has_wake_credential': True},
                                     {'addr': '192.0.2.6', 'has_wake_credential': False}],
                        'paired_hosts_not_listed': ['192.0.2.7']}
        manager = PowerManager(Engine())
        self.assertEqual(manager.paired_hosts(), {'192.0.2.5', '192.0.2.7'})
        self.assertIn('遥控游玩', friendly('rp_off: Remote Play is disabled'))
        self.assertEqual(friendly(''), '操作失败')

    def test_pair_validates_pin(self):
        from power import PowerManager
        manager = PowerManager(object())
        with self.assertRaises(ValueError):
            manager.pair('192.0.2.5', pin='1234', account_id='x')
        with self.assertRaises(ValueError):
            manager.pair('192.0.2.5', pin='12345678')


if __name__ == '__main__':
    unittest.main()
