"""The skill catalog is discovered off the request path, and persisted.

Every test here answers one question: **can a turn end up waiting for skill
discovery?** The proof is never a duration — a fast disk makes any wall-clock
assertion pass — but a walk that is HELD OPEN while the caller is served. A stub
enumeration blocks on an event and counts its runs, so "the caller did not wait"
is observable as "it returned rows while the walk is still blocked", and "sessions
share one walk" is observable as a run count of exactly one.

The cold-start tests shrink ``_COLD_CATALOG_WAIT_SECS`` instead of sleeping past
it: the branch under test is "the budget expired", which a 50 ms budget reaches
exactly as the shipped two seconds does.

Everything lives under ``tmp_path``, including the SQLite index (resolved from the
skills root's parent). The fixture releases every gate and closes every loader
before it returns, then JOINS each refresh worker, so no background walk can touch
a directory pytest has already removed.
"""

from __future__ import annotations

import os
import threading
import time
import types
from pathlib import Path

import pytest

from conftest import make_dir_link
from kiro_crew import skill_search_index as skill_search_index_module
from kiro_crew import skill_trust
from kiro_crew import skills as skills_module
from kiro_crew.skill_runtime import catalog as catalog_module
from kiro_crew.skill_search_index import SKILL_SEARCH_INDEX_FILENAME, SkillSearchIndex
from kiro_crew.skills import SkillsLoader, _trusted_skill_roots

_WAIT = 30.0
"""Bound on every wait a test itself must unblock, so a missed release fails by
name at that line instead of hanging the xdist worker."""


def _skill(root: Path, key: str, *, description: str = "a skill", body: str = "step one") -> Path:
    path = root / key / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {key.split('/')[-1]}\ndescription: {description}\n---\n# H\n{body}\n",
        encoding="utf-8",
    )
    return path


class _Walk:
    """A stand-in enumeration that can be held open and counts its runs."""

    def __init__(self, rows: list[tuple[str, Path, str | None]], *, gate: threading.Event | None):
        self._rows = rows
        self.gate = gate
        self._lock = threading.Lock()
        self.calls = 0
        self._started = threading.Semaphore(0)

    def __call__(self, project_key: str | None = None) -> list[tuple[str, Path, str | None]]:
        with self._lock:
            self.calls += 1
        self._started.release()
        if self.gate is not None:
            assert self.gate.wait(timeout=_WAIT), "walk gate was never released"
        return list(self._rows)

    def await_start(self) -> None:
        assert self._started.acquire(timeout=_WAIT), "the background walk never started"


class _DropAfterScopeRead:
    """A connection stand-in that commits another connection's drop mid-read."""

    def __init__(self, conn, other: SkillSearchIndex) -> None:
        self._conn = conn
        self._other = other
        self.fired = False

    def execute(self, sql: str, *args):
        cursor = self._conn.execute(sql, *args)
        if "FROM skill_catalog_scope" in sql and not self.fired:
            self.fired = True
            assert self._other.drop_catalog(), "the other connection could not drop"
        return cursor

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


class _CountingLock:
    """A lock that counts the threads waiting on it, so a test can await one queuing."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._guard = threading.Lock()
        self.waiting = 0

    def __enter__(self) -> _CountingLock:
        with self._guard:
            self.waiting += 1
        assert self._lock.acquire(timeout=_WAIT), "the lock was never released"
        with self._guard:
            self.waiting -= 1
        return self

    def __exit__(self, *_exc) -> None:
        self._lock.release()


class _BusyTimeoutAtBegin:
    """A connection stand-in recording the busy timeout each ``BEGIN IMMEDIATE`` runs under."""

    def __init__(self, conn) -> None:
        self._conn = conn
        self.seen: list[int] = []

    def execute(self, sql: str, *args):
        if sql == "BEGIN IMMEDIATE":
            self.seen.append(self._conn.execute("PRAGMA busy_timeout").fetchone()[0])
        return self._conn.execute(sql, *args)

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


class _Harness:
    """Loaders that share one home, plus the gates holding their walks open."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.root = home / "skills"
        self.root.mkdir(parents=True, exist_ok=True)
        self._loaders: list[SkillsLoader] = []
        self._gates: list[threading.Event] = []

    def loader(self) -> SkillsLoader:
        loader = SkillsLoader(skills_path=self.root, install_builtins=False)
        self._loaders.append(loader)
        return loader

    def gate(self) -> threading.Event:
        event = threading.Event()
        self._gates.append(event)
        return event

    def stub_walk(
        self,
        loader: SkillsLoader,
        monkeypatch: pytest.MonkeyPatch,
        rows: list[tuple[str, Path, str | None]],
        *,
        blocked: bool = True,
    ) -> _Walk:
        walk = _Walk(rows, gate=self.gate() if blocked else None)
        monkeypatch.setattr(loader, "_iter_uncached", walk)
        return walk

    def stale_snapshot(
        self, monkeypatch: pytest.MonkeyPatch, *, blocked: bool = False
    ) -> tuple[SkillsLoader, Path, Path, _Walk]:
        """A stored snapshot naming ``alpha`` and ``gone``, and a fresh loader over it.

        The fresh loader's walk finds only ``alpha``, which is what the tree looks
        like once ``gone`` is deleted: a list naming ``gone`` is the stale answer.
        """
        alpha = _skill(self.root, "alpha")
        gone = _skill(self.root, "gone")
        first = self.loader()
        # A real cold build, so it runs under a patient budget whatever the test
        # set: on a loaded runner it outlasts `instant_budget`'s 50 ms.
        with monkeypatch.context() as patient:
            patient.setattr(skills_module, "_COLD_CATALOG_WAIT_SECS", _WAIT)
            assert sorted(_keys(first._iter())) == ["alpha", "gone"]
        _await(lambda: first._load_catalog_snapshot("") is not None, "the snapshot")
        second = self.loader()
        walk = self.stub_walk(second, monkeypatch, rows=[("alpha", alpha, None)], blocked=blocked)
        return second, alpha, gone, walk

    def fail_drops(self, loader: SkillsLoader, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        """Fail *loader*'s drops, as a neighbour holding the write lock would.

        Returns the names of the threads that attempted one, so a test can tell
        the invalidation's own attempt from a retry.
        """
        index = loader._search_index
        assert index is not None
        attempts: list[str] = []

        def failing() -> bool:
            attempts.append(threading.current_thread().name)
            return False

        monkeypatch.setattr(index, "drop_catalog", failing)
        return attempts

    def expire(self, loader: SkillsLoader, scope: str = "") -> None:
        """Push *scope*'s in-memory deadline into the past, as the TTL would."""
        with loader._catalog_lock:
            _deadline, rows = loader._iter_cache[scope]
            loader._iter_cache[scope] = (time.monotonic() - 1.0, rows)

    def settle(self) -> None:
        for event in self._gates:
            event.set()
        for loader in self._loaders:
            loader.close()
            worker = loader._catalog_worker
            if worker is not None:
                worker.join(timeout=_WAIT)
                assert not worker.is_alive(), "a refresh worker outlived its loader"


@pytest.fixture
def harness(tmp_path: Path):
    built = _Harness(tmp_path)
    try:
        yield built
    finally:
        built.settle()


@pytest.fixture
def instant_budget(monkeypatch: pytest.MonkeyPatch):
    """Reach the cold-start budget's expiry without spending two seconds on it."""
    monkeypatch.setattr(skills_module, "_COLD_CATALOG_WAIT_SECS", 0.05)


def _keys(rows: list[tuple[str, Path, str | None]]) -> list[str]:
    return [name for name, _path, _within in rows]


@pytest.fixture
def patient_budget(monkeypatch: pytest.MonkeyPatch):
    """A cold budget that only a broken fence can exhaust.

    The stub walk is instant, so this only has to outlast a slow runner; a broken
    fence fails by name long before it is spent.
    """
    monkeypatch.setattr(skills_module, "_COLD_CATALOG_WAIT_SECS", _WAIT)


def _on_thread(target, what: str):
    """Run *target* on its own thread and return its result, bounded by ``_WAIT``.

    A test that re-enters the loader from inside a patched seam does it here, so
    a later refactor that holds a lock across that seam fails by name instead of
    deadlocking the worker.
    """
    result: list = []
    thread = threading.Thread(target=lambda: result.append(target()), daemon=True)
    thread.start()
    thread.join(timeout=_WAIT)
    assert not thread.is_alive(), f"{what} never returned"
    assert result, f"{what} raised"
    return result[0]


def _trusted(loader: SkillsLoader) -> bool:
    with loader._catalog_lock:
        return catalog_module._snapshot_trusted_locked(loader)


def _await(condition, what: str) -> None:
    """Poll *condition* until true, so no assertion rests on a fixed sleep."""
    deadline = time.monotonic() + _WAIT
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


class TestNoTurnWaitsForAWalk:
    def test_an_expired_deadline_serves_the_previous_list(self, harness, monkeypatch):
        _skill(harness.root, "alpha")
        loader = harness.loader()
        assert _keys(loader._iter()) == ["alpha"]

        harness.expire(loader)
        walk = harness.stub_walk(loader, monkeypatch, rows=[])

        # The caller arrives after expiry and is served the list it already had,
        # while the re-walk it triggered is still blocked.
        assert _keys(loader._iter()) == ["alpha"]
        walk.await_start()
        assert walk.gate is not None and not walk.gate.is_set()

        walk.gate.set()
        _await(lambda: loader._iter() == [], "the background walk to publish")

    def test_a_failed_walk_leaves_the_previous_list_serving(self, harness, monkeypatch):
        _skill(harness.root, "alpha")
        loader = harness.loader()
        assert _keys(loader._iter()) == ["alpha"]
        harness.expire(loader)

        def explode(project_key: str | None = None):
            raise OSError("the tree could not be read")

        monkeypatch.setattr(loader, "_iter_uncached", explode)
        assert _keys(loader._iter()) == ["alpha"]
        # The worker survives a failed walk and keeps draining.
        _await(lambda: not loader._catalog_refreshes, "the failed walk to be retired")
        assert _keys(loader._iter()) == ["alpha"]

    def test_concurrent_readers_share_one_walk(self, harness, monkeypatch, instant_budget):
        loader = harness.loader()
        walk = harness.stub_walk(loader, monkeypatch, rows=[])
        ready = threading.Barrier(5, timeout=_WAIT)
        results: list[list] = []
        lock = threading.Lock()

        def read() -> None:
            ready.wait()
            rows = loader._iter()
            with lock:
                results.append(rows)

        threads = [threading.Thread(target=read, name=f"reader-{i}") for i in range(4)]
        try:
            for thread in threads:
                thread.start()
            ready.wait()
            for thread in threads:
                thread.join(timeout=_WAIT)
                assert not thread.is_alive(), "a reader blocked past the cold budget"
        finally:
            assert walk.gate is not None
            # Asserted BEFORE the release: every reader returned while the one
            # shared walk was still in flight, which is the property under test.
            still_blocked = not walk.gate.is_set()
            walk.gate.set()

        assert still_blocked, "the readers did not overlap the walk they share"
        assert len(results) == 4
        assert walk.calls == 1

    def test_a_scope_served_from_a_snapshot_starts_no_worker(self, harness, monkeypatch):
        _skill(harness.root, "alpha")
        first = harness.loader()
        assert _keys(first._iter()) == ["alpha"]
        _await(
            lambda: first._search_index is not None
            and first._search_index.catalog_snapshot(first._catalog_scope_id("")) is not None,
            "the snapshot to be stored",
        )

        second = harness.loader()
        monkeypatch.setattr(
            second, "_iter_uncached", lambda project_key=None: pytest.fail("walked the tree")
        )
        assert _keys(second._iter()) == ["alpha"]
        assert second._catalog_worker is None


class TestTheStoredSnapshot:
    def test_a_fresh_loader_reuses_it_instead_of_walking(self, harness, monkeypatch):
        _skill(harness.root, "alpha")
        _skill(harness.root, "team/beta")
        first = harness.loader()
        expected = _keys(first._iter())
        assert sorted(expected) == ["alpha", "team/beta"]
        _await(lambda: first._load_catalog_snapshot("") is not None, "the snapshot to be stored")

        second = harness.loader()
        walk = harness.stub_walk(second, monkeypatch, rows=[], blocked=False)
        assert _keys(second._iter()) == expected, "precedence order must survive a round trip"
        assert walk.calls == 0

    def test_an_empty_scope_is_not_rewalked(self, harness, monkeypatch):
        first = harness.loader()
        assert first._iter() == []
        _await(lambda: first._load_catalog_snapshot("") is not None, "the empty snapshot")

        second = harness.loader()
        walk = harness.stub_walk(second, monkeypatch, rows=[], blocked=False)
        assert second._iter() == []
        assert walk.calls == 0, "an enumerated-empty scope must not re-walk every turn"

    def test_it_is_not_shared_across_root_sets(self, harness, monkeypatch):
        _skill(harness.root, "alpha")
        first = harness.loader()
        assert _keys(first._iter()) == ["alpha"]
        _await(lambda: first._load_catalog_snapshot("") is not None, "the snapshot")

        other_root = harness.home / "other-skills"
        other_root.mkdir()
        other = SkillsLoader(skills_path=other_root, install_builtins=False)
        harness._loaders.append(other)
        assert other._catalog_scope_id("") != first._catalog_scope_id("")
        assert other._load_catalog_snapshot("") is None

    def test_a_project_scope_is_keyed_apart_from_the_projectless_one(self, harness):
        loader = harness.loader()
        assert loader._catalog_scope_id("") != loader._catalog_scope_id(str(harness.home / "repo"))

    def test_a_falsy_project_key_selects_one_scope(self, harness, monkeypatch):
        """``None`` and ``""`` both mean "no trusted project" and must not split.

        Two scopes for one meaning would have each served the other's snapshot,
        and a caller stubbing the trust verdict the looser way would crash on a
        key the snapshot layer cannot digest.
        """
        _skill(harness.root, "alpha")
        loader = harness.loader()
        monkeypatch.setattr(loader, "_trusted_project_key", lambda project_dir: None)
        assert _keys(loader._iter(harness.home / "repo")) == ["alpha"]
        assert loader.catalog_status(harness.home / "repo") == "complete"
        with loader._catalog_lock:
            assert list(loader._iter_cache) == [""]

    def test_an_untrusted_project_reads_the_projectless_scope(self, harness):
        _skill(harness.root, "alpha")
        project = harness.home / "repo"
        (project / ".kiro" / "skills" / "local").mkdir(parents=True)
        _skill(project / ".kiro" / "skills", "local")
        skill_trust.reset_cache_for_tests()
        loader = harness.loader()
        # No grant: the project key resolves to "", so the project's own rows are
        # neither enumerated nor reachable through a stored snapshot.
        assert _keys(loader._iter(project)) == ["alpha"]
        assert loader._catalog_fingerprint_hint(project) == loader._catalog_fingerprint_hint(None)


class TestInvalidation:
    def test_a_mutation_clears_the_snapshot(self, harness):
        _skill(harness.root, "alpha")
        loader = harness.loader()
        assert _keys(loader._iter()) == ["alpha"]
        _await(lambda: loader._load_catalog_snapshot("") is not None, "the snapshot")

        loader._invalidate_iter_cache()
        assert loader._load_catalog_snapshot("") is None

    def test_a_walk_in_flight_cannot_republish_over_a_mutation(
        self, harness, monkeypatch, instant_budget
    ):
        stale = _skill(harness.root, "stale")
        loader = harness.loader()
        walk = harness.stub_walk(loader, monkeypatch, rows=[("stale", stale, None)])
        assert loader._iter() == []
        walk.await_start()
        done = loader._catalog_refreshes[""][0]

        loader._invalidate_iter_cache()
        assert walk.gate is not None
        walk.gate.set()
        assert done.wait(timeout=_WAIT), "the fenced walk never finished"

        with loader._catalog_lock:
            assert "" not in loader._iter_cache, "a fenced walk published its stale answer"
        assert loader._load_catalog_snapshot("") is None

    def test_a_mutation_queues_a_replacement_behind_a_fenced_build(self, harness, monkeypatch):
        """Nobody is waiting on this path, so the loop's retry cannot cover it.

        An invalidation that lands while a build runs fences that build. If joining
        it queued no replacement, the post-mutation re-walk would never be scheduled
        at all and the next turn would pay cold discovery.
        """
        alpha = _skill(harness.root, "alpha")
        loader = harness.loader()
        assert _keys(loader._iter()) == ["alpha"]

        walk = harness.stub_walk(loader, monkeypatch, rows=[("alpha", alpha, None)])
        harness.expire(loader)
        assert _keys(loader._iter()) == ["alpha"]
        walk.await_start()

        loader._invalidate_iter_cache()
        assert walk.gate is not None
        walk.gate.set()
        # The fenced build publishes nothing; a replacement must run after it.
        _await(lambda: walk.calls >= 2, "a replacement build after the fenced one")
        _await(lambda: _keys(loader._iter()) == ["alpha"], "the replacement to publish")

    @pytest.mark.parametrize("mutator", ["this loader, its drop failing", "a sibling loader"])
    def test_a_snapshot_read_that_races_a_mutation_is_not_served(
        self, harness, monkeypatch, instant_budget, mutator
    ):
        """A mutation between the stored read and the adoption fences the rows.

        The walk is held past the budget, so nothing papers over a fence that let
        the rows through: the turn must say "building", never name ``gone``. A
        sibling's drop is visible only through the index epoch; this loader's own
        drop that FAILED leaves the epoch unmoved and is caught by its generation.
        """
        second, _alpha, _gone, walk = harness.stale_snapshot(monkeypatch, blocked=True)
        if mutator == "a sibling loader":
            mutate = harness.loader()._invalidate_iter_cache
        else:
            harness.fail_drops(second, monkeypatch)
            mutate = second._invalidate_iter_cache
        real_load = second._load_catalog_snapshot

        def read_then_mutate(project_key):
            stored = real_load(project_key)
            _on_thread(mutate, "the mutation")
            return stored

        monkeypatch.setattr(second, "_load_catalog_snapshot", read_then_mutate)
        assert second._iter() == [], "a fenced snapshot was served"
        assert second.catalog_status() == "building"

        monkeypatch.setattr(second, "_load_catalog_snapshot", real_load)
        assert walk.gate is not None
        walk.gate.set()
        _await(lambda: _keys(second._iter()) == ["alpha"], "the walk to publish")
        assert second.catalog_status() == "complete"

    def test_the_snapshot_tier_is_off_while_a_drop_is_running(
        self, harness, monkeypatch, instant_budget
    ):
        """The generation moves before the drop, and the tier stays off until it lands.

        A read in that window finds the pre-mutation rows still stored, under an
        epoch the drop has not moved yet, so only the watermark can refuse them.
        """
        second, _alpha, _gone, _walk = harness.stale_snapshot(monkeypatch, blocked=True)
        index = second._search_index
        assert index is not None
        real_drop = index.drop_catalog
        raced: list[list[str]] = []

        def read_then_drop() -> bool:
            raced.append(_keys(_on_thread(second._iter, "the racing read")))
            return real_drop()

        monkeypatch.setattr(index, "drop_catalog", read_then_drop)
        second._invalidate_iter_cache()
        assert raced == [[]], "a read during the drop adopted the stale rows"
        assert _trusted(second)

    def test_a_failed_drop_is_landed_by_the_next_build_s_own_store(
        self, harness, monkeypatch, patient_budget
    ):
        """One walk lands the drop and stores its own rows, and nothing drops them after.

        The drop goes into the store's transaction. As a write of its own before
        the store it would refuse the walk's rows, since it moves the epoch; after
        the store it would delete them; and either one blocks the serial worker.
        """
        second, _alpha, _gone, walk = harness.stale_snapshot(monkeypatch)
        attempts = harness.fail_drops(second, monkeypatch)

        second._invalidate_iter_cache()
        assert len(attempts) == 1
        assert not _trusted(second)
        stored = second._load_catalog_snapshot("")
        assert stored is not None and "gone" in _keys(stored.rows), "the stale rows are stored"
        assert _keys(second._iter()) == ["alpha"], "an untrusted snapshot was served"

        _await(lambda: _trusted(second), "the build to land the drop")
        stored = second._load_catalog_snapshot("")
        assert stored is not None and _keys(stored.rows) == ["alpha"], "the walk's store was lost"
        assert walk.calls == 1
        assert len(attempts) == 1, "the build retried the drop as a write of its own"

    def test_a_build_s_drop_covers_only_the_generation_it_checked(self, harness, monkeypatch):
        """A mutation that overtakes the build's check is not vouched for by its drop."""
        second, _alpha, _gone, _walk = harness.stale_snapshot(monkeypatch)
        harness.fail_drops(second, monkeypatch)
        second._invalidate_iter_cache()
        lock = _CountingLock()
        monkeypatch.setattr(second, "_catalog_drop_lock", lock)
        index = second._search_index
        assert index is not None
        real_store = index.store_catalog
        overtaking: list[threading.Thread] = []

        def overtaken_store(*args, **kwargs):
            assert kwargs["drop_first"], "the build did not carry the pending drop"
            thread = threading.Thread(target=second._invalidate_iter_cache, daemon=True)
            thread.start()
            overtaking.append(thread)
            _await(lambda: lock.waiting >= 1, "the overtaking mutation to queue its drop")
            return real_store(*args, **kwargs)

        monkeypatch.setattr(index, "store_catalog", overtaken_store)
        done = second._request_catalog_refresh("")
        assert done is not None and done.wait(timeout=_WAIT), "the build never finished"
        for thread in overtaking:
            thread.join(timeout=_WAIT)
            assert not thread.is_alive(), "the overtaking mutation never returned"
        with second._catalog_lock:
            assert second._snapshot_clean_generation == 1
            assert second._catalog_generation == 2
        assert not _trusted(second), "the build's drop vouched for a later failed one"

    @pytest.mark.parametrize("mutations", [1, 2])
    def test_a_store_a_mutation_overtakes_is_dropped_after_it(
        self, harness, monkeypatch, mutations
    ):
        """A drop never runs inside a build's store, and runs once for those queued behind it.

        A mutation that moves the generation after the build's check cannot stop
        the store. Dropping first, a drop that failed would let the pre-mutation
        rows land on top under a fresh build time, for every other loader to
        adopt; dropping once per queued mutation would delete rows stored since
        and refuse other processes' walks for nothing.
        """
        _skill(harness.root, "alpha")
        loader = harness.loader()
        lock = _CountingLock()
        monkeypatch.setattr(loader, "_catalog_drop_lock", lock)
        index = loader._search_index
        assert index is not None
        real_store, real_drop = index.store_catalog, index.drop_catalog
        storing = threading.Event()
        overlapped: list[bool] = []
        overtaking: list[threading.Thread] = []

        def overtaken_store(*args, **kwargs):
            storing.set()
            try:
                for _ in range(mutations):
                    thread = threading.Thread(target=loader._invalidate_iter_cache, daemon=True)
                    thread.start()
                    overtaking.append(thread)
                _await(lambda: lock.waiting >= mutations, "the mutations to queue their drops")
                return real_store(*args, **kwargs)
            finally:
                storing.clear()

        def recorded_drop() -> bool:
            overlapped.append(storing.is_set())
            return real_drop()

        monkeypatch.setattr(index, "store_catalog", overtaken_store)
        monkeypatch.setattr(index, "drop_catalog", recorded_drop)
        done = loader._request_catalog_refresh("")
        assert done is not None and done.wait(timeout=_WAIT), "the build never finished"
        for thread in overtaking:
            thread.join(timeout=_WAIT)
            assert not thread.is_alive(), "an overtaking mutation never returned"
        assert overlapped == [False], f"drops (inside a store?): {overlapped}"
        assert index.catalog_snapshot(loader._catalog_scope_id("")) is None, "stale rows survived"
        assert _trusted(loader)

    def test_the_caches_a_mutation_changes_are_emptied_before_its_drop(self, harness, monkeypatch):
        """Only the list keeps being served while the drop waits on the database.

        The frontmatter cache is keyed by mtime, and an edit inside one timestamp
        tick keeps it, so it must not outlive the mutation by the drop's wait.
        """
        path = _skill(harness.root, "alpha")
        loader = harness.loader()
        assert _keys(loader._iter()) == ["alpha"]
        assert loader._cached_frontmatter(path, within=None)
        assert loader._vet_unconfined_path(harness.root / "unwalked" / "SKILL.md")
        index = loader._search_index
        assert index is not None
        real_drop = index.drop_catalog
        during: list[tuple[int, int, bool]] = []

        def observed_drop() -> bool:
            with loader._catalog_lock:
                listed = "" in loader._iter_cache
            during.append((len(loader._fm_cache), len(loader._read_vetted), listed))
            return real_drop()

        monkeypatch.setattr(index, "drop_catalog", observed_drop)
        loader._invalidate_iter_cache()
        assert during == [(0, 0, True)], "a cache outlived the mutation into its drop"

    def test_a_drop_that_raises_still_clears_the_list(self, harness, monkeypatch):
        _skill(harness.root, "alpha")
        loader = harness.loader()
        assert _keys(loader._iter()) == ["alpha"]
        index = loader._search_index
        assert index is not None

        def raising() -> bool:
            raise OverflowError("an index value that does not convert")

        monkeypatch.setattr(index, "drop_catalog", raising)
        with pytest.raises(OverflowError):
            loader._invalidate_iter_cache()
        with loader._catalog_lock:
            assert loader._iter_cache == {}, "the pre-mutation list kept being served"

    def test_a_cold_read_during_a_drop_keeps_its_building_mark(
        self, harness, monkeypatch, instant_budget
    ):
        """A partial answer served while the drop waits stays reported as partial.

        It describes the post-mutation tree, so clearing it once the drop returns
        would turn "still discovering" into "no skills" while the walk still runs,
        and charge the next turn the cold budget again.
        """
        loader = harness.loader()
        walk = harness.stub_walk(loader, monkeypatch, rows=[])
        dropping, release = threading.Event(), harness.gate()

        def held_drop(_loader, _generation) -> None:
            dropping.set()
            assert release.wait(timeout=_WAIT), "the drop was never released"

        monkeypatch.setattr(catalog_module, "_land_invalidation_drop", held_drop)
        mutation = threading.Thread(target=loader._invalidate_iter_cache, daemon=True)
        mutation.start()
        assert dropping.wait(timeout=_WAIT), "the drop never started"
        assert loader._iter() == []
        assert loader.catalog_status() == "building"
        release.set()
        mutation.join(timeout=_WAIT)
        assert not mutation.is_alive(), "the invalidation never returned"
        assert walk.gate is not None and not walk.gate.is_set()
        assert loader.catalog_status() == "building", "the drop wiped a post-mutation mark"

    def test_a_list_a_walk_published_during_the_drop_is_kept(self, harness, monkeypatch):
        """Only the lists cached before the drop are cleared after it."""
        alpha = _skill(harness.root, "alpha")
        beta = _skill(harness.root, "beta")
        loader = harness.loader()
        assert _keys(loader._iter()) == ["alpha", "beta"]
        harness.stub_walk(
            loader, monkeypatch, rows=[("alpha", alpha, None), ("beta", beta, None)], blocked=False
        )
        dropping, release = threading.Event(), harness.gate()

        def held_drop(_loader, _generation) -> None:
            dropping.set()
            assert release.wait(timeout=_WAIT), "the drop was never released"

        monkeypatch.setattr(catalog_module, "_land_invalidation_drop", held_drop)
        with loader._catalog_lock:
            before = loader._iter_cache[""]
        mutation = threading.Thread(target=loader._invalidate_iter_cache, daemon=True)
        mutation.start()
        assert dropping.wait(timeout=_WAIT), "the drop never started"
        done = loader._request_catalog_refresh("")
        assert done is not None and done.wait(timeout=_WAIT), "the walk never finished"
        with loader._catalog_lock:
            published = loader._iter_cache[""]
        assert published is not before, "the post-mutation walk did not publish"
        release.set()
        mutation.join(timeout=_WAIT)
        assert not mutation.is_alive(), "the invalidation never returned"
        with loader._catalog_lock:
            assert loader._iter_cache.get("") is published, "a post-mutation list was wiped"

    def test_a_mutation_s_drop_waits_no_longer_than_the_index_s_own_timeout(
        self, harness, monkeypatch, opened
    ):
        """The drop holds the handle's lock while it waits, so every reader waits as long.

        Read off the connection rather than timed: the busy timeout in force when
        the drop's ``BEGIN IMMEDIATE`` runs is the base one, so no per-drop wait is
        longer. With a zero base the held write lock refuses the drop at once.
        """
        monkeypatch.setattr(skill_search_index_module, "_BUSY_TIMEOUT_SECS", 0)
        _skill(harness.root, "alpha")
        loader = harness.loader()
        assert _keys(loader._iter()) == ["alpha"]
        _await(lambda: loader._load_catalog_snapshot("") is not None, "the snapshot")
        index = loader._search_index
        assert index is not None
        conn = index._db()
        assert conn is not None
        spy = _BusyTimeoutAtBegin(conn)
        monkeypatch.setattr(index, "_db", lambda: spy)
        holder = opened(SkillSearchIndex(index._path))
        held = holder._db()
        assert held is not None
        held.execute("BEGIN IMMEDIATE")
        try:
            loader._invalidate_iter_cache()
        finally:
            held.rollback()
        assert spy.seen == [0], f"the drop waited with busy_timeout={spy.seen}"
        assert not _trusted(loader), "a drop refused by the held lock was trusted"

    def test_a_later_successful_drop_restores_trust(self, harness, monkeypatch):
        second, _alpha, _gone, _walk = harness.stale_snapshot(monkeypatch)
        index = second._search_index
        assert index is not None
        real_drop = index.drop_catalog
        harness.fail_drops(second, monkeypatch)
        second._invalidate_iter_cache()
        assert not _trusted(second)

        monkeypatch.setattr(index, "drop_catalog", real_drop)
        second._invalidate_iter_cache()
        assert _trusted(second), "a drop that landed did not restore the snapshot tier"

    def test_a_walk_fenced_by_a_failed_drop_does_not_store(self, harness, monkeypatch):
        """With the epoch unmoved, only the generation stops a pre-mutation store."""
        second, alpha, gone, walk = harness.stale_snapshot(monkeypatch, blocked=True)
        index = second._search_index
        assert index is not None
        before = second._load_catalog_snapshot("")
        assert before is not None
        done = second._request_catalog_refresh("")
        assert done is not None
        walk.await_start()
        harness.fail_drops(second, monkeypatch)
        second._invalidate_iter_cache()
        assert walk.gate is not None
        walk.gate.set()
        assert done.wait(timeout=_WAIT), "the fenced walk never finished"
        after = index.catalog_snapshot(second._catalog_scope_id(""))
        assert after is not None and after.built_at == before.built_at, "a fenced walk stored"

    def test_a_snapshot_never_replaces_a_fresher_walk(self, harness, monkeypatch):
        """The snapshot tier fills only a miss, and checks for one before the fence.

        A walk that publishes while a stored read is in flight is the fresher
        answer, even when a mutation moved the generation meanwhile: the read
        returns it rather than refusing, waiting and walking again.
        """
        second, _alpha, _gone, walk = harness.stale_snapshot(monkeypatch)
        read_done = threading.Event()
        release = threading.Event()
        real_load = second._load_catalog_snapshot

        def held_read(project_key):
            stored = real_load(project_key)
            read_done.set()
            assert release.wait(timeout=_WAIT), "the held read was never released"
            return stored

        monkeypatch.setattr(second, "_load_catalog_snapshot", held_read)
        served: list[list[str]] = []
        reader = threading.Thread(target=lambda: served.append(_keys(second._iter())), daemon=True)
        reader.start()
        try:
            assert read_done.wait(timeout=_WAIT), "the stored read never ran"
            second._invalidate_iter_cache()
            done = second._request_catalog_refresh("")
            assert done is not None and done.wait(timeout=_WAIT), "the walk never published"
        finally:
            release.set()
            reader.join(timeout=_WAIT)
        assert not reader.is_alive()
        assert served == [["alpha"]], "the stored read replaced the walk's list"
        assert walk.calls == 1, "a fresher cached list was ignored and walked again"

    def test_a_close_during_a_build_stops_the_adoption(self, harness, monkeypatch):
        """A build in flight keeps the index open, so only ``_closed`` can refuse."""
        second, _alpha, _gone, walk = harness.stale_snapshot(monkeypatch, blocked=True)
        assert second._request_catalog_refresh("") is not None
        walk.await_start()
        real_load = second._load_catalog_snapshot

        def read_then_close(project_key):
            stored = real_load(project_key)
            _on_thread(second.close, "the close")
            assert second._catalog_building, "the index was closed under the read"
            return stored

        monkeypatch.setattr(second, "_load_catalog_snapshot", read_then_close)
        assert second._iter() == []
        with second._catalog_lock:
            assert "" not in second._iter_cache, "a closed loader adopted the snapshot"

    def test_a_drop_landing_as_the_rows_are_published_retracts_them(
        self, harness, monkeypatch, instant_budget
    ):
        """The epoch is re-read AFTER publishing, so no gap is left for a drop.

        Re-read first, a sibling's drop committing between the re-read and the
        publish would leave the pre-mutation rows served for a whole TTL.
        """
        second, _alpha, _gone, _walk = harness.stale_snapshot(monkeypatch, blocked=True)
        sibling = harness.loader()
        real_publish = catalog_module._publish_locked
        fired: list[bool] = []

        def publish_then_drop(loader, project_key, rows):
            real_publish(loader, project_key, rows)
            if loader is second and not fired:
                fired.append(True)
                _on_thread(sibling._invalidate_iter_cache, "the sibling's mutation")

        monkeypatch.setattr(catalog_module, "_publish_locked", publish_then_drop)
        assert second._iter() == [], "rows a drop overtook stayed served"
        assert fired == [True]
        with second._catalog_lock:
            assert "" not in second._iter_cache, "the overtaken rows stayed cached"

    # NULL (and NaN, which SQLite stores as NULL) is refused by the NOT NULL column.
    # A REAL past the 64-bit range is what an INTEGER column keeps it as.
    @pytest.mark.parametrize("value", [9e999, 1e300, float(2**63), 1.5, "abc", b"\x00"])
    @pytest.mark.parametrize("table", ["skill_catalog_epoch", "skill_catalog_scope"])
    def test_an_unusable_epoch_walks_once_and_never_raises(
        self, harness, monkeypatch, patient_budget, value, table
    ):
        """The index is agent-writable, so its epoch can be any SQLite value."""
        _skill(harness.root, "alpha")
        first = harness.loader()
        assert _keys(first._iter()) == ["alpha"]
        _await(lambda: first._load_catalog_snapshot("") is not None, "the snapshot")
        index = first._search_index
        assert index is not None
        conn = index._db()
        assert conn is not None
        conn.execute(f"UPDATE {table} SET epoch = ?", (value,))
        conn.commit()

        second = harness.loader()
        real_walk = second._iter_uncached
        walks: list[str | None] = []

        def counted(project_key=None):
            walks.append(project_key)
            return real_walk(project_key)

        monkeypatch.setattr(second, "_iter_uncached", counted)
        assert [row["key"] for row in second.list_skills()] == ["alpha"]
        assert "alpha" in second.get_context(budget=4000)
        assert [row["key"] for row in second.list_skills()] == ["alpha"]
        assert len(walks) <= 1, f"{len(walks)} walks over an unusable epoch"

        assert first._search_index is not None and first._search_index.drop_catalog()
        assert isinstance(first._search_index.catalog_epoch(), int), "a drop did not heal it"

    def test_an_oversized_catalog_is_rejected_whole(self, tmp_path, monkeypatch):
        """The index is agent-writable, so its row count is adversary-controlled.

        Rejecting whole, rather than truncating, is the point: a truncated catalog
        would be served as if it were the complete enumeration.
        """
        monkeypatch.setattr(skill_search_index_module, "_MAX_CATALOG_ROWS", 3)
        index = SkillSearchIndex(tmp_path / SKILL_SEARCH_INDEX_FILENAME)
        try:
            epoch = index.catalog_epoch()
            assert epoch is not None
            rows = [(f"k{i}", f"/p/{i}", "") for i in range(3)]
            assert index.store_catalog("scope", rows, epoch=epoch) == "stored"
            got = index.catalog_snapshot("scope")
            assert got is not None and len(got[0]) == 3

            epoch = index.catalog_epoch()
            assert epoch is not None
            assert (
                index.store_catalog("scope", rows + [("k3", "/p/3", "")], epoch=epoch) == "stored"
            )
            assert index.catalog_snapshot("scope") is None, "an oversized table was served"
        finally:
            index.close()

    def test_a_catalog_field_over_the_cap_is_rejected_whole(self, tmp_path, monkeypatch):
        monkeypatch.setattr(skill_search_index_module, "_MAX_CATALOG_FIELD_CHARS", 16)
        index = SkillSearchIndex(tmp_path / SKILL_SEARCH_INDEX_FILENAME)
        try:
            epoch = index.catalog_epoch()
            assert epoch is not None
            assert (
                index.store_catalog("scope", [("k", "/p/" + "x" * 64, "")], epoch=epoch) == "stored"
            )
            assert index.catalog_snapshot("scope") is None
        finally:
            index.close()

    def test_the_index_refuses_a_write_whose_epoch_moved(self, tmp_path):
        index = SkillSearchIndex(tmp_path / SKILL_SEARCH_INDEX_FILENAME)
        try:
            epoch = index.catalog_epoch()
            assert epoch is not None
            assert index.store_catalog("scope", [("k", "/p", "")], epoch=epoch) == "stored"

            index.drop_catalog()
            assert index.catalog_snapshot("scope") is None
            # A walk that started before the drop still holds the old epoch.
            assert index.store_catalog("scope", [("k", "/p", "")], epoch=epoch) == "stale"
            assert index.catalog_snapshot("scope") is None

            moved = index.catalog_epoch()
            assert moved is not None and moved != epoch
            assert index.store_catalog("scope", [("k", "/p", "")], epoch=moved) == "stored"
        finally:
            index.close()

    def test_the_index_refuses_a_write_with_no_epoch(self, tmp_path):
        index = SkillSearchIndex(tmp_path / SKILL_SEARCH_INDEX_FILENAME)
        try:
            assert index.store_catalog("scope", [("k", "/p", "")], epoch=None) == "unavailable"
            assert index.catalog_snapshot("scope") is None
        finally:
            index.close()

    def test_a_snapshot_read_does_not_tear_against_a_drop(self, tmp_path, monkeypatch, opened):
        """The scope row and its rows come from one transaction.

        As two statements, a drop committed by another connection between them
        would answer the scope's build time with no rows: a complete, empty catalog
        that serves a turn no skills at all.
        """
        path = tmp_path / SKILL_SEARCH_INDEX_FILENAME
        index = opened(SkillSearchIndex(path))
        other = opened(SkillSearchIndex(path))
        rows = [("alpha", "/p/alpha", ""), ("beta", "/p/beta", "")]
        assert index.store_catalog("scope", rows, epoch=index.catalog_epoch()) == "stored"
        conn = index._db()
        assert conn is not None
        proxy = _DropAfterScopeRead(conn, other)
        monkeypatch.setattr(index, "_conn", proxy)
        stored = index.catalog_snapshot("scope")
        assert proxy.fired, "the drop never ran between the reads"
        assert stored is not None and stored.rows == rows, "a torn read was served"
        assert other.catalog_epoch() != stored.epoch, "the drop is not visible to a fence"

    def test_a_drop_reports_a_held_write_lock(self, tmp_path, monkeypatch, opened):
        """``False`` is what keeps the loader from trusting the stored rows."""
        monkeypatch.setattr(skill_search_index_module, "_BUSY_TIMEOUT_SECS", 0.05)
        path = tmp_path / SKILL_SEARCH_INDEX_FILENAME
        index = opened(SkillSearchIndex(path))
        holder = opened(SkillSearchIndex(path))
        assert index.store_catalog("scope", [], epoch=index.catalog_epoch()) == "stored"
        conn = holder._db()
        assert conn is not None
        conn.execute("BEGIN IMMEDIATE")
        try:
            assert index.drop_catalog() is False
        finally:
            conn.rollback()
        assert index.catalog_snapshot("scope") is not None, "a failed drop dropped rows"
        assert index.drop_catalog() is True
        assert index.catalog_snapshot("scope") is None

    def test_a_leaked_transaction_does_not_wedge_the_catalog(self, tmp_path, opened):
        """A transaction an earlier failure left open is ended, not built upon."""
        index = opened(SkillSearchIndex(tmp_path / SKILL_SEARCH_INDEX_FILENAME))
        rows = [("alpha", "/p/alpha", "")]
        assert index.store_catalog("scope", rows, epoch=index.catalog_epoch()) == "stored"
        conn = index._db()
        assert conn is not None
        conn.execute("BEGIN")
        conn.execute("SELECT 1 FROM skill_catalog").fetchall()
        stored = index.catalog_snapshot("scope")
        assert stored is not None and stored.rows == rows
        conn.execute("BEGIN")
        assert index.drop_catalog() is True
        assert index.catalog_snapshot("scope") is None

    def test_the_top_of_the_epoch_range_never_becomes_a_real(self, tmp_path, opened):
        """``+ 1`` past the 64-bit range would store a REAL, which no read accepts."""
        index = opened(SkillSearchIndex(tmp_path / SKILL_SEARCH_INDEX_FILENAME))
        conn = index._db()
        assert conn is not None
        top = 2**63 - 1
        rows = [("k", "/p", "")]
        for land in ("drop", "store"):
            conn.execute("UPDATE skill_catalog_epoch SET epoch = ?", (top,))
            conn.commit()
            assert index.catalog_epoch() == top
            assert index.store_catalog("scope", rows, epoch=top) == "stored"
            if land == "drop":
                assert index.drop_catalog() is True
            else:
                assert index.store_catalog("scope", rows, epoch=top, drop_first=True) == "stored"
            kind = conn.execute("SELECT typeof(epoch) FROM skill_catalog_epoch").fetchone()[0]
            assert kind == "integer", f"the {land} stored a {kind} epoch"
            epoch = index.catalog_epoch()
            assert epoch is not None and epoch != top
            assert index.store_catalog("scope", rows, epoch=epoch) == "stored"

    def test_a_drop_at_the_top_never_rewrites_the_epoch_it_replaces(self, tmp_path, opened):
        """A hand-written neighbour one below the top must not steer the reset back onto it."""
        index = opened(SkillSearchIndex(tmp_path / SKILL_SEARCH_INDEX_FILENAME))
        conn = index._db()
        assert conn is not None
        top = 2**63 - 1
        conn.execute("UPDATE skill_catalog_epoch SET epoch = ?", (top,))
        conn.commit()
        assert index.store_catalog("scope", [("k", "/p", "")], epoch=top) == "stored"
        conn.execute(
            "INSERT INTO skill_catalog_scope (scope, built_at, epoch) VALUES ('other', 0, ?)",
            (top - 1,),
        )
        conn.commit()
        assert index.drop_catalog() is True
        epoch = index.catalog_epoch()
        assert epoch is not None and epoch != top, "the reset landed on the epoch it replaced"
        assert index.store_catalog("scope", [("k", "/p", "")], epoch=top) == "stale"

    def test_a_value_that_does_not_bind_rolls_the_store_back(self, tmp_path, opened):
        """A store that fails mid-transaction must not keep holding the write lock."""
        index = opened(SkillSearchIndex(tmp_path / SKILL_SEARCH_INDEX_FILENAME))
        epoch = index.catalog_epoch()
        assert epoch is not None
        assert index.store_catalog("scope", [("k", "/p", 2**70)], epoch=epoch) == "unavailable"
        assert index._conn is not None and not index._conn.in_transaction
        assert index.store_catalog("scope", [("k", "/p", "")], epoch=epoch) == "stored"

    def test_a_store_can_land_a_pending_drop_in_its_own_transaction(self, tmp_path, opened):
        index = opened(SkillSearchIndex(tmp_path / SKILL_SEARCH_INDEX_FILENAME))
        epoch = index.catalog_epoch()
        assert epoch is not None
        assert index.store_catalog("other", [("old", "/p/old", "")], epoch=epoch) == "stored"
        rows = [("k", "/p/k", "")]
        assert index.store_catalog("scope", rows, epoch=epoch, drop_first=True) == "stored"
        assert index.catalog_snapshot("other") is None, "the drop left another scope's rows"
        stored = index.catalog_snapshot("scope")
        assert stored is not None and stored.rows == rows
        assert stored.epoch == index.catalog_epoch() != epoch, "the rows predate the drop"

    @pytest.mark.parametrize("version", [9e999, 1e300, 1.5, "abc"])
    def test_a_schema_version_that_is_not_an_integer_rebuilds_the_index(
        self, tmp_path, opened, version
    ):
        """Agent-writable too: an infinite REAL raises OverflowError out of a plain ``int()``.

        Such a version is a mismatch like any other, so the index is rebuilt and
        stays usable in every later process rather than being disabled for good.
        """
        path = tmp_path / SKILL_SEARCH_INDEX_FILENAME
        writer = SkillSearchIndex(path)
        try:
            conn = writer._db()
            assert conn is not None
            conn.execute("UPDATE skill_index_schema SET version = ?", (version,))
            conn.commit()
        finally:
            writer.close()
        for _process in range(2):
            index = opened(SkillSearchIndex(path))
            epoch = index.catalog_epoch()
            assert epoch is not None, "a non-integer schema version disabled the index"
            assert index.store_catalog("scope", [("k", "/p/k", "")], epoch=epoch) == "stored"
            assert index.drop_catalog() is True
            conn = index._db()
            assert conn is not None
            row = conn.execute("SELECT version FROM skill_index_schema").fetchone()
            assert row == (skill_search_index_module._SCHEMA_VERSION,)
            index.close()

    def test_a_refused_write_leaves_the_index_usable(self, tmp_path):
        """The epoch check runs inside the write transaction, so it must roll back.

        A refusal that left the transaction open would hold the write lock for the
        life of the process and fail every later write on this connection.
        """
        index = SkillSearchIndex(tmp_path / SKILL_SEARCH_INDEX_FILENAME)
        try:
            epoch = index.catalog_epoch()
            assert epoch is not None
            assert index.store_catalog("scope", [("k", "/p", "")], epoch=epoch + 99) == "stale"
            assert index.catalog_snapshot("scope") is None
            # The connection is still writable, and the metadata half still works.
            assert index.store_catalog("scope", [("k", "/p", "")], epoch=epoch) == "stored"
            assert index.catalog_snapshot("scope") is not None
            index.drop_catalog()
            assert index.catalog_snapshot("scope") is None
        finally:
            index.close()


class TestAnUnfinishedFirstWalk:
    def test_it_reports_building_rather_than_no_skills(self, harness, monkeypatch, instant_budget):
        loader = harness.loader()
        harness.stub_walk(loader, monkeypatch, rows=[])
        assert loader._iter() == []
        assert loader.catalog_status() == "building"

    def test_the_directory_says_so_instead_of_returning_nothing(
        self, harness, monkeypatch, instant_budget
    ):
        loader = harness.loader()
        harness.stub_walk(loader, monkeypatch, rows=[])
        context = loader.get_context(budget=4_000)
        assert "discovery in progress" in context
        # The always-loaded case is the one a silent empty answer would break.
        assert "always-loaded" in context

    def test_search_marks_the_answer_incomplete(self, harness, monkeypatch, instant_budget):
        loader = harness.loader()
        harness.stub_walk(loader, monkeypatch, rows=[])
        assert loader.search_skills_report("anything") == ([], True)

    def test_it_clears_once_the_walk_publishes(self, harness, monkeypatch, instant_budget):
        alpha = _skill(harness.root, "alpha")
        loader = harness.loader()
        walk = harness.stub_walk(loader, monkeypatch, rows=[("alpha", alpha, None)])
        assert loader._iter() == []
        assert loader.catalog_status() == "building"

        assert walk.gate is not None
        walk.gate.set()
        _await(lambda: _keys(loader._iter()) == ["alpha"], "the first walk to publish")
        assert loader.catalog_status() == "complete"
        assert loader.get_context(budget=4_000).find("discovery in progress") == -1

    def test_only_the_first_caller_pays_the_budget(self, harness, monkeypatch):
        """A SUBSEQUENT turn must not wait, even while the first walk runs.

        The budget buys a complete answer when the tree is small. Once the scope is
        marked incomplete that answer has already been given, so charging the next
        turn the same wait buys nothing — and on a big tree it made every turn until
        the walk finished pay the ceiling.
        """
        loader = harness.loader()
        harness.stub_walk(loader, monkeypatch, rows=[])
        # A budget long enough that a second wait would be unmistakable.
        monkeypatch.setattr(skills_module, "_COLD_CATALOG_WAIT_SECS", 0.4)

        first = time.monotonic()
        assert loader._iter() == []
        waited = time.monotonic() - first
        assert loader.catalog_status() == "building"

        second = time.monotonic()
        assert loader._iter() == []
        again = time.monotonic() - second
        # Compared against the FIRST call's own wait, not a fixed duration, so the
        # assertion does not depend on this host's speed.
        assert again < waited / 4, f"the second call waited {again:.3f}s after {waited:.3f}s"


class TestAnExactKeyReadDoesNotWaitForDiscovery:
    def test_it_serves_a_complete_key_while_the_walk_is_blocked(
        self, harness, monkeypatch, instant_budget
    ):
        _skill(harness.root, "alpha", body="the alpha procedure")
        loader = harness.loader()
        harness.stub_walk(loader, monkeypatch, rows=[])
        assert loader._iter() == []
        assert loader.catalog_status() == "building"

        content = loader.read_scoped_skill("alpha")
        assert content is not None and "the alpha procedure" in content

    def test_it_does_not_cross_namespaces(self, harness, monkeypatch, instant_budget):
        _skill(harness.root, "team-a/review", body="team a procedure")
        _skill(harness.root, "team-b/review", body="team b procedure")
        loader = harness.loader()
        harness.stub_walk(loader, monkeypatch, rows=[])
        assert loader._iter() == []

        content = loader.read_scoped_skill("team-b/review")
        assert content is not None and "team b procedure" in content
        assert "team a procedure" not in content
        assert loader.read_scoped_skill("review") is None, "a bare leaf must not resolve"

    def test_it_honors_the_agent_mapping(self, harness, monkeypatch, instant_budget):
        _skill(harness.root, "alpha")
        _skill(harness.root, "beta")
        loader = harness.loader()
        harness.stub_walk(loader, monkeypatch, rows=[])
        assert loader._iter() == []

        only = [str(harness.root / "beta" / "*")]
        assert loader.read_scoped_skill("beta", only=only) is not None
        assert loader.read_scoped_skill("alpha", only=only) is None

    def test_an_empty_mapping_admits_nothing(self, harness, monkeypatch, instant_budget):
        _skill(harness.root, "alpha")
        loader = harness.loader()
        harness.stub_walk(loader, monkeypatch, rows=[])
        assert loader._iter() == []
        assert loader.read_scoped_skill("alpha", only=[]) is None

    @pytest.mark.parametrize(
        "key", ["../escape", "alpha/../../escape", "/etc/passwd", "alpha/../beta"]
    )
    def test_a_key_is_never_treated_as_a_path(self, harness, monkeypatch, instant_budget, key):
        """The key is rejected BEFORE it reaches the filesystem.

        ``load_skill`` refuses the same shapes, so the read fails either way. What
        this pins is the earlier half: a caller-influenced key must not even be
        PROBED outside the skills root, because an ``is_file`` on a composed path is
        an existence oracle for the operator's home.
        """
        _skill(harness.root, "alpha")
        _skill(harness.home, "escape", body="outside the skills root")
        loader = harness.loader()
        harness.stub_walk(loader, monkeypatch, rows=[])
        assert loader._iter() == []

        real_is_file = Path.is_file
        probed: list[str] = []

        def counting_is_file(self: Path, *args, **kwargs):
            probed.append(str(self))
            return real_is_file(self, *args, **kwargs)

        monkeypatch.setattr(Path, "is_file", counting_is_file)
        assert loader.read_scoped_skill(key) is None
        outside = [
            path for path in probed if "escape" in path or path.startswith("/etc/") or ".." in path
        ]
        assert outside == [], f"probed a composed path outside the root: {outside}"

    def test_it_stops_once_the_catalog_is_authoritative(self, harness, monkeypatch):
        """A complete catalog is the only authority; no second resolution path."""
        _skill(harness.root, "alpha")
        loader = harness.loader()
        assert _keys(loader._iter()) == ["alpha"]
        assert loader.catalog_status() == "complete"

        # Absent from the enumeration and the catalog is complete: the direct
        # resolver must not answer, even though the file is on disk.
        _skill(harness.root, "late-arrival", body="written out of band")
        assert loader.read_scoped_skill("late-arrival") is None
        assert loader._exact_read_while_building("late-arrival", None, None, 99_000) is None


class TestListingCost:
    def test_a_warm_listing_takes_no_stat_per_skill(self, harness, monkeypatch):
        for index in range(5):
            _skill(harness.root, f"pack/skill-{index}")
        loader = harness.loader()
        assert len(loader._iter()) == 5
        _await(
            lambda: len(loader._catalog_fingerprint_hint(None)) == 5,
            "the walk to record its fingerprints",
        )

        real_stat = Path.stat
        stats: list[str] = []

        def counting_stat(self: Path, *args, **kwargs):
            if self.name == "SKILL.md":
                stats.append(str(self))
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", counting_stat)
        rows = loader.list_skills()
        assert len(rows) == 5
        assert stats == [], "a warm listing re-stat'ed every skill"
        assert {row["size_bytes"] for row in rows} == {
            (harness.root / "pack" / "skill-0" / "SKILL.md").stat().st_size
        }

    def test_a_loader_serving_a_snapshot_still_validates_by_stat(self, harness, monkeypatch):
        _skill(harness.root, "alpha", description="from the first walk")
        first = harness.loader()
        assert _keys(first._iter()) == ["alpha"]
        _await(lambda: first._load_catalog_snapshot("") is not None, "the snapshot")

        # A fresh process holds no fingerprints of its own, so it must NOT trust
        # the stored ones: an edit made between the two runs has to be noticed.
        _skill(harness.root, "alpha", description="edited out of band")
        second = harness.loader()
        monkeypatch.setattr(
            second, "_iter_uncached", lambda project_key=None: pytest.fail("walked the tree")
        )
        assert second._catalog_fingerprint_hint(None) == {}
        rows = second.list_skills()
        assert [row["description"] for row in rows] == ["edited out of band"]


class TestAStoredRowIsNotAnAdmission:
    """The index is an agent-writable crew-home leaf, so a row is a claim only."""

    def test_a_row_outside_every_root_refuses_the_whole_snapshot(self, harness, monkeypatch):
        """One bad row refuses the snapshot; it never silently trims it.

        A trimmed catalog would be served as the complete enumeration, so the safe
        direction is to fall back to walking.
        """
        _skill(harness.root, "alpha")
        first = harness.loader()
        assert _keys(first._iter()) == ["alpha"]
        _await(lambda: first._load_catalog_snapshot("") is not None, "the snapshot")

        outside = harness.home / "elsewhere" / "SKILL.md"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("---\nname: x\ndescription: injected\n---\nbody\n", encoding="utf-8")
        index = first._search_index
        assert index is not None
        assert (
            index.store_catalog(
                first._catalog_scope_id(""),
                [
                    ("alpha", str(harness.root / "alpha" / "SKILL.md"), ""),
                    ("injected", str(outside), ""),
                ],
                epoch=index.catalog_epoch(),
            )
            == "stored"
        )

        second = harness.loader()
        assert second._load_catalog_snapshot("") is None, "a row outside every root was served"
        # Falling back to the walk is what keeps the answer complete.
        assert _keys(second._iter()) == ["alpha"]

    @pytest.mark.parametrize("invalidate", [False, True])
    def test_a_row_swapped_to_a_sensitive_target_is_refused_at_the_read(
        self, harness, monkeypatch, invalidate
    ):
        """Containment is lexical, so the READ is where a changed file is caught.

        With *invalidate*, a mutation lands while the caller still holds the
        adopted list: the cache is emptied, and the held row is still checked.
        """
        path = _skill(harness.root, "alpha", body="the real body")
        first = harness.loader()
        assert _keys(first._iter()) == ["alpha"]
        _await(lambda: first._load_catalog_snapshot("") is not None, "the snapshot")

        second = harness.loader()
        harness.stub_walk(second, monkeypatch, rows=[], blocked=False)
        held = second._iter()
        assert _keys(held) == ["alpha"]
        if invalidate:
            second._invalidate_iter_cache()

        refused: list[str] = []
        monkeypatch.setattr(skills_module, "validate_file_path", refused.append)
        _key, held_path, within = held[0]
        assert second._read_enumerated_skill_bytes(held_path, within) is None
        assert refused == [str(path)], "the read did not re-run admission"

    def test_a_path_this_process_walked_is_admitted_without_a_recheck(self, harness, monkeypatch):
        path = _skill(harness.root, "alpha", body="the real body")
        loader = harness.loader()
        assert _keys(loader._iter()) == ["alpha"]
        assert str(path) in loader._walk_vetted

        calls: list[str] = []
        monkeypatch.setattr(skills_module, "validate_file_path", calls.append)
        raw = loader._read_enumerated_skill_bytes(path, None)
        assert raw is not None and b"the real body" in raw
        assert calls == [], "a walked path paid an admission re-check"

    def test_an_unvetted_path_is_checked_before_it_is_read(self, harness, monkeypatch):
        """Fail-closed: a path no walk returned, from anywhere, is checked first."""
        loader = harness.loader()
        stray = _skill(harness.home / "elsewhere", "stray", body="not a walked skill")
        calls: list[str] = []
        monkeypatch.setattr(skills_module, "validate_file_path", calls.append)
        assert loader._read_enumerated_skill_bytes(stray, None) is None
        assert calls == [str(stray)], "an unvetted path was read unchecked"

    def test_the_paths_a_check_admitted_stay_bounded(self, harness, monkeypatch):
        """The stored rows are agent-influenced, so what they vet must not grow with them."""
        monkeypatch.setattr(skills_module, "_VETTED_READS_MAX", 50)
        loader = harness.loader()
        for cycle in range(10):
            for i in range(100):
                assert loader._vet_unconfined_path(harness.root / f"f{cycle}-{i}" / "SKILL.md")
            assert len(loader._read_vetted) <= 50
        assert str(harness.root / "f9-99" / "SKILL.md") in loader._read_vetted
        loader._invalidate_iter_cache()
        assert not loader._read_vetted, "an invalidation kept paths a check admitted"

    def test_the_admitted_roots_are_resolved_once_per_root_set(self, harness, monkeypatch):
        """A vet per unvetted row must not re-resolve every root per row."""
        loader = harness.loader()
        resolved: list[None] = []
        real = skills_module._trusted_skill_roots
        monkeypatch.setattr(
            skills_module, "_trusted_skill_roots", lambda: resolved.append(None) or real()
        )
        for i in range(5):
            assert loader._vet_unconfined_path(harness.root / f"row-{i}" / "SKILL.md")
        assert len(resolved) == 1, f"the roots were resolved {len(resolved)} times"

        extra = harness.home / "extra"
        outside = _skill(extra, "ext")
        assert not loader._vet_unconfined_path(outside)
        loader._adopt_extra_paths([extra.resolve()])
        assert loader._vet_unconfined_path(outside), "a root-set change kept the old roots"
        assert len(resolved) == 2

    def test_admission_is_paid_once_per_path(self, harness, monkeypatch):
        path = _skill(harness.root, "alpha", body="the real body")
        first = harness.loader()
        assert _keys(first._iter()) == ["alpha"]
        _await(lambda: first._load_catalog_snapshot("") is not None, "the snapshot")

        second = harness.loader()
        harness.stub_walk(second, monkeypatch, rows=[], blocked=False)
        assert _keys(second._iter()) == ["alpha"]

        calls: list[str] = []
        real = skills_module.validate_file_path
        monkeypatch.setattr(
            skills_module,
            "validate_file_path",
            lambda candidate: calls.append(candidate) or real(candidate),
        )
        assert second._read_enumerated_skill_bytes(path, None) is not None
        assert second._read_enumerated_skill_bytes(path, None) is not None
        assert len(calls) == 1, f"admission ran {len(calls)} times"

    def test_a_walk_vets_exactly_the_paths_it_returned(self, harness, monkeypatch):
        """A walk's set REPLACES the previous one.

        A row the walk rejected, or a skill deleted since, falls out of it, so a
        reader still holding an older list has that row checked again.
        """
        second, alpha, gone, walk = harness.stale_snapshot(monkeypatch, blocked=True)
        assert sorted(_keys(second._iter())) == ["alpha", "gone"]
        assert not second._walk_vetted, "a stored row was vetted without a check"

        harness.expire(second)
        assert second._iter()
        assert walk.gate is not None
        walk.gate.set()
        _await(lambda: bool(second._walk_vetted), "the walk to publish")
        assert second._walk_vetted == frozenset({str(alpha)})

        calls: list[str] = []
        real = skills_module.validate_file_path
        monkeypatch.setattr(
            skills_module, "validate_file_path", lambda c: calls.append(c) or real(c)
        )
        assert second._read_enumerated_skill_bytes(alpha, None) is not None
        second._read_enumerated_skill_bytes(gone, None)
        assert calls == [str(gone)], "only the row the walk did not return is checked"

    def test_only_the_newest_walk_vouches_for_a_path(self, harness, monkeypatch):
        """Whatever scope published last: an older walk of another one vouches for nothing.

        Every scope enumerates the same unconfined roots, so the newest walk is
        the freshest view of them. A path it dropped (deleted, or rejected) is
        checked again, and so is one a check admitted before it.
        """
        alpha = _skill(harness.root, "alpha")
        gone = _skill(harness.root, "gone")
        loader = harness.loader()
        assert sorted(_keys(loader._iter())) == ["alpha", "gone"]
        assert str(gone) in loader._walk_vetted
        unwalked = harness.root / "unwalked" / "SKILL.md"
        assert loader._vet_unconfined_path(unwalked)
        assert str(unwalked) in loader._read_vetted

        harness.stub_walk(loader, monkeypatch, rows=[("alpha", alpha, None)], blocked=False)
        with loader._catalog_lock:
            generation = loader._catalog_generation
        loader._run_catalog_build("another-scope", generation)
        assert loader._walk_vetted == frozenset({str(alpha)})
        assert not loader._read_vetted, "a check admitted before the walk outlived it"

        calls: list[str] = []
        real = skills_module.validate_file_path
        monkeypatch.setattr(
            skills_module, "validate_file_path", lambda c: calls.append(c) or real(c)
        )
        loader._read_enumerated_skill_bytes(gone, None)
        loader._read_enumerated_skill_bytes(alpha, None)
        assert calls == [str(gone)], "an older walk still vouched for a dropped path"

    def test_a_check_that_straddles_an_invalidation_is_not_remembered(self, harness, monkeypatch):
        """The check ran against the tree before the mutation, so it vouches for nothing after."""
        loader = harness.loader()
        path = harness.root / "unwalked" / "SKILL.md"
        real = skills_module.validate_file_path

        def check_then_mutate(candidate):
            result = real(candidate)
            _on_thread(loader._invalidate_iter_cache, "the mutation")
            return result

        monkeypatch.setattr(skills_module, "validate_file_path", check_then_mutate)
        assert loader._vet_unconfined_path(path)
        assert str(path) not in loader._read_vetted, "a pre-mutation check was remembered"

    def test_a_row_resolving_outside_every_root_is_refused_at_the_read(self, harness, monkeypatch):
        """The read applies the walk's containment, not just its sensitive-path screen.

        The target here is an ordinary file, so only containment refuses it: the
        walk would not list it, and a stored row naming it must not be read either.
        """
        path = _skill(harness.root, "alpha")
        first = harness.loader()
        assert _keys(first._iter()) == ["alpha"]
        _await(lambda: first._load_catalog_snapshot("") is not None, "the snapshot")
        outside = _skill(harness.home / "outside", "foreign", description="OUTSIDE-FILE")
        # The skill directory, not the file, becomes the link: a directory
        # junction needs no privilege on Windows, so this runs on every host.
        path.unlink()
        path.parent.rmdir()
        make_dir_link(path.parent, outside.parent)
        assert Path(os.path.realpath(path)) == Path(os.path.realpath(outside))
        assert skills_module.validate_file_path(str(path)) is not None

        second = harness.loader()
        harness.stub_walk(second, monkeypatch, rows=[], blocked=False)
        held = second._iter()
        assert _keys(held) == ["alpha"]
        refusals: list[str] = []
        assert second._read_enumerated_skill_bytes(path, None, refusal_reasons=refusals) is None
        assert refusals == ["snapshot_path_refused"]


class TestARootSetChangeInvalidatesTheCatalog:
    def test_reconfiguring_extra_paths_drops_the_stored_snapshot(self, harness):
        _skill(harness.root, "alpha")
        loader = harness.loader()
        assert _keys(loader._iter()) == ["alpha"]
        _await(lambda: loader._load_catalog_snapshot("") is not None, "the snapshot")
        before = loader._catalog_generation

        extra = harness.home / "extra-skills"
        extra.mkdir(exist_ok=True)
        loader._adopt_extra_paths([extra])
        assert loader._catalog_generation > before, "a root change did not move the generation"
        assert loader._load_catalog_snapshot("") is None

    def test_a_walk_over_the_old_roots_does_not_publish_under_the_new_scope(
        self, harness, monkeypatch, instant_budget
    ):
        """An ordinary `skills.extra_paths` edit lands mid-walk."""
        stale = _skill(harness.root, "stale")
        loader = harness.loader()
        walk = harness.stub_walk(loader, monkeypatch, rows=[("stale", stale, None)])
        assert loader._iter() == []
        walk.await_start()
        scope_before = loader._catalog_scope_id("")

        extra = harness.home / "extra-skills"
        extra.mkdir(exist_ok=True)
        # Only the root set moves: the generation check alone must not be what
        # catches this, so it is restored to what the in-flight walk captured.
        loader._extra_paths = [extra]
        assert loader._catalog_scope_id("") != scope_before

        assert walk.gate is not None
        walk.gate.set()
        _await(lambda: not loader._catalog_refreshes, "the walk to finish")
        # The in-memory list is what the next turn reads, so it is the half that
        # matters: rows walked over the OLD roots must not become the answer for the
        # new configuration. Capturing the scope before the walk keeps the PERSISTED
        # rows filed correctly on their own, so asserting only on disk would pass
        # with the publish guard removed.
        with loader._catalog_lock:
            assert "" not in loader._iter_cache, "old-root rows became the new scope's answer"
        assert loader._load_catalog_snapshot("") is None, "rows landed under the new scope"

    def test_a_row_naming_a_foreign_project_root_is_dropped(self, harness, monkeypatch):
        """A confined row's root decides what its body is read under.

        Taking the stored value on trust would let a forged row name ANY checkout
        and have it read as a granted one, without that project's consent.
        """
        _skill(harness.root, "alpha")
        first = harness.loader()
        assert _keys(first._iter()) == ["alpha"]
        _await(lambda: first._load_catalog_snapshot("") is not None, "the snapshot")

        foreign = harness.home / "not-granted"
        _skill(foreign / ".kiro" / "skills", "sneaky")
        index = first._search_index
        assert index is not None
        assert (
            index.store_catalog(
                first._catalog_scope_id(""),
                [
                    ("alpha", str(harness.root / "alpha" / "SKILL.md"), ""),
                    (
                        "sneaky",
                        str(foreign / ".kiro" / "skills" / "sneaky" / "SKILL.md"),
                        str(foreign),
                    ),
                ],
                epoch=index.catalog_epoch(),
            )
            == "stored"
        )

        second = harness.loader()
        harness.stub_walk(second, monkeypatch, rows=[], blocked=False)
        # The projectless scope selected this snapshot, so no confine root can be
        # legitimate in it at all — and one bad row refuses the whole table.
        assert second._load_catalog_snapshot("") is None

    def test_a_confined_row_whose_key_is_not_its_path_refuses_the_snapshot(self, harness):
        """The confined branch needs the same pair check as the unconfined one."""
        loader = harness.loader()
        project = harness.home / "repo"
        _skill(project / ".kiro" / "skills", "alpha")
        _skill(project / ".kiro" / "skills", "beta")
        key = str(project)
        assert (
            loader._key_denotes_path(
                "alpha",
                os.path.abspath(project / ".kiro" / "skills" / "alpha" / "SKILL.md"),
                (loader._dir,),
                (),
            )
            is False
        ), "the unconfined form must not admit a project path"

        index = loader._search_index
        assert index is not None
        scope = loader._catalog_scope_id(key)
        rows = [
            # alpha's key against beta's path, both inside the granted project.
            ("alpha", str(project / ".kiro" / "skills" / "beta" / "SKILL.md"), key),
        ]
        assert index.store_catalog(scope, rows, epoch=index.catalog_epoch()) == "stored"
        assert loader._load_catalog_snapshot(key) is None, "a mismatched confined pair was served"

        good = [("alpha", str(project / ".kiro" / "skills" / "alpha" / "SKILL.md"), key)]
        assert index.store_catalog(scope, good, epoch=index.catalog_epoch()) == "stored"
        snapshot = loader._load_catalog_snapshot(key)
        assert snapshot is not None and _keys(snapshot[0]) == ["alpha"]

    def test_a_row_whose_key_is_not_its_path_refuses_the_snapshot(self, harness):
        """The key and the path are read by DIFFERENT gates.

        A mapping is matched against the path while the body is delivered by
        re-resolving the key, so a row pairing one skill's key with another's path
        passes a mapping admitting the second and serves the first.
        """
        _skill(harness.root, "alpha")
        _skill(harness.root, "beta")
        first = harness.loader()
        assert sorted(_keys(first._iter())) == ["alpha", "beta"]
        _await(lambda: first._load_catalog_snapshot("") is not None, "the snapshot")

        index = first._search_index
        assert index is not None
        assert (
            index.store_catalog(
                first._catalog_scope_id(""),
                [
                    # `alpha`'s key against `beta`'s path: both halves are individually
                    # inside the root, and only the PAIR is wrong.
                    ("alpha", str(harness.root / "beta" / "SKILL.md"), ""),
                    ("beta", str(harness.root / "beta" / "SKILL.md"), ""),
                ],
                epoch=index.catalog_epoch(),
            )
            == "stored"
        )

        second = harness.loader()
        assert second._load_catalog_snapshot("") is None, "an unbound key/path pair was served"
        assert sorted(_keys(second._iter())) == ["alpha", "beta"]

    def test_an_admitted_provider_target_still_binds_by_its_leaf(self, harness):
        """An app's skills are symlinked in, so the stored path resolves out of root."""
        loader = harness.loader()
        provider = _trusted_skill_roots()[0]
        assert loader._key_denotes_path(
            "pack/alpha",
            os.path.join(provider, "apps", "builtins", "x", "skills", "alpha", "SKILL.md"),
            (loader._dir,),
            (provider,),
        )
        assert not loader._key_denotes_path(
            "pack/alpha",
            os.path.join(provider, "apps", "builtins", "x", "skills", "beta", "SKILL.md"),
            (loader._dir,),
            (provider,),
        )


class TestPersistBeforePublish:
    def test_a_cross_process_invalidation_stops_the_publish(self, harness, monkeypatch):
        """`store_catalog` is where another process's mutation is detected."""
        alpha = _skill(harness.root, "alpha")
        loader = harness.loader()
        index = loader._search_index
        assert index is not None
        monkeypatch.setattr(index, "store_catalog", lambda *a, **k: "stale")
        walk = harness.stub_walk(loader, monkeypatch, rows=[("alpha", alpha, None)], blocked=False)
        loader._run_catalog_build("", loader._catalog_generation)
        assert walk.calls == 1
        with loader._catalog_lock:
            assert "" not in loader._iter_cache, "stale rows were published"

    def test_missing_persistence_still_publishes(self, harness, monkeypatch):
        """A read-only home must not make every turn re-walk."""
        alpha = _skill(harness.root, "alpha")
        loader = harness.loader()
        index = loader._search_index
        assert index is not None
        monkeypatch.setattr(index, "store_catalog", lambda *a, **k: "unavailable")
        harness.stub_walk(loader, monkeypatch, rows=[("alpha", alpha, None)], blocked=False)
        loader._run_catalog_build("", loader._catalog_generation)
        with loader._catalog_lock:
            assert "" in loader._iter_cache


class TestTheWatcherStaysOffTheLoop:
    @pytest.mark.asyncio
    async def test_a_config_change_offloads_the_invalidation(self, harness, monkeypatch):
        """`_adopt_extra_paths` invalidates SQLite, which must not run on the loop."""
        loader = harness.loader()
        threads: list[str] = []
        real = loader._adopt_extra_paths

        def recording(resolved):
            threads.append(threading.current_thread().name)
            return real(resolved)

        monkeypatch.setattr(loader, "_adopt_extra_paths", recording)
        # `change.new` is a whole config; the screening reads skills.extra_paths.
        change = types.SimpleNamespace(
            new=types.SimpleNamespace(skills=types.SimpleNamespace(extra_paths=[]))
        )
        await loader._on_config_change(change)
        assert (
            threads and threads[0] != threading.current_thread().name
        ), "the adoption ran on the calling (event-loop) thread"


class TestLifecycle:
    def test_close_does_not_wait_for_a_walk(self, harness, monkeypatch, instant_budget):
        loader = harness.loader()
        walk = harness.stub_walk(loader, monkeypatch, rows=[])
        assert loader._iter() == []
        walk.await_start()
        pending = loader._catalog_refreshes[""][0]

        loader.close()
        assert loader._closed is True
        # A caller already waiting on this build is released rather than left to
        # sit out its whole budget for an answer that will never arrive.
        assert pending.is_set()
        # A closed loader refuses to queue more work rather than raising.
        assert loader._request_catalog_refresh("") is None

    def test_a_walk_that_finishes_after_close_still_persists(
        self, harness, monkeypatch, instant_budget
    ):
        """A fallback-only host converges on nothing else.

        The unsigned MCP fallback builds a loader per call and closes it as soon as
        the search returns, so discarding a finished walk's store would make every
        call on such a host re-walk the tree forever.
        """
        alpha = _skill(harness.root, "alpha")
        loader = harness.loader()
        walk = harness.stub_walk(loader, monkeypatch, rows=[("alpha", alpha, None)])
        assert loader._iter() == []
        walk.await_start()

        loader.close()
        assert walk.gate is not None
        walk.gate.set()
        worker = loader._catalog_worker
        assert worker is not None
        worker.join(timeout=_WAIT)
        assert not worker.is_alive(), "the worker did not observe close()"
        # Nothing published into the closed loader...
        with loader._catalog_lock:
            assert "" not in loader._iter_cache
        # ...but the next process finds the snapshot it wrote.
        later = harness.loader()
        blocked = harness.stub_walk(later, monkeypatch, rows=[], blocked=False)
        assert _keys(later._iter()) == ["alpha"], "the abandoned walk persisted nothing"
        assert blocked.calls == 0

    def test_a_mutation_queues_the_rewalk_itself(self, harness, monkeypatch):
        """The next turn must not be the one that starts the post-mutation walk."""
        _skill(harness.root, "alpha")
        loader = harness.loader()
        assert _keys(loader._iter()) == ["alpha"]

        beta = _skill(harness.root, "beta")
        walk = harness.stub_walk(loader, monkeypatch, rows=[("beta", beta, None)], blocked=False)
        loader._invalidate_iter_cache()
        walk.await_start()
        _await(lambda: _keys(loader._iter()) == ["beta"], "the queued re-walk to publish")
        assert walk.calls >= 1
