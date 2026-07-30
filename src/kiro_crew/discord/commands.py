"""Discord command parsing.

Commands (``!``-prefixed — Discord's client swallows bare ``/`` messages into
its slash-command UI, so text commands use ``!``; ``/``-prefixed forms are
also accepted for muscle-memory parity with Telegram):

  !new         — start a fresh session (advances the generation counter)
  !compact     — trigger context compaction
  !link        — mirror this conversation's dashboard tab back here
  !unlink      — stop mirroring
  !stop        — stop the current reply and clear the queue (alias: !cancel)
  !help        — show available commands

Mid-turn overrides (prefix a message sent WHILE a reply is running; they
override the global ``messaging.queue_mode`` for that one message):
  !queue <msg> — hold this message and answer it after the current turn
  !steer <msg> — fold this message into the running turn right now

Per-conversation generation + awaiting-compact state lives in the shared
``messaging.conversation.ConversationState`` (re-exported here to mirror the
Telegram module's layout).
"""

from __future__ import annotations

from kiro_crew.messaging.conversation import ConversationState  # noqa: F401

# ── Command constants (each accepts ! and / prefixes) ──

_NEW_ALIASES = frozenset(("!new", "!start", "/new", "/start"))
_COMPACT_ALIASES = frozenset(("!compact", "/compact"))
_HELP_ALIASES = frozenset(("!help", "/help"))
_LINK_ALIASES = frozenset(("!link", "/link"))
_UNLINK_ALIASES = frozenset(("!unlink", "/unlink"))
_STOP_ALIASES = frozenset(("!stop", "!cancel", "/stop", "/cancel"))
# ``!session`` (singular) is a typo-safe alias, not a separate command. Without
# it the message falls through to the LLM as ordinary chat text, which reads as
# "the feature isn't installed" rather than "you typed it wrong" — and because
# parse_command matches only the FIRST token, a trailing phrase (e.g.
# "!session link to a certain session") resolves here too instead of being sent
# to the model.
_SESSIONS_ALIASES = frozenset(("!sessions", "/sessions", "!session", "/session"))

_PREFIXES = ("!", "/")


def parse_command(text: str) -> str | None:
    """Return 'new', 'compact', 'link', 'unlink', 'help', 'stop', 'sessions', or None."""
    stripped = text.strip()
    cmd = stripped.split()[0].lower() if stripped.startswith(_PREFIXES) and stripped.split() else ""
    if cmd in _NEW_ALIASES:
        return "new"
    if cmd in _COMPACT_ALIASES:
        return "compact"
    if cmd in _LINK_ALIASES:
        return "link"
    if cmd in _UNLINK_ALIASES:
        return "unlink"
    if cmd in _SESSIONS_ALIASES:
        return "sessions"
    if cmd in _HELP_ALIASES:
        return "help"
    if cmd in _STOP_ALIASES:
        return "stop"
    return None


_QUEUE_ALIASES = frozenset(("!queue", "/queue"))
_STEER_ALIASES = frozenset(("!steer", "/steer"))


def parse_mid_turn_override(text: str) -> tuple[str | None, str]:
    """Detect a per-message mid-turn override.

    ``!queue <msg>`` forces the message to be queued (answered after the current
    turn); ``!steer <msg>`` forces it to steer the running turn. Each overrides
    the global ``messaging.queue_mode`` for THIS message only. Returns
    ``(mode, rest)`` with the directive stripped -- ``mode`` is ``"queue"`` or
    ``"steer"`` -- or ``(None, text)`` when there is no directive (or the
    directive carries no message body, e.g. a bare ``!queue``).
    """
    parts = text.lstrip().split(None, 1)
    if len(parts) != 2:  # needs a directive AND a message body
        return None, text
    cmd, rest = parts[0].lower(), parts[1]
    if cmd in _QUEUE_ALIASES:
        return "queue", rest
    if cmd in _STEER_ALIASES:
        return "steer", rest
    return None, text
