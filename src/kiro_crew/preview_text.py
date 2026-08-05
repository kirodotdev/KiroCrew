"""Markdown → plain-text stripping for one-line UI previews.

The sidebar/session-list preview (`last_message` in slot ``to_dict()`` and
``HistoryLog.last_message_preview``) is a single truncated line rendered as
plain text — raw markdown markers (``**bold**``, ``` ```diff `` fences, link
syntax) read as noise there. This helper strips markdown down to readable
plain text, Slack/Telegram-preview style, WITHOUT rendering it.

Deliberately gentler than ``voice_reply.strip_markdown`` (which optimizes for
speech): emoji are kept, inline code keeps its literal text, and single
underscores are left alone so ``snake_case`` identifiers survive intact.
"""

from __future__ import annotations

import re

from kiro_crew.constants import OPTIONS_RE_LINE

# Fenced code blocks — ```lang ... ``` (or unterminated, running to the end
# of the message). Replaced with a short placeholder; the code body would
# dominate a 80–120 char preview otherwise.
_FENCE_RE = re.compile(r"```([^\n`]*)\n?[\s\S]*?(?:```|\Z)")
# <mcwidget> bodies render as an iframe elsewhere; raw HTML is noise here.
_MCWIDGET_RE = re.compile(r"<mcwidget\b[^>]*>[\s\S]*?(?:</mcwidget>|\Z)", re.IGNORECASE)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
# Trailing quick-reply block — rendered as buttons, not text. Reuse the canonical
# ReDoS-hardened, line-anchored parser (constants.OPTIONS_RE_LINE) so this strip
# can't drift from the dashboard/Slack copies and handles `]` inside a label.
_OPTIONS_RE = OPTIONS_RE_LINE
_HEADER_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r"^\s*>\s?", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+", re.MULTILINE)
_HR_RE = re.compile(r"^\s*([-*_])\1{2,}\s*$", re.MULTILINE)
_BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1", re.DOTALL)
_ITALIC_STAR_RE = re.compile(r"\*([^*\n]+)\*")
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)


def _fence_placeholder(m: re.Match) -> str:
    lang = (m.group(1) or "").strip().lower()
    return " (diff) " if lang == "diff" else " (code) "


def strip_markdown_preview(text: str) -> str:
    """Best-effort plain text for a one-line preview of *text*.

    Strips markdown syntax (fences → ``(code)``/``(diff)`` placeholders,
    emphasis markers, link/image syntax, headers, quote/bullet markers) and
    collapses all whitespace to single spaces. Truncation is the caller's
    job — this only cleans.
    """
    t = _FENCE_RE.sub(_fence_placeholder, text)
    t = _MCWIDGET_RE.sub(" (widget) ", t)
    t = _INLINE_CODE_RE.sub(r"\1", t)
    t = _IMAGE_RE.sub(lambda m: m.group(1) or "(image)", t)
    t = _LINK_RE.sub(r"\1", t)
    t = _OPTIONS_RE.sub("", t)
    # Line-anchored markers must go before whitespace collapse.
    t = _HR_RE.sub("", t)
    t = _HEADER_RE.sub("", t)
    t = _BLOCKQUOTE_RE.sub("", t)
    t = _BULLET_RE.sub("", t)
    t = _BOLD_RE.sub(r"\2", t)
    t = _ITALIC_STAR_RE.sub(r"\1", t)
    t = _STRIKE_RE.sub(r"\1", t)
    return " ".join(t.split())
