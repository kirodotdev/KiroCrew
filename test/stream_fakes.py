"""Shared doubles for tests that stand in for ``stream_and_collect``."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


def report_finished_turn(kwargs: dict[str, Any]) -> None:
    """Report the completion a real collector hears before it returns.

    ``stream_and_collect`` passes the backend's completion event to its
    ``on_complete`` callback, and a workflow step keeps its session past the
    step only on that evidence (``repo_checkout_guard.TrackedWork.turn_ended``).
    A double that returns text without reporting it models a turn the backend
    may still be running, which ends the step's session.
    """
    on_complete = kwargs.get("on_complete")
    if on_complete is not None:
        on_complete(SimpleNamespace(stop_reason="end_turn", synthetic_completion=False))
