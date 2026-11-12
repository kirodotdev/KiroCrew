"""Transport-independent monitor state.

The scheduler, provider adapters, and session delivery code exchange these
small records. They deliberately contain no provider clients or callbacks, so
they can be persisted and evaluated without starting an agent turn.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any, Protocol

from kiro_crew.monitoring.limits import MAX_RUNTIME_CEILING_SECS
from kiro_crew.monitoring.registry import (
    all_pull_request_monitor_kinds,
    is_pull_request_monitor_kind,
)

logger = logging.getLogger(__name__)

MONITOR_STATE_VERSION = 1
DEFAULT_MONITOR_RUNTIME_SECS = 14_400
#: Default wake ceiling. Zero is the unlimited sentinel: wakes measure how often
#: the subject needs its owner, not cost, so the runtime, token and provider-error
#: budgets are what keep a default watch affordable.
DEFAULT_MONITOR_AGENT_TURNS = 0
DEFAULT_MONITOR_TOKENS = 250_000
DEFAULT_MONITOR_PROVIDER_ERRORS = 3
DEFAULT_MONITOR_CADENCE_SECS = 300
MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET = 100
MAX_MONITOR_CHECK_IDENTITY_CHARS = 200
MONITOR_STOP_INVALID_RECORD = "invalid_monitor_record"
MIN_MONITOR_CADENCE_SECS = 15
MAX_MONITOR_CADENCE_SECS = 86_400
MAX_MONITOR_RUNTIME_SECS = MAX_RUNTIME_CEILING_SECS
#: Ceiling on an EXPLICIT positive wake budget; zero (unlimited) is not bound by
#: it. Matches the prompt loop's ``max_cycles`` bound so both cycle caps agree.
MAX_MONITOR_AGENT_TURNS = 1_000
MAX_MONITOR_TOKENS = 1_000_000
MAX_MONITOR_PROVIDER_ERRORS = 20
MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS = 1_000
MAX_MONITOR_STOP_REASON_CHARS = 500
MAX_MONITOR_CHECK_NAMES = 8
MAX_MONITOR_PROVIDER_CONCURRENCY = 4
# The normal turn ceiling is two hours. One extra minute lets the raw completion
# callback win the timeout race while keeping missing evidence restart-durable
# and bounded.
MONITOR_COMPLETION_EVIDENCE_TIMEOUT_SECS = 7_260
MONITOR_BUSY_RETRY_SECS = 15
#: Shortest a coalescing window stays open before an actionable change may wake
#: the session. A floor, not a timeout: a subject whose checks are still landing
#: can look settled for a moment, and firing on that reports a convergence that
#: did not happen. Successive changes to the one subject age from when the window
#: opened, so a burst of edits costs one wake rather than one per change.
DEFAULT_MONITOR_COALESCE_SECS = 240.0
#: A delivered wake re-arms after this long while the same actionable
#: fingerprint persists. Level-triggered re-assertion: an unresolved condition
#: is re-reported on this interval rather than once, and a future timestamp
#: (clock rollback) reads as stale so it can never suppress a wake forever.
DEFAULT_MONITOR_REALERT_SECS = 6 * 3600
#: How many consecutive COUNTED ticks may carry a byte-identical verdict before
#: the watch is retired as stuck.
#:
#: It must exceed the floor's tick equivalent at the default cadence
#: (``DEFAULT_MONITOR_STALL_MIN_SECS // DEFAULT_MONITOR_CADENCE_SECS`` = 6), or
#: the count never binds, and stay inside the ticks a default watch gets before
#: its runtime budget ends (``DEFAULT_MONITOR_RUNTIME_SECS //
#: DEFAULT_MONITOR_CADENCE_SECS`` = 48), or the stall can never fire. 6 < 12 < 48.
DEFAULT_MONITOR_STALL_TICKS = 12
#: Least wall-clock a stall streak must cover before it may retire a watch.
#:
#: Cadence is user-set from 15s to 86400s, so a tick count alone is the wrong
#: unit: twelve ticks at 15s is 165s, and an agent may still be working. The trip
#: needs BOTH the count and this floor.
#:
#: It must exceed ``DEFAULT_MONITOR_COALESCE_SECS`` widely, or a burst being
#: folded looks like a stall, and stay well under ``DEFAULT_MONITOR_REALERT_SECS``,
#: because a re-alert zeroes the streak and a longer floor could never be reached.
#: 240 << 1800 << 21600.
DEFAULT_MONITOR_STALL_MIN_SECS = 1800
MONITOR_STOP_RUNTIME_BUDGET = "runtime_budget"
MONITOR_STOP_AGENT_TURN_BUDGET = "agent_turn_budget"
MONITOR_STOP_TOKEN_BUDGET = "token_budget"
MONITOR_STOP_PROVIDER_ERROR_BUDGET = "provider_error_budget"
MONITOR_STOP_APPROVAL_STALL = "approval_stall"
#: A watch retired because its own verdict stopped moving. Kept distinct from
#: ``approval_stall`` (tool approval failed) and the ``*_budget`` reasons (a bound
#: was spent) because each has a different remedy.
MONITOR_STOP_VERDICT_STALL = "verdict_stall"
MONITOR_STOP_COMPLETION_UNAVAILABLE = "completion_evidence_unavailable"
MONITOR_STOP_UNSUPPORTED_VERSION = "unsupported_monitor_version"
MONITOR_STOP_USER = "user_stop"
MONITOR_STOP_SESSION_UNAVAILABLE = "session_unavailable"
MONITOR_STOP_SESSION_CLOSE = "session_close"
PULL_REQUEST_OBSERVATION_FIELDS = (
    "blocking_review",
    "checks",
    "checks_complete",
    "draft",
    "head_revision",
    "kind",
    "mergeability",
    "review_decision",
    "review_threads_complete",
    "state",
    "target",
    "unresolved_review_threads",
)
PULL_REQUEST_CHECK_FIELDS = ("failed", "passed", "pending", "unknown")
#: The one OPTIONAL check bucket: rows displaced by a newer run of the same
#: identity. They carry no verdict, so readiness leaves them out of both actionable
#: and pending and the overflow marker does not count them. Written only when
#: non-empty, so other subjects keep the shared canonical shape the fingerprint
#: hashes.
PULL_REQUEST_SUPERSEDED_CHECK_FIELD = "superseded"
#: Replaces the last entry when the displaced bucket is cut. Spent inside the
#: bucket, not on ``checks_complete``, since displaced rows carry no verdict. The
#: projection writes it and the compact inspection reads it, so both share this name.
PULL_REQUEST_SUPERSEDED_INCOMPLETE_IDENTITY = f"{PULL_REQUEST_SUPERSEDED_CHECK_FIELD}:incomplete"
PULL_REQUEST_BLOCKING_REVIEWS = {
    "unknown",
    "changes_requested",
    "unresolved_threads",
    "none",
}
PULL_REQUEST_MERGEABILITY = {"conflicting", "behind", "blocked", "pending", "mergeable"}
PULL_REQUEST_REVIEW_DECISIONS = {
    "none",
    "approved",
    "changes_requested",
    "review_required",
    "unknown",
}
PULL_REQUEST_STATES = {"open", "closed", "merged", "unknown"}
MONITOR_PUBLIC_FIELDS = (
    "version",
    "config_generation",
    "kind",
    "target",
    "objective",
    "created_ts",
    "budgets",
    "cadence_secs",
    "wake_instructions",
    "wake_on_terminal",
    "last_observation",
    "last_observation_status",
    "last_observation_reason_code",
    "last_fingerprint",
    "last_observed_at",
    "last_wake_fingerprint",
    "last_wake_reason_code",
    "wake_in_flight",
    "wake_delivery",
    "wake_count",
    "completion_evidence_deadline",
    "last_completion_fingerprint",
    "last_completion_disposition",
    "last_completed_at",
    "token_usage_known",
    "agent_turns",
    "input_tokens",
    "output_tokens",
    "probe_count",
    "provider_error_count",
    "consecutive_provider_errors",
    "last_probe_at",
    "last_decision",
    "last_provider_error",
    "next_probe_at",
    "outcome",
    "stopped_reason",
    "user_stop_reason",
    "stopped_at",
)


def is_finite_non_negative_number(value: object) -> bool:
    """Whether *value* is a real, finite, non-negative timestamp-shaped number.

    ``bool`` is excluded even though it is an ``int`` subclass, and an ``int``
    too large to convert to a float (``10**400``) is rejected rather than
    letting ``math.isfinite``'s ``OverflowError`` escape to the caller.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


class MonitorDecision(str, Enum):
    """Effect the monitor controller applies after a probe."""

    NO_CHANGE = "no_change"
    RECORD_ONLY = "record_only"
    WAKE_ACTIONABLE = "wake_actionable"
    STOP_SUCCESS = "stop_success"
    STOP_BLOCKED = "stop_blocked"
    RETRY_PROVIDER = "retry_provider"
    STOP_BUDGET = "stop_budget"


class MonitorObservationStatus(str, Enum):
    """Domain-owned classification of one canonical observation."""

    PENDING = "pending"
    ACTIONABLE = "actionable"
    SUCCESS = "success"
    BLOCKED = "blocked"
    PROVIDER_ERROR = "provider_error"


class ProviderErrorKind(str, Enum):
    """Provider failures that have different retry safety."""

    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    NOT_FOUND = "not_found"
    SETUP = "setup"


class MonitorOutcome(str, Enum):
    """Durable terminal result retained after the monitor stops."""

    SUCCESS = "success"
    BLOCKED = "blocked"
    BUDGET = "budget"
    USER_STOP = "user_stop"
    SESSION_CLOSE = "session_close"
    TARGET_UNAVAILABLE = "target_unavailable"


#: Terminal outcomes a directive re-arm may displace, because the SYSTEM imposed
#: them: a spent bound, a finished subject, a lapsed approval, a vanished target.
#: Everything else -- ``USER_STOP``, ``SESSION_CLOSE``, and any outcome a later
#: version adds -- was recorded FOR a consumer and is retained evidence.
#:
#: Single source of truth: ``autonudge._stopped_row_is_replaceable`` and the
#: ``mcp_tools.control`` preflight both read it, so they cannot disagree.
REARMABLE_MONITOR_OUTCOMES = frozenset(
    {
        MonitorOutcome.SUCCESS,
        MonitorOutcome.BLOCKED,
        MonitorOutcome.BUDGET,
        MonitorOutcome.TARGET_UNAVAILABLE,
    }
)


def retained_outcome_blocks_rearm(outcome: object, stopped_reason: object = "") -> bool:
    """Whether a recorded *outcome* is evidence a re-arm must not displace.

    Accepts the enum or its serialized value, so one predicate serves both the
    in-process record and the endpoint reading of it. Fails CLOSED: an outcome
    this version does not recognise is treated as evidence, matching the ruling
    that only a system-imposed stop is automatically re-armable.

    ``None`` means no terminal outcome was recorded, which blocks nothing.
    """
    if outcome is None or outcome == "":
        return False
    try:
        resolved = MonitorOutcome(outcome)
    except ValueError:
        # An unknown outcome is evidence, not a system stop.
        return True
    if resolved is MonitorOutcome.BLOCKED and str(stopped_reason or "") == (
        MONITOR_STOP_INVALID_RECORD
    ):
        # A quarantined malformed record is an inspection artifact retained for a
        # human, not a stop the system chose; the arm path refuses it too.
        return True
    return resolved not in REARMABLE_MONITOR_OUTCOMES


class MonitorActionDisposition(str, Enum):
    """Terminal disposition reported by a started monitor action turn."""

    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLATION = "cancellation"
    APPROVAL_STALL = "approval_stall"


class MonitorDispatchResult(str, Enum):
    """Typed result of handing one claimed wake to its owning session."""

    DISPATCHED = "dispatched"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"


class MonitorCreationSurface(str, Enum):
    """Authenticated surface that armed a durable monitor."""

    UNKNOWN = "unknown"
    DASHBOARD = "dashboard"
    CHANNEL = "channel"


def monitor_frontend_contract() -> dict[str, object]:
    """Return the monitor data contract the dashboard consumes.

    The bundle ships a checked-in snapshot of this (``website/src/monitoring/
    contract.json``); the gateway also serves it at runtime (``GET
    /api/monitor-contract``) so ``pullRequestMonitorKinds`` can name a kind an
    edition contributed at boot, which the snapshot cannot. With no
    contributed kinds the two agree -- the snapshot parity test pins it.
    """
    return {
        "monitorStateVersion": MONITOR_STATE_VERSION,
        "limits": {
            "cadenceSecs": {
                "minimum": MIN_MONITOR_CADENCE_SECS,
                "maximum": MAX_MONITOR_CADENCE_SECS,
                "defaultValue": DEFAULT_MONITOR_CADENCE_SECS,
            },
            "maxRuntimeSecs": {
                "minimum": 1,
                "maximum": MAX_MONITOR_RUNTIME_SECS,
                "defaultValue": DEFAULT_MONITOR_RUNTIME_SECS,
            },
            # Zero is admitted as the unlimited sentinel, which is why this one
            # field has a floor of 0 where its three siblings have 1.
            "maxAgentTurns": {
                "minimum": 0,
                "maximum": MAX_MONITOR_AGENT_TURNS,
                "defaultValue": DEFAULT_MONITOR_AGENT_TURNS,
            },
            "maxTokens": {
                "minimum": 1,
                "maximum": MAX_MONITOR_TOKENS,
                "defaultValue": DEFAULT_MONITOR_TOKENS,
            },
            "maxProviderErrors": {
                "minimum": 1,
                "maximum": MAX_MONITOR_PROVIDER_ERRORS,
                "defaultValue": DEFAULT_MONITOR_PROVIDER_ERRORS,
            },
            "wakeInstructions": {"maximumLength": MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS},
        },
        # The LIVE pull-request kinds -- built-ins plus any an edition contributed
        # at boot, so the runtime contract the gateway serves names a
        # contributed kind. The bundled snapshot still lists only the built-ins;
        # with no plugins registered the two agree, which the parity test pins.
        "pullRequestMonitorKinds": sorted(all_pull_request_monitor_kinds()),
        "enums": {
            "wakeDelivery": [item.value for item in MonitorDispatchResult],
            "lastCompletionDisposition": [item.value for item in MonitorActionDisposition],
            "lastDecision": [item.value for item in MonitorDecision],
            "lastProviderError": [item.value for item in ProviderErrorKind],
            "lastObservationStatus": [item.value for item in MonitorObservationStatus],
            "outcome": [item.value for item in MonitorOutcome],
        },
    }


@dataclass(frozen=True)
class MonitorActionCompletion:
    """Authoritative evidence that one monitor action turn stopped running."""

    monitor_id: str
    fingerprint: str
    disposition: MonitorActionDisposition
    completed_ts: float
    input_tokens: int | None = None
    output_tokens: int | None = None

    def __post_init__(self) -> None:
        for name in ("monitor_id", "fingerprint"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.disposition, MonitorActionDisposition):
            raise ValueError("disposition must be a MonitorActionDisposition")
        if not is_finite_non_negative_number(self.completed_ts):
            raise ValueError("completed_ts must be a finite non-negative number")
        for name in ("input_tokens", "output_tokens"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or None")


@dataclass(frozen=True)
class MonitorBudgets:
    """Hard bounds for a structured monitor.

    ``max_agent_turns`` is the one field where zero means unlimited, matching the
    legacy prompt loop's ``max_cycles``. The other three reject it: a watch with
    no runtime, token or provider-error bound is a watch with no cost ceiling at
    all, which is what the unlimited wake count relies on to stay affordable.
    """

    max_runtime_secs: int = DEFAULT_MONITOR_RUNTIME_SECS
    max_agent_turns: int = DEFAULT_MONITOR_AGENT_TURNS
    max_tokens: int = DEFAULT_MONITOR_TOKENS
    max_provider_errors: int = DEFAULT_MONITOR_PROVIDER_ERRORS

    def __post_init__(self) -> None:
        for name in (
            "max_runtime_secs",
            "max_agent_turns",
            "max_tokens",
            "max_provider_errors",
        ):
            value = getattr(self, name)
            floor = 0 if name == "max_agent_turns" else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < floor:
                requirement = (
                    "a non-negative integer, where zero means unlimited"
                    if floor == 0
                    else "a positive integer"
                )
                raise ValueError(f"{name} must be {requirement}")
        if self.max_runtime_secs > MAX_MONITOR_RUNTIME_SECS:
            raise ValueError(f"max_runtime_secs must be at most {MAX_MONITOR_RUNTIME_SECS}")
        if self.max_agent_turns > MAX_MONITOR_AGENT_TURNS:
            raise ValueError(f"max_agent_turns must be at most {MAX_MONITOR_AGENT_TURNS}")


class MonitorSeverity(str, Enum):
    """How the decision engine treats one named condition.

    Mirrors the cron kernel's ``irq.Severity``; ``test_monitor_conditions.py``
    pins the two member-for-member so they cannot drift.
    """

    #: An anomaly. Masked per condition, and folded into a coalesced wake.
    WAKE = "wake"
    #: The subject reached an end state. Reserved: the structured engine takes
    #: terminality from ``MonitorObservationStatus``, so nothing here reads it; it
    #: exists so the parity pin covers the whole vocabulary.
    TERMINAL = "terminal"
    #: BYPASSES the coalescing floor and fires now, for a condition where waiting
    #: observes nothing more (a conflicted pull request dispatches no checks).
    #: It bypasses the DELAY, not the MASK: a persisting condition still wakes at
    #: most once per re-alert interval.
    IMMEDIATE = "immediate"


class MonitorResetsOn(str, Enum):
    """What clears a condition, and so how long its dedupe memory is worth."""

    #: A new revision of the subject clears it (check rollups: a new head makes
    #: the condition describe something that is gone).
    REVISION = "revision"
    #: No revision clears it: it belongs to the subject. A review comment belongs
    #: to the conversation, so a force-push must not replay every comment seen.
    NEVER = "never"


#: Every dedupe key the engine stores carries exactly one of these, so a head
#: change can drop the revision-scoped half without asking a probe what any
#: stored key meant. Prefixing is unconditional: a scheme that prefixed only the
#: sticky half could be spoofed by a condition key that happened to start with
#: the sentinel, and condition keys are attacker-influenceable (a CI workflow
#: names its own jobs).
#:
#: The reset needs this rather than the conditions in hand, because a
#: revision-scoped condition can go unreported for a tick and come back: scoped
#: to only the keys reported ON the head-change tick, its memory would survive
#: the reset and mask a real wake for the rest of the re-alert interval.
#:
#: These two characters are the PERSISTED encoding of the distinction, which is
#: why they are named for the two key spaces rather than for the field a probe
#: sets.
MONITOR_REVISION_KEY_SPACE = "="
MONITOR_STICKY_KEY_SPACE = "~"
_MONITOR_KEY_SPACES = (MONITOR_REVISION_KEY_SPACE, MONITOR_STICKY_KEY_SPACE)

#: State keys this version retired. Dropped on load rather than carried in
#: ``extra_fields``, which is reserved for fields a NEWER version owns: kept
#: there they would be re-serialized on every write forever, and a reader would
#: have two spellings of the coalescing window to choose between.
_RETIRED_MONITOR_STATE_FIELDS = frozenset({"coalesce_fingerprint", "coalesce_opened_at"})

MAX_MONITOR_CONDITION_KEY_CHARS = 200

#: How many conditions a subject may carry.
#:
#: A cap smaller than what a bounded probe can legitimately name silently drops
#: real blockers, so it is the per-bucket check bound plus the fixed keys an
#: adapter adds: review verdict, unresolved-thread count, one mergeability
#: condition and the PR-level-comment-body digest. Four can co-occur (conflict and
#: behind are exclusive), plus one key of margin. Derived so neither half can drift.
MAX_MONITOR_FIXED_CONDITIONS = 5
MAX_MONITOR_CONDITIONS = MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET + MAX_MONITOR_FIXED_CONDITIONS


@dataclass(frozen=True)
class MonitorCondition:
    """One named thing a probe saw about one subject during one tick.

    This is layer 3's named entry in engine terms. A probe reports only what it
    wants the engine to act on: there is deliberately no "seen and fine"
    severity, because a condition the engine would neither wake nor fold is
    simply not returned.

    ``key`` is a semantic string, stable across ticks, never a hash --
    ``conflict``, ``red:<check>``, ``unresolved_threads``. A hash cannot be
    deduplicated per condition, cannot be folded with a sibling and cannot be
    re-asserted, because nothing can tell whether two hashes describe the same
    condition.

    ``brief`` stays ahead of ``resets_on`` positionally to match the cron
    kernel's ``Observation``, so the two types read the same way round.
    """

    key: str
    severity: MonitorSeverity = MonitorSeverity.WAKE
    brief: str = ""
    resets_on: MonitorResetsOn = MonitorResetsOn.REVISION

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("condition key must be a non-empty string")
        if len(self.key) > MAX_MONITOR_CONDITION_KEY_CHARS:
            raise ValueError("condition key is too long")
        if self.key.startswith(_MONITOR_KEY_SPACES):
            # A probe never writes a key space itself. Refusing here is what
            # keeps the two spaces impossible to confuse from outside.
            raise ValueError("condition key must not begin with a reserved key space")
        if not isinstance(self.severity, MonitorSeverity):
            raise ValueError("condition severity must be a MonitorSeverity")
        if not isinstance(self.resets_on, MonitorResetsOn):
            raise ValueError("condition resets_on must be a MonitorResetsOn")
        if not isinstance(self.brief, str):
            raise ValueError("condition brief must be a string")


def monitor_condition_dedupe_key(condition: MonitorCondition) -> str:
    """The key *condition* is remembered under, key space included.

    THE only place ``resets_on`` is read. Everything downstream asks the KEY
    which space it is in, so the two spellings never have to agree twice.
    """
    space = (
        MONITOR_REVISION_KEY_SPACE
        if condition.resets_on is MonitorResetsOn.REVISION
        else MONITOR_STICKY_KEY_SPACE
    )
    return space + condition.key


def monitor_dedupe_key_resets_on_revision(key: str) -> bool:
    """Whether a stored dedupe key lives in the revision space."""
    return not key.startswith(MONITOR_STICKY_KEY_SPACE)


def adopt_monitor_dedupe_key(key: str) -> str:
    """Adopt a dedupe key persisted before the key spaces existed.

    Records written by an earlier version key the re-alert map by whole-subject
    fingerprint, with no space. Read as-is those keys could never match one this
    version computes, so every armed watch would wake once more for a condition
    it had already reported. A bare key is adopted into the revision space, which
    is what every pre-space key was: the sticky space did not exist.
    """
    return key if key.startswith(_MONITOR_KEY_SPACES) else MONITOR_REVISION_KEY_SPACE + key


@dataclass(frozen=True)
class MonitorObservation:
    """Small canonical result produced by a typed provider probe.

    Subject level. ``conditions`` is the per-condition level beneath it: the
    named entries this tick found, each with its own dedupe identity, urgency
    claim and reset scope. A probe that reports none is a legal shape -- the
    engine reads the subject as one revision-scoped condition keyed by its
    fingerprint, which is exactly what a single-fingerprint subject was before
    conditions existed.
    """

    fingerprint: str
    status: MonitorObservationStatus
    provider_error: ProviderErrorKind | None = None
    supplemental_provider_error: ProviderErrorKind | None = None
    reason_code: str = ""
    summary: str = ""
    head_changed: bool = False
    conditions: tuple[MonitorCondition, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.status, MonitorObservationStatus):
            raise ValueError("status must be a MonitorObservationStatus")
        if not isinstance(self.fingerprint, str):
            raise ValueError("fingerprint must be a string")
        if not isinstance(self.conditions, tuple) or any(
            not isinstance(item, MonitorCondition) for item in self.conditions
        ):
            raise ValueError("conditions must be a tuple of MonitorCondition")
        if len(self.conditions) > MAX_MONITOR_CONDITIONS:
            raise ValueError("too many conditions for one observation")
        keys = [item.key for item in self.conditions]
        if len(set(keys)) != len(keys):
            # Two conditions under one key are one condition the engine would
            # mask and age twice, so the duplicate is refused at the boundary
            # rather than silently collapsed inside a tick.
            raise ValueError("condition keys must be unique within an observation")
        if not isinstance(self.reason_code, str):
            raise ValueError("reason_code must be a string")
        if not isinstance(self.summary, str):
            raise ValueError("summary must be a string")
        if not isinstance(self.head_changed, bool):
            raise ValueError("head_changed must be a boolean")
        if self.status is MonitorObservationStatus.PROVIDER_ERROR:
            if not isinstance(self.provider_error, ProviderErrorKind):
                raise ValueError("provider_error must be a ProviderErrorKind for a provider error")
            if self.supplemental_provider_error is not None:
                raise ValueError("supplemental_provider_error is not valid for a provider error")
            if self.head_changed:
                raise ValueError("head_changed is not valid for a provider error observation")
            if self.conditions:
                # A failed read is no evidence ABOUT the subject, so it has no
                # condition to name. Refusing here keeps the error paths from
                # ever seeding the re-alert map with a key nothing observed.
                raise ValueError("conditions are not valid for a provider error observation")
            return
        if not self.fingerprint:
            raise ValueError("fingerprint is required for a comparable observation")
        if self.provider_error is not None:
            raise ValueError("provider_error is only valid for a provider error observation")
        if self.supplemental_provider_error is not None and not isinstance(
            self.supplemental_provider_error, ProviderErrorKind
        ):
            raise ValueError("supplemental_provider_error must be a ProviderErrorKind")


@dataclass(frozen=True)
class MonitorVerdict:
    """One decision and the observations it was rendered against.

    A bare :class:`MonitorDecision` says what the controller should do but not
    what it saw, so the verdict carries its evidence instead of leaving consumers
    to re-derive it from persisted state.

    ``entries`` is plural because a subject can report several independent
    conditions (a failing check, a stale review stamp), even though a
    single-subject probe fills it with one entry.

    There is no operator-facing text field: delivery composes the wake text from
    durable state in ``monitoring.controller.format_monitor_wake``.

    Nothing here may name a provider. A fact meaningful to only one monitored
    kind belongs on that kind's observation, never on the shared verdict.
    """

    decision: MonitorDecision
    entries: tuple[MonitorObservation, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.decision, MonitorDecision):
            raise ValueError("decision must be a MonitorDecision")
        if not isinstance(self.entries, tuple):
            raise ValueError("entries must be a tuple")
        for entry in self.entries:
            if not isinstance(entry, MonitorObservation):
                raise ValueError("every verdict entry must be a MonitorObservation")


@dataclass(frozen=True)
class MonitorProbeResult:
    """What one probe learned about ONE subject, in terms the engine can read.

    Names no host. ``canonical`` holds the subject's durable facts, shaped by its
    kind and opaque to the decision engine, which only persists and compares it;
    ``observation`` is the generic classification the engine acts on.

    A record rather than a bare list of rows, so a new defaulted field (such as a
    host's own overall verdict) reaches every caller without a signature change.
    """

    canonical: dict[str, object]
    observation: MonitorObservation

    def __post_init__(self) -> None:
        if not isinstance(self.observation, MonitorObservation):
            raise ValueError("observation must be a MonitorObservation")
        if not isinstance(self.canonical, dict):
            raise ValueError("canonical must be a dict")


class MonitorProbe(Protocol):
    """The external probe boundary, shared by every path that observes a subject.

    PLURAL by contract: it takes a sequence of subjects and returns one result per
    subject, so a host that answers for many subjects in one call fits unchanged.

    The returned mapping is keyed by the subject string AS PASSED IN, not by any
    identity the host derives, so a host may normalize subjects internally.

    Nothing in this signature names a host, so any kind can satisfy it.
    """

    def probe(
        self,
        subjects: Sequence[str],
        *,
        previous_observations: Mapping[str, Mapping[str, object]] | None = None,
    ) -> Mapping[str, MonitorProbeResult]: ...


class MonitorProbeProvider(MonitorProbe, Protocol):
    """The probe contract the controller calls, one provider per monitored kind.

    :class:`MonitorProbe` plus ``use_owner_credentials``: the controller passes
    False when the monitor's owner did not authorize probing with their
    credentials. The persistence-only path (``shadow.run_shadow_probe``) takes the
    narrower :class:`MonitorProbe` because it never applies that gate. Every
    built-in provider and every contributed kind's provider satisfies both.
    """

    def probe(
        self,
        subjects: Sequence[str],
        *,
        previous_observations: Mapping[str, Mapping[str, object]] | None = None,
        use_owner_credentials: bool = True,
    ) -> Mapping[str, MonitorProbeResult]: ...


def transient_probe_failure() -> MonitorProbeResult:
    """The result a probe that could not answer usably is treated as returning."""
    return MonitorProbeResult(
        canonical={},
        observation=MonitorObservation(
            "",
            MonitorObservationStatus.PROVIDER_ERROR,
            provider_error=ProviderErrorKind.TRANSIENT,
            reason_code="provider_transient",
        ),
    )


def resolve_probe_result(results: object, subject: str) -> MonitorProbeResult:
    """Take one subject's result out of a probe's mapping, or fail closed.

    A provider may omit a subject or answer with the wrong shape. Neither is a
    verdict, and an untyped value would fail deep inside the engine, so both
    become a transient failure here. Every consumer resolves through this
    function so they agree on what an unusable answer means.

    The unusable case is logged HERE: the fallback looks identical to a provider's
    own ``provider_transient``, so a caller could not tell them apart.
    """
    if not isinstance(results, Mapping):
        logger.error(
            "structured monitor probe answered with %s, not a mapping",
            type(results).__name__,
        )
        return transient_probe_failure()
    result = results.get(subject)
    if not isinstance(result, MonitorProbeResult):
        logger.error("structured monitor probe gave no usable result for %r", subject)
        return transient_probe_failure()
    return result


#: Longest short-reading digest a loaded record may carry. The gate writes a
#: 16-character hex digest; the bound is what holds when the record came off a store
#: an agent shell can write. A longer value is dropped, which fails to match and so
#: delivers.
_MAX_SHORT_READING_DIGEST_CHARS = 64


@dataclass
class MonitorState:
    """Restart-durable state for one structured monitor."""

    kind: str
    target: str
    objective: str
    created_ts: float
    creation_surface: MonitorCreationSurface = MonitorCreationSurface.UNKNOWN
    version: int = MONITOR_STATE_VERSION
    config_generation: int = 1
    budgets: MonitorBudgets = field(default_factory=MonitorBudgets)
    cadence_secs: int = DEFAULT_MONITOR_CADENCE_SECS
    wake_instructions: str = ""
    #: Opt-in: wake the owning session ONCE when the watch ends on a terminal
    #: pull-request transition (merged or closed-unmerged), reusing the ordinary
    #: actionable-wake path. Default False keeps every existing and stored watch
    #: turn-free on merge/close, exactly as before this field existed.
    wake_on_terminal: bool = False
    last_observation: dict[str, object] = field(default_factory=dict)
    last_observation_status: MonitorObservationStatus | None = None
    last_observation_reason_code: str = ""
    last_fingerprint: str = ""
    last_observed_at: float = 0.0
    last_wake_fingerprint: str = ""
    wake_in_flight: bool = False
    wake_delivery: MonitorDispatchResult | None = None
    wake_count: int = 0
    completion_evidence_deadline: float = 0.0
    last_wake_reason_code: str = ""
    last_completion_fingerprint: str = ""
    last_completion_disposition: MonitorActionDisposition | None = None
    last_completed_at: float = 0.0
    token_usage_known: bool = True
    agent_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    consecutive_provider_errors: int = 0
    probe_count: int = 0
    provider_error_count: int = 0
    last_probe_at: float = 0.0
    last_decision: MonitorDecision | None = None
    last_provider_error: ProviderErrorKind | None = None
    #: The coalescing window, one entry per condition currently waiting out the
    #: floor: dedupe key to the time that condition's window opened.
    #:
    #: Per condition because one timestamp cannot age several conditions: a late
    #: one would be released by a window opened before it existed.
    #:
    #: An empty map means no window is open, so a record without this field loads
    #: as a fresh start rather than a window opened at time zero.
    coalesce_windows: dict[str, float] = field(default_factory=dict)
    #: Per-condition time of the last wake it caused, for level-triggered
    #: re-assertion: a condition re-wakes only once its entry is older than the
    #: re-alert interval. Keyed by dedupe key, so the key space says whether a
    #: head change clears it. Pruned unconditionally each settled decision,
    #: because on a durable per-loop record this map grows across restarts and
    #: the growth is a durability cost, not untidiness.
    coalesce_alerted: dict[str, float] = field(default_factory=dict)
    #: The stall streak: a digest of the last COUNTED verdict, and how many
    #: consecutive ticks have reached exactly that verdict.
    #:
    #: A tick counts only when it settled the subject AND the engine then did
    #: nothing about it -- a ``NO_CHANGE``. Any other settled decision zeroes all
    #: three fields, because it means the watch was working: a wake acted, a
    #: record or a retry deferred on purpose, a stop already ended it.
    #:
    #: The digest is derived from the verdict each tick; what is stored is the
    #: digest of the PREVIOUS counted verdict, which nothing else in the record holds.
    #:
    #: All three fields load as absent-means-fresh, so a record without them
    #: starts its streak on its next tick. Do not seed the streak from
    #: ``last_decision`` and ``last_fingerprint``: they do not carry the whole
    #: verdict, so a seeded digest could claim a false match and stop a live watch.
    stall_digest: str = ""
    #: Consecutive counted ticks whose verdict digest matched ``stall_digest``,
    #: counting this one. Zero before the first counted tick and after ANY tick
    #: that was not one, so the word "consecutive" means what it says.
    stall_streak: int = 0
    #: When the current streak's first counted tick landed. Zero means no streak.
    #:
    #: Wall-clock is stored directly rather than as a tick ceiling derived from
    #: ``cadence_secs``: a cached ceiling goes wrong when the cadence changes.
    #:
    #: ``time.time()`` is not monotonic, but a backward jump only delays a trip and
    #: a forward jump cannot supply the counted ticks. The fold must zero this pair
    #: on every tick that is not counted, unsettled ticks included, or a stale
    #: streak could give the next non-retryable provider error a stall's reason.
    stall_started_at: float = 0.0
    #: Adoption metering, so a gate that never fires can be told from one doing
    #: its job. ``quiet_ticks`` counts ticks the probe judged QUIET; ``wakes``
    #: counts turns actually DELIVERED otherwise (undecided ticks are
    #: ``gate_fallbacks``; a refused fire counts in neither). The streak floor
    #: delivers on some quiet ticks, so ``quiet_ticks - floor_ticks`` is the count
    #: that cost no model turn.
    quiet_ticks: int = 0
    wakes: int = 0
    #: Ticks where the gate could not decide and so resolved toward firing.
    #: Counted at the observation (it measures gate failure, not delivery, which is
    #: ``cycle_count``'s), and apart from ``wakes`` so a broken gate cannot read as
    #: a busy, well-used watch.
    gate_fallbacks: int = 0
    #: Ticks still owed to the agent after a wake, during which the gate is
    #: bypassed and the loop fires on its plain timer.
    #:
    #: A woken agent often needs another turn, and the probe watches the SUBJECT,
    #: so an agent that has not pushed yet looks like no change. Firing once more
    #: after each wake costs one turn and keeps a pure gate from starving that work.
    followup_ticks: int = 0
    #: Consecutive quiet observations since the last delivered turn.
    #:
    #: The gate watches the SUBJECT, so a loop whose duty is to act WHILE the
    #: subject is quiet -- refresh a heartbeat file, chase a reviewer who still
    #: has not replied, keep a branch rebased on a moving base -- produces no
    #: observable change and would never be delivered again. Inference cannot
    #: tell that intent from the wording, and guessing it is worse than bounding
    #: it: after enough consecutive quiet ticks the loop is delivered anyway.
    quiet_streak: int = 0
    #: Turns delivered because the quiet streak hit its floor rather than because
    #: anything was observed. Counted apart from wakes so the metering does not
    #: report a periodic delivery as a real signal.
    floor_ticks: int = 0
    #: True from the tick that DECIDES a floor delivery until that delivery is
    #: CONFIRMED.
    #:
    #: The decision durably resets ``quiet_streak`` while the debt lives only in
    #: memory, so a gateway death in between would lose the owed turn.
    #: (``followup_ticks`` covers a REFUSED fire, not a stopped process.)
    #:
    #: Found set on a later tick, it fires WITHOUT observing, since re-observing
    #: would answer quiet again. Cleared only where delivery is confirmed (where
    #: ``floor_ticks`` is charged), so a refusal or death leaves it owed.
    floor_fire_pending: bool = False
    #: Pull-request watches only: the digest of the SHORT reading the last delivered
    #: turn was decided on, and how many ticks since then have repeated exactly that
    #: reading without a turn.
    #:
    #: A short reading (the fetch reached the subject and left part of it unread) can
    #: never be screened quiet on the half that arrived, so without these every tick
    #: of a degraded forge delivers a turn carrying nothing the last one did not. A
    #: repeat of the reading a turn was already delivered on is HELD instead, up to
    #: the gate's ceiling, and anything else -- a change in what was read, a different
    #: reason it came back short, a whole reading -- delivers or ends the run.
    #:
    #: The digest is written only where a delivery is confirmed, so a refused fire or
    #: a process that stops before its turn lands holds nothing. A whole reading and
    #: an update that re-aims the loop clear both fields, and an unreadable value
    #: loads as no baseline at all, which delivers the next short reading.
    short_reading_digest: str = ""
    short_reading_held: int = 0
    #: Pull-request watches only: the blockers the last DELIVERED turn carried, so a
    #: later tick can tell a blocker that is NEW to the owner from one they were
    #: already shown. ``delivered_failed_checks`` is the sorted check identities that
    #: were failing in that reading; ``delivered_conflicting`` whether it conflicted
    #: with its base. A blocker present now but absent from this basis reached the
    #: subject AFTER the owner last looked, so the gate wakes on it even when a narrow
    #: custom judge would stay quiet -- the typed fact the judge cannot be allowed to
    #: silence. A blocker already in the basis (a red PR still under repair) stays
    #: quiet, which is what the judge's own override bar is for.
    #:
    #: Written only where a delivery is confirmed, like ``short_reading_digest``: a
    #: refused fire or a process that stops before the turn lands records no new basis,
    #: so the next tick still measures against what the owner was actually shown. An
    #: update that re-aims the loop clears both, so the first reading of a new subject
    #: is a delivery, not a spurious wake.
    delivered_failed_checks: list[str] = field(default_factory=list)
    delivered_conflicting: bool = False
    #: Work-ledger watches only: the worker-report revision the probe read on its
    #: newest successful observation, and the one it had read when the last turn
    #: was DELIVERED. While the two are equal no worker has reported since the
    #: conductor last looked, so the quiet floor waits four times longer. Empty
    #: means "not known yet", which keeps the shipped floor: an unknown revision
    #: resolves toward delivering, like every other doubt in the gate.
    ledger_revision: str = ""
    ledger_delivered_revision: str = ""
    #: True from just before a probe runs until its verdict has been consumed.
    #:
    #: The kernel commits dedupe state BEFORE raising a wake. For a driver that
    #: awaits the verdict, a cancelled await (shutdown mid-poll) records the
    #: observation as reported with no turn dispatched, losing the signal.
    #:
    #: Found still set on the next tick, it means a poll was interrupted, so that
    #: tick fires instead of trusting a quiet verdict: one extra turn at worst.
    poll_in_flight: bool = False
    #: Non-empty when the subject is terminal but the final turn owed to a CHANNEL
    #: loop has not been delivered yet. The VALUE is the outcome to record once it
    #: has (``"success"`` for a merge, ``"blocked"`` for a close without merging),
    #: so the classification survives the wait without a second field and without
    #: writing ``outcome`` early.
    #:
    #: A channel-bound loop learns its watch finished from a delivered turn, so
    #: settlement waits for it even when the channel is busy. This marker stops
    #: retries from re-announcing, and since no outcome is recorded yet, a restart
    #: in the window finds a live loop rather than a finished one.
    terminal_pending: str = ""
    #: Non-empty once that owed turn LANDED but its settlement has not: the delivery
    #: moves the outcome out of ``terminal_pending`` into this field, so a restart
    #: in the window settles without repeating the channel turn. A separate field,
    #: not a new value of ``terminal_pending``, so a gateway that predates it files
    #: the key under ``extra_fields`` and writes it back untouched, reads nothing
    #: owed, and keeps the monitor running at the same state version.
    terminal_delivered: str = ""
    next_probe_at: float = 0.0
    outcome: MonitorOutcome | None = None
    stopped_reason: str = ""
    user_stop_reason: str = ""
    stopped_at: float = 0.0
    # Persisted only after the dashboard accepts the terminal notice. False is
    # fail-safe: a restart may repeat a notice, but it cannot lose the only one.
    terminal_notification_delivered: bool = False
    extra_fields: dict[str, object] = field(default_factory=dict, repr=False)
    _raw_payload: dict[str, object] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in ("kind", "target", "objective"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version <= 0:
            raise ValueError("version must be a positive integer")
        if (
            isinstance(self.config_generation, bool)
            or not isinstance(self.config_generation, int)
            or self.config_generation <= 0
        ):
            raise ValueError("config_generation must be a positive integer")
        for name in (
            "created_ts",
            "last_observed_at",
            "last_completed_at",
            "completion_evidence_deadline",
            "last_probe_at",
            "next_probe_at",
            "stopped_at",
            "stall_started_at",
        ):
            value = getattr(self, name)
            if not is_finite_non_negative_number(value):
                raise ValueError(f"{name} must be a finite non-negative number")
        for name in (
            "wake_count",
            "agent_turns",
            "input_tokens",
            "output_tokens",
            "consecutive_provider_errors",
            "probe_count",
            "provider_error_count",
            "quiet_ticks",
            "wakes",
            "gate_fallbacks",
            "followup_ticks",
            "quiet_streak",
            "floor_ticks",
            "stall_streak",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        # Normalised toward DOUBT, not through ``bool()``: this flag means "a turn
        # may be owed", and ``bool("")`` would suppress it. Refusing the record
        # would take a working watch down, so an unreadable value costs one turn.
        if not isinstance(self.poll_in_flight, bool):
            self.poll_in_flight = True
        # Same fail-safe: an unreadable value means a forced turn may be owed.
        if not isinstance(self.floor_fire_pending, bool):
            self.floor_fire_pending = True
        # Normalised toward delivering. An unreadable revision becomes "unknown",
        # which keeps the shipped floor.
        if not isinstance(self.ledger_revision, str):
            self.ledger_revision = ""
        if not isinstance(self.ledger_delivered_revision, str):
            self.ledger_delivered_revision = ""
        # Normalised toward delivering, as a pair. The digest is what lets a repeat be
        # held, so an unreadable digest, or one longer than any digest this build
        # writes, becomes "no baseline" and fails to match. An unreadable count says
        # nothing about how long the hold has lasted, so it drops the baseline too
        # rather than restarting a hold from zero.
        if (
            not isinstance(self.short_reading_digest, str)
            or len(self.short_reading_digest) > _MAX_SHORT_READING_DIGEST_CHARS
        ):
            self.short_reading_digest = ""
        if (
            isinstance(self.short_reading_held, bool)
            or not isinstance(self.short_reading_held, int)
            or self.short_reading_held < 0
        ):
            self.short_reading_held = 0
            self.short_reading_digest = ""
        # The delivered-blocker basis is a set the gate subtracts a live reading from,
        # so an unreadable value becomes the EMPTY basis, never a guess. Empty means
        # "the owner has seen no blocker", which makes every live blocker read as new
        # and wakes -- the safe direction, matching every other doubt in the gate. A
        # list, like the check buckets on ``last_observation``, so it round-trips
        # through JSON without the tuple/list asymmetry ``asdict`` would otherwise
        # leave on the record; any non-string entry or non-list value drops to empty.
        checks = self.delivered_failed_checks
        if isinstance(checks, (list, tuple)) and all(isinstance(item, str) for item in checks):
            self.delivered_failed_checks = list(checks)
        else:
            self.delivered_failed_checks = []
        if not isinstance(self.delivered_conflicting, bool):
            self.delivered_conflicting = False
        # The marker carries an outcome name, so an unreadable value cannot be
        # guessed. Keep it PENDING and record the cautious classification: a
        # delivery still happens, and a subject wrongly called blocked prompts a
        # look rather than a false all-clear.
        if not isinstance(self.terminal_pending, str):
            self.terminal_pending = "blocked" if self.terminal_pending else ""
        if not isinstance(self.terminal_delivered, str):
            self.terminal_delivered = "blocked" if self.terminal_delivered else ""
        if not isinstance(self.budgets, MonitorBudgets):
            raise ValueError("budgets must be MonitorBudgets")
        if not isinstance(self.creation_surface, MonitorCreationSurface):
            raise ValueError("creation_surface must be a MonitorCreationSurface")
        if (
            isinstance(self.cadence_secs, bool)
            or not isinstance(self.cadence_secs, int)
            or self.cadence_secs <= 0
        ):
            raise ValueError("cadence_secs must be a positive integer")
        if not isinstance(self.last_observation, dict):
            raise ValueError("last_observation must be an object")
        _validate_strict_json_object("last_observation", self.last_observation)
        if not isinstance(self.wake_instructions, str):
            raise ValueError("wake_instructions must be a string")
        if not isinstance(self.wake_on_terminal, bool):
            raise ValueError("wake_on_terminal must be a boolean")
        if any(
            not isinstance(value, str)
            for value in (
                self.last_fingerprint,
                self.last_wake_fingerprint,
                self.last_completion_fingerprint,
                self.last_wake_reason_code,
                self.last_observation_reason_code,
            )
        ):
            raise ValueError("monitor observation metadata must be strings")
        if self.last_observation_status is not None and not isinstance(
            self.last_observation_status, MonitorObservationStatus
        ):
            raise ValueError("last_observation_status must be a MonitorObservationStatus")
        if not isinstance(self.wake_in_flight, bool):
            raise ValueError("wake_in_flight must be a boolean")
        if self.wake_delivery is not None and not isinstance(
            self.wake_delivery, MonitorDispatchResult
        ):
            raise ValueError("wake_delivery must be a MonitorDispatchResult")
        if self.last_completion_disposition is not None and not isinstance(
            self.last_completion_disposition, MonitorActionDisposition
        ):
            raise ValueError("last_completion_disposition must be a MonitorActionDisposition")
        if not isinstance(self.token_usage_known, bool):
            raise ValueError("token_usage_known must be a boolean")
        if self.last_decision is not None and not isinstance(self.last_decision, MonitorDecision):
            raise ValueError("last_decision must be a MonitorDecision")
        if self.last_provider_error is not None and not isinstance(
            self.last_provider_error, ProviderErrorKind
        ):
            raise ValueError("last_provider_error must be a ProviderErrorKind")
        if not isinstance(self.coalesce_windows, dict):
            raise ValueError("coalesce_windows must be an object")
        for key, value in self.coalesce_windows.items():
            if not isinstance(key, str) or not key:
                raise ValueError("coalesce_windows keys must be non-empty strings")
            if not is_finite_non_negative_number(value):
                raise ValueError("coalesce_windows values must be finite non-negative numbers")
        if not isinstance(self.coalesce_alerted, dict):
            raise ValueError("coalesce_alerted must be an object")
        for key, value in self.coalesce_alerted.items():
            if not isinstance(key, str):
                raise ValueError("coalesce_alerted keys must be strings")
            if not is_finite_non_negative_number(value):
                raise ValueError("coalesce_alerted values must be finite non-negative numbers")
        # A key with no key space matches nothing the engine computes, so its
        # window would never close and its alert would never mask. Adopting here
        # rather than in the loader covers every path that builds a state.
        self.coalesce_windows = {
            adopt_monitor_dedupe_key(key): value for key, value in self.coalesce_windows.items()
        }
        self.coalesce_alerted = {
            adopt_monitor_dedupe_key(key): value for key, value in self.coalesce_alerted.items()
        }
        if not isinstance(self.stall_digest, str):
            raise ValueError("stall_digest must be a string")
        if self.outcome is not None and not isinstance(self.outcome, MonitorOutcome):
            raise ValueError("outcome must be a MonitorOutcome")
        if not isinstance(self.stopped_reason, str):
            raise ValueError("stopped_reason must be a string")
        if not isinstance(self.user_stop_reason, str):
            raise ValueError("user_stop_reason must be a string")
        if len(self.user_stop_reason) > MAX_MONITOR_STOP_REASON_CHARS:
            raise ValueError("user_stop_reason is too long")
        if not isinstance(self.terminal_notification_delivered, bool):
            self.terminal_notification_delivered = False
        if not isinstance(self.extra_fields, dict):
            raise ValueError("extra_fields must be an object")
        _validate_strict_json_object("extra_fields", self.extra_fields)
        if self._raw_payload is not None and not isinstance(self._raw_payload, dict):
            raise ValueError("_raw_payload must be an object")
        if self._raw_payload is not None:
            _validate_strict_json_object("_raw_payload", self._raw_payload)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def _validate_strict_json_object(name: str, value: dict[str, object]) -> None:
    """Reject state that Python can encode only with non-standard JSON literals."""
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain strict JSON values") from exc


def monitor_state_from_dict(raw: object) -> MonitorState:
    """Decode a persisted monitor while ignoring fields owned by newer versions.

    The caller is responsible for deactivating versions it does not implement.
    Keeping the recognized identity fields makes such records inspectable.
    """
    if not isinstance(raw, dict):
        raise ValueError("monitor state must be an object")
    if raw.get("version", MONITOR_STATE_VERSION) != MONITOR_STATE_VERSION:
        # A newer version may give familiar fields different semantics. Keep an
        # inert local view for the loader while retaining the exact raw payload
        # for a later compatible controller to inspect and rewrite unchanged.
        values: dict[str, Any] = {"version": raw.get("version")}
        for key in ("kind", "target", "objective"):
            value = raw.get(key)
            values[key] = value if isinstance(value, str) and value else f"unsupported_{key}"
        created_ts = raw.get("created_ts")
        values["created_ts"] = created_ts if is_finite_non_negative_number(created_ts) else 0.0
        values["budgets"] = MonitorBudgets()
        values["_raw_payload"] = deepcopy(raw)
        return MonitorState(**values)
    allowed = {
        item.name
        for item in fields(MonitorState)
        if item.name not in {"extra_fields", "_raw_payload"}
    }
    values = {key: value for key, value in raw.items() if key in allowed}
    values["extra_fields"] = {
        key: value
        for key, value in raw.items()
        if key not in allowed and key not in _RETIRED_MONITOR_STATE_FIELDS
    }
    budgets = values.get("budgets")
    if isinstance(budgets, dict):
        values["budgets"] = MonitorBudgets(**budgets)
    elif budgets is not None and not isinstance(budgets, MonitorBudgets):
        raise ValueError("monitor budgets must be an object")
    outcome = values.get("outcome")
    if outcome is not None:
        values["outcome"] = MonitorOutcome(outcome)
    disposition = values.get("last_completion_disposition")
    if disposition is not None:
        values["last_completion_disposition"] = MonitorActionDisposition(disposition)
    delivery = values.get("wake_delivery")
    if delivery is not None:
        values["wake_delivery"] = MonitorDispatchResult(delivery)
    decision = values.get("last_decision")
    if decision is not None:
        values["last_decision"] = MonitorDecision(decision)
    provider_error = values.get("last_provider_error")
    if provider_error is not None:
        values["last_provider_error"] = ProviderErrorKind(provider_error)
    observation_status = values.get("last_observation_status")
    if observation_status is not None:
        values["last_observation_status"] = MonitorObservationStatus(observation_status)
    creation_surface = values.get("creation_surface")
    if creation_surface is not None:
        values["creation_surface"] = MonitorCreationSurface(creation_surface)
    # An ABSENT coalesce_alerted (not an empty one, which means nothing alerted
    # yet) is seeded from last_wake_fingerprint and last_completed_at, so a
    # monitor that already woke is not re-woken on its first probe after loading.
    if "coalesce_alerted" not in raw:
        woke_on = values.get("last_wake_fingerprint")
        woke_at = values.get("last_completed_at")
        if (
            isinstance(woke_on, str)
            and woke_on
            and isinstance(woke_at, (int, float))
            and not isinstance(woke_at, bool)
            and is_finite_non_negative_number(woke_at)
            and woke_at > 0
        ):
            values["coalesce_alerted"] = {adopt_monitor_dedupe_key(woke_on): float(woke_at)}
    else:
        # A record persisted before the key spaces existed keys this map by
        # whole-subject fingerprint. Adopting each key into the revision space
        # keeps a monitor that already alerted from reading as never-alerted, so
        # a subject with no conditions of its own -- whose synthesized key IS its
        # fingerprint -- crosses the upgrade without one extra wake.
        stored = values.get("coalesce_alerted")
        if isinstance(stored, dict):
            values["coalesce_alerted"] = {
                (adopt_monitor_dedupe_key(key) if isinstance(key, str) else key): value
                for key, value in stored.items()
            }
    return MonitorState(**values)


def quarantine_monitor_state(raw: object) -> MonitorState:
    """Build a valid inert replacement for a malformed current-version record."""
    if not isinstance(raw, dict):
        raise ValueError("monitor state must be an object")

    def _identity(name: str) -> str:
        value = raw.get(name)
        return value if isinstance(value, str) and value else f"invalid_{name}"

    raw_created_ts = raw.get("created_ts")
    created_ts: int | float = 0.0
    if is_finite_non_negative_number(raw_created_ts):
        assert isinstance(raw_created_ts, (int, float)) and not isinstance(raw_created_ts, bool)
        created_ts = raw_created_ts
    raw_payload_candidate: dict[str, object] = deepcopy(raw)
    try:
        _validate_strict_json_object("_raw_payload", raw_payload_candidate)
    except ValueError:
        # Python's permissive JSON decoder accepts non-finite numbers that the
        # persisted monitor contract forbids. Keep the outer loop and its inert
        # quarantine view even though that invalid payload cannot be rewritten.
        raw_payload = None
    else:
        raw_payload = raw_payload_candidate
    return MonitorState(
        kind=_identity("kind"),
        target=_identity("target"),
        objective=_identity("objective"),
        created_ts=created_ts,
        # An inert quarantine must be constructible even below the ordinary
        # default runtime. The original invalid budget remains in _raw_payload.
        budgets=MonitorBudgets(max_runtime_secs=1),
        outcome=MonitorOutcome.BLOCKED,
        stopped_reason=MONITOR_STOP_INVALID_RECORD,
        _raw_payload=raw_payload,
    )


def monitor_state_to_dict(state: MonitorState) -> dict[str, object]:
    """Encode known state while preserving fields written by a newer version."""
    if state._raw_payload is not None:
        return deepcopy(state._raw_payload)
    payload = asdict(state)
    extra = payload.pop("extra_fields")
    payload.pop("_raw_payload")
    if isinstance(extra, dict):
        for key, value in extra.items():
            payload.setdefault(key, value)
    return payload


def _bounded_check_identities(values: object) -> bool:
    """Whether one canonical check bucket is a bounded list of bounded identities."""
    return (
        isinstance(values, list)
        and len(values) <= MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET
        and all(
            isinstance(value, str) and value and len(value) <= MAX_MONITOR_CHECK_IDENTITY_CHARS
            for value in values
        )
    )


def _public_pull_request_observation(
    raw: dict[str, object],
    *,
    expected_kind: str,
) -> dict[str, object]:
    """Project only the bounded canonical schema across the public boundary."""
    if not raw:
        return {}
    # ``checks_complete`` was added after the initial GitHub schema. Old
    # snapshots could only be written from a complete rollup, so absence has
    # the precise legacy meaning True. Present malformed values still fail
    # closed instead of being truthiness-coerced.
    projected = raw if "checks_complete" in raw else {**raw, "checks_complete": True}
    checks = projected.get("checks")
    if not isinstance(checks, dict):
        return {}
    for field_name in PULL_REQUEST_OBSERVATION_FIELDS:
        if field_name not in projected:
            return {}
    for field_name in PULL_REQUEST_CHECK_FIELDS:
        if not _bounded_check_identities(checks.get(field_name)):
            return {}
    # The superseded bucket is the only projected one that may be absent, so it is
    # validated exactly when it is there. A key outside the projected names is not
    # copied at all, which is how provider diagnostics stay on the private side.
    if PULL_REQUEST_SUPERSEDED_CHECK_FIELD in checks and not _bounded_check_identities(
        checks[PULL_REQUEST_SUPERSEDED_CHECK_FIELD]
    ):
        return {}
    unresolved = projected.get("unresolved_review_threads")
    blocking_review = projected.get("blocking_review")
    mergeability = projected.get("mergeability")
    review_decision = projected.get("review_decision")
    pull_request_state = projected.get("state")
    if (
        projected.get("kind") != expected_kind
        or not is_pull_request_monitor_kind(expected_kind)
        or not isinstance(projected.get("target"), str)
        or not projected.get("target")
        or not isinstance(projected.get("head_revision"), str)
        or not isinstance(projected.get("draft"), bool)
        or not isinstance(projected.get("checks_complete"), bool)
        or not isinstance(projected.get("review_threads_complete"), bool)
        or isinstance(unresolved, bool)
        or not isinstance(unresolved, int)
        or unresolved < 0
        or not isinstance(blocking_review, str)
        or blocking_review not in PULL_REQUEST_BLOCKING_REVIEWS
        or not isinstance(mergeability, str)
        or mergeability not in PULL_REQUEST_MERGEABILITY
        or not isinstance(review_decision, str)
        or review_decision not in PULL_REQUEST_REVIEW_DECISIONS
        or not isinstance(pull_request_state, str)
        or pull_request_state not in PULL_REQUEST_STATES
    ):
        return {}
    public = {key: deepcopy(projected[key]) for key in PULL_REQUEST_OBSERVATION_FIELDS}
    public["checks"] = {
        key: deepcopy(checks[key])
        for key in (*PULL_REQUEST_CHECK_FIELDS, PULL_REQUEST_SUPERSEDED_CHECK_FIELD)
        if key in checks
    }
    return public


def monitor_state_public_dict(state: MonitorState) -> dict[str, object]:
    """Return the stable inspect/dashboard fields without persistence internals."""
    payload = {key: deepcopy(getattr(state, key)) for key in MONITOR_PUBLIC_FIELDS}
    payload["budgets"] = asdict(state.budgets)
    payload["last_observation"] = _public_pull_request_observation(
        state.last_observation,
        expected_kind=state.kind,
    )
    return payload
