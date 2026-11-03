"""A process restart never replaces the image in the middle of an unattended app update.

Both exec-based restarts skip ``exit_path._shutdown``'s app-update drain. The
update coordinator's restart defers while an update applies, and holds the loop
off new ones until it execs or defers. The dashboard restart waits the budget
shutdown gives an update. The in-flight census counts an applying update, so the
coordinator does not start an apply beside one.
"""

from __future__ import annotations

import os
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from slack_gateway_helpers import _restore_update_check_cache  # noqa: F401
from slack_gateway_helpers import _make_orchestrator

from kiro_crew import platform_compat, update_ownership
from kiro_crew.apps import auto_update
from kiro_crew.dashboard.handlers import updates
from kiro_crew.slack import gateway as gw


@pytest.fixture(autouse=True)
def _loop_state(_floor_monkeypatch):
    """The applying set and the restart hold are module state; each test starts clear."""
    _floor_monkeypatch.setattr(auto_update, "_APPLYING", set())
    _floor_monkeypatch.setattr(auto_update, "_RESTART_HOLD", {"holders": 0})


def _executable(tmp_path) -> str:
    """A real native executable, so the restart paths' interpreter guard passes."""
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


def _orchestrator(drain: AsyncMock) -> gw.GatewayOrchestrator:
    orch = _make_orchestrator()
    orch.dashboard_state = None
    orch.sessions = SimpleNamespace(
        inbound_callback_count=0,
        fence_update_restart=MagicMock(return_value=True),
        close_all=AsyncMock(),
    )
    orch._drain_update_callback_work = drain
    return orch


class TestTheCensus:
    def test_an_app_update_mid_apply_counts_as_background_work(self):
        orch = _make_orchestrator()
        orch.dashboard_state = None
        orch._session_tasks = {}
        assert orch._count_in_flight_work() == 0
        auto_update._APPLYING.add("my-app")
        assert orch._in_flight_work_counts() == (0, 1)


class TestTheUpdateRestart:
    @pytest.mark.asyncio
    async def test_an_app_update_mid_apply_defers_it_before_anything_closes(
        self, monkeypatch, tmp_path
    ):
        auto_update._APPLYING.add("my-app")
        drain = AsyncMock(return_value=True)
        orch = _orchestrator(drain)
        reexec = MagicMock()
        monkeypatch.setattr(gw.platform_compat, "reexec_python_module", reexec)
        interpreter = _executable(tmp_path)

        def respawn() -> str:
            return interpreter

        await orch._restart_after_update(respawn)

        assert orch._update_apply_deferred is True
        assert orch._pending_update_respawn is respawn
        drain.assert_not_awaited()
        orch.sessions.close_all.assert_not_awaited()
        reexec.assert_not_called()
        # The watchdog waits out this deferral like a callback drain's.
        assert update_ownership.current_owner() == "a deferred restart into an applied update"
        # The loop is not held, so the update can finish and the next can run.
        assert auto_update.restart_hold_reason() == ""
        assert auto_update.applying()

    @pytest.mark.asyncio
    async def test_it_holds_the_loop_while_it_drains_and_releases_on_deferral(
        self, monkeypatch, tmp_path
    ):
        seen: list[str] = []

        async def _drain(*, timeout):
            seen.append(auto_update.restart_hold_reason())
            return False

        orch = _orchestrator(AsyncMock(side_effect=_drain))
        monkeypatch.setattr(gw, "flush_breadcrumb_writes", lambda _timeout: None)
        interpreter = _executable(tmp_path)

        await orch._restart_after_update(lambda: interpreter)

        assert seen == [auto_update.RESTART_HOLD_REASON]
        assert orch._update_apply_deferred is True
        assert auto_update.restart_hold_reason() == ""


def _dashboard_state() -> SimpleNamespace:
    return SimpleNamespace(
        _gateway_restart_in_progress=False,
        push_update_progress=Mock(),
        sessions=SimpleNamespace(close_all=AsyncMock()),
    )


class TestTheDashboardRestart:
    @pytest.mark.asyncio
    async def test_it_waits_for_an_app_update_and_never_cancels_it(self, monkeypatch, tmp_path):
        order: list[str] = []
        auto_update._APPLYING.add("my-app")

        async def _settle(timeout):
            order.append(f"settle:{timeout}")
            auto_update._APPLYING.discard("my-app")
            return auto_update.hold_for_restart()

        monkeypatch.setattr(auto_update, "settle_for_restart", _settle)
        monkeypatch.setattr(updates, "resolve_restart_launcher", lambda: None)
        monkeypatch.setattr(updates, "reexec_launcher", Mock())
        monkeypatch.setattr(updates, "reexec_python_module", lambda *_a, **_k: order.append("exec"))
        state = _dashboard_state()
        state.sessions.close_all.side_effect = lambda: order.append("close_all")

        assert await updates._restart_gateway(state, resolver=lambda: _executable(tmp_path))

        from kiro_crew.gateway_shutdown_budget import UPDATE_INSTALLER_STOP_SECS

        assert order == [f"settle:{UPDATE_INSTALLER_STOP_SECS}", "close_all", "exec"]
        # The stand-in exec returned, so the restart ended and let the loop go.
        assert auto_update.restart_hold_reason() == ""

    @pytest.mark.asyncio
    async def test_the_slack_restart_waits_for_it_before_closing_sessions(self, monkeypatch):
        from kiro_crew.gateway_shutdown_budget import UPDATE_INSTALLER_STOP_SECS
        from kiro_crew.slack.events import _handle_restart
        from kiro_crew.slack.handler import set_owner_id

        order: list[str] = []

        async def _settle(timeout):
            order.append(f"settle:{timeout}")
            return True

        monkeypatch.setattr(auto_update, "settle_for_restart", _settle)
        monkeypatch.setattr("kiro_crew.slack.events.sel", lambda: MagicMock())
        monkeypatch.setattr(
            "kiro_crew.sel.flush_audit_queue_before_hard_exit", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            "kiro_crew.cli.drain_log_queue_before_hard_exit", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(platform_compat, "hard_exit", lambda code: order.append(f"exit:{code}"))
        monkeypatch.setenv("INVOCATION_ID", "abc")
        set_owner_id("U_OWNER")
        orch = MagicMock()
        orch.dashboard_state = None
        orch.sessions = MagicMock()
        orch.sessions.close_all = AsyncMock(side_effect=lambda **_k: order.append("close_all"))

        await _handle_restart(orch, "U_OWNER", "", AsyncMock())

        assert order == [f"settle:{UPDATE_INSTALLER_STOP_SECS}", "close_all", "exit:1"]

    @pytest.mark.asyncio
    async def test_a_refused_restart_takes_no_hold(self, monkeypatch, tmp_path):
        settle = AsyncMock(return_value=True)
        monkeypatch.setattr(auto_update, "settle_for_restart", settle)
        monkeypatch.setattr(updates, "resolve_restart_launcher", lambda: None)
        state = _dashboard_state()

        assert not await updates._restart_gateway(state, resolver=lambda: str(tmp_path / "gone"))

        settle.assert_not_awaited()
        assert auto_update.restart_hold_reason() == ""
