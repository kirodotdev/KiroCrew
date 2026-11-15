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
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_STRANDS,
    ACP_BACKENDS_CREW_OAUTH_BRIDGE,
)
from kiro_crew.connections import crew_oauth, crew_oauth_proxy
from kiro_crew.connections.oauth_clients import provider_for_server
from kiro_crew.kas_env_shim import SHIM_MODULE
from kiro_crew.mediated_request_capability import ApprovalMint

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
    # Each member has a frame-level proof; nothing joins without one (codex, copilot
    # and pi: ``test_crew_oauth_bridge_codex_copilot_pi.py``; goose and deepseek
    # below).
    assert {
        ACP_BACKEND_KAS,
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_DEEPSEEK,
    } <= ACP_BACKENDS_CREW_OAUTH_BRIDGE
    assert ACP_BACKEND_OPENCODE not in ACP_BACKENDS_CREW_OAUTH_BRIDGE


def test_custody_is_per_session_and_the_card_follows_the_chat_backend(monkeypatch):
    _custody(monkeypatch, chat=ACP_BACKEND_KIRO)
    entry = {"url": GITHUB_URL}
    assert crew_oauth.custody_on("github") is True
    # The Connections card: kiro-cli chats keep the native sign-in.
    assert crew_oauth.manages("github") is False
    assert crew_oauth.routed_slug("github", entry, provider_for_server) == ""
    # A bridged crewmate beside them is; kiro-cli never is.
    for backend in (
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_KAS,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_DEEPSEEK,
        ACP_BACKEND_STRANDS,
    ):
        assert crew_oauth.routed_slug("github", entry, provider_for_server, backend) == "github"
    for backend in (ACP_BACKEND_KIRO, ACP_BACKEND_OPENCODE):
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
        builtin_tools=None,
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


@pytest.mark.parametrize(
    "backend",
    [
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_COPILOT,
        ACP_BACKEND_STRANDS,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_DEEPSEEK,
    ],
)
def test_a_bridged_harness_array_carries_the_shim_element(home, monkeypatch, tmp_path, backend):
    _custody(monkeypatch, chat=ACP_BACKEND_KIRO)
    client = _client_with_array(monkeypatch, tmp_path, backend)
    (element,) = [e for e in client._resolve_session_mcp_servers() if e["name"] == "github"]
    assert element["type"] == "stdio" and "url" not in element
    assert SHIM_MODULE in element["args"]
    assert {"name": "KIROCREW_HOME", "value": str(home)} in element["env"]
    assert GITHUB_URL not in json.dumps(element)
    assert crew_oauth_proxy.is_connection_server(SESSION, "github")
    (written,) = client._crew_bridge_snapshots
    assert (home / "crew-oauth-bridge" / written).is_file()
    # The process ends: its bridge and snapshot go with it.
    client._reset_state()
    assert not crew_oauth_proxy.is_connection_server(SESSION, "github")
    assert not (home / "crew-oauth-bridge" / written).exists()


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


def _live_call(path: str, call_id: str = "") -> tuple[dict, dict | None]:
    """The live ``tool_call`` frame of *path* and its permission request, if captured."""
    frames = _frames(path)
    permission = next((f for f in frames if f.get("method") == "session/request_permission"), None)
    call_id = call_id or (permission or {}).get("params", {}).get("toolCall", {}).get("toolCallId")
    tool_call = next(
        f["params"]["update"]
        for f in frames
        if f.get("method") == "session/update"
        and f["params"]["update"].get("sessionUpdate") == "tool_call"
        and (not call_id or f["params"]["update"].get("toolCallId") == call_id)
    )
    return copy.deepcopy(tool_call), copy.deepcopy(permission)


def _dispatch_event(path: str, backend: str, args: dict):
    """A live MCP call of *path* carrying *args*, through the shared builders."""
    tool_call, permission = _live_call(path)
    tool_call["rawInput"] = args
    permission["params"]["toolCall"]["rawInput"] = args
    caches = _caches()
    d._build_tool_call_event(tool_call, **caches)
    event, _ = d.build_permission_event(
        JsonRpcMessage.from_dict(permission),
        harness_backend=backend,
        harness_mcp_servers=("crew-probe",),
        **caches,
    )
    return event


def _claude_event(args: dict, _tmp_path=None):
    """The live claude MCP call (``mcp-stdio-mount-live``) carrying *args*."""
    return _dispatch_event("claude/mcp-stdio-mount-live.jsonl", ACP_BACKEND_CLAUDE, args)


LIVE_ARGS = {"owner": "o", "issue_number": 7}


def _replay(path: str, backend: str, tmp_path, args):
    """The live ask-mode capture *path* (``crew-oauth-bridge-args-live``) replayed
    through a client, its ``rawInput`` swapped for *args* (``None``: as captured)."""
    client = AcpClient(work_dir=tmp_path, acp_backend=backend)
    client._session_key = SESSION
    client._deepseek_read_only_issue = ""
    client._session_mcp_cache = [{"name": "probe"}]
    for frame in _frames(path):
        frame = copy.deepcopy(frame)
        params = frame.get("params") or {}
        update = params.get("update") or {}
        if args is not None:
            for block in (update, params.get("toolCall") or {}):
                if "rawInput" in block:
                    block["rawInput"] = args
        if update.get("sessionUpdate") == "tool_call":
            client._extract_tool_event(JsonRpcMessage.from_dict(frame))
        elif frame.get("method") == "session/request_permission":
            return client._build_permission_event(JsonRpcMessage.from_dict(frame))
    raise AssertionError(f"{path}: no permission request")


def _goose_event(args, tmp_path):
    """goose 1.57.0 in approve mode: identity on ``_meta.goose.toolCall``, and both
    frames' ``rawInput`` is the ``arguments`` its MCP server received."""
    return _replay("goose/crew-oauth-bridge-args-live.jsonl", ACP_BACKEND_GOOSE, tmp_path, args)


def _deepseek_event(args, tmp_path):
    """dsh with Crew's gate composed: the ``tool_call`` title is the public MCP name,
    ``rawInput`` the arguments its server received; the ask names only the call id."""
    return _replay(
        "deepseek/crew-oauth-bridge-args-live.jsonl", ACP_BACKEND_DEEPSEEK, tmp_path, args
    )


@pytest.mark.parametrize(
    "build, server, tool",
    [
        pytest.param(_claude_event, "crew-probe", "crew_probe_echo", id="claude"),
        pytest.param(_goose_event, "probe", "lookup", id="goose"),
        pytest.param(_deepseek_event, "probe", "lookup", id="deepseek"),
    ],
)
@pytest.mark.parametrize("args", [{}, {"": "{}"}, {"owner": "o", "repo": "r", "issue_number": 7}])
def test_a_member_frame_binds_the_arguments_the_bridge_forwards(
    tmp_path, build, server, tool, args
):
    _register(server)
    event = build(args, tmp_path)
    assert crew_oauth_proxy.connection_identity(event) == (server, tool)
    if build is _deepseek_event:
        # A title names the call for the proxy alone: no trusted MCP identity.
        assert (event.mcp_server_name, event.connection_mcp_identity) == ("", (server, tool))
    assert ApprovalMint(SESSION, event).for_connection
    assert crew_oauth_proxy.note_approval(SESSION, event)
    assert not crew_oauth_proxy.consume_approval(SESSION, server, tool, {**args, "x": 1})
    assert crew_oauth_proxy.consume_approval(SESSION, server, tool, args)


def test_a_title_names_no_server_that_is_not_bridged(tmp_path):
    """dsh names a server only by title, so only a Crew-owned Connection is named:
    any other placed server keeps the empty identity it had."""
    event = _deepseek_event({"a": 1}, tmp_path)
    assert crew_oauth_proxy.connection_identity(event) == ("", "")
    assert crew_oauth_proxy.note_approval(SESSION, event) == ""


@pytest.mark.parametrize(
    "backend, title, placed, expected",
    [
        (ACP_BACKEND_DEEPSEEK, "mcp__github__", ("github",), ("", "")),
        (ACP_BACKEND_DEEPSEEK, "mcp__gh__x", ("gh", "gh__x"), ("", "")),
        (ACP_BACKEND_OPENCODE, "github_get_me", ("github",), ("", "")),
        (
            ACP_BACKEND_DEEPSEEK,
            "mcp__github__get_me",
            ("github",),
            ("github", "get_me"),
        ),
        (ACP_BACKEND_DEEPSEEK, "github__get_me", ("github",), ("", "")),
        (ACP_BACKEND_GOOSE, "github__get_me", ("github",), ("", "")),
    ],
)
def test_a_title_is_split_only_at_one_bridged_server(backend, title, placed, expected):
    assert d.bridged_title_identity(backend, title, placed, ("github",)) == expected


@pytest.mark.parametrize("build", [_goose_event, _deepseek_event])
def test_the_live_ask_binds_the_arguments_its_server_received(tmp_path, build):
    """As captured: the approval consumes for the arguments the stub server logged."""
    _register("probe")
    assert crew_oauth_proxy.note_approval(SESSION, build(None, tmp_path))
    assert crew_oauth_proxy.consume_approval(SESSION, "probe", "lookup", LIVE_ARGS)


def test_a_ceiling_denying_the_server_refuses_the_dsh_ask_before_any_mint(tmp_path, monkeypatch):
    """dsh's ask names only the call id, so the gate judges the title Crew rebuilds
    from the bridged pair: a ceiling (and an operator deny) on the server binds it,
    and an allow rule on it grants nothing, since the identity stays untrusted."""
    import dataclasses

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.hooks import (
        TOOL_AUTO_APPROVE,
        TOOL_DENY,
        HookManager,
        HooksConfig,
        hook_gate_kwargs,
    )
    from kiro_crew.platform import context as ctx_mod
    from kiro_crew.platform.bootstrap import build_default_context
    from kiro_crew.platform.governance import parse_policy

    _register("probe")
    event = _deepseek_event(None, tmp_path)
    assert (event.title, event.mcp_server_name, event.tool_name) == ("mcp__probe__lookup", "", "")
    gate = hook_gate_kwargs(event)
    denied = HookManager(HooksConfig(auto_deny_tools=["@probe"]))
    assert denied.on_tool_call(event.title, **gate).action == TOOL_DENY
    allowed = HookManager(HooksConfig(auto_approve_tools=["@probe/*"]))
    assert allowed.on_tool_call(event.title, **gate).action != TOOL_AUTO_APPROVE
    policy = {
        "version": 1,
        "boot": {"fail_closed": True},
        "mcp": {"mode": "deny", "deny": ["@probe"]},
    }
    base = build_default_context(KiroCrewConfig.load())
    ctx_mod.set_context(dataclasses.replace(base, governance=parse_policy(policy)))
    try:
        result = HookManager(HooksConfig(auto_approve_tools=["*"])).on_tool_call(
            event.title, **gate
        )
    finally:
        ctx_mod.reset_context()
    assert result.action == TOOL_DENY
    assert "governance" in result.reason.lower()


def test_opencode_stays_out_because_its_ask_carries_no_arguments(tmp_path):
    """opencode 1.19.1 in ask mode: both the ``tool_call`` and the request carry
    ``rawInput`` ``{}``, and the arguments the server got arrive only after the answer."""
    frames = _frames("opencode/crew-oauth-bridge-args-live.jsonl")
    ask = next(f for f in frames if f.get("method") == "session/request_permission")
    assert ask["params"]["toolCall"]["rawInput"] == {}
    after = frames[frames.index(ask) + 1 :]
    assert any(
        (f.get("params") or {}).get("update", {}).get("rawInput") == LIVE_ARGS for f in after
    )
    assert ACP_BACKEND_OPENCODE not in ACP_BACKENDS_CREW_OAUTH_BRIDGE


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
