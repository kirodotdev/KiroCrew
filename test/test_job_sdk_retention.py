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

import collections
import json
import threading
import uuid
from pathlib import Path

from job_sdk_helpers import (  # noqa: F401 -- the fixture is requested by name
    _wait_terminal,
    _wait_until,
    sdk_fixture,
)

from kiro_crew.apps import job_sdk
from kiro_crew.apps.job_sdk import DONE, RUNNING, JobRun, JobSDK, forget_sdk


def _terminal_file_count(sdk: JobSDK, kind: str) -> int:
    """Terminal records of ``kind`` currently on disk."""
    return sum(1 for r in sdk.store.iter_runs() if r.kind == kind and r.is_terminal)


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
        _wait_until(lambda: _terminal_file_count(sdk, "capped") == cap)
        assert _terminal_file_count(sdk, "capped") == cap

    def test_the_newest_runs_are_the_ones_kept(self, sdk: JobSDK) -> None:
        cap = 2
        sdk.register("capped", lambda h: None, retention=cap)
        ids = [_run_and_finish(sdk, "capped") for _ in range(5)]
        # Wait for the final run's sweep to settle the store at the cap before
        # reading which ids survived.
        _wait_until(
            lambda: len({r.run_id for r in sdk.store.iter_runs() if r.kind == "capped"}) == cap
        )
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
        _wait_until(lambda: sdk.get(first) is None)
        assert sdk.get(first) is None

    def test_a_cap_on_one_kind_does_not_touch_another(self, sdk: JobSDK) -> None:
        sdk.register("capped", lambda h: None, retention=1)
        sdk.register("plain", lambda h: None)
        for _ in range(3):
            _run_and_finish(sdk, "capped")
            _run_and_finish(sdk, "plain")
        _wait_until(lambda: _terminal_file_count(sdk, "capped") == 1)
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
        _wait_until(lambda: sdk.get(live_id) is None)
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
        _wait_until(
            lambda: {r.run_id for r in sdk.store.iter_runs() if r.kind == "deliver"} == expected
        )
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
        _wait_until(
            lambda: len({r.run_id for r in sdk.store.iter_runs() if r.kind == "mixed"}) == cap
        )
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
        _wait_until(lambda: _terminal_file_count(sdk, "burst") == cap, timeout=10.0)
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
            _wait_until(
                lambda: len({r.run_id for r in sdk.store.iter_runs() if r.kind == "capped"}) == 2
            )
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

        # Each "restart" resets the per-process seq counter to 0, which is the
        # condition that makes seeding load-bearing; without the reset the shared
        # in-process counter would paper over the bug. The counter is a module
        # global shared with every other test, so it is saved and restored.
        saved_seq = job_sdk._seq  # noqa: SLF001 - module global under test

        def restart_counter() -> None:
            job_sdk._seq = 0  # noqa: SLF001 - simulating a fresh process

        try:
            restart_counter()
            first = JobSDK("across", tmp_path)
            try:
                first.register("k", lambda h: None)  # no retention
                old_ids = [run_finish(first, "k") for _ in range(3)]
            finally:
                forget_sdk(first.app_name)

            # The restart: reset the counter and build a fresh SDK over the same
            # store, still uncapped.
            restart_counter()
            second = JobSDK("across", tmp_path)
            try:
                second.register("k", lambda h: None)
                new_ids = [run_finish(second, "k") for _ in range(2)]
                # Now the kind opts into retention=2. Another restart, then drive
                # one more completion to trigger a sweep; the two most-recently-
                # completed records overall (the second process's) must survive.
                restart_counter()
                third = JobSDK("across", tmp_path)
                try:
                    third.register("k", lambda h: None, retention=2)
                    newest = run_finish(third, "k")
                    _wait_until(
                        lambda: len({r.run_id for r in third.store.iter_runs() if r.kind == "k"})
                        == 2
                    )
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
        finally:
            job_sdk._seq = saved_seq  # noqa: SLF001 - restore the shared counter


class TestTombstoneSetStaysBounded:
    """The eviction tombstone set holds only outstanding evictions, not history."""

    def test_evicted_ids_does_not_grow_with_completions(self, sdk: JobSDK) -> None:
        sdk.register("capped", lambda h: None, retention=1)
        for _ in range(8):
            _run_and_finish(sdk, "capped")
        _wait_until(lambda: _terminal_file_count(sdk, "capped") == 1)
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
        # its body names. Write a legitimate victim record first; the forged
        # file's body points at the victim's id.
        victim = JobRun(
            run_id="a" * 32,
            app=sdk.app_name,
            kind="k",
            status=DONE,
            seq=5,
        )
        sdk.store.write(victim)
        forged_stem = "b" * 32
        forged = JobRun(
            run_id="a" * 32,  # body names the victim, not the filename
            app=sdk.app_name,
            kind="k",
            status=DONE,
            seq=6,
        )
        # Write the forged body under a DIFFERENT filename, bypassing store.write
        # (which would name it by run_id).
        (sdk.store.dir / f"{forged_stem}.json").write_text(
            json.dumps(forged.to_dict()), encoding="utf-8"
        )

        # iter_runs must skip the forged file (body id != filename stem) and yield
        # only the victim.
        seen = {(r.run_id) for r in sdk.store.iter_runs() if r.kind == "k"}
        assert seen == {"a" * 32}, "the forged record should be skipped by iter_runs"
        # The victim's file is intact; the forged file is left alone (not followed
        # into deleting the victim).
        assert (sdk.store.dir / f"{'a' * 32}.json").exists()
        assert (sdk.store.dir / f"{forged_stem}.json").exists()


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
