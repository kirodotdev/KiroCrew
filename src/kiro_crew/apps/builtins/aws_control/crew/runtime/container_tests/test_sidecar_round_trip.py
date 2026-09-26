"""One customer's conversation, carried across a task replacement.

The two halves of durability run in different processes and meet only at the object
key, so each half passing its own tests proves nothing about the pair: a writer and a
reader can agree with each other while both disagree with the contract. These tests
drive the pair as one path.

The replacement is simulated the way the platform performs one. The first task's data
home is abandoned whole, and a SECOND ``Settings`` is built over an empty directory
with the same crew name and prefix. Nothing is copied between the two homes. Everything
the second task has, it got from the bucket.

The bucket is a dict behind two separate adapters, and that shape is the subject. The
sidecar puts objects through the object-store interface; the front gets one through the
reader interface. Neither can see the other's keys, so a key either matches or the fetch
misses, exactly as it would against S3.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from container.common import keys
from container.front import transcript as front
from container.sidecar import backup as backup_mod
from container.sidecar import restore as restore_mod
from container.sidecar.store import ObjectAbsent, ObjectTooLarge

from ._settings_helper import make_settings

SLOT_ID = "cust-8831"
STEM = "dashboard_cust-8831"


class _Bucket:
    """One in-memory bucket, plus the order its objects were written in."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []


class _StoreSide:
    """The sidecar's view of the bucket: put from a descriptor, get by key."""

    def __init__(self, bucket: _Bucket) -> None:
        self.bucket = bucket

    def put(self, key: str, body, size: int) -> None:
        # ``read(size)`` rather than ``read()``: the caller's size is the length the
        # real store declares as ``ContentLength``, so reading to end-of-file here
        # would accept an upload that sends more bytes than it promised.
        self.bucket.objects[key] = body.read(size)
        self.bucket.puts.append(key)

    def get(self, key: str, *, limit: int) -> bytes:
        try:
            raw = self.bucket.objects[key]
        except KeyError:
            raise ObjectAbsent(key) from None
        if len(raw) > limit:
            raise ObjectTooLarge(key)
        return raw


class _ReaderSide:
    """The front's view of the same bucket: one blocking get, absence as an exception."""

    def __init__(self, bucket: _Bucket) -> None:
        self.bucket = bucket
        self.requested: list[str] = []

    def get(self, key: str) -> bytes:
        self.requested.append(key)
        try:
            return self.bucket.objects[key]
        except KeyError:
            raise front.TranscriptAbsent(key) from None


@pytest.fixture
def bucket() -> _Bucket:
    return _Bucket()


def _first_task(tmp_path: Path):
    return make_settings(tmp_path / "task-a", crew="crew-9", prefix="crews")


def _replacement_task(tmp_path: Path):
    """A second task with the same identity and an empty data home."""
    return make_settings(tmp_path / "task-b", crew="crew-9", prefix="crews")


def _write_transcript(settings, stem: str, payload: bytes) -> Path:
    path = settings.sessions_dir / f"{stem}{keys.TRANSCRIPT_SUFFIX}"
    path.write_bytes(payload)
    return path


def _write_authority(settings, *, slots: str = '{"keys": ["cust-8831"]}') -> None:
    (settings.config_dir / "session_map.json").write_bytes(b'{"cust-8831": "sess-1"}')
    (settings.config_dir / "open_slots.json").write_bytes(slots.encode("utf-8"))


def test_transcript_bytes_are_identical_after_a_task_replacement(tmp_path, bucket):
    payload = b'{"role": "user", "content": "hello"}\n{"role": "assistant"}\n'
    first = _first_task(tmp_path)
    _write_transcript(first, STEM, payload)
    _write_authority(first)

    backup_mod.run_cycle(first, _StoreSide(bucket), state={})

    second = _replacement_task(tmp_path)
    landed = second.sessions_dir / f"{STEM}{keys.TRANSCRIPT_SUFFIX}"
    assert not landed.exists()

    outcome = asyncio.run(
        front.ensure_local_transcript(second, SLOT_ID, reader=_ReaderSide(bucket))
    )

    assert outcome.action == "fetched"
    assert landed.read_bytes() == payload


def test_the_writer_and_the_reader_address_one_key(tmp_path, bucket):
    first = _first_task(tmp_path)
    _write_transcript(first, STEM, b"one line\n")
    _write_authority(first)
    backup_mod.run_cycle(first, _StoreSide(bucket), state={})

    second = _replacement_task(tmp_path)
    reader = _ReaderSide(bucket)
    asyncio.run(front.ensure_local_transcript(second, SLOT_ID, reader=reader))

    # The reader asked for exactly one key, and the writer had put that key. A doubled
    # crew segment or a missing namespace shows up here as a requested key nothing put.
    assert reader.requested == [keys.transcript_key(second, STEM)]
    assert reader.requested[0] in bucket.objects


def test_both_authority_files_are_identical_after_a_task_replacement(tmp_path, bucket):
    first = _first_task(tmp_path)
    _write_transcript(first, STEM, b"turn\n")
    _write_authority(first)
    backup_mod.run_cycle(first, _StoreSide(bucket), state={})
    before = {name: (first.config_dir / name).read_bytes() for name in keys.AUTHORITY_NAMES}

    second = _replacement_task(tmp_path)
    result = restore_mod.restore_authority(second, _StoreSide(bucket))

    assert sorted(result.restored) == sorted(keys.AUTHORITY_NAMES)
    assert result.absent == []
    for name, raw in before.items():
        assert (second.config_dir / name).read_bytes() == raw


def test_an_archived_segment_keeps_its_own_key_and_its_bytes(tmp_path, bucket):
    first = _first_task(tmp_path)
    _write_authority(first)
    nested = first.archive_dir / STEM / "0001.jsonl"
    nested.parent.mkdir(parents=True, exist_ok=True)
    payload = b'{"segment": 1}\n'
    nested.write_bytes(payload)

    backup_mod.run_cycle(first, _StoreSide(bucket), state={})

    key = keys.data_key(first, nested)
    assert key in bucket.objects
    # The nested path is part of the key, so the segment and a live transcript of the
    # same stem cannot collide.
    assert key.endswith(f"archive/{STEM}/0001.jsonl")
    assert bucket.objects[key] == payload


def test_the_transcripts_are_uploaded_before_the_authority_files_that_index_them(tmp_path, bucket):
    """The index goes last, so it never names an object the bucket does not hold.

    The front fetches a named transcript lazily and reads an absent one as a
    conversation with no history, so an index ahead of its bytes serves a live
    conversation empty with nothing raised anywhere.
    """
    first = _first_task(tmp_path)
    _write_transcript(first, STEM, b"turn\n")
    _write_authority(first)

    backup_mod.run_cycle(first, _StoreSide(bucket), state={})

    authority = [
        keys.authority_slot_key(first, keys.AUTHORITY_SLOTS[0], name)
        for name in keys.AUTHORITY_NAMES
    ]
    transcript = keys.transcript_key(first, STEM)
    assert bucket.puts[-1] == keys.authority_pointer_key(first)
    assert bucket.puts[-1 - len(authority) : -1] == authority
    assert bucket.puts.index(transcript) < min(bucket.puts.index(k) for k in authority)


def test_a_second_cycle_resends_only_what_changed(tmp_path, bucket):
    first = _first_task(tmp_path)
    path = _write_transcript(first, STEM, b"first turn\n")
    _write_authority(first)
    state: dict = {}
    backup_mod.run_cycle(first, _StoreSide(bucket), state=state)
    puts_after_first = len(bucket.puts)

    second_result = backup_mod.run_cycle(first, _StoreSide(bucket), state=state)
    assert second_result.uploaded == []
    assert len(bucket.puts) == puts_after_first

    grown = b"first turn\nsecond turn\n"
    path.write_bytes(grown)
    third_result = backup_mod.run_cycle(first, _StoreSide(bucket), state=state)

    assert third_result.uploaded == [keys.transcript_key(first, STEM)]
    assert bucket.objects[keys.transcript_key(first, STEM)] == grown


def test_a_transcript_that_grows_after_it_is_opened_uploads_the_length_it_had(tmp_path, bucket):
    """The upload is a version that was really on disk, not a race with the writer."""
    first = _first_task(tmp_path)
    path = _write_transcript(first, STEM, b"aaaa")
    snapshot = backup_mod.open_snapshot(path, root=first.data_home)
    assert snapshot is not None
    try:
        with path.open("ab") as fh:
            fh.write(b"bbbb")
        _StoreSide(bucket).put("k", snapshot.fh, snapshot.size)
    finally:
        snapshot.close()

    assert bucket.objects["k"] == b"aaaa"


def test_a_replacement_with_an_empty_bucket_starts_the_conversation_fresh(tmp_path, bucket):
    """A crew's first task finds nothing, and that is a first boot rather than a fault."""
    second = _replacement_task(tmp_path)

    result = restore_mod.restore_authority(second, _StoreSide(bucket))
    outcome = asyncio.run(
        front.ensure_local_transcript(second, SLOT_ID, reader=_ReaderSide(bucket))
    )

    assert result.restored == []
    assert sorted(result.absent) == sorted(keys.AUTHORITY_NAMES)
    assert outcome.action == "absent"


def test_a_local_authority_file_wins_over_the_bucket_copy(tmp_path, bucket):
    """A data home that outlived its task leads the bucket, so its copy is kept."""
    first = _first_task(tmp_path)
    _write_authority(first, slots='{"keys": ["cust-8831"]}')
    backup_mod.run_cycle(first, _StoreSide(bucket), state={})

    second = _replacement_task(tmp_path)
    local = b'{"keys": ["cust-8831", "cust-9002"]}'
    (second.config_dir / "open_slots.json").write_bytes(local)

    result = restore_mod.restore_authority(second, _StoreSide(bucket))

    assert result.kept_local == ["open_slots.json"]
    assert result.restored == ["session_map.json"]
    assert (second.config_dir / "open_slots.json").read_bytes() == local
