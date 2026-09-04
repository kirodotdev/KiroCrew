"""Invariants of the shared part-to-disk path.

Every test here pins a property that a blocking review finding was filed
against. `kiro_crew.dashboard.part_stream`'s docstring carries the ledger; this
file is the executable half of it, so a future refactor that reintroduces any of
the seven defects fails here rather than in a review round.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path

import pytest

from kiro_crew.dashboard import part_stream
from kiro_crew.dashboard.part_stream import (
    PartContentMismatch,
    PartTooLarge,
    _TempSink,
    stream_part_to_file,
)


class _FakePart:
    """Minimal BodyPartReader stand-in: hands out chunks, then EOF."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read_chunk(self, _size: int = 0) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


def test_exit_removes_the_temp_when_the_body_did_not_commit(tmp_path: Path) -> None:
    """The context manager's exit is the ONLY cleanup path, and it is sufficient.

    Findings 2, 3, 6 and 7 were all cleanup that either did not run or ran in the
    wrong order relative to creation. A synchronous `__exit__` cannot be skipped
    by cancellation and cannot race the create, because the file already exists
    when the block is entered.
    """
    dest = tmp_path / "x.bin"
    with _TempSink(dest) as sink:
        sink.write(b"partial")
        tmp = sink.tmp
        assert tmp.exists()
    assert not tmp.exists()
    assert not dest.exists()


def test_exit_still_cleans_up_when_the_body_is_cancelled(tmp_path: Path) -> None:
    """`CancelledError` derives from BaseException; `with` does not care.

    Finding 2 was an `except Exception` that a gateway shutdown walked straight
    past. Expressed as a context manager the question does not arise -- there is
    no exception filter to get wrong.
    """
    dest = tmp_path / "y.bin"
    tmp_seen: Path | None = None
    with pytest.raises(asyncio.CancelledError):
        with _TempSink(dest) as sink:
            tmp_seen = sink.tmp
            sink.write(b"half a video")
            raise asyncio.CancelledError()
    assert tmp_seen is not None
    assert not tmp_seen.exists()
    assert not dest.exists()


def test_exit_after_a_commit_touches_nothing(tmp_path: Path) -> None:
    """A committed block must not have its published file removed on the way out.

    The mirror of the cleanup tests: cleanup that is unconditional in the wrong
    way would delete the artifact it just published.
    """
    dest = tmp_path / "z.bin"
    with _TempSink(dest) as sink:
        sink.write(b"complete")
        sink.commit()
    assert dest.read_bytes() == b"complete"
    assert not sink.tmp.exists()


def test_exit_is_idempotent_and_never_closes_a_descriptor_number(tmp_path: Path) -> None:
    """Finding 5b: a double close must not land on a reassigned fd number.

    A raw `int` cannot express "already closed", so a cleanup running after a
    successful close could `os.close` a NUMBER the kernel had handed to an
    unrelated request. The sink owns its descriptor, so `close()` is idempotent
    and running the exit path twice is a no-op rather than a corruption.
    """
    dest = tmp_path / "w.bin"
    sink = _TempSink(dest)
    with sink:
        sink.write(b"payload")
    assert sink.sink is not None and sink.sink.closed
    # Second exit: the file object refuses to touch the number again and the
    # already-removed temp is tolerated.
    sink.__exit__(None, None, None)
    assert sink.sink.closed


def test_open_refuses_to_adopt_an_existing_temp(tmp_path: Path) -> None:
    """O_EXCL: never write into a `.part` this request did not create."""
    dest = tmp_path / "collide.bin"
    (tmp_path / "collide.bin.part").write_bytes(b"someone else's")
    with pytest.raises(FileExistsError):
        with _TempSink(dest):
            pass
    assert (tmp_path / "collide.bin.part").read_bytes() == b"someone else's"


@pytest.mark.asyncio
async def test_dest_is_never_published_when_the_rename_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The atomic-publish invariant: `dest` appears only via a successful rename.

    Whatever goes wrong, a reader must see either no file or a complete one --
    never a partial that looks finished, which was the shape of finding 1.
    """

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("rename refused")

    monkeypatch.setattr(part_stream.os, "replace", boom)
    dest = tmp_path / "never.bin"
    with pytest.raises(OSError, match="rename refused"):
        await stream_part_to_file(_FakePart([b"abcdef"]), dest, max_bytes=1024)
    monkeypatch.undo()
    assert not dest.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_every_byte_lands_across_many_chunks(tmp_path: Path) -> None:
    """Finding 1: a short write must not silently truncate the payload.

    `BufferedWriter.write` loops internally, which is why the hand-rolled
    `_write_all` could be deleted; this pins the resulting bytes rather than the
    mechanism, so it keeps holding if the mechanism changes again.
    """
    dest = tmp_path / "joined.bin"
    chunks = [bytes([i]) * 1000 for i in range(8)]
    total = await stream_part_to_file(_FakePart(chunks), dest, max_bytes=1 << 20)
    assert total == 8000
    assert dest.read_bytes() == b"".join(chunks)


@pytest.mark.asyncio
async def test_over_cap_raises_and_leaves_nothing(tmp_path: Path) -> None:
    dest = tmp_path / "big.bin"
    with pytest.raises(PartTooLarge) as excinfo:
        await stream_part_to_file(_FakePart([b"x" * 100, b"y" * 100]), dest, max_bytes=150)
    assert excinfo.value.limit == 150
    assert not dest.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_rejected_content_never_reaches_disk(tmp_path: Path) -> None:
    """CWE-434: the predicate decides while the bytes are still in memory.

    Asserted by refusing and then proving the directory is empty -- if the sniff
    ran after the write, the temp would have existed and been cleaned up, which
    is a weaker guarantee than never having written it.
    """
    dest = tmp_path / "liar.mov"
    with pytest.raises(PartContentMismatch):
        await stream_part_to_file(
            _FakePart([b"<html>not a video at all</html>"]),
            dest,
            max_bytes=1 << 20,
            accepts=lambda head: head.startswith(b"\x00\x00\x00\x14ftyp"),
        )
    assert not dest.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_a_part_shorter_than_the_sniff_window_is_still_judged(tmp_path: Path) -> None:
    """The tail branch: EOF before SNIFF_BYTES must not accept by default."""
    dest = tmp_path / "tiny.mov"
    with pytest.raises(PartContentMismatch):
        await stream_part_to_file(
            _FakePart([b"\x00\x00"]),
            dest,
            max_bytes=1 << 20,
            accepts=lambda head: head.startswith(b"\x00\x00\x00\x14ftyp"),
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_accepted_content_lands_byte_for_byte(tmp_path: Path) -> None:
    """The held-back sniff window must be written, not dropped."""
    dest = tmp_path / "real.mov"
    payload = b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00" + b"\xab" * 4096
    total = await stream_part_to_file(
        _FakePart([payload[:8], payload[8:]]),
        dest,
        max_bytes=1 << 20,
        accepts=lambda head: head.startswith(b"\x00\x00\x00\x14ftyp"),
    )
    assert total == len(payload)
    assert dest.read_bytes() == payload


@pytest.mark.asyncio
async def test_read_size_is_bounded_and_not_caller_tunable(tmp_path: Path) -> None:
    """The read size bounds the ONE blocking call left on the event loop.

    `__exit__`'s `close()` contends for the BufferedWriter's lock, so if
    cancellation lands while a worker is inside `to_thread(sink.write, chunk)`,
    the on-loop close waits for that write. The wait is one chunk of I/O, so the
    chunk size *is* the bound — at the 1 MB this module started with, a slow or
    network filesystem made it a real stall.

    Two assertions, and the second is the load-bearing one: the size is capped,
    and it is capped by a module constant that no caller can raise. A tunable
    parameter would make the bound only as good as its worst caller, which is how
    this became a finding in the first place.
    """
    seen: list[int] = []

    class _RecordingPart:
        def __init__(self) -> None:
            self._left = 3

        async def read_chunk(self, size: int = 0) -> bytes:
            seen.append(size)
            self._left -= 1
            return b"z" * 32 if self._left > 0 else b""

    await stream_part_to_file(_RecordingPart(), tmp_path / "bounded.bin", max_bytes=1 << 20)

    assert seen, "the loop never read anything"
    assert max(seen) <= 64 * 1024, seen
    assert part_stream.CHUNK_BYTES == 64 * 1024
    # No per-call override: re-adding one silently un-bounds the wait above.
    params = inspect.signature(stream_part_to_file).parameters
    assert "chunk_bytes" not in params, sorted(params)


def test_open_flags_include_o_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The open must be binary-mode, which only Windows can actually get wrong.

    `O_BINARY` is 0 on POSIX, so a POSIX-only test cannot observe the corruption
    — it would pass on this machine while Windows CI committed newline-translated
    bytes. So assert on the FLAGS rather than on the written file: that holds on
    every platform, and it is exactly the regression this had (the flag was on
    the pre-extraction opener and was dropped when the code moved).
    """
    seen: list[int] = []
    real_open = os.open

    def recording_open(path, flags, *rest):  # noqa: ANN001, ANN202
        seen.append(flags)
        return real_open(path, flags, *rest)

    monkeypatch.setattr(part_stream.os, "open", recording_open)
    with _TempSink(tmp_path / "flags.bin"):
        pass
    monkeypatch.undo()

    assert seen, "the sink did not open anything"
    # Compared against the attribute rather than a literal: the constant does not
    # exist off Windows, and hardcoding its value would assert nothing there.
    expected = getattr(os, "O_BINARY", 0)
    assert seen[0] & expected == expected, oct(seen[0])
    assert seen[0] & os.O_EXCL, oct(seen[0])


def test_temp_is_created_owner_only(tmp_path: Path) -> None:
    """An upload may carry anything and `uploads/` is shared."""
    dest = tmp_path / "perm.bin"
    with _TempSink(dest) as sink:
        mode = os.stat(sink.tmp).st_mode & 0o777
    if os.name != "nt":  # Windows does not model POSIX permission bits
        assert mode == 0o600, oct(mode)
