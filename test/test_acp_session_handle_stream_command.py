"""Tests for AcpSessionHandle.stream_command — native slash-command execution.

The dashboard's shared-runtime sessions previously routed slash commands
through session/prompt (a full LLM turn that *summarized* kiro-cli's output).
stream_command sends ``_kiro.dev/commands/execute`` with the TuiCommand OBJECT
form (``{command, args}`` — kiro-cli 2.14.0 returns no response on the string
form) and drains session/update events with prompt()'s turn discipline, so the
command's own structured output comes back deterministically.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.session_handle import AcpRuntimeError, AcpSessionHandle
from kiro_crew.acp.types import (
    EVENT_AGENT_SWITCHED,
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    METHOD_AGENT_SWITCHED,
    METHOD_COMMANDS_EXECUTE,
    METHOD_COMPACTION_STATUS,
    METHOD_SESSION_UPDATE,
    STOP_REASON_COMPACTION_FAILED,
    STOP_REASON_TOOL_STALL,
    JsonRpcMessage,
)

_REQ_ID = 7


class _CommandRuntime:
    """Runtime double for commands/execute turns.

    ``send_request`` records the outbound frame and enqueues the scripted
    session/update frames followed by the JSON-RPC response — AFTER the send,
    so the pre-turn stale-frame drain cannot eat them. ``respond=False``
    enqueues nothing, modelling kiro-cli's known no-response behavior.
    """

    def __init__(
        self,
        queue: asyncio.Queue,
        response_result: dict[str, Any] | None = None,
        updates: list[JsonRpcMessage] | None = None,
        acp_backend: str = "",
        respond: bool = True,
    ) -> None:
        self.pid = None
        self.is_alive = MagicMock(return_value=True)
        self.send_notification = AsyncMock()
        self.supports_image_prompt = False
        self.acp_backend = acp_backend
        self.requests: list[tuple[str, dict]] = []
        self.marks: list[tuple[str, bool]] = []
        self._queue = queue
        self._response_result = response_result if response_result is not None else {}
        self._updates = updates or []
        self._respond = respond
        self._last_activity = time.monotonic()

    def mark_turn_active(self, session_id: str, active: bool) -> None:
        self.marks.append((session_id, active))

    async def send_request(self, method: str, params: dict) -> int:
        self.requests.append((method, params))
        for frame in self._updates:
            self._queue.put_nowait(frame)
        if self._respond:
            self._queue.put_nowait(JsonRpcMessage(id=_REQ_ID, result=self._response_result))
        return _REQ_ID


def _make(
    response_result: dict[str, Any] | None = None,
    updates: list[JsonRpcMessage] | None = None,
    acp_backend: str = "",
    respond: bool = True,
) -> tuple[AcpSessionHandle, _CommandRuntime]:
    queue: asyncio.Queue = asyncio.Queue()
    rt = _CommandRuntime(
        queue,
        response_result=response_result,
        updates=updates,
        acp_backend=acp_backend,
        respond=respond,
    )
    handle = AcpSessionHandle("sA", queue, rt)
    return handle, rt


async def _collect(handle: AcpSessionHandle, command: str) -> list:
    return [ev async for ev in handle.stream_command(command, timeout=5.0)]


# ── Request shape ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sends_object_form_without_args():
    """A bare command goes out as the TuiCommand OBJECT form with empty args —
    the string form returns no response on kiro-cli 2.14.0."""
    handle, rt = _make(response_result={"message": "ok"})
    await _collect(handle, "/tools")
    assert rt.requests == [
        (
            METHOD_COMMANDS_EXECUTE,
            {"sessionId": "sA", "command": {"command": "tools", "args": {}}},
        )
    ]


@pytest.mark.asyncio
async def test_sends_object_form_with_value_arg():
    """``/agent foo`` carries the remainder as the ``value`` arg."""
    handle, rt = _make(response_result={"message": "ok"})
    await _collect(handle, "/agent foo")
    method, params = rt.requests[0]
    assert method == METHOD_COMMANDS_EXECUTE
    assert params["command"] == {"command": "agent", "args": {"value": "foo"}}


# ── Result extraction & completion ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_response_text_yielded_then_complete():
    """The command's output lives in the RESPONSE result, not update chunks —
    it must surface as a text chunk before the terminal event."""
    handle, _ = _make(response_result={"message": "13 tools available"})
    events = await _collect(handle, "/tools")
    kinds = [ev.kind for ev in events]
    assert kinds == [EVENT_TEXT_CHUNK, EVENT_COMPLETE]
    assert events[0].text == "13 tools available"
    # Turn bookkeeping: the handle is reusable afterwards.
    assert handle._turn_done.is_set()
    assert handle.is_turn_active is False


@pytest.mark.asyncio
async def test_structured_data_formatted_as_json_block():
    """result.data renders as a readable JSON block (agent/model filtered)."""
    handle, _ = _make(
        response_result={
            "message": "MCP servers",
            "data": {"servers": ["a", "b"], "agent": {"name": "x"}},
        }
    )
    events = await _collect(handle, "/mcp")
    text_events = [ev for ev in events if ev.kind == EVENT_TEXT_CHUNK]
    assert len(text_events) == 1
    assert "MCP servers" in text_events[0].text
    assert "```json" in text_events[0].text
    assert '"servers"' in text_events[0].text
    # agent metadata is filtered from the block (surfaced separately).
    assert '"agent"' not in text_events[0].text


@pytest.mark.asyncio
async def test_empty_result_yields_only_complete():
    """No message/data → no empty text chunk, just the terminal event."""
    handle, _ = _make(response_result={})
    events = await _collect(handle, "/tools")
    assert [ev.kind for ev in events] == [EVENT_COMPLETE]


@pytest.mark.asyncio
async def test_result_text_is_credential_redacted():
    """Command output is backend-echoed text that reaches the dashboard —
    the two-pass redaction (URLs + credentials) must run on it, matching
    send_command's auditable control at the same surface."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    handle, _ = _make(response_result={"message": f"key {secret} leaked"})
    events = await _collect(handle, "/env")
    text_events = [ev for ev in events if ev.kind == EVENT_TEXT_CHUNK]
    assert len(text_events) == 1
    assert secret not in text_events[0].text


@pytest.mark.asyncio
async def test_agent_switch_extracted_from_result():
    """A command result carrying data.agent (e.g. /agent) emits
    EVENT_AGENT_SWITCHED so the dashboard's indicator updates."""
    handle, _ = _make(
        response_result={
            "message": "switched",
            "data": {"agent": {"name": "kirocrew"}},
        }
    )
    events = await _collect(handle, "/agent kirocrew")
    switches = [ev for ev in events if ev.kind == EVENT_AGENT_SWITCHED]
    assert [ev.text for ev in switches] == ["kirocrew"]


@pytest.mark.asyncio
async def test_agent_switch_not_duplicated_when_notification_arrived():
    """When the native agent-switch notification already reported the switch,
    the result-extracted fallback must not emit a second event."""
    notification = JsonRpcMessage(
        method=METHOD_AGENT_SWITCHED,
        params={"sessionId": "sA", "agentName": "kirocrew"},
    )
    handle, _ = _make(
        response_result={"data": {"agent": {"name": "kirocrew"}}},
        updates=[notification],
    )
    events = await _collect(handle, "/agent kirocrew")
    switches = [ev for ev in events if ev.kind == EVENT_AGENT_SWITCHED]
    assert len(switches) == 1


# ── Event draining ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drains_session_update_chunks_before_response():
    """session/update frames emitted during command execution stream through
    with prompt()'s dispatch logic, ahead of the response-extracted text."""
    update = JsonRpcMessage(
        method=METHOD_SESSION_UPDATE,
        params={
            "sessionId": "sA",
            "update": {"sessionUpdate": "agent_message_chunk", "text": "streamed"},
        },
    )
    handle, _ = _make(response_result={"message": "final"}, updates=[update])
    events = await _collect(handle, "/tools")
    texts = [ev.text for ev in events if ev.kind == EVENT_TEXT_CHUNK]
    assert texts == ["streamed", "final"]
    assert events[-1].kind == EVENT_COMPLETE


# ── Turn discipline ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rejected_while_turn_active():
    """A command during an active turn is refused, not interleaved — it shares
    prompt()'s concurrent-turn guard on the same session queue."""
    handle, _ = _make(response_result={"message": "ok"})
    handle._turn_done.clear()  # a turn is in flight
    with pytest.raises(AcpRuntimeError):
        await _collect(handle, "/tools")


@pytest.mark.asyncio
async def test_turn_marked_active_and_released():
    """The turn is marked active around the send and released at completion,
    so shared-runtime frame routing sees the command like any other turn."""
    handle, rt = _make(response_result={"message": "ok"})
    await _collect(handle, "/tools")
    assert rt.marks == [("sA", True), ("sA", False)]


@pytest.mark.asyncio
async def test_send_failure_keeps_handle_reusable():
    """A send_request failure re-sets _turn_done and unmarks the turn — the
    BaseException guard — so the handle is not wedged permanently active."""
    handle, rt = _make()

    async def _boom(method: str, params: dict) -> int:
        raise AcpRuntimeError("broken pipe")

    rt.send_request = _boom  # type: ignore[method-assign]
    with pytest.raises(AcpRuntimeError):
        await _collect(handle, "/tools")
    assert handle._turn_done.is_set()
    # Marked active before the (failed) write, unmarked by the guard.
    assert rt.marks == [("sA", True), ("sA", False)]


# ── Transport carve-outs & failure modes ─────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/compact", "/compact keep the design", "/help"])
async def test_prompt_transport_commands_stay_on_prompt(command):
    """/compact and /help keep the PROMPT transport: kiro-cli 2.14.0 returns
    no response for them over commands/execute, and the compaction flow
    (session.py, Slack !compact) watches compaction status on the prompt
    stream — routing them natively would strand it until timeout."""
    from kiro_crew.acp.types import METHOD_PROMPT

    handle, rt = _make(response_result={"stopReason": "end_turn"})
    events = await _collect(handle, command)
    assert [m for m, _ in rt.requests] == [METHOD_PROMPT]
    assert events[-1].kind == EVENT_COMPLETE


@pytest.mark.asyncio
async def test_kas_backend_falls_back_to_prompt():
    """_kiro.dev/commands/execute is kiro-cli-specific: a KAS shared-runtime
    session must keep degrading softly through session/prompt instead of
    erroring on an unimplemented method."""
    from kiro_crew.acp.types import ACP_BACKEND_KAS, METHOD_PROMPT

    handle, rt = _make(response_result={"stopReason": "end_turn"}, acp_backend=ACP_BACKEND_KAS)
    events = await _collect(handle, "/tools")
    assert [m for m, _ in rt.requests] == [METHOD_PROMPT]
    assert events[-1].kind == EVENT_COMPLETE


@pytest.mark.asyncio
async def test_no_response_terminates_at_timeout():
    """An unanswered commands/execute request must terminate at the bounded
    command timeout with a terminal event — not drain for the chat-turn
    ceiling — and leave the handle reusable."""
    handle, _ = _make(respond=False)
    start = time.monotonic()
    events = await _collect_with_timeout(handle, "/tools", timeout=0.5)
    elapsed = time.monotonic() - start
    assert events[-1].kind == EVENT_COMPLETE
    assert events[-1].stop_reason == "timeout"
    assert elapsed < 5.0
    assert handle._turn_done.is_set()
    assert handle.is_turn_active is False


async def _collect_with_timeout(handle: AcpSessionHandle, command: str, timeout: float) -> list:
    return [ev async for ev in handle.stream_command(command, timeout=timeout)]


# ── Post-compaction-failure budget (issue #3583) ─────────────────────────────


def _failed_compaction(params: dict | None = None) -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_COMPACTION_STATUS,
        params=params if params is not None else {"status": {"type": "failed"}},
    )


@pytest.mark.asyncio
async def test_failed_compaction_then_no_response_ends_the_turn(monkeypatch):
    """kiro-cli reports compaction `failed` and then abandons the prompt: no
    response, no end_turn. The turn must end at the post-failure budget with
    STOP_REASON_COMPACTION_FAILED instead of draining to the turn ceiling and
    holding the slot (issue #3583)."""
    from kiro_crew.acp import session_handle as sh

    monkeypatch.setattr(sh, "_COMPACTION_FAILED_TURN_BUDGET", 0.2)

    handle, _rt = _make(respond=False, updates=[_failed_compaction()])
    start = time.monotonic()
    events = [ev async for ev in handle.prompt("hello", timeout=30.0)]
    elapsed = time.monotonic() - start

    assert events[0].kind == EVENT_COMPACTION_STATUS
    assert events[0].text == "failed"
    assert events[-1].kind == EVENT_COMPLETE
    assert events[-1].stop_reason == STOP_REASON_COMPACTION_FAILED
    # Proves the budget ended it, not the 30s turn deadline. The bound is loose
    # because the check runs on the loop's own tick: the dispatch loop parks on
    # the session queue for up to 5s, so a fired budget is acted on at the next
    # wake, not the instant it expires.
    assert elapsed < 10.0, f"turn ran too long ({elapsed:.2f}s) — the hang is back"
    assert handle._turn_done.is_set()
    assert handle.is_turn_active is False


@pytest.mark.asyncio
async def test_a_tool_in_flight_suspends_the_post_failure_budget(monkeypatch):
    """Shared-runtime twin of the client rule: a tool dispatched after the failed
    compaction is live work, so the budget must not cancel the session under it.
    The tool-stall watchdog owns that case on its own longer, liveness-gated
    budget."""
    from kiro_crew.acp import session_handle as sh

    monkeypatch.setattr(sh, "_COMPACTION_FAILED_TURN_BUDGET", 0.05)

    handle, _rt = _make(respond=False, updates=[_failed_compaction()])

    # The turn ceiling must outlast one queue tick (the loop parks on the
    # session queue for up to 5s, and the budget is checked at the loop top), or
    # the unsuspended budget could not fire either and the test would be vacuous.
    start = time.monotonic()
    events = []
    async for ev in handle.prompt("hello", timeout=8.0):
        events.append(ev)
        if ev.kind == EVENT_COMPACTION_STATUS and ev.text == "failed":
            # The turn recovers and dispatches a tool. Set from the consumer
            # side because prompt()'s prologue clears the flag at turn start.
            handle._tool_dispatched = True
    elapsed = time.monotonic() - start

    # The turn ran to its own deadline instead of being reaped at the budget,
    # so the terminal event is the ordinary timeout, not the compaction reason.
    assert events[-1].stop_reason != STOP_REASON_COMPACTION_FAILED
    assert elapsed >= 7.0, f"turn ended at {elapsed:.2f}s — the budget reaped a live tool"
    # Still armed: the budget re-fires once the tool resolves.
    assert handle._compaction_failed_at is not None


@pytest.mark.asyncio
async def test_failed_compaction_notice_carries_the_reason(monkeypatch):
    """The dashboard notice reads AcpEvent.title, and kiro-cli leaves `summary`
    empty on failure — the notification's own reason rides the title so the row
    stops collapsing to "unknown error"."""
    from kiro_crew.acp import session_handle as sh

    monkeypatch.setattr(sh, "_COMPACTION_FAILED_TURN_BUDGET", 0.2)

    handle, _rt = _make(
        respond=False,
        updates=[_failed_compaction({"status": {"type": "failed", "reason": "history too long"}})],
    )
    events = [ev async for ev in handle.prompt("hello", timeout=30.0)]

    assert events[0].title == "history too long"


@pytest.mark.asyncio
async def test_co_tenant_fanout_frames_do_not_defer_the_budget(monkeypatch):
    """On a shared runtime, ownerless global notifications are fanned out to
    every co-tenant queue (msg.fanout_no_owner). They are ANOTHER session's
    traffic: if they reset this session's post-failure silence clock, a busy
    co-tenant defers the budget to the multi-hour outer deadline and the
    original #3583 hang survives on shared runtimes."""
    from kiro_crew.acp import session_handle as sh

    monkeypatch.setattr(sh, "_COMPACTION_FAILED_TURN_BUDGET", 0.2)

    handle, _rt = _make(respond=False, updates=[_failed_compaction()])

    stop_feeding = asyncio.Event()

    async def _co_tenant_chatter():
        # Recurring ownerless roster updates, well inside the budget interval.
        while not stop_feeding.is_set():
            frame = JsonRpcMessage(method="kiro/subagents/update", params={"subagents": []})
            frame.fanout_no_owner = True
            handle._queue.put_nowait(frame)
            await asyncio.sleep(0.05)

    feeder = asyncio.create_task(_co_tenant_chatter())
    try:
        start = time.monotonic()
        events = [ev async for ev in handle.prompt("hello", timeout=30.0)]
        elapsed = time.monotonic() - start
    finally:
        stop_feeding.set()
        await feeder

    assert events[-1].kind == EVENT_COMPLETE
    assert events[-1].stop_reason == STOP_REASON_COMPACTION_FAILED
    assert elapsed < 10.0, f"turn ran {elapsed:.2f}s — co-tenant fanout frames deferred the budget"


@pytest.mark.asyncio
async def test_ownerless_failure_does_not_arm_a_co_tenant_budget(monkeypatch):
    """A compaction notification with no sessionId is fanned out to every
    co-tenant, so at most one recipient actually compacted. Arming this
    session's budget from a peer's failure would reap this session's live turn
    at the budget — and every consumer resets the session on that terminal, so
    the peer's failure destroys unrelated work."""
    from kiro_crew.acp import session_handle as sh

    monkeypatch.setattr(sh, "_COMPACTION_FAILED_TURN_BUDGET", 0.2)

    frame = _failed_compaction()
    frame.fanout_no_owner = True
    handle, _rt = _make(respond=False, updates=[frame])

    # The deadline must sit past the dispatch loop's queue park (up to 5s) or
    # the budget never gets a tick to fire on and the test cannot fail.
    start = time.monotonic()
    events = [ev async for ev in handle.prompt("hello", timeout=7.0)]
    elapsed = time.monotonic() - start

    assert handle._compaction_failed_at is None, "a peer's failure armed this session"
    assert events[-1].kind == EVENT_COMPLETE
    assert events[-1].stop_reason != STOP_REASON_COMPACTION_FAILED
    assert (
        elapsed >= 5.5
    ), f"turn ended after {elapsed:.2f}s — a co-tenant's failure reaped this turn"


@pytest.mark.asyncio
async def test_ownerless_completion_does_not_disarm_the_budget(monkeypatch):
    """The mirror direction, and the one that silently restores the #3583 hang:
    a peer's SUCCESSFUL compaction is fanned out too, and clearing this
    session's armed budget from it would leave a genuinely abandoned turn
    draining to the multi-hour turn ceiling again."""
    from kiro_crew.acp import session_handle as sh

    monkeypatch.setattr(sh, "_COMPACTION_FAILED_TURN_BUDGET", 0.2)

    peer_ok = _failed_compaction({"status": {"type": "completed"}, "summary": "peer"})
    peer_ok.fanout_no_owner = True
    # This session's own failure arms the budget; the peer's completion must not
    # clear it.
    handle, _rt = _make(respond=False, updates=[_failed_compaction(), peer_ok])

    events = [ev async for ev in handle.prompt("hello", timeout=30.0)]

    assert events[-1].kind == EVENT_COMPLETE
    assert (
        events[-1].stop_reason == STOP_REASON_COMPACTION_FAILED
    ), "a co-tenant's successful compaction disarmed this session's budget"


@pytest.mark.asyncio
async def test_completed_compaction_does_not_arm_the_budget(monkeypatch):
    """A successful compaction must not arm the budget: the turn keeps running
    and completes on the backend's own response."""
    from kiro_crew.acp import session_handle as sh

    monkeypatch.setattr(sh, "_COMPACTION_FAILED_TURN_BUDGET", 0.2)

    handle, _rt = _make(
        response_result={"stopReason": "end_turn"},
        updates=[_failed_compaction({"status": {"type": "completed"}, "summary": "3k saved"})],
    )
    events = [ev async for ev in handle.prompt("hello", timeout=5.0)]

    assert events[0].kind == EVENT_COMPACTION_STATUS
    assert events[0].text == "completed"
    assert events[-1].kind == EVENT_COMPLETE
    assert events[-1].stop_reason == "end_turn"
    assert handle._compaction_failed_at is None


# ── Tool-idle watchdog frame ownership (issue #4872) ─────────────────────────


class _Clock:
    """Module-local monotonic clock: real time plus a test-driven offset.

    Installed over ``session_handle``'s own ``time`` name only, so advancing it
    moves the clock the dispatch loop reads (``last_data_ts`` and friends) while
    asyncio's timers keep running on the real one. That is what lets a test put
    a MINUTE between two frames without waiting a minute, and without the
    5-second queue parks turning into 5-second fake-time jumps.
    """

    def __init__(self) -> None:
        self.offset = 0.0

    def monotonic(self) -> float:
        return time.monotonic() + self.offset

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - passthrough
        return getattr(time, name)


def _own_update(text: str = "streamed") -> JsonRpcMessage:
    """A session/update frame ROUTED to this session (owner known)."""
    return JsonRpcMessage(
        method=METHOD_SESSION_UPDATE,
        params={
            "sessionId": "sA",
            "update": {"sessionUpdate": "agent_message_chunk", "text": text},
        },
    )


def _co_tenant_roster() -> JsonRpcMessage:
    """A co-tenant's roster notification: no sessionId, so the runtime fanned it
    out to every registered session and marked it ownerless."""
    frame = JsonRpcMessage(method="_kiro.dev/subagent/list_update", params={"subagents": []})
    frame.fanout_no_owner = True
    return frame


async def _run_with_late_frame(monkeypatch, clock: _Clock, late_frame: JsonRpcMessage) -> list:
    """Drive one turn with a tool in flight and exactly one late frame.

    Timeline, in the module's clock: an OWN frame at ~0 (which also arms
    ``_tool_dispatched``), ``late_frame`` dequeued at ~50s, the watchdog's first
    evaluation at ~55s (the loop parks on the queue for 5 real seconds), and
    then -- for a turn the watchdog leaves alone -- the backend's own response,
    so both outcomes terminate and are told apart by their stop reason.

    With ``check_after_secs`` at 30s the two candidate reference points fall on
    opposite sides of the threshold: measured from the late frame the tool has
    been idle ~5s, measured from this session's own last frame ~55s. Which one
    the watchdog uses is exactly what this issue is about.
    """
    from kiro_crew.acp import session_handle as sh

    monkeypatch.setattr(sh, "time", clock)

    handle, _rt = _make(respond=False, updates=[_own_update()])
    handle._watchdog = replace(
        handle._watchdog,
        check_after_secs=30.0,
        tool_stall_suspect_secs=30.0,
        tool_stall_hard_cap_secs=30.0,
    )

    async def _feeder() -> None:
        # Land the late frame a fake-minute after the session's own frame.
        await asyncio.sleep(0.2)
        clock.offset = 50.0
        handle._queue.put_nowait(late_frame)
        # Past the watchdog's first evaluation (~5 real seconds), answer the
        # prompt so a turn the watchdog does NOT stall still terminates -- and
        # terminates with a stop reason that cannot be confused for a stall.
        await asyncio.sleep(8.0)
        handle._queue.put_nowait(JsonRpcMessage(id=_REQ_ID, result={"stopReason": "end_turn"}))

    feeder = asyncio.create_task(_feeder())
    try:
        events = []
        async for ev in handle.prompt("hello", timeout=600.0):
            events.append(ev)
            if ev.kind == EVENT_TEXT_CHUNK:
                # The turn dispatches a tool. Set from the consumer side because
                # prompt()'s prologue clears the flag at turn start.
                handle._tool_dispatched = True
        return events
    finally:
        feeder.cancel()


@pytest.mark.asyncio
async def test_co_tenant_fanout_frame_does_not_defer_the_tool_watchdog(monkeypatch):
    """The tool clock must measure THIS session's silence.

    On a shared runtime an ownerless notification is fanned out to every
    co-tenant queue (``msg.fanout_no_owner``). Counting it as progress on this
    session's in-flight tool makes the main-turn watchdog more patient than
    configured on traffic the session never produced -- and because roster
    churn recurs, the deferral has no bound: a wedged tool keeps its watchdog
    pushed out for as long as a neighbour stays busy.
    """
    events = await _run_with_late_frame(monkeypatch, _Clock(), _co_tenant_roster())

    assert events[-1].kind == EVENT_COMPLETE
    assert (
        events[-1].stop_reason == STOP_REASON_TOOL_STALL
    ), "a co-tenant's ownerless frame deferred the tool-idle watchdog: the " "turn ended as %r" % (
        events[-1].stop_reason,
    )
    # The idle on the terminal event is measured from the session's OWN last
    # frame (~55s), never from the co-tenant's (~5s) -- so it clears the window.
    idle = int(re.search(r"idle_secs=(\d+)", events[-1].text).group(1))
    assert idle >= 30, f"idle {idle}s was measured from the co-tenant's frame"


@pytest.mark.asyncio
async def test_own_session_frame_still_defers_the_tool_watchdog(monkeypatch):
    """The other half of the contract: a frame ROUTED to this session is its own
    progress and must keep deferring the watchdog, or a legitimately-streaming
    tool gets cancelled. Provenance is the discriminator, not the frame's
    method -- so the identical timeline with an owned frame must NOT stall."""
    events = await _run_with_late_frame(monkeypatch, _Clock(), _own_update("more"))

    assert events[-1].kind == EVENT_COMPLETE
    assert (
        events[-1].stop_reason == "end_turn"
    ), "an owned frame no longer satisfies the tool watchdog: the turn ended " "as %r" % (
        events[-1].stop_reason,
    )
