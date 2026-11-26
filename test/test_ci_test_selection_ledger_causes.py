"""Contract tests for the reasons the miss ledger decides at report time, and its settled passes.

`scripts/ci_test_selection_ledger_causes.py`, through `ci_test_selection_ledger.py`
and the in-memory GitHub of `ci_test_selection_ledger_fake.py`. Pinned:

* FLAKE -- a failure is a flake only when another branch failed the file and
  passed it on a rerun, inside the window; its own branch's rerun clears nothing.
* ONE OFF -- a counted Windows miss never rerun and seen once in 14 days
  carries a note, and every count is what it is without the note.
* REAL MISS -- a file that fails on one branch alone, head after head, stays
  caused by the diff, with or without notes.
* FROM MAIN -- a main-red issue the watcher wrote labels the failures created
  between its first red run and its close, whether it is open, closed or was
  filed after the row was recorded; nothing else labels one.
* SETTLED -- a pass that left a run unread holds the newest settled answer of
  the last 36 hours, and is judged on its own rows when there is none.
* DATA -- notes that do not read as notes label nothing and settle nothing.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from ci_test_selection_ledger_fake import (
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
from scripts import ci_test_selection_ledger_causes as causes

shadow = ledger.shadow

NOW = datetime(2026, 11, 19, tzinfo=timezone.utc)
MISS = "test/test_win.py"


def _day(day: int, hour: int = 0) -> str:
    return f"2026-11-{day:02d}T{hour:02d}:00:00Z"


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeGh:
    return install(monkeypatch, tmp_path)


def _pass(tmp_path: Path, *extra: str, now: datetime = NOW) -> int:
    """One pass that keeps notes beside the ledger, at `now`."""
    argv = ["--ledger", str(tmp_path / "ledger.jsonl"), "--rates-out", str(tmp_path / "rates.json")]
    argv += [
        "--summary-out",
        str(tmp_path / "summary.md"),
        "--notes",
        str(tmp_path / "notes.jsonl"),
    ]
    argv += ["--verdict-dir", str(tmp_path / "verdicts")]
    argv += ["--windows-status-out", str(tmp_path / "status.json")]
    return ledger.main([*argv, *extra], now=now)


def _status(tmp_path: Path) -> dict[str, Any]:
    status: dict[str, Any] = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    return status


def _notes(tmp_path: Path, kind: str) -> list[dict[str, Any]]:
    return [note for note in causes.read_notes(tmp_path / "notes.jsonl") if note["kind"] == kind]


def _windows(tmp_path: Path) -> dict[int, dict[str, Any]]:
    """Each run's classified Windows failure of `MISS`, by run id."""
    held = ledger.current(rows(tmp_path), None)
    notes = causes.read_notes(tmp_path / "notes.jsonl")
    # With no notes the call is the one a pass without `--notes` makes.
    marked = (
        ledger.classify_windows(held, 3.0, notes) if notes else ledger.classify_windows(held, 3.0)
    )
    return {row["run"]: row for row in marked if row["file"] == MISS}


def _rerun_green(gh: FakeGh, run_id: int) -> None:
    """A second attempt of `run_id` that reran its red Windows shard to a pass."""
    first = gh.jobs[(run_id, 1)]
    gh.set_attempts(run_id, [first, [win_job(run_id * 10 + 4, 2)]])


# -- FLAKE --------------------------------------------------------------------


@pytest.mark.parametrize(
    "pr, day, rerun, flake",
    [
        # Another branch failed the file and passed it on a rerun, five days before.
        (7, 13, True, True),
        # Another branch failed it and was never rerun: no one saw it pass.
        (7, 13, False, False),
        # Its own branch reran it green: a diff that makes a test flaky is that diff's doing.
        (8, 13, True, False),
        # Another branch's flaky pass, outside the window.
        (7, 3, True, False),
    ],
)
def test_a_miss_is_a_flake_only_when_another_branch_passed_the_file_on_a_rerun(
    gh: FakeGh, tmp_path: Path, pr: int, day: int, rerun: bool, flake: bool
) -> None:
    win_run(gh, 1, pr, [MISS], date=_day(day))
    if rerun:
        _rerun_green(gh, 1)
    win_run(gh, 2, 8, [MISS], date=_day(18))

    assert run_main(tmp_path) == 0

    row = _windows(tmp_path)[2]
    assert row["miss"] and not row["rerun_green"] and not row["shared"]
    assert (row.get("flake", False), row["diff_caused"]) == (flake, not flake)
    cell = "flake" if flake else ""
    assert f"| #8 | aaaaaaaaaaaa | 2 | {MISS} | unclear | shadow |  | {cell} |" in summary(tmp_path)


def test_a_linux_failure_another_branch_passed_on_a_rerun_is_a_flake_in_the_fit(
    gh: FakeGh, tmp_path: Path
) -> None:
    failed_run(gh, 1, 7, ["test/test_a.py"], date=_day(13))
    gh.set_attempts(1, [gh.jobs[(1, 1)], [job(19, 2)]])
    failed_run(gh, 2, 8, ["test/test_a.py"], date=_day(18))

    assert run_main(tmp_path) == 0

    assert "| #8 | aaaaaaaaaaaa | 2 | test/test_a.py | G | flake |  |" in summary(tmp_path)
    assert "Misses caused by the diff: 0." in summary(tmp_path)


# -- ONE OFF ------------------------------------------------------------------


def test_a_windows_miss_never_rerun_and_seen_once_is_noted_one_off_and_no_count_moves(
    gh: FakeGh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _twenty_trials(gh)
    gh.messages[MISS] = "PermissionError: [WinError 32] in use"
    win_run(gh, 1, 7, [MISS], date=_day(18))
    # A Linux miss seen once, and a Windows failure inside the run set: neither is noted.
    failed_run(gh, 2, 6, ["test/test_a.py"], date=_day(18))
    win_run(gh, 3, 5, ["test/test_in.py"], date=_day(18))

    assert _pass(tmp_path) == 0

    assert _windows(tmp_path)[1]["one_off"] and _windows(tmp_path)[1]["diff_caused"]
    text = summary(tmp_path)
    assert f"| #7 | aaaaaaaaaaaa | 1 | {MISS} | os | shadow |  |  | one_off |" in text
    assert text.count("one_off") == 1
    held = ledger.current(rows(tmp_path), None)
    marked = [*ledger.classify(held, 3.0), *ledger.classify_windows(held, 3.0)]
    assert sorted((row["run"], row["file"]) for row in marked if row["diff_caused"]) == [
        (1, MISS),
        (2, "test/test_a.py"),
        (3, "test/test_in.py"),
    ]
    assert [row["run"] for row in marked if row["one_off"]] == [1]
    status = _status(tmp_path)
    assert status["diff_caused_windows_misses"] == 1
    # With the note never given, the summary differs in that one cell and nothing else.
    monkeypatch.setattr(ledger.causes, "one_off", lambda row, same_file: False)
    assert _pass(tmp_path, "--limit", "0") == 0
    assert summary(tmp_path) == text.replace(" one_off |", "  |")
    assert _status(tmp_path) == status


@pytest.mark.parametrize(
    "seen_again, rerun_red, cleared",
    [
        # The same branch failed the file on another head a day later: a second sighting.
        (True, False, False),
        # Rerun, and red again: it was rerun.
        (False, True, False),
        # Cleared rows carry their reason, never the note.
        (False, False, True),
    ],
)
def test_a_failure_seen_twice_rerun_or_cleared_is_not_noted_one_off(
    gh: FakeGh, tmp_path: Path, seen_again: bool, rerun_red: bool, cleared: bool
) -> None:
    win_run(gh, 1, 7, [MISS], date=_day(17))
    if seen_again:
        win_run(gh, 2, 7, [MISS], date=_day(18))
    if rerun_red:
        gh.set_attempts(1, [gh.jobs[(1, 1)], [win_job(14, 2, "failure")]])
        gh.logs[14] = [MISS]
    if cleared:
        gh.issues = [_issue(40, f"{MISS}::test_one", first_run=1)]

    assert _pass(tmp_path) == 0

    row = _windows(tmp_path)[1]
    assert row["one_off"] is False and row["diff_caused"] is not cleared
    assert "one_off |" not in summary(tmp_path)


def test_a_sighting_outside_the_window_leaves_the_miss_noted_one_off(
    gh: FakeGh, tmp_path: Path
) -> None:
    win_run(gh, 1, 7, [MISS], date=_day(2))
    win_run(gh, 2, 7, [MISS], date=_day(18))

    assert _pass(tmp_path) == 0

    assert [_windows(tmp_path)[run]["one_off"] for run in (1, 2)] == [True, True]


# -- REAL MISS ----------------------------------------------------------------


def test_a_file_one_branch_alone_fails_head_after_head_stays_caused_by_the_diff(
    gh: FakeGh, tmp_path: Path
) -> None:
    for run_id, day in ((1, 16), (2, 17), (3, 18)):
        win_run(gh, run_id, 9, [MISS], date=_day(day))
    # A Linux failure outside the run set, on one pull request: the plain miss.
    failed_run(gh, 4, 6, ["test/test_a.py"], date=_day(18))

    assert run_main(tmp_path) == 0

    assert {run: row["diff_caused"] for run, row in _windows(tmp_path).items()} == {
        1: True,
        2: True,
        3: True,
    }
    text = summary(tmp_path)
    assert "Windows misses caused by the diff: 3; on fork runs, not counted: 0." in text
    assert "Misses caused by the diff: 1." in text


def test_notes_about_other_files_and_other_days_clear_no_real_miss(
    gh: FakeGh, tmp_path: Path
) -> None:
    for run_id, day in ((11, 16), (12, 17), (13, 18)):
        win_run(gh, run_id, 9, [MISS], date=_day(day))
    failed_run(gh, 14, 6, ["test/test_a.py"], date=_day(18))
    gh.issues = [
        # Main was red on another file, and on these files only before these runs.
        _issue(40, "test/test_c.py::test_one", first_run=1),
        _issue(41, f"{MISS}::test_one", first_run=1, closed=_day(15)),
        _issue(42, "test/test_a.py::test_one", first_run=1, closed=_day(15)),
        # And it went red on them again only after these runs.
        _issue(43, f"{MISS}::test_one", first_run=20),
        _issue(44, "test/test_a.py::test_one", first_run=20),
    ]

    assert _pass(tmp_path) == 0

    assert len(_notes(tmp_path, "main_red")) == 5
    text = summary(tmp_path)
    assert "Windows misses caused by the diff: 3; on fork runs, not counted: 0." in text
    assert "Misses caused by the diff: 1." in text


# -- FROM MAIN ----------------------------------------------------------------


def _issue(
    number: int,
    nodeid: str,
    first_run: int = 5,
    closed: str | None = None,
    login: str = "github-actions[bot]",
    **changed: Any,
) -> dict[str, Any]:
    """A main-red issue as the REST listing gives it, with the watcher's state record."""
    watch = shadow.watch_module()
    url = "https://github.com/kirodotdev/KiroCrew/actions/runs/5"
    state = {"first_sha": "abc123", "first_run_id": first_run, "first_url": url, "streak": 1}
    state.update({"last_sha": "abc123", "last_run_id": first_run, "last_url": url})
    state["failures"] = {nodeid: "assert 1 == 2"}
    return {
        "number": number,
        "state": "closed" if closed else "open",
        "closed_at": closed,
        "title": watch.TITLE_PREFIX + "1 test",
        "body": f"text\n{watch.STATE_OPEN}{watch.dump_state(state)}{watch.STATE_CLOSE}\n",
        "user": {"login": login},
        "labels": [{"name": "main-red"}],
        **changed,
    }


def _three_runs(gh: FakeGh) -> None:
    """The same file failing before main went red (4), while it was (6) and after it closed (8)."""
    failed_run(gh, 4, 7, ["test/test_a.py"], date=_day(18, 7))
    failed_run(gh, 6, 8, ["test/test_a.py"], date=_day(18, 9))
    failed_run(gh, 8, 9, ["test/test_a.py"], date=_day(18, 12))


def _linux(tmp_path: Path) -> dict[int, tuple[bool, bool, bool]]:
    notes = causes.read_notes(tmp_path / "notes.jsonl")
    marked = ledger.classify(ledger.current(rows(tmp_path), None), 0.0, notes)
    return {row["run"]: (row["main_red"], row["from_main"], row["diff_caused"]) for row in marked}


def test_an_issue_closed_before_the_pass_labels_the_failures_of_its_span_alone(
    gh: FakeGh, tmp_path: Path
) -> None:
    _three_runs(gh)
    gh.issues = [_issue(40, "test/test_a.py::test_one", closed=_day(18, 10))]

    assert _pass(tmp_path, "--window-days", "0") == 0

    # No issue was open, so no row was recorded `main_red`: the span decides.
    assert _linux(tmp_path) == {
        4: (False, False, True),
        6: (False, True, False),
        8: (False, False, True),
    }
    assert "| #8 | aaaaaaaaaaaa | 6 | test/test_a.py | G | from_main |  |" in summary(tmp_path)
    # One call lists the open and the closed issues alike.
    listed = [call for call in gh.calls if "/issues?" in call]
    assert len(listed) == 1 and "state=all" in listed[0] and "since=" not in listed[0]
    # The span outlives the listing: a pass whose listing lacks the issue reports the same.
    gh.issues = []
    assert _pass(tmp_path, "--window-days", "0") == 0
    assert _linux(tmp_path)[6] == (False, True, False)


def test_an_issue_filed_after_the_row_was_recorded_still_labels_it(
    gh: FakeGh, tmp_path: Path
) -> None:
    _three_runs(gh)

    assert _pass(tmp_path, "--window-days", "0") == 0
    assert _linux(tmp_path)[6] == (False, False, True)

    gh.issues = [_issue(40, "test/test_a.py::test_one")]
    assert _pass(tmp_path, "--window-days", "0", now=NOW + timedelta(hours=1)) == 0

    # Still open, so every run from the first red one on is from main.
    assert _linux(tmp_path) == {
        4: (False, False, True),
        6: (False, True, False),
        8: (False, True, False),
    }


def test_a_row_recorded_under_the_open_issue_names_one_reason_not_two(
    gh: FakeGh, tmp_path: Path
) -> None:
    failed_run(gh, 6, 8, ["test/test_a.py"])
    gh.issues = [_issue(40, "test/test_a.py::test_one")]

    assert _pass(tmp_path) == 0

    assert _linux(tmp_path) == {6: (True, True, False)}
    assert "| #8 | aaaaaaaaaaaa | 6 | test/test_a.py | G | main_red |  |" in summary(tmp_path)


NOT_THE_WATCHERS = {
    "a person wrote it": {"user": {"login": "someone"}},
    "no author": {"user": None},
    "another title": {"title": "Main is not red"},
    "no state record": {"body": "main is red on test/test_a.py"},
    "a pull request": {"pull_request": {}},
    "another label": {"labels": [{"name": "bug"}]},
    "closed with no close time": {"state": "closed", "closed_at": None},
    "a close time that is not one": {"state": "closed", "closed_at": "yesterday"},
    "a state GitHub does not give": {"state": "locked"},
    "not an issue": None,
}


@pytest.mark.parametrize("name", sorted(NOT_THE_WATCHERS))
def test_an_issue_that_does_not_read_as_the_watchers_labels_nothing(
    gh: FakeGh, tmp_path: Path, name: str
) -> None:
    failed_run(gh, 6, 8, ["test/test_a.py"])
    changed = NOT_THE_WATCHERS[name]
    issue = _issue(40, "test/test_a.py::test_one")
    gh.issues = [{**issue, **changed} if changed is not None else "issue"]  # type: ignore[list-item]

    assert _pass(tmp_path) == 0

    assert _notes(tmp_path, "main_red") == []
    assert _linux(tmp_path) == {6: (False, False, True)}


def test_a_listing_that_is_not_a_list_labels_nothing(gh: FakeGh, tmp_path: Path) -> None:
    failed_run(gh, 6, 8, ["test/test_a.py"])
    gh.issues = {"message": "Not Found"}  # type: ignore[assignment]

    assert _pass(tmp_path) == 0

    assert _linux(tmp_path) == {6: (False, False, True)}


def test_a_span_keeps_every_file_a_pass_saw_the_issue_name(gh: FakeGh, tmp_path: Path) -> None:
    failed_run(gh, 6, 8, ["test/test_a.py", "test/test_c.py"])
    gh.issues = [_issue(40, "test/test_a.py::test_one")]
    assert _pass(tmp_path, "--window-days", "0") == 0

    # The watcher rewrote the list on a later red run of main, then closed the issue.
    gh.issues = [_issue(40, "test/test_c.py::test_one", closed=_day(18, 10))]
    assert _pass(tmp_path, "--window-days", "0", now=NOW + timedelta(hours=1)) == 0

    (span,) = _notes(tmp_path, "main_red")
    assert span["files"] == ["test/test_a.py", "test/test_c.py"]
    assert span["closed"] == _day(18, 10)
    assert "Misses caused by the diff: 0." in summary(tmp_path)


# -- SETTLED ------------------------------------------------------------------


def _twenty_trials(gh: FakeGh) -> None:
    for run_id in range(101, 121):
        win_run(gh, run_id, run_id, [], date=_day(10))


def _two_misses_one_a_flake(gh: FakeGh) -> None:
    """Two Windows misses, and the older run on another branch that shows one is flaky."""
    win_run(gh, 201, 7, [MISS], date=_day(17))
    _rerun_green(gh, 201)
    win_run(gh, 202, 8, [MISS], date=_day(18, 1))
    win_run(gh, 203, 9, ["test/test_other.py"], date=_day(18, 2))


@pytest.mark.parametrize("gap_hours, held", [(1, True), (37, False)])
def test_a_pass_that_left_runs_unread_holds_the_last_settled_answer_for_36_hours(
    gh: FakeGh, tmp_path: Path, gap_hours: int, held: bool
) -> None:
    _twenty_trials(gh)
    assert _pass(tmp_path) == 0
    first = _status(tmp_path)
    assert first["settled"] is True and first["windows_selection_ok"] is True
    _two_misses_one_a_flake(gh)
    later = NOW + timedelta(hours=gap_hours)

    # Newest first, and two run attempts a pass: the flaky run is left unread.
    assert _pass(tmp_path, "--limit", "2", now=later) == 0

    status = _status(tmp_path)
    assert status["settled"] is False and status["diff_caused_windows_misses"] == 2
    assert status["windows_selection_ok"] is held
    assert status.get("held_from") == (causes.stamp(NOW) if held else None)
    assert summary(tmp_path).rstrip().splitlines()[-1] == json.dumps(status, sort_keys=True)

    # The next pass reaches it: one miss is a flake, and the pass speaks for itself.
    assert _pass(tmp_path, now=later + timedelta(hours=1)) == 0
    status = _status(tmp_path)
    assert status["settled"] is True and "held_from" not in status
    assert status["diff_caused_windows_misses"] == 1 and status["windows_selection_ok"] is True
    assert [note["settled"] for note in _notes(tmp_path, "pass")] == [True, False, True]


def test_a_settled_pass_over_the_bar_turns_selection_off_and_is_held_too(
    gh: FakeGh, tmp_path: Path
) -> None:
    _twenty_trials(gh)
    win_run(gh, 202, 8, [MISS], date=_day(18, 1))
    win_run(gh, 203, 9, ["test/test_other.py"], date=_day(18, 2))
    assert _pass(tmp_path) == 0
    assert _status(tmp_path)["settled"] is True
    assert _status(tmp_path)["windows_selection_ok"] is False

    win_run(gh, 204, 10, [], date=_day(18, 3))
    win_run(gh, 205, 11, [], date=_day(18, 4))
    assert _pass(tmp_path, "--limit", "1", now=NOW + timedelta(hours=1)) == 0

    status = _status(tmp_path)
    assert status["settled"] is False and status["windows_selection_ok"] is False


def test_a_pass_a_failed_read_ended_is_not_settled(
    gh: FakeGh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _twenty_trials(gh)
    assert _pass(tmp_path) == 0
    win_run(gh, 202, 8, [MISS], date=_day(18, 1))

    def no_listing(args: list[str], root: Path) -> Any:
        if "/workflows/ci.yml/runs?" in args[1]:
            raise subprocess.CalledProcessError(1, args)
        return gh.gh_json(args, root)

    monkeypatch.setattr(shadow, "_gh_json", no_listing)
    assert _pass(tmp_path, now=NOW + timedelta(hours=1)) == ledger.EXIT_NOT_RECORDED

    assert [(n["settled"], n["ok"]) for n in _notes(tmp_path, "pass")] == [
        (True, True),
        (False, None),
    ]


def test_an_unreadable_run_leaves_the_pass_unsettled(gh: FakeGh, tmp_path: Path) -> None:
    win_run(gh, 202, 8, [MISS], date=_day(18, 1))
    gh.unreadable.add(2023)

    assert _pass(tmp_path) == 0

    assert [note["settled"] for note in _notes(tmp_path, "pass")] == [False]


def test_the_answer_a_pass_keeps_is_of_the_whole_ledger_not_of_the_days_it_listed(
    gh: FakeGh, tmp_path: Path
) -> None:
    # Two misses three days old, on two branches and two files, and twenty trials a day old.
    win_run(gh, 201, 7, [MISS], date=_day(16))
    win_run(gh, 202, 8, ["test/test_other.py"], date=_day(16, 1))
    for run_id in range(301, 321):
        win_run(gh, run_id, run_id, [], date=_day(18))
    assert _pass(tmp_path, now=NOW - timedelta(hours=2)) == 0

    # The nightly pass lists two days: its own report holds neither miss.
    assert _pass(tmp_path, "--since", "2026-11-17") == 0

    assert "Windows misses caused by the diff: 0; on fork runs, not counted: 0." in summary(
        tmp_path
    )
    kept = [(note["settled"], note["ok"]) for note in _notes(tmp_path, "pass")]
    assert kept == [(True, False), (True, False)]
    # A later pass that leaves a run unread holds that answer: both misses are inside 14 days.
    win_run(gh, 401, 9, [], date=_day(18, 5))
    win_run(gh, 402, 10, [], date=_day(18, 6))
    assert _pass(tmp_path, "--limit", "1", now=NOW + timedelta(hours=1)) == 0
    status = _status(tmp_path)
    assert (status["settled"], status["windows_selection_ok"]) == (False, False)
    assert status["held_from"] == causes.stamp(NOW) and status["diff_caused_windows_misses"] == 2


def test_a_run_the_ledger_already_holds_does_not_leave_a_full_pass_unsettled(
    gh: FakeGh, tmp_path: Path
) -> None:
    for run_id in range(101, 120):
        win_run(gh, run_id, run_id, [], date=_day(10))
    assert _pass(tmp_path) == 0
    first = _status(tmp_path)
    assert (first["settled"], first["windows_selection_ok"], first["trials"]) == (True, False, 19)
    win_run(gh, 120, 120, [], date=_day(18))

    # The limit is reached at the twentieth trial; every older run is in the ledger.
    assert _pass(tmp_path, "--limit", "1", now=NOW + timedelta(hours=1)) == 0

    status = _status(tmp_path)
    assert (status["settled"], status["windows_selection_ok"], status["trials"]) == (True, True, 20)
    assert "held_from" not in status


def test_a_listing_github_cut_short_leaves_the_pass_unsettled(
    gh: FakeGh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ledger, "MAX_LISTED_RUNS", 2)
    for run_id in range(1, 5):
        win_run(gh, run_id, run_id, [], date=_day(18))

    # With no `--since` there is no window to halve.
    assert _pass(tmp_path) == 0

    assert [note["settled"] for note in _notes(tmp_path, "pass")] == [False]


def test_a_report_reads_the_notes_and_adds_none(gh: FakeGh, tmp_path: Path) -> None:
    _twenty_trials(gh)
    assert _pass(tmp_path) == 0
    held = (tmp_path / "notes.jsonl").read_bytes()
    gh.calls.clear()

    assert _pass(tmp_path, "--limit", "0", now=NOW + timedelta(hours=1)) == 0

    assert gh.calls == [] and (tmp_path / "notes.jsonl").read_bytes() == held
    assert _status(tmp_path)["settled"] is True


def test_without_notes_the_status_line_is_judged_on_the_rows_alone(
    gh: FakeGh, tmp_path: Path
) -> None:
    _twenty_trials(gh)

    assert run_main(tmp_path, "--windows-status-out", str(tmp_path / "status.json")) == 0

    assert "settled" not in _status(tmp_path) and not (tmp_path / "notes.jsonl").exists()


def _passes(*held: tuple[float, bool, bool | None]) -> list[dict[str, Any]]:
    return [
        causes.pass_note(NOW - timedelta(hours=hours), settled, ok) for hours, settled, ok in held
    ]


@pytest.mark.parametrize(
    "held, fresh, expected",
    [
        # No pass has ever said whether it settled: judged on the rows.
        ((), False, (False, False, False)),
        ((), True, (False, True, False)),
        # The newest pass settled: its own answer.
        (((40, True, True), (1, True, False)), False, (True, False, False)),
        # Unsettled: the newest settled answer of the last 36 hours, either way.
        (((30, True, True), (1, False, False)), False, (False, True, True)),
        (((30, True, False), (1, False, True)), True, (False, False, True)),
        (((35, True, False), (30, True, True), (1, False, None)), False, (False, True, True)),
        # None that recent, or none that reached an answer: judged on the rows.
        (((37, True, True), (1, False, False)), False, (False, False, False)),
        (((30, False, None), (1, False, None)), False, (False, False, False)),
    ],
)
def test_settle_holds_only_a_settled_answer_of_the_last_36_hours(
    held: tuple[tuple[float, bool, bool | None], ...],
    fresh: bool,
    expected: tuple[bool, bool, bool],
) -> None:
    status = causes.settle({"windows_selection_ok": fresh, "bar": 2}, _passes(*held), NOW)

    assert (status["settled"], status["windows_selection_ok"], "held_from" in status) == expected
    assert status["bar"] == 2


# -- DATA ---------------------------------------------------------------------

SPAN = {
    "kind": "main_red",
    "issue": 40,
    "first_run": 5,
    "closed": None,
    "files": ["test/test_a.py"],
    "date": _day(18),
}
BAD_NOTES = {
    "not json": b"<<<<<<<\n",
    "not text": b"\xff\xfe\x00\n",
    "a list": b"[1, 2]\n",
    "another kind": json.dumps({**SPAN, "kind": "run"}).encode() + b"\n",
    "no date": json.dumps({k: v for k, v in SPAN.items() if k != "date"}).encode() + b"\n",
    "a date that is no day": json.dumps({**SPAN, "date": "2026-13-45T00:00:00Z"}).encode() + b"\n",
    "files that are no list": json.dumps({**SPAN, "files": "test/test_a.py"}).encode() + b"\n",
    "a file that is no test path": json.dumps({**SPAN, "files": ["a|b.py"]}).encode() + b"\n",
    "a first run that is no count": json.dumps({**SPAN, "first_run": True}).encode() + b"\n",
    "a pass that says nothing": json.dumps({"kind": "pass", "date": _day(18)}).encode() + b"\n",
    "a good line then a bad one": json.dumps(SPAN).encode() + b"\n{}\n",
}


@pytest.mark.parametrize("name", sorted(BAD_NOTES))
def test_notes_that_do_not_read_as_notes_label_nothing_and_settle_nothing(
    gh: FakeGh, tmp_path: Path, name: str
) -> None:
    failed_run(gh, 6, 8, ["test/test_a.py"])
    assert run_main(tmp_path) == 0
    (tmp_path / "notes.jsonl").write_bytes(BAD_NOTES[name])

    assert causes.read_notes(tmp_path / "notes.jsonl") == []
    assert _pass(tmp_path, "--limit", "0") == 0

    assert "Misses caused by the diff: 1." in summary(tmp_path)
    assert _status(tmp_path)["settled"] is False


def test_notes_over_the_size_limit_are_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "notes.jsonl"
    causes.write_notes(path, [SPAN])
    assert causes.read_notes(path) == [SPAN]

    monkeypatch.setattr(causes, "MAX_NOTES_BYTES", path.stat().st_size - 1)

    assert causes.read_notes(path) == []


def test_old_spans_and_old_pass_notes_leave_the_notes() -> None:
    old_span = {**SPAN, "issue": 39, "closed": causes.stamp(NOW - timedelta(days=91))}
    # Open for longer than a span is kept: it leaves once no pass has listed it that long.
    open_span = {**SPAN, "issue": 38, "date": causes.stamp(NOW - timedelta(days=91))}
    kept_span = {**SPAN, "closed": causes.stamp(NOW - timedelta(days=89))}
    old_pass, kept_pass = _passes((24 * 8, True, True), (24 * 6, True, True))

    merged = causes.merge_notes([old_span, open_span, kept_span, old_pass], [kept_pass], NOW)

    assert merged == [kept_span, kept_pass]
    assert all(causes.valid_note(note) for note in merged)


def test_every_note_a_pass_writes_reads_back(gh: FakeGh, tmp_path: Path) -> None:
    failed_run(gh, 6, 8, ["test/test_a.py"])
    gh.issues = [
        _issue(40, "test/test_a.py::test_one", closed=_day(18, 10)),
        _issue(41, "a|b`c.py::test_one"),
        _issue(42, "test/" + "a" * 400 + ".py::test_one"),
    ]

    assert _pass(tmp_path) == 0

    text = (tmp_path / "notes.jsonl").read_text(encoding="utf-8")
    assert all(causes.valid_note(json.loads(line)) for line in text.splitlines())
    # A name that is no test path names no file: the span stays, and labels nothing.
    assert [(s["issue"], s["files"]) for s in _notes(tmp_path, "main_red")] == [
        (40, ["test/test_a.py"]),
        (41, []),
        (42, []),
    ]
