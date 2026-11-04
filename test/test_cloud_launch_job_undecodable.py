"""A launch job file whose bytes are not UTF-8 is skipped like one that does not parse.

``LaunchJobStore.get`` answers ``None`` for a job file that is not valid JSON, so
``list`` drops that one job and still returns the others. A file whose bytes do
not decode as UTF-8 is the same unusable record, so it is skipped the same way
instead of raising ``UnicodeDecodeError`` out of ``list`` and hiding every job.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import pytest

from kiro_crew.cloud import launch_job as lj

# Not UTF-8: 0xff never starts a UTF-8 sequence.
_UNDECODABLE = b'\xff\xfe{"id": "x"}'

T = TypeVar("T")


def _call(fn: Callable[[], T]) -> T:
    """Run *fn*, failing the test (not erroring it) when it raises on the bytes."""
    try:
        return fn()
    except UnicodeDecodeError as exc:
        pytest.fail(f"a job file that is not UTF-8 raised {exc!r}")


def test_a_job_file_that_is_not_utf8_reads_as_absent(tmp_path) -> None:
    store = lj.LaunchJobStore(root=tmp_path / "launch-jobs")
    bad = store.create(profile="dev", region="us-east-1", size_key="balanced")
    store._path(bad.id).write_bytes(_UNDECODABLE)

    assert _call(lambda: store.get(bad.id)) is None


def test_list_skips_a_job_file_that_is_not_utf8_and_keeps_the_rest(tmp_path) -> None:
    store = lj.LaunchJobStore(root=tmp_path / "launch-jobs")
    good = store.create(profile="dev", region="us-east-1", size_key="balanced")
    bad = store.create(profile="dev", region="us-east-1", size_key="balanced")
    store._path(bad.id).write_bytes(_UNDECODABLE)

    assert [job.id for job in _call(store.list)] == [good.id]


def test_snapshot_reads_each_file_once_for_both_answers(tmp_path, monkeypatch) -> None:
    """The jobs and the unreadable ids come from one read of each file.

    Two scans let a worker's save() rewrite a file between them, so a running job
    could be skipped by the first and read as valid by neither.
    """
    store = lj.LaunchJobStore(root=tmp_path / "launch-jobs")
    good = store.create(profile="dev", region="us-east-1", size_key="balanced")
    bad = store.create(profile="dev", region="us-east-1", size_key="balanced")
    store._path(bad.id).write_bytes(_UNDECODABLE)
    store._path(store.create(profile="d", region="us-east-1", size_key="balanced").id).write_text(
        "{not json", encoding="utf-8"
    )
    reads: list[str] = []
    real_read = store._read

    def counting_read(path):
        reads.append(path.stem)
        return real_read(path)

    monkeypatch.setattr(store, "_read", counting_read)

    jobs, undecodable = _call(store.snapshot)

    assert [job.id for job in jobs] == [good.id]
    assert undecodable == [bad.id]  # bad JSON is skipped but not reported, as before
    assert sorted(reads) == sorted(set(reads)) and len(reads) == 3
