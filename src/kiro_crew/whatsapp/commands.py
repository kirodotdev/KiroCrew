"""WhatsApp command parsing.

Commands (operator only — the transport marks non-operator senders and the
dispatcher refuses steering for them):
  /new      — start a fresh session (advances the generation counter)
  /compact  — trigger context compaction

Per-conversation generation + awaiting-compact state lives in the shared
``messaging.conversation.ConversationState`` (re-exported so callers can
import it from this module, mirroring ``weixin.commands``).
"""

from __future__ import annotations

from kiro_crew.messaging.conversation import ConversationState  # noqa: F401

_NEW_ALIASES = frozenset(("/new",))
_COMPACT_ALIASES = frozenset(("/compact",))


def parse_command(text: str) -> str | None:
    """Return 'new', 'compact', or None."""
    lower = (text or "").strip().lower()
    if lower in _NEW_ALIASES:
        return "new"
    if lower in _COMPACT_ALIASES:
        return "compact"
    return None
