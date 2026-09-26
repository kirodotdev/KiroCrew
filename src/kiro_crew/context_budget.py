"""The context figures a rotation compaction projects from, in a module light enough to import anywhere.

Two numbers every context consumer agrees on. ``context`` derives its section
caps from ``CONTEXT_BUDGET_BASE`` and walks a replay under
``replay_budget_chars``; the compaction coordinator (``session_compaction``,
imported by ``kiro_crew.session`` on every CLI invocation) projects a rotation
from ``replay_budget_chars`` and ``startup_context_chars``. Keeping them here
lets both import at module scope: ``context`` itself pulls ~200 further modules
that a ``kirocrew <cmd>`` never needs, and ``context`` imports THIS module,
never the other way round.

The admission budget is Crew-owned background context, in characters, and is
independent of the model window: a larger window does not enlarge the
discretionary pool. The replay tail is a separate allowance and IS
window-scaled, down to a fifth of its reference figure on the smallest real
window and never above it.
"""

from __future__ import annotations

#: Crew-owned background admission, in characters, at any window. ``context``
#: derives every section cap from it and enforces it as the one shared budget
#: (``_ResolvedCaps.max_context``): section limits do not add capacity. Not a
#: bound on the provider's full model input and not a token estimate.
CONTEXT_BUDGET_BASE = 33_000

#: Startup allowance for the authored standing-rule tier, admitted OUTSIDE the
#: discretionary pool above: standing rules are not discretionary, so a fresh
#: session receives up to both. Window-independent like the pool. The value
#: restores the allowance a 1M-window session had before the pool was pinned to
#: its smallest-window value (165_000 * 0.226 = 37_290).
LESSONS_STARTUP_CAP = 37_000

#: Startup allowance for the ``pref.*`` semantic rows a fresh session reads
#: complete, admitted beside the pool like the rule allowance above and
#: window-independent like it: the semantic share (7.7%) of the 165_000
#: reference base a 1M-window session had before the pool was pinned
#: (165_000 * 0.077 = 12_705).
PREFS_STARTUP_CAP = 12_700

#: The window the replay tail budget was tuned for.
REFERENCE_WINDOW_TOKENS = 1_000_000

#: Kept for the readers that floor a scaled base: the base is window-independent,
#: so its floor is itself.
MIN_CONTEXT_BUDGET_BASE = CONTEXT_BUDGET_BASE

#: Conversation tail a session replay carries at the reference window: 80K chars
#: is about 20K tokens, which fits beside the system context on a 200K window.
REPLAY_BUDGET_CHARS = 80_000

#: The replay tail shrinks with the window but never below this share of the
#: reference figure: 20% is the 200K tier, the smallest real model window.
REPLAY_SCALE_FLOOR = 0.2


def effective_window(window_tokens: int | None) -> int:
    """Resolve a usable context-window size, defaulting to the reference (1M).

    A ``None``/unset or non-positive window falls back to the reference window,
    NOT to a small default. This is deliberate: the default deployment runs
    ``provider=acp`` + ``model="auto"``, and the registry maps ``"auto"`` to 200K
    even though ACP auto actually runs a 1M-window model. Treating an
    unknown/auto window as the reference means ONLY an explicitly-selected
    smaller model scales the replay down: an unresolved window never silently
    shrinks the default deployment to 20%.
    """
    if not window_tokens or window_tokens <= 0:
        return REFERENCE_WINDOW_TOKENS
    return window_tokens


def replay_scale(window_tokens: int | None) -> float:
    """The factor the replay's per-window figures scale by: window over reference, in [0.2, 1.0].

    One function for the tail budget and the per-row ``inject`` ceiling, so the
    two shrink together and a small window cannot clip every inject row to
    nothing while still carrying a full tail. Larger windows cannot enlarge the
    tail beyond the reference allowance.
    """
    return max(
        REPLAY_SCALE_FLOOR, min(1.0, effective_window(window_tokens) / REFERENCE_WINDOW_TOKENS)
    )


def replay_budget_chars(window_tokens: int | None) -> int:
    """Characters of conversation tail a session replay carries for *window_tokens*.

    80K chars at the 1M reference, 16K on a 200K model. This is the ONE tail
    figure: the replay renders under it, a rotation compaction cuts its verbatim
    tail at it and projects the successor's usage from it, so no second number
    can leave a row in neither the digest nor the tail. ``None`` is the 1M
    reference.
    """
    return round(REPLAY_BUDGET_CHARS * replay_scale(window_tokens))


def startup_context_chars(window_tokens: int | None) -> int:
    """Ceiling, in characters, on the session context a fresh session on *window_tokens* receives.

    A rotation compaction re-seeds a fresh session and projects where the
    successor starts from this figure plus the tail it carries. It is the three
    allowances ``build_session_context`` admits a fresh session up to: the
    shared discretionary pool (``_resolve_caps(window).max_context``), the
    standing-rule tier admitted beside it (``lessons_startup``) and the
    ``pref.*`` rows admitted beside it (``prefs_startup``). All are
    window-independent, so the window is taken for the contract and does not
    change the answer. A thread's own history allowance and provider-side
    overhead sit outside it; the successor's first confirmed reading settles
    what the projection could not see.
    """
    return CONTEXT_BUDGET_BASE + LESSONS_STARTUP_CAP + PREFS_STARTUP_CAP
