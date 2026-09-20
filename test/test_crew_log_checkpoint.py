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
    "class": "96f8e5381997f6fd",
}

#: The savepoint version the digests above were taken at.
#:
#: A fold ADDED to :data:`PROJECTION_NAMES` lands its digest here without moving the
#: version, and that is not a way around the bump: the version retires savepoints
#: that would resume onto changed meaning, and a new fold has none on any disk --
#: ``_checkpoint_from`` matches a file to a fold by NAME, so no file on disk claims
#: to be this one. Bumping for a new fold would instead retire every VALID savepoint
#: of the other folds, costing each a refold to retire nothing. What obliges the bump
#: is an EXISTING fold's digest moving, and the five above are unchanged.
_DIGESTS_RECORDED_AT_VERSION = 2


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


def _spy_on_prefix_digests(monkeypatch) -> list[int]:
    """The record count of every prefix hash a test's reads perform.

    Kept as an instrument rather than an assertion. The obvious property to pin
    with it -- that a savepoint rejected on its cheap metadata never pays a full
    hash -- turned out NOT to be observable from outside ``fold_session``: the same
    call that rejects a savepoint then folds cold and WRITES a fresh one, and
    writing one must compute its digest. So a rejection and a write are
    indistinguishable through this seam, and asserting no hashing would have been
    asserting something false.
    """
    calls: list[int] = []
    real = CrewLog.raw_prefix_digest

    def spy(self, records):
        calls.append(records)
        return real(self, records)

    monkeypatch.setattr(CrewLog, "raw_prefix_digest", spy)
    return calls


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
        for name in crew_log.PROJECTION_NAMES + crew_log.INTERNAL_PROJECTION_NAMES
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


def _corrupt_one_consumed_line(unit_id: str = SESSION) -> None:
    """Make one already-folded interior line unparseable, keeping the line count.

    The point of holding the count is that every OTHER guard stays satisfied: the
    header is untouched so the identity matches, the first seq is unchanged, and
    the file still reaches the savepoint's seq. Only the prefix digest can notice,
    which is what makes the test measure the digest rather than its neighbours.
    Writers are released first because Windows refuses to rewrite a file while a
    descriptor is open on it.
    """
    _release_writers()
    path = store.segment_paths(lg.KIND_SESSION, unit_id)[0]
    lines = path.read_bytes().split(b"\n")
    interior = 5
    assert len(lines) > interior + 1, "fixture is too short to have an interior line"
    lines[interior] = b'{"seq": "this line is no longer parseable"'
    path.write_bytes(b"\n".join(lines))


def test_a_savepoint_whose_consumed_prefix_changed_is_ignored():
    """A line damaged AFTER it was folded retires the savepoint that folded it.

    This is the case the other guards cannot see. ``_iter_entries`` skips a damaged
    interior line on purpose, so a cold fold omits that entry while a savepoint
    written before the damage still carries the value it contributed -- and the
    savepoint is the one that looks clean. Identity, first seq and length all still
    match, so the prefix digest is the only thing that can reject it.
    """
    _long_log()
    crew_log.fold_session(SESSION)
    assert _files() == sorted(f"{name}.json" for name in crew_log.PROJECTION_NAMES)

    _corrupt_one_consumed_line()

    cold = _cold_bundle()
    resumed = crew_log.fold_session(SESSION)

    assert _rendered(resumed) == _rendered(cold)


def test_a_prefix_damaged_mid_fold_is_folded_again_rather_than_served(monkeypatch):
    """The digest is checked BEFORE the pass, so damage during it needs a second look.

    :func:`test_a_savepoint_whose_consumed_prefix_changed_is_ignored` covers damage
    that is already on disk when the savepoint is read. This covers damage that lands
    while the fold is in flight: the savepoint verified, the pass consumed the entries
    above it, and the prefix it was trusted for differs from the prefix on disk. The
    identity is rechecked after the pass for exactly this reason, so the two guards
    are one shape -- check, do the work, check again -- and a digest that is only
    checked once is the asymmetry this closes.
    """
    handle = _long_log()
    crew_log.fold_session(SESSION)
    # Growth after the savepoint, so the next fold actually reads the file: with
    # nothing to consume it returns before ``iter_from`` is ever called.
    _grow(handle, 3, first=_LONG_TURNS + 1)
    reads: list[int] = []
    real = CrewLog.iter_from

    def corrupt_once(self, from_seq=1, *args, **kwargs):
        entries = list(real(self, from_seq, *args, **kwargs))
        if not reads:
            # After the savepoint's digest was checked and after the entries were
            # materialized: the window the post-pass recheck exists to catch.
            reads.append(from_seq)
            _corrupt_one_consumed_line()
        return iter(entries)

    monkeypatch.setattr(CrewLog, "iter_from", corrupt_once)
    bundle = crew_log.fold_session(SESSION, ("status",))

    assert reads, "the fold did not read the file, so nothing was exercised"
    assert reads[0] > 1, "the fold started at seq 1, so it never resumed from a savepoint"
    # The file as it stands is the only honest answer: the entry reader skips the
    # damaged record, so state still carrying what it contributed is spliced. The
    # patch stays in place -- ``reads`` makes it a pass-through now, and undoing it
    # would also undo the fixture's data home, which is set on the same monkeypatch.
    assert _rendered(bundle) == _rendered(_cold_bundle(("status",)))


def test_a_savepoint_carries_the_digest_its_own_pass_read(monkeypatch):
    """A write must not hash the file itself, or it can certify bytes no fold saw.

    :func:`test_a_prefix_damaged_mid_fold_is_folded_again_rather_than_served` is this
    same window seen from the other end: there the question is what the fold SERVES,
    here it is what the fold PERSISTS. A digest read at write time is taken after the
    pass, so a consumed record that changed in between is hashed together with state
    folded from its earlier value -- and because the recorded digest and the recorded
    state then agree with each other, every later resume recomputes those same changed
    bytes, matches, and serves state a cold fold disagrees with. Nothing rechecks a
    digest that verifies, so that answer stands for the life of the unit.

    The damage lands between the pass and the write, which is the only place it can
    separate the two readings: a digest read BEFORE the pass describes the clean bytes,
    so the savepoint written from it does not match the file and is refused, and the
    next read folds cold. The value-only change is deliberate for the reason
    :func:`_alter_the_last_consumed_value` gives -- it leaves every other guard
    satisfied, so the digest is the only one that can notice.

    ``usage`` is the fold asked for because it is the one that READS the damaged
    value. Against ``status`` this test passes with the fix reverted: the change is to
    a turn's credits, which that fold does not carry, so the two answers agree and the
    assertion measures nothing.
    """
    handle = _long_log()
    crew_log.fold_session(SESSION)
    # Far enough past what is already persisted to earn a second write, so the write
    # path is actually reached. A handful of turns would return before it.
    _grow(handle, _LONG_TURNS, first=_LONG_TURNS + 1)
    writes: list[int] = []
    real = savepoints.save

    def damage_then_write(log, bundle, **kwargs):
        if not writes:
            writes.append(bundle.last_seq)
            _alter_the_last_consumed_value()
        return real(log, bundle, **kwargs)

    monkeypatch.setattr(savepoints, "save", damage_then_write)
    crew_log.fold_session(SESSION, ("usage",))
    monkeypatch.setattr(savepoints, "save", real)

    assert writes, "the fold never reached the write, so nothing was exercised"
    resumed = crew_log.fold_session(SESSION, ("usage",))

    assert _rendered(resumed) == _rendered(_cold_bundle(("usage",)))


def test_a_savepoint_records_the_digest_it_was_handed_rather_than_re_reading():
    """The write persists its caller's digest, so it cannot substitute a later reading.

    This is the mechanism behind
    :func:`test_a_savepoint_carries_the_digest_its_own_pass_read`, pinned on its own so
    a revert fails here too and not only through a fold. The caller's digest was read
    before its pass; a write that hashed the file again would quietly replace it with
    one taken after, and that replacement is the whole defect. Handing in a digest the
    file cannot produce is what tells the two apart -- it reaches disk only if nothing
    re-read -- and the same planted value proves the digest is a CHECK rather than a
    note, because the savepoint carrying it is refused on the way back in.
    """
    handle = _long_log()
    bundle = _cold_bundle(("status",))
    honest = savepoints.prefix_witness(handle, bundle.last_seq)
    assert honest is not None, "the fixture's own boundary did not resolve"
    planted = honest._replace(sha="0" * 64)
    assert planted.sha != honest.sha

    returned = savepoints.save(handle, bundle, prefix=planted)

    assert returned.saved_seq == bundle.last_seq
    assert _payload("status")["prefix_sha"] == planted.sha
    assert savepoints.load(handle, ("status",)) is None


def test_a_read_that_reused_a_cached_bundle_writes_no_savepoint():
    """A pass standing on an earlier call's state has no evidence about its prefix.

    A savepoint may only be written from a digest of the bytes its state was folded
    from. A pass that resumed from DISK has one -- the savepoint records the digest
    its own writer read, and :func:`savepoints.resumed_prefix_still_verifies` checks
    it again -- so the custody holds across the two calls. A cached bundle records no
    digest at all, and the bytes below its seq were consumed by an earlier call, so
    nothing this pass can read is evidence about them. It therefore takes no witness,
    and :func:`savepoints.save` writes nothing without one.

    What that costs is a savepoint that lags a hot cached reader until a read folds
    the prefix itself, which the module already allows for: resuming from an older
    savepoint replays the tail and reaches the same value.
    """
    handle = _long_log()
    first = crew_log.fold_session(SESSION, ("status",))
    on_disk = savepoints.load(handle, ("status",))
    assert on_disk is not None, "the first fold wrote nothing, so there is no baseline"
    # Far enough past what is persisted that the write threshold is crossed, so a
    # warm read that DID witness its prefix would reach the write.
    _grow(handle, _LONG_TURNS, first=_LONG_TURNS + 1)

    warm = crew_log.fold_session(SESSION, ("status",), since=first)

    assert warm.last_seq > first.last_seq, "the warm read consumed nothing to earn a write"
    assert savepoints.write_is_earned(warm.last_seq, first.saved_seq)
    assert warm.saved_seq == first.saved_seq
    after = savepoints.load(handle, ("status",))
    assert after is not None and after.saved_seq == on_disk.saved_seq


def test_damage_below_a_cached_bundle_is_not_written_into_a_savepoint():
    """The reason the cached path must not persist, seen through what a later read serves.

    :func:`test_a_savepoint_carries_the_digest_its_own_pass_read` closes this window
    WITHIN one pass. This is the same window across two calls, and it is the half a
    digest read before the pass cannot see: the prefix changed between the call that
    folded it and the call that would write it down, so a digest read now is honest
    about the file and wrong about the state beside it. The two then agree with each
    other, every later resume recomputes those same bytes, matches, and serves state a
    cold fold disagrees with -- for the life of the unit, because nothing rechecks a
    digest that verifies.

    Serving the cached value here is not the defect and is not what this measures: an
    incremental read has always been allowed to be stale. Writing it down is what
    makes the staleness outlive the process.

    ``usage`` is the fold asked for because it is the one that READS the damaged
    value, for the reason :func:`_alter_the_last_consumed_value` gives.
    """
    handle = _long_log()
    first = crew_log.fold_session(SESSION, ("usage",))
    # Below the cached bundle's seq, so the damage is inside the prefix that bundle
    # folded rather than in the tail this next read consumes.
    _alter_the_last_consumed_value()
    _grow(handle, _LONG_TURNS, first=_LONG_TURNS + 1)

    warm = crew_log.fold_session(SESSION, ("usage",), since=first)
    assert warm.last_seq > first.last_seq, "the warm read consumed nothing to earn a write"

    resumed = crew_log.fold_session(SESSION, ("usage",))

    assert _rendered(resumed) == _rendered(_cold_bundle(("usage",)))


def _insert_blank_interior_line(unit_id: str = SESSION) -> None:
    """Add one blank line among the entries, touching no record.

    A blank line is not a record to the entry reader, which skips it, but it IS a
    record to the raw framing the digest walks. Everything else a savepoint checks
    stays satisfied: the header is untouched, the first seq is unchanged, and every
    real record still reads exactly as it did.
    """
    _release_writers()
    path = store.segment_paths(lg.KIND_SESSION, unit_id)[0]
    lines = path.read_bytes().split(b"\n")
    interior = 5
    assert len(lines) > interior + 1, "fixture is too short to have an interior line"
    lines.insert(interior, b"")
    path.write_bytes(b"\n".join(lines))


def _alter_the_last_consumed_value(unit_id: str = SESSION) -> None:
    """Change a VALUE in the last already-folded record, keeping its seq.

    Not an unparseable line. That drops the record, which puts the log's last seq
    BELOW the savepoint's, and the past-the-end guard then refuses the savepoint for
    a reason that has nothing to do with the digest -- so the test would pass while
    the digest gap it is about stayed open. A record that stays valid at the same
    seq leaves identity, first seq and length all satisfied, which leaves the digest
    as the only guard that could notice.
    """
    _release_writers()
    path = store.segment_paths(lg.KIND_SESSION, unit_id)[0]
    lines = path.read_bytes().split(b"\n")
    last = max(index for index, line in enumerate(lines) if line.strip())
    record = json.loads(lines[last])
    assert record["type"] == "turn/completed", f"fixture's last record is {record['type']!r}"
    assert record["data"]["credits"] != 99.0
    record["data"]["credits"] = 99.0
    lines[last] = json.dumps(record, separators=(",", ":")).encode()
    path.write_bytes(b"\n".join(lines))


def test_a_blank_interior_line_does_not_shorten_what_the_prefix_digest_covers():
    """The digest must cover every record a fold consumed, not one fewer.

    Two counts of "how much of this file did the fold read" have to agree. The
    savepoint took one by seq arithmetic over the ENTRIES it folded; the digest
    takes the other by walking RAW records. A blank interior line is a record to
    the walk and not to the fold, so the walk stopped one record early and the LAST
    record the fold consumed sat outside the digest.

    Inserted rather than substituted on purpose: replacing a record leaves the raw
    count equal to the seq span, so the two counts agree and the gap never opens.
    """
    _long_log()
    _insert_blank_interior_line()
    crew_log.fold_session(SESSION)
    assert _files() == sorted(
        f"{name}.json" for name in crew_log.PROJECTION_NAMES
    ), "no savepoint was written, so there was nothing for the damage to slip past"

    _alter_the_last_consumed_value()

    cold = _cold_bundle()
    resumed = crew_log.fold_session(SESSION)

    assert _rendered(resumed) == _rendered(cold)


def test_a_savepoint_over_a_log_with_a_blank_interior_line_still_resumes(monkeypatch):
    """Counting raw records must not cost the reuse a savepoint exists for.

    The guard above needs the digest's boundary to be a RAW record count while the
    savepoint's seq stays an entry number, so the two legitimately differ by every
    blank interior line. A cross-check demanding they be EQUAL would look stricter
    and would instead refuse every savepoint written over such a log: fail-closed,
    and therefore invisible to a test that only asks whether damage is caught.
    Asserted through the seq the read STARTS from, because a cold fold reaches the
    same value and would hide a savepoint that was silently rejected.
    """
    _long_log()
    _insert_blank_interior_line()
    crew_log.fold_session(SESSION)
    saved_through = _payload("status")["seq"]
    _grow(CrewLog.open(lg.KIND_SESSION, SESSION), 3, first=_LONG_TURNS + 1)

    seen = _spy_on_reads(monkeypatch)
    resumed = crew_log.fold_session(SESSION)

    assert seen == [saved_through + 1]
    assert resumed.projection("status").value["turns_completed"] == _LONG_TURNS + 3


def test_a_savepoint_still_resumes_after_the_log_merely_grew(monkeypatch):
    """Appending must not move the digest: growth is what resuming is FOR.

    The digest covers the consumed prefix only, so entries added after a savepoint
    cannot disturb it. Asserted through the seq the read STARTS from rather than
    through the value, because a cold fold reaches the same value and would hide a
    savepoint that was silently rejected.
    """
    handle = _long_log()
    crew_log.fold_session(SESSION)
    saved_through = _payload("status")["seq"]
    _grow(handle, 3, first=_LONG_TURNS + 1)

    seen = _spy_on_reads(monkeypatch)
    resumed = crew_log.fold_session(SESSION)

    # Resumed from the savepoint and read only the tail, rather than from seq 1.
    assert seen == [saved_through + 1]
    assert resumed.projection("status").value["turns_completed"] == _LONG_TURNS + 3


def test_a_savepoint_without_a_prefix_digest_is_ignored():
    """A payload from a build before this guard is retired, not trusted.

    The alternative was bumping ``CHECKPOINT_VERSION``, and the rule that constant
    documents is about what a fold STORES, not about the envelope around it: the
    fold state here is unchanged. Rejecting the envelope field directly costs one
    cold fold per fold per unit, once, and keeps the version number meaning what it
    says.
    """
    _long_log()
    crew_log.fold_session(SESSION)
    payload = _payload("status")
    del payload["prefix_sha"]
    payload["state"]["turns_completed"] = 99999
    _write_payload("status", payload)

    resumed = crew_log.fold_session(SESSION)

    assert resumed.projection("status").value["turns_completed"] == _LONG_TURNS


def test_an_unframeable_segment_does_not_raise_on_the_digest_path():
    """The digest keeps ``load``'s promise: every failure is a cold fold.

    A segment whose bytes cannot be framed at all is damage, and the answer is the
    same as for every other unusable input -- fold from seq 1 -- rather than an
    exception out of a read the caller asked to serve.
    """
    _long_log()
    crew_log.fold_session(SESSION)
    _release_writers()
    path = store.segment_paths(lg.KIND_SESSION, SESSION)[0]
    header = path.read_bytes().split(b"\n")[0]
    path.write_bytes(header + b"\n" + b"x" * (store.MAX_ENTRY_BYTES + 64))

    resumed = crew_log.fold_session(SESSION)

    assert resumed.projection("status").value["turns_completed"] == 0


def test_a_corrupt_savepoint_is_ignored_rather_than_raised():
    _long_log()
    crew_log.fold_session(SESSION)
    savepoints.checkpoint_path(lg.KIND_SESSION, SESSION, "status").write_text(
        '{"v": 1, "fold": "sta', encoding="utf-8"
    )

    cold = _cold_bundle()
    resumed = crew_log.fold_session(SESSION)

    assert resumed.projection("status").value == cold.projection("status").value


def test_a_savepoint_that_exhausts_the_parser_stack_is_ignored_rather_than_raised(
    monkeypatch,
):
    """``RecursionError`` is a ``RuntimeError``, so a guard on ``ValueError`` misses it.

    A payload nested past the interpreter's stack limit raises it out of
    ``json.loads``: measured at depth 20,000, which is 40 KB of file, far under
    :data:`savepoints.MAX_CHECKPOINT_BYTES` -- so the size cap does not stand in for
    this guard, and a payload small enough to pass the cap can still reach it.

    The class is injected at the module's own parse point rather than nested for
    real, because the depth that raises is interpreter- and platform-dependent and a
    literal deep enough to be certain everywhere can exhaust the C stack instead of
    raising. The injection is confined to this module, so the store's own record
    parsing -- and therefore the cold answer this compares against -- is untouched.

    A savepoint is the one unusable payload that survives being read, since the file
    stays on disk. So this rejection has to reach the cold fold like every other:
    an escape costs the session every later fold rather than one.
    """
    _long_log()
    crew_log.fold_session(SESSION)
    cold = _cold_bundle()

    class _ExhaustedParser:
        dumps = staticmethod(json.dumps)

        @staticmethod
        def loads(*_args: Any, **_kwargs: Any) -> Any:
            raise RecursionError("maximum recursion depth exceeded while decoding")

    monkeypatch.setattr(savepoints, "json", _ExhaustedParser)

    resumed = crew_log.fold_session(SESSION)

    assert resumed.projection("status").value == cold.projection("status").value
    assert resumed.projection("status").value["turns_completed"] == _LONG_TURNS


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

    returned = savepoints.save(
        handle,
        poisoned_bundle := _with(bundle, {"status": poisoned}),
        prefix=savepoints.prefix_witness(handle, bundle.last_seq),
    )

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
        returned = savepoints.save(
            handle, bundle, prefix=savepoints.prefix_witness(handle, bundle.last_seq)
        )
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
    # Read while the segments are still there, so the save gets PAST the prefix check
    # and the identity check is what refuses it. A witness read afterwards would be
    # ``None`` and would refuse first, leaving the guard this test names unexercised.
    witness = savepoints.prefix_witness(handle, bundle.last_seq)
    unit_dir = store.crew_log_dir(lg.KIND_SESSION, SESSION)
    for child in sorted(unit_dir.iterdir()):
        child.unlink()

    returned = savepoints.save(handle, bundle, prefix=witness)

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
    # Read while the unit is still on disk, for the same reason as the test above:
    # a ``None`` witness would refuse before the path this test drives.
    witness = savepoints.prefix_witness(handle, bundle.last_seq)
    unit_dir = store.crew_log_dir(lg.KIND_SESSION, SESSION)
    for child in sorted(unit_dir.iterdir()):
        child.unlink()
    unit_dir.rmdir()

    returned = savepoints.save(handle, bundle, prefix=witness)

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
    returned = savepoints.save(
        handle, bundle, prefix=savepoints.prefix_witness(handle, bundle.last_seq)
    )

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
    the inode changes: ``log_origin`` reads the creation stamp from disk, so a
    recreation is caught, but a test that recreates the file really is testing the
    store's identity read rather than this control flow. What is pinned here is the
    control flow alone -- a mismatch after the pass folds again instead of serving
    spliced state. The read itself is pinned beside the fold tests.
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
