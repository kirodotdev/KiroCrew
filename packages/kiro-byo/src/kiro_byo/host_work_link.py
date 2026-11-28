"""Optional, read-only Crew work correlation for BYO execution.

A Crew-owned task/assignment must never become a second BYO scheduler or an
authorization token.  The link is supplied only by the ACP host's session
metadata and is used to correlate BYO session events and durable Runs.

This uses a new, opt-in `_meta.kiro.byoWorkLink` namespace.  Unmodified Crew
builds need not send it and retain exactly their existing behavior.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

_ALLOWED_KEYS = frozenset({"taskId", "assignmentId", "role", "parentSessionId"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_ROLES = frozenset({"conductor", "worker", "background", "session"})


def validate_work_link(value: Any) -> dict[str, str]:
    """Allowlist host metadata, refusing malformed/unbounded identifiers.

    Unlike a tool permission, this information is purely diagnostic. Never
    include it in model instructions or use it to bypass a Crew consent check.
    """
    if not isinstance(value, Mapping):
        raise ValueError("byoWorkLink must be an object")
    if set(value) - _ALLOWED_KEYS:
        raise ValueError("byoWorkLink contains unsupported keys")
    result: dict[str, str] = {}
    for key in ("taskId", "assignmentId", "role", "parentSessionId"):
        if key not in value:
            continue
        item = value[key]
        if not isinstance(item, str):
            raise ValueError(f"byoWorkLink.{key} must be a string")
        if key == "role":
            if item not in _ROLES:
                raise ValueError("byoWorkLink.role is not recognized")
        elif not _IDENTIFIER.fullmatch(item):
            raise ValueError(f"byoWorkLink.{key} must be an opaque identifier (max 128 ASCII chars)")
        result[key] = item
    # Any nonempty correlation must identify a task. A role or parent alone
    # cannot be attributed to Crew work and must not look like a valid link.
    # The empty object is reserved for an explicit detach.
    if result and "taskId" not in result:
        raise ValueError("byoWorkLink nonempty correlation requires taskId")
    return result


def work_link_from_params(params: Mapping[str, Any]) -> tuple[bool, dict[str, str]]:
    """Return (explicitly supplied, value). An absent extension is no change.

    An empty object is an explicit detach, useful after Crew reassigns a task.
    A legacy ACP client without the extension never gains inferred task IDs.
    """
    meta = params.get("_meta")
    kiro = meta.get("kiro") if isinstance(meta, Mapping) else None
    if not isinstance(kiro, Mapping) or "byoWorkLink" not in kiro:
        return False, {}
    return True, validate_work_link(kiro["byoWorkLink"])


def projected_work_link(value: Any) -> dict[str, str]:
    """Sanitize old/stored data before returning a public diagnostic view."""
    try:
        return validate_work_link(value)
    except (ValueError, TypeError):
        return {}
