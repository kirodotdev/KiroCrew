"""The durable queue copy leaves out a queued send's paste blocks.

Each block in ``meta.pastes`` repeats a span of the entry's ``content``, so
carrying them would store the paste twice and charge it twice against the
durable byte budget.
"""

from __future__ import annotations

import copy
import json

from kiro_crew.dashboard.slot_queue_repository import (
    MAX_DURABLE_QUEUE_BYTES,
    durable_queue_entries,
    warn_if_not_durable,
)


def _entry(content: str) -> dict:
    block = {"id": "p1", "seq": 1, "lines": 1, "content": content}
    return {
        "id": "q1",
        "content": content,
        "kind": "",
        "meta": {"sendId": "send_1", "pastes": [block]},
    }


def test_durable_copy_drops_pastes_and_keeps_the_rest_of_meta():
    item = _entry("x" * 10_000)
    before = copy.deepcopy(item)

    [entry] = durable_queue_entries([item])

    assert entry["content"] == item["content"]
    assert entry["meta"] == {"sendId": "send_1"}
    assert item == before


def test_entry_that_fits_only_without_its_pastes_is_still_written():
    # The content alone fits; content plus its paste copy would not.
    item = _entry("x" * (MAX_DURABLE_QUEUE_BYTES - 500))
    assert len(json.dumps(item)) > MAX_DURABLE_QUEUE_BYTES

    entries = durable_queue_entries([item])

    assert [e["id"] for e in entries] == ["q1"]
    assert warn_if_not_durable([item], "q1", "slot") is True
