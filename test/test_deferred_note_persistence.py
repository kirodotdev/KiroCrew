"""Deferred-note hold durability.

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
import contextlib
import time
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
    async def test_a_bind_during_the_save_is_reported_as_conditional_delivery(
        self, tmp_path: Path, monkeypatch
    ):
        """A note the drain will drop must never be acknowledged as unconditional.

        ``deliveryConditional`` is computed AFTER the durability await, so a cron
        binding an unbound slot during that save flips ``not linked_session_key`` to
        false and the flag reported ``false`` -- for a note whose halves the next
        drain drops, because both were stamped for the empty binding. The guarded
        save already answers this: it returns ``False`` only when routing moved off
        the key the note was authorized against.

        The bind is landed INSIDE the patched save so it lands in the real window
        rather than before the note is written, which would be a different case
        (an already-bound slot, genuinely unconditional).
        """
        import kiro_crew.dashboard.chat_handlers as handlers_mod

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "bind-race")
        slot.linked_session_key = ""
        shown: list[dict] = []
        slot._on_message = lambda _key, msg: shown.append(msg)
        slot._has_reader = False

        async def _save_rebinds_then_refuses(state_, slot_, *a, **kw):
            slot_.linked_session_key = "cron:9001"
            return False

        monkeypatch.setattr(handlers_mod, "save_slot_off_loop", _save_rebinds_then_refuses)
        monkeypatch.setattr(handlers_mod, "dispatch_note_mirror", lambda *a, **kw: None)

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/bind-race/note", json={"content": "note during a rebind"}
            )
            assert resp.status == 200
            body = await resp.json()

        assert body["visibleDeferred"] is False, "must exercise the IMMEDIATE path"
        assert slot.linked_session_key == "cron:9001", "the bind must have landed in the window"
        assert (
            body["appended"] is False
        ), f"the guarded save REFUSED the write, so nothing reached disk: {body}"
        assert body["deliveryConditional"] is True, (
            "a refused guarded save means routing moved off the authorized key, so the "
            f"halves will be dropped and the 200 must say so; got {body}"
        )
        assert [
            m for m in shown if m.get("cls") == "reconcile-note"
        ] == [], f"the note was released into the session that replaced its own: {shown}"
        assert [
            m for m in slot._pending if m.get("cls") == "reconcile-note"
        ] == [], "the note was left drainable by a reader of the replacement session"

    @pytest.mark.asyncio
    async def test_both_halves_are_settled_before_the_durable_save_can_be_outrun(
        self, tmp_path: Path, monkeypatch
    ):
        """Probe what a turn admitted DURING the durable save would observe.

        The save is the slow step, so a concurrent message can drive a turn while it
        runs, and what that turn could see mid-save is what decides correctness. The
        context entry must already be queued, or the drain misses it and the note is
        applied to a later turn. The visible row must NOT be in the stream queue, or an
        active reader renders a row a 404 is about to withdraw.
        """
        import kiro_crew.dashboard.chat_handlers as handlers_mod

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "ordering-at-the-save")
        assert slot is not None, "precondition: the slot must exist to accept a note"
        seen: dict[str, object] = {}

        async def _observe_then_commit(_state, observed_slot, *_a, **_k):
            # What a turn admitted here would ACTUALLY receive, which is the property:
            # the entry may be queued mid-save so long as a drain refuses to emit it.
            seen["context_drainable"] = [
                e
                for e in observed_slot._pending_context
                if "ordering probe" in str(e.get("content", "")) and not e.get("awaitingCommit")
            ]
            seen["streamable"] = [
                m for m in observed_slot._pending if m.get("cls") == "reconcile-note"
            ]
            return True

        monkeypatch.setattr(handlers_mod, "save_slot_off_loop", _observe_then_commit)
        monkeypatch.setattr(handlers_mod, "dispatch_note_mirror", lambda *_a, **_k: None)

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/ordering-at-the-save/note",
                json={"content": "ordering probe"},
            )
            assert resp.status == 200, f"the note should be accepted; got {resp.status}"

        assert seen, "precondition: the durable save must have run"
        assert seen.get("context_drainable") == [], (
            "a turn admitted during the save could drain this note's context while its "
            f"visible row was still withheld: {seen.get('context_drainable')}"
        )
        assert (
            seen.get("streamable") == []
        ), f"an unconfirmed row was already drainable by a reader: {seen.get('streamable')}"

    @pytest.mark.asyncio
    async def test_a_drain_between_hold_and_release_cannot_reorder_the_held_row(
        self, tmp_path: Path, monkeypatch
    ):
        """A held row must land ahead of rows appended after it, whatever the queue did.

        A reader draining while a row is held empties the queue the row was going to
        rejoin, so any index recorded at append time describes a queue that has since
        been reshaped -- clamping such an index to the tail puts the held row BEHIND rows
        that were appended later. Position has to come from the row itself, not from
        where the queue happened to be.
        """
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "drain-between")
        slot._pending.clear()

        for i in range(5):
            slot.append("assistant", f"earlier {i}", "")
        held = slot.append("inject", "held across the drain", "reconcile-note", defer_stream=True)
        assert [m.get("content") for m in slot._pending] == [
            f"earlier {i}" for i in range(5)
        ], "precondition: the held row must not be queued"

        slot.drain()
        assert slot._pending == [], "precondition: the reader drained everything queued"
        later = slot.append("assistant", "appended after the held row", "")
        slot.broadcast_appended_row(held)

        order = [m.get("content") for m in slot._pending]
        assert order == ["held across the drain", "appended after the held row"], (
            f"the held row was appended before {later.get('content')!r}, so a reader must "
            f"receive it first; got {order}"
        )

    @pytest.mark.asyncio
    async def test_a_staged_note_never_evicts_an_accepted_context_entry(
        self, tmp_path: Path, monkeypatch
    ):
        """A note's provisional half must not push someone else's accepted context out.

        The note stages its context before the durability await so a drain can hold rather
        than miss it, which means it occupies a slot while the save runs. With one slot
        free, a `/context` post landing inside that window found the queue full and the
        bounded FIFO dropped the OLDEST entry -- an accepted one -- to make room. A
        provisional entry may be displaced; an accepted one may not.
        """
        import asyncio as _asyncio

        import kiro_crew.dashboard.chat_handlers as handlers_mod
        from kiro_crew.dashboard.state import _MAX_PENDING_CONTEXT

        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "staging-evicts-accepted")
        slot._pending_context.clear()
        now = time.time()
        for i in range(_MAX_PENDING_CONTEXT - 1):
            slot._pending_context.append(
                {
                    "content": f"accepted entry {i}",
                    "source": f"producer-{i}",
                    "injectedAt": now,
                    "maxAge": 86400,
                }
            )
        assert len(slot._pending_context) == _MAX_PENDING_CONTEXT - 1

        released = _asyncio.Event()

        async def _save_holds_the_window_open(state_, slot_, *a, **kw):
            # A concurrent producer lands while the note's provisional half holds a slot.
            slot_.append_pending_context(
                {
                    "content": "the newcomer's accepted entry",
                    "source": "newcomer",
                    "injectedAt": time.time(),
                    "maxAge": 86400,
                }
            )
            released.set()
            return True

        monkeypatch.setattr(handlers_mod, "save_slot_off_loop", _save_holds_the_window_open)

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/staging-evicts-accepted/note",
                json={"content": "the staged note"},
            )
            assert resp.status == 200
        assert released.is_set(), "precondition: the concurrent producer must have run"

        contents = [str(e.get("content", "")) for e in slot._pending_context]
        assert "accepted entry 0" in contents, (
            "the staged note's provisional half filled the queue, so the concurrent "
            f"producer evicted an ACCEPTED entry to make room; queue={contents[:3]}..."
        )
        assert "the newcomer's accepted entry" in contents

    @pytest.mark.asyncio
    async def test_a_cancelled_note_whose_slot_rebound_leaves_no_row_behind(
        self, tmp_path: Path, monkeypatch
    ):
        """A cancelled note must not survive in a window whose routing moved.

        The cancellation path settles from the save's outcome, and reading only "reached the
        transcript" from that outcome drops the routing half. A committed save whose slot
        rebound then keeps a row authorized for the previous session, so a detail read
        returns one session's note as the replacement session's history -- the same harm the
        non-cancelled path already discards the window row for.
        """
        import asyncio as _asyncio

        import kiro_crew.dashboard.chat_handlers as handlers_mod
        import kiro_crew.dashboard.chat_runner as runner_mod

        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "cancelled-then-rebound")
        slot._pending_context.clear()
        authored_key = effective_session_key(slot)

        async def _commit_then_rebind(state_, slot_, *a, **kw):
            await _asyncio.sleep(0.25)
            slot_.linked_session_key = "cron:9411"
            return True

        monkeypatch.setattr(handlers_mod, "save_slot_off_loop", _commit_then_rebind)

        async with self._make_client(state) as client:
            posting = _asyncio.ensure_future(
                client.post(
                    "/api/chat/slots/cancelled-then-rebound/note",
                    json={"content": "authorized for the first session"},
                )
            )
            await _asyncio.sleep(0.08)
            posting.cancel()
            with contextlib.suppress(_asyncio.CancelledError, Exception):
                await posting
            # Let the shielded save finish and its resolution callback run.
            await _asyncio.sleep(0.40)

        assert slot.linked_session_key == "cron:9411", "precondition: the rebind must land"
        assert effective_session_key(slot) != authored_key
        leaked = [
            m
            for m in slot.messages
            if "authorized for the first session" in str(m.get("content", ""))
        ]
        assert leaked == [], (
            "a cancelled note stayed in the window after its slot rebound, so a detail read "
            f"serves it as the replacement session's history; {leaked}"
        )
        assert (
            runner_mod.drain_pending_context(slot) == ""
        ), "its context half also survived the rebind"

    @pytest.mark.asyncio
    async def test_a_save_that_commits_despite_cancellation_keeps_both_halves(
        self, tmp_path: Path, monkeypatch
    ):
        """If the save commits, cancellation must not strip the note's context half.

        Shutdown cancels the handler but the executor save runs on and commits the row, so
        a cleanup that withdraws the in-memory halves leaves a restored note the model has
        no context for. The decision therefore follows the save rather than the unwind:
        committed means both halves live, and the context half is released to a drain.
        """
        import asyncio as _asyncio

        import kiro_crew.dashboard.chat_handlers as handlers_mod
        import kiro_crew.dashboard.chat_runner as runner_mod

        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "commits-despite-cancel")
        slot._pending_context.clear()

        async def _save_commits_after_the_cancel(state_, slot_, *a, **kw):
            await _asyncio.sleep(0.25)
            return True

        monkeypatch.setattr(handlers_mod, "save_slot_off_loop", _save_commits_after_the_cancel)

        async with self._make_client(state) as client:
            posting = _asyncio.ensure_future(
                client.post(
                    "/api/chat/slots/commits-despite-cancel/note",
                    json={"content": "committed but abandoned"},
                )
            )
            await _asyncio.sleep(0.08)
            posting.cancel()
            with contextlib.suppress(_asyncio.CancelledError, Exception):
                await posting
            # Let the shielded save finish and its resolution callback run.
            await _asyncio.sleep(0.35)

        rows = [m for m in slot.messages if "committed but abandoned" in str(m.get("content", ""))]
        drained = runner_mod.drain_pending_context(slot)
        assert rows and "committed but abandoned" in drained, (
            "the save committed the row, but cancellation settled the note against the "
            "unwind instead of the save, so the halves disagree: "
            f"row_kept={bool(rows)} context_drainable={'committed but abandoned' in drained}"
        )

    @pytest.mark.asyncio
    async def test_a_completed_cancellation_rechecks_routing_before_broadcasting(
        self, tmp_path: Path, monkeypatch
    ):
        """A cancelled note whose save ALREADY completed must re-read routing first.

        `_note_commit_settled` returns the routing verdict the durability task computed
        at its own final line. Between that line and this coroutine resuming there is an
        ordinary scheduling window, and a rebind landing in it leaves the verdict stale --
        so the completed-cancellation branch broadcast the OLD session's row from the
        REPLACEMENT session's window. The two sibling paths re-read the history key
        synchronously; this branch did not.
        """
        import asyncio as _asyncio

        import kiro_crew.dashboard.chat_handlers as handlers_mod

        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "completed-cancel-rebind")
        slot._pending_context.clear()
        broadcast: list[object] = []
        monkeypatch.setattr(
            type(slot), "broadcast_appended_row", lambda self, row: broadcast.append(row)
        )
        monkeypatch.setattr(
            handlers_mod,
            "snapshot_note_destinations",
            lambda *a, **kw: (("telegram", "c1"), None),
        )

        # The save COMMITS and reports routing intact, so the task's own verdict is True.
        async def _durable_and_intact(state_, slot_, *a, **kw):
            return True, True, False

        monkeypatch.setattr(handlers_mod, "_immediate_note_is_durable", _durable_and_intact)

        real_shield = _asyncio.shield

        # Models the finding's window: the task reaches done, a rebind lands, and the
        # coroutine is then cancelled at the shield, so the verdict predates the rebind.
        async def _shield_then_rebind_and_cancel(awaitable, **kw):
            await awaitable
            slot.linked_session_key = "telegram:replacement-session"
            raise _asyncio.CancelledError

        monkeypatch.setattr(_asyncio, "shield", _shield_then_rebind_and_cancel)

        async with self._make_client(state) as client:
            with contextlib.suppress(Exception):
                await client.post(
                    "/api/chat/slots/completed-cancel-rebind/note",
                    json={"content": "authored before the rebind"},
                )

        monkeypatch.setattr(_asyncio, "shield", real_shield)

        assert broadcast == [], (
            "the completed-cancellation branch broadcast a row authored for the previous "
            "session after the slot rebound, so the replacement session's window shows "
            f"another conversation's note; {broadcast}"
        )
        drainable = [
            e
            for e in slot._pending_context
            if "authored before the rebind" in str(e.get("content", ""))
            and not e.get("awaitingCommit")
        ]
        assert (
            drainable == []
        ), f"its context half was released into the rebound session; {drainable}"

    @pytest.mark.asyncio
    async def test_a_cancelled_commit_restarts_the_ttl_it_released(
        self, tmp_path: Path, monkeypatch
    ):
        """A released context half must not expire on time spent waiting for its own save.

        Expiry is ``injectedAt + maxAge < now``. The cancelled path popped
        awaitingCommit but left the original stamp, so a note whose save outlasted its
        maxAge was released already-expired and the next read discarded it unseen -- the
        non-cancelled path restarts the stamp for exactly this reason.
        """
        import asyncio as _asyncio

        import kiro_crew.dashboard.chat_handlers as handlers_mod
        import kiro_crew.dashboard.chat_runner as runner_mod

        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "cancelled-ttl")
        slot._pending_context.clear()

        # The save outlasts the note's own maxAge, which is what makes the stamp stale.
        async def _save_outlasts_the_ttl(state_, slot_, *a, **kw):
            await _asyncio.sleep(0.5)
            return True

        monkeypatch.setattr(handlers_mod, "save_slot_off_loop", _save_outlasts_the_ttl)
        monkeypatch.setattr(handlers_mod, "dispatch_note_mirror", lambda *_a, **_k: None)

        async with self._make_client(state) as client:
            posting = _asyncio.ensure_future(
                client.post(
                    "/api/chat/slots/cancelled-ttl/note",
                    json={"content": "held past its own ttl", "maxAge": 0.35},
                )
            )
            await _asyncio.sleep(0.08)
            posting.cancel()
            with contextlib.suppress(_asyncio.CancelledError, Exception):
                await posting
            # Past the save (0.5s) so the release has happened, but well inside the
            # RESTARTED ttl -- the original stamp is already expired by this point.
            await _asyncio.sleep(0.55)

        entries = [
            e for e in slot._pending_context if "held past its own ttl" in str(e.get("content", ""))
        ]
        assert entries, "precondition: the committed save must have released the context half"
        assert not entries[0].get(
            "awaitingCommit"
        ), "precondition: the entry must be released for this test to be about its stamp"
        drained = runner_mod.drain_pending_context(slot)
        assert "held past its own ttl" in drained, (
            "the released context half was discarded as expired, because the stamp still "
            "dated from before a save that outlasted its maxAge; the note reached no turn"
        )

    @pytest.mark.asyncio
    async def test_cancellation_at_the_save_leaves_neither_half_behind(
        self, tmp_path: Path, monkeypatch
    ):
        """A cancelled note commit must leave no row without its context, nor the reverse.

        An ordinary client disconnect cancels the handler at the durability await. Unwinding
        released only the lock, so the appended row stayed in the window -- persisted by the
        next save -- while its context half sat flagged awaitingCommit, which every drain
        holds and only a TTL ever clears. Neither half may survive alone.
        """
        import asyncio as _asyncio

        import kiro_crew.dashboard.chat_handlers as handlers_mod
        import kiro_crew.dashboard.chat_runner as runner_mod

        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "cancelled-mid-commit")
        slot._pending_context.clear()

        async def _save_never_returns(state_, slot_, *a, **kw):
            await _asyncio.Event().wait()
            return True

        monkeypatch.setattr(handlers_mod, "save_slot_off_loop", _save_never_returns)

        async with self._make_client(state) as client:
            posting = _asyncio.ensure_future(
                client.post(
                    "/api/chat/slots/cancelled-mid-commit/note",
                    json={"content": "abandoned mid-commit"},
                )
            )
            await _asyncio.sleep(0.15)
            posting.cancel()
            with contextlib.suppress(_asyncio.CancelledError, Exception):
                await posting

        stranded_rows = [
            m for m in slot.messages if "abandoned mid-commit" in str(m.get("content", ""))
        ]
        drainable_context = [
            e
            for e in slot._pending_context
            if "abandoned mid-commit" in str(e.get("content", "")) and not e.get("awaitingCommit")
        ]
        assert drainable_context == [], (
            "a cancelled commit released its context half while its save was unresolved, so "
            f"a turn can cite a note that may never have persisted; {drainable_context}"
        )
        assert (
            runner_mod.drain_pending_context(slot) == ""
        ), "the abandoned note still reaches a turn"
        assert not any(
            m.get("broadcast") for m in stranded_rows
        ), "an unresolved note's row was shown to observers, who cannot un-see it"
        assert (
            not slot._note_commit_lock.locked()
        ), "the commit lock was not released, so every later note would block"

    @pytest.mark.asyncio
    async def test_time_held_for_a_commit_does_not_consume_a_notes_ttl(
        self, tmp_path: Path, monkeypatch
    ):
        """Being held for its own commit must not expire a note's context.

        A drain during the durability window holds the entry rather than emitting it, so a
        short-TTL note whose save outlasts that TTL would be re-queued already expired and
        silently dropped by the next drain -- lost to the race, not to the caller's TTL.
        The clock restarts when the entry becomes drainable, so the hold costs it nothing.
        """
        import asyncio as _asyncio

        import kiro_crew.dashboard.chat_handlers as handlers_mod
        import kiro_crew.dashboard.chat_runner as runner_mod

        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "held-past-its-ttl")
        slot._pending_context.clear()

        async def _save_outlasting_the_ttl(state_, slot_, *a, **kw):
            runner_mod.drain_pending_context(slot_)
            await _asyncio.sleep(0.30)
            return True

        monkeypatch.setattr(handlers_mod, "save_slot_off_loop", _save_outlasting_the_ttl)

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/held-past-its-ttl/note",
                json={"content": "short lived note", "maxAge": 0.2},
            )
            assert resp.status == 200
            assert (await resp.json())["contextSkipped"] is False

        after = runner_mod.drain_pending_context(slot)
        assert "short lived note" in after, (
            "the entry expired while held for its own commit, so the note was lost to the "
            f"race rather than to its TTL; got {after!r}"
        )

    @pytest.mark.asyncio
    async def test_a_committed_note_leaves_no_row_in_a_rebound_window(
        self, tmp_path: Path, monkeypatch
    ):
        """A note authorized for one session must not sit in another's window.

        Retaining the in-memory row after a rebind because it reached disk leaves content
        authorized for session A inside the buffer a rebound slot now serves as B. Only
        one call site purges foreign-authorized rows, ``drain_pending_context`` on the
        turn path, so a detail read before any turn returns A's note as B's history.
        The committed row is untouched: withdrawal is from the window, not the transcript.
        """
        import kiro_crew.dashboard.chat_handlers as handlers_mod

        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "rebound-window")
        authored_key = effective_session_key(slot)

        async def _save_commits_then_rebinds(state_, slot_, *a, **kw):
            slot_.linked_session_key = "cron:9100"
            return True

        monkeypatch.setattr(handlers_mod, "save_slot_off_loop", _save_commits_then_rebinds)

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/rebound-window/note",
                json={"content": "authorized for the first session only"},
            )
            assert resp.status == 200
            body = await resp.json()

        assert slot.linked_session_key == "cron:9100", "precondition: the rebind landed"
        assert (
            effective_session_key(slot) != authored_key
        ), "precondition: the slot must now serve a DIFFERENT session"
        leaked = [
            m
            for m in slot.messages
            if "authorized for the first session only" in str(m.get("content", ""))
        ]
        assert leaked == [], (
            "a note authorized for the previous session is still in the window this slot "
            f"now serves, so a detail read returns it as the new session's history; {leaked}"
        )
        assert body["appended"] is True, (
            "the row did reach the transcript it was authorized for; only the window copy "
            f"is withdrawn, so the caller must not be told the append failed; got {body}"
        )
        assert body["deliveryConditional"] is True

    @pytest.mark.asyncio
    async def test_a_turn_starting_during_the_lock_wait_holds_the_note(
        self, tmp_path: Path, monkeypatch
    ):
        """A turn dispatched while the commit lock is contended must defer the note.

        The immediate-vs-held decision reads ``running``/``_in_stage_execution`` once. The
        commit lock can then be contended for the length of another note's durable save,
        and a turn dispatching in that window leaves the decision stale: an ``inject`` row
        appended for a running slot takes the tail the replay path skips, so the user's
        request is replayed twice. The state has to be re-read after the wait.
        """
        import asyncio as _asyncio

        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "turn-starts-during-wait")
        slot._pending_context.clear()
        rows_before = len(slot.messages)

        # Held from here, so the note blocks on acquire exactly as it would behind
        # another note's durable save -- without a second in-flight request.
        await slot._note_commit_lock.acquire()

        async with self._make_client(state) as client:
            posting = _asyncio.ensure_future(
                client.post(
                    "/api/chat/slots/turn-starts-during-wait/note",
                    json={"content": "arrives while contended"},
                )
            )
            # No timing assertion needed: the handler cannot pass its acquire while this
            # test holds the lock, so the flag flips strictly before the recheck runs.
            await _asyncio.sleep(0.05)
            slot._in_stage_execution = True
            slot._note_commit_lock.release()
            resp = await posting
            assert resp.status == 200
            body = await resp.json()

        appended = [
            m for m in slot.messages if "arrives while contended" in str(m.get("content", ""))
        ]
        assert appended == [], (
            "a note whose slot began a turn while it waited for the commit lock was "
            f"appended anyway, so the replay path drops the user's message; {appended}"
        )
        assert body["visibleDeferred"] is True, (
            "the note must be HELD once the slot is running, not reported as appended; "
            f"got {body}"
        )
        assert len(slot.messages) == rows_before

    @pytest.mark.asyncio
    async def test_a_failed_durable_write_withholds_the_channel_mirror(
        self, tmp_path: Path, monkeypatch
    ):
        """A forced write that raised must withhold the channel mirror.

        This is the protection against a channel note outliving the line it asserts: the
        dispatch is gated on the durable write, not on any response flag. The 200 still
        reports the row as appended, because the row WAS written -- what the failure costs
        is the channel copy, which is the half that could otherwise be orphaned.
        """
        import kiro_crew.dashboard.chat_handlers as handlers_mod

        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "durable-write-raises")
        slot._pending_context.clear()
        dispatched: list[object] = []
        monkeypatch.setattr(
            handlers_mod, "dispatch_note_mirror", lambda *a, **kw: dispatched.append(a)
        )

        # Forces the dispatchable branch, which is the only one that awaits the durable
        # write; without it the note never reaches the failure this test is about.
        monkeypatch.setattr(
            handlers_mod, "snapshot_note_destinations", lambda *a, **kw: (("telegram", "c1"), None)
        )

        async def _save_raises(state_, slot_, *a, **kw):
            raise OSError("disk is gone")

        monkeypatch.setattr(handlers_mod, "save_slot_off_loop", _save_raises)

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/durable-write-raises/note",
                json={"content": "acknowledged but not on disk"},
            )
            body = await resp.json()

        assert dispatched == [], (
            "the channel mirror was dispatched after the forced durable write raised, so a "
            f"channel note can assert a line the transcript may lose; {dispatched} {body}"
        )

    @pytest.mark.asyncio
    async def test_a_flip_to_deferred_rechecks_the_context_cap(self, tmp_path: Path):
        """A queue that filled during the lock wait must not have this entry evict from it.

        The source cap is evaluated before the commit lock is taken. On a flip to the hold
        path the pre-wait decision would otherwise stand, so an entry admitted against a
        clear cap lands in a queue that filled meanwhile and displaces one already accepted.
        """
        import asyncio as _asyncio

        from kiro_crew.dashboard.chat_handlers import _MAX_CONTEXT_PER_SOURCE

        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "flip-context-cap")
        slot._pending_context.clear()
        await slot._note_commit_lock.acquire()

        async with self._make_client(state) as client:
            posting = _asyncio.ensure_future(
                client.post(
                    "/api/chat/slots/flip-context-cap/note",
                    json={"content": "arrives while the queue fills", "source": "cron"},
                )
            )
            await _asyncio.sleep(0.05)
            # The queue fills to the per-source cap while the note waits on the lock.
            for i in range(_MAX_CONTEXT_PER_SOURCE):
                slot.append_pending_context(
                    {
                        "content": f"already accepted {i}",
                        "source": "cron",
                        "injectedAt": time.time(),
                        "maxAge": 3600,
                    }
                )
            slot._in_stage_execution = True
            slot._note_commit_lock.release()
            resp = await posting
            body = await resp.json()

        assert body.get("contextSkipped") is True, (
            "the note was admitted against a cap read before the lock wait, so its context "
            f"half displaces an already-accepted entry; got {body}"
        )
        survivors = [
            e for e in slot._pending_context if "already accepted" in str(e.get("content"))
        ]
        assert (
            len(survivors) == _MAX_CONTEXT_PER_SOURCE
        ), f"an accepted entry was evicted by the flipped note; {len(survivors)} survived"

    def test_a_cancelled_committed_note_is_published_not_just_released(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A cancelled note whose save committed must be broadcast, not silently released.

        The row is appended with `broadcast=False` pending the durability decision. Releasing
        the context half without broadcasting leaves a turn citing a line no observer saw --
        the note exists in the transcript and never appeared in the conversation.
        """
        import kiro_crew.dashboard.chat_handlers as handlers_mod

        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "cancelled-but-committed")
        slot._pending_context.clear()
        shown: list[object] = []
        monkeypatch.setattr(
            type(slot), "broadcast_appended_row", lambda self, row: shown.append(row)
        )
        row = {"role": "inject", "content": "committed under cancellation"}
        entry = {"content": "committed under cancellation", "awaitingCommit": True}

        handlers_mod._resolve_cancelled_note_commit(slot, row, entry, True, True)

        assert "awaitingCommit" not in entry, "a committed note's context must be released"
        assert shown == [row], (
            "the context half was released without broadcasting the visible row, so a turn "
            f"cites a line no observer ever saw; shown={shown}"
        )

    def test_a_provisional_entry_survives_a_drain_after_its_ttl_elapses(
        self, tmp_path: Path
    ) -> None:
        """An `awaitingCommit` entry must be held even once its TTL has elapsed.

        A short-TTL note whose durable save runs long can have its lifetime expire while it
        is still provisional. Checking expiry first discards it before the hold applies, so
        the note's context is lost permanently without ever having been offered to a turn --
        and the hold exists precisely because the note is not yet committed.
        """
        import kiro_crew.dashboard.chat_runner as runner_mod

        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "provisional-ttl")
        slot._pending_context.clear()
        entry = {
            "content": "provisional and past its ttl",
            "source": "cron",
            "injectedAt": time.time() - 600,
            "maxAge": 1,
            "awaitingCommit": True,
        }
        slot._pending_context.append(entry)

        rendered = runner_mod.drain_pending_context(slot)

        assert "provisional and past its ttl" not in rendered, (
            "a provisional entry was emitted to a turn before its note committed; it could "
            "cite a transcript line that is still withdrawable"
        )
        assert any(q is entry for q in slot._pending_context), (
            "the provisional entry was expired away before the hold applied, so a slow "
            "durable write silently discards the note's context half"
        )

    @pytest.mark.asyncio
    async def test_a_written_row_reports_appended_even_when_the_forced_save_fails(
        self, tmp_path: Path, monkeypatch
    ):
        """`appended` means "the visible line was written", not "the write reached disk".

        Folding durability into it forked the field's meaning by hidden server state and
        needed a second field to disambiguate, whose documented handling was identical to
        `appended: true`. The protection against a channel note outliving its line is the
        withheld mirror dispatch, not the response flag; retry stays keyed to the two
        genuine not-accepted signals, `503` and `404`.
        """
        import kiro_crew.dashboard.chat_handlers as handlers_mod

        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "not-durable-contract")
        slot._pending_context.clear()
        monkeypatch.setattr(
            handlers_mod,
            "snapshot_note_destinations",
            lambda *a, **kw: (("telegram", "c1"), None),
        )

        async def _save_raises(state_, slot_, *a, **kw):
            raise OSError("disk is gone")

        monkeypatch.setattr(handlers_mod, "save_slot_off_loop", _save_raises)

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/not-durable-contract/note",
                json={"content": "written but not durable"},
            )
            body = await resp.json()

        assert body["appended"] is True, (
            "a written row must report appended:true -- durability is not what this field "
            f"means, and the mirror dispatch is what a failed save withholds; {body}"
        )
        assert body["visibleDeferred"] is False, f"the note was not held; {body}"
        assert "visibleNotDurable" not in body, (
            "the response must not carry a durability field: its documented handling is "
            f"identical to appended:true, so it told a caller nothing; {body}"
        )
        rows = [m for m in slot.messages if "written but not durable" in str(m.get("content"))]
        assert rows, "precondition: the row must actually be in the window for this to matter"

    @pytest.mark.asyncio
    async def test_provisional_note_is_hidden_from_slot_detail_during_the_durability_await(
        self, tmp_path: Path, monkeypatch
    ):
        """A slot-detail read landing inside the durability await must not see the note.

        The immediate path appends the visible row into slot.messages
        (broadcast=False, defer_stream=True) and only then awaits the forced save.
        Those flags withhold the stream and the push, but _prepare_messages
        rebuilds off slot.messages, so a fetch in the await window would return
        the row as settled history -- and if a rebind lands there the write path
        withdraws it, so serving it hands one session's note to its replacement.
        """
        import kiro_crew.dashboard.chat_handlers as handlers_mod
        from kiro_crew.dashboard.chat_utils import _prepare_messages

        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "provisional-read-window")
        slot._pending_context.clear()
        monkeypatch.setattr(
            handlers_mod,
            "snapshot_note_destinations",
            lambda *a, **kw: (("telegram", "c1"), None),
        )

        seen_in_window: dict[str, object] = {}

        # Runs inside the durability await, where a slot-detail fetch would land.
        async def _snapshot_then_durable(state_, slot_, *a, **kw):
            projected = _prepare_messages(list(slot_.messages), slot_.running, live_child="")
            seen_in_window["projection_has_note"] = any(
                "leaked mid-await" in str(m.get("content")) for m in projected
            )
            seen_in_window["window_has_note"] = any(
                "leaked mid-await" in str(m.get("content")) for m in slot_.messages
            )
            return True, True, False

        monkeypatch.setattr(handlers_mod, "_immediate_note_is_durable", _snapshot_then_durable)

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/provisional-read-window/note",
                json={"content": "leaked mid-await"},
            )
            assert resp.status == 200, await resp.text()

        assert seen_in_window["window_has_note"] is True, (
            "precondition: the provisional row must be in slot.messages during the await -- "
            "otherwise this test cannot exercise the read-projection leak"
        )
        assert seen_in_window["projection_has_note"] is False, (
            "the provisional note was returned by the slot-detail read projection while its "
            "durability was still awaiting -- a fetch here (or a rebind-driven withdrawal) "
            "would serve it as another session's settled history"
        )
        # Released once settled, so the projection shows the confirmed row.
        after = _prepare_messages(list(slot.messages), slot.running, live_child="")
        assert any("leaked mid-await" in str(m.get("content")) for m in after), (
            "a confirmed note must appear in the read projection after release; the gate "
            "must hide only the provisional window, not the settled row"
        )

    @pytest.mark.asyncio
    async def test_a_confirmed_note_survives_a_restart_rather_than_being_hidden_forever(
        self, tmp_path: Path, monkeypatch
    ):
        """The provisional marker must never reach disk.

        The forced durable save runs BEFORE broadcast_appended_row strips the
        marker, and _save_slot_to_history then clears _dirty -- so a persisted
        marker is never corrected on disk. A restart would restore the row with
        the marker still set and _prepare_messages would hide it permanently,
        losing an acknowledged visible line.
        """
        import kiro_crew.dashboard.chat_handlers as handlers_mod
        from kiro_crew.dashboard.chat_utils import _prepare_messages

        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "survives-restart")
        slot._pending_context.clear()
        monkeypatch.setattr(
            handlers_mod,
            "snapshot_note_destinations",
            lambda *a, **kw: (("telegram", "c1"), None),
        )

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/survives-restart/note",
                json={"content": "must survive the restart"},
            )
            assert resp.status == 200, await resp.text()

        # NO save here on purpose: the forced durability save inside the request
        # already wrote this row, and it ran BEFORE the marker was stripped.
        slot_key = slot.key

        # The restart: a fresh slot rehydrated from what is actually on disk.
        fresh_state = _make_state(tmp_path)
        fresh = _rehydrate_slot_from_history(fresh_state, slot_key)
        assert fresh is not None, "precondition: the slot must rehydrate from disk"

        restored = [
            m for m in fresh.messages if "must survive the restart" in str(m.get("content"))
        ]
        assert restored, (
            "precondition: the note must be on disk and restored into the window -- "
            "otherwise this test cannot exercise the permanent-hiding regression"
        )
        assert all(
            not (isinstance(m.get("meta"), dict) and m["meta"].get("provisional")) for m in restored
        ), (
            "the provisional marker was persisted, so the restored row carries it and the "
            "read projection hides an acknowledged note forever"
        )
        projected = _prepare_messages(list(fresh.messages), fresh.running, live_child="")
        assert any("must survive the restart" in str(m.get("content")) for m in projected), (
            "a confirmed note vanished from the read projection after a restart -- the "
            "visible line the 200 promised is permanently invisible"
        )

    @pytest.mark.asyncio
    async def test_a_rebind_after_the_durability_await_blocks_the_broadcast(
        self, tmp_path: Path, monkeypatch
    ):
        """A rebind landing after the durability task returns must not retarget the row.

        The task checks routing inside itself, then the parent broadcasts using that
        captured value. A cron rebind landing in the scheduling gap between the shield
        returning and the broadcast would publish the old session's row from the new
        session's window, and release its context half there too.
        """
        import kiro_crew.dashboard.chat_handlers as handlers_mod

        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "rebind-after-await")
        slot._pending_context.clear()
        broadcast: list[object] = []
        monkeypatch.setattr(
            type(slot), "broadcast_appended_row", lambda self, row: broadcast.append(row)
        )
        monkeypatch.setattr(
            handlers_mod,
            "snapshot_note_destinations",
            lambda *a, **kw: (("telegram", "c1"), None),
        )

        real_key = handlers_mod.slot_history_key

        # The rebind lands exactly once, as the durability task reports success -- inside
        # the scheduling gap between that return and the broadcast.
        async def _durable_then_rebind(state_, slot_, *a, **kw):
            slot_.linked_session_key = "telegram:someone-else"
            return True, True, False

        monkeypatch.setattr(handlers_mod, "_immediate_note_is_durable", _durable_then_rebind)

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/rebind-after-await/note",
                json={"content": "authored before the rebind"},
            )
            assert resp.status in (200, 404)

        assert broadcast == [], (
            "the row was broadcast after the slot was rebound, so the old session's note "
            f"is published from the new session's window; {broadcast}"
        )
        drainable = [
            e
            for e in slot._pending_context
            if "authored before the rebind" in str(e.get("content")) and not e.get("awaitingCommit")
        ]
        assert (
            drainable == []
        ), f"its context half was released into the rebound session; {drainable}"
        assert real_key is handlers_mod.slot_history_key

    def test_note_reauthorizes_after_the_commit_lock(self) -> None:
        """The ownership gate must run again AFTER the commit lock is acquired.

        The pre-lock gate predates the wait, while the session stamp and the
        ``routing_intact`` comparison are captured after it, so a cron or workflow rebind
        landing in that window would be stamped and validated against the NEW session --
        the comparison then holds against itself and app content lands in a session the
        caller does not own. Driving this over HTTP needs the app-auth middleware that
        populates ``request.get("app")``; a dashboard-user request returns early from the
        gate, so it would pass whether or not the re-check exists. This pins the ordering
        instead: the re-check must appear after the acquire, and release before returning.
        """
        import inspect

        from kiro_crew.dashboard.chat_handlers import api_chat_slot_note

        src = inspect.getsource(api_chat_slot_note)
        acquire = src.index("await slot._note_commit_lock.acquire()")
        needle = '_reauthorize_after_await(state, slot, name, request_app, "note_post")'
        post_wait = src[acquire:]
        assert post_wait.count(needle) >= 2, (
            "the ownership gate must be re-run after the commit lock on BOTH post-wait "
            "branches; the deferred-transition branch stamps the note's session and history "
            "key from the possibly-rebound slot, so skipping it there validates a rebind "
            "against itself"
        )
        # The not-deferred branch still HOLDS the lock, so its denial must release first.
        last = post_wait.rindex(needle)
        assert "_note_commit_lock.release()" in post_wait[last : last + 400], (
            "the still-holding branch's denial path must release the commit lock before "
            "returning, or a refused note leaves the lock held and every later note blocks"
        )

    @pytest.mark.asyncio
    async def test_an_oversized_note_flipping_to_deferred_is_refused(self, tmp_path: Path):
        """A note inside the immediate bound must be refused if a turn flips it to held.

        The size guards run under the pre-lock decision, so a note between the held bound
        and the larger immediate bound passes them. When the post-acquire re-read flips it
        to the hold path it is persisted verbatim, and the restore sanitizer drops it --
        silently losing a note the 200 acknowledged.
        """
        import asyncio as _asyncio

        from kiro_crew.dashboard.slot_buffers import MAX_DEFERRED_NOTE_CHARS

        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "oversized-flip")
        slot._pending_context.clear()
        oversized = "x" * (MAX_DEFERRED_NOTE_CHARS + 200)
        await slot._note_commit_lock.acquire()

        async with self._make_client(state) as client:
            posting = _asyncio.ensure_future(
                client.post(
                    "/api/chat/slots/oversized-flip/note",
                    json={"content": oversized},
                )
            )
            await _asyncio.sleep(0.05)
            slot._in_stage_execution = True
            slot._note_commit_lock.release()
            resp = await posting
            body = await resp.json()

        assert resp.status == 413, (
            "a note too large for the hold path was accepted once a turn flipped it "
            f"there, so a restart drops it; status={resp.status} body={body}"
        )
        assert body["code"] == "deferred_note_too_large"
        assert slot._deferred_notes == [], (
            "the oversized note was persisted into the hold queue, where the restore "
            f"sanitizer drops it; {slot._deferred_notes}"
        )

    def test_no_second_consumer_can_deliver_a_staged_note(self) -> None:
        """Only the drain may consume the context queue, and it must honour the hold.

        A staged note is invisible to a turn because ONE function reads the queue for
        delivery and that function holds a flagged entry. That is an invariant, not an
        accident: a second consumer added later would deliver a note whose visible half
        can still be withdrawn, and no test elsewhere would notice. This pins both halves
        -- the hold itself, and the fact that nothing else drains.
        """
        import inspect
        from pathlib import Path as _Path

        import kiro_crew.dashboard.chat_runner as runner_mod

        drain_src = inspect.getsource(runner_mod.drain_pending_context)
        assert 'entry.get("awaitingCommit")' in drain_src, (
            "the only delivery path stopped honouring the staged-note hold, so a turn can "
            "take a note whose visible half may yet be withdrawn"
        )
        root = _Path(runner_mod.__file__).parent
        readers = {
            path.name
            for path in root.glob("*.py")
            if "_pending_context" in path.read_text(encoding="utf-8")
        }
        assert readers == {
            "chat_handlers.py",  # produces entries and withdraws its own
            "chat_note_mirror.py",  # docstring reference only
            "chat_runner.py",  # the single drain
            "slot_buffers.py",  # append/evict/purge mechanics
            "state.py",  # the queue itself
        }, (
            "a new module touches the pending-context queue; if it DELIVERS entries it must "
            f"skip any carrying awaitingCommit, as drain_pending_context does; got {readers}"
        )

    @pytest.mark.asyncio
    async def test_a_slow_commit_neither_blocks_a_turn_nor_loses_its_note(
        self, tmp_path: Path, monkeypatch
    ):
        """A note whose save outlasts any cap must still reach a turn, and block none.

        Capping the wait made inclusion depend on wall-clock luck: a save slower than the
        cap let the drain proceed without the note, so it landed a turn late. The context
        half is now queued before the save and HELD by the drain until the commit clears
        it, so a turn never waits and the note is never skipped -- the interleaving is
        impossible rather than unlikely.
        """
        import asyncio as _asyncio

        import kiro_crew.dashboard.chat_handlers as handlers_mod
        import kiro_crew.dashboard.chat_runner as runner_mod

        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "slow-commit-one-turn")
        slot._pending_context.clear()
        released = _asyncio.Event()
        save_completed = {"v": False}

        async def _save_slower_than_any_cap(state_, slot_, *a, **kw):
            await released.wait()
            save_completed["v"] = True
            return True

        monkeypatch.setattr(handlers_mod, "save_slot_off_loop", _save_slower_than_any_cap)
        observed: dict[str, object] = {}

        async def _a_turn_drains_mid_commit():
            await _asyncio.sleep(0.05)
            mid = runner_mod.drain_pending_context(slot)
            observed["save_done_when_drain_returned"] = save_completed["v"]
            observed["mid_had_note"] = "the slow note" in mid
            released.set()

        async with self._make_client(state) as client:
            resp, _ = await _asyncio.gather(
                client.post(
                    "/api/chat/slots/slow-commit-one-turn/note",
                    json={"content": "the slow note"},
                ),
                _a_turn_drains_mid_commit(),
            )
            assert resp.status == 200

        assert (
            observed["save_done_when_drain_returned"] is False
        ), "the drain returned only after the commit completed, so a slow save stalls a turn"
        assert not observed[
            "mid_had_note"
        ], "the mid-commit drain emitted a note whose visible half could still be withdrawn"
        after = runner_mod.drain_pending_context(slot)
        assert "the slow note" in after, (
            "the note was dropped rather than held: a drain during the commit must leave "
            f"it queued for the next one; got {after!r}"
        )

    @pytest.mark.asyncio
    async def test_overlapping_notes_queue_context_in_submission_not_completion_order(
        self, tmp_path: Path, monkeypatch
    ):
        """Concurrent same-slot notes must reach the model in transcript order.

        The context half is queued after the durability await, so without serialization
        the note whose save settles FIRST queues first -- and if the later submission
        settles sooner the model reads the two background blocks reversed against the
        transcript, citing an older note as though it came after a newer one.
        """
        import asyncio as _asyncio

        import kiro_crew.dashboard.chat_handlers as handlers_mod

        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "reverse-settling-notes")
        slot._pending_context.clear()

        settle_order: list[str] = []
        calls = {"n": 0}

        async def _save_settles_out_of_order(state_, slot_, *a, **kw):
            calls["n"] += 1
            mine = calls["n"]
            await _asyncio.sleep(0.25 if mine == 1 else 0.02)
            settle_order.append(f"save{mine}")
            return True

        monkeypatch.setattr(handlers_mod, "save_slot_off_loop", _save_settles_out_of_order)

        async with self._make_client(state) as client:

            async def _post(label: str):
                return await client.post(
                    "/api/chat/slots/reverse-settling-notes/note",
                    json={"content": label},
                )

            first, second = await _asyncio.gather(_post("first note"), _post("second note"))
            assert (first.status, second.status) == (200, 200)

        queued = [str(e.get("content", "")) for e in slot._pending_context]
        assert len(queued) == 2, f"both notes must queue a context half; got {queued}"
        assert settle_order == ["save1", "save2"], (
            "serialization must make the first save to start settle first; without it the "
            f"second overtakes it, which is the race under test; got {settle_order}"
        )
        # Arrival order across two concurrent POSTs is not ours to fix -- the platform's
        # scheduler decides it -- so the invariant is queue order MATCHING transcript order.
        transcript = [
            str(m.get("content", "")) for m in slot.messages if m.get("cls") == "reconcile-note"
        ]
        assert len(transcript) == 2, f"both rows must be on the transcript; got {transcript}"
        assert queued == transcript, (
            "the later-settling note queued first, so the model reads the two background "
            f"blocks reversed against the transcript; queue={queued} transcript={transcript}"
        )

    @pytest.mark.asyncio
    async def test_a_same_source_add_racing_the_save_cannot_pass_the_per_source_cap(
        self, tmp_path: Path, monkeypatch
    ):
        """The per-source cap must be re-read at the commit, not only before the await.

        The pre-await gate counts this source's live entries, then the durability await
        gives another caller of the SAME source time to take the last slot. Appending on
        the stale count pushes that source one past its cap, and the next append then
        evicts a different source's context early. Both caps have to be read where the
        entry is actually queued.
        """
        import kiro_crew.dashboard.chat_handlers as handlers_mod

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "source-cap-races-the-save")
        slot._pending_context.clear()
        cap = handlers_mod._MAX_CONTEXT_PER_SOURCE
        for i in range(cap - 1):
            slot._pending_context.append(
                {"content": f"same source {i}", "source": "note", "ephemeral": True}
            )
        assert not handlers_mod._source_cap_reached(
            slot, "note"
        ), "precondition: one same-source slot free"

        async def _a_same_source_add_takes_the_last_slot(_state, observed_slot, *_a, **_k):
            observed_slot._pending_context.append(
                {"content": "same-source add mid-save", "source": "note", "ephemeral": True}
            )
            return True

        monkeypatch.setattr(
            handlers_mod, "save_slot_off_loop", _a_same_source_add_takes_the_last_slot
        )
        monkeypatch.setattr(handlers_mod, "dispatch_note_mirror", lambda *_a, **_k: None)

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/source-cap-races-the-save/note",
                json={"content": "a note that must not exceed its source cap"},
            )
            assert resp.status == 200
            body = await resp.json()

        live = [e for e in slot._pending_context if e.get("source") == "note"]
        assert len(live) <= cap, (
            f"this source now holds {len(live)} entries against a cap of {cap}, so the next "
            f"append evicts another source's context early"
        )
        assert (
            body["contextSkipped"] is True
        ), f"a source that filled during the save must report the skip: {body}"

    @pytest.mark.asyncio
    async def test_a_filler_racing_the_save_cannot_evict_another_callers_context(
        self, tmp_path: Path, monkeypatch
    ):
        """Capacity read before the await is stale by the time the note commits.

        The note's context is queued only once durability settles, and that await is long
        enough for another caller to take the last free slot. Appending then would make
        room by dropping someone else's oldest live entry, which nothing reports and no
        rollback recovers. Capacity has to be read at the commit, and a full queue reports
        contextSkipped rather than displacing a caller that got there first.
        """
        import kiro_crew.dashboard.chat_handlers as handlers_mod
        import kiro_crew.dashboard.state as state_mod

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "filler-races-the-save")
        oldest = {"content": "another caller's oldest entry", "source": "other"}
        slot._pending_context.clear()
        slot._pending_context.append(oldest)
        while len(slot._pending_context) < state_mod._MAX_PENDING_CONTEXT - 1:
            slot._pending_context.append({"content": "filler", "source": "other"})
        assert not slot.pending_context_at_capacity(), "precondition: one slot must be free"

        async def _a_concurrent_caller_takes_the_last_slot(_state, observed_slot, *_a, **_k):
            observed_slot._pending_context.append(
                {"content": "took the last slot mid-save", "source": "other"}
            )
            return True

        monkeypatch.setattr(
            handlers_mod, "save_slot_off_loop", _a_concurrent_caller_takes_the_last_slot
        )
        monkeypatch.setattr(handlers_mod, "dispatch_note_mirror", lambda *_a, **_k: None)

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/filler-races-the-save/note",
                json={"content": "a note that must not displace anyone"},
            )
            assert resp.status == 200
            body = await resp.json()

        assert (
            slot._pending_context[0] is oldest
        ), "the note evicted another caller's oldest live entry to make room for itself"
        assert (
            body["contextSkipped"] is True
        ), f"a queue that filled during the save must report the skip: {body}"
        assert not [
            e for e in slot._pending_context if "must not displace" in str(e.get("content", ""))
        ], "the note's context was queued despite the queue being full at commit"

    @pytest.mark.asyncio
    async def test_a_full_context_queue_skips_rather_than_evicting_another_caller(
        self, tmp_path: Path, monkeypatch
    ):
        """A note must not make room by dropping an entry it cannot give back.

        The queue evicts its oldest entries to fit a new one. That is safe for a caller
        that keeps what it queues, but a note's entry can be rolled back by a routing
        race, and the rollback cannot restore what the append evicted -- another
        caller's queued context would be gone for a note that was then refused. At
        capacity the note reports contextSkipped instead, leaving the queue untouched.
        """
        import kiro_crew.dashboard.state as state_mod

        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "context-queue-full")
        oldest = {"content": "another caller's oldest entry", "source": "other"}
        slot._pending_context.clear()
        slot._pending_context.append(oldest)
        while len(slot._pending_context) < state_mod._MAX_PENDING_CONTEXT:
            slot._pending_context.append({"content": "filler", "source": "other"})
        assert slot.pending_context_at_capacity(), "precondition: the queue must be full"

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/context-queue-full/note",
                json={"content": "a note arriving at a full queue"},
            )
            assert resp.status == 200
            body = await resp.json()

        assert body["contextSkipped"] is True, f"a full queue must report the skip: {body}"
        assert (
            slot._pending_context[0] is oldest
        ), "the note evicted another caller's oldest entry to make room for itself"
        assert not [
            e for e in slot._pending_context if "full queue" in str(e.get("content", ""))
        ], "the note's context was queued despite the cap"

    @pytest.mark.asyncio
    async def test_two_held_rows_release_in_append_order_either_way_round(
        self, tmp_path: Path, monkeypatch
    ):
        """Two notes held at once must reach a live reader in the order they were appended.

        Each held row reserves the queue position it would have taken, and the save is an
        await, so two notes can be outstanding together. If both reserved the same index
        the second to release would land in front of the first, handing a reader an order
        the transcript never had. Checked in BOTH release orders, because which save
        settles first is not the order the notes were written in.
        """
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "two-held-rows")
        slot._pending.clear()

        first = slot.append("inject", "note one", "reconcile-note", defer_stream=True)
        second = slot.append("inject", "note two", "reconcile-note", defer_stream=True)
        assert [m.get("content") for m in slot._pending] == [], "neither may be queued yet"

        slot.broadcast_appended_row(second)
        slot.broadcast_appended_row(first)
        assert [m.get("content") for m in slot._pending] == ["note one", "note two"], (
            "released newest-first, the queue must still read in APPEND order; got "
            f"{[m.get('content') for m in slot._pending]}"
        )

        slot._pending.clear()
        third = slot.append("inject", "note three", "reconcile-note", defer_stream=True)
        fourth = slot.append("inject", "note four", "reconcile-note", defer_stream=True)
        slot.broadcast_appended_row(third)
        slot.broadcast_appended_row(fourth)
        assert [m.get("content") for m in slot._pending] == ["note three", "note four"], (
            "released oldest-first, the queue must read in append order; got "
            f"{[m.get('content') for m in slot._pending]}"
        )

    @pytest.mark.asyncio
    async def test_a_released_note_rejoins_the_stream_in_transcript_order(
        self, tmp_path: Path, monkeypatch
    ):
        """A held row must reach a live reader in the order the transcript records.

        The row enters the durable window before the guarded save and rejoins the live
        stream after it, so anything appended during that save is queued in between.
        Releasing at the tail would hand a reader the two rows inverted, and no later
        frame corrects it -- the client renders what the stream delivered.
        """
        import kiro_crew.dashboard.chat_handlers as handlers_mod

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "stream-order")

        async def _another_row_lands_inside_the_save(_state, observed_slot, *_a, **_k):
            observed_slot.append("assistant", "a reply appended during the save", "")
            return True

        monkeypatch.setattr(handlers_mod, "save_slot_off_loop", _another_row_lands_inside_the_save)
        monkeypatch.setattr(handlers_mod, "dispatch_note_mirror", lambda *_a, **_k: None)

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/stream-order/note", json={"content": "the note came first"}
            )
            assert resp.status == 200

        streamed = [
            m
            for m in slot._pending
            if m.get("cls") == "reconcile-note" or m.get("role") == "assistant"
        ]
        assert len(streamed) == 2, f"both rows must be queued for the reader; got {streamed}"
        assert streamed[0].get("cls") == "reconcile-note", (
            "the note was appended first, so a live reader must receive it first; got "
            f"{[(m.get('role'), m.get('cls')) for m in streamed]}"
        )
        window = [
            m
            for m in slot.messages
            if m.get("cls") == "reconcile-note" or m.get("role") == "assistant"
        ]
        assert [m.get("cls") for m in window] == [
            m.get("cls") for m in streamed
        ], "the live stream order must match the durable window order"

    @pytest.mark.asyncio
    async def test_a_row_landing_during_the_save_is_not_pushed_ahead_of_the_note(
        self, tmp_path: Path, monkeypatch
    ):
        """The live PUSH must not invert what the window and the reader queue order.

        A note's row is appended withheld and pushed only on release, so a row landing
        during the slow save pushed IMMEDIATELY and the note pushed after it -- leaving a
        dashboard with no stream reader showing the reply before the note it answered,
        until a reload. The window and the reader queue were already correct; only the
        push had no ordering.
        """
        import kiro_crew.dashboard.chat_handlers as handlers_mod

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "push-order")
        slot._pending_context.clear()
        pushed: list[dict] = []
        # No reader attached, which is the branch that pushes rather than queueing.
        slot._on_message = lambda _key, msg: pushed.append(msg)
        slot._has_reader = False

        async def _another_row_lands_inside_the_save(_state, observed_slot, *_a, **_k):
            observed_slot.append("assistant", "a reply appended during the save", "")
            return True

        monkeypatch.setattr(handlers_mod, "save_slot_off_loop", _another_row_lands_inside_the_save)
        monkeypatch.setattr(handlers_mod, "dispatch_note_mirror", lambda *_a, **_k: None)

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/push-order/note", json={"content": "the note came first"}
            )
            assert resp.status == 200, await resp.text()

        relevant = [
            m for m in pushed if m.get("cls") == "reconcile-note" or m.get("role") == "assistant"
        ]
        assert len(relevant) == 2, f"both rows must reach the push path; got {relevant}"
        assert relevant[0].get("cls") == "reconcile-note", (
            "the reply was pushed before the note it answered, so a dashboard with no "
            "stream reader renders them inverted; got "
            f"{[(m.get('role'), m.get('cls')) for m in relevant]}"
        )
        window = [
            m
            for m in slot.messages
            if m.get("cls") == "reconcile-note" or m.get("role") == "assistant"
        ]
        assert [m.get("cls") for m in window] == [
            m.get("cls") for m in relevant
        ], "the push order must match the durable window order"

    @pytest.mark.asyncio
    async def test_a_reader_draining_mid_save_still_sees_transcript_order(
        self, tmp_path: Path, monkeypatch
    ):
        """A reader must not receive a later row before the note it follows.

        The note is withheld from ``_pending`` until it settles, while later rows entered
        it immediately -- so an attached reader draining during the slow save took the
        later row first, and ``broadcast_appended_row``'s timestamp insert cannot recall
        a row already delivered. Ordering has to hold across the drain, not just within
        the queue.
        """
        import kiro_crew.dashboard.chat_handlers as handlers_mod

        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "reader-order")
        slot._pending_context.clear()
        observed: list[dict] = []

        # A reader consuming DURING the save: this is the drain the note cannot undo.
        async def _row_lands_then_a_reader_drains(_state, observed_slot, *_a, **_k):
            observed_slot.append("assistant", "a reply appended during the save", "")
            observed.extend(observed_slot.drain())
            return True

        monkeypatch.setattr(handlers_mod, "save_slot_off_loop", _row_lands_then_a_reader_drains)
        monkeypatch.setattr(handlers_mod, "dispatch_note_mirror", lambda *_a, **_k: None)

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/reader-order/note", json={"content": "the note came first"}
            )
            assert resp.status == 200, await resp.text()

        observed.extend(slot.drain())
        seen = [
            m for m in observed if m.get("cls") == "reconcile-note" or m.get("role") == "assistant"
        ]
        assert len(seen) == 2, f"the reader must receive both rows; got {seen}"
        assert seen[0].get("cls") == "reconcile-note", (
            "the reader received the reply before the note it follows, and a delivered row "
            f"cannot be recalled; got {[(m.get('role'), m.get('cls')) for m in seen]}"
        )
        window = [
            m
            for m in slot.messages
            if m.get("cls") == "reconcile-note" or m.get("role") == "assistant"
        ]
        assert [m.get("cls") for m in window] == [
            m.get("cls") for m in seen
        ], "the reader's order must match the durable window order"

    @pytest.mark.asyncio
    async def test_turn_ws_frames_wait_for_a_provisional_note(self, tmp_path: Path, monkeypatch):
        """Direct turn frames must not reach the socket before a withheld note.

        Turn output is broadcast straight to WS clients, bypassing ``_pending`` and the
        push, so a reply streaming inside the note's durable-save window rendered before
        the note it answers -- and a frame already on the wire cannot be recalled. The
        immediate arm is only taken on an idle, channel-bound slot, which is the window
        this drives.
        """
        import kiro_crew.dashboard.chat_handlers as handlers_mod
        from kiro_crew.dashboard.chat_runner import chunk_generation

        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "ws-order")
        slot._pending_context.clear()
        wire: list[tuple[str, object]] = []
        state.broadcast_ws = lambda event, payload: wire.append((event, payload))
        # Both deliveries land on ONE sink, which is the only way their order is comparable:
        # the note goes out via the slot push, the turn frame straight to the socket.
        slot._on_message = lambda _key, msg: wire.append(("note_push", msg))
        slot._has_reader = False
        monkeypatch.setattr(
            handlers_mod,
            "snapshot_note_destinations",
            lambda *a, **kw: (("telegram", "c1"), None),
        )

        # A turn streaming a chunk INSIDE the durability await: the real call site routes
        # its frame through the slot, so the hold is what decides the wire order.
        async def _a_turn_streams_during_the_save(_state, observed_slot, *_a, **_k):
            observed_slot.broadcast_ws_or_hold(
                _state,
                "chat_chunk",
                {
                    "slot": observed_slot.key,
                    "content": "a reply chunk",
                    "seq": 1,
                    "gen": chunk_generation(),
                },
            )
            return True

        monkeypatch.setattr(handlers_mod, "save_slot_off_loop", _a_turn_streams_during_the_save)

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/ws-order/note", json={"content": "the note came first"}
            )
            assert resp.status == 200, await resp.text()

        chunks = [i for i, (event, _p) in enumerate(wire) if event == "chat_chunk"]
        notes = [
            i
            for i, (event, p) in enumerate(wire)
            if event == "note_push" and "the note came first" in str(p)
        ]
        assert chunks, f"precondition: the turn frame must reach the wire; got {wire}"
        assert notes, f"precondition: the note must reach the wire; got {wire}"
        assert notes[0] < chunks[0], (
            "the turn's chunk reached the socket before the note it follows, and a frame "
            f"already sent cannot be recalled; wire order was {[e for e, _ in wire]}"
        )

    @pytest.mark.asyncio
    async def test_a_withdrawn_row_is_not_broadcast_when_the_context_was_consumed(
        self, tmp_path: Path, monkeypatch
    ):
        """A row removed from the window must never reach observers.

        Under the double race -- a permanent delete winning the guarded save while a
        turn has already drained the pending context -- the response stays a 200,
        because the model did receive the note and that cannot be taken back. The
        visible row is still withdrawn, though, so it has no transcript line for an
        observer to anchor on: pushing it renders a note that the same response reports
        as `appended: false`, and no later reload can reproduce it.
        """
        import kiro_crew.dashboard.chat_handlers as handlers_mod

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "withdrawn-not-broadcast")
        shown: list[dict] = []
        slot._on_message = lambda _key, msg: shown.append(msg)
        slot._has_reader = False

        async def _turn_drains_then_delete_wins(_state, observed_slot, *_a, **_k):
            observed_slot._pending_context.clear()
            return False

        monkeypatch.setattr(handlers_mod, "save_slot_off_loop", _turn_drains_then_delete_wins)
        monkeypatch.setattr(handlers_mod, "session_was_deleted", lambda *_a, **_k: True)
        monkeypatch.setattr(handlers_mod, "dispatch_note_mirror", lambda *_a, **_k: None)

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/withdrawn-not-broadcast/note",
                json={"content": "a withdrawn note nobody should be shown"},
            )
            assert (
                resp.status == 404
            ), f"the context was staged, so the note took no effect; got {resp.status}"

        assert [
            m for m in shown if m.get("cls") == "reconcile-note"
        ] == [], f"a withdrawn row was pushed to observers: {shown}"
        assert [
            m for m in slot._pending if m.get("cls") == "reconcile-note"
        ] == [], "a withdrawn row was left drainable by a reader"
        assert [
            m for m in slot.messages if m.get("cls") == "reconcile-note"
        ] == [], "the withdrawn row is still in the window"

    @pytest.mark.asyncio
    async def test_a_context_consumed_during_the_save_is_not_denied_by_a_404(
        self, tmp_path: Path, monkeypatch
    ):
        """A 404 must not deny an effect the model already received.

        The context half is queued before the durable save so a turn admitted during
        that save drains it with the note. That same interleaving means a turn can
        consume it and the save can THEN be refused by a session delete. The visible
        row is recallable; a context already folded into a prompt is not, so answering
        404 -- the note took no effect -- would be false. The honest answer keeps the
        200 and reports the halves: nothing appended, delivery conditional.
        """
        import kiro_crew.dashboard.chat_handlers as handlers_mod

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "context-consumed-then-deleted")

        async def _turn_drains_then_delete_wins(_state, observed_slot, *_a, **_k):
            # Exactly what drain_pending_context does when a turn folds the queue into
            # its prompt, and then the guarded save loses to a permanent delete.
            observed_slot._pending_context.clear()
            return False

        monkeypatch.setattr(handlers_mod, "save_slot_off_loop", _turn_drains_then_delete_wins)
        monkeypatch.setattr(handlers_mod, "session_was_deleted", lambda *_a, **_k: True)
        monkeypatch.setattr(handlers_mod, "dispatch_note_mirror", lambda *_a, **_k: None)

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/context-consumed-then-deleted/note",
                json={"content": "a note whose context a turn already consumed"},
            )
            assert resp.status == 404, (
                f"the context was STAGED, so a turn could not have consumed it and the "
                f"note took no effect; got {resp.status}"
            )

        assert [
            m for m in slot.messages if m.get("cls") == "reconcile-note"
        ] == [], "the refused visible row is still in the window"
        assert not [
            e for e in slot._pending_context if "already consumed" in str(e.get("content", ""))
        ], "the staged context must never reach the queue when the note is refused"

    @pytest.mark.asyncio
    async def test_a_note_lost_to_delete_is_never_shown_and_leaves_no_half(
        self, tmp_path: Path, monkeypatch
    ):
        """A 404-ed note must not have been broadcast, and must leave nothing behind.

        The visible row has to be appended before the guarded save, because that save
        is what persists it -- so showing it at append time would let observers read a
        note whose caller is then told the session does not exist. Broadcasting is
        therefore deferred until persistence settles, and the refused row is withdrawn
        along with its context half.
        """
        import kiro_crew.dashboard.chat_handlers as handlers_mod

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "lost-to-delete-broadcast")
        shown: list[dict] = []
        slot._on_message = lambda _key, msg: shown.append(msg)
        slot._has_reader = False

        async def _save_refuses(*_a, **_k):
            return False

        monkeypatch.setattr(handlers_mod, "save_slot_off_loop", _save_refuses)
        monkeypatch.setattr(handlers_mod, "session_was_deleted", lambda *_a, **_k: True)
        monkeypatch.setattr(handlers_mod, "dispatch_note_mirror", lambda *_a, **_k: None)

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/lost-to-delete-broadcast/note",
                json={"content": "a note for a session that is being deleted"},
            )
            assert resp.status == 404, f"a lost note must answer 404; got {resp.status}"

        assert [
            m for m in shown if m.get("cls") == "reconcile-note"
        ] == [], f"observers were shown a note the caller was told does not exist: {shown}"
        assert [
            m for m in slot.messages if m.get("cls") == "reconcile-note"
        ] == [], "the refused row is still in the window"
        assert not [
            e for e in slot._pending_context if "being deleted" in str(e.get("content", ""))
        ], "the context half outlived the 404"

    @pytest.mark.asyncio
    async def test_a_rebind_after_the_save_commits_is_not_reported_unconditional(
        self, tmp_path: Path, monkeypatch
    ):
        """A committed write does not make delivery unconditional if routing then moved.

        The save pins its write to the authorized key, so a commit proves the ROW
        landed there -- but a rebind can land after it, and the drain answers to the
        slot's new routing. Reporting ``deliveryConditional`` false there promises a
        delivery to a session the note is not routed to.
        """
        import kiro_crew.dashboard.chat_handlers as handlers_mod

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "post-commit-rebind")
        shown: list[dict] = []
        slot._on_message = lambda _key, msg: shown.append(msg)
        slot._has_reader = False

        async def _save_commits_then_rebinds(state_, slot_, *a, **kw):
            slot_.linked_session_key = "cron:8003"
            return True

        monkeypatch.setattr(handlers_mod, "save_slot_off_loop", _save_commits_then_rebinds)
        monkeypatch.setattr(handlers_mod, "dispatch_note_mirror", lambda *a, **kw: None)

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/post-commit-rebind/note",
                json={"content": "note whose routing moved after the commit"},
            )
            assert resp.status == 200
            body = await resp.json()

        assert slot.linked_session_key == "cron:8003", "the rebind must land after the commit"
        assert body["appended"] is True, "the row DID land on the authorized transcript"
        assert body["contextSkipped"] is True, (
            "the routing gate dropped the context half, so reporting contextSkipped=false "
            f"would tell the caller their note reaches a turn it can never reach; got {body}"
        )
        assert (
            not slot._pending_context
        ), "the context half must NOT be queued once routing moved off the authorized key"
        assert body["deliveryConditional"] is True, (
            f"routing moved off the authorized key after the commit, so delivery is "
            f"conditional; got {body}"
        )
        assert [
            m for m in shown if m.get("cls") == "reconcile-note"
        ] == [], f"the committed row was pushed into the session that replaced its own: {shown}"
        assert [
            m for m in slot._pending if m.get("cls") == "reconcile-note"
        ] == [], "the committed row was left drainable by a reader of the replacement session"

    @pytest.mark.asyncio
    async def test_a_delete_winning_the_save_answers_404_not_a_false_success(
        self, tmp_path: Path, monkeypatch
    ):
        """A note whose session was destroyed mid-save must not be reported appended.

        ``save_slot_off_loop`` answers the same ``False`` for a permanent deletion and
        for a rebind, so collapsing the two let the 200 claim ``appended`` for a row
        that will never reach disk -- describing a session the operator had just
        destroyed. The delete arm now answers the endpoint's uniform 404.
        """
        import kiro_crew.dashboard.chat_handlers as handlers_mod

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        _seeded_slot(state, "delete-race")
        dispatched: list[str] = []

        async def _save_refuses(state_, slot_, *a, **kw):
            return False

        monkeypatch.setattr(handlers_mod, "save_slot_off_loop", _save_refuses)
        monkeypatch.setattr(handlers_mod, "session_was_deleted", lambda state_, slot_: True)
        monkeypatch.setattr(
            handlers_mod, "dispatch_note_mirror", lambda *a, **kw: dispatched.append("sent")
        )

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/delete-race/note", json={"content": "note into a deleted session"}
            )
            body = await resp.json()

        assert resp.status == 404, (
            f"the save was refused because the session is gone, so the response must not "
            f"report success; got {resp.status} {body}"
        )
        assert body.get("code") == "slot_not_found", f"expected the uniform 404 shape, got {body}"
        assert "appended" not in body, f"a 404 must not carry an append claim: {body}"
        assert dispatched == [], "no channel note may be dispatched for a lost transcript row"

    @pytest.mark.asyncio
    async def test_a_refused_save_without_a_delete_witness_answers_200_conditionally(
        self, tmp_path: Path, monkeypatch
    ):
        """A refusal the delete witness does not confirm keeps its conditional 200.

        Guards the discrimination in the other direction -- answering 404 for every
        refused save would turn an ordinary routing move into a missing slot -- and pins
        the fail-SAFE fallback: an unconfirmed refusal reports conditional, not gone.
        """
        import kiro_crew.dashboard.chat_handlers as handlers_mod

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        _seeded_slot(state, "rebind-race")

        async def _save_refuses(state_, slot_, *a, **kw):
            return False

        monkeypatch.setattr(handlers_mod, "save_slot_off_loop", _save_refuses)
        monkeypatch.setattr(handlers_mod, "session_was_deleted", lambda state_, slot_: False)
        monkeypatch.setattr(handlers_mod, "dispatch_note_mirror", lambda *a, **kw: None)

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/rebind-race/note", json={"content": "note during a rebind"}
            )
            body = await resp.json()

        assert resp.status == 200, f"a rebind is not a missing slot; got {resp.status} {body}"
        assert (
            body["deliveryConditional"] is True
        ), f"the routing moved off the authorized key, so delivery is conditional: {body}"

    @pytest.mark.asyncio
    async def test_a_rebind_during_the_durable_write_cannot_retarget_the_mirror(
        self, tmp_path: Path, monkeypatch
    ):
        """The mirror must carry the binding the note was AUTHORED for.

        The session key and the destinations were both resolved AFTER the durability
        await, so a rebind landing inside that window was the one snapshotted -- and the
        mirror then delivered content authored for one conversation to the binding that
        replaced it, a recipient it was never authorized for.

        The rebind lands INSIDE the patched save so it falls in the real window; landing
        it before the post would be an ordinary already-bound slot and pass vacuously.
        """
        import kiro_crew.dashboard.chat_handlers as handlers_mod

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "retarget-race")
        authored_key = handlers_mod.effective_session_key(slot)
        seen: list[tuple[str, str]] = []

        async def _save_rebinds(state_, slot_, *a, **kw):
            slot_.linked_session_key = "cron:7002"
            return True

        def _fake_snapshot(state_, slot_, key_):
            # Tags WHEN it ran, so resolving after the rebind is visible in the payload.
            tag = "replacement" if slot_.linked_session_key == "cron:7002" else "authored"
            return ((tag, "C1"), None)

        def _fake_dispatch(state_, slot_, session_, content_, source_, destinations_):
            seen.append((session_, destinations_[0][0]))

        monkeypatch.setattr(handlers_mod, "save_slot_off_loop", _save_rebinds)
        monkeypatch.setattr(handlers_mod, "snapshot_note_destinations", _fake_snapshot)
        monkeypatch.setattr(handlers_mod, "dispatch_note_mirror", _fake_dispatch)

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/retarget-race/note",
                json={"content": "authored before the rebind"},
            )
            assert resp.status == 200
            assert (await resp.json())["appended"] is True, "must exercise the IMMEDIATE path"

        assert slot.linked_session_key == "cron:7002", "the rebind must land inside the window"
        assert seen == [(authored_key, "authored")], (
            f"the mirror was handed {seen} instead of the authoring binding "
            f"({authored_key!r}, 'authored'): a rebind during the durable write "
            f"retargeted the session key, the destinations, or both"
        )

    @pytest.mark.asyncio
    async def test_an_immediate_note_reaches_disk_before_its_channel_note_is_sent(
        self, tmp_path: Path, monkeypatch
    ):
        """A channel send must not be able to outlive the transcript row it claims.

        ``slot.append`` only updates the in-memory window, so a mirror dispatched
        straight after it can reach a user while an ordinary gateway crash still
        loses the transcript line, leaving an orphan channel note nothing recovers.

        Asserted AT THE DISPATCH MOMENT by reading the on-disk transcript from
        inside the fake mirror, not by recording call order: an order assertion
        also passes when the save runs first but commits nothing, which is the
        failure this is meant to catch.
        """
        import kiro_crew.dashboard.chat_handlers as handlers_mod

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _seeded_slot(state, "durable-first")
        key = slot_history_key(slot)
        durable_at_dispatch: list[bool] = []

        def _fake_dispatch(state_, slot_, session_, content_, source_, destinations_):
            rows = state.conversation_log.read_messages(key)
            durable_at_dispatch.append(any(content_ == (row.get("content") or "") for row in rows))

        monkeypatch.setattr(handlers_mod, "dispatch_note_mirror", _fake_dispatch)

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/durable-first/note",
                json={"content": "durable before mirrored"},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["appended"] is True, (
                "this test must exercise the IMMEDIATE path; a held note is not "
                "mirrored here at all and would pass vacuously"
            )

        assert durable_at_dispatch == [True], (
            "the mirror must be dispatched only once the visible line is on disk; "
            f"durable-at-dispatch={durable_at_dispatch}"
        )

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
