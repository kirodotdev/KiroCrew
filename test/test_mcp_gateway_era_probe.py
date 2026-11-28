"""The gateway answers the stateless-era probe itself, so no backend pipe goes modern.

A client on a current MCP SDK opens with ``server/discover``. A pipe serves the
era of its first request, and the gateway's backends are shared on the
``initialize`` handshake (init cache, spawn permit, handshake replay), so the
probe must never reach one: it is answered "method not found", the reply every
client reads as "use ``initialize``".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from test_mcp_gateway_recaller import _CALL, _FakeWriter, _register, _run
from test_stub_broker_reconnect import (
    _drive,
    _init_frame,
    _reap,
    _spawn_stub,
    _start_daemon,
    _stop_daemon,
    _tool_frame,
)

pytestmark = pytest.mark.xdist_group("mcp_gateway")

_ERA_SERVER = Path(__file__).with_name("fake_era_mcp_server.py")

_DISCOVER = {
    "jsonrpc": "2.0",
    "id": "probe-1",
    "method": "server/discover",
    "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
}


@pytest.mark.asyncio
async def test_server_discover_is_answered_and_never_forwarded(monkeypatch) -> None:
    writer = _FakeWriter()
    backend, _audit = await _run(
        [_register("dashboard:era"), _DISCOVER, _CALL], monkeypatch, writer=writer
    )
    # Only the call after the probe reached the backend.
    assert len(backend.callers) == 1
    replies = [f for f in writer.frames if f.get("id") == "probe-1"]
    assert replies == [
        {
            "jsonrpc": "2.0",
            "id": "probe-1",
            "error": {"code": -32601, "message": "Method not found"},
        }
    ]


@pytest.mark.asyncio
async def test_a_probing_client_handshakes_and_survives_a_broker_restart(
    tmp_path: Path, short_sock_dir
) -> None:
    """A real daemon, a real stub and a server that locks its pipe's era.

    The client probes, then falls back to ``initialize`` on the refusal, as the
    MCP SDKs do. Its captured ``initialize`` is what the stub replays to the next
    daemon, so its calls keep working after the broker restarts.
    """
    sock = Path(short_sock_dir) / "gw.sock"
    work_dir = tmp_path / "wd"
    work_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    def _resolver(_key: object) -> tuple[str, list[str], dict[str, str], str]:
        return sys.executable, [str(_ERA_SERVER)], {}, str(work_dir)

    stop, daemon = await _start_daemon(sock, _resolver)
    procs = []
    try:
        proc = await _spawn_stub(
            socket_path=sock, work_dir=work_dir, home=home, session_key="dashboard:era"
        )
        procs.append(proc)
        probe = await _drive(proc, json.dumps({**_DISCOVER, "id": 1}) + "\n", req_id=1)
        assert probe.get("error", {}).get("code") == -32601, probe
        assert "result" in await _drive(proc, _init_frame(2), req_id=2)
        call = await _drive(proc, _tool_frame(3), req_id=3)
        assert call["result"]["content"][0]["text"] == "handshake", call

        await _stop_daemon(stop, daemon)
        stop, daemon = await _start_daemon(sock, _resolver)
        call = await _drive(proc, _tool_frame(4), req_id=4)
        if call.get("error", {}).get("code") == -32603:
            # A call that reached the dying bridge before the stub saw the old
            # daemon go is failed retryably by design; one retry with a fresh
            # id is what a client does with it.
            call = await _drive(proc, _tool_frame(5), req_id=5)
        assert call["result"]["content"][0]["text"] == "handshake", call
    finally:
        await _reap(procs)
        await _stop_daemon(stop, daemon)
