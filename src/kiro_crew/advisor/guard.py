"""Emission guard: dedupe, suppression, budget, escalation, cooldown.

Every validated reviewer note passes through :class:`EmissionGuard` before
delivery. The guard is the noise-control authority: repeated notes are
suppressed, content-free notes never emit, non-blockers respect a per-update
budget, a higher-severity duplicate escalates in place, and an interruption
starts a cooldown that only a new blocker may cross. Epoch boundaries clear
all guard state (dedupe, budget, cooldown) so a rewritten conversation is
judged fresh.
"""

from __future__ import annotations

import re
import time
from typing import Callable

from kiro_crew.advisor.output import SEVERITIES, AdvisorNote

#: Default cap on nits+concerns admitted per observation update.
DEFAULT_NON_BLOCKER_BUDGET = 4

#: Default seconds after an interruption during which non-blockers are held.
DEFAULT_COOLDOWN_SECS = 120.0

#: Default cap on blocker interruptions per observation epoch. The cooldown
#: spaces them out; this bounds how many one turn can take from a reviewer that
#: is wrong about several in a row. Past it a blocker is preserved as a card.
DEFAULT_INTERRUPTION_CAP = 3

_WS_RUN = re.compile(r"\s+")
#: Unicode-aware: a note written entirely in CJK/Cyrillic/Arabic for a
#: non-English session is real content; only genuinely symbol-only text
#: (punctuation, whitespace) is content-free.
_HAS_CONTENT = re.compile(r"\w", re.UNICODE)


def _normalize(text: str) -> str:
    return _WS_RUN.sub(" ", text).strip().casefold()


def _severity_rank(severity: str) -> int:
    return SEVERITIES.index(severity)


class EmissionGuard:
    """Admission control for advisor notes within one observation epoch."""

    def __init__(
        self,
        non_blocker_budget: int = DEFAULT_NON_BLOCKER_BUDGET,
        cooldown_secs: float = DEFAULT_COOLDOWN_SECS,
        clock: Callable[[], float] = time.monotonic,
        interruption_cap: int = DEFAULT_INTERRUPTION_CAP,
    ) -> None:
        self._non_blocker_budget = non_blocker_budget
        self._cooldown_secs = cooldown_secs
        self._interruption_cap = interruption_cap
        self._interruptions_this_epoch = 0
        self._clock = clock
        self._admitted: dict[str, str] = {}  # normalized text -> severity
        self._non_blockers_this_update = 0
        self._cooldown_until: float | None = None

    def admit(self, note: AdvisorNote) -> AdvisorNote | None:
        """Return the note (possibly escalated) if it may emit, else None."""
        if not _HAS_CONTENT.search(note.text):
            return None
        key = _normalize(note.text)
        previous = self._admitted.get(key)
        if previous is not None:
            if _severity_rank(note.severity) <= _severity_rank(previous):
                return None
            # Higher-severity duplicate escalates the existing note.
            self._admitted[key] = note.severity
            return note
        if note.severity != "blocker":
            if self._in_cooldown():
                return None
            if self._non_blockers_this_update >= self._non_blocker_budget:
                return None
            self._non_blockers_this_update += 1
        self._admitted[key] = note.severity
        return note

    def note_interruption(self) -> None:
        """Record that an admitted blocker interrupted the primary."""
        self._cooldown_until = self._clock() + self._cooldown_secs
        self._interruptions_this_epoch += 1

    def may_interrupt(self) -> bool:
        """Whether a blocker may still steer the running turn this epoch."""
        return self._interruptions_this_epoch < self._interruption_cap

    def begin_update(self) -> None:
        """Reset the per-update non-blocker budget."""
        self._non_blockers_this_update = 0

    def begin_epoch(self) -> None:
        """Clear all state at an observation epoch boundary."""
        self._admitted.clear()
        self._non_blockers_this_update = 0
        self._cooldown_until = None
        self._interruptions_this_epoch = 0

    def _in_cooldown(self) -> bool:
        return self._cooldown_until is not None and self._clock() < self._cooldown_until
