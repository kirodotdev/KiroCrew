"""A pi or claude worker's Crew ``wait`` keeps its trusted identity (R-ERR-5 pi, claude).

pi names an MCP call only by its ``mcp__<server>__<tool>`` title, with every server
character outside ``[A-Za-z0-9_]`` written as ``_``, and sends no ``_meta``; claude
names it only in the adapter's ``_meta.claudeCode.toolName``. Neither carries a
server field, so ``ToolCallState.identity_of`` dropped the server and the stall
monitor lost the wait's declared-duration contract. The frames below are copied
from live captures (pi-acp 0.0.34, claude-agent-acp 0.94.0) of the harness
requirement measurements.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp._dispatch import parse_session_update
from kiro_crew.acp.harness_tool_names import claude_fused_mcp_tool, pi_fused_mcp_tool
from kiro_crew.acp.liveness import VERDICT_WORKING, ToolCallState
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
)
from kiro_crew.providers.acp_turns import _to_llm_event

#: The servers a stock pi or claude Crew session hands its harness.
_CREW_SERVERS = ("kirocrew-core", "kirocrew-cron")

#: pi's live ``tool_call`` for Crew's ``wait``: the server spelled ``kirocrew_core``.
_PI_WAIT = {
    "kind": "other",
    "rawInput": {"reason": "probe", "seconds": 2},
    "sessionUpdate": "tool_call",
    "status": "pending",
    "title": "mcp__kirocrew_core__wait",
    "toolCallId": "call_0_0",
}

#: claude's live ``tool_call`` and the update that carries the arguments.
_CLAUDE_WAIT = {
    "_meta": {"claudeCode": {"toolName": "mcp__kirocrew-core__wait"}},
    "content": [],
    "kind": "other",
    "name": "mcp__kirocrew-core__wait",
    "rawInput": {},
    "sessionUpdate": "tool_call",
    "status": "pending",
    "title": "mcp__kirocrew-core__wait",
    "toolCallId": "toolu_0_0",
}
_CLAUDE_WAIT_ARGS = {
    "_meta": {"claudeCode": {"toolName": "mcp__kirocrew-core__wait"}},
    "rawInput": {"reason": "probe", "seconds": 1},
    "sessionUpdate": "tool_call_update",
    "toolCallId": "toolu_0_0",
}


def _event(update: dict, backend: str):
    (ev,) = parse_session_update(update, harness_backend=backend)
    return ev


def _pi(title: str = "mcp__kirocrew_core__wait", kind: str = "other"):
    return _event({**_PI_WAIT, "title": title, "kind": kind}, ACP_BACKEND_PI)


def _claude(stamp: str | None = "mcp__kirocrew-core__wait"):
    update = {**_CLAUDE_WAIT}
    if stamp is None:
        update.pop("_meta")
    else:
        update["_meta"] = {"claudeCode": {"toolName": stamp}}
    return _event(update, ACP_BACKEND_CLAUDE)


def test_the_live_pi_wait_frame_is_a_trusted_wait():
    ev = _pi()
    assert ev.mcp_identity_trusted is False and not ev.mcp_server_name
    server, tool = ToolCallState.identity_of(ev, backend=ACP_BACKEND_PI, servers=_CREW_SERVERS)
    assert (server, tool) == ("kirocrew-core", "wait")
    assert ToolCallState(
        command=ev.tool_input, mcp_server_name=server, tool_name=tool
    ).is_trusted_wait()
    # Without the backend, or with no servers handed off, it names nothing.
    assert ToolCallState.identity_of(ev, servers=_CREW_SERVERS) == ("", "")
    assert ToolCallState.identity_of(ev, backend=ACP_BACKEND_PI) == ("", "")


def test_the_live_claude_wait_frame_is_a_trusted_wait():
    ev = _claude()
    assert ev.mcp_identity_trusted is False and not ev.mcp_server_name
    assert ev.claude_mcp_tool_name == "mcp__kirocrew-core__wait"
    server, tool = ToolCallState.identity_of(ev, backend=ACP_BACKEND_CLAUDE, servers=_CREW_SERVERS)
    assert (server, tool) == ("kirocrew-core", "wait")
    assert ToolCallState.identity_of(ev, servers=_CREW_SERVERS) == ("", "")
    assert ToolCallState.identity_of(ev, backend=ACP_BACKEND_CLAUDE) == ("", "")


@pytest.mark.parametrize(
    ("title", "kind"),
    [
        pytest.param("mcp__kirocrew_core__x__wait", "other", id="server-kirocrew_core__x"),
        pytest.param("mcp__kirocrew_core___wait", "other", id="server-kirocrew_core_"),
        pytest.param("mcp__kirocrew_core_x__wait", "other", id="server-kirocrew-core-x"),
        pytest.param("mcp__kirocrewcore__wait", "other", id="server-kirocrewcore"),
        pytest.param("mcp__kirocrew_core__wait_", "other", id="tool-trailing-underscore"),
        pytest.param("mcp__kirocrew_core__", "other", id="no-tool"),
        pytest.param("kirocrew_core__wait", "other", id="no-mcp-prefix"),
        pytest.param("mcp__kirocrew_core__wait", "execute", id="shell-command-title"),
        pytest.param("mcp__kirocrew_core__wait", "read", id="read-path-title"),
    ],
)
def test_a_look_alike_pi_title_stays_untrusted(title, kind):
    ev = _pi(title, kind)
    assert ToolCallState.identity_of(ev, backend=ACP_BACKEND_PI, servers=_CREW_SERVERS) == ("", "")


@pytest.mark.parametrize(
    "servers",
    [
        pytest.param(("kirocrew_core",), id="only-a-look-alike-handed-off"),
        pytest.param(("kirocrew-core", "kirocrew_core"), id="look-alike-beside-crew"),
        pytest.param(("kirocrew_core", "kirocrew-core"), id="look-alike-first"),
        pytest.param(("kirocrew-cron",), id="crew-core-not-handed-off"),
        pytest.param((), id="nothing-handed-off"),
    ],
)
def test_pi_reads_kirocrew_core_only_when_no_other_server_spells_the_same(servers):
    """pi writes ``kirocrew-core`` and a user's ``kirocrew_core`` the same way,
    so the title names Crew's server only when no other handed-off server does."""
    ev = _pi()
    assert ToolCallState.identity_of(ev, backend=ACP_BACKEND_PI, servers=servers) == ("", "")


@pytest.mark.parametrize(
    "stamp",
    [
        pytest.param("mcp__kirocrew_core__wait", id="server-kirocrew_core"),
        pytest.param("mcp__kirocrew-core-x__wait", id="server-kirocrew-core-x"),
        pytest.param("mcp__kirocrew-core__x__wait", id="server-kirocrew-core__x"),
        pytest.param("mcp__kirocrew-core___wait", id="server-kirocrew-core_"),
        pytest.param("mcp__Kirocrew-core__wait", id="server-case"),
        pytest.param("mcp__kirocrew-core__", id="no-tool"),
        pytest.param("Bash", id="builtin"),
        pytest.param(None, id="no-adapter-stamp"),
    ],
)
def test_a_look_alike_claude_stamp_stays_untrusted(stamp):
    ev = _claude(stamp)
    assert ToolCallState.identity_of(ev, backend=ACP_BACKEND_CLAUDE, servers=_CREW_SERVERS) == (
        "",
        "",
    )


def test_claude_reads_its_stamp_never_its_title():
    # The title alone, with no adapter stamp, names nothing.
    ev = _claude(None)
    assert ev.wire_title == "mcp__kirocrew-core__wait" and not ev.claude_mcp_tool_name
    # A user server the session did not place names nothing either.
    ev = _claude()
    assert ToolCallState.identity_of(
        ev, backend=ACP_BACKEND_CLAUDE, servers=("kirocrew-cron",)
    ) == (
        "",
        "",
    )


@pytest.mark.parametrize("backend", [ACP_BACKEND_PI, ACP_BACKEND_OPENCODE, ACP_BACKEND_CODEX])
def test_the_claude_stamp_is_kept_on_claude_alone(backend):
    assert _event(_CLAUDE_WAIT, backend).claude_mcp_tool_name == ""


@pytest.mark.parametrize("backend", [None, ACP_BACKEND_OPENCODE, ACP_BACKEND_CODEX, "kas"])
def test_the_fused_names_are_read_on_their_own_backend_alone(backend):
    assert ToolCallState.identity_of(_pi(), backend=backend, servers=_CREW_SERVERS) == ("", "")
    assert ToolCallState.identity_of(_claude(), backend=backend, servers=_CREW_SERVERS) == ("", "")


def test_each_harness_name_names_nothing_on_the_other_backend():
    assert ToolCallState.identity_of(_pi(), backend=ACP_BACKEND_CLAUDE, servers=_CREW_SERVERS) == (
        "",
        "",
    )
    assert ToolCallState.identity_of(_claude(), backend=ACP_BACKEND_PI, servers=_CREW_SERVERS) == (
        "",
        "",
    )


def test_a_shell_or_server_named_frame_is_not_reread():
    for ev, backend in ((_pi(), ACP_BACKEND_PI), (_claude(), ACP_BACKEND_CLAUDE)):
        ev.is_shell = True
        assert ToolCallState.identity_of(ev, backend=backend, servers=_CREW_SERVERS) == ("", "")
    for ev, backend in ((_pi(), ACP_BACKEND_PI), (_claude(), ACP_BACKEND_CLAUDE)):
        ev.mcp_server_name = "third-party"
        assert ToolCallState.identity_of(ev, backend=backend, servers=_CREW_SERVERS) == ("", "")


def test_the_helpers_split_only_at_the_one_join():
    assert pi_fused_mcp_tool(
        "mcp__kirocrew_core__send_message", "other", "kirocrew-core", _CREW_SERVERS
    ) == ("send_message")
    assert (
        pi_fused_mcp_tool("mcp__kirocrew_core__a__b", "other", "kirocrew-core", _CREW_SERVERS) == ""
    )
    assert claude_fused_mcp_tool(
        "mcp__kirocrew-core__send_message", "kirocrew-core", _CREW_SERVERS
    ) == ("send_message")
    assert claude_fused_mcp_tool("mcp__kirocrew-core__wa.it", "kirocrew-core", _CREW_SERVERS) == ""


def _subagent_events(update_frames: list[dict], backend: str) -> list:
    """The frames as the production ``AcpClient`` parses them for a subagent run."""
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp.types import METHOD_SESSION_UPDATE, JsonRpcMessage

    client = AcpClient(acp_backend=backend)
    out = []
    for update in update_frames:
        msg = JsonRpcMessage(
            method=METHOD_SESSION_UPDATE, params={"sessionId": "s-1", "update": update}
        )
        if update["sessionUpdate"] == "tool_call":
            parsed = [client._extract_tool_event(msg)]
        else:
            parsed = [
                client._extract_tool_call_update(msg),
                client._extract_tool_call_refinement(msg),
            ]
        out += [ev for ev in parsed if ev is not None]
    return out


def _feed(info, events, backend: str, servers: tuple[str, ...]) -> None:
    """Feed *events* to the subagent hooks in the order ``run_turn`` calls them."""
    from kiro_crew.acp.types import EVENT_TOOL_CALL, EVENT_TOOL_CALL_UPDATE
    from kiro_crew.subagent import SubagentManager

    for ev in events:
        if ev.kind == EVENT_TOOL_CALL:
            SubagentManager._note_tool_dispatch(info, ev, backend, servers)
        elif ev.kind == EVENT_TOOL_CALL_UPDATE:
            SubagentManager._note_tool_update(info, ev)


def _claude_frames(seconds: int) -> list[dict]:
    return [
        _CLAUDE_WAIT,
        {**_CLAUDE_WAIT_ARGS, "rawInput": {"reason": "probe", "seconds": seconds}},
    ]


def _pi_frames(seconds: int) -> list[dict]:
    return [{**_PI_WAIT, "rawInput": {"reason": "probe", "seconds": seconds}}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "frames"), [(ACP_BACKEND_PI, _pi_frames), (ACP_BACKEND_CLAUDE, _claude_frames)]
)
async def test_a_subagent_wait_is_held_to_its_declared_seconds(backend, frames):
    """220s into a 270s wait, the reaper reads WORKING and never badges it stalled."""
    from kiro_crew.subagent import SubagentInfo, SubagentManager

    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock(), stall_idle_secs=120)
    mgr._fire_event = AsyncMock()  # type: ignore[method-assign]
    now = 1_000.0
    info = SubagentInfo(id="a1b2c3d4", task="t", agent="")
    info.turns, info._pid, info.last_activity = 1, 4242, now - 220
    _feed(info, _subagent_events(frames(270), backend), backend, _CREW_SERVERS)
    assert info._inflight_tool.is_trusted_wait()
    info._inflight_tool.dispatch_ts -= 220
    verdict, evidence = await mgr._stall_verdict(info)
    assert verdict == VERDICT_WORKING and "declared 270s" in evidence, evidence
    with patch("kiro_crew.subagent.record_slow_command"):
        await mgr._maybe_flag_stall("a1b2c3d4", info, now)
        await mgr._maybe_flag_stall("a1b2c3d4", info, now + 60)
    assert info.stalled is False
    mgr._fire_event.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "servers", "trusted"),
    [
        (ACP_BACKEND_PI, ("kirocrew-core",), True),
        (ACP_BACKEND_PI, ("kirocrew-core", "kirocrew_core"), False),
        (ACP_BACKEND_CLAUDE, ("kirocrew-core",), True),
        (ACP_BACKEND_CLAUDE, ("kirocrew-cron",), False),
    ],
)
async def test_the_subagent_run_loop_reads_the_servers_off_its_provider(backend, servers, trusted):
    """``run_turn`` hands the provider's harness and the servers it was handed to
    the dispatch snapshot, so the reaper judges the live wait."""
    from subagent_helpers import _mock_ctx_builder_auto_spawn, _mock_sessions

    from kiro_crew.execution_context import execution_for_store
    from kiro_crew.subagent import SubagentInfo, SubagentManager

    info = SubagentInfo(
        execution_context=execution_for_store(""),
        id="pc01",
        task="test",
        parent_session_key="slack:C:T",
    )
    frames = _pi_frames(270) if backend == ACP_BACKEND_PI else _claude_frames(270)
    events = _subagent_events(frames, backend)
    seen: dict[str, object] = {}

    async def _stream(*_a, **_kw):  # type: ignore[no-untyped-def]
        # Through the provider's own copy, as the run loop receives them.
        for ev in events:
            yield _to_llm_event(ev)
        seen["judged"] = info._inflight_tool

    sessions = _mock_sessions()
    provider = AsyncMock()
    provider.client = SimpleNamespace(backend=backend, _harness_mcp_server_names=lambda: servers)
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = lambda: 0.0
    provider.stream = MagicMock(side_effect=lambda *a, **kw: _stream())
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    ctx = _mock_ctx_builder_auto_spawn()
    ctx.hooks.auto_approve_subagent_tools = True
    manager = SubagentManager(sessions=sessions, ctx_builder=ctx)
    manager.hook_store = MagicMock()
    manager.hook_store.fire = AsyncMock()
    manager._log_spawned(info)

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        await manager._run_inner(info, "subagent:pc01")

    judged = seen["judged"]
    assert isinstance(judged, ToolCallState)
    assert judged.is_trusted_wait() is trusted
    if trusted:
        assert '"seconds": 270' in judged.command
