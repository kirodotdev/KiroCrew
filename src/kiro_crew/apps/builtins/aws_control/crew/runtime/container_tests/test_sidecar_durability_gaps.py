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
"""

from __future__ import annotations

import json
import pathlib
import shutil
import threading
import time
from pathlib import Path

import pytest
from container.common import config as cfg
from container.common import keys, objects, statefile
from container.sidecar import __main__ as sidecar_main
from container.sidecar import backup as backup_mod
from container.sidecar import generation as generation_mod
from container.sidecar import restore as restore_mod
from container.sidecar.store import ObjectAbsent

from ._settings_helper import make_settings

STEM = "dashboard_cust-91"


def _settings(tmp_path: Path, *, interval: int = 60):
    s = make_settings(tmp_path, crew="crew-91", prefix="crews")
    for name in keys.AUTHORITY_NAMES:
        (s.config_dir / name).write_bytes(b"{}")
    return s.__class__(**{**s.__dict__, "backup_interval_secs": interval})


def _transcript(settings, payload: bytes, stem: str = STEM) -> Path:
    path = settings.sessions_dir / f"{stem}{keys.TRANSCRIPT_SUFFIX}"
    path.write_bytes(payload)
    return path


class _Recorder:
    """Accepts every put, remembers bytes, and counts cycles by their first key."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []

    def put(self, key: str, body, size: int) -> None:
        self.objects[key] = body.read(size)
        self.puts.append(key)

    def get(self, key: str, *, limit: int) -> bytes:  # pragma: no cover - unused
        raise ObjectAbsent(key)


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
    key = keys.transcript_key(settings, STEM)

    class _SignallingRecorder(_Recorder):
        """Sets the stop mid-upload, then writes what the backend's drain would flush."""

        def put(self, key_: str, body, size: int) -> None:
            super().put(key_, body, size)
            if key_ == key and not stop.is_set():
                stop.set()
                path.write_bytes(b"before-flush\nflushed-on-drain\n")

    store = _SignallingRecorder()

    assert sidecar_main.run(settings, store, stop=stop) == 0
    assert store.objects[key] == b"before-flush\nflushed-on-drain\n"


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
        def put(self, key: str, body, size: int) -> None:
            raise RuntimeError("SlowDown")

        def get(self, key: str, *, limit: int) -> bytes:  # pragma: no cover - unused
            raise ObjectAbsent(key)

    assert sidecar_main.run(settings, _Throttled(), stop=stop) == 1


# --- 2. a permanent failure ends the process ------------------------------------


class _DeniedStore:
    """Every put is refused with a code no retry resolves."""

    def __init__(self) -> None:
        self.attempts = 0

    def put(self, key: str, body, size: int) -> None:
        self.attempts += 1
        raise objects.StoreUnusable(f"PutObject on s3://b/{key} failed with AccessDenied")

    def get(self, key: str, *, limit: int) -> bytes:  # pragma: no cover - unused
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

        def put(self, key: str, body, size: int) -> None:
            self.attempts += 1
            raise RuntimeError("SlowDown")

        def get(self, key: str, *, limit: int) -> bytes:  # pragma: no cover - unused
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
        def put(self, key: str, body, size: int) -> None:  # pragma: no cover - unused
            raise AssertionError("restore does not put")

        def get(self, key: str, *, limit: int) -> bytes:
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
    segment.write_bytes(b"older half\n")
    store = _Recorder()

    backup_mod.run_cycle(settings, store, state={})

    assert store.objects[keys.data_key(settings, segment)] == b"older half\n"


# --- 5. the index is published last, and only when the bytes are there ------------


def _authority_keys(settings, slot: str = keys.AUTHORITY_SLOTS[0]) -> set[str]:
    """The keys a cycle publishing into *slot* writes the pair to.

    Slot ``a`` by default, because a bucket with no pointer has no committed generation
    and :func:`backup.target_slot` then publishes into the first slot.
    """
    return {keys.authority_slot_key(settings, slot, name) for name in keys.AUTHORITY_NAMES}


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

    authority = _authority_keys(settings)
    pointer = keys.authority_pointer_key(settings)
    last_data = max(i for i, key in enumerate(store.puts) if key not in authority | {pointer})
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
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / f"{STEM}-0001{keys.TRANSCRIPT_SUFFIX}").write_bytes(b"older half\n")
    shutil.rmtree(settings.archive_dir)
    settings.archive_dir.symlink_to(elsewhere, target_is_directory=True)
    store = _Recorder()

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, store, state={})

    assert _authority_keys(settings).isdisjoint(store.objects)
    assert set(caught.value.result.withheld) == _authority_keys(settings)


def test_a_clean_cycle_still_publishes_the_authority_files(tmp_path):
    """The withholding is conditional. A cycle that reaches everything publishes both."""
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    store = _Recorder()

    result = backup_mod.run_cycle(settings, store, state={})

    assert _authority_keys(settings) <= set(store.objects)
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

    published = keys.authority_slot_key(settings, keys.AUTHORITY_SLOTS[0], "open_slots.json")
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


def test_a_deadline_with_room_left_uploads_normally(tmp_path):
    """The bound must not cost an ordinary drain the objects it had time for."""
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    store = _Recorder()

    result = backup_mod.run_cycle(settings, store, state={}, deadline=time.monotonic() + 3600)

    assert result.refused == []
    assert keys.data_key(settings, settings.sessions_dir / f"{STEM}{keys.TRANSCRIPT_SUFFIX}") in (
        store.objects
    )


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


def test_the_index_phase_will_not_start_a_put_it_has_half_an_attempt_for(tmp_path):
    """Enough for the read alone is not enough: the connect can spend the whole timeout."""
    settings = _settings(tmp_path)
    plan = backup_mod.objects_to_back_up(settings)
    result = backup_mod.CycleResult()
    try:
        backup_mod._commit_authority(
            plan.authority,
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
            slot=keys.AUTHORITY_SLOTS[0],
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
            store=_Recorder(),
            state={},
            result=result,
            deadline=time.monotonic() - 1,
        )
    finally:
        plan.close_authority()

    assert len(result.withheld) == len(keys.AUTHORITY_NAMES)
    assert result.uploaded == []


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


def test_a_cycle_that_publishes_one_authority_file_commits_no_generation(tmp_path):
    """A generation is committed holding a WHOLE pair or it is not committed at all.

    A crew whose backend has written one of the two publishes that one, with no failure
    anywhere. A generation committed with just it would assert that a complete publication
    one file, and every later restore would boot from it and let the backend flush its own
    empty view of the other -- reintroducing, through the pointer, the loss the protocol
    here to prevent.
    """
    settings = _settings(tmp_path)
    (settings.config_dir / keys.AUTHORITY_NAMES[0]).unlink()
    _transcript(settings, b"a turn\n")
    store = _Recorder()

    result = backup_mod.run_cycle(settings, store, state={})

    assert keys.authority_pointer_key(settings) not in store.puts
    assert keys.AUTHORITY_NAMES[0] in result.gone


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
        def put(self, key: str, body, size: int) -> None:
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
        def put(self, key: str, body, size: int) -> None:
            if key == pointer:
                raise RuntimeError("SlowDown")
            super().put(key, body, size)

    store = _PointerFails()

    result = backup_mod.run_cycle(settings, store, state={})

    assert result.complete, "an interval cycle has a next cycle, so it waits rather than fails"
    assert pointer in result.withheld
    assert result.refused == []


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
        def get(self, key: str, *, limit: int) -> bytes:
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
        def get(self, key: str, *, limit: int) -> bytes:
            assert key == pointer, "the cycle reads nothing but the pointer"
            raise RuntimeError("InternalError")

    result = backup_mod.run_cycle(settings, _PointerUnreadable(), state={})

    assert result.complete, "an interval cycle re-reads the pointer, so it waits rather than fails"
    assert result.refused == []
    assert sorted(result.withheld) == sorted(
        keys.authority_slot_key(settings, keys.AUTHORITY_SLOTS[0], name)
        for name in keys.AUTHORITY_NAMES
    )


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
        f"gen/{keys.AUTHORITY_SLOTS[0]}/{name}": b'{"from": "the committed generation"}'
        for name in keys.AUTHORITY_NAMES
    }
    for name in keys.AUTHORITY_NAMES:
        present[f"data/{name}"] = b'{"from": "generation zero"}'
    present[keys.AUTHORITY_POINTER_NAME] = _pointer()
    bucket = _Bucket(present)

    restore_mod.restore_authority(settings, bucket)

    fetched = [key for key in bucket.gets if key.endswith(".json")]
    assert all(
        f"gen/{keys.AUTHORITY_SLOTS[0]}/" in key for key in fetched if "authority" not in key
    )
    assert not any(key.endswith(f"data/{name}") for key in fetched for name in keys.AUTHORITY_NAMES)


@pytest.mark.parametrize(
    "committed, expected",
    [
        (None, keys.AUTHORITY_SLOTS[0]),
        (keys.AUTHORITY_SLOTS[0], keys.AUTHORITY_SLOTS[1]),
        (keys.AUTHORITY_SLOTS[1], keys.AUTHORITY_SLOTS[0]),
    ],
)
def test_a_cycle_never_publishes_into_the_committed_slot(committed, expected):
    """The one property everything else rests on, pinned in all three states.

    A cycle that wrote the committed slot would be editing the pair a replacement is
    entitled to read, and an interruption halfway through would leave that pair torn with
    the pointer still vouching for it. Writing the other slot makes the damage invisible to
    every reader, because no reader looks there until the pointer moves.
    """
    assert backup_mod.target_slot(committed) == expected
    assert backup_mod.target_slot(committed) != committed


# --- 11. the pair is read from the committed generation and nowhere else -----------------------


class _Bucket:
    """A bucket holding exactly the objects given, by their key's trailing name."""

    def __init__(self, present: dict[str, bytes]) -> None:
        self._present = present
        self.gets: list[str] = []

    def get(self, key: str, *, limit: int) -> bytes:
        self.gets.append(key)
        for name, raw in self._present.items():
            if key.endswith(name):
                return raw
        raise ObjectAbsent(key)

    def put(self, key: str, body, size: int) -> None:  # pragma: no cover - unused
        raise AssertionError("the restore does not write to the bucket")


class _UnreadablePointer(_Bucket):
    """A bucket whose pointer cannot be read, which is not the same as not holding one."""

    def get(self, key: str, *, limit: int) -> bytes:
        if key.endswith(keys.AUTHORITY_POINTER_NAME):
            raise PermissionError("the pointer is there and this task may not read it")
        return super().get(key, limit=limit)


def _pointer(slot: str = keys.AUTHORITY_SLOTS[0]) -> bytes:
    return generation_mod.pointer_body(slot)


@pytest.mark.parametrize("present", list(keys.AUTHORITY_NAMES))
def test_half_a_pair_with_no_pointer_is_read_as_generation_zero(tmp_path, present):
    """The case that must BOOT: one file, and no pointer saying a generation was committed.

    A crew whose backend never wrote one of the two publishes the other, with no failure
    anywhere -- the plan skips a file that is not there. Refusing that boot strands every
    replacement task on a bucket that is merely young, and nothing is at risk, because the
    file the backend then writes overwrites nothing that was ever published.
    """
    settings = _settings(tmp_path)

    result = restore_mod.restore_authority(settings, _Bucket({present: b"{}"}))

    assert present in result.restored + result.kept_local
    assert result.absent == [name for name in keys.AUTHORITY_NAMES if name != present]


@pytest.mark.parametrize("missing", list(keys.AUTHORITY_NAMES))
def test_a_committed_generation_that_lost_a_member_refuses_the_boot(tmp_path, missing):
    """The case that must REFUSE: a pair was published whole and one member is gone now.

    Starting from the remaining file lets the backend flush its own empty view of the other
    over a real conversation list. Either half is the same hazard, so both are pinned.
    """
    settings = _settings(tmp_path)
    present = {
        f"gen/{keys.AUTHORITY_SLOTS[0]}/{name}": b"{}"
        for name in keys.AUTHORITY_NAMES
        if name != missing
    }
    present[keys.AUTHORITY_POINTER_NAME] = _pointer()

    with pytest.raises(restore_mod.RestoreFailed, match="does not hold"):
        restore_mod.restore_authority(settings, _Bucket(present))


def test_a_committed_generation_holding_its_whole_pair_restores_it(tmp_path):
    """The ordinary case: a committed generation holding the pair it was committed with."""
    settings = _settings(tmp_path)
    present = {f"gen/{keys.AUTHORITY_SLOTS[0]}/{name}": b"{}" for name in keys.AUTHORITY_NAMES}
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
    pointer = json.dumps({"slot": keys.AUTHORITY_SLOTS[0], "authority": [keys.AUTHORITY_NAMES[0]]})
    present = {f"gen/{keys.AUTHORITY_SLOTS[0]}/{name}": b"{}" for name in keys.AUTHORITY_NAMES}
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
    present = {f"gen/{keys.AUTHORITY_SLOTS[0]}/{name}": b"{}" for name in keys.AUTHORITY_NAMES}
    present[keys.AUTHORITY_POINTER_NAME] = json.dumps(
        {"slot": keys.AUTHORITY_SLOTS[0], "authority": listed}
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
    """The supervisor counts its 45s from SIGTERM delivery, so this must too.

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
    """A drain with nothing to upload must not report itself as a lossy one.

    The gate exists to stop a PUT that cannot finish. An object the bucket already holds
    needs no PUT, so cutting it off records a refusal for work that was never owed -- and a
    refusal withholds the authority pair, fails the cycle and exits the task non-zero. With
    the pair reserved out of the window and a whole PUT demanded on top, that fires within
    seconds of the stop, which is the ordinary case rather than a corner.
    """
    settings = _settings(tmp_path)
    path = _transcript(settings, b"a turn\n")
    plan = backup_mod.objects_to_back_up(settings)
    key = next(k for k, p in plan.data if p.name == path.name)
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
            deadline=time.monotonic() - 1,
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
            plan.data, settings=settings, store=_Recorder(), state={}, result=result
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
            plan.data, settings=settings, store=_Recorder(), state={}, result=result
        )
    finally:
        plan.close_authority()

    assert result.gone == [path.name]
    assert result.refused == []
    assert result.complete
