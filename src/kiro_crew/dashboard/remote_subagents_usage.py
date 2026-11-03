"""What a remote run's peer reports back, carried into the hub's shadow record.

Split out of ``remote_subagents.py`` (file line cap); that module imports both
names, so its callers are unchanged.
"""

from __future__ import annotations

import math
from typing import Any

from kiro_crew.subagent import SubagentInfo


def _peer_count(raw: object) -> float | None:
    """*raw* as a finite, non-negative number, or None when it is not one."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        value = float(raw)
    except OverflowError:
        # A peer's JSON can carry an int no float holds (10**400); letting it
        # raise would kill the monitor before the run's terminal record lands.
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _take_peer_usage(info: SubagentInfo, payload: dict[str, object]) -> None:
    """Carry the peer's terminal usage into the shadow record.

    The peer reports ``credits`` and, for a completed run, an explicit
    ``made_no_tool_calls`` once the run is done. Without them the shadow keeps
    zero cost, and a run whose tools were all pre-approved on the peer (so no
    request was relayed here) reads as one that called no tool at all. It also
    reports ``leaked_tool_call``: the run ended on a tool call written as prose,
    which never ran, and the parent's completion report must say so.
    """
    credits = _peer_count(payload.get("credits"))
    if credits is not None:
        info.credits = credits
    if payload.get("made_no_tool_calls") is False:
        # The peer saw a tool call; the count itself is the peer's, so only
        # the evidence that there was one is recorded here.
        info.tool_count = max(int(info.tool_count or 0), 1)
    if payload.get("leaked_tool_call") is True:
        info.leaked_tool_call = True


def _still_registered(state: Any, instance_id: str) -> bool:
    """False only when the registry is readable and does not name *instance_id*.

    An owed stop is retried until its crew confirms; a crew the operator removed
    never will. Blocking (the registry reads its file), so call it off the loop.
    An unreadable or absent registry answers True, so the stop keeps trying.
    """
    registry = getattr(state, "instances_registry", None)
    if registry is None:
        return True
    try:
        return registry.get(instance_id) is not None
    except Exception:
        return True
