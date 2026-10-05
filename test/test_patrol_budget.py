"""Patrol budget: the nudge header shows what is left, and the script renews it.

Two halves of one contract. ``nudge_cycle_header`` puts a capped loop's
remaining cycles and runtime in front of the agent every cycle, and marks
``10% or less left`` near the end. goal-conductor's ``patrol_budget.py`` then decides
the bounds: ``check`` at arm time (interval x cycles must cover the runtime, so
time ends the loop, not a short cycle count), ``renew`` on a ``10% or less left``
cycle (one more base budget, at most three times, within the server ceiling).
The script parses the header line, so the two are pinned together here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

from kiro_crew import validation
from kiro_crew.autonudge import NUDGE_RENEW_DUE_SHARE, NudgeLoop, nudge_cycle_header

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "goal-conductor"
    / "scripts"
    / "patrol_budget.py"
)

NOW = 1_800_000_000.0


@pytest.fixture(scope="module")
def pb():
    return load_skill_script("patrol_budget_under_test", SCRIPT)


def _loop(**kw) -> NudgeLoop:
    base = dict(id="l1", slot_key="chat-1-1", message="patrol", cycle_count=0)
    base.update(kw)
    return NudgeLoop(**base)  # type: ignore[arg-type]


class TestHeader:
    def test_uncapped_loop_keeps_the_bare_tag(self):
        assert nudge_cycle_header(_loop(cycle_count=4), now=NOW) == "[auto-nudge cycle 5]"

    def test_capped_loop_adds_both_figures(self):
        loop = _loop(cycle_count=9, max_cycles=240, max_runtime_secs=86400, created_ts=NOW - 3600)
        assert nudge_cycle_header(loop, now=NOW) == (
            "[auto-nudge cycle 10]\n[patrol budget: cycle 10/240, 82800s/86400s runtime left]"
        )

    def test_runtime_without_an_anchor_is_omitted(self):
        loop = _loop(max_cycles=10, max_runtime_secs=600, created_ts=0.0)
        assert nudge_cycle_header(loop, now=NOW).endswith("[patrol budget: cycle 1/10]")

    def test_spent_runtime_is_floored_at_zero_and_due(self):
        loop = _loop(max_runtime_secs=600, created_ts=NOW - 5000)
        assert nudge_cycle_header(loop, now=NOW).endswith("0s/600s runtime left; 10% or less left]")

    def test_renew_due_turns_on_at_the_cycle_threshold(self):
        # 100 cycles: delivering cycle 89 leaves 11 (not due), cycle 90 leaves 10 (due).
        assert "10% or less left" not in nudge_cycle_header(_loop(cycle_count=88, max_cycles=100))
        assert nudge_cycle_header(_loop(cycle_count=89, max_cycles=100)).endswith(
            "; 10% or less left]"
        )

    def test_renew_due_turns_on_at_the_runtime_threshold(self):
        early = _loop(max_runtime_secs=1000, created_ts=NOW - 899)
        late = _loop(max_runtime_secs=1000, created_ts=NOW - 900)
        assert "10% or less left" not in nudge_cycle_header(early, now=NOW)
        assert "10% or less left" in nudge_cycle_header(late, now=NOW)

    @pytest.mark.parametrize(
        "bad", ["24", None, True, -5, object(), float("inf"), float("-inf"), float("nan"), 10**400]
    )
    def test_malformed_caps_give_the_bare_tag(self, bad):
        # A hand-edited store can hold any type; the fire must not raise.
        loop = _loop(cycle_count=2, max_cycles=bad, max_runtime_secs=bad, created_ts=NOW)
        assert nudge_cycle_header(loop, now=NOW) == "[auto-nudge cycle 3]"

    def test_gateway_share_matches_the_script(self, pb):
        assert NUDGE_RENEW_DUE_SHARE == pb.RENEW_THRESHOLD


class TestCheck:
    def test_matching_bounds_pass(self, pb):
        result, code = pb.check(300, 288, 86400)
        assert code == 0 and result["ok"]

    def test_long_interval_few_cycles_is_refused_with_a_fix(self, pb):
        # The reported failure: 30 minutes x 24 cycles ends after 12 h of a 24 h
        # budget, and 30 minutes is past the conductor's 900 s ceiling as well.
        result, code = pb.check(1800, 24, 86400)
        assert code == 20
        s = result["suggest"]
        assert s["interval_secs"] == 900 and s["max_cycles"] == 96
        assert s["interval_secs"] * s["max_cycles"] >= s["max_runtime_secs"] == 86400
        assert pb.check(s["interval_secs"], s["max_cycles"], s["max_runtime_secs"])[1] == 0

    @pytest.mark.parametrize(
        "interval,cycles,runtime", [(900, 2, 3600), (900, 4, 3000), (600, 10, 5000)]
    )
    def test_interval_must_fit_the_renewal_window(self, pb, interval, cycles, runtime):
        # 900 s x 2 cycles for 3600 s: the one cycle shows 75% left, the next
        # tick is past the deadline, so the loop never sees `10% or less left`.
        result, code = pb.check(interval, cycles, runtime)
        assert code == 20
        assert any("renewal window" in p for p in result["problems"])
        good = result["suggest"]
        assert good["interval_secs"] <= pb.RENEW_THRESHOLD * good["max_runtime_secs"]
        assert pb.check(good["interval_secs"], good["max_cycles"], good["max_runtime_secs"])[1] == 0

    def test_huge_bounds_are_refused_not_crashed(self, pb):
        huge = 10**400
        result, code = pb.check(huge, huge, huge)
        assert code == 20 and result["problems"]
        budget = {"cycle": 1, "max_cycles": huge, "runtime_left": huge, "max_runtime_secs": huge}
        assert pb.renew(budget, 240, 86400, open_items=1)[1] in (10, 30)

    @pytest.mark.parametrize(
        "interval,clamped", [(15, 300), (299, 300), (901, 900), (1800, 900), (86400, 900)]
    )
    def test_interval_outside_the_policy_band_is_refused_and_clamped(self, pb, interval, clamped):
        # 300..900 s, even where the server would accept the value.
        result, code = pb.check(interval, 1000, 86400)
        assert code == 20
        assert any("300..900" in p for p in result["problems"])
        s = result["suggest"]
        assert s["interval_secs"] == clamped
        assert pb.check(s["interval_secs"], s["max_cycles"], s["max_runtime_secs"])[1] == 0

    @pytest.mark.parametrize("interval", [300, 600, 900])
    def test_interval_inside_the_policy_band_passes(self, pb, interval):
        assert pb.check(interval, 1000, 86400)[1] == 0

    @pytest.mark.parametrize("runtime", [3000, 3600, 4000, 8999])
    def test_a_short_runtime_keeps_its_value_and_shortens_the_interval(self, pb, runtime):
        # The runtime is the user's time budget: only the interval moves.
        result, code = pb.check(900, 1000, runtime)
        assert code == 20
        s = result["suggest"]
        assert s["max_runtime_secs"] == runtime
        assert pb.POLICY_MIN_INTERVAL_SECS <= s["interval_secs"] == runtime // 10
        assert pb.check(s["interval_secs"], s["max_cycles"], s["max_runtime_secs"])[1] == 0

    @pytest.mark.parametrize(
        "interval,cycles,runtime", [(300, 10, 1000), (600, 1, 600), (900, 1, 300), (20, 10, 2999)]
    )
    def test_a_runtime_too_short_for_the_floor_is_refused_not_stretched(
        self, pb, interval, cycles, runtime
    ):
        # 10% of anything under 3000 s is under the 300 s floor, so no interval
        # fits. The user's runtime is never raised: refuse and let them choose.
        result, code = pb.check(interval, cycles, runtime)
        assert code == 21 and not result["ok"]
        assert result["suggest"] is None
        assert any("ask the user for a longer runtime" in p for p in result["problems"])

    def test_a_too_short_runtime_exits_21_from_the_command_line(self, pb, capsys):
        code = pb.main(
            ["check", "--interval-secs", "300", "--max-cycles", "10", "--max-runtime-secs", "1000"]
        )
        assert code == 21
        assert json.loads(capsys.readouterr().out)["suggest"] is None

    def test_policy_band_sits_inside_the_server_range(self, pb):
        assert pb.SERVER_MIN_INTERVAL_SECS <= pb.POLICY_MIN_INTERVAL_SECS == 300
        assert pb.POLICY_MAX_INTERVAL_SECS == 900 <= pb.SERVER_MAX_INTERVAL_SECS

    def test_runtime_beyond_the_cycle_ceiling_is_shortened(self, pb):
        # 300 s x 1000 cycles covers only 300000 s, so a 7-day ask is cut to that.
        result, code = pb.check(300, 960, 604800)
        assert code == 20
        assert result["suggest"] == {
            "interval_secs": 300,
            "max_cycles": 1000,
            "max_runtime_secs": 300000,
        }

    @pytest.mark.parametrize(
        "schema", [validation.MONITOR_START_SCHEMA, validation.MONITOR_UPDATE_SCHEMA]
    )
    def test_ceilings_match_the_server(self, pb, schema):
        spec = {f.name: f for f in schema.fields}
        assert spec["max_cycles"].max_val == pb.SERVER_MAX_CYCLES
        assert spec["max_runtime_secs"].max_val >= pb.POLICY_MAX_RUNTIME_SECS
        assert spec["interval_secs"].min_val == pb.SERVER_MIN_INTERVAL_SECS
        assert spec["interval_secs"].max_val == pb.SERVER_MAX_INTERVAL_SECS


class TestRenew:
    def _line(self, loop: NudgeLoop) -> str:
        return nudge_cycle_header(loop, now=NOW).splitlines()[1]

    def test_parses_the_gateway_line(self, pb):
        loop = _loop(
            cycle_count=229, max_cycles=240, max_runtime_secs=86400, created_ts=NOW - 80000
        )
        assert pb.parse_line(self._line(loop)) == {
            "cycle": 230,
            "max_cycles": 240,
            "runtime_left": 6400,
            "max_runtime_secs": 86400,
        }

    def test_first_renewal_adds_one_base_budget(self, pb):
        budget = {"cycle": 230, "max_cycles": 240, "runtime_left": 6400, "max_runtime_secs": 86400}
        result, code = pb.renew(budget, 240, 86400, open_items=2)
        assert code == 0
        assert result == {
            "action": "renew",
            "renewal": 1,
            "monitor_update": {"max_cycles": 480, "max_runtime_secs": 172800},
        }

    def test_not_due_waits(self, pb):
        budget = {"cycle": 10, "max_cycles": 240, "runtime_left": 80000, "max_runtime_secs": 86400}
        assert pb.renew(budget, 240, 86400, open_items=2)[1] == 10

    def test_no_open_items_does_not_renew(self, pb):
        budget = {"cycle": 239, "max_cycles": 240, "runtime_left": 10, "max_runtime_secs": 86400}
        assert pb.renew(budget, 240, 86400, open_items=0)[1] == 20

    def test_the_count_comes_from_the_loop_and_stops_at_three(self, pb):
        # Three base budgets already added on top of the first: ask the user.
        # A fourth (500 cycles, 5 days) stays under both ceilings, so only the
        # renewal cap can refuse it.
        budget = {"cycle": 395, "max_cycles": 400, "runtime_left": 100, "max_runtime_secs": 345600}
        result, code = pb.renew(budget, 100, 86400, open_items=1)
        assert code == 30 and result["renewals"] == 3

    def test_server_ceiling_stops_renewal(self, pb):
        budget = {"cycle": 999, "max_cycles": 1000, "runtime_left": 50, "max_runtime_secs": 604800}
        assert pb.renew(budget, 900, 500000, open_items=1)[1] == 30

    def test_a_partial_renewal_is_refused(self, pb):
        # 300 s / 288 cycles / 1 day, renewed twice: 864 cycles, 3 days. One more
        # full base would pass 1000 cycles, so no clamped half-renewal is offered.
        budget = {"cycle": 860, "max_cycles": 864, "runtime_left": 900, "max_runtime_secs": 259200}
        result, code = pb.renew(budget, 288, 86400, open_items=1)
        assert code == 30 and result["action"] == "ask_user"

    def test_check_suggestion_at_the_cycle_ceiling_is_not_renewed(self, pb):
        # check caps a long ask at a short interval at exactly 1000 cycles.
        budget = {"cycle": 950, "max_cycles": 1000, "runtime_left": 5000, "max_runtime_secs": 90000}
        assert pb.renew(budget, 1000, 90000, open_items=1)[1] == 30

    def test_every_renewal_keeps_the_check_rule(self, pb):
        interval, cycles, runtime = 300, 288, 86400
        assert pb.check(interval, cycles, runtime)[1] == 0
        m, r = cycles, runtime
        while True:
            budget = {"cycle": m, "max_cycles": m, "runtime_left": 0, "max_runtime_secs": r}
            result, code = pb.renew(budget, cycles, runtime, open_items=1)
            if code != 0:
                assert code == 30
                break
            m = result["monitor_update"]["max_cycles"]
            r = result["monitor_update"]["max_runtime_secs"]
            assert pb.check(interval, m, r)[1] == 0

    def test_threshold_text_matches_the_constant(self, pb):
        assert pb.RENEW_THRESHOLD == 0.10
        skill = (SCRIPT.parents[1] / "SKILL.md").read_text(encoding="utf-8")
        assert "10% or less of either budget is left" in " ".join(skill.split())
        assert "renew due" not in skill
        assert "at most 10% of cycles or runtime is left" in " ".join(pb.__doc__.split())
        spec = Path(__file__).resolve().parents[1] / "docs/system-specs/common/injected-messages.md"
        assert "at or under 10% of its cap" in " ".join(spec.read_text(encoding="utf-8").split())

    def test_cli_round_trip(self, pb, capsys):
        loop = _loop(
            cycle_count=229, max_cycles=240, max_runtime_secs=86400, created_ts=NOW - 80000
        )
        code = pb.main(
            [
                "renew",
                "--line",
                self._line(loop),
                "--base-cycles",
                "240",
                "--base-runtime-secs",
                "86400",
                "--open-items",
                "3",
            ]
        )
        assert code == 0
        assert json.loads(capsys.readouterr().out)["monitor_update"]["max_cycles"] == 480

    def test_cli_rejects_a_line_with_no_figures(self, pb, capsys):
        code = pb.main(
            [
                "renew",
                "--line",
                "[auto-nudge cycle 3]",
                "--base-cycles",
                "1",
                "--base-runtime-secs",
                "1",
                "--open-items",
                "1",
            ]
        )
        assert code == 2
