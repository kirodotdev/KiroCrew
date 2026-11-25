"""Contract tests for scripts/ci_test_selection_ledger_night.py and test-selection-ledger.yml.

Pinned:

* CHAIN -- ticks that follow each other extend one ledger: no row lost, none
  twice, a tick that kept out of a low pool between two that read does not
  break it, and the report is of the whole ledger, not of the days a pass lists.
* TRUST -- the ledger is restored only from this workflow's own successful
  scheduled or dispatched run on the default branch of this repository.
* REFUSE -- a failed read or a restored file that is not a ledger fails and
  uploads nothing; a pool under the reserve uploads nothing and does not fail;
  only a workflow that never kept a ledger starts an empty one unasked.
* WORKFLOW -- read-only, this repository only, never two passes at once,
  bounded passes in the quiet hours, and an upload only on the pass's word.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from ci_test_selection_ledger_fake import SKIP

from scripts import ci_test_selection_ledger as ledger
from scripts import ci_test_selection_ledger_night as night
from scripts import ci_test_selection_step as step

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOWS / night.WORKFLOW
REPO = "octo/crew"
BRANCH = "trunk"
TOKEN = "token-for-the-api"
BLOBS = "https://blobs.example.invalid/ledger/"
TODAY = datetime(2026, 11, 24, 5, 11, tzinfo=timezone.utc)
ENV = {"GITHUB_REPOSITORY": REPO, "DEFAULT_BRANCH": BRANCH, "GH_TOKEN": TOKEN}
# The calls it takes to read the busiest day measured, 1,240 pull-request runs.
BUSIEST_DAY_CALLS = 2250
# The REST requests an hour's pool holds, and the hours it is fullest (UTC).
POOL = 15000
QUIET_HOURS = range(5, 9)
# What a run of the workflow may hold besides a live archive (bytes).
NEVER, EXPIRED = "never uploaded", "expired"
# For each field of the rule, a run that uploaded the artifact and must not be restored from.
UNTRUSTED = {
    "another workflow": {"path": f".github/workflows/{step.WORKFLOW}"},
    "a branch": {"head_branch": "feature"},
    "a pull request": {"event": "pull_request"},
    "a failed pass": {"conclusion": "failure"},
    "a fork": {"head_repository": {"full_name": "fork/crew"}},
}


def _run(run_id: int, **changed: Any) -> dict[str, Any]:
    """A successful scheduled run of the workflow, as the API lists it."""
    return {
        "id": run_id,
        "path": f".github/workflows/{night.WORKFLOW}",
        "head_branch": BRANCH,
        "status": "completed",
        "conclusion": "success",
        "event": step.EVENTS[0],
        "created_at": "2026-11-23T06:41:00Z",
        "repository": {"full_name": REPO},
        "head_repository": {"full_name": REPO},
        **changed,
    }


def _row(run: int, day: datetime = TODAY) -> dict[str, Any]:
    row = {**SKIP, "run": run, "date": day.strftime("%Y-%m-%dT%H:%M:%SZ")}
    assert ledger.valid_row(row)
    return row


def _lines(rows: list[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(row) + "\n" for row in rows).encode("utf-8")


def _zip(members: dict[str, bytes]) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as zipped:
        for name, body in members.items():
            zipped.writestr(name, body)
    return data.getvalue()


def _ledger_zip(*rows: dict[str, Any]) -> bytes:
    return _zip({night.LEDGER_NAME: _lines(list(rows))})


class Api:
    """GitHub as the restore reads it: the workflow's runs, newest first, and what each holds."""

    def __init__(self, *held: tuple[dict[str, Any], bytes | str]) -> None:
        self.held = list(held)
        self.listing: Any = None
        self.more_runs = 0
        self.sent: list[tuple[str, str]] = []

    def __call__(self, url: str, token: str) -> tuple[int, dict[str, str], bytes]:
        self.sent.append((url, token))
        if url.startswith(BLOBS):
            return 200, {}, self._holds(int(url[len(BLOBS) :]))  # type: ignore[return-value]
        if "/actions/workflows/" in url:
            if self.listing is not None:
                return self.listing
            runs = [run for run, _holds in self.held]
            body = {"total_count": len(runs) + self.more_runs, "workflow_runs": runs}
            return 200, {}, json.dumps(body).encode()
        zipped = re.fullmatch(r".*/actions/artifacts/(\d+)/zip", url)
        if zipped:
            return 302, {"location": f"{BLOBS}{zipped.group(1)}"}, b""
        one = re.fullmatch(r".*/actions/runs/(\d+)", url)
        if one:
            run = next(run for run, _holds in self.held if run["id"] == int(one.group(1)))
            return 200, {}, json.dumps(run).encode()
        run_id = int(re.fullmatch(r".*/actions/runs/(\d+)/artifacts\?name=.*", url).group(1))  # type: ignore[union-attr]
        holds = self._holds(run_id)
        listed = []
        if holds != NEVER:
            listed.append(
                {
                    "id": run_id,
                    "name": night.ARTIFACT,
                    "expired": holds == EXPIRED,
                    "workflow_run": {"id": run_id},
                }
            )
        return 200, {}, json.dumps({"artifacts": listed}).encode()

    def _holds(self, run_id: int) -> bytes | str:
        return next(holds for run, holds in self.held if run["id"] == run_id)

    def reads(self) -> int:
        return len([url for url, _token in self.sent if url.startswith(step.API_URL)])


class Pass:
    """`ledger.main` for the tests: a pass that appends `fresh`, and the real report."""

    def __init__(self, fresh: list[dict[str, Any]] | None = None, status: int = 0) -> None:
        self.fresh, self.status = fresh or [], status
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> int:
        self.calls.append(argv)
        if argv[argv.index("--limit") + 1] == "0":
            # No request is made under `--limit 0`: the real script reads the file alone.
            return ledger.main(argv)
        if self.status != ledger.EXIT_REFUSED:
            ledger.append_rows(Path(argv[argv.index("--ledger") + 1]), self.fresh)
        return self.status


def _night(
    tmp_path: Path, api: Api, fresh: list[dict[str, Any]] | None = None, **env: str
) -> tuple[list[str], Path, Pass]:
    out = tmp_path / f"night-{len(list(tmp_path.glob('night-*')))}"
    out.mkdir()
    run = Pass(fresh)
    lines = night.night(out, {**ENV, **env}, api, run, TODAY, tmp_path, pool=lambda: None)
    return lines, out, run


def _tick(
    tmp_path: Path, api: Api, run: Pass, low: str | None = None, **env: str
) -> tuple[int, Path, dict[str, str], str]:
    """One run of the script as the workflow starts it: exit, directory, outputs, summary."""
    out = tmp_path / f"tick-{len(list(tmp_path.glob('tick-*')))}"
    output, summary = out.with_suffix(".output"), out.with_suffix(".md")
    files = {"GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)}
    status = night.main(
        ["--out", str(out)], {**ENV, **files, **env}, api, run, TODAY, pool=lambda: low
    )
    outputs = (
        dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
        if output.exists()
        else {}
    )
    return status, out, outputs, summary.read_text(encoding="utf-8")


def _uploaded(out: Path) -> bytes:
    """The artifact the workflow would upload from `out`: every file of the directory."""
    return _zip({path.name: path.read_bytes() for path in sorted(out.iterdir())})


def test_ticks_extend_one_ledger_through_one_that_kept_out_of_a_low_pool(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = TODAY - timedelta(days=night.SINCE_DAYS + 10)
    # What each tick reads; None is a tick the pool was too low for. The first
    # reads nothing and still leaves a ledger for the next to continue.
    ticks: list[list[dict[str, Any]] | None] = [
        [],
        [_row(1, old), _row(2, old)],
        None,
        None,
        [_row(3)],
        [],
    ]
    api = Api()
    held: list[dict[str, Any]] = []
    since_good = 0
    for number, fresh in enumerate(ticks):
        run = Pass(fresh, ledger.EXIT_REFUSED if fresh is None else 0)
        api.sent.clear()
        status, out, outputs, summary = _tick(tmp_path, api, run)
        printed = capsys.readouterr().out
        # A tick is green whether it read or kept out; only one that read uploads.
        assert status == 0
        if fresh is None:
            assert outputs == {} and "::error::" not in printed
            assert printed.count("::notice::") == 1 and "was not started" in printed
            assert summary.startswith(printed.split("::notice::", 1)[1].strip())
            api.held.insert(0, (_run(100 + number), NEVER))
            since_good += 1
            continue
        held += fresh
        assert outputs == {"upload": "true"}
        # No row lost, none twice, in the order they were read.
        assert ledger.read_ledger(out / night.LEDGER_NAME) == held
        if number == 0:
            assert summary.startswith("The ledger starts empty tonight: no run of this workflow")
            reads = 1
        else:
            # The run before the ones that kept out, found by looking back past them.
            assert summary.startswith(
                f"The ledger continues from run {100 + number - 1 - since_good} "
            )
            assert f"{len(held) - len(fresh)} rows" in summary.splitlines()[0]
            reads = 1 + since_good + 1 + 1
        assert api.reads() == reads
        collect, report = run.calls
        day = (TODAY - timedelta(days=night.SINCE_DAYS)).strftime("%Y-%m-%d")
        assert collect[collect.index("--since") + 1] == day
        # What the restore spent comes off what the pass may spend.
        assert collect[collect.index("--max-calls") + 1] == str(night.MAX_CALLS - reads)
        assert collect[collect.index("--reserve") + 1] == str(night.RESERVE)
        # The report lists no run and has no first day: it is of the whole ledger.
        assert "--since" not in report and "--max-calls" not in report
        assert f"Runs excluded: {len(held)}" in summary
        assert json.loads((out / night.RATES_NAME).read_text(encoding="utf-8"))
        # What this tick uploads is what a later one restores.
        api.held.insert(0, (_run(100 + number), _uploaded(out)))
        since_good = 0


def test_the_token_goes_to_the_api_alone_and_old_rows_are_dropped_and_counted(
    tmp_path: Path,
) -> None:
    archive = _zip({night.LEDGER_NAME: _lines([_row(1)]), night.SUMMARY_NAME: b"old"})
    api = Api((_run(70), archive))
    lines, out, _run_ = _night(tmp_path, api)
    assert lines[0] == "The ledger continues from run 70 (2026-11-23T06:41:00Z): 1 rows."
    assert api.reads() == 3 and len(api.sent) == len(set(api.sent)) == 4
    for url, token in api.sent:
        assert token == (TOKEN if url.startswith(step.API_URL + "/") else "")
    assert f"branch={BRANCH}" in api.sent[0][0] and night.WORKFLOW in api.sent[0][0]
    stale = _row(2, TODAY - timedelta(days=night.KEEP_DAYS + 1))
    edge = _row(3, TODAY - timedelta(days=night.KEEP_DAYS))
    lines, out, _run_ = _night(tmp_path, Api((_run(70), _ledger_zip(stale, edge))))
    assert f"1 rows, 1 older than {night.KEEP_DAYS} days dropped" in lines[0]
    assert ledger.read_ledger(out / night.LEDGER_NAME) == [edge]


@pytest.mark.parametrize("why", sorted(UNTRUSTED))
def test_a_run_that_is_not_this_workflows_own_is_never_restored_from(
    tmp_path: Path, why: str
) -> None:
    forged, good = _ledger_zip(_row(666)), _ledger_zip(_row(1))
    # Newer than the workflow's own run and holding the same artifact name: passed over.
    api = Api((_run(71, **UNTRUSTED[why]), forged), (_run(70), good))
    lines, out, _run_ = _night(tmp_path, api)
    assert "continues from run 70" in lines[0]
    assert ledger.read_ledger(out / night.LEDGER_NAME) == [_row(1)]
    assert not any("/runs/71/" in url or url.endswith("/71/zip") for url, _token in api.sent)
    # Named by hand, it is refused before its artifact is asked for.
    api = Api((_run(71, **UNTRUSTED[why]), forged), (_run(70), good))
    with pytest.raises(night.Refused, match="is not a successful scheduled or dispatched run"):
        _night(tmp_path, api, RESTORE_RUN="71")
    assert api.reads() == 1


def test_only_a_workflow_that_never_kept_a_ledger_starts_empty_unasked(tmp_path: Path) -> None:
    never = [(_run(90 - n), NEVER) for n in range(night.WALK_RUNS)]
    # Every successful run was looked at and none ever listed the artifact.
    for held in ([], never[:3], never):
        api = Api(*held)
        lines, out, _run_ = _night(tmp_path, api)
        assert lines[0].startswith("The ledger starts empty tonight: no run of this workflow")
        assert "Earlier days of data are not in it" in lines[0]
        assert api.reads() == 1 + len(held)
        assert ledger.read_ledger(out / night.LEDGER_NAME) == []
    # Runs further back were not looked at: one of them may hold the ledger.
    api = Api(*never)
    api.more_runs = 1
    with pytest.raises(night.Refused, match=f"none of the newest {night.WALK_RUNS} successful"):
        _night(tmp_path, api)
    # The walk is bounded: the listing and one read for each run looked at.
    assert api.reads() == 1 + night.WALK_RUNS
    # A ledger was kept and has expired: that is not a first night.
    api = Api((_run(71), NEVER), (_run(70), EXPIRED))
    with pytest.raises(night.Refused, match="one has expired.*restore_run.*start_empty"):
        _night(tmp_path, api)


def test_a_person_can_name_the_run_or_start_empty_and_the_summary_says_so(
    tmp_path: Path,
) -> None:
    api = Api((_run(72), _ledger_zip(_row(2))), (_run(70), _ledger_zip(_row(1))))
    # The listing is not read: the named run is, and it is held to the same rule.
    api.listing = (500, {}, b"")
    lines, out, _run_ = _night(tmp_path, api, RESTORE_RUN="70")
    assert "continues from run 70 (2026-11-23T06:41:00Z), named by hand (`restore_run`)" in lines[0]
    assert ledger.read_ledger(out / night.LEDGER_NAME) == [_row(1)]
    assert api.reads() == 3
    for bad in ("7x", "-1", "70; rm"):
        with pytest.raises(night.Refused, match="restore_run is not a run id"):
            _night(tmp_path, api, RESTORE_RUN=bad)
    # A named run with no live artifact is refused, not an empty start.
    for holds in (NEVER, EXPIRED):
        with pytest.raises(night.Refused, match="restore_run: run 70 holds no live"):
            _night(tmp_path, Api((_run(70), holds)), RESTORE_RUN="70")
    api = Api((_run(70), _ledger_zip(_row(1))))
    lines, out, _run_ = _night(tmp_path, api, START_EMPTY="true")
    assert lines[0] == "The ledger starts empty tonight: a person asked for it (`start_empty`)."
    assert api.sent == [] and ledger.read_ledger(out / night.LEDGER_NAME) == []
    assert (out / night.SUMMARY_NAME).read_text(encoding="utf-8").startswith(lines[0])


def _refusals() -> dict[str, tuple[Any, str]]:
    """Name -> (the API to restore from, a part of the one line that refuses)."""
    good = _lines([_row(1)])

    def listing(answer: Any) -> Api:
        api = Api((_run(70), _zip({night.LEDGER_NAME: good})))
        api.listing = answer
        return api

    def holding(archive: bytes) -> Api:
        return Api((_run(70), archive))

    return {
        "the listing fails": (listing((502, {}, b"")), "HTTP 502"),
        "the listing is not one": (listing((200, {}, b'{"runs": []}')), "did not return a list"),
        "not an archive": (holding(b"not a zip"), "unreadable archive"),
        "no ledger in the archive": (holding(_zip({night.SUMMARY_NAME: b"x"})), "does not hold"),
        "a line that is not a row": (
            holding(_zip({night.LEDGER_NAME: good + b'{"kind": "run"}\n'})),
            "not a ledger row",
        ),
        "a line that is not JSON": (
            holding(_zip({night.LEDGER_NAME: good + b"<<<<<<<\n"})),
            "not a ledger row",
        ),
        "not text": (holding(_zip({night.LEDGER_NAME: b"\xff\xfe\x00"})), "is not one"),
    }


@pytest.mark.parametrize("name", sorted(_refusals()))
def test_what_cannot_be_restored_fails_and_nothing_is_uploaded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    api, part = _refusals()[name]
    run = Pass([_row(5)])
    status, out, outputs, summary = _tick(tmp_path, api, run)
    # No pass ran on top of it, no ledger was left, and the upload has no word to go on.
    assert status == 1 and outputs == {} and run.calls == []
    assert not (out / night.LEDGER_NAME).exists()
    printed = capsys.readouterr().out
    assert printed.startswith("::error::") and part in printed and len(printed.splitlines()) == 1
    assert "nothing uploaded" in summary and part in summary


def test_a_ledger_over_the_size_limit_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _lines([_row(1), _row(2)])
    archive = _zip({night.LEDGER_NAME: rows})
    monkeypatch.setattr(night, "MAX_LEDGER_BYTES", len(rows) - 1)
    with pytest.raises(night.Refused, match=f"{night.LEDGER_NAME} is over"):
        _night(tmp_path, Api((_run(70), archive)))
    monkeypatch.setattr(night, "MAX_LEDGER_BYTES", len(rows))
    assert "2 rows" in _night(tmp_path, Api((_run(70), archive)))[0][0]
    monkeypatch.setattr(night, "MAX_ARCHIVE_BYTES", 10)
    with pytest.raises(night.Refused, match="the archive is over 10 bytes"):
        _night(tmp_path, Api((_run(70), archive)))


def test_a_cut_pass_keeps_its_rows_and_an_unknown_exit_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    held = (_run(70), _ledger_zip(_row(1)))
    # A pass that ended on a failed read keeps what it read, and the summary says so.
    status, out, outputs, summary = _tick(
        tmp_path, Api(held), Pass([_row(2)], ledger.EXIT_NOT_RECORDED)
    )
    assert status == 0 and outputs == {"upload": "true"}
    assert ledger.read_ledger(out / night.LEDGER_NAME) == [_row(1), _row(2)]
    assert "ended early on a failed read" in summary
    # A pool that falls under the reserve after the restore: the ledger's own check
    # ends the pass, and there is nothing new to report.
    run = Pass([_row(2)], ledger.EXIT_REFUSED)
    status, out, outputs, summary = _tick(tmp_path, Api(held), run)
    assert status == 0 and outputs == {} and len(run.calls) == 1
    assert not (out / night.SUMMARY_NAME).exists()
    # An exit the ledger does not document is not taken for a pass.
    status, out, outputs, summary = _tick(tmp_path, Api(held), Pass([], 1))
    assert status == 1 and outputs == {}
    capsys.readouterr()


def test_a_low_pool_is_met_before_the_restore_and_is_not_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    held = (_run(70), _ledger_zip(_row(1)))
    # The pool is read first: under the reserve, not one request is made, the restore's included.
    api, run = Api(held), Pass([_row(2)])
    status, out, outputs, summary = _tick(tmp_path, api, run, low="the REST pool has 10 requests")
    printed = capsys.readouterr().out
    assert status == 0 and outputs == {} and api.sent == [] and run.calls == []
    assert printed.count("::notice::") == 1 and "::error::" not in printed
    assert "was not started: the REST pool has 10 requests" in printed
    assert summary.strip() == printed.split("::notice::", 1)[1].strip()
    # A pool spent between that read and the restore refuses the restore's own request:
    # the pass kept out, as it would have a moment earlier. It is not a failed read.
    limited = (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1"}, b"")
    api, run = Api(held), Pass([_row(2)])
    api.listing = limited
    status, out, outputs, summary = _tick(tmp_path, api, run)
    printed = capsys.readouterr().out
    assert status == 0 and outputs == {} and run.calls == [] and api.reads() == 1
    assert printed.count("::notice::") == 1 and "::error::" not in printed
    assert "refused the restore's request for a primary rate limit" in printed
    assert not (out / night.LEDGER_NAME).exists()


def test_the_first_day_listed_is_two_days_back_or_the_one_a_person_gave(tmp_path: Path) -> None:
    assert night.since_day({}, TODAY) == "2026-11-22"
    assert night.since_day({"SINCE": "2026-11-18"}, TODAY) == "2026-11-18"
    for bad in ("yesterday", "2026-11-18T00:00", "2026-11-18 --limit 0"):
        with pytest.raises(night.Refused, match="since must be"):
            night.since_day({"SINCE": bad}, TODAY)
    for env in ({"GITHUB_REPOSITORY": "octo"}, {"DEFAULT_BRANCH": "a b"}, {"DEFAULT_BRANCH": ""}):
        with pytest.raises(night.Refused, match="GITHUB_REPOSITORY or DEFAULT_BRANCH"):
            _night(tmp_path, Api(), **env)


# -- the workflow -----------------------------------------------------------
#
# Each lookup below is by role (the job that runs the pass script, the step
# that runs it, the upload of the ledger's artifact), and each assertion is of
# a property of that role. A job or a step added for another purpose changes
# none of them.


def _document() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers() -> dict[str, Any]:
    doc = _document()
    return doc.get("on") or doc[True]


def _runs_the_pass(step_: dict[str, Any]) -> bool:
    return f"scripts/{Path(night.__file__).name}" in str(step_.get("run", ""))


def _ledger_job() -> dict[str, Any]:
    """The job that runs the pass script: the script is run in one place."""
    found = [job for job in _document()["jobs"].values() if any(map(_runs_the_pass, job["steps"]))]
    assert len(found) == 1, "the pass script must be run by exactly one job"
    return found[0]


def _steps() -> list[dict[str, Any]]:
    return _ledger_job()["steps"]


def _pass_step() -> dict[str, Any]:
    found = [one for one in _steps() if _runs_the_pass(one)]
    assert len(found) == 1, "the pass script must be run by exactly one step"
    return found[0]


def _all_steps() -> list[dict[str, Any]]:
    return [one for job in _document()["jobs"].values() for one in job.get("steps") or []]


def _ledger_upload() -> dict[str, Any]:
    """The upload of the ledger's artifact: one writer of that name in the whole workflow."""
    found = [
        one
        for one in _all_steps()
        if "actions/upload-artifact@" in str(one.get("uses", ""))
        and one.get("with", {}).get("name") == night.ARTIFACT
    ]
    assert len(found) == 1, "the ledger artifact must have exactly one writer"
    return found[0]


def _ticks() -> list[tuple[int, int]]:
    """`(hour, minute)` of each scheduled tick, every day of the week."""
    ticks = []
    for entry in _triggers()["schedule"]:
        minute, hour, day, month, weekday = entry["cron"].split()
        assert (day, month, weekday) == ("*", "*", "*"), entry
        ticks.append((int(hour), int(minute)))
    return ticks


def test_only_a_schedule_or_a_person_starts_it_and_every_permission_is_read() -> None:
    assert set(_triggers()) == {"schedule", "workflow_dispatch"}
    # The scopes the ledger's reads need, and no other: the token every job gets.
    assert _document()["permissions"] == {
        "contents": "read",
        "actions": "read",
        "checks": "read",
        "issues": "read",
    }
    # No job widens that for itself.
    for name, job in _document()["jobs"].items():
        assert set((job.get("permissions") or {}).values()) <= {"read"}, name
    job = _ledger_job()
    assert job["runs-on"] == "ubuntu-latest"
    # Only the repository whose runs the ledger reads: a fork has none of its own to read.
    assert job["if"] == f"github.repository == '{ledger.shadow.REPO}'"


def test_two_passes_never_overlap_and_a_running_one_is_not_cancelled() -> None:
    group = _document()["concurrency"]
    assert group["cancel-in-progress"] is False and "${{" not in group["group"]
    # No job leaves that group for one of its own.
    assert all("concurrency" not in job for job in _document()["jobs"].values())
    # A tick finds the one before it ended: a pass that outlasts its timeout is killed.
    minutes = sorted(hour * 60 + minute for hour, minute in _ticks())
    assert min(b - a for a, b in zip(minutes, minutes[1:])) >= _ledger_job()["timeout-minutes"]


def test_the_ticks_are_bounded_passes_in_the_quiet_hours() -> None:
    ticks = _ticks()
    assert len(set(ticks)) == len(ticks)
    for hour, minute in ticks:
        assert hour in QUIET_HOURS and minute % 30 != 0, (hour, minute)
    # A night of passes can read the busiest day measured, and a pass starts
    # only with more than half the pool left and spends under a twentieth of it.
    assert len(ticks) * night.MAX_CALLS >= BUSIEST_DAY_CALLS
    assert night.RESERVE >= POOL // 2 and night.MAX_CALLS < POOL // 20
    assert night.RESERVE + night.MAX_CALLS < POOL
    # No tick starts while the red-cause census reads the same pool.
    census = yaml.safe_load((WORKFLOWS / "ci-red-census.yml").read_text(encoding="utf-8"))
    for entry in (census.get("on") or census[True])["schedule"]:
        minute, hour = (int(part) for part in entry["cron"].split()[:2])
        busy = range(hour * 60 + minute - 5, hour * 60 + minute + 30)
        assert all(h * 60 + m not in busy for h, m in ticks), entry


def test_the_pass_never_waits_and_is_given_only_what_the_script_reads() -> None:
    steps, step_ = _steps(), _pass_step()
    before = steps[: steps.index(step_)]
    # The waiting `gh` wrapper is not put in front of the pass: under the reserve it ends at once.
    assert "gh-rate-limit" not in yaml.safe_dump(before + [step_])
    assert step_["run"].strip() == f"python3 scripts/{Path(night.__file__).name} --out ledger"
    # Neither the pass nor the upload may fail without failing the job.
    assert "continue-on-error" not in step_ and "continue-on-error" not in _ledger_upload()
    for one in _all_steps():
        assert "${{" not in str(one.get("run", "")), one
    read = set(re.findall(r'env(?:\.get\(|\[)"([A-Z_]+)"', Path(night.__file__).read_text()))
    given = set(step_["env"]) | {"GITHUB_REPOSITORY", "GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT"}
    assert read == given
    assert step_["env"]["GH_TOKEN"] == "${{ github.token }}"
    inputs = _triggers()["workflow_dispatch"]["inputs"]
    assert {step_["env"][name] for name in ("SINCE", "RESTORE_RUN", "START_EMPTY")} == {
        "${{ inputs.%s }}" % name for name in inputs
    }
    assert "secrets." not in yaml.safe_dump(_document()["jobs"])
    # The gh version is in the log before the pass that depends on it.
    assert any(str(one.get("run", "")).strip() == "gh --version" for one in before)


def test_the_ledger_is_uploaded_only_on_the_pass_word_and_kept_as_long_as_its_rows() -> None:
    steps, step_, upload = _steps(), _pass_step(), _ledger_upload()
    # In the pass's own job and after it, so the word it waits on is that job's.
    assert upload in steps and steps.index(step_) < steps.index(upload)
    # Only on the output the script writes when what it left may be continued: a
    # pass that failed, or kept out of a low pool, writes none.
    assert "if" not in step_
    assert upload["if"] == f"steps.{step_['id']}.outputs.upload == 'true'"
    assert Path(night.__file__).read_text(encoding="utf-8").count('"upload=true\\n"') == 1
    # No other step carries the pass's id, so the word cannot come from elsewhere.
    assert [one.get("id") for one in steps].count(step_["id"]) == 1
    assert upload["with"]["path"].rstrip("/") == step_["run"].split()[-1]
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["retention-days"] == night.KEEP_DAYS
    assert WORKFLOW.name == night.WORKFLOW


def test_every_action_is_pinned_as_elsewhere_and_the_checkout_keeps_no_credential() -> None:
    pinned: dict[str, set[str]] = {}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        if path == WORKFLOW:
            continue
        for job in (yaml.safe_load(path.read_text(encoding="utf-8")).get("jobs") or {}).values():
            for one in job.get("steps") or []:
                name, _, ref = str(one.get("uses", "")).partition("@")
                pinned.setdefault(name, set()).add(ref)
    for one in _all_steps():
        if "uses" not in one:
            continue
        name, _, ref = one["uses"].partition("@")
        assert re.fullmatch(r"[0-9a-f]{40}", ref) and ref in pinned[name], one["uses"]
        if name == "actions/checkout":
            assert one["with"]["persist-credentials"] is False
    # The pass's job checks out every directory the pass imports from, before the pass.
    steps = _steps()
    checkouts = [
        one
        for one in steps[: steps.index(_pass_step())]
        if str(one.get("uses", "")).startswith("actions/checkout@")
    ]
    assert checkouts, "the pass's job must check the scripts out before the pass"
    for one in checkouts:
        assert {"scripts", ".github/scripts"} <= set(one["with"]["sparse-checkout"].split())
