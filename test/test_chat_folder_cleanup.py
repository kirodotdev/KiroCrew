"""Bulk delete of folders that hold no live session.

``POST /api/chat/folders/cleanup`` lists (dry run) or deletes every folder whose
subtree holds no live session and no setting the person chose. These tests pin
which rows it takes, which it spares, and that only the person may call it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat_folder_cleanup import api_chat_folders_cleanup, removable_folder_ids
from kiro_crew.dashboard.folder_repository import FolderRepository
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.loop_lock import LoopBoundLock


def _f(fid: str, parent: str = "", order: int = 0, **extra: Any) -> dict[str, Any]:
    return {"id": fid, "name": fid, "parent_id": parent, "order": order, **extra}


def _tree() -> list[dict[str, Any]]:
    # work/            top level, person-made
    #   goal/          empty branch an agent left behind
    #     worker/
    #   busy/          one live session below
    #     lead/        <- live session filed here
    #   repo/          carries a project directory
    # Slack            channel folder
    # Travel           empty top-level folder
    return [
        _f("work", order=0),
        _f("goal", "work", 0),
        _f("worker", "goal", 0),
        _f("busy", "work", 1),
        _f("lead", "busy", 0),
        _f("repo", "work", 2, project_dir="/srv/repo"),
        _f("slack", order=1, channel="slack"),
        _f("travel", order=2),
    ]


def _slot(key: str, folder_id: str) -> _ChatSlot:
    slot = _ChatSlot(key)
    slot.folder_id = folder_id
    return slot


class TestRemovableFolderIds:
    def test_takes_empty_branches_below_the_top_level(self) -> None:
        ids = removable_folder_ids(_tree(), {"lead"}, include_top_level=False)
        assert ids == ["goal", "worker"]

    def test_top_level_is_opt_in(self) -> None:
        ids = removable_folder_ids(_tree(), {"lead"}, include_top_level=True)
        assert ids == ["goal", "worker", "travel"]

    def test_a_live_session_keeps_every_ancestor(self) -> None:
        ids = removable_folder_ids(_tree(), {"lead"}, include_top_level=True)
        assert "busy" not in ids and "lead" not in ids and "work" not in ids

    def test_once_the_live_session_is_gone_its_branch_goes_too(self) -> None:
        ids = removable_folder_ids(_tree(), set(), include_top_level=False)
        assert ids == ["goal", "worker", "busy", "lead"]

    @pytest.mark.parametrize(
        "field,value",
        [
            ("project_dir", "/srv/x"),
            ("default_agent", "kirocrew-worker"),
            ("steering_dirs", ["/srv/steer"]),
            ("tags", ["t1"]),
            ("color", "#3b82f6"),
            ("icon", "🧪"),
            ("channel", "discord"),
        ],
    )
    def test_a_configured_folder_is_kept_with_its_ancestors(self, field: str, value: Any) -> None:
        folders = [_f("top"), _f("mid", "top"), _f("leaf", "mid", **{field: value})]
        assert removable_folder_ids(folders, set(), include_top_level=True) == []

    def test_a_kept_child_does_not_hide_an_empty_sibling(self) -> None:
        folders = [_f("top"), _f("kept", "top", 0, tags=["t"]), _f("empty", "top", 1)]
        assert removable_folder_ids(folders, set(), include_top_level=True) == ["empty"]

    def test_a_malformed_stored_order_does_not_break_the_walk(self) -> None:
        # folders.json is loaded verbatim, so a hand edit can leave siblings
        # with orders that do not compare; the walk must still finish.
        rows = [
            _f("work"),
            {**_f("goal", "work"), "order": "2"},
            {**_f("worker", "work"), "order": None},
            {**_f("nan", "work"), "order": float("nan")},
            {**_f("flag", "work"), "order": True},
            _f("plain", "work", 1),
        ]
        assert sorted(removable_folder_ids(rows, set(), include_top_level=False)) == [
            "flag",
            "goal",
            "nan",
            "plain",
            "worker",
        ]

    def test_a_folder_a_channel_files_into_by_name_is_kept(self) -> None:
        # A channel adopts an existing folder by name without stamping it, so
        # the name its setting carries is what marks it; matched like the
        # channel lookup, ignoring case and surrounding space.
        ids = removable_folder_ids(
            _tree(), {"lead"}, include_top_level=True, kept_names=[" TRAVEL "]
        )
        assert ids == ["goal", "worker"]

    def test_a_duplicated_id_keeps_every_row_and_its_parent(self) -> None:
        # Deleting by id removes every row carrying it, so an empty row must
        # not take a configured twin (here filed under another parent) along.
        folders = [
            _f("top"),
            _f("a", "top"),
            _f("dup", "a"),
            _f("b", "top"),
            _f("dup", "b", project_dir="/srv/x"),
            _f("free", "top"),
        ]
        assert removable_folder_ids(folders, set(), include_top_level=False) == ["free"]

    def test_a_parent_cycle_keeps_the_rows_on_it(self) -> None:
        folders = [_f("a", "b"), _f("b", "a"), _f("c")]
        assert removable_folder_ids(folders, set(), include_top_level=True) == ["c"]


def _state(
    *slots: _ChatSlot,
    archived: list[dict[str, Any]] | None = None,
    job_folders: set[str] | None = None,
) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state.crons = MagicMock()
    state.crons.chat_folder_ids_async = AsyncMock(return_value=set(job_folders or ()))
    state._folders = _tree()
    state._slots = {s.key: s for s in slots}
    state.push_slots_update = MagicMock()
    state.conversation_log = MagicMock()
    state.conversation_log.list_sessions.return_value = archived or []
    repo = FolderRepository(lambda: MagicMock())
    lock = LoopBoundLock()

    async def _mutate(fn: Any, on_committed: Any = None, prepare: Any = None) -> Any:
        return await repo.mutate(
            lambda: state._folders,
            lock,
            fn,
            lambda: MagicMock(name="folders.json"),
            lambda _path, _snapshot: None,
            on_committed,
            prepare,
        )

    async def _read(fn: Any) -> Any:
        return await repo.read(lambda: state._folders, lock, fn)

    state.mutate_folders = _mutate
    state.read_folders = _read
    state.folder_lock = lock
    return state


def _app(state: DashboardState, *, app_scope: str = "", member: str = "") -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _scope(request: web.Request, handler: Any) -> Any:
        request["app"] = app_scope
        if member:
            request["member_chat_principal"] = member
        return await handler(request)

    app.middlewares.append(_scope)
    app.router.add_post("/api/chat/folders/cleanup", api_chat_folders_cleanup)
    return app


def _ids(state: DashboardState) -> set[str]:
    return {f["id"] for f in state._folders}


class TestCleanupRoute:
    @pytest.mark.asyncio
    async def test_a_channel_configured_folder_is_not_offered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = _state(_slot("chat-1-1", "lead"))
        under_lock: list[bool] = []

        def _names() -> set[str]:
            # The delete reads it inside the store lock, so a channel save that
            # adopts the folder cannot commit between this read and the delete.
            # (The dry run is a preview and reads it outside.)
            under_lock.append(state.folder_lock.locked())
            return {"travel"}

        monkeypatch.setattr("kiro_crew.dashboard.chat_folder_cleanup.channel_folder_names", _names)
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders/cleanup", json={"dry_run": True, "include_top_level": True}
            )
            body = await resp.json()
            assert body["ids"] == ["goal", "worker"]
            resp = await client.post(
                "/api/chat/folders/cleanup",
                json={"include_top_level": True, "ids": ["goal", "worker", "travel"]},
            )
            body = await resp.json()
        assert body["deleted"] == ["goal", "worker"]
        assert "travel" in _ids(state)
        assert under_lock == [False, True]

    @pytest.mark.asyncio
    async def test_a_folder_a_saved_job_files_into_is_kept(self) -> None:
        # The job keeps the folder's id; deleting it would land every later
        # run's tab unfiled, and recreating it by name would not help.
        state = _state(_slot("chat-1-1", "lead"), job_folders={"worker"})
        async with TestClient(TestServer(_app(state))) as client:
            dry = await (
                await client.post("/api/chat/folders/cleanup", json={"dry_run": True})
            ).json()
            real = await (
                await client.post("/api/chat/folders/cleanup", json={"ids": ["goal", "worker"]})
            ).json()
        assert dry["ids"] == []
        assert real["deleted"] == []
        assert {"goal", "worker"} <= _ids(state)

    @pytest.mark.asyncio
    async def test_the_delete_reads_saved_jobs_under_the_folder_lock(self) -> None:
        # A job save that names a folder writes while holding the same lock, so
        # reading jobs under it means no save can land between read and delete.
        state = _state(_slot("chat-1-1", "lead"))
        held: list[bool] = []

        async def _jobs() -> set[str]:
            held.append(state.folder_lock.locked())
            return set()

        state.crons.chat_folder_ids_async = _jobs
        async with TestClient(TestServer(_app(state))) as client:
            await client.post("/api/chat/folders/cleanup", json={"dry_run": True})
            await client.post("/api/chat/folders/cleanup", json={"ids": ["goal", "worker"]})
        assert held == [False, True]

    @pytest.mark.asyncio
    async def test_unreadable_job_store_deletes_nothing(self) -> None:
        state = _state(_slot("chat-1-1", "lead"))
        state.crons.chat_folder_ids_async = AsyncMock(side_effect=RuntimeError("store busy"))
        before = _ids(state)
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.post("/api/chat/folders/cleanup", json={"ids": ["goal", "worker"]})
            assert resp.status == 503
            assert (await resp.json())["code"] == "cron_unreadable"
        assert _ids(state) == before

    @pytest.mark.asyncio
    async def test_unreadable_channel_config_deletes_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _broken() -> set[str]:
            raise OSError("config.json unreadable")

        monkeypatch.setattr("kiro_crew.dashboard.chat_folder_cleanup.channel_folder_names", _broken)
        state = _state(_slot("chat-1-1", "lead"))
        before = _ids(state)
        async with TestClient(TestServer(_app(state))) as client:
            dry = await client.post("/api/chat/folders/cleanup", json={"dry_run": True})
            real = await client.post("/api/chat/folders/cleanup", json={"ids": ["goal", "worker"]})
            assert dry.status == 503
            assert real.status == 503
            assert (await real.json())["code"] == "config_unreadable"
        assert _ids(state) == before

    @pytest.mark.asyncio
    async def test_dry_run_lists_and_changes_nothing(self) -> None:
        state = _state(
            _slot("chat-1-1", "lead"),
            archived=[{"folder_id": "worker"}, {"folder_id": "worker"}, {"folder_id": "busy"}],
        )
        before = _ids(state)
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.post("/api/chat/folders/cleanup", json={"dry_run": True})
            body = await resp.json()
        assert resp.status == 200
        assert body["ids"] == ["goal", "worker"]
        # Archived sessions are reported so the preview can say what loses its
        # folder; they do not keep the folder.
        assert body["archived"] == {"worker": 2}
        assert _ids(state) == before

    @pytest.mark.asyncio
    async def test_delete_removes_only_the_listed_rows(self) -> None:
        state = _state(_slot("chat-1-1", "lead"))
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.post("/api/chat/folders/cleanup", json={"ids": ["goal", "worker"]})
            body = await resp.json()
        assert resp.status == 200
        assert body["deleted"] == ["goal", "worker"]
        assert _ids(state) == {"work", "busy", "lead", "repo", "slack", "travel"}
        state.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_with_top_level(self) -> None:
        state = _state(_slot("chat-1-1", "lead"))
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders/cleanup",
                json={"include_top_level": True, "ids": ["goal", "worker", "travel"]},
            )
            body = await resp.json()
        assert body["deleted"] == ["goal", "worker", "travel"]

    @pytest.mark.asyncio
    async def test_a_session_filed_after_the_preview_keeps_its_folder(self) -> None:
        state = _state()
        async with TestClient(TestServer(_app(state))) as client:
            preview = await (
                await client.post("/api/chat/folders/cleanup", json={"dry_run": True})
            ).json()
            assert "worker" in preview["ids"]
            state._slots["chat-1-2"] = _slot("chat-1-2", "worker")
            body = await (
                await client.post("/api/chat/folders/cleanup", json={"ids": preview["ids"]})
            ).json()
        assert "worker" not in body["deleted"] and "goal" not in body["deleted"]
        assert {"goal", "worker"} <= _ids(state)

    @pytest.mark.asyncio
    async def test_a_folder_emptied_after_the_preview_is_not_deleted(self) -> None:
        # The preview showed goal/worker only; busy/lead empty out afterwards.
        state = _state(_slot("chat-1-1", "lead"))
        async with TestClient(TestServer(_app(state))) as client:
            preview = await (
                await client.post("/api/chat/folders/cleanup", json={"dry_run": True})
            ).json()
            assert preview["ids"] == ["goal", "worker"]
            del state._slots["chat-1-1"]
            body = await (
                await client.post("/api/chat/folders/cleanup", json={"ids": preview["ids"]})
            ).json()
        assert body["deleted"] == ["goal", "worker"]
        assert {"busy", "lead"} <= _ids(state)

    @pytest.mark.asyncio
    async def test_a_child_made_after_the_preview_keeps_its_parents(self) -> None:
        state = _state(_slot("chat-1-1", "lead"))
        async with TestClient(TestServer(_app(state))) as client:
            preview = await (
                await client.post("/api/chat/folders/cleanup", json={"dry_run": True})
            ).json()
            state._folders.append(_f("new", "worker"))
            body = await (
                await client.post("/api/chat/folders/cleanup", json={"ids": preview["ids"]})
            ).json()
        assert body["deleted"] == []
        assert {"goal", "worker", "new"} <= _ids(state)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [{}, {"ids": "goal"}, {"ids": [1]}])
    async def test_delete_without_previewed_ids_is_refused(self, payload: dict[str, Any]) -> None:
        state = _state()
        before = _ids(state)
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.post("/api/chat/folders/cleanup", json=payload)
            body = await resp.json()
        assert resp.status == 400
        assert body["code"] == "invalid_ids"
        assert _ids(state) == before

    @pytest.mark.asyncio
    async def test_nothing_to_delete_writes_nothing(self) -> None:
        state = _state()
        state._folders = [_f("only")]
        async with TestClient(TestServer(_app(state))) as client:
            body = await (await client.post("/api/chat/folders/cleanup", json={"ids": []})).json()
        assert body["deleted"] == []
        state.push_slots_update.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("scope", [{"app_scope": "issue-radar"}, {"member": "member:abc"}])
    async def test_agents_are_refused(self, scope: dict[str, str]) -> None:
        state = _state()
        before = _ids(state)
        async with TestClient(TestServer(_app(state, **scope))) as client:
            resp = await client.post("/api/chat/folders/cleanup", json={})
            body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "folder_delete_forbidden"
        assert _ids(state) == before


class TestCronChatFolderIds:
    @pytest.mark.asyncio
    async def test_reads_every_job_folder_and_fails_closed_on_a_bad_store(
        self, tmp_path: Any
    ) -> None:
        from kiro_crew.cron import CronService, CronStoreUnreadable

        svc = CronService(base_dir=tmp_path)
        svc.add_job("a", "m", every_secs=300, persistent_session=True, chat_folder_id="f1")
        svc.add_job("b", "m", every_secs=300)
        assert await svc.chat_folder_ids_async() == {"f1"}

        (tmp_path / "crons.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(CronStoreUnreadable):
            await svc.chat_folder_ids_async()


class TestChannelFolderNames:
    @pytest.mark.parametrize("degraded", [{"*"}, {"slack"}])
    def test_a_degraded_load_raises_instead_of_reading_as_empty(
        self, monkeypatch: pytest.MonkeyPatch, degraded: set[str]
    ) -> None:
        from types import SimpleNamespace

        from kiro_crew.dashboard import chat_folder_cleanup as mod

        cfg = SimpleNamespace(degraded_sections=frozenset(degraded))
        monkeypatch.setattr(mod.KiroCrewConfig, "load", staticmethod(lambda: cfg))
        with pytest.raises(mod._ConfigUnreadable):
            mod.channel_folder_names()

    def test_reads_every_channel_s_folder_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        from kiro_crew.dashboard import chat_folder_cleanup as mod

        cfg = SimpleNamespace(
            degraded_sections=frozenset({"memory"}),
            slack=SimpleNamespace(session_folder=" Slack Inbox "),
            discord=SimpleNamespace(session_folder=""),
        )
        monkeypatch.setattr(mod.KiroCrewConfig, "load", staticmethod(lambda: cfg))
        assert mod.channel_folder_names() == {"slack inbox"}
