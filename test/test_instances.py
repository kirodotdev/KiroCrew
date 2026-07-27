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
import json
import logging
import socket
from pathlib import Path

import pytest

# ── config flag + constants ────────────────────────────────────────────────


class TestConfig:
    def test_defaults_off_and_tunables(self):
        from kiro_crew.config.loader import InstancesConfig
        from kiro_crew.instances.constants import (
            DEFAULT_TUNNEL_BASE_PORT,
            DEFAULT_WARM_SET_CAP,
        )

        c = InstancesConfig()
        assert c.enabled is False
        assert c.warm_set_cap == DEFAULT_WARM_SET_CAP == 5
        assert c.tunnel_base_port == DEFAULT_TUNNEL_BASE_PORT == 7778

    def test_clamps_out_of_range(self):
        from kiro_crew.config.loader import InstancesConfig

        c = InstancesConfig(warm_set_cap=0, tunnel_base_port=99999)
        assert c.warm_set_cap == 1
        assert c.tunnel_base_port == 7778

    def test_roundtrip_and_schema(self):
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.config.schema import SCHEMA_REGISTRY

        d = KiroCrewConfig().to_dict()
        assert d["instances"] == {
            "enabled": False,
            "warm_set_cap": 5,
            "tunnel_base_port": 7778,
            "ssh_compression": True,
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
            "instances.max_recovery_attempts",
            "instances.recover_backoff_max_secs",
            "instances.probe_failure_threshold",
        ):
            assert p in paths

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

    def test_mint_nonzero_exit_surfaces_stdout_reason(self, monkeypatch):
        """A `kirocrew token` failure prints its reason to stdout (exit 1).

        The error message must carry that reason, not the old opaque
        "<no stderr>" — stderr is empty in this case.
        """
        from kiro_crew.instances import token_mint as tm

        class FakeProc:
            returncode = 1

            async def communicate(self):
                return (
                    b"\xe2\x9d\x8c Could not reach gateway on port 5477: "
                    b"Connection refused\n",
                    b"",
                )

        async def fake_exec(*a, **k):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        with pytest.raises(tm.TokenMintError) as exc:
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        msg = str(exc.value)
        assert "Could not reach gateway on port 5477" in msg
        assert "Connection refused" in msg
        assert "<no stderr>" not in msg

    def test_mint_nonzero_exit_scrubs_token_from_stdout(self, monkeypatch):
        """Defensive: even if a token URL rides along on a non-zero exit, the
        raw JWT is never surfaced in the exception (only a scrubbed marker)."""
        from kiro_crew.instances import token_mint as tm

        class FakeProc:
            returncode = 1

            async def communicate(self):
                return b"http://localhost:5477?token=SECRETJWT\n", b""

        async def fake_exec(*a, **k):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        with pytest.raises(tm.TokenMintError) as exc:
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        assert "SECRETJWT" not in str(exc.value)

    def test_mint_parse_failure_surfaces_stdout(self, monkeypatch):
        """Exit 0 but no parseable token → the diagnostic includes the stdout
        tail so the operator can see what the remote actually printed."""
        from kiro_crew.instances import token_mint as tm

        class FakeProc:
            returncode = 0

            async def communicate(self):
                return b"unexpected banner, no url here\n", b""

        async def fake_exec(*a, **k):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        with pytest.raises(tm.TokenMintError) as exc:
            asyncio.run(tm.mint_remote_token("cd-1", ttl="20h"))
        assert "unexpected banner" in str(exc.value)


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

        run_marker.clear_marker(7879)
        assert not marker.exists()
        run_marker.clear_marker(7879)  # clearing a missing marker is a no-op

    def test_no_marker_when_launcher_absent(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.instances import run_marker

        bindir = tmp_path / "venv2" / "bin"
        bindir.mkdir(parents=True)  # no sibling 'kirocrew' launcher
        monkeypatch.setattr(sys, "executable", str(bindir / "python"))

        assert run_marker.gateway_launcher_path() is None
        run_marker.write_marker(7000)
        assert not run_marker.marker_path(7000).exists()


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
        assert a.id == "cloud-desktop-1" and a.remote_port == 7777 and a.was_connected is False
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
        reg.set_last_active("cd-1")
        assert reg.get_last_active().id == "cd-1"

    def test_remove_clears_last_active_and_reload(self, tmp_path):
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        reg.set_last_active("cd-1")
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


class _FakeTunnel:
    def __init__(self, iid, ssh_host, lp, rp, *, connect_timeout_secs=0, compression=True, probe_failure_threshold=0, on_exit=None):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        self.iid = iid
        self.stopped = False
        self.start_result = True
        self._S = TunnelState
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
        import kiro_crew.instances.ssh_tunnel_manager as stm

        monkeypatch.setattr(stm, "_is_port_free", lambda port, host="127.0.0.1": True)

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

        async def ok_mint(host, *, remote_bin="", ttl="20h", remote_port=None, embed_parent_port=None):
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
        mgr_on = SshTunnelManager(
            reg, base_port=53500, mint_token=ok_mint, tunnel_factory=factory
        )
        reg.add(name="CD2", ssh_host="cd-2-alias", instance_id="cd-2")
        assert (await mgr_on.connect("cd-2")).state == TunnelState.CONNECTED
        assert captured["compression"] is True


class TestSshTunnelManager:
    @pytest.fixture(autouse=True)
    def _free_ports(self, monkeypatch):
        # Connect now probes _is_port_free (CSE SEC-016 mirror conflict check).
        # Keep these unit tests hermetic / independent of the host's real ports.
        import kiro_crew.instances.ssh_tunnel_manager as stm

        monkeypatch.setattr(stm, "_is_port_free", lambda port, host="127.0.0.1": True)

    def _mgr(self, tmp_path, *, mint=None, factory=_FakeTunnel):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        reg = InstancesRegistry(path=tmp_path / "instances.json")

        async def ok_mint(host, *, remote_bin="", ttl="20h", remote_port=None, embed_parent_port=None):
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
        # CSE SEC-016 mirror: local_port == remote_port (default 7777), not an
        # allocator-assigned port.
        assert inst.was_connected is True and inst.local_port == inst.remote_port == 7777
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

        async def bad_mint(host, *, remote_bin="", ttl="20h", remote_port=None, embed_parent_port=None):
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
        # Regression: connect() records local_port (== remote_port under the
        # SEC-016 mirror), but disconnect() must reset it to the unallocated
        # sentinel. Otherwise the freed port stays recorded and reads as
        # reserved forever, blocking reconnect.
        from kiro_crew.instances.registry import _UNALLOCATED_PORT

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")

        await mgr.connect("cd-1")
        assert reg.get("cd-1").local_port == reg.get("cd-1").remote_port == 7777

        await mgr.disconnect("cd-1")
        inst = reg.get("cd-1")
        assert inst.local_port == _UNALLOCATED_PORT  # port hint cleared
        assert inst.was_connected is False
        # the cleared port is no longer counted as reserved
        assert 7777 not in mgr._reserved_ports()

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


class _State:
    def __init__(self, registry, manager=None):
        self.instances_registry = registry
        self.instances_manager = manager


def _enable(tmp_path: Path, monkeypatch, *, enabled=True):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"instances": {"enabled": enabled}}))
    from kiro_crew.config import loader

    loader._invalidate_config_cache()


def _body(resp):
    return json.loads(resp.body.decode())


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
            handlers.api_instances_add(_FakeReq(state, body={"name": "CD", "ssh_host": "cd-1-alias"}))
        )
        assert r.status == 201
        r = asyncio.run(handlers.api_instances_list(_FakeReq(state)))
        b = _body(r)
        assert b["warm_set_cap"] == 5 and len(b["instances"]) == 1
        # no manager on this state => enabled-in-config but not active (needs restart)
        assert b["active"] is False

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

    def test_status_404(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        r = asyncio.run(
            handlers.api_instances_status(_FakeReq(_State(self._reg(tmp_path)), match={"id": "ghost"}))
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

    def test_update_paths(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = _State(reg)
        # success (only allowed fields applied)
        r = asyncio.run(
            handlers.api_instances_update(_FakeReq(state, match={"id": "cd-1"}, body={"name": "New"}))
        )
        assert r.status == 200 and _body(r)["name"] == "New"
        # unknown id -> 404
        assert (
            asyncio.run(
                handlers.api_instances_update(_FakeReq(state, match={"id": "ghost"}, body={"name": "x"}))
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
        assert asyncio.run(handlers.api_instances_update(_FakeReq(state, match={"id": "cd-1"}))).status == 400
        assert (
            asyncio.run(
                handlers.api_instances_update(_FakeReq(state, match={"id": "cd-1"}, body=42))
            ).status
            == 400
        )

    def test_remove_success_and_404(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = _State(reg)
        r = asyncio.run(handlers.api_instances_remove(_FakeReq(state, match={"id": "cd-1"})))
        assert r.status == 200 and _body(r)["removed"] == "cd-1"
        assert (
            asyncio.run(handlers.api_instances_remove(_FakeReq(state, match={"id": "ghost"}))).status == 404
        )

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
            asyncio.run(handlers.api_instances_connect(_FakeReq(state, match={"id": "ghost"}))).status == 404
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
                handlers.api_instances_restart(_FakeReq(_State(reg, object()), match={"id": "ghost"}))
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
            handlers.api_instances_restart(_FakeReq(_State(reg, FakeMgr(True)), match={"id": "cd-1"}))
        )
        assert r.status == 200 and _body(r)["ok"] is True
        # failure -> 502
        r = asyncio.run(
            handlers.api_instances_restart(_FakeReq(_State(reg, FakeMgr(False)), match={"id": "cd-1"}))
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


class TestDiagnostics:
    def _set_probes(self, monkeypatch, ssh, remote, local):
        from kiro_crew.instances import diagnostics as diag

        async def _ssh(h):
            return ssh

        async def _rem(h, p):
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

    def __init__(self, iid, ssh_host, lp, rp, *, connect_timeout_secs=0, compression=True, probe_failure_threshold=0, on_exit=None):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        self._S = TunnelState
        self.status = TunnelStatus(instance_id=iid, local_port=lp, remote_port=rp)
        self.start_result = True

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
        import kiro_crew.instances.ssh_tunnel_manager as stm

        monkeypatch.setattr(stm, "_is_port_free", lambda port, host="127.0.0.1": True)

    def _mgr(self, tmp_path, *, mint=None, factory=_ResilTunnel):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        reg = InstancesRegistry(path=tmp_path / "instances.json")

        async def ok_mint(host, *, remote_bin="", ttl="20h", remote_port=None, embed_parent_port=None):
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
        ok = await mgr._rebuild(reg.get("cd-1"), "cd-1-alias", 53999)
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
        t._stderr_buf = "** WARNING: connection is not using a post-quantum key exchange algorithm.\n"
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
    async def test_reap_orphan_forwarder_kills_only_matching(self, tmp_path, monkeypatch):
        import os as _os
        import signal as _signal

        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        reg = InstancesRegistry(path=tmp_path / "i.json")
        mgr = SshTunnelManager(reg, base_port=53400)  # default (real) factory

        async def fake_ps():
            return [
                "111 ssh -N -o BatchMode=yes -L 127.0.0.1:7779:127.0.0.1:7879 host-a",  # match
                "222 ssh -N -L 127.0.0.1:9999:127.0.0.1:9999 host-b",  # different port
                "333 some-daemon --flag -L 127.0.0.1:7779: not-ssh",  # not ssh
            ]

        mgr._ps_lines = fake_ps  # type: ignore[assignment]
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(_os, "kill", lambda pid, sig: killed.append((pid, sig)))
        n = await mgr._reap_orphan_forwarder(7779)
        assert n == 1 and killed == [(111, _signal.SIGTERM)]

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
    async def test_refresh_passes_instance_remote_port(self, tmp_path):
        # F1 regression: connect AND proactive re-mint must target the instance's
        # actual remote_port (not the default 7777), or a non-default-port
        # instance gets an invalid re-minted token.
        seen: list = []

        async def capturing_mint(host, *, remote_bin="", ttl="20h", remote_port=None, embed_parent_port=None):
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

        async def fake_run(host, sub, *, remote_bin="", marker_port=None, timeout_secs=60.0):
            calls["a"] = (host, sub, marker_port)
            return (0, "")

        monkeypatch.setattr(stm, "run_remote_kirocrew", fake_run)
        r = asyncio.run(mgr.restart_remote("cd-1"))
        # remote_port defaults to 7777 → threaded so restart uses the marker resolver.
        assert r["ok"] and calls["a"] == ("cd-1-alias", "restart", 7777)
        # validation failure
        r = asyncio.run(mgr.restart_remote("bad"))
        assert not r["ok"] and "invalid ssh settings" in r["message"]
        # unknown
        r = asyncio.run(mgr.restart_remote("ghost"))
        assert not r["ok"]

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
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        monkeypatch.setattr(stm, "_is_port_free", lambda port, host="127.0.0.1": port_free)

        async def ok_mint(host, *, remote_bin="", ttl="20h", remote_port=None, embed_parent_port=None):
            return "TOK"

        return SshTunnelManager(reg, mint_token=ok_mint, tunnel_factory=factory)

    @pytest.mark.asyncio
    async def test_local_port_mirrors_remote_port(self, tmp_path, monkeypatch):
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
        # local forward port == remote (configured) port
        assert captured["lp"] == 7900 == captured["rp"]
        assert reg.get("cd-1").local_port == 7900

    @pytest.mark.asyncio
    async def test_mirror_overrides_stale_local_port(self, tmp_path, monkeypatch):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        captured: dict = {}

        def factory(iid, ssh_host, lp, rp, **k):
            captured["lp"] = lp
            return _FakeTunnel(iid, ssh_host, lp, rp, **k)

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1", remote_port=7900)
        reg.update("cd-1", local_port=8123)  # stale random local port from old allocator
        mgr = self._mgr(reg, factory, monkeypatch)

        status = await mgr.connect("cd-1")
        assert status.state == TunnelState.CONNECTED
        assert captured["lp"] == 7900  # stale 8123 ignored; mirror wins
        assert reg.get("cd-1").local_port == 7900

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
        assert "already in use" in (status.error or "")
        assert "distinct remote port" in (status.error or "")
        # We fail before opening the tunnel — factory never invoked.
        assert "called" not in captured


class TestLastError:
    """Retained last-error: a failed connect remembers *why* so a sticky tab
    whose tunnel is down can show its error instead of a bare "disconnected"."""

    @pytest.fixture(autouse=True)
    def _free_ports(self, monkeypatch):
        import kiro_crew.instances.ssh_tunnel_manager as stm

        monkeypatch.setattr(stm, "_is_port_free", lambda port, host="127.0.0.1": True)

    def _mgr(self, tmp_path, *, mint=None, factory=_FakeTunnel):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        reg = InstancesRegistry(path=tmp_path / "instances.json")

        async def ok_mint(host, *, remote_bin="", ttl="20h", remote_port=None, embed_parent_port=None):
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

        async def bad_mint(host, *, remote_bin="", ttl="20h", remote_port=None, embed_parent_port=None):
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

        async def flaky_mint(host, *, remote_bin="", ttl="20h", remote_port=None, embed_parent_port=None):
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

        async def bad_mint(host, *, remote_bin="", ttl="20h", remote_port=None, embed_parent_port=None):
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
        import kiro_crew.instances.ssh_tunnel_manager as stm

        monkeypatch.setattr(stm, "_is_port_free", lambda port, host="127.0.0.1": True)

    def _mgr(self, tmp_path, *, mint=None, factory=_FakeTunnel):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        reg = InstancesRegistry(path=tmp_path / "instances.json")

        async def ok_mint(host, *, remote_bin="", ttl="20h", remote_port=None, embed_parent_port=None):
            return "SECRET_TOK"

        return reg, SshTunnelManager(
            reg, base_port=53400, mint_token=mint or ok_mint, tunnel_factory=factory
        )

    @pytest.mark.asyncio
    async def test_surfaces_error_when_no_live_tunnel(self, tmp_path):
        import types

        from kiro_crew.dashboard.handlers_instances import _status_for
        from kiro_crew.instances.token_mint import TokenMintError

        async def bad_mint(host, *, remote_bin="", ttl="20h", remote_port=None, embed_parent_port=None):
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
        import kiro_crew.instances.ssh_tunnel_manager as stm

        monkeypatch.setattr(stm, "_is_port_free", lambda port, host="127.0.0.1": True)

    def _mgr(self, tmp_path, *, mint=None, factory=_FakeTunnel):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        reg = InstancesRegistry(path=tmp_path / "instances.json")

        async def ok_mint(host, *, remote_bin="", ttl="20h", remote_port=None, embed_parent_port=None):
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

        async def mint(host, *, remote_bin="", ttl="20h", remote_port=None, embed_parent_port=None):
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
