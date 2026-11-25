"""A child that exits while ``initialize`` is in flight ends the start with its stderr.

The adapter exits before it answers ``initialize``: before it reads the frame, so the
write meets a closed pipe, or after, so the read of the answer meets the end of
stdout. Either way ``_initialize_session`` raises one non-transient ``AcpError``
naming the exit code and the child's stderr, which the chat does not read as a lost
connection to retry. A closed pipe waits for the child's reap first, and a signal
exit names the signal. A pipe that breaks while the child still runs, or under a
kill this client sends during that wait, keeps its ``AcpProcessDied``.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

from kiro_crew.acp import client_start
from kiro_crew.acp.client import AcpClient, AcpError, AcpProcessDied
from kiro_crew.llm_helpers import acp_error_is_pipe_death, acp_error_is_transient
from kiro_crew.testing.wait import async_wait_until, default_timeout, finished

#: The child's last words. "not running" is a phrase the chat reads as a dead
#: process in an error's text, so the verdict must not come from the tail.
_STDERR_LINE = "error: engine not running: failed to load engine binary"
_SAY_AND_EXIT = (
    "import sys\n"
    f"sys.stderr.write({_STDERR_LINE!r} + '\\n')\n"
    "sys.stderr.flush()\n"
    "sys.exit(3)\n"
)
#: Says why, then reads the ``initialize`` frame, so the read of its answer meets EOF.
_EXITS_AFTER_READING = (
    "import sys\n"
    f"sys.stderr.write({_STDERR_LINE!r} + '\\n')\n"
    "sys.stderr.flush()\n"
    "sys.stdin.readline()\n"
    "sys.exit(3)\n"
)
#: Closes its stdin and keeps running, so the write meets a closed pipe on a live child.
_CLOSES_STDIN_AND_RUNS = (
    "import os, sys, time\n"
    "os.close(0)\n"
    "sys.stderr.write('stdin closed\\n')\n"
    "sys.stderr.flush()\n"
    "time.sleep(60)\n"
)
#: Closes its stdin, says why, and exits once the descriptor in ``argv[1]`` is closed,
#: so the write meets a closed pipe while the child is still unreaped.
_CLOSES_STDIN_THEN_EXITS = (
    "import os, sys\n"
    "os.close(0)\n"
    f"sys.stderr.write({_STDERR_LINE!r} + '\\n')\n"
    "sys.stderr.flush()\n"
    "os.read(int(sys.argv[1]), 1)\n"
    "sys.exit(3)\n"
)
#: Says why, then dies on a signal it raises itself (default action and unblocked, whatever
#: disposition the worker passed down).
_SAY_AND_SIGTERM = (
    "import os, signal, sys\n"
    f"sys.stderr.write({_STDERR_LINE!r} + '\\n')\n"
    "sys.stderr.flush()\n"
    "signal.signal(signal.SIGTERM, signal.SIG_DFL)\n"
    "signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})\n"
    "os.kill(os.getpid(), signal.SIGTERM)\n"
)

# Each case's child exiting, dying on a signal or closing its stdin IS the case under
# test, so one shared module-scoped child cannot serve them; the group lands every such
# child on one worker so their cold starts never run side by side (D8).
pytestmark = pytest.mark.xdist_group(name="acp_initialize_death_children")


async def _client_over(
    script: str, tmp_path: Path, *, args: tuple[str, ...] = (), pass_fds: tuple[int, ...] = ()
) -> AcpClient:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,  # flake-ok: one child per case, its own exit is what the case asserts
        "-c",
        script,
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        pass_fds=pass_fds,
    )
    client = AcpClient(work_dir=tmp_path)
    client._process = proc
    client._pid = proc.pid
    client._stderr_task = asyncio.ensure_future(client._drain_stderr(proc.stderr))
    return client


async def _stderr_read(client: AcpClient, line: str) -> None:
    # The tail comes from the lines already drained, so the start's own bounded
    # stderr settle never decides what it carries.
    await async_wait_until(
        lambda: line in client._stderr_lines, describe=lambda: list(client._stderr_lines)
    )


async def _close(client: AcpClient) -> None:
    proc = client._process
    assert proc is not None
    if proc.returncode is None:
        proc.kill()
    await finished(proc.wait(), "the child's reap")
    task = client._stderr_task
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["before-reading", "after-reading"])
async def test_a_child_exiting_during_initialize_ends_the_start_with_its_stderr(
    route: str, tmp_path: Path
) -> None:
    script = _SAY_AND_EXIT if route == "before-reading" else _EXITS_AFTER_READING
    client = await _client_over(script, tmp_path)
    try:
        await _stderr_read(client, _STDERR_LINE)
        if route == "before-reading":
            # Reaped before the write, so the write meets the closed pipe.
            assert client._process is not None
            await finished(client._process.wait(), "the child's exit")
        with pytest.raises(AcpError) as raised:
            await finished(client._initialize_session(), "the handshake")
    finally:
        await _close(client)
    exc = raised.value
    assert str(exc) == f"the agent exited during initialize (code=3) — {_STDERR_LINE}"
    assert not isinstance(exc, AcpProcessDied)
    assert acp_error_is_transient(exc) is False
    assert acp_error_is_pipe_death(exc) is False


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="closes its own stdin: POSIX pipe semantics")
async def test_a_closed_pipe_waits_for_the_reap_before_it_reads_the_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The bound must not expire under load: the child exits as soon as it is released.
    monkeypatch.setattr(client_start, "_INIT_EXIT_REAP_SECS", default_timeout())
    release_r, release_w = os.pipe()
    try:
        client = await _client_over(
            _CLOSES_STDIN_THEN_EXITS, tmp_path, args=(str(release_r),), pass_fds=(release_r,)
        )
    finally:
        os.close(release_r)
    proc = client._process
    assert proc is not None
    real_wait = proc.wait

    async def wait_after_release() -> int:
        # The child is unreaped when the write fails; the handler's reap wait is
        # what lets it exit.
        nonlocal release_w
        if release_w >= 0:
            os.close(release_w)
            release_w = -1
        return await real_wait()

    proc.wait = wait_after_release  # type: ignore[method-assign]
    try:
        await _stderr_read(client, _STDERR_LINE)
        with pytest.raises(AcpError) as raised:
            await finished(client._initialize_session(), "the handshake")
    finally:
        if release_w >= 0:
            os.close(release_w)
            release_w = -1
        await _close(client)
    assert str(raised.value) == f"the agent exited during initialize (code=3) — {_STDERR_LINE}"
    assert not isinstance(raised.value, AcpProcessDied)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX signal exit")
async def test_a_child_killed_by_a_signal_names_it(tmp_path: Path) -> None:
    client = await _client_over(_SAY_AND_SIGTERM, tmp_path)
    try:
        await _stderr_read(client, _STDERR_LINE)
        assert client._process is not None
        await finished(client._process.wait(), "the child's exit")
        with pytest.raises(AcpError) as raised:
            await finished(client._initialize_session(), "the handshake")
    finally:
        await _close(client)
    assert str(raised.value) == (
        "the agent exited during initialize (code=-15, SIGTERM, not sent by this client)"
        f" — {_STDERR_LINE}"
    )


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="closes its own stdin: POSIX pipe semantics")
async def test_a_pipe_broken_under_a_running_child_stays_a_process_death(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reap wait is meant to expire here: the child keeps running. Not raising
    # keeps this control runnable on a client without the reap wait.
    monkeypatch.setattr(client_start, "_INIT_EXIT_REAP_SECS", 0.0, raising=False)
    client = await _client_over(_CLOSES_STDIN_AND_RUNS, tmp_path)
    try:
        await _stderr_read(client, "stdin closed")
        with pytest.raises(AcpProcessDied, match="pipe broken"):
            await finished(client._initialize_session(), "the handshake")
        assert client._process is not None and client._process.returncode is None
    finally:
        await _close(client)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="closes its own stdin: POSIX pipe semantics")
async def test_a_kill_this_client_sends_during_the_reap_wait_keeps_the_process_death(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A teardown closes the pipes before it signals the child, so its kill lands
    # while the start waits for the reap.
    monkeypatch.setattr(client_start, "_INIT_EXIT_REAP_SECS", default_timeout(), raising=False)
    client = await _client_over(_CLOSES_STDIN_AND_RUNS, tmp_path)
    proc = client._process
    assert proc is not None
    real_wait = proc.wait

    async def wait_after_a_kill() -> int:
        if proc.returncode is None:
            client._kill_sent = (int(signal.SIGKILL), "forced stop")
            proc.kill()
        return await real_wait()

    proc.wait = wait_after_a_kill  # type: ignore[method-assign]
    try:
        await _stderr_read(client, "stdin closed")
        with pytest.raises(AcpProcessDied, match="pipe broken"):
            await finished(client._initialize_session(), "the handshake")
    finally:
        await _close(client)
