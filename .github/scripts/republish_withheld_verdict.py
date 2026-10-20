#!/usr/bin/env python3
"""Re-run the advisory review lane(s) whose withheld verdict never reached a PR.

A whole-design review lane -- Design, UX, First Principles -- can compute a
verdict for the current head and fail to publish it. The lane first looks up its
own comment slot. When that lookup finds the occupant, the lane takes the
REPLACE path: the shared comment-write primitive overwrites an occupant left by
an older head, and stands down when the slot already holds a verdict for the
current head. When the lookup misses the occupant (a degraded, unretried read),
the lane takes the CREATE path, and the primitive's own slot read then finds the
occupied slot and refuses to write beside it. That create-path refusal is the
withhold. The lane completes ``success`` and reports honestly that it published
nothing, the verdict is retained as a workflow artifact, and PR Readiness scores
the lane as a NAMED PENDING -- ``(verdict not published, re-run this lane)`` --
rather than passed. Without a re-run of the lane the pull request reads as
unreviewed for its own head until a human intervenes.

A re-run helps when the lookup failure was transient: the fresh run's lookup
finds the occupant and publishes through the replace path. It cannot help when
the cause persists -- a lookup or slot read that stays unreadable, a write that
keeps failing -- which is why re-runs are capped at ``MAX_RERUN_ATTEMPTS``.

This script is that recovery, driven by ``pr-readiness-sweep.yml``. The sweep is
the right engine for it -- it already holds ``actions: write``, runs on a 5-minute
schedule, and is the existing self-healing re-fire path for the analogous
dropped-readiness-signal case -- and it hands each withheld-verdict PR here.

Why RE-RUN the lane rather than replay the retained artifact: replaying a stored
verdict into a slot would re-open the exact authority question the write
primitive's refusal exists to answer (on what grounds may a run replace the
comment that is there?). Re-running the lane instead recomputes a fresh verdict
and publishes it through the SAME guarded upsert, which already resolves that
question correctly -- it replaces an occupant left by an OLDER head, and stands
down when the slot already holds a verdict for the current head. So the recovery
adds no new trust surface; it only re-drives the mechanism that already exists.

Which lanes are owed is decided by ``pr_status.py --disposition-gate``'s
``unpublished`` array -- the one ``evaluate_reviewer_markers`` predicate that
both PR Readiness and the local ``pr_status.py`` gate answer from, so this
script cannot disagree with either about whether a head is reviewed. It is
scoped to the three whole-design whole-design lanes by construction: the required
lanes (Opus, GPT, Security Scope) fail CLOSED on a withheld verdict and are
recovered by their own check-run finalize, never read as a stale ``success``.

Self-terminating: once a re-run republishes, the lane leaves ``unpublished`` and
PR Readiness drops the ``[verdict-unpublished]`` token the sweep gated on, so the
next sweep does not re-fire it. A lane whose re-run is already in flight has an
``in_progress`` check-run, which PR Readiness scores ``(not started)`` -- again
no token -- so the window between dispatch and republish re-fires nothing.

Run-location follows the two review paths, exactly as the human-override handler
(``ai-review-human-override.yml``) locates a lane to re-run:
  * SAME-REPO: the lane runs on ``pull_request`` for this head, found by its
    workflow file + head SHA, bound to this PR by (head repo, head ref).
  * FORK: the lane runs via ``workflow_run`` from the default branch, so no run
    query by the PR head finds it. The lane writes its own run id into the
    check-run it posts on the head, as ``<!-- ai-review-fork-lane run=<id> -->``
    in ``output.text``; that marker is read back from the check-run row whose
    ``external_id`` is ``<lane>-pr-<PR>-<fast-gate run>-<attempt>``, so a
    sibling pull request on the same fork head can never be the one re-run.

Nothing here is fatal to the sweep. Every failure to re-run one lane is reported
and the script moves on; the sweep tries again on its next tick.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# The disposition gate lives beside pr_status.py in the prepare-pr skill. It is
# invoked as a subprocess (not imported): it already runs as a CLI in
# pr-readiness.yml's own disposition step, so this reuses that entry point
# unchanged rather than taking a second dependency on the module's internals.
_GATE = (
    Path(__file__).resolve().parents[1].parent
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "kirocrew-prepare-pr"
    / "scripts"
    / "pr_status.py"
)

# Each whole-design lane's reviewer name (as evaluate_reviewer_markers reports it in
# ``unpublished``) mapped to the check-run name and the two workflow files that
# can own its run -- the same-repo lane and the fork Stage-2 lane. This mirrors
# the lane table in ai-review-human-override.yml; test_republish_withheld_verdict
# pins the two against each other so a lane rename cannot drift them apart.
# ``fork_lane`` is the prefix the fork lane writes into its check-run's
# ``external_id`` (``<fork_lane>-pr-<PR>-<run>-<attempt>``).
LANES = {
    "DESIGN": {
        "check_name": "Design Review",
        "same_repo": "design-review.yml",
        "fork": "fork-design-review.yml",
        "fork_lane": "design",
    },
    "UX": {
        "check_name": "UX Review",
        "same_repo": "ux-review.yml",
        "fork": "fork-ux-review.yml",
        "fork_lane": "ux",
    },
    "FIRST-PRINCIPLES": {
        "check_name": "First Principles Review",
        "same_repo": "first-principles-review.yml",
        "fork": "fork-first-principles-review.yml",
        "fork_lane": "first-principles",
    },
}

# The fork lane-run marker, written by every fork-*-review.yml into the
# check-run's output.text. KEEP the literal in sync with those workflows and
# with ai-review-human-override.yml's reader.
_FORK_LANE_MARKER_PREFIX = "<!-- ai-review-fork-lane run="

# The re-run is bounded per workflow run. A lane whose withhold cause persists
# -- a head/slot read that stays unreadable, a PATCH that keeps failing -- would
# otherwise re-run on every STALE_MINUTES tick forever (a full paid model review
# every 15-20 minutes per lane per PR), since the sweep places this recovery
# OUTSIDE MAX_DISPATCH on purpose. GitHub's own per-run re-run limit (50) is far
# too high to be a cost bound. A run's ``run_attempt`` counts the attempts the
# platform has recorded for it (1 for the first run, incremented by each rerun),
# so once it reaches this cap the sweep stops re-running that lane and asks for a
# manual re-run instead. Three attempts is enough to clear a transient cause and
# small enough to bound the fail-open cost.
MAX_RERUN_ATTEMPTS = 3

# Wall-clock bound, in seconds, on every child process. The sweep runs in a
# shared concurrency group with a 15-minute job timeout, so one hung ``gh`` call
# would otherwise hold that group for every PR until the job is killed. A
# timed-out call is treated like any other failed read: report, move on, retry
# next tick. A paginated read or the disposition gate's comment walk fits well
# inside it.
SUBPROCESS_TIMEOUT_SECONDS = 120


def _gh_json(args: list[str]) -> object | None:
    """Run a read-only ``gh api`` call and parse its JSON, or ``None`` on any
    failure. A failed read never raises: this is a best-effort recovery and the
    sweep must survive a transient API error to try again next tick."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv prefix, no shell
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"gh call timed out after {SUBPROCESS_TIMEOUT_SECONDS}s\n")
        return None
    except OSError:
        return None
    if proc.returncode != 0:
        if proc.stderr:
            sys.stderr.write(proc.stderr.strip()[:200] + "\n")
        return None
    try:
        return json.loads(proc.stdout)
    except (ValueError, TypeError):
        return None


def owed_lanes(repo: str, pr: str, head: str) -> list[str] | None:
    """The whole-design lane names that owe ``head`` a verdict and have none, from
    the ONE predicate both gates answer from. ``None`` when the gate could not
    read the comments (fail-closed: re-run nothing rather than guess)."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                sys.executable,
                str(_GATE),
                "--disposition-gate",
                "--repo",
                repo,
                "--pr",
                pr,
                "--head",
                head,
            ],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        out = json.loads(proc.stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(out, dict) or not out.get("ok"):
        return None
    unpublished = out.get("unpublished") or []
    # Only lanes this script knows how to re-run. A name the gate reports but
    # LANES does not carry is left to a human, never silently dropped as done.
    return [name for name in unpublished if name in LANES]


def _same_repo_run_id(
    repo: str, workflow: str, head: str, head_repo: str, head_ref: str
) -> tuple[str, int] | None:
    """Newest same-repo ``pull_request`` run of ``workflow`` for ``head``, bound
    to this PR by (head repository, head ref) -- not by ``.pull_requests``,
    which is empty on fork runs. Mirrors the readiness lane-run binding.

    Returns ``(run_id, run_attempt)`` -- the attempt count is already in the
    runs-list payload -- or ``None`` when no matching run is found."""
    data = _gh_json(
        [
            "api",
            "--method",
            "GET",
            "--paginate",
            f"repos/{repo}/actions/runs?event=pull_request&head_sha={head}&per_page=100",
        ]
    )
    if not isinstance(data, dict):
        return None
    runs = data.get("workflow_runs")
    if not isinstance(runs, list):
        return None
    best_id = None
    best_attempt = 1
    for run in runs:
        if not isinstance(run, dict):
            continue
        if run.get("path") != f".github/workflows/{workflow}":
            continue
        if (run.get("head_repository") or {}).get("full_name") != head_repo:
            continue
        if run.get("head_branch") != head_ref:
            continue
        rid = run.get("id")
        if isinstance(rid, int) and (best_id is None or rid > best_id):
            best_id = rid
            attempt = run.get("run_attempt")
            best_attempt = attempt if isinstance(attempt, int) and attempt >= 1 else 1
    return (str(best_id), best_attempt) if best_id is not None else None


def _fork_trigger(repo: str, head: str, head_repo: str, head_ref: str) -> str | None:
    """``<run id>-<attempt>`` of the Fast Gate run a fork PR's lanes are bound
    to: the newest ``fast-gate.yml`` ``pull_request`` run for ``head`` on THIS
    pull request's head repository and branch. The same binding PR Readiness and
    ``ai-review-human-override.yml``'s ``bind_fork_trigger`` use. ``None`` when
    the head repository or branch is gone (a deleted fork binds no run) or no
    such run is found."""
    if not head_repo or not head_ref:
        return None
    data = _gh_json(
        [
            "api",
            "--method",
            "GET",
            "--paginate",
            f"repos/{repo}/actions/runs?event=pull_request&head_sha={head}&per_page=100",
        ]
    )
    if not isinstance(data, dict):
        return None
    runs = data.get("workflow_runs")
    if not isinstance(runs, list):
        return None
    best: dict | None = None
    for run in runs:
        if not isinstance(run, dict):
            continue
        if run.get("path") != ".github/workflows/fast-gate.yml":
            continue
        if (run.get("head_repository") or {}).get("full_name") != head_repo:
            continue
        if run.get("head_branch") != head_ref:
            continue
        rid = run.get("id")
        if isinstance(rid, int) and (best is None or rid > best["id"]):
            best = run
    if best is None:
        return None
    attempt = best.get("run_attempt")
    return f"{best['id']}-{attempt if isinstance(attempt, int) and attempt >= 1 else 1}"


def _fork_run_id(
    repo: str,
    pr: str,
    spec: dict[str, str],
    head: str,
    head_repo: str,
    head_ref: str,
) -> tuple[str, int] | None:
    """The fork Stage-2 lane's run id, read from the lane-run marker the lane
    writes into its check-run's ``output.text`` on the head. Only the row whose
    ``external_id`` is ``<lane>-pr-<pr>-<fast-gate run>-<attempt>`` is read, so
    a sibling pull request sharing this fork head cannot be the lane re-run.
    The resolved run is then VERIFIED to be that fork workflow before it is
    returned, so a check-run of this name posted by anything else cannot
    redirect the re-run.

    Returns ``(run_id, run_attempt)`` -- the attempt count comes from the same
    ``actions/runs/{id}`` read the verification already makes -- or ``None``."""
    trigger = _fork_trigger(repo, head, head_repo, head_ref)
    if trigger is None:
        return None
    want = f"{spec['fork_lane']}-pr-{pr}-{trigger}"
    fork_workflow = spec["fork"]
    enc = spec["check_name"].replace(" ", "%20")
    data = _gh_json(
        [
            "api",
            "--method",
            "GET",
            "--paginate",
            f"repos/{repo}/commits/{head}/check-runs?check_name={enc}&per_page=100&filter=all",
        ]
    )
    if not isinstance(data, dict):
        return None
    rows = data.get("check_runs")
    if not isinstance(rows, list):
        return None
    # Newest check-run row bound to this PR's trigger, by id (distinct per POST,
    # monotonically increasing).
    best = None
    best_id = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("external_id") != want:
            continue
        cid = row.get("id")
        if isinstance(cid, int) and (best_id is None or cid > best_id):
            best_id = cid
            best = row
    if best is None:
        return None
    text = (best.get("output") or {}).get("text") or ""
    marker_at = text.find(_FORK_LANE_MARKER_PREFIX)
    if marker_at < 0:
        return None
    rest = text[marker_at + len(_FORK_LANE_MARKER_PREFIX) :]
    digits = ""
    for ch in rest:
        if ch.isdigit():
            digits += ch
        else:
            break
    if not digits:
        return None
    # Verify the resolved run really is the expected fork lane before re-running
    # it -- the marker is trusted, but a check-run of this name could in
    # principle be posted by any workflow with checks:write. The same read also
    # carries ``run_attempt``, so the attempt bound costs no extra REST call.
    run = _gh_json(["api", f"repos/{repo}/actions/runs/{digits}"])
    if not isinstance(run, dict) or run.get("path") != f".github/workflows/{fork_workflow}":
        return None
    attempt = run.get("run_attempt")
    return digits, attempt if isinstance(attempt, int) and attempt >= 1 else 1


def _rerun(repo: str, run_id: str) -> bool:
    """Re-run a completed workflow run. ``gh run rerun`` re-executes the run,
    whose lane re-reads the head and republishes through its guarded upsert.
    A run that is not in a re-runnable state (still in flight) returns False and
    is simply retried on the next sweep -- never forced."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["gh", "api", "--method", "POST", f"repos/{repo}/actions/runs/{run_id}/rerun"],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _resolve_pr_meta(repo: str, pr: str) -> tuple[bool, str, str] | None:
    """`(is_fork, head_repo_full_name, head_ref)` for the PR, or ``None`` on an
    unreadable PR. Read once, only for a candidate the sweep already narrowed to
    a withheld-verdict pending, so it adds one REST read for a rare PR."""
    data = _gh_json(["api", f"repos/{repo}/pulls/{pr}"])
    if not isinstance(data, dict):
        return None
    head = data.get("head") or {}
    head_repo = (head.get("repo") or {}).get("full_name") or ""
    head_ref = head.get("ref") or ""
    # A PR is a fork when its head repository is not this one; a deleted-fork
    # head carries no repo, which we treat as fork (its lanes ran via
    # workflow_run), so the fork run-marker path is used.
    is_fork = head_repo != repo
    return is_fork, head_repo, head_ref


def republish(repo: str, pr: str, head: str) -> int:
    """Re-run every owing whole-design lane for ``head``. Returns the number of lanes
    re-run; 0 when none was owed or none could be located/re-run."""
    lanes = owed_lanes(repo, pr, head)
    if lanes is None:
        print(
            f"PR #{pr}: the disposition gate could not read this PR's comments; "
            f"re-running nothing this tick."
        )
        return 0
    if not lanes:
        print(f"PR #{pr}: no whole-design lane owes {head} a verdict; nothing to re-run.")
        return 0
    meta = _resolve_pr_meta(repo, pr)
    if meta is None:
        print(
            f"PR #{pr}: could not read the pull request to locate its lane runs; "
            f"the next sweep retries."
        )
        return 0
    is_fork, head_repo, head_ref = meta
    rerun = 0
    for name in lanes:
        spec = LANES[name]
        check_name = spec["check_name"]
        if is_fork:
            located = _fork_run_id(repo, pr, spec, head, head_repo, head_ref)
            which = spec["fork"]
        else:
            located = _same_repo_run_id(repo, spec["same_repo"], head, head_repo, head_ref)
            which = spec["same_repo"]
        if not located:
            print(
                f"PR #{pr}: {check_name} owes {head} a verdict but its {which} run could not "
                f"be located; re-run it manually from the Actions tab."
            )
            continue
        run_id, run_attempt = located
        # Bound the recovery: once a run has been re-run MAX_RERUN_ATTEMPTS times
        # and the verdict still has not published, the withhold cause is not
        # transient. Stop re-running it (the sweep would otherwise re-fire the
        # lane every STALE_MINUTES forever) and ask for a manual re-run.
        if run_attempt >= MAX_RERUN_ATTEMPTS:
            print(
                f"PR #{pr}: {check_name} ({which} run {run_id}) has reached "
                f"{run_attempt} attempts without publishing its verdict for {head}; "
                f"not re-running it. Re-run this lane manually from the Actions tab."
            )
            continue
        if _rerun(repo, run_id):
            rerun += 1
            print(
                f"PR #{pr}: re-running {check_name} ({which} run {run_id}) to republish its "
                f"withheld verdict for {head}."
            )
        else:
            print(
                f"PR #{pr}: could not re-run {check_name} ({which} run {run_id}); it may still "
                f"be in flight. The next sweep retries it."
            )
    return rerun


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--pr", required=True, help="pull request number")
    parser.add_argument("--head", required=True, help="current head SHA")
    args = parser.parse_args(argv)
    republish(args.repo, args.pr, args.head)
    # Always 0: a failure to re-run a lane is reported and retried next tick,
    # never surfaced as an error that could redden the sweep.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
