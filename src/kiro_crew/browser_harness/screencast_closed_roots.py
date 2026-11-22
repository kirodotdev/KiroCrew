"""Secret fields in closed shadow roots, for the live stream's recorder mask.

A page-side query cannot enter a closed shadow root, so the live stream's mask
(:meth:`kiro_crew.browser_harness.screencast.LiveStream._mask_rects`) asks the
browser's own DOM here. ``screencast`` stays the import path for every name.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from kiro_crew.browser_harness.dom_census import MalformedDocument, closed_secret_count

#: One CDP command on the stream's own session: ``(method, params) -> reply``.
Send = Callable[[str, dict[str, Any]], Awaitable[Any]]


async def closed_secrets(send: Send) -> int | None:
    """Secret-looking fields in closed shadow roots of the tab, or ``None``.

    Read from the browser's own DOM (``DOM.getDocument`` with ``pierce``),
    which reaches into closed roots no page-side query can enter. The whole
    document is read every time: two reads of a live page taken apart can
    disagree, so no cheaper count stands in for it.
    """
    try:
        reply = await send("DOM.getDocument", {"depth": -1, "pierce": True})
    except Exception:  # noqa: BLE001 - unknown: the frame is not recorded as masked
        return None
    finally:
        with contextlib.suppress(Exception):
            await send("DOM.disable", {})
    root = reply.get("root") if isinstance(reply, dict) else None
    try:
        return closed_secret_count(root)
    except MalformedDocument:
        return None
