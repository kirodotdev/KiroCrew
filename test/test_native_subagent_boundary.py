"""Harness-native subtask boundary (RFC §14.8, SPEC-ADDENDUM §7, parity row H16).

kiro-cli ``use_subagent`` children and KAS ``agentSubtaskId`` subtasks run
INSIDE the parent's harness process. Kiro Crew can see them (a session id, a
roster, attributable tool events) but cannot cancel, pause or resume one alone.
The contract pinned here:

* they are COUNTED on the parent handle (``native_child_sessions``), never
  charged a ``HostBudget`` slot, a lane slot or a taskq row;
* the recovery boundary is the PARENT session: one ``session/cancel`` covers
  every child, a child's ``kirocrew/status`` is rejected (``child_origin``), a
  child cannot be resumed independently, and a late child frame after the turn
  resurrects nothing;
* the Claude backend exposes no per-child identity, so its counter stays 0 and
  the liveness oracle still bounds the parent (declared gap, not a defect);
* a Kiro Crew ``spawn_run`` child is the boundary for its OWN native
  grandchildren -- they are counted on that child's handle only;
* a 200-child parent inflates neither ``HostBudget`` nor the adaptive
  controller's timeout-rate signal.

SPEC-ADDENDUM §10 scenarios about native subtasks -> tests:

* "对不支持细粒度控制的原生子任务，验证声明的恢复边界与降级行为" ->
  ``test_parent_cancel_is_the_only_lever_and_covers_every_child``,
  ``test_child_status_frame_is_rejected_child_origin``,
  ``test_native_child_cannot_be_resumed_independently``,
  ``test_claude_backend_counts_no_children_but_oracle_still_bounds_the_parent``.
* "等待期间进程、FD、连接、内存有界" ->
  ``test_native_children_take_no_host_budget_slot``,
  ``test_roster_is_bounded_and_overflow_is_counted_not_stored``,
  ``test_report_uncharged_replaces_per_label_and_bounds_labels``.
* "≥3 层父子任务；叶子等待期间无关任务持续完成" ->
  ``test_nested_spawn_run_child_is_the_boundary_for_its_native_grandchildren``,
  ``test_shared_runtime_with_two_owners_drops_an_unannounced_grandchild_frame``.
* "黑盒工具真实卡死时有界处理且保留部分结果" ->
  ``test_parent_stall_is_bounded_with_two_hundred_children``.
* "取消、重启、迟到结果、租约过期同时发生，不产生复活、重复执行、额度泄漏" ->
  ``test_late_child_frame_after_the_turn_resurrects_nothing``,
  ``test_two_hundred_children_do_not_trip_the_timeout_rate_signal``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp import session_handle as sh_mod
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import (
    NATIVE_CHILD_NOT_RESUMABLE,
    NATIVE_CHILD_ROSTER_CAP,
    NATIVE_CHILDREN_UNCHARGED_KIND,
    AcpSessionHandle,
    WatchdogSettings,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    EVENT_COMPLETE,
    EVENT_STRUCTURED_STATUS,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    EVENT_TOOL_CALL,
    METHOD_KIRO_SESSION_UPDATE,
    METHOD_SESSION_UPDATE,
    METHOD_SUBAGENT_LIST_UPDATE,
    STATUS_EXTENSION_KEY,
    STATUS_EXTENSION_VERSION,
    STATUS_PHASE_WAITING,
    WAIT_REASON_INPUT,
    JsonRpcMessage,
)
from kiro_crew.adaptive.controller import AdaptiveController, HostSample
from kiro_crew.adaptive.signals import SIGNAL_TIMEOUTS, classify
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.mcp_gateway.host_budget import HostBudget, HostBudgetExhausted, HostBudgetLimits

pytestmark = pytest.mark.timeout(30)

PARENT = "sA"
CHILD_RUN = "sB"  # a Kiro Crew spawn_run child: its own handle, its own task row
N_CHILDREN = 200


# ── fakes ─────────────────────────────────────────────────────────────────────


def _runtime(backend: str = ACP_BACKEND_KIRO) -> MagicMock:
    rt = MagicMock()
    rt._last_activity = time.monotonic()
    rt.pid = None
    rt.acp_backend = backend
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    return rt


def _handle(backend: str = ACP_BACKEND_KIRO, session: str = PARENT) -> AcpSessionHandle:
    return AcpSessionHandle(session, asyncio.Queue(), _runtime(backend))


def _update_msg(update: dict, *, session_id: str = PARENT, meta=None, fanout=False):
    params: dict = {"sessionId": session_id, "update": update}
    if meta is not None:
        params["_meta"] = meta
    msg = JsonRpcMessage(method=METHOD_SESSION_UPDATE, params=params)
    msg.fanout_no_owner = fanout
    return msg


def _tool_call(tool_call_id: str = "c1", command: str = "git log", **extra) -> dict:
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": tool_call_id,
        "title": "bash",
        "kind": "execute",
        "rawInput": {"command": command},
        **extra,
    }


def _status_meta(**fields) -> dict:
    body = {"version": STATUS_EXTENSION_VERSION, "phase": STATUS_PHASE_WAITING}
    body.setdefault("wait_reason", WAIT_REASON_INPUT)
    body.update(fields)
    return {STATUS_EXTENSION_KEY: body}


def _claude_task_call(tool_call_id: str = "task-1") -> dict:
    """What the Claude adapter emits for its in-harness Task tool: an ordinary
    ``tool_call`` on the PARENT sessionId. No child sessionId, no roster, no
    ``_meta.kiro`` -- the child has no identity Kiro Crew can name."""
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": tool_call_id,
        "title": "Task",
        "kind": "other",
        "rawInput": {"description": "grep", "prompt": "find callers", "subagent_type": "general"},
    }


def _kas_subtask_call(subtask_id: str, tool_call_id: str = "k1") -> dict:
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": tool_call_id,
        "title": f"Sub-agent: {subtask_id}",
        "status": "in_progress",
        "_meta": {"kiro": {"agentSubtaskId": subtask_id, "kind": "agent-subtask"}},
    }


def _kas_pipeline_call(*stage_ids: str) -> dict:
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": "pipe-1",
        "title": "pipeline",
        "_meta": {
            "kiro": {
                "pipeline": {
                    "stages": [
                        {"agentSubtaskId": s, "name": s, "status": "in_progress"} for s in stage_ids
                    ]
                }
            }
        },
    }


def _feed_children(handle: AcpSessionHandle, n: int, prefix: str = "sub") -> list[str]:
    sids = [f"{prefix}-{i}" for i in range(n)]
    for i, sid in enumerate(sids):
        handle._handle_update(_update_msg(_tool_call(tool_call_id=f"c{i}"), session_id=sid))
    return sids


class _SilentQueue:
    """A queue that never yields a frame: the tool the parent dispatched has
    gone silent. Each poll advances wall time a little so the (tiny) stall
    window is crossed deterministically."""

    async def get(self):
        await asyncio.sleep(0.06)
        raise asyncio.TimeoutError

    def qsize(self) -> int:
        return 0


_FAST_WD = WatchdogSettings(check_after_secs=0.01, tool_stall_suspect_secs=0.05)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float) -> None:
        self.t += secs


class _FakeManager:
    """The ``ExecActuator`` surface the controller reads."""

    def __init__(self, user_max: int = 10) -> None:
        self._user = user_max
        self.running_count = 1
        self._queue: list[dict[str, Any]] = []
        self._agents: dict[str, Any] = {}

    @property
    def user_max_concurrent(self) -> int:
        return self._user

    def set_effective_cap(self, cap: Optional[int]) -> int:
        return self._user if cap is None else min(self._user, cap)


# ── (1) counting: every native identity seam lands on the parent handle ──────


def test_kiro_child_routed_frames_count_on_parent_plain_spelling():
    handle = _handle()
    assert handle.native_child_sessions == frozenset()
    sids = _feed_children(handle, 3)
    # Repeats of the same child are one child; the parent's own frames are not children.
    handle._handle_update(_update_msg(_tool_call(tool_call_id="again"), session_id=sids[0]))
    handle._handle_update(_update_msg(_tool_call(tool_call_id="own")))
    assert handle.native_child_sessions == frozenset(sids)
    assert handle.native_child_overflow == 0
    assert handle.status_rejections == {}


def test_child_frames_emit_display_activity_only_never_the_kinds_that_run():
    """A child's frames become crew-monitor activity; nothing a task runner
    acts on (tool call, completion, structured status) is minted for them, so
    no lane slot and no task row can follow from a native child."""
    handle = _handle()
    events = handle._handle_update(_update_msg(_tool_call(tool_call_id="cc"), session_id="sub-1"))
    kinds = {e.kind for e in events}
    assert kinds == {EVENT_SUBAGENT_ACTIVITY}
    assert all(e.sub_session_id == "sub-1" for e in events)
    assert not kinds & {EVENT_TOOL_CALL, EVENT_COMPLETE, EVENT_STRUCTURED_STATUS}


def test_session_handle_never_reaches_the_scheduler():
    """Structural pin for "never a taskq row / lane slot": the handle module
    imports neither the task store nor admission, so the native seam has no
    way to mint either. The only scheduler-facing surface is a read
    (``native_child_sessions``) and a report (``report_native_children``)."""
    src = inspect.getsource(sh_mod)
    for forbidden in ("kiro_crew.taskq", "subagent_manager.admission", "yield_slot("):
        assert forbidden not in src, forbidden


@pytest.mark.asyncio
async def test_kiro_child_stream_extension_spelling_counts_on_parent():
    """kiro-cli 2.21.x carries the child stream as ``_kiro.dev/session/update``;
    that route counts exactly like the plain spelling."""
    q: asyncio.Queue = asyncio.Queue()
    handle = AcpSessionHandle(PARENT, q, _runtime())
    handle._tool_dispatched = True
    for i in range(3):
        q.put_nowait(
            JsonRpcMessage.from_dict(
                {
                    "method": METHOD_KIRO_SESSION_UPDATE,
                    "params": {
                        "sessionId": f"ext-{i}",
                        "update": {"toolCallId": f"tc{i}", "title": "read file"},
                    },
                }
            )
        )
    q.put_nowait(JsonRpcMessage(id=1, result={"stopReason": "end_turn"}))
    events = [ev async for ev in handle._dispatch_events(req_id=1, timeout=5.0)]
    activity = [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY]
    assert {e.sub_session_id for e in activity} == {"ext-0", "ext-1", "ext-2"}
    assert handle.native_child_sessions == frozenset({"ext-0", "ext-1", "ext-2"})
    assert events[-1].kind == EVENT_COMPLETE and events[-1].stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_kiro_roster_counts_only_when_this_handle_owns_it():
    """The ``_kiro.dev/subagent/list_update`` roster names no session, so the
    runtime fans it out to every co-tenant. Only the sole owner (frame not
    marked ``fanout_no_owner``) may count it; a co-tenant must not adopt
    another session's children."""
    roster = {"subagents": [{"sessionId": "r-1"}, {"session_id": "r-2"}, {"sessionId": ""}, "junk"]}

    async def run(fanout: bool) -> AcpSessionHandle:
        q: asyncio.Queue = asyncio.Queue()
        handle = AcpSessionHandle(PARENT, q, _runtime())
        msg = JsonRpcMessage(method=METHOD_SUBAGENT_LIST_UPDATE, params=roster)
        msg.fanout_no_owner = fanout
        q.put_nowait(msg)
        q.put_nowait(JsonRpcMessage(id=1, result={"stopReason": "end_turn"}))
        events = [ev async for ev in handle._dispatch_events(req_id=1, timeout=5.0)]
        (lst,) = [e for e in events if e.kind == EVENT_SUBAGENT_LIST]
        assert lst.runtime_global is fanout
        return handle

    owner = await run(fanout=False)
    assert owner.native_child_sessions == frozenset({"r-1", "r-2"})
    tenant = await run(fanout=True)
    assert tenant.native_child_sessions == frozenset()


def test_kas_roster_counts_agent_subtask_and_pipeline_stages():
    handle = _handle(ACP_BACKEND_KAS)
    events = handle._handle_update(_update_msg(_kas_subtask_call("st-1")))
    assert [e.kind for e in events] == [EVENT_SUBAGENT_LIST]
    handle._handle_update(_update_msg(_kas_pipeline_call("st-2", "st-3")))
    assert handle.native_child_sessions == frozenset({"st-1", "st-2", "st-3"})
    # A repeat lifecycle frame (status flip) for the same subtask is one child.
    handle._handle_update(_update_msg(_kas_subtask_call("st-1", tool_call_id="k2")))
    assert len(handle.native_child_sessions) == 3


def test_counter_and_overflow_reset_per_turn():
    handle = _handle()
    _feed_children(handle, 2)
    assert len(handle.native_child_sessions) == 2
    # The per-turn reset lives at the top of _run_turn; exercise the same
    # fields it clears rather than driving a whole prompt.
    handle._native_child_sids.clear()
    handle._native_child_overflow = 0
    assert handle.native_child_sessions == frozenset()
    assert handle.native_child_overflow == 0


def test_roster_is_bounded_and_overflow_is_counted_not_stored():
    """Ids are backend-controlled bytes; a flooding harness cannot grow gateway
    memory through the roster. Non-string / empty / over-long / own-id are
    ignored outright."""
    handle = _handle()
    for i in range(NATIVE_CHILD_ROSTER_CAP + 7):
        handle._note_native_child(f"flood-{i}")
    assert len(handle.native_child_sessions) == NATIVE_CHILD_ROSTER_CAP
    assert handle.native_child_overflow == 7
    before = handle.native_child_sessions
    for junk in (None, 12, "", "x" * 129, PARENT, ["sub"]):
        handle._note_native_child(junk)
    assert handle.native_child_sessions == before
    assert handle.native_child_overflow == 7


# ── (1) budget: observed, reported, never charged ────────────────────────────


def test_native_children_take_no_host_budget_slot():
    """200 native children on a parent leave every charged counter and every
    admission decision exactly where they were; they appear only under
    ``uncharged`` as "native children (uncharged)"."""
    budget = HostBudget(HostBudgetLimits(max_procs=2, max_rss_mb=300, max_fds=6))
    parent_charge = budget.reserve(label=PARENT, kind="pooled")
    before = budget.snapshot()

    handle = _handle()
    _feed_children(handle, N_CHILDREN)
    assert handle.report_native_children(budget) == N_CHILDREN

    after = budget.snapshot()
    assert after["uncharged"] == {NATIVE_CHILDREN_UNCHARGED_KIND: N_CHILDREN}
    assert budget.uncharged(NATIVE_CHILDREN_UNCHARGED_KIND) == N_CHILDREN
    for key in ("procs", "rss_mb", "fds", "charges", "by_kind", "rejections"):
        assert after[key] == before[key], key
    assert (after["procs"], after["rss_mb"], after["fds"]) == (1, 150, 3)
    # Admission is unchanged: the second real backend is admitted, the third
    # is refused -- the same ceiling arithmetic as with zero children.
    second = budget.reserve(label="other", kind="exclusive")
    with pytest.raises(HostBudgetExhausted) as exc:
        budget.reserve(label="third", kind="pooled")
    assert exc.value.dimension == "procs" and exc.value.in_use == 2
    second.release()
    parent_charge.release()
    assert budget.snapshot()["procs"] == 0
    # Releasing charges never touches the report; the parent's next turn does.
    assert budget.uncharged(NATIVE_CHILDREN_UNCHARGED_KIND) == N_CHILDREN


def test_report_uncharged_replaces_per_label_and_bounds_labels():
    budget = HostBudget(HostBudgetLimits())
    budget.report_uncharged("native_children", 5, label=PARENT)
    budget.report_uncharged("native_children", 2, label=PARENT)  # re-report replaces
    budget.report_uncharged("native_children", 3, label="sC")
    assert budget.uncharged("native_children") == 5
    budget.report_uncharged("native_children", 0, label=PARENT)  # zero removes
    assert budget.snapshot()["uncharged"] == {"native_children": 3}
    budget.report_uncharged("native_children", 0, label="sC")
    assert budget.snapshot()["uncharged"] == {}
    # Bounded labels: a report storm past 256 distinct labels is dropped.
    for i in range(300):
        budget.report_uncharged("native_children", 1, label=f"s{i}")
    assert budget.uncharged("native_children") == 256
    # A handle reports through duck typing; a budget-less caller is a no-op.
    fresh = HostBudget(HostBudgetLimits())
    handle = _handle()
    _feed_children(handle, 4)
    assert handle.report_native_children(object()) == 4
    handle.report_native_children(fresh)
    assert fresh.uncharged("native_children") == 4


def test_report_counts_overflow_too():
    budget = HostBudget(HostBudgetLimits())
    handle = _handle()
    for i in range(NATIVE_CHILD_ROSTER_CAP + 3):
        handle._note_native_child(f"f-{i}")
    assert handle.report_native_children(budget) == NATIVE_CHILD_ROSTER_CAP + 3
    assert budget.uncharged(NATIVE_CHILDREN_UNCHARGED_KIND) == NATIVE_CHILD_ROSTER_CAP + 3


# ── (2) the recovery boundary is the parent session ──────────────────────────


def test_child_status_frame_is_rejected_child_origin():
    """M's origin rule: a ``kirocrew/status`` riding a child-routed frame is
    the child's, and the child has no task row -- rejected, never
    re-attributed to the parent. The child is still counted."""
    handle = _handle()
    events = handle._handle_update(
        _update_msg(
            _tool_call(tool_call_id="cc"),
            session_id="sub-1",
            meta=_status_meta(tool_call_id="cc", cancellable=True, resumable=True),
        )
    )
    assert [e.kind for e in events] == [EVENT_SUBAGENT_ACTIVITY]
    assert handle.status_rejections == {"child_origin": 1}
    assert handle.native_child_sessions == frozenset({"sub-1"})
    # Even a status that NAMES the parent is refused when it rides a child frame.
    handle._handle_update(
        _update_msg(
            _tool_call(tool_call_id="cd"), session_id="sub-1", meta=_status_meta(session_id=PARENT)
        )
    )
    assert handle.status_rejections == {"child_origin": 2}
    # The parent's own status on its own frame is still accepted.
    own = handle._handle_update(_update_msg(_tool_call(tool_call_id="p1"), meta=_status_meta()))
    assert EVENT_STRUCTURED_STATUS in {e.kind for e in own}


@pytest.mark.asyncio
async def test_parent_cancel_is_the_only_lever_and_covers_every_child():
    """Cancelling the parent sends exactly ONE ``session/cancel`` naming the
    parent; no per-child cancel exists to send."""
    rt = _runtime()
    handle = AcpSessionHandle(PARENT, asyncio.Queue(), rt)
    _feed_children(handle, N_CHILDREN)
    handle._turn_done.clear()  # a turn is in flight
    await handle.cancel()
    rt.send_notification.assert_awaited_once()
    method, params = rt.send_notification.call_args.args[:2]
    assert method == "session/cancel"
    assert params["sessionId"] == PARENT
    assert not any(sid in json.dumps(params) for sid in handle.native_child_sessions)
    # The turn now reads inactive, so a provider-level second cancel answers
    # "no_turn" instead of firing again -- the boundary was taken once.
    assert handle.is_turn_active is False


@pytest.mark.asyncio
async def test_parent_stall_is_bounded_with_two_hundred_children():
    """The liveness oracle bounds the PARENT turn regardless of how many native
    children it fanned out: the stall is recovered by one session-scoped
    cancel on the parent and the turn ends ``error: tool stall`` -- never a
    kill of the shared runtime, never N recoveries."""
    rt = _runtime()
    handle = AcpSessionHandle(PARENT, asyncio.Queue(), rt, watchdog=_FAST_WD)
    handle._handle_update(
        _update_msg(_tool_call(tool_call_id="fanout", command="kiro use_subagent"))
    )
    _feed_children(handle, N_CHILDREN)
    assert handle._tool_dispatched is True
    handle._stale_eligible = False
    handle._queue = _SilentQueue()  # type: ignore[assignment]

    events = [ev async for ev in handle._dispatch_events(req_id=1, timeout=30.0)]

    rt.send_notification.assert_awaited_once()
    assert rt.send_notification.call_args.args[0] == "session/cancel"
    assert rt.send_notification.call_args.args[1]["sessionId"] == PARENT
    assert events[-1].kind == EVENT_COMPLETE
    assert events[-1].stop_reason == "error: tool stall"
    assert len(handle.native_child_sessions) == N_CHILDREN  # counted, not recovered one by one


def test_native_child_cannot_be_resumed_independently():
    """A ``spawn_continue``-style resume that names a native child id is
    refused with a STRUCTURED reason naming the parent and the lever that
    exists. An id that is not a child of this handle gets no opinion (None)."""
    handle = _handle()
    _feed_children(handle, 2)
    reason = handle.native_child_resume_refusal("sub-1")
    assert reason is not None
    assert reason.startswith(f"{NATIVE_CHILD_NOT_RESUMABLE}: sub-1")
    assert PARENT in reason and "parent session" in reason
    assert handle.native_child_resume_refusal("some-conversation") is None
    assert handle.native_child_resume_refusal(PARENT) is None


def test_spawn_continue_on_a_native_child_id_never_starts_a_run():
    """Through the REAL ``continue_conversation`` path a native child id has no
    state.json and no session mapping, so the run is refused (``done`` with a
    typed error) before any session is created. The precise
    ``native_child_not_resumable`` reason is the seam above; wiring it ahead
    of the generic lookup miss is an integration TODO recorded in the handoff."""
    from kiro_crew.subagent import SubagentManager

    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_or_create = AsyncMock()
    sessions.resumable_sid = MagicMock(return_value=None)
    sessions.is_continuable = MagicMock(return_value=False)
    sessions.mark_continuable = MagicMock()
    sessions.unmark_continuable = MagicMock()
    sessions.seed_conversation = MagicMock()
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    manager = SubagentManager(sessions=sessions, ctx_builder=ctx)
    handle = _handle()
    _feed_children(handle, 1)
    (child_sid,) = handle.native_child_sessions
    with patch("kiro_crew.subagent.sel"), patch("kiro_crew.subagent.read_state", return_value=None):
        info = manager.continue_conversation(child_sid, "keep going")
    assert info is not None and info.done
    assert info.error and ":" in info.error  # typed ``<class>: ...``
    assert info.error.split(":", 1)[0] in {"conversation_gone", NATIVE_CHILD_NOT_RESUMABLE}
    sessions.get_or_create.assert_not_called()
    assert child_sid not in manager._agents


def test_late_child_frame_after_the_turn_resurrects_nothing():
    """A child frame that arrives after the parent turn ended (late result)
    updates the count and nothing else: no event kind a runner acts on, no
    status, no charge -- and the next turn's reset forgets it."""
    handle = _handle()
    handle._turn_done.set()  # no turn in flight
    events = handle._handle_update(
        _update_msg(_tool_call(tool_call_id="late"), session_id="late-1")
    )
    assert {e.kind for e in events} <= {EVENT_SUBAGENT_ACTIVITY}
    assert handle.native_child_sessions == frozenset({"late-1"})
    assert handle.status_rejections == {}
    assert handle.is_turn_active is False


# ── (3) Claude backend: no per-child identity -- declared gap H16 ────────────


@pytest.mark.asyncio
async def test_claude_backend_counts_no_children_but_oracle_still_bounds_the_parent():
    """The Claude adapter's Task tool is an ordinary tool call on the parent:
    no child sessionId, no roster, no ``_meta.kiro``. The counter stays 0 --
    not because there are no children, but because the harness exposes none
    (parity row H16, a declared capability gap). The stall watchdog still
    bounds the parent turn with the same single session-scoped cancel."""
    rt = _runtime(ACP_BACKEND_CLAUDE)
    handle = AcpSessionHandle(PARENT, asyncio.Queue(), rt, watchdog=_FAST_WD)
    events = handle._handle_update(_update_msg(_claude_task_call()))
    assert [e.kind for e in events] == [EVENT_TOOL_CALL]
    for i in range(3):
        handle._handle_update(
            _update_msg(
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": f"task-{i}",
                    "status": "in_progress",
                }
            )
        )
    assert handle.native_child_sessions == frozenset()
    assert handle.native_child_overflow == 0
    budget = HostBudget(HostBudgetLimits())
    assert handle.report_native_children(budget) == 0
    assert budget.snapshot()["uncharged"] == {}

    handle._stale_eligible = False
    handle._queue = _SilentQueue()  # type: ignore[assignment]
    events = [ev async for ev in handle._dispatch_events(req_id=1, timeout=30.0)]
    rt.send_notification.assert_awaited_once()
    assert rt.send_notification.call_args.args[0] == "session/cancel"
    assert rt.send_notification.call_args.args[1]["sessionId"] == PARENT
    assert events[-1].kind == EVENT_COMPLETE and events[-1].stop_reason == "error: tool stall"


def test_claude_frames_carrying_a_child_status_are_rejected_like_any_other():
    """If a Claude-side extension ever attaches a child-shaped status to the
    parent's frames, the origin rule still applies: session mismatch."""
    handle = _handle(ACP_BACKEND_CLAUDE)
    events = handle._handle_update(
        _update_msg(_claude_task_call(), meta=_status_meta(session_id="claude-child"))
    )
    assert EVENT_STRUCTURED_STATUS not in {e.kind for e in events}
    assert handle.status_rejections == {"session_mismatch": 1}


# ── (4) nested: the Kiro Crew spawn_run child is the boundary ─────────────────


def test_nested_spawn_run_child_is_the_boundary_for_its_native_grandchildren():
    """S (parent, its own handle) -> spawn_run child B (its own handle, a task
    row) -> B's native ``use_subagent`` grandchildren. The grandchildren are
    counted on B only; S counts nothing for them (B is S's Kiro Crew child,
    not a native one), and each handle refuses to resume the other's ids."""
    parent = _handle(session=PARENT)
    child_run = _handle(session=CHILD_RUN)
    grand = _feed_children(child_run, 5, prefix="gc")
    parent._handle_update(_update_msg(_tool_call(tool_call_id="spawn", command="spawn_run")))
    assert child_run.native_child_sessions == frozenset(grand)
    assert parent.native_child_sessions == frozenset()
    assert CHILD_RUN not in parent.native_child_sessions  # a task row, never a native child
    assert child_run.native_child_resume_refusal("gc-0") is not None
    assert parent.native_child_resume_refusal("gc-0") is None
    budget = HostBudget(HostBudgetLimits())
    parent.report_native_children(budget)
    child_run.report_native_children(budget)
    assert budget.snapshot()["uncharged"] == {NATIVE_CHILDREN_UNCHARGED_KIND: 5}


@pytest.mark.asyncio
async def test_shared_runtime_with_two_owners_drops_an_unannounced_grandchild_frame(tmp_path):
    """On a SHARED runtime with two registered sessions, a frame for an
    unregistered child sessionId names no owner: the runtime drops it
    (counted), neither handle adopts it, and no session is cross-talked. The
    count on a multi-tenant runtime is therefore best-effort ("observed"),
    which the docs declare; the boundary holds regardless."""
    rt = AcpRuntime(work_dir=str(tmp_path))
    reader = asyncio.StreamReader()
    proc = MagicMock()
    proc.stdout = reader
    proc.stdin = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = 4242
    rt._process = proc
    rt._pid = 4242
    rt._initialized = True
    qa: asyncio.Queue = asyncio.Queue()
    qb: asyncio.Queue = asyncio.Queue()
    rt._session_queues.update({PARENT: qa, CHILD_RUN: qb})
    task = asyncio.ensure_future(rt._reader_loop())
    try:
        frame = {
            "method": METHOD_SESSION_UPDATE,
            "params": {"sessionId": "gc-9", "update": _tool_call(tool_call_id="g")},
        }
        reader.feed_data((json.dumps(frame) + "\n").encode())
        for _ in range(20):
            await asyncio.sleep(0)
        assert qa.empty() and qb.empty()
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    handle_a = AcpSessionHandle(PARENT, qa, _runtime())
    handle_b = AcpSessionHandle(CHILD_RUN, qb, _runtime())
    assert handle_a.native_child_sessions == handle_b.native_child_sessions == frozenset()


# ── (5) 200 children: no budget inflation, no controller timeout signal ──────


def test_two_hundred_children_do_not_trip_the_timeout_rate_signal():
    """The controller learns about work through ``record_start`` /
    ``record_completion`` (one per Kiro Crew run) and the daemon's budget
    snapshot. A parent with 200 native children contributes exactly ONE
    start and ONE completion, and the budget's charged ``procs`` is unchanged,
    so ``attributable_timeout_rate`` is 0 and no ``timeouts`` signal fires.
    Control: the same 200 recorded as attributable timeouts WOULD trip it --
    which is exactly what the boundary prevents."""
    clock = _Clock()
    mgr = _FakeManager()
    with patch.object(ctl_live(), "watch_object", return_value=None):
        ctl = AdaptiveController(mgr, cfg=KiroCrewConfig(), clock=clock)
    budget = HostBudget(HostBudgetLimits(max_procs=40))
    budget.reserve(label=PARENT, kind="pooled")
    handle = _handle()
    _feed_children(handle, N_CHILDREN)
    handle.report_native_children(budget)

    ctl.record_start(1200.0, ok=True, key="pool:kiro")  # the parent's session start
    clock.advance(5.0)
    ctl.record_completion(ok=True)  # the parent's turn finished
    sample = ctl.build_sample(
        loop_lag_ms=5.0,
        host=HostSample(free_mem_mb=16_000.0, rss_mb=300.0, fd_count=50, fd_limit=1000),
        gate_snap={},
        budget_snap=budget.snapshot(),
    )
    assert sample.attributable_timeout_rate == 0.0
    assert sample.proc_count == 1 and sample.proc_limit == 40
    assert sample.completions == 1
    report = classify(sample, ctl.policy.params.thresholds)
    assert SIGNAL_TIMEOUTS not in report.signals
    assert not report.corroborated and not report.severe
    assert ctl.state()["last_sample"]["attributable_timeout_rate"] == 0.0

    # Control: per-child accounting would have tripped the very signal (E) the
    # boundary keeps quiet -- 200 attributable "timeouts" against 1 success.
    bad = _FakeManager()
    with patch.object(ctl_live(), "watch_object", return_value=None):
        bad_ctl = AdaptiveController(bad, cfg=KiroCrewConfig(), clock=clock)
    bad_ctl.record_start(1200.0, ok=True)
    for _ in range(N_CHILDREN):
        bad_ctl.record_completion(ok=False, attributable_timeout=True)
    bad_sample = bad_ctl.build_sample(
        loop_lag_ms=5.0,
        host=HostSample(free_mem_mb=16_000.0, rss_mb=300.0, fd_count=50, fd_limit=1000),
        gate_snap={},
        budget_snap=budget.snapshot(),
    )
    assert bad_sample.attributable_timeout_rate > ctl.policy.params.thresholds.timeout_rate
    assert SIGNAL_TIMEOUTS in classify(bad_sample, bad_ctl.policy.params.thresholds).signals


def ctl_live():
    from kiro_crew.config import live

    return live
