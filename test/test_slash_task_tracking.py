"""Regression: slash-command dispatch tasks must be retained, not fire-and-forgotten.

``_handle_slash`` scheduled the slash handler (and every user-facing ``_respond``) with
``asyncio.create_task(...)`` and discarded the task. The event loop keeps only a WEAK
reference, so the task can be garbage-collected mid-execution — silently dropping the
user's slash command or its reply. The fix routes them through ``_spawn_tracked``, which
retains a strong reference in ``_bg_tasks`` until completion.

These tests assert the structural contract (the task is tracked while pending), which is
deterministic, rather than trying to trigger the GC race.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

import kiro_crew.slack.events as events
from kiro_crew.slack.events import _handle_slash, register_slash_command


def _make_orch() -> MagicMock:
    orch = MagicMock()
    orch.slack = MagicMock()  # truthy
    orch._owner_id = "U_OWNER"  # truthy
    orch.slack_command = "kirocrew"  # _handle_slash compares cmd to f"/{slack_command}"
    return orch


@pytest.mark.asyncio
async def test_slash_handler_task_is_retained(tmp_path, monkeypatch) -> None:
    # Allow the caller through the authorization gate.
    monkeypatch.setattr(events, "is_allowed_user", lambda _cid: True)
    monkeypatch.setattr(events, "sel", lambda: MagicMock())

    gate = asyncio.Event()  # keep the handler pending so the task stays in _bg_tasks
    ran = asyncio.Event()

    async def _slow_handler(orch, caller_id, args, respond):  # noqa: ANN001
        ran.set()
        await gate.wait()

    register_slash_command("zzztest", _slow_handler, "test handler")
    try:
        events._bg_tasks.clear()
        orch = _make_orch()
        await _handle_slash(orch, {"command": "/kirocrew", "user_id": "U1", "text": "zzztest do a thing", "response_url": "https://example.test/r"})

        # The dispatched handler task must be retained while pending.
        assert len(events._bg_tasks) >= 1, (
            "slash handler task was not retained — it can be GC'd mid-execution and the "
            "command silently does nothing"
        )
        # Let the handler start, then release it and confirm it actually ran + cleaned up.
        await asyncio.sleep(0)
        assert ran.is_set()
        gate.set()
        await asyncio.gather(*list(events._bg_tasks), return_exceptions=True)
        await asyncio.sleep(0)
        assert len(events._bg_tasks) == 0  # discarded on completion
    finally:
        events.SLASH_REGISTRY.pop("zzztest", None)
        events._bg_tasks.clear()


@pytest.mark.asyncio
async def test_unauthorized_reply_task_is_retained(tmp_path, monkeypatch) -> None:
    # Denied caller → the "not authorized" reply is also a tracked task.
    monkeypatch.setattr(events, "is_allowed_user", lambda _cid: False)
    monkeypatch.setattr(events, "sel", lambda: MagicMock())

    events._bg_tasks.clear()
    orch = _make_orch()
    await _handle_slash(orch, {"command": "/kirocrew", "user_id": "U_BAD", "text": "status", "response_url": "https://example.test/r"})
    # The reply task must be retained (else the user gets no error message).
    assert len(events._bg_tasks) >= 1
    await asyncio.gather(*list(events._bg_tasks), return_exceptions=True)
    await asyncio.sleep(0)
    events._bg_tasks.clear()


@pytest.mark.asyncio
async def test_owner_not_configured_reply_retained(tmp_path, monkeypatch) -> None:
    # Allowed caller but no owner/slack configured → "owner not configured" reply path.
    monkeypatch.setattr(events, "is_allowed_user", lambda _cid: True)
    monkeypatch.setattr(events, "sel", lambda: MagicMock())
    orch = _make_orch()
    orch._owner_id = ""  # falsy → triggers the owner-not-configured branch
    events._bg_tasks.clear()
    await _handle_slash(orch, {"command": "/kirocrew", "user_id": "U1", "text": "status", "response_url": "https://example.test/r"})
    assert len(events._bg_tasks) >= 1
    await asyncio.gather(*list(events._bg_tasks), return_exceptions=True)
    await asyncio.sleep(0)
    events._bg_tasks.clear()


@pytest.mark.asyncio
async def test_unknown_subcommand_help_reply_retained(tmp_path, monkeypatch) -> None:
    # Unknown sub-command → help-text reply is dispatched as a tracked task.
    monkeypatch.setattr(events, "is_allowed_user", lambda _cid: True)
    monkeypatch.setattr(events, "sel", lambda: MagicMock())
    monkeypatch.setattr(events, "_build_help_text", lambda _cmd: "help")
    orch = _make_orch()
    events._bg_tasks.clear()
    await _handle_slash(orch, {"command": "/kirocrew", "user_id": "U1", "text": "no_such_subcmd_xyz", "response_url": "https://example.test/r"})
    assert len(events._bg_tasks) >= 1
    await asyncio.gather(*list(events._bg_tasks), return_exceptions=True)
    await asyncio.sleep(0)
    events._bg_tasks.clear()


@pytest.mark.asyncio
async def test_user_mention_disabled_reply_retained(tmp_path, monkeypatch) -> None:
    # "<@user>" mention → multi-user-disabled reply is dispatched as a tracked task.
    monkeypatch.setattr(events, "is_allowed_user", lambda _cid: True)
    monkeypatch.setattr(events, "sel", lambda: MagicMock())
    orch = _make_orch()
    events._bg_tasks.clear()
    await _handle_slash(
        orch,
        {"command": "/kirocrew", "user_id": "U1", "text": "<@U99999>", "response_url": "https://example.test/r"},
    )
    assert len(events._bg_tasks) >= 1
    await asyncio.gather(*list(events._bg_tasks), return_exceptions=True)
    await asyncio.sleep(0)
    events._bg_tasks.clear()


@pytest.mark.asyncio
async def test_channel_mention_track_tasks_retained(tmp_path, monkeypatch) -> None:
    # "#<channel>" mention → prompt_track_channel + ack reply, both tracked tasks.
    monkeypatch.setattr(events, "is_allowed_user", lambda _cid: True)
    monkeypatch.setattr(events, "sel", lambda: MagicMock())

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(events, "prompt_track_channel", _noop)
    orch = _make_orch()
    events._bg_tasks.clear()
    await _handle_slash(
        orch,
        {"command": "/kirocrew", "user_id": "U1", "text": "<#C12345|general>", "response_url": "https://example.test/r"},
    )
    assert len(events._bg_tasks) >= 1  # track + ack reply
    await asyncio.gather(*list(events._bg_tasks), return_exceptions=True)
    await asyncio.sleep(0)
    events._bg_tasks.clear()


@pytest.mark.asyncio
async def test_raising_handler_task_still_self_cleans(tmp_path, monkeypatch) -> None:
    # A slash handler that raises must NOT leak in _bg_tasks — the done-callback
    # discards regardless of outcome and consumes the exception (so it isn't surfaced
    # as an unretrieved-task-exception warning). Covers the done-callback contract.
    monkeypatch.setattr(events, "is_allowed_user", lambda _cid: True)
    monkeypatch.setattr(events, "sel", lambda: MagicMock())

    async def _boom(orch, caller_id, args, respond):  # noqa: ANN001
        raise RuntimeError("handler exploded")

    events.register_slash_command("zzzboom", _boom, "raising handler")
    try:
        events._bg_tasks.clear()
        orch = _make_orch()
        await _handle_slash(
            orch,
            {"command": "/kirocrew", "user_id": "U1", "text": "zzzboom", "response_url": "https://example.test/r"},
        )
        assert len(events._bg_tasks) >= 1  # retained while pending
        await asyncio.gather(*list(events._bg_tasks), return_exceptions=True)
        await asyncio.sleep(0)
        assert len(events._bg_tasks) == 0  # self-cleaned despite the failure
    finally:
        events.SLASH_REGISTRY.pop("zzzboom", None)
        events._bg_tasks.clear()
