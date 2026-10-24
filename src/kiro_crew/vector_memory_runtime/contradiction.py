"""Contradiction detection for episodic memory — background sweep only.

``sweep_contradictions`` scans all active episodes pairwise (within a
cosine similarity floor) for contradiction patterns. Returns candidate
pairs for operator review (dry_run=True) or retirement (dry_run=False).
The older entry in each pair is the retirement candidate.

No LLM calls — the heuristic uses pattern matching on correction language,
value conflicts, and negation asymmetry. Conservative by design: false
negatives leave the status quo (both entries persist); false positives are
undoable tombstones (the existing retirement audit trail applies).

This module does NOT hook into the write path. Write-time auto-retirement
based on regex patterns is too fragile for production — casual corrections
are expressed in too many ways for a pattern matcher to catch reliably
without a high false-positive rate. The sweep is intended for periodic
maintenance, with ``dry_run=True`` as the default so operators can review
candidates before enabling auto-retirement.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.vector_memory import VectorMemoryStore

logger = logging.getLogger("kiro_crew.vector_memory")

# Minimum cosine similarity to consider a pair as potentially contradictory.
_CONTRADICTION_COSINE_FLOOR = 0.80

# Maximum contradictions per sweep cycle.
_SWEEP_MAX_RETIREMENTS = 20

# Patterns indicating correction or negation.
_CORRECTION_PATTERNS = re.compile(
    r"\b(?:"
    r"actually|correction|corrected|update[ds]?|"
    r"no longer|not anymore|instead of|rather than|"
    r"changed (?:to|from)|switched (?:to|from)|"
    r"was wrong|were wrong|is wrong|mistake|"
    r"supersede[ds]?|replace[ds]?|overrid(?:e[ds]?|den)|"
    r"used to be|previously was|formerly|"
    r"not (?:true|correct|right|accurate)|incorrect|"
    r"disregard|ignore (?:the )?(?:previous|earlier|old)|"
    r"scratch that|never mind|on second thought"
    r")\b",
    re.IGNORECASE,
)

_NEGATION_PATTERNS = re.compile(
    r"\b(?:"
    r"(?:is|are|was|were|do|does|did|should|would|could|can|will|have|has) not|"
    r"(?:is|are|was|were|do|does|did|should|would|could|can|will|have|has)n't|"
    r"never|no longer|not (?:any|the|a )"
    r")\b",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().strip()


def _extract_key_value_assertions(text: str) -> list[tuple[str, str]]:
    """Extract 'key: value' / 'key = value' / 'key is value' patterns."""
    pattern = re.compile(
        r"(\b[\w.]+(?:\s[\w.]+)?)\s*(?::|=|is|are|was|were)\s+(.+?)(?:[.;,!\n]|$)",
        re.IGNORECASE,
    )
    return [(m.group(1).strip().lower(), m.group(2).strip().lower()) for m in pattern.finditer(text)]


def texts_contradict(text_a: str, text_b: str) -> tuple[bool, str]:
    """Heuristic contradiction detection between two topically-similar texts.

    Returns (contradicts, reason). Conservative: prefers false negatives
    over false positives.
    """
    norm_a = _normalize(text_a)
    norm_b = _normalize(text_b)

    # 1. Correction language in the newer text referencing the older topic.
    if _CORRECTION_PATTERNS.search(norm_b):
        words_a = set(norm_a.split())
        words_b = set(norm_b.split())
        shared = words_a & words_b - {
            "the", "a", "an", "is", "are", "was", "were", "to", "of",
            "in", "for", "and", "or", "not", "it", "that", "this",
        }
        if len(shared) >= 2:
            return True, "correction_language_with_shared_topic"

    # 2. Direct value contradictions in structured facts.
    assertions_a = _extract_key_value_assertions(text_a)
    assertions_b = _extract_key_value_assertions(text_b)
    for key_a, val_a in assertions_a:
        for key_b, val_b in assertions_b:
            if key_a == key_b and val_a != val_b:
                return True, f"value_conflict:{key_a}={val_a}_vs_{val_b}"

    # 3. Negation asymmetry.
    neg_a = bool(_NEGATION_PATTERNS.search(norm_a))
    neg_b = bool(_NEGATION_PATTERNS.search(norm_b))
    if neg_a != neg_b:
        stripped_a = _NEGATION_PATTERNS.sub("", norm_a).strip()
        stripped_b = _NEGATION_PATTERNS.sub("", norm_b).strip()
        words_sa = set(stripped_a.split())
        words_sb = set(stripped_b.split())
        overlap = len(words_sa & words_sb)
        union = len(words_sa | words_sb)
        if union > 0 and overlap / union > 0.5:
            return True, "negation_asymmetry"

    return False, ""


def sweep_contradictions(
    store: VectorMemoryStore,
    *,
    max_retirements: int = _SWEEP_MAX_RETIREMENTS,
    dry_run: bool = True,
) -> list[dict]:
    """Background sweep: find contradictory episode pairs.

    For each pair above the cosine floor, the OLDER entry is the candidate.
    **dry_run=True** (the default) returns candidates without retiring.
    Set dry_run=False to auto-retire via the existing tombstone path.

    Returns [{id, text, reason, cosine, superseded_by, created_at, retired?}].
    """
    rows = store._fetch_all_locked(
        f"SELECT * FROM {store._epi_rel} WHERE is_deleted = 0{store._epi_guard}",
        scan="episodic_contradiction_sweep",
    )

    active: list[tuple[dict, list[float]]] = []
    for raw in store._eligible_rows(rows, "episode"):
        row = dict(raw)
        blob = row.get("embedding")
        if blob:
            import struct
            dim = len(blob) // 4
            vec = list(struct.unpack(f"{dim}f", blob))
            active.append((row, vec))

    if len(active) < 2:
        return []

    import math

    def cosine_sim(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb) if na > 0 and nb > 0 else 0.0

    candidates: list[dict] = []
    seen_ids: set[str] = set()

    for i, (row_a, vec_a) in enumerate(active):
        for j, (row_b, vec_b) in enumerate(active):
            if j <= i:
                continue
            cos = cosine_sim(vec_a, vec_b)
            if cos < _CONTRADICTION_COSINE_FLOOR:
                continue
            contradicts, reason = texts_contradict(row_a["text"], row_b["text"])
            if not contradicts:
                continue
            ts_a = row_a.get("created_at", "")
            ts_b = row_b.get("created_at", "")
            older, newer = (row_a, row_b) if ts_a < ts_b else (row_b, row_a)
            if older["id"] in seen_ids:
                continue
            seen_ids.add(older["id"])
            candidates.append({
                "id": older["id"],
                "text": older["text"][:500],
                "reason": reason,
                "cosine": round(cos, 4),
                "superseded_by": newer["id"],
                "created_at": older.get("created_at", ""),
            })
            if len(candidates) >= max_retirements:
                break
        if len(candidates) >= max_retirements:
            break

    if not dry_run:
        from kiro_crew.vector_memory_runtime.retirement import retire_one_episodic
        for entry in candidates:
            try:
                retire_one_episodic(store, entry["id"], entry["text"], entry["superseded_by"])
                entry["retired"] = True
            except Exception:
                logger.debug("Sweep: failed to retire %s", entry["id"], exc_info=True)
                entry["retired"] = False

    return candidates
