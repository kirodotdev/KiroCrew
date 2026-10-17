"""Regression test: meta redaction must iterate a snapshot, not the live dict.

``_redact_meta`` is reached from ``_save_slot_to_history``, which runs in the
flush executor thread. The event loop keeps mutating that same message's meta
while the flush runs (streaming tool calls, growing file-change lists), so
iterating ``meta.items()`` directly raised

    RuntimeError: dictionary changed size during iteration

That exception propagated out of ``_save_slot_to_history`` and aborted the whole
slot's save. Observed twice in the gateway log:

    ERROR kiro_crew.dashboard.chat_persistence: Failed to save slot
    chat-73-... to history
    WARNING kiro_crew.dashboard.state: Flush failed for slot chat-73-...

Scope of the damage, stated precisely: ``_flush_dirty_slots`` clears
``slot._dirty`` only on success, so the aborted flush left the slot dirty and the
next 5s flush retried it, and shutdown re-serialises via
``save_all_slots_to_history(force=True)``. ``atomic_write`` is the last step and
the raise happens before it, so no partial or corrupt file was ever produced.
The reliable costs are therefore a failed flush interval and the repeating
ERROR/WARNING pair; PERMANENT transcript loss additionally required a hard kill
inside the failing window.

The mutation here is driven deterministically by a nested value that writes to
its own parent when redacted, rather than by a real thread — same failure, no
timing dependence. Reverting either ``list(...)`` snapshot makes these fail.
"""
from __future__ import annotations

import pytest

from kiro_crew.dashboard.chat_utils import (
    _redact_meta,
    _redact_meta_for_role,
    _redact_value,
)


class _MutatesParentWhenRead(dict):
    """A nested meta value that grows its PARENT dict the moment it is redacted.

    Stands in for the event loop appending a new meta key while the flush thread
    is midway through iterating the same dict.
    """

    def __init__(self, parent: dict) -> None:
        super().__init__(inner="value")
        self._parent = parent

    def items(self):  # type: ignore[no-untyped-def]
        self._parent[f"appended_{len(self._parent)}"] = "by-event-loop"
        return super().items()


def _meta_that_mutates_mid_iteration() -> dict:
    meta: dict = {"first": "a", "second": "b"}
    meta["nested"] = _MutatesParentWhenRead(meta)
    meta["last"] = "c"
    return meta


def test_redact_meta_survives_concurrent_key_insertion() -> None:
    """_redact_meta does not raise when meta grows during the redaction pass."""
    meta = _meta_that_mutates_mid_iteration()

    out = _redact_meta(meta)

    # The pre-mutation key set is what gets persisted; the point is that the
    # save completes at all rather than aborting the whole slot.
    assert {"first", "second", "nested", "last"} <= set(out)
    assert out["first"] == "a"
    # The racing write did land on the live dict — proving the mutation fired
    # and that the snapshot, not luck, is what kept the pass alive.
    assert any(k.startswith("appended_") for k in meta)


def test_redact_meta_for_role_survives_concurrent_key_insertion() -> None:
    """The mcp_oauth branch iterates meta directly and needs the same snapshot."""
    meta = _meta_that_mutates_mid_iteration()
    meta["oauth_url"] = "https://example.com/authorize?client_id=abc"

    out = _redact_meta_for_role("mcp_oauth", meta)

    assert out["oauth_url"] == "https://example.com/authorize?client_id=abc"
    assert any(k.startswith("appended_") for k in meta)


def test_redact_value_snapshots_lists() -> None:
    """A list value is snapshotted too — same live-container exposure.

    Note this asserts snapshot SEMANTICS, not crash avoidance: unlike a dict, a
    list never raises on concurrent resize. Without the snapshot the appended
    element is redacted into the output (len 4); with it, it is not (len 3).
    """
    live: list = ["a", "b"]

    class _GrowsSibling(dict):
        def items(self):  # type: ignore[no-untyped-def]
            live.append("appended-by-event-loop")
            return super().items()

    live.append(_GrowsSibling(inner="x"))

    out = _redact_value(live)

    assert isinstance(out, list)
    # Snapshot taken before the mutation, so the appended element is absent from
    # this pass's output.
    assert len(out) == 3
    assert "appended-by-event-loop" in live


@pytest.mark.parametrize(
    "meta",
    [
        {},
        {"plain": "value"},
        {"nested": {"deep": {"deeper": "value"}}},
        {"listy": ["a", {"b": "c"}]},
    ],
)
def test_redact_meta_preserves_structure(meta: dict) -> None:
    """Snapshotting must not change ordinary redaction behaviour."""
    out = _redact_meta(meta)
    assert out == meta
    assert out is not meta


def test_flush_file_changes_marks_slot_dirty() -> None:
    """An in-place meta write must flag the slot, or the snapshot can drop it.

    Counterpart to the snapshot above. Before the snapshot existed, a flush
    racing `_flush_file_changes` RAISED, which left `slot._dirty` set and got the
    key persisted on the next 5s retry -- the crash was accidentally standing in
    for the missing dirty flag. With the race fixed the flush SUCCEEDS without
    the late key and clears `_dirty`, so on the error/cancel path (which, unlike
    the success path, is not followed by an explicit `save_slot_off_loop`) the
    `file_changes` would never reach disk.

    `state.py`'s in-place-mutation contract states it directly: "the periodic
    flush skips non-dirty slots, so an unflagged in-place mutation can be lost on
    restart."
    """
    from kiro_crew.dashboard.chat_runner import _flush_file_changes

    class _Slot:
        key = "chat-1-test"

        def __init__(self) -> None:
            self.messages = [{"role": "assistant", "content": "hi", "meta": {}}]
            self._file_changes = [{"path": "/tmp/x.txt", "content": "before"}]
            self._dirty = False

    slot = _Slot()
    _flush_file_changes(slot)  # type: ignore[arg-type]

    assert slot.messages[0]["meta"]["file_changes"], "meta written in place"
    assert slot._dirty is True, (
        "in-place meta write must mark the slot dirty, else the periodic flush "
        "skips it and the file_changes are lost on restart"
    )


def _flush_harness(save_fn):
    """Drive DashboardState._flush_dirty_slots with a stubbed save.

    Returns the slot so a test can assert on `_dirty` after the pass.
    """
    from kiro_crew.dashboard import chat as chat_mod
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

    class _Slot:
        key = "chat-1-test"
        # Reuse the REAL property descriptor rather than reimplementing it, so
        # this harness exercises the actual generation-bump logic.
        _dirty = _ChatSlot._dirty

        def __init__(self) -> None:
            self.messages = [{"role": "assistant", "content": "hi", "meta": {}}]
            self._dirty_flag = False
            self._dirty_gen = 0
            self._dirty = True  # through the setter, so _dirty_gen advances

    class _State:
        conversation_log = True

        # The dirty-bit bookkeeping these tests pin now lives in
        # ``flush_slot_now``, which ``_flush_dirty_slots`` calls per slot. Bind
        # the REAL method so they still exercise production logic rather than a
        # reimplementation of it.
        flush_slot_now = DashboardState.flush_slot_now

        def __init__(self) -> None:
            self._slots = {"chat-1-test": slot}

        def _persist_open_slots(self) -> None:
            pass

        def _persist_context_snapshots(self) -> None:
            pass

    slot = _Slot()
    original = chat_mod._save_slot_to_history
    chat_mod._save_slot_to_history = save_fn
    try:
        DashboardState._flush_dirty_slots(_State())  # type: ignore[arg-type]
    finally:
        chat_mod._save_slot_to_history = original
    return slot


def test_flush_dirty_slots_keeps_dirty_visible_during_save() -> None:
    """`_dirty` must read True for the WHOLE save, not just until it starts.

    Two independent readers depend on this, which is why the flush compares
    `_dirty_gen` instead of consuming the bit up front:

    * `chat_fork` (chat_fork.py:137,141) treats `_dirty` as "unpersisted
      in-memory state exists". A False read makes it skip BOTH the in-memory tail
      append and the durable pre-fork save, so it forks from stale disk and the
      new session permanently omits the newest messages.
    * `_save_slot_to_history`'s resumed-slot no-op guard skips when
      `_resumed_count > 0 and len(window) <= _resumed_count and not _dirty`.
    """
    seen: list[bool] = []

    def save_observing_dirty(state, slot, **kwargs) -> None:
        # Stands in for a concurrent chat_fork / the no-op guard reading _dirty
        # from inside the save window.
        seen.append(slot._dirty)

    _flush_harness(save_observing_dirty)

    assert seen == [True], (
        "_dirty must still read True inside the save, else a concurrent fork "
        "reads stale disk and drops the newest messages"
    )


def test_flush_dirty_slots_preserves_concurrent_dirty_mark() -> None:
    """A mark set DURING the save must survive the post-save clear.

    This is the ordering the snapshot fix exposed. The worker thread reads the
    messages, the event loop then attaches `file_changes` and sets
    `_dirty=True`, and the worker writes its now-stale snapshot. If the worker
    clears `_dirty` AFTER saving, it overwrites that request with False and the
    late key is skipped by every later pass — silent loss. Clearing before the
    save keeps the request alive.
    """

    def save_with_concurrent_mutation(state, slot, **kwargs) -> None:
        # Stands in for the event loop running _flush_file_changes while this
        # save is in flight: new meta lands, and the slot is re-marked dirty.
        slot.messages[0]["meta"]["file_changes"] = [{"path": "/tmp/x.txt"}]
        slot._dirty = True

    slot = _flush_harness(save_with_concurrent_mutation)

    assert slot._dirty is True, (
        "a dirty mark set during the save must survive, or the concurrently "
        "attached file_changes are never written to disk"
    )


def test_flush_dirty_slots_rearms_dirty_on_failure() -> None:
    """A failed save must leave the slot dirty so the next pass retries."""

    def save_that_raises(state, slot, **kwargs) -> None:
        raise OSError("disk full")

    slot = _flush_harness(save_that_raises)

    assert slot._dirty is True, "a failed flush must stay dirty to be retried"
