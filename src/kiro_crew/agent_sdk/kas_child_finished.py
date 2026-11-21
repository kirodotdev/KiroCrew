"""Whether an activity event marks a native KAS child call finished, for application code.

The agent-SDK boundary in front of ``acp.kas_child_finished``: the three
``PostToolUse`` surfaces (the dashboard turn, a channel turn and a sub-agent run)
import the predicate from here, never from the ACP layer.
"""

from __future__ import annotations

from kiro_crew.acp.kas_child_finished import is_finished_child_call

__all__ = ["is_finished_child_call"]
