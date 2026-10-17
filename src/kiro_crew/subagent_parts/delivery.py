"""How a run's outcome reaches its parent, and the parent-end gate that suppresses it:
the callback contracts, the injection-notice outcome line, the delivery-routing field table
and its parked predicate, and the run ids the teardown gate holds.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import time

    from kiro_crew.subagent import (
        Iterable,
        Iterator,
        LLMEvent,
        SubagentInfo,
    )


def _injection_notice_outcome(info: "SubagentInfo") -> str:
    """One-sentence outcome line for the injection-failure fallback notice.

    ``notify_injection_failed`` fires whenever a terminal report could not be
    injected into the parent — for EVERY terminal state, not just successful
    completion. Asserting "finished" for a run that was stopped or rejected
    before it executed misdescribes the outcome, so the line branches on the
    record's canonical :attr:`SubagentInfo.outcome` with one before-start
    refinement per branch: ``_exec_started`` — the marker ``_run_inner`` sets
    when execution actually begins — is ``None`` exactly when the run never
    executed, which covers every spawn-rejection site (all of them construct
    their record without it) with no wording contract between ``error``
    strings and this notice. The "no result to deliver" phrasings are guarded
    on the absence of any output so they can never contradict the result-path
    recovery hint.

    The completed branch names no delivery MECHANISM. Only two of the six
    ``notify_injection_failed`` call sites pass a timeout; the rest pass
    "provider dead after prompt-busy retries", "ACP process died", a raw
    ``str(exception)`` and the last injection-failure reason. Since the notice
    prints ``reason`` on the line directly above this one, asserting a timeout
    here would contradict it — and the reader is an LLM deciding whether to
    retry. The cause belongs to ``reason``; this line states only the outcome.

    Pure function of the record, unit-tested per branch.
    """
    never_ran = info._exec_started is None and not info.result and not info.result_path
    outcome = info.outcome
    if outcome == "stopped":
        if never_ran:
            return "The run was stopped before it started, so there is no result to deliver."
        return "The run was stopped before it completed."
    if outcome == "failed":
        if never_ran:
            return "The run failed before it started, so there is no result to deliver."
        return "The agent failed before a result could be delivered."
    return "The agent finished, but its result could not be delivered."


# Event callback: (event_type, info, extra_data) -> None
SubagentEventCallback = Callable[[str, "SubagentInfo", dict], Awaitable[None]]


class ToolApprovalCallback(Protocol):
    async def __call__(self, event: LLMEvent, parent_session_key: str = "") -> bool:
        pass


class SpawnApprovalUnreachable(Exception):
    """A spawn-approval prompt has no surface that could ever answer it.

    Raised BY a :class:`SpawnApprovalCallback`, at the point it would otherwise
    park, and handled by the spawn gate in ``subagent_manager/admission/gate.py``.

    Why an exception rather than a ``False`` return, and why the callback rather
    than the gate decides:

    * ``False`` already means "a human refused", and the two must not collapse:
      a refusal is a decision, this is the absence of anyone who could decide.
      They want different prose, and only this one is a misconfiguration.
    * The gate cannot compute the answer. Every non-human auto-approve shortcut
      the callback owns -- ``hooks.auto_approve_sources``, the CLI ``--approval``
      mode, the YOLO override, slot trust -- is evaluated inside the callback and
      never reaches the gate's cascade, so a gate-side probe would have to
      re-derive all four and would reject spawns those rungs mean to allow (the
      ``auto_approve_sources`` opt-in is the documented workaround). Raising from
      the callback puts the check where "we are about
      to park with nobody attached" is the only remaining possibility.

    The message SHOULD name the surface that was missing ("no dashboard client is
    connected"), because the raiser is the only party that knows what the
    surfaces are. The gate quotes it and adds the config rungs, which are the
    gate's own; that split is what keeps the gate's prose from going stale when
    channel-side delivery lands.

    A callback that never raises it keeps today's behaviour unchanged.
    """

    # Reported as ``kiro_crew.subagent``'s, where callers import it from: a traceback,
    # a log line and a pickle name an exception by its module.
    __module__ = "kiro_crew.subagent"


class SpawnApprovalCallback(Protocol):
    async def __call__(
        self, request_id: str, description: str, parent_session_key: str = ""
    ) -> bool:
        pass


# ── Delivery routing state: enumerated from the PRODUCING side ────────────────
#
# Every ``SubagentInfo`` attribute that the modules owning terminal-outcome routing
# WRITE -- ``subagent_manager/terminal.py``, ``subagent_manager/waves.py``,
# ``subagent_manager/cancellation.py`` and the gateway's completion consumer
# (``slack/gateway_runtime/subagent_wiring.py``, ``subagent_report.py``,
# ``subagent_injection.py`` and ``runtime_charge.py``) -- classified by what it says
# about whether the outcome has reached the parent.
#
# The list exists because "has this run's outcome reached its parent" has more than one
# representation, and reading only the obvious one was wrong four separate times. A
# parent-end teardown has to suppress the delivery of a run whose parent is gone, so a
# representation it does not know about is a delivery that lands in a conversation that
# ended -- and the injector CREATES a session when none is live, so that delivery rebuilds
# the conversation the teardown just took down.
#
# ``PARKS_WHEN_SET``  -- truthy means the outcome is parked somewhere and has not landed.
# ``PARKS_WHEN_UNSET`` -- falsy means it has not landed; truthy means it has.
# ``NOT_DELIVERY_STATE`` -- written by those modules but says nothing about delivery.
#
# ``test_the_delivery_parked_states_are_enumerated_from_the_producers`` recomputes the
# write set from those modules' AST and fails when it stops matching this table, so a new
# field written by any of them cannot be added without being classified here.
PARKS_WHEN_SET = "parks-when-set"


PARKS_WHEN_UNSET = "parks-when-unset"


NOT_DELIVERY_STATE = "not-delivery-state"


DELIVERY_ROUTING_FIELDS: "dict[str, str]" = {
    # The gateway parked this member's per-agent injection for the wave digest. Two
    # fields on purpose: the flag is the restart-safety contract the run loop reads, the
    # timestamp is the hold-deadline sweep's only input, and the sweep must not mutate the
    # flag. Either being set means the result is not in the parent's context.
    "_digest_held": PARKS_WHEN_SET,
    "_digest_held_at": PARKS_WHEN_SET,
    # The held SIBLINGS whose delivery tombstones this member owes once its digest is
    # handed off. Non-empty means other runs' deliveries are parked ON this record.
    "_digest_settle_deliveries": PARKS_WHEN_SET,
    # The announce sits in the parent's slot queue because the slot was busy. Delivery is
    # not consumption: a turn has to drain it.
    "_delivery_queued": PARKS_WHEN_SET,
    # Set the moment ``_on_done`` RETURNS. Its truth is the only positive evidence the
    # outcome reached the parent -- which is why it reads the other way round, and why
    # reading it ALONE was wrong: two routes above return having merely parked the work.
    "_reported_to_parent": PARKS_WHEN_UNSET,
    # A synthetic record ``force_digest_flush`` builds to release an expired hold. It is a
    # CARRIER of a future injection rather than a member with a parked outcome, and it
    # carries a fresh id, so an id-keyed gate can never recognise it -- which is why the
    # wave hold is disarmed at its source (``_expired_digest_holds``) instead.
    "_digest_flush_only": NOT_DELIVERY_STATE,
    # A memory-wait expiry's report is owed in the store. It says who clears that mark,
    # not where the outcome is: the parked states above say that.
    "_report_owed": NOT_DELIVERY_STATE,
    # The route positively delivered the report. Set inside ``_on_done``, before
    # ``_reported_to_parent``, it decides only whether a memory-wait expiry's
    # owed mark may be cleared; the teardown gate reads the states above.
    "_report_delivered": NOT_DELIVERY_STATE,
    # An injection was given up on. Nothing in this process delivers the report
    # again, so there is no parked delivery for a teardown to stop; it decides
    # only whether the run's ``delivered`` tombstone is written.
    "_report_undelivered": NOT_DELIVERY_STATE,
    # Run bookkeeping these modules also write. None of them says where an outcome is.
    "_finalized": NOT_DELIVERY_STATE,
    "_reap_reason": NOT_DELIVERY_STATE,
    "_reap_started": NOT_DELIVERY_STATE,
    "_recovering": NOT_DELIVERY_STATE,
    # Recovery clears the first attempt's startup clocks before waiting to
    # launch its replacement. They govern startup reaping, not delivery.
    "_exec_started": NOT_DELIVERY_STATE,
    "_exec_started_mono": NOT_DELIVERY_STATE,
    "_startup_deadline_stamp": NOT_DELIVERY_STATE,
    # Context-overflow recovery clears the retired shared runtime's identity
    # before it waits to start the dedicated replacement. These fields govern
    # process lifecycle and sampling, not where terminal output was delivered.
    "_pid": NOT_DELIVERY_STATE,
    "_session_sharing": NOT_DELIVERY_STATE,
    "_shared_provider": NOT_DELIVERY_STATE,
    # The recovery respawn resets the dead process's RSS readings so the spawn
    # guard prices the fresh process as warming; memory sizing, not delivery.
    "_rss_generation": NOT_DELIVERY_STATE,
    "_rss_samples": NOT_DELIVERY_STATE,
    "_slot_released": NOT_DELIVERY_STATE,
    "_stop_origin": NOT_DELIVERY_STATE,
    "done": NOT_DELIVERY_STATE,
    "elapsed": NOT_DELIVERY_STATE,
    "error": NOT_DELIVERY_STATE,
    "last_rss_gb": NOT_DELIVERY_STATE,
    # Owned continuation work is cancelled separately when a parent ends; its presence
    # says nothing about whether this run's terminal outcome reached that parent.
    "pending_followups": NOT_DELIVERY_STATE,
    "reaped": NOT_DELIVERY_STATE,
    "result": NOT_DELIVERY_STATE,
    "streaming_text": NOT_DELIVERY_STATE,
    "user_stopped": NOT_DELIVERY_STATE,
}


# The modules the table is derived from. Named here so the test and the table cannot
# disagree about which producers were read.
DELIVERY_ROUTING_MODULES: tuple[str, ...] = (
    "subagent_manager/terminal.py",
    "subagent_manager/waves.py",
    "subagent_manager/cancellation.py",
    "slack/gateway_runtime/subagent_wiring.py",
    "slack/gateway_runtime/subagent_report.py",
    "slack/gateway_runtime/subagent_injection.py",
    "slack/gateway_runtime/runtime_charge.py",
)


def delivery_is_parked(info: "SubagentInfo") -> bool:
    """True when this run's outcome has not reached its parent.

    Reads :data:`DELIVERY_ROUTING_FIELDS` rather than naming fields inline, so the
    predicate and the classification cannot drift -- the drift is what let a parked
    representation through on four separate rounds.

    The union is deliberately conservative. Answering True for a run whose delivery has in
    fact landed costs nothing: the gate only SKIPS an injection, and a run that already
    delivered does not inject again. Answering False for a parked one rebuilds a retired
    conversation.
    """
    for field_name, rule in DELIVERY_ROUTING_FIELDS.items():
        value = getattr(info, field_name, None)
        if rule == PARKS_WHEN_SET and value:
            return True
        if rule == PARKS_WHEN_UNSET and not value:
            return True
    return False


def _audit_ids(ids: "Iterable[str]", cap: int = 20) -> str:
    """Render run ids for the parent-end audit line, bounded.

    Bound on the facade (``kiro_crew.subagent``) rather than defined in the component that
    logs, because a component method's module-level names resolve against the facade's
    globals at runtime -- a helper defined beside its caller raises ``NameError`` there.

    A wave can carry more ids than one log line should hold, and a silently truncated list
    is worse than a count: it reads as the whole set.
    """
    listed = list(ids)
    if not listed:
        return "none"
    if len(listed) <= cap:
        return ",".join(listed)
    return ",".join(listed[:cap]) + f",+{len(listed) - cap}-more"


# How long the delivery gate remembers a teardown-cancelled run id when nothing has
# explicitly discarded it.
#
# A BACKSTOP, not the primary rule. The primary rule is that the gate keeps an id until
# that run's delivery has actually been suppressed, which is what ``_report_terminal_impl``
# discards on -- so the ordinary case never depends on this number. It exists for a marked
# run that never reaches a terminal at all.
#
# A day rather than an hour, because an approval-parked run is deliberately NOT cancelled
# (the approval is a person's decision to make) and a person can take far longer than an
# hour to answer. An hour let a later teardown prune the mark while such a run was still
# waiting, and its completion then injected into whatever the key served by then.
_TEARDOWN_GATE_TTL_SECS = 86400.0


class _AgingIdSet:
    """A membership set of run ids that forgets an entry once it is OLD, never when full.

    Age since MARKING is the only eviction rule, and the reason is that the alternative
    is unsafe. A CAPACITY rule evicts by arrival order regardless of whether the run
    could still announce, so a single parent with more queued children than the capacity
    would evict its own earliest ids while its reports were still being spawned -- and
    those reports then walk through the gate and rebuild the conversation the teardown
    took down. An age rule cannot do that: the TTL is chosen to exceed every window in
    which a marked run has an announce left.

    Age is also why the lifetime is not tied to the run's ``_agents`` record. That record
    is popped while a run is still tearing down (a dashboard "clear completed" does it),
    so discarding on the pop would disarm the gate while the run can still announce --
    the same reason ``_teardown_gates`` outlives those records.

    Not an LRU: a read must not extend an entry's life, or a hot gate check on one id
    would keep others alive past the point the TTL is reasoned about.
    """

    __slots__ = ("_marked_at", "_ttl")

    def __init__(self, ttl_secs: float) -> None:
        self._marked_at: dict[str, float] = {}
        self._ttl = max(1.0, float(ttl_secs))

    def _prune(self, now: float) -> None:
        cutoff = now - self._ttl
        if not self._marked_at:
            return
        # Insertion-ordered, and marking times are monotonic, so the expired entries are
        # a PREFIX: stop at the first live one instead of scanning the whole dict.
        for agent_id, marked_at in list(self._marked_at.items()):
            if marked_at > cutoff:
                break
            del self._marked_at[agent_id]

    def add(self, agent_id: str) -> None:
        if not agent_id:
            return
        now = time.monotonic()
        self._prune(now)
        self._marked_at.pop(agent_id, None)
        self._marked_at[agent_id] = now

    def update(self, agent_ids: "Iterable[str]") -> None:
        for agent_id in agent_ids:
            self.add(agent_id)

    def discard(self, agent_id: str) -> None:
        self._marked_at.pop(agent_id, None)

    def __contains__(self, agent_id: object) -> bool:
        return agent_id in self._marked_at

    def __iter__(self) -> "Iterator[str]":
        return iter(tuple(self._marked_at))

    def __len__(self) -> int:
        return len(self._marked_at)
