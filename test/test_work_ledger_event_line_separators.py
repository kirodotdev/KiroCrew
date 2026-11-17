"""An event whose text holds U+2028, U+2029 or U+0085 survives the next append.

Events are one JSON line each, written with ``ensure_ascii=False``, so those
characters sit raw inside the line. A reader that split with ``str.splitlines``
cut the line there, dropped both halves as unparseable, and the next append
rewrote the log from what it had read -- the event was gone for good. A worker's
``summary`` is free text and the report route keeps U+2028 and U+2029, so a real
report can carry one.
"""

from __future__ import annotations

import pytest
import work_ledger_helpers
from work_ledger_helpers import CONDUCTOR, _new_item

from kiro_crew import work_ledger as wl

_isolated_home = work_ledger_helpers._isolated_home


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085"])
def test_a_report_with_a_unicode_line_separator_survives_the_next_write(separator):
    item_id = _new_item()
    summary = f"first half{separator}second half"
    wl.apply_worker_report(CONDUCTOR, item_id, status="progress", summary=summary)
    # A later append reads the whole log and rewrites it from what it parsed.
    wl.apply_worker_report(CONDUCTOR, item_id, status="blocked", summary="next")

    texts = [event.text for event in wl.read_events(CONDUCTOR, item_id)]
    assert summary in texts
    assert "next" in texts
