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

from kiro_crew.monitoring.models import MonitorOutcome, MonitorState, retained_outcome_blocks_rearm

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
#: declares the goal reached on a goal whose text carries ``{{STOP_FILE}}`` (the
#: apps' patrols, ``/goal``, a custom goal; the popover's default text has the
#: agent call ``autonudge_stop`` instead, and that call's removal of the row is
#: unchanged here), so the record is KEPT and deactivated under this reason rather
#: than removed -- a removed row left the goal popover on its empty form with
#: nothing saying the goal was met.
#: The stop file itself is left where it was written; the arm path unlinks it
#: before a new loop is armed on the slot (``authorize_and_add_nudge``).
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
# get a model session at all. A delivered cycle whose turn dies on
# ``session/new timed out`` costs a full turn's dispatch and produces nothing, so
# re-arming on the plain interval buys another identical failure: observed on an
# operator host as 15 consecutive auto-nudge cycles all ending in
# ``session/new timed out after 90s (0/10 MCP server(s) reported)``, stopped only
# by ``max_cycles`` running out. System-imposed like the other bounds -- the
# remedy (host pressure easing, a gate unstarving) is not something the loop can
# arrange -- so it is re-armable.
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
# FAILING -- turns that reached a model session and dispatched but died (a
# backend error after retries were spent, a persistent tool error, a prompt
# timeout), the turn outcome ``error`` or ``timeout``. The three narrower bounds
# each cover one deterministic sub-case: ``structural_terminal`` a malformed
# payload the backend rejects by shape, ``approval_stalled`` an unanswered
# approval, ``session_start_failures`` a cycle that never got a session at all.
# A cycle that got a session, dispatched, and then errored is none of those, so
# without this bound a loop firing every interval into a turn that always fails
# spends its whole cycle cap producing nothing -- the exact waste
# ``session_start_failures`` was built to end, for the broader class the three
# narrow bounds leave uncovered. System-imposed like them (the remedy -- the
# backend recovering, the tool being fixed -- is not something the loop can
# arrange), so it is re-armable, and evidence-driven: only a DELIVERED cycle of
# this loop's own that ended in a fault advances it, and a single landed turn on
# the slot clears it, so a loop that recovers is never held back.
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
# model-side content refusal that nothing retried. A refusal is deterministic for
# one model: the next cycle fires the same prompt into the same model and is
# declined again, so an unattended loop would spend every remaining cycle on
# refusal cards while the goal it drives waits for a person who is never told.
# The refusal fallback (``agent.refusal_fallback_model``) is attended-only, so for
# a loop's own cycle the refusal card is terminal. System-imposed like the
# structural stop and re-armable for the same reason: the remedy (a person
# rewording the goal, switching the model, or configuring a fallback) is not
# something the loop can arrange, and any genuine new turn in the session clears
# the slot's verdict so a corrected context is not stopped by a stale one.
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


class NudgeAdmissionRefused(RuntimeError):
    """The session authorized for an arm disappeared before its commit point."""


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
#: Everything else — a manual pause (``"manual"``), a
#: research tombstone (``AUTONUDGE_STOP_REASON``, consumed by the auto_research
#: watchdog to tell deliberate completion from crash cleanup), and any reason
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
# This mirrors ``messaging.link.CHANNEL_SESSION_NAMESPACES``, spelled out here
# rather than derived from it, for two independent reasons:
#
# 1. IMPORT WEIGHT. ``autonudge`` is imported at module scope by ``mcp_core``
#    (i.e. by every MCP server process) and by the dashboard chat layer, and it
#    depends only on config/security/platform_compat today. Naming
#    ``kiro_crew.messaging.link`` runs ``messaging/__init__``, which pulls the
#    driver/renderer/transport layer and, transitively, the ACP client, agent,
#    hooks, artifacts, metrics and sqlite — measured at 48 additional
#    ``kiro_crew`` modules to obtain one tuple of string literals.
# 2. THIS IS A KEY-SHAPE QUESTION, NOT A LIVE-CAPABILITY ONE. ``is_channel_key``
#    selects the RE-ARM STRATEGY and the expiry-notification metadata, so it has
#    to answer identically whether or not the transport happens to be registered
#    at this instant. Deriving it from a runtime ``supports_proactive_send``
#    lookup fails toward the WRONG branch: a loop whose transport is momentarily
#    absent would read as a dashboard slot, so ``_run_fire_cycle`` would stop
#    self-re-arming it — and nothing else ever will, since
#    ``notify_turn_complete`` never fires for a channel key — while the expiry
#    notice would synthesize a ``dashboard:<namespace>:<id>`` jump link pointing
#    at no slot.
#
# Membership therefore does NOT assert deliverability; it asserts "this key names
# a conversation rather than a chat slot". Whether a nudge can actually be
# delivered stays with the fail-closed ladder ``chat_runner._resolve_channel_target``
# (defined in ``dashboard/chat_turn/recipient.py``: governance, then a REGISTERED
# transport, then ``supports_proactive_send``), which logs its reason and degrades
# to a no-op.
# So a namespace is listed even when nothing can currently be delivered to it,
# and the two clearest cases are both here: ``whatsapp`` has no transport package
# in this fork at all, and ``feishu`` ships one that declares
# ``supports_proactive_send=False`` (its renderer only replies to an inbound
# message id, so a nudge cycle has nowhere to put the answer). Both still classify
# as channel keys, because the alternative is worse than a refusal: an unlisted key
# is read as a dashboard slot and silently stops being re-armed, whereas a listed
# one reaches the ladder and is refused with a logged reason. Being listed is
# likewise not an arming permission — that is ``binding_key_for``, which is
# narrower still and gated on an ownership check and a fire route.
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
    # Wall-clock budget in seconds, measured from ``created_ts`` (0 = unlimited).
    # A cycle cap alone cannot bound COST: a loop whose turns are slow or whose
    # idle gap is long can run for days within its cycle budget. Anchoring on
    # the persisted ``created_ts`` (not arm time) makes the budget restart-proof
    # — a gateway restart re-arms the loop but never resets its clock, and the
    # clock keeps running through a pause. The user's RESUME of a loop whose
    # TIME budget is spent does: a revival flagged ``fresh_run`` on a row that
    # stopped on ``runtime_budget``, or whose budget has elapsed by the press,
    # re-anchors ``created_ts`` (``_update_unserialized``); a spent CAP zeroes
    # ``cycle_count`` on its own, each bound resetting only its own counter. A
    # resume with that allowance left, a reconciler re-arm or a ``monitor_update``
    # bound raise keeps both.
    max_runtime_secs: int = 0
    #: Whether this loop may be observation-gated. Defaults to FALSE, which is what
    #: a record stored before this field existed decodes to.
    #:
    #: THE PRINCIPLE, stated once because it is easy to get backwards: gating is
    #: the state that can silently stop work -- a gated loop whose subject is merged
    #: or closed DEACTIVATES -- so every uncertainty resolves to UNGATED, and only an
    #: explicit boolean true gates. An absent key is a loop nobody chose to gate,
    #: usually a generic goal loop that predates the feature; a corrupt value is not
    #: a decision either. Being wrong in this direction costs a turn per interval,
    #: which is what today already costs. Being wrong the other way stops a
    #: recurring task because its instruction happened to mention a pull request.
    #:
    #: Persisted because the opt-out has to SURVIVE. The instruction is the target,
    #: so editing it re-infers the subject; without a remembered decision an
    #: explicitly ungated loop would be silently re-gated by the next wording
    #: change -- exactly the harm the opt-out exists to prevent, arriving through
    #: the documented way to revise a loop.
    gate: bool = False
    #: The owner's judge brief: ``targets``, ``wake_when``, ``quiet_when``. Empty
    #: means the owner named no criteria of their own, NOT that no judge applies: a
    #: gated loop is then screened under
    #: :func:`~kiro_crew.autonudge_judge.default_spec`. The explicit bypass is
    #: ``judge: false``, which normalises to the reserved
    #: :data:`~kiro_crew.validation.JUDGE_OFF_KEY` marker stored here, and an ungated
    #: loop is never screened at all.
    #:
    #: The default is never written back, so this field records what the OWNER asked
    #: for: granting them a criterion later stays an empty-to-set change rather than an
    #: edit of shipped text, and the mid-tick replacement guard compares a re-read
    #: against what was stored rather than against the merged form a tick ran under.
    #:
    #: Stored even while the consent scope is off, deliberately: an armed loop has
    #: to survive the switch being turned on later, and re-arming every watch after
    #: a consent change would be a worse answer than storing a brief nobody reads
    #: yet. Nothing is COLLECTED or sent until ``nudge_evidence`` is granted.
    judge: dict = field(default_factory=dict)
    #: Per-target read cursor, ``target -> next_since``, so each tick reads only the
    #: rows that arrived since the last one. Advanced only on a successful read, so
    #: a refused or failing target does not silently skip its own rows.
    judge_cursors: dict = field(default_factory=dict)
    #: What the pull-request reading looked like the last time this loop reached a
    #: verdict: ``{"digest": str, "remarks": [id, ...]}``, and nothing larger, because
    #: those are the two things a later tick asks of it -- whether the subject moved,
    #: and which remarks it had already been shown.
    #:
    #: Its OWN field rather than ``MonitorState.last_observation``, and advanced with
    #: the verdict rather than with the reading, for the reason ``judge_cursors`` is:
    #: the reading has to be published BEFORE the judge is asked, because that record
    #: is the channel the judge reads it through, and the ask is an await that ordinary
    #: user input cancels. A baseline advanced there would mark a new comment seen on a
    #: tick that reached no verdict, and the next tick would read it as old -- with the
    #: digest matching too, so both the delta and the unchanged check would suppress a
    #: wake nobody ever judged. A tick that did not reach a verdict consumes nothing.
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
    #:
    #: On the LOOP rather than on ``MonitorState``, which is where the probe path keeps
    #: the same doubt as ``poll_in_flight``: a judge-only loop -- a conductor watching
    #: sibling sessions -- has no monitor record at all, so every mechanism guarded by
    #: ``monitor is not None`` misses exactly the loops the judge exists for. That is
    #: also why a REFUSED fire is covered here and not by ``followup_ticks``.
    #:
    #: A lost clear costs one extra fire, which is the safe direction: the judge
    #: advanced durable read cursors before deciding, so without this the next tick
    #: reads nothing new, answers quiet, and the turn is gone until the streak floor.
    judge_wake_pending: bool = False
    #: The previous verdict, summarised and text-free, carried into the next tick's
    #: state so a judge can see it already passed on comparable evidence once.
    judge_last_verdict: dict = field(default_factory=dict)
    #: The last few verdicts with the label their delivery earned, oldest first. This
    #: is the judge's own hit rate ON THIS LOOP, which is the one calibration reading
    #: no global curve can give it: a conductor patrolling workers and a loop watching
    #: one pull request have different subjects, so what counts as a wake worth
    #: spending differs per loop and only the loop's own history measures it.
    #:
    #: Bounded to :data:`~kiro_crew.autonudge_judge.MAX_STORED_VERDICTS`, which is
    #: sized from the QUIET-STREAK FLOOR rather than from the window the judge reads:
    #: the retroactive pass needs the delivery that closes a full streak still in the
    #: store when it labels the suppressions that opened it.
    judge_recent_verdicts: list = field(default_factory=list)
    # WHY the loop was last deactivated: "" (active / never stopped),
    # "manual" (user pause / any caller that didn't say otherwise),
    # "autonudge_stop" (deliberate directive), "cycle_cap",
    # "runtime_budget", or "approval_stalled" (set by _timer's terminal
    # bounds), "stop_sentinel" (the stop file existed when _timer woke) or
    # "monitor_terminal" (the watched subject merged or closed). "approval_stalled"
    # is from before approval stalls became a hold; it is still read on old rows.
    # Persisted so revival logic can distinguish a manual pause from a bound
    # expiry — elapsed wall-clock keeps growing after a manual pause, so
    # WITHOUT this record a paused loop whose budget has since elapsed is
    # indistinguishable from a budget-stopped one, and a budget raise would
    # resume unattended execution against the user's explicit pause.
    stopped_reason: str = ""
    # Evidence that a cycle in this loop's session asked for tool approval and
    # nobody answered within the window. Set by ``notify_approval_stalled`` and
    # read by ``_timer`` as a HOLD on every later wake: the loop stays active but
    # fires no cycle, so it spends neither its cycle cap nor (see
    # ``approval_stalled_at``) its runtime budget while nobody is there to answer.
    # It holds on proof that it could not act, never on a prediction that it
    # might not be able to. A loop whose turns only touch auto-approved tools
    # never reaches an interactive wait, so it can never be flagged here.
    # Cleared by ``release_approval_hold`` once a person is back (an approval
    # answered in the slot, a message typed into it, a manual fire), which
    # resumes the loop with no re-arm by the user, and on every revival.
    # Persisted, because the condition that produced it usually outlives a
    # restart.
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
    # session (``session/new`` timed out or otherwise failed). Raised by
    # ``notify_cycle_start_failed`` and zeroed by ``notify_cycle_landed``, both
    # driven by evidence from the slot's own turns rather than by a prediction.
    # Consumed by ``_timer``: past ``_START_FAILURE_BACKOFF_AFTER`` the wake is
    # deferred, past ``_START_FAILURE_STANDDOWN_AFTER`` the loop stops with
    # ``SESSION_START_FAILURE_REASON``. Persisted, because the host condition
    # that produces it routinely outlives a restart, and cleared on every revival
    # so a recovered loop is not stood down by stale evidence.
    consecutive_start_failures: int = 0
    # How many of this loop's OWN delivered cycles in a row ended in a fault --
    # a turn that reached a model session and dispatched but died (turn outcome
    # ``error`` or ``timeout``). Raised by ``notify_cycle_failed`` and zeroed by
    # ``notify_cycle_landed`` (any landed turn on the slot proves progress is
    # possible), both driven by evidence from the slot's own turns. Consumed by
    # ``_timer``: past ``_CONSECUTIVE_FAILURE_STANDDOWN_AFTER`` the loop stops
    # with ``CONSECUTIVE_FAILURE_REASON``. Distinct from
    # ``consecutive_start_failures`` because the two measure different failures
    # with different remedies -- a cycle that never got a session versus one that
    # ran and errored -- and a loop can hit either. Persisted, because the
    # condition that produces it (a wedged backend, a broken tool) routinely
    # outlives a restart, and cleared on every revival so a recovered loop is not
    # stood down by stale evidence.
    consecutive_failed_cycles: int = 0
    # Absolute wall-clock deadline for the next fire (0 = unset: the next arm
    # starts a fresh full countdown). This is what makes the countdown
    # deadline-preserving — user turns cancel the pending timer TASK but never
    # touch this field, so the schedule survives an active conversation.
    # Cleared on every delivered fire (the next cycle is measured from the
    # nudge turn's END, whose timestamp is only known at notify_turn_complete).
    # Every assignment is persisted: add/update/fire bookkeeping write it
    # inline, and turn-lifecycle arms schedule a supervised background write,
    # so a restart resumes the countdown. A lost background write degrades to
    # a fresh full countdown after restart, never a lost or premature fire.
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
    # transcript row. Empty (the default) means the row is byte-identical to
    # what it has always been, so no existing loop changes behaviour.
    #
    # ``message`` serves two consumers with opposite needs. The model needs the
    # whole instruction re-delivered every cycle — that is the guarantee the
    # nudge exists to provide. A person reading the transcript needs only "a
    # nudge happened", yet gets the same multi-KB payload appended per cycle:
    # measured on one long-running loop, 44 nudge rows of ~7.9KB were 51.8% of
    # the entire 671,900-char session file.
    #
    # The PROMPT is never affected by this field (see
    # ``GatewayOrchestrator._fire_dashboard_nudge``): shortening the model's
    # copy would delete real instruction, which is the opposite of the point.
    # Scoped to the dashboard transcript row — channel-bound loops
    # (``slack:``/``discord:``/``webex:``) deliver the nudge as the turn's own
    # input and have no separate display surface to shorten.
    #
    # Appended LAST rather than placed beside ``message`` so a persisted store
    # written by this version still loads on a build that predates the field:
    # ``_load`` filters unknown keys, so a downgrade degrades to the verbose
    # display instead of raising.
    banner: str = ""
    # WHO armed this loop, reduced to the one distinction the authorizer needs:
    # True when the arming request came from a turn OF THE BOUND SESSION ITSELF
    # (the ``monitor_start`` / ``monitor_watch`` session directive, applied by
    # the turn loop to the exact session that produced it), False for every
    # external arm (REST, workflow ``ctx.nudge``, an app handler).
    #
    # Exists because crew- and member-mode slots refuse automation turns armed
    # from OUTSIDE the session -- nothing may inject work into a member's own
    # thread -- yet a member is by definition a self-directed resident agent,
    # and refusing its own ``monitor_start`` left the conductor member thread
    # never waking again (the very loop it exists to run). The authorizer
    # admits the self-arm and records it here so the FIRE-time re-check in
    # ``GatewayOrchestrator._fire_dashboard_nudge`` can tell "this slot was a
    # member when its own turn armed the loop" from "this slot switched into
    # crew mode after an outsider armed it", which is the case that guard
    # exists to stop. Persisted for the same reason ``gate`` is: a restart
    # re-arms every loop, and a self-armed member loop that lost this bit
    # would be refused at its first post-restart wake. Absent in a store
    # written before the field existed decodes to False -- every such loop
    # was armed under the old rule, which admitted no self-arm.
    self_armed: bool = False
    # Monotonic per-loop CONFIG generation. Advanced by ``_update_unserialized``
    # ONLY on a real configuration change (a changed ``message``) or a revival
    # (inactive -> active), never by internal timer/cycle bookkeeping. Captured
    # at fire time and compared atomically (under the service ``_lock``) before a
    # structural-terminal stop is applied, so a stale completion of an OLD
    # instruction cannot deactivate a loop whose instruction was re-committed
    # since (the A->B->A race that value-identity on ``message`` alone cannot
    # tell apart). Reuses the module's generation/fence pattern rather than a new
    # concurrency framework. Absent in a store written before this field ->
    # decodes to 0, and a first fire simply captures 0.
    config_generation: int = 0
    # The gateway's default conductor patrol (``conductor_patrol.ensure_patrol``,
    # armed when a conductor binds a worker and holds no loop). The one thing it
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


def runtime_budget_exceeded(loop: "NudgeLoop", now: float | None = None) -> bool:
    """True when *loop* has a wall-clock budget and it is spent.

    Single source of truth shared by ``_timer`` (enforcement) and the expiry
    notifier (wording), so the two can never disagree on WHY a loop stopped.
    A loop with no ``created_ts`` (a malformed/legacy store entry) never
    trips the budget — there is no anchor to measure from, and guessing one
    could kill a healthy loop on its first cycle after an upgrade.
    """
    if not loop.max_runtime_secs or not loop.created_ts:
        return False
    return (now if now is not None else time.time()) - loop.created_ts >= loop.max_runtime_secs


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
    """True when *loop* has a wall-clock budget and the clock has run it out.

    ``runtime_budget_exceeded`` asked at resume time, with the same defensive
    reads as :func:`cap_reached` and for the same reason. The clock keeps running
    through a pause, so a loop paused with an hour of budget left and resumed the
    next day reads as spent here exactly as the timer would read it.
    """
    budget = _positive_number(loop.max_runtime_secs)
    anchor = _positive_number(loop.created_ts)
    if not budget or not anchor:
        return False
    return (now if now is not None else time.time()) - anchor >= budget


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
    ``NUDGE_RENEW_DUE_SHARE`` of its cap. It is a fact, not an instruction:
    every capped loop gets it, and only an agent whose own instructions say so
    acts on it (goal-conductor renews on those cycles).

    The first line is unchanged, so every reader of the tag still matches. An
    uncapped loop gets the first line alone, byte-identical to before.
    ``N`` is the cycle being delivered (``cycle_count + 1``); the runtime figure
    is floored at 0 and omitted when there is no ``created_ts`` to measure from,
    the same rule ``runtime_budget_exceeded`` applies.
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
        elapsed = (now if now is not None else time.time()) - created_ts
        left = max(0, int(max_runtime - elapsed))
        parts.append(f"{left}s/{int(max_runtime)}s runtime left")
        due = due or left <= NUDGE_RENEW_DUE_SHARE * max_runtime
    if not parts:
        return tag
    return f"{tag}\n[patrol budget: {', '.join(parts)}{'; 10% or less left' if due else ''}]"
