"""A byte that is not UTF-8 in a cron history file costs its line, not the file.

Decoding a whole history file raises ``UnicodeDecodeError``, which is not an
``OSError``, so one torn or foreign byte made every read raise and every append
raise in its trim. The readers skip such a line, and the trim and the index
rewrite on delete keep every line they keep byte for byte.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.cron_history import CronHistoryStore, CronRunRecord

_GARBAGE = b"\xff\xfe not utf-8\n"


def _no_raise(fn: Callable[..., Any], *args: Any) -> Any:
    """*fn*'s result, or a marker string, so a raise fails the assert that names it."""
    try:
        return fn(*args)
    except UnicodeDecodeError:
        return "raised UnicodeDecodeError"


@pytest.fixture
def store(tmp_path: Path) -> CronHistoryStore:
    store = CronHistoryStore(base_dir=tmp_path)
    store._append_sync(CronRunRecord(run_id="r1", job_id="job1", started_at=1.0))
    for path in (store._job_path("job1"), store._index_path):
        with open(path, "ab") as handle:
            handle.write(_GARBAGE)
    return store


def test_the_readers_skip_an_undecodable_line(store: CronHistoryStore) -> None:
    job = _no_raise(store._get_job_history_sync, "job1", 0, 20)
    index = _no_raise(store._get_all_history_sync, 0, 20, None)
    detail = _no_raise(store._get_run_detail_sync, "job1", "r1")

    assert job != "raised UnicodeDecodeError" and job[1] == 1
    assert index != "raised UnicodeDecodeError" and index[1] == 1
    assert detail != "raised UnicodeDecodeError" and detail["run_id"] == "r1"


def test_an_append_still_lands_and_trims(store: CronHistoryStore) -> None:
    store._max_records_per_job = 2
    store._max_index_records = 2

    result = _no_raise(store._append_sync, CronRunRecord(run_id="r2", job_id="job1"))

    assert result != "raised UnicodeDecodeError"
    rows, _total = store._get_job_history_sync("job1", 0, 20)
    assert [row["run_id"] for row in rows] == ["r2"]
    # The trim kept the newest two lines, the undecodable one among them, unchanged.
    assert _GARBAGE.strip() in store._job_path("job1").read_bytes()


def test_deleting_a_job_keeps_the_other_jobs_rows_byte_for_byte(tmp_path: Path) -> None:
    store = CronHistoryStore(base_dir=tmp_path)
    store._append_sync(CronRunRecord(run_id="keep", job_id="other", summary="caf\u00e9"))
    store._append_sync(CronRunRecord(run_id="gone", job_id="job1"))
    with open(store._index_path, "ab") as handle:
        handle.write(_GARBAGE)
    kept_line = store._index_path.read_bytes().splitlines()[0]

    assert _no_raise(store._delete_job_history_sync, "job1") is True

    assert store._index_path.read_bytes().splitlines() == [kept_line]


def test_a_record_with_a_bad_byte_inside_a_field_is_skipped(tmp_path: Path) -> None:
    """A damaged record is not read as a whole one with a replacement character."""
    store = CronHistoryStore(base_dir=tmp_path)
    store._append_sync(CronRunRecord(run_id="r1", job_id="job1"))
    with open(store._job_path("job1"), "ab") as handle:
        handle.write(b'{"run_id": "r2", "job_id": "job1", "summary": "a\xffb"}\n')

    rows, total = store._get_job_history_sync("job1", 0, 20)

    assert total == 1 and [row["run_id"] for row in rows] == ["r1"]
