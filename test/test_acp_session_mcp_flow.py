"""Vertical-slice tests for editor-supplied stdio MCP servers reaching the model.

These cover the Phase-2 ownership path end to end WITHOUT a live kiro-cli (that
full black-box gate is Phase 3):

* ``_mcp_fingerprint`` — the config-identity that gates provider reuse.
* ``POST /api/chat/slots/{slot}/mcp`` — the daemon route that stores/clears the
  per-slot ACP MCP set and rejects malformed/duplicate input, secret-safe.
* ``SessionManager.get_or_create`` — threads the set to the ACP factory on cold
  start and RECREATES the provider when the set changes (kiro binds servers at
  session/new), while an unchanged/empty set reuses byte-identically.
* ``AcpProvider`` — forwards the set to ``runtime.create_session(mcp_servers=)``,
  the seam where the provider binary becomes the sole long-lived owner.
* An e2e proving a validated ACP set drives REAL MCP ``initialize`` /
  ``tools/list`` / ``tools/call`` traffic against a fixture and streams the tool
  result — no faked result.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import platform_compat
from kiro_crew.acp_server.mcp_config import parse_mcp_servers, servers_to_acp_dicts
from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard.chat import api_chat_slot_mcp
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.session import SessionManager, _mcp_fingerprint

# ── a fixture stdio MCP server: initialize + tools/list + echo tools/call ──

_ECHO_MCP = r"""
import sys, json
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "serverInfo": {"name": "fixture-echo", "version": "0"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": "echo", "description": "echo text",
             "inputSchema": {"type": "object",
                             "properties": {"text": {"type": "string"}}}}]}})
    elif method == "tools/call":
        args = (msg.get("params") or {}).get("arguments") or {}
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": "echo:" + str(args.get("text", ""))}]}})
    elif method and method.startswith("notifications/"):
        pass  # notifications get no response
"""


def _acp_server(name: str, script: str = _ECHO_MCP) -> dict:
    """A canonical ACP stdio mcpServers entry running *script* under this python."""
    return {"name": name, "command": sys.executable, "args": ["-c", script]}


# ─────────────────────────── fingerprint identity ───────────────────────────


class TestMcpFingerprint:
    def test_empty_and_none_are_equal_and_blank(self):
        assert _mcp_fingerprint(None) == ""
        assert _mcp_fingerprint([]) == ""

    def test_same_set_same_fingerprint(self):
        a = [_acp_server("echo"), _acp_server("other", "import sys")]
        b = [_acp_server("echo"), _acp_server("other", "import sys")]
        assert _mcp_fingerprint(a) == _mcp_fingerprint(b)

    def test_order_independent(self):
        a = [_acp_server("a"), _acp_server("b")]
        b = [_acp_server("b"), _acp_server("a")]
        assert _mcp_fingerprint(a) == _mcp_fingerprint(b)

    def test_command_change_changes_fingerprint(self):
        a = [{"name": "s", "command": "/bin/a", "args": [], "env": []}]
        b = [{"name": "s", "command": "/bin/b", "args": [], "env": []}]
        assert _mcp_fingerprint(a) != _mcp_fingerprint(b)

    def test_env_change_changes_fingerprint(self):
        a = [{"name": "s", "command": "/bin/a", "args": [], "env": [{"name": "K", "value": "1"}]}]
        b = [{"name": "s", "command": "/bin/a", "args": [], "env": [{"name": "K", "value": "2"}]}]
        assert _mcp_fingerprint(a) != _mcp_fingerprint(b)


# ─────────────────────────────── daemon route ───────────────────────────────


def _make_app(state: DashboardState, *, caller: str = "internal") -> web.Application:
    @web.middleware
    async def auth_context(request: web.Request, handler):
        if caller == "internal":
            request["internal_auth"] = True
        elif caller == "app":
            request["app"] = "example-app"
        return await handler(request)

    app = web.Application(middlewares=[auth_context])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/mcp", api_chat_slot_mcp)
    return app


def _mock_state(slot: _ChatSlot | None = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {}
    if slot:
        state._slots[slot.key] = slot
    return state


class TestSlotMcpRoute:
    @pytest.mark.asyncio
    async def test_registers_canonical_servers(self):
        slot = _ChatSlot("s")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s/mcp",
                json={
                    "servers": [
                        {
                            "name": "echo",
                            "command": "/bin/echo",
                            "args": ["hi"],
                            "env": [{"name": "K", "value": "V"}],
                        }
                    ]
                },
            )
            assert resp.status == 200
            data = await resp.json()
            assert data == {"ok": True, "servers": ["echo"]}
        # Stored in canonical ACP shape (env as array-of-{name,value}).
        assert slot.session_mcp_servers == [
            {
                "name": "echo",
                "command": "/bin/echo",
                "args": ["hi"],
                "env": [{"name": "K", "value": "V"}],
            }
        ]

    @pytest.mark.asyncio
    async def test_empty_clears(self):
        slot = _ChatSlot("s")
        slot.session_mcp_servers = [{"name": "old", "command": "/x", "args": [], "env": []}]
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s/mcp", json={"servers": []})
            assert resp.status == 200
        assert slot.session_mcp_servers == []

    @pytest.mark.asyncio
    async def test_app_token_cannot_register_host_commands(self):
        slot = _ChatSlot("s")
        slot._app = "example-app"
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state, caller="app"))) as client:
            resp = await client.post(
                "/api/chat/slots/s/mcp",
                json={"servers": [{"name": "shell", "command": "/bin/sh"}]},
            )
            assert resp.status == 403
        assert slot.session_mcp_servers == []

    @pytest.mark.asyncio
    async def test_duplicate_name_rejected_secret_safe(self):
        slot = _ChatSlot("s")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s/mcp",
                json={
                    "servers": [
                        {
                            "name": "dup",
                            "command": "/a",
                            "env": [{"name": "SECRET", "value": "tok"}],
                        },
                        {"name": "dup", "command": "/b"},
                    ]
                },
            )
            assert resp.status == 400
            msg = (await resp.json())["error"]
            assert "duplicate" in msg
            assert "tok" not in msg  # never leaks an env value
        # Rejected input must not mutate the slot.
        assert slot.session_mcp_servers == []

    @pytest.mark.asyncio
    async def test_unsupported_transport_rejected(self):
        slot = _ChatSlot("s")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s/mcp",
                json={"servers": [{"name": "remote", "url": "https://mcp.example"}]},
            )
            assert resp.status == 400
            assert "transport" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_slot_not_found(self):
        state = _mock_state()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/missing/mcp", json={"servers": []})
            assert resp.status == 404


# ───────────────────── get_or_create threading + reuse ──────────────────────


@pytest.fixture
def cfg():
    c = KiroCrewConfig()
    c.session.timeout_secs = 2
    return c


def _recording_factory(calls: list):
    """Factory recording the session_mcp_servers each cold start received."""

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        calls.append(kwargs.get("session_mcp_servers"))
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.is_process_alive = lambda: True
        m.is_alive = lambda: True
        m.context_usage_pct = lambda: 0.0
        return m

    return factory


class TestGetOrCreateMcpThreading:
    @pytest.mark.asyncio
    async def test_set_threaded_to_factory_on_cold_start(self, cfg):
        calls: list = []
        mgr = SessionManager(cfg, provider_factory=_recording_factory(calls))
        servers = [_acp_server("echo")]
        await mgr.get_or_create("k", session_mcp_servers=servers)
        mgr.release("k")
        assert calls == [servers]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_same_set_reuses_provider(self, cfg):
        calls: list = []
        mgr = SessionManager(cfg, provider_factory=_recording_factory(calls))
        servers = [_acp_server("echo")]
        p1, new1, _ = await mgr.get_or_create("k", session_mcp_servers=servers)
        mgr.release("k")
        p2, new2, _ = await mgr.get_or_create("k", session_mcp_servers=list(servers))
        mgr.release("k")
        assert p1 is p2 and new1 is True and new2 is False
        assert len(calls) == 1  # no recreation for an identical set
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_changed_set_recreates_provider(self, cfg):
        calls: list = []
        mgr = SessionManager(cfg, provider_factory=_recording_factory(calls))
        p1, _, _ = await mgr.get_or_create("k", session_mcp_servers=[_acp_server("echo")])
        mgr.release("k")
        p2, new2, _ = await mgr.get_or_create(
            "k", session_mcp_servers=[_acp_server("echo", "import sys")]
        )
        mgr.release("k")
        assert p1 is not p2  # provider recreated for a changed MCP set
        assert new2 is True
        assert len(calls) == 2
        p1.shutdown.assert_awaited()  # the stale provider was shut down
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_no_servers_is_byte_identical_reuse(self, cfg):
        calls: list = []
        mgr = SessionManager(cfg, provider_factory=_recording_factory(calls))
        await mgr.get_or_create("k")
        mgr.release("k")
        await mgr.get_or_create("k")
        mgr.release("k")
        assert calls == [None]  # never threaded, single cold start, reused
        await mgr.close_all()


# ───────────────── provider forwards config to create_session ────────────────


class _FakeHandle:
    def __init__(self) -> None:
        self.session_id = "kiro-sess-1"

    async def set_model(self, model):  # pragma: no cover - not hit (model unset)
        return None


class _FakeRuntime:
    last_create_kwargs: dict = {}
    last_load_kwargs: dict = {}

    def __init__(self, **kwargs) -> None:
        self.pid = 4321
        self._kwargs = kwargs

    async def spawn(self) -> None:
        return None

    def saw_not_logged_in(self) -> bool:
        return False

    def is_alive(self) -> bool:
        return True

    async def kill(self) -> None:
        return None

    async def create_session(self, cwd=None, agent=None, mcp_servers=None):
        type(self).last_create_kwargs = {"cwd": cwd, "agent": agent, "mcp_servers": mcp_servers}
        return _FakeHandle()

    async def load_session(self, session_file, resume_sid, cwd=None, agent=None, mcp_servers=None):
        type(self).last_load_kwargs = {
            "session_file": session_file,
            "resume_sid": resume_sid,
            "cwd": cwd,
            "agent": agent,
            "mcp_servers": mcp_servers,
        }
        return _FakeHandle()


class _FakeSessionProvider:
    def __init__(self, handle, runtime, owns_runtime=False) -> None:
        self.handle = handle
        self.runtime = runtime
        self.resumed = False


class TestProviderForwarding:
    @pytest.mark.asyncio
    async def test_forwards_mcp_servers_to_runtime_create_session(self, tmp_path, monkeypatch):
        from kiro_crew.providers import acp as acp_provider

        monkeypatch.setattr(acp_provider, "AcpRuntime", _FakeRuntime)
        monkeypatch.setattr(acp_provider, "AcpSessionProvider", _FakeSessionProvider)
        _FakeRuntime.last_create_kwargs = {}

        servers = servers_to_acp_dicts(parse_mcp_servers([_acp_server("echo")]))
        provider = acp_provider.AcpProvider(
            work_dir=tmp_path, model="", session_mcp_servers=servers
        )
        await provider._start_kiro_runtime_impl({})
        # The editor's set rode into kiro-cli's session/new via create_session.
        assert _FakeRuntime.last_create_kwargs["mcp_servers"] == servers

    @pytest.mark.asyncio
    async def test_no_servers_forwards_none(self, tmp_path, monkeypatch):
        from kiro_crew.providers import acp as acp_provider

        monkeypatch.setattr(acp_provider, "AcpRuntime", _FakeRuntime)
        monkeypatch.setattr(acp_provider, "AcpSessionProvider", _FakeSessionProvider)
        _FakeRuntime.last_create_kwargs = {}

        provider = acp_provider.AcpProvider(work_dir=tmp_path, model="")
        await provider._start_kiro_runtime_impl({})
        assert _FakeRuntime.last_create_kwargs["mcp_servers"] is None

    @pytest.mark.asyncio
    async def test_forwards_mcp_servers_to_runtime_load_session(self, tmp_path):
        from kiro_crew.providers import acp as acp_provider

        servers = servers_to_acp_dicts(parse_mcp_servers([_acp_server("echo")]))
        provider = acp_provider.AcpProvider(
            work_dir=tmp_path, model="", session_mcp_servers=servers
        )
        runtime = _FakeRuntime()
        _FakeRuntime.last_load_kwargs = {}

        await provider._load_session_with_retry(
            runtime, str(tmp_path / "session.json"), "resume-1", tmp_path, ""
        )

        assert _FakeRuntime.last_load_kwargs["mcp_servers"] == servers


# ────────────────────── e2e: real MCP traffic to fixture ─────────────────────


async def _rpc(proc: asyncio.subprocess.Process, obj: dict) -> dict:
    """Send one JSON-RPC request and read frames until the matching id returns."""
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write((json.dumps(obj) + "\n").encode())
    await proc.stdin.drain()
    while True:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=5.0)
        if not line:
            raise AssertionError("server closed before responding")
        try:
            msg = json.loads(line.decode().strip())
        except ValueError:
            continue
        if msg.get("id") == obj.get("id"):
            return msg


class _RealMcpRuntime:
    """A fake ACP runtime that speaks REAL MCP to the client-supplied servers.

    Unlike ``_FakeRuntime`` (which only records), this actually spawns each
    stdio server and performs ``initialize`` + ``tools/list`` at create_session,
    then ``tools/call`` on prompt — proving the editor's validated MCP set turns
    into genuine wire traffic and a streamed tool result.
    """

    def __init__(self) -> None:
        self.procs: list[asyncio.subprocess.Process] = []
        self.tools: list[str] = []

    async def create_session(self, mcp_servers: list[dict]) -> "_RealMcpSession":
        for server in mcp_servers:
            proc = await asyncio.create_subprocess_exec(
                server["command"],
                *server["args"],
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(os.environ),
            )
            self.procs.append(proc)
            init = await _rpc(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05"},
                },
            )
            assert "result" in init
            listed = await _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            self.tools += [t["name"] for t in listed["result"]["tools"]]
        return _RealMcpSession(self.procs)

    async def close(self) -> None:
        for proc in self.procs:
            if proc.returncode is None:
                proc.terminate()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=2.0)


class _RealMcpSession:
    def __init__(self, procs: list[asyncio.subprocess.Process]) -> None:
        self._procs = procs

    async def prompt(self, text: str) -> str:
        # Call the fixture's echo tool with real tools/call traffic.
        resp = await _rpc(
            self._procs[0],
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"text": text}},
            },
        )
        return resp["result"]["content"][0]["text"]


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="spawns real child processes")
class TestEndToEndMcpTraffic:
    @pytest.mark.asyncio
    async def test_validated_set_drives_real_tool_call(self):
        # Production serialization: ACP wire -> validated -> canonical dicts.
        servers = servers_to_acp_dicts(parse_mcp_servers([_acp_server("echo")]))
        runtime = _RealMcpRuntime()
        try:
            session = await runtime.create_session(mcp_servers=servers)
            # initialize + tools/list actually happened over the wire.
            assert runtime.tools == ["echo"]
            # A prompt drives a real tools/call and streams the tool's result.
            result = await session.prompt("hello world")
            assert result == "echo:hello world"
        finally:
            await runtime.close()
        # The child was reaped — no fixture process survives the session.
        for proc in runtime.procs:
            assert proc.returncode is not None
