"""Telegram command parsing.

Commands:
  /new         — start a fresh session (advances the generation counter)
  /compact     — trigger context compaction
  /model       — pick the model from an inline-button list
  /yolo        — auto-approve every tool for a bounded window
  /link        — resume mirroring dashboard replies here (on by default)
  /unlink      — stop mirroring dashboard replies here
  /stop        — stop the current reply and clear the queue (alias: /cancel)
  /help        — show available commands

Mid-turn overrides (prefix a message sent WHILE a reply is running; they
override the global ``messaging.queue_mode`` for that one message):
  /queue <msg> — hold this message and answer it after the current turn
  /steer <msg> — fold this message into the running turn right now

``COMMAND_SPEC`` is the single source of truth behind both the ``/help`` card
and the Bot API ``/`` menu, so the two cannot drift apart.

Per-conversation generation + awaiting-compact state lives in the shared
``messaging.conversation.ConversationState`` (re-exported here so existing
callers importing it from this module keep working).
"""

from __future__ import annotations

import re

from kiro_crew.messaging.conversation import ConversationState  # noqa: F401

# ── Command constants ──

_NEW_ALIASES = frozenset(("/new", "/start"))
_COMPACT_ALIASES = frozenset(("/compact",))
_HELP_ALIASES = frozenset(("/help",))
_LINK_ALIASES = frozenset(("/link",))
_UNLINK_ALIASES = frozenset(("/unlink",))
_STOP_ALIASES = frozenset(("/stop", "/cancel"))
# ``/models`` (plural) is a typo-safe alias, not a separate command: without it
# the message falls through to the model as ordinary chat text, which reads as
# "the feature isn't installed" rather than "you typed it wrong".
_MODEL_ALIASES = frozenset(("/model", "/models"))
_YOLO_ALIASES = frozenset(("/yolo",))


def parse_command(text: str) -> str | None:
    """Return the command name for *text*, or None when it is not a command."""
    stripped = text.strip()
    # Telegram commands always start with /
    cmd = stripped.split()[0].lower() if stripped.startswith("/") else ""
    if cmd in _NEW_ALIASES:
        return "new"
    if cmd in _COMPACT_ALIASES:
        return "compact"
    if cmd in _LINK_ALIASES:
        return "link"
    if cmd in _UNLINK_ALIASES:
        return "unlink"
    if cmd in _MODEL_ALIASES:
        return "model"
    if cmd in _YOLO_ALIASES:
        return "yolo"
    if cmd in _HELP_ALIASES:
        return "help"
    if cmd in _STOP_ALIASES:
        return "stop"
    return None


def parse_command_argument(text: str) -> str:
    """Return the text following a command token (``""`` when there is none)."""
    parts = text.strip().split(None, 1)
    return parts[1].strip() if len(parts) == 2 else ""


_QUEUE_ALIASES = frozenset(("/queue",))
_STEER_ALIASES = frozenset(("/steer",))


def parse_mid_turn_override(text: str) -> tuple[str | None, str]:
    """Detect a per-message mid-turn override.

    ``/queue <msg>`` forces the message to be queued (answered after the current
    turn); ``/steer <msg>`` forces it to steer the running turn. Each overrides
    the global ``messaging.queue_mode`` for THIS message only. Returns
    ``(mode, rest)`` with the directive stripped -- ``mode`` is ``"queue"`` or
    ``"steer"`` -- or ``(None, text)`` when there is no directive (or the
    directive carries no message body, e.g. a bare ``/queue``).
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


def is_bare_mid_turn_override(text: str) -> bool:
    """True for a lone ``/queue`` / ``/steer`` carrying no message body.

    Those two are prefixes, not standalone commands, so the bare token matches
    neither :func:`parse_command` nor :func:`parse_mid_turn_override` and would
    otherwise reach the model as ordinary chat text — the user sees an answer to
    the literal string "/queue" instead of being told they left the message off.
    """
    parts = text.strip().split()
    return len(parts) == 1 and parts[0].lower() in (_QUEUE_ALIASES | _STEER_ALIASES)


# ── Command catalogue (help card + Bot API menu) ──

#: Ordered ``(command, description)`` rows rendered by BOTH ``/help`` and the
#: Bot API ``/`` menu. Names carry no leading slash because ``setMyCommands``
#: rejects one. ``/queue`` and ``/steer`` are deliberately absent: the Telegram
#: client SENDS a menu entry on tap, and a bare ``/queue`` has no message body
#: to act on, so listing them would put a dead entry in the menu — they stay
#: documented in the help card's footer instead.
COMMAND_SPEC: tuple[tuple[str, str], ...] = (
    ("new", "Start a fresh conversation"),
    ("compact", "Compress the context when it gets long"),
    ("model", "Choose the model from a list"),
    ("yolo", "Auto-approve every tool for a while (on / off / renew)"),
    ("link", "Resume mirroring dashboard replies here (on by default)"),
    ("unlink", "Stop mirroring dashboard replies here"),
    ("stop", "Stop the current reply and clear the queue"),
    ("help", "Show the command list"),
)

# Bot API limits on a setMyCommands entry.
_BOT_COMMAND_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_BOT_COMMAND_DESC_LIMIT = 256
_BOT_COMMAND_MAX = 100


def bot_command_payload() -> list[dict[str, str]]:
    """``COMMAND_SPEC`` shaped as a Bot API ``setMyCommands`` array.

    Rows that violate the Bot API's own constraints (name ``[a-z0-9_]{1,32}``,
    non-empty description) are skipped rather than sent: Telegram rejects the
    WHOLE array on a single bad row, so one malformed entry would otherwise cost
    the user the entire menu.
    """
    rows: list[dict[str, str]] = []
    for name, desc in COMMAND_SPEC:
        if not _BOT_COMMAND_RE.match(name) or not desc:
            continue
        rows.append({"command": name, "description": desc[:_BOT_COMMAND_DESC_LIMIT]})
        if len(rows) >= _BOT_COMMAND_MAX:
            break
    return rows


_HELP_HEADER = "🦞 Kiro Crew — Telegram"
_HELP_FOOTER = (
    "While a reply is running, prefix a message to control it:\n"
    "/queue <msg> — answer it after the current turn\n"
    "/steer <msg> — fold it into the running turn now\n"
    "\n"
    "Just send a message to chat. Replies stream in real-time."
)


def build_help_text() -> str:
    """Render the ``/help`` card from :data:`COMMAND_SPEC`."""
    lines = [_HELP_HEADER, "", "Commands:"]
    lines += [f"/{name} — {desc}" for name, desc in COMMAND_SPEC]
    lines += ["", _HELP_FOOTER]
    return "\n".join(lines)
