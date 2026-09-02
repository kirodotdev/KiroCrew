"""Seeding cached verdicts into config: once per server, and never re-imposed."""

from __future__ import annotations

import pytest

from kiro_crew.mcp_gateway import verdict_cache as vc
from kiro_crew.mcp_gateway.seed import apply_seed, plan_seed


@pytest.fixture
def cache(tmp_path) -> vc.VerdictCache:
    return vc.VerdictCache(vc.cache_path(tmp_path))


class TestPlan:
    def test_recommended_server_is_added_and_marked(self, cache) -> None:
        plan = plan_seed(cache=cache, verdicts={"a": (True, False)}, current_stub=set())
        assert plan.add_stub == ("a",)
        assert plan.mark_applied == ("a",)
        assert plan.wants_share == ()

    def test_already_applied_server_is_left_alone(self, cache) -> None:
        """The load-bearing guarantee: an operator's "off" survives every restart."""
        cache.mark_applied("a")
        plan = plan_seed(cache=cache, verdicts={"a": (True, True)}, current_stub=set())
        assert plan.is_empty

    def test_not_recommended_is_marked_so_it_is_not_reconsidered(self, cache) -> None:
        plan = plan_seed(cache=cache, verdicts={"a": (False, False)}, current_stub=set())
        assert plan.add_stub == ()
        assert plan.mark_applied == ("a",)

    def test_already_stubbed_server_is_marked_but_not_re_added(self, cache) -> None:
        plan = plan_seed(cache=cache, verdicts={"a": (True, False)}, current_stub={"a"})
        assert plan.add_stub == ()
        assert plan.mark_applied == ("a",)

    def test_share_is_reported_separately_and_never_applied_here(self, cache) -> None:
        """Seeding never flips the global sharing switch on its own."""
        plan = plan_seed(cache=cache, verdicts={"a": (True, True)}, current_stub=set())
        assert plan.wants_share == ("a",)
        section: dict = {}
        apply_seed(plan, section, cache)
        assert "enabled" not in section

    def test_plan_is_deterministic(self, cache) -> None:
        plan = plan_seed(
            cache=cache,
            verdicts={"b": (True, False), "a": (True, False)},
            current_stub=set(),
        )
        assert plan.add_stub == ("a", "b")


class TestApply:
    def test_merges_into_existing_allowlist_without_dropping_entries(self, cache) -> None:
        section = {"stub_servers": ["z"]}
        plan = plan_seed(cache=cache, verdicts={"a": (True, False)}, current_stub={"z"})
        assert apply_seed(plan, section, cache) is True
        assert section["stub_servers"] == ["a", "z"]

    def test_non_list_allowlist_is_replaced_not_crashed_on(self, cache) -> None:
        """A hand-edited config can hold anything; seeding must not raise."""
        section = {"stub_servers": "oops"}
        plan = plan_seed(cache=cache, verdicts={"a": (True, False)}, current_stub=set())
        assert apply_seed(plan, section, cache) is True
        assert section["stub_servers"] == ["a"]

    def test_markers_are_recorded_so_the_next_start_is_a_no_op(self, cache) -> None:
        section: dict = {}
        plan = plan_seed(cache=cache, verdicts={"a": (True, False)}, current_stub=set())
        apply_seed(plan, section, cache)

        again = plan_seed(
            cache=cache, verdicts={"a": (True, False)}, current_stub=set(section["stub_servers"])
        )
        assert again.is_empty

    def test_operator_turning_it_off_survives(self, cache) -> None:
        """End to end on the promise: seed, user removes it, restart, stays off."""
        section: dict = {}
        plan = plan_seed(cache=cache, verdicts={"a": (True, False)}, current_stub=set())
        apply_seed(plan, section, cache)
        assert section["stub_servers"] == ["a"]

        section["stub_servers"] = []  # the operator switches it off in the UI

        plan2 = plan_seed(cache=cache, verdicts={"a": (True, False)}, current_stub=set())
        assert apply_seed(plan2, section, cache) is False
        assert section["stub_servers"] == []

    def test_markers_survive_a_reload(self, cache, tmp_path) -> None:
        plan = plan_seed(cache=cache, verdicts={"a": (True, False)}, current_stub=set())
        apply_seed(plan, {}, cache)
        cache.flush()

        fresh = vc.load_cache(tmp_path)
        assert fresh.was_applied("a") is True
        assert plan_seed(cache=fresh, verdicts={"a": (True, False)}, current_stub=set()).is_empty

    def test_empty_plan_changes_nothing(self, cache) -> None:
        section = {"stub_servers": ["keep"]}
        assert apply_seed(plan_seed(cache=cache, verdicts={}, current_stub=set()), section, cache) is False
        assert section["stub_servers"] == ["keep"]
