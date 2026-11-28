"""An over-nested injection receipt must leave service status unknown."""

from datetime import datetime, timezone

import pytest
from stray_line_helpers import cyclic_gc_quiesced, too_deep_line  # noqa: F401

from kiro_crew import injection_receipts, memory_lesson_dedup


@pytest.mark.usefixtures("cyclic_gc_quiesced")
def test_a_receipt_past_the_decoder_limit_counts_as_unknown(tmp_path, monkeypatch):
    instant = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
    day = instant.astimezone().date().isoformat()
    (tmp_path / f"{day}.jsonl").write_bytes(too_deep_line())
    monkeypatch.setattr(injection_receipts, "_receipt_dir", lambda: tmp_path)

    assert memory_lesson_dedup._served_keys("member-alice", {"pending-lesson"}, instant) is None
