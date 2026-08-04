"""Webex command parsing.

Commands:
  /new         — start a fresh session (advances the generation counter)
  /compact     — trigger context compaction
  /help        — show available commands

Per-conversation generation + awaiting-compact state lives in the shared
``messaging.conversation.ConversationState`` (re-exported here so callers
can import it from this module, mirroring the Telegram/WeCom packages).
"""

from __future__ import annotations

from kiro_crew.messaging.conversation import ConversationState  # noqa: F401

_NEW_ALIASES = frozenset(("/new", "/start"))
_COMPACT_ALIASES = frozenset(("/compact",))
_HELP_ALIASES = frozenset(("/help",))


def parse_command(text: str) -> str | None:
    """Return 'new', 'compact', 'help', or None."""
    stripped = text.strip()
    cmd = stripped.split()[0].lower() if stripped.startswith("/") else ""
    if cmd in _NEW_ALIASES:
        return "new"
    if cmd in _COMPACT_ALIASES:
        return "compact"
    if cmd in _HELP_ALIASES:
        return "help"
    return None


HELP_TEXT = (
    "**Kiro Crew commands**\n"
    "- `/new` — start a fresh conversation\n"
    "- `/compact` — compress the conversation context\n"
    "- `/help` — show this help\n\n"
    "Anything else is sent to the agent."
)
