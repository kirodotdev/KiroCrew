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
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

# Bare import, like every sibling test module: `test/` is not a package, and
# `test` is a CPython STDLIB package name — `from test.chat_test_helpers import`
# resolves to the stdlib `test` and fails with ModuleNotFoundError on CI.
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard.state import _SLOTS_BROADCAST_INTERVAL_S, DashboardState
from kiro_crew.history import ConversationLog

FOLDER_ID = "f-design"


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
    async def test_create_without_folder_still_emits_one_broadcast(self, tmp_path):
        """Guard the ordinary path: coalescing must not drop the announcement."""
        state = _make_state(tmp_path)
        seen = _record_broadcasts(state)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots", json={"name": "s1"})
            assert resp.status == 200
            assert (await resp.json())["folder_id"] == ""

        assert len(seen) == 1
        assert any(s["key"] == "s1" for s in seen[0])

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
    """Folder tags copied onto NEW chats filed into the folder (issue #5419).

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
        state = self._tagged_state(tmp_path, ["t1", "t2"])
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "fresh", "folder_id": FOLDER_ID}
            )
            assert resp.status == 200
            assert sorted((await resp.json())["tags"]) == ["t1", "t2"]
        assert sorted(state._slots["fresh"].tags) == ["t1", "t2"]

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
        """A folder id that no longer exists in the vocabulary is dropped, not stamped."""
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
