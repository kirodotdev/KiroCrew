"""The batch guard's wiring into both transports: a prompt sent through the
real send path reaches the guard after the setting is read off the event loop
(``refresh_feeding``), and a resumed session marks its window resumed
(``mark_resumed``). Each test fails when its call site is removed.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture
from acp_runtime_helpers import _make_runtime
from test_update_provider import _UNALLOCATABLE_PID

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import ACP_BACKEND_KIRO, METHOD_SESSION_LOAD
from kiro_crew.testing.wait import finished

_ITEM = "it_0badc0de"


def _guard():
    return importlib.import_module("kiro_crew.acp.batch_guard")


@pytest.fixture
def guard_on(monkeypatch):
    monkeypatch.setattr(_guard(), "guard_settings", lambda: (True, ()))


# -- a prompt reaches the guard --


@pytest.mark.asyncio
async def test_a_client_prompt_feeds_the_guard_once_the_setting_is_read(tmp_path, guard_on):
    client = AcpClient(work_dir=tmp_path)
    client._session_id = "sid"
    client._send_request = AsyncMock(return_value=3)
    assert await client._send_prompt(f"bind {_ITEM} now") == 3
    window = client._batch_window
    assert window._feeding is True
    assert _ITEM in window.seen


def _handle() -> AcpSessionHandle:
    rt = AcpRuntime(work_dir="/tmp")
    proc = MagicMock()
    proc.stdout = asyncio.StreamReader()
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = _UNALLOCATABLE_PID
    rt._process = proc
    rt._pid = _UNALLOCATABLE_PID
    rt._initialized = True
    queue: asyncio.Queue = asyncio.Queue()
    rt._session_queues["sA"] = queue
    return AcpSessionHandle("sA", queue, rt)


@pytest.mark.asyncio
async def test_a_handle_prompt_feeds_the_guard_once_the_setting_is_read(guard_on):
    handle = _handle()
    sent = asyncio.Event()

    async def _send(*_args, **_kwargs):
        sent.set()
        return 7

    handle._runtime.send_request = _send  # type: ignore[method-assign]
    gen = handle.prompt(f"bind {_ITEM} now")
    turn = asyncio.ensure_future(gen.__anext__())
    # The prompt is fed to the guard before it is sent, so the send is the event.
    await finished(sent.wait(), "the prompt's send")
    turn.cancel()
    with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
        await turn
    await gen.aclose()
    window = handle._batch_window
    assert window._feeding is True
    assert _ITEM in window.seen


# -- a resumed session is marked resumed --


@pytest.mark.asyncio
async def test_a_client_resume_marks_the_window_resumed(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    client = AcpClient(work_dir=tmp_path)
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    client._process = proc
    client._next_req_id = MagicMock(side_effect=range(1, 100))
    client._resume_session_id = "old-sess"
    session_dir = Path.home() / ".kiro" / "sessions" / "cli"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "old-sess.json").write_text("{}")
    responses = {
        1: {"protocolVersion": "2025-08-22", "agentCapabilities": {"loadSession": True}},
        2: {"modes": ["chat"]},
    }

    async def fake_wait(req_id, timeout=50.0, *, method="", expected_mcp=None):
        return responses.get(req_id, {})

    client._wait_for_response = AsyncMock(side_effect=fake_wait)
    client._drain_notifications = AsyncMock()

    await client._initialize_session()

    assert client._resumed is True
    assert client._batch_window.seen.resumed is True


@pytest.mark.asyncio
async def test_a_runtime_load_marks_the_handle_window_resumed(monkeypatch):
    rt, _, _ = _make_runtime()
    rt._acp_backend = ACP_BACKEND_KIRO
    rt._can_load_session = True

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_LOAD:
            return {"modes": {"currentModeId": "kirocrew"}}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)
    handle = await rt.load_session(
        "/home/u/.kiro/sessions/cli/sid-123.json", "sid-123", cwd="/work", agent="kirocrew"
    )
    assert handle._batch_window.seen.resumed is True
