"""The owner app's own slots, and the routes that decide ownership without the checkpoint.

A task-review tab runs on the session the taskrunner mints for it (the taskrunner's source
is pinned to mint that shape), so its owner app reads, drives and closes the tab without
the session grant; a link minted for another tab is foreign, and another app is refused.
An owner app's slot linked to another conversation is refused on send, create, resume and
approve alike, and a memory-mode mismatch on the app's own slot is a 409 conflict, not a
missing slot. Resume, whose handler makes the ownership decision, answers an app with the
uniform 404 and no denial row for a missing transcript or a name under construction, and
refuses with no write when a build of the name starts during its reads; a person resuming
a name under construction still gets the retryable 409. The mode route, which names its
slot in the body, re-reads the grant after reading the body and never trusts a slot
replaced during that read. The delete and side-open handlers, mounted without the
checkpoint, answer a missing and a foreign slot with one body. The route-table pins, the
live-table sweep, the allow audit, the real per-slot handlers, the requests that can
create the slot they name and the binders live in ``test_slot_ownership_checkpoint.py``;
acquisition after setup and the ownership rechecks after later awaits live in
``test_slot_ownership_checkpoint_post_await.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from slot_ownership_checkpoint_helpers import (  # noqa: F401 -- fixtures are requested by name
    _GRANT,
    _NOT_FOUND,
    APP,
    _as,
    _client,
    _persist_and_close,
    chat_run_fixture,
    sel_spy_fixture,
    state_fixture,
)

from kiro_crew.dashboard import slot_ownership
from kiro_crew.dashboard.state import SlotOrigin, _ChatSlot
from kiro_crew.testing.wait import LoopHop

# ── owner session, own conflicts, resume and mode pins ──────────────────────


def _task_review_slot(state, token: str = "abc", linked_token: str | None = None) -> _ChatSlot:
    """The tab ``handlers/taskrunner._task_result_slot`` mints for an app's task."""
    slot = state.get_or_create_slot(
        f"task-review-{token}",
        linked_session_key=f"taskrunner:t1:chat:{linked_token or token}",
    )
    slot._app = APP
    return slot


class TestTheOwnersSessionIsTheOneMintedForIt:
    def test_a_task_review_tab_runs_on_its_own_session(self, state) -> None:
        assert slot_ownership.own_session_key(_task_review_slot(state)) == "taskrunner:t1:chat:abc"
        assert slot_ownership.app_owns_slot_session(APP, _task_review_slot(state, "t2"))

    def test_a_link_minted_for_another_tab_is_foreign(self, state) -> None:
        slot = _task_review_slot(state, "abc", linked_token="other")
        assert slot_ownership.own_session_key(slot) == "dashboard:task-review-abc"
        assert not slot_ownership.app_owns_slot_session(APP, slot)

    def test_the_taskrunner_mints_the_shape_the_checkpoint_reads(self) -> None:
        from kiro_crew.dashboard.handlers import taskrunner

        src = Path(taskrunner.__file__).read_text(encoding="utf-8")
        assert "task_review_session_key(task_id, token)" in src
        assert 'f"{TASK_REVIEW_SLOT_PREFIX}{token}"' in src

    @pytest.mark.asyncio
    async def test_the_owner_app_reads_drives_and_closes_its_task_review_tab(
        self, state, chat_run
    ) -> None:
        _task_review_slot(state)
        with patch(_GRANT, return_value=False):
            async with _client(state, APP) as client:
                detail = await client.get("/api/chat/slots/task-review-abc")
                assert detail.status == 200, await detail.text()
                send = await client.post(
                    "/api/chat?ws=1", json={"slot": "task-review-abc", "message": "hi"}
                )
                assert send.status == 200, await send.text()
                closed = await client.delete("/api/chat/slots/task-review-abc")
                assert closed.status == 200, await closed.text()
        assert "task-review-abc" not in state._slots

    @pytest.mark.asyncio
    async def test_another_app_is_refused_on_the_tab(self, state) -> None:
        _task_review_slot(state)
        async with _client(state, "other-app") as client:
            resp = await client.get("/api/chat/slots/task-review-abc")
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)


class TestOneRuleOnEveryPathToAnOwnedLinkedSlot:
    """An owner app's slot linked to another conversation is refused on every path to it."""

    @pytest.fixture
    def linked(self, state) -> _ChatSlot:
        slot = state.get_or_create_slot("mine", app=APP)
        slot.linked_session_key = "slack:1700000000.000100"
        slot.title = "kept"
        return slot

    @pytest.mark.asyncio
    async def test_send(self, state, chat_run, linked) -> None:
        with patch(_GRANT, return_value=True):
            async with _client(state, APP) as client:
                resp = await client.post("/api/chat?ws=1", json={"slot": "mine", "message": "x"})
                assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        chat_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_create(self, state, linked) -> None:
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots", json={"name": "mine", "title": "x"})
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        assert linked.title == "kept"

    @pytest.mark.asyncio
    async def test_resume_of_the_live_slot(self, state, linked) -> None:
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots/mine/resume", json={})
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)

    @pytest.mark.asyncio
    async def test_approve(self, state, linked) -> None:
        import asyncio

        pending = asyncio.get_running_loop().create_future()
        linked._approval_futures["r1"] = pending
        with patch(_GRANT, return_value=True):
            async with _client(state, APP) as client:
                resp = await client.post(
                    "/api/chat/slots/mine/approve", json={"request_id": "r1", "action": "approved"}
                )
                assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        assert not pending.done()


class TestAnAppsOwnConflictIsNotAMissingSlot:
    @pytest.mark.asyncio
    async def test_a_memory_mode_mismatch_on_the_apps_own_slot_is_a_409(
        self, state, chat_run
    ) -> None:
        state.get_or_create_slot("mine", app=APP)
        with patch(_GRANT, return_value=False):
            async with _client(state, APP) as client:
                send = await client.post(
                    "/api/chat?ws=1",
                    json={"slot": "mine", "message": "x", "memory_mode": "incognito"},
                )
                create = await client.post(
                    "/api/chat/slots", json={"name": "mine", "memory_mode": "incognito"}
                )
                for resp in (send, create):
                    body = await resp.json()
                    assert resp.status == 409, body
                    assert "already exists" in body["error"]
        chat_run.assert_not_called()


class TestResumeAnswersAnAppLikeEveryOtherRoute:
    @pytest.mark.asyncio
    async def test_a_missing_transcript_writes_no_audit_row(self, state, sel_spy) -> None:
        async with _client(state, APP) as client:
            for n in range(3):
                resp = await client.post(f"/api/chat/slots/nope{n}/resume", json={})
                assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        assert sel_spy.denials() == []

    @pytest.mark.asyncio
    async def test_a_name_under_construction_is_the_uniform_404(self, state, sel_spy) -> None:
        await _persist_and_close(state, state.get_or_create_slot("appconv", app=APP))
        state._slots_under_construction.add("x1")
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots/x1/resume", json={"key": "dashboard:appconv"})
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        assert sel_spy.denials() == []
        assert state.conversation_log.get_metadata("dashboard:appconv").get("closed")

    @pytest.mark.asyncio
    async def test_the_person_still_gets_the_retryable_conflict(self, state) -> None:
        await _persist_and_close(state, state.get_or_create_slot("s1", origin=SlotOrigin.USER))
        state._slots_under_construction.add("s1")
        async with _client(state, "") as client:
            resp = await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            assert resp.status == 409
            assert (await resp.json())["code"] == "resume_in_progress"

    @pytest.mark.asyncio
    async def test_construction_starting_during_the_reads_leaves_the_marker(
        self, state, sel_spy, monkeypatch
    ) -> None:
        """Another build of the key starting inside the reads refuses with no write."""
        from kiro_crew.dashboard import chat_handlers

        await _persist_and_close(state, state.get_or_create_slot("a1", app=APP))
        log = state.conversation_log
        cleared: list[str] = []
        real_clear = log.clear_closed
        load_cfg = chat_handlers._load_restore_cfg

        def clear_closed(key, **kw):
            cleared.append(key)
            return real_clear(key, **kw)

        def construct_then_load():
            state._slots_under_construction.add("a1")
            return load_cfg()

        monkeypatch.setattr(log, "clear_closed", clear_closed)
        monkeypatch.setattr(chat_handlers, "_load_restore_cfg", construct_then_load)
        async with _client(state, APP) as client:
            resp = await client.post("/api/chat/slots/a1/resume", json={})
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        state._slots_under_construction.discard("a1")
        assert cleared == []
        meta, readable = log.get_metadata_status("dashboard:a1")
        assert readable and meta.get("closed")
        assert "a1" not in state._slots


class TestModeRereadsTheGrantAfterTheBody:
    @pytest.mark.asyncio
    async def test_a_grant_removed_during_the_upload_is_not_used(self, state) -> None:
        slot = state.get_or_create_slot("u1", origin=SlotOrigin.USER)
        grant = MagicMock(side_effect=[True, False])
        with patch(_GRANT, grant):
            async with _client(state, APP) as client:
                resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "u1"})
                assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        assert grant.call_count == 2
        assert not slot._trust

    @pytest.mark.asyncio
    async def test_a_slot_replaced_during_the_grant_read_is_not_trusted(self, state) -> None:
        state.get_or_create_slot("shared", app=APP)
        hop = LoopHop(asyncio.get_running_loop())
        replacement = []
        calls = []

        async def replace():
            state._slots.pop("shared")
            replacement.append(state.get_or_create_slot("shared", app="other-app"))

        def grant(_app: str) -> bool:
            calls.append(None)
            if len(calls) == 2:
                hop.run(replace(), "the slot replacement")
            return True

        with patch(_GRANT, grant):
            async with _client(state, APP) as client:
                resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "shared"})
                hop.check()
                assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        assert len(calls) == 2
        assert replacement and not replacement[0]._trust


class TestHandlersOutsideTheChainAnswerOneBody:
    """Mounted without the checkpoint, a missing and a foreign slot still read the same."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", ["delete", "side-open"])
    async def test_missing_and_foreign(self, state, route) -> None:
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_delete
        from kiro_crew.dashboard.handlers.side import api_side_open

        state.get_or_create_slot("theirs", origin=SlotOrigin.USER)
        app = web.Application(middlewares=[_as(APP)])
        app["state"] = state
        app.router.add_delete("/api/chat/slots/{slot}", api_chat_slot_delete)
        app.router.add_post("/api/chat/slots/{slot}/side/open", api_side_open)
        bodies = []
        async with TestClient(TestServer(app)) as client:
            for name in ("absent", "theirs"):
                if route == "delete":
                    resp = await client.delete(f"/api/chat/slots/{name}")
                else:
                    resp = await client.post(f"/api/chat/slots/{name}/side/open", json={})
                bodies.append((resp.status, await resp.json()))
        assert bodies == [(404, _NOT_FOUND), (404, _NOT_FOUND)]
        assert "theirs" in state._slots
