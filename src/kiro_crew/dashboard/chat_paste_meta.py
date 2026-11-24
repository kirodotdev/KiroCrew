"""The collapsed-paste blocks a send carries, kept for a queued send's row.

The composer folds a long paste into a ``[ Paste #N ]`` chip and sends the
blocks behind each chip as ``meta.pastes``. A dispatched send persists its
``meta`` on the user row, so the row draws the chip again on any device and after
a reload. A queued send reaches its row through the drain, which writes only the
keys the queue entry carries. Without the blocks there, the row came back as the
whole pasted text, and a bot's chat queues every send.
"""

from __future__ import annotations

from typing import Any

#: The ``meta`` key the composer sends the blocks under and the row keeps them in.
PASTES_META_KEY = "pastes"
#: More blocks than one message's composer makes; a longer list is refused whole.
PASTE_BLOCKS_MAX = 100
PASTE_ID_MAX_LEN = 64


def _block(raw: object, message: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    block_id, seq, lines, content = (raw.get(k) for k in ("id", "seq", "lines", "content"))
    if not isinstance(block_id, str) or not 0 < len(block_id) <= PASTE_ID_MAX_LEN:
        return None
    if type(seq) is not int or type(lines) is not int or seq < 0 or lines < 0:
        return None
    # Each block must be a span of the text the turn receives. If the message
    # lost whitespace at either end, keep only the block text that is still present.
    if not isinstance(content, str):
        return None
    if content not in message:
        content = content.strip()
    if not content or content not in message:
        return None
    return {"id": block_id, "seq": seq, "lines": lines, "content": content}


def paste_meta(user_meta: object, message: str) -> list[dict[str, Any]]:
    """The send's paste blocks, checked, or ``[]`` to carry nothing.

    A malformed list is refused whole, as ``attachment_meta`` refuses a list:
    the row then shows the full text, which is what it showed before.
    """
    if not isinstance(user_meta, dict):
        return []
    raw = user_meta.get(PASTES_META_KEY)
    if not isinstance(raw, list) or not raw or len(raw) > PASTE_BLOCKS_MAX:
        return []
    blocks = [_block(item, message) for item in raw]
    if any(b is None for b in blocks):
        return []
    return [b for b in blocks if b is not None]


def merged_pastes(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every block the queue entries carry, in entry order.

    A merged row holds each send's text, so repeated blocks keep a chip for
    each occurrence of the same paste.
    """
    out: list[dict[str, Any]] = []
    for entry in entries:
        meta = entry.get("meta")
        blocks = meta.get(PASTES_META_KEY) if isinstance(meta, dict) else None
        for block in blocks if isinstance(blocks, list) else ():
            block_id = block.get("id") if isinstance(block, dict) else None
            if isinstance(block_id, str):
                out.append(block)
    return out
