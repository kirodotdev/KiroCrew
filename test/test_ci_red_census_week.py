"""The week's red-cause census is the sum of its stored days, and of nothing else.

`scripts/ci_red_census_week.py` downloads the day artifacts the nightly census
kept and adds them up. Three things are held here:

* THE SUM -- for any rows and pushes, split over days any way, the week's
  numbers are the numbers one census of all of it gives: no push is counted
  twice, whichever days it was red on.
* WHICH ARTIFACTS -- only an artifact of this workflow's own scheduled or
  dispatched runs on the default branch is opened; the day it holds is read
  from its own window; a whole day wins over a partial one; a day nothing holds
  is named missing, and the exit code says so.
* WHAT IS IN THEM -- a file that is not what the census writes makes the day
  missing, and nothing a job is named reaches the summary.

Every artifact is built with the census's own writer, and the API and the
download are stand-ins: no socket, no process.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parents[1]
REPO = "kirodotdev/KiroCrew"
WORKFLOW = "ci-red-census.yml"
BRANCH = "main"
SUNDAY = dt.datetime(2026, 11, 22, 7, 17, tzinfo=dt.timezone.utc)
WEEK = [f"2026-11-{day}" for day in range(15, 22)]
SHARD = "Backend Tests (3.12, 1)"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WEEKLY = _load("ci_red_census_week_under_test", ROOT / "scripts" / "ci_red_census_week.py")
CENSUS = WEEKLY.CENSUS


def row(
    job_id: int,
    cause: str = "diff",
    sha: str = "a" * 40,
    job: str = SHARD,
    workflow: str = "CI",
    event: str = "pull_request",
) -> dict[str, Any]:
    return {
        "run_id": 1000 + job_id,
        "job_id": job_id,
        "workflow": workflow,
        "job": job,
        "event": event,
        "head_sha": sha,
        "pr": 7 if event == "pull_request" else "",
        "cause": cause,
        "evidence": "step: Run tests",
    }


def day_files(
    day: str,
    rows: list[dict[str, Any]],
    pushes: set[tuple[str, str]] | None = None,
    partial: str | None = None,
    runs: int = 10,
) -> dict[str, bytes]:
    """The files the census writes for one whole UTC day, by its own writer."""
    census = CENSUS.Census()
    census.rows = rows
    census.partial = partial
    census.workflows = 2
    census.stats = CENSUS.RunStats(
        runs=runs,
        read_from=f"{day}T00:00:05Z",
        read_to=f"{day}T23:59:55Z",
        ci_pushes=frozenset(pushes or set()),
    )
    after = (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()
    summary = {
        **CENSUS.summarize(census, 1),
        "since": f"{day}T00:00:00Z",
        "until": f"{after}T00:00:00Z",
    }
    with tempfile.TemporaryDirectory() as folder:
        CENSUS.write_outputs(Path(folder), census, summary, "unused")
        return {path.name: path.read_bytes() for path in sorted(Path(folder).iterdir())}


def zipped(files: dict[str, bytes]) -> bytes:
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return blob.getvalue()


def a_day(day: str, rows: list[dict[str, Any]], **more: Any) -> bytes:
    return zipped(day_files(day, rows, **more))


class Store:
    """The repository's runs and artifacts, answered the way `gh api` answers."""

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []
        self.artifacts: list[dict[str, Any]] = []
        self.blobs: dict[int, bytes | None] = {}
        self.paths: list[str] = []
        self.downloaded: list[int] = []

    def run(
        self,
        run_id: int,
        event: str = "schedule",
        branch: str = BRANCH,
        path: str = f".github/workflows/{WORKFLOW}",
        home: str = REPO,
    ) -> int:
        self.runs.append(
            {
                "id": run_id,
                "event": event,
                "head_branch": branch,
                "path": path,
                "head_repository": {"full_name": home},
            }
        )
        return run_id

    def artifact(
        self,
        artifact_id: int,
        run_id: int,
        blob: bytes | None,
        made: str,
        expired: bool = False,
    ) -> None:
        self.artifacts.append(
            {
                "id": artifact_id,
                "name": WEEKLY.ARTIFACT,
                "expired": expired,
                "created_at": made,
                "workflow_run": {"id": run_id},
            }
        )
        self.blobs[artifact_id] = blob

    def nightly(self, days: dict[str, bytes | None]) -> None:
        """One scheduled run a day, the morning after, each with its artifact."""
        for at, (day, blob) in enumerate(sorted(days.items()), start=1):
            after = (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()
            self.artifact(100 + at, self.run(at), blob, f"{after}T07:40:00Z")

    def gh(self, argv: list[str]) -> tuple[int, str, str]:
        assert argv[0] == "api" and len(argv) == 2
        self.paths.append(argv[1])
        if "/workflows/" in argv[1]:
            return 0, json.dumps({"workflow_runs": self.runs}), ""
        return 0, json.dumps({"artifacts": self.artifacts}), ""

    def download(self, path: str) -> bytes | None:
        artifact = int(path.split("/")[-2])
        self.downloaded.append(artifact)
        return self.blobs[artifact]


def assemble(store: Store, out_dir: Path, *more: str) -> int:
    api = CENSUS.Api(run=store.gh, sleep=lambda seconds: None, now=lambda: SUNDAY)
    args = ["--repo", REPO, "--workflow", WORKFLOW, "--branch", BRANCH, "--out-dir", str(out_dir)]
    return int(WEEKLY.main([*args, *more], api=api, download=store.download))


def summary_of(out_dir: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    return loaded


# ── The sum ──────────────────────────────────────────────────────────────────

SHAS = [letter * 40 for letter in "abcdef"]
a_row = st.builds(
    lambda cause, sha, job, workflow, event: (cause, sha, job, workflow, event),
    st.sampled_from(CENSUS.CAUSES),
    st.sampled_from(SHAS),
    st.sampled_from([SHARD, "Backend Tests (Windows) (3)", "Coverage Gate"]),
    st.sampled_from(["CI", "PR Hygiene"]),
    st.sampled_from(["pull_request", "push"]),
)
a_week = st.lists(
    st.tuples(st.sampled_from(WEEK), a_row), max_size=40
).flatmap(  # each red job on the day it ran, and the pushes of each day
    lambda jobs: st.tuples(
        st.just(jobs),
        st.lists(
            st.tuples(
                st.sampled_from(WEEK),
                st.sampled_from(["pull_request", "push"]),
                st.sampled_from(SHAS),
            ),
            max_size=20,
        ),
    )
)

GOAL_KEYS = (
    "red_jobs",
    "runs",
    "by_cause",
    "broken_main_share_of_red_pr_pushes",
    "rate_limit_share_of_red_jobs",
    "backend_shard_flaky_or_infrastructure_reds",
)


@settings(max_examples=60, deadline=None)
@given(a_week)
def test_the_week_is_what_one_census_of_all_seven_days_gives(week: Any) -> None:
    jobs, pushes = week
    by_day: dict[str, list[dict[str, Any]]] = {day: [] for day in WEEK}
    for job_id, (day, (cause, sha, job, workflow, event)) in enumerate(jobs, start=1):
        by_day[day].append(row(job_id, cause, sha, job, workflow, event))
    pushed: dict[str, set[tuple[str, str]]] = {day: set() for day in WEEK}
    for day, event, sha in pushes:
        pushed[day].add((event, sha))
    store = Store()
    store.nightly({day: a_day(day, by_day[day], pushes=pushed[day]) for day in WEEK})

    with tempfile.TemporaryDirectory() as folder:
        status = assemble(store, Path(folder))
        got = summary_of(Path(folder))

    whole = CENSUS.Census()
    whole.rows = [one for day in WEEK for one in by_day[day]]
    whole.stats = CENSUS.RunStats(
        runs=10 * len(WEEK),
        read_from=None,
        read_to=None,
        ci_pushes=frozenset(set().union(*pushed.values())),
    )
    want = CENSUS.summarize(whole, len(WEEK))
    assert status == 0
    assert {key: got[key] for key in GOAL_KEYS} == {key: want[key] for key in GOAL_KEYS}
    assert (got["days"], got["days_missing"], got["days_partial"]) == (7, [], [])


def test_a_push_red_on_two_days_is_one_red_push_and_one_push(tmp_path: Path) -> None:
    sha = "b" * 40
    push = {("pull_request", sha)}
    store = Store()
    store.nightly(
        {
            **{day: a_day(day, []) for day in WEEK},
            WEEK[2]: a_day(WEEK[2], [row(1, "broken_main", sha)], pushes=push),
            WEEK[3]: a_day(WEEK[3], [row(2, "diff", sha)], pushes=push),
        }
    )

    assert assemble(store, tmp_path) == 0

    summary = summary_of(tmp_path)
    assert summary["broken_main_share_of_red_pr_pushes"] == {
        "share": 1.0,
        "broken_main": 1,
        "broken_main_only": 0,
        "n": 1,
    }
    assert summary["backend_shard_flaky_or_infrastructure_reds"]["pushes"] == 1
    assert summary["red_jobs"] == 2
    assert (tmp_path / "ci-pushes.csv").read_text(encoding="utf-8").count(sha) == 1


def test_a_day_stored_twice_is_counted_once(tmp_path: Path) -> None:
    store = Store()
    store.nightly({day: a_day(day, [row(at)]) for at, day in enumerate(WEEK, start=1)})
    again = a_day(WEEK[0], [row(1)])
    store.artifact(900, store.run(90, event="workflow_dispatch"), again, "2026-11-21T12:00:00Z")

    assert assemble(store, tmp_path) == 0
    assert summary_of(tmp_path)["red_jobs"] == 7


# ── Which artifacts ──────────────────────────────────────────────────────────


def test_seven_whole_days_cost_two_listings_and_seven_downloads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store()
    store.nightly({day: a_day(day, [row(at)]) for at, day in enumerate(WEEK, start=1)})

    assert assemble(store, tmp_path) == 0

    assert store.paths == [
        f"repos/{REPO}/actions/workflows/{WORKFLOW}/runs?per_page=100",
        f"repos/{REPO}/actions/artifacts?name=ci-red-census-day&per_page=100",
    ]
    assert sorted(store.downloaded) == [101, 102, 103, 104, 105, 106, 107]
    printed = capsys.readouterr().out
    assert "2026-11-15 to 2026-11-21 UTC (7 day(s))" in printed
    assert printed.endswith("REST calls: 9\n")
    assert "Incomplete" not in printed
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "ci-pushes.csv",
        "red-jobs.csv",
        "summary.json",
        "summary.md",
    ]


@pytest.mark.parametrize(
    "untrusted",
    [
        {"event": "pull_request"},
        {"event": "pull_request_target"},
        {"branch": "someone/main"},
        {"path": ".github/workflows/ci.yml"},
        {"home": "someone/KiroCrew"},
    ],
)
def test_an_artifact_of_any_other_run_is_never_opened(
    tmp_path: Path, untrusted: dict[str, str]
) -> None:
    store = Store()
    store.nightly({day: a_day(day, []) for day in WEEK[:-1]})
    forged = a_day(WEEK[-1], [row(at, "rate_limit") for at in range(1, 50)])
    store.artifact(900, store.run(90, **untrusted), forged, "2026-11-22T07:00:00Z")
    store.artifact(901, 4242, forged, "2026-11-22T07:01:00Z")

    assert assemble(store, tmp_path) == WEEKLY.PARTIAL_EXIT

    assert 900 not in store.downloaded and 901 not in store.downloaded
    summary = summary_of(tmp_path)
    assert (summary["red_jobs"], summary["days_missing"]) == (0, [WEEK[-1]])


def test_the_day_is_read_from_the_artifacts_own_window(tmp_path: Path) -> None:
    store = Store()
    store.nightly({day: a_day(day, []) for day in WEEK})
    # A dispatch on Saturday that read the Monday again.
    reread = a_day(WEEK[1], [row(1), row(2)])
    store.artifact(900, store.run(90, event="workflow_dispatch"), reread, "2026-11-21T15:00:00Z")
    before = a_day("2026-11-14", [row(3)])
    store.artifact(901, store.run(91), before, "2026-11-15T07:40:00Z")

    assert assemble(store, tmp_path) == 0
    assert summary_of(tmp_path)["red_jobs"] == 2


def test_a_whole_day_replaces_a_partial_one_whichever_is_newer(tmp_path: Path) -> None:
    for newer_is_whole in (True, False):
        store = Store()
        store.nightly({day: a_day(day, []) for day in WEEK[1:]})
        whole = a_day(WEEK[0], [row(1), row(2)])
        cut = a_day(WEEK[0], [row(1)], partial="7500 GraphQL points spent")
        first, second = (cut, whole) if newer_is_whole else (whole, cut)
        store.artifact(900, store.run(90), first, "2026-11-16T07:40:00Z")
        store.artifact(
            901, store.run(91, event="workflow_dispatch"), second, "2026-11-16T09:00:00Z"
        )
        out_dir = tmp_path / str(newer_is_whole)

        assert assemble(store, out_dir) == 0
        assert summary_of(out_dir)["red_jobs"] == 2


def test_a_missing_day_and_a_partial_day_are_said_first_and_exit_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store()
    days = {day: a_day(day, [row(at, "flaky")]) for at, day in enumerate(WEEK, start=1)}
    del days[WEEK[2]]
    days[WEEK[4]] = a_day(WEEK[4], [row(5, "flaky")], partial="7500 GraphQL points spent")
    store.nightly(days)

    assert assemble(store, tmp_path) == WEEKLY.PARTIAL_EXIT

    summary = summary_of(tmp_path)
    assert (summary["days"], summary["days_missing"], summary["days_partial"]) == (
        6,
        [WEEK[2]],
        [WEEK[4]],
    )
    # Six flaky shard reds over the six days that were read, not over seven.
    assert summary["backend_shard_flaky_or_infrastructure_reds"]["per_day"] == 1.0
    lines = capsys.readouterr().out.split("\n")
    assert lines[2].startswith("**Incomplete: 6 of 7 day(s) were read")
    assert lines[4:6] == [
        f"- {WEEK[2]}: no census is stored",
        f"- {WEEK[4]}: partial (7500 GraphQL points spent)",
    ]


def test_an_expired_artifact_and_one_older_than_the_week_are_not_opened(tmp_path: Path) -> None:
    store = Store()
    store.nightly({day: a_day(day, []) for day in WEEK[1:]})
    store.artifact(900, store.run(90), a_day(WEEK[0], []), "2026-11-16T07:40:00Z", expired=True)
    store.artifact(901, store.run(91), a_day(WEEK[0], []), "2026-11-14T23:00:00Z")

    assert assemble(store, tmp_path) == WEEKLY.PARTIAL_EXIT
    assert 900 not in store.downloaded and 901 not in store.downloaded


def test_no_more_than_a_month_of_artifacts_is_opened(tmp_path: Path) -> None:
    store = Store()
    junk = zipped({"note.txt": b"not a census"})
    for at in range(40):
        store.artifact(500 + at, store.run(50 + at), junk, f"2026-11-21T08:{at:02d}:00Z")

    assert assemble(store, tmp_path) == 1
    assert len(store.downloaded) == WEEKLY.MAX_DOWNLOADS == 30
    assert not tmp_path.exists() or not any(tmp_path.iterdir())


def test_a_week_with_nothing_stored_fails_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert assemble(Store(), tmp_path / "week") == 1
    captured = capsys.readouterr()
    assert "no stored census for any day of 2026-11-15 to 2026-11-21" in captured.err
    assert captured.out == "REST calls: 2\n"
    assert not (tmp_path / "week").exists()


def test_a_listing_that_cannot_be_read_fails_the_week(tmp_path: Path) -> None:
    api = CENSUS.Api(run=lambda argv: (1, "", "gh: HTTP 502"), sleep=lambda seconds: None)
    args = ["--repo", REPO, "--workflow", WORKFLOW, "--branch", BRANCH, "--out-dir", str(tmp_path)]
    assert WEEKLY.main(args, api=api, download=lambda path: None) == 1


def test_until_and_days_choose_another_week(tmp_path: Path) -> None:
    store = Store()
    store.nightly({day: a_day(day, [row(at)]) for at, day in enumerate(WEEK, start=1)})

    assert assemble(store, tmp_path, "--until", WEEK[2], "--days", "3") == 0
    summary = summary_of(tmp_path)
    assert (summary["days_wanted"], summary["red_jobs"]) == (WEEK[:3], 3)
    assert (summary["since"], summary["until"]) == ("2026-11-15T00:00:00Z", "2026-11-18T00:00:00Z")


@pytest.mark.parametrize(
    "flags",
    [
        ["--until", "2026-11-21T09:00Z"],
        ["--until", "yesterday"],
        ["--days", "0"],
        ["--repo", "not a repo"],
        ["--workflow", "../ci.yml"],
        ["--branch", "main?per_page=1"],
    ],
)
def test_an_argument_that_is_not_a_plain_name_or_day_starts_no_read(
    tmp_path: Path, flags: list[str]
) -> None:
    store = Store()
    with pytest.raises(SystemExit):
        assemble(store, tmp_path, *flags)
    assert store.paths == []


# ── What is in them ──────────────────────────────────────────────────────────


def _with(files: dict[str, bytes], name: str, body: bytes | None) -> bytes:
    changed = dict(files)
    if body is None:
        del changed[name]
    else:
        changed[name] = body
    return zipped(changed)


def _summary_with(files: dict[str, bytes], **fields: Any) -> bytes:
    summary = {**json.loads(files["summary.json"]), **fields}
    return _with(files, "summary.json", json.dumps(summary).encode())


GOOD = day_files(WEEK[0], [row(1)])
BAD_ARTIFACTS = {
    "not a zip": b"PK-not-really",
    "no summary": _with(GOOD, "summary.json", None),
    "no pushes": _with(GOOD, "ci-pushes.csv", None),
    "summary is a list": _with(GOOD, "summary.json", b"[]"),
    "summary is not JSON": _with(GOOD, "summary.json", b"{"),
    "not UTF-8": _with(GOOD, "red-jobs.csv", b"\xff\xfe"),
    "other columns": _with(GOOD, "red-jobs.csv", b"run_id,job_id\n1,2\n"),
    "a row too long": _with(
        GOOD, "red-jobs.csv", GOOD["red-jobs.csv"] + b"1,2,3,4,5,6,7,diff,x,y\n"
    ),
    "an unknown cause": _with(
        GOOD, "red-jobs.csv", GOOD["red-jobs.csv"].replace(b",diff,", b",gremlins,")
    ),
    "a run id that is no number": _with(
        GOOD, "red-jobs.csv", GOOD["red-jobs.csv"].replace(b"1001,1,", b"x,1,")
    ),
    "an unknown event in the pushes": _with(GOOD, "ci-pushes.csv", b"event,head_sha\nfork,abc\n"),
    "no window": _summary_with(GOOD, since=None),
    "a window that is not a day": _summary_with(GOOD, until=f"{WEEK[0]}T12:00:00Z"),
    "a window that starts mid-day": _summary_with(
        GOOD, since=f"{WEEK[0]}T09:00:00Z", until=f"{WEEK[1]}T09:00:00Z"
    ),
    "a window that is not a stamp": _summary_with(GOOD, since="last tuesday"),
}


@pytest.mark.parametrize("blob", BAD_ARTIFACTS.values(), ids=BAD_ARTIFACTS.keys())
def test_an_artifact_that_is_not_what_the_census_writes_holds_no_day(blob: bytes) -> None:
    assert WEEKLY.read_day(zipped(GOOD)) is not None
    assert WEEKLY.read_day(blob) is None


def test_a_file_that_would_unpack_too_large_is_not_unpacked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(WEEKLY, "MAX_MEMBER_BYTES", len(GOOD["red-jobs.csv"]) - 1)
    assert WEEKLY.read_day(zipped(GOOD)) is None


def test_a_download_that_fails_leaves_the_day_missing(tmp_path: Path) -> None:
    store = Store()
    store.nightly({**{day: a_day(day, []) for day in WEEK}, WEEK[3]: None})

    assert assemble(store, tmp_path) == WEEKLY.PARTIAL_EXIT
    assert summary_of(tmp_path)["days_missing"] == [WEEK[3]]


def test_what_a_job_is_named_and_why_a_day_stopped_reach_no_summary_line_of_their_own(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job = "build\r\n::error::forged <img src=x onerror=1>"
    reason = "spent\n::warning::forged-reason `x` " + "y" * 200
    store = Store()
    days = {day: a_day(day, []) for day in WEEK}
    days[WEEK[0]] = a_day(WEEK[0], [row(1, job=job, workflow="CI\n::notice::forged-workflow")])
    days[WEEK[1]] = a_day(WEEK[1], [], partial=reason)
    store.nightly(days)

    assert assemble(store, tmp_path) == WEEKLY.PARTIAL_EXIT

    captured = capsys.readouterr()
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    for text in (summary, captured.out, captured.err):
        assert "onerror" not in text and "forged-workflow" not in text
        assert not any(line.startswith("::") for line in text.split("\n"))
    (note,) = [line for line in summary.split("\n") if line.startswith(f"- {WEEK[1]}")]
    assert len(note) <= len(f"- {WEEK[1]}: partial ()") + CENSUS.EVIDENCE_CHARS
    assert "`" not in note and all(" " <= ch <= "~" for ch in note)
    raw = (tmp_path / "red-jobs.csv").read_bytes()
    assert b"\r" not in raw and raw.count(b"\n") == 2


def test_the_download_is_one_gh_call_read_as_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[list[str], dict[str, Any]]] = []

    class Finished:
        returncode, stdout = 0, b"PK\x03\x04\xff"

    def fake_run(argv: list[str], **options: Any) -> Finished:
        seen.append((argv, options))
        return Finished()

    monkeypatch.setattr(WEEKLY.subprocess, "run", fake_run)

    assert WEEKLY.download_with_gh("repos/o/r/actions/artifacts/7/zip", 900) == b"PK\x03\x04\xff"
    ((argv, options),) = seen
    assert argv == ["gh", "api", "repos/o/r/actions/artifacts/7/zip"]
    assert options["timeout"] == 900 and "encoding" not in options and "text" not in options
