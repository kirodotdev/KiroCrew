"""Tests for POST /api/chat/slots/{slot}/note ``visibleOnly`` mode, and the
measured deferred-note-lost-at-close defect the close action sequences around.

Two subjects, deliberately in one file because they are two halves of one
contract: ``visibleOnly`` exists for a breadcrumb written just before a tab
closes, and ``TestDeferredNoteLostOnClose`` pins the reason that breadcrumb may
only be followed by a close when the POST reported ``appended: true``.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.chat import api_chat_slot_note
from kiro_crew.dashboard.chat_handlers import _MAX_CONTEXT_PER_SOURCE
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


@asynccontextmanager
async def _note_client(state: DashboardState):
    """A minimal app carrying the /note route only.

    Deliberately NOT ``_make_app``: the visibleOnly cases never close a slot, so
    the smaller app keeps the surface under test to the one handler.
    """
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/note", api_chat_slot_note)
    async with TestClient(TestServer(app)) as c:
        yield c


@asynccontextmanager
async def _note_and_close_client(state: DashboardState):
    """The full chat app PLUS the /note route.

    ``_make_app`` registers DELETE /api/chat/slots/{slot} but not /note, so the
    close cases need both.
    """
    app = _make_app(state)
    app.router.add_post("/api/chat/slots/{slot}/note", api_chat_slot_note)
    async with TestClient(TestServer(app)) as c:
        yield c


def _slot(state: DashboardState, name: str = "s1") -> _ChatSlot:
    slot = _ChatSlot(name)
    state._slots[name] = slot
    return slot


def _disk_hits(root: Path, needle: str) -> list[str]:
    """Every file under *root* whose text contains *needle*.

    Reads the whole tree rather than a computed transcript path so an assertion
    about absence cannot pass merely because the path was guessed wrong.
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


class TestNoteVisibleOnly:
    """``visibleOnly: true`` -- write the visible row, build NO context entry."""

    @pytest.mark.asyncio
    async def test_visible_only_writes_the_row_and_enqueues_no_context(self, tmp_path: Path):
        """The whole point: transcript row present, context queue untouched."""
        state = _make_state(tmp_path)
        slot = _slot(state)

        async with _note_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/note",
                json={"content": "Nothing else, closing this tab.", "visibleOnly": True},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["appended"] is True
            assert data["visibleDeferred"] is False
            assert data["contextSkipped"] is True
            # pending counts queue + held entries, so it proves neither half of
            # the context channel was touched -- not just the queue.
            assert data["pending"] == 0

        assert len(slot.messages) == 1
        msg = slot.messages[0]
        assert msg["role"] == "inject"
        assert msg["cls"] == "reconcile-note"
        assert msg["content"] == "Nothing else, closing this tab."
        assert slot._pending_context == []
        assert slot._deferred_notes == []

    @pytest.mark.asyncio
    async def test_visible_only_omitted_still_does_both_writes(self, tmp_path: Path):
        """Regression guard: the default path is unchanged by the new field."""
        state = _make_state(tmp_path)
        slot = _slot(state)

        async with _note_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/note",
                json={"content": "board sync done", "source": "board-sync"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["appended"] is True
            assert data["contextSkipped"] is False
            assert data["pending"] == 1

        assert len(slot.messages) == 1
        assert len(slot._pending_context) == 1
        entry = slot._pending_context[0]
        assert entry["content"] == "board sync done"
        assert entry["source"] == "board-sync"
        # The 24h default still applies on the unchanged path.
        assert entry["maxAge"] == 86400

    @pytest.mark.asyncio
    async def test_visible_only_false_does_both_writes(self, tmp_path: Path):
        """An EXPLICIT false must behave exactly like an omitted field."""
        state = _make_state(tmp_path)
        slot = _slot(state)

        async with _note_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/note",
                json={"content": "explicit false", "visibleOnly": False},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["contextSkipped"] is False
            assert data["pending"] == 1

        assert len(slot.messages) == 1
        assert len(slot._pending_context) == 1

    @pytest.mark.asyncio
    async def test_visible_only_null_is_treated_as_omitted(self, tmp_path: Path):
        """An explicit null means omitted -- the same reading ``maxAge`` gives it."""
        state = _make_state(tmp_path)
        slot = _slot(state)

        async with _note_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/note",
                json={"content": "explicit null", "visibleOnly": None},
            )
            assert resp.status == 200
            assert (await resp.json())["contextSkipped"] is False

        assert len(slot._pending_context) == 1

    @pytest.mark.asyncio
    async def test_visible_only_defers_with_a_none_context_while_a_turn_runs(self, tmp_path: Path):
        """A running turn still owns the transcript tail, so the row is HELD.

        The held entry must carry ``context: None`` -- there is no context half
        to promote, and a flush that found one would queue an entry the caller
        explicitly declined.
        """
        state = _make_state(tmp_path)
        slot = _slot(state)
        slot.task = asyncio.get_running_loop().create_future()
        assert slot.running is True

        async with _note_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/note",
                json={"content": "held breadcrumb", "visibleOnly": True},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["appended"] is False
            assert data["visibleDeferred"] is True
            assert data["contextSkipped"] is True
            assert data["pending"] == 0

        assert len(slot.messages) == 0
        assert len(slot._deferred_notes) == 1
        held = slot._deferred_notes[0]
        assert held["context"] is None
        assert held["content"] == "held breadcrumb"
        assert held["cls"] == "reconcile-note"
        assert slot._pending_context == []

    @pytest.mark.asyncio
    async def test_flushing_a_visible_only_hold_writes_only_the_row(self, tmp_path: Path):
        """The flush must not invent a context half for a ``context: None`` hold.

        ``flush_deferred_notes`` promotes the context entry only ``if ctx is not
        None``, so a visibleOnly hold has to come out of the flush as one
        transcript row and an empty queue. Exercised through the real flush
        rather than by reading the held record, because that guard is the seam
        where a wrong default would resurrect the entry the caller declined.
        """
        state = _make_state(tmp_path)
        slot = _slot(state)
        slot.task = asyncio.get_running_loop().create_future()

        async with _note_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/note",
                json={"content": "flush me", "visibleOnly": True},
            )
            assert (await resp.json())["visibleDeferred"] is True

        slot.task = None
        assert slot.running is False
        assert slot.flush_deferred_notes() == 1

        assert len(slot.messages) == 1
        assert slot.messages[0]["role"] == "inject"
        assert slot.messages[0]["content"] == "flush me"
        assert slot._pending_context == []
        assert slot._deferred_notes == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_value",
        [
            "true",  # a JSON string, the most likely client bug
            "false",  # truthy as a string -- coercion would invert the meaning
            1,  # isinstance(1, bool) is False: must NOT be accepted as True
            0,
            [],
            {},
            1.0,
        ],
    )
    async def test_non_boolean_visible_only_is_a_400(self, tmp_path: Path, bad_value: object):
        """The TYPE is validated, mirroring ``_validate_max_age``.

        ``1`` and ``0`` are included on purpose: ``isinstance(True, int)`` is
        True, so a validator written as ``isinstance(x, (bool, int))`` would
        admit them. Coercing an int here would silently drop a context entry the
        caller never asked to drop.
        """
        state = _make_state(tmp_path)
        slot = _slot(state)

        async with _note_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/note",
                json={"content": "x", "visibleOnly": bad_value},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_visible_only"

        # A rejected request writes NEITHER half.
        assert slot.messages == []
        assert slot._pending_context == []

    @pytest.mark.asyncio
    async def test_visible_only_does_not_consume_the_per_source_cap(self, tmp_path: Path):
        """A skipped context half occupies no cap bucket.

        The cap counts live ``_pending_context`` + held entries. ``visibleOnly``
        creates neither, so flooding one source with visible-only notes must
        leave the whole cap available to a later ordinary note.
        """
        state = _make_state(tmp_path)
        slot = _slot(state)

        async with _note_client(state) as client:
            for i in range(_MAX_CONTEXT_PER_SOURCE + 5):
                resp = await client.post(
                    "/api/chat/slots/s1/note",
                    json={"content": f"row-{i}", "source": "flood", "visibleOnly": True},
                )
                assert resp.status == 200
                assert (await resp.json())["contextSkipped"] is True

            resp = await client.post(
                "/api/chat/slots/s1/note",
                json={"content": "ordinary", "source": "flood"},
            )
            assert resp.status == 200
            assert (await resp.json())["contextSkipped"] is False

        assert len(slot.messages) == _MAX_CONTEXT_PER_SOURCE + 6
        assert len(slot._pending_context) == 1

    @pytest.mark.asyncio
    async def test_visible_only_full_cap_path_is_unchanged(self, tmp_path: Path):
        """The pre-existing cap behaviour still holds for ordinary notes.

        Filling the bucket with ORDINARY notes and then posting one more must
        still write the visible row and report ``contextSkipped`` -- the flag now
        has two causes, and neither may swallow the other.
        """
        state = _make_state(tmp_path)
        slot = _slot(state)

        async with _note_client(state) as client:
            for i in range(_MAX_CONTEXT_PER_SOURCE):
                resp = await client.post(
                    "/api/chat/slots/s1/note",
                    json={"content": f"n-{i}", "source": "flood"},
                )
                assert (await resp.json())["contextSkipped"] is False

            resp = await client.post(
                "/api/chat/slots/s1/note",
                json={"content": "over-cap", "source": "flood"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["appended"] is True
            assert data["contextSkipped"] is True

        assert len(slot.messages) == _MAX_CONTEXT_PER_SOURCE + 1
        assert len(slot._pending_context) == _MAX_CONTEXT_PER_SOURCE

    @pytest.mark.asyncio
    async def test_visible_only_still_redacts_the_visible_row(self, tmp_path: Path):
        """Redaction is a property of the visible sink, so it is unchanged."""
        state = _make_state(tmp_path)
        slot = _slot(state)
        secret = "AKIAIOSFODNN7EXAMPLE"  # noqa: S105 - synthetic AWS-shaped key

        async with _note_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/note",
                json={"content": f"closing; key {secret}", "visibleOnly": True},
            )
            assert resp.status == 200

        assert len(slot.messages) == 1
        assert secret not in slot.messages[0]["content"]

    @pytest.mark.asyncio
    async def test_visible_only_still_validates_max_age(self, tmp_path: Path):
        """maxAge is validated UNCONDITIONALLY, even with no entry to carry it.

        Silently ignoring a malformed TTL because this call happens to skip the
        context half would move the failure to some later caller who does not
        skip it.
        """
        state = _make_state(tmp_path)
        slot = _slot(state)

        async with _note_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/note",
                json={"content": "x", "visibleOnly": True, "maxAge": "soon"},
            )
            assert resp.status == 400

        assert slot.messages == []

    @pytest.mark.asyncio
    async def test_visible_only_rejects_empty_content(self, tmp_path: Path):
        """Content validation is unchanged -- visibleOnly is not a bypass."""
        state = _make_state(tmp_path)
        slot = _slot(state)

        async with _note_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/note",
                json={"content": "", "visibleOnly": True},
            )
            assert resp.status == 400

        assert slot.messages == []


class TestDeferredNoteLostOnClose:
    """A held note SURVIVES its slot closing — and why this class kept the old name.

    It was a DEFECT PIN. ``close_slot`` never flushed, ``_deferred_notes`` is an
    in-memory ``__slots__`` attribute the persistence layer never reads, so once the
    slot was popped the held note died with the frame and the DELETE still returned
    200: data loss reported as success. The pin existed so that whoever later added
    the flush would see these tests fail and revisit the client-side rule
    deliberately rather than by accident. That is exactly what happened.

    ``close_slot`` now flushes inside the archive-save's ``try``, so a flush failure
    shares the restore arm — the shape the bulk-cleanup path already used. Both of
    its callers are covered: the tab close and session control's ``close_target``.

    The client rule is UNCHANGED and still correct: a note arriving mid-turn is still
    deferred and still answers ``appended: false``, so sequencing the close on
    ``appended === true`` still refuses to close on a breadcrumb that is only held.
    The flush removes the data-loss consequence; it does not make it right to close
    on a note the backend has not durably recorded.
    """

    def test_the_COUPLING_between_this_defect_and_the_client_rule_is_pinned(self):
        """A held note must never be silently destroyed by a close.

        TWO arms satisfy that invariant, and this asserts the DISJUNCTION rather
        than either arm, which is what keeps it honest in both directions:

        * the BACKEND flushes held notes on close, so nothing is lost; or
        * the CLIENT refuses to close while a note is merely deferred.

        Today only the second holds. Crucially, someone who later adds the flush
        makes the FIRST arm true and this test stays GREEN -- an earlier version
        asserted ``"flush_deferred_notes" not in backend``, which turned red on the
        real fix and named that fix as the fault.

        The client arm is pinned BEHAVIOURALLY, not by source text, in
        ``website/src/test/OptionActionDispatch.test.tsx`` -- "refuses to close on a
        DEFERRED note, and says why" drives the hook with ``appended: false`` and
        asserts no close is dispatched. That is why this test no longer greps the
        hook for a condition spelling: rewording ``appended !== true`` is a cosmetic
        edit that must not fail a test, and the behaviour is covered where it can
        actually be exercised.
        """
        import inspect

        from kiro_crew.dashboard import chat_handlers

        # `api_chat_slot_delete` DELEGATES the close sequence to `close_slot`, which is
        # where the flush lives and which session control's `close_target` shares.
        backend_flushes = "flush_deferred_notes" in (
            inspect.getsource(chat_handlers.api_chat_slot_delete)
            + inspect.getsource(chat_handlers.close_slot)
        )

        hook_test = (
            Path(__file__).resolve().parents[1] / "website/src/test/OptionActionDispatch.test.tsx"
        )
        assert hook_test.is_file(), f"client behavioural pin not found at {hook_test}"
        client_refusal_is_pinned = "refuses to close on a DEFERRED note" in hook_test.read_text()

        assert backend_flushes or client_refusal_is_pinned, (
            "NEITHER arm now protects a held note on close: the close path no longer "
            "flushes held notes, and the client's refusal is no longer pinned by "
            "OptionActionDispatch.test.tsx. Restore one of them -- either make the "
            "close flush held notes, or reinstate the behavioural test that proves "
            "the client will not close on a deferred breadcrumb."
        )

    @pytest.mark.asyncio
    async def test_CONTROL_immediate_note_survives_the_same_close(self, tmp_path: Path):
        """POSITIVE CONTROL for the defect test below.

        Without this, "absent from disk" would be a fact about the harness (a
        wrong search root, a save that never runs under a TestServer) rather than
        about the world. The same close, the same disk probe, an IMMEDIATE note:
        it must land in ``slot.messages`` AND on disk.
        """
        state = _make_state(tmp_path)
        slot = _slot(state)
        assert slot.running is False  # no turn -> immediate path

        async with _note_and_close_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/note", json={"content": "CONTROL-IMMEDIATE"}
            )
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

        This test asserted the loss until the flush was added — the pin existed so the
        fix could not land silently. It now pins the fix instead, and deliberately keeps
        the DEFER behaviour above unchanged: a note arriving mid-turn is still held and
        still answers ``appended: false``, so the client's refusal to close on a merely
        deferred breadcrumb remains correct. What changed is only that a close no longer
        destroys what it held.
        """
        state = _make_state(tmp_path)
        slot = _slot(state)
        slot.task = asyncio.get_running_loop().create_future()
        assert slot.running is True

        async with _note_and_close_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/note", json={"content": "BREADCRUMB-DEFERRED"}
            )
            data = await resp.json()
            # Unchanged: still deferred, still honestly reported as not appended.
            assert data["visibleDeferred"] is True
            assert data["appended"] is False
            assert len(slot._deferred_notes) == 1
            assert len(slot.messages) == 0

            dresp = await client.delete("/api/chat/slots/s1")
            assert dresp.status == 200

        # Flushed, not abandoned: the held queue is empty because it was promoted.
        assert slot._deferred_notes == [], "the close left the note held, so it was lost"
        assert [m["content"] for m in slot.messages] == [
            "BREADCRUMB-DEFERRED"
        ], "the held note did not reach the transcript"
        # ...and it reached disk. The control above proves this probe CAN find a note.
        assert _disk_hits(tmp_path, "BREADCRUMB-DEFERRED"), "flushed in memory but not saved"
        assert "s1" not in state._slots
