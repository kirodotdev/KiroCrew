"""Drive a real child process to a named failpoint and end it with ``SIGKILL``.

The wait is for a TERMINAL marker value, never for the file to exist and never
for a duration: the child writes ``"<store>:<failpoint>"`` in one call and the
parent proceeds only on that exact value, so a kill cannot land during setup and
read as a verdict. The child's own output is quoted when it dies first.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.testing.wait import wait_until

CHILD = Path(__file__).resolve().parents[1] / "fixtures" / "crash_child.py"
SRC = Path(__file__).resolve().parents[2] / "src"

#: Only an upper bound on a failure: the wait returns as soon as the marker
#: holds its terminal value, so a passing run does not spend it.
MARKER_TIMEOUT = 60.0
#: How long a reaped child may take to be collected after a signal.
REAP_TIMEOUT = 30.0
#: How much of the child's output a failure quotes: enough for a traceback.
OUTPUT_TAIL = 4000


@dataclass
class Child:
    """One running child: its process, its marker file, and where its output goes."""

    popen: "subprocess.Popen[bytes]"
    marker: Path
    out_path: Path
    expected: str

    def announced(self) -> bool:
        try:
            return self.marker.read_text(encoding="utf-8") == self.expected
        except (FileNotFoundError, UnicodeDecodeError):
            return False

    def output_tail(self) -> str:
        return self.out_path.read_text(encoding="utf-8", errors="replace")[-OUTPUT_TAIL:]


def declared_env(tmp_path: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """The child's whole environment: named, never inherited.

    Inheriting the runner's environment would make the child depend on whatever
    else the shard exported. PATH is carried because interpreter start-up can
    need it; nothing else is.
    """
    env = {
        "KIROCREW_HOME": str(tmp_path / "home"),
        "PYTHONPATH": str(SRC),
        "PATH": os.environ.get("PATH", ""),
        "TMPDIR": str(tmp_path),
        "CRASH_DIR": str(tmp_path),
        # The child imports the package from the checkout; without this it would
        # leave __pycache__ directories beside repository sources, a side effect
        # outside the test's own directory. ``spawn`` also passes ``-B``.
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.update(extra or {})
    return env


def spawn(
    tmp_path: Path,
    store: str,
    failpoint: str,
    *,
    env: dict[str, str] | None = None,
    tag: str = "child",
) -> Child:
    """Start the child for ``store``/``failpoint``; it announces itself on its marker file."""
    marker = tmp_path / f"{tag}.marker"
    out_path = tmp_path / f"{tag}.output"
    # One file for both streams: nothing reads a pipe before the kill, so a child
    # that wrote more than the pipe buffer would block before its failpoint.
    with out_path.open("wb") as out:
        popen = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-B", str(CHILD), store, failpoint, str(marker)],
            env=declared_env(tmp_path, env),
            cwd=tmp_path,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    return Child(popen, marker, out_path, f"{store}:{failpoint}")


def wait_announced(child: Child, *, timeout: float = MARKER_TIMEOUT) -> None:
    """Block until the child holds its terminal marker, or fail the moment it exits first."""

    def _announced() -> bool:
        if child.announced():
            return True
        code = child.popen.poll()
        if code is not None:
            if child.announced():  # announced, then exited: still a success
                return True
            raise AssertionError(
                f"child exited with {code} before failpoint {child.expected!r}; "
                f"output:\n{child.output_tail()}"
            )
        return False

    wait_until(_announced, timeout=timeout, describe=child.output_tail)


def kill(child: Child) -> None:
    """``SIGKILL`` the child: no atexit hook, no drain, no chance to tidy up."""
    child.popen.send_signal(signal.SIGKILL)
    assert child.popen.wait(timeout=REAP_TIMEOUT) != 0, "the child exited on its own"


def reap(child: Child) -> None:
    """Make sure a child is gone; used in ``finally`` so a failed assertion leaves nothing."""
    if child.popen.poll() is None:  # pragma: no cover - only after an earlier failure
        child.popen.kill()
        child.popen.wait(timeout=REAP_TIMEOUT)


def run_to_failpoint_and_kill(
    tmp_path: Path,
    store: str,
    failpoint: str,
    *,
    env: dict[str, str] | None = None,
    tag: str = "child",
) -> None:
    """Run the child to its failpoint and kill it; the store is left as a real kill leaves it."""
    child = spawn(tmp_path, store, failpoint, env=env, tag=tag)
    try:
        wait_announced(child)
        kill(child)
    finally:
        reap(child)


def race(
    tmp_path: Path,
    store: str,
    failpoint: str,
    names: list[str],
    *,
    env: dict[str, str] | None = None,
) -> None:
    """Start one child per name, hold them all on a lock until each is ready, then release them.

    The lock is the barrier: every racer has its store open and says so before
    the parent lets go, so the claims overlap in time instead of running one
    after another. Each racer exits 0 when its work is done.
    """
    import fcntl

    gate_path = tmp_path / "gate"
    gate_path.write_bytes(b"")
    children: list[Child] = []
    with gate_path.open("rb") as gate:
        fcntl.flock(gate, fcntl.LOCK_EX)
        try:
            for name in names:
                children.append(
                    spawn(
                        tmp_path,
                        store,
                        failpoint,
                        env={**(env or {}), "CRASH_WHO": name, "CRASH_GATE": str(gate_path)},
                        tag=f"racer-{name}",
                    )
                )
            for child in children:
                wait_announced(child)
            fcntl.flock(gate, fcntl.LOCK_UN)
            for child in children:
                code = child.popen.wait(timeout=MARKER_TIMEOUT)
                assert code == 0, f"racer exited {code}:\n{child.output_tail()}"
        finally:
            for child in children:
                reap(child)
