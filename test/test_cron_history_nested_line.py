"""A cron history line nested past the recursion limit is skipped, not raised.

``json.loads`` raises ``RecursionError`` on such a line, and it is not a
``ValueError``. The history readers skip malformed lines, so they skip this one
too: the job's history, the run history page and a run's detail still load.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from stray_line_helpers import cyclic_gc_quiesced  # noqa: F401 - fixture
from stray_line_helpers import too_deep_to_decode

from kiro_crew.cron_history import CronHistoryStore, CronRunRecord


def _no_raise(fn: Callable[..., Any], *args: Any) -> Any:
    """*fn*'s result, or a marker string, so a raise fails the assert that names it."""
    try:
        return fn(*args)
    except RecursionError:
        return "raised RecursionError"


@pytest.fixture
def store(tmp_path: Path) -> CronHistoryStore:
    store = CronHistoryStore(base_dir=tmp_path)
    store._append_sync(CronRunRecord(run_id="r1", job_id="job1", started_at=1.0))
    nested = too_deep_to_decode() + "\n"
    for path in (store._job_path("job1"), store._index_path):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(nested)
    return store


@pytest.mark.usefixtures("cyclic_gc_quiesced")
def test_job_history_skips_a_nested_line(store: CronHistoryStore) -> None:
    result = _no_raise(store._get_job_history_sync, "job1", 0, 20)
    assert result != "raised RecursionError"
    rows, total = result
    assert total == 1 and rows[0]["run_id"] == "r1"


@pytest.mark.usefixtures("cyclic_gc_quiesced")
def test_all_history_skips_a_nested_line(store: CronHistoryStore) -> None:
    result = _no_raise(store._get_all_history_sync, 0, 20, None)
    assert result != "raised RecursionError"
    rows, total = result
    assert total == 1 and rows[0]["run_id"] == "r1"


@pytest.mark.usefixtures("cyclic_gc_quiesced")
def test_run_detail_skips_a_nested_line(store: CronHistoryStore) -> None:
    row = _no_raise(store._get_run_detail_sync, "job1", "missing")
    assert row is None
