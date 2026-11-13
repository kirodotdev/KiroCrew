"""One malformed entry in the pending deploy store does not break the whole store.

The store is a JSON list the dashboard and the deploy tool both read and write.
A list item that is not an object, or whose ``created_at_epoch`` is not a
finite number, can never be confirmed. It is skipped, so the pending list, a new
preview, a confirm and a dismiss all keep working on the rest of the store.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kiro_crew.deploy import pending
from kiro_crew.testing.clock import ManualClock

_NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc).timestamp()

_MALFORMED = [
    "a string",
    7,
    None,
    ["a", "list"],
    {"id": "null-epoch", "created_at_epoch": None},
    {"id": "text-epoch", "created_at_epoch": "soon"},
    # Python's json reads 1e309 as inf, which never expires and cannot be sent as JSON.
    {"id": "inf-epoch", "created_at_epoch": float("inf")},
    {"id": "nan-epoch", "created_at_epoch": float("nan")},
]


@pytest.fixture(autouse=True)
def _store(tmp_path: Path, _floor_monkeypatch):
    store = tmp_path / "deploy" / "pending-deploys.json"
    _floor_monkeypatch.setattr(pending, "_store_path", lambda: store)
    ManualClock(start=_NOW).install(_floor_monkeypatch, pending)
    store.parent.mkdir(parents=True)
    good = {"id": "good", "site_id": "s", "created_at_epoch": _NOW - 60}
    store.write_text(json.dumps([*_MALFORMED, good]))
    return store


def test_list_skips_malformed_entries_and_keeps_the_good_one():
    assert [e["id"] for e in pending.list_pending()] == ["good"]


def test_a_new_preview_still_records_its_entry():
    entry = pending.add_pending({"site_id": "s2"})
    assert [e["id"] for e in pending.list_pending()] == ["good", entry["id"]]


def test_the_good_entry_can_still_be_claimed():
    claimed = pending.claim_pending("good")
    assert claimed is not None and claimed["id"] == "good"


def test_the_good_entry_can_still_be_dismissed():
    assert pending.remove_pending("good") is True
    assert pending.list_pending() == []


def test_an_infinite_epoch_is_not_listed(_store):
    good = {"id": "good", "created_at_epoch": _NOW - 60}
    _store.write_text(json.dumps([{"id": "inf", "created_at_epoch": float("inf")}, good]))
    assert [e["id"] for e in pending.list_pending()] == ["good"]


def test_an_integer_epoch_of_any_size_is_compared_not_converted(_store):
    """A huge int is a valid JSON number: it is kept or expired as before, never raised on."""
    good = {"id": "good", "created_at_epoch": _NOW - 60}
    huge_past = {"id": "huge-past", "created_at_epoch": -(10**400)}
    huge_future = {"id": "huge-future", "created_at_epoch": 10**400}
    _store.write_text(json.dumps([huge_past, huge_future, good]))
    assert [e["id"] for e in pending.list_pending()] == ["huge-future", "good"]
    entry = pending.add_pending({"site_id": "s2"})
    assert [e["id"] for e in pending.list_pending()] == ["huge-future", "good", entry["id"]]
