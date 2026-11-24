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
# Cap lessons per consolidation: each write_lesson makes a blocking embed of its
# rule, so an uncapped LLM lessons array could occupy a worker thread for minutes.
_MAX_LESSONS_PER_CONSOLIDATION = 10

# Characters the ``## Current Semantic Memory`` block of ONE consolidation prompt
# may hold. The block hands the model the active semantic table so it updates
# or deletes the keys it already holds instead of minting near-duplicates, and
# the table only grows: one long-lived store measured 1,977 active rows
# rendering to about 2 million characters, 97% of the prompt, until a pass
# failed as too large to send. The chat path reads the same table under
# ``semantic_cap`` (12,000 characters per turn); consolidation runs rarely (a
# 30-message threshold or a 3-hour idle) and reads the table as the context it
# merges INTO, so its allowance is wider: 64 KiB, a few times the per-turn cap
# and the scale of the transcript span beside it, which keeps the whole prompt
# in the low hundreds of thousands of characters on a store of any size. At the
# measured median row (856-character values) the cap keeps about 70 of the most
# recently updated rows; a table that fits renders exactly as before.
_SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION = 65_536

# Longest value, in characters as the per-turn facts block renders it, that ONE
# automated consolidation semantic write may carry. A longer value is refused whole,
# never shortened: a value cut short would read as the fact itself. Owner edits,
# imports and the dashboard are not held to it; they keep the generic byte ceiling in
# ``validate_semantic``. The unit is the one every context budget counts in, ``len()``
# of the text shown: a string as itself, a dict or list as the JSON text the block
# shows, every non-ASCII character as written. The number:
# one value plus the longest bare key still fits the per-turn facts block on its own
# (``context_assembly.budget._SEMANTIC_MEMORY_CAP``, the budget for ALL facts
# together), so a value over it can never be shown there whole; it equals the
# episodic text maximum the same prompt states, so the model is asked for one
# number; and it is more than twice the median value measured above, so only the
# long tail is refused. Fixed rather than derived from that cap, so a budget retune
# cannot move write policy silently: a test pins it under the cap instead.
_MAX_CONSOLIDATION_SEMANTIC_VALUE_CHARS = 2_000

# Ceiling on episodic rows ONE semantic write may retire, and the reason it needs a
# ceiling at all. `_retire_stale_episodic` tombstones episodes that reference a value
# the semantic store just superseded, and consolidation rewrites the same keys every
# cycle -- so an uncapped rule applies once per key per cycle, forever. Measured on a
# store hours old: 21 of 101 episodes already retired, 14 of them by this rule.
#
# Three is a judgement about what the rule is FOR: a superseded value should retire the
# few episodes that restate it, not a slice of the store. A candidate beyond the cap is
# left ALIVE rather than tombstoned, which is the safe direction -- a stale episode is
# outranked by the newer semantic row it contradicts, while a wrongly retired one is
# invisible to every reader.
_MAX_EPISODIC_RETIRED_PER_WRITE = 3

# Prompt-injection detection patterns. Kept in this dependency-free module (not
# in ``vector_memory``, whose numpy/faiss/snowballstemmer imports are heavy) so
# prompt-building callers such as ``security.contains_injection`` can screen
# untrusted external content (e.g. Slack thread-parent / metadata) via a plain
# top-level import — no lazy import and no fail-open path. ``vector_memory``
# re-exports both names, so existing import paths stay valid.
_INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"ignore\s+(all\s+)?above",
        r"you\s+are\s+now",
        r"new\s+instructions?:",
        r"system\s*prompt",
        r"<\s*system\s*>",
        r"<\s*/?\s*instructions?\s*>",
        r"IMPORTANT:\s*override",
        r"forget\s+(everything|all)",
        r"disregard\s+(all|previous|your)\s+instructions",
        # Synonyms of "previous" for the ignore/disregard/forget directive.
        # Each optional group opens on a distinct literal word followed by
        # required whitespace, so matching stays linear.
        r"(ignore|disregard|forget)\s+(all\s+)?(of\s+)?(the\s+|your\s+)?"
        r"(previous|prior|earlier|preceding|above|foregoing)\s+instructions",
        r"act\s+as\s+if",
        r"pretend\s+you\s+are",
        r"new\s+persona",
        r"no\s+restrictions",
    ]
]


# Unicode Default_Ignorable_Code_Point (DerivedCoreProperties.txt): characters that
# render as nothing, such as U+200B ZERO WIDTH SPACE, U+00AD SOFT HYPHEN or a
# variation selector. Written out because the stdlib has no lookup for the property.
# Matches a whole run of them.
_DEFAULT_IGNORABLE_RE = re.compile(
    r"[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f"
    r"\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufff8"
    r"\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]+"
)

# An invisible character can sit inside a word ("ig\u200bnore") or be the only
# thing between two words ("you\u200bare\u200bnow"). Neither removing it nor
# reading it as a space suits both, so each run of them becomes one WORD JOINER
# and the patterns are rewritten to let it stand between two letters of a word
# or count as whitespace. Words fused without one ("forgetAll") stay unmatched.
_INVISIBLE_MARK = "\u2060"
_INJECTION_PATTERNS_INVISIBLE = [
    re.compile(
        # After a letter, or a letter made optional ("instructions?"), and
        # before a letter or a colon; never after an escape such as ``\s``.
        re.sub(
            r"((?<=[A-Za-z])(?<!\\[A-Za-z])|(?<=[A-Za-z]\?))(?=[A-Za-z:])",
            _INVISIBLE_MARK + "?",
            p.pattern,
        ).replace(r"\s", "[\\s" + _INVISIBLE_MARK + "]"),
        p.flags,
    )
    for p in _INJECTION_PATTERNS
]


def _contains_injection(text: str) -> bool:
    """Check if text contains known prompt injection patterns.

    Invisible characters are read as the patterns' own gaps, so a directive
    cannot hide from them behind characters a reader never sees.
    """
    if not _DEFAULT_IGNORABLE_RE.search(text):
        return any(p.search(text) for p in _INJECTION_PATTERNS)
    marked = _DEFAULT_IGNORABLE_RE.sub(_INVISIBLE_MARK, text)
    return any(p.search(marked) for p in _INJECTION_PATTERNS_INVISIBLE)
