"""Behavioural tests for the merged-PR fetch in .github/workflows/ship-report.yml.

The fetch must return every PR merged in the reporting window however many
there are. GitHub search caps one query at 1000 results, so the step splits a
full slice at its midpoint until every slice fits. A window the step cannot
cover completely must fail the run, because a failed run does not advance the
window and the next run re-covers it.

The `gh` stub below emulates the search: it honours the inclusive
`merged:A..B` range and truncates at min(--limit, 1000), which is what makes
an unsplit query lose PRs.

Skipped where the POSIX toolchain the step needs (bash, jq, GNU date) is
unavailable, which is the case on the Windows leg of the matrix.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ship-report.yml"


def _has_gnu_date() -> bool:
    """The step splits slices with GNU ``date -d``; BSD ``date`` (macOS) has no
    such flag, so these tests skip there instead of failing on the tool."""
    if shutil.which("date") is None:
        return False
    probe = subprocess.run(
        ["date", "-u", "-d", "@0", "+%s"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    not WORKFLOW.exists()
    or os.name == "nt"
    or shutil.which("bash") is None
    or shutil.which("jq") is None
    or not _has_gnu_date(),
    reason="requires the workflow file plus a POSIX bash, jq and GNU date",
)

START = "2026-10-29T09:57:35Z"
END = "2026-10-30T09:57:33Z"
SEARCH_CAP = 1000

GH_STUB = f"""#!{sys.executable}
import json, os, sys

args = sys.argv[1:]
if args[:2] != ["pr", "list"]:
    print("gh stub: unhandled: " + " ".join(args), file=sys.stderr)
    sys.exit(90)
search = args[args.index("--search") + 1]
limit = int(args[args.index("--limit") + 1])
with open(os.environ["FIXTURES"] + "/calls.log", "a") as log:
    log.write(search + "\\n")
query = search[len("merged:"):]
if query.startswith(">="):
    lo, hi = query[2:], "9999"
else:
    lo, hi = query.split("..")
prs = json.load(open(os.environ["FIXTURES"] + "/prs.json"))
hits = [p for p in prs if lo <= p["mergedAt"] <= hi]
hits.sort(key=lambda p: p["mergedAt"], reverse=True)
json.dump(hits[: min(limit, {SEARCH_CAP})], sys.stdout)
"""


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _pr(number: int, merged: str) -> dict:
    return {
        "number": number,
        "title": f"fix: change {number}",
        "author": {"login": f"user{number % 7}", "is_bot": False},
        "mergedAt": merged,
        "url": f"https://github.com/o/r/pull/{number}",
        "body": f"**Goal:** ship change {number}.",
    }


def _spread(n: int, first: int = 10_000) -> list[dict]:
    """n PRs strictly inside the window, evenly spaced."""
    start, end = _parse(START), _parse(END)
    span = (end - start).total_seconds()
    return [
        _pr(first + i, _ts(start + timedelta(seconds=1 + int(i * (span - 2) / max(n, 1)))))
        for i in range(n)
    ]


def _expected(prs: list[dict]) -> list[dict]:
    """The step's contract: the half-open window, ordered by merge time."""
    inside = [p for p in prs if START < p["mergedAt"] <= END]
    return sorted(inside, key=lambda p: p["mergedAt"])


class Runner:
    def __init__(self, root: Path) -> None:
        self.fixtures = root / "fixtures"
        self.work = root / "work"
        bindir = root / "bin"
        for d in (self.fixtures, self.work, bindir):
            d.mkdir(parents=True)
        stub = bindir / "gh"
        stub.write_text(GH_STUB)
        stub.chmod(0o755)
        self.outputs_file = self.work / "gh_output"
        steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["report"]["steps"]
        self.script = next(s for s in steps if s.get("id") == "fetch")["run"]
        self.env = {
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FIXTURES": str(self.fixtures),
            "GH_TOKEN": "stub",
            "GITHUB_REPOSITORY": "kirodotdev/KiroCrew",
            "GITHUB_OUTPUT": str(self.outputs_file),
            "START": START,
            "END": END,
        }

    def fetch(self, prs: list[dict]) -> subprocess.CompletedProcess[str]:
        (self.fixtures / "prs.json").write_text(json.dumps(prs))
        (self.fixtures / "calls.log").write_text("")
        self.outputs_file.write_text("")
        return subprocess.run(  # noqa: S603 - fixed argv, test-local stubs
            ["bash", "-c", self.script],
            cwd=self.work,
            env=self.env,
            text=True,
            encoding="utf-8",
            capture_output=True,
        )

    @property
    def classified(self) -> list[dict]:
        return json.loads((self.work / "prs.json").read_text())

    @property
    def selected(self) -> list[dict]:
        """The selected PRs as fetched, without the `kind` the step adds."""
        return [{k: v for k, v in p.items() if k != "kind"} for p in self.classified]

    @property
    def count(self) -> str:
        lines = self.outputs_file.read_text().splitlines()
        return dict(line.split("=", 1) for line in lines if "=" in line)["count"]

    @property
    def calls(self) -> list[str]:
        return (self.fixtures / "calls.log").read_text().splitlines()


@pytest.fixture()
def runner(tmp_path: Path) -> Runner:
    return Runner(tmp_path)


@pytest.mark.parametrize("n", [0, 1, 300, 301, 999, 1000, 1001, 2500])
def test_every_pr_in_the_window_is_reported(runner: Runner, n: int) -> None:
    """Empty, at the old 300 cap, one past it, either side of the search cap,
    and well beyond it: all of them come back complete and in order."""
    prs = _spread(n)
    step = runner.fetch(prs)
    assert step.returncode == 0, step.stderr
    assert runner.selected == _expected(prs)
    assert runner.count == str(n)


@pytest.mark.parametrize("n", [0, 300, 999])
def test_a_window_under_the_search_cap_is_one_query(runner: Runner, n: int) -> None:
    runner.fetch(_spread(n))
    assert runner.calls == [f"merged:{START}..{END}"]


def test_a_full_slice_is_split_rather_than_truncated(runner: Runner) -> None:
    runner.fetch(_spread(2500))
    assert len(runner.calls) > 1
    assert runner.calls[0] == f"merged:{START}..{END}"


def test_the_window_is_half_open(runner: Runner) -> None:
    """(START, END]: a PR merged exactly at START belongs to the previous report,
    one merged after END to the next."""
    prs = [
        _pr(1, START),
        _pr(2, _ts(_parse(START) + timedelta(seconds=1))),
        _pr(3, END),
        _pr(4, _ts(_parse(END) + timedelta(minutes=5))),
    ]
    step = runner.fetch(prs)
    assert step.returncode == 0, step.stderr
    assert [p["number"] for p in runner.selected] == [2, 3]


def test_a_pr_on_a_split_point_is_reported_once(runner: Runner) -> None:
    """Neighbouring slices share their midpoint second, since the range is
    inclusive at both ends."""
    mid = _ts(
        datetime.fromtimestamp(
            (_parse(START).timestamp() + _parse(END).timestamp()) // 2, tz=timezone.utc
        )
    )
    prs = _spread(SEARCH_CAP) + [_pr(1, mid)]
    step = runner.fetch(prs)
    assert step.returncode == 0, step.stderr
    numbers = [p["number"] for p in runner.selected]
    assert numbers.count(1) == 1
    assert len(numbers) == len(set(numbers)) == SEARCH_CAP + 1


def test_output_fields_are_the_ones_the_body_steps_read(runner: Runner) -> None:
    """The narrative and template steps read these exact fields; `kind` is
    added here so both classify a PR the same way."""
    prs = _spread(3)
    runner.fetch(prs)
    assert runner.selected == _expected(prs)
    assert set(runner.classified[0]) == {
        "number",
        "title",
        "author",
        "mergedAt",
        "url",
        "body",
        "kind",
    }


@pytest.mark.parametrize(
    ("title", "kind"),
    [
        ("feat(chat): a", "Features"),
        ("fix: b", "Fixes"),
        ("docs(rfc): c", "Docs"),
        ("ci: d", "CI and chores"),
        ("chore(release): e", "CI and chores"),
        ("build: f", "CI and chores"),
        ("test(ci): g", "CI and chores"),
        ("refactor(apps): h", "Refactoring"),
        ("perf: i", "Refactoring"),
        ("revert: j", "Reverts"),
        ("Bump foo from 1 to 2", "Other"),
    ],
)
def test_each_pr_is_classified_by_its_title_prefix(runner: Runner, title: str, kind: str) -> None:
    pr = {**_spread(1)[0], "title": title}
    step = runner.fetch([pr])
    assert step.returncode == 0, step.stderr
    assert runner.classified[0]["kind"] == kind


def test_an_unsplittable_slice_fails_instead_of_dropping_prs(runner: Runner) -> None:
    """A full one-second slice cannot be narrowed further. Failing leaves the
    window unadvanced; reporting a truncated list would lose PRs for good."""
    second = _ts(_parse(START) + timedelta(hours=1))
    prs = [_pr(20_000 + i, second) for i in range(SEARCH_CAP)]
    step = runner.fetch(prs)
    assert step.returncode == 1
    assert "cannot split the search further" in step.stdout
