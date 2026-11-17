"""The session's per-call silence timer: which bound each tool call gets, what restarts it,
and when it may run at all.

The class a dispatched call falls in (``acp.tool_silence``), the frames that count as
that call's own activity, a Kiro Crew tool's heartbeat, the calls a native sub-agent
has open, which hold its parent's sub-agent call, the model's next step that retires
the timers of the calls before it, and the call whose silence ran past its bound. The
dispatch loop acts on that call (``_dispatch_events``); nothing here cancels anything.

A timer runs only while this session can prove it would see its call's activity; where
it cannot, the call is not judged by its silence, and the liveness oracle and the hard
cap judge it as they judged every call before the timer. So a harness sub-agent call
loses its timer once a frame naming a session reached no session this turn, or the
roster route is closed now (``_retire_unseen_subagent_timers``); a call on Kiro Crew's
own servers loses it on a handle no keepalive reaches (``_retire_unreachable_crew_timers``);
a call loses it when a frame of a kind this module does not read names it
(``_note_call_frame``, ``_retire_call_timer``); a call is not judged while an ask for it
waits on a person, and every call is paused while an ask naming no call waits or a KAS
hook runs (``_held_calls``, ``_sync_silence_hold``). The dispatch loop adds the last
case: no timer is read while a concurrent request reader may hold this session's frames.

Each function here whose first parameter is ``self`` is an ``AcpSessionHandle`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_child_calls_overflow``, ``_child_open_calls``,
``_silence_held_parked``, ``_silence_held_since``, ``_silence_held_total``,
``_unrouted_child_frames_at_turn``.

Instance fields read: ``_hook_tasks``, ``_mcp_network_servers``, ``_parked_since``,
``_parked_total``, ``_pending_elicitations``, ``_permission_gate_events``, ``_runtime``,
``_session_id``, ``_tool_tracker``, ``_watchdog``, ``keepalive_routed``.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from kiro_crew.acp import kas_wire
from kiro_crew.acp._dispatch import (
    MAX_ACTIVE_TOOL_CALLS,
    classify_tool_call,
    liveness_key,
    scoped_tool_cache_key,
)
from kiro_crew.acp.liveness import ToolCallState
from kiro_crew.acp.tool_silence import (
    CALL_ACTIVITY_UPDATES,
    HARNESS_CHILD_SESSION_STREAMS,
    SILENCE_CLASS_NONE,
    SILENCE_CLASS_SUBAGENT,
    SilentCall,
    call_silence,
    classify_tool_silence,
    is_unterminated_builtin,
    silence_bound,
    stamp_activity,
)
from kiro_crew.acp.types import ACP_BACKEND_KAS, TERMINAL_TOOL_STATUSES, AcpEvent
from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS

if TYPE_CHECKING:
    from kiro_crew.acp.session_handle import AcpSessionHandle

#: The session handle's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.session_handle")

#: Kiro Crew's own MCP servers: the only ones whose tools reach
#: ``/api/session-keepalive``, which is authenticated and session-scoped, and
#: the ones whose calls a handle that route cannot reach does not time.
_CREW_SERVERS: frozenset[str] = frozenset(KIROCREW_BIN_MCP_SERVERS)


def _silence_class_for(self: AcpSessionHandle, ev: AcpEvent) -> tuple[str, str]:
    """``(silence class, server the frame named)`` for a just-dispatched ``tool_call``.

    The server is the one the harness's own frame named (``ev.mcp_server_name``); the
    tool name is the verified identity's when there is one (KAS stamps a
    ``kirocrew-core`` call's tool only in its envelope), else the harness's stamp.
    """
    _trusted_server, trusted_tool = ToolCallState.identity_of(ev)
    server = ev.mcp_server_name if isinstance(ev.mcp_server_name, str) else ""
    tool_name = trusted_tool or (ev.tool_name if isinstance(ev.tool_name, str) else "")
    silence_class = classify_tool_silence(
        backend=getattr(self._runtime, "acp_backend", None),
        is_shell=bool(ev.is_shell),
        server=server,
        tool_name=tool_name,
        kind=ev.tool_kind if isinstance(ev.tool_kind, str) else "",
        network_servers=self._mcp_network_servers,
    )
    return silence_class, server


def _consumer_parked(parked_total: float, parked_since: float | None, now: float) -> float:
    """The turn's consumer park at *now*: *parked_total* banked, plus a park still open."""
    return parked_total + (max(0.0, now - parked_since) if parked_since is not None else 0.0)


def _hold_beyond_parks(
    held_since: float | None, parked_at_hold: float, now: float, parked: float
) -> float:
    """The open hold's stretch at *now* that no consumer park covers; 0.0 with none open.

    *held_since* is when the hold opened and *parked_at_hold* the turn's consumer
    park then, *parked* that park at *now*. The park that elapsed meanwhile is
    already the park's to subtract, so the hold keeps only the rest.

    The clamp never binds: while one hold is open the park grows at most at the
    clock's rate, and a turn's start closes the hold before it zeroes the park.
    Nor does *parked_at_hold*'s value between holds matter: it is written at every
    open and read only while that hold is open.
    """
    if held_since is None:
        return 0.0
    return max(0.0, (now - held_since) - (parked - parked_at_hold))


def _park_baseline(self: AcpSessionHandle, now: float) -> float:
    """The time at *now* no call is charged for: the turn's consumer park and its holds.

    A park still open is included: a stamp can land while the consumer holds an
    event (a keepalive arrives over HTTP), and ``_parked_total`` banks a park only
    when it ends. So is a hold still open (``_sync_silence_hold``). An instant both
    parked and held counts once: the consumer parks on the very ask that opened the
    hold, or holds any event while a KAS hook runs, so a hold accrues only while no
    park is open (``_hold_beyond_parks``).
    """
    parked = _consumer_parked(self._parked_total, self._parked_since, now)
    open_hold = _hold_beyond_parks(self._silence_held_since, self._silence_held_parked, now, parked)
    return parked + self._silence_held_total + open_hold


def _held_state(self: AcpSessionHandle, tool_call_id: object) -> ToolCallState | None:
    """The state of the call *tool_call_id* names, if this turn holds it."""
    key = liveness_key(tool_call_id)
    entry = self._tool_tracker.active_calls.get(key) if key else None
    return entry[0] if entry is not None else None


def note_tool_call_activity(self: AcpSessionHandle, tool_call_id: object) -> bool:
    """A frame naming *tool_call_id* arrived: restart that call's silence timer.

    Returns whether a call this turn holds was stamped. An id the turn does not
    hold (a sibling already gone, a child's call) is a no-op.
    """
    state = self._held_state(tool_call_id)
    if state is None:
        return False
    now = time.monotonic()
    return stamp_activity(state, now, self._park_baseline(now))


def _retire_call_timer(self: AcpSessionHandle, tool_call_id: object) -> bool:
    """Stop timing the call *tool_call_id* names, for the rest of the turn.

    For a frame naming the call that this module cannot read: it may say the call
    is waiting on something, or working somewhere no frame reaches, so the call's
    silence proves nothing from here on. Its row stays for the oracle and the hard
    cap. Returns whether the turn holds the call.
    """
    state = self._held_state(tool_call_id)
    if state is None:
        return False
    state.silence_class = SILENCE_CLASS_NONE
    return True


def _note_call_frame(self: AcpSessionHandle, update: object) -> None:
    """A frame of this session's own (*update*, a ``session/update`` payload) named a call.

    A kind that is the call's own activity (``tool_silence.CALL_ACTIVITY_UPDATES``)
    restarts its timer. Any other kind naming a call this turn holds retires that
    call's timer (``_retire_call_timer``): what it means is unknown, so the call's
    silence after it is not proof the call was lost. A frame naming no call this
    turn holds (a child's nested call, a call already settled) does nothing.
    """
    if not isinstance(update, dict):
        return
    call_id = update.get("toolCallId")
    if update.get("sessionUpdate") in CALL_ACTIVITY_UPDATES:
        self.note_tool_call_activity(call_id)
    else:
        self._retire_call_timer(call_id)


def note_subagent_frame(self: AcpSessionHandle) -> int:
    """A frame of one of this session's sub-agents arrived.

    The frame names the sub-agent's own session, not the call that started it, so
    it restarts the timer of every sub-agent call in flight and of nothing else: a
    sub-agent's chatter must never stand in for a lost file edit beside it.
    Returns how many calls it stamped.
    """
    now = time.monotonic()
    parked = self._park_baseline(now)
    stamped = 0
    for _call_id, state in self._tool_tracker.states():
        if state.silence_class == SILENCE_CLASS_SUBAGENT and stamp_activity(state, now, parked):
            stamped += 1
    return stamped


def note_tool_heartbeat(
    self: AcpSessionHandle, tool_call_id: str = "", *, activity_at: float | None = None
) -> int:
    """One of Kiro Crew's own tools on this session reported it is alive.

    Sent with ``/api/session-keepalive``, about every 30 s while a long tool blocks
    (the same heartbeat its MCP progress notification carries). *tool_call_id* is
    the harness's ``toolCallId`` the tool was called under; a heartbeat naming a
    call this turn holds restarts that call alone. One naming a call a native
    sub-agent of this session has open is that sub-agent's activity, so it
    restarts the sub-agent calls in flight and nothing else, as the sub-agent's
    own frames do. One that names none, or one this handle cannot match, restarts
    every call in flight on Kiro Crew's own servers: the route is authenticated
    and session-scoped, so it came from one of them. *activity_at* (monotonic,
    never later than now) is when the work the tool waits on last moved, for a
    tool that waits on sub-agents; None means now. A stamp earlier than now keeps
    the call's earlier park baseline: a park between that activity and now is time
    the call was not charged for, and moving the baseline to now would subtract it
    from the wrong side. That fails toward a longer timer. Returns how many calls
    it stamped.
    """
    now = time.monotonic()
    at = now if activity_at is None else min(float(activity_at), now)
    parked = self._park_baseline(now)
    calls = self._tool_tracker.states()
    key = liveness_key(tool_call_id) if tool_call_id else ""
    targets = [state for call_id, state in calls if key and call_id == key]
    if not targets and key and key in self._child_open_calls.values():
        targets = [
            state for _call_id, state in calls if state.silence_class == SILENCE_CLASS_SUBAGENT
        ]
    elif not targets:
        targets = [state for _call_id, state in calls if state.silence_server in _CREW_SERVERS]
    late = at < now
    return sum(
        1
        for state in targets
        if stamp_activity(state, at, state.activity_parked if late else parked)
    )


def note_kas_subtask_frame(self: AcpSessionHandle, update: object) -> bool:
    """A KAS frame on this session that names one of its sub-agents (``agentSubtaskId``).

    KAS streams a native sub-agent on its parent's own session, each frame naming
    the subtask, whatever the frame is: its text, its thinking, its tool calls, its
    plan or usage. Every such frame is that sub-agent's activity (``note_subagent_frame``).
    Positive KAS gate (H5): kiro-cli streams a child under the child's own
    ``sessionId`` instead. Returns whether the frame named a subtask.
    """
    if getattr(self._runtime, "acp_backend", None) == ACP_BACKEND_KAS and isinstance(update, dict):
        kiro = kas_wire.kiro_meta(update)
        if kiro is not None and kiro.get(kas_wire.FIELD_AGENT_SUBTASK_ID):
            self.note_subagent_frame()
            return True
    return False


def _note_child_calls(self: AcpSessionHandle, update: object, scope: str) -> None:
    """Track the calls a native sub-agent of this session has open, from its frame.

    *update* is one frame's ``update`` payload a native child of this session sent
    (under its own ``sessionId``, or a KAS ``agentSubtaskId`` frame), *scope* names
    the child. A native child runs inside this session's harness, where no Kiro
    Crew watchdog times its calls, and a call it has running sends nothing while it
    runs: a shell, a built-in, a Kiro Crew ``wait`` or ``ask_question`` (whose
    heartbeat names the child's call, not the parent's), another MCP call, or its
    own sub-agent call. So ANY call it has open keeps the parent's sub-agent calls
    from being judged, as the keepalive route counts a ``spawn_sub_agents`` child
    mid-tool as moving now (``live_turn._subagent_activity_at``). A call opens on
    its ``tool_call`` frame and closes on any frame for it with a terminal status,
    the opening frame included. A built-in the harness never terminates
    (``tool_silence.is_unterminated_builtin``: kiro-cli's ``introspect``) holds
    nothing, as it gets no timer on the parent. Bounded like the call map: past
    the bound the hold stays for the rest of the turn, the direction that cannot
    cut a working child.
    """
    if not isinstance(update, dict):
        return
    call_id = update.get("toolCallId")
    if not isinstance(call_id, str) or not call_id:
        return
    key = liveness_key(scoped_tool_cache_key(scope, call_id))
    status = update.get("status")
    if isinstance(status, str) and status in TERMINAL_TOOL_STATUSES:
        self._child_open_calls.pop(key, None)
        return
    if update.get("sessionUpdate") != "tool_call" or key in self._child_open_calls:
        return
    identity = classify_tool_call(update)
    if is_unterminated_builtin(
        getattr(self._runtime, "acp_backend", None),
        server=identity.mcp_server_name or "",
        tool_name=identity.tool_name or "",
    ):
        return
    if len(self._child_open_calls) >= MAX_ACTIVE_TOOL_CALLS:
        self._child_calls_overflow = True
        return
    self._child_open_calls[key] = liveness_key(call_id)


def _unrouted_child_frames_now(self: AcpSessionHandle) -> int | None:
    """The runtime's count of frames that named a session and reached none, or None.

    None when the runtime cannot say (``AcpRuntime.unrouted_child_frames``), which
    proves nothing about what reached this session.
    """
    read = getattr(self._runtime, "unrouted_child_frames", None)
    count = read() if callable(read) else None
    return count if isinstance(count, int) and not isinstance(count, bool) else None


def _roster_route_proven(self: AcpSessionHandle) -> bool:
    """Whether every frame a kiro-cli native sub-agent sent this turn reached this session.

    Proven only when the runtime routes a roster child's frames here now
    (``AcpRuntime.routes_roster_children_to``) and no frame naming a session has
    reached no session since this turn started (``unrouted_child_frames``). The
    first answer holds only at the instant it is asked, so the count is what
    sees a route that closed and opened again between two reads: a co-tenant
    registered for less than one queue read drops the child's frames and is
    gone before the next read. The count is the runtime's, not this session's
    (a lost frame's owner is not always known), so any frame lost on the
    runtime this turn counts here, the direction that cannot cut a working
    sub-agent.
    """
    routes = getattr(self._runtime, "routes_roster_children_to", None)
    if not callable(routes) or routes(self._session_id) is not True:
        return False
    baseline = self._unrouted_child_frames_at_turn
    return baseline is not None and self._unrouted_child_frames_now() == baseline


def _retire_unseen_subagent_timers(self: AcpSessionHandle, *, runtime_shared: bool = False) -> int:
    """Stop timing a harness sub-agent call whose sub-agent's frames may not reach this session.

    kiro-cli streams a native sub-agent under the child's own ``sessionId``, which
    the runtime hands to this session only while it is the one session registered
    there; beside another session the frames are dropped, so the sub-agent can be
    working with nothing here to show it. Unless every frame is proven to have
    reached this session (``_roster_route_proven``), such a call loses its
    silence timer for good, as a call the model moved past does
    (``_retire_silence_timers``): its row stays for the oracle and the hard cap.
    For good, because the frames dropped meanwhile are gone, and a timer read
    again once the route is back would count them as silence.
    *runtime_shared* is set for a frame the runtime fanned out to several sessions,
    which proves the route was closed when it was sent, whatever the runtime
    says now. Kiro Crew's own ``spawn_*`` calls reach this session through their
    heartbeats and results on any runtime, and a KAS sub-agent's frames ride this
    session's own id, so neither is touched. Returns how many timers it retired.
    """
    if getattr(self._runtime, "acp_backend", None) not in HARNESS_CHILD_SESSION_STREAMS:
        return 0
    calls = [
        state
        for _call_id, state in self._tool_tracker.states()
        if state.silence_class == SILENCE_CLASS_SUBAGENT and not state.silence_server
    ]
    if not calls:
        return 0
    if not runtime_shared and self._roster_route_proven():
        return 0
    for state in calls:
        state.silence_class = SILENCE_CLASS_NONE
    return len(calls)


def _retire_unreachable_crew_timers(self: AcpSessionHandle) -> int:
    """Stop timing every call on Kiro Crew's own servers while no keepalive reaches this handle.

    Such a call's proof of activity is its heartbeat, which reaches a handle only
    through ``/api/session-keepalive``: the route looks ``X-Session-Key`` up in the
    gateway's own session registry and hands the beat to the provider it finds
    there. A handle whose provider that registry never published sees none of
    them: ``kirocrew chat`` and ``kirocrew run`` hold theirs in their own process,
    a sub-agent run on a shared handle holds its provider outside the registry, and
    so does an app's own runner. So the call's silence proves nothing there, and
    it is judged by the oracle and the hard cap. ``keepalive_routed`` is True only
    once that registry published this handle's provider
    (``kiro_crew.keepalive_route``). For good, as
    ``_retire_unseen_subagent_timers`` is: a beat lost before the mark is gone.
    Returns how many timers it retired.
    """
    if self.keepalive_routed is True:
        return 0
    retired = 0
    for _call_id, state in self._tool_tracker.states():
        if state.silence_server in _CREW_SERVERS and state.silence_class != SILENCE_CLASS_NONE:
            state.silence_class = SILENCE_CLASS_NONE
            retired += 1
    return retired


def _retire_silence_timers(self: AcpSessionHandle, update: dict) -> int:
    """This session's model spoke or thought: stop timing every call already in flight.

    *update* is a ``session/update`` the runtime routed to this session as its own
    text or thinking. The model starts a new step only once every call of its
    previous batch has returned to it, so a call dispatched before that frame is
    one the harness has moved past, whether or not a terminal frame for it ever
    comes. Its row stays for the oracle and the hard cap, which judge it as before
    this timer. A call its harness never terminates at all (kiro-cli's
    ``introspect``) is not left to this rule, since the model's next step can be
    another call with no text before it: it gets no class at dispatch
    (``tool_silence.HARNESS_UNTERMINATED_TOOLS``).
    A frame that names a KAS sub-agent (``agentSubtaskId``) is that child's, not
    this model's, and retires nothing. Returns how many timers it retired.
    """
    kiro = kas_wire.kiro_meta(update)
    if kiro is not None and kiro.get(kas_wire.FIELD_AGENT_SUBTASK_ID):
        return 0
    retired = 0
    for _call_id, state in self._tool_tracker.states():
        if state.silence_class != SILENCE_CLASS_NONE:
            state.silence_class = SILENCE_CLASS_NONE
            retired += 1
    return retired


def _reset_tool_silence(self: AcpSessionHandle) -> None:
    """Turn start: forget the previous turn's native child calls and holds.

    The hold's total is this turn's own, as ``_parked_total`` is, and an open
    hold closes here: ``_run_turn`` drops the asks the last turn left unanswered
    right after this, and their hold must not pause this turn's calls until a
    timer read notices they are gone. A KAS hook still running opens it again
    at the next ``_sync_silence_hold``.

    And take the runtime's count of unrouted frames, which every later read
    compares against (``_roster_route_proven``). Taken before the prompt is
    sent, so a frame lost at any point of this turn moves it, even one the
    reader dropped before this session dequeued the call it belongs to.
    """
    self._child_open_calls.clear()
    self._child_calls_overflow = False
    self._silence_held_total = 0.0
    self._silence_held_since = None
    self._unrouted_child_frames_at_turn = self._unrouted_child_frames_now()


def _held_calls(self: AcpSessionHandle) -> tuple[frozenset[str], bool]:
    """The asks still waiting for an answer: ``(the calls they name, one names no call)``.

    A permission request is pending from the frame until ``approve_tool`` or
    ``reject_tool`` takes it (``_permission_gate_events``), an elicitation until
    ``answer_elicitation`` does (``_pending_elicitations``). While one waits, its
    call cannot move and nothing reaches this session to say how long the
    person takes, whether or not the consumer holds the event meanwhile.
    *calls* are the liveness keys of the calls they name; an ask that names no
    call holds every call (``_sync_silence_hold``). A native child's ask names
    the child's own call, which this turn's tracker does not hold; the child's
    open call holds its parent's sub-agent call instead (``_note_child_calls``).
    """
    calls: set[str] = set()
    unnamed = False
    asks = [event.tool_call_id for event in self._permission_gate_events.values()]
    asks += [request.tool_call_id for request in self._pending_elicitations.values()]
    for tool_call_id in asks:
        key = liveness_key(tool_call_id)
        if key:
            calls.add(key)
        else:
            unnamed = True
    return frozenset(calls), unnamed


def _sync_silence_hold(self: AcpSessionHandle) -> bool:
    """Open or close the hold that pauses every call's timer; True while it is open.

    The hold is open while a KAS hook this session started runs (``_hook_tasks``),
    since a hook can gate a call already dispatched and no frame names which, or
    while an ask naming no call waits (``_held_calls``). A held stretch is
    subtracted from every call's silence as a consumer park is
    (``_park_baseline``), so a call is charged neither for the hook nor for the
    wait, and a stretch a consumer park also covers is subtracted once: the hold
    keeps the turn's park at its start (``_silence_held_parked``) and banks only
    the time no park covers. Called where either can start or end, and before
    every timer read.
    """
    now = time.monotonic()
    open_now = bool(getattr(self, "_hook_tasks", None)) or self._held_calls()[1]
    since = self._silence_held_since
    parked = _consumer_parked(self._parked_total, self._parked_since, now)
    if open_now and since is None:
        self._silence_held_since = now
        self._silence_held_parked = parked
    elif not open_now and since is not None:
        self._silence_held_total += _hold_beyond_parks(
            since, self._silence_held_parked, now, parked
        )
        self._silence_held_since = None
    return open_now


def note_ask_answered(self: AcpSessionHandle, tool_call_id: str) -> None:
    """An ask was answered (or taken to be): the call it named moves again from now.

    The answer is the call's last proven activity, and what the call does next
    reaches this session as any call's frames do, so its timer restarts here. A
    hold the ask kept open is closed. A child's ask names the child's call, which
    this turn's tracker does not hold, so it restarts nothing.
    """
    self._sync_silence_hold()
    self.note_tool_call_activity(tool_call_id)


def _silent_tool_call(self: AcpSessionHandle, now: float) -> SilentCall | None:
    """The in-flight call furthest past its silence bound at *now*, or None.

    Read by the dispatch loop while it runs, so no consumer park is open. No call
    is judged while the hold is open (``_sync_silence_hold``), a call is not
    judged while an ask for it waits (``_held_calls``), a sub-agent call is not
    judged while a native child of this session has any call open
    (``_note_child_calls``), and a harness sub-agent call whose
    sub-agent's frames may not reach this session, or a call on Kiro Crew's own
    servers on a handle no keepalive reaches, is retired first
    (``_retire_unseen_subagent_timers``, ``_retire_unreachable_crew_timers``).
    """
    if self._sync_silence_hold():
        return None
    self._retire_unseen_subagent_timers()
    self._retire_unreachable_crew_timers()
    held, _unnamed = self._held_calls()
    child_call_open = bool(self._child_open_calls) or self._child_calls_overflow
    paused = self._park_baseline(now)
    found: SilentCall | None = None
    for call_id, state in self._tool_tracker.states():
        bound = silence_bound(self._watchdog, state.silence_class)
        # The tracker's keys are already liveness keys, as ``held``'s are.
        if bound <= 0 or call_id in held:
            continue
        if state.silence_class == SILENCE_CLASS_SUBAGENT and child_call_open:
            continue
        silence = call_silence(state, now, paused)
        if silence <= bound:
            continue
        if found is None or silence - bound > found.silence - found.bound:
            found = SilentCall(call_id=call_id, state=state, silence=silence, bound=bound)
    return found
