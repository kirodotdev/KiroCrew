"""A cron in-flight marker whose pid does not fit an int is skipped, not raised.

JSON ``1e999`` parses as an infinite float, and ``int()`` of it raises
``OverflowError``, which is not a ``ValueError``. Both marker readers skip a
file of a shape they never write, so they skip this one too.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kiro_crew import cron_inflight


def _no_raise(fn: Callable[..., Any], *args: Any) -> Any:
    """*fn*'s result, or a marker string, so a raise fails the assert that names it."""
    try:
        return fn(*args)
    except OverflowError:
        return "raised OverflowError"


def test_read_markers_skips_a_marker_whose_pid_overflows(tmp_path: Path) -> None:
    cron_inflight.write_marker(tmp_path, "good", "kept", 1000.0, run="aaaa")
    cron_inflight.write_marker(tmp_path, "bad", "broken", 1001.0, run="bbbb")
    bad = cron_inflight.marker_path(tmp_path, "bad", "bbbb")
    data = json.loads(bad.read_text(encoding="utf-8"))
    bad.write_text(json.dumps(data).replace(f'"pid": {data["pid"]}', '"pid": 1e999'))

    markers = _no_raise(cron_inflight.read_markers, tmp_path)
    assert markers != "raised OverflowError"
    assert [m.job_id for m in markers] == ["good"]


def test_the_attribution_record_with_an_overflowing_pid_reads_as_none(tmp_path: Path) -> None:
    cron_inflight.write_marker(tmp_path, "good", "kept", 1000.0, run="aaaa")
    (marker,) = cron_inflight.read_markers(tmp_path)
    assert cron_inflight.record_attribution(tmp_path, "dump-1", [marker], [])
    record = cron_inflight.running_dir(tmp_path) / cron_inflight.ATTRIBUTION_RECORD_NAME
    text = record.read_text(encoding="utf-8")
    record.write_text(text.replace(f'"pid": {marker.pid}', '"pid": 1e999'), encoding="utf-8")

    assert _no_raise(cron_inflight.read_recorded_attribution, tmp_path, "dump-1") is None
