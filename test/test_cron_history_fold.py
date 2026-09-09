"""Folding run records into the counts a cost audit reasons over.

These counts are what crosses the sandbox boundary in place of the run text they
came from, so two properties matter. A count cannot carry a credential, which is
why the fold happens gateway-side. And "the job said the same thing every run"
has to survive the numbers inside that sentence changing, or a disk check whose
percentages move every run reads as a job doing different work each time.
"""

from __future__ import annotations

from kiro_crew.mcp_cron import _NOOP_RE as NOOP_RE
from kiro_crew.mcp_cron import _fold_runs as fold_runs
from kiro_crew.mcp_cron import _normalize_summary as normalize_summary


def _runs(*summaries: str, failures: int = 0) -> list[dict[str, object]]:
    out: list[dict[str, object]] = [{"status": "success", "summary": s} for s in summaries]
    out += [{"status": "failure", "summary": "", "error": "boom"} for _ in range(failures)]
    return out


class TestNormalize:
    def test_digits_collapse_so_a_changing_count_still_compares_equal(self) -> None:
        assert normalize_summary("Disk healthy: tmp 1%, home 19%") == normalize_summary(
            "Disk healthy: tmp 4%, home 22%"
        )

    def test_case_and_whitespace_collapse(self) -> None:
        assert normalize_summary("  No   NEW items\n") == normalize_summary("no new items")

    def test_genuinely_different_text_stays_different(self) -> None:
        assert normalize_summary("Found a failure") != normalize_summary("Nothing to do")

    def test_empty_input(self) -> None:
        assert normalize_summary("") == ""


class TestNoopDetection:
    def test_the_real_no_op_phrasings_match(self) -> None:
        for summary in (
            "No new messages. Timestamp unchanged.",
            "Clean run. Disk healthy: tmp 1%, home 19%.",
            "SKIPPED: prior First Pass Triage comment detected",
            "Nothing to do.",
            "All clear.",
            "Already handled.",
            "0 new items",
            "Up to date.",
        ):
            assert NOOP_RE.search(summary), summary

    def test_a_summary_reporting_real_work_does_not_match(self) -> None:
        for summary in (
            "Found 3 failing checks and opened an issue.",
            "Merged 2 pull requests.",
            "Posted the digest.",
        ):
            assert not NOOP_RE.search(summary), summary


class TestFold:
    def test_counts(self) -> None:
        stats = fold_runs(_runs("Nothing to do.", "Nothing to do.", "Found 3 items.", failures=2))
        assert stats["runs"] == 3
        assert stats["failures"] == 2
        assert stats["noop_runs"] == 2
        assert stats["distinct_summaries"] == 2

    def test_same_every_run_sees_through_changing_numbers(self) -> None:
        stats = fold_runs(_runs("Clean. tmp 1%", "Clean. tmp 3%", "Clean. tmp 9%"))
        assert stats["same_every_run"] is True
        assert stats["distinct_summaries"] == 1

    def test_a_single_run_is_never_same_every_run(self) -> None:
        assert fold_runs(_runs("Clean."))["same_every_run"] is False

    def test_a_failed_run_contributes_only_to_failures(self) -> None:
        """A crashed run says nothing about whether the job had work to do."""
        stats = fold_runs(_runs(failures=3))
        assert stats["runs"] == 0
        assert stats["failures"] == 3
        assert stats["noop_runs"] == 0
        assert stats["distinct_summaries"] == 0

    def test_an_unknown_status_is_treated_as_a_run(self) -> None:
        """An absent status is the store's own default for a completed run."""
        stats = fold_runs([{"summary": "Nothing to do."}])
        assert stats["runs"] == 1
        assert stats["noop_runs"] == 1

    def test_empty_input(self) -> None:
        assert fold_runs([]) == {
            "runs": 0,
            "failures": 0,
            "distinct_summaries": 0,
            "noop_runs": 0,
            "same_every_run": False,
        }

    def test_a_missing_summary_is_not_an_error(self) -> None:
        stats = fold_runs([{"status": "success"}, {"status": "success", "summary": None}])
        assert stats["runs"] == 2
        assert stats["distinct_summaries"] == 1
