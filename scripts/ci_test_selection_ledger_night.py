#!/usr/bin/env python3
"""One nightly pass of the test-selection miss ledger, for `test-selection-ledger.yml`.

    python3 scripts/ci_test_selection_ledger_night.py --out DIR

It restores the ledger the last pass kept, reads a bounded part of the
pull-request runs since `SINCE` with `scripts/ci_test_selection_ledger.py`, then
reports on the whole ledger. `DIR` ends holding `ledger.jsonl`, `summary.md`
and, unless the rates are withheld, `rates.json`: the one artifact the workflow
uploads, and only when this script wrote `upload=true` to `GITHUB_OUTPUT`. It
reads `GH_TOKEN`, `GITHUB_REPOSITORY`, `DEFAULT_BRANCH`, `SINCE`, `RESTORE_RUN`,
`START_EMPTY`, `GITHUB_OUTPUT` and `GITHUB_STEP_SUMMARY`. It posts nothing and
changes nothing on GitHub.

Where the history lives
-----------------------
Verdict artifacts are kept a week, so the days of data the enforcement gate
counts live only in the chain of this workflow's own artifacts: each pass
restores the newest one and uploads it again with what it read.

The ledger is restored only from a run GitHub reports as a successful
`schedule` or `workflow_dispatch` run of this workflow file on the default
branch of this repository (`ci_test_selection_step.trusted_run`, the check the
`changes` job's shadow step uses for the bundle): any run can upload an
artifact of the same name. A pass that did not start uploads nothing and still
ends green, so the newest such run THAT HOLDS the artifact is taken, looking
back through at most `WALK_RUNS` of them.

* No run of this workflow has ever kept a ledger (every successful run was
  looked at and none lists the artifact): the ledger starts empty, and the
  summary says so. It never reads as a continued history.
* The read failed, a rate limit included; the restored file is not a ledger (an
  unreadable archive, no `ledger.jsonl`, over the size limit, a line that is
  not a ledger row); or the runs looked at hold no live ledger though one was
  kept before (it expired, or lies further back): exit 1, nothing uploaded,
  nothing appended. The chain stays as it was; a later pass, or a person,
  restores it.
* `RESTORE_RUN` (a run id, by hand) restores from that run, held to the same
  rule. `START_EMPTY=true` (by hand) starts an empty ledger on purpose. The
  summary's first line records either.

Rows older than `KEEP_DAYS` days are dropped when the ledger is restored, and
the first line says how many.

What a pass spends
------------------
The restore's requests (three as a rule, one more for each run looked back
through) count against `MAX_CALLS`, the most a pass makes. The hour's pool is
read first, from `/rate_limit`, which costs nothing: unless it holds `RESERVE`
beyond what the pass may spend, the pass does not start, the restore included.
A notice says what the pool has, nothing is uploaded, and the exit is 0,
because a pass that keeps out of a low pool did what it should. A request the
restore makes that GitHub refuses for a rate limit ends the pass the same way.
It never waits for a reset; a later tick reads the rest. The report reads the
ledger file alone.

Trust
-----
A verdict is uploaded by the pull request's own run, so ledger numbers are
evidence for a person. Nothing may turn selection on, make a tier skippable or
pass the gate on them alone; turning selection off on them is safe.
"""

from __future__ import annotations

import argparse
import functools
import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import ci_test_selection_ledger as ledger  # noqa: E402  (path set immediately above)
import ci_test_selection_step as step  # noqa: E402

WORKFLOW = "test-selection-ledger.yml"
ARTIFACT = "test-selection-ledger"
LEDGER_NAME = "ledger.jsonl"
RATES_NAME = "rates.json"
SUMMARY_NAME = "summary.md"
# What one pass may spend and what the hour's pool must keep beyond it: a pass
# starts only with more than half the pool left, and spends under a twentieth.
MAX_CALLS = 700
RESERVE = 7500
# Run attempts a pass may record: `MAX_CALLS` ends a pass long before this does.
LIMIT = 100000
# Successful runs looked back through for one that holds the ledger: four
# nights of ticks that all kept out of a low pool.
WALK_RUNS = 20
# Days of runs a pass lists, today included: yesterday whole, and the day before
# for a run that ended late or a night that did not run.
SINCE_DAYS = 2
# Days of rows the ledger keeps: the artifact's own retention.
KEEP_DAYS = 90
# A day of rows measured about 0.9 MB (0.11 MB zipped): four times `KEEP_DAYS` of them.
MAX_LEDGER_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_DAY = re.compile(r"\d{4}-\d\d-\d\d")
_RUN = re.compile(r"[0-9]{1,20}")
_BRANCH = re.compile(r"[A-Za-z0-9_./-]{1,200}")
BY_HAND = "dispatch this workflow with `restore_run` set to a run that holds one, or `start_empty`"

RunLedger = Callable[[list[str]], int]
# Why the pool is too low for a pass to start, None when it is not.
Pool = Callable[[], "str | None"]


class Refused(Exception):
    """The pass failed and uploads nothing; the message is the one line that says why."""


class NotStarted(Exception):
    """The pass kept out of a low pool: nothing read, nothing uploaded, and no failure."""


class Counted:
    """A `Get` that counts the requests made to the API through it."""

    def __init__(self, get: step.Get) -> None:
        self.get, self.calls = get, 0

    def __call__(self, url: str, token: str) -> tuple[int, dict[str, str], bytes]:
        self.calls += url.startswith(step.API_URL + "/")
        return self.get(url, token)


def restored_rows(text: bytes, path: Path, today: datetime) -> tuple[int, int]:
    """Write the restored ledger to `path` without its rows older than `KEEP_DAYS`.

    Returns `(rows kept, rows dropped)`. Anything that is not a ledger raises `Refused`.
    """
    path.write_bytes(text)
    try:
        rows = ledger.read_ledger(path)
    except (ValueError, OSError) as error:
        path.unlink()
        raise Refused(f"the restored ledger is not one: {error}") from error
    oldest = (today - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    kept = [row for row in rows if str(row["date"])[:10] >= oldest]
    path.unlink()
    ledger.append_rows(path, kept)
    return len(kept), len(rows) - len(kept)


def last_kept(get: step.Get, repo: str, token: str, branch: str) -> tuple[dict[str, Any], int]:
    """The newest trusted run that holds a live ledger, and its artifact's id.

    Raises `step.NothingPublished` when every successful run of the workflow
    was looked at and none ever listed the artifact, and `Refused` when the
    runs looked at hold none though one may exist.
    """
    runs = step._json(
        get,
        f"{step.API_URL}/repos/{repo}/actions/workflows/{WORKFLOW}/runs"
        f"?branch={branch}&status=success&per_page={WALK_RUNS}",
        token,
    )
    listed = runs.get("workflow_runs") if isinstance(runs, dict) else None
    if not isinstance(listed, list):
        raise step.NoBundle("the workflow runs API did not return a list")
    expired = False
    for run in [r for r in listed if step.trusted_run(r, repo, WORKFLOW, branch)]:
        live, gone = step.run_artifact(get, repo, token, run["id"], ARTIFACT)
        if live is not None:
            return run, live
        expired = expired or gone
    total = runs.get("total_count")
    if not expired and isinstance(total, int) and total <= len(listed):
        raise step.NothingPublished("no run of this workflow has kept a ledger yet")
    raise Refused(
        f"none of the newest {len(listed)} successful runs holds a live ledger"
        f"{', and one has expired' if expired else ''}: {BY_HAND}"
    )


def restore(get: step.Get, env: Mapping[str, str], path: Path, today: datetime) -> str:
    """Put the last kept ledger at `path` and say, in one line, where tonight's starts from."""
    repo, branch = env.get("GITHUB_REPOSITORY", ""), env.get("DEFAULT_BRANCH", "")
    if not step._REPO.fullmatch(repo) or not _BRANCH.fullmatch(branch):
        raise Refused("GITHUB_REPOSITORY or DEFAULT_BRANCH is not set to a repository and a branch")
    if env.get("START_EMPTY", "") == "true":
        return "The ledger starts empty tonight: a person asked for it (`start_empty`)."
    token, wanted = env.get("GH_TOKEN", ""), env.get("RESTORE_RUN", "")
    if wanted and not _RUN.fullmatch(wanted):
        raise Refused("restore_run is not a run id")
    try:
        if wanted:
            run = step._json(get, f"{step.API_URL}/repos/{repo}/actions/runs/{wanted}", token)
            if not step.trusted_run(run, repo, WORKFLOW, branch):
                raise Refused(
                    f"run {wanted} is not a successful scheduled or dispatched run of "
                    f"{WORKFLOW} on {branch} of this repository"
                )
            artifact, _gone = step.run_artifact(get, repo, token, run["id"], ARTIFACT)
            if artifact is None:
                raise Refused(f"restore_run: run {wanted} holds no live {ARTIFACT} artifact")
        else:
            run, artifact = last_kept(get, repo, token, branch)
        archive = step.download(get, repo, artifact, token, MAX_ARCHIVE_BYTES)
        text = step.member(archive, LEDGER_NAME, alone=False, most=MAX_LEDGER_BYTES)
    except step.NothingPublished as nothing:
        return f"The ledger starts empty tonight: {nothing}. Earlier days of data are not in it."
    except step.RateLimited as limited:
        raise NotStarted(
            kept_out(f"GitHub refused the restore's request for a {limited}")
        ) from limited
    except step.NoBundle as error:
        raise Refused(f"the last ledger could not be restored: {error}") from error
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise Refused(
            f"the last ledger could not be restored: {type(error).__name__}: {error}"
        ) from error
    kept, dropped = restored_rows(text, path, today)
    old = f", {dropped} older than {KEEP_DAYS} days dropped" if dropped else ""
    hand = ", named by hand (`restore_run`)" if wanted else ""
    return (
        f"The ledger continues from run {run['id']} ({ledger.cell(run.get('created_at'))})"
        f"{hand}: {kept} rows{old}."
    )


def kept_out(why: str) -> str:
    """The one line of a pass that kept out of a low pool."""
    return (
        f"Test-selection miss ledger: this pass was not started: {why}. "
        "Nothing was read or uploaded; a later pass reads these runs."
    )


def since_day(env: Mapping[str, str], today: datetime) -> str:
    """The first UTC day a pass lists: `SINCE` when a person set it, else `SINCE_DAYS` back."""
    asked = env.get("SINCE", "")
    if not asked:
        return (today - timedelta(days=SINCE_DAYS)).strftime("%Y-%m-%d")
    if not _DAY.fullmatch(asked):
        raise Refused("since must be a YYYY-MM-DD date, or empty")
    return asked


def night(
    out: Path,
    env: Mapping[str, str],
    get: step.Get,
    run_ledger: RunLedger,
    today: datetime,
    root: Path = REPO_ROOT,
    pool: Pool | None = None,
) -> list[str]:
    """One pass into `out`; the lines that head the summary.

    Raises `Refused` for a failure and `NotStarted` for a pass that kept out of
    a low pool: either way `out` is not for upload.
    """
    path = out / LEDGER_NAME
    since = since_day(env, today)
    low = (pool or ledger.Budget(MAX_CALLS, RESERVE, root).refusal)()
    if low:
        raise NotStarted(kept_out(low))
    counted = Counted(get)
    lines = [restore(counted, env, path, today)]
    # An empty ledger is still a file: the next pass restores what this one uploads.
    path.touch()
    with tempfile.TemporaryDirectory() as scratch:
        common = ["--ledger", str(path), "--root", str(root)]
        read = run_ledger(
            [*common, "--since", since, "--limit", str(LIMIT)]
            + ["--max-calls", str(MAX_CALLS - counted.calls), "--reserve", str(RESERVE)]
            + ["--rates-out", f"{scratch}/rates.json"]
        )
        if read == ledger.EXIT_REFUSED:
            raise NotStarted(
                kept_out(
                    f"the hour's REST pool fell under the {RESERVE} requests it keeps plus "
                    f"the {MAX_CALLS} it may spend"
                )
            )
        if read == ledger.EXIT_NOT_RECORDED:
            lines.append(
                "Tonight's pass ended early on a failed read; the rows read before it are kept."
            )
        elif read not in (0, ledger.EXIT_WITHHELD):
            raise Refused(f"the ledger pass ended with exit {read}")
        # The whole ledger, from the file alone: `--limit 0` makes no request.
        report = run_ledger(
            [*common, "--limit", "0", "--rates-out", str(out / RATES_NAME)]
            + ["--summary-out", str(out / SUMMARY_NAME)]
        )
    if report == ledger.EXIT_WITHHELD:
        lines.append("No rates were proposed: a failing file has no tier.")
    elif report != 0:
        raise Refused(f"the ledger report ended with exit {report}")
    lines.append(f"Runs listed from {since} (UTC).")
    summary = out / SUMMARY_NAME
    body = summary.read_text(encoding="utf-8")
    summary.write_text("\n\n".join(lines) + "\n\n" + body, encoding="utf-8")
    return lines


def _append(path: str | None, text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    get: step.Get | None = None,
    run_ledger: RunLedger = ledger.main,
    today: datetime | None = None,
    pool: Pool | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--out", required=True, help="directory the artifact's files go to")
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    get = get or functools.partial(step.http_get, most=MAX_ARCHIVE_BYTES)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    status = 0
    try:
        night(out, env, get, run_ledger, today or datetime.now(timezone.utc), pool=pool)
        text = (out / SUMMARY_NAME).read_text(encoding="utf-8")
        # The one signal the upload step reads: what `out` holds may be continued.
        _append(env.get("GITHUB_OUTPUT"), "upload=true\n")
    except NotStarted as kept_out:
        text = f"{kept_out}\n"
        print(f"::notice::{kept_out}")
    except Refused as refused:
        status = 1
        text = f"### Test-selection miss ledger: nothing uploaded\n\n{refused}\n"
        print(f"::error::{refused}")
    _append(env.get("GITHUB_STEP_SUMMARY"), text)
    return status


if __name__ == "__main__":
    sys.exit(main())
