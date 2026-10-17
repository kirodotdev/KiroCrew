"""What the writer does with an entry it cannot simply upload.

The contract this subsystem is held to has one rule above the others: nothing in the
backup set is skipped quietly. So every branch that could end in "this object was not
uploaded" is pinned here, and each is pinned on the OBSERVABLE the operator gets --
the cycle result, and whether the cycle raises -- rather than on a log line.

The set splits three ways and the difference matters:

* **Too large.** Uploaded anyway. The restore side will refuse to read it, so the pair
  is incomplete for that one conversation, and the cycle says so.
* **Not a file whose bytes can be uploaded.** Refused, and the cycle raises. A link or
  a FIFO where a transcript belongs is not a transcript.
* **Gone.** Not a failure. An entry listed and then removed is a conversation its owner
  deleted between the two steps.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from container.common import keys
from container.sidecar import backup as backup_mod
from container.sidecar.store import ObjectAbsent

from ._settings_helper import make_settings

STEM = "dashboard_cust-77"


def _authority_keys_in(settings, keyset) -> set[str]:
    """The authority-pair keys in *keyset*, matched by their ``gen/<id>/<name>`` shape.

    A cycle mints a writer-unique generation id, so the pair's keys are not known in
    advance; this picks them out of the store objects or the withheld list.
    """
    prefix = keys.generations_prefix(settings)
    names = set(keys.AUTHORITY_NAMES)
    found: set[str] = set()
    for key in keyset:
        if not key.startswith(prefix):
            continue
        gen_id, _, name = key[len(prefix) :].partition("/")
        if name in names and keys.is_generation_id(gen_id):
            found.add(key)
    return found


class _RecordingStore:
    """Accepts every put and remembers the keys and byte counts.

    ``get`` answers ABSENT rather than raising ``KeyError``, because that is the real
    store's contract and the cycle reads the two differently: an absent generation pointer
    is a first boot, while a pointer that could not be read withholds the authority pair
    for its own reason. A fake that raised ``KeyError`` withheld the pair in every test
    here, which hid whichever branch the test meant to measure.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

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
        self.objects[key] = body.read(size)

    def get(
        self,
        key: str,
        *,
        limit: int,
        deadline: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> bytes:
        try:
            return self.objects[key]
        except KeyError:
            raise ObjectAbsent(key) from None

    def get_with_etag(
        self,
        key: str,
        *,
        limit: int,
        deadline: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> tuple[bytes, str | None]:
        etag = '"etag"' if key in self.objects else None
        return self.get(key, limit=limit), etag


class _FailingStore:
    """Fails the put for any key containing *failing_marker* and accepts the rest."""

    def __init__(self, failing_marker: str) -> None:
        self.failing_marker = failing_marker
        self.objects: dict[str, bytes] = {}

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
        if self.failing_marker in key:
            raise RuntimeError("SlowDown")
        self.objects[key] = body.read(size)

    def get(
        self,
        key: str,
        *,
        limit: int,
        deadline: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> bytes:
        try:
            return self.objects[key]
        except KeyError:
            raise ObjectAbsent(key) from None

    def get_with_etag(
        self,
        key: str,
        *,
        limit: int,
        deadline: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> tuple[bytes, str | None]:
        etag = '"etag"' if key in self.objects else None
        return self.get(key, limit=limit), etag


def _settings(tmp_path: Path):
    s = make_settings(tmp_path, crew="crew-77", prefix="crews")
    for name in keys.AUTHORITY_NAMES:
        (s.config_dir / name).write_bytes(b"{}")
    return s


def _transcript(settings, payload: bytes, stem: str = STEM) -> Path:
    path = settings.sessions_dir / f"{stem}{keys.TRANSCRIPT_SUFFIX}"
    path.write_bytes(payload)
    return path


def _blob_key(settings, payload: bytes) -> str:
    """The content-addressed key the writer stores *payload* under: ``data/blob/<sha256>``."""
    import hashlib as _hashlib

    return keys.blob_key(settings, _hashlib.sha256(payload).hexdigest())


def test_an_object_above_the_ceiling_is_uploaded_and_named(tmp_path, monkeypatch):
    """The size that makes it unreadable on the way back does not make it unwritten."""
    settings = _settings(tmp_path)
    payload = b"x" * 64
    _transcript(settings, payload)
    monkeypatch.setattr(backup_mod, "MAX_OBJECT_BYTES", 8)
    store = _RecordingStore()

    result = backup_mod.run_cycle(settings, store, state={})

    key = _blob_key(settings, payload)
    assert result.above_ceiling == [key]
    assert key in result.uploaded
    assert store.objects[key] == payload
    assert result.complete


def test_a_symlink_where_a_transcript_belongs_is_refused_loudly(tmp_path):
    settings = _settings(tmp_path)
    real = tmp_path / "outside.jsonl"
    real.write_bytes(b"not this task's bytes\n")
    link = settings.sessions_dir / f"{STEM}{keys.TRANSCRIPT_SUFFIX}"
    link.symlink_to(real)
    store = _RecordingStore()

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, store, state={})

    # Unreachable rather than refused: the link is the entry's shape, so every later
    # cycle meets it too and no wait can end.
    assert [name for name, _ in caught.value.result.unreachable] == [link.name]
    assert caught.value.result.refused == []
    _old_key = f"{keys.full_key(settings, keys.NAMESPACE)}sessions/{STEM}{keys.TRANSCRIPT_SUFFIX}"
    assert _old_key not in store.objects


def test_a_fifo_where_a_transcript_belongs_is_refused_without_hanging(tmp_path):
    """A FIFO opened for reading blocks until a writer arrives, so it is refused."""
    settings = _settings(tmp_path)
    path = settings.sessions_dir / f"{STEM}{keys.TRANSCRIPT_SUFFIX}"
    os.mkfifo(path)

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, _RecordingStore(), state={})

    assert [name for name, _ in caught.value.result.unreachable] == [path.name]
    assert caught.value.result.refused == []


def test_a_socket_where_a_transcript_belongs_is_refused_permanently(tmp_path):
    """A UNIX socket named like a transcript is a PERMANENT refusal, not a transient one.

    Opening a socket with ``O_RDONLY | O_NONBLOCK`` fails with ENXIO before the descriptor
    exists, so the open never reaches the ``S_ISREG`` check that would classify a socket as a
    permanent refusal. Treated as transient, it would be retried every cycle and -- being a
    transcript-shaped refusal -- would withhold the authority pair forever, stranding every
    conversation served after the socket appeared. ENXIO is now a permanent refusal, so the
    socket lands in UNREACHABLE (permanent) rather than REFUSED (transient).

    It fails if ENXIO falls through to the generic ``permanent=False`` refusal: the socket
    then appears in ``refused`` and the pair is withheld on every cycle without end.
    """
    import stat as _stat

    settings = _settings(tmp_path)
    path = settings.sessions_dir / f"{STEM}{keys.TRANSCRIPT_SUFFIX}"
    # os.mknod makes the socket NODE directly (no AF_UNIX bind, which would hit the ~108-char
    # sockaddr_un path limit under a deep tmp_path); open_snapshot's O_RDONLY|O_NONBLOCK open
    # of it is what raises ENXIO regardless of how the node was created.
    os.mknod(path, _stat.S_IFSOCK | 0o600)

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, _RecordingStore(), state={})

    # Permanent: it lands in unreachable, not the transient refused list.
    assert [name for name, _ in caught.value.result.unreachable] == [path.name]
    assert caught.value.result.refused == []


def test_an_entry_removed_after_it_is_listed_is_not_a_failure(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    path = _transcript(settings, b"turn\n")
    real_open = backup_mod.open_snapshot

    def _open_after_deleting(target: Path, *, root: Path):
        if target == path:
            path.unlink()
        return real_open(target, root=root)

    monkeypatch.setattr(backup_mod, "open_snapshot", _open_after_deleting)

    result = backup_mod.run_cycle(settings, _RecordingStore(), state={})

    assert result.gone == [path.name]
    assert result.complete


def test_a_failed_upload_raises_and_is_not_recorded_as_done(tmp_path):
    """The next cycle must resend it, so the fingerprint map keeps only successes."""
    settings = _settings(tmp_path)
    _transcript(settings, b"turn\n")
    state: dict = {}
    key = _blob_key(settings, b"turn\n")

    with pytest.raises(backup_mod.BackupIncomplete):
        backup_mod.run_cycle(settings, _FailingStore(keys.BLOB_PREFIX), state=state)

    assert key not in state
    store = _RecordingStore()
    second = backup_mod.run_cycle(settings, store, state=state)
    assert key in second.uploaded
    assert store.objects[key] == b"turn\n"


def test_a_failed_upload_withholds_the_authority_files(tmp_path):
    """A partial cycle must not advance the index past the bytes it failed to write.

    The authority files name the transcripts, so publishing them here would point a
    replacement at an object that is not in the bucket. Leaving them alone keeps the
    pair at the last cycle that completed: older, and true.

    The failure is an UPLOAD failure, which is the case withholding is for: the next
    cycle retries the object and publishes a pair the bucket supports. Planting a FIFO
    instead looks like the same thing and is not -- see the two tests below.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"turn\n")
    store = _FailingStore(keys.BLOB_PREFIX)

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, store, state={})

    assert [name for name, _why in caught.value.result.refused] == [
        f"{STEM}{keys.TRANSCRIPT_SUFFIX}"
    ]
    assert not _authority_keys_in(settings, store.objects)
    assert set(caught.value.result.withheld) == _authority_keys_in(
        settings, caught.value.result.withheld
    )
    assert len(caught.value.result.withheld) == len(keys.AUTHORITY_NAMES)


def test_an_unreachable_entry_does_not_withhold_the_authority_files(tmp_path):
    """Withholding is a WAIT, and a planted name is not something to wait for.

    A FIFO or a symlink at a transcript's path is refused by every cycle, not just this
    one, so withholding the pair on it withholds the pair FOREVER. The index then freezes
    at the moment the entry appeared while transcripts keep uploading past it, and the
    next task replacement restores a conversation list that predates every conversation
    served since -- an unbounded, silent loss, traded for a bounded one.

    So the pair is published. The cycle is still incomplete and the entry is still named,
    which is what an operator needs to remove it.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"turn\n")
    os.mkfifo(settings.sessions_dir / f"planted{keys.TRANSCRIPT_SUFFIX}")
    store = _RecordingStore()

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, store, state={})

    assert len(_authority_keys_in(settings, store.objects)) == len(keys.AUTHORITY_NAMES)
    assert caught.value.result.withheld == []
    assert (
        not caught.value.result.complete
    ), "the cycle did not reach everything, so it must not report a lossless one"


def test_a_second_cycle_publishes_the_pair_again_with_the_entry_still_there(tmp_path):
    """The property the old withholding destroyed: the index keeps up with the bucket.

    One cycle publishing the pair is not the claim -- a freeze shows itself on the cycle
    AFTER the entry appears. So a second cycle runs with the same planted name and a new
    conversation, and the pair it publishes has to reflect that conversation rather than
    staying where the first cycle left it.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"turn\n")
    os.mkfifo(settings.sessions_dir / f"planted{keys.TRANSCRIPT_SUFFIX}")
    state: dict = {}
    store = _RecordingStore()

    with pytest.raises(backup_mod.BackupIncomplete):
        backup_mod.run_cycle(settings, store, state=state)

    (settings.config_dir / "session_map.json").write_bytes(b'{"slot-2": "sid-2"}')
    _transcript(settings, b"a later turn\n", stem="dashboard_cust-92")

    with pytest.raises(backup_mod.BackupIncomplete):
        backup_mod.run_cycle(settings, store, state=state)

    # Slot-agnostic on purpose: the generation protocol alternates slots, so naming one
    # would pin the alternation instead of the property. These bytes reached the config
    # file only between the two cycles, so only the second cycle can have published them.
    published = [raw for key, raw in store.objects.items() if key.endswith("session_map.json")]
    assert b'{"slot-2": "sid-2"}' in published, (
        "the second cycle must republish the index; a pair frozen at the first cycle is "
        "the permanent freeze this split exists to prevent"
    )
    assert _blob_key(settings, b"a later turn\n") in store.objects


def test_a_directory_named_like_a_transcript_is_not_in_the_set(tmp_path):
    settings = _settings(tmp_path)
    (settings.sessions_dir / f"{STEM}{keys.TRANSCRIPT_SUFFIX}").mkdir()

    names = [key for key, _ in backup_mod.objects_to_back_up(settings).data]

    _old_key = f"{keys.full_key(settings, keys.NAMESPACE)}sessions/{STEM}{keys.TRANSCRIPT_SUFFIX}"
    assert _old_key not in names


def test_a_file_without_the_transcript_suffix_is_not_in_the_set(tmp_path):
    settings = _settings(tmp_path)
    (settings.sessions_dir / "notes.txt").write_bytes(b"scratch\n")

    paths = [path.name for _, path in backup_mod.objects_to_back_up(settings).data]

    assert "notes.txt" not in paths


def test_an_entry_whose_type_readdir_withheld_is_classified_before_the_fd_closes(
    tmp_path, monkeypatch
):
    """``DT_UNKNOWN`` makes ``is_dir`` stat through the listing's own descriptor.

    ``os.scandir(fd)`` hands back entries whose ``dir_fd`` IS that descriptor and whose
    ``path`` is the bare name, so an entry whose type ``readdir`` did not report answers
    ``is_dir`` by ``fstatat`` through it. CPython documents that type for network
    filesystems, and the data home is EFS-mounted, so this is the deployed case rather
    than a corner of it.

    Pinned because the failure is total and silent in shape: classified after the close,
    the entry raises ``EBADF`` from inside ``objects_to_back_up``, which ``run_cycle``
    calls ABOVE its own ``try`` -- so every cycle for the life of the task dies
    identically and not one transcript, segment or authority file is ever uploaded.
    """
    settings = _settings(tmp_path)
    name = f"{STEM}{keys.TRANSCRIPT_SUFFIX}"
    _transcript(settings, b"{}")
    real_scandir = os.scandir

    class _TypeWithheld:
        """A ``DirEntry`` that has to reach the kernel to answer ``is_dir``."""

        def __init__(self, entry_name: str, dir_fd: int) -> None:
            self.name = entry_name
            self._dir_fd = dir_fd

        def is_dir(self, *, follow_symlinks: bool = True) -> bool:
            # Exactly what CPython's fallback does, and the one call a closed
            # descriptor turns into EBADF.
            os.fstat(self._dir_fd)
            return False

    def scandir_withholding_types(arg):
        if not isinstance(arg, int):
            return real_scandir(arg)

        class _Listing:
            def __enter__(self) -> list[_TypeWithheld]:
                return [_TypeWithheld(name, arg)]

            def __exit__(self, *exc: object) -> None:
                """Never suppresses, so the real listing's behaviour is unchanged."""

        return _Listing()

    monkeypatch.setattr(os, "scandir", scandir_withholding_types)
    found, refused = backup_mod._live_transcripts(settings)

    assert refused == []
    assert [path.name for path in found] == [name]


def test_a_linked_directory_under_the_archive_is_named_rather_than_walked_past(tmp_path):
    """The one drop ``onerror`` cannot see, and the loudest thing it could be.

    ``os.fwalk(follow_symlinks=False)`` does not descend a linked subdirectory and does
    not report it: it compares the name's ``lstat`` against the opened descriptor's
    ``stat`` and drops the entry without calling ``onerror``. Dropped that way its
    segments reach neither the set nor either refusal list, so the cycle reports itself
    COMPLETE, the pointer advances over an index naming conversations whose archived
    halves were never uploaded, and the archive is on an ephemeral disk -- gone, with no
    record of which ones.

    Unreachable rather than refused, on the same permanence rule the archive root
    follows: a link does not become a directory next cycle, so withholding the pair on
    it would never end.
    """
    settings = _settings(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "segment-1.jsonl").write_bytes(b"not this task's bytes\n")
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    (settings.archive_dir / "kept.jsonl").write_bytes(b"{}")
    link = settings.archive_dir / "linked"
    link.symlink_to(outside, target_is_directory=True)

    sink = backup_mod._ArchiveWalkSink()
    found = list(backup_mod._archived_segments(settings, sink))

    assert [path.name for path in found] == ["kept.jsonl"]
    assert [where for where, _ in sink.unreachable] == [str(link)]
    assert sink.refused == []


def test_the_authority_descriptors_are_closed_when_an_enumerator_raises(tmp_path, monkeypatch):
    """The authority pair is opened first, so a later raise must not strand it.

    ``run_cycle``'s ``finally: plan.close_authority()`` is the only close site, and it
    calls this function ABOVE that ``try`` -- so a raise from an enumerator escapes with
    both descriptors still open and this function is the only place that can release
    them.

    Asserted on the snapshots THEMSELVES rather than on a descriptor count, and the test
    keeps a reference to each. Nothing else holds one once the raise unwinds, so
    refcounting closes the file objects the moment they are dropped and a count taken
    afterwards is identical whether or not anything released them on purpose -- it would
    pass with the release removed, which is a test that measures nothing. Holding the
    reference is what makes "released deliberately" the only way this can be green.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"{}")
    opened: list[backup_mod.Snapshot] = []
    real_open_snapshot = backup_mod.open_snapshot

    def recording_open_snapshot(path, *, root):
        snapshot = real_open_snapshot(path, root=root)
        if snapshot is not None:
            opened.append(snapshot)
        return snapshot

    def stale(_settings_arg):
        raise OSError("stale file handle")

    monkeypatch.setattr(backup_mod, "open_snapshot", recording_open_snapshot)
    monkeypatch.setattr(backup_mod, "_live_transcripts", stale)

    with pytest.raises(OSError):
        backup_mod.objects_to_back_up(settings)

    assert len(opened) == len(keys.AUTHORITY_NAMES)
    assert [snapshot.fh.closed for snapshot in opened] == [True] * len(opened)


def test_backup_incomplete_reports_the_true_count_not_the_capped_sample():
    """The count comes from the true counters, so an over-cap run is not under-reported.

    ``CycleResult`` keeps a bounded SAMPLE of names plus an exact counter per kind. The
    exception's message must take its count from the counters, or a run with more failures
    than the sample cap -- 4,097 reported as 4,096 -- would understate the loss. GPT flagged
    the old ``len(blocked)`` form, which counted the capped sample list.
    """
    over = backup_mod._RESULT_SAMPLE_CAP + 500
    result = backup_mod.CycleResult()
    for i in range(over):
        result.record_refused(f"refused-{i}", "boom")
    for i in range(over):
        result.record_unreachable(f"unreachable-{i}", "a link")
    for i in range(over):
        result.record_gone_referenced(f"vanished-{i}")

    message = str(backup_mod.BackupIncomplete(result))
    # The sample lists are capped, but the reported totals are the exact counters.
    assert len(result.refused) == backup_mod._RESULT_SAMPLE_CAP
    assert f"{over * 3} object(s) in the backup set did not reach the bucket" in message
    assert f"{over} refused" in message
    assert f"{over} unreachable" in message
    assert f"{over} vanished-but-indexed" in message
