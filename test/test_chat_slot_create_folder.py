"""POST /api/chat/slots files a new slot into its folder BEFORE announcing it.

The bug these pin: ``get_or_create_slot`` broadcasts the whole slot list while
still inside the create handler, i.e. before the HTTP response reaches the
browser. So a folder applied *after* creation (the old client-side
``setSlotFolder`` PATCH) could never win the race — the dashboard received a
slots frame for an unfiled slot, rendered the new session at the top level, and
only ~200ms later moved it into the folder. Measured in a real browser: the new
row's first paint was at root every time.

The fix is ordering, so the tests assert ordering: the create handler wraps its
whole set-up in ``suspend_slots_push()``, which means exactly ONE coalesced
``slots`` broadcast is emitted and it already carries ``folder_id``. Asserting
only the final state would pass even with the race reintroduced.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

# Bare import, like every sibling test module: `test/` is not a package, and
# `test` is a CPython STDLIB package name — `from test.chat_test_helpers import`
# resolves to the stdlib `test` and fails with ModuleNotFoundError on CI.
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.state import (
    _DEFERRED_SLOTS_FLUSH_DELAY_S,
    _SLOTS_BROADCAST_INTERVAL_S,
    DashboardState,
)
from kiro_crew.history import ConversationLog

FOLDER_ID = "f-design"


def _raise_on_slots(payload):
    """A ``_broadcast`` double that fails the way the evidenced defect does.

    The exception TYPE is incidental — ``json.dumps`` on a non-serializable slot
    value is one such shape — so these tests pin the ordering instead: any
    raise out of the flush must not unwind past the durable write.
    """
    if payload.get("_type") == "slots":
        raise TypeError("Object of type object is not JSON serializable")


def _make_state(tmp_path):
    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.recycle_background = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    state._folders.append({"id": FOLDER_ID, "name": "Design Review", "order": 0})
    return state


def _make_app(state) -> web.Application:
    from kiro_crew.dashboard.chat import api_chat_slot_create

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", api_chat_slot_create)
    return app


def _as_app_handler(app_name: str):
    """The create handler reached with an app claim on the request.

    Middleware sets ``request["app"]`` in production; the tests set it directly
    so the ownership branch is exercised through the real handler.
    """

    from kiro_crew.dashboard.chat import api_chat_slot_create

    async def handler(request: web.Request) -> web.Response:
        request["app"] = app_name
        return await api_chat_slot_create(request)

    return handler


def _record_broadcasts(state) -> list[list[dict]]:
    """Capture the slot list carried by each real ``slots`` broadcast.

    Patching ``_broadcast`` rather than ``push_slots_update`` keeps the
    suspend/coalesce logic under test — stubbing the push itself would make the
    coalescing invisible and the ordering assertion meaningless.
    """
    seen: list[list[dict]] = []

    def capture(payload):
        if payload.get("_type") == "slots":
            seen.append(json.loads(payload["slots"]))

    state._broadcast = capture  # type: ignore[method-assign]
    return seen


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)


class TestCreateInFolder:
    @pytest.mark.asyncio
    async def test_create_with_folder_files_the_slot(self, tmp_path):
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "s1", "folder_id": FOLDER_ID}
            )
            assert resp.status == 200
            assert (await resp.json())["folder_id"] == FOLDER_ID
        assert state._slots["s1"].folder_id == FOLDER_ID

    @pytest.mark.asyncio
    async def test_unknown_folder_is_rejected(self, tmp_path):
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "s1", "folder_id": "nope"}
            )
            assert resp.status == 400
            # The client switches on `code`; the prose is advisory and localizable.
            assert (await resp.json())["code"] == "folder_not_found"
        # Rejected before creation — no half-made slot left behind.
        assert "s1" not in state._slots

    @pytest.mark.asyncio
    async def test_every_broadcast_shows_the_slot_already_filed(self, tmp_path):
        """The ordering guarantee. Fails if the folder is applied post-broadcast."""
        state = _make_state(tmp_path)
        seen = _record_broadcasts(state)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "s1", "folder_id": FOLDER_ID}
            )
            assert resp.status == 200

        # Exactly one coalesced broadcast, not create-then-correct.
        assert len(seen) == 1, f"expected 1 coalesced slots broadcast, got {len(seen)}"

        # And no frame may ever show the slot outside its folder — this is the
        # assertion that reproduces the browser-visible flash when it regresses.
        for frame in seen:
            entry = next((s for s in frame if s["key"] == "s1"), None)
            assert entry is not None
            assert entry["folder_id"] == FOLDER_ID, (
                "a slots frame announced the slot unfiled — the dashboard renders "
                "that as the session flashing at the top level"
            )

    @pytest.mark.asyncio
    async def test_create_without_folder_defers_one_complete_broadcast(
        self, tmp_path, monkeypatch
    ):
        """The ordinary response wins the race with its full-list announcement."""
        state = _make_state(tmp_path)
        seen = _record_broadcasts(state)
        scheduled: list[tuple[float, object, tuple[object, ...]]] = []
        loop = asyncio.get_running_loop()
        real_call_later = loop.call_later
        armed_timer = MagicMock()
        state._slots_broadcast_timer = armed_timer

        def capture_deferred_flush(delay, callback, *args, **kwargs):
            if callback == state._deferred_slots_flush:
                scheduled.append((delay, callback, args))
                return MagicMock()
            return real_call_later(delay, callback, *args, **kwargs)

        monkeypatch.setattr(loop, "call_later", capture_deferred_flush)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots", json={"name": "s1"})
            assert resp.status == 200
            assert (await resp.json())["folder_id"] == ""

        # The response completed without serializing every open slot, and the old
        # trailing timer cannot publish during the fixed handoff delay.
        assert seen == []
        armed_timer.cancel.assert_called_once_with()
        assert state._slots_broadcast_timer is None
        assert len(scheduled) == 1
        delay, callback, args = scheduled.pop()
        assert delay == pytest.approx(_DEFERRED_SLOTS_FLUSH_DELAY_S)
        assert args == ("create slot 's1'", 1)

        # Other windows still receive exactly one complete snapshot immediately after.
        callback(*args)
        assert scheduled == []
        assert len(seen) == 1
        assert any(s["key"] == "s1" for s in seen[0])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("memory_mode", ["incognito", "temporary"])
    async def test_private_memory_create_publishes_synchronously(
        self, tmp_path, monkeypatch, memory_mode
    ):
        state = _make_state(tmp_path)
        seen = _record_broadcasts(state)
        scheduled: list[tuple[float, object, tuple[object, ...]]] = []
        loop = asyncio.get_running_loop()
        real_call_later = loop.call_later

        def capture_deferred_flush(delay, callback, *args, **kwargs):
            if callback == state._deferred_slots_flush:
                scheduled.append((delay, callback, args))
                return MagicMock()
            return real_call_later(delay, callback, *args, **kwargs)

        monkeypatch.setattr(loop, "call_later", capture_deferred_flush)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots",
                json={"name": "private", "memory_mode": memory_mode},
            )
            assert resp.status == 200
            assert (await resp.json())["memory_mode"] == memory_mode

        assert scheduled == []
        assert len(seen) == 1
        assert any(s["key"] == "private" for s in seen[0])

    @pytest.mark.asyncio
    async def test_unknown_create_field_publishes_synchronously(
        self, tmp_path, monkeypatch
    ):
        state = _make_state(tmp_path)
        seen = _record_broadcasts(state)
        scheduled: list[tuple[float, object, tuple[object, ...]]] = []
        loop = asyncio.get_running_loop()
        real_call_later = loop.call_later

        def capture_deferred_flush(delay, callback, *args, **kwargs):
            if callback == state._deferred_slots_flush:
                scheduled.append((delay, callback, args))
                return MagicMock()
            return real_call_later(delay, callback, *args, **kwargs)

        monkeypatch.setattr(loop, "call_later", capture_deferred_flush)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots",
                json={"name": "future", "future_metadata": "present"},
            )
            assert resp.status == 200

        assert scheduled == []
        assert len(seen) == 1
        assert any(s["key"] == "future" for s in seen[0])

    @pytest.mark.asyncio
    async def test_deferred_flush_waits_for_active_persistence_suspension(
        self, tmp_path, monkeypatch
    ):
        """A delayed plain-create callback cannot cross a metadata save barrier."""
        state = _make_state(tmp_path)
        broadcasts: list[str] = []
        persistence = {"durable": False}

        def broadcast_after_persistence():
            broadcasts.append("published")
            assert persistence["durable"], "published before the slot mutation was durable"

        monkeypatch.setattr(state, "_do_slots_broadcast", broadcast_after_persistence)

        with state.suspend_slots_push():
            assert state._slots_push_pending is False

            state._deferred_slots_flush("create slot 'plain-create'", 1)

            assert broadcasts == []
            assert state._slots_push_pending is True
            persistence["durable"] = True

        assert broadcasts == ["published"]
        assert state._slots_push_pending is False

    @pytest.mark.asyncio
    async def test_overlapping_suspension_disables_outer_deferral(
        self, tmp_path, monkeypatch
    ):
        state = _make_state(tmp_path)
        seen = _record_broadcasts(state)
        scheduled: list[tuple[float, object, tuple[object, ...]]] = []
        loop = asyncio.get_running_loop()
        real_call_later = loop.call_later

        def capture_deferred_flush(delay, callback, *args, **kwargs):
            if callback == state._deferred_slots_flush:
                scheduled.append((delay, callback, args))
                return MagicMock()
            return real_call_later(delay, callback, *args, **kwargs)

        monkeypatch.setattr(loop, "call_later", capture_deferred_flush)

        with state.suspend_slots_push() as defer_flush:
            state.get_or_create_slot("overlap")
            defer_flush("create slot 'overlap'")
            with pytest.raises(LookupError, match="inner failed"):
                with state.suspend_slots_push():
                    raise LookupError("inner failed")

        assert scheduled == []
        assert len(seen) == 1
        assert any(s["key"] == "overlap" for s in seen[0])
        assert state._slots_push_overlapped is False

    @pytest.mark.asyncio
    async def test_successful_deferred_flush_starts_coalescing_window(
        self, tmp_path, monkeypatch
    ):
        state = _make_state(tmp_path)
        broadcasts: list[str] = []
        scheduled: list[tuple[float, object, tuple[object, ...]]] = []
        loop = asyncio.get_running_loop()
        armed_timer = MagicMock()
        trailing_timer = MagicMock()
        state._slots_broadcast_timer = armed_timer

        monkeypatch.setattr(
            state, "_do_slots_broadcast", lambda: broadcasts.append("published")
        )
        monkeypatch.setattr("kiro_crew.dashboard.state.time.monotonic", lambda: 100.0)

        def capture_callback(delay, callback, *args, **kwargs):
            scheduled.append((delay, callback, args))
            return trailing_timer

        monkeypatch.setattr(loop, "call_later", capture_callback)

        state._deferred_slots_flush("create slot 's1'", 1)
        assert broadcasts == ["published"]
        assert state._slots_broadcast_last == 100.0
        armed_timer.cancel.assert_called_once_with()
        assert state._slots_broadcast_timer is None

        state.push_slots_update()

        assert broadcasts == ["published"]
        assert len(scheduled) == 1
        assert state._slots_broadcast_timer is trailing_timer
        delay, callback, args = scheduled[0]
        assert delay == pytest.approx(_SLOTS_BROADCAST_INTERVAL_S)
        assert callback == state._trailing_slots_flush
        assert args == ()

    @pytest.mark.asyncio
    async def test_defer_then_body_error_flushes_synchronously_and_propagates(
        self, tmp_path, monkeypatch
    ):
        """An in-flight body error must disable deferral while unwinding."""
        state = _make_state(tmp_path)
        seen = _record_broadcasts(state)
        scheduled = []
        loop = asyncio.get_running_loop()
        real_call_later = loop.call_later

        def capture_deferred_flush(delay, callback, *args, **kwargs):
            if callback == state._deferred_slots_flush:
                scheduled.append((delay, callback, args))
                return MagicMock()
            return real_call_later(delay, callback, *args, **kwargs)

        monkeypatch.setattr(loop, "call_later", capture_deferred_flush)

        with pytest.raises(LookupError, match="body failed"):
            with state.suspend_slots_push() as defer_flush:
                state.get_or_create_slot("s1")
                defer_flush("create slot 's1'")
                raise LookupError("body failed")

        assert scheduled == []
        assert len(seen) == 1
        assert any(s["key"] == "s1" for s in seen[0])

    @pytest.mark.asyncio
    async def test_failed_deferred_publication_logs_and_retries_once(
        self, tmp_path, monkeypatch, caplog
    ):
        state = _make_state(tmp_path)
        attempts = 0
        scheduled: list[tuple[float, object, tuple[object, ...]]] = []
        loop = asyncio.get_running_loop()
        real_call_later = loop.call_later

        def fail_broadcast():
            nonlocal attempts
            attempts += 1
            raise TypeError("not serializable")

        def capture_retry(delay, callback, *args, **kwargs):
            if callback == state._deferred_slots_flush:
                scheduled.append((delay, callback, args))
                return MagicMock()
            return real_call_later(delay, callback, *args, **kwargs)

        monkeypatch.setattr(state, "_do_slots_broadcast", fail_broadcast)
        monkeypatch.setattr(loop, "call_later", capture_retry)
        caplog.set_level("ERROR", logger="kiro_crew.dashboard.state")

        state._deferred_slots_flush("create slot 's1'", 1)
        assert attempts == 1
        assert len(scheduled) == 1
        delay, callback, args = scheduled.pop()
        assert delay == pytest.approx(_SLOTS_BROADCAST_INTERVAL_S)
        assert args == ("create slot 's1'", 0)

        callback(*args)
        assert attempts == 2
        assert scheduled == []
        failures = [
            record
            for record in caplog.records
            if "Deferred slots publication failed" in record.message
        ]
        assert len(failures) == 2
        assert all("create slot 's1'" in record.message for record in failures)

    @pytest.mark.asyncio
    async def test_refiling_an_existing_slot_name_is_broadcast(self, tmp_path):
        """get_or_create_slot returns an existing named slot WITHOUT pushing.

        This handler is now the only thing that files a slot, so it has to emit
        the frame itself. Otherwise the requester sees the move and every other
        connected client keeps the stale folder placement indefinitely.
        """
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            first = await client.post("/api/chat/slots", json={"name": "s1"})
            assert first.status == 200

            # Only start recording now, so we observe the RE-create alone.
            seen = _record_broadcasts(state)
            again = await client.post(
                "/api/chat/slots", json={"name": "s1", "folder_id": FOLDER_ID}
            )
            assert again.status == 200
            assert (await again.json())["folder_id"] == FOLDER_ID

            # The first POST took the leading edge of the slot-broadcast
            # coalescing window, so this frame arrives on the trailing edge.
            await asyncio.sleep(_SLOTS_BROADCAST_INTERVAL_S + 0.05)

        assert seen, "re-filing an existing slot emitted no slots frame at all"
        entry = next((s for s in seen[-1] if s["key"] == "s1"), None)
        assert entry is not None
        assert entry["folder_id"] == FOLDER_ID

    @pytest.mark.asyncio
    async def test_refiling_flags_the_folder_breadcrumb_for_reinjection(self, tmp_path):
        """A CHANGED folder must re-inject the [FOLDER] breadcrumb next turn.

        chat_runner gates the breadcrumb on `is_new or slot._folder_changed`. A
        slot addressed by name may already have had turns (`is_new=False`), so
        without the flag the model keeps believing the session is in its old
        folder. PATCH /api/chat/slots/{slot}/folder sets it; this path must too.
        """
        state = _make_state(tmp_path)
        state._folders.append({"id": "f-other", "name": "Other", "order": 1})
        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/slots", json={"name": "s1", "folder_id": FOLDER_ID})
            # Simulate a slot that has already run a turn: the flag is consumed.
            state._slots["s1"]._folder_changed = False

            resp = await client.post(
                "/api/chat/slots", json={"name": "s1", "folder_id": "f-other"}
            )
            assert resp.status == 200
        assert state._slots["s1"].folder_id == "f-other"
        assert state._slots["s1"]._folder_changed is True

    @pytest.mark.asyncio
    async def test_refiling_into_the_same_folder_does_not_flag(self, tmp_path):
        """Only a CHANGE re-injects; an idempotent re-create must not."""
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/slots", json={"name": "s1", "folder_id": FOLDER_ID})
            state._slots["s1"]._folder_changed = False

            resp = await client.post(
                "/api/chat/slots", json={"name": "s1", "folder_id": FOLDER_ID}
            )
            assert resp.status == 200
        assert state._slots["s1"]._folder_changed is False


class TestCreateAppIsolation:
    """`name` can address an EXISTING slot, and this handler mutates it.

    get_or_create_slot returns an existing slot without consulting ownership, so
    without the App Kit §5.2 check an app token could refile (or retitle) a slot
    belonging to another app or to the dashboard.
    """

    @pytest.mark.asyncio
    async def test_app_cannot_refile_a_dashboard_owned_slot(self, tmp_path):
        state = _make_state(tmp_path)
        app = _make_app(state)
        # A dashboard-created slot is unscoped (_app == "").
        state.get_or_create_slot("s1")
        assert state._slots["s1"]._app == ""

        # Now the same request path, but arriving with an app claim.
        app.router.add_post("/api/as-app/slots", _as_app_handler("evil-app"))
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/as-app/slots", json={"name": "s1", "folder_id": FOLDER_ID}
            )
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"
        # The dashboard's slot was NOT moved.
        assert state._slots["s1"].folder_id == ""

    @pytest.mark.asyncio
    async def test_app_cannot_refile_another_apps_slot(self, tmp_path):
        state = _make_state(tmp_path)
        app = _make_app(state)
        state.get_or_create_slot("s1", app="owner-app")

        app.router.add_post("/api/as-app/slots", _as_app_handler("other-app"))
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/as-app/slots", json={"name": "s1", "folder_id": FOLDER_ID}
            )
            assert resp.status == 404
            # Byte-identical to the unscoped-slot denial: the response must not
            # distinguish "exists but not yours" from "does not exist".
            assert await resp.json() == {"error": "not found", "code": "slot_not_found"}
        assert state._slots["s1"].folder_id == ""

    @pytest.mark.asyncio
    async def test_app_can_refile_its_own_slot(self, tmp_path):
        """The check must not lock an app out of the slots it owns."""
        state = _make_state(tmp_path)
        app = _make_app(state)
        state.get_or_create_slot("s1", app="owner-app")

        app.router.add_post("/api/as-app/slots", _as_app_handler("owner-app"))
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/as-app/slots", json={"name": "s1", "folder_id": FOLDER_ID}
            )
            assert resp.status == 200
        assert state._slots["s1"].folder_id == FOLDER_ID

    @pytest.mark.asyncio
    async def test_dashboard_caller_is_unaffected(self, tmp_path):
        """An empty app claim is the dashboard user and keeps full access."""
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            first = await client.post("/api/chat/slots", json={"name": "s1"})
            assert first.status == 200
            again = await client.post(
                "/api/chat/slots", json={"name": "s1", "folder_id": FOLDER_ID}
            )
            assert again.status == 200
        assert state._slots["s1"].folder_id == FOLDER_ID


class TestFolderTagInheritance:
    """Folder tags copied onto NEW chats filed into the folder.

    Creation-only: re-opening an existing session inside the folder must not
    re-stamp tags, and moving an existing session into a tagged folder via the
    folder PATCH must not retro-tag. Direct folder only.
    """

    @staticmethod
    def _tagged_state(tmp_path, folder_tags):
        state = _make_state(tmp_path)
        # Give the referenced folder a tag list and register the vocabulary so
        # the inheritance validation (ids must exist) passes.
        state._folders[0]["tags"] = list(folder_tags)
        state._tags = [
            {"id": tid, "name": tid, "color": "#6b7280", "order": i}
            for i, tid in enumerate(folder_tags)
        ]
        # Direct population stands in for a successful load_tags(), which is
        # what makes the vocabulary authoritative (the inheritance validator
        # deliberately fails open when it is not).
        state._tags_authoritative = True
        return state

    @staticmethod
    def _app_with_folder_patch(state) -> web.Application:
        from kiro_crew.dashboard.chat import api_chat_slot_create
        from kiro_crew.dashboard.chat_folders import api_chat_slot_folder

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots", api_chat_slot_create)
        app.router.add_patch("/api/chat/slots/{slot}/folder", api_chat_slot_folder)
        return app

    @pytest.mark.asyncio
    async def test_new_slot_in_folder_inherits_the_folders_tags(self, tmp_path):
        """(c) A genuinely new chat filed into a tagged folder copies its tags."""
        from kiro_crew.dashboard.state import _ChatSlot

        state = self._tagged_state(tmp_path, ["t1", "t2"])
        birth_revisions: list[str] = []
        original_init = _ChatSlot.__init__

        def _recording_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            birth_revisions.append(self.tags_revision)

        with patch.object(_ChatSlot, "__init__", _recording_init):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots", json={"name": "fresh", "folder_id": FOLDER_ID}
                )
                assert resp.status == 200
                assert sorted((await resp.json())["tags"]) == ["t1", "t2"]
        assert sorted(state._slots["fresh"].tags) == ["t1", "t2"]
        # "tags changed => revision changed": the inherited list must not ship
        # under the newborn's birth revision, which a slots GET racing the
        # awaited folder read may already have snapshotted with an empty list.
        assert birth_revisions
        assert state._slots["fresh"].tags_revision not in birth_revisions
        assert state._slots["fresh"].tags_revision > max(birth_revisions)

    @pytest.mark.asyncio
    async def test_new_slot_without_folder_inherits_nothing(self, tmp_path):
        """A chat created outside any folder gets no tags — the guard is folder-scoped."""
        state = self._tagged_state(tmp_path, ["t1"])
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots", json={"name": "loose"})
            assert resp.status == 200
            assert (await resp.json())["tags"] == []
        assert state._slots["loose"].tags == []

    @pytest.mark.asyncio
    async def test_reopening_an_existing_slot_does_not_re_stamp_tags(self, tmp_path):
        """(d) Addressing an ALREADY-OPEN slot by name is not a mint — no inheritance.

        The slot is created first with no folder, then re-created by the same
        name into the tagged folder. It is filed (folder membership updates), but
        the folder's tags must NOT be copied on — only a genuinely new chat
        inherits.
        """
        state = self._tagged_state(tmp_path, ["t1", "t2"])
        async with TestClient(TestServer(_make_app(state))) as client:
            first = await client.post("/api/chat/slots", json={"name": "reused"})
            assert first.status == 200
            assert (await first.json())["tags"] == []

            again = await client.post(
                "/api/chat/slots", json={"name": "reused", "folder_id": FOLDER_ID}
            )
            assert again.status == 200
            body = await again.json()
            # Filed into the folder...
            assert body["folder_id"] == FOLDER_ID
            # ...but tags were NOT retro-stamped: it was not a fresh slot.
            assert body["tags"] == []
        assert state._slots["reused"].tags == []

    @pytest.mark.asyncio
    async def test_moving_an_existing_slot_into_a_tagged_folder_does_not_retro_tag(
        self, tmp_path
    ):
        """(e) PATCH /slots/{slot}/folder moves without inheriting the folder's tags."""
        state = self._tagged_state(tmp_path, ["t1", "t2"])
        async with TestClient(TestServer(self._app_with_folder_patch(state))) as client:
            # A slot created outside the folder, untagged.
            created = await client.post("/api/chat/slots", json={"name": "mover"})
            assert created.status == 200
            assert (await created.json())["tags"] == []

            # Move it into the tagged folder via the folder PATCH endpoint.
            moved = await client.patch(
                "/api/chat/slots/mover/folder", json={"folder_id": FOLDER_ID}
            )
            assert moved.status == 200
        assert state._slots["mover"].folder_id == FOLDER_ID
        # The move must not have copied the folder's tags onto the slot.
        assert state._slots["mover"].tags == []

    @pytest.mark.asyncio
    async def test_untagged_folder_leaves_a_new_slot_untagged(self, tmp_path):
        """An empty/absent folder tag list is inherited as no tags."""
        state = _make_state(tmp_path)  # folder has no `tags` key
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "fresh", "folder_id": FOLDER_ID}
            )
            assert resp.status == 200
            assert (await resp.json())["tags"] == []

    @pytest.mark.asyncio
    async def test_stale_folder_tag_id_is_not_copied_onto_the_slot(self, tmp_path):
        """A folder id absent from the vocabulary is dropped, not stamped."""
        state = _make_state(tmp_path)
        state._folders[0]["tags"] = ["gone", "t1"]
        # Only t1 is a live tag; "gone" was deleted from the vocabulary.
        state._tags = [{"id": "t1", "name": "t1", "color": "#6b7280", "order": 0}]
        state._tags_authoritative = True
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "fresh", "folder_id": FOLDER_ID}
            )
            assert resp.status == 200
            assert (await resp.json())["tags"] == ["t1"]

    @pytest.mark.asyncio
    async def test_malformed_folder_tag_entry_does_not_crash_slot_creation(self, tmp_path):
        """A non-string entry in a folder's persisted tags is skipped, never raised on.

        folders.json is hand-editable: a dict (or any unhashable) in `tags`
        would blow up the set-membership test AFTER the slot was inserted,
        turning one malformed store row into a 500 on every chat created in
        that folder. The isinstance guard skips it and still copies the valid
        sibling ids.
        """
        state = _make_state(tmp_path)
        state._folders[0]["tags"] = [{}, None, 42, "t1"]
        state._tags = [{"id": "t1", "name": "t1", "color": "#6b7280", "order": 0}]
        state._tags_authoritative = True
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "fresh", "folder_id": FOLDER_ID}
            )
            assert resp.status == 200
            assert (await resp.json())["tags"] == ["t1"]
        assert state._slots["fresh"].tags == ["t1"]


class TestDurableWriteOrdering:
    """The durable write runs INSIDE the suspension, ahead of the broadcast.

    ``suspend_slots_push``'s ``__exit__`` flushes the owed push, and on the
    coalescing window's LEADING edge that flush broadcasts synchronously — so an
    exception there escapes ``__exit__`` and unwinds the rest of the handler.
    With the forced save sitting after the ``with`` block, that unwind skipped a
    metadata mutation the request had already acknowledged, and this save is the
    only durable record of a recreate's folder filing or pinned title.

    ``session_control.py``'s create span already keeps its persist inside the
    suspension for the same reason ("a slot whose birth write fails is never
    broadcast at all"); these pin the ordering here, not the exception, so any
    future raise from the flush is covered too.
    """

    @pytest.mark.asyncio
    async def test_raising_exit_broadcast_cannot_skip_the_folder_write(self, tmp_path):
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            # A session that has been used, which is the case the forced save is
            # the only durable record for: re-filing an EXISTING slot. Its
            # window is non-empty, so the save takes the full-save path.
            slot = state.get_or_create_slot("s1")
            slot.append("user", "hello")
            slot.drain()
            pinned = slot_history_key(slot)

            # get_or_create_slot above already consumed a leading edge. Wait the
            # window out, or the flush below is deferred to the trailing timer,
            # the handler returns 200, and this test proves nothing.
            await asyncio.sleep(_SLOTS_BROADCAST_INTERVAL_S + 0.05)
            state._broadcast = _raise_on_slots  # type: ignore[method-assign]

            resp = await client.post("/api/chat/slots", json={"name": "s1", "folder_id": FOLDER_ID})

        # The failure still reaches the caller: this is an ordering fix, not a
        # swallow. Whether an already-committed create should answer 500 at all
        # is a separate, declined question, and folding it in here would
        # resurrect it.
        assert resp.status == 500
        # But the acknowledged mutation is on disk. Outside the suspension, the
        # escaping exception skipped this write entirely and nothing reconciled
        # the in-memory slot against it.
        meta = state.conversation_log._read_metadata(pinned) or {}
        assert meta.get("folder_id") == FOLDER_ID, (
            "the folder filing never reached disk — a broadcast failure unwound "
            "past the durable write, so a restart resurrects the old placement"
        )

    @pytest.mark.asyncio
    async def test_raising_exit_broadcast_cannot_skip_the_pinned_title_write(self, tmp_path):
        """Same ordering, via the other field that reaches disk only here."""
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            slot = state.get_or_create_slot("s1")
            slot.append("user", "hello")
            slot.drain()
            pinned = slot_history_key(slot)

            await asyncio.sleep(_SLOTS_BROADCAST_INTERVAL_S + 0.05)
            state._broadcast = _raise_on_slots  # type: ignore[method-assign]

            resp = await client.post("/api/chat/slots", json={"name": "s1", "title": "Pinned"})

        assert resp.status == 500
        meta = state.conversation_log._read_metadata(pinned) or {}
        assert meta.get("title") == "Pinned", (
            "the pinned title never reached disk — a restart rehydrates the old "
            "title with a refreshable 'auto' origin"
        )


class TestFreshFolderCreateStaysOffTheExecutor:
    """A brand-new session filed into a project-less folder skips the project probe.

    The sidebar's "New chat" inside a folder sends ``folder_id`` and no ``name``.
    Validating a folder's project ``stat``s the directory, so it belongs on a
    worker thread; but most folders declare no project, and a thread hop that
    resolves to ``""`` queues on the gateway's shared default executor inside the
    process-wide ``suspend_slots_push``, before the response. The chain walk runs
    on the loop and only a declared project's ``stat`` is sent to a thread.
    """

    @pytest.mark.asyncio
    async def test_fresh_folder_create_makes_no_project_probe(self, tmp_path):
        from kiro_crew.dashboard import chat_folders

        state = _make_state(tmp_path)
        real_validate = chat_folders._validate_project_dir
        with patch.object(
            chat_folders, "_validate_project_dir", MagicMock(side_effect=real_validate)
        ) as validate:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots", json={"folder_id": FOLDER_ID})
                assert resp.status == 200
                assert (await resp.json())["folder_id"] == FOLDER_ID
        assert validate.call_count == 0

    @pytest.mark.asyncio
    async def test_folder_with_a_project_still_validates_it(self, tmp_path):
        """The shortcut only skips folders that declare no project anywhere up the chain."""
        from kiro_crew.dashboard.chat_folders import _folder_declared_project

        folders = [
            {"id": "root", "name": "Root", "project_dir": str(tmp_path)},
            {"id": "child", "name": "Child", "parent_id": "root"},
            {"id": "plain", "name": "Plain"},
            {"id": "typed", "name": "Typed", "project_dir": 7},
            {"id": "loop-a", "name": "A", "parent_id": "loop-b"},
            {"id": "loop-b", "name": "B", "parent_id": "loop-a"},
        ]
        assert _folder_declared_project(folders, "child") == (str(tmp_path), None)
        assert _folder_declared_project(folders, "root") == (str(tmp_path), None)
        assert _folder_declared_project(folders, "plain") == (None, None)
        assert _folder_declared_project(folders, "missing") == (None, None)
        assert _folder_declared_project(folders, "typed") == ("", "project_dir must be a string")
        # A parent cycle terminates rather than spinning.
        assert _folder_declared_project(folders, "loop-a") == (None, None)

    @pytest.mark.asyncio
    async def test_project_validation_runs_off_the_loop_only_when_declared(self, tmp_path):
        """One chain walk on the loop; the ``stat`` hops to a thread, and only when there is one."""
        import threading

        from kiro_crew.dashboard import chat_folders as cf

        target = tmp_path / "repo"
        target.mkdir()
        folders = [
            {"id": "root", "name": "Root", "project_dir": str(target)},
            {"id": "child", "name": "Child", "parent_id": "root"},
            {"id": "plain", "name": "Plain"},
        ]
        real_validate = cf._validate_project_dir
        ran_on: list[int] = []

        def spy(raw: str) -> tuple[str, str | None]:
            ran_on.append(threading.get_ident())
            return real_validate(raw)

        with patch.object(cf, "_validate_project_dir", spy):
            assert await cf.resolve_folder_project_dir_off_loop(folders, "plain") == ("", None)
            assert ran_on == [], "a project-less chain must not validate anything"
            resolved, error = await cf.resolve_folder_project_dir_off_loop(folders, "child")
        assert (resolved, error) == (str(target.resolve()), None)
        assert len(ran_on) == 1
        assert ran_on[0] != threading.get_ident(), "the stat must not run on the loop thread"


class TestOwnerFolderCreatePersistsTheFiling:
    """A fresh owner-dashboard create in a folder has a durable writer for its filing.

    On the owner path the member assignment publishes the newborn's execution
    context through ``bind_session_execution``, whose ``update_metadata_if``
    UPSERTS the session's metadata line. From then on the forced birth save has
    a line to merge into, and it is the only writer of ``folder_id`` (and the
    inherited tags, pinned title, project and colour) before the first message:
    ``slot._dirty`` stays False, so no periodic flush would write them later. A
    restart before the first message must rehydrate the tab filed, not at root.
    """

    @pytest.mark.asyncio
    async def test_owner_folder_create_writes_folder_id_to_the_metadata_line(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
        from kiro_crew.dashboard.chat import api_chat_slot_create
        from kiro_crew.history import _sessions_dir
        from kiro_crew.memory_stores import provision_member_memory

        config = KiroCrewConfig()
        config.agents["local-only-crew"] = KiroCrewAgentConfig(kiro_agent="local-only-crew")
        config.default_agent = "local-only-crew"
        provision_member_memory(config, "local-only-crew")
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load",
            staticmethod(lambda: config),
        )
        state = _make_state(tmp_path)
        # A newborn has no live or resumable kiro-cli session; a bare MagicMock
        # would answer "yes" to both and read as V1 history the member cannot take.
        state.sessions.get_provider = MagicMock(return_value=None)
        state.sessions.resumable_sid = MagicMock(return_value=None)
        # `bind_session_execution` publishes through ``ConversationLog()`` at the
        # default sessions dir, so the state's log must be that same log for the
        # birth save to see the line the assignment created.
        state.conversation_log = ConversationLog(base_dir=_sessions_dir())

        async def owner_handler(request: web.Request) -> web.Response:
            request["app"] = ""
            request["user"] = "local-app"
            return await api_chat_slot_create(request)

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots", owner_handler)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots", json={"folder_id": FOLDER_ID})
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert body["folder_id"] == FOLDER_ID
        slot = state._slots[body["key"]]
        assert not slot.messages
        meta = state.conversation_log._read_metadata(slot_history_key(slot)) or {}
        assert meta.get("memory_store"), f"the owner assignment must have published a line: {meta}"
        assert meta.get("folder_id") == FOLDER_ID, (
            "the folder filing never reached the metadata line -- a restart before "
            "the first message rehydrates the tab unfiled"
        )
