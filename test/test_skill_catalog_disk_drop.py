"""A skill dropped straight into a skills root is seen without the cache TTL.

Agents install a skill by unzipping it into ``~/.kiro/crew/skills/<name>/`` and
search for it at once. The catalog cache compares a cheap stamp of the roots on
every read (``_catalog_tree_stamp``), so such a drop is listed by the next read
instead of after ``_ITER_CACHE_TTL_SECS``. The harness, helpers and budget
fixtures are shared with ``test_skill_catalog_snapshot``.
"""

from __future__ import annotations

import shutil

import pytest
import test_skill_catalog_snapshot as snapshot
from test_skill_catalog_snapshot import _await, _keys, _skill

from kiro_crew import skills as skills_module
from kiro_crew.skill_runtime import catalog as catalog_module
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

# The shared fixtures, re-exposed so pytest collects them for this module.
harness = snapshot.harness
instant_budget = snapshot.instant_budget
patient_budget = snapshot.patient_budget


class TestAnOnDiskChangeIsSeenAtOnce:
    """A skill dropped straight into a root (an ``unzip``) needs no TTL to show.

    The TTL is pinned out of reach, so any answer that reflects the change came
    from the stamp comparison, never from the deadline expiring.
    """

    @pytest.fixture(autouse=True)
    def _ttl_out_of_reach(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            skills_module,
            "_ITER_CACHE_TTL_SECS",
            10_000_000.0,  # flake-ok: a TTL that never expires, so only the stamp refreshes
        )

    def test_a_dropped_skill_is_listed_by_the_next_read(self, harness, patient_budget):
        _skill(harness.root, "alpha")
        _skill(harness.root, "pack/one")
        loader = harness.loader()
        assert _keys(loader._iter()) == ["alpha", "pack/one"]

        _skill(harness.root, "dropped")
        assert _keys(loader._iter()) == ["alpha", "dropped", "pack/one"]

        # One level down, under a container dir with no SKILL.md of its own.
        _skill(harness.root, "pack/two")
        assert _keys(loader._iter()) == ["alpha", "dropped", "pack/one", "pack/two"]

        shutil.rmtree(harness.root / "dropped")
        assert _keys(loader._iter()) == ["alpha", "pack/one", "pack/two"]

    def test_a_list_adopted_from_the_snapshot_sees_a_later_drop(
        self, harness, monkeypatch, patient_budget
    ):
        _skill(harness.root, "alpha")
        first = harness.loader()
        assert _keys(first._iter()) == ["alpha"]
        _await(lambda: first._load_catalog_snapshot("") is not None, "the snapshot")

        second = harness.loader()
        assert _keys(second._iter()) == ["alpha"]
        with second._catalog_lock:
            assert "" not in second._catalog_refreshes, "adoption walked a fresh snapshot"
        # Its first served read records the stamp the adopted list is held to.
        assert _keys(second._iter()) == ["alpha"]
        _skill(harness.root, "dropped")
        assert _keys(second._iter()) == ["alpha", "dropped"]

    def test_an_unchanged_tree_is_served_without_a_walk(self, harness, monkeypatch):
        _skill(harness.root, "alpha")
        loader = harness.loader()
        with monkeypatch.context() as patient:
            patient.setattr(skills_module, "_COLD_CATALOG_WAIT_SECS", FALLBACK_TIMEOUT_SECS)
            assert _keys(loader._iter()) == ["alpha"]
        walk = harness.stub_walk(loader, monkeypatch, rows=[], blocked=False)

        # An in-place edit is left to the TTL re-walk: it does not move the stamp.
        _skill(harness.root, "alpha", description="edited in place")
        for _ in range(3):
            assert _keys(loader._iter()) == ["alpha"]
        assert walk.calls == 0
        assert not loader._catalog_refreshes

    def test_a_walk_over_budget_charges_one_read(self, harness, monkeypatch, instant_budget):
        _skill(harness.root, "alpha")
        loader = harness.loader()
        with monkeypatch.context() as patient:
            patient.setattr(skills_module, "_COLD_CATALOG_WAIT_SECS", FALLBACK_TIMEOUT_SECS)
            assert _keys(loader._iter()) == ["alpha"]
        dropped = _skill(harness.root, "dropped")
        alpha = harness.root / "alpha" / "SKILL.md"
        walk = harness.stub_walk(
            loader, monkeypatch, rows=[("alpha", alpha, None), ("dropped", dropped, None)]
        )

        # The re-walk is held open past the budget: the read is served what was
        # cached, and the new stamp is recorded so the next read does not wait too.
        assert _keys(loader._iter()) == ["alpha"]
        walk.await_start()
        with loader._catalog_lock:
            recorded = loader._catalog_stamps[""]
        assert recorded == catalog_module._catalog_tree_stamp(loader)
        assert _keys(loader._iter()) == ["alpha"]
        assert walk.calls == 1

        assert walk.gate is not None
        walk.gate.set()
        _await(lambda: _keys(loader._iter()) == ["alpha", "dropped"], "the re-walk to publish")
