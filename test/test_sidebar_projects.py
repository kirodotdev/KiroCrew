"""Sidebar Projects: a folder kind whose chats are locked to the conductor agent.

Covers the storage of the ``kind`` flag, and the invariant that inside a Project
no route starts a new chat on any agent other than ``kirocrew-conductor`` --
except a project conductor's own workers, which keep their agent and are filed
beside their creator so the sidebar nests them under it.
"""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app_with_agent_routes, _make_folder_app, _make_state
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard import project_folders as pf
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_folder_cleanup import removable_folder_ids
from kiro_crew.dashboard.chat_utils import slot_history_key

CONDUCTOR = pf.PROJECT_AGENT


@pytest.fixture(autouse=True)
def _isolate(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    _floor_monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    from kiro_crew.dashboard import create_rate_limit

    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


def _tree(state):
    """A project ``P``, a folder ``P/sub`` under it, and an ordinary folder ``F``.

    No route can put a folder under a Project any more (``TestTopLevelOnly``);
    ``P/sub`` stands for a row a damaged or hand-edited store still holds, which
    the ancestor walk keeps locked the same way as the Project itself.
    """
    state._folders.extend(
        [
            {"id": "proj00000001", "name": "P", "parent_id": "", "order": 0, "kind": "project"},
            {"id": "subf00000001", "name": "sub", "parent_id": "proj00000001", "order": 1},
            {"id": "fold00000001", "name": "F", "parent_id": "", "order": 2},
        ]
    )


def _resolve_everything(monkeypatch, target):
    """Make every agent name resolve, as an installed template would.

    The test config has no ``kirocrew-conductor`` template installed, and a
    stated namespace refuses an unresolved name; the lock under test runs before
    resolution, so resolution itself is not what these tests are about.
    """
    real = target.resolve_agent_bindings

    def resolve(cfg, agent_name=None, project_dir=None, **kwargs):
        return dataclasses.replace(real(cfg, None, project_dir), requested_resolved=True)

    monkeypatch.setattr(target, "resolve_agent_bindings", resolve)


# ── Folder predicates ─────────────────────────────────────────────────────────


class TestPredicates:
    def test_project_root_walks_ancestors_and_survives_a_cycle(self):
        folders = [
            {"id": "p", "kind": "project", "parent_id": ""},
            {"id": "c", "parent_id": "p"},
            {"id": "g", "parent_id": "c"},
            {"id": "plain", "parent_id": ""},
            {"id": "x", "parent_id": "y"},
            {"id": "y", "parent_id": "x"},
        ]
        assert pf.project_root_id(folders, "p") == "p"
        assert pf.project_root_id(folders, "g") == "p"
        assert pf.project_root_id(folders, "plain") == ""
        assert pf.project_root_id(folders, "x") == ""
        assert pf.project_root_id(folders, "") == ""
        assert pf.project_root_id(folders, "missing") == ""

    def test_a_legacy_folder_with_no_kind_is_not_a_project(self):
        assert pf.project_root_id([{"id": "f", "name": "F"}], "f") == ""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(None, ("", None)), ("", ("", None)), ("project", ("project", None))],
    )
    def test_kind_validation_accepts_two_shapes(self, raw, expected):
        assert pf.validate_folder_kind(raw) == expected

    @pytest.mark.parametrize("raw", ["Project", "folder", 1, True, ["project"]])
    def test_kind_validation_refuses_anything_else(self, raw):
        assert pf.validate_folder_kind(raw)[1]

    def test_admits_only_the_project_agent_inside_a_project(self):
        folders = [{"id": "p", "kind": "project"}, {"id": "c", "parent_id": "p"}]
        assert pf.project_admits_agent(folders, "c", CONDUCTOR)
        assert not pf.project_admits_agent(folders, "c", "kirocrew")
        assert pf.project_admits_agent(folders, "", "kirocrew")

    def test_conductor_home_folder_reads_api_rows(self):
        folders = [{"id": "p", "kind": "project"}, {"id": "f"}]
        rows = [
            {"key": "c1", "agent": CONDUCTOR, "folder_id": "p"},
            {"key": "w1", "agent": "kirocrew-worker", "folder_id": "p"},
            {"key": "c2", "agent": CONDUCTOR, "folder_id": "f"},
        ]
        assert pf.conductor_home_folder("dashboard:c1", rows, folders) == "p"
        # A worker in the project is not a conductor; its creates are not nested.
        assert pf.conductor_home_folder("dashboard:w1", rows, folders) == ""
        # A conductor outside a project files where it is told.
        assert pf.conductor_home_folder("dashboard:c2", rows, folders) == ""
        assert pf.conductor_home_folder("dashboard:gone", rows, folders) == ""

    def test_switch_lock_allows_the_project_agent_and_a_same_name_reset(self):
        state = SimpleNamespace(_folders=[{"id": "p", "kind": "project"}])
        slot = SimpleNamespace(key="s", folder_id="p", agent="kirocrew-worker")
        assert pf.project_pins_agent(state, slot, "kirocrew")
        assert not pf.project_pins_agent(state, slot, CONDUCTOR)
        assert not pf.project_pins_agent(state, slot, "kirocrew-worker")
        # The project agent as a same-named crewmate would run on its memory.
        assert pf.project_pins_agent(state, slot, CONDUCTOR, "member")
        assert pf.project_switch_kind(state, slot, CONDUCTOR, "") == "template"
        assert pf.project_switch_kind(state, slot, CONDUCTOR, "member") == "member"
        unfiled = SimpleNamespace(key="u", folder_id="", agent="kirocrew-worker")
        assert not pf.project_pins_agent(state, unfiled, "kirocrew")
        assert pf.project_switch_kind(state, unfiled, CONDUCTOR, "") == ""


class TestCleanupKeepsAnEmptyProject:
    def test_an_empty_project_is_not_removable(self):
        folders = [
            {"id": "p", "name": "P", "kind": "project"},
            {"id": "e", "name": "E"},
        ]
        assert removable_folder_ids(folders, set(), include_top_level=True) == ["e"]


# ── Folder API: the kind is set at create and never changed ──────────────────


class TestFolderApi:
    @pytest.mark.asyncio
    async def test_create_stores_the_project_kind(self, tmp_path):
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            resp = await client.post("/api/chat/folders", json={"name": "Ship", "kind": "project"})
            assert resp.status == 201, await resp.text()
            body = await resp.json()
            plain = await client.post("/api/chat/folders", json={"name": "Plain"})
            assert plain.status == 201
            plain_body = await plain.json()
        assert body["kind"] == "project"
        assert "kind" not in plain_body, "an ordinary folder keeps today's on-disk shape"
        assert next(f for f in state._folders if f["id"] == body["id"])["kind"] == "project"

    @pytest.mark.asyncio
    async def test_create_refuses_an_unknown_kind(self, tmp_path):
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            resp = await client.post("/api/chat/folders", json={"name": "X", "kind": "space"})
            assert resp.status == 400
            assert (await resp.json())["code"] == "folder_kind_invalid"
        assert state._folders == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["project", "", None])
    async def test_patch_cannot_make_or_unmake_a_project(self, tmp_path, value):
        state = _make_state(tmp_path)
        _tree(state)
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            for fid in ("fold00000001", "proj00000001"):
                resp = await client.patch(f"/api/chat/folders/{fid}", json={"kind": value})
                assert resp.status == 400
                assert (await resp.json())["code"] == "folder_kind_immutable"
        kinds = {f["id"]: f.get("kind") for f in state._folders}
        assert kinds["fold00000001"] is None
        assert kinds["proj00000001"] == "project"


# ── Dashboard create route ────────────────────────────────────────────────────


class TestSlotCreateLock:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("folder_id", ["proj00000001", "subf00000001"])
    @pytest.mark.parametrize(
        "requested",
        [{}, {"agent": "kirocrew"}, {"agent": "kirocrew", "agent_kind": "member"}],
    )
    async def test_a_new_chat_in_a_project_runs_on_the_conductor(
        self, tmp_path, monkeypatch, folder_id, requested
    ):
        state = _make_state(tmp_path)
        _tree(state)
        _resolve_everything(monkeypatch, chat_handlers)
        app = _make_app_with_agent_routes(state)
        async with TestClient(TestServer(as_owner(app))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "p-chat", "folder_id": folder_id, **requested}
            )
            assert resp.status == 200, await resp.text()
        slot = state._slots["p-chat"]
        assert slot.agent == CONDUCTOR
        assert slot.agent_kind == "template"
        assert slot.folder_id == folder_id

    @pytest.mark.asyncio
    async def test_an_ordinary_folder_keeps_the_requested_agent(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        _tree(state)
        _resolve_everything(monkeypatch, chat_handlers)
        app = _make_app_with_agent_routes(state)
        async with TestClient(TestServer(as_owner(app))) as client:
            resp = await client.post(
                "/api/chat/slots",
                json={"name": "f-chat", "folder_id": "fold00000001", "agent": "kirocrew"},
            )
            assert resp.status == 200, await resp.text()
        assert state._slots["f-chat"].agent == "kirocrew"

    @pytest.mark.asyncio
    async def test_naming_a_never_run_chat_into_a_project_switches_it_first(
        self, tmp_path, monkeypatch
    ):
        # POST naming an existing slot files it: the same switch the folder PATCH runs.
        state = _make_state(tmp_path)
        _tree(state)
        calls = []

        async def fake(state_, slot, name, agent, kind, caller):
            calls.append((name, agent, kind))
            return web.json_response({"error": "busy", "code": "turn_in_flight"}, status=409)

        monkeypatch.setattr(chat_handlers, "switch_slot_agent", fake)
        state.get_or_create_slot("fresh", agent="kirocrew")
        app = _make_app_with_agent_routes(state)
        async with TestClient(TestServer(as_owner(app))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "fresh", "folder_id": "subf00000001"}
            )
            assert resp.status == 409, await resp.text()
        assert calls == [("fresh", CONDUCTOR, "template")]
        assert state._slots["fresh"].folder_id == ""

    @pytest.mark.asyncio
    async def test_a_named_move_the_folder_refuses_switches_nothing(self, tmp_path, monkeypatch):
        # The folder is validated before the switch: a Project whose directory is
        # gone refuses the request, and the chat keeps its agent.
        state = _make_state(tmp_path)
        _tree(state)
        state._folders[0]["project_dir"] = str(tmp_path / "gone")
        calls = []

        async def fake(state_, slot, name, agent, kind, caller):
            calls.append(name)
            return web.json_response({}, status=200)

        monkeypatch.setattr(chat_handlers, "switch_slot_agent", fake)
        state.get_or_create_slot("fresh", agent="kirocrew")
        app = _make_app_with_agent_routes(state)
        async with TestClient(TestServer(as_owner(app))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "fresh", "folder_id": "proj00000001"}
            )
            assert resp.status == 400, await resp.text()
            assert (await resp.json())["code"] == "folder_project_invalid"
        assert calls == []
        assert state._slots["fresh"].agent == "kirocrew"


# ── Agent switch ──────────────────────────────────────────────────────────────


class TestSwitchLock:
    @pytest.mark.asyncio
    async def test_switch_away_from_the_conductor_is_refused(self, tmp_path):
        state = _make_state(tmp_path)
        _tree(state)
        slot = state.get_or_create_slot("p-chat", agent=CONDUCTOR)
        slot.folder_id = "subf00000001"
        app = _make_app_with_agent_routes(state)
        async with TestClient(TestServer(as_owner(app))) as client:
            for agent in ("kirocrew", ""):
                resp = await client.post("/api/chat/slots/p-chat/agent", json={"agent": agent})
                assert resp.status == 409, await resp.text()
                assert (await resp.json())["code"] == pf.PROJECT_AGENT_LOCKED
        assert slot.agent == CONDUCTOR

    @pytest.mark.asyncio
    async def test_switch_in_an_ordinary_folder_is_not_locked(self, tmp_path):
        state = _make_state(tmp_path)
        _tree(state)
        slot = state.get_or_create_slot("f-chat", agent=CONDUCTOR)
        slot.folder_id = "fold00000001"
        assert pf.project_agent_switch_refusal(state, slot, "kirocrew", "") is None

    @pytest.mark.asyncio
    async def test_the_conductor_as_a_crewmate_is_refused(self, tmp_path):
        state = _make_state(tmp_path)
        _tree(state)
        slot = state.get_or_create_slot("p-chat", agent=CONDUCTOR)
        slot.folder_id = "proj00000001"
        app = _make_app_with_agent_routes(state)
        async with TestClient(TestServer(as_owner(app))) as client:
            resp = await client.post(
                "/api/chat/slots/p-chat/agent",
                json={"agent": CONDUCTOR, "agent_kind": "member"},
            )
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == pf.PROJECT_AGENT_LOCKED
        assert slot.agent == CONDUCTOR


# ── Move into a project ───────────────────────────────────────────────────────


class TestMoveIntoProject:
    def _switch(self, monkeypatch, status=200):
        calls = []

        async def fake(state, slot, name, agent, kind, caller):
            calls.append((name, agent, kind))
            if status == 200:
                slot.agent = agent
                return web.json_response({"ok": True, "agent": agent})
            return web.json_response({"error": "busy", "code": "turn_in_flight"}, status=status)

        monkeypatch.setattr(chat_handlers, "switch_slot_agent", fake)
        return calls

    @pytest.mark.asyncio
    async def test_a_never_run_chat_moved_in_is_put_on_the_conductor(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        _tree(state)
        calls = self._switch(monkeypatch)
        state.get_or_create_slot("fresh", agent="kirocrew")
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            resp = await client.patch(
                "/api/chat/slots/fresh/folder", json={"folder_id": "subf00000001"}
            )
            assert resp.status == 200, await resp.text()
        assert calls == [("fresh", CONDUCTOR, "template")]
        assert state._slots["fresh"].folder_id == "subf00000001"

    @pytest.mark.asyncio
    async def test_a_refused_switch_refuses_the_move(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        _tree(state)
        self._switch(monkeypatch, status=409)
        state.get_or_create_slot("fresh", agent="kirocrew")
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            resp = await client.patch(
                "/api/chat/slots/fresh/folder", json={"folder_id": "proj00000001"}
            )
            assert resp.status == 409
        assert state._slots["fresh"].folder_id == ""

    @pytest.mark.asyncio
    async def test_a_stale_generation_switches_nothing(self, tmp_path, monkeypatch):
        # A delayed self-filing request that resolved an earlier slot under this
        # key: the move is refused, and the chat now under the key keeps its agent.
        state = _make_state(tmp_path)
        _tree(state)
        calls = self._switch(monkeypatch)
        state.get_or_create_slot("fresh", agent="kirocrew")
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            resp = await client.patch(
                "/api/chat/slots/fresh/folder",
                json={"folder_id": "proj00000001", "expected_created": "an-earlier-slot"},
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "session_gone"
        assert calls == []
        assert state._slots["fresh"].agent == "kirocrew"
        assert state._slots["fresh"].folder_id == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "case", ["has_history", "already_conductor", "within_project", "plain"]
    )
    async def test_the_switch_is_skipped_when_nothing_would_start(
        self, tmp_path, monkeypatch, case
    ):
        state = _make_state(tmp_path)
        _tree(state)
        calls = self._switch(monkeypatch)
        slot = state.get_or_create_slot(
            "s", agent=CONDUCTOR if case == "already_conductor" else "kirocrew"
        )
        target = "proj00000001"
        if case == "has_history":
            slot.total_messages = 3
        elif case == "within_project":
            slot.folder_id = "subf00000001"
        elif case == "plain":
            target = "fold00000001"
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s/folder", json={"folder_id": target})
            assert resp.status == 200, await resp.text()
        assert calls == []
        assert state._slots["s"].folder_id == target


# ── session_create ────────────────────────────────────────────────────────────


class TestSessionCreate:
    def _key(self, slot) -> str:
        return slot_history_key(slot)

    def test_a_conductors_worker_is_filed_beside_it_and_keeps_its_agent(
        self, tmp_path, monkeypatch
    ):
        state = _make_state(tmp_path)
        _tree(state)
        _resolve_everything(monkeypatch, sc)
        conductor = state.get_or_create_slot("cond", agent=CONDUCTOR)
        conductor.folder_id = "proj00000001"
        for folder_id in ("", "fold00000001", "subf00000001"):
            created = asyncio.run(
                sc.create_session(
                    state,
                    caller_session_key=self._key(conductor),
                    agent="kirocrew-worker",
                    agent_kind="template",
                    folder_id=folder_id,
                )
            )
            child = state.get_slot(created["target"])
            assert child is not None
            assert child.folder_id == "proj00000001", folder_id
            assert child.agent == "kirocrew-worker"
            assert child._created_by == conductor.key or child._created_by.endswith(conductor.key)

    @pytest.mark.parametrize("caller_folder", ["", "proj00000001"])
    def test_any_other_create_into_a_project_runs_on_the_conductor(
        self, tmp_path, monkeypatch, caller_folder
    ):
        state = _make_state(tmp_path)
        _tree(state)
        _resolve_everything(monkeypatch, sc)
        # A plain chat, or a worker already inside the project (not a conductor).
        caller = state.get_or_create_slot("other", agent="kirocrew-worker")
        caller.folder_id = caller_folder
        created = asyncio.run(
            sc.create_session(
                state,
                caller_session_key=self._key(caller),
                agent="kirocrew-worker",
                folder_id="subf00000001",
            )
        )
        child = state.get_slot(created["target"])
        assert child is not None
        assert child.agent == CONDUCTOR
        assert child.folder_id == "subf00000001"

    def test_an_ordinary_folder_create_is_unchanged(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        _tree(state)
        _resolve_everything(monkeypatch, sc)
        caller = state.get_or_create_slot("other", agent="kirocrew-worker")
        created = asyncio.run(
            sc.create_session(
                state,
                caller_session_key=self._key(caller),
                agent="kirocrew-worker",
                folder_id="fold00000001",
            )
        )
        child = state.get_slot(created["target"])
        assert child.agent == "kirocrew-worker"
        assert child.folder_id == "fold00000001"


# ── Paths that inherit or configure an agent ─────────────────────────────────


class TestInheritedAgentPaths:
    def test_a_cron_tab_on_another_agent_lands_unfiled(self, tmp_path):
        from kiro_crew.dashboard.cron_inject import chat_folder_for_minted_tab

        state = _make_state(tmp_path)
        _tree(state)
        job = SimpleNamespace(
            id="j", name="nightly", chat_folder_id="subf00000001", member_id="", agent_id="kirocrew"
        )
        assert chat_folder_for_minted_tab(state, job) == ""
        job.agent_id = CONDUCTOR
        assert chat_folder_for_minted_tab(state, job) == "subf00000001"
        job.agent_id, job.chat_folder_id = "kirocrew", "fold00000001"
        assert chat_folder_for_minted_tab(state, job) == "fold00000001"

    def test_a_cron_tab_on_another_agent_is_not_moved_into_a_project(self, tmp_path):
        from kiro_crew.dashboard.cron_inject import move_cron_job_tab

        state = _make_state(tmp_path)
        _tree(state)
        job = SimpleNamespace(id="j", name="nightly", member_id="", agent_id="kirocrew")
        slot = state.get_or_create_slot("cron-j", agent="kirocrew")
        slot.folder_id = "fold00000001"
        asyncio.run(move_cron_job_tab(state, job, "fold00000001", "subf00000001"))
        assert slot.folder_id == "fold00000001"

    def test_session_fork_into_a_project_refuses_another_agent(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        _tree(state)
        caller = state.get_or_create_slot("src", agent="kirocrew")
        caller.append("user", "hello", "msg msg-u")
        with pytest.raises(sc.SessionControlError) as err:
            asyncio.run(
                sc.fork_session(
                    state,
                    caller_session_key=slot_history_key(caller),
                    folder_id="proj00000001",
                )
            )
        assert err.value.code == pf.PROJECT_AGENT_LOCKED

    @pytest.mark.parametrize(("name", "expected"), [("P", ""), ("sub", ""), ("F", "fold00000001")])
    def test_a_channel_folder_inside_a_project_files_nothing(self, tmp_path, name, expected):
        # Resolved to nothing at the one lookup every channel filing path uses
        # (reconcile, inbound surfacing, backfill), so no placement into a
        # Project is ever persisted for a channel arrival.
        from kiro_crew.dashboard.channel_folders import folder_id_for_name

        state = _make_state(tmp_path)
        _tree(state)
        assert asyncio.run(folder_id_for_name(state, "slack", name)) == expected


def test_mcp_session_create_makes_no_path_segments_for_a_project_conductor():
    """The MCP walk would mkdir ``<goal>/<agent>`` inside the project; it is skipped."""
    from kiro_crew import mcp_dashboard as md

    client = AsyncMock()
    rows = {
        "/api/chat/slots": [{"key": "cond", "agent": CONDUCTOR, "folder_id": "p"}],
        "/api/chat/folders": [{"id": "p", "name": "Ship", "parent_id": "", "kind": "project"}],
    }
    calls: list[str] = []

    def get_rows(_client, path):
        calls.append(path)
        return rows[path], None

    ctx = SimpleNamespace(client=client)
    import unittest.mock as um

    with (
        um.patch.object(
            md, "_refuse_tree_shaping_if_unverifiable", return_value=("dashboard:cond", "", None)
        ),
        um.patch.object(md, "_get_rows", side_effect=get_rows),
        um.patch.object(md, "_ensure_chat_folder_path") as walk,
    ):
        out = md._resolve_folder_for_new_session(
            ctx, "Goal/kirocrew-worker", "filing", nest_in_project=True
        )
        assert out == ("", "Ship", "", None)
        walk.assert_not_called()
        # Without the flag (fork, revive) the walk runs as before.
        walk.return_value = ("f2", [], None)
        md._resolve_folder_for_new_session(ctx, "Goal/kirocrew-worker", "filing")
        walk.assert_called_once()


# ── Paths an independent break attempt found ──────────────────────────────────


def _spy_switch(monkeypatch):
    calls = []

    async def fake(state, slot, name, agent, kind, caller):
        calls.append((name, agent, kind))
        slot.agent, slot.agent_kind = agent, kind
        return web.json_response({"ok": True, "agent": agent})

    monkeypatch.setattr(chat_handlers, "switch_slot_agent", fake)
    return calls


class TestTopLevelOnly:
    """A Project is a top-level sidebar item holding chats only."""

    @pytest.mark.asyncio
    async def test_a_project_cannot_be_created_inside_a_folder(self, tmp_path):
        state = _make_state(tmp_path)
        _tree(state)
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Nested", "kind": "project", "parent_id": "fold00000001"},
            )
            assert resp.status == 400, await resp.text()
            assert (await resp.json())["code"] == pf.PROJECT_TOP_LEVEL
        assert not any(f["name"] == "Nested" for f in state._folders)

    @pytest.mark.asyncio
    async def test_no_folder_can_be_created_inside_a_project(self, tmp_path):
        state = _make_state(tmp_path)
        _tree(state)
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            for parent in ("proj00000001", "subf00000001"):
                resp = await client.post(
                    "/api/chat/folders", json={"name": "kirocrew-worker", "parent_id": parent}
                )
                assert resp.status == 400, await resp.text()
                assert (await resp.json())["code"] == pf.PROJECT_TOP_LEVEL
            # An ordinary folder still nests as it does today.
            ok = await client.post(
                "/api/chat/folders", json={"name": "child", "parent_id": "fold00000001"}
            )
            assert ok.status == 201, await ok.text()
        assert not any(f["name"] == "kirocrew-worker" for f in state._folders)

    @pytest.mark.asyncio
    async def test_a_project_cannot_be_moved_into_a_folder(self, tmp_path):
        state = _make_state(tmp_path)
        _tree(state)
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            resp = await client.patch(
                "/api/chat/folders/proj00000001", json={"parent_id": "fold00000001"}
            )
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == pf.PROJECT_TOP_LEVEL
            # Moving it to the top level, where it already is, stays allowed.
            top = await client.patch("/api/chat/folders/proj00000001", json={"parent_id": ""})
            assert top.status == 200, await top.text()
        assert next(f for f in state._folders if f["id"] == "proj00000001")["parent_id"] == ""

    @pytest.mark.asyncio
    async def test_no_folder_can_be_moved_into_a_project(self, tmp_path):
        state = _make_state(tmp_path)
        _tree(state)
        # A folder whose chats have all run is refused too: a Project holds no folders.
        slot = state.get_or_create_slot("old", agent="kirocrew")
        slot.folder_id, slot.total_messages = "fold00000001", 2
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            for parent in ("proj00000001", "subf00000001"):
                resp = await client.patch(
                    "/api/chat/folders/fold00000001", json={"parent_id": parent}
                )
                assert resp.status == 409, await resp.text()
                assert (await resp.json())["code"] == pf.PROJECT_TOP_LEVEL
        assert next(f for f in state._folders if f["id"] == "fold00000001")["parent_id"] == ""
        assert pf.slot_project_id(state, slot) == ""

    def test_the_predicate_allows_the_top_level_and_ordinary_nesting(self):
        folders = [{"id": "p", "kind": "project"}, {"id": "f", "parent_id": ""}]
        assert pf.project_nesting_error(folders, True, "") == ""
        assert pf.project_nesting_error(folders, False, "") == ""
        assert pf.project_nesting_error(folders, False, "f") == ""
        assert pf.project_nesting_error(folders, True, "f") == "project_nested"
        assert pf.project_nesting_error(folders, False, "p") == "project_parent"


class TestLockHoles:
    @pytest.mark.asyncio
    async def test_a_send_cannot_bind_an_agent_onto_an_agentless_project_chat(self, tmp_path):
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        _tree(state)
        blank = state.get_or_create_slot("blank")
        blank.folder_id = "proj00000001"
        blank.total_messages = 1
        async with TestClient(TestServer(as_owner(_make_app(state)))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "blank", "agent": "kirocrew-dev", "message": ""}
            )
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == pf.PROJECT_AGENT_LOCKED
        assert blank.agent == ""

    @pytest.mark.asyncio
    async def test_a_send_binds_a_bare_conductor_as_the_template(self, tmp_path):
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        _tree(state)
        blank = state.get_or_create_slot("blank")
        blank.folder_id = "proj00000001"
        blank.total_messages = 1
        async with TestClient(TestServer(as_owner(_make_app(state)))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "blank", "agent": CONDUCTOR, "message": ""}
            )
            assert resp.status == 400, await resp.text()  # message_required, after the bind
        assert (blank.agent, blank.agent_kind) == (CONDUCTOR, "template")

    @pytest.mark.asyncio
    async def test_a_crewmate_named_like_the_conductor_is_not_the_conductor(
        self, tmp_path, monkeypatch
    ):
        state = _make_state(tmp_path)
        _tree(state)
        calls = _spy_switch(monkeypatch)
        slot = state.get_or_create_slot("m", agent=CONDUCTOR)
        slot.agent_kind = "member"
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            resp = await client.patch(
                "/api/chat/slots/m/folder", json={"folder_id": "proj00000001"}
            )
            assert resp.status == 200, await resp.text()
        # Switched to the template like any other never-run chat moved in.
        assert calls == [("m", CONDUCTOR, "template")]
        member = SimpleNamespace(folder_id="proj00000001", agent=CONDUCTOR, agent_kind="member")
        assert not pf.is_project_conductor(state, member, "proj00000001")
        assert not pf.project_admits_agent(state._folders, "proj00000001", CONDUCTOR, "member")

    @pytest.mark.asyncio
    async def test_a_same_name_chat_created_meanwhile_is_not_filed_in(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        _tree(state)
        _resolve_everything(monkeypatch, chat_handlers)
        real = chat_handlers.resolve_folder_project_dir_off_loop

        async def racing(snapshot, folder_id):
            state.get_or_create_slot("racy", agent="kirocrew")
            return await real(snapshot, folder_id)

        monkeypatch.setattr(chat_handlers, "resolve_folder_project_dir_off_loop", racing)
        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "racy", "folder_id": "proj00000001"}
            )
            assert resp.status == 409, await resp.text()
        assert state._slots["racy"].folder_id == ""

    def test_an_import_never_lands_in_a_project_named_imported(self, tmp_path):
        from kiro_crew.dashboard.arrival_folders import arrival_folder_id

        state = _make_state(tmp_path)
        state._folders.append(
            {
                "id": "impp00000001",
                "name": "Imported",
                "parent_id": "",
                "order": 0,
                "kind": "project",
            }
        )
        filing = asyncio.run(arrival_folder_id(state, origin="laptop", request_app=""))
        assert filing.folder_id
        assert pf.project_root_id(state._folders, filing.folder_id) == ""

    def test_a_cron_job_on_a_crewmate_named_like_the_conductor_lands_unfiled(self, tmp_path):
        from kiro_crew.dashboard.cron_inject import chat_folder_for_minted_tab

        state = _make_state(tmp_path)
        _tree(state)
        job = SimpleNamespace(
            id="j", name="n", chat_folder_id="subf00000001", member_id=CONDUCTOR, agent_id=""
        )
        assert chat_folder_for_minted_tab(state, job) == ""

    @pytest.mark.asyncio
    async def test_a_cron_tab_leaves_the_project_when_its_agent_changes(self, tmp_path):
        from kiro_crew.dashboard import cron_inject

        state = _make_state(tmp_path)
        _tree(state)
        job = SimpleNamespace(
            id="j",
            name="n",
            chat_folder_id="subf00000001",
            member_id="",
            agent_id="kirocrew",
            memory_store="",
        )
        tab = state.get_or_create_slot("cron-j", agent=CONDUCTOR)
        tab.folder_id = "subf00000001"
        tab.linked_session_key = "cron:j"
        cron_inject._bind_cron_slot(state, job, [])
        await asyncio.gather(*list(state._background_tasks))
        assert tab.folder_id == ""

    @pytest.mark.asyncio
    async def test_a_move_made_while_the_project_exit_waits_stands(self, tmp_path):
        from kiro_crew.dashboard import cron_inject
        from kiro_crew.dashboard.chat_folders import _slot_meta_txn_lock

        state = _make_state(tmp_path)
        _tree(state)
        job = SimpleNamespace(
            id="j",
            name="n",
            chat_folder_id="subf00000001",
            member_id="",
            agent_id="kirocrew",
            memory_store="",
        )
        tab = state.get_or_create_slot("cron-j", agent=CONDUCTOR)
        tab.folder_id = "subf00000001"
        tab.linked_session_key = "cron:j"
        # A person's move holds the metadata lock when the exit is scheduled and
        # commits before the exit gets it: the exit must not undo that move.
        async with _slot_meta_txn_lock(state):
            cron_inject._bind_cron_slot(state, job, [])
            await asyncio.sleep(0)
            tab.folder_id = "fold00000001"
        await asyncio.gather(*list(state._background_tasks))
        assert tab.folder_id == "fold00000001"

    @pytest.mark.asyncio
    async def test_a_cron_tab_a_person_dragged_into_a_project_stays(self, tmp_path):
        from kiro_crew.dashboard import cron_inject

        state = _make_state(tmp_path)
        _tree(state)
        job = SimpleNamespace(
            id="j",
            name="n",
            chat_folder_id="",
            member_id="",
            agent_id="kirocrew",
            memory_store="",
        )
        tab = state.get_or_create_slot("cron-j", agent="kirocrew")
        tab.folder_id = "subf00000001"  # an existing chat, moved in on its own agent
        tab.linked_session_key = "cron:j"
        cron_inject._bind_cron_slot(state, job, [])
        await asyncio.gather(*list(state._background_tasks))
        assert tab.folder_id == "subf00000001"

    def test_a_send_naming_a_crewmate_chats_own_agent_is_not_pinned(self, tmp_path):
        # A crewmate chat that already ran keeps its agent in a Project; a send
        # naming that same agent (no kind on the wire) re-binds nothing.
        state = _make_state(tmp_path)
        _tree(state)
        chat = state.get_or_create_slot("c", agent="maya")
        chat.agent_kind = "member"
        chat.folder_id = "proj00000001"
        assert not pf.project_pins_agent(state, chat, "maya", "")

    def test_a_fork_keeps_the_template_kind(self, tmp_path):
        state = _make_state(tmp_path)
        _tree(state)
        cond = state.get_or_create_slot("cond", agent=CONDUCTOR)
        cond.agent_kind = "template"
        cond.folder_id = "proj00000001"
        cond.append("user", "hello", "msg msg-u")
        cond.append("assistant", "hi", "msg msg-a")
        created = asyncio.run(sc.fork_session(state, caller_session_key=slot_history_key(cond)))
        child = state.get_slot(created["target"])
        assert (child.folder_id, child.agent, child.agent_kind) == (
            "proj00000001",
            CONDUCTOR,
            "template",
        )

    def test_a_same_name_reset_stays_in_its_namespace(self, tmp_path):
        state = _make_state(tmp_path)
        _tree(state)
        worker = state.get_or_create_slot("w", agent="kirocrew-worker")
        worker.agent_kind = "template"
        worker.folder_id = "proj00000001"
        assert pf.project_pins_agent(state, worker, "kirocrew-worker", "member")
        assert not pf.project_pins_agent(state, worker, "kirocrew-worker", "template")
        assert pf.project_switch_kind(state, worker, "kirocrew-worker", "") == "template"
