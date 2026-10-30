"""The MCP probe and listing routes read ``~/.kiro/settings/mcp.json`` off the loop.

``_run_mcp_probe``, ``GET /api/mcp``, ``GET /api/mcp/active`` and
``POST /api/mcp/probe`` each read the Kiro-global ``mcp.json`` for its
disabled / ``disabledTools`` state. Read on the event loop, a slow home
directory (a network mount) stalled every session for the length of the read.
These tests spy on the retrying read to record the thread it ran on, and check
that a read still contended after its retries answers a coded 503; no real
server is probed or spawned.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable
from unittest.mock import AsyncMock

import pytest

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
