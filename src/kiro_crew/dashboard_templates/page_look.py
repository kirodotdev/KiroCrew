"""What a crewmate's own Dashboard page aims for, said once for every surface.

The first-build line (``goal_reminder.DASHBOARD_BUILD_LINE``) and the
``dashboard_save`` view description (``mcp_panel``) both carry :data:`PAGE_LOOK`
verbatim, so the two places a crewmate reads before it writes a page cannot drift
into different rules. The dashboard skill says the same at length in its "A page
worth returning to" section. Principles only: no theme, object, palette, library or
layout is named, and the honesty rules (recorded values, a real measure for
progress, plain words, no internal ids or paths) are part of it.
"""

from __future__ import annotations

from typing import Final

#: The look a crewmate's page aims for, with the claims it may make.
PAGE_LOOK: Final[str] = (
    "Aim for a page worth returning to, not just a tidy set of numbers. Carry the "
    "user's chosen style forward; with style left open, choose your own theme from "
    "the project and its context. Backgrounds, wallpaper, texture and expressive "
    "frames can give the whole page character. Subtle ambient motion and animation "
    "run constantly while the page is in view, not only when a value changes, and "
    "give it presence; data-driven motion on top reveals real changes. Keep "
    "effects light on resources and pause them while hidden. Reduced motion is only "
    "for a reader who set it: under prefers-reduced-motion: reduce every value jumps "
    "to its target and ambient layers hold still, and every other reader gets the "
    "motion. Charts, comparisons and state "
    "distributions can use recorded values; progress needs a real measure, not an "
    "arbitrary fraction assigned to running or idle. Words, numbers and graphics "
    "agree with the folded numbers; plain words explain each phase and state, keeping "
    "internal ids, session keys and file-system paths out of view, and free text an "
    "agent wrote into a fold is shown as a short plain sentence. Pair surface and text "
    "colours in light and dark, using host or custom variables. Pair type rather "
    "than name it: at most two families from the role variables var(--font-sans), "
    "var(--font-serif) and var(--font-mono), one that reads for headings and body and "
    "one for numbers, code or ids; monospace only for code, ids and aligned figures; "
    "hierarchy from size, weight and colour; tabular-nums to align figures; text in "
    "any one script in one style, since the generic families pick each script's "
    "system face; a style the user names wins. Give long content "
    "room to fit the width from 320px upward: a label and its value wrap or stack, "
    "and nothing is clipped or scrolls sideways. Judge both the whole picture and "
    "the view of the work, including when data stands still."
)
