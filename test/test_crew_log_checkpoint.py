"""Fold savepoints on disk -- one test per property the files promise.

The load-bearing one is :func:`test_a_resumed_fold_equals_a_cold_fold`: a read that
resumes from a savepoint must reach the value a read that folds the whole file
reaches. Everything else here is a reason NOT to resume -- a file describing a
different log, a log that lost its front, a log shorter than the savepoint, a
payload this build cannot read -- and each one is checked by proving the fold still
lands on the cold answer.
"""

from __future__ import annotations

import gc
import hashlib
import json
import time
from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import checkpoint as savepoints
from kiro_crew.crew_log import lease
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import store

SESSION = "s-savepoint"
GATEWAY = "gateway"

#: Turns that put the log past :data:`savepoints.MIN_ADVANCE_ENTRIES`, so a write
#: is owed. Four entries per turn, and the opener makes one more.
_LONG_TURNS = (savepoints.MIN_ADVANCE_ENTRIES // 4) + 4


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


def _turn_items(turn: int) -> list[dict[str, Any]]:
    """The four entries of one whole turn, unwritten."""
    return [
        {"type": "turn/started", "data": {"turn": turn, "actor": "user", "depth": 0}},
        {"type": "step/started", "data": {"turn": turn, "step": 1}},
        {"type": "step/completed", "data": {"turn": turn, "step": 1, "ms": 120}},
        {
            "type": "turn/completed",
            "data": {
                "turn": turn,
                "stop_reason": "end_turn",
                "depth": 0,
                "duration_ms": 900,
                "model": "opus",
                "provider": "kiro",
                "credits": 0.5,
                "tokens": {"input": 100, "output": 20, "cache_read": 5, "cache_write": 1},
            },
        },
    ]


def _every_fold_items() -> list[dict[str, Any]]:
    """Entries that drive the tool and approval folds, not only the counters.

    The digest pinned by
    :func:`test_changing_what_a_fold_stores_forces_the_savepoint_version_to_move`
    only sees the fold paths its script reaches, so the script has to reach all
    five rather than the three a plain turn exercises.
    """
    return [
        {
            "type": "tool/called",
            "data": {
                "name": "fs_read",
                "server": "builtin",
                "kind": "read",
                "call_id": "c-1",
                "turn": 3,
            },
        },
        {
            "type": "tool/completed",
            "data": {
                "name": "fs_read",
                "server": "builtin",
                "call_id": "c-1",
                "status": "ok",
                "elapsed_ms": 42,
                "turn": 3,
            },
        },
        {
            "type": "tool/called",
            "data": {
                "name": "shell",
                "server": "builtin",
                "kind": "exec",
                "call_id": "c-2",
                "turn": 3,
            },
        },
        {
            "type": "tool/completed",
            "data": {
                "name": "shell",
                "server": "builtin",
                "call_id": "c-2",
                "status": "error",
                "is_error": True,
                "elapsed_ms": 7,
                "turn": 3,
            },
        },
        {
            "type": "approval/requested",
            "data": {"approval_id": "a-1", "tool": "shell", "reason": "writes", "turn": 3},
        },
        {
            "type": "approval/decided",
            "data": {
                "approval_id": "a-1",
                "decision": "allow",
                "by": "raymond",
                "cause": "asked",
                "turn": 3,
            },
        },
    ]


def _state_digest(state: dict[str, Any]) -> str:
    """A stable digest of one fold's stored state."""
    blob = json.dumps(state, sort_keys=True, ensure_ascii=True).encode("ascii")
    return hashlib.sha256(blob).hexdigest()[:16]


#: What each fold STORES over the script above, recorded at
#: :data:`_DIGESTS_RECORDED_AT_VERSION`. A fold whose meaning changes while its
#: keys do not moves its digest here, which is what obliges the version bump that
#: retires savepoints written by the older build.
_FOLD_STATE_DIGESTS: dict[str, str] = {
    "status": "c0fcfd81e27d700b",
    "usage": "c56df0d14126410f",
    "timeline": "ca89b3c765575d9a",
    "tools": "008b36fed498d32b",
    "approvals": "c9db629215cc2620",
}

#: The savepoint version the digests above were taken at.
_DIGESTS_RECORDED_AT_VERSION = 1


def _log(unit_id: str = SESSION) -> CrewLog:
    handle = CrewLog.create(lg.KIND_SESSION, unit_id, owner="raymond", agent="kirocrew")
    handle.append(
        "session/opened",
        {
            "agent": "kirocrew",
            "slot": "dashboard:1",
            "model": "opus",
            "cwd": "/w",
            "owner": "raymond",
            "resumed": False,
        },
        src=GATEWAY,
    )
    return handle


def _grow(handle: CrewLog, turns: int, *, first: int = 1) -> None:
    """*turns* whole turns appended in bounded groups.

    ``append_many`` rather than one ``append`` per entry: a fixture long enough to
    cross :data:`savepoints.MIN_ADVANCE_ENTRIES` is hundreds of entries, and one
    lock plus one fsync each is what makes such a fixture slow enough to fail a
    shard on the slowest runner.
    """
    items: list[dict[str, Any]] = []
    for turn in range(first, first + turns):
        items.extend(_turn_items(turn))
    for start in range(0, len(items), 256):
        handle.append_many(items[start : start + 256], src=GATEWAY)


def _long_log(unit_id: str = SESSION) -> CrewLog:
    """A log past the write threshold, so folding it owes a savepoint."""
    handle = _log(unit_id)
    _grow(handle, _LONG_TURNS)
    return handle


def _dir(unit_id: str = SESSION):
    return savepoints.checkpoint_dir(lg.KIND_SESSION, unit_id)


def _files(unit_id: str = SESSION) -> list[str]:
    directory = _dir(unit_id)
    return sorted(child.name for child in directory.iterdir()) if directory.is_dir() else []


def _payload(name: str, unit_id: str = SESSION) -> dict[str, Any]:
    return json.loads(savepoints.checkpoint_path(lg.KIND_SESSION, unit_id, name).read_text())


def _write_payload(name: str, payload: dict[str, Any], unit_id: str = SESSION) -> None:
    path = savepoints.checkpoint_path(lg.KIND_SESSION, unit_id, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _cold_bundle(
    names: tuple[str, ...] = crew_log.PROJECTION_NAMES, *, handle: CrewLog | None = None
) -> crew_log.SessionProjections:
    """Every *names* fold over the whole log, built and persisting NOTHING.

    Through the module's own public pure surface -- ``initial`` then ``advance``
    over the log's entries -- rather than through a switch on ``fold_session``.
    That switch would be a public parameter with no production caller, and this
    needs none: the from-scratch answer IS `advance` from an empty checkpoint, so
    the oracle these tests compare against is the documented whole-file form
    instead of a second mode of the function under test.
    """
    reader = handle if handle is not None else CrewLog.open(lg.KIND_SESSION, SESSION)
    checkpoints = {
        name: crew_log.advance(
            crew_log.initial(name), reader.iter_from(1, known=crew_log.KNOWN_TYPES)
        )
        for name in names
    }
    return crew_log.SessionProjections(
        session_id=SESSION,
        last_seq=reader.last_seq,
        checkpoints=checkpoints,
        origin=crew_log.log_origin(reader),
        saved_seq=0,
    )


def _rendered(bundle: crew_log.SessionProjections) -> dict[str, Any]:
    return {name: proj.to_dict() for name, proj in bundle.rendered().items()}


def _with(
    bundle: crew_log.SessionProjections, checkpoints: dict[str, crew_log.Checkpoint]
) -> crew_log.SessionProjections:
    """*bundle* carrying *checkpoints* instead of its own."""
    return crew_log.SessionProjections(
        session_id=bundle.session_id,
        last_seq=bundle.last_seq,
        checkpoints=checkpoints,
        origin=bundle.origin,
        saved_seq=bundle.saved_seq,
    )


def _drop_the_log(unit_id: str = SESSION) -> None:
    """Delete the unit's segments, leaving the directory and its savepoints.

    A recreated log is what the identity check exists for, and this is the cheap
    way to reach one: unlinking the segments lets ``CrewLog.create`` stamp a fresh
    ``created_at`` on a new inode under the same id, which is exactly the state a
    session that was deleted and reopened leaves behind.
    """
    for segment in store.segment_paths(lg.KIND_SESSION, unit_id):
        segment.unlink()


def _recreate_distinctly(turns: int, unit_id: str = SESSION) -> CrewLog:
    """Replace the unit's log with a new one of *turns* turns, distinguishably.

    The pause is the load-bearing part. A log's identity combines its header's
    ``created_at`` -- stamped in epoch MILLISECONDS -- with the file's device and
    inode, so a log deleted and recreated inside one millisecond onto a recycled
    inode is indistinguishable from the original. Recreating immediately in a
    scratch directory hits exactly that: the same millisecond is likely and the
    just-freed inode is commonly handed straight back, which made these tests pass
    or fail with the run's timing. Waiting past a millisecond tick makes the
    identity differ by construction, so what the test measures is the guard rather
    than the clock. The narrow collision itself is a property of the identity these
    tests do not own.
    """
    time.sleep(0.003)
    _drop_the_log(unit_id)
    fresh = _log(unit_id)
    _grow(fresh, turns)
    return fresh


def _release_writers() -> None:
    """Let every unreferenced crew log handle release its lease.

    A handle claims the unit's lease on its first append and releases it from a
    ``weakref.finalize``, so a removal taking the lease ``sole`` reports the unit
    as OWNED until the writer is collected. Naming the collection is what keeps
    such a test about removal rather than about reference counting.
    """
    gc.collect()


def _spy_on_reads(monkeypatch) -> list[int]:
    """The ``from_seq`` of every pass over a log this test makes."""
    seen: list[int] = []
    real = CrewLog.iter_from

    def spy(self, from_seq=1, *args, **kwargs):
        seen.append(from_seq)
        return real(self, from_seq, *args, **kwargs)

    monkeypatch.setattr(CrewLog, "iter_from", spy)
    return seen


# --------------------------------------------------------------------------- #
# Writing one
# --------------------------------------------------------------------------- #


def test_a_folded_session_writes_one_savepoint_per_fold():
    handle = _long_log()
    bundle = crew_log.fold_session(SESSION)

    assert _files() == sorted(f"{name}.json" for name in crew_log.PROJECTION_NAMES)
    assert bundle.saved_seq == bundle.last_seq == handle.last_seq
    payload = _payload("status")
    assert payload["fold"] == "status"
    assert payload["seq"] == handle.last_seq
    assert payload["unit"] == SESSION
    assert payload["v"] == savepoints.CHECKPOINT_VERSION
    assert payload["first_seq"] == 1
    assert payload["origin"] == crew_log.log_origin(handle)
    # The state is the fold's own bookkeeping, not the rendered value: that
    # distinction is what keeps the render free to change shape.
    assert payload["state"]["turns_completed"] == _LONG_TURNS


def test_a_short_session_leaves_no_savepoint():
    """A log a cold fold reads cheaply is not worth a file."""
    handle = _log()
    _grow(handle, 3)
    bundle = crew_log.fold_session(SESSION)

    assert not _dir().exists()
    assert bundle.saved_seq == 0
    assert bundle.projection("status").value["turns_completed"] == 3


def test_the_cold_oracle_these_tests_compare_against_matches_a_real_read():
    """The builder above must be the same answer ``fold_session`` reaches.

    Every rejection test asserts the fold "lands on the cold answer", so the cold
    answer has to be trustworthy on its own. This pins it against a real read of a
    session with no savepoint yet, where the two must agree by construction.
    """
    _long_log()

    oracle = _cold_bundle()
    real = crew_log.fold_session(SESSION)

    assert _rendered(oracle) == _rendered(real)
    # And the builder itself persists nothing, which is what lets the rejection
    # tests use it without first perturbing the state they are about to check.
    assert oracle.saved_seq == 0


def test_a_second_read_that_barely_grew_rewrites_nothing():
    """A savepoint is allowed to lag, which is what keeps the write off every growth."""
    handle = _long_log()
    first = crew_log.fold_session(SESSION)
    before = _payload("status")["seq"]

    _grow(handle, 2, first=_LONG_TURNS + 1)
    again = crew_log.fold_session(SESSION, since=first)

    assert _payload("status")["seq"] == before
    assert again.saved_seq == first.saved_seq
    assert again.last_seq > again.saved_seq
    # Lagging costs nothing a reader can see.
    assert again.projection("status").value["turns_completed"] == _LONG_TURNS + 2


def test_one_requested_fold_writes_only_its_own_file():
    _long_log()
    crew_log.fold_session(SESSION, ("status",))

    assert _files() == ["status.json"]


# --------------------------------------------------------------------------- #
# Resuming from one
# --------------------------------------------------------------------------- #


def test_a_resumed_fold_equals_a_cold_fold(monkeypatch):
    """The module's contract: resuming and folding from scratch reach one value."""
    handle = _long_log()
    crew_log.fold_session(SESSION)
    saved_through = _payload("status")["seq"]
    _grow(handle, 5, first=_LONG_TURNS + 1)

    cold = _cold_bundle()
    seen = _spy_on_reads(monkeypatch)
    resumed = crew_log.fold_session(SESSION)

    assert _rendered(resumed) == _rendered(cold)
    # And it resumed rather than re-read the file: the pass started after the
    # savepoint, which is the whole point of writing one.
    assert seen == [saved_through + 1]


def test_a_read_after_a_restart_resumes_from_disk(monkeypatch):
    """No bundle in hand is the case the in-memory cache cannot serve."""
    _long_log()
    crew_log.fold_session(SESSION)
    saved_through = _payload("status")["seq"]

    seen = _spy_on_reads(monkeypatch)
    crew_log.fold_session(SESSION)

    # Nothing grew, so the resumed fold reads no entries at all.
    assert seen == []
    assert saved_through > 0


def test_an_unchanged_tail_still_rechecks_the_log_identity(monkeypatch):
    """A resumed no-read pass still describes the log identity seen before it."""
    _long_log()
    expected = crew_log.fold_session(SESSION)
    seen = _spy_on_reads(monkeypatch)
    calls: list[int] = []

    def moved_once(_handle):
        calls.append(len(calls) + 1)
        return "before-recreation" if len(calls) == 1 else "settled-recreation"

    monkeypatch.setattr(crew_log, "log_origin", moved_once)
    resumed = crew_log.fold_session(SESSION)

    # The first attempt resumed at the unchanged tail and read nothing, so without
    # the recheck it would have returned there and ``seen`` would be empty. The
    # single cold read from seq 1 IS the retry, and it is what the recheck bought.
    assert seen == [1]
    # Rechecked rather than read once: the identity is sampled again after the pass.
    assert len(calls) > 2
    assert _rendered(resumed) == _rendered(expected)


def test_one_unusable_file_costs_only_its_own_fold(monkeypatch):
    """A partial savepoint set is a partial resume, never a refusal."""
    handle = _long_log()
    crew_log.fold_session(SESSION)
    savepoints.checkpoint_path(lg.KIND_SESSION, SESSION, "tools").unlink()
    _grow(handle, 2, first=_LONG_TURNS + 1)

    cold = _cold_bundle()
    seen = _spy_on_reads(monkeypatch)
    resumed = crew_log.fold_session(SESSION)

    assert _rendered(resumed) == _rendered(cold)
    # From seq 1, because ``tools`` has to see the whole file -- and the four folds
    # that kept their savepoint still skip what they already consumed, which is why
    # one missing file is not five cold folds.
    assert seen == [1]


def test_the_load_helper_reports_nothing_for_a_session_with_no_files():
    handle = _log()
    _grow(handle, 2)

    assert savepoints.load(handle, crew_log.PROJECTION_NAMES) is None


# --------------------------------------------------------------------------- #
# Reasons not to resume
# --------------------------------------------------------------------------- #


def test_a_savepoint_past_the_end_of_the_log_is_ignored():
    """The short-store fallback: a log that does not reach the savepoint."""
    handle = _long_log()
    crew_log.fold_session(SESSION)
    payload = _payload("status")
    payload["seq"] = handle.last_seq + 500
    payload["state"]["turns_completed"] = 99999
    _write_payload("status", payload)

    cold = _cold_bundle()
    resumed = crew_log.fold_session(SESSION)

    assert resumed.projection("status").value == cold.projection("status").value
    assert resumed.projection("status").value["turns_completed"] == _LONG_TURNS


def test_a_savepoint_whose_front_moved_is_ignored():
    """Retention drops segments off the front, so the two folds would differ."""
    _long_log()
    crew_log.fold_session(SESSION)
    payload = _payload("status")
    payload["first_seq"] = 2
    payload["state"]["turns_completed"] = 99999
    _write_payload("status", payload)

    cold = _cold_bundle()
    resumed = crew_log.fold_session(SESSION)

    assert resumed.projection("status").value == cold.projection("status").value


def test_a_savepoint_from_a_recreated_log_is_ignored():
    """Same id, different file: its seqs start again and mean something else.

    The recreated log is grown PAST the stale savepoint's seq on purpose. Within
    reach of it, the short-store guard rejects the file and this test would pass
    with the identity check deleted -- so it would pin nothing it is named for.
    """
    _long_log()
    crew_log.fold_session(SESSION)
    stale = _payload("status")

    handle = _recreate_distinctly(_LONG_TURNS + 8)
    _write_payload("status", stale)
    assert stale["seq"] <= handle.last_seq, "the seq guard must not be what rejects this"
    assert stale["first_seq"] == 1, "the front guard must not be what rejects this"
    assert stale["origin"] != crew_log.log_origin(handle)

    cold = _cold_bundle()
    resumed = crew_log.fold_session(SESSION)

    assert resumed.projection("status").value == cold.projection("status").value
    assert resumed.projection("status").value["turns_completed"] == _LONG_TURNS + 8


def test_a_savepoint_naming_another_fold_is_ignored():
    """A renamed or copied file says what it is, and it is checked."""
    _long_log()
    crew_log.fold_session(SESSION)
    payload = _payload("status")
    payload["fold"] = "usage"
    payload["state"]["turns_completed"] = 99999
    _write_payload("status", payload)

    cold = _cold_bundle()
    resumed = crew_log.fold_session(SESSION)

    assert resumed.projection("status").value == cold.projection("status").value


def test_a_savepoint_naming_another_unit_is_ignored():
    _long_log()
    crew_log.fold_session(SESSION)
    payload = _payload("status")
    payload["unit"] = "s-somebody-else"
    payload["state"]["turns_completed"] = 99999
    _write_payload("status", payload)

    resumed = crew_log.fold_session(SESSION)

    assert resumed.projection("status").value["turns_completed"] == _LONG_TURNS


def test_a_savepoint_from_a_future_build_is_ignored():
    _long_log()
    crew_log.fold_session(SESSION)
    payload = _payload("status")
    payload["v"] = savepoints.CHECKPOINT_VERSION + 1
    payload["state"]["turns_completed"] = 99999
    _write_payload("status", payload)

    resumed = crew_log.fold_session(SESSION)

    assert resumed.projection("status").value["turns_completed"] == _LONG_TURNS


def test_changing_what_a_fold_stores_forces_the_savepoint_version_to_move(monkeypatch):
    """A fold's stored state is pinned, so changing it cannot pass CI silently.

    ``CHECKPOINT_VERSION`` and ``_state_matches_fold`` both guard the payload's
    SHAPE, and the case neither can see is a fold whose meaning changes while its
    keys do not -- a counting fix in ``usage`` or ``status`` being the likely one.
    A savepoint written by the old build then resumes onto the new logic, so the
    long sessions this module exists to speed up are exactly the ones that keep
    serving pre-fix numbers, for the life of the unit.

    The obligation is therefore recorded as a test rather than as a sentence:
    prose cannot fail, and a rule nothing enforces is one a future fix forgets.
    The digest is over each fold's STATE, which is what a savepoint stores, so
    editing a comment or renaming a local does not move it and a changed number
    does. ``render`` is deliberately outside it: a rendering change moves the cold
    fold and the resumed fold together, so an old savepoint stays valid.

    The clock is frozen because four of the five folds retain an entry's ``ts``,
    which would otherwise move every digest on every run. ``store.now_ms`` is the
    one clock the log stamps entries from, so pinning it is what makes the state
    a function of the script alone.
    """
    monkeypatch.setattr(store, "now_ms", lambda: 1_700_000_000_000)

    handle = _log()
    _grow(handle, 2)
    handle.append_many(_every_fold_items(), src=GATEWAY)
    entries = list(handle.iter_from(1, known=crew_log.KNOWN_TYPES))

    measured = {
        name: _state_digest(crew_log.advance(crew_log.initial(name), entries).state)
        for name in crew_log.PROJECTION_NAMES
    }

    assert savepoints.CHECKPOINT_VERSION == _DIGESTS_RECORDED_AT_VERSION, (
        "CHECKPOINT_VERSION moved, so re-record _FOLD_STATE_DIGESTS at the new "
        "version: the point of the bump is that savepoints from the old one retire "
        "to a cold fold, and this pin is what proves the bump was not forgotten"
    )
    assert measured == _FOLD_STATE_DIGESTS, (
        "a fold now stores something different, so every savepoint on disk "
        "describes the OLD meaning and will resume onto this logic. Bump "
        f"CHECKPOINT_VERSION in checkpoint.py (currently {savepoints.CHECKPOINT_VERSION}) "
        "so those files retire to a cold fold, then record the new digests here: "
        f"{measured}"
    )


def test_a_corrupt_savepoint_is_ignored_rather_than_raised():
    _long_log()
    crew_log.fold_session(SESSION)
    savepoints.checkpoint_path(lg.KIND_SESSION, SESSION, "status").write_text(
        '{"v": 1, "fold": "sta', encoding="utf-8"
    )

    cold = _cold_bundle()
    resumed = crew_log.fold_session(SESSION)

    assert resumed.projection("status").value == cold.projection("status").value


def test_a_savepoint_refused_by_the_fold_surface_is_ignored():
    """The state is validated by the same code a caller-supplied checkpoint is."""
    _long_log()
    crew_log.fold_session(SESSION)
    payload = _payload("status")
    payload["state"] = "not an object"
    _write_payload("status", payload)

    resumed = crew_log.fold_session(SESSION)

    assert resumed.projection("status").value["turns_completed"] == _LONG_TURNS


def test_malformed_object_fold_state_reaches_the_cold_answer():
    """An object with missing keys or a wrong stable JSON kind is not resumable."""
    _long_log()
    crew_log.fold_session(SESSION)
    payload = _payload("status")
    valid_state = payload["state"]
    cold = _cold_bundle()

    for malformed in ({}, {**valid_state, "entries": "many"}):
        payload["state"] = malformed
        _write_payload("status", payload)
        resumed = crew_log.fold_session(SESSION)
        assert resumed.projection("status").value == cold.projection("status").value


def test_a_fold_whose_state_is_over_the_cap_is_not_written(monkeypatch):
    _long_log()
    monkeypatch.setattr(savepoints, "MAX_CHECKPOINT_BYTES", 64)
    bundle = crew_log.fold_session(SESSION)

    assert _files() == []
    assert bundle.saved_seq == 0
    # The read it was folded for is unaffected: a missing savepoint costs time.
    assert bundle.projection("status").value["turns_completed"] == _LONG_TURNS


def test_a_savepoint_file_over_the_cap_is_not_read(monkeypatch):
    _long_log()
    crew_log.fold_session(SESSION)
    payload = _payload("status")
    payload["state"]["pad"] = "x" * (savepoints.MAX_CHECKPOINT_BYTES + 10)
    payload["state"]["turns_completed"] = 99999
    _write_payload("status", payload)

    resumed = crew_log.fold_session(SESSION)

    assert resumed.projection("status").value["turns_completed"] == _LONG_TURNS


# --------------------------------------------------------------------------- #
# Against removal
# --------------------------------------------------------------------------- #


def test_a_lone_surrogate_in_fold_state_still_writes_and_reloads():
    """A crew log's own JSON admits one, so a fold can retain one in a label.

    With a non-ASCII-escaping serializer the UTF-8 encode raises out of a function
    that promises never to raise, and the projection route answers 500 for a file
    the store accepted.
    """
    handle = _long_log()
    bundle = _cold_bundle(("status",))
    poisoned = bundle.checkpoints["status"]
    poisoned.state["model"] = "opus-\ud800"

    returned = savepoints.save(handle, poisoned_bundle := _with(bundle, {"status": poisoned}))

    assert returned.saved_seq == poisoned_bundle.last_seq
    assert _payload("status")["state"]["model"] == "opus-\ud800"
    reloaded = savepoints.load(handle, ("status",))
    assert reloaded is not None
    assert reloaded.checkpoints["status"].state["model"] == "opus-\ud800"


def test_no_savepoint_is_written_while_another_owner_holds_the_unit():
    """Removal takes the lease ``sole``; a reader that cannot share it writes nothing.

    The writer handle is dropped before the sole lease is taken, because the lease
    is refcounted per process: a handle that has appended holds a shared claim, and
    ``sole`` is refused while any claim exists. So the log is built, its writer is
    collected, the sole lease is taken, and only then is a read handle opened.
    """
    _long_log()
    _release_writers()
    sole = lease.acquire(
        store.crew_log_dir(lg.KIND_SESSION, SESSION) / lease.LEASE_FILE,
        kind=lg.KIND_SESSION,
        unit_id=SESSION,
        sole=True,
    )
    try:
        handle = CrewLog.open(lg.KIND_SESSION, SESSION)
        bundle = _cold_bundle(handle=handle)
        returned = savepoints.save(handle, bundle)
    finally:
        lease.release(sole)

    assert _files() == []
    assert returned.saved_seq == 0


def test_a_unit_whose_segments_are_gone_is_not_touched_at_all():
    """Not one file, including a lease.

    Removal unlinks a unit's lease LAST, so a directory can outlive its segments
    with no lease file in it -- and taking the lease creates one, putting a reader's
    file into a unit that is already gone. Establishing the log's identity stats
    the newest segment, so it fails first and the lease is never reached, which is
    what makes "writes nothing" mean nothing at all rather than nothing durable.

    The writer is dropped first and the handle reopened for reading, because the
    lease is refcounted per process: while a writer's claim is alive, ``acquire``
    shares it and never reaches the file, so nothing would be created either way
    and this test would pass with the guard deleted.
    """
    _long_log()
    _release_writers()
    handle = CrewLog.open(lg.KIND_SESSION, SESSION)
    bundle = _cold_bundle(handle=handle)
    unit_dir = store.crew_log_dir(lg.KIND_SESSION, SESSION)
    for child in sorted(unit_dir.iterdir()):
        child.unlink()

    returned = savepoints.save(handle, bundle)

    assert list(unit_dir.iterdir()) == []
    assert returned.saved_seq == 0


def test_a_read_of_a_removed_unit_leaves_no_directory_behind():
    """``atomic_write`` creates its target's parents, so a write can rebuild the tree.

    Driving the real save path rather than ``_ensure_dir`` alone is the point: the
    directory guard does not hold on its own, and what makes the property true is
    the identity check, the lease and the teardown together.

    The writer is dropped and the handle reopened for reading before anything is
    unlinked. Windows refuses to unlink a file another descriptor holds open, and
    the writer holds the unit's lease file, so emptying the directory under a live
    writer raises ``PermissionError`` there while passing on POSIX.
    """
    _long_log()
    _release_writers()
    handle = CrewLog.open(lg.KIND_SESSION, SESSION)
    bundle = _cold_bundle(handle=handle)
    unit_dir = store.crew_log_dir(lg.KIND_SESSION, SESSION)
    for child in sorted(unit_dir.iterdir()):
        child.unlink()
    unit_dir.rmdir()

    returned = savepoints.save(handle, bundle)

    assert not unit_dir.exists(), "a reader must not resurrect a removed unit"
    assert returned.saved_seq == 0


def test_a_savepoint_written_as_the_unit_disappears_is_discarded(monkeypatch):
    """A writer that unlinks segments without the lease is still cleaned up after."""
    handle = _long_log()
    bundle = _cold_bundle()
    unit_dir = store.crew_log_dir(lg.KIND_SESSION, SESSION)
    real = savepoints._save_one

    def vanish(directory, checkpoint, **kwargs):
        written = real(directory, checkpoint, **kwargs)
        for segment in store.segment_paths(lg.KIND_SESSION, SESSION):
            segment.unlink()
        return written

    monkeypatch.setattr(savepoints, "_save_one", vanish)
    returned = savepoints.save(handle, bundle)

    assert not _dir().exists()
    assert returned.saved_seq == 0
    # The unit directory survives only because the store's own control files are
    # still in it; a removal unlinks those itself, and the lease last of all.
    assert not [child for child in unit_dir.iterdir() if not child.name.startswith(".")]


def test_a_recreated_unit_directory_is_torn_down_with_the_savepoints():
    """Nothing else collects an empty unit directory, so the reader removes its own.

    The retention sweep decides from a unit's own entries, and a unit with no
    segments has none, so a directory rebuilt by ``atomic_write``'s parent creation
    would sit under the sessions root forever.

    The state is CONSTRUCTED and the teardown driven directly, because reaching it
    from inside a save would mean unlinking the unit's lease file while the save
    holds it open: POSIX allows that, Windows refuses it with ``PermissionError``.
    The constructed state is the real one anyway -- a removal unlinks that lease
    last, so what a late write rebuilds is a directory holding nothing but the
    savepoints.
    """
    _long_log()
    crew_log.fold_session(SESSION)
    assert _dir().is_dir(), "the savepoints must exist for the teardown to remove them"
    _release_writers()
    handle = CrewLog.open(lg.KIND_SESSION, SESSION)
    unit_dir = store.crew_log_dir(lg.KIND_SESSION, SESSION)
    for child in sorted(unit_dir.iterdir()):
        if child.is_file():
            child.unlink()

    assert savepoints._discard_if_unit_gone(handle, _dir()) is True
    assert not unit_dir.exists()


def test_a_log_recreated_mid_fold_is_folded_again_rather_than_spliced(monkeypatch):
    """``iter_from`` opens by name, so the entries can come from a different file.

    The seqs do not say so -- a recreated log starts its own again -- which is why
    the identity is read after the pass as well as before it.

    The identity is driven directly rather than by recreating the file and hoping
    the inode changes. ``log_origin`` reads ``created_at`` from the handle's CACHED
    header, so across a recreation it compares device and inode alone, and a
    just-freed inode is commonly handed straight back -- which decided this test by
    the run's timing rather than by the code. What is pinned here is the control
    flow: a mismatch after the pass folds again instead of serving spliced state.
    """
    handle = _long_log()
    crew_log.fold_session(SESSION)
    # Growth after the savepoint, so the next fold actually reads the file: with
    # nothing to consume it returns before ``iter_from`` is ever called.
    _grow(handle, 3, first=_LONG_TURNS + 1)
    reads: list[int] = []
    real = CrewLog.iter_from
    truth = crew_log.log_origin
    seen: list[None] = []

    def recreate_once(self, from_seq=1, *args, **kwargs):
        entries = list(real(self, from_seq, *args, **kwargs))
        if not reads:
            reads.append(from_seq)
            _drop_the_log()
            _grow(_log(), 3)
        return iter(entries)

    def moved(target):
        seen.append(None)
        # Different only while the first pass is in flight, so the second attempt
        # sees a settled file and its bundle is the one served.
        return f"swapped-{len(seen)}" if len(seen) <= 2 else truth(target)

    monkeypatch.setattr(CrewLog, "iter_from", recreate_once)
    monkeypatch.setattr(crew_log, "log_origin", moved)
    bundle = crew_log.fold_session(SESSION, ("status",))

    assert reads, "the fold did not read the file, so nothing was exercised"
    # The second attempt folded the file as it stands, so the value describes it
    # alone rather than the first attempt's state carried onto it.
    assert bundle.projection("status").value["turns_completed"] == 3


def test_a_log_changing_identity_on_every_pass_reports_it_as_unknown(monkeypatch):
    """Two races in a row: the value is served, and nothing may reuse or persist it."""
    _long_log()
    counter = iter(range(100))
    monkeypatch.setattr(crew_log, "log_origin", lambda _target: f"moved-{next(counter)}")

    bundle = crew_log.fold_session(SESSION, ("status",))

    assert bundle.origin is None
    assert bundle.saved_seq == 0
    assert _files() == []
    # Served, not refused: the session exists and the panel still renders.
    assert bundle.projection("status").value["turns_completed"] == _LONG_TURNS


def test_removing_a_unit_takes_its_savepoints_with_it():
    _long_log()
    crew_log.fold_session(SESSION)
    assert _dir().is_dir()
    _release_writers()

    removed = store.remove_unit(lg.KIND_SESSION, SESSION, guard=lambda _dir: True)

    assert removed == store.REMOVE_REMOVED
    assert not store.crew_log_dir(lg.KIND_SESSION, SESSION).exists()


def test_a_savepoint_file_does_not_count_as_a_segment():
    """The store reads its own segments by name, and this file is not one."""
    handle = _long_log()
    crew_log.fold_session(SESSION)

    assert store.segment_first_seqs(lg.KIND_SESSION, SESSION) == [1]
    reopened = CrewLog.open(lg.KIND_SESSION, SESSION)
    assert reopened.last_seq == handle.last_seq
