"""Agent-queue priority tiers: the names and their order.

A leaf module on purpose. The dashboard validates, persists and restores a
chat's tier, and the task-queue dispatcher ranks lanes by it; importing the
names from ``kiro_crew.taskq`` would load the whole task store into every
dashboard import (``test_dashboard_handlers_lazy_tasks`` pins that it does not).
"""

from __future__ import annotations

#: Queue priority names, lowest first. The index is the tier's rank.
QUEUE_PRIORITIES: tuple[str, ...] = ("low", "medium", "high")
DEFAULT_QUEUE_PRIORITY = "medium"
_PRIORITY_RANK: dict[str, int] = {name: rank for rank, name in enumerate(QUEUE_PRIORITIES)}
DEFAULT_PRIORITY_RANK = _PRIORITY_RANK[DEFAULT_QUEUE_PRIORITY]


def normalize_queue_priority(value: object) -> str:
    """*value* as a known priority name; anything else is the default."""
    name = str(value or "").strip().lower()
    return name if name in _PRIORITY_RANK else DEFAULT_QUEUE_PRIORITY


def priority_rank(value: object) -> int:
    """Rank of a priority name (higher dispatches first); unknown is medium."""
    return _PRIORITY_RANK[normalize_queue_priority(value)]
