"""Whether a send names the agent its slot takes."""

from __future__ import annotations

from typing import Any


def _send_binds_agent(agent: str, slot: Any) -> bool:
    """Whether a ``POST /api/chat`` naming *agent* writes it onto *slot*."""
    return bool(agent) and slot.agent in (None, "")
