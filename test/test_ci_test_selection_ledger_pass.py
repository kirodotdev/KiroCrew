"""Contract tests for what one pass of the miss ledger reads, and what it spends.

`scripts/ci_test_selection_ledger.py`, read through the in-memory GitHub of
`ci_test_selection_ledger_fake.py`. Pinned:

* what failed -- a failed job's annotations are the answer when they can be the
  whole of it; otherwise its log is read too, and a run whose failing tests
  cannot all be read is excluded with the reason, never a trial and never asked
  for again;
* the listing -- a window GitHub will not list whole is read in parts that leave
  no gap and no overlap;
* what a pass spends -- it does not start under its reserve, stops at its calls,
  keeps what it read whatever ends it, reads a verdict from one listing and one
  download, and reads no job of a run that can show nothing.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from ci_test_selection_ledger_fake import (
    FULL,
    ROOT,
    VERDICT,
    WIN_SELECT,
    FakeGh,
    failed_run,
    install,
    job,
    rows,
    run_main,
    summary,
    win_job,
    win_run,
)

from scripts import ci_test_selection_ledger as ledger

# The script's own binding of the shadow module: the one its calls go through.
shadow = ledger.shadow


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeGh:
    return install(monkeypatch, tmp_path)


# -- what failed: annotations first -------------------------------------------


def _annotated_run(gh: FakeGh, annotated: list[str], logged: list[str] | None) -> None:
    """Run 1: shard 2 failed; its annotations name `annotated`, its log `logged` (None: unreadable)."""
    held = {**VERDICT, "tiers": {"G": sorted({*annotated, *(logged or [])})}, "would_skip": []}
    gh.add(1, 7, [[job(11, 1), job(12, 2, "failure")]], verdict=held)
    gh.annotations[12] = annotated
    if logged is not None:
        gh.logs[12] = logged


def _suite_limit() -> int:
    """The number of failures the suite annotates, read from where it is set."""
    source = (ROOT / "test" / "conftest_floor_reporting.py").read_text(encoding="utf-8")
    found = re.search(r"^_MAX_ANNOTATED_FAILURES = (\d+)$", source, re.MULTILINE)
    assert found is not None
    return int(found.group(1))


def test_the_annotation_limit_is_the_suites_own() -> None:
    assert ledger.ANNOTATED_FAILURES_MAX == _suite_limit()


@pytest.mark.parametrize("named", [1, 2, ledger.ANNOTATED_FAILURES_MAX - 1])
def test_annotations_that_name_fewer_tests_than_the_suite_writes_are_the_whole_answer(
    gh: FakeGh, tmp_path: Path, named: int
) -> None:
    files = [f"test/test_n{n}.py" for n in range(named)]
    _annotated_run(gh, files, None)

    assert run_main(tmp_path) == 0

    assert [r["file"] for r in rows(tmp_path, "failure")] == files
    # The log, which the Actions token cannot read in this repository, is never asked for.
    assert not [call for call in gh.calls if "logs" in call]


@pytest.mark.parametrize(
    "annotated",
    [0, ledger.ANNOTATED_FAILURES_MAX, ledger.ANNOTATED_FAILURES_MAX + 3],
    ids=["none named", "at the limit", "past the limit"],
)
def test_annotations_that_may_not_be_the_whole_answer_need_the_log(
    gh: FakeGh, tmp_path: Path, annotated: int
) -> None:
    files = [f"test/test_n{n:02}.py" for n in range(annotated)]
    _annotated_run(gh, files, [*files, "test/test_only_in_the_log.py"])

    assert run_main(tmp_path) == 0

    # What either names failed.
    assert [r["file"] for r in rows(tmp_path, "failure")] == sorted(
        [*files, "test/test_only_in_the_log.py"]
    )

    # The same run when the log cannot be read: its failing tests are not all
    # known, so it is excluded with the reason, is no trial, and is not asked for again.
    del gh.logs[12]
    again = tmp_path / "again"
    again.mkdir()
    shutil.copytree(tmp_path / "verdicts", again / "verdicts")
    assert run_main(again) == 0
    # Without the Linux failures no failure can be called Windows-only either.
    assert [(r["kind"], r["reason"]) for r in rows(again)] == [
        ("skip", ledger.UNREAD_FAILURES),
        ("windows_skip", f"Linux: {ledger.UNREAD_FAILURES}"),
    ]
    assert f"Runs excluded: 1; {ledger.UNREAD_FAILURES}: 1." in summary(again)
    assert "Runs fitted: 0 with a `select` verdict; 0 more" in summary(again)
    before = len(gh.calls)
    assert run_main(again) == 0
    assert not [call for call in gh.calls[before:] if "/runs/1/" in call or "check-runs" in call]


def test_a_full_page_of_annotations_is_not_taken_for_all_of_them(
    gh: FakeGh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ledger, "ANNOTATION_PAGE", 2)
    _annotated_run(gh, ["test/test_a.py", "test/test_b.py"], ["test/test_c.py"])

    assert run_main(tmp_path) == 0

    assert [r["file"] for r in rows(tmp_path, "failure")] == [
        "test/test_a.py",
        "test/test_b.py",
        "test/test_c.py",
    ]


def test_a_windows_label_is_read_from_an_annotation_as_from_a_log(
    gh: FakeGh, tmp_path: Path
) -> None:
    win_run(gh, 1, 7, [])
    gh.set_attempts(1, [[job(11, 1), win_job(12, 1), win_job(13, 2, "failure")]])
    gh.annotations[13] = ["test/test_win.py"]
    gh.messages["test/test_win.py"] = "PermissionError: [WinError 5] Access is denied"

    assert run_main(tmp_path) == 0

    assert [(r["file"], r["label"]) for r in rows(tmp_path, "windows_failure")] == [
        ("test/test_win.py", "os")
    ]
    assert not [call for call in gh.calls if "logs" in call]


# -- the listing, in parts ------------------------------------------------------


def _windows_read(gh: FakeGh) -> list[tuple[str, str]]:
    """The `created` windows whose first page was asked for, as `(start, end)`."""
    found = [
        re.search(r"&page=1&created=([\dTZ:-]+)\.\.([\dTZ:-]+)", call)
        for call in gh.calls
        if "event=pull_request" in call
    ]
    return [(match[1], match[2]) for match in found if match]


@pytest.mark.parametrize("most", [1, 2, 3, 7])
def test_a_window_github_will_not_list_whole_is_read_in_parts_with_no_gap_and_no_overlap(
    gh: FakeGh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, most: int
) -> None:
    monkeypatch.setattr(ledger, "MAX_LISTED_RUNS", most)
    stamps = [f"2026-11-1{day}T{hour:02}:00:00Z" for day in (1, 2, 4) for hour in (0, 9, 23)]
    for run_id, stamp in enumerate(stamps, 1):
        gh.add(run_id, run_id, [[job(run_id * 10, 1)]], date=stamp)

    assert run_main(tmp_path, "--since", "2026-11-11", "--no-windows") == 0

    # Every run once, newest first, however small a part GitHub lists.
    assert [r["run"] for r in rows(tmp_path, "run")] == list(range(len(stamps), 0, -1))
    # The parts that were listed (not halved again) tile the window: each starts
    # one second after the one before it ends.
    listed = sorted(
        (start, end)
        for start, end in _windows_read(gh)
        if len([s for s in stamps if start <= s <= end]) <= most
    )
    assert listed[0][0] == "2026-11-11T00:00:00Z"
    for (_, end), (start, _) in zip(listed, listed[1:]):
        gap = datetime.fromisoformat(start.replace("Z", "+00:00")) - datetime.fromisoformat(
            end.replace("Z", "+00:00")
        )
        assert gap == timedelta(seconds=1)


def test_a_listing_that_cannot_be_split_says_what_it_did_not_read(
    gh: FakeGh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ledger, "MAX_LISTED_RUNS", 2)
    for run_id in range(1, 5):
        gh.add(run_id, run_id, [[job(run_id * 10, 1)]])

    # With no `--since` there is no window to halve.
    assert run_main(tmp_path, "--no-windows") == 0

    assert "the listing holds 4 runs and GitHub gives 2: the 2 oldest are not read" in (
        capsys.readouterr().err
    )


# -- what a pass spends -------------------------------------------------------


def _calls(gh: FakeGh, start: int = 0) -> list[str]:
    return [call for call in gh.calls[start:] if call != "rate_limit"]


def test_a_pass_does_not_start_when_the_pool_is_under_its_reserve(
    gh: FakeGh, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh.add(1, 7, [[job(11, 1)]])
    gh.pool = 1099

    assert run_main(tmp_path, "--reserve", "1000", "--max-calls", "100") == ledger.EXIT_REFUSED

    assert _calls(gh) == [] and rows(tmp_path) == []
    assert "not started: the REST pool has 1099 requests left this hour" in capsys.readouterr().err
    gh.pool = 1100
    assert run_main(tmp_path, "--reserve", "1000", "--max-calls", "100") == 0
    assert [r["run"] for r in rows(tmp_path, "run")] == [1]


def test_a_pass_stops_at_its_calls_keeps_what_it_read_and_a_later_pass_reads_the_rest(
    gh: FakeGh, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for run_id in range(1, 7):
        gh.add(run_id, run_id, [[job(run_id * 10, 1)]])

    passes = 0
    while len(rows(tmp_path, "run")) < 6:
        before = len(gh.calls)
        assert run_main(tmp_path, "--max-calls", "4", "--no-windows") == 0
        passes += 1
        # The limit is checked between runs: a pass ends within one run's calls of it.
        assert len(_calls(gh, before)) <= 4 + 2
        assert passes <= 6

    assert passes > 1 and "stopped early: this pass has made its 4 calls" in capsys.readouterr().err
    assert [r["run"] for r in rows(tmp_path, "run")] == [6, 5, 4, 3, 2, 1]


def test_a_call_one_reader_makes_through_another_is_counted_once(
    gh: FakeGh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for run_id in range(1, 4):
        gh.add(run_id, run_id, [[job(run_id * 10, 1)]])
    answer = gh.gh_json

    def json_through_text(args: list[str], root: Path) -> Any:
        # As the real reader does: the JSON call is one text call, parsed.
        shadow._run(["gh", "api", "rate_limit-shaped"], root)
        return answer(args, root)

    monkeypatch.setattr(shadow, "_run", lambda argv, root: "")
    monkeypatch.setattr(shadow, "_gh_json", json_through_text)

    assert run_main(tmp_path, "--no-windows") == 0

    assert f"gh calls this pass: {len(gh.calls)}" in capsys.readouterr().err


def test_a_pass_stops_when_the_pool_falls_under_the_reserve(
    gh: FakeGh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ledger, "RECHECK_CALLS", 2)
    for run_id in range(1, 5):
        gh.add(run_id, run_id, [[job(run_id * 10, 1)]])
    gh.pool = 2000
    drained = gh.gh_json

    def other_jobs_spend_the_pool(args: list[str], root: Path) -> Any:
        if args[1] != "rate_limit":
            gh.pool -= 400
        return drained(args, root)

    monkeypatch.setattr(shadow, "_gh_json", other_jobs_spend_the_pool)

    assert run_main(tmp_path, "--reserve", "1000", "--no-windows") == 0

    assert 0 < len(rows(tmp_path, "run")) < 4
    assert "stopped early: the REST pool is down to" in capsys.readouterr().err


def test_a_pass_that_fails_midway_keeps_the_rows_it_read(
    gh: FakeGh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ledger, "KEEP_EVERY_RUNS", 2)
    gh.page_size = 3
    for run_id in range(1, 7):
        gh.add(run_id, run_id, [[job(run_id * 10, 1)]])
    listing = gh.gh_json

    def second_page_fails(args: list[str], root: Path) -> Any:
        if "/runs?" in args[1] and "&page=2" in args[1]:
            raise subprocess.CalledProcessError(1, args)
        return listing(args, root)

    monkeypatch.setattr(shadow, "_gh_json", second_page_fails)

    assert run_main(tmp_path, "--no-windows") == ledger.EXIT_NOT_RECORDED

    assert [r["run"] for r in rows(tmp_path, "run")] == [6, 5, 4]
    monkeypatch.setattr(shadow, "_gh_json", listing)
    assert run_main(tmp_path, "--no-windows") == 0
    assert [r["run"] for r in rows(tmp_path, "run")] == [6, 5, 4, 3, 2, 1]


def test_a_green_run_under_full_verdicts_is_excluded_without_reading_a_job(
    gh: FakeGh, tmp_path: Path
) -> None:
    full_both = {**FULL, "windows": {"verdict": "full", "reason": "linux_full: no bundle"}}
    gh.add(1, 7, [[job(11, 1)]], verdict=full_both)["conclusion"] = "success"
    gh.add(2, 8, [[job(21, 1)]], verdict=FULL)["conclusion"] = "success"
    # Red under the same verdict: read, for the files it says failed elsewhere.
    failed_run(gh, 3, 9, ["test/test_a.py"], verdict=full_both)
    gh.runs[0]["conclusion"] = "failure"
    # Green, but one of its verdicts selects: a trial.
    gh.add(
        4, 10, [[job(41, 1), win_job(42, 1)]], verdict={**FULL, "windows": WIN_SELECT["windows"]}
    )
    gh.runs[0]["conclusion"] = "success"
    gh.add(5, 11, [[job(51, 1)]])["conclusion"] = "success"

    assert run_main(tmp_path) == 0

    reasons = {r["run"]: r["reason"] for r in rows(tmp_path, "skip")}
    assert reasons == {1: ledger.FULL_GREEN, 2: ledger.FULL_GREEN}
    assert not [call for call in gh.calls if "/runs/1/" in call or "/runs/2/" in call]
    assert sorted(r["run"] for r in rows(tmp_path, "run")) == [3, 4, 5]
    assert [r["run"] for r in rows(tmp_path, "windows_run")] == [4]
    assert "Not read, so not a trial: a green push" in summary(tmp_path)
    # Neither lane of the two is asked for again.
    before = len(gh.calls)
    assert run_main(tmp_path) == 0
    assert not [call for call in gh.calls[before:] if "/attempts/" in call]


def test_the_verdicts_of_a_pass_come_from_one_listing_and_one_download_each(
    gh: FakeGh, tmp_path: Path
) -> None:
    for run_id in range(1, 6):
        gh.add(
            run_id,
            run_id,
            [[job(run_id * 10, 1)]],
            verdict=None,
            date=f"2026-11-1{run_id}T00:00:00Z",
        )
        gh.artifacts[run_id] = [json.dumps(VERDICT)]
    gh.expired.add(2)
    del gh.artifacts[4]
    gh.twice.add(5)

    assert run_main(tmp_path, "--no-windows", offline=False) == 0

    assert sorted(r["run"] for r in rows(tmp_path, "run")) == [1, 3]
    reasons = {r["run"]: r["reason"] for r in rows(tmp_path, "skip")}
    # An expired artifact is no verdict, like none at all; two of one run are not one verdict.
    assert reasons == {2: ledger.NO_VERDICT, 4: ledger.NO_VERDICT, 5: ledger.BAD_VERDICT}
    assert len([call for call in gh.calls if "/actions/artifacts?name=" in call]) == 1
    assert len([call for call in gh.calls if call.endswith("/zip")]) == 2


def test_the_artifact_listing_is_read_only_as_far_back_as_the_runs_asked_for(
    gh: FakeGh, tmp_path: Path
) -> None:
    gh.page_size = 2
    for run_id in range(1, 9):
        gh.add(
            run_id,
            run_id,
            [[job(run_id * 10, 1)]],
            verdict=None,
            date=f"2026-11-1{run_id}T00:00:00Z",
        )
        gh.artifacts[run_id] = [json.dumps(VERDICT)]

    assert run_main(tmp_path, "--no-windows", "--limit", "3", offline=False) == 0

    assert [r["run"] for r in rows(tmp_path, "run")] == [8, 7, 6]
    # Runs 8 to 6 are on the first two pages of four.
    assert len([call for call in gh.calls if "/actions/artifacts?name=" in call]) == 2
