"""A popped sub-agent completion keeps exactly one owner when its turn fails.

``_run_chat`` receives a popped completion's one-shot ``_on_discarded``
callback. When the turn exits before the model consumed the completion, the
callback moves onto an exact ``lifecycle_recovery`` row (or, on a direct Stop
or slot teardown, runs once). These tests pin two properties of that hand-over:

- a cancel that lands in the turn's teardown awaits still leaves the retry row
  owning the callback, because the exit guard runs the settlement the turn's
  own ``finally`` never reached;
- a user's message already queued behind the failed completion still drains in
  the ordinary hand-off; only a retry that would be the next entry drained is
  held.

A queued completion on a slot whose close hands its key to a recreate is retired
only once the handed-over tail committed: a failed tail leaves the callback
unfired so the manager keeps the delivery for restart recovery.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state
from test_slot_close_recreation_race import (
    NAME,
    _arm_running_turn,
    _reached,
    _Req,
    _slot_with_committed_and_uncommitted_rows,
)

from kiro_crew import autonudge
from kiro_crew.acp.client import AcpError
from kiro_crew.dashboard import chat_handlers, chat_runner
from kiro_crew.dashboard.chat import _run_chat
from kiro_crew.dashboard.chat_utils import LIFECYCLE_RECOVERY_KIND

COMPLETION = "[Subagent completion event] the child finished"


def _state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    return state


def _failing_client() -> AsyncMock:
    """A client whose every prompt fails terminally before any token."""
    client = AsyncMock()
    client.context_usage_pct = MagicMock(return_value=10.0)
    client.set_model = AsyncMock()
    client.served_model = ""
    client.client = None

    async def _stream(_msg):
        raise AcpError("provider rejected the prompt", transient=False)
        yield  # pragma: no cover -- makes _stream an async generator

    client.stream = _stream
    client.stream_command = _stream
    return client


def _completion_rows(slot) -> list[dict]:
    return [item for item in slot._queue if item.get("kind") == LIFECYCLE_RECOVERY_KIND]


@pytest.mark.asyncio
async def test_teardown_cancel_still_transfers_the_completion_to_a_retry_row(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    state.sessions.get_or_create = AsyncMock(return_value=(_failing_client(), True, False))
    drain = AsyncMock(return_value=False)
    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", drain)

    async def _cancelled_teardown(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(chat_runner, "_clear_local_turn_marker", _cancelled_teardown)
    discarded = MagicMock()

    with pytest.raises(asyncio.CancelledError):
        await _run_chat(
            state,
            slot,
            COMPLETION,
            _turn_actor="subagent",
            _on_consumed=MagicMock(),
            _on_discarded=discarded,
        )

    rows = _completion_rows(slot)
    assert [row["content"] for row in rows] == [COMPLETION]
    assert rows[0]["_on_discarded"] is not None
    discarded.assert_not_called()
    # The retry is the only queued entry, so the guard's tail holds it.
    drain.assert_not_awaited()

    # The retry row is now the sole owner: removing it retires the callback once.
    assert slot.queue_remove_by_id(rows[0]["id"]) == COMPLETION
    discarded.assert_called_once_with()


@pytest.mark.asyncio
async def test_queued_user_message_drains_ahead_of_a_held_completion_retry(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    state.sessions.get_or_create = AsyncMock(return_value=(_failing_client(), True, False))
    drain = AsyncMock(return_value=False)
    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", drain)
    slot.queue_append("the user's next message")
    discarded = MagicMock()

    await _run_chat(
        state,
        slot,
        COMPLETION,
        _turn_actor="subagent",
        _on_consumed=MagicMock(),
        _on_discarded=discarded,
    )

    assert [(item["content"], item.get("kind", "")) for item in slot._queue] == [
        ("the user's next message", ""),
        (COMPLETION, LIFECYCLE_RECOVERY_KIND),
    ]
    drain.assert_awaited_once()
    discarded.assert_not_called()


@pytest.mark.asyncio
async def test_a_lone_completion_retry_is_held_not_drained(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    state.sessions.get_or_create = AsyncMock(return_value=(_failing_client(), True, False))
    drain = AsyncMock(return_value=False)
    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", drain)
    discarded = MagicMock()

    await _run_chat(
        state,
        slot,
        COMPLETION,
        _turn_actor="subagent",
        _on_consumed=MagicMock(),
        _on_discarded=discarded,
    )

    assert [row["content"] for row in _completion_rows(slot)] == [COMPLETION]
    assert len(slot._queue) == 1
    drain.assert_not_awaited()
    discarded.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("tail_commits", [True, False])
async def test_handover_close_retires_a_queued_completion_only_after_its_tail_commits(
    tmp_path, monkeypatch, tail_commits
):
    monkeypatch.setattr(autonudge, "_INSTANCE", None)
    state = _make_state(tmp_path)
    original = await _slot_with_committed_and_uncommitted_rows(state)
    discarded = MagicMock()
    queued_id = original.queue_insert(
        0, COMPLETION, kind=LIFECYCLE_RECOVERY_KIND, on_discarded=discarded
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)
    if not tail_commits:

        async def _wedged(*_args, **_kwargs):
            raise OSError("disk wedged")

        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", _wedged)

    close = asyncio.create_task(chat_handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    state.get_or_create_slot(NAME)
    release.set()
    resp = await close

    if tail_commits:
        assert resp.status == 200
        discarded.assert_called_once_with()
    else:
        assert resp.status == 500
        discarded.assert_not_called()
        assert [item["id"] for item in original._queue] == [queued_id]
        assert original._queue[0]["_on_discarded"] is discarded


@pytest.mark.asyncio
async def test_an_enriched_completion_retry_stays_a_completion_across_a_second_failure(
    tmp_path, monkeypatch
):
    """A drained failure notice enriches ``turn.message``; the retry keeps its identity.

    The retry row queues the raw announce, so its replay mark must follow the
    raw bytes. Without it the second failure requeues the result as an ordinary
    recovery, whose discard would tombstone a completion the model never read.
    """
    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    state.sessions.get_or_create = AsyncMock(return_value=(_failing_client(), True, False))
    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", AsyncMock(return_value=False))
    # The context-bearing arm is the one that drains pending failure notices.
    state.context_builder = MagicMock()
    state.context_builder.build_message.side_effect = lambda message, *_a, **_k: (message, None)
    slot._pending_subagent_failures = ["[Subagent delivery failed] another child timed out"]
    discarded = MagicMock()

    await _run_chat(
        state,
        slot,
        COMPLETION,
        _turn_actor="subagent",
        _on_consumed=MagicMock(),
        _on_discarded=discarded,
    )

    rows = _completion_rows(slot)
    assert [row["content"] for row in rows] == [COMPLETION]
    assert rows[0].get("_replays_completion") is True

    # Drain it the way the queue hand-off does and fail again.
    retry = slot._queue.pop(0)
    await _run_chat(
        state,
        slot,
        retry["content"],
        _turn_actor="subagent",
        _synthetic_recovery_turn=True,
        _replays_completion=True,
        _on_consumed=retry["_on_consumed"],
        _on_discarded=retry["_on_discarded"],
    )

    rows = _completion_rows(slot)
    assert [row["content"] for row in rows] == [COMPLETION]
    assert rows[0].get("_replays_completion") is True
    discarded.assert_not_called()
