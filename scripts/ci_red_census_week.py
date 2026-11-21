#!/usr/bin/env python3
"""The week's red-cause census, put together from the days already read.

`ci-red-census.yml` reads yesterday every night with
`scripts/ci_red_cause_census.py` and keeps what it wrote as the artifact
`ci-red-census-day`. Once a week this adds seven of those days up:

    python3 scripts/ci_red_census_week.py --repo kirodotdev/KiroCrew \\
        --workflow ci-red-census.yml --branch main --out-dir week

It reads no run and no check run, so it spends no GraphQL point. It makes a few
REST calls through `gh api`: this workflow's recent runs, the artifacts of that
name, and one download for each artifact it opens. ``--local DIR`` is a day
the caller already holds, as the census wrote it: the workflow passes the day
its own run has just read, which needs no listing to find.

Which day an artifact holds is read from the window in its own `summary.json`,
never from a name or a date on the artifact. For each day the newest artifact
that holds the whole day is used; a day only a census the budget stopped has
read is used as it is and named partial; a day no artifact holds is named
missing. The summary's first line says how many of the days it covers, the
lines under it name each day that is missing or partial and say how to read
it, and the per-day number is divided by the days that were read.

Adding days up counts nothing twice. The days are disjoint: a run belongs to
the day it was created. A push that was red on two days is one red push,
because the rows are added up by head SHA, and a push `CI` ran for on two days
is one push, because each day keeps its pushes (`ci-pushes.csv`), not a count.

What a day cannot see is what happened after it was read: a job re-run to green
later than the next morning's census stays under the cause it had then.

An artifact is untrusted until its run is known. Any workflow run, a pull
request's included, can upload an artifact of any name, so only an artifact of
a `schedule` or `workflow_dispatch` run of this workflow file on the default
branch of this repository is opened. What is in it is read as data: a file that
is not what the census writes makes the day missing, and every cell is cleaned
again on the way out.

It writes the census's own four files to ``--out-dir`` and prints the summary
and its REST call count. It exits 0 for seven whole days, 3 when a day is
missing or partial, and 1 when nothing could be read.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import importlib.util
import io
import json
import subprocess  # noqa: S404 - fixed argv, no shell
import sys
import zipfile
from pathlib import Path
from typing import Any, Callable, NamedTuple

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = "ci-red-census-day"
EVENTS = ("schedule", "workflow_dispatch")
DAYS = 7
# The most artifacts one run opens: a month of nightly ones. Which day an
# artifact holds is known only once it is open, so a week further back than
# this is out of reach.
MAX_DOWNLOADS = 30
# The most one file of an artifact may unpack to.
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MEMBERS = ("summary.json", "red-jobs.csv", "ci-pushes.csv")
PARTIAL_EXIT = 3
HOW_TO_READ_A_DAY = (
    "To fill a day in: run this workflow by hand with `day` set to it, which reads that "
    "day again (yesterday and the day before fit one run's budget), then once more "
    "with `week` ticked and `day` set to the last day of this week."
)
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CENSUS = _load("ci_red_cause_census_for_week", ROOT / "scripts" / "ci_red_cause_census.py")

Download = Callable[[str], bytes | None]


def download_with_gh(path: str, timeout: float = CENSUS.GH_TIMEOUT_SECONDS) -> bytes | None:
    """The body of one REST read as bytes, or ``None`` when it cannot be read."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["gh", "api", path], capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


class Day(NamedTuple):
    """One stored day: its rows, its pushes and the totals its summary holds."""

    day: str
    rows: list[dict[str, str]]
    pushes: frozenset[tuple[str, str]]
    summary: dict[str, Any]

    @property
    def partial(self) -> str | None:
        reason = self.summary.get("partial")
        return CENSUS.evidence_text(str(reason)) if reason else None


def days_ending(until: dt.date, count: int) -> list[str]:
    return [(until - dt.timedelta(days=back)).isoformat() for back in range(count - 1, -1, -1)]


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _stamp(value: Any) -> str | None:
    """A UTC stamp the census wrote, or ``None`` for anything else."""
    if not isinstance(value, str):
        return None
    try:
        dt.datetime.strptime(value, _STAMP)
    except ValueError:
        return None
    return value


def _table(text: str, columns: tuple[str, ...]) -> list[dict[str, str]] | None:
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) != columns:
        return None
    rows = list(reader)
    if any(None in row or None in row.values() for row in rows):
        return None
    return rows


def read_day(blob: bytes) -> Day | None:
    """The day one artifact holds, or ``None`` when it is not what the census writes."""
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            sizes = {info.filename: info.file_size for info in archive.infolist()}
            if any(sizes.get(name, MAX_MEMBER_BYTES + 1) > MAX_MEMBER_BYTES for name in MEMBERS):
                return None
            files = {name: archive.read(name).decode("utf-8") for name in MEMBERS}
    except (zipfile.BadZipFile, KeyError, ValueError, OSError):
        return None
    return day_of(files)


def read_folder(folder: Path) -> Day | None:
    """The day a folder of the census's files holds, or ``None``."""
    try:
        if any((folder / name).stat().st_size > MAX_MEMBER_BYTES for name in MEMBERS):
            return None
        files = {name: (folder / name).read_text(encoding="utf-8") for name in MEMBERS}
    except (OSError, ValueError):
        return None
    return day_of(files)


def day_of(files: dict[str, str]) -> Day | None:
    """The day the census's three files describe, or ``None`` when they are not its own."""
    try:
        summary = json.loads(files["summary.json"])
    except ValueError:
        return None
    rows = _table(files["red-jobs.csv"], CENSUS.CSV_COLUMNS)
    pushes = _table(files["ci-pushes.csv"], CENSUS.PUSH_COLUMNS)
    if not isinstance(summary, dict) or rows is None or pushes is None:
        return None
    since, until = _stamp(summary.get("since")), _stamp(summary.get("until"))
    if since is None or until is None or not since.endswith("T00:00:00Z"):
        return None
    start = dt.datetime.strptime(since, _STAMP)
    if dt.datetime.strptime(until, _STAMP) - start != dt.timedelta(days=1):
        return None
    if any(
        row["cause"] not in CENSUS.CAUSES
        or not row["run_id"].isdigit()
        or not row["job_id"].isdigit()
        for row in rows
    ):
        return None
    if any(push["event"] not in CENSUS.PUSH_EVENTS for push in pushes):
        return None
    return Day(
        day=start.date().isoformat(),
        rows=rows,
        pushes=frozenset((push["event"], push["head_sha"]) for push in pushes),
        summary=summary,
    )


def trusted_runs(api: Any, repo: str, workflow: str, branch: str) -> set[int]:
    """The ids of this workflow's own runs: scheduled or dispatched, on the default branch."""
    body = api.rest_json(f"repos/{repo}/actions/workflows/{workflow}/runs?per_page={CENSUS.PAGE}")
    rows = body.get("workflow_runs") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise CENSUS.CensusError("the workflow runs API did not return a list")
    found: set[int] = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), int):
            continue
        home = row.get("head_repository")
        if (
            row.get("event") in EVENTS
            and row.get("head_branch") == branch
            and row.get("path") == f".github/workflows/{workflow}"
            and isinstance(home, dict)
            and home.get("full_name") == repo
        ):
            found.add(row["id"])
    return found


def stored_artifacts(api: Any, repo: str, runs: set[int], first_day: str) -> list[int]:
    """Ids of the day artifacts of ``runs`` made on or after ``first_day``, newest first."""
    body = api.rest_json(f"repos/{repo}/actions/artifacts?name={ARTIFACT}&per_page={CENSUS.PAGE}")
    rows = body.get("artifacts") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise CENSUS.CensusError("the artifacts API did not return a list")
    kept: list[tuple[str, int]] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), int) or row.get("expired"):
            continue
        run = row.get("workflow_run")
        made = str(row.get("created_at") or "")
        # A day is read after it ends, so its artifact is never older than the day.
        if isinstance(run, dict) and run.get("id") in runs and made >= first_day:
            kept.append((made, row["id"]))
    return [artifact for _made, artifact in sorted(kept, reverse=True)]


def gather(
    api: Any,
    download: Download,
    repo: str,
    workflow: str,
    branch: str,
    wanted: list[str],
    held: Day | None = None,
) -> tuple[dict[str, Day], int]:
    """``(the best stored census of each wanted day, downloads made)``.

    ``held`` is a day the caller already has; it stands unless it is partial
    and a stored one is whole.
    """
    runs = trusted_runs(api, repo, workflow, branch)
    found: dict[str, Day] = {held.day: held} if held and held.day in wanted else {}
    downloads = 0
    for artifact in stored_artifacts(api, repo, runs, wanted[0]):
        if downloads >= MAX_DOWNLOADS:
            break
        if all(day in found and found[day].partial is None for day in wanted):
            break
        downloads += 1
        blob = download(f"repos/{repo}/actions/artifacts/{artifact}/zip")
        day = read_day(blob) if blob else None
        if day is None or day.day not in wanted:
            continue
        # Newest first: a later artifact replaces a kept one only to make a
        # partial day whole.
        kept = found.get(day.day)
        if kept is None or (kept.partial is not None and day.partial is None):
            found[day.day] = day
    return found, downloads


def add_up(found: dict[str, Day], wanted: list[str]) -> tuple[Any, list[str]]:
    """``(one census of the days found, a line for each day missing or partial)``."""
    census = CENSUS.Census()
    notes: list[str] = []
    pushes: set[tuple[str, str]] = set()
    spans: list[str] = []
    runs = 0
    seen: set[tuple[str, str]] = set()
    for name in wanted:
        day = found.get(name)
        if day is None:
            notes.append(f"{name}: no census is stored")
            continue
        if day.partial is not None:
            notes.append(f"{name}: partial ({day.partial})")
        for row in day.rows:
            key = (row["run_id"], row["job_id"])
            if key not in seen:
                seen.add(key)
                census.rows.append(row)
        pushes |= day.pushes
        runs += _count(day.summary.get("runs"))
        spans += [
            stamp
            for stamp in (
                _stamp(day.summary.get("runs_read_from")),
                _stamp(day.summary.get("runs_read_to")),
            )
            if stamp
        ]
        census.workflows = max(census.workflows, _count(day.summary.get("workflows")))
        census.log_unread += _count(day.summary.get("log_unread"))
        census.logs_read += _count(day.summary.get("logs_read"))
        census.suites_unread += _count(day.summary.get("suites_unread"))
    census.rows.sort(key=lambda row: (int(row["run_id"]), int(row["job_id"])))
    census.stats = CENSUS.RunStats(
        runs=runs,
        read_from=min(spans, default=None),
        read_to=max(spans, default=None),
        ci_pushes=frozenset(pushes),
    )
    return census, notes


def render(summary: dict[str, Any], repo: str, wanted: list[str], notes: list[str]) -> str:
    """The census's own summary under a first line that says how many days it covers."""
    lines = CENSUS.render_markdown(summary, repo, wanted[0], wanted[-1]).split("\n")
    lines[0] = (
        f"### Red-cause census of the week: {repo}, {wanted[0]} to {wanted[-1]} UTC, "
        f"{summary['days']:g} of {len(wanted)} day(s) read"
    )
    if notes:
        said = [
            "**Incomplete: the counts below cover only the days that were read.**",
            "",
            *[f"- {note}" for note in notes],
            "",
            HOW_TO_READ_A_DAY,
            "",
        ]
        lines[2:2] = said
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--workflow", required=True, help="the census workflow's file name")
    parser.add_argument("--branch", required=True, help="the default branch")
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument(
        "--until",
        type=CENSUS.parse_bound,
        default=None,
        help="the last UTC day of the week, YYYY-MM-DD (default: yesterday)",
    )
    parser.add_argument(
        "--local",
        type=Path,
        default=None,
        help="a folder holding one day as the census wrote it, used before any stored one",
    )
    parser.add_argument(
        "--gh-timeout-seconds", type=CENSUS._positive, default=CENSUS.GH_TIMEOUT_SECONDS
    )
    return parser


def main(argv: list[str] | None = None, api: Any = None, download: Download | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if CENSUS._REPO.fullmatch(args.repo) is None:
        parser.error("--repo is not owner/name")
    for name, value in (("--workflow", args.workflow), ("--branch", args.branch)):
        if not value or not all(ch.isalnum() or ch in "._-/" for ch in value) or ".." in value:
            parser.error(f"{name} is not a plain name")
    if args.until is not None and not args.until.whole_day:
        parser.error("--until is a day, YYYY-MM-DD")
    timeout = args.gh_timeout_seconds
    api = api or CENSUS.Api(run=lambda argv: CENSUS.run_gh(argv, timeout))
    download = download or (lambda path: download_with_gh(path, timeout))
    last = args.until.at.date() if args.until else api.now().date() - dt.timedelta(days=1)
    wanted = days_ending(last, DAYS)
    downloads = 0
    try:
        held = read_folder(args.local) if args.local is not None else None
        found, downloads = gather(
            api, download, args.repo, args.workflow, args.branch, wanted, held
        )
    except CENSUS.CensusError as exc:
        print(f"ci_red_census_week: {exc}", file=sys.stderr)
        status = 1
    else:
        census, notes = add_up(found, wanted)
        summary = CENSUS.summarize(census, max(len(found), 1))
        summary.update(
            days=len(found),
            since=f"{wanted[0]}T00:00:00Z",
            until=f"{(last + dt.timedelta(days=1)).isoformat()}T00:00:00Z",
            days_wanted=wanted,
            days_missing=[day for day in wanted if day not in found],
            days_partial=[day for day in wanted if day in found and found[day].partial],
        )
        if found:
            markdown = render(summary, args.repo, wanted, notes)
            CENSUS.write_outputs(args.out_dir, census, summary, markdown)
            print(markdown)
            status = PARTIAL_EXIT if notes else 0
        else:
            print(
                f"ci_red_census_week: no stored census for any day of {wanted[0]} to {wanted[-1]}",
                file=sys.stderr,
            )
            status = 1
    print(f"REST calls: {api.rest_calls + downloads}")
    return status


if __name__ == "__main__":
    sys.exit(main())
