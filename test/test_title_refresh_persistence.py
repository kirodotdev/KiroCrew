"""The title refresh's durable edges: write ordering and the consumed mark on the error
path, a title pinned at slot create (script and cron slots included), resume
hydration of provenance, cancel safety, milestone under-spend, the durable-mark gate
and the post-persist push guard, and successor slots.
``test_title_refresh.py`` heads the suite and lists its parts.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from title_refresh_helpers import _fake_state, _patch_generator, _titled_slot

from kiro_crew.dashboard import chat_title
from kiro_crew.dashboard.chat_title import (
    _TITLE_ORIGIN_AUTO,
    _TITLE_ORIGIN_USER,
    _TITLE_REFRESH_MILESTONES,
    _rehydrated_refresh_mark,
    maybe_refresh_title,
)
from kiro_crew.dashboard.state import _ChatSlot


# ── local-review regressions: write ordering, error-path budget, pin origin ──
class TestPersistWriteOrdering:
    @pytest.mark.asyncio
    async def test_rename_landing_mid_write_is_repersisted(self, tmp_path):
        """A rename landing while the background persist's off-thread write is
        in flight bumps the epoch; the persist loop must then write AGAIN with
        the current (user) values, so the disk can never end up on the stale
        auto title regardless of flock acquisition order."""
        from kiro_crew.history import ConversationLog

        log = ConversationLog(base_dir=tmp_path)
        log.append("dashboard:chat-1-1", "user", "seed")

        slot = _ChatSlot("chat-1-1")
        slot.title = "Auto title"
        slot._titled = True
        slot._title_origin = _TITLE_ORIGIN_AUTO

        state = MagicMock()
        state.conversation_log = log

        writes: list[dict] = []
        real_update = log.update_metadata_if

        def _racing_update(key, fields, guard, **kwargs):
            writes.append(dict(fields))
            result = real_update(key, fields, guard, **kwargs)
            if len(writes) == 1:
                # Simulate the rename winning the race after this (stale)
                # write's guard ran under the lock but before the awaiting
                # coroutine resumes: the epoch has moved. (A rename landing
                # BEFORE the guard runs is folded into this same write by the
                # guard's re-snapshot and needs no second write.)
                slot.title = "User chosen name"
                slot._title_origin = _TITLE_ORIGIN_USER
                slot._title_epoch += 1
            return result

        log.update_metadata_if = _racing_update  # type: ignore[method-assign]

        await chat_title._persist_title(state, slot)

        assert len(writes) == 2, "epoch move during the write must trigger a re-persist"
        assert writes[-1]["title"] == "User chosen name"
        assert writes[-1]["title_origin"] == _TITLE_ORIGIN_USER
        persisted = ConversationLog(base_dir=tmp_path).get_metadata("dashboard:chat-1-1")
        assert persisted["title"] == "User chosen name"
        assert persisted["title_origin"] == _TITLE_ORIGIN_USER

    @pytest.mark.asyncio
    async def test_stable_epoch_writes_exactly_once(self, tmp_path):
        from kiro_crew.history import ConversationLog

        log = ConversationLog(base_dir=tmp_path)
        log.append("dashboard:chat-1-1", "user", "seed")
        slot = _ChatSlot("chat-1-1")
        slot.title = "Auto title"
        slot._titled = True
        slot._title_origin = _TITLE_ORIGIN_AUTO
        state = MagicMock()
        state.conversation_log = log

        count = 0
        real_update = log.update_metadata_if

        def _counting(key, fields, guard, **kwargs):
            nonlocal count
            count += 1
            return real_update(key, fields, guard, **kwargs)

        log.update_metadata_if = _counting  # type: ignore[method-assign]
        await chat_title._persist_title(state, slot)
        assert count == 1


class TestRefreshErrorPathPersistsBudget:
    @pytest.mark.asyncio
    async def test_error_path_persists_consumed_mark(self, monkeypatch):
        """A propagated generation error must still persist the consumed
        milestone — otherwise a restart reloads the old mark and re-spends the
        refresh budget."""
        _patch_generator(monkeypatch, RuntimeError("bg session down"))
        persisted: list[int] = []

        async def _spy_persist(_state, s):
            persisted.append(s._title_refresh_mark)
            return True

        monkeypatch.setattr(chat_title, "_persist_title", _spy_persist)
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
        await maybe_refresh_title(_fake_state(slot), slot)
        assert persisted == [_TITLE_REFRESH_MILESTONES[0]]


class TestSlotCreatePinIsFinal:
    @pytest.mark.asyncio
    async def test_pinned_title_records_user_origin(self, tmp_path, monkeypatch):
        """POST /api/chat/slots with an explicit title on an already-auto-titled
        slot must flip the origin to "user" (and bump the epoch), so the
        background refresh can never rewrite a pinned name."""
        import json as _json  # noqa: F401 — parity with sibling create tests
        from unittest.mock import AsyncMock

        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_ready_kiro_prerequisite

        from kiro_crew.dashboard.chat import api_chat_slot_create
        from kiro_crew.dashboard.state import DashboardState
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        sessions = MagicMock(count=0)
        sessions.remove = AsyncMock()
        sessions.recycle_background = AsyncMock()
        sessions.get_pid = MagicMock(return_value=None)
        state = DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )
        state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots", api_chat_slot_create)

        async with TestClient(TestServer(app)) as client:
            # First create the slot, then simulate it having been auto-titled.
            resp = await client.post("/api/chat/slots", json={"name": "s1"})
            assert resp.status == 200
            slot = state._slots["s1"]
            slot.title = "Auto generated"
            slot._titled = True
            slot._title_origin = _TITLE_ORIGIN_AUTO
            epoch_before = slot._title_epoch

            # Re-address the SAME slot with an explicit pinned title.
            resp = await client.post(
                "/api/chat/slots", json={"name": "s1", "title": "Pinned by caller"}
            )
            assert resp.status == 200
            assert slot.title == "Pinned by caller"
            assert slot._title_origin == _TITLE_ORIGIN_USER
            assert slot._title_epoch == epoch_before + 1

        # And the refresh refuses to touch the pinned name.
        calls = _patch_generator(monkeypatch, "Model idea")
        slot.messages = [
            {"role": "user", "content": f"m{i}"} for i in range(_TITLE_REFRESH_MILESTONES[0])
        ]
        await maybe_refresh_title(_fake_state(slot), slot)
        assert calls == []
        assert slot.title == "Pinned by caller"


class TestScriptCronSlotCreateTitles:
    _CRON_KEY = "cron:nightly-dispatcher"

    @staticmethod
    def _make_route(tmp_path, monkeypatch, *, cron: bool):
        from unittest.mock import AsyncMock

        from aiohttp import web
        from chat_test_helpers import _make_ready_kiro_prerequisite

        from kiro_crew.dashboard import chat_handlers
        from kiro_crew.dashboard.chat import api_chat_slot_create
        from kiro_crew.dashboard.state import DashboardState
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        sessions = MagicMock(count=0)
        sessions.remove = AsyncMock()
        sessions.recycle_background = AsyncMock()
        sessions.get_pid = MagicMock(return_value=None)
        state = DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )
        state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
        if cron:

            async def _cron_creator(_request):
                return TestScriptCronSlotCreateTitles._CRON_KEY

            monkeypatch.setattr(chat_handlers, "cron_slot_creator", _cron_creator)

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots", api_chat_slot_create)
        return state, app

    @pytest.mark.asyncio
    async def test_a_cron_reopen_keeps_a_user_renamed_title(self, tmp_path, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer

        state, app = self._make_route(tmp_path, monkeypatch, cron=True)
        slot = state.get_or_create_slot("nightly-dispatch")
        slot.title = "User renamed title"
        slot._titled = True
        slot._title_origin = _TITLE_ORIGIN_USER
        slot._title_epoch = 4
        slot._created_by = self._CRON_KEY

        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/chat/slots",
                json={"name": "nightly-dispatch", "title": "Nightly dispatch"},
            )

        assert response.status == 200
        assert slot.title == "User renamed title"
        assert slot._title_origin == _TITLE_ORIGIN_USER
        assert slot._title_epoch == 4

    @pytest.mark.asyncio
    async def test_a_cron_reopens_a_closed_session_with_its_saved_title(
        self, tmp_path, monkeypatch
    ):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.chat_api.slot_lifecycle import close_slot

        state, app = self._make_route(tmp_path, monkeypatch, cron=True)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/chat/slots",
                json={"name": "nightly-dispatch", "title": "Nightly dispatch"},
            )
            assert response.status == 200

            slot = state._slots["nightly-dispatch"]
            slot.append("user", "Keep this saved session")
            slot.title = "User renamed title"
            slot._titled = True
            slot._title_origin = _TITLE_ORIGIN_USER
            slot._title_epoch = 4
            slot.created_at = "2000-01-01T00:00:00+00:00"
            await close_slot(state, slot, slot.key)
            assert "nightly-dispatch" not in state._slots
            persisted = state.conversation_log.get_metadata("dashboard:nightly-dispatch")
            assert persisted["title"] == "User renamed title"
            assert persisted["title_origin"] == _TITLE_ORIGIN_USER
            assert persisted["created_by"] == self._CRON_KEY
            saved_created_at = persisted["created_at"]

            response = await client.post(
                "/api/chat/slots",
                json={"name": "nightly-dispatch", "title": "Nightly dispatch"},
            )

        assert response.status == 200
        reopened = state._slots["nightly-dispatch"]
        assert reopened.title == "User renamed title"
        assert reopened._title_origin == _TITLE_ORIGIN_USER
        assert reopened._titled is True
        assert reopened.created_at != saved_created_at

        calls = _patch_generator(monkeypatch, "Model replacement")
        reopened.messages = [
            {"role": "user", "content": f"message {index}"}
            for index in range(_TITLE_REFRESH_MILESTONES[0])
        ]
        await maybe_refresh_title(_fake_state(reopened), reopened)
        assert calls == []
        assert reopened.title == "User renamed title"

        persisted = state.conversation_log.get_metadata("dashboard:nightly-dispatch")
        assert persisted["title"] == "User renamed title"
        assert persisted["title_origin"] == _TITLE_ORIGIN_USER

    @pytest.mark.asyncio
    async def test_a_closed_cron_reopen_rebases_the_auto_title_mark(self, tmp_path, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.chat_api.slot_lifecycle import close_slot

        state, app = self._make_route(tmp_path, monkeypatch, cron=True)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/chat/slots",
                json={"name": "nightly-dispatch", "title": "Nightly dispatch"},
            )
            assert response.status == 200

            slot = state._slots["nightly-dispatch"]
            for index in range(30):
                slot.append("user", f"saved message {index}")
            slot.title = "Saved automatic title"
            slot._titled = True
            slot._title_origin = _TITLE_ORIGIN_AUTO
            slot._title_refresh_mark = 30
            slot._created_by = self._CRON_KEY
            await close_slot(state, slot, slot.key)

            response = await client.post(
                "/api/chat/slots",
                json={"name": "nightly-dispatch", "title": "Nightly dispatch"},
            )

        assert response.status == 200
        reopened = state._slots["nightly-dispatch"]
        assert reopened.messages == []
        assert reopened.title == "Saved automatic title"
        assert reopened._title_origin == _TITLE_ORIGIN_AUTO
        assert reopened._title_refresh_mark == _rehydrated_refresh_mark(30, 0)

        calls = _patch_generator(monkeypatch, "Refreshed automatic title")
        monkeypatch.setattr(chat_title, "_title_refresh_every", lambda: 10)
        reopened.messages = [
            {"role": "user", "content": f"new message {index}"} for index in range(30)
        ]
        await maybe_refresh_title(_fake_state(reopened), reopened)
        assert calls == ["Saved automatic title"]
        assert reopened.title == "Refreshed automatic title"

    @pytest.mark.asyncio
    async def test_a_closed_cron_reopen_runs_the_new_slot_steps(self, tmp_path, monkeypatch):
        from unittest.mock import AsyncMock

        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard import chat_handlers
        from kiro_crew.dashboard.chat_api.slot_lifecycle import close_slot

        state, app = self._make_route(tmp_path, monkeypatch, cron=True)
        slot = state.get_or_create_slot("nightly-dispatch")
        slot.append("user", "Keep this saved session")
        slot.title = "User renamed title"
        slot._titled = True
        slot._title_origin = _TITLE_ORIGIN_USER
        slot._created_by = self._CRON_KEY
        slot.created_at = "2000-01-01T00:00:00+00:00"
        await close_slot(state, slot, slot.key)

        folder_id = "folder-1"
        state._folders = [
            {
                "id": folder_id,
                "name": "Reports",
                "parent_id": None,
                "hidden": False,
                "tags": ["tag-1"],
            }
        ]
        state._tags = [{"id": "tag-1", "name": "Report", "color": "#6b7280", "order": 0}]
        state._tags_authoritative = True

        pin_store = AsyncMock(return_value="")
        record_selection = AsyncMock(return_value=None)
        cfg = SimpleNamespace(
            default_agent="",
            dashboard=SimpleNamespace(default_project=""),
        )
        monkeypatch.setattr(chat_handlers, "pin_private_agent_store", pin_store)
        monkeypatch.setattr(chat_handlers, "_record_explicit_agent_selection", record_selection)
        monkeypatch.setattr(
            chat_handlers,
            "resolve_agent_bindings",
            lambda *_args, **_kwargs: SimpleNamespace(selection_kind=""),
        )
        monkeypatch.setattr(chat_handlers.KiroCrewConfig, "load", staticmethod(lambda: cfg))
        monkeypatch.setattr(chat_handlers, "is_owner_dashboard_request", lambda _request: True)
        monkeypatch.setattr(chat_handlers, "default_project_dir", lambda _workspace: "")
        monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *_args, **_kwargs: None)

        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/chat/slots",
                json={
                    "name": "nightly-dispatch",
                    "title": "Nightly dispatch",
                    "folder_id": folder_id,
                },
                headers={"X-Internal-Secret": "test"},
            )

        assert response.status == 200
        reopened = state._slots["nightly-dispatch"]
        assert reopened.title == "User renamed title"
        assert reopened._title_origin == _TITLE_ORIGIN_USER
        assert reopened._created_by == self._CRON_KEY
        assert reopened.created_at != "2000-01-01T00:00:00+00:00"
        assert reopened.folder_id == folder_id
        assert reopened.tags == ["tag-1"]
        # An agent-less reopen is the default TEMPLATE: a template pick
        # never pins member memory, and the selection is still recorded.
        pin_store.assert_not_awaited()
        record_selection.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_cron_new_slot_gets_the_pinned_title(self, tmp_path, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer

        state, app = self._make_route(tmp_path, monkeypatch, cron=True)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/chat/slots",
                json={"name": "nightly-dispatch", "title": "Nightly dispatch"},
            )

        assert response.status == 200
        slot = state._slots["nightly-dispatch"]
        assert slot.title == "Nightly dispatch"
        assert slot._titled is True
        assert slot._title_origin == _TITLE_ORIGIN_USER

    @pytest.mark.asyncio
    async def test_a_non_cron_reopen_still_replaces_the_title(self, tmp_path, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer

        state, app = self._make_route(tmp_path, monkeypatch, cron=False)
        slot = state.get_or_create_slot("manual-session")
        slot.title = "Current title"
        slot._titled = True
        slot._title_origin = _TITLE_ORIGIN_USER
        slot._title_epoch = 2

        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/chat/slots",
                json={"name": "manual-session", "title": "Replacement title"},
            )

        assert response.status == 200
        assert slot.title == "Replacement title"
        assert slot._title_origin == _TITLE_ORIGIN_USER
        assert slot._title_epoch == 3


# ── server-review regressions: resume hydration, pin persistence, cancel path ─
class TestResumeRehydratesProvenance:
    """The HTTP resume path is the THIRD slot-hydration path; it must restore
    title provenance + the consumed refresh budget like the persistence
    loaders, or the refresh is silently disabled after resume-from-History."""

    @pytest.mark.asyncio
    async def test_resume_restores_auto_origin_and_mark(self, tmp_path, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app, _make_state

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")
        log.update_metadata(
            "dashboard:s1",
            {"title": "Auto name", "title_origin": "auto", "title_refresh_mark": 8},
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            # ``key`` uses the filename-stem spelling list_sessions() returns,
            # matching what resume deep links actually carry.
            resp = await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard_s1"})
            assert resp.status == 200
            slot = state._slots["s1"]
            assert slot._titled is True
            assert slot._title_origin == _TITLE_ORIGIN_AUTO, "refresh stays enabled"
            assert slot._title_refresh_mark == 8, "consumed budget not re-spendable"

    @pytest.mark.asyncio
    async def test_resume_legacy_title_maps_to_user(self, tmp_path, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app, _make_state

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")
        log.update_metadata("dashboard:s1", {"title": "Pre-existing name"})

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard_s1"})
            assert resp.status == 200
            slot = state._slots["s1"]
            assert (
                slot._title_origin == _TITLE_ORIGIN_USER
            ), "legacy origin-less title must stay conservative (never refreshed)"

    @pytest.mark.asyncio
    async def test_resume_with_caller_title_is_a_pin(self, tmp_path, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app, _make_state

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.conversation_log.append("dashboard:s1", "user", "hello")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/resume",
                json={"key": "dashboard:s1", "title": "Pinned on resume"},
            )
            assert resp.status == 200
            slot = state._slots["s1"]
            assert slot.title == "Pinned on resume"
            assert slot._title_origin == _TITLE_ORIGIN_USER
            assert slot._title_epoch == 1


class TestCreatePinPersists:
    @pytest.mark.asyncio
    async def test_title_only_pin_is_persisted(self, tmp_path, monkeypatch):
        """A pinned title WITHOUT a folder must still persist the slot —
        otherwise a restart rehydrates the previous title with a refreshable
        "auto" origin and the background refresh may rewrite the pin."""
        from unittest.mock import AsyncMock

        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_state

        from kiro_crew.dashboard import chat_handlers
        from kiro_crew.dashboard.chat import api_chat_slot_create

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        save_spy = AsyncMock()
        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", save_spy)
        state = _make_state(tmp_path)
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots", api_chat_slot_create)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "s1", "title": "Pinned, no folder"}
            )
            assert resp.status == 200
            assert save_spy.await_count == 1, "pin without folder must persist"
            saved_slot = save_spy.await_args.args[1]
            assert saved_slot.title == "Pinned, no folder"
            assert saved_slot._title_origin == _TITLE_ORIGIN_USER


class TestRefreshCancelSafety:
    @pytest.mark.asyncio
    async def test_mark_is_persisted_before_generation(self, monkeypatch):
        """The consumed milestone is persisted BEFORE the generation await, so
        a task cancellation mid-generation (gateway shutdown) can never leave
        the disk on the old mark and re-spend the budget after restart."""
        import asyncio

        order: list[str] = []

        async def _spy_persist(_state, s):
            order.append(f"persist:{s._title_refresh_mark}")
            return True

        async def _cancelled_generation(_state, _messages, _current, *, session_key: str = ""):
            order.append("generate")
            raise asyncio.CancelledError()

        monkeypatch.setattr(chat_title, "_persist_title", _spy_persist)
        monkeypatch.setattr(chat_title, "_generate_refreshed_title", _cancelled_generation)
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
        with pytest.raises(asyncio.CancelledError):
            await maybe_refresh_title(_fake_state(slot), slot)
        assert order[0] == f"persist:{_TITLE_REFRESH_MILESTONES[0]}"
        assert order[1] == "generate"
        assert slot._title_in_flight is False


class TestMilestoneUnderSpend:
    @pytest.mark.asyncio
    async def test_late_first_attempt_consumes_all_lower_milestones(self, monkeypatch):
        """DELIBERATE under-spend: one attempt at turn >= the last milestone
        consumes every milestone at or below it — a late-eligible session gets
        ONE refresh, never a catch-up burst. The budget is a ceiling."""
        calls = _patch_generator(monkeypatch, "New Title")
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[-1] + 5)
        state = _fake_state()
        await maybe_refresh_title(state, slot)
        assert len(calls) == 1
        await maybe_refresh_title(state, slot)
        assert len(calls) == 1, "no catch-up second call for the skipped milestone"


class TestResumeTitleEchoIsNotAPin:
    """The sidebar's resume call ALWAYS sends a title (``title || key``), and
    that value can be a STALE echo of an older name (notification deep link,
    sidebar row rendered before a background refresh landed). Persisted
    metadata is therefore AUTHORITATIVE on resume: the request title applies
    only when no persisted title exists."""

    @pytest.mark.asyncio
    async def test_echo_of_persisted_title_rehydrates_provenance(self, tmp_path, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app, _make_state

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")
        log.update_metadata(
            "dashboard:s1",
            {"title": "Auto name", "title_origin": "auto", "title_refresh_mark": 8},
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/resume",
                json={"key": "dashboard_s1", "title": "Auto name"},  # echo
            )
            assert resp.status == 200
            slot = state._slots["s1"]
            assert slot.title == "Auto name"
            assert (
                slot._title_origin == _TITLE_ORIGIN_AUTO
            ), "an echoed title must not be classified as a user pin"
            assert slot._title_refresh_mark == 8

    @pytest.mark.asyncio
    async def test_key_placeholder_echo_is_not_a_pin(self, tmp_path, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app, _make_state

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")
        log.update_metadata("dashboard:s1", {"title": "Auto name", "title_origin": "auto"})

        async with TestClient(TestServer(_make_app(state))) as client:
            # resumeChatSlot sends `title: title || key` — the key placeholder.
            resp = await client.post(
                "/api/chat/slots/s1/resume",
                json={"key": "dashboard_s1", "title": "dashboard_s1"},
            )
            assert resp.status == 200
            slot = state._slots["s1"]
            assert slot.title == "Auto name", "persisted title wins over placeholder"
            assert slot._title_origin == _TITLE_ORIGIN_AUTO

    @pytest.mark.asyncio
    async def test_stale_request_title_never_reverts_refreshed_name(self, tmp_path, monkeypatch):
        """The blocking scenario: the background refresh renamed the session
        on disk, then a resume arrives carrying the PRE-refresh title (stale
        client cache). The stale title must neither revert the refreshed name
        nor lock the session as user-origin."""
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app, _make_state

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")
        log.update_metadata(
            "dashboard:s1",
            {"title": "Refreshed name", "title_origin": "auto", "title_refresh_mark": 8},
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/resume",
                json={"key": "dashboard_s1", "title": "Old pre-refresh name"},  # stale
            )
            assert resp.status == 200
            slot = state._slots["s1"]
            assert slot.title == "Refreshed name", "stale echo must not revert the refresh"
            assert slot._title_origin == _TITLE_ORIGIN_AUTO, "must not lock as user-origin"
            assert slot._title_refresh_mark == 8


# ── round-5 regressions: durable-mark gate, post-persist push guard ──────────
class TestRefreshDurableMarkGate:
    @pytest.mark.asyncio
    async def test_failed_mark_persist_aborts_before_generation(self, monkeypatch):
        """If the consumed milestone cannot be made durable (history write
        failure), the refresh must NOT spend the LLM call — a restart would
        reload the old mark and repeat the milestone, breaking the budget."""
        generated: list[str] = []

        async def _failing_persist(_state, _slot):
            return False

        async def _spy_generate(_state, _messages, current, *, session_key: str = ""):
            generated.append(current)
            return "New Title"

        monkeypatch.setattr(chat_title, "_persist_title", _failing_persist)
        monkeypatch.setattr(chat_title, "_generate_refreshed_title", _spy_generate)
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
        state = _fake_state()
        await maybe_refresh_title(state, slot)
        assert generated == [], "no LLM spend on a non-durable mark"
        assert slot.title == "Initial auto title"
        assert slot._title_in_flight is False
        state.push_slot_title.assert_not_called()

    @pytest.mark.asyncio
    async def test_persist_returns_false_on_write_failure(self, tmp_path):
        from kiro_crew.history import ConversationLog

        log = ConversationLog(base_dir=tmp_path)
        log.append("dashboard:chat-1-1", "user", "seed")

        def _boom(_key, _fields, _guard):
            raise OSError("disk full")

        log.update_metadata_if = _boom  # type: ignore[method-assign]
        slot = _ChatSlot("chat-1-1")
        slot.title = "T"
        slot._titled = True
        slot._title_origin = _TITLE_ORIGIN_AUTO
        state = MagicMock()
        state.conversation_log = log
        assert await chat_title._persist_title(state, slot) is False

    @pytest.mark.asyncio
    async def test_persist_returns_true_without_log(self):
        state = MagicMock()
        state.conversation_log = None
        slot = _ChatSlot("chat-1-1")
        assert await chat_title._persist_title(state, slot) is True


class TestRefreshPushGuard:
    @pytest.mark.asyncio
    async def test_rename_during_final_persist_is_not_overwritten_in_sidebar(self, monkeypatch):
        """A rename landing during the refresh's final persist await has
        already broadcast its name; the refresh must not push its stale title
        over it. The push (if any) must carry the slot's CURRENT title."""
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
        persist_calls = {"n": 0}

        async def _persist_with_rename(_state, s, **_kw):
            persist_calls["n"] += 1
            if persist_calls["n"] == 2:
                # The FINAL persist (after the refresh assigned its title):
                # simulate a manual rename landing during this await.
                s.title = "User chosen name"
                s._title_origin = _TITLE_ORIGIN_USER
                s._title_epoch += 1
            return True

        _patch_generator(monkeypatch, "Refreshed title")
        monkeypatch.setattr(chat_title, "_persist_title", _persist_with_rename)
        state = _fake_state(slot)
        await maybe_refresh_title(state, slot)
        assert slot.title == "User chosen name"
        state.push_slot_title.assert_not_called(), (
            "stale refresh title must not be broadcast over the rename's push"
        )


class TestSuccessorSlotIsNotTitled:
    """A session closed and reopened under the same key while a background
    title is in flight gets a NEW slot with its own epoch. The old slot's title
    must not be written for, or pushed to, the successor."""

    @staticmethod
    def _successor(state, slot) -> _ChatSlot:
        successor = _ChatSlot(slot.key)
        successor.title = "Successor title"
        state._slots[slot.key] = successor
        return successor

    @staticmethod
    def _spy_persist(monkeypatch, on_call=None):
        calls: list[tuple[str, bool | None]] = []

        async def _persist(_state, s, *, still_current=None):
            if on_call is not None:
                on_call(len(calls))
            calls.append((s.title, None if still_current is None else still_current()))
            return True

        monkeypatch.setattr(chat_title, "_persist_title", _persist)
        return calls

    @pytest.mark.asyncio
    async def test_refresh_swap_during_generation_writes_nothing(self, monkeypatch):
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
        state = _fake_state(slot)
        holder = {}

        async def _generate(_state, _messages, _current, *, session_key: str = ""):
            holder["successor"] = self._successor(state, slot)
            return "Old session title"

        monkeypatch.setattr(chat_title, "_generate_refreshed_title", _generate)
        calls = self._spy_persist(monkeypatch)
        await maybe_refresh_title(state, slot)
        assert calls == [("Initial auto title", None)], "only the pre-spend mark write"
        assert slot.title == "Initial auto title"
        assert holder["successor"].title == "Successor title"
        state.push_slot_title.assert_not_called()

    @pytest.mark.asyncio
    async def test_refresh_of_a_replaced_slot_spends_nothing(self, monkeypatch):
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
        state = _fake_state(slot)
        successor = self._successor(state, slot)
        calls = _patch_generator(monkeypatch, "Old session title")
        persists = self._spy_persist(monkeypatch)
        await chat_title.title_then_refresh(state, slot)
        assert calls == [] and persists == [], "no LLM call and no mark write"
        assert successor.title == "Successor title"
        state.push_slot_title.assert_not_called()

    @pytest.mark.asyncio
    async def test_refresh_swap_during_persist_is_refused_and_not_pushed(self, monkeypatch):
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
        state = _fake_state(slot)
        _patch_generator(monkeypatch, "Old session title")

        def _swap_on_final_write(n):
            if n == 1:
                self._successor(state, slot)

        calls = self._spy_persist(monkeypatch, _swap_on_final_write)
        await maybe_refresh_title(state, slot)
        assert calls[1] == ("Old session title", False), "the write must be refused"
        state.push_slot_title.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_title_swap_during_generation_writes_nothing(self, monkeypatch):
        monkeypatch.setattr(chat_title, "_TITLE_REVEAL_STEP_SECS", 0)
        slot = _ChatSlot("chat-1-1")
        slot.messages = [{"role": "user", "content": "debug my flaky test"}]
        state = _fake_state(slot)
        holder = {}

        async def _generate(_state, _messages, *, session_key: str = ""):
            holder["successor"] = self._successor(state, slot)
            return "Old session title"

        monkeypatch.setattr(chat_title, "_generate_title_via_kiro", _generate)
        calls = self._spy_persist(monkeypatch)
        await chat_title._maybe_auto_title(state, slot)
        assert calls == []
        assert slot._titled is False
        assert holder["successor"].title == "Successor title"
        state.push_slot_title.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_title_swap_during_reveal_stops_painting(self, monkeypatch):
        monkeypatch.setattr(chat_title, "_TITLE_REVEAL_STEP_SECS", 0)
        slot = _ChatSlot("chat-1-1")
        slot.messages = [{"role": "user", "content": "debug my flaky test"}]
        state = _fake_state(slot)

        async def _generate(_state, _messages, *, session_key: str = ""):
            return "one two three four five"

        state.push_slot_title.side_effect = lambda *_a, **_kw: self._successor(state, slot)
        monkeypatch.setattr(chat_title, "_generate_title_via_kiro", _generate)
        calls = self._spy_persist(monkeypatch)
        await chat_title._maybe_auto_title(state, slot)
        assert state.push_slot_title.call_count == 1, "no frame after the swap"
        assert calls == []
        assert slot._titled is False

    @pytest.mark.asyncio
    async def test_auto_title_swap_during_persist_is_refused(self, monkeypatch):
        monkeypatch.setattr(chat_title, "_TITLE_REVEAL_STEP_SECS", 0)
        slot = _ChatSlot("chat-1-1")
        slot.messages = [{"role": "user", "content": "debug my flaky test"}]
        state = _fake_state(slot)
        suggested = []

        async def _generate(_state, _messages, *, session_key: str = ""):
            return "Old session title"

        async def _suggest(_state, s):
            suggested.append(s)

        monkeypatch.setattr(chat_title, "_generate_title_via_kiro", _generate)
        monkeypatch.setattr(chat_title, "maybe_suggest_folder", _suggest)
        calls = self._spy_persist(monkeypatch, lambda _n: self._successor(state, slot))
        await chat_title._maybe_auto_title(state, slot)
        assert calls == [("Old session title", False)], "the write must be refused"
        assert all(c.kwargs.get("full") is False for c in state.push_slot_title.mock_calls)
        assert suggested == [], "no folder card for a slot that lost its key"

    @pytest.mark.asyncio
    async def test_auto_title_fallback_swap_during_persist_is_refused(self, monkeypatch):
        slot = _ChatSlot("chat-1-1")
        slot.messages = [
            {"role": "user", "content": "something vague"},
            {"role": "assistant", "content": "some reply"},
        ]
        state = _fake_state(slot)

        async def _skip(*_a, **_kw):
            return ""

        monkeypatch.setattr(chat_title, "_generate_title_via_kiro", _skip)
        calls = self._spy_persist(monkeypatch, lambda _n: self._successor(state, slot))
        await chat_title._maybe_auto_title(state, slot)
        assert calls == [("something vague", False)], "the write must be refused"
        state.push_slot_title.assert_not_called()


class TestResumeCorruptedTitleMetadata:
    @pytest.mark.asyncio
    async def test_non_string_persisted_title_does_not_crash_resume(self, tmp_path, monkeypatch):
        """A non-string ``title`` in a corrupted/legacy session JSONL must be
        treated as absent — not redacted (TypeError → HTTP 500)."""
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app, _make_state

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")
        log.update_metadata("dashboard:s1", {"title": 12345})  # corrupted: non-string

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/resume",
                json={"key": "dashboard_s1", "title": "Caller name"},
            )
            assert resp.status == 200, "corrupted title metadata must not 500 the resume"
            slot = state._slots["s1"]
            # Non-string persisted title == absent → the caller-supplied name
            # applies via the never-titled pin branch.
            assert slot.title == "Caller name"
            assert slot._title_origin == _TITLE_ORIGIN_USER
