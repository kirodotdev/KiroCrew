"""Shared transcript contract for return-triggered task brief rows."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

WARM_TASK_BRIEF_CLAIM_KIND = "warm_task_brief_claim"
WARM_TASK_BRIEF_PREPARING_KIND = "warm_task_brief_preparing"
WARM_TASK_BRIEF_RESULT_KIND = "warm_task_brief_result"
WARM_TASK_BRIEF_FAILURE_KIND = "warm_task_brief_failure"
WARM_TASK_BRIEF_MESSAGE_KINDS = frozenset(
    {
        WARM_TASK_BRIEF_PREPARING_KIND,
        WARM_TASK_BRIEF_RESULT_KIND,
        WARM_TASK_BRIEF_FAILURE_KIND,
    }
)
WARM_TASK_BRIEF_KINDS = WARM_TASK_BRIEF_MESSAGE_KINDS | {WARM_TASK_BRIEF_CLAIM_KIND}


def is_warm_task_brief_message(row: Mapping[str, Any]) -> bool:
    """Whether *row* belongs to a task brief, including its hidden claim."""
    meta = row.get("meta")
    # A client can persist any JSON as ``meta.kind``; an unhashable one must not
    # raise in the set lookup and abort every later replay of the session.
    kind = meta.get("kind") if isinstance(meta, Mapping) else None
    return isinstance(kind, str) and kind in WARM_TASK_BRIEF_KINDS
