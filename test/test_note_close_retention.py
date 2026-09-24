"""Tests for the close-time deferred-note retention fix.

A note held for a running turn lived only in ``_deferred_notes``, an in-memory
``__slots__`` attribute the persistence layer never reads, so closing the tab dropped it
while the DELETE still answered 200 -- data loss reported as success. ``close_slot`` now
flushes the hold inside the archive-save's ``try``, commits the visible row, and keeps the
note's context half as a ``contextOnly`` residual under a fresh id for a later
``adopt_closed`` rehydration.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.chat import api_chat_slot_note
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


@asynccontextmanager
async def _note_and_close_client(state: DashboardState):
    """The full chat app PLUS the /note route.

    ``_make_app`` registers DELETE /api/chat/slots/{slot} but not /note, so the close cases need
    both.
    """
    app = _make_app(state)
    app.router.add_post("/api/chat/slots/{slot}/note", api_chat_slot_note)
    async with TestClient(TestServer(app)) as c:
        yield c


def _slot(state: DashboardState, name: str = "s1") -> _ChatSlot:
    slot = _ChatSlot(name)
    state._slots[name] = slot
    return slot


async def _post_note(client: TestClient, content: object, slot: str = "s1", **fields: object):
    """POST the /note route for *slot* carrying *content* plus any extra JSON *fields*."""
    return await client.post(f"/api/chat/slots/{slot}/note", json={"content": content, **fields})


def _state_and_slot(tmp_path: Path, name: str = "s1") -> tuple[DashboardState, _ChatSlot]:
    """A fresh state rooted at *tmp_path* plus one registered slot -- the common arrange."""
    state = _make_state(tmp_path)
    return state, _slot(state, name)


def _disk_hits(root: Path, needle: str) -> list[str]:
    """Every file under *root* whose text contains *needle*.

    Reads the whole tree rather than a computed transcript path so an assertion about absence cannot
    pass merely because the path was guessed wrong.
    """
    hits: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if needle in text:
            hits.append(str(path))
    return hits


class TestDeferredNoteLostOnClose:
    """A held note SURVIVES its slot closing.

    The name describes the defect these tests bound, not a state of this tree: ``close_slot`` never
    flushed and ``_deferred_notes`` is an in-memory ``__slots__`` attribute the persistence layer
    never reads, so a popped slot took the held note with it while the DELETE still returned 200 --
    loss reported as success.

    ``close_slot`` now flushes inside the archive-save's ``try``, so a flush failure shares the
    restore arm -- the shape the bulk-cleanup path already used. Both callers are covered: the tab
    close and session control's ``close_target``.

    The client rule is UNCHANGED and still correct: a mid-turn note is still deferred and still
    answers ``appended: false``, so sequencing the close on ``appended === true`` still refuses to
    close on a merely held breadcrumb. The flush removes the data-loss consequence, not the rule.
    """

    @pytest.mark.asyncio
    async def test_CONTROL_immediate_note_survives_the_same_close(self, tmp_path: Path):
        """POSITIVE CONTROL for the defect test below.

        Without this, "absent from disk" would be a fact about the harness (a wrong search root, a
        save that never runs under a TestServer) not about the world. Same close, same probe, an
        IMMEDIATE note: it must reach both.
        """
        state, slot = _state_and_slot(tmp_path)
        assert slot.running is False  # no turn -> immediate path

        async with _note_and_close_client(state) as client:
            resp = await _post_note(client, "CONTROL-IMMEDIATE")
            assert (await resp.json())["appended"] is True
            assert len(slot.messages) == 1

            dresp = await client.delete("/api/chat/slots/s1")
            assert dresp.status == 200

        assert len(slot.messages) == 1
        assert slot.messages[0]["content"] == "CONTROL-IMMEDIATE"
        hits = _disk_hits(tmp_path, "CONTROL-IMMEDIATE")
        assert hits, "control failed: an immediate note did not reach disk either"

    @pytest.mark.asyncio
    async def test_a_deferred_note_now_SURVIVES_its_slot_closing(self, tmp_path: Path):
        """The defect this class was named for is FIXED: ``close_slot`` flushes.

        It pins the fix, and deliberately keeps the DEFER behaviour above unchanged: a note arriving
        mid-turn is still held and still answers ``appended: false``, so the client's refusal to
        close on a merely deferred breadcrumb stays correct.
        """
        state, slot = _state_and_slot(tmp_path)
        slot.task = asyncio.get_running_loop().create_future()
        assert slot.running is True

        async with _note_and_close_client(state) as client:
            resp = await _post_note(client, "BREADCRUMB-DEFERRED")
            data = await resp.json()
            # Unchanged: still deferred, still honestly reported as not appended.
            assert data["visibleDeferred"] is True
            assert data["appended"] is False
            assert len(slot._deferred_notes) == 1
            assert len(slot.messages) == 0

            dresp = await client.delete("/api/chat/slots/s1")
            assert dresp.status == 200

        # The ROW was committed, so the human's copy is not waiting on a reopen.
        assert [m["content"] for m in slot.messages] == [
            "BREADCRUMB-DEFERRED"
        ], "the held note did not reach the transcript"
        # ...and it reached disk. The control above proves this probe CAN find a note.
        assert _disk_hits(tmp_path, "BREADCRUMB-DEFERRED"), "flushed in memory but not saved"
        assert "s1" not in state._slots
        # What REMAINS held is the context half alone, and only as a durable residual:
        # queuing it would have destroyed it with the popped frame.
        assert [n.get("contextOnly") for n in slot._deferred_notes] == [True]
        assert slot._pending_context == []

    @pytest.mark.asyncio
    async def test_a_close_keeps_the_context_half_durable(self, tmp_path: Path):
        """The context half of a note held at close must reach DISK, not a dead queue.

        ``flush_deferred_notes`` promotes a context half into ``slot._pending_context``, which the
        persistence layer never serializes, and ``_close_slot`` pops the slot after the save. So a
        close that queued the half destroyed it, while the delivered row's ``meta.noteId`` retired
        the durable entry in the same write -- leaving nothing for an ``adopt_closed`` rehydration
        to replay. An ordinary restore skips a closed session outright, so the durable residual is
        the only channel that survives the close at all.

        Asserted on the SERIALIZED bytes rather than the live object, because it is the on-disk copy
        the rehydration reads.
        """
        state, slot = _state_and_slot(tmp_path)
        slot.task = asyncio.get_running_loop().create_future()

        async with _note_and_close_client(state) as client:
            resp = await _post_note(client, "CTX-HALF-MUST-SURVIVE")
            assert (await resp.json())["visibleDeferred"] is True
            assert isinstance(slot._deferred_notes[0]["context"], dict)

            assert (await client.delete("/api/chat/slots/s1")).status == 200

        hits = _disk_hits(tmp_path, "contextOnly")
        assert hits, "the context half was queued into volatile state, not persisted"
        on_disk = json.loads(
            next(
                line
                for line in Path(hits[0]).read_text(encoding="utf-8").splitlines()
                if "contextOnly" in line
            )
        )
        residuals = [n for n in on_disk["deferred_notes"] if n.get("contextOnly")]
        assert len(residuals) == 1, on_disk["deferred_notes"]
        assert residuals[0]["context"]["content"] == "CTX-HALF-MUST-SURVIVE"

    @pytest.mark.asyncio
    async def test_the_cancelled_turns_own_flush_cannot_drain_the_hold_first(self, tmp_path: Path):
        """Retention has to happen BEFORE the turn is cancelled.

        ``_close_slot`` pops the slot, then cancels its task. The cancelled turn runs its own
        teardown flush (``chat_runner._finish_queue_cycle``) and, with the slot already gone from
        the registry, takes the branch that hands a context half to the next turn -- promoting it
        into ``_pending_context``, which is never serialized and dies with this frame. A retention
        flush placed after the cancel finds an empty hold and retains nothing, so the close still
        loses the half it was changed to keep.

        The tests above use a bare future for ``slot.task``: cancelling one runs no teardown, so
        none of them can see this. This one uses a real task with the runner's own flush in its
        ``finally``.
        """
        state, slot = _state_and_slot(tmp_path)
        teardown_flushed = asyncio.Event()

        async def _turn() -> None:
            try:
                await asyncio.sleep(3600)
            finally:
                # Exactly what _finish_queue_cycle does at a cycle's end: a PLAIN
                # flush, on a slot the close has already popped.
                slot.flush_deferred_notes()
                teardown_flushed.set()

        slot.task = asyncio.get_running_loop().create_task(_turn())
        await asyncio.sleep(0)
        assert slot.running is True

        async with _note_and_close_client(state) as client:
            resp = await _post_note(client, "CTX-BEFORE-CANCEL")
            assert (await resp.json())["visibleDeferred"] is True
            assert (await client.delete("/api/chat/slots/s1")).status == 200

        assert teardown_flushed.is_set(), "the turn's teardown never ran: race not covered"
        hits = _disk_hits(tmp_path, "contextOnly")
        assert hits, "the cancelled turn drained the hold and the close lost the context half"
        assert _disk_hits(tmp_path, "CTX-BEFORE-CANCEL"), "the visible row did not reach disk"

    def test_a_residual_is_not_drained_by_a_plain_flush_while_closing(self, tmp_path: Path):
        """The residual outlives every flush a single close runs through.

        A close reaches several flush sites -- its own, the cancelled turn's, the hand-over tail's
        -- and only the first knows it is retaining, so the slot's own in-flight close decides
        rather than the keyword; otherwise a plain flush undoes the retention one call later.
        """
        state, slot = _state_and_slot(tmp_path)
        slot._deferred_notes.append(
            {
                "id": "resid0000001",
                "content": "RESIDUAL",
                "cls": "reconcile-note",
                "session": "dashboard:s1",
                "context": {"content": "RESIDUAL", "role": "system"},
                "contextOnly": True,
            }
        )

        slot.begin_close()
        slot.flush_deferred_notes()

        assert [n.get("contextOnly") for n in slot._deferred_notes] == [True]
        assert slot._pending_context == []
        assert slot._dropped_note_ids == set()

        # Negative control: off the close path the residual IS delivered.
        slot.cancel_close()
        slot.flush_deferred_notes()
        assert slot._deferred_notes == []
        assert [c["content"] for c in slot._pending_context] == ["RESIDUAL"]
        assert slot.messages == [], "a context-only residual must not append a second row"
