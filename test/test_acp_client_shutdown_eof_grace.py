"""``AcpClient.shutdown()`` lets the agent exit on the stdin close before it kills.

Closing stdin is how an ACP client ends a session, and an agent tidies up on it:
kiro-cli removes the lock it wrote for the session. A SIGKILL sent in the same
breath as the close skips that step, and every closed session then leaves its
lock behind.

These tests run a real child in its own process group, because the contract is
what happens to real processes: the lock file is gone, and nothing that ignores
the close outlives the shutdown -- neither the agent, nor a child it left in its
group, nor one that was never recorded.

POSIX only. On Windows ``_kill_process`` drains the tree through pinned handles
before it reaches the pipes, so it never waits and these paths do not exist.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import textwrap
from unittest.mock import AsyncMock

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp import client_lifecycle
from kiro_crew.acp.client import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO, AcpClient
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, async_wait_until, await_until

pytestmark = pytest.mark.skipif(
    platform_compat.IS_WINDOWS,
    reason="POSIX process-group teardown; Windows drains pinned handles and never waits",
)

# A stand-in agent. It writes a lock, says it is ready, and then reads stdin to
# EOF. What it does next is the mode:
#   tidy      removes the lock after a short clean-up and exits
#   stubborn  ignores the close (and SIGTERM) and stays
#   litter    tidy, but leaves three processes behind that ignore SIGTERM: one
#             it started in its own group, one it started in a group of its
#             own, and one it double-forked into its own group, which the
#             kernel hands to init so no walk from the agent finds it
#   handoff   stubborn, with a grandchild in a group of its own whose parent
#             exits when the test says so: from then on no walk from the agent
#             finds the grandchild, and no signal to the agent's group reaches it
_AGENT = textwrap.dedent("""
    import os, signal, subprocess, sys, time

    mode, lock = sys.argv[1], sys.argv[2]
    stay = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)"
    with open(lock, "w") as f:
        f.write(str(os.getpid()))
    extra = ""
    if mode == "litter":
        quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        recorded = subprocess.Popen([sys.executable, "-c", stay], **quiet)
        escaped = subprocess.Popen([sys.executable, "-c", stay], start_new_session=True, **quiet)
        read_end, write_end = os.pipe()
        middle = os.fork()
        if middle == 0:
            adopted = subprocess.Popen([sys.executable, "-c", stay], **quiet)
            os.write(write_end, str(adopted.pid).encode())
            os._exit(0)
        os.waitpid(middle, 0)
        extra = " %d %d %s" % (recorded.pid, escaped.pid, os.read(read_end, 32).decode())
    if mode == "handoff":
        quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        read_end, write_end = os.pipe()
        parent = os.fork()
        if parent == 0:
            late = subprocess.Popen([sys.executable, "-c", stay], start_new_session=True, **quiet)
            os.write(write_end, str(late.pid).encode())
            while not os.path.exists(lock + ".go"):
                time.sleep(0.01)
            os._exit(0)
        extra = " %d %s" % (parent, os.read(read_end, 32).decode())
    if mode in ("stubborn", "handoff"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    print("ready" + extra, flush=True)
    while sys.stdin.readline():
        pass
    if mode in ("stubborn", "handoff"):
        time.sleep(600)
    time.sleep(0.2)  # the clean-up kiro-cli was measured to need before its lock is gone
    os.remove(lock)
    """)


@pytest.fixture
def started(tmp_path):
    """Start the stand-in agent behind a real ``AcpClient``; kill what is left afterwards."""
    script = tmp_path / "agent.py"
    script.write_text(_AGENT, encoding="utf-8")
    spawned: list[tuple[int, str | None]] = []

    async def _start(mode: str):
        lock = tmp_path / f"{mode}.lock"
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script),
            mode,
            str(lock),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            cwd=tmp_path,
        )
        spawned.append((proc.pid, platform_compat.get_process_start_id(proc.pid)))
        assert proc.stdout is not None
        line = await await_until(
            asyncio.ensure_future(proc.stdout.readline()), what="the agent's ready line"
        )
        words = line.decode().split()
        assert words[:1] == ["ready"], line
        others = [int(word) for word in words[1:]]
        spawned.extend((pid, platform_compat.get_process_start_id(pid)) for pid in others)
        client = AcpClient(work_dir=tmp_path)
        client._process = proc
        client._pid = proc.pid
        client._start_time = spawned[0][1]
        client._child_pids = {}
        return client, proc, lock, others

    yield _start

    for pid, start_id in spawned:
        # Only a pid that still is the process recorded at spawn: a number the
        # kernel has reused since is somebody else's.
        if start_id is not None and platform_compat.get_process_start_id(pid) == start_id:
            try:
                platform_compat.kill_pid(pid, platform_compat.SIGKILL)
            except OSError:
                pass


def _gone(pid: int, start_id: str | None) -> bool:
    """Killed, or not the process recorded: a zombie that init has not reaped is gone."""
    return (
        platform_compat.pid_is_zombie(pid) is True
        or platform_compat.get_process_start_id(pid) != start_id
    )


@pytest.mark.asyncio
async def test_shutdown_lets_the_agent_remove_its_lock(started, monkeypatch):
    """The reported leak: one lock left behind by every closed session."""
    # A lost-run bound, not the thing under test: the agent exits long before it.
    monkeypatch.setattr(client_lifecycle, "_EOF_EXIT_GRACE_SECS", FALLBACK_TIMEOUT_SECS)
    client, proc, lock, _ = await started("tidy")
    assert lock.exists()

    await client.shutdown()

    assert not lock.exists(), "the agent was killed before it could remove its lock"
    assert proc.returncode == 0, "the agent exited on the stdin close, it was not killed"


@pytest.mark.asyncio
async def test_shutdown_still_kills_an_agent_that_ignores_the_close(started, monkeypatch):
    """The wait is bounded: an agent that stays is killed when it runs out."""
    monkeypatch.setattr(
        client_lifecycle,
        "_EOF_EXIT_GRACE_SECS",
        0.05,  # flake-ok: this agent never exits, so the grace only sets the test's length
    )
    client, proc, _, _ = await started("stubborn")

    await client.shutdown()

    assert proc.returncode == -signal.SIGKILL
    assert client._kill_sent == (int(platform_compat.SIGKILL), "forced stop")


@pytest.mark.asyncio
async def test_what_an_exited_agent_left_behind_is_still_killed(started, monkeypatch):
    """An agent that exits on the close can leave processes behind.

    A kill sent while the agent is alive reaches them through its group. After
    the wait the agent may be gone first, and its number with it. So the group
    is signalled by the id read while the agent lived, once a recorded child
    still in it vouches for it: that reaches the member no walk from the agent
    ever saw. A recorded child in another group is killed by identity.
    """
    monkeypatch.setattr(client_lifecycle, "_EOF_EXIT_GRACE_SECS", FALLBACK_TIMEOUT_SECS)
    client, proc, lock, left = await started("litter")
    names = ("recorded child in the group", "recorded child in its own group", "adopted member")
    starts = [platform_compat.get_process_start_id(pid) for pid in left]
    assert len(left) == len(names) and None not in starts

    await client.shutdown()

    assert proc.returncode == 0 and not lock.exists()

    def _standing() -> list[str]:
        return [name for name, pid, start in zip(names, left, starts) if not _gone(pid, start)]

    await async_wait_until(lambda: not _standing(), off_loop=True, describe=_standing)


@pytest.mark.asyncio
async def test_a_cancel_during_the_wait_still_kills_a_child_that_left_the_tree(
    started, monkeypatch
):
    """The children recorded before the wait are the ones killed after a cancel.

    The grandchild is recorded when the stop starts. During the wait its parent
    exits, so a second walk from the agent does not find it, and it sits in a
    group of its own. Only the record taken before the wait still names it.
    """
    monkeypatch.setattr(client_lifecycle, "_EOF_EXIT_GRACE_SECS", FALLBACK_TIMEOUT_SECS)
    client, proc, lock, (parent, late) = await started("handoff")
    parent_start = platform_compat.get_process_start_id(parent)
    late_start = platform_compat.get_process_start_id(late)
    assert parent_start is not None and late_start is not None
    waiting = asyncio.Event()
    real_wait = proc.wait

    async def _wait():
        waiting.set()
        return await real_wait()

    proc.wait = _wait  # type: ignore[method-assign]

    stop = asyncio.ensure_future(client.shutdown())
    await await_until(waiting, what="shutdown reaching its wait for the agent to exit")
    lock.with_name(lock.name + ".go").touch()
    await async_wait_until(
        lambda: _gone(parent, parent_start),
        off_loop=True,
        describe=lambda: "the grandchild's parent is still running",
    )
    stop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop

    await async_wait_until(
        lambda: _gone(late, late_start),
        off_loop=True,
        describe=lambda: "the grandchild outlived the cancelled shutdown",
    )


@pytest.mark.asyncio
async def test_a_cancel_as_the_agent_exits_still_kills_what_it_left(started, monkeypatch):
    """A cancel that lands as the agent exits must not skip what the agent left behind."""
    monkeypatch.setattr(client_lifecycle, "_EOF_EXIT_GRACE_SECS", FALLBACK_TIMEOUT_SECS)
    client, proc, _, left = await started("litter")
    starts = [platform_compat.get_process_start_id(pid) for pid in left]
    assert left and None not in starts
    real_wait = proc.wait

    async def _exit_then_cancel():
        await real_wait()
        raise asyncio.CancelledError

    proc.wait = _exit_then_cancel  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await client.shutdown()

    def _standing() -> list[int]:
        return [pid for pid, start in zip(left, starts) if not _gone(pid, start)]

    await async_wait_until(lambda: not _standing(), off_loop=True, describe=_standing)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "waits"), [(ACP_BACKEND_KIRO, True), (ACP_BACKEND_CLAUDE, False)]
)
async def test_only_kiro_cli_is_given_the_wait(tmp_path, backend, waits):
    """The lock is kiro-cli's. Every other backend keeps the kill that does not wait."""
    client = AcpClient(work_dir=tmp_path)
    client._acp_backend = backend
    client._process = None
    client._pid = None
    client._child_pids = {}
    client._kill_process = AsyncMock()

    await client.shutdown()

    # Exact: a backend that does not wait gets the plain call.
    expected = {"force": True}
    if waits:
        expected["exit_grace"] = client_lifecycle._EOF_EXIT_GRACE_SECS
    assert client._kill_process.await_args.kwargs == expected


@pytest.mark.asyncio
async def test_a_claude_client_whose_kill_is_wrapped_still_shuts_down(tmp_path):
    """The claude idle reader wraps ``_kill_process`` in a function that takes only ``force``."""
    client = AcpClient(work_dir=tmp_path)
    client._acp_backend = ACP_BACKEND_CLAUDE
    client._process = None
    client._pid = None
    client._child_pids = {}
    client._install_idle_reader()

    await client.shutdown()

    assert client._session_id is None


@pytest.mark.asyncio
async def test_a_cancel_during_the_wait_still_kills_the_agent(started, monkeypatch):
    """Shutdown is when a cancel arrives, and the agent must not outlive it."""
    monkeypatch.setattr(client_lifecycle, "_EOF_EXIT_GRACE_SECS", FALLBACK_TIMEOUT_SECS)
    client, proc, _, _ = await started("stubborn")
    waiting = asyncio.Event()
    real_wait = proc.wait

    async def _wait():
        waiting.set()
        return await real_wait()

    proc.wait = _wait  # type: ignore[method-assign]

    stop = asyncio.ensure_future(client.shutdown())
    await await_until(waiting, what="shutdown reaching its wait for the agent to exit")
    stop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop

    assert await await_until(asyncio.ensure_future(real_wait()), what="the agent's exit") == (
        -signal.SIGKILL
    )
