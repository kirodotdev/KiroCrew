"""A transcript's display floor: rows stamped at or before it stay on disk, unshown.

The floor is a row ``ts`` on the session's metadata line (:data:`DISPLAY_FLOOR_KEY`).
A cron job's ``on_tab_close="fresh"`` reopen writes it
(``cron_inject.start_fresh_cron_tab``) and the tab's history read
(``chat_api.slot_detail``) and resume page (``chat_api.resume``) honour it. The
rows themselves are untouched, so the agent's context, a cold-start replay and
the save model read them as before.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from kiro_crew.history_schema import transcript_sort_key

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog

DISPLAY_FLOOR_KEY = "display_floor"


def read_display_floor(log: "ConversationLog", key: str) -> str:
    """The floor on *key*'s metadata line, or ``""`` when it has none or is unreadable.

    A read that raises or does not answer ``(meta, readable)`` is unreadable too:
    the tab then shows every row, the same as before any floor was written.
    """
    try:
        meta, readable = log.get_metadata_status(key)
        floor = meta.get(DISPLAY_FLOOR_KEY) if readable else None
    except Exception:
        return ""
    return floor if isinstance(floor, str) else ""


def clip_to_floor(
    rows: list[dict[str, Any]], page_start: int, floor: str
) -> tuple[list[dict[str, Any]], int, bool]:
    """Drop *rows* up to the last one stamped at or before *floor*.

    Returns the kept rows, the corpus index of the first of them (*page_start*
    counts in the same corpus), and whether the page met the floor, in which
    case nothing older is served. A floor that is not a timestamp hides nothing.
    """
    floor_key = transcript_sort_key(floor)
    if floor_key[0]:
        return rows, page_start, False
    for i in range(len(rows) - 1, -1, -1):
        ts = rows[i].get("ts")
        if isinstance(ts, str) and ts and transcript_sort_key(ts) <= floor_key:
            return rows[i + 1 :], page_start + i + 1, True
    return rows, page_start, False


async def clip_page_off_loop(
    log: "ConversationLog | None", key: str, rows: list[dict[str, Any]], page_start: int
) -> tuple[list[dict[str, Any]], int, bool]:
    """:func:`clip_to_floor` for a page of *key*'s rows, with the floor read off the loop.

    Only a ``cron:`` line carries a floor, so no other key's metadata is read.
    """
    if log is None or not key.startswith("cron:"):
        return rows, page_start, False
    floor = await asyncio.to_thread(read_display_floor, log, key)
    return clip_to_floor(rows, page_start, floor) if floor else (rows, page_start, False)
