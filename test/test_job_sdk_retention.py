"""Retention: the cap bounds the store, and a live predicate makes eviction safe.

Two properties, the second the reason the first is not enough on its own:

* a kind with a count cap settles its on-disk terminal-record file count at that
  cap once more than the cap have run and finished;
* a run that is terminal by status but still LIVE per its kind's registered
  predicate is never evicted while live, and becomes evictable once liveness
  clears -- the terminal-but-still-writing case that a status-only eviction
  deletes out from under its own writer.

A kind with no ``retention`` keeps every record, exactly as before retention
existed, so the additive default is pinned here too.

The ``sdk`` fixture and the bounded waits live in ``job_sdk_helpers.py``. Runs
execute on real daemon threads, so terminal state is awaited with a bounded poll
rather than a sleep, which is what keeps the suite from flaking on a loaded host.
"""

from __future__ import annotations

import asyncio
import collections
import json
import threading
import time
import uuid
from pathlib import Path

from job_sdk_helpers import (  # noqa: F401 -- the fixture is requested by name
    _wait_terminal,
    _wait_until,
    sdk_fixture,
)

from conftest import requires_symlinks
from kiro_crew.apps import job_sdk, job_sdk_retention
from kiro_crew.apps.job_sdk import DONE, RUNNING, JobRun, JobSDK, forget_sdk


def _terminal_file_count(sdk: JobSDK, kind: str) -> int:
    """Terminal records of ``kind`` currently on disk."""
    return sum(1 for r in sdk.store.iter_runs() if r.kind == kind and r.is_terminal)


def _file_count(sdk: JobSDK) -> int:
    """Record files in the store, counted by name without opening any.

    The waits below poll this rather than reading records: on Windows a reader
    holding a record open makes the sweep's delete of that record fail, so a
    poll that reads records races the very eviction it waits for. Record
    contents are read only once the count has settled.
    """
    if not sdk.store.dir.is_dir():
        return 0
    return len(list(sdk.store.dir.glob("*.json")))


def _gone(sdk: JobSDK, run_id: str) -> bool:
    """Whether ``run_id``'s record file is absent, checked without opening it."""
    return not (sdk.store.dir / f"{run_id}.json").exists()


def _run_and_finish(sdk: JobSDK, kind: str) -> str:
    """Start a run of a trivial already-registered kind and wait for it done."""
    run_id = sdk.start(kind)
    _wait_terminal(sdk, run_id)
    return run_id


class TestNoRetentionKeepsEverything:
    """The additive default: a kind that never opts in loses no record."""

    def test_without_retention_every_record_survives(self, sdk: JobSDK) -> None:
        sdk.register("plain", lambda h: None)
        for _ in range(6):
            _run_and_finish(sdk, "plain")
        assert _terminal_file_count(sdk, "plain") == 6


class TestCountCapSettlesTheStore:
    """Acceptance (a): run N > cap, drive terminal, the file count settles at the cap."""

    def test_file_count_settles_at_cap_after_eviction(self, sdk: JobSDK) -> None:
        cap = 3
        sdk.register("capped", lambda h: None, retention=cap)
        for _ in range(cap + 4):
            _run_and_finish(sdk, "capped")
        # The sweep runs on the worker thread AFTER the terminal write that
        # ``_wait_terminal`` observes, so wait on the asserted post-sweep state
        # rather than reading it the instant the last run goes terminal.
        _wait_until(lambda: _file_count(sdk) == cap)
        assert _terminal_file_count(sdk, "capped") == cap

    def test_the_newest_runs_are_the_ones_kept(self, sdk: JobSDK) -> None:
        cap = 2
        sdk.register("capped", lambda h: None, retention=cap)
        ids = [_run_and_finish(sdk, "capped") for _ in range(5)]
        # Wait for the final run's sweep to settle the store at the cap before
        # reading which ids survived.
        _wait_until(lambda: _file_count(sdk) == cap)
        survivors = {r.run_id for r in sdk.store.iter_runs() if r.kind == "capped"}
        # The last two started are the two kept; the first three are evicted.
        assert survivors == set(ids[-cap:])
        assert not survivors & set(ids[:-cap])

    def test_eviction_calls_on_evict_before_removing_the_record(self, sdk: JobSDK) -> None:
        evicted: list[str] = []
        sdk.register(
            "capped",
            lambda h: None,
            retention=1,
            on_evict=lambda r: evicted.append(r.run_id),
        )
        first = _run_and_finish(sdk, "capped")
        _run_and_finish(sdk, "capped")
        # The second run's sweep evicts the first; wait for the callback to fire
        # before asserting on it (it runs after that run's terminal write), and
        # then wait for the record's deletion too -- ``remove`` runs AFTER
        # ``on_evict``, so the callback firing does not yet mean the file is gone.
        _wait_until(lambda: evicted == [first])
        assert evicted == [first]
        _wait_until(lambda: _gone(sdk, first))
        assert sdk.get(first) is None

    def test_a_cap_on_one_kind_does_not_touch_another(self, sdk: JobSDK) -> None:
        sdk.register("capped", lambda h: None, retention=1)
        sdk.register("plain", lambda h: None)
        for _ in range(3):
            _run_and_finish(sdk, "capped")
            _run_and_finish(sdk, "plain")
        _wait_until(lambda: _file_count(sdk) == 1 + 3)
        assert _terminal_file_count(sdk, "capped") == 1
        assert _terminal_file_count(sdk, "plain") == 3


class TestLivenessMakesEvictionSafe:
    """Acceptance (b): a terminal-but-live run is kept while live, evicted once clear."""

    def test_a_live_terminal_run_is_not_evicted_while_live(self, sdk: JobSDK) -> None:
        # Liveness is a flag the test owns: the run reaches DONE status, but the
        # predicate reports it still live (its "posting" phase), so retention must
        # keep it past the cap.
        posting: set[str] = set()

        def live(r: JobRun) -> bool:
            return r.run_id in posting

        cap = 1
        sdk.register("deliver", lambda h: None, retention=cap, live=live)

        # The run that will stay "live" (mid-delivery) after it reaches terminal.
        live_id = _run_and_finish(sdk, "deliver")
        posting.add(live_id)

        # Now push well past the cap with ordinary finished runs. The live run is
        # terminal by status but must survive every sweep while posting holds.
        for _ in range(cap + 3):
            _run_and_finish(sdk, "deliver")

        assert sdk.get(live_id) is not None, "a live terminal run was evicted while still live"

    def test_it_becomes_evictable_once_liveness_clears(self, sdk: JobSDK) -> None:
        posting: set[str] = set()

        def live(r: JobRun) -> bool:
            return r.run_id in posting

        cap = 1
        sdk.register("deliver", lambda h: None, retention=cap, live=live)

        live_id = _run_and_finish(sdk, "deliver")
        posting.add(live_id)
        for _ in range(cap + 2):
            _run_and_finish(sdk, "deliver")
        assert sdk.get(live_id) is not None

        # Delivery finishes: the predicate goes False. The next sweep (driven by a
        # fresh terminal run) now counts the once-live run and evicts it, since it
        # is older than the cap's worth of survivors.
        posting.discard(live_id)
        _run_and_finish(sdk, "deliver")
        _wait_until(lambda: _gone(sdk, live_id))
        assert sdk.get(live_id) is None

    def test_a_live_run_does_not_consume_a_cap_slot(self, sdk: JobSDK) -> None:
        # A live terminal run is kept AND uncounted, so a burst of live runs cannot
        # push a just-finished ordinary run off the cap. Each run is marked live
        # the instant it finishes -- before the NEXT run's sweep can weigh it --
        # because a sweep that ran while posting was still empty would evict it as
        # an ordinary old run, which is the test's own ordering, not the behaviour.
        posting: set[str] = set()
        sdk.register(
            "deliver",
            lambda h: None,
            retention=2,
            live=lambda r: r.run_id in posting,
        )
        live_ids: list[str] = []
        for _ in range(3):
            rid = _run_and_finish(sdk, "deliver")
            posting.add(rid)
            live_ids.append(rid)
        # Two ordinary runs: they are the cap's worth, and the three live ones
        # beside them must not evict either. Wait for the last run's sweep to
        # settle (all five survive: 3 live + 2 ordinary == cap) before reading.
        ordinary = [_run_and_finish(sdk, "deliver") for _ in range(2)]
        expected = set(live_ids) | set(ordinary)
        _wait_until(lambda: _file_count(sdk) == len(expected))
        survivors = {r.run_id for r in sdk.store.iter_runs() if r.kind == "deliver"}
        for rid in ordinary:
            assert rid in survivors
        for rid in live_ids:
            assert rid in survivors, "a live run was evicted by runs beside it"


class TestRetentionNeverEvictsALiveWorker:
    """A run still EXECUTING is never evicted, even once its terminal record lands."""

    def test_an_executing_run_is_spared_by_the_sweep(self, sdk: JobSDK) -> None:
        # One kind whose worker blocks until the test releases it, so it stays in
        # the live table while the test checks it; a cap of 1 with its sole run
        # still live must evict nothing.
        release = threading.Event()
        started = threading.Event()

        def blocker(h) -> None:  # noqa: ANN001 - handle type is the SDK's
            started.set()
            release.wait(timeout=5.0)

        sdk.register("blocker", blocker, retention=1)
        blocked_id = sdk.start("blocker")
        assert started.wait(timeout=5.0)

        # The blocking run is live; nothing to evict for its kind.
        assert sdk.get(blocked_id) is not None
        release.set()
        _wait_terminal(sdk, blocked_id)
        # Once finished and alone under the cap of 1, it is kept.
        assert sdk.get(blocked_id) is not None


class TestCompletionOrderNotStartOrder:
    """A long run that started first but finished last is not evicted by its own sweep.

    ``seq`` is minted at the terminal write, so retention ranks by completion.
    Ranking by START order instead let a long run A carry the lowest seq while K
    runs started after it finished first and filled the cap -- so when A finally
    finished, its own sweep sorted A oldest and deleted the result it had just
    produced.
    """

    def test_a_long_first_run_of_a_kind_outlives_later_quick_ones(self, sdk: JobSDK) -> None:
        # One kind. Run A blocks (started first, finishes last). While it blocks,
        # cap+1 quick runs of the SAME kind start and finish. A's seq is minted
        # only when A finishes, so A is the newest completion and must survive;
        # the oldest-completed quick run is the one evicted to hold the cap.
        release = threading.Event()
        a_started = threading.Event()

        def gated(h) -> None:  # noqa: ANN001 - handle type is the SDK's
            if h.params.get("role") == "long":
                a_started.set()
                release.wait(timeout=5.0)

        cap = 2
        sdk.register("mixed", gated, retention=cap)
        a_id = sdk.start("mixed", params={"role": "long"})
        assert a_started.wait(timeout=5.0)

        # Quick runs of the same kind complete while A is still blocked.
        quick = [_run_and_finish(sdk, "mixed") for _ in range(cap + 1)]

        # A is still live, so it has not been evicted by any quick run's sweep.
        assert sdk.get(a_id) is not None

        # A finishes last. Its own sweep must keep A (newest completion) and evict
        # the oldest-completed quick run to hold the cap.
        release.set()
        _wait_terminal(sdk, a_id)
        _wait_until(lambda: _file_count(sdk) == cap)
        survivors = {r.run_id for r in sdk.store.iter_runs() if r.kind == "mixed"}
        assert a_id in survivors, "a long run that finished last was evicted by its own sweep"
        assert quick[0] not in survivors, "the oldest-completed quick run should be evicted"


class TestOnEvictFiresExactlyOncePerRun:
    """Concurrent sweeps must not call ``on_evict`` twice for one evicted run."""

    def test_concurrent_completions_evict_each_run_once(self, sdk: JobSDK) -> None:
        # Many runs of one capped kind finish at about the same time, so their
        # sweeps race. Without serialization + a once-guard, two sweeps read the
        # store before either deletes, compute the same evictable tail, and call
        # on_evict twice on one run. Count calls per run id and assert none > 1.
        counts: "collections.Counter[str]" = collections.Counter()
        count_lock = threading.Lock()
        gate = threading.Event()

        def on_evict(r: JobRun) -> None:
            with count_lock:
                counts[r.run_id] += 1

        def gated(h) -> None:  # noqa: ANN001 - handle type is the SDK's
            # Every run waits on one gate, then returns together, so their
            # terminal writes and sweeps overlap as much as the host allows.
            gate.wait(timeout=5.0)

        cap = 2
        n = 8
        sdk.register("burst", gated, retention=cap, on_evict=on_evict)
        ids = [sdk.start("burst") for _ in range(n)]
        # Release them all at once.
        gate.set()
        # n-cap runs are evicted, so their records are GONE -- do not wait on each
        # run's terminal record (an evicted one never shows terminal). Wait for
        # the store to settle at the cap and for every eviction callback to fire.
        _wait_until(lambda: _file_count(sdk) == cap, timeout=10.0)
        _wait_until(lambda: sum(counts.values()) == n - cap, timeout=10.0)
        with count_lock:
            doubled = {rid: c for rid, c in counts.items() if c > 1}
        assert not doubled, f"on_evict fired more than once for: {doubled}"
        # Exactly the evicted runs got a callback, each once.
        assert sum(counts.values()) == n - cap
        assert all(c == 1 for c in counts.values())
        assert len(ids) == n


class TestOrderingSurvivesARestart:
    """A run started after a gateway restart outranks records the old process left.

    The counter is process-local, so a naive implementation resets it to 0 on
    restart and a kind with records on disk (carrying the old process's high
    seqs) would see each new run minted a low seq, sort it oldest, and evict the
    just-finished run -- deleting the latest result retention exists to keep. A
    FRESH ``JobSDK`` over a store that already holds high-seq records stands in
    for that restart: its construction seeds the counter above them.
    """

    def test_a_run_started_after_a_restart_is_not_evicted_as_oldest(self, tmp_path: Path) -> None:
        # Stand in for the pre-restart process: two terminal records of a capped
        # kind, carrying high seqs, written straight to the store.
        pre = JobSDK("restart-app", tmp_path)
        try:
            for seq in (98, 99):
                pre.store.write(
                    JobRun(
                        run_id=uuid.uuid4().hex,
                        app="restart-app",
                        kind="capped",
                        status=DONE,
                        seq=seq,
                        created_at="2026-01-01T00:00:00Z",
                        updated_at="2026-01-01T00:00:00Z",
                    )
                )
        finally:
            forget_sdk(pre.app_name)

        # The "restart": a fresh SDK over the same store. Its construction seeds
        # the shared counter above 99, so the next run it starts outranks both.
        sdk = JobSDK("restart-app", tmp_path)
        try:
            sdk.register("capped", lambda h: None, retention=2)
            new_id = sdk.start("capped")
            _wait_terminal(sdk, new_id)
            _wait_until(lambda: _file_count(sdk) == 2)
            survivors = {r.run_id for r in sdk.store.iter_runs() if r.kind == "capped"}
            # The new run survives; one old record is evicted to hold the cap of 2.
            assert new_id in survivors, "a run started after a restart was evicted as oldest"
            assert len(survivors) == 2
        finally:
            with sdk._lock:  # noqa: SLF001 - teardown needs the live table
                live = list(sdk._live.values())
            for entry in live:
                entry.handle.discarded.set()
                entry.handle.cancelled.set()
            for entry in live:
                entry.thread.join(timeout=5.0)
            forget_sdk(sdk.app_name)

    def test_uncapped_completions_across_a_restart_stay_ordered_for_later_retention(
        self, tmp_path: Path
    ) -> None:
        # GPT F1: an uncapped kind still mints completion seqs. If its seqs are
        # not seeded across restarts, a second process reuses low seqs, and when
        # the kind LATER opts into retention the sweep sorts by those seqs and
        # deletes the newest records. Run real completions (no cap) in one SDK,
        # then a fresh SDK (the restart) runs more, then enable retention: the
        # records from the second process must be the ones kept.
        def run_finish(s: JobSDK, kind: str) -> str:
            rid = s.start(kind)
            _wait_terminal(s, rid)
            return rid

        # Each fresh ``JobSDK`` starts its own counter at 0, as a restarted
        # process does, so the order across SDKs is the seed's (and the
        # wall-clock floor's) to keep.
        first = JobSDK("across", tmp_path)
        try:
            first.register("k", lambda h: None)  # no retention
            old_ids = [run_finish(first, "k") for _ in range(3)]
        finally:
            forget_sdk(first.app_name)

        # The restart: a fresh SDK, with its own counter, over the same
        # store, still uncapped.
        second = JobSDK("across", tmp_path)
        try:
            second.register("k", lambda h: None)
            new_ids = [run_finish(second, "k") for _ in range(2)]
            # Now the kind opts into retention=2. Another restart, then drive
            # one more completion to trigger a sweep; the two most-recently-
            # completed records overall (the second process's) must survive.
            third = JobSDK("across", tmp_path)
            try:
                third.register("k", lambda h: None, retention=2)
                newest = run_finish(third, "k")
                # Count files by name, not by reading them: on Windows a
                # reader holding a record open makes the sweep's delete of
                # that record fail, so polling through ``iter_runs`` races
                # the very eviction it waits for.
                _wait_until(lambda: len(list(third.store.dir.glob("*.json"))) == 2)
                survivors = {r.run_id for r in third.store.iter_runs() if r.kind == "k"}
                assert newest in survivors
                # None of the first process's three oldest survive once the
                # cap of 2 is applied; the kept records are the newer ones.
                assert not (set(old_ids) & survivors), (
                    "an uncapped run from before the restart outranked a newer "
                    "one -- seeding did not hold across the restart"
                )
                assert survivors.issubset(set(new_ids) | {newest})
            finally:
                forget_sdk(third.app_name)
        finally:
            forget_sdk(second.app_name)


class TestTheWallClockFloorOrdersAFailedSeed:
    """A post-restart run outranks earlier records even when the seed saw nothing."""

    def test_a_partial_seed_does_not_evict_the_newest_run(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # A record an earlier process wrote a second ago, then a "restart" (the
        # counter back to 0) whose seed scan comes back empty -- a transient
        # listing failure. Without the wall-clock floor the new run is minted a
        # tiny seq, sorts oldest, and its own sweep evicts it.
        sdk = JobSDK("floor-app", tmp_path)
        try:
            old_id = uuid.uuid4().hex
            sdk.store.write(
                JobRun(
                    run_id=old_id,
                    app="floor-app",
                    kind="k",
                    status=DONE,
                    seq=time.time_ns() - 1_000_000_000,
                )
            )
            monkeypatch.setattr(sdk.store, "max_seq", lambda: 0)
            sdk.register("k", lambda h: None, retention=1)
            new_id = _run_and_finish(sdk, "k")
            _wait_until(lambda: _gone(sdk, old_id))
            assert sdk.get(new_id) is not None, "the newest run was evicted as oldest"
        finally:
            forget_sdk(sdk.app_name)


class TestAPlantedHugeSeqCannotPoisonTheCounter:
    """A forged record's out-of-range ``seq`` is ignored by the seed."""

    def test_a_4300_digit_seq_on_disk_does_not_break_terminal_writes(self, sdk: JobSDK) -> None:
        # 4300 digits is the most ``json.loads`` accepts; one more digit and
        # ``json.dumps`` refuses to write. Seeding the shared counter from it
        # would make every later terminal write, in every app, fail.
        sdk.store.dir.mkdir(parents=True, exist_ok=True)
        planted_id = "c" * 32
        body = json.dumps(
            JobRun(run_id=planted_id, app=sdk.app_name, kind="k", status=DONE).to_dict()
        ).replace('"seq": 0', '"seq": ' + "9" * 4300)
        (sdk.store.dir / f"{planted_id}.json").write_text(body, encoding="utf-8")
        assert sdk.store.max_seq() == 0
        sdk.register("k", lambda h: None)
        run_id = _run_and_finish(sdk, "k")
        assert sdk.get(run_id).status == DONE
        assert 0 < sdk.get(run_id).seq < job_sdk_retention._MAX_SEQ  # noqa: SLF001

    def test_raise_to_refuses_an_out_of_range_floor(self) -> None:
        counter = job_sdk_retention._SeqCounter()  # noqa: SLF001
        counter.raise_to(job_sdk_retention._MAX_SEQ)  # noqa: SLF001
        counter.raise_to(job_sdk_retention._SEED_CEILING)  # noqa: SLF001 - no headroom left
        counter.raise_to(-5)
        assert counter._value == 0  # noqa: SLF001
        counter.raise_to(7)
        assert counter._value == 7  # noqa: SLF001

    def test_an_out_of_range_seq_sorts_as_zero(self) -> None:
        big = JobRun(run_id="a" * 32, app="x", kind="k", status=DONE, seq=1 << 70)
        small = JobRun(run_id="b" * 32, app="x", kind="k", status=DONE, seq=5)
        assert job_sdk_retention._order_key(big) < job_sdk_retention._order_key(
            small
        )  # noqa: SLF001

    def test_one_apps_near_cap_seq_does_not_reorder_another_app(self, tmp_path: Path) -> None:
        # App A's store holds a record just under the seed ceiling, which A's
        # seed accepts. The counter is per SDK, so A's value never reaches B's
        # numbers and B's capped kind keeps each run it finishes.
        a = JobSDK("app-a", tmp_path / "a")
        b = JobSDK("app-b", tmp_path / "b")
        try:
            a.store.write(
                JobRun(
                    run_id=uuid.uuid4().hex,
                    app="app-a",
                    kind="k",
                    status=DONE,
                    seq=job_sdk_retention._SEED_CEILING - 2,  # noqa: SLF001 - accepted by a seed
                )
            )
            a.register("k", lambda h: None)
            _run_and_finish(a, "k")
            b.register("k", lambda h: None, retention=1)
            first = _run_and_finish(b, "k")
            second = _run_and_finish(b, "k")
            _wait_until(lambda: _gone(b, first))
            assert b.get(second) is not None, "app B's newest run was evicted"
            # A shared counter would have carried A's seed into B's numbers.
            assert b.get(second).seq < job_sdk_retention._SEED_CEILING - 2  # noqa: SLF001
        finally:
            forget_sdk(a.app_name)
            forget_sdk(b.app_name)


class TestTombstoneSetStaysBounded:
    """The eviction tombstone set holds only outstanding evictions, not history."""

    def test_evicted_ids_does_not_grow_with_completions(self, sdk: JobSDK) -> None:
        sdk.register("capped", lambda h: None, retention=1)
        for _ in range(8):
            _run_and_finish(sdk, "capped")
        _wait_until(lambda: _file_count(sdk) == 1)
        # Every eviction's ``remove`` succeeded, so its tombstone was dropped; the
        # set does not accumulate one id per historical run.
        _wait_until(lambda: len(sdk._evicted_ids) == 0)  # noqa: SLF001 - asserting the bound
        assert sdk._evicted_ids == set()  # noqa: SLF001


class TestForgedRecordIsSkipped:
    """A record file whose body id does not match its filename is not acted on."""

    def test_a_mismatched_filename_record_is_not_evicted_or_followed(self, sdk: JobSDK) -> None:
        # The store only ever writes ``{run_id}.json``. Plant a file whose BODY
        # names a different run_id than its filename -- as a hand-copied record
        # would -- and confirm retention neither counts it nor deletes the file
        # its body names. The forged body points at the victim's id and carries
        # a LOWER seq, so a sweep that admitted it would evict by the body id and
        # delete the victim's file.
        evicted: list[str] = []
        sdk.register("k", lambda h: None, retention=1, on_evict=lambda r: evicted.append(r.run_id))
        victim_id = _run_and_finish(sdk, "k")
        forged_stem = "b" * 32
        forged = JobRun(run_id=victim_id, app=sdk.app_name, kind="k", status=DONE, seq=0)
        # Write the forged body under a DIFFERENT filename, bypassing store.write
        # (which would name it by run_id).
        (sdk.store.dir / f"{forged_stem}.json").write_text(
            json.dumps(forged.to_dict()), encoding="utf-8"
        )

        seen = {r.run_id for r in sdk.store._iter_runs_nolink()}  # noqa: SLF001
        assert seen == {victim_id}, "the forged record should be skipped by the sweep's scan"

        # A second run finishing sweeps the kind: only the genuine older run is
        # evicted, and the forged file is neither counted nor followed.
        newest = _run_and_finish(sdk, "k")
        _wait_until(lambda: evicted == [victim_id])
        assert evicted == [victim_id]
        assert sdk.get(newest) is not None
        assert (sdk.store.dir / f"{forged_stem}.json").exists()


class TestSweepReadsThroughTheBoundedFunnel:
    """The sweep's scan refuses planted links and oversized files instead of reading them."""

    @requires_symlinks
    def test_a_planted_symlink_record_is_neither_counted_nor_evicted(
        self, sdk: JobSDK, tmp_path: Path
    ) -> None:
        # A link-following read would admit a symlinked record whose target is a
        # well-formed terminal run of this kind, count it, and hand it to
        # ``on_evict`` as the oldest. The no-follow funnel refuses the link, so
        # the sweep never sees it.
        evicted: list[str] = []
        sdk.register("k", lambda h: None, retention=1, on_evict=lambda r: evicted.append(r.run_id))
        first = _run_and_finish(sdk, "k")
        link_id = "d" * 32
        outside = tmp_path / "outside-record.json"
        planted = JobRun(run_id=link_id, app=sdk.app_name, kind="k", status=DONE, seq=0)
        outside.write_text(json.dumps(planted.to_dict()), encoding="utf-8")
        (sdk.store.dir / f"{link_id}.json").symlink_to(outside)

        assert link_id not in {r.run_id for r in sdk.store._iter_runs_nolink()}  # noqa: SLF001

        second = _run_and_finish(sdk, "k")
        _wait_until(lambda: evicted == [first])
        assert evicted == [first], "the sweep read through the planted link"
        assert sdk.get(second) is not None
        assert (sdk.store.dir / f"{link_id}.json").is_symlink()
        assert outside.exists()

    def test_an_oversized_record_is_skipped(self, sdk: JobSDK, monkeypatch) -> None:
        sdk.register("k", lambda h: None)
        kept = _run_and_finish(sdk, "k")
        big_id = "e" * 32
        big = JobRun(
            run_id=big_id, app=sdk.app_name, kind="k", status=DONE, seq=7, error="x" * 4096
        )
        (sdk.store.dir / f"{big_id}.json").write_text(json.dumps(big.to_dict()), encoding="utf-8")
        # Set the bound to the genuine record's own size, so it is read and the
        # larger planted record is over the bound and skipped.
        small = (sdk.store.dir / f"{kept}.json").stat().st_size
        large = (sdk.store.dir / f"{big_id}.json").stat().st_size
        assert large > small
        monkeypatch.setattr(job_sdk, "_MAX_RECORD_BYTES", small)
        assert {r.run_id for r in sdk.store._iter_runs_nolink()} == {kept}  # noqa: SLF001
        assert sdk.store.max_seq() == sdk.get(kept).seq

    def test_scan_is_empty_when_the_directory_is_absent(self, tmp_path: Path) -> None:
        store = job_sdk.JobStore(tmp_path / "never-created")
        assert list(store._iter_runs_nolink()) == []  # noqa: SLF001
        assert store.max_seq() == 0

    def test_scan_survives_a_directory_listing_failure(self, sdk: JobSDK, monkeypatch) -> None:
        sdk.register("k", lambda h: None)
        _run_and_finish(sdk, "k")

        def _boom(self, pattern):
            raise OSError("listing failed")

        monkeypatch.setattr(Path, "glob", _boom)
        assert list(sdk.store._iter_runs_nolink()) == []  # noqa: SLF001

    def test_an_unparseable_record_is_skipped(self, sdk: JobSDK) -> None:
        sdk.register("k", lambda h: None)
        kept = _run_and_finish(sdk, "k")
        (sdk.store.dir / f"{'f' * 32}.json").write_text("{not json", encoding="utf-8")
        assert {r.run_id for r in sdk.store._iter_runs_nolink()} == {kept}  # noqa: SLF001


class TestTheTerminalPathSurvivesRetentionFailures:
    """Seeding, stamping and sweeping never raise out of the terminal ``finally``."""

    def test_a_deeply_nested_record_is_skipped_and_the_run_still_finishes(
        self, sdk: JobSDK
    ) -> None:
        # ``json.loads`` raises RecursionError (not ValueError) on deep nesting.
        sdk.register("k", lambda h: None, retention=1)
        sdk.store.dir.mkdir(parents=True, exist_ok=True)
        (sdk.store.dir / f"{'a' * 32}.json").write_text("[" * 200_000, encoding="utf-8")
        assert list(sdk.store._iter_runs_nolink()) == []  # noqa: SLF001
        run_id = _run_and_finish(sdk, "k")
        assert sdk.get(run_id).status == DONE
        assert sdk.get(run_id).seq > 0

    def test_a_record_not_named_as_a_run_id_is_skipped(self, sdk: JobSDK) -> None:
        # Its body id matches its (non-hex) stem, so only the name check stops it;
        # ``remove`` could never delete it, and ``on_evict`` must not see it.
        evicted: list[str] = []
        sdk.register("k", lambda h: None, retention=1, on_evict=lambda r: evicted.append(r.run_id))
        first = _run_and_finish(sdk, "k")
        stem = "not-a-run-id"
        odd = JobRun(run_id=stem, app=sdk.app_name, kind="k", status=DONE, seq=0)
        (sdk.store.dir / f"{stem}.json").write_text(json.dumps(odd.to_dict()), encoding="utf-8")
        assert stem not in {r.run_id for r in sdk.store._iter_runs_nolink()}  # noqa: SLF001
        _run_and_finish(sdk, "k")
        _wait_until(lambda: evicted == [first])
        assert evicted == [first]

    def test_a_failing_seed_still_mints_a_seq_and_writes_the_terminal_record(
        self, sdk: JobSDK, monkeypatch
    ) -> None:
        def _boom() -> int:
            raise RuntimeError("seed scan failed")

        monkeypatch.setattr(sdk.store, "max_seq", _boom)
        sdk.register("k", lambda h: None)
        run_id = _run_and_finish(sdk, "k")
        assert sdk.get(run_id).status == DONE
        assert sdk.get(run_id).seq > 0
        assert sdk._seq_seeded is True  # noqa: SLF001 - a failed seed is not retried per run

    def test_a_failing_stamp_does_not_skip_the_terminal_write(
        self, sdk: JobSDK, monkeypatch
    ) -> None:
        def _boom() -> int:
            raise RuntimeError("counter failed")

        monkeypatch.setattr(sdk._seq_counter, "next", _boom)  # noqa: SLF001
        sdk.register("k", lambda h: None)
        run_id = _run_and_finish(sdk, "k")
        assert sdk.get(run_id).status == DONE
        # The live entry is popped after the terminal write ``_run_and_finish``
        # observes, so await it rather than reading it at once.
        _wait_until(lambda: sdk.cancelling_and_live_ids()[1] == frozenset())

    def test_a_failing_sweep_does_not_escape_the_terminal_finally(
        self, sdk: JobSDK, monkeypatch
    ) -> None:
        def _boom(self, runner, kind) -> None:
            raise RuntimeError("sweep failed")

        monkeypatch.setattr(job_sdk_retention, "_sweep", _boom)
        sdk.register("k", lambda h: None, retention=1)
        ids = [_run_and_finish(sdk, "k") for _ in range(3)]
        assert all(sdk.get(i).status == DONE for i in ids)
        # The live entry is popped after the terminal write ``_run_and_finish``
        # observes, so await it rather than reading it at once.
        _wait_until(lambda: sdk.cancelling_and_live_ids()[1] == frozenset())


class TestADisabledSdkSweepsNothing:
    """A worker that outlives disable cannot run its stale sweep over a replacement SDK's records."""

    def test_a_stubborn_worker_does_not_evict_the_re_enabled_sdks_records(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # Disable joins workers for a bounded time and leaves a stubborn one
        # running. Re-enable builds a fresh SDK over the same directory, here
        # with retention OFF. When the old worker finally returns, its sweep
        # carries the old cap (1) and the old ``on_evict``, and the disable
        # cleared its live table -- so without the closed fence it would evict
        # one of the replacement's two records.
        monkeypatch.setattr(job_sdk, "_CLEANUP_JOIN_SECS", 0.2)
        started = threading.Event()
        stop = threading.Event()
        worker: list[threading.Thread] = []
        stale_evictions: list[str] = []

        def stubborn(h, **kw):
            worker.append(threading.current_thread())
            started.set()
            stop.wait(10.0)  # ignores cancel, so disable leaves it running

        old = JobSDK("reenable-app", tmp_path)
        try:
            old.register(
                "k",
                stubborn,
                cancellable=True,
                retention=1,
                on_evict=lambda r: stale_evictions.append(r.run_id),
            )
            old.start("k")
            assert started.wait(5.0)
            cleanup = asyncio.run(old.remove_all_async())
            assert cleanup.still_running == 1
        finally:
            forget_sdk(old.app_name)

        new = JobSDK("reenable-app", tmp_path)
        try:
            new.register("k", lambda h: None)
            kept = {_run_and_finish(new, "k") for _ in range(2)}

            stop.set()
            for t in worker:
                t.join(timeout=5.0)
                assert not t.is_alive(), "the stubborn worker outlived its test"

            assert stale_evictions == [], "a disabled SDK fired its stale on_evict"
            assert {r.run_id for r in new.store.iter_runs()} == kept
        finally:
            stop.set()
            forget_sdk(new.app_name)


class TestReconcileDoesNotResurrectAnEvictedRun:
    """A guarded reconcile write refuses an ABSENT record, not just a terminal one."""

    def test_an_evicted_record_is_not_recreated_as_interrupted(self, sdk: JobSDK) -> None:
        # reconcile works off a stale snapshot: it can hold a RUNNING view of a
        # run that has since finished AND been evicted by retention, whose
        # on_evict already released its resources. The guarded write
        # (_persist only_if_not_terminal) re-reads under the lock; an ABSENT
        # record there means evicted, and recreating it would resurrect a
        # released run as falsely interrupted. Simulate: a run id with NO file on
        # disk (evicted), fed to _persist as a running record the way reconcile
        # would.
        run_id = "c" * 32
        stale = JobRun(
            run_id=run_id,
            app=sdk.app_name,
            kind="k",
            status=RUNNING,
            origin=job_sdk._ORIGIN,  # noqa: SLF001 - same-process origin, as reconcile sees
        )
        # No file exists for run_id (it was evicted). The guarded write must
        # REFUSE rather than create one.
        wrote = sdk._persist(stale, only_if_not_terminal=True)  # noqa: SLF001
        assert wrote is False, "reconcile recreated a record retention had evicted"
        assert sdk.get(run_id) is None, "an evicted run was resurrected on disk"


def test_done_is_the_status_these_runs_reach() -> None:
    # A guard on the helper's assumption: the trivial runner returns None, so its
    # terminal status is DONE, which is what the cap counts.
    assert DONE == "done"
