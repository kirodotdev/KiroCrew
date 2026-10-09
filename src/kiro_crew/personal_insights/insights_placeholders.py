from __future__ import annotations

import re
from typing import Final

PLACEHOLDER_SET_VERSION: Final[str] = "1.0"

_ANGLE: Final[re.Pattern[str]] = re.compile(r"<[^<>]+>")
_CURLY_SINGLE: Final[re.Pattern[str]] = re.compile(r"(?<!\{)\{[^{}]+\}(?!\})")
_CURLY_DOUBLE: Final[re.Pattern[str]] = re.compile(r"\{\{[^{}]+\}\}")
_BRACKET_CAPS: Final[re.Pattern[str]] = re.compile(r"\[[A-Z0-9_]+\]")
_LITERAL_TOKENS: Final[tuple[str, ...]] = ("TODO", "TBD", "FIXME", "YOUR_", "REPLACE_")
_ELLIPSIS_LINE: Final[re.Pattern[str]] = re.compile(r"^\s*\.\.\.\s*$", re.MULTILINE)


def contains_placeholder(text: str) -> bool:
    if _ANGLE.search(text):
        return True
    if _CURLY_DOUBLE.search(text):
        return True
    if _CURLY_SINGLE.search(text):
        return True
    if _BRACKET_CAPS.search(text):
        return True
    for token in _LITERAL_TOKENS:
        if token in text:
            return True
    if _ELLIPSIS_LINE.search(text):
        return True
    return False


def declared_literal_overlaps_placeholder(declared_literal: str) -> bool:
    return contains_placeholder(declared_literal)
