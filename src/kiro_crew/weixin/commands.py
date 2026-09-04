"""Weixin command parsing.

The vocabulary is DATA — :data:`COMMANDS` — and both the matcher and the
`/help` card read it, so a command cannot be added without appearing in help.

Per-conversation generation + awaiting-compact state lives in the shared
``messaging.conversation.ConversationState`` (re-exported so callers can import
it from this module, mirroring ``wecom.commands``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kiro_crew.messaging.conversation import ConversationState  # noqa: F401

#: Weixin's command vocabulary. No `/link` row: iLink is DM-only and the channel
#: has no dashboard-mirror command, so listing one would advertise a capability
#: that does not exist here.
# ── Command grammar ───────────────────────────────────────────────────────────
# Kept LOCAL rather than shared. WeCom grew its own ``build_help_text`` in #5105,
# so Weixin is the only caller these three would have, and a shared module with one
# consumer is a guess about the second. The day a second channel wants this table
# shape, this is the block to move -- with two callers to shape it, instead of one.


@dataclass(frozen=True)
class CommandSpec:
    """One command: its canonical name, what it does, and how it is spelled.

    ``name`` is the value a dispatcher switches on; the displayed form adds the
    ``/``. ``aliases`` are EXTRA spellings beyond ``f"/{name}"``, which is always
    accepted: native-language words (`新对话`), and short forms (`cancel` for
    `stop`).
    """

    name: str
    help_text: str
    aliases: tuple[str, ...] = field(default=())


def match_command(text: str, specs: tuple[CommandSpec, ...]) -> str | None:
    """Return the ``name`` of the command *text* invokes, else ``None``.

    Matching is EXACT against the whole stripped message, so a message that
    merely begins with a command word stays ordinary prose the model answers.
    ``f"/{name}"`` is matched case-insensitively (a phone keyboard capitalises),
    while a non-ASCII alias is compared as written because casing is meaningless
    for it and ``str.lower()`` on some scripts is not a no-op.

    The ``/`` is hardcoded because both adopters use it. Discord prefixes with
    ``!`` (its client swallows a bare ``/``) but does not consume this module; the
    day it migrates is the day a prefix parameter earns its place.
    """
    stripped = (text or "").strip()
    if not stripped:
        return None
    lowered = stripped.lower()
    for spec in specs:
        if lowered == f"/{spec.name}":
            return spec.name
        for alias in spec.aliases:
            if stripped == alias or lowered == alias.lower():
                return spec.name
    return None


def build_help_text(header: str, specs: tuple[CommandSpec, ...], footer: str = "") -> str:
    """Render a help card from *specs*.

    Aliases are shown beside their command, because a user who was taught `新对话`
    needs to see it is the same thing as `/new` rather than discovering two
    commands. The caller puts anything else it needs to explain into *footer*.
    """
    lines = [header, ""]
    for spec in specs:
        spelled = f"/{spec.name}"
        if spec.aliases:
            spelled += " (" + " / ".join(spec.aliases) + ")"
        lines.append(f"{spelled} — {spec.help_text}")
    if footer:
        lines += ["", footer]
    return "\n".join(lines)


# Named ``COMMANDS`` (not ``COMMAND_SPEC``) because the shared cancel-drift
# tripwire in ``test/test_messaging_dispatch.py`` discovers a channel's table by
# EXPORT NAME: ``COMMAND_SPEC`` is its ``(canonical, aliases, description)``
# 3-tuple shape (teams), while ``COMMANDS`` is its dataclass-row shape carrying
# ``.name``/``.aliases`` (whatsapp), which is what these rows are. Exporting
# dataclass rows as ``COMMAND_SPEC`` makes that tripwire call ``len()`` on one.
COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec("new", "开始新对话", aliases=("新对话", "清空")),
    CommandSpec("compact", "压缩上下文，腾出空间"),
    CommandSpec("stop", "停止当前回复", aliases=("/cancel", "停止")),
    CommandSpec("help", "显示命令列表", aliases=("帮助",)),
)

_HELP_HEADER = "🦞 Kiro Crew — 微信"
_HELP_FOOTER = "直接发消息即可对话。较长的回复会分成多条消息。"


def build_help() -> str:
    """The `/help` card, rendered from :data:`COMMANDS`."""
    return build_help_text(_HELP_HEADER, COMMANDS, _HELP_FOOTER)


def parse_command(text: str) -> str | None:
    """Return the command name *text* invokes, else ``None``."""
    return match_command(text, COMMANDS)
