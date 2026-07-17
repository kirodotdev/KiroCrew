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
