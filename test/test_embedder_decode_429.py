"""Regression: bytes_to_floats must not raise on corrupt/legacy blobs (#429).

A blob whose length isn't a multiple of 4 used to raise ``struct.error`` and
abort the whole dedup sweep; a legacy JSON-encoded embedding was mis-decoded as
garbage floats. It now returns ``[]`` (skippable) / decodes JSON correctly.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.knowledge.embedder import bytes_to_floats, floats_to_bytes


def test_binary_roundtrip() -> None:
    vec = [0.1, 0.2, 0.3, 0.4]
    out = bytes_to_floats(floats_to_bytes(vec))
    assert out == pytest.approx(vec)


def test_short_binary_roundtrip() -> None:
    # A short (8-byte) vector must still round-trip — no >=16 floor.
    vec = [1.5, -2.5]
    assert bytes_to_floats(floats_to_bytes(vec)) == pytest.approx(vec)


def test_non_multiple_of_four_returns_empty() -> None:
    # 3 bytes: used to raise struct.error and abort the sweep.
    assert bytes_to_floats(b"\x01\x02\x03") == []


def test_empty_returns_empty() -> None:
    assert bytes_to_floats(b"") == []


def test_legacy_json_blob_decoded() -> None:
    blob = json.dumps([1.0, 2.0, 3.0]).encode("utf-8")
    assert bytes_to_floats(blob) == [1.0, 2.0, 3.0]


def test_non_numeric_json_returns_empty() -> None:
    # A JSON list of non-numbers must not flow into dedup mean-pooling.
    assert bytes_to_floats(json.dumps(["bad"]).encode("utf-8")) == []


def test_overflowing_int_json_returns_empty() -> None:
    # A 309-digit integer overflows float(); must return [] rather than raise
    # OverflowError and abort the dedup sweep.
    blob = json.dumps([int("1" + "0" * 309)]).encode("utf-8")
    assert bytes_to_floats(blob) == []
