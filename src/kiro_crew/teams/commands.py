"""Microsoft Teams command parsing.

The command vocabulary lives in ONE table, :data:`COMMAND_SPEC`, which drives
both the parser and the ``/help`` card. A hand-written help string drifts from
the parser silently -- it keeps advertising a command that was renamed, or omits
one that works -- and the user has no other way to discover the vocabulary.

Per-conversation generation + awaiting-compact state lives in the shared
``messaging.conversation.ConversationState`` (re-exported here so callers can
import it from this module, mirroring the Telegram/WeCom/Webex packages).
"""

from __future__ import annotations

from kiro_crew.messaging.conversation import ConversationState  # noqa: F401

#: Ordered ``(canonical, aliases, description)`` rows. ``canonical`` is what
#: :func:`parse_command` returns; ``aliases`` are the accepted spellings
#: including the canonical one. Teams uses ``/`` (unlike Discord, whose client
#: swallows a bare ``/`` message into its own slash-command UI).
COMMAND_SPEC: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("new", ("/new", "/start"), "Start a fresh conversation"),
    ("compact", ("/compact",), "Compress the context when it gets long"),
    ("stop", ("/stop", "/cancel"), "Stop the current reply and clear the queue"),
    ("yolo", ("/yolo",), "Auto-approve tools everywhere until it expires (on / off / renew)"),
    (
        "sessions",
        ("/sessions",),
        "Continue a recent or matching dashboard session here (owner only)",
    ),
    ("link", ("/link",), "Resume mirroring dashboard replies here"),
    ("unlink", ("/unlink",), "Stop mirroring dashboard replies here"),
    ("dashboard", ("/dashboard",), "Get a dashboard login link"),
    ("help", ("/help",), "Show this command list"),
)

#: alias -> canonical name, derived so the two can never disagree.
_ALIAS_TO_CANONICAL: dict[str, str] = {
    alias: canonical for canonical, aliases, _ in COMMAND_SPEC for alias in aliases
}

#: Mid-turn routing directives. These are PREFIXES carrying a message body, not
#: standalone commands: ``/queue what time is it`` queues that text. A bare
#: directive with no body is answered with usage rather than handed to the model,
#: which would otherwise reply to the literal string ``/queue`` and be
#: indistinguishable to the user from the feature not existing.
_QUEUE_ALIASES = frozenset({"/queue"})
_STEER_ALIASES = frozenset({"/steer"})

#: Spellings that mean "stop the running turn", DERIVED from the table above so
#: the ingress path and the parser cannot disagree. The client needs this before a
#: turn exists: a ``/stop`` is exempt from the in-flight-turn ceiling, because
#: shedding the one message that frees a slot is how a saturated gateway stays
#: saturated.
STOP_ALIASES = frozenset(
    alias for canonical, aliases, _desc in COMMAND_SPEC if canonical == "stop" for alias in aliases
)

DIRECTIVE_USAGE = (
    "Add a message after the directive — `/queue <message>` answers it after the "
    "current reply, `/steer <message>` folds it into the reply in progress."
)


def parse_command(text: str) -> str | None:
    """Return the canonical command name for ``text``, or None.

    Only the FIRST whitespace-delimited token is considered, so ``/new`` and
    ``/new please`` both resolve to ``new``.
    """
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return None
    token = stripped.split()[0].lower()
    return _ALIAS_TO_CANONICAL.get(token)


def command_argument(text: str) -> str:
    """The remainder after the command token (``/yolo 30m`` -> ``30m``)."""
    parts = (text or "").strip().split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def parse_directive(text: str) -> tuple[str | None, str]:
    """Split a leading ``/queue``|``/steer`` directive off ``text``.

    Returns ``(mode, payload)`` where ``mode`` is ``"queue"``, ``"steer"`` or
    None. The payload is turn content, never a command: ``/queue /new`` queues
    the literal text ``/new`` rather than executing it. A directive whose payload
    is empty returns its mode with an empty payload so the caller can answer with
    :data:`DIRECTIVE_USAGE`.
    """
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return None, stripped
    parts = stripped.split(maxsplit=1)
    token = parts[0].lower()
    payload = parts[1].strip() if len(parts) > 1 else ""
    if token in _QUEUE_ALIASES:
        return "queue", payload
    if token in _STEER_ALIASES:
        return "steer", payload
    return None, stripped


_HELP_HEADER = "**Kiro Crew — Microsoft Teams**"
_HELP_FOOTER = (
    "While a reply is running, prefix a message to control it:\n"
    "- `/queue <message>` — answer it after the current reply\n"
    "- `/steer <message>` — fold it into the reply in progress\n"
    "\n"
    "Anything else is sent to the agent."
)


def build_help_text() -> str:
    """Render the ``/help`` card from :data:`COMMAND_SPEC`."""
    lines = [_HELP_HEADER, "", "Commands:"]
    lines += [f"- `/{canonical}` — {desc}" for canonical, _, desc in COMMAND_SPEC]
    lines += ["", _HELP_FOOTER]
    return "\n".join(lines)


HELP_TEXT = build_help_text()
