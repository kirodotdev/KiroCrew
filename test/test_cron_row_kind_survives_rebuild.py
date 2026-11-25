"""A cron row keeps its ``injectKind`` and ``mid`` on the slot-detail rebuild.

A cron notification's row has a JSON ``cls`` holding only ``cronLabel`` and a
stored ``meta`` holding ``injectKind`` and ``mid``. The live ``chat_message``
frame merges the two. A row the slot-detail page serves from the in-memory
window (an unflushed row, or a flushed one when every disk row in the window
carries a ``mid``) goes through ``_prepare_messages`` and must send the same
keys, or the dashboard stops treating the row as the start of a turn and
folds the reply above it into that turn.
"""

from __future__ import annotations

import json

from kiro_crew.dashboard.chat_utils import _prepare_messages
from kiro_crew.dashboard.session_deliver import cron, origin_row
from kiro_crew.dashboard.state import _ChatSlot, row_mid


def test_a_rebuilt_cron_row_carries_its_kind_and_mid():
    slot = _ChatSlot("s1")
    role, cls, meta = origin_row(cron("nightly"))
    row = slot.append(role, '[Cron notification from "nightly"]\nreport\n', cls, meta=meta)
    assert json.loads(row["cls"]) == {"cronLabel": "nightly"}
    (out,) = _prepare_messages(list(slot.messages), False, live_child="")
    assert out["meta"] == {"cronLabel": "nightly", "injectKind": "cron", "mid": row_mid(row)}


def test_an_inject_row_without_stored_meta_keeps_its_cls_meta():
    row = {"role": "inject", "content": "report", "cls": json.dumps({"cronLabel": "nightly"})}
    (out,) = _prepare_messages([row], False, live_child="")
    assert out["meta"] == {"cronLabel": "nightly"}


def test_stored_meta_wins_on_a_shared_key():
    row = {
        "role": "inject",
        "content": "report",
        "cls": json.dumps({"cronLabel": "from-cls"}),
        "meta": {"injectKind": "cron", "cronLabel": "from-meta"},
    }
    (out,) = _prepare_messages([row], False, live_child="")
    assert out["meta"] == {"cronLabel": "from-meta", "injectKind": "cron"}


def test_a_queued_row_keeps_its_cls_meta_only():
    row = {
        "role": "queued",
        "content": "report",
        "cls": json.dumps({"cronLabel": "nightly", "queue_id": "q-1"}),
        "meta": {"queueId": "q-1"},
    }
    (out,) = _prepare_messages([row], False, live_child="")
    assert out["meta"] == {"cronLabel": "nightly", "queue_id": "q-1"}
