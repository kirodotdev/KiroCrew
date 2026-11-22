"""An opencode worker's Crew ``wait`` keeps its trusted identity (R-ERR-5 opencode).

opencode names an MCP call only by its fused ``<server>_<tool>`` title and sends
no server field, so ``ToolCallState.identity_of`` dropped the server and the stall
monitor lost the wait's declared-duration contract. The live capture
``test/fixtures/acp_frames/opencode/crew-wait-live.jsonl`` is the evidence.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp._dispatch import parse_session_update
from kiro_crew.acp.harness_tool_names import opencode_fused_mcp_tool
from kiro_crew.acp.liveness import VERDICT_WORKING, ToolCallState
from kiro_crew.acp_backends import ACP_BACKEND_CODEX, ACP_BACKEND_MIMOCODE, ACP_BACKEND_OPENCODE

_FIXTURE = Path(__file__).parent / "fixtures" / "acp_frames" / "opencode" / "crew-wait-live.jsonl"


def _frames() -> list[dict]:
    lines = _FIXTURE.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[1:] if line.strip()]


def _updates() -> list[dict]:
    return [
        f["params"]["update"]
        for f in _frames()
        if f.get("method") == "session/update" and "update" in f.get("params", {})
    ]


def _wait_tool_call() -> dict:
    (update,) = [u for u in _updates() if u.get("sessionUpdate") == "tool_call"]
    return update


def _event(update: dict):
    (ev,) = parse_session_update(update)
    return ev


def test_the_live_opencode_wait_frame_is_a_trusted_wait():
    update = _wait_tool_call()
    assert update["title"] == "kirocrew-core_wait" and "_meta" not in update
    ev = _event(update)
    assert ev.mcp_identity_trusted is False and not ev.mcp_server_name

    server, tool = ToolCallState.identity_of(ev, backend=ACP_BACKEND_OPENCODE)
    assert (server, tool) == ("kirocrew-core", "wait")
    state = ToolCallState(command=ev.tool_input, mcp_server_name=server, tool_name=tool)
    assert state.is_trusted_wait()
    # Without the backend the frame names nothing, as before.
    assert ToolCallState.identity_of(ev) == ("", "")


def _retitled(title: str, kind: str = "other") -> dict:
    return {**_wait_tool_call(), "title": title, "kind": kind}


@pytest.mark.parametrize(
    "update",
    [
        pytest.param(_retitled("kirocrew-core_x_wait"), id="server-kirocrew-core_x"),
        pytest.param(_retitled("kirocrew-core_x___wait"), id="server-kirocrew-core_x_"),
        pytest.param(_retitled("kirocrew-core-x_wait"), id="server-kirocrew-core-x"),
        pytest.param(_retitled("kirocrew_core_wait"), id="server-kirocrew"),
        pytest.param(_retitled("kirocrew-corex_wait"), id="server-kirocrew-corex"),
        pytest.param(_retitled("other_wait"), id="another-server"),
        pytest.param(_retitled("wait"), id="bare-wait"),
        pytest.param(_retitled("kirocrew-core_wait extra"), id="trailing-text"),
        pytest.param(_retitled("kirocrew-core_wait", kind="think"), id="builtin-kind"),
    ],
)
def test_a_look_alike_opencode_title_stays_untrusted(update):
    ev = _event(update)
    assert ToolCallState.identity_of(ev, backend=ACP_BACKEND_OPENCODE) == ("", "")


@pytest.mark.parametrize("backend", [None, "", ACP_BACKEND_CODEX, ACP_BACKEND_MIMOCODE, "kas"])
def test_the_fused_title_is_read_on_opencode_alone(backend):
    ev = _event(_wait_tool_call())
    assert ToolCallState.identity_of(ev, backend=backend) == ("", "")


def test_a_shell_or_server_named_frame_is_not_reread():
    ev = _event(_wait_tool_call())
    ev.is_shell = True
    assert ToolCallState.identity_of(ev, backend=ACP_BACKEND_OPENCODE) == ("", "")
    ev = _event(_wait_tool_call())
    ev.mcp_server_name = "third-party"
    assert ToolCallState.identity_of(ev, backend=ACP_BACKEND_OPENCODE) == ("", "")


def test_the_split_is_unique_only_with_no_underscore_in_either_half():
    assert opencode_fused_mcp_tool("kirocrew-core_wait", "other", "kirocrew-core") == "wait"
    assert opencode_fused_mcp_tool("kirocrew-core_monitor_start", "other", "kirocrew-core") == ""
    assert opencode_fused_mcp_tool("a_b_wait", "other", "a_b") == ""
    assert opencode_fused_mcp_tool("kirocrew-core_wa.it", "other", "kirocrew-core") == ""


def _client_events(backend: str, seconds: int | None = None) -> list:
    """The fixture's tool frames as the production ``AcpClient`` parses them.

    A subagent on opencode runs on ``AcpClient`` (opencode is not in
    ``ACP_BACKENDS_ACP_RUNTIME``), so this is the parser its run loop reads.
    """
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp.types import METHOD_SESSION_UPDATE, JsonRpcMessage

    client = AcpClient(acp_backend=backend)
    out = []
    for update in _updates():
        if update.get("sessionUpdate") not in ("tool_call", "tool_call_update"):
            continue
        if seconds is not None and update.get("rawInput"):
            update = {**update, "rawInput": {**update["rawInput"], "seconds": seconds}}
        msg = JsonRpcMessage(
            method=METHOD_SESSION_UPDATE,
            params={"sessionId": "opencode-session-1", "update": update},
        )
        if update["sessionUpdate"] == "tool_call":
            parsed = [client._extract_tool_event(msg)]
        else:
            parsed = [client._extract_tool_call_update(msg)]
            parsed.append(client._extract_tool_call_refinement(msg))
        out += [ev for ev in parsed if ev is not None]
    return out


def _permission_event(call_id: str):
    """opencode's permission request for the same call: a title, no wire title."""
    from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, AcpEvent

    return AcpEvent(kind=EVENT_PERMISSION_REQUEST, tool_call_id=call_id, title="kirocrew-core_wait")


def _run(info, events, backend, approval_secs: float = 0.0) -> None:
    """Feed *events* to the subagent hooks in the order ``run_turn`` calls them.

    *approval_secs* is how long the permission request waited on a person
    before opencode started the call.
    """
    from kiro_crew.acp.types import EVENT_TOOL_CALL, EVENT_TOOL_CALL_UPDATE
    from kiro_crew.subagent import SubagentManager

    (tool_call,) = [ev for ev in events if ev.kind == EVENT_TOOL_CALL]
    in_progress = next(ev for ev in events if ev.kind == EVENT_TOOL_CALL_UPDATE)
    SubagentManager._note_tool_dispatch(info, tool_call, backend)
    SubagentManager._note_tool_dispatch(info, _permission_event(tool_call.tool_call_id), backend)
    info._inflight_tool.dispatch_ts -= approval_secs
    SubagentManager._note_tool_update(info, in_progress)


@pytest.mark.asyncio
async def test_an_opencode_subagent_wait_is_held_to_its_declared_seconds():
    """220s into a 270s wait, the reaper reads WORKING and never badges it
    stalled, through the permission request opencode sends in between, even
    when a person took 300s to approve it."""
    from kiro_crew.subagent import SubagentInfo, SubagentManager

    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock(), stall_idle_secs=120)
    mgr._fire_event = AsyncMock()
    now = 1_000.0
    info = SubagentInfo(id="a1b2c3d4", task="t", agent="")
    info.turns, info._pid, info.last_activity = 1, 4242, now - 220
    events = _client_events(ACP_BACKEND_OPENCODE, seconds=270)
    _run(info, events, ACP_BACKEND_OPENCODE, approval_secs=300)
    assert info._inflight_tool.is_trusted_wait()
    info._inflight_tool.dispatch_ts -= 220
    verdict, evidence = await mgr._stall_verdict(info)
    assert verdict == VERDICT_WORKING and "declared 270s" in evidence, evidence
    with patch("kiro_crew.subagent.record_slow_command"):
        await mgr._maybe_flag_stall("a1b2c3d4", info, now)
        await mgr._maybe_flag_stall("a1b2c3d4", info, now + 60)
    assert info.stalled is False
    mgr._fire_event.assert_not_called()


def test_another_backends_subagent_keeps_the_frame_untrusted():
    from kiro_crew.subagent import SubagentInfo

    info = SubagentInfo(id="a1b2c3d4", task="t", agent="")
    _run(info, _client_events(ACP_BACKEND_CODEX), ACP_BACKEND_CODEX)
    assert not info._inflight_tool.is_trusted_wait()


def test_a_refinement_cannot_stretch_seconds_already_read():
    state = ToolCallState(
        command='{"seconds": 5}', mcp_server_name="kirocrew-core", tool_name="wait"
    )
    state.refine_wait_input('{"seconds": 3600}')
    assert state.command == '{"seconds": 5}'
    untrusted = ToolCallState(command="{}")
    untrusted.refine_wait_input('{"seconds": 3600}')
    assert untrusted.command == "{}"


def test_only_an_input_naming_seconds_restarts_the_wait_clock():
    state = ToolCallState(command="{}", mcp_server_name="kirocrew-core", tool_name="wait")
    assert state.refine_wait_input('{"reason": "x"}', now=50.0) is False
    assert state.dispatch_ts == 0.0
    assert state.refine_wait_input('{"seconds": 270}', now=50.0) is True
    assert state.dispatch_ts == 50.0
    assert state.refine_wait_input('{"seconds": 270}', now=99.0) is False
    assert state.dispatch_ts == 50.0


@pytest.mark.asyncio
@pytest.mark.parametrize(("backend", "trusted"), [(ACP_BACKEND_OPENCODE, True), ("kiro", False)])
async def test_the_subagent_run_loop_reads_the_harness_off_its_provider(backend, trusted):
    """``run_turn`` hands the provider's harness to the dispatch snapshot and the
    refinement to ``_note_tool_update``, so the reaper judges the live wait."""
    from types import SimpleNamespace

    from subagent_helpers import _mock_ctx_builder_auto_spawn, _mock_sessions

    from kiro_crew.execution_context import execution_for_store
    from kiro_crew.subagent import SubagentInfo, SubagentManager

    info = SubagentInfo(
        execution_context=execution_for_store(""),
        id="oc01",
        task="test",
        parent_session_key="slack:C:T",
    )
    events = _client_events(ACP_BACKEND_OPENCODE, seconds=270)
    seen: dict[str, object] = {}

    async def _stream(*_a, **_kw):  # type: ignore[no-untyped-def]
        for ev in events[:2]:
            yield ev
        seen["judged"] = info._inflight_tool

    sessions = _mock_sessions()
    provider = AsyncMock()
    provider.client = SimpleNamespace(backend=backend)
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
        await manager._run_inner(info, "subagent:oc01")

    judged = seen["judged"]
    assert isinstance(judged, ToolCallState)
    assert judged.is_trusted_wait() is trusted
    if trusted:
        assert '"seconds": 270' in judged.command
