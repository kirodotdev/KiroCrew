"""Tests for :mod:`kiro_crew.dashboard._types`.

``_types`` exists so the dashboard's many modules share ONE ``TYPE_CHECKING``
block instead of repeating the same eight service imports. Two properties make
it worth its own test: the ``__all__`` contract (what a new module gets from
``from _types import *``), and the fact that none of those names may exist at
runtime -- binding them would drag the cron/session/history stacks into every
dashboard import and reintroduce the circular-import problem the module avoids.
"""

from __future__ import annotations

from kiro_crew.dashboard import _types


def test_all_names_the_shared_dashboard_services() -> None:
    """The re-export contract is the sorted-by-domain list of service types."""
    assert _types.__all__ == [
        "ContextBuilder",
        "CronService",
        "ConversationLog",
        "HistoryConsolidator",
        "LessonStore",
        "SessionManager",
        "SubagentManager",
        "TaskRunner",
    ]


def test_no_exported_name_is_bound_at_runtime() -> None:
    """Every name is TYPE_CHECKING-only, so none is a real module attribute.

    A regression here (an import escaping the ``if TYPE_CHECKING`` guard) would
    not fail type-checking, only slow -- or circularly break -- every dashboard
    import, so assert it directly.
    """
    bound = [name for name in _types.__all__ if hasattr(_types, name)]
    assert bound == [], f"runtime-bound names leaked out of TYPE_CHECKING: {bound}"
