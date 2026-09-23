from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, cast
from unittest.mock import AsyncMock

import pytest

import kiro_crew
from kiro_crew.dashboard.remote_subagents import (
    RemoteSubagentError,
    RemoteSubagentService,
)
from kiro_crew.dashboard.remote_workspaces import (
    WorkspaceArchiveRejected,
    api_remote_workspace_upload,
    install_workspace,
)
from kiro_crew.mcp_tools import spawn as spawn_tools
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.validation import SPAWN_RUN_SCHEMA, validate_tool_args


class _Content:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def read(self, limit: int) -> bytes:
        return self._body[:limit]


class _Response:
    def __init__(self, status: int, payload: dict[str, object]) -> None:
        self.status = status
        self.content = _Content(json.dumps(payload).encode("utf-8"))


class _Instances:
    def __init__(
        self,
        responses: list[tuple[int, dict[str, object]] | BaseException],
        *,
        connected: tuple[str, ...] = ("crew-a", "crew-b"),
    ) -> None:
        self._responses = list(responses)
        self._connected = connected
        self.calls: list[
            tuple[str, str, str, dict[str, object] | bytes | None]
        ] = []

    def status(self, instance_id: str) -> object | None:
        if instance_id not in self._connected:
            return None
        return SimpleNamespace(state=SimpleNamespace(value="connected"))

    def status_all(self) -> dict[str, object]:
        return {
            instance_id: SimpleNamespace(state=SimpleNamespace(value="connected"))
            for instance_id in self._connected
        }

    async def peer_version(self, _instance_id: str) -> tuple[bool, str]:
        return True, kiro_crew.__version__

    @asynccontextmanager
    async def proxy_request(
        self,
        instance_id: str,
        method: str,
        path: str,
        *,
        data: bytes | None = None,
        **_kwargs: object,
    ):
        try:
            body = json.loads(data) if data else None
        except (ValueError, UnicodeDecodeError):
            body = data
        self.calls.append((instance_id, method, path, body))
        scripted = self._responses.pop(0)
        if isinstance(scripted, BaseException):
            raise scripted
        status, payload = scripted
        yield _Response(status, payload)


class _Manager:
    def __init__(self) -> None:
        self.external_agents: list[SubagentInfo] = []
        self.events: list[tuple[str, str, dict[str, object]]] = []
        self.reported: list[SubagentInfo] = []
        self._completion_keep = "head"
        self._completion_keep_chars = 3
        self._result_ttl_secs = 3600
        self.external_canceller: Callable[[SubagentInfo], Awaitable[bool]] | None = None

    def get(self, agent_id: str) -> SubagentInfo | None:
        return next((info for info in self.external_agents if info.id == agent_id), None)

    def forget_external(self, agent_id: str) -> SubagentInfo | None:
        info = self.get(agent_id)
        if info is not None:
            self.external_agents.remove(info)
        return info

    def set_external_canceller(
        self, callback: Callable[[SubagentInfo], Awaitable[bool]] | None
    ) -> None:
        self.external_canceller = callback

    def register_external(self, info: SubagentInfo) -> None:
        self.external_agents.append(info)

    async def _fire_event(
        self, event: str, info: SubagentInfo, extra: dict[str, object]
    ) -> None:
        self.events.append((event, info.id, extra))

    async def report_external(self, info: SubagentInfo) -> bool:
        self.reported.append(info)
        return True


@pytest.mark.asyncio
async def test_remote_spawn_picks_least_loaded_peer_and_relays_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._POLL_SECONDS", 0.0)
    instances = _Instances(
        [
            (200, {"id": "peer01"}),
            (200, {"done": False, "turns": 2, "last_tool": "Reading src"}),
            (
                200,
                {
                    "done": True,
                    "result": "remote-result",
                    "error": "",
                    "outcome": "completed",
                    "partial": False,
                },
            ),
        ]
    )
    manager = _Manager()
    manager.external_agents.append(
        SubagentInfo(
            id="occupied",
            task="existing",
            executor="remote",
            instance_id="crew-a",
            remote_id="peer00",
        )
    )
    state = SimpleNamespace(instances_manager=instances)
    service = RemoteSubagentService(state, manager)  # type: ignore[arg-type]

    info = await service.spawn(
        task="review exact refactor SHA",
        parent_session="dashboard:chat-1",
        agent="kirocrew",
        max_turns=50,
        cwd="/home/kirocrew/workplace/repo",
        model="",
        reasoning_effort="",
        include_memory=False,
        include_lessons=False,
        include_project=True,
        memory_mode="temporary",
        batch_id="batch01",
        batch_total=1,
    )
    monitor = service._monitors[info.id]
    await monitor

    assert info.instance_id == "crew-b"
    assert info.remote_id == "peer01"
    assert info.done is True
    assert info.result == "rem"
    assert info.result_truncated is True
    assert manager.reported == [info]
    assert manager.events[0][0] == "subagent_spawn"
    post = instances.calls[0]
    assert post[:3] == ("crew-b", "POST", "api/spawn")
    assert post[3] == {
        "task": "review exact refactor SHA",
        "parent_session": "",
        "silent": True,
        "include_memory": False,
        "include_lessons": False,
        "include_project": True,
        "memory_mode": "temporary",
        "agent": "kirocrew",
        "max_turns": 50,
        "cwd": "/home/kirocrew/workplace/repo",
    }
    await service.close()


@pytest.mark.asyncio
async def test_remote_spawn_refuses_disconnected_explicit_peer() -> None:
    manager = _Manager()
    service = RemoteSubagentService(
        cast(
            Any,
            SimpleNamespace(instances_manager=_Instances([], connected=("crew-a",))),
        ),
        cast(Any, manager),
    )

    with pytest.raises(RemoteSubagentError) as raised:
        await service.spawn(
            task="x",
            parent_session="dashboard:chat-1",
            agent="",
            max_turns=0,
            cwd="",
            model="",
            reasoning_effort="",
            include_memory=False,
            include_lessons=False,
            include_project=False,
            memory_mode="temporary",
            batch_id="",
            batch_total=0,
            instance_id="crew-z",
        )

    assert raised.value.code == "remote_instance_not_connected"
    assert manager.external_agents == []


@pytest.mark.asyncio
async def test_remote_cancel_targets_peer_run() -> None:
    instances = _Instances([(200, {"ok": True, "cancelled": True})])
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), _Manager()  # type: ignore[arg-type]
    )
    info = SubagentInfo(
        id="local01",
        task="x",
        executor="remote",
        instance_id="crew-a",
        remote_id="peer01",
    )

    assert await service.cancel(info) is True
    assert instances.calls == [("crew-a", "DELETE", "api/spawn/peer01", None)]


def test_manager_external_inventory_does_not_consume_local_running_capacity() -> None:
    manager = SubagentManager.__new__(SubagentManager)
    manager._agents = {
        "local01": SubagentInfo(id="local01", task="local"),
    }
    manager._external_agents = {}
    manager._batch_submitted = {}
    manager._batch_progress_ts = {}
    remote = SubagentInfo(
        id="remote01",
        task="remote",
        parent_session_key="dashboard:chat-1",
        executor="remote",
        instance_id="crew-a",
        remote_id="peer01",
        batch_id="batch01",
        batch_total=1,
    )

    manager.register_external(remote)

    assert manager.get("remote01") is remote
    assert {info.id for info in manager.all_agents} == {"local01", "remote01"}
    assert [info.id for info in manager.running] == ["local01"]
    assert manager._batch_submitted["batch01"] == [1, 1]


def test_spawn_schema_accepts_remote_pool_selection() -> None:
    cleaned = validate_tool_args(
        {"task": "review", "executor": "remote", "instance_id": "crew-a"},
        SPAWN_RUN_SCHEMA,
    )
    assert cleaned["executor"] == "remote"
    assert cleaned["instance_id"] == "crew-a"


def test_spawn_tool_forwards_remote_placement_and_reports_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(spawn_tools.mcp_core, "_resolve_session_key", lambda: "dashboard:chat-1")

    def post(path: str, body: dict[str, object]) -> dict[str, object]:
        assert path == "/api/spawn"
        calls.append(body)
        return {
            "id": f"run{len(calls)}",
            "executor": "remote",
            "instance_id": "crew-a",
        }

    monkeypatch.setattr(spawn_tools.mcp_core, "_post", post)
    result = spawn_tools.spawn_run(
        "spawn_run",
        {
            "tasks": ["review a", "review b"],
            "executor": "remote",
            "instance_id": "crew-a",
            "include_memory": False,
            "include_lessons": False,
        },
    )

    assert len(calls) == 2
    assert all(call["executor"] == "remote" for call in calls)
    assert all(call["instance_id"] == "crew-a" for call in calls)
    assert "[remote:crew-a]" in result


def _tar_payload(entries: list[tuple[str, bytes, int, str]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data, mode, kind in entries:
            member = tarfile.TarInfo(name)
            member.mode = mode
            if kind == "file":
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
            else:
                member.type = tarfile.SYMTYPE
                member.linkname = data.decode("utf-8")
                archive.addfile(member)
    return buffer.getvalue()


def test_workspace_install_is_content_addressed_and_rejects_unsafe_members(
    tmp_path,
) -> None:
    payload = _tar_payload(
        [
            ("README.md", b"hello\n", 0o644, "file"),
            ("scripts/run.sh", b"#!/bin/sh\n", 0o755, "file"),
        ]
    )
    digest = hashlib.sha256(payload).hexdigest()

    path, reused = install_workspace(payload, digest, "a" * 40, root=tmp_path)

    assert reused is False
    assert (path / "README.md").read_text(encoding="utf-8") == "hello\n"
    assert (path / "scripts/run.sh").stat().st_mode & 0o111
    assert install_workspace(payload, digest, "a" * 40, root=tmp_path) == (path, True)

    for bad in (
        _tar_payload([("../escape", b"x", 0o644, "file")]),
        _tar_payload([("link", b"/etc/passwd", 0o777, "symlink")]),
    ):
        with pytest.raises(WorkspaceArchiveRejected):
            install_workspace(bad, hashlib.sha256(bad).hexdigest(), "b" * 40, root=tmp_path)


@pytest.mark.asyncio
async def test_remote_spawn_syncs_parent_project_when_no_remote_cwd(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._POLL_SECONDS", 0.0)
    payload = b"tracked-source"
    digest = hashlib.sha256(payload).hexdigest()
    instances = _Instances(
        [
            (
                200,
                {
                    "path": "/home/kirocrew/workplace/kirocrew-remote-workspaces/abc",
                    "sha256": digest,
                    "commit": "a" * 40,
                },
            ),
            (200, {"id": "peer01"}),
            (200, {"done": True, "result": "ok", "error": ""}),
        ],
        connected=("crew-a",),
    )
    manager = _Manager()
    manager.external_agents.clear()
    state = SimpleNamespace(
        instances_manager=instances,
        get_slot=lambda _name: SimpleNamespace(project=str(tmp_path)),
    )
    service = RemoteSubagentService(state, manager)  # type: ignore[arg-type]
    monkeypatch.setattr(service, "_build_project_archive", lambda _path: (payload, digest, "a" * 40))

    info = await service.spawn(
        task="review",
        parent_session="dashboard:chat-1",
        agent="",
        max_turns=0,
        cwd="",
        model="",
        reasoning_effort="",
        include_memory=False,
        include_lessons=False,
        include_project=True,
        memory_mode="temporary",
        batch_id="",
        batch_total=0,
    )
    monitor = service._monitors[info.id]
    await monitor

    assert instances.calls[0][:3] == ("crew-a", "POST", "api/remote-workspaces")
    spawn_body = instances.calls[1][3]
    assert isinstance(spawn_body, dict)
    assert spawn_body["cwd"] == (
        "/home/kirocrew/workplace/kirocrew-remote-workspaces/abc"
    )
    assert info.cwd == "/home/kirocrew/workplace/kirocrew-remote-workspaces/abc"
    await service.close()


@pytest.mark.asyncio
async def test_restart_restores_terminal_mapping_and_redelivers_once(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    run_dir = tmp_path / "remote-subagents" / "abcdef12"
    run_dir.mkdir(parents=True)
    result_path = run_dir / "result.txt"
    result_path.write_text("restored-result", encoding="utf-8")
    original = SubagentInfo(
        id="abcdef12",
        task="review",
        parent_session_key="dashboard:chat-1",
        agent="kirocrew",
        done=True,
        result="restored-result",
        result_path=str(result_path),
        executor="remote",
        instance_id="crew-a",
        remote_id="peer01",
    )
    RemoteSubagentService._persist_info(original, delivered=False)

    manager = _Manager()
    service = RemoteSubagentService(SimpleNamespace(instances_manager=None), manager)  # type: ignore[arg-type]
    await service.ensure_restored()

    restored = manager.get("abcdef12")
    assert restored is not None
    assert restored.executor == "remote"
    assert restored.result == "res"
    assert restored.result_truncated is True
    assert manager.reported == [restored]
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["delivered"] is True
    await service.close()


@pytest.mark.asyncio
async def test_remote_monitor_recovers_from_disconnect_and_terminal_404(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._POLL_SECONDS", 0.0)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._RETRY_SECONDS", 0.0)

    reconnecting = _Instances(
        [
            (200, {"id": "peer01"}),
            OSError("tunnel dropped"),
            (200, {"done": True, "turns": {}, "result": "reconnected", "error": ""}),
        ],
        connected=("crew-a",),
    )
    manager = _Manager()
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=reconnecting), manager  # type: ignore[arg-type]
    )
    info = await service.spawn(
        task="review",
        parent_session="dashboard:chat-1",
        agent="",
        max_turns=0,
        cwd="/remote/project",
        model="",
        reasoning_effort="",
        include_memory=False,
        include_lessons=False,
        include_project=True,
        memory_mode="temporary",
        batch_id="",
        batch_total=0,
    )
    await service._monitors[info.id]
    assert info.done is True
    assert info.error == ""
    assert manager.reported == [info]
    await service.close()

    missing = _Instances(
        [(200, {"id": "peer02"}), (404, {}), (404, {}), (404, {})],
        connected=("crew-a",),
    )
    missing_manager = _Manager()
    missing_service = RemoteSubagentService(
        SimpleNamespace(instances_manager=missing), missing_manager  # type: ignore[arg-type]
    )
    missing_info = await missing_service.spawn(
        task="review",
        parent_session="dashboard:chat-1",
        agent="",
        max_turns=0,
        cwd="/remote/project",
        model="",
        reasoning_effort="",
        include_memory=False,
        include_lessons=False,
        include_project=True,
        memory_mode="temporary",
        batch_id="",
        batch_total=0,
    )
    await missing_service._monitors[missing_info.id]
    assert missing_info.done is True
    assert "no longer available" in missing_info.error
    assert missing_manager.reported == [missing_info]
    await missing_service.close()


@pytest.mark.asyncio
async def test_corrupt_persistence_is_ignored_fail_closed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    state_path = tmp_path / "remote-subagents" / "abcdef12" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "id": "abcdef12",
                "executor": "remote",
                "instance_id": "crew-a",
                "remote_id": "../peer",
            }
        ),
        encoding="utf-8",
    )
    malformed_path = tmp_path / "remote-subagents" / "abcdef13" / "state.json"
    malformed_path.parent.mkdir(parents=True)
    malformed_path.write_text(
        json.dumps(
            {
                "version": 1,
                "id": "abcdef13",
                "executor": "remote",
                "instance_id": "crew-a",
                "remote_id": "peer02",
                "max_turns": {"not": "an integer"},
            }
        ),
        encoding="utf-8",
    )
    manager = _Manager()
    service = RemoteSubagentService(SimpleNamespace(instances_manager=None), manager)  # type: ignore[arg-type]

    await service.ensure_restored()

    assert manager.external_agents == []
    assert manager.reported == []
    assert service._monitors == {}
    await service.close()


@pytest.mark.asyncio
async def test_remote_terminal_enters_normal_batch_digest_reporter() -> None:
    manager = SubagentManager.__new__(SubagentManager)
    info = SubagentInfo(
        id="remote01",
        task="remote",
        done=True,
        executor="remote",
        instance_id="crew-a",
        remote_id="peer01",
        batch_id="batch01",
        batch_total=2,
    )
    manager._external_agents = {info.id: info}
    manager._claim_finalize = lambda candidate: candidate is info  # type: ignore[method-assign]
    reporter = AsyncMock(return_value=True)
    manager._run_terminal_report = reporter  # type: ignore[method-assign]

    assert await manager.report_external(info) is True
    reporter.assert_awaited_once_with(
        info,
        source="Remote subagent",
        injection_timeout_reason="remote completion delivery timed out",
        mark_delivered_on_success=False,
        settle_digest=True,
    )


def _manager_with_remote(info: SubagentInfo) -> tuple[SubagentManager, AsyncMock]:
    manager = cast(Any, SubagentManager.__new__(SubagentManager))
    manager._agents = {}
    manager._external_agents = {info.id: info}
    manager._queue = []
    manager._teardown_cancelled_ids = set()
    manager._followup_watchers = {}
    manager._followup_watcher_parents = {}
    manager._followup_watcher_infos = {}
    manager._report_owners = {}
    manager._batch_submitted = {}
    manager._batch_progress_ts = {}
    manager._admission = SimpleNamespace(
        taskq_pending_ids_for_async=AsyncMock(return_value=[]),
    )
    canceller = AsyncMock(return_value=True)
    manager.set_external_canceller(canceller)
    return cast(SubagentManager, manager), canceller


@pytest.mark.asyncio
async def test_stop_all_and_parent_teardown_cancel_remote_children() -> None:
    stop_info = SubagentInfo(
        id="remote01",
        task="remote",
        parent_session_key="dashboard:chat-1",
        executor="remote",
        instance_id="crew-a",
        remote_id="peer01",
    )
    stop_manager, stop_canceller = _manager_with_remote(stop_info)

    assert await stop_manager.cancel_for_parent("dashboard:chat-1") == (1, 0)
    stop_canceller.assert_awaited_once_with(stop_info)
    assert stop_info.user_stopped is True

    teardown_info = SubagentInfo(
        id="remote02",
        task="remote",
        parent_session_key="dashboard:chat-2",
        executor="remote",
        instance_id="crew-b",
        remote_id="peer02",
    )
    teardown_manager, teardown_canceller = _manager_with_remote(teardown_info)

    selected = teardown_manager.snapshot_teardown_children("dashboard:chat-2")
    assert selected == ("remote02",)
    assert "remote02" in teardown_manager._teardown_cancelled_ids
    assert (
        await teardown_manager.cancel_for_teardown(
            selected,
            parent_session_key="dashboard:chat-2",
            verb="remove",
        )
        == 1
    )
    teardown_canceller.assert_awaited_once_with(teardown_info)


class _WorkspaceRequest:
    def __init__(self, *, user: str, app: str = "", internal_auth: bool = False) -> None:
        self.app = {"state": SimpleNamespace(owner_id="owner")}
        self.query = {"sha256": "a" * 64, "commit": "b" * 40}
        self.content = _Content(b"archive")
        self._values = {"user": user, "app": app, "internal_auth": internal_auth}

    def __contains__(self, key: str) -> bool:
        return key in self._values

    def __getitem__(self, key: str) -> object:
        return self._values[key]

    def get(self, key: str, default: object = None) -> object:
        return self._values.get(key, default)


@pytest.mark.asyncio
async def test_workspace_route_accepts_only_owner_cookie_requests(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for request in (
        _WorkspaceRequest(user="owner", internal_auth=True),
        _WorkspaceRequest(user="owner", app="notes"),
        _WorkspaceRequest(user="other"),
    ):
        response = await api_remote_workspace_upload(cast(Any, request))
        assert response.status == 403

    installed = tmp_path / "workspace"
    monkeypatch.setattr(
        "kiro_crew.dashboard.remote_workspaces.install_workspace",
        lambda _payload, _digest, _commit: (installed, False),
    )
    response = await api_remote_workspace_upload(
        cast(Any, _WorkspaceRequest(user="owner"))
    )
    assert response.status == 200
    assert isinstance(response.body, (bytes, bytearray))
    payload = json.loads(response.body)
    assert payload["path"] == str(installed)
    assert payload["reused"] is False


@pytest.mark.asyncio
async def test_delivered_remote_records_expire_at_configured_ttl(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    info = SubagentInfo(
        id="abcdef12",
        task="done",
        done=True,
        executor="remote",
        instance_id="crew-a",
        remote_id="peer01",
    )
    RemoteSubagentService._persist_info(info, delivered=True)
    manager = _Manager()
    manager._result_ttl_secs = 0
    service = RemoteSubagentService(SimpleNamespace(instances_manager=None), manager)  # type: ignore[arg-type]

    await service.ensure_restored()

    assert manager.get("abcdef12") is None
    assert not (tmp_path / "remote-subagents" / "abcdef12").exists()
    await service.close()


def test_workspace_install_prunes_only_expired_marked_snapshots(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_workspaces._WORKSPACE_TTL_SECS", 1)
    first = _tar_payload([("one.txt", b"one", 0o644, "file")])
    first_path, _ = install_workspace(
        first,
        hashlib.sha256(first).hexdigest(),
        "a" * 40,
        root=tmp_path,
    )
    old = time.time() - 10
    os.utime(first_path, (old, old))

    unmarked = tmp_path / ("f" * 24)
    unmarked.mkdir()
    os.utime(unmarked, (old, old))

    second = _tar_payload([("two.txt", b"two", 0o644, "file")])
    second_path, _ = install_workspace(
        second,
        hashlib.sha256(second).hexdigest(),
        "b" * 40,
        root=tmp_path,
    )

    assert second_path.is_dir()
    assert not first_path.exists()
    assert unmarked.is_dir()


@pytest.mark.asyncio
async def test_restart_resumes_polling_an_unfinished_remote_mapping(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._POLL_SECONDS", 0.0)
    original = SubagentInfo(
        id="abcdef12",
        task="review",
        parent_session_key="dashboard:chat-1",
        executor="remote",
        instance_id="crew-a",
        remote_id="peer01",
    )
    RemoteSubagentService._persist_info(original, delivered=False)
    instances = _Instances(
        [(200, {"done": True, "result": "after-restart", "error": ""})],
        connected=("crew-a",),
    )
    manager = _Manager()
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )

    await service.ensure_restored()
    await service._monitors["abcdef12"]

    restored = manager.get("abcdef12")
    assert restored is not None and restored.done is True
    assert manager.reported == [restored]
    assert instances.calls == [("crew-a", "GET", "api/spawn/peer01", None)]
    await service.close()
