"""Policy tests: python3 -m unittest discover -s ps5-jb-server/tests -p test_policy.py."""
import importlib.util
import inspect
import pathlib
import re
import unittest
import json
import subprocess
from types import SimpleNamespace
import tempfile
import threading
from unittest import mock

POLICY_PATH = pathlib.Path(__file__).resolve().parents[1] / "server" / "policy.py"
spec = importlib.util.spec_from_file_location("managed_policy_under_test", POLICY_PATH)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)

# Firewall rules are always rendered for an injected host/LAN, never for the
# module's legacy deployment defaults (an RFC 1918 host on another subnet).
# The loopback "LAN" below only exists in the rendered text: no nft is ever run.
HOST_IP = "127.0.0.1"
LAN = "127.0.0.0/8"


def make_firewall(**kwargs):
    kwargs.setdefault("host_ip", HOST_IP)
    kwargs.setdefault("lan", LAN)
    return policy.Firewall(**kwargs)


class ValidationTests(unittest.TestCase):
    def test_defaults_and_canonical_values(self):
        # The default upstream is a legacy-deployment value owned by policy.py;
        # compare against the module constant instead of embedding it here.
        self.assertEqual(policy.validate_config({}), {
            "domains": [], "networks": [],
            "upstream_dns": policy._DEFAULT["upstream_dns"]})
        self.assertEqual(policy.validate_config({
            "domains": ["EXAMPLE.com.", "*.BÜCHER.de", "example.com"],
            "networks": ["8.8.8.8", "1.1.1.9/24", "8.8.8.8/32"],
            "upstream_dns": "8.8.8.8",
        }), {"domains": ["example.com", "*.xn--bcher-kva.de"],
              "networks": ["8.8.8.8/32", "1.1.1.0/24"],
              "upstream_dns": "8.8.8.8"})

    def test_invalid_config_is_rejected(self):
        invalid = [None, [], {"extra": 1}, {"domains": "x.com"},
                   {"domains": [1]}, {"networks": "8.8.8.8"},
                   {"networks": ["::/0"]}, {"networks": ["8.8.8.8; reboot"]},
                   {"upstream_dns": "::1"}, {"upstream_dns": "1.2.3.4/32"},
                   {"upstream_dns": 123}, {"domains": ["x.com"] * 201},
                   {"networks": ["1.1.1.1"] * 201}]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                policy.validate_config(raw)

    def test_upstream_rejects_unusable_destinations(self):
        # The upstream is the home router or a public resolver, so private
        # addresses are legitimate; loopback/broadcast/multicast/link-local and
        # reserved ranges are not usable as a forwarder.
        for address in ["127.0.0.1", "127.9.8.7", "0.0.0.0", "224.0.0.1",
                        "239.1.2.3", "255.255.255.255", "169.254.1.1", "240.0.0.1"]:
            with self.subTest(address=address), self.assertRaises(ValueError):
                policy.validate_config({"upstream_dns": address})
        for address in ["192.0.2.1", "198.51.100.53", "8.8.8.8", "192.168.1.1"]:
            self.assertEqual(policy.validate_config({"upstream_dns": address})["upstream_dns"], address)

    def test_default_upstream_is_public_and_allowed(self):
        default = policy._DEFAULT["upstream_dns"]
        self.assertFalse(default.startswith("192.168."), default)
        self.assertEqual(policy.validate_config({"upstream_dns": default})["upstream_dns"], default)

    def test_invalid_domains_are_rejected(self):
        for domain in ["", " x.com", "x.com ", "https://x.com", "x.com:53",
                       "a.*.com", "*x.com", "*", ".x.com", "x..com", "x.com..",
                       "-x.com", "x-.com", "x_com", "x.com\n", "x.com; flush ruleset",
                       "a" * 64 + ".com", "1.2.3.4", "x\x00.com", "xn--.com"]:
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                policy.validate_config({"domains": [domain]})

    def test_domain_matching_obeys_label_boundaries_and_excludes_apex(self):
        domains = ["example.com", "*.wild.example", "xn--bcher-kva.de"]
        for name in ["EXAMPLE.COM.", "a.wild.example", "a.b.wild.example", "BÜCHER.de"]:
            self.assertTrue(policy.domain_allowed(name, domains), name)
        for name in ["sub.example.com", "wild.example", "evilwild.example", "a.example.com.evil",
                     "a.wild.example\n", "https://example.com", "*.wild.example"]:
            self.assertFalse(policy.domain_allowed(name, domains), name)

    def test_local_domains_are_fixed_tuple(self):
        self.assertEqual(policy.LOCAL_DOMAINS, (
            "manuals.playstation.net", "ena.net.playstation.net", "www.msftconnecttest.com"))


class RecordingRunner:
    def __init__(self, exists=False, fail_check=False):
        self.calls = []
        self.exists = exists
        self.fail_check = fail_check

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv == ["nft", "-j", "list", "tables"]:
            tables = [{"table": {"family": "inet", "name": "ps5guard"}}] if self.exists else []
            return SimpleNamespace(stdout=json.dumps({"nftables": tables}), returncode=0)
        if self.fail_check and "-c" in argv:
            raise subprocess.CalledProcessError(1, argv, stderr="invalid rules")
        return SimpleNamespace(stdout='{"nftables": []}', returncode=0)


class FirewallTests(unittest.TestCase):
    def test_rules_limit_forwarding_and_nat_to_managed_interface_and_lan(self):
        rules = make_firewall().render({"networks": ["8.8.8.8", "1.1.1.0/24"]}, {"9.9.9.9": 60})
        self.assertIn("table inet ps5guard", rules)
        self.assertIn("type filter hook forward priority filter; policy drop;", rules)
        self.assertIn("meta nfproto ipv6 counter drop", rules)
        self.assertIn('iifname "eth0" oifname "eth0"', rules)
        self.assertIn(f"ip saddr {LAN} ip daddr {LAN}", rules)
        self.assertIn("8.8.8.8/32", rules)
        self.assertIn("1.1.1.0/24", rules)
        self.assertIn("9.9.9.9 timeout 60s", rules)
        self.assertIn("flags timeout", rules)
        self.assertIn("flags interval", rules)
        self.assertIn("auto-merge", rules)
        self.assertIn("type nat hook postrouting priority srcnat; policy accept;", rules)
        self.assertIn(f'oifname "eth0" ip saddr {LAN} ip daddr != {LAN} counter masquerade', rules)
        self.assertNotIn("hook input", rules)
        self.assertNotIn("hook output", rules)
        self.assertNotIn("flush ruleset", rules)

    def test_dns_blocks_precede_allowances_and_established_replies_recheck_authorization(self):
        rules = make_firewall().render({"networks": ["0.0.0.0/0"]}, {})
        for proto in ("tcp", "udp"):
            block = f"{proto} dport {{ 53, 853 }} counter drop"
            self.assertIn(block, rules)
            self.assertLess(rules.index(block), rules.index("ip daddr @static_v4"))
        self.assertIn("ct direction original", rules)
        self.assertIn("ct direction reply", rules)
        self.assertIn(f"ct original ip saddr {LAN}", rules)
        self.assertIn("ct original ip daddr @static_v4", rules)
        self.assertIn("ct original ip daddr @dynamic_v4", rules)
        self.assertNotIn("ct state established accept", rules)
        self.assertNotIn("ct state { established, related } accept", rules)
        self.assertIn("ct original proto-dst { 53, 853 }", rules)

    def test_lease_expiry_removal_and_ttl_bounds(self):
        rules = make_firewall().render({}, {"9.9.9.9": 0, "8.8.8.8": -1, "1.1.1.1": 10**100})
        self.assertNotIn("9.9.9.9 timeout", rules)
        self.assertNotIn("8.8.8.8 timeout", rules)
        self.assertIn("1.1.1.1 timeout 2147483s", rules)
        self.assertNotIn("1.1.1.1", make_firewall().render({}, {}))
        for value in ["127.0.0.1", "169.254.1.1", "224.0.0.1", "255.255.255.255",
                      "0.0.0.0", "192.0.2.77", "::1", "8.8.8.8; delete table inet other"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                make_firewall().render({}, {value: 60})
        for ttl in [True, "1", 1.5, None]:
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                make_firewall().render({}, {"8.8.8.8": ttl})

    def test_constructor_rejects_rule_injection(self):
        for kwargs in [{"interface": 'eth0"; flush ruleset'}, {"interface": "eth0\n"},
                       {"host_ip": "::1"}, {"lan": "::/0"}, {"host_ip": "198.51.100.1"}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                make_firewall(**kwargs)

    def test_apply_checks_identical_atomic_script_before_install(self):
        for exists in (False, True):
            runner = RecordingRunner(exists=exists)
            firewall = make_firewall(runner=runner)
            firewall.apply({}, {"8.8.8.8": 1})
            self.assertEqual([call[0] for call in runner.calls], [
                ["nft", "-j", "list", "tables"], ["nft", "-c", "-f", "-"], ["nft", "-f", "-"]])
            checked, applied = runner.calls[1][1], runner.calls[2][1]
            self.assertEqual(checked["input"], applied["input"])
            self.assertTrue(checked["check"])
            self.assertNotIn("shell", checked)
            script = checked["input"]
            self.assertEqual(script.startswith("delete table inet ps5guard\n"), exists)
            self.assertNotIn("flush ruleset", script)
            self.assertNotIn("delete table inet filter", script)

    def test_failed_check_does_not_install(self):
        runner = RecordingRunner(fail_check=True)
        with self.assertRaises(subprocess.CalledProcessError):
            make_firewall(runner=runner).apply({}, {})
        self.assertEqual(len(runner.calls), 2)

    def test_nft_subprocesses_have_bounded_runtime(self):
        runner = RecordingRunner()
        firewall = make_firewall(runner=runner)
        firewall.apply({}, {})
        firewall.counters()
        for argv, kwargs in runner.calls:
            with self.subTest(argv=argv):
                self.assertEqual(kwargs.get("timeout"), 10)

    def test_counters_return_structured_json(self):
        runner = RecordingRunner()
        self.assertEqual(make_firewall(runner=runner).counters(), {"nftables": []})
        self.assertEqual(runner.calls[0][0], ["nft", "-j", "list", "table", "inet", "ps5guard"])


class ConfigStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = pathlib.Path(self.directory.name) / "state" / "config.json"

    def test_initial_defaults_and_snapshots_are_independent(self):
        store = policy.ConfigStore(self.path)
        snapshot = store.snapshot()
        snapshot["domains"].append("mutated.example")
        self.assertEqual(store.snapshot(), policy.validate_config({}))
        self.assertIsNone(store.previous)
        self.assertIsInstance(store.lock, type(threading.RLock()))
        with store.lock:
            self.assertEqual(store.snapshot(), policy.validate_config({}))
        self.assertEqual(policy.ConfigStore(self.path).snapshot(), store.snapshot())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_successful_update_and_previous_survive_restart(self):
        old = policy.validate_config({"domains": ["old.example"]})
        store = policy.ConfigStore(self.path, initial=old)
        observed = []
        result = store.update({"domains": ["NEW.example."]}, observed.append)
        expected = policy.validate_config({"domains": ["new.example"]})
        self.assertEqual(observed, [expected])
        self.assertEqual(result, expected)
        self.assertEqual(store.snapshot(), expected)
        self.assertEqual(store.previous, old)
        previous_copy = store.previous
        previous_copy["domains"].clear()
        reloaded = policy.ConfigStore(self.path)
        self.assertEqual(reloaded.snapshot(), expected)
        self.assertEqual(reloaded.previous, old)
        document = json.loads(self.path.read_text())
        self.assertEqual(document, {"active": expected, "previous": old})

    def test_callback_mutation_does_not_corrupt_stored_config(self):
        store = policy.ConfigStore(self.path)
        def apply(value):
            value["domains"].append("mutation.example")
        store.update({"domains": ["valid.example"]}, apply)
        self.assertEqual(store.snapshot()["domains"], ["valid.example"])

    def test_failed_apply_leaves_disk_active_and_previous_unchanged(self):
        store = policy.ConfigStore(self.path)
        store.update({"domains": ["working.example"]}, lambda value: None)
        before = self.path.read_bytes()
        previous = store.previous
        def fail(value):
            raise RuntimeError("nft check failed")
        with self.assertRaisesRegex(RuntimeError, "nft check failed"):
            store.update({"domains": ["bad.example"]}, fail)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(store.snapshot()["domains"], ["working.example"])
        self.assertEqual(store.previous, previous)

    def test_invalid_update_never_calls_apply(self):
        store = policy.ConfigStore(self.path)
        calls = []
        with self.assertRaises(ValueError):
            store.update({"unknown": "data"}, calls.append)
        self.assertEqual(calls, [])

    def test_io_failure_compensates_runtime_and_preserves_old_disk(self):
        store = policy.ConfigStore(self.path, initial={"domains": ["working.example"]})
        old = store.snapshot()
        before = self.path.read_bytes()
        calls = []
        with mock.patch.object(policy.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                store.update({"domains": ["new.example"]}, calls.append)
        self.assertEqual(calls, [policy.validate_config({"domains": ["new.example"]}), old])
        self.assertEqual(store.snapshot(), old)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIsNone(store.previous)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_failure_after_replace_restores_previous_document_and_runtime(self):
        store = policy.ConfigStore(self.path)
        before = self.path.read_bytes()
        original_fsync = policy.os.fsync
        fsync_calls = 0
        def fail_directory_once(fd):
            nonlocal fsync_calls
            fsync_calls += 1
            if fsync_calls == 2:
                raise OSError("directory fsync failure")
            return original_fsync(fd)
        calls = []
        with mock.patch.object(policy.os, "fsync", side_effect=fail_directory_once):
            with self.assertRaisesRegex(OSError, "directory fsync failure"):
                store.update({"domains": ["new.example"]}, calls.append)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(calls[-1], policy.validate_config({}))
        self.assertEqual(store.snapshot(), policy.validate_config({}))

    def test_compensation_failure_is_reported_explicitly(self):
        store = policy.ConfigStore(self.path)
        calls = []
        def apply(value):
            calls.append(value)
            if len(calls) == 2:
                raise RuntimeError("cannot restore firewall")
        with mock.patch.object(policy.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(RuntimeError, "compensat"):
                store.update({"domains": ["new.example"]}, apply)
        self.assertEqual(len(calls), 2)

    def test_rollback_applies_previous_and_persists_reverse_rollback(self):
        store = policy.ConfigStore(self.path)
        with self.assertRaises(ValueError):
            store.rollback(lambda value: None)
        original = store.snapshot()
        changed = store.update({"domains": ["changed.example"]}, lambda value: None)
        calls = []
        self.assertEqual(store.rollback(calls.append), original)
        self.assertEqual(calls, [original])
        self.assertEqual(store.previous, changed)
        reloaded = policy.ConfigStore(self.path)
        self.assertEqual(reloaded.snapshot(), original)
        self.assertEqual(reloaded.previous, changed)

    def test_corrupt_persisted_config_fails_closed(self):
        self.path.parent.mkdir(parents=True)
        for data in ['{"active": {"extra": 1}, "previous": null}',
                     '{"active": {}, "previous": {"networks": ["::/0"]}}',
                     '{"active": {}, "previous": null, "extra": true}',
                     "not json"]:
            self.path.write_text(data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                policy.ConfigStore(self.path)


if __name__ == "__main__":
    unittest.main()
