"""How long a harness's background launch keeps its session off the cleanup sweeps.

Claude Code runs a backgrounded command or a Workflow in the session's own process
tree after the prompt returns, and reports the launch on the launching tool call; it
reports nothing a client can read while that work runs, nor when it ends. So the RSS
recycle and the idle sweep treat the session as busy for a bounded time after the
newest launch: long enough for an ordinary workflow, short enough that a launch
cannot exempt a runaway tree from the memory ceiling for the rest of the session's
life. ``session.harness_background_hold_secs`` sets that time.
"""

from __future__ import annotations

import math

from kiro_crew.config.sections_defaults import DEFAULT_HARNESS_BACKGROUND_HOLD_SECS
from kiro_crew.config.sections_limits import (
    HARNESS_BACKGROUND_HOLD_SECS_MAX,
    HARNESS_BACKGROUND_HOLD_SECS_MIN,
)

#: The hold a session gets when ``session.harness_background_hold_secs`` is unset.
HARNESS_BACKGROUND_WORK_HOLD_SECS = float(DEFAULT_HARNESS_BACKGROUND_HOLD_SECS)


def background_hold_secs(cfg: object) -> float:
    """The configured hold, read from ``cfg.session`` at each sweep.

    The loader already clamps the value to its range. The bounds are applied again
    here because this is the one place both sweeps read it, and a value set on the
    live config object after load never passed the loader. Anything that is not a
    number reads as the default.
    """
    value = getattr(getattr(cfg, "session", None), "harness_background_hold_secs", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return HARNESS_BACKGROUND_WORK_HOLD_SECS
    return float(
        min(max(value, HARNESS_BACKGROUND_HOLD_SECS_MIN), HARNESS_BACKGROUND_HOLD_SECS_MAX)
    )


def _minutes(secs: float, *, up: bool = False) -> str:
    # The age rounds up so a launch just past the hold never reads as equal to it.
    minutes = math.ceil(secs / 60) if up else round(secs / 60)
    return f"{max(1, minutes)} min"


def hold_expired_reason(launch: tuple[float, str], hold_secs: float, *, orphaned: bool) -> str:
    """Why the idle sweep ended a session whose launched work outlived the hold.

    *launch* is ``(seconds since the newest launch, description)``; *orphaned* says the
    sweep ended it because its tab closed rather than on the idle clock. Shown in the
    chat as the recycle notice's reason and written to the gateway log.
    """
    age, label = launch
    why = "tab closed" if orphaned else "idle"
    return (
        f"{why}; its agent started background work {_minutes(age, up=True)} ago ({label}), "
        f"longer than the {_minutes(hold_secs)} hold set by "
        "session.harness_background_hold_secs, so that work may have been stopped"
    )
