from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from kiro_crew.personal_insights.insights_ontology import BehaviorKey
from kiro_crew.personal_insights.insights_platform import (
    RuntimePlatform,
    compiled_candidate_cache_identity,
    reject_client_platform,
)
from kiro_crew.personal_insights.insights_registry import (
    CLASS_LESSON,
    CLASS_PROMPT,
    CLASS_STEERING,
    Capability,
    Registry,
    behavior_key_matches,
)

MINIMUM_SUPPORTING_SESSIONS: Final[int] = 2
MAX_ROOTS: Final[int] = 3

TIER_SINGLE: Final[str] = "single"
TIER_REPEATED: Final[str] = "repeated"
TIER_RECURRING: Final[str] = "recurring"
TIER_USUALLY: Final[str] = "usually"
VALID_TIERS: Final[frozenset[str]] = frozenset({TIER_REPEATED, TIER_RECURRING, TIER_USUALLY})

SCOPE_NONE: Final[str] = "none"
SCOPE_CAPABILITY: Final[str] = "capability"
NO_REPOSITORY_SCOPE: Final[str] = "no_repository_scope"

REJECT_SINGLE_TIER: Final[str] = "single_tier_rejected"
REJECT_SUPPORT_FLOOR: Final[str] = "below_two_supporting_sessions"
REJECT_PREREQUISITE: Final[str] = "prerequisite_failed"
REJECT_BASE_DIGEST: Final[str] = "base_digest_failed"
REJECT_PLATFORM: Final[str] = "platform_unsupported"
REJECT_NOT_ACCEPTED: Final[str] = "evidence_not_accepted"
REJECT_GUIDANCE: Final[str] = "guidance_not_cleared"
REJECT_CONFLICT: Final[str] = "conflicts_with_disposition"
REJECT_INVALID_COUNTS: Final[str] = "invalid_session_counts"
REJECT_INVALID_EVIDENCE: Final[str] = "invalid_claim_evidence"

_EFFORT_ORDER: Final[dict[str, int]] = {"low": 0, "medium": 1, "high": 2}
_REVERSIBILITY_ORDER: Final[dict[str, int]] = {
    "fully_reversible": 0,
    "reversible_with_undo": 1,
    "irreversible": 2,
}
_LATENCY_ORDER: Final[dict[str, int]] = {"immediate": 0, "deferred": 1, "manual": 2}

RANKING_LEVELS: Final[tuple[str, ...]] = (
    "verified_consequence",
    "supporting_session_count",
    "burden_vector",
    "implementation_effort",
    "reversibility",
    "verification_latency",
    "selection_priority",
    "capability_id",
    "canonical_tuple",
)

_CONTROL_DELIMITERS: Final[tuple[str, ...]] = ("\x1e", "\x1f", "\x00", "\n", "\r", "\t")


def reversibility_rank(reversibility: str) -> int:
    return _REVERSIBILITY_ORDER[reversibility]


class ModelError(Exception):
    pass


@dataclass(frozen=True)
class BurdenVector:
    verification_failures: int = 0
    corrections: int = 0
    tool_errors: int = 0
    interruptions: int = 0
    repeated_operations: int = 0

    def as_tuple(self) -> tuple[int, int, int, int, int]:
        return (
            self.verification_failures,
            self.corrections,
            self.tool_errors,
            self.interruptions,
            self.repeated_operations,
        )


@dataclass(frozen=True)
class ClaimEvidence:
    behavior_key: BehaviorKey
    supporting_session_count: int
    claim_eligible_session_count: int
    wording_tier: str
    verified_consequence: bool
    burden: BurdenVector
    repository_scope: str | None = None
    base_digest: str | None = None
    workspace_id: str | None = None
    display_locator: str | None = None
    accepted: bool = True
    guidance_cleared: bool = True
    conflicts_with_disposition: bool = False


def validate_claim_evidence(evidence: ClaimEvidence) -> None:
    if evidence.supporting_session_count < 0 or evidence.claim_eligible_session_count < 0:
        raise ModelError("session counts must be non-negative")
    if evidence.supporting_session_count > evidence.claim_eligible_session_count:
        raise ModelError("supporting sessions must not exceed claim-eligible sessions")
    if evidence.wording_tier not in VALID_TIERS:
        raise ModelError("wording tier must be a valid non-single tier")
    for value in evidence.burden.as_tuple():
        if value < 0:
            raise ModelError("burden counts must be non-negative")
    if not evidence.accepted:
        raise ModelError("candidate requires accepted claim evidence")
    if not evidence.guidance_cleared:
        raise ModelError("candidate requires phase-one guidance clearance")
    if evidence.conflicts_with_disposition:
        raise ModelError("candidate conflicts with an accepted or deferred action")


def validate_steering_target(evidence: ClaimEvidence) -> None:
    if not evidence.workspace_id:
        raise ModelError("steering target requires a non-empty stable workspace id")
    locator = evidence.display_locator or ""
    if not locator:
        raise ModelError("steering target requires a normalized display locator")
    if locator.startswith("/") or ".." in locator.split("/"):
        raise ModelError("steering display locator must be normalized workspace-relative")


def _evidence_rejection(evidence: ClaimEvidence) -> str | None:
    if evidence.supporting_session_count < 0 or evidence.claim_eligible_session_count < 0:
        return REJECT_INVALID_COUNTS
    if evidence.supporting_session_count > evidence.claim_eligible_session_count:
        return REJECT_INVALID_COUNTS
    if any(value < 0 for value in evidence.burden.as_tuple()):
        return REJECT_INVALID_COUNTS
    if evidence.wording_tier == TIER_SINGLE or evidence.wording_tier not in VALID_TIERS:
        return REJECT_SINGLE_TIER
    if not evidence.accepted:
        return REJECT_NOT_ACCEPTED
    if not evidence.guidance_cleared:
        return REJECT_GUIDANCE
    if evidence.conflicts_with_disposition:
        return REJECT_CONFLICT
    if evidence.claim_eligible_session_count < MINIMUM_SUPPORTING_SESSIONS:
        return REJECT_SUPPORT_FLOOR
    return None


@dataclass(frozen=True)
class Candidate:
    behavior_key: BehaviorKey
    capability_id: str
    action_class: str
    stable_target_scope: str
    selection_priority: int
    implementation_effort: str
    reversibility: str
    verification_latency: str
    supporting_session_count: int
    verified_consequence: bool
    burden: BurdenVector
    cache_identity: str
    action_key_value: str


@dataclass(frozen=True)
class Rejected:
    behavior_key: BehaviorKey
    capability_id: str
    reason: str


@dataclass(frozen=True)
class EnumerationResult:
    candidates: tuple[Candidate, ...]
    rejected: tuple[Rejected, ...]
    roots: tuple[Candidate, ...]


def _reject_control_delimiters(value: str, where: str) -> str:
    for delimiter in _CONTROL_DELIMITERS:
        if delimiter in value:
            raise ModelError(f"{where} must not contain control or delimiter characters")
    return value


def _normalize_repo_scope(repo_scope: str | None) -> str:
    if repo_scope is None:
        return NO_REPOSITORY_SCOPE
    _reject_control_delimiters(repo_scope, "repository scope")
    if repo_scope.startswith("/"):
        raise ModelError("repository scope must not be absolute")
    if ".." in repo_scope.split("/"):
        raise ModelError("repository scope must not contain traversal")
    return repo_scope


def stable_target_scope(capability: Capability, evidence: ClaimEvidence) -> str:
    cls = capability.cls
    if cls == CLASS_PROMPT:
        return SCOPE_NONE
    if cls not in (CLASS_LESSON, CLASS_STEERING):
        return SCOPE_CAPABILITY
    if cls == CLASS_LESSON:
        base = _reject_control_delimiters(evidence.behavior_key.scope, "lesson scope")
        repo = _normalize_repo_scope(evidence.repository_scope)
        return f"lesson_scope\x1f{base}\x1f{repo}"
    workspace = _reject_control_delimiters(evidence.workspace_id or "", "workspace id")
    locator = _reject_control_delimiters(evidence.display_locator or "", "display locator")
    if locator.startswith("/"):
        raise ModelError("display locator must be workspace-relative")
    if ".." in locator.split("/"):
        raise ModelError("display locator must not contain traversal")
    return f"workspace_file\x1f{workspace}\x1f{locator}"


def action_key(
    behavior_key: BehaviorKey,
    capability_id: str,
    action_class: str,
    stable_scope: str,
) -> str:
    parts = [behavior_key.canonical(), capability_id, action_class, stable_scope]
    framed = "".join(f"{len(part)}:{part}\x1e" for part in parts)
    return hashlib.sha256(framed.encode("utf-8")).hexdigest()


def _check_prerequisites(capability: Capability, evidence: ClaimEvidence) -> str | None:
    for prerequisite in capability.prerequisites:
        if prerequisite == "repository_scope" and not evidence.repository_scope:
            return REJECT_PREREQUISITE
        if prerequisite == "base_digest" and not evidence.base_digest:
            return REJECT_BASE_DIGEST
    return None


def _dedup(candidates: list[Candidate]) -> list[Candidate]:
    seen: dict[tuple[str, str, str, str], Candidate] = {}
    for candidate in candidates:
        tuple_key = (
            candidate.behavior_key.canonical(),
            candidate.capability_id,
            candidate.action_class,
            candidate.stable_target_scope,
        )
        existing = seen.get(tuple_key)
        if existing is None or _dedup_preference(candidate, existing):
            seen[tuple_key] = candidate
    return [seen[key] for key in sorted(seen)]


def _dedup_preference(candidate: Candidate, existing: Candidate) -> bool:
    if candidate.supporting_session_count != existing.supporting_session_count:
        return candidate.supporting_session_count > existing.supporting_session_count
    return candidate.action_key_value < existing.action_key_value


def _rank_key(candidate: Candidate) -> tuple:
    return (
        0 if candidate.verified_consequence else 1,
        -candidate.supporting_session_count,
        tuple(-value for value in candidate.burden.as_tuple()),
        _EFFORT_ORDER[candidate.implementation_effort],
        _REVERSIBILITY_ORDER[candidate.reversibility],
        _LATENCY_ORDER[candidate.verification_latency],
        candidate.selection_priority,
        candidate.capability_id,
        (
            candidate.behavior_key.canonical(),
            candidate.capability_id,
            candidate.action_class,
            candidate.stable_target_scope,
        ),
    )


def enumerate_candidates(
    registry: Registry,
    evidences: list[ClaimEvidence],
    runtime: RuntimePlatform,
    request_fields: dict[str, object] | None = None,
) -> EnumerationResult:
    reject_client_platform(request_fields or {})
    candidates: list[Candidate] = []
    rejected: list[Rejected] = []
    for evidence in evidences:
        for capability in registry.capabilities:
            if not behavior_key_matches(capability, evidence.behavior_key):
                continue
            evidence_reason = _evidence_rejection(evidence)
            if evidence_reason is not None:
                rejected.append(
                    Rejected(evidence.behavior_key, capability.capability_id, evidence_reason)
                )
                continue
            if runtime.platform not in capability.supported_platforms:
                rejected.append(
                    Rejected(evidence.behavior_key, capability.capability_id, REJECT_PLATFORM)
                )
                continue
            prerequisite_reason = _check_prerequisites(capability, evidence)
            if prerequisite_reason is not None:
                rejected.append(
                    Rejected(evidence.behavior_key, capability.capability_id, prerequisite_reason)
                )
                continue
            scope = stable_target_scope(capability, evidence)
            candidates.append(
                Candidate(
                    behavior_key=evidence.behavior_key,
                    capability_id=capability.capability_id,
                    action_class=capability.cls,
                    stable_target_scope=scope,
                    selection_priority=capability.selection_priority,
                    implementation_effort=capability.implementation_effort,
                    reversibility=capability.reversibility,
                    verification_latency=capability.verification_latency,
                    supporting_session_count=evidence.supporting_session_count,
                    verified_consequence=evidence.verified_consequence,
                    burden=evidence.burden,
                    cache_identity=compiled_candidate_cache_identity(
                        runtime, capability.capability_id
                    ),
                    action_key_value=action_key(
                        evidence.behavior_key, capability.capability_id, capability.cls, scope
                    ),
                )
            )
    deduped = _dedup(candidates)
    deduped.sort(key=_rank_key)
    roots = _select_roots(deduped)
    return EnumerationResult(
        candidates=tuple(deduped), rejected=tuple(rejected), roots=tuple(roots)
    )


def _select_roots(sorted_candidates: list[Candidate]) -> list[Candidate]:
    queues: dict[str, list[Candidate]] = {}
    order: list[str] = []
    for candidate in sorted_candidates:
        key = candidate.behavior_key.canonical()
        if key not in queues:
            queues[key] = []
            order.append(key)
        queues[key].append(candidate)
    heads = [queues[key][0] for key in order]
    heads.sort(key=_rank_key)
    return heads[:MAX_ROOTS]


def same_behavior_fallback(
    result: EnumerationResult, behavior_key: BehaviorKey, failed_capability_ids: set[str]
) -> Candidate | None:
    key = behavior_key.canonical()
    for candidate in result.candidates:
        if candidate.behavior_key.canonical() != key:
            continue
        if candidate.capability_id in failed_capability_ids:
            continue
        return candidate
    return None


@dataclass(frozen=True)
class BehaviorQueue:
    candidates: tuple[Candidate, ...]
    wording_attempt_ceiling: int

    @property
    def behavior_canonical(self) -> str:
        if not self.candidates:
            return ""
        return self.candidates[0].behavior_key.canonical()

    def attempts(self) -> list[Candidate]:
        if self.wording_attempt_ceiling < 0:
            raise ModelError("wording attempt ceiling must not be negative")
        return list(self.candidates[: self.wording_attempt_ceiling])

    def is_exhausted(self, consumed: int) -> bool:
        return consumed >= min(len(self.candidates), self.wording_attempt_ceiling)


def select_roots_with_queues(
    result: EnumerationResult, wording_attempt_ceiling: int
) -> list[BehaviorQueue]:
    queues: dict[str, list[Candidate]] = {}
    order: list[str] = []
    for candidate in result.candidates:
        key = candidate.behavior_key.canonical()
        if key not in queues:
            queues[key] = []
            order.append(key)
        queues[key].append(candidate)
    root_order = [root.behavior_key.canonical() for root in result.roots]
    ordered_keys = root_order + [key for key in order if key not in root_order]
    return [
        BehaviorQueue(
            candidates=tuple(queues[key]),
            wording_attempt_ceiling=wording_attempt_ceiling,
        )
        for key in ordered_keys
    ]


def _behavior_order(result: EnumerationResult) -> tuple[list[str], dict[str, list[Candidate]]]:
    queues_by_key: dict[str, list[Candidate]] = {}
    order: list[str] = []
    for candidate in result.candidates:
        key = candidate.behavior_key.canonical()
        if key not in queues_by_key:
            queues_by_key[key] = []
            order.append(key)
        queues_by_key[key].append(candidate)
    root_order = [root.behavior_key.canonical() for root in result.roots]
    behavior_order = root_order + [key for key in order if key not in root_order]
    return behavior_order, queues_by_key


@dataclass(frozen=True)
class WordingPlan:
    queues: tuple[BehaviorQueue, ...]
    selected: tuple[Candidate, ...]
    total_attempts_used: int


def plan_wording(result: EnumerationResult, total_wording_attempt_ceiling: int) -> WordingPlan:
    return execute_post_wording(
        result, total_wording_attempt_ceiling, validator=lambda candidate: True
    )


def execute_post_wording(
    result: EnumerationResult,
    total_wording_attempt_ceiling: int,
    validator: "Callable[[Candidate], bool]",
) -> WordingPlan:
    if total_wording_attempt_ceiling < 0:
        raise ModelError("total wording attempt ceiling must not be negative")
    behavior_order, queues_by_key = _behavior_order(result)
    all_queues = tuple(
        BehaviorQueue(
            candidates=tuple(queues_by_key[key]),
            wording_attempt_ceiling=total_wording_attempt_ceiling,
        )
        for key in behavior_order
    )
    selected: list[Candidate] = []
    attempts_used = 0
    for key in behavior_order:
        if len(selected) >= MAX_ROOTS:
            break
        if attempts_used >= total_wording_attempt_ceiling:
            break
        for candidate in queues_by_key[key]:
            if attempts_used >= total_wording_attempt_ceiling:
                break
            attempts_used += 1
            if validator(candidate):
                selected.append(candidate)
                break
    return WordingPlan(
        queues=all_queues,
        selected=tuple(selected[:MAX_ROOTS]),
        total_attempts_used=attempts_used,
    )
