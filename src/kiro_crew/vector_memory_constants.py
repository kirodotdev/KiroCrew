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

# Characters the ``## Current Semantic Memory`` block of ONE consolidation prompt
# may hold, its omission notice included. The block hands the model the active
# semantic table so it updates or deletes the keys it already holds instead of
# minting near-duplicates, and the table only grows: one long-lived store
# measured 1,977 active rows rendering to about 2 million characters, 97% of the
# prompt, until a pass failed as too large to send. The chat path reads the same
# table under ``semantic_cap`` (12,000 characters per turn); consolidation runs
# rarely (a 30-message threshold or a 3-hour idle) and reads the table as the
# context it merges INTO, so its allowance is wider: 64 KiB, a few times the
# per-turn cap and the scale of the transcript span beside it, which keeps the
# whole prompt in the low hundreds of thousands of characters on a store of any
# size. A table that fits renders whole; one that does not keeps about 70 rows
# at the measured median row (856-character values), picked by the pools below.
_SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION = 65_536

# How an over-cap block is picked (``_bounded_semantic_table``): pools in this
# order, each row taken once. Each of these three caps what its pool takes and
# reserves nothing; what they leave unspent goes to the two pools after them.
# The rotation is a slice of the table in key order, resumed after the last key
# the previous pass's slice showed, so a row that is never relevant and never
# recent is still shown in turn: about this many characters of the table each
# time the cursor advances, the knob for a faster sweep of a large store. The
# lessons allowance holds the ``lesson.*`` rows most relevant to the span: a
# lesson's only retirement path in a pass is a delete, and a delete of a key the
# block did not show is withheld, while lessons left to compete with facts on
# recency can fill the block. The recency allowance shows the newest facts
# whatever their score, which covers a row written with its vector deferred. The
# rest of the cap goes to the facts most relevant to the span, then to whatever
# is newest. The three come to 24 KiB of the 64 KiB, so the lessons and recency
# pools run short of room only on a pass whose rotation shows one row wider than
# the cap less both their allowances and the notice (about 48,800 characters):
# they then share what that row leaves, lessons first, the price of showing it.
_SEMANTIC_ROTATION_CHARS_PER_CONSOLIDATION = 8_192
_SEMANTIC_LESSON_CHARS_PER_CONSOLIDATION = 8_192
_SEMANTIC_RECENCY_CHARS_PER_CONSOLIDATION = 8_192

# Seconds an over-cap pass waits for its relevance scores, counted from the moment
# it asks. They need one ``PRIORITY_BULK`` embed of the span, and bulk work has no
# deadline of its own: it queues behind every interactive and normal embed and the
# backfill sweep's pace, and a native call already running cannot be stopped, so an
# unbounded wait holds the pass, its lease and an embed-pool slot for as long as a
# busy or wedged embedder takes. Past the first allowance the embed gives up and the
# rows score on keywords alone. 20 s covers an embed queued behind a sweep row and
# its pace (about 7 s at the default duty) and a burst of interactive embeds, and is
# a third of what the pass's own writes may spend embedding
# (``_EMBED_BUDGET_SECS_PER_PASS``). The second allowance is for the keyword and
# cosine scans around the embed and the wait for a pool worker; past both the pass
# stops waiting and fills the block from the rotation and recency alone.
_SEMANTIC_RELEVANCE_EMBED_SECS_PER_CONSOLIDATION = 20.0
_SEMANTIC_RELEVANCE_SCAN_SECS_PER_CONSOLIDATION = 10.0

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


def _contains_injection(text: str) -> bool:
    """Check if text contains known prompt injection patterns."""
    return any(p.search(text) for p in _INJECTION_PATTERNS)
