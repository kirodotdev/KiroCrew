"""The in-flight tool call the liveness oracle judges.

:class:`ToolCallState` is what a dispatch records about its call, :class:`InFlightToolTracker`
is the rule that picks which call a stall verdict is read against, and
:func:`wait_tool_verdict` is the wait tool's declared-duration verdict. A new field a
dispatch records goes on :class:`ToolCallState`; :mod:`kiro_crew.acp.liveness`
re-exports every name here.
"""

from __future__ import annotations

from dataclasses import dataclass

from kiro_crew.acp.liveness_command import (
    MAX_RETAINED_NAME_CHARS,
    MAX_RETAINED_TITLE_CHARS,
    bounded_command_text,
    parse_wait_seconds,
)
from kiro_crew.acp.liveness_evidence import (
    _KEEPALIVE_TOOL_NAMES,
    _MCP_SEPARATOR_RE,
    _WAIT_TOOL_NAME,
    VERDICT_UNKNOWN,
    VERDICT_WORKING,
    WAIT_TOOL_SLACK_SECS,
)
from kiro_crew.acp.liveness_interactive import INTERACTIVE_NONE
from kiro_crew.constants import WAIT_TOOL_MAX_SECS
from kiro_crew.session_directive import CORE_MCP_SERVER


def wait_tool_verdict(tool: ToolCallState, now: float) -> tuple[str, str] | None:
    """The oracle's wait-tool verdict, selected by *tool*'s adapter identity.

    Returns ``None`` unless :meth:`ToolCallState.is_trusted_wait` holds, so the
    caller applies its other evidence; otherwise
    :meth:`ToolCallState.declared_wait_verdict`. The model-authored title never
    selects the contract.
    """
    if not tool.is_trusted_wait():
        return None
    return tool.declared_wait_verdict(now)


@dataclass
class ToolCallState:
    """Snapshot of the in-flight tool call the oracle reasons about.

    ``title`` and ``command`` are bounded at construction (the point of
    retention) by ``MAX_RETAINED_TITLE_CHARS`` / ``MAX_RETAINED_COMMAND_CHARS``:
    a state is held until the call's terminal result, and a turn may hold many,
    so an unbounded backend string here would be an unbounded row.
    """

    title: str = ""
    command: str = ""  # redacted cached tool input

    def __post_init__(self) -> None:
        if len(self.title) > MAX_RETAINED_TITLE_CHARS:
            self.title = self.title[:MAX_RETAINED_TITLE_CHARS]
        self.command = bounded_command_text(self.command)
        if len(self.tool_name) > MAX_RETAINED_NAME_CHARS:
            self.tool_name = self.tool_name[:MAX_RETAINED_NAME_CHARS]
        if len(self.mcp_server_name) > MAX_RETAINED_NAME_CHARS:
            self.mcp_server_name = self.mcp_server_name[:MAX_RETAINED_NAME_CHARS]

    dispatch_ts: float = 0.0  # time.monotonic() at EVENT_TOOL_CALL
    is_shell: bool = False
    # ``boottime_now()`` at EVENT_TOOL_CALL — the SAME clock /proc dates process
    # start times against, so a child's start can be compared to its dispatch
    # without mixing clocks (see :func:`boottime_now`). Separate from
    # ``dispatch_ts``, which stays monotonic because it measures ELAPSED time (the
    # wait tool's declared duration), where excluding suspend is correct. None
    # when the clock is unavailable; every consumer must then decline to attribute
    # a process to this dispatch rather than guess.
    dispatch_boot_ts: float | None = None
    # ``steady_now()`` taken alongside ``dispatch_boot_ts``. On darwin that stamp
    # is the wall clock, which can step; this one cannot, so the pair lets the
    # oracle detect a step since dispatch before it grounds an absence claim on
    # start-time attribution. None where the platform needs no check (Linux) or
    # the caller took no such stamp — on darwin the oracle then declines to
    # attribute at all rather than trust an unvalidated wall clock.
    dispatch_steady_ts: float | None = None
    # Consumer parking banked in THIS TURN before the stamp above was taken
    # (``AcpSessionHandle._parked_total``, which is per-turn and includes the whole
    # of a human approval wait). The stamp is taken when the tool_call frame is
    # PROCESSED, and the dispatch loop is suspended at its yield for every
    # consumer-side await — an approval, an IM send, a hook — so a tool whose
    # frame queued behind one of those was already spawned by the time its stamp
    # is taken. The child cannot predate the turn, so parking-so-far bounds that
    # lag and widens the attribution window by exactly as much as the loop itself
    # measured. Left 0.0 by a dispatch path with no consumer parking.
    dispatch_parked_secs: float = 0.0
    # Trusted tool name from ``_meta.kiro.toolName`` (empty when the backend
    # does not emit ``_meta``; fail-closed). Required for established_flat
    # attribution: the narrowing applies ONLY when this matches a known
    # model-wrapping tool (see ``_MODEL_WRAPPING_TOOLS``).
    tool_name: str = ""
    # Trusted MCP server from the adapter identity channel, set only when the
    # tool_call frame's identity is provenance-verified
    # (``AcpEvent.mcp_identity_trusted``); empty otherwise (fail-closed). Read by
    # :meth:`is_trusted_wait`, and by the ``remote_flat`` scan, which reads only
    # the subtree of the server this names.
    mcp_server_name: str = ""
    # Pre-dispatch verdict of :func:`classify_interactive_command` for a shell
    # tool (one of the ``INTERACTIVE_*`` classes; ``none`` for a non-shell tool
    # or an unmatched command). The oracle itself does not read it — it is
    # carried here so the caller's window policy and its post-stall
    # classification see the same value the dispatch computed, and so a
    # detached consult never re-derives it from a different command string.
    interactive_risk: str = INTERACTIVE_NONE

    def is_trusted_wait(self) -> bool:
        """True when the adapter-authored identity names the kirocrew-core ``wait``.

        Keys on ``mcp_server_name`` + ``tool_name``, never on ``title``, which is
        model-authored prose. ``mcp_server_name`` is set only from a
        provenance-verified identity channel, so an unverified call fails closed.
        A server-qualified ``tool_name`` resolves to its last segment, the same
        normalization ``session_directive.match_tool`` applies to this field; the
        server itself is still authenticated by ``mcp_server_name`` alone.
        """
        if self.is_shell or self.mcp_server_name != CORE_MCP_SERVER:
            return False
        return _MCP_SEPARATOR_RE.split(self.tool_name)[-1] == _WAIT_TOOL_NAME

    def is_trusted_keepalive_tool(self) -> bool:
        """True when the adapter-authored identity names a kirocrew-core tool
        that pings ``/api/session-keepalive`` while it runs.

        Same identity rule as :meth:`is_trusted_wait`: ``mcp_server_name`` must
        be the provenance-verified core server, and the title is never read.
        """
        if self.is_shell or self.mcp_server_name != CORE_MCP_SERVER:
            return False
        return _MCP_SEPARATOR_RE.split(self.tool_name)[-1] in _KEEPALIVE_TOOL_NAMES

    def declared_wait_verdict(self, now: float) -> tuple[str, str]:
        """Declared-duration contract for the kirocrew-core ``wait`` tool.

        The session is WORKING by definition until the declared sleep plus
        :data:`WAIT_TOOL_SLACK_SECS` elapses. The verdict reads only this call's
        own input and dispatch instant, never a process tree, so it is
        attributable to one session even on a shared runtime. *now* is on the
        same monotonic clock as ``dispatch_ts``. A declared duration above
        :data:`WAIT_TOOL_MAX_SECS` counts as that maximum, since the tool never
        accepts a longer one. The caller decides that this is a wait call.
        """
        secs = parse_wait_seconds(self.command)
        if secs is None:
            return VERDICT_UNKNOWN, "wait tool without parseable seconds"
        secs = min(secs, WAIT_TOOL_MAX_SECS)
        elapsed = now - self.dispatch_ts
        if elapsed < secs + WAIT_TOOL_SLACK_SECS:
            return VERDICT_WORKING, f"wait tool declared {secs}s ({elapsed:.0f}s elapsed)"
        return VERDICT_UNKNOWN, f"wait tool declared {secs}s elapsed"


class InFlightToolTracker:
    """The in-flight tool hand-off rule, shared by the two agent loops.

    Both :class:`~kiro_crew.acp.session_handle.AcpSessionHandle` (the main
    agent's watchdog) and ``SubagentInfo`` (the sub-agent reaper) have to answer
    the same question — *which tool call is the one a stall verdict should be
    read against right now* — and each would otherwise answer it with its own
    copy of the rule. Two copies drift: one keyed the judged call to a single
    slot that any result emptied, the other tracked calls by toolCallId. One
    implementation keeps both honest.

    The rule, in one place:

    * **dispatch** stores a call by its id and makes it the judged call. A call
      with no id (a backend that omits one) still becomes the judged call but is
      not stored — a terminal frame for it can only be matched by id, so storing
      it under ``""`` would let the next id-less dispatch overwrite a live entry.
    * **a terminal result** for a stored call drops it; when it was the judged
      call, the judged call moves to the NEWEST remaining dispatch (insertion
      order is dispatch order), or clears when none remain. A non-terminal
      (progress) frame changes nothing — the call is still running.
    * **prompt start** clears everything: a new turn judges nothing until it
      dispatches.

    Every operation returns whether the JUDGED call changed, because that is the
    signal both callers act on: the liveness oracle's evidence baseline is keyed
    to the judged command, so when the judged call changes the caller must retire
    the oracle (``AcpSessionHandle._retire_liveness_state`` /
    ``SubagentInfo``'s ``_stall_oracle.fresh()`` + ``_stall_gen`` bump) so a walk
    still running against the previous command cannot write into the next one's
    baseline. The retirement itself stays with each caller — one swaps a bound
    oracle and releases a consult future, the other bumps a generation counter —
    because that machinery is not shared; only the hand-off decision is.

    The sub-agent uses this in its degenerate single-call shape: a native child's
    tool call is judged on arrival (so a stall is attributed to the child's own
    command) but its result frame never reaches the sub-agent's loop, so the
    stored entry is simply overwritten by the next dispatch rather than popped by
    a terminal — exactly what a single live slot does. Storing by id costs it
    nothing (its runtime emits at most one in-flight call at a time) and keeps the
    one rule honest for the main agent's genuinely-parallel calls.

    ``acp.client`` keeps its own plain ``set`` of call ids for the tool-stall
    timer and does NOT use this: that set only answers "is any call still open",
    never "which call is judged", so this tracker would be apparatus it has no
    buyer for.

    The ``aux`` payload travels beside each :class:`ToolCallState` untouched: the
    main agent carries the shell call's pre-dispatch interactive classification
    there, and the sub-agent carries ``None``. The tracker never reads it.
    """

    __slots__ = ("_calls", "_current_id", "_evicted_count", "_idless", "_max_calls")

    def __init__(self, max_calls: int | None = None) -> None:
        # Insertion order IS dispatch order, so the newest remaining call is
        # ``next(reversed(...))`` — the main agent's parallel calls can finish in
        # any order and the judged call must fall back to the most recent one
        # still open, not the oldest.
        self._calls: dict[str, tuple[ToolCallState, object]] = {}
        self._current_id: str = ""
        # An id-less dispatch (a backend that omits a toolCallId, or a sub-agent
        # native-child call that will never get a terminal frame here) is the
        # judged call but is held HERE, not in ``_calls`` — it has no stable key
        # to pop by, so keeping it out of the running set means ``active_calls``
        # and ``any_active`` reflect only genuinely poppable calls, and the next
        # dispatch simply overwrites this slot. ``_current_id == ""`` with a
        # non-None ``_idless`` is exactly the id-less-judged state.
        self._idless: tuple[ToolCallState, object] | None = None
        # Most stored calls at once (``None``: unbounded). A backend that emits
        # distinct tool_call frames whose results never report a terminal status
        # would otherwise grow the map for the length of the turn. At the cap the
        # OLDEST call is evicted -- the one most likely never to get a terminal --
        # and the call just dispatched is always kept, so the judged call stays
        # the one actually in flight. ``_evicted_count`` counts the rows this
        # turn lost, so an emptied map does not prove that nothing is running
        # and the turn can say how many attributions it dropped.
        self._max_calls = max_calls
        self._evicted_count = 0

    @property
    def current(self) -> ToolCallState | None:
        """The :class:`ToolCallState` a stall verdict is read against, or None."""
        if self._current_id:
            entry = self._calls.get(self._current_id)
            return entry[0] if entry is not None else None
        return self._idless[0] if self._idless is not None else None

    @property
    def current_aux(self) -> object:
        """The aux payload beside the judged call (``None`` when none)."""
        if self._current_id:
            entry = self._calls.get(self._current_id)
            return entry[1] if entry is not None else None
        return self._idless[1] if self._idless is not None else None

    @property
    def current_id(self) -> str:
        """The judged call's toolCallId (``""`` when none or id-less)."""
        return self._current_id

    @property
    def any_active(self) -> bool:
        """Whether any dispatched call is still open.

        Counts only genuinely stored (poppable) calls — an id-less judged call
        is never handed the slot by a sibling's result, so it does not keep the
        running set 'active'.
        """
        return bool(self._calls)

    @property
    def evicted(self) -> bool:
        """Whether a dispatch evicted a stored call since the last :meth:`clear`.

        Once set, an empty running set does not prove that no call is running:
        the evicted call may still be live.
        """
        return self._evicted_count > 0

    @property
    def evicted_count(self) -> int:
        """Stored calls evicted at the cap since the last :meth:`clear`.

        The overflow, counted: the caller says it out loud once per turn, so a
        map that dropped rows never reads like one that was never that full.
        """
        return self._evicted_count

    @property
    def active_calls(self) -> dict[str, tuple[ToolCallState, object]]:
        """The live ``{toolCallId: (state, aux)}`` map, read-only by convention.

        Backs ``AcpSessionHandle._active_tool_calls`` so the few call sites and
        tests that read it (membership, newest-remaining, the stored aux) keep
        working. An id-less judged call is NOT in this map (it has no key).
        Callers must not mutate it directly — the hand-off goes through
        :meth:`dispatch` / :meth:`result` / :meth:`clear`.
        """
        return self._calls

    def dispatch(self, call_id: str, state: ToolCallState, aux: object = None) -> bool:
        """Record *state* under *call_id* and make it the judged call.

        A falsy *call_id* is judged but not stored (see the class docstring):
        it takes the id-less slot, which the next dispatch overwrites and which
        never appears in ``active_calls``. Returns True — a dispatch always
        changes the judged call.
        """
        self._current_id = call_id or ""
        if call_id:
            self._idless = None
            if (
                self._max_calls is not None
                and call_id not in self._calls
                and len(self._calls) >= self._max_calls
            ):
                self._calls.pop(next(iter(self._calls)), None)
                self._evicted_count += 1
            self._calls[call_id] = (state, aux)
        else:
            self._idless = (state, aux)
        return True

    def result(self, call_id: str, *, terminal: bool) -> bool:
        """Apply a tool result for *call_id*.

        A non-*terminal* (progress) frame is a no-op and returns False — the call
        is still running. A terminal frame drops the stored call; when it was the
        judged one the judged call moves to the newest remaining dispatch, or
        clears. A terminal frame for a call not in the running set (a sibling
        already gone, or a native child whose result never reached here) leaves
        a stored judged call untouched, but DOES move the slot off an id-less
        judged call — the sub-agent's own next result is how a finished native
        child stops being judged. Returns whether the judged call changed.
        """
        if not terminal:
            return False
        existed = (call_id or "") in self._calls
        self._calls.pop(call_id or "", None)
        if self._current_id:
            # A stored call is judged.
            if (call_id or "") != self._current_id:
                # A sibling finished; the judged call is untouched.
                return False
            self._current_id = next(reversed(self._calls)) if self._calls else ""
            return True
        # An id-less call is judged (``_current_id == ""``). Any terminal result
        # this agent sees (its own next completed call) hands the slot to the
        # newest call still running, or clears it — a finished native child must
        # not stay judged for the rest of the prompt.
        if self._idless is None and not existed:
            return False
        self._idless = None
        if self._calls:
            self._current_id = next(reversed(self._calls))
        return True

    def clear(self) -> bool:
        """Drop every call (prompt start). Returns whether a judged call existed."""
        had = bool(self._current_id) or bool(self._calls) or self._idless is not None
        self._calls.clear()
        self._current_id = ""
        self._idless = None
        self._evicted_count = 0
        return had
