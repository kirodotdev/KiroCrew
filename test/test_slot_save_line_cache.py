"""A full rewrite rebuilds only the rows the entry cache does not hold.

When a save must rewrite the whole file (an already-written row changed), each
unchanged row's entry and line come from the entry cache, keyed by the row's raw
digest. Only edited rows and rows outside the cache's bounds are built, redacted
and serialized again, and the file is the same bytes a rebuild of every row writes.
These tests drive the real save, ``_save_slot_to_history``, against a real file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_slot_save_append_tail import _Meter, _primed

from kiro_crew.dashboard import chat_persistence
from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.slot_persistence import transcript_merge

WINDOW = 300


def _rebuilt_from_scratch(state, slot, path: Path, monkeypatch: pytest.MonkeyPatch) -> bytes:
    """The file a save writes when it builds every row again (no cache, no append)."""
    with monkeypatch.context() as mp:
        mp.setattr(chat_persistence, "_entry_cache_bounds", lambda: (0, 0))
        mp.setattr(transcript_merge, "appendable_tail", lambda *a, **kw: None)
        slot._frozen_prefix_cache = None
        slot._dirty = True
        assert _save_slot_to_history(state, slot) is True
    return path.read_bytes()


def _edit_and_save(state, slot, path: Path, monkeypatch: pytest.MonkeyPatch, index: int):
    slot.messages[index].setdefault("meta", {})["done"] = True
    slot.messages[index]["meta"]["output"] = f"result for row {index}"
    slot._dirty = True
    with monkeypatch.context() as mp:
        meter = _Meter(mp, path)
        assert _save_slot_to_history(state, slot) is True
    return meter, path.read_bytes()


def test_a_full_rewrite_after_one_edit_builds_only_the_edited_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, slot, path = _primed(tmp_path, monkeypatch, WINDOW)

    meter, written = _edit_and_save(state, slot, path, monkeypatch, -3)

    assert meter.full_writes == 1  # an already-written row changed
    assert (meter.built, meter.serialized) == (1, WINDOW)
    assert b'"result for row -3"' in written
    # A cache hit writes what building every row again writes.
    assert _rebuilt_from_scratch(state, slot, path, monkeypatch) == written


def test_an_edit_deep_in_a_row_is_caught_by_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, slot, path = _primed(tmp_path, monkeypatch, WINDOW)
    slot.messages[10]["meta"]["nested"] = {"items": [1, 2]}
    slot._dirty = True
    assert _save_slot_to_history(state, slot) is True

    slot.messages[10]["meta"]["nested"]["items"].append(3)
    slot._dirty = True
    with monkeypatch.context() as mp:
        meter = _Meter(mp, path)
        assert _save_slot_to_history(state, slot) is True

    assert (meter.full_writes, meter.built) == (1, 1)
    rows = [json.loads(ln) for ln in path.read_bytes().splitlines()[1:]]
    assert {"items": [1, 2, 3]} in [r.get("meta", {}).get("nested") for r in rows]
    assert _rebuilt_from_scratch(state, slot, path, monkeypatch) == path.read_bytes()


def test_rows_outside_the_cache_bounds_are_built_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, slot, path = _primed(tmp_path, monkeypatch, WINDOW)
    # Room for the newest 100 of the 300 rows; the 200 older ones go uncached.
    monkeypatch.setattr(chat_persistence, "_entry_cache_bounds", lambda: (100, 1 << 30))
    _edit_and_save(state, slot, path, monkeypatch, -1)  # refill under the small bound

    meter, written = _edit_and_save(state, slot, path, monkeypatch, -2)

    assert meter.full_writes == 1
    assert meter.built == WINDOW - 100 + 1
    assert _rebuilt_from_scratch(state, slot, path, monkeypatch) == written
