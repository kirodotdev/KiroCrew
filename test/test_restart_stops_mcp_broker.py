"""The in-app restart paths stop the MCP broker this gateway owns before the exec.

``_shutdown`` stops the pooled MCP gateway broker on a clean exit, because
``gatewayd`` is spawned detached and would otherwise outlive the process that
started it. The two exec-based restart paths -- the dashboard's
``_restart_gateway`` and the update coordinator's ``_restart_after_update`` --
never ran that shutdown. They relied on the successor to adopt or replace the
survivor, and the successor can do that only when the daemon's recorded owner
pid is its own (an ``os.execv`` that kept the pid) or gone. Through a launcher
that runs the selected gateway as a supervised child, the old pid lives on as
the supervisor: the daemon's owner-liveness sweeper keeps finding a live owner
with an unchanged start time and never exits, and the successor's election meets
a daemon "owned by another LIVE gateway" -- refused for the whole generation, so
every stubbed server falls back to per-session exec and a daemon on the previous
code keeps its pooled backends running.

These tests pin the ordering on both paths (sessions closed, broker stopped,
exec), that a refused or deferred restart leaves the broker serving, and that a
broker which will not stop cannot strand a gateway whose sessions are already
closed.
"""

from __future__ import annotations

import os
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers import updates
from kiro_crew.slack import gateway as slack_gateway
from kiro_crew.slack.gateway import GatewayOrchestrator


def _executable(tmp_path) -> str:
    """A real native executable, so the restart paths' interpreter guard passes.

    Contents only (``copyfile``): the single piece of metadata the guard reads
    is the exec bit, set on the next line.
    """
    source = next(
        (c for c in ("/bin/true", "/usr/bin/true", "/bin/cat") if os.path.exists(c)),
        None,
    )
    if source is None:  # pragma: no cover - a POSIX host without any of these
        pytest.skip("no native binary available to copy")
    target = tmp_path / "python"
    shutil.copyfile(source, target)
    platform_compat.chmod_safe(target, 0o700)
    return str(target)


def _recorder(seen: list[str], label: str, **kwargs) -> AsyncMock:
    async def _record(*_a, **_k):
        seen.append(label)

    return AsyncMock(side_effect=_record, **kwargs)


def _state(seen: list[str], *, stop=None) -> SimpleNamespace:
    state = SimpleNamespace(
        _gateway_restart_in_progress=False,
        push_update_progress=Mock(),
        sessions=SimpleNamespace(close_all=_recorder(seen, "close_all")),
    )
    if stop is not None:
        state._mcp_gateway_stop = stop
    return state


def _dashboard_exec_seams(monkeypatch, seen: list[str]) -> None:
    monkeypatch.setattr(updates, "resolve_restart_launcher", lambda: None)
    monkeypatch.setattr(updates, "reexec_launcher", Mock())
    monkeypatch.setattr(updates, "reexec_python_module", lambda *_a, **_k: seen.append("exec"))


class TestTheDashboardRestart:
    @pytest.mark.asyncio
    async def test_the_restart_owns_a_missing_bundle_through_its_teardown(
        self, monkeypatch, tmp_path
    ):
        """Every caller restarts through here, so none of them is raced by the watchdog.

        After an outside installer the bundle can be gone; close_all drops the
        in-flight count to 0, and an unowned gap would let the watchdog fire
        in the middle of the teardown.
        """
        from kiro_crew import update_ownership

        seen: list[str] = []
        owners: list[object] = []
        _dashboard_exec_seams(monkeypatch, seen)
        state = _state(seen)
        state.sessions.close_all = AsyncMock(
            side_effect=lambda: owners.append(update_ownership.current_owner())
        )

        assert await updates._restart_gateway(state, resolver=lambda: _executable(tmp_path)) is True

        assert owners == ["the restart into an applied update"]
        assert update_ownership.current_owner() is None

    @pytest.mark.asyncio
    async def test_a_restart_that_coalesces_leaves_a_deferral_in_place(self, monkeypatch, tmp_path):
        """Only a restart that commits ends one; a coalesced click must not let the watchdog fire."""
        from kiro_crew import update_ownership

        seen: list[str] = []
        _dashboard_exec_seams(monkeypatch, seen)
        state = _state(seen)
        state._gateway_restart_in_progress = True
        update_ownership.note_restart_deferred()

        restarted = await updates._restart_gateway(state, resolver=lambda: _executable(tmp_path))

        assert restarted is False
        assert update_ownership.current_owner() == "a deferred restart into an applied update"

    @pytest.mark.asyncio
    async def test_a_restart_with_no_interpreter_ends_the_deferral(self, monkeypatch, tmp_path):
        """The pruned tree took the bundle too; the watchdog's exit is what relaunches."""
        from kiro_crew import update_ownership

        seen: list[str] = []
        _dashboard_exec_seams(monkeypatch, seen)
        state = _state(seen)
        update_ownership.note_restart_deferred()

        restarted = await updates._restart_gateway(
            state, resolver=lambda: str(tmp_path / "pruned" / "python")
        )

        assert restarted is False
        assert seen == []
        assert update_ownership.current_owner() is None

    @pytest.mark.asyncio
    async def test_a_restart_that_commits_ends_the_deferral(self, monkeypatch, tmp_path):
        from kiro_crew import update_ownership

        seen: list[str] = []
        _dashboard_exec_seams(monkeypatch, seen)
        state = _state(seen)
        update_ownership.note_restart_deferred()

        assert await updates._restart_gateway(state, resolver=lambda: _executable(tmp_path)) is True

        assert update_ownership.current_owner() is None

    @pytest.mark.asyncio
    async def test_stops_the_broker_between_closing_sessions_and_the_exec(
        self, monkeypatch, tmp_path
    ):
        """Sessions first, so nothing is mid-call; broker next; exec last."""
        seen: list[str] = []
        _dashboard_exec_seams(monkeypatch, seen)
        stop = _recorder(seen, "stop")
        state = _state(seen, stop=stop)

        assert await updates._restart_gateway(state, resolver=lambda: _executable(tmp_path)) is True

        stop.assert_awaited_once_with()
        assert seen == ["close_all", "stop", "exec"]

    @pytest.mark.asyncio
    async def test_a_refused_restart_leaves_the_broker_serving(self, monkeypatch, tmp_path):
        """The guard refuses before the drain; the gateway keeps serving, broker included."""
        seen: list[str] = []
        _dashboard_exec_seams(monkeypatch, seen)
        stop = _recorder(seen, "stop")
        state = _state(seen, stop=stop)

        assert (
            await updates._restart_gateway(state, resolver=lambda: str(tmp_path / "pruned"))
            is False
        )

        stop.assert_not_awaited()
        assert seen == []

    @pytest.mark.asyncio
    async def test_a_broker_that_will_not_stop_does_not_strand_the_restart(
        self, monkeypatch, tmp_path
    ):
        """Past the point of no return, the exec must still be reached."""
        seen: list[str] = []
        _dashboard_exec_seams(monkeypatch, seen)
        state = _state(seen, stop=AsyncMock(side_effect=RuntimeError("daemon wedged")))

        assert await updates._restart_gateway(state, resolver=lambda: _executable(tmp_path)) is True

        assert seen == ["close_all", "exec"]
        assert "error" not in [c.args[0] for c in state.push_update_progress.call_args_list]


def _orchestrator(seen: list[str], **overrides) -> SimpleNamespace:
    fields = dict(
        _pending_update_respawn=None,
        _update_apply_deferred=False,
        _UPDATE_DRAIN_TIMEOUT_SECS=GatewayOrchestrator._UPDATE_DRAIN_TIMEOUT_SECS,
        dashboard_state=None,
        sessions=SimpleNamespace(
            close_all=_recorder(seen, "close_all"), fence_update_restart=Mock(return_value=True)
        ),
        _drain_update_callback_work=AsyncMock(return_value=True),
        _stop_mcp_broker=_recorder(seen, "stop"),
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _update_exec_seams(monkeypatch, seen: list[str]) -> None:
    monkeypatch.setattr(slack_gateway, "resolve_restart_launcher", lambda: None)
    monkeypatch.setattr(slack_gateway, "flush_breadcrumb_writes", Mock())
    monkeypatch.setattr(slack_gateway.platform_compat, "reexec_launcher", Mock())
    monkeypatch.setattr(
        slack_gateway.platform_compat,
        "reexec_python_module",
        lambda *_a, **_k: seen.append("exec"),
    )


class TestTheUpdateRestart:
    @pytest.mark.asyncio
    async def test_stops_the_broker_between_closing_sessions_and_the_exec(
        self, monkeypatch, tmp_path
    ):
        seen: list[str] = []
        _update_exec_seams(monkeypatch, seen)
        orch = _orchestrator(seen)

        await GatewayOrchestrator._restart_after_update(orch, lambda: _executable(tmp_path))

        orch._stop_mcp_broker.assert_awaited_once_with()
        assert seen == ["close_all", "stop", "exec"]

    @pytest.mark.asyncio
    async def test_a_deferred_restart_leaves_the_broker_serving(self, monkeypatch, tmp_path):
        """A pre-fence drain that does not finish defers the restart with every
        session open; the broker they use must stay up with them."""
        seen: list[str] = []
        _update_exec_seams(monkeypatch, seen)
        orch = _orchestrator(
            seen,
            _UPDATE_DRAIN_TIMEOUT_SECS=0.01,
            _drain_update_callback_work=AsyncMock(return_value=False),
        )

        await GatewayOrchestrator._restart_after_update(orch, lambda: _executable(tmp_path))

        assert orch._update_apply_deferred is True
        orch._stop_mcp_broker.assert_not_awaited()
        assert seen == []


def _real_orchestrator() -> GatewayOrchestrator:
    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U1"}):
        orch = GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    orch.dashboard_state = None
    orch.sessions = None
    return orch


class TestTheDaemonThisGatewayOwns:
    """Through the real orchestrator: the seam is ``_stop_mcp_broker``, and what
    it reaches is the manager's ``shutdown`` -- the SIGTERM the daemon needs to
    take its pooled backends down with it and release the socket."""

    @pytest.mark.asyncio
    async def test_the_update_restart_terminates_it_before_the_exec(self, monkeypatch, tmp_path):
        seen: list[str] = []
        _update_exec_seams(monkeypatch, seen)
        orch = _real_orchestrator()
        manager = MagicMock()
        manager.shutdown = _recorder(seen, "shutdown")
        orch._mcp_gateway_manager = manager

        await orch._restart_after_update(lambda: _executable(tmp_path))

        manager.shutdown.assert_awaited_once_with()
        assert orch._mcp_gateway_manager is None
        assert seen == ["shutdown", "exec"]

    @pytest.mark.asyncio
    async def test_the_dashboard_restart_reaches_it_through_the_wired_seam(
        self, monkeypatch, tmp_path
    ):
        """``_wire_mcp_gateway_dashboard`` is what the handler's stop rides on."""
        seen: list[str] = []
        _dashboard_exec_seams(monkeypatch, seen)
        orch = _real_orchestrator()
        manager = MagicMock()
        manager.shutdown = _recorder(seen, "shutdown")
        orch._mcp_gateway_manager = manager
        state = _state(seen)
        orch.dashboard_state = state
        orch._wire_mcp_gateway_dashboard()

        assert await updates._restart_gateway(state, resolver=lambda: _executable(tmp_path)) is True

        manager.shutdown.assert_awaited_once_with()
        assert orch._mcp_gateway_manager is None
        assert seen == ["close_all", "shutdown", "exec"]
