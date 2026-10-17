"""The periodic flush persists metadata edits on a message-less newborn.

A session from ``session_create`` has a birth metadata line before its first
message. Edits that only mark the slot dirty (the sidebar color PATCH is one)
reach that line through ``flush_slot_now`` even though the slot has no message
window, so a crash after the route acknowledges the edit keeps it.
"""

from __future__ import annotations

import asyncio

from chat_test_helpers import _make_state

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key


def _newborn(state):
    caller = state.get_or_create_slot("chat-1")
    created = asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))
    return state.get_slot(created["target"])


def test_a_dirty_newborn_metadata_edit_is_flushed(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    state = _make_state(tmp_path)
    child = _newborn(state)
    assert not child.messages
    assert state.conversation_log.get_metadata(slot_history_key(child)), "birth line"

    # What the sidebar color PATCH does: set the field and mark the slot dirty.
    child.color_index = 4
    child._dirty = True

    state.flush_slot_now(child)

    meta = state.conversation_log.get_metadata(slot_history_key(child))
    assert meta.get("color_index") == 4
    assert child._dirty is False


def test_a_plain_empty_tab_still_gets_no_file(tmp_path):
    state = _make_state(tmp_path)
    plain = state.get_or_create_slot("chat-plain")
    plain.color_index = 1
    plain._dirty = True

    state.flush_slot_now(plain)

    meta, readable = state.conversation_log.get_metadata_status(slot_history_key(plain))
    assert readable and not meta, "no metadata line may be invented for a plain empty tab"


def test_a_clean_empty_slot_is_not_saved(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-quiet")
    slot._dirty = False
    calls: list[dict] = []

    from kiro_crew.dashboard import chat

    monkeypatch.setattr(chat, "_save_slot_to_history", lambda *a, **k: calls.append(k))

    state.flush_slot_now(slot)

    assert calls == []


def _commit_before_the_lock(state, monkeypatch, commit):
    """Run ``commit`` after the save took its snapshot and before its locked merge."""
    log = state.conversation_log
    original = log.update_metadata_if

    def _interleaved(key, fields, guard, **kwargs):
        commit()
        return original(key, fields, guard, **kwargs)

    monkeypatch.setattr(log, "update_metadata_if", _interleaved)


def test_an_older_queue_snapshot_does_not_overwrite_a_newer_committed_queue(tmp_path, monkeypatch):
    from kiro_crew.dashboard.slot_queue_repository import queue_persist_signature

    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    state = _make_state(tmp_path)
    child = _newborn(state)
    key = slot_history_key(child)
    child.color_index = 4
    child._dirty = True
    newer = [{"text": "follow-up", "id": "q-1"}]

    def _another_writer_commits_the_queue():
        state.conversation_log.update_metadata(key, {"queued_prompts": newer})
        child._queue_persisted_sig = queue_persist_signature(newer)

    _commit_before_the_lock(state, monkeypatch, _another_writer_commits_the_queue)

    state.flush_slot_now(child)

    meta = state.conversation_log.get_metadata(key)
    assert meta.get("queued_prompts") == newer
    assert meta.get("color_index") != 4
    assert child._dirty is True, "a refused pass stays owed to the next flush"


def test_a_slot_replaced_under_the_same_name_is_not_written_over(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    state = _make_state(tmp_path)
    child = _newborn(state)
    key = slot_history_key(child)
    child.color_index = 4
    child._dirty = True

    def _recreate_under_the_same_name():
        state._slots[child.key] = object()

    _commit_before_the_lock(state, monkeypatch, _recreate_under_the_same_name)

    state.flush_slot_now(child)

    assert state.conversation_log.get_metadata(key).get("color_index") != 4
    assert child._dirty is True


def test_a_closing_empty_save_still_commits_the_close(tmp_path, monkeypatch):
    from kiro_crew.dashboard import chat_persistence as cp
    from kiro_crew.dashboard.slot_queue_repository import queue_persist_signature

    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    state = _make_state(tmp_path)
    child = _newborn(state)
    key = slot_history_key(child)
    newer = [{"text": "follow-up", "id": "q-1"}]

    def _another_writer_commits_the_queue():
        state.conversation_log.update_metadata(key, {"queued_prompts": newer})
        child._queue_persisted_sig = queue_persist_signature(newer)

    _commit_before_the_lock(state, monkeypatch, _another_writer_commits_the_queue)

    assert cp._save_slot_to_history(state, child, closed=True) is True
    assert state.conversation_log.get_metadata(key).get("closed") is True


def test_a_newer_queue_committed_after_the_merge_keeps_its_witness(tmp_path, monkeypatch):
    from kiro_crew.dashboard.slot_queue_repository import queue_persist_signature

    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    state = _make_state(tmp_path)
    child = _newborn(state)
    key = slot_history_key(child)
    child.color_index = 4
    child._dirty = True
    newer = [{"text": "follow-up", "id": "q-1"}]
    log = state.conversation_log
    original = log.update_metadata_if

    def _a_full_save_commits_right_after(key_, fields, guard, **kwargs):
        applied = original(key_, fields, guard, **kwargs)
        # The merge's lock is released here; a newer writer commits next.
        log.update_metadata(key, {"queued_prompts": newer})
        child._queue_persisted_sig = queue_persist_signature(newer)
        return applied

    monkeypatch.setattr(log, "update_metadata_if", _a_full_save_commits_right_after)

    state.flush_slot_now(child)

    assert log.get_metadata(key).get("color_index") == 4
    assert child._queue_persisted_sig == queue_persist_signature(newer)


def test_a_route_forced_save_still_commits_as_on_main(tmp_path, monkeypatch):
    # The tag, folder, pin and transfer routes save with force=True and roll
    # back on False. They do not opt in to the flush's refusals, so the same
    # race that refuses the flush still commits the route's edit.
    from kiro_crew.dashboard import chat_persistence as cp
    from kiro_crew.dashboard.slot_queue_repository import queue_persist_signature

    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    state = _make_state(tmp_path)
    child = _newborn(state)
    key = slot_history_key(child)
    child.pinned = True
    newer = [{"text": "follow-up", "id": "q-1"}]

    def _another_writer_commits_the_queue():
        state.conversation_log.update_metadata(key, {"queued_prompts": newer})
        child._queue_persisted_sig = queue_persist_signature(newer)

    _commit_before_the_lock(state, monkeypatch, _another_writer_commits_the_queue)

    assert cp._save_slot_to_history(state, child, force=True, expected_slot_name=child.key) is True
    meta = state.conversation_log.get_metadata(key)
    assert meta.get("pinned") is True
    # The route's older, empty queue snapshot does not overwrite the queue the
    # other writer committed, and the witness still names that newer queue.
    assert meta.get("queued_prompts") == newer
    assert child._queue_persisted_sig == queue_persist_signature(newer)


def test_queue_drift_alone_on_a_line_less_tab_is_saved_once(tmp_path, monkeypatch):
    # A line-less tab has nothing on disk to carry the queue. The merge credits
    # the witness, so a second pass does not re-run the save for the same drift,
    # and the tab still gets no file.
    from kiro_crew.dashboard import chat
    from kiro_crew.dashboard.chat_utils import slot_history_key

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-queued")
    slot._dirty = False
    slot._queue_persisted_sig = "not-the-current-queue"
    assert slot.queue_persist_pending
    original = chat._save_slot_to_history
    calls: list[dict] = []

    def _counting(*a, **k):
        calls.append(k)
        return original(*a, **k)

    monkeypatch.setattr(chat, "_save_slot_to_history", _counting)

    state.flush_slot_now(slot)
    state.flush_slot_now(slot)

    assert len(calls) == 1
    assert not slot.queue_persist_pending
    assert not state.conversation_log.has_log(slot_history_key(slot))


def test_a_dirty_line_less_tab_is_saved_once(tmp_path, monkeypatch):
    from kiro_crew.dashboard import chat

    state = _make_state(tmp_path)
    plain = state.get_or_create_slot("chat-plain")
    plain._dirty = True
    original = chat._save_slot_to_history
    calls: list[dict] = []

    def _counting(*a, **k):
        calls.append(k)
        return original(*a, **k)

    monkeypatch.setattr(chat, "_save_slot_to_history", _counting)

    state.flush_slot_now(plain)
    state.flush_slot_now(plain)

    assert len(calls) == 1
    assert plain._dirty is False


def test_a_queue_cancel_on_a_clean_message_less_session_reaches_disk(tmp_path, monkeypatch):
    # A restored message-less session with a line: cancelling its persisted
    # queued prompt changes the queue without marking the slot dirty. The flush
    # must still write the cancel, or a restart restores the cancelled prompt.
    from kiro_crew.dashboard.slot_queue_repository import queue_persist_signature

    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    state = _make_state(tmp_path)
    child = _newborn(state)
    key = slot_history_key(child)
    cancelled = [{"text": "follow-up", "id": "q-1"}]
    state.conversation_log.update_metadata(key, {"queued_prompts": cancelled})
    child._queue_persisted_sig = queue_persist_signature(cancelled)
    child._dirty = False
    assert child.queue_persist_pending

    state.flush_slot_now(child)

    assert not state.conversation_log.get_metadata(key).get("queued_prompts")
    assert not child.queue_persist_pending


def test_an_unreadable_transcript_on_queue_drift_does_not_escape_the_flush(tmp_path, monkeypatch):
    # The merge reads the transcript under its lock. An OSError there stays
    # contained like any failed write, or it would end the periodic flush loop
    # for every slot.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-denied")
    slot._dirty = False
    slot._queue_persisted_sig = "not-the-current-queue"
    assert slot.queue_persist_pending

    def _denied(*_a, **_k):
        raise PermissionError("denied")

    monkeypatch.setattr(state.conversation_log, "update_metadata_if", _denied)

    state.flush_slot_now(slot)

    assert slot.queue_persist_pending, "the drift stays owed to the next pass"
