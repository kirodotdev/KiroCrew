"""The Agent-call stamp claude-agent-acp puts on a native sub-agent's frames.

One reader for ``_meta.claudeCode.parentToolUseId``, shared by the native-child
reading (``client_native_children``) and the batch window feed
(``client_tool_events``), which must agree on which frames are a child's.
"""

from __future__ import annotations

from typing import Any


def parent_tool_use(update: dict[str, Any]) -> str:
    """The Agent call a frame is stamped with, or ``""`` for an unstamped frame.

    The adapter stamps ``parentToolUseId`` as a string only on a subagent's frames;
    the parent's own frames carry none, or carry it as null.
    """
    meta = update.get("_meta")
    claude_meta = meta.get("claudeCode") if isinstance(meta, dict) else None
    value = claude_meta.get("parentToolUseId") if isinstance(claude_meta, dict) else None
    return value if isinstance(value, str) else ""
