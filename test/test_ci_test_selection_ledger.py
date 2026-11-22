"""Contract tests for scripts/ci_test_selection_ledger.py, the test-selection miss ledger.

Every test reads GitHub through one in-memory fake of the two `gh` entry points
the script uses, so nothing here starts a process or touches the network.

Pinned:

* JOIN -- a failing file the verdict would skip is a miss; a failure that went
  green on a rerun, that the watcher's main-red issue names, or that another PR
  shares is kept and reported apart.
* OBSERVE -- only a run whose every Linux backend shard ran the full suite to a
  pass or a fail adds trials; any other run is recorded as excluded.
* LEDGER -- a second pass adds no row, a rerun supersedes, an unreadable run is
  tried again, and a listing longer than one page is read to its end.
* DATA -- a verdict or ledger line of the wrong shape is refused, every row the
  script writes reads back, and no cell of the summary can carry markup.
* TRUST -- no row of a fork run changes a number the script proposes or how a
  same-repository failure is classified.
* WINDOWS -- a file failing on Windows and not on Linux in one run is a
  Windows-only failure, a miss outside `windows.run` under `select`; Windows
  trials, labels, sources and the auto-off line are reported apart from Linux.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts import ci_test_selection_ledger as ledger

# The script's own binding of the shadow module: the one its calls go through.
shadow = ledger.shadow

ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-11-18T00:00:00Z"
LATER = "2026-11-18T01:00:00Z"
VERDICT: dict[str, Any] = {
    "schema": 1,
    "base": "b" * 40,
    "head": "a" * 40,
    "bundle_commit": None,
    "verdict": "select",
    "reason": None,
    "tiers": {
        "A": ["test/test_gate.py"],
        "B": ["test/test_b.py"],
        "G": ["test/test_a.py", "test/test_c.py"],
    },
    "would_skip": ["test/test_a.py", "test/test_c.py"],
    "would_run_share": 0.5,
}
SKIP: dict[str, Any] = {
    "kind": "skip",
    "run": 1,
    "attempt": 1,
    "source": "1:pr-7",
    "fork": False,
    "pr": 7,
    "head": "a" * 40,
    "date": DAY,
    "reason": "x",
}
OLD_RUN = {**{k: v for k, v in SKIP.items() if k not in ("source", "reason")}, "kind": "run"}
FULL: dict[str, Any] = {
    **VERDICT,
    "verdict": "full",
    "reason": "no bundle",
    "tiers": {tier: [] for tier in "ABG"},
    "would_skip": [],
}


def job(job_id: int, shard: int, conclusion: str = "success", full: bool = True) -> dict[str, Any]:
    step = {
        "name": f"{ledger.FULL_SUITE_STEP} (shard {shard}/8)",
        "conclusion": "success" if full else "skipped",
    }
    return {
        "id": job_id,
        "name": f"Backend Tests (3.12, {shard})",
        "conclusion": conclusion,
        "steps": [{"name": "Select test scope", "conclusion": "success"}, step],
    }


class FakeGh:
    """GitHub as the script reads it: runs, each attempt's jobs, job logs, artifacts, issues."""

    def __init__(self, verdicts: Path) -> None:
        self.verdicts = verdicts
        self.runs: list[dict[str, Any]] = []
        self.jobs: dict[tuple[int, int], list[dict[str, Any]]] = {}
        self.logs: dict[int, list[str]] = {}
        self.messages: dict[str, str] = {}
        self.artifacts: dict[int, list[str]] = {}
        self.issues: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.page_size = 100
        self.overlap = 0

    def add(
        self,
        run_id: int,
        pr: int | None,
        attempts: list[list[dict[str, Any]]],
        verdict: dict[str, Any] | None = VERDICT,
        date: str = DAY,
        branch: str | None = None,
        fork: bool = False,
    ) -> dict[str, Any]:
        run = {
            "id": run_id,
            "run_attempt": len(attempts),
            "head_sha": "a" * 40,
            "created_at": date,
            "pull_requests": [{"number": pr}] if pr else [],
            "repository": {"id": 1},
            "head_repository": {"id": 2 if fork else 1},
            "head_branch": branch or f"pr-{pr}",
        }
        self.runs.insert(0, run)
        self.set_attempts(run_id, attempts)
        if verdict is not None:
            (self.verdicts / f"{run_id}.json").write_text(json.dumps(verdict), encoding="utf-8")
        return run

    def set_attempts(self, run_id: int, attempts: list[list[dict[str, Any]]]) -> None:
        for number, jobs in enumerate(attempts, 1):
            self.jobs[(run_id, number)] = jobs
        for run in self.runs:
            if run["id"] == run_id:
                run["run_attempt"] = len(attempts)

    def _page(self, url: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        found = re.search(r"[?&]page=(\d+)", url)
        start = (int(found.group(1)) - 1) * (self.page_size - self.overlap) if found else 0
        return items[start : start + self.page_size]

    def gh_json(self, args: list[str], root: Path) -> Any:
        url = args[1]
        self.calls.append(url)
        if "/workflows/ci.yml/runs?" in url:
            event = re.search(r"[?&]event=(\w+)", url)
            listed = [r for r in self.runs if event and r.get("event", "pull_request") == event[1]]
            return self._page(url, listed)
        attempt = re.search(r"/runs/(\d+)/attempts/(\d+)/jobs\?", url)
        if attempt:
            key = (int(attempt.group(1)), int(attempt.group(2)))
            if key not in self.jobs:
                raise subprocess.CalledProcessError(1, args)
            other = [{"id": 900 + n, "name": f"Lint {n}", "conclusion": "success"} for n in (1, 2)]
            jobs = other + self.jobs[key]
            return {"total_count": len(jobs), "jobs": self._page(url, jobs)}
        artifact = re.search(r"/runs/(\d+)/artifacts\?name=", url)
        if artifact:
            names = self.artifacts.get(int(artifact.group(1)), [])
            return [{"name": "test-selection-verdict", "expired": False}] if names else []
        if "/issues?" in url:
            return self.issues
        raise AssertionError(f"unexpected gh call: {url}")

    def run(self, argv: list[str], root: Path) -> str:
        self.calls.append(" ".join(argv[1:4]))
        if argv[1:3] == ["run", "download"]:
            target = Path(argv[argv.index("-D") + 1])
            for number, text in enumerate(self.artifacts[int(argv[3])]):
                (target / f"verdict-{number}.json").write_text(text, encoding="utf-8")
            return ""
        job_id = int(argv[-1].rsplit("/", 2)[1])
        if job_id not in self.logs:
            raise subprocess.CalledProcessError(1, argv)
        return "".join(
            f"2026-11-18T00:00:00.0Z FAILED {name}::test_one - "
            f"{self.messages.get(name, 'assert 1 == 2')}\n"
            for name in self.logs[job_id]
        )


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeGh:
    verdicts = tmp_path / "verdicts"
    verdicts.mkdir()
    fake = FakeGh(verdicts)
    monkeypatch.setattr(shadow, "_gh_json", fake.gh_json)
    monkeypatch.setattr(shadow, "_run", fake.run)
    return fake


def run_main(tmp_path: Path, *extra: str, offline: bool = True) -> int:
    argv = ["--ledger", str(tmp_path / "ledger.jsonl"), "--rates-out", str(tmp_path / "rates.json")]
    argv += ["--summary-out", str(tmp_path / "summary.md")]
    if offline:
        argv += ["--verdict-dir", str(tmp_path / "verdicts")]
    return ledger.main([*argv, *extra])


def rows(tmp_path: Path, kind: str | None = None) -> list[dict[str, Any]]:
    path = tmp_path / "ledger.jsonl"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    parsed = [json.loads(line) for line in text.splitlines()]
    return [row for row in parsed if kind is None or row["kind"] == kind]


def rates(tmp_path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((tmp_path / "rates.json").read_text(encoding="utf-8"))
    return loaded


def summary(tmp_path: Path) -> str:
    return (tmp_path / "summary.md").read_text(encoding="utf-8")


def failed_run(gh: FakeGh, run_id: int, pr: int, files: list[str], **kwargs: Any) -> None:
    """One full-suite run whose shard 2 failed `files` and whose shard 1 passed."""
    gh.add(run_id, pr, [[job(run_id * 10 + 1, 1), job(run_id * 10 + 2, 2, "failure")]], **kwargs)
    gh.logs[run_id * 10 + 2] = files


# -- JOIN ---------------------------------------------------------------------


def test_a_failing_file_the_verdict_would_skip_is_a_diff_caused_miss(
    gh: FakeGh, tmp_path: Path
) -> None:
    failed_run(gh, 1, 7, ["test/test_a.py", "test/test_gate.py"])
    gh.add(2, 8, [[job(21, 1), job(22, 2)]])

    assert run_main(tmp_path) == 0

    failures = {row["file"]: row for row in rows(tmp_path, "failure")}
    assert failures["test/test_a.py"]["tier"] == "G" and failures["test/test_a.py"]["miss"]
    assert (
        failures["test/test_gate.py"]["tier"] == "A" and not failures["test/test_gate.py"]["miss"]
    )
    assert [row["tiers"] for row in rows(tmp_path, "run")] == [{"A": 1, "B": 1, "G": 2}] * 2
    fitted = rates(tmp_path)
    assert fitted["schema"] == 1 and sorted(fitted["tiers"]) == ["A", "B", "G"]
    # Two runs hold tier G's two files each: one diff-caused failure in four trials.
    assert fitted["tiers"]["G"]["rate"] == 0.25
    assert fitted["tiers"]["G"]["upper95"] == pytest.approx(ledger.wilson_upper(1, 4), abs=1e-8)
    assert fitted["tiers"]["B"]["rate"] == 0.0 and set(fitted["tiers"]["B"]) == {"rate", "upper95"}
    text = summary(tmp_path)
    assert "Misses caused by the diff: 1." in text and "Misses not caused by it: 0." in text
    assert "| #7 | aaaaaaaaaaaa | 1 | test/test_a.py | G |" in text
    assert "| G | 4 | 1 | 0 |" in text and "Days of data: 0.0." in text


def test_a_file_failing_on_another_pr_in_the_window_is_shared_and_reported_apart(
    gh: FakeGh, tmp_path: Path
) -> None:
    failed_run(gh, 1, 7, ["test/test_a.py"])
    failed_run(gh, 2, 8, ["test/test_a.py"], date="2026-11-19T00:00:00Z")
    # The same PR again, and another PR outside the three-day window: neither shares.
    failed_run(gh, 3, 9, ["test/test_c.py"])
    failed_run(gh, 4, 9, ["test/test_c.py"], date="2026-11-18T06:00:00Z")
    failed_run(gh, 5, 10, ["test/test_c.py"], date="2026-11-25T00:00:00Z")

    assert run_main(tmp_path) == 0

    marked = ledger.classify(ledger.current(rows(tmp_path), None), 3.0)
    assert {(r["run"], r["shared"], r["diff_caused"]) for r in marked} == {
        (1, True, False),
        (2, True, False),
        (3, False, True),
        (4, False, True),
        (5, False, True),
    }
    assert rates(tmp_path)["tiers"]["G"]["rate"] == 0.3
    text = summary(tmp_path)
    assert "Misses caused by the diff: 3." in text and "Misses not caused by it: 2." in text
    assert "| #8 | aaaaaaaaaaaa | 2 | test/test_a.py | G | shared |" in text
    assert "Days of data: 7.0." in text


def test_a_failure_under_a_full_verdict_fits_nothing_but_still_marks_a_file_shared(
    gh: FakeGh, tmp_path: Path
) -> None:
    failed_run(gh, 1, 7, ["test/test_a.py"])
    failed_run(gh, 2, 8, ["test/test_a.py"], verdict=FULL)

    assert run_main(tmp_path) == 0

    assert rates(tmp_path)["tiers"]["G"]["rate"] == 0.0
    text = summary(tmp_path)
    assert "Runs fitted: 1 with a `select` verdict; 1 more had a `full` verdict." in text
    assert "| G | 2 | 0 | 1 |" in text


def test_a_shard_that_went_green_on_a_rerun_clears_its_failure_and_a_red_one_does_not(
    gh: FakeGh, tmp_path: Path
) -> None:
    first = [job(11, 1, "failure"), job(12, 2, "failure")]
    second = [job(13, 1, "success"), job(14, 2, "failure")]
    gh.add(1, 7, [first, second])
    gh.logs.update({11: ["test/test_a.py"], 12: ["test/test_c.py"], 14: ["test/test_b.py"]})

    assert run_main(tmp_path) == 0

    green = {row["file"]: row["rerun_green"] for row in rows(tmp_path, "failure")}
    # Shard 2 is still red, on another file: that clears neither of its files.
    assert green == {"test/test_a.py": True, "test/test_b.py": False, "test/test_c.py": False}
    text = summary(tmp_path)
    assert "| #7 | aaaaaaaaaaaa | 1 | test/test_a.py | G | rerun_green |" in text
    assert "Misses caused by the diff: 1." in text
    assert rates(tmp_path)["tiers"]["G"]["rate"] == 0.5


def test_a_rerun_that_lists_only_the_rerun_shard_keeps_the_other_shards_first_result(
    gh: FakeGh, tmp_path: Path
) -> None:
    gh.add(1, 7, [[job(11, 1), job(12, 2, "failure")], [job(13, 2)]])
    gh.logs[12] = ["test/test_a.py"]

    assert run_main(tmp_path) == 0

    assert [row["rerun_green"] for row in rows(tmp_path, "failure")] == [True]
    assert rows(tmp_path, "skip") == []


def _watcher_issue(
    number: int, nodeid: str, login: str = "github-actions[bot]", first_run: int = 1
) -> dict[str, Any]:
    watch = shadow.watch_module()
    url = "https://github.com/kirodotdev/KiroCrew/actions/runs/5"
    state = {"first_sha": "abc123", "first_run_id": first_run, "first_url": url, "streak": 1}
    state.update({"last_sha": "abc123", "last_run_id": 5, "last_url": url})
    state["failures"] = {nodeid: "assert 1 == 2"}
    return {
        "number": number,
        "state": "open",
        "title": watch.TITLE_PREFIX + "1 test",
        "body": f"text\n{watch.STATE_OPEN}{watch.dump_state(state)}{watch.STATE_CLOSE}\n",
        "user": {"login": login},
        "labels": [{"name": "main-red"}],
    }


def test_main_red_is_what_the_watchers_own_issue_names_and_nothing_a_person_wrote(
    gh: FakeGh, tmp_path: Path
) -> None:
    failed_run(gh, 1, 7, ["test/test_a.py", "test/test_c.py"])
    gh.issues = [
        _watcher_issue(40, "test/test_a.py::test_one"),
        # A person's issue with the label, a well-formed record and the other file's name.
        _watcher_issue(41, "test/test_c.py::test_one", login="someone"),
    ]

    assert run_main(tmp_path) == 0

    red = {row["file"]: row["main_red"] for row in rows(tmp_path, "failure")}
    assert red == {"test/test_a.py": True, "test/test_c.py": False}
    assert "| #7 | aaaaaaaaaaaa | 1 | test/test_a.py | G | main_red |" in summary(tmp_path)
    # Recorded with the row: a pass after the issue closes reports the same thing.
    gh.issues = []
    assert run_main(tmp_path) == 0
    assert "test/test_a.py | G | main_red |" in summary(tmp_path)


def test_main_red_does_not_reach_back_before_main_went_red(gh: FakeGh, tmp_path: Path) -> None:
    failed_run(gh, 4, 7, ["test/test_a.py"])
    failed_run(gh, 6, 7, ["test/test_a.py"], date=LATER)
    gh.issues = [_watcher_issue(40, "test/test_a.py::test_one", first_run=5)]

    assert run_main(tmp_path) == 0

    assert {row["run"]: row["main_red"] for row in rows(tmp_path, "failure")} == {4: False, 6: True}


def test_a_pr_that_merged_between_two_passes_is_still_one_pr(gh: FakeGh, tmp_path: Path) -> None:
    failed_run(gh, 1, 7, ["test/test_a.py"])
    assert run_main(tmp_path) == 0
    # A merged PR: GitHub lists none on the run of its later push.
    failed_run(gh, 2, None, ["test/test_a.py"], branch="pr-7", date=LATER)

    assert run_main(tmp_path) == 0

    assert [(row["pr"], row["source"]) for row in rows(tmp_path, "failure")] == [
        (7, "1:pr-7"),
        (None, "1:pr-7"),
    ]
    marked = ledger.classify(ledger.current(rows(tmp_path), None), 3.0)
    assert [(row["shared"], row["diff_caused"]) for row in marked] == [(False, True)] * 2
    assert rates(tmp_path)["tiers"]["G"]["rate"] == 0.5
    text = summary(tmp_path)
    assert "Misses caused by the diff: 2." in text and "Misses not caused by it: 0." in text
    assert "| #7 | aaaaaaaaaaaa | 1 |" in text and "| 1:pr-7 | aaaaaaaaaaaa | 2 |" in text


def test_a_run_whose_head_repository_is_gone_is_excluded(gh: FakeGh, tmp_path: Path) -> None:
    failed_run(gh, 1, None, ["test/test_a.py"], branch="topic", fork=True)
    gh.runs[0]["head_repository"] = None

    assert run_main(tmp_path) == 0

    assert [(row["kind"], row["reason"]) for row in rows(tmp_path)] == [("skip", ledger.NO_SOURCE)]
    assert [call for call in gh.calls if "/jobs" in call] == []


def test_a_failing_file_with_no_tier_withholds_the_rates(gh: FakeGh, tmp_path: Path) -> None:
    failed_run(gh, 1, 7, ["test/test_new.py"])

    assert run_main(tmp_path) == ledger.EXIT_WITHHELD == 3

    assert not (tmp_path / "rates.json").exists()
    text = summary(tmp_path)
    assert "Failing files a `select` verdict gave no tier: 1." in text
    assert "| #7 | aaaaaaaaaaaa | 1 | test/test_new.py |" in text
    # `--since` reports from a later day on, which leaves that run out.
    assert run_main(tmp_path, "--limit", "0", "--since", "2026-11-19") == 0
    assert rates(tmp_path) == {"schema": 1, "tiers": {}}


# -- TRUST --------------------------------------------------------------------


def _same_repo_view(tmp_path: Path) -> tuple[Any, Any, Any]:
    marked = ledger.classify(ledger.current(rows(tmp_path), None), 3.0)
    own = [row for row in marked if not row["fork"]]
    text = summary(tmp_path)
    table = [line for line in text.splitlines() if line.startswith(("| A ", "| B ", "| G "))]
    return rates(tmp_path), own, (table, text.split("Fork runs")[0])


def test_no_fork_row_changes_a_fitted_number_or_a_same_repository_classification(
    gh: FakeGh, tmp_path: Path
) -> None:
    failed_run(gh, 1, 7, ["test/test_a.py"])
    gh.add(2, 8, [[job(21, 1), job(22, 2)]], date=LATER)
    assert run_main(tmp_path) == 0
    before = _same_repo_view(tmp_path)
    assert before[0]["tiers"]["G"]["rate"] == 0.25

    # A fork fails the same file inside the window: it would make PR 7's miss "shared".
    failed_run(gh, 3, None, ["test/test_a.py"], branch="topic", fork=True, date=LATER)
    # A fork's verdict lists a thousand files in tier G: they would be trials.
    padded = {**VERDICT, "tiers": {"G": [f"test/test_pad_{n}.py" for n in range(1000)]}}
    gh.add(4, None, [[job(41, 1)]], verdict=padded, branch="pad", fork=True)
    # A fork's verdict gives its failing file no tier: it would withhold the rates.
    bare = {**VERDICT, "tiers": {"A": []}, "would_skip": []}
    failed_run(gh, 5, None, ["test/test_c.py"], verdict=bare, branch="bare", fork=True)
    # A fork run a week on: it would stretch the days of data.
    gh.add(6, None, [[job(61, 1)]], branch="late", fork=True, date="2026-11-25T00:00:00Z")

    assert run_main(tmp_path) == 0

    assert _same_repo_view(tmp_path) == before
    assert len(rows(tmp_path, "run")) == 6
    text = summary(tmp_path)
    assert "Fork runs, reported but not fitted: 4." in text
    assert "Misses on fork runs, reported and not fitted: 1." in text
    assert "| 2:topic | aaaaaaaaaaaa | 3 | test/test_a.py | G | shared |" in text


def test_a_same_repository_failure_is_evidence_against_a_fork_row_only_one_way(
    gh: FakeGh, tmp_path: Path
) -> None:
    failed_run(gh, 1, 7, ["test/test_a.py"])
    failed_run(gh, 2, None, ["test/test_a.py"], branch="topic", fork=True, date=LATER)

    assert run_main(tmp_path) == 0

    marked = ledger.classify(ledger.current(rows(tmp_path), None), 3.0)
    assert {row["run"]: row["shared"] for row in marked} == {1: False, 2: True}


# -- OBSERVE ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("jobs", "logs", "reason"),
    [
        ([job(11, 1, full=False), job(12, 2, full=False)], {}, "did not run the full suite"),
        ([job(11, 1), job(12, 2, "cancelled")], {}, "neither passed nor failed"),
        ([job(11, 1), job(12, 2, "skipped", full=False)], {}, "neither passed nor failed"),
        ([job(11, 1), job(12, 2, "failure")], {12: []}, "names no failing test"),
        ([], {}, "no Linux backend shard ran"),
    ],
)
def test_a_run_that_does_not_prove_what_it_ran_is_excluded_and_adds_no_trial(
    gh: FakeGh,
    tmp_path: Path,
    jobs: list[dict[str, Any]],
    logs: dict[int, list[str]],
    reason: str,
) -> None:
    gh.add(1, 7, [jobs])
    gh.logs.update(logs)

    assert run_main(tmp_path) == 0

    assert [reason in row["reason"] for row in rows(tmp_path, "skip")] == [True]
    assert rows(tmp_path, "run") == rows(tmp_path, "windows_run") == []
    assert rates(tmp_path) == {"schema": 1, "tiers": {}}
    assert "Runs excluded: 1; " in summary(tmp_path) and reason in summary(tmp_path)


def test_a_tier_with_no_trial_is_never_proposed_as_skippable() -> None:
    run = {"kind": "run", "run": 1, "fork": False, "verdict": "select", "tiers": {"G": 0}}
    assert ledger.fit([run], [])["G"]["rate"] == 1.0
    assert ledger.fit([run], [])["G"]["upper95"] == 1.0


def test_the_upper_bound_is_wilsons_and_is_rounded_up() -> None:
    assert ledger.wilson_upper(0, 0) == 1.0
    assert ledger.wilson_upper(0, 100) == pytest.approx(0.0369935, abs=1e-6)
    assert ledger.wilson_upper(5, 5) == 1.0
    run = {"kind": "run", "run": 1, "fork": False, "verdict": "select", "tiers": {"G": 100}}
    bound = ledger.fit([run], [])["G"]["upper95"]
    assert bound >= ledger.wilson_upper(0, 100) and bound - ledger.wilson_upper(0, 100) < 1e-8


@pytest.mark.parametrize(("failures", "trials"), [(1, 0), (1, 1), (2, 1), (3, 2), (50, 7)])
def test_a_count_the_interval_is_not_defined_on_is_never_skippable(
    failures: int, trials: int
) -> None:
    assert ledger.wilson_upper(failures, trials) == 1.0
    run = {"kind": "run", "run": 1, "fork": False, "verdict": "select", "tiers": {"G": trials}}
    failure = {"tier": "G", "fork": False, "diff_caused": True}

    fitted = ledger.fit([run], [failure] * failures)["G"]

    assert (fitted["rate"], fitted["upper95"]) == (1.0, 1.0)


def test_a_ledger_with_more_failures_than_trials_still_reports(gh: FakeGh, tmp_path: Path) -> None:
    held = {**OLD_RUN, "source": "1:pr-7", "verdict": "select", "tiers": {"G": 1}}
    failure = {k: v for k, v in SKIP.items() if k != "reason"} | {
        "kind": "failure",
        "tier": "G",
        "miss": True,
        "rerun_green": False,
        "main_red": False,
    }
    lines = [held, {**failure, "file": "test/test_a.py"}, {**failure, "file": "test/test_c.py"}]
    assert all(ledger.valid_row(row) for row in lines)
    (tmp_path / "ledger.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in lines), encoding="utf-8"
    )
    gh.add(2, 8, [[job(21, 1)]], verdict={**VERDICT, "tiers": {"B": ["test/test_b.py"]}})

    # A pass that appends, then reports over those rows: it ends, and proposes nothing skippable.
    assert run_main(tmp_path, "--no-windows") == 0

    assert len(rows(tmp_path)) == 4
    assert rates(tmp_path)["tiers"]["G"] == {"rate": 1.0, "upper95": 1.0}
    assert "| G | 1 | 2 | 0 | 1.00000000 | 1.00000000 |" in summary(tmp_path)


def test_the_proposed_bound_never_rounds_down() -> None:
    run = {"kind": "run", "run": 1, "fork": False, "verdict": "select", "tiers": {"G": 47328}}
    failure = {"tier": "G", "fork": False, "diff_caused": True}
    exact = ledger.wilson_upper(8, 47328)

    bound = ledger.fit([run], [failure] * 8)["G"]["upper95"]

    # Rounding to the nearest would give 0.00033354, below the bound itself.
    assert round(exact, 8) < exact
    assert bound == 0.00033355


def test_the_full_suite_step_is_the_one_ci_gates_on_scope() -> None:
    lines = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8").splitlines()
    named = [at for at, line in enumerate(lines) if f"- name: {ledger.FULL_SUITE_STEP}" in line]
    assert named, "ci.yml no longer has the step the ledger reads a full-suite run from"
    for at in named:
        assert lines[at + 1].strip() == "if: ${{ steps.scope.outputs.reduced == 'false' }}"


# -- LEDGER -------------------------------------------------------------------


def test_a_second_pass_adds_no_row_and_a_later_rerun_supersedes(gh: FakeGh, tmp_path: Path) -> None:
    failed_run(gh, 1, 7, ["test/test_a.py"])
    gh.add(2, 8, [[job(21, 1), job(22, 2)]])
    assert run_main(tmp_path) == 0
    first = rows(tmp_path)
    before = len(gh.calls)

    assert run_main(tmp_path) == 0

    assert rows(tmp_path) == first
    # The second pass lists the runs and the issue, and reads no job or log again.
    assert not [call for call in gh.calls[before:] if "/jobs" in call or "logs" in call]

    gh.set_attempts(1, [[job(11, 1), job(12, 2, "failure")], [job(11, 1), job(13, 2)]])
    assert run_main(tmp_path) == 0

    assert rows(tmp_path)[: len(first)] == first
    assert [(r["run"], r["attempt"]) for r in rows(tmp_path, "run")] == [(2, 1), (1, 1), (1, 2)]
    # Run 1 counts once, as its second attempt: four trials in tier G, none diff-caused.
    assert "| G | 4 | 0 | 1 |" in summary(tmp_path)
    assert rates(tmp_path)["tiers"]["G"]["rate"] == 0.0


def test_an_unreadable_run_records_nothing_and_a_later_pass_reads_it(
    gh: FakeGh, tmp_path: Path
) -> None:
    gh.add(1, 7, [[job(11, 1), job(12, 2, "failure")]])
    gh.add(2, 8, [[job(21, 1)]])
    del gh.jobs[(2, 1)]

    assert run_main(tmp_path) == 0
    assert rows(tmp_path) == []

    gh.logs[12] = ["test/test_a.py"]
    gh.jobs[(2, 1)] = [job(21, 1)]
    assert run_main(tmp_path) == 0
    assert sorted(row["run"] for row in rows(tmp_path, "run")) == [1, 2]


def test_passes_read_past_the_first_page_until_every_run_is_recorded(
    gh: FakeGh, tmp_path: Path
) -> None:
    gh.page_size = 2
    for run_id in range(1, 6):
        gh.add(run_id, run_id, [[job(run_id * 10, 1)]])

    assert run_main(tmp_path, "--limit", "2") == 0
    assert [row["run"] for row in rows(tmp_path, "run")] == [5, 4]
    assert run_main(tmp_path, "--limit", "2") == 0
    assert run_main(tmp_path, "--limit", "2") == 0

    assert [row["run"] for row in rows(tmp_path, "run")] == [5, 4, 3, 2, 1]
    assert rates(tmp_path)["tiers"]["G"]["rate"] == 0.0


def test_a_run_listed_on_two_pages_is_recorded_once(gh: FakeGh, tmp_path: Path) -> None:
    gh.page_size, gh.overlap = 2, 1
    for run_id in range(1, 5):
        gh.add(run_id, run_id, [[job(run_id * 10, 1)]])

    assert run_main(tmp_path) == 0

    assert [row["run"] for row in rows(tmp_path, "run")] == [4, 3, 2, 1]
    assert len([call for call in gh.calls if "/attempts/1/jobs?per_page=100&page=1" in call]) == 4


def test_backend_jobs_reads_every_page_of_an_attempt(gh: FakeGh) -> None:
    gh.page_size = 2
    gh.jobs[(1, 1)] = [job(n, n) for n in range(1, 6)]

    found = shadow.backend_jobs(1, 1, ROOT)

    assert found is not None and [j["id"] for j in found] == [1, 2, 3, 4, 5]
    assert [call[-6:] for call in gh.calls] == ["page=1", "page=2", "page=3", "page=4"]


def test_since_narrows_the_listing(gh: FakeGh, tmp_path: Path) -> None:
    assert run_main(tmp_path, "--since", "2026-11-18") == 0
    assert [c for c in gh.calls if "/runs?" in c][0].endswith("&page=1&created=%3E%3D2026-11-18")
    with pytest.raises(SystemExit):
        run_main(tmp_path, "--since", "2026-11-18&per_page=1")


def test_a_fork_run_is_keyed_on_its_head_repository_and_branch(gh: FakeGh, tmp_path: Path) -> None:
    failed_run(gh, 1, None, ["test/test_a.py"], branch="topic", fork=True)
    failed_run(gh, 2, None, ["test/test_a.py"], branch="topic", fork=True, date=LATER)

    assert run_main(tmp_path) == 0

    # Two pushes of one fork PR are one PR: its failure is not shared with itself.
    assert {row["source"] for row in rows(tmp_path, "failure")} == {"2:topic"}
    marked = ledger.classify(ledger.current(rows(tmp_path), None), 3.0)
    assert [(row["shared"], row["diff_caused"]) for row in marked] == [(False, True)] * 2
    text = summary(tmp_path)
    assert "Fork runs, reported but not fitted: 2." in text
    assert "Misses on fork runs, reported and not fitted: 2." in text
    assert "Misses caused by the diff: 0." in text
    assert rates(tmp_path) == {"schema": 1, "tiers": {}}


# -- verdict artifacts ----------------------------------------------------------


def test_the_verdict_is_read_from_the_runs_artifact(gh: FakeGh, tmp_path: Path) -> None:
    failed_run(gh, 1, 7, ["test/test_a.py"], verdict=None)
    gh.artifacts[1] = [json.dumps(VERDICT)]
    gh.add(2, 8, [[job(21, 1)]], verdict=None)
    gh.add(3, 9, [[job(31, 1)]], verdict=None)
    gh.artifacts[3] = [json.dumps(VERDICT), json.dumps(VERDICT)]

    assert run_main(tmp_path, offline=False) == 0

    assert [(r["run"], r["miss"]) for r in rows(tmp_path, "failure")] == [(1, True)]
    reasons = {row["run"]: row["reason"] for row in rows(tmp_path, "skip")}
    # Run 2 uploaded none; run 3's artifact holds two JSON files, so neither is the verdict.
    assert reasons == {2: ledger.NO_VERDICT, 3: ledger.BAD_VERDICT}
    assert not [call for call in gh.calls if "/runs/2/attempts" in call or "/runs/3/att" in call]


def test_a_run_not_scored_offline_costs_no_call_and_leaves_no_row(
    gh: FakeGh, tmp_path: Path
) -> None:
    gh.add(1, 7, [[job(11, 1)]], verdict=None)

    assert run_main(tmp_path) == 0

    assert rows(tmp_path) == []
    assert [call for call in gh.calls if "/runs/1/" in call] == []


# -- DATA ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"schema": 2},
        {"verdict": "skip"},
        {"tiers": ["test/test_a.py"]},
        {"tiers": {"G": "test/test_a.py"}},
        {"tiers": {"G | [x](http://example.invalid)": []}},
        {"tiers": {"G": ["test/test_a.py | <img src=x>"]}},
        {"would_skip": None},
        {"would_skip": [7]},
    ],
)
def test_a_verdict_of_the_wrong_shape_is_refused(change: dict[str, Any]) -> None:
    assert ledger.parse_verdict(json.dumps(VERDICT)) is not None
    assert ledger.parse_verdict(json.dumps({**VERDICT, **change})) is None


@pytest.mark.parametrize(
    "text",
    ["", "not json", "[1, 2]", "[" * 100_000],
    ids=["empty", "not-json", "a-list", "deeply-nested"],
)
def test_text_that_is_not_a_verdict_object_is_refused(text: str) -> None:
    assert ledger.parse_verdict(text) is None


def test_an_oversized_or_invalid_verdict_excludes_the_run_before_any_job_is_read(
    gh: FakeGh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh.add(1, 7, [[job(11, 1)]])
    gh.add(2, 8, [[job(21, 1)]], verdict={**VERDICT, "schema": 2})
    monkeypatch.setattr(ledger, "MAX_VERDICT_BYTES", len(json.dumps(VERDICT)) - 1)

    assert run_main(tmp_path) == 0

    assert [row["reason"] for row in rows(tmp_path)] == [ledger.BAD_VERDICT] * 2
    assert [call for call in gh.calls if "/jobs" in call] == []


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        "[]",
        '{"kind": "run"}',
        json.dumps({**SKIP, "head": "x"}),
        json.dumps({**SKIP, "date": "soon"}),
        json.dumps({**SKIP, "fork": "no"}),
        # A row keyed on `pr` alone, with no `source`: nothing it could be joined on.
        json.dumps({**OLD_RUN, "verdict": "select", "tiers": {}}),
    ],
)
def test_a_ledger_line_of_the_wrong_shape_stops_the_pass_before_it_appends(
    gh: FakeGh, tmp_path: Path, line: str
) -> None:
    failed_run(gh, 1, 7, ["test/test_a.py"])
    (tmp_path / "ledger.jsonl").write_text(line + "\n", encoding="utf-8")

    assert run_main(tmp_path) == ledger.EXIT_NOT_RECORDED == 2

    assert (tmp_path / "ledger.jsonl").read_text(encoding="utf-8") == line + "\n"
    assert not (tmp_path / "rates.json").exists()


LONG = "test/" + "a" * 250 + ".py"
HOSTILE_RUNS: dict[str, dict[str, Any]] = {
    "a-plain-run": {},
    "branch-with-slashes": {"head_branch": "feature/a/b"},
    "branch-not-ascii": {"head_branch": "ветка-枝-\U0001f33f"},
    "branch-255-characters": {"head_branch": "b" * 255},
    "branch-with-a-lone-surrogate": {"head_branch": "x\ud800y"},
    "branch-with-markup-and-newlines": {"head_branch": '"|`\n\r\t\x00 --> </td>'},
    "no-branch": {"head_branch": None},
    "no-head-repository": {"head_repository": None},
    "head-repository-id-is-text": {"head_repository": {"id": "1"}},
    "head-repository-id-is-a-float": {"head_repository": {"id": float("nan")}},
    "no-base-repository": {"repository": None},
    "pull-request-number-is-text": {"pull_requests": [{"number": "7"}]},
    "pull-requests-is-not-a-list": {"pull_requests": {"number": 7}},
    "attempt-is-text": {"run_attempt": "1"},
    "attempt-is-zero": {"run_attempt": 0},
    "attempt-is-missing": {"run_attempt": None},
    "run-id-is-text": {"id": "1"},
    "run-id-is-a-float": {"id": 1.5},
    "head-sha-is-short": {"head_sha": "abc"},
    "head-sha-is-missing": {"head_sha": None},
    "date-has-fractions": {"created_at": "2026-11-18T00:00:00.123Z"},
    "date-has-an-offset": {"created_at": "2026-11-18T00:00:00+00:00"},
    "date-is-not-a-day": {"created_at": "2026-13-41T00:00:00Z"},
    "date-is-missing": {"created_at": None},
}
HOSTILE_LOGS = {
    "a-plain-failure": ["test/test_a.py"],
    "a-path-the-parser-cuts-short": [LONG],
    "a-cut-path-beside-a-real-one": ["test/test_a.py", LONG],
    "a-path-that-climbs": ["../../etc/x.py"],
}


@pytest.mark.parametrize("change", list(HOSTILE_RUNS.values()), ids=list(HOSTILE_RUNS))
@pytest.mark.parametrize("failing", list(HOSTILE_LOGS.values()), ids=list(HOSTILE_LOGS))
def test_every_row_the_writer_emits_reads_back(
    gh: FakeGh, tmp_path: Path, change: dict[str, Any], failing: list[str]
) -> None:
    failed_run(gh, 1, 7, failing)
    gh.runs[0].update(change)
    gh.add(2, 8, [[job(21, 1)]])

    first = run_main(tmp_path)

    # Whatever GitHub or a log said, the ledger written is one the next pass accepts.
    assert first in (0, ledger.EXIT_WITHHELD)
    written = ledger.read_ledger(tmp_path / "ledger.jsonl")
    assert all(ledger.valid_row(row) for row in written)
    assert written == rows(tmp_path) and 2 in {row["run"] for row in written}
    assert run_main(tmp_path) == first
    assert ledger.read_ledger(tmp_path / "ledger.jsonl") == written


def test_a_failing_path_the_log_parser_cut_short_excludes_the_run(
    gh: FakeGh, tmp_path: Path
) -> None:
    failed_run(gh, 1, 7, ["test/test_a.py", LONG])

    assert run_main(tmp_path) == 0

    assert [(row["kind"], row["reason"]) for row in rows(tmp_path)] == [("skip", ledger.BAD_ROW)]
    assert "Runs excluded: 1; its rows would not read back from the ledger: 1." in summary(tmp_path)
    assert run_main(tmp_path) == 0


def test_the_ledger_is_replaced_whole_and_an_unfinished_last_line_is_closed(
    gh: FakeGh, tmp_path: Path
) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_text(json.dumps(SKIP), encoding="utf-8")
    gh.add(2, 8, [[job(21, 1)]])

    assert run_main(tmp_path, "--no-windows") == 0

    assert [row["run"] for row in ledger.read_ledger(path)] == [1, 2]
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("ledger")) == [
        "ledger.jsonl"
    ]
    ledger.append_rows(path, [])
    assert [row["run"] for row in ledger.read_ledger(path)] == [1, 2]
    with pytest.raises(ValueError):
        ledger.append_rows(path, [{**SKIP, "run": float("inf")}])
    assert [row["run"] for row in ledger.read_ledger(path)] == [1, 2]


def test_an_unreadable_main_red_issue_records_nothing(
    gh: FakeGh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed_run(gh, 1, 7, ["test/test_a.py"])

    def no_issues(args: list[str], root: Path) -> Any:
        if "/issues?" in args[1]:
            raise subprocess.CalledProcessError(1, args)
        return gh.gh_json(args, root)

    monkeypatch.setattr(shadow, "_gh_json", no_issues)
    assert run_main(tmp_path) == ledger.EXIT_NOT_RECORDED == 2
    assert rows(tmp_path) == []


def test_no_summary_cell_can_carry_markup(gh: FakeGh, tmp_path: Path) -> None:
    assert ledger.cell("a|b`c<d>\n[e](f)@g") == "a?b?c?d???e??f??g"
    failed_run(gh, 1, None, ["test/test_a.py"], branch="x|`<b>@team\n| y")

    assert run_main(tmp_path) == 0

    line = next(text for text in summary(tmp_path).splitlines() if "test/test_a.py" in text)
    assert line == "| 1:x???b??team?? y | aaaaaaaaaaaa | 1 | test/test_a.py | G |  |"


# -- WINDOWS ------------------------------------------------------------------

WIN_SELECT = {**VERDICT, "windows": {"verdict": "select", "run": ["test/test_in.py"]}}


def win_job(
    job_id: int, shard: int, conclusion: str = "success", full: bool = True
) -> dict[str, Any]:
    return {**job(job_id, shard, conclusion, full), "name": f"Backend Tests (Windows) ({shard})"}


def win_run(
    gh: FakeGh,
    run_id: int,
    pr: int | None,
    windows: list[str],
    linux: tuple[str, ...] = (),
    full: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """A full Linux run failing `linux`, beside two Windows shards, the second failing `windows`."""
    base = run_id * 10
    jobs = [job(base + 1, 1, "failure" if linux else "success")]
    red = "failure" if windows else "success"
    jobs += [win_job(base + 2, 1, full=full), win_job(base + 3, 2, red, full=full)]
    gh.logs.update({base + 1: list(linux), base + 3: windows})
    return gh.add(run_id, pr, [jobs], **{"verdict": WIN_SELECT, **kwargs})


def test_a_windows_only_failure_outside_windows_run_is_a_windows_miss_and_inside_it_is_not(
    gh: FakeGh, tmp_path: Path
) -> None:
    gh.messages.update({"test/test_win.py": "OSError: [WinError 32] in use"})
    gh.messages["test/test_in.py"] = "Failed: Timeout >120.0s"
    failing = ["test/test_win.py", "test/test_in.py", "test/test_gate.py"]
    win_run(gh, 1, 7, failing, ("test/test_gate.py",))

    assert run_main(tmp_path) == 0

    found = {row["file"]: row for row in rows(tmp_path, "windows_failure")}
    # test_gate.py failed on Linux in the same run: it is not Windows-only.
    assert sorted(found) == ["test/test_in.py", "test/test_win.py"]
    assert found["test/test_win.py"]["miss"] and not found["test/test_win.py"]["in_run"]
    assert found["test/test_in.py"]["in_run"] and not found["test/test_in.py"]["miss"]
    assert [found[f]["label"] for f in sorted(found)] == ["timing", "os"]
    assert [r["trial"] for r in rows(tmp_path, "windows_run")] == ["shadow"]
    assert all(r["os"] == "windows" for r in rows(tmp_path) if r["kind"].startswith("windows_"))
    text = summary(tmp_path)
    assert "Windows misses caused by the diff: 1; on fork runs, not counted: 0." in text
    assert "| #7 | aaaaaaaaaaaa | 1 | test/test_win.py | os | shadow |  |  |" in text
    assert "Windows-only failures: 2; os: 1; timing: 1; unclear: 0." in text
    # The Linux numbers hold the Linux failure alone.
    assert "Misses caused by the diff: 0." in text and "| A | 1 | 1 | 0 |" in text


def test_a_windows_failure_on_another_branch_or_on_linux_elsewhere_is_not_diff_caused(
    gh: FakeGh, tmp_path: Path
) -> None:
    win_run(gh, 1, 7, ["test/test_win.py", "test/test_b.py"])
    win_run(gh, 2, 8, ["test/test_win.py"], date=LATER)
    failed_run(gh, 3, 9, ["test/test_b.py"], date=LATER)

    assert run_main(tmp_path) == 0

    marked = ledger.classify_windows(ledger.current(rows(tmp_path), None), 3.0)
    assert {(r["run"], r["file"], r["shared"], r["diff_caused"]) for r in marked} == {
        (1, "test/test_win.py", True, False),
        (1, "test/test_b.py", True, False),
        (2, "test/test_win.py", True, False),
    }
    assert "Windows misses not caused by it: 3;" in summary(tmp_path)


def test_a_run_with_a_windows_shard_short_of_the_full_suite_is_excluded(
    gh: FakeGh, tmp_path: Path
) -> None:
    win_run(gh, 1, 7, ["test/test_win.py"], full=False)
    gh.add(2, 8, [[job(21, 1)]], verdict=VERDICT)

    assert run_main(tmp_path) == 0

    assert rows(tmp_path, "windows_run") == rows(tmp_path, "windows_failure") == []
    assert sorted(r["reason"] for r in rows(tmp_path, "windows_skip")) == [
        "a backend shard did not run the full suite",
        ledger.NO_WINDOWS_BLOCK,
    ]
    # The Linux trial of run 1 stands: only its Windows half is excluded.
    assert len(rows(tmp_path, "run")) == 2


def test_a_full_windows_run_beside_selected_linux_shards_is_still_a_windows_trial(
    gh: FakeGh, tmp_path: Path
) -> None:
    jobs = [job(11, 1, "failure", full=False), job(12, 2, full=False)]
    jobs += [win_job(13, 1), win_job(14, 2, "failure")]
    gh.logs.update({11: ["test/test_gate.py"], 14: ["test/test_win.py", "test/test_gate.py"]})
    gh.add(1, 7, [jobs], verdict=WIN_SELECT)

    assert run_main(tmp_path) == 0

    # The Linux half is excluded; the Windows half is a trial, net of what Linux failed.
    assert [r["reason"] for r in rows(tmp_path, "skip")] == [
        "a backend shard did not run the full suite"
    ]
    assert [r["trial"] for r in rows(tmp_path, "windows_run")] == ["shadow"]
    assert [(r["file"], r["miss"]) for r in rows(tmp_path, "windows_failure")] == [
        ("test/test_win.py", True)
    ]
    assert "Windows misses caused by the diff: 1;" in summary(tmp_path)


@pytest.mark.parametrize(
    "message, label",
    [
        ("OSError: [WinError 5] Access is denied", "os"),
        ("PermissionError: [Errno 13] denied", "os"),
        ("assert 'C:\\\\Users\\\\x' == '/tmp/x'", "os"),
        ("assert 'a\\\\b\\\\c.py' == 'a/b/c.py'", "os"),
        ("assert 'x\\r\\n' == 'x\\n'", "os"),
        ("AttributeError: module 'os' has no attribute 'getuid'", "os"),
        ("FileNotFoundError: npm.CMD", "os"),
        ("Failed: Timeout >120.0s", "timing"),
        ("AssertionError: async_wait_until(done) still false after 5.00s", "timing"),
        ("[gw3] node down: Not properly terminated", "timing"),
        ("assert 1 == 2", "unclear"),
    ],
)
def test_each_windows_label_is_read_from_the_failure_message(message: str, label: str) -> None:
    assert ledger.windows_label([message]) == label


def test_each_trial_carries_its_source_and_a_push_counts_without_a_verdict(
    gh: FakeGh, tmp_path: Path
) -> None:
    for run_id, source in ((1, "sample"), (2, "label")):
        block = {"verdict": "full", "source": source}
        win_run(gh, run_id, run_id, [], verdict={**VERDICT, "windows": block})
    for run_id, branch in ((3, "main"), (4, "release/1.2"), (5, "feature")):
        win_run(gh, run_id, None, ["test/test_win.py"], verdict=None, branch=branch)["event"] = (
            "push"
        )

    assert run_main(tmp_path) == 0

    trials = {r["run"]: r["trial"] for r in rows(tmp_path, "windows_run")}
    assert trials == {1: "sample", 2: "label", 3: "push", 4: "push"}
    # A push writes Windows rows only, and selects nothing it could miss.
    assert {r["run"] for r in rows(tmp_path, "run")} == {1, 2}
    assert [r["miss"] for r in rows(tmp_path, "windows_failure")] == [False, False]
    assert "Windows trials: 4; shadow: 0; sample: 1; label: 1; push: 2." in summary(tmp_path)
    # None of the four ran under a `select` Windows verdict.
    assert "Windows trials that can show a miss (a `select` Windows verdict): 0." in summary(
        tmp_path
    )


def test_a_miss_names_the_source_of_its_trial(gh: FakeGh, tmp_path: Path) -> None:
    for run_id, source in enumerate(("sample", "label"), 1):
        block = {"verdict": "select", "run": [], "source": source}
        win_run(
            gh, run_id, run_id, [f"test/test_{source}.py"], verdict={**VERDICT, "windows": block}
        )

    assert run_main(tmp_path) == 0

    text = summary(tmp_path)
    for run_id, source in enumerate(("sample", "label"), 1):
        line = (
            f"| #{run_id} | aaaaaaaaaaaa | {run_id} | test/test_{source}.py | unclear | {source} |"
        )
        assert line in text
    assert "Windows trials that can show a miss (a `select` Windows verdict): 2." in text


def test_a_cancelled_run_is_excluded_as_cancelled(gh: FakeGh, tmp_path: Path) -> None:
    push = gh.add(1, None, [[]], verdict=None, branch="main")
    push.update(event="push", conclusion="cancelled")

    assert run_main(tmp_path) == 0

    assert [r["reason"] for r in rows(tmp_path, "windows_skip")] == [ledger.RUN_CANCELLED]


def test_without_windows_no_windows_job_or_push_run_is_read(gh: FakeGh, tmp_path: Path) -> None:
    win_run(gh, 1, 7, ["test/test_win.py"])
    win_run(gh, 2, None, [], verdict=None, branch="main")["event"] = "push"

    assert run_main(tmp_path, "--no-windows") == 0

    assert [r["kind"] for r in rows(tmp_path)] == ["run"]
    assert "## Windows" not in summary(tmp_path)
    assert not [call for call in gh.calls if "/runs/2/" in call or "event=push" in call]


def test_a_linux_only_ledger_gains_its_windows_half_once_and_keeps_its_linux_rows(
    gh: FakeGh, tmp_path: Path
) -> None:
    win_run(gh, 1, 7, ["test/test_win.py"])
    gh.add(2, 8, [[job(21, 1)]], verdict={"schema": 2})
    assert run_main(tmp_path, "--no-windows") == 0
    linux = rows(tmp_path)
    assert [(r["run"], r["kind"]) for r in linux] == [(2, "skip"), (1, "run")]

    assert run_main(tmp_path) == 0

    # Run 1 gains its Windows rows; run 2's verdict was refused, so it has no Windows half.
    assert rows(tmp_path)[: len(linux)] == linux
    added = rows(tmp_path)[len(linux) :]
    assert [(r["run"], r["kind"]) for r in added] == [(1, "windows_run"), (1, "windows_failure")]
    before = len(gh.calls)
    assert run_main(tmp_path) == 0
    assert len(rows(tmp_path)) == len(linux) + 2
    assert not [call for call in gh.calls[before:] if "/jobs" in call or "logs" in call]


def test_an_unreadable_windows_log_keeps_the_linux_rows_and_leaves_windows_for_later(
    gh: FakeGh, tmp_path: Path
) -> None:
    win_run(gh, 1, 7, ["test/test_win.py"], ("test/test_a.py",))
    windows_log = gh.logs.pop(13)

    assert run_main(tmp_path) == 0
    linux = rows(tmp_path)
    assert [r["kind"] for r in linux] == ["run", "failure"]

    gh.logs[13] = windows_log
    assert run_main(tmp_path) == 0

    assert rows(tmp_path)[: len(linux)] == linux
    added = rows(tmp_path)[len(linux) :]
    assert [r["kind"] for r in added] == ["windows_run", "windows_failure"]


def test_a_linux_rerun_keeps_the_windows_rows_its_unread_windows_half_left() -> None:
    def row(kind: str, attempt: int) -> dict[str, object]:
        return {"kind": kind, "run": 1, "attempt": attempt, "date": "2026-11-20T00:00:00Z"}

    held = [row("run", 1), row("windows_run", 1), row("windows_failure", 1), row("run", 2)]

    assert ledger.current(held, None) == held[1:]
    assert ledger.current([*held, row("windows_run", 2)], None) == [held[3], row("windows_run", 2)]


def test_a_pr_backlog_past_the_limit_still_reads_the_newer_push_runs(
    gh: FakeGh, tmp_path: Path
) -> None:
    for run_id in range(1, 5):
        gh.add(run_id, run_id, [[job(run_id * 10, 1)]], date=f"2026-11-1{run_id}T00:00:00Z")
    push = win_run(gh, 9, None, [], verdict=None, branch="main", date="2026-11-13T12:00:00Z")
    push["event"] = "push"

    assert run_main(tmp_path, "--limit", "2") == 0

    # Newest first across both events: PR run 4, then the push, which is newer than PR run 3.
    assert [r["run"] for r in rows(tmp_path) if r["kind"] in ("run", "windows_run")] == [4, 9]


@pytest.mark.parametrize(
    "block",
    [
        "select",
        {"verdict": "skip", "run": []},
        {"verdict": "select"},
        {"verdict": "select", "run": ["../x"]},
        {"verdict": "full", "source": "push"},
    ],
)
def test_a_windows_block_of_the_wrong_shape_excludes_the_windows_half_alone(
    block: object, gh: FakeGh, tmp_path: Path
) -> None:
    win_run(
        gh, 1, 7, ["test/test_win.py"], ("test/test_a.py",), verdict={**VERDICT, "windows": block}
    )

    assert run_main(tmp_path) == 0

    # The Linux observation stands, with its miss; no Windows trial or failure is recorded.
    assert [r["kind"] for r in rows(tmp_path)] == ["run", "failure", "windows_skip"]
    assert [r["miss"] for r in rows(tmp_path, "failure")] == [True]
    assert [r["reason"] for r in rows(tmp_path, "windows_skip")] == [ledger.BAD_WINDOWS_BLOCK]


NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)


def _at(days_ago: float, **row: Any) -> dict[str, Any]:
    stamp = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"fork": False, "date": stamp, **row}


@pytest.mark.parametrize(
    "trials, full, ago, ok",
    [
        (20, 0, [], True),
        (20, 0, [1, 2], False),
        (20, 0, [15, 0], True),
        (19, 0, [], False),
        # Trials under a `full` verdict cannot show a miss: they do not make up the minimum.
        (19, 30, [], False),
        (20, 30, [], True),
    ],
)
def test_windows_selection_turns_off_on_the_bar_or_too_few_trials(
    trials: int, full: int, ago: list[float], ok: bool
) -> None:
    held = [_at(1, kind="windows_run", verdict="select") for _ in range(trials)]
    held += [_at(1, kind="windows_run", verdict="full") for _ in range(full)]
    held += [_at(15, kind="windows_run", verdict="select")]
    misses = [_at(days, miss=True, diff_caused=True) for days in ago]
    misses += [_at(0, miss=True, diff_caused=False), _at(0, miss=True, diff_caused=True, fork=True)]

    status = ledger.windows_status(held, misses, NOW)

    assert status == {
        "windows_selection_ok": ok,
        "window_days": 14,
        "diff_caused_windows_misses": len([d for d in ago if d <= 14]),
        "trials": trials + full,
        "trials_that_can_miss": trials,
        "bar": 2,
    }


def test_the_status_line_is_in_the_summary_and_its_own_file(gh: FakeGh, tmp_path: Path) -> None:
    win_run(gh, 1, 7, [])

    assert run_main(tmp_path, "--windows-status-out", str(tmp_path / "status.json")) == 0

    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["windows_selection_ok"] is False and status["bar"] == 2
    assert summary(tmp_path).rstrip().splitlines()[-1] == json.dumps(status, sort_keys=True)
