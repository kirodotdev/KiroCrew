"""The window-scaled budget base, and the replay tail budget derived from it.

Two numbers every context consumer agrees on, in a module light enough to import
anywhere. ``context`` builds its section caps from ``budget_base``; the
compaction coordinator (``session_compaction``, imported by ``kiro_crew.session``
on every CLI invocation) projects a rotation from ``replay_budget_chars`` and
``startup_context_chars``. Keeping them here lets both import at module scope:
``context`` itself pulls ~200 further modules that a ``kirocrew <cmd>`` never
needs, and ``context`` imports THIS module, never the other way round.

The reference deployment is a 1M-token window and the base was hand-tuned for
it, so every section's share of the window is fixed and the base scales
linearly with the active window (a section that is 20% of a 1M window stays 20%
of a 200K window, one-fifth the chars). At the reference window the factor is
exactly 1.0, so the default deployment is byte-for-byte unchanged.
"""

from __future__ import annotations

#: Character budget base at the reference window (~55k tokens). ``context``
#: derives every section cap and the global ceiling from it.
CONTEXT_BUDGET_BASE = 165_000

#: The window the base was tuned for.
REFERENCE_WINDOW_TOKENS = 1_000_000

#: Floor so a pathologically small (or misreported) window cannot collapse the
#: caps to ~0 and inject a degenerate context. 20% of the base is the 200K tier,
#: the smallest real model window: below that, memory stops being useful before
#: the model even runs, so clamp rather than shrink further.
MIN_CONTEXT_BUDGET_BASE = int(CONTEXT_BUDGET_BASE * 0.2)

#: Conversation tail a session replay carries at the reference window: 80K chars
#: is about 20K tokens, which fits beside the system context on a 200K window.
REPLAY_BUDGET_CHARS = 80_000

#: The additive session-context sections as fractions of the base. ``context``
#: derives its per-section caps from THIS table, and their sum is the global
#: ceiling ``build_session_context`` enforces when lazy skills are on
#: (``_ResolvedCaps.max_context``): one table, so the ceiling a rotation
#: projection charges cannot drift from the one the context builder applies.
#: Within-section caps (per-message truncation, inject clipping) are not here:
#: they never add to the total.
SECTION_FRACTIONS: dict[str, float] = {
    "compressed_history": 0.27,  # LLM-compressed thread summary
    "prefs": 0.026,  # user preferences
    "projects": 0.039,  # active projects
    "memory_history": 0.16,  # daily history (multi-tier decay)
    "semantic": 0.077,  # semantic memory (vector)
    "episodic": 0.077,  # episodic memory (vector)
    "lessons": 0.226,  # learned corrections (high priority)
    "skills": 0.15,  # skills top-K block (lazy-loaded)
    "steering": 0.10,  # steering resource files
    "preamble_headroom": 0.03,  # fixed rules/identity/workspace/docs/date
}


def effective_window(window_tokens: int | None) -> int:
    """Resolve a usable context-window size, defaulting to the reference (1M).

    A ``None``/unset or non-positive window falls back to the reference window,
    NOT to a small default. This is deliberate: the default deployment runs
    ``provider=acp`` + ``model="auto"``, and the registry maps ``"auto"`` to 200K
    even though ACP auto actually runs a 1M-window model. Treating an
    unknown/auto window as the reference means ONLY an explicitly-selected
    smaller model scales the budget down: an unresolved window never silently
    shrinks the default deployment to 20%.
    """
    if not window_tokens or window_tokens <= 0:
        return REFERENCE_WINDOW_TOKENS
    return window_tokens


def budget_base(window_tokens: int | None) -> int:
    """The context budget base scaled to *window_tokens*, floored at ``MIN_CONTEXT_BUDGET_BASE``."""
    window = effective_window(window_tokens)
    return max(
        MIN_CONTEXT_BUDGET_BASE, round(CONTEXT_BUDGET_BASE * window / REFERENCE_WINDOW_TOKENS)
    )


def reference_cap(fraction: float) -> int:
    """A section's cap at the reference window: the fraction of the untouched base."""
    return int(CONTEXT_BUDGET_BASE * fraction)


def section_cap(fraction: float, window_tokens: int | None) -> int:
    """A section's cap for *window_tokens*: the reference cap scaled by the base factor.

    Two rounding steps on purpose, reference cap first and then the scale, so
    the figure is byte-identical to the module-level ``_*_CAP`` constants at the
    reference window (factor exactly 1.0) and ``context`` can derive its resolved
    caps through this one function.
    """
    return int(reference_cap(fraction) * (budget_base(window_tokens) / CONTEXT_BUDGET_BASE))


def startup_context_chars(window_tokens: int | None) -> int:
    """Ceiling, in characters, on the session-context block a fresh session receives.

    A rotation compaction re-seeds a fresh session and projects where the
    successor starts from this figure plus the tail it carries. It is the FULL
    ceiling, the sum of every additive section cap, which is what
    ``build_session_context`` enforces once lazy skills are on; charging the
    bare base would understate the successor by about a sixth of it and let a
    rotation be judged sufficient that lands the successor back over the
    threshold. Scaled to the window like every section cap.
    """
    return sum(section_cap(fraction, window_tokens) for fraction in SECTION_FRACTIONS.values())


def replay_budget_chars(window_tokens: int | None) -> int:
    """Characters of conversation tail a session replay carries for *window_tokens*.

    80K chars at the 1M reference, 16K on a 200K model. This is the ONE tail
    figure: the replay renders under it, a rotation compaction cuts its verbatim
    tail at it and projects the successor's usage from it, so no second number
    can leave a row in neither the digest nor the tail. ``None`` is the 1M
    reference.
    """
    return max(1, round(REPLAY_BUDGET_CHARS * budget_base(window_tokens) / CONTEXT_BUDGET_BASE))
