#!/usr/bin/env python3
"""The Windows shards hold: start ci.yml's Windows backend shards only after the
Linux backend shards of the same run passed.

Invoked from the ``windows-shards-hold`` job in .github/workflows/ci.yml with:

    GH_TOKEN REPO RUN_ID MODE LINUX_JOB LINUX_SHARDS

MODE is the repository variable CI_WINDOWS_SHARDS. The job does not start on
any value but ``after-linux``, and this script releases on any other value too.
LINUX_JOB is the ``name:`` of the Linux shard job and LINUX_SHARDS its shard
count; a shard's check is named ``<LINUX_JOB> (<python>, <n>)``.

## What it waits for

It lists this run's jobs and keeps, for each Linux shard, the row of the
newest attempt (a re-run adds a row and leaves the earlier one in the listing).

* Every shard ``completed`` / ``success``: release. The Windows shards start.
* A shard ``completed`` with ``failure``, ``timed_out`` or ``cancelled``: hold.
  The Windows shards do not start for this attempt.
* Anything else is still running: read again later.

## It holds only on a red it read, and a hold is always red

``hold=true`` is written to $GITHUB_OUTPUT only on the second case, and the
script then exits 1, so a run whose Windows shards were held has a failed job
and cannot conclude ``success``. ``backend-test-windows`` skips on that output,
never on this job's result, so a job that failed for any other reason (a runner
fault, a timeout) still starts the Windows shards.

Every other way out releases them, which is what happens when this job does
not exist: MODE is not ``after-linux``, the inputs are malformed, the listing
names no Linux shard at all, a shard ended with a conclusion not listed above,
MAX_FAILED_READS reads in a row failed, or TOTAL_BUDGET ran out. The hold
exists to save runner time, so nothing short of a red Linux shard it
positively read may keep the Windows shards from running.

## What it costs

One runner for as long as the Linux shards take, and one listing (one GET per
100 jobs of the run) at FIRST_WAIT and then every POLL_INTERVAL seconds. The
installation token's REST budget is shared with every other workflow, which is
why the first read waits for the earliest moment the shards can all be done
and the interval is minutes, not seconds. Every call is a GET.

## The exit codes

    0  release: see above
    1  hold: a Linux shard of this run is red and ``hold=true`` was written

``gh`` is found on PATH; the tests call ``main`` with their own reader, sleep
and clock instead (test_windows_shards_hold.py).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from typing import Callable

MODE_AFTER_LINUX = "after-linux"

# Seconds before the first read. Measured on 444 pull request pushes: the
# first red Linux shard completed 19.5 minutes after the shards were released
# at the 10th percentile, and the last green one 26.6 minutes at the median.
FIRST_WAIT = 600
POLL_INTERVAL = 180
# Seconds after which the Windows shards start whatever the Linux shards are
# doing. The job's timeout-minutes covers it.
TOTAL_BUDGET = 2700
MAX_FAILED_READS = 3
PAGE_SIZE = 100
MAX_PAGES = 10
GH_TIMEOUT = 60

RED = ("failure", "timed_out", "cancelled")

Reader = Callable[[str, str, int], "list[dict] | None"]


def read_jobs_page(repo: str, run_id: str, page: int) -> list[dict] | None:
    """One page of the run's jobs across all attempts, or None when unreadable."""
    try:
        done = subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "GET",
                f"repos/{repo}/actions/runs/{run_id}/jobs",
                "-f",
                "filter=all",
                "-f",
                f"per_page={PAGE_SIZE}",
                "-f",
                f"page={page}",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GH_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    try:
        rows = json.loads(done.stdout)["jobs"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return None
    return rows


def read_jobs(repo: str, run_id: str, reader: Reader) -> list[dict] | None:
    """Every job row of the run, or None when any page is unreadable."""
    rows: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        chunk = reader(repo, run_id, page)
        if chunk is None:
            return None
        rows.extend(chunk)
        if len(chunk) < PAGE_SIZE:
            return rows
    return None


def newest_shard_rows(rows: list[dict], job: str) -> dict[int, dict]:
    """Shard number -> that shard's row from the newest attempt that has one."""
    shard_name = re.compile(rf"^{re.escape(job)} \((?:[^()]*, )?(\d+)\)$")
    newest: dict[int, dict] = {}

    def age(row: dict) -> tuple[int, int]:
        attempt, row_id = row.get("run_attempt"), row.get("id")
        return (
            attempt if isinstance(attempt, int) else 0,
            row_id if isinstance(row_id, int) else 0,
        )

    for row in rows:
        name = row.get("name")
        match = shard_name.match(name) if isinstance(name, str) else None
        if not match:
            continue
        shard = int(match.group(1))
        if shard not in newest or age(row) > age(newest[shard]):
            newest[shard] = row
    return newest


def judge(rows: list[dict], job: str, shards: int) -> tuple[str, list[str]]:
    """``(state, shard names)``: green, red, waiting, absent or odd."""
    newest = newest_shard_rows(rows, job)
    if not newest:
        return "absent", []
    red: list[str] = []
    odd: list[str] = []
    passed = 0
    for shard in sorted(newest):
        row = newest[shard]
        if row.get("status") != "completed":
            continue
        conclusion = row.get("conclusion")
        label = f"{row.get('name')} ({conclusion})"
        if conclusion == "success":
            passed += 1
        elif conclusion in RED:
            red.append(label)
        else:
            odd.append(label)
    if red:
        return "red", red
    if odd:
        return "odd", odd
    if passed == shards and set(newest) == set(range(1, shards + 1)):
        return "green", []
    return "waiting", []


def _summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError:
        pass


def _release(why: str, warn: bool = False) -> int:
    if warn:
        print(f"::warning::Windows Shards Hold: {why} Starting the Windows shards.")
    else:
        print(f"{why} Starting the Windows shards.")
    _summary(["### Windows Shards Hold", "", f"{why} Starting the Windows shards."])
    return 0


def _hold(red: list[str], job: str) -> int:
    way_out = (
        "Fix the Linux failure and push. If the Linux shards pass on a re-run instead, use "
        "'Re-run failed jobs': it re-runs this job with them, and the Windows shards start "
        "once the Linux shards are green."
    )
    names = ", ".join(red)
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("hold=true\n")
    print(
        f"::error title=Windows shards held::{names}: the Windows backend shards did not "
        f"start, because CI_WINDOWS_SHARDS={MODE_AFTER_LINUX} starts them only after every "
        f"'{job}' shard passed. This head has no Windows verdict. {way_out}"
    )
    _summary(
        [
            "### Windows Shards Hold: Windows shards held",
            "",
            f"`CI_WINDOWS_SHARDS={MODE_AFTER_LINUX}` starts the Windows backend shards only "
            f"after every `{job}` shard passed, and these did not:",
            "",
            *[f"- {name}" for name in red],
            "",
            "The Windows backend shards did not start. This head has no Windows verdict.",
            "",
            way_out,
        ]
    )
    return 1


def main(
    env: dict[str, str] | None = None,
    reader: Reader = read_jobs_page,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    env = dict(os.environ) if env is None else env
    mode = env.get("MODE", "")
    repo = env.get("REPO", "")
    run_id = env.get("RUN_ID", "")
    job = env.get("LINUX_JOB", "")
    shards_text = env.get("LINUX_SHARDS", "")
    if mode != MODE_AFTER_LINUX:
        return _release(f"CI_WINDOWS_SHARDS is '{mode}', not '{MODE_AFTER_LINUX}'.")
    if (
        not re.fullmatch(r"[\w.-]+/[\w.-]+", repo)
        or not re.fullmatch(r"[0-9]+", run_id)
        or not job
        or not re.fullmatch(r"[1-9][0-9]?", shards_text)
    ):
        return _release("No repository, run id, Linux job name or shard count to read.", warn=True)
    shards = int(shards_text)

    started = clock()
    failed_reads = 0
    wait = float(FIRST_WAIT)
    while True:
        sleep(wait)
        rows = read_jobs(repo, run_id, reader)
        if rows is None:
            failed_reads += 1
            if failed_reads >= MAX_FAILED_READS:
                return _release(
                    f"Could not read the jobs of run {run_id} {failed_reads} times in a row.",
                    warn=True,
                )
        else:
            failed_reads = 0
            state, names = judge(rows, job, shards)
            if state == "red":
                return _hold(names, job)
            if state == "green":
                return _release(f"All {shards} '{job}' shards passed.")
            if state == "absent":
                return _release(f"Run {run_id} lists no '{job}' shard.", warn=True)
            if state == "odd":
                return _release(
                    f"A '{job}' shard ended with a conclusion this job does not judge "
                    f"({', '.join(names)}).",
                    warn=True,
                )
        left = TOTAL_BUDGET - (clock() - started)
        if left <= 0:
            return _release(
                f"The '{job}' shards had no verdict after {TOTAL_BUDGET}s.",
                warn=True,
            )
        wait = min(float(POLL_INTERVAL), left)


if __name__ == "__main__":
    sys.exit(main())
