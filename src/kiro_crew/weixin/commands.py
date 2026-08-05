"""Weixin command parsing.

Commands:
  /new (or 新对话 / 清空)  — start a fresh session (advances the generation counter)
  /compact               — trigger context compaction

Per-conversation generation + awaiting-compact state lives in the shared
``messaging.conversation.ConversationState`` (re-exported so callers can import
it from this module, mirroring ``wecom.commands``).
"""

from __future__ import annotations

from kiro_crew.messaging.conversation import ConversationState  # noqa: F401

_NEW_ALIASES = frozenset(("/new", "新对话", "清空"))
_COMPACT_ALIASES = frozenset(("/compact",))


def parse_command(text: str) -> str | None:
    """Return 'new', 'compact', or None."""
    stripped = (text or "").strip()
    lower = stripped.lower()
    if lower in _NEW_ALIASES or stripped in _NEW_ALIASES:
        return "new"
    if lower in _COMPACT_ALIASES:
        return "compact"
    return None
