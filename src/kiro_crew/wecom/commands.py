"""WeCom command parsing.

Commands:
  /new (or 新对话 / 清空)  — start a fresh session (advances the generation counter)
  /compact               — trigger context compaction

Per-conversation generation + awaiting-compact state lives in the shared
``messaging.conversation.ConversationState`` (re-exported here so existing
callers importing it from this module keep working).
"""

from __future__ import annotations

from kiro_crew.messaging.conversation import ConversationState  # noqa: F401

# ── Command constants ──

_NEW_ALIASES = frozenset(("/new", "新对话", "清空"))
_COMPACT_ALIASES = frozenset(("/compact",))
_LINK_ALIASES = frozenset(("/link",))
_UNLINK_ALIASES = frozenset(("/unlink",))


def parse_command(text: str) -> str | None:
    """Return 'new', 'compact', 'link', 'unlink', or None."""
    stripped = text.strip()
    lower = stripped.lower()
    if lower in _NEW_ALIASES or stripped in _NEW_ALIASES:
        return "new"
    if lower in _COMPACT_ALIASES:
        return "compact"
    if lower in _LINK_ALIASES:
        return "link"
    if lower in _UNLINK_ALIASES:
        return "unlink"
    return None
