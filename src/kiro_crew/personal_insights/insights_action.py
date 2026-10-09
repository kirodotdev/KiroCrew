from __future__ import annotations

import hashlib
import json
from typing import Any, Final

from kiro_crew.personal_insights import insights_verification as _verify
from kiro_crew.personal_insights.insights_canonical import canonical_lesson_digest
from kiro_crew.personal_insights.insights_placeholders import contains_placeholder
from kiro_crew.personal_insights.insights_platform import parse_cache_identity
from kiro_crew.personal_insights.insights_registry import (
    Capability,
    verification_kind_for_template,
)

ACTION_SCHEMA_VERSION: Final[str] = "kiro.personal-insights.action/3.0"

P1_APPLIES_NOTHING: Final[bool] = True

_COVERAGE_STATES: Final[frozenset[str]] = frozenset({"complete", "partial_source"})
_EVIDENCE_TIERS: Final[frozenset[str]] = frozenset({"repeated", "recurring", "usually"})
_TARGET_KINDS: Final[frozenset[str]] = frozenset(
    {"none", "lesson_scope", "workspace_file", "capability"}
)
_ARTIFACT_KINDS: Final[frozenset[str]] = frozenset(
    {"text", "structured_tool", "static_argv", "unified_diff"}
)
_VERIFICATION_KINDS: Final[frozenset[str]] = frozenset(
    {"read_only_argv", "state_readback", "future_observation"}
)
_UNDO_SCOPES: Final[frozenset[str]] = frozenset({"not_applicable", "file_state", "lesson_state"})
_RISKS: Final[frozenset[str]] = frozenset({"copy_only", "local_state_proposal"})

_CLASS_TO_TARGET_KIND: Final[dict[str, str]] = {
    "prompt": "none",
    "lesson_proposal": "lesson_scope",
    "steering_patch": "workspace_file",
    "existing_capability": "capability",
}
_CLASS_TO_UNDO_SCOPE: Final[dict[str, str]] = {
    "prompt": "not_applicable",
    "lesson_proposal": "lesson_state",
    "steering_patch": "file_state",
    "existing_capability": "not_applicable",
}


_READ_ONLY_ARGV_EXPECTED_EXIT: Final[int] = 0
_READ_ONLY_ARGV_OUTPUT_CLASS: Final[str] = "text"


class ActionValidationError(Exception):
    pass


def validate_single_artifact(artifact: dict[str, Any]) -> None:
    present = [key for key in ("content", "structured_parameters", "argv") if artifact.get(key)]
    if len(present) != 1:
        raise ActionValidationError("exactly one artifact representation is required")


def validate_no_placeholder(text: str) -> None:
    if contains_placeholder(text):
        raise ActionValidationError("artifact contains an undeclared placeholder")


def validate_exact_argv(argv: list[str], registry_tokens: tuple[str, ...]) -> None:
    if tuple(argv) != tuple(registry_tokens):
        raise ActionValidationError("argv must equal the exact registry token array")


def validate_undo(risk: str, undo_artifact: str | None) -> None:
    if risk == "local_state_proposal" and not undo_artifact:
        raise ActionValidationError("local_state_proposal requires an exact undo artifact")


def _build_oracle(
    capability: Capability,
    candidate: Any,
    *,
    structured_parameters: dict[str, Any] | None,
    state_readback_expected_digest: str | None,
    future_observation_minimum_new_sessions: int | None,
) -> tuple[str, dict[str, Any], int]:
    kind = verification_kind_for_template(capability.verification_template)
    if kind == "read_only_argv":
        argv_oracle = _verify.build_read_only_argv(
            tuple(capability.template.argv_tokens),
            _READ_ONLY_ARGV_EXPECTED_EXIT,
            _READ_ONLY_ARGV_OUTPUT_CLASS,
        )
        argv_payload = {
            "kind": argv_oracle.kind,
            "argv": list(argv_oracle.argv),
            "expected_exit": argv_oracle.expected_exit,
            "expected_output_class": argv_oracle.expected_output_class,
        }
        return kind, argv_payload, 0
    if kind == "state_readback":
        if candidate.action_class == "lesson_proposal":
            if not structured_parameters:
                raise ActionValidationError("lesson state_readback requires structured parameters")
            expected_digest = canonical_lesson_digest(structured_parameters)
        else:
            if state_readback_expected_digest is None:
                raise ActionValidationError(
                    "steering state_readback requires an explicit expected_digest"
                )
            expected_digest = state_readback_expected_digest
        target = "lesson" if candidate.action_class == "lesson_proposal" else "steering"
        state_oracle = _verify.build_state_readback(target, expected_digest)
        state_payload = {
            "kind": state_oracle.kind,
            "target_kind": state_oracle.target_kind,
            "expected_digest": state_oracle.expected_digest,
        }
        return kind, state_payload, 0
    if future_observation_minimum_new_sessions is None:
        raise ActionValidationError("future_observation requires an explicit minimum_new_sessions")
    if (
        not isinstance(future_observation_minimum_new_sessions, int)
        or isinstance(future_observation_minimum_new_sessions, bool)
        or future_observation_minimum_new_sessions < 1
    ):
        raise ActionValidationError("future_observation minimum_new_sessions must be positive")
    future_oracle = _verify.build_future_observation(
        candidate.behavior_key.canonical(), future_observation_minimum_new_sessions
    )
    future_payload = {
        "kind": future_oracle.kind,
        "behavior_key_canonical": future_oracle.behavior_key_canonical,
        "minimum_new_sessions": future_oracle.minimum_new_sessions,
    }
    return kind, future_payload, future_oracle.minimum_new_sessions


def _require(value: object, where: str) -> Any:
    if value is None or value == "":
        raise ActionValidationError(f"missing required field: {where}")
    return value


def _content_addressed_id(parts: list[str]) -> str:
    framed = "".join(f"{len(part)}:{part}\x1e" for part in parts)
    return hashlib.sha256(framed.encode("utf-8")).hexdigest()


def compile_action(
    candidate: Any,
    capability: Capability,
    *,
    rank: int,
    claim_ids: tuple[str, ...],
    claim_evidence_version_ids: tuple[str, ...] = (),
    title: str | None = None,
    why: str | None = None,
    why_claim_ids: tuple[str, ...] = (),
    why_work_area_claim_id: str | None = None,
    evidence_coverage_state: str | None = None,
    supporting_sessions: int | None = None,
    claim_eligible_sessions: int | None = None,
    counterexample_sessions: int | None = None,
    wording_tier: str | None = None,
    active_guidance_match_id: str | None = None,
    expected_observation: str | None = None,
    verification_display: str | None = None,
    content: str | None = None,
    structured_parameters: dict[str, Any] | None = None,
    undo_artifact: str | None = None,
    generated_at: str | None = None,
    expires_at: str | None = None,
    state_readback_expected_digest: str | None = None,
    future_observation_minimum_new_sessions: int | None = None,
) -> dict[str, Any]:
    _require(claim_ids, "claim_ids")
    _require(claim_evidence_version_ids, "claim_evidence_version_ids")
    _require(title, "title")
    _require(why, "why")
    _require(active_guidance_match_id, "active_guidance_match_id")
    _require(expected_observation, "expected_observation")
    _require(verification_display, "verification_display")
    _require(generated_at, "generated_at")
    _require(expires_at, "expires_at")
    if not isinstance(rank, int) or isinstance(rank, bool) or rank < 1:
        raise ActionValidationError("rank must be a positive integer")
    assert generated_at is not None and expires_at is not None
    if expires_at <= generated_at:
        raise ActionValidationError("expires_at must be after generated_at")
    if evidence_coverage_state not in _COVERAGE_STATES:
        raise ActionValidationError("invalid evidence coverage_state")
    if wording_tier not in _EVIDENCE_TIERS:
        raise ActionValidationError("invalid evidence wording_tier")
    for count in (supporting_sessions, claim_eligible_sessions, counterexample_sessions):
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ActionValidationError("evidence counts must be non-negative integers")
    assert supporting_sessions is not None and claim_eligible_sessions is not None
    if supporting_sessions > claim_eligible_sessions:
        raise ActionValidationError("supporting sessions must not exceed claim-eligible")

    action_class = candidate.action_class
    target_kind = _CLASS_TO_TARGET_KIND[action_class]
    if target_kind not in _TARGET_KINDS:
        raise ActionValidationError("invalid target kind")

    kind = capability.template.kind
    if kind not in _ARTIFACT_KINDS:
        raise ActionValidationError("invalid artifact kind")
    argv: list[str] = []
    artifact_content: str | None = None
    structured: dict[str, Any] = {}
    if kind == "static_argv":
        argv = list(capability.template.argv_tokens)
        validate_exact_argv(argv, capability.template.argv_tokens)
    elif kind in ("text", "unified_diff"):
        if content is None or content == "":
            raise ActionValidationError("text or unified_diff artifact requires content")
        artifact_content = content
        validate_no_placeholder(artifact_content)
    elif kind == "structured_tool":
        if not structured_parameters:
            raise ActionValidationError("structured_tool artifact requires structured parameters")
        structured = structured_parameters
    artifact = {
        "kind": kind,
        "content": artifact_content,
        "structured_parameters": structured,
        "argv": argv,
    }
    validate_single_artifact(artifact)

    risk = capability.risk_class
    if risk not in _RISKS:
        raise ActionValidationError("invalid risk class")
    validate_undo(risk, undo_artifact)
    undo_scope = _CLASS_TO_UNDO_SCOPE[action_class]
    if undo_scope not in _UNDO_SCOPES:
        raise ActionValidationError("invalid undo scope")

    verification_kind, oracle_payload, minimum_new_sessions = _build_oracle(
        capability,
        candidate,
        structured_parameters=structured_parameters,
        state_readback_expected_digest=state_readback_expected_digest,
        future_observation_minimum_new_sessions=future_observation_minimum_new_sessions,
    )

    execution_platform, _identity = parse_cache_identity(candidate.cache_identity)
    assert title is not None and why is not None
    action_id = _content_addressed_id(
        [candidate.action_key_value, title, why, generated_at, str(rank)]
    )
    compiled: dict[str, Any] = {
        "schema_version": ACTION_SCHEMA_VERSION,
        "action_id": action_id,
        "action_key": candidate.action_key_value,
        "claim_ids": list(claim_ids),
        "claim_evidence_version_ids": list(claim_evidence_version_ids),
        "behavior_key": {
            "subject_code": candidate.behavior_key.subject_code,
            "predicate_code": candidate.behavior_key.predicate_code,
            "polarity": candidate.behavior_key.polarity,
            "scope": candidate.behavior_key.scope,
        },
        "rank": rank,
        "title": title,
        "action_class": action_class,
        "why_claim_ids": list(why_claim_ids),
        "why_work_area_claim_id": why_work_area_claim_id,
        "why": why,
        "evidence": {
            "supporting_sessions": supporting_sessions,
            "claim_eligible_sessions": claim_eligible_sessions,
            "counterexample_sessions": counterexample_sessions,
            "coverage_state": evidence_coverage_state,
            "wording_tier": wording_tier,
        },
        "active_guidance_match_id": active_guidance_match_id,
        "capability_id": candidate.capability_id,
        "execution_platform": execution_platform,
        "target": {
            "kind": target_kind,
            "locator": None,
            "display_locator": None,
            "stable_scope": candidate.stable_target_scope,
            "exists": True,
            "base_digest": None,
        },
        "artifact": artifact,
        "expected_observation": expected_observation,
        "verification": {
            "kind": verification_kind,
            "oracle": oracle_payload,
            "display_artifact": verification_display,
            "minimum_new_sessions": minimum_new_sessions,
        },
        "undo": {
            "required": risk == "local_state_proposal",
            "scope": undo_scope,
            "artifact": undo_artifact,
        },
        "risk": risk,
        "generated_at": generated_at,
        "expires_at": expires_at,
    }
    _assert_json_serializable(compiled)
    return compiled


def _assert_json_serializable(payload: dict[str, Any]) -> None:
    json.dumps(payload, ensure_ascii=True, sort_keys=True)
