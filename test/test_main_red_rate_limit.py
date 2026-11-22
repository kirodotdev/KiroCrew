"""The main-red watcher records a red run of main when the API rate limit is spent.

The job's token draws on the installation rate limit every workflow of the
repository shares, and a red main is when the fleet spends it. A watcher that
ends on the refused call files no issue, and pull requests keep failing on
tests they did not break. Two properties are pinned, with the
harness of ``test_main_red_watch.py`` (the step run for real, ``gh`` stubbed,
behind the job's own rate-limit wrapper, a fixed clock and a ``sleep`` that
records and stands in for the reset; nothing sleeps):

* WAITING -- a refusal on the label call, on the issue lookup or on a read of
  the failed jobs is waited out once and retried, and the issue is opened or
  updated. A reset past the job's wait budget is not waited for: the run fails
  having written nothing, so it files no duplicate and closes nothing.
* REPLAY -- the next red run that names a test counts the red runs before it
  that no issue records, under the rules a live run is held to, so the
  issue names the first red commit and the whole streak.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from main_red_rate_limit_harness import RESET_FAR
from main_red_rate_limit_harness import sleeps as _sleeps
from test_main_red_watch import (  # noqa: F401 - jq_instrument is the fixture _needs_posix uses
    GREEN_JOBS,
    SHA,
    _calls,
    _jobs,
    _needs_posix,
    _notes,
    _open_issue,
    _run_row,
    _run_step,
    _wrote,
    jq_instrument,
    mod,
    prev_mod,
)

REPO_URL = "https://github.com/example/repo"
BREAK = "test/test_break.py::test_it"
OTHER = "test/test_elsewhere.py::test_other"
# One test red on a Linux and on a Windows shard: the shape of a break.
TWO_JOBS = _jobs(
    (11, "Backend Tests (3.12, 3)", "failure"), (14, "Backend Tests (Windows) (3)", "failure")
)
TWO_NOTES = {11: _notes(BREAK), 14: _notes(BREAK)}
ONE_JOB = _jobs((11, "Backend Tests (3.12, 3)", "failure"))
# The reset is 10 minutes off, inside the job's wait budget; the wrapper adds 5 s.
SOON = 600
WAITED = [SOON + 5]
EARLIER_SHA = "b" * 40


def _earlier(number: int, run_id: int, conclusion: str = "failure", sha: str = EARLIER_SHA) -> dict:
    return _run_row(number, conclusion, run_id, head_sha=sha)


def _state(stub: Path, name: str) -> dict[str, Any]:
    state = mod.parse_state((stub / name).read_text(encoding="utf-8"))
    assert state is not None
    return state


def _read_runs(stub: Path) -> list[str]:
    """The ids of the earlier runs whose jobs were read."""
    marks = [c.split("/actions/runs/")[1].split("/")[0] for c in _calls(stub) if "/jobs?" in c]
    return [run_id for run_id in marks if run_id != "1000"]


# ── Waiting: a refusal inside the budget delays the issue, never loses it ────


@_needs_posix
def test_a_limit_spent_from_the_first_call_is_waited_out_once_and_the_issue_is_filed(
    tmp_path: Path,
) -> None:
    """The label call is refused first; the run waits there and files a labelled issue."""
    out, stub = _run_step(
        tmp_path, jobs=TWO_JOBS, logs={}, annotations=TWO_NOTES, spent=("",), reset_in=SOON
    )
    assert out.returncode == 0, out.stderr
    assert _sleeps(stub) == WAITED
    refused = (stub / "refused.log").read_text(encoding="utf-8").splitlines()
    assert len(refused) == 1 and refused[0].startswith("label create main-red")
    (created,) = _wrote(stub, "create")
    assert "--label main-red" in created
    assert "Could not ensure the main-red label" not in out.stdout
    assert list(_state(stub, "edit-77.md")["failures"]) == [BREAK]


@_needs_posix
def test_a_refused_issue_lookup_is_waited_out_and_the_open_issue_is_updated(
    tmp_path: Path,
) -> None:
    out, stub = _run_step(
        tmp_path,
        jobs=TWO_JOBS,
        logs={},
        annotations=TWO_NOTES,
        issues=[_open_issue()],
        spent=("issue list",),
        reset_in=SOON,
    )
    assert out.returncode == 0, out.stderr
    assert _sleeps(stub) == WAITED
    assert _wrote(stub, "create") == []
    assert _state(stub, "edit-5.md")["streak"] == 2


@_needs_posix
@pytest.mark.parametrize(
    "call",
    ["api repos/example/repo/actions/runs/1000/jobs", "api repos/example/repo/check-runs/11/"],
    ids=["job-list", "annotations"],
)
def test_a_refused_read_of_the_failed_jobs_is_waited_out_and_the_issue_is_filed(
    tmp_path: Path, call: str
) -> None:
    out, stub = _run_step(
        tmp_path, jobs=TWO_JOBS, logs={}, annotations=TWO_NOTES, spent=(call,), reset_in=SOON
    )
    assert out.returncode == 0, out.stderr
    assert _sleeps(stub) == WAITED
    assert len(_wrote(stub, "create")) == 1
    assert "The API rate limit of this token is used up" not in out.stdout
    state = _state(stub, "edit-77.md")
    assert (list(state["failures"]), state["streak"]) == ([BREAK], 1)


@_needs_posix
def test_a_green_run_waits_out_a_refused_lookup_and_closes_the_issue(tmp_path: Path) -> None:
    out, stub = _run_step(
        tmp_path,
        jobs=GREEN_JOBS,
        conclusion="success",
        issues=[_open_issue()],
        spent=("issue list",),
        reset_in=SOON,
    )
    assert out.returncode == 0, out.stderr
    assert _sleeps(stub) == WAITED
    assert len(_wrote(stub, "comment")) == 1 and len(_wrote(stub, "close")) == 1


@_needs_posix
@pytest.mark.parametrize("conclusion", ["failure", "success"])
def test_a_reset_past_the_budget_is_not_waited_for_and_nothing_is_written(
    tmp_path: Path, conclusion: str
) -> None:
    """No duplicate and no close on a run that could not be read; the run is red."""
    out, stub = _run_step(
        tmp_path,
        jobs=TWO_JOBS if conclusion == "failure" else GREEN_JOBS,
        logs={},
        annotations=TWO_NOTES,
        conclusion=conclusion,
        issues=[_open_issue()],
        spent=("",),
    )
    assert out.returncode == 1
    assert _sleeps(stub) == []
    assert _wrote(stub, "create", "edit", "comment", "close") == []
    assert f"resets in {RESET_FAR + 5}s, past what is left of this job's" in out.stderr


# ── Replay: the next red run counts the red runs no issue records ─────────


def test_the_runs_to_replay_are_the_red_ones_back_to_a_green_run_or_the_record() -> None:
    rows = [
        _earlier(49, 990),
        _earlier(48, 980, "cancelled"),
        _earlier(47, 970, "timed_out"),
        _earlier(46, 960),
        _earlier(45, 950, "success"),
        _earlier(44, 940),
    ]
    listing = {"workflow_runs": rows}
    found, capped = prev_mod.unfinished(listing, 50, None)
    assert ([row["id"] for row in found], capped) == ([960, 970, 990], False)
    # The record of an open issue holds run 960: the runs after it are the ones to count.
    record = mod.next_state(None, {BREAK: ""}, SHA, 960, f"{REPO_URL}/actions/runs/960")
    found, capped = prev_mod.unfinished(listing, 50, record)
    assert ([row["id"] for row in found], capped) == ([970, 990], False)
    assert prev_mod.unfinished(listing, 46, None) == ([], False)
    assert prev_mod.unfinished({"workflow_runs": "x"}, 50, None) == ([], False)


def test_the_walk_back_stops_after_five_runs_and_says_so() -> None:
    rows = [_earlier(40 + n, 900 + n) for n in range(8)]
    found, capped = prev_mod.unfinished({"workflow_runs": rows}, 50, None)
    assert ([row["id"] for row in found], capped) == ([903, 904, 905, 906, 907], True)
    found, capped = prev_mod.unfinished({"workflow_runs": rows[3:]}, 50, None)
    assert (len(found), capped) == (5, False)


def test_a_replayed_run_counts_only_as_a_live_one_would() -> None:
    two = [{BREAK: "x"}, {BREAK: "x"}]
    one = [{BREAK: "x"}]
    elsewhere = [{OTHER: "y"}]
    first, second, third = _earlier(47, 970), _earlier(48, 980), _earlier(49, 990)

    def counted(*runs: tuple[dict, list], record: dict | None = None) -> list[int]:
        return [row["id"] for row in prev_mod.replay(record, list(runs), "example/repo")[1]]

    # No record: a test red in two jobs opens one, a test red in one job does not.
    assert counted((first, two)) == [970]
    assert counted((first, one)) == []
    # The same test file red in one job of two runs in a row opens it at the second.
    assert counted((first, one), (second, one)) == [980]
    assert counted((first, one), (second, elsewhere)) == []
    # Once a record is open, every run that names a test adds to it; one that names none does not.
    assert counted((first, two), (second, elsewhere), (third, [])) == [970, 980]
    record = mod.next_state(None, {BREAK: ""}, SHA, 960, f"{REPO_URL}/actions/runs/960")
    state, runs = prev_mod.replay(record, [(first, elsewhere), (second, one)], "example/repo")
    assert state is not None and [row["id"] for row in runs] == [970, 980]
    assert (state["streak"], state["first_run_id"], state["last_run_id"]) == (3, 960, 980)
    assert list(state["failures"]) == [BREAK]


@_needs_posix
def test_a_red_run_no_watcher_recorded_is_named_first_red_by_the_next_red_run(
    tmp_path: Path,
) -> None:
    """The first red run of a break went unrecorded; the second names it and counts both."""
    out, stub = _run_step(
        tmp_path,
        jobs=TWO_JOBS,
        logs={},
        annotations={**TWO_NOTES, 91: _notes(BREAK), 94: _notes(BREAK)},
        runs=[_run_row(50, "failure", 1000), _earlier(48, 990), _earlier(47, 980, "success")],
        past_jobs={
            990: _jobs(
                (91, "Backend Tests (3.12, 3)", "failure"),
                (94, "Backend Tests (Windows) (3)", "failure"),
            )
        },
    )
    assert out.returncode == 0, out.stderr
    assert len(_wrote(stub, "create")) == 1
    said = (
        "::notice::1 earlier red run(s) of main that no main-red issue records are counted now"
        f" ({REPO_URL}/actions/runs/990); first red is {EARLIER_SHA[:12]}."
    )
    assert said in out.stdout
    assert said.removeprefix("::notice::") in (stub / "summary.md").read_text(encoding="utf-8")
    state = _state(stub, "edit-77.md")
    assert (state["first_sha"], state["first_run_id"]) == (EARLIER_SHA[:12], 990)
    assert (state["last_sha"], state["last_run_id"], state["streak"]) == (SHA[:12], 1000, 2)
    assert f"{REPO_URL}/actions/runs/990" in (stub / "edit-77.md").read_text(encoding="utf-8")
    assert _read_runs(stub) == ["990"]


@_needs_posix
def test_a_red_run_missed_while_the_issue_was_open_adds_to_its_streak(tmp_path: Path) -> None:
    """The record holds run 980; run 990 after it was red and unrecorded."""
    out, stub = _run_step(
        tmp_path,
        jobs=TWO_JOBS,
        logs={},
        annotations={**TWO_NOTES, 91: _notes(OTHER)},
        issues=[_open_issue(run_id=980)],
        runs=[
            _run_row(50, "failure", 1000),
            _earlier(49, 990),
            _earlier(48, 980),
            _earlier(47, 970),
        ],
        past_jobs={990: _jobs((91, "Backend Tests (3.12, 3)", "failure"))},
    )
    assert out.returncode == 0, out.stderr
    assert _wrote(stub, "create") == []
    state = _state(stub, "edit-5.md")
    assert (state["first_sha"], state["first_run_id"], state["streak"]) == ("a" * 12, 980, 3)
    assert list(state["failures"]) == [BREAK]
    assert _read_runs(stub) == ["990"]


@_needs_posix
def test_what_the_replay_counts_opens_no_issue_for_a_run_red_in_one_job(
    tmp_path: Path,
) -> None:
    """The earlier run was red in two jobs and unrecorded; this one is red in one, elsewhere.

    The replay counts the earlier run, and this run still opens nothing: an
    issue opens by this run's own failures, never by what the replay counted.
    """
    past = {
        990: _jobs(
            (91, "Backend Tests (3.12, 3)", "failure"),
            (94, "Backend Tests (Windows) (3)", "failure"),
        )
    }
    runs = [_run_row(50, "failure", 1000), _earlier(49, 990)]
    notes = {11: _notes(OTHER), 91: _notes(BREAK), 94: _notes(BREAK)}
    out, stub = _run_step(
        tmp_path, jobs=ONE_JOB, logs={}, annotations=notes, runs=runs, past_jobs=past
    )
    assert out.returncode == 0, out.stderr
    assert "not opening an issue for a failure seen in one job" in out.stdout
    assert "are counted now" not in out.stdout
    assert _wrote(stub, "create", "edit", "comment", "close") == []
    # Red in one job on the earlier run's test file: the look back opens the
    # issue, and the replayed record names the earlier run first red.
    out, stub = _run_step(
        tmp_path / "same-file",
        jobs=ONE_JOB,
        logs={},
        annotations={**notes, 11: _notes(f"{BREAK}_again")},
        runs=runs,
        past_jobs=past,
    )
    assert out.returncode == 0, out.stderr
    assert "was red on the same test file (test/test_break.py), so an issue is opened" in out.stdout
    assert "are counted now" in out.stdout
    state = _state(stub, "edit-77.md")
    assert (state["first_run_id"], state["streak"]) == (990, 2)


@_needs_posix
def test_an_unrecorded_run_red_in_one_job_does_not_change_when_an_issue_opens(
    tmp_path: Path,
) -> None:
    """Red in one job, on another test file than this run: neither run opens an issue."""
    past = {990: _jobs((91, "Backend Tests (3.12, 3)", "failure"))}
    runs = [_run_row(50, "failure", 1000), _earlier(49, 990)]
    notes = {11: _notes(OTHER), 91: _notes(BREAK)}
    out, stub = _run_step(
        tmp_path, jobs=ONE_JOB, logs={}, annotations=notes, runs=runs, past_jobs=past
    )
    assert out.returncode == 0, out.stderr
    assert "not opening an issue for a failure seen in one job" in out.stdout
    assert "was not red on the same test file" in out.stdout
    assert _wrote(stub, "create", "edit", "comment", "close") == []
    # The earlier run was read once, for the replay and the look back alike.
    assert _read_runs(stub) == ["990"]
    # This run red in two jobs: the issue opens at this run, as with no earlier run.
    out, stub = _run_step(
        tmp_path / "two",
        jobs=TWO_JOBS,
        logs={},
        annotations={**TWO_NOTES, 91: _notes(BREAK)},
        runs=runs,
        past_jobs=past,
    )
    assert out.returncode == 0, out.stderr
    state = _state(stub, "edit-77.md")
    assert (state["first_run_id"], state["streak"]) == (1000, 1)


@_needs_posix
def test_a_green_run_before_this_one_ends_the_walk_back(tmp_path: Path) -> None:
    out, stub = _run_step(
        tmp_path,
        jobs=TWO_JOBS,
        logs={},
        annotations=TWO_NOTES,
        runs=[_run_row(50, "failure", 1000), _earlier(49, 990, "success"), _earlier(48, 980)],
    )
    assert out.returncode == 0, out.stderr
    assert _read_runs(stub) == []
    assert _state(stub, "edit-77.md")["first_run_id"] == 1000


@_needs_posix
def test_a_replay_read_the_limit_refuses_inside_the_budget_is_waited_out(tmp_path: Path) -> None:
    out, stub = _run_step(
        tmp_path,
        jobs=TWO_JOBS,
        logs={},
        annotations={**TWO_NOTES, 91: _notes(BREAK), 94: _notes(BREAK)},
        runs=[_run_row(50, "failure", 1000), _earlier(49, 990)],
        past_jobs={
            990: _jobs(
                (91, "Backend Tests (3.12, 3)", "failure"),
                (94, "Backend Tests (Windows) (3)", "failure"),
            )
        },
        spent=("api repos/example/repo/check-runs/94/",),
        reset_in=SOON,
    )
    assert out.returncode == 0, out.stderr
    assert _sleeps(stub) == WAITED
    assert _state(stub, "edit-77.md")["first_run_id"] == 990


@_needs_posix
def test_a_replay_that_cannot_read_a_run_counts_none_and_the_run_is_still_recorded(
    tmp_path: Path,
) -> None:
    """Two unrecorded runs, the newer unreadable past the budget: neither is counted."""
    two_legs = _jobs(
        (91, "Backend Tests (3.12, 3)", "failure"), (94, "Backend Tests (Windows) (3)", "failure")
    )
    out, stub = _run_step(
        tmp_path,
        jobs=TWO_JOBS,
        logs={},
        annotations={**TWO_NOTES, 91: _notes(BREAK), 94: _notes(BREAK)},
        runs=[_run_row(50, "failure", 1000), _earlier(49, 990), _earlier(48, 980)],
        past_jobs={980: two_legs, 990: _jobs((97, "Backend Tests (3.12, 3)", "failure"))},
        rate_limited=(97,),
    )
    assert out.returncode == 0, out.stderr
    assert _sleeps(stub) == []
    said = (
        "The API rate limit of this token is used up, so the failing tests of"
        f" {REPO_URL}/actions/runs/990 could not be read;"
        " the red runs before this one are not replayed."
    )
    assert said in out.stdout and said in (stub / "summary.md").read_text(encoding="utf-8")
    state = _state(stub, "edit-77.md")
    assert (state["first_run_id"], state["streak"]) == (1000, 1)


@_needs_posix
def test_at_most_five_earlier_runs_are_replayed_and_the_notice_says_so(tmp_path: Path) -> None:
    earlier = [_earlier(40 + n, 900 + n) for n in range(1, 8)]
    leg = _jobs(
        (91, "Backend Tests (3.12, 3)", "failure"), (94, "Backend Tests (3.12, 4)", "failure")
    )
    out, stub = _run_step(
        tmp_path,
        jobs=TWO_JOBS,
        logs={},
        annotations={**TWO_NOTES, 91: _notes(BREAK), 94: _notes(BREAK)},
        runs=[_run_row(50, "failure", 1000), *earlier],
        past_jobs={row["id"]: leg for row in earlier},
    )
    assert out.returncode == 0, out.stderr
    assert _read_runs(stub) == ["903", "904", "905", "906", "907"]
    assert "Only the 5 runs before this one were read." in out.stdout
    state = _state(stub, "edit-77.md")
    assert (state["first_run_id"], state["streak"]) == (903, 6)


@_needs_posix
def test_a_green_run_replays_nothing(tmp_path: Path) -> None:
    """What a replay would add to the issue, the same green run would close."""
    out, stub = _run_step(
        tmp_path,
        jobs=GREEN_JOBS,
        conclusion="success",
        runs=[_run_row(50, "success", 1000), _earlier(49, 990)],
    )
    assert out.returncode == 0, out.stderr
    assert not [c for c in _calls(stub) if "/actions/workflows/" in c]
    assert _read_runs(stub) == []
    assert _wrote(stub, "create", "edit", "comment", "close") == []


# ── Replay: runs a closed issue recorded are not unrecorded ──────────────────

TWO_LEGS_BEFORE = _jobs(
    (91, "Backend Tests (3.12, 3)", "failure"), (94, "Backend Tests (Windows) (3)", "failure")
)


def _closed_issue(run_id: int, number: int = 4) -> dict:
    """An issue the watcher filed, closed while main was still red; its record ends at run_id."""
    return {**_open_issue(number=number, run_id=run_id), "state": "CLOSED"}


def test_the_newest_run_a_closed_watcher_issue_records_bounds_the_walk_back() -> None:
    rows = [_earlier(49, 990), _earlier(48, 980), _earlier(47, 970)]
    forged = {**_closed_issue(990), "author": {"login": "someone", "is_bot": False}}
    assert prev_mod.last_recorded([_closed_issue(970), _closed_issue(980), forged]) == 980
    assert prev_mod.last_recorded([forged, {"title": "x"}, 7]) == -1
    assert prev_mod.last_recorded(None) == -1
    found, _ = prev_mod.unfinished({"workflow_runs": rows}, 50, None, 980)
    assert [row["id"] for row in found] == [990]


@_needs_posix
def test_runs_a_closed_issue_recorded_are_not_replayed_and_open_no_issue(tmp_path: Path) -> None:
    """The issue was closed by a merged fix while main was red; the next run is red in one job.

    The two runs before it are in the closed record, so they are not counted and
    a failure seen in one job opens nothing, as with no earlier run.
    """
    out, stub = _run_step(
        tmp_path,
        jobs=ONE_JOB,
        logs={},
        annotations={11: _notes(OTHER), 91: _notes(BREAK), 94: _notes(BREAK)},
        runs=[_run_row(50, "failure", 1000), _earlier(49, 990), _earlier(48, 980)],
        past_jobs={980: TWO_LEGS_BEFORE, 990: TWO_LEGS_BEFORE},
        closed_issues=[_closed_issue(990)],
    )
    assert out.returncode == 0, out.stderr
    assert "are counted now" not in out.stdout
    assert "not opening an issue for a failure seen in one job" in out.stdout
    assert _wrote(stub, "create", "edit", "comment", "close") == []
    # Run 990 is read for the look back alone; run 980 is not read at all.
    assert _read_runs(stub) == ["990"]


@_needs_posix
def test_only_the_runs_after_a_closed_issues_record_are_replayed(tmp_path: Path) -> None:
    out, stub = _run_step(
        tmp_path,
        jobs=TWO_JOBS,
        logs={},
        annotations={**TWO_NOTES, 91: _notes(BREAK), 94: _notes(BREAK)},
        runs=[_run_row(50, "failure", 1000), _earlier(49, 990), _earlier(48, 980)],
        past_jobs={980: TWO_LEGS_BEFORE, 990: TWO_LEGS_BEFORE},
        closed_issues=[_closed_issue(980)],
    )
    assert out.returncode == 0, out.stderr
    assert _read_runs(stub) == ["990"]
    state = _state(stub, "edit-77.md")
    assert (state["first_run_id"], state["streak"]) == (990, 2)


@_needs_posix
def test_without_the_closed_issues_nothing_is_replayed_and_the_run_is_recorded(
    tmp_path: Path,
) -> None:
    """What a closed issue recorded cannot be told apart, so no earlier run is counted."""
    out, stub = _run_step(
        tmp_path,
        jobs=TWO_JOBS,
        logs={},
        annotations={**TWO_NOTES, 91: _notes(BREAK), 94: _notes(BREAK)},
        runs=[_run_row(50, "failure", 1000), _earlier(49, 990)],
        past_jobs={990: TWO_LEGS_BEFORE},
        closed_refused=True,
    )
    assert out.returncode == 0, out.stderr
    said = "the red runs before this one are not replayed."
    assert said in out.stdout and said in (stub / "summary.md").read_text(encoding="utf-8")
    assert _read_runs(stub) == []
    state = _state(stub, "edit-77.md")
    assert (state["first_run_id"], state["streak"]) == (1000, 1)


@_needs_posix
@pytest.mark.parametrize("label_refused", [False, True], ids=["label", "no-label"])
def test_a_closed_issue_filed_without_the_label_bounds_the_walk_back_too(
    tmp_path: Path, label_refused: bool
) -> None:
    """The labelled list misses an issue filed bare; the list of any label holds it."""
    out, stub = _run_step(
        tmp_path,
        jobs=TWO_JOBS,
        logs={},
        annotations={**TWO_NOTES, 91: _notes(BREAK), 94: _notes(BREAK)},
        runs=[_run_row(50, "failure", 1000), _earlier(49, 990), _earlier(48, 980)],
        past_jobs={980: TWO_LEGS_BEFORE, 990: TWO_LEGS_BEFORE},
        closed_issues=[],
        closed_unlabelled=[_closed_issue(980)],
        label_refused=label_refused,
    )
    assert out.returncode == 0, out.stderr
    listed = [c for c in _calls(stub) if "--state closed" in c]
    assert len(listed) == (1 if label_refused else 2)
    assert _read_runs(stub) == ["990"]
    state = _state(stub, "edit-77.md")
    assert (state["first_run_id"], state["streak"]) == (990, 2)


@_needs_posix
def test_a_first_red_run_after_a_green_one_does_not_read_the_closed_issues(tmp_path: Path) -> None:
    out, stub = _run_step(
        tmp_path,
        jobs=TWO_JOBS,
        logs={},
        annotations=TWO_NOTES,
        runs=[_run_row(50, "failure", 1000), _earlier(49, 990, "success")],
    )
    assert out.returncode == 0, out.stderr
    assert not [c for c in _calls(stub) if "--state closed" in c]
    assert len(_wrote(stub, "create")) == 1


# ── Replay: a suspected-red issue is not a record of main ────────────────────


def _suspected(run_id: int, number: int = 8) -> dict:
    """A `Main may be red` issue as the suspect workflow files it: its own title and marker.

    It is written by the same Actions token, and its record is shaped like the
    watcher's, so only the title prefix and the marker keep the two apart.
    """
    record = mod.next_state(None, {BREAK: "x"}, "c" * 40, run_id, f"{REPO_URL}/actions/runs/1")
    return {
        "number": number,
        "title": f"ci: Main may be red: {BREAK}",
        "body": f"### Main may be red (not confirmed)\n\n<!-- main-red-suspect-state"
        f" {mod.dump_state(record)} -->\n",
        "author": _open_issue()["author"],
    }


def test_a_suspected_red_issue_bounds_no_walk_back_and_is_not_the_watchers_issue() -> None:
    suspected = _suspected(990)
    assert prev_mod.last_recorded([suspected]) == -1
    assert mod.pick_issue([suspected]) is None


@_needs_posix
def test_a_suspected_red_issue_open_or_closed_changes_nothing_about_the_replay(
    tmp_path: Path,
) -> None:
    """One is open and one was closed: the watcher files its own issue and replays as without."""
    out, stub = _run_step(
        tmp_path,
        jobs=TWO_JOBS,
        logs={},
        annotations={**TWO_NOTES, 91: _notes(BREAK), 94: _notes(BREAK)},
        issues=[_suspected(995)],
        runs=[_run_row(50, "failure", 1000), _earlier(49, 990), _earlier(48, 980, "success")],
        past_jobs={990: TWO_LEGS_BEFORE},
        closed_issues=[],
        closed_unlabelled=[_suspected(990, number=7)],
    )
    assert out.returncode == 0, out.stderr
    assert len(_wrote(stub, "create")) == 1
    assert all(c.startswith("issue edit 77 ") for c in _wrote(stub, "edit"))
    assert not (stub / "edit-8.md").exists()
    assert _read_runs(stub) == ["990"]
    state = _state(stub, "edit-77.md")
    assert (state["first_run_id"], state["streak"]) == (990, 2)
