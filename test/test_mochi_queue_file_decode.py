"""A queue file that is not valid UTF-8 must read as unreadable, not raise.

``read_queue`` promises "None on any failure", and its callers rely on it: the
poller treats None as "no queue" and the MCP server falls back with
``read_queue(path) or _empty_queue(now)``. Before this fix the read step caught
only ``OSError``, so one non-UTF-8 byte (a hand edit saved in a legacy code
page, or a corrupted file) raised ``UnicodeDecodeError`` out of both callers.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.mochi.queue_file import read_queue

# "café" in latin-1: byte 0xe9 is not valid UTF-8.
_LATIN1_QUEUE = b'{"tasks": [], "planned_at": "caf\xe9", "planned_until": "2026-10-26T00:00:00Z"}'


def test_undecodable_queue_file_reads_as_none_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    queue = tmp_path / "queue.json"
    queue.write_bytes(_LATIN1_QUEUE)

    with caplog.at_level(logging.WARNING):
        try:
            result = read_queue(queue)
        except UnicodeDecodeError as exc:
            pytest.fail(f"an undecodable queue file escaped read_queue: {exc!r}")

    assert result is None
    assert any(
        "[read_queue] Failed to read" in rec.getMessage() and str(queue) in rec.getMessage()
        for rec in caplog.records
    ), [rec.getMessage() for rec in caplog.records]


def test_utf8_queue_file_still_reads(tmp_path: Path) -> None:
    queue = tmp_path / "queue.json"
    queue.write_text(
        '{"tasks": [], "planned_at": "café", "planned_until": "2026-10-26T00:00:00Z"}',
        encoding="utf-8",
    )

    result = read_queue(queue)

    assert result is not None
    assert result["planned_at"] == "café"
