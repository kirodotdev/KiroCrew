"""Tests for the close-time deferred-note retention fix.

A note held for a running turn is durable in the slot's metadata line, but ``close_slot``
never flushed it, so its visible row never reached the transcript: a session reopened from
History showed no trace of a note the DELETE had answered 200 for. ``close_slot`` now
flushes the hold inside the archive-save's ``try`` and commits the row.
"""

from __future__ import annotations

import asyncio
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
    flushed, so the held note's row never reached the transcript and a History resume, which does
    not read the durable hold, showed nothing while the DELETE still returned 200.

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
        assert [n.get("rowCommitted") for n in slot._deferred_notes] == [True]

    @pytest.mark.asyncio
    async def test_a_completed_close_keeps_the_context_half_in_the_durable_hold(
        self, tmp_path: Path
    ):
        state, slot = _state_and_slot(tmp_path)
        slot.task = asyncio.get_running_loop().create_future()

        async with _note_and_close_client(state) as client:
            await _post_note(client, "CTX-HALF-KEPT")
            note_id = slot._deferred_notes[0]["id"]
            assert (await client.delete("/api/chat/slots/s1")).status == 200

        assert slot._pending_context == []
        assert [m["content"] for m in slot.messages] == ["CTX-HALF-KEPT"]
        assert slot.messages[0]["meta"].get("noteRowFor") == note_id
        assert "noteId" not in slot.messages[0]["meta"]
        hits = _disk_hits(tmp_path, '"noteRowFor"')
        assert len(hits) == 1
        text = Path(hits[0]).read_text(encoding="utf-8")
        assert (
            text.count("CTX-HALF-KEPT") >= 3
        ), "row, hold entry and its context must all be on disk"
        assert f'"id": "{note_id}"' in text or f'"id":"{note_id}"' in text

    def test_a_restore_delivers_a_row_committed_hold_as_context_only(self):
        from kiro_crew.dashboard.slot_buffers import drop_committed_restored_notes

        entry = {
            "id": "held00000009",
            "content": "RESTORED-CTX",
            "cls": "reconcile-note",
            "session": "dashboard:s1",
            "context": {
                "content": "RESTORED-CTX",
                "source": "app",
                "injectedAt": 1.0,
                "maxAge": None,
            },
        }
        rows = [
            {"role": "inject", "content": "RESTORED-CTX", "meta": {"noteRowFor": "held00000009"}}
        ]
        restored = drop_committed_restored_notes(rows, [entry])
        assert [e.get("rowCommitted") for e in restored] == [True]

        slot = _ChatSlot("s1")
        slot._deferred_notes = restored
        slot.flush_deferred_notes()

        assert slot.messages == [], "a row-committed hold must not append a second row"
        assert [c["content"] for c in slot._pending_context] == ["RESTORED-CTX"]
        assert "held00000009" in slot._dropped_note_ids

    @pytest.mark.asyncio
    async def test_an_aborted_close_keeps_the_held_context_deliverable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from kiro_crew.dashboard import chat_handlers

        state, slot = _state_and_slot(tmp_path)
        slot.task = asyncio.get_running_loop().create_future()
        slot._deferred_notes.append(
            {
                "id": "held00000011",
                "content": "ABORTED-CTX",
                "cls": "reconcile-note",
                "session": "dashboard:s1",
                "context": {
                    "content": "ABORTED-CTX",
                    "source": "app",
                    "injectedAt": 1.0,
                    "maxAge": None,
                },
            }
        )

        async def _save_fails(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", _save_fails)
        with pytest.raises(chat_handlers.SlotCloseError):
            await chat_handlers.close_slot(state, slot, "s1")

        assert state._slots.get("s1") is slot
        assert [m["content"] for m in slot.messages] == ["ABORTED-CTX"]
        slot.task = None
        slot.flush_deferred_notes()
        assert [m["content"] for m in slot.messages] == ["ABORTED-CTX"]
        assert [c["content"] for c in slot._pending_context] == ["ABORTED-CTX"]

    @pytest.mark.asyncio
    async def test_the_archival_flush_delivers_nothing_live_to_a_replacement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """After the pop the name can belong to a replacement, and live delivery routes by name.

        The turn here is a bare future, so cancelling it runs no teardown flush and the hold
        reaches the close's own archival flush. The replacement is minted at the pop under a
        different transcript, so the archive path runs rather than the hand-over exit.
        """
        from kiro_crew.dashboard import chat_handlers

        state, slot = _state_and_slot(tmp_path)
        slot.task = asyncio.get_running_loop().create_future()
        slot._deferred_notes.append(
            {
                "id": "held00000004",
                "content": "PRIVATE-HELD-NOTE",
                "cls": "reconcile-note",
                "session": "dashboard:s1",
                "context": None,
            }
        )
        delivered: list[dict] = []
        slot._on_message = lambda _key, msg: delivered.append(msg)
        replacement = _ChatSlot("s1")
        replacement.linked_session_key = "cron:replacement"
        real_unblock = chat_handlers._unblock_pending_waits

        def _replacement_takes_the_name(st: DashboardState, popped: _ChatSlot) -> None:
            st._slots["s1"] = replacement
            real_unblock(st, popped)

        monkeypatch.setattr(chat_handlers, "_unblock_pending_waits", _replacement_takes_the_name)

        await chat_handlers.close_slot(state, slot, "s1")

        assert state._slots.get("s1") is replacement, "fixture: the replacement never took the name"
        assert [m["content"] for m in slot.messages] == ["PRIVATE-HELD-NOTE"]
        assert _disk_hits(tmp_path, "PRIVATE-HELD-NOTE"), "the row was not persisted"
        assert [msg.get("content") for msg in delivered] == []

    @pytest.mark.asyncio
    async def test_a_restored_slot_shows_the_rows_its_archival_flush_appended(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from kiro_crew.dashboard import chat_handlers

        state, slot = _state_and_slot(tmp_path)
        slot.task = asyncio.get_running_loop().create_future()
        slot._deferred_notes.append(
            {
                "id": "held00000007",
                "content": "RESTORED-TAB-NOTE",
                "cls": "reconcile-note",
                "session": "dashboard:s1",
                "context": None,
            }
        )
        delivered: list[dict] = []
        slot._on_message = lambda _key, msg: delivered.append(msg)

        async def _save_fails(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", _save_fails)

        with pytest.raises(chat_handlers.SlotCloseError):
            await chat_handlers.close_slot(state, slot, "s1")

        assert state._slots.get("s1") is slot
        assert [msg.get("content") for msg in delivered] == ["RESTORED-TAB-NOTE"]
