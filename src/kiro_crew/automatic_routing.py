"""High-confidence routing for ordinary dashboard chat messages.

This module is deliberately pure: it does not call an LLM, read Knowledge data,
or start a workflow. It only classifies user text and validates the already
selected slot project path. The chat handler owns clarification and dispatch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew.security import is_sensitive_path

CREW_SOFTWARE_DELIVERY = "software-delivery"
CREW_KNOWLEDGE_QUALITY = "knowledge-quality"
CREW_QUALITY_ENGINEERING = "quality-engineering"
CREW_NONE = "none"

ROUTE_SMALL_CHANGE = "small_change"
ROUTE_FEATURE = "feature"
ROUTE_PRODUCTION_CHANGE = "production_change"
ROUTE_RETRIEVAL_AUDIT = "retrieval_audit"
ROUTE_RETRIEVAL_AUDIT_RISK = "retrieval_audit_with_risk_review"
ROUTE_QA_PLAN = "qa_plan"
ROUTE_E2E_VALIDATION = "e2e_validation"
ROUTE_UX_REVIEW = "ux_review"
ROUTE_FULL_QUALITY_REVIEW = "full_quality_review"
ROUTE_NONE = "none"

CONFIDENCE_HIGH = "high"
CONFIDENCE_LOW = "low"

_CHANGE_WORDS = frozenset(
    {
        "add",
        "build",
        "change",
        "create",
        "delete",
        "edit",
        "fix",
        "implement",
        "modify",
        "patch",
        "refactor",
        "remove",
        "rewrite",
        "update",
        "write",
    }
)
_CODE_MARKERS = frozenset(
    {
        "api",
        "app",
        "bug",
        "class",
        "code",
        "component",
        "endpoint",
        "file",
        "flow",
        "function",
        "implementation",
        "module",
        "project",
        "repo",
        "repository",
        "script",
        "source",
        "test",
        "tests",
        "workflow",
    }
)
_PRODUCTION_MARKERS = frozenset(
    {
        "commit",
        "deploy",
        "deployment",
        "live",
        "merge",
        "publish",
        "production",
        "prod",
        "release",
        "rollout",
        "ship",
        "push",
    }
)
_AUDIT_WORDS = frozenset(
    {"audit", "check", "compare", "inspect", "investigate", "review", "validate", "verify"}
)
_KNOWLEDGE_MARKERS = frozenset(
    {
        "embedding",
        "embeddings",
        "fts",
        "fts5",
        "knowledge",
        "knowledgebase",
        "retrieval",
        "search",
        "sidecar",
        "vector",
        "wal",
    }
)
_RISK_MARKERS = frozenset(
    {
        "migration",
        "read-only",
        "readonly",
        "security",
        "side-effect",
        "sideeffects",
        "unsafe",
        "wal",
    }
)
_QUALITY_MARKERS = frozenset(
    {
        "acceptance",
        "accessibility",
        "browser",
        "e2e",
        "end-to-end",
        "playwright",
        "qa",
        "quality",
        "readiness",
        "regression",
        "test",
        "tests",
        "testing",
        "usability",
        "ux",
        "validation",
        "verification",
    }
)
_UX_MARKERS = frozenset({"accessibility", "usability", "ux", "visual", "keyboard", "responsive"})
_E2E_MARKERS = frozenset({"browser", "e2e", "end-to-end", "playwright"})
_FULL_MARKERS = frozenset({"all", "full", "readiness", "regression", "release"})
_QUALITY_ACTIONS = frozenset(
    {
        "assess",
        "audit",
        "check",
        "compare",
        "inspect",
        "investigate",
        "plan",
        "review",
        "run",
        "test",
        "validate",
        "verify",
    }
)
_IMPLEMENTATION_PRECEDENCE = frozenset(
    {"build", "fix", "implement", "modify", "patch", "refactor", "rewrite"}
)
_QUESTION_WORDS = frozenset({"can", "could", "explain", "how", "is", "what", "why"})
_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])/(?:[^\s'\"`]|\\ )+")


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """The bounded routing contract consumed by the dashboard dispatcher."""

    crew_id: str = CREW_NONE
    route: str = ROUTE_NONE
    confidence: str = CONFIDENCE_HIGH
    reason_codes: tuple[str, ...] = ()
    project_path: str = ""
    approval_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "crew_route_decision",
            "crew_id": self.crew_id,
            "route": self.route,
            "confidence": self.confidence,
            "reason_codes": list(self.reason_codes),
            "project_path": self.project_path,
            "approval_required": self.approval_required,
        }


def _tokens(message: str) -> set[str]:
    return set(re.findall(r"[a-z0-9][a-z0-9_-]*", message.lower()))


def _valid_project_path(project_path: str | Path | None) -> str:
    if not isinstance(project_path, (str, Path)):
        return ""
    raw = str(project_path).strip()
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        return ""
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        return ""
    if is_sensitive_path(str(resolved)):
        return ""
    return str(resolved)


def _absolute_path_from_message(message: str) -> str:
    """Return a safe absolute path mentioned in text for diagnostics only."""

    match = _PATH_RE.search(message)
    if not match:
        return ""
    return _valid_project_path(match.group(0).replace("\\ ", " "))


def _decision_for_software(
    *,
    message: str,
    tokens: set[str],
    project_path: str | Path | None,
    reason_codes: tuple[str, ...],
) -> RouteDecision:
    resolved = _valid_project_path(project_path)
    if not resolved:
        return RouteDecision(
            confidence=CONFIDENCE_LOW, reason_codes=reason_codes + ("project_path.required",)
        )
    production = bool(tokens & _PRODUCTION_MARKERS)
    route = (
        ROUTE_PRODUCTION_CHANGE
        if production
        else (
            ROUTE_FEATURE
            if len(tokens & _CHANGE_WORDS) > 1 or tokens & {"architecture", "feature", "multi-file"}
            else ROUTE_SMALL_CHANGE
        )
    )
    return RouteDecision(
        crew_id=CREW_SOFTWARE_DELIVERY,
        route=route,
        confidence=CONFIDENCE_HIGH,
        reason_codes=reason_codes,
        project_path=resolved,
        approval_required=production,
    )


def _quality_route(tokens: set[str], text: str) -> str:
    if (tokens & _FULL_MARKERS) or "full validation" in text or "release readiness" in text:
        return ROUTE_FULL_QUALITY_REVIEW
    if tokens & _UX_MARKERS:
        return ROUTE_UX_REVIEW
    if tokens & _E2E_MARKERS:
        return ROUTE_E2E_VALIDATION
    return ROUTE_QA_PLAN


def _quality_decision(
    *,
    message: str,
    text: str,
    tokens: set[str],
    project_path: str | Path | None,
) -> RouteDecision | None:
    markers = tokens & _QUALITY_MARKERS
    if not markers:
        return None
    actions = tokens & _QUALITY_ACTIONS
    explicit_plan = bool(tokens & {"plan", "strategy", "readiness"})
    # Questions are informational by default, even when they mention an action
    # such as "validate" or planning/readiness terminology. Explicit imperative
    # planning language remains eligible for quality routing.
    question_only = bool(tokens & _QUESTION_WORDS)
    if question_only:
        return RouteDecision(reason_codes=("no_high_confidence_route",))
    # An implementation request wins over validation markers. "Create a test
    # plan" remains quality work; "Implement a Playwright browser flow" does not.
    if tokens & _IMPLEMENTATION_PRECEDENCE:
        return None
    if not actions and not explicit_plan:
        return None
    resolved = _valid_project_path(project_path)
    route = _quality_route(tokens, text)
    if not resolved:
        return RouteDecision(
            crew_id=CREW_QUALITY_ENGINEERING,
            route=route,
            confidence=CONFIDENCE_LOW,
            reason_codes=("explicit_quality_validation", "project_path.required"),
        )
    return RouteDecision(
        crew_id=CREW_QUALITY_ENGINEERING,
        route=route,
        confidence=CONFIDENCE_HIGH,
        reason_codes=("explicit_quality_validation",),
        project_path=resolved,
    )


def classify_message(message: str, *, project_path: str | Path | None = None) -> RouteDecision:
    """Classify one ordinary user message without side effects."""

    if not isinstance(message, str) or not message.strip():
        return RouteDecision(reason_codes=("message.empty",))

    text = message.strip().lower()
    tokens = _tokens(text)
    change_words = tokens & _CHANGE_WORDS
    code_markers = tokens & _CODE_MARKERS
    production_markers = tokens & _PRODUCTION_MARKERS
    audit_words = tokens & _AUDIT_WORDS
    knowledge_markers = tokens & _KNOWLEDGE_MARKERS
    risk_markers = tokens & _RISK_MARKERS

    if audit_words and knowledge_markers:
        route = ROUTE_RETRIEVAL_AUDIT_RISK if risk_markers else ROUTE_RETRIEVAL_AUDIT
        return RouteDecision(
            crew_id=CREW_KNOWLEDGE_QUALITY,
            route=route,
            confidence=CONFIDENCE_HIGH,
            reason_codes=("explicit_retrieval_audit",)
            + (("explicit_risk_review",) if risk_markers else ()),
            project_path=_valid_project_path(project_path),
        )

    quality = _quality_decision(
        message=message, text=text, tokens=tokens, project_path=project_path
    )
    if quality is not None:
        return quality

    if production_markers and (
        change_words or tokens & {"deploy", "release", "publish", "push", "commit", "ship"}
    ):
        return _decision_for_software(
            message=message,
            tokens=tokens,
            project_path=project_path,
            reason_codes=("explicit_production_side_effect",),
        )

    if change_words and code_markers:
        return _decision_for_software(
            message=message,
            tokens=tokens,
            project_path=project_path,
            reason_codes=("explicit_code_change",),
        )

    if (change_words and not code_markers) or (audit_words and not knowledge_markers):
        return RouteDecision(confidence=CONFIDENCE_LOW, reason_codes=("ambiguous_task_intent",))

    return RouteDecision(reason_codes=("no_high_confidence_route",))


def classify_clarification_answer(
    original_message: str,
    answer: str,
    *,
    project_path: str | Path | None = None,
) -> RouteDecision:
    """Classify one answer and make an unresolved answer use the default path."""

    if not isinstance(answer, str) or not answer.strip():
        return RouteDecision(reason_codes=("clarification.unresolved",))
    answer_tokens = _tokens(answer)
    if answer_tokens & {"other", "something", "else", "default", "normal", "none"}:
        return RouteDecision(reason_codes=("clarification.default_path",))

    code_hint = answer_tokens & (_CHANGE_WORDS | _CODE_MARKERS | {"software", "coding"})
    knowledge_hint = answer_tokens & (_KNOWLEDGE_MARKERS | {"audit", "knowledge"})
    if code_hint and not knowledge_hint:
        return _decision_for_software(
            message=f"{original_message}\nClarification: {answer}",
            tokens=_tokens(f"{original_message} {answer}"),
            project_path=project_path,
            reason_codes=("clarification.code_change",),
        )
    if knowledge_hint and not code_hint:
        combined = f"{original_message}\nClarification: {answer}"
        return (
            classify_message(combined, project_path=project_path)
            if _tokens(combined) & _AUDIT_WORDS
            else RouteDecision(
                crew_id=CREW_KNOWLEDGE_QUALITY,
                route=ROUTE_RETRIEVAL_AUDIT,
                confidence=CONFIDENCE_HIGH,
                reason_codes=("clarification.retrieval_audit",),
                project_path=_valid_project_path(project_path),
            )
        )
    return RouteDecision(reason_codes=("clarification.unresolved",))


__all__ = [
    "CONFIDENCE_HIGH",
    "CONFIDENCE_LOW",
    "CREW_KNOWLEDGE_QUALITY",
    "CREW_NONE",
    "CREW_QUALITY_ENGINEERING",
    "CREW_SOFTWARE_DELIVERY",
    "ROUTE_E2E_VALIDATION",
    "ROUTE_FEATURE",
    "ROUTE_FULL_QUALITY_REVIEW",
    "ROUTE_NONE",
    "ROUTE_PRODUCTION_CHANGE",
    "ROUTE_QA_PLAN",
    "ROUTE_RETRIEVAL_AUDIT",
    "ROUTE_RETRIEVAL_AUDIT_RISK",
    "ROUTE_SMALL_CHANGE",
    "ROUTE_UX_REVIEW",
    "RouteDecision",
    "classify_clarification_answer",
    "classify_message",
]
