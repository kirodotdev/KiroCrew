"""Provider-neutral pull-request readiness observations."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

from kiro_crew.monitoring.models import (
    MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET,
    MAX_MONITOR_CHECK_IDENTITY_CHARS,
    MAX_MONITOR_CONDITION_KEY_CHARS,
    MAX_MONITOR_CONDITIONS,
    PULL_REQUEST_MERGEABILITY,
    PULL_REQUEST_MONITOR_KINDS,
    PULL_REQUEST_REVIEW_DECISIONS,
    PULL_REQUEST_STATES,
    MonitorCondition,
    MonitorObservation,
    MonitorObservationStatus,
    MonitorProbeResult,
    MonitorResetsOn,
    MonitorSeverity,
    ProviderErrorKind,
)
from kiro_crew.security import redact

PULL_REQUEST_CHECK_STATES = frozenset({"failed", "passed", "pending", "unknown"})
MAX_PULL_REQUEST_HEAD_REVISION_CHARS = 128

_URL_IN_CHECK_IDENTITY_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_HEAD_REVISION_RE = re.compile(rf"^[0-9a-fA-F]{{1,{MAX_PULL_REQUEST_HEAD_REVISION_CHARS}}}$")

_PROVIDER_ERROR_REASONS = {
    ProviderErrorKind.RATE_LIMITED: "provider_rate_limited",
    ProviderErrorKind.AUTHENTICATION: "provider_authentication",
    ProviderErrorKind.AUTHORIZATION: "provider_authorization",
    ProviderErrorKind.NOT_FOUND: "provider_not_found",
    ProviderErrorKind.TRANSIENT: "provider_transient",
}


def opaque_provider_check_identity(namespace: str, raw_identity: object) -> str:
    """Return a stable identity without retaining provider-controlled display text."""
    digest = hashlib.sha256(str(raw_identity).encode("utf-8")).hexdigest()[:16]
    return f"{namespace}:{digest}"


class PullRequestProviderError(Exception):
    """A provider failure carrying only its safe retry category."""

    def __init__(self, kind: ProviderErrorKind) -> None:
        super().__init__(kind.value)
        self.kind = kind


def classify_provider_error_text(raw: str) -> ProviderErrorKind:
    """Classify CLI diagnostics without retaining or returning their text."""
    lowered = raw.lower()
    if any(
        marker in lowered for marker in ("http 429", "rate limit", "too many requests", "throttled")
    ):
        return ProviderErrorKind.RATE_LIMITED
    if any(
        marker in lowered
        for marker in (
            "http 401",
            "unauthorized",
            "not logged in",
            "authentication",
            "invalid token",
            "expired token",
            "revoked token",
        )
    ):
        return ProviderErrorKind.AUTHENTICATION
    if any(marker in lowered for marker in ("http 404", "not found", "does not exist")):
        return ProviderErrorKind.NOT_FOUND
    if any(
        marker in lowered for marker in ("http 403", "forbidden", "permission", "access denied")
    ):
        return ProviderErrorKind.AUTHORIZATION
    return ProviderErrorKind.TRANSIENT


def provider_failure_result(error: PullRequestProviderError) -> PullRequestProbeResult:
    """Convert a safe typed provider failure into a generic monitor result."""
    return provider_error_result(error.kind, _PROVIDER_ERROR_REASONS[error.kind])


@dataclass(frozen=True)
class PullRequestCheck:
    """One normalized, bounded provider check."""

    identity: str
    state: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, str) or not self.identity:
            raise ValueError("check identity must be a non-empty string")
        if self.state not in PULL_REQUEST_CHECK_STATES:
            raise ValueError("check state is not supported")
        normalized = " ".join(
            "".join(
                (
                    " "
                    if unicodedata.category(character).startswith("C")
                    or unicodedata.category(character) in {"Zl", "Zp"}
                    else character
                )
                for character in self.identity
            ).split()
        )
        identity = redact(_URL_IN_CHECK_IDENTITY_RE.sub("[provider-url]", normalized))
        if not identity:
            raise ValueError("check identity must remain non-empty after redaction")
        if len(identity) > MAX_MONITOR_CHECK_IDENTITY_CHARS:
            digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
            prefix_length = MAX_MONITOR_CHECK_IDENTITY_CHARS - len(digest) - 1
            identity = f"{identity[:prefix_length]}#{digest}"
        object.__setattr__(self, "identity", identity)


@dataclass(frozen=True)
class PullRequestFacts:
    """Provider-native state normalized into the shared readiness vocabulary."""

    kind: str
    target: str
    state: str
    draft: bool
    head_revision: str
    mergeability: str
    review_decision: str
    checks: tuple[PullRequestCheck, ...]
    unresolved_review_threads: int
    review_threads_complete: bool
    checks_complete: bool = True

    def __post_init__(self) -> None:
        if self.kind not in PULL_REQUEST_MONITOR_KINDS:
            raise ValueError("kind is not a supported pull-request monitor")
        if not isinstance(self.target, str) or not self.target:
            raise ValueError("target must be a non-empty string")
        if self.state not in PULL_REQUEST_STATES:
            raise ValueError("state is not supported")
        if not isinstance(self.draft, bool):
            raise ValueError("draft must be a boolean")
        if not isinstance(self.head_revision, str) or (
            self.head_revision and _HEAD_REVISION_RE.fullmatch(self.head_revision) is None
        ):
            raise ValueError("head_revision must be bounded hexadecimal text")
        if self.mergeability not in PULL_REQUEST_MERGEABILITY:
            raise ValueError("mergeability is not supported")
        if self.review_decision not in PULL_REQUEST_REVIEW_DECISIONS:
            raise ValueError("review_decision is not supported")
        if not isinstance(self.checks, tuple) or any(
            not isinstance(check, PullRequestCheck) for check in self.checks
        ):
            raise ValueError("checks must be normalized pull-request checks")
        if (
            isinstance(self.unresolved_review_threads, bool)
            or not isinstance(self.unresolved_review_threads, int)
            or self.unresolved_review_threads < 0
        ):
            raise ValueError("unresolved_review_threads must be a non-negative integer")
        if not isinstance(self.review_threads_complete, bool):
            raise ValueError("review_threads_complete must be a boolean")
        if not isinstance(self.checks_complete, bool):
            raise ValueError("checks_complete must be a boolean")


@dataclass(frozen=True)
class PullRequestProbeResult(MonitorProbeResult):
    """Canonical facts and their generic monitor classification."""

    response: object | None
    canonical: dict[str, object]
    observation: MonitorObservation


def build_pull_request_probe_result(
    facts: PullRequestFacts,
    *,
    previous_observation: Mapping[str, object] | None = None,
    response: object | None = None,
    supplemental_provider_error: ProviderErrorKind | None = None,
) -> PullRequestProbeResult:
    """Build the shared canonical snapshot, fingerprint, and classification."""
    canonical = canonical_pull_request_facts(facts)
    status, reason_code = classify_pull_request_facts(facts)
    fingerprint_facts = (
        actionable_fingerprint_facts(canonical)
        if status is MonitorObservationStatus.ACTIONABLE
        else canonical
    )
    previous_head = (
        previous_observation.get("head_revision")
        if isinstance(previous_observation, Mapping)
        else None
    )
    head_changed = (
        facts.state == "open"
        and isinstance(previous_head, str)
        and bool(previous_head)
        and bool(facts.head_revision)
        and previous_head != facts.head_revision
    )
    return PullRequestProbeResult(
        response=facts if response is None else response,
        canonical=canonical,
        observation=MonitorObservation(
            fingerprint_pull_request_facts(fingerprint_facts),
            status,
            supplemental_provider_error=supplemental_provider_error,
            reason_code=reason_code,
            head_changed=head_changed,
            # Conditions are carried only for an ACTIONABLE subject, because the
            # coalescing window is the only thing that reads them and nothing
            # else is put through it. A PENDING subject naming conditions would
            # be state with no reader, which is the shape that rots.
            conditions=(
                pull_request_conditions(canonical)
                if status is MonitorObservationStatus.ACTIONABLE
                else ()
            ),
        ),
    )


def provider_error_result(
    kind: ProviderErrorKind,
    reason_code: str,
) -> PullRequestProbeResult:
    """Return a provider-neutral error result with no durable raw payload."""
    return PullRequestProbeResult(
        response=None,
        canonical={},
        observation=MonitorObservation(
            "",
            MonitorObservationStatus.PROVIDER_ERROR,
            provider_error=kind,
            reason_code=reason_code,
        ),
    )


def canonical_pull_request_facts(facts: PullRequestFacts) -> dict[str, object]:
    """Project one exact bounded canonical fact object."""
    buckets = {
        state: sorted(check.identity for check in facts.checks if check.state == state)
        for state in ("failed", "passed", "pending", "unknown")
    }
    overflow = not facts.checks_complete or any(
        len(values) > MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET for values in buckets.values()
    )
    checks = {
        state: values[:MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET] for state, values in buckets.items()
    }
    if overflow:
        checks["unknown"] = [
            *checks["unknown"][: MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET - 1],
            "checks:incomplete",
        ]
    if facts.review_decision == "changes_requested":
        blocking_review = "changes_requested"
    elif facts.unresolved_review_threads:
        blocking_review = "unresolved_threads"
    elif not facts.review_threads_complete:
        blocking_review = "unknown"
    else:
        blocking_review = "none"
    return {
        "blocking_review": blocking_review,
        "checks": checks,
        "checks_complete": not overflow,
        "draft": facts.draft,
        "head_revision": facts.head_revision,
        "kind": facts.kind,
        "mergeability": facts.mergeability,
        "review_decision": facts.review_decision,
        "review_threads_complete": facts.review_threads_complete,
        "state": facts.state,
        "target": facts.target,
        "unresolved_review_threads": facts.unresolved_review_threads,
    }


def actionable_fingerprint_facts(canonical: Mapping[str, object]) -> dict[str, object]:
    """Keep known blockers stable while unrelated unsettled facts churn."""
    checks = canonical.get("checks")
    if not isinstance(checks, Mapping):
        raise ValueError("canonical pull-request checks are malformed")
    blocking_review = canonical.get("blocking_review")
    mergeability = canonical.get("mergeability")
    return {
        "blocking_review": (
            blocking_review
            if blocking_review in {"changes_requested", "unresolved_threads"}
            else "none"
        ),
        "failed_checks": checks.get("failed"),
        "checks_complete": canonical.get("checks_complete"),
        "head_revision": canonical.get("head_revision"),
        "kind": canonical.get("kind"),
        "mergeability": (mergeability if mergeability in {"conflicting", "behind"} else "none"),
        "review_threads_complete": canonical.get("review_threads_complete"),
        "state": canonical.get("state"),
        "target": canonical.get("target"),
        "unresolved_review_threads": canonical.get("unresolved_review_threads"),
    }


def _check_condition_key(identity: str) -> str:
    """The dedupe key for one failing check, kept distinct under the length bound.

    Truncating to the bound is what a key must never do on its own: two check
    identities sharing a long prefix -- the shape a matrix job produces, where the
    varying part is the SUFFIX -- collapse to one key, and one key is one
    condition, so the second failure is masked and aged as the first and is never
    reported. Appending a digest of the whole identity keeps the key inside the
    bound while preserving what the bound would otherwise erase.
    """
    key = f"red:{identity}"
    if len(key) <= MAX_MONITOR_CONDITION_KEY_CHARS:
        return key
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{key[: MAX_MONITOR_CONDITION_KEY_CHARS - len(digest) - 1]}-{digest}"


def pull_request_conditions(canonical: Mapping[str, object]) -> tuple[MonitorCondition, ...]:
    """Name every actionable condition a pull request is carrying at once.

    One base derivation for all four pull-request kinds: every adapter funnels
    through :func:`build_pull_request_probe_result`, so the conditions come from
    the canonical facts rather than from any provider's response shape. An
    adapter whose evidence streams must stay in separate namespaces keeps them
    apart by CHECK IDENTITY -- the identity a provider puts in ``checks`` is what
    ends up inside ``red:<identity>`` -- so nothing here merges two streams that
    the provider kept apart.

    This is where the representation defect is repaired. ``blocking_review`` is a
    single precedence winner, so a pull request carrying BOTH
    ``changes_requested`` and unresolved review threads records only the first
    and the second is lost before anything can act on it. Here they are two
    conditions, each masked, aged and reset on its own, and both survive.

    Both are ``NEVER``: a review verdict and a review thread belong to the
    conversation, not to the commit under review, so a force-push must not
    replay them. A failing check is the opposite -- it is a property of the
    revision that dispatched it, so a new head genuinely clears it.

    ``conflict`` is the ``IMMEDIATE`` one. A conflicted pull request dispatches
    no checks, so the pending count the coalescing floor waits on never drains,
    and holding the wake would strand the owner for the whole floor on a signal
    that is already actionable. ``behind`` is not urgent in that way: the branch
    still builds, so waiting continues to observe something.

    Conditions are capped, and the cap is on the CHECK expansion alone because it
    is the only unbounded one: the canonical projection already bounds each check
    bucket, and the review conditions are three fixed keys.
    """
    checks = canonical.get("checks")
    if not isinstance(checks, Mapping):
        raise ValueError("canonical pull-request checks are malformed")
    conditions: list[MonitorCondition] = []
    failed = checks.get("failed")
    if isinstance(failed, (list, tuple)):
        for identity in list(failed)[:MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET]:
            if isinstance(identity, str) and identity:
                conditions.append(
                    MonitorCondition(
                        key=_check_condition_key(identity),
                        severity=MonitorSeverity.WAKE,
                        brief=f"check failed: {identity}",
                        resets_on=MonitorResetsOn.REVISION,
                    )
                )
    if canonical.get("review_decision") == "changes_requested":
        conditions.append(
            MonitorCondition(
                key="changes_requested",
                severity=MonitorSeverity.WAKE,
                brief="a reviewer requested changes",
                resets_on=MonitorResetsOn.NEVER,
            )
        )
    unresolved = canonical.get("unresolved_review_threads")
    if isinstance(unresolved, int) and not isinstance(unresolved, bool) and unresolved > 0:
        conditions.append(
            MonitorCondition(
                key="unresolved_threads",
                severity=MonitorSeverity.WAKE,
                brief=f"{unresolved} unresolved review threads",
                resets_on=MonitorResetsOn.NEVER,
            )
        )
    mergeability = canonical.get("mergeability")
    if mergeability == "conflicting":
        conditions.append(
            MonitorCondition(
                key="conflict",
                severity=MonitorSeverity.IMMEDIATE,
                brief="the branch conflicts with its target",
                resets_on=MonitorResetsOn.REVISION,
            )
        )
    elif mergeability == "behind":
        conditions.append(
            MonitorCondition(
                key="behind",
                severity=MonitorSeverity.WAKE,
                brief="the branch is behind its target",
                resets_on=MonitorResetsOn.REVISION,
            )
        )
    # Deduplicate by key while keeping order: a provider is free to report two
    # checks under one identity, and two conditions under one key is one
    # condition the engine would mask and age twice.
    seen: set[str] = set()
    unique: list[MonitorCondition] = []
    for condition in conditions:
        if condition.key in seen:
            continue
        seen.add(condition.key)
        unique.append(condition)
    return tuple(unique[:MAX_MONITOR_CONDITIONS])


def classify_pull_request_facts(
    facts: PullRequestFacts,
) -> tuple[MonitorObservationStatus, str]:
    """Apply the one cross-provider review-readiness precedence."""
    if facts.state == "merged":
        return MonitorObservationStatus.SUCCESS, "pull_request_merged"
    if facts.state == "closed":
        return MonitorObservationStatus.BLOCKED, "pull_request_closed"
    if facts.state != "open" or not facts.head_revision:
        return MonitorObservationStatus.PENDING, "pull_request_state_unknown"
    if facts.draft:
        return MonitorObservationStatus.PENDING, "pull_request_draft"
    check_states = {check.state for check in facts.checks}
    if "failed" in check_states:
        return MonitorObservationStatus.ACTIONABLE, "checks_failed"
    if facts.review_decision == "changes_requested":
        return MonitorObservationStatus.ACTIONABLE, "changes_requested"
    if facts.unresolved_review_threads:
        return MonitorObservationStatus.ACTIONABLE, "unresolved_review_threads"
    if facts.mergeability == "conflicting":
        return MonitorObservationStatus.ACTIONABLE, "merge_conflict"
    if facts.mergeability == "behind":
        return MonitorObservationStatus.ACTIONABLE, "branch_behind"
    if not facts.checks_complete:
        return MonitorObservationStatus.PENDING, "checks_incomplete"
    if "pending" in check_states:
        return MonitorObservationStatus.PENDING, "checks_pending"
    if "unknown" in check_states:
        return MonitorObservationStatus.PENDING, "checks_unknown"
    if not facts.review_threads_complete:
        return MonitorObservationStatus.PENDING, "review_threads_incomplete"
    if facts.review_decision == "unknown":
        return MonitorObservationStatus.PENDING, "review_state_unknown"
    if facts.review_decision == "review_required":
        return MonitorObservationStatus.PENDING, "review_required"
    if facts.mergeability in {"pending", "blocked"}:
        return MonitorObservationStatus.PENDING, "mergeability_pending"
    return MonitorObservationStatus.SUCCESS, "review_ready"


def fingerprint_pull_request_facts(canonical: Mapping[str, object]) -> str:
    """Hash the stable canonical JSON representation."""
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
