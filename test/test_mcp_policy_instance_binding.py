"""A pooled Kiro Crew MCP backend authenticates against the gateway that owns it.

The reported failure: in a Codex-backed dashboard session, ``kirocrew-core`` refused
``memory_recall`` with ``resolution_failed (cause: URLError)`` and
``kirocrew-dashboard`` answered ``internal_auth_mismatch``. Both servers read the
gateway port from their own environment and pair it with the credential published
for that port, so the port decides which INSTANCE they authenticate against.

A pooled control-plane backend is spawned by the MCP gateway daemon, from the
daemon's environment, and the daemon is started during gateway boot BEFORE the
dashboard binds and exports ``KIROCREW_BOUND_PORT``. It never inherits the bound
port; it does inherit any ``KIROCREW_PORT`` the gateway's shell carried. A gateway
started from another gateway's agent shell therefore pointed every pooled
backend at THAT gateway: a dead port gives ``URLError`` on the policy read, a live
one answers this home's credential with ``internal_auth_mismatch``.

Covered here:

* the daemon binds a control-plane backend to its owner's run-marker port and
  drops the foreign ``KIROCREW_PORT`` (the failing topology);
* a credential rotation and restart on the same port is picked up by the next
  read, and a client bound to instance A never sends to instance B;
* the negative cache recovers through a half-open probe after the gateway comes
  back, while an excluded tool stays excluded;
* a codex stub for a Crew server carries the instance env its fallback needs.
"""

from __future__ import annotations

import io
import json
import os
import time
import types
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from test_mcp_gateway_claim import _register

import kiro_crew.mcp_shared as mcp_shared
from kiro_crew.instances import run_marker
from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway.daemon import launch as daemon_launch
from kiro_crew.mcp_gateway.pool import PoolKey
from kiro_crew.port_resolution import resolve_client_port_src

OWNER_PORT = 47211  # instance A: the gateway that owns the daemon
FOREIGN_PORT = 47299  # instance B: the gateway whose shell started A

#: Launch approval is not this module's subject, but the resolver tests below spawn
#: through ``env_target_resolver``, so the approval they rely on is granted
#: explicitly (:func:`approved_launch`) rather than inherited from the autouse
#: fixture. A test that does not ask for it runs against the real, empty store.
ENFORCE_LAUNCH_APPROVAL = True


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def approved_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat the resolved target as operator-approved, for the binding tests only."""
    from kiro_crew.mcp_gateway import launch_approval

    monkeypatch.setattr(launch_approval, "launch_approved", lambda *_a, **_k: True)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data home with a ``run/`` dir, and no inherited port variables."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    for name in ("KIROCREW_PORT", "KIROCREW_BOUND_PORT", "KIROCREW_BOUND_HOST"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "run").mkdir(mode=0o700)
    return tmp_path


def _publish_marker(home: Path, port: int, pid: int, token: str) -> None:
    """What ``run_marker.write_marker`` leaves for a gateway serving *port*."""
    run = home / "run"
    (run / f"gateway-{port}.bin").write_text("", encoding="ascii")
    (run / f"gateway-{port}.pid").write_text(f"{pid}\n", encoding="ascii")
    (run / f"gateway-{port}.start").write_text(f"{token}\n", encoding="ascii")


def _publish_secret(home: Path, port: int, value: str) -> None:
    """The per-port credential a gateway publishes beside its marker (synthetic)."""
    (home / "run" / f"gateway-{port}.secret").write_text(value, encoding="ascii")


@pytest.fixture
def owner(monkeypatch: pytest.MonkeyPatch) -> int:
    """This test process plays the owning gateway; its start token is pinned."""
    pid = os.getpid()
    monkeypatch.setattr(gw, "_OWNER_PID", pid)
    monkeypatch.setattr(run_marker, "pid_start_token", lambda p: "start-A" if p == pid else "")
    return pid


def _control_plane_key(name: str = "kirocrew-core") -> PoolKey:
    frame = _register("dashboard:chat-1-1")
    frame["server_name"] = name
    return PoolKey.from_register(frame)


def _resolve_in(env: dict[str, str]) -> tuple[int, str]:
    """Which port a backend spawned with *env* dials, through the real chain."""
    with patch.dict(os.environ, env, clear=True):
        return resolve_client_port_src(None)


# ── run_marker.port_served_by ─────────────────────────────────────────────


class TestPortServedBy:
    def test_the_owners_marker_names_its_port(self, home, owner):
        _publish_marker(home, OWNER_PORT, owner, "start-A")
        assert run_marker.port_served_by(owner) == OWNER_PORT

    def test_a_recycled_pid_with_another_start_identity_is_not_the_owner(self, home, owner):
        """A crashed gateway's marker can name a pid that now belongs to someone else."""
        _publish_marker(home, OWNER_PORT, owner, "start-of-a-dead-gateway")
        assert run_marker.port_served_by(owner) is None

    def test_an_empty_start_identity_is_unproven(self, home, owner):
        _publish_marker(home, OWNER_PORT, owner, "")
        assert run_marker.port_served_by(owner) is None

    def test_a_marker_of_another_gateway_is_ignored(self, home, owner):
        _publish_marker(home, FOREIGN_PORT, owner + 1, "start-A")
        assert run_marker.port_served_by(owner) is None

    def test_two_ports_naming_one_owner_is_ambiguous(self, home, owner):
        _publish_marker(home, OWNER_PORT, owner, "start-A")
        _publish_marker(home, FOREIGN_PORT, owner, "start-A")
        assert run_marker.port_served_by(owner) is None

    def test_no_run_dir_is_no_answer(self, tmp_path, monkeypatch, owner):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "empty"))
        assert run_marker.port_served_by(owner) is None


class TestPortServedBySuccessorOfASupervisor:
    """An in-app restart through a supervising launcher leaves the owner pid alive
    as the launcher; the successor gateway writes ITS pid into the marker, and an
    adopted daemon still names the supervisor as its owner."""

    SUCCESSOR = 424242

    @pytest.fixture
    def tokens(self, monkeypatch, owner):
        live = {owner: "start-A", self.SUCCESSOR: "start-S"}
        monkeypatch.setattr(run_marker, "pid_start_token", lambda p: live.get(p, ""))
        return live

    def _supervises(self, monkeypatch, verdict: bool, calls: list | None = None):
        def fake(pid, descendant):
            if calls is not None:
                calls.append((pid, descendant))
            return verdict

        monkeypatch.setattr(run_marker.platform_compat, "is_exec_supervisor_of", fake)

    def test_the_successors_marker_names_the_port(self, home, owner, tokens, monkeypatch):
        calls: list = []
        self._supervises(monkeypatch, True, calls)
        _publish_marker(home, OWNER_PORT, self.SUCCESSOR, "start-S")
        assert run_marker.port_served_by(owner) == OWNER_PORT
        assert calls == [(owner, self.SUCCESSOR)]

    def test_a_gateway_the_owner_does_not_supervise_is_not_its_successor(
        self, home, owner, tokens, monkeypatch
    ):
        """Another live gateway's marker (a Python process between, or no ancestry)."""
        self._supervises(monkeypatch, False)
        _publish_marker(home, FOREIGN_PORT, self.SUCCESSOR, "start-S")
        assert run_marker.port_served_by(owner) is None

    def test_a_successor_marker_with_a_stale_start_identity_is_unproven(
        self, home, owner, tokens, monkeypatch
    ):
        self._supervises(monkeypatch, True)
        _publish_marker(home, OWNER_PORT, self.SUCCESSOR, "start-of-a-dead-gateway")
        assert run_marker.port_served_by(owner) is None

    def test_an_empty_successor_start_identity_is_unproven(self, home, owner, tokens, monkeypatch):
        self._supervises(monkeypatch, True)
        _publish_marker(home, OWNER_PORT, self.SUCCESSOR, "")
        assert run_marker.port_served_by(owner) is None

    def test_two_successor_markers_are_ambiguous(self, home, owner, tokens, monkeypatch):
        self._supervises(monkeypatch, True)
        _publish_marker(home, OWNER_PORT, self.SUCCESSOR, "start-S")
        _publish_marker(home, FOREIGN_PORT, self.SUCCESSOR, "start-S")
        assert run_marker.port_served_by(owner) is None

    def test_the_owners_own_marker_wins_without_asking_about_ancestry(
        self, home, owner, tokens, monkeypatch
    ):
        calls: list = []
        self._supervises(monkeypatch, True, calls)
        _publish_marker(home, OWNER_PORT, owner, "start-A")
        _publish_marker(home, FOREIGN_PORT, self.SUCCESSOR, "start-S")
        assert run_marker.port_served_by(owner) == OWNER_PORT
        assert calls == []

    def test_an_adopted_daemon_binds_its_backend_to_the_successor(
        self, home, owner, tokens, monkeypatch
    ):
        """The reviewed trigger: operator KIROCREW_PORT set, daemon adopted after
        an in-app restart. The spawn is bound, not declined for the daemon's life."""
        self._supervises(monkeypatch, True)
        _publish_marker(home, OWNER_PORT, self.SUCCESSOR, "start-S")
        env = {"KIROCREW_PORT": str(OWNER_PORT)}
        daemon_launch.bind_control_plane_to_owner(env, "kirocrew-core")
        assert env["KIROCREW_BOUND_PORT"] == str(OWNER_PORT)


# ── the failing topology: a pooled control plane under a foreign KIROCREW_PORT ─


class TestPooledControlPlaneBinding:
    def test_the_daemon_env_alone_dials_the_foreign_instance(self):
        """The defect, stated through the real chain: this is what a pooled
        backend inherited before the fix. No bound port (the daemon predates the
        bind) and the shell's foreign port first in line."""
        assert _resolve_in({"KIROCREW_PORT": str(FOREIGN_PORT)}) == (FOREIGN_PORT, "env")

    def test_a_control_plane_backend_is_bound_to_its_owner(
        self, home, owner, monkeypatch, approved_launch
    ):
        _publish_marker(home, OWNER_PORT, owner, "start-A")
        monkeypatch.setenv("KIROCREW_PORT", str(FOREIGN_PORT))
        monkeypatch.setenv("MC_MCP_TARGET_KIROCREW_CORE", "kirocrew mcp-core")

        resolved = gw.env_target_resolver(_control_plane_key())

        assert resolved is not None
        _command, _args, env, _work_dir = resolved
        assert env["KIROCREW_BOUND_PORT"] == str(OWNER_PORT)
        assert "KIROCREW_PORT" not in env
        assert _resolve_in(env) == (OWNER_PORT, "bound")

    def test_every_crew_server_is_bound_not_only_core(
        self, home, owner, monkeypatch, approved_launch
    ):
        """``kirocrew-dashboard`` failed in the report too; it is a control plane here."""
        _publish_marker(home, OWNER_PORT, owner, "start-A")
        monkeypatch.setenv("KIROCREW_PORT", str(FOREIGN_PORT))
        monkeypatch.setenv("MC_MCP_TARGET_KIROCREW_DASHBOARD", "kirocrew mcp-dashboard")

        resolved = gw.env_target_resolver(_control_plane_key("kirocrew-dashboard"))

        assert resolved is not None
        assert _resolve_in(resolved[2]) == (OWNER_PORT, "bound")

    def test_a_third_party_backend_env_is_untouched(
        self, home, owner, monkeypatch, approved_launch
    ):
        """Only Crew's own servers read the gateway port; nothing else is rewritten."""
        _publish_marker(home, OWNER_PORT, owner, "start-A")
        monkeypatch.setenv("KIROCREW_PORT", str(FOREIGN_PORT))
        monkeypatch.setenv("MC_MCP_TARGET_ECHO_MCP", "echo-mcp --stdio")
        frame = _register("dashboard:chat-1-1")

        resolved = gw.env_target_resolver(PoolKey.from_register(frame))

        assert resolved is not None
        env = resolved[2]
        assert env["KIROCREW_PORT"] == str(FOREIGN_PORT)
        assert "KIROCREW_BOUND_PORT" not in env

    def test_an_unprovable_inherited_port_declines_the_spawn(
        self, home, owner, monkeypatch, approved_launch
    ):
        """The prewarm window: gatewayd listens, and prewarms, before its owner has
        bound and published a marker. A control-plane backend spawned then would be
        pinned to the inherited port for life, so the resolver declines and the
        spawn is retried once the owner is provable."""
        monkeypatch.setenv("KIROCREW_PORT", str(FOREIGN_PORT))
        monkeypatch.setenv("MC_MCP_TARGET_KIROCREW_CORE", "kirocrew mcp-core")

        assert gw.env_target_resolver(_control_plane_key()) is None

        _publish_marker(home, OWNER_PORT, owner, "start-A")
        resolved = gw.env_target_resolver(_control_plane_key())
        assert resolved is not None
        assert _resolve_in(resolved[2]) == (OWNER_PORT, "bound")

    def test_nothing_inherited_still_declines_without_proof(self, home, owner):
        """No marker yet and no port variable: the client chain would still pick a
        port (a lone live marker, else the default), which may be a sibling gateway
        on this data home, so the spawn is declined rather than guessed."""
        env = {"OTHER": "x"}
        with pytest.raises(daemon_launch.OwnerPortUnproven):
            daemon_launch.bind_control_plane_to_owner(env, "kirocrew-core")
        assert env == {"OTHER": "x"}

    def test_nothing_inherited_would_have_dialled_a_sibling(self, home, owner):
        """The reviewed trigger: a sibling gateway's live marker is the only one, so
        an unbound backend would resolve to ITS port."""
        sibling = os.getpid() + 7
        _publish_marker(home, FOREIGN_PORT, sibling, "start-sibling")
        env: dict[str, str] = {}
        with pytest.raises(daemon_launch.OwnerPortUnproven):
            daemon_launch.bind_control_plane_to_owner(env, "kirocrew-core")
        assert "KIROCREW_BOUND_PORT" not in env

    def test_an_unowned_daemon_reads_no_marker(self, home, monkeypatch):
        monkeypatch.setattr(gw, "_OWNER_PID", 0)
        lookup = MagicMock()
        monkeypatch.setattr(run_marker, "port_served_by", lookup)
        env = {"KIROCREW_PORT": str(FOREIGN_PORT)}
        daemon_launch.bind_control_plane_to_owner(env)
        assert env == {"KIROCREW_PORT": str(FOREIGN_PORT)}
        lookup.assert_not_called()

    def test_an_inherited_bound_port_is_not_evidence(self, home, owner):
        """An agent shell of gateway A carries A's KIROCREW_BOUND_PORT, and the
        daemon of a gateway started from it inherits that export before its own
        bind replaces it. The owner's marker overrides it."""
        _publish_marker(home, OWNER_PORT, owner, "start-A")
        env = {"KIROCREW_BOUND_PORT": str(FOREIGN_PORT)}
        daemon_launch.bind_control_plane_to_owner(env)
        assert env == {"KIROCREW_BOUND_PORT": str(OWNER_PORT)}

    def test_the_operators_own_port_survives_a_foreign_inherited_bound_port(self, home, owner):
        """The operator set KIROCREW_PORT to this gateway; the inherited export
        names the parent gateway. The operator's port is kept, the export replaced."""
        _publish_marker(home, OWNER_PORT, owner, "start-A")
        env = {"KIROCREW_PORT": str(OWNER_PORT), "KIROCREW_BOUND_PORT": str(FOREIGN_PORT)}
        daemon_launch.bind_control_plane_to_owner(env)
        assert env == {"KIROCREW_PORT": str(OWNER_PORT), "KIROCREW_BOUND_PORT": str(OWNER_PORT)}
        assert _resolve_in(env) == (OWNER_PORT, "env")

    def test_a_lasting_decline_warns_once_per_server(self, home, owner, monkeypatch, caplog):
        """With no marker the decline lasts the gateway's life, so the first one is
        a WARNING an operator sees. Later ones drop to DEBUG so the log stays readable."""
        import logging

        monkeypatch.setattr(daemon_launch, "_OWNER_PORT_DECLINES_WARNED", set())
        with caplog.at_level(logging.DEBUG, logger=daemon_launch.logger.name):
            for _ in range(3):
                with pytest.raises(daemon_launch.OwnerPortUnproven):
                    daemon_launch.bind_control_plane_to_owner(
                        {"KIROCREW_PORT": str(FOREIGN_PORT)}, "kirocrew-core"
                    )
            with pytest.raises(daemon_launch.OwnerPortUnproven):
                daemon_launch.bind_control_plane_to_owner(
                    {"KIROCREW_PORT": str(FOREIGN_PORT)}, "kirocrew-dashboard"
                )
        declines = [r for r in caplog.records if "not spawning pooled" in r.getMessage()]
        warned = [r.getMessage() for r in declines if r.levelno == logging.WARNING]
        assert len(declines) == 4
        assert len(warned) == 2
        assert any("'kirocrew-core'" in m for m in warned)
        assert any("'kirocrew-dashboard'" in m for m in warned)

    def test_an_unprovable_inherited_bound_port_declines_too(self, home, owner):
        env = {"KIROCREW_BOUND_PORT": str(FOREIGN_PORT)}
        with pytest.raises(daemon_launch.OwnerPortUnproven):
            daemon_launch.bind_control_plane_to_owner(env, "kirocrew-core")
        assert env == {"KIROCREW_BOUND_PORT": str(FOREIGN_PORT)}

    def test_the_operators_own_port_is_kept(self, home, owner):
        _publish_marker(home, OWNER_PORT, owner, "start-A")
        env = {"KIROCREW_PORT": str(OWNER_PORT)}
        daemon_launch.bind_control_plane_to_owner(env)
        assert env == {"KIROCREW_PORT": str(OWNER_PORT), "KIROCREW_BOUND_PORT": str(OWNER_PORT)}


# ── per-call identity: instance A never authenticates against instance B ─────


def _policy_response(exclude: list[str]) -> MagicMock:
    body = MagicMock()
    body.read.return_value = json.dumps({"exclude": exclude}).encode("utf-8")
    body.__enter__ = MagicMock(return_value=body)
    body.__exit__ = MagicMock(return_value=False)
    return body


@pytest.fixture
def policy_state(monkeypatch: pytest.MonkeyPatch):
    """Fresh resolver state, a recording audit sink, and a declared session key."""
    for name, value in (
        ("_last_failure_time", 0.0),
        ("_last_probe_time", 0.0),
        ("_last_startup_race_time", 0.0),
        ("_last_startup_race_key", ""),
        ("_failure_count", 0),
        ("_last_failure_class", ""),
    ):
        monkeypatch.setattr(mcp_shared, name, value)
    monkeypatch.setattr(mcp_shared, "_excluded_tools_by_session", {})
    monkeypatch.setattr(mcp_shared, "_included_tools_by_session", {})
    audit = MagicMock()
    monkeypatch.setattr(mcp_shared, "sel", lambda: audit)
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:chat-1-1")
    return audit


class TestInstanceIsolation:
    def test_the_bound_backend_sends_instance_As_credential_to_instance_A(
        self, home, owner, policy_state, monkeypatch
    ):
        _publish_marker(home, OWNER_PORT, owner, "start-A")
        _publish_secret(home, OWNER_PORT, "synthetic-credential-A")
        _publish_secret(home, FOREIGN_PORT, "synthetic-credential-B")
        env = {"KIROCREW_HOME": str(home), "KIROCREW_PORT": str(FOREIGN_PORT)}
        daemon_launch.bind_control_plane_to_owner(env, "kirocrew-core")
        urlopen = MagicMock(return_value=_policy_response(["blocked"]))
        monkeypatch.setattr(mcp_shared, "loopback_urlopen", urlopen)

        with patch.dict(os.environ, {**env, "KIROCREW_SESSION_KEY": "dashboard:chat-1-1"}):
            policy = mcp_shared._resolve_tool_policy()

        assert policy.unresolved == "" and policy.excluded == {"blocked"}
        request = urlopen.call_args.args[0]
        assert request.full_url == f"http://127.0.0.1:{OWNER_PORT}/api/session-tool-policy"
        assert request.get_header("X-internal-secret") == "synthetic-credential-A"
        assert all(str(FOREIGN_PORT) not in c.args[0].full_url for c in urlopen.call_args_list)

    def test_a_credential_rotation_and_restart_on_the_same_port_is_read_fresh(
        self, home, owner, policy_state, monkeypatch
    ):
        """The secret is re-read per call, so a restarted gateway's new credential
        is the one the next read sends. A refused read is never cached as an
        authorization result."""
        monkeypatch.setenv("KIROCREW_BOUND_PORT", str(OWNER_PORT))
        _publish_secret(home, OWNER_PORT, "synthetic-generation-1")
        forbidden = urllib.error.HTTPError(
            url="http://127.0.0.1/",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=io.BytesIO(b'{"error":"Forbidden","code":"internal_auth_mismatch"}'),
        )
        urlopen = MagicMock(side_effect=[forbidden, _policy_response(["blocked"])])
        monkeypatch.setattr(mcp_shared, "loopback_urlopen", urlopen)

        first = mcp_shared._resolve_tool_policy()
        # The gateway restarts on the same port and publishes a new credential.
        _publish_secret(home, OWNER_PORT, "synthetic-generation-2")
        second = mcp_shared._resolve_tool_policy()

        assert first.unresolved == "policy_forbidden"
        assert second.unresolved == "" and second.excluded == {"blocked"}
        sent = [c.args[0].get_header("X-internal-secret") for c in urlopen.call_args_list]
        assert sent == ["synthetic-generation-1", "synthetic-generation-2"]

    def test_the_failure_audit_names_the_port_and_its_source(self, home, policy_state, monkeypatch):
        """An ``env`` port is the rung a foreign KIROCREW_PORT occupies; the audit
        row says so instead of only naming the exception class."""
        monkeypatch.setenv("KIROCREW_PORT", str(FOREIGN_PORT))
        monkeypatch.setattr(
            mcp_shared,
            "loopback_urlopen",
            MagicMock(side_effect=urllib.error.URLError("connection refused")),
        )
        mcp_shared._resolve_tool_policy()
        (event,) = [
            c.kwargs
            for c in policy_state.log_api_access.call_args_list
            if c.kwargs.get("operation") == "tool_policy.resolution_failed"
        ]
        assert f"port={FOREIGN_PORT}" in event["resources"]
        assert "port_source=env" in event["resources"]


# ── negative-cache recovery ──────────────────────────────────────────────────


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class TestNegativeCacheRecovery:
    @pytest.fixture
    def clock(self, monkeypatch: pytest.MonkeyPatch) -> _Clock:
        """The mcp_shared family's ``time`` binding, never the process-wide clock.

        Patching ``time.monotonic`` itself would hand the synthetic clock to every
        deadline and liveness check in this worker. Instead the family's ``time``
        name is replaced with a stand-in whose ``monotonic`` is the test clock and
        whose every other attribute is the real ``time`` module's. The patch goes
        through the ``mcp_shared`` facade: several owners bind ``time``, and the
        facade mirrors the write to each of them and undoes it everywhere.
        """
        c = _Clock()
        real = {name: getattr(time, name) for name in dir(time) if not name.startswith("__")}
        monkeypatch.setattr(mcp_shared, "time", types.SimpleNamespace(**{**real, "monotonic": c}))
        return c

    @pytest.fixture
    def gateway(self, home, policy_state, monkeypatch):
        monkeypatch.setenv("KIROCREW_BOUND_PORT", str(OWNER_PORT))
        _publish_secret(home, OWNER_PORT, "synthetic-credential-A")
        urlopen = MagicMock()
        monkeypatch.setattr(mcp_shared, "loopback_urlopen", urlopen)
        return urlopen

    def test_inside_the_probe_interval_the_cache_answers_and_refuses(self, gateway, clock):
        gateway.side_effect = urllib.error.URLError("connection refused")
        assert mcp_shared._resolve_tool_policy().unresolved == "resolution_failed"
        clock.now += mcp_shared._NEGATIVE_CACHE_PROBE_INTERVAL - 1
        policy = mcp_shared._resolve_tool_policy()
        assert policy.unresolved == "resolution_failed"
        assert policy.detail == "URLError"
        assert gateway.call_count == 1

    def test_a_probe_after_recovery_resolves_and_keeps_the_exclusion(self, gateway, clock):
        """The gateway comes back 3s into the outage. Before the probe the session
        was refused for the rest of the 60s window; now the first call past the
        probe interval reads the policy, and the operator's exclusion holds."""
        gateway.side_effect = urllib.error.URLError("connection refused")
        mcp_shared._resolve_tool_policy()
        gateway.side_effect = None
        gateway.return_value = _policy_response(["blocked"])
        clock.now += mcp_shared._NEGATIVE_CACHE_PROBE_INTERVAL
        assert clock.now - 1000.0 < mcp_shared._NEGATIVE_CACHE_TTL

        policy = mcp_shared._resolve_tool_policy()

        assert policy.unresolved == ""
        assert "blocked" in policy.excluded
        assert gateway.call_count == 2

    def test_a_failed_probe_rearms_the_window_and_still_refuses(self, gateway, clock):
        gateway.side_effect = urllib.error.URLError("connection refused")
        mcp_shared._resolve_tool_policy()
        clock.now += mcp_shared._NEGATIVE_CACHE_PROBE_INTERVAL
        assert mcp_shared._resolve_tool_policy().unresolved == "resolution_failed"
        assert gateway.call_count == 2
        # The probe's failure restarted the interval: the next call is cached again.
        clock.now += 1
        assert mcp_shared._resolve_tool_policy().unresolved == "resolution_failed"
        assert gateway.call_count == 2

    def test_one_answered_read_ends_the_window_for_every_session(self, gateway, clock, monkeypatch):
        """A pooled backend serves many sessions; the outage was the gateway's, so
        the first answered read clears it for the siblings that never resolved."""
        gateway.side_effect = urllib.error.URLError("connection refused")
        mcp_shared._resolve_tool_policy("dashboard:chat-2-2")
        gateway.side_effect = None
        gateway.return_value = _policy_response([])
        clock.now += mcp_shared._NEGATIVE_CACHE_PROBE_INTERVAL
        assert mcp_shared._resolve_tool_policy("dashboard:chat-2-2").unresolved == ""

        clock.now += 1
        gateway.return_value = _policy_response(["blocked"])
        sibling = mcp_shared._resolve_tool_policy("dashboard:chat-3-3")

        assert sibling.unresolved == "" and sibling.excluded == {"blocked"}
        assert mcp_shared._last_failure_time == 0.0

    @pytest.mark.parametrize(
        ("status", "body", "probe_reason"),
        [
            (404, b"", "agent_not_resolved"),
            (403, b'{"error":"Forbidden","code":"member_session_unverified"}', "policy_forbidden"),
            (409, b'{"error":"bad spec"}', "policy_unreadable"),
        ],
    )
    def test_a_caller_specific_answer_still_ends_the_outage(
        self, gateway, clock, status, body, probe_reason
    ):
        """The probe that gets through may be answered about ITS caller only. The
        gateway answered, so the outage is over for the sibling sessions."""
        gateway.side_effect = urllib.error.URLError("connection refused")
        mcp_shared._resolve_tool_policy("dashboard:chat-2-2")
        gateway.side_effect = urllib.error.HTTPError(
            url="http://127.0.0.1/", code=status, msg="answered", hdrs=None, fp=io.BytesIO(body)
        )
        clock.now += mcp_shared._NEGATIVE_CACHE_PROBE_INTERVAL
        assert mcp_shared._resolve_tool_policy("dashboard:chat-2-2").unresolved == probe_reason

        clock.now += 1
        gateway.side_effect = None
        gateway.return_value = _policy_response(["blocked"])
        sibling = mcp_shared._resolve_tool_policy("dashboard:chat-3-3")

        assert sibling.unresolved == "" and sibling.excluded == {"blocked"}

    def test_a_call_that_cannot_dial_does_not_spend_the_probe(self, gateway, clock, monkeypatch):
        """A call with no session key returns before the read; if it took the
        probe slot, the next real caller would wait another whole interval."""
        gateway.side_effect = urllib.error.URLError("connection refused")
        mcp_shared._resolve_tool_policy("dashboard:chat-2-2")
        clock.now += mcp_shared._NEGATIVE_CACHE_PROBE_INTERVAL
        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.setattr(mcp_shared, "_policy_session_key", lambda: "")
        mcp_shared._resolve_tool_policy()
        assert gateway.call_count == 1

        gateway.side_effect = None
        gateway.return_value = _policy_response(["blocked"])
        assert mcp_shared._resolve_tool_policy("dashboard:chat-3-3").unresolved == ""
        assert gateway.call_count == 2

    def test_a_5xx_probe_keeps_the_outage_armed(self, gateway, clock):
        gateway.side_effect = urllib.error.URLError("connection refused")
        mcp_shared._resolve_tool_policy("dashboard:chat-2-2")
        gateway.side_effect = urllib.error.HTTPError(
            url="http://127.0.0.1/", code=503, msg="broken", hdrs=None, fp=io.BytesIO(b"")
        )
        clock.now += mcp_shared._NEGATIVE_CACHE_PROBE_INTERVAL
        assert mcp_shared._resolve_tool_policy("dashboard:chat-2-2").unresolved == (
            "resolution_failed"
        )
        clock.now += 1
        assert mcp_shared._resolve_tool_policy("dashboard:chat-3-3").unresolved == (
            "resolution_failed"
        )
        assert gateway.call_count == 2


# ── codex: a stub for a Crew server names its instance ───────────────────────


class TestCodexStubInstanceEnv:
    def test_a_crew_server_stub_carries_the_bound_port_and_a_third_party_stub_does_not(
        self, tmp_path, monkeypatch
    ):
        import codex_session_mcp_helpers as helpers

        from kiro_crew import agent as agent_mod
        from kiro_crew.acp import session_mcp
        from kiro_crew.providers.mirrors.codex import codex_projection

        agents = tmp_path / "agents"
        agents.mkdir()
        monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", agents)
        monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "settings-mcp.json")
        monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: True)
        monkeypatch.setattr(
            session_mcp,
            "managed_mcp_spec_entry",
            lambda name, **_k: dict(helpers._CORE) if name == "kirocrew-core" else None,
        )
        monkeypatch.setattr(session_mcp, "_mcp_registry_mode", lambda: False)
        monkeypatch.setenv("KIROCREW_BOUND_PORT", str(OWNER_PORT))
        monkeypatch.delenv("KIROCREW_PORT", raising=False)
        helpers._write_spec(
            agents,
            servers={"kirocrew-core": dict(helpers._CORE), "echo-mcp": {"command": "/bin/e"}},
            tools=["*"],
        )
        stubs = [
            {
                "name": "kirocrew-core",
                "command": "/stub",
                "args": [],
                "type": "stdio",
                "env": [{"name": "KIROCREW_STUB_SESSION_TOKEN", "value": "tok"}],
            },
            {"name": "echo-mcp", "command": "/stub", "args": [], "env": [], "type": "stdio"},
        ]

        out = codex_projection(
            "kirocrew",
            stub_server_names=("kirocrew-core", "echo-mcp"),
            stub_elements=stubs,
            session_key="dashboard:chat-1-1",
        ).params["mcpServers"]

        by_name = {e["name"]: e for e in out}
        core = {p["name"]: p["value"] for p in by_name["kirocrew-core"]["env"]}
        echo = {p["name"]: p["value"] for p in by_name["echo-mcp"]["env"]}
        assert core["KIROCREW_BOUND_PORT"] == str(OWNER_PORT)
        assert core["KIROCREW_STUB_SESSION_TOKEN"] == "tok"
        # Instance only: the stub names its session by token, never by key.
        assert "KIROCREW_SESSION_KEY" not in core
        assert echo == {}
        # The caller's stub list is not mutated.
        assert stubs[0]["env"] == [{"name": "KIROCREW_STUB_SESSION_TOKEN", "value": "tok"}]
