"""Deferred-note hold durability (issue #4093).

``POST /api/chat/slots/{slot}/note`` accepts a note while a turn is running and
replies ``200`` with ``visibleDeferred: true`` — a delivery promise for a
transcript line. Before the fix both halves of the hold lived in memory only
(``_ChatSlot._deferred_notes``), so a gateway restart between the 200 and the
next turn silently voided the promise.

What these tests pin, per the issue's regression gates:

(a) a note accepted mid-turn survives a persistence round-trip: persist →
    fresh slot restore → the first flush delivers exactly one copy, and once
    the save that commits the delivered rows lands, a second restart
    re-delivers nothing;
(b) the durable copy is retired by the SAVE that commits the delivered rows —
    atomically, in the same file write — never by the flush itself, so a crash
    between flush and save re-delivers (at-least-once) instead of losing the
    acknowledged note (loss would void the 200's promise; a repeat does not);
(c) the 200 is not returned before the durable write lands: a failed or
    unreadable-record write rolls the hold back (by identity, never equality)
    and answers a retryable 503 instead of a 200 that lies about durability;
(d) restore is a trust boundary: persisted notes are sanitized and capped at
    ``MAX_DEFERRED_NOTES``, a note without an authorization session is dropped
    rather than delivered unconditionally, and a malformed context half is
    dropped alone (poison-pill prevention) while the visible line survives.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_handlers import (
    _persist_deferred_note_hold,
    api_chat_slot_note,
)
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
    DeferredHoldRebound,
    NoteEvidence,
    drop_committed_restored_notes,
    persist_deferred_notes_sync,
    sanitize_restored_deferred_notes,
    serialize_deferred_notes,
)
from kiro_crew.dashboard.state import DashboardState


def _seeded_slot(state: DashboardState, name: str):
    """A slot with a metadata line on disk — the durable identity the hold
    attaches to (the persist guard refuses to upsert a line that a concurrent
    deletion may just have removed)."""
    slot = state.get_or_create_slot(name)
    slot._titled = True
    slot.append("user", "kick off the long turn")
    slot.drain()
    _save_slot_to_history(state, slot, closed=False)
    return slot


def _hold_note(slot, content: str = "held while running") -> dict:
    note = {
        "content": content,
        "cls": "reconcile-note",
        "context": {
            "content": content,
            "source": "note",
            "ephemeral": True,
            "injectedAt": 1_000_000.0,
        },
        "session": effective_session_key(slot),
    }
    slot._deferred_notes.append(note)
    return note


def _meta(state: DashboardState, slot) -> dict:
    return state.conversation_log._read_metadata(slot_history_key(slot))


def _persist(state: DashboardState, slot, ensure: dict | None = None):
    """Call the writer the way the production caller does: both parameters are
    required (the ensure pin and the authorized-key pin are load-bearing), so
    tests that only exercise the merge pass the last-held note — or an id-less
    stand-in, which the resolver and pin both skip — and the slot's own key."""
    if ensure is None:
        ensure = (
            slot._deferred_notes[-1]
            if slot._deferred_notes
            else {"content": "stand-in", "cls": "reconcile-note", "context": None}
        )
    return persist_deferred_notes_sync(state.conversation_log, slot, ensure, slot_history_key(slot))


class TestEnqueueDurability:
    """Gate (c): the 200 acknowledges a hold that is already on disk."""

    @asynccontextmanager
    async def _make_client(self, state: DashboardState):
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/note", api_chat_slot_note)
        async with TestClient(TestServer(app)) as c:
            yield c

    @pytest.mark.asyncio
    async def test_200_means_the_hold_is_already_durable(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "s1")
        slot.task = asyncio.get_running_loop().create_future()
        try:
            async with self._make_client(state) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/note", json={"content": "note during turn"}
                )
                assert resp.status == 200
                assert (await resp.json())["visibleDeferred"] is True

            persisted = _meta(state, slot).get("deferred_notes")
            assert isinstance(persisted, list) and len(persisted) == 1
            assert persisted[0]["content"] == "note during turn"
            assert persisted[0]["session"] == effective_session_key(slot)
            assert persisted[0]["context"] is not None
            assert persisted[0]["id"], "the durable entry must carry the merge identity"
        finally:
            slot.task = None

    @pytest.mark.asyncio
    async def test_failed_durable_write_rolls_back_and_answers_503(
        self, tmp_path: Path, monkeypatch
    ):
        """No 200 may promise durability the write did not deliver."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "s2")
        slot.task = asyncio.get_running_loop().create_future()

        def _boom(conversation_log, s, ensure, authorized_history_key):
            raise OSError("disk full")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.persist_deferred_notes_sync", _boom)
        try:
            async with self._make_client(state) as client:
                resp = await client.post("/api/chat/slots/s2/note", json={"content": "x"})
                assert resp.status == 503
                assert (await resp.json())["code"] == "deferred_note_persist_failed"

            assert slot._deferred_notes == [], "the unpersisted hold must be rolled back"
            assert not _meta(state, slot).get("deferred_notes")
        finally:
            slot.task = None

    @pytest.mark.asyncio
    async def test_rollback_removes_the_failed_note_by_identity(self, tmp_path: Path, monkeypatch):
        """Two same-content notes from a capped source are byte-identical dicts
        (context None). An equality rollback would evict the sibling that
        already holds a durable 200; identity rollback removes THIS note."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "s2b")
        session = effective_session_key(slot)
        earlier = {"content": "same", "cls": "reconcile-note", "context": None, "session": session}
        failing = {"content": "same", "cls": "reconcile-note", "context": None, "session": session}
        assert earlier == failing and earlier is not failing
        slot._deferred_notes[:] = [earlier, failing]

        def _boom(conversation_log, s):
            raise OSError("disk full")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.persist_deferred_notes_sync", _boom)
        resp = await _persist_deferred_note_hold(state, slot, failing, slot_history_key(slot))
        assert resp is not None and resp.status == 503
        assert len(slot._deferred_notes) == 1
        assert (
            slot._deferred_notes[0] is earlier
        ), "the sibling note with a durable 200 must survive the rollback"

    @pytest.mark.asyncio
    async def test_rebind_during_persist_refuses_the_foreign_write(
        self, tmp_path: Path, monkeypatch
    ):
        """The durable write is pinned to the transcript authorized at
        enqueue: a cron/workflow rebind landing in the persist window must be
        refused (uniform not-found shape) with the note rolled back — never
        committed into the foreign transcript's metadata."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "rb1")
        authorized_key = slot_history_key(slot)
        note = {
            "id": "rebound00001",
            "content": "authorized before the rebind",
            "cls": "reconcile-note",
            "context": None,
            "session": effective_session_key(slot),
        }
        slot._deferred_notes.append(note)
        # The rebind lands before the worker writes (a cron claiming the
        # unbound linked_session_key) — the slot now resolves elsewhere.
        slot.linked_session_key = "cron:job-42"
        foreign_key = slot_history_key(slot)
        assert foreign_key != authorized_key

        resp = await _persist_deferred_note_hold(state, slot, note, authorized_key)
        assert resp is not None and resp.status == 404
        assert slot._deferred_notes == [], "the refused note must be rolled back"
        assert not state.conversation_log._read_metadata(authorized_key).get("deferred_notes")
        assert not state.conversation_log._read_metadata(foreign_key).get(
            "deferred_notes"
        ), "nothing may be written into the transcript the rebind installed"

    @pytest.mark.asyncio
    async def test_rebind_dropped_note_is_refused_not_acknowledged(
        self, tmp_path: Path, monkeypatch
    ):
        """A note the turn-end flush DROPS at the rebind seam leaves the hold
        exactly like a delivered one — absent. Reading that absence as
        "delivered" returned a 200 for a note that was never delivered and has
        no durable copy (no recovery path: the caller will not retry). With
        the positive-evidence rule the rebind branch answers its uniform 404:
        no live row, no durable entry, no committed row."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "rbdrop")
        authorized_key = slot_history_key(slot)
        note = {
            "id": "rebounddrop1",
            "content": "dropped at the seam, never delivered",
            "cls": "reconcile-note",
            "context": None,
            "session": effective_session_key(slot),
        }
        # The concurrent turn-end flush hit the rebind seam: it drained the
        # hold, delivered nothing, and recorded the drop.
        slot._dropped_note_ids.add("rebounddrop1")
        assert slot._deferred_notes == []
        # The rebind itself lands before this worker's locked write.
        slot.linked_session_key = "cron:job-99"
        assert slot_history_key(slot) != authorized_key

        resp = await _persist_deferred_note_hold(state, slot, note, authorized_key)
        assert resp is not None, "a dropped note must not be acknowledged with a 200"
        assert resp.status == 404
        assert not state.conversation_log._read_metadata(authorized_key).get("deferred_notes")

    @pytest.mark.asyncio
    async def test_flush_drop_with_written_merge_is_refused(self, tmp_path: Path, monkeypatch):
        """The success-path twin of the rebind drop: for a channel-origin slot
        ``slot_history_key`` and ``effective_session_key`` diverge, so the
        flush can drop the note at ITS seam while the persist guard's key
        check still passes and the merge commits. The written merge then
        carries NO representation of the note (the ensure pin skips dropped
        ids), so ``written=True`` alone is not evidence — the endpoint must
        refuse rather than acknowledge a note nothing will deliver or
        replay."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "twindrop")
        key = slot_history_key(slot)
        note = {
            "id": "twindropid01",
            "content": "dropped by the flush, key still matches",
            "cls": "reconcile-note",
            "context": None,
            "session": "app:some-other-session",
        }
        # The flush dropped it (session mismatch at the flush seam) while the
        # slot's HISTORY key never changed — the persist guard sees no rebind.
        slot._dropped_note_ids.add("twindropid01")
        assert slot._deferred_notes == []
        assert slot_history_key(slot) == key

        resp = await _persist_deferred_note_hold(state, slot, note, key)
        assert resp is not None, "written-without-the-note must not read as durable"
        assert resp.status == 404
        persisted = _meta(state, slot).get("deferred_notes") or []
        assert all(
            entry.get("id") != "twindropid01" for entry in persisted
        ), "the dropped note must not be resurrected into the durable hold"

    @pytest.mark.asyncio
    async def test_delivered_live_row_keeps_the_200_on_persist_failure(
        self, tmp_path: Path, monkeypatch
    ):
        """Evidence clause (a): the flush DELIVERED the note — its row is in
        the slot's live message list, stamped ``meta.noteId`` — so the 200
        stands even when this writer's own durable write fails. An error
        answer would make the caller re-post a line the user already saw."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "liverow")
        note = {
            "id": "liverowid001",
            "content": "delivered mid-persist",
            "cls": "reconcile-note",
            "context": None,
            "session": effective_session_key(slot),
        }
        slot.messages.append(
            {
                "role": "inject",
                "content": note["content"],
                "cls": "reconcile-note",
                "meta": {"noteSession": effective_session_key(slot), "noteId": "liverowid001"},
            }
        )

        def _boom(conversation_log, s, ensure, authorized_history_key):
            raise OSError("write failed after the flush delivered")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.persist_deferred_notes_sync", _boom)
        resp = await _persist_deferred_note_hold(state, slot, note, slot_history_key(slot))
        assert resp is None, "a delivered note keeps its 200 on positive evidence"

    @pytest.mark.asyncio
    async def test_hold_full_after_a_racing_delivery_keeps_the_200(
        self, tmp_path: Path, monkeypatch
    ):
        """When DeferredHoldFull races a turn-end flush that already delivered
        the note, a 429 would make the caller re-post a line the user already
        saw. The rollback finding nothing means delivered — the 200 stands,
        same as the generic failure branch."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "hf2")
        note = {
            "id": "deliveredrace",
            "content": "drained by the flush mid-persist",
            "cls": "reconcile-note",
            "context": None,
            "session": effective_session_key(slot),
        }

        def _full(conversation_log, s, ensure, authorized_history_key):
            raise DeferredHoldFull("ceiling")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.persist_deferred_notes_sync", _full)
        # The racing flush drained AND DELIVERED the note: its row is in the
        # slot's live message list, stamped with the note id — the positive
        # evidence (clause a) the 200 stands on. Mere absence from the hold is
        # NOT evidence of delivery (a rebind-seam drop looks identical there).
        assert slot._deferred_notes == []
        slot.messages.append(
            {
                "role": "inject",
                "content": note["content"],
                "cls": "reconcile-note",
                "meta": {"noteSession": effective_session_key(slot), "noteId": note["id"]},
            }
        )
        resp = await _persist_deferred_note_hold(state, slot, note, slot_history_key(slot))
        assert resp is None, "a delivered note must keep its 200 — a 429 would duplicate it"

    def test_unreadable_record_raises_instead_of_reading_as_no_identity(
        self, tmp_path: Path, monkeypatch
    ):
        """``update_metadata_if`` returns False both for 'no metadata line' and
        for 'record unreadable' — but for the latter the guard never runs, the
        slot's file exists, and its tab WILL come back after a restart. That
        case must surface as a failure (→ 503 upstream), not as a durable 200
        with nothing behind it."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "s2c")
        _hold_note(slot)

        def _unreadable(key, fields, guard):
            return False  # guard never invoked, mirroring the unreadable branch

        monkeypatch.setattr(state.conversation_log, "update_metadata_if", _unreadable)
        with pytest.raises(RuntimeError, match="unreadable"):
            _persist(state, slot)

    @pytest.mark.asyncio
    async def test_slot_without_a_durable_identity_still_accepts(self, tmp_path: Path, monkeypatch):
        """No metadata line on disk => the slot itself would not survive a
        restart, so there is no durable promise to keep. The note is held in
        memory and the 200 keeps its this-lifetime meaning."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s3")  # never saved: no metadata line
        slot.task = asyncio.get_running_loop().create_future()
        try:
            async with self._make_client(state) as client:
                resp = await client.post("/api/chat/slots/s3/note", json={"content": "x"})
                assert resp.status == 200
            assert len(slot._deferred_notes) == 1
            assert not _meta(state, slot), "no metadata line may be upserted for the hold"
        finally:
            slot.task = None

    @pytest.mark.asyncio
    async def test_oversized_deferred_note_is_rejected_before_the_200(
        self, tmp_path: Path, monkeypatch
    ):
        """The durable copy is persisted verbatim, so the size bound lives at
        the enqueue boundary where the caller can act on it — never as a
        truncation that would replay altered content for an acknowledged
        note. Immediate (non-held) notes keep the larger shared bound."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "s5")
        slot.task = asyncio.get_running_loop().create_future()
        try:
            async with self._make_client(state) as client:
                resp = await client.post(
                    "/api/chat/slots/s5/note",
                    json={"content": "x" * (MAX_DEFERRED_NOTE_CHARS + 1)},
                )
                assert resp.status == 413
                assert (await resp.json())["code"] == "deferred_note_too_large"
            assert slot._deferred_notes == []
            assert not _meta(state, slot).get("deferred_notes")
        finally:
            slot.task = None

    @pytest.mark.asyncio
    async def test_failed_write_keeps_the_200_when_a_sibling_already_persisted(
        self, tmp_path: Path, monkeypatch
    ):
        """The merge writers commit the WHOLE live list, so a concurrent
        sibling POST can persist THIS note durably even though this writer's
        own attempt failed. A 503 then would orphan that durable copy: the
        caller re-posts, and the restore replays the original — duplicate.
        When the note is already on disk, the 200 stands and nothing is
        rolled back."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "sib1")
        session = effective_session_key(slot)
        key = slot_history_key(slot)
        note = {
            "id": "siblingwrote",
            "content": "persisted by the concurrent sibling",
            "cls": "reconcile-note",
            "context": None,
            "session": session,
        }
        slot._deferred_notes.append(note)
        # The sibling's merge writer already committed the whole live list.
        state.conversation_log.update_metadata(
            key, {"deferred_notes": serialize_deferred_notes([note])}
        )

        def _boom(conversation_log, s, ensure, authorized_history_key):
            raise OSError("this writer's own attempt fails")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.persist_deferred_notes_sync", _boom)
        resp = await _persist_deferred_note_hold(state, slot, note, key)
        assert resp is None, "an already-durable note must keep its 200"
        assert slot._deferred_notes == [note], "the still-undelivered note stays held"
        assert _meta(state, slot)["deferred_notes"][0]["id"] == "siblingwrote"

    @pytest.mark.asyncio
    async def test_delete_winning_the_lock_is_refused_not_acknowledged(
        self, tmp_path: Path, monkeypatch
    ):
        """A slot whose metadata file existed when the write began but is gone
        under the lock lost a race with a permanent delete. The persist's
        ``False`` (no metadata line) must NOT fall through to the 200 the
        never-persisted case gets: the session and any durable copy are gone,
        so the endpoint answers its uniform not-found shape instead."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "delwon")
        key = slot_history_key(slot)
        note = _hold_note(slot, "racing a permanent delete")
        note["id"] = "deletewonrace"

        def _delete_wins(conversation_log, s, ensure, authorized_history_key):
            # The permanent delete lands between the durable-identity probe
            # and the locked guard: the guard sees no metadata line.
            state.conversation_log._path(key).unlink()
            return DeferredHoldOutcome(written=False, evidence=NoteEvidence(False, False))

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.persist_deferred_notes_sync", _delete_wins
        )
        resp = await _persist_deferred_note_hold(state, slot, note, key)
        assert resp is not None, "delete-won must not be acknowledged with a 200"
        assert resp.status == 404
        assert note not in slot._deferred_notes, "the refused note is rolled back"

    @pytest.mark.asyncio
    async def test_rebind_refusal_yields_to_a_sibling_durable_copy(
        self, tmp_path: Path, monkeypatch
    ):
        """A sibling's merge writer can persist this note into the AUTHORIZED
        transcript before the rebind is detected. The durable entry is real
        and will replay after a restart, so the rebind's 404 would invite the
        caller to re-post a duplicate — the 200 stands and nothing is rolled
        back."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "rebsib")
        key = slot_history_key(slot)
        note = _hold_note(slot, "persisted before the rebind was seen")
        note["id"] = "rebindsibling"
        state.conversation_log.update_metadata(
            key, {"deferred_notes": serialize_deferred_notes([note])}
        )

        def _rebound(conversation_log, s, ensure, authorized_history_key):
            # What the real locked resolver reports for this scenario: the
            # sibling's merge writer already put the entry on disk.
            raise DeferredHoldRebound(
                "slot rebound mid-persist", evidence=NoteEvidence(durable=True, committed=False)
            )

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.persist_deferred_notes_sync", _rebound
        )
        resp = await _persist_deferred_note_hold(state, slot, note, key)
        assert resp is None, "a sibling-durable note keeps its 200 across a rebind refusal"
        assert slot._deferred_notes[-1] is note, "no rollback of the held note"
        assert _meta(state, slot)["deferred_notes"][0]["id"] == "rebindsibling"

    @pytest.mark.asyncio
    async def test_redaction_growth_cannot_smuggle_an_over_bound_hold(
        self, tmp_path: Path, monkeypatch
    ):
        """The 413 bound must hold on the PERSISTED string: redaction can grow
        content (a flagged URL becomes a longer [REDACTED: ...] tag), and a
        persisted entry over the bound is dropped fail-closed by the restore
        sanitizer — a 200 would be an acknowledgement the restart silently
        breaks."""
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        # Build content whose RAW length is under the bound but whose
        # redacted form is over it, computed against the real redactors so
        # the test tracks their behavior instead of hardcoding tag widths.
        unit = "http://a.co/?AccessKeyId=v "
        content = ""
        while len(content) + len(unit) <= MAX_DEFERRED_NOTE_CHARS:
            content += unit
        redacted, _ = redact_exfiltration_urls(content)
        redacted, _ = redact_credentials(redacted)
        if len(redacted) <= MAX_DEFERRED_NOTE_CHARS:
            pytest.skip("redactors no longer grow this input; bound cannot be smuggled")
        assert len(content) <= MAX_DEFERRED_NOTE_CHARS

        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "rg1")
        slot.task = asyncio.get_running_loop().create_future()
        try:
            async with self._make_client(state) as client:
                resp = await client.post("/api/chat/slots/rg1/note", json={"content": content})
                assert resp.status == 413
                assert (await resp.json())["code"] == "deferred_note_too_large"
            assert slot._deferred_notes == []
            assert not _meta(state, slot).get("deferred_notes")
        finally:
            slot.task = None

    @pytest.mark.asyncio
    async def test_memory_only_state_keeps_prior_semantics(self, tmp_path: Path, monkeypatch):
        """With no conversation log at all, nothing survives a restart — the
        hold stays in memory and the endpoint neither writes nor fails."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.conversation_log = None
        slot = state.get_or_create_slot("s4")
        slot.task = asyncio.get_running_loop().create_future()
        try:
            async with self._make_client(state) as client:
                resp = await client.post("/api/chat/slots/s4/note", json={"content": "x"})
                assert resp.status == 200
            assert len(slot._deferred_notes) == 1
        finally:
            slot.task = None


class TestRestartRoundTrip:
    """Gates (a) and (b): survive one restart, deliver once, retire via the save."""

    def test_note_survives_restart_and_first_flush_delivers_exactly_once(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "rt1")
        _hold_note(slot, "survives the restart")
        assert _persist(state, slot).written is True

        # The restart: the slot is gone from memory and comes back off disk.
        del state._slots["rt1"]
        restored = _rehydrate_slot_from_history(state, "rt1")
        assert restored is not None
        assert len(restored._deferred_notes) == 1
        assert restored._deferred_notes[0]["content"] == "survives the restart"
        assert restored._deferred_notes[0]["session"] == effective_session_key(restored)

        # First flush after the restart delivers exactly one copy.
        restored._titled = True
        assert restored.flush_deferred_notes() == 1
        injected = [m for m in restored.messages if m.get("role") == "inject"]
        assert len(injected) == 1
        assert injected[0]["content"] == "survives the restart"
        assert restored._deferred_notes == []

        # Gate (b): the flush does NOT clear the durable copy — the delivered
        # row is still only in the in-memory window, and clearing now would
        # open a crash window that loses the acknowledged note outright.
        assert _meta(state, restored).get(
            "deferred_notes"
        ), "the durable hold must outlive the flush until the rows are saved"

        # The save that commits the delivered rows retires the hold in the
        # same atomic file write (the key is slot-owned, cleared by absence).
        restored.drain()
        _save_slot_to_history(state, restored, closed=False)
        assert "deferred_notes" not in _meta(state, restored)

        # Second restart: nothing to re-deliver, and the row is on disk.
        del state._slots["rt1"]
        again = _rehydrate_slot_from_history(state, "rt1")
        assert again is not None
        assert again._deferred_notes == []
        assert any(
            m.get("role") == "inject" and m.get("content") == "survives the restart"
            for m in again.messages
        )

    def test_crash_between_flush_and_save_redelivers_instead_of_losing(
        self, tmp_path: Path, monkeypatch
    ):
        """The failure direction is at-least-once: a restart that catches the
        gateway after the flush but before the row save must re-deliver the
        note (the delivered row died with the in-memory window), never lose
        it. This is exactly why the flush cannot clear the durable copy."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "rt2")
        _hold_note(slot, "must not vanish")
        assert _persist(state, slot).written is True

        del state._slots["rt2"]
        restored = _rehydrate_slot_from_history(state, "rt2")
        assert restored is not None
        restored._titled = True
        assert restored.flush_deferred_notes() == 1
        # No save happens: the "crash". The window (with the delivered row)
        # dies here; the durable hold on disk is what survives.

        del state._slots["rt2"]
        again = _rehydrate_slot_from_history(state, "rt2")
        assert again is not None
        assert [n["content"] for n in again._deferred_notes] == ["must not vanish"]

    def test_dropped_notes_redrop_on_replay_and_a_save_retires_them(
        self, tmp_path: Path, monkeypatch
    ):
        """The flush never writes the durable hold — not even for dropped
        notes. A dropped entry retained on disk is harmless: the restore
        replays it, the first flush re-drops it at the same rebind seam (its
        persisted session stamp still mismatches), and the next full save
        retires it. What must never happen is a metadata clear racing ahead
        of a row-committing save."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "rt3")
        slot._deferred_notes.append(
            {
                "id": "feedcafe0001",
                "content": "authorized elsewhere",
                "cls": "reconcile-note",
                "context": None,
                "session": "dashboard:someone-else",
            }
        )
        assert _persist(state, slot).written is True
        assert _meta(state, slot).get("deferred_notes")

        # Drop at the rebind seam: memory drains, the disk copy stays.
        assert slot.flush_deferred_notes() == 0
        assert slot._deferred_notes == []
        assert _meta(state, slot).get(
            "deferred_notes"
        ), "the flush must not write metadata, even for a fully-dropped hold"

        # Replay after a restart re-drops rather than delivering.
        del state._slots["rt3"]
        restored = _rehydrate_slot_from_history(state, "rt3")
        assert restored is not None
        assert len(restored._deferred_notes) == 1
        restored._titled = True
        assert restored.flush_deferred_notes() == 0
        assert not any(m.get("role") == "inject" for m in restored.messages)

        # The save retires the dropped entry (the live hold is empty).
        restored.append("user", "next turn")
        restored.drain()
        _save_slot_to_history(state, restored, closed=False)
        assert "deferred_notes" not in _meta(state, restored)

    def test_enqueue_persist_retains_delivered_but_unsaved_entries(
        self, tmp_path: Path, monkeypatch
    ):
        """The durable hold may only SHRINK via a row-committing save. A
        concurrent enqueue's persist runs after a flush delivered note A into
        the still-unsaved window; mirroring live state would erase A from the
        one durable place it exists, and a crash would lose a 200-acknowledged
        note. The merge must retain A's disk entry alongside the new note."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "rt5")
        session = effective_session_key(slot)
        slot._deferred_notes.append(
            {
                "id": "aaaa00000001",
                "content": "delivered but unsaved",
                "cls": "reconcile-note",
                "context": None,
                "session": session,
            }
        )
        assert _persist(state, slot).written is True

        # The turn-end flush delivers A into the in-memory window; no save yet.
        slot._titled = True
        assert slot.flush_deferred_notes() == 1

        # A new note lands and persists while A's row is still unsaved.
        slot._deferred_notes.append(
            {
                "id": "bbbb00000002",
                "content": "the racing enqueue",
                "cls": "reconcile-note",
                "context": None,
                "session": session,
            }
        )
        assert _persist(state, slot).written is True

        persisted = _meta(state, slot)["deferred_notes"]
        ids = [entry["id"] for entry in persisted]
        assert "aaaa00000001" in ids, "the merge must retain the delivered-but-unsaved entry"
        assert "bbbb00000002" in ids
        # Order: retained (older) entries first — the replay delivery order.
        assert ids.index("aaaa00000001") < ids.index("bbbb00000002")

        # The save that commits A's row retires A and keeps the live hold B.
        slot.drain()
        _save_slot_to_history(state, slot, closed=False)
        persisted = _meta(state, slot)["deferred_notes"]
        assert [entry["id"] for entry in persisted] == ["bbbb00000002"]

    def test_save_keeps_an_entry_whose_row_it_does_not_write(self, tmp_path: Path, monkeypatch):
        """Retirement is ROW-DERIVED: a /note persist that lands after the
        save's window snapshot (e.g. winning the history lock during the
        save's patient acquire) must survive that save — the save reads the
        on-disk and live holds under the lock and retires ONLY entries whose
        delivered rows are in the window it writes."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "rd1")
        session = effective_session_key(slot)
        slot._deferred_notes.append(
            {
                "id": "aaaa0000000a",
                "content": "delivered, row in this save's window",
                "cls": "reconcile-note",
                "context": None,
                "session": session,
            }
        )
        assert _persist(state, slot).written is True
        slot._titled = True
        assert slot.flush_deferred_notes() == 1  # A's row is now in the window

        # B lands durably AFTER the flush — the racing enqueue: on disk (and
        # held live), its row nowhere.
        slot._deferred_notes.append(
            {
                "id": "bbbb0000000b",
                "content": "persisted mid-save, no row yet",
                "cls": "reconcile-note",
                "context": None,
                "session": session,
            }
        )
        assert _persist(state, slot).written is True

        slot.drain()
        _save_slot_to_history(state, slot, closed=False)
        persisted = _meta(state, slot).get("deferred_notes")
        assert persisted is not None
        assert [entry["id"] for entry in persisted] == ["bbbb0000000b"], (
            "the save must retire exactly the entry whose row it wrote (A) "
            "and keep the one whose row does not exist yet (B)"
        )
        del state._slots["rd1"]
        restored = _rehydrate_slot_from_history(state, "rd1")
        assert restored is not None
        assert [n["id"] for n in restored._deferred_notes] == ["bbbb0000000b"]

    def test_drop_records_survive_a_failed_save(self, tmp_path: Path, monkeypatch):
        """A dropped note's row never exists, so its recorded id is the ONLY
        retirement path. The save must consume the record only AFTER its
        atomic write commits — consumed before, a failed write would leak the
        entry into the durable hold forever."""
        import kiro_crew.dashboard.chat_persistence as cp

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "dr1")
        slot._deferred_notes.append(
            {
                "id": "droppedid001",
                "content": "authorized elsewhere",
                "cls": "reconcile-note",
                "context": None,
                "session": "dashboard:someone-else",
            }
        )
        assert _persist(state, slot).written is True
        assert slot.flush_deferred_notes() == 0  # dropped at the rebind seam
        assert slot._dropped_note_ids == {"droppedid001"}

        real_atomic_write = cp.atomic_write

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(cp, "atomic_write", _boom)
        slot.append("user", "next turn")
        slot.drain()
        with pytest.raises(OSError):
            _save_slot_to_history(state, slot, closed=False)
        assert slot._dropped_note_ids == {
            "droppedid001"
        }, "a failed write must not consume the retirement record"
        assert _meta(state, slot).get("deferred_notes"), "the entry is still on disk"

        monkeypatch.setattr(cp, "atomic_write", real_atomic_write)
        _save_slot_to_history(state, slot, closed=False)
        assert slot._dropped_note_ids == set()
        assert "deferred_notes" not in _meta(state, slot)

    def test_full_save_write_through_and_clear_by_absence(self, tmp_path: Path, monkeypatch):
        """``deferred_notes`` is slot-owned: the full save writes the live hold
        and, once the hold is empty, clears the on-disk copy by absence."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "rt4")
        _hold_note(slot, "written by the full save")
        slot.append("assistant", "still running")
        slot.drain()
        _save_slot_to_history(state, slot, closed=False)
        assert _meta(state, slot)["deferred_notes"][0]["content"] == "written by the full save"

        slot._deferred_notes.clear()
        slot.append("assistant", "turn finished")
        slot.drain()
        _save_slot_to_history(state, slot, closed=False)
        assert "deferred_notes" not in _meta(state, slot), (
            "an owned key must clear by absence, or a restart re-delivers a "
            "note the user already saw"
        )


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
        ceiling = 2 * MAX_DEFERRED_NOTES
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
        """A durable hold at the 2x ceiling (10 delivered-but-unsaved retained
        + 10 live) is a documented, supported state. A restore must replay ALL
        of it: the newest half are undelivered 200-acknowledged notes whose
        callers were told not to re-post, so capping the restore at the live
        cap would silently discard exactly those."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "tb2")
        session = effective_session_key(slot)
        ceiling = 2 * MAX_DEFERRED_NOTES
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
        # Fill the durable hold to the 2x ceiling with retained entries.
        retained = [
            {"id": f"ret{i:09d}", "content": f"r{i}", "cls": "reconcile-note", "session": session}
            for i in range(2 * MAX_DEFERRED_NOTES)
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
        assert len(persisted) == 2 * MAX_DEFERRED_NOTES, "no retained entry may be evicted"
        assert all(entry["id"].startswith("ret") for entry in persisted)

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
