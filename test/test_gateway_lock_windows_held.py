"""A real Windows mandatory lock on ``gateway.lock`` reads as HELD, never free.

Runs only on a Windows host (CI: Backend Tests (Windows)). A child process
stands in for a draining gateway: it stamps its pid at byte 0 of
``<home>/gateway.lock`` and takes ``msvcrt.locking`` on byte 0 -- the same
byte ``platform_lock_compat`` locks for the real gateway -- then holds it while
this process probes. Under that mandatory lock the stamped pid cannot be read
from another handle, so :func:`gateway_lock.lock_holder` must answer
"positively held, holder unnameable" (``LockProbeError.held is True``) and
``kirocrew gateway-pid`` must print ``holder=held``: the verdict the desktop
waits out before respawning. Reporting nobody here would let it spawn a second
writer into a live lock; reporting indeterminate would leave it with no gateway.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from kiro_crew import cli_server
from kiro_crew.gateway_lock import LOCK_FILENAME, LockProbeError, lock_holder

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="real msvcrt mandatory lock")

_HOLDER = textwrap.dedent("""
    import msvcrt, os, sys
    fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
    os.write(fd, f"{os.getpid()}\\n".encode())
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    print(f"locked {os.getpid()}", flush=True)
    sys.stdin.readline()
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    print("released", flush=True)
    sys.stdin.readline()
    os.close(fd)
    """)


@pytest.fixture
def held_lock(tmp_path: Path):
    """Yield (home, child, pid) with byte 0 of home/gateway.lock locked by child."""
    lock = tmp_path / LOCK_FILENAME
    child = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(lock)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
    )
    try:
        status, _, pid = child.stdout.readline().strip().partition(" ")
        assert status == "locked"
        # The pid the child itself reports: under a venv redirector Popen.pid
        # names the launcher, not the interpreter that took the lock.
        yield tmp_path, child, int(pid)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=30)


def _release(child: subprocess.Popen) -> None:
    child.stdin.write("\n")
    child.stdin.flush()
    assert child.stdout.readline().strip() == "released"


def test_lock_holder_reports_held_under_a_real_mandatory_lock(held_lock):
    home, child, holder_pid = held_lock

    with pytest.raises(LockProbeError) as excinfo:
        lock_holder(home)

    assert excinfo.value.held is True
    # The holder's pid IS in the file -- it is the byte-0 lock, not a missing
    # stamp, that keeps another handle from reading it while the lock is held.
    _release(child)
    assert (home / LOCK_FILENAME).read_text().strip() == str(holder_pid)


def test_lock_holder_reports_nobody_once_the_lock_is_released(held_lock):
    home, child, _pid = held_lock
    _release(child)

    holder = lock_holder(home)

    assert holder.pid is None


def test_gateway_pid_prints_held_under_a_real_mandatory_lock(held_lock, monkeypatch, capsys):
    home, _child, _pid = held_lock
    monkeypatch.setattr(cli_server, "config_dir", lambda: home)

    cli_server._gateway_pid()

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["holder"] == "held"
