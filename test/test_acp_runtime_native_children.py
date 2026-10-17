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

# ── the recognition cap's residual: what a TRUNCATED roster says out loud ─────
#
# `_snapshot_subagent_sessions` recognises at most NATIVE_CHILD_ROSTER_CAP ids —
# the same bound `AcpSessionHandle` counts them under — and reports what it
# refused two ways: one warning naming the count, and a distinct auto-reject
# reason on a permission request it cannot attribute. Both are SNAPSHOT-scoped:
# the frame carries the backend's full list, so the previous frame's truncation
# must not colour this frame's refusals. `test_native_subagent_boundary.py` pins
# the cap and the two reasons end to end; what follows pins the two properties
# an operator reads them THROUGH — one line per truncated roster rather than per
# truncated id, and an attribution that expires with the snapshot that earned it.


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

    Two volume properties, one per axis of the same product:

    Per FRAME. The roster arrives as `subagent/list_update`, which kiro-cli
    re-broadcasts on every child status change — so above the cap every
    rebroadcast re-earns the warning at a frame rate this client does not
    choose. Measured on this handler with the throttle removed: 10 over-cap
    snapshots → 10 identical WARNING records (~245 message bytes each), and the
    tail count holds at 40, so the log VOLUME is what grows, not the number. It
    is the assertion below that fails in that state. The frames
    below only move a child's `status`, which is the real steady state and the
    case a "same count, don't log" throttle would appear to handle by accident:
    it suppresses while the tail happens to hold still and floods again the
    moment one child completes.

    Per ID. A single frame's tail must not become one line per truncated id
    either — the count is the whole diagnostic, "40 announced children are
    unrecognisable here", and a per-id line states it only if the reader counts
    the lines.

    And a roster INSIDE the cap must say nothing at all: the warning has to
    mean "children went unrecognised", never "a roster arrived", or an
    operator cannot use its presence as the signal.
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

    Same mechanism as `_note_dropped_frame` above, deliberately: one interval
    constant, a monotonic window, a count flushed on the next event past it, and
    no timer task on the demux loop. What the summary carries is the count of
    repeated snapshots and the LARGEST tail they named — the peak, because
    sizing the cap reads the worst case, and which tail happens to be current at
    an arbitrary flush instant is noise.
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

    Without this the throttle would swallow a genuinely new truncation for the
    rest of the runtime's life, which is worse than the volume it fixes: the
    warning's whole job is that a truncated tail is otherwise invisible. The
    boundary is the overflow count returning to 0, which is exactly the two
    events that already retire the snapshot ATTRIBUTION (a roster inside the
    cap, and the owning session unregistering) — one lifetime for both halves of
    the same signal, so the loud line and the auto-reject reason can never
    disagree about whether the cap is under pressure.
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

    A `list_update` carries the backend's FULL child list, so the count of ids
    it truncated describes that frame and nothing later. Left sticky, one
    truncated roster would re-label every unknown-session denial for the rest of
    the runtime's life as a cap truncation — and the SEL reason is precisely the
    signal an operator uses to decide whether to raise the cap, so a stuck one
    both invents cap pressure that is not there and buries the next real
    truncation in it. Unregistering the owner is not the only way back: the very
    next clean roster is already the whole truth.

    Both halves are asserted where the operator reads them — the SEL row's
    `error` field and the `child_permission_denied` metric — not on the private
    counter alone.
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

    `_note_native_child` answers "is this id tracked as a child of mine", and
    the KAS display roster keys its row on that answer, which is what keeps
    every native-child store inside one cap. The parent's own id is refused by
    the count, so it must be refused by the row too: a row the counted set does
    not hold can never be recognised as a duplicate, and here it would also
    render the session as a sub-agent of itself. The frame is still a parent
    sub-agent frame, so it keeps emitting its list event rather than falling
    through to be re-rendered as an ordinary tool call.
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

    kiro-cli carries the parent turn's OWN tool-call chunk on the extension
    method, under the parent's own sessionId and the parent's own toolCallId --
    recorded live in ``test/fixtures/acp_frames/kiro/session.jsonl`` -- so the
    sessionId is the only thing that separates it from a child's update. Two
    consequences, both checked here: it must not reach the child-frame parser, so
    the origin-scoped identity caches stay empty, and it must yield no
    sub-agent activity, because ``messaging.driver`` puts every activity event's
    toolCallId into the set that refuses session directives as
    ``native_subagent_isolation``. Yielding one for the parent's own tool call
    makes the parent's directives refuse themselves.
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
