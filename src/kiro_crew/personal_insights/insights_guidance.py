from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from kiro_crew.personal_insights.insights_canonical import (
    bound_source_digest as _bound_source_digest,
)
from kiro_crew.personal_insights.insights_canonical import (
    canonical_argv_digest,
    canonical_lesson_digest,
    canonical_steering_digest,
    canonical_text_digest,
    source_content_digest,
)
from kiro_crew.personal_insights.insights_ontology import (
    PREDICATE_CODES,
    BehaviorKey,
    OntologyError,
    is_known_predicate,
)

ACTIVE_GUIDANCE_MATCH_SCHEMA: Final[str] = "kiro.personal-insights.active-guidance-match/1.0"


class GuidanceError(Exception):
    pass


TAG_PREFIX: Final[str] = "kiro-insights-behavior: "

LEGACY_LEXICON_VERSION: Final[str] = "1.0"

PHASE_BEHAVIOR: Final[str] = "behavior"
PHASE_ARTIFACT: Final[str] = "artifact"

MATCH_TAGGED: Final[str] = "tagged_behavior_match"
MATCH_LEGACY: Final[str] = "legacy_lexicon_match"
MATCH_AMBIGUOUS: Final[str] = "ambiguous_relevant_overlap"
MATCH_NONE: Final[str] = "no_detected_overlap"
MATCH_EXACT_ARTIFACT: Final[str] = "exact_artifact_match"

_LEGACY_LEXICON: Final[dict[str, tuple[str, ...]]] = {
    "repeats_context_setup": ("front-load the recurring context", "restate the project context"),
    "verification_omission_explicit": ("without verifying", "claim without checking"),
    "retries_operation": ("retry the same operation", "rerun the same command"),
    "uses_parallel_delegation": ("fan out in parallel", "parallel subagents"),
    "encounters_tool_errors": ("tool error recovery", "handle the tool error"),
    "interrupts_long_steps": ("interrupt the long step", "cancel the long running step"),
    "reuses_effective_prompt": ("reuse the effective prompt", "save the working prompt"),
    "needs_capability_discovery": ("discover available capability", "list what is available"),
    "preserves_reversible_changes": ("keep the change reversible", "preserve an undo path"),
}


@dataclass(frozen=True)
class GuidanceDocument:
    source_id: str
    text: str
    kind: str


@dataclass(frozen=True)
class PhaseOneMatch:
    match_state: str
    predicate_code: str | None
    source_digest: str
    scope: str = "global"


@dataclass(frozen=True)
class ActiveGuidanceMatchRecord:
    schema_version: str
    snapshot_digest: str
    source_digests: tuple[str, ...]
    behavior_key: dict[str, str]
    artifact_digest: str | None
    phase: str
    match_state: str
    captured_at: str


def _extract_tag_codes(text: str) -> tuple[list[str], bool]:
    codes: list[str] = []
    malformed = False
    marker = "kiro-insights-behavior:"
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\n")
        if marker not in line:
            continue
        if not line.startswith(marker):
            malformed = True
            continue
        if not line.startswith(TAG_PREFIX):
            malformed = True
            continue
        value = line[len(TAG_PREFIX) :]
        if value == "" or value != value.strip() or " " in value:
            malformed = True
        else:
            codes.append(value)
    return codes, malformed


def classify_phase_one(source_id: str, text: str, scope: str = "global") -> PhaseOneMatch:
    digest = source_content_digest(text)
    codes, malformed = _extract_tag_codes(text)
    if codes or malformed:
        distinct = set(codes)
        if malformed or len(distinct) != 1:
            return PhaseOneMatch(MATCH_AMBIGUOUS, None, digest, scope)
        only = next(iter(distinct))
        if not is_known_predicate(only):
            return PhaseOneMatch(MATCH_AMBIGUOUS, None, digest, scope)
        return PhaseOneMatch(MATCH_TAGGED, only, digest, scope)
    hits: set[str] = set()
    lowered = text.lower()
    for code in sorted(PREDICATE_CODES):
        for phrase in _LEGACY_LEXICON.get(code, ()):
            if phrase in lowered:
                hits.add(code)
                break
    if len(hits) == 1:
        return PhaseOneMatch(MATCH_LEGACY, next(iter(hits)), digest, scope)
    if len(hits) > 1:
        return PhaseOneMatch(MATCH_AMBIGUOUS, None, digest, scope)
    return PhaseOneMatch(MATCH_NONE, None, digest, scope)


def phase_one_blocks_enumeration(match: PhaseOneMatch) -> bool:
    return match.match_state in (MATCH_TAGGED, MATCH_LEGACY)


def phase_one_blocks_behavior(match: PhaseOneMatch, behavior_key: BehaviorKey) -> bool:
    if match.match_state not in (MATCH_TAGGED, MATCH_LEGACY):
        return False
    if match.predicate_code != behavior_key.predicate_code:
        return False
    if match.scope == "global":
        return True
    return match.scope == behavior_key.scope


def reinforcement_from_match(match: PhaseOneMatch, behavior_key: BehaviorKey) -> dict[str, object]:
    return {
        "predicate_code": behavior_key.predicate_code,
        "scope": behavior_key.scope,
        "match_state": match.match_state,
        "source_digest": match.source_digest,
        "reinforced": True,
    }


def phase_two_matches(artifact_kind: str, compiled: object, active_guidance: list) -> bool:
    if artifact_kind in ("steering_patch", "unified_diff"):
        target = canonical_steering_digest(str(compiled))
        return any(canonical_steering_digest(str(body)) == target for body in active_guidance)
    if artifact_kind in ("lesson_proposal", "structured_tool"):
        target = canonical_lesson_digest(_as_lesson_fields(compiled))
        return any(
            canonical_lesson_digest(_as_lesson_fields(body)) == target for body in active_guidance
        )
    if artifact_kind in ("static_argv",):
        target = canonical_argv_digest(_as_argv(compiled))
        return any(canonical_argv_digest(_as_argv(body)) == target for body in active_guidance)
    target = canonical_text_digest(str(compiled))
    return any(canonical_text_digest(str(body)) == target for body in active_guidance)


def _as_lesson_fields(value: object) -> dict:
    if isinstance(value, dict):
        return value
    raise GuidanceError("structured lesson comparison requires a mapping")


def _as_argv(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)) and all(isinstance(token, str) for token in value):
        return tuple(value)
    raise GuidanceError("static argv comparison requires a sequence of strings")


def phase_two_exact_artifact_match(
    compiled_artifact: str, active_guidance_bodies: list[str], artifact_kind: str
) -> bool:
    return phase_two_matches(artifact_kind, compiled_artifact, list(active_guidance_bodies))


def bound_source_digest(kind: str, scope: str, content: str) -> str:
    return _bound_source_digest(kind, scope, content)


def guidance_source_digests(documents: list[GuidanceDocument]) -> tuple[str, ...]:
    return tuple(source_content_digest(document.text) for document in documents)


def replay_requires_rematch(
    previous_digests: tuple[str, ...], documents: list[GuidanceDocument]
) -> bool:
    return tuple(previous_digests) != guidance_source_digests(documents)


def guidance_bound_digests(documents: list[GuidanceDocument], scope: str) -> tuple[str, ...]:
    return tuple(
        _bound_source_digest(document.kind, scope, document.text) for document in documents
    )


@dataclass(frozen=True)
class CompiledCandidate:
    behavior_key: BehaviorKey
    artifact_kind: str
    compiled: object


@dataclass(frozen=True)
class PhaseTwoResult:
    behavior_key: BehaviorKey
    artifact_kind: str
    matched: bool


@dataclass(frozen=True)
class ReplayOutcome:
    changed: bool
    phase_one: tuple[PhaseOneMatch, ...]
    phase_two: tuple[PhaseTwoResult, ...] | None
    bound_digests: tuple[str, ...]


def rerun_if_changed(
    previous_digests: tuple[str, ...],
    recapture: "Callable[[], list[GuidanceDocument]]",
    *,
    scope: str,
    compiled_candidates: list[CompiledCandidate],
    active_guidance_artifacts: dict[str, list],
) -> ReplayOutcome:
    documents = recapture()
    current = guidance_bound_digests(documents, scope)
    changed = tuple(previous_digests) != current
    if not changed:
        return ReplayOutcome(changed=False, phase_one=(), phase_two=None, bound_digests=current)
    phase_one = tuple(classify_phase_one(doc.source_id, doc.text, scope) for doc in documents)
    phase_two = tuple(
        PhaseTwoResult(
            behavior_key=candidate.behavior_key,
            artifact_kind=candidate.artifact_kind,
            matched=phase_two_matches(
                candidate.artifact_kind,
                candidate.compiled,
                active_guidance_artifacts.get(candidate.artifact_kind, []),
            ),
        )
        for candidate in compiled_candidates
    )
    return ReplayOutcome(
        changed=True, phase_one=phase_one, phase_two=phase_two, bound_digests=current
    )


def _is_hex64(value: str) -> bool:
    if len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _is_timezone_aware_iso(value: str) -> bool:
    if not value:
        return False
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.tzinfo.utcoffset(parsed) is not None


def validate_match_record(record: "ActiveGuidanceMatchRecord") -> None:
    if record.schema_version != ACTIVE_GUIDANCE_MATCH_SCHEMA:
        raise GuidanceError("wrong active-guidance-match schema version")
    if not _is_hex64(record.snapshot_digest):
        raise GuidanceError("snapshot_digest must be a 64-char hex sha256")
    for digest in record.source_digests:
        if not _is_hex64(digest):
            raise GuidanceError("every source digest must be a 64-char hex sha256")
    if record.phase not in (PHASE_BEHAVIOR, PHASE_ARTIFACT):
        raise GuidanceError("unknown phase")
    if record.match_state not in (
        MATCH_TAGGED,
        MATCH_LEGACY,
        MATCH_AMBIGUOUS,
        MATCH_NONE,
        MATCH_EXACT_ARTIFACT,
    ):
        raise GuidanceError("unknown match_state")
    if set(record.behavior_key) != {"subject_code", "predicate_code", "polarity", "scope"}:
        raise GuidanceError("behavior key must carry exactly the four closed keys")
    try:
        BehaviorKey(
            subject_code=record.behavior_key["subject_code"],
            predicate_code=record.behavior_key["predicate_code"],
            polarity=record.behavior_key["polarity"],
            scope=record.behavior_key["scope"],
        )
    except OntologyError as exc:
        raise GuidanceError(f"invalid behavior key: {exc}") from exc
    if not _is_timezone_aware_iso(record.captured_at):
        raise GuidanceError("captured_at must be a timezone-aware ISO-8601 timestamp")
    if record.phase == PHASE_ARTIFACT:
        if record.artifact_digest is None or not _is_hex64(record.artifact_digest):
            raise GuidanceError("artifact phase requires a 64-char hex artifact_digest")
        if record.match_state != MATCH_EXACT_ARTIFACT:
            raise GuidanceError("artifact phase requires exact_artifact_match")
    else:
        if record.artifact_digest is not None:
            raise GuidanceError("behavior phase must not carry an artifact_digest")


def match_record(
    snapshot_digest: str,
    source_digests: tuple[str, ...],
    behavior_key: BehaviorKey,
    phase: str,
    match_state: str,
    captured_at: str,
    artifact_digest: str | None = None,
) -> ActiveGuidanceMatchRecord:
    return ActiveGuidanceMatchRecord(
        schema_version=ACTIVE_GUIDANCE_MATCH_SCHEMA,
        snapshot_digest=snapshot_digest,
        source_digests=tuple(source_digests),
        behavior_key={
            "subject_code": behavior_key.subject_code,
            "predicate_code": behavior_key.predicate_code,
            "polarity": behavior_key.polarity,
            "scope": behavior_key.scope,
        },
        artifact_digest=artifact_digest,
        phase=phase,
        match_state=match_state,
        captured_at=captured_at,
    )
