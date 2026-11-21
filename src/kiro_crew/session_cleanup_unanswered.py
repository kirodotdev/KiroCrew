"""The idle sweep's unanswered-turn hooks.

Before an idle reset the sweep asks how old an unanswered user prompt on the
session is; after the reset succeeds it logs that age and asks the owner to
record that same prompt's turn as failed, so the chat shows why no answer came.
The owner is the dashboard (``kiro_crew.dashboard.idle_unanswered``), which holds
the slots and the transcript; a manager with no dashboard installs no hooks and
records nothing. Neither hook can stop or delay the reset: a probe that raises
or outlives ``PROBE_TIMEOUT_SECS`` reads as "no unanswered turn", and a failing
recorder is logged and ignored.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from kiro_crew.session_cleanup import CleanupDeps

PROBE_TIMEOUT_SECS = 5.0


@dataclass(frozen=True, slots=True)
class Unanswered:
    """An unanswered prompt the probe found: its age, and its identity, which the
    recorder compares so it never marks a prompt that arrived after the probe."""

    age: float
    prompt: object


class UnansweredHooks(Protocol):
    async def probe(self, key: str) -> Unanswered | None: ...

    async def mark_failed(self, key: str, prompt: object) -> None: ...


def no_hooks() -> UnansweredHooks | None:
    """Default ``CleanupDeps.unanswered_turn_hooks``: no dashboard, no hooks."""
    return None


async def turn_age(deps: CleanupDeps, key: str) -> Unanswered | None:
    """The unanswered prompt on *key*, or ``None``; bounded by the probe timeout."""
    hooks = deps.unanswered_turn_hooks()
    if hooks is None:
        return None
    try:
        return await asyncio.wait_for(hooks.probe(key), timeout=PROBE_TIMEOUT_SECS)
    except Exception:
        deps.logger.debug("unanswered-turn probe failed for %s", key, exc_info=True)
        return None


async def record_expiry(deps: CleanupDeps, key: str, found: Unanswered | None) -> None:
    """Log an expiry that left *found* unanswered and record its turn as failed."""
    if found is None:
        return
    deps.logger.error(
        "Idle sweep: session %s expired with unanswered turn (age: %.1fs)", key, found.age
    )
    hooks = deps.unanswered_turn_hooks()
    if hooks is None:
        return
    try:
        await hooks.mark_failed(key, found.prompt)
    except Exception:
        deps.logger.warning("Failed to mark unanswered turn failed for %s", key, exc_info=True)
