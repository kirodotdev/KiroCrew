"""Sandboxed PoC execution.

Runs a finding-bound PoC against a :class:`~lib.targets.Target` under strict,
code-enforced limits (SECURITY_NOTES.md #1, #3):

- **Live-target refusal** — ``target.assert_safe()`` runs first; a forbidden
  target raises before anything executes.
- **Bounded wall-clock** — the subprocess is killed at ``timeout_s``; that maps
  to the ``TIMEOUT`` verdict, never a hang.
- **Capped output** — captured stdout/stderr is truncated to ``max_output_bytes``
  so a noisy PoC cannot exhaust memory or the store.
- **cwd jail + no shell** — the PoC runs with ``cwd`` set to a throwaway temp dir
  and is invoked as an argv list (never ``shell=True``), so there is no shell
  interpolation and its relative file activity stays in the jail. (The pod is
  the real network/process isolation boundary; this is defense in depth.)
- **Minimal env** — only PATH + the target handle are passed; no ambient
  credentials are inherited.

Output is scrubbed of secrets before it leaves this module.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

from .exploit import (
    BLOCKED,
    ERROR,
    EXPLOITED,
    TIMEOUT,
    VERDICT_MARKER,
    ExploitEvidence,
    PoC,
    scrub_secrets,
)
from .targets import Target


@dataclass
class ExecLimits:
    timeout_s: float = 30.0
    max_output_bytes: int = 64_000


def classify_verdict(exit_code: int, timed_out: bool, output: str) -> str:
    """Map raw process state + output marker to a verdict.

    A PoC must explicitly print ``SECSCAN_VERDICT: EXPLOITED|BLOCKED``. Absent a
    clear marker on a clean exit, the result is ERROR (inconclusive) rather than
    a false EXPLOITED/BLOCKED — we never guess a security verdict.
    """
    if timed_out:
        return TIMEOUT
    marker_line = ""
    for line in reversed(output.splitlines()):
        if VERDICT_MARKER in line:
            marker_line = line
            break
    if exit_code == 0 and marker_line:
        payload = marker_line.split(VERDICT_MARKER, 1)[1].strip().upper()
        if payload.startswith(EXPLOITED):
            return EXPLOITED
        if payload.startswith(BLOCKED):
            return BLOCKED
    return ERROR


def run_poc(poc: PoC, target: Target, limits: ExecLimits | None = None) -> ExploitEvidence:
    limits = limits or ExecLimits()
    target.assert_safe()  # refuses live/production targets — do not remove

    jail = tempfile.mkdtemp(prefix="secscan-poc-")
    script_path = os.path.join(jail, "poc.py")
    with open(script_path, "w", encoding="utf-8") as fh:
        fh.write(poc.script)

    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    env.update(target.env())

    timed_out = False
    exit_code = -1
    raw = ""
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, script_path],
            cwd=jail,
            env=env,
            capture_output=True,
            text=True,
            timeout=limits.timeout_s,
        )
        exit_code = proc.returncode
        raw = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        raw = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
    except Exception as exc:  # spawn failure etc. — inconclusive, not a crash
        raw = f"executor error: {exc}"
    finally:
        duration = time.monotonic() - started
        shutil.rmtree(jail, ignore_errors=True)

    if len(raw) > limits.max_output_bytes:
        raw = raw[: limits.max_output_bytes] + "\n…[truncated]"

    verdict = classify_verdict(exit_code, timed_out, raw)
    scrubbed = scrub_secrets(raw, extra=[getattr(target, "token", "")])
    return ExploitEvidence(
        finding_id=poc.finding_id,
        verdict=verdict,
        exit_code=exit_code,
        duration_s=round(duration, 3),
        output=scrubbed,
        target_name=getattr(target, "name", "unknown"),
    )
