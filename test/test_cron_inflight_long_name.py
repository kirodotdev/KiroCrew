"""A running job's marker reads back whatever its name, so a stall can name it.

A job name may be 500 characters, and ``json.dumps`` writes an emoji as a
12-byte escape pair, so an uncut name pushed the marker past the 4096-byte size
the reader accepts: ``read_markers`` skipped it, and a hard exit during that job
left nothing that named it. The marker keeps the same 128-character name the
attribution record keeps.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew import cron_inflight


def test_a_marker_for_a_long_emoji_name_reads_back(tmp_path: Path) -> None:
    name = "\U0001f680" * 500
    cron_inflight.write_marker(tmp_path, "job1", name, 1000.0, run="aaaa")

    markers = cron_inflight.read_markers(tmp_path)

    assert [m.job_id for m in markers] == ["job1"]
    assert markers[0].name == name[:128]


def test_a_short_name_is_kept_whole(tmp_path: Path) -> None:
    cron_inflight.write_marker(tmp_path, "job1", "nightly digest", 1000.0, run="aaaa")

    assert [m.name for m in cron_inflight.read_markers(tmp_path)] == ["nightly digest"]
