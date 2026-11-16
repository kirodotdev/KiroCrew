"""A watched pull request that merges or closes wakes its session one last time.

The shapes are the misses observed on a real host: a dashboard worker armed a gated
``monitor_start`` loop on its pull request, the pull request merged between two
quiet ticks, and the next tick stopped the loop with only the user's bell notice --
no turn, so the worker never reported. A conductor or a Bot on the dashboard arms
the same loop on the same path, so each of them is covered here too.

The second half pins the follow-on miss: ``monitor_update`` on that ended loop
answered "update requested", the turn boundary refused it afterwards, and the agent
believed a retarget had landed while nothing was watching anything.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from autonudge_helpers import _enable  # noqa: F401
from autonudge_helpers import _structured_monitor

from kiro_crew import autonudge as _an
from kiro_crew import mcp_core, session_directive
from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.autonudge_service.model import terminal_wake_line
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.mcp_tools import control
from kiro_crew.members_parts.member_sessions import member_slot_key
from kiro_crew.monitoring.models import MonitorOutcome
from kiro_crew.probes import gh_pr
from kiro_crew.slack import gateway as gw

HEAD = "e0a19a0a2e69" + "0" * 28
MERGED_AT = "2026-11-15T14:37:24Z"

#: The sessions that arm a gated pull-request loop on the dashboard.
DASHBOARD_SESSIONS = {
    "worker": "chat-965-1794618813",
    "conductor": "chat-827-1794359705",
    "bot": member_slot_key("helper"),
}


def _poll(state: str):
    def _fake(identity, message, probe):
        probe.observation = gh_pr.PrObservation(
            repo="acme/widgets",
            pr=37164,
            host="github.com",
            status="ok",
            observed_at=1_000.0,
            state=state,
            merged_at=MERGED_AT if state == "MERGED" else "",
            head=HEAD,
        )
        return _an.irq.Verdict(_an.irq.Outcome.QUIET, "read")

    return _fake


def _loop(slot_key: str) -> NudgeLoop:
    return NudgeLoop(
        id="mon37164",
        slot_key=slot_key,
        message="watch https://github.com/acme/widgets/pull/37164 to merge",
        idle_secs=600,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#37164"),
        gate=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("who", sorted(DASHBOARD_SESSIONS))
async def test_a_merge_wakes_the_dashboard_session_once_then_ends_the_watch(
    tmp_path, monkeypatch, who
):
    """Cases 1, 3, 4: the tick that reads the merge fires once, then settles."""
    seen: list[str] = []

    async def on_fire(loop):
        seen.append(terminal_wake_line(loop))
        return True

    monkeypatch.setattr(_an.irq, "poll", _poll("MERGED"))
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = _loop(DASHBOARD_SESSIONS[who])
    service._loops[loop.id] = loop
    try:
        await service._timer(loop, delay=0)
        assert len(seen) == 1, "exactly one final turn reaches the session"
        assert f"PR acme/widgets#37164 merged at {MERGED_AT}, head {HEAD[:12]}." in seen[0]
        assert "last wake" in seen[0]
        assert loop.active is False
        assert loop.monitor is not None
        assert loop.monitor.outcome is MonitorOutcome.SUCCESS
        assert loop.stopped_reason == _an.MONITOR_TERMINAL_REASON
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_busy_session_keeps_the_final_turn_owed(tmp_path, monkeypatch):
    """Case 4: the worker can be mid-turn when the merge is seen; the news must wait."""

    async def on_fire(loop):
        return False

    monkeypatch.setattr(_an.irq, "poll", _poll("MERGED"))
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = _loop(DASHBOARD_SESSIONS["worker"])
    service._loops[loop.id] = loop
    try:
        await service._timer(loop, delay=0)
        assert loop.active is True
        assert loop.monitor is not None
        assert loop.monitor.outcome is None
        assert loop.monitor.terminal_pending == "success"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_close_without_merge_says_so_and_settles_blocked(tmp_path, monkeypatch):
    seen: list[str] = []

    async def on_fire(loop):
        seen.append(terminal_wake_line(loop))
        return True

    monkeypatch.setattr(_an.irq, "poll", _poll("CLOSED"))
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = _loop(DASHBOARD_SESSIONS["conductor"])
    service._loops[loop.id] = loop
    try:
        await service._timer(loop, delay=0)
        assert seen and "closed without merging" in seen[0]
        assert loop.monitor is not None
        assert loop.monitor.outcome is MonitorOutcome.BLOCKED
    finally:
        service.stop()


def test_no_line_when_nothing_is_owed():
    assert terminal_wake_line(_loop("chat-1-123")) == ""


# -- the dashboard fire puts the result first ------------------------------------


def _orchestrator() -> gw.GatewayOrchestrator:
    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    orch.dashboard_state = SimpleNamespace(
        get_slot=MagicMock(return_value=None),
        push_slots_update=MagicMock(),
        _background_tasks=set(),
        run_background_turn=MagicMock(side_effect=lambda _slot, coro: coro),
        _slots={},
        _restricted_keys=set(),
        sessions=None,
    )
    orch.autonudge_svc = MagicMock()
    orch.autonudge_svc.remove = AsyncMock()
    orch.autonudge_svc.monitor_dispatch_is_authorized = AsyncMock(return_value=True)
    orch._session_tasks = {}
    return orch


def _slot(key: str) -> MagicMock:
    slot = MagicMock()
    slot.key = key
    slot.running = False
    slot.is_closing = False
    slot.mode = ""
    slot.memory_mode = "persistent"
    slot._last_turn_structural_terminal = False
    slot._last_turn_structural_terminal_loop_id = ""
    slot._last_turn_structural_terminal_loop_gen = 0
    slot._last_turn_model_refused_loop_id = ""
    return slot


async def _runner_that_accepts(_state, _slot, _text, **kwargs):
    """The runner past its last refusing gate: it marks the turn accepted."""
    kwargs["monitor_completion"].mark_accepted()


async def _runner_refused_in_preparation(_state, _slot, _text, **kwargs):
    """A gate refuses the turn before provider entry, so nothing is accepted."""
    assert kwargs["monitor_completion"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("who", sorted(DASHBOARD_SESSIONS))
async def test_the_final_dashboard_turn_opens_with_the_result(who):
    key = DASHBOARD_SESSIONS[who]
    orch = _orchestrator()
    loop = _loop(key)
    assert loop.monitor is not None
    loop.monitor.terminal_pending = "success"
    loop.monitor.terminal_note = f"PR acme/widgets#37164 merged at {MERGED_AT}, head {HEAD[:12]}."
    slot = _slot(key)
    orch.dashboard_state.get_slot = MagicMock(return_value=slot)

    def _spawn(state, slot, coro, **kwargs):
        return asyncio.ensure_future(coro)

    with (
        patch.object(gw, "spawn_guarded_turn", _spawn),
        patch("kiro_crew.dashboard.chat._run_chat", new=_runner_that_accepts),
        patch.object(orch, "_dashboard_mode_admits", AsyncMock(return_value=True)),
    ):
        assert await orch._fire_dashboard_nudge(loop) is True
    rows = [call for call in slot.append.call_args_list if call.args[0] == "nudge"]
    assert rows, "the final turn was never written"
    assert (
        rows[0]
        .args[1]
        .startswith(
            f"[monitor finished] PR acme/widgets#37164 merged at {MERGED_AT}, head {HEAD[:12]}."
        )
    )


# -- monitor_update on an ended loop is refused in the same call -----------------


@pytest.fixture()
def bound(monkeypatch: pytest.MonkeyPatch) -> None:
    slot = DASHBOARD_SESSIONS["worker"]
    monkeypatch.setattr(mcp_core, "_autonudge_binding_key", lambda sk: slot)
    monkeypatch.setattr(mcp_core, "_structured_monitor_binding_key", lambda sk: slot)
    monkeypatch.setattr(
        mcp_core, "_resolve_session_key_strict", lambda *a, **k: f"dashboard:{slot}"
    )


def _reading_of_an_ended_gated_loop() -> dict[str, Any]:
    """What ``api_session_monitor_get`` returns once the gated loop settled on a merge."""
    return {
        "enabled": True,
        "monitor": None,
        "autonudge_loop": {
            "id": "f96698bf",
            "active": False,
            "gate": True,
            "stopped_reason": _an.MONITOR_TERMINAL_REASON,
        },
    }


def test_monitor_update_on_an_ended_loop_is_refused_at_call_time(bound, monkeypatch):
    """Cases 2 and 5: the retarget onto the next pull request answered "requested"."""
    monkeypatch.setattr(mcp_core, "_get", lambda *a, **k: _reading_of_an_ended_gated_loop())
    out = control.monitor_update(
        "monitor_update", {"message": "watch https://github.com/acme/widgets/pull/37030"}
    )
    assert session_directive.decode(out, "monitor_update") is None, out
    assert "NOT applied" in out
    assert "already ended" in out
    assert "monitor_start" in out


def test_monitor_update_on_an_ended_structured_monitor_is_refused(bound, monkeypatch):
    monkeypatch.setattr(
        mcp_core,
        "_get",
        lambda *a, **k: {
            "enabled": True,
            "active": False,
            "monitor": {"outcome": "success", "target": "acme/widgets#37171", "stopped_reason": ""},
            "autonudge_loop": None,
        },
    )
    out = control.monitor_update("monitor_update", {"target": "acme/widgets#37030"})
    assert session_directive.decode(out, "monitor_update") is None, out
    assert "acme/widgets#37171" in out


def test_monitor_start_still_replaces_the_ended_loop(bound, monkeypatch):
    """The remedy the refusal names has to work."""
    monkeypatch.setattr(mcp_core, "_get", lambda *a, **k: _reading_of_an_ended_gated_loop())
    out = control.monitor_start(
        "monitor_start",
        {"message": "watch https://github.com/acme/widgets/pull/37030", "interval_secs": 600},
    )
    assert session_directive.decode(out, "monitor_start") is not None, out


def test_monitor_update_on_a_live_loop_still_emits(bound, monkeypatch):
    reading = _reading_of_an_ended_gated_loop()
    reading["autonudge_loop"]["active"] = True
    reading["autonudge_loop"]["stopped_reason"] = ""
    monkeypatch.setattr(mcp_core, "_get", lambda *a, **k: reading)
    out = control.monitor_update("monitor_update", {"interval_secs": 900})
    assert session_directive.decode(out, "monitor_update") is not None, out


@pytest.mark.asyncio
async def test_a_final_turn_that_never_starts_is_not_reported_delivered():
    """An unattended slot queued past the background cap never runs the turn.

    Reporting that fire as delivered would settle the watch with the news unsent,
    so the fire waits for the turn to start and answers not-delivered instead.
    """
    key = DASHBOARD_SESSIONS["worker"]
    orch = _orchestrator()
    loop = _loop(key)
    assert loop.monitor is not None
    loop.monitor.terminal_pending = "success"
    slot = _slot(key)
    orch.dashboard_state.get_slot = MagicMock(return_value=slot)

    async def _queue_timed_out(_slot, coro):
        coro.close()
        return None

    orch.dashboard_state.run_background_turn = _queue_timed_out

    def _spawn(state, slot, coro, **kwargs):
        return asyncio.ensure_future(coro)

    with (
        patch.object(gw, "spawn_guarded_turn", _spawn),
        patch("kiro_crew.dashboard.chat._run_chat", new=AsyncMock()),
        patch.object(orch, "_dashboard_mode_admits", AsyncMock(return_value=True)),
    ):
        assert await orch._fire_dashboard_nudge(loop) is False
    assert not [
        c for c in slot.append.call_args_list if c.args[0] == "nudge"
    ], "a turn that never ran writes no row, so a retry cannot duplicate one"


@pytest.mark.asyncio
async def test_a_close_then_merge_while_busy_reports_the_merge(tmp_path, monkeypatch):
    """The owed ending follows the newest reading, not the first one seen."""

    async def on_fire(loop):
        return False

    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = _loop(DASHBOARD_SESSIONS["worker"])
    service._loops[loop.id] = loop
    try:
        monkeypatch.setattr(_an.irq, "poll", _poll("CLOSED"))
        assert await service._monitor_tick_is_quiet(loop) is False
        assert loop.monitor is not None
        assert loop.monitor.terminal_pending == "blocked"
        monkeypatch.setattr(_an.irq, "poll", _poll("MERGED"))
        assert await service._monitor_tick_is_quiet(loop) is False
        assert loop.monitor.terminal_pending == "success"
        assert f"merged at {MERGED_AT}" in terminal_wake_line(loop)
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_final_turn_refused_while_being_prepared_is_not_delivered():
    """A closing session refuses the turn after it left the queue but before the provider."""
    key = DASHBOARD_SESSIONS["bot"]
    orch = _orchestrator()
    loop = _loop(key)
    assert loop.monitor is not None
    loop.monitor.terminal_pending = "success"
    slot = _slot(key)
    orch.dashboard_state.get_slot = MagicMock(return_value=slot)

    def _spawn(state, slot, coro, **kwargs):
        return asyncio.ensure_future(coro)

    with (
        patch.object(gw, "spawn_guarded_turn", _spawn),
        patch("kiro_crew.dashboard.chat._run_chat", new=_runner_refused_in_preparation),
        patch.object(orch, "_dashboard_mode_admits", AsyncMock(return_value=True)),
    ):
        assert await orch._fire_dashboard_nudge(loop) is False
    assert not [c for c in slot.append.call_args_list if c.args[0] == "nudge"]
