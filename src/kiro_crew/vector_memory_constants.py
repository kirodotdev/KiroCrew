"""Lightweight constants + helpers shared with :mod:`kiro_crew.vector_memory`.

Split out so callers that only need the caps or the injection-pattern screen
(e.g. the consolidation prompt builder in :mod:`kiro_crew.history`, or
``security.contains_injection``) can import them at module top level without
pulling ``vector_memory``'s heavy transitive deps (snowballstemmer + the
numpy/faiss optional imports) at import time. ``vector_memory`` re-exports
these, so both import paths stay valid.
"""

from __future__ import annotations

import re

_MAX_SEMANTIC_PER_CONSOLIDATION = 20
_MAX_EPISODIC_PER_CONSOLIDATION = 10
# Cap lessons per consolidation: each write_lesson can perform up to 6 blocking
# embeds (1 rule + _MAX_BACKFILLS_PER_CALL lazy backfills), so an uncapped LLM
# lessons array could occupy a worker thread for minutes.
_MAX_LESSONS_PER_CONSOLIDATION = 10

# Prompt-injection detection patterns. Kept in this dependency-free module (not
# in ``vector_memory``, whose numpy/faiss/snowballstemmer imports are heavy) so
# prompt-building callers such as ``security.contains_injection`` can screen
# untrusted external content (e.g. Slack thread-parent / metadata) via a plain
# top-level import — no lazy import and no fail-open path. ``vector_memory``
# re-exports both names, so existing import paths stay valid.
_INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"ignore\s+(all\s+)?previous\s+instructions",
        r"ignore\s+(all\s+)?above",
        r"you\s+are\s+now",
        r"new\s+instructions?:",
        r"system\s*prompt",
        r"<\s*system\s*>",
        r"<\s*/?\s*instructions?\s*>",
        r"IMPORTANT:\s*override",
        r"forget\s+(everything|all)",
        r"disregard\s+(all|previous|your)\s+instructions",
        r"act\s+as\s+if",
        r"pretend\s+you\s+are",
        r"new\s+persona",
        r"no\s+restrictions",
    ]
]


def _contains_injection(text: str) -> bool:
    """Check if text contains known prompt injection patterns."""
    return any(p.search(text) for p in _INJECTION_PATTERNS)
