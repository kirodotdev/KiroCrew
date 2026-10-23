"""The D8 measures in `fix_loop_metrics.py`: counters, the CI-waste census, its call cap.

The weekly report is how the team learns whether the review-loop changes worked
(docs/request-for-change/rfc-review-loop-and-escaped-bug-reduction.md, D8). Two
things must hold for that to be believable:

* The counters that claim to need no API call must read only what the harvest
  already fetched, and must publish an empty denominator as "n/a", never 0.0%.
* The census is opt-in and its call cap is hard. A cap that is only a hint turns
  one Monday run into an outage of the shared hourly token pool.

Everything here runs on fixtures: no `gh`, no network, no git.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from skill_script_helpers import load_skill_script

ROOT = Path(__file__).resolve().parent.parent
METRICS_SCRIPT = ROOT / ".github" / "scripts" / "fix_loop_metrics.py"
ANALYSIS = ROOT / ".github" / "workflows" / "fix-loop-analysis.yml"
WORKFLOWS = ROOT / ".github" / "workflows"

BASE = datetime(2026, 10, 12, 8, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def mod() -> Any:
    return load_skill_script("fix_loop_metrics_d8", METRICS_SCRIPT)


def stamp(minutes: float) -> str:
    return (BASE + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def job(
    name: str, conclusion: str, start: float, minutes: float, app: str = "github-actions"
) -> dict:
    return {
        "name": name,
        "conclusion": conclusion,
        "started_at": stamp(start),
        "completed_at": stamp(start + minutes),
        "app": {"slug": app},
    }


def sha(n: int) -> str:
    return f"{n:040x}"


LINT = "Backend Lint & Type Check (3.12)"
HEAVY = "Backend Tests (3.12, 1)"


class TestSizeCounters:
    @staticmethod
    def pr(files: Any, hours: float) -> dict:
        return {
            "changedFiles": files,
            "createdAt": stamp(0),
            "mergedAt": stamp(hours * 60),
        }

    def test_boundaries(self, mod: Any) -> None:
        prs = [
            self.pr(49, 1),
            self.pr(50, 1),
            self.pr(79, 1),
            self.pr(80, 1),
            self.pr(5, 167.9),
            self.pr(5, 168),
        ]
        got = mod.size_counters(prs)
        assert got["files_ge_hint"] == 3
        assert got["files_ge_flag"] == 1
        assert got["open_ge_flag_hours"] == 1
        assert got["flagged"] == 2
        assert got["flagged_pct"] == 33.3

    def test_a_pr_that_is_both_wide_and_old_is_flagged_once(self, mod: Any) -> None:
        assert mod.size_counters([self.pr(90, 200)])["flagged"] == 1

    def test_missing_size_is_counted_not_guessed(self, mod: Any) -> None:
        got = mod.size_counters([{"createdAt": stamp(0), "mergedAt": stamp(60)}])
        assert got["size_unknown"] == 1
        assert got["flagged"] == 0

    def test_empty_window_has_no_percentage(self, mod: Any) -> None:
        assert mod.size_counters([])["flagged_pct"] is None


class TestPrCounters:
    def test_counts_merged_and_fix_titles_and_carries_the_size_rule(self, mod: Any) -> None:
        prs = [
            {"title": "fix: a", "changedFiles": 90, "createdAt": stamp(0), "mergedAt": stamp(60)},
            {"title": "fix(x): b", "changedFiles": 1, "createdAt": stamp(0), "mergedAt": stamp(60)},
            {"title": "revert: c"},
            {"title": "feat: d"},
        ]
        out = mod.pr_counters(prs)
        assert (out["merged_prs"], out["fix_prs"]) == (4, 3)
        assert out["size"]["flagged"] == 1
        assert out["size"]["size_unknown"] == 2

    def test_only_the_size_rule_is_counted_until_a_producer_exists(self, mod: Any) -> None:
        """D3, D4 and D7 add their own adoption counters with the change that writes the line."""
        assert set(mod.pr_counters([])) == {"merged_prs", "fix_prs", "size"}

    def test_no_prs_means_no_percentage(self, mod: Any) -> None:
        assert mod.pr_counters([])["size"]["flagged_pct"] is None


class TestHeadSummary:
    def test_only_github_actions_jobs_count(self, mod: Any) -> None:
        runs = [job(LINT, "success", 0, 5), job("external", "failure", 0, 50, app="other-app")]
        head = mod.summarise_head(sha(1), runs)
        assert head["seconds"] == 300
        assert head["red_local"] == [] and head["red_other"] == []

    def test_no_actions_job_means_no_head(self, mod: Any) -> None:
        assert mod.summarise_head(sha(1), [job(LINT, "success", 0, 5, app="other-app")]) is None
        assert mod.summarise_head(sha(1), []) is None

    def test_derived_jobs_are_not_a_cause(self, mod: Any) -> None:
        runs = [job(LINT, "failure", 0, 2), job("Await Fast Gate", "failure", 0, 1)]
        head = mod.summarise_head(sha(1), runs)
        assert head["red_local"] == [(LINT, 120.0)]
        assert head["red_other"] == []
        assert mod.is_static_only(head)

    def test_a_red_test_job_makes_the_push_not_static_only(self, mod: Any) -> None:
        runs = [job(LINT, "failure", 0, 2), job(HEAVY, "failure", 0, 20)]
        assert not mod.is_static_only(mod.summarise_head(sha(1), runs))

    def test_a_timed_out_job_is_a_red_cause_too(self, mod: Any) -> None:
        runs = [job(LINT, "failure", 0, 2), job(HEAVY, "timed_out", 0, 60)]
        head = mod.summarise_head(sha(1), runs)
        assert head["red_other"] == [HEAVY]
        assert not mod.is_static_only(head)

    def test_a_green_push_is_not_static_only(self, mod: Any) -> None:
        assert not mod.is_static_only(mod.summarise_head(sha(1), [job(LINT, "success", 0, 2)]))

    def test_cancelled_and_unfinished_jobs_cost_what_they_ran(self, mod: Any) -> None:
        unfinished = {"name": HEAVY, "app": {"slug": "github-actions"}, "started_at": stamp(0)}
        head = mod.summarise_head(sha(1), [job(HEAVY, "cancelled", 0, 4), unfinished])
        assert head["seconds"] == 240


class TestWasteForPr:
    def heads(self, mod: Any, *pushes: list[dict]) -> list[dict]:
        return [mod.summarise_head(sha(i), runs) for i, runs in enumerate(pushes, start=1)]

    def test_lint_red_push_followed_by_another_push_is_avoidable(self, mod: Any) -> None:
        red = [job(LINT, "failure", 0, 2), job(HEAVY, "cancelled", 0, 8)]
        green = [job(LINT, "success", 60, 2), job(HEAVY, "success", 60, 30)]
        out = mod.waste_for_pr(self.heads(mod, red, green))
        assert out["avoidable"] == 1
        assert out["avoidable_seconds"] == 600  # the whole push: 2 + 8 minutes
        assert out["avoidable_floor_seconds"] == 120  # only the red job
        assert out["first_push_red_local"] is True
        assert out["failed_jobs"] == [LINT]

    def test_the_last_push_is_never_avoidable(self, mod: Any) -> None:
        red = [job(LINT, "failure", 0, 2)]
        assert mod.waste_for_pr(self.heads(mod, red))["avoidable"] == 0

    def test_a_push_with_another_red_cause_is_not_avoidable(self, mod: Any) -> None:
        mixed = [job(LINT, "failure", 0, 2), job(HEAVY, "failure", 0, 20)]
        green = [job(LINT, "success", 60, 2)]
        out = mod.waste_for_pr(self.heads(mod, mixed, green))
        assert out["avoidable"] == 0
        assert out["first_push_red_local"] is True  # M3 counts any local-command red

    def test_heads_are_ordered_by_start_not_by_discovery(self, mod: Any) -> None:
        green_late = [job(LINT, "success", 120, 2)]
        red_early = [job(LINT, "failure", 0, 2)]
        out = mod.waste_for_pr(self.heads(mod, green_late, red_early))
        assert out["avoidable"] == 1
        assert out["first_push_red_local"] is True

    def test_green_first_push_is_not_red(self, mod: Any) -> None:
        out = mod.waste_for_pr(
            self.heads(mod, [job(LINT, "success", 0, 2)], [job(LINT, "failure", 60, 2)])
        )
        assert out["first_push_red_local"] is False
        assert out["avoidable"] == 0

    def test_no_observed_head(self, mod: Any) -> None:
        out = mod.waste_for_pr([])
        assert (out["pushes"], out["avoidable"], out["first_push_red_local"]) == (0, 0, False)


class TestAggregate:
    @staticmethod
    def rec(pushes: int, avoidable: int, minutes: float, first_red: bool = False) -> dict:
        return {
            "pushes": pushes,
            "avoidable": avoidable,
            "avoidable_seconds": minutes * 60,
            "avoidable_floor_seconds": minutes * 30,
            "first_push_red_local": first_red,
            "failed_jobs": ["Docs Lint"] * avoidable,
        }

    def test_rates_and_raw_counts(self, mod: Any) -> None:
        recs = [
            self.rec(4, 1, 30, True),
            self.rec(3, 0, 0),
            self.rec(5, 2, 60, True),
            self.rec(0, 0, 0),
        ]
        out = mod.aggregate_waste(recs)
        assert (out["prs"], out["prs_with_push"], out["pushes"]) == (4, 3, 12)
        assert (out["avoidable_pushes"], out["prs_with_avoidable"]) == (3, 2)
        assert out["m1_per_100_prs"] == 75.0
        assert out["avoidable_job_minutes"] == 90.0
        assert out["m2_job_minutes_per_pr"] == 22.5
        assert out["avoidable_floor_job_minutes"] == 45.0
        assert (out["m3_first_push_red_prs"], out["m3_pct"]) == (2, 66.7)
        assert out["failed_local_jobs"] == {"Docs Lint": 3}

    def test_empty_sample_has_no_rates(self, mod: Any) -> None:
        out = mod.aggregate_waste([])
        assert out["m1_per_100_prs"] is None
        assert out["m2_job_minutes_per_pr"] is None
        assert out["m3_pct"] is None
        assert out["m1_ci95"] is None

    def test_interval_reproduces_design_f_sampling_error(self, mod: Any) -> None:
        """Design F: mean 0.097 and sd 0.386 per PR, so 200 PRs give about +-55%."""
        counts = [1] * 19 + [0] * 181  # mean 0.095
        low, high = mod.mean_ci95_per_100(counts)
        assert low < 9.5 < high
        assert 0.4 < (high - low) / 2 / 9.5 < 0.7

    def test_interval_needs_two_prs_and_never_goes_below_zero(self, mod: Any) -> None:
        assert mod.mean_ci95_per_100([1]) is None
        low, _ = mod.mean_ci95_per_100([0, 0, 0, 0, 0, 0, 0, 0, 0, 1])
        assert low == 0.0


class TestPushedHeads:
    def test_sources_dedupe_and_order(self, mod: Any) -> None:
        commits = [{"sha": sha(1)}, {"sha": sha(2).upper()}]
        events = [
            {"event": "head_ref_force_pushed", "commit_id": sha(3)},
            {"event": "labeled", "commit_id": sha(9)},
            {"event": "head_ref_force_pushed", "commit_id": sha(1)},
        ]
        assert mod.pushed_heads(sha(4), commits, events) == [sha(1), sha(2), sha(3), sha(4)]

    def test_values_that_are_not_a_sha_never_reach_an_api_path(self, mod: Any) -> None:
        commits = [{"sha": "../../etc"}, {"sha": "abc"}, {"sha": None}, "junk"]
        events = [{"event": "head_ref_force_pushed", "commit_id": "x; rm -rf /"}]
        assert mod.pushed_heads(None, commits, events) == []


class FakeGitHub:
    """Stand-in for `gh_json("api", path)` over a small PR universe."""

    def __init__(
        self,
        runs: dict[str, list[dict]],
        commits: dict[int, list[str]] | None = None,
        forced: dict[int, list[str]] | None = None,
        remaining: Any = 5000,
        gone: frozenset[str] = frozenset(),
        broken: frozenset[str] = frozenset(),
    ) -> None:
        self.runs, self.commits, self.forced = runs, commits or {}, forced or {}
        self.remaining, self.gone, self.broken = remaining, gone, broken
        self.paths: list[str] = []

    def __call__(self, *args: str) -> Any:
        assert args[0] == "api", args
        path = args[1]
        self.paths.append(path)
        if path == "rate_limit":
            if self.remaining is None:
                raise RuntimeError("gh failed (1): HTTP 500")
            return {"resources": {"core": {"remaining": self.remaining}}}
        if m := re.fullmatch(r"repos/o/r/pulls/(\d+)/commits\?per_page=100&page=1", path):
            return [{"sha": s} for s in self.commits.get(int(m.group(1)), [])]
        if m := re.fullmatch(r"repos/o/r/issues/(\d+)/events\?per_page=100&page=1", path):
            return [
                {"event": "head_ref_force_pushed", "commit_id": s}
                for s in self.forced.get(int(m.group(1)), [])
            ]
        if m := re.fullmatch(
            r"repos/o/r/commits/([0-9a-f]{40})/check-runs\?per_page=100&page=1", path
        ):
            if m.group(1) in self.broken:
                raise RuntimeError("gh failed (1): gh: Forbidden (HTTP 403)")
            if m.group(1) in self.gone:
                raise RuntimeError("gh failed (1): No commit found for SHA (HTTP 422)")
            runs = self.runs.get(m.group(1), [])
            return {"total_count": len(runs), "check_runs": runs}
        raise AssertionError(f"unexpected API path {path}")


def pr(number: int, merged_min: float, head: str, login: str = "human") -> dict:
    return {
        "number": number,
        "mergedAt": stamp(merged_min),
        "headRefOid": head,
        "author": {"login": login},
    }


def one_wasteful_pr_universe() -> tuple[dict[str, list[dict]], dict[int, list[str]]]:
    """PR 1: a lint-red push, force-pushed away, then a green head. PR 2: one green head."""
    runs = {
        sha(1): [job(LINT, "failure", 0, 2), job(HEAVY, "cancelled", 0, 8)],
        sha(2): [job(LINT, "success", 60, 2), job(HEAVY, "success", 60, 30)],
        sha(3): [job(LINT, "success", 0, 2)],
    }
    return runs, {1: [sha(1)]}


class TestCensus:
    def census(self, mod: Any, fake: FakeGitHub, prs: list[dict], **kw: Any) -> dict:
        args = {"sample": 10, "max_calls": 1000, "reserve": 0, **kw}
        return mod.ci_waste_census("o/r", prs, fetch=fake, **args)

    def test_force_pushed_head_is_found_through_the_events(self, mod: Any) -> None:
        runs, forced = one_wasteful_pr_universe()
        fake = FakeGitHub(runs, forced=forced)
        out = self.census(mod, fake, [pr(1, 600, sha(2)), pr(2, 500, sha(3))])
        group = out["all_authors"]
        assert (group["prs"], group["pushes"], group["avoidable_pushes"]) == (2, 3, 1)
        assert group["m1_per_100_prs"] == 50.0
        assert group["avoidable_job_minutes"] == 10.0
        assert (group["m3_first_push_red_prs"], group["m3_pct"]) == (1, 50.0)
        assert out["truncated_by_budget"] is False
        assert out["calls_used"] == len(fake.paths)

    def test_the_documented_defaults(self, mod: Any) -> None:
        assert mod.CENSUS_DEFAULT_MAX_CALLS == 1000
        assert mod.CENSUS_DEFAULT_SAMPLE == 100

    def test_the_cap_is_hard_and_the_cut_pr_is_dropped_whole(self, mod: Any) -> None:
        runs, forced = one_wasteful_pr_universe()
        fake = FakeGitHub(runs, forced=forced)
        # probe 1 + PR 2 (newest): commits, events, one head = 3 calls -> 4 used.
        # PR 1 needs 2 + 2 heads = 4 more calls; a cap of 6 stops it after 2.
        out = self.census(mod, fake, [pr(1, 500, sha(2)), pr(2, 600, sha(3))], max_calls=6)
        assert len(fake.paths) <= 6
        assert out["calls_used"] == len(fake.paths)
        assert out["truncated_by_budget"] is True
        assert out["prs_sampled"] == 1
        assert out["all_authors"]["prs"] == 1
        assert out["all_authors"]["avoidable_pushes"] == 0  # half of PR 1 never entered a rate

    @pytest.mark.parametrize("cap", [1, 2, 3, 5, 8, 13])
    def test_no_cap_is_ever_exceeded(self, mod: Any, cap: int) -> None:
        runs, forced = one_wasteful_pr_universe()
        fake = FakeGitHub(runs, forced=forced)
        self.census(mod, fake, [pr(1, 500, sha(2)), pr(2, 600, sha(3))], max_calls=cap)
        assert len(fake.paths) <= cap

    def test_sample_takes_the_newest_merged_prs(self, mod: Any) -> None:
        runs, forced = one_wasteful_pr_universe()
        fake = FakeGitHub(runs, forced=forced)
        out = self.census(mod, fake, [pr(1, 500, sha(2)), pr(2, 600, sha(3))], sample=1)
        assert out["prs_sampled"] == 1
        assert any("/pulls/2/" in p for p in fake.paths)
        assert not any("/pulls/1/" in p for p in fake.paths)

    def test_rate_limit_lowers_the_cap_below_the_remaining_pool(self, mod: Any) -> None:
        runs, forced = one_wasteful_pr_universe()
        fake = FakeGitHub(runs, forced=forced, remaining=210)
        out = self.census(mod, fake, [pr(1, 500, sha(2))], reserve=200, max_calls=1000)
        assert out["calls_cap_configured"] == 1000
        assert out["calls_cap_effective"] == 10
        assert out["rate_limit_remaining_before"] == 210
        assert out["calls_used"] <= 10

    def test_an_exhausted_pool_reads_nothing_beyond_the_probe(self, mod: Any) -> None:
        runs, forced = one_wasteful_pr_universe()
        fake = FakeGitHub(runs, forced=forced, remaining=50)
        out = self.census(mod, fake, [pr(1, 500, sha(2))], reserve=200)
        assert fake.paths == ["rate_limit"]
        assert out["truncated_by_budget"] is True
        assert out["prs_sampled"] == 0

    def test_a_failed_probe_falls_back_to_the_configured_cap(self, mod: Any) -> None:
        runs, forced = one_wasteful_pr_universe()
        fake = FakeGitHub(runs, forced=forced, remaining=None)
        out = self.census(mod, fake, [pr(1, 500, sha(2))])
        assert out["rate_limit_remaining_before"] is None
        assert out["calls_cap_effective"] == 1000
        assert out["prs_sampled"] == 1

    def test_a_head_github_no_longer_serves_is_skipped_and_counted(self, mod: Any) -> None:
        runs, forced = one_wasteful_pr_universe()
        fake = FakeGitHub(runs, forced=forced, gone=frozenset({sha(1)}))
        out = self.census(mod, fake, [pr(1, 500, sha(2))])
        assert out["heads_unreadable"] == 1
        assert out["all_authors"]["pushes"] == 1

    def test_a_head_with_no_jobs_is_counted_so_the_push_count_reads_as_a_lower_bound(
        self, mod: Any
    ) -> None:
        runs, forced = one_wasteful_pr_universe()
        runs.pop(sha(1))  # the force-pushed head's runs are gone
        out = self.census(mod, FakeGitHub(runs, forced=forced), [pr(1, 500, sha(2))])
        assert out["heads_without_jobs"] == 1
        assert out["all_authors"]["pushes"] == 1

    def test_any_other_failure_is_loud(self, mod: Any) -> None:
        runs, forced = one_wasteful_pr_universe()
        fake = FakeGitHub(runs, forced=forced, broken=frozenset({sha(2)}))
        with pytest.raises(RuntimeError, match="HTTP 403"):
            self.census(mod, fake, [pr(1, 500, sha(2))])

    def test_truncated_check_run_listing_is_reported(self, mod: Any) -> None:
        class Capped(FakeGitHub):
            def __call__(self, *args: str) -> Any:
                out = super().__call__(*args)
                if isinstance(out, dict) and "check_runs" in out:
                    out["total_count"] = 99999  # server has more than we will page through
                return out

        fake = Capped({sha(3): [job(LINT, "success", 0, 2)]})
        out = self.census(mod, fake, [pr(2, 600, sha(3))])
        assert out["heads_truncated"] == 1
        assert len(fake.paths) <= 1 + 2 + 3  # probe + commits/events + API_MAX_PAGES

    def test_agent_subset_is_reported_beside_all_authors(self, mod: Any) -> None:
        runs, forced = one_wasteful_pr_universe()
        fake = FakeGitHub(runs, forced=forced)
        prs = [pr(1, 600, sha(2), login="Mate-Bot"), pr(2, 500, sha(3), login="human")]
        out = self.census(mod, fake, prs, agent_logins=frozenset({"mate-bot"}))
        assert out["agents"]["prs"] == 1
        assert out["agents"]["m1_per_100_prs"] == 100.0
        assert out["all_authors"]["prs"] == 2

    def test_no_agent_logins_means_no_agent_group(self, mod: Any) -> None:
        runs, forced = one_wasteful_pr_universe()
        out = self.census(mod, FakeGitHub(runs, forced=forced), [pr(1, 600, sha(2))])
        assert out["agents"] is None


class TestTableRows:
    def counters(self, mod: Any) -> dict:
        return mod.pr_counters([{"title": "fix: a", "body": "", "changedFiles": 3}])

    def test_census_off_says_so(self, mod: Any) -> None:
        rows = "\n".join(mod.d8_table_rows(self.counters(mod), {"enabled": False}))
        assert "CI-waste census (M1 to M3) | off (opt-in" in rows
        assert "Size: flagged" in rows

    def test_every_rate_has_its_counts_beside_it(self, mod: Any) -> None:
        runs, forced = one_wasteful_pr_universe()
        waste = mod.ci_waste_census(
            "o/r",
            [pr(1, 600, sha(2)), pr(2, 500, sha(3))],
            sample=10,
            max_calls=1000,
            reserve=0,
            fetch=FakeGitHub(runs, forced=forced),
        )
        rows = "\n".join(mod.d8_table_rows(self.counters(mod), waste))
        assert (
            "M1 Avoidable pushes per 100 merged PRs (all authors) | 50.0 (1 pushes in 1 of 2 PRs"
            in rows
        )
        assert (
            "M3 PRs whose first push is red on a local-command job (all authors) | 50.0% (1/2)"
            in rows
        )
        assert "API calls used / cap (finished)" in rows

    def test_a_cut_census_is_labelled(self, mod: Any) -> None:
        waste = mod.ci_waste_census(
            "o/r", [pr(1, 600, sha(2))], sample=5, max_calls=2, reserve=0, fetch=FakeGitHub({})
        )
        assert "CUT SHORT by the cap" in "\n".join(mod.d8_table_rows(self.counters(mod), waste))

    def test_a_census_that_read_no_pr_renders_na(self, mod: Any) -> None:
        waste = mod.ci_waste_census(
            "o/r", [pr(1, 600, sha(2))], sample=5, max_calls=1, reserve=0, fetch=FakeGitHub({})
        )
        rows = "\n".join(mod.d8_table_rows(self.counters(mod), waste))
        assert waste["prs_sampled"] == 0
        assert "None" not in rows
        assert "M1 Avoidable pushes per 100 merged PRs (all authors) | n/a" in rows

    def test_empty_denominators_render_na_never_a_number(self, mod: Any) -> None:
        counters = mod.pr_counters([])
        rows = "\n".join(mod.d8_table_rows(counters, {"enabled": False}))
        assert "None" not in rows
        assert "n/a" in rows


class TestMain:
    """`main` end to end on canned git and gh output: wiring, JSON, summary table."""

    def run_main(self, mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *extra: str):
        runs, forced = one_wasteful_pr_universe()
        github = FakeGitHub(runs, forced=forced)
        prs = [pr(1, 600, sha(2), login="mate-bot"), pr(2, 500, sha(3), login="human")]
        for item in prs:
            item.update(title="fix: a", body="")

        fields = mod.PR_LIST_FIELDS

        def fake_gh(*args: str) -> Any:
            if args[0] == "repo":
                return {"defaultBranchRef": {"name": "main"}}
            if args[0] == "api" and "/actions/workflows/" in args[1]:
                return {"workflow_runs": []}
            if args[0] == "pr":
                assert fields in args, "the PR search must carry the D8 fields"
                return prs
            return github(*args)

        monkeypatch.setattr(mod, "gh_json", fake_gh)
        monkeypatch.setattr(mod, "git", lambda *a, **k: "5\n" if a[0] == "rev-list" else "")
        monkeypatch.setattr(
            mod,
            "deferral_metrics",
            lambda repo: {"open": 0, "tracked": 0, "tracked_pct": None, "overdue": 0},
        )
        monkeypatch.setattr(
            mod,
            "override_metrics",
            lambda repo, since_iso: {
                "overrides_total": 0,
                "overrides_by_class": {},
                "overrides_self_authored": 0,
                "overrides_deferred_without_issue": 0,
                "owner_decisions": [],
            },
        )
        out, summary = tmp_path / "m.json", tmp_path / "m.md"
        argv = [
            "fix_loop_metrics.py",
            "--since",
            "7 days ago",
            "--since-iso",
            "2026-10-05T00:00:00Z",
            "--repo",
            "o/r",
            "--out",
            str(out),
            "--summary",
            str(summary),
            *extra,
        ]
        monkeypatch.setattr("sys.argv", argv)
        assert mod.main() == 0
        return (
            json.loads(out.read_text(encoding="utf-8")),
            summary.read_text(encoding="utf-8"),
            github,
        )

    def test_default_run_spends_no_census_call(
        self, mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        data, table, github = self.run_main(mod, monkeypatch, tmp_path)
        assert data["ci_waste"] == {"enabled": False}
        assert github.paths == []
        assert data["pr_counters"]["fix_prs"] == 2
        assert set(data["pr_counters"]) == {"merged_prs", "fix_prs", "size"}
        assert "off (opt-in" in table
        assert data["harvest"]["merged_fix_prs"] == 0  # the gate-run probe saw none

    def test_census_on_with_a_cap_and_agent_logins(
        self, mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        data, table, github = self.run_main(
            mod,
            monkeypatch,
            tmp_path,
            "--ci-waste",
            "true",
            "--ci-waste-max-calls",
            "7",
            "--agent-logins",
            "Mate-Bot",
        )
        assert len(github.paths) <= 7
        assert data["ci_waste"]["calls_cap_configured"] == 7
        assert data["ci_waste"]["agents"]["prs"] == 1
        assert "M1 Avoidable pushes per 100 merged PRs (agent logins)" in table

    def test_the_reserve_is_a_constant_not_a_flag(
        self, mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        with pytest.raises(SystemExit):
            self.run_main(mod, monkeypatch, tmp_path, "--ci-waste-reserve", "0")
        data, _, _ = self.run_main(mod, monkeypatch, tmp_path, "--ci-waste", "true")
        assert data["ci_waste"]["rate_limit_reserve"] == mod.CENSUS_DEFAULT_RESERVE

    def test_an_empty_agent_logins_value_is_accepted(
        self, mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        data, _, _ = self.run_main(
            mod, monkeypatch, tmp_path, "--ci-waste", "true", "--agent-logins", ""
        )
        assert data["ci_waste"]["agents"] is None

    def test_a_typo_cannot_switch_the_census_on(
        self, mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        with pytest.raises(SystemExit):
            self.run_main(mod, monkeypatch, tmp_path, "--ci-waste", "ture")


class TestCommandLine:
    def test_bool_is_strict(self, mod: Any) -> None:
        import argparse

        assert mod.cli_bool("true") is True
        assert mod.cli_bool(" False ") is False
        for bad in ("", "yes", "1", "tru"):
            with pytest.raises(argparse.ArgumentTypeError):
                mod.cli_bool(bad)

    def test_ints_have_a_floor(self, mod: Any) -> None:
        import argparse

        assert mod.cli_int(1)("1000") == 1000
        for bad in ("0", "-5", "ten", ""):
            with pytest.raises(argparse.ArgumentTypeError):
                mod.cli_int(1)(bad)
        assert mod.cli_int(0)("0") == 0

    def test_logins(self, mod: Any) -> None:
        import argparse

        assert mod.cli_logins("") == frozenset()
        assert mod.cli_logins("Mate-Bot, app[bot] ,,") == frozenset({"mate-bot", "app[bot]"})
        for bad in ("a b", "-x", "x;y", "$(id)"):
            with pytest.raises(argparse.ArgumentTypeError):
                mod.cli_logins(bad)


class TestWiring:
    """The census is opt-in and the workflow cannot turn it on by accident."""

    def metrics_step(self) -> dict:
        doc = yaml.safe_load(ANALYSIS.read_text(encoding="utf-8"))
        steps = doc["jobs"]["metrics"]["steps"]
        return next(s for s in steps if s.get("id") == "metrics")

    def test_the_census_defaults_to_off(self) -> None:
        env = self.metrics_step()["env"]
        assert env["CI_WASTE"].rstrip().endswith("|| 'false' }}")

    def test_workflow_defaults_equal_the_script_defaults(self, mod: Any) -> None:
        env = self.metrics_step()["env"]
        assert env["CI_WASTE_SAMPLE"].rstrip().endswith(f"|| '{mod.CENSUS_DEFAULT_SAMPLE}' }}}}")
        assert (
            env["CI_WASTE_MAX_CALLS"].rstrip().endswith(f"|| '{mod.CENSUS_DEFAULT_MAX_CALLS}' }}}}")
        )

    def test_inputs_reach_the_shell_only_through_env(self) -> None:
        run = self.metrics_step()["run"]
        assert "${{" not in run
        for flag, var in (
            ("--ci-waste", "CI_WASTE"),
            ("--ci-waste-sample", "CI_WASTE_SAMPLE"),
            ("--ci-waste-max-calls", "CI_WASTE_MAX_CALLS"),
            ("--agent-logins", "AGENT_LOGINS"),
        ):
            assert re.search(rf'{re.escape(flag)} "\${var}"', run), flag

    def test_dispatch_offers_the_census_inputs(self) -> None:
        doc = yaml.safe_load(ANALYSIS.read_text(encoding="utf-8"))
        inputs = (doc.get("on") or doc.get(True))["workflow_dispatch"]["inputs"]
        assert inputs["ci_waste"]["default"] is False
        assert {"ci_waste_sample", "ci_waste_max_calls"} <= set(inputs)

    def test_the_schedule_is_untouched(self) -> None:
        doc = yaml.safe_load(ANALYSIS.read_text(encoding="utf-8"))
        assert (doc.get("on") or doc.get(True))["schedule"] == [{"cron": "0 17 * * 1"}]

    def test_every_local_command_job_name_still_exists_in_a_workflow(self, mod: Any) -> None:
        """A renamed job silently shrinks M1; this is the pin the module comment promises."""
        text = "\n".join(p.read_text(encoding="utf-8") for p in sorted(WORKFLOWS.glob("*.yml")))
        names = {n.split(" (")[0] for n in mod.LOCAL_COMMAND_JOBS | mod.DERIVED_JOBS}
        missing = sorted(n for n in names if f"name: {n}" not in text)
        assert not missing, f"census job names no workflow declares: {missing}"
