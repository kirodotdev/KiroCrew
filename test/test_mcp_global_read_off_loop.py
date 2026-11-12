"""The MCP routes read ``~/.kiro/settings/mcp.json`` off the loop.

``_run_mcp_probe``, ``GET /api/mcp``, ``GET /api/mcp/active`` and
``POST /api/mcp/probe`` each read the Kiro-global ``mcp.json`` for its
disabled / ``disabledTools`` state. Read on the event loop, a slow home
directory (a network mount) stalled every session for the length of the read.
These tests spy on the retrying read to record the thread it ran on, and check
that a read still contended after its retries answers a coded 503; no real
server is probed or spawned. The toggle, remove and DELETE routes answer the
same 503 before they write, and again when their agent-config sync meets a held
``kirocrew.json``, which the sync now lets raise.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest
from handlers_mcp_coverage_helpers import _request as _owner_request
from handlers_mcp_coverage_helpers import sandbox_fixture  # noqa: F401
from handlers_mcp_coverage_helpers import _write_global

import kiro_crew.mcp_discovery as disc
from kiro_crew import atomic_write
from kiro_crew.dashboard.handlers import mcp as mcp_mod


class _ThreadRecordingFile:
    """Notes which thread read ``_GLOBAL_MCP_JSON`` through the retrying read."""

    def __init__(self, real: Path) -> None:
        self._real = real
        self.reader_threads: list[int] = []

    def spy(self, real_read: Callable[..., bytes]) -> Callable[..., bytes]:
        def _read(path: Any, **kwargs: Any) -> bytes:
            if Path(path) == self._real:
                self.reader_threads.append(threading.get_ident())
            return real_read(path, **kwargs)

        return _read


class _State:
    def __init__(self) -> None:
        self._background_tasks: set = set()
        self.sessions = SimpleNamespace(final_drain_started=False)


def _request(state: _State) -> Any:
    """The owner-claim request stand-in ``test_mcp_probe_treadmill`` uses, plus a query."""

    class _App(dict):
        pass

    class _Req(dict):
        def __init__(self, app: _App) -> None:
            super().__init__(user="local-app", app="")
            self.app = app
            self.query: dict[str, str] = {}
            self._claims = {"user": "local-app", "app": ""}

        def get(self, key: str, default: Any = None) -> Any:
            return self._claims.get(key, default)

        def __contains__(self, key: object) -> bool:
            return key in self._claims

        def __getitem__(self, key: str) -> Any:
            return self._claims[key]

    app = _App()
    app["state"] = state
    return _Req(app)


@pytest.fixture
def global_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _ThreadRecordingFile:
    real = tmp_path / "mcp.json"
    real.write_text(json.dumps({"mcpServers": {"x": {"command": "true"}}}), encoding="utf-8")
    recorder = _ThreadRecordingFile(real)
    monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", real)
    monkeypatch.setattr(
        atomic_write, "read_bytes_with_retry", recorder.spy(atomic_write.read_bytes_with_retry)
    )
    monkeypatch.setattr(mcp_mod, "_kirocrew_mcp_json", lambda: tmp_path / "absent-kc.json")
    monkeypatch.setattr(disc, "list_servers", lambda *a, **k: [])
    monkeypatch.setattr(disc, "probe_all", AsyncMock(return_value=[]))
    monkeypatch.setattr(mcp_mod, "_evaluate_shareability", AsyncMock(return_value=None))
    monkeypatch.setattr(mcp_mod, "_bg_mcp_probe", AsyncMock(return_value=None))
    monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", [])
    monkeypatch.setattr(mcp_mod, "_mcp_probe_ts", time.time())
    monkeypatch.setattr(mcp_mod, "_mcp_probe_in_progress", False)
    return recorder


def _run_probe() -> Awaitable[Any]:
    return mcp_mod._run_mcp_probe(None)


def _list_servers() -> Awaitable[Any]:
    return mcp_mod.api_mcp_servers(_request(_State()))


def _list_active() -> Awaitable[Any]:
    return mcp_mod.api_mcp_active(_request(_State()))


def _probe_route() -> Awaitable[Any]:
    return mcp_mod.api_mcp_probe(_request(_State()))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [_run_probe, _list_servers, _list_active, _probe_route],
    ids=["_run_mcp_probe", "api_mcp_servers", "api_mcp_active", "api_mcp_probe"],
)
async def test_global_mcp_json_is_read_off_the_loop_thread(
    global_file: _ThreadRecordingFile, call: Callable[[], Awaitable[Any]]
) -> None:
    loop_thread = threading.get_ident()

    resp = await call()

    if resp is not None:
        assert resp.status == 200
    assert global_file.reader_threads, "the global mcp.json was not read at all"
    assert loop_thread not in global_file.reader_threads, "mcp.json was read on the event loop"


def _always_contended(path: Any, **kwargs: Any) -> bytes:
    raise PermissionError(13, "Permission denied", str(path))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [_list_servers, _list_active, _probe_route],
    ids=["api_mcp_servers", "api_mcp_active", "api_mcp_probe"],
)
async def test_a_read_still_contended_after_retries_answers_a_coded_503(
    global_file: _ThreadRecordingFile,
    monkeypatch: pytest.MonkeyPatch,
    call: Callable[[], Awaitable[Any]],
) -> None:
    # A writer's os.replace on Windows makes the open fail with PermissionError;
    # once the retry budget is spent the route answers a retryable 503, never a
    # bare 500 and never an empty config that shows every server as enabled.
    monkeypatch.setattr(atomic_write, "read_bytes_with_retry", _always_contended)

    resp = await call()

    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "mcp_config_busy"


@pytest.mark.asyncio
async def test_a_missing_global_file_still_reads_as_no_servers(
    global_file: _ThreadRecordingFile, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", tmp_path / "absent-mcp.json")

    resp = await _list_servers()

    assert resp.status == 200


def _held(target: Path, reader_threads: list[int]) -> Callable[..., bytes]:
    """The retrying read, with *target* held by another process past the retry.

    Each read of *target* notes its thread in *reader_threads*.
    """
    real_read = atomic_write.read_bytes_with_retry

    def _read(path: Any, **kwargs: Any) -> bytes:
        if Path(path) == target:
            reader_threads.append(threading.get_ident())
            raise PermissionError(13, "Permission denied", str(path))
        return real_read(path, **kwargs)

    return _read


_MUTATIONS: dict[str, Callable[[], Awaitable[Any]]] = {
    "api_mcp_toggle": lambda: mcp_mod.api_mcp_toggle(
        _owner_request({"name": "srv", "enabled": False})
    ),
    "api_mcp_toggle_tool": lambda: mcp_mod.api_mcp_toggle_tool(
        _owner_request({"server": "srv", "tool": "t", "enabled": False})
    ),
    "api_mcp_toggle_all": lambda: mcp_mod.api_mcp_toggle_all(_owner_request({"enabled": False})),
    "api_mcp_remove": lambda: mcp_mod.api_mcp_remove(_owner_request({"name": "srv"})),
    "api_mcp_server_detail_delete": lambda: mcp_mod.api_mcp_server_detail(
        _owner_request(None, match_info={"name": "srv"}, method="DELETE")
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("route", sorted(_MUTATIONS))
async def test_a_mutation_route_answers_a_held_mcp_json_with_a_coded_503_and_writes_nothing(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    from kiro_crew.dashboard.handlers import _shared

    _write_global(sandbox, {"srv": {"command": "x"}})
    before = sandbox.global_json.read_bytes()
    reader_threads: list[int] = []
    monkeypatch.setattr(
        atomic_write, "read_bytes_with_retry", _held(sandbox.global_json, reader_threads)
    )
    no_manager = MagicMock()
    no_manager.available.return_value = False
    monkeypatch.setattr(_shared, "_capability_manager", lambda: no_manager)

    resp = await _MUTATIONS[route]()

    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "mcp_config_busy"
    # Off the loop, where the retry runs: on the loop it is a single attempt.
    assert reader_threads, "mcp.json was not read through the retrying read"
    assert threading.get_ident() not in reader_threads, "mcp.json was read on the event loop"
    assert sandbox.global_json.read_bytes() == before
    assert sandbox.synced == [] and sandbox.batched == []


@pytest.mark.asyncio
@pytest.mark.parametrize("route", sorted(set(_MUTATIONS) - {"api_mcp_toggle_tool"}))
async def test_a_route_whose_agent_sync_meets_a_held_agent_config_answers_a_coded_503(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    from kiro_crew.dashboard.handlers import _shared

    def _held_sync(*args: Any, **kwargs: Any) -> None:
        raise PermissionError(13, "Permission denied", "kirocrew.json")

    _write_global(sandbox, {"srv": {"command": "x"}})
    monkeypatch.setattr(mcp_mod, "_sync_mcp_to_agent", _held_sync)
    monkeypatch.setattr(mcp_mod, "_sync_mcp_to_agent_batch", _held_sync)
    no_manager = MagicMock()
    no_manager.available.return_value = False
    monkeypatch.setattr(_shared, "_capability_manager", lambda: no_manager)

    resp = await _MUTATIONS[route]()

    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "mcp_config_busy"


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
def test_a_held_agent_config_raises_out_of_the_sync_and_is_left_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch: bool
) -> None:
    from kiro_crew.dashboard.handlers import agents, mcp_agent_sync

    agent_cfg = tmp_path / "kirocrew.json"
    agent_cfg.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    before = agent_cfg.read_bytes()
    monkeypatch.setattr(agents, "_installed_agent_config", lambda: agent_cfg)
    reader_threads: list[int] = []
    monkeypatch.setattr(atomic_write, "read_bytes_with_retry", _held(agent_cfg, reader_threads))

    # Skipping would answer "applied" while kiro-cli still loads the old spec.
    with pytest.raises(PermissionError):
        if batch:
            mcp_agent_sync._sync_mcp_to_agent_batch_unlocked(["srv"], False)
        else:
            mcp_agent_sync._sync_mcp_to_agent_unlocked("srv", False)

    assert reader_threads, "the agent config was not read through the retrying read"
    assert agent_cfg.read_bytes() == before
