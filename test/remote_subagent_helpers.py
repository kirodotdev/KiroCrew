"""Shared fakes for the remote sub-agent executor tests."""

from __future__ import annotations

import io
import json
import os
import tarfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, cast
from unittest.mock import AsyncMock

import pytest
from git_maintenance_helpers import git_maintenance_off_entries

import kiro_crew
from kiro_crew.dashboard import remote_workspaces
from kiro_crew.dashboard.remote_subagents import RemoteSubagentError, RemoteSubagentService
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_manager.published_depth import PublishedQueueDepths
from kiro_crew.testing.wait import async_wait_until, await_until

# Installs go through dir_fd-relative creates; a platform without them refuses.
_PINNED_INSTALL = pytest.mark.skipif(
    not remote_workspaces._can_extract_pinned(),
    reason="workspace install needs descriptor-pinned extraction (POSIX)",
)


async def _settle_monitor(service: RemoteSubagentService, run_id: str) -> None:
    """Wait for one run's monitor, bounded; stop the service if it never settles."""
    try:
        await await_until(service._monitors[run_id], what=f"remote monitor for {run_id}")
    except AssertionError as exc:
        await service.close()
        pytest.fail(f"remote monitor for {run_id} did not settle: {exc}")


class _Content:
    """aiohttp StreamReader double that delivers the body in small chunks.

    Like the real reader, ``read(n)`` resolves with whatever is buffered -- here
    only the first chunk -- so code that does a single read sees a truncated
    body. Readers must drain ``iter_chunked`` to EOF.
    """

    def __init__(self, body: bytes, chunk: int = 7) -> None:
        self._body = body
        self._chunk = chunk

    async def read(self, limit: int) -> bytes:
        return self._body[: min(limit, self._chunk)]

    async def iter_chunked(self, size: int):
        step = max(1, min(size, self._chunk))
        for start in range(0, len(self._body), step):
            yield self._body[start : start + step]


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
        advertises: bool = True,
    ) -> None:
        self._responses = list(responses)
        self._connected = connected
        # A current peer lists the fields it enforces in /api/version; a peer
        # without that change (same major.minor) silently drops them.
        self._advertises = advertises
        self.calls: list[tuple[str, str, str, dict[str, object] | bytes | None]] = []

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

    async def peer_capability(self, _instance_id: str, path: str) -> tuple[bool, object]:
        assert path == "/api/version"
        body: dict[str, object] = {"version": kiro_crew.__version__}
        if self._advertises:
            body["spawn_enforces"] = ["approval_floor", "approval_relay", "memory_mode", "run_id"]
        return True, body

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


class _Sessions:
    """The two session-store reads the hub's approval floor makes."""

    def __init__(self, policy: str = "", present: bool | None = True) -> None:
        self.policy = policy
        self.present = present

    def get_approval_policy(self, _key: str) -> str:
        return self.policy

    def has_session(self, _key: str) -> bool | None:
        return self.present


class _Manager:
    def __init__(self) -> None:
        self.external_agents: list[SubagentInfo] = []
        self.events: list[tuple[str, str, dict[str, object]]] = []
        self.reported: list[SubagentInfo] = []
        self._sessions: Any = _Sessions()
        self._is_yolo: Callable[[], bool] | None = None
        self._global_approval_mode = ""
        self._completion_keep = "head"
        self._completion_keep_chars = 3
        self._result_ttl_secs = 3600
        self.external_placement: Any = None
        # A person who approves every spawn: a Normal-mode parent's remote spawn
        # waits for this before anything leaves the hub.
        self.spawn_approvals: list[tuple[str, str, str]] = []
        self.approve_spawns = True

    async def _on_spawn_approval(self, request_id: str, label: str, parent: str) -> bool:
        self.spawn_approvals.append((request_id, label, parent))
        return self.approve_spawns

    @property
    def _external_agents(self) -> dict[str, SubagentInfo]:
        return {info.id: info for info in self.external_agents}

    def bind_external_placement(self, placement: Any) -> None:
        self.external_placement = placement

    def get(self, agent_id: str) -> SubagentInfo | None:
        return next((info for info in self.external_agents if info.id == agent_id), None)

    def forget_external(self, agent_id: str) -> SubagentInfo | None:
        info = self.get(agent_id)
        if info is not None:
            self.external_agents.remove(info)
        return info

    def register_external(self, info: SubagentInfo) -> None:
        self.external_agents.append(info)

    async def _fire_event(self, event: str, info: SubagentInfo, extra: dict[str, object]) -> None:
        self.events.append((event, info.id, extra))

    async def report_external(self, info: SubagentInfo) -> bool:
        self.reported.append(info)
        return True


def _auto_manager() -> "_Manager":
    """A manager whose parent is on auto: the spawn needs no person and is placed
    inline, so a placement failure is the spawn's own refusal."""
    manager = _Manager()
    manager._sessions = _Sessions(policy="auto")
    return manager


async def _spawn_placed(service: Any, **kwargs: Any) -> SubagentInfo:
    """``service.spawn`` and, for a spawn that waited for approval, its placement.

    A placement failure there ends the run instead of raising; it is raised
    here with the same code, so one assertion covers both paths.
    """
    info = await service.spawn(**kwargs)
    runner = service._approvals.get(info.id)
    if runner is not None:
        await await_until(runner, what=f"placement of {info.id}")
    if info.done and info.error_code:
        raise RemoteSubagentError(info.error, code=info.error_code)
    return info


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


def _pax_payload(comment_bytes: int) -> bytes:
    """A one-byte file behind a PAX comment of *comment_bytes*: the file size the
    member check counts is 1, while the header tarfile retains is the comment."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        member = tarfile.TarInfo("a.txt")
        member.size = 1
        member.pax_headers = {"comment": "x" * comment_bytes}
        archive.addfile(member, io.BytesIO(b"a"))
    return buffer.getvalue()


def _ownership_request(manager: object) -> Any:
    return SimpleNamespace(app={"state": SimpleNamespace(subagents=manager)})


def _manager_with_remote(info: SubagentInfo) -> tuple[SubagentManager, AsyncMock]:
    manager = cast(Any, SubagentManager.__new__(SubagentManager))
    manager._agents = {}
    manager._external_agents = {info.id: info}
    manager._queue = []
    manager._undurable_in_dispatch = {}
    manager._shutting_down = False
    manager._teardown_store_fences = {}
    manager._teardown_store_sweeps = []
    manager._teardown_sweeps_owed = []
    manager._teardown_fence_lock = threading.Lock()
    manager._teardown_cancelled_ids = set()
    manager._followup_watchers = {}
    manager._followup_watcher_parents = {}
    manager._followup_watcher_infos = {}
    manager._report_owners = {}
    manager._batch_submitted = {}
    manager._batch_progress_ts = {}
    manager._published_depths = PublishedQueueDepths()
    manager._admission = SimpleNamespace(
        taskq_pending_ids_for_async=AsyncMock(return_value=[]),
        starting_claim_ids_for=lambda _parent: [],
        taskq_store=lambda: None,
        _drain_queue_impl=lambda: None,
    )
    canceller = AsyncMock(return_value=True)
    manager.bind_external_placement(
        SimpleNamespace(
            cancel=canceller,
            dismiss=AsyncMock(return_value=True),
            acknowledge_delivered=AsyncMock(),
        )
    )
    return cast(SubagentManager, manager), canceller


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


def _git_project(root) -> str:
    import subprocess

    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    env.update(git_maintenance_off_entries(env))
    run = lambda *argv: subprocess.run(  # noqa: E731
        ["git", "-C", str(root), *argv], check=True, capture_output=True, env=env
    )
    run("init", "-q")
    (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
    run("add", "app.py")
    run(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        "init",
    )
    return run("rev-parse", "HEAD").stdout.decode().strip()


def _commit_all(root, *paths: str) -> None:
    import subprocess

    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    env.update(git_maintenance_off_entries(env))
    run = lambda *argv: subprocess.run(  # noqa: E731
        ["git", "-C", str(root), *argv], check=True, capture_output=True, env=env
    )
    run("add", "-f", *paths)
    run(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        "more",
    )


def _names(payload: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        return set(archive.getnames())


def gzip_decompress(payload: bytes) -> bytes:
    import gzip

    return gzip.decompress(payload)


def _spawn_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "task": "review the secret plan",
        "parent_session": "dashboard:chat-1",
        "agent": "",
        "max_turns": 0,
        "cwd": "/remote/project",
        "model": "",
        "reasoning_effort": "",
        "include_memory": False,
        "include_lessons": False,
        "include_project": True,
        "memory_mode": "persistent",
        "batch_id": "",
        "batch_total": 0,
    }
    kwargs.update(overrides)
    return kwargs


def _raising_sessions() -> Any:
    class _Broken:
        def get_approval_policy(self, _key: str) -> str:
            raise RuntimeError("session store unreadable")

        def has_session(self, _key: str) -> bool | None:
            raise RuntimeError("session store unreadable")

    return _Broken()


class _AnswerRequest(_WorkspaceRequest):
    def __init__(self, body: object, *, approval_id: str = "a" * 16, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.match_info = {"agent_id": "run-q1", "approval_id": approval_id}
        self._body = body

    async def json(self) -> object:
        return self._body


def _floored_info(**overrides: object) -> SubagentInfo:
    values: dict[str, object] = {
        "id": "local-r1",
        "task": "t",
        "parent_session_key": "dashboard:chat-1",
        "executor": "remote",
        "instance_id": "crew-a",
        "remote_id": "peer01",
        "approval_floor": "interactive",
    }
    values.update(overrides)
    return SubagentInfo(**values)  # type: ignore[arg-type]


def _no_snapshot(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("the project must not be packaged before the spawn is approved")


def _failing_terminal_write(service: RemoteSubagentService, failures: int) -> list[bool]:
    """Make the first *failures* terminal (``done=True``) writes fail; log each one."""
    real_persist = service._persist_info
    attempts: list[bool] = []

    def persist(info: SubagentInfo, **kw: Any) -> object:
        if kw.get("done") is True:
            attempts.append(len(attempts) >= failures)
            if not attempts[-1]:
                raise OSError("disk full")
        return real_persist(info, **kw)

    service._persist_info = persist  # type: ignore[method-assign]
    return attempts


async def _approved_spawn_held_at(
    service: RemoteSubagentService, monkeypatch: pytest.MonkeyPatch, held: dict[str, object]
) -> tuple[SubagentInfo, threading.Event]:
    """Spawn an approved remote run and hold its placement at the first
    ``_persist_info`` call whose keywords equal *held*; return it and the release."""
    reached, release = threading.Event(), threading.Event()
    real_persist = service._persist_info

    def persist(info: SubagentInfo, **kw: Any) -> object:
        # Only the first matching write is held: the stop's own commit may match.
        if kw == held and not reached.is_set():
            reached.set()
            assert release.wait(10), "the test never released the placement"
        return real_persist(info, **kw)

    service._persist_info = persist  # type: ignore[method-assign]
    info = await service.spawn(**_spawn_kwargs())
    await async_wait_until(reached.is_set, describe=lambda: "placement never reached the hold")
    return info, release


def _expired_snapshot(base: Path, nonce: str) -> Path:
    from kiro_crew.dashboard import remote_workspaces

    digest = "a" * 64
    tree = base / f"{digest[:24]}-{nonce}"
    tree.mkdir(parents=True)
    (tree / remote_workspaces._MARKER).write_text(json.dumps({"sha256": digest}))
    old = time.time() - remote_workspaces._WORKSPACE_TTL_SECS - 60
    os.utime(tree, (old, old))
    return tree


def _remote_record(**overrides: Any) -> SubagentInfo:
    fields: dict[str, Any] = {
        "id": "abcdef01",
        "task": "t",
        "parent_session_key": "dashboard:chat-1",
        "executor": "remote",
        "instance_id": "crew-a",
        "remote_id": "0123456789abcdef",
    }
    fields.update(overrides)
    return SubagentInfo(**fields)
