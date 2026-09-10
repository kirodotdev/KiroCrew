"""A CI xdist run must refuse to replace a dead worker, on every platform.

Losing a worker sends the session into xdist's node-replacement path, where the
node is left in `assigned_work` but absent from `registered_collections`
(#2803). That path either dies with INTERNALERROR or never completes the session
at all, which is how #4227 burned the full 40-minute job cap without ever naming
the test at fault. `--max-worker-restart=0` is what keeps us out of it: the
first dead worker ends the run promptly with the victim named.

This started as a Windows-only guard, because the TRIGGER that was understood
first is Windows-only: `pytest-timeout` has no SIGALRM there, so it falls back
to its thread method, and that method cannot fail a single test -- it terminates
the whole worker, so every per-test timeout surfaces as `node down: Not properly
terminated`.

The DESTINATION is not platform-specific, and a POSIX shard reached it. On
`Backend Tests (3.12, 4)` of run 33788776072 (2026-09-03), with the session
reporting `timeout method: signal` -- so a slow test there fails as one test,
and this was not the Windows trigger -- a worker died anyway:

    19:12:56  [gw2] node down: Not properly terminated
    19:12:56  F
    19:12:56  replacing crashed worker gw2
    19:16:12  <last progress output; 99.8% of 21077 items reported>
    19:28:32  ##[error]The operation was canceled.     <- the 40-minute cap

12m20s of zero output, no summary, and so the one recorded failure -- the
crashed item itself -- was never named. The three sibling shards passed in 28 to
32 minutes on the same commit, so wall-time capacity (#7516) was not the cause.

Measured on the pins these jobs install (pytest 9.0.3, pytest-xdist 3.5.0,
pytest-timeout 2.2.0) with one test forced past `--timeout`:

    --max-worker-restart=2   run never finishes; one attempt was still live
                             after 108 minutes, no summary, no victim named
    --max-worker-restart=0   exits 1 in 12.4s and names the crashed test

Upgrading the pin is not an option: 3.8.0, the current release, has the same
defect shape, so there is no fixed version to move to.

The trade-off is deliberate: refusing the replacement abandons the work still
pending on that shard, so a killed shard reports fewer tests than it collected.
That is the right trade for CI -- the job is red either way, and a named red
beats an uninterpretable cancellation at the cap.

`setup.cfg` keeps `--max-worker-restart=2` deliberately, to absorb a one-off
memory-pressure crash on a developer machine, so this has to be overridden per
job -- and nothing else in the repository enforces it. Deleting the flag, or
"tidying" one invocation to match another, silently restores a hang that
surfaces weeks later as a cancelled job on an unrelated PR.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"

FLAG = "--max-worker-restart=0"

#: The worker count, however it is spelled. `-n0` / `--numprocesses=0` runs
#: IN-PROCESS with no workers at all, so there is nothing to lose and nothing to
#: replace; requiring the flag there would red a job for a hazard it cannot have.
_WORKERS_RE = re.compile(r"(?:\s-n\s*|\s--numprocesses[= ])(\S+)")


def _logical_lines(run: str) -> list[str]:
    """Join shell backslash continuations so one command is one string.

    A per-line scan is the wrong tool here: the invocations this guards already
    span three physical lines, and the flag could legitimately move onto a
    continuation line. A scan that missed it would report a fact about itself,
    not about the workflow.
    """
    return re.sub(r"\\\s*\n\s*", " ", run).splitlines()


def _ci_xdist_invocations() -> list[tuple[str, str]]:
    """Every (job name, command) that runs pytest under xdist, any platform.

    No `runs-on` filter: the node-replacement path is reached by whatever kills
    a worker, and only the frequency of the kill is platform-specific.
    """
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    found: list[tuple[str, str]] = []
    for job_name, job in workflow["jobs"].items():
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            for line in _logical_lines(str(step.get("run", ""))):
                if "pytest" not in line:
                    continue
                workers = _WORKERS_RE.search(line)
                if workers is None or workers.group(1) == "0":
                    continue
                found.append((job_name, line.strip()))
    return found


def test_ci_has_xdist_jobs_to_guard() -> None:
    # Anti-vacuity. Every other assertion here iterates this list, so an empty
    # list would make the whole file pass while guarding nothing -- exactly what
    # a job rename or a shard removal would do.
    invocations = _ci_xdist_invocations()
    assert invocations, (
        "ci.yml has no job running pytest under xdist. If that is deliberate, "
        "delete this file; if it is a rename or a respelling of -n, update the "
        "discovery in _ci_xdist_invocations so the guard keeps applying."
    )


def test_both_platforms_are_covered() -> None:
    """The guard used to be Windows-only; #4227 recurred on a POSIX shard.

    Pinning that BOTH families are discovered is what stops the coverage gap
    from reopening quietly: a platform filter reintroduced in the discovery
    above would still leave every assertion below passing.
    """
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    runs_on = {
        job_name: str(workflow["jobs"][job_name].get("runs-on", "")).lower()
        for job_name, _ in _ci_xdist_invocations()
    }
    assert any("windows" in target for target in runs_on.values()), (
        f"no Windows xdist job discovered, only {sorted(runs_on)}"
    )
    assert any("windows" not in target for target in runs_on.values()), (
        f"no non-Windows xdist job discovered, only {sorted(runs_on)}"
    )


def test_every_xdist_invocation_refuses_worker_replacement() -> None:
    # The regression this exists for: one invocation losing the flag is enough,
    # because a shard hangs on the first worker it happens to lose.
    for job_name, command in _ci_xdist_invocations():
        assert FLAG in command, (
            f"{job_name} runs pytest under xdist without {FLAG}: {command!r}. "
            f"Without it a dead worker sends the session into xdist's "
            f"node-replacement path, which hangs until the job cap (#4227, and "
            f"again on a POSIX shard in run 33788776072). setup.cfg's "
            f"--max-worker-restart=2 is a deliberate developer-machine setting, "
            f"so this has to be overridden per job."
        )


def test_the_flag_is_not_weakened_to_allow_a_replacement() -> None:
    # `--max-worker-restart=1` reads like a compromise and is not one: a single
    # replacement is the case #4227 actually observed, on both platforms, so any
    # nonzero value re-enters the same path. Catch the plausible near-miss edit
    # explicitly rather than only the outright deletion.
    for job_name, command in _ci_xdist_invocations():
        for value in re.findall(r"--max-worker-restart[= ](\S+)", command):
            assert value == "0", (
                f"{job_name} sets --max-worker-restart={value}. Any nonzero "
                f"value allows the node replacement that hangs the session; the "
                f"first replacement is the one #4227 hit."
            )


def test_a_serial_invocation_is_not_required_to_carry_the_flag() -> None:
    """`-n0` means no workers, so the guard must not demand the flag there.

    This pins the DISCOVERY boundary, not the flag: it fails if a broadened scan
    starts scooping up in-process invocations, which would make
    `backend-test-macos` red for a hazard it cannot have -- it runs one socket
    test with no workers to lose. It deliberately does not forbid the flag on a
    `-n0` line; passing it there is an inert no-op, and a test that rejected it
    would red a harmless change for no gain.
    """
    serial = [
        line.strip()
        for job in yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"].values()
        for step in (job.get("steps") or [])
        if isinstance(step, dict)
        for line in _logical_lines(str(step.get("run", "")))
        if "pytest" in line and _declares_no_workers(line)
    ]
    assert serial, "no -n0 pytest invocation left in ci.yml; drop this test if that is intended"
    guarded = {command for _, command in _ci_xdist_invocations()}
    for line in serial:
        assert line not in guarded, f"a -n0 invocation was discovered as xdist: {line!r}"


def _declares_no_workers(line: str) -> bool:
    workers = _WORKERS_RE.search(line)
    return workers is not None and workers.group(1) == "0"
