"""Shared fixture and helpers for the wake judge suites.

``test_autonudge_wake_judge.py`` and its topic siblings (``test_autonudge_wake_judge_point.py``,
``test_autonudge_wake_judge_collectors.py``, ``test_autonudge_wake_judge_tick.py`` and
``test_autonudge_wake_judge_surfaces.py``) import what they use from here by name, and each
imports the autouse ``_restore_judge_process_globals`` so every wake judge test runs with
the judge's process globals restored. This is a plain module, not a conftest.
"""

from __future__ import annotations

import pytest

from kiro_crew import autonudge_judge as judge
from kiro_crew.autonudge import NudgeLoop
from kiro_crew.decisions.points import nudge_wake as point
from kiro_crew.decisions.types import Answer


@pytest.fixture(autouse=True)
def _restore_judge_process_globals():
    """Restore the judge module's process-wide stash state around every test.

    The ``_PR_BODIES*`` globals are process-wide, and a forgotten loss notice makes
    EVERY loop's take report a loss by design, so one test's overflow would leak into
    the next. Restored rather than cleared, so a test's own setup is kept.
    """
    saved = (
        dict(judge._PR_BODIES),
        dict(judge._PR_BODIES_DROPPED),
        judge._PR_BODIES_FORGOTTEN,
        judge._PR_BODIES_DROPPED_TOTAL,
        judge._PR_BODIES_FORGOTTEN_TAKES,
    )
    try:
        yield
    finally:
        judge._PR_BODIES.clear()
        judge._PR_BODIES.update(saved[0])
        judge._PR_BODIES_DROPPED.clear()
        judge._PR_BODIES_DROPPED.update(saved[1])
        judge._PR_BODIES_FORGOTTEN = saved[2]
        judge._PR_BODIES_DROPPED_TOTAL = saved[3]
        judge._PR_BODIES_FORGOTTEN_TAKES = saved[4]


def _owner_dashboard_identity():
    """The owner's dashboard claims, for the routes the owner gate covers.

    ``app == ""`` with the owner's subject: ``state.owner_id`` when one is
    configured, else the signed local bootstrap subject.
    """
    from aiohttp import web

    @web.middleware
    async def middleware(request, handler):
        state = request.app.get("state")
        request["user"] = str(getattr(state, "owner_id", "") or "") or "local-app"
        request["app"] = ""
        return await handler(request)

    return middleware


def answers(
    owner: str = point.NEEDS_OWNER_QUIET,
    owner_p: float = 0.9,
    outcome: str = point.OUTCOME_PROGRESS_ONLY,
    outcome_p: float = 0.9,
    urgency: str = point.URGENCY_NONE,
) -> dict[str, Answer]:
    """One complete, in-domain answer set."""
    return {
        point.Q_NEEDS_OWNER: Answer(point.Q_NEEDS_OWNER, owner, owner_p),
        point.Q_OUTCOME: Answer(point.Q_OUTCOME, outcome, outcome_p),
        point.Q_URGENCY: Answer(point.Q_URGENCY, urgency, 0.9),
    }


def _loop(brief: dict | None = None, *, gate: bool = True) -> NudgeLoop:
    loop = NudgeLoop(id="l1", slot_key="chat-1-1", message="watch chat-2-2")
    loop.judge = dict(brief or {})
    # GATED, because that is the population the judge screens: monitor_start's own
    # directive gates unless the caller passes `gate=false`, and an ungated loop is
    # one whose duty is to act while its subject is quiet. A helper that left this
    # False would be testing the bypass in every case that meant to test the judge.
    loop.gate = gate
    return loop
