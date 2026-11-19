"""Contract tests for scripts/ci_test_selection_ledger.py, the selection miss ledger.

Pinned: a failing file in a skippable tier is a miss; a shared, rerun-green or
main-red failure is kept but not diff-caused; a second pass over the same runs
appends nothing; the proposed rate is the upper 95% Wilson bound.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts import ci_test_map_shadow as shadow
from scripts import ci_test_selection_ledger as ledger

VERDICT = {
    "pr": 7,
    "head": "a" * 40,
    "tiers": {"always": ["test/test_gate.py"], "low": ["test/test_a.py", "test/test_b.py"]},
    "skippable": ["low"],
}
LOG = "2026-11-18T00:00:00.0Z FAILED test/test_a.py::test_one - assert 1 == 2\n"


def _sample(
    run: int,
    pr: int,
    attempts: list[list[str]],
    date: str = "2026-11-18T00:00:00Z",
    green: bool = False,
) -> dict[str, Any]:
    return {
        "run": run,
        "attempt": len(attempts),
        "last_green": green,
        "pr": pr,
        "head": "a" * 40,
        "date": date,
        "attempts": attempts,
    }


def _failures(rows: list[dict[str, Any]], main_red: str = "") -> dict[str, dict[str, Any]]:
    return {r["file"]: r for r in ledger.mark_shared(rows, 3, main_red)}


def test_a_failing_file_in_a_skippable_tier_is_a_diff_caused_miss() -> None:
    failing = shadow.failing_test_files(shadow._nodeid_parser()(LOG))
    rows = ledger.run_rows(_sample(1, 7, [failing]), VERDICT)
    assert rows[0]["tier_counts"] == {"always": 1, "low": 2}
    row = _failures(rows)["test/test_a.py"]
    assert row["tier"] == "low" and row["miss"] and row["diff_caused"]


def test_an_always_run_failure_is_not_a_miss() -> None:
    rows = ledger.run_rows(_sample(1, 7, [["test/test_gate.py"]]), VERDICT)
    assert not _failures(rows)["test/test_gate.py"]["miss"]


def test_a_file_failing_on_another_pr_in_the_window_is_shared() -> None:
    rows = ledger.run_rows(_sample(1, 7, [["test/test_a.py"]]), VERDICT)
    rows += ledger.run_rows(_sample(2, 8, [["test/test_a.py"]]), VERDICT)
    far = _sample(3, 9, [["test/test_b.py"]], date="2026-11-01T00:00:00Z")
    rows += ledger.run_rows(far, VERDICT)
    rows += ledger.run_rows(_sample(4, 10, [["test/test_b.py"]]), VERDICT)
    marked = ledger.mark_shared(rows, 3)
    assert [r["shared"] for r in marked] == [True, True, False, False]
    assert [r["diff_caused"] for r in marked] == [False, False, True, True]


def test_a_rerun_green_or_main_red_failure_is_kept_but_not_diff_caused() -> None:
    rerun = ledger.run_rows(_sample(1, 7, [["test/test_a.py"], []], green=True), VERDICT)
    row = _failures(rerun)["test/test_a.py"]
    assert row["miss"] and row["rerun_green"] and not row["diff_caused"]
    # A rerun still red on another file clears neither: its job never went green.
    still_red = _sample(3, 9, [["test/test_a.py"], ["test/test_b.py"]])
    rows = _failures(ledger.run_rows(still_red, VERDICT))
    assert not rows["test/test_a.py"]["rerun_green"] and rows["test/test_a.py"]["diff_caused"]
    assert rows["test/test_b.py"]["diff_caused"]
    red = ledger.run_rows(_sample(2, 8, [["test/test_b.py"]]), VERDICT)
    row = _failures(red, "fails test/test_b.py")["test/test_b.py"]
    assert row["main_red"] and not row["diff_caused"]
    rates = ledger.refit(rerun + red, 3, "fails test/test_b.py")
    assert rates["tiers"]["low"]["failures"] == 0
    assert rates["tiers"]["low"]["not_diff_caused"] == 2


def _main(tmp_path: Path, monkeypatch, samples: list, main_red: list[str]) -> list[str]:
    """Run main() once per entry of `main_red`, the main-red text open at that pass."""
    monkeypatch.setattr(ledger, "fetch_samples", lambda limit, args, recorded: samples)
    texts = iter(main_red)
    monkeypatch.setattr(ledger, "main_red_text", lambda: next(texts))
    argv = ["--ledger", str(tmp_path / "ledger.jsonl"), "--rates-out", str(tmp_path / "r.json")]
    argv += ["--summary-out", str(tmp_path / "summary.md")]
    out = []
    for _ in main_red:
        assert ledger.main(argv) == 0
        out.append((tmp_path / "r.json").read_text(encoding="utf-8"))
    return out


def test_main_red_is_decided_from_the_issues_open_at_each_report(tmp_path, monkeypatch) -> None:
    samples = [(_sample(1, 7, [["test/test_a.py"]]), VERDICT)]
    passes = _main(tmp_path, monkeypatch, samples, ["", "test/test_a.py is red", ""])
    lows = [json.loads(text)["tiers"]["low"] for text in passes]
    # An issue filed after the run was recorded clears it; closing the issue restores it.
    assert [(low["failures"], low["not_diff_caused"]) for low in lows] == [(1, 0), (0, 1), (1, 0)]
    assert len((tmp_path / "ledger.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_a_failing_file_with_no_tier_withholds_the_rates(tmp_path, monkeypatch) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in ledger.run_rows(_sample(1, 7, [["test/test_new.py"]]), VERDICT)
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ledger, "main_red_text", lambda: "")
    rates = tmp_path / "rates.json"
    argv = ["--ledger", str(path), "--rates-out", str(rates), "--limit", "0"]
    argv += ["--summary-out", str(tmp_path / "summary.md")]
    assert ledger.main(argv) == 1
    assert not rates.exists()
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "Failing files the verdict gave no tier: 1." in summary
    assert "| 7 | aaaaaaaaaaaa | test/test_new.py |" in summary


def test_main_red_text_reads_the_open_main_red_issues(monkeypatch) -> None:
    issues = [{"title": "main red", "body": "test/test_a.py fails"}]
    monkeypatch.setattr(ledger.shadow, "_gh_json", lambda args, root: issues)
    assert "test/test_a.py" in ledger.main_red_text()


def test_a_second_pass_adds_no_duplicate_rows(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "ledger.jsonl"
    samples = [(_sample(1, 7, [["test/test_a.py"]]), VERDICT), (_sample(2, 8, [[]]), VERDICT)]
    asked: list[dict[int, int]] = []

    def fetch(limit: int, args: Any, recorded: dict[int, int]) -> list:
        asked.append(recorded)
        return samples

    monkeypatch.setattr(ledger, "fetch_samples", fetch)
    monkeypatch.setattr(ledger, "main_red_text", lambda: "")
    argv = ["--ledger", str(path), "--rates-out", str(tmp_path / "rates.json")]
    argv += ["--summary-out", str(tmp_path / "summary.md")]
    assert ledger.main(argv) == 0
    first = path.read_text(encoding="utf-8")
    assert len(first.splitlines()) == 3
    assert ledger.main(argv) == 0
    assert path.read_text(encoding="utf-8") == first
    assert asked[1] == {1: 1, 2: 1}
    rates = json.loads((tmp_path / "rates.json").read_text(encoding="utf-8"))
    assert rates["tiers"]["low"]["trials"] == 4 and rates["tiers"]["low"]["failures"] == 1
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "Runs: 2. Days of data: 0.0." in summary
    assert "| 7 | aaaaaaaaaaaa | test/test_a.py | low |  |" in summary

    # A later green rerun of run 1 supersedes its first observation, trials counted once.
    samples[:] = [(_sample(1, 7, [["test/test_a.py"], []], green=True), VERDICT)]
    assert ledger.main(argv) == 0
    assert len(path.read_text(encoding="utf-8").splitlines()) == 5
    assert asked[2] == {1: 1, 2: 1}
    low = json.loads((tmp_path / "rates.json").read_text(encoding="utf-8"))["tiers"]["low"]
    assert (low["trials"], low["failures"], low["not_diff_caused"]) == (4, 0, 1)
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "Runs: 2." in summary and "| test/test_a.py | low | rerun_green |" in summary


def test_the_rate_is_the_upper_wilson_bound() -> None:
    assert ledger.wilson_upper(0, 0) == 1.0
    assert ledger.wilson_upper(0, 100) == pytest.approx(0.0370, abs=1e-4)
    assert ledger.wilson_upper(5, 100) == pytest.approx(0.1118, abs=1e-4)


def test_fetch_reads_each_attempt_and_skips_runs_without_a_verdict(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "11.json").write_text(json.dumps(VERDICT), encoding="utf-8")
    runs = [
        {"id": 10, "run_attempt": 1, "head_sha": "b" * 40, "created_at": "2026-11-18T00:00:00Z"},
        {"id": 11, "run_attempt": 2, "head_sha": "a" * 40, "created_at": "2026-11-18T00:00:00Z"},
        {"id": 12, "run_attempt": 1, "head_sha": "c" * 40, "created_at": "2026-11-18T00:00:00Z"},
    ]
    jobs = [{"name": "Backend Tests (3.12, 1)", "conclusion": "success"}]

    def gh_json(args: list[str], root: Path) -> Any:
        return jobs if "/attempts/2/jobs" in args[1] else runs

    # The ledger imports the shadow script by its top-level name: patch that module.
    monkeypatch.setattr(ledger.shadow, "_gh_json", gh_json)
    rows = {1: {"failing": ["test/test_a.py"]}, 2: {}}
    monkeypatch.setattr(
        ledger.shadow, "_red_row", lambda run, root, parse: rows[run["run_attempt"]]
    )
    args = ledger.argparse.Namespace(verdict_dir=str(tmp_path))
    (sample, verdict), *rest = ledger.fetch_samples(5, args, recorded={12: 1, 11: 1})
    assert not rest and verdict == VERDICT
    assert sample == _sample(11, 7, [["test/test_a.py"], []], green=True)
    # Only a newer attempt than the ledger holds is read again.
    assert ledger.fetch_samples(5, args, recorded={11: 2, 12: 1}) == []


def test_a_rerun_is_green_only_when_every_linux_backend_job_succeeded(monkeypatch) -> None:
    def jobs_of(*jobs: tuple[str, str]) -> None:
        listed = [{"name": name, "conclusion": conclusion} for name, conclusion in jobs]
        monkeypatch.setattr(ledger.shadow, "_gh_json", lambda args, root: listed)

    jobs_of(("Backend Tests (3.12, 1)", "success"), ("Backend Tests (3.12, 2)", "failure"))
    assert not ledger._attempt_green(1, 2)
    jobs_of(("Backend Tests (Windows) (1)", "success"))
    assert not ledger._attempt_green(1, 2)

    def unreadable(args: list[str], root: Path) -> Any:
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(ledger.shadow, "_gh_json", unreadable)
    assert not ledger._attempt_green(1, 2)


def test_a_run_without_the_artifact_has_no_verdict(monkeypatch) -> None:
    def no_artifact(argv: list[str], root: Path) -> str:
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(ledger.shadow, "_run", no_artifact)
    args = ledger.argparse.Namespace(verdict_dir=None, artifact="v", verdict_file="v.json")
    assert ledger._verdict(1, args) is None
