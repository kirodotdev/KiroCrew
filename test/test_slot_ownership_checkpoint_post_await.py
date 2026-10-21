"""Ownership is re-checked after the awaits between an app's check and the act it allows.

A person's transcript created and closed during the setup of an app's send or create is
never adopted. A live slot of the caller's own that appears during a send's or create's
setup stays the live slot and the request succeeds, for an app and for the dashboard user
alike, and cancelling the acquisition recheck releases its reservation of the free key.
Title generation, the session summary, regenerate, switch-variant and rewind answer the
uniform 404 when the app's slot is replaced under the same name during one of their
awaits: the replacement keeps its own title, a summary whose slot is replaced before its
read-back is neither pushed nor cached, and regenerate, switch-variant and rewind leave
the replacement's messages alone and dispatch no chat run. The send and create handlers
take no await between the final acquisition recheck and the acquisition itself. The
route-table pins, the live-table sweep, the real per-slot handlers and the closed-session
resume races live in ``test_slot_ownership_checkpoint.py``; the owner app's own slots and
the routes that decide ownership without the checkpoint live in
``test_slot_ownership_checkpoint_owner_session.py``.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slot_ownership_checkpoint_helpers import (  # noqa: F401 -- fixtures are requested by name
    _GRANT,
    _NOT_FOUND,
    APP,
    _client,
    _persist,
    _persist_and_close,
    _with_reply,
    chat_run_fixture,
    regen_run_fixture,
    state_fixture,
)

from kiro_crew.dashboard.state import SlotOrigin, _ChatSlot
from kiro_crew.testing.wait import await_until


class TestAcquisitionAfterSetup:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", ["send", "create"])
    async def test_a_user_transcript_created_during_setup_is_not_adopted(
        self, state, chat_run, monkeypatch, route
    ) -> None:
        async def create_and_close(_request):
            assert "s1" not in state._slots
            await _persist_and_close(state, state.get_or_create_slot("s1", origin=SlotOrigin.USER))
            return ""

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.cron_slot_creator", create_and_close)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._maybe_auto_title", AsyncMock())
        with patch(_GRANT, return_value=False):
            async with _client(state, APP) as client:
                url, body = (
                    ("/api/chat?ws=1", {"slot": "s1", "message": "APP-PROMPT"})
                    if route == "send"
                    else ("/api/chat/slots", {"name": "s1", "title": "APP-TITLE"})
                )
                resp = await client.post(url, json=body)
                assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        chat_run.assert_not_called()
        assert "s1" not in state._slots
        assert "s1" not in state._slots_under_construction
        meta = await asyncio.to_thread(state.conversation_log.get_metadata, "dashboard:s1")
        assert not meta.get("app")
        assert meta.get("title") != "APP-TITLE"
        rows = await asyncio.to_thread(state.conversation_log.read_messages, "dashboard:s1")
        assert all(row.get("content") != "APP-PROMPT" for row in rows)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", ["send", "create"])
    @pytest.mark.parametrize("caller", [APP, ""])
    async def test_a_live_slot_appearing_during_setup_keeps_the_post_create_decision(
        self, state, chat_run, monkeypatch, route, caller
    ) -> None:
        appeared = []

        async def create_meanwhile(_request):
            slot = state.get_or_create_slot("s1", app=caller)
            appeared.append(slot)
            return ""

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.cron_slot_creator", create_meanwhile)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._maybe_auto_title", AsyncMock())
        with patch(_GRANT, return_value=False):
            async with _client(state, caller) as client:
                url, body = (
                    ("/api/chat?ws=1", {"slot": "s1", "message": "hello"})
                    if route == "send"
                    else ("/api/chat/slots", {"name": "s1"})
                )
                resp = await client.post(url, json=body)
                assert resp.status == 200, await resp.text()
        assert state._slots["s1"] is appeared[0]
        assert not state._slots_under_construction

    @pytest.mark.asyncio
    async def test_cancellation_releases_the_free_key_reservation(self, state, monkeypatch) -> None:
        from kiro_crew.dashboard import chat_handlers

        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def read_owner(*_args):
            entered.set()
            try:
                assert release.wait(5), "transcript read was not released"
                return ""
            finally:
                finished.set()

        monkeypatch.setattr(chat_handlers, "transcript_acquisition_reason", read_owner)
        pending = asyncio.create_task(
            chat_handlers._app_slot_acquisition_recheck(state, APP, "s1", None, "test")
        )
        try:
            assert await asyncio.to_thread(entered.wait, 5), "transcript read did not start"
            assert "s1" in state._slots_under_construction
            with pytest.raises(ValueError, match="still being built"):
                state.get_or_create_slot("s1", origin=SlotOrigin.USER)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await await_until(pending)
            assert "s1" not in state._slots_under_construction
            assert "s1" not in state._slots
        finally:
            release.set()
            await await_until(
                asyncio.gather(pending, return_exceptions=True), timeout=5, what="teardown"
            )
            assert await asyncio.to_thread(finished.wait, 5)


class TestPostAwaitOwnership:
    @staticmethod
    async def replace(state):
        state._slots.pop("s1")
        replacement = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        replacement.title = "PERSON-TITLE"
        await asyncio.to_thread(
            state.conversation_log.update_metadata,
            "dashboard:s1",
            {"app": "", "title": replacement.title},
        )
        return replacement

    @pytest.mark.asyncio
    @pytest.mark.parametrize("phase", ["generation", "persist-entry", "persist-exit"])
    async def test_title_cannot_retitle_a_replacement(self, state, monkeypatch, phase) -> None:
        from kiro_crew.dashboard import chat_title

        slot = state.get_or_create_slot("s1", app=APP)
        await _persist(state, slot)
        original_title = slot.title
        push = MagicMock()
        monkeypatch.setattr(state, "push_slot_title", push)
        persist = chat_title._persist_title

        async def generate(*_args, **_kwargs):
            if phase == "generation":
                await self.replace(state)
            return "APP-TITLE"

        async def persist_with_replacement(*args, **kwargs):
            if phase == "persist-entry":
                await self.replace(state)
            result = await persist(*args, **kwargs)
            if phase == "persist-exit":
                await self.replace(state)
            return result

        monkeypatch.setattr(chat_title, "_generate_title_via_kiro", generate)
        monkeypatch.setattr(chat_title, "_persist_title", persist_with_replacement)
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots/s1/generate-title")
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        assert state._slots["s1"].title == "PERSON-TITLE"
        meta = await asyncio.to_thread(state.conversation_log.get_metadata, "dashboard:s1")
        assert meta["title"] == "PERSON-TITLE"
        assert not meta.get("app")
        push.assert_not_called()
        if phase == "generation":
            assert slot.title == original_title

    @pytest.mark.asyncio
    @pytest.mark.parametrize("phase", ["generation", "publication", "read-back"])
    async def test_summary_cannot_publish_into_a_replacement(
        self, state, monkeypatch, phase
    ) -> None:
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.dashboard import chat_handlers, chat_summary

        slot = state.get_or_create_slot("s1", app=APP)
        for index in range(3):
            slot.append("user", f"goal {index}")
            slot.append("assistant", f"result {index}")
        await _persist(state, slot)
        cfg = KiroCrewConfig()
        cfg.session_summary.enabled = True
        monkeypatch.setattr(chat_handlers.KiroCrewConfig, "load", staticmethod(lambda: cfg))
        pushed = MagicMock()
        monkeypatch.setattr(state, "push_session_summary", pushed)
        log = state.conversation_log
        publication_hold = log.publication_hold
        read_back = chat_handlers.read_cached_intent_summary

        async def generate(*_args, **_kwargs):
            if phase == "generation":
                await self.replace(state)
            return json.dumps(
                {"intents": [{"title": "App goal", "ranges": [[1, 1]]}], "constraints": []}
            )

        @contextlib.contextmanager
        def publish_with_replacement(key):
            with publication_hold(key):
                if phase == "publication":
                    # The worker has obtained the write lock; the map identity
                    # is the only signal that changes, not the disk signature.
                    state._slots["s1"] = _ChatSlot("s1")
                yield

        async def read_with_replacement(*args):
            result = await read_back(*args)
            if phase == "read-back":
                await self.replace(state)
            return result

        model = AsyncMock(side_effect=generate)
        monkeypatch.setattr(chat_summary, "run_bg_oneliner", model)
        monkeypatch.setattr(log, "publication_hold", publish_with_replacement)
        monkeypatch.setattr(chat_handlers, "read_cached_intent_summary", read_with_replacement)
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots/s1/summary")
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        model.assert_awaited_once()
        assert state._slots["s1"] is not slot
        assert not slot._summary_in_flight
        if phase != "read-back":
            pushed.assert_not_called()
            assert slot._summary_turn_mark == 0
            assert await asyncio.to_thread(log.get_cached_intent_summary, "dashboard:s1") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", ["regenerate", "switch-variant"])
    async def test_replacement_after_history_save_cannot_dispatch_or_broadcast(
        self, state, regen_run, monkeypatch, route
    ) -> None:
        from kiro_crew.dashboard import chat_regenerate

        slot = state.get_or_create_slot("s1", app=APP)
        _with_reply(slot)
        slot.messages[-1]["variants"] = [{"content": "alternative"}]
        save = chat_regenerate.save_slot_off_loop
        pushed = MagicMock()
        monkeypatch.setattr(state, "push_slots_update", pushed)

        async def save_and_replace(*args, **kwargs):
            result = await save(*args, **kwargs)
            await self.replace(state)
            pushed.reset_mock()
            return result

        monkeypatch.setattr(chat_regenerate, "save_slot_off_loop", save_and_replace)
        async with _client(state, APP) as client:
            resp = await client.post(f"/api/chat/slots/s1/{route}", json={"index": 0})
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        regen_run.assert_not_called()
        pushed.assert_not_called()
        assert state._slots["s1"].messages == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("phase", ["discard", "flush", "save", "cleanup"])
    async def test_rewind_does_not_commit_or_dispatch_a_replacement(
        self, state, monkeypatch, phase
    ) -> None:
        from kiro_crew.dashboard import chat_rewind

        slot = state.get_or_create_slot("s1", app=APP)
        await _persist(state, slot)
        before = list(slot.messages)
        run = AsyncMock()
        monkeypatch.setattr(chat_rewind, "_run_chat", run)
        state.sessions._session_map.get.return_value = "orphan" if phase == "cleanup" else ""
        save = chat_rewind._save_slot_to_history

        async def discard(*_args, **_kwargs):
            if phase == "discard":
                await self.replace(state)
            return True

        async def flush():
            if phase == "flush":
                await self.replace(state)

        def save_and_replace(*args, **kwargs):
            result = save(*args, **kwargs)
            if phase == "save":
                state._slots["s1"] = _ChatSlot("s1")
            return result

        async def cleanup(_sid):
            if phase == "cleanup":
                await self.replace(state)

        state.sessions.discard_conversation.side_effect = discard
        state.sessions.aflush.side_effect = flush
        monkeypatch.setattr(chat_rewind, "_save_slot_to_history", save_and_replace)
        monkeypatch.setattr(chat_rewind, "_delete_orphan_kiro_session", cleanup)
        async with _client(state, APP) as client:
            resp = await client.post(
                "/api/chat/slots/s1/rewind", json={"at_message_index": 0, "content": "APP-EDIT"}
            )
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        await await_until(slot.task, what="the slot's turn")
        run.assert_not_called()
        assert state._slots["s1"].messages == []
        if phase != "cleanup":
            assert slot.messages == before


class TestAcquisitionRecheckPlacement:
    @pytest.mark.parametrize("handler_name", ["api_chat", "api_chat_slot_create"])
    def test_no_await_between_final_recheck_and_acquisition(self, handler_name) -> None:
        import inspect

        from kiro_crew.dashboard import chat_handlers

        tree = ast.parse(inspect.getsource(getattr(chat_handlers, handler_name)))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        recheck = next(
            node
            for node in calls
            if isinstance(node.func, ast.Name) and node.func.id == "_app_slot_acquisition_recheck"
        )
        acquire = next(
            node
            for node in calls
            if isinstance(node.func, ast.Attribute) and node.func.attr == "get_or_create_slot"
        )
        assert recheck.end_lineno is not None
        assert recheck.end_lineno < acquire.lineno
        assert not [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.Await, ast.AsyncWith, ast.AsyncFor))
            and recheck.end_lineno < node.lineno < acquire.lineno
        ]
