"""A Windows xdist run must refuse to replace a dead worker.

`pytest-timeout` has no SIGALRM on Windows, so it falls back to its thread
method, and that method cannot fail a single test -- it terminates the whole
xdist worker. Every per-test timeout on a Windows runner therefore surfaces as
`node down: Not properly terminated` and pushes xdist into its node-replacement
path, where the node is left in `assigned_work` but absent from
`registered_collections` (#2803). That path either dies with INTERNALERROR or
never completes the session at all, which is how #4227 burned the full
40-minute job cap without ever naming the test at fault.

Measured on the pins this job installs (pytest 9.0.3, pytest-xdist 3.5.0,
pytest-timeout 2.2.0) with one test forced past `--timeout`:

    --max-worker-restart=2   run never finishes; one attempt was still live
                             after 108 minutes, no summary, no victim named
    --max-worker-restart=0   exits 1 in 12.4s and names the crashed test

Upgrading the pin is not an option: 3.8.0, the current release, has the same
defect shape, so there is no fixed version to move to. Refusing the replacement
is what keeps us out of the bad path, and nothing else in the repository
enforces it -- deleting the flag, or "tidying" the Windows invocations to match
the POSIX job, silently restores a hang that surfaces weeks later as a
cancelled job on an unrelated PR.

Deliberately one-directional. It does NOT assert that the POSIX job lacks the
flag: adopting it there may well be right (a worker can die on Linux too, e.g.
under OOM), and a test forbidding that would turn a good change red.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"

FLAG = "--max-worker-restart=0"


def _logical_lines(run: str) -> list[str]:
    """Join shell backslash continuations so one command is one string.

    A per-line scan is the wrong tool here: the invocation this guards already
    spans three physical lines, and the flag could legitimately move onto a
    continuation line. A scan that missed it would report a fact about itself,
    not about the workflow.
    """
    return re.sub(r"\\\s*\n\s*", " ", run).splitlines()


def _windows_xdist_invocations() -> list[tuple[str, str]]:
    """Every (job name, command) that runs pytest under xdist on Windows."""
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    found: list[tuple[str, str]] = []
    for job_name, job in workflow["jobs"].items():
        if "windows" not in str(job.get("runs-on", "")).lower():
            continue
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            for line in _logical_lines(str(step.get("run", ""))):
                # `-n` is what makes a replacement possible at all; without
                # xdist there is no worker to lose.
                if "pytest" in line and re.search(r"\s-n\s", line):
                    found.append((job_name, line.strip()))
    return found


def test_ci_has_a_windows_xdist_job_to_guard() -> None:
    # Anti-vacuity. Every other assertion here iterates this list, so an empty
    # list would make the whole file pass while guarding nothing -- exactly what
    # a job rename or a shard removal would do.
    invocations = _windows_xdist_invocations()
    assert invocations, (
        "ci.yml has no Windows job running pytest under xdist. If that is "
        "deliberate, delete this file; if it is a rename, update the discovery "
        "in _windows_xdist_invocations so the guard keeps applying."
    )


def test_every_windows_xdist_invocation_refuses_worker_replacement() -> None:
    # The regression this exists for: one invocation losing the flag is enough,
    # because a shard hangs on the first timeout it happens to hit.
    for job_name, command in _windows_xdist_invocations():
        assert FLAG in command, (
            f"{job_name} runs pytest under xdist on Windows without {FLAG}: "
            f"{command!r}. Without it a timeout-killed worker sends the session "
            f"into xdist's node-replacement path, which hangs until the job cap "
            f"(#4227). setup.cfg's --max-worker-restart=2 is a deliberate "
            f"developer-machine setting, so this has to be overridden per job."
        )


def test_the_flag_is_not_weakened_to_allow_a_replacement() -> None:
    # `--max-worker-restart=1` reads like a compromise and is not one: a single
    # replacement is the case #4227 actually observed, so any nonzero value
    # re-enters the same path. Catch the plausible near-miss edit explicitly
    # rather than only the outright deletion.
    for job_name, command in _windows_xdist_invocations():
        for value in re.findall(r"--max-worker-restart[= ](\S+)", command):
            assert value == "0", (
                f"{job_name} sets --max-worker-restart={value} on Windows. Any "
                f"nonzero value allows the node replacement that hangs the "
                f"session; the first replacement is the one #4227 hit."
            )
