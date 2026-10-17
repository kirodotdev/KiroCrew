"""The four ways the first version of this pair claimed durability it did not have.

Each test here exists because a review found a path where the writer reported success,
or kept reporting progress, while a task replacement would still have lost the
conversation. They are grouped in one file because they are one theme: the difference
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
"""

from __future__ import annotations

import errno
import json
import os
import pathlib
import shutil
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from container.common import config as cfg
from container.common import keys, objects, statefile
from container.front import transcript as front_transcript
from container.sidecar import __main__ as sidecar_main
from container.sidecar import backup as backup_mod
from container.sidecar import generation as generation_mod
from container.sidecar import restore as restore_mod
from container.sidecar.store import ObjectAbsent

from ._settings_helper import make_settings

STEM = "dashboard_cust-91"

#: The SLOT KEY the backend's index names this conversation by. The transcript file carries
#: a ``dashboard_`` prefix on top of it, and the two are asserted equal through the function
#: that actually names the file rather than by spelling the prefix twice -- comparing the two
#: namespaces directly is a defect this suite once agreed with.
SLOT_KEY = "cust-91"
assert (
    front_transcript.transcript_stem(SLOT_KEY) == STEM
), "the slot key and the transcript stem must be related by the production mapping"


class _ClockProxy:
    """Stand in for the ``time`` module with a controlled ``monotonic`` only (GPT F2).

    ``backup.py`` does ``import time`` and calls ``time.monotonic()``, so patching
    ``backup_mod.time.monotonic`` would mutate the stdlib ``time`` SINGLETON -- every other
    xdist worker thread reading ``time.monotonic()`` would then receive this test's finite
    synthetic sequence (and run off its end) instead of real time. Patching the module-local
    binding ``backup_mod.time`` with this proxy instead confines the override to the module
    under test: ``monotonic`` returns the test's value and every other attribute delegates to
    the real module, so ``time.sleep`` and friends keep working for everyone else.

    Pass a zero-arg callable (``lambda: next(clock)`` for a scripted sequence, or
    ``lambda: now`` for a frozen reading); it is called on each ``monotonic()``.
    """

    def __init__(self, monotonic: "Callable[[], float]") -> None:
        self._monotonic = monotonic

    def monotonic(self) -> float:
        return self._monotonic()

    def __getattr__(self, name: str):
        return getattr(time, name)


def _settings(tmp_path: Path, *, interval: int = 60):
    s = make_settings(tmp_path, crew="crew-91", prefix="crews")
    for name in keys.AUTHORITY_NAMES:
        (s.config_dir / name).write_bytes(b"{}")
    return s.__class__(**{**s.__dict__, "backup_interval_secs": interval})


def _transcript(settings, payload: bytes, stem: str = STEM) -> Path:
    path = settings.sessions_dir / f"{stem}{keys.TRANSCRIPT_SUFFIX}"
    path.write_bytes(payload)
    return path


def _blob_key(settings, payload: bytes) -> str:
    """The content-addressed key the writer stores *payload* under: ``data/blob/<sha256>``."""
    import hashlib as _hashlib

    return keys.blob_key(settings, _hashlib.sha256(payload).hexdigest())


class _Recorder:
    """Accepts every put, remembers bytes, and counts cycles by their first key.

    Models S3's conditional writes well enough for the generation CAS: each stored key
    carries a monotonic ETag, ``get_etag`` returns it (or ``None`` when absent), and ``put``
    honours ``if_match``/``if_none_match`` by raising :class:`PreconditionFailed` when the
    precondition does not hold -- so a test can stage a concurrent writer and see the stale
    commit rejected.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []
        self.etags: dict[str, str] = {}
        self._etag_seq = 0

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
    ) -> str | None:
        if if_none_match == "*" and key in self.objects:
            raise backup_mod.PreconditionFailed(f"{key} already exists")
        if if_match is not None and self.etags.get(key) != if_match:
            raise backup_mod.PreconditionFailed(f"{key} ETag is not {if_match}")
        self.objects[key] = body.read(size)
        self.puts.append(key)
        self._etag_seq += 1
        self.etags[key] = f'"etag-{self._etag_seq}"'
        # Return the ETag the write LEFT, as S3's PutObject response does: the ownership
        # claim takes its release CAS validator from here, not from a follow-up GET.
        return self.etags[key]

    def get(
        self,
        key: str,
        *,
        limit: int,
        deadline: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> bytes:
        # Serves what was put, so a pointer this store committed on an earlier cycle is
        # visible when the next cycle re-reads it -- the CAS validator round-trip the
        # writer-unique generation protocol depends on.
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
        # Delegates to self.get so a subclass overriding get (to serve or to raise an
        # unreadable pointer) is honoured; the ETag rides alongside, one logical GET.
        return self.get(key, limit=limit), self.etags.get(key)


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


def _authority_keys_in(settings, keyset) -> set[str]:
    """The authority-pair keys present in *keyset*, matched by their ``gen/<id>/<name>`` shape.

    A cycle mints a WRITER-UNIQUE generation id, so the pair's keys are not known in advance
    -- they are ``<prefix>gen/<id>/<name>``. This picks them out of whatever the store holds
    or the cycle withheld, so a test asserts about the pair without predicting the id.
    """
    prefix = keys.generations_prefix(settings)
    names = set(keys.AUTHORITY_NAMES)
    found: set[str] = set()
    for key in keyset:
        if not key.startswith(prefix):
            continue
        rest = key[len(prefix) :]
        gen_id, _, name = rest.partition("/")
        if name in names and keys.is_generation_id(gen_id):
            found.add(key)
    return found


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


# --- 9. the index gets its own room inside the deadline ---------------------------


def test_the_index_is_not_left_to_run_past_the_window(tmp_path):
    """The data phase stops early enough that the authority pair has room to publish.

    A data phase allowed to spend the whole deadline reaches the end with nothing left for
    the index, and those PUTs are then killed mid-request -- publishing one file and not
    the other, which is the torn index the two-phase order exists to avoid.
    """
    reserved = backup_mod._reserve_for_authority(1000.0, 2)

    assert reserved is not None
    assert reserved < 1000.0


def test_an_interval_cycle_reserves_nothing_because_it_has_no_deadline():
    """Reserving against no deadline would invent one."""
    assert backup_mod._reserve_for_authority(None, 2) is None


# --- 11. a bound the transport does not honour is not a bound ---------------------


def test_the_per_object_budget_covers_every_attempt_the_client_may_make():
    """Reserved short, the gate admits an upload the drain window cannot finish.

    The timeout bounds the connect and the read separately, so one attempt can spend it
    twice, and standard mode waits between attempts. A budget that counts one timeout per
    attempt is therefore under the real worst case by more than half, and the object it
    waves through is killed mid-PUT.
    """
    waits = 2 ** (cfg.BACKUP_MAX_ATTEMPTS - 1) - 1
    spendable = cfg.BACKUP_MAX_ATTEMPTS * 2 * cfg.BACKUP_REQUEST_TIMEOUT_SECS + waits

    assert cfg.BACKUP_ATTEMPT_COST_SECS == 2 * cfg.BACKUP_REQUEST_TIMEOUT_SECS
    assert cfg.BACKUP_PER_OBJECT_BUDGET_SECS >= spendable


def test_the_client_spends_only_the_constants_the_budget_is_derived_from():
    """A timeout or an attempt count written into the store would make the budget a guess.

    The KEY NAME is part of the assertion, not decoration, and it is the whole premise the
    budget rests on. Measured against boto3/botocore 1.42.91 by counting the requests a
    client actually sends to a closed port: ``max_attempts: 1`` sends TWO and resolves to
    ``total_max_attempts: 2``, while ``total_max_attempts: 1`` sends ONE; at three they send
    four and three. So the retries spelling buys one attempt more than the budget reserves --
    at one attempt, this gate admits an upload on ten seconds that the transport may spend
    twenty-one on.

    Asserted as source text rather than by building a client, because botocore is absent from
    the runners this suite collects on: a client-building test would SKIP on every one of
    them, which is no guard at all in the only place the drift can land.
    """
    from container.sidecar import store as store_mod

    src = pathlib.Path(store_mod.__file__).read_text(encoding="utf-8")

    assert "connect_timeout=BACKUP_REQUEST_TIMEOUT_SECS" in src
    assert "read_timeout=BACKUP_REQUEST_TIMEOUT_SECS" in src
    assert '"total_max_attempts": BACKUP_MAX_ATTEMPTS' in src
    assert '"max_attempts"' not in src


def test_the_window_still_fits_one_transcript_after_the_authority_reservation():
    """The reservation is only sound if a whole PUT still fits in what it leaves.

    Three authority objects -- the pair plus the generation pointer -- come out of the
    window before the data phase starts. Reserve more than the window can spare and a
    transcript does not fit in the remainder, so the final cycle publishes an index and not
    one conversation, which is a cycle doing nothing while reporting that it ran.
    """
    deadline = time.monotonic() + cfg.SIDECAR_DRAIN_SECS
    reserved = backup_mod._reserve_for_authority(deadline, len(keys.AUTHORITY_NAMES) + 1)

    assert reserved is not None
    assert backup_mod._time_for_one_more(reserved, cfg.BACKUP_PER_OBJECT_BUDGET_SECS)


def test_the_authority_reservation_sets_aside_a_whole_attempt_per_object():
    """One timeout is HALF an attempt, because the connect and the read are bounded apart.

    Reserved at one timeout per object, the index phase starts its last PUT with five
    seconds against an attempt that can spend ten, and the kill lands mid-request -- which
    publishes one file of the pair and not the other, the exact state the two-phase order
    and the generation pointer exist to make unreachable.
    """
    deadline = time.monotonic() + 1000.0
    reserved = backup_mod._reserve_for_authority(deadline, 3)

    assert reserved is not None
    assert deadline - reserved == pytest.approx(3 * cfg.BACKUP_ATTEMPT_COST_SECS)


def test_a_withheld_authority_phase_reserves_no_window_for_puts_it_will_not_make(tmp_path):
    """The reservation must match the phase that RUNS, not the phase that might have.

    When the pointer cannot be read there is no committed slot, so the authority pair is
    withheld this cycle and its PUTs do not happen. Reserving a whole attempt per authority
    object anyway carves that time off the data phase for uploads that never run, shrinking
    what the transcripts get for no gain. So the count handed to the reservation is zero on
    the withheld path and the pair-plus-pointer only when the phase will actually publish.

    The proof spies the count the reservation is called with: a healthy final cycle reserves
    ``len(authority) + 2`` (the pair, the transcript index, and the pointer); the same cycle
    with an unreadable pointer reserves ``0``.
    """
    counts: list[int] = []
    real_reserve = backup_mod._reserve_for_authority

    def _spy(deadline, count):
        counts.append(count)
        return real_reserve(deadline, count)

    # Healthy final cycle: the phase runs, so it reserves the pair, the index, and the pointer.
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(backup_mod, "_reserve_for_authority", _spy)
        backup_mod.run_cycle(settings, _Recorder(), state={}, deadline=time.monotonic() + 3600)
    healthy = counts[-1]
    assert healthy == len(keys.AUTHORITY_NAMES) + 2

    # Same final cycle, pointer unreadable: the pair is withheld, so it reserves nothing.
    settings2 = _settings(tmp_path / "second")
    _transcript(settings2, b"a turn\n")
    pointer = keys.authority_pointer_key(settings2)

    class _PointerUnreadable(_Recorder):
        def get(
            self,
            key: str,
            *,
            limit: int,
            deadline: float | None = None,
            cancel: Callable[[], bool] | None = None,
        ) -> bytes:
            assert key == pointer
            raise RuntimeError("InternalError")

    counts.clear()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(backup_mod, "_reserve_for_authority", _spy)
        with pytest.raises(backup_mod.BackupIncomplete):
            backup_mod.run_cycle(
                settings2, _PointerUnreadable(), state={}, deadline=time.monotonic() + 3600
            )
    assert counts[-1] == 0, "a withheld authority phase must reserve no window"


def test_the_index_phase_will_not_start_a_put_it_has_half_an_attempt_for(tmp_path):
    """Enough for the read alone is not enough: the connect can spend the whole timeout."""
    settings = _settings(tmp_path)
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    try:
        backup_mod._commit_authority(
            plan.authority,
            settings=settings,
            store=_Recorder(),
            state={},
            result=result,
            deadline=time.monotonic() + cfg.BACKUP_ATTEMPT_COST_SECS - 3,
        )
    finally:
        plan.close_authority()

    assert [name for name, _why in result.refused] == [
        key.rsplit("/", 1)[-1] for key, _snapshot in plan.authority
    ]
    assert result.withheld == [key for key, _snapshot in plan.authority]


def test_the_pointer_is_not_started_with_less_than_one_attempt_left(tmp_path):
    """The pointer is the commit, so a kill mid-PUT is the one write nothing can repair."""
    settings = _settings(tmp_path)
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    try:
        backup_mod._commit_generation(
            plan,
            settings=settings,
            store=_Recorder(),
            state={},
            result=result,
            generation_id=plan.generation_id,
            deadline=time.monotonic() + cfg.BACKUP_ATTEMPT_COST_SECS - 3,
        )
    finally:
        plan.close_authority()

    assert [key for key, _why in result.refused] == [keys.authority_pointer_key(settings)]


def test_the_index_phase_stops_rather_than_publishing_half_the_pair(tmp_path):
    """Both files, or neither: a half-new pair disagrees with itself about the slots."""
    settings = _settings(tmp_path)
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    try:
        backup_mod._commit_authority(
            plan.authority,
            settings=settings,
            store=_Recorder(),
            state={},
            result=result,
            deadline=time.monotonic() - 1,
        )
    finally:
        plan.close_authority()

    assert len(result.withheld) == len(keys.AUTHORITY_NAMES)
    assert result.uploaded == []


def test_the_index_upload_is_budgeted_and_cancellable_under_a_deadline(tmp_path):
    """A slow index trickle must not outlast the drain window and lose the newest history.

    The transcript index can grow to the module's entry/byte caps, so the final cycle's
    index PUT is not the short write the old comment claimed. Under a deadline it carries
    the one-attempt budget the sibling pair PUTs and the admission check already use, plus
    the cycle's yield check as a cancel, so a kill mid-upload is prevented rather than
    discovered. With no deadline (an interval cycle) it stays unbounded, like the pointer.
    """

    class _BudgetRecorder(_Recorder):
        def __init__(self) -> None:
            super().__init__()
            self.budgets: dict[str, float | None] = {}
            self.cancels: dict[str, object] = {}

        def put(self, key, body, size, *, budget=None, cancel=None, **kw):
            self.budgets[key] = budget
            self.cancels[key] = cancel
            super().put(key, body, size, **kw)

    settings = _settings(tmp_path)
    gen_id = keys.new_generation_id()
    index_key = keys.transcript_index_key(settings, gen_id)
    sentinel_cancel: Callable[[], bool] = lambda: False  # noqa: E731

    # Under a deadline: budgeted at one attempt and cancellable with the cycle's check.
    store = _BudgetRecorder()
    result = backup_mod.CycleResult()
    backup_mod._commit_transcript_index(
        {STEM: f"gen/{gen_id}/blob/abc"},
        settings=settings,
        store=store,
        state={},
        result=result,
        generation_id=gen_id,
        deadline=time.monotonic() + 3600,
        cancel=sentinel_cancel,
    )
    assert index_key in store.puts, "the index was published"
    assert store.budgets[index_key] == cfg.BACKUP_ATTEMPT_COST_SECS
    assert store.cancels[index_key] is sentinel_cancel

    # No deadline (interval cycle): unbounded, matching the pointer's own rationale.
    store2 = _BudgetRecorder()
    result2 = backup_mod.CycleResult()
    backup_mod._commit_transcript_index(
        {STEM: f"gen/{gen_id}/blob/abc"},
        settings=settings,
        store=store2,
        state={},
        result=result2,
        generation_id=gen_id,
        deadline=None,
        cancel=sentinel_cancel,
    )
    assert store2.budgets[index_key] is None


# --- 10. publication links the inode we wrote, not a name we reopened -------------


def test_publication_links_the_open_descriptor_not_a_reopened_name(tmp_path):
    """Closing the temporary first would publish whatever its name pointed at by then.

    These directories are ones the agent writes in, so a concurrent turn replacing the
    temporary between the close and the link would have its own inode published under the
    target name and receive every later write. Linking the descriptor removes the window.
    """
    target = tmp_path / "published.json"

    assert statefile.link_new(target, b'{"keys": []}', prefix="probe-")
    assert target.read_bytes() == b'{"keys": []}'


def test_publication_refuses_an_existing_target_without_clobbering_it(tmp_path):
    """An existing file is the copy to keep, and the refusal is the filesystem's."""
    target = tmp_path / "published.json"
    target.write_bytes(b"older and better\n")

    assert statefile.link_new(target, b"newer\n", prefix="probe-") is False
    assert target.read_bytes() == b"older and better\n"


def test_publication_leaves_no_temporary_behind(tmp_path):
    """On both paths out: the one that published, and the one that found a file there."""
    target = tmp_path / "published.json"
    statefile.link_new(target, b"first\n", prefix="probe-")
    statefile.link_new(target, b"second\n", prefix="probe-")

    assert [p.name for p in tmp_path.iterdir()] == ["published.json"]


def test_a_cycle_that_holds_one_authority_file_commits_no_generation(tmp_path):
    """A generation is committed holding a WHOLE pair or it is not committed at all.

    A generation committed with one file would assert that a complete publication is one
    file, and every later restore would boot from it and let the backend flush its own empty
    view of the other -- reintroducing, through the pointer, the loss the protocol is here to
    prevent. With no pointer committed the file has nowhere a reader looks either, so it is
    withheld rather than published and the cycle says so; see
    :func:`test_one_authority_file_present_is_refused_rather_than_half_published`.
    """
    settings = _settings(tmp_path)
    (settings.config_dir / keys.AUTHORITY_NAMES[0]).unlink()
    _transcript(settings, b"a turn\n")
    store = _Recorder()

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, store, state={})

    assert keys.authority_pointer_key(settings) not in store.puts
    assert keys.AUTHORITY_NAMES[0] in caught.value.result.gone


def test_a_pointer_that_cannot_be_published_refuses_the_final_cycle(tmp_path):
    """On the final cycle an unpublished pointer is a refusal, because nothing follows it.

    The withheld path's recovery is "the next cycle commits it", and the final cycle has
    no next cycle. Left withheld, the pair is on disk in a slot no pointer names, the
    cycle reports complete, the sidecar exits zero and the supervisor reports a lossless
    stop -- while the replacement boots from the generation the pointer still names, which
    is the older index without the conversations this cycle just uploaded. Silent, and the
    quietest possible shape of the loss this whole protocol exists to prevent.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    pointer = keys.authority_pointer_key(settings)

    class _PointerFails(_Recorder):
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
            if key == pointer:
                raise RuntimeError("AccessDenied")
            super().put(key, body, size)

    store = _PointerFails()

    with pytest.raises(backup_mod.BackupIncomplete):
        backup_mod.run_cycle(settings, store, state={}, deadline=time.monotonic() + 3600)


def test_a_pointer_that_cannot_be_published_only_waits_on_an_interval_cycle(tmp_path):
    """The mirror: an interval cycle withholds, because its next cycle really does follow.

    Refusing here would turn one transient PUT failure into a non-zero exit on a sidecar
    that is going to run again in seconds, and a supervisor that ends the task on it.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    pointer = keys.authority_pointer_key(settings)

    class _PointerFails(_Recorder):
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
            if key == pointer:
                raise RuntimeError("SlowDown")
            super().put(key, body, size)

    store = _PointerFails()

    result = backup_mod.run_cycle(settings, store, state={})

    assert result.complete, "an interval cycle has a next cycle, so it waits rather than fails"
    assert pointer in result.withheld
    assert result.refused == []


def test_a_lost_pointer_put_drops_the_cached_belief_so_a_later_cycle_re_commits(tmp_path):
    """An unconfirmed pointer PUT must not leave a belief that pins the generation.

    The commit skips its PUT when ``state`` already says this key holds this slot's
    fingerprint. So a PUT whose response is lost -- the write may have landed, may not, the
    slot is unknown -- must not leave that fingerprint cached: left in place, every later
    cycle sees ``state.get(key) == fingerprint``, skips the re-PUT, records the key as
    unchanged, and reports a COMPLETE stop while the generation the pointer names is frozen
    behind a belief a failed write installed. The except paths therefore drop the key.

    The proof seeds the pointer key with a stale belief, fails the pointer PUT on an
    interval cycle, and asserts the belief is gone afterwards -- so the next cycle re-attempts
    the commit rather than trusting the cache. It fails if the ``state.pop`` is removed.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    pointer = keys.authority_pointer_key(settings)

    class _PointerFails(_Recorder):
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
            if key == pointer:
                raise RuntimeError("RequestTimeout")  # the response is lost, not a refusal
            super().put(key, body, size)

    # A stale belief from an earlier committed cycle: any fingerprint under the key is
    # enough, because the fix drops whatever is there rather than matching a value.
    state = {pointer: backup_mod.Fingerprint(inode=0, size=1, mtime_ns=0)}
    result = backup_mod.run_cycle(settings, _PointerFails(), state=state)

    assert pointer in result.withheld
    assert pointer not in state, (
        "an unconfirmed pointer PUT must invalidate the cached belief, or a later cycle "
        "skips the re-commit and reports the freeze as a complete stop"
    )


def test_a_concurrent_writer_committing_first_rejects_this_cycles_stale_commit(tmp_path):
    """Two sidecars on one prefix must not clobber each other's committed generation.

    In the task-replacement window the draining old sidecar and the starting new one can both
    write this prefix. Both read the same committed pointer, both pick the other slot, both
    publish a pair into it and both commit the pointer -- and without a guard the second
    overwrites the first's generation. The commit is therefore a compare-and-swap: it reads
    the pointer's ETag and PUTs ``If-Match`` it, so a writer that advanced the pointer since
    this cycle read it defeats this PUT with a 412, and the stale commit is rejected rather
    than winning the race.

    The proof stages a concurrent writer inside the pointer read: `get_with_etag` returns the
    stored ETag but then advances it (as if another sidecar committed) so this cycle's
    conditional PUT carries a stale ``If-Match`` that fails to match. The commit must be
    refused, not landed.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    pointer = keys.authority_pointer_key(settings)

    class _ConcurrentWriterCommitsFirst(_Recorder):
        def __init__(self) -> None:
            super().__init__()
            # A pointer already committed by an earlier generation, so this cycle reads it and
            # its commit takes the If-Match branch rather than the If-None-Match create. The
            # body is a valid whole-pair pointer so the read accepts it rather than refusing it
            # as unusable.
            self._committed = generation_mod.pointer_body(keys.new_generation_id())
            self.objects[pointer] = self._committed
            self.etags[pointer] = '"etag-original"'
            # A committed generation always carries its transcript index (empty here), written
            # before the pointer; the cycle reads it after the pointer, so the fake serves it
            # or the read refuses the cycle as a deleted-while-referenced index before the CAS
            # path this test exercises is ever reached.
            committed_gen = json.loads(self._committed.decode())["generation"]
            self.objects[keys.transcript_index_key(settings, committed_gen)] = b"{}"

        def get_with_etag(
            self,
            key: str,
            *,
            limit: int,
            deadline: float | None = None,
            cancel: Callable[[], bool] | None = None,
        ) -> tuple[bytes, str | None]:
            if key not in self.objects:
                raise ObjectAbsent(key)
            raw = self.objects[key]
            etag = self.etags.get(key)
            if key == pointer:
                # A concurrent writer commits AFTER this read: the stored ETag moves on, so
                # the validator this read returned fails to match at commit time.
                self.etags[pointer] = '"etag-advanced-by-a-concurrent-writer"'
            return raw, etag

    store = _ConcurrentWriterCommitsFirst()
    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, store, state={})

    result = caught.value.result
    assert not result.complete, "a stale commit rejected by CAS must make the cycle incomplete"
    assert any(
        key == pointer for key, _why in result.refused
    ), "the rejected stale commit must be recorded as a refusal, not silently dropped"
    # The concurrent writer's generation stands: our commit did not overwrite the pointer.
    assert store.objects[pointer] == store._committed


def test_a_commit_with_no_cas_validator_fails_closed(tmp_path):
    """A present pointer whose ETag the store could not supply must NOT commit unconditionally.

    The CAS validator is the pointer's ETag read at cycle start. If the pointer exists but the
    store returned no ETag for it, there is no precondition to present -- and committing anyway
    would overwrite a concurrent writer's generation blind, defeating the guard. So the commit
    fails CLOSED: it is refused and the pointer is NOT written. It fails if the commit runs
    unconditionally on a missing validator.

    Exercised directly on ``_commit_generation`` so the fail-closed branch is isolated from the
    rest of the cycle: a committed pointer carrying ``etag=None`` is the missing-validator case.
    """
    settings = _settings(tmp_path)
    plan = backup_mod.objects_to_back_up(settings)
    pointer = keys.authority_pointer_key(settings)
    committed = generation_mod.Pointer(
        generation=keys.new_generation_id(), authority=frozenset(keys.AUTHORITY_NAMES), etag=None
    )
    store = _Recorder()
    result = backup_mod.CycleResult()
    try:
        backup_mod._commit_generation(
            plan,
            settings=settings,
            store=store,
            state={},
            result=result,
            generation_id=plan.generation_id,
            committed=committed,
        )
    finally:
        plan.close_authority()

    assert any(
        key == pointer for key, _why in result.refused
    ), "a missing CAS validator must be refused, not committed blind"
    assert not result.complete
    # The pointer was never written: no unconditional commit happened.
    assert pointer not in store.objects


def test_two_writers_publish_into_distinct_generations_and_neither_clobbers_the_other(tmp_path):
    """The finding's core: two concurrent writers must not commit a mixed authority pair.

    Under a shared two-slot scheme both writers in the task-replacement window target the same
    slot and interleave their PUTs into it, so the committed pair could be a cross-writer tear.
    Writer-unique immutable generation keys remove the shared object: each cycle mints its own
    ``gen/<id>/`` and writes only there, so a second writer's pair lands under a DIFFERENT id
    and cannot overwrite the first's. This proves two independent cycles (standing in for two
    writers) address disjoint generation directories, and neither writes a key the other did.

    It fails if the pair keys stop carrying a per-cycle-unique id -- e.g. a reversion to a
    fixed slot -- because the two cycles' authority keys would then collide.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")

    # Two cycles, each starting from an EMPTY bucket state so each mints a fresh generation
    # rather than skipping as unchanged -- the concurrent-writer window, where neither has
    # seen the other's commit.
    store_a = _Recorder()
    backup_mod.run_cycle(settings, store_a, state={})
    store_b = _Recorder()
    backup_mod.run_cycle(settings, store_b, state={})

    pair_a = _authority_keys_in(settings, store_a.objects)
    pair_b = _authority_keys_in(settings, store_b.objects)
    assert len(pair_a) == len(keys.AUTHORITY_NAMES)
    assert len(pair_b) == len(keys.AUTHORITY_NAMES)
    # Distinct generation directories: the two writers' pairs share no key, so neither
    # overwrote the other's -- the torn-pair hazard the shared slot allowed cannot occur.
    assert pair_a.isdisjoint(pair_b), "two writers wrote the same generation key"
    gen_a = {key.rsplit("/", 2)[-2] for key in pair_a}
    gen_b = {key.rsplit("/", 2)[-2] for key in pair_b}
    assert len(gen_a) == 1 and len(gen_b) == 1, "each cycle's pair is under one generation id"
    assert gen_a.isdisjoint(gen_b), "the two writers minted the same generation id"


def test_a_superseded_task_cannot_commit_over_its_replacement(tmp_path):
    """The incarnation fence: a predecessor the CAS alone would admit is still refused.

    A predecessor task whose cycle began AFTER its replacement committed reads the
    replacement's pointer, so it holds a FRESH ETag and its ``If-Match`` would succeed --
    rolling the committed generation back to the predecessor's older local state. The
    incarnation closes that: the committed pointer carries the replacement's (newer) token,
    and a commit whose own incarnation is older is refused. This stages exactly that -- a
    committed pointer with a NEWER incarnation and a valid ETag -- and drives a commit with an
    OLDER incarnation, asserting it is refused and the pointer is left untouched.

    It fails if the fence is removed: with only the CAS, the fresh ETag passes and the stale
    commit lands.
    """
    settings = _settings(tmp_path)
    plan = backup_mod.objects_to_back_up(settings)
    pointer = keys.authority_pointer_key(settings)
    store = _Recorder()
    # The replacement's committed pointer: a NEWER incarnation, with a live ETag the
    # predecessor's cycle-start read would carry.
    newer = "99999999999999999999-ffffffffffffffff"
    committed_body = generation_mod.pointer_body(keys.new_generation_id(), newer)
    store.objects[pointer] = committed_body
    store.etags[pointer] = '"etag-live"'
    committed = generation_mod.Pointer(
        generation=json.loads(committed_body.decode())["generation"],
        authority=frozenset(keys.AUTHORITY_NAMES),
        incarnation=newer,
        etag='"etag-live"',
    )
    result = backup_mod.CycleResult()
    try:
        backup_mod._commit_generation(
            plan,
            settings=settings,
            store=store,
            state={},
            result=result,
            generation_id=plan.generation_id,
            committed=committed,
            # This task's incarnation is OLDER than the committed one: it was superseded.
            incarnation="00000000000000000001-0000000000000000",
        )
    finally:
        plan.close_authority()

    assert any(
        key == pointer and "superseded" in why for key, why in result.refused
    ), "a superseded task's commit must be refused with a superseded reason"
    # The pointer still carries the replacement's generation, not this task's.
    assert store.objects[pointer] == committed_body


def test_the_incarnation_is_derived_from_the_committed_pointer_not_the_clock(tmp_path):
    """A task's incarnation follows the committed pointer's counter, independent of any clock.

    The GPT finding: a wall-clock incarnation is unsound across hosts -- a replacement on a
    clock BEHIND its predecessor mints an OLDER token and is refused as superseded forever,
    and a single future-dated commit poisons the pointer. The fix derives the counter from
    SHARED STORAGE: ``startup_incarnation`` reads the committed pointer and mints the next
    counter after it. This proves a replacement reading a committed counter N mints N+1 --
    with no reference to the wall clock -- so it is strictly greater than what it read.

    It fails if the incarnation reverts to a clock read: then the counter would not track the
    committed pointer's value.
    """
    settings = _settings(tmp_path)
    store = _Recorder()
    # A committed pointer carrying counter 7.
    committed = "00000000000000000007-abcdefabcdefabcd"
    body = generation_mod.pointer_body(keys.new_generation_id(), committed)
    store.objects[keys.authority_pointer_key(settings)] = body
    store.etags[keys.authority_pointer_key(settings)] = '"etag"'

    minted = backup_mod.startup_incarnation(settings, store)

    # The next counter after 7 is 8, regardless of what any clock reads.
    assert keys.incarnation_counter(minted) == 8
    # And it is strictly greater than the committed one, so this task is never self-refused.
    assert keys.incarnation_counter(minted) > keys.incarnation_counter(committed)


def test_a_replacement_on_a_slower_clock_is_not_refused_as_superseded(tmp_path):
    """The exact harm the finding named: a behind-the-clock replacement must still commit.

    Under the old wall-clock scheme a replacement whose host clock lagged its predecessor's
    minted an OLDER token and the fence refused its every commit permanently -- silent loss of
    all its turns. With a store-derived counter the replacement reads the predecessor's
    committed counter and mints the next one, so it commits regardless of clock skew.

    It fails if a replacement that read a committed counter can mint one not greater than it.
    """
    settings = _settings(tmp_path)
    store = _Recorder()
    # Predecessor committed counter 5 (whatever its host clock was).
    predecessor = "00000000000000000005-1111111111111111"
    body = generation_mod.pointer_body(keys.new_generation_id(), predecessor)
    store.objects[keys.authority_pointer_key(settings)] = body
    store.etags[keys.authority_pointer_key(settings)] = '"etag"'

    # The replacement derives its incarnation from the pointer, not its (lagging) clock.
    replacement = backup_mod.startup_incarnation(settings, store)
    assert keys.incarnation_counter(replacement) > keys.incarnation_counter(predecessor)

    # Driving a real commit with the derived incarnation is NOT fenced out.
    plan = backup_mod.objects_to_back_up(settings)
    committed = generation_mod.Pointer(
        generation=json.loads(body.decode())["generation"],
        authority=frozenset(keys.AUTHORITY_NAMES),
        incarnation=predecessor,
        etag='"etag"',
    )
    result = backup_mod.CycleResult()
    try:
        backup_mod._commit_generation(
            plan,
            settings=settings,
            store=store,
            state={},
            result=result,
            generation_id=plan.generation_id,
            committed=committed,
            incarnation=replacement,
        )
    finally:
        plan.close_authority()
    assert not any("superseded" in why for _k, why in result.refused)


def test_an_equal_counter_different_token_commit_is_refused_as_superseded(tmp_path):
    """GPT F1: two tasks sharing a counter must not roll each other back.

    ``next_incarnation`` is a non-atomic read-plus-one, so two tasks that both start while the
    pointer carries counter N both mint N+1 with different random suffixes. If one commits and
    the other then re-reads a FRESH pointer ETag, a strictly-greater-only fence would admit the
    second's stale commit (equal counters) and roll back the first's history. The fence must
    treat an equal counter with a DIFFERENT token as a supersession: a task that finds a
    different N+1 already committed cannot prove it is the later one and steps aside.

    It fails if the fence compares the counter alone: then the equal-counter commit lands and
    rolls back the committed generation.
    """
    settings = _settings(tmp_path)
    plan = backup_mod.objects_to_back_up(settings)
    # Same counter 3, different suffixes: a DIFFERENT task's N+1 is already committed.
    committed_token = "00000000000000000003-ffffffffffffffff"
    this_token = "00000000000000000003-0000000000000000"
    body = generation_mod.pointer_body(keys.new_generation_id(), committed_token)
    store = _Recorder()
    store.objects[keys.authority_pointer_key(settings)] = body
    store.etags[keys.authority_pointer_key(settings)] = '"etag"'
    committed = generation_mod.Pointer(
        generation=json.loads(body.decode())["generation"],
        authority=frozenset(keys.AUTHORITY_NAMES),
        incarnation=committed_token,
        etag='"etag"',
    )
    result = backup_mod.CycleResult()
    try:
        backup_mod._commit_generation(
            plan,
            settings=settings,
            store=store,
            state={},
            result=result,
            generation_id=plan.generation_id,
            committed=committed,
            incarnation=this_token,
        )
    finally:
        plan.close_authority()
    pointer = keys.authority_pointer_key(settings)
    # Equal counter, different token: refused as superseded, and the committed pointer is
    # left as the other task wrote it (not rolled back to this task's generation).
    assert any(k == pointer and "superseded" in why for k, why in result.refused)
    assert result.superseded, "the fence must flag the result superseded so run_cycle can end"
    assert store.objects[pointer] == body


def test_a_task_recommitting_its_own_token_across_cycles_is_not_superseded(tmp_path):
    """The equal-token case is NOT a supersession: one task's ordinary cycles must commit.

    A task mints its incarnation once and passes the SAME token every cycle, so a later cycle
    sees its OWN token committed. That is the identical token, not a different N+1, so the
    fence does not refuse it -- otherwise a single task could never commit twice.
    """
    settings = _settings(tmp_path)
    own = "00000000000000000003-0000000000000000"
    plan = backup_mod.objects_to_back_up(settings)
    body = generation_mod.pointer_body(keys.new_generation_id(), own)
    store = _Recorder()
    store.objects[keys.authority_pointer_key(settings)] = body
    store.etags[keys.authority_pointer_key(settings)] = '"etag"'
    committed = generation_mod.Pointer(
        generation=json.loads(body.decode())["generation"],
        authority=frozenset(keys.AUTHORITY_NAMES),
        incarnation=own,
        etag='"etag"',
    )
    result = backup_mod.CycleResult()
    try:
        backup_mod._commit_generation(
            plan,
            settings=settings,
            store=store,
            state={},
            result=result,
            generation_id=plan.generation_id,
            committed=committed,
            incarnation=own,
        )
    finally:
        plan.close_authority()
    # Its own token: not superseded, so no superseded refusal.
    assert not any("superseded" in why for _k, why in result.refused)


def test_the_pointer_carries_the_task_incarnation_round_trip(tmp_path):
    """``read_pointer`` reads back the incarnation ``pointer_body`` wrote, so the fence works."""
    settings = _settings(tmp_path)
    token = "01790000000000000000-abcdefabcdefabcd"
    body = generation_mod.pointer_body(_GEN, token)
    pointer = generation_mod.read_pointer(settings, _Bucket({keys.AUTHORITY_POINTER_NAME: body}))

    assert pointer is not None
    assert pointer.incarnation == token
    assert pointer.generation == _GEN


def test_a_pointer_written_before_the_incarnation_field_reads_as_empty(tmp_path):
    """A bucket from a writer that predates the incarnation has no key: read it as "".

    "" never compares as newer than a real token, so a bucket an earlier writer made stays
    committable -- the fence does not wedge a predecessor-free bucket.
    """
    settings = _settings(tmp_path)
    legacy = json.dumps({"generation": _GEN, "authority": sorted(keys.AUTHORITY_NAMES)}).encode()
    pointer = generation_mod.read_pointer(settings, _Bucket({keys.AUTHORITY_POINTER_NAME: legacy}))

    assert pointer is not None
    assert pointer.incarnation == ""


@pytest.mark.parametrize("bad", ["z", "not-a-token", "123", "01790000000000000000", 5, 1.5, None])
def test_a_malformed_incarnation_is_refused_rather_than_freezing_the_fence(tmp_path, bad):
    """A non-empty incarnation that is not a well-formed token must be refused, not trusted.

    Tokens are always ``<20-digit counter>-<16 hex>`` and the fence reads the leading COUNTER
    from them. A non-conforming non-empty value -- a bare ``"z"``, a bad shape, a non-string --
    has no counter to read, and if ``read_pointer`` passed it through the pointer would carry
    a value the fence cannot order, so ``read_pointer`` refuses it by the same shape check that
    guards the generation id. ``read_pointer`` exists to distrust bucket bytes. "" alone is the
    accepted non-token value (back-compat, counter 0), proven by the test above.

    It fails if a malformed incarnation is accepted.
    """
    settings = _settings(tmp_path)
    body = json.dumps(
        {"generation": _GEN, "incarnation": bad, "authority": sorted(keys.AUTHORITY_NAMES)}
    ).encode()
    with pytest.raises(generation_mod.PointerUnusable):
        generation_mod.read_pointer(settings, _Bucket({keys.AUTHORITY_POINTER_NAME: body}))


def test_two_writers_differing_transcript_bytes_take_distinct_blob_keys(tmp_path):
    """The F1 fix for the DATA objects: a stale transcript PUT cannot overwrite a newer one.

    The hazard the finding named is a transcript object: two sidecars overlapping in the
    task-replacement window wrote one mutable ``data/sessions/<stem>`` key, so the old
    writer's PUT could land second and overwrite the replacement's newer bytes, and the new
    writer's own fingerprint then answered already-durable, so no re-upload, no error. Content
    addressing removes the shared key: a transcript's key IS the digest of its bytes, so two
    writers of DIFFERENT bytes take DIFFERENT keys and neither overwrites the other; the
    committed index names whichever digest its cycle recorded. This proves two cycles writing
    different bytes for the same stem land at two distinct blob keys, both present.

    It fails if the transcript key stops being content-addressed -- a reversion to the mutable
    per-stem key would collide the two writes on one key.
    """
    settings = _settings(tmp_path)

    _transcript(settings, b"older bytes\n")
    store = _Recorder()
    backup_mod.run_cycle(settings, store, state={})

    # The replacement serves a newer turn for the SAME slot: different bytes, same stem.
    _transcript(settings, b"newer bytes\n")
    backup_mod.run_cycle(settings, store, state={})

    old_blob = _blob_key(settings, b"older bytes\n")
    new_blob = _blob_key(settings, b"newer bytes\n")
    assert old_blob != new_blob
    # Both are present: the newer write did not overwrite the older at a shared key.
    assert store.objects[old_blob] == b"older bytes\n"
    assert store.objects[new_blob] == b"newer bytes\n"


def test_a_commit_drops_the_superseded_generations_state_entries(tmp_path):
    """The lifetime state map must not gain three permanent entries per committing cycle.

    Each cycle mints a fresh generation id and records its two authority keys and its
    transcript-index key in *state* so an unchanged pair/index is not re-PUT. Those keys are
    per-generation, read only for the CURRENTLY committed generation, so once the pointer
    moves off a generation its three entries are dead. Without eviction the one DurableState
    the sidecar holds for its whole life grows by three every cycle. This drives two
    committing cycles and asserts the first generation's three keys are gone from state after
    the second commits, while the second generation's are present.

    It fails if the superseded generation's entries are left in state (unbounded growth).
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    store = _Recorder()
    state = backup_mod.DurableState()

    backup_mod.run_cycle(settings, store, state=state)
    pointer_body = store.objects[keys.authority_pointer_key(settings)]
    gen_one = json.loads(pointer_body.decode())["generation"]

    # Change the pair so a NEW generation is committed (an unchanged pair would be skipped).
    (settings.config_dir / "open_slots.json").write_bytes(b'{"keys": ["cust-new"]}')
    backup_mod.run_cycle(settings, store, state=state)
    gen_two = json.loads(store.objects[keys.authority_pointer_key(settings)].decode())["generation"]
    assert gen_one != gen_two

    # The superseded generation's control-plane keys are evicted from the lifetime map.
    for name in keys.AUTHORITY_NAMES:
        assert keys.authority_generation_key(settings, gen_one, name) not in state
    assert keys.transcript_index_key(settings, gen_one) not in state
    # The newly committed generation's keys are retained for the next cycle's unchanged check.
    for name in keys.AUTHORITY_NAMES:
        assert keys.authority_generation_key(settings, gen_two, name) in state
    assert keys.transcript_index_key(settings, gen_two) in state


def test_a_pointer_that_cannot_be_read_refuses_the_final_cycle(tmp_path):
    """The read twin of the pin above: an unreadable pointer is the same loss as an unsent one.

    A transient GetObject failure on the pointer is not a permanent code, so it arrives as
    PointerUnusable rather than StoreUnusable and does not end the process. Without a
    committed slot the cycle cannot choose where to write, so the pair is withheld -- and on
    the final cycle that withholding is the whole loss: the pointer still names the older
    generation, the pair this drain flush produced is never published, and the task reports
    a clean stop. The transcripts are in the bucket and nothing references them.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    pointer = keys.authority_pointer_key(settings)

    class _PointerUnreadable(_Recorder):
        def get(
            self,
            key: str,
            *,
            limit: int,
            deadline: float | None = None,
            cancel: Callable[[], bool] | None = None,
        ) -> bytes:
            assert key == pointer, "the cycle reads nothing but the pointer"
            raise RuntimeError("InternalError")

    with pytest.raises(backup_mod.BackupIncomplete):
        backup_mod.run_cycle(
            settings, _PointerUnreadable(), state={}, deadline=time.monotonic() + 3600
        )


def test_a_pointer_that_cannot_be_read_only_waits_on_an_interval_cycle(tmp_path):
    """The mirror again: an interval cycle re-reads the pointer next time, so it waits."""
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    pointer = keys.authority_pointer_key(settings)

    class _PointerUnreadable(_Recorder):
        def get(
            self,
            key: str,
            *,
            limit: int,
            deadline: float | None = None,
            cancel: Callable[[], bool] | None = None,
        ) -> bytes:
            assert key == pointer, "the cycle reads nothing but the pointer"
            raise RuntimeError("InternalError")

    result = backup_mod.run_cycle(settings, _PointerUnreadable(), state={})

    assert result.complete, "an interval cycle re-reads the pointer, so it waits rather than fails"
    assert result.refused == []
    assert set(result.withheld) == _authority_keys_in(settings, result.withheld)
    assert len(result.withheld) == len(keys.AUTHORITY_NAMES)


def test_the_collection_gate_declines_off_linux_not_merely_off_posix():
    """Read as source text, because the branch it pins cannot be taken on this host.

    macOS is POSIX and has no ``/proc``, so a gate written against ``os.name`` admits a
    platform where publication's descriptor path does not exist and every test reaching it
    fails on the platform rather than on the code. The condition is the thing being
    pinned, and only its source can say what it tests on a host that never takes it.
    """
    conftest = pathlib.Path(__file__).parent / "conftest.py"
    lines = conftest.read_text(encoding="utf-8").splitlines()
    deps_branch = next(i for i, line in enumerate(lines) if line.startswith("elif _missing_image"))
    gate = next(lines[i] for i in range(deps_branch - 1, -1, -1) if lines[i].startswith("if "))
    assert "sys.platform" in gate and '"linux"' in gate, gate
    assert "os.name" not in gate, gate


def test_a_committed_generation_wins_over_legacy_keys_in_the_same_bucket(tmp_path):
    """A bucket holding BOTH layouts: the pointer decides, and the legacy keys are older.

    This is what a bucket looks like the moment the protocol is adopted -- generation 0 is
    the pair the previous writer left, and it is never deleted or rewritten. Reading it
    after a generation has been committed would boot from the older publication and then
    flush it forward over the newer one.
    """
    settings = _settings(tmp_path)
    present = {
        f"gen/{_GEN}/{name}": b'{"from": "the committed generation"}'
        for name in keys.AUTHORITY_NAMES
    }
    for name in keys.AUTHORITY_NAMES:
        present[f"data/{name}"] = b'{"from": "generation zero"}'
    present[keys.AUTHORITY_POINTER_NAME] = _pointer()
    bucket = _Bucket(present)

    restore_mod.restore_authority(settings, bucket)

    fetched = [key for key in bucket.gets if key.endswith(".json")]
    assert all(f"gen/{_GEN}/" in key for key in fetched if "authority" not in key)
    assert not any(key.endswith(f"data/{name}") for key in fetched for name in keys.AUTHORITY_NAMES)


# --- 11. the pair is read from the committed generation and nowhere else -----------------------


class _Bucket:
    """A bucket holding exactly the objects given, by their key's trailing name.

    A stored key CONTAINING a slash is matched as a suffix, which is how a test plants
    ``gen/<slot>/<name>``. A stored BARE name means the legacy key and must not also answer
    for a generation slot -- otherwise planting ``session_map.json`` silently populates both
    slots as well, and a survey of the slots reads a legacy-only bucket as two complete
    generations.
    """

    def __init__(self, present: dict[str, bytes]) -> None:
        self._present = present
        self.gets: list[str] = []

    def get(
        self,
        key: str,
        *,
        limit: int,
        deadline: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> bytes:
        self.gets.append(key)
        for name, raw in self._present.items():
            if "/" in name:
                if key.endswith(name):
                    return raw
            elif key.endswith(f"/{name}") and keys.GENERATION_PREFIX not in key:
                return raw
        raise ObjectAbsent(key)

    def get_with_etag(
        self,
        key: str,
        *,
        limit: int,
        deadline: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> tuple[bytes, str | None]:
        return self.get(key, limit=limit), None

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
        raise AssertionError("the restore does not write to the bucket")


class _UnreadablePointer(_Bucket):
    """A bucket whose pointer cannot be read, which is not the same as not holding one."""

    def get(
        self,
        key: str,
        *,
        limit: int,
        deadline: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> bytes:
        if key.endswith(keys.AUTHORITY_POINTER_NAME):
            raise PermissionError("the pointer is there and this task may not read it")
        return super().get(key, limit=limit)


#: A fixed, well-formed generation id for the restore tests, so a planted pointer and the
#: keys it names agree without minting a fresh one each time.
_GEN = "00000000000000000001-0123456789abcdef"


def _pointer(generation_id: str = _GEN) -> bytes:
    return generation_mod.pointer_body(generation_id)


@pytest.mark.parametrize("missing", list(keys.AUTHORITY_NAMES))
def test_a_committed_generation_that_lost_a_member_refuses_the_boot(tmp_path, missing):
    """The case that must REFUSE: a pair was published whole and one member is gone now.

    Starting from the remaining file lets the backend flush its own empty view of the other
    over a real conversation list. Either half is the same hazard, so both are pinned.
    """
    settings = _settings(tmp_path)
    present = {f"gen/{_GEN}/{name}": b"{}" for name in keys.AUTHORITY_NAMES if name != missing}
    present[keys.AUTHORITY_POINTER_NAME] = _pointer()

    with pytest.raises(restore_mod.RestoreFailed, match="does not hold"):
        restore_mod.restore_authority(settings, _Bucket(present))


def test_a_committed_generation_holding_its_whole_pair_restores_it(tmp_path):
    """The ordinary case: a committed generation holding the pair it was committed with."""
    settings = _settings(tmp_path)
    present = {f"gen/{_GEN}/{name}": b"{}" for name in keys.AUTHORITY_NAMES}
    present[keys.AUTHORITY_POINTER_NAME] = _pointer()

    result = restore_mod.restore_authority(settings, _Bucket(present))

    assert sorted(result.restored + result.kept_local) == sorted(keys.AUTHORITY_NAMES)


def test_a_pointer_that_cannot_be_read_refuses_the_boot(tmp_path):
    """Unreadable is not absent, and only absent is permission to boot on a partial pair.

    Reading a denial as "no pointer" would hand back the very boot the pointer gates, so
    posture matches the one this module already takes for an authority file it cannot read.
    """
    settings = _settings(tmp_path)

    with pytest.raises(restore_mod.RestoreFailed, match="could not be read"):
        restore_mod.restore_authority(
            settings, _UnreadablePointer({keys.AUTHORITY_NAMES[0]: b"{}"})
        )


@pytest.mark.parametrize("raw", [b"not json", b"[]", b'{"authority": "session_map.json"}'])
def test_a_pointer_that_does_not_parse_refuses_the_boot(tmp_path, raw):
    """A pointer present but unusable cannot say which generation is committed."""
    settings = _settings(tmp_path)
    present = {keys.AUTHORITY_NAMES[0]: b"{}", keys.AUTHORITY_POINTER_NAME: raw}

    with pytest.raises(restore_mod.RestoreFailed):
        restore_mod.restore_authority(settings, _Bucket(present))


def test_a_pointer_that_under_lists_a_known_name_refuses_the_boot(tmp_path):
    """A pointer omitting a name this version knows is unusable, not a partial generation.

    Read as a generation that simply contains one file, the omitted name would present as
    legitimately absent -- the same answer a first boot gets -- and the backend would flush
    its own empty view over whatever the bucket holds. The writer commits the pair whole,
    so a pointer under-listing it is an object no cycle of this writer produced.
    """
    settings = _settings(tmp_path)
    pointer = json.dumps({"generation": _GEN, "authority": [keys.AUTHORITY_NAMES[0]]})
    present = {f"gen/{_GEN}/{name}": b"{}" for name in keys.AUTHORITY_NAMES}
    present[keys.AUTHORITY_POINTER_NAME] = pointer.encode()

    with pytest.raises(restore_mod.RestoreFailed):
        restore_mod.restore_authority(settings, _Bucket(present))


def test_a_pointer_naming_a_name_this_version_does_not_know_still_restores(tmp_path):
    """The tolerance the check above must not cost: a rollback has to stay bootable.

    A newer writer that commits a third authority file names it in the pointer. This
    version cannot restore what it has no name for, but the pair it does know is whole and
    committed, so refusing would make rolling back to this version unbootable on a bucket
    a newer one wrote -- a worse failure than ignoring a file this version never reads.
    """
    settings = _settings(tmp_path)
    listed = [*keys.AUTHORITY_NAMES, "a_later_authority_file.json"]
    present = {f"gen/{_GEN}/{name}": b"{}" for name in keys.AUTHORITY_NAMES}
    present[keys.AUTHORITY_POINTER_NAME] = json.dumps(
        {"generation": _GEN, "authority": listed}
    ).encode()

    result = restore_mod.restore_authority(settings, _Bucket(present))

    assert result.absent == []


def test_both_absent_is_still_a_first_boot(tmp_path):
    """The allowed case has to stay allowed, or no crew could ever start."""
    settings = _settings(tmp_path)

    result = restore_mod.restore_authority(settings, _Bucket({}))

    assert sorted(result.absent) == sorted(keys.AUTHORITY_NAMES)
    assert result.restored == []


# --- 12. the drain deadline is anchored where the supervisor starts counting -------


def test_the_final_deadline_is_measured_from_when_the_stop_was_observed():
    """The supervisor counts ``SIDECAR_DRAIN_SECS`` from SIGTERM delivery, so this must too.

    A cycle already in flight when the signal lands keeps running, so a deadline computed
    when the loop next looks at the flag would start counting a window already partly
    spent -- and the final cycle would be killed mid-PUT believing it had time.
    """
    observed = time.monotonic() - 30.0

    from_stop = sidecar_main._drain_deadline(observed)
    from_now = sidecar_main._drain_deadline(None)

    assert from_stop < from_now
    assert from_stop == pytest.approx(observed + cfg.SIDECAR_DRAIN_SECS)


def test_the_signal_handler_records_the_first_instant_only(monkeypatch):
    """A second signal must not push the deadline out; the window started at the first."""
    monkeypatch.setattr(sidecar_main, "_STOP_OBSERVED", [])
    monkeypatch.setattr(sidecar_main, "_STOP", threading.Event())

    sidecar_main._on_signal(15, None)
    first = sidecar_main._STOP_OBSERVED[0]
    sidecar_main._on_signal(15, None)

    assert sidecar_main._STOP_OBSERVED == [first]


def test_an_interval_cycle_stands_down_when_the_stop_arrives(tmp_path):
    """Its uploads predate the flush, so the final cycle takes them anyway.

    Continuing would only spend the drain window that cycle needs, which is the window
    the whole ordering exists to protect.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    _transcript(settings, b"another\n", stem="dashboard_cust-92")
    store = _Recorder()

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, store, state={}, yield_when=lambda: True)

    assert store.objects == {}
    assert {name for name, _why in caught.value.result.refused}


def test_the_final_cycle_does_not_stand_down_on_the_flag_that_made_it_final(tmp_path):
    """It is the cycle whose uploads matter, so it runs to its deadline instead."""
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    store = _Recorder()

    result = backup_mod.run_cycle(settings, store, state={}, deadline=time.monotonic() + 3600)

    assert result.refused == []
    assert store.objects


# --- 13. the temporary is created in the directory we pinned ----------------------


def test_publication_creates_its_temporary_through_the_pinned_directory():
    """Creating it by path lets a rename between the open and the create detach it.

    The bytes would land in the replacement directory while the link published into the
    old, detached one, so the backend would start from an empty history and the backup
    would then overwrite the bucket with it.
    """
    source = pathlib.Path(statefile.__file__).read_text(encoding="utf-8")

    assert "dir_fd=parent_fd" in source
    assert "tempfile.mkstemp" not in source


def test_publication_still_lands_the_bytes_through_the_pinned_directory(tmp_path):
    """Non-vacuity for the check above: the mechanism has to still work."""
    target = tmp_path / "published.json"

    assert statefile.link_new(target, b'{"keys": ["cust-1"]}', prefix="probe-")
    assert target.read_bytes() == b'{"keys": ["cust-1"]}'
    assert [p.name for p in tmp_path.iterdir()] == ["published.json"]


# --- 12. the drain gate cuts off PUTs, not objects that need none -----------------


def test_an_already_durable_object_is_unchanged_rather_than_refused(tmp_path):
    """A drain whose objects are already durable must not report itself as a lossy one.

    The gate exists to stop a PUT that cannot finish. An object the bucket already holds
    needs no PUT, so cutting it off records a refusal for work that was never owed -- and a
    refusal withholds the authority pair, fails the cycle and exits the task non-zero. Under
    content addressing the key IS the digest, so confirming durability requires hashing the
    object first; with the window still open the hash runs, the object is found durable, and
    it is recorded unchanged rather than refused. (A deadline so tight the hash itself cannot
    fit is the hashing-deadline case, pinned separately -- there the object is named, because
    the window is already blown.)
    """
    settings = _settings(tmp_path)
    path = _transcript(settings, b"a turn\n")
    plan = backup_mod.objects_to_back_up(settings)
    # The object is durable under its CONTENT-ADDRESSED key, so that is what state is seeded
    # with -- marking exactly these bytes already in the bucket.
    key = _blob_key(settings, b"a turn\n")
    try:
        snapshot = backup_mod.open_snapshot(path, root=settings.data_home)
        assert snapshot is not None
        try:
            state = {key: snapshot.fingerprint}
        finally:
            snapshot.close()
        result = backup_mod.CycleResult()
        backup_mod._upload_phase(
            plan.data,
            settings=settings,
            store=_Recorder(),
            state=state,
            result=result,
            committed_index={},
            # Window open: the hash fits, so durability is confirmed and no PUT is owed.
            deadline=time.monotonic() + cfg.SIDECAR_DRAIN_SECS,
        )
    finally:
        plan.close_authority()

    assert result.unchanged == [key]
    assert result.refused == []
    assert result.complete


def test_an_object_that_does_need_a_put_is_still_cut_off(tmp_path):
    """The mirror, so the hoist above did not simply disable the gate."""
    settings = _settings(tmp_path)
    path = _transcript(settings, b"a turn\n")
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    try:
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
        plan.close_authority()

    assert [name for name, _why in result.refused] == [path.name]
    assert result.unchanged == []


# --- 13. an unreachable tree is not a deleted conversation ------------------------


def test_a_missing_directory_is_refused_rather_than_read_as_a_deletion(tmp_path):
    """``gone`` means the owner deleted a conversation, not that the tree went away.

    One errno covers both: the walk down to a transcript meets ENOENT whether the leaf was
    unlinked or a directory above it vanished -- an unmounted data home, a removed archive
    directory -- and the bytes behind that directory may be perfectly live. Recorded as a
    deletion it raises nothing, so the cycle publishes an index for conversations it never
    looked at and the front then serves empty history for every one of them.
    """
    settings = _settings(tmp_path)
    path = _transcript(settings, b"a turn\n")
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    shutil.rmtree(path.parent)
    try:
        backup_mod._upload_phase(
            plan.data,
            settings=settings,
            store=_Recorder(),
            state={},
            result=result,
            committed_index={},
        )
    finally:
        plan.close_authority()

    assert result.gone == []
    assert [name for name, _why in result.refused] == [path.name]
    assert not result.complete


def test_an_unlinked_leaf_is_still_a_deletion_rather_than_a_refusal(tmp_path):
    """The over-strict direction: an owner deleting a conversation is not a failure.

    This is the case the distinction exists to keep. Refusing here would fail every cycle
    that raced a deletion, which is a routine thing for a customer to do.
    """
    settings = _settings(tmp_path)
    path = _transcript(settings, b"a turn\n")
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    path.unlink()
    try:
        backup_mod._upload_phase(
            plan.data,
            settings=settings,
            store=_Recorder(),
            state={},
            result=result,
            committed_index={},
        )
    finally:
        plan.close_authority()

    assert result.gone == [path.name]
    assert result.refused == []
    assert result.complete


# --- 12. the reserved window is enforced on the transmission ------------------------
#
# The gate reserves BACKUP_PER_OBJECT_BUDGET_SECS and then admits the PUT. Nothing
# inside the PUT honoured that number: connect_timeout and read_timeout bound one
# connect and one read, and a bucket delivering small chunks under the read timeout
# trips neither however long the whole object takes. So the only thing that ended a
# slow upload was the drain window's SIGKILL, arriving mid-request -- losing that
# object and saying nothing about the ones behind it, which is the exact outcome the
# deadline was built to replace.


class _Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, secs: float) -> None:
        self.now += secs


def test_a_body_past_its_window_stops_sending(tmp_path):
    """The read is refused, so a progressing-but-slow transmission is cut.

    The whole point is that this happens while the connection is healthy: every chunk
    arrives, none of them late enough to trip a socket timeout, and the object is still
    not going to finish. Reading is what the transport does repeatedly during a send, so
    it is the one place a request can be stopped mid-flight.
    """
    path = tmp_path / "body.bin"
    path.write_bytes(b"x" * 64)
    clock = _Clock()
    with path.open("rb") as fh:
        reader = objects.BoundedReader(fh, 64, deadline=clock.now + 10.0, clock=clock)
        assert reader.read(8) == b"x" * 8
        assert not reader.expired()
        clock.advance(10.0)
        assert reader.expired()
        with pytest.raises(objects.UploadDeadlineExceeded) as caught:
            reader.read(8)

    # The message names what did not go, because a cut upload's value is telling the
    # operator which bytes are missing.
    assert "56 of 64 B unsent" in str(caught.value)


def test_a_rewind_does_not_refresh_the_window(tmp_path):
    """A retry is part of getting this object there, not a second allowance.

    botocore seeks the body back to its start before re-sending, so a deadline reset on
    seek would let ``attempts`` slow attempts each spend the whole window -- the gate
    would reserve one object's worth of time and the object could spend several.
    """
    path = tmp_path / "body.bin"
    path.write_bytes(b"y" * 32)
    clock = _Clock()
    with path.open("rb") as fh:
        reader = objects.BoundedReader(fh, 32, deadline=clock.now + 5.0, clock=clock)
        assert reader.read(4) == b"y" * 4
        clock.advance(5.0)
        reader.seek(0)
        assert reader.tell() == 0, "the rewind itself still works"
        with pytest.raises(objects.UploadDeadlineExceeded):
            reader.read(4)


class _BudgetRecorder(_Recorder):
    """Remembers the window it was handed for each object."""

    def __init__(self) -> None:
        super().__init__()
        self.budgets: list[float | None] = []

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
        self.budgets.append(budget)
        super().put(key, body, size)


def test_the_final_cycle_hands_the_store_the_window_it_reserved(tmp_path):
    """One number, not two that agree only when the network is fast.

    The gate admits the object by reserving ``BACKUP_PER_OBJECT_BUDGET_SECS``, so that is
    what the PUT must be bounded by. Reserving one number and bounding by another is how
    a gate comes to admit an upload the window cannot hold.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    store = _BudgetRecorder()
    try:
        backup_mod._upload_phase(
            plan.data,
            settings=settings,
            store=store,
            state={},
            result=result,
            committed_index={},
            deadline=time.monotonic() + 600.0,
        )
    finally:
        plan.close_authority()

    assert store.budgets == [cfg.BACKUP_PER_OBJECT_BUDGET_SECS]
    assert result.complete


def test_an_interval_cycle_hands_the_store_no_window(tmp_path):
    """The over-strict direction, and the reason this is not bounded everywhere.

    An interval cycle has no drain window to overrun and a next cycle to finish the
    object. Bounding it there would refuse an object slower than the window FOREVER --
    a 64 MiB transcript needs about 6.7 MB/s to fit -- so a slow link would never get
    one into the bucket at all. On the final cycle the alternative is losing it
    silently, which is why the bound belongs only there.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    store = _BudgetRecorder()
    try:
        backup_mod._upload_phase(
            plan.data, settings=settings, store=store, state={}, result=result, committed_index={}
        )
    finally:
        plan.close_authority()

    assert store.budgets == [None]


def test_an_upload_cut_by_its_window_is_refused_rather_than_lost(tmp_path):
    """The gain over the SIGKILL: the object is named, and the index does not move.

    A kill mid-PUT loses the object being sent and every object after it, with nothing
    recorded. A cut upload is an ordinary refusal: it withholds the authority pair, so
    the index still describes a state the bucket supports, and the cycle is incomplete
    so the final cycle's exit code reports the loss.
    """
    settings = _settings(tmp_path)
    path = _transcript(settings, b"a turn\n")

    class _TooSlow(_Recorder):
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
            raise objects.UploadDeadlineExceeded(
                f"PutObject on {key} did not finish inside its {budget:.0f}s budget"
            )

    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    try:
        backup_mod._upload_phase(
            plan.data,
            settings=settings,
            store=_TooSlow(),
            state={},
            result=result,
            committed_index={},
            deadline=time.monotonic() + 600.0,
        )
    finally:
        plan.close_authority()

    assert [name for name, _why in result.refused] == [path.name]
    assert not result.complete
    # Refused, NOT unreachable: the bucket answered and the transport worked, so the next
    # cycle attempts this object again. Classing it permanent would end the process over
    # a slow network.
    assert result.unreachable == []


def test_a_cut_upload_is_not_read_as_a_permanent_fault(tmp_path):
    """Through the real store, with a client that drains the body slowly.

    ``StoreUnusable`` ends the process, so classifying this as permanent would turn a
    slow bucket into a crash -- and the sidecar would stop backing anything up at all.
    The store is asked to judge by its own reader's clock rather than by the exception
    that surfaced, because the transport owns what a body raising mid-send looks like.
    """
    from container.sidecar import store as store_mod
    from container.sidecar.store import S3ObjectStore, StoreUnusable

    path = tmp_path / "big.jsonl"
    path.write_bytes(b"z" * 4096)
    clock = _Clock()

    class _FakeTime:
        """Stands in for the ``time`` module at both sites that read the clock."""

        monotonic = staticmethod(clock)

    class _SlowClient:
        """Reads the body in small chunks, spending clock time on each one.

        Every read succeeds and none of them is slow enough to trip a socket timeout.
        This is the shape the finding names: healthy, progressing, and never finishing.
        """

        def put_object(self, **kw):  # pragma: no cover - raises before it returns
            body = kw["Body"]
            while body.read(16):
                clock.advance(1.0)
            return {}

    with pytest.MonkeyPatch.context() as mp:
        # Both, because the deadline and the reader that enforces it read the clock in
        # different modules: patching only one leaves a real reading on the other side of
        # the comparison and the deadline can never be reached.
        mp.setattr(store_mod, "time", _FakeTime)
        mp.setattr(objects, "time", _FakeTime)
        store = S3ObjectStore("a-bucket", client=_SlowClient())
        with path.open("rb") as fh:
            with pytest.raises(objects.UploadDeadlineExceeded) as caught:
                store.put("crews/c/big.jsonl", fh, 4096, budget=10.0)

    assert not isinstance(caught.value, StoreUnusable)
    # Derived, not written: the BODY's share of a budget is the budget less the response wait
    # the body cannot bound, so a count written here would go stale the moment that split
    # moves. One chunk goes per simulated second, and the read after the share elapses is the
    # one refused.
    sent = 16 * int(10.0 - cfg.BACKUP_REQUEST_TIMEOUT_SECS)
    assert f"{4096 - sent} of 4096 B unsent" in str(caught.value)


def test_a_transport_that_swallows_the_body_error_is_still_read_as_the_window(tmp_path):
    """The store judges by its own reader's clock, not by what came out of the transport.

    A body raising mid-send is not guaranteed to surface as itself: botocore may wrap it
    in a connection error, or convert it into a retry that then fails on its own. Judged
    by the exception, an expired window would reach ``classify_permanent`` and could end
    the process over a slow network. Judged by the clock, it is the window either way.
    """
    from container.sidecar import store as store_mod
    from container.sidecar.store import S3ObjectStore, StoreUnusable

    path = tmp_path / "big.jsonl"
    path.write_bytes(b"z" * 4096)
    clock = _Clock()

    class _FakeTime:
        monotonic = staticmethod(clock)

    class _SwallowingClient:
        """Drains until the body objects, then reports a generic transport fault."""

        def put_object(self, **kw):
            body = kw["Body"]
            try:
                while body.read(16):
                    clock.advance(1.0)
            except objects.UploadDeadlineExceeded as exc:
                raise ConnectionError("connection reset by peer") from exc
            return {}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(store_mod, "time", _FakeTime)
        mp.setattr(objects, "time", _FakeTime)
        store = S3ObjectStore("a-bucket", client=_SwallowingClient())
        with path.open("rb") as fh:
            with pytest.raises(objects.UploadDeadlineExceeded) as caught:
                store.put("crews/c/big.jsonl", fh, 4096, budget=10.0)

    assert not isinstance(caught.value, StoreUnusable)
    assert "did not finish inside its 10s budget" in str(caught.value)
    # The transport's own error is kept as the cause, so the log still shows what the
    # connection reported.
    assert isinstance(caught.value.__cause__, ConnectionError)


def test_a_final_cycle_get_is_cut_when_the_drain_deadline_expires(tmp_path):
    """A trickling preflight GET is refused once the cycle deadline is past, not SIGKILLed.

    The final cycle's pointer and transcript-index reads run inside the supervisor's drain
    window. The socket's read timeout a chunk-trickling connection never trips cannot bound
    them, so a large index on a merely degraded link would outlive the window and be
    SIGKILLed mid-read -- the final commit never happens and the flushed turns are lost.
    Threading the cycle deadline into the GET wraps the body in a deadline-bearing reader
    that cuts the read the moment the window is spent. This drives a GET whose body trickles
    one chunk per simulated second against a deadline already in the near past and asserts the
    read raises :class:`~container.common.objects.UploadDeadlineExceeded`.

    It fails if the deadline is not threaded into the GET: the body reads to its end and the
    too-slow stream is never cut.
    """
    from container.sidecar import store as store_mod
    from container.sidecar.store import S3ObjectStore

    clock = _Clock()

    class _FakeTime:
        """Patched at both clock-reading sites, as the PUT deadline tests are."""

        monotonic = staticmethod(clock)

    class _TricklingBody:
        """A GET body that yields one small chunk per read, spending clock time on each."""

        def __init__(self) -> None:
            self._remaining = 4096

        def read(self, amt: int = -1) -> bytes:
            if self._remaining <= 0:
                return b""
            clock.advance(1.0)
            take = 16 if amt is None or amt < 0 else min(amt, 16)
            take = min(take, self._remaining)
            self._remaining -= take
            return b"z" * take

    class _SlowGetClient:
        def get_object(self, **kw):  # noqa: N803 - botocore casing
            return {"Body": _TricklingBody(), "ETag": '"etag"', "ContentLength": 4096}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(store_mod, "time", _FakeTime)
        mp.setattr(objects, "time", _FakeTime)
        store = S3ObjectStore("a-bucket", client=_SlowGetClient())
        # A deadline five simulated seconds out: the trickling body spends one second per
        # chunk, so the read is cut long before the 4096 bytes arrive.
        deadline = clock() + 5.0
        with pytest.raises(objects.UploadDeadlineExceeded):
            store.get_with_etag(
                "crews/c/gen/x/transcript_index.json", limit=1 << 26, deadline=deadline
            )


# --- 13. an interval upload does not outlive the stop it was told about --------------


def test_a_body_cut_by_a_stop_stops_sending(tmp_path):
    """The unbounded body is still endable, which is what makes it safe to leave unbounded.

    An interval cycle passes no deadline on purpose, so time cannot end its upload. The
    stop can: the transport asks the body for a chunk many times during a send, so the
    predicate is asked there too and the request ends while it is still progressing.
    """
    path = tmp_path / "body.bin"
    path.write_bytes(b"x" * 64)
    stopping = threading.Event()
    with path.open("rb") as fh:
        reader = objects.BoundedReader(fh, 64, cancel=stopping.is_set)
        assert reader.read(8) == b"x" * 8
        assert not reader.cancelled()
        stopping.set()
        assert reader.cancelled()
        with pytest.raises(objects.UploadCancelled) as caught:
            reader.read(8)

    # Named, like the window refusal, because the value of cutting an upload is knowing
    # which bytes did not go.
    assert "56 of 64 B unsent" in str(caught.value)


def test_the_stop_is_checked_before_the_read_not_after(tmp_path):
    """Waiting for one more chunk is the cost this exists to avoid.

    Checked after the read, the cut still waits on the connection that the shutdown cannot
    afford to wait for -- and a chunk the transport never sends is an unbounded wait.
    """
    path = tmp_path / "body.bin"
    path.write_bytes(b"x" * 32)
    stopping = threading.Event()
    stopping.set()
    reads: list[int] = []

    class _CountingFile:
        def __init__(self, fh):
            self._fh = fh

        def read(self, amt):
            reads.append(amt)
            return self._fh.read(amt)

        def tell(self):
            return self._fh.tell()

        def seekable(self):
            return True

        def seek(self, offset, whence=0):
            return self._fh.seek(offset, whence)

    with path.open("rb") as fh:
        reader = objects.BoundedReader(_CountingFile(fh), 32, cancel=stopping.is_set)
        with pytest.raises(objects.UploadCancelled):
            reader.read(4)

    assert reads == [], "the refusal must not consume a chunk first"


def test_a_large_read_rechecks_the_stop_between_underlying_slices(tmp_path):
    """GPT F2: a single big read() is cut mid-transfer, not only before it starts.

    read_bounded asks for GET_CHUNK_BYTES (1 MiB) per read. If BoundedReader issued one
    underlying read of the whole amount, a link trickling below the socket read timeout would
    block there for the whole chunk -- past the drain window -- with the deadline/cancel checks
    (which run only between reads) never getting a turn. The read is served in slices of at
    most _READ_SLICE_BYTES with a check between each, so a stop that arrives DURING the read
    cuts it after the next slice rather than after the whole amount.

    It fails if read() issues one underlying read for the whole `want` (the mutation: the stop
    is only seen before the read, so a big read runs to completion despite a mid-read stop).
    """
    slice_n = objects._READ_SLICE_BYTES
    total = slice_n * 4
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * total)
    stopping = threading.Event()
    slices: list[int] = []

    class _SlicingFile:
        def __init__(self, fh):
            self._fh = fh

        def read(self, amt):
            slices.append(amt)
            # The stop arrives after the FIRST underlying slice is served.
            if len(slices) == 1:
                stopping.set()
            return self._fh.read(amt)

        def tell(self):
            return self._fh.tell()

        def seekable(self):
            return True

        def seek(self, offset, whence=0):
            return self._fh.seek(offset, whence)

    with path.open("rb") as fh:
        reader = objects.BoundedReader(_SlicingFile(fh), total, cancel=stopping.is_set)
        with pytest.raises(objects.UploadCancelled):
            reader.read(total)

    # Each underlying read is bounded by the slice size, and the whole `want` is NOT read in
    # one call: the stop set after the first slice cut the read before the remaining slices.
    assert all(n <= slice_n for n in slices), slices
    assert len(slices) == 1, "the stop after the first slice must cut before the next read"


def test_a_slice_read_that_blocks_is_cut_mid_flight_by_the_stop(tmp_path):
    """GPT F1: a single underlying slice read is interruptible WHILE it is in flight.

    The between-slice checks do not bound a read that is itself blocking: a link trickling
    below the socket read timeout keeps one ``self._fh.read(64 KiB)`` alive for far longer
    than the drain window with no check reachable inside it. The slice read now runs on a
    worker thread the caller waits on in short steps, so a stop that arrives WHILE the read is
    blocked cuts it mid-flight rather than after the whole slice arrives.

    It fails if the slice read is issued directly on the calling thread (the mutation): the
    stop set during the block is never seen until the blocked read returns, so ``read`` would
    hang on the never-returning read instead of raising promptly.
    """
    release = threading.Event()
    stopping = threading.Event()
    entered = threading.Event()

    class _TricklingFile:
        def read(self, amt):
            entered.set()
            # Models a slice read that does not return within the window: it blocks until
            # explicitly released, standing in for a link delivering below the read timeout.
            release.wait(5.0)
            return b"z" * amt

        def tell(self):
            return 0

        def seekable(self):
            return True

        def seek(self, offset, whence=0):
            return 0

    reader = objects.BoundedReader(
        _TricklingFile(), objects._READ_SLICE_BYTES, cancel=stopping.is_set
    )

    def _arm_stop() -> None:
        # Set the stop once the underlying read is in flight (entered), NOT on a wall-clock
        # delay. If the read never starts, the bounded wait makes this thread return rather
        # than hang.
        entered.wait(5.0)
        stopping.set()

    arm = threading.Thread(target=_arm_stop)
    arm.start()
    try:
        with pytest.raises(objects.UploadCancelled):
            reader.read(objects._READ_SLICE_BYTES)
        # The cut landed WHILE the underlying read was still blocked -- `release` is never set
        # in this test, so the only way `read` returned (by raising) is the in-flight read
        # being abandoned, not waited out. This is an ordering fact, independent of how the
        # runner schedules threads: no wall-clock bound can flake it.
        assert not release.is_set(), "cancellation must land before the blocked read returns"
    finally:
        arm.join(5.0)
        release.set()  # let the orphaned worker read finish so no thread lingers


def test_a_rewind_does_not_clear_the_stop(tmp_path):
    """A retry meets the same stop, because the predicate is asked rather than latched.

    botocore seeks the body back to its start before re-sending. A stop cleared on seek
    would let the transport's own retry restart the upload the shutdown just ended, which
    is the whole cost back again.
    """
    path = tmp_path / "body.bin"
    path.write_bytes(b"y" * 32)
    stopping = threading.Event()
    with path.open("rb") as fh:
        reader = objects.BoundedReader(fh, 32, cancel=stopping.is_set)
        assert reader.read(4) == b"y" * 4
        stopping.set()
        reader.seek(0)
        assert reader.tell() == 0, "the rewind itself still works"
        with pytest.raises(objects.UploadCancelled):
            reader.read(4)


def test_a_body_with_no_stop_predicate_reads_to_the_end(tmp_path):
    """The over-strict direction: no predicate is no cut, not an immediate one.

    The final cycle passes none -- the flag is what made it final, so standing down on it
    would refuse the one cycle whose uploads matter.
    """
    path = tmp_path / "body.bin"
    path.write_bytes(b"z" * 16)
    with path.open("rb") as fh:
        reader = objects.BoundedReader(fh, 16)
        assert not reader.cancelled()
        assert reader.read(-1) == b"z" * 16


class _CancelRecorder(_Recorder):
    """Remembers the stop predicate it was handed for each object."""

    def __init__(self) -> None:
        super().__init__()
        self.cancels: list[Callable[[], bool] | None] = []

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
        self.cancels.append(cancel)
        super().put(key, body, size)


def test_an_interval_cycle_hands_the_store_the_stop_flag(tmp_path):
    """The gap the window bound leaves open, closed by the other mechanism.

    The between-objects check at the top of the phase cannot see a stop that lands DURING
    a PUT, and an interval PUT has no budget to end it. So the flag goes to the store: the
    upload is cut inside the transmission, the cycle returns, and the final cycle starts
    with the window the supervisor is counting rather than what is left of it.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    store = _CancelRecorder()
    stopping = threading.Event()
    try:
        backup_mod._upload_phase(
            plan.data,
            settings=settings,
            store=store,
            state={},
            result=result,
            committed_index={},
            yield_when=stopping.is_set,
        )
    finally:
        plan.close_authority()

    assert store.cancels == [stopping.is_set]


def test_the_final_cycle_hands_the_store_no_stop_flag(tmp_path):
    """The cycle whose uploads matter is not stood down by the flag that made it final.

    Its bound is the deadline, which is measured from when the stop was observed. Passing
    the flag here would cut every upload of the drain cycle immediately and lose exactly
    the turns the drain exists to save.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    store = _CancelRecorder()
    try:
        backup_mod._upload_phase(
            plan.data,
            settings=settings,
            store=store,
            state={},
            result=result,
            committed_index={},
            deadline=time.monotonic() + 600.0,
            yield_when=None,
        )
    finally:
        plan.close_authority()

    assert store.cancels == [None]


def test_the_authority_phase_is_cut_by_the_stop_too(tmp_path):
    """One invariant, both phases. An index PUT outlives a stop exactly as a transcript can.

    The interval authority phase has no deadline either, so without the flag it is the
    second way an in-flight upload eats the drain window.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    store = _CancelRecorder()
    stopping = threading.Event()
    try:
        backup_mod._commit_authority(
            plan.authority,
            settings=settings,
            store=store,
            state={},
            result=result,
            yield_when=stopping.is_set,
        )
    finally:
        plan.close_authority()

    assert store.cancels, "the authority phase uploaded nothing, so nothing was pinned"
    assert store.cancels == [stopping.is_set] * len(store.cancels)


def test_an_upload_cut_by_the_stop_is_refused_rather_than_lost(tmp_path):
    """Recorded like the window refusal: the pair is withheld and the name is in the log.

    The object is left to the final cycle DELIBERATELY, so the index must not name it yet.
    A refusal is what withholds the pair, and it is transient -- the next cycle sends the
    object rather than the process ending over an ordinary shutdown.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")

    class _Cut(_Recorder):
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
            raise objects.UploadCancelled(f"PutObject on {key} was cut by a stop")

    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    try:
        backup_mod._upload_phase(
            plan.data, settings=settings, store=_Cut(), state={}, result=result, committed_index={}
        )
    finally:
        plan.close_authority()

    assert not result.complete
    assert [name for name, _why in result.refused] == [f"{STEM}{keys.TRANSCRIPT_SUFFIX}"]
    assert "cut by the stop" in result.refused[0][1]
    # Transient, so the next cycle attempts it. Classified permanent it would be an
    # unreachable entry and the pair would publish without it.
    assert not result.unreachable


def test_a_cut_index_leaves_the_pointer_where_it_is(tmp_path):
    """Cutting the authority phase must not publish a generation it did not finish.

    The pointer is the LAST step precisely so an interrupted pair leaves the previous
    generation committed and whole. A cut index that still moved the pointer would be the
    torn index the phase order exists to prevent -- worse than the window it saves.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    result.refused.append(("index.json", "the upload was cut by the stop"))
    store = _Recorder()

    try:
        backup_mod._commit_generation(
            plan,
            settings=settings,
            store=store,
            state={},
            result=result,
            generation_id=plan.generation_id,
        )
    finally:
        plan.close_authority()

    assert store.puts == [], "the pointer moved over a cut authority phase"


def test_the_store_reads_a_swallowed_body_error_as_the_stop(tmp_path):
    """Judged by the reader's own state, not by what came out of the transport.

    A body raising mid-send may surface as itself, wrapped in a connection error, or as a
    retry that fails on its own. Judged by the exception, a cut upload could reach
    ``classify_permanent`` and end the process over an ordinary shutdown.
    """
    from container.sidecar import store as store_mod
    from container.sidecar.store import S3ObjectStore, StoreUnusable

    path = tmp_path / "big.jsonl"
    path.write_bytes(b"z" * 4096)
    stopping = threading.Event()

    class _SwallowingClient:
        """Drains one chunk, then the stop lands and the body's refusal is wrapped."""

        def put_object(self, **kw):
            body = kw["Body"]
            body.read(16)
            stopping.set()
            try:
                while body.read(16):
                    pass
            except objects.UploadCancelled as exc:
                raise ConnectionError("connection reset by peer") from exc
            return {}

    store = S3ObjectStore("a-bucket", client=_SwallowingClient())
    with path.open("rb") as fh:
        with pytest.raises(objects.UploadCancelled) as caught:
            store.put("crews/c/big.jsonl", fh, 4096, cancel=stopping.is_set)

    assert not isinstance(caught.value, StoreUnusable)
    assert "was cut by a stop" in str(caught.value)
    assert isinstance(caught.value.__cause__, ConnectionError)
    assert store_mod.UploadCancelled is objects.UploadCancelled


def test_both_cuts_are_one_class_the_caller_can_catch(tmp_path):
    """One handling path, two names. A cut upload is never permanent, whichever cut it was.

    The caller does the same thing for both -- record the refusal, let the next cycle send
    it -- so they share a base. Separate names are what the operator's log needs.
    """
    assert issubclass(objects.UploadCancelled, objects.UploadCut)
    assert issubclass(objects.UploadDeadlineExceeded, objects.UploadCut)
    assert not issubclass(objects.UploadCut, objects.StoreUnusable)


def test_an_in_flight_interval_upload_ends_when_the_stop_arrives(tmp_path):
    """The finding, end to end: the drain cycle begins instead of waiting on this PUT.

    Without the flag reaching the body, a slow interval PUT keeps reading until it finishes
    on its own. Its cycle cannot return before then and ``run`` cannot begin the final
    cycle, whose deadline is anchored at the signal -- so the turns the backend flushed on
    its way out are never attempted.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n" * 64)
    stopping = threading.Event()
    chunks: list[int] = []

    class _SlowButProgressing:
        """A connection that never stalls long enough to trip a socket timeout."""

        def put_object(self, **kw):
            body = kw["Body"]
            while True:
                chunk = body.read(8)
                if not chunk:
                    return {}
                chunks.append(len(chunk))
                # The stop lands partway through, as a SIGTERM during an ordinary upload.
                if len(chunks) == 3:
                    stopping.set()

    from container.sidecar.store import S3ObjectStore

    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    try:
        backup_mod._upload_phase(
            plan.data,
            settings=settings,
            store=S3ObjectStore("a-bucket", client=_SlowButProgressing()),
            state={},
            result=result,
            committed_index={},
            yield_when=stopping.is_set,
        )
    finally:
        plan.close_authority()

    assert len(chunks) == 3, "the upload kept reading after the stop"
    assert not result.complete
    assert [name for name, _why in result.refused] == [f"{STEM}{keys.TRANSCRIPT_SUFFIX}"]


# --- 14. the drain window covers every step the final cycle spends ------------------


def _authority_reservation_count() -> int:
    """What the cycle reserves the authority phase: a PUT per file, the index, the pointer."""
    return len(keys.AUTHORITY_NAMES) + 2


def test_the_drain_window_covers_every_step_the_final_cycle_spends():
    """Sized to the uploads alone, the window is spent before the data phase may start.

    The cycle READS the generation pointer before it accounts for anything, and that GET
    carries no budget -- its only bound is the client's connect and read timeouts, so it can
    spend a whole attempt's cost and still succeed. Whatever it spends comes off the front of
    the window, and the authority reservation comes off the back, so a window sized at
    reservation-plus-one-PUT leaves the data gate nothing the moment the pointer read is
    slower than instant.
    """
    needed = (
        2 * cfg.BACKUP_ATTEMPT_COST_SECS  # the two un-budgeted preflight GETs (pointer, index)
        + _authority_reservation_count() * cfg.BACKUP_ATTEMPT_COST_SECS  # the reservation
        + cfg.BACKUP_PER_OBJECT_BUDGET_SECS  # one worst-case data PUT
    )

    assert cfg.SIDECAR_DRAIN_SECS >= needed


def test_the_data_phase_still_admits_an_upload_after_a_slow_pointer_read():
    """The same claim through the real gate, not a restatement of the arithmetic.

    A pin that recomputes the sum can agree with a window that the production functions
    reject. This spends the pointer read's worst case against a deadline anchored at the
    stop, reserves the authority phase with the function the cycle uses, and asks the gate
    the cycle's own question. Refused here, the drain uploads nothing: every changed
    transcript is recorded unattempted, the pair is withheld, and the replacement adopts the
    previous generation's index while most of the window goes unused.
    """
    stop = 1000.0
    deadline = stop + cfg.SIDECAR_DRAIN_SECS
    # Both preflight GETs (pointer, then committed index) answer slowly, before the cycle
    # accounts for anything.
    now = stop + 2 * cfg.BACKUP_ATTEMPT_COST_SECS
    data_deadline = backup_mod._reserve_for_authority(deadline, _authority_reservation_count())

    assert data_deadline is not None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(backup_mod, "time", _ClockProxy(lambda: now))
        assert backup_mod._time_for_one_more(data_deadline, cfg.BACKUP_PER_OBJECT_BUDGET_SECS)


def test_the_stop_timeout_the_platform_is_asked_for_still_fits_the_cap():
    """The window cannot be widened past what Fargate accepts on a container definition."""
    total = (
        cfg.FRONT_DRAIN_SECS
        + cfg.BACKEND_DRAIN_SECS
        + cfg.SIDECAR_DRAIN_SECS
        + cfg.TEARDOWN_REAP_MARGIN_SECS
    )

    assert total <= cfg.MAX_TASK_STOP_TIMEOUT_SECS


# --- 15. publication is reported only when the bytes are reachable by name -----------


def test_publication_refuses_a_parent_renamed_under_it(tmp_path):
    """Pinning the parent makes every step agree; it does not make them reachable.

    A rename after the descriptor is opened detaches that directory from all of them at
    once, so the temporary is created there, the link publishes there, and both agree on a
    directory that the path does not resolve to. Reported as published, the caller's next
    act is to start the conversation with no history and let the following backup upload
    that emptiness over the bucket's copy.
    """
    parent = tmp_path / "sessions"
    parent.mkdir()
    target = parent / "session_map.json"
    real_link = os.link

    def rename_the_parent_then_link(src, dst, **kw):
        # Exactly the window: the descriptor still addresses this inode, and the path stops
        # naming it. A fresh directory takes its place, as a concurrent turn would leave.
        parent.rename(tmp_path / "detached")
        (tmp_path / "sessions").mkdir()
        return real_link(src, dst, **kw)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "link", rename_the_parent_then_link)
        with pytest.raises(OSError, match="no longer resolves"):
            statefile.link_new(target, b'{"keys": ["cust-1"]}', prefix="probe-")

    assert not target.exists(), "the path must not name a published file"
    assert not (
        tmp_path / "detached" / "session_map.json"
    ).exists(), "the copy left in the detached directory must be removed, not abandoned"


def test_publication_refuses_when_the_replacement_directory_holds_that_name(tmp_path):
    """Existence at the path is not the test; being OUR inode is.

    The directory that takes the old path can already hold a file of the same name -- a
    concurrent turn that renamed ours aside and wrote its own. The name then resolves, so a
    check for existence is satisfied while the bytes just written are in the detached
    directory and the caller is told they were published. Only comparing the inode the link
    actually created separates the two.
    """
    parent = tmp_path / "sessions"
    parent.mkdir()
    target = parent / "session_map.json"
    real_link = os.link

    def swap_in_a_directory_that_already_has_the_name(src, dst, **kw):
        parent.rename(tmp_path / "detached")
        replacement = tmp_path / "sessions"
        replacement.mkdir()
        (replacement / "session_map.json").write_bytes(b"someone else's index\n")
        return real_link(src, dst, **kw)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "link", swap_in_a_directory_that_already_has_the_name)
        with pytest.raises(OSError, match="no longer resolves"):
            statefile.link_new(target, b'{"keys": ["cust-1"]}', prefix="probe-")

    # The stranger's file is untouched: the unlink goes through the pinned descriptor, so it
    # can only reach the copy in the directory ours actually landed in.
    assert target.read_bytes() == b"someone else's index\n"
    assert not (tmp_path / "detached" / "session_map.json").exists()


def test_publication_does_not_unlink_a_foreign_file_that_replaced_our_name(tmp_path):
    """The cleanup must remove OUR inode, never whatever is at the name.

    The parent is NOT detached here: it stays the live directory the pinned descriptor
    holds. What changes is the name -- a concurrent writer renames its own newer file over
    ours in that same directory after our link lands. The fresh resolution then sees a
    foreign inode, so publication is correctly refused; but the cleanup unlinks through the
    pinned descriptor, which still points at the live directory, so unlinking the name
    blindly would delete the stranger's file. It must stat the name first and remove it only
    when it is still our own inode -- on a foreign inode, unlink nothing.
    """
    parent = tmp_path / "sessions"
    parent.mkdir()
    target = parent / "session_map.json"
    real_link = os.link

    def rename_a_stranger_over_our_name(src, dst, **kw):
        # Our link lands first, then a concurrent turn replaces the name in the SAME live
        # directory with its own file (rename is atomic and clobbers ours).
        rv = real_link(src, dst, **kw)
        stranger = parent / "stranger.json"
        stranger.write_bytes(b"a concurrent writer's newer index\n")
        os.replace(stranger, target)
        return rv

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "link", rename_a_stranger_over_our_name)
        with pytest.raises(OSError, match="no longer resolves"):
            statefile.link_new(target, b'{"keys": ["cust-1"]}', prefix="probe-")

    # The stranger's file is untouched: the name resolved to a foreign inode, so the cleanup
    # unlinked nothing. Deleting it would destroy a file this writer never wrote.
    assert target.read_bytes() == b"a concurrent writer's newer index\n"


def test_publication_still_succeeds_when_the_parent_stays_put(tmp_path):
    """The over-strict direction: the reachability check must not refuse the ordinary path."""
    parent = tmp_path / "sessions"
    parent.mkdir()
    target = parent / "session_map.json"

    assert statefile.link_new(target, b'{"keys": ["cust-1"]}', prefix="probe-") is True
    assert target.read_bytes() == b'{"keys": ["cust-1"]}'
    assert [p.name for p in parent.iterdir()] == ["session_map.json"], "no temporary left"


def test_an_existing_target_is_still_reported_as_already_there(tmp_path):
    """``False`` is not an error, and the new check must not turn it into one."""
    parent = tmp_path / "sessions"
    parent.mkdir()
    target = parent / "session_map.json"
    target.write_bytes(b"older\n")

    assert statefile.link_new(target, b"newer\n", prefix="probe-") is False
    assert target.read_bytes() == b"older\n"


# --- 16. some of the authority pair is not the same as none of it --------------------


def test_one_authority_file_present_is_refused_rather_than_half_published(tmp_path):
    """No whole pair means no generation, so the file it has has nowhere readable to go.

    With no pointer committed, a restore reads the legacy keys, which this writer never
    writes -- so uploading the one file that exists puts it in a generation nothing can
    reach while the cycle reports itself complete and exits zero. The two files have
    independent writers, so a one-file window is ordinary timing skew rather than an extreme
    state, which is why it must not be the quiet path.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    (settings.config_dir / "open_slots.json").unlink()
    store = _Recorder()

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, store, state={})

    assert "open_slots.json" in [name for name, _why in caught.value.result.refused]
    # The file that IS there is withheld rather than published into an unreachable generation.
    assert not any("session_map.json" in key for key in store.objects)
    assert any("session_map.json" in key for key in caught.value.result.withheld)
    assert not any(key.endswith("generation.json") for key in store.objects)
    # The transcript still uploads: the index is what is waiting, not the conversation.
    assert _blob_key(settings, b"a turn\n") in store.objects


def test_no_authority_file_at_all_is_still_a_clean_first_boot(tmp_path):
    """The over-strict direction. A task that has served no turn has no index to preserve.

    Refusing here would make every first boot a failing cycle and a non-zero drain, which is
    the case the module's contract calls out as explicitly not a fault.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    for name in keys.AUTHORITY_NAMES:
        (settings.config_dir / name).unlink()
    store = _Recorder()

    result = backup_mod.run_cycle(settings, store, state={})

    assert result.complete
    assert result.refused == []
    assert sorted(result.gone) == sorted(keys.AUTHORITY_NAMES)
    assert _blob_key(settings, b"a turn\n") in store.objects


def test_a_whole_pair_still_publishes_and_commits(tmp_path):
    """The third direction: the refusal is conditional on the pair being PARTIAL."""
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    store = _Recorder()

    result = backup_mod.run_cycle(settings, store, state={})

    assert result.complete
    assert len(_authority_keys_in(settings, store.objects)) == len(keys.AUTHORITY_NAMES)


# --- 17. the reserved number is the whole request, not just the transmission -----------


def test_the_reserved_budget_covers_the_response_wait_the_body_cannot_bound():
    """A body-enforced deadline stops at the last chunk; the request does not.

    Once the body is drained the transport waits for the RESPONSE and never asks the body
    again, so that wait is bounded only by the client's read timeout. Reserving the
    transmission alone lets a PUT finish exactly on its allowance and still spend one more
    timeout, which is the gate admitting an upload the window cannot hold.
    """
    assert (
        cfg.BACKUP_PER_OBJECT_BUDGET_SECS
        == cfg.BACKUP_TRANSMISSION_BUDGET_SECS + cfg.BACKUP_REQUEST_TIMEOUT_SECS
    )
    assert cfg.BACKUP_TRANSMISSION_BUDGET_SECS > 0


def test_the_store_hands_the_body_the_transmission_share_not_the_reservation(tmp_path):
    """The two numbers are different on purpose, and the body must get the smaller one."""
    path = tmp_path / "big.jsonl"
    path.write_bytes(b"z" * 64)
    seen: list[float | None] = []
    real_reader = objects.BoundedReader

    class _Recording(real_reader):  # type: ignore[misc,valid-type]
        def __init__(self, fh, limit, *, deadline=None, clock=None, cancel=None):
            seen.append(deadline)
            super().__init__(fh, limit, deadline=deadline, clock=clock, cancel=cancel)

    class _Client:
        def put_object(self, **kw):
            while kw["Body"].read(64):
                pass
            return {}

    from container.sidecar import store as store_mod

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(store_mod, "BoundedReader", _Recording)
        store = store_mod.S3ObjectStore("a-bucket", client=_Client())
        before = time.monotonic()
        with path.open("rb") as fh:
            store.put("crews/c/big.jsonl", fh, 64, budget=cfg.BACKUP_PER_OBJECT_BUDGET_SECS)

    assert len(seen) == 1 and seen[0] is not None
    share = seen[0] - before

    assert share <= cfg.BACKUP_TRANSMISSION_BUDGET_SECS + 1.0
    assert share < cfg.BACKUP_PER_OBJECT_BUDGET_SECS


def test_the_window_still_covers_every_step_after_the_response_allowance():
    """The reservation grew, so the window has to have grown with it."""
    needed = (
        2 * cfg.BACKUP_ATTEMPT_COST_SECS
        + _authority_reservation_count() * cfg.BACKUP_ATTEMPT_COST_SECS
        + cfg.BACKUP_PER_OBJECT_BUDGET_SECS
    )

    assert cfg.SIDECAR_DRAIN_SECS >= needed
    assert (
        cfg.FRONT_DRAIN_SECS
        + cfg.BACKEND_DRAIN_SECS
        + cfg.SIDECAR_DRAIN_SECS
        + cfg.TEARDOWN_REAP_MARGIN_SECS
    ) <= cfg.MAX_TASK_STOP_TIMEOUT_SECS


def test_the_sessions_root_is_listed_through_the_descriptor_it_checked(tmp_path):
    """One directory throughout, here too. A second lookup is a second directory.

    Checking a descended descriptor and then re-resolving the root by name lets the root be
    swapped between the two, so the listing walks the very tree the check refused while the
    check reports it sound -- the refusal is worth nothing. Listing through the descriptor is
    what makes the check and the walk address one inode.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / f"stranger{keys.TRANSCRIPT_SUFFIX}").write_bytes(b"not ours\n")
    real_descend = backup_mod._descend

    def swap_the_root_after_the_check(root, parts):
        fd = real_descend(root, parts)
        # The check has passed on the real directory. It is moved aside rather than removed,
        # so the descriptor still addresses it with its entries intact, and the NAME now
        # means the stranger's tree -- exactly what a concurrent rename leaves behind.
        settings.sessions_dir.rename(tmp_path / "moved-aside")
        settings.sessions_dir.symlink_to(elsewhere, target_is_directory=True)
        return fd

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(backup_mod, "_descend", swap_the_root_after_the_check)
        found, refused = backup_mod._live_transcripts(settings)

    assert refused == []
    assert [p.name for p in found] == [
        f"{STEM}{keys.TRANSCRIPT_SUFFIX}"
    ], "the listing followed the swapped name instead of the checked descriptor"


# --- 18. a bucket with no pointer is generation 0; there is no slot survey ----------------
#
# Each cycle publishes its pair under a WRITER-UNIQUE generation id and the durability
# process holds only ``get`` and ``put`` -- never a ``list`` (a ``list`` turns "bring back
# my own files" into "enumerate the bucket"). So a bucket with no committed pointer cannot
# be surveyed for an orphaned generation: it is read as GENERATION 0, the legacy ``data/``
# keys the previous protocol left, and a bucket holding neither those nor a pointer is a
# first boot. A generation whose pair uploaded but whose pointer PUT never landed is
# unreferenced and unlisted; the next cycle mints a fresh id and commits a valid pointer,
# so a boot in that window reads generation 0 rather than adopting an orphan it cannot find.


def test_an_orphaned_generation_with_no_pointer_is_not_discovered(tmp_path):
    """A pair under a unique id whose pointer never landed reads as a first boot, not adopted.

    There is no ``list`` to find an unreferenced generation id, so a bucket holding only
    such objects and no pointer is generation 0 -- here, a first boot, because no legacy
    keys are present either. The next cycle commits a fresh pointer; nothing here silently
    boots from an orphan it cannot name.
    """
    settings = _settings(tmp_path)
    for name in keys.AUTHORITY_NAMES:
        (settings.config_dir / name).unlink()
    orphan = "00000000000000000009-fedcba9876543210"
    present = {f"gen/{orphan}/{name}": b"{}" for name in keys.AUTHORITY_NAMES}

    result = restore_mod.restore_authority(settings, _Bucket(present))

    assert sorted(result.absent) == sorted(keys.AUTHORITY_NAMES)
    assert result.restored == []


def test_an_empty_bucket_with_no_pointer_still_boots(tmp_path):
    """The over-strict direction. Nothing anywhere is the genuine first boot.

    Refusing here would strand every task that has served no turn, which is the case the
    module's contract names as explicitly not a fault.
    """
    settings = _settings(tmp_path)

    result = restore_mod.restore_authority(settings, _Bucket({}))

    assert sorted(result.absent) == sorted(keys.AUTHORITY_NAMES)
    assert result.restored == []


# --- 19. a conversation that vanishes mid-cycle must not leave the index ahead of it ----
#
# The pair is captured BEFORE the enumeration, so it still names a conversation deleted
# during the cycle. The skew argument says an OLDER index is harmless -- but only because
# every slot it names already has bytes in the bucket, and a conversation created and
# deleted inside one interval was never uploaded by any cycle. Withholding here cannot
# freeze the index the way an unreachable entry would: a deleted file is not listed by the
# next cycle at all, so the verdict is a race within one cycle rather than a shape on disk.


def _vanish_after_listing(settings, monkeypatch, victim: str):
    """Delete *victim* between the listing and its open, which is the whole race."""
    real_open = backup_mod.open_snapshot

    def open_then_vanish(path, *, root):
        if path.name == victim and path.exists():
            path.unlink()
        return real_open(path, root=root)

    monkeypatch.setattr(backup_mod, "open_snapshot", open_then_vanish)


def _index_naming(settings, *slots: str) -> None:
    """Write a session map and open-slots pair that NAME *slots*, as the backend would.

    *slots* are SLOT KEYS (``cust-91``), which is what the backend writes -- NOT transcript
    stems (``dashboard_cust-91``). Passing a stem here is what made this suite agree with a
    comparison that could never match in production.
    """
    for slot in slots:
        assert not slot.startswith("dashboard_"), (
            f"{slot!r} is a transcript stem, not a slot key: the index names conversations "
            "by slot key and the file carries the prefix"
        )
    (settings.config_dir / "session_map.json").write_bytes(
        json.dumps({slot: {"sid": f"sid-{slot}"} for slot in slots}).encode()
    )
    (settings.config_dir / "open_slots.json").write_bytes(
        json.dumps({"keys": list(slots)}).encode()
    )


def test_a_gone_transcript_the_index_names_withholds_the_pair(tmp_path, monkeypatch):
    """The hole: the captured pair names it, the bucket has never held it, and it commits.

    The replacement then restores an index naming a slot that resolves to nothing, and the
    front reads that as a conversation that never had history.
    """
    settings = _settings(tmp_path)
    victim = f"{STEM}{keys.TRANSCRIPT_SUFFIX}"
    _transcript(settings, b"a turn\n")
    _index_naming(settings, SLOT_KEY)
    _vanish_after_listing(settings, monkeypatch, victim)
    store = _Recorder()

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, store, state={})

    result = caught.value.result
    assert result.gone_referenced == [victim]
    assert not result.complete
    assert set(result.withheld) == _authority_keys_in(settings, result.withheld)
    assert len(result.withheld) == len(keys.AUTHORITY_NAMES)
    assert not any(key.endswith("generation.json") for key in store.objects)
    assert not any("session_map.json" in key for key in store.objects)
    assert "captured index still named it" in str(caught.value)


def test_a_gone_transcript_the_index_does_not_name_still_publishes(tmp_path, monkeypatch):
    """The routine case, and the reason membership is the test rather than the deletion.

    An owner deleting a conversation is ordinary use. If the captured index does not name
    it, the published pair cannot send a reader to it, so withholding would cost every
    other conversation its index update and protect nothing.
    """
    settings = _settings(tmp_path)
    victim = f"{STEM}{keys.TRANSCRIPT_SUFFIX}"
    _transcript(settings, b"a turn\n")
    _index_naming(settings, "some-other-conversation")
    _vanish_after_listing(settings, monkeypatch, victim)
    store = _Recorder()

    result = backup_mod.run_cycle(settings, store, state={})

    assert result.gone == [victim]
    assert result.gone_undurable == [(victim, victim)]
    assert result.gone_referenced == []
    assert result.complete
    assert result.withheld == []
    assert len(_authority_keys_in(settings, store.objects)) == len(keys.AUTHORITY_NAMES)


def test_a_gone_transcript_the_bucket_already_holds_still_publishes(tmp_path, monkeypatch):
    """The harmless skew, which must stay harmless: those bytes ARE in the bucket.

    An older index naming a conversation the bucket still holds resolves to real bytes, so
    the name is not a candidate at all even though the index names it.
    """
    settings = _settings(tmp_path)
    victim = f"{STEM}{keys.TRANSCRIPT_SUFFIX}"
    _transcript(settings, b"a turn\n")
    _index_naming(settings, SLOT_KEY)
    store = _Recorder()

    state: dict = {}
    first = backup_mod.run_cycle(settings, store, state=state)
    assert first.complete

    _vanish_after_listing(settings, monkeypatch, victim)
    result = backup_mod.run_cycle(settings, store, state=state)

    assert result.gone == [victim]
    assert result.gone_undurable == []
    assert result.gone_referenced == []
    assert result.complete


def test_a_captured_index_that_cannot_be_read_withholds_rather_than_assuming_empty(
    tmp_path, monkeypatch
):
    """Unknown is not "names nothing". Guessing empty commits the one pair least checkable.

    The candidate is undurable either way, so reading the index as empty would publish an
    index this cycle could not read against bytes it knows are absent.
    """
    settings = _settings(tmp_path)
    victim = f"{STEM}{keys.TRANSCRIPT_SUFFIX}"
    _transcript(settings, b"a turn\n")
    (settings.config_dir / "session_map.json").write_bytes(b"{ not json")
    (settings.config_dir / "open_slots.json").write_bytes(b"{}")
    _vanish_after_listing(settings, monkeypatch, victim)

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, _Recorder(), state={})

    assert caught.value.result.gone_referenced == [victim]
    assert not caught.value.result.complete


def test_a_gone_transcript_does_not_withhold_on_the_following_cycle(tmp_path, monkeypatch):
    """Why this is not the frozen index an unreachable entry would cause.

    A file that is really deleted is not listed by the next cycle, so the verdict cannot
    recur and the pair publishes without any intervention.
    """
    settings = _settings(tmp_path)
    victim = f"{STEM}{keys.TRANSCRIPT_SUFFIX}"
    _transcript(settings, b"a turn\n")
    _index_naming(settings, SLOT_KEY)
    _vanish_after_listing(settings, monkeypatch, victim)
    store = _Recorder()
    state: dict = {}

    with pytest.raises(backup_mod.BackupIncomplete):
        backup_mod.run_cycle(settings, store, state=state)
    monkeypatch.undo()
    assert not (settings.sessions_dir / victim).exists(), "the victim really is deleted"

    second = backup_mod.run_cycle(settings, store, state=state)

    assert second.gone_referenced == []
    assert second.complete
    assert len(_authority_keys_in(settings, store.objects)) == len(keys.AUTHORITY_NAMES)


def test_an_open_slot_alone_is_enough_to_name_a_conversation(tmp_path, monkeypatch):
    """Both files name conversations, so reading only the session map would miss one.

    An open tab is a slot the front will fetch on the next turn, which is exactly the read
    that must not meet an absent object.
    """
    settings = _settings(tmp_path)
    victim = f"{STEM}{keys.TRANSCRIPT_SUFFIX}"
    _transcript(settings, b"a turn\n")
    (settings.config_dir / "session_map.json").write_bytes(b"{}")
    (settings.config_dir / "open_slots.json").write_bytes(json.dumps({"keys": [SLOT_KEY]}).encode())
    _vanish_after_listing(settings, monkeypatch, victim)

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, _Recorder(), state={})

    assert caught.value.result.gone_referenced == [victim]


def test_an_authority_file_that_is_simply_absent_is_not_this_case(tmp_path):
    """The over-strict direction: a first boot has no index to preserve and nothing to say.

    ``authority_gone`` is a different list from the data phase's, and reading the two as one
    would make every task that has served no turn a failing cycle.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    for name in keys.AUTHORITY_NAMES:
        (settings.config_dir / name).unlink()
    store = _Recorder()

    result = backup_mod.run_cycle(settings, store, state={})

    assert result.gone_referenced == []
    assert result.complete


def test_an_over_entry_cap_transcript_index_refuses_publication(tmp_path, monkeypatch):
    """GPT F2: an over-cap live set is refused DURING the scan; the pair is withheld, count reported.

    A slot repeatedly closed and reopened leaves transcripts behind, so the live set and the
    index it builds grow with the whole slot history. ``_live_transcripts`` bounds the listing
    AT the entry cap WHILE it scans -- it never materialises the whole directory -- and refuses
    the moment the directory holds more than the cap, withholding the authority pair so the
    last readable generation stays committed and reporting the over-count. This is the memory
    bound: enforcing the cap only downstream (on the already-materialised merged index) would
    let a pathological directory OOM the sidecar before a byte is uploaded.

    It fails if the cap is removed from the scan: the oversized listing materialises and the
    cycle reports clean (or OOMs on a real pathological directory).
    """
    monkeypatch.setattr(backup_mod, "_TRANSCRIPT_INDEX_MAX_ENTRIES", 2)
    settings = _settings(tmp_path)
    for i in range(3):  # three live transcripts, cap is two
        _transcript(settings, b"turn\n", stem=f"dashboard_cust-{i}")
    store = _Recorder()

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, store, state={})

    result = caught.value.result
    assert not result.complete
    # The authority pair is withheld, not committed.
    assert set(result.withheld) == _authority_keys_in(settings, store.puts) or not any(
        _authority_keys_in(settings, store.objects)
    )
    # The refusal is raised during the scan and reports the over-count: 3 transcripts, 1 over
    # the 2-entry cap. It names the sessions directory, not the index object, because the
    # bound is now enforced before the index is ever built.
    assert any(
        "more than the 2-entry transcript cap" in why and "1 over" in why
        for _k, why in result.refused
    )


def test_an_over_byte_cap_transcript_index_refuses_publication(tmp_path, monkeypatch):
    """The serialized-SIZE cap, independent of the entry count: a restore reads with a limit.

    The restore reads the index with ``limit=MAX_OBJECT_BYTES``; an index past that is
    ``ObjectTooLarge`` -- an unreadable bucket. So publication is refused on the byte cap too,
    well under that ceiling, keeping the committed index readable.

    It fails if only the entry count is capped: a few very long stems could still serialize
    past the reader's limit and publish.
    """
    monkeypatch.setattr(backup_mod, "_TRANSCRIPT_INDEX_MAX_BYTES", 50)
    settings = _settings(tmp_path)
    _transcript(settings, b"turn\n", stem="dashboard_" + "x" * 200)
    store = _Recorder()

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, store, state={})

    result = caught.value.result
    assert not result.complete
    assert any("over the" in why and "byte cap" in why for _k, why in result.refused)
    assert not _authority_keys_in(settings, store.objects)


# --- F2: writer ownership is claimed in shared storage before restoration --------------------


def test_claim_ownership_bumps_the_committed_incarnation_under_a_cas(tmp_path):
    """`claim_ownership` advances the committed pointer's incarnation in place, by CAS.

    The claim fences a predecessor from the instant of restoration rather than after the
    successor's first commit. It bumps the committed incarnation (same generation) under an
    ``If-Match`` on the ETag it read.

    It fails if the claim does not advance the counter, or writes a new generation rather than
    bumping in place.
    """
    settings = _settings(tmp_path)
    store = _Recorder()
    gen_id = keys.new_generation_id()
    base = keys.new_incarnation()
    store.objects[keys.authority_pointer_key(settings)] = generation_mod.pointer_body(gen_id, base)
    store.etags[keys.authority_pointer_key(settings)] = '"ptr-1"'

    claimed = generation_mod.claim_ownership(settings, store).token

    assert keys.incarnation_counter(claimed) == keys.incarnation_counter(base) + 1
    committed = generation_mod.read_pointer(settings, store)
    assert committed is not None and committed.incarnation == claimed
    assert committed.generation == gen_id  # same generation, only the incarnation advanced


def test_claim_ownership_fences_a_predecessors_later_commit(tmp_path):
    """After the successor claims, a predecessor's commit of its older incarnation is refused.

    This is the F2 fix end to end: the predecessor holds the pre-claim incarnation, the
    successor claims (bumping the committed one), and the predecessor's later commit is then
    refused by the incarnation fence rather than deleting the successor's slot.

    It fails if the claim does not raise the committed incarnation above the predecessor's --
    the predecessor's stale pair would then commit.
    """
    settings = _settings(tmp_path)
    store = _Recorder()
    gen_id = keys.new_generation_id()
    predecessor = keys.new_incarnation()
    store.objects[keys.authority_pointer_key(settings)] = generation_mod.pointer_body(
        gen_id, predecessor
    )
    store.etags[keys.authority_pointer_key(settings)] = '"ptr-1"'

    generation_mod.claim_ownership(settings, store)

    committed = generation_mod.read_pointer(settings, store)
    assert committed is not None
    # The committed token supersedes the predecessor's, and the predecessor's does not
    # supersede the committed one, so the predecessor's commit is fenced.
    assert keys.incarnation_supersedes(committed.incarnation, predecessor)
    assert not keys.incarnation_supersedes(predecessor, committed.incarnation)


def test_release_ownership_restores_the_prior_pointer_when_its_claim_still_stands(tmp_path):
    """GPT F1: an aborted startup releases its claim, restoring the predecessor's pointer.

    The claim bumps the committed incarnation before restoration. A task that never reaches
    readiness aborts without committing, so leaving the bump raised would fence a still-healthy
    predecessor forever. ``release_ownership`` restores the EXACT pre-claim bytes under a CAS,
    so the committed incarnation returns to the predecessor's own token.

    It fails if the release does not restore the prior incarnation (the permanent-fence bug).
    """
    settings = _settings(tmp_path)
    store = _Recorder()
    gen_id = keys.new_generation_id()
    predecessor = keys.new_incarnation()
    store.objects[keys.authority_pointer_key(settings)] = generation_mod.pointer_body(
        gen_id, predecessor
    )
    store.etags[keys.authority_pointer_key(settings)] = '"ptr-1"'

    claim = generation_mod.claim_ownership(settings, store)
    bumped = generation_mod.read_pointer(settings, store)
    assert bumped is not None and keys.incarnation_supersedes(bumped.incarnation, predecessor)

    released = generation_mod.release_ownership(settings, store, claim)

    assert released is True
    restored = generation_mod.read_pointer(settings, store)
    assert restored is not None
    # The predecessor's own incarnation is committed again, so the fence stops superseding it.
    assert restored.incarnation == predecessor
    assert not keys.incarnation_supersedes(restored.incarnation, predecessor)
    assert restored.generation == gen_id


def test_release_ownership_is_a_no_op_when_a_newer_successor_has_claimed(tmp_path):
    """A release that lost the pointer to a newer successor leaves that live claim in place.

    If a second successor claimed on top of the first before the first aborted, reverting to
    the first's prior bytes would clobber the second's live claim and un-fence the aborting
    task against it. The release is a CAS on the first claim's own ETag, so once the pointer
    has moved on the release is a no-op.

    It fails if the release writes unconditionally and clobbers the newer claim.
    """
    settings = _settings(tmp_path)
    store = _Recorder()
    gen_id = keys.new_generation_id()
    predecessor = keys.new_incarnation()
    store.objects[keys.authority_pointer_key(settings)] = generation_mod.pointer_body(
        gen_id, predecessor
    )
    store.etags[keys.authority_pointer_key(settings)] = '"ptr-1"'

    first = generation_mod.claim_ownership(settings, store)
    # A newer successor claims on top before the first task aborts.
    second = generation_mod.claim_ownership(settings, store)
    after_second = generation_mod.read_pointer(settings, store)
    assert after_second is not None and after_second.incarnation == second.token

    released = generation_mod.release_ownership(settings, store, first)

    assert released is False
    # The newer successor's claim is untouched.
    now = generation_mod.read_pointer(settings, store)
    assert now is not None and now.incarnation == second.token


def test_release_ownership_un_fences_the_predecessors_next_commit(tmp_path):
    """End to end: after a failed successor releases, the predecessor's commit is accepted again.

    This is the F1 fix proven at the fence: the predecessor holds its pre-claim incarnation, a
    successor claims (fencing it) then aborts and releases, and the predecessor's later commit
    -- which the raised bump would have refused as superseded -- is accepted.

    It fails if the release does not restore the predecessor's standing.
    """
    settings = _settings(tmp_path)
    store = _Recorder()
    gen_id = keys.new_generation_id()
    predecessor = keys.new_incarnation()
    store.objects[keys.authority_pointer_key(settings)] = generation_mod.pointer_body(
        gen_id, predecessor
    )
    store.etags[keys.authority_pointer_key(settings)] = '"ptr-1"'

    claim = generation_mod.claim_ownership(settings, store)
    committed_after_claim = generation_mod.read_pointer(settings, store)
    assert committed_after_claim is not None
    # While claimed, the predecessor would be fenced.
    assert keys.incarnation_supersedes(committed_after_claim.incarnation, predecessor)

    generation_mod.release_ownership(settings, store, claim)

    committed_after_release = generation_mod.read_pointer(settings, store)
    assert committed_after_release is not None
    # The predecessor is not superseded, so its next commit is accepted.
    assert not keys.incarnation_supersedes(committed_after_release.incarnation, predecessor)


def test_a_claim_whose_put_commits_but_loses_its_ack_is_released_not_stranded(tmp_path):
    """GPT F2: a claim PUT that commits on S3 but whose ack is lost does not strand the claim.

    ``store.put`` can land the bytes and still raise -- a degraded link dropping the response
    after the SDK's retries. ``claim_ownership`` would then raise before returning a
    ClaimedOwnership, so the supervisor's abort has no claim object to release, yet the
    committed incarnation was bumped, fencing a live predecessor for good. The recovery
    re-reads the pointer on an ambiguous failure, sees this claim's token committed, and
    CAS-releases it back to the committed owner before re-raising the original failure.

    It fails if the ambiguous PUT failure is not recovered: the committed-but-unacked claim's
    token stays on the pointer, and the predecessor stays fenced.
    """

    class _LostAckOnClaim(_Recorder):
        def __init__(self) -> None:
            super().__init__()
            self._raised = False

        def put(self, key, body, size, **kw):
            # The claim write (if_match on the pointer, claim body) commits, then its ack is
            # lost exactly once -- the recovery's own release PUT is allowed through.
            is_claim_put = (
                key == keys.authority_pointer_key(settings)
                and kw.get("if_match") is not None
                and not self._raised
            )
            etag = super().put(key, body, size, **kw)  # the bytes land
            if is_claim_put:
                self._raised = True
                raise backup_mod.StoreUnusable("the claim PUT response was lost after it committed")
            return etag

    settings = _settings(tmp_path)
    store = _LostAckOnClaim()
    gen_id = keys.new_generation_id()
    predecessor = keys.new_incarnation()
    predecessor_body = generation_mod.pointer_body(gen_id, predecessor)
    store.objects[keys.authority_pointer_key(settings)] = predecessor_body
    store.etags[keys.authority_pointer_key(settings)] = '"ptr-1"'

    # The original failure still propagates: the task refuses to start, it could not confirm
    # ownership.
    with pytest.raises(backup_mod.StoreUnusable):
        generation_mod.claim_ownership(settings, store)

    # But the committed-but-unacked claim was released: the pointer is back to the
    # predecessor's own incarnation, so the predecessor is not left fenced.
    committed = generation_mod.read_pointer(settings, store)
    assert committed is not None
    assert committed.incarnation == predecessor
    assert not committed.is_claim


def test_claim_disables_rollback_when_a_concurrent_write_lands_before_the_reread(tmp_path):
    """GPT F1: the release validator comes from the claim's own PUT, not a fallible reread.

    The release's CAS validator is the ETag the claim's own write RETURNS (S3's PutObject
    response), never a follow-up GET. A confirmation GET after the claim can fail or time out
    on a degraded link even though the PUT already committed -- and that exception would escape
    before any ClaimedOwnership exists to release, leaving a live predecessor fenced for good.
    So this test makes every GET after the claim's PUT raise, and asserts the claim still
    returns with a usable release validator and the abort path then reverts the predecessor.

    It fails if claim_ownership performs a post-PUT confirmation read (the F1 crash: an
    exception there escapes and no release is possible).
    """
    settings = _settings(tmp_path)
    gen_id = keys.new_generation_id()
    predecessor = keys.new_incarnation()

    class _NoReadAfterClaimPut(_Recorder):
        """Any GET of the pointer AFTER the claim's PUT raises, as a degraded link would."""

        def __init__(self) -> None:
            super().__init__()
            self._claim_written = False

        def put(self, key, body, size, **kw):
            etag = super().put(key, body, size, **kw)
            if key == keys.authority_pointer_key(settings):
                self._claim_written = True
            return etag

        def get(self, key, *, limit, deadline=None, cancel=None):
            if self._claim_written and key == keys.authority_pointer_key(settings):
                raise ObjectAbsent(key)  # stand-in for a GET that fails on a degraded link
            return super().get(key, limit=limit, deadline=deadline)

    store = _NoReadAfterClaimPut()
    store.objects[keys.authority_pointer_key(settings)] = generation_mod.pointer_body(
        gen_id, predecessor
    )
    store.etags[keys.authority_pointer_key(settings)] = '"ptr-1"'

    # The claim must NOT read the pointer back after its PUT; it returns a usable validator.
    claim = generation_mod.claim_ownership(settings, store)
    assert claim.prior_etag is not None
    assert claim.prior_body == generation_mod.pointer_body(gen_id, predecessor)

    # The abort path can therefore revert, restoring the live predecessor's own incarnation.
    assert generation_mod.release_ownership(settings, store, claim) is True
    store._claim_written = False  # let the read through to verify the restored state
    restored = generation_mod.read_pointer(settings, store)
    assert restored is not None and restored.incarnation == predecessor
    assert not keys.incarnation_supersedes(restored.incarnation, predecessor)


def test_release_after_a_newer_claim_is_a_no_op_cas(tmp_path):
    """GPT F1: a release whose claim a newer successor has superseded is a no-op via CAS.

    If another successor claims on top of this one, this claim's release CAS (on the ETag its
    own write left) fails to match the committed pointer, so the revert is refused rather
    than clobbering the newer, live claim.
    """
    settings = _settings(tmp_path)
    store = _Recorder()
    gen_id = keys.new_generation_id()
    predecessor = keys.new_incarnation()
    store.objects[keys.authority_pointer_key(settings)] = generation_mod.pointer_body(
        gen_id, predecessor
    )
    store.etags[keys.authority_pointer_key(settings)] = '"ptr-1"'

    claim = generation_mod.claim_ownership(settings, store)
    # A newer successor claims on top, moving the pointer's ETag past this claim's.
    newer = generation_mod.claim_ownership(settings, store)
    assert newer.token != claim.token

    # This claim's release cannot match the committed ETag now: no-op, newer claim stands.
    assert generation_mod.release_ownership(settings, store, claim) is False
    now = generation_mod.read_pointer(settings, store)
    assert now is not None and now.incarnation == newer.token


def test_release_restores_the_carried_committed_owner_through_a_nested_claim(tmp_path):
    """GPT F3: a nested claim restores the LIVE committed owner, not a dead claimant's token.

    When this claim bumps from a pointer that was ITSELF a provisional claim (an overlapping
    task that also claimed before readiness and is also aborting), the committed owner the
    first claim bumped from is carried forward in ``committed_incarnation``. The release then
    restores THAT real predecessor, so the live writer is un-fenced -- not the dead first
    claimant's token.

    It fails if the release restores the immediate prior pointer (the dead-claimant-token bug)
    or refuses to revert at all (leaving the live predecessor fenced).
    """
    settings = _settings(tmp_path)
    store = _Recorder()
    gen_id = keys.new_generation_id()
    live_predecessor = keys.new_incarnation()
    dead_claim_token = keys.next_incarnation(live_predecessor)
    # The committed pointer is a CLAIM by a prior overlapping (also aborting) task, and it
    # carries the real committed owner it bumped from.
    store.objects[keys.authority_pointer_key(settings)] = generation_mod.pointer_body(
        gen_id, dead_claim_token, claim=True, committed_incarnation=live_predecessor
    )
    store.etags[keys.authority_pointer_key(settings)] = '"ptr-claim"'

    claim = generation_mod.claim_ownership(settings, store)
    # The claim carried the live committed owner forward, not the dead claimant's token.
    assert claim.prior_body == generation_mod.pointer_body(gen_id, live_predecessor)

    released = generation_mod.release_ownership(settings, store, claim)

    # The release restores the LIVE predecessor, not the dead claimant's token.
    assert released is True
    now = generation_mod.read_pointer(settings, store)
    assert now is not None and now.incarnation == live_predecessor
    assert now.is_claim is False
    assert not keys.incarnation_supersedes(now.incarnation, live_predecessor)


def test_claim_ownership_pre_mints_a_base_token_at_generation_zero_without_writing(tmp_path):
    """GPT F4: generation 0 mints a base token for the sidecar to adopt but writes no pointer.

    There is no committed pointer to CAS-bump, and writing one would commit an empty
    generation and strand the first real commit's legacy migration. But the task still mints
    its base token (counter 1) BEFORE restoration and returns it so the supervisor exports it
    and the sidecar ADOPTS it -- rather than the sidecar deriving a HIGHER token from a
    predecessor's first commit that lands in the restore-to-sidecar window and then
    superseding and overwriting it. Nothing is written, so there is nothing to release.

    It fails if the claim writes a pointer at generation 0, or returns an empty token the
    sidecar would then derive past.
    """
    settings = _settings(tmp_path)
    store = _Recorder()

    claimed = generation_mod.claim_ownership(settings, store)

    # A base token (counter 1) is minted and will be exported for the sidecar to adopt.
    assert claimed.token != ""
    assert keys.incarnation_counter(claimed.token) == 1
    # Nothing was written and there is nothing to release.
    assert claimed.prior_body is None
    assert claimed.prior_etag is None
    assert keys.authority_pointer_key(settings) not in store.objects


def test_interval_preflight_reads_forward_the_stop_predicate_to_their_gets(tmp_path):
    """GPT F2: an interval cycle's pointer and index GETs are cut by the stop, not run to end.

    An interval cycle has no deadline (it has a next interval) but its preflight reads -- the
    pointer and the possibly-multi-megabyte transcript index -- must still be cut the instant
    a stop arrives, or a trickling read would run across the SIGTERM and eat the drain window
    the final cycle needs. The reads thread the stop predicate through to the store GET so the
    body is cut between chunks.

    This asserts the predicate reaches the store's get/get_with_etag; it fails if either read
    drops it (the F2 bug: deadline=None and no stop means an unbounded interval GET).
    """
    settings = _settings(tmp_path)
    gen_id = keys.new_generation_id()
    seen_cancel: dict[str, object] = {}

    class _CancelRecorder(_Recorder):
        def get(self, key, *, limit, deadline=None, cancel=None):
            seen_cancel[f"get:{key}"] = cancel
            return super().get(key, limit=limit, deadline=deadline)

        def get_with_etag(self, key, *, limit, deadline=None, cancel=None):
            seen_cancel[f"etag:{key}"] = cancel
            return super().get_with_etag(key, limit=limit, deadline=deadline)

    store = _CancelRecorder()
    store.objects[keys.authority_pointer_key(settings)] = generation_mod.pointer_body(
        gen_id, keys.new_incarnation()
    )
    store.etags[keys.authority_pointer_key(settings)] = '"ptr-1"'
    store.objects[keys.transcript_index_key(settings, gen_id)] = b"{}"

    def _stop() -> bool:
        return False

    generation_mod.read_pointer(settings, store, cancel=_stop)
    generation_mod.read_transcript_index(settings, store, gen_id, cancel=_stop)

    # Both preflight reads forwarded the stop predicate to the store GET (F2). Without the
    # fix, the store would have been handed cancel=None and the interval read would be
    # unbounded across a SIGTERM.
    assert seen_cancel[f"etag:{keys.authority_pointer_key(settings)}"] is _stop
    assert seen_cancel[f"get:{keys.transcript_index_key(settings, gen_id)}"] is _stop


def test_the_sidecar_adopts_the_claimed_incarnation_from_the_environment(tmp_path, monkeypatch):
    """The sidecar uses the supervisor's claimed token rather than minting a second one.

    If it re-derived its own, it would bump a second time and leave the committed pointer
    naming a token no live task holds -- the writer's own first commit fenced against its own
    claim. Adopting the exported token is what makes the pre-restore claim and the writer one
    ownership.

    It fails if ``startup_incarnation`` ignores the env var and derives from the pointer.
    """
    settings = _settings(tmp_path)
    store = _Recorder()
    claimed = keys.next_incarnation(keys.new_incarnation())
    monkeypatch.setenv(generation_mod.ENV_CLAIMED_INCARNATION, claimed)

    assert backup_mod.startup_incarnation(settings, store) == claimed


# --- F3: a transient startup pointer read propagates rather than minting a base token --------


def test_startup_incarnation_propagates_an_unreadable_pointer(tmp_path):
    """A present-but-unreadable pointer is NOT swallowed into a base token.

    A base token minted from an unreadable pointer is strictly less than a predecessor's
    committed one, so the fence would refuse every commit for the task's life. Propagating
    ``PointerUnusable`` makes the writer refuse to start instead -- a restart re-reads the
    pointer, which a transient fault clears.

    It fails if the handler returns a base token (the permanent-fence regression).
    """
    settings = _settings(tmp_path)

    class _PointerUnreadable(_Recorder):
        def get_with_etag(
            self,
            key: str,
            *,
            limit: int,
            deadline: float | None = None,
            cancel: Callable[[], bool] | None = None,
        ) -> tuple[bytes, str | None]:
            if key == keys.authority_pointer_key(settings):
                raise RuntimeError("SlowDown reading the pointer")
            return super().get_with_etag(key, limit=limit, deadline=deadline)

    with pytest.raises(generation_mod.PointerUnusable):
        backup_mod.startup_incarnation(settings, _PointerUnreadable())


def test_an_unreadable_startup_pointer_refuses_the_boot_with_a_distinct_exit_code(
    tmp_path, monkeypatch
):
    """`main` ends non-zero on an unreadable startup pointer rather than running a doomed writer.

    The exit code is the only signal the supervisor reads, so the refusal has to reach it --
    and distinctly from the bucket-unusable code, since the cause and remedy differ.

    It fails if the process mints a base token and runs, committing nothing for its whole life.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"turn\n")
    monkeypatch.setattr(sidecar_main.common, "load", lambda: settings)
    monkeypatch.setattr(signal, "signal", lambda *_args: None)

    class _PointerUnreadable(_Recorder):
        def get_with_etag(
            self,
            key: str,
            *,
            limit: int,
            deadline: float | None = None,
            cancel: Callable[[], bool] | None = None,
        ) -> tuple[bytes, str | None]:
            if key == keys.authority_pointer_key(settings):
                raise RuntimeError("SlowDown reading the pointer")
            return super().get_with_etag(key, limit=limit, deadline=deadline)

    monkeypatch.setattr(sidecar_main, "S3ObjectStore", lambda bucket: _PointerUnreadable())

    code = sidecar_main.main([])
    assert code != 0
    assert code != 3  # distinct from the bucket-unusable exit


# --- Opus: a failed replacement-blob PUT must not erase the prior committed blob's record ----


def test_a_refused_new_blob_put_keeps_the_prior_committed_blob_recorded_durable(tmp_path):
    """A changing transcript whose new blob PUT fails must NOT drop the prior durable key.

    A transcript that gains turns takes a fresh content key; the prior key is genuinely
    durable (its blob is in the bucket and the committed index names it). The eviction of
    that prior key must happen only once the NEW key is recorded durable -- never before the
    PUT. If the new PUT is refused (a transport fault), the prior key must stay in *state* so
    a conversation the owner deletes next cycle is still judged durable against its committed
    blob, rather than wrongly withholding the pair on a lossless cycle.

    It fails if the eviction runs before the PUT (the original ordering): the prior key is
    popped and the refused new key is never recorded, so neither is in *state*.
    """
    settings = _settings(tmp_path)
    path = _transcript(settings, b"turn 1\n")
    store = _Recorder()
    state = backup_mod.DurableState()
    # Cycle 1: the first blob lands and is recorded durable.
    backup_mod.run_cycle(settings, store, state=state)
    prior_key = state.live_blobs[STEM]
    assert prior_key in state

    # The transcript gains a turn -> a new content key. The store refuses the NEW blob's PUT
    # (a transport fault), but the pointer/authority objects still succeed.
    path.write_bytes(b"turn 1\nturn 2\n")
    new_digest = __import__("hashlib").sha256(b"turn 1\nturn 2\n").hexdigest()
    new_key = keys.blob_key(settings, new_digest)

    class _RefusesTheNewBlob(_Recorder):
        def __init__(self, seed: _Recorder) -> None:
            super().__init__()
            self.objects = dict(seed.objects)
            self.etags = dict(seed.etags)

        def put(self, key: str, body, size: int, **kw) -> str | None:
            if key == new_key:
                raise RuntimeError("SlowDown writing the new blob")
            return super().put(key, body, size, **kw)

    store2 = _RefusesTheNewBlob(store)
    try:
        backup_mod.run_cycle(settings, store2, state=state)
    except backup_mod.BackupIncomplete:
        pass  # the refused blob makes the cycle incomplete, which is expected

    # The new key was refused, so it is NOT durable -- and the prior key must STILL be
    # recorded, because the eviction only runs once the new key lands.
    assert new_key not in state, "a refused PUT must not record the new key durable"
    assert prior_key in state, (
        "the prior committed blob key was evicted before its replacement landed -- a failed "
        "PUT erased the proof the still-committed blob is durable"
    )


# --- GPT F1: a superseded sidecar ends the process instead of serving uncommittable state ----


def test_run_cycle_raises_sidecar_superseded_on_the_incarnation_fence(tmp_path):
    """A superseded commit raises SidecarSuperseded, a distinct cause from BackupIncomplete.

    The incarnation fence records a refusal when a newer task's incarnation is committed.
    That refusal must surface as SidecarSuperseded, not a plain BackupIncomplete, so the loop
    can tell a never-clearing supersession apart from an ordinary retryable incomplete cycle.

    It fails if the superseded refusal is an undifferentiated BackupIncomplete: the loop would
    retry it forever while the front keeps accepting turns no cycle can commit.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"turn\n")
    # A committed pointer carrying a NEWER incarnation than this task's.
    committed_token = "00000000000000000009-ffffffffffffffff"
    this_token = "00000000000000000003-0000000000000000"
    store = _Recorder()
    committed_gen = keys.new_generation_id()
    body = generation_mod.pointer_body(committed_gen, committed_token)
    store.objects[keys.authority_pointer_key(settings)] = body
    store.etags[keys.authority_pointer_key(settings)] = '"etag"'
    # The committed generation's (empty) transcript index must exist, or the cycle refuses on
    # an unreadable index BEFORE it reaches the commit fence -- the fence is what this tests.
    store.objects[keys.transcript_index_key(settings, committed_gen)] = b"{}"

    with pytest.raises(backup_mod.SidecarSuperseded):
        backup_mod.run_cycle(
            settings, store, state=backup_mod.DurableState(), incarnation=this_token
        )


def test_a_superseded_sidecar_ends_the_process_non_zero_rather_than_looping(tmp_path):
    """run() ends non-zero on a supersession instead of retrying it every interval.

    A superseded task can never commit again, so looping is the appearance of durability
    while its front accepts lost turns. run() must let SidecarSuperseded end the process --
    the same posture it takes for StoreUnusable.

    It fails if the loop swallows the supersession as an ordinary failed cycle and keeps
    running (max_cycles would be reached and run() would return 0).
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"turn\n")
    committed_token = "00000000000000000009-ffffffffffffffff"
    store = _Recorder()
    committed_gen = keys.new_generation_id()
    body = generation_mod.pointer_body(committed_gen, committed_token)
    store.objects[keys.authority_pointer_key(settings)] = body
    store.etags[keys.authority_pointer_key(settings)] = '"etag"'
    store.objects[keys.transcript_index_key(settings, committed_gen)] = b"{}"
    # The sidecar adopts a token OLDER than the committed one, so every commit is fenced.
    import os as _os

    _os.environ[generation_mod.ENV_CLAIMED_INCARNATION] = "00000000000000000003-0000000000000000"
    try:
        with pytest.raises(backup_mod.SidecarSuperseded):
            sidecar_main.run(settings, store, max_cycles=5)
    finally:
        _os.environ.pop(generation_mod.ENV_CLAIMED_INCARNATION, None)


def test_a_claim_fence_is_retryable_not_a_supersession(tmp_path):
    """Opus: a newer incarnation that is a PROVISIONAL CLAIM must not kill a live predecessor.

    A replacement bumps the pointer's incarnation BEFORE restoration (claim_ownership) and may
    then fail readiness and abort, releasing the claim. If a healthy predecessor serving turns
    read that claim at its interval and raised SidecarSuperseded, it would be torn down long
    before the failed replacement's 180s-gated abort released the claim -- an outage, and the
    predecessor's turns since its last commit lost. So a commit fenced by a CLAIM pointer is
    refused as RETRYABLE (BackupIncomplete, superseded unset), and the predecessor steps aside
    only once a REAL, unmarked commit carries a newer incarnation.

    It fails if a claim fence sets result.superseded (the outage: run_cycle would raise
    SidecarSuperseded and the predecessor's process would end).
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"turn\n")
    committed_gen = keys.new_generation_id()
    # A newer incarnation, but written as a provisional CLAIM (claim=True), as a replacement's
    # pre-restore ownership bump is. The predecessor adopts an OLDER token.
    claim_token = "00000000000000000009-ffffffffffffffff"
    predecessor = "00000000000000000003-0000000000000000"
    store = _Recorder()
    store.objects[keys.authority_pointer_key(settings)] = generation_mod.pointer_body(
        committed_gen, claim_token, claim=True
    )
    store.etags[keys.authority_pointer_key(settings)] = '"etag"'
    store.objects[keys.transcript_index_key(settings, committed_gen)] = b"{}"
    state = backup_mod.DurableState()

    # A claim fence is refused RETRYABLY: run_cycle raises BackupIncomplete, NOT
    # SidecarSuperseded, so the predecessor's loop keeps it alive and retries next interval.
    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, store, state=state, incarnation=predecessor)
    assert not isinstance(caught.value, backup_mod.SidecarSuperseded)

    # A REAL (unmarked) newer commit, by contrast, DOES supersede the predecessor terminally.
    store.objects[keys.authority_pointer_key(settings)] = generation_mod.pointer_body(
        committed_gen, claim_token
    )
    store.etags[keys.authority_pointer_key(settings)] = '"etag2"'
    with pytest.raises(backup_mod.SidecarSuperseded):
        backup_mod.run_cycle(settings, store, state=state, incarnation=predecessor)
