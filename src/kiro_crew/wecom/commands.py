"""WeCom command parsing.

Commands:
  /new (or 新对话 / 清空)  — start a fresh session (advances the generation counter)
  /compact               — trigger context compaction
  /stop (or /cancel)     — stop the reply that is running
  /help (or 帮助)         — show the command list
  /link / /unlink        — mirror dashboard replies into this chat

Mid-turn overrides (prefix a message sent WHILE a reply is running; each
overrides the global ``messaging.queue_mode`` for that one message):
  /steer <msg>  — fold this message into the running turn now
  /queue <msg>  — NOT supported here; answered with a resend prompt (a reply is
                  addressed by the inbound req_id, so a held message has no
                  request left to answer). The prefix is still parsed, so the
                  directive is stripped instead of reaching the model as text.

``COMMAND_SPEC`` is the single source of truth behind the ``/help`` card, so the
card cannot drift from what the dispatcher actually intercepts —
``test_wecom_commands.py`` pins that both ways.

Strings here are Chinese, matching the rest of this module and the Weixin
channel: WeCom is a Chinese-locale platform and its users are the people reading
these acks. Every other channel is English for the same reason.

Per-conversation generation + awaiting-compact state lives in the shared
``messaging.conversation.ConversationState`` (re-exported here so existing
callers importing it from this module keep working).
"""

from __future__ import annotations

from kiro_crew.messaging.conversation import ConversationState  # noqa: F401

# ── Command constants ──

_NEW_ALIASES = frozenset(("/new", "新对话", "清空"))
_COMPACT_ALIASES = frozenset(("/compact", "压缩"))
_HELP_ALIASES = frozenset(("/help", "帮助", "/?"))
_STOP_ALIASES = frozenset(("/stop", "/cancel", "停止"))
_LINK_ALIASES = frozenset(("/link",))
_UNLINK_ALIASES = frozenset(("/unlink",))

_QUEUE_ALIASES = frozenset(("/queue",))
_STEER_ALIASES = frozenset(("/steer",))

#: Ordered ``(command, description)`` rows rendered by ``/help``. ``/queue`` and
#: ``/steer`` are absent because they are PREFIXES, not standalone commands: a
#: bare one carries no message to act on, so listing them as commands would
#: invite a dead input. They are documented in the footer instead.
COMMAND_SPEC: tuple[tuple[str, str], ...] = (
    ("new", "开始新对话"),
    ("compact", "压缩上下文，腾出空间"),
    ("stop", "停止正在生成的回复"),
    ("link", "把 dashboard 的回复同步到这里"),
    ("unlink", "停止同步 dashboard 的回复"),
    ("help", "显示命令列表"),
)

_HELP_HEADER = "Kiro Crew — 企业微信"
_HELP_FOOTER = (
    "回复生成中时，可以给消息加前缀来控制它：\n"
    "/steer <消息> — 立即并入正在进行的回复\n"
    "/queue <消息> — 本渠道暂不支持排队，会提示你稍后重发\n"
    "\n"
    "直接发消息即可对话，回复会实时流式返回。"
)


def build_help_text() -> str:
    """Render the ``/help`` card from :data:`COMMAND_SPEC`."""
    lines = [_HELP_HEADER, "", "命令："]
    lines += [f"/{name} — {desc}" for name, desc in COMMAND_SPEC]
    lines += ["", _HELP_FOOTER]
    return "\n".join(lines)


def _match_alias(text: str) -> str | None:
    """Exact-match one command alias."""
    lower = text.lower()
    if lower in _NEW_ALIASES or text in _NEW_ALIASES:
        return "new"
    if lower in _COMPACT_ALIASES or text in _COMPACT_ALIASES:
        return "compact"
    if lower in _STOP_ALIASES or text in _STOP_ALIASES:
        return "stop"
    if lower in _HELP_ALIASES or text in _HELP_ALIASES:
        return "help"
    if lower in _LINK_ALIASES:
        return "link"
    if lower in _UNLINK_ALIASES:
        return "unlink"
    return None


def _after_leading_mention(text: str) -> str | None:
    """Return the text following ONE leading ``@name`` token, else ``None``.

    Addressing the bot is mandatory in a WeCom group, so the platform delivers
    the command as ``@Kiro /new``. Unlike Slack's ``<@BOTID>``, the mention
    arrives as plain text with no delimiter and no ``is_mention`` flag, and the
    bot's display name never reaches this module — so it is recognized purely
    structurally: a leading ``@`` run of non-whitespace, then whitespace, then
    the remainder. Exactly one token is consumed and the remainder is not
    otherwise touched, which keeps ``@a @b /new`` and ``@Kiro please /new`` out.
    """
    if not text.startswith("@"):
        return None
    parts = text.split(None, 1)
    if len(parts) != 2:
        return None
    return parts[1].strip()


def parse_command(text: str) -> str | None:
    """Return the command name for *text*, or ``None`` when it is not a command."""
    stripped = text.strip()
    cmd = _match_alias(stripped)
    if cmd is not None:
        return cmd
    # Retry once past a group mention. Only the command CANDIDATE is normalized:
    # the message itself is never rewritten, so mentioned prose still reaches the
    # model verbatim, and the alias match stays exact so only a bare command
    # behind the mention is intercepted.
    candidate = _after_leading_mention(stripped)
    if candidate is None:
        return None
    return _match_alias(candidate)


def parse_mid_turn_override(text: str) -> tuple[str | None, str]:
    """Detect a per-message mid-turn override.

    Returns ``(mode, rest)`` with the directive stripped — ``mode`` is
    ``"queue"`` or ``"steer"`` — or ``(None, text)`` when there is no directive
    (including a bare ``/queue`` with no message body).

    The payload after the directive is turn CONTENT, never a command: ``/queue
    /new`` queues the literal text ``/new`` rather than scheduling a reset.
    """
    stripped = text.lstrip()
    candidate = _after_leading_mention(stripped.strip())
    if candidate is not None:
        stripped = candidate
    parts = stripped.split(None, 1)
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

    Those two are prefixes, not standalone commands, so a bare token matches
    neither :func:`parse_command` nor :func:`parse_mid_turn_override` and would
    otherwise reach the model as ordinary chat text — the user would get an answer
    ABOUT the string "/queue", indistinguishable from the feature not existing.
    """
    stripped = text.strip()
    candidate = _after_leading_mention(stripped)
    if candidate is not None:
        stripped = candidate
    parts = stripped.split()
    return len(parts) == 1 and parts[0].lower() in (_QUEUE_ALIASES | _STEER_ALIASES)


def build_override_usage() -> str:
    """Usage shown for a bare ``/steer`` or ``/queue``."""
    return "ℹ️ /steer 和 /queue 需要跟上要发送的内容，例如：/steer 换成中文回答"
