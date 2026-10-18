"""The four ways the first version of this pair claimed durability it did not have.

Each test here exists because a review found a path where the writer reported success,
or kept reporting progress, while a task replacement would still have lost the
conversation. They are grouped together because they are one theme: the difference
between "the backup ran" and "the bytes are in the bucket".

1. **The shutdown cycle has to BEGIN after the stop.** The supervisor signals this
   process only after the backend has flushed, so a cycle already running when the
   signal lands cannot contain that flush. Accepting it as the final one loses exactly
   the turns the final cycle exists to save.
2. **A failure no retry resolves has to end the process.** A denied ``PutObject`` is
   not a slow bucket. Retrying it at the next interval forever fills the log while the
   task keeps taking turns nothing will ever save.
3. **A missing bucket is not an absent object.** Read as absence, it reports both
   authority files missing, and the backend boots with an empty slot table and flushes
   it over the real one.
4. **A symlink above a file is as dangerous as a symlink at it.** ``O_NOFOLLOW`` guards
   the last name only, so a plain descent follows a link planted at the archive
   directory -- which the agent writes in -- and uploads every file behind it.
5. **The index must not name bytes that are not there.** The authority files say which
   conversations exist and the front fetches each named transcript lazily, so an
   authority table uploaded ahead of its transcripts sends the front to an absent
   object, which it reads as a conversation that never had history.
6. **The index is a snapshot, not a read.** Opening the authority files fixes the
   instant they describe. Read at send time instead, a slot the backend flushed
   mid-cycle names a transcript that cycle never enumerated.
7. **A body the transport cannot rewind is not a body it can send.** The transport seeks
   the body back before it signs or resends; a body that refuses turns a transient error
   into a lost object, and one that can seek PAST its snapshot sends bytes the cycle never
   measured.
8. **The final cycle needs a bound, not just a window.** It uploads sequentially and
   nothing bounds how many objects changed, so the drain window can elapse mid-upload.
   A deadline turns that from a kill into a short cycle that names what it missed.
9. **The index needs its own room inside that bound.** A data phase allowed to spend
   the whole deadline leaves the authority pair none, and those PUTs are then killed
   mid-request -- publishing one file and not the other.
10. **Publication links an inode, not a name.** Closing the temporary before linking it
    publishes whatever its pathname points at by then, and these are directories the
    agent writes in.
11. **A bound the transport does not honour is not a bound.** The gate admits an upload by
    reserving what one PUT may cost, so that number has to be derived from what the client
    is configured to spend -- every attempt's connect AND read, plus the waits between
    attempts. Reserved short, the gate admits an object the drain window cannot finish and
    the kill lands mid-PUT, which is the outcome the deadline exists to replace.
12. **Deriving the number is not enforcing it.** A socket timeout bounds one read, not a
    request: a connection handing over small chunks inside that timeout never trips it, so
    a large object's send time is unbounded no matter how the reservation was computed. The
    window the gate reserves has to be handed to the transmission and stop it, or the gate
    is reserving time the PUT is free to overrun -- back to a kill mid-PUT, with this object
    and every object behind it lost and unnamed.

Items 1 to 8 are tested in this module, and the rest in sibling modules, one theme each:
``test_sidecar_durability_gaps_deadline.py`` (items 9, 11 and 12, the final cycle's bound),
``test_sidecar_durability_gaps_restore.py`` (item 10, and the boot's read of the committed
generation), ``test_sidecar_durability_gaps_commit.py`` (the generation commit),
``test_sidecar_durability_gaps_walk.py`` (a transcript tree that changes under the walk) and
``test_sidecar_durability_gaps_ownership.py`` (the writer's ownership claim). The fakes they
share are in ``sidecar_durability_gaps_helpers.py``.
"""

from __future__ import annotations

import errno
import os
import shutil
import signal
import threading
import time
from collections.abc import Callable

import pytest
from container.common import config as cfg
from container.common import keys, objects
from container.sidecar import __main__ as sidecar_main
from container.sidecar import backup as backup_mod
from container.sidecar import restore as restore_mod
from container.sidecar.store import ObjectAbsent

from .sidecar_durability_gaps_helpers import (
    STEM,
    _authority_keys_in,
    _blob_key,
    _ClockProxy,
    _Recorder,
    _settings,
    _transcript,
)

# --- 1. the shutdown cycle begins after the stop --------------------------------


def test_a_cycle_in_flight_when_the_stop_arrives_is_not_the_final_one(tmp_path):
    """The flush the supervisor is waiting for happens AFTER that cycle started.

    Driven the way the real signal arrives: the stop is set from inside the first
    cycle's upload, which is exactly the window a SIGTERM during an orderly deploy
    lands in. The file then grows, standing in for what the backend's drain flushes,
    and the assertion is that the bucket ends up holding the grown bytes.
    """
    settings = _settings(tmp_path)
    path = _transcript(settings, b"before-flush\n")
    stop = threading.Event()
    blob_prefix = keys.full_key(settings, keys.NAMESPACE + keys.BLOB_PREFIX)

    class _SignallingRecorder(_Recorder):
        """Sets the stop mid-upload, then writes what the backend's drain would flush."""

        def put(
            self,
            key_: str,
            body,
            size: int,
            *,
            budget: float | None = None,
            cancel: Callable[[], bool] | None = None,
            if_match: str | None = None,
            if_none_match: str | None = None,
        ) -> None:
            super().put(key_, body, size, if_match=if_match, if_none_match=if_none_match)
            # The transcript is now written at a content-addressed blob key, so the stop is
            # tripped on the first blob PUT (the transcript's) rather than a fixed key name.
            if key_.startswith(blob_prefix) and not stop.is_set():
                stop.set()
                path.write_bytes(b"before-flush\nflushed-on-drain\n")

    store = _SignallingRecorder()

    assert sidecar_main.run(settings, store, stop=stop) == 0
    assert (
        store.objects[_blob_key(settings, b"before-flush\nflushed-on-drain\n")]
        == b"before-flush\nflushed-on-drain\n"
    )


def test_the_post_stop_cycle_runs_whole_before_the_process_returns(tmp_path):
    """Returning while the final cycle is still uploading is the same loss.

    ``run`` may only return once the post-stop cycle has finished, so a cycle that is
    counted has also completed. Pinned by counting cycles: a stop observed during the
    first one produces exactly two, not one.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"turn\n")
    stop = threading.Event()
    cycles: list[int] = []
    real_run_cycle = backup_mod.run_cycle

    def counting(*args, **kwargs):
        cycles.append(1)
        if len(cycles) == 1:
            stop.set()
        return real_run_cycle(*args, **kwargs)

    store = _Recorder()
    saved, backup_mod.run_cycle = backup_mod.run_cycle, counting
    try:
        assert sidecar_main.run(settings, store, stop=stop) == 0
    finally:
        backup_mod.run_cycle = saved
    assert len(cycles) == 2


def test_a_post_stop_cycle_that_did_not_complete_exits_non_zero(tmp_path):
    """A clean return would tell the operator the final state is durable.

    The upload is refused with a transient code, which during normal running is logged
    and retried at the next interval. On the way out there is no next interval, so the
    only place it can still be reported is the exit code.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"turn\n")
    stop = threading.Event()
    stop.set()

    class _Throttled:
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
            raise RuntimeError("SlowDown")

        def get(
            self,
            key: str,
            *,
            limit: int,
            deadline: float | None = None,
            cancel: Callable[[], bool] | None = None,
        ) -> bytes:  # pragma: no cover - unused
            raise ObjectAbsent(key)

        def get_with_etag(
            self,
            key: str,
            *,
            limit: int,
            deadline: float | None = None,
            cancel: Callable[[], bool] | None = None,
        ) -> tuple[bytes, str | None]:
            # The startup incarnation reads the pointer through this; absent means
            # generation 0, so the task mints its base token rather than refusing.
            raise ObjectAbsent(key)

    assert sidecar_main.run(settings, _Throttled(), stop=stop) == 1


# --- 2. a permanent failure ends the process ------------------------------------


class _DeniedStore:
    """Every put is refused with a code no retry resolves."""

    def __init__(self) -> None:
        self.attempts = 0

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
        self.attempts += 1
        raise objects.StoreUnusable(f"PutObject on s3://b/{key} failed with AccessDenied")

    def get(
        self,
        key: str,
        *,
        limit: int,
        deadline: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> bytes:  # pragma: no cover - unused
        raise ObjectAbsent(key)

    def get_with_etag(
        self,
        key: str,
        *,
        limit: int,
        deadline: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> tuple[bytes, str | None]:
        # Absent pointer at startup: generation 0, base token minted, then the first PUT
        # meets the permanent denial. Not raised here, so the denial is what ends the task.
        raise ObjectAbsent(key)


def test_a_permanently_denied_upload_is_not_retried_at_the_next_interval(tmp_path):
    """Retrying it is a durability window that never closes while the log claims work.

    ``max_cycles`` would allow several passes, so a loop that swallowed this would show
    more than one attempt. Exactly one means it left the loop on the first answer.
    """
    settings = _settings(tmp_path, interval=1)
    _transcript(settings, b"turn\n")
    store = _DeniedStore()

    with pytest.raises(objects.StoreUnusable):
        sidecar_main.run(settings, store, max_cycles=5)

    assert store.attempts == 1


def test_a_permanent_denial_becomes_a_non_zero_exit_code(tmp_path, monkeypatch):
    """The supervisor reads the exit code, so the classification has to reach it."""
    settings = _settings(tmp_path)
    _transcript(settings, b"turn\n")
    monkeypatch.setattr(sidecar_main.common, "load", lambda: settings)
    monkeypatch.setattr(sidecar_main, "S3ObjectStore", lambda bucket: _DeniedStore())
    # ``main`` installs the sidecar's own SIGTERM/SIGINT handlers, and nothing restores
    # them: left in place they belong to this pytest worker for the rest of the session,
    # so a later Ctrl-C or a CI cancellation would set a sidecar stop event instead of
    # interrupting the run. The exit code is what this test is about, and the handlers
    # are not part of it.
    monkeypatch.setattr(signal, "signal", lambda *_args: None)

    assert sidecar_main.main([]) == 3


def test_a_throttle_is_still_retried_rather_than_fatal(tmp_path):
    """The rule is about permanence, not about failure, so the common case is unchanged.

    A ``SlowDown`` resolves itself, and exiting on it would tear the task down and lose
    the state the backup exists to keep -- the opposite mistake.
    """
    settings = _settings(tmp_path, interval=1)
    _transcript(settings, b"turn\n")

    class _Throttled:
        def __init__(self) -> None:
            self.attempts = 0

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
            self.attempts += 1
            raise RuntimeError("SlowDown")

        def get(
            self,
            key: str,
            *,
            limit: int,
            deadline: float | None = None,
            cancel: Callable[[], bool] | None = None,
        ) -> bytes:  # pragma: no cover - unused
            raise ObjectAbsent(key)

        def get_with_etag(
            self,
            key: str,
            *,
            limit: int,
            deadline: float | None = None,
            cancel: Callable[[], bool] | None = None,
        ) -> tuple[bytes, str | None]:
            # Absent pointer at startup -> generation 0 -> base token; the throttle below is
            # what the test exercises, so the pointer read must not itself end the task.
            raise ObjectAbsent(key)

    store = _Throttled()
    assert sidecar_main.run(settings, store, max_cycles=2) == 0
    assert store.attempts > 1


# --- 3. a missing bucket is not an absent object --------------------------------


def test_a_missing_bucket_is_not_in_the_absence_set():
    """Absence lets the boot continue; this must not.

    Pinned on the set itself as well as on the behaviour below, because the set is the
    thing an edit would reach for: adding a code here is adding a way to boot empty.
    """
    assert "NoSuchBucket" not in objects.ABSENT_CODES
    assert "NoSuchBucket" in objects.PERMANENT_CODES


def test_a_missing_bucket_refuses_the_boot_instead_of_restoring_nothing(tmp_path):
    """The failure this prevents is silent: an empty list, then a flush over the real one."""
    settings = _settings(tmp_path)

    class _NoBucket:
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
        ) -> None:  # pragma: no cover - unused
            raise AssertionError("restore does not put")

        def get(
            self,
            key: str,
            *,
            limit: int,
            deadline: float | None = None,
            cancel: Callable[[], bool] | None = None,
        ) -> bytes:
            raise objects.StoreUnusable("GetObject on s3://typo/x failed with NoSuchBucket")

    with pytest.raises(restore_mod.RestoreFailed, match="not the same"):
        restore_mod.restore_authority(settings, _NoBucket())


def test_one_absence_set_serves_both_processes():
    """Two copies is how the bucket case diverged in the first place.

    The front's reader and the writer's store classify the same answer, so the set has
    exactly one definition and both reach it here.
    """
    from container.front import transcript as front_transcript
    from container.sidecar import store as sidecar_store

    assert front_transcript.objects.ABSENT_CODES is objects.ABSENT_CODES
    assert sidecar_store.is_absent is objects.is_absent


# --- 4. a symlink ABOVE the file --------------------------------------------------


def test_a_symlinked_archive_directory_uploads_nothing_behind_it(tmp_path):
    """``followlinks=False`` governs directories the walk FINDS, not the root it is given.

    The link is planted where rotation writes, which is a directory the agent already
    writes in, and it points at a tree holding a file that is not this task's state. The
    cycle must refuse rather than give that file a key of its own.
    """
    settings = _settings(tmp_path)
    outside = tmp_path / "not-the-data-home"
    outside.mkdir()
    (outside / "boot-secret").write_bytes(b"a credential\n")
    shutil.rmtree(settings.archive_dir)
    settings.archive_dir.symlink_to(outside, target_is_directory=True)
    store = _Recorder()

    with pytest.raises(backup_mod.BackupIncomplete):
        backup_mod.run_cycle(settings, store, state={})

    assert not any("boot-secret" in key for key in store.objects)


def test_a_symlinked_directory_above_a_transcript_refuses_the_open(tmp_path):
    """The descent is what refuses it: ``O_NOFOLLOW`` on the last name cannot.

    ``sessions`` is replaced, so the transcript's own name is a real file and only the
    directory above it is a link. Opening the full path in one call would succeed.
    """
    settings = _settings(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = elsewhere / f"{STEM}{keys.TRANSCRIPT_SUFFIX}"
    target.write_bytes(b"not this task's turn\n")
    shutil.rmtree(settings.sessions_dir)
    settings.sessions_dir.symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(backup_mod.RefusedEntry, match="symlink"):
        backup_mod.open_snapshot(
            settings.sessions_dir / f"{STEM}{keys.TRANSCRIPT_SUFFIX}",
            root=settings.data_home,
        )


def test_an_ordinary_nested_archive_segment_is_still_uploaded(tmp_path):
    """The descent must not cost the nesting rotation is free to use."""
    settings = _settings(tmp_path)
    nested = settings.archive_dir / "2026" / "09"
    nested.mkdir(parents=True, exist_ok=True)
    segment = nested / f"{STEM}-0001{keys.TRANSCRIPT_SUFFIX}"
    payload = b"older half\n"
    segment.write_bytes(payload)
    store = _Recorder()

    backup_mod.run_cycle(settings, store, state={})

    # An archive segment keeps its full RELATIVE PATH as the key prefix (so the control
    # plane finds a conversation's rows by prefix-listing that path) and appends its content
    # DIGEST, so two overlapping writers of distinct bytes never collide on one mutable key.
    import hashlib

    digest = hashlib.sha256(payload).hexdigest()
    assert store.objects[keys.archive_segment_key(settings, segment, digest)] == payload


# --- 5. the index is published last, and only when the bytes are there ------------


def test_every_transcript_is_committed_before_the_authority_files(tmp_path):
    """The order is pinned on the recorded SEQUENCE, because both orders upload both.

    A transcript PUT that fails after the authority table is already in the bucket
    leaves a table naming an object nobody can fetch, and the front serves that slot
    as a conversation with no history.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    store = _Recorder()

    backup_mod.run_cycle(settings, store, state={})

    # The DATA phase is the content-addressed blobs. The authority phase is the pair, the
    # transcript index, and the pointer -- all under gen/<id>/ or the pointer name -- and it
    # must come after every blob, so a blob PUT that failed cannot leave a committed index
    # naming it.
    blob_prefix = keys.full_key(settings, keys.NAMESPACE + keys.BLOB_PREFIX)
    authority = _authority_keys_in(settings, store.puts)
    last_data = max(i for i, key in enumerate(store.puts) if key.startswith(blob_prefix))
    first_authority = min(i for i, key in enumerate(store.puts) if key in authority)
    assert last_data < first_authority


def test_the_generation_pointer_is_the_last_object_of_the_cycle(tmp_path):
    """The whole safety of the pointer rests on this order, so it is pinned on the sequence.

    A pointer committed before the pair would name a generation a cycle interrupted
    uploaded, and every replacement task would then refuse to boot on a bucket that only
    mid-phase never finished writing. Committed last, an interruption leaves the pointer
    all, which the restore reads as a crew that has not published a pair yet.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    store = _Recorder()

    backup_mod.run_cycle(settings, store, state={})

    pointer = keys.authority_pointer_key(settings)
    assert store.puts[-1] == pointer
    assert all(i < store.puts.index(pointer) for i in range(len(store.puts) - 1))


def test_a_refused_transcript_withholds_the_authority_files_entirely(tmp_path):
    """Withholding leaves the pair at the last complete cycle: older, and coherent.

    Publishing the table here would advance the index past bytes this cycle failed to
    write, which is the same loss as publishing it first.

    A FAILED UPLOAD is the case withholding is for, and it is what this plants. An earlier
    version planted a symlinked archive root instead: that is refused by every later cycle
    as well, so withholding on it never ends -- pinned as its own case below.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    blob_prefix = keys.full_key(settings, keys.NAMESPACE + keys.BLOB_PREFIX)

    class _TranscriptPutFails(_Recorder):
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
            # The transcript is written at a content-addressed blob key now, so the planted
            # failure is on the blob PUT rather than a ``.jsonl`` key name.
            if key.startswith(blob_prefix):
                raise RuntimeError("SlowDown")
            super().put(key, body, size, if_match=if_match, if_none_match=if_none_match)

    store = _TranscriptPutFails()

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, store, state={})

    assert not _authority_keys_in(settings, store.objects)
    assert set(caught.value.result.withheld) == _authority_keys_in(
        settings, caught.value.result.withheld
    )
    assert len(caught.value.result.withheld) == len(keys.AUTHORITY_NAMES)


def test_a_permanently_unreachable_entry_leaves_the_authority_files_published(tmp_path):
    """The mirror, and the reason the two are not one list.

    A planted NAME is refused on every cycle, so withholding the pair on it freezes the
    index permanently: a replacement task then restores a conversation list from before the
    link was planted, while every other transcript keeps uploading unreferenced. The cycle
    stays incomplete and names the entry; the pair is published.

    The example is one entry under a healthy root, which is what the accepted residue
    actually is. A refused ROOT is the other case and withholds -- see
    :func:`test_an_unreachable_data_root_withholds_the_authority_files`.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    outsider = elsewhere / "not-this-task.jsonl"
    outsider.write_bytes(b"someone else's turn\n")
    planted = settings.sessions_dir / f"planted{keys.TRANSCRIPT_SUFFIX}"
    planted.symlink_to(outsider)
    store = _Recorder()

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, store, state={})

    assert len(_authority_keys_in(settings, store.objects)) == len(keys.AUTHORITY_NAMES)
    assert caught.value.result.withheld == []
    assert [name for name, _why in caught.value.result.unreachable] == [planted.name]
    assert not any(
        "not-this-task" in key for key in store.objects
    ), "nothing behind the link may be given a key of its own"
    # The healthy transcript beside it still reaches the bucket, which is why freezing the
    # index on this refusal would be the worse trade.
    assert _blob_key(settings, b"a turn\n") in store.objects


def test_an_unreachable_sessions_root_withholds_the_authority_files(tmp_path):
    """A refused LIVE root is the whole tree the pair can send a reader to.

    ``os.scandir`` and ``os.walk`` resolve the root they are given, so a link planted there
    lists a stranger's files as this task's; each is then refused per-ENTRY on the way down.
    Treated as the accepted residue, every transcript is refused while the pair publishes,
    and the replacement reads each absent object as a conversation that never had history --
    silent loss of all of it, not of one name. Holding the pointer at the last complete
    generation gives up nothing that was going to be uploaded.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / f"{STEM}-0001{keys.TRANSCRIPT_SUFFIX}").write_bytes(b"a stranger's half\n")
    root = settings.sessions_dir
    shutil.rmtree(root)
    root.symlink_to(elsewhere, target_is_directory=True)
    store = _Recorder()

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, store, state={})

    assert set(caught.value.result.withheld) == _authority_keys_in(
        settings, caught.value.result.withheld
    )
    assert len(caught.value.result.withheld) == len(keys.AUTHORITY_NAMES)
    assert not _authority_keys_in(settings, store.objects)
    assert settings.sessions_dir.name in [name for name, _why in caught.value.result.refused]
    assert not any(f"{STEM}-0001" in key for key in store.objects)


def test_an_unreachable_archive_root_does_not_freeze_the_authority_files(tmp_path):
    """The archive root is the one root withholding cannot protect, so it must not withhold.

    A link or non-directory there is a SHAPE: every later cycle meets the identical error, so
    a withholding started here never ends and the index freezes at the moment the name
    appeared while live transcripts keep uploading past it. And it buys nothing even once --
    the front deliberately never fetches archived segments (it would have to list), so the
    published pair cannot send a reader to the subtree that went unenumerated. The cycle still
    fails loudly and names the entry; only the freeze is given up.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / f"{STEM}-0001{keys.TRANSCRIPT_SUFFIX}").write_bytes(b"a stranger's half\n")
    root = settings.archive_dir
    shutil.rmtree(root)
    root.symlink_to(elsewhere, target_is_directory=True)
    store = _Recorder()

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, store, state={})

    result = caught.value.result
    assert [name for name, _why in result.unreachable] == [settings.archive_dir.name]
    assert result.refused == []
    assert result.withheld == [], "a permanent shape must not withhold the pair"
    assert len(_authority_keys_in(settings, store.objects)) == len(keys.AUTHORITY_NAMES)
    assert not any(f"{STEM}-0001" in key for key in store.objects)
    # The live transcript still reaches the bucket, which is what the freeze would have cost.
    assert _blob_key(settings, b"a turn\n") in store.objects


def test_a_directory_the_walk_cannot_list_is_named_rather_than_skipped(tmp_path):
    """``os.fwalk`` swallows an OSError and continues when ``onerror`` is unset.

    The segments under it then contributed nothing, no refusal was recorded, the cycle
    reported itself complete and the pointer advanced -- and the archive lives on an ephemeral
    task disk, so those segments were gone with no record of which ones.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    buried = settings.archive_dir / "locked"
    buried.mkdir(parents=True)
    (buried / f"{STEM}-0001{keys.TRANSCRIPT_SUFFIX}").write_bytes(b"an older half\n")
    real_fwalk = os.fwalk

    def fwalk_that_cannot_list_one_dir(*args, **kwargs):
        onerror = kwargs.get("onerror")
        for entry in real_fwalk(*args, **kwargs):
            parent, dirnames, _filenames, _fd = entry
            if "locked" in dirnames:
                dirnames.remove("locked")
                exc = PermissionError(errno.EACCES, "Permission denied")
                exc.filename = str(buried)
                assert onerror is not None, "the walk was given no onerror collector"
                onerror(exc)
            yield entry

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(backup_mod.os, "fwalk", fwalk_that_cannot_list_one_dir)
        with pytest.raises(backup_mod.BackupIncomplete) as caught:
            backup_mod.run_cycle(settings, _Recorder(), state={})

    result = caught.value.result
    assert [name for name, _why in result.refused] == [str(buried)]
    assert not result.complete, "a subtree that could not be listed is not a complete cycle"
    assert not any(f"{STEM}-0001" in key for key in result.uploaded)


def test_a_clean_cycle_still_publishes_the_authority_files(tmp_path):
    """The withholding is conditional. A cycle that reaches everything publishes both."""
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    store = _Recorder()

    result = backup_mod.run_cycle(settings, store, state={})

    assert len(_authority_keys_in(settings, store.objects)) == len(keys.AUTHORITY_NAMES)
    assert result.withheld == []


# --- 6. the index is a snapshot taken before the enumeration it indexes -----------


def test_the_authority_files_are_opened_before_the_transcripts_are_enumerated(
    tmp_path, monkeypatch
):
    """Opening is what fixes the instant. Reading at send time indexes a later state.

    The backend can republish a slot table at any point in a cycle, and it does so the
    way it publishes a transcript: a temporary file and a rename, which leaves an already
    open descriptor addressing the whole previous version. So the pair this cycle sends is
    the index as it stood BEFORE the enumeration. Read at send time instead, the table
    would name a slot whose transcript this cycle never listed, and the bucket would hold
    an index pointing at bytes that are not there.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    slots = settings.config_dir / "open_slots.json"
    slots.write_bytes(b'{"keys": []}')
    real = backup_mod._live_transcripts

    def republish_a_slot_mid_cycle(s):
        replacement = slots.with_suffix(".json.tmp")
        replacement.write_bytes(b'{"keys": ["cust-new"]}')
        replacement.replace(slots)
        return real(s)

    monkeypatch.setattr(backup_mod, "_live_transcripts", republish_a_slot_mid_cycle)
    store = _Recorder()

    backup_mod.run_cycle(settings, store, state={})

    published = next(
        key
        for key in _authority_keys_in(settings, store.objects)
        if key.endswith("/open_slots.json")
    )
    assert store.objects[published] == b'{"keys": []}'


def test_the_authority_descriptors_are_closed_even_when_the_phase_is_withheld(tmp_path):
    """The withheld path never sends them, so closing cannot live at the send site."""
    settings = _settings(tmp_path)
    plan = backup_mod.objects_to_back_up(settings)
    assert plan.authority, "the fixture writes both authority files"

    plan.close_authority()

    assert all(snapshot.fh.closed for _key, snapshot in plan.authority)


def test_an_authority_file_that_does_not_exist_yet_is_not_a_failure(tmp_path):
    """On a first boot the backend has not written one, and there is no index to keep."""
    settings = _settings(tmp_path)
    for name in keys.AUTHORITY_NAMES:
        (settings.config_dir / name).unlink()
    store = _Recorder()

    result = backup_mod.run_cycle(settings, store, state={})

    assert sorted(result.gone) == sorted(keys.AUTHORITY_NAMES)
    assert result.refused == []


# --- the drain windows and the platform stop timeout are one contract -------------


def test_the_stop_timeout_covers_every_drain_window_the_supervisor_spends():
    """The supervisor spends the three in sequence; the platform must outlast their sum."""
    from container.common import config as cfg

    assert cfg.TASK_STOP_TIMEOUT_SECS >= (
        cfg.FRONT_DRAIN_SECS + cfg.BACKEND_DRAIN_SECS + cfg.SIDECAR_DRAIN_SECS
    )


def test_the_supervisor_reads_the_shared_drain_windows_rather_than_its_own():
    """One contract, one definition: a private copy here drifts from the task definition."""
    from container.common import config as cfg
    from container.supervisor import __main__ as sup

    assert (sup.FRONT_DRAIN_SECS, sup.BACKEND_DRAIN_SECS, sup.SIDECAR_DRAIN_SECS) == (
        cfg.FRONT_DRAIN_SECS,
        cfg.BACKEND_DRAIN_SECS,
        cfg.SIDECAR_DRAIN_SECS,
    )


# --- 7. a body the transport cannot rewind is not a body it can send --------------


def test_the_upload_body_can_be_rewound_for_a_retry(tmp_path):
    """The transport seeks the body back before it signs or resends it.

    A body that refuses to seek turns a transient S3 error into a lost object, and on the
    final cycle there is no next interval to correct it.
    """
    path = _transcript(_settings(tmp_path), b"one turn\n")
    with path.open("rb") as fh:
        reader = objects.BoundedReader(fh, 9)

        first = reader.read()
        assert reader.seekable()
        reader.seek(0)
        second = reader.read()

    assert first == second == b"one turn\n"


def test_a_rewind_restores_the_bound_rather_than_the_file_length(tmp_path):
    """The point of the bound survives the rewind: a grown file still sends its prefix."""
    settings = _settings(tmp_path)
    path = _transcript(settings, b"one turn\n")
    with path.open("rb") as fh:
        reader = objects.BoundedReader(fh, 9)
        reader.read()
        path.write_bytes(b"one turn\nand another\n")

        reader.seek(0)

        assert reader.read() == b"one turn\n"


def test_a_rewind_cannot_reach_outside_the_snapshot(tmp_path):
    """Offsets are the view's own, so no transport can seek to a byte it did not include."""
    settings = _settings(tmp_path)
    path = _transcript(settings, b"one turn\nand another\n")
    with path.open("rb") as fh:
        reader = objects.BoundedReader(fh, 9)

        assert reader.seek(-5) == 0
        assert reader.seek(500) == 9
        assert reader.read() == b""


# --- 8. the final cycle is bounded, and says what it did not reach -----------------


def test_the_final_cycle_stops_at_its_deadline_and_names_what_it_skipped(tmp_path):
    """An overrun must be a report, not a kill in the middle of a PUT.

    A deadline already in the past leaves room for nothing, so every object is recorded by
    name and the cycle raises. Trying one more and being SIGKILLed would lose that object
    AND leave the ones behind it unmentioned.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    _transcript(settings, b"another\n", stem="dashboard_cust-92")
    store = _Recorder()

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, store, state={}, deadline=time.monotonic() - 1)

    skipped = {name for name, _why in caught.value.result.refused}
    assert skipped == {
        f"{STEM}{keys.TRANSCRIPT_SUFFIX}",
        f"dashboard_cust-92{keys.TRANSCRIPT_SUFFIX}",
    }
    assert store.objects == {}


def test_the_upload_set_is_enumerated_lazily_so_the_deadline_gate_runs_first(tmp_path):
    """The whole inventory must not be built before the deadline check can bound it.

    With retention off and archive segments accumulated, pairing every path with its key
    up front is a second materialisation of the entire set, and the sidecar can exhaust
    its allocation building it before the phase's deadline gate runs even once -- so the
    final cycle loses everything since the prior one. ``BackupSet.data`` is therefore a
    lazy, re-iterable view: the pairs are produced on demand, the gate is reached with the
    later ones still unbuilt, and a stop names the reached-but-skipped one plus the rest by
    draining the iterator rather than slicing a list that was never built.

    The proof: with a deadline already in the past, only the FIRST pair is opened (the gate
    fires the moment it is checked), and every remaining transcript is still named by
    name -- which can only happen if the remainder is drained lazily, not sliced off a
    prebuilt list.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    for i in range(4):
        _transcript(settings, b"more\n", stem=f"dashboard_cust-{200 + i}")

    expected_names = {
        f"{STEM}{keys.TRANSCRIPT_SUFFIX}",
        *(f"dashboard_cust-{200 + i}{keys.TRANSCRIPT_SUFFIX}" for i in range(4)),
    }
    plan = backup_mod.objects_to_back_up(settings)
    try:
        # data is a lazy view, not a built list, and iterating it twice yields the same
        # pairs afresh (a one-shot iterator would come back empty the second time).
        assert not isinstance(plan.data, list)
        first = [key for key, _p in plan.data]
        second = [key for key, _p in plan.data]
        assert first == second
        assert len(first) == 5

        opened: list[str] = []
        real_open = backup_mod.open_snapshot

        def _spy(path, *, root):
            opened.append(path.name)
            return real_open(path, root=root)

        backup_mod.open_snapshot = _spy
        try:
            result = backup_mod.CycleResult()
            backup_mod._upload_phase(
                plan.data,
                settings=settings,
                store=_Recorder(),
                state={},
                result=result,
                committed_index={},
                deadline=time.monotonic() - 1,
            )
        finally:
            backup_mod.open_snapshot = real_open
    finally:
        plan.close_authority()

    # Exactly one pair was opened before the gate fired; the other four were never
    # opened, yet all five are named -- the remainder came from draining the iterator.
    assert len(opened) == 1
    assert {name for name, _why in result.refused} == expected_names


def test_hashing_an_object_checks_the_deadline_before_it_starts(tmp_path):
    """A content key is a digest of the whole object, and hashing it is unbounded work.

    On the final cycle that read runs inside the drain window, so a large tree hashed with no
    deadline check could spend the window here -- before the PUT gate downstream ever runs --
    and the process would be SIGKILLed mid-hash with nothing said. So the per-object budget is
    checked BEFORE the first chunk: a deadline already past means the hash does not start and
    the deadline outcome is raised. It fails if hashing begins regardless of the window.
    """
    settings = _settings(tmp_path)
    path = _transcript(settings, b"a turn\n")
    snapshot = backup_mod.open_snapshot(path, root=settings.data_home)
    assert snapshot is not None
    try:
        with pytest.raises(backup_mod._DigestDeadlineExceeded):
            # A deadline already in the past: no budget for even the first chunk.
            backup_mod._digest_of(snapshot, deadline=time.monotonic() - 1)
    finally:
        snapshot.close()


def test_hashing_a_large_object_aborts_between_chunks_on_a_fake_clock(tmp_path):
    """The window can run out DURING a hash, not only before it, so the check is per-chunk.

    A single object larger than one chunk is hashed in a loop, and a clock that advances past
    the budget partway through must stop the hash with the deadline outcome rather than read
    to the end. Driven by a fake ``time.monotonic`` that is inside the window on the first
    check and past it on the next, so the abort is the between-chunks guard, not the
    before-start one. It fails if the hash reads the whole object regardless of the clock.
    """
    settings = _settings(tmp_path)
    # Two chunks' worth, so the loop checks the deadline more than once.
    payload = b"x" * (backup_mod._DIGEST_CHUNK_BYTES + 1)
    path = _transcript(settings, payload)
    snapshot = backup_mod.open_snapshot(path, root=settings.data_home)
    assert snapshot is not None

    stop = 1000.0
    deadline = stop + cfg.SIDECAR_DRAIN_SECS
    # First check (before the first chunk) is well inside the window; the next check (before
    # the second chunk) is past it, so the hash aborts between chunks.
    clock = iter([stop, deadline + 1.0, deadline + 1.0, deadline + 1.0])
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(backup_mod, "time", _ClockProxy(lambda: next(clock)))
            with pytest.raises(backup_mod._DigestDeadlineExceeded):
                backup_mod._digest_of(snapshot, deadline=deadline)
    finally:
        snapshot.close()


def test_a_hash_that_overruns_the_window_names_the_object_rather_than_uploading_it(tmp_path):
    """End to end: a final-cycle hash overrun is a named refusal, not a lost upload.

    The upload phase hashes each object to derive its content key. When that hashing cannot
    fit the window, the object and the rest of the iterator are the unreached remainder --
    recorded refused by name, nothing uploaded -- exactly as a PUT that cannot fit the window
    is. Driven with a deadline already past so the first object's hash does not start; the
    object is refused and the store never saw a PUT.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    store = _Recorder()
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    try:
        backup_mod._upload_phase(
            plan.data,
            settings=settings,
            store=store,
            state={},
            result=result,
            committed_index={},
            deadline=time.monotonic() - 1,
        )
    finally:
        plan.close_authority()

    assert [name for name, _why in result.refused] == [f"{STEM}{keys.TRANSCRIPT_SUFFIX}"]
    assert "hashing" in result.refused[0][1]
    # Nothing was uploaded: the hash never completed, so no blob PUT was attempted.
    assert store.puts == []


def test_a_stop_names_a_bounded_remainder_rather_than_draining_the_archive(tmp_path):
    """A stop mid-cycle must not walk the whole unreached tail to name it.

    The gate is reached over a lazy iterator whose end is the archive tree, and retention-off
    lets that tree grow without bound. Draining it to name every unreached object rebuilds the
    whole inventory in a list at the one moment the cycle is stopping -- the unbounded-retention
    hazard the streamed enumeration removes from the walk, reappearing on the stop path. So the
    gate names at most ``_UNREACHED_SAMPLE_CAP`` objects and then records one count-free
    remainder marker, leaving the rest of the iterator unwalked.

    The proof feeds an iterator far longer than the cap through a spy that counts how many
    pairs are pulled, with a stop that fires immediately. A draining gate pulls every pair; a
    bounded gate pulls no more than the cap (plus the one lookahead that trips the marker). The
    refusal record is asserted to hold the cap's worth of names plus the marker, and the pull
    count is asserted to stay at the cap boundary rather than reaching the iterator's end.
    """
    settings = _settings(tmp_path)
    cap = backup_mod._UNREACHED_SAMPLE_CAP
    total = cap * 4  # far past the cap, so a drain is unmistakable in the pull count

    pulled = 0

    def _pairs():
        nonlocal pulled
        for i in range(total):
            pulled += 1
            yield (f"key-{i}", tmp_path / f"seg-{i:05d}{keys.TRANSCRIPT_SUFFIX}")

    result = backup_mod.CycleResult()
    backup_mod._upload_phase(
        _pairs(),
        settings=settings,
        store=_Recorder(),
        state={},
        result=result,
        committed_index={},
        yield_when=lambda: True,  # the stop is already up: the first pair trips the gate
    )

    names = [name for name, _why in result.refused]
    # cap real names + exactly one count-free remainder marker, never the whole tail.
    assert len(names) == cap + 1
    assert names[-1] == backup_mod._UNREACHED_REMAINDER_NAME
    assert backup_mod._UNREACHED_REMAINDER_NAME not in names[:-1]
    # The iterator was pulled only up to the cap boundary (cap names, then one lookahead
    # that trips the marker) -- not drained to its end. This is what fails if the gate goes
    # back to naming the remainder by draining ``it``.
    assert pulled <= cap + 1
    assert pulled < total


def test_the_archive_tree_is_streamed_so_its_inventory_is_never_held_in_a_list(tmp_path):
    """The archive walk is driven ONE step at a time by the consumer, never exhausted first.

    Rotation is free to nest and retention-off lets the archive grow without bound, so a
    walk that appended every path into a ``found`` list before anything streamed could
    exhaust the sidecar's allocation before the upload phase's deadline gate ran even once
    -- the final cycle losing everything since the prior one. Streaming is what lets the
    gate stop mid-walk.

    The proof spies on ``os.fwalk`` and records which archive directories it has VISITED by
    the time the consumer has pulled the first archived path and stopped. A streamed walk
    has visited only the directories needed to reach that first file; an eager walk that
    built the whole ``found`` list first would have visited EVERY directory in the tree
    before the consumer saw a single path. The assertion is that at least one directory is
    still unvisited when the consumer stops -- which a prebuilt list cannot satisfy.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    # Several sibling directories, each with a segment, so a streamed walk reaching the
    # first file leaves later sibling directories unvisited while an eager list does not.
    seg_dirs = []
    for month in ("07", "08", "09", "10", "11"):
        d = settings.archive_dir / "2026" / month
        d.mkdir(parents=True)
        (d / f"seg-{month}.jsonl").write_bytes(b"older\n")
        seg_dirs.append(d)

    visited: list[str] = []
    real_fwalk = os.fwalk

    def spy_fwalk(*args, **kwargs):
        for entry in real_fwalk(*args, **kwargs):
            parent = entry[0]
            visited.append(str(parent))
            yield entry

    plan = backup_mod.objects_to_back_up(settings)
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(backup_mod.os, "fwalk", spy_fwalk)
            it = iter(plan.data)
            # Pull the live head, then the FIRST archived path, then stop.
            pulled = [next(it)]
            while not pulled[-1][1].name.startswith("seg-"):
                pulled.append(next(it))
            visited_at_first_archive = list(visited)
            it.close()  # type: ignore[attr-defined]  # release the walk's descriptor
    finally:
        plan.close_authority()

    # The walk visited only the directories on the way to the first segment; at least one
    # sibling archive directory is still unvisited. An eager ``found`` list would have
    # walked the whole tree (every directory visited) before yielding the first path.
    all_dir_count = 1 + 1 + len(seg_dirs)  # archive root + "2026" + the month dirs
    assert len(visited_at_first_archive) < all_dir_count, (
        f"the walk visited {visited_at_first_archive} before the consumer pulled one "
        "archived path -- the inventory was materialised rather than streamed"
    )
    assert not isinstance(plan.data, list)


def test_the_archive_refusal_record_is_bounded_to_a_sample_and_a_count(tmp_path):
    """A pathological tree's refusals are sampled, not held one entry per fault.

    The streamed enumeration removes the unbounded FILE inventory; the walk's REFUSALS -- a
    directory it could not list, a linked subtree it dropped -- are the other per-archive
    collection and are bounded the same way, to a capped sample plus a true count. The
    cycle's decisions read whether there were ANY refusals, not their identity, so the
    bound changes no decision while it stops a pathological tree from retaining one entry
    per fault.
    """
    cap = backup_mod._RESULT_SAMPLE_CAP
    sink = backup_mod._ArchiveWalkSink()
    for i in range(cap + 10):
        sink.cannot_reach((f"/archive/linked-{i}", "a link"))
    assert len(sink.unreachable) == cap
    assert sink.unreachable_count == cap + 10
    # A non-empty sample still makes the cycle incomplete: the withhold decision keys on
    # presence, which the sample preserves.
    assert sink.unreachable


def test_the_lifetime_state_map_does_not_grow_with_the_archive(tmp_path):
    """Immutable archive keys are evicted oldest-first AS THEY ARE INSERTED, so *state*
    never holds more than the cap's worth at any moment -- mid-cycle or after.

    The map exists to skip re-uploading an unchanged object, and for the flat, bounded live
    set it holds every entry. The archive is the unbounded one -- rotation nests it,
    retention-off never prunes it -- so one entry per segment would grow the map for the
    process's whole life. Archive segments are immutable, so a dropped entry costs one
    re-upload of identical bytes; that makes evict-oldest safe. Live and authority entries
    are never counted against the cap or dropped.

    The proof inserts a live and an authority key, then records archive keys ONE AT A TIME
    through the same helper the upload path uses, asserting after EVERY insertion that the
    archive population never exceeds the cap -- so the peak is bounded, not just the
    post-cycle total. It then checks the oldest archive keys were the ones evicted while the
    newest, the live, and the authority key survive. It fails if the enforcement moves back
    to a post-hoc sweep (an intermediate assertion trips) or counts the wrong keys.
    """
    settings = _settings(tmp_path)
    cap = backup_mod._ARCHIVE_STATE_CAP
    fp = backup_mod.Fingerprint(inode=1, size=1, mtime_ns=1)

    live_key = keys.blob_key(settings, "a" * 64)
    authority_key = keys.authority_generation_key(
        settings, keys.new_generation_id(), keys.AUTHORITY_NAMES[0]
    )
    state = backup_mod.DurableState()
    # Live and authority entries go in with is_archive False (the default): they are the
    # bounded flat set, never counted against the cap or evicted.
    backup_mod._record_durable(settings, state, live_key, fp)
    backup_mod._record_durable(settings, state, authority_key, fp)

    # Archive segments are content-addressed blobs now, indistinguishable from a live
    # transcript blob by key -- so the caller marks them is_archive=True and the FIFO on
    # DurableState is the only record of which keys are archive. Each insertion goes through
    # the production helper, and the archive population is checked after EACH one via the
    # FIFO length, cheaply. The keys are distinct digests in a known order so the oldest is
    # well defined.
    archive_keys = [keys.blob_key(settings, f"{i:064x}") for i in range(cap + 25)]
    for key in archive_keys:
        backup_mod._record_durable(settings, state, key, fp, is_archive=True)
        assert len(state.archive_order) <= cap, "the archive population must never exceed the cap"

    assert len(state.archive_order) == cap, "the FIFO must track exactly the retained keys"
    # The 25 oldest archive keys are gone; the newest survive.
    assert archive_keys[0] not in state
    assert archive_keys[-1] in state
    # Live and authority entries are untouched, whatever the archive did.
    assert live_key in state and authority_key in state


def test_re_recording_an_archive_key_evicts_nothing(tmp_path):
    """A re-upload of a segment already in *state* changes no count and drops nothing.

    An archived segment is immutable, so re-recording its key (a re-upload of identical
    bytes after the entry was, say, never evicted) must not be read as growth and must not
    push an unrelated oldest entry out. The per-insertion cap keys on whether the insertion
    is NEW, so a repeat at the cap is a no-op. It fails if a re-record is counted as an
    insertion and evicts a still-wanted key.
    """
    settings = _settings(tmp_path)
    fp = backup_mod.Fingerprint(inode=1, size=1, mtime_ns=1)
    state = backup_mod.DurableState()
    archive_keys = [
        keys.blob_key(settings, f"{i:064x}") for i in range(backup_mod._ARCHIVE_STATE_CAP)
    ]
    first = archive_keys[0]
    for key in archive_keys:
        backup_mod._record_durable(settings, state, key, fp, is_archive=True)
    assert first in state
    # Re-record the oldest key at exactly the cap: no new identity, so nothing is evicted.
    backup_mod._record_durable(settings, state, first, fp, is_archive=True)
    assert first in state
    assert len(state.archive_order) == backup_mod._ARCHIVE_STATE_CAP


def test_a_changing_live_transcript_does_not_grow_the_state_map(tmp_path):
    """A live transcript's superseded content key is evicted, so *state* stays bounded.

    A changing live transcript takes a FRESH content-addressed key every cycle, so without
    eviction the superseded version's blob key would stay in the lifetime *state* map for the
    process's whole life -- one entry per version per changing session, unbounded growth with
    no in-process recovery. The eviction drops a stem's prior blob key as the new one is
    recorded, so a transcript that changes across many cycles leaves exactly ONE live-blob
    entry for its stem. This drives several cycles on one slot with growing content and
    asserts the state map holds one blob for it, not one per cycle.

    It fails if the eviction is removed: each cycle's new digest would leave its predecessor
    behind and the live-blob count would climb with the number of cycles.
    """
    settings = _settings(tmp_path)
    path = _transcript(settings, b"turn 1\n")
    store = _Recorder()
    state = backup_mod.DurableState()

    blob_prefix = keys.full_key(settings, keys.NAMESPACE + keys.BLOB_PREFIX)
    for i in range(2, 7):
        backup_mod.run_cycle(settings, store, state=state)
        path.write_bytes(f"turn {i}\n".encode())  # the conversation gains a turn each cycle

    backup_mod.run_cycle(settings, store, state=state)

    # Exactly one live-transcript blob key is retained in state for this one stem, not one
    # per cycle -- the superseded versions were evicted as the new ones were recorded.
    live_blob_keys = [k for k in state if k.startswith(blob_prefix)]
    assert len(live_blob_keys) == 1, f"state kept {len(live_blob_keys)} live blobs, expected 1"
    assert state.live_blobs[STEM] == live_blob_keys[0]


def test_the_result_upload_tallies_are_a_bounded_sample_plus_a_true_count():
    """uploaded/unchanged keep a capped sample of names but count every one.

    The archive puts one key per segment through these lists every cycle, so an unbounded
    list would hold the whole inventory for the log's sake. The sample is capped and the
    count stays exact -- the summary reads the count, and nothing reads the identity of an
    uploaded key to decide anything. It fails if the recording stops capping (sample grows
    past the cap) or stops counting (count tracks the truncated sample).
    """
    cap = backup_mod._RESULT_SAMPLE_CAP
    result = backup_mod.CycleResult()
    for i in range(cap + 40):
        result.record_uploaded(f"data/sessions/archive/seg-{i}.jsonl")
    assert len(result.uploaded) == cap, "the sample must be capped"
    assert result.uploaded_count == cap + 40, "the count must stay exact past the cap"
    assert f"{cap + 40} uploaded" in result.summary()


def test_the_refusal_record_is_a_bounded_length_capped_sample_plus_a_true_count():
    """Upload failures are recorded like uploads: a capped, LENGTH-bounded sample plus a count.

    On a pathological run every archived segment can fail its upload, and each failure carries
    an exception string that can itself be arbitrarily long. An entry per failure would retain
    the whole inventory on the FAILURE path -- the same unbounded-retention hazard the streamed
    enumeration removes from the success path. So the sample is capped in number AND each
    reason is truncated, while the count stays exact and the cycle stays incomplete. It fails
    if the recorder stops capping the count of entries, stops truncating the reason, or lets
    the count track the truncated sample.
    """
    cap = backup_mod._RESULT_SAMPLE_CAP
    reason_cap = backup_mod._REFUSAL_REASON_MAX_CHARS
    result = backup_mod.CycleResult()
    huge_reason = "x" * (reason_cap * 5)
    for i in range(cap + 30):
        result.record_refused(f"data/sessions/archive/seg-{i}.jsonl", huge_reason)

    assert len(result.refused) == cap, "the refusal sample must be capped in number"
    assert result.refused_count == cap + 30, "the count must stay exact past the cap"
    # Each sampled reason is length-bounded, so one huge exception cannot blow the record up.
    assert all(len(reason) <= reason_cap + len("… (truncated)") for _n, reason in result.refused)
    # A non-empty sample still makes the cycle incomplete, and the summary reads the count.
    assert not result.complete
    assert f"{cap + 30} refused" in result.summary()


def test_the_gone_and_ceiling_sibling_records_are_bounded_too():
    """Every per-object list over the same population is capped, not just four of them.

    ``gone``, ``above_ceiling`` and ``gone_undurable`` sit over the SAME object population as
    ``uploaded``/``unchanged``/``refused``/``unreachable`` -- one entry per object, including
    every archived segment -- so leaving them unbounded retains the whole inventory through a
    different field. Each is a capped sample plus an exact count, and ``gone_undurable`` feeds
    ``gone_referenced`` whose count (not its retained sample) drives the withhold decision. It
    fails if any of the three siblings stops capping or stops counting.
    """
    cap = backup_mod._RESULT_SAMPLE_CAP
    result = backup_mod.CycleResult()
    for i in range(cap + 15):
        result.record_gone(f"seg-{i}.jsonl")
        result.record_above_ceiling(f"data/sessions/archive/seg-{i}.jsonl")
        result.record_gone_undurable(f"seg-{i}.jsonl", f"key-{i}")

    assert len(result.gone) == cap and result.gone_count == cap + 15
    assert len(result.above_ceiling) == cap and result.above_ceiling_count == cap + 15
    assert len(result.gone_undurable) == cap and result.gone_undurable_count == cap + 15
    assert f"{cap + 15} gone" in result.summary()

    # gone_referenced is likewise bounded, and its COUNT is what the withhold decision reads.
    for i in range(cap + 5):
        result.record_gone_referenced(f"seg-{i}.jsonl")
    assert len(result.gone_referenced) == cap and result.gone_referenced_count == cap + 5
    assert not result.complete, "a non-zero gone_referenced_count must make the cycle incomplete"


def test_every_sampled_identifier_is_length_bounded():
    """A sampled object key or file name is truncated, so one deep archive path cannot bloat it.

    The sample caps bound how MANY identifiers are retained, but an agent writes transcripts
    under arbitrarily deep, arbitrarily long archive paths, so an unbounded-length identifier
    per entry would exhaust the sidecar through a different field than the capped count. Every
    retention point that keeps a name or key truncates it to ``_IDENTIFIER_MAX_CHARS``. This
    feeds one pathologically long walk-built path through each record method and the archive
    sink and asserts the retained string is bounded, while the count still rises.

    It fails if any retention point stops truncating its identifier: the long path is kept
    whole and the per-entry memory is unbounded again.
    """
    id_cap = backup_mod._IDENTIFIER_MAX_CHARS
    bound = id_cap + len("… (truncated)")
    huge = "data/sessions/archive/" + "d/" * 5000 + "seg.jsonl"  # a deeply nested walk path
    assert len(huge) > bound, "precondition: the test path must exceed the cap"

    result = backup_mod.CycleResult()
    result.record_uploaded(huge)
    result.record_unchanged(huge)
    result.record_refused(huge, "boom")
    result.record_unreachable(huge, "boom")
    result.record_gone(huge)
    result.record_above_ceiling(huge)
    result.record_gone_undurable(huge, huge)
    result.record_gone_referenced(huge)

    assert all(len(k) <= bound for k in result.uploaded)
    assert all(len(k) <= bound for k in result.unchanged)
    assert all(len(n) <= bound for n, _r in result.refused)
    assert all(len(n) <= bound for n, _r in result.unreachable)
    assert all(len(n) <= bound for n in result.gone)
    assert all(len(k) <= bound for k in result.above_ceiling)
    assert all(len(n) <= bound and len(k) <= bound for n, k in result.gone_undurable)
    assert all(len(n) <= bound for n in result.gone_referenced)

    # The archive-walk sink and the fold path that merges it into the result are retention
    # points too, so both must bound the identifier.
    sink = backup_mod._ArchiveWalkSink()
    sink.refuse((huge, "boom"))
    sink.cannot_reach((huge, "boom"))
    assert all(len(n) <= bound for n, _r in sink.refused)
    assert all(len(n) <= bound for n, _r in sink.unreachable)
    folded = backup_mod.CycleResult()
    folded.fold_archive_refusals(sink)
    assert all(len(n) <= bound for n, _r in folded.refused)
    assert all(len(n) <= bound for n, _r in folded.unreachable)


def test_every_sampled_reason_is_length_bounded():
    """A sampled refusal/unreachable REASON is truncated, not just its identifier.

    A reason is an interpolated ``OSError`` string that carries a near-PATH_MAX filename, so
    a bounded sample that keeps the reason whole still retains unbounded bytes per row -- the
    archive sink in particular can sample thousands of errors, each a long reason. Every
    retention point that keeps a reason truncates it to ``_REFUSAL_REASON_MAX_CHARS``: the
    ``CycleResult`` record methods, the archive-walk sink, and the fold that merges the sink.

    It fails if the sink (or any retention point) stops truncating its reason -- the regression
    GPT flagged, where ``_bounded_identifier`` was applied to the name but the reason was kept
    raw at ``_ArchiveWalkSink.refuse`` / ``.cannot_reach``.
    """
    reason_cap = backup_mod._REFUSAL_REASON_MAX_CHARS
    bound = reason_cap + len("… (truncated)")
    name = "data/sessions/archive/seg.jsonl"
    huge_reason = "[Errno 13] Permission denied: '" + "x" * 5000 + "'"  # a near-PATH_MAX OSError
    assert len(huge_reason) > bound, "precondition: the test reason must exceed the cap"

    result = backup_mod.CycleResult()
    result.record_refused(name, huge_reason)
    result.record_unreachable(name, huge_reason)
    assert all(len(r) <= bound for _n, r in result.refused)
    assert all(len(r) <= bound for _n, r in result.unreachable)

    sink = backup_mod._ArchiveWalkSink()
    sink.refuse((name, huge_reason))
    sink.cannot_reach((name, huge_reason))
    assert all(len(r) <= bound for _n, r in sink.refused)
    assert all(len(r) <= bound for _n, r in sink.unreachable)

    folded = backup_mod.CycleResult()
    folded.fold_archive_refusals(sink)
    assert all(len(r) <= bound for _n, r in folded.refused)
    assert all(len(r) <= bound for _n, r in folded.unreachable)


def test_a_deadline_with_room_left_uploads_normally(tmp_path):
    """The bound must not cost an ordinary drain the objects it had time for."""
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    store = _Recorder()

    result = backup_mod.run_cycle(settings, store, state={}, deadline=time.monotonic() + 3600)

    assert result.refused == []
    assert _blob_key(settings, b"a turn\n") in store.objects


def test_an_interval_cycle_has_no_deadline_because_it_has_a_next_interval(tmp_path):
    """Only the cycle running inside the drain window is bounded."""
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    store = _Recorder()

    result = backup_mod.run_cycle(settings, store, state={})

    assert result.refused == []


def test_the_stop_timeout_leaves_room_for_the_reap_after_the_last_window():
    """Draining is signal, wait, sweep and reap -- not only the children's own time."""
    from container.common import config as cfg

    windows = cfg.FRONT_DRAIN_SECS + cfg.BACKEND_DRAIN_SECS + cfg.SIDECAR_DRAIN_SECS

    assert cfg.TASK_STOP_TIMEOUT_SECS >= windows + cfg.TEARDOWN_REAP_MARGIN_SECS
