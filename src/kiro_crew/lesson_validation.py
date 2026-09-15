"""Shared validation for durable lesson text."""

from __future__ import annotations

import re

from kiro_crew.model_registry import MODEL_ID_LITERAL_PATTERN

_MODEL_ID_TOKEN_RE = rf"{MODEL_ID_LITERAL_PATTERN}" r"(?:\w|[.-](?=\w))*" r"(?!\w|[.-](?=\w))"
_MODEL_CLAUSE_END_RE = (
    r"(?=$|\n|[.,;:!?)\]]|\s+(?:"
    r"for|when|whenever|if|unless|in|on|at|to|over|instead|rather|and|or|but|as|"
    r"with|without|because|since|by|until|while|so|only|from|during|before|after|"
    r"except|via|per|not"
    r")\b)"
)
_RUNNING_AS_MODEL_IDENTITY_RE = (
    r"(?:(?:the\s+)?(?:current|active|selected)\s+model\b"
    r"|(?:the\s+)?(?:current|active|selected)\s+backend\s+model\b"
    rf"|(?:the\s+)?(?:current|active|selected)\s+backend\b{_MODEL_CLAUSE_END_RE}"
    r"|(?:the\s+)?(?:model\s+backend|backend\s+model)\b"
    r"|(?:the\s+)?model\b"
    rf"|(?:(?:the\s+)?(?:model|backend)\s+)?{_MODEL_ID_TOKEN_RE})"
)
_VOLATILE_MODEL_FACT_RE = re.compile(
    r"\b(?:current|active)\s+model(?:\s+identity)?\s*"
    r"(?:is\b|was\b|changes?\b|shown\b|[:=])"
    rf"|\b(?:selected|session)\s+model(?:\s+identity)?\s*"
    rf"(?:is\b|was\b|[:=])\s*{MODEL_ID_LITERAL_PATTERN}"
    r"|\b(?:selected|session)\s+model\s+identity\s*(?:changes?\b|shown\b)"
    rf"|\brunning\s+as\s+{_RUNNING_AS_MODEL_IDENTITY_RE}",
    re.IGNORECASE,
)
_MODEL_SELECTION_VERB_RE = (
    r"(?:(?:use|choose|select|prefer)\b(?!\s+of\b)" r"|switch\s+to\b|stick\s+with\b)"
)
_MODEL_OBJECT_RE = (
    r"(?:(?:the|a|an|this|that|our|your)\s+)?"
    r"(?:(?:default|primary|fallback|cheaper|newer|latest|same)\s+)?"
    r"(?:(?:model|backend|provider)\s+)?"
    rf"{_MODEL_ID_TOKEN_RE}"
    r"(?:\s+(?:model|backend|provider)\b)?"
    rf"{_MODEL_CLAUSE_END_RE}"
)
_BEHAVIORAL_MODEL_PIN_RE = re.compile(
    rf"(?:\b(?:always|never|should|must)\s+{_MODEL_SELECTION_VERB_RE}"
    rf"|(?:^|[.!?]\s+|\n\s*)"
    rf"\s*(?:(?:for|when)\b[^,\n]{{0,120}},\s*)?"
    rf"(?:(?:please|kindly)\s+)?(?:do\s+)?"
    rf"{_MODEL_SELECTION_VERB_RE})"
    rf"\s+{_MODEL_OBJECT_RE}",
    re.IGNORECASE,
)


def contains_volatile_lesson_fact(
    rule: object,
    negative: object = None,
) -> bool:
    """Whether either persisted field records runtime identity or a model pin.

    Runtime model-identity assertions and recognized imperatives whose selected
    concrete model-ID object ends its clause are volatile in every category. A
    version literal or an ID that qualifies a following tooling noun is durable,
    including in a NOT-clause.
    """
    rule_text = rule if isinstance(rule, str) else ""
    negative_text = negative if isinstance(negative, str) else ""
    return any(
        _VOLATILE_MODEL_FACT_RE.search(text) or _BEHAVIORAL_MODEL_PIN_RE.search(text)
        for text in (rule_text, negative_text)
        if text
    )
