from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

ONTOLOGY_SCHEMA_VERSION: Final[str] = "kiro.personal-insights.behavior-ontology/2.0"

SUBJECT_CODES: Final[frozenset[str]] = frozenset(
    {
        "session_owner",
        "work_session",
        "work_area",
        "workflow_pattern",
        "interaction_pattern",
        "tool_use_pattern",
        "verification_pattern",
        "friction_pattern",
        "strength_pattern",
    }
)

PREDICATE_CODES: Final[frozenset[str]] = frozenset(
    {
        "works_in_area",
        "repeats_context_setup",
        "requests_iterative_refinement",
        "verifies_changes",
        "verification_omission_explicit",
        "retries_operation",
        "uses_parallel_delegation",
        "encounters_tool_errors",
        "interrupts_long_steps",
        "reuses_effective_prompt",
        "needs_capability_discovery",
        "preserves_reversible_changes",
        "notable_singular_event",
    }
)

POLARITY_POSITIVE: Final[str] = "positive"
POLARITY_NEGATIVE: Final[str] = "negative"
POLARITY_NEUTRAL: Final[str] = "neutral"

POLARITIES: Final[frozenset[str]] = frozenset(
    {POLARITY_POSITIVE, POLARITY_NEGATIVE, POLARITY_NEUTRAL}
)

GLOBAL_SCOPE: Final[str] = "global"

_WORKSPACE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^ws-[A-Za-z0-9_-]{8,}$")


@dataclass(frozen=True)
class PredicateDeclaration:
    predicate_code: str
    allowed_polarities: tuple[str, ...]
    compatible_features: tuple[str, ...]
    dimensions: tuple[str, ...]
    opportunity_rule: str
    observability_rule: str
    support_rule: str
    counterexample_rule: str
    completeness_requirement: str
    wording_tiers: tuple[str, ...]


OPPORTUNITY_RULES: Final[frozenset[str]] = frozenset(
    {
        "any_session",
        "sessions_with_tool_use",
        "sessions_with_verification_opportunity",
        "sessions_with_long_steps",
        "sessions_with_repository_scope",
    }
)
OBSERVABILITY_RULES: Final[frozenset[str]] = frozenset(
    {
        "owner_message_text",
        "structural_tool_events",
        "lifecycle_events",
        "deterministic_error_events",
        "deterministic_skipped_gate_event",
        "timing_fields",
    }
)
SUPPORT_RULES: Final[frozenset[str]] = frozenset(
    {"positive_event_present", "explicit_owner_statement"}
)
COUNTEREXAMPLE_RULES: Final[frozenset[str]] = frozenset(
    {"opposing_event_present", "no_counterexample_defined"}
)
COMPLETENESS_RULES: Final[frozenset[str]] = frozenset(
    {"requires_complete_source", "tolerates_partial_source"}
)

_WORK_AREA: Final[tuple[str, ...]] = ("work_area",)
_INTERACTION: Final[tuple[str, ...]] = ("interaction_style",)
_STRENGTH: Final[tuple[str, ...]] = ("strength",)
_FRICTION: Final[tuple[str, ...]] = ("friction", "opportunity")
_MOMENT: Final[tuple[str, ...]] = ("moment",)
_WORDING: Final[tuple[str, ...]] = ("single", "repeated", "recurring", "usually")
_POSITIVE_ONLY: Final[tuple[str, ...]] = (POLARITY_POSITIVE,)

_DECLARATION_SPECS: Final[tuple[tuple, ...]] = (
    (
        "works_in_area",
        ("messages", "code_activity"),
        _WORK_AREA,
        "any_session",
        "owner_message_text",
        "positive_event_present",
        "no_counterexample_defined",
        "tolerates_partial_source",
    ),
    (
        "repeats_context_setup",
        ("messages",),
        _INTERACTION,
        "any_session",
        "owner_message_text",
        "positive_event_present",
        "no_counterexample_defined",
        "tolerates_partial_source",
    ),
    (
        "requests_iterative_refinement",
        ("messages",),
        _INTERACTION,
        "any_session",
        "owner_message_text",
        "positive_event_present",
        "no_counterexample_defined",
        "tolerates_partial_source",
    ),
    (
        "verifies_changes",
        ("structural_tools", "lifecycle"),
        _STRENGTH,
        "sessions_with_verification_opportunity",
        "structural_tool_events",
        "positive_event_present",
        "opposing_event_present",
        "requires_complete_source",
    ),
    (
        "verification_omission_explicit",
        ("skipped_gate",),
        _FRICTION,
        "sessions_with_verification_opportunity",
        "deterministic_skipped_gate_event",
        "explicit_owner_statement",
        "opposing_event_present",
        "requires_complete_source",
    ),
    (
        "retries_operation",
        ("structural_tools", "errors"),
        _FRICTION,
        "sessions_with_tool_use",
        "structural_tool_events",
        "positive_event_present",
        "no_counterexample_defined",
        "tolerates_partial_source",
    ),
    (
        "uses_parallel_delegation",
        ("structural_tools",),
        _STRENGTH,
        "sessions_with_tool_use",
        "structural_tool_events",
        "positive_event_present",
        "no_counterexample_defined",
        "tolerates_partial_source",
    ),
    (
        "encounters_tool_errors",
        ("errors",),
        _FRICTION,
        "sessions_with_tool_use",
        "deterministic_error_events",
        "positive_event_present",
        "no_counterexample_defined",
        "tolerates_partial_source",
    ),
    (
        "interrupts_long_steps",
        ("lifecycle", "timing"),
        _FRICTION,
        "sessions_with_long_steps",
        "lifecycle_events",
        "positive_event_present",
        "no_counterexample_defined",
        "tolerates_partial_source",
    ),
    (
        "reuses_effective_prompt",
        ("messages",),
        _STRENGTH,
        "any_session",
        "owner_message_text",
        "positive_event_present",
        "no_counterexample_defined",
        "tolerates_partial_source",
    ),
    (
        "needs_capability_discovery",
        ("messages",),
        _FRICTION,
        "any_session",
        "owner_message_text",
        "explicit_owner_statement",
        "no_counterexample_defined",
        "tolerates_partial_source",
    ),
    (
        "preserves_reversible_changes",
        ("structural_tools",),
        _STRENGTH,
        "sessions_with_repository_scope",
        "structural_tool_events",
        "positive_event_present",
        "opposing_event_present",
        "requires_complete_source",
    ),
    (
        "notable_singular_event",
        ("messages", "lifecycle"),
        _MOMENT,
        "any_session",
        "lifecycle_events",
        "positive_event_present",
        "no_counterexample_defined",
        "tolerates_partial_source",
    ),
)

PREDICATE_DECLARATIONS: Final[dict[str, PredicateDeclaration]] = {
    code: PredicateDeclaration(
        predicate_code=code,
        allowed_polarities=_POSITIVE_ONLY,
        compatible_features=features,
        dimensions=dimensions,
        opportunity_rule=opportunity,
        observability_rule=observability,
        support_rule=support,
        counterexample_rule=counterexample,
        completeness_requirement=completeness,
        wording_tiers=_WORDING,
    )
    for (
        code,
        features,
        dimensions,
        opportunity,
        observability,
        support,
        counterexample,
        completeness,
    ) in _DECLARATION_SPECS
}


def evidence_abstains(
    declaration: PredicateDeclaration, available_features: tuple[str, ...]
) -> bool:
    return not all(feature in available_features for feature in declaration.compatible_features)


class OntologyError(Exception):
    pass


def is_known_subject(code: str) -> bool:
    return code in SUBJECT_CODES


def is_known_predicate(code: str) -> bool:
    return code in PREDICATE_CODES


def is_valid_scope(scope: str) -> bool:
    return scope == GLOBAL_SCOPE or bool(_WORKSPACE_ID_RE.match(scope))


def polarity_compatible(predicate_code: str, polarity: str) -> bool:
    declaration = PREDICATE_DECLARATIONS.get(predicate_code)
    if declaration is None:
        return False
    return polarity in declaration.allowed_polarities


@dataclass(frozen=True)
class BehaviorKey:
    subject_code: str
    predicate_code: str
    polarity: str
    scope: str

    def __post_init__(self) -> None:
        if self.subject_code not in SUBJECT_CODES:
            raise OntologyError(f"unknown subject code: {self.subject_code}")
        if self.predicate_code not in PREDICATE_CODES:
            raise OntologyError(f"unknown predicate code: {self.predicate_code}")
        if self.polarity not in POLARITIES:
            raise OntologyError(f"unknown polarity: {self.polarity}")
        if not polarity_compatible(self.predicate_code, self.polarity):
            raise OntologyError(
                f"incompatible polarity {self.polarity} for predicate {self.predicate_code}"
            )
        if not is_valid_scope(self.scope):
            raise OntologyError("scope must be global or a stable workspace id")

    def canonical(self) -> str:
        return f"{self.subject_code}|{self.predicate_code}|{self.polarity}|{self.scope}"


def behavior_key(subject_code: str, predicate_code: str, polarity: str, scope: str) -> BehaviorKey:
    return BehaviorKey(
        subject_code=subject_code,
        predicate_code=predicate_code,
        polarity=polarity,
        scope=scope,
    )
