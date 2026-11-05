"""The loop record, its stop-reason vocabulary and the predicates over one record.

:class:`NudgeLoop` is what ``autonudge.json`` stores one row as and what every
surface serializes. The reasons a loop records when it stops, the sets that decide
which of those stops a re-arm may displace, the error types the service raises, and
the functions that answer a question about ONE record (is it a structured monitor,
has its runtime budget run out, which session keys name a channel conversation, what
its cycle header says) live here with it, because each is a reading of the record's
fields and nothing else. Nothing here touches a service, a clock beyond ``time.time``
or a file.
"""

from __future__ import annotations

import decimal
import math
import secrets
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

from kiro_crew.monitoring.models import MonitorOutcome, MonitorState, retained_outcome_blocks_rearm

if TYPE_CHECKING:
    import asyncio

#: ``stopped_reason`` for a loop whose watched subject finished (a merged or
#: closed pull request). Distinct from the bound reasons because there is nothing
#: left to SERVICE, not because it went well: only a merge is recorded as a
#: success, while a pull request closed without merging is recorded as blocked and
#: still needs a decision. What the two share -- and what this reason means -- is
#: that re-arming would poll a dead subject, so a revival check that treated it as
#: a cap would bring back a watch with nothing to watch.
MONITOR_TERMINAL_REASON = "monitor_terminal"

#: ``stopped_reason`` for a loop whose own kill switch fired: the stop file at
#: ``stop_sentinel_path`` existed when the timer woke. The file is how the agent
#: declares a ``{{STOP_FILE}}`` goal reached, so the record is KEPT and deactivated
#: rather than removed: a removed row would leave the goal popover on its empty form
#: with nothing saying the goal was met.
#: The stop file stays where it was written; the next default-sentinel arm on the
#: slot removes it after its new row is durable (``mutations._retire_stale_stop_sentinel``).
STOP_SENTINEL_REASON = "stop_sentinel"

#: The two FINISHED stops -- the agent created its stop file, or the watched
#: subject merged or closed. Terminal in a way the bounds are not: there is nothing
#: to resume, so a revival (``update(active=True)``) is refused whoever asks -- the
#: popover's Play, ``monitor_update``, an app reconciler re-arming its loops -- and
#: the only ways on are to clear the record or to arm a NEW loop, which may displace
#: it (both are in ``_REPLACEABLE_LOOP_STOP_REASONS`` below). The popover renders
#: both as Done.
FINISHED_LOOP_REASONS = frozenset({STOP_SENTINEL_REASON, MONITOR_TERMINAL_REASON})


_MIN_IDLE_SECS = 15
_MAX_IDLE_SECS = 86400  # 24h


# Persisted source category for a deliberate ``autonudge_stop`` directive.
# The caller's free-form explanation is intentionally not stored on the row: it
# is model-authored text and the watchdog only needs the deterministic source.
# On the REMOVAL path it reaches the WARNING stop line instead, through
# ``remove(stop_detail=...)`` (``autonudge_stop_log``); a deactivated row logs
# its ``stopped_reason`` alone.
AUTONUDGE_STOP_REASON = "autonudge_stop"


# Persisted reason for a loop stopped because one of its cycles could not obtain
# tool approval. Named separately from the other bounds because its remedy is
# different in kind: the cap and the budget are raised, this one needs an
# authorization the loop cannot grant itself.
APPROVAL_STALL_REASON = "approval_stalled"


# Persisted reason for a loop that stood down because its cycles kept failing to
# get a model session at all (``session/new timed out``). Each such cycle costs a
# full dispatch and produces nothing, so re-arming on the plain interval only repeats
# the failure until ``max_cycles`` runs out. System-imposed (the loop cannot ease
# host pressure itself), so it is re-armable.
SESSION_START_FAILURE_REASON = "session_start_failures"


# Consecutive start failures before a wake is DEFERRED instead of fired, and
# before the loop stands down for good. Two separate numbers because they answer
# two different questions: the first assumes the host is briefly busy and slows
# the poll (the failures are themselves evidence of contention, so retrying at
# full rate adds to it), the second concludes that whatever is wrong is not
# clearing and stops spending turns on it. A single landed turn on the slot
# clears the streak, so a loop that recovers is never held back.
_START_FAILURE_BACKOFF_AFTER = 3
_START_FAILURE_STANDDOWN_AFTER = 5


# Persisted reason for a loop stood down because its own delivered cycles kept
# FAILING after dispatch (turn outcome ``error`` or ``timeout``). It covers what
# ``structural_terminal``, ``approval_stalled`` and ``session_start_failures`` leave
# out, so a loop whose turn always fails does not spend its whole cycle cap.
# Re-armable like them. Only this loop's own DELIVERED faulted cycles advance it, and
# a single landed turn on the slot clears it.
CONSECUTIVE_FAILURE_REASON = "consecutive_failures"


# Consecutive failed own-cycles before the loop stands down. Higher than the
# start-failure stand-down because a failed turn is a broader, noisier signal
# than a session that never started -- a couple of transient backend errors are
# weather, five in a row with nothing landing between them is a loop that cannot
# make progress and is only spending turns to keep failing.
_CONSECUTIVE_FAILURE_STANDDOWN_AFTER = 5


# Persisted reason for a loop stopped because its LAST delivered cycle ended on
# a STRUCTURAL terminal error -- the backend rejected the prompt's shape as
# malformed, deterministically, so re-firing the identical context every
# interval can only reproduce the rejection. System-imposed like a spent bound:
# the remedy is a NEW context (a human /clear then a fresh message, which the
# genuine-turn reset in chat_runner already clears the slot's verdict for), so a
# later directive re-arm may displace it -- it is a member of
# ``_REPLACEABLE_LOOP_STOP_REASONS`` (via ``_TERMINAL_BOUND_REASONS`` below) for
# exactly that reason.
STRUCTURAL_TERMINAL_REASON = "structural_terminal"


# Persisted reason for a loop stopped because its LAST delivered cycle ended on a
# model-side content refusal that nothing retried. A refusal repeats for the same
# prompt and model, and ``agent.refusal_fallback_model`` is attended-only, so re-firing
# would spend every remaining cycle on refusal cards. Re-armable like the structural
# stop; any genuine new turn in the session clears the slot's verdict.
MODEL_REFUSED_REASON = "model_refused"


def new_goal_token() -> str:
    """A fresh opaque identity for a goal write.

    Random rather than content-derived so the value can be served next to the goal's
    own redaction without becoming a brute-force oracle against the masked span. Its
    only consumer compares it for equality against a value from a prior GET.
    """
    return secrets.token_hex(16)


class AutoNudgeStaleBaseline(RuntimeError):
    """Raised when an update's confirmed baseline does not match the stored goal.

    Compared INSIDE ``_update_unserialized``'s lock, because any check outside it is the
    TOCTOU this exists to close: a second client committing between a caller's read and
    its write would otherwise have its goal silently overwritten last-write-wins. The
    HTTP layer answers this with 409 so the loss becomes a refusal the user can see.
    """


#: Stable caller-facing text for the admission refusal raised after shutdown closes.
#: Authorizers return it unchanged, so callers that must distinguish this 503 from
#: audit or credential outages compare against this value rather than copying text.
SERVICE_SHUTTING_DOWN_MESSAGE = (
    "The gateway is restarting, so this was not applied. Try again once it is back."
)


class NudgeAdmissionReason(str, Enum):
    """The typed causes an admission refusal can expose at a caller boundary."""

    SESSION_CHANGED = "session_changed"
    SERVICE_SHUTTING_DOWN = "service_shutting_down"
    PERSISTENCE_LEASE_CONSUMED = "persistence_lease_consumed"


class NudgeAdmissionRefused(RuntimeError):
    """A mutation cannot enter or finish under its requested admission."""

    def __init__(
        self,
        message: str,
        *,
        reason: NudgeAdmissionReason = NudgeAdmissionReason.SESSION_CHANGED,
    ) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class _MutationAdmission:
    """One service-generation lease for durable mutation writes.

    Captured by a mutation before its first await, so a shutdown that closes
    admission while the caller waits on a lock still lets that already-admitted
    owner reach its durable boundary. ``generation`` ties the lease to one
    ``start()``; a single-use lease (the default) admits exactly one store write,
    while a timer callback that crossed its post-sleep boundary holds a
    multi-write lease for the writes its own task makes (``owner_task``).
    """

    generation: int
    owner_task: "asyncio.Task[Any] | None" = None
    allow_multiple_persistence: bool = False
    persistence_submitted: bool = False


@dataclass
class _TransactionSubmissions:
    """How many store writes one admitted transaction has started.

    ``_run_admitted_transaction`` creates one for the child it supervises and carries
    it in that child's copied context, so only a write the transaction itself starts
    counts. A detached write under the same lease runs in another context and leaves
    it untouched. The caller's cancellation hold reads it: a caller cancelled before
    this transaction submitted anything leaves at once.
    """

    count: int = 0


# The two stops where a bound the user typed ran out, each the ending ``_timer``
# records when that bound trips. The user's resume (``fresh_run``) resets the
# counter BEHIND the spent bound alone: a spent cap zeroes ``cycle_count``, a
# spent budget re-anchors ``created_ts``, and each is read either from the reason
# the loop stopped with or from the bounds as they stand at the press (the wall
# clock keeps running through a pause). Play on a spent bound is otherwise a dead
# press, re-stopped on its first tick unless the bound is raised first; the other
# counter describes an allowance that is not spent and is kept, because the cap is
# a lifetime limit the user typed and a pause must not quietly mint a fresh one.
CYCLE_CAP_REASON = "cycle_cap"
RUNTIME_BUDGET_REASON = "runtime_budget"
_BUDGET_EXHAUSTED_REASONS = frozenset({CYCLE_CAP_REASON, RUNTIME_BUDGET_REASON})


# System-imposed terminal bounds. Membership here gives a reason TWO properties:
# (1) ``update`` refuses to overwrite an ALREADY-inactive loop with one of these
# (the no-op branches in ``_update_unserialized`` read ``_KEPT_STOP_REASONS``,
# which these are part of), so a stop these mark cannot clobber a
# manual pause the user landed first -- e.g. a structural stop firing on an
# in-flight cycle right after the user paused must NOT replace that pause and
# make it directive-revivable; and (2) they are re-armable (folded into
# ``_REPLACEABLE_LOOP_STOP_REASONS`` below). ``structural_terminal`` needs both,
# for the same reason ``cycle_cap``/``runtime_budget`` do.
_TERMINAL_BOUND_REASONS = _BUDGET_EXHAUSTED_REASONS | {
    APPROVAL_STALL_REASON,
    STRUCTURAL_TERMINAL_REASON,
    SESSION_START_FAILURE_REASON,
    CONSECUTIVE_FAILURE_REASON,
    MODEL_REFUSED_REASON,
}


# Persisted reason for a loop ``_load`` deactivated because its kill-switch path
# became sensitive (``repair_sentinel_path`` dropped it). System-imposed: the
# re-arm path re-validates the sentinel, so displacing this row is safe.
SENTINEL_DROPPED_REASON = "sentinel_dropped"


# Persisted reason for a paused loop that recorded no reason of its own —
# ``update(active=False)`` stores this default.
MANUAL_STOP_REASON = "manual"


#: Stops the SYSTEM imposed on a legacy loop, which a directive re-arm may
#: therefore displace: a lapsed approval, a spent bound, a finished subject (a
#: merged or closed watch, or the loop's own stop file), a dropped kill switch.
#: Everything else — a manual pause (``"manual"``), an
#: app's stop tombstone (``AUTONUDGE_STOP_REASON`` on a slot the
#: ``stopped_loop_tombstone`` seam keeps, read by that app to tell
#: deliberate completion from crash cleanup), and any reason
#: this version does not know — is evidence some consumer may read, so it fails
#: CLOSED to preserved.
_REPLACEABLE_LOOP_STOP_REASONS = (
    _TERMINAL_BOUND_REASONS | FINISHED_LOOP_REASONS | {SENTINEL_DROPPED_REASON}
)


#: Stops a LATER deactivation must not overwrite. A reasonless pause (the goal
#: popover's, pressed off a record that still read running) or a bound-tagged stop
#: reaching a row one of these already stopped keeps the earlier reason: the record
#: keeps why the loop ended, which the popover words its state by -- and for a
#: finished loop the reason is also what refuses the revival, so ``manual`` stamped
#: over it would hand Play back on a loop with nothing to resume.
_KEPT_STOP_REASONS = _TERMINAL_BOUND_REASONS | FINISHED_LOOP_REASONS


def reason_in(value: object, reasons: frozenset[str]) -> bool:
    """Whether a STORED stop reason is one of ``reasons``.

    A loop's ``stopped_reason`` is read back from the state file as written, so
    a hand-edited or malformed value can be anything; a non-string matches no
    reason rather than raising on the set lookup, which is how the resume path
    reads it too.
    """
    return isinstance(value, str) and value in reasons


def _stopped_row_is_replaceable(loop: "NudgeLoop") -> bool:
    """Whether a directive re-arm (``replace_stopped``) may displace this row.

    Callers have already established the row is INACTIVE. The split is by who
    recorded the stop: a stop the system imposed (bound expiry, approval
    stall, terminal subject, crash retirement) is automatically re-armable,
    while a stop a person or an app recorded — ``USER_STOP``, session-close
    retention, a manual pause, a research tombstone — is retained evidence.
    Unknown outcomes and reasons are treated as evidence (fail closed).

    An EMPTY reason is evidence too: a pause recorded before the reason field
    existed carries one, and the store holds nothing that tells it apart from a
    torn write. The torn shape that CAN be told apart — no reason AND a live
    deadline — is resumed by ``_load`` (``_is_torn_deactivation``) before any
    re-arm asks, so refusing here costs nothing for that case.
    """
    state = loop.monitor
    if state is not None and state.outcome is not None:
        # Delegated to the shared predicate in ``monitoring.models`` so the MCP
        # preflight, which reads the same record over the session-monitor
        # endpoint, cannot answer differently from this enforcement point. The
        # quarantined-record and fail-closed rules live there.
        return not retained_outcome_blocks_rearm(state.outcome, state.stopped_reason)
    return (loop.stopped_reason or "") in _REPLACEABLE_LOOP_STOP_REASONS


# Namespaced session-key prefixes that identify messaging-channel sessions
# (as opposed to bare dashboard chat-slot keys). Channel-bound loops have no
# dashboard turn-lifecycle hooks (notify_turn_complete / notify_user_input),
# so they run on a fixed interval instead of an idle timer: the timer re-arms
# itself right after every delivered fire.
#
# Mirrors ``messaging.link.CHANNEL_SESSION_NAMESPACES`` but is spelled out, because:
# 1. importing ``kiro_crew.messaging.link`` pulls about 48 more modules into every
#    MCP server process, for one tuple of literals;
# 2. this is a key-shape question. A runtime ``supports_proactive_send`` lookup
#    would read a loop whose transport is briefly absent as a dashboard slot, and
#    nothing would ever re-arm it.
#
# Membership does NOT assert deliverability: ``chat_runner._resolve_channel_target``
# decides that and refuses with a logged reason. So ``whatsapp`` (no transport) and
# ``feishu`` (``supports_proactive_send=False``) are still listed, since an unlisted
# key would silently stop being re-armed. Nor is it an arming permission; that is
# ``binding_key_for``.
_CHANNEL_KEY_PREFIXES = (
    "slack:",
    "discord:",
    "telegram:",
    "wecom:",
    "whatsapp:",
    "webex:",
    "teams:",
    "weixin:",
    "imessage:",
    "feishu:",
    "unified:",
)


def is_channel_key(key: str) -> bool:
    """True when *key* names a messaging-channel session (``slack:<ts>``,
    ``discord:{agent}:direct:{user}`` ...) rather than a dashboard chat slot.

    A CLASSIFICATION, not a permission: see :data:`_CHANNEL_KEY_PREFIXES` for why
    the set is spelled out, and why membership says nothing about whether a nudge
    can be delivered. Callers asking "may this session be armed?" want
    :func:`binding_key_for` instead.
    """
    return key.startswith(_CHANNEL_KEY_PREFIXES)


class AutoNudgeStoreUnvetted(RuntimeError):
    """Raised when a persist is attempted after the loader refused the store.

    An empty ``_loops`` then means "could not vet" rather than "store is empty", so
    writing it would delete rows the operator still has to correct.

    This must RAISE rather than return: every mutation caller already wraps its persist
    in ``except BaseException`` and rolls back, so returning success defeated those
    handlers and left the caller confirming a loop that existed only in memory.
    """


@dataclass
class NudgeLoop:
    """A single auto-nudge loop bound to one session.

    ``slot_key`` is the binding key: either a bare dashboard chat-slot key
    (e.g. ``chat-1-1721...``, idle-timer driven via notify_turn_complete) or a
    namespaced messaging-channel session key (e.g. ``slack:<thread_ts>``,
    ``discord:{agent}:direct:{user_id}``), which runs on a fixed interval.
    The field keeps its historical name for store/REST/WS compatibility.
    """

    id: str
    slot_key: str
    message: str
    idle_secs: int = 60
    max_cycles: int = 0  # 0 = unlimited
    cycle_count: int = 0
    active: bool = True
    last_fire_ts: float = 0.0
    created_ts: float = 0.0
    stop_sentinel_path: str = ""  # optional absolute path; if present loop halts
    # Opaque per-write identity of ``message``, for stale-baseline (409) detection.
    # RANDOM: a digest served beside its own redaction is an oracle for the masked span.
    # NOT PERSISTED -- re-minted on every load, so a pre-restart value cannot authorise.
    goal_token: str = ""
    # Runtime budget in seconds, measured from ``created_ts`` (0 = unlimited); a
    # cycle cap alone cannot bound cost when turns are slow. Anchored on the persisted
    # ``created_ts`` so a restart never resets the clock, and it keeps running through
    # a pause. Only a ``fresh_run`` resume of a time-spent loop re-anchors it
    # (``_update_unserialized``); a spent cap zeroes ``cycle_count`` on its own.
    max_runtime_secs: int = 0
    #: Whether this loop may be observation-gated. Defaults to FALSE, which is what
    #: an absent key decodes to.
    #:
    #: Gating can silently stop work (a gated loop DEACTIVATES when its subject merges
    #: or closes), so every uncertainty resolves to UNGATED and only an explicit
    #: boolean true gates. Persisted so an opt-out survives: editing the instruction
    #: re-infers the subject, and would otherwise re-gate the loop.
    gate: bool = False
    #: The owner's judge brief: ``targets``, ``wake_when``, ``quiet_when``. Empty
    #: means the owner named no criteria of their own, NOT that no judge applies: a
    #: gated loop is then screened under
    #: :func:`~kiro_crew.autonudge_judge.default_spec`. The explicit bypass is
    #: ``judge: false``, which normalises to the reserved
    #: :data:`~kiro_crew.validation.JUDGE_OFF_KEY` marker stored here, and an ungated
    #: loop is never screened at all.
    #:
    #: The default is never written back, so this records only what the OWNER asked
    #: for, and the mid-tick replacement guard compares a re-read against it.
    #: Stored even while the consent scope is off, so an armed loop survives the switch
    #: being turned on; nothing is COLLECTED or sent until ``nudge_evidence`` is granted.
    judge: dict = field(default_factory=dict)
    #: Per-target read cursor, ``target -> next_since``, so each tick reads only the
    #: rows that arrived since the last one. Advanced only on a successful read, so
    #: a refused or failing target does not silently skip its own rows.
    judge_cursors: dict = field(default_factory=dict)
    #: What the pull-request reading looked like the last time this loop reached a
    #: verdict: ``{"digest": str, "remarks": [id, ...]}`` -- whether the subject moved,
    #: and which remarks it had already been shown.
    #:
    #: Not ``MonitorState.last_observation``: the reading is published BEFORE the judge
    #: is asked, and user input can cancel the ask. Advancing the baseline there would
    #: mark a new comment seen with no verdict, suppressing a wake nobody judged. A tick
    #: that did not reach a verdict consumes nothing.
    judge_pr_seen: dict = field(default_factory=dict)
    #: Consecutive judge QUIET verdicts, and the counter the judge's streak floor is
    #: measured against. Its OWN field rather than ``MonitorState.quiet_streak``:
    #: that record requires a probe ``kind`` and ``target``, and a loop watching
    #: sibling sessions has no honest value for either, so a judge-only loop must be
    #: able to hold a streak without one.
    judge_quiet_streak: int = 0
    #: A judge verdict decided this loop should FIRE and that delivery is not yet
    #: confirmed. Set with the verdict's durable write and cleared only once the fire
    #: is settled, so the owed turn survives a process that stops in between.
    #: On the LOOP, not ``MonitorState``, because a judge-only loop has no monitor
    #: record; that is also why a REFUSED fire is covered here, not by
    #: ``followup_ticks``. A lost clear costs one extra fire, the safe direction: the
    #: judge already advanced its read cursors, so the next tick would see nothing new.
    judge_wake_pending: bool = False
    #: The previous verdict, summarised and text-free, carried into the next tick's
    #: state so a judge can see it already passed on comparable evidence once.
    judge_last_verdict: dict = field(default_factory=dict)
    #: The last few verdicts with the label their delivery earned, oldest first: the
    #: judge's hit rate ON THIS LOOP, since a worthwhile wake differs per subject.
    #: Bounded to :data:`~kiro_crew.autonudge_judge.MAX_STORED_VERDICTS`, sized from the
    #: QUIET-STREAK FLOOR so the delivery closing a full streak is still stored when
    #: the retroactive pass labels the suppressions that opened it.
    judge_recent_verdicts: list = field(default_factory=list)
    # WHY the loop was last deactivated: "" (active / never stopped),
    # "manual" (user pause / any caller that didn't say otherwise),
    # "autonudge_stop" (deliberate directive), "cycle_cap",
    # "runtime_budget", or "approval_stalled" (set by _timer's terminal
    # bounds), "stop_sentinel" (the stop file existed when _timer woke) or
    # "monitor_terminal" (the watched subject merged or closed). "approval_stalled"
    # is still read on old rows written before approval stalls became a hold.
    # Persisted so revival can tell a manual pause from a bound expiry: runtime keeps
    # growing after a pause, and a budget raise must not resume a loop the user paused.
    stopped_reason: str = ""
    # Evidence that a cycle in this loop's session asked for tool approval and
    # nobody answered within the window. Set by ``notify_approval_stalled`` and
    # read by ``_timer`` as a HOLD: the loop stays active but fires no cycle, spending
    # neither its cycle cap nor its runtime budget. Set only on proof, never on
    # prediction. Cleared by ``release_approval_hold`` once a person is back, and on
    # every revival. Persisted, because the cause usually outlives a restart.
    approval_stalled: bool = False
    # When the current hold began (0 = not held, or a legacy row). On release the
    # held time is added to ``created_ts``, so a hold does not spend the runtime
    # budget: a loop held overnight resumes with the budget it had left.
    approval_stalled_at: float = 0.0
    # The person-wait hold of a work-ledger watch (``_holds_for_person_wait``).
    # ``ledger_seen_fp`` is the ledger's fingerprint read at the top of the tick that
    # delivered the last turn, set only while every open item waited on a person
    # ("" otherwise). A later tick whose ledger still matches it holds, and sets
    # ``waiting_on_person`` so the popover and ``monitor_inspect`` can say so. A
    # worker's report changes the fingerprint and releases it. Persisted, so a
    # restart's replay tick holds again when nothing moved meanwhile.
    ledger_seen_fp: str = ""
    waiting_on_person: bool = False
    # When the current person-wait hold began (0 = not held). On release the held
    # time is added to ``created_ts``, so the hold spends neither the runtime budget
    # nor the backstop, the same credit ``approval_stalled_at`` gives.
    waiting_on_person_at: float = 0.0
    # How many of this loop's cycles in a row ended without ever getting a model
    # session. Raised by ``notify_cycle_start_failed``, zeroed by
    # ``notify_cycle_landed``. Past ``_START_FAILURE_BACKOFF_AFTER`` ``_timer`` defers
    # the wake; past ``_START_FAILURE_STANDDOWN_AFTER`` the loop stops with
    # ``SESSION_START_FAILURE_REASON``. Persisted, and cleared on every revival.
    consecutive_start_failures: int = 0
    # How many of this loop's OWN delivered cycles in a row ended in a fault --
    # a turn that dispatched but died (outcome ``error`` or ``timeout``). Raised by
    # ``notify_cycle_failed``, zeroed by ``notify_cycle_landed`` (any landed turn on
    # the slot). Past ``_CONSECUTIVE_FAILURE_STANDDOWN_AFTER`` the loop stops with
    # ``CONSECUTIVE_FAILURE_REASON``. Separate from ``consecutive_start_failures``
    # because the remedies differ. Persisted, and cleared on every revival.
    consecutive_failed_cycles: int = 0
    # Absolute wall-clock deadline for the next fire (0 = unset: the next arm
    # starts a fresh full countdown). User turns cancel the timer TASK but never touch
    # this, so the schedule survives an active conversation. Cleared on every delivered
    # fire: the next cycle counts from the nudge turn's END (notify_turn_complete).
    # Persisted on every assignment; a lost background write degrades to a fresh full
    # countdown after restart, never a lost or premature fire.
    next_due_ts: float = 0.0
    # Optional observation/controller state. ``gate=True`` records belong to the
    # prompt path; controller-owned records carry state with ``gate=False``.
    monitor: MonitorState | None = None
    # Durable delivery generation outside MonitorState. A future-version monitor
    # is retained as an opaque payload, so mutating a field inside its local view
    # cannot survive serialization. Keeping the exact terminal identity on the
    # stable outer record preserves both the opaque bytes and restart dedupe.
    terminal_notification_outcome: str = ""
    terminal_notification_stopped_at: float = 0.0
    # Optional SHORT stand-in for ``message`` in the VISIBLE dashboard
    # transcript row (empty = show ``message``). A person needs only "a nudge
    # happened", but a multi-KB message repeated every cycle bloats the session file.
    # The PROMPT always gets the full ``message`` (``GatewayOrchestrator._fire_dashboard_nudge``).
    # Channel-bound loops have no separate display row, so this does not apply to them.
    banner: str = ""
    # WHO armed this loop, reduced to the one distinction the authorizer needs:
    # True when the arming request came from a turn OF THE BOUND SESSION ITSELF
    # (the ``monitor_start`` / ``monitor_watch`` session directive, applied by
    # the turn loop to the exact session that produced it), False for every
    # external arm (REST, workflow ``ctx.nudge``, an app handler).
    #
    # Crew- and member-mode slots refuse automation armed from OUTSIDE the session,
    # but must admit their own ``monitor_start``. The FIRE-time re-check in
    # ``GatewayOrchestrator._fire_dashboard_nudge`` reads this to tell a self-armed
    # member loop from a slot that switched into crew mode after an outsider armed it.
    # Persisted so a restart does not refuse a self-armed loop; absent decodes to False.
    self_armed: bool = False
    # Monotonic per-loop CONFIG generation. Advanced by ``_update_unserialized``
    # ONLY on a real configuration change (a changed ``message``) or a revival
    # (inactive -> active), never by timer/cycle bookkeeping. Captured at fire time
    # and compared under ``_lock`` before a structural-terminal stop, so a stale
    # completion of an OLD instruction cannot stop a loop re-committed since (the
    # A->B->A case ``message`` equality cannot tell apart). Absent decodes to 0.
    config_generation: int = 0
    # The gateway's default conductor patrol (``conductor_patrol.ensure_patrol``,
    # armed when a conductor binds a worker and holds no active loop). The one thing it
    # changes: a create-only arm of a loop that is NOT a default patrol -- the
    # conductor's own ``monitor_start`` -- displaces an ACTIVE default patrol
    # instead of answering 409, so the agent's explicit arm always wins over the
    # gateway's fallback. Persisted so the rule survives a restart. Only an
    # explicit boolean True counts; anything else decodes to False.
    default_patrol: bool = False


def is_structured_monitor_loop(loop: NudgeLoop) -> bool:
    """Distinguish controller records from prompt loops carrying probe state."""
    return getattr(loop, "monitor", None) is not None and not getattr(loop, "gate", False)


def terminal_notification_delivery_matches(
    loop: NudgeLoop,
    outcome: MonitorOutcome,
    stopped_at: float,
) -> bool:
    """Whether this exact terminal generation has a durable delivery record."""
    if loop.terminal_notification_outcome:
        return (
            loop.terminal_notification_outcome == outcome.value
            and loop.terminal_notification_stopped_at == stopped_at
        )
    state = loop.monitor
    return state is not None and state.terminal_notification_delivered


class MonitorUpdateConflict(ValueError):
    """A structured mutation would break active action correlation."""


class MonitorCredentialFollowUpFailed(RuntimeError):
    """A committed monitor update's credential follow-up failed."""

    def __init__(
        self,
        loop: NudgeLoop,
        *,
        kind: Literal["revoke", "grant"],
        rolled_back: bool,
    ) -> None:
        self.loop = loop
        self.kind = kind
        self.rolled_back = rolled_back
        super().__init__(f"monitor credential {kind} follow-up failed")


class MonitorCredentialRollbackFailed(RuntimeError):
    """A credential follow-up and its monitor compensation both failed."""

    def __init__(
        self,
        loop: NudgeLoop,
        *,
        kind: Literal["revoke", "grant"],
    ) -> None:
        self.loop = loop
        self.kind = kind
        super().__init__(f"monitor credential {kind} rollback failed")


def runtime_budget_exceeded(loop: "NudgeLoop", now: float | None = None) -> bool:
    """True when *loop* has a runtime budget and it is spent.

    Single source of truth shared by ``_timer`` (enforcement) and the expiry
    notifier (wording), so the two can never disagree on WHY a loop stopped.
    A loop with no ``created_ts`` (a malformed/legacy store entry) never
    trips the budget — there is no anchor to measure from, and guessing one
    could kill a healthy loop on its first cycle after an upgrade. A *now*
    the caller passes is a wall-clock reading and is used as given; without
    one the spent time comes from :func:`runtime_elapsed`.
    """
    if not loop.max_runtime_secs or not loop.created_ts:
        return False
    if now is not None:
        return now - loop.created_ts >= loop.max_runtime_secs
    return runtime_elapsed(loop, loop.created_ts) >= loop.max_runtime_secs


def runtime_elapsed(loop: "NudgeLoop", created_ts: float) -> float:
    """Seconds of the runtime budget spent since *created_ts*.

    ``created_ts`` is wall-clock and persisted, which is what lets the budget
    survive a restart. The time spent is read from it once per process -- on
    the first read after a load -- and advanced on the monotonic clock from
    there, so a wall-clock step while the gateway runs neither spends the budget
    nor gives it back. A writer that moves ``created_ts`` in this process moves
    the anchor with it (:func:`restart_runtime_clock`,
    :func:`credit_held_runtime`); a ``created_ts`` the anchor was not taken for
    is read again as after a load. The anchor lives in the instance's
    ``__dict__``, outside the dataclass fields, so it is never serialized and
    every load takes a fresh one.
    """
    anchor = loop.__dict__.get(_RUNTIME_ANCHOR)
    if anchor is None or anchor[0] != created_ts:
        anchor = (created_ts, max(0.0, time.time() - created_ts), time.monotonic())
        loop.__dict__[_RUNTIME_ANCHOR] = anchor
    return max(0.0, float(anchor[1] + (time.monotonic() - anchor[2])))


#: Where :func:`runtime_elapsed` keeps the budget's anchor on a loop, and where
#: :func:`note_hold_start` keeps each hold's monotonic start. Both live in the
#: instance's ``__dict__``, outside the dataclass fields, so neither is stored.
_RUNTIME_ANCHOR = "_runtime_anchor"
_HOLD_STARTS = "_hold_monotonic_starts"
#: The two holds that hand their time back to the budget when they end.
APPROVAL_HOLD = "approval"
PERSON_WAIT_HOLD = "person_wait"


def restart_runtime_clock(loop: "NudgeLoop") -> None:
    """Anchor the budget at the ``created_ts`` just written as now: nothing spent yet.

    For the writers that set ``created_ts`` to the current time (a new loop, and a
    resume that re-anchors a spent budget), so the first read does not take the
    spent time from the wall clock, which may have stepped since the write.
    """
    loop.__dict__[_RUNTIME_ANCHOR] = (loop.created_ts, 0.0, time.monotonic())


def snapshot_runtime_anchor(loop: "NudgeLoop") -> tuple[float, float, float] | None:
    """The budget's anchor on *loop* as it stands, or None before its first read."""
    anchor: tuple[float, float, float] | None = loop.__dict__.get(_RUNTIME_ANCHOR)
    return anchor


def restore_runtime_anchor(loop: "NudgeLoop", anchor: tuple[float, float, float] | None) -> None:
    """Put back an anchor :func:`snapshot_runtime_anchor` returned.

    For a rollback that restores ``created_ts``. The anchor is keyed to the
    ``created_ts`` it was taken for and lives outside the dataclass fields such a
    rollback restores, so a writer that re-anchored (:func:`restart_runtime_clock`)
    and then failed would leave an anchor keyed to a ``created_ts`` the loop does not
    hold. :func:`runtime_elapsed` would read the spent time from the wall clock again,
    with any step the wall clock took since the anchor it replaced was taken. None is
    put back as found: :func:`runtime_elapsed` reads it as it reads no anchor.
    """
    loop.__dict__[_RUNTIME_ANCHOR] = anchor


def note_hold_start(
    loop: "NudgeLoop", kind: str, since: float, *, mono: float | None = None
) -> None:
    """Pair the start of *loop*'s *kind* hold, wall stamp *since*, with a monotonic reading."""
    loop.__dict__.setdefault(_HOLD_STARTS, {})[kind] = (
        since,
        time.monotonic() if mono is None else mono,
    )


def hold_span(loop: "NudgeLoop", kind: str, since: float, now: float) -> tuple[float, float]:
    """The monotonic start of *loop*'s *kind* hold, and how many seconds it has lasted.

    Measured from :func:`note_hold_start`'s reading when the hold began in this
    process. A hold that began before a restart has none, and the wall clock's
    span from *since* to *now* is all there is; a stamp that is not a positive
    number at or before *now* spans nothing.
    """
    mono = time.monotonic()
    start = loop.__dict__.get(_HOLD_STARTS, {}).get(kind)
    if start is not None and start[0] == since:
        return start[1], max(0.0, mono - start[1])
    if isinstance(since, bool) or not isinstance(since, (int, float)) or not 0 < since <= now:
        return mono, 0.0
    return mono - (now - since), now - since


def credit_held_runtime(loop: "NudgeLoop", previous_created: float, held: float) -> None:
    """Hand *held* seconds of a hold that just ended back to the budget's anchor.

    The caller has moved ``created_ts`` forward by the hold's wall-clock span,
    which is what a restart reads. The anchor, which the budget reads while the
    gateway runs, moves by *held*, the span :func:`hold_span` measured, and is
    keyed to the new ``created_ts``, so :func:`runtime_elapsed` does not re-read
    the spent time from the wall clock: a wall-clock step before or during the
    hold neither spends the budget nor gives it back. An anchor this process has
    not taken for *previous_created* is left to the first read.
    """
    anchor = loop.__dict__.get(_RUNTIME_ANCHOR)
    if anchor is not None and anchor[0] == previous_created:
        loop.__dict__[_RUNTIME_ANCHOR] = (loop.created_ts, anchor[1] - held, anchor[2])


def cap_reached(loop: "NudgeLoop") -> bool:
    """True when *loop* has a cycle cap and its count has reached it.

    The cap check ``_timer`` makes before a fire, asked at resume time so the
    user's resume cannot read "cycles left" on a loop the timer would re-stop
    unfired. Read defensively through ``_positive_number``, as
    ``nudge_cycle_header`` reads the same fields: ``_load`` leaves the bounds as
    the store wrote them, and this runs inside a revival after ``active`` has
    flipped and before the write, so a non-numeric value an agent wrote must
    read as "no cap" rather than raise and leave the loop half-revived in memory.
    """
    cap = _positive_number(loop.max_cycles)
    return bool(cap) and _positive_number(loop.cycle_count) >= cap


def budget_elapsed(loop: "NudgeLoop", now: float | None = None) -> bool:
    """True when *loop* has a runtime budget and the clock has run it out.

    ``runtime_budget_exceeded`` asked at resume time, with the same defensive
    reads as :func:`cap_reached` and for the same reason. The clock keeps running
    through a pause, so a loop paused with an hour of budget left and resumed the
    next day reads as spent here exactly as the timer would read it. Without *now*
    the time spent is :func:`runtime_elapsed`, the timer's own reading, so a
    wall-clock step cannot make the resume and the timer disagree.
    """
    budget = _positive_number(loop.max_runtime_secs)
    anchor = _positive_number(loop.created_ts)
    if not budget or not anchor:
        return False
    return (now - anchor if now is not None else runtime_elapsed(loop, anchor)) >= budget


#: Share of a loop's cycle or runtime cap at or under which the nudge header
#: says ``10% or less left``. Mirrors ``RENEW_THRESHOLD`` in goal-conductor's
#: ``patrol_budget.py``, which re-checks it before it renews.
NUDGE_RENEW_DUE_SHARE = 0.10


def _positive_number(value: object) -> float:
    """``value`` as a finite positive float, else 0.

    ``inf`` would pass ``> 0`` and then make ``int()`` raise, and an int too big
    for a float makes the float arithmetic raise, so both are read as "no cap".
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        number = float(value)
    except OverflowError:
        return 0
    return number if math.isfinite(number) and number > 0 else 0


#: The two persisted bounds a user types, in the order a log line names them.
LOOP_BOUND_FIELDS = ("max_cycles", "max_runtime_secs")

#: The largest limit a stored bound string that is not plain digits can name: the
#: largest float, the range a float reading of such a string accepted. It also keeps
#: ``int()`` from building an integer of any size from an exponent (``"1e999999999"``).
_MAX_TEXT_BOUND = decimal.Decimal(sys.float_info.max)


def _text_bound(text: str) -> int | None:
    """The limit a stored bound string names, read exactly, or None.

    Plain digits are read by ``int()``. Any other text is read as a decimal, never
    through a float, which would read ``"1e-400"`` as 0 and ``"9007199254740993.0"``
    without its last digit. It names a limit only when it is finite, non-negative,
    whole and at most ``_MAX_TEXT_BOUND``.
    """
    text = text.strip()
    if text.isascii() and text.isdigit():
        try:
            return int(text)
        except ValueError:  # more digits than int() reads from text
            return None
    try:
        number = decimal.Decimal(text)
    except (decimal.DecimalException, ValueError):
        return None
    if not number.is_finite() or number < 0 or number > _MAX_TEXT_BOUND:
        return None
    whole = number.to_integral_value()
    return int(whole) if whole == number else None


def stored_bound(value: object) -> int | None:
    """The limit a STORED loop bound names, as an int >= 0, or None when it names none.

    A lossless integer form is the limit the user typed: a non-negative int, a
    whole finite float, or a string holding one exactly (``"24"``, ``" 24 "``,
    ``"24.0"``; see :func:`_text_bound`). Anything else -- ``null``, a negative or
    fractional number, ``nan``, a bool, other text, a container -- cannot be read
    as a limit, and reading it as 0 would quietly remove one, so it answers None.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return _text_bound(value)
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and math.isfinite(value) and value >= 0 and value.is_integer():
        return int(value)
    return None


def unreadable_bound_field(loop: "NudgeLoop") -> str | None:
    """The first bound of *loop* with no lossless integer reading, else None."""
    for name in LOOP_BOUND_FIELDS:
        if stored_bound(getattr(loop, name, 0)) is None:
            return name
    return None


def nudge_cycle_header(loop: "NudgeLoop", now: float | None = None) -> str:
    """The ``[auto-nudge cycle N]`` tag, plus a budget line when the loop has a cap.

    A patrol that cannot see its own budget cannot renew it in time: once the
    cap is spent the loop deactivates and the agent never gets another turn in
    which to call ``monitor_update``. So every capped cycle states what is left::

        [auto-nudge cycle 229]
        [patrol budget: cycle 229/240, 7560s/86400s runtime left; 10% or less left]

    ``10% or less left`` appears once either budget is at or under
    ``NUDGE_RENEW_DUE_SHARE`` of its cap; it is a fact, not an instruction.
    An uncapped loop gets the first line alone. ``N`` is ``cycle_count + 1``; the
    runtime figure is floored at 0 and omitted without a ``created_ts``, as in
    ``runtime_budget_exceeded``.
    """
    cycle = loop.cycle_count + 1
    tag = f"[auto-nudge cycle {cycle}]"
    # Read the caps defensively, as the gateway reads ``banner``: ``_load`` builds
    # a loop straight from parsed JSON, so a hand-edited store can carry any type
    # here. A value that is not a number means "no budget line", never a crash --
    # a raise would kill the fire, and the service re-arms an undelivered cycle.
    max_cycles = _positive_number(getattr(loop, "max_cycles", 0))
    max_runtime = _positive_number(getattr(loop, "max_runtime_secs", 0))
    created_ts = _positive_number(getattr(loop, "created_ts", 0))
    parts: list[str] = []
    due = False
    if max_cycles:
        parts.append(f"cycle {cycle}/{int(max_cycles)}")
        due = max(0, max_cycles - cycle) <= NUDGE_RENEW_DUE_SHARE * max_cycles
    if max_runtime and created_ts:
        elapsed = (now - created_ts) if now is not None else runtime_elapsed(loop, created_ts)
        left = max(0, int(max_runtime - elapsed))
        parts.append(f"{left}s/{int(max_runtime)}s runtime left")
        due = due or left <= NUDGE_RENEW_DUE_SHARE * max_runtime
    if not parts:
        return tag
    return f"{tag}\n[patrol budget: {', '.join(parts)}{'; 10% or less left' if due else ''}]"
