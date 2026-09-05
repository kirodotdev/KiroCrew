"""Tests for the Instances (multi-instance management) feature.

Covers the Phase 1 backend: config flag/constants, registry CRUD + hints,
PortAllocator, token-mint helper (parse/ttl/command-build/mocked ssh, no token
in logs), injection-safe validation, SshTunnelManager (mocked subprocess via an
injected tunnel factory + mint), and the owner-only API handlers (enabled
gating, Slack-origin rejection, CRUD, token-not-leaked).

Async paths are driven through ``asyncio.run`` from sync test functions so the
suite needs no asyncio pytest plugin.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from kiro_crew import platform_compat

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

    def test_is_port_free_detects_live_listener_even_with_reuseaddr(self):
        """A genuinely LISTENing port is still reported in-use.

        Regression guard for the disconnect->reconnect fix: `_is_port_free`
        now sets SO_REUSEADDR (so a just-freed port lingering in TIME_WAIT is
        not a false positive, matching ssh's own `-L` listener bind). This must
        NOT relax detection of a real, live listener — a true two-instance port
        collision still has to be caught. SO_REUSEADDR exempts TIME_WAIT only,
        never an active LISTEN, so the probe (also SO_REUSEADDR) must still fail
        to bind against a LISTENing socket that itself set SO_REUSEADDR.
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
        real = tm.redact_credentials
        monkeypatch.setattr(
            tm, "redact_credentials", lambda text, *a, **k: seen.append(len(text)) or real(text)
        )

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

        # The preferred producer implements Linux and macOS only. When it says
        # nothing, the fallback is consulted rather than the token going empty --
        # that fallback is the ONLY start identity available on Windows (a
        # creation FILETIME), so skipping it would make a pod there permanently
        # unprovable.
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


# ── SshTunnelManager (mocked) ─────────────────────────────────────────────────


def _patch_port_probe(monkeypatch, *, manager_free: bool = True, allocator_free: bool = True):
    """Make loopback port probing deterministic in BOTH namespaces that probe.

    The connect path probes twice, and each site resolves ``_is_port_free`` from
    its own module, so patching one leaves the other binding real sockets:

    * ``PortAllocator.allocate`` resolves the name in ``port_allocator``. Left
      real, allocation walks upward past whatever the host happens to be
      holding, so which port an instance is handed depends on host state — and
      a test asserting an exact port passes only where the base port is free.
    * ``ssh_tunnel_manager`` holds its own re-bound reference for the advisory
      re-probe it runs on the port it just allocated.

    The two knobs are separate because the answers legitimately differ: the
    conflict case models a TOCTOU loss, where allocation succeeds and the
    re-probe then finds that port taken.
    """
    import kiro_crew.instances.port_allocator as pa
    import kiro_crew.instances.ssh_tunnel_manager as stm

    monkeypatch.setattr(stm, "_is_port_free", lambda port, host="127.0.0.1": manager_free)
    monkeypatch.setattr(pa, "_is_port_free", lambda port, host="127.0.0.1": allocator_free)


class _FakeTunnel:
    def __init__(
        self,
        iid,
        ssh_host,
        lp,
        rp,
        *,
        connect_timeout_secs=0,
        compression=True,
        probe_failure_threshold=0,
        on_exit=None,
        transport="ssh",
        ssm_target="",
        aws_profile="",
        aws_region="",
    ):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        self.iid = iid
        self.stopped = False
        self.start_result = True
        self._S = TunnelState
        # Mirrors _SshTunnel.pid (None when no live child). connect() persists
        # `tunnel.pid or 0` as the forwarder_pid hint; tests that assert a
        # recorded pid set this to a concrete value via a factory wrapper.
        self.pid = None
        # Recorded so transport-selection tests can assert which transport the
        # manager chose for this instance.
        self.transport = transport
        self.ssm_target = ssm_target
        self.aws_profile = aws_profile
        self.aws_region = aws_region
        self.ssh_host = ssh_host
        self.connect_timeout_secs = connect_timeout_secs
        self.status = TunnelStatus(instance_id=iid, local_port=lp, remote_port=rp)

    async def start(self):
        self.status.state = self._S.CONNECTED if self.start_result else self._S.ERROR
        if not self.start_result:
            self.status.error = "boom"
        return self.start_result

    async def stop(self):
        self.stopped = True
        self.status.state = self._S.STOPPED


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
    out = subprocess.run(
        ["ssh", "-G", "-F", str(cfg), *ssh_args, host],
        capture_output=True,
        text=True,
        timeout=_SSH_CONFIG_PROBE_TIMEOUT_SECS,
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
    _ADVERSARIAL_CONFIG = """\
Host {host}
  HostName 127.0.0.1
  User probeuser
  ControlMaster auto  # wokeignore:rule=master
  ControlPath {sock}
  ControlPersist 10m
"""

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
        configuration option`` -- every tunnel then fails where it used to
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
        # remote_port (#1972).
        assert inst.was_connected is True
        assert inst.local_port >= mgr._allocator.base_port
        assert inst.local_port != inst.remote_port
        assert reg.get_last_active().id == "cd-1"
        # idempotent
        assert (await mgr.connect("cd-1")).state == TunnelState.CONNECTED

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
        # the cleared port is no longer counted as reserved
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


# ── API handlers ──────────────────────────────────────────────────────────────


class _FakeReq:
    def __init__(self, state, *, headers=None, match=None, body=None, query=None, user="owner"):
        self.app = {"state": state}
        self.headers = headers or {}
        self.match_info = match or {}
        self.query = query or {}
        self._body = body
        # Mirrors aiohttp Request mapping: require_auth sets request["user"].
        self._attrs = {"user": user} if user is not None else {}

    def get(self, key, default=None):
        return self._attrs.get(key, default)

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _fake_reconfigure(mgr, keep_intent=True):
    """Mirror SshTunnelManager.reconfigure on a stub: teardown then persist.

    The real method holds the manager lock across both steps; a stub cannot model
    the lock, so it models the ORDER, which is what the handler depends on.
    """

    async def reconfigure(instance_id, apply):
        try:
            await mgr.disconnect(instance_id, keep_intent=keep_intent)
        except Exception:
            pass  # the real method logs and persists anyway
        return apply()

    return reconfigure


class _State:
    def __init__(self, registry, manager=None):
        self.instances_registry = registry
        self.instances_manager = manager


class _ConnectedMgr:
    """Manager stub where the named instances report a live tunnel.

    Only the three members ``_status_for`` touches are implemented, which is
    what the warm-set-cap tests need: the cap is derived from how many instances
    come back ``connected``.
    """

    def __init__(self, connected):
        self._connected = set(connected)

    def status(self, instance_id):
        if instance_id not in self._connected:
            return None
        return types.SimpleNamespace(
            to_dict=lambda: {"instance_id": instance_id, "state": "connected"}
        )

    def token_ttl_remaining(self, instance_id):
        return None

    def last_error(self, instance_id):
        return None


def _enable(tmp_path: Path, monkeypatch, *, enabled=True, warm_set_cap=None):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    section: dict = {"enabled": enabled}
    if warm_set_cap is not None:
        section["warm_set_cap"] = warm_set_cap
    (tmp_path / "config.json").write_text(json.dumps({"instances": section}))
    from kiro_crew.config import loader

    loader._invalidate_config_cache()


def _body(resp):
    return json.loads(resp.body.decode())


class TestValidatorsRejectEmbeddedNewlines:
    """Every anchored validator in this package must use ``\\Z``, not ``$``.

    Python's ``$`` also matches just BEFORE a trailing newline, so a value like
    ``"20h\\n"`` passes a ``$``-anchored check and then reaches an ssh/ssm
    argument list carrying an embedded newline. Every regex here guards a value
    that ends up on such a command line, so this is one bug class rather than one
    regex — a ratchet is the only thing that keeps a future edit from
    reintroducing it.
    """

    def test_no_anchored_pattern_uses_a_dollar_anchor(self):
        import re as _re

        offenders = []
        for mod in ("registry", "validation", "constants"):
            path = (
                Path(__file__).resolve().parents[1]
                / "src"
                / "kiro_crew"
                / "instances"
                / f"{mod}.py"
            )
            for line in path.read_text().splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # An anchored pattern literal that still ends a branch with `$`.
                if _re.search(r'r"\^[^"]*\$(?:\|\^\$)?"', stripped):
                    offenders.append(f"{mod}.py: {stripped}")
        assert not offenders, (
            "anchored with `$`, which also matches before a trailing newline; "
            "use `\\Z`:\n  " + "\n  ".join(offenders)
        )

    def test_a_trailing_newline_never_survives_validation(self, tmp_path):
        """Two safe answers, and every guard must give one of them.

        ``validation.py`` SANITIZES — it strips before matching and returns the
        cleaned value, so a newline cannot reach the argument list it guards. The
        registry and the ttl check REJECT, because they persist what they are
        given. What must not happen is a guard accepting the value and passing the
        newline through unchanged.
        """
        from kiro_crew.instances import validation
        from kiro_crew.instances.registry import (
            InstancesRegistry,
            InvalidInstanceError,
            validate_ttl,
        )

        for fn, dirty in (
            (validation.validate_ssh_host, "host\n"),
            (validation.validate_remote_bin, "/usr/bin/kirocrew\n"),
            (validation.validate_ssm_target, "i-0123456789abcdef0\n"),
            (validation.validate_ssm_run_as, "ec2-user\n"),
            (validation.validate_aws_profile, "Admin\n"),
            (validation.validate_aws_region, "us-west-2\n"),
        ):
            cleaned = fn(dirty)
            assert "\n" not in cleaned, f"{fn.__name__} passed a newline through"

        # The persisting layers refuse outright rather than silently rewriting.
        with pytest.raises(InvalidInstanceError):
            validate_ttl("20h\n")
        reg = InstancesRegistry(tmp_path / "instances.json")
        with pytest.raises(InvalidInstanceError):
            reg.add(name="CD", ssh_host="cd-1-alias\n", instance_id="cd-1")
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        with pytest.raises(InvalidInstanceError):
            reg.update("cd-1", ttl="20h\n")


class TestReconfigureAtomicity:
    """`reconfigure` must serialize against everything else taking the lock."""

    def _manager(self, tmp_path):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        reg = InstancesRegistry(tmp_path / "instances.json")
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        return SshTunnelManager(reg), reg

    def test_reconfigure_holds_the_lock_across_teardown_and_persist(self, tmp_path):
        """`connect` takes the same lock, so a reconfiguration in progress must
        block it — that mutual exclusion is what removes the window where a
        connect could read the pre-edit coordinates."""
        mgr, _reg = self._manager(tmp_path)
        applied: list[str] = []

        async def scenario():
            # Stand in for a connect that already holds the lock.
            async with mgr._lock:
                task = asyncio.create_task(
                    mgr.reconfigure("cd-1", lambda: applied.append("persisted"))
                )
                # Yield generously: while the lock is held, nothing may persist.
                for _ in range(5):
                    await asyncio.sleep(0)
                assert applied == [], "reconfigure wrote without holding the lock"
            await task
            assert applied == ["persisted"]

        asyncio.run(scenario())

    def test_reconfigure_cancels_this_instances_in_flight_recovery(self, tmp_path):
        """Self-heal reads the record BEFORE it takes the lock, so a recovery
        already in flight carries the pre-edit coordinates. It must be cancelled
        and awaited, or it reinstalls a tunnel to the old machine after the edit —
        and `connect()` being idempotent would then hand that tunnel out for the
        new settings. Another instance's recovery must be left alone."""
        mgr, _reg = self._manager(tmp_path)
        started = asyncio.Event()
        outcome: list[str] = []

        async def scenario():
            async def stale_recovery():
                started.set()
                try:
                    await asyncio.sleep(30)
                    outcome.append("reinstalled-old-coordinates")
                except asyncio.CancelledError:
                    outcome.append("cancelled")
                    raise

            async def other_instance_recovery():
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    outcome.append("other-cancelled")
                    raise

            mgr._track_recovery("cd-1", asyncio.create_task(stale_recovery()))
            other = asyncio.create_task(other_instance_recovery())
            mgr._track_recovery("cd-2", other)
            await started.wait()

            await mgr.reconfigure("cd-1", lambda: outcome.append("persisted"))
            # The stale recovery is finished (not merely signalled) before the
            # coordinates are written.
            assert outcome == ["cancelled", "persisted"], outcome
            assert not other.done(), "another instance's recovery was cancelled"
            other.cancel()
            await asyncio.gather(other, return_exceptions=True)

        asyncio.run(scenario())

    def test_an_in_flight_token_mint_cannot_outlive_the_reconfiguration(self, tmp_path):
        """Both background writers must be stopped AND unwound before the move.

        Self-heal rebuilds a tunnel and token refresh mints a credential, each from
        the record it read. A refresh left running would finish after the edit and
        store a token minted from the pre-edit coordinates against the rebuilt
        tunnel — the embedded dashboard would be handed a credential the new remote
        never issued. Signalling a cancel is not enough; it has to be awaited.
        """
        mgr, _reg = self._manager(tmp_path)
        events: list[str] = []

        async def slow_mint():
            try:
                await asyncio.sleep(30)
                events.append("stored-stale-token")
            except asyncio.CancelledError:
                events.append("refresh-unwound")
                raise

        async def scenario():
            mgr._refresh_tasks["cd-1"] = asyncio.create_task(slow_mint())
            await asyncio.sleep(0)  # let it reach its await

            await mgr.reconfigure("cd-1", lambda: events.append("persisted"))

            # Unwound BEFORE the write, not merely signalled.
            assert events == ["refresh-unwound", "persisted"], events
            assert "cd-1" not in mgr._refresh_tasks
            # A refresh cannot be restarted while the barrier is up either.
            mgr._reconfiguring.add("cd-1")
            mgr._schedule_token_refresh("cd-1")
            assert "cd-1" not in mgr._refresh_tasks
            mgr._reconfiguring.discard("cd-1")

        asyncio.run(scenario())

    def test_a_recovery_scheduled_mid_reconfigure_is_refused(self, tmp_path):
        """Cancelling the recoveries in flight is not enough on its own.

        Self-heal reads the record before it takes the lock, and the cancellation
        itself awaits — so a tunnel exiting during that await schedules a FRESH
        recovery holding pre-edit coordinates. The barrier raised at the start of
        a reconfiguration is what makes that new attempt refuse to run. This drives
        the exact window: the tunnel dies while the cancellation is in flight.
        """
        mgr, _reg = self._manager(tmp_path)
        applied: list[str] = []
        original = mgr._cancel_recovery

        async def cancel_then_the_tunnel_dies(instance_id: str) -> None:
            await original(instance_id)
            # Barrier is up, lock not yet taken: the scheduling seam must refuse.
            mgr._on_tunnel_exit(instance_id)

        mgr._cancel_recovery = cancel_then_the_tunnel_dies  # type: ignore[method-assign]

        async def scenario():
            await mgr.reconfigure("cd-1", lambda: applied.append("persisted"))
            assert applied == ["persisted"]
            # No recovery was scheduled for this instance while the barrier held.
            assert not mgr._recovery_by_instance.get("cd-1")
            # And the barrier is down afterwards, so normal self-heal resumes.
            assert "cd-1" not in mgr._reconfiguring

        asyncio.run(scenario())

    def test_reconfigure_aborts_and_keeps_the_tunnel_when_stop_fails(self, tmp_path):
        """A failed stop must not orphan the forward or advance the record.

        Nothing is discarded unless the stop succeeded: the tunnel keeps its place
        in ``_tunnels`` AND its token and refresh task, because a live forward with
        no credential is not usable (session transfer reports
        ``transfer_no_credential``). Nothing is persisted either.
        """
        mgr, _reg = self._manager(tmp_path)
        applied: list[str] = []

        class _StubbornTunnel:
            async def stop(self):
                raise OSError("ssh process will not die")

        mgr._tunnels["cd-1"] = _StubbornTunnel()  # type: ignore[assignment]
        mgr._tokens["cd-1"] = "live-token"

        async def idle_refresh():
            await asyncio.sleep(30)

        async def scenario():
            mgr._refresh_tasks["cd-1"] = asyncio.create_task(idle_refresh())
            await asyncio.sleep(0)

            with pytest.raises(OSError):
                await mgr.reconfigure("cd-1", lambda: applied.append("persisted"))
            assert applied == [], "coordinates were written over a live tunnel"
            assert "cd-1" in mgr._tunnels, "the forward was left untracked"
            assert mgr._tokens.get("cd-1") == "live-token", "a live tunnel lost its token"
            # The refresh loop keeps the live tunnel's credential fresh, so a
            # REJECTED edit must not have torn it down either.
            task = mgr._refresh_tasks.get("cd-1")
            assert task is not None and not task.done(), "a live tunnel lost its refresh"
            assert "cd-1" not in mgr._reconfiguring, "barrier not cleared"
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        asyncio.run(scenario())

    def test_a_cancelled_request_does_not_release_the_lock_mid_write(self, tmp_path):
        """Cancelling the caller must not open the critical section.

        The registry write runs on a worker thread. If the awaiting task is
        cancelled (the client hung up), an unshielded await would unwind the
        ``async with`` and free the lock while that write was still in flight — a
        concurrent connect could then read the pre-edit coordinates. The write is
        awaited out under the lock, and only then does the cancellation land.
        """
        mgr, _reg = self._manager(tmp_path)
        started = threading.Event()
        finished = threading.Event()

        def slow_write():
            started.set()
            time.sleep(0.3)
            finished.set()
            return "persisted"

        async def scenario():
            task = asyncio.create_task(mgr.reconfigure("cd-1", slow_write))
            await asyncio.to_thread(started.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # The write ran to completion before the lock was surrendered.
            assert finished.is_set(), "the write was abandoned mid-flight"
            assert not mgr._lock.locked(), "the lock was not released"

        asyncio.run(scenario())


class TestHandlers:
    def _reg(self, tmp_path):
        from kiro_crew.instances.registry import InstancesRegistry

        return InstancesRegistry(path=tmp_path / "instances.json")

    def test_disabled_returns_403(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch, enabled=False)
        r = asyncio.run(handlers.api_instances_list(_FakeReq(_State(self._reg(tmp_path)))))
        assert r.status == 403 and "disabled" in _body(r)["error"]

    def test_slack_origin_rejected(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        req = _FakeReq(_State(self._reg(tmp_path)), headers={"X-Session-Key": "slack:T:C"})
        r = asyncio.run(handlers.api_instances_list(req))
        assert r.status == 403 and "owner-only" in _body(r)["error"]

    def test_unauthenticated_rejected(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        # No authenticated user (require_auth would have set request["user"]).
        req = _FakeReq(_State(self._reg(tmp_path)), user=None)
        r = asyncio.run(handlers.api_instances_list(req))
        assert r.status == 401 and "authentication required" in _body(r)["error"]

    def test_add_list_includes_cap(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        state = _State(reg)
        r = asyncio.run(
            handlers.api_instances_add(
                _FakeReq(state, body={"name": "CD", "ssh_host": "cd-1-alias"})
            )
        )
        assert r.status == 201
        r = asyncio.run(handlers.api_instances_list(_FakeReq(state)))
        b = _body(r)
        # One crew registered => automatic cap 1, and adding it is enough: the
        # cap does not wait for the tunnel to come up.
        assert b["warm_set_cap"] == 1 and len(b["instances"]) == 1
        # no manager on this state => enabled-in-config but not active (needs restart)
        assert b["active"] is False

    def test_automatic_cap_covers_every_registered_crew_not_just_connected_ones(
        self, tmp_path, monkeypatch
    ):
        """The served cap covers every REGISTERED crew, connected or not.

        This is the regression that produced "one random crew is broken". The cap
        used to be the live connected count, so a crew whose tunnel came up just
        after this request was not counted, the cap arrived one short, and the
        viewport evicted a pane to honour it. Eviction unmounts the pane and
        cold-boots the remote SPA on the next click, which reads as a disconnect —
        and which crew lost depended on connection order, so the victim moved on
        every restart. Here 3 are registered and only 2 are connected; the cap
        must still be 3.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        for name in ("a", "b", "c"):
            reg.add(name=name, ssh_host=f"{name}-alias")
        state = _State(reg, _ConnectedMgr(["a", "c"]))
        b = _body(asyncio.run(handlers.api_instances_list(_FakeReq(state))))
        assert len(b["instances"]) == 3
        assert b["warm_set_cap"] == 3

    def test_adding_a_crew_widens_the_served_cap(self, tmp_path, monkeypatch):
        """Otherwise every new crew has to be paired with a config edit.

        Forgetting that edit reintroduces the eviction above, so the cap has to
        rise on its own when the fleet grows.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="a", ssh_host="a-alias")
        state = _State(reg, _ConnectedMgr([]))
        before = _body(asyncio.run(handlers.api_instances_list(_FakeReq(state))))["warm_set_cap"]
        reg.add(name="b", ssh_host="b-alias")
        after = _body(asyncio.run(handlers.api_instances_list(_FakeReq(state))))["warm_set_cap"]
        assert (before, after) == (1, 2)

    def test_explicit_cap_is_served_even_below_the_registered_count(self, tmp_path, monkeypatch):
        """An operator's own number is the budget and is not widened for them."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch, warm_set_cap=1)
        reg = self._reg(tmp_path)
        for name in ("a", "b"):
            reg.add(name=name, ssh_host=f"{name}-alias")
        state = _State(reg, _ConnectedMgr(["a", "b"]))
        assert _body(asyncio.run(handlers.api_instances_list(_FakeReq(state))))["warm_set_cap"] == 1

    def test_list_active_reflects_manager_running(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        # manager present => active True; absent => active False
        r = asyncio.run(handlers.api_instances_list(_FakeReq(_State(reg, object()))))
        assert _body(r)["active"] is True
        r = asyncio.run(handlers.api_instances_list(_FakeReq(_State(reg))))
        assert _body(r)["active"] is False

    def test_add_invalid_ssh_host_400(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        r = asyncio.run(
            handlers.api_instances_add(
                _FakeReq(_State(self._reg(tmp_path)), body={"name": "x", "ssh_host": "bad host;rm"})
            )
        )
        assert r.status == 400

    def test_connect_returns_token_but_list_does_not_leak(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        class FakeMgr:
            def __init__(self):
                self._tok = {}

            async def connect(self, iid):
                self._tok[iid] = "SECRET_TOK"
                reg.update(iid, was_connected=True, local_port=7778)
                return TunnelStatus(iid, TunnelState.CONNECTED, local_port=7778, remote_port=7777)

            async def disconnect(self, iid):
                self._tok.pop(iid, None)
                return True

            def status(self, iid):
                return (
                    TunnelStatus(iid, TunnelState.CONNECTED, local_port=7778, remote_port=7777)
                    if iid in self._tok
                    else None
                )

            def get_token(self, iid):
                return self._tok.get(iid, "")

            async def token_validates(self, local_port, token):
                return True  # stored token still good — no re-mint

            async def refresh_token(self, iid):
                self._tok[iid] = "FRESH_TOK"
                return "FRESH_TOK"

            def token_ttl_remaining(self, iid):
                return 72000 if iid in self._tok else None

        state = _State(reg, FakeMgr())
        r = asyncio.run(handlers.api_instances_connect(_FakeReq(state, match={"id": "cd-1"})))
        assert r.status == 200 and _body(r)["token"] == "SECRET_TOK"
        # list must NOT leak the token
        r = asyncio.run(handlers.api_instances_list(_FakeReq(state)))
        assert "SECRET_TOK" not in r.body.decode()

    def test_connect_remints_when_stored_token_stale(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        class FakeMgr:
            # connect() returns a CONNECTED tunnel whose stored token is stale
            # (e.g. failed self-heal re-mint / remote restart). The gate must
            # probe, find it rejected, re-mint once, and serve the fresh token.
            def __init__(self):
                self.refreshed = []
                self._tok = {"cd-1": "STALE_TOK"}

            async def connect(self, iid):
                return TunnelStatus(iid, TunnelState.CONNECTED, local_port=7778, remote_port=7777)

            def get_token(self, iid):
                return self._tok.get(iid, "")

            async def token_validates(self, local_port, token):
                return False  # remote rejects the stored token

            async def refresh_token(self, iid):
                self._tok[iid] = "FRESH_TOK"
                self.refreshed.append(iid)
                return "FRESH_TOK"

        mgr = FakeMgr()
        r = asyncio.run(
            handlers.api_instances_connect(_FakeReq(_State(reg, mgr), match={"id": "cd-1"}))
        )
        assert r.status == 200
        assert _body(r)["token"] == "FRESH_TOK"  # served the fresh mint, not the stale token
        assert mgr.refreshed == ["cd-1"]

    def test_connect_502_when_stale_and_remint_fails(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        class FakeMgr:
            # Probe confirms the stored token is no good, and the re-mint also
            # fails (link genuinely down). The handler must NOT serve the
            # unconfirmed token (which would reproduce the stuck 403) — it
            # returns 502 with no token in the body.
            def __init__(self):
                self._tok = {"cd-1": "STALE_TOK"}

            async def connect(self, iid):
                return TunnelStatus(iid, TunnelState.CONNECTED, local_port=7778, remote_port=7777)

            def get_token(self, iid):
                return self._tok.get(iid, "")

            async def token_validates(self, local_port, token):
                return False

            async def refresh_token(self, iid):
                return None  # re-mint failed (SSH/link unreachable)

        r = asyncio.run(
            handlers.api_instances_connect(_FakeReq(_State(reg, FakeMgr()), match={"id": "cd-1"}))
        )
        assert r.status == 502
        assert "token" not in _body(r)  # never serve a token we couldn't confirm
        assert "STALE_TOK" not in r.body.decode()

    def test_connect_failure_promotes_the_diagnosis_verdict_to_a_top_level_code(
        self, tmp_path, monkeypatch
    ):
        """A failed connect names WHICH link broke where a client can read it.

        The ladder's verdict already travels in ``diagnosis.code``, but the
        dashboard's error journal reads a top-level ``code`` — so without the
        promotion the one field that distinguishes "cannot SSH at all" from "SSH
        works, the remote gateway is down" never reaches the surface that offers to
        act on it. Only a NEGATIVE verdict is promoted: the stored diagnosis is the
        last ladder run, so a stale ``ok`` must not be published as this call's
        reason.
        """
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        def connect_returning(status):
            class FakeMgr:
                async def connect(self, iid):
                    return status

            return asyncio.run(
                handlers.api_instances_connect(
                    _FakeReq(_State(reg, FakeMgr()), match={"id": "cd-1"})
                )
            )

        diagnosed = connect_returning(
            TunnelStatus(
                "cd-1",
                TunnelState.ERROR,
                error="tunnel failed",
                diagnosis={
                    "code": "ssh_unreachable",
                    "ok": False,
                    "reason": "Can't SSH to the host",
                    "probes": [{"name": "ssh", "ok": False}],
                },
            )
        )
        assert diagnosed.status == 502 and _body(diagnosed)["code"] == "ssh_unreachable"

        # No diagnosis on record — the response still names the stage that failed
        # rather than leaving the client to parse prose.
        undiagnosed = connect_returning(
            TunnelStatus("cd-1", TunnelState.ERROR, error="tunnel failed")
        )
        assert undiagnosed.status == 502
        assert _body(undiagnosed)["code"] == "instance_connect_failed"

        # A stale healthy verdict is not this failure's reason.
        stale_ok = connect_returning(
            TunnelStatus(
                "cd-1",
                TunnelState.ERROR,
                error="tunnel failed",
                diagnosis={"code": "ok", "ok": True, "reason": "all good", "probes": []},
            )
        )
        assert _body(stale_ok)["code"] == "instance_connect_failed"

    def test_connect_missing_manager_and_unknown_id_carry_codes(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        no_mgr = asyncio.run(
            handlers.api_instances_connect(_FakeReq(_State(reg), match={"id": "cd-1"}))
        )
        assert no_mgr.status == 503
        assert _body(no_mgr)["code"] == "instances_manager_unavailable"

        class MissingMgr:
            async def connect(self, iid):
                raise KeyError(iid)

        ghost = asyncio.run(
            handlers.api_instances_connect(
                _FakeReq(_State(reg, MissingMgr()), match={"id": "ghost"})
            )
        )
        assert ghost.status == 404 and _body(ghost)["code"] == "instance_not_found"

    def test_status_404(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        r = asyncio.run(
            handlers.api_instances_status(
                _FakeReq(_State(self._reg(tmp_path)), match={"id": "ghost"})
            )
        )
        assert r.status == 404

    def test_status_diagnose_runs_ladder(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        calls = []

        class FakeMgr:
            async def diagnose(self, iid):
                calls.append(iid)
                # No live tunnel (status() is None below), so diagnose returns
                # the result instead of storing it on a tunnel status.
                return {"code": "remote_down", "ok": False, "reason": "remote dashboard down"}

            def status(self, iid):
                return None  # never connected — no live tunnel

            def token_ttl_remaining(self, iid):
                return None

            def last_error(self, iid):
                return None  # no retained connect failure for this instance

        r = asyncio.run(
            handlers.api_instances_status(
                _FakeReq(_State(reg, FakeMgr()), match={"id": "cd-1"}, query={"diagnose": "1"})
            )
        )
        assert r.status == 200 and calls == ["cd-1"]
        # Regression: the diagnosis must be surfaced even with no live tunnel,
        # otherwise Diagnose on a disconnected instance shows nothing.
        body = _body(r)
        assert body["diagnosis"]["code"] == "remote_down"
        assert body["diagnosis"]["reason"] == "remote dashboard down"

    def test_add_defaults_remote_port_to_the_stock_gateway_port(self, tmp_path, monkeypatch):
        """#1972: the ADD endpoint must not carry its own stale port default.

        The registry default and the handler default were separate literals, so
        correcting the registry left the HTTP path (which is what the Add form
        actually calls) still handing out an earlier default dashboard port. Every
        unit test built records via ``reg.add`` directly and so could not see it;
        an isolated-pod run against the real endpoint did.
        """
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.registry import DEFAULT_REMOTE_PORT

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        state = _State(reg)
        body = {"name": "Defaulted", "ssh_host": "cd-1-alias", "id": "cd-1"}
        assert asyncio.run(handlers.api_instances_add(_FakeReq(state, body=body))).status == 201
        assert reg.get("cd-1").remote_port == DEFAULT_REMOTE_PORT == 5476

    def test_add_duplicate_and_bad_body(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        state = _State(self._reg(tmp_path))
        body = {"name": "CD", "ssh_host": "cd-1-alias", "id": "cd-1"}
        assert asyncio.run(handlers.api_instances_add(_FakeReq(state, body=body))).status == 201
        # duplicate id -> 400
        assert asyncio.run(handlers.api_instances_add(_FakeReq(state, body=body))).status == 400
        # missing/invalid JSON body -> 400
        assert asyncio.run(handlers.api_instances_add(_FakeReq(state))).status == 400
        # body not an object -> 400
        assert asyncio.run(handlers.api_instances_add(_FakeReq(state, body=["x"]))).status == 400

    def test_add_error_bodies_carry_a_machine_readable_code(self, tmp_path, monkeypatch):
        """Every add rejection names its cause in ``code``, not only in prose.

        The dashboard reads this field (``utils/errorReport``'s ``parseErrorCode``)
        to attach the failure's cause to an agent hand-off, and a first-time setup
        rejection is exactly the case where the user cannot diagnose it alone. A
        duplicate is kept distinct from an invalid field because the two are
        different user actions — rename versus correct — and a client that cannot
        tell them apart has to match on prose.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        state = _State(self._reg(tmp_path))
        body = {"name": "CD", "ssh_host": "cd-1-alias", "id": "cd-1"}
        assert asyncio.run(handlers.api_instances_add(_FakeReq(state, body=body))).status == 201

        dup = asyncio.run(handlers.api_instances_add(_FakeReq(state, body=body)))
        assert dup.status == 400 and _body(dup)["code"] == "instance_duplicate"

        no_json = asyncio.run(handlers.api_instances_add(_FakeReq(state)))
        assert no_json.status == 400 and _body(no_json)["code"] == "invalid_json"

        not_object = asyncio.run(handlers.api_instances_add(_FakeReq(state, body=["x"])))
        assert not_object.status == 400 and _body(not_object)["code"] == "invalid_body"

        bad_field = asyncio.run(
            handlers.api_instances_add(
                _FakeReq(state, body={"name": "Bad", "ssh_host": "h", "remote_port": "not-a-port"})
            )
        )
        assert bad_field.status == 400 and _body(bad_field)["code"] == "invalid_field"

        rejected = asyncio.run(
            handlers.api_instances_add(_FakeReq(state, body={"name": "", "ssh_host": ""}))
        )
        assert rejected.status == 400 and _body(rejected)["code"] == "instance_invalid"

    def test_update_paths(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = _State(reg)
        # success (only allowed fields applied)
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cd-1"}, body={"name": "New"})
            )
        )
        assert r.status == 200 and _body(r)["name"] == "New"
        # unknown id -> 404
        assert (
            asyncio.run(
                handlers.api_instances_update(
                    _FakeReq(state, match={"id": "ghost"}, body={"name": "x"})
                )
            ).status
            == 404
        )
        # invalid value -> 400
        assert (
            asyncio.run(
                handlers.api_instances_update(
                    _FakeReq(state, match={"id": "cd-1"}, body={"ssh_host": "bad host;rm"})
                )
            ).status
            == 400
        )
        # bad JSON / non-object body -> 400
        assert (
            asyncio.run(handlers.api_instances_update(_FakeReq(state, match={"id": "cd-1"}))).status
            == 400
        )
        assert (
            asyncio.run(
                handlers.api_instances_update(_FakeReq(state, match={"id": "cd-1"}, body=42))
            ).status
            == 400
        )

    def test_a_wrong_typed_patch_field_is_refused_not_reinterpreted(self, tmp_path, monkeypatch):
        """PATCH validated values but never their TYPE, so the decoded JSON went
        straight into the record: a non-string name reached `name.strip()` and
        answered 500, and `remote_port: true` validated as port 1 because bool is
        an int. Every case must be a 400 that leaves the record untouched -- and a
        400 specifically, because coercing the value would store a port or a name
        the caller never asked for.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1", remote_port=7777)
        state = _State(reg)
        for body in (
            {"name": 123},
            {"name": None},
            {"name": {"first": "CD"}},
            {"ssh_host": ["cd-1-alias"]},
            {"ttl": 20},
            {"remote_port": True},
            {"remote_port": "7778"},
            {"remote_port": 7778.5},
            {"connection_method": 1},
        ):
            r = asyncio.run(
                handlers.api_instances_update(_FakeReq(state, match={"id": "cd-1"}, body=body))
            )
            assert r.status == 400, f"{body!r} answered {r.status}"
            assert _body(r)["code"] == "instance_invalid"
        after = reg.get("cd-1")
        assert after.name == "CD" and after.ssh_host == "cd-1-alias"
        assert after.remote_port == 7777 and after.ttl == "20h"

    def test_every_editable_field_has_a_declared_type(self):
        """The allowed-field set is DERIVED from the type map, so a field cannot be
        made editable without a type to check it against. Pinned as a ratchet: the
        previous shape listed the fields twice, which is how a value reached a
        validator with no type check in front of it.
        """
        import inspect

        from kiro_crew.dashboard import handlers_instances as handlers

        src = inspect.getsource(handlers.api_instances_update)
        assert "allowed = set(_PATCH_FIELD_TYPES)" in src
        assert set(handlers._PATCH_FIELD_TYPES) >= {"name", "ssh_host", "remote_port", "ttl"}

    def test_update_tears_down_a_tunnel_its_own_edit_invalidated(self, tmp_path, monkeypatch):
        """A tunnel is built from ssh_host/remote_port/connection_method, so editing
        one of those leaves a live tunnel forwarding the OLD port to the OLD host
        under the new label. The edit must tear it down, and must keep
        ``was_connected`` — that flag records an explicit user disconnect, which an
        edit is not, and clearing it would drop the crew out of the switcher."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        reg.update("cd-1", was_connected=True)

        class _Manager:
            def __init__(self):
                self.disconnected = []

            async def disconnect(self, instance_id, *, keep_intent=False):
                self.disconnected.append((instance_id, keep_intent))
                # Mirror the real manager: the port hint always clears, and the
                # connect intent clears ONLY for an explicit user disconnect.
                hints = {"local_port": 0}
                if not keep_intent:
                    hints["was_connected"] = False
                reg.update(instance_id, **hints)
                return True

            def status(self, instance_id):
                return None

            def last_error(self, instance_id):
                return None

        mgr = _Manager()
        mgr.reconfigure = _fake_reconfigure(mgr)  # type: ignore[method-assign]
        state = _State(reg, manager=mgr)
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cd-1"}, body={"remote_port": 7999})
            )
        )
        assert r.status == 200 and _body(r)["remote_port"] == 7999
        # One teardown before the write. The post-write sweep is dated — it fires
        # only for a tunnel that connected before the record changed — and this
        # manager reports none live afterwards, so nothing more to tear down. The
        # teardown is a reconfiguration, so it must not claim to be a user
        # disconnect (that flag is what keeps the crew in the switcher).
        assert mgr.disconnected == [("cd-1", True)], (
            "the stale tunnel was left running, or the teardown claimed to be a " "user disconnect"
        )
        inst = reg.get("cd-1")
        assert inst is not None and inst.was_connected is True

    def test_ttl_beyond_the_minters_bound_is_refused(self, tmp_path, monkeypatch):
        """The token minters accept at most four digits. A ttl this layer lets
        through is persisted happily and then fails at the next connect, blaming
        the tunnel for a value the edit should have refused — so the registry
        enforces the same bound the minters do."""
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.registry import InvalidInstanceError

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        # Direct registry write: the API is not the only caller.
        with pytest.raises(InvalidInstanceError):
            reg.update("cd-1", ttl="99999h")
        # Accepted forms still are.
        assert reg.update("cd-1", ttl="9999h").ttl == "9999h"
        assert reg.update("cd-1", ttl="30m").ttl == "30m"

        state = _State(reg)
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cd-1"}, body={"ttl": "99999h"})
            )
        )
        assert r.status == 400 and _body(r)["code"] == "instance_invalid"

    def test_update_refuses_an_invalid_edit_without_touching_the_tunnel(
        self, tmp_path, monkeypatch
    ):
        """A rejected save must not cost the user their connection: the proposed
        record is validated before the teardown, so a typo answers 400 with the
        crew still connected."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        reg.update("cd-1", was_connected=True)

        class _Manager:
            def __init__(self):
                self.disconnected = []

            async def disconnect(self, instance_id, *, keep_intent=False):
                self.disconnected.append(instance_id)
                return True

            def status(self, instance_id):
                return None

            def last_error(self, instance_id):
                return None

        mgr = _Manager()
        mgr.reconfigure = _fake_reconfigure(mgr)  # type: ignore[method-assign]
        state = _State(reg, manager=mgr)
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cd-1"}, body={"ssh_host": "bad host;rm"})
            )
        )
        assert r.status == 400 and _body(r)["code"] == "instance_invalid"
        assert mgr.disconnected == [], "a rejected edit tore down the tunnel anyway"
        inst = reg.get("cd-1")
        assert inst is not None
        assert inst.ssh_host == "cd-1-alias" and inst.was_connected is True

    def test_update_restores_intent_even_when_the_sweep_found_no_tunnel(
        self, tmp_path, monkeypatch
    ):
        """``disconnect()`` clears the persisted intent whether or not it tracked
        a live tunnel, and reports False in that case. Restoring only on a True
        return would drop the crew out of the switcher."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        reg.update("cd-1", was_connected=True)

        class _NoTunnelManager:
            async def disconnect(self, instance_id, *, keep_intent=False):
                # Mirrors the real manager: the registry cleanup runs even with
                # no live tunnel tracked, and the return value is False.
                hints = {"local_port": 0}
                if not keep_intent:
                    hints["was_connected"] = False
                reg.update(instance_id, **hints)
                return False

            def status(self, instance_id):
                return None

            def last_error(self, instance_id):
                return None

        no_tunnel = _NoTunnelManager()
        no_tunnel.reconfigure = _fake_reconfigure(no_tunnel)  # type: ignore[method-assign]
        state = _State(reg, manager=no_tunnel)
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cd-1"}, body={"remote_port": 7999})
            )
        )
        assert r.status == 200
        inst = reg.get("cd-1")
        assert inst is not None and inst.remote_port == 7999
        assert inst.was_connected is True, "the crew lost its switcher entry"

    def test_update_tears_the_tunnel_down_exactly_once(self, tmp_path, monkeypatch):
        """One teardown, inside the reconfiguration. An extra one after the write
        would hit whatever connected next — i.e. a Connect the user just made on
        the new coordinates."""
        import time as _time

        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        class _FreshTunnelManager:
            def __init__(self):
                self.disconnect_calls = 0

            async def disconnect(self, instance_id, *, keep_intent=False):
                self.disconnect_calls += 1
                return True

            def status(self, instance_id):
                # Connected just now — i.e. after the write this handler is about
                # to make, which is the case the dated sweep must spare.
                return TunnelStatus(
                    instance_id=instance_id,
                    state=TunnelState.CONNECTED,
                    connected_at=_time.time() + 60,
                )

            def last_error(self, instance_id):
                return None

            def token_ttl_remaining(self, instance_id):
                return None

        mgr = _FreshTunnelManager()
        mgr.reconfigure = _fake_reconfigure(mgr)  # type: ignore[method-assign]
        state = _State(reg, manager=mgr)
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cd-1"}, body={"remote_port": 7999})
            )
        )
        assert r.status == 200 and _body(r)["remote_port"] == 7999
        # Only the pre-edit teardown ran; the sweep spared the newer tunnel.
        assert mgr.disconnect_calls == 1

    def test_update_does_not_revive_a_crew_disconnected_mid_edit(self, tmp_path, monkeypatch):
        """An explicit Disconnect landing while a transport edit is in flight must
        win. The edit's teardown preserves intent rather than restoring a snapshot
        of it, so the user's disconnect is not overwritten."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        reg.update("cd-1", was_connected=True)

        class _DisconnectMidEdit:
            def __init__(self):
                self.calls = 0

            async def disconnect(self, instance_id, *, keep_intent=False):
                self.calls += 1
                hints = {"local_port": 0}
                if not keep_intent:
                    hints["was_connected"] = False
                reg.update(instance_id, **hints)
                if self.calls == 1:
                    # The user presses Disconnect while the save is in flight.
                    reg.update(instance_id, was_connected=False)
                return True

            def status(self, instance_id):
                return None

            def last_error(self, instance_id):
                return None

        mid_edit = _DisconnectMidEdit()
        mid_edit.reconfigure = _fake_reconfigure(mid_edit)  # type: ignore[method-assign]
        state = _State(reg, manager=mid_edit)
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cd-1"}, body={"remote_port": 7999})
            )
        )
        assert r.status == 200 and _body(r)["remote_port"] == 7999
        inst = reg.get("cd-1")
        assert inst is not None
        assert inst.was_connected is False, "the edit revived a crew the user disconnected"
        # The response must report the same thing, so the dashboard does not
        # reconnect off a stale view.
        assert _body(r)["was_connected"] is False

    def test_update_rewrites_the_coordinates_inside_the_teardown_critical_section(
        self, tmp_path, monkeypatch
    ):
        """The teardown and the coordinate rewrite must reach the manager as ONE
        operation. Done as two, a connect can read the OLD record in between, and
        whether its tunnel is CONNECTED or still CONNECTING when the write lands
        decides whether anything notices — so the handler must not persist on its
        own when a manager is present."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        reg.update("cd-1", was_connected=True)

        events: list[str] = []

        class _OrderingManager:
            async def disconnect(self, instance_id, *, keep_intent=False):
                events.append(f"teardown(keep_intent={keep_intent})")
                return True

            async def reconfigure(self, instance_id, apply):
                events.append("enter-critical-section")
                await self.disconnect(instance_id, keep_intent=True)
                out = apply()
                events.append("leave-critical-section")
                return out

            def status(self, instance_id):
                return None

            def last_error(self, instance_id):
                return None

            def token_ttl_remaining(self, instance_id):
                return None

        state = _State(reg, manager=_OrderingManager())
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cd-1"}, body={"remote_port": 7999})
            )
        )
        assert r.status == 200 and _body(r)["remote_port"] == 7999
        assert events == [
            "enter-critical-section",
            "teardown(keep_intent=True)",
            "leave-critical-section",
        ], events
        inst = reg.get("cd-1")
        assert inst is not None and inst.was_connected is True

    def test_update_refuses_to_save_when_the_tunnel_cannot_be_torn_down(
        self, tmp_path, monkeypatch
    ):
        """A stop that failed leaves the OLD forward live.

        Persisting the new coordinates then leaves the record describing one
        machine while the still-open tunnel serves another — and that tunnel is
        the one the user reaches. So the edit aborts, nothing is written, and the
        caller is told to disconnect and retry.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        reg.update("cd-1", was_connected=True)

        class _WedgedManager:
            async def reconfigure(self, instance_id, apply):
                raise OSError("ssh process will not die")

            async def disconnect(self, instance_id, *, keep_intent=False):
                raise OSError("ssh process will not die")

            def status(self, instance_id):
                return None

            def last_error(self, instance_id):
                return None

            def token_ttl_remaining(self, instance_id):
                return None

        state = _State(reg, manager=_WedgedManager())
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cd-1"}, body={"ssh_host": "cd-2-alias"})
            )
        )
        assert r.status == 503 and _body(r)["code"] == "tunnel_teardown_failed"
        inst = reg.get("cd-1")
        assert inst is not None
        assert inst.ssh_host == "cd-1-alias", "coordinates advanced over a live tunnel"
        assert inst.was_connected is True

    def test_update_leaves_a_healthy_tunnel_alone_when_only_the_label_changes(
        self, tmp_path, monkeypatch
    ):
        """A rename does not change how the tunnel is opened, so dropping the
        connection for it would be a self-inflicted outage."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        class _Manager:
            def __init__(self):
                self.disconnected = []

            async def disconnect(self, instance_id, *, keep_intent=False):
                self.disconnected.append(instance_id)
                return True

            def status(self, instance_id):
                return None

            def last_error(self, instance_id):
                return None

        mgr = _Manager()
        mgr.reconfigure = _fake_reconfigure(mgr)  # type: ignore[method-assign]
        state = _State(reg, manager=mgr)
        # Re-sending the SAME host alongside a new name is still only a rename.
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(
                    state,
                    match={"id": "cd-1"},
                    body={"name": "Renamed", "ssh_host": "cd-1-alias"},
                )
            )
        )
        assert r.status == 200 and _body(r)["name"] == "Renamed"
        assert mgr.disconnected == []

    def test_remove_success_and_404(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = _State(reg)
        r = asyncio.run(handlers.api_instances_remove(_FakeReq(state, match={"id": "cd-1"})))
        assert r.status == 200 and _body(r)["removed"] == "cd-1"
        assert (
            asyncio.run(
                handlers.api_instances_remove(_FakeReq(state, match={"id": "ghost"}))
            ).status
            == 404
        )

    def test_remove_sweeps_a_reconnect_that_raced_the_removal(self, tmp_path, monkeypatch):
        """The offloaded reg.remove yields the loop between the pre-removal
        disconnect and the deletion, so a tab reconnect can re-establish a
        tunnel for the record mid-delete. A successful removal must disconnect
        AGAIN afterwards: the record is gone by then, so connect refuses new
        attempts and the sweep tears down whatever slipped in."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        class _RacingManager:
            def __init__(self):
                self.disconnect_calls = 0
                self.live = False

            async def disconnect(self, instance_id):
                self.disconnect_calls += 1
                if self.disconnect_calls == 1:
                    # A reconnect slips in right after the pre-removal teardown.
                    self.live = True
                else:
                    self.live = False
                return True

        mgr = _RacingManager()
        mgr.reconfigure = _fake_reconfigure(mgr)  # type: ignore[method-assign]
        state = _State(reg, manager=mgr)
        r = asyncio.run(handlers.api_instances_remove(_FakeReq(state, match={"id": "cd-1"})))
        assert r.status == 200
        assert mgr.disconnect_calls == 2, "no post-removal teardown sweep ran"
        assert mgr.live is False, "the racing reconnect's tunnel survived the removal"

    def test_connect_503_404_and_502(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        # manager unavailable -> 503
        assert (
            asyncio.run(
                handlers.api_instances_connect(_FakeReq(_State(reg, None), match={"id": "cd-1"}))
            ).status
            == 503
        )

        class FakeMgr:
            async def connect(self, iid):
                if iid == "ghost":
                    raise KeyError(iid)
                return TunnelStatus(iid, TunnelState.ERROR, error="boom")

            def get_token(self, iid):
                return ""

        state = _State(reg, FakeMgr())
        # KeyError -> 404
        assert (
            asyncio.run(
                handlers.api_instances_connect(_FakeReq(state, match={"id": "ghost"}))
            ).status
            == 404
        )
        # non-connected result -> 502 with the error surfaced
        r = asyncio.run(handlers.api_instances_connect(_FakeReq(state, match={"id": "cd-1"})))
        assert r.status == 502 and _body(r)["error"] == "boom"

    def test_disconnect_reports_was_connected(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)

        class FakeMgr:
            async def disconnect(self, iid):
                return True

        r = asyncio.run(
            handlers.api_instances_disconnect(
                _FakeReq(_State(self._reg(tmp_path), FakeMgr()), match={"id": "cd-1"})
            )
        )
        assert r.status == 200 and _body(r)["was_connected"] is True

    def test_restart_paths(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        # known id but no manager -> 503
        assert (
            asyncio.run(
                handlers.api_instances_restart(_FakeReq(_State(reg, None), match={"id": "cd-1"}))
            ).status
            == 503
        )
        # unknown id -> 404 (checked before the manager)
        assert (
            asyncio.run(
                handlers.api_instances_restart(
                    _FakeReq(_State(reg, object()), match={"id": "ghost"})
                )
            ).status
            == 404
        )

        class FakeMgr:
            def __init__(self, ok):
                self._ok = ok

            async def restart_remote(self, iid):
                return {"ok": self._ok, "message": "" if self._ok else "fail"}

        # success -> 200
        r = asyncio.run(
            handlers.api_instances_restart(
                _FakeReq(_State(reg, FakeMgr(True)), match={"id": "cd-1"})
            )
        )
        assert r.status == 200 and _body(r)["ok"] is True
        # failure -> 502
        r = asyncio.run(
            handlers.api_instances_restart(
                _FakeReq(_State(reg, FakeMgr(False)), match={"id": "cd-1"})
            )
        )
        assert r.status == 502

    def test_audit_failure_never_breaks_request(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)

        def boom():
            raise RuntimeError("sel unavailable")

        # _audit swallows SEL failures so the control plane stays available.
        monkeypatch.setattr(handlers, "sel", boom)
        r = asyncio.run(handlers.api_instances_list(_FakeReq(_State(self._reg(tmp_path)))))
        assert r.status == 200

    def test_update_locks_addressing_fields_for_correlated_cloud_instance(
        self, tmp_path, monkeypatch
    ):
        # Regression test for #3387: PATCH must not let a non-dashboard caller
        # (CLI, script, agent) rewrite the fields Stop/Start/Delete use to
        # resolve an EC2 stack launched by Kiro Crew — doing so strands a running,
        # billing instance with no dashboard path to reach it.
        from kiro_crew.cloud import launch_job as lj
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        launched = reg.add(
            name="Cloud",
            connection_method="ssm",
            ssm_target="i-0123abcd",
            aws_profile="prod",
            aws_region="us-east-1",
            instance_id="cloud-launched",
        )
        # A job Kiro Crew provisioned whose EC2 instance id matches the
        # instance's ssm_target — this is what makes it "correlated".
        store = lj.LaunchJobStore()  # honours KIROCREW_HOME, same as production
        job = store.create(profile="prod", region="us-east-1", size_key="light")
        job.instance_id = "i-0123abcd"
        store.save(job)
        state = _State(reg)

        # Editing an addressing field is rejected...
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cloud-launched"}, body={"aws_region": "us-west-2"})
            )
        )
        assert r.status == 400
        body = _body(r)
        assert body["code"] == "cloud_instance_addressing_locked"
        # ...and the record is untouched.
        assert reg.get("cloud-launched").aws_region == "us-east-1"

        # A non-addressing field on the same correlated instance is still editable.
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cloud-launched"}, body={"name": "Renamed"})
            )
        )
        assert r.status == 200 and _body(r)["name"] == "Renamed"

        # A hand-added SSM instance whose ssm_target matches no launch job is
        # NOT correlated — its addressing fields stay editable.
        reg.add(
            name="Hand-added",
            connection_method="ssm",
            ssm_target="i-89ab1234",
            instance_id="cloud-manual",
        )
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cloud-manual"}, body={"aws_region": "eu-west-1"})
            )
        )
        assert r.status == 200 and _body(r)["aws_region"] == "eu-west-1"

        assert launched.ssm_target == "i-0123abcd"  # confidence check: fixture unchanged

    def test_update_fails_closed_when_correlation_check_errors(self, tmp_path, monkeypatch):
        # If the launch job store can't be read, the correlation check must
        # NOT fall back to "not correlated" — that would let this addressing
        # edit through and strand a launched, billing instance the same way
        # #3387 did. The PATCH refuses the edit instead of persisting one it
        # could not verify was safe.
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(
            name="Cloud",
            connection_method="ssm",
            ssm_target="i-0123abcd",
            aws_profile="prod",
            aws_region="us-east-1",
            instance_id="cloud-launched",
        )
        state = _State(reg)

        def boom(ssm_target):
            raise OSError("disk unavailable")

        monkeypatch.setattr(handlers, "_is_correlated_cloud_instance", boom)

        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cloud-launched"}, body={"aws_region": "us-west-2"})
            )
        )
        assert r.status == 503
        assert _body(r)["code"] == "cloud_instance_correlation_check_failed"
        # ...and the record is untouched.
        assert reg.get("cloud-launched").aws_region == "us-east-1"

        # A non-addressing field never invokes the (broken) correlation check.
        r = asyncio.run(
            handlers.api_instances_update(
                _FakeReq(state, match={"id": "cloud-launched"}, body={"name": "Renamed"})
            )
        )
        assert r.status == 200 and _body(r)["name"] == "Renamed"


# ══════════════════════════════════════════════════════════════════════════
# Phase 3-4: resilience + convenience
# ══════════════════════════════════════════════════════════════════════════


class TestTokenMintGeneric:
    def test_ttl_to_seconds(self):
        from kiro_crew.instances.token_mint import TokenMintError, ttl_to_seconds

        assert ttl_to_seconds("20h") == 72000
        assert ttl_to_seconds("30m") == 1800
        with pytest.raises(TokenMintError):
            ttl_to_seconds("bad")

    def test_generic_builders_and_token_delegation(self):
        from kiro_crew.instances.token_mint import (
            build_candidate_command,
            build_remote_command,
            build_remote_token_command,
        )

        assert 'exec "$b" restart;' in build_candidate_command("restart")
        assert '"$HOME/bin/kirocrew" restart' in build_remote_command("~/bin/kirocrew", "restart")
        # token builder emits identical strings via the generic builders it delegates to
        assert 'exec "$b" token --ttl 20h;' in build_remote_token_command("", ttl="20h")

    def test_run_remote_kirocrew(self, monkeypatch):
        from kiro_crew.instances import token_mint as tm

        class FakeProc:
            returncode = 0

            async def communicate(self):
                return b"", b""

        async def fake_exec(*a, **k):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        rc, err = asyncio.run(tm.run_remote_kirocrew("cd-1", "restart"))
        assert rc == 0 and err == ""

    def test_run_remote_kirocrew_honors_connect_timeout_secs(self, monkeypatch):
        """#3579: the fail-fast 10s ConnectTimeout default must not silently
        override a caller-supplied budget -- a restart on a slow-proxy host
        needs the same connect budget the mint itself gets."""
        from kiro_crew.instances import token_mint as tm

        class FakeProc:
            returncode = 0

            async def communicate(self):
                return b"", b""

        captured = {}

        async def fake_exec(*argv, **k):
            captured["argv"] = argv
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        rc, _ = asyncio.run(tm.run_remote_kirocrew("cd-1", "restart", connect_timeout_secs=45.0))
        assert rc == 0
        assert "ConnectTimeout=45" in captured["argv"]

    def test_run_remote_kirocrew_redacts_stderr(self, monkeypatch):
        # Proxy-controlled stderr carrying a credential is redacted before return,
        # so a caller logging the tail cannot leak it.
        from kiro_crew.instances import token_mint as tm

        class FakeProc:
            returncode = 255

            async def communicate(self):
                return b"", b"WSSH error AKIAIOSFODNN7EXAMPLE banner"

        async def fake_exec(*a, **k):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        rc, err = asyncio.run(tm.run_remote_kirocrew("cd-1", "restart"))
        assert rc == 255
        assert "AKIAIOSFODNN7EXAMPLE" not in err
        assert "[REDACTED: credential]" in err

    class _HangProc:
        """First ``communicate`` times out; the reap (a SECOND communicate)
        records itself and returns. ``wait`` must never be touched: on a
        killed child blocked writing into a full stderr pipe it hangs the
        caller forever (#5989)."""

        def __init__(self) -> None:
            self.pid = 4242
            self.returncode: int | None = None
            self.kill_calls = 0
            self.wait_calls = 0
            self.communicate_calls = 0

        async def communicate(self):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise asyncio.TimeoutError
            self.returncode = -9
            return b"", b""

        def kill(self) -> None:
            self.kill_calls += 1

        async def wait(self) -> int:
            self.wait_calls += 1
            return -9

    def test_mint_timeout_reaps_child_via_communicate_not_wait(self, monkeypatch):
        from kiro_crew.instances import token_mint as tm

        proc = self._HangProc()

        async def fake_exec(*a, **k):
            return proc

        killed: list[tuple[int, int]] = []

        async def _tree(pid, sig):
            killed.append((pid, sig))
            return True

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(platform_compat, "kill_process_tree_async", _tree)
        with pytest.raises(tm.TokenMintError, match="timed out minting"):
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        assert killed == [(proc.pid, platform_compat.SIGKILL)]
        assert proc.kill_calls == 1
        assert proc.communicate_calls == 2
        assert proc.wait_calls == 0

    def test_run_remote_kirocrew_timeout_reaps_child_via_communicate_not_wait(self, monkeypatch):
        from kiro_crew.instances import token_mint as tm

        proc = self._HangProc()

        async def fake_exec(*a, **k):
            return proc

        killed: list[tuple[int, int]] = []

        async def _tree(pid, sig):
            killed.append((pid, sig))
            return True

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(platform_compat, "kill_process_tree_async", _tree)
        rc, err = asyncio.run(tm.run_remote_kirocrew("cd-1", "restart"))
        assert rc == -1
        assert "timed out after" in err
        assert killed == [(proc.pid, platform_compat.SIGKILL)]
        assert proc.kill_calls == 1
        assert proc.communicate_calls == 2
        assert proc.wait_calls == 0


class TestDiagnostics:
    def _set_probes(self, monkeypatch, ssh, remote, local):
        from kiro_crew.instances import diagnostics as diag

        async def _ssh(h, connect_timeout_secs=10.0):
            return ssh

        async def _rem(h, p, connect_timeout_secs=10.0):
            return remote

        async def _loc(p):
            return local

        monkeypatch.setattr(diag, "_probe_ssh", _ssh)
        monkeypatch.setattr(diag, "_probe_remote_dashboard", _rem)
        monkeypatch.setattr(diag, "_probe_local_forward", _loc)

    def test_ladder_first_broken_link(self, monkeypatch):
        from kiro_crew.instances.diagnostics import (
            NOT_CONNECTED,
            OK,
            REMOTE_DOWN,
            SSH_UNREACHABLE,
            TUNNEL_DOWN,
            diagnose_instance,
        )

        self._set_probes(monkeypatch, False, True, True)
        assert asyncio.run(diagnose_instance("cd-1-alias", 7777, 7778)).code == SSH_UNREACHABLE
        self._set_probes(monkeypatch, True, False, True)
        assert asyncio.run(diagnose_instance("cd-1-alias", 7777, 7778)).code == REMOTE_DOWN
        self._set_probes(monkeypatch, True, True, False)
        assert asyncio.run(diagnose_instance("cd-1-alias", 7777, 7778)).code == TUNNEL_DOWN
        self._set_probes(monkeypatch, True, True, True)
        r = asyncio.run(diagnose_instance("cd-1-alias", 7777, 7778))
        assert r.code == OK and r.ok and r.to_dict()["ok"] is True
        # local_port == 0 (never connected): ssh + remote up, but no forward to
        # probe → NOT_CONNECTED, not the misleading TUNNEL_DOWN "reconnect".
        self._set_probes(monkeypatch, True, True, True)
        assert asyncio.run(diagnose_instance("cd-1-alias", 7777, 0)).code == NOT_CONNECTED

    def test_invalid_host_short_circuits(self):
        from kiro_crew.instances.diagnostics import UNKNOWN, diagnose_instance

        r = asyncio.run(diagnose_instance("-obadhost", 7777, 7778))
        assert r.code == UNKNOWN and r.probes == []

    def test_probe_helpers_via_mocked_subprocess(self, monkeypatch):
        from kiro_crew.instances import diagnostics as diag

        class FakeProc:
            def __init__(self, rc, out=b""):
                self.returncode = rc
                self._out = out

            async def wait(self):
                return self.returncode

            async def communicate(self):
                return (self._out, b"")

            def kill(self):
                pass

        def mk(rc, out=b""):
            async def _exec(*a, **k):
                return FakeProc(rc, out)

            return _exec

        # _run_ok: exit 0 -> True, nonzero -> False
        monkeypatch.setattr(asyncio, "create_subprocess_exec", mk(0))
        assert asyncio.run(diag._run_ok(["true"], 1.0)) is True
        monkeypatch.setattr(asyncio, "create_subprocess_exec", mk(1))
        assert asyncio.run(diag._run_ok(["false"], 1.0)) is False

        # _run_stdout: exit 0 -> decoded stdout, nonzero -> None
        monkeypatch.setattr(asyncio, "create_subprocess_exec", mk(0, b"200"))
        assert asyncio.run(diag._run_stdout(["x"], 1.0)) == "200"
        monkeypatch.setattr(asyncio, "create_subprocess_exec", mk(1, b"x"))
        assert asyncio.run(diag._run_stdout(["x"], 1.0)) is None

        # _probe_ssh delegates to _run_ok
        monkeypatch.setattr(asyncio, "create_subprocess_exec", mk(0))
        assert asyncio.run(diag._probe_ssh("cd-1")) is True

        # _probe_remote_dashboard: a real HTTP code -> True, '000'/empty -> False
        monkeypatch.setattr(asyncio, "create_subprocess_exec", mk(0, b"200"))
        assert asyncio.run(diag._probe_remote_dashboard("cd-1", 7777)) is True
        monkeypatch.setattr(asyncio, "create_subprocess_exec", mk(0, b"000"))
        assert asyncio.run(diag._probe_remote_dashboard("cd-1", 7777)) is False

    def test_probes_honor_connect_timeout_secs(self, monkeypatch):
        """#3579: the hardcoded ConnectTimeout=10 must not silently override a
        caller-supplied budget -- a diagnosis on a slow-proxy host the user
        already tuned instances.connect_timeout_secs for must not be
        misreported as unreachable just because the probe never saw that
        tuning."""
        from kiro_crew.instances import diagnostics as diag

        captured = {}

        class FakeProc:
            returncode = 0

            async def wait(self):
                return 0

            async def communicate(self):
                return (b"200", b"")

        async def fake_exec(*argv, **k):
            captured["argv"] = argv
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        assert asyncio.run(diag._probe_ssh("cd-1", connect_timeout_secs=42.0)) is True
        assert "ConnectTimeout=42" in captured["argv"]
        # Both probes share token_mint._build_ssh_argv with the mint, so the two
        # options a probe cannot work without are pinned HERE too: without
        # BatchMode a probe hangs on an interactive prompt instead of reporting
        # unreachable, and without AddressFamily=inet it can resolve ::1 and miss
        # the IPv4 loopback forward. A mint-motivated edit to the shared builder
        # would otherwise change the ladder with no signal on this side.
        assert "BatchMode=yes" in captured["argv"]
        assert "AddressFamily=inet" in captured["argv"]

        assert (
            asyncio.run(diag._probe_remote_dashboard("cd-1", 7777, connect_timeout_secs=42.0))
            is True
        )
        assert "ConnectTimeout=42" in captured["argv"]
        assert "BatchMode=yes" in captured["argv"]
        assert "AddressFamily=inet" in captured["argv"]

    def test_probe_local_forward(self):
        from kiro_crew.instances import diagnostics as diag

        # no port -> False without connecting
        assert asyncio.run(diag._probe_local_forward(0)) is False
        # a real listening socket -> reachable
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        try:
            assert asyncio.run(diag._probe_local_forward(port)) is True
        finally:
            s.close()


class _ResilTunnel:
    """Controllable fake tunnel for self-heal tests."""

    def __init__(
        self,
        iid,
        ssh_host,
        lp,
        rp,
        *,
        connect_timeout_secs=0,
        compression=True,
        probe_failure_threshold=0,
        on_exit=None,
        transport="ssh",
        ssm_target="",
        aws_profile="",
        aws_region="",
    ):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        self._S = TunnelState
        self.transport = transport
        self.status = TunnelStatus(instance_id=iid, local_port=lp, remote_port=rp)
        self.start_result = True
        # Mirrors _SshTunnel.pid; _mark_recovered persists it after a rebuild.
        self.pid = None

    async def start(self):
        self.status.state = self._S.CONNECTED if self.start_result else self._S.ERROR
        return self.start_result

    async def stop(self):
        self.status.state = self._S.STOPPED


class TestTunnelStatus:
    def test_to_dict_includes_diagnosis_only_when_set(self):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        # no diagnosis -> key absent
        d = TunnelStatus("cd-1", TunnelState.CONNECTED, local_port=7778, remote_port=7777).to_dict()
        assert d["state"] == "connected" and "diagnosis" not in d
        # diagnosis attached -> surfaced verbatim
        diag = {"code": "tunnel_down", "ok": False, "reason": "x", "probes": []}
        d2 = TunnelStatus("cd-1", TunnelState.ERROR, diagnosis=diag).to_dict()
        assert d2["diagnosis"] == diag


class TestSelfHealRefreshRestart:
    @pytest.fixture(autouse=True)
    def _free_ports(self, monkeypatch):
        _patch_port_probe(monkeypatch)

    def _mgr(self, tmp_path, *, mint=None, factory=_ResilTunnel):
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
            return "TOK"

        return reg, SshTunnelManager(
            reg, base_port=53900, mint_token=mint or ok_mint, tunnel_factory=factory
        )

    @pytest.mark.asyncio
    async def test_recover_tier1_then_tier2(self, tmp_path):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")
        # Tier 1 success
        mgr._tunnels["cd-1"].status.state = TunnelState.ERROR
        await mgr._recover("cd-1")
        assert mgr.status("cd-1").state == TunnelState.CONNECTED
        assert mgr._recover_attempts.get("cd-1", 0) == 0

    def test_recover_releases_lock_during_io(self, tmp_path):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        started = asyncio.Event()
        release = asyncio.Event()

        class SlowTunnel(_ResilTunnel):
            async def start(self):
                started.set()
                await release.wait()  # block the "slow" rebuild I/O
                self.status.state = self._S.CONNECTED
                return True

        reg, mgr = self._mgr(tmp_path, factory=SlowTunnel)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        # seed a live ERROR tunnel so _recover proceeds to a (slow) tier-1 rebuild
        mgr._tunnels["cd-1"] = _ResilTunnel("cd-1", "cd-1-alias", 53999, 7777)
        mgr._tunnels["cd-1"].status.state = TunnelState.ERROR

        async def main():
            task = asyncio.create_task(mgr._recover("cd-1"))
            await asyncio.wait_for(started.wait(), timeout=2)
            # Slow rebuild is in flight — the manager lock must NOT be held (the fix).
            assert not mgr._lock.locked()
            release.set()
            await asyncio.wait_for(task, timeout=2)
            assert mgr.status("cd-1").state == TunnelState.CONNECTED
            assert mgr._recover_attempts.get("cd-1", 0) == 0

        asyncio.run(main())

    @pytest.mark.asyncio
    async def test_recover_attempt_cap_then_diagnose(self, tmp_path, monkeypatch):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        def failing(*a, **k):
            t = _ResilTunnel(*a, **k)
            t.start_result = False
            return t

        reg, mgr = self._mgr(tmp_path, factory=failing)
        mgr._max_recovery = 2
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        # seed a live ERROR tunnel
        mgr._tunnels["cd-1"] = _ResilTunnel("cd-1", "cd-1-alias", 53999, 7777)
        mgr._tunnels["cd-1"].status.state = TunnelState.ERROR
        diag_calls = []
        monkeypatch.setattr(mgr, "_schedule_diagnosis", lambda i: diag_calls.append(i))
        for _ in range(mgr._max_recovery + 1):
            mgr._tunnels["cd-1"].status.state = TunnelState.ERROR
            await mgr._recover("cd-1")
        assert diag_calls == ["cd-1"], diag_calls  # diagnosis scheduled once cap exceeded

    # ── respawn-loop fix (orphaned port-holder) ──────────────────────────────

    @pytest.mark.asyncio
    async def test_rebuild_stops_old_before_replace(self, tmp_path):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        old = _ResilTunnel("cd-1", "cd-1-alias", 53999, 7777)
        old.status.state = TunnelState.ERROR
        mgr._tunnels["cd-1"] = old
        inst = reg.get("cd-1")
        # _rebuild takes resolved transport params (not a bare ssh host) so the
        # same code path serves both the ssh and ssm transports.
        ok = await mgr._rebuild(inst, mgr._resolve_transport(inst), 53999)
        assert ok is True
        # The old tunnel's child must be stopped (port freed) before the replace,
        # else it orphans and holds the forward port -> respawn loop.
        assert old.status.state == TunnelState.STOPPED
        assert mgr._tunnels["cd-1"] is not old

    @pytest.mark.asyncio
    async def test_connect_stops_stale_tunnel_before_replace(self, tmp_path):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        # Use the _FakeTunnel (tracks .stopped) for this manager.
        reg, mgr = self._mgr(tmp_path, factory=_FakeTunnel)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        stale = _FakeTunnel("cd-1", "cd-1-alias", 53910, 7777)
        stale.status.state = TunnelState.ERROR  # tracked but not CONNECTED
        mgr._tunnels["cd-1"] = stale
        st = await mgr.connect("cd-1")
        assert st.state == TunnelState.CONNECTED
        assert stale.stopped is True  # stale tunnel terminated before replacement
        assert mgr._tunnels["cd-1"] is not stale

    @pytest.mark.asyncio
    async def test_wait_until_ready_rejects_child_that_dies_during_probe(self):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, _SshTunnel

        class _Proc:
            def __init__(self):
                self.returncode = None
                self.stderr = self

            async def read(self):
                return b"bind [127.0.0.1]:53991: Address already in use\r\n"

        t = _SshTunnel("cd-1", "cd-1-alias", 53991, 7777, connect_timeout_secs=1.0)
        t._proc = _Proc()

        async def reachable():
            # Simulate a stale holder answering while OUR child dies the same tick.
            t._proc.returncode = 255
            return True

        t._port_reachable = reachable  # type: ignore[assignment]
        assert await t._wait_until_ready() is False
        assert t.status.state == TunnelState.ERROR
        assert "in use" in t.status.error.lower()
        assert "post-quantum" not in t.status.error.lower()

    def test_exit_error_strips_post_quantum_noise(self):
        from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel

        t = _SshTunnel("cd-1", "h", 1, 2)
        t._stderr_buf = (
            "** WARNING: connection is not using a post-quantum key exchange algorithm.\n"
            '** This session may be vulnerable to "store now, decrypt later" attacks.\n'
            "** The server may need to be upgraded. See https://openssh.com/pq.html\n"
            "bind [127.0.0.1]:7778: Address already in use\n"
        )
        err = t._exit_error(255)
        assert "post-quantum" not in err.lower()
        assert "already in use" in err.lower()
        # Pure-noise stderr falls back to the bare exit code (no false detail).
        t._stderr_buf = (
            "** WARNING: connection is not using a post-quantum key exchange algorithm.\n"
        )
        assert t._exit_error(255) == "ssh exited with code 255"

    def test_exit_error_classifies_wssh_transport_vs_auth(self):
        from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel

        t = _SshTunnel("cd-1", "h", 1, 2)

        # WSSH transport drop carrying ANSI + passthrough auth-prompt prose is
        # NOT an auth verdict — it classifies as a transport drop, and the ANSI is
        # stripped from the surfaced detail (never reflected raw).
        t._stderr_buf = (
            "\x1b[1G\x1b[31m[Message from WSSH Proxy Service] "
            "Your SSH session ended unexpectedly. Re-authenticate if your session expired.\x1b[0m\n"
        )
        err = t._exit_error(255)
        assert "transport drop" in err.lower()
        assert "auth failed" not in err.lower()
        assert "\x1b" not in err  # ANSI stripped

        # Banner-exchange timeout (the re-mint failure case) is also transport.
        t._stderr_buf = "Connection timed out during banner exchange\n"
        assert "transport drop" in t._exit_error(255).lower()

        # A genuine ssh auth failure IS reported as auth.
        t._stderr_buf = "host: Permission denied (publickey).\n"
        auth = t._exit_error(255)
        assert "auth failed" in auth.lower()
        assert "transport drop" not in auth.lower()

        # A real certificate-expiry message stays an auth verdict.
        t._stderr_buf = "Certificate has expired\n"
        assert "auth failed" in t._exit_error(255).lower()

    def test_recover_backoff_grows_and_caps(self):
        from kiro_crew.instances.ssh_tunnel_manager import (
            _RECOVER_BACKOFF_MAX_SECS,
            _recover_backoff_secs,
        )

        assert _recover_backoff_secs(1) < _recover_backoff_secs(2) < _recover_backoff_secs(3)
        assert _recover_backoff_secs(99) == _RECOVER_BACKOFF_MAX_SECS

    @pytest.mark.asyncio
    async def test_refresh_token_once(self, tmp_path):
        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1", ttl="20h")
        await mgr.connect("cd-1")
        assert mgr._refresh_tasks.get("cd-1") is not None
        ttl0 = mgr.token_ttl_remaining("cd-1")
        assert ttl0 is not None and ttl0 > 71000
        ok = await mgr._refresh_token_once("cd-1")
        assert ok and mgr.get_token("cd-1") == "TOK"
        # disconnect cancels refresh + clears ttl
        await mgr.disconnect("cd-1")
        assert "cd-1" not in mgr._refresh_tasks
        assert mgr.token_ttl_remaining("cd-1") is None

    @pytest.mark.asyncio
    async def test_a_token_minted_for_a_replaced_tunnel_is_discarded(self, tmp_path):
        """A mint runs WITHOUT the manager lock, so the tunnel it was minted for can
        be torn down and replaced while it is in flight — and `instance_id in
        self._tunnels` is true again for the REPLACEMENT, so it cannot tell the two
        apart. The request-driven `refresh_token()` the embedded dashboard calls is
        not a task in `_refresh_tasks`, so it cannot be cancelled by name either;
        the generation stamp is what makes the write refuse itself.
        """
        # Only the mint under test blocks; connect's own mints must not.
        arm = asyncio.Event()
        started = asyncio.Event()
        release = asyncio.Event()
        minted = 0

        async def slow_mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            nonlocal minted
            minted += 1
            if arm.is_set():
                arm.clear()
                started.set()
                await release.wait()
                return "TOK-STALE"
            return f"TOK-{minted}"

        reg, mgr = self._mgr(tmp_path, mint=slow_mint)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")
        first_epoch = mgr._tunnel_epoch["cd-1"]

        # A refresh begins against the CURRENT tunnel and blocks inside its mint.
        arm.set()
        refresh = asyncio.create_task(mgr._refresh_token_once("cd-1"))
        await asyncio.wait_for(started.wait(), timeout=5)
        # Meanwhile the tunnel is replaced (an edit + reconnect, or a self-heal).
        await mgr.disconnect("cd-1")
        await mgr.connect("cd-1")
        assert mgr._tunnel_epoch["cd-1"] > first_epoch
        good = mgr.get_token("cd-1")

        release.set()
        assert await refresh is False, "a token for a superseded tunnel must not be stored"
        # The valid token of the CURRENT tunnel is untouched.
        assert mgr.get_token("cd-1") == good

    @pytest.mark.asyncio
    async def test_a_refresh_refuses_to_start_while_the_instance_is_being_edited(self, tmp_path):
        """The barrier is up precisely because the coordinates are about to move, so
        a mint started now could only produce a token for the machine the user is
        leaving. Refusing is what lets the client retry after the edit instead of
        being handed a credential the new remote never issued.
        """
        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")
        mgr._reconfiguring.add("cd-1")
        try:
            assert await mgr._refresh_token_once("cd-1") is False
            assert await mgr.refresh_token("cd-1") is None
        finally:
            mgr._reconfiguring.discard("cd-1")
        assert await mgr._refresh_token_once("cd-1") is True

    @pytest.mark.asyncio
    async def test_refresh_passes_instance_remote_port(self, tmp_path):
        # F1 regression: connect AND proactive re-mint must target the instance's
        # actual remote_port (not the default 7777), or a non-default-port
        # instance gets an invalid re-minted token.
        seen: list = []

        async def capturing_mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            seen.append(remote_port)
            return "TOK"

        reg, mgr = self._mgr(tmp_path, mint=capturing_mint)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1", remote_port=9001)
        await mgr.connect("cd-1")
        assert seen == [9001]  # initial mint targets the right port
        assert await mgr._refresh_token_once("cd-1") is True
        assert seen[-1] == 9001  # proactive refresh re-mints with the same port

    def test_restart_remote(self, tmp_path, monkeypatch):
        from kiro_crew.instances import ssh_tunnel_manager as stm

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        reg.add(name="Bad", ssh_host="-obadhost", instance_id="bad")
        calls = {}

        async def fake_run(
            host,
            sub,
            *,
            remote_bin="",
            marker_port=None,
            timeout_secs=60.0,
            connect_timeout_secs=10.0,
        ):
            calls["a"] = (host, sub, marker_port, connect_timeout_secs)
            return (0, "")

        monkeypatch.setattr(stm, "run_remote_kirocrew", fake_run)
        r = asyncio.run(mgr.restart_remote("cd-1"))
        # remote_port defaults to 5476 → threaded so restart uses the marker resolver.
        # connect_timeout_secs comes from the configured mint budget (unset here,
        # so the ssh default from constants.DEFAULT_MINT_TIMEOUT_SECS), not the
        # 10s ssh-exec fail-fast fallback -- this is the fix for #3579: a restart
        # on a slow-proxy host must reuse the same budget the mint itself gets.
        assert r["ok"] and calls["a"] == ("cd-1-alias", "restart", 5476, 30.0)
        # validation failure
        r = asyncio.run(mgr.restart_remote("bad"))
        assert not r["ok"] and "invalid ssh settings" in r["message"]
        # unknown
        r = asyncio.run(mgr.restart_remote("ghost"))
        assert not r["ok"]

    def test_diagnose_caps_connect_timeout_at_the_diagnostics_ceiling(self, tmp_path, monkeypatch):
        """#3579: a user who raised instances.connect_timeout_secs for a
        genuinely slow proxy still wants a diagnosis to resolve in well
        under a minute, not silently inherit the full tunable -- diagnose()
        must cap what it forwards, not pass the configured value straight
        through."""
        from kiro_crew.instances import ssh_tunnel_manager as stm

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        mgr._connect_timeout = 90.0  # well above the diagnostics cap
        captured = {}

        async def fake_diagnose(ssh_host, remote_port, local_port, connect_timeout_secs=10.0):
            captured["connect_timeout_secs"] = connect_timeout_secs
            from kiro_crew.instances.diagnostics import OK, DiagnosisResult

            return DiagnosisResult(OK, "ok", [])

        monkeypatch.setattr(stm, "diagnose_instance", fake_diagnose)
        result = asyncio.run(mgr.diagnose("cd-1"))
        assert result is not None
        assert captured["connect_timeout_secs"] == stm._DIAGNOSTICS_CONNECT_TIMEOUT_CAP_SECS
        assert captured["connect_timeout_secs"] < 90.0

    def test_probe_loop_tears_down_after_threshold(self, tmp_path, monkeypatch):
        from kiro_crew.instances import ssh_tunnel_manager as stm
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, _SshTunnel

        monkeypatch.setattr(stm, "_PROBE_INTERVAL", 0.01)

        class FakeProc:
            def __init__(self):
                self._rc = None

            @property
            def returncode(self):
                return self._rc

            def terminate(self):
                self._rc = -15

            def kill(self):
                self._rc = -9

            async def wait(self):
                return self._rc if self._rc is not None else 0

            stderr = None

        async def main():
            t = _SshTunnel("cd-1", "h", 7778, 7777, probe_failure_threshold=2)
            t._proc = FakeProc()
            t.status.state = TunnelState.CONNECTED

            async def _unreachable():
                return False

            t._port_reachable = _unreachable
            await asyncio.wait_for(t._probe_loop(), timeout=2)
            await asyncio.sleep(0.05)
            assert t._probe_failed is True
            assert "health probe failed" in t._exit_error(-15)

        asyncio.run(main())

    def test_sshtunnel_start_success_then_stop(self, monkeypatch):
        from kiro_crew.instances import ssh_tunnel_manager as stm
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, _SshTunnel

        monkeypatch.setattr(stm, "_PROBE_INTERVAL", 0)  # no probe-loop task

        class FakeProc:
            def __init__(self):
                self.returncode = None
                self._exited = asyncio.Event()

            async def wait(self):
                await self._exited.wait()
                return self.returncode

            def terminate(self):
                self.returncode = -15
                self._exited.set()

            def kill(self):
                self.returncode = -9
                self._exited.set()

            stderr = None

        async def fake_exec(*a, **k):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

        async def main():
            t = _SshTunnel("cd-1", "cd-1-alias", 7778, 7777)

            async def _reachable():
                return True

            t._port_reachable = _reachable  # forward comes up immediately
            ok = await t.start()
            assert ok and t.status.state == TunnelState.CONNECTED
            assert t.status.connected_at > 0
            # second start while CONNECTED is a no-op
            assert await t.start() is True
            await t.stop()
            assert t.status.state == TunnelState.STOPPED

        asyncio.run(main())


# ── start_dashboard instances hook registration (regression) ─────────────────


class TestInstancesStartupHooks:
    """Regression for "Cannot modify frozen list".

    The instances startup/cleanup hooks must be registered on the aiohttp app
    BEFORE ``runner.setup()`` freezes its signal lists. If registered after,
    ``on_startup.append`` raises ``RuntimeError`` and the startup signal (which
    fires during setup) would never run the hook anyway.
    """

    def _state(self):
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.state import DashboardState

        return DashboardState(
            sessions=MagicMock(), crons=MagicMock(), lessons=MagicMock(), start_time=0.0
        )

    def test_register_then_freeze_then_startup_creates_manager(self, tmp_path, monkeypatch):
        from aiohttp import web

        from kiro_crew.dashboard.server import _register_instances_hooks

        _enable(tmp_path, monkeypatch, enabled=True)
        app = web.Application()
        state = self._state()

        # Register before freeze (mirrors start_dashboard ordering), then freeze
        # the app exactly as ``runner.setup()`` does. Neither step must raise.
        _register_instances_hooks(app, state, port=7777)
        app.freeze()

        # on_startup fires during setup; empty registry => the hook creates the
        # manager and returns early (no real ssh / no last-active instance).
        asyncio.run(app.on_startup.send(app))
        assert state.instances_manager is not None
        assert state.instances_registry is not None

        # on_cleanup must shut the manager down through the same frozen-safe path.
        called = {}

        async def _fake_shutdown():
            called["shutdown"] = True

        state.instances_manager.shutdown = _fake_shutdown
        asyncio.run(app.on_cleanup.send(app))
        assert called.get("shutdown") is True

    def test_disabled_skips_manager_creation(self, tmp_path, monkeypatch):
        from aiohttp import web

        from kiro_crew.dashboard.server import _register_instances_hooks

        _enable(tmp_path, monkeypatch, enabled=False)
        app = web.Application()
        state = self._state()

        _register_instances_hooks(app, state, port=7777)
        app.freeze()
        asyncio.run(app.on_startup.send(app))

        # Flag off => no registry/manager created, and cleanup is a safe no-op.
        assert state.instances_manager is None
        asyncio.run(app.on_cleanup.send(app))


class TestPortMirror:
    """CSE SEC-016: the SSH tunnel's local port mirrors the remote (configured)
    port so the embedded dashboard's Origin (http://127.0.0.1:<port>) matches the
    remote gateway's trusted port. Each connected instance must use a distinct
    remote port; a local bind conflict hard-fails (no dynamic fallback)."""

    @staticmethod
    def _mgr(reg, factory, monkeypatch, *, port_free=True):
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        # Allocation must still succeed when ``port_free`` is False: that case
        # models the TOCTOU loss where the port is taken between allocating it
        # and the manager's re-probe.
        _patch_port_probe(monkeypatch, manager_free=port_free)

        async def ok_mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            return "TOK"

        return SshTunnelManager(reg, mint_token=ok_mint, tunnel_factory=factory)

    @pytest.mark.asyncio
    async def test_local_port_allocated_not_mirrored(self, tmp_path, monkeypatch):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        captured: dict = {}

        def factory(iid, ssh_host, lp, rp, **k):
            captured["lp"], captured["rp"] = lp, rp
            return _FakeTunnel(iid, ssh_host, lp, rp, **k)

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1", remote_port=7900)
        mgr = self._mgr(reg, factory, monkeypatch)

        status = await mgr.connect("cd-1")
        assert status.state == TunnelState.CONNECTED
        # The forward still points AT the remote's port...
        assert captured["rp"] == 7900
        # ...but the local end is allocated from the tunnel base, NOT mirrored.
        assert captured["lp"] != 7900
        assert captured["lp"] >= mgr._allocator.base_port
        assert reg.get("cd-1").local_port == captured["lp"]

    @pytest.mark.asyncio
    async def test_two_instances_share_one_remote_port(self, tmp_path, monkeypatch):
        """#1972: two stock installs both reporting the SAME remote port connect.

        This is the case mirroring made impossible — and the shipped defaults put
        every stock pair in it.
        """
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        seen: dict[str, int] = {}

        def factory(iid, ssh_host, lp, rp, **k):
            seen[iid] = lp
            return _FakeTunnel(iid, ssh_host, lp, rp, **k)

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        # Both remotes are stock: same remote port, which is also the port a
        # stock hub would itself be holding.
        reg.add(name="A", ssh_host="host-a", instance_id="cd-a", remote_port=5476)
        reg.add(name="B", ssh_host="host-b", instance_id="cd-b", remote_port=5476)
        mgr = self._mgr(reg, factory, monkeypatch)

        assert (await mgr.connect("cd-a")).state == TunnelState.CONNECTED
        assert (await mgr.connect("cd-b")).state == TunnelState.CONNECTED
        # Distinct local ports, neither of them the shared remote port.
        assert seen["cd-a"] != seen["cd-b"]
        assert 5476 not in seen.values()
        assert reg.get("cd-a").local_port != reg.get("cd-b").local_port

    @pytest.mark.asyncio
    async def test_reconnect_does_not_reclaim_its_own_recorded_port(self, tmp_path, monkeypatch):
        """A recorded port is NOT preferred; allocation is the only path.

        ``shutdown`` documents that it "Leaves registry hints intact", so a
        recorded ``local_port`` survives a gateway RESTART, not only a crash.
        Preferring it would look like iframe-origin stability but cannot deliver
        any: after a restart the token is re-minted and the pane reloads, so there
        is no origin or ``mc_token_<port>`` cookie left to preserve. The in-session
        case that does want the same port is served by ``_recover`` instead.
        """
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        captured: dict = {}

        def factory(iid, ssh_host, lp, rp, **k):
            captured["lp"] = lp
            return _FakeTunnel(iid, ssh_host, lp, rp, **k)

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1", remote_port=7900)
        mgr = self._mgr(reg, factory, monkeypatch)
        base = mgr._allocator.base_port
        reg.update("cd-1", local_port=base + 2)  # survivor of a restart

        assert (await mgr.connect("cd-1")).state == TunnelState.CONNECTED
        # The lower, free base port wins; the recorded one is never asked for.
        assert captured["lp"] == base
        assert reg.get("cd-1").local_port == base

    @pytest.mark.asyncio
    async def test_port_conflict_hard_fails(self, tmp_path, monkeypatch):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        captured: dict = {}

        def factory(iid, ssh_host, lp, rp, **k):
            captured["called"] = True
            return _FakeTunnel(iid, ssh_host, lp, rp, **k)

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1", remote_port=7900)
        mgr = self._mgr(reg, factory, monkeypatch, port_free=False)

        status = await mgr.connect("cd-1")
        assert status.state == TunnelState.ERROR
        assert "was taken while connecting" in (status.error or "")
        # We fail before opening the tunnel — factory never invoked.
        assert "called" not in captured


class TestLastError:
    """Retained last-error: a failed connect remembers *why* so a sticky tab
    whose tunnel is down can show its error instead of a bare "disconnected"."""

    @pytest.fixture(autouse=True)
    def _free_ports(self, monkeypatch):
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
    async def test_retained_on_validation_failure(self, tmp_path):
        reg, mgr = self._mgr(tmp_path)
        reg.add(name="Bad", ssh_host="-obadhost", instance_id="bad")
        await mgr.connect("bad")
        assert mgr.status("bad") is None  # no live tunnel was created
        assert "invalid ssh settings" in (mgr.last_error("bad") or "")

    @pytest.mark.asyncio
    async def test_retained_on_tunnel_start_failure(self, tmp_path):
        def failing(*a, **k):
            t = _FakeTunnel(*a, **k)
            t.start_result = False
            return t

        reg, mgr = self._mgr(tmp_path, factory=failing)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        await mgr.connect("cd-1")
        # _FakeTunnel.start sets status.error="boom" on failure.
        assert mgr.status("cd-1") is None  # failed tunnel popped, not left lingering
        assert mgr.last_error("cd-1") == "boom"

    @pytest.mark.asyncio
    async def test_retained_on_mint_failure_after_teardown(self, tmp_path):
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
        await mgr.connect("cd-1")
        assert mgr.status("cd-1") is None  # tunnel popped on mint failure
        assert "token mint failed" in (mgr.last_error("cd-1") or "")

    @pytest.mark.asyncio
    async def test_cleared_on_successful_connect(self, tmp_path):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState
        from kiro_crew.instances.token_mint import TokenMintError

        calls = {"n": 0}

        async def flaky_mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TokenMintError("first attempt fails")
            return "SECRET_TOK"

        reg, mgr = self._mgr(tmp_path, mint=flaky_mint)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        await mgr.connect("cd-1")
        assert mgr.last_error("cd-1")  # set after the first failure
        assert (await mgr.connect("cd-1")).state == TunnelState.CONNECTED
        assert mgr.last_error("cd-1") is None  # cleared on the clean connect
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_cleared_on_explicit_disconnect(self, tmp_path):
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
        await mgr.connect("cd-1")
        assert mgr.last_error("cd-1")
        await mgr.disconnect("cd-1")
        assert mgr.last_error("cd-1") is None


class TestStatusForRetainedError:
    """_status_for must surface a retained error (state="error") when no live
    tunnel exists, and fall back to "disconnected" only when there is none."""

    @pytest.fixture(autouse=True)
    def _free_ports(self, monkeypatch):
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
    async def test_surfaces_error_when_no_live_tunnel(self, tmp_path):
        import types

        from kiro_crew.dashboard.handlers_instances import _status_for
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
        await mgr.connect("cd-1")  # fails -> no live tunnel, last_error retained

        state = types.SimpleNamespace(instances_manager=mgr)
        d = _status_for(state, "cd-1")
        assert d["state"] == "error"
        assert "token mint failed" in d["error"]

    @pytest.mark.asyncio
    async def test_disconnected_when_no_tunnel_and_no_error(self, tmp_path):
        import types

        from kiro_crew.dashboard.handlers_instances import _status_for

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        state = types.SimpleNamespace(instances_manager=mgr)
        d = _status_for(state, "cd-1")
        assert d == {"instance_id": "cd-1", "state": "disconnected"}

    @pytest.mark.asyncio
    async def test_live_tunnel_status_wins(self, tmp_path):
        import types

        from kiro_crew.dashboard.handlers_instances import _status_for

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        await mgr.connect("cd-1")
        state = types.SimpleNamespace(instances_manager=mgr)
        d = _status_for(state, "cd-1")
        assert d["state"] == "connected"
        await mgr.shutdown()


class TestStartupRevive:
    """_revive_intended_instances: reconnect every was_connected instance on
    startup and isolate per-instance failures. No credential-staleness gate —
    a failed reconnect simply leaves a sticky error tab to retry."""

    @pytest.fixture(autouse=True)
    def _free_ports(self, monkeypatch):
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
    async def test_revives_all_was_connected(self, tmp_path):
        import kiro_crew.dashboard.server as server
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="A", ssh_host="host-a", instance_id="a", remote_port=7777)
        reg.add(name="B", ssh_host="host-b", instance_id="b", remote_port=7778)
        reg.add(name="C", ssh_host="host-c", instance_id="c", remote_port=7779)
        reg.update("a", was_connected=True)
        reg.update("b", was_connected=True)  # c was never connected

        await server._revive_intended_instances(reg, mgr)

        assert mgr.status("a").state == TunnelState.CONNECTED
        assert mgr.status("b").state == TunnelState.CONNECTED
        assert mgr.status("c") is None  # not intended -> not revived
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_some_fail_isolated_and_intent_preserved(self, tmp_path):
        import kiro_crew.dashboard.server as server
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState
        from kiro_crew.instances.token_mint import TokenMintError

        async def mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            if "bad" in host:
                raise TokenMintError("unreachable")
            return "SECRET_TOK"

        reg, mgr = self._mgr(tmp_path, mint=mint)
        reg.add(name="Good", ssh_host="host-good", instance_id="good", remote_port=7777)
        reg.add(name="Bad", ssh_host="host-bad", instance_id="bad", remote_port=7778)
        reg.update("good", was_connected=True)
        reg.update("bad", was_connected=True)

        # One unreachable host must NOT abort the rest or raise.
        await server._revive_intended_instances(reg, mgr)

        assert mgr.status("good").state == TunnelState.CONNECTED
        assert mgr.status("bad") is None  # mint failed -> no live tunnel
        # Intent preserved so the tab persists; retained error explains why.
        assert reg.get("bad").was_connected is True
        assert "token mint failed" in (mgr.last_error("bad") or "")
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_noop_when_none_intended(self, tmp_path):
        import kiro_crew.dashboard.server as server

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="A", ssh_host="host-a", instance_id="a")  # was_connected False

        await server._revive_intended_instances(reg, mgr)  # returns early, no raise
        assert mgr.status("a") is None

    @pytest.mark.asyncio
    async def test_instances_startup_schedules_revive_in_background(self, monkeypatch):
        """_instances_startup must NOT await the reconnect.

        on_startup handlers run during runner.setup(), before the HTTP port
        binds, so awaiting serial SSH-tunnel connects (each of which can hang
        for its full timeout when the network is down) delays the port bind
        past the desktop app's 30s gateway-wait window. Revive must be a
        tracked background task so the handler returns promptly.
        """
        import asyncio
        import types

        from aiohttp import web

        import kiro_crew.dashboard.server as server

        cfg = types.SimpleNamespace(
            instances=types.SimpleNamespace(
                enabled=True,
                tunnel_base_port=53400,
                ssh_compression=False,
                connect_timeout_secs=15.0,
                mint_timeout_secs=30.0,
                max_recovery_attempts=8,
                recover_backoff_max_secs=30.0,
                probe_failure_threshold=3,
            )
        )
        monkeypatch.setattr(server, "KiroCrewConfig", types.SimpleNamespace(load=lambda: cfg))
        monkeypatch.setattr(server, "InstancesRegistry", lambda: object())
        monkeypatch.setattr(server, "SshTunnelManager", lambda *a, **k: object())

        started = asyncio.Event()
        release = asyncio.Event()

        async def _blocking_revive(registry, manager):
            started.set()
            await release.wait()  # simulate a hung SSH connect that never returns

        monkeypatch.setattr(server, "_revive_intended_instances", _blocking_revive)

        app = web.Application()
        state = types.SimpleNamespace(
            _background_tasks=set(), instances_registry=None, instances_manager=None
        )
        server._register_instances_hooks(app, state, 5476)
        startup_handler = list(app.on_startup)[-1]

        # Must return promptly even though revive never completes.
        await asyncio.wait_for(startup_handler(app), timeout=2.0)

        # Revive was scheduled as a tracked background task, not awaited.
        assert len(state._background_tasks) == 1
        await asyncio.wait_for(started.wait(), timeout=2.0)  # it did start in the bg

        # Cleanup: release the hung revive and drain the task.
        release.set()
        for t in list(state._background_tasks):
            await asyncio.wait_for(t, timeout=2.0)


# ── SSM connection method ──────────────────────────────────────────────────


class TestSsmValidation:
    """Injection-safe validation of the SSM-transport inputs."""

    def test_valid_targets(self):
        from kiro_crew.instances.validation import validate_ssm_target

        assert validate_ssm_target("i-0123456789abcdef0") == "i-0123456789abcdef0"
        assert validate_ssm_target("mi-0123456789abcdef0") == "mi-0123456789abcdef0"
        assert validate_ssm_target("i-abcdef12") == "i-abcdef12"  # legacy 8-char id
        assert validate_ssm_target("  i-0123456789abcdef0  ") == "i-0123456789abcdef0"

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "i-",
            "x-0123456789abcdef0",  # wrong prefix
            "i-0123456789ABCDEF0",  # uppercase hex not used by AWS ids
            "i-0123456789abcdef0; rm -rf /",  # shell metacharacters
            "-i-0123456789abcdef0",  # option injection
            "i-0123456789abcdef0 --region evil",  # argv smuggling
            "$(whoami)",
        ],
    )
    def test_rejects_bad_targets(self, bad):
        from kiro_crew.instances.validation import SsmValidationError, validate_ssm_target

        with pytest.raises(SsmValidationError):
            validate_ssm_target(bad)

    def test_profile_and_region(self):
        from kiro_crew.instances.validation import (
            SsmValidationError,
            validate_aws_profile,
            validate_aws_region,
        )

        # Empty is allowed: "use the default chain / default region".
        assert validate_aws_profile("") == ""
        assert validate_aws_region("") == ""
        assert validate_aws_profile("my-profile_1.x") == "my-profile_1.x"
        # '+' is legal in profile names: IAM entity names permit it, and SSO
        # tooling derives "<account>+<permission-set>" shaped profiles.
        assert validate_aws_profile("AdminAccess+dev") == "AdminAccess+dev"
        assert validate_aws_region("us-east-1") == "us-east-1"
        assert validate_aws_region("us-gov-west-1") == "us-gov-west-1"
        # Option injection + metacharacters + bogus region shapes are refused.
        for bad in ("-oProxyCommand=x", "-dev", "a b", "a;b", "a$(b)", "a$b"):
            with pytest.raises(SsmValidationError):
                validate_aws_profile(bad)
        for bad in ("useast1", "US-EAST-1", "us-east-1; rm -rf /", "-us-east-1"):
            with pytest.raises(SsmValidationError):
                validate_aws_region(bad)

    def test_ssm_run_as_accepts_unix_usernames_and_defaults_when_empty(self):
        """Empty means "the default user", never an empty ``sudo -u``."""
        from kiro_crew.instances.validation import validate_ssm_run_as

        assert validate_ssm_run_as("ubuntu") == "ubuntu"
        assert validate_ssm_run_as("ec2-user") == "ec2-user"
        assert validate_ssm_run_as("_svc_01") == "_svc_01"
        assert validate_ssm_run_as("  ubuntu  ") == "ubuntu"
        # Empty / None fall back to the default rather than producing `sudo -u ''`.
        assert validate_ssm_run_as("") == "ec2-user"
        assert validate_ssm_run_as(None) == "ec2-user"  # type: ignore[arg-type]

    def test_ssm_run_as_rejects_injection_and_bad_usernames(self):
        """It is interpolated into `sudo -u <user> -i` on the remote box."""
        from kiro_crew.instances.validation import SsmValidationError, validate_ssm_run_as

        for bad in (
            "root; rm -rf /",
            "user name",
            "-oProxyCommand=x",
            "Ubuntu",  # uppercase is not a valid Unix username here
            "1user",  # must not start with a digit
            "us$er",
            "a" * 33,  # over the length cap
        ):
            with pytest.raises(SsmValidationError):
                validate_ssm_run_as(bad)


class TestSsmRegistry:
    """Registry support for connection_method + the SSM coordinate fields."""

    def _reg(self, tmp_path):
        from kiro_crew.instances.registry import InstancesRegistry

        return InstancesRegistry(path=tmp_path / "instances.json")

    def test_defaults_to_ssh_for_backcompat(self, tmp_path):
        reg = self._reg(tmp_path)
        inst = reg.add(name="Dev", ssh_host="dev-1")
        assert inst.connection_method == "ssh"
        assert inst.ssm_target == "" and inst.aws_profile == "" and inst.aws_region == ""

    def test_legacy_record_without_connection_method_loads_as_ssh(self, tmp_path):
        """A pre-SSM instances.json must keep working (defaults to ssh)."""
        import json

        path = tmp_path / "instances.json"
        path.write_text(
            json.dumps(
                {
                    "instances": [
                        {"id": "old", "name": "Old", "ssh_host": "old-host", "remote_port": 7777}
                    ],
                    "last_active_id": "old",
                }
            ),
            encoding="utf-8",
        )
        inst = self._reg(tmp_path).get("old")
        assert inst is not None
        assert inst.connection_method == "ssh"
        assert inst.ssh_host == "old-host"

    def test_add_ssm_instance(self, tmp_path):
        reg = self._reg(tmp_path)
        inst = reg.add(
            name="EC2 Box",
            connection_method="ssm",
            ssm_target="i-0123456789abcdef0",
            aws_profile="dev",
            aws_region="eu-west-2",
            remote_port=7777,
        )
        assert inst.connection_method == "ssm"
        assert inst.ssm_target == "i-0123456789abcdef0"
        assert inst.aws_profile == "dev" and inst.aws_region == "eu-west-2"
        # Round-trips through disk.
        reloaded = self._reg(tmp_path).get(inst.id)
        assert reloaded.connection_method == "ssm"
        assert reloaded.ssm_target == "i-0123456789abcdef0"

    def test_ssm_requires_target_and_ssh_requires_host(self, tmp_path):
        from kiro_crew.instances.registry import InvalidInstanceError

        reg = self._reg(tmp_path)
        # ssm without a target is invalid...
        with pytest.raises(InvalidInstanceError):
            reg.add(name="No target", connection_method="ssm")
        # ...and ssh without a host is still invalid.
        with pytest.raises(InvalidInstanceError):
            reg.add(name="No host", connection_method="ssh")
        # An unknown method is refused rather than silently treated as ssh.
        with pytest.raises(InvalidInstanceError):
            reg.add(name="Bogus", connection_method="telnet", ssh_host="h")

    def test_update_can_switch_method(self, tmp_path):
        reg = self._reg(tmp_path)
        reg.add(name="Dev", ssh_host="dev-1", instance_id="dev")
        u = reg.update("dev", connection_method="ssm", ssm_target="i-0123456789abcdef0")
        assert u.connection_method == "ssm"

    def test_no_aws_credentials_persisted(self, tmp_path):
        """Only the profile NAME may be stored — never a key/secret."""
        reg = self._reg(tmp_path)
        reg.add(
            name="EC2",
            connection_method="ssm",
            ssm_target="i-0123456789abcdef0",
            aws_profile="dev",
            instance_id="ec2",
        )
        raw = (tmp_path / "instances.json").read_text(encoding="utf-8")
        for marker in ("AKIA", "ASIA", "aws_secret_access_key", "aws_session_token"):
            assert marker not in raw

    def test_aws_profile_allows_plus_and_rejects_metacharacters(self, tmp_path):
        """The record check accepts '+' (SSO-derived profile names) but still
        refuses whitespace and shell metacharacters, mirroring validation.py."""
        from kiro_crew.instances.registry import InvalidInstanceError

        reg = self._reg(tmp_path)
        inst = reg.add(
            name="SSO box",
            connection_method="ssm",
            ssm_target="i-0123456789abcdef0",
            aws_profile="AdminAccess+dev",
            instance_id="sso",
        )
        assert inst.aws_profile == "AdminAccess+dev"
        assert reg.list()[0].aws_profile == "AdminAccess+dev"
        for i, bad in enumerate(("a b", "a;b", "a$(b)", "a$b")):
            with pytest.raises(InvalidInstanceError):
                reg.add(
                    name="bad profile",
                    connection_method="ssm",
                    ssm_target="i-0123456789abcdef0",
                    aws_profile=bad,
                    instance_id=f"bad-{i}",
                )

    def test_aws_profile_regex_is_single_sourced(self):
        """The registry's early record check aliases validation.py's pattern.

        There is exactly one AWS-profile charset: registry._AWS_PROFILE_RE is
        the SAME compiled object as validation._AWS_PROFILE_RE, so the two
        check sites cannot drift. If someone re-introduces a second copy this
        identity check fails even when the copies happen to be textually
        equal. Note the pattern accepts a leading '-' by design: option
        injection is blocked by the separate startswith('-') guard in
        validation.validate_aws_profile, not by the character class, and the
        empty "default chain" value is handled by the `if self.aws_profile`
        guard at the registry check site.
        """
        from kiro_crew.instances import registry, validation

        assert registry._AWS_PROFILE_RE is validation._AWS_PROFILE_RE

    def test_ssm_run_as_defaults_and_round_trips(self, tmp_path):
        """A record written before ssm_run_as existed must load as the default.

        Regression guard for the design-review finding: the remote user was
        hard-coded to ``ec2-user`` inside ``cloud.ssm.run_command``, so an Ubuntu
        AMI's mint failed. It is now a per-instance field — but an older registry
        file has no key at all, and a file with an explicit empty string would
        fail validation, so BOTH must resolve to the default.
        """
        from kiro_crew.instances.registry import Instance

        assert Instance.from_dict({"id": "a", "name": "A"}).ssm_run_as == "ec2-user"
        assert Instance.from_dict({"id": "a", "ssm_run_as": ""}).ssm_run_as == "ec2-user"
        assert Instance.from_dict({"id": "a", "ssm_run_as": "ubuntu"}).ssm_run_as == "ubuntu"

        reg = self._reg(tmp_path)
        inst = reg.add(
            name="Ubuntu box",
            connection_method="ssm",
            ssm_target="i-0123456789abcdef0",
            ssm_run_as="ubuntu",
            instance_id="ubu",
        )
        assert inst.ssm_run_as == "ubuntu"
        assert reg.list()[0].ssm_run_as == "ubuntu"
        assert "ssm_run_as" in inst.to_dict()

    def test_ssm_run_as_is_validated_on_add(self, tmp_path):
        """User input reaches `sudo -u` on the remote box, so it is validated."""
        from kiro_crew.instances.registry import InvalidInstanceError

        reg = self._reg(tmp_path)
        with pytest.raises(InvalidInstanceError):
            reg.add(
                name="bad",
                connection_method="ssm",
                ssm_target="i-0123456789abcdef0",
                ssm_run_as="root; rm -rf /",
                instance_id="bad",
            )

    def test_error_messages_carry_no_raw_regex(self, tmp_path):
        """Form errors must read in plain English, not as a regex.

        UX-review finding: the pattern flowed verbatim into the Settings form.
        """
        from kiro_crew.instances.registry import InvalidInstanceError

        reg = self._reg(tmp_path)
        with pytest.raises(InvalidInstanceError) as e:
            reg.add(
                name="bad",
                connection_method="ssm",
                ssm_target="not-an-id",
                instance_id="bad2",
            )
        msg = str(e.value)
        assert "^" not in msg and "[a-f0-9]" not in msg and "{8,17}" not in msg
        assert "hex digits" in msg


class TestSsmTunnelArgv:
    """The SSM port-forward argv (loopback-bound, no shell, no injected opts)."""

    @pytest.fixture(autouse=True)
    def _bare_resolver(self, monkeypatch):
        """Pin the shared aws-CLI resolver (#4770) to the bare name so the
        argv-shape assertions stay deterministic across hosts."""
        from kiro_crew.cloud import ssm

        monkeypatch.setattr(ssm, "resolve_aws_bin", lambda: "aws")

    def test_start_builds_argv_off_the_event_loop(self, monkeypatch):
        """The SSM branch's argv build resolves the aws CLI (#4770), which
        probes the filesystem — start() must run it in a worker thread, never
        on the gateway event loop (a stalled network mount on PATH would
        otherwise freeze every request)."""
        from kiro_crew.instances import ssh_tunnel_manager as stm
        from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel

        seen: dict = {}

        def probe_builder(*a, **k):
            try:
                asyncio.get_running_loop()
                seen["on_loop"] = True
            except RuntimeError:
                seen["on_loop"] = False
            return ["aws", "ssm", "start-session"]

        monkeypatch.setattr(stm, "_build_ssm_tunnel_argv", probe_builder)

        class FakeProc:
            returncode = None
            stderr = None
            pid = 4242

            def terminate(self):
                self.returncode = -15

            def kill(self):
                self.returncode = -9

            async def wait(self):
                return self.returncode

        async def fake_exec(*a, **k):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

        async def main():
            t = _SshTunnel(
                "cd-1",
                "",
                7778,
                7777,
                transport="ssm",
                ssm_target="i-0123456789abcdef0",
            )

            async def _reachable():
                return True

            t._port_reachable = _reachable
            ok = await t.start()
            assert ok
            await t.stop()

        asyncio.run(main())
        assert seen["on_loop"] is False  # built in a worker thread, not on the loop

    def test_argv_shape(self):
        from kiro_crew.instances.ssh_tunnel_manager import _build_ssm_tunnel_argv

        argv = _build_ssm_tunnel_argv(
            "i-0123456789abcdef0", 7777, 7777, profile="dev", region="eu-west-2"
        )
        assert argv[:3] == ["aws", "ssm", "start-session"]
        assert "--target" in argv and "i-0123456789abcdef0" in argv
        assert "AWS-StartPortForwardingSession" in argv
        assert "portNumber=7777,localPortNumber=7777" in argv
        assert argv[argv.index("--region") + 1] == "eu-west-2"
        assert argv[argv.index("--profile") + 1] == "dev"
        # argv list => no shell; nothing is a single concatenated string.
        assert all(isinstance(a, str) for a in argv)

    def test_omits_empty_profile_and_region(self):
        from kiro_crew.instances.ssh_tunnel_manager import _build_ssm_tunnel_argv

        argv = _build_ssm_tunnel_argv("i-0123456789abcdef0", 7777, 7777)
        assert "--profile" not in argv and "--region" not in argv

    def test_ssh_argv_unchanged_for_ssh_instances(self):
        """Regression guard: the SSH argv must not gain SSM flags."""
        from kiro_crew.instances.ssh_tunnel_manager import _build_ssh_tunnel_argv

        argv = _build_ssh_tunnel_argv("dev-1", 7777, 7777)
        assert argv[0] == "ssh" and "-N" in argv
        assert argv[-1] == "dev-1"
        assert "-L" in argv
        assert argv[argv.index("-L") + 1] == "127.0.0.1:7777:127.0.0.1:7777"
        assert "ssm" not in argv


class TestSsmTunnelProcessGroup:
    """Cross-platform teardown of the aws wrapper + session-manager-plugin.

    The plugin grandchild is what holds the forwarded port, so a teardown that
    signals only the ``aws`` wrapper wedges the port. GPT/design review found the
    original code used bare ``start_new_session=True`` and raw
    ``os.killpg``/``os.getpgid`` — both POSIX-only — so on native Windows (a
    supported platform) the plugin orphaned. Everything must route through
    ``platform_compat``.
    """

    def _tunnel(self, transport):
        from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel

        return _SshTunnel(
            instance_id="i1",
            ssh_host="dev-1",
            local_port=7777,
            remote_port=7777,
            transport=transport,
            ssm_target="i-0123456789abcdef0" if transport == "ssm" else "",
        )

    @pytest.mark.asyncio
    async def test_ssm_spawn_passes_both_isolation_kwargs(self, monkeypatch):
        """POSIX gets setsid; Windows gets CREATE_NEW_PROCESS_GROUP."""
        import kiro_crew.instances.ssh_tunnel_manager as mod

        seen = {}

        async def fake_exec(*argv, **kw):
            seen.update(kw)
            raise OSError("stop here — we only care about the spawn kwargs")

        monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(mod.platform_compat, "IS_POSIX", True)
        monkeypatch.setattr(mod.platform_compat, "CREATE_NEW_PROCESS_GROUP", 0x200)
        await self._tunnel("ssm").start()
        assert seen["start_new_session"] is True
        assert seen["creationflags"] == 0x200

        # Same call on Windows: no setsid (it is silently ignored there), but the
        # creation flag is what makes the tree taskkill /T-reapable.
        seen.clear()
        monkeypatch.setattr(mod.platform_compat, "IS_POSIX", False)
        await self._tunnel("ssm").start()
        assert seen["start_new_session"] is False
        assert seen["creationflags"] == 0x200

    @pytest.mark.asyncio
    async def test_ssh_spawn_gets_no_process_group(self, monkeypatch):
        """Regression guard: the SSH transport's spawn is unchanged."""
        import kiro_crew.instances.ssh_tunnel_manager as mod

        seen = {}

        async def fake_exec(*argv, **kw):
            seen.update(kw)
            raise OSError("stop")

        monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(mod.platform_compat, "IS_POSIX", True)
        await self._tunnel("ssh").start()
        assert seen["start_new_session"] is False
        assert seen["creationflags"] == 0

    @pytest.mark.asyncio
    async def test_ssm_child_gets_plugin_search_path_and_ssh_inherits(self, monkeypatch, tmp_path):
        """The SSM child needs a PATH that can find session-manager-plugin.

        The argv head is resolved absolutely, but the aws CLI then looks the
        plugin up BY NAME on this child's own PATH — under a GUI-launched gateway
        the minimal launchd one — so the tunnel died inside a correctly resolved
        ``aws`` (#5392). SSH keeps ``env=None`` (inherit): its binary lives in
        the system bin dir and widening a tunnel child's PATH without a reason to
        is the opposite of what this fix argues for.
        """
        import kiro_crew.instances.ssh_tunnel_manager as mod
        from kiro_crew.deploy import engine

        seen = {}

        async def fake_exec(*argv, **kw):
            seen.update(kw)
            raise OSError("stop here — we only care about the spawn kwargs")

        # tmp_path stand-ins: a host path literal would flake and is unrunnable
        # on Windows, which this class deliberately also exercises. `aws` sits on
        # the inherited PATH so the head resolves absolutely (a PATH hit needs no
        # provenance check), which is what makes the widening applicable. Windows
        # resolves executables by PATHEXT rather than the exec bit, so the planted
        # file differs there — otherwise the head falls back to the bare name and
        # the widening is (correctly) withheld.
        inherited = tmp_path / "sysbin"
        inherited.mkdir()
        if os.name == "nt":
            fake_aws = inherited / "aws.cmd"
            fake_aws.write_text("@echo off\n")
            monkeypatch.setenv("PATHEXT", ".cmd")
        else:
            fake_aws = inherited / "aws"
            fake_aws.write_text("#!/bin/sh\n")
            fake_aws.chmod(0o755)
        install_dir = tmp_path / "install"
        monkeypatch.setenv("PATH", str(inherited))
        monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(install_dir),))
        monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(mod.platform_compat, "IS_POSIX", True)

        await self._tunnel("ssm").start()
        child_path = seen["env"]["PATH"].split(os.pathsep)
        assert str(install_dir) in child_path
        # Appended: the inherited PATH still wins every name it can resolve.
        assert child_path.index(str(inherited)) < child_path.index(str(install_dir))

        seen.clear()
        await self._tunnel("ssh").start()
        assert seen["env"] is None

    def test_teardown_routes_through_the_platform_shim(self, monkeypatch):
        """Not raw os.killpg — that leaves the plugin alive on Windows."""
        import kiro_crew.instances.ssh_tunnel_manager as mod

        calls = []
        monkeypatch.setattr(
            mod.platform_compat,
            "kill_process_tree",
            lambda pid, sig: (calls.append((pid, sig)), True)[1],
        )
        assert self._tunnel("ssm")._signal_group(4321, 15) is True
        assert calls == [(4321, 15)]

    def test_teardown_reports_undelivered_instead_of_raising(self, monkeypatch):
        """The shim propagates; a failure must degrade to the single-proc kill."""
        import kiro_crew.instances.ssh_tunnel_manager as mod

        for exc in (ProcessLookupError, PermissionError, OSError, ValueError):

            def boom(pid, sig, _e=exc):
                raise _e("nope")

            monkeypatch.setattr(mod.platform_compat, "kill_process_tree", boom)
            assert self._tunnel("ssm")._signal_group(4321, 15) is False


class TestSsmTransportSelection:
    """The manager must drive the transport each instance is configured for."""

    @pytest.fixture(autouse=True)
    def _free_ports(self, monkeypatch):
        # These tests assert which TRANSPORT the manager selects (ssh vs ssm) via
        # _FakeTunnel; they are not about real local-port availability. connect()
        # probes the real _is_port_free (CSE SEC-016 mirror-conflict check), and
        # on a busy CI shard the fixed ports below (53510-53513) can already be
        # bound -- connect() then returns an error status WITHOUT registering the
        # tunnel, so `mgr._tunnels[id]` raises KeyError and the test flakes. Stub
        # the probe to always-free, exactly as the other SshTunnelManager test
        # classes do, so transport selection is tested deterministically.
        _patch_port_probe(monkeypatch)

    def _mgr(self, tmp_path, *, mint=None, connect_timeout_secs=None):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        async def ok_mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            return "SSH_TOKEN"

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        manager_kwargs = {}
        if connect_timeout_secs is not None:
            manager_kwargs["connect_timeout_secs"] = connect_timeout_secs
        return reg, SshTunnelManager(
            reg,
            base_port=53500,
            mint_token=mint or ok_mint,
            tunnel_factory=_FakeTunnel,
            **manager_kwargs,
        )

    @pytest.mark.parametrize(
        ("configured", "ssh_expected", "ssm_expected"),
        [
            (None, 15.0, 25.0),
            (15.0, 15.0, 15.0),
            (45.0, 45.0, 45.0),
            (0.0, 15.0, 25.0),
            (200.0, 120.0, 120.0),
        ],
    )
    def test_connect_timeout_matrix(self, tmp_path, configured, ssh_expected, ssm_expected):
        from kiro_crew.config.loader import InstancesConfig
        from kiro_crew.instances.constants import (
            CONNECT_TIMEOUT_CEILING_SECS,
            DEFAULT_CONNECT_TIMEOUT_SECS,
            DEFAULT_SSM_CONNECT_TIMEOUT_SECS,
        )

        assert DEFAULT_CONNECT_TIMEOUT_SECS == 15.0
        assert DEFAULT_SSM_CONNECT_TIMEOUT_SECS == 25.0
        assert CONNECT_TIMEOUT_CEILING_SECS == 120.0

        config = InstancesConfig(connect_timeout_secs=configured)
        _, mgr = self._mgr(
            tmp_path,
            connect_timeout_secs=config.connect_timeout_secs,
        )
        assert mgr._connect_timeout_for("ssh") == ssh_expected
        assert mgr._connect_timeout_for("ssm") == ssm_expected

    @pytest.mark.asyncio
    async def test_ssh_instance_uses_ssh_transport(self, tmp_path):
        reg, mgr = self._mgr(tmp_path)
        reg.add(name="Dev", ssh_host="dev-1", instance_id="dev", remote_port=53510)
        await mgr.connect("dev")
        tunnel = mgr._tunnels["dev"]
        assert tunnel.transport == "ssh"
        assert tunnel.ssh_host == "dev-1"
        assert mgr.get_token("dev") == "SSH_TOKEN"
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_ssm_instance_uses_ssm_transport_and_ssm_mint(self, tmp_path, monkeypatch):
        import kiro_crew.instances.ssh_tunnel_manager as mod
        from kiro_crew.instances.constants import DEFAULT_SSM_CONNECT_TIMEOUT_SECS

        seen = {}

        async def fake_ssm_mint(target, **kwargs):
            seen["target"] = target
            seen.update(kwargs)
            return "SSM_TOKEN"

        monkeypatch.setattr(mod, "mint_remote_token_ssm", fake_ssm_mint)
        # The plugin presence check must not gate the unit test.
        monkeypatch.setattr("kiro_crew.cloud.ssm.session_manager_plugin_installed", lambda: True)

        reg, mgr = self._mgr(tmp_path)
        reg.add(
            name="EC2",
            connection_method="ssm",
            ssm_target="i-0123456789abcdef0",
            aws_profile="dev",
            aws_region="eu-west-2",
            instance_id="ec2",
            remote_port=53511,
        )
        await mgr.connect("ec2")

        tunnel = mgr._tunnels["ec2"]
        assert tunnel.transport == "ssm"
        assert tunnel.ssm_target == "i-0123456789abcdef0"
        assert tunnel.aws_profile == "dev" and tunnel.aws_region == "eu-west-2"
        assert tunnel.connect_timeout_secs == DEFAULT_SSM_CONNECT_TIMEOUT_SECS == 25.0
        # Token came from the SSM mint (NOT the ssh mint seam).
        assert mgr.get_token("ec2") == "SSM_TOKEN"
        assert seen["target"] == "i-0123456789abcdef0"
        assert seen["aws_profile"] == "dev" and seen["aws_region"] == "eu-west-2"
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_mint_timeout_threads_to_ssh_mint(self, tmp_path):
        """A configured instances.mint_timeout_secs reaches the ssh mint call."""
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        seen = {}

        async def capturing_mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            seen["timeout_secs"] = timeout_secs
            return "TOK"

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        mgr = SshTunnelManager(
            reg,
            base_port=53520,
            mint_timeout_secs=77.0,
            mint_token=capturing_mint,
            tunnel_factory=_FakeTunnel,
        )
        reg.add(name="Dev", ssh_host="dev-1", instance_id="dev", remote_port=53521)
        await mgr.connect("dev")
        assert seen["timeout_secs"] == 77.0
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_mint_timeout_ssm_default_and_override(self, tmp_path, monkeypatch):
        """SSM mint keeps its higher default; an explicit override wins for it too."""
        import kiro_crew.instances.ssh_tunnel_manager as mod
        from kiro_crew.instances.constants import DEFAULT_SSM_MINT_TIMEOUT_SECS
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        seen = {}

        async def fake_ssm_mint(target, **kwargs):
            seen["timeout_secs"] = kwargs.get("timeout_secs")
            return "SSM_TOKEN"

        monkeypatch.setattr(mod, "mint_remote_token_ssm", fake_ssm_mint)
        monkeypatch.setattr("kiro_crew.cloud.ssm.session_manager_plugin_installed", lambda: True)

        def add_ssm(reg, iid, port):
            reg.add(
                name=iid,
                connection_method="ssm",
                ssm_target="i-0123456789abcdef0",
                aws_profile="dev",
                aws_region="eu-west-2",
                instance_id=iid,
                remote_port=port,
            )

        # Default manager -> SSM mint gets the higher SSM default (90s).
        reg = InstancesRegistry(path=tmp_path / "a.json")
        mgr = SshTunnelManager(reg, base_port=53530, tunnel_factory=_FakeTunnel)
        add_ssm(reg, "ec2a", 53531)
        await mgr.connect("ec2a")
        assert seen["timeout_secs"] == DEFAULT_SSM_MINT_TIMEOUT_SECS == 90.0
        await mgr.shutdown()

        # Explicit override wins for the SSM transport too.
        reg2 = InstancesRegistry(path=tmp_path / "b.json")
        mgr2 = SshTunnelManager(
            reg2, base_port=53540, mint_timeout_secs=45.0, tunnel_factory=_FakeTunnel
        )
        add_ssm(reg2, "ec2b", 53541)
        await mgr2.connect("ec2b")
        assert seen["timeout_secs"] == 45.0
        await mgr2.shutdown()

    @pytest.mark.asyncio
    async def test_plugin_probe_runs_off_the_event_loop(self, tmp_path, monkeypatch):
        """#5392: the prerequisite probe must not block the gateway event loop.

        The probe resolves the plugin through the deploy engine's shared resolver
        — PATH scan, then the well-known install dirs, then executable-provenance
        validation — so a stalled network mount on any of those would freeze every
        request and heartbeat. Pinned by the THREAD it actually runs on rather
        than by source inspection, so an edit that drops the offload fails here
        even if it keeps the wording.
        """
        import threading

        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        loop_thread = threading.get_ident()
        ran_on: dict = {}

        def _probe():
            ran_on["thread"] = threading.get_ident()
            return False  # short-circuit: no tunnel spawn, error status asserted

        monkeypatch.setattr("kiro_crew.cloud.ssm.session_manager_plugin_installed", _probe)
        reg, mgr = self._mgr(tmp_path)
        reg.add(
            name="EC2",
            connection_method="ssm",
            ssm_target="i-0123456789abcdef0",
            instance_id="ec2",
            remote_port=53514,
        )

        st = await mgr.connect("ec2")

        assert st.state == TunnelState.ERROR
        assert ran_on["thread"] != loop_thread
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_ssm_connect_fails_clean_without_plugin(self, tmp_path, monkeypatch):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        monkeypatch.setattr("kiro_crew.cloud.ssm.session_manager_plugin_installed", lambda: False)
        reg, mgr = self._mgr(tmp_path)
        reg.add(
            name="EC2",
            connection_method="ssm",
            ssm_target="i-0123456789abcdef0",
            instance_id="ec2",
            remote_port=53512,
        )
        st = await mgr.connect("ec2")
        assert st.state == TunnelState.ERROR
        assert "session-manager-plugin" in st.error
        # No tunnel was spawned for a missing prerequisite.
        assert mgr.status("ec2") is None
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_invalid_ssm_target_surfaces_error_without_spawn(self, tmp_path, monkeypatch):
        """A registry record hand-edited to a bad target must not reach argv."""
        import dataclasses

        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = self._mgr(tmp_path)
        reg.add(
            name="EC2",
            connection_method="ssm",
            ssm_target="i-0123456789abcdef0",
            instance_id="ec2",
            remote_port=53513,
        )
        # Simulate a hand-edited instances.json that bypassed registry validation:
        # the authoritative guard is the tunnel manager's pre-argv validation.
        tampered = dataclasses.replace(reg.get("ec2"), ssm_target="-oProxyCommand=evil")
        monkeypatch.setattr(mgr._registry, "get", lambda iid: tampered if iid == "ec2" else None)
        st = await mgr.connect("ec2")
        assert st.state == TunnelState.ERROR
        assert "invalid" in st.error.lower()
        await mgr.shutdown()


class TestSsmDiagnostics:
    """The SSM diagnosis ladder reports the first broken link, SSM-worded."""

    @pytest.mark.asyncio
    async def test_unreachable_node_is_first_rung(self, monkeypatch):
        import kiro_crew.instances.diagnostics as diag

        async def no_node(*a, **k):
            return False

        monkeypatch.setattr(diag, "_probe_ssm_managed", no_node)
        res = await diag.diagnose_instance_ssm("i-0123456789abcdef0", 7777, 7777)
        assert res.code == diag.SSM_UNREACHABLE
        assert res.ok is False
        # Must NOT tell an SSM user to check SSH access.
        assert "ssh" not in res.reason.lower()

    @pytest.mark.asyncio
    async def test_remote_down_then_not_connected_then_ok(self, monkeypatch):
        import kiro_crew.instances.diagnostics as diag

        async def node_ok(*a, **k):
            return True

        monkeypatch.setattr(diag, "_probe_ssm_managed", node_ok)

        async def dash_down(*a, **k):
            return False

        monkeypatch.setattr(diag, "_probe_remote_dashboard_ssm", dash_down)
        res = await diag.diagnose_instance_ssm("i-0123456789abcdef0", 7777, 7777)
        assert res.code == diag.REMOTE_DOWN

        async def dash_ok(*a, **k):
            return True

        monkeypatch.setattr(diag, "_probe_remote_dashboard_ssm", dash_ok)
        # local_port == 0 -> never connected (not a broken tunnel)
        res = await diag.diagnose_instance_ssm("i-0123456789abcdef0", 7777, 0)
        assert res.code == diag.NOT_CONNECTED

        async def fwd_ok(_lp):
            return True

        monkeypatch.setattr(diag, "_probe_local_forward", fwd_ok)
        res = await diag.diagnose_instance_ssm("i-0123456789abcdef0", 7777, 7777)
        assert res.code == diag.OK and res.ok is True

    @pytest.mark.asyncio
    async def test_invalid_target_short_circuits(self):
        import kiro_crew.instances.diagnostics as diag

        res = await diag.diagnose_instance_ssm("-oProxyCommand=evil", 7777, 7777)
        assert res.code == diag.UNKNOWN
        assert res.probes == []


class TestSsmExitErrorClassification:
    """SSM stderr must be classified with SSM vocabulary, not ssh's."""

    def _tunnel(self, stderr):
        from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel

        t = _SshTunnel("ec2", "", 7777, 7777, transport="ssm", ssm_target="i-0123456789abcdef0")
        t._stderr_buf = stderr
        return t

    @pytest.mark.parametrize(
        "stderr,expected",
        [
            ("An error occurred (AccessDeniedException) ...", "IAM denied ssm:StartSession"),
            ("The security token included in the request is expired", "credentials missing"),
            ("SessionManagerPlugin is not found", "session-manager-plugin is not installed"),
            ("TargetNotConnected: i-0 is not connected", "not a connected managed node"),
        ],
    )
    def test_classification(self, stderr, expected):
        t = self._tunnel(stderr)
        msg = t._exit_error(255)
        assert expected.lower() in msg.lower()
        # Never mislabels an SSM failure as an ssh auth problem.
        assert "ssh auth failed" not in msg

    def test_probe_failure_message_shared(self):
        t = self._tunnel("")
        t._probe_failed = True
        assert "health probe failed" in t._exit_error(0)

    def test_ssh_classification_unchanged(self):
        """Regression guard: the ssh classifier still owns ssh instances."""
        from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel

        t = _SshTunnel("dev", "dev-1", 7777, 7777)
        t._stderr_buf = "Permission denied (publickey)."
        assert "ssh auth failed" in t._exit_error(255)


# ── #5235: hard-kill-orphaned forwarder reclaim (pid + exact-argv guard) ────


class TestForwarderPidHints:
    """The registry pid hint lifecycle: recorded on connect, moved by recovery,
    cleared by disconnect. Fake tunnels; no real processes or ports."""

    @pytest.fixture(autouse=True)
    def _free_ports(self, monkeypatch):
        import kiro_crew.instances.ssh_tunnel_manager as stm

        monkeypatch.setattr(stm, "_is_port_free", lambda port, host="127.0.0.1": True)

    def _mgr(self, tmp_path, *, factory=_FakeTunnel):
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
            reg, base_port=54200, mint_token=ok_mint, tunnel_factory=factory
        )

    def test_registry_field_default_roundtrip_and_validation(self, tmp_path):
        from kiro_crew.instances.registry import Instance, InvalidInstanceError

        # Older registry files have no keys -> sentinel defaults.
        old = Instance.from_dict({"id": "a", "name": "A"})
        assert old.forwarder_pid == 0
        assert old.forwarder_start == ""
        # A hand-edited negative pid normalizes to the sentinel instead of
        # poisoning every later update() with a validation error.
        assert Instance.from_dict({"id": "a", "name": "A", "forwarder_pid": -7}).forwarder_pid == 0
        inst = Instance(
            id="a", name="A", ssh_host="host-a", forwarder_pid=4321, forwarder_start="12345"
        )
        d = inst.to_dict()
        assert d["forwarder_pid"] == 4321
        assert d["forwarder_start"] == "12345"
        loaded = Instance.from_dict(d)
        assert loaded.forwarder_pid == 4321
        assert loaded.forwarder_start == "12345"
        # A pid can never be negative; the sentinel 0 is the floor.
        bad = Instance(id="a", name="A", ssh_host="host-a", forwarder_pid=-1)
        with pytest.raises(InvalidInstanceError):
            bad.validate()
        bad_start = Instance(id="a", name="A", ssh_host="host-a", forwarder_start=123)  # type: ignore[arg-type]
        with pytest.raises(InvalidInstanceError):
            bad_start.validate()

    @pytest.mark.asyncio
    async def test_connect_persists_forwarder_identity(self, tmp_path, monkeypatch):
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        # A REAL pid (our own), so the recorded start-time identity is the
        # genuine platform value rather than "".
        my_pid = os.getpid()
        key = b"k" * 32
        monkeypatch.setattr(stm, "_reclaim_identity_key", lambda: key)

        def factory(*a, **k):
            t = _FakeTunnel(*a, **k)
            t.pid = my_pid
            return t

        reg, mgr = self._mgr(tmp_path, factory=factory)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        assert (await mgr.connect("cd-1")).state == TunnelState.CONNECTED
        inst = reg.get("cd-1")
        assert inst.forwarder_pid == my_pid
        assert inst.forwarder_start == (pc.process_start_time(my_pid) or "")
        assert inst.forwarder_start != ""  # readable for a live process we own
        assert inst.local_port > 0
        # The identity is signed with the gateway's key, bound to this
        # instance, pid, start, and port.
        assert inst.forwarder_sig == stm._forwarder_identity_sig(
            key, "cd-1", my_pid, inst.forwarder_start, inst.local_port
        )

    @pytest.mark.asyncio
    async def test_disconnect_clears_forwarder_identity_with_local_port(self, tmp_path):
        def factory(*a, **k):
            t = _FakeTunnel(*a, **k)
            t.pid = os.getpid()
            return t

        reg, mgr = self._mgr(tmp_path, factory=factory)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")
        assert reg.get("cd-1").forwarder_pid == os.getpid()
        await mgr.disconnect("cd-1")
        inst = reg.get("cd-1")
        # One atomic reset: a freed port is not reserved forever, and a stale
        # identity never even reaches a later reclaim's checks.
        assert inst.local_port == 0
        assert inst.forwarder_pid == 0
        assert inst.forwarder_start == ""
        assert inst.forwarder_sig == ""

    @pytest.mark.asyncio
    async def test_mark_recovered_refreshes_forwarder_identity(self, tmp_path):
        """A rebuild replaces the child; the recorded identity must move with
        it, or the replacement leaks unrecorded at the next hard-kill."""
        from kiro_crew import platform_compat as pc

        def factory(*a, **k):
            t = _FakeTunnel(*a, **k)
            t.pid = 54321  # dead pid: start-time identity records as ""
            return t

        reg, mgr = self._mgr(tmp_path, factory=factory)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")
        assert reg.get("cd-1").forwarder_pid == 54321
        assert reg.get("cd-1").forwarder_start == ""
        mgr._tunnels["cd-1"].pid = os.getpid()  # the rebuilt child's pid
        await mgr._mark_recovered("cd-1")
        inst = reg.get("cd-1")
        assert inst.forwarder_pid == os.getpid()
        assert inst.forwarder_start == (pc.process_start_time(os.getpid()) or "")
        assert inst.was_connected is True


class TestOrphanForwarderReclaim:
    """End-to-end reclaim behavior against REAL processes holding REAL ports.

    The reclaim path (``_reclaim_orphan_forwarder``) is keyed on the recorded
    pid behind a strict exact-argv identity check. These tests spawn a real
    child bound to a real loopback port and drive a real ``connect()``:

    * the leaked-forwarder case proves the child is terminated and its port
      released (identity confirmed via the real /proc//ps argv read);
    * the #1972 regression cases prove a process this manager did not spawn is
      NEVER signalled — whether its pid is recorded (pid recycled), unrecorded,
      or its port is not even occupied.

    ``_is_port_free`` stays REAL here (unlike the fake-port manager tests):
    occupancy of the holder's port is the trigger under test.
    """

    def _mgr(self, tmp_path, *, base_port):
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
            reg, base_port=base_port, mint_token=ok_mint, tunnel_factory=_FakeTunnel
        )

    def _spawn_port_holder(self):
        """Spawn a real child LISTENing on a free loopback port.

        Returns ``(proc, port, argv)``. Readiness is signalled over stdout so
        the bind (and /proc argv population) cannot be raced. A daemon reaper
        thread ``wait()``s the child so that, once signalled, it disappears
        immediately instead of lingering as a zombie — mirroring production,
        where the leaked forwarder's parent is dead and init reaps it.

        The child's parent is the TEST process, i.e. a LIVE parent: the
        reclaim's orphan gate refuses it as-is (which the live-parent test
        pins with the real ``get_ppid``). Tests that exercise the gates
        BEHIND the orphan gate monkeypatch ``get_ppid`` to 1 for their own
        scope — a real double-fork orphan would leave an unsupervised process
        behind on a test crash and is exactly the residue the suite forbids.
        """
        code = (
            "import socket, sys, time\n"
            "s = socket.socket()\n"
            "s.bind(('127.0.0.1', 0))\n"
            "s.listen(1)\n"
            "sys.stdout.write('%d\\n' % s.getsockname()[1])\n"
            "sys.stdout.flush()\n"
            "time.sleep(120)\n"
        )
        argv = [sys.executable, "-c", code]
        proc = subprocess.Popen(
            argv,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            line = proc.stdout.readline()
            port = int(line.strip())
        except Exception:
            proc.kill()
            raise
        threading.Thread(target=proc.wait, daemon=True).start()
        return proc, port, argv

    @staticmethod
    def _fake_orphan(monkeypatch):
        """Make the orphan gate see every pid as init-reparented (test scope)."""
        from kiro_crew import platform_compat as pc

        monkeypatch.setattr(pc, "get_ppid", lambda pid: 1)

    _TEST_IDENTITY_KEY = b"k" * 32

    @classmethod
    def _pin_identity_key(cls, monkeypatch):
        """Pin the reclaim signing key and return a sig factory.

        The real key derives from the SEL trust root, which a test home does
        not initialize; pinning a fixed key keeps the MAC math (and its
        compare_digest gate) fully real while making signatures computable by
        the test exactly the way the pre-kill gateway would have.
        """
        import kiro_crew.instances.ssh_tunnel_manager as stm

        monkeypatch.setattr(stm, "_reclaim_identity_key", lambda: cls._TEST_IDENTITY_KEY)

        def sign(instance_id, pid, start, port):
            return stm._forwarder_identity_sig(
                cls._TEST_IDENTITY_KEY, instance_id, pid, start, port
            )

        return sign

    @staticmethod
    def _cleanup(proc):
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    @pytest.mark.skipif(
        sys.platform != "linux",
        reason="positive reclaim needs the python stand-in's kernel argv to equal "
        "the spawn argv; macOS framework python re-execs (rewriting argv[0]) and "
        "Windows fails closed by design — production targets (ssh/aws binaries) "
        "do not re-exec",
    )
    @pytest.mark.asyncio
    async def test_hard_kill_leaked_forwarder_is_reclaimed_by_pid(self, tmp_path, monkeypatch):
        """hard-kill -> restart -> reconnect: the recorded child is terminated
        and its port released; connect proceeds on a fresh port."""
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc
        from kiro_crew.instances.port_allocator import _is_port_free
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        proc, port, argv = self._spawn_port_holder()
        try:
            # The identity check compares the recorded pid's REAL argv against
            # the command line the manager would construct. The manager builds
            # ssh argv; the leaked stand-in is a python child — point the
            # builder at the stand-in's exact argv so the real /proc read and
            # the element-wise comparison are exercised end to end. The orphan
            # gate is faked open (the stand-in's parent is this test).
            monkeypatch.setattr(
                stm,
                "_build_ssh_tunnel_argv",
                lambda host, lp, rp, compression=True: list(argv),
            )
            self._fake_orphan(monkeypatch)
            sign = self._pin_identity_key(monkeypatch)
            start = pc.process_start_time(proc.pid)
            assert start, "test needs a readable start-time identity"
            reg, mgr = self._mgr(tmp_path, base_port=54300)
            reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
            # What the pre-kill gateway persisted: port + signed child identity.
            reg.update(
                "cd-1",
                local_port=port,
                forwarder_pid=proc.pid,
                forwarder_start=start,
                forwarder_sig=sign("cd-1", proc.pid, start, port),
                was_connected=True,
            )

            st = await mgr.connect("cd-1")

            assert st.state == TunnelState.CONNECTED
            # (a) the old forwarder process is no longer alive…
            assert proc.wait(timeout=10) is not None
            # …and its port is released.
            deadline = time.monotonic() + 5.0
            while not _is_port_free(port) and time.monotonic() < deadline:
                time.sleep(0.05)
            assert _is_port_free(port), "reclaimed forwarder's port was not released"
            # The connect allocated around the (still-reserved) recorded port.
            inst = reg.get("cd-1")
            assert inst.local_port != port
        finally:
            self._cleanup(proc)

    @pytest.mark.skipif(
        sys.platform != "linux",
        reason="positive reclaim needs the python stand-in's kernel argv to equal "
        "the spawn argv; macOS framework python re-execs and Windows fails closed",
    )
    @pytest.mark.asyncio
    async def test_sigterm_ignoring_forwarder_is_sigkill_escalated(self, tmp_path, monkeypatch):
        """A verified leaked forwarder that ignores SIGTERM is SIGKILLed after
        the grace (identity re-confirmed before the destructive escalation)."""
        import signal as signal_mod

        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        code = (
            "import signal, socket, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "s = socket.socket()\n"
            "s.bind(('127.0.0.1', 0))\n"
            "s.listen(1)\n"
            "sys.stdout.write('%d\\n' % s.getsockname()[1])\n"
            "sys.stdout.flush()\n"
            "time.sleep(120)\n"
        )
        argv = [sys.executable, "-c", code]
        proc = subprocess.Popen(
            argv, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        try:
            port = int(proc.stdout.readline().strip())
            threading.Thread(target=proc.wait, daemon=True).start()
            monkeypatch.setattr(
                stm,
                "_build_ssh_tunnel_argv",
                lambda host, lp, rp, compression=True: list(argv),
            )
            self._fake_orphan(monkeypatch)
            sign = self._pin_identity_key(monkeypatch)
            # Keep the TERM grace short so the escalation happens quickly.
            monkeypatch.setattr(stm, "_RECLAIM_TERM_GRACE_SECS", 0.3)
            start = pc.process_start_time(proc.pid)
            assert start
            reg, mgr = self._mgr(tmp_path, base_port=54700)
            reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
            reg.update(
                "cd-1",
                local_port=port,
                forwarder_pid=proc.pid,
                forwarder_start=start,
                forwarder_sig=sign("cd-1", proc.pid, start, port),
                was_connected=True,
            )

            st = await mgr.connect("cd-1")

            assert st.state == TunnelState.CONNECTED
            rc = proc.wait(timeout=10)
            assert rc is not None
            # SIGTERM was ignored by the child, so death proves the SIGKILL
            # escalation ran (exit by signal reports negative on POSIX).
            assert rc == -signal_mod.SIGKILL
        finally:
            self._cleanup(proc)

    @pytest.mark.asyncio
    async def test_unsigned_or_forged_record_is_never_signalled(self, tmp_path, monkeypatch):
        """Forged-registry regression (agent-writable instances.json): a record
        with fully matching process attributes but WITHOUT the gateway's MAC —
        missing or wrong — authorizes nothing, even with the orphan gate open
        and the argv matching. Only the gateway can mint the signature (its
        key derives from the deny-listed SEL trust root)."""
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        proc, port, argv = self._spawn_port_holder()
        try:
            monkeypatch.setattr(
                stm,
                "_build_ssh_tunnel_argv",
                lambda host, lp, rp, compression=True: list(argv),
            )
            self._fake_orphan(monkeypatch)
            self._pin_identity_key(monkeypatch)
            start = pc.process_start_time(proc.pid) or "x"
            # "" = unsigned; hex garbage = wrong MAC; non-ASCII and a lone
            # surrogate = the malformed-text shapes that must read as
            # verification failure, never crash the connect path.
            for forged_sig in ("", "deadbeef" * 8, "签名不对", "\udc80bad"):
                reg, mgr = self._mgr(tmp_path, base_port=55000)
                reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
                reg.update(
                    "cd-1",
                    local_port=port,
                    forwarder_pid=proc.pid,
                    forwarder_start=start,
                    forwarder_sig=forged_sig,
                    was_connected=True,
                )
                st = await mgr.connect("cd-1")
                assert st.state == TunnelState.CONNECTED
                assert proc.poll() is None, f"a record with sig={forged_sig!r} was honored"
                reg.remove("cd-1")
        finally:
            self._cleanup(proc)

    @pytest.mark.asyncio
    async def test_live_parent_forwarder_is_never_signalled(self, tmp_path, monkeypatch):
        """Forged-registry regression: the registry is agent-writable, so a
        record can truthfully describe a LIVE sibling gateway's forwarder
        (real pid, real start time, matching argv). The orphan gate must
        refuse it: a process whose parent is alive belongs to a running
        gateway and is never a hard-kill leak."""
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        # Direct child of the test process == a live parent (this process).
        proc, port, argv = self._spawn_port_holder()
        try:
            monkeypatch.setattr(
                stm,
                "_build_ssh_tunnel_argv",
                lambda host, lp, rp, compression=True: list(argv),
            )
            sign = self._pin_identity_key(monkeypatch)
            start = pc.process_start_time(proc.pid) or "x"
            reg, mgr = self._mgr(tmp_path, base_port=54900)
            reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
            reg.update(
                "cd-1",
                local_port=port,
                forwarder_pid=proc.pid,
                forwarder_start=start,
                forwarder_sig=sign("cd-1", proc.pid, start, port),
                was_connected=True,
            )

            st = await mgr.connect("cd-1")

            assert st.state == TunnelState.CONNECTED
            assert proc.poll() is None, "a live-parented forwarder was signalled"
        finally:
            self._cleanup(proc)

    @pytest.mark.skipif(os.name == "nt", reason="ppid probe of a posix-spawned stand-in")
    @pytest.mark.asyncio
    async def test_recorded_start_mismatch_is_never_signalled(self, tmp_path, monkeypatch):
        """Pid-recycling regression: the recorded pid now belongs to a process
        whose argv happens to match, but its start-time identity does not.
        Nothing may be signalled — the start-time pin is what defeats a
        recycled pid running an identical command line."""
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        proc, port, argv = self._spawn_port_holder()
        try:
            monkeypatch.setattr(
                stm,
                "_build_ssh_tunnel_argv",
                lambda host, lp, rp, compression=True: list(argv),
            )
            self._fake_orphan(monkeypatch)
            sign = self._pin_identity_key(monkeypatch)
            reg, mgr = self._mgr(tmp_path, base_port=54800)
            reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
            reg.update(
                "cd-1",
                local_port=port,
                forwarder_pid=proc.pid,
                forwarder_start="not-the-recorded-identity",
                forwarder_sig=sign("cd-1", proc.pid, "not-the-recorded-identity", port),
                was_connected=True,
            )

            st = await mgr.connect("cd-1")

            assert st.state == TunnelState.CONNECTED
            assert proc.poll() is None, "a start-time-mismatched pid was signalled"
        finally:
            self._cleanup(proc)

    @pytest.mark.asyncio
    async def test_recorded_pid_with_foreign_argv_is_never_signalled(self, tmp_path, monkeypatch):
        """#1972 regression: the recorded pid was recycled onto a process this
        manager did not spawn (its argv is not the forward command line). It
        must be left alone even with a matching start time and an open orphan
        gate."""
        from kiro_crew import platform_compat as pc
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        proc, port, _argv = self._spawn_port_holder()
        try:
            self._fake_orphan(monkeypatch)
            sign = self._pin_identity_key(monkeypatch)
            start = pc.process_start_time(proc.pid) or "x"
            reg, mgr = self._mgr(tmp_path, base_port=54400)
            reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
            # Even with a CORRECT start-time identity the argv gate must still
            # refuse: the real argv builder expects `ssh …`, not python.
            reg.update(
                "cd-1",
                local_port=port,
                forwarder_pid=proc.pid,
                forwarder_start=start,
                forwarder_sig=sign("cd-1", proc.pid, start, port),
                was_connected=True,
            )

            st = await mgr.connect("cd-1")  # real argv builder: expects ssh …

            assert st.state == TunnelState.CONNECTED
            assert proc.poll() is None, "a foreign process holding the port was signalled"
            inst = reg.get("cd-1")
            assert inst.local_port != port  # allocated around the occupied port
        finally:
            self._cleanup(proc)

    @pytest.mark.asyncio
    async def test_unrecorded_port_holder_is_never_signalled(self, tmp_path):
        """No recorded identity -> no candidate: the reclaim never scans the
        process table for whoever holds the port (that scan is what #1972
        removed)."""
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        proc, port, _argv = self._spawn_port_holder()
        try:
            reg, mgr = self._mgr(tmp_path, base_port=54500)
            reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
            reg.update("cd-1", local_port=port, was_connected=True)  # identity stays unset

            st = await mgr.connect("cd-1")

            assert st.state == TunnelState.CONNECTED
            assert proc.poll() is None, "an unrecorded port holder was signalled"
        finally:
            self._cleanup(proc)

    @pytest.mark.asyncio
    async def test_free_port_short_circuits_before_the_identity_check(self, tmp_path, monkeypatch):
        """A free recorded port means nothing leaked: the recorded pid is not
        signalled even when its identity WOULD match (e.g. the pid was
        recycled onto an innocent process while nothing holds the port)."""
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        code = "import sys, time; sys.stdout.write('R'); sys.stdout.flush(); time.sleep(120)"
        argv = [sys.executable, "-c", code]
        proc = subprocess.Popen(
            argv, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        try:
            assert proc.stdout.read(1) == b"R"
            # Even a would-be-exact identity must not matter: the port gates.
            monkeypatch.setattr(
                stm,
                "_build_ssh_tunnel_argv",
                lambda host, lp, rp, compression=True: list(argv),
            )
            self._fake_orphan(monkeypatch)
            sign = self._pin_identity_key(monkeypatch)
            # A port nothing listens on: bind(0) to reserve one, then close it.
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]
            s.close()

            start = pc.process_start_time(proc.pid) or "x"
            reg, mgr = self._mgr(tmp_path, base_port=54600)
            reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
            reg.update(
                "cd-1",
                local_port=free_port,
                forwarder_pid=proc.pid,
                forwarder_start=start,
                forwarder_sig=sign("cd-1", proc.pid, start, free_port),
                was_connected=True,
            )

            st = await mgr.connect("cd-1")

            assert st.state == TunnelState.CONNECTED
            assert proc.poll() is None, "reclaim signalled a pid while its port was free"
        finally:
            self._cleanup(proc)

    @pytest.mark.skipif(not socket.has_ipv6, reason="host has no IPv6 support")
    def test_reclaim_ignores_a_foreign_ipv6_listener_on_the_same_port(self, monkeypatch):
        """A reclaim asks whether OUR forwarder released ITS port, not whether the
        port is free for a new one.

        An ``ssh -L`` child binds 127.0.0.1 alone, so an unrelated listener on
        ``::1`` says nothing about whether the orphan let go. Probing both
        families here would report a fully reclaimed forwarder as ``not_gone``
        and record that wrong outcome in the SEL audit.
        """
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc

        squatter = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        squatter.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        try:
            squatter.bind(("::1", 0))
        except OSError:
            squatter.close()
            pytest.skip("::1 is not assignable here")
        squatter.listen(1)
        port = squatter.getsockname()[1]

        monkeypatch.setattr(pc, "process_start_time", lambda pid: "identity-A")
        monkeypatch.setattr(pc, "process_argv_matches_exact", lambda pid, argv: True)
        monkeypatch.setattr(pc, "pid_exists", lambda pid: False)  # child already exited
        monkeypatch.setattr(pc, "kill_pid", lambda pid, sig: True)
        monkeypatch.setattr(stm, "_RECLAIM_TERM_GRACE_SECS", 0.05)

        try:
            outcome = stm._verify_and_reclaim_forwarder(
                4242, "identity-A", ["ssh", "-N"], port, False, "instance=t pid=4242"
            )
        finally:
            squatter.close()

        assert (
            outcome == "reclaimed"
        ), "a foreign ::1 listener must not make a released IPv4 forward read as not_gone"

    def test_sigkill_withheld_when_identity_changes_during_grace(self, monkeypatch):
        """Open-box guard test: if the pid stops matching its recorded
        start-time identity during the TERM grace (exit + recycle inside a
        poll gap, which pid_exists cannot observe), the destructive SIGKILL is
        withheld — leak-not-mis-kill."""
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc

        delivered: list[int] = []
        starts = iter(["identity-A", "identity-B"])  # pre-TERM, then re-check

        monkeypatch.setattr(pc, "process_start_time", lambda pid: next(starts))
        monkeypatch.setattr(pc, "process_argv_matches_exact", lambda pid, argv: True)
        monkeypatch.setattr(pc, "pid_exists", lambda pid: True)
        monkeypatch.setattr(pc, "kill_pid", lambda pid, sig: delivered.append(sig) or True)
        monkeypatch.setattr(stm, "_is_port_free", lambda port, host="127.0.0.1": False)
        monkeypatch.setattr(stm, "_RECLAIM_TERM_GRACE_SECS", 0.05)

        outcome = stm._verify_and_reclaim_forwarder(
            4242, "identity-A", ["ssh", "-N"], 50505, False, "instance=t pid=4242"
        )

        assert outcome == "recycled_during_grace"
        assert delivered == [pc.SIGTERM], "SIGKILL must be withheld on identity change"

    def test_sigkill_withheld_when_pid_vanishes_but_port_lingers(self, monkeypatch):
        """Open-box guard test: a pid that no longer exists while the port is
        still held (SSM wrapper gone, plugin lingering — or a recycle inside a
        poll gap) is NOT a safe SIGKILL fall-through: getpgid on a recycled
        pid would resolve the REPLACEMENT process. No verified identity, no
        SIGKILL."""
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc

        delivered: list[int] = []
        starts = iter(["identity-A", None])  # pre-TERM ok; re-check: pid gone

        monkeypatch.setattr(pc, "process_start_time", lambda pid: next(starts))
        monkeypatch.setattr(pc, "process_argv_matches_exact", lambda pid, argv: True)
        monkeypatch.setattr(pc, "pid_exists", lambda pid: False)
        monkeypatch.setattr(pc, "pgroup_exists", lambda pgid: True)  # plugin lingers
        monkeypatch.setattr(pc, "kill_pid", lambda pid, sig: delivered.append(sig) or True)
        monkeypatch.setattr(
            stm._SshTunnel,
            "_signal_group",
            staticmethod(lambda pid, sig: delivered.append(sig) or True),
        )
        monkeypatch.setattr(stm, "_is_port_free", lambda port, host="127.0.0.1": False)
        monkeypatch.setattr(stm, "_RECLAIM_TERM_GRACE_SECS", 0.05)

        outcome = stm._verify_and_reclaim_forwarder(
            4242, "identity-A", ["aws", "ssm"], 50506, True, "instance=t pid=4242"
        )

        assert outcome == "recycled_during_grace"
        assert delivered == [pc.SIGTERM], "SIGKILL must be withheld when the pid is gone"

    def test_sigkill_withheld_when_argv_stops_matching_during_grace(self, monkeypatch):
        """Open-box guard test: on macOS the start token is 1s-granular, so a
        same-second pid reuse can keep it matching — the argv half of the
        re-check is what breaks the tie. argv mismatch at escalation time
        withholds the SIGKILL."""
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc

        delivered: list[int] = []
        argv_answers = iter([True, False])  # pre-TERM ok; re-check: different argv

        monkeypatch.setattr(pc, "process_start_time", lambda pid: "identity-A")
        monkeypatch.setattr(pc, "process_argv_matches_exact", lambda pid, argv: next(argv_answers))
        monkeypatch.setattr(pc, "pid_exists", lambda pid: True)
        monkeypatch.setattr(pc, "kill_pid", lambda pid, sig: delivered.append(sig) or True)
        monkeypatch.setattr(stm, "_is_port_free", lambda port, host="127.0.0.1": False)
        monkeypatch.setattr(stm, "_RECLAIM_TERM_GRACE_SECS", 0.05)

        outcome = stm._verify_and_reclaim_forwarder(
            4242, "identity-A", ["ssh", "-N"], 50507, False, "instance=t pid=4242"
        )

        assert outcome == "recycled_during_grace"
        assert delivered == [pc.SIGTERM], "SIGKILL must be withheld on argv change"


# ── Generic chat proxy ────────────────────────────────────────────────────────


class TestProxyRequest:
    """SshTunnelManager.proxy_request — the remote-crew chat carrier."""

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
            reg, base_port=53500, mint_token=mint or ok_mint, tunnel_factory=factory
        )

    @staticmethod
    def _fake_session(calls, *, statuses):
        """A ClientSession stand-in recording request kwargs; statuses pop per call."""

        class _Resp:
            def __init__(self, status):
                self.status = status

            def release(self):
                return None

        class _Sess:
            def __init__(self, *a, **k):
                self.closed = False

            async def request(self, method, url, **kwargs):
                calls.append({"method": method, "url": url, **kwargs})
                return _Resp(statuses.pop(0))

            async def close(self):
                self.closed = True

        return _Sess

    @pytest.mark.asyncio
    async def test_not_connected_raises_typed_error(self, tmp_path):
        from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError

        _reg, mgr = self._mgr(tmp_path)
        with pytest.raises(ProxyRequestError) as ei:
            async with mgr.proxy_request("nope", "GET", "api/status"):
                pass
        assert ei.value.code == "proxy_peer_not_connected"
        assert ei.value.http_status == 503

    @pytest.mark.asyncio
    async def test_success_sends_port_scoped_cookie_and_refuses_redirects(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.instances import ssh_tunnel_manager as m

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        st = await mgr.connect("cd-1")
        calls: list = []
        monkeypatch.setattr(m.aiohttp, "ClientSession", self._fake_session(calls, statuses=[200]))

        async with mgr.proxy_request(
            "cd-1", "POST", "/api/chat", params={"a": "b"}, data=b"{}"
        ) as resp:
            assert resp.status == 200
        assert len(calls) == 1
        call = calls[0]
        assert call["method"] == "POST"
        assert call["url"] == f"http://127.0.0.1:{st.local_port}/api/chat"
        # Credential travels as the PORT-SCOPED cookie, never a bare name.
        assert call["headers"]["Cookie"] == f"mc_token_{st.local_port}=SECRET_TOK"
        # SSRF guard: a compromised peer answering 30x must not steer the hub.
        assert call["allow_redirects"] is False

    @pytest.mark.asyncio
    async def test_401_gets_exactly_one_remint_retry(self, tmp_path, monkeypatch):
        from kiro_crew.instances import ssh_tunnel_manager as m

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")
        calls: list = []
        monkeypatch.setattr(
            m.aiohttp, "ClientSession", self._fake_session(calls, statuses=[401, 200])
        )
        remints = []

        async def fake_refresh(instance_id):
            remints.append(instance_id)
            mgr._tokens[instance_id] = "FRESH_TOK"
            return "FRESH_TOK"

        monkeypatch.setattr(mgr, "refresh_token", fake_refresh)

        async with mgr.proxy_request("cd-1", "GET", "api/chat/slots") as resp:
            assert resp.status == 200
        assert remints == ["cd-1"]
        assert len(calls) == 2
        assert "FRESH_TOK" in calls[1]["headers"]["Cookie"]

    @pytest.mark.asyncio
    async def test_persistent_401_raises_unauthorized_not_a_loop(self, tmp_path, monkeypatch):
        from kiro_crew.instances import ssh_tunnel_manager as m
        from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")
        calls: list = []
        monkeypatch.setattr(m.aiohttp, "ClientSession", self._fake_session(calls, statuses=[401]))

        async def no_refresh(instance_id):
            return None

        monkeypatch.setattr(mgr, "refresh_token", no_refresh)
        with pytest.raises(ProxyRequestError) as ei:
            async with mgr.proxy_request("cd-1", "GET", "api/chat/slots"):
                pass
        assert ei.value.code == "proxy_unauthorized"
        assert len(calls) == 1  # no retry storm

    @pytest.mark.asyncio
    async def test_401_after_a_successful_remint_is_still_typed(self, tmp_path, monkeypatch):
        """The re-mint succeeding does not make the SECOND 401 a normal reply.

        Previously the retry guard was `status in (401, 403) and not reminted`,
        so once a fresh credential had been minted a second rejection fell
        through to the caller as a bare peer 401 — the UI would read "the chat
        endpoint said no" instead of a credential failure, with no coded error.
        """
        from kiro_crew.instances import ssh_tunnel_manager as m
        from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")
        calls: list = []
        monkeypatch.setattr(
            m.aiohttp, "ClientSession", self._fake_session(calls, statuses=[401, 401])
        )

        async def fake_refresh(instance_id):
            return "FRESH"

        monkeypatch.setattr(mgr, "refresh_token", fake_refresh)
        with pytest.raises(ProxyRequestError) as ei:
            async with mgr.proxy_request("cd-1", "GET", "api/chat/slots"):
                pass
        assert ei.value.code == "proxy_unauthorized"
        assert len(calls) == 2  # original + exactly one retry, then stop

    @pytest.mark.asyncio
    async def test_transport_error_maps_to_unreachable(self, tmp_path, monkeypatch):
        from kiro_crew.instances import ssh_tunnel_manager as m
        from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")

        class _BoomSess:
            def __init__(self, *a, **k):
                pass

            async def request(self, *a, **k):
                raise asyncio.TimeoutError()

            async def close(self):
                return None

        monkeypatch.setattr(m.aiohttp, "ClientSession", _BoomSess)
        with pytest.raises(ProxyRequestError) as ei:
            async with mgr.proxy_request("cd-1", "GET", "api/status"):
                pass
        assert ei.value.code == "proxy_peer_unreachable"
        assert ei.value.http_status == 502


class TestPeerRequestSharedDance:
    """The derivation shared by the three peer-request methods.

    `proxy_request`, `send_session_bundle` and `search_sessions_remote` each
    make an authenticated call to a CONNECTED peer over the open forward. The
    part that is identical — connected-only, loopback target, port-scoped cookie
    name, credential read per attempt — lives in `_peer_target` /
    `_peer_cookie_header` so it is stated once. What is NOT shared is each
    method's error contract, and that is what these tests pin.
    """

    def _mgr(self, tmp_path):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        reg = InstancesRegistry(path=tmp_path / "instances.json")

        async def ok_mint(host, **kwargs):
            return "SECRET_TOK"

        return reg, SshTunnelManager(
            reg, base_port=53700, mint_token=ok_mint, tunnel_factory=_FakeTunnel
        )

    @pytest.mark.asyncio
    async def test_peer_target_is_the_single_source_of_the_port_scoped_cookie(self, tmp_path):
        """One derivation, not three: loopback URL + `mc_token_{port}`.

        The port scope is load-bearing — the peer keys its cookie on the port the
        CLIENT connected to, so two remotes both serving 7777 through different
        forwards must not collide, and a bare `mc_token` would 403 every call.
        """
        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        st = await mgr.connect("cd-1")

        url, cookie_name = mgr._peer_target("cd-1", "/api/chat/slots")
        assert url == f"http://127.0.0.1:{st.local_port}/api/chat/slots"
        assert cookie_name == f"mc_token_{st.local_port}"
        # Leading slash is optional — callers pass both spellings.
        assert mgr._peer_target("cd-1", "api/chat/slots")[0] == url

    def test_peer_cookie_header_refuses_when_no_credential(self, tmp_path):
        from kiro_crew.instances.ssh_tunnel_manager import _PeerUnavailable

        _reg, mgr = self._mgr(tmp_path)
        with pytest.raises(_PeerUnavailable) as ei:
            mgr._peer_cookie_header("cd-1", "mc_token_1")
        assert ei.value.kind == "no_credential"

    @pytest.mark.asyncio
    async def test_each_caller_keeps_its_own_not_connected_code(self, tmp_path):
        """The shared helper must NOT flatten the three error families.

        `proxy_*`, `transfer_*` and `search_*` belong to three separate route
        contracts pinned by test_error_code_contract.py. Deriving them from the
        helper's `kind` would be tidier and wrong — this is the drift guard that
        catches it, and it is the reason the codes are literals at each site.
        """
        from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError

        _reg, mgr = self._mgr(tmp_path)

        with pytest.raises(ProxyRequestError) as ei:
            async with mgr.proxy_request("nope", "GET", "api/chat"):
                pass
        assert ei.value.code == "proxy_peer_not_connected"

        ok, payload = await mgr.send_session_bundle("nope", {"bundle_version": 2})
        assert (ok, payload["code"]) == (False, "transfer_peer_not_connected")

        ok, payload = await mgr.search_sessions_remote("nope", "q", 10)
        assert (ok, payload["code"]) == (False, "search_peer_not_connected")

    @pytest.mark.asyncio
    async def test_each_caller_keeps_its_own_no_credential_code(self, tmp_path):
        """Same split for the missing-credential condition, on a LIVE tunnel."""
        from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")
        mgr._tokens.pop("cd-1", None)  # connected, but the credential is gone

        with pytest.raises(ProxyRequestError) as ei:
            async with mgr.proxy_request("cd-1", "GET", "api/chat"):
                pass
        assert ei.value.code == "proxy_no_credential"
        assert ei.value.http_status == 503

        ok, payload = await mgr.send_session_bundle("cd-1", {"bundle_version": 2})
        assert (ok, payload["code"]) == (False, "transfer_no_credential")

        ok, payload = await mgr.search_sessions_remote("cd-1", "q", 10)
        assert (ok, payload["code"]) == (False, "search_no_credential")

    def test_proxy_request_exposes_no_timeout_override(self):
        """The timeout policy is a property of the method, not a per-call choice.

        It is connect+read-idle rather than total because a proxied chat turn
        streams for minutes and a total cap would sever it. Nothing ever passed
        the old override, so the parameter only invited a caller to break that
        for itself; this pins the subtraction.
        """
        import inspect

        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        params = inspect.signature(SshTunnelManager.proxy_request).parameters
        assert "timeout" not in params
        assert set(params) == {
            "self",
            "instance_id",
            "method",
            "path",
            "params",
            "data",
            "content_type",
        }


class TestProxyHandlerPolicy:
    """api_instances_proxy — the policy gates in front of the carrier.

    The streaming pump itself needs a real transport (StreamResponse.prepare),
    so it is exercised live against a connected peer; every DECISION the
    handler makes before the pump is covered here.
    """

    def _req(self, tmp_path, monkeypatch, *, path, method="GET", manager="stub", enabled=True):
        _enable(tmp_path, monkeypatch, enabled=enabled)
        # The proxy requires the positively-identified OWNER (same bar as
        # federated search); the fake request has no real token subject, so
        # satisfy the gate explicitly. The deny path has its own test below.
        from kiro_crew.dashboard.handlers import source_providers as sp

        monkeypatch.setattr(sp, "is_owner_dashboard_request", lambda r: True)
        from kiro_crew.instances.registry import InstancesRegistry

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        state = _State(reg, manager if manager != "stub" else object())
        req = _FakeReq(state, match={"id": "cd-1", "path": path})
        req.method = method
        return req

    @pytest.mark.asyncio
    async def test_client_disconnect_midstream_does_not_write_after_reset(
        self, tmp_path, monkeypatch
    ):
        """A browser that drops mid-stream must not produce a SECOND write.

        The pump caught `ConnectionResetError` and then fell through to
        `write_eof()` on the very transport that had just refused a write — the
        second exception escaped the handler and crashed the request. The
        handler now returns from the except block, so exactly one write is
        attempted after the reset: none.
        """
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        writes: list[str] = []

        class _Resp:
            def __init__(self):
                self.headers: dict = {}

            async def prepare(self, request):
                return None

            async def write(self, chunk):
                writes.append("write")
                raise ConnectionResetError("client went away")

            async def write_eof(self):
                writes.append("write_eof")  # must never happen after the reset
                raise ConnectionResetError("transport is gone")

        async def _chunks():
            yield b"data: hello\n\n"

        class _Mgr:
            @contextlib.asynccontextmanager
            async def proxy_request(self, iid, method, path, **kwargs):
                yield types.SimpleNamespace(
                    status=200,
                    headers={"Content-Type": "text/event-stream"},
                    content=types.SimpleNamespace(iter_any=_chunks),
                )

        from kiro_crew.dashboard import handlers_instances as hi

        req = self._req(tmp_path, monkeypatch, path="api/chat/stream", manager=_Mgr())
        req.body_exists = False
        monkeypatch.setattr(hi.web, "StreamResponse", lambda **kw: _Resp())
        # Must not raise: the disconnect is terminal, not a crash.
        await api_instances_proxy(req)
        assert writes == ["write"]  # no write_eof on the dead transport

    @pytest.mark.asyncio
    async def test_non_owner_identity_is_refused(self, tmp_path, monkeypatch):
        """A Slack-minted dashboard identity passes _guard but must NOT reach
        the peer: the proxy executes with the owner's manager-held credential."""
        from kiro_crew.dashboard.handlers import source_providers as sp
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        req = self._req(tmp_path, monkeypatch, path="api/chat/slots")
        monkeypatch.setattr(sp, "is_owner_dashboard_request", lambda r: False)
        monkeypatch.setattr(sp, "stale_owner_session_response", lambda r: None)
        resp = await api_instances_proxy(req)
        assert resp.status == 403
        assert _body(resp)["code"] == "owner_only"

    @pytest.mark.asyncio
    async def test_disabled_feature_is_403(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        req = self._req(tmp_path, monkeypatch, path="api/chat/slots", enabled=False)
        resp = await api_instances_proxy(req)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_traversal_is_refused_before_any_url_is_built(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        req = self._req(tmp_path, monkeypatch, path="api/../etc/passwd")
        resp = await api_instances_proxy(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_non_api_path_is_refused(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        req = self._req(tmp_path, monkeypatch, path="assets/main.js")
        resp = await api_instances_proxy(req)
        assert resp.status == 400
        assert _body(resp)["code"] == "proxy_path_denied"

    @pytest.mark.asyncio
    async def test_peer_instances_plane_is_refused_no_chaining(self, tmp_path, monkeypatch):
        """The allowlist subsumes the old explicit deny: `api/instances` is not
        an allowed prefix, so one hub still cannot chain through a peer into a
        third machine's SSH control plane."""
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        req = self._req(tmp_path, monkeypatch, path="api/instances/other/proxy/api/status")
        resp = await api_instances_proxy(req)
        assert resp.status == 400
        assert _body(resp)["code"] == "proxy_path_denied"

    @pytest.mark.asyncio
    async def test_peer_token_route_is_refused_with_denied_shape(self, tmp_path, monkeypatch):
        """A peer's credential-minting route must never be proxied: its JSON
        reply passes the content-type gate, so a deny-only policy would carry
        a minted peer token back through the hub in-band. The allowlist is the
        no-remote-credential-on-hub invariant, pinned here rather than in prose."""
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        req = self._req(tmp_path, monkeypatch, path="api/token/local")
        resp = await api_instances_proxy(req)
        assert resp.status == 400
        assert _body(resp)["code"] == "proxy_path_denied"

    @pytest.mark.parametrize(
        "raw",
        [
            "api/token/local",  # GET /api/token/local mints a dashboard token
            "api/apps/someapp/token",  # POST /api/apps/{name}/token
            "api/webhooks/tokens",  # POST /api/webhooks/tokens
            "api/status",  # harmless, but not part of the chat surface either
            "api",  # the bare prefix names no endpoint
            "api/chatx/slots",  # allowed prefix must match whole segments
        ],
    )
    def test_paths_outside_the_chat_allowlist_are_refused(self, raw):
        """The vet policy is a positive prefix allowlist (`api/chat` today):
        anything the feature never asked for is refused by default instead of
        proxied silently — including every future sensitive peer endpoint."""
        from kiro_crew.dashboard.handlers_instances import (
            _PROXY_PATH_DENIED_REASON,
            _proxy_canonical_path,
        )

        path, reason = _proxy_canonical_path(raw)
        assert path == ""
        assert reason == _PROXY_PATH_DENIED_REASON

    def test_bare_chat_prefix_itself_is_forwarded(self):
        """`POST /api/chat` is the primary send route the feature rides on —
        the prefix itself must pass, not only paths strictly beneath it."""
        from kiro_crew.dashboard.handlers_instances import _proxy_canonical_path

        assert _proxy_canonical_path("api/chat") == ("api/chat", "")
        assert _proxy_canonical_path("/api/chat/") == ("api/chat", "")

    def test_event_stream_prefix_is_forwarded(self):
        """`GET /api/stream` is the out-of-turn half of the chat view: the peer's
        own SSE broadcast, carrying session-list and slot-state changes while the
        per-turn reply streams back from `api/chat`. It is a leaf endpoint, so
        the bare prefix is the whole surface this row grants."""
        from kiro_crew.dashboard.handlers_instances import _proxy_canonical_path

        assert _proxy_canonical_path("api/stream") == ("api/stream", "")
        assert _proxy_canonical_path("/api/stream/") == ("api/stream", "")

    @pytest.mark.parametrize(
        "raw",
        [
            "api/ws",  # the SSE sibling: an upgrade cannot cross this proxy
            "api/ws/stt",
            "api/streaming",  # whole-segment match, not a string prefix
            "api/stream-x",
            "api/file-stream",  # a DIFFERENT endpoint that merely ends in stream
        ],
    )
    def test_stream_row_does_not_admit_its_neighbours(self, raw):
        """The row is `("api", "stream")` — whole segments, nothing adjacent.

        `api/ws` is the one to keep refused on purpose: it is the same event bus
        over WebSocket, and admitting it would require a `101 Switching
        Protocols` to pass the reply content-type gate that exists to stop a peer
        serving active content onto the authenticated hub origin.
        """
        from kiro_crew.dashboard.handlers_instances import (
            _PROXY_PATH_DENIED_REASON,
            _proxy_canonical_path,
        )

        path, reason = _proxy_canonical_path(raw)
        assert path == ""
        assert reason == _PROXY_PATH_DENIED_REASON

    def test_allowlist_constant_is_pinned_exactly(self):
        """Widening the proxied surface must be a REVIEWED act: this pins the
        constant's exact value, so adding a row fails here until the test is
        updated alongside it. The shape floor (every row >= 2 segments, rooted
        at `api`) guards the fail-open edits an exact pin alone would also
        catch — kept separate so the failure message names the broken
        invariant."""
        from kiro_crew.dashboard.handlers_instances import _PROXY_ALLOWED_PREFIXES

        assert _PROXY_ALLOWED_PREFIXES == (("api", "chat"), ("api", "stream"))
        for prefix in _PROXY_ALLOWED_PREFIXES:
            # An empty row prefix-matches EVERYTHING and a one-segment row
            # restores the whole peer /api/ surface; both must be impossible.
            assert len(prefix) >= 2
            assert prefix[0] == "api"
            assert all(isinstance(seg, str) and seg for seg in prefix)

    def test_malformed_allowlist_row_fails_closed(self, monkeypatch):
        """Even if a bad edit ships an empty or one-segment row, the vet must
        not widen: rows shallower than two segments are ignored, so the
        policy degrades to refusing more, never to forwarding more."""
        from kiro_crew.dashboard import handlers_instances as hi

        monkeypatch.setattr(hi, "_PROXY_ALLOWED_PREFIXES", ((), ("api",)))
        path, reason = hi._proxy_canonical_path("api/token/local")
        assert path == ""
        assert reason

    @pytest.mark.parametrize(
        "raw",
        [
            "api/%2e%2e/api/instances/x",  # one layer: the router already decoded one
            "api/%252e%252e/api/instances/x",  # two layers
            "api/%25252e%25252e/api/instances/x",  # three
            "api/..%2fapi%2finstances/x",  # encoded separator, raw dots
            "api/%2e%2e%2fapi%2finstances/x",  # both encoded
        ],
    )
    def test_encoded_traversal_cannot_reach_the_control_plane(self, raw):
        """Every encoding depth resolves to the SAME refusal.

        The denylist shape this replaced inspected a half-decoded string while
        the peer resolved the fully-decoded one, so `%252e%252e` arrived as
        `%2e%2e`, matched no rule, and normalized back into `api/instances`.
        Decoding to a fixed point before vetting is what closes the class —
        so this is parametrized over depth rather than pinned to one payload.
        """
        from kiro_crew.dashboard.handlers_instances import _proxy_canonical_path

        path, reason = _proxy_canonical_path(raw)
        assert path == ""
        assert reason

    def test_canonical_path_is_rebuilt_from_vetted_segments(self):
        """The forwarded path is constructed, not merely approved: a caller
        cannot get one string past the policy and a different one onto the
        wire."""
        from kiro_crew.dashboard.handlers_instances import _proxy_canonical_path

        assert _proxy_canonical_path("/api/chat/slots/") == ("api/chat/slots", "")
        assert _proxy_canonical_path("api/%63hat/slots") == ("api/chat/slots", "")
        # `instances` only matters as the FIRST segment under api/ — a session
        # or resource that merely contains the word is still reachable.
        assert _proxy_canonical_path("api/chat/instances") == ("api/chat/instances", "")

    @pytest.mark.parametrize(
        "raw,expect",
        [
            ("api//chat", "empty path segment"),
            ("api/chat%00/x", "illegal character in path segment"),
            ("api/ch at/x", "illegal character in path segment"),
            ("api/%zz/x", "malformed percent-encoding in path"),
            ("api/.../x", "path traversal"),
        ],
    )
    def test_only_plainly_named_segments_are_forwarded(self, raw, expect):
        from kiro_crew.dashboard.handlers_instances import _proxy_canonical_path

        path, reason = _proxy_canonical_path(raw)
        assert path == ""
        assert reason == expect

    @pytest.mark.asyncio
    async def test_disallowed_method_is_405(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        req = self._req(tmp_path, monkeypatch, path="api/chat/slots", method="OPTIONS")
        resp = await api_instances_proxy(req)
        assert resp.status == 405

    @pytest.mark.asyncio
    async def test_missing_manager_is_503(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        req = self._req(tmp_path, monkeypatch, path="api/chat/slots", manager=None)
        resp = await api_instances_proxy(req)
        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_hub_token_is_stripped_from_forwarded_query(self, tmp_path, monkeypatch):
        """The browser's ?token= is the HUB's credential — it must never
        cross the tunnel (a peer receiving it holds a replayable credential
        for this gateway)."""
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        captured: dict = {}

        class _Mgr:
            @contextlib.asynccontextmanager
            async def proxy_request(self, iid, method, path, **kwargs):
                captured.update(kwargs)
                # Refused content type short-circuits before StreamResponse.prepare,
                # so the handler stays testable without a real transport.
                yield types.SimpleNamespace(status=200, headers={"Content-Type": "text/html"})

        req = self._req(tmp_path, monkeypatch, path="api/chat/slots", manager=_Mgr())
        req.query = {"token": "HUB_SECRET", "limit": "5"}
        req.body_exists = False
        resp = await api_instances_proxy(req)
        assert captured["params"] == {"limit": "5"}  # token stripped
        # ... and the HTML reply was refused (content-type gate).
        assert resp.status == 502
        assert _body(resp)["code"] == "proxy_content_type_refused"

    @pytest.mark.asyncio
    async def test_error_bodies_carry_machine_readable_codes(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        req = self._req(tmp_path, monkeypatch, path="assets/x.js")
        assert _body(await api_instances_proxy(req))["code"] == "proxy_path_denied"
        req = self._req(tmp_path, monkeypatch, path="api/chat/slots", method="OPTIONS")
        assert _body(await api_instances_proxy(req))["code"] == "proxy_method_not_allowed"
        req = self._req(tmp_path, monkeypatch, path="api/chat/slots", manager=None)
        assert _body(await api_instances_proxy(req))["code"] == "instances_manager_unavailable"
