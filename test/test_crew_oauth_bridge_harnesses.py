"""Crew-owned OAuth on the array harnesses (``dashboard.feature_previews.crew_oauth``).

Custody is decided per session from the session's backend
(``ACP_BACKENDS_CREW_OAUTH_BRIDGE``), never for kiro-cli; the Connections card keeps
reading the chat backend. A bridged session's ``session/new`` array carries the
gateway shim instead of the vendor URL, and each member harness's own permission
frames (the live captures under ``test/fixtures/acp_frames``) mint the approval the
gateway proxy forwards a call against, bound to the arguments the MCP server gets.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.acp import _dispatch as d
from kiro_crew.acp import client as client_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import OUTCOME_SELECTED, JsonRpcMessage
from kiro_crew.agent_sdk import backends
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_STRANDS,
    ACP_BACKENDS_CREW_OAUTH_BRIDGE,
)
from kiro_crew.connections import crew_oauth, crew_oauth_proxy
from kiro_crew.connections.oauth_clients import provider_for_server
from kiro_crew.kas_env_shim import SHIM_MODULE

FRAMES = Path(__file__).parent / "fixtures" / "acp_frames"
GITHUB_URL = "https://api.githubcopilot.com/mcp/"
SESSION = "dashboard_chat-bridge-1"


@pytest.fixture(autouse=True)
def _clean_proxy():
    crew_oauth_proxy._bridges.clear()
    crew_oauth_proxy._approvals.clear()
    yield
    crew_oauth_proxy._bridges.clear()
    crew_oauth_proxy._approvals.clear()


@pytest.fixture
def home(tmp_path, monkeypatch):
    data_home = tmp_path / "crew-home"
    data_home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(data_home))
    return data_home


def _custody(
    monkeypatch, *, on: bool = True, chat: str = ACP_BACKEND_KAS, grant: bool | None = True
) -> None:
    monkeypatch.setattr(crew_oauth, "enabled", lambda: on)
    monkeypatch.setattr(crew_oauth, "_chat_backend", lambda: chat)
    monkeypatch.setattr(crew_oauth, "holds_grant", lambda slug: grant)
    monkeypatch.setenv("KIROCREW_BOUND_PORT", "8123")


def _frames(path: str) -> list[dict]:
    lines = (FRAMES / path).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[1:] if line.strip()]


def _register(server: str, session: str = SESSION, version: str = "v1") -> None:
    assert crew_oauth_proxy.register_bridge(
        version=version,
        token="tok",
        session_key=session,
        server=server,
        slug="github",
        mcp_url=GITHUB_URL,
    )


# --- custody gate -------------------------------------------------------------


def test_kiro_cli_never_reaches_the_bridge():
    assert ACP_BACKEND_KIRO not in ACP_BACKENDS_CREW_OAUTH_BRIDGE
    assert crew_oauth.serves_backend(ACP_BACKEND_KIRO) is False
    assert crew_oauth.serves_backend("kiro") is False
    # Each member has a frame-level proof below; nothing joins without one.
    assert ACP_BACKENDS_CREW_OAUTH_BRIDGE == {ACP_BACKEND_KAS, ACP_BACKEND_CLAUDE}


def test_custody_is_per_session_and_the_card_follows_the_chat_backend(monkeypatch):
    _custody(monkeypatch, chat=ACP_BACKEND_KIRO)
    entry = {"url": GITHUB_URL}
    assert crew_oauth.custody_on("github") is True
    # The Connections card: kiro-cli chats keep the native sign-in.
    assert crew_oauth.manages("github") is False
    assert crew_oauth.routed_slug("github", entry, provider_for_server) == ""
    # A claude or KAS crewmate beside them is bridged; kiro-cli never is.
    for backend in (ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS):
        assert crew_oauth.routed_slug("github", entry, provider_for_server, backend) == "github"
    for backend in (
        ACP_BACKEND_KIRO,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_DEEPSEEK,
        ACP_BACKEND_STRANDS,
    ):
        assert crew_oauth.routed_slug("github", entry, provider_for_server, backend) == ""
    _custody(monkeypatch, on=False, chat=ACP_BACKEND_CLAUDE)
    assert crew_oauth.routed_slug("github", entry, provider_for_server, ACP_BACKEND_CLAUDE) == ""


@pytest.mark.parametrize(
    "chat, grant, bridged",
    [
        (ACP_BACKEND_KIRO, True, True),
        (ACP_BACKEND_KIRO, False, False),
        (ACP_BACKEND_KIRO, None, False),
        (ACP_BACKEND_CLAUDE, False, True),
    ],
)
def test_a_session_is_bridged_only_when_crew_can_hold_the_grant(monkeypatch, chat, grant, bridged):
    """Under kiro-cli chats the card offers only the native Connect, so a crewmate
    is bridged only once Crew already holds the pair; else it keeps today's path."""
    _custody(monkeypatch, chat=chat, grant=grant)
    routed = crew_oauth.routed_slug(
        "github", {"url": GITHUB_URL}, provider_for_server, ACP_BACKEND_CLAUDE
    )
    assert (routed == "github") is bridged


def test_chat_sessions_are_bridged_only_on_a_bridged_chat_backend(monkeypatch):
    for chat, on, expected in (
        (ACP_BACKEND_CLAUDE, True, True),
        (ACP_BACKEND_KAS, True, True),
        (ACP_BACKEND_KIRO, True, False),
        (ACP_BACKEND_CLAUDE, False, False),
    ):
        _custody(monkeypatch, on=on, chat=chat)
        assert crew_oauth.bridges_chat_sessions() is expected


@pytest.mark.parametrize(
    "chat, kiro_selectable, strips",
    [
        (ACP_BACKEND_KAS, True, True),
        (ACP_BACKEND_CLAUDE, True, False),
        (ACP_BACKEND_KIRO, True, False),
        (ACP_BACKEND_CLAUDE, False, True),
    ],
)
def test_the_spec_secret_goes_only_when_no_kiro_cli_session_reads_it(
    monkeypatch, chat, kiro_selectable, strips
):
    _custody(monkeypatch, chat=chat)
    selectable = {ACP_BACKEND_KAS, ACP_BACKEND_CLAUDE} | ({""} if kiro_selectable else set())
    monkeypatch.setattr(backends, "selectable_backends", lambda: frozenset(selectable))
    assert crew_oauth.strips_spec_secret("github") is strips
    _custody(monkeypatch, on=False, chat=chat)
    assert crew_oauth.strips_spec_secret("github") is False


# --- the session/new array ------------------------------------------------------


def _client_with_array(monkeypatch, tmp_path, backend: str) -> AcpClient:
    """A client whose mirror projects one remote GitHub element, as every mirror does."""
    projection = SimpleNamespace(
        params={
            "mcpServers": [{"name": "github", "type": "http", "url": GITHUB_URL, "headers": []}]
        },
        denied_tools=frozenset(),
        agent_spec={},
        unhonoured_servers=(),
        harness_deny_rules=(),
        zero_tools=False,
        derived_spec_snapshot=None,
        restricted_servers=frozenset(),
        disabled_servers=frozenset(),
    )
    mirror = SimpleNamespace(session_projection=lambda *a, **k: projection)
    monkeypatch.setattr(client_mod, "mirror_for", lambda _backend: mirror)
    monkeypatch.setattr(client_mod, "injection_server_names", lambda *a, **k: frozenset())
    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=backend)
    client._session_key = SESSION
    return client


def test_a_bridged_harness_array_carries_the_shim_element(home, monkeypatch, tmp_path):
    _custody(monkeypatch, chat=ACP_BACKEND_KIRO)
    client = _client_with_array(monkeypatch, tmp_path, ACP_BACKEND_CLAUDE)
    (element,) = [e for e in client._resolve_session_mcp_servers() if e["name"] == "github"]
    assert element["type"] == "stdio" and "url" not in element
    assert SHIM_MODULE in element["args"]
    assert {"name": "KIROCREW_HOME", "value": str(home)} in element["env"]
    assert GITHUB_URL not in json.dumps(element)
    assert crew_oauth_proxy.is_connection_server(SESSION, "github")
    (written,) = client._crew_bridge_snapshots
    assert (home / "kas-mcp-launch" / written).is_file()
    # The process ends: its bridge and snapshot go with it.
    client._reset_state()
    assert not crew_oauth_proxy.is_connection_server(SESSION, "github")
    assert not (home / "kas-mcp-launch" / written).exists()


@pytest.mark.parametrize(
    "backend", [ACP_BACKEND_OPENCODE, ACP_BACKEND_DEEPSEEK, ACP_BACKEND_STRANDS]
)
def test_an_unbridged_harness_keeps_the_vendor_element(home, monkeypatch, tmp_path, backend):
    _custody(monkeypatch, chat=ACP_BACKEND_CLAUDE)
    client = _client_with_array(monkeypatch, tmp_path, backend)
    (element,) = [e for e in client._resolve_session_mcp_servers() if e["name"] == "github"]
    assert element["url"] == GITHUB_URL and element["type"] == "http"
    assert not crew_oauth_proxy._bridges


# --- each harness's frames mint the approval for the forwarded arguments --------


def _caches() -> dict:
    return dict(
        tool_input_cache={},
        shell_cache={},
        raw_params_cache={},
        mcp_server_name_cache={},
        tool_name_cache={},
        tool_input_redacted_cache={},
        diff_path_cache={},
        harness_tool_name_cache={},
    )


def _claude_event(args: dict):
    """The live claude MCP call (``mcp-stdio-mount-live``) carrying *args*."""
    frames = _frames("claude/mcp-stdio-mount-live.jsonl")
    permission = next(f for f in frames if f.get("method") == "session/request_permission")
    call_id = permission["params"]["toolCall"]["toolCallId"]
    tool_call = next(
        f["params"]["update"]
        for f in frames
        if f.get("method") == "session/update"
        and f["params"]["update"].get("sessionUpdate") == "tool_call"
        and f["params"]["update"].get("toolCallId") == call_id
    )
    tool_call, permission = copy.deepcopy(tool_call), copy.deepcopy(permission)
    tool_call["rawInput"] = args
    permission["params"]["toolCall"]["rawInput"] = args
    caches = _caches()
    d._build_tool_call_event(tool_call, **caches)
    event, _ = d.build_permission_event(
        JsonRpcMessage.from_dict(permission),
        harness_backend=ACP_BACKEND_CLAUDE,
        harness_mcp_servers=("crew-probe",),
        **caches,
    )
    return event


@pytest.mark.parametrize(
    "build, server, tool",
    [
        pytest.param(_claude_event, "crew-probe", "crew_probe_echo", id="claude"),
    ],
)
@pytest.mark.parametrize("args", [{}, {"owner": "o", "repo": "r", "issue_number": 7}])
def test_a_member_frame_binds_the_arguments_the_bridge_forwards(build, server, tool, args):
    _register(server)
    event = build(args)
    assert (event.mcp_server_name, event.tool_name) == (server, tool)
    assert crew_oauth_proxy.note_approval(SESSION, event)
    assert not crew_oauth_proxy.consume_approval(SESSION, server, tool, {**args, "x": 1})
    assert crew_oauth_proxy.consume_approval(SESSION, server, tool, args)


def test_opencode_and_deepseek_frames_name_no_server_so_they_stay_out(tmp_path):
    """Neither harness's permission request names a trusted MCP server: no approval
    could ever be minted, so a bridged call would always be refused."""
    # opencode: the live MCP tool_call pair, then a request in its captured shape.
    updates = [
        f
        for f in _frames("opencode/mcp-directive-call-live.jsonl")
        if f.get("method") == "session/update"
        and f["params"]["update"].get("sessionUpdate") in ("tool_call", "tool_call_update")
    ][:2]
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
    client._session_mcp_cache = [{"name": "kirocrew-core"}]
    for update in updates:
        client._extract_tool_event(JsonRpcMessage.from_dict(copy.deepcopy(update)))
    first = updates[0]["params"]["update"]
    request = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "session/request_permission",
        "params": {
            "sessionId": "opencode-session-1",
            "options": [{"kind": "allow_once", "name": "Allow once", "optionId": "once"}],
            "toolCall": {
                "kind": "other",
                "rawInput": updates[1]["params"]["update"]["rawInput"],
                "status": "pending",
                "title": first["title"],
                "toolCallId": first["toolCallId"],
            },
        },
    }
    opencode = client._build_permission_event(JsonRpcMessage.from_dict(request))
    # deepseek: the live mcp__probe__read pair.
    frames = _frames("deepseek/side-read-only-live.jsonl")
    call = next(
        f
        for f in frames
        if f.get("method") == "session/update"
        and f["params"]["update"].get("title") == "mcp__probe__read"
    )
    call_id = call["params"]["update"]["toolCallId"]
    ask = next(
        f
        for f in frames
        if f.get("method") == "session/request_permission"
        and f["params"]["toolCall"]["toolCallId"] == call_id
    )
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_DEEPSEEK)
    client._deepseek_read_only_issue = ""
    client._session_mcp_cache = [{"name": "probe"}]
    client._extract_tool_event(JsonRpcMessage.from_dict(call))
    deepseek = client._build_permission_event(JsonRpcMessage.from_dict(ask))
    for event, server in ((opencode, "kirocrew-core"), (deepseek, "probe")):
        _register(server, version=server)
        assert event.mcp_server_name == ""
        assert crew_oauth_proxy.note_approval(SESSION, event) == ""


# --- allow-always ---------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bridged, sent", [(True, "allow-once"), (False, "allow-with-updates")])
async def test_allow_always_on_a_bridged_server_is_answered_once(tmp_path, bridged, sent):
    """An allow-always would stop the harness asking, and the proxy refuses an unasked call."""
    if bridged:
        _register("crew-probe")
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    client._session_key = SESSION
    client._permission_gate_events[42] = _claude_event({})
    client._permission_options[42] = {"once": "allow-once", "always": "allow-with-updates"}
    sent_with: list = []

    async def _send(request_id, payload, **recheck):
        # The transport runs the mint under its write lock, right before the frame.
        if "before_write" in recheck:
            recheck["before_write"]()
        sent_with.append(payload)

    client._send_response = AsyncMock(side_effect=_send)
    assert await client.approve_tool(42, always=True) is True
    assert sent_with == [{"outcome": {"outcome": OUTCOME_SELECTED, "optionId": sent}}]
    if bridged:
        assert crew_oauth_proxy.consume_approval(SESSION, "crew-probe", "crew_probe_echo", {})


def test_a_claude_session_asks_for_every_bridged_tool_despite_an_allow_rule(tmp_path):
    """A user ``mcp__github__*`` allow would pre-approve the call with no permission
    request, so no approval is minted and the proxy refuses it; ask outranks allow."""
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    client._session_key = SESSION
    client._session_mcp_cache = [{"name": "github", "type": "stdio"}, {"name": "other"}]
    assert "settings" not in client._claude_session_meta()["claudeCode"]["options"]
    _register("github")
    meta = client._claude_session_meta()["claudeCode"]["options"]
    assert meta["settings"]["permissions"]["ask"] == ["mcp__github"]
    assert meta["allowDangerouslySkipPermissions"] is False


def test_a_reseed_after_session_new_keeps_the_placed_roster(tmp_path):
    """A settings re-seed after ``session/new`` drops the projection cache; the live
    session still runs the array it was sent, so a bridged call keeps its identity."""
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    client._session_mcp_cache = [{"name": "crew-probe", "type": "stdio"}]
    client._begin_session_report(list(client._session_mcp_cache))
    client._invalidate_session_mcp_projection()
    assert client._session_mcp_cache is None
    assert client._placed_mcp_server_names() == ("crew-probe",)
