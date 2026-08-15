"""Outbound rendering for the WhatsApp channel.

WhatsApp renders its own formatting dialect, not Markdown: ``*bold*``,
``_italic_``, ``~strikethrough~``, ``` ```monospace``` ``` and `` `inline` ``.
Headings, links and tables have no native form. :func:`to_whatsapp_text`
converts the agent's Markdown into that dialect (conservatively — anything it
cannot map degrades to plain text, never to visible ``**`` litter), and
:func:`render_chunks` splits oversized payloads at block boundaries with code
fences kept intact, mirroring the weixin chunker with WhatsApp's 4096 cap.
"""

from __future__ import annotations

import re

WHATSAPP_CHUNK_LIMIT = 4096

_FENCE_RE = re.compile(r"^```([^\n`]*)\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_BOLD_US_RE = re.compile(r"__(.+?)__")
_STRIKE_RE = re.compile(r"~~(.+?)~~")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+")


def _convert_line(line: str) -> str:
    heading = _HEADING_RE.match(line)
    if heading:
        # WhatsApp has no headings; bold the text instead.
        line = f"*{heading.group(2).strip()}*"
    line = _BULLET_RE.sub(lambda m: f"{m.group(1)}• ", line)
    line = _BOLD_RE.sub(r"*\1*", line)
    line = _BOLD_US_RE.sub(r"*\1*", line)
    line = _STRIKE_RE.sub(r"~\1~", line)
    # [label](url) → "label (url)"; bare label when the label IS the url.
    line = _LINK_RE.sub(lambda m: m.group(2) if m.group(1) == m.group(2) else f"{m.group(1)} ({m.group(2)})", line)
    return line


def to_whatsapp_text(content: str) -> str:
    """Convert agent Markdown to WhatsApp's formatting dialect."""
    lines = (content or "").splitlines()
    out: list[str] = []
    in_code = False
    for raw in lines:
        line = raw.rstrip()
        if _FENCE_RE.match(line.strip()):
            in_code = not in_code
            out.append("```")
            continue
        out.append(line if in_code else _convert_line(line))
    if in_code:
        out.append("```")  # unterminated fence would swallow the rest
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_blocks(content: str) -> list[str]:
    """Split into paragraphs + intact fenced code blocks."""
    blocks: list[str] = []
    current: list[str] = []
    in_code = False
    for raw in content.splitlines():
        line = raw.rstrip()
        if _FENCE_RE.match(line.strip()):
            if not in_code and current:
                blocks.append("\n".join(current).strip())
                current = []
            current.append(line)
            in_code = not in_code
            if not in_code:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        if in_code:
            current.append(line)
            continue
        if not line.strip():
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return [b for b in blocks if b]


def _hard_split(text: str, limit: int) -> list[str]:
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def render_chunks(content: str, limit: int = WHATSAPP_CHUNK_LIMIT) -> list[str]:
    """Delivery-ready chunks of WhatsApp-dialect text (see module docstring)."""
    text = to_whatsapp_text(content)
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf = ""
    for block in _split_blocks(text):
        if len(block) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(_hard_split(block, limit))
            continue
        candidate = f"{buf}\n\n{block}" if buf else block
        if len(candidate) <= limit:
            buf = candidate
        else:
            if buf:
                chunks.append(buf)
            buf = block
    if buf:
        chunks.append(buf)
    return chunks
