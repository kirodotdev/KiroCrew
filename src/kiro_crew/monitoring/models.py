"""Transport-independent monitor state.

The scheduler, provider adapters, and session delivery code exchange these
small records. They deliberately contain no provider clients or callbacks, so
they can be persisted and evaluated without starting an agent turn.
"""

from __future__ import annotations

import json
import math
from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any

MONITOR_STATE_VERSION = 1
DEFAULT_MONITOR_RUNTIME_SECS = 14_400
DEFAULT_MONITOR_AGENT_TURNS = 8
DEFAULT_MONITOR_TOKENS = 250_000
DEFAULT_MONITOR_PROVIDER_ERRORS = 3
DEFAULT_MONITOR_CADENCE_SECS = 300
MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET = 100
MAX_MONITOR_CHECK_IDENTITY_CHARS = 200
MONITOR_STOP_INVALID_RECORD = "invalid_monitor_record"
MIN_MONITOR_CADENCE_SECS = 15
MAX_MONITOR_CADENCE_SECS = 86_400
MAX_MONITOR_RUNTIME_SECS = 604_800
MAX_MONITOR_AGENT_TURNS = 8
MAX_MONITOR_TOKENS = 1_000_000
MAX_MONITOR_PROVIDER_ERRORS = 20
MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS = 1_000
MAX_MONITOR_STOP_REASON_CHARS = 500
MAX_MONITOR_CHECK_NAMES = 8
# The normal turn ceiling is two hours. One extra minute lets the raw completion
# callback win the timeout race while keeping missing evidence restart-durable
# and bounded.
MONITOR_COMPLETION_EVIDENCE_TIMEOUT_SECS = 7_260
MONITOR_BUSY_RETRY_SECS = 15
MONITOR_STOP_RUNTIME_BUDGET = "runtime_budget"
MONITOR_STOP_AGENT_TURN_BUDGET = "agent_turn_budget"
MONITOR_STOP_TOKEN_BUDGET = "token_budget"
MONITOR_STOP_PROVIDER_ERROR_BUDGET = "provider_error_budget"
MONITOR_STOP_APPROVAL_STALL = "approval_stall"
MONITOR_STOP_COMPLETION_UNAVAILABLE = "completion_evidence_unavailable"
MONITOR_STOP_UNSUPPORTED_VERSION = "unsupported_monitor_version"
MONITOR_STOP_USER = "user_stop"
MONITOR_STOP_SESSION_UNAVAILABLE = "session_unavailable"
MONITOR_STOP_SESSION_CLOSE = "session_close"
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
    "last_observation",
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


def _is_finite_non_negative_number(value: object) -> bool:
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
        if (
            isinstance(self.completed_ts, bool)
            or not isinstance(self.completed_ts, (int, float))
            or not math.isfinite(self.completed_ts)
            or self.completed_ts < 0
        ):
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

    Unlike legacy AutoNudge values, zero never means unlimited here.
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
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_agent_turns > DEFAULT_MONITOR_AGENT_TURNS:
            raise ValueError(f"max_agent_turns must be at most {DEFAULT_MONITOR_AGENT_TURNS}")


@dataclass(frozen=True)
class MonitorObservation:
    """Small canonical result produced by a typed provider probe."""

    fingerprint: str
    status: MonitorObservationStatus
    provider_error: ProviderErrorKind | None = None
    supplemental_provider_error: ProviderErrorKind | None = None
    reason_code: str = ""
    summary: str = ""
    head_changed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.status, MonitorObservationStatus):
            raise ValueError("status must be a MonitorObservationStatus")
        if not isinstance(self.fingerprint, str):
            raise ValueError("fingerprint must be a string")
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
            return
        if not self.fingerprint:
            raise ValueError("fingerprint is required for a comparable observation")
        if self.provider_error is not None:
            raise ValueError("provider_error is only valid for a provider error observation")
        if self.supplemental_provider_error is not None and not isinstance(
            self.supplemental_provider_error, ProviderErrorKind
        ):
            raise ValueError("supplemental_provider_error must be a ProviderErrorKind")


@dataclass
class MonitorState:
    """Restart-durable state for one structured monitor."""

    kind: str
    target: str
    objective: str
    created_ts: float
    version: int = MONITOR_STATE_VERSION
    config_generation: int = 1
    budgets: MonitorBudgets = field(default_factory=MonitorBudgets)
    cadence_secs: int = DEFAULT_MONITOR_CADENCE_SECS
    wake_instructions: str = ""
    last_observation: dict[str, object] = field(default_factory=dict)
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
    #: Adoption metering. Without these two numbers a probe gate that never
    #: fires and a probe gate that is doing its job are indistinguishable from
    #: the outside -- which is how the earlier attempts at this saving stayed at
    #: zero adoption, unnoticed, for over a week. ``quiet_ticks`` counts the ticks
    #: the probe judged QUIET; ``wakes`` counts the turns actually DELIVERED because
    #: it judged otherwise -- not every non-quiet tick, since a gate that could not
    #: decide is ``gate_fallbacks`` below and a fire the slot refuses is charged to
    #: neither. A quiet verdict is
    #: not the same as a free tick: the streak floor below deliberately delivers on
    #: one of them, so ``quiet_ticks`` minus ``floor_ticks`` is the count that cost
    #: no model turn. Saying "cost no turn" here would overstate the saving by
    #: exactly the floor, which is the one number this PR must not get wrong.
    quiet_ticks: int = 0
    wakes: int = 0
    #: Ticks where the gate could not decide, so the tick resolved toward firing on
    #: the plain timer. Counted at the OBSERVATION, which is why the wording avoids
    #: claiming the loop fired: a busy slot can still refuse that turn, and whether
    #: it landed is ``cycle_count``'s business. What this measures is GATE FAILURE,
    #: and a gate that could not decide has failed whether or not the slot happened
    #: to accept the turn. Counted apart from ``wakes`` so the metering cannot
    #: flatter itself: a gate that is permanently broken would otherwise read as
    #: a busy, well-used watch.
    gate_fallbacks: int = 0
    #: Ticks still owed to the agent after a wake, during which the gate is
    #: bypassed and the loop fires on its plain timer.
    #:
    #: A woken agent usually cannot finish inside one turn -- it reads the
    #: findings, fixes some, and needs another turn to finish. The probe cannot
    #: see any of that: it watches the SUBJECT, so an agent that was woken and
    #: has not yet pushed produces no observable change, and a pure gate would
    #: report "nothing happened" and starve the work it just started. Firing once
    #: more after every wake costs one turn per wake and removes that stall
    #: entirely, which is the right trade against a watch that goes quiet holding
    #: half-finished work.
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
    #: True from just before a probe runs until its verdict has been consumed.
    #:
    #: The kernel commits its dedupe state BEFORE raising a wake, which is right
    #: for the cron driver -- there the raise IS the delivery. For a driver that
    #: awaits the verdict, the two come apart: cancel the await (a gateway
    #: shutdown lands mid-poll) and the observation is recorded as reported while
    #: no turn was ever dispatched, so the next run reads the same state as
    #: unchanged and the real signal is lost until the streak floor.
    #:
    #: Finding this flag still set on the next tick therefore means "a poll was
    #: interrupted and its result may already have been consumed on disk" -- so
    #: that tick fires instead of trusting a quiet verdict. Being wrong costs one
    #: turn; being silent costs the signal.
    poll_in_flight: bool = False
    #: Non-empty when the subject is terminal but the final turn owed to a CHANNEL
    #: loop has not been delivered yet. The VALUE is the outcome to record once it
    #: has (``"success"`` for a merge, ``"blocked"`` for a close without merging),
    #: so the classification survives the wait without a second field and without
    #: writing ``outcome`` early.
    #:
    #: A channel-bound loop learns its watch finished from a delivered turn, not
    #: from the dashboard notification, so settling before that turn lands would
    #: leave an inactive loop with nothing to re-arm the moment the channel is
    #: busy -- and a busy thread is the ordinary case. The settlement therefore
    #: waits: this marker is what stops the retry from re-announcing forever, and
    #: because no outcome is recorded in the meantime a restart in the window finds
    #: a plain live loop rather than one tagged as finished and refused revival.
    terminal_pending: str = ""
    next_probe_at: float = 0.0
    outcome: MonitorOutcome | None = None
    stopped_reason: str = ""
    user_stop_reason: str = ""
    stopped_at: float = 0.0
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
        ):
            value = getattr(self, name)
            if not _is_finite_non_negative_number(value):
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
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        # NORMALISED, not validated, and normalised toward DOUBT rather than
        # through ``bool()``. This flag means "a turn may be owed"; a stored ``""``
        # or ``0`` is a record we cannot read, and ``bool("")`` would clear the
        # fail-safe and let a quiet verdict suppress a turn that was owed. Refusing
        # the monitor outright is worse -- it takes a working watch down -- so an
        # unreadable value becomes True and costs at most one turn.
        if not isinstance(self.poll_in_flight, bool):
            self.poll_in_flight = True
        # The marker carries an outcome name, so an unreadable value cannot be
        # guessed. Keep it PENDING and record the cautious classification: a
        # delivery still happens, and a subject wrongly called blocked prompts a
        # look rather than a false all-clear.
        if not isinstance(self.terminal_pending, str):
            self.terminal_pending = "blocked" if self.terminal_pending else ""
        if not isinstance(self.budgets, MonitorBudgets):
            raise ValueError("budgets must be MonitorBudgets")
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
        if any(
            not isinstance(value, str)
            for value in (
                self.last_fingerprint,
                self.last_wake_fingerprint,
                self.last_completion_fingerprint,
                self.last_wake_reason_code,
            )
        ):
            raise ValueError("monitor fingerprints must be strings")
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
        if self.outcome is not None and not isinstance(self.outcome, MonitorOutcome):
            raise ValueError("outcome must be a MonitorOutcome")
        if not isinstance(self.stopped_reason, str):
            raise ValueError("stopped_reason must be a string")
        if not isinstance(self.user_stop_reason, str):
            raise ValueError("user_stop_reason must be a string")
        if len(self.user_stop_reason) > MAX_MONITOR_STOP_REASON_CHARS:
            raise ValueError("user_stop_reason is too long")
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
        values["created_ts"] = created_ts if _is_finite_non_negative_number(created_ts) else 0.0
        values["budgets"] = MonitorBudgets()
        values["_raw_payload"] = deepcopy(raw)
        return MonitorState(**values)
    allowed = {
        item.name
        for item in fields(MonitorState)
        if item.name not in {"extra_fields", "_raw_payload"}
    }
    values = {key: value for key, value in raw.items() if key in allowed}
    values["extra_fields"] = {key: value for key, value in raw.items() if key not in allowed}
    budgets = values.get("budgets")
    if isinstance(budgets, dict):
        values["budgets"] = MonitorBudgets(**budgets)
    elif budgets is not None and not isinstance(budgets, MonitorBudgets):
        raise ValueError("monitor budgets must be an object")
    outcome = values.get("outcome")
    if outcome is not None:
        values["outcome"] = MonitorOutcome(outcome)
    else:
        values["outcome"] = None
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
    if _is_finite_non_negative_number(raw_created_ts):
        assert isinstance(raw_created_ts, (int, float)) and not isinstance(raw_created_ts, bool)
        created_ts = raw_created_ts
    return MonitorState(
        kind=_identity("kind"),
        target=_identity("target"),
        objective=_identity("objective"),
        created_ts=created_ts,
        outcome=MonitorOutcome.BLOCKED,
        stopped_reason=MONITOR_STOP_INVALID_RECORD,
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


def monitor_state_public_dict(state: MonitorState) -> dict[str, object]:
    """Return the stable inspect/dashboard fields without persistence internals."""
    payload = {key: deepcopy(getattr(state, key)) for key in MONITOR_PUBLIC_FIELDS}
    payload["budgets"] = asdict(state.budgets)
    return payload
