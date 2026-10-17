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
from collections.abc import Callable
from pathlib import Path

import pytest
from container.common import keys
from container.front import transcript as front
from container.sidecar import backup as backup_mod
from container.sidecar import generation
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

    def put(
        self,
        key: str,
        body,
        size: int,
        *,
        budget: float | None = None,
        cancel: Callable[[], bool] | None = None,
        if_match: str | None = None,
        if_none_match: str | None = None,
    ) -> None:
        # ``read(size)`` rather than ``read()``: the caller's size is the length the
        # real store declares as ``ContentLength``, so reading to end-of-file here
        # would accept an upload that sends more bytes than it promised.
        self.bucket.objects[key] = body.read(size)
        self.bucket.puts.append(key)

    def get(
        self,
        key: str,
        *,
        limit: int,
        deadline: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> bytes:
        try:
            raw = self.bucket.objects[key]
        except KeyError:
            raise ObjectAbsent(key) from None
        if len(raw) > limit:
            raise ObjectTooLarge(key)
        return raw

    def get_with_etag(
        self,
        key: str,
        *,
        limit: int,
        deadline: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> tuple[bytes, str | None]:
        raw = self.get(key, limit=limit)
        return raw, ('"etag"' if key in self.bucket.objects else None)


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


def test_the_writer_and_the_reader_resolve_to_one_blob(tmp_path, bucket):
    import hashlib

    first = _first_task(tmp_path)
    payload = b"one line\n"
    _write_transcript(first, STEM, payload)
    _write_authority(first)
    backup_mod.run_cycle(first, _StoreSide(bucket), state={})

    second = _replacement_task(tmp_path)
    reader = _ReaderSide(bucket)
    asyncio.run(front.ensure_local_transcript(second, SLOT_ID, reader=reader))

    # The reader resolves the transcript through the committed generation: it reads the
    # pointer, then that generation's transcript index, then the content-addressed blob.
    # Three single-key GETs, no listing -- and the blob key it lands on is exactly the one
    # the writer put the bytes under, which is the writer/reader agreement this guards.
    blob = keys.blob_key(second, hashlib.sha256(payload).hexdigest())
    assert reader.requested == [
        keys.authority_pointer_key(second),
        keys.transcript_index_key(second, _committed_generation(bucket, first)),
        blob,
    ]
    assert blob in bucket.objects


def _committed_generation(bucket: _Bucket, settings) -> str:
    """The generation id the committed pointer names, read straight from the bucket bytes."""
    import json

    body = bucket.objects[keys.authority_pointer_key(settings)]
    return json.loads(body.decode())["generation"]


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


def test_an_archived_segment_keeps_its_path_so_the_control_plane_can_find_it(tmp_path, bucket):
    first = _first_task(tmp_path)
    _write_authority(first)
    nested = first.archive_dir / STEM / "0001.jsonl"
    nested.parent.mkdir(parents=True, exist_ok=True)
    payload = b'{"segment": 1}\n'
    nested.write_bytes(payload)

    backup_mod.run_cycle(first, _StoreSide(bucket), state={})

    # An archive segment keeps its full RELATIVE PATH as the key PREFIX -- so the owner's
    # control plane finds a conversation's rows by prefix-listing ``sessions/archive/<conv>/``
    # -- and APPENDS the segment's content digest so two overlapping writers of distinct
    # bytes never collide on one mutable key.
    import hashlib

    digest = hashlib.sha256(payload).hexdigest()
    key = keys.archive_segment_key(first, nested, digest)
    assert key in bucket.objects
    # The path prefix is intact (prefix-discoverable), and the digest is the final component.
    assert f"archive/{STEM}/0001.jsonl." in key
    assert key.endswith(digest)
    assert bucket.objects[key] == payload


def test_overlapping_writers_of_a_same_path_segment_do_not_overwrite_each_other(tmp_path, bucket):
    """Two tasks rotating the same-named segment with different bytes keep BOTH, losing neither.

    An archive segment's key is its PATH plus its content DIGEST. Two overlapping tasks
    (predecessor draining + replacement) can rotate the same stem in the same second on
    isolated filesystems, deriving the IDENTICAL path -- their per-filesystem collision
    counters cannot see each other. A bare path key PUT unconditionally would race and
    silently drop one distinct segment. The digest suffix sends distinct bytes to distinct
    keys, so both land and the create-only fence never meets a 412 for differing content.

    The store enforces ``If-None-Match: *`` (raises on an existing key), so if the archive
    branch ever reverted to a bare path key + unconditional PUT, the second writer's PUT
    would overwrite (or, create-only, 412-adopt) the first and this test would fail.
    """
    from container.sidecar.store import PreconditionFailed

    class _PreconditionStore(_StoreSide):
        def put(self, key, body, size, *, if_none_match=None, **kw):
            if if_none_match == "*" and key in self.bucket.objects:
                raise PreconditionFailed(key)
            super().put(key, body, size, if_none_match=if_none_match, **kw)

    import hashlib

    first = _first_task(tmp_path)
    _write_authority(first)
    nested = first.archive_dir / STEM / "0001.jsonl"
    nested.parent.mkdir(parents=True, exist_ok=True)

    # Task A rotates a segment at this path with ITS bytes.
    bytes_a = b'{"segment": "from task A"}\n'
    nested.write_bytes(bytes_a)
    backup_mod.run_cycle(first, _PreconditionStore(bucket), state={})

    # Task B rotates a segment at the SAME path (same stem, same second) with DIFFERENT bytes
    # -- the overlap the fix is built around. Fresh state models a distinct task.
    bytes_b = b'{"segment": "from task B, longer and different"}\n'
    nested.write_bytes(bytes_b)
    backup_mod.run_cycle(first, _PreconditionStore(bucket), state={})

    key_a = keys.archive_segment_key(first, nested, hashlib.sha256(bytes_a).hexdigest())
    key_b = keys.archive_segment_key(first, nested, hashlib.sha256(bytes_b).hexdigest())
    assert key_a != key_b
    # BOTH segments are in the bucket -- neither overwrote the other.
    assert bucket.objects[key_a] == bytes_a
    assert bucket.objects[key_b] == bytes_b


def test_an_identical_archive_segment_from_two_cycles_converges_on_one_key(tmp_path, bucket):
    """Identical bytes take the same content-suffixed key, so a re-PUT is a harmless no-op."""
    from container.sidecar.store import PreconditionFailed

    class _PreconditionStore(_StoreSide):
        def put(self, key, body, size, *, if_none_match=None, **kw):
            if if_none_match == "*" and key in self.bucket.objects:
                raise PreconditionFailed(key)
            super().put(key, body, size, if_none_match=if_none_match, **kw)

    import hashlib

    first = _first_task(tmp_path)
    _write_authority(first)
    nested = first.archive_dir / STEM / "0001.jsonl"
    nested.parent.mkdir(parents=True, exist_ok=True)
    payload = b'{"segment": 1}\n'
    nested.write_bytes(payload)
    key = keys.archive_segment_key(first, nested, hashlib.sha256(payload).hexdigest())

    # Two cycles with the identical segment: the second meets the create-only precondition
    # on an existing key, recorded durable rather than raising -- one object, no duplicate.
    backup_mod.run_cycle(first, _PreconditionStore(bucket), state={})
    backup_mod.run_cycle(first, _PreconditionStore(bucket), state={})
    assert bucket.objects[key] == payload
    assert bucket.puts.count(key) == 1


def test_the_transcripts_are_uploaded_before_the_authority_files_that_index_them(tmp_path, bucket):
    """The index goes last, so it never names an object the bucket does not hold.

    The front fetches a named transcript lazily and reads an absent one as a
    conversation with no history, so an index ahead of its bytes serves a live
    conversation empty with nothing raised anywhere.
    """
    import hashlib

    first = _first_task(tmp_path)
    _write_transcript(first, STEM, b"turn\n")
    _write_authority(first)

    backup_mod.run_cycle(first, _StoreSide(bucket), state={})

    prefix = keys.generations_prefix(first)
    pair = [
        k
        for k in bucket.puts
        if k.startswith(prefix) and k.rsplit("/", 1)[-1] in keys.AUTHORITY_NAMES
    ]
    assert len(pair) == len(keys.AUTHORITY_NAMES)
    transcript = keys.blob_key(first, hashlib.sha256(b"turn\n").hexdigest())
    index = keys.transcript_index_key(first, _committed_generation(bucket, first))
    pointer = keys.authority_pointer_key(first)
    # The pointer is the LAST object; the transcript index and the pair precede it; and the
    # transcript blob precedes every one of them -- so a committed index never names a blob
    # that is not already in the bucket.
    assert bucket.puts[-1] == pointer
    assert bucket.puts.index(index) < bucket.puts.index(pointer)
    assert all(bucket.puts.index(k) < bucket.puts.index(index) for k in pair)
    assert bucket.puts.index(transcript) < min(bucket.puts.index(k) for k in pair)


def test_a_second_cycle_resends_only_what_changed(tmp_path, bucket):
    import hashlib

    first = _first_task(tmp_path)
    path = _write_transcript(first, STEM, b"first turn\n")
    _write_authority(first)
    state: dict = {}
    backup_mod.run_cycle(first, _StoreSide(bucket), state=state)
    puts_after_first = len(bucket.puts)

    # Nothing changed: the transcript's blob, the pair, and the index are all unchanged, so
    # the cycle re-PUTs nothing and the pointer is not rewritten.
    second_result = backup_mod.run_cycle(first, _StoreSide(bucket), state=state)
    assert second_result.uploaded == []
    assert len(bucket.puts) == puts_after_first

    # The transcript grows: its content changes, so it takes a NEW content-addressed key and
    # the committed index must be republished to name that new blob.
    grown = b"first turn\nsecond turn\n"
    path.write_bytes(grown)
    third_result = backup_mod.run_cycle(first, _StoreSide(bucket), state=state)

    grown_blob = keys.blob_key(first, hashlib.sha256(grown).hexdigest())
    assert grown_blob in third_result.uploaded
    assert bucket.objects[grown_blob] == grown
    # The index now names the grown blob, so the committed generation resolves to it.
    index = keys.transcript_index_key(first, _committed_generation(bucket, first))
    import json

    assert json.loads(bucket.objects[index].decode())[STEM] == hashlib.sha256(grown).hexdigest()


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


def test_an_unparseable_transcript_index_refuses_the_turn(tmp_path, bucket):
    """A committed index that will not parse must not fall back to a legacy key.

    The index exists, so a reader cannot know the right blob -- and the legacy key may be an
    object a newer writer never wrote, so serving it would hand the customer an empty or
    stale history. The front refuses the turn (TranscriptUnavailable) rather than guess.
    """
    import json

    second = _replacement_task(tmp_path)
    gen_id = keys.new_generation_id()
    bucket.objects[keys.authority_pointer_key(second)] = json.dumps(
        {"generation": gen_id, "authority": sorted(keys.AUTHORITY_NAMES)}
    ).encode()
    bucket.objects[keys.transcript_index_key(second, gen_id)] = b"{ this is not json"
    reader = _ReaderSide(bucket)

    with pytest.raises(front.TranscriptUnavailable):
        asyncio.run(front.ensure_local_transcript(second, SLOT_ID, reader=reader))


def test_a_replacement_cycle_carries_forward_unfetched_transcript_mappings(tmp_path, bucket):
    """A replacement must not drop a conversation's mapping just because it has not served it.

    The front fetches a transcript lazily, on the turn that continues it, so a replacement's
    first cycle enumerates on local disk only the conversations it has served -- a subset of
    those the committed authority pair names. If that cycle published an index built from the
    local set alone, every unserved conversation would vanish from the committed generation,
    and the front reading the new index would miss it and serve an empty history: the silent
    loss this subsystem exists to prevent. So the cycle carries forward the committed index's
    mapping for every stem the captured pair still names, overlaying its own local digests.
    This proves a second task
    that served ONE of two conversations still commits an index naming BOTH.
    """
    import hashlib
    import json

    # First task serves two conversations and backs them up.
    first = _first_task(tmp_path)
    _write_transcript(first, "dashboard_cust-1", b"one\n")
    _write_transcript(first, "dashboard_cust-2", b"two\n")
    (first.config_dir / "session_map.json").write_bytes(b'{"cust-1": "s1", "cust-2": "s2"}')
    (first.config_dir / "open_slots.json").write_bytes(b'{"keys": ["cust-1", "cust-2"]}')
    backup_mod.run_cycle(first, _StoreSide(bucket), state={})

    # The replacement restores the authority pair, then its backend serves ONLY cust-1 (the
    # front fetched that one transcript); cust-2's bytes are still only in the bucket.
    second = _replacement_task(tmp_path)
    restore_mod.restore_authority(second, _StoreSide(bucket))
    _write_transcript(second, "dashboard_cust-1", b"one\n")

    backup_mod.run_cycle(second, _StoreSide(bucket), state={})

    gen = _committed_generation(bucket, second)
    index = json.loads(bucket.objects[keys.transcript_index_key(second, gen)].decode())
    # BOTH conversations are named: cust-1 from this task's local disk, cust-2 carried
    # forward from the committed index because the restored pair still names it.
    assert index["dashboard_cust-1"] == hashlib.sha256(b"one\n").hexdigest()
    assert index["dashboard_cust-2"] == hashlib.sha256(b"two\n").hexdigest()


def test_a_deleted_conversation_is_not_carried_forward(tmp_path, bucket):
    """The complement: a stem the pair does not name is dropped, not carried.

    Carrying forward keys on the CAPTURED pair, so a conversation its owner deleted (gone
    from the slot table) is forgotten rather than pinned forever. This proves a second cycle
    whose pair dropped cust-2 commits an index that omits it.
    """
    import hashlib
    import json

    first = _first_task(tmp_path)
    _write_transcript(first, "dashboard_cust-1", b"one\n")
    _write_transcript(first, "dashboard_cust-2", b"two\n")
    (first.config_dir / "session_map.json").write_bytes(b'{"cust-1": "s1", "cust-2": "s2"}')
    (first.config_dir / "open_slots.json").write_bytes(b'{"keys": ["cust-1", "cust-2"]}')
    state: dict = {}
    backup_mod.run_cycle(first, _StoreSide(bucket), state=state)

    # cust-2 is deleted: gone from the slot table AND its transcript removed from disk, so
    # the second cycle neither enumerates it locally nor carries it forward (the pair no
    # longer names it). cust-1's transcript is unchanged.
    (first.config_dir / "session_map.json").write_bytes(b'{"cust-1": "s1"}')
    (first.config_dir / "open_slots.json").write_bytes(b'{"keys": ["cust-1"]}')
    (first.sessions_dir / "dashboard_cust-2.jsonl").unlink()
    backup_mod.run_cycle(first, _StoreSide(bucket), state=state)

    gen = _committed_generation(bucket, first)
    index = json.loads(bucket.objects[keys.transcript_index_key(first, gen)].decode())
    assert index == {"dashboard_cust-1": hashlib.sha256(b"one\n").hexdigest()}


def test_a_deleted_conversation_is_pruned_from_the_lifetime_state_maps(tmp_path, bucket):
    """A departed stem's live-blob mapping AND its blob key leave *state* on the next commit.

    ``_evict_prior_live_blob`` bounds a CHANGING transcript to one blob per stem, but it
    never fires for a stem that simply stops appearing -- a session created, backed up, then
    deleted. Without the departed-stem prune its ``live_blobs`` mapping and its
    content-addressed blob key would linger in the lifetime map for the process's whole life,
    one dead entry per create/delete cycle, unbounded. ``_prune_departed_live_blobs`` retires
    both after a commit, against the MERGED index (so a replacement task's bucket-only stems,
    carried forward, are never mistaken for departed). This creates two conversations, deletes
    one, and asserts the deleted stem's mapping and blob key are gone while the surviving one's
    are kept.

    It fails if the prune is removed: the deleted stem's ``live_blobs`` entry and its blob key
    stay in *state* across the deletion cycle.
    """
    first = _first_task(tmp_path)
    _write_transcript(first, "dashboard_cust-1", b"one\n")
    _write_transcript(first, "dashboard_cust-2", b"two\n")
    (first.config_dir / "session_map.json").write_bytes(b'{"cust-1": "s1", "cust-2": "s2"}')
    (first.config_dir / "open_slots.json").write_bytes(b'{"keys": ["cust-1", "cust-2"]}')
    # DurableState, not a plain dict: the prune and its live_blobs companion live here, and a
    # plain {} would silently no-op the very behaviour under test.
    state = backup_mod.DurableState()
    backup_mod.run_cycle(first, _StoreSide(bucket), state=state)

    gone_key = state.live_blobs["dashboard_cust-2"]
    assert (
        gone_key in state
    ), "precondition: the deleted stem's blob key is recorded before deletion"

    # cust-2 deleted: gone from the slot table and its transcript removed from disk.
    (first.config_dir / "session_map.json").write_bytes(b'{"cust-1": "s1"}')
    (first.config_dir / "open_slots.json").write_bytes(b'{"keys": ["cust-1"]}')
    (first.sessions_dir / "dashboard_cust-2.jsonl").unlink()
    backup_mod.run_cycle(first, _StoreSide(bucket), state=state)

    # The departed stem is pruned from BOTH maps; the surviving stem is untouched.
    assert "dashboard_cust-2" not in state.live_blobs, "the departed stem's mapping was not pruned"
    assert gone_key not in state, "the departed stem's blob key was not dropped from state"
    surviving_key = state.live_blobs["dashboard_cust-1"]
    assert surviving_key == _blob_key(first, b"one\n")
    assert surviving_key in state, "the surviving stem's blob key must stay"


def _blob_key(settings, payload: bytes) -> str:
    """The content-addressed key the writer stores *payload* under: ``data/blob/<sha256>``."""
    import hashlib as _hashlib

    return keys.blob_key(settings, _hashlib.sha256(payload).hexdigest())


def test_a_committed_index_that_omits_a_stem_does_not_resurrect_the_legacy_object(tmp_path, bucket):
    """A recycled slot id must not inherit a deleted conversation's legacy bytes.

    Once a committed pointer exists, the committed transcript index is the sole authority on
    which blob a stem resolves to. A legacy ``data/sessions/<stem>.jsonl`` object -- the
    pre-upgrade layout the content-addressed writer never writes -- can linger in the bucket
    for a stem the index does not name, and a reused slot id maps to that same stem. The
    front must treat the unlisted stem as ABSENT (a fresh conversation) rather than fetch the
    legacy object, or it would serve one customer another's obsolete history and the next
    cycle would promote it into the committed index. This plants exactly that legacy object
    under a committed generation whose index does not name the stem, and asserts the fetch is
    absent and nothing landed on disk.

    It fails if the resolver falls back to the legacy key once a committed index exists.
    """
    import json

    second = _replacement_task(tmp_path)
    # A committed generation whose index names a DIFFERENT conversation, not this stem.
    gen_id = keys.new_generation_id()
    bucket.objects[keys.authority_pointer_key(second)] = json.dumps(
        {"generation": gen_id, "authority": sorted(keys.AUTHORITY_NAMES)}
    ).encode()
    bucket.objects[keys.transcript_index_key(second, gen_id)] = json.dumps(
        {"dashboard_someone-else": "a" * 64}
    ).encode()
    # A lingering object at the OLD per-stem location -- obsolete bytes that must NOT be
    # served. The content-addressed protocol never writes this key; it is spelled inline
    # here (the derivation helper is gone) precisely to prove the front never consults it.
    _old_key = f"{keys.full_key(second, keys.NAMESPACE)}sessions/{STEM}{keys.TRANSCRIPT_SUFFIX}"
    bucket.objects[_old_key] = b"obsolete history\n"
    reader = _ReaderSide(bucket)

    outcome = asyncio.run(front.ensure_local_transcript(second, SLOT_ID, reader=reader))

    assert outcome.action == "absent"
    assert (
        _old_key not in reader.requested
    ), "the old per-stem object was fetched -- a deleted conversation would be resurrected"
    assert not (second.sessions_dir / f"{STEM}{keys.TRANSCRIPT_SUFFIX}").exists()


def test_read_transcript_index_refuses_an_absent_committed_index(tmp_path, bucket):
    """A committed generation's index is pointer-referenced, so absence is a deletion, refused.

    Every committing cycle writes the index (empty when there is no transcript) before it
    commits the pointer, so an absent index under a committed pointer is one external
    retention deleted while it was still referenced -- not a generation that published none.
    Reading it as empty would make every stem a fresh conversation and overwrite real
    history, so :func:`read_transcript_index` raises instead of returning ``{}``.
    """
    first = _first_task(tmp_path)
    gen_id = keys.new_generation_id()
    # A committed generation whose index object is NOT in the bucket (deleted by retention).
    with pytest.raises(generation.TranscriptIndexUnusable):
        generation.read_transcript_index(first, _StoreSide(bucket), gen_id)


def test_the_front_refuses_when_the_committed_index_object_is_absent(tmp_path, bucket):
    """A committed pointer whose index object retention deleted must refuse, not serve empty."""
    import json

    second = _replacement_task(tmp_path)
    gen_id = keys.new_generation_id()
    bucket.objects[keys.authority_pointer_key(second)] = json.dumps(
        {"generation": gen_id, "authority": sorted(keys.AUTHORITY_NAMES)}
    ).encode()
    # The index object the pointer references is NOT present (deleted while referenced).
    reader = _ReaderSide(bucket)

    with pytest.raises(front.TranscriptUnavailable):
        asyncio.run(front.ensure_local_transcript(second, SLOT_ID, reader=reader))


def test_the_front_refuses_when_an_index_named_blob_is_absent(tmp_path, bucket):
    """An index-named blob retention deleted must refuse, not serve a fresh conversation."""
    import json

    second = _replacement_task(tmp_path)
    gen_id = keys.new_generation_id()
    bucket.objects[keys.authority_pointer_key(second)] = json.dumps(
        {"generation": gen_id, "authority": sorted(keys.AUTHORITY_NAMES)}
    ).encode()
    # The index NAMES a blob for this stem, but the blob object itself is absent (deleted
    # while the index still references it).
    bucket.objects[keys.transcript_index_key(second, gen_id)] = json.dumps(
        {STEM: "a" * 64}
    ).encode()
    reader = _ReaderSide(bucket)

    with pytest.raises(front.TranscriptUnavailable):
        asyncio.run(front.ensure_local_transcript(second, SLOT_ID, reader=reader))
