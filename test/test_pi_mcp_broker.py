"""Host-side Pi MCP broker — secrets stay out of Pi's namespace (GPT F1)."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import pytest
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base

from kiro_crew.acp.pi_mcp_broker import (
    ENV_BROKER_SOCK,
    PiMcpBroker,
    broker_socket_path,
)
from kiro_crew.mcp_gateway import transport
from kiro_crew.mcp_gateway.pool import READ_BUFFER_LIMIT_BYTES


@pytest.fixture
def broker_dir(tmp_path: Path) -> Path:
    with tempfile.TemporaryDirectory(
        prefix=SHORT_TMP_PREFIX + "pmb-", dir=short_tmp_base()
    ) as path:
        yield Path(path)


@pytest.fixture(autouse=True)
def isolate_broker_child_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the protocol with fixture children, independent of host userns."""
    from kiro_crew.acp import pi_mcp_broker

    async def fixture_argv(argv, *, mode, extra_hidden_dirs, _prepare):
        del mode, extra_hidden_dirs, _prepare
        return argv, None

    monkeypatch.setattr(pi_mcp_broker, "wrap_argv_async", fixture_argv)


def test_broker_socket_path_requires_nonce(broker_dir: Path) -> None:
    with pytest.raises(ValueError, match="nonce"):
        broker_socket_path(artifact_dir=str(broker_dir), pid=1, nonce="")


def test_env_name_is_stable() -> None:
    assert ENV_BROKER_SOCK == "KIROCREW_PI_MCP_BROKER_SOCK"


def test_only_verified_host_children_omit_the_pi_credential_mask(broker_dir: Path) -> None:
    broker = PiMcpBroker(
        [],
        socket_path=str(broker_dir / "unused.sock"),
        hidden_dirs=("/private/credentials",),
        host_control_plane_servers=frozenset({"kirocrew-core"}),
    )
    assert broker._child_hidden_dirs("kirocrew-core", "kirocrew-core") == ()
    assert broker._child_hidden_dirs("kirocrew-core", " kirocrew-core") == ("/private/credentials",)
    assert broker._child_hidden_dirs("kirocrew-cron", "kirocrew-cron") == ("/private/credentials",)
    assert broker._child_hidden_dirs("third-party", "third-party") == ("/private/credentials",)


@pytest.mark.asyncio
async def test_padded_server_name_cannot_collide_with_managed_core(broker_dir: Path) -> None:
    broker = PiMcpBroker(
        [{"name": " kirocrew-core", "command": "/not/a/real/command"}],
        socket_path=str(broker_dir / "padded.sock"),
        hidden_dirs=("/private/credentials",),
        host_control_plane_servers=frozenset({"kirocrew-core"}),
    )
    await broker.start()
    try:
        assert broker.initialized_servers == frozenset()
        assert broker.tool_count == 0
    finally:
        await broker.stop()


@pytest.mark.asyncio
async def test_broker_lists_and_calls_a_stdio_echo_server(broker_dir: Path, tmp_path: Path) -> None:
    # Minimal stdio MCP server: initialize → tools/list → tools/call.
    script = tmp_path / "echo_mcp.py"
    script.write_text(
        """\
import json, sys
def recv():
    line = sys.stdin.readline()
    return json.loads(line) if line else None
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()
while True:
    msg = recv()
    if msg is None:
        break
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":"2024-11-05","capabilities":{},"serverInfo":{"name":"echo","version":"0"}}})
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        send({"jsonrpc":"2.0","id":mid,"result":{"tools":[{"name":"ping","description":"pong","inputSchema":{"type":"object","properties":{}}}]}})
    elif method == "tools/call":
        args = (msg.get("params") or {}).get("arguments") or {}
        send({"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text","text":"pong:"+json.dumps(args)}]}})
    else:
        send({"jsonrpc":"2.0","id":mid,"error":{"code":-32601,"message":"unknown"}})
""",
        encoding="utf-8",
    )
    sock = broker_socket_path(artifact_dir=str(broker_dir), pid=99, nonce="abc")
    broker = PiMcpBroker(
        [{"name": "echo", "command": sys.executable, "args": [str(script)]}],
        socket_path=sock,
    )
    await broker.start()
    try:
        assert broker.tool_count == 1
        reader, writer = await transport.connect(sock)
        try:
            writer.write(
                (
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "bridge/list", "params": {}})
                    + "\n"
                ).encode()
            )
            await writer.drain()
            listed = json.loads((await reader.readline()).decode())
            assert listed["id"] == 1
            tools = listed["result"]["tools"]
            assert tools[0]["server"] == "echo"
            assert tools[0]["name"] == "ping"
            broker.note_permission(
                "r", {"toolCallId": "call", "title": "mcp__echo__ping", "input": {"x": 1}}
            )
            broker.approve_permission("r", broker.stage_permission("r"))
            writer.write(
                (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "bridge/call",
                            "params": {
                                "toolCallId": "call",
                                "server": "echo",
                                "tool": "ping",
                                "arguments": {"x": 1},
                            },
                        }
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            called = json.loads((await reader.readline()).decode())
            assert called["id"] == 2
            assert "pong" in json.dumps(called["result"])
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await broker.stop()


@pytest.mark.asyncio
async def test_broker_endpoint_carries_no_server_env(broker_dir: Path, tmp_path: Path) -> None:
    """F1 posture: the advertised endpoint string must not embed secrets."""
    script = tmp_path / "noop_mcp.py"
    script.write_text(
        "import json,sys\n"
        "def recv():\n"
        "    line=sys.stdin.readline()\n"
        "    return json.loads(line) if line else None\n"
        "def send(o):\n"
        "    sys.stdout.write(json.dumps(o)+'\\n'); sys.stdout.flush()\n"
        "while True:\n"
        "    m=recv()\n"
        "    if m is None: break\n"
        "    if m.get('method')=='initialize':\n"
        "        send({'jsonrpc':'2.0','id':m['id'],'result':{'protocolVersion':'2024-11-05','capabilities':{},'serverInfo':{'name':'n','version':'0'}}})\n"
        "    elif m.get('method')=='tools/list':\n"
        "        send({'jsonrpc':'2.0','id':m['id'],'result':{'tools':[]}})\n",
        encoding="utf-8",
    )
    secret = "super-secret-bearer-token-xyz"
    sock = broker_socket_path(artifact_dir=str(broker_dir), pid=7, nonce="nonce1")
    broker = PiMcpBroker(
        [
            {
                "name": "core",
                "command": sys.executable,
                "args": [str(script)],
                "env": [{"name": "KIROCREW_SESSION_KEY", "value": secret}],
            }
        ],
        socket_path=sock,
    )
    await broker.start()
    try:
        assert secret not in broker.endpoint
        assert secret not in broker.socket_path
        assert ENV_BROKER_SOCK not in broker.endpoint
    finally:
        await broker.stop()


def _stdio_mcp_script(tmp_path: Path, *, tools: list[dict], name: str = "fixture") -> Path:
    """Write a minimal NDJSON MCP server that serves the given tools/list."""
    script = tmp_path / f"{name}_mcp.py"
    tools_literal = json.dumps(tools)
    script.write_text(
        "import json, sys\n"
        f"TOOLS = json.loads({tools_literal!r})\n"
        "NAME = " + repr(name) + "\n"
        "def recv():\n"
        "    line = sys.stdin.readline()\n"
        "    return json.loads(line) if line else None\n"
        "def send(obj):\n"
        "    sys.stdout.write(json.dumps(obj) + '\\n')\n"
        "    sys.stdout.flush()\n"
        "while True:\n"
        "    msg = recv()\n"
        "    if msg is None:\n"
        "        break\n"
        "    mid = msg.get('id')\n"
        "    method = msg.get('method')\n"
        "    if method == 'initialize':\n"
        "        send({'jsonrpc':'2.0','id':mid,'result':{"
        "'protocolVersion':'2024-11-05','capabilities':{},"
        "'serverInfo':{'name': NAME,'version':'0'}}})\n"
        "    elif method == 'notifications/initialized':\n"
        "        pass\n"
        "    elif method == 'tools/list':\n"
        "        send({'jsonrpc':'2.0','id':mid,'result':{'tools': TOOLS}})\n"
        "    elif method == 'tools/call':\n"
        "        send({'jsonrpc':'2.0','id':mid,'result':{"
        "'content':[{'type':'text','text':'ok'}]}})\n"
        "    else:\n"
        "        send({'jsonrpc':'2.0','id':mid,'error':{"
        "'code':-32601,'message':'unknown'}})\n",
        encoding="utf-8",
    )
    return script


@pytest.mark.asyncio
async def test_broker_mounts_oversized_tools_list_like_core(
    broker_dir: Path, tmp_path: Path
) -> None:
    """Regression: kirocrew-core's tools/list exceeds asyncio's 64 KiB default.

    Without ``limit=READ_BUFFER_LIMIT_BYTES`` on the MCP child pipes, readline
    raises mid-frame and the broker reports a false "exited" handshake while
    leaner siblings (cron) still mount.
    """
    # A schema pushes both the MCP and bridge NDJSON frames past 64 KiB.
    padding = "x" * (70 * 1024)
    tools = [
        {
            "name": "spawn_run",
            "description": "spawn",
            "inputSchema": {"type": "object", "description": padding, "properties": {}},
        },
        {
            "name": "wait",
            "description": "short",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]
    core_script = _stdio_mcp_script(tmp_path, tools=tools, name="coreish")
    cron_tools = [
        {
            "name": "cron_list",
            "description": "list",
            "inputSchema": {"type": "object", "properties": {}},
        }
    ]
    cron_script = _stdio_mcp_script(tmp_path, tools=cron_tools, name="cronish")
    sock = broker_socket_path(artifact_dir=str(broker_dir), pid=42, nonce="biglist")
    broker = PiMcpBroker(
        [
            {
                "name": "kirocrew-core",
                "command": sys.executable,
                "args": [str(core_script)],
            },
            {
                "name": "kirocrew-cron",
                "command": sys.executable,
                "args": [str(cron_script)],
            },
        ],
        socket_path=sock,
    )
    await broker.start()
    try:
        assert "kirocrew-core" in broker._children
        assert "kirocrew-cron" in broker._children
        assert broker.tool_count == 3
        # bridge/list of the oversized index must also clear the IPC read limit.
        reader, writer = await transport.connect(sock, limit=READ_BUFFER_LIMIT_BYTES)
        try:
            writer.write(
                (
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "bridge/list", "params": {}})
                    + "\n"
                ).encode()
            )
            await writer.drain()
            listed = json.loads((await reader.readline()).decode())
            assert listed["id"] == 1
            names = {(t["server"], t["name"]) for t in listed["result"]["tools"]}
            assert ("kirocrew-core", "spawn_run") in names
            assert ("kirocrew-core", "wait") in names
            assert ("kirocrew-cron", "cron_list") in names
            # Frame really was > 64 KiB (the failure mode this pins).
            assert len(json.dumps(listed).encode()) > 65536
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await broker.stop()


@pytest.mark.parametrize("mutation", ["missing", "arguments", "tool", "replay", "denied"])
def test_broker_requires_exact_single_use_host_approval(broker_dir, mutation):
    broker = PiMcpBroker([], socket_path=str(broker_dir / "b.sock"))
    envelope = {"toolCallId": "call1", "title": "mcp__echo__ping", "input": {"x": 1}}
    if mutation != "missing":
        broker.note_permission("request1", envelope)
        if mutation == "denied":
            broker.reject_permission("request1")
        else:
            broker.approve_permission("request1", broker.stage_permission("request1"))
    if mutation == "replay":
        broker._consume_approval(
            "call1", "mcp__echo__ping", {"x": 1}, generation=broker._grant_generations.get("call1")
        )
    with pytest.raises(PermissionError, match="host-approved"):
        broker._consume_approval(
            "call1",
            "mcp__echo__other" if mutation == "tool" else "mcp__echo__ping",
            {"x": 2} if mutation == "arguments" else {"x": 1},
            generation=broker._grant_generations.get("call1"),
        )


@pytest.mark.asyncio
async def test_broker_start_cancellation_reaps_registered_children(broker_dir, monkeypatch):
    from unittest.mock import AsyncMock

    broker = PiMcpBroker([], socket_path=str(broker_dir / "b.sock"))
    child = AsyncMock()

    async def interrupted_spawn():
        broker._children["echo"] = child
        raise asyncio.CancelledError()

    monkeypatch.setattr(broker, "_spawn_children", interrupted_spawn)
    with pytest.raises(asyncio.CancelledError):
        await broker.start()
    child.kill.assert_awaited_once()
    assert not broker._children


def test_mismatched_request_does_not_consume_legitimate_approval(broker_dir):
    broker = PiMcpBroker([], socket_path=str(broker_dir / "b.sock"))
    broker.note_permission("r", {"toolCallId": "c", "title": "mcp__echo__ping", "input": {"x": 1}})
    broker.approve_permission("r", broker.stage_permission("r"))
    with pytest.raises(PermissionError):
        broker._consume_approval(
            "c", "mcp__echo__ping", {"x": 2}, generation=broker._grant_generations.get("c")
        )
    broker._consume_approval(
        "c", "mcp__echo__ping", {"x": 1}, generation=broker._grant_generations.get("c")
    )


@pytest.mark.asyncio
async def test_stderr_is_redacted_and_bounded(broker_dir, caplog):
    from types import SimpleNamespace

    from kiro_crew.acp import pi_mcp_broker

    broker = PiMcpBroker([], socket_path=str(broker_dir / "b.sock"))
    secret = "sk-ant-api03-" + "A" * 100
    stream = asyncio.StreamReader()
    stream.feed_data(("Authorization: Bearer " + secret + " " + "x" * 5000 + "\n").encode())
    stream.feed_eof()
    caplog.set_level("DEBUG", logger=pi_mcp_broker.__name__)
    await broker._drain_stderr("fixture", SimpleNamespace(stderr=stream))
    assert secret not in caplog.text
    assert len(caplog.records[-1].message) < 2200


@pytest.mark.parametrize("replacement", ["terminal", "same_request", "new_request"])
def test_late_delivery_cannot_resurrect_superseded_call(broker_dir, replacement):
    broker = PiMcpBroker([], socket_path=str(broker_dir / "b.sock"))
    envelope = {"toolCallId": "c", "title": "mcp__echo__ping", "input": {"x": 1}}
    broker.note_permission("old", envelope)
    old_generation = broker.stage_permission("old")
    if replacement == "terminal":
        broker.finish_call("c")
    else:
        new_request = "old" if replacement == "same_request" else "new"
        broker.note_permission(new_request, envelope)
        broker.reject_permission(new_request)
    broker.approve_permission("old", old_generation)
    assert not broker._approved_calls
    assert not broker._pending_approvals
    assert not broker._delivering


@pytest.mark.asyncio
async def test_delivery_timeout_invalidates_late_allow(broker_dir, monkeypatch):
    from kiro_crew.acp import pi_mcp_broker

    broker = PiMcpBroker([], socket_path=str(broker_dir / "b.sock"))
    broker.note_permission("r", {"toolCallId": "c", "title": "mcp__echo__ping", "input": {}})
    generation = broker.stage_permission("r")
    monkeypatch.setattr(pi_mcp_broker, "_DELIVERY_TIMEOUT_SECS", 0.001)
    with pytest.raises(asyncio.TimeoutError):
        await broker.wait_for_delivery("c")
    broker.approve_permission("r", generation)
    assert not broker._approved_calls
    assert not broker._approval_generations


@pytest.mark.asyncio
async def test_overlapping_call_id_cannot_consume_new_generation(broker_dir):
    broker = PiMcpBroker([], socket_path=str(broker_dir / "b.sock"))
    envelope = {"toolCallId": "c", "title": "mcp__echo__ping", "input": {}}
    broker.note_permission("old", envelope)
    old_generation = broker.stage_permission("old")
    waiter = asyncio.create_task(broker.wait_for_delivery("c"))
    await asyncio.sleep(0)
    broker.note_permission("new", envelope)
    new_generation = broker.stage_permission("new")
    broker.approve_permission("old", old_generation)
    broker.approve_permission("new", new_generation)
    observed = await waiter
    with pytest.raises(PermissionError):
        broker._consume_approval("c", "mcp__echo__ping", {}, generation=observed)
    broker._consume_approval("c", "mcp__echo__ping", {}, generation=new_generation)


@pytest.mark.asyncio
async def test_stop_attempts_every_child_and_clears_state_after_cleanup_error(broker_dir):
    from unittest.mock import AsyncMock

    socket_path = broker_dir / "b.sock"
    socket_path.touch()
    broker = PiMcpBroker([], socket_path=str(socket_path))
    first, second = AsyncMock(), AsyncMock()
    first.kill.side_effect = OSError("cleanup failed")
    broker._children = {"first": first, "second": second}
    with pytest.raises(OSError, match="cleanup failed"):
        await broker.stop()
    first.kill.assert_awaited_once()
    second.kill.assert_awaited_once()
    assert not broker._children
    assert not broker._approved_calls
    if not __import__("kiro_crew.platform_compat", fromlist=["IS_WINDOWS"]).IS_WINDOWS:
        assert not socket_path.exists()


@pytest.mark.asyncio
async def test_start_preserves_original_error_when_cleanup_fails(broker_dir, monkeypatch):
    from unittest.mock import AsyncMock

    broker = PiMcpBroker([], socket_path=str(broker_dir / "b.sock"))
    monkeypatch.setattr(
        broker, "_spawn_children", AsyncMock(side_effect=ValueError("startup failed"))
    )
    monkeypatch.setattr(broker, "stop", AsyncMock(side_effect=OSError("cleanup failed")))
    with pytest.raises(ValueError, match="startup failed"):
        await broker.start()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [asyncio.TimeoutError, asyncio.CancelledError])
@pytest.mark.parametrize("replacement", [None, "pending", "delivering", "approved"])
async def test_failed_delivery_revokes_published_generation_only(
    broker_dir, monkeypatch, failure, replacement
):
    broker = PiMcpBroker([], socket_path=str(broker_dir / "b.sock"))
    envelope = {"toolCallId": "c", "title": "mcp__echo__ping", "input": {}}
    broker.note_permission("old", envelope)
    old_generation = broker.stage_permission("old")
    new_generation = None

    async def publish_then_fail(waiter, *, timeout):
        nonlocal new_generation
        waiter.close()
        broker.approve_permission("old", old_generation)
        assert broker._grant_generations["c"] is old_generation
        if replacement is not None:
            broker.note_permission("new", envelope)
            new_generation = broker._approval_generations["new"]
            if replacement in ("delivering", "approved"):
                broker.stage_permission("new")
            if replacement == "approved":
                broker.approve_permission("new", new_generation)
        raise failure()

    monkeypatch.setattr(asyncio, "wait_for", publish_then_fail)
    with pytest.raises(failure):
        await broker.wait_for_delivery("c")
    assert old_generation not in broker._approval_generations.values()
    assert old_generation not in broker._delivery_generations.values()
    assert old_generation not in broker._grant_generations.values()
    if replacement is None:
        assert not broker._approved_calls
        assert not broker._pending_approvals
        assert not broker._delivering
    else:
        if replacement != "approved":
            assert broker._approval_generations["new"] is new_generation
            broker.approve_permission("new", new_generation)
        broker._consume_approval("c", "mcp__echo__ping", {}, generation=new_generation)


@pytest.mark.asyncio
@pytest.mark.parametrize("large_field", ["description", "inputSchema"])
async def test_large_metadata_does_not_break_bridge_list(broker_dir, tmp_path, large_field):
    """A valid 9 MiB tools/list must not overflow Pi's 8 MiB receiver."""
    from kiro_crew.acp.pi_mcp_broker import _TOOL_DESCRIPTION_MAX_CHARS

    padding = "x" * (9 * 1024 * 1024)
    tool = {"name": "large", "description": "large", "inputSchema": {"type": "object"}}
    tool[large_field] = padding if large_field == "description" else {"description": padding}
    large = _stdio_mcp_script(tmp_path, tools=[tool], name="large")
    healthy = _stdio_mcp_script(tmp_path, tools=[{"name": "ping"}], name="healthy")
    sock = broker_socket_path(artifact_dir=str(broker_dir), pid=42, nonce="metadata")
    broker = PiMcpBroker(
        [
            {"name": name, "command": sys.executable, "args": [str(script)]}
            for name, script in [("large", large), ("healthy", healthy)]
        ],
        socket_path=sock,
    )
    await broker.start()
    try:
        reader, writer = await transport.connect(sock, limit=8 * 1024 * 1024)
        try:
            writer.write(b'{"jsonrpc":"2.0","id":1,"method":"bridge/list"}\n')
            await writer.drain()
            frame = await asyncio.wait_for(reader.readline(), timeout=5)
            assert len(frame) < 8 * 1024 * 1024
            tools = json.loads(frame)["result"]["tools"]
            assert tools[-1]["name"] == "ping"
            if large_field == "description":
                assert broker.initialized_servers == {"large", "healthy"}
                assert tools[0]["description"] == padding[:_TOOL_DESCRIPTION_MAX_CHARS]
                assert tools[0]["inputSchema"] == {"type": "object"}
            else:
                assert broker.initialized_servers == {"healthy"}
                assert "size limit" in broker.server_failures["large"]
                assert len(tools) == 1
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await broker.stop()


@pytest.mark.asyncio
async def test_metadata_budget_covers_all_servers_without_losing_admitted_tools(
    broker_dir, tmp_path, monkeypatch
):
    from kiro_crew.acp import pi_mcp_broker

    monkeypatch.setattr(pi_mcp_broker, "_TOOL_INDEX_MAX_BYTES", 2000)
    schema = {"type": "object", "description": "x" * 1000}
    specs = []
    for name, tools in [
        ("first", [{"name": "one", "inputSchema": schema}]),
        ("second", [{"name": "two", "inputSchema": schema}]),
        ("last", [{"name": "three"}]),
    ]:
        script = _stdio_mcp_script(tmp_path, tools=tools, name=name)
        specs.append({"name": name, "command": sys.executable, "args": [str(script)]})
    broker = PiMcpBroker(
        specs,
        socket_path=broker_socket_path(artifact_dir=str(broker_dir), pid=42, nonce="aggregate"),
    )
    await broker.start()
    try:
        assert broker.initialized_servers == {"first", "last"}
        assert broker.tool_count == 2
        assert [(t["server"], t["name"]) for t in broker._tool_index] == [
            ("first", "one"),
            ("last", "three"),
        ]
        assert broker._tool_index[0]["inputSchema"] == schema
        assert "size limit" in broker.server_failures["second"]
    finally:
        await broker.stop()


@pytest.mark.asyncio
async def test_disabled_tool_preserves_third_party_siblings(broker_dir, tmp_path):
    script = _stdio_mcp_script(
        tmp_path, tools=[{"name": "read"}, {"name": "write"}], name="third_party"
    )
    broker = PiMcpBroker(
        [
            {
                "name": "third-party",
                "command": sys.executable,
                "args": [str(script)],
                "disabledTools": ["write"],
            }
        ],
        socket_path=broker_socket_path(artifact_dir=str(broker_dir), pid=99, nonce="deny"),
    )
    try:
        await broker.start()
        assert broker.initialized_servers == frozenset({"third-party"})
        assert [(tool["server"], tool["name"]) for tool in broker._tool_index] == [
            ("third-party", "read")
        ]
    finally:
        await broker.stop()
