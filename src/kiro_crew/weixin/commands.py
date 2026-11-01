"""Weixin command parsing.

The vocabulary is DATA — :data:`COMMANDS` — and both the matcher and the
`/help` card read it, so a command cannot be added without appearing in help.

Per-conversation generation + awaiting-compact state lives in the shared
``messaging.conversation.ConversationState`` (re-exported so callers can import
it from this module, mirroring ``wecom.commands``).
"""

from __future__ import annotations

from kiro_crew.messaging.command_table import (
    CommandSpec,
    build_help_text,
    match_command,
)
from kiro_crew.messaging.conversation import ConversationState  # noqa: F401

#: Weixin's command vocabulary. No `/link` row: iLink is DM-only and the channel
#: has no dashboard-mirror command, so listing one would advertise a capability
#: that does not exist here. The table shape is shared with Feishu through
#: ``messaging.command_table``; the rows and the help wording are Weixin's own.
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
