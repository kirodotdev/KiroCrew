"""Deferred-note hold: restore is a trust boundary.

Split from ``test_deferred_note_persistence.py`` to keep both files under the
line cap. Persisted notes are sanitized and capped at ``MAX_DEFERRED_NOTES`` on
restore, a note without an authorization session is dropped, a malformed context
half is dropped alone, and an async flush loads a lazily restored window off the
event loop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state
from test_deferred_note_persistence import _hold_note, _meta, _persist, _seeded_slot

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat_handlers import _persist_deferred_note_hold
from kiro_crew.dashboard.chat_persistence import (
    _rehydrate_slot_from_history,
    _save_slot_to_history,
)
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.dashboard.slot_buffers import (
    MAX_DEFERRED_NOTE_CHARS,
    MAX_DEFERRED_NOTES,
    DeferredHoldFull,
    DeferredHoldOutcome,
    NoteEvidence,
    drop_committed_restored_notes,
    sanitize_restored_deferred_notes,
    serialize_deferred_notes,
)
from kiro_crew.dashboard.state import DashboardState, load_window_off_loop
from kiro_crew.session_lifecycle import STOP_DECLINED_KEY_MAX_CHARS


class TestRestoreTrustBoundary:
    """Gate (d): persisted metadata is validated, capped, and fail-closed."""

    def test_restore_is_a_trust_boundary(self, tmp_path: Path, monkeypatch):
        """Persisted notes are sanitized and capped at the durable CEILING
        (not the live cap — every durable entry is a 200-acknowledged note,
        and a restore that kept only the live cap's worth would silently
        discard acknowledged content); a note without an authorization
        session is dropped, never delivered unconditionally."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "tb1")
        session = effective_session_key(slot)
        ceiling = 3 * MAX_DEFERRED_NOTES
        raw = [
            "not a dict",
            {"content": "", "session": session},  # empty content: dropped
            {"content": "no session"},  # unconditional delivery: dropped
            {"content": "bad session", "session": 7},  # non-str session: dropped
        ] + [
            {"content": f"n{i}", "session": session, "cls": "", "context": "not-a-dict"}
            for i in range(ceiling + 5)
        ]
        state.conversation_log.update_metadata(slot_history_key(slot), {"deferred_notes": raw})

        del state._slots["tb1"]
        restored = _rehydrate_slot_from_history(state, "tb1")
        assert restored is not None
        notes = restored._deferred_notes
        assert len(notes) == ceiling, "a restore is bounded by the durable ceiling"
        assert [n["content"] for n in notes] == [f"n{i}" for i in range(ceiling)]
        for note in notes:
            assert note["session"] == session
            assert note["cls"] == "reconcile-note"  # empty cls falls back
            assert note["context"] is None  # non-dict context dropped

    def test_restore_replays_every_acknowledged_entry_up_to_the_ceiling(
        self, tmp_path: Path, monkeypatch
    ):
        """A durable hold at the 3x ceiling (10 delivered-but-unsaved retained
        + 10 live) is a documented, supported state. A restore must replay ALL
        of it: the newest half are undelivered 200-acknowledged notes whose
        callers were told not to re-post, so capping the restore at the live
        cap would silently discard exactly those."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "tb2")
        session = effective_session_key(slot)
        ceiling = 3 * MAX_DEFERRED_NOTES
        entries = [
            {
                "id": f"ack{i:09d}",
                "content": f"acked {i}",
                "cls": "reconcile-note",
                "session": session,
            }
            for i in range(ceiling)
        ]
        state.conversation_log.update_metadata(slot_history_key(slot), {"deferred_notes": entries})

        del state._slots["tb2"]
        restored = _rehydrate_slot_from_history(state, "tb2")
        assert restored is not None
        assert len(restored._deferred_notes) == ceiling
        restored._titled = True
        assert (
            restored.flush_deferred_notes() == ceiling
        ), "the first flush must deliver every restored acknowledged note"
        # The save that commits the delivered rows retires all of them.
        restored.drain()
        _save_slot_to_history(state, restored, closed=False)
        assert "deferred_notes" not in _meta(state, restored)

    def test_restored_context_is_schema_validated(self):
        """A malformed context half must be dropped ALONE (visible note kept):
        a non-numeric maxAge/injectedAt raises TypeError inside
        context_entry_expired at promotion, and a missing content KeyErrors at
        drain — a poison pill that re-raises at every flush seam."""
        good_ctx = {
            "content": "ctx",
            "source": "note",
            "ephemeral": True,
            "injectedAt": 123.0,
            "maxAge": 60,
            "noteSession": "stale:stamp",  # must be discarded on rebuild
        }
        raw = [
            {"content": "bad maxAge", "session": "s", "context": {**good_ctx, "maxAge": "bad"}},
            {
                "content": "bad injectedAt",
                "session": "s",
                "context": {**good_ctx, "injectedAt": None},
            },
            {
                "content": "no ctx content",
                "session": "s",
                "context": {"source": "note", "injectedAt": 1.0, "ephemeral": True},
            },
            {
                "content": "bool injectedAt",
                "session": "s",
                "context": {**good_ctx, "injectedAt": True},
            },
            {"content": "good", "session": "s", "context": dict(good_ctx)},
        ]
        notes = sanitize_restored_deferred_notes(raw)
        assert [n["content"] for n in notes] == [
            "bad maxAge",
            "bad injectedAt",
            "no ctx content",
            "bool injectedAt",
            "good",
        ]
        assert [n["context"] for n in notes[:4]] == [None, None, None, None]
        kept = notes[4]["context"]
        assert kept == {
            "content": "ctx",
            "source": "note",
            "ephemeral": True,
            "injectedAt": 123.0,
            "maxAge": 60,
        }, "a valid context is rebuilt with exactly the known keys"

    def test_serialized_hold_is_verbatim(self):
        """The durable copy replays exactly what the 200 accepted — content is
        never truncated or altered. The size problem is solved at the enqueue
        boundary (413) instead."""
        content = "x" * MAX_DEFERRED_NOTE_CHARS
        note = {
            "content": content,
            "cls": "reconcile-note",
            "context": {"content": content, "source": "note", "ephemeral": True, "injectedAt": 1.0},
            "session": "s",
        }
        [entry] = serialize_deferred_notes([note])
        assert entry["content"] == content
        assert entry["context"]["content"] == content

    def test_restore_replaces_over_bound_id(self):
        oversized = "i" * 13
        [note] = sanitize_restored_deferred_notes(
            [{"id": oversized, "content": "kept", "session": "s"}]
        )
        assert note["id"] != oversized
        assert len(note["id"]) == 12

    def test_restore_replaces_over_bound_class(self):
        oversized = "c" * 65
        [note] = sanitize_restored_deferred_notes(
            [{"content": "kept", "cls": oversized, "session": "s"}]
        )
        assert note["cls"] == "reconcile-note"

    def test_restore_drops_over_bound_session(self):
        oversized = "s" * (STOP_DECLINED_KEY_MAX_CHARS + 1)
        notes = sanitize_restored_deferred_notes(
            [
                {"content": "dropped", "session": oversized},
                {"content": "kept", "session": "s"},
            ]
        )
        assert [note["content"] for note in notes] == ["kept"]

    def test_restore_drops_over_bound_content_instead_of_truncating(self):
        """The enqueue boundary rejects oversized deferred notes before any
        200, so an over-bound persisted entry can only be tampering or
        corruption — dropped fail-closed, never altered."""
        oversized = "x" * (MAX_DEFERRED_NOTE_CHARS + 1)
        raw = [
            {"content": oversized, "session": "s"},
            {"content": "kept", "session": "s", "context": None},
        ]
        notes = sanitize_restored_deferred_notes(raw)
        assert [n["content"] for n in notes] == ["kept"]

    def test_hold_full_refuses_instead_of_evicting(self, tmp_path: Path, monkeypatch):
        """A retained entry is the only durable copy of an acknowledged note.
        When the union would exceed the ceiling, the NEW note is refused
        (DeferredHoldFull -> the handler's 429), never an eviction."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "hf1")
        session = effective_session_key(slot)
        # Fill the durable hold to the 3x ceiling with retained entries.
        retained = [
            {"id": f"ret{i:09d}", "content": f"r{i}", "cls": "reconcile-note", "session": session}
            for i in range(3 * MAX_DEFERRED_NOTES)
        ]
        state.conversation_log.update_metadata(slot_history_key(slot), {"deferred_notes": retained})
        slot._deferred_notes.append(
            {
                "id": "new000000001",
                "content": "one too many",
                "cls": "reconcile-note",
                "context": None,
                "session": session,
            }
        )
        with pytest.raises(DeferredHoldFull):
            _persist(state, slot)
        persisted = _meta(state, slot)["deferred_notes"]
        assert len(persisted) == 3 * MAX_DEFERRED_NOTES, "no retained entry may be evicted"
        assert all(entry["id"].startswith("ret") for entry in persisted)

    def test_plain_notes_fill_no_more_of_the_hold_than_two_live_caps(
        self, tmp_path: Path, monkeypatch
    ):
        """The third share of the durable hold is for merge cards alone: a plain
        note is refused at the plain-note ceiling, and a merge card past it is
        still accepted."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "hf2")
        session = effective_session_key(slot)
        retained = [
            {"id": f"ret{i:09d}", "content": f"r{i}", "cls": "reconcile-note", "session": session}
            for i in range(2 * MAX_DEFERRED_NOTES)
        ]
        state.conversation_log.update_metadata(slot_history_key(slot), {"deferred_notes": retained})
        plain_note = {
            "id": "new000000001",
            "content": "one plain note too many",
            "cls": "reconcile-note",
            "context": None,
            "session": session,
        }
        slot._deferred_notes.append(plain_note)
        with pytest.raises(DeferredHoldFull):
            _persist(state, slot)
        assert len(_meta(state, slot)["deferred_notes"]) == 2 * MAX_DEFERRED_NOTES

        slot._deferred_notes[:] = [
            {
                **plain_note,
                "id": "card00000001",
                "content": "merge card",
                "merged_from": {
                    "session": "dashboard:fork",
                    "slot": "fork",
                    "title": "Fork",
                    "after": "",
                    "through": "message-1",
                    "digest": "ab" * 32,
                    "messages": 1,
                },
            }
        ]
        assert _persist(state, slot).written is True
        persisted = _meta(state, slot)["deferred_notes"]
        assert len(persisted) == 2 * MAX_DEFERRED_NOTES + 1
        assert persisted[-1]["id"] == "card00000001"

    def test_ensure_pins_a_note_a_racing_flush_already_drained(self, tmp_path: Path, monkeypatch):
        """F2: the POST's note can be drained by a turn-end flush before the
        worker thread reads the live hold. The write must still contain that
        note's entry — otherwise the 200 acknowledges a note with no durable
        copy anywhere."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "en1")
        note = {
            "id": "racedrained1",
            "content": "drained before the persist read",
            "cls": "reconcile-note",
            "context": None,
            "session": effective_session_key(slot),
        }
        # The racing flush already drained it: the live hold is empty and the
        # note was never on disk.
        assert slot._deferred_notes == []
        assert _persist(state, slot, ensure=note).written is True
        persisted = _meta(state, slot)["deferred_notes"]
        assert [entry["id"] for entry in persisted] == ["racedrained1"]

    def test_non_finite_ttl_fields_are_dropped_fail_closed(self):
        """NaN/Infinity pass isinstance and sign checks (NaN comparisons are
        all False), producing restored context that never expires. The
        sanitizer drops the context half fail-closed while keeping the
        visible note, same as any other malformed context."""
        base = {
            "content": "note body",
            "session": "dashboard:x",
            "cls": "reconcile-note",
        }

        def entry(ctx_overrides):
            ctx = {
                "content": "ctx",
                "source": "note",
                "ephemeral": True,
                "injectedAt": 1_000.0,
            }
            ctx.update(ctx_overrides)
            return dict(base, id="ttlprobe0001", context=ctx)

        for bad in (
            {"injectedAt": float("nan")},
            {"injectedAt": float("inf")},
            {"maxAge": float("nan")},
            {"maxAge": float("inf")},
            {"maxAge": 0},
            {"maxAge": -1},
            {"maxAge": 10**400},
        ):
            restored = sanitize_restored_deferred_notes([entry(bad)])
            assert len(restored) == 1, f"visible note must survive {bad}"
            assert restored[0]["context"] is None, f"context must drop for {bad}"
        good = sanitize_restored_deferred_notes([entry({"maxAge": 60})])
        assert good[0]["context"] is not None

    def test_restore_drops_an_entry_whose_row_is_already_committed(
        self, tmp_path: Path, monkeypatch
    ):
        """The rows-only handover save commits the delivered row (meta.noteId)
        while deferring the metadata rewrite, so a restart in that window
        sees BOTH the committed row and the stale on-disk hold. Restoring
        that hold would deliver the acknowledged note a second time — the
        restore drops entries the transcript already owns."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "rowwin")
        key = slot_history_key(slot)
        note = {
            "id": "rowcommitted",
            "content": "delivered; save was rows-only; hold is stale",
            "cls": "reconcile-note",
            "context": None,
            "session": effective_session_key(slot),
        }
        slot._deferred_notes.append(note)
        slot._titled = True
        # Deliver the row and commit it with a full save...
        assert slot.flush_deferred_notes() == 1
        slot.drain()
        _save_slot_to_history(state, slot, closed=False)
        # ...then recreate the rows-only window: the stale hold is still on
        # the metadata line even though the row is committed.
        state.conversation_log.update_metadata(
            key, {"deferred_notes": serialize_deferred_notes([note])}
        )
        del state._slots["rowwin"]
        restored = _rehydrate_slot_from_history(state, "rowwin")
        assert restored is not None
        assert restored._deferred_notes == [], "a committed row's hold must not replay"
        # The filtered entry's row lives in the already-committed transcript,
        # which a later save's own window may never carry (rows-only handover
        # commits into the frozen prefix). The restore must record the id for
        # row-less retirement, and the next full save must actually retire
        # the entry — otherwise it survives every retirement pass and
        # permanently consumes one of the durable hold's ceiling slots.
        assert "rowcommitted" in restored._dropped_note_ids
        restored._titled = True
        restored.append("user", "next turn after restore")
        restored.drain()
        _save_slot_to_history(state, restored, closed=False)
        leftover = state.conversation_log._read_metadata(key).get("deferred_notes") or []
        assert all(
            entry.get("id") != "rowcommitted" for entry in leftover
        ), "the committed entry must be retired by the next full save, not retained forever"

    @staticmethod
    def _restore_lazy_row_whose_hold_awaits_settling(state: DashboardState, name: str):
        """Seed *name* with a committed plain note ``plaindone``, a committed
        merge card ``carddone`` and an uncommitted note ``stillheld``, leave
        all three on the metadata line as a crash between the flush and the
        save that retires them does, and bring the tab back as a sidebar row
        whose window is not loaded. Apply filled the hold from the line and
        the loader cleared the line; only the window load can settle it."""
        slot = _seeded_slot(state, name)
        key = slot_history_key(slot)
        session = effective_session_key(slot)
        block = {
            "session": "dashboard:fork",
            "slot": "fork",
            "title": "the fork",
            "after": "",
            "through": "msg-f",
            "digest": "ab" * 32,
            "messages": 1,
        }
        committed_plain = {
            "id": "plaindone",
            "content": "delivered plain note",
            "cls": "reconcile-note",
            "context": None,
            "session": session,
        }
        committed_card = {
            "id": "carddone",
            "content": "merge summary",
            "cls": "reconcile-note",
            "context": {
                "content": "merge summary",
                "source": "merge",
                "ephemeral": True,
                "injectedAt": 1_000_000.0,
            },
            "session": session,
            "merged_from": block,
        }
        held_plain = {
            "id": "stillheld",
            "content": "not delivered yet",
            "cls": "reconcile-note",
            "context": None,
            "session": session,
        }
        durable = serialize_deferred_notes([committed_plain, committed_card, held_plain])
        slot._deferred_notes = [dict(committed_plain), dict(committed_card)]
        slot._titled = True
        assert slot.flush_deferred_notes() == 2
        slot.drain()
        _save_slot_to_history(state, slot, closed=False)
        state.conversation_log.update_metadata(key, {"deferred_notes": durable})
        del state._slots[name]

        tail = state.conversation_log.read_messages_chained(key)[-2:]
        restored = _rehydrate_slot_from_history(state, name, _lazy_tail=tail)
        assert restored is not None
        assert restored.window_pending
        assert restored._lazy_window._applied.meta == {}
        assert [entry["id"] for entry in restored._deferred_notes] == [
            "plaindone",
            "carddone",
            "stillheld",
        ]
        return restored

    @staticmethod
    def _rows_carrying_note(slot, note_id: str) -> int:
        return sum(1 for row in slot.messages if (row.get("meta") or {}).get("noteId") == note_id)

    def test_a_sidebar_rows_first_window_load_settles_its_hold(self, tmp_path: Path, monkeypatch):
        """A tab the startup restore builds as a sidebar row applies its hold
        from the metadata line but loads its rows later, and the loader keeps
        no raw line meanwhile. The first window load must settle the hold the
        same way an eager restore does: the committed plain note's entry leaves
        the hold and its id is recorded for row-less retirement, the committed
        merge card's context is queued exactly once, and the uncommitted note
        stays held. Before the fix the load read the cleared line, emptied the
        hold and queued nothing."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        restored = self._restore_lazy_row_whose_hold_awaits_settling(state, "lazyhold")

        asyncio.run(load_window_off_loop(restored))

        assert not restored.window_pending
        assert [entry["id"] for entry in restored._deferred_notes] == ["stillheld"]
        assert [entry["noteId"] for entry in restored._pending_context] == ["carddone"]
        assert restored._pending_context[0]["content"] == "merge summary"
        assert "plaindone" in restored._dropped_note_ids
        assert "carddone" not in restored._dropped_note_ids

    def test_a_flush_on_a_pending_sidebar_row_settles_its_hold_first(
        self, tmp_path: Path, monkeypatch
    ):
        """The cleanup sweep and the hand-over drain flush a popped sidebar row
        without loading its window. The flush must settle the hold before it
        copies it: only the uncommitted note is delivered, each note keeps one
        row, and the committed card's context is queued once, by the settle.
        Before the fix the flush copied and cleared the unsettled hold, its
        first append loaded the window against the empty hold, and every
        committed note got a second row."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        restored = self._restore_lazy_row_whose_hold_awaits_settling(state, "lazyflush")

        assert restored.flush_deferred_notes() == 1

        assert not restored.window_pending
        assert self._rows_carrying_note(restored, "plaindone") == 1
        assert self._rows_carrying_note(restored, "carddone") == 1
        assert self._rows_carrying_note(restored, "stillheld") == 1
        assert [entry["noteId"] for entry in restored._pending_context] == ["carddone"]
        assert restored._deferred_notes == []
        assert "plaindone" in restored._dropped_note_ids
        assert "carddone" not in restored._dropped_note_ids

    @pytest.mark.parametrize("seam", ("cleanup", "handover"))
    @pytest.mark.asyncio
    async def test_async_note_flush_loads_pending_window_off_loop(
        self, seam: str, tmp_path: Path, monkeypatch
    ):
        """Cleanup and handover must preload a lazy transcript before flushing notes."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        name = f"lazy-{seam}"
        restored = self._restore_lazy_row_whose_hold_awaits_settling(state, name)
        lazy_window = restored._lazy_window
        assert lazy_window is not None

        reads_on_loop: list[bool] = []
        pending_window_type = type(lazy_window)
        original_read = pending_window_type.read

        def recording_read(self, slot):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                reads_on_loop.append(False)
            else:
                reads_on_loop.append(True)
            return original_read(self, slot)

        monkeypatch.setattr(pending_window_type, "read", recording_read)
        if seam == "cleanup":
            monkeypatch.setattr(
                chat_handlers,
                "select_idle_slot_keys",
                lambda *_args, **_kwargs: ([name], False),
            )
            async with TestClient(TestServer(_make_app(state))) as client:
                response = await client.post(
                    "/api/chat/slots/cleanup", json={"max_inactive_days": 1}
                )
                payload = await response.json()
            assert response.status == 200
            assert payload["keys"] == [name]
        else:
            state._slots.pop(name)
            replacement = state.get_or_create_slot(name)
            assert replacement is not restored
            assert chat_handlers._replacement_shares_transcript(state, name, restored)
            result = await chat_handlers._persist_handover_tail(state, name, restored)
            assert result.rows_committed is True

        assert reads_on_loop == [False]
        assert not restored.window_pending
        assert self._rows_carrying_note(restored, "stillheld") == 1

    @pytest.mark.parametrize("seam", ("cleanup", "handover"))
    @pytest.mark.asyncio
    async def test_async_note_flush_leaves_an_empty_holds_window_unread(
        self, seam: str, tmp_path: Path, monkeypatch
    ):
        """Only cleanup leaves an empty hold's pending window unread.

        Cleanup reaches no message reader when the hold is empty, matching
        ``flush_deferred_notes``'s early return. Handover must still preload:
        replacement tightening inspects ``slot.messages`` before it checks
        whether any row needs tightening, so skipping the preload would read the
        pending transcript on the event loop.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        name = f"empty-{seam}"
        restored = self._restore_lazy_row_whose_hold_awaits_settling(state, name)
        lazy_window = restored._lazy_window
        assert lazy_window is not None
        # The hold retires without this object: a save elsewhere committed the
        # rows, or every entry was already delivered. The row stays pending.
        restored._deferred_notes.clear()

        reads: list[bool] = []
        pending_window_type = type(lazy_window)
        original_read = pending_window_type.read

        def recording_read(self, slot):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                reads.append(False)
            else:
                reads.append(True)
            return original_read(self, slot)

        monkeypatch.setattr(pending_window_type, "read", recording_read)
        if seam == "cleanup":
            monkeypatch.setattr(
                chat_handlers,
                "select_idle_slot_keys",
                lambda *_args, **_kwargs: ([name], False),
            )
            async with TestClient(TestServer(_make_app(state))) as client:
                response = await client.post(
                    "/api/chat/slots/cleanup", json={"max_inactive_days": 1}
                )
                payload = await response.json()
            assert response.status == 200
            assert payload["keys"] == [name]
            assert payload["failed"] == []
        else:
            state._slots.pop(name)
            replacement = state.get_or_create_slot(name)
            assert replacement is not restored
            assert chat_handlers._replacement_shares_transcript(state, name, restored)
            result = await chat_handlers._persist_handover_tail(state, name, restored)
            assert result.rows_committed is True

        if seam == "cleanup":
            assert reads == [], "cleanup must not read an empty hold's transcript"
            assert restored.window_pending
            assert restored.loaded_messages() == []
        else:
            assert reads == [False], "handover tightening must preload off the event loop"
            assert not restored.window_pending
        assert restored._deferred_notes == []

    @pytest.mark.asyncio
    async def test_recorded_drop_dominates_durable_evidence(self, tmp_path: Path, monkeypatch):
        """A durable entry whose id the flush recorded DROPPED is already
        scheduled for row-less retirement: the next save removes it with no
        delivered row. Counting it as evidence would back a 200 with an entry
        the save destroys — a loss the caller never retries. The refusal must
        stand even when a sibling's merge writer put the entry on disk."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "dropdur")
        key = slot_history_key(slot)
        note = {
            "id": "dropdurable1",
            "content": "sibling persisted it; flush dropped it",
            "cls": "reconcile-note",
            "context": None,
            "session": "app:some-other-session",
        }
        # A sibling's merge writer committed the whole live list first...
        state.conversation_log.update_metadata(
            key, {"deferred_notes": serialize_deferred_notes([note])}
        )
        # ...then the turn-end flush dropped the note at its seam.
        slot._dropped_note_ids.add("dropdurable1")
        assert slot._deferred_notes == []
        assert slot_history_key(slot) == key

        resp = await _persist_deferred_note_hold(state, slot, note, key)
        assert resp is not None, "a drop-marked durable entry is not delivery evidence"
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_delete_landing_before_the_probe_is_still_refused(
        self, tmp_path: Path, monkeypatch
    ):
        """A permanent delete can land BEFORE the durable-identity probe runs,
        so a point-in-time file check sees 'no file' and misreads the slot as
        never-persisted — a 200 for a note that cannot survive restart. The
        slot-side flag is monotonic: once the slot has observed its on-disk
        identity, a no-line outcome is refused no matter when the delete
        landed."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "earlydel")
        key = slot_history_key(slot)
        assert slot._disk_meta_observed is True, "the full save proves the identity"
        note = _hold_note(slot, "racing an earlier permanent delete")
        note["id"] = "earlydelete01"
        # The delete lands BEFORE _persist_deferred_note_hold's probe.
        state.conversation_log._path(key).unlink()

        def _no_line(conversation_log, s, ensure, authorized_history_key):
            return DeferredHoldOutcome(written=False, evidence=NoteEvidence(False, False))

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.persist_deferred_notes_sync", _no_line
        )
        resp = await _persist_deferred_note_hold(state, slot, note, key)
        assert resp is not None and resp.status == 404
        assert note not in slot._deferred_notes

    def test_committed_row_filter_is_pure_over_the_loaded_window(self):
        """The restore-time dedup must scan the message window the restore
        already loaded — never re-open the transcript on the event loop. The
        filter drops exactly the entries whose noteId a loaded row carries,
        and an empty/absent window keeps everything (duplicate direction,
        never loss)."""
        notes = [
            {"id": "aaa111", "content": "x", "session": "s"},
            {"id": "bbb222", "content": "y", "session": "s"},
        ]
        messages = [
            {"role": "reconcile-note", "content": "x", "meta": {"noteId": "aaa111"}},
            {"role": "user", "content": "unrelated"},
            "not-a-dict",
        ]
        kept = drop_committed_restored_notes(messages, list(notes))
        assert [entry["id"] for entry in kept] == ["bbb222"]
        assert drop_committed_restored_notes([], list(notes)) == notes
        assert drop_committed_restored_notes(None, list(notes)) == notes

    def test_late_ensure_does_not_resurrect_a_committed_note(self, tmp_path: Path, monkeypatch):
        """A delayed persist worker must not re-add a hold whose delivered row
        a flush+save pair already COMMITTED and retired — the restore would
        replay a second copy of a line the transcript permanently carries.
        Under the lock, an ensure whose noteId is already in the committed
        transcript is skipped."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "le1")
        note = {
            "id": "committed0001",
            "content": "delivered, saved, retired — then the worker wakes",
            "cls": "reconcile-note",
            "context": None,
            "session": effective_session_key(slot),
        }
        slot._deferred_notes.append(note)
        slot._titled = True
        # The flush delivers the row and the save commits + retires it —
        # all BEFORE the enqueue's persist worker gets scheduled.
        assert slot.flush_deferred_notes() == 1
        slot.drain()
        _save_slot_to_history(state, slot, closed=False)
        assert "deferred_notes" not in _meta(state, slot)

        # The late worker finally runs, with the note it was told to ensure.
        assert _persist(state, slot, ensure=note).written is True
        assert not _meta(state, slot).get(
            "deferred_notes"
        ), "a committed note's hold must not be resurrected"
        # And a restart replays nothing extra: exactly one copy in the rows.
        del state._slots["le1"]
        restored = _rehydrate_slot_from_history(state, "le1")
        assert restored is not None
        assert restored._deferred_notes == []
        copies = [
            m
            for m in restored.messages
            if m.get("role") == "inject" and "then the worker wakes" in str(m.get("content"))
        ]
        assert len(copies) == 1

    def test_empty_window_merge_save_unions_instead_of_shrinking(self, tmp_path: Path, monkeypatch):
        """F1's merge-path sibling: a forced save of an empty-window slot must
        not mirror the (possibly just-drained) live hold over a disk entry
        whose delivered row this save does not write. Merge writers union;
        only the full save's paired snapshot retires."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("mw1")
        slot._titled = True
        key = slot_history_key(slot)
        state.conversation_log.update_metadata(
            key,
            {
                "deferred_notes": [
                    {
                        "id": "keepme000001",
                        "content": "delivered into an unsaved window",
                        "cls": "reconcile-note",
                        "session": effective_session_key(slot),
                    }
                ]
            },
        )
        assert slot.messages == [] and slot._deferred_notes == []
        _save_slot_to_history(state, slot, force=True)
        persisted = _meta(state, slot).get("deferred_notes")
        assert (
            persisted and persisted[0]["id"] == "keepme000001"
        ), "the empty-window merge save must retain the disk entry"

    def test_sanitizer_rejects_non_list_values(self):
        assert sanitize_restored_deferred_notes(None) == []
        assert sanitize_restored_deferred_notes("[]") == []
        assert sanitize_restored_deferred_notes({"content": "x"}) == []
