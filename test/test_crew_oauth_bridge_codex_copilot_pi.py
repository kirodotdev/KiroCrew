"""Crew-owned OAuth on codex, copilot and pi (``ACP_BACKENDS_CREW_OAUTH_BRIDGE``).

Each harness's live frames (``test/fixtures/acp_frames``) must mint the approval the
gateway proxy forwards a call against, bound to the arguments the MCP server gets.
codex also needs the proxy to strip ``annotations`` from ``tools/list``: a tool
marked ``readOnlyHint`` runs unasked there, and the proxy refuses an unasked call.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_crew_oauth_mvp import _post, _proxy_app, proxy_env  # noqa: F401 -- fixture

from kiro_crew.acp import _dispatch as d
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_PI,
    ACP_BACKEND_STRANDS,
    ACP_BACKENDS_CREW_OAUTH_BRIDGE,
)
from kiro_crew.connections import crew_oauth, crew_oauth_proxy
from kiro_crew.kas_env_shim import SHIM_MODULE

FRAMES = Path(__file__).parent / "fixtures" / "acp_frames"
GITHUB_URL = "https://api.githubcopilot.com/mcp/"
SESSION = "dashboard_chat-bridge-2"
ARGS = {"owner": "octo", "repo": "hello", "issue_number": 7}


@pytest.fixture(autouse=True)
def _clean_proxy():
    crew_oauth_proxy._bridges.clear()
    crew_oauth_proxy._approvals.clear()
    yield
    crew_oauth_proxy._bridges.clear()
    crew_oauth_proxy._approvals.clear()


def _frames(rel: str) -> list[dict]:
    lines = (FRAMES / rel).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _register(server: str) -> None:
    assert crew_oauth_proxy.register_bridge(
        version=server,
        token="tok",
        session_key=SESSION,
        server=server,
        slug="github",
        mcp_url=GITHUB_URL,
    )


def _pair(rel: str) -> tuple[dict, dict]:
    frames = _frames(rel)
    ask = next(f for f in frames if f.get("method") == "session/request_permission")
    call = next(
        f
        for f in frames
        if f.get("method") == "session/update"
        and f["params"]["update"].get("sessionUpdate") == "tool_call"
    )
    return copy.deepcopy(call), copy.deepcopy(ask)


def test_the_bridge_set_is_pinned_and_never_holds_kiro_cli():
    assert ACP_BACKENDS_CREW_OAUTH_BRIDGE == {
        ACP_BACKEND_KAS,
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_COPILOT,
        ACP_BACKEND_PI,
        ACP_BACKEND_STRANDS,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_DEEPSEEK,
    }


# --- each harness's frames bind the arguments the server receives ---------------


def test_codex_binds_the_inner_arguments_its_server_received():
    """Live: codex's ``rawInput`` wraps the arguments its ``tools/call`` carried."""
    call, ask = _pair("codex/crew-oauth-bridge-args-live.jsonl")
    assert call["params"]["update"]["rawInput"] == {
        "server": "github",
        "tool": "get_issue",
        "arguments": ARGS,
    }
    assert ask["params"]["_meta"] == {"is_mcp_tool_approval": True}
    caches = dict(
        tool_input_cache={},
        shell_cache={},
        raw_params_cache={},
        mcp_server_name_cache={},
        tool_name_cache={},
        tool_input_redacted_cache={},
        diff_path_cache={},
        harness_tool_name_cache={},
    )
    d._build_tool_call_event(call["params"]["update"], **caches)
    event, _ = d.build_permission_event(
        JsonRpcMessage.from_dict(ask), harness_backend=ACP_BACKEND_CODEX, **caches
    )
    _register("github")
    assert (event.mcp_server_name, event.tool_name) == ("github", "get_issue")
    assert crew_oauth_proxy.note_approval(SESSION, event)
    assert not crew_oauth_proxy.consume_approval(SESSION, "github", "get_issue", {**ARGS, "x": 1})
    assert crew_oauth_proxy.consume_approval(SESSION, "github", "get_issue", ARGS)


@pytest.mark.parametrize(
    "raw",
    [
        {"server": "other", "tool": "get_issue", "arguments": ARGS},
        {"server": "github", "tool": "other", "arguments": ARGS},
        {"server": "github", "tool": "get_issue", "arguments": ARGS, "extra": 1},
    ],
)
def test_a_codex_wrapper_naming_another_call_is_not_unwrapped(raw):
    assert crew_oauth_proxy._call_arguments(raw, "github", "get_issue") == raw


def test_pi_binds_the_gate_envelope_input(tmp_path):
    """Live: the confirm the extension raised carries the call's arguments as ``input``."""
    rel = "pi/mcp-stdio-mount-live.jsonl"
    call, ask = _pair(rel)
    raw = ask["params"]["toolCall"]["rawInput"]
    body = json.loads(raw["message"])
    # The owner the extension now writes from its registration table
    # (``test_pi_gate_extension_mcp``); the capture predates the field.
    body["mcp"] = {"server": "probe-server", "tool": "echo"}
    raw["message"] = json.dumps(body)
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
    client._pi_gate_nonce = _frames(rel)[0]["_meta"]["gate_envelope_nonce"]
    client._pi_mcp_server_names = ("probe-server",)
    client._extract_tool_event(JsonRpcMessage.from_dict(call))
    event = client._build_permission_event(JsonRpcMessage.from_dict(ask))
    _register("probe-server")
    # The pair binds the Connection approval only; no other gate sees it as trusted.
    assert event.connection_mcp_identity == ("probe-server", "echo")
    assert event.mcp_server_name == ""
    assert crew_oauth_proxy.note_approval(SESSION, event)
    assert not crew_oauth_proxy.consume_approval(SESSION, "probe-server", "echo", {})
    assert crew_oauth_proxy.consume_approval(SESSION, "probe-server", "echo", {"text": "ping"})


@pytest.mark.parametrize(
    "mcp", [None, {"server": "not-handed-off", "tool": "echo"}, {"server": "probe-server"}]
)
def test_a_pi_envelope_without_a_handed_off_owner_mints_nothing(tmp_path, mcp):
    rel = "pi/mcp-stdio-mount-live.jsonl"
    call, ask = _pair(rel)
    raw = ask["params"]["toolCall"]["rawInput"]
    raw["message"] = json.dumps({**json.loads(raw["message"]), "mcp": mcp})
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
    client._pi_gate_nonce = _frames(rel)[0]["_meta"]["gate_envelope_nonce"]
    client._pi_mcp_server_names = ("probe-server",)
    client._extract_tool_event(JsonRpcMessage.from_dict(call))
    event = client._build_permission_event(JsonRpcMessage.from_dict(ask))
    _register("probe-server")
    assert event.connection_mcp_identity is None
    assert crew_oauth_proxy.note_approval(SESSION, event) == ""


def test_copilot_binds_the_launch_file_servers_call(tmp_path):
    """Live: the probe server of the launch file, asked through its title identity."""
    call, ask = _pair("copilot/mcp-stdio-mount-live.jsonl")
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_COPILOT)
    client._session_mcp_cache = [{"name": "probe", "type": "stdio"}]
    client._extract_tool_event(JsonRpcMessage.from_dict(call))
    event = client._build_permission_event(JsonRpcMessage.from_dict(ask))
    _register("probe")
    assert (event.mcp_server_name, event.tool_name) == ("probe", "marker")
    assert crew_oauth_proxy.note_approval(SESSION, event)
    assert crew_oauth_proxy.consume_approval(SESSION, "probe", "marker", {})


# --- tools/list loses its annotations -------------------------------------------

_TOOLS = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {"tools": [{"name": "get_issue", "annotations": {"readOnlyHint": True}}]},
}


def test_annotations_are_stripped_from_json_and_event_stream_answers():
    plain = crew_oauth_proxy.without_tool_annotations(
        json.dumps(_TOOLS).encode(), "application/json"
    )
    assert json.loads(plain)["result"]["tools"] == [{"name": "get_issue"}]
    stream = f"event: message\nid: 7\ndata: {json.dumps(_TOOLS)}\n\n".encode()
    out = crew_oauth_proxy.without_tool_annotations(stream, "text/event-stream").decode()
    assert out.startswith("event: message\nid: 7\ndata: ")
    data = json.loads(out.split("data: ", 1)[1])
    assert data["result"]["tools"] == [{"name": "get_issue"}]
    # The MCP priming event (an id, empty data) passes untouched beside the answer.
    primed = f"id: 1\ndata:\n\nevent: message\ndata: {json.dumps(_TOOLS)}\n\n".encode()
    out = crew_oauth_proxy.without_tool_annotations(primed, "text/event-stream").decode()
    assert out.startswith("id: 1\ndata:\n\n") and "readOnlyHint" not in out
    assert crew_oauth_proxy.without_tool_annotations(b"data: {cut", "text/event-stream") is None
    assert crew_oauth_proxy.without_tool_annotations(b"\xff", "application/json") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("sse", [False, True])
async def test_the_proxy_rewrites_a_tools_list_answer(proxy_env, sse):  # noqa: F811
    async def _mcp(request):
        body = await request.json()
        answer = {**_TOOLS, "id": body["id"]}
        if body["method"] == "initialize" or not sse:
            return web.json_response(answer)
        text = f"event: message\ndata: {json.dumps(answer)}\n\n"
        return web.Response(text=text, content_type="text/event-stream")

    vendor = web.Application()
    vendor.router.add_post("/mcp", _mcp)
    async with TestServer(vendor) as upstream:
        crew_oauth_proxy.register_bridge(
            version="v1",
            token="tok",
            session_key=SESSION,
            server="github",
            slug="github",
            mcp_url=str(upstream.make_url("/mcp")),
        )
        async with TestClient(TestServer(_proxy_app())) as client:
            listed = await _post(client, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
            text = await listed.text()
            init = await _post(client, {"jsonrpc": "2.0", "id": 4, "method": "initialize"})
            untouched = await init.json()
    assert listed.status == 200 and "readOnlyHint" not in text and '"get_issue"' in text
    assert listed.headers["Content-Type"].startswith(
        "text/event-stream" if sse else "application/json"
    )
    # Only a tools/list answer is rewritten.
    assert untouched["result"]["tools"][0]["annotations"] == {"readOnlyHint": True}


# --- pi's broker-only composition carries the Connection --------------------------


def _pi_client(monkeypatch, tmp_path, *, disabled=()) -> AcpClient:
    element = {"name": "github", "type": "http", "url": GITHUB_URL, "headers": []}
    projection = SimpleNamespace(
        servers=[
            element,
            {"name": "other", "type": "http", "url": "https://x.test/", "headers": []},
        ],
        disabled_tools=frozenset(disabled),
        disabled_servers=frozenset(),
        zero_tools=False,
        agent_spec={},
        allowlist=None,
    )
    import kiro_crew.acp.session_mcp as session_mcp

    monkeypatch.setattr(session_mcp, "session_mcp_projection", lambda *a, **k: projection)
    monkeypatch.setattr(crew_oauth, "enabled", lambda: True)
    monkeypatch.setattr(crew_oauth, "_chat_backend", lambda: ACP_BACKEND_PI)
    monkeypatch.setenv("KIROCREW_BOUND_PORT", "8123")
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-agent"))
    (tmp_path / "home").mkdir()
    (tmp_path / "work").mkdir()
    client = AcpClient(work_dir=tmp_path / "work", agent="kirocrew", acp_backend=ACP_BACKEND_PI)
    client._session_key = SESSION
    return client


def test_a_pi_session_gets_the_connection_bridge_and_nothing_else(monkeypatch, tmp_path):
    client = _pi_client(monkeypatch, tmp_path)
    mounts = client._mirrorless_member_mounts(frozenset())
    (shim,) = [e for e in mounts if e["name"] == "github"]
    assert shim["type"] == "stdio" and SHIM_MODULE in shim["args"]
    assert not [e for e in mounts if e["name"] == "other"]
    assert crew_oauth_proxy.is_connection_server(SESSION, "github")
    (written,) = client._crew_bridge_snapshots
    client._reset_state()
    assert not (tmp_path / "home" / "crew-oauth-bridge" / written).exists()


def test_a_pi_connection_with_a_tool_switched_off_is_withheld(monkeypatch, tmp_path):
    client = _pi_client(monkeypatch, tmp_path, disabled={("github", "delete_repo")})
    assert not [e for e in client._mirrorless_member_mounts(frozenset()) if e["name"] == "github"]
    assert not crew_oauth_proxy._bridges


def test_strands_binds_the_gate_modules_mcp_owner_and_input(tmp_path):
    """The gate module names the server it resolved (``owner: mcp:<server>``)."""
    call, ask = _pair("strands/mcp-stdio-mount-live.jsonl")
    raw = ask["params"]["toolCall"]["rawInput"]
    nonce = "0123456789abcdef0123456789abcdef"
    body = {**json.loads(raw["message"]), "nonce": nonce, "owner": "mcp:probe", "input": ARGS}
    raw["message"] = json.dumps(body)
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_STRANDS)
    client._pi_gate_nonce = nonce
    client._session_mcp_cache = [{"name": "probe", "type": "stdio"}]
    client._extract_tool_event(JsonRpcMessage.from_dict(call))
    event = client._build_permission_event(JsonRpcMessage.from_dict(ask))
    _register("probe")
    assert (event.mcp_server_name, event.tool_name) == ("probe", "codeword")
    assert crew_oauth_proxy.note_approval(SESSION, event)
    assert not crew_oauth_proxy.consume_approval(SESSION, "probe", "codeword", {})
    assert crew_oauth_proxy.consume_approval(SESSION, "probe", "codeword", ARGS)


# --- the bridge snapshot leaf -------------------------------------------------------


def test_a_bridge_snapshot_lives_in_its_own_leaf_and_the_shim_reads_it(tmp_path, monkeypatch):
    from kiro_crew import kas_env_shim
    from kiro_crew.acp import kas_env_launch

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    entry = kas_env_launch.relay_crew_connection(
        {"url": GITHUB_URL},
        session_key=SESSION,
        agent_id="a",
        server="github",
        slug="github",
        port="8123",
    )
    (name,) = kas_env_launch.batch_snapshot_names([{"mcpServers": {"github": entry}}])
    assert (home / kas_env_shim.BRIDGE_LEAF / name).is_file()
    assert not (home / kas_env_shim.SNAPSHOT_LEAF).exists()
    args = entry["args"][entry["args"].index("--session") :]
    server, doc, _env = kas_env_shim._resolve_snapshot(args, {"KIROCREW_HOME": str(home)})
    assert server == "github" and doc["connection"] == "github"
    # Only a Connection may come out of this leaf: anything else is refused.
    path = home / kas_env_shim.BRIDGE_LEAF / name
    forged = {k: v for k, v in doc.items() if k != "connection"} | {"command": "x", "args": []}
    path.write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(SystemExit):
        kas_env_shim._resolve_snapshot(args, {"KIROCREW_HOME": str(home)})
    kas_env_launch.discard_batch([], [name])
    assert not path.exists() and not crew_oauth_proxy._bridges


def test_an_enforced_child_reads_the_bridge_leaf_and_never_kas_mcp_launch(tmp_path, monkeypatch):
    from kiro_crew import sandbox
    from kiro_crew.agent_sdk import tool_gate

    assert "crew-oauth-bridge" in sandbox.crew_host_runtime_leaves()
    assert "kas-mcp-launch" not in sandbox.crew_host_runtime_leaves()
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: True, raising=False)
    for backend in (
        ACP_BACKEND_CODEX,
        ACP_BACKEND_COPILOT,
        ACP_BACKEND_PI,
        ACP_BACKEND_STRANDS,
    ):
        names = {Path(p).name for p in tool_gate.adapter_hidden_credential_dirs(backend)}
        if not names:
            # This host applies no credential mask (Windows), so no leaf is hidden.
            continue
        assert "kas-mcp-launch" in names, backend
        assert "crew-oauth-bridge" not in names, backend


# --- the bridge under the credential mask reaches the gateway by capability --------


def test_only_the_exact_bridge_launch_gets_an_agent_socket_pickup(monkeypatch, tmp_path):
    """The mask hides ``.local_secret`` and ``run/`` from an enforced child, so the
    bridge gets the pickup ``kirocrew-core`` gets; an unregistered shim does not."""
    from kiro_crew.acp import ipc_pickup, kas_env_launch
    from kiro_crew.acp.session_mcp import acp_server_element
    from kiro_crew.agent_ipc_wire import BRIDGE_PICKUP_PREFIX, GATEWAY_PID_ENV, PICKUP_ENV
    from kiro_crew.dashboard import agent_ipc

    store = agent_ipc.CapabilityStore()
    monkeypatch.setattr(agent_ipc, "STORE", store)
    monkeypatch.setattr(agent_ipc, "agent_socket_bound", lambda: True)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    entry = kas_env_launch.relay_crew_connection(
        {"url": GITHUB_URL},
        session_key=SESSION,
        agent_id="a",
        server="github",
        slug="github",
        port="8123",
    )
    real = acp_server_element("github", entry)
    # The launcher's marker words as unused arguments of some other command.
    forged = {**real, "command": "/bin/sh", "args": ["-c", "true", *real["args"]]}
    renamed = {**real, "name": "other"}

    class _Owner:
        pass

    owner = _Owner()
    bridged, *others = ipc_pickup.with_ipc_pickups(
        [real, forged, renamed],
        backend=ACP_BACKEND_CODEX,
        session_token="tok",
        runtime_pid=lambda: None,
        owner=owner,
    )
    env = {p["name"]: p["value"] for p in bridged["env"]}
    assert env[PICKUP_ENV] and env[GATEWAY_PID_ENV]
    for other in others:
        assert PICKUP_ENV not in {p["name"] for p in other["env"]}
    (pickup,) = store._pickups.values()
    assert pickup.server == f"{BRIDGE_PICKUP_PREFIX}github" and pickup.session_token == "tok"


def test_a_bridge_holding_a_pickup_sends_no_internal_secret(monkeypatch):
    from kiro_crew import agent_ipc_client, kas_connection_bridge
    from kiro_crew.config import loader_locations

    agent_ipc_client._reset_for_tests()
    monkeypatch.setattr(agent_ipc_client, "take_pickup", lambda: True)
    monkeypatch.setattr(agent_ipc_client, "current_capability", lambda port_fn: ("", ""))

    def _no_secret(port):
        raise AssertionError("the internal secret is masked; the bridge must not read it")

    monkeypatch.setattr(loader_locations, "read_local_secret", _no_secret)
    doc = {"port": 8123, "version": "v1", "bridge_token": "tok", "session": SESSION}
    bridge = kas_connection_bridge.build_bridge(doc, server="github", out=None, err=None)
    assert isinstance(bridge._opener, kas_connection_bridge._AgentSocketOpener)
    assert bridge._headers["X-Session-Key"] == SESSION
    assert "X-Internal-Secret" not in bridge._headers
    agent_ipc_client._reset_for_tests()


@pytest.mark.asyncio
async def test_a_capability_for_another_session_is_refused(proxy_env):  # noqa: F811
    from kiro_crew.dashboard import agent_ipc

    @web.middleware
    async def _grant(request, handler):
        request[agent_ipc.GRANT_KEY] = object()
        return await handler(request)

    app = _proxy_app()
    app.middlewares.append(_grant)
    crew_oauth_proxy.register_bridge(
        version="v1",
        token="tok",
        session_key=SESSION,
        server="github",
        slug="github",
        mcp_url="https://x.test/mcp",
    )
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            crew_oauth_proxy.PROXY_PATH,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                crew_oauth_proxy.BRIDGE_HEADER: "v1",
                crew_oauth_proxy.BRIDGE_TOKEN_HEADER: "tok",
                "X-Session-Key": "another-session",
            },
        )
    assert resp.status == 403
    # Refused before the pin: the bridge's own first request still pins it.
    assert crew_oauth_proxy._bridges["v1"].pid is None


@pytest.mark.asyncio
async def test_a_tools_list_stream_left_open_is_relayed_event_by_event(proxy_env):  # noqa: F811
    """A provider may keep the stream open after its answer; the answer still arrives."""
    import asyncio

    release = asyncio.Event()

    async def _mcp(request):
        body = await request.json()
        out = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await out.prepare(request)
        await out.write(
            f"id: 1\ndata:\n\ndata: {json.dumps({**_TOOLS, 'id': body['id']})}\n\n".encode()
        )
        await release.wait()  # still open, as with keep-alives
        await out.write(b": ping\n\n")
        await out.write_eof()
        return out

    vendor = web.Application()
    vendor.router.add_post("/mcp", _mcp)
    async with TestServer(vendor) as upstream:
        crew_oauth_proxy.register_bridge(
            version="v1",
            token="tok",
            session_key=SESSION,
            server="github",
            slug="github",
            mcp_url=str(upstream.make_url("/mcp")),
        )
        async with TestClient(TestServer(_proxy_app())) as client:
            try:
                resp = await _post(client, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})

                async def _until_the_tool_list() -> bytes:
                    got = b""
                    while b'"get_issue"' not in got:
                        part = await resp.content.readany()
                        assert part, f"the stream ended before its tool list: {got!r}"
                        got += part
                    return got

                # Bounded as a whole, while the provider still holds the stream open.
                seen = await asyncio.wait_for(_until_the_tool_list(), 30)
            finally:
                release.set()
            seen += await resp.content.read()
    assert resp.status == 200 and b"readOnlyHint" not in seen
    assert seen.startswith(b"id: 1\ndata:\n\n") and seen.endswith(b": ping\n\n")
