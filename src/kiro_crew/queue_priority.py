"""Agent-queue priority tiers: the names and their order.

A leaf module on purpose. The dashboard validates a chat's tier, and the
task-queue dispatcher ranks lanes by it; importing the names from
``kiro_crew.taskq`` would load the whole task store into every dashboard import
(``test_dashboard_handlers_lazy_tasks`` pins that it does not).

A tier lives in gateway memory only and every chat starts at Medium after a
restart. Transcript metadata is a file the agent's own tools can write, so a
tier read back from it would let a prompt-injected agent move its chat between
tiers past the owner-only PATCH gate: raise it to High, or undo an owner's Low
(the same reason ``jev_route`` is kept in memory only).
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
