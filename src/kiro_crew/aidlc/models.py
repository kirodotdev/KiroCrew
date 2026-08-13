"""Simplified models for project management — Activity and Comment only."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> float:
    return time.time()


@dataclass
class Activity:
    id: str = field(default_factory=_new_id)
    project_id: str = ""
    target_type: str = ""  # "project", "task", "comment"
    target_id: str = ""
    actor_id: str = "system"
    action: str = ""  # "created", "updated", "completed", "failed", etc.
    value: str = ""
    timestamp: float = field(default_factory=_now)


@dataclass
class Comment:
    id: str = field(default_factory=_new_id)
    target_type: str = ""  # "project", "task"
    target_id: str = ""
    content: str = ""
    author_id: str = "system"
    timestamp: float = field(default_factory=_now)


def serialize(obj) -> dict | list | str | int | float | bool | None:
    """Convert dataclass/enum to JSON-safe dict."""
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if isinstance(obj, (list, tuple)):
        return [serialize(x) for x in obj]
    return obj
