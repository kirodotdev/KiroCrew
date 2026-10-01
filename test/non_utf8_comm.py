"""Processes and fixture ``/proc`` tables whose ``comm`` is not valid UTF-8.

A process may name itself arbitrary bytes with ``prctl(PR_SET_NAME)``, and the
kernel cuts any longer name at 15 bytes -- mid-character when it is multibyte.
:data:`BAD_COMM` is the ordinary case: ``run_データ処理.py`` cut where the
kernel cuts it, which is what running a script of that name produces. A reader
that decodes ``stat``, ``status`` or ``comm`` strictly raises on such a
process; these helpers let a test show the reader still sees it.

Every process started here is this test's own child, killed by its own process
group and reaped before the helper returns. Nothing else is ever signalled.
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import os
import select
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

#: The name a script must have for the kernel to store :data:`BAD_COMM`.
SCRIPT_NAME = "run_データ処理.py"

#: ``run_データ処理.py`` as the kernel stores it: 15 bytes, the last character cut.
BAD_COMM = SCRIPT_NAME.encode()[:15]

#: Renames itself (unless it already runs under the name), optionally starts a
#: grandchild in its own session and group, prints that grandchild's pid (or 0)
#: and sleeps until killed.
_CHILD = """
import ctypes, subprocess, sys, time
if {rename!r}:
    ctypes.CDLL(None, use_errno=True).prctl(15, ctypes.c_char_p({name!r}), 0, 0, 0)
grandchild = 0
if {with_grandchild!r}:
    grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"]).pid
print(grandchild, flush=True)
time.sleep(120)
"""

#: How long a child may take to report that it is ready.
_READY_TIMEOUT_SECS = 20.0


def write_stat(
    root: Path,
    pid: int,
    *,
    comm: bytes = BAD_COMM,
    state: str = "S",
    ppid: int = 1,
    pgrp: int = 0,
    session: int = 0,
    start_ticks: int = 1000,
    rss_pages: int = 10,
) -> Path:
    """Write a kernel-shaped ``<root>/<pid>/stat`` and return the pid directory."""
    tail = [state, ppid, pgrp, session] + [0] * 7 + [3, 4] + [0] * 6 + [start_ticks, 0, rss_pages]
    directory = root / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "stat").write_bytes(
        b"%d (%s) " % (pid, comm) + " ".join(str(t) for t in tail).encode() + b"\n"
    )
    return directory


@functools.cache
def comm_is_settable() -> bool:
    """Whether a process on this host can name itself :data:`BAD_COMM` and be read back.

    Probed once, on a throwaway thread of this process: the same
    ``prctl(PR_SET_NAME)`` a renamed child makes, read back through
    ``/proc/self/task/<tid>/comm``. A seccomp filter that refuses the call, or a
    libc without the symbol, answers no here, so the live tests stand aside
    instead of failing on the host. Only the probe thread is renamed, and it
    exits at once. Each child still checks its own name and skips if it differs.

    The join has no timeout on purpose: this is a ``skipif`` verdict cached for
    the worker's life, and a wall-clock bound on it turns a loaded host into a
    silent skip of every live test (testing-conventions). The probe is one
    ``prctl`` and a 16-byte ``/proc`` read, with nothing to wait on.
    """
    if sys.platform != "linux" or not os.path.isdir("/proc/self/task"):
        return False
    answer: list[bool] = []

    def probe() -> None:
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(15, ctypes.c_char_p(BAD_COMM), 0, 0, 0) != 0:
                answer.append(False)
                return
            with open(f"/proc/self/task/{threading.get_native_id()}/comm", "rb") as fh:
                answer.append(fh.read().rstrip(b"\n") == BAD_COMM)
        except (OSError, AttributeError):
            answer.append(False)

    thread = threading.Thread(target=probe, name="comm-probe", daemon=True)
    thread.start()
    thread.join()
    return answer == [True]


def _read_ready_line(proc: subprocess.Popen[bytes]) -> bytes:
    """The child's first line, bounded so a stalled child fails by name."""
    assert proc.stdout is not None
    deadline = time.monotonic() + _READY_TIMEOUT_SECS
    while time.monotonic() < deadline:
        ready, _, _ = select.select([proc.stdout], [], [], 0.1)
        if ready:
            return proc.stdout.readline()
        if proc.poll() is not None:
            return b""
    raise AssertionError(f"child {proc.pid} did not report ready in {_READY_TIMEOUT_SECS}s")


@contextmanager
def _child(argv: list[str], *, cwd: Path | None = None) -> Iterator[tuple[int, int]]:
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        cwd=cwd,
        start_new_session=True,
    )
    try:
        grandchild = int(_read_ready_line(proc) or b"0")
        try:
            with open(f"/proc/{proc.pid}/comm", "rb") as fh:
                named = fh.read().rstrip(b"\n") == BAD_COMM
        except OSError:
            named = False
        if not named:
            pytest.skip("this host would not give the child a non-UTF-8 name")
        yield proc.pid, grandchild
    finally:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
        if proc.stdout is not None:
            proc.stdout.close()


@contextmanager
def renamed_child(*, with_grandchild: bool = False) -> Iterator[tuple[int, int]]:
    """A live child named :data:`BAD_COMM` through ``prctl``; its own session and group leader.

    Yields ``(child pid, grandchild pid or 0)``; the grandchild's name is the
    interpreter's. The whole group is SIGKILLed and the child reaped on exit.
    Both run in a throwaway directory, never the checkout pytest was started in.
    """
    source = _CHILD.format(rename=True, name=BAD_COMM, with_grandchild=with_grandchild)
    with tempfile.TemporaryDirectory(prefix="non-utf8-comm-") as cwd:
        with _child([sys.executable, "-c", source], cwd=Path(cwd)) as pids:
            yield pids


@contextmanager
def script_named_child(directory: Path) -> Iterator[int]:
    """A live child that is a script named :data:`SCRIPT_NAME`, run directly: no ``prctl``.

    The kernel names an executed script after its file, cut at 15 bytes, so this
    is how an ordinary ``./run_データ処理.py &`` comes to carry :data:`BAD_COMM`.
    """
    script = directory / SCRIPT_NAME
    script.write_text(
        f"#!{sys.executable}\n" + _CHILD.format(rename=False, name=BAD_COMM, with_grandchild=False),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    try:
        with _child([str(script)], cwd=directory) as (pid, _):
            yield pid
    except PermissionError:
        pytest.skip(f"{directory} does not allow executing a script")
