"""Advisor usage attribution helpers.

A reviewer turn is real model spend and must be visible, but it is not the
parent's turn: the row is keyed by the reviewer session's own stable
synthetic key (``advisor:<parent session key>``) and tagged with the ``advisor`` surface --
those two existing fields attribute the spend; the parent link is carried by
the advisory row itself, not by the usage row.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.advisor.runtime import ReviewerSession

#: Dispatch-origin tag advisor reviewer rows carry in the usage store.
ADVISOR_USAGE_SURFACE = "advisor"


def advisor_usage_kwargs(session: ReviewerSession) -> dict[str, Any]:
    """Keyword arguments for persisting one reviewer turn's usage row.

    Spread into ``persist_token_record_async`` alongside the model/event the
    reviewer turn produced.
    """
    return {
        "slot_key": session.session_id,
        "surface": ADVISOR_USAGE_SURFACE,
    }
