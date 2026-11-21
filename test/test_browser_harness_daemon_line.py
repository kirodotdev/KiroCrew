"""``browser_harness.daemon_line``: op arguments sized by their bytes on the daemon's line."""

from __future__ import annotations

import json

import pytest

from kiro_crew.browser_harness import daemon_line


def test_wire_bytes_count_the_json_escape_not_the_characters() -> None:
    assert daemon_line.wire_bytes("abc") == 3
    assert daemon_line.wire_bytes('"\\') == 4
    assert daemon_line.wire_bytes("漢") == 6
    assert daemon_line.wire_bytes("😀") == 12


def test_an_expression_fits_up_to_the_line_less_the_envelope() -> None:
    room = daemon_line.DAEMON_LINE_BYTES - daemon_line.ENVELOPE_BYTES
    assert daemon_line.expression_fits("x" * room)
    assert not daemon_line.expression_fits("x" * (room + 1))
    # A quote in the expression costs two bytes on the line.
    assert not daemon_line.expression_fits('"' * (room // 2 + 1))


@pytest.mark.parametrize(
    "text",
    ["", "short", "x" * 40_000, "漢" * 20_000, '😀a"' * 5_000],
    ids=["empty", "short", "ascii", "cjk", "mixed"],
)
def test_type_chunks_keep_the_text_and_each_piece_under_the_limit(text: str) -> None:
    chunks = daemon_line.type_chunks(text)
    assert "".join(chunks) == text
    assert all(daemon_line.wire_bytes(c) <= daemon_line.TYPE_CHUNK_BYTES for c in chunks)
    assert all(chunks) or chunks == [""]
    line = max(len(json.dumps({"text": c})) for c in chunks)
    assert line < daemon_line.DAEMON_LINE_BYTES


def test_a_text_that_fits_is_one_piece() -> None:
    assert daemon_line.type_chunks("hello") == ["hello"]
    assert len(daemon_line.type_chunks("x" * daemon_line.TYPE_CHUNK_BYTES)) == 1
    assert len(daemon_line.type_chunks("x" * (daemon_line.TYPE_CHUNK_BYTES + 1))) == 2
