"""A local MCP probe spawns its child off the calling event loop's thread.

``create_subprocess_exec`` forks and execs synchronously before its first
await, so a probe that spawned on the gateway loop froze every tab for as long
as the fork/exec took. These tests replace the spawn seam with a fake and
record where it ran; no real MCP server is started.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from kiro_crew import mcp_discovery, platform_compat
from kiro_crew.mcp_discovery import McpServerInfo, probe_server


class _Stdin:
    def write(self, _data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _Stdout:
    async def readline(self) -> bytes:
        await asyncio.sleep(3600)  # a server that never answers
        return b""


class _Stderr:
    async def read(self, _n: int) -> bytes:
        return b""


class _Proc:
    pid = 987654
    returncode: int | None = None
    stdin = _Stdin()
    stdout = _Stdout()
    stderr = _Stderr()

    async def wait(self) -> int:
        return 0

    def kill(self) -> None:
        pass


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake the sandbox wrap, the spawn, and both platforms' reap; record the spawn's thread."""
    seen: dict[str, Any] = {"spawned": threading.Event(), "reaped": threading.Event()}

    async def _wrap(argv: list[str], **kwargs: Any) -> tuple[list[str], dict[str, str], None]:
        return list(argv), dict(kwargs.get("env") or {}), None

    async def _spawn(*_argv: str, **_kwargs: Any) -> _Proc:
        seen["thread"] = threading.current_thread()
        seen["loop"] = asyncio.get_running_loop()
        seen["spawned"].set()
        return _Proc()

    def _reap(pid: int, _sig: int) -> None:
        seen["reaped_pid"] = pid
        seen["reaped"].set()

    monkeypatch.setattr(mcp_discovery, "sandboxed_spawn_argv_async", _wrap)
    monkeypatch.setattr(mcp_discovery, "create_subprocess_limited", _spawn)
    monkeypatch.setattr(mcp_discovery.shutil, "which", lambda *_a, **_k: "/usr/bin/true")
    # POSIX reaps the process group with killpg; Windows reaps the tree.
    monkeypatch.setattr(mcp_discovery.os, "killpg", _reap, raising=False)
    monkeypatch.setattr(platform_compat, "kill_process_tree", _reap)
    return seen


@pytest.mark.asyncio
async def test_spawn_runs_off_the_calling_loop_thread(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_discovery, "_get_probe_timeout", lambda: 0.2)
    server = await probe_server(McpServerInfo(name="slow", command="true"))

    assert seams["thread"] is not threading.current_thread()
    assert seams["thread"].name.startswith("mc-mcpprobe")
    assert seams["loop"] is not asyncio.get_running_loop()
    # The private loop's result comes back whole: timeout reported, group reaped.
    assert (server.status, server.error) == ("error", "timeout")
    assert seams["reaped_pid"] == _Proc.pid


@pytest.mark.asyncio
async def test_cancel_reaches_the_private_probe_and_reaps_the_child(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Long enough that only a forwarded cancel -- not the probe's own
    # deadline -- can end the probe inside the wait below.
    monkeypatch.setattr(mcp_discovery, "_get_probe_timeout", lambda: 120)
    task = asyncio.ensure_future(probe_server(McpServerInfo(name="hung", command="true")))
    assert await asyncio.to_thread(seams["spawned"].wait, 10)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await asyncio.to_thread(seams["reaped"].wait, 10)
    assert seams["reaped_pid"] == _Proc.pid
