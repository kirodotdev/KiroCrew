"""iMessage command parsing.

Commands:
  /new         -- start a fresh session (advances the generation counter)
  /compact     -- trigger context compaction
  /stop        -- stop the reply that is running (alias /cancel)
  /help        -- show available commands

Per-conversation generation + awaiting-compact state lives in the shared
``messaging.conversation.ConversationState`` (re-exported here so callers
can import it from this module, mirroring the other channel packages).

``HELP_TEXT`` is plain text with no markdown: iMessage renders none, so a
bulleted list written in asterisks would arrive as literal asterisks.
"""

from __future__ import annotations

from kiro_crew.messaging.conversation import ConversationState  # noqa: F401

_NEW_ALIASES = frozenset(("/new", "/start"))
_COMPACT_ALIASES = frozenset(("/compact",))
_STOP_ALIASES = frozenset(("/stop", "/cancel"))
_HELP_ALIASES = frozenset(("/help",))


def parse_command(text: str) -> str | None:
    """Return 'new', 'compact', 'stop', 'help', or None."""
    stripped = text.strip()
    cmd = stripped.split()[0].lower() if stripped.startswith("/") else ""
    if cmd in _NEW_ALIASES:
        return "new"
    if cmd in _COMPACT_ALIASES:
        return "compact"
    if stripped.lower() in _STOP_ALIASES:
        # The WHOLE message, not the first word: "/cancel my 3pm meeting" is an
        # instruction for the agent, not a request to stop the reply.
        return "stop"
    if cmd in _HELP_ALIASES:
        return "help"
    return None


HELP_TEXT = (
    "Kiro Crew commands\n"
    "/new — start a fresh conversation\n"
    "/compact — compress the conversation context\n"
    "/stop — stop the reply that is running\n"
    "/help — show this help\n\n"
    "Anything else is sent to the agent."
)
