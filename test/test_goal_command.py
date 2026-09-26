"""
Covers ``_handle_goal_command`` in isolation — the pure glue over the async
``AutoNudgeService``: status/empty, arm (default + ``--max`` parse/clamp), clear,
and the AutoNudge-disabled path. Automatic and manual goal lifecycle behavior
is covered by test_automatic_goal.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import kiro_crew.dashboard.chat_runner as chat_runner


def _make_slot(key: str = "slot-1", agent: str = "kirocrew") -> MagicMock:
    slot = MagicMock()
    slot.key = key
    slot.agent = agent
    slot.linked_session_key = ""
    slot.append = MagicMock()
    return slot


def _make_state() -> MagicMock:
    state = MagicMock()
    state._slots = {}
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
    loop = SimpleNamespace(
        id="loop-1",
        slot_key="slot-1",
        max_cycles=15,
        goal=SimpleNamespace(objective="Ship the fix", status="working"),
    )
    svc = _fake_service(loop=loop)
    _install(monkeypatch, svc)
    slot, state = _make_slot(), _make_state()

    await chat_runner._handle_goal_command(state, slot, "/goal status")

    body = _last_assistant_body(slot)
    assert "Ship the fix" in body and "15" in body
    svc.add.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,objective,budget",
    [
        ("/goal ship the feature", "ship the feature", 50),
        ("/goal --max 5 do the thing", "do the thing", 5),
        ("/goal --max 999 big goal", "big goal", 50),
    ],
)
async def test_arm_uses_shared_goal_engine(monkeypatch, command, objective, budget):
    from kiro_crew.dashboard import session_directive_apply

    svc = _fake_service(loop=None)
    audit = _install(monkeypatch, svc)
    apply = AsyncMock(return_value={})
    monkeypatch.setattr(session_directive_apply, "apply_goal", apply)
    monkeypatch.setattr(chat_runner, "effective_session_key", lambda slot: "dashboard:chat-1-123")
    slot, state = _make_slot(), _make_state()

    await chat_runner._handle_goal_command(state, slot, command)

    apply.assert_awaited_once_with(
        state,
        "dashboard:chat-1-123",
        {"action": "start", "objective": objective},
        human_request=True,
        turn_is_current=None,
        max_cycles=budget,
    )
    assert objective in _last_assistant_body(slot)
    audit.log_tool_invocation.assert_called_once()


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
    loop = SimpleNamespace(
        id="loop-xyz", slot_key="slot-1", max_cycles=15, goal=SimpleNamespace(objective="Ship")
    )
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


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/goal status", "/goal clear", "/goal another request"])
async def test_ambiguous_channel_loops_are_not_selected(monkeypatch, command):
    slot = _make_slot("slack_1.1")
    slot.linked_session_key = "slack:1.1"
    state = _make_state()
    state._slots[slot.key] = slot
    legacy = SimpleNamespace(id="legacy", slot_key=slot.key, max_cycles=5)
    canonical = SimpleNamespace(id="canonical", slot_key=slot.linked_session_key, max_cycles=5)
    svc = _fake_service()
    svc.get_by_slot.side_effect = lambda key: {
        slot.key: legacy,
        slot.linked_session_key: canonical,
    }.get(key)
    _install(monkeypatch, svc)

    await chat_runner._handle_goal_command(state, slot, command)

    assert "multiple automation records" in _last_assistant_body(slot)
    svc.add.assert_not_awaited()
    svc.remove.assert_not_awaited()
