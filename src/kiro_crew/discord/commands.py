"""Discord command parsing.

Commands (``!``-prefixed — Discord's client swallows bare ``/`` messages into
its slash-command UI, so text commands use ``!``; ``/``-prefixed forms are
also accepted for muscle-memory parity with Telegram, and the same names are
published as real slash commands):

  !new         — start a fresh session (advances the generation counter)
  !compact     — trigger context compaction
  !model       — pick the model from a button list
  !status      — show runtime stats
  !sessions    — continue a recent dashboard session here (owner only)
  !link        — mirror this conversation's dashboard tab back here
  !unlink      — stop mirroring
  !stop        — stop the current reply and clear the queue (alias: !cancel)
  !help        — show available commands

Mid-turn overrides (prefix a message sent WHILE a reply is running; they
override the global ``messaging.queue_mode`` for that one message):
  !queue <msg> — hold this message and answer it after the current turn
  !steer <msg> — fold this message into the running turn right now

``COMMAND_SPEC`` is the single source of truth behind both the ``!help`` card
and the slash-command menu published to Discord, so the two cannot drift apart.

Per-conversation generation + awaiting-compact state lives in the shared
``messaging.conversation.ConversationState`` (re-exported here to mirror the
Telegram module's layout).
"""

from __future__ import annotations

from typing import Any

# The wire limits belong to the module that talks to the REST API and enforces
# them on whatever a caller hands it; the catalogue below builds rows against the
# same two so a description this module truncates and one the client truncates
# cannot disagree.
from kiro_crew.discord.client import _APP_COMMAND_DESC_LIMIT, _APP_COMMAND_NAME_RE
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
# ``!models`` (plural) is a typo-safe alias for the same reason.
_MODEL_ALIASES = frozenset(("!model", "/model", "!models", "/models"))
_STATUS_ALIASES = frozenset(("!status", "/status"))

_PREFIXES = ("!", "/")


def parse_command_argument(text: str) -> str:
    """Return the optional text following a Discord command token."""
    parts = text.strip().split(None, 1)
    return parts[1].strip() if len(parts) == 2 else ""


def parse_command(text: str) -> str | None:
    """Return the command name for *text*, or None when it is not a command.

    One of 'new', 'compact', 'model', 'status', 'sessions',
    'link', 'unlink', 'stop', 'help'.
    """
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
    if cmd in _MODEL_ALIASES:
        return "model"
    if cmd in _STATUS_ALIASES:
        return "status"
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


def is_bare_mid_turn_override(text: str) -> bool:
    """True for a lone ``!queue`` / ``!steer`` carrying no message body.

    Those two are message PREFIXES, not standalone commands, so the bare token
    matches neither :func:`parse_command` nor :func:`parse_mid_turn_override` and
    would otherwise reach the model as ordinary chat text -- the user sees an
    answer to the literal string "!queue" instead of being told they left the
    message off. Mirrors the Telegram parser of the same name; Discord has no
    ``@BotUsername`` suffix to strip, so the token is matched as typed.
    """
    parts = text.strip().split()
    return len(parts) == 1 and parts[0].lower() in (_QUEUE_ALIASES | _STEER_ALIASES)


# ── Command catalogue (help card + slash-command menu) ──

#: Ordered ``(command, description)`` rows rendered by BOTH the ``!help`` card
#: and the slash-command menu published to Discord. Names carry no prefix: the
#: bulk-overwrite payload rejects one, and the card names the accepted prefixes
#: itself. Descriptions stay inside Discord's 100-character ceiling, which is
#: tighter than Telegram's 256.
#:
#: ``queue`` and ``steer`` are deliberately absent. They are message PREFIXES,
#: and a slash-menu tap sends the bare token with no message body to act on, so
#: a menu entry for either would be dead however it was worded. Both stay
#: documented in the card's footer, which is where a running turn's options are
#: useful anyway.
COMMAND_SPEC: tuple[tuple[str, str], ...] = (
    ("new", "Start a fresh conversation"),
    ("compact", "Compress the context when it gets long"),
    ("model", "Choose the model from a list"),
    ("status", "Show gateway runtime stats and the approval mode"),
    ("sessions", "Continue a recent or matching dashboard session here (owner only)"),
    ("link", "Resume mirroring dashboard replies here (on by default)"),
    ("unlink", "Stop mirroring dashboard replies here"),
    ("stop", "Stop the current reply and clear the queue"),
    ("help", "Show the command list"),
)

# Discord application-command wire constants. CHAT_INPUT is the slash-command
# type; STRING is the only option type needed because every argument here is
# free text or a fixed choice.
_APP_COMMAND_CHAT_INPUT = 1
_APP_OPTION_STRING = 3
_APP_CONTEXT_GUILD = 0
_APP_CONTEXT_BOT_DM = 1

#: Where each command is offered: an approved guild thread and the bot DM.
#: ``contexts`` supersedes the deprecated ``dm_permission``, which is therefore
#: never emitted.
_APP_COMMAND_CONTEXTS = (_APP_CONTEXT_GUILD, _APP_CONTEXT_BOT_DM)

#: Option rows for the commands that take an argument, keyed by command name;
#: a command absent here takes none. Option names obey the same
#: ``[a-z0-9_-]{1,32}`` rule as a command name and descriptions the same
#: 100-character ceiling, and a choice list may hold at most 25 entries. A
#: ``value`` is what comes back in ``DiscordInteraction.options``, so it has to
#: be a token the ``!`` text handlers already accept: the slash and text paths
#: share one parser.
_COMMAND_OPTIONS: dict[str, tuple[dict[str, Any], ...]] = {
    "sessions": (
        {
            "type": _APP_OPTION_STRING,
            "name": "query",
            "description": "Match a session by title or slot name",
            "required": False,
        },
    ),
}


def _option_payload(option: dict[str, Any]) -> dict[str, Any]:
    """Copy one option row, choices included.

    The table is module state shared by every call, so the payload hands out
    copies: a caller that adjusts a row in place would otherwise change what
    every later registration sends.
    """
    row = dict(option)
    choices = row.get("choices")
    if choices:
        row["choices"] = [dict(choice) for choice in choices]
    return row


def application_command_payload() -> list[dict[str, Any]]:
    """``COMMAND_SPEC`` shaped as Discord's bulk-overwrite command array.

    The array is the whole global command set for
    ``PUT /applications/{id}/commands``. Rows that break Discord's own
    constraints (name ``[a-z0-9_-]{1,32}``, non-empty description) are skipped
    rather than sent, because Discord rejects the ENTIRE array on one bad row:
    a single malformed entry would otherwise cost the user every slash command.
    """
    rows: list[dict[str, Any]] = []
    for name, desc in COMMAND_SPEC:
        if not _APP_COMMAND_NAME_RE.match(name) or not desc:
            continue
        row: dict[str, Any] = {
            "name": name,
            "description": desc[:_APP_COMMAND_DESC_LIMIT],
            "type": _APP_COMMAND_CHAT_INPUT,
            "contexts": list(_APP_COMMAND_CONTEXTS),
        }
        options = _COMMAND_OPTIONS.get(name)
        if options:
            row["options"] = [_option_payload(opt) for opt in options]
        rows.append(row)
    return rows


_HELP_HEADER = "🦞 **Kiro Crew — Discord**"
_HELP_FOOTER = (
    "While a reply is running, prefix a message to control it:\n"
    "`!queue <msg>` — answer it after the current turn\n"
    "`!steer <msg>` — fold it into the running turn now\n"
    "\n"
    "Just send a message to chat. Replies stream in real-time."
)


def _usage(name: str) -> str:
    """Render one command's invocation with its argument placeholders.

    Placeholders come from the same option table the slash registration uses, so
    an argument cannot appear in the menu and be missing from the card.
    """
    parts = [f"!{name}"]
    for opt in _COMMAND_OPTIONS.get(name, ()):
        arg = str(opt["name"])
        parts.append(f"<{arg}>" if opt.get("required") else f"[{arg}]")
    return " ".join(parts)


def build_help_text() -> str:
    """Render the ``!help`` card from :data:`COMMAND_SPEC`.

    The two accepted prefixes are named once in the heading instead of per row:
    every command takes either, and printing both forms on every row doubles the
    card's length for no added information.
    """
    lines = [_HELP_HEADER, "", "Commands (`!` or `/` — both prefixes work):"]
    lines += [f"`{_usage(name)}` — {desc}" for name, desc in COMMAND_SPEC]
    lines += ["", _HELP_FOOTER]
    return "\n".join(lines)


#: The shortest ``!``-token this module will read as a mistyped command. Two
#: characters and a leading LETTER, on top of Discord's own command-name grammar
#: (``_APP_COMMAND_NAME_RE``), are what keep prose out: a single character or a
#: leading digit is far more likely to be text than an attempted command.
_MIN_COMMAND_NAME_LEN = 2


def unknown_command_usage(text: str) -> str:
    """The usage reply for a command-shaped ``!token`` that names no command.

    ``""`` means "not a mistyped command", so the caller runs the message as an
    ordinary turn. Without this an unrecognized ``!sesions`` reaches the model,
    which answers the literal text and reads as "the feature does not exist"
    rather than "you typed it wrong" -- the same reasoning that makes
    ``!session`` a typo-safe alias, generalized to every misspelling.

    Two deliberate narrowings, because a false positive costs the user a whole
    message:

    * Only ``!``. A ``/`` token is Discord's own slash-command prefix, which its
      client captures into the command picker rather than sending, so a
      ``/``-leading message that does arrive is far more likely to be a path
      (``/etc/hosts is wrong``) than an attempted command.
    * A SHAPE test, never a dictionary of near-misses. The token must be a name
      Discord itself would accept, at least :data:`_MIN_COMMAND_NAME_LEN` long
      and starting with a letter, so ``!!!``, ``!?``, ``!``, ``!5`` and
      ``!(see below)`` all fall through to the model. Known commands and the
      ``!queue``/``!steer`` directives are recognized through the real parsers,
      so a command added above cannot start answering itself with this card.
    """
    stripped = text.strip()
    if not stripped.startswith("!"):
        return ""
    token = stripped.split()[0]
    name = token[1:].lower()
    if (
        not _APP_COMMAND_NAME_RE.match(name)
        or len(name) < _MIN_COMMAND_NAME_LEN
        or not name[:1].isalpha()
    ):
        return ""
    if parse_command(token) is not None or token.lower() in (_QUEUE_ALIASES | _STEER_ALIASES):
        return ""
    # The escape hatch is named first: this is the one reply a user gets for a
    # message that was never a command, and it must tell them how to resend it.
    # Echoing the token back is safe because the shape check above admits only
    # ``[a-z0-9_-]``, so it carries no backtick, mention or credential syntax.
    return (
        f"❓ `{token}` isn't a command. To chat, send the message without the "
        f"leading `!`.\n\n{build_help_text()}"
    )
