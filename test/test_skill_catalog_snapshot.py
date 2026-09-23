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

from kiro_crew import skill_search_index as skill_search_index_module
from kiro_crew import skill_trust
from kiro_crew import skills as skills_module
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
        assert loader.search_skills("anything") == []
        assert loader.search_incomplete is True

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

    def test_a_row_swapped_to_a_sensitive_target_is_refused_at_the_read(self, harness, monkeypatch):
        """Containment is lexical, so the READ is where a changed file is caught."""
        path = _skill(harness.root, "alpha", body="the real body")
        first = harness.loader()
        assert _keys(first._iter()) == ["alpha"]
        _await(lambda: first._load_catalog_snapshot("") is not None, "the snapshot")

        second = harness.loader()
        harness.stub_walk(second, monkeypatch, rows=[], blocked=False)
        assert _keys(second._iter()) == ["alpha"]
        assert str(path) in second._snapshot_unadmitted

        refused: list[str] = []
        monkeypatch.setattr(
            skills_module, "validate_file_path", lambda candidate: refused.append(candidate) or None
        )
        assert second._read_enumerated_skill_bytes(path, None) is None
        assert refused == [str(path)], "the read did not re-run admission"

    def test_a_path_this_process_walked_is_admitted_without_a_recheck(self, harness, monkeypatch):
        path = _skill(harness.root, "alpha", body="the real body")
        loader = harness.loader()
        assert _keys(loader._iter()) == ["alpha"]
        assert loader._snapshot_unadmitted == set()

        calls: list[str] = []
        monkeypatch.setattr(
            skills_module, "validate_file_path", lambda candidate: calls.append(candidate) or None
        )
        raw = loader._read_enumerated_skill_bytes(path, None)
        assert raw is not None and b"the real body" in raw
        assert calls == [], "a walked path paid an admission re-check"

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

    def test_a_walk_retires_the_whole_unadmitted_set(self, harness, monkeypatch):
        alpha = _skill(harness.root, "alpha")
        first = harness.loader()
        assert _keys(first._iter()) == ["alpha"]
        _await(lambda: first._load_catalog_snapshot("") is not None, "the snapshot")

        second = harness.loader()
        walk = harness.stub_walk(second, monkeypatch, rows=[("alpha", alpha, None)])
        assert _keys(second._iter()) == ["alpha"]
        assert second._snapshot_unadmitted

        harness.expire(second)
        assert _keys(second._iter()) == ["alpha"]
        assert walk.gate is not None
        walk.gate.set()
        _await(lambda: not second._snapshot_unadmitted, "the walk to retire the set")


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

    def test_a_rejected_row_keeps_its_marker_when_a_walk_publishes(self, harness, monkeypatch):
        """A walk retires only the paths IT returned.

        Clearing the whole set would hand a reader still holding the old list an
        unadmitted path the walk had just rejected.
        """
        alpha = _skill(harness.root, "alpha")
        stale = harness.root / "gone" / "SKILL.md"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("---\nname: gone\ndescription: d\n---\nbody\n", encoding="utf-8")
        first = harness.loader()
        assert sorted(_keys(first._iter())) == ["alpha", "gone"]
        _await(lambda: first._load_catalog_snapshot("") is not None, "the snapshot")

        second = harness.loader()
        # The walk finds only alpha, so `gone` must keep its unadmitted marker.
        walk = harness.stub_walk(second, monkeypatch, rows=[("alpha", alpha, None)])
        assert sorted(_keys(second._iter())) == ["alpha", "gone"]
        assert {str(alpha), str(stale)} <= second._snapshot_unadmitted

        harness.expire(second)
        assert second._iter()
        assert walk.gate is not None
        walk.gate.set()
        _await(lambda: str(alpha) not in second._snapshot_unadmitted, "the walk to retire alpha")
        assert str(stale) in second._snapshot_unadmitted, "a rejected row lost its marker"


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
