"""The transcript restore is bounded by size as well as by shape.

Every other guard on this path answers "is this entry the right KIND of thing". This one
answers "how much of it is this process willing to hold": the bytes come from the backup
bucket and live in memory for the length of a turn, so without a ceiling one stored
object decides how much memory the task uses.

Two bounds, because a header is a claim rather than a bound. `ContentLength` is checked
before a byte is read, and the streaming read enforces the same ceiling on what actually
arrives.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from container.front import transcript as t


class _Body:
    """A minimal stand-in for a botocore streaming body."""

    def __init__(self, data: bytes) -> None:
        self._buf = io.BytesIO(data)
        self.reads = 0

    def read(self, size: int | None = None) -> bytes:
        self.reads += 1
        return self._buf.read(size)


def test_a_transcript_under_the_ceiling_reads_whole() -> None:
    body = _Body(b"a" * 4096)
    assert t._read_bounded(body, "k", limit=8192) == b"a" * 4096


def test_a_transcript_over_the_ceiling_is_refused() -> None:
    with pytest.raises(t.TranscriptTooLarge, match="exceeds"):
        t._read_bounded(_Body(b"a" * 9000), "some/key.jsonl", limit=8192)


def test_the_refusal_names_the_key() -> None:
    """An operator has to know WHICH object, since the fix is on the bucket side."""
    with pytest.raises(t.TranscriptTooLarge) as exc:
        t._read_bounded(_Body(b"a" * 9000), "crews/frontdesk/dashboard_slot-1.jsonl", limit=8192)
    assert "crews/frontdesk/dashboard_slot-1.jsonl" in str(exc.value)


def test_a_refusal_is_a_transcript_failure_so_the_turn_fails() -> None:
    """``TranscriptTooLarge`` must be a ``TranscriptUnavailable``.

    The caller's contract is that ``TranscriptUnavailable`` fails the turn and anything
    else lets it proceed. A size refusal that was not one of those would let the backend
    serve a turn with the conversation missing and then overwrite the real history.
    """
    assert issubclass(t.TranscriptTooLarge, t.TranscriptUnavailable)


def test_the_read_is_chunked_rather_than_one_call() -> None:
    """The ceiling has to be enforced while reading, not after.

    A single ``body.read()`` materialises whatever the object is, so the limit would be
    checked once the memory was already committed. Asserting on the number of reads is
    what pins that: one read of everything would be a single call.
    """
    body = _Body(b"a" * (3 * t._READ_CHUNK_BYTES))
    t._read_bounded(body, "k")
    assert body.reads >= 3


def test_an_object_declaring_itself_too_large_is_refused_before_reading(tmp_path: Path) -> None:
    """The header check exists to avoid starting a read that cannot end well.

    It is not trusted -- the streaming ceiling still applies -- but an object that
    announces itself as oversized is refused without pulling any of it.
    """

    class _Client:
        def __init__(self) -> None:
            self.body = _Body(b"a" * 16)

        def get_object(self, Bucket: str, Key: str):  # noqa: N803 - botocore's own casing
            return {"ContentLength": t.MAX_TRANSCRIPT_BYTES + 1, "Body": self.body}

    reader = t.S3TranscriptReader("bkt")
    client = _Client()
    reader._client = client

    with pytest.raises(t.TranscriptTooLarge, match="declares"):
        reader.get("some/key.jsonl")
    assert client.body.reads == 0, "the body was read despite the declared size"


def test_a_lying_header_does_not_get_past_the_streaming_ceiling() -> None:
    """A header is a claim by the source, so the read enforces the bound itself."""

    class _Client:
        def get_object(self, Bucket: str, Key: str):  # noqa: N803 - botocore's own casing
            return {"ContentLength": 10, "Body": _Body(b"a" * (t.MAX_TRANSCRIPT_BYTES + 1))}

    reader = t.S3TranscriptReader("bkt")
    reader._client = _Client()

    with pytest.raises(t.TranscriptTooLarge, match="exceeds"):
        reader.get("some/key.jsonl")
