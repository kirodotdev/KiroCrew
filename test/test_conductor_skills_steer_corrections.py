"""The conductor skills must send corrections to a busy worker with ``steer: true``.

A plain ``session_send`` to a busy target queues behind its running turn, so a
correction sent that way only arrives after the worker has spent the whole turn
going the wrong way. ``session_send`` accepts ``steer`` for exactly this;
these tests pin that the goal and pipeline conductor skills tell the conductor
to use it for corrections, keep it off for everything else, and warn that a
steer does not withdraw messages already queued.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.skills import _BUILTIN_SKILLS_DIR

GOAL_SKILL = Path(_BUILTIN_SKILLS_DIR) / "goal-conductor" / "SKILL.md"
PIPELINE_SKILL = Path(_BUILTIN_SKILLS_DIR) / "pipeline-conductor" / "SKILL.md"


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def _section(body: str, heading: str, next_heading: str) -> str:
    start = body.index(heading)
    end = body.index(next_heading, start + len(heading))
    return body[start:end]


def test_goal_conductor_steers_a_correction_into_a_running_item():
    section = _section(
        _flat(GOAL_SKILL), "### Goal changes mid-flight", "## When a conductor dispatched you"
    )
    assert "`session_send` the correction straight into it with `steer: true`" in section
    assert "**Steer corrections, queue everything else.**" in section
    # Everything that can wait keeps today's queue-after-turn delivery.
    assert "Leave `steer` off for answers to a `question`, seeds" in section
    # A steer is additive: what is already queued still runs.
    assert "A steer does not withdraw what is already queued" in section
    assert "`queue_depth`" in section


def test_pipeline_conductor_forwards_a_live_mode_change_as_a_steer():
    section = _section(_flat(PIPELINE_SKILL), "## Live steering", "## Merge, cleanup, reconcile")
    assert "forward it with `session_send` and `steer: true`" in section
    assert "Keep `steer` off for rulings, nudges" in section
    assert "A steer does not withdraw messages already queued" in section
