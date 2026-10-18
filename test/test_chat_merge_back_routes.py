"""Merging a fork back into its parent: sidebar rows, the fork route,
dashboard authorization and closing the slot that holds the card.

The shared fixtures and helpers are in ``chat_merge_back_helpers.py``.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_merge_back_helpers import (  # noqa: F401
    _cards,
    _client,
    _close_route,
    _draft,
    _drop_ids_on_disk,
    _ends_at,
    _merge,
    _pending_twin_of,
    _place_merge_card,
    _restart_as_sidebar_rows,
    _run_turn,
    _seed,
    _slots_on_session,
    _window_loads_on_the_loop,
    _workflow_result_for,
    audit_fixture,
    chats_fixture,
    model_fixture,
)
from chat_test_helpers import _make_state, drain_background_tasks

from kiro_crew.dashboard import chat_handlers, chat_merge_back, chat_persistence, workflow_inject
from kiro_crew.dashboard.chat_api.slot_lifecycle import close_slot
from kiro_crew.dashboard.chat_merge_back import (
    MERGE_NOTE_SOURCE,
    api_chat_slot_merge_back,
    api_chat_slot_merge_back_draft,
    covered_digest,
)
from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.dashboard.slot_buffers import MAX_FORK_PARENT_KEY_CHARS, MERGED_FROM_META_KEY
from kiro_crew.dashboard.state import _MAX_PENDING_CONTEXT
from kiro_crew.dashboard.transcript_binding import bind_transcript
from kiro_crew.testing.wait import async_wait_until, await_until, default_timeout


class TestSidebarRowParent:
    """A parent the startup restore built as a sidebar row, its window not loaded yet."""

    @pytest.mark.asyncio
    async def test_a_sidebar_parent_rewound_during_cursor_work_is_refused(
        self, chats, model, tmp_path, monkeypatch
    ):
        # The merge loads the restored window before it records the parent's
        # cards, so the card the rewind removes is in that record.
        async with _client(chats.state) as client:
            _, first_draft = await _draft(client)
            first_status, _ = await _merge(client, "Redis works.", first_draft)
        # Consumed, so the restart leaves the parent a sidebar row.
        await _run_turn(chats.state, chats.parent, "thanks")
        _seed(chats.fork, ("user", "now try memcached"), ("assistant", "memcached works"))
        rows = list(chats.fork.messages)
        start = next(
            i for i, row in enumerate(rows) if row["meta"]["mid"] == first_draft["through"]
        )
        second_draft = {
            "through": rows[-1]["meta"]["mid"],
            "digest": covered_digest(rows[start + 1 :]),
        }
        state = await _restart_as_sidebar_rows(chats, tmp_path, monkeypatch)
        parent = state._slots["parent"]
        assert parent.window_pending
        landed_cursor = chat_merge_back._landed_cursor

        def rewind_past_the_first_card(*args, **kwargs):
            result = landed_cursor(*args, **kwargs)
            (first,) = _cards(parent)
            parent.messages[:] = parent.messages[: parent.messages.index(first)]
            return result

        monkeypatch.setattr(chat_merge_back, "_landed_cursor", rewind_past_the_first_card)
        async with _client(state) as client:
            status, body = await _merge(client, "Memcached works.", second_draft)

        assert first_status == 200
        assert (status, body.get("code")) == (409, "merge_draft_stale")
        assert parent._deferred_notes == []

    @pytest.mark.asyncio
    async def test_a_draft_loads_the_parents_window_off_the_loop(
        self, chats, model, tmp_path, monkeypatch, caplog
    ):
        state = await _restart_as_sidebar_rows(chats, tmp_path, monkeypatch)
        assert state._slots["parent"].window_pending

        with caplog.at_level(logging.WARNING):
            async with _client(state) as client:
                status, body = await _draft(client)

        assert status == 200, body
        assert body["messages"] == 2
        assert not state._slots["parent"].window_pending
        assert _window_loads_on_the_loop(caplog) == []

    @pytest.mark.asyncio
    async def test_a_merge_loads_the_parents_window_off_the_loop(
        self, chats, model, tmp_path, monkeypatch, caplog
    ):
        draft = {
            "through": chats.fork.messages[-1]["meta"]["mid"],
            "digest": covered_digest(chats.fork.messages[2:]),
        }
        state = await _restart_as_sidebar_rows(chats, tmp_path, monkeypatch)
        assert state._slots["parent"].window_pending

        with caplog.at_level(logging.WARNING):
            async with _client(state) as client:
                status, body = await _merge(client, "Redis works.", draft)

        assert status == 200, body
        assert not state._slots["parent"].window_pending
        assert _window_loads_on_the_loop(caplog) == []
        assert len(_cards(state._slots["parent"])) == 1

    @pytest.mark.asyncio
    async def test_a_pending_sidebar_twin_settles_after_reconciliation_without_replay(self, chats):
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.slot_persistence import metadata_codec
        from kiro_crew.dashboard.state import load_window_off_loop

        state = chats.state
        key = slot_history_key(chats.parent)
        await _place_merge_card(state, chats.parent, chats.fork, "lazy twin summary")
        [context] = chats.parent._pending_context
        note_id = context["noteId"]
        meta = state.conversation_log.get_metadata(key)
        twin = state.get_or_create_slot("twin")
        purpose = metadata_codec.Recent(name="twin", history_key=key)
        applied = metadata_codec.apply(state, twin, meta, purpose)
        twin.linked_session_key = effective_session_key(chats.parent)
        twin._lazy_window = chat_persistence._PendingWindow(state, key, applied)
        assert twin.window_pending
        assert [entry.get("consumed") for entry in twin._deferred_notes] == [None]

        await chat_runner._record_consumed_merge_contexts(state, [context])

        assert state._consumed_merge_cards[note_id] == (key, True)
        assert twin.window_pending
        assert [entry.get("consumed") for entry in twin._deferred_notes] == [True]
        await load_window_off_loop(twin)
        assert not twin.window_pending
        assert twin._pending_context == []

    @pytest.mark.asyncio
    async def test_consumption_does_not_load_a_pending_sidebar_twin(self, chats, caplog):
        from kiro_crew.dashboard import chat_runner

        state = chats.state
        key = slot_history_key(chats.parent)
        await _place_merge_card(state, chats.parent, chats.fork, "twin summary")
        [context] = chats.parent._pending_context
        note_id = context["noteId"]
        twin, loader = _pending_twin_of(state, chats.parent, read_raises=True)

        with caplog.at_level(logging.WARNING):
            await chat_runner._record_consumed_merge_contexts(state, [context])

        assert state._consumed_merge_cards[note_id] == (key, True)
        [held] = state.conversation_log._read_metadata(key)["deferred_notes"]
        assert held["consumed"] is True
        assert twin.window_pending
        assert (loader.reads, loader.loads) == (0, 0)
        assert _window_loads_on_the_loop(caplog) == []


class TestEndToEndWithTheForkRoute:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("forked_from", [7, "s" * (MAX_FORK_PARENT_KEY_CHARS + 1)])
    async def test_a_restored_invalid_fork_parent_is_not_a_fork(self, chats, model, forked_from):
        chats.state.flush_slot_now(chats.fork)
        fork_history_key = slot_history_key(chats.fork)
        chats.state.conversation_log.update_metadata(
            fork_history_key,
            {
                "forked_from": forked_from,
                "forked_from_created_at": chats.parent._disk_meta_created_at,
            },
        )
        chats.state._slots.pop(chats.fork.key)
        restored = _rehydrate_slot_from_history(chats.state, chats.fork.key)
        assert restored is not None
        assert restored.forked_from is None
        assert restored.forked_from_created_at == ""
        through = restored.messages[-1]["meta"]["mid"]

        async with _client(chats.state) as client:
            draft_status, draft_body = await _draft(client)
            merge_status, merge_body = await _merge(client, "Redis works.", _ends_at(through))

        assert (draft_status, draft_body["code"]) == (409, "not_a_fork")
        assert (merge_status, merge_body["code"]) == (409, "not_a_fork")
        assert model.calls == []

    @pytest.mark.asyncio
    async def test_a_legacy_fork_still_drafts_and_merges_into_its_original_parent(
        self, chats, model
    ):
        chats.fork.forked_from_created_at = ""

        async with _client(chats.state) as client:
            draft_status, draft = await _draft(client)
            merge_status, body = await _merge(client, "Redis works.", draft)

        assert draft_status == 200, draft
        assert merge_status == 200, body
        assert [card["content"] for card in _cards(chats.parent)] == ["Redis works."]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("candidate_created_at", ["", "not-a-time"])
    async def test_a_legacy_fork_refuses_a_candidate_without_orderable_identity_as_unconfirmed(
        self, chats, model, candidate_created_at
    ):
        chats.fork.forked_from_created_at = ""
        chats.parent._disk_meta_created_at = candidate_created_at
        through = chats.fork.messages[-1]["meta"]["mid"]

        async with _client(chats.state) as client:
            draft_status, draft_body = await _draft(client)
            merge_status, merge_body = await _merge(client, "Redis works.", _ends_at(through))

        assert (draft_status, draft_body["code"]) == (409, "parent_unconfirmed")
        assert (merge_status, merge_body["code"]) == (409, "parent_unconfirmed")
        assert model.calls == []
        assert _cards(chats.parent) == []
        assert chats.parent._deferred_notes == []

    @pytest.mark.asyncio
    async def test_a_legacy_fork_of_a_parent_with_a_naive_created_at_is_unconfirmed_not_deleted(
        self, chats, model
    ):
        # Older builds stamped the metadata line with a naive local time, so the
        # parent's transcript is real and unchanged, only unorderable.
        naive = "2026-09-30T09:00:00"
        chats.state.conversation_log.update_metadata(
            slot_history_key(chats.parent), {"created_at": naive}
        )
        chats.parent._disk_meta_created_at = naive
        chats.fork.forked_from_created_at = ""
        through = chats.fork.messages[-1]["meta"]["mid"]

        async with _client(chats.state) as client:
            draft_status, draft_body = await _draft(client)
            merge_status, merge_body = await _merge(client, "Redis works.", _ends_at(through))

        assert (draft_status, draft_body["code"]) == (409, "parent_unconfirmed")
        assert (merge_status, merge_body["code"]) == (409, "parent_unconfirmed")
        assert "cannot be confirmed" in draft_body["error"]
        assert model.calls == []
        assert _cards(chats.parent) == []
        assert chats.parent._deferred_notes == []
        on_disk = chats.state.conversation_log.read_messages_chained_full(
            slot_history_key(chats.parent)
        )
        assert not any(MERGED_FROM_META_KEY in (row.get("meta") or {}) for row in on_disk)

    @pytest.mark.asyncio
    async def test_a_recorded_identity_against_a_parent_without_created_at_is_unconfirmed(
        self, chats, model
    ):
        chats.fork.forked_from_created_at = chats.parent._disk_meta_created_at
        assert chats.fork.forked_from_created_at
        chats.parent._disk_meta_created_at = ""
        through = chats.fork.messages[-1]["meta"]["mid"]

        async with _client(chats.state) as client:
            draft_status, draft_body = await _draft(client)
            merge_status, merge_body = await _merge(client, "Redis works.", _ends_at(through))

        assert (draft_status, draft_body["code"]) == (409, "parent_unconfirmed")
        assert (merge_status, merge_body["code"]) == (409, "parent_unconfirmed")
        assert model.calls == []
        assert _cards(chats.parent) == []
        assert chats.parent._deferred_notes == []

    @pytest.mark.asyncio
    async def test_a_legacy_fork_refuses_a_newer_candidate_as_deleted(self, chats, model):
        chats.fork.forked_from_created_at = ""
        chats.fork._disk_meta_created_at = "2026-10-01T12:00:00+00:00"
        chats.parent._disk_meta_created_at = "2026-10-01T13:00:00+00:00"
        through = chats.fork.messages[-1]["meta"]["mid"]

        async with _client(chats.state) as client:
            draft_status, draft_body = await _draft(client)
            merge_status, merge_body = await _merge(client, "Redis works.", _ends_at(through))

        assert (draft_status, draft_body["code"]) == (409, "parent_deleted")
        assert (merge_status, merge_body["code"]) == (409, "parent_deleted")
        assert model.calls == []
        assert _cards(chats.parent) == []
        assert chats.parent._deferred_notes == []

    @pytest.mark.asyncio
    async def test_a_chat_made_on_a_deleted_parents_key_gets_nothing(
        self, tmp_path, monkeypatch, model
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        parent = state.get_or_create_slot("parent")
        parent.created_at = "2026-10-01T12:00:00+00:00"
        parent.title = "Parent"
        parent._titled = True
        _seed(parent, ("user", "pick a cache"), ("assistant", "memcached or redis?"))
        state.flush_slot_now(parent)
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/parent/fork", json={})
            forked = await resp.json()
            assert resp.status == 200, forked
            child = state._slots[forked["key"]]
            child.created_at = "2026-10-01T12:00:00+00:00"
            state.conversation_log.update_metadata(
                slot_history_key(child), {"created_at": child.created_at}
            )
            child._disk_meta_created_at = child.created_at
            child.forked_from_created_at = ""
            _seed(child, ("user", "try redis"), ("assistant", "redis works"))
            _, draft = await _draft(client, slot=child.key)
            # A permanent delete, then a new chat on the same key.
            state._slots.pop("parent")
            state.conversation_log.delete_session(slot_history_key(parent))
            stranger = state.get_or_create_slot("parent")
            stranger.created_at = "2026-10-01T12:00:01+00:00"
            _seed(stranger, ("user", "an unrelated chat"), ("assistant", "hello"))
            state.flush_slot_now(stranger)
            assert stranger._disk_meta_created_at not in ("", child.forked_from_created_at)
            calls_before_refusals = len(model.calls)
            draft_status, draft_body = await _draft(client, slot=child.key)
            status, body = await _merge(client, "Redis works.", draft, slot=child.key)

        assert (draft_status, draft_body["code"]) == (409, "parent_deleted")
        assert (status, body["code"]) == (409, "parent_deleted")
        assert len(model.calls) == calls_before_refusals
        assert _cards(stranger) == []
        assert stranger._deferred_notes == []
        on_disk = state.conversation_log.read_messages_chained_full(slot_history_key(stranger))
        assert not any(MERGED_FROM_META_KEY in (row.get("meta") or {}) for row in on_disk)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reopened", ["before the write", "after the write"])
    async def test_a_parent_reopened_while_the_card_is_written_shows_it_once(
        self, chats, model, monkeypatch, reopened
    ):
        # Closing the parent and resuming it from History while the merge's durable
        # write runs hydrates a replacement. Hydrated before the write, it holds no
        # card; after, it holds the durable copy. Either way it shows the card once.
        state = chats.state
        paused, resume = threading.Event(), threading.Event()
        write = chat_handlers.persist_deferred_notes_sync

        def pause_around_the_write(*args, **kwargs):
            if reopened == "before the write":
                paused.set()
                assert resume.wait(10), "the parent write was not released"
                return write(*args, **kwargs)
            outcome = write(*args, **kwargs)
            paused.set()
            assert resume.wait(10), "the parent write was not released"
            return outcome

        async with _client(state) as client:
            _, draft = await _draft(client)
            monkeypatch.setattr(
                chat_handlers, "persist_deferred_notes_sync", pause_around_the_write
            )
            merging = asyncio.create_task(_merge(client, "Redis works.", draft))
            await async_wait_until(paused.is_set, describe=lambda: "the parent write never started")
            state._slots.pop("parent")
            replacement = _rehydrate_slot_from_history(state, "parent")
            resume.set()
            status, body = await await_until(merging, what="the reopened-parent merge")

        assert status == 200, body
        assert replacement is not None and state._slots["parent"] is replacement
        [card] = _cards(replacement)
        assert card["content"] == "Redis works."
        note_id = card["meta"]["noteId"]
        assert [e for e in replacement._pending_context if e.get("noteId") == note_id] != []
        assert all(note.get("id") != note_id for note in replacement._deferred_notes)
        assert _cards(chats.parent) == []

    @pytest.mark.asyncio
    async def test_a_reopened_parent_with_a_full_hold_still_takes_the_card(
        self, chats, model, monkeypatch
    ):
        # The reopened parent starts a turn and fills its live hold while the merge
        # write is paused. The card was admitted on the closed parent, so the full
        # hold does not turn it away: it waits there and the turn's end shows it once.
        state = chats.state
        paused, resume = threading.Event(), threading.Event()
        write = chat_handlers.persist_deferred_notes_sync

        def pause_before_the_write(*args, **kwargs):
            paused.set()
            assert resume.wait(10), "the parent write was not released"
            return write(*args, **kwargs)

        async with _client(state) as client:
            _, draft = await _draft(client)
            monkeypatch.setattr(
                chat_handlers, "persist_deferred_notes_sync", pause_before_the_write
            )
            merging = asyncio.create_task(_merge(client, "Redis works.", draft))
            await async_wait_until(paused.is_set, describe=lambda: "the parent write never started")
            state._slots.pop("parent")
            replacement = _rehydrate_slot_from_history(state, "parent")
            assert replacement is not None
            replacement.task = asyncio.get_running_loop().create_future()
            session = effective_session_key(replacement)
            replacement._deferred_notes.extend(
                {
                    "id": f"held{i:08d}",
                    "content": f"held {i}",
                    "cls": "reconcile-note",
                    "context": None,
                    "source": "",
                    "session": session,
                }
                for i in range(chat_handlers._MAX_DEFERRED_NOTES)
            )
            resume.set()
            status, body = await await_until(merging, what="the full-hold merge")

        assert (status, body["deferred"]) == (200, True), body
        assert _cards(replacement) == []
        replacement.task = None
        replacement.flush_deferred_notes()
        [card] = _cards(replacement)
        assert card["content"] == "Redis works."
        note_id = card["meta"]["noteId"]
        assert [e["noteId"] for e in replacement._pending_context if e.get("noteId")] == [note_id]
        assert replacement._deferred_notes == []

    @pytest.mark.asyncio
    async def test_a_fork_of_a_restored_pre_id_parent_merges_only_its_own_messages(
        self, chats, model
    ):
        # A transcript written before rows carried ids: no line on disk has one,
        # while the parent's window rows hold ids a restore minted.
        _drop_ids_on_disk(chats.state, chats.parent)

        async with _client(chats.state) as client:
            resp = await client.post("/api/chat/slots/parent/fork", json={})
            forked = await resp.json()
            assert resp.status == 200, forked
            child = chats.state._slots[forked["key"]]
            _seed(child, ("user", "a different idea"), ("assistant", "it also works"))
            status, draft = await _draft(client, slot=child.key)

        assert status == 200, draft
        assert draft["messages"] == 2
        branch = model.calls[-1]["prompt"].split("FORK MESSAGES")[1]
        assert "a different idea" in branch
        assert "pick a cache" not in branch

    @pytest.mark.asyncio
    async def test_a_fork_made_by_the_fork_route_merges_back(self, chats, model):
        async with _client(chats.state) as client:
            resp = await client.post("/api/chat/slots/parent/fork", json={})
            forked = await resp.json()
            assert resp.status == 200, forked
            child = chats.state._slots[forked["key"]]
            _seed(child, ("user", "a different idea"), ("assistant", "it also works"))

            status, draft = await _draft(client, slot=child.key)
            assert status == 200, draft
            status, body = await _merge(client, "different idea works", draft, slot=child.key)

        assert status == 200, body
        assert draft["messages"] == 2
        (card,) = [c for c in _cards(chats.parent) if c["content"] == "different idea works"]
        assert card["meta"][MERGED_FROM_META_KEY]["slot"] == child.key


class TestMergeBackDashboardAuthorization:
    @staticmethod
    def _real_auth_app(state, internal_secret: str) -> web.Application:
        from kiro_crew.dashboard.token_auth import token_auth_middleware

        app = web.Application(
            middlewares=[
                token_auth_middleware(
                    mixed_internal_paths=frozenset({"/api/chat"}),
                    internal_secret=internal_secret,
                )
            ]
        )
        app["state"] = state
        app.router.add_post(
            "/api/chat/slots/{slot}/merge-back/draft", api_chat_slot_merge_back_draft
        )
        app.router.add_post("/api/chat/slots/{slot}/merge-back", api_chat_slot_merge_back)
        return app

    @pytest.mark.asyncio
    async def test_claimless_internal_secret_cannot_draft_or_merge(self, chats, model, audit):
        secret = "test-internal-secret"
        app = self._real_auth_app(chats.state, secret)
        through = chats.fork.messages[-1]["meta"]["mid"]
        headers = {"X-Internal-Secret": secret}

        async with TestClient(TestServer(app)) as client:
            draft_response = await client.post(
                "/api/chat/slots/fork/merge-back/draft", json={}, headers=headers
            )
            merge_response = await client.post(
                "/api/chat/slots/fork/merge-back",
                json={
                    "summary": "attacker context",
                    "through": through,
                    "digest": "0" * 64,
                },
                headers=headers,
            )
            draft_body = await draft_response.json()
            merge_body = await merge_response.json()

        assert (draft_response.status, draft_body["code"]) == (
            404,
            "slot_not_found",
        )
        assert (merge_response.status, merge_body["code"]) == (
            404,
            "slot_not_found",
        )
        assert model.calls == []
        assert _cards(chats.parent) == []
        assert chats.parent._deferred_notes == []
        assert chats.parent._pending_context == []
        assert [call["caller"] for call in audit] == ["internal", "internal"]

    @pytest.mark.asyncio
    async def test_real_dashboard_token_still_drafts_and_merges(self, chats, model):
        from kiro_crew.dashboard.token_auth import generate_token

        app = self._real_auth_app(chats.state, "test-internal-secret")
        token = generate_token("local-app", ttl_seconds=300)
        params = {"token": token}

        async with TestClient(TestServer(app)) as client:
            draft_response = await client.post(
                "/api/chat/slots/fork/merge-back/draft",
                params=params,
                json={},
            )
            draft = await draft_response.json()
            assert draft_response.status == 200, draft
            merge_response = await client.post(
                "/api/chat/slots/fork/merge-back",
                json={
                    "summary": "Redis works.",
                    "through": draft["through"],
                    "digest": draft["digest"],
                },
                params=params,
            )
            body = await merge_response.json()

        assert merge_response.status == 200, body
        assert [card["content"] for card in _cards(chats.parent)] == ["Redis works."]


class TestCarrierClosedAfterTheCard:
    """The slot holding the card closes after the card was written, while a twin of the chat stays open."""

    @pytest.mark.asyncio
    async def test_a_warm_twin_takes_the_card_when_its_carrier_closes(self, chats, model):
        state = chats.state
        session = effective_session_key(chats.parent)
        key = slot_history_key(chats.parent)
        async with _client(state) as client:
            close_status, closed = await _close_route(client)
            assert (close_status, closed) == (200, {"ok": True})
            assert workflow_inject.inject_workflow_result(
                state, "wf-1", _workflow_result_for(session, "wf-1")
            )
            create = await client.post("/api/chat/slots", json={"name": "parent"})
            assert create.status == 200, await create.json()
            twins = _slots_on_session(state, session)
            assert [slot.key for slot in twins] == ["workflow-wf-1", "parent"]
            for slot in twins:
                await _run_turn(state, slot, f"a turn in {slot.key}")
                state.flush_slot_now(slot)
            _, draft = await _draft(client)
            merge_status, merged = await _merge(client, "Redis works.", draft)
            assert (merge_status, merged["parent"], merged["deferred"]) == (
                200,
                "workflow-wf-1",
                False,
            ), merged
            carrier = state._slots["workflow-wf-1"]
            assert [entry["source"] for entry in carrier._pending_context] == [MERGE_NOTE_SOURCE]

            carrier_close_status, carrier_closed = await _close_route(client, "workflow-wf-1")

        assert (carrier_close_status, carrier_closed) == (200, {"ok": True})
        assert "workflow-wf-1" not in state._slots
        assert carrier._pending_context == []
        twin = state._slots["parent"]
        assert [entry["content"] for entry in twin._pending_context] == ["Redis works."]
        prompts = await _run_turn(state, twin, "carry on")
        assert prompts[0].count("Redis works.") == 1
        state.flush_slot_now(twin)
        assert not state.conversation_log.get_metadata(key).get("deferred_notes")

    @pytest.mark.asyncio
    async def test_a_twin_that_has_not_read_its_transcript_takes_the_card(self, chats, model):
        # The create that dedups by name mints the chat without hydrating it, so
        # the twin has observed no identity yet. Its transcript is on disk, and
        # that identity is the carrier's.
        state = chats.state
        session = effective_session_key(chats.parent)
        key = slot_history_key(chats.parent)
        original_created_at = chats.parent._disk_meta_created_at
        async with _client(state) as client:
            close_status, closed = await _close_route(client)
            assert (close_status, closed) == (200, {"ok": True})
            assert workflow_inject.inject_workflow_result(
                state, "wf-first", _workflow_result_for(session, "wf-first")
            )
            carrier = state._slots["workflow-wf-first"]
            await _run_turn(state, carrier, "workflow result acknowledged")
            state.flush_slot_now(carrier)
            created = await client.post("/api/chat/slots", json={"name": "parent"})
            created_body = await created.json()
            assert (created.status, created_body["key"]) == (200, "parent"), created_body
            minted = state._slots["parent"]
            assert minted._disk_meta_created_at == ""
            assert state.conversation_log.thread_transcript_identity(key) == original_created_at
            _, draft = await _draft(client)
            merge_status, merged = await _merge(client, "Redis works.", draft)
            assert (merge_status, merged["parent"]) == (200, "workflow-wf-first"), merged

            carrier_close_status, carrier_closed = await _close_route(client, "workflow-wf-first")

        assert (carrier_close_status, carrier_closed) == (200, {"ok": True})
        assert [entry["content"] for entry in minted._pending_context] == ["Redis works."]
        prompts = await _run_turn(state, minted, "carry on")
        assert prompts[0].count("Redis works.") == 1

    @pytest.mark.asyncio
    async def test_the_idle_sweep_hands_the_card_to_the_warm_twin(self, chats, model, monkeypatch):
        state = chats.state
        session = effective_session_key(chats.parent)
        async with _client(state) as client:
            close_status, closed = await _close_route(client)
            assert (close_status, closed) == (200, {"ok": True})
            assert workflow_inject.inject_workflow_result(
                state, "wf-1", _workflow_result_for(session, "wf-1")
            )
            create = await client.post("/api/chat/slots", json={"name": "parent"})
            assert create.status == 200, await create.json()
            for slot in _slots_on_session(state, session):
                await _run_turn(state, slot, f"a turn in {slot.key}")
                state.flush_slot_now(slot)
            _, draft = await _draft(client)
            merge_status, merged = await _merge(client, "Redis works.", draft)
            assert (merge_status, merged["parent"]) == (200, "workflow-wf-1"), merged
            carrier = state._slots["workflow-wf-1"]
            monkeypatch.setattr(
                chat_handlers,
                "select_idle_slot_keys",
                lambda *_args, **_kwargs: (["workflow-wf-1"], False),
            )

            sweep = await client.post(
                "/api/chat/slots/cleanup", json={"max_inactive_days": 1, "active_slot": "parent"}
            )
            swept = await sweep.json()

        assert (sweep.status, swept["keys"]) == (200, ["workflow-wf-1"]), swept
        assert carrier._pending_context == []
        twin = state._slots["parent"]
        prompts = await _run_turn(state, twin, "carry on")
        assert prompts[0].count("Redis works.") == 1

    @pytest.mark.asyncio
    async def test_a_twin_with_a_full_queue_passes_the_card_to_the_next_twin(self, chats, model):
        state = chats.state
        session = effective_session_key(chats.parent)
        async with _client(state) as client:
            close_status, closed = await _close_route(client)
            assert (close_status, closed) == (200, {"ok": True})
            for run_id in ("wf-1", "wf-2"):
                assert workflow_inject.inject_workflow_result(
                    state, run_id, _workflow_result_for(session, run_id)
                )
            create = await client.post("/api/chat/slots", json={"name": "parent"})
            assert create.status == 200, await create.json()
            twins = _slots_on_session(state, session)
            assert [slot.key for slot in twins] == ["workflow-wf-1", "workflow-wf-2", "parent"]
            for slot in twins:
                await _run_turn(state, slot, f"a turn in {slot.key}")
                state.flush_slot_now(slot)
            _, draft = await _draft(client)
            merge_status, merged = await _merge(client, "Redis works.", draft)
            assert (merge_status, merged["parent"]) == (200, "workflow-wf-1"), merged
            full_twin = state._slots["workflow-wf-2"]
            full_twin._pending_context.extend(
                {"content": f"filler {index}", "source": f"filler-{index}"}
                for index in range(_MAX_PENDING_CONTEXT)
            )

            carrier_close_status, carrier_closed = await _close_route(client, "workflow-wf-1")

        assert (carrier_close_status, carrier_closed) == (200, {"ok": True})
        assert len(full_twin._pending_context) == _MAX_PENDING_CONTEXT
        twin = state._slots["parent"]
        assert [entry["content"] for entry in twin._pending_context] == ["Redis works."]

    @pytest.mark.asyncio
    async def test_a_card_a_turn_puts_back_after_the_close_wait_reaches_the_twin(
        self, chats, model
    ):
        state = chats.state
        session = effective_session_key(chats.parent)
        async with _client(state) as client:
            await _open_parent_beside_its_workflow_slot(state, client, session)
            _, draft = await _draft(client)
            merge_status, merged = await _merge(client, "Redis works.", draft)
            assert (merge_status, merged["parent"]) == (200, "workflow-wf-1"), merged
            carrier = state._slots["workflow-wf-1"]
            [card] = carrier._pending_context
            carrier._pending_context.clear()
            turn_may_end = asyncio.Event()

            async def turn_that_outlives_the_close_wait():
                deadline = asyncio.get_running_loop().time() + default_timeout()
                while not turn_may_end.is_set():
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        return
                    try:
                        await asyncio.wait_for(turn_may_end.wait(), timeout=remaining)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        continue
                carrier._pending_context.append(card)

            carrier.task = asyncio.create_task(turn_that_outlives_the_close_wait())
            try:
                carrier_close_status, carrier_closed = await _close_route(client, "workflow-wf-1")
                twin = state._slots["parent"]
                assert twin._pending_context == []
            finally:
                turn_may_end.set()
                await await_until(carrier.task, what="the outliving turn")
                await drain_background_tasks(state)

        assert (carrier_close_status, carrier_closed) == (200, {"ok": True})
        assert carrier._pending_context == []
        assert [entry["content"] for entry in twin._pending_context] == ["Redis works."]

    @pytest.mark.asyncio
    async def test_a_close_cancelled_during_the_identity_read_still_hands_the_card_over(
        self, chats, model, monkeypatch
    ):
        state = chats.state
        minted, carrier = await _mint_parent_beside_a_carrier_holding_the_card(state, chats)
        read_started, read_may_finish = _block_identity_reads(state, monkeypatch)

        closing = asyncio.create_task(close_slot(state, carrier, carrier.key, hand_back=False))
        await async_wait_until(read_started.is_set, describe=lambda: "the identity read never ran")
        closing.cancel()
        read_may_finish.set()
        await await_until(
            asyncio.gather(closing, return_exceptions=True), what="the cancelled close"
        )

        assert carrier.key not in state._slots
        assert [entry["content"] for entry in minted._pending_context] == ["Redis works."]
        assert state.conversation_log.get_metadata(slot_history_key(carrier)).get("closed")

    @pytest.mark.asyncio
    async def test_a_twin_opened_during_the_identity_read_takes_the_card(
        self, chats, model, monkeypatch
    ):
        state = chats.state
        session = effective_session_key(chats.parent)
        minted, carrier = await _mint_parent_beside_a_carrier_holding_the_card(state, chats)
        minted._pending_context.extend(
            {"content": f"filler {index}", "source": f"filler-{index}"}
            for index in range(_MAX_PENDING_CONTEXT)
        )
        read_started, read_may_finish = _block_identity_reads(state, monkeypatch)

        closing = asyncio.create_task(close_slot(state, carrier, carrier.key, hand_back=False))
        await async_wait_until(read_started.is_set, describe=lambda: "the identity read never ran")
        late_twin = state.get_or_create_slot(name="workflow-wf-late")
        late_twin.linked_session_key = session
        bind_transcript(state, late_twin)
        late_twin._disk_meta_created_at = carrier._disk_meta_created_at
        read_may_finish.set()
        await await_until(closing, what="the carrier close")

        assert len(minted._pending_context) == _MAX_PENDING_CONTEXT
        assert [entry["content"] for entry in late_twin._pending_context] == ["Redis works."]

    @pytest.mark.asyncio
    async def test_a_held_card_the_hand_over_tail_flushes_reaches_the_twin(self, chats, model):
        # The carrier was busy when the merge landed, so the card is held. A close
        # that yields to a same-key replacement writes the tail through
        # ``_persist_handover_tail``, whose flush queues the card on the popped slot.
        state = chats.state
        session = effective_session_key(chats.parent)
        async with _client(state) as client:
            await _open_parent_beside_its_workflow_slot(state, client, session)
            carrier = state._slots["workflow-wf-1"]
            carrier.task = asyncio.get_running_loop().create_future()
            _, draft = await _draft(client)
            merge_status, merged = await _merge(client, "Redis works.", draft)
            assert (merge_status, merged["parent"], merged["deferred"]) == (
                200,
                "workflow-wf-1",
                True,
            ), merged
        carrier.task.cancel()
        carrier.task = None
        assert state._slots.pop("workflow-wf-1") is carrier

        await chat_handlers._persist_handover_tail(state, "workflow-wf-1", carrier)

        assert carrier._pending_context == []
        assert [entry["content"] for entry in state._slots["parent"]._pending_context] == [
            "Redis works."
        ]

    @pytest.mark.asyncio
    async def test_the_idle_sweep_yields_to_a_replacement_opened_during_its_last_hand_off(
        self, chats, model, monkeypatch
    ):
        # The card is held, so the sweep's first hand-off has nothing to move and
        # reads nothing. The flush queues it, and the second hand-off reads the
        # minted parent's identity; a same-key replacement opens during that read.
        state = chats.state
        session = effective_session_key(chats.parent)
        async with _client(state) as client:
            close_status, closed = await _close_route(client)
            assert (close_status, closed) == (200, {"ok": True})
            assert workflow_inject.inject_workflow_result(
                state, "wf-first", _workflow_result_for(session, "wf-first")
            )
            carrier = state._slots["workflow-wf-first"]
            await _run_turn(state, carrier, "workflow result acknowledged")
            state.flush_slot_now(carrier)
            created = await client.post("/api/chat/slots", json={"name": "parent"})
            assert created.status == 200, await created.json()
            carrier.task = asyncio.get_running_loop().create_future()
            _, draft = await _draft(client)
            merge_status, merged = await _merge(client, "Redis works.", draft)
            assert (merge_status, merged["parent"], merged["deferred"]) == (
                200,
                "workflow-wf-first",
                True,
            ), merged
            carrier.task.cancel()
            carrier.task = None
            monkeypatch.setattr(
                chat_handlers,
                "select_idle_slot_keys",
                lambda *_args, **_kwargs: (["workflow-wf-first"], False),
            )
            read_started, read_may_finish = _block_identity_reads(state, monkeypatch)

            sweeping = asyncio.create_task(
                client.post("/api/chat/slots/cleanup", json={"max_inactive_days": 1})
            )
            await async_wait_until(
                read_started.is_set, describe=lambda: "the identity read never ran"
            )
            replacement = state.get_or_create_slot(name="workflow-wf-first")
            replacement.linked_session_key = session
            bind_transcript(state, replacement)
            read_may_finish.set()
            sweep = await await_until(sweeping, what="the idle sweep")
            swept = await sweep.json()

        assert sweep.status == 200, swept
        assert "workflow-wf-first" not in swept["keys"]
        assert state._slots["workflow-wf-first"] is replacement
        assert not state.conversation_log.get_metadata(slot_history_key(carrier)).get("closed")

    @pytest.mark.asyncio
    async def test_a_moved_card_does_not_take_the_seat_a_held_note_reserved(self, chats, model):
        # The twin's queue has one free seat, and a held note's context owns it.
        state = chats.state
        session = effective_session_key(chats.parent)
        async with _client(state) as client:
            await _open_parent_beside_its_workflow_slot(state, client, session)
            _, draft = await _draft(client)
            merge_status, merged = await _merge(client, "Redis works.", draft)
            assert (merge_status, merged["parent"]) == (200, "workflow-wf-1"), merged
            twin = state._slots["parent"]
            twin._pending_context.extend(
                {"content": f"filler {index}", "source": f"filler-{index}"}
                for index in range(_MAX_PENDING_CONTEXT - 1)
            )
            twin.task = asyncio.get_running_loop().create_future()
            delivery = await chat_handlers.deliver_note(
                state, twin, content="held plain note", source="plain note"
            )
            assert not isinstance(delivery, web.Response)
            assert delivery.deferred is True

            carrier_close_status, carrier_closed = await _close_route(client, "workflow-wf-1")

        assert (carrier_close_status, carrier_closed) == (200, {"ok": True})
        assert "Redis works." not in [entry["content"] for entry in twin._pending_context]
        twin.task.cancel()
        twin.task = None
        assert twin.flush_deferred_notes() == 1
        assert twin._pending_context[-1]["content"] == "held plain note"


async def _open_parent_beside_its_workflow_slot(state, client, session: str) -> None:
    """Close the parent, land a workflow result on its session, and open the parent again beside it, both warm."""
    close_status, closed = await _close_route(client)
    assert (close_status, closed) == (200, {"ok": True})
    assert workflow_inject.inject_workflow_result(
        state, "wf-1", _workflow_result_for(session, "wf-1")
    )
    create = await client.post("/api/chat/slots", json={"name": "parent"})
    assert create.status == 200, await create.json()
    for slot in _slots_on_session(state, session):
        await _run_turn(state, slot, f"a turn in {slot.key}")
        state.flush_slot_now(slot)


async def _mint_parent_beside_a_carrier_holding_the_card(state, chats):
    """The parent minted beside a warm workflow slot that holds a merge card; the minted parent has read nothing."""
    session = effective_session_key(chats.parent)
    async with _client(state) as client:
        close_status, closed = await _close_route(client)
        assert (close_status, closed) == (200, {"ok": True})
        assert workflow_inject.inject_workflow_result(
            state, "wf-first", _workflow_result_for(session, "wf-first")
        )
        carrier = state._slots["workflow-wf-first"]
        await _run_turn(state, carrier, "workflow result acknowledged")
        state.flush_slot_now(carrier)
        created = await client.post("/api/chat/slots", json={"name": "parent"})
        assert created.status == 200, await created.json()
        minted = state._slots["parent"]
        assert minted._disk_meta_created_at == ""
        _, draft = await _draft(client)
        merge_status, merged = await _merge(client, "Redis works.", draft)
        assert (merge_status, merged["parent"]) == (200, "workflow-wf-first"), merged
    return minted, carrier


def _block_identity_reads(state, monkeypatch):
    """Make the transcript identity read wait for a release; returns (read started, release)."""
    read_started = threading.Event()
    read_may_finish = threading.Event()
    read_identity = state.conversation_log.thread_transcript_identity

    def blocked_read(*args, **kwargs):
        read_started.set()
        assert read_may_finish.wait(10), "the identity read was not released"
        return read_identity(*args, **kwargs)

    monkeypatch.setattr(state.conversation_log, "thread_transcript_identity", blocked_read)
    return read_started, read_may_finish
