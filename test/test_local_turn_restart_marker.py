"""The durable "turn in flight" marker that survives a gateway force exit.

A turn cut by process death leaves ``user -> assistant(partial) -> tool rows``
and no error row, which the transcript heuristic (``is_turn_interrupted``)
reads as a finished answer. ``_run_chat`` writes the turn's generation to the
slot-owned ``turn_in_flight_generation`` metadata key before provider dispatch
and omits it at teardown; every restore path converts a leftover value into the
existing interruption row.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_persistence import (
    _rehydrate_slot_from_history,
    _save_slot_to_history,
    restore_recent_sessions,
)
from kiro_crew.dashboard.chat_runner import (
    _begin_local_turn_marker,
    _clear_local_turn_marker,
    _local_turn_generation_for,
    _retire_local_turn_marker,
)
from kiro_crew.history import SLOT_OWNED_META_KEYS

_RESTART_KIND = "gateway_restart_interruption"


def _restart_rows(messages: list[dict]) -> list[dict]:
    return [m for m in messages if (m.get("meta") or {}).get("kind") == _RESTART_KIND]


def _provider_mock() -> AsyncMock:
    # Same shape as test_dashboard_chat._provider_mock: the telemetry accessors
    # the runner reads after a turn are SYNCHRONOUS on the real provider, so they
    # are pinned as MagicMock or each call hands back a never-awaited coroutine.
    client = AsyncMock()
    client.context_usage_pct = MagicMock(return_value=10.0)
    client.context_window_tokens = MagicMock(return_value=0)
    client.context_used_tokens = MagicMock(return_value=0)
    client.mcp_session_report = MagicMock(return_value=None)
    client.available_models = MagicMock(return_value=[])
    client.client.pop_pending_oauth_requests = MagicMock(return_value=[])
    return client


def _client_streaming(events: list) -> AsyncMock:
    client = _provider_mock()

    async def _stream(msg):
        for ev in events:
            yield ev

    client.stream = _stream
    client.stream_command = _stream
    return client


def _state_for_run_chat(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.context_builder = None
    state.consolidator = MagicMock()
    state._hook_store = None
    state._yolo = False
    return state


# ── marker lifecycle ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_marker_is_durable_before_dispatch_and_omitted_after_clear(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "do something long", "msg msg-u")
    slot._turn_generation = 7

    generation = _local_turn_generation_for(slot)
    await _begin_local_turn_marker(state, slot, generation)

    assert generation == 7
    assert slot._turn_in_flight_generation == 7
    assert state.conversation_log is not None
    assert state.conversation_log.get_metadata("dashboard:chat-1")["turn_in_flight_generation"] == 7

    slot.append("assistant", "done", "msg msg-a")
    await _clear_local_turn_marker(state, slot, generation)

    assert slot._turn_in_flight_generation == 0
    assert "turn_in_flight_generation" not in state.conversation_log.get_metadata(
        "dashboard:chat-1"
    )


def test_marker_key_is_slot_owned():
    # Omission on the teardown save IS the durable clear. An unowned key would be
    # carried forward by carry_unowned_metadata and flag every later restart.
    assert "turn_in_flight_generation" in SLOT_OWNED_META_KEYS


def test_retire_is_generation_checked_so_a_successor_keeps_its_marker():
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("chat-1")
    slot._turn_in_flight_generation = 9
    # A stale predecessor's clear must not erase the generation a successor wrote.
    assert _retire_local_turn_marker(slot, 8) is False
    assert slot._turn_in_flight_generation == 9
    assert _retire_local_turn_marker(slot, 0) is False
    assert _retire_local_turn_marker(slot, 9) is True
    assert slot._turn_in_flight_generation == 0
    assert _retire_local_turn_marker(slot, 9) is False


@pytest.mark.asyncio
async def test_admission_writes_only_metadata_when_the_transcript_exists(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "first", "msg msg-u")
    slot.append("assistant", "answered", "msg msg-a")
    assert _save_slot_to_history(state, slot, force=True)
    slot.append("user", "second", "msg msg-u")
    assert state.conversation_log is not None
    before = state.conversation_log.read_messages("dashboard:chat-1")

    await _begin_local_turn_marker(state, slot, 4)

    # The unflushed user row stays with the periodic flush: admitting a turn
    # must not change what the prompt builder reads off disk.
    assert state.conversation_log.read_messages("dashboard:chat-1") == before
    assert state.conversation_log.get_metadata("dashboard:chat-1")["turn_in_flight_generation"] == 4


@pytest.mark.asyncio
async def test_admission_creates_the_transcript_for_a_newborn_slot(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "first ever", "msg msg-u")
    assert state.conversation_log is not None
    assert state.conversation_log.get_metadata("dashboard:chat-1") == {}

    await _begin_local_turn_marker(state, slot, 1)

    # No line to merge into, so the full save is the one writer that can make
    # the marker durable; it carries the user row along.
    assert state.conversation_log.get_metadata("dashboard:chat-1")["turn_in_flight_generation"] == 1
    assert [m["role"] for m in state.conversation_log.read_messages("dashboard:chat-1")] == ["user"]


def _replace_slot_under_same_key(state, old_slot):
    """Mimic a tab close + same-key reopen that resumes the same transcript."""
    from kiro_crew.dashboard.state import _ChatSlot

    replacement = _ChatSlot(old_slot.key)
    replacement._tab_id = old_slot._tab_id
    state._slots[old_slot.key] = replacement
    return replacement


@pytest.mark.asyncio
async def test_admission_merge_refuses_once_a_replacement_owns_the_key(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "first", "msg msg-u")
    slot.append("assistant", "answered", "msg msg-a")
    assert _save_slot_to_history(state, slot, force=True)
    replacement = _replace_slot_under_same_key(state, slot)
    replacement.append("user", "the replacement's turn", "msg msg-u")
    assert _save_slot_to_history(state, replacement, force=True)
    assert state.conversation_log is not None
    before = state.conversation_log.read_messages("dashboard:chat-1")

    # The cancelled predecessor's admission lands after the replacement took the key.
    await _begin_local_turn_marker(state, slot, 3)

    meta = state.conversation_log.get_metadata("dashboard:chat-1")
    assert "turn_in_flight_generation" not in meta
    assert state.conversation_log.read_messages("dashboard:chat-1") == before
    # In memory the stale object still carries its value; nothing reads it again.
    assert slot._turn_in_flight_generation == 3


@pytest.mark.asyncio
async def test_newborn_forced_save_refuses_once_a_replacement_owns_the_key(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "predecessor's first row", "msg msg-u")
    replacement = _replace_slot_under_same_key(state, slot)
    replacement.append("user", "the replacement's turn", "msg msg-u")
    assert _save_slot_to_history(state, replacement, force=True)
    assert state.conversation_log is not None

    # ``require_existing`` is satisfied by the replacement's file, so the merge
    # path is taken and the guard refuses; the newborn fallback must not then
    # rebuild the window from the stale object's rows either.
    await _begin_local_turn_marker(state, slot, 2)

    rows = state.conversation_log.read_messages("dashboard:chat-1")
    assert [m["content"] for m in rows] == ["the replacement's turn"]
    assert "turn_in_flight_generation" not in state.conversation_log.get_metadata(
        "dashboard:chat-1"
    )


@pytest.mark.asyncio
async def test_teardown_clear_refuses_to_overwrite_a_replacement_slot(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "first", "msg msg-u")
    await _begin_local_turn_marker(state, slot, 5)
    replacement = _replace_slot_under_same_key(state, slot)
    replacement.append("user", "first", "msg msg-u")
    replacement.append("assistant", "partial from the replacement", "msg msg-a")
    replacement.append("tool", "tool output", "msg msg-tool")
    await _begin_local_turn_marker(state, replacement, 1)
    assert state.conversation_log is not None
    before = state.conversation_log.read_messages("dashboard:chat-1")
    assert state.conversation_log.get_metadata("dashboard:chat-1")["turn_in_flight_generation"] == 1

    # The old runner's finally fires after close_slot's 2 s grace has admitted a
    # same-key replacement whose own turn is in flight. An unfenced forced save
    # from the stale object would republish ITS slot-owned metadata (no marker)
    # over the replacement's line and erase the replacement's in-flight marker.
    await _clear_local_turn_marker(state, slot, 5)

    assert state.conversation_log.get_metadata("dashboard:chat-1")["turn_in_flight_generation"] == 1
    assert state.conversation_log.read_messages("dashboard:chat-1") == before
    assert slot._turn_in_flight_generation == 0


@pytest.mark.asyncio
async def test_a_cancelled_admission_write_lands_before_the_cancel_propagates(
    tmp_path, monkeypatch
):
    """A tab close while the metadata merge is on its thread must not let that
    write land AFTER the teardown clear: the admission waits for the job."""
    import asyncio
    import threading

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "first", "msg msg-u")
    assert _save_slot_to_history(state, slot, force=True)
    assert state.conversation_log is not None
    log = state.conversation_log
    real_merge = log.update_metadata_if
    entered = threading.Event()
    release = threading.Event()

    def slow_merge(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return real_merge(*args, **kwargs)

    monkeypatch.setattr(log, "update_metadata_if", slow_merge)
    task = asyncio.ensure_future(_begin_local_turn_marker(state, slot, 6))
    await asyncio.to_thread(entered.wait, 10)
    task.cancel()
    # The cancellation is delivered only once the write has landed.
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    assert log.get_metadata("dashboard:chat-1")["turn_in_flight_generation"] == 6
    # And a clear issued afterwards is ordered after it, so nothing resurrects it.
    await _clear_local_turn_marker(state, slot, 6)
    assert "turn_in_flight_generation" not in log.get_metadata("dashboard:chat-1")


@pytest.mark.asyncio
async def test_repeated_cancellation_still_waits_for_the_admission_write(tmp_path, monkeypatch):
    """A graceful shutdown escalating after its timeout cancels the runner more
    than once. The second cancel must not abandon the thread job either: a write
    left running would land after the teardown clear and restore a stale marker."""
    import asyncio
    import threading

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "first", "msg msg-u")
    assert _save_slot_to_history(state, slot, force=True)
    assert state.conversation_log is not None
    log = state.conversation_log
    real_merge = log.update_metadata_if
    entered = threading.Event()
    release = threading.Event()

    def slow_merge(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return real_merge(*args, **kwargs)

    monkeypatch.setattr(log, "update_metadata_if", slow_merge)
    task = asyncio.ensure_future(_begin_local_turn_marker(state, slot, 7))
    await asyncio.to_thread(entered.wait, 10)
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "a cancel delivered mid-write must not end the admission"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    assert log.get_metadata("dashboard:chat-1")["turn_in_flight_generation"] == 7
    await _clear_local_turn_marker(state, slot, 7)
    assert "turn_in_flight_generation" not in log.get_metadata("dashboard:chat-1")


@pytest.mark.asyncio
async def test_a_failed_metadata_merge_never_refuses_the_turn(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "first", "msg msg-u")
    assert _save_slot_to_history(state, slot, force=True)
    slot._dirty = False
    assert state.conversation_log is not None
    monkeypatch.setattr(
        state.conversation_log,
        "update_metadata_if",
        MagicMock(side_effect=OSError("disk unavailable")),
    )

    await _begin_local_turn_marker(state, slot, 5)

    assert slot._turn_in_flight_generation == 5
    assert slot._dirty is True


@pytest.mark.asyncio
async def test_a_failed_marker_save_never_refuses_the_turn(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "do something", "msg msg-u")
    slot._turn_generation = 3
    slot._dirty = False
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_persistence._save_slot_to_history",
        MagicMock(side_effect=OSError("disk unavailable")),
    )

    generation = _local_turn_generation_for(slot)
    await _begin_local_turn_marker(state, slot, generation)

    # The marker is a recovery hint, not the user's words: it stays in memory
    # for the periodic flush and the turn goes ahead.
    assert generation == 3
    assert slot._turn_in_flight_generation == 3
    assert slot._dirty is True


def test_generation_is_bound_before_the_marker_save_is_awaited():
    from kiro_crew.dashboard.state import _ChatSlot

    # A cancellation inside the admission save must still leave the finally a
    # generation to retire, so the generation is a plain read, not the save's
    # return value.
    slot = _ChatSlot("chat-1")
    slot._turn_generation = 0
    assert _local_turn_generation_for(slot) == 1
    slot._turn_generation = 12
    assert _local_turn_generation_for(slot) == 12


@pytest.mark.asyncio
async def test_a_cancel_inside_the_admission_save_still_clears_the_marker(tmp_path, monkeypatch):
    """Tab close while the marker save is in flight: the save commits after the
    cancel, and teardown must still retire the marker it wrote."""
    import asyncio

    from kiro_crew.dashboard.chat import _run_chat

    state = _state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("cancel-in-admission")
    slot.append("user", "hello", "msg msg-u")
    real_begin = __import__(
        "kiro_crew.dashboard.chat_runner", fromlist=["x"]
    )._begin_local_turn_marker

    async def cancelling_begin(st, sl, generation):
        await real_begin(st, sl, generation)
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_runner._begin_local_turn_marker", cancelling_begin
    )

    # _run_chat absorbs the cancellation (its except arm persists the partial
    # reply) and reaches its finally, which owns the clear.
    await _run_chat(state, slot, "hello")

    assert slot._turn_in_flight_generation == 0
    assert state.conversation_log is not None
    assert "turn_in_flight_generation" not in state.conversation_log.get_metadata(
        "dashboard:cancel-in-admission"
    )


@pytest.mark.asyncio
async def test_clear_for_another_generation_writes_nothing(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._turn_in_flight_generation = 5
    save = AsyncMock(return_value=True)
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.save_slot_off_loop", save)

    await _clear_local_turn_marker(state, slot, 4)

    save.assert_not_awaited()
    assert slot._turn_in_flight_generation == 5


def test_empty_window_forced_save_writes_the_marker_as_a_clearable_field(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "hello", "msg msg-u")
    assert _save_slot_to_history(state, slot, force=True)
    assert state.conversation_log is not None

    # The merge writer cannot delete a key, so it writes the current value
    # either way: the admitted generation while a turn runs, zero afterwards.
    slot.messages.clear()
    slot._turn_in_flight_generation = 6
    assert _save_slot_to_history(state, slot, force=True)
    assert state.conversation_log.get_metadata("dashboard:chat-1")["turn_in_flight_generation"] == 6
    slot._turn_in_flight_generation = 0
    assert _save_slot_to_history(state, slot, force=True)
    assert state.conversation_log.get_metadata("dashboard:chat-1")["turn_in_flight_generation"] == 0


# ── restore reconciliation ─────────────────────────────────────────────────


def test_partial_assistant_and_finished_tools_are_flagged_after_restart(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "inspect the page", "msg msg-u")
    slot.append("assistant", "I am inspecting it now", "msg msg-a")
    slot.append("tool", "read complete", "tool", meta={"done": True})
    slot._turn_in_flight_generation = 11
    assert _save_slot_to_history(state, slot, force=True)
    # RED on main: this tail reads as a finished answer.
    del state._slots["chat-1"]

    restored = _rehydrate_slot_from_history(state, "chat-1")

    assert restored is not None
    assert restored.to_dict()["interrupted"] is True
    assert restored.messages[-1]["role"] == "error"
    assert restored.messages[-1]["meta"]["kind"] == _RESTART_KIND
    assert restored._turn_in_flight_generation == 0
    assert restored._dirty is True
    # Appended past the window boundary, so the next save writes it rather than
    # counting it as already on disk.
    assert restored._disk_window_len == len(restored.messages) - 1


def test_reconciliation_is_idempotent_once_the_row_is_saved(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "inspect the page", "msg msg-u")
    slot.append("assistant", "partial result", "msg msg-a")
    slot._turn_in_flight_generation = 12
    assert _save_slot_to_history(state, slot, force=True)

    del state._slots["chat-1"]
    restored = _rehydrate_slot_from_history(state, "chat-1")
    assert restored is not None
    assert _save_slot_to_history(state, restored, force=True)
    assert state.conversation_log is not None
    assert "turn_in_flight_generation" not in state.conversation_log.get_metadata(
        "dashboard:chat-1"
    )

    del state._slots["chat-1"]
    restored_again = _rehydrate_slot_from_history(state, "chat-1")
    assert restored_again is not None
    assert len(_restart_rows(restored_again.messages)) == 1
    assert restored_again._turn_in_flight_generation == 0


def test_a_second_restart_before_the_save_does_not_duplicate_the_row(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "inspect the page", "msg msg-u")
    slot.append("assistant", "partial result", "msg msg-a")
    slot._turn_in_flight_generation = 12
    assert _save_slot_to_history(state, slot, force=True)

    # Two restores from the same unchanged bytes: the decision re-runs and the
    # row is appended once per process, never accumulated on disk.
    for _ in range(2):
        del state._slots["chat-1"]
        restored = _rehydrate_slot_from_history(state, "chat-1")
        assert restored is not None
        assert len(_restart_rows(restored.messages)) == 1


def test_existing_interruption_evidence_does_not_gain_a_duplicate_error(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "inspect the page", "msg msg-u")
    slot.append("assistant", "partial result", "msg msg-a")
    slot.append("error", "backend stopped", "msg msg-err")
    slot._turn_in_flight_generation = 13
    assert _save_slot_to_history(state, slot, force=True)

    del state._slots["chat-1"]
    restored = _rehydrate_slot_from_history(state, "chat-1")

    assert restored is not None
    assert restored.to_dict()["interrupted"] is True
    assert [m["role"] for m in restored.messages].count("error") == 1
    assert restored._turn_in_flight_generation == 0
    assert restored._dirty is True


def test_deliberate_stop_clears_a_stale_marker_without_flagging_interruption(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "inspect the page", "msg msg-u")
    slot.append("system", "Stopped", json.dumps({"kind": "stop_event"}))
    slot._turn_in_flight_generation = 14
    assert _save_slot_to_history(state, slot, force=True)

    del state._slots["chat-1"]
    restored = _rehydrate_slot_from_history(state, "chat-1")

    assert restored is not None
    assert restored.to_dict()["interrupted"] is False
    assert _restart_rows(restored.messages) == []
    assert restored._turn_in_flight_generation == 0
    assert restored._dirty is True


def test_a_stop_card_behind_tool_rows_still_counts_as_deliberate(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "inspect the page", "msg msg-u")
    slot.append("assistant", "working", "msg msg-a")
    slot.append("system", "Stopped", json.dumps({"kind": "stop_event"}))
    slot.append("tool", "late tool row", "tool", meta={"done": True})
    slot._turn_in_flight_generation = 14
    assert _save_slot_to_history(state, slot, force=True)

    del state._slots["chat-1"]
    restored = _rehydrate_slot_from_history(state, "chat-1")

    assert restored is not None
    assert _restart_rows(restored.messages) == []
    assert restored.to_dict()["interrupted"] is False


@pytest.mark.parametrize("malformed", [True, "11", -3, 0, 2.5, None])
def test_malformed_marker_values_are_ignored(tmp_path, malformed):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "hello", "msg msg-u")
    slot.append("assistant", "complete", "msg msg-a")
    assert _save_slot_to_history(state, slot, force=True)
    assert state.conversation_log is not None
    state.conversation_log.update_metadata(
        "dashboard:chat-1", {"turn_in_flight_generation": malformed}
    )

    del state._slots["chat-1"]
    restored = _rehydrate_slot_from_history(state, "chat-1")

    assert restored is not None
    assert restored.to_dict()["interrupted"] is False
    assert _restart_rows(restored.messages) == []
    # No marker means nothing to clear: the window is not dirtied for nothing.
    assert restored._dirty is False


def test_recent_session_restore_also_reconciles_the_marker(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "inspect the page", "msg msg-u")
    slot.append("assistant", "partial result", "msg msg-a")
    slot._turn_in_flight_generation = 15
    assert _save_slot_to_history(state, slot, force=True)
    del state._slots["chat-1"]

    assert restore_recent_sessions(state, window_minutes=30) == 1

    restored = state.get_slot("chat-1")
    assert restored is not None
    assert restored.to_dict()["interrupted"] is True
    assert restored.messages[-1]["meta"]["kind"] == _RESTART_KIND
    assert restored._turn_in_flight_generation == 0


def test_local_marker_and_relay_marker_together_append_one_row(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "inspect the page", "msg msg-u")
    slot.append("assistant", "partial result", "msg msg-a")
    assert _save_slot_to_history(state, slot, force=True)
    assert state.conversation_log is not None
    # Inconsistent metadata claiming both kinds of execution: one row, not two.
    state.conversation_log.update_metadata(
        "dashboard:chat-1",
        {
            "turn_in_flight_generation": 4,
            "executor": "remote",
            "instance_id": "peer-1",
            "remote_slot": "chat-9",
            "relay_in_flight": True,
        },
    )

    del state._slots["chat-1"]
    restored = _rehydrate_slot_from_history(state, "chat-1")

    assert restored is not None
    assert [m["role"] for m in restored.messages].count("error") == 1
    assert restored.messages[-1]["meta"]["kind"] == _RESTART_KIND


# ── _run_chat wiring ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_landed_turn_leaves_no_marker_on_disk(tmp_path, monkeypatch):
    from kiro_crew.acp.types import STOP_REASON_END_TURN
    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    state = _state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("landed")
    # The send handler appends the user row before dispatch; mirror it so the
    # slot has a window (an empty newborn has no metadata line to carry a key).
    slot.append("user", "hello", "msg msg-u")
    client = _client_streaming(
        [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="the answer"),
            LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
    )
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    await _run_chat(state, slot, "hello")

    assert slot._turn_in_flight_generation == 0
    assert state.conversation_log is not None
    assert "turn_in_flight_generation" not in state.conversation_log.get_metadata(
        "dashboard:landed"
    )
    # And a restart afterwards reads a finished answer, not an interruption.
    del state._slots["landed"]
    restored = _rehydrate_slot_from_history(state, "landed")
    assert restored is not None
    assert restored.to_dict()["interrupted"] is False


@pytest.mark.asyncio
async def test_a_cancelled_turn_clears_its_marker_while_the_process_lives(tmp_path, monkeypatch):
    from kiro_crew.acp.types import STOP_REASON_CANCELLED
    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    state = _state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("cancelled")
    # The send handler appends the user row before dispatch; mirror it so the
    # slot has a window (an empty newborn has no metadata line to carry a key).
    slot.append("user", "hello", "msg msg-u")
    client = _client_streaming(
        [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial"),
            LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED),
        ]
    )
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    await _run_chat(state, slot, "hello")

    # The process did not die, so whatever ended the turn is already in the
    # transcript; the marker must not flag a later clean restart.
    assert slot._turn_in_flight_generation == 0
    assert state.conversation_log is not None
    assert "turn_in_flight_generation" not in state.conversation_log.get_metadata(
        "dashboard:cancelled"
    )


@pytest.mark.asyncio
async def test_shutdown_preserves_the_marker_of_an_unlanded_turn(tmp_path, monkeypatch):
    from kiro_crew import shutdown_event
    from kiro_crew.acp.types import STOP_REASON_CANCELLED
    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    state = _state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("shutdown-cancelled")
    # The send handler appends the user row before dispatch; mirror it so the
    # slot has a window (an empty newborn has no metadata line to carry a key).
    slot.append("user", "hello", "msg msg-u")
    client = _client_streaming(
        [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial"),
            LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED),
        ]
    )
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    shutdown_event.set()
    try:
        await _run_chat(state, slot, "hello")
    finally:
        shutdown_event.clear()

    assert slot._turn_in_flight_generation > 0
    assert state.conversation_log is not None
    meta = state.conversation_log.get_metadata("dashboard:shutdown-cancelled")
    assert meta["turn_in_flight_generation"] == slot._turn_in_flight_generation
    # The next process converts it into the interruption row.
    del state._slots["shutdown-cancelled"]
    restored = _rehydrate_slot_from_history(state, "shutdown-cancelled")
    assert restored is not None
    assert restored.to_dict()["interrupted"] is True


@pytest.mark.asyncio
async def test_a_turn_that_lands_during_shutdown_still_clears(tmp_path, monkeypatch):
    from kiro_crew import shutdown_event
    from kiro_crew.acp.types import STOP_REASON_END_TURN
    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    state = _state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("shutdown-landed")
    # The send handler appends the user row before dispatch; mirror it so the
    # slot has a window (an empty newborn has no metadata line to carry a key).
    slot.append("user", "hello", "msg msg-u")
    client = _client_streaming(
        [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="the answer"),
            LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
    )
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    shutdown_event.set()
    try:
        await _run_chat(state, slot, "hello")
    finally:
        shutdown_event.clear()

    assert slot._turn_in_flight_generation == 0
    assert state.conversation_log is not None
    assert "turn_in_flight_generation" not in state.conversation_log.get_metadata(
        "dashboard:shutdown-landed"
    )


@pytest.mark.asyncio
async def test_the_marker_is_on_disk_before_the_provider_streams(tmp_path, monkeypatch):
    from kiro_crew.acp.types import STOP_REASON_END_TURN
    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    state = _state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("ordered")
    # The send handler appends the user row before dispatch; mirror it so the
    # slot has a window (an empty newborn has no metadata line to carry a key).
    slot.append("user", "hello", "msg msg-u")
    seen: list[object] = []
    client = _provider_mock()

    async def _stream(msg):
        assert state.conversation_log is not None
        seen.append(
            state.conversation_log.get_metadata("dashboard:ordered").get(
                "turn_in_flight_generation"
            )
        )
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial")
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

    client.stream = _stream
    client.stream_command = _stream
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    await _run_chat(state, slot, "hello")

    # A force exit at any point of the stream would have left this value behind.
    assert seen and type(seen[0]) is int and seen[0] > 0


# ── the opening row travels with the marker ─────────────────────────────────
#
# Rows ride the periodic flush; the marker is a metadata merge written before
# dispatch. A force exit inside that window loses the opening row, and a restore
# that judges the tail without it reads the PREVIOUS turn's ending (a Stop card,
# a finished answer) instead of the lost prompt. The marker therefore carries a
# copy of the row, and every restore path puts it back before deciding.


@pytest.mark.asyncio
async def test_admission_carries_the_opening_row_and_the_clear_drops_it(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("assistant", "earlier answer", "msg msg-a")
    assert _save_slot_to_history(state, slot, force=True)
    user_row = slot.append("user", "the prompt that opens this turn", "msg msg-u")
    slot._turn_generation = 3

    await _begin_local_turn_marker(state, slot, _local_turn_generation_for(slot))

    assert state.conversation_log is not None
    stored = state.conversation_log.get_metadata("dashboard:chat-1")["turn_in_flight_prompt"]
    assert stored["role"] == "user"
    assert stored["content"] == "the prompt that opens this turn"
    assert stored["cls"] == "msg msg-u"
    assert stored["ts"] == user_row["ts"]
    assert stored["meta"] == {"mid": user_row["meta"]["mid"]}
    assert slot._turn_in_flight_prompt == stored

    slot.append("assistant", "done", "msg msg-a")
    await _clear_local_turn_marker(state, slot, 3)

    assert slot._turn_in_flight_prompt is None
    assert "turn_in_flight_prompt" not in state.conversation_log.get_metadata("dashboard:chat-1")


def test_prompt_copy_key_is_slot_owned():
    assert "turn_in_flight_prompt" in SLOT_OWNED_META_KEYS


def test_retire_clears_the_prompt_copy_with_the_generation():
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("chat-1")
    slot._turn_in_flight_generation = 9
    slot._turn_in_flight_prompt = {"role": "user", "content": "x"}
    assert _retire_local_turn_marker(slot, 8) is False
    assert slot._turn_in_flight_prompt is not None
    assert _retire_local_turn_marker(slot, 9) is True
    assert slot._turn_in_flight_prompt is None


def test_opening_row_copy_picks_the_newest_unanswered_opener():
    from kiro_crew.dashboard.chat_runner import _local_turn_opening_row
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("chat-1")
    slot.append("user", "first", "msg msg-u")
    slot.append("assistant", "answered", "msg msg-a")
    # An answered opener is not the turn being admitted.
    assert _local_turn_opening_row(slot) is None

    slot.append(
        "user",
        "second",
        "msg msg-u",
        meta={"files": ["f1"], "dirs": [], "sendId": "s-1", "_directive_user_origin": True},
    )
    slot.append("tool", "late tool row", "tool", meta={"done": True})
    copy = _local_turn_opening_row(slot)
    assert copy is not None
    assert copy["content"] == "second"
    # Identity and attachments only: no send id, no directive flag.
    assert set(copy["meta"]) == {"mid", "files", "dirs"}

    slot.append("inject", "[Cron notification] tick", "msg msg-u", meta={"injectKind": "cron"})
    copy = _local_turn_opening_row(slot)
    assert copy is not None
    assert copy["role"] == "inject" and copy["meta"]["injectKind"] == "cron"

    # An untagged inject dispatches nothing and is looked through.
    slot.append("inject", "hook halted", "msg msg-u")
    copy = _local_turn_opening_row(slot)
    assert copy is not None and copy["content"] == "[Cron notification] tick"


def _prompt_copy(row: dict) -> dict:
    return {
        "role": row["role"],
        "content": row["content"],
        "cls": row["cls"],
        "ts": row["ts"],
        "meta": {"mid": row["meta"]["mid"]},
    }


def test_a_lost_opening_row_is_restored_before_the_tail_is_judged(tmp_path):
    # The chat-434 shape with the previous turn ended by Stop and the new prompt
    # still in the flush window when the process died: on disk the tail is the
    # Stop card, the marker names a row the window does not hold.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "first", "msg msg-u")
    slot.append("assistant", "working", "msg msg-a")
    slot.append("system", "Stopped", json.dumps({"kind": "stop_event"}))
    assert _save_slot_to_history(state, slot, force=True)
    lost = slot.append("user", "second prompt, never flushed", "msg msg-u")
    assert state.conversation_log is not None
    state.conversation_log.update_metadata(
        "dashboard:chat-1",
        {"turn_in_flight_generation": 15, "turn_in_flight_prompt": _prompt_copy(lost)},
    )

    del state._slots["chat-1"]
    restored = _rehydrate_slot_from_history(state, "chat-1")

    assert restored is not None
    # RED before the copy: the Stop tail read as deliberate, no row, no flag.
    tail = restored.messages[-1]
    assert tail["role"] == "user"
    assert tail["content"] == "second prompt, never flushed"
    assert tail["ts"] == lost["ts"]
    assert tail["meta"]["mid"] == lost["meta"]["mid"]
    assert restored.to_dict()["interrupted"] is True
    # The unanswered prompt is the interruption evidence; no second row.
    assert _restart_rows(restored.messages) == []
    assert restored._turn_in_flight_generation == 0
    assert restored._turn_in_flight_prompt is None
    assert restored._dirty is True
    assert restored._disk_window_len == len(restored.messages) - 1

    # The restored row is written by the next save and survives another restart.
    assert _save_slot_to_history(state, restored, force=True)
    del state._slots["chat-1"]
    again = _rehydrate_slot_from_history(state, "chat-1")
    assert again is not None
    assert [m["content"] for m in again.messages if m["role"] == "user"] == [
        "first",
        "second prompt, never flushed",
    ]
    assert "turn_in_flight_prompt" not in state.conversation_log.get_metadata("dashboard:chat-1")


def test_an_opening_row_already_on_disk_is_not_restored_twice(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    opener = slot.append("user", "inspect the page", "msg msg-u")
    slot.append("assistant", "I am inspecting it now", "msg msg-a")
    slot.append("tool", "read complete", "tool", meta={"done": True})
    slot._turn_in_flight_generation = 11
    slot._turn_in_flight_prompt = _prompt_copy(opener)
    assert _save_slot_to_history(state, slot, force=True)

    del state._slots["chat-1"]
    restored = _rehydrate_slot_from_history(state, "chat-1")

    assert restored is not None
    assert [m["content"] for m in restored.messages if m["role"] == "user"] == ["inspect the page"]
    assert restored.messages[-1]["meta"]["kind"] == _RESTART_KIND
    assert restored.to_dict()["interrupted"] is True


def test_a_copy_without_an_id_matches_on_ts_and_role(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    opener = slot.append("user", "inspect the page", "msg msg-u")
    slot.append("assistant", "partial", "msg msg-a")
    assert _save_slot_to_history(state, slot, force=True)
    assert state.conversation_log is not None
    copy = _prompt_copy(opener)
    copy["meta"] = {}
    state.conversation_log.update_metadata(
        "dashboard:chat-1", {"turn_in_flight_generation": 16, "turn_in_flight_prompt": copy}
    )

    del state._slots["chat-1"]
    restored = _rehydrate_slot_from_history(state, "chat-1")

    assert restored is not None
    assert [m["content"] for m in restored.messages if m["role"] == "user"] == ["inspect the page"]
    assert len(_restart_rows(restored.messages)) == 1


@pytest.mark.parametrize(
    "malformed",
    [
        "a string",
        ["user", "content"],
        {"role": "assistant", "content": "an answer is not an opener"},
        {"role": "system", "content": "Stopped"},
        {"role": "user", "content": ""},
        {"role": "user"},
        {"content": "no role"},
    ],
)
def test_malformed_prompt_copies_are_ignored(tmp_path, malformed):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "inspect the page", "msg msg-u")
    slot.append("assistant", "partial", "msg msg-a")
    assert _save_slot_to_history(state, slot, force=True)
    assert state.conversation_log is not None
    state.conversation_log.update_metadata(
        "dashboard:chat-1",
        {"turn_in_flight_generation": 17, "turn_in_flight_prompt": malformed},
    )

    del state._slots["chat-1"]
    restored = _rehydrate_slot_from_history(state, "chat-1")

    assert restored is not None
    # The generation still counts; only the copy is dropped.
    assert [m["role"] for m in restored.messages] == ["user", "assistant", "error"]
    assert restored.to_dict()["interrupted"] is True


def test_a_restored_copy_carries_only_identity_and_attachments(tmp_path):
    # The metadata line is a writable file: a flag planted on the copy must not
    # come back as a row property another reader acts on.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "first", "msg msg-u")
    slot.append("assistant", "answered", "msg msg-a")
    assert _save_slot_to_history(state, slot, force=True)
    assert state.conversation_log is not None
    state.conversation_log.update_metadata(
        "dashboard:chat-1",
        {
            "turn_in_flight_generation": 18,
            "turn_in_flight_prompt": {
                "role": "user",
                "content": "lost prompt",
                "cls": 12,
                "ts": None,
                "meta": {
                    "mid": "m-lost",
                    "files": ["a.txt", 7],
                    "dirs": ["src"],
                    "_directive_user_origin": True,
                    "sendId": "s-9",
                    "injectKind": "cron",
                },
            },
        },
    )

    del state._slots["chat-1"]
    restored = _rehydrate_slot_from_history(state, "chat-1")

    assert restored is not None
    tail = restored.messages[-1]
    assert tail["role"] == "user" and tail["content"] == "lost prompt"
    assert tail["cls"] == ""
    # A mixed-type list is dropped whole; the clean one survives.
    assert tail["meta"] == {"mid": "m-lost", "dirs": ["src"], "injectKind": "cron"}
    assert restored.to_dict()["interrupted"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_a_restricted_session_never_gets_a_marker(tmp_path, mode):
    # The transcript save honours the retention mode, so a marker written to a
    # restricted session's existing file could never be cleared and the prompt
    # copy would outlive the session. The admission writes nothing, on disk or
    # in memory, and the teardown clear has nothing to do.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "earlier, while persistent", "msg msg-u")
    slot.append("assistant", "answered", "msg msg-a")
    assert _save_slot_to_history(state, slot, force=True)
    assert state.conversation_log is not None
    before = dict(state.conversation_log.get_metadata("dashboard:chat-1"))
    slot.memory_mode = mode
    slot.append("user", "a private prompt", "msg msg-u")
    slot._turn_generation = 4

    await _begin_local_turn_marker(state, slot, _local_turn_generation_for(slot))
    await _clear_local_turn_marker(state, slot, 4)

    after = state.conversation_log.get_metadata("dashboard:chat-1")
    assert "turn_in_flight_generation" not in after
    assert "turn_in_flight_prompt" not in after
    assert after == before
    assert slot._turn_in_flight_generation == 0
    assert slot._turn_in_flight_prompt is None
    # The private prompt never reached the file through any path.
    transcript = (tmp_path / "sessions").rglob("*.jsonl")
    assert all("a private prompt" not in path.read_text() for path in transcript)
