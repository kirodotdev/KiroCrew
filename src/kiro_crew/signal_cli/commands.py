"""Signal command parsing.

Commands (WhatsApp's surface, the parity target for this phone-first channel):
  /new (or /start)       -- start a fresh session (advances the generation counter)
  /compact               -- trigger context compaction
  /help (or /commands)   -- show available commands
  /status                -- runtime summary (is it alive, is it working)
  /stop (or /cancel)     -- abort the running turn and clear the queue

Every row is operator-only except ``/help``, through :func:`is_operator_only`.
On Signal the operator is narrower than the allow-list
(``SignalTransport.is_operator``): the account itself when it is listed, else
the list's only entry, else nobody. Under a unified ``dm_scope`` every allowed
contact shares the operator's session, so another contact's ``/new`` or
``/stop`` would act on the operator's conversation.

Per-conversation generation + awaiting-compact state lives in the shared
``messaging.conversation.ConversationState`` (re-exported here so callers can
import it from this module, mirroring the other channel packages).

``HELP_TEXT`` is plain text: this channel sends no markup, so a bulleted list
written in asterisks would arrive as literal asterisks.
"""

from __future__ import annotations

from kiro_crew.messaging.conversation import ConversationState  # noqa: F401

#: The cancel spellings, named as the other channels name theirs so the shared
#: cancel gate's drift test (``test_messaging_dispatch``) reads them.
_STOP_ALIASES = frozenset(("/stop", "/cancel"))

#: ``(name, aliases, operator_only)``. :func:`parse_command` and
#: :func:`is_operator_only` both read this table, so a command cannot ship
#: unroutable or unclassified.
_COMMANDS: tuple[tuple[str, frozenset[str], bool], ...] = (
    ("new", frozenset(("/new", "/start")), True),
    ("compact", frozenset(("/compact",)), True),
    # Discloses the command list and nothing about the operator or the host.
    ("help", frozenset(("/help", "/commands")), False),
    # Describes the host process.
    ("status", frozenset(("/status",)), True),
    # With no buttons this is the channel's only cancel affordance.
    ("stop", _STOP_ALIASES, True),
)

STOPPED_TEXT = "⏹️ Stopped."
STOP_NOTHING_RUNNING_TEXT = "ℹ️ Nothing was running; queue cleared."
STATUS_UNAVAILABLE_TEXT = "⚠️ Couldn't read the runtime status."


def parse_command(text: str) -> str | None:
    """Return the command name ('new', 'compact', 'help', 'status', 'stop') or None."""
    stripped = text.strip()
    cmd = stripped.split()[0].lower() if stripped.startswith("/") else ""
    for name, aliases, _operator_only in _COMMANDS:
        if cmd in aliases:
            return name
    return None


def is_operator_only(command: str) -> bool:
    """Whether *command* may run only for the operator.

    An unknown name answers ``True``: a caller holding a name that is not in
    the table is confused about it, and the safe reading is "restricted".
    """
    for name, _aliases, operator_only in _COMMANDS:
        if name == command:
            return operator_only
    return True


HELP_TEXT = (
    "Kiro Crew commands\n"
    "/new (or /start) — start a fresh conversation\n"
    "/compact — compress the conversation context\n"
    "/status — show a runtime summary\n"
    "/stop (or /cancel) — stop the current reply and clear the queue\n"
    "/help (or /commands) — show this help\n\n"
    "When the agent asks to approve a tool, reply 1, 2 or 3.\n"
    "Anything else is sent to the agent."
)
