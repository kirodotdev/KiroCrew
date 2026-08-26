"""Dependency-free text helpers shared by the tips runtime (``kiro_crew.tips``)
and the release-time catalog generator (``scripts/generate_tips_catalog.py``).

Kept import-light on purpose: the generator script runs outside the installed
package (``sys.path`` insert) and must not pull the aiohttp runtime.
"""

from __future__ import annotations

# Cap for catalog summaries (user-visible tip bodies in the fallback path).
SUMMARY_MAX_CHARS = 300

_SENTENCE_ENDINGS = (". ", "! ", "? ")


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
