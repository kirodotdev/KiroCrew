"""Outbound rendering for the Weixin channel.

iLink clients render Markdown natively, so we PRESERVE Markdown (headers,
tables, fenced code) and only split when a payload exceeds the 4000-char limit,
breaking at logical block boundaries (paragraphs / blank lines / fences) and
keeping code fences intact.

Ported from Nous Research's Hermes Agent ``gateway/platforms/weixin.py``
(MIT License): ``_normalize_markdown_blocks`` + ``_split_markdown_blocks``,
plus a block-packing chunker.
"""

from __future__ import annotations

import re

WEIXIN_CHUNK_LIMIT = 4000

_FENCE_RE = re.compile(r"^```([^\n`]*)\s*$")


def normalize_markdown(content: str) -> str:
    """Collapse >1 consecutive blank line (outside code fences); trim ends."""
    lines = (content or "").splitlines()
    result: list[str] = []
    in_code = False
    blank_run = 0
    for raw in lines:
        line = raw.rstrip()
        if _FENCE_RE.match(line.strip()):
            in_code = not in_code
            result.append(line)
            blank_run = 0
            continue
        if in_code:
            result.append(line)
            continue
        if not line.strip():
            blank_run += 1
            if blank_run <= 1:
                result.append("")
            continue
        blank_run = 0
        result.append(line)
    return "\n".join(result).strip()


def split_markdown_blocks(content: str) -> list[str]:
    """Split into logical blocks (paragraphs + intact fenced code blocks)."""
    if not content:
        return []
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


def render_chunks(content: str, limit: int = WEIXIN_CHUNK_LIMIT) -> list[str]:
    """Return delivery-ready message chunks.

    Whole content stays one message when it fits. Otherwise pack Markdown
    blocks (joined by a blank line) greedily up to ``limit``; an individual
    block that itself exceeds ``limit`` is hard-split as a last resort (a fence
    larger than the limit cannot be preserved).
    """
    content = normalize_markdown(content)
    if not content:
        return []
    if len(content) <= limit:
        return [content]

    chunks: list[str] = []
    buf = ""
    for block in split_markdown_blocks(content):
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
