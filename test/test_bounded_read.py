"""A capped read returns what ``read(limit)`` returns and never asks for the limit.

``stream.read(cap + 1)`` makes CPython allocate ``cap + 1`` before the first byte
arrives, so a small file under a large cap costs the cap. These tests pin the two
halves of :func:`kiro_crew.bounded_read.bounded_read`: the bytes and the refusals
are the ones the single read gave, and no request is larger than
``READ_CHUNK_BYTES`` or than what the open file holds.

Sizes are recorded from the calls themselves, so nothing here reads a clock.
"""

from __future__ import annotations

import errno
import io
import os
from pathlib import Path

import pytest

from kiro_crew import agent_discovery
from kiro_crew import bounded_read as bounded_read_module
from kiro_crew import hooks, jsonl_util
from kiro_crew.bounded_read import READ_CHUNK_BYTES, bounded_read, bounded_read_fd

_CHUNK = READ_CHUNK_BYTES


class _Recorder:
    """A stream with no descriptor that records the size of every request."""

    def __init__(self, data: bytes) -> None:
        self._inner = io.BytesIO(data)
        self.requests: list[int] = []

    def read(self, size: int, /) -> bytes:
        self.requests.append(size)
        return self._inner.read(size)


class _RecordingFile:
    """An open file that records each request and can run a step before one."""

    def __init__(self, handle: io.BufferedReader, before_read=None) -> None:
        self._handle = handle
        self._before_read = before_read
        self.requests: list[int] = []

    def fileno(self) -> int:
        return self._handle.fileno()

    def read(self, size: int, /) -> bytes:
        if self._before_read is not None and not self.requests:
            self._before_read()
        self.requests.append(size)
        return self._handle.read(size)


def _payload(size: int) -> bytes:
    """*size* bytes whose every position is distinguishable from its neighbours."""
    return bytes(i % 251 for i in range(size))


# Caps below, at and above one chunk, each met by a stream that is empty, one
# unit short of the cap, exactly at it, one over it and far over it.
_CAPS = (0, 1, 100, _CHUNK - 1, _CHUNK, _CHUNK + 1, 3 * _CHUNK + 7)


def _sizes_around(cap: int) -> list[int]:
    return sorted({0, 1, max(cap - 1, 0), cap, cap + 1, cap + 2 * _CHUNK + 5})


@pytest.mark.parametrize("cap", _CAPS)
def test_a_stream_gives_the_bytes_of_one_read_in_requests_of_at_most_a_chunk(cap: int) -> None:
    for size in _sizes_around(cap):
        data = _payload(size)
        stream = _Recorder(data)

        got = bounded_read(stream, cap + 1)

        assert got == io.BytesIO(data).read(cap + 1), (cap, size)
        # The caller's own refusal, "longer than the cap", is unchanged.
        assert (len(got) > cap) == (size > cap), (cap, size)
        assert max(stream.requests) <= _CHUNK, (cap, size, stream.requests)
        # Never asked past the limit either: the requests sum to what a full
        # read needs plus, at most, the one that learned the stream had ended.
        assert sum(stream.requests[:-1]) <= cap + 1, (cap, size, stream.requests)


def test_a_small_file_under_a_large_cap_is_asked_for_what_it_holds(tmp_path: Path) -> None:
    path = tmp_path / "small.bin"
    data = _payload(4096)
    path.write_bytes(data)

    with path.open("rb") as handle:
        stream = _RecordingFile(handle)
        got = bounded_read(stream, hooks.MAX_FILE_BYTES + 1)

    assert got == data
    assert stream.requests[0] == len(data) + 1
    assert max(stream.requests) <= _CHUNK


def test_a_large_file_is_taken_in_one_request_sized_by_the_file(tmp_path: Path) -> None:
    path = tmp_path / "large.bin"
    data = _payload(3 * _CHUNK + 11)
    path.write_bytes(data)

    with path.open("rb") as handle:
        stream = _RecordingFile(handle)
        got = bounded_read(stream, hooks.MAX_FILE_BYTES + 1)

    assert got == data
    assert stream.requests[0] == len(data) + 1
    assert all(size <= _CHUNK for size in stream.requests[1:])


@pytest.mark.parametrize("over", [0, 1, 2 * _CHUNK + 3])
def test_a_file_at_or_over_the_cap_is_never_asked_for_more_than_the_limit(
    tmp_path: Path, over: int
) -> None:
    cap = _CHUNK + 9
    path = tmp_path / "edge.bin"
    data = _payload(cap + over)
    path.write_bytes(data)

    with path.open("rb") as handle:
        stream = _RecordingFile(handle)
        got = bounded_read(stream, cap + 1)

    assert got == data[: cap + 1]
    assert (len(got) > cap) == (over > 0)
    assert max(stream.requests) <= cap + 1


@pytest.mark.parametrize(
    ("grown_by", "expect_refusal"),
    [(10, False), (_CHUNK * 2 + 5, False), (_CHUNK * 4, True)],
)
def test_a_file_that_grows_after_its_size_was_sampled_is_read_to_the_limit(
    tmp_path: Path, grown_by: int, expect_refusal: bool
) -> None:
    cap = _CHUNK * 3
    path = tmp_path / "growing.bin"
    start = _payload(1000)
    extra = _payload(grown_by)[::-1]
    path.write_bytes(start)

    def grow() -> None:
        # A writer appending between the size sample and the first read.
        with path.open("ab") as writer:
            writer.write(extra)

    with path.open("rb") as handle:
        stream = _RecordingFile(handle, before_read=grow)
        got = bounded_read(stream, cap + 1)

    assert got == (start + extra)[: cap + 1]
    assert (len(got) > cap) is expect_refusal
    # Sized by the 1000 bytes sampled, then by the chunk: never by the cap.
    assert stream.requests[0] == len(start) + 1
    assert all(size <= _CHUNK for size in stream.requests[1:])


@pytest.mark.parametrize("limit", [100, 512, 513, 9000])
def test_a_stream_that_reports_no_size_is_read_by_the_chunk(limit: int) -> None:
    # Small enough to sit in the pipe's buffer on every platform (4 KB on Windows),
    # so the writer closes before any reader exists.
    data = _payload(512)
    read_end, write_end = os.pipe()
    with os.fdopen(write_end, "wb") as writer:
        writer.write(data)
    with os.fdopen(read_end, "rb") as handle:
        stream = _RecordingFile(handle)
        got = bounded_read(stream, limit)

    assert got == data[:limit]
    assert max(stream.requests) <= _CHUNK


def test_a_stream_that_answers_short_is_drained_to_the_limit() -> None:
    data = _payload(40)

    class _Trickle(_Recorder):
        def read(self, size: int, /) -> bytes:
            return super().read(min(size, 3))

    assert bounded_read(_Trickle(data), 25) == data[:25]
    assert bounded_read(_Trickle(data), 400) == data


@pytest.mark.parametrize("limit", [1, 4, 5, 6, 7, 8, 13, 14, 15, 200])
def test_a_text_stream_gives_the_characters_of_one_read(
    monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    # Multi-byte characters and a CRLF that a chunk boundary can split.
    raw = "añ\r\nb€c\r\n\r\nd😀e\r\n".encode("utf-8")

    def stream() -> io.TextIOWrapper:
        return io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8", newline=None)

    monkeypatch.setattr(bounded_read_module, "READ_CHUNK_BYTES", 3)

    got = bounded_read(stream(), limit)

    assert got == stream().read(limit)
    assert isinstance(got, str)


def test_a_text_file_is_asked_for_what_it_holds(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("stamp-1.2.3\n", encoding="utf-8")

    with path.open(encoding="utf-8") as handle:
        assert bounded_read(handle, 1024 * 1024 + 1) == "stamp-1.2.3\n"


@pytest.mark.parametrize("empty", [b"", ""])
def test_an_empty_stream_gives_the_stream_s_own_empty_value(empty) -> None:
    stream = io.BytesIO(b"") if isinstance(empty, bytes) else io.StringIO("")

    got = bounded_read(stream, 10)

    assert got == empty
    assert type(got) is type(empty)


def test_a_zero_limit_reads_nothing() -> None:
    stream = _Recorder(b"content")

    assert bounded_read(stream, 0) == b""
    assert stream.requests == [0]


def test_a_negative_limit_is_refused_before_any_read() -> None:
    stream = _Recorder(b"content")

    with pytest.raises(ValueError):
        bounded_read(stream, -1)
    assert stream.requests == []


def _record_os_reads(monkeypatch: pytest.MonkeyPatch, fd: int) -> list[int]:
    requests: list[int] = []
    real_read = os.read

    def recording(target: int, size: int) -> bytes:
        if target == fd:
            requests.append(size)
        return real_read(target, size)

    monkeypatch.setattr(os, "read", recording)
    return requests


@pytest.mark.parametrize("size", [0, 1, 4096, _CHUNK + 1, 3 * _CHUNK + 7])
@pytest.mark.parametrize("cap", [4096, 3 * _CHUNK])
def test_a_descriptor_gives_the_bytes_up_to_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int, cap: int
) -> None:
    path = tmp_path / "record.bin"
    data = _payload(size)
    path.write_bytes(data)

    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        requests = _record_os_reads(monkeypatch, fd)
        got = bounded_read_fd(fd, cap + 1)
    finally:
        os.close(fd)

    assert got == data[: cap + 1]
    assert (len(got) > cap) == (size > cap)
    assert max(requests) <= max(_CHUNK, min(size, cap) + 1)


# -- The guarded readers ------------------------------------------------------
#
# Each one opens the file itself, so the request sizes are recorded on the handle
# it builds: ``os.fdopen`` is wrapped for the one inode under test.


class _SpiedHandle:
    def __init__(self, handle, requests: list[int], before_read) -> None:
        self._handle = handle
        self._requests = requests
        self._before_read = before_read

    def read(self, size: int = -1, /) -> bytes:
        if self._before_read is not None and not self._requests:
            self._before_read()
        self._requests.append(size)
        return self._handle.read(size)

    def __enter__(self) -> "_SpiedHandle":
        return self

    def __exit__(self, *exc: object) -> None:
        self._handle.close()

    def __getattr__(self, name: str):
        return getattr(self._handle, name)


def _spy_on_reads_of(monkeypatch: pytest.MonkeyPatch, path: Path, before_read=None) -> list[int]:
    """Record every ``read`` size a reader passes to a handle opened on *path*."""
    target = path.stat()
    requests: list[int] = []
    real_fdopen = os.fdopen

    def fdopen(fd, *args, **kwargs):
        handle = real_fdopen(fd, *args, **kwargs)
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (target.st_dev, target.st_ino):
            return handle
        return _SpiedHandle(handle, requests, before_read)

    monkeypatch.setattr(os, "fdopen", fdopen)
    return requests


def _guarded_read(kind: str, path: Path):
    if kind == "text":
        return hooks.safe_read_file(str(path)).encode("utf-8")
    if kind == "bytes":
        return hooks.safe_read_file_bytes(str(path))
    if kind == "nolink":
        return hooks.safe_read_file_bytes_nolink(str(path))
    if kind == "identity":
        info = path.stat()
        return hooks.safe_read_file_bytes_with_identity(str(path), {(info.st_dev, info.st_ino)})
    if kind == "spec":
        return agent_discovery._read_spec_bytes(Path(os.path.realpath(path)))
    raise AssertionError(kind)


_GUARDED = ("text", "bytes", "nolink", "identity", "spec")


def _assert_refused_as_too_large(kind: str, path: Path) -> None:
    # The text reader refuses with EFBIG, at the open handle's size or mid-read;
    # the byte readers refuse on the length read with their own error.
    if kind == "text":
        with pytest.raises(OSError) as caught:
            _guarded_read(kind, path)
        assert caught.value.errno == errno.EFBIG
    else:
        with pytest.raises(hooks.FileTooLargeError):
            _guarded_read(kind, path)


@pytest.mark.parametrize("kind", _GUARDED)
def test_a_guarded_read_of_a_small_file_never_asks_for_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    path = tmp_path / "skill.md"
    data = b"line\n" * 800
    path.write_bytes(data)
    requests = _spy_on_reads_of(monkeypatch, path)

    assert _guarded_read(kind, path) == data

    assert requests, "the reader never read the handle this test spies on"
    assert max(requests) <= _CHUNK, requests


@pytest.mark.parametrize("kind", _GUARDED)
@pytest.mark.parametrize("size", ["empty", "at", "over", "far over"])
def test_a_guarded_reader_keeps_its_cap_at_every_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, size: str
) -> None:
    cap = 2 * _CHUNK + 5
    monkeypatch.setattr(hooks, "MAX_FILE_BYTES", cap)
    path = tmp_path / "spec.json"
    data = _payload({"empty": 0, "at": cap, "over": cap + 1, "far over": 4 * cap}[size])
    # Printable, so the text reader decodes the same bytes it is compared with.
    data = bytes(65 + (b % 26) for b in data)
    path.write_bytes(data)

    if len(data) <= cap:
        assert _guarded_read(kind, path) == data
        return
    _assert_refused_as_too_large(kind, path)


@pytest.mark.parametrize("kind", _GUARDED)
def test_a_guarded_reader_refuses_a_file_that_grows_past_the_cap_mid_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    cap = 2 * _CHUNK + 5
    monkeypatch.setattr(hooks, "MAX_FILE_BYTES", cap)
    path = tmp_path / "growing.json"
    path.write_bytes(b"a" * 1000)

    def grow() -> None:
        with path.open("ab") as writer:
            writer.write(b"b" * (2 * cap))

    requests = _spy_on_reads_of(monkeypatch, path, before_read=grow)

    _assert_refused_as_too_large(kind, path)
    # Refused by reading one unit past the cap in pieces, not by asking for it.
    assert max(requests) <= _CHUNK, requests


def test_the_ceiling_reader_asks_for_what_the_file_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "events.jsonl"
    data = b'{"n": 1}\n' * 300
    path.write_bytes(data)
    requests = _spy_on_reads_of(monkeypatch, path)

    with jsonl_util.open_regular_nofollow(path, max_bytes=hooks.MAX_FILE_BYTES) as handle:
        head = handle.read(10)
        rest = handle.read()

    assert head + rest == data
    assert max(requests) <= _CHUNK, requests
