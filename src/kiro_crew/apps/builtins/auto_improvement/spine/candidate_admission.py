"""Opt-in project evidence check that both keep paths consult before committing a candidate.

``candidateAdmissionCommand`` in the app config names the checker as an argv list whose
first element is an absolute path. It runs in the same sandbox as the target repository's
tests, reads one JSON request on stdin, and admits the candidate only by printing a JSON
object whose ``request_sha256`` is the SHA-256 of the exact request bytes and whose
``status`` is ``"admitted"``. Anything else refuses the keep. Unset, nothing changes.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.platform_compat import SIGKILL, kill_popen_tree, kill_process_group, pgroup_exists
from kiro_crew.sandbox import popen_limited, sandboxed_spawn_argv

from . import ledger as L
from .driver_git import _git
from .push_policy import normalize_branch, strip_credential_env, write_protected_targets

if TYPE_CHECKING:
    from .contracts import Proposal
    from .driver import Driver

MAX_RESPONSE_BYTES = 65536
TIMEOUT_S = 60

#: Only POSIX gives the checker a process group that can be killed whole once the check
#: ends. On Windows a process the checker left running would outlive the check, so a
#: configured checker refuses the run there instead.
SUPPORTED = os.name == "posix"


def parse_command(value: object) -> tuple[str, ...] | None:
    """The configured checker argv, or ``None`` when unset.

    A malformed value, or any value where the checker is not supported, raises, so the run
    refuses to start: falling back to "no checker", as the budget keys fall back to a
    default, would keep candidates the operator asked to have checked.
    """
    if value is None:
        return None
    if not SUPPORTED:
        raise ValueError("candidateAdmissionCommand is not supported on Windows")
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(arg, str) for arg in value)
        or not Path(value[0]).is_absolute()
    ):
        raise ValueError("candidateAdmissionCommand must be an argv list with an absolute path")
    return tuple(value)


def _exited_unreaped(proc: subprocess.Popen[bytes], seconds: float) -> bool:
    """Whether the checker exited within ``seconds``. Where ``os.waitid`` exists it is not
    reaped, so its pid still names its process group when :func:`_stop_group` kills it."""
    waitid = getattr(os, "waitid", None)
    if waitid is None:
        try:
            proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            return False
        return True
    end = time.monotonic() + seconds
    while True:
        try:
            if waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                return True
        except ChildProcessError:
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.02)


def _stop_group(proc: subprocess.Popen[bytes]) -> None:
    """Kill whatever the check left in the checker's process group, then reap the checker.

    The group is killed while the checker is unreaped, so its id cannot name another group.
    Where the checker was already reaped, the group is killed only while a member still holds
    its id, which keeps it from being reissued.
    """
    kill_popen_tree(proc)
    proc.wait()
    if os.name == "posix" and pgroup_exists(proc.pid):
        with contextlib.suppress(OSError, ValueError):
            kill_process_group(proc.pid, SIGKILL)


def _ask(argv: tuple[str, ...], request: bytes) -> tuple[int, bytes]:
    """Run the checker in the gate's sandbox; return its exit code and bounded stdout.

    The checker leads its own process group, and whatever it answers that whole group is
    killed before this returns, so nothing it started still runs when the clone is checked.
    """
    sandboxed, env, cleanup = sandboxed_spawn_argv(
        list(argv), mode="strict", extra_hidden_dirs=write_protected_targets()
    )
    try:
        # Output goes to a file, not a pipe, so a runaway checker cannot fill memory.
        with tempfile.TemporaryFile() as out:
            proc = popen_limited(
                sandboxed,
                stdin=subprocess.PIPE,
                stdout=out,
                stderr=subprocess.DEVNULL,
                cwd=str(Path(argv[0]).parent),
                env=strip_credential_env(env),
                start_new_session=True,
            )
            try:
                # The request is a few hundred bytes, well inside a pipe buffer, so this
                # write cannot wait on a checker that never reads it.
                if proc.stdin is not None:
                    with contextlib.suppress(BrokenPipeError), proc.stdin:
                        proc.stdin.write(request)
                exited = _exited_unreaped(proc, TIMEOUT_S)
            finally:
                _stop_group(proc)
            if not exited:
                raise subprocess.TimeoutExpired(list(argv), TIMEOUT_S)
            out.seek(0)
            return proc.returncode, out.read(MAX_RESPONSE_BYTES + 1)
    finally:
        if cleanup:
            Path(cleanup).unlink(missing_ok=True)


def _clone_state(driver: Driver) -> tuple[str, str, str] | None:
    """HEAD, the branch tip and the worktree status, or ``None`` when git cannot read the tip
    or the status, which counts as neither clean nor unchanged. The keep checks the branch out
    and commits with ``git add -A``, so a commit or file the checker leaves would be published
    as part of the candidate. The tip is read apart from HEAD, which the pre-push reviewer's
    shell can park elsewhere."""
    branch = f"refs/heads/{normalize_branch(driver.branch)}"
    tip = _git(["rev-parse", "-q", "--verify", branch], driver.clone)
    status = _git(["status", "--porcelain", "-z", "--untracked-files=all"], driver.clone)
    if tip.returncode != 0 or status.returncode != 0:
        return None
    return driver.head_sha(), tip.stdout.strip(), status.stdout


def _verdict(argv: tuple[str, ...], request: bytes) -> str:
    """The checker's answer to ``request``: ``""`` admits, anything else names the refusal."""
    try:
        code, out = _ask(argv, request)
    except subprocess.TimeoutExpired:
        return "timed out"
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return "could not run"
    if code != 0:
        return f"exit {code}"
    try:
        response = json.loads(out) if len(out) <= MAX_RESPONSE_BYTES else None
    except (ValueError, RecursionError):
        response = None
    if (
        not isinstance(response, dict)
        or response.get("request_sha256") != hashlib.sha256(request).hexdigest()
    ):
        return "invalid response"
    if response.get("status") != "admitted":
        return "rejected"
    return ""


def _refusal(driver: Driver, winner: Proposal, argv: tuple[str, ...]) -> str:
    """Why ``winner`` may not be kept, or ``""`` when the checker admitted it.

    The checker vouches for the winner's diff alone, so the clone must be clean before the
    check and unchanged after it, whatever it answered. A change is undone where git can
    (``_reset_provisional`` halts and quarantines when it cannot); a file left behind keeps
    the clone dirty, so every later keep refuses here. The checker can write the clone's git
    metadata, so no host git runs on it after the check until the isolation probe passes, and
    none at all once the clone is retired.
    """
    if getattr(driver, "_repository_retired", False):
        return "the clone was retired"
    before = _clone_state(driver)
    if before is None:
        return "git could not read the clone"
    head, tip, status = before
    if status:
        return "the clone has uncommitted changes"
    request = json.dumps(
        {
            "request_id": uuid.uuid4().hex,
            "base_sha": head,
            "branch": driver.branch,
            "candidate_id": winner.cand_id,
            "patch_sha256": hashlib.sha256(winner.diff.encode("utf-8")).hexdigest(),
            "profile_id": driver.profile.id,
            "track": winner.candidate.kind,
            "metric": driver.profile.ruler.primary_name,
            "direction": driver._metric_direction(),
        },
        sort_keys=True,
    ).encode("utf-8")
    why = _verdict(argv, request)
    if driver._retire_if_unsafe("candidate admission"):
        return "the clone's git metadata became unsafe during the check"
    if _clone_state(driver) != before:
        driver._reset_provisional(tip)
        return "the clone changed during the check"
    return why


def admit_candidate(driver: Driver, winner: Proposal) -> bool:
    """Whether ``winner`` may be kept. A refusal is recorded as ``failed_verify``."""
    argv = driver.candidate_admission
    if argv is None:
        return True
    why = _refusal(driver, winner, argv)
    if not why:
        return True
    note = f"candidate admission refused: {why}"
    driver.stats.not_kept += 1
    driver._record(winner, L.STATUS_FAILED_VERIFY, note)
    driver.log.warning("%s (%s)", note, winner.cand_id)
    return False
