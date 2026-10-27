"""Shared harness for the resource-subscription gateway tests.

A REAL gatewayd runs in-process against ``fake_pool_mcp_server.py`` in its
subscription mode, and connections reach it two ways: a real stub process, which
is what kiro-cli runs, and :class:`RawConnection`, which speaks the stub protocol
directly from the test process. The second is what a connection the Kiro Crew
server opens for itself looks like to the daemon: its peer is the server's own
process, so it shares that process's ancestry with every stub the server spawned.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway import stub as stub_mod
from kiro_crew.mcp_gateway import stub_register, transport
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.mcp_gateway.pool import BackendPool
from kiro_crew.testing.wait import async_wait_until, finished

FAKE_SERVER = Path(__file__).with_name("fake_pool_mcp_server.py")

#: Bound on one reply through the gateway. Generous for the same reason as the
#: pool integration tests: a real process round trip on a loaded CI host.
REPLY_TIMEOUT = 60.0

SERVER = "fake"
AGENT = "probe"


def stub_argv(socket_path: Path, work_dir: Path) -> list[str]:
    """The stub argv the rewriter would write for the fake server, pooled."""
    return [
        "--server",
        SERVER,
        "--agent",
        AGENT,
        "--target-command",
        sys.executable,
        "--target-args",
        str(FAKE_SERVER),
        "--work-dir",
        str(work_dir),
        "--socket",
        str(socket_path),
        "--sandbox-mode",
        "off",
        "--approval-mode",
        "auto",
        "--poolable",
    ]


def register_payload_like_the_stub(socket_path: Path, work_dir: Path) -> dict[str, Any]:
    """The Register a real stub with :func:`stub_argv` sends, built in-process.

    Built by the stub's own parser and payload builder so the connection lands
    on the same PoolKey -- and so the same pooled backend -- as the real stub.
    """
    args = stub_mod._parse_args(stub_argv(socket_path, work_dir))
    payload = stub_register.build_register_payload(args)
    payload.pop("stub_session_token", None)
    return payload


def clean_env(home: Path) -> dict[str, str]:
    import os

    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("KIROCREW_CHANNEL_ID", "KIROCREW_SESSION_KEY", STUB_SESSION_TOKEN_ENV)
    }
    env["KIROCREW_HOME"] = str(home)
    return env


async def spawn_stub(
    socket_path: Path,
    work_dir: Path,
    home: Path,
    *,
    session_token: str = "",
    session_key: str = "",
) -> asyncio.subprocess.Process:
    """A real stub process, as kiro-cli launches it for the fake server."""
    env = clean_env(home)
    if session_token:
        env[STUB_SESSION_TOKEN_ENV] = session_token
    if session_key:
        env["KIROCREW_SESSION_KEY"] = session_key
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "kiro_crew.mcp_gateway.stub",
        *stub_argv(socket_path, work_dir),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=work_dir,
        env=env,
    )


class StubProcess:
    """JSON-RPC over a real stub's stdio, keeping every notification it prints."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.messages: list[dict[str, Any]] = []
        self._changed = asyncio.Condition()
        self._pump = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self.proc.stdout is not None
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                return
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            async with self._changed:
                self.messages.append(msg)
                self._changed.notify_all()

    async def request(self, req_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self.proc.stdin is not None
        frame = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        self.proc.stdin.write((json.dumps(frame) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()
        return await wait_for_message(self.messages, self._changed, lambda m: m.get("id") == req_id)

    async def close(self) -> None:
        if self.proc.returncode is None:
            try:
                await pc.kill_process_tree_async(self.proc.pid, pc.SIGKILL)
            except Exception:  # noqa: BLE001 - teardown must never mask a failure
                pass
        try:
            await finished(self.proc.wait(), "stub wait")
        except (asyncio.TimeoutError, ProcessLookupError, AssertionError):
            pass
        self._pump.cancel()


async def wait_for_message(
    messages: list[dict[str, Any]],
    changed: asyncio.Condition,
    match: Any,
    *,
    start: int = 0,
    timeout: float = REPLY_TIMEOUT,
) -> dict[str, Any]:
    """The first message at or after *start* that *match* accepts."""

    async def _scan() -> dict[str, Any]:
        async with changed:
            while True:
                for msg in messages[start:]:
                    if match(msg):
                        return msg
                await changed.wait()

    return await asyncio.wait_for(_scan(), timeout=timeout)


class RawConnection:
    """One stub-protocol connection opened from the test process itself."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.stub_uuid = ""
        self.messages: list[dict[str, Any]] = []
        self._changed = asyncio.Condition()
        self._pump: Optional[asyncio.Task[None]] = None

    @classmethod
    async def register(cls, socket_path: Path, payload: dict[str, Any]) -> "RawConnection":
        reader, writer = await transport.connect(socket_path)
        conn = cls(reader, writer)
        frame = dict(payload)
        frame["stub_uuid"] = conn.stub_uuid = str(uuid.uuid4())
        await conn._send(frame)
        reply = json.loads(await asyncio.wait_for(reader.readline(), REPLY_TIMEOUT))
        assert reply.get("type") == "registered", reply
        await conn._send({"type": "ensure_backend"})
        ready = json.loads(await asyncio.wait_for(reader.readline(), REPLY_TIMEOUT))
        assert ready.get("type") == "ready", ready
        conn._pump = asyncio.create_task(conn._read())
        return conn

    async def _send(self, frame: dict[str, Any]) -> None:
        self.writer.write((json.dumps(frame) + "\n").encode("utf-8"))
        await self.writer.drain()

    async def _read(self) -> None:
        while True:
            line = await self.reader.readline()
            if not line:
                return
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            async with self._changed:
                self.messages.append(msg)
                self._changed.notify_all()

    async def request(self, req_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        await self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        return await wait_for_message(self.messages, self._changed, lambda m: m.get("id") == req_id)

    async def wait_for(self, match: Any, *, start: int = 0) -> dict[str, Any]:
        return await wait_for_message(self.messages, self._changed, match, start=start)

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (OSError, ConnectionError):
            pass
        if self._pump is not None:
            self._pump.cancel()


def updated_for(uri: str) -> Any:
    return lambda m: m.get("method") == "notifications/resources/updated" and (
        (m.get("params") or {}).get("uri") == uri
    )


INIT_PARAMS = {
    "protocolVersion": "2025-11-25",
    "capabilities": {},
    "clientInfo": {"name": "kiro", "version": "0.0.0"},
}


class Daemon:
    """A running in-process gatewayd and the pool it built."""

    def __init__(
        self, socket_path: Path, pools: list[BackendPool], task: asyncio.Task[None]
    ) -> None:
        self.socket_path = socket_path
        self.pools = pools
        self.task = task

    def backends(self) -> list[Any]:
        return [b for pool in self.pools for b in pool.all_backends()]


@asynccontextmanager
async def running_daemon(
    monkeypatch: pytest.MonkeyPatch,
    socket_path: Path,
    work_dir: Path,
    launch_log: Path,
    subscribe_log: Path,
    caller_log: Path,
    *,
    approved: Optional[Any] = None,
) -> AsyncIterator[Daemon]:
    """gatewayd on *socket_path*, launching the fake server in subscription mode.

    *approved*, when given, is called per spawn; returning False stands in for a
    launch the operator's approval does not admit, which the real resolver
    answers with ``None``.
    """
    pools: list[BackendPool] = []

    class _RecordingPool(BackendPool):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            pools.append(self)

    monkeypatch.setattr(gw, "BackendPool", _RecordingPool)

    def _resolver(_key: object) -> Optional[tuple[str, list[str], dict[str, str], str]]:
        if approved is not None and not approved():
            return None
        return (
            sys.executable,
            [str(FAKE_SERVER), str(launch_log), str(caller_log)],
            {"FAKE_POOL_SUBSCRIBE_LOG": str(subscribe_log)},
            str(work_dir),
        )

    stop = asyncio.Event()
    task = asyncio.create_task(
        gw.run_gatewayd(
            socket_path=socket_path,
            max_backends=8,
            idle_timeout_secs=300,
            stop_event=stop,
            target_resolver=_resolver,
            prewarm_count=0,
        )
    )
    await async_wait_until(
        lambda: transport.endpoint_exists(socket_path),
        off_loop=True,
        describe=lambda: "gatewayd never bound its endpoint",
    )
    try:
        yield Daemon(socket_path, pools, task)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=30)
