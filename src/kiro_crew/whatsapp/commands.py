"""WhatsApp command parsing.

The shipped surface is deliberately small, because WhatsApp is a phone-first
surface with no slash-command registry, no autocomplete and no buttons
(``max_buttons=0``): every command has to be worth remembering by hand.

  /new (or /start)   : start a fresh session (advances the generation counter)
  /compact           : trigger context compaction
  /help              : list the commands
  /status            : runtime summary (is it alive, is it working)
  /stop (or /cancel) : abort the running turn and clear the queue

Each row's reason for existing is in :data:`COMMANDS`. Commands are matched
against the WHOLE message, not as a prefix: ``whatsapp/transport.py`` drops a
non-operator group message as soon as :func:`parse_command` is truthy, so a
prefix match would start swallowing ordinary group chatter that merely opens
with a slash.

Deliberately NOT ported from Slack. **Slack's command surface is not the parity
target**, and the omissions below are a design position rather than a backlog.
Slack carries its own YOLO grant, its own second trust store on top of the
session's approval policy, and its own redirect seam that mirrors dashboard
replies into the chat. Each is state that already exists somewhere else, and a
per-channel copy is a second place the same thing is written, free to drift from
the first. This channel keeps ONE of each: trust is the session's own approval
policy (``sessions.set_approval_policy``), and the dashboard owns settings.

So a command lands here only when it needs no duplicate state, and each of these
either needs a capability this channel does not have, crosses a trust boundary a
phone must not, or would grow a second settings surface:

- ``/model``: the picker is a list of the advertised models. With no buttons it
  would degrade to a numbered text list, and the digits ``1``/``2``/``3``
  already mean "answer the pending tool approval" on this channel
  (``messaging/approval.py``), so the two grammars would collide.
- ``/yolo``: a blanket auto-approve grant. The per-tool prompt is already
  answerable by typing a digit, and a grant typed on a phone widens what a
  borrowed or unlocked handset can do on the operator's machine.
- ``/dashboard`` and ``/link-to-dashboard``: both mint a presigned login link.
  A link pasted into a WhatsApp chat lands on every linked device and in the
  account's own message history, none of which this process controls.
- ``/link`` and ``/unlink``: mirror dashboard replies into the chat, which
  needs ``supports_session_resume``; this channel derives its session key from
  the chat JID and declares it ``False``.
- ``/voice``: the CAPABILITY exists here (``files_outbound=True``, and
  ``client.send_voice_bytes`` sends a real push-to-talk note), so the reason is
  not a missing primitive. Slack's ``/voice`` is a settings SURFACE: a modal plus
  per-session bang controls for voice, engine, rate and pitch. Per-channel copies
  of a settings surface are what this channel deliberately does not grow, because
  each one becomes a second place the same preference is written and drifts from
  the dashboard's. Voice configuration belongs in Settings, once.
- ``/sessions``: a list of recent session titles is a disclosure surface, and
  it needs the paging and rich blocks this channel has neither of.
- ``/title``: names a thread; ``threads=False`` here.
- ``/agent``, ``/ta`` and ``/project``: they repoint the agent and its
  discovery root. That is setup, it belongs to the dashboard, and there is no
  name list on this surface to pick from.
- ``/users``, ``/channels``, ``/config`` and ``/allowlist``: they edit the very
  access control that admits the sender. ``is_operator`` is deliberately
  narrower than ``dm_policy`` here, so the chat must not be able to widen it.
- ``/restart``: takes the gateway down. There is no confirmation affordance on
  this surface, so a mistap costs the operator their agent.
- ``/queue <msg>`` and ``/steer <msg>``: prefix directives by construction,
  which is exactly the shape this channel refuses. The busy path already
  steers when the provider supports it.

Per-conversation generation + awaiting-compact state lives in the shared
``messaging.conversation.ConversationState`` (re-exported so callers can
import it from this module, mirroring ``weixin.commands``).
"""

from __future__ import annotations

from dataclasses import dataclass

from kiro_crew.messaging.conversation import ConversationState  # noqa: F401


@dataclass(frozen=True)
class WhatsAppCommand:
    """One row of the command table.

    *aliases* is ordered and its FIRST entry is canonical: that is the spelling
    :func:`help_text` shows, so the rendered card is stable rather than
    dependent on set iteration order.
    """

    name: str
    aliases: tuple[str, ...]
    summary: str
    operator_only: bool


#: The whole command surface. :func:`parse_command`, :func:`help_text` and
#: :func:`is_operator_only` all read this table, so a command cannot ship
#: unroutable, undocumented, or unclassified.
COMMANDS: tuple[WhatsAppCommand, ...] = (
    # Dropping a stale context is the one thing a phone user cannot otherwise
    # do: the idle/daily rotation is invisible and arrives on its own schedule.
    # ``/start`` is muscle memory carried over from Telegram and Discord.
    WhatsAppCommand(
        name="new",
        aliases=("/new", "/start"),
        summary="Start a fresh conversation",
        operator_only=True,
    ),
    # A phone conversation runs for days, so this is the channel where the
    # context fills up first. It compacts in place, on the spot: the automatic
    # backstops (the hard threshold in the dispatcher's post-turn notice and the
    # backend autocompactor) fire on their own schedule, and this is the only
    # entry point the operator can reach on demand.
    WhatsAppCommand(
        name="compact",
        aliases=("/compact",),
        summary="Compress the context now",
        operator_only=True,
    ),
    # The only discovery path on this surface. WhatsApp shows no command menu
    # and completes nothing, so a command absent from this card is a command
    # nobody finds. Not operator-only: it discloses the command list and
    # nothing about the operator or their machine.
    WhatsAppCommand(
        name="help",
        aliases=("/help", "/commands"),
        summary="Show this command list",
        operator_only=False,
    ),
    # Answers "is it alive, is it stuck" when a reply is slow and the operator
    # is away from the dashboard. Operator-only because the summary describes
    # the host process.
    WhatsAppCommand(
        name="status",
        aliases=("/status",),
        summary="Show a runtime summary",
        operator_only=True,
    ),
    # With no buttons there is no cancel affordance at all, so a runaway turn
    # would keep spending tokens until it finished on its own.
    WhatsAppCommand(
        name="stop",
        aliases=("/stop", "/cancel"),
        summary="Stop the current reply and clear the queue",
        operator_only=True,
    ),
)

# ── User-facing strings (backend chat text has no i18n catalog) ──

NEW_SESSION_TEXT = "Started a fresh session."
#: Receipts for ``/compact``. Every one of them describes something that has
#: already happened by the time it is sent, because the operator's next decision
#: depends on it: after a "compacted" they can keep going, after a "busy" they
#: have to ask again, and after a "nothing to compact" there is nothing to wait
#: for. A receipt promising a compaction for later would read as all three.
COMPACTED_TEXT = "Context compacted."
COMPACT_BUSY_TEXT = "Still working on the last message; try /compact again shortly."
COMPACT_NOTHING_TEXT = "There's no conversation to compact yet."
COMPACT_FAILED_TEXT = "Couldn't compact the context; please try again."
#: The capability refusal (#8156): informational, never an error. This surface
#: keeps its plain-text voice; the wording tracks
#: ``messaging.commands.compact_unsupported_reply``.
COMPACT_AUTO_MANAGED_TEXT = (
    "This backend manages compaction automatically; it summarizes the "
    "conversation on its own as context fills, so manual /compact isn't "
    "needed (and isn't supported) here."
)
#: The hard-threshold notice, sent AFTER the automatic compaction it reports.
COMPACT_AUTO_TEXT = "Context was near its limit, so it was compacted automatically."
#: The soft-threshold nudge, sent once per conversation until a compaction or a
#: fresh generation clears the flag.
CONTEXT_LONG_TEXT = (
    "This conversation's context is getting long. Reply /compact to compress it, "
    "or /new to start fresh."
)
STOPPED_TEXT = "Stopped."
STOP_NOTHING_RUNNING_TEXT = "Nothing was running; queue cleared."
STATUS_UNAVAILABLE_TEXT = "Couldn't read the runtime status."

_HELP_HEADER = "Kiro Crew on WhatsApp"
_HELP_COMMANDS_LABEL = "Commands:"
# States the whole-message rule because it is not guessable: a message that
# merely starts with a command reaches the model instead, which otherwise reads
# as the command having been ignored. Phrased so it holds however the caller
# gates the public rows, since only the session-acting ones are restricted.
_HELP_FOOTER = (
    "Send any other message to chat. A command has to be the whole message, "
    "and a command that acts on the session runs only for the linked account."
)
_HELP_ALIAS_TEMPLATE = "{primary} (or {alternates})"
_HELP_ROW_TEMPLATE = "{spelling} - {summary}"
_HELP_ALIAS_SEPARATOR = ", "


def parse_command(text: str) -> str | None:
    """Return the command name for *text*, or ``None`` when it is not one.

    The comparison is against the whole trimmed, lower-cased message. Anything
    with an argument, a trailing word, or an unknown slash token falls through
    as ordinary chat text so the model, not this table, answers it.
    """
    candidate = (text or "").strip().lower()
    if not candidate:
        return None
    for command in COMMANDS:
        if candidate in command.aliases:
            return command.name
    return None


def command_argument(text: str) -> str:
    """Return the text following the leading command token (``""`` if none).

    Mirrors the sibling channels' ``parse_command_argument``. Every row in
    :data:`COMMANDS` is argument-free, so :func:`parse_command` rejects a
    message carrying one; this is the single place a row that grows an argument
    reads it, rather than each call site re-splitting the raw message.
    """
    parts = (text or "").strip().split(None, 1)
    return parts[1].strip() if len(parts) == 2 else ""


def help_text() -> str:
    """Render the ``/help`` card from :data:`COMMANDS`."""
    lines = [_HELP_HEADER, "", _HELP_COMMANDS_LABEL]
    for command in COMMANDS:
        spelling = command.aliases[0]
        if len(command.aliases) > 1:
            spelling = _HELP_ALIAS_TEMPLATE.format(
                primary=spelling,
                alternates=_HELP_ALIAS_SEPARATOR.join(command.aliases[1:]),
            )
        lines.append(_HELP_ROW_TEMPLATE.format(spelling=spelling, summary=command.summary))
    lines += ["", _HELP_FOOTER]
    return "\n".join(lines)


def is_operator_only(command: str) -> bool:
    """Whether *command* may run only for the linked account.

    An unknown name answers ``True``: a caller reaching this with a name that
    is not in the table is confused about what it is holding, and the safe
    reading of that is "restricted".
    """
    for row in COMMANDS:
        if row.name == command:
            return row.operator_only
    return True
