"""Shared validation for durable lesson text."""

from __future__ import annotations

import re

from kiro_crew.model_registry import MODEL_ID_LITERAL_PATTERN

_VOLATILE_MODEL_FACT_RE = re.compile(
    r"\b(?:current|active)\s+model(?:\s+identity)?\s*"
    r"(?:is\b|was\b|changes?\b|shown\b|[:=])"
    rf"|\b(?:selected|session)\s+model(?:\s+identity)?\s*"
    rf"(?:is\b|was\b|[:=])\s*{MODEL_ID_LITERAL_PATTERN}"
    r"|\b(?:selected|session)\s+model\s+identity\s*(?:changes?\b|shown\b)"
    rf"|\brunning\s+as\s+(?:(?:the\s+)?(?:current|active|selected)?\s*"
    rf"(?:model|backend)\b|{MODEL_ID_LITERAL_PATTERN})",
    re.IGNORECASE,
)
_BEHAVIORAL_MODEL_PIN_RE = re.compile(
    rf"(?:\b(?:always|never|should|must)\s+(?:use|choose|select|prefer)\b"
    rf"|(?:^|[.!?]\s+|\n\s*)"
    rf"\s*(?:(?:for|when)\b[^,\n]{{0,120}},\s*)?"
    rf"(?:(?:please|kindly)\s+)?(?:do\s+)?"
    rf"(?:use|choose|select|prefer)\b(?!\s+of\b))"
    rf"[^.\n]{{0,160}}{MODEL_ID_LITERAL_PATTERN}",
    re.IGNORECASE,
)


def contains_volatile_lesson_fact(
    rule: object,
    negative: object = None,
) -> bool:
    """Whether either persisted field records runtime identity or a model pin.

    Runtime model-identity assertions and recognized concrete-ID model-selection
    imperatives are volatile in every category. A model-version literal without
    either form is durable, including in a NOT-clause.
    """
    rule_text = rule if isinstance(rule, str) else ""
    negative_text = negative if isinstance(negative, str) else ""
    return any(
        _VOLATILE_MODEL_FACT_RE.search(text) or _BEHAVIORAL_MODEL_PIN_RE.search(text)
        for text in (rule_text, negative_text)
        if text
    )
