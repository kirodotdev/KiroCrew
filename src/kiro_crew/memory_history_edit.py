"""Today's part of a V1 daily-history edit made on the dashboard's aggregate view.

The Memory tab shows a V1 store's history as one document: today's file first, then
the earlier days (``MemoryStore._read_recent_history_uncached``), joined by a blank
line. Its save writes today's file only, so writing the whole submitted document
there copies every earlier day into today, once more on each save.
"""

from __future__ import annotations


def today_part_of_aggregate_edit(submitted: str, baseline: str, today_text: str) -> str:
    """The text for today's file when *submitted* keeps the earlier days unchanged.

    *baseline* is the aggregate the editor was shown and *today_text* the part of it
    that came from today's file (normalized and stripped, as the aggregate holds it).
    When *submitted* still ends with the earlier days exactly as shown, only the text
    before them is today's. Otherwise *submitted* is returned unchanged: an edit to an
    earlier day, or a whole replacement, keeps the save it had.
    """
    rest = baseline[len(today_text) :] if today_text else baseline
    if not rest.strip() or not submitted.endswith(rest):
        return submitted
    head = submitted[: len(submitted) - len(rest)].rstrip("\n")
    return head + "\n" if head else ""
