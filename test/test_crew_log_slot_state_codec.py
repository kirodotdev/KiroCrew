"""``slot_state_codec``: a slot fold state stored plain when small, compressed when large.

The round trip keeps the state's key order, and a damaged or oversized compressed
payload is refused rather than decoded. The restart behaviour this exists for is in
``test_crew_log_slot_savepoint_large_state.py``.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.crew_log import slot_state_codec
from kiro_crew.projection import MAX_PAYLOAD_BYTES


def test_a_small_state_is_still_stored_plain():
    """Below half the cap the payload is unchanged, so older builds keep reading it."""
    encoded = json.dumps({"b": 1, "a": 2})
    assert slot_state_codec.encode_state(encoded) == {slot_state_codec.STATE_JSON: encoded}


def test_a_compressed_state_keeps_its_key_order():
    """The fold's meaning depends on insertion order, so the round trip keeps it."""
    state = {f"k{n:05d}": "x" * 40 for n in range(30_000, 0, -1)}
    encoded = json.dumps(state)
    stored = slot_state_codec.encode_state(encoded)
    assert slot_state_codec.STATE_ZLIB in stored
    assert list(json.loads(slot_state_codec.decode_state(stored) or "{}")) == list(state)


@pytest.mark.parametrize("size", [10, 2 * MAX_PAYLOAD_BYTES])
def test_a_lone_surrogate_round_trips_plain_and_compressed(size):
    """A writer can accept a lone surrogate; the savepoint must not raise on it."""
    encoded = json.dumps({"artifacts": {"path": "\ud800"}, "pad": "x" * size}, ensure_ascii=False)
    assert slot_state_codec.decode_state(slot_state_codec.encode_state(encoded)) == encoded


@pytest.mark.parametrize(
    "stored",
    [
        {slot_state_codec.STATE_ZLIB: "not base64!"},
        {slot_state_codec.STATE_ZLIB: "aGVsbG8="},
        {slot_state_codec.STATE_ZLIB: 7},
        {},
    ],
    ids=["bad-base64", "not-zlib", "wrong-type", "neither-key"],
)
def test_a_damaged_compressed_state_is_refused(stored):
    assert slot_state_codec.decode_state(stored) is None


def test_a_state_that_inflates_past_the_bound_is_refused(_floor_monkeypatch):
    """A bound on what one file can make the reader allocate."""
    _floor_monkeypatch.setattr(slot_state_codec, "MAX_DECODED_BYTES", 1024)
    stored = slot_state_codec.encode_state(json.dumps({"a": "x" * (2 * MAX_PAYLOAD_BYTES)}))
    assert slot_state_codec.decode_state(stored) is None
