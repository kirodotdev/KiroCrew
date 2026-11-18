"""Feishu command parsing.

Commands:
  /new (or /reset)    — start a fresh conversation
  /compact            — compact the context
  /agent [name]       — show or switch the agent for this conversation
  /stop (or /cancel)  — stop the reply that is running
  /help               — show the command list

The vocabulary is DATA -- :data:`COMMANDS` -- and both the matcher and the
``/help`` card read it, through the shared ``messaging.command_table`` shape
Weixin uses, so a command cannot be added without appearing in help.

Only slash spellings are accepted. A bare word such as ``停止`` or ``帮助`` has
always reached the agent as an ordinary message on Feishu, and still does.

Strings are Chinese, matching the rest of this channel and WeCom / Weixin.
"""

from __future__ import annotations

from kiro_crew.messaging.command_table import CommandSpec, build_help_text, match_command

# Named ``COMMANDS`` because the shared cancel-drift tripwire in
# ``test/test_messaging_dispatch.py`` discovers a channel's dataclass-row table
# by that export name and reads each row's ``.name`` / ``.aliases``.
COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec("new", "开始新对话", aliases=("/reset",)),
    CommandSpec("compact", "压缩上下文，腾出空间"),
    CommandSpec("agent", "查看或切换本对话的 agent（/agent <名称>）"),
    CommandSpec("stop", "停止当前回复", aliases=("/cancel",)),
    CommandSpec("help", "显示命令列表"),
)

_HELP_HEADER = "🦞 Kiro Crew — 飞书"
_HELP_FOOTER = "直接发消息即可对话。回复生成中发来的消息会并入当前回复。"


def build_help() -> str:
    """The ``/help`` card, rendered from :data:`COMMANDS`."""
    return build_help_text(_HELP_HEADER, COMMANDS, _HELP_FOOTER)


def parse_command(text: str) -> str | None:
    """Return the command name *text* invokes, else ``None``.

    ``/agent`` takes an argument, so only its bare form matches here; the
    dispatcher reads ``/agent <name>`` itself.
    """
    return match_command(text, COMMANDS)
