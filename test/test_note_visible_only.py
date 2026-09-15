"""Tests for POST /api/chat/slots/{slot}/note ``visibleOnly`` mode, and the
measured deferred-note-lost-at-close defect the close action sequences around.

Two subjects in one file because they are halves of one contract: ``visibleOnly`` exists for a
breadcrumb written just before a tab closes, and ``TestDeferredNoteLostOnClose`` pins why that
breadcrumb may be followed by a close only when the POST reported ``appended: true``.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.chat import api_chat_slot_note
from kiro_crew.dashboard.chat_handlers import _MAX_CONTEXT_PER_SOURCE, save_slot_off_loop
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history import HistoryLockTimeout


@asynccontextmanager
async def _note_client(state: DashboardState):
    """A minimal app carrying the /note route only.

    Deliberately NOT ``_make_app``: the visibleOnly cases never close a slot, so the smaller app
    keeps the surface under test to the one handler.
    """
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/note", api_chat_slot_note)
    async with TestClient(TestServer(app)) as c:
        yield c


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


class TestNoteVisibleOnly:
    """``visibleOnly: true`` -- write the visible row, build NO context entry."""

    @pytest.mark.asyncio
    async def test_visible_only_writes_the_row_and_enqueues_no_context(self, tmp_path: Path):
        """The whole point: transcript row present, context queue untouched."""
        state, slot = _state_and_slot(tmp_path)

        async with _note_client(state) as client:
            resp = await _post_note(client, "Nothing else, closing this tab.", visibleOnly=True)
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
    @pytest.mark.parametrize("arm", ["refusal", "exception"])
    async def test_a_failed_durable_write_is_not_answered_as_appended(
        self, tmp_path: Path, arm: str
    ):
        """Both failure arms answer alike, because both write nothing.

        ``save_slot_off_loop`` returns ``False`` WITHOUT raising when the slot's routing moved off
        the key the call authorized, so that refusal is invisible to a ``try``;
        ``_save_slot_to_history`` writes through an atomic rename, so a raise likewise leaves the
        transcript exactly as it stands. Answering 200 on either arm would hand the documented
        close-on-``appended === true`` rule a row that exists only in the live window.

        The row is RETRACTED on BOTH arms, sharing one assertion set so they cannot drift: a row
        left in the window is unpersisted state ``flush_slot_now`` makes durable anyway, and the
        503's re-post instruction then lands the note twice. Retracting only on the refusal arm
        would leave a lock timeout duplicating.
        """
        state, slot = _state_and_slot(tmp_path)

        async def _fail(*_args: object, **_kwargs: object) -> bool:
            if arm == "exception":
                raise HistoryLockTimeout("held elsewhere")
            return False

        with patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", _fail):
            async with _note_client(state) as client:
                resp = await _post_note(client, f"Closing, {arm}.", visibleOnly=True)
                assert resp.status == 503
                data = await resp.json()
                assert data["code"] == "note_not_durable"
                assert "appended" not in data
                assert "taken back out of the live window" in data["error"]

        assert slot.messages == [], "the failed row was left for the periodic flush"
        assert slot.total_messages == 0
        assert slot._dirty is False, "an unpersisted row was retracted but the slot stayed dirty"
        assert slot._pending_context == []

    @pytest.mark.asyncio
    async def test_a_refused_row_is_never_surfaced_to_a_connected_client(self, tmp_path: Path):
        """A retraction is server-side, so the row must not reach a client first.

        ``append`` surfaces through two doors -- the ``chat_message`` broadcast and ``_pending``,
        which an attached stream reader drains -- and both fire before the durable write. A row
        surfaced then retracted stays on screen as a ghost that the 503's own re-post instruction
        duplicates, so delivery waits for the commit.
        """
        state, slot = _state_and_slot(tmp_path)
        surfaced: list[dict] = []
        slot._on_message = lambda _key, row: surfaced.append(row)

        async def _refuse(*_args: object, **_kwargs: object) -> bool:
            assert surfaced == [], "the row was broadcast before its durable write"
            assert slot._pending == [], "the row was queued for the stream reader"
            return False

        with patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", _refuse):
            async with _note_client(state) as client:
                resp = await _post_note(client, "Ghost candidate.", visibleOnly=True)
                assert resp.status == 503

        assert surfaced == [], "a retracted row was broadcast to connected clients"
        assert slot._pending == []
        assert slot.messages == []

    @pytest.mark.asyncio
    async def test_a_pinned_row_is_surfaced_through_both_doors(self, tmp_path: Path):
        """The deferral must not COST the delivery -- staging is not dropping.

        Negative control for the test above: with the same instrumentation and a committing save,
        the row reaches the broadcast and the reader queue.
        """
        state, slot = _state_and_slot(tmp_path)
        surfaced: list[dict] = []
        slot._on_message = lambda _key, row: surfaced.append(row)

        async with _note_client(state) as client:
            resp = await _post_note(client, "Delivered breadcrumb.", visibleOnly=True)
            assert resp.status == 200

        assert [r["content"] for r in surfaced] == ["Delivered breadcrumb."]
        assert [r["content"] for r in slot._pending] == ["Delivered breadcrumb."]
        assert surfaced[0] is slot.messages[0], "a second row object was surfaced"

    @pytest.mark.asyncio
    async def test_a_retraction_keeps_a_concurrent_append_and_its_dirty_mark(self, tmp_path: Path):
        """A row that landed BEHIND ours during the await survives the retraction.

        The durable attempt awaits, so position is not a handle on our row and the dirty mark is not
        ours alone to clear: a positional pop would evict the newer row, and clearing the mark would
        strand it with no flush to carry it.
        """
        state, slot = _state_and_slot(tmp_path)

        async def _refuse_after_an_append(*_args: object, **_kwargs: object) -> bool:
            slot.append(role="agent", content="ARRIVED-DURING-THE-SAVE")
            return False

        with patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", _refuse_after_an_append):
            async with _note_client(state) as client:
                resp = await _post_note(client, "Refused note.", visibleOnly=True)
                assert resp.status == 503

        assert [m["content"] for m in slot.messages] == ["ARRIVED-DURING-THE-SAVE"]
        assert slot._dirty is True, "the concurrent row was left unsaved with no flush owed"

    @pytest.mark.asyncio
    async def test_a_close_that_won_the_lock_is_not_undone_by_the_pin(self, tmp_path: Path):
        """A dismissal already on disk must outrank a note save that commits after it.

        Drives the REAL save, because the guard under test lives inside it. The pin is open-shaped
        and ``closed``/``closed_at`` are ``SLOT_OWNED``, so a rebuild writes neither and carries
        neither back: committing after the close erases the dismissal, and the holder is popped so
        nothing rewrites it. A concurrent POST and DELETE reach this by ordinary interleaving.
        """
        state, slot = _state_and_slot(tmp_path)
        slot.append("user", "BEFORE-CLOSE")
        slot.drain()
        assert await save_slot_off_loop(state, slot, closed=True, best_effort=False)
        hkey = "dashboard:s1"
        assert state.conversation_log.get_metadata(hkey).get("closed") is True

        async with _note_client(state) as client:
            resp = await _post_note(client, "Breadcrumb after the close.", visibleOnly=True)
            assert resp.status == 503
            assert (await resp.json())["code"] == "note_not_durable"

        assert (
            state.conversation_log.get_metadata(hkey).get("closed") is True
        ), "the note's pin erased a dismissal the close had already committed"
        assert _disk_hits(tmp_path, "Breadcrumb after the close.") == []
        assert [m["content"] for m in slot.messages] == ["BEFORE-CLOSE"]

    @pytest.mark.asyncio
    async def test_a_refusal_records_the_app_isolation_denial(self, tmp_path: Path, monkeypatch):
        """A refusal is an authorization outcome, so it must reach the audit log.

        The deferred sibling logs ``note_post``/``denied``/``app_isolation`` when a rebind takes its
        hold, and the immediate path refuses for the same reasons. A refusal recorded nowhere leaves
        an app able to hit the guard repeatedly with nothing in the log to show it.

        The RAISING arm is the control: an I/O or lock failure is not an authorization outcome, and
        classifying it as one would put a denial in the log for every transient disk error.
        """
        state = _make_state(tmp_path)
        events: list[dict] = []
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
        )

        async def _refuse(*_args: object, **_kwargs: object) -> bool:
            return False

        async def _raise(*_args: object, **_kwargs: object) -> bool:
            raise HistoryLockTimeout("contended")

        for name, save, want_denials in (("s1", _refuse, 1), ("s2", _raise, 0)):
            _slot(state, name)
            events.clear()
            with patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", save):
                async with _note_client(state) as client:
                    resp = await _post_note(client, "Refused note.", slot=name, visibleOnly=True)
                    assert resp.status == 503

            denials = [
                e
                for e in events
                if e.get("operation") == "note_post" and e.get("outcome") == "denied"
            ]
            assert len(denials) == want_denials, f"slot {name}: {len(denials)} denial(s) logged"
            # The `ok` was written BEFORE the pin, so a failed pin left a false success
            # standing; and the raising arm recorded nothing at all. Both are now final.
            assert [e for e in events if e.get("outcome") == "ok"] == []
            errors = [e for e in events if e.get("outcome") == "error"]
            assert len(errors) == (0 if want_denials else 1), f"slot {name}: {errors}"
            if want_denials:
                assert denials[0]["source"] == "app_isolation"
                assert denials[0]["resources"] == f"slot={name}"
                assert "Refused note." not in str(denials[0]), "the audit line carried the content"

    @pytest.mark.asyncio
    async def test_a_same_key_recreation_does_not_receive_the_pin(self, tmp_path: Path):
        """A transcript recreated under the same key is a different conversation.

        Drives the REAL save, because the guard under test lives inside it. Neither existing guard
        sees this: a channel-, cron- or workflow-born slot keeps its ``history_key`` across a
        close-and-recreate so ``expected_history_key`` matches, and the replacement republishes an
        OPEN line so ``refuse_if_closed`` finds none. Only the generation distinguishes them.
        """
        state = _make_state(tmp_path)
        original = _slot(state)
        original._tab_id = "gen-original"
        original.append("user", "BEFORE-RECREATION")
        original.drain()
        assert await save_slot_off_loop(state, original, best_effort=False)

        replacement = _ChatSlot("s1")
        replacement._tab_id = "gen-replacement"
        replacement.append("user", "REPLACEMENT-ROW")
        replacement.drain()
        assert await save_slot_off_loop(state, replacement, best_effort=False)

        hkey = "dashboard:s1"
        meta = state.conversation_log.get_metadata(hkey)
        assert meta.get("tab_id") == "gen-replacement"
        assert not meta.get("closed"), "an open line, or the closed guard would catch this instead"

        # The handler resolved the ORIGINAL before the recreation popped it.
        state._slots["s1"] = original
        async with _note_client(state) as client:
            resp = await _post_note(client, "Breadcrumb into the replacement.", visibleOnly=True)
            assert resp.status == 503
            assert (await resp.json())["code"] == "note_not_durable"

        assert _disk_hits(tmp_path, "Breadcrumb into the replacement.") == []
        assert _disk_hits(tmp_path, "REPLACEMENT-ROW") != [], "the replacement's row was destroyed"
        assert (
            state.conversation_log.get_metadata(hkey).get("tab_id") == "gen-replacement"
        ), "the pin republished the original's generation over the replacement's"

    @pytest.mark.asyncio
    async def test_a_committed_pin_is_not_delivered_after_a_rebind(self, tmp_path: Path):
        """A rebind keeps the OBJECT, so the session keys are what withhold delivery.

        An unbound slot can have its empty binding claimed by a cron or workflow injection
        while the pin's save awaits. Identity still matches, so the guard above cannot see
        it, and the live doors would hand this session's note to the new one.
        """
        state, slot = _state_and_slot(tmp_path)

        async def _commit_then_rebind(*_args: object, **_kwargs: object) -> bool:
            slot.linked_session_key = "dashboard:someone-else"
            return True

        with patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", _commit_then_rebind):
            async with _note_client(state) as client:
                resp = await _post_note(client, "Breadcrumb.", visibleOnly=True)
                assert resp.status == 409
                assert (await resp.json())["code"] == "note_committed_undelivered"

        assert slot._pending == [], "the note was delivered into the new binding"

    @pytest.mark.asyncio
    async def test_a_committed_pin_is_not_delivered_to_a_replacement_slot(self, tmp_path: Path):
        """The row must not surface into whatever slot now holds the name.

        The generation guard settles the DISK; this settles DELIVERY. A recreation during the save
        leaves the registry pointing at a new slot, and surfacing the row there would show one app's
        breadcrumb in another's live window. The save is made to COMMIT so the identity check is the
        only thing withholding it.
        """
        state = _make_state(tmp_path)
        _slot(state)
        replacement = _ChatSlot("s1")

        async def _commit_then_recreate(*_args: object, **_kwargs: object) -> bool:
            state._slots["s1"] = replacement
            return True

        with patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", _commit_then_recreate):
            async with _note_client(state) as client:
                resp = await _post_note(
                    client, "Breadcrumb for a slot that left.", visibleOnly=True
                )
                assert resp.status == 409
                body = await resp.json()
                # Terminal, NOT the retryable refusal: the bytes are committed, so an
                # obedient re-post would append a second copy into a same-generation
                # reopen. The instruction must forbid closing AND reposting.
                assert body["code"] == "note_committed_undelivered"
                assert "do NOT re-post" in body["error"]
                assert "do not close the slot" in body["error"]

        assert [m["content"] for m in replacement.messages] == []
        assert replacement._pending == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    async def test_a_restricted_slot_cannot_be_promised_a_durable_row(
        self, tmp_path: Path, mode: str
    ):
        """A slot that never persists its transcript is refused before the append.

        ``_save_slot_to_history`` returns True WITHOUT writing when ``memory_mode != "persistent"``,
        so the pin would report durable and the client's rule would close the tab over a row that
        never reached disk -- and an incognito transcript has no later run to self-correct from.
        """
        state, slot = _state_and_slot(tmp_path)
        slot.memory_mode = mode
        saves: list[object] = []

        async def _record(*args: object, **kwargs: object) -> bool:
            saves.append(args)
            return True

        with patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", _record):
            async with _note_client(state) as client:
                resp = await _post_note(client, "Breadcrumb.", visibleOnly=True)
                assert resp.status == 409
                assert (await resp.json())["code"] == "note_not_persistable"

        # Refused before BOTH writes, so there is nothing observable to undo.
        assert saves == []
        assert slot.messages == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("case", "extra"),
        [
            ("omitted", {}),
            ("false", {"visibleOnly": False}),
            ("null", {"visibleOnly": None}),
        ],
    )
    async def test_a_non_true_visible_only_still_does_both_writes(
        self, tmp_path: Path, case: str, extra: dict[str, object]
    ):
        """Regression guard: only a literal ``true`` skips the context half.

        The three non-true spellings share one assertion set because they are one rule, and a case
        drifting from its siblings would hide exactly that. ``null`` must read as omitted -- the
        reading ``maxAge`` gives it -- and an explicit ``false`` must not take a different path from
        an absent field.
        """
        state, slot = _state_and_slot(tmp_path)

        async with _note_client(state) as client:
            resp = await _post_note(client, f"both writes {case}", source="board-sync", **extra)
            assert resp.status == 200
            data = await resp.json()
            assert data["appended"] is True
            assert data["contextSkipped"] is False
            assert data["pending"] == 1

        assert len(slot.messages) == 1
        assert len(slot._pending_context) == 1
        entry = slot._pending_context[0]
        assert entry["content"] == f"both writes {case}"
        assert entry["source"] == "board-sync"
        # The 24h default still applies on every unchanged path.
        assert entry["maxAge"] == 86400

    @pytest.mark.asyncio
    async def test_visible_only_defers_with_a_none_context_while_a_turn_runs(self, tmp_path: Path):
        """A running turn still owns the transcript tail, so the row is HELD.

        The held entry must carry ``context: None`` -- there is no context half to promote, and a
        flush that found one would queue an entry the caller explicitly declined.
        """
        state, slot = _state_and_slot(tmp_path)
        slot.task = asyncio.get_running_loop().create_future()
        assert slot.running is True

        async with _note_client(state) as client:
            resp = await _post_note(client, "held breadcrumb", visibleOnly=True)
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

        ``flush_deferred_notes`` promotes the context entry only ``if ctx is not None``, so a
        visibleOnly hold must leave the flush as one transcript row and an empty queue. Exercised
        through the real flush, not by reading the held record, because that guard is where a wrong
        default would resurrect the declined entry.
        """
        state, slot = _state_and_slot(tmp_path)
        slot.task = asyncio.get_running_loop().create_future()

        async with _note_client(state) as client:
            resp = await _post_note(client, "flush me", visibleOnly=True)
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

        ``1`` and ``0`` are included on purpose: ``isinstance(True, int)`` is True, so a validator
        written as ``isinstance(x, (bool, int))`` would admit them. Coercing an int here would
        silently drop a context entry the caller never asked to drop.
        """
        state, slot = _state_and_slot(tmp_path)

        async with _note_client(state) as client:
            resp = await _post_note(client, "x", visibleOnly=bad_value)
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_visible_only"

        # A rejected request writes NEITHER half.
        assert slot.messages == []
        assert slot._pending_context == []

    @pytest.mark.asyncio
    async def test_visible_only_does_not_consume_the_per_source_cap(self, tmp_path: Path):
        """A skipped context half occupies no cap bucket.

        The cap counts live ``_pending_context`` + held entries. ``visibleOnly`` creates neither, so
        flooding one source with visible-only notes must leave the whole cap available to a later
        ordinary note.
        """
        state, slot = _state_and_slot(tmp_path)

        async with _note_client(state) as client:
            for i in range(_MAX_CONTEXT_PER_SOURCE + 5):
                resp = await _post_note(client, f"row-{i}", source="flood", visibleOnly=True)
                assert resp.status == 200
                assert (await resp.json())["contextSkipped"] is True

            resp = await _post_note(client, "ordinary", source="flood")
            assert resp.status == 200
            assert (await resp.json())["contextSkipped"] is False

        assert len(slot.messages) == _MAX_CONTEXT_PER_SOURCE + 6
        assert len(slot._pending_context) == 1

    @pytest.mark.asyncio
    async def test_visible_only_full_cap_path_is_unchanged(self, tmp_path: Path):
        """The pre-existing cap behaviour still holds for ordinary notes.

        Filling the bucket with ORDINARY notes and then posting one more must still write the
        visible row and report ``contextSkipped`` -- the flag now has two causes, and neither may
        swallow the other.
        """
        state, slot = _state_and_slot(tmp_path)

        async with _note_client(state) as client:
            for i in range(_MAX_CONTEXT_PER_SOURCE):
                resp = await _post_note(client, f"n-{i}", source="flood")
                assert (await resp.json())["contextSkipped"] is False

            resp = await _post_note(client, "over-cap", source="flood")
            assert resp.status == 200
            data = await resp.json()
            assert data["appended"] is True
            assert data["contextSkipped"] is True

        assert len(slot.messages) == _MAX_CONTEXT_PER_SOURCE + 1
        assert len(slot._pending_context) == _MAX_CONTEXT_PER_SOURCE

    @pytest.mark.asyncio
    async def test_visible_only_still_redacts_the_visible_row(self, tmp_path: Path):
        """Redaction is a property of the visible sink, so it is unchanged."""
        state, slot = _state_and_slot(tmp_path)
        secret = "AKIAIOSFODNN7EXAMPLE"  # noqa: S105 - synthetic AWS-shaped key

        async with _note_client(state) as client:
            resp = await _post_note(client, f"closing; key {secret}", visibleOnly=True)
            assert resp.status == 200

        assert len(slot.messages) == 1
        assert secret not in slot.messages[0]["content"]

    @pytest.mark.asyncio
    async def test_visible_only_still_validates_max_age(self, tmp_path: Path):
        """maxAge is validated UNCONDITIONALLY, even with no entry to carry it.

        Silently ignoring a malformed TTL because this call happens to skip the context half would
        move the failure to some later caller who does not skip it.
        """
        state, slot = _state_and_slot(tmp_path)

        async with _note_client(state) as client:
            resp = await _post_note(client, "x", visibleOnly=True, maxAge="soon")
            assert resp.status == 400

        assert slot.messages == []

    @pytest.mark.asyncio
    async def test_visible_only_rejects_empty_content(self, tmp_path: Path):
        """Content validation is unchanged -- visibleOnly is not a bypass."""
        state, slot = _state_and_slot(tmp_path)

        async with _note_client(state) as client:
            resp = await _post_note(client, "", visibleOnly=True)
            assert resp.status == 400

        assert slot.messages == []


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
