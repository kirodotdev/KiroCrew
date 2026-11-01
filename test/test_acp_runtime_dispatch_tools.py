"""``AcpSessionHandle._dispatch_events``: permission requests and tool calls.

The permission event and the deny-set refusals at the handle's answering site, tool
calls and their results, the tool-stall recovery, and thinking chunks.
"""

import asyncio
import json
from unittest.mock import AsyncMock

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
from kiro_crew.acp.types import METHOD_SESSION_UPDATE
from kiro_crew.testing.wait import await_until

# ── _dispatch_events: notification kind branches ──


@pytest.mark.asyncio
async def test_dispatch_permission_request():
    """Permission request notification yields EVENT_PERMISSION_REQUEST.

    Uses kiro-cli's REAL payload shape: the tool info is nested under
    ``params["toolCall"]`` (title/kind/toolCallId), NOT flat under ``params``.
    A prior ``tool_call`` update (kind="execute") seeds the trusted shell cache
    so the permission event resolves ``is_shell=True`` — the signal chat_runner's
    trust-mode gate needs to waive the tool-name length cap on shell commands.
    """
    from kiro_crew.acp.types import (
        EVENT_PERMISSION_REQUEST,
        METHOD_REQUEST_PERMISSION,
        METHOD_SESSION_UPDATE,
    )

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
        # First a tool_call update (seeds the trusted is_shell cache).
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tcP",
                        "title": "git status",
                        "kind": "execute",
                    },
                },
            },
        )
        # Then the permission request in kiro's real toolCall-nested shape.
        _feed(
            reader,
            {
                "id": 5001,
                "method": METHOD_REQUEST_PERMISSION,
                "params": {
                    "sessionId": "sA",
                    "toolCall": {"title": "git status", "kind": "execute", "toolCallId": "tcP"},
                    "options": [
                        {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                        {
                            "optionId": "allow_always",
                            "name": "Allow always",
                            "kind": "allow_always",
                        },
                    ],
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        perm = [e for e in events if e.kind == EVENT_PERMISSION_REQUEST]
        assert len(perm) == 1
        assert perm[0].title == "git status"
        assert perm[0].request_id == 5001
        assert perm[0].tool_kind == "execute"
        assert perm[0].tool_call_id == "tcP"
        # The critical regression guard: is_shell must be True so the trust-mode
        # gate does not reject the long shell command title on the length cap.
        assert perm[0].is_shell is True
        # Advertised optionIds recorded so approve/reject echo the exact ids.
        assert handle._permission_options[5001] == {"once": "allow_once", "always": "allow_always"}
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_a_spec_disabled_tool_is_refused_before_the_event_is_yielded():
    """The deny set the mirror's projection returned is enforced in the dispatch loop.

    Unit-testing ``_deny_spec_disabled_tool`` alone would pass on a loop that never
    called it, and an unwired restriction on a host that approves its own tools is
    the whole defect. So this drives the real loop: the ``tool_call`` frame seeds the
    trusted identity cache, the approval arrives, and NOTHING is yielded -- a
    consumer that auto-approves on hooks or trust must not get the chance, and a
    human must not be asked to re-decide what the agent spec settled.
    """
    from kiro_crew.acp.types import (
        EVENT_PERMISSION_REQUEST,
        METHOD_REQUEST_PERMISSION,
        METHOD_SESSION_UPDATE,
    )

    rt, reader, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    # What AcpRuntime sets from the projection on a mirrored host.
    handle.spec_denied_tools = frozenset({("kirocrew-core", "spawn_run")})
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        # The adapter's own resolution of what will run -- the one identity channel
        # the model cannot reach.
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tcD",
                        "title": "mcp.kirocrew-core.spawn_run",
                        "kind": "execute",
                        "rawInput": {"server": "kirocrew-core", "tool": "spawn_run"},
                    },
                },
            },
        )
        _feed(
            reader,
            {
                "id": 6001,
                "method": METHOD_REQUEST_PERMISSION,
                "params": {
                    "sessionId": "sA",
                    "toolCall": {"kind": "execute", "toolCallId": "tcD"},
                    "options": [
                        {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "cancel", "name": "Cancel", "kind": "reject_once"},
                    ],
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        assert [e for e in events if e.kind == EVENT_PERMISSION_REQUEST] == []
        answered = [
            json.loads(call.args[0].decode())
            for call in proc.stdin.write.call_args_list
            if b'"id": 6001' in call.args[0] or b'"id":6001' in call.args[0]
        ]
        assert answered, "the request must be ANSWERED, never dropped -- a dropped one hangs"
        assert answered[-1]["result"]["outcome"] == {
            "outcome": "selected",
            "optionId": "cancel",
        }
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_a_tool_the_deny_set_does_not_name_still_reaches_the_consumer():
    """The refusal is narrow: a set that names another tool changes nothing."""
    from kiro_crew.acp.types import (
        EVENT_PERMISSION_REQUEST,
        METHOD_REQUEST_PERMISSION,
        METHOD_SESSION_UPDATE,
    )

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.spec_denied_tools = frozenset({("kirocrew-core", "spawn_run")})
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
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tcE",
                        "title": "mcp.kirocrew-core.send_message",
                        "kind": "execute",
                        "rawInput": {"server": "kirocrew-core", "tool": "send_message"},
                    },
                },
            },
        )
        _feed(
            reader,
            {
                "id": 6002,
                "method": METHOD_REQUEST_PERMISSION,
                "params": {
                    "sessionId": "sA",
                    "toolCall": {"kind": "execute", "toolCallId": "tcE"},
                    "options": [
                        {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "cancel", "name": "Cancel", "kind": "reject_once"},
                    ],
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        perm = [e for e in events if e.kind == EVENT_PERMISSION_REQUEST]
        assert len(perm) == 1
        assert perm[0].request_id == 6002
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_an_unidentifiable_mcp_approval_never_reaches_the_consumer():
    """The second refusal is wired into the dispatch loop, not merely callable.

    A standalone MCP approval carries no correlated ``tool_call`` frame, so the
    handle cannot check it against the deny set. It must be ANSWERED here rather than
    yielded: the consumer auto-approves by hook glob and in trust mode, and this
    session's spec switched a tool off.
    """
    from kiro_crew.acp.types import (
        EVENT_PERMISSION_REQUEST,
        METHOD_REQUEST_PERMISSION,
    )

    rt, reader, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.spec_denied_tools = frozenset({("kirocrew-core", "spawn_run")})
    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        # No tool_call frame first: this is codex's standalone shape, which still
        # carries the MCP marker.
        _feed(
            reader,
            {
                "id": 6003,
                "method": METHOD_REQUEST_PERMISSION,
                "params": {
                    "sessionId": "sA",
                    "toolCall": {"kind": "execute", "toolCallId": "tcUnknown"},
                    "_meta": {"is_mcp_tool_approval": True},
                    "options": [
                        {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "cancel", "name": "Cancel", "kind": "reject_once"},
                    ],
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        assert [e for e in events if e.kind == EVENT_PERMISSION_REQUEST] == []
        answered = [
            json.loads(call.args[0].decode())
            for call in proc.stdin.write.call_args_list
            if b'"id": 6003' in call.args[0] or b'"id":6003' in call.args[0]
        ]
        assert answered, "the request must be ANSWERED, never dropped -- a dropped one hangs"
        assert answered[-1]["result"]["outcome"]["optionId"] == "cancel"
    finally:
        await _stop_reader(task)


def test_both_deny_set_refusals_run_at_the_handles_one_answering_site():
    """Structural: ``AcpClient`` splits these across two sites because it HAS two.

    The handle has one, so both belong on it. A refusal that exists but is not called
    from the loop is a restriction nothing enforces, and the behavioural tests above
    each cover only their own branch.
    """
    import inspect

    body = inspect.getsource(AcpSessionHandle._dispatch_events)
    assert "self._deny_spec_disabled_tool(" in body
    assert "self._refuse_unidentifiable_mcp_approval(" in body


@pytest.mark.asyncio
async def test_dispatch_tool_call_and_result():
    """Tool call + tool result notifications yield correct events."""
    from kiro_crew.acp.types import EVENT_TOOL_CALL, EVENT_TOOL_RESULT

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
        # Tool call
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc1",
                        "title": "bash",
                        "kind": "shell",
                    },
                },
            },
        )
        # Tool result (real kiro 2.10.0 shape: nested block.content.text)
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "tc1",
                        "content": [{"content": {"type": "text", "text": "output here"}}],
                    },
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")

        tc = [e for e in events if e.kind == EVENT_TOOL_CALL]
        tr = [e for e in events if e.kind == EVENT_TOOL_RESULT]
        assert len(tc) == 1 and tc[0].tool_call_id == "tc1" and tc[0].title == "bash"
        assert len(tr) == 1 and tr[0].tool_output == "output here"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_tool_stall_cancels_session_not_runtime(monkeypatch):
    """A dispatched tool that goes silent must be recovered by a session-scoped
    session/cancel (so co-tenant sessions on the shared runtime survive), NOT by
    killing the runtime process. The turn ends with stop_reason 'tool_stall'."""
    from kiro_crew.acp.session_handle import WatchdogSettings
    from kiro_crew.acp.types import EVENT_COMPLETE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    rt.send_notification = AsyncMock()  # type: ignore[method-assign]
    handle = AcpSessionHandle(
        "sA",
        q["sA"],
        rt,
        watchdog=WatchdogSettings(check_after_secs=0.01, tool_stall_suspect_secs=0.05),
    )
    handle._tool_dispatched = True  # a tool was dispatched this turn
    handle._stale_eligible = False  # the stale-turn check must NOT be what fires

    # Queue that always times out (empty forever), advancing the wall clock a
    # little each poll so the stall idle window is crossed deterministically.
    class _SilentQueue:
        async def get(self):
            # flake-ok: pre-existing 60 ms stand-in queue poll that crosses the 50 ms stall window, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- the stubbed queue's own poll delay, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0.06)
            raise asyncio.TimeoutError

        def qsize(self) -> int:
            return 0  # always empty; TOCTOU guard sees no new frames

    handle._queue = _SilentQueue()  # type: ignore[assignment]

    events = []
    async for ev in handle._dispatch_events(req_id=1, timeout=30.0):
        events.append(ev)

    # Recovery was a session-scoped session/cancel for THIS sessionId — the
    # runtime process is never killed (no killpg/SIGKILL on the stall path).
    rt.send_notification.assert_awaited_once()
    assert rt.send_notification.call_args.args[0] == "session/cancel"
    assert rt.send_notification.call_args.args[1]["sessionId"] == "sA"
    # Turn ends cleanly, flagged as a stall.
    assert events and events[-1].kind == EVENT_COMPLETE
    assert events[-1].stop_reason == "error: tool stall"


@pytest.mark.asyncio
async def test_tool_stall_recovery_completes_even_if_cancel_fails(monkeypatch):
    """If session/cancel raises or times out (an unresponsive runtime is likely
    right after a stall), the watchdog must still complete the turn — the
    bounded wait_for + except must not let recovery hang or bubble."""
    from kiro_crew.acp.session_handle import WatchdogSettings
    from kiro_crew.acp.types import EVENT_COMPLETE

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle(
        "sA",
        q["sA"],
        rt,
        watchdog=WatchdogSettings(check_after_secs=0.01, tool_stall_suspect_secs=0.05),
    )
    handle._tool_dispatched = True
    handle._stale_eligible = False
    # cancel() fails (stands in for the wait_for timeout path — both raise into
    # the same except Exception).
    handle.cancel = AsyncMock(side_effect=RuntimeError("runtime unresponsive"))  # type: ignore[method-assign]

    class _SilentQueue:
        async def get(self):
            # flake-ok: pre-existing 60 ms stand-in queue poll that crosses the 50 ms stall window, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- the stubbed queue's own poll delay, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0.06)
            raise asyncio.TimeoutError

        def qsize(self) -> int:
            return 0  # always empty; TOCTOU guard sees no new frames

    handle._queue = _SilentQueue()  # type: ignore[assignment]

    events = []
    async for ev in handle._dispatch_events(req_id=1, timeout=30.0):
        events.append(ev)

    handle.cancel.assert_awaited_once()
    assert events and events[-1].kind == EVENT_COMPLETE
    assert events[-1].stop_reason == "error: tool stall"


@pytest.mark.asyncio
async def test_dispatch_thinking_chunk():
    """agent_thought_chunk yields EVENT_THINKING_CHUNK."""
    from kiro_crew.acp.types import EVENT_THINKING_CHUNK

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
                    "update": {"sessionUpdate": "agent_thought_chunk", "text": "thinking..."},
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
        think = [e for e in events if e.kind == EVENT_THINKING_CHUNK]
        assert len(think) == 1 and think[0].text == "thinking..."
    finally:
        await _stop_reader(task)
