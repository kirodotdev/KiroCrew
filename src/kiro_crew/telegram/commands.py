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
  /kirocrew dashboard [<N>h|<N>m] — send a presigned dashboard login link

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
# Two-token command: ``/kirocrew dashboard [<TTL>]``. The bare ``/kirocrew``
# token is deliberately NOT a command on its own (see parse_command).
_DASHBOARD_ALIASES = frozenset(("/kirocrew",))

# Telegram bot usernames: 5-32 chars, alphanumeric + underscore, by convention
# ending in "bot" -- but any alnum/underscore run after @ is accepted here
# rather than hardcoding that suffix, since the client appends whatever the
# bot's actual registered username is. Captured (not just matched) so the
# caller can check it against the bot's OWN username before stripping.
_BOT_MENTION_RE = re.compile(r"@([A-Za-z0-9_]+)$")


def _strip_bot_mention(cmd: str, bot_username: str) -> str:
    """Strip a trailing ``@BotUsername`` from a command token -- but ONLY when
    it names *this* bot.

    Telegram's own clients (mobile/desktop) append ``@BotUsername`` to a slash
    command in any chat with more than one participant/bot -- e.g.
    ``/new@KiroCrewBot`` instead of bare ``/new``. This is standard,
    documented Bot API client behavior triggered by registering a command
    menu (``set_my_commands``, called at gateway startup), not something this
    codebase's UI controls. Every alias set in this module is defined without
    the suffix, so without any stripping every command silently fell through
    to being sent to the LLM as ordinary chat text in exactly the multi-user
    surface (a Telegram forum-topic supergroup) this integration exists to
    support.

    Telegram delivers a command addressed to another bot in the same group to
    every bot present in it (Bot API convention: a bot ignores what is not
    addressed to it). Stripping any mention unconditionally would let a
    command meant for a DIFFERENT bot -- e.g. ``/yolo@OtherBot on`` -- match
    this bot's own alias set and execute here instead of being ignored. The
    mention is stripped only when it case-insensitively matches
    *bot_username*; any other mention (or no *bot_username*, e.g. before
    ``getMe`` has resolved at startup) is left attached, so the token fails
    every alias match and falls through as an ordinary, unrecognized message
    rather than being guessed at.
    """
    m = _BOT_MENTION_RE.search(cmd)
    if not m or not bot_username or m.group(1).lower() != bot_username.lower():
        return cmd
    return cmd[: m.start()]


def parse_command(text: str, bot_username: str = "") -> str | None:
    """Return the command name for *text*, or None when it is not a command.

    *bot_username* (this bot's own registered username, from ``getMe``) gates
    ``@BotUsername`` suffix stripping -- see :func:`_strip_bot_mention`.
    """
    stripped = text.strip()
    # Telegram commands always start with /
    if not stripped.startswith("/"):
        return None
    parts = stripped.split()
    cmd = _strip_bot_mention(parts[0].lower(), bot_username)
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
    # /kirocrew dashboard [<TTL>] -- requires the explicit "dashboard"
    # subcommand, so a bare "/kirocrew" (typo or menu tap) falls through as
    # ordinary chat text instead of minting a login link.
    if cmd in _DASHBOARD_ALIASES and len(parts) >= 2 and parts[1].lower() == "dashboard":
        return "dashboard"
    return None


def parse_command_argument(text: str) -> str:
    """Return the text following a command token (``""`` when there is none)."""
    parts = text.strip().split(None, 1)
    return parts[1].strip() if len(parts) == 2 else ""


def parse_dashboard_ttl(text: str) -> int:
    """Parse the optional TTL from a ``/kirocrew dashboard [<N>h|<N>m]`` command.

    Returns the session TTL in seconds. Defaults to 3600 (1 hour) when no
    duration is given or the duration is unparseable.
    """
    from kiro_crew.dashboard.token_auth import parse_duration

    parts = text.strip().split()
    # Expected: ["/kirocrew", "dashboard", "<ttl>"]
    if len(parts) >= 3:
        parsed = parse_duration(parts[2].lower())
        if parsed is not None:
            return parsed
    return 3600


def format_ttl(ttl_secs: int) -> str:
    """Render a TTL in seconds as a human duration ("2h", "90m" -> "1h 30m").

    Never truncates: a non-hour-multiple >= 1h renders both components so the
    reply reports exactly how long the login link stays live.
    """
    hours, rem = divmod(ttl_secs, 3600)
    mins = rem // 60
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


_QUEUE_ALIASES = frozenset(("/queue",))
_STEER_ALIASES = frozenset(("/steer",))


def parse_mid_turn_override(text: str, bot_username: str = "") -> tuple[str | None, str]:
    """Detect a per-message mid-turn override.

    ``/queue <msg>`` forces the message to be queued (answered after the current
    turn); ``/steer <msg>`` forces it to steer the running turn. Each overrides
    the global ``messaging.queue_mode`` for THIS message only. Returns
    ``(mode, rest)`` with the directive stripped -- ``mode`` is ``"queue"`` or
    ``"steer"`` -- or ``(None, text)`` when there is no directive (or the
    directive carries no message body, e.g. a bare ``/queue``).

    *bot_username* gates ``@BotUsername`` suffix stripping -- see
    :func:`_strip_bot_mention`.
    """
    parts = text.lstrip().split(None, 1)
    if len(parts) != 2:  # needs a directive AND a message body
        return None, text
    cmd, rest = _strip_bot_mention(parts[0].lower(), bot_username), parts[1]
    if cmd in _QUEUE_ALIASES:
        return "queue", rest
    if cmd in _STEER_ALIASES:
        return "steer", rest
    return None, text


def is_bare_mid_turn_override(text: str, bot_username: str = "") -> bool:
    """True for a lone ``/queue`` / ``/steer`` carrying no message body.

    Those two are prefixes, not standalone commands, so the bare token matches
    neither :func:`parse_command` nor :func:`parse_mid_turn_override` and would
    otherwise reach the model as ordinary chat text — the user sees an answer to
    the literal string "/queue" instead of being told they left the message off.

    *bot_username* gates ``@BotUsername`` suffix stripping -- see
    :func:`_strip_bot_mention`.
    """
    parts = text.strip().split()
    return len(parts) == 1 and _strip_bot_mention(parts[0].lower(), bot_username) in (
        _QUEUE_ALIASES | _STEER_ALIASES
    )


# ── Command catalogue (help card + Bot API menu) ──

#: Ordered ``(command, description)`` rows rendered by BOTH ``/help`` and the
#: Bot API ``/`` menu. Names carry no leading slash because ``setMyCommands``
#: rejects one. ``/queue`` and ``/steer`` are deliberately absent: the Telegram
#: client SENDS a menu entry on tap, and a bare ``/queue`` has no message body
#: to act on, so listing them would put a dead entry in the menu — they stay
#: documented in the help card's footer instead. ``/kirocrew`` is absent for
#: the same reason: a menu tap sends the bare token, which is not a command
#: without its ``dashboard`` subcommand — it is documented in the footer.
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
    "/kirocrew dashboard [<N>h|<N>m] — get a dashboard login link (DM only)\n"
    "\n"
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
