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
MONITOR_STOP_INVALID_RECORD = "invalid_monitor_record"
MONITOR_STOP_RUNTIME_BUDGET = "runtime_budget"
MONITOR_STOP_AGENT_TURN_BUDGET = "agent_turn_budget"
MONITOR_STOP_TOKEN_BUDGET = "token_budget"
MONITOR_STOP_APPROVAL_STALL = "approval_stall"
MONITOR_STOP_COMPLETION_UNAVAILABLE = "completion_evidence_unavailable"
MONITOR_STOP_UNSUPPORTED_VERSION = "unsupported_monitor_version"


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
    reason_code: str = ""
    summary: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.status, MonitorObservationStatus):
            raise ValueError("status must be a MonitorObservationStatus")
        if not isinstance(self.fingerprint, str):
            raise ValueError("fingerprint must be a string")
        if not isinstance(self.reason_code, str):
            raise ValueError("reason_code must be a string")
        if not isinstance(self.summary, str):
            raise ValueError("summary must be a string")
        if self.status is MonitorObservationStatus.PROVIDER_ERROR:
            if not isinstance(self.provider_error, ProviderErrorKind):
                raise ValueError("provider_error must be a ProviderErrorKind for a provider error")
            return
        if not self.fingerprint:
            raise ValueError("fingerprint is required for a comparable observation")
        if self.provider_error is not None:
            raise ValueError("provider_error is only valid for a provider error observation")


@dataclass
class MonitorState:
    """Restart-durable state for one structured monitor."""

    kind: str
    target: str
    objective: str
    created_ts: float
    version: int = MONITOR_STATE_VERSION
    budgets: MonitorBudgets = field(default_factory=MonitorBudgets)
    cadence_secs: int = DEFAULT_MONITOR_CADENCE_SECS
    last_observation: dict[str, object] = field(default_factory=dict)
    last_fingerprint: str = ""
    last_observed_at: float = 0.0
    last_wake_fingerprint: str = ""
    wake_in_flight: bool = False
    last_completion_fingerprint: str = ""
    last_completion_disposition: MonitorActionDisposition | None = None
    last_completed_at: float = 0.0
    token_usage_known: bool = True
    agent_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    consecutive_provider_errors: int = 0
    next_probe_at: float = 0.0
    outcome: MonitorOutcome | None = None
    stopped_reason: str = ""
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
        for name in (
            "created_ts",
            "last_observed_at",
            "last_completed_at",
            "next_probe_at",
            "stopped_at",
        ):
            value = getattr(self, name)
            if not _is_finite_non_negative_number(value):
                raise ValueError(f"{name} must be a finite non-negative number")
        for name in (
            "agent_turns",
            "input_tokens",
            "output_tokens",
            "consecutive_provider_errors",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
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
        if any(
            not isinstance(value, str)
            for value in (
                self.last_fingerprint,
                self.last_wake_fingerprint,
                self.last_completion_fingerprint,
            )
        ):
            raise ValueError("monitor fingerprints must be strings")
        if not isinstance(self.wake_in_flight, bool):
            raise ValueError("wake_in_flight must be a boolean")
        if self.last_completion_disposition is not None and not isinstance(
            self.last_completion_disposition, MonitorActionDisposition
        ):
            raise ValueError("last_completion_disposition must be a MonitorActionDisposition")
        if not isinstance(self.token_usage_known, bool):
            raise ValueError("token_usage_known must be a boolean")
        if self.outcome is not None and not isinstance(self.outcome, MonitorOutcome):
            raise ValueError("outcome must be a MonitorOutcome")
        if not isinstance(self.stopped_reason, str):
            raise ValueError("stopped_reason must be a string")
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
    return MonitorState(**values)


def quarantine_monitor_state(raw: object) -> MonitorState:
    """Build an inert inspection view while preserving strict raw JSON exactly."""
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
        _raw_payload=deepcopy(raw),
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
