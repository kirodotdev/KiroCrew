"""Run the three independent browser lanes after CI's private-MCP prerequisite.

Linux CI only. Each lane is a subreaper: harness gateways start new sessions,
so a process-group signal alone cannot drain them after pytest is interrupted.
No product imports, new dependencies, test selection, or test-policy overrides.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

MEMORY_UI_TIMEOUT = 12 * 60
COMMANDS = {
    "i18n": ["npm", "--prefix", "website", "run", "i18n:render"],
    "smoke": [sys.executable, "setup.py", "test_e2e"],
    "memory-ui": [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-n0",
        "--no-cov",
        "-p",
        "no:cacheprovider",
        "--timeout=600",
        "test/e2e/test_memory_ui_evidence.py",
    ],
}


def _children() -> list[int]:
    # Only our unreaped children, never a host-wide PID/name search. Their PIDs
    # cannot be reused until THIS single-threaded process calls waitpid.
    path = Path(f"/proc/self/task/{os.getpid()}/children")
    return [int(pid) for pid in path.read_text(encoding="ascii").split()]


def _drain(grace: float, direct: dict[int, subprocess.Popen]) -> bool:
    """Reap dead children; stop live residue, including adopted setsid descendants."""
    remaining = _children()
    had_live_children = False
    deadline = time.monotonic() + grace
    while remaining:
        sig = signal.SIGTERM if time.monotonic() < deadline else signal.SIGKILL
        for pid in remaining:
            # /proc lists zombies too. Reap before classifying or signalling,
            # and let Popen retain the wait status for each direct child.
            if pid in direct:
                if direct[pid].poll() is not None:
                    del direct[pid]
                    continue
            else:
                try:
                    reaped, _ = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    continue
                if reaped:
                    continue
            # An unreaped child pins this PID even if it exits before kill.
            # Never signal a PID after waitpid has released that ownership.
            had_live_children = True
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        # Parent exit adopts descendants; rescan until none remain, including
        # children adopted during this pass. No waitpid(-1) may steal statuses.
        time.sleep(0.02)
        remaining = _children()
    return had_live_children


def _supervise(commands: dict[str, list[str]], *, timeout=None, grace=2.0) -> int:
    """Await every lane, retaining failure in either completion order."""
    if sys.platform != "linux":
        raise RuntimeError("ci_e2e_parallel is for the Linux E2E job only")
    libc = ctypes.CDLL(None, use_errno=True)
    # PR_SET_CHILD_SUBREAPER is Linux's ABI value, on x86_64 and aarch64.
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot enable E2E child reaping")
    cancelled = 0

    def stop(signum, _frame):
        nonlocal cancelled
        cancelled = cancelled or signum

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    children: dict[str, subprocess.Popen] = {}
    result = 0
    started = time.monotonic()
    try:
        for name, command in commands.items():
            if cancelled:
                break
            children[name] = subprocess.Popen(command, start_new_session=True)
        pending = dict(children)
        while pending and not cancelled:
            for name, child in list(pending.items()):
                rc = child.poll()
                if rc is not None:
                    print(f"[e2e:{name}] exit={rc}", flush=True)
                    result = result or (128 - rc if rc < 0 else rc)
                    del pending[name]
            # Reap adopted exits while lanes still run: a harness may be
            # waiting for its stopped process group to disappear before exit.
            # Direct children must remain exclusively owned by Popen.poll().
            direct_pids = {child.pid for child in pending.values()}
            for pid in _children():
                if pid not in direct_pids:
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        pass
            if pending and timeout is not None and time.monotonic() - started >= timeout:
                print("::error::Memory UI lane exceeded its 12-minute budget", flush=True)
                result = 124
                break
            if pending:
                time.sleep(0.05)
    finally:
        # Preserve Popen's direct-child statuses before the final waitpid drain.
        for child in children.values():
            child.poll()
        residue = _drain(
            grace, {child.pid: child for child in children.values() if child.returncode is None}
        )
        for child in children.values():
            child.wait()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if cancelled:
        return 128 + cancelled
    if residue and not result:
        print("::error::E2E lane left children alive after exit; drained", flush=True)
        return 1
    return result


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] in COMMANDS:
        lane = sys.argv[1]
        # Keep pytest temporary roots (and their retention sweeps) disjoint.
        # Harnesses still choose fresh homes/ports; no live HOME is substituted.
        root = (
            Path(os.environ["RUNNER_TEMP"])
            / {"smoke": "e2e-s", "memory-ui": "e2e-u", "i18n": "e2e-i"}[lane]
        )
        root.mkdir(parents=True, exist_ok=True)
        os.environ["TMPDIR"] = str(root)
        if lane == "memory-ui":
            os.environ["KIROCREW_E2E"] = "1"
        return _supervise(
            {lane: COMMANDS[lane]},
            timeout=MEMORY_UI_TIMEOUT if lane == "memory-ui" else None,
        )
    if len(sys.argv) != 1:
        raise SystemExit("usage: ci_e2e_parallel.py [smoke|memory-ui|i18n]")
    script = str(Path(__file__).resolve())
    # Separate supervisors give the UI its own unchanged 12-minute cap without
    # cancelling the smoke lane or losing adopted grandchildren from either.
    return _supervise({lane: [sys.executable, script, lane] for lane in COMMANDS}, grace=5.0)


if __name__ == "__main__":
    raise SystemExit(main())
