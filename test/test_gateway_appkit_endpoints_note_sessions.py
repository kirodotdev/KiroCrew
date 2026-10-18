"""Tests for App Kit notes across turns, sessions and cleanup.

Covers a held note owed to the next user turn (never an automatic one), an
immediate note never following an unbound slot into a foreign session, slot
cleanup persisting held notes, and the audited save-path drop of a rebound
note.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app as _make_chat_app
from chat_test_helpers import _make_state as _make_chat_state

from kiro_crew.dashboard import chat_persistence, chat_runner
from kiro_crew.dashboard.chat import api_chat_slot_note
from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
from kiro_crew.dashboard.chat_runner import drain_pending_context
from kiro_crew.dashboard.chat_utils import _history_key_for
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


class TestAutomaticSuccessorsDoNotConsumeNotes:
    """A held note is owed to the next USER turn, not to an automatic one."""

    @pytest.mark.asyncio
    async def test_an_automatic_queued_turn_does_not_consume_a_held_note(self, tmp_path: Path):
        """A cron-notification successor must leave the note for the user turn."""
        state = _make_chat_state(tmp_path)
        slot = _ChatSlot("s1")
        state._slots["s1"] = slot
        slot._deferred_notes.append(
            {"content": "owed to the next user turn", "cls": "reconcile-note", "context": None}
        )

        # Head of the queue is AUTOMATIC (carries a structural origin tag).
        slot.queue_append("cron said hello", kind="cron_notification")
        assert slot._queue and slot._queue[0].get("kind"), "fixture: head must be automatic"
        with (
            patch.object(chat_runner, "_dequeue_next_message", return_value=(None, [])),
            patch.object(chat_runner, "_dequeue_next_system_message", return_value=(None, [])),
        ):
            await chat_runner._start_next_queued_turn(state, slot)
        assert (
            slot._deferred_notes
        ), "an automatic queued turn consumed a note owed to the next user turn"

        # Head is now USER-authored (no tag), so the note IS delivered above it.
        slot._queue.clear()
        slot.queue_append("a person typed this")
        assert not slot._queue[0].get("kind"), "fixture: head must be user-authored"
        with (
            patch.object(chat_runner, "_dequeue_next_message", return_value=(None, [])),
            patch.object(chat_runner, "_dequeue_next_system_message", return_value=(None, [])),
        ):
            await chat_runner._start_next_queued_turn(state, slot)
        assert (
            slot._deferred_notes == []
        ), "a user-authored queued turn was not given the note it was owed"
        assert any("owed to the next user turn" in m.get("content", "") for m in slot.messages)


class TestImmediateNoteSessionBinding:
    """An immediate note must not follow an unbound slot into a foreign session.

    The ownership gate admits an UNBOUND slot by design -- it refuses a slot
    bound elsewhere -- and both halves of an immediate note resolve their
    destination later: the queued half at the next turn's drain, the visible row
    at the next save. A cron or workflow claiming the empty binding in between
    would otherwise absorb content authorized for the app's own session.
    """

    @asynccontextmanager
    async def _as_owner_app(self, state: DashboardState):
        app = web.Application()
        app["state"] = state

        async def _note(request):
            request["app"] = "owner-app"
            return await api_chat_slot_note(request)

        app.router.add_post("/api/chat/slots/{slot}/note", _note)
        async with TestClient(TestServer(app)) as c:
            yield c

    async def _post_immediate_note(self, client, slot, mark: str):
        resp = await client.post("/api/chat/slots/cron-42/note", json={"content": mark})
        assert resp.status == 200
        # Fixture controls: an UNBOUND slot really is admitted, and this is the
        # IMMEDIATE arm rather than the deferred one, or the test is vacuous.
        assert (await resp.json())["visibleDeferred"] is False
        assert any(mark in m.get("content", "") for m in slot.messages)
        assert len(slot._pending_context) == 1

    @pytest.mark.asyncio
    async def test_an_immediate_note_does_not_follow_a_rebound_slot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Neither half may reach the session the slot is rebound to."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_chat_state(tmp_path)
        slot = _ChatSlot("cron-42")
        slot._app = "owner-app"
        state._slots["cron-42"] = slot
        assert slot.linked_session_key == "", "fixture: the slot must start UNBOUND"

        mark = "authorized for the app's own slot"
        async with self._as_owner_app(state) as client:
            await self._post_immediate_note(client, slot, mark)

        # A cron claims the empty binding, exactly as cron_inject.py does.
        slot.linked_session_key = "cron:42"

        await save_slot_off_loop(state, slot, force=True)
        foreign = state.conversation_log.read_messages("cron:42")
        assert not any(
            mark in (m.get("content") or "") for m in foreign
        ), "the note was persisted into the foreign session's transcript"
        assert mark not in drain_pending_context(
            slot
        ), "the note reached the foreign session's next prompt"
        assert not any(
            mark in m.get("content", "") for m in slot.messages
        ), "the note is still readable in the rebound slot"

    @pytest.mark.asyncio
    async def test_an_unrebound_slots_note_is_left_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Control: the guard drops ONLY content whose session actually changed."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_chat_state(tmp_path)
        slot = _ChatSlot("cron-42")
        slot._app = "owner-app"
        state._slots["cron-42"] = slot

        mark = "note that must survive"
        async with self._as_owner_app(state) as client:
            await self._post_immediate_note(client, slot, mark)

        # Binding unchanged, so nothing may be dropped from either half.
        await save_slot_off_loop(state, slot, force=True)
        own = state.conversation_log.read_messages("dashboard:cron-42")
        assert any(
            mark in (m.get("content") or "") for m in own
        ), "the guard dropped a note whose session never changed"
        assert mark in drain_pending_context(slot)
        assert any(mark in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    async def test_a_rebind_during_the_save_does_not_retarget_the_note(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Authorization and the write target must come from ONE observation.

        The filter runs in the flush executor thread while the event loop can
        rebind the slot, so reading the routing twice authorizes the row against
        one session and then writes the file of another.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_chat_state(tmp_path)
        slot = _ChatSlot("cron-42")
        slot._app = "owner-app"
        state._slots["cron-42"] = slot

        mark = "authorized before the cron claimed the slot"
        async with self._as_owner_app(state) as client:
            await self._post_immediate_note(client, slot, mark)

        # A cron claims the binding DURING the save: flip it the first time the
        # authorization predicate is consulted, which on a per-row read sits
        # between the filter and the write-target resolution.
        real = chat_persistence._note_authorized_elsewhere
        consulted: list[bool] = []

        def _rebind_mid_save(stamped, live_session):
            if not consulted:
                consulted.append(True)
                slot.linked_session_key = "cron:42"
            return real(stamped, live_session)

        monkeypatch.setattr(chat_persistence, "_note_authorized_elsewhere", _rebind_mid_save)
        await save_slot_off_loop(state, slot, force=True)
        assert consulted, "fixture: the authorization predicate was never consulted"

        foreign = state.conversation_log.read_messages("cron:42")
        assert not any(
            mark in (m.get("content") or "") for m in foreign
        ), "a rebind during the save retargeted the note into the foreign transcript"
        # Non-vacuity: the note must land in its OWN session rather than nowhere.
        own = state.conversation_log.read_messages("dashboard:cron-42")
        assert any(
            mark in (m.get("content") or "") for m in own
        ), "the note was dropped everywhere instead of landing in its own session"


class TestCleanupPersistsHeldNotes:
    """POST /api/chat/slots/cleanup must not archive an accepted note away.

    The bulk archive pass writes each stale slot's final record and only
    cancels its running turn afterwards. A note still held for the next turn is
    therefore flushed into a slot that has already been saved and removed from
    the registry, so the content behind a 200 never reaches the transcript.
    """

    @asynccontextmanager
    async def _make_client(self, state: DashboardState):
        app = _make_chat_app(state)
        app.router.add_post("/api/chat/slots/{slot}/note", api_chat_slot_note)
        async with TestClient(TestServer(app)) as c:
            yield c

    @pytest.mark.asyncio
    async def test_cleanup_does_not_feed_a_held_note_to_the_doomed_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The context half must not be drained by a turn about to be cancelled.

        The flush promotes a held note's context into ``_pending_context``, and
        the archival save is an await the still-running turn can resume across.
        That turn drains and CLEARS the queue, then gets cancelled -- so the
        context reaches nobody. The visible row survives either way, which is why
        the sibling test above cannot see this.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.autonudge.get_instance",
            lambda: MagicMock(list_all=MagicMock(return_value=[])),
        )
        state = _make_chat_state(tmp_path)
        # flake-ok: pre-existing wall-clock read minus nine days that stamps the user row so the cleanup pass with a three-day cutoff sees the slot as stale, moved verbatim; tracked for a de-flake pass
        old_ts = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
        slot = state.get_or_create_slot("stale-2")
        slot.append("user", "work from last week", ts=old_ts)
        slot.drain()

        drained: list[str] = []

        async def _draining_turn():
            # Stands in for the live turn: _run_chat calls drain_pending_context,
            # which builds the prefix and clears the queue.
            # flake-ok: pre-existing unbounded while-True poll loop in the stand-in turn that drains pending context until the test cancels the task, moved verbatim; tracked for a de-flake pass
            while True:
                got = drain_pending_context(slot)
                if got:
                    drained.append(got)
                # flake-ok: pre-existing zero-second sleep that yields the stand-in turn to the event loop so it can drain pending context across the cleanup awaits, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0)

        turn = asyncio.get_running_loop().create_task(_draining_turn())
        slot.task = turn
        mark = "context owed to the next turn"
        try:
            async with self._make_client(state) as client:
                resp = await client.post("/api/chat/slots/stale-2/note", json={"content": mark})
                assert resp.status == 200
                assert (await resp.json())["visibleDeferred"] is True
                assert len(slot._deferred_notes) == 1, "fixture: the note must be HELD"

                resp = await client.post("/api/chat/slots/cleanup", json={"max_inactive_days": 3})
                assert (await resp.json())["keys"] == [
                    "stale-2"
                ], "fixture: the stale slot must actually be archived"
        finally:
            turn.cancel()

        assert drained == [], "the doomed turn drained the note's context half before cancellation"
        assert any(
            mark in (e.get("content") or "") for e in slot._pending_context
        ), "the note's context half was consumed and is now owed to nobody"
        # Regression: the visible row still reaches the archived record.
        persisted = state.conversation_log.read_messages_chained(_history_key_for("stale-2"))
        assert any(mark in (m.get("content") or "") for m in persisted)

    @pytest.mark.asyncio
    async def test_a_stale_slots_held_note_survives_bulk_cleanup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A note accepted with 200 must be in the archived record."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.autonudge.get_instance",
            lambda: MagicMock(list_all=MagicMock(return_value=[])),
        )
        state = _make_chat_state(tmp_path)
        # flake-ok: pre-existing wall-clock read minus nine days that stamps the user row so the bulk cleanup with a three-day cutoff archives the slot, moved verbatim; tracked for a de-flake pass
        old_ts = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
        slot = state.get_or_create_slot("stale-1")
        slot.append("user", "work from last week", ts=old_ts)
        slot.drain()

        # A stale slot whose turn is still running: the note defers, and the
        # cleanup pass cancels that turn only AFTER its final save.
        # flake-ok: pre-existing 30 s sleep task that stands in for a still-running turn so the note defers until cleanup cancels it, moved verbatim; tracked for a de-flake pass
        turn = asyncio.get_running_loop().create_task(asyncio.sleep(30))
        slot.task = turn
        try:
            async with self._make_client(state) as client:
                resp = await client.post(
                    "/api/chat/slots/stale-1/note",
                    json={"content": "note accepted before archive"},
                )
                assert resp.status == 200
                assert (await resp.json())["visibleDeferred"] is True
                assert len(slot._deferred_notes) == 1

                resp = await client.post("/api/chat/slots/cleanup", json={"max_inactive_days": 3})
                assert (await resp.json())["keys"] == [
                    "stale-1"
                ], "fixture did not archive the stale slot"
        finally:
            turn.cancel()

        persisted = [
            m.get("content")
            for m in state.conversation_log.read_messages_chained(_history_key_for("stale-1"))
        ]
        assert (
            "note accepted before archive" in persisted
        ), "the archived record lost a note the endpoint had accepted"

    @pytest.mark.asyncio
    async def test_a_failed_archive_reports_the_turn_it_already_cancelled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A rolled-back slot must not return silently missing its turn.

        The cancel is deliberately BEFORE the archival save (so the doomed turn
        cannot drain the note's context half), which means a save that then fails
        restores a slot whose turn is already dead. ``running`` derives from the
        task, so a completed cancel reads False and the tab looks idle and
        dispatchable -- the turn's output is gone with nothing on the rollback path
        saying so. Reverting the cancel is not the fix: it would delete the bulk
        cleanup's only flush, which the sibling tests above pin.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.autonudge.get_instance",
            lambda: MagicMock(list_all=MagicMock(return_value=[])),
        )
        state = _make_chat_state(tmp_path)
        # flake-ok: pre-existing wall-clock read minus nine days that stamps the user row so the cleanup pass with a three-day cutoff selects the slot for archive, moved verbatim; tracked for a de-flake pass
        old_ts = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
        slot = state.get_or_create_slot("stale-3")
        slot.append("user", "work from last week", ts=old_ts)
        slot.drain()

        # flake-ok: pre-existing 30 s sleep task that stands in for a live turn the cleanup pass cancels before its failing archive save, moved verbatim; tracked for a de-flake pass
        turn = asyncio.get_running_loop().create_task(asyncio.sleep(30))
        slot.task = turn

        async def _failing_save(*a, **k):
            raise RuntimeError("archive write failed")

        try:
            with patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", new=_failing_save):
                async with self._make_client(state) as client:
                    resp = await client.post(
                        "/api/chat/slots/cleanup", json={"max_inactive_days": 3}
                    )
                    body = await resp.json()
        finally:
            turn.cancel()

        assert body.get("failed") == ["stale-3"], f"fixture: archive must fail: {body}"
        assert body.get("keys") == [], "fixture: nothing should have archived"
        assert state._slots.get("stale-3") is slot, "fixture: the slot must be restored"
        assert turn.done(), "fixture: the turn must have been cancelled before the save"

        errors = [m.get("content") or "" for m in slot.messages if m.get("role") == "error"]
        assert any(
            "did not finish" in e for e in errors
        ), "the rollback restored a slot whose turn was killed, and said nothing"
        assert slot.task is None, "the dead task is still presented as this slot's live turn"


class TestPersistenceRebindDenialIsAudited:
    """The save-path drop of a rebound note must record a SEL denial.

    ``_note_authorized_elsewhere`` has three call sites. The two drain-side ones
    share a single count-gated denial (``_ChatSlot.drain``, ``state_parts/slot_turns.py``); the save-path filter
    was the only unaudited one, so a rebind landing between the write and the next
    periodic save dropped the row with nothing recorded anywhere.

    Lives here rather than in a persistence module because the SEL-denial spy
    idiom and the rest of this endpoint's note coverage already do, which also
    keeps the PR's file set unchanged.
    """

    def test_a_save_path_rebind_drop_records_a_sel_denial(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from types import SimpleNamespace

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_chat_state(tmp_path)
        slot = _ChatSlot("s1")
        state._slots["s1"] = slot
        slot.linked_session_key = "dashboard:s1"

        window = [
            {"role": "user", "content": "authorized here", "ts": "1"},
            {
                "role": "inject",
                "content": "authorized elsewhere",
                "ts": "2",
                "meta": {"noteSession": "cron:job42"},
            },
        ]
        events: list[dict] = []

        with patch(
            "kiro_crew.dashboard.chat_persistence.sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
        ):
            chat_persistence._save_slot_to_history(state, slot, messages=window, force=True)

        denials = [e for e in events if e.get("source") == "app_isolation"]
        assert (
            len(denials) == 1
        ), f"the save dropped a rebound note row and recorded no denial: {events}"
        denial = denials[0]
        assert denial["outcome"] == "denied"
        assert denial["operation"] == "note_save_drop"
        assert denial["caller"] == "dashboard"
        assert denial["resources"] == "slot=s1 dropped=1"
        assert "rebound" in denial["error"]
        # Never the note's content: BSC4 forbids sensitive data in a security event.
        assert "authorized elsewhere" not in repr(denial)
        # Non-fatal: the authorized remainder still reaches disk, and the dropped
        # row does not. `critical` is left default so a denial cannot fail a save.
        assert "critical" not in denial
        persisted = [
            m.get("content")
            for m in state.conversation_log.read_messages_chained(_history_key_for("s1"))
        ]
        assert "authorized here" in persisted, f"the save was aborted: {persisted}"
        assert "authorized elsewhere" not in persisted

    def test_a_save_with_nothing_dropped_records_no_denial(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Count-gated: this is the periodic path, so a clean save must stay silent."""
        from types import SimpleNamespace

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_chat_state(tmp_path)
        slot = _ChatSlot("s2")
        state._slots["s2"] = slot
        slot.linked_session_key = "dashboard:s2"

        window = [{"role": "user", "content": "nothing to drop", "ts": "1"}]
        events: list[dict] = []

        with patch(
            "kiro_crew.dashboard.chat_persistence.sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
        ):
            chat_persistence._save_slot_to_history(state, slot, messages=window, force=True)

        assert [
            e for e in events if e.get("source") == "app_isolation"
        ] == [], "an ungated emit would record a denial on every save of every slot"
