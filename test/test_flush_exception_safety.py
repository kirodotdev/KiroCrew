"""Exception safety of the deferred-note flush, at every seam that calls it.

``slot.flush_deferred_notes()`` is called at five seams. Every one of them is a
bare statement whose *following* code is what frees the slot, so a raise inside
the flush does not merely delay a held note -- it skips that cleanup:

* ``_start_next_queued_turn`` -- the successor turn is never dispatched.
* ``_finish_queue_cycle`` -- ``append("done")`` / ``chat_done`` never run, so
  ``slot.task`` stays non-None and the UI spinner never clears.
* ``_stage_loop``'s ``finally`` -- same wedge, and because the flush sits inside
  a ``finally`` a raise there also replaces any in-flight exception.
* the bulk-cleanup close path -- the slot has already been ``pop``ed from
  ``state._slots`` and the archive save below is skipped, so the transcript is
  lost outright.
* ``close_slot`` -- the tab close and session control's ``close_target``. This one
  did not flush AT ALL until it was added: the slot is popped above the save, so
  archiving without flushing wrote the transcript without the held note and still
  reported success.

Separately, ``flush_deferred_notes`` clears its backing list before writing, so
a raise part-way through the write loop loses every not-yet-written note.

The existing flush coverage (``test_gateway_appkit_endpoints.py``,
``test_chat_runner_coverage.py``) asserts flush *ordering* only -- no test
anywhere makes the flush, or a note write inside it, raise.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.state import _ChatSlot


class _Boom(RuntimeError):
    """Distinct type, so a test cannot pass on some unrelated failure."""


def _slot(key: str = "flush-guard-1") -> _ChatSlot:
    slot = _ChatSlot(key)
    # Titled on purpose: an untitled slot makes the end-of-turn cycle spawn
    # _maybe_auto_title, which is a real LLM path.
    slot._titled = True
    return slot


def _hold(slot: _ChatSlot, *texts: str, with_context: bool = True) -> None:
    """Put notes in the held queue the way the /note endpoint does."""
    for text in texts:
        slot._deferred_notes.append(
            {
                "content": text,
                "cls": "reconcile-note",
                "session": None,
                "context": {"content": text} if with_context else None,
            }
        )


def _raising_flush() -> MagicMock:
    return MagicMock(side_effect=_Boom("flush blew up"))


# ---------------------------------------------------------------------------
# Root cause: the write loop clears before it writes
# ---------------------------------------------------------------------------


class TestPartialFlushKeepsUnwrittenNotes:
    def test_a_raise_mid_write_does_not_lose_the_remaining_notes(self, tmp_path: Path):
        """Note 2 fails => notes 2 and 3 must still be held, not dropped.

        ``held`` is a local copy and the backing list is cleared before the
        loop, so without the fix the unwritten remainder is unreachable once
        the frame unwinds -- irrecoverable loss, independent of any caller.
        """
        slot = _slot()
        _hold(slot, "first", "second", "third")

        calls: list[str] = []
        real_append = type(slot).append

        def _append(self, role="", content="", cls="", **kw):
            calls.append(content)
            if content == "second":
                raise _Boom("append failed on note 2")
            return real_append(self, role=role, content=content, cls=cls, **kw)

        with patch.object(type(slot), "append", _append):
            with pytest.raises(_Boom):
                slot.flush_deferred_notes()

        # Note 1 was written and must not be replayed.
        assert calls == ["first", "second"]
        assert [m["content"] for m in slot.messages] == ["first"]
        # The unwritten remainder is what this test is about.
        assert [n["content"] for n in slot._deferred_notes] == ["second", "third"]

    def test_the_retained_notes_are_delivered_by_the_next_flush(self, tmp_path: Path):
        """Retention is only worth anything if a later flush drains them."""
        slot = _slot()
        _hold(slot, "first", "second", "third")

        real_append = type(slot).append

        def _append(self, role="", content="", cls="", **kw):
            if content == "second":
                raise _Boom("append failed on note 2")
            return real_append(self, role=role, content=content, cls=cls, **kw)

        with patch.object(type(slot), "append", _append):
            with pytest.raises(_Boom):
                slot.flush_deferred_notes()

        # Second attempt, nothing patched: the held remainder is written.
        assert slot.flush_deferred_notes() == 2
        assert [m["content"] for m in slot.messages] == ["first", "second", "third"]
        assert slot._deferred_notes == []

    def test_a_restored_note_does_not_re_promote_its_context_half(self, tmp_path: Path):
        """The context half is promoted before the visible line is appended.

        So a note whose ``append`` raised has ALREADY put its context on the
        pending queue. Restoring the note for a retry must not queue that
        context a second time, or the next turn reads the note twice.
        """
        slot = _slot()
        _hold(slot, "first", "second")

        real_append = type(slot).append

        def _append(self, role="", content="", cls="", **kw):
            if content == "second":
                raise _Boom("append failed after its context was promoted")
            return real_append(self, role=role, content=content, cls=cls, **kw)

        with patch.object(type(slot), "append", _append):
            with pytest.raises(_Boom):
                slot.flush_deferred_notes()

        assert [e["content"] for e in slot._pending_context] == ["first", "second"]
        slot.flush_deferred_notes()
        # "second" must appear exactly once, not twice.
        assert [e["content"] for e in slot._pending_context] == ["first", "second"]


# ---------------------------------------------------------------------------
# Seam 1 -- _start_next_queued_turn
# ---------------------------------------------------------------------------


class TestSeam1StartNextQueuedTurn:
    @pytest.mark.asyncio
    async def test_a_flush_raise_still_dispatches_the_successor_turn(self, tmp_path: Path):
        """The flush sits above the dequeue; everything below it is the handoff."""
        state = _make_state(tmp_path)
        slot = _slot()
        state._slots[slot.key] = slot
        slot._queue.append({"id": "q1", "content": "queued work"})

        with (
            patch.object(type(slot), "flush_deferred_notes", _raising_flush()),
            patch.object(chat_runner, "spawn_guarded_turn") as spawn,
            patch.object(chat_runner, "_run_chat", new=AsyncMock()),
        ):
            spawn.return_value = MagicMock(done=MagicMock(return_value=True))
            started = await chat_runner._start_next_queued_turn(state, slot)

        assert started is True, "the queued turn must still be dispatched"
        assert spawn.called, "spawn_guarded_turn must be reached"
        assert slot._queue == [], "the queue item must have been consumed"
        slot.task = None


# ---------------------------------------------------------------------------
# Seam 2 -- _finish_queue_cycle
# ---------------------------------------------------------------------------


class TestSeam2FinishQueueCycle:
    @pytest.mark.asyncio
    async def test_a_flush_raise_still_reaches_the_terminal_done(self, tmp_path: Path):
        """Below the flush is the only code that frees the slot for the UI."""
        state = _make_state(tmp_path)
        slot = _slot()
        state._slots[slot.key] = slot
        slot.task = asyncio.get_running_loop().create_future()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.refresh_slot_source_status = MagicMock()
        state.push_refresh = MagicMock()

        with (
            patch.object(type(slot), "flush_deferred_notes", _raising_flush()),
            # _finish_queue_cycle fire-and-forgets generate_session_summary, which
            # hands KiroCrewConfig.load to asyncio.to_thread -- a thread that would
            # outlive this test and read the operator's real config after teardown
            # has restored the data-home environment.
            patch.object(chat_runner, "generate_session_summary", new=AsyncMock()),
        ):
            chat_runner._finish_queue_cycle(state, slot)
            await asyncio.sleep(0)

        assert any(m.get("role") == "done" for m in slot.messages), "no done row => wedge"
        assert slot.task is None, "slot.task left set => the UI spinner never clears"
        assert any(
            c.args and c.args[0] == "chat_done" for c in state.broadcast_ws.call_args_list
        ), "chat_done never broadcast => wedge"


# ---------------------------------------------------------------------------
# Seam 3 -- _stage_loop's finally
# ---------------------------------------------------------------------------


class TestSeam3StageLoopFinally:
    @pytest.mark.asyncio
    async def test_a_flush_raise_in_the_finally_still_closes_the_slot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A raise inside a ``finally`` skips the rest of it AND masks any
        in-flight exception, so this is the worst-placed of the three."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _stage_loop

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])

        slot = _ChatSlot("stage-flush-guard", mode="orchestrator")
        slot._titled = True
        slot._stage_titles = ["A"]
        slot._orch_tracker = None

        async def _noop(s, sl, msg, **kw):
            return None

        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _noop)

        with patch.object(type(slot), "flush_deferred_notes", _raising_flush()):
            await _stage_loop(state, slot, auto_run=True)

        assert slot._in_stage_execution is False
        assert any(m.get("role") == "done" for m in slot.messages), "no done row => wedge"
        assert slot.task is None, "slot.task left set => the slot is wedged"
        assert any(
            c.args and c.args[0] == "chat_done" for c in state.broadcast_ws.call_args_list
        ), "chat_done never broadcast => wedge"


# ---------------------------------------------------------------------------
# Seam 4 -- bulk-cleanup close path
# ---------------------------------------------------------------------------


class TestSeam4BulkCleanup:
    @staticmethod
    def _stale(state, key: str) -> _ChatSlot:
        old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        slot = state.get_or_create_slot(key)
        slot.append("user", "old msg", ts=old_ts)
        slot.drain()
        return slot

    @pytest.mark.asyncio
    async def test_a_flush_raise_restores_and_reports_the_popped_slot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The slot is popped from the registry ABOVE the flush, and
        ``_deferred_notes`` is in-memory only -- a ``__slots__`` attribute the
        persistence layer never reads. So archiving anyway would write the
        transcript WITHOUT the held note, discard the slot, and still report the
        key in ``archived``: data loss reported as success. The flush shares the
        archive-save's ``except`` arm instead, so the slot comes back with its
        note still held and the key is reported in ``failed``."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = self._stale(state, "stale-flush")
        _hold(slot, "held note")

        with patch.object(_ChatSlot, "flush_deferred_notes", _raising_flush()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/cleanup", json={"max_inactive_days": 1})
                data = await resp.json()

        assert data["ok"] is True, "the raise must not escape the handler"
        assert data["archived"] == 0 and data["keys"] == []
        assert data["failed"] == ["stale-flush"], "the failure must be reported, not swallowed"
        assert "stale-flush" in state._slots, "the popped slot must be put back"
        held = [n["content"] for n in state._slots["stale-flush"]._deferred_notes]
        assert held == ["held note"], "the held note must survive for a later flush"

    @pytest.mark.asyncio
    async def test_a_flush_raise_does_not_abort_the_rest_of_the_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The flush is inside the ``for name in stale_keys`` loop, so an
        unguarded raise also abandons every slot after the failing one."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        for key in ("stale-a", "stale-b", "stale-c"):
            self._stale(state, key)

        with patch.object(_ChatSlot, "flush_deferred_notes", _raising_flush()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/cleanup", json={"max_inactive_days": 1})
                data = await resp.json()

        assert data["archived"] == 0
        assert sorted(data["failed"]) == [
            "stale-a",
            "stale-b",
            "stale-c",
        ], "every stale slot must be reached and reported, not abandoned at the first raise"
        assert sorted(state._slots) == ["stale-a", "stale-b", "stale-c"]


class TestSeam5CloseSlot:
    """``close_slot`` — the tab ✕ and session control's ``close_target``.

    This seam did NOT flush at all until it was added, while the bulk-cleanup path
    above did, with a documented rationale that applies verbatim here: the slot is
    popped before the archive save, ``_deferred_notes`` is in-memory only, so
    saving without flushing writes the transcript WITHOUT the held note and still
    reports success. The frontend's ``appended === true`` gate does not cover it —
    a note the backend ACCEPTED and deferred is exactly what that gate lets
    through — and neither of the two production callers had any other guard.
    """

    @staticmethod
    async def _close(state, key: str) -> None:
        from kiro_crew.dashboard.chat_handlers import close_slot

        await close_slot(state, state._slots[key], key)

    @pytest.mark.asyncio
    async def test_a_held_note_is_flushed_before_the_archive_save(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("close-flush-1")
        slot._titled = True
        _hold(slot, "held note")

        order: list[str] = []
        real_flush = _ChatSlot.flush_deferred_notes

        def tracking_flush(self):
            order.append("flush")
            return real_flush(self)

        async def tracking_save(*_a, **_kw):
            order.append("save")

        with (
            patch.object(_ChatSlot, "flush_deferred_notes", tracking_flush),
            patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", tracking_save),
        ):
            await self._close(state, "close-flush-1")

        assert order == ["flush", "save"], (
            "the held note must be flushed BEFORE the archive save, or the transcript "
            f"is written without it (saw {order})"
        )

    @pytest.mark.asyncio
    async def test_a_flush_raise_restores_the_slot_and_keeps_the_note(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The flush must share the save's ``except`` arm, not sit above the try.

        Placed above it, a raise would leave the slot popped with its note still in
        an object nothing else references — the close reported as done, the note
        gone, and no way to retry. Sharing the arm puts the slot back instead.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("close-flush-2")
        slot._titled = True
        _hold(slot, "held note")

        with patch.object(_ChatSlot, "flush_deferred_notes", _raising_flush()):
            with pytest.raises(Exception):
                await self._close(state, "close-flush-2")

        assert "close-flush-2" in state._slots, "the popped slot must be put back"
        held = [n["content"] for n in state._slots["close-flush-2"]._deferred_notes]
        assert held == ["held note"], "the held note must survive for a later flush"


class TestHandoverDrainReportsALostNote:
    """A hand-over close reports a held note ONLY through the drain's return value.

    ``_persist_handover_tail`` is the whole of what both hand-over exits do with a held
    note: the slot is popped, a replacement owns the key, and the periodic flush only
    visits ``_slots``, so nothing retries. Each caller branches on that bool alone --
    ``close_slot`` raises ``SlotCloseError``, the bulk sweep files the key under
    ``failed`` -- so True after a failed flush is a close reported clean over a note
    that is already unreachable.
    """

    @pytest.mark.asyncio
    async def test_a_failed_flush_makes_the_drain_report_failure(self, tmp_path: Path):
        from kiro_crew.dashboard.chat_handlers import _persist_handover_tail

        slot = _slot()
        _hold(slot, "held note")
        state = _make_state(tmp_path)
        # Pin the no-rows-owed exit, which is where the swallowed failure was masked:
        # with rows owed the save's own answer could decide the result either way.
        assert len(slot.messages) - slot._disk_window_len == 0
        assert not slot._dirty

        with patch.object(_ChatSlot, "flush_deferred_notes", _raising_flush()):
            drained = await _persist_handover_tail(state, slot.key, slot)

        assert drained is False, "a note that could not be flushed is owed and undelivered"

    @pytest.mark.asyncio
    async def test_a_failed_flush_is_not_masked_by_a_committed_row_write(self, tmp_path: Path):
        """The second exit: rows reach disk, so only the flush decides the answer."""
        from kiro_crew.dashboard.chat_handlers import _persist_handover_tail

        slot = _slot()
        _hold(slot, "held note")
        slot.append(role="user", content="a row that never reached disk")
        state = _make_state(tmp_path)
        assert len(slot.messages) - slot._disk_window_len > 0

        with (
            patch.object(_ChatSlot, "flush_deferred_notes", _raising_flush()),
            patch(
                "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
                AsyncMock(return_value=True),
            ),
        ):
            drained = await _persist_handover_tail(state, slot.key, slot)

        assert drained is False

    @pytest.mark.asyncio
    async def test_control_a_clean_drain_still_reports_success(self, tmp_path: Path):
        """Without this the assertions above would pass for a drain that never returns True."""
        from kiro_crew.dashboard.chat_handlers import _persist_handover_tail

        slot = _slot()
        state = _make_state(tmp_path)

        drained = await _persist_handover_tail(state, slot.key, slot)

        assert drained is True
