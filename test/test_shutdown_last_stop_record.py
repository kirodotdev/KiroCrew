"""What the gateway's graceful shutdown records for ``kirocrew resume``, and that no boot replays it."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_gateway_helpers import _mock_context_builder  # noqa: F401
from slack_gateway_helpers import _restore_update_check_cache  # noqa: F401
from slack_gateway_helpers import _make_orchestrator, _mock_dashboard_state, _mock_sessions


@pytest.mark.asyncio
async def test_shutdown_records_the_chats_it_cuts_before_memory_or_sessions_stop(monkeypatch):
    """A stop records the chats it cuts for ``kirocrew resume`` before anything can end them."""
    from kiro_crew.dashboard import restart_resume

    order: list[str] = []
    monkeypatch.setattr(
        restart_resume, "write_intent", lambda names: order.append(f"boot record {names}")
    )
    orch = _make_orchestrator()
    orch.dashboard_state = _mock_dashboard_state()
    orch.dashboard_state._slots = {
        "cut": SimpleNamespace(_turn_in_flight_generation=2),
        "idle": SimpleNamespace(_turn_in_flight_generation=0),
    }
    orch._memory_startup = MagicMock()
    orch._memory_startup.stop.side_effect = lambda: order.append("memory-stop")
    orch.sessions = _mock_sessions()
    orch.sessions.close_all = AsyncMock(side_effect=lambda *_a, **_k: order.append("close"))
    monkeypatch.setattr(
        restart_resume, "write_last_stop", lambda names: order.append(f"record {names}")
    )

    await orch._shutdown()

    assert order[0] == "record ['cut']", order
    assert {"memory-stop", "close"} <= set(order), order
    assert not [step for step in order if step.startswith("boot record")], order


@pytest.mark.asyncio
async def test_the_boot_after_a_stop_resumes_nothing_on_its_own(tmp_path, monkeypatch):
    """An operator who stops the gateway to end runaway turns must not see them re-run.

    The stop's record is real on disk, and the boot's own resume reads none of it.
    """
    from kiro_crew.dashboard import chat_resume_all, restart_resume

    monkeypatch.setattr(restart_resume, "intent_path", lambda: tmp_path / "boot.json")
    monkeypatch.setattr(restart_resume, "last_stop_path", lambda: tmp_path / "stop.json")
    started = MagicMock(return_value=(202, {"total": 1}))
    monkeypatch.setattr(chat_resume_all, "start_resume_all", started)
    orch = _make_orchestrator()
    orch.dashboard_state = _mock_dashboard_state()
    orch.dashboard_state._slots = {"runaway": SimpleNamespace(_turn_in_flight_generation=3)}

    await orch._shutdown()
    await restart_resume.resume_after_restart(orch.dashboard_state)

    assert restart_resume.read_last_stop() == ["runaway"]
    assert not (tmp_path / "boot.json").exists()
    started.assert_not_called()


@pytest.mark.asyncio
async def test_a_shutdown_with_no_running_chat_clears_the_last_stops_record(tmp_path, monkeypatch):
    """The record names the LAST stop's chats only, so ``kirocrew resume`` never reaches older ones."""
    from kiro_crew.dashboard import restart_resume

    monkeypatch.setattr(restart_resume, "last_stop_path", lambda: tmp_path / "stop.json")
    restart_resume.write_last_stop(["cut-by-an-earlier-stop"])
    orch = _make_orchestrator()
    orch.dashboard_state = _mock_dashboard_state()
    orch.dashboard_state._slots = {"idle": SimpleNamespace(_turn_in_flight_generation=0)}

    await orch._shutdown()

    assert restart_resume.read_last_stop() == []


@pytest.mark.asyncio
async def test_a_stop_waits_for_approval_writes_once_after_the_closes(tmp_path, monkeypatch):
    """Recording the cut chats must not spend the stop's budget on a second approval-write wait."""
    from kiro_crew.dashboard import crew_api_approvals, restart_resume

    monkeypatch.setattr(restart_resume, "last_stop_path", lambda: tmp_path / "stop.json")
    order: list[str] = []
    monkeypatch.setattr(
        crew_api_approvals,
        "finish_writes_before_restart",
        AsyncMock(side_effect=lambda *_a: order.append("restart wait")),
    )
    monkeypatch.setattr(
        crew_api_approvals,
        "finish_writes_before_exit",
        AsyncMock(side_effect=lambda *_a: order.append("stop wait")),
    )
    orch = _make_orchestrator()
    orch.dashboard_state = _mock_dashboard_state()
    orch.dashboard_state._slots = {"cut": SimpleNamespace(_turn_in_flight_generation=1)}
    orch.sessions = _mock_sessions()
    orch.sessions.close_all = AsyncMock(side_effect=lambda *_a, **_k: order.append("close"))

    await orch._shutdown()

    assert order.count("stop wait") == 1 and "restart wait" not in order, order
    assert order.index("close") < order.index("stop wait"), order


@pytest.mark.asyncio
async def test_a_restart_still_waits_for_approval_writes_after_recording(tmp_path, monkeypatch):
    """A restart still waits for approval writes once it has recorded the cut chats."""
    from kiro_crew.dashboard import crew_api_approvals, restart_resume

    monkeypatch.setattr(restart_resume, "intent_path", lambda: tmp_path / "boot.json")
    waited = AsyncMock()
    monkeypatch.setattr(crew_api_approvals, "finish_writes_before_restart", waited)
    state = SimpleNamespace(_slots={"cut": SimpleNamespace(_turn_in_flight_generation=1)})

    await restart_resume.record_running_slots(state)

    waited.assert_awaited_once_with(state)
    assert restart_resume.take_intent() == frozenset({"cut"})
