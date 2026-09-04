"""Dependency-free text helpers shared by the tips runtime (``kiro_crew.tips``)
and the release-time catalog generator (``scripts/generate_tips_catalog.py``).

Kept import-light on purpose: the generator script runs outside the installed
package (``sys.path`` insert) and must not pull the aiohttp runtime.
"""

from __future__ import annotations

import re

# Cap for catalog summaries (user-visible tip bodies in the fallback path).
SUMMARY_MAX_CHARS = 300

_SENTENCE_ENDINGS = (". ", "! ", "? ")

# Emoji and dingbat ranges. A tip body is plain UI text, so a decorative glyph
# that a doc uses for emphasis has no place in it.
_EMOJI_RE = re.compile("[\U0001f000-\U0001faff\u2190-\u21ff\u2600-\u27bf\u2b00-\u2bff\ufe0f\u200d]")

# Markdown blocks that are never the doc's opening prose: a heading, an
# admonition blockquote, a fenced code block, an HTML comment or tag, a table,
# a list, or a standalone image.
_NON_PROSE_PREFIXES = (
    "#",
    ">",
    "```",
    "~~~",
    "<!--",
    "<",
    "|",
    "- ",
    "* ",
    "+ ",
    "![",
)
_ORDERED_ITEM_RE = re.compile(r"^\d+[.)]\s")


def strip_decoration(text: str) -> str:
    """Drop emoji and collapse whitespace, leaving plain UI text."""
    return " ".join(_EMOJI_RE.sub("", text).split())


def first_prose_paragraph(body: str) -> str:
    """Return the first real prose paragraph of *body*, whitespace-collapsed.

    ``body`` is the document text following its H1. Selecting the first
    non-empty block is not enough: several packaged docs open with a
    ``> **warning**`` admonition, whose raw ``>`` markers and emoji then ship
    verbatim as a user-visible tip. Blocks that markdown makes structural
    rather than prose are skipped instead, and a fenced block is skipped whole
    so its contents cannot be read as a paragraph.
    """
    blocks = body.lstrip("\n").split("\n\n")
    in_fence = False
    for block in blocks:
        stripped = block.strip()
        if not stripped:
            continue
        fence_marks = stripped.count("```") + stripped.count("~~~")
        if in_fence:
            # An odd number of markers inside the fence closes it.
            if fence_marks % 2:
                in_fence = False
            continue
        if stripped.startswith(("```", "~~~")) and fence_marks % 2:
            in_fence = True
            continue
        if stripped.startswith(_NON_PROSE_PREFIXES) or _ORDERED_ITEM_RE.match(stripped):
            continue
        para = strip_decoration(stripped)
        if para:
            return para
    return ""


def truncate_summary(text: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    """Truncate *text* to at most *limit* chars without cutting mid-word.

    A plain ``text[:limit]`` slice chops mid-word ("… its cycle budget. Ea"),
    and the fallback tips path serves that string verbatim to the user.
    Instead:

    1. If the text already fits, return it unchanged.
    2. Prefer cutting after the last complete sentence that fits.
    3. Otherwise cut at the last word boundary and append an ellipsis.
    """
    if len(text) <= limit:
        return text
    # Look one char past the limit so a sentence ending exactly at the edge
    # (period at index limit-1, space at index limit) still counts as a hit.
    window = text[: limit + 1]
    best = -1
    for sep in _SENTENCE_ENDINGS:
        idx = window.rfind(sep)
        if idx > best:
            best = idx
    if best != -1:
        return window[: best + 1]
    # No full sentence fits: cut the partial last word, append an ellipsis.
    window = text[:limit]
    if " " in window:
        words = window.rsplit(" ", 1)[0].rstrip()
    else:
        # Single unbroken token longer than the limit: hard cut is all we have.
        words = window[: limit - 1]
    if not words:
        words = window[: limit - 1]
    return words + "\u2026"
