"""Versioned retrieval policy for private member memory, never global V1.

The cosine cuts are precision-first operating points from the committed
50-topic Qwen3 admission experiment. They are provisional and model-dependent,
not universal semantic boundaries. Lexical recovery and age-neutral ranking
are explicit policies, not claimed measurements from that experiment.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Iterable
from typing import Final

ALGORITHM_VERSION: Final = "member-v2"
SHORT_COSINE_FLOOR: Final = 0.62
LONG_COSINE_FLOOR: Final = 0.57
LONG_TEXT_CHARS: Final = 300

# Function words do not constitute evidence about a task. CJK text is also
# segmented into adjacent pairs: Python's \w otherwise treats a whole Chinese
# sentence as one word, defeating keyword recovery when the model is absent.
_STOP_WORDS = frozenset(
    "a an and are as at be been but by can could did do does for from had has "
    "have how i if in into is it its me my of on or our please should that the "
    "their them there these they this to us was we were what when where which "
    "who why will with would you your about tell use using used".split()
)
_CJK = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]+")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def terms(text: str) -> frozenset[str]:
    """Return normalized task terms, including CJK pairs and identifier parts."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    result: set[str] = set()
    for run in _CJK.findall(normalized):
        result.update(run[i : i + 2] for i in range(len(run) - 1))
        if len(run) == 1:
            result.add(run)
    without_cjk = _CJK.sub(" ", normalized)
    result.update(
        token
        for token in _WORD.findall(without_cjk)
        if token not in _STOP_WORDS and (len(token) > 1 or token.isdigit())
    )
    return frozenset(result)


def visible_json(value_json: str) -> str:
    """Decode storage escapes before lexical retrieval and bounded prompt rendering."""
    try:
        return json.dumps(json.loads(value_json), ensure_ascii=False)
    except (ValueError, TypeError, RecursionError):
        return str(value_json)


def lexical_evidence(query_terms: Iterable[str], text: str) -> tuple[list[str], float, bool]:
    """Admit a majority of meaningful query terms, requiring two when possible."""
    wanted = frozenset(query_terms)
    matched = sorted(wanted & terms(text))
    coverage = len(matched) / len(wanted) if wanted else 0.0
    enough = bool(wanted) and len(matched) >= min(2, len(wanted)) and coverage >= 0.5
    return matched, coverage, enough


def cosine_floor(text: str) -> float:
    return LONG_COSINE_FLOOR if len(text) > LONG_TEXT_CHARS else SHORT_COSINE_FLOOR


def relevance_evidence(query_terms: Iterable[str], text: str, cosine: float | None) -> dict:
    """A serializable explanation for both admission and rejection."""
    matched, coverage, keyword_match = lexical_evidence(query_terms, text)
    floor = cosine_floor(text)
    semantic_match = cosine is not None and math.isfinite(cosine) and cosine >= floor
    if semantic_match and keyword_match:
        reason = "semantic_and_keyword"
    elif semantic_match:
        reason = "semantic_match"
    elif keyword_match:
        reason = "keyword_match"
    else:
        reason = "insufficient_relevance"
    return {
        "algorithm": ALGORITHM_VERSION,
        "admitted": semantic_match or keyword_match,
        "reason": reason,
        "cosine": round(cosine, 4) if cosine is not None and math.isfinite(cosine) else None,
        "cosine_floor": floor,
        "matched_terms": matched,
        "query_coverage": round(coverage, 4),
    }


def rank_score(evidence: dict, *, importance: float) -> float:
    """Rank private memory by relevance and importance, independently of age."""
    cosine = max(0.0, evidence["cosine"] or 0.0)
    lexical = evidence["query_coverage"]
    # A row awaiting embedding still competes through the lexical channel, but
    # it cannot claim the semantic channel it has not been measured on. Applying
    # the same weights to both populations prevents missing-vector state alone
    # from promoting a weaker lexical match over stronger cosine evidence.
    relevance = 0.7 * cosine + 0.3 * lexical
    return round(relevance * (0.85 + 0.15 * importance), 4)


def superseded_value_is_asserted(text: str, key: str, old_value: str) -> bool:
    """Conservative literal evidence; topic similarity alone never retires a row.

    Require an assignment naming the full semantic key at the start of a
    clause. A shared attribute ("color") does not identify the subject of
    pref.color: Bob's color can remain red after the owner's changes. Without
    this explicit key linkage, uncertain prose is deliberately retained.
    """
    needle = unicodedata.normalize("NFKC", old_value).strip().strip('"').casefold()
    subject = unicodedata.normalize("NFKC", key).strip().casefold()
    if not needle or "." not in subject or needle == subject:
        return False
    assignment = (
        r"^\s*" + re.escape(subject) + r"\s*(?::|=|\bis\b)\s*" + re.escape(needle) + r"(?!\w)"
    )
    # A dot within pref.color is part of the subject, not a sentence boundary.
    for clause in re.split(
        r"[!?;\n。！？；]|\.\s+", unicodedata.normalize("NFKC", text).casefold()
    ):
        if re.search(r"\b(?:not|never|previously|formerly|old|historical|used to)\b", clause):
            continue
        if re.search(assignment, clause):
            return True
    return False
