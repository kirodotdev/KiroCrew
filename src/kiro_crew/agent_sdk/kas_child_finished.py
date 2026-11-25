"""Whether an activity event marks a native KAS child call finished, for application code.

The agent-SDK boundary in front of ``acp.kas_child_finished``: the three
``PostToolUse`` surfaces (the dashboard turn, a channel turn and a sub-agent run)
import the predicate from here, never from the ACP layer. Only a KAS session sends
that event shape, so the answer is also keyed on the session's backend, by positive
identity: a same-shaped event on any other backend is not a finished child.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.acp.kas_child_finished import is_finished_child_call as _marked
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.agent_sdk.capabilities import capabilities_of


def is_finished_child_call(event: Any, provider: Any) -> bool:
    """Whether *event*, on *provider*'s session, is a KAS child's marked finished call."""
    return capabilities_of(provider).backend == ACP_BACKEND_KAS and _marked(event)


__all__ = ["is_finished_child_call"]
