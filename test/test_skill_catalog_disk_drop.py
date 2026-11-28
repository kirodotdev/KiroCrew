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
    def _ttl_out_of_reach(self, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
        _floor_monkeypatch.setattr(
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
            assert "" in second._catalog_stamps, "adoption did not finish revalidating"
        # Warm reads keep the walk's stamp.
        assert _keys(second._iter()) == ["alpha"]
        _skill(harness.root, "dropped")
        assert _keys(second._iter()) == ["alpha", "dropped"]

    def test_a_drop_right_after_adoption_is_seen_by_the_next_read(
        self, harness, monkeypatch, patient_budget
    ):
        _skill(harness.root, "alpha")
        first = harness.loader()
        assert _keys(first._iter()) == ["alpha"]
        _await(lambda: first._load_catalog_snapshot("") is not None, "the snapshot")

        second = harness.loader()
        assert _keys(second._iter()) == ["alpha"]
        # No read in between: the adoption's re-walk carries its own stamp.
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

    @pytest.mark.parametrize("key", ["beta", "pack/beta"])
    @pytest.mark.parametrize("remove", [False, True], ids=["add", "remove"])
    def test_a_change_before_adoption_is_seen_on_the_first_read(
        self, harness, monkeypatch, patient_budget, key, remove
    ):
        _skill(harness.root, "alpha")
        _skill(harness.root, "pack/one")
        if remove:
            _skill(harness.root, key)
        first = harness.loader()
        first._iter()
        stored = first._load_catalog_snapshot("")
        assert stored is not None
        # Snapshot age must not trigger the refresh this test requires.
        monkeypatch.setattr(
            skills_module,
            "_CATALOG_REVALIDATE_AFTER_SECS",
            float("inf"),  # flake-ok: disable age refresh to isolate snapshot adoption
        )
        if remove:
            shutil.rmtree(harness.root / key)
        else:
            _skill(harness.root, key)
        second = harness.loader()
        assert second._load_catalog_snapshot("") == stored
        expected = ["alpha", "pack/one"] if remove else sorted(["alpha", "pack/one", key])
        assert sorted(_keys(second._iter())) == expected

    @pytest.mark.parametrize("action", ["search", "list", "read"])
    def test_unsigned_search_uses_a_fresh_loader_after_each_install(
        self, harness, monkeypatch, patient_budget, action
    ):
        from kiro_crew import mcp_core
        from kiro_crew.mcp_tools.skills import skill_search

        monkeypatch.setattr(mcp_core, "require_strict_session_key", lambda *_: (None, None))
        monkeypatch.setattr(mcp_core, "SkillsLoader", lambda **_: harness.loader())
        monkeypatch.setattr(
            skills_module,
            "_CATALOG_REVALIDATE_AFTER_SECS",
            float("inf"),  # flake-ok: disable age refresh to isolate snapshot adoption
        )
        _skill(harness.root, "alpha")
        assert "alpha" in skill_search("skill_search", {"query": "alpha"})
        for key in ("beta", "gamma"):
            _skill(harness.root, key, description=f"unique {key}")
            result = skill_search("skill_search", {"action": action, "query": key, "key": key})
            assert f"unique {key}" in result
            assert harness._loaders[-1]._closed

    def test_adoption_over_budget_waits_once_then_publishes(
        self, harness, monkeypatch, patient_budget, request
    ):
        alpha = _skill(harness.root, "alpha")
        first = harness.loader()
        assert _keys(first._iter()) == ["alpha"]
        beta = _skill(harness.root, "beta")
        second = harness.loader()
        walk = harness.stub_walk(
            second, monkeypatch, [("alpha", alpha, None), ("beta", beta, None)]
        )
        request.getfixturevalue("instant_budget")
        assert _keys(second._iter()) == ["alpha"]
        walk.await_start()
        with second._catalog_lock:
            done, _ = second._catalog_refreshes[""]
        with monkeypatch.context() as warm:
            warm.setattr(done, "wait", lambda **_: pytest.fail("charged another wait"))
            assert _keys(second._iter()) == ["alpha"]
        assert walk.calls == 1
        assert walk.gate is not None
        walk.gate.set()
        assert done.wait(timeout=FALLBACK_TIMEOUT_SECS), "revalidation did not publish"
        assert _keys(second._iter()) == ["alpha", "beta"]

    def test_a_walk_published_during_adoption_is_the_served_answer(
        self, harness, monkeypatch, patient_budget
    ):
        _skill(harness.root, "alpha")
        first = harness.loader()
        assert _keys(first._iter()) == ["alpha"]
        _skill(harness.root, "beta")
        second = harness.loader()
        adopt = second._adopt_snapshot

        def adopt_then_publish(*args, **kwargs):
            rows = adopt(*args, **kwargs)
            assert _keys(rows) == ["alpha"]
            done = second._request_catalog_refresh("")
            assert done is not None and done.wait(timeout=FALLBACK_TIMEOUT_SECS)
            return rows

        monkeypatch.setattr(second, "_adopt_snapshot", adopt_then_publish)
        assert _keys(second._iter()) == ["alpha", "beta"]
