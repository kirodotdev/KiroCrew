"""Outbound rendering for the Weixin channel.

iLink clients render Markdown natively, so we PRESERVE Markdown (headers,
tables, fenced code) and only split when a payload exceeds the 4000-char limit,
through the shared fence-aware splitter in ``messaging/split.py``.

Ported from Nous Research's Hermes Agent ``gateway/platforms/weixin.py``
(MIT License): ``_normalize_markdown_blocks`` + ``_split_markdown_blocks``.
"""

from __future__ import annotations

import re

from kiro_crew.messaging.renderer import chunk_for_transport
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

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


def render_chunks(content: str, limit: int = WEIXIN_CHUNK_LIMIT) -> list[str]:
    """Return delivery-ready message chunks.

    Whole content stays one message when it fits. Otherwise the character path
    of ``chunk_for_transport`` cuts it with the shared fence-aware splitter: a
    fence spanning a cut is sealed in one chunk and reopened, info string
    included, in the next, a line is cut only when it cannot fit a chunk at all,
    and each chunk is bounded to ``limit`` before it ships.
    """
    content = normalize_markdown(content)
    if not content:
        return []
    if len(content) <= limit:
        return [content]
    caps = TransportCapabilities(max_message_chars=limit)
    return chunk_for_transport(content, caps, redactor=_redact_all)


def _redact_all(text: str) -> str:
    """Both outbound redactors as one callable, for the splitter's seam grade."""
    out, _ = redact_exfiltration_urls(text)
    out, _ = redact_credentials(out)
    return out
