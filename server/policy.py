"""Validated PS5 gateway policy and transactional local configuration."""
from __future__ import annotations

import copy
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading

LOCAL_DOMAINS = (
    "manuals.playstation.net",
    "ena.net.playstation.net",
    "www.msftconnecttest.com",
)
_DEFAULT = {"domains": [], "networks": [], "upstream_dns": "1.1.1.1"}
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.ASCII)


def _domain(value: str, *, wildcard: bool = True) -> str:
    if not isinstance(value, str) or not value or not value.isprintable() or any(c.isspace() for c in value):
        raise ValueError(f"域名不合法：{value!r}（不能包含空格或不可见字符）")
    prefix = ""
    if wildcard and value.startswith("*."):
        prefix, value = "*.", value[2:]
    if value.endswith("."):
        value = value[:-1]
    try:
        labels = [part.encode("idna").decode("ascii").lower() for part in value.split(".")]
        for label in labels:
            if not _LABEL.fullmatch(label):
                raise ValueError(f"域名不合法：{value}")
            if label.startswith("xn--"):
                if label.encode("ascii").decode("idna").encode("idna").decode("ascii") != label:
                    raise ValueError(f"国际化域名不合法：{value}")
        result = ".".join(labels)
        if len(result) > 253:
            raise ValueError(f"域名过长：{value[:40]}…")
        try:
            ipaddress.ip_address(result)
        except ValueError:
            pass
        else:
            raise ValueError(f"{result} 是 IP 地址，请填到「允许的 IPv4 / CIDR」")
        return prefix + result
    except (UnicodeError, AttributeError) as exc:
        raise ValueError(f"国际化域名不合法：{value}") from exc


def validate_config(raw) -> dict:
    """Return an independent canonical configuration, rejecting unknown fields."""
    if not isinstance(raw, dict) or set(raw) - set(_DEFAULT):
        raise ValueError("配置只能包含 domains、networks、upstream_dns 三项")
    result = {}
    for key in ("domains", "networks"):
        values = raw.get(key, [])
        if not isinstance(values, list) or len(values) > 200:
            raise ValueError(("域名" if key == "domains" else "IPv4 / CIDR") + " 白名单最多 200 项")
        canonical = []
        for value in values:
            if not isinstance(value, str):
                raise ValueError("白名单条目必须是文本")
            if key == "domains":
                item = _domain(value)
            else:
                try:
                    item = str(ipaddress.IPv4Network(value, strict=False))
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"不是合法的 IPv4 地址或 CIDR：{value}") from exc
            if item not in canonical:
                canonical.append(item)
        result[key] = canonical
    upstream = raw.get("upstream_dns", _DEFAULT["upstream_dns"])
    if not isinstance(upstream, str):
        raise ValueError("上游 DNS 必须是 IPv4 地址")
    try:
        address = ipaddress.IPv4Address(upstream)
    except ValueError as exc:
        raise ValueError("上游 DNS 必须是 IPv4 地址") from exc
    if (address.is_loopback or address.is_unspecified or address.is_multicast or
            address.is_link_local or address.is_reserved):
        raise ValueError("上游 DNS 不能是回环、未指定、组播、广播、链路本地或保留地址")
    result["upstream_dns"] = str(address)
    return result


def domain_allowed(name, domains) -> bool:
    """Match exact names or explicit subdomain rules; wildcard apex is excluded."""
    try:
        name = _domain(name, wildcard=False)
        rules = [_domain(rule) for rule in domains]
    except (ValueError, TypeError):
        return False
    return any(name.endswith("." + rule[2:]) if rule.startswith("*.") else name == rule for rule in rules)


class Firewall:
    """Own only inet ps5guard. The runner has subprocess.run's call signature."""

    def __init__(self, host_ip='127.0.0.1', lan='127.0.0.0/8', interface='eth0', runner=None):
        try:
            host = ipaddress.IPv4Address(host_ip)
            network = ipaddress.IPv4Network(lan, strict=False)
        except (ValueError, TypeError) as exc:
            raise ValueError("host_ip and lan must be IPv4") from exc
        if host not in network or host in (network.network_address, network.broadcast_address):
            raise ValueError("host_ip must be a usable address within lan")
        if not isinstance(interface, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,15}", interface):
            raise ValueError("invalid interface name")
        self.host_ip, self.lan, self.interface = str(host), str(network), interface
        self.runner = runner if runner is not None else subprocess.run
        self.lock = threading.RLock()

    # Keep milliseconds below INT_MAX even on older nft/kernel builds.
    MAX_TIMEOUT = 2147483

    @classmethod
    def _lease_elements(cls, leases):
        """Validated (sort key, nft element text) pairs; leases with TTL <= 0 are dropped."""
        if not isinstance(leases, dict):
            raise ValueError("leases must map IPv4 addresses to TTL seconds")
        dynamic = []
        for value, ttl in leases.items():
            if not isinstance(value, str) or type(ttl) is not int:
                raise ValueError("leases require string IPv4 keys and integer TTLs")
            address = cls._lease_address(value)
            if ttl > 0:
                dynamic.append((int(address), f"{address} timeout {min(ttl, cls.MAX_TIMEOUT)}s"))
        return dynamic

    @staticmethod
    def _lease_address(value):
        try:
            address = ipaddress.IPv4Address(value)
        except ValueError as exc:
            raise ValueError("lease address must be IPv4") from exc
        if not address.is_global or address.is_multicast or address.is_reserved or address.is_loopback or address.is_link_local:
            raise ValueError("lease address must be a public unicast IPv4 address")
        return address

    def render(self, config, leases: dict[str, int]) -> str:
        """Render a new table; apply prepends its deletion if it already exists."""
        config = validate_config(config)
        dynamic = self._lease_elements(leases)
        static_elements = ", ".join(config["networks"])
        dynamic_elements = ", ".join(item for _, item in sorted(dynamic))
        scope = f'iifname "{self.interface}" oifname "{self.interface}"'
        original = f"{scope} ct direction original ip saddr {self.lan}"
        reply = (f"{scope} ct direction reply ct state {{ established, related }} "
                 f"ip daddr {self.lan} ct original ip saddr {self.lan}")
        lines = [
            "table inet ps5guard {",
            "  set static_v4 {",
            "    type ipv4_addr; flags interval; auto-merge;",
        ]
        if static_elements:
            lines.append(f"    elements = {{ {static_elements} }}")
        lines += ["  }", "  set dynamic_v4 {", "    type ipv4_addr; flags timeout;"]
        if dynamic_elements:
            lines.append(f"    elements = {{ {dynamic_elements} }}")
        lines += [
            "  }",
            "  chain forward {",
            "    type filter hook forward priority filter; policy drop;",
            "    meta nfproto ipv6 counter drop",
            "    ct state invalid counter drop",
        ]
        # These blocks precede all address allowances, including a configured /0.
        for protocol in ("tcp", "udp"):
            lines.append(f"    {original} ip daddr != {self.lan} {protocol} dport {{ 53, 853 }} counter drop")
        lines += [
            f"    {reply} ct original ip daddr != {self.lan} meta l4proto {{ tcp, udp }} ct original proto-dst {{ 53, 853 }} counter drop",
            f"    {scope} ip saddr {self.lan} ip daddr {self.lan} counter accept",
        ]
        for destination in ("static_v4", "dynamic_v4"):
            lines.append(f"    {original} ip daddr @{destination} counter accept")
            lines.append(f"    {reply} ct original ip daddr @{destination} counter accept")
        lines += [
            "    counter drop",
            "  }",
            "  chain postrouting {",
            "    type nat hook postrouting priority srcnat; policy accept;",
            f'    oifname "{self.interface}" ip saddr {self.lan} ip daddr != {self.lan} counter masquerade',
            "  }",
            "}",
        ]
        return "\n".join(lines) + "\n"

    def _run(self, argv, **kwargs):
        return self.runner(argv, capture_output=True, text=True, check=True, timeout=10, **kwargs)

    def apply(self, config, leases: dict[str, int]):
        """Validate and atomically install a full table replacement."""
        rules = self.render(config, leases)
        with self.lock:
            tables = json.loads(self._run(["nft", "-j", "list", "tables"]).stdout)
            exists = any(row.get("table", {}).get("family") == "inet" and
                         row.get("table", {}).get("name") == "ps5guard"
                         for row in tables["nftables"])
            script = ("delete table inet ps5guard\n" if exists else "") + rules
            self._run(["nft", "-c", "-f", "-"], input=script)
            self._run(["nft", "-f", "-"], input=script)

    def add_leases(self, leases: dict[str, int], refresh=()):
        """Add dynamic addresses with one atomic `nft -f -`, leaving rules and counters intact.

        refresh names addresses that should still be in the kernel set and need a
        longer timeout. nft 1.0.6 (Debian 12) cannot change an element's timeout,
        so they are deleted and re-added inside the same transaction: there is no
        window without authorization. Each refreshed element is first added with
        a one-second timeout so the delete cannot fail if the kernel already
        expired it. Errors (table missing, nft failure) propagate to the caller,
        which falls back to apply().
        """
        elements = self._lease_elements(leases)
        if not elements:
            return
        # Only addresses granted by this call can be refreshed; leases keys are validated.
        granted = {value for value, ttl in leases.items() if ttl > 0}
        stale = sorted(set(refresh) & granted, key=lambda value: int(ipaddress.IPv4Address(value)))
        target = "element inet ps5guard dynamic_v4"
        lines = []
        if stale:
            lines.append(f"add {target} {{ " + ", ".join(f"{ip} timeout 1s" for ip in stale) + " }")
            lines.append(f"delete {target} {{ " + ", ".join(stale) + " }")
        lines.append(f"add {target} {{ " + ", ".join(item for _, item in sorted(elements)) + " }")
        with self.lock:
            self._run(["nft", "-f", "-"], input="\n".join(lines) + "\n")

    def counters(self):
        """Return nft's JSON table including rule counters; propagate query failures."""
        with self.lock:
            result = json.loads(self._run(["nft", "-j", "list", "table", "inet", "ps5guard"]).stdout)
            if not isinstance(result, (dict, list)):
                raise ValueError("nft counters response must be a JSON object or array")
            return result


class ConfigStore:
    """One atomic JSON envelope preserves active and previous configurations.

    Callbacks must apply atomically or restore their own state before raising.
    The public RLock also lets DNS and admin coordinate a larger transaction.
    """

    def __init__(self, path, initial=None):
        self.path = Path(path).resolve()
        self.lock = threading.RLock()
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._active = validate_config({} if initial is None else initial)
            self._previous = None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._persist(self._active, self._previous)
        else:
            if not isinstance(document, dict) or set(document) != {"active", "previous"}:
                raise ValueError("stored configuration requires active and previous")
            self._active = validate_config(document["active"])
            self._previous = (None if document["previous"] is None else
                              validate_config(document["previous"]))

    def snapshot(self) -> dict:
        with self.lock:
            return copy.deepcopy(self._active)

    @property
    def previous(self):
        with self.lock:
            return copy.deepcopy(self._previous)

    def _atomic_write(self, data: bytes):
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            # A directory fsync makes the rename durable on the Debian target.
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            # mkstemp selected this path in the same validated parent directory.
            if Path(temporary).exists():
                Path(temporary).unlink()

    def _persist(self, active, previous):
        document = {"active": active, "previous": previous}
        self._atomic_write((json.dumps(document, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("utf-8"))

    def update(self, raw, apply_callback):
        """Apply then persist; compensate runtime and disk if persistence fails."""
        new_config = validate_config(raw)
        with self.lock:
            old_config = copy.deepcopy(self._active)
            old_document = self.path.read_bytes()
            apply_callback(copy.deepcopy(new_config))
            try:
                self._persist(new_config, old_config)
            except Exception as original_error:
                recovery_errors = []
                try:
                    apply_callback(copy.deepcopy(old_config))
                except Exception as exc:
                    recovery_errors.append(f"runtime: {exc}")
                try:
                    if self.path.read_bytes() != old_document:
                        self._atomic_write(old_document)
                except Exception as exc:
                    recovery_errors.append(f"disk: {exc}")
                if recovery_errors:
                    raise RuntimeError("configuration persistence failed; compensation failed: " +
                                       "; ".join(recovery_errors)) from original_error
                raise
            self._previous = old_config
            self._active = new_config
            return copy.deepcopy(self._active)

    def rollback(self, apply_callback):
        """Restore previous via the same transaction, preserving reverse rollback."""
        with self.lock:
            if self._previous is None:
                raise ValueError("没有可回退的上一版配置")
            return self.update(copy.deepcopy(self._previous), apply_callback)
