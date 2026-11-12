"""Native child sessions on a shared ``AcpRuntime``.

The roster cap's truncation warning, summary and attribution, and child
``_kiro.dev/session/update`` frames run through the session handle.
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _stub_sel_for_permission_tests  # noqa: F401
from acp_runtime_helpers import (
    _bounded,
    _drain,
    _drain_audits,
    _feed,
    _last_written_frame,
    _make_runtime,
    _register,
    _start_reader,
    _stop_reader,
)

from kiro_crew.acp.runtime import AcpSessionHandle
from kiro_crew.acp.session_handle import NATIVE_CHILD_ROSTER_CAP
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    EVENT_PERMISSION_REQUEST,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    METHOD_KIRO_SESSION_UPDATE,
    METHOD_REQUEST_PERMISSION,
    JsonRpcMessage,
)
from kiro_crew.metrics.events import CHILD_PERMISSION_DENIED

# ── what a TRUNCATED roster says out loud ─────
#
# Past NATIVE_CHILD_ROSTER_CAP ids, `_snapshot_subagent_sessions` warns with the count
# and auto-rejects with a distinct reason, both scoped to the snapshot. The cap itself
# is pinned in `test_native_subagent_boundary.py`.


def _unroutable_permission_frame(child_sid: str, request_id: int) -> dict:
    """A permission REQUEST for a session this runtime has no queue for."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": child_sid,
            "toolCall": {"toolCallId": "tc-1", "title": "bash"},
            "options": [
                {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
            ],
        },
    }


async def _await_count(seq: list, n: int, what: str, timeout: float = 5.0) -> None:
    """Wait on the observable condition — the answer/audit runs as a spawned
    task off the reader loop, so a sleep guess is the flake this suite's
    `_drain` docstring describes."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while len(seq) < n:
        if loop.time() >= deadline:
            raise AssertionError(f"only {len(seq)} {what} recorded, expected {n}")
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_roster_overflow_warns_once_per_episode_not_once_per_frame(caplog):
    """A truncation episode gets ONE warning, however many frames re-announce it.

    kiro-cli re-broadcasts `subagent/list_update` on every child status change. The
    frames only move `status`, so a "same count, don't log" throttle would fail. One
    line per roster, not per id, and nothing inside the cap.
    """
    import logging

    rt, _, _ = _make_runtime()
    _register(rt, "parent-session")
    overflow = 40
    roster = [{"sessionId": f"c-{i}"} for i in range(NATIVE_CHILD_ROSTER_CAP + overflow)]
    statuses = ("running", "pending", "completed")

    with caplog.at_level(logging.DEBUG, logger="kiro_crew.acp.runtime"):
        rt._snapshot_subagent_sessions({"subagents": roster})
        truncation = [r for r in caplog.records if "recognition cap" in r.getMessage()]
        assert len(truncation) == 1
        assert truncation[0].levelno == logging.WARNING
        assert str(overflow) in truncation[0].getMessage()
        assert len(rt._subagent_sessions) == NATIVE_CHILD_ROSTER_CAP
        assert rt._subagent_roster_overflow == overflow

        # Nine more frames naming the SAME children with moved statuses. The id
        # set is identical, the frame is not, and the tail count is unchanged.
        for n in range(9):
            rt._snapshot_subagent_sessions(
                {
                    "subagents": [
                        {"sessionId": e["sessionId"], "status": statuses[(i + n) % 3]}
                        for i, e in enumerate(roster)
                    ]
                }
            )
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1, [r.getMessage() for r in warnings]
        assert rt._subagent_roster_overflow == overflow
        # Inside the interval the repeats are held, not emitted — this is the
        # assertion that fails on the per-frame implementation.
        assert rt._roster_overflow_repeats == 9
        assert rt._roster_overflow_peak == overflow

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="kiro_crew.acp.runtime"):
        rt._snapshot_subagent_sessions({"subagents": roster[:8]})
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert rt._subagent_roster_overflow == 0
    # The episode ended under one interval, so its residual repeat count is
    # flushed rather than dropped: a truncation storm that stops quickly must
    # still report more than its first frame.
    assert [r.getMessage() for r in caplog.records if "further snapshot(s)" in r.getMessage()] == [
        f"subagent roster truncated on 9 further snapshot(s); largest tail "
        f"{overflow} id(s) past the {NATIVE_CHILD_ROSTER_CAP}-id recognition cap"
    ]


@pytest.mark.asyncio
async def test_repeated_roster_truncation_folds_into_one_throttled_summary(caplog):
    """The repeats become a throttled DEBUG summary carrying the count and peak.

    Same mechanism as `_note_dropped_frame`, with no timer task. It carries the peak
    tail, since sizing the cap reads the worst case.
    """
    import logging

    import kiro_crew.acp.runtime as runtime_mod

    rt, _, _ = _make_runtime()
    _register(rt, "parent-session")

    def _over(extra: int) -> dict:
        return {
            "subagents": [{"sessionId": f"c-{i}"} for i in range(NATIVE_CHILD_ROSTER_CAP + extra)]
        }

    with caplog.at_level(logging.DEBUG, logger="kiro_crew.acp.runtime"):
        rt._snapshot_subagent_sessions(_over(40))  # the loud one, opens the window
        for extra in (40, 77, 40, 12):
            rt._snapshot_subagent_sessions(_over(extra))
        assert [
            r.getMessage() for r in caplog.records if "further snapshot(s)" in r.getMessage()
        ] == []

        # Age the window out; the next truncated snapshot flushes the summary.
        rt._roster_overflow_summary_at -= runtime_mod._ROSTER_OVERFLOW_SUMMARY_INTERVAL_SECS + 1.0
        rt._snapshot_subagent_sessions(_over(40))

    summaries = [r for r in caplog.records if "further snapshot(s)" in r.getMessage()]
    assert len(summaries) == 1, [r.getMessage() for r in summaries]
    assert summaries[0].levelno == logging.DEBUG
    assert "truncated on 5 further snapshot(s)" in summaries[0].getMessage()
    assert "largest tail 77 id(s)" in summaries[0].getMessage()
    # Still exactly one loud record for the whole episode.
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1
    # Flushing reopens the window rather than closing the episode.
    assert rt._roster_overflow_repeats == 0
    assert rt._roster_overflow_peak == 0
    assert rt._roster_overflow_summary_at != 0.0


@pytest.mark.asyncio
async def test_a_truncation_after_the_roster_recovers_is_loud_again(caplog):
    """The reset boundary is the EPISODE, and both ways out of one re-arm it.

    The boundary is the overflow count returning to 0 (a roster inside the cap, or the
    owner unregistering), the same events that retire the attribution.
    """
    import logging

    rt, _, _ = _make_runtime()
    _register(rt, "parent-session")
    over = {"subagents": [{"sessionId": f"c-{i}"} for i in range(NATIVE_CHILD_ROSTER_CAP + 40)]}
    inside = {"subagents": [{"sessionId": "c-0"}]}

    def _loud() -> list[str]:
        return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]

    with caplog.at_level(logging.DEBUG, logger="kiro_crew.acp.runtime"):
        rt._snapshot_subagent_sessions(over)
        rt._snapshot_subagent_sessions(over)
        assert len(_loud()) == 1
        # Way out #1: a roster the cap did not truncate.
        rt._snapshot_subagent_sessions(inside)
        assert rt._roster_overflow_summary_at == 0.0
        rt._snapshot_subagent_sessions(over)
        assert len(_loud()) == 2, _loud()

        # Way out #2: the owner leaves, taking its roster with it. The next
        # owner's first truncation is a new episode.
        rt._snapshot_subagent_sessions(over)
        rt.unregister_session("parent-session")
        assert rt._subagent_roster_overflow == 0
        assert rt._roster_overflow_summary_at == 0.0
        _register(rt, "successor-session")
        rt._snapshot_subagent_sessions(over)
        assert len(_loud()) == 3, _loud()
        assert rt._subagent_owner == "successor-session"


@pytest.mark.asyncio
async def test_unroutable_permission_reason_follows_the_last_roster_snapshot():
    """The cap-truncation attribution expires with the snapshot that earned it.

    A `list_update` carries the full child list, so the next clean roster clears it.
    Asserted on the SEL row's `error` and the `child_permission_denied` metric.
    """
    import kiro_crew.sel as sel_mod

    audited: list[dict] = []
    denied: list[dict] = []

    class _CapturingSel:
        def log_tool_invocation(self, **kwargs):  # noqa: D401 - stub
            audited.append(kwargs)

    def _spy_counter(name, attrs=None, **_kw):
        if name == CHILD_PERMISSION_DENIED:
            denied.append(dict(attrs or {}))

    rt, reader, proc = _make_runtime()
    _register(rt, "parent-session")
    task = await _start_reader(rt)
    try:
        with (
            patch.object(sel_mod, "sel", lambda: _CapturingSel()),
            patch("kiro_crew.acp.runtime.emit_counter", _spy_counter),
        ):
            # Snapshotted by direct call, not fed as a frame: a roster naming
            # NATIVE_CHILD_ROSTER_CAP children serialises past this reader's
            # line limit, so a fed frame would measure the stdout buffer
            # instead of the cap.
            rt._snapshot_subagent_sessions(
                {"subagents": [{"sessionId": f"c-{i}"} for i in range(NATIVE_CHILD_ROSTER_CAP + 2)]}
            )
            assert rt._subagent_roster_overflow == 2

            _feed(reader, _unroutable_permission_frame("ghost-a", 301))
            await _drain(reader)
            await _await_count(denied, 1, "denial metric")
            await _drain_audits(rt)
            assert denied == [{"surface": "runtime", "reason": "roster_overflow_auto_reject"}]
            assert [row["error"] for row in audited] == ["roster_overflow_auto_reject"]

            # A later roster that truncated NOTHING: this child was never
            # announced, and saying "the cap truncated it" would be false.
            rt._snapshot_subagent_sessions({"subagents": [{"sessionId": "c-0"}]})
            assert rt._subagent_roster_overflow == 0

            _feed(reader, _unroutable_permission_frame("ghost-b", 302))
            await _drain(reader)
            await _await_count(denied, 2, "denial metric")
            await _drain_audits(rt)
            assert denied[1] == {
                "surface": "runtime",
                "reason": "unregistered_session_auto_reject",
            }
            assert audited[1]["error"] == "unregistered_session_auto_reject"

            # The owner leaving is the other way back: the truncated roster it
            # owned is gone, so a denial on the warm runtime after it must not
            # still be attributed to that roster's cap pressure.
            rt._snapshot_subagent_sessions(
                {"subagents": [{"sessionId": f"c-{i}"} for i in range(NATIVE_CHILD_ROSTER_CAP + 2)]}
            )
            rt.unregister_session("parent-session")
            _feed(reader, _unroutable_permission_frame("ghost-c", 303))
            await _drain(reader)
            await _await_count(denied, 3, "denial metric")
            await _drain_audits(rt)
            assert denied[2]["reason"] == "unregistered_session_auto_reject"
            assert audited[2]["error"] == "unregistered_session_auto_reject"

        # No request was left hanging or counted as a drop: each got the
        # request's own least-destructive reject option, immediately.
        answered = [json.loads(c.args[0].decode()) for c in proc.stdin.write.call_args_list]
        assert [(f["id"], f["result"]["outcome"]) for f in answered] == [
            (301, {"outcome": "selected", "optionId": "reject_once"}),
            (302, {"outcome": "selected", "optionId": "reject_once"}),
            (303, {"outcome": "selected", "optionId": "reject_once"}),
        ]
        assert rt._dropped_frames == {}
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_a_kas_frame_naming_the_parent_itself_gets_no_native_child_row():
    """A parent is never its own sub-agent — in the count OR in the display row.

    The KAS row keys on `_note_native_child`, which refuses the parent's id. The frame
    still emits its list event instead of rendering as a tool call.
    """
    rt, _, _ = _make_runtime()
    rt._acp_backend = ACP_BACKEND_KAS
    handle = AcpSessionHandle("sA", asyncio.Queue(), rt)

    def _subtask_frame(subtask_id: str, tool_call_id: str) -> dict:
        return {
            "sessionUpdate": "tool_call",
            "toolCallId": tool_call_id,
            "title": f"Sub-agent: {subtask_id}",
            "status": "in_progress",
            "_meta": {"kiro": {"agentSubtaskId": subtask_id, "kind": "agent-subtask"}},
        }

    events = handle._handle_kas_subagent(_subtask_frame("sA", "k1"))
    assert events is not None and [e.kind for e in events] == [EVENT_SUBAGENT_LIST]
    assert handle.native_child_sessions == frozenset()
    assert handle._kas_subagent_roster == {}
    assert handle.native_child_overflow == 0

    # A real child on the same handle still gets its row, so the refusal above
    # is about identity and not about the roster being inert.
    handle._handle_kas_subagent(_subtask_frame("child-1", "k2"))
    assert set(handle._kas_subagent_roster) == {"child-1"} == set(handle.native_child_sessions)


# ── Child `_kiro.dev/session/update` through the session handle ──────────────
#
# The runtime routing tests above prove the frame REACHES the slot queue; these
# prove the handle then runs it through the same child-frame parser as plain
# `session/update`, so the origin-scoped caches populate and a later child
# permission request verifies its MCP identity instead of raising the
# interactive UNVERIFIED card.


def _make_handle_for_child_frames():
    """A session handle over a mocked runtime, driven via _dispatch_events."""
    from kiro_crew.acp.session_handle import AcpSessionHandle

    queue: asyncio.Queue = asyncio.Queue()
    rt = MagicMock()
    rt.pid = None
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    rt.send_request = AsyncMock(return_value=1)
    rt.send_response = AsyncMock()
    rt.acp_backend = ""
    rt._last_activity = time.monotonic()
    handle = AcpSessionHandle("parent-sid", queue, rt)
    handle._turn_done.clear()
    return handle, queue


def _child_kiro_update_frame(
    tcid: str = "tc-child-1",
    *,
    session_id: str = "child-a",
    meta: bool = True,
    raw_input: dict | None = None,
) -> JsonRpcMessage:
    update: dict = {
        "sessionUpdate": "tool_call",
        "toolCallId": tcid,
        "title": "@example-server/get-item",
        "kind": "other",
        "rawInput": {"itemId": "item-0001"} if raw_input is None else raw_input,
    }
    if meta:
        update["_meta"] = {"kiro": {"mcpServerName": "example-server", "toolName": "get-item"}}
    return JsonRpcMessage(
        method=METHOD_KIRO_SESSION_UPDATE,
        params={"sessionId": session_id, "update": update},
    )


def _child_permission_frame(tcid: str = "tc-child-1", req_id: int = 90) -> JsonRpcMessage:
    return JsonRpcMessage(
        id=req_id,
        method=METHOD_REQUEST_PERMISSION,
        params={
            "sessionId": "child-a",
            "toolCall": {
                "toolCallId": tcid,
                "title": "@example-server/get-item",
                "input": {"itemId": "item-0001"},
            },
            "options": [
                {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
            ],
        },
    )


@pytest.mark.asyncio
async def test_child_kiro_session_update_populates_caches_and_retags():
    """A child `_kiro.dev/session/update` tool_call frame runs through the
    shared child-frame parser: the origin-scoped caches capture raw params,
    shell classification, and the `_meta.kiro` identity, and the parsed event
    is re-tagged as subagent activity — never as parent transcript."""
    handle, queue = _make_handle_for_child_frames()
    queue.put_nowait(_child_kiro_update_frame())
    queue.put_nowait(JsonRpcMessage(id=1, result={"stopReason": "end_turn"}))

    events = [ev async for ev in handle._dispatch_events(1, 5.0)]

    activity = [ev for ev in events if ev.kind == EVENT_SUBAGENT_ACTIVITY]
    assert activity and activity[0].sub_session_id == "child-a"
    assert activity[0].tool_call_id == "tc-child-1"
    # Never parent transcript: no tool card, no text chunk.
    assert not [ev for ev in events if ev.kind in (EVENT_TOOL_CALL, EVENT_TEXT_CHUNK)]
    # The security payload: origin-scoped (cache_scope=frame sid) cache writes.
    key = "child-a|tc-child-1"
    assert handle._tool_call_mcp_server[key] == "example-server"
    assert handle._tool_call_tool_name[key] == "get-item"
    assert handle._tool_call_is_shell[key] is False
    assert handle._tool_call_raw_params[key] == {"itemId": "item-0001"}


@pytest.mark.asyncio
async def test_child_kiro_session_update_identity_flips_permission_trust():
    """End-to-end through the handle: the child's `_kiro.dev/session/update`
    tool_call populates the caches, so the SUBSEQUENT permission request for
    the same toolCallId carries verified MCP identity — the flag every
    unconditional auto-approve path reads."""
    handle, queue = _make_handle_for_child_frames()
    queue.put_nowait(_child_kiro_update_frame())
    queue.put_nowait(_child_permission_frame())
    queue.put_nowait(JsonRpcMessage(id=1, result={"stopReason": "end_turn"}))

    events = [ev async for ev in handle._dispatch_events(1, 5.0)]

    perms = [ev for ev in events if ev.kind == EVENT_PERMISSION_REQUEST]
    assert perms, "full-fidelity child permission must be yielded, not auto-rejected"
    ev = perms[0]
    assert ev.sub_session_id == "child-a"
    assert ev.raw_params_trusted is True
    assert ev.mcp_server_name == "example-server"
    assert ev.tool_name == "get-item"
    assert ev.mcp_identity_trusted is True
    assert ev.child_low_fidelity is False
    assert ev.child_mcp_identity_trusted is True
    assert ev.child_unconditional_grant_eligible is True


@pytest.mark.asyncio
async def test_child_kiro_session_update_without_meta_stays_unverified():
    """Identity comes ONLY from `_meta.kiro` on a frame this client parsed: a
    child tool_call without it still caches params (arguments-fidelity is
    independent) but the permission event's identity half stays unverified,
    so identity-gated grant paths fail closed."""
    handle, queue = _make_handle_for_child_frames()
    queue.put_nowait(_child_kiro_update_frame(meta=False))
    queue.put_nowait(_child_permission_frame())
    queue.put_nowait(JsonRpcMessage(id=1, result={"stopReason": "end_turn"}))

    events = [ev async for ev in handle._dispatch_events(1, 5.0)]

    perms = [ev for ev in events if ev.kind == EVENT_PERMISSION_REQUEST]
    assert perms
    ev = perms[0]
    assert ev.mcp_server_name == ""
    assert ev.tool_name == ""
    assert ev.child_mcp_identity_trusted is False
    # Full arguments-fidelity keeps the event deliverable; only the identity
    # carve-out is withheld.
    assert ev.child_low_fidelity is False


@pytest.mark.asyncio
async def test_child_steer_echo_never_settles_parent_steer_ledger():
    """A routed child frame with a steer discriminant classifies as "steer"
    before "subagent_activity" — the steer branch must ignore a frame naming
    another session, or a child's steering_consumed surfaces as a PARENT
    steer lifecycle event and can settle a pending user steer the parent
    backend never consumed. An own-session steer echo still yields."""
    from kiro_crew.acp.types import EVENT_STEER_CONSUMED

    def _steer_frame(sid: str) -> JsonRpcMessage:
        return JsonRpcMessage(
            method=METHOD_KIRO_SESSION_UPDATE,
            params={
                "sessionId": sid,
                "update": {"sessionUpdate": "steering_consumed", "content": "go left"},
            },
        )

    handle, queue = _make_handle_for_child_frames()
    queue.put_nowait(_steer_frame("child-a"))
    queue.put_nowait(JsonRpcMessage(id=1, result={"stopReason": "end_turn"}))
    events = [ev async for ev in handle._dispatch_events(1, 5.0)]
    assert not [ev for ev in events if ev.kind == EVENT_STEER_CONSUMED]

    handle2, queue2 = _make_handle_for_child_frames()
    queue2.put_nowait(_steer_frame("parent-sid"))
    queue2.put_nowait(JsonRpcMessage(id=1, result={"stopReason": "end_turn"}))
    events2 = [ev async for ev in handle2._dispatch_events(1, 5.0)]
    steer = [ev for ev in events2 if ev.kind == EVENT_STEER_CONSUMED]
    assert steer and steer[0].text == "go left"


@pytest.mark.asyncio
async def test_child_tool_call_chunk_stays_fail_closed_but_visible():
    """A child update whose discriminant is `tool_call_chunk` (an id+title
    progress shape with no rawInput and no `_meta.kiro`) writes NOTHING into
    the identity caches: there is no security payload to mint trust from, so
    the subsequent permission request stays unverified and falls to the
    interactive card — fail-closed, never fail-open. The crew monitor still
    shows the activity (the display path keys on the toolCallId alone)."""
    handle, queue = _make_handle_for_child_frames()
    queue.put_nowait(
        JsonRpcMessage(
            method=METHOD_KIRO_SESSION_UPDATE,
            params={
                "sessionId": "child-a",
                "update": {
                    "sessionUpdate": "tool_call_chunk",
                    "toolCallId": "tc-chunk-1",
                    "title": "@example-server/get-item",
                },
            },
        )
    )
    queue.put_nowait(_child_permission_frame(tcid="tc-chunk-1"))
    queue.put_nowait(JsonRpcMessage(id=1, result={"stopReason": "end_turn"}))

    events = [ev async for ev in handle._dispatch_events(1, 5.0)]

    activity = [ev for ev in events if ev.kind == EVENT_SUBAGENT_ACTIVITY]
    assert activity and activity[0].tool_call_id == "tc-chunk-1"
    # No cache entry minted from a payload that carries no provenance.
    assert handle._tool_call_mcp_server == {}
    assert handle._tool_call_raw_params == {}
    # The permission request for that id resolves fail-closed: this
    # fidelity-unaware consumer auto-rejects it (the ⛔ notice is the
    # observable), and no yielded permission event ever carries verified
    # identity. Either way, nothing can auto-APPROVE.
    assert any("auto-rejected" in (ev.text or "") for ev in activity)
    for ev in events:
        if ev.kind == EVENT_PERMISSION_REQUEST:
            assert ev.child_mcp_identity_trusted is False
            assert ev.child_unconditional_grant_eligible is False


@pytest.mark.asyncio
async def test_own_session_kiro_session_update_is_not_child_activity():
    """A `_kiro.dev/session/update` frame naming THIS session is not a child frame.

    kiro-cli sends the parent's own tool-call chunk this way (recorded in
    ``test/fixtures/acp_frames/kiro/session.jsonl``). Activity for it would make the
    parent's directives refuse as ``native_subagent_isolation``.
    """
    handle, queue = _make_handle_for_child_frames()
    queue.put_nowait(_child_kiro_update_frame(session_id="parent-sid"))
    queue.put_nowait(JsonRpcMessage(id=1, result={"stopReason": "end_turn"}))

    events = [ev async for ev in handle._dispatch_events(1, 5.0)]

    assert [ev for ev in events if ev.kind == EVENT_SUBAGENT_ACTIVITY] == []
    assert handle._tool_call_mcp_server == {}
    assert handle._tool_call_raw_params == {}


@pytest.mark.asyncio
async def test_between_turns_child_permission_is_answered_not_queued():
    """With no in-flight prompt nothing consumes the slot queue until the next
    turn's drain — a queued request would strand the backend. It is answered
    fail-closed immediately instead."""
    rt, reader, proc = _make_runtime()
    queues = _register(rt, "parent-session")
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "method": "_kiro.dev/subagent/list_update",
                "params": {"subagents": [{"sessionId": "child-a"}]},
            },
        )
        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "id": 96,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "child-a",
                    "options": [
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}
                    ],
                },
            },
        )
        await _drain(reader)
        await _drain_audits(rt)

        frame = _last_written_frame(proc)
        assert frame["id"] == 96
        assert frame["result"]["outcome"] == {"outcome": "selected", "optionId": "reject_once"}
        # Nothing left queued for a consumer that may not exist for hours.
        while not queues["parent-session"].empty():
            assert queues["parent-session"].get_nowait().id != 96
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_pending_non_prompt_request_does_not_enable_routing():
    """A pending set_mode/steer/config request leaves an entry in
    _routed_requests but proves nothing about a consuming prompt loop — a
    child permission arriving then must be answered fail-closed, not parked
    on a queue nobody reads."""
    rt, reader, proc = _make_runtime()
    _register(rt, "parent-session")
    rt._routed_requests[5] = "parent-session"  # e.g. an unanswered set_mode
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "method": "_kiro.dev/subagent/list_update",
                "params": {"subagents": [{"sessionId": "child-a"}]},
            },
        )
        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "id": 97,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "child-a",
                    "options": [
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}
                    ],
                },
            },
        )
        await _drain(reader)
        await _drain_audits(rt)

        frame = _last_written_frame(proc)
        assert frame["id"] == 97
        assert frame["result"]["outcome"] == {"outcome": "selected", "optionId": "reject_once"}
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_notice_yield_abandonment_clears_turn_state():
    """The drain-time rejection notices are yields, i.e. abandonment points.
    A consumer that closes the stream at a notice yield must not leave the
    handle permanently turn-active (mark_turn_active leaked True /
    _turn_done cleared) — the notice loop must live inside the same
    try/finally as the dispatch loop."""
    from kiro_crew.acp.types import EVENT_SUBAGENT_ACTIVITY

    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(return_value=7)
    handle._pending_reject_notices.append(("child-a", "Running: sha256sum x"))

    gen = handle.prompt("hi", timeout=3.0)
    first = await gen.__anext__()
    assert first.kind == EVENT_SUBAGENT_ACTIVITY
    assert "auto-rejected" in (first.text or "")
    # Consumer abandons the stream at the notice yield.
    await gen.aclose()

    assert handle.is_turn_active is False
    assert "sA" not in rt._turn_active_sessions


@pytest.mark.asyncio
async def test_handle_owned_rejections_are_sel_audited():
    """Every permission decision leaves a SEL record (repo convention). The
    fail-close fidelity gate and the pre-turn drain answer requests that no
    consumer ever sees, so the handle must emit the denial audit itself —
    otherwise those rejections are invisible to SEL."""
    import contextlib

    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)

    audited: list[tuple[object, str, str]] = []
    handle._audit_handle_reject = (  # type: ignore[method-assign]
        lambda request_id, title, error, sub_session_id="": audited.append(
            (request_id, title, error)
        )
    )
    rt.send_response = AsyncMock()

    # Pre-turn drain path: a stranded permission request in the queue.
    q["sA"].put_nowait(
        JsonRpcMessage.from_dict(
            {
                "jsonrpc": "2.0",
                "id": 55,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "child-a",
                    "toolCall": {"toolCallId": "tc-9", "title": "Running: rm -rf x"},
                    "options": [
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}
                    ],
                },
            }
        )
    )
    rt.send_request = AsyncMock(return_value=9)
    gen = handle.prompt("hi", timeout=0.2)
    with contextlib.suppress(StopAsyncIteration, asyncio.TimeoutError, Exception):
        await _bounded(gen.__anext__(), what="the first prompt event")
    await gen.aclose()

    assert audited, "pre-turn drain reject was not SEL-audited"
    assert audited[0][2] == "stranded_request_pre_turn_drain"


# ── whether a roster child's frames reach a session, said out loud ─────
#
# The per-call silence timer asks `routes_roster_children_to` before it judges a
# native sub-agent call by its sub-agent's silence (`acp-client.md`, "Per-call
# silence timer"). Each shape drives the REAL reader with a roster and then one child
# update, and the answer must be what the reader then did with that update.


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["alone", "beside-a-co-tenant", "roster-taken-while-shared"])
async def test_the_roster_route_answer_is_what_the_reader_does(shape):
    rt, reader, _proc = _make_runtime()
    queues = _register(rt, "sA")
    rt._turn_active_sessions.add("sA")
    if shape != "alone":
        _register(rt, "sB")
    task = await _start_reader(rt)
    try:
        assert rt.routes_roster_children_to("sA") is (shape == "alone")
        _feed(
            reader,
            {
                "method": "_kiro.dev/subagent/list_update",
                "params": {"subagents": [{"sessionId": "child-1"}]},
            },
        )
        await _drain(reader)
        if shape == "roster-taken-while-shared":
            # The co-tenant leaves after the roster: sA is alone again, but the
            # roster that names the child proved no owner, so the route stays shut.
            rt.unregister_session("sB")
        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "child-1",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "working"},
                    },
                },
            },
        )
        await _drain(reader)
        reached = []
        while not queues["sA"].empty():
            msg = queues["sA"].get_nowait()
            if (msg.params or {}).get("sessionId") == "child-1":
                reached.append(msg)

        assert bool(reached) is (shape == "alone")
        assert rt.routes_roster_children_to("sA") is bool(reached)
        assert rt.routes_roster_children_to("sB") is False
    finally:
        await _stop_reader(task)


# ── how many frames naming a session reached none, counted ─────
#
# The per-call silence timer compares this count across a turn (`acp-client.md`,
# "Per-call silence timer"): `routes_roster_children_to` answers for the instant it is
# asked, and a co-tenant registered for less than one timer read is gone by the next.
# Each shape drives the REAL reader with one child frame and asks what the count did.

_CHILD_TOOL_CALL = {
    "jsonrpc": "2.0",
    "method": "session/update",
    "params": {
        "sessionId": "child-1",
        "update": {"sessionUpdate": "tool_call", "toolCallId": "child-wait", "title": "wait"},
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("shape", "lost"),
    [
        ("routed-alone", 0),
        ("dropped-beside-a-co-tenant", 1),
        ("named-by-no-roster", 1),
        ("asked-beside-a-co-tenant", 1),
        ("asked-between-turns", 1),
        ("ownerless-fanned-out", 0),
    ],
)
async def test_unroutable_frames_naming_a_session_are_counted(shape, lost):
    rt, reader, _proc = _make_runtime()
    queues = _register(rt, "sA")
    if shape != "asked-between-turns":
        rt._turn_active_sessions.add("sA")
    task = await _start_reader(rt)
    try:
        if shape != "named-by-no-roster":
            _feed(
                reader,
                {
                    "method": "_kiro.dev/subagent/list_update",
                    "params": {"subagents": [{"sessionId": "child-1"}]},
                },
            )
            await _drain(reader)
        before = rt.unrouted_child_frames()
        if shape in (
            "dropped-beside-a-co-tenant",
            "asked-beside-a-co-tenant",
            "ownerless-fanned-out",
        ):
            _register(rt, "sB")
        if shape in ("asked-beside-a-co-tenant", "asked-between-turns"):
            _feed(
                reader,
                {
                    "jsonrpc": "2.0",
                    "id": 98,
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": "child-1",
                        "options": [
                            {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}
                        ],
                    },
                },
            )
        elif shape == "ownerless-fanned-out":
            _feed(reader, {"method": "_kiro.dev/subagent/list_update", "params": {"subagents": []}})
        else:
            _feed(reader, _CHILD_TOOL_CALL)
        await _drain(reader)
        await _drain_audits(rt)
        if "sB" in rt._session_queues:
            rt.unregister_session("sB")

        assert rt.unrouted_child_frames() - before == lost
        if shape == "dropped-beside-a-co-tenant":
            # Asked now, the route is open again: only the count says a frame was lost.
            assert rt.routes_roster_children_to("sA") is True
        reached = []
        while not queues["sA"].empty():
            msg = queues["sA"].get_nowait()
            if (msg.params or {}).get("sessionId") == "child-1":
                reached.append(msg)
        assert bool(reached) is (shape == "routed-alone")
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)
