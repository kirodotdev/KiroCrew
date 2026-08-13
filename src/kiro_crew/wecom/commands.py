"""WeCom command parsing.

Commands:
  /new (or 新对话 / 清空)  — start a fresh session (advances the generation counter)
  /compact               — trigger context compaction

Per-conversation generation + awaiting-compact state lives in the shared
``messaging.conversation.ConversationState`` (re-exported here so existing
callers importing it from this module keep working).
"""

from __future__ import annotations

from kiro_crew.messaging.conversation import ConversationState  # noqa: F401

# ── Command constants ──

_NEW_ALIASES = frozenset(("/new", "新对话", "清空"))
_COMPACT_ALIASES = frozenset(("/compact",))
_LINK_ALIASES = frozenset(("/link",))
_UNLINK_ALIASES = frozenset(("/unlink",))


def _match_alias(text: str) -> str | None:
    """Exact-match one command alias."""
    lower = text.lower()
    if lower in _NEW_ALIASES or text in _NEW_ALIASES:
        return "new"
    if lower in _COMPACT_ALIASES:
        return "compact"
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
    """Return 'new', 'compact', 'link', 'unlink', or None."""
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
