"""``AcpSessionHandle._dispatch_events``: the other session updates.

Agent switches, MCP OAuth and server initialization, unknown server requests, tool
call updates, usage, cost and credits metadata, and sub-agent list and activity.
"""

import asyncio
import json

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _stub_sel_for_permission_tests  # noqa: F401
from acp_runtime_helpers import (
    _await_routed,
    _feed,
    _make_runtime,
    _register,
    _start_reader,
    _stop_reader,
)

from kiro_crew.acp.runtime import AcpSessionHandle
from kiro_crew.acp.types import JSONRPC_METHOD_NOT_FOUND, METHOD_SESSION_UPDATE, JsonRpcMessage
from kiro_crew.testing.wait import await_until


@pytest.mark.asyncio
async def test_dispatch_agent_switched():
    """Agent switched notification yields EVENT_AGENT_SWITCHED."""
    from kiro_crew.acp.types import EVENT_AGENT_SWITCHED, METHOD_AGENT_SWITCHED

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_AGENT_SWITCHED,
                "params": {
                    "sessionId": "sA",
                    "agentName": "kirocrew-lite",
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        sw = [e for e in events if e.kind == EVENT_AGENT_SWITCHED]
        assert len(sw) == 1 and sw[0].text == "kirocrew-lite"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_mcp_oauth_request():
    """MCP OAuth request notification yields EVENT_MCP_OAUTH_REQUEST."""
    from kiro_crew.acp.types import EVENT_MCP_OAUTH_REQUEST, METHOD_MCP_OAUTH_REQUEST

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_MCP_OAUTH_REQUEST,
                "params": {
                    "sessionId": "sA",
                    "serverName": "github-mcp",
                    "oauthUrl": "https://auth.example.com",
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        oauth = [e for e in events if e.kind == EVENT_MCP_OAUTH_REQUEST]
        assert len(oauth) == 1
        assert oauth[0].server_name == "github-mcp"
        assert oauth[0].oauth_url == "https://auth.example.com"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_mcp_server_initialized():
    """MCP server initialized yields EVENT_MCP_SERVER_INITIALIZED."""
    from kiro_crew.acp.types import EVENT_MCP_SERVER_INITIALIZED, METHOD_MCP_SERVER_INITIALIZED

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_MCP_SERVER_INITIALIZED,
                "params": {
                    "sessionId": "sA",
                    "serverName": "builder-mcp",
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        init = [e for e in events if e.kind == EVENT_MCP_SERVER_INITIALIZED]
        assert len(init) == 1 and init[0].server_name == "builder-mcp"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_mcp_server_init_failure():
    """MCP server init failure yields EVENT_MCP_SERVER_INIT_FAILURE."""
    from kiro_crew.acp.types import EVENT_MCP_SERVER_INIT_FAILURE, METHOD_MCP_SERVER_INIT_FAILURE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_MCP_SERVER_INIT_FAILURE,
                "params": {
                    "sessionId": "sA",
                    "serverName": "bad-mcp",
                    "error": "timeout",
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        fail = [e for e in events if e.kind == EVENT_MCP_SERVER_INIT_FAILURE]
        assert len(fail) == 1 and fail[0].server_name == "bad-mcp" and fail[0].text == "timeout"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_unknown_server_request_gets_error_response():
    """Unknown server→client request gets a -32601 error response."""
    rt, reader, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        # Unknown method WITH an id (server request, not notification)
        _feed(reader, {"id": 9999, "method": "unknown/method", "params": {"sessionId": "sA"}})
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        # Check that an error response was sent back
        calls = proc.stdin.write.call_args_list
        error_sent = False
        for call in calls:
            data = json.loads(call.args[0].decode())
            if data.get("id") == 9999 and "error" in data:
                assert data["error"]["code"] == JSONRPC_METHOD_NOT_FOUND
                error_sent = True
        assert error_sent, "Expected -32601 error response for unknown server request"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_tool_call_update_raw_output():
    """tool_call_update with rawOutput yields EVENT_TOOL_RESULT."""
    from kiro_crew.acp.types import EVENT_TOOL_RESULT

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "tc2",
                        "rawOutput": {"items": [{"Text": "raw stuff"}]},
                    },
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        tr = [e for e in events if e.kind == EVENT_TOOL_RESULT]
        assert len(tr) == 1 and tr[0].tool_output == "raw stuff"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_tool_call_update_refinement():
    """tool_call_update with title but no content yields EVENT_TOOL_CALL_UPDATE."""
    from kiro_crew.acp.types import EVENT_TOOL_CALL_UPDATE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "tc3",
                        "title": "Reading file",
                        "kind": "fs",
                        "rawInput": "/etc/hosts",
                    },
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        tu = [e for e in events if e.kind == EVENT_TOOL_CALL_UPDATE]
        assert len(tu) == 1
        assert tu[0].title == "Reading file"
        assert tu[0].tool_input == "/etc/hosts"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_usage_update():
    """usage_update sets context stats on last_prompt_stats."""
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:

        async def drive():
            async for _ in handle.prompt("hi", timeout=3.0):
                pass

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "usage_update",
                        "usage": {"used": 5000, "size": 10000},
                    },
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        assert handle.last_prompt_stats.context_pct == 50.0
        assert handle.last_prompt_stats.context_used_tokens == 5000
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_cost_and_prompt_tokens_reach_event_complete():
    """claude seam billing: a session-cumulative usage_update cost and the
    PromptResponse token counts are delta'd/folded into last_prompt_stats and
    surfaced on EVENT_COMPLETE.usage. Two turns
    prove the delta: turn 2 is billed only its own movement of the cumulative
    counter, and its own token counts."""
    from kiro_crew.acp.types import EVENT_COMPLETE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:

        async def run_turn(cost_amount, input_tokens, output_tokens):
            events: list = []

            async def drive():
                async for ev in handle.prompt("hi", timeout=3.0):
                    events.append(ev)

            driver = asyncio.ensure_future(drive())
            req_id = (await _await_routed(rt, "sA"))["sA"]
            _feed(
                reader,
                {
                    "method": METHOD_SESSION_UPDATE,
                    "params": {
                        "sessionId": "sA",
                        "update": {
                            "sessionUpdate": "usage_update",
                            "used": 5000,
                            "size": 10000,
                            "cost": {"amount": cost_amount, "currency": "USD"},
                        },
                    },
                },
            )
            _feed(
                reader,
                {
                    "id": req_id,
                    "result": {
                        "stopReason": "end_turn",
                        "inputTokens": input_tokens,
                        "outputTokens": output_tokens,
                        "cachedReadTokens": 7,
                        "cachedWriteTokens": 3,
                    },
                },
            )
            await await_until(driver, what="the driver finishing its turn")
            (complete,) = [ev for ev in events if ev.kind == EVENT_COMPLETE]
            return complete

        first = await run_turn(0.30, 100, 50)
        assert first.usage.cost_usd == pytest.approx(0.30)
        assert first.usage.input_tokens == 100
        assert first.usage.output_tokens == 50
        assert first.usage.cache_read_tokens == 7
        assert first.usage.cache_creation_tokens == 3

        # Turn 2: the cumulative counter moved 0.30 -> 0.50, so this turn's
        # cost is the 0.20 delta, and the token counts are its own, not a sum.
        second = await run_turn(0.50, 40, 20)
        assert second.usage.cost_usd == pytest.approx(0.20)
        assert second.usage.input_tokens == 40
        assert second.usage.output_tokens == 20
    finally:
        await _stop_reader(task)


@pytest.mark.parametrize(
    "cost",
    [
        "0.5",  # cost not dict-shaped
        {"amount": "0.5"},
        {"amount": True},
        {"amount": float("nan")},
        {"amount": -0.01},
        {"amount": 10**400},
    ],
)
def test_handle_update_malformed_cost_is_noop(cost):
    """A malformed agent-supplied cost must degrade to absent at the
    parse_usage_cost chokepoint — no exception inside the prompt-turn dispatch
    path, and no stats movement."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    msg = JsonRpcMessage(
        method=METHOD_SESSION_UPDATE,
        params={
            "sessionId": "sA",
            "update": {"sessionUpdate": "usage_update", "used": 1, "size": 2, "cost": cost},
        },
    )
    events = handle._handle_update(msg)  # must not raise
    assert events == []
    assert handle.last_prompt_stats.cost_usd == 0.0
    assert handle.last_prompt_stats.cost_session_usd == 0.0


@pytest.mark.parametrize(
    "used,size",
    [
        ("5000", "10000"),  # numeric strings
        (float("inf"), 10000),
        (float("nan"), float("nan")),
        (10**400, 10000),  # bignum: math.isfinite itself raises OverflowError
        ([5000], {"n": 1}),
        (True, True),
    ],
)
def test_handle_update_malformed_usage_is_noop(used, size):
    """The session-handle path consumes the same agent-supplied usage_update as
    AcpClient. parse_usage_update validates at the shared chokepoint, so a
    malformed used/size must be a no-op here too — not a TypeError/
    OverflowError inside the prompt-turn dispatch."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    msg = JsonRpcMessage(
        method=METHOD_SESSION_UPDATE,
        params={
            "sessionId": "sA",
            "update": {"sessionUpdate": "usage_update", "used": used, "size": size},
        },
    )
    events = handle._handle_update(msg)  # must not raise
    assert events == []
    assert handle.last_prompt_stats.context_pct == 0.0
    assert handle.last_prompt_stats.context_used_tokens == 0


@pytest.mark.asyncio
async def test_dispatch_metadata_credits():
    """_kiro.dev/metadata meteringUsage(unit=credit) accumulates into last_prompt_stats
    and is propagated onto EVENT_COMPLETE; non-credit units are ignored."""
    from kiro_crew.acp.types import EVENT_COMPLETE, METHOD_METADATA

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events: list = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_METADATA,
                "params": {
                    "sessionId": "sA",
                    "contextUsagePercentage": 12.5,
                    "meteringUsage": [
                        {"unit": "credit", "value": 1.0},
                        {"unit": "token", "value": 999},  # not a credit — ignored
                        {"unit": "credit", "value": 0.23},
                    ],
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        assert handle.last_prompt_stats.credits == pytest.approx(1.23)
        assert handle.last_prompt_stats.context_pct == 12.5
        complete = [e for e in events if e.kind == EVENT_COMPLETE]
        assert complete and complete[-1].usage.credits == pytest.approx(1.23)
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_metadata_credits_robust():
    """Non-numeric / missing meteringUsage values and metadata with no meteringUsage
    are handled without raising; credits stays 0."""
    from kiro_crew.acp.types import METHOD_METADATA

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:

        async def drive():
            async for _ in handle.prompt("hi", timeout=3.0):
                pass

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_METADATA,
                "params": {
                    "sessionId": "sA",
                    "meteringUsage": [{"unit": "credit", "value": "oops"}, {"unit": "credit"}],
                },
            },
        )
        _feed(
            reader, {"method": METHOD_METADATA, "params": {"sessionId": "sA"}}
        )  # no meteringUsage
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        assert handle.last_prompt_stats.credits == 0.0
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_metadata_credits_routed_per_session():
    """Concurrent sessions on one runtime each accrue only their own kiro credits —
    metadata notifications are demuxed by sessionId, no cross-talk."""
    from kiro_crew.acp.types import METHOD_METADATA

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA", "sB")
    handle_a = AcpSessionHandle("sA", q["sA"], rt)
    handle_b = AcpSessionHandle("sB", q["sB"], rt)
    task = await _start_reader(rt)

    async def drive(handle):
        async for _ in handle.prompt("go", timeout=5.0):
            pass

    da = asyncio.ensure_future(drive(handle_a))
    db = asyncio.ensure_future(drive(handle_b))
    try:
        sid_to_req = await _await_routed(rt, "sA", "sB")
        assert set(sid_to_req) == {"sA", "sB"}, "both prompts must be in flight"

        _feed(
            reader,
            {
                "method": METHOD_METADATA,
                "params": {
                    "sessionId": "sA",
                    "meteringUsage": [{"unit": "credit", "value": 2.0}],
                },
            },
        )
        _feed(
            reader,
            {
                "method": METHOD_METADATA,
                "params": {
                    "sessionId": "sB",
                    "meteringUsage": [{"unit": "credit", "value": 0.5}],
                },
            },
        )
        _feed(reader, {"id": sid_to_req["sA"], "result": {"stopReason": "end_turn"}})
        _feed(reader, {"id": sid_to_req["sB"], "result": {"stopReason": "end_turn"}})
        await await_until(asyncio.gather(da, db), what="every driver finishing its turn")

        assert handle_a.last_prompt_stats.credits == pytest.approx(2.0)
        assert handle_b.last_prompt_stats.credits == pytest.approx(0.5)
    finally:
        for t in (da, db):
            if not t.done():
                t.cancel()
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_subagent_list():
    """Subagent list notification yields EVENT_SUBAGENT_LIST."""
    from kiro_crew.acp.types import EVENT_SUBAGENT_LIST, METHOD_SUBAGENT_LIST_UPDATE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_SUBAGENT_LIST_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "subagents": [{"id": "sub1"}],
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        sl = [e for e in events if e.kind == EVENT_SUBAGENT_LIST]
        assert len(sl) == 1 and sl[0].subagents == [{"id": "sub1"}]
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_subagent_activity_tool():
    """Subagent activity with toolCallId yields EVENT_SUBAGENT_ACTIVITY.

    The kiro frame's params.sessionId is the SUB-session id (not the parent's
    registered session), so the reader would correctly drop it. We inject it
    straight into the parent's queue to exercise the dispatch branch.
    """
    from kiro_crew.acp.types import EVENT_SUBAGENT_ACTIVITY, METHOD_KIRO_SESSION_UPDATE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        q["sA"].put_nowait(
            JsonRpcMessage.from_dict(
                {
                    "method": METHOD_KIRO_SESSION_UPDATE,
                    "params": {
                        "sessionId": "sub-1",
                        "update": {"toolCallId": "tc5", "title": "read file"},
                    },
                }
            )
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        sa = [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY]
        assert len(sa) == 1
        assert sa[0].sub_session_id == "sub-1"
        assert sa[0].tool_call_id == "tc5"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_subagent_activity_text():
    """Subagent activity with agent_message_chunk yields text event."""
    from kiro_crew.acp.types import EVENT_SUBAGENT_ACTIVITY, METHOD_KIRO_SESSION_UPDATE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        q["sA"].put_nowait(
            JsonRpcMessage.from_dict(
                {
                    "method": METHOD_KIRO_SESSION_UPDATE,
                    "params": {
                        "sessionId": "sub-2",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "text": "hello from sub",
                        },
                    },
                }
            )
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        sa = [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY]
        assert len(sa) == 1
        assert sa[0].text == "hello from sub"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_subagent_activity_text_is_redacted():
    """Sub-agent streamed text is LLM output surfaced on the dashboard, so it
    MUST be scrubbed (credentials + exfil URLs) before being yielded."""
    from kiro_crew.acp.types import EVENT_SUBAGENT_ACTIVITY, METHOD_KIRO_SESSION_UPDATE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        q["sA"].put_nowait(
            JsonRpcMessage.from_dict(
                {
                    "method": METHOD_KIRO_SESSION_UPDATE,
                    "params": {
                        "sessionId": "sub-3",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "text": "leaked AKIAIOSFODNN7EXAMPLE key",
                        },
                    },
                }
            )
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        sa = [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY]
        assert len(sa) == 1
        assert "AKIAIOSFODNN7EXAMPLE" not in sa[0].text
        assert "[REDACTED: credential]" in sa[0].text
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_subagent_activity_ignores_this_session():
    """The extension spelling naming THIS session yields no sub-agent activity.

    kiro-cli carries the parent turn's own ``tool_call_chunk`` on
    ``_kiro.dev/session/update`` with ``params.sessionId`` set to the parent's
    session -- the same method a child's update arrives on -- so the sessionId is
    the only thing separating the two. Treating the parent's frame as a child's
    puts a sub-agent on the session the user is already watching, with that
    session's own id, once per tool call. Recorded live in
    ``test/fixtures/acp_frames/kiro/session.jsonl``.
    """
    from kiro_crew.acp.types import EVENT_SUBAGENT_ACTIVITY, METHOD_KIRO_SESSION_UPDATE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        q["sA"].put_nowait(
            JsonRpcMessage.from_dict(
                {
                    "method": METHOD_KIRO_SESSION_UPDATE,
                    "params": {
                        "sessionId": "sA",
                        "update": {
                            "sessionUpdate": "tool_call_chunk",
                            "toolCallId": "tc-own",
                            "title": "shell",
                            "kind": "execute",
                        },
                    },
                }
            )
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        assert [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY] == []
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_dispatch_subagent_activity_ignores_this_session_text():
    """Same scoping for the text carrier, so one guard cannot cover half the shape."""
    from kiro_crew.acp.types import EVENT_SUBAGENT_ACTIVITY, METHOD_KIRO_SESSION_UPDATE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        q["sA"].put_nowait(
            JsonRpcMessage.from_dict(
                {
                    "method": METHOD_KIRO_SESSION_UPDATE,
                    "params": {
                        "sessionId": "sA",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "text": "the parent's own streamed text",
                        },
                    },
                }
            )
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        assert [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY] == []
    finally:
        await _stop_reader(task)
