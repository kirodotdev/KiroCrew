"""A launch job file the store cannot read is skipped, and reported as unreadable.

``LaunchJobStore.get`` answers ``None`` for a job file that holds no readable job,
so ``list`` drops that one job and still returns the others. Bytes that are not
UTF-8, bad JSON (a file cut short) and JSON that is not an object are all such a
record. ``snapshot`` reports each one's id, because its status is unknown and it
may be a launch still running: launch admission refuses while one exists.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import pytest

from kiro_crew.cloud import launch_job as lj

# Not UTF-8: 0xff never starts a UTF-8 sequence.
_UNDECODABLE = b'\xff\xfe{"id": "x"}'

# One entry per way an existing job file can hold no readable job.
_UNREADABLE = {
    "not_utf8": _UNDECODABLE,
    "bad_json": b'{"status": "running", "tag": "kc-',
    "not_an_object": b'["running"]',
    # An object whose fields have the wrong JSON type.
    "created_at_null": b'{"status": "running", "created_at": null}',
    "created_at_text": b'{"status": "running", "created_at": "soon"}',
    "step_not_an_object": b'{"status": "running", "steps": ["provision"]}',
    # A whole number too large for a float: float() raises OverflowError.
    "created_at_too_large": b'{"status": "running", "created_at": 1' + b"0" * 400 + b"}",
}

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


@pytest.mark.parametrize("kind", sorted(_UNREADABLE))
def test_a_job_file_that_cannot_be_read_is_skipped_and_reported(tmp_path, kind) -> None:
    store = lj.LaunchJobStore(root=tmp_path / "launch-jobs")
    good = store.create(profile="dev", region="us-east-1", size_key="balanced")
    bad = store.create(profile="dev", region="us-east-1", size_key="balanced")
    store._path(bad.id).write_bytes(_UNREADABLE[kind])

    jobs, unreadable = _call(store.snapshot)

    assert _call(lambda: store.get(bad.id)) is None
    assert [job.id for job in jobs] == [good.id]
    assert unreadable == [bad.id]
    assert store.unreadable_ids() == [bad.id]


def test_a_job_file_whose_read_keeps_failing_is_reported(tmp_path, monkeypatch) -> None:
    """A file that exists but every read attempt fails has an unknown status too."""
    store = lj.LaunchJobStore(root=tmp_path / "launch-jobs")
    good = store.create(profile="dev", region="us-east-1", size_key="balanced")
    bad = store.create(profile="dev", region="us-east-1", size_key="balanced")
    bad_path = store._path(bad.id)
    real_read_text = type(bad_path).read_text

    def read_text(self, *a, **k):
        if self == bad_path:
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(type(bad_path), "read_text", read_text)

    jobs, unreadable = store.snapshot()

    assert [job.id for job in jobs] == [good.id]
    assert unreadable == [bad.id]


def test_a_job_file_that_is_gone_is_absent_not_unreadable(tmp_path) -> None:
    store = lj.LaunchJobStore(root=tmp_path / "launch-jobs")
    gone = store.create(profile="dev", region="us-east-1", size_key="balanced")
    path = store._path(gone.id)
    path.unlink()

    assert store._read(path) == (None, False)
    assert store.snapshot() == ([], [])


def test_snapshot_reads_each_file_once_for_both_answers(tmp_path, monkeypatch) -> None:
    """The jobs and the unreadable ids come from one read of each file.

    Two scans let a worker's save() rewrite a file between them, so a running job
    could be skipped by the first and read as valid by neither.
    """
    store = lj.LaunchJobStore(root=tmp_path / "launch-jobs")
    good = store.create(profile="dev", region="us-east-1", size_key="balanced")
    bad = store.create(profile="dev", region="us-east-1", size_key="balanced")
    store._path(bad.id).write_bytes(_UNDECODABLE)
    cut_short = store.create(profile="d", region="us-east-1", size_key="balanced")
    store._path(cut_short.id).write_text("{not json", encoding="utf-8")
    reads: list[str] = []
    real_read = store._read

    def counting_read(path):
        reads.append(path.stem)
        return real_read(path)

    monkeypatch.setattr(store, "_read", counting_read)

    jobs, unreadable = _call(store.snapshot)

    assert [job.id for job in jobs] == [good.id]
    assert unreadable == sorted([bad.id, cut_short.id])
    assert sorted(reads) == sorted(set(reads)) and len(reads) == 3
