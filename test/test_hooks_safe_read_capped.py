"""The guarded byte reads size their buffer by the file, not by the 50 MB ceiling."""

from __future__ import annotations

import os
import tracemalloc

import pytest

from kiro_crew import hooks


@pytest.fixture
def traced():
    started = not tracemalloc.is_tracing()
    if started:
        tracemalloc.start()
    yield
    if started:
        tracemalloc.stop()


def _peak_of(fn):
    tracemalloc.reset_peak()
    base, _ = tracemalloc.get_traced_memory()
    result = fn()
    _, peak = tracemalloc.get_traced_memory()
    return result, peak - base


@pytest.mark.parametrize("reader", ["plain", "identity"])
def test_small_file_read_does_not_allocate_the_ceiling(tmp_path, traced, reader):
    target = tmp_path / "small.json"
    target.write_bytes(b'{"a": 1}\n' * 20)
    if reader == "plain":
        call = lambda: hooks.safe_read_file_bytes(str(target))  # noqa: E731
    else:
        allowed = {hooks.stat_identity(str(target))}
        call = lambda: hooks.safe_read_file_bytes_with_identity(str(target), allowed)  # noqa: E731

    data, peak = _peak_of(call)

    assert data == target.read_bytes()
    assert peak < 1 << 20, f"read of a 180-byte file peaked at {peak} bytes"


def test_ceiling_still_refuses_one_byte_over(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 64)
    exact = tmp_path / "exact"
    exact.write_bytes(b"x" * 64)
    over = tmp_path / "over"
    over.write_bytes(b"x" * 65)

    assert hooks.safe_read_file_bytes(str(exact)) == b"x" * 64
    with pytest.raises(hooks.FileTooLargeError):
        hooks.safe_read_file_bytes(str(over))


@pytest.mark.parametrize("cap, expected", [(64, 65), (200, 100)])
def test_a_source_reporting_no_size_is_read_up_to_the_cap(cap, expected):
    # A pipe fstats with st_size 0, as procfs files do; the bytes behind it still count.
    rfd, wfd = os.pipe()
    os.write(wfd, b"y" * 100)
    os.close(wfd)
    with os.fdopen(rfd, "rb") as fh:
        data = hooks._read_capped(fh, cap)
    assert data == b"y" * expected
