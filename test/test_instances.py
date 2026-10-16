"""Tests for the Instances (multi-instance management) feature.

Covers the Phase 1 backend: config flag/constants, registry CRUD + hints,
PortAllocator, token-mint helper (parse/ttl/command-build/mocked ssh, no token
in logs), the gateway run-marker, injection-safe validation and SshTunnelManager
(mocked subprocess via an injected tunnel factory + mint). The API handler,
connect-state, diagnosis, self-heal, SSM, forwarder-reclaim and chat-proxy tests,
the generic token-mint tests and the validators' newline ratchet live in sibling
``test_instances_*.py`` files.

Async paths are driven through ``asyncio.run`` from sync test functions so the
suite needs no asyncio pytest plugin.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest
from instances_helpers import _FakeTunnel, _patch_port_probe

# ── config flag + constants ────────────────────────────────────────────────


class TestConfig:
    def test_defaults_off_and_tunables(self):
        from kiro_crew.config.loader import InstancesConfig
        from kiro_crew.instances.constants import (
            DEFAULT_CONNECT_TIMEOUT_SECS,
            DEFAULT_MINT_TIMEOUT_SECS,
            DEFAULT_TUNNEL_BASE_PORT,
            DEFAULT_WARM_SET_CAP,
        )

        c = InstancesConfig()
        assert c.enabled is False
        # 0 == automatic: the cap follows the REGISTERED crew count (resolved per
        # request by resolve_warm_set_cap), so no configured crew is ever evicted.
        assert c.warm_set_cap == DEFAULT_WARM_SET_CAP == 0
        assert c.tunnel_base_port == DEFAULT_TUNNEL_BASE_PORT == 7778
        assert DEFAULT_CONNECT_TIMEOUT_SECS == 15.0
        assert c.connect_timeout_secs is None
        assert DEFAULT_MINT_TIMEOUT_SECS == 30.0
        assert c.mint_timeout_secs is None

    def test_clamps_out_of_range(self):
        from kiro_crew.config.loader import InstancesConfig

        # 0 is a legal value (automatic), so only a negative cap is clamped, and
        # it falls back to automatic rather than to the tightest possible cap.
        c = InstancesConfig(warm_set_cap=-3, tunnel_base_port=99999)
        assert c.warm_set_cap == 0
        assert c.tunnel_base_port == 7778

    def test_zero_warm_set_cap_is_kept_as_automatic(self):
        from kiro_crew.config.loader import InstancesConfig

        assert InstancesConfig(warm_set_cap=0).warm_set_cap == 0

    def test_roundtrip_and_schema(self):
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.config.schema import SCHEMA_REGISTRY

        d = KiroCrewConfig().to_dict()
        assert d["instances"] == {
            "enabled": False,
            "warm_set_cap": 0,
            "tunnel_base_port": 7778,
            "ssh_compression": True,
            "connect_timeout_secs": None,
            "mint_timeout_secs": None,
            "max_recovery_attempts": 8,
            "recover_backoff_max_secs": 30.0,
            "probe_failure_threshold": 3,
        }
        paths = {e.path for e in SCHEMA_REGISTRY}
        for p in (
            "instances",
            "instances.enabled",
            "instances.warm_set_cap",
            "instances.tunnel_base_port",
            "instances.ssh_compression",
            "instances.connect_timeout_secs",
            "instances.mint_timeout_secs",
            "instances.max_recovery_attempts",
            "instances.recover_backoff_max_secs",
            "instances.probe_failure_threshold",
        ):
            assert p in paths
        timeout_entry = next(
            e for e in SCHEMA_REGISTRY if e.path == "instances.connect_timeout_secs"
        )
        assert timeout_entry.type == "number"
        assert timeout_entry.nullable is True
        assert timeout_entry.default_value is None

    def test_recovery_knobs_parse_from_config_file(self, tmp_path, monkeypatch):
        import json

        from kiro_crew.config.loader import KiroCrewConfig

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "instances": {
                        "max_recovery_attempts": 12,
                        "recover_backoff_max_secs": 45.0,
                        "probe_failure_threshold": 5,
                    }
                }
            )
        )
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_file)
        cfg = KiroCrewConfig.load()
        assert cfg.instances.max_recovery_attempts == 12
        assert cfg.instances.recover_backoff_max_secs == 45.0
        assert cfg.instances.probe_failure_threshold == 5

    def test_recovery_knob_clamps(self):
        from kiro_crew.config.loader import InstancesConfig

        c = InstancesConfig(
            max_recovery_attempts=0, recover_backoff_max_secs=0, probe_failure_threshold=0
        )
        assert c.max_recovery_attempts == 8
        assert c.recover_backoff_max_secs == 30.0
        assert c.probe_failure_threshold == 3

        # Upper bound: a pathological max_recovery_attempts is clamped down to the
        # ceiling (warned, not silently dropped) so it can't spin a near-infinite
        # self-heal loop. The boundary value itself is left untouched.
        from kiro_crew.instances.constants import MAX_RECOVERY_ATTEMPTS_CEILING

        assert MAX_RECOVERY_ATTEMPTS_CEILING == 100
        assert (
            InstancesConfig(max_recovery_attempts=10_000).max_recovery_attempts
            == MAX_RECOVERY_ATTEMPTS_CEILING
        )
        assert (
            InstancesConfig(
                max_recovery_attempts=MAX_RECOVERY_ATTEMPTS_CEILING
            ).max_recovery_attempts
            == MAX_RECOVERY_ATTEMPTS_CEILING
        )

        # recover_backoff_max_secs has the same two-sided guard: a pathological
        # pacing is clamped down to the ceiling so the attempt cap can't be stretched
        # into a multi-day wall-clock window; the boundary value is left untouched.
        from kiro_crew.instances.constants import RECOVER_BACKOFF_MAX_CEILING_SECS

        assert RECOVER_BACKOFF_MAX_CEILING_SECS == 300.0
        assert (
            InstancesConfig(recover_backoff_max_secs=86_400.0).recover_backoff_max_secs
            == RECOVER_BACKOFF_MAX_CEILING_SECS
        )
        assert (
            InstancesConfig(
                recover_backoff_max_secs=RECOVER_BACKOFF_MAX_CEILING_SECS
            ).recover_backoff_max_secs
            == RECOVER_BACKOFF_MAX_CEILING_SECS
        )

    def test_connect_timeout_default_and_clamps(self):
        from kiro_crew.config.loader import InstancesConfig
        from kiro_crew.instances.constants import (
            CONNECT_TIMEOUT_CEILING_SECS,
            DEFAULT_CONNECT_TIMEOUT_SECS,
        )

        # Unset remains distinguishable from an explicit value equal to the SSH
        # default, so the manager can select the transport-specific default.
        c = InstancesConfig()
        assert DEFAULT_CONNECT_TIMEOUT_SECS == 15.0
        assert c.connect_timeout_secs is None

        c = InstancesConfig(connect_timeout_secs=DEFAULT_CONNECT_TIMEOUT_SECS)
        assert c.connect_timeout_secs == DEFAULT_CONNECT_TIMEOUT_SECS

        # Explicit override is honored.
        c = InstancesConfig(connect_timeout_secs=45.0)
        assert c.connect_timeout_secs == 45.0

        # Below 1 falls back to the transport-specific defaults.
        c = InstancesConfig(connect_timeout_secs=0.5)
        assert c.connect_timeout_secs is None

        c = InstancesConfig(connect_timeout_secs=-10.0)
        assert c.connect_timeout_secs is None

        # Above the ceiling is clamped.
        assert CONNECT_TIMEOUT_CEILING_SECS == 120.0
        c = InstancesConfig(connect_timeout_secs=999.0)
        assert c.connect_timeout_secs == CONNECT_TIMEOUT_CEILING_SECS

        # Boundary value itself is left untouched.
        c = InstancesConfig(connect_timeout_secs=CONNECT_TIMEOUT_CEILING_SECS)
        assert c.connect_timeout_secs == CONNECT_TIMEOUT_CEILING_SECS

    def test_connect_timeout_parses_from_config_file(self, tmp_path, monkeypatch):
        import json

        from kiro_crew.config.loader import KiroCrewConfig

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"instances": {"connect_timeout_secs": 45.0}}))
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_file)
        cfg = KiroCrewConfig.load()
        assert cfg.instances.connect_timeout_secs == 45.0

        cfg_file.write_text(json.dumps({"instances": {}}))
        cfg = KiroCrewConfig.load()
        assert cfg.instances.connect_timeout_secs is None

        cfg_file.write_text(json.dumps({"instances": {"connect_timeout_secs": None}}))
        cfg = KiroCrewConfig.load()
        assert cfg.instances.connect_timeout_secs is None

        cfg_file.write_text(json.dumps({"instances": {"connect_timeout_secs": 15.0}}))
        cfg = KiroCrewConfig.load()
        assert cfg.instances.connect_timeout_secs == 15.0

    def test_mint_timeout_default_and_clamps(self):
        from kiro_crew.config.loader import InstancesConfig
        from kiro_crew.instances.constants import (
            DEFAULT_MINT_TIMEOUT_SECS,
            MINT_TIMEOUT_CEILING_SECS,
            MINT_TIMEOUT_FLOOR_SECS,
        )

        # Unset by default; the per-transport defaults live in constants.
        c = InstancesConfig()
        assert c.mint_timeout_secs is None
        assert DEFAULT_MINT_TIMEOUT_SECS == 30.0

        # Explicit override is honored — including the SSH-default value.
        c = InstancesConfig(mint_timeout_secs=60.0)
        assert c.mint_timeout_secs == 60.0
        c = InstancesConfig(mint_timeout_secs=DEFAULT_MINT_TIMEOUT_SECS)
        assert c.mint_timeout_secs == DEFAULT_MINT_TIMEOUT_SECS

        # Below the floor falls back to unset (transport defaults).
        assert MINT_TIMEOUT_FLOOR_SECS == 10.0
        c = InstancesConfig(mint_timeout_secs=5.0)
        assert c.mint_timeout_secs is None

        c = InstancesConfig(mint_timeout_secs=-30.0)
        assert c.mint_timeout_secs is None

        # The floor value itself is left untouched.
        c = InstancesConfig(mint_timeout_secs=MINT_TIMEOUT_FLOOR_SECS)
        assert c.mint_timeout_secs == MINT_TIMEOUT_FLOOR_SECS

        # Above the ceiling is clamped.
        assert MINT_TIMEOUT_CEILING_SECS == 120.0
        c = InstancesConfig(mint_timeout_secs=999.0)
        assert c.mint_timeout_secs == MINT_TIMEOUT_CEILING_SECS

        # Boundary value itself is left untouched.
        c = InstancesConfig(mint_timeout_secs=MINT_TIMEOUT_CEILING_SECS)
        assert c.mint_timeout_secs == MINT_TIMEOUT_CEILING_SECS

    def test_mint_timeout_parses_from_config_file(self, tmp_path, monkeypatch):
        import json

        from kiro_crew.config.loader import KiroCrewConfig

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"instances": {"mint_timeout_secs": 60.0}}))
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_file)
        cfg = KiroCrewConfig.load()
        assert cfg.instances.mint_timeout_secs == 60.0

        cfg_file.write_text(json.dumps({"instances": {}}))
        cfg = KiroCrewConfig.load()
        assert cfg.instances.mint_timeout_secs is None

        cfg_file.write_text(json.dumps({"instances": {"mint_timeout_secs": None}}))
        cfg = KiroCrewConfig.load()
        assert cfg.instances.mint_timeout_secs is None

        cfg_file.write_text(json.dumps({"instances": {"mint_timeout_secs": 30.0}}))
        cfg = KiroCrewConfig.load()
        assert cfg.instances.mint_timeout_secs == 30.0


# ── PortAllocator ───────────────────────────────────────────────────────────


class TestPortAllocator:
    def test_rejects_bad_base(self):
        from kiro_crew.instances.port_allocator import PortAllocator

        with pytest.raises(ValueError):
            PortAllocator(base_port=0)

    def test_skips_bound_and_excluded(self):
        from kiro_crew.instances.port_allocator import PortAllocator

        # Bind an OS-assigned free port so the test never collides with a port
        # something else already holds (the old hard-coded base was flaky).
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        base = s.getsockname()[1]
        try:
            pa = PortAllocator(base_port=base)
            assert pa.allocate() > base  # base is bound -> skipped
            assert pa.allocate(exclude={base, base + 1, base + 2}) >= base + 3
        finally:
            s.close()

    def test_preferred_port_is_returned_when_free(self):
        """A free, in-range ``preferred`` is handed back unchanged.

        This is the origin-stability contract for a crew's embedded pane: the
        loopback port is the browser origin of that pane, so returning the same
        recorded port across reconnects keeps its origin-keyed localStorage
        settings alive. ``preferred`` well above ``base`` must win over the
        first-free port.
        """
        from kiro_crew.instances.port_allocator import PortAllocator

        # Two OS-assigned free ports; the higher one is the "recorded" one.
        socks = []
        for _ in range(2):
            sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sk.bind(("127.0.0.1", 0))
            socks.append(sk)
        low, high = sorted(s.getsockname()[1] for s in socks)
        for s in socks:
            s.close()
        base = min(low, high)
        pa = PortAllocator(base_port=base)
        # Even though a lower free port exists, the preferred one is returned.
        assert pa.allocate(preferred=high) == high

    def test_preferred_port_falls_back_when_excluded_or_out_of_range(self):
        """A ``preferred`` that is excluded, below base, or occupied yields.

        The preference is honoured, never enforced: a port another instance now
        holds (``exclude``) or one outside the allocator's range must not be
        handed back — allocation falls through to first-free instead.
        """
        from kiro_crew.instances.port_allocator import PortAllocator

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        base = s.getsockname()[1]
        s.close()
        pa = PortAllocator(base_port=base)
        # Preferred is reserved by another instance -> not returned.
        got = pa.allocate(exclude={base}, preferred=base)
        assert got != base and got > base
        # Preferred below base is ignored, never handed back out of range.
        assert pa.allocate(preferred=base - 1) >= base

    def test_is_port_free_detects_live_listener_even_with_reuseaddr(self):
        """A genuinely LISTENing port is still reported in-use.

        Regression guard for the disconnect->reconnect fix: on POSIX
        `_is_port_free` sets SO_REUSEADDR (so a just-freed port lingering in
        TIME_WAIT is not a false positive, matching ssh's own `-L` listener
        bind). This must NOT relax detection of a real, live listener — a true
        two-instance port collision still has to be caught. SO_REUSEADDR
        exempts TIME_WAIT only, never an active LISTEN, so the probe must still
        fail to bind against a LISTENing socket that itself set SO_REUSEADDR.

        On Windows the same occupier is the port-squat shape: two SO_REUSEADDR
        sockets can both listen there, so the probe uses SO_EXCLUSIVEADDRUSE
        and this test runs unchanged on the Windows matrix as the live proof.
        """
        from kiro_crew.instances.port_allocator import _is_port_free

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # The occupier sets SO_REUSEADDR too (as ssh does); the probe must still
        # be denied while this socket is actively listening.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        try:
            assert _is_port_free(port) is False
        finally:
            s.close()

    class _ProbeSocket:
        """Socket double that records the options the probe sets."""

        def __init__(self, calls, bind_error=None):
            self._calls = calls
            self._bind_error = bind_error

        def setsockopt(self, level, name, value):
            self._calls.append(("setsockopt", level, name, value))

        def bind(self, addr):
            self._calls.append(("bind", addr))
            if self._bind_error is not None:
                raise self._bind_error

        def close(self):
            self._calls.append(("close",))

    def _fake_socket_module(self, calls, *, exclusive=True, bind_error=None):
        import types

        ns = types.SimpleNamespace(
            AF_INET=socket.AF_INET,
            AF_INET6=socket.AF_INET6,
            SOCK_STREAM=socket.SOCK_STREAM,
            SOL_SOCKET=socket.SOL_SOCKET,
            SO_REUSEADDR=socket.SO_REUSEADDR,
            socket=lambda family, kind: self._ProbeSocket(calls, bind_error),
        )
        if exclusive:
            # Winsock's value (~SO_REUSEADDR); any int distinct from SO_REUSEADDR works.
            ns.SO_EXCLUSIVEADDRUSE = -5
        return ns

    def test_windows_probe_sets_exclusiveaddruse_and_never_reuseaddr(self, monkeypatch):
        """Windows probe: SO_EXCLUSIVEADDRUSE only.

        With SO_REUSEADDR, a Windows probe binds next to a live listener that set
        it too (OpenSSH's -L listener does), reads the port as free, and the
        forward becomes a second listener that never gets the traffic.
        """
        import kiro_crew.instances.port_allocator as pa
        from kiro_crew import platform_compat as pc

        calls: list = []
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pa, "socket", self._fake_socket_module(calls))

        assert pa._is_addr_free(7778, "127.0.0.1") is True

        opts = [c for c in calls if c[0] == "setsockopt"]
        assert opts == [("setsockopt", socket.SOL_SOCKET, -5, 1)]
        assert calls[-2:] == [("bind", ("127.0.0.1", 7778)), ("close",)]

    def test_windows_probe_reads_exclusive_bind_refusal_as_in_use(self, monkeypatch):
        import kiro_crew.instances.port_allocator as pa
        from kiro_crew import platform_compat as pc

        calls: list = []
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(
            pa,
            "socket",
            self._fake_socket_module(calls, bind_error=OSError(errno.EADDRINUSE, "in use")),
        )

        assert pa._is_addr_free(7778, "127.0.0.1") is False
        assert pa._is_port_free(7778) is False

    def test_windows_probe_without_the_constant_sets_no_reuse_option(self, monkeypatch):
        """No SO_EXCLUSIVEADDRUSE in this build: plain bind, never SO_REUSEADDR."""
        import kiro_crew.instances.port_allocator as pa
        from kiro_crew import platform_compat as pc

        calls: list = []
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pa, "socket", self._fake_socket_module(calls, exclusive=False))

        assert pa._is_addr_free(7778, "::1") is True
        assert [c for c in calls if c[0] == "setsockopt"] == []

    def test_posix_probe_still_sets_reuseaddr_only(self, monkeypatch):
        """POSIX probe unchanged: SO_REUSEADDR, so TIME_WAIT is not a false positive."""
        import kiro_crew.instances.port_allocator as pa
        from kiro_crew import platform_compat as pc

        calls: list = []
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pa, "socket", self._fake_socket_module(calls))

        assert pa._is_addr_free(7778, "127.0.0.1") is True
        opts = [c for c in calls if c[0] == "setsockopt"]
        assert opts == [("setsockopt", socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)]

    def test_is_port_free_true_for_unbound_port(self):
        from kiro_crew.instances.port_allocator import _is_port_free

        # Grab an OS-assigned port, then release it — it is now free to bind.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        assert _is_port_free(port) is True

    @pytest.mark.skipif(not socket.has_ipv6, reason="host has no IPv6 support")
    def test_is_port_free_rejects_port_held_on_ipv6_loopback_only(self):
        """A port free on 127.0.0.1 but LISTENing on ::1 counts as in use.

        The forward binds one address, so leaving the other loopback family to a
        foreign listener makes `localhost:<port>` resolve to whichever socket the
        client's resolver and the platform's bind precedence pick. The probe must
        therefore clear every loopback address, not just IPv4.
        """
        from kiro_crew.instances.port_allocator import _is_addr_free, _is_port_free

        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        try:
            s.bind(("::1", 0))
        except OSError:  # ::1 not configured on this host
            s.close()
            pytest.skip("::1 is not assignable here")
        s.listen(1)
        port = s.getsockname()[1]
        try:
            # Quick check: the IPv4 half really is free, so only the ::1 half can be
            # what makes the aggregate probe say "in use".
            assert _is_addr_free(port, "127.0.0.1") is True
            assert _is_port_free(port) is False
        finally:
            s.close()

    def test_is_port_free_treats_unassignable_address_as_free(self, monkeypatch):
        """EADDRNOTAVAIL means the address does not exist, not that it is taken.

        Without this, a host with IPv6 compiled in but ::1 not configured would
        see every candidate port as occupied and connect would never allocate one.
        """
        import kiro_crew.instances.port_allocator as pa

        real_socket = socket.socket

        def fake_socket(family, type_):
            sock = real_socket(family, type_)
            if family == socket.AF_INET6:
                sock.close()

                class _Unassignable:
                    def setsockopt(self, *a):
                        pass

                    def bind(self, *a):
                        raise OSError(errno.EADDRNOTAVAIL, "Cannot assign address")

                    def close(self):
                        pass

                return _Unassignable()
            return sock

        monkeypatch.setattr(pa.socket, "socket", fake_socket)

        s = real_socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        assert pa._is_port_free(port) is True

    @pytest.mark.parametrize(
        "creation_errno",
        [errno.EMFILE, errno.ENFILE, errno.ENOBUFS],
        ids=["EMFILE", "ENFILE", "ENOBUFS"],
    )
    def test_is_port_free_propagates_when_the_probe_cannot_run(self, monkeypatch, creation_errno):
        """A probe that could not RUN answers neither "free" nor "in use".

        Reading it as free would hand out a port a listener on the unprobed
        family may hold; reading it as in use would send the allocator through
        every candidate and fail with a port-exhaustion message naming the wrong
        cause. So it propagates, which is also what the pre-dual-stack code did
        (a creation error was never caught).
        """
        import kiro_crew.instances.port_allocator as pa

        real_socket = socket.socket

        def fake_socket(family, type_):
            if family == socket.AF_INET6:
                raise OSError(creation_errno, "probe could not be run")
            return real_socket(family, type_)

        monkeypatch.setattr(pa.socket, "socket", fake_socket)

        s = real_socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with pytest.raises(OSError) as excinfo:
            pa._is_port_free(port)
        assert excinfo.value.errno == creation_errno

    @pytest.mark.parametrize(
        "creation_errno",
        [errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT],
        ids=["EAFNOSUPPORT", "EPROTONOSUPPORT"],
    )
    def test_is_port_free_treats_absent_family_as_free(self, monkeypatch, creation_errno):
        """A family the kernel will not create cannot be holding the port.

        Both errnos are reported by IPv6-less kernels depending on the stack.
        Failing closed here would refuse every candidate port on such a host and
        `connect` would never allocate one.
        """
        import kiro_crew.instances.port_allocator as pa

        real_socket = socket.socket

        def fake_socket(family, type_):
            if family == socket.AF_INET6:
                raise OSError(creation_errno, "no such protocol family")
            return real_socket(family, type_)

        monkeypatch.setattr(pa.socket, "socket", fake_socket)

        s = real_socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        assert pa._is_port_free(port) is True

    def test_allocate_skips_port_held_on_one_loopback_family(self, monkeypatch):
        """The allocator climbs past a port the aggregate probe rejects."""
        import kiro_crew.instances.port_allocator as pa

        base = 41000
        monkeypatch.setattr(
            pa, "_is_addr_free", lambda port, host: not (port == base and host == "::1")
        )
        assert pa.PortAllocator(base_port=base).allocate() == base + 1


# ── token mint ──────────────────────────────────────────────────────────────


class TestTokenMint:
    def test_parse(self):
        from kiro_crew.instances.token_mint import parse_token_from_stdout

        assert parse_token_from_stdout("http://localhost:7777?token=eyJa.b\n") == "eyJa.b"
        assert parse_token_from_stdout("https://h/?x=1&token=TOK&y=2") == "TOK"
        assert parse_token_from_stdout("nothing here") == ""

    @pytest.mark.parametrize("bad", ["abc", "20", "0h", "-1h", "20s", "99999h"])
    def test_ttl_rejects(self, bad):
        from kiro_crew.instances.token_mint import TokenMintError, _validate_ttl

        with pytest.raises(TokenMintError):
            _validate_ttl(bad)

    def test_command_builders(self):
        from kiro_crew.instances.token_mint import build_remote_token_command

        # empty remote_bin -> candidate-ladder path (build_remote_command -> build_candidate_command)
        assert 'exec "$b" token --ttl 20h;' in build_remote_token_command("", ttl="20h")
        custom = build_remote_token_command("~/bin/kirocrew", ttl="30m")
        assert '"$HOME/bin/kirocrew" token --ttl 30m' in custom
        default = build_remote_token_command("", ttl=None)
        assert 'exec "$b" token;' in default and "--ttl" not in default
        # port is threaded through so the remote mint targets the right gateway
        # (not the default 7777) — essential for instances on a custom port.
        with_port = build_remote_token_command("~/bin/kirocrew", ttl="20h", port=7879)
        assert '"$HOME/bin/kirocrew" token --ttl 20h --port 7879' in with_port
        # invalid port is rejected (kept out of the shell command unvalidated)
        from kiro_crew.instances.token_mint import TokenMintError

        with pytest.raises(TokenMintError):
            build_remote_token_command("", ttl="20h", port=99999)

    def test_token_command_prefers_run_marker_for_port(self):
        from kiro_crew.config.paths import CONFIG_DIR_NAME, LEGACY_CONFIG_DIR_NAME
        from kiro_crew.instances.token_mint import (
            build_candidate_command,
            build_remote_token_command,
        )

        # empty remote_bin + port -> run-marker clause runs BEFORE the candidate
        # ladder, keyed by the same port, and execs the recorded launcher. The
        # marker is probed under each candidate data home (KIROCREW_HOME override,
        # the current default, then the legacy home) so a migrated remote whose
        # non-interactive SSH shell doesn't export KIROCREW_HOME still hits the
        # marker written under the new default home. The default/legacy home
        # segments are asserted via the SHARED config.paths constants (not
        # re-hardcoded literals) so that re-hardcoding — the read/write desync
        # this fix closes — fails this test loudly at PR time.
        default_marker = f'"$HOME/{CONFIG_DIR_NAME}/run/gateway-7879.bin"'
        legacy_marker = f'"$HOME/{LEGACY_CONFIG_DIR_NAME}/run/gateway-7879.bin"'
        cmd = build_remote_token_command("", ttl="20h", port=7879)
        assert '"${KIROCREW_HOME:+$KIROCREW_HOME/run/gateway-7879.bin}"' in cmd
        assert default_marker in cmd
        assert legacy_marker in cmd
        # new default home is probed before the legacy home
        assert cmd.index(default_marker) < cmd.index(legacy_marker)
        assert 'exec "$__kb" token --ttl 20h --port 7879;' in cmd
        assert cmd.index("for __mk in ") < cmd.index("for b in ")  # marker tried first
        # it still falls through to the candidate ladder (older remotes/no marker)
        assert 'exec "$b" token --ttl 20h --port 7879;' in cmd

        # no port -> no marker clause (can't key it); pure candidate ladder.
        # NOTE: guard on the sentinels the generator actually emits — the marker
        # prelude is a "for __mk in ...done" loop over "…/gateway-<port>.bin"
        # paths — NOT the retired "__mk=" token (which would make these vacuous).
        no_port = build_remote_token_command("", ttl="20h")
        assert "for __mk in" not in no_port and "gateway-" not in no_port

        # explicit custom remote_bin is never overridden by the marker.
        custom = build_remote_token_command("~/bin/kirocrew", ttl="20h", port=7879)
        assert "for __mk in" not in custom and "gateway-" not in custom
        assert '"$HOME/bin/kirocrew" token --ttl 20h --port 7879' in custom

        # generic candidate builder: marker only when a port is supplied.
        assert "for __mk in" not in build_candidate_command("restart")
        assert "gateway-" not in build_candidate_command("restart")
        assert "gateway-7880.bin" in build_candidate_command("token", marker_port=7880)

        # the sibling remote-exec path (restart, via run_remote_kirocrew) also
        # prefers the marker when the caller passes the remote port.
        from kiro_crew.instances.token_mint import build_remote_command

        restart = build_remote_command("", "restart", marker_port=7781)
        assert "gateway-7781.bin" in restart and 'exec "$__kb" restart;' in restart
        # ...unless an explicit custom remote_bin is set (never overridden).
        pinned_restart = build_remote_command("~/bin/kirocrew", "restart", marker_port=7781)
        assert "for __mk in" not in pinned_restart and "gateway-" not in pinned_restart

    def test_ssh_argv_shape(self):
        from kiro_crew.instances.token_mint import _build_ssh_argv

        argv = _build_ssh_argv("cd-1", "echo hi")
        assert argv[0] == "ssh" and argv[-2] == "cd-1"
        assert "BatchMode=yes" in argv and "AddressFamily=inet" in argv
        # -n redirects ssh's stdin from the null device. Without it the child
        # inherits the gateway's stdin, and through a ProxyCommand that channel
        # stays open after the remote command exits, so ssh waits for an EOF a
        # console-less gateway never sends and the probe reports a reachable
        # host as unreachable. It must precede the host, or ssh reads it as
        # part of the remote command.
        assert "-n" in argv
        assert argv.index("-n") < argv.index("cd-1")
        # Default fail-fast connect bound is preserved for callers that
        # don't thread a budget (e.g. run_remote_kirocrew).
        assert "ConnectTimeout=10" in argv
        # The mint threads its configurable budget into ConnectTimeout so a
        # slow ProxyCommand/banner exchange isn't killed at the 10s default
        # before mint_timeout_secs can matter (OpenSSH >= 8.6 counts the
        # banner/KEX exchange against ConnectTimeout).
        argv = _build_ssh_argv("cd-1", "echo hi", connect_timeout_secs=60.0)
        assert "ConnectTimeout=60" in argv and "ConnectTimeout=10" not in argv
        # Sub-second values clamp up to ssh's integer floor of 1.
        argv = _build_ssh_argv("cd-1", "echo hi", connect_timeout_secs=0.2)
        assert "ConnectTimeout=1" in argv

    def test_mint_success_and_no_token_in_logs(self, monkeypatch, caplog):
        from kiro_crew.instances import token_mint as tm

        class FakeProc:
            returncode = 0

            async def communicate(self):
                return b"http://localhost:7777?token=SECRETJWT\n", b""

        async def fake_exec(*a, **k):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        with caplog.at_level(logging.INFO):
            tok = asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        assert tok == "SECRETJWT"
        assert "SECRETJWT" not in caplog.text

    def test_mint_nonzero_exit_raises(self, monkeypatch):
        from kiro_crew.instances import token_mint as tm

        class FakeProc:
            returncode = 127

            async def communicate(self):
                return b"", b"kirocrew binary not found"

        async def fake_exec(*a, **k):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        with pytest.raises(tm.TokenMintError):
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))

    # ── diagnosability: an older remote prints its reason to STDOUT ──────────

    def _fake_proc(self, monkeypatch, rc: int, out: bytes, err: bytes) -> None:
        class FakeProc:
            returncode = rc

            async def communicate(self):
                return out, err

        async def fake_exec(*a, **k):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    def test_nonzero_exit_surfaces_stdout_tail_when_stderr_empty(self, monkeypatch):
        """The real reason travels with the error instead of '<no stderr>'.

        Pre-fix ``kirocrew token`` printed its failure prose to stdout, so a
        stderr-only message degraded to a useless ``<no stderr>`` and someone
        had to SSH in to find out why.
        """
        from kiro_crew.instances import token_mint as tm

        self._fake_proc(
            monkeypatch,
            1,
            b"\xe2\x9d\x8c Could not reach gateway on port 5476: <urlopen error refused>\n",
            b"",
        )
        with pytest.raises(tm.TokenMintError) as excinfo:
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        msg = str(excinfo.value)
        assert "Could not reach gateway on port 5476" in msg
        assert "stdout tail:" in msg
        assert "<no stderr>" in msg  # stderr genuinely was empty — still reported

    def test_unparseable_output_surfaces_stdout_tail(self, monkeypatch):
        from kiro_crew.instances import token_mint as tm

        self._fake_proc(monkeypatch, 0, b"Gateway returned empty token\n", b"")
        with pytest.raises(tm.TokenMintError) as excinfo:
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        msg = str(excinfo.value)
        assert "could not parse a token" in msg
        assert "Gateway returned empty token" in msg

    def test_stdout_tail_never_leaks_a_token(self, monkeypatch):
        """A URL-borne or bare token on stdout is scrubbed before it reaches the error.

        The tail is only built on failure paths, but a partially-successful
        remote (URL printed, then non-zero exit) can still put a live credential
        on stdout — so the token substitution must happen unconditionally.

        The bare-token case uses the shape this app ACTUALLY mints:
        ``generate_token`` returns ``base64url(payload).base64url(signature)`` —
        two segments, not the three of a classic JWT. A fabricated three-segment
        token here would let a two-segment-blind pattern pass while leaving real
        tokens unscrubbed, so the segment count is asserted explicitly and
        ``test_bare_token_scrubbed_at_every_segment_count`` pins the full range.
        """
        from kiro_crew.instances import token_mint as tm

        minted = "eyJzdWIiOiJvd25lciIsImV4cCI6MTIzfQ.c2lnbmF0dXJlLWJ5dGVz"
        assert minted.count(".") == 1

        self._fake_proc(
            monkeypatch,
            1,
            f"http://localhost:5476?token={minted}\nbare {minted} too\nboom\n".encode(),
            b"",
        )
        with pytest.raises(tm.TokenMintError) as excinfo:
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        msg = str(excinfo.value)
        assert minted not in msg
        # no fragment of the credential survives either — a pattern that matched
        # only part of the token would leave the remaining segment(s) behind.
        for segment in minted.split("."):
            assert segment not in msg
        assert "boom" in msg

    @pytest.mark.parametrize("segments", [2, 3, 5])
    def test_bare_token_scrubbed_at_every_segment_count(self, monkeypatch, segments):
        """Two-segment (minted), three-segment (JWT) and five-segment (JWE) all scrub.

        Five segments is the compact-JWE shape: a pattern capped lower would
        match a prefix and leave ``.ciphertext.tag`` in the surfaced error.
        """
        from kiro_crew.instances import token_mint as tm

        bare = "eyJhbGciOiJIUzI1NiJ9" + "".join(f".seg{i}" for i in range(segments - 1))
        self._fake_proc(monkeypatch, 1, f"{bare}\nwhy it died\n".encode(), b"")
        with pytest.raises(tm.TokenMintError) as excinfo:
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        msg = str(excinfo.value)
        assert bare not in msg
        assert f"seg{segments - 2}" not in msg  # last segment gone, not just a prefix
        assert "why it died" in msg

    def test_stdout_tail_is_bounded_and_absent_when_stdout_empty(self, monkeypatch):
        from kiro_crew.instances import token_mint as tm

        # bounded: only the TAIL is carried (the reason is printed last). The
        # reason sits on its own line, as a real remote prints it — the scan
        # window's left edge falls inside the preceding noise run, which is
        # dropped as potentially-clipped without touching the reason.
        long_out = ("x" * 5000 + "\nREAL-REASON\n").encode()
        self._fake_proc(monkeypatch, 1, long_out, b"")
        with pytest.raises(tm.TokenMintError) as excinfo:
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        msg = str(excinfo.value)
        assert "REAL-REASON" in msg
        assert len(msg) < 600

        # modern remote (reason on stderr, nothing on stdout) keeps the original
        # single-stream message shape — no empty "stdout tail:" noise.
        self._fake_proc(monkeypatch, 127, b"", b"kirocrew binary not found")
        with pytest.raises(tm.TokenMintError) as excinfo:
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        assert "stdout tail:" not in str(excinfo.value)
        assert "kirocrew binary not found" in str(excinfo.value)

    def test_success_path_never_builds_the_stdout_tail(self, monkeypatch):
        """The tail is built inside the failure branches only.

        On success, stdout holds a live token; running the scrub over it would be
        pointless work on credential-bearing text and would contradict the
        "only ever built on a failure path" invariant the helper documents. This
        locks the invariant to control flow instead of a comment.
        """
        from kiro_crew.instances import token_mint as tm

        calls: list[str] = []
        monkeypatch.setattr(
            tm, "_redacted_output_tail", lambda out, *a, **k: calls.append(out) or ""
        )

        self._fake_proc(monkeypatch, 0, b"http://localhost:5476?token=eyJa.b\n", b"")
        assert asyncio.run(tm.mint_remote_token("cd-1", ttl="20h")) == "eyJa.b"
        assert calls == []

        # ...but a failing mint still builds it.
        self._fake_proc(monkeypatch, 1, b"reason on stdout\n", b"")
        with pytest.raises(tm.TokenMintError):
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        assert len(calls) == 1

    def test_scrubbers_never_scan_more_than_the_bounded_window(self, monkeypatch):
        """The scrub cost must not scale with the remote's stdout size.

        `re` does not release the GIL, so scrubbing an unbounded payload blocks
        the gateway's event loop in proportion to its length — measured ~1s per
        MB (13s on 13MB), and an executor hop is no cure (~1.1s stall on the
        same payload, GIL-bound). Asserting on the *input length* handed to the
        redactors instead of on wall-clock keeps this deterministic.
        """
        from kiro_crew.instances import token_mint as tm

        seen: list[int] = []
        real = tm.redact
        monkeypatch.setattr(tm, "redact", lambda text: seen.append(len(text)) or real(text))

        huge = ("x" * 60 + " could not reach gateway\n") * 20_000  # ~1.7 MB
        self._fake_proc(monkeypatch, 1, huge.encode(), b"")
        with pytest.raises(tm.TokenMintError) as excinfo:
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))

        assert seen and max(seen) <= tm._OUTPUT_SCAN_CHARS
        # The bound must not cost diagnosability: the reason still travels.
        assert "could not reach gateway" in str(excinfo.value)

    def test_secret_clipped_by_the_window_boundary_is_never_shown(self, monkeypatch):
        """A token straddling the window start must not leak its suffix.

        The window slice can land mid-run, which would show the token regexes a
        fragment they cannot match while its suffix still lands inside the carried
        300 chars. Truncated input therefore drops the leading run of URL /
        base64url characters. The floor for that drop is low on purpose: a
        fragment that looks too far from the end to matter is still pulled into
        the tail when the scrubbers shrink the text around it, which the third
        case below pins.
        """
        from kiro_crew.instances import token_mint as tm

        secret = "eyJ" + "A" * 3000 + ".SIGNATURE-MUST-NOT-APPEAR"
        for stdout in (f"noise\n{secret}\n", secret):  # with and without a trailing newline
            self._fake_proc(monkeypatch, 1, stdout.encode(), b"")
            with pytest.raises(tm.TokenMintError) as excinfo:
                asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
            msg = str(excinfo.value)
            assert "SIGNATURE-MUST-NOT-APPEAR" not in msg
            assert "AAAA" not in msg
            assert "<clipped>" in msg

        # A clipped fragment far from the end is NOT unreachable: the scrubbers
        # shrink the window (each blob collapses to `<redacted>`), pulling earlier
        # text into the carried tail. This is why the clipped-run floor is low
        # instead of the window-minus-tail distance — with a 2100-char floor this
        # case surfaces the raw fragment.
        blob = "eyJ" + "z" * 200 + "." + "y" * 200
        redactable = f"{blob}\n" * 5
        secret_run = "MUSTNOTAPPEAR" * 39  # one unbroken run, no whitespace
        # Size the prefix so the window's left edge lands INSIDE secret_run.
        prefix_len = tm._OUTPUT_SCAN_CHARS - len(redactable) - len(secret_run) // 2
        shrinking = "p" * prefix_len + "\n" + secret_run + "\n" + redactable
        assert len(shrinking) > tm._OUTPUT_SCAN_CHARS
        self._fake_proc(monkeypatch, 1, shrinking.encode(), b"")
        with pytest.raises(tm.TokenMintError) as excinfo:
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        assert "MUSTNOTAPPEAR" not in str(excinfo.value)

        # A long stdout still carries its reason when nothing is clipped away.
        padded = "x" * 4000 + "\n" + "short-run-word " * 20 + "\nWHY-IT-FAILED\n"
        self._fake_proc(monkeypatch, 1, padded.encode(), b"")
        with pytest.raises(tm.TokenMintError) as excinfo:
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        msg = str(excinfo.value)
        assert "WHY-IT-FAILED" in msg
        assert "short-run-word" in msg


# ── gateway run-marker (mint prefers the running gateway's install) ───────────


class TestRunMarker:
    def test_write_read_clear(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.instances import run_marker

        # Fabricate a venv layout: <venv>/bin/{python,kirocrew}
        bindir = tmp_path / "venv" / "bin"
        bindir.mkdir(parents=True)
        launcher = bindir / "kirocrew"
        launcher.write_text("#!/bin/sh\n")
        launcher.chmod(0o755)
        monkeypatch.setattr(sys, "executable", str(bindir / "python"))

        assert run_marker.gateway_launcher_path() == str(launcher)

        run_marker.write_marker(7879)
        marker = run_marker.marker_path(7879)
        assert marker.read_text(encoding="utf-8").strip() == str(launcher)
        # The pid sidecar rides alongside and names THIS process.
        assert run_marker.read_pid(7879) == os.getpid()
        # The start identity is its OWN file, so the pid file stays a bare pid
        # that main's shipped whole-file isdigit() reader still parses.
        start = run_marker._start_path_for(run_marker.pid_path(7879))
        assert start.name == "gateway-7879.start"
        assert start.read_text(encoding="utf-8").strip() == run_marker.pid_start_token(os.getpid())

        run_marker.clear_marker(7879)
        assert not marker.exists()
        assert not run_marker.pid_path(7879).exists()  # sidecar cleared too
        assert not start.exists()  # ...and so is the start identity it attests
        assert run_marker.read_pid(7879) is None
        run_marker.clear_marker(7879)  # clearing a missing marker is a no-op

    def test_port_only_marker_when_launcher_absent(self, tmp_path, monkeypatch):
        """No console script → still write the marker, but empty.

        Discovery needs only the filename, so skipping the write would deny
        discovery to source-tree launches. Mint stays unaffected because its
        shell clause requires a non-empty executable path.
        """
        import sys

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.instances import run_marker

        bindir = tmp_path / "venv2" / "bin"
        bindir.mkdir(parents=True)  # no sibling 'kirocrew' launcher
        monkeypatch.setattr(sys, "executable", str(bindir / "python"))

        assert run_marker.gateway_launcher_path() is None
        run_marker.write_marker(7000)
        marker = run_marker.marker_path(7000)
        assert marker.exists()
        assert marker.read_text(encoding="utf-8") == ""
        # Discoverable by port...
        assert run_marker.marker_ports() == [7000]

    def test_mint_clause_ignores_an_empty_marker(self):
        """...and inert for mint: the exec is guarded on a non-empty -x path."""
        from kiro_crew.instances.token_mint import build_candidate_command

        cmd = build_candidate_command("status", marker_port=7000)
        assert '[ -n "$__kb" ] && [ -x "$__kb" ]' in cmd

    def test_run_dir_lockdown_is_the_directory_helper(self, tmp_path, monkeypatch):
        """``run/`` holds the gateway's credential, pid and launcher marker.

        A bare ``os.chmod(0o700)`` is a silent no-op on Windows, so the
        directory and everything created inside it keep the inherited DACL.
        The tightening must go through ``platform_compat.restrict_dir_to_owner``,
        whose Windows grants carry ``(OI)(CI)`` and so also cover the ``.pid``
        and ``.bin`` sidecars, which ``atomic_write(mode=0o600)`` cannot.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import platform_compat
        from kiro_crew.instances import run_marker

        seen: list[str] = []
        # Re-patch over the session-wide Windows stub in conftest so this
        # assertion is not vacuous on the Windows matrix.
        monkeypatch.setattr(platform_compat, "restrict_dir_to_owner", lambda p: seen.append(str(p)))
        d = run_marker._run_dir()
        assert seen == [str(d)]

    def test_run_dir_lockdown_failure_is_best_effort(self, tmp_path, monkeypatch):
        """A refused lockdown must not break gateway startup.

        ``restrict_dir_to_owner`` is fail-loud by design, but ``_run_dir`` is on
        the path of ``secret_path()``, which the dashboard calls outside a
        try/except. The existing contract — tighten if possible, otherwise carry
        on — is what the caller depends on, so the raise is absorbed here.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import platform_compat
        from kiro_crew.instances import run_marker

        def _refuse(_path):
            raise OSError("lockdown refused")

        monkeypatch.setattr(platform_compat, "restrict_dir_to_owner", _refuse)
        d = run_marker._run_dir()
        assert d.is_dir()


# ── Windows sharing-violation retry on the sidecar readers (GH-12364) ──


class TestRunMarkerSidecarReadRetry:
    """A sidecar reader must not fold a transient Windows sharing violation into
    ``""`` / ``None``.

    The markers are published with ``mkstemp`` + ``os.replace``
    (``write_marker``). On Windows a reader that lands in the ``os.replace``
    window raises ``PermissionError`` (``WinError 32``). The old plain
    ``read_text`` folded that into ``""`` -- indistinguishable from a genuinely
    absent marker, so a caller checking whether a gateway runs on a port got a
    silent FALSE NEGATIVE. Every reader now goes through
    ``atomic_write.read_bytes_with_retry``, which retries that bounded window.

    ``read_sharing_violation`` patches ``Path.read_bytes`` and so covers the
    uncapped readers (``read_secret`` / ``read_launcher`` via ``_read_sidecar``).
    The capped readers (``read_pid`` and the ``.start`` sidecar) pass
    ``max_bytes`` and so read through ``_read_bytes``; those are faulted by
    patching ``atomic_write._read_bytes``, the single chokepoint both branches of
    the helper call. POSIX permits the read, so this fault only reproduces on the
    simulator here and on a real Windows host -- it is skipped on this Linux
    worker's live filesystem, which is why the simulator exists. Not run on a
    real Windows host.
    """

    @pytest.fixture(autouse=True)
    def _no_backoff_sleep(self, _floor_monkeypatch):
        """Keep the bounded retry instant; attempt COUNT is what matters here.

        Only the simulator tests belong under this fixture. The real-file
        concurrency test lives in ``TestRunMarkerSidecarConcurrentRewrite``
        below, OUTSIDE it, because there the attempts must be spaced in time:
        with the sleep zeroed the whole budget fits inside one writer syscall.

        Patches through ``_floor_monkeypatch`` (the isolation floor's own
        ``MonkeyPatch``), not the shared ``monkeypatch``: an autouse fixture that
        used the shared instance would be lifted by any test here that called
        ``monkeypatch.undo()`` mid-body, silently restoring the real backoff
        sleep for the rest of that test (D11 / semgrep
        ``kirocrew.test-autouse-shared-monkeypatch``).
        """
        from kiro_crew import atomic_write as aw

        _floor_monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)

    def _write_full_marker(self, run_marker, port, monkeypatch, tmp_path):
        """Fabricate a venv launcher and write a full marker set for *port*."""
        bindir = tmp_path / "venv" / "bin"
        bindir.mkdir(parents=True)
        launcher = bindir / ("kirocrew.exe" if os.name == "nt" else "kirocrew")
        launcher.write_text("#!/bin/sh\n")
        launcher.chmod(0o755)
        monkeypatch.setattr(sys, "executable", str(bindir / "python"))
        run_marker.write_marker(port)
        return launcher

    def test_read_secret_retries_the_sharing_violation(self, tmp_path, monkeypatch):
        """A transient fault on the credential sidecar retries, not folds to ""."""
        from windows_sim import read_sharing_violation

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import platform_compat
        from kiro_crew.atomic_write import atomic_write
        from kiro_crew.instances import run_marker

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        atomic_write(run_marker.secret_path(6776), "s3cr3t-value", mode=0o600)

        with read_sharing_violation(match="gateway-6776.secret", times=2) as sim:
            got = run_marker.read_secret(6776)

        assert got == "s3cr3t-value"  # not "" -- the window was ridden out
        assert sim["n"] == 3  # 2 faults + the successful read

    def test_read_launcher_retries_the_sharing_violation(self, tmp_path, monkeypatch):
        """The marker (launcher path) reader rides the window out too."""
        from windows_sim import read_sharing_violation

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import platform_compat
        from kiro_crew.instances import run_marker

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        launcher = self._write_full_marker(run_marker, 6776, monkeypatch, tmp_path)

        with read_sharing_violation(match="gateway-6776.bin", times=2) as sim:
            got = run_marker.read_launcher(6776)

        assert got == str(launcher)  # not None
        assert sim["n"] == 3

    def test_read_pid_retries_the_sharing_violation(self, tmp_path, monkeypatch):
        """read_pid -- the identity claim the issue names -- must not fold to None.

        read_pid reads through the capped path (``max_bytes``), so it is faulted
        at ``_read_bytes`` rather than ``Path.read_bytes``.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import atomic_write as aw
        from kiro_crew import platform_compat
        from kiro_crew.instances import run_marker

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        self._write_full_marker(run_marker, 6776, monkeypatch, tmp_path)
        pid_name = run_marker.pid_path(6776).name

        real_read_bytes = aw._read_bytes
        state = {"n": 0}

        def _faulting(target, max_bytes):
            if target.name == pid_name:
                state["n"] += 1
                if state["n"] <= 2:
                    raise PermissionError(
                        f"[WinError 32] simulated sharing violation reading {target}"
                    )
            return real_read_bytes(target, max_bytes)

        monkeypatch.setattr(aw, "_read_bytes", _faulting)
        got = run_marker.read_pid(6776)

        assert got == os.getpid()  # not None -- the window was ridden out
        assert state["n"] == 3

    def test_absent_marker_still_reads_as_empty(self, tmp_path, monkeypatch):
        """A GENUINELY absent file still yields ""/None -- absence is unchanged.

        FileNotFoundError is not a sharing violation; the helper does not retry
        it and the readers fold it to their empty value exactly as before.
        """
        from windows_sim import read_sharing_violation

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import platform_compat
        from kiro_crew.instances import run_marker

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

        # No marker set written at all. Even under an active fault simulator, a
        # missing file raises FileNotFoundError, not PermissionError, so it is
        # not retried and the empty return is preserved.
        with read_sharing_violation(times=10_000):
            assert run_marker.read_secret(9999) == ""
            assert run_marker.read_launcher(9999) is None
        assert run_marker.read_pid(9999) is None

    def test_no_contention_common_path_is_unchanged(self, tmp_path, monkeypatch):
        """With no concurrent writer the readers return the same values as before.

        The retry helper is a plain read on POSIX and on the no-fault Windows
        path, so the common case is byte-for-byte prior behaviour.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.instances import run_marker

        launcher = self._write_full_marker(run_marker, 7001, monkeypatch, tmp_path)
        assert run_marker.read_launcher(7001) == str(launcher)
        assert run_marker.read_pid(7001) == os.getpid()


class TestRunMarkerSidecarConcurrentRewrite:
    """Real files, real threads: a reader racing a tight ``atomic_write`` loop.

    Deliberately NOT a member of ``TestRunMarkerSidecarReadRetry``. That class's
    autouse fixture zeroes ``_REPLACE_BACKOFF_SECONDS`` because its simulator
    tests count attempts; here the attempts must be spaced in TIME. On Windows
    the writer's ``os.replace`` (``MoveFileEx`` superseding the target) runs with
    the GIL released, and every read that opens the file meanwhile raises
    WinError 32, so with the sleep zeroed the reader burns all ten attempts in a
    few microseconds INSIDE that one syscall and folds to ``None`` -- a false
    absence the ``Backend Tests (Windows)`` matrix saw intermittently. The
    product's 50 ms backoff is what rides the window out, so it stays in force.
    """

    def test_concurrent_rewrites_never_fold_to_a_false_absence(self, tmp_path, monkeypatch):
        """The issue's own reproduction, on real files.

        One thread rewrites a sidecar in a tight ``atomic_write`` (mkstemp +
        os.replace) loop while the main thread reads it thousands of times. On
        POSIX no sharing violation occurs, so this asserts the positive invariant
        that holds on every platform: a read that returns a value never returns a
        TORN or EMPTY one -- it is always a value some writer published, never ""
        while the file exists. (The Windows-specific fold is covered
        deterministically by the simulator tests in
        ``TestRunMarkerSidecarReadRetry``.)

        The launcher-marker sidecar stands in for the whole sidecar family here:
        the read-retry chokepoint (``read_bytes_with_retry``) is shared by every
        reader, so the concurrent-rewrite invariant is identical whichever sidecar
        is exercised. ``read_secret`` has its own deterministic coverage.

        The writer's failure is surfaced, not swallowed: a writer that dies
        (``replace_with_retry`` out of budget against a reader's open handle)
        leaves a stable file behind, and every later read would then pass this
        test vacuously. The reads also start only after the writer's first
        publish, so they are known to race a live writer.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.atomic_write import atomic_write
        from kiro_crew.instances import run_marker

        marker = run_marker.marker_path(6776)
        values = {f"/opt/venv-{i}/bin/kirocrew" for i in range(50)}
        atomic_write(marker, "/opt/venv-0/bin/kirocrew\n", mode=0o600)

        stop = threading.Event()
        first_publish = threading.Event()
        writer_error: list[BaseException] = []

        def _rewrite():
            i = 0
            try:
                while not stop.is_set():
                    atomic_write(marker, f"/opt/venv-{i % 50}/bin/kirocrew\n", mode=0o600)
                    i += 1
                    first_publish.set()
            except BaseException as exc:  # surfaced on the main thread below
                writer_error.append(exc)

        writer = threading.Thread(target=_rewrite, daemon=True)
        writer.start()
        try:
            # The reads must race a live writer: on a saturated host the main
            # thread could otherwise finish every read before the writer's first
            # publish and prove nothing.
            assert first_publish.wait(5), "the writer never published: the reads raced nothing"
            for _ in range(3000):
                got = run_marker.read_launcher(6776)
                # The sidecar exists for the whole run, so a reader must never
                # see it as absent, and must only ever see a published value.
                assert got is not None, "a reader folded a concurrent write into a false absence"
                assert got in values, f"a reader saw a torn value: {got!r}"
        finally:
            stop.set()
            writer.join(timeout=5)
        assert not writer.is_alive(), "the writer thread did not stop"
        assert not writer_error, f"the writer died mid-run: {writer_error[0]!r}"


# ── run-marker port discovery (clients find a gateway on a non-default port) ──


class TestRunMarkerDiscovery:
    """``marker_ports`` — filename-only discovery.

    This backs ``cli_server.resolve_client_port``'s zero-config fallback, so the
    contract under test is: filename-only parsing and no directory creation.
    Deciding whether a discovered port is *trustworthy* is deliberately NOT this
    module's job (a listener is not proof of identity) — see
    ``TestResolveClientPortRunMarker`` for the ownership gate.
    """

    def _marker(self, home, name: str) -> None:
        d = home / "run"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text("/some/venv/bin/kirocrew\n", encoding="utf-8")

    def test_no_run_dir_yields_no_ports_and_creates_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.instances import run_marker

        assert run_marker.marker_ports() == []
        # Discovery is read-only: a client merely looking for a gateway must not
        # materialise run/ (marker_path() would, via _run_dir()).
        assert not (tmp_path / "run").exists()

    def test_lists_sorted_ports_and_ignores_non_markers(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.instances import run_marker

        self._marker(tmp_path, "gateway-6776.bin")
        self._marker(tmp_path, "gateway-5476.bin")
        # Non-markers / malformed ports must be ignored rather than crash:
        for junk in (
            "gateway-.bin",
            "gateway-abc.bin",
            "gateway-6776.bin.old",
            "gateway--1.bin",
            "gateway-0.bin",  # not a usable TCP port
            "gateway-70000.bin",  # out of range
            "gateway-67 76.bin",
            "sandbox-6776.bin",
        ):
            self._marker(tmp_path, junk)
        # A directory that merely looks like a marker is not a marker.
        (tmp_path / "run" / "gateway-9999.bin").mkdir()

        assert run_marker.marker_ports() == [5476, 6776]

    def test_no_bare_liveness_helper_is_exposed(self, tmp_path, monkeypatch):
        """Reachability must not be offered as a stand-in for identity.

        A client command sends the local secret to whatever answers on the
        discovered port, so "something is listening" is not a safe basis for
        trusting a marker. Keeping any such helper out of this module stops a
        future caller from reaching for the unsafe check.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.instances import run_marker

        assert not hasattr(run_marker, "port_is_live")
        assert not hasattr(run_marker, "live_marker_ports")

    def test_read_pid_rejects_junk_and_missing(self, tmp_path, monkeypatch):
        """The sidecar is an identity claim, so parse it strictly."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.instances import run_marker

        assert run_marker.read_pid(6776) is None  # absent
        d = tmp_path / "run"
        d.mkdir(parents=True, exist_ok=True)
        for junk in ("", "  ", "abc", "-1", "0", "12 34", "12.5", "1e3"):
            (d / "gateway-6776.pid").write_text(junk, encoding="utf-8")
            assert run_marker.read_pid(6776) is None, junk
        (d / "gateway-6776.pid").write_text(" 4242 \n", encoding="utf-8")
        assert run_marker.read_pid(6776) == 4242

    def test_explicit_pid_reader_is_bounded_ascii_decimal_and_read_only(self, tmp_path):
        from kiro_crew.instances import run_marker

        path = tmp_path / "missing" / "gateway.pid"
        assert run_marker._read_pid_path(path) is None
        assert not path.parent.exists()
        path.parent.mkdir()
        for junk in (b"\xd9\xa4\xd9\xa2", b"+42", b"-42", b"0", b"9" * 65):
            path.write_bytes(junk)
            assert run_marker._read_pid_path(path) is None
        path.write_bytes(b" 4242 \n")
        assert run_marker._read_pid_path(path) == 4242
        path.write_bytes(b"9" * 64)
        assert run_marker._read_pid_path(path) == int(b"9" * 64)

    def test_the_start_identity_is_read_from_its_own_sidecar(self, tmp_path):
        """The token lives beside the pid file, never inside it.

        Keeping the pid file a bare pid is what lets the reader SHIPPED on an
        older client -- whole file, stripped, ``isdigit()`` -- keep parsing a
        record this gateway wrote, instead of answering ``None`` and denying a
        gateway that genuinely is ours.
        """
        from kiro_crew.instances import run_marker

        pid_file = tmp_path / "gateway-7999.pid"
        pid_file.write_bytes(b"4242\n")
        start = run_marker._start_path_for(pid_file)
        assert start == tmp_path / "gateway-7999.start"

        # No sidecar: a pid with no proof of freshness, never a wildcard match.
        assert run_marker.read_pid_record_path(pid_file) == (4242, "")
        start.write_bytes(b"246853591\n")
        assert run_marker.read_pid_record_path(pid_file) == (4242, "246853591")

        # Defensive read-side parsing: oversized, non-ASCII and whitespace-bearing
        # sidecars all read as unproven rather than as some other process's value.
        for junk in (b"9" * 129, b"\xd9\xa4\xd9\xa2", b"12 34", b"12\n34"):
            start.write_bytes(junk)
            assert run_marker.read_pid_record_path(pid_file) == (4242, ""), junk

        # Reading a record never materialises the sidecar it looked for.
        absent = tmp_path / "elsewhere" / "gateway-7000.pid"
        assert run_marker.read_pid_record_path(absent) is None
        assert not absent.parent.exists()

    def test_the_pid_body_still_parses_under_the_reader_shipped_on_main(
        self, tmp_path, monkeypatch
    ):
        """The whole point of splitting the record out of the pid file.

        ``read_pid`` as shipped on ``origin/main`` is ``_read_sidecar(port,
        ".pid")`` -- the WHOLE file, stripped -- gated on ``isdigit()``. This
        asserts the body a live ``write_marker`` produces against exactly that
        reader, so a second line can never be reintroduced without a red test.
        An older client venv sharing this data home is the caller that breaks.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.instances import run_marker

        run_marker.write_marker(7999)
        pid_file = run_marker.pid_path(7999)
        # The invariant is "one line holding the bare pid", not a literal byte
        # string: atomic_write opens in text mode, so Windows translates the
        # trailing \n to \r\n — for THIS branch and for main's writer alike,
        # which is exactly why the shipped reader strips before isdigit().
        body = pid_file.read_bytes()
        assert body.decode("ascii").splitlines() == [str(os.getpid())]

        shipped = run_marker._read_sidecar(7999, ".pid")  # main's read, verbatim
        assert shipped.isdigit()
        assert int(shipped) == os.getpid()

        # The identity is still recorded -- just in the file next to it.
        assert run_marker.read_pid_record_path(pid_file) == (
            os.getpid(),
            run_marker.pid_start_token(os.getpid()),
        )

    def test_start_identity_comes_from_the_shared_start_id_producer(self, monkeypatch):
        """One producer for the value written and the value compared.

        ``platform_compat.get_process_start_id`` is microsecond-resolution on
        macOS, unlike ``process_start_time``'s ``ps -o lstart=`` spelling, whose
        1-second granularity lets a pid recycled inside the same second forge an
        identical value.
        """
        from kiro_crew import platform_compat
        from kiro_crew.instances import run_marker

        monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: "1730.000042")
        assert run_marker.pid_start_token(4242) == "1730.000042"

        # The preferred producer answers None on an unrecognised host or a
        # failed read. When it says nothing, the fallback is consulted rather
        # than the token going empty -- that fallback is the ONLY start identity
        # available on Windows (a creation FILETIME), so skipping it would make a
        # pod there permanently unprovable.
        monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: None)
        monkeypatch.setattr(platform_compat, "process_start_time", lambda pid: "133724160000000000")
        assert run_marker.pid_start_token(4242) == "133724160000000000"

        # A space-padded fallback (the macOS `ps -o lstart=` spelling) is
        # collapsed to a single token, because the reader refuses inner whitespace.
        monkeypatch.setattr(
            platform_compat, "process_start_time", lambda pid: "Thu Sep  4 00:00:00 2026"
        )
        assert run_marker.pid_start_token(4242) == "Thu-Sep-4-00:00:00-2026"

        # Only when NEITHER producer will answer is the token unproven.
        monkeypatch.setattr(platform_compat, "process_start_time", lambda pid: None)
        assert run_marker.pid_start_token(4242) == ""

        def _boom(pid: int) -> str:
            raise OSError("libproc exploded")

        monkeypatch.setattr(platform_compat, "get_process_start_id", _boom)
        monkeypatch.setattr(platform_compat, "process_start_time", _boom)
        assert run_marker.pid_start_token(4242) == ""

        # A raising primary must still let the working fallback answer.
        monkeypatch.setattr(platform_compat, "process_start_time", lambda pid: "133724160000000001")
        assert run_marker.pid_start_token(4242) == "133724160000000001"

    def test_read_launcher_reads_marker_content(self, tmp_path, monkeypatch):
        """read_launcher returns the recorded path, None for absent/empty, and
        never creates run/ (read-only, like read_pid)."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.instances import run_marker

        assert run_marker.read_launcher(6776) is None  # absent
        assert not (tmp_path / "run").exists()  # read never materialises run/
        d = tmp_path / "run"
        d.mkdir(parents=True, exist_ok=True)
        # Empty marker: a source-tree launch records no launcher (port-only).
        (d / "gateway-6776.bin").write_text("", encoding="utf-8")
        assert run_marker.read_launcher(6776) is None
        (d / "gateway-6776.bin").write_text("  \n", encoding="utf-8")
        assert run_marker.read_launcher(6776) is None
        (d / "gateway-6776.bin").write_text("/opt/venv/bin/kirocrew\n", encoding="utf-8")
        assert run_marker.read_launcher(6776) == "/opt/venv/bin/kirocrew"

    def test_write_prunes_markers_from_earlier_runs(self, tmp_path, monkeypatch):
        """A gateway is a singleton per home, so markers naming other ports are
        crash residue. Left alone they cost every client command an extra
        listener lookup, so the live gateway reaps them when it writes its own.
        """
        import sys

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.instances import run_marker

        bindir = tmp_path / "venv" / "bin"
        bindir.mkdir(parents=True)
        launcher = bindir / "kirocrew"
        launcher.write_text("#!/bin/sh\n")
        launcher.chmod(0o755)
        monkeypatch.setattr(sys, "executable", str(bindir / "python"))

        # Residue from three earlier runs on other ports.
        self._marker(tmp_path, "gateway-5476.bin")
        (tmp_path / "run" / "gateway-5476.pid").write_text("111\n", encoding="utf-8")
        (tmp_path / "run" / "gateway-5476.start").write_text("111-start\n", encoding="utf-8")
        self._marker(tmp_path, "gateway-6777.bin")
        self._marker(tmp_path, "gateway-9001.bin")

        run_marker.write_marker(6776)

        assert run_marker.marker_ports() == [6776]
        assert not (tmp_path / "run" / "gateway-5476.pid").exists()
        # The start identity goes with the pid it attests; left behind it would
        # pair a dead generation's token with a pid file a later gateway rewrites.
        assert not (tmp_path / "run" / "gateway-5476.start").exists()
        assert run_marker.read_pid(6776) == os.getpid()

    def test_prune_keeps_unrelated_files(self, tmp_path, monkeypatch):
        """Pruning targets only this module's own marker/pid pairs."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.instances import run_marker

        d = tmp_path / "run"
        d.mkdir(parents=True, exist_ok=True)
        (d / "sandbox-6776.bin").write_text("keep me", encoding="utf-8")
        (d / "gateway-abc.bin").write_text("keep me", encoding="utf-8")
        self._marker(tmp_path, "gateway-6777.bin")

        run_marker.prune_markers(keep_port=6776)

        assert not (d / "gateway-6777.bin").exists()
        assert (d / "sandbox-6776.bin").exists()
        assert (d / "gateway-abc.bin").exists()


# ── injection-safe validation ────────────────────────────────────────────────


class TestValidation:
    @pytest.mark.parametrize("good", ["cd-1-alias", "user@host.example.com", "h_1"])
    def test_ssh_host_accept(self, good):
        from kiro_crew.instances.validation import validate_ssh_host

        assert validate_ssh_host(good) == good

    @pytest.mark.parametrize(
        "bad", ["-oProxyCommand=x", "a b", "a;b", "a$b", "a@b@c", "", "@h", "h@", "`x`"]
    )
    def test_ssh_host_reject(self, bad):
        from kiro_crew.instances.validation import SshValidationError, validate_ssh_host

        with pytest.raises(SshValidationError):
            validate_ssh_host(bad)

    def test_remote_bin(self):
        from kiro_crew.instances.validation import SshValidationError, validate_remote_bin

        assert validate_remote_bin("") == ""
        assert validate_remote_bin("~/.local/bin/kirocrew") == "~/.local/bin/kirocrew"
        for bad in ("$(x)", "a;b", "`x`", "-rf", 'a"b'):
            with pytest.raises(SshValidationError):
                validate_remote_bin(bad)


# ── registry ──────────────────────────────────────────────────────────────────


class TestRegistry:
    def _reg(self, tmp_path):
        from kiro_crew.instances.registry import InstancesRegistry

        return InstancesRegistry(path=tmp_path / "instances.json")

    def test_crud_and_collision(self, tmp_path):
        from kiro_crew.instances.registry import DuplicateInstanceError

        reg = self._reg(tmp_path)
        a = reg.add(name="Cloud Desktop 1", ssh_host="cd-1-alias")
        assert a.id == "cloud-desktop-1" and a.remote_port == 5476 and a.was_connected is False
        b = reg.add(name="Cloud Desktop 1", ssh_host="cd-2-alias")
        assert b.id == "cloud-desktop-1-2"
        with pytest.raises(DuplicateInstanceError):
            reg.add(name="x", ssh_host="h", instance_id="cloud-desktop-1")
        assert len(reg.list()) == 2

    def test_update_validation_and_hints(self, tmp_path):
        from kiro_crew.instances.registry import InstanceNotFoundError, InvalidInstanceError

        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        u = reg.update("cd-1", local_port=7778, was_connected=True)
        assert u.local_port == 7778 and u.was_connected is True
        with pytest.raises(InvalidInstanceError):
            reg.update("cd-1", remote_port=70000)
        with pytest.raises(InvalidInstanceError):
            reg.update("cd-1", id="nope")
        with pytest.raises(InstanceNotFoundError):
            reg.update("ghost", name="z")

    def test_update_mark_last_active_is_one_mutation(self, tmp_path):
        """update(mark_last_active=True) records the auto-revive target in the
        same read-modify-write as the field changes, and plain update leaves
        the recorded target alone."""
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        reg.add(name="CD2", ssh_host="cd-2", instance_id="cd-2")

        u = reg.update("cd-1", mark_last_active=True, local_port=7778, was_connected=True)
        assert u.local_port == 7778 and u.was_connected is True
        assert reg.get_last_active().id == "cd-1"

        # A plain update on another instance does not steal the target.
        reg.update("cd-2", local_port=7779)
        assert reg.get_last_active().id == "cd-1"

    def test_remove_clears_last_active_and_reload(self, tmp_path):
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        reg.update("cd-1", mark_last_active=True)
        assert reg.remove("cd-1") is True
        assert reg.remove("cd-1") is False
        assert reg.get_last_active() is None
        # fresh instance reads the same (empty) file
        assert self._reg(tmp_path).list() == []

    def test_no_credentials_persisted(self, tmp_path):
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        raw = (tmp_path / "instances.json").read_text(encoding="utf-8")
        assert "token" not in raw.lower()

    def test_env_home_path(self, tmp_path, monkeypatch):
        from kiro_crew.instances.registry import InstancesRegistry

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        assert InstancesRegistry().path == tmp_path / "instances.json"

    def test_two_registries_over_one_file_share_a_lock(self, tmp_path):
        """Objects over the same file hold one lock; different files hold two.

        Callers construct a registry per request, so a lock owned by the object
        serialises nothing between them.
        """
        from kiro_crew.instances.registry import InstancesRegistry

        first = self._reg(tmp_path)
        second = self._reg(tmp_path)
        assert first._lock is second._lock

        other = tmp_path / "elsewhere"
        other.mkdir()
        assert InstancesRegistry(other / "instances.json")._lock is not first._lock

    def test_a_second_writer_cannot_read_between_a_read_and_its_write(self, tmp_path):
        """An interleaved second registry must not drop the first's record.

        The writing thread is held inside its own write and the second thread is
        released to add a different instance. Both records have to survive: a
        reader admitted before the first write lands sees the pre-add document
        and persists it back without that record.

        Both waits are bounded, so the serialised case simply times out and
        proceeds rather than hanging.
        """
        from kiro_crew.instances.registry import InstancesRegistry

        path = tmp_path / "instances.json"
        writer = InstancesRegistry(path)
        intruder = InstancesRegistry(path)

        inside_write = threading.Event()
        intruder_done = threading.Event()
        real_write = type(writer)._write

        def holding_write(self, doc):
            inside_write.set()
            intruder_done.wait(timeout=2.0)
            real_write(self, doc)

        writer._write = types.MethodType(holding_write, writer)

        def add_intruder():
            inside_write.wait(timeout=2.0)
            intruder.add(name="Second", ssh_host="second-1", instance_id="second-1")
            intruder_done.set()

        thread = threading.Thread(target=add_intruder, daemon=True)
        thread.start()
        writer.add(name="First", ssh_host="first-1", instance_id="first-1")
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "the second writer never finished"

        ids = sorted(inst.id for inst in InstancesRegistry(path).list())
        assert ids == ["first-1", "second-1"]


# ── SshTunnelManager (mocked) ─────────────────────────────────────────────────


#: Test classes that drive ``SshTunnelManager.connect`` on the REAL port probe on
#: purpose, each with the reason. Everything else must pin the probe.
_REAL_PORT_PROBE_CLASSES = {
    "test_instances_orphan_reclaim.py::TestOrphanForwarderReclaim": "occupancy of a real port is the trigger",
}


def test_every_manager_connect_test_pins_the_port_probe():
    """A test that connects a manager must not depend on the host's free ports.

    ``connect()`` probes its allocated loopback port for real. A test that hands
    the manager a fixed ``base_port`` and leaves the probe real passes only where
    nothing else holds that port; on a shared macOS runner something does, and
    ``connect()`` quietly returns an error status instead of a tunnel.

    Two shapes are checked: a class that builds and connects a manager, and a
    module-level helper that builds one for other tests to connect. Either must
    pin the probe outside any single ``test_`` method -- ``_patch_port_probe``
    or a patch of ``_is_port_free``, written in place or reached through a
    module-level helper that does it -- or be named above with the reason the
    real probe is the subject.
    """
    import ast

    def _seg(src: str, node) -> str:
        return ast.get_source_segment(src, node) or ""

    def _pins(seg: str, helpers: set) -> bool:
        if "_patch_port_probe(" in seg or '"_is_port_free", lambda' in seg:
            return True
        return any(f"{name}(" in seg for name in helpers)

    def _pinned(src: str, node, helpers: set) -> bool:
        if not isinstance(node, ast.ClassDef):
            return _pins(_seg(src, node), helpers)
        return any(
            isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not m.name.startswith("test_")
            and _pins(_seg(src, m), helpers)
            for m in node.body
        )

    offenders = []
    for path in sorted(Path(__file__).parent.rglob("test_*.py")):
        src = path.read_text(encoding="utf-8")
        if "SshTunnelManager(" not in src:
            continue
        top = [
            n
            for n in ast.parse(src).body
            if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        # Module-level helpers that pin the probe themselves, e.g. a `_free_ports`.
        helpers = {
            n.name
            for n in top
            if not isinstance(n, ast.ClassDef)
            and not n.name.startswith("test_")
            and _pins(_seg(src, n), set())
        }
        for node in top:
            seg = _seg(src, node)
            if "SshTunnelManager(" not in seg:
                continue
            # A builder matters only where the same file connects what it builds.
            builder = (
                not isinstance(node, ast.ClassDef)
                and not node.name.startswith("test_")
                and ".connect(" in src
            )
            if ".connect(" not in seg and not builder:
                continue
            if _pinned(src, node, helpers):
                continue
            key = f"{path.name}::{node.name}"
            if key not in _REAL_PORT_PROBE_CLASSES:
                offenders.append(key)
    assert offenders == [], (
        "these build or connect a SshTunnelManager on the host's real port probe; "
        f"pin it with _patch_port_probe: {offenders}"
    )


class TestSshTunnelArgvCompression:
    @pytest.fixture(autouse=True)
    def _free_ports(self, monkeypatch):
        _patch_port_probe(monkeypatch)

    def test_compression_flag_present_by_default(self):
        from kiro_crew.instances.ssh_tunnel_manager import _build_ssh_tunnel_argv

        argv = _build_ssh_tunnel_argv("host-a", 7779, 7879)
        assert "-C" in argv
        # -C must sit before the -L forward / host (an ssh option, not a positional)
        assert argv.index("-C") < argv.index("-L")
        assert argv[0] == "ssh" and argv[-1] == "host-a"

    def test_compression_flag_omitted_when_disabled(self):
        from kiro_crew.instances.ssh_tunnel_manager import _build_ssh_tunnel_argv

        argv = _build_ssh_tunnel_argv("host-a", 7779, 7879, compression=False)
        assert "-C" not in argv
        # the rest of the shape is intact
        assert "BatchMode=yes" in argv and "AddressFamily=inet" in argv
        assert "127.0.0.1:7779:127.0.0.1:7879" in argv

    @pytest.mark.asyncio
    async def test_manager_threads_compression_to_tunnel(self, tmp_path):
        # The manager must thread ssh_compression to the tunnel factory on
        # connect() -- that flag is what _build_ssh_tunnel_argv uses to add/omit
        # -C. Drive a real connect and assert the captured value both ways.
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager, TunnelState

        captured: dict = {}

        def factory(*a, compression=True, **k):
            captured["compression"] = compression
            return _FakeTunnel(*a, compression=compression, **k)

        async def ok_mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            return "SECRET_TOK"

        reg = InstancesRegistry(path=tmp_path / "instances.json")

        # ssh_compression=False -> factory receives compression=False
        mgr_off = SshTunnelManager(
            reg, base_port=53400, ssh_compression=False, mint_token=ok_mint, tunnel_factory=factory
        )
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        assert (await mgr_off.connect("cd-1")).state == TunnelState.CONNECTED
        assert captured["compression"] is False

        # default (on) -> factory receives compression=True
        captured.clear()
        mgr_on = SshTunnelManager(reg, base_port=53500, mint_token=ok_mint, tunnel_factory=factory)
        reg.add(name="CD2", ssh_host="cd-2-alias", instance_id="cd-2")
        assert (await mgr_on.connect("cd-2")).state == TunnelState.CONNECTED
        assert captured["compression"] is True


class TestTunnelStatusCarriesTheDialIdentity:
    """The status surfaces the identity the tunnel was ACTUALLY dialled with.

    A credential path binds to this (not to the agent-writable registry row), so
    the fields have to name the node and account the open port reaches and must
    not change for the life of the tunnel.
    """

    def test_an_ssm_tunnel_surfaces_its_node_and_account(self):
        from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel

        tun = _SshTunnel(
            "ssm-1",
            "",
            53410,
            7879,
            transport="ssm",
            ssm_target="mi-0a1b2c3d4e5f67890",
            aws_profile="prof",
            aws_region="us-east-1",
        )
        d = tun.status.to_dict()
        assert d["transport"] == "ssm"
        assert d["ssm_target"] == "mi-0a1b2c3d4e5f67890"
        assert d["aws_profile"] == "prof"
        assert d["aws_region"] == "us-east-1"

    def test_an_ssh_tunnel_carries_no_ssm_coordinates(self):
        from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel

        # Even if an SSH forward is handed SSM-shaped values, the status reports
        # none, so a record requiring an SSM node can never match an SSH tunnel.
        tun = _SshTunnel(
            "ssh-1",
            "host.example",
            53411,
            7879,
            transport="ssh",
            ssm_target="mi-should-be-ignored",
            aws_profile="prof",
            aws_region="us-east-1",
        )
        d = tun.status.to_dict()
        assert d["transport"] == "ssh"
        assert d["ssm_target"] == ""
        assert d["aws_profile"] == ""
        assert d["aws_region"] == ""


class TestSshTunnelWindowsRefusal:
    """On native Windows an ssh-tunnel spawn failure names the platform.

    The surface is documented "not yet" supported on Windows, where the
    ``ssh -N -L`` child needs the OpenSSH client on PATH (not guaranteed). When
    that spawn fails, ``start()`` reports "SSH tunnel is not yet supported on
    Windows" instead of a generic spawn error indistinguishable from "ssh is
    missing", mirroring ``apps/lifecycle_scripts.py``. Crucially it does NOT
    refuse before the spawn: a Windows host whose ``ssh`` IS on PATH still spawns
    and connects, so a tunnel that works today is untouched. SSM/POSIX unchanged.
    """

    @pytest.mark.asyncio
    async def test_ssh_spawn_failure_on_windows_names_the_platform(self, monkeypatch):
        from kiro_crew import platform_compat
        from kiro_crew.instances.ssh_tunnel_manager import (
            _WINDOWS_SSH_TUNNEL_UNSUPPORTED,
            TunnelState,
            _SshTunnel,
        )

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

        # ssh is not on PATH on this Windows host, so the spawn raises OSError —
        # the exact generic-error case the issue reports.
        async def _ssh_missing(*a, **k):
            raise FileNotFoundError("[WinError 2] The system cannot find the file specified")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _ssh_missing)

        tun = _SshTunnel("cd-1", "cd-1-alias", 53400, 7879, transport="ssh")
        ok = await tun.start()

        assert ok is False
        assert tun.status.state == TunnelState.ERROR
        # Names the platform constraint, not just "failed to spawn ssh tunnel".
        assert tun.status.error.startswith(_WINDOWS_SSH_TUNNEL_UNSUPPORTED)
        assert "not yet supported on Windows" in tun.status.error
        assert not tun.status.error.startswith("failed to spawn")

    @pytest.mark.asyncio
    async def test_ssh_tunnel_is_not_refused_before_spawn_on_windows(self, monkeypatch):
        # The fix must NOT close a path that may work: a Windows host whose ssh
        # IS on PATH spawns successfully, so start() reaches the spawn rather than
        # refusing up front. (The spawn is stubbed to raise AFTER being entered,
        # so we only assert the spawn was reached, not the connect outcome.)
        from kiro_crew import platform_compat
        from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

        reached_spawn = False

        async def _spawn(*a, **k):
            nonlocal reached_spawn
            reached_spawn = True
            raise OSError("stop here, after entering the spawn")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)

        tun = _SshTunnel("cd-1", "cd-1-alias", 53400, 7879, transport="ssh")
        await tun.start()

        assert reached_spawn is True, "start() must reach the spawn, not refuse before it"

    @pytest.mark.asyncio
    async def test_ssm_spawn_failure_on_windows_keeps_the_generic_message(self, monkeypatch):
        # The Windows wording is scoped to the ssh transport: an SSM spawn
        # failure on Windows keeps the generic spawn-error message.
        from kiro_crew import platform_compat
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, _SshTunnel

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

        async def _fail(*a, **k):
            raise OSError("boom")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fail)

        tun = _SshTunnel(
            "ssm-1", "", 53410, 7879, transport="ssm", ssm_target="i-0123456789abcdef0"
        )
        ok = await tun.start()

        assert ok is False
        assert tun.status.state == TunnelState.ERROR
        assert tun.status.error.startswith("failed to spawn ssm tunnel")
        assert "Windows" not in tun.status.error

    @pytest.mark.asyncio
    async def test_ssh_spawn_failure_on_posix_keeps_the_generic_message(self, monkeypatch):
        # Control: on POSIX an ssh spawn failure keeps the generic message
        # (behaviour unchanged off Windows).
        from kiro_crew import platform_compat
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, _SshTunnel

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)

        async def _fail(*a, **k):
            raise OSError("boom")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fail)

        tun = _SshTunnel("cd-2", "cd-2-alias", 53420, 7879, transport="ssh")
        ok = await tun.start()

        assert ok is False
        assert tun.status.state == TunnelState.ERROR
        assert tun.status.error.startswith("failed to spawn ssh tunnel")
        assert "Windows" not in tun.status.error


requires_ssh = pytest.mark.skipif(shutil.which("ssh") is None, reason="ssh not available")

#: Hang guard for a ``ssh -G`` config resolution, NOT a performance budget. The call does
#: no network work, so any real duration is process-spawn overhead -- and on a loaded
#: 4-core Windows runner that starves: at 30s this timed out and failed the shard on a
#: config that was correct. Widening the guard cannot weaken an assertion (a genuine hang
#: still fails, a few seconds later); pinning it low turns runner load into a red build.
_SSH_CONFIG_PROBE_TIMEOUT_SECS = 120


def _ssh_effective_config(tmp_path, config_text: str, ssh_args: list[str], host: str) -> dict:
    """Return ssh's OWN resolved settings (``ssh -G``) for *ssh_args* under a config.

    Asks the real ssh binary how it would interpret the production command line,
    rather than asserting on option strings: the point at issue is precedence
    between the command line and ``~/.ssh/config``, which only ssh can answer.

    *ssh_args* is everything between the ``ssh`` binary and the host, flags
    included. Passing the whole thing rather than only the ``-o`` pairs matters:
    some settings resolve differently depending on flags like ``-N``.
    """
    cfg = tmp_path / "ssh_config"
    cfg.write_text(config_text.format(host=host, sock=str(tmp_path / "cm-%r@%h:%p")), "utf-8")
    # ``cwd=tmp_path``: the real binary is the point of this probe (only ssh can
    # answer its own precedence rules), so it stays real -- but it runs pinned to
    # the test's own directory rather than inheriting the worker's checkout cwd.
    out = subprocess.run(
        ["ssh", "-G", "-F", str(cfg), *ssh_args, host],
        capture_output=True,
        text=True,
        timeout=_SSH_CONFIG_PROBE_TIMEOUT_SECS,
        cwd=tmp_path,
    )
    assert out.returncode == 0, f"ssh -G failed: {out.stderr}"
    # Repeated keys are accumulated, not overwritten: ssh prints one
    # ``identityfile`` line per candidate, and how many appear varies by
    # release and by whether the config named one.
    resolved: dict[str, str] = {}
    for line in out.stdout.splitlines():
        key, _, value = line.partition(" ")
        key, value = key.strip().lower(), value.strip()
        resolved[key] = f"{resolved[key]}\n{value}" if key in resolved else value
    return resolved


def _ssh_args(argv: list[str]) -> list[str]:
    """Everything between the ``ssh`` binary and the trailing host."""
    return argv[1:-1]


class TestSshTunnelMultiplexing:
    """The supervised-child contract must survive a user's ssh_config.

    Multiplexing moves the local forward off the child the gateway supervises:
    ssh hands it to an existing shared connection and exits 0. That recreates
    the fork-and-exit shape ``-N`` without ``-f`` exists to avoid, so a tunnel
    that is genuinely serving reports ``ssh exited with code 0`` and is torn
    down.
    """

    _HOST = "kc-test-multiplex-host"

    #: A user config that enables multiplexing for the instance host.
    _ADVERSARIAL_CONFIG = (
        "Host {host}\n"
        "  HostName 127.0.0.1\n"
        "  User probeuser\n"
        "  ControlMaster auto\n"  # wokeignore:rule=master
        "  ControlPath {sock}\n"
        "  ControlPersist 10m\n"
    )

    def test_tunnel_argv_pins_multiplexing_off(self):
        from kiro_crew.instances.ssh_tunnel_manager import _build_ssh_tunnel_argv

        argv = _build_ssh_tunnel_argv("host-a", 7779, 7879)
        assert "ControlPath=none" in argv
        assert "ControlMaster=no" in argv  # wokeignore:rule=master
        # Options, so they precede the -L forward and the positional host.
        assert argv.index("ControlPath=none") < argv.index("-L")
        assert argv.index("ControlMaster=no") < argv.index("-L")  # wokeignore:rule=master
        assert argv[-1] == "host-a"

    @requires_ssh
    def test_user_ssh_config_cannot_re_enable_multiplexing(self, tmp_path):
        """Ask the real ssh how it resolves the production argv, twice.

        The pinned run must end up sharing nothing. The unpinned run over the
        SAME config is the control: it shows ssh honouring the user's settings,
        so the assertions above test the pins rather than restating ssh's
        defaults.
        """
        from kiro_crew.instances.ssh_tunnel_manager import _build_ssh_tunnel_argv

        args = _ssh_args(_build_ssh_tunnel_argv(self._HOST, 7779, 7879))
        pinned = _ssh_effective_config(tmp_path, self._ADVERSARIAL_CONFIG, args, self._HOST)
        assert pinned.get("controlpath") in (None, "none")
        assert pinned.get("controlmaster") in ("no", "false")  # wokeignore:rule=master

        bare = ["-N", "-L", "127.0.0.1:7779:127.0.0.1:7879"]
        unpinned = _ssh_effective_config(tmp_path, self._ADVERSARIAL_CONFIG, bare, self._HOST)
        assert unpinned.get("controlpath") not in (None, "none")
        assert unpinned.get("controlmaster") == "auto"  # wokeignore:rule=master

    @requires_ssh
    def test_pins_do_not_override_a_user_ignoreunknown(self, tmp_path):
        """A pinned `-o` must not displace a directive the user also sets.

        ssh takes the FIRST value obtained for a directive and reads the command
        line before ``~/.ssh/config``, so pinning a single-valued directive here
        silently discards the user's own. ``IgnoreUnknown`` is the one that
        bites: it is how a cross-platform config carries an option this ssh does
        not recognise, and losing it turns a working config into ``Bad
        configuration option`` -- every tunnel then fails where it would
        connect. Multiplexing is safe to pin because a supervised tunnel must
        never share a connection; that reasoning does not generalise.

        The keyword is invented so no OpenSSH release knows it, which keeps the
        result independent of platform and version.
        """
        from kiro_crew.instances.ssh_tunnel_manager import _build_ssh_tunnel_argv

        cfg = tmp_path / "ssh_config"
        cfg.write_text(
            "IgnoreUnknown UserPrivateOption\n"
            "UserPrivateOption yes\n"
            "\n"
            f"Host {self._HOST}\n"
            "  HostName 127.0.0.1\n",
            "utf-8",
        )
        args = _ssh_args(_build_ssh_tunnel_argv(self._HOST, 7779, 7879))
        out = subprocess.run(
            ["ssh", "-G", "-F", str(cfg), *args, self._HOST],
            capture_output=True,
            text=True,
            timeout=_SSH_CONFIG_PROBE_TIMEOUT_SECS,
            cwd=tmp_path,
        )
        assert out.returncode == 0, f"production argv broke a working config: {out.stderr}"
        assert "bad configuration option" not in out.stderr.lower()

    @requires_ssh
    def test_per_host_ssh_config_is_still_inherited(self, tmp_path):
        """Only process ownership is overridden; connection coordinates are not.

        The registry carries no inline `-i`/`-p`/`-J` fields and relies on the
        ssh-config alias path for identity, port, and bastion reachability, so
        pinning must not turn the argv into a general ssh_config override.
        """
        from kiro_crew.instances.ssh_tunnel_manager import _build_ssh_tunnel_argv

        config = (
            self._ADVERSARIAL_CONFIG
            + "  Port 2222\n"
            + "  IdentityFile ~/.ssh/some-key.pem\n"
            + "  ProxyCommand /bin/true %h %p\n"
        )
        args = _ssh_args(_build_ssh_tunnel_argv(self._HOST, 7779, 7879))
        resolved = _ssh_effective_config(tmp_path, config, args, self._HOST)

        assert resolved.get("hostname") == "127.0.0.1"
        assert resolved.get("user") == "probeuser"
        assert resolved.get("port") == "2222"
        assert "some-key.pem" in resolved.get("identityfile", "")
        assert resolved.get("proxycommand", "").startswith("/bin/true")
        # The argv's own pins are still in force alongside the inherited values.
        assert resolved.get("batchmode") == "yes"
        assert resolved.get("exitonforwardfailure") == "yes"
        assert resolved.get("addressfamily") == "inet"


class TestSshTunnelManager:
    @pytest.fixture(autouse=True)
    def _free_ports(self, monkeypatch):
        # Connect now probes _is_port_free (CSE SEC-016 mirror conflict check).
        # Keep these unit tests hermetic / independent of the host's real ports.
        _patch_port_probe(monkeypatch)

    def _mgr(self, tmp_path, *, mint=None, factory=_FakeTunnel):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        reg = InstancesRegistry(path=tmp_path / "instances.json")

        async def ok_mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            return "SECRET_TOK"

        return reg, SshTunnelManager(
            reg, base_port=53400, mint_token=mint or ok_mint, tunnel_factory=factory
        )

    @pytest.mark.asyncio
    async def test_connect_persists_and_idempotent(self, tmp_path):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        st = await mgr.connect("cd-1")
        assert st.state == TunnelState.CONNECTED
        assert mgr.get_token("cd-1") == "SECRET_TOK"
        inst = reg.get("cd-1")
        # local_port is ALLOCATED from the tunnel base, not mirrored onto
        # remote_port.
        assert inst.was_connected is True
        assert inst.local_port >= mgr._allocator.base_port
        assert inst.local_port != inst.remote_port
        assert reg.get_last_active().id == "cd-1"
        # idempotent
        assert (await mgr.connect("cd-1")).state == TunnelState.CONNECTED

    @pytest.mark.asyncio
    async def test_reconnect_prefers_recorded_port_for_origin_stability(self, tmp_path):
        """A crew keeps its recorded loopback port across a gateway restart.

        The pane is an iframe served at ``http://<host>:<local_port>``, so the
        loopback port IS its browser origin, and origin-keyed client state —
        ``localStorage`` UI preferences (mc-chat-config, pinLastPrompt,
        hideEmptyFolderBody, ...) — is reset whenever that origin moves. The
        recorded ``local_port`` survives a restart (``shutdown`` keeps registry
        hints), so connect() must PREFER it: a first-free allocation lets a crew
        land on a DIFFERENT port after the restart whenever another instance
        holds the lower port, moving the origin and the settings behind it. The
        preference keeps both put.
        """
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager, TunnelState

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="A", ssh_host="a-alias", instance_id="a")
        reg.add(name="B", ssh_host="b-alias", instance_id="b")

        # First boot: A takes base, B takes base+1 (B's connect excludes A's
        # recorded port via the registry).
        base = mgr._allocator.base_port
        assert (await mgr.connect("a")).local_port == base
        assert (await mgr.connect("b")).local_port == base + 1

        # Restart: a fresh manager over the SAME registry, which still holds both
        # recorded ports. Reconnect B FIRST — the order that, under first-free,
        # would hand B the now-vacant lower port (base) and move its origin.
        mgr2 = SshTunnelManager(
            reg, base_port=base, mint_token=mgr._mint_token, tunnel_factory=_FakeTunnel
        )
        st_b = await mgr2.connect("b")
        assert st_b.state == TunnelState.CONNECTED
        assert st_b.local_port == base + 1, "B must keep its recorded port, not slide to base"
        assert reg.get("b").local_port == base + 1
        # A then reconnects onto its own recorded port too — no collision.
        st_a = await mgr2.connect("a")
        assert st_a.local_port == base
        assert reg.get("a").local_port == base

    @pytest.mark.asyncio
    async def test_connect_rebuild_replaces_a_connected_tunnel(self, tmp_path):
        """``rebuild=True`` is the pane's Retry after a watchdog verdict on a
        document that DID navigate: every probe says the tunnel is fine, so the
        idempotent connect would hand the same (stalled) forwarder back. The
        rebuild must stop the old child, spawn a new one, keep the user's
        connect intent, and answer CONNECTED with a live token."""
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        first = await mgr.connect("cd-1")
        assert first.state == TunnelState.CONNECTED
        old_tunnel = mgr._tunnels["cd-1"]

        second = await mgr.connect("cd-1", rebuild=True)
        assert second.state == TunnelState.CONNECTED
        assert mgr._tunnels["cd-1"] is not old_tunnel, "a rebuild must spawn a new forwarder"
        assert old_tunnel.stopped, "the stalled forwarder must be stopped, not orphaned"
        assert mgr.get_token("cd-1") == "SECRET_TOK"
        inst = reg.get("cd-1")
        assert inst.was_connected is True, "rebuild keeps the connect intent (keep_intent)"
        assert inst.local_port == second.local_port
        assert inst.local_port >= mgr._allocator.base_port
        # The freed port is excluded from the re-allocation: a cause bound to the
        # old port (not just a stalled stream on the old forwarder) is escaped too.
        assert second.local_port != first.local_port, "rebuild must not re-use the port it freed"
        # A plain connect afterwards is idempotent on the NEW tunnel.
        assert (await mgr.connect("cd-1")).state == TunnelState.CONNECTED
        assert mgr._tunnels["cd-1"] is not old_tunnel

    @pytest.mark.asyncio
    async def test_connect_rebuild_teardown_failure_is_an_error_status(self, tmp_path):
        """A stop that raises during rebuild must NOT propagate out of connect()
        (the handler would turn it into an unexplained 500). It is reported like
        every other connect failure: ERROR status with the reason, retained for
        last_error, and the old tunnel is left tracked and intact — nothing is
        removed unless the stop succeeded."""
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        first = await mgr.connect("cd-1")
        assert first.state == TunnelState.CONNECTED
        old_tunnel = mgr._tunnels["cd-1"]

        async def boom():
            raise RuntimeError("kill failed: EPERM")

        old_tunnel.stop = boom

        st = await mgr.connect("cd-1", rebuild=True)
        assert st.state == TunnelState.ERROR
        assert "EPERM" in (st.error or "")
        assert "EPERM" in (mgr.last_error("cd-1") or "")
        # The live tunnel is untouched: still tracked, credential still held.
        assert mgr._tunnels["cd-1"] is old_tunnel
        assert mgr.get_token("cd-1") == "SECRET_TOK"
        assert reg.get("cd-1").was_connected is True

    @pytest.mark.asyncio
    async def test_connect_only_if_connected_declines_without_side_effects(self, tmp_path):
        """The viewport's auto-warm must never bring a tunnel up or touch the
        connect intent: for an absent tunnel it answers DISCONNECTED, spawns
        nothing, mints nothing, and leaves was_connected exactly as the user
        last set it (here: cleared by an explicit disconnect)."""
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        # Never connected.
        st = await mgr.connect("cd-1", only_if_connected=True)
        assert st.state == TunnelState.DISCONNECTED
        assert "cd-1" not in mgr._tunnels
        assert not mgr.get_token("cd-1")
        assert reg.get("cd-1").was_connected is False

        # Connected, then explicitly disconnected: the race auto-warm loses.
        assert (await mgr.connect("cd-1")).state == TunnelState.CONNECTED
        await mgr.disconnect("cd-1")
        assert reg.get("cd-1").was_connected is False
        st = await mgr.connect("cd-1", only_if_connected=True)
        assert st.state == TunnelState.DISCONNECTED
        assert (
            "cd-1" not in mgr._tunnels
        ), "a connected-only connect must not re-open a closed tunnel"
        assert (
            reg.get("cd-1").was_connected is False
        ), "a connected-only connect must not re-persist intent"

    @pytest.mark.asyncio
    async def test_connect_only_if_connected_answers_a_live_tunnel_like_plain_connect(
        self, tmp_path
    ):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        first = await mgr.connect("cd-1")
        tunnel = mgr._tunnels["cd-1"]
        st = await mgr.connect("cd-1", only_if_connected=True)
        assert st.state == TunnelState.CONNECTED
        assert st.local_port == first.local_port
        assert mgr._tunnels["cd-1"] is tunnel, "idempotent on the live tunnel"
        assert mgr.get_token("cd-1") == "SECRET_TOK"

    @pytest.mark.asyncio
    async def test_connect_rebuild_and_only_if_connected_are_exclusive(self, tmp_path):
        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        with pytest.raises(ValueError):
            await mgr.connect("cd-1", rebuild=True, only_if_connected=True)

    @pytest.mark.asyncio
    async def test_connect_rebuild_with_no_tunnel_is_a_plain_connect(self, tmp_path):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        st = await mgr.connect("cd-1", rebuild=True)
        assert st.state == TunnelState.CONNECTED
        assert mgr.get_token("cd-1") == "SECRET_TOK"

    @pytest.mark.asyncio
    async def test_connect_resets_recover_attempts(self, tmp_path):
        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        # Simulate a prior give-up that left the counter past the cap.
        mgr._recover_attempts["cd-1"] = 99
        await mgr.connect("cd-1")
        # A successful (re)connect clears the stale give-up counter so the next
        # unexpected drop gets a full fresh recovery budget.
        assert "cd-1" not in mgr._recover_attempts

    def test_unknown_instance_raises(self, tmp_path):
        _reg, mgr = self._mgr(tmp_path)
        with pytest.raises(KeyError):
            asyncio.run(mgr.connect("ghost"))

    def test_validation_failure(self, tmp_path):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="Bad", ssh_host="-obadhost", instance_id="bad")  # registry ok; manager rejects
        st = asyncio.run(mgr.connect("bad"))
        assert st.state == TunnelState.ERROR and "invalid ssh settings" in st.error
        assert mgr.status("bad") is None and mgr.get_token("bad") == ""

    def test_tunnel_failure(self, tmp_path):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        def failing(*a, **k):
            t = _FakeTunnel(*a, **k)
            t.start_result = False
            return t

        reg, mgr = self._mgr(tmp_path, factory=failing)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        st = asyncio.run(mgr.connect("cd-1"))
        assert st.state == TunnelState.ERROR and mgr.get_token("cd-1") == ""

    def test_mint_failure_tears_down(self, tmp_path):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState
        from kiro_crew.instances.token_mint import TokenMintError

        async def bad_mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            raise TokenMintError("nope")

        reg, mgr = self._mgr(tmp_path, mint=bad_mint)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        st = asyncio.run(mgr.connect("cd-1"))
        assert st.state == TunnelState.ERROR and "token mint failed" in st.error
        assert mgr.status("cd-1") is None

    @pytest.mark.asyncio
    async def test_disconnect_and_shutdown(self, tmp_path):
        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        await mgr.connect("cd-1")
        assert await mgr.disconnect("cd-1") is True
        assert reg.get("cd-1").was_connected is False
        assert mgr.get_token("cd-1") == ""
        # shutdown preserves registry hints for lazy reconnect
        await mgr.connect("cd-1")
        await mgr.shutdown()
        assert mgr.status_all() == {}
        assert reg.get("cd-1").was_connected is True

    @pytest.mark.asyncio
    async def test_disconnect_clears_local_port(self, tmp_path):
        # Regression: connect() records the allocated local_port, but
        # disconnect() must reset it to the unallocated sentinel. Otherwise the
        # freed port stays recorded and reads as reserved forever.
        from kiro_crew.instances.registry import _UNALLOCATED_PORT

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")

        await mgr.connect("cd-1")
        allocated = reg.get("cd-1").local_port
        assert allocated >= mgr._allocator.base_port

        await mgr.disconnect("cd-1")
        inst = reg.get("cd-1")
        assert inst.local_port == _UNALLOCATED_PORT  # port hint cleared
        assert inst.was_connected is False
        # the cleared port is not counted as reserved
        assert allocated not in mgr._reserved_ports()

    @pytest.mark.asyncio
    async def test_disconnect_clears_stale_port_without_live_tunnel(self, tmp_path):
        # A port left recorded by an unclean prior exit (no live tunnel tracked)
        # must still be clearable via disconnect, so the user can recover.
        from kiro_crew.instances.registry import _UNALLOCATED_PORT

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        reg.update("cd-1", local_port=7777, was_connected=True)  # simulate stale hint

        assert await mgr.disconnect("cd-1") is False  # no live tunnel existed
        inst = reg.get("cd-1")
        assert inst.local_port == _UNALLOCATED_PORT and inst.was_connected is False

    @pytest.mark.asyncio
    async def test_token_validates_status_mapping(self, tmp_path, monkeypatch):
        from kiro_crew.instances import ssh_tunnel_manager as m

        class _Resp:
            def __init__(self, status):
                self.status = status

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _Sess:
            status = 200

            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def get(self, url, params=None):
                return _Resp(_Sess.status)

        _reg, mgr = self._mgr(tmp_path)
        monkeypatch.setattr(m.aiohttp, "ClientSession", _Sess)
        _Sess.status = 200
        assert await mgr.token_validates(7778, "TOK") is True  # 2xx => valid
        _Sess.status = 403
        assert await mgr.token_validates(7778, "TOK") is False  # remote rejected => stale
        _Sess.status = 500
        assert await mgr.token_validates(7778, "TOK") is False  # non-2xx => not confirmed
        # missing token / unknown port => re-mint needed (no probe)
        assert await mgr.token_validates(7778, "") is False
        assert await mgr.token_validates(0, "TOK") is False

    @pytest.mark.asyncio
    async def test_token_validates_denies_on_error(self, tmp_path, monkeypatch):
        from kiro_crew.instances import ssh_tunnel_manager as m

        class _BoomSess:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def get(self, *a, **k):
                raise asyncio.TimeoutError()

        _reg, mgr = self._mgr(tmp_path)
        monkeypatch.setattr(m.aiohttp, "ClientSession", _BoomSess)
        # Probe inconclusive (timeout) => deny-by-default (force a re-mint).
        assert await mgr.token_validates(7778, "TOK") is False
