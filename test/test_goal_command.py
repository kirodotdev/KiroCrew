"""
Covers ``_handle_goal_command`` in isolation — the pure glue over the async
``AutoNudgeService``: status/empty, arm (default + ``--max`` parse/clamp), clear,
and the AutoNudge-disabled path. The judge gate at ``HOOK_EVENT_STOP`` is a
follow-up CR and is not exercised here.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import kiro_crew.dashboard.chat_runner as chat_runner


def _make_slot(key: str = "slot-1", agent: str = "kirocrew") -> MagicMock:
    slot = MagicMock()
    slot.key = key
    slot.agent = agent
    slot.append = MagicMock()
    return slot


def _make_state() -> MagicMock:
    state = MagicMock()
    state.push_slots_update = MagicMock()
    return state


def _fake_service(loop: object | None = None) -> MagicMock:
    """A stand-in AutoNudgeService: sync ``get_by_slot`` + async ``add``/``remove``."""
    svc = MagicMock()
    svc.get_by_slot = MagicMock(return_value=loop)
    svc.add = AsyncMock(return_value=SimpleNamespace(id="loop-abc"))
    svc.remove = AsyncMock(return_value=None)
    return svc


def _install(monkeypatch: pytest.MonkeyPatch, svc: MagicMock | None) -> MagicMock:
    # Helper uses the module-level get_instance imported into chat_runner, so
    # patch chat_runner.get_instance (patching autonudge.get_instance would not
    # intercept the already-bound name).
    monkeypatch.setattr(chat_runner, "get_instance", lambda: svc)
    # Avoid real SEL side effects; return the mock so callers can inspect it.
    audit = MagicMock()
    monkeypatch.setattr(chat_runner, "sel", lambda: audit)
    return audit


def _last_assistant_body(slot: MagicMock) -> str:
    for call in reversed(slot.append.call_args_list):
        if call.args and call.args[0] == "assistant":
            return call.args[1]
    raise AssertionError("no assistant message was appended")


@pytest.mark.asyncio
async def test_status_no_active_goal(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _fake_service(loop=None)
    _install(monkeypatch, svc)
    slot, state = _make_slot(), _make_state()

    await chat_runner._handle_goal_command(state, slot, "/goal")

    body = _last_assistant_body(slot)
    assert "No active goal" in body
    svc.add.assert_not_awaited()
    svc.remove.assert_not_awaited()
    # Always finalizes the turn.
    state.push_slots_update.assert_called_once()
    assert any(c.args and c.args[0] == "done" for c in slot.append.call_args_list)


@pytest.mark.asyncio
async def test_status_with_active_goal_shows_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = SimpleNamespace(id="loop-1", max_cycles=15)
    svc = _fake_service(loop=loop)
    _install(monkeypatch, svc)
    slot, state = _make_slot(), _make_state()

    await chat_runner._handle_goal_command(state, slot, "/goal status")

    body = _last_assistant_body(slot)
    assert "Active goal" in body and "15" in body
    svc.add.assert_not_awaited()


@pytest.mark.asyncio
async def test_arm_default_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    svc = _fake_service(loop=None)
    audit = _install(monkeypatch, svc)
    monkeypatch.setattr(chat_runner.Path, "home", classmethod(lambda cls: tmp_path))
    # The goal-stop sentinel now derives from data_home() (data home moved to
    # ~/.kiro/crew). data_home() reads KIROCREW_HOME (pinned elsewhere by
    # conftest), so redirect it to track the patched home and keep the
    # ~/.kirocrew/goal-stop layout this test builds authoritative.
    monkeypatch.setattr(chat_runner, "data_home", lambda: tmp_path / ".kirocrew")
    slot, state = _make_slot(key="a/b:c"), _make_state()
    stale_sentinel = tmp_path / ".kirocrew" / "goal-stop" / "a_b_c.stop"
    stale_sentinel.parent.mkdir(parents=True)
    stale_sentinel.write_text("stop", encoding="utf-8")

    await chat_runner._handle_goal_command(state, slot, "/goal ship the feature")

    svc.add.assert_awaited_once()
    call = svc.add.await_args
    assert call.args[0] == "a/b:c"  # slot key passed positionally
    assert call.kwargs["max_cycles"] == 50
    assert call.kwargs["idle_secs"] == 15
    # Objective is embedded in the nudge, along with the safety rules.
    assert "ship the feature" in call.kwargs["message"]
    assert "never git push" in call.kwargs["message"]
    # V1-4 lean nudge: compressed, but must still carry every load-bearing
    # directive (STOP CHECK sentinel, evidence-based DONE CHECK, blocker path,
    # one-atomic-step rule) so the shorter form can't silently drop a control.
    _msg = call.kwargs["message"]
    assert 'autonudge_stop(reason="sentinel")' in _msg
    assert 'autonudge_stop(reason="goal met")' in _msg
    assert 'autonudge_stop(reason="blocked")' in _msg
    assert "atomic step" in _msg
    assert "concrete evidence" in _msg
    # Sentinel path is per-slot, slug-sanitized, and cleared before re-arming.
    sentinel = call.kwargs["stop_sentinel_path"]
    assert sentinel == str(stale_sentinel)
    assert not stale_sentinel.exists()
    body = _last_assistant_body(slot)
    assert "Goal set" in body and "50-turn budget" in body
    audit.log_tool_invocation.assert_called_once()
    assert audit.log_tool_invocation.call_args.kwargs["session_key"] == "a/b:c"


@pytest.mark.asyncio
async def test_arm_with_max_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _fake_service(loop=None)
    _install(monkeypatch, svc)
    slot, state = _make_slot(), _make_state()

    await chat_runner._handle_goal_command(state, slot, "/goal --max 5 do the thing")

    call = svc.add.await_args
    assert call.kwargs["max_cycles"] == 5
    assert "do the thing" in call.kwargs["message"]
    assert "do the thing" in _last_assistant_body(slot)


@pytest.mark.asyncio
async def test_arm_max_flag_is_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _fake_service(loop=None)
    _install(monkeypatch, svc)
    slot, state = _make_slot(), _make_state()

    await chat_runner._handle_goal_command(state, slot, "/goal --max 999 big goal")

    assert svc.add.await_args.kwargs["max_cycles"] == 50  # clamped to the ceiling


@pytest.mark.asyncio
async def test_arm_bare_max_flag_shows_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _fake_service(loop=None)
    _install(monkeypatch, svc)
    slot, state = _make_slot(), _make_state()

    await chat_runner._handle_goal_command(state, slot, "/goal --max 5")

    assert "Usage:" in _last_assistant_body(slot)
    svc.add.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_command_shows_status(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _fake_service(loop=None)
    _install(monkeypatch, svc)
    slot, state = _make_slot(), _make_state()

    await chat_runner._handle_goal_command(state, slot, "/goal   ")

    body = _last_assistant_body(slot)
    assert "No active goal" in body
    svc.add.assert_not_awaited()


@pytest.mark.asyncio
async def test_clear_active_goal(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = SimpleNamespace(id="loop-xyz", max_cycles=15)
    svc = _fake_service(loop=loop)
    _install(monkeypatch, svc)
    slot, state = _make_slot(), _make_state()

    await chat_runner._handle_goal_command(state, slot, "/goal clear")

    svc.remove.assert_awaited_once_with("loop-xyz")
    assert "cleared" in _last_assistant_body(slot).lower()


@pytest.mark.asyncio
async def test_clear_when_no_goal(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _fake_service(loop=None)
    _install(monkeypatch, svc)
    slot, state = _make_slot(), _make_state()

    await chat_runner._handle_goal_command(state, slot, "/goal clear")

    svc.remove.assert_not_awaited()
    assert "No active goal to clear" in _last_assistant_body(slot)


@pytest.mark.asyncio
async def test_autonudge_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, None)  # get_instance() -> None
    slot, state = _make_slot(), _make_state()

    await chat_runner._handle_goal_command(state, slot, "/goal do a thing")

    body = _last_assistant_body(slot)
    assert "unavailable" in body.lower()
    # Turn is still finalized even on the disabled path.
    state.push_slots_update.assert_called_once()


# ── A scheduled composer message owns the slot ──────────────────────────────
#
# The service protects scheduled messages: generic ``add``/``remove`` raise
# ``MonitorUpdateConflict`` instead of replacing or deleting one. Nothing above
# ``_run_chat`` writes a ``done`` row for an escaped exception, so the handler
# must answer at its own boundary or the turn spinner never stops.


def _scheduled_record(loop_id: str = "sched-1") -> SimpleNamespace:
    """Shape ``autonudge.is_scheduled_message`` positively classifies."""
    return SimpleNamespace(
        id=loop_id, max_cycles=1, scheduled_message=True, scheduled_at=1.0e12
    )


def _assert_terminal(slot: MagicMock, state: MagicMock) -> str:
    """The turn ended: one assistant row, one slots push, one ``done`` row last."""
    kinds = [c.args[0] for c in slot.append.call_args_list if c.args]
    assert kinds.count("assistant") == 1
    assert kinds[-1] == "done"
    state.push_slots_update.assert_called_once()
    return _last_assistant_body(slot)


def _assert_directs_to_original_chat_banner(body: str) -> None:
    """Scheduled messages are managed only in their original chat.

    The Schedule page is read-only guidance, so the refusal must send the user
    to the banner above the composer in that chat and never claim the Schedule
    tab can unschedule it.
    """
    lowered = body.lower()
    assert "unschedule" in lowered
    assert "banner above the composer" in lowered
    assert "chat where it was scheduled" in lowered
    assert "schedule tab" not in lowered
    assert "schedule page" not in lowered


@pytest.mark.asyncio
async def test_clear_short_circuits_when_scheduled_message_owns_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = _fake_service(loop=_scheduled_record())
    svc.remove = AsyncMock(side_effect=AssertionError("remove must not be reached"))
    audit = _install(monkeypatch, svc)
    slot, state = _make_slot(), _make_state()

    await chat_runner._handle_goal_command(state, slot, "/goal clear")

    body = _assert_terminal(slot, state)
    assert "scheduled message" in body.lower()
    _assert_directs_to_original_chat_banner(body)
    assert "cleared" not in body.lower()
    svc.remove.assert_not_awaited()
    assert audit.log_tool_invocation.call_args.kwargs["outcome"] == "conflict"


@pytest.mark.asyncio
async def test_arm_short_circuits_when_scheduled_message_owns_slot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    svc = _fake_service(loop=_scheduled_record())
    svc.add = AsyncMock(side_effect=AssertionError("add must not be reached"))
    audit = _install(monkeypatch, svc)
    monkeypatch.setattr(chat_runner, "data_home", lambda: tmp_path)
    slot, state = _make_slot(), _make_state()

    await chat_runner._handle_goal_command(state, slot, "/goal --max 5 ship the feature")

    body = _assert_terminal(slot, state)
    assert "scheduled message" in body.lower()
    _assert_directs_to_original_chat_banner(body)
    assert "Goal set" not in body
    svc.add.assert_not_awaited()
    assert audit.log_tool_invocation.call_args.kwargs["outcome"] == "conflict"
    # The stop sentinel is a side effect of ARMING; a refused arm leaves no trace.
    assert not (tmp_path / "goal-stop").exists()


@pytest.mark.asyncio
async def test_clear_survives_conflict_raised_by_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Race: the read saw an ordinary goal, but a schedule landed before remove."""
    from kiro_crew.autonudge import MonitorUpdateConflict

    svc = _fake_service(loop=SimpleNamespace(id="loop-xyz", max_cycles=15))
    svc.remove = AsyncMock(
        side_effect=MonitorUpdateConflict(
            "protected scheduled messages require an authenticated dashboard user"
        )
    )
    audit = _install(monkeypatch, svc)
    slot, state = _make_slot(), _make_state()

    await chat_runner._handle_goal_command(state, slot, "/goal clear")  # must not raise

    body = _assert_terminal(slot, state)
    assert "scheduled message" in body.lower()
    assert "cleared" not in body.lower()
    svc.remove.assert_awaited_once_with("loop-xyz")
    assert audit.log_tool_invocation.call_args.kwargs["outcome"] == "conflict"


@pytest.mark.asyncio
async def test_arm_survives_conflict_raised_by_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Race: the read saw an empty slot, but a schedule landed before add."""
    from kiro_crew.autonudge import MonitorUpdateConflict

    svc = _fake_service(loop=None)
    svc.add = AsyncMock(
        side_effect=MonitorUpdateConflict(
            "scheduled message must be unscheduled by an authenticated dashboard user"
        )
    )
    audit = _install(monkeypatch, svc)
    monkeypatch.setattr(chat_runner, "data_home", lambda: tmp_path)
    slot, state = _make_slot(), _make_state()

    await chat_runner._handle_goal_command(state, slot, "/goal ship the feature")  # must not raise

    body = _assert_terminal(slot, state)
    assert "scheduled message" in body.lower()
    assert "Goal set" not in body
    svc.add.assert_awaited_once()
    assert audit.log_tool_invocation.call_args.kwargs["outcome"] == "conflict"


@pytest.mark.asyncio
async def test_status_is_read_only_when_scheduled_message_owns_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = _fake_service(loop=_scheduled_record())
    audit = _install(monkeypatch, svc)
    slot, state = _make_slot(), _make_state()

    await chat_runner._handle_goal_command(state, slot, "/goal status")

    body = _assert_terminal(slot, state)
    assert "scheduled message" in body.lower()
    # Status never advertises a `/goal clear` that the service would refuse.
    assert "Active goal" not in body
    svc.add.assert_not_awaited()
    svc.remove.assert_not_awaited()
    assert audit.log_tool_invocation.call_args.kwargs["outcome"] == "ok"


@pytest.mark.asyncio
async def test_ordinary_goal_paths_unchanged_by_scheduled_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Opposite failure mode: an ordinary goal record must not be mistaken for a schedule."""
    ordinary = SimpleNamespace(id="loop-ord", max_cycles=15, scheduled_message=False, scheduled_at=0.0)
    svc = _fake_service(loop=ordinary)
    _install(monkeypatch, svc)
    monkeypatch.setattr(chat_runner, "data_home", lambda: tmp_path)
    slot, state = _make_slot(), _make_state()

    await chat_runner._handle_goal_command(state, slot, "/goal status")
    assert "Active goal" in _last_assistant_body(slot)

    slot, state = _make_slot(), _make_state()
    await chat_runner._handle_goal_command(state, slot, "/goal clear")
    svc.remove.assert_awaited_once_with("loop-ord")
    assert "cleared" in _last_assistant_body(slot).lower()

    svc.get_by_slot.return_value = None
    slot, state = _make_slot(), _make_state()
    await chat_runner._handle_goal_command(state, slot, "/goal ship it")
    svc.add.assert_awaited_once()
    assert "Goal set" in _last_assistant_body(slot)


@pytest.mark.parametrize(
    ("message", "mutates"),
    [
        ("ordinary text", False),
        ("/goal", False),
        ("/goal status", False),
        ("/goal --max 5", False),
        ("/goal clear", True),
        ("/goal ship the feature", True),
        ("/goal --max 5 ship the feature", True),
    ],
)
def test_goal_mutation_classifier_matches_live_command_semantics(
    message: str, mutates: bool
) -> None:
    from kiro_crew.goal_command import goal_command_mutates_automation

    assert goal_command_mutates_automation(message) is mutates
