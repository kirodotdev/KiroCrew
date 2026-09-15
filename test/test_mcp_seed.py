"""Seeding cached verdicts into config: once per server, and never re-imposed."""

from __future__ import annotations

import pytest

from kiro_crew.config.sections import _resolve_stub_servers
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
    def test_seeding_records_an_override_and_leaves_the_roster_alone(self, cache) -> None:
        """The roster belongs to whoever ships it; a local discovery is a deviation.

        Merging into ``stub_servers`` would put the seeder and the edition on one
        key: the next release either drops the seeded name or has to reconcile a
        list it thought it owned, and seeding is once-per-server so the dropped
        name is never re-added.
        """
        section = {"stub_servers": ["z"]}
        plan = plan_seed(cache=cache, verdicts={"a": (True, False)}, current_stub={"z"})
        assert apply_seed(plan, section, cache) is True
        assert section["stub_servers"] == ["z"]
        assert section["stub_overrides"] == {"a": True}
        assert _resolve_stub_servers(section) == ["z", "a"]

    def test_a_server_the_roster_already_carries_gets_no_override(self, cache) -> None:
        """It is stubbed either way, and an override that merely agrees would pin it
        against a later roster change."""
        section = {"stub_servers": ["a"]}
        plan = plan_seed(cache=cache, verdicts={"a": (True, False)}, current_stub=set())
        apply_seed(plan, section, cache)
        assert "stub_overrides" not in section
        assert _resolve_stub_servers(section) == ["a"]

    def test_a_non_list_roster_is_not_crashed_on(self, cache) -> None:
        """A hand-edited config can hold anything; seeding must not raise."""
        section = {"stub_servers": "oops"}
        plan = plan_seed(cache=cache, verdicts={"a": (True, False)}, current_stub=set())
        assert apply_seed(plan, section, cache) is True
        assert section["stub_overrides"] == {"a": True}
        assert _resolve_stub_servers(section) == ["a"]

    def test_markers_are_recorded_so_the_next_start_is_a_no_op(self, cache) -> None:
        section: dict = {}
        plan = plan_seed(cache=cache, verdicts={"a": (True, False)}, current_stub=set())
        apply_seed(plan, section, cache)

        again = plan_seed(
            cache=cache,
            verdicts={"a": (True, False)},
            current_stub=set(_resolve_stub_servers(section)),
        )
        assert again.is_empty

    def test_operator_turning_it_off_survives(self, cache) -> None:
        """End to end on the promise: seed, user removes it, restart, stays off."""
        section: dict = {}
        plan = plan_seed(cache=cache, verdicts={"a": (True, False)}, current_stub=set())
        apply_seed(plan, section, cache)
        assert _resolve_stub_servers(section) == ["a"]

        # The operator switches it off in the UI, which flips their own decision
        # rather than rewriting a roster.
        section["stub_overrides"] = {"a": False}

        plan2 = plan_seed(cache=cache, verdicts={"a": (True, False)}, current_stub=set())
        assert apply_seed(plan2, section, cache) is False
        assert _resolve_stub_servers(section) == []

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
