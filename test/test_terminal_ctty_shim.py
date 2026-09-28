"""Controlling terminal acquired AFTER ``exec`` instead of in a fork of the gateway.

The defect: the terminal handler asked CPython for the PTY's ``TIOCSCTTY`` through
``preexec_fn``, which is implemented by ``fork()``-ing the whole multi-GB,
~120-thread gateway and running Python in the clone before ``exec``. Measured at
3GB resident that blocked the event loop for ~107ms per terminal open, and a clone
that cannot reach ``exec`` blocks it without bound, because the parent waits for
that exec inside an un-awaitable ``os.read(errpipe_read, ...)`` on the loop thread.

The fix asks the post-exec shim for it with ``--ctty-fd=``. These tests pin the
three properties that make the fix real rather than a rename: the flag reaches the
shim, the shim claims the terminal for the exec'd image, and the resulting shell
can still be interrupted with Ctrl+C.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from unittest.mock import patch

import pytest

from kiro_crew import _spawn_exec_shim as shim
from kiro_crew import sandbox
from kiro_crew.sandbox import (
    RLIMIT_PROFILE_NONE,
    RLIMIT_PROFILE_TOOL,
    spawn_shim_argv,
)

pty = pytest.importorskip("pty", reason="POSIX pseudo-terminals only")

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX controlling terminals only")


@pytest.fixture(autouse=True)
def _clear_shim_cache():
    """The argv prefix is cached per (profile, ctty); these tests vary both."""
    sandbox._SHIM_ARGV_CACHE.clear()
    yield
    sandbox._SHIM_ARGV_CACHE.clear()


# --------------------------------------------------------------------------
# The flag reaches the shim
# --------------------------------------------------------------------------


class TestCttyArgvContract:
    @posix_only
    def test_no_limits_profile_alone_still_skips_the_interpreter_hop(self):
        # Unchanged behaviour, and the reason the terminal got nothing from the
        # shim before: an interactive shell carries no rlimits and no OOM bias, so
        # without a ctty request there is nothing to run post-exec.
        assert spawn_shim_argv(RLIMIT_PROFILE_NONE) == ()

    @posix_only
    def test_ctty_request_makes_the_no_limits_profile_carry_a_prefix(self):
        prefix = spawn_shim_argv(RLIMIT_PROFILE_NONE, ctty_fd=0)
        assert prefix, "a ctty request must not be dropped as 'nothing to do'"
        assert prefix[0] == sys.executable
        assert prefix[1:4] == ("-I", "-S", "-c")
        assert "--ctty-fd=0" in prefix
        assert prefix[-1] == "--"
        # No limits and no bias for this profile, so the ctty flag is the only one.
        assert not [f for f in prefix if f.startswith("--rlimits=")]
        assert "--oom-bias" not in prefix

    @posix_only
    def test_cache_does_not_serve_a_ctty_prefix_for_a_plain_request(self):
        # One cache, two different answers for the same profile: a shared key
        # would hand a terminal's prefix to a spawn that has no terminal, or the
        # reverse.
        with_ctty = spawn_shim_argv(RLIMIT_PROFILE_TOOL, ctty_fd=0)
        without = spawn_shim_argv(RLIMIT_PROFILE_TOOL)
        assert "--ctty-fd=0" in with_ctty
        assert "--ctty-fd=0" not in without
        assert spawn_shim_argv(RLIMIT_PROFILE_TOOL, ctty_fd=0) == with_ctty

    def test_windows_returns_no_prefix_even_for_a_ctty_request(self):
        # POSIX controlling terminals do not exist there, and that branch uses
        # ConPTY rather than a fork.
        with patch.object(sandbox.os, "name", "nt"):
            assert spawn_shim_argv(RLIMIT_PROFILE_NONE, ctty_fd=0) == ()


class TestShimCttyOptionParsing:
    def test_bad_fd_value_is_refused_rather_than_ignored(self, capsys):
        assert shim.main(["--ctty-fd=nope", "--", "/bin/true"]) == 127
        assert "--ctty-fd=" in capsys.readouterr().err

    def test_negative_fd_is_refused(self, capsys):
        assert shim.main(["--ctty-fd=-1", "--", "/bin/true"]) == 127
        assert "--ctty-fd=" in capsys.readouterr().err

    def test_failure_to_claim_the_terminal_does_not_exec_the_command(self, capsys):
        # Fail closed: a shell with no controlling terminal looks like a working
        # terminal until Ctrl+C does nothing, so exec'ing anyway would ship a
        # silent substitution.
        execs: list[object] = []
        with (
            patch.object(shim.os, "login_tty", side_effect=OSError(1, "nope")),
            patch.object(shim.os, "execv", lambda *a: execs.append(a)),
        ):
            assert shim.main(["--ctty-fd=0", "--", "/bin/true"]) == 127
        assert execs == [], "the command must not run without the terminal it asked for"
        assert "controlling terminal" in capsys.readouterr().err

    def test_claim_precedes_exec_when_it_succeeds(self):
        order: list[str] = []
        with (
            patch.object(shim.os, "login_tty", lambda fd: order.append(f"login_tty({fd})")),
            patch.object(shim.os, "execv", lambda *a: (order.append("execv"), exec_stop())[1]),
        ):
            shim.main(["--ctty-fd=3", "--", "/bin/true"])
        assert order == ["login_tty(3)", "execv"]

    def test_missing_login_tty_is_reported_not_silently_skipped(self, capsys):
        execs: list[object] = []
        with (
            patch.object(shim.os, "login_tty", None),
            patch.object(shim.os, "execv", lambda *a: execs.append(a)),
        ):
            assert shim.main(["--ctty-fd=0", "--", "/bin/true"]) == 127
        assert execs == []
        assert "login_tty" in capsys.readouterr().err


def exec_stop() -> None:
    """Stand in for ``execv`` not returning, without replacing the test process."""
    raise OSError(2, "stop here")


# --------------------------------------------------------------------------
# The property the ioctl exists for, against a real PTY
# --------------------------------------------------------------------------


async def _read_until(fd: int, needle: bytes, seen: bytearray, deadline: float) -> bool:
    """Accumulate from *fd* until *needle* appears or *deadline* passes.

    *fd* is put in non-blocking mode and read directly on the loop. Handing a
    blocking ``os.read`` to ``run_in_executor`` and bounding it with
    ``asyncio.wait_for`` does not work here: a started executor future cannot be
    cancelled, so each expired timeout leaves a pool thread parked in that read
    for the rest of the process, holding the PTY open. The case that leaks worst
    is the one this file needs most -- the control test, where the child
    deliberately never writes again, so every poll would strand another worker.
    """
    os.set_blocking(fd, False)
    while True:
        if needle in bytes(seen):
            return True
        if time.monotonic() >= deadline:
            return False
        try:
            chunk: bytes | None = os.read(fd, 4096)
        except BlockingIOError:
            chunk = None  # nothing buffered yet
        except OSError:
            break  # PTY closed, or the child is gone (EIO on Linux)
        if chunk is None:
            await asyncio.sleep(0.02)
            continue
        if not chunk:
            break  # EOF
        seen.extend(chunk)
    return needle in bytes(seen)


# Stands in for the interactive shell. A shell is the wrong instrument for this
# assertion: a PTY echoes whatever is typed at it, so a marker written INTO the
# terminal comes back out of it whether or not any signal was delivered, and
# interactive Bash abandons the rest of a command line when SIGINT arrives rather
# than running it. This child reports the signal itself, so the marker can only
# appear if SIGINT actually reached the foreground process group.
_SIGINT_REPORTER = (
    "import signal,sys\n"
    "def on_sigint(*_):\n"
    "    sys.stdout.write('SIGINT_REACHED_THE_CHILD\\n')\n"
    "    sys.stdout.flush()\n"
    "    sys.exit(0)\n"
    "signal.signal(signal.SIGINT, on_sigint)\n"
    "sys.stdout.write('CHILD_READY\\n')\n"
    "sys.stdout.flush()\n"
    "signal.pause()\n"
)


@posix_only
@pytest.mark.asyncio
async def test_shim_child_owns_the_terminal_so_ctrl_c_reaches_it():
    """End to end: real PTY, real shim, and a real Ctrl+C byte.

    Asserting on ``tcgetpgrp`` and on delivered SIGINT rather than on the ioctl
    having been called is the point. The PTY's foreground process group is what
    the kernel consults to deliver SIGINT, so this is the property a user feels,
    and it fails if the terminal is merely inherited instead of claimed.
    """
    prefix = spawn_shim_argv(RLIMIT_PROFILE_NONE, ctty_fd=0)
    assert prefix, "shim source must be available for this test to mean anything"

    master, worker = pty.openpty()  # wokeignore:rule=master
    proc = await asyncio.create_subprocess_exec(
        *prefix,
        sys.executable,
        "-I",
        "-S",
        "-c",
        _SIGINT_REPORTER,
        stdin=worker,
        stdout=worker,
        stderr=worker,
        start_new_session=True,
        # No preexec_fn: that is the defect. The claim happens after exec.
    )
    os.close(worker)
    seen = bytearray()
    try:
        assert await _read_until(
            master, b"CHILD_READY", seen, time.monotonic() + 20  # wokeignore:rule=master
        ), "the shim did not exec the child"

        # execv keeps the pid, so the handler's teardown (kill_process_tree, the
        # foreground-pgrp comparison, the cwd probe) still addresses the child.
        fg = os.tcgetpgrp(master)  # wokeignore:rule=master
        assert fg == proc.pid, "the PTY's foreground process group must be the child"

        os.write(master, b"\x03")  # Ctrl+C  # wokeignore:rule=master
        assert await _read_until(
            master,  # wokeignore:rule=master
            b"SIGINT_REACHED_THE_CHILD",
            seen,
            time.monotonic() + 20,
        ), "Ctrl+C did not deliver SIGINT to the foreground process group"
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        os.close(master)  # wokeignore:rule=master


@posix_only
@pytest.mark.asyncio
async def test_without_the_ctty_request_the_child_does_not_own_the_terminal():
    """The control that proves the flag is what carries the property.

    Same spawn, same ``start_new_session``, no ``--ctty-fd``: the child gets a
    session but never claims the terminal, so it does not become the PTY's
    foreground process group and a Ctrl+C byte has no route to it. This is what
    shipping the 'just drop preexec_fn' half of the fix would have produced.

    No Ctrl+C byte is written here on purpose. With no session owning the PTY the
    kernel has no foreground group of ours to signal, and the one thing worse than
    a weak assertion would be delivering SIGINT to the test runner's own process
    group to find that out.
    """
    master, worker = pty.openpty()  # wokeignore:rule=master
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-S",
        "-c",
        _SIGINT_REPORTER,
        stdin=worker,
        stdout=worker,
        stderr=worker,
        start_new_session=True,
    )
    os.close(worker)
    seen = bytearray()
    try:
        assert await _read_until(
            master, b"CHILD_READY", seen, time.monotonic() + 20  # wokeignore:rule=master
        )
        try:
            fg = os.tcgetpgrp(master)  # wokeignore:rule=master
        except OSError:
            fg = -1
        assert fg != proc.pid, (
            "without --ctty-fd the child must NOT be the terminal's foreground " "process group"
        )
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        os.close(master)  # wokeignore:rule=master
