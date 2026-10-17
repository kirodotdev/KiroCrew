"""The verdict and stored-row builders the wake judge feedback tests share.

``test_wake_judge_feedback.py`` and its ``_service`` sibling import all three, and the
``_decision_log`` sibling imports ``_verdict``: ``_verdict``, an ``irq.Verdict`` with an
empty body; ``_suppressed``, the stored row of a verdict that withheld its turn; and
``_delivered``, the stored row of a verdict whose turn was confirmed delivered.
"""

from __future__ import annotations

from kiro_crew import irq


def _verdict(outcome: irq.Outcome) -> irq.Verdict:
    return irq.Verdict(outcome, body="")


def _suppressed(at: float, row_id: str = "", answered: bool = True) -> dict:
    """A stored row for a verdict that withheld the turn."""
    row: dict = {"outcome": "quiet", "at": at, "suppressed": True, "answered": answered}
    if row_id:
        row["id"] = row_id
    return row


def _delivered(at: float, row_id: str = "", outcome: str = "wake") -> dict:
    """A stored row for a verdict whose turn was confirmed delivered."""
    row: dict = {"outcome": outcome, "at": at, "delivered": True, "answered": True}
    if row_id:
        row["id"] = row_id
    return row
