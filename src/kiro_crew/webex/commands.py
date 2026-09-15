"""Webex command parsing.

The command vocabulary is the parity bar the two richest adapted channels
(Telegram, Discord) set, minus anything Webex's platform cannot express. Webex
has no command-menu API to publish to — unlike Telegram's ``setMyCommands`` —
so :data:`COMMAND_SPEC` exists purely to keep the ``/help`` card and the parser
in lockstep. A hand-written help constant drifts from the parser silently, and
the user is the one who finds out.

``/queue`` and ``/steer`` are PREFIXES rather than standalone commands: they
carry a message body and override ``messaging.queue_mode`` for that one message.
A bare token is caught by :func:`is_bare_mid_turn_override` so it answers with
usage instead of reaching the model as the literal string "/queue".

Per-conversation generation + awaiting-compact state lives in the shared
``messaging.conversation.ConversationState`` (re-exported here so callers can
import it from this module, mirroring the Telegram/WeCom packages).
"""

from __future__ import annotations

from kiro_crew.messaging.conversation import ConversationState  # noqa: F401

_NEW_ALIASES = frozenset(("/new", "/start"))
_COMPACT_ALIASES = frozenset(("/compact",))
_HELP_ALIASES = frozenset(("/help",))
_STOP_ALIASES = frozenset(("/stop", "/cancel"))
_LINK_ALIASES = frozenset(("/link",))
_UNLINK_ALIASES = frozenset(("/unlink",))
_MODEL_ALIASES = frozenset(("/model", "/models"))
_SESSIONS_ALIASES = frozenset(("/sessions", "/session"))
_YOLO_ALIASES = frozenset(("/yolo",))
_DASHBOARD_ALIASES = frozenset(("/kirocrew",))

_QUEUE_ALIASES = frozenset(("/queue",))
_STEER_ALIASES = frozenset(("/steer",))
_MID_TURN_ALIASES = _QUEUE_ALIASES | _STEER_ALIASES

#: alias -> canonical name, and the one mapping :func:`parse_command` and
#: :data:`_ALL_COMMANDS` both read, so a command reaching one and not the other
#: cannot happen: an alias missing from the parser answers as ordinary chat, and
#: one missing from the token set is answered with "unknown command".
#:
#: The aliases stay as the named sets above rather than being spelled inline here,
#: and ``_STOP_ALIASES`` MUST keep that name: a cross-channel tripwire ``getattr``s
#: it off this module to check the shared cancel-during-a-governance-deny exemption
#: covers every spelling this channel accepts. Inlining it costs nothing visible —
#: the probe falls back to its default, and these two spellings are a subset of what
#: the other channels contribute, so the union it asserts on does not move. The
#: coverage just stops. The rest are named for symmetry; only that one has a reader
#: outside this module.
_ALIAS_TO_COMMAND: dict[str, str] = {
    alias: canonical
    for canonical, aliases in (
        ("new", _NEW_ALIASES),
        ("compact", _COMPACT_ALIASES),
        ("help", _HELP_ALIASES),
        ("stop", _STOP_ALIASES),
        ("link", _LINK_ALIASES),
        ("unlink", _UNLINK_ALIASES),
        ("model", _MODEL_ALIASES),
        ("sessions", _SESSIONS_ALIASES),
        ("yolo", _YOLO_ALIASES),
        ("dashboard", _DASHBOARD_ALIASES),
    )
    for alias in aliases
}

#: Every command token, so a caller can tell "a command the user misspelled"
#: from "ordinary chat that happens to start with a slash". The mid-turn
#: directives are added here and NOT to the mapping above, because they are
#: prefixes rather than commands (see :func:`parse_command`).
_ALL_COMMANDS = frozenset(_ALIAS_TO_COMMAND) | _MID_TURN_ALIASES


def _first_token(text: str) -> str:
    """The leading ``/command`` token, lowercased, or ``""`` if there is none.

    A command is a slash followed by letters and nothing else: ``/new``,
    ``/kirocrew``. Deliberately NOT "anything starting with a slash" — a user
    pasting ``/usr/bin/python3 -V`` or ``/home/me/foo.py: fix this`` is sending a
    message, not calling a command, and :func:`is_unknown_command` would
    otherwise answer them with the help card instead of the agent.
    """
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return ""
    token = stripped.split(maxsplit=1)[0].lower()
    return token if token[1:].isalpha() else ""


def parse_command(text: str) -> str | None:
    """Return the command name, or ``None`` when *text* is ordinary chat.

    ``/queue`` and ``/steer`` deliberately do NOT resolve here: they are mid-turn
    prefixes handled by :func:`parse_mid_turn_override`, and treating a
    ``/queue do the thing`` as a command would swallow the message body — which is
    why they are absent from :data:`_ALIAS_TO_COMMAND`.
    """
    # ``""`` is never a key, so ordinary chat falls through as None.
    return _ALIAS_TO_COMMAND.get(_first_token(text))


def parse_command_argument(text: str) -> str:
    """Everything after the leading command token, stripped.

    ``"/yolo on"`` yields ``"on"``; a bare ``"/yolo"`` yields ``""``.
    """
    parts = text.strip().split(None, 1)
    return parts[1].strip() if len(parts) == 2 else ""


def parse_mid_turn_override(text: str) -> tuple[str | None, str]:
    """Detect a per-message mid-turn override.

    ``/queue <msg>`` forces the message to be queued (answered after the current
    turn); ``/steer <msg>`` forces it to fold into the running turn. Each
    overrides the global ``messaging.queue_mode`` for THIS message only.

    Returns ``(mode, rest)`` with the directive stripped, or ``(None, text)``
    when there is no directive — including a bare ``/queue`` with no message
    body, which :func:`is_bare_mid_turn_override` answers with usage instead.
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

    Those two are prefixes, so a bare token matches neither
    :func:`parse_command` nor :func:`parse_mid_turn_override` and would
    otherwise reach the model as ordinary chat text — the user sees an answer to
    the literal string "/queue" instead of being told they left the message off.

    ``maxsplit=2`` rather than a full split: this runs on every inbound message,
    and the question is only whether there is exactly one token, so there is no
    reason to allocate one string per word of a long paste.
    """
    parts = text.strip().split(maxsplit=2)
    return len(parts) == 1 and parts[0].lower() in _MID_TURN_ALIASES


def is_unknown_command(text: str) -> bool:
    """True for a leading ``/token`` this parser does not recognise.

    Lets the dispatcher answer with the help card rather than forwarding a
    typo'd command to the model, which would spend a whole turn explaining that
    it does not know what ``/nwe`` means.
    """
    cmd = _first_token(text)
    return bool(cmd) and cmd not in _ALL_COMMANDS


def strip_bot_mention(text: str, bot_name: str) -> str:
    """Remove a leading @mention of the bot from a group-space message.

    Webex only delivers a space message to a bot that was @mentioned in it, and
    the platform does NOT strip the mention — so a bot named "Ops Bot" receives
    "Ops Bot what is failing?" and reads its own name as the first word the user
    typed.

    Only a LEADING mention is removed, and only the bot's own name: a mention
    later in the sentence is the user talking about the bot, which is content.
    Webex renders a mention as plain text in the ``text`` field (the markup is in
    ``html``), so this is a prefix strip rather than markup parsing. Note Webex
    shows only the bot's FIRST name in a mention, so the first word is matched too.
    """
    if not text or not bot_name:
        return text
    stripped = text.lstrip()
    words = bot_name.split()
    # Longest first, so a full name is consumed whole rather than having its first
    # word matched and the remainder left behind as if the user had typed it.
    candidates = [bot_name] + ([words[0]] if len(words) > 1 else [])
    for candidate in candidates:
        for form in (f"@{candidate}", candidate):
            if not stripped.lower().startswith(form.lower()):
                continue
            rest = stripped[len(form) :]
            # A word boundary is required: the first-name candidate "Ops" must not
            # match the "Ops" inside "OpsBot" and leave "Bot" reading as the
            # user's first word.
            if rest and (rest[0].isalnum() or rest[0] == "_"):
                continue
            return rest.lstrip(" :,")
    return text


# ── Command catalogue (drives the /help card) ──

#: Ordered ``(command, description)`` rows rendered by ``/help``. Webex has no
#: command-menu API, so this list has exactly one consumer —
#: :func:`build_help_text` — and exists so the card cannot drift from
#: :func:`parse_command`. ``/queue``, ``/steer`` and ``/kirocrew`` are absent
#: because each needs an argument to mean anything; they are documented in the
#: footer instead.
COMMAND_SPEC: tuple[tuple[str, str], ...] = (
    ("new", "Start a fresh conversation"),
    ("compact", "Compress the context when it gets long"),
    ("model", "List the models this account can use, and pick one"),
    ("sessions", "List this conversation's earlier sessions"),
    ("yolo", "Auto-approve every tool for a while (on / off / renew)"),
    ("link", "Resume mirroring dashboard replies here (on by default)"),
    ("unlink", "Stop mirroring dashboard replies here"),
    ("stop", "Stop the current reply and clear the queue"),
    ("help", "Show this command list"),
)

_HELP_HEADER = "**Kiro Crew — Webex**"
_HELP_FOOTER = (
    "`/kirocrew dashboard [<N>h|<N>m]` — get a dashboard login link (DM only)\n"
    "\n"
    "While a reply is running, prefix a message to control it:\n"
    "- `/queue <msg>` — answer it after the current turn\n"
    "- `/steer <msg>` — fold it into the running turn now\n"
    "\n"
    "In a group space, put my @mention before the command.\n"
    "\n"
    "Anything else is sent to the agent."
)


def build_help_text() -> str:
    """Render the ``/help`` card from :data:`COMMAND_SPEC`."""
    lines = [_HELP_HEADER, "", "**Commands**"]
    lines += [f"- `/{name}` — {desc}" for name, desc in COMMAND_SPEC]
    lines += ["", _HELP_FOOTER]
    return "\n".join(lines)
