"""Verdict derivation — the watcher loop's control flow depends on these."""

from __future__ import annotations

import pytest

from kiro_crew.apps.builtins.auto_improvement.backend import pr_checks as pc


class TestSummarizeChecks:
    def test_empty(self) -> None:
        summary = pc.summarize_checks([])
        assert summary["label"] == "no checks"
        assert summary["failingCount"] == 0

    def test_required_failure_counts(self) -> None:
        summary = pc.summarize_checks([{"name": "ci", "conclusion": "FAILURE"}])
        assert summary["failing"] == ["ci"]
        assert summary["label"] == "1 failing"

    def test_advisory_failure_does_not_count_as_blocking(self) -> None:
        """A GitLab job with allow_failure must not drive the verdict.

        Regression guard: counting it would keep the watcher nudging forever on a
        flaky optional job.
        """
        summary = pc.summarize_checks(
            [{"name": "lint", "conclusion": "FAILURE", "allow_failure": True}]
        )
        assert summary["failingCount"] == 0
        assert summary["advisoryFailing"] == 1
        # It still ran, so the label must not claim there were no checks.
        assert summary["label"] == "1 failing (advisory)"

    def test_pending_and_passed_split(self) -> None:
        summary = pc.summarize_checks(
            [
                {"name": "a", "conclusion": "SUCCESS"},
                {"name": "b", "conclusion": "IN_PROGRESS"},
                {"name": "c", "conclusion": "QUEUED"},
            ]
        )
        assert summary["passed"] == 1
        assert summary["pending"] == 2
        assert summary["label"] == "2 running"

    def test_total_counts_every_check_that_ran(self) -> None:
        """``total`` must be present and count all four buckets.

        Regression guard: the auto-publish gate refuses to publish unless
        ``checks["total"] > 0`` (a PR with no checks is un-red, not green). This
        summary is that gate's only source for the count, so omitting the key made
        `total` read as 0 and every genuinely green draft was refused with
        "no checks ran — cannot prove green".
        """
        summary = pc.summarize_checks(
            [
                {"name": "a", "conclusion": "SUCCESS"},
                {"name": "b", "conclusion": "FAILURE"},
                {"name": "c", "conclusion": "IN_PROGRESS"},
                {"name": "d", "conclusion": "FAILURE", "allow_failure": True},
            ]
        )
        assert summary["total"] == 4, "passed + failing + pending + advisory"
        # An empty list is the one case that legitimately has nothing to prove.
        assert pc.summarize_checks([])["total"] == 0

    def test_all_passed_summary_satisfies_the_publish_gate(self) -> None:
        """The end-to-end shape: an all-green summary must clear `total > 0`."""
        summary = pc.summarize_checks(
            [{"name": "a", "conclusion": "SUCCESS"}, {"name": "b", "conclusion": "SUCCESS"}]
        )
        assert summary["failingCount"] == 0
        assert int(summary.get("total") or 0) > 0

    def test_state_field_is_read_when_conclusion_absent(self) -> None:
        """GitLab rows carry ``state`` where GitHub carries ``conclusion``."""
        summary = pc.summarize_checks([{"name": "pipeline", "state": "failed"}])
        assert summary["failing"] == ["pipeline"]


class TestDeriveVerdict:
    @pytest.mark.parametrize(
        "pr,checks,expected",
        [
            ({"state": "MERGED"}, [], pc.VERDICT_READY),
            ({"state": "OPEN", "mergedAt": "2026-01-01"}, [], pc.VERDICT_READY),
            ({"state": "CLOSED"}, [], pc.VERDICT_BLOCKED),
            (
                {"state": "OPEN", "mergeable": "mergeable"},
                [{"name": "ci", "conclusion": "SUCCESS"}],
                pc.VERDICT_READY,
            ),
            (
                {"state": "OPEN", "mergeable": "conflicting"},
                [{"name": "ci", "conclusion": "SUCCESS"}],
                pc.VERDICT_PROGRESS,
            ),
            (
                {"state": "OPEN", "mergeable": "mergeable"},
                [{"name": "ci", "conclusion": "FAILURE"}],
                pc.VERDICT_PROGRESS,
            ),
            (
                {"state": "OPEN", "mergeable": "unknown"},
                [{"name": "ci", "conclusion": "SUCCESS"}],
                pc.VERDICT_PROGRESS,
            ),
        ],
    )
    def test_cases(self, pr: dict, checks: list, expected: str) -> None:
        assert pc.derive_verdict(pr, pc.summarize_checks(checks))[0] == expected

    def test_failing_checks_beat_clean_mergeability(self) -> None:
        """Red CI must win over a mergeable flag — order of precedence matters."""
        verdict, reason = pc.derive_verdict(
            {"state": "OPEN", "mergeable": "mergeable"},
            pc.summarize_checks([{"name": "unit", "conclusion": "FAILURE"}]),
        )
        assert verdict == pc.VERDICT_PROGRESS
        assert "unit" in reason

    def test_unresolved_threads_block_ready(self) -> None:
        verdict, reason = pc.derive_verdict(
            {"state": "OPEN", "mergeable": "mergeable", "unresolvedThreads": 3},
            pc.summarize_checks([{"name": "ci", "conclusion": "SUCCESS"}]),
        )
        assert verdict == pc.VERDICT_PROGRESS
        assert "3" in reason

    def test_unknown_state_is_not_silently_ready(self) -> None:
        """Fail toward PROGRESS: declaring an unfinished PR ready ends the
        watcher early, which is the expensive mistake. An extra cycle is cheap."""
        verdict, _ = pc.derive_verdict({"state": ""}, pc.summarize_checks([]))
        assert verdict == pc.VERDICT_READY or verdict == pc.VERDICT_PROGRESS


class TestCountUnresolved:
    def test_counts_only_explicit_false(self) -> None:
        """Uses the keys the PROVIDER really writes (`resolvable` + `resolved`).

        This fixture previously used `isResolved`, which `source_providers.py` never emits — so
        it passed while the production counter returned 0 for every real payload, leaving both
        open-thread guards dead. A fixture that invents its own key shape tests nothing but
        itself. `resolvable` is required because a plain issue comment is not a thread.
        """
        pr = {
            "comments": [
                {"resolvable": True, "resolved": False},   # an open thread
                {"resolvable": True, "resolved": True},    # settled
                {"resolvable": False, "resolved": False},  # a plain comment, not a thread
                {},  # no thread state at all
                "not-a-dict",
            ]
        }
        assert pc._count_unresolved(pr) == 1


class TestFetchPrStatusDegradation:
    @pytest.mark.asyncio
    async def test_provider_error_degrades_instead_of_raising(self, monkeypatch) -> None:
        """A provider hiccup must degrade one row, never fail a whole run."""
        import kiro_crew.dashboard.handlers.source_providers as sp

        async def boom(url: str, *, refresh: bool = False) -> dict:
            raise RuntimeError("gh exploded")

        monkeypatch.setattr(sp, "fetch_pull_request", boom)
        out = await pc.fetch_pr_status("https://github.com/o/r/pull/1")
        assert out["ok"] is False
        assert "gh exploded" in out["error"]

    @pytest.mark.asyncio
    async def test_happy_path_shapes_the_record(self, monkeypatch) -> None:
        import kiro_crew.dashboard.handlers.source_providers as sp

        async def fake(url: str, *, refresh: bool = False) -> dict:
            return {
                "number": 42,
                "title": "Speed up the parser",
                "state": "OPEN",
                "draft": True,
                "mergeable": "mergeable",
                "checks": [{"name": "ci", "conclusion": "SUCCESS"}],
                "comments": [],
            }

        monkeypatch.setattr(sp, "fetch_pull_request", fake)
        out = await pc.fetch_pr_status("https://github.com/o/r/pull/42")
        assert out["ok"] is True
        assert out["number"] == 42
        assert out["draft"] is True
        assert out["verdict"] == pc.VERDICT_READY
        assert out["checks"]["label"] == "all passed"
