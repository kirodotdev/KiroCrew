"""``kirocrew-core`` under an enforced harness's credential mask, end to end.

The real server process, launched through the real sandbox chokepoint with the mask
codex sessions get (``adapter_hidden_credential_dirs("codex")``), calls
``memory_recall`` against a gateway that runs the real ``token_auth`` middleware and
the real agent socket. The child's environment is what codex-rs gives a stdio MCP
server: a cleared environment plus the element's own ``env``.

Without a pickup the child reads no secret through the mask and is refused
``internal_secret_missing``, which is the reported bug. With one it redeems on the
agent socket and the same call is admitted as an internal caller for its session.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from aiohttp import web

from kiro_crew.agent_ipc_wire import GATEWAY_PID_ENV, PICKUP_ENV
from kiro_crew.testing.wait import default_timeout

pytestmark = [
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux namespace mask"),
    pytest.mark.timeout(180),
]

_SECRET = "f" * 32
_SESSION = "dashboard:chat-e2e"
_TOKEN = "e2e-stub-token"
_SRC = str(Path(os.path.abspath(__file__)).parents[1] / "src")


def _src_spellings() -> tuple[str, ...]:
    """This tree's ``src`` under every spelling a mask may name it by.

    A checkout inside a session scratch dir sits under a masked root, and the mask
    names that root through ``$HOME``, which can be a symlink; a window opened under
    another spelling would not lift it.
    """
    real = os.path.realpath(_SRC)
    out = {_SRC, real}
    home = os.environ.get("HOME", "")
    real_home = os.path.realpath(home) if home else ""
    if real_home and real.startswith(real_home + os.sep):
        out.add(home + real[len(real_home) :])
    return tuple(sorted(out))


_SERVER = "from kiro_crew.mcp_core import run_mcp_core_server; run_mcp_core_server()"


def _frames() -> bytes:
    msgs = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "e2e", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "memory_recall", "arguments": {"query": "what did I decide"}},
        },
    ]
    return b"".join(json.dumps(m).encode() + b"\n" for m in msgs)


def _call_through_mask(argv: list[str], env: dict[str, str]) -> str:
    """Run the server, send the frames, and return the ``tools/call`` result text."""
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    lines: queue.Queue[bytes] = queue.Queue()

    def _pump() -> None:
        # The only blocking read, on a daemon thread, so the wait below is bounded and
        # killing the child is what ends this thread.
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            lines.put(raw)
        lines.put(b"")

    reader = threading.Thread(target=_pump, daemon=True)
    reader.start()
    try:
        assert proc.stdin is not None
        proc.stdin.write(_frames())
        proc.stdin.flush()
        while True:
            try:
                line = lines.get(timeout=default_timeout())
            except queue.Empty:
                raise AssertionError("server did not answer within the wait budget") from None
            if not line:
                raise AssertionError("server exited before answering")
            msg = json.loads(line)
            if msg.get("id") == 2:
                return "".join(c.get("text", "") for c in msg["result"]["content"])
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        try:
            proc.wait(timeout=default_timeout())
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=default_timeout())
        reader.join(timeout=default_timeout())


@pytest.mark.asyncio
async def test_kirocrew_core_reaches_the_gateway_through_the_codex_mask_only_by_capability(
    short_sock_dir, monkeypatch
):
    from kiro_crew.agent_sdk.tool_gate import adapter_hidden_credential_dirs
    from kiro_crew.dashboard import agent_ipc
    from kiro_crew.dashboard.token_auth import token_auth_middleware
    from kiro_crew.sandbox import SandboxUnavailableError
    from kiro_crew.sandbox_spawn import sandboxed_spawn_argv_async

    home = short_sock_dir / "h"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr(agent_ipc, "STORE", agent_ipc.CapabilityStore())
    monkeypatch.setattr(
        agent_ipc, "verify_session_token", lambda tok: _SESSION if tok == _TOKEN else ""
    )

    seen: list[dict] = []

    async def recall(request: web.Request) -> web.Response:
        seen.append(
            {
                "internal_auth": bool(request.get("internal_auth")),
                "session": request.headers.get("X-Session-Key", ""),
                "agent_socket": request.get(agent_ipc.GRANT_KEY) is not None,
            }
        )
        return web.json_response({"answer": "remembered: ship the fix"})

    app = web.Application(
        middlewares=[
            token_auth_middleware(internal_paths={"/api/memory/recall"}, internal_secret=_SECRET)
        ]
    )
    app.router.add_get("/api/memory/recall", recall)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    agent_path = await agent_ipc.start_agent_site(runner, port)
    try:
        assert agent_path is not None
        # The credential an UNMASKED child would read: every place read_local_secret
        # looks. The mask is what keeps it from this child.
        (home / "run").mkdir(exist_ok=True)
        for name in (f"gateway-{port}.secret", f"gateway-{port}-127.0.0.1.secret"):
            (home / "run" / name).write_text(_SECRET)
        (home / ".local_secret").write_text(_SECRET)

        mask = adapter_hidden_credential_dirs("codex")
        assert str(home / "run") in mask and str(home / ".local_secret") in mask
        # What codex-rs gives a stdio server: env_clear(), its allowlist, the element env.
        element_env = {
            "HOME": os.environ.get("HOME", str(home)),
            "PATH": os.environ.get("PATH", ""),
            "LANG": "C.UTF-8",
            "KIROCREW_HOME": str(home),
            "KIROCREW_BOUND_PORT": str(port),
            "KIROCREW_SESSION_KEY": _SESSION,
            "KIROCREW_STUB_SESSION_TOKEN": _TOKEN,
            "PYTHONPATH": os.pathsep.join(_src_spellings()),
        }
        try:
            wrapped = await sandboxed_spawn_argv_async(
                [sys.executable, "-c", _SERVER],
                env=element_env,
                extra_hidden_dirs=mask,
                # Open a window onto this tree so the child imports it, not an
                # installed copy.
                extra_private_dirs=_src_spellings(),
            )
        except SandboxUnavailableError:
            pytest.skip("no OS sandbox backend on this host, so the mask cannot be applied")
        argv, env, _cleanup = wrapped
        if argv[:2] == [sys.executable, "-c"]:
            pytest.skip("no OS sandbox backend on this host, so the mask cannot be applied")
        loop = asyncio.get_running_loop()

        before = await asyncio.wait_for(
            loop.run_in_executor(None, _call_through_mask, argv, dict(env)), 120
        )
        assert "internal credential" in before, before
        assert seen == []

        # This test process is the session runtime's root: the server descends from it.
        def _with_pickup(base: dict[str, str]) -> dict[str, str]:
            pickup = agent_ipc.issue_pickup(
                server="kirocrew-core", session_token=_TOKEN, runtime_pid=os.getpid
            )
            # This process is also the gateway that serves the agent socket.
            return {**base, PICKUP_ENV: pickup, GATEWAY_PID_ENV: str(os.getpid())}

        after = await asyncio.wait_for(
            loop.run_in_executor(None, _call_through_mask, argv, _with_pickup(dict(env))), 120
        )
        assert "remembered: ship the fix" in after, after
        assert seen == [{"internal_auth": True, "session": _SESSION, "agent_socket": True}]

        # A warm-pool runtime starts its servers before a session claims it, so the
        # element names no session; through the mask the child cannot verify the token
        # mapping either, and learns its session from the gateway instead.
        warm = {k: v for k, v in env.items() if k != "KIROCREW_SESSION_KEY"}
        claimed = await asyncio.wait_for(
            loop.run_in_executor(None, _call_through_mask, argv, _with_pickup(warm)), 120
        )
        assert "remembered: ship the fix" in claimed, claimed
        assert seen[-1] == {"internal_auth": True, "session": _SESSION, "agent_socket": True}
    finally:
        agent_ipc._BOUND_PATHS.discard(str(agent_path))
        await runner.cleanup()
