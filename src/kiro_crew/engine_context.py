"""The vocabulary of the agent engine's context breakdown, with no imports of its own.

Shared by the ACP side that parses the engine's frames
(:mod:`kiro_crew.acp.kas_profile`) and the crew log fold that keeps the result
(:mod:`kiro_crew.crew_log.projection_usage_measured`), so the two cannot disagree about
which categories exist or what a usable count is. A leaf, because the fold must not
import the ACP package to learn a list of names.
"""

from __future__ import annotations

import math
from typing import Any, Final

#: The breakdown schema this module knows the categories of. A frame with another
#: schema is still recorded, and the fold marks it, so a reader can say the numbers
#: come from a contract this build does not know.
BREAKDOWN_SCHEMA: Final[int] = 1

#: The categories schema 1 defines, in the order the engine lists them. Their
#: ``chars`` sum to the request's ``totalChars``.
KNOWN_CATEGORIES: Final[tuple[str, ...]] = (
    "kiroInstructions",
    "toolSpecs",
    "mcpTools",
    "agentsIndex",
    "skillsIndex",
    "powersIndex",
    "steering",
    "skills",
    "toolIO",
    "history",
    "workspace",
    "memory",
    "attachedFiles",
    "other",
)

#: ``history`` sub-fields, wire name to the name Kiro Crew records.
HISTORY_PARTS: Final[tuple[tuple[str, str], ...]] = (
    ("userChars", "user"),
    ("assistantChars", "assistant"),
    ("thinkingChars", "thinking"),
    ("compactionSummaryChars", "compaction_summary"),
)

#: Ceiling on every count and duration kept, so a recorded value is at most eight
#: digits. A real request is a few million characters at most.
MAX_COUNT: Final[int] = 99_999_999


def count(value: Any) -> int | None:
    """*value* as a non-negative count, clamped to :data:`MAX_COUNT`, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value < 0:
        return None
    return min(int(value), MAX_COUNT)
