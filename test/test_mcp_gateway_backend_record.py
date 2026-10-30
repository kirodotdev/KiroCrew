"""The ``<socket>.backends`` record: its format, its identity-pinned reap, and
the boot-time reap a new gatewayd runs once it holds the singleton lock.

A pooled backend is a session leader, so it outlives a gatewayd that dies
without draining it. The record is what lets someone reclaim it, and these tests
pin the two halves of that contract: a backend the record vouches for is reaped
on every death path (not only the supervisor's own SIGKILL), and a pid the record
cannot vouch for -- recycled, gone, or written without a start id -- is never
signalled.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import platform_compat, process_identity
from kiro_crew.mcp_gateway import backend_record
from kiro_crew.testing.ids import unallocatable_pids
from kiro_crew.testing.wait import async_wait_until, finished

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")


async def _session_leader_with_child(
    spawned: list[asyncio.subprocess.Process],
) -> asyncio.subprocess.Process:
    """A session leader that ignores SIGTERM and has a child in its own group,
    the shape of a wrapper-launched MCP backend. Registered in *spawned* the
    moment it exists, so teardown reaps it whatever fails after."""
    proc = await asyncio.create_subprocess_exec(
        "/bin/sh",
        "-c",
        "trap '' TERM; sleep 600 & wait",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    spawned.append(proc)
    # Wait for the child so the group holds two processes before anything reads it.
    await async_wait_until(
        lambda: len(platform_compat.process_descendants(proc.pid)) >= 1,
        off_loop=True,
    )
    return proc


@contextlib.asynccontextmanager
async def _spawned() -> AsyncIterator[list[asyncio.subprocess.Process]]:
    """Every process a test starts, each a session leader. Entered before the
    first spawn, so a failed later setup step cannot leak an earlier one; on
    exit each group is SIGKILLed and waited a bounded time, so a stuck exit
    fails here instead of hanging."""
    procs: list[asyncio.subprocess.Process] = []
    try:
        yield procs
    finally:
        for proc in procs:
            await _cleanup(proc)


async def _group_gone(pgid: int, timeout: float = 10.0) -> bool:
    """True once no process, zombie included, is left in group *pgid*.

    A killed leader is this test's own child and stays a zombie until asyncio's
    child watcher collects it. macOS answers signal 0 to a group holding only
    zombies with EPERM rather than ESRCH, so EPERM means "not gone yet" and
    only ESRCH proves the group is empty."""

    def _check() -> bool:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False

    try:
        await async_wait_until(_check, timeout=timeout, off_loop=True)
        return True
    except AssertionError:
        return False


_CLEANUP_WAIT_SECS = 10.0


def _record_for(pid: int) -> str:
    """The record line a backend spawned as *pid* would publish."""
    return backend_record.render([(pid, platform_compat.get_process_start_id(pid))])


async def _cleanup(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    if proc.returncode is None:
        await finished(proc.wait(), "process wait")


# ── format ───────────────────────────────────────────────────────────────


def test_render_writes_each_pid_with_its_start_id() -> None:
    assert backend_record.render([(111, "9001"), (222, "9002")]) == "111 9001\n222 9002"


def test_render_writes_a_pid_without_an_identity_bare() -> None:
    assert backend_record.render([(4242, None)]) == "4242"


def test_render_never_reads_a_live_identity() -> None:
    """A read at heartbeat time can meet a recycled pid; render must only
    publish the identity the backend captured at spawn."""
    with patch.object(platform_compat, "get_process_start_id", side_effect=AssertionError):
        assert backend_record.render([(111, "spawn-id")]) == "111 spawn-id"


def test_parse_reads_new_and_legacy_lines_and_drops_junk() -> None:
    raw = "111 9001\n222\n\nnot-a-pid 5\n1 77\n0\n333 abc extra\n"
    assert backend_record.parse(raw) == [(111, "9001"), (222, None), (333, "abc")]


def test_runtime_reconcile_reads_only_pids_from_the_new_format(tmp_path: Path, monkeypatch) -> None:
    from kiro_crew import runtime_reconcile as rr

    sock = tmp_path / "gateway.sock"
    backend_record.record_path(sock).write_text("111 9001\n222 9002\n", encoding="utf-8")
    monkeypatch.setattr(rr, "configured_socket_path", lambda: sock)
    # A whitespace split would also answer 9001 and 9002 as hosted backends.
    assert rr._mcp_backend_pids() == {111, 222}


# ── reap: identity ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reap_skips_a_recycled_pid(tmp_path: Path) -> None:
    path = tmp_path / "gw.sock.backends"
    path.write_text("111 old-start\n", encoding="utf-8")
    with (
        # The POSIX branch: Windows checks identity inside the pinned kill,
        # which test_windows_reap_uses_the_handle_pinned_kill_off_the_loop covers.
        patch.object(platform_compat, "IS_WINDOWS", False),
        patch.object(platform_compat, "get_process_start_id", return_value="new-start"),
        patch.object(platform_compat, "kill_process_group") as kill,
        patch.object(platform_compat, "kill_process_tree_pinned") as kill_win,
    ):
        assert await backend_record.reap(path, reason="test") == 0
    kill.assert_not_called()
    kill_win.assert_not_called()
    assert not path.exists(), "the record of a dead generation is consumed either way"


@pytest.mark.asyncio
async def test_reap_skips_a_legacy_bare_pid(tmp_path: Path) -> None:
    path = tmp_path / "gw.sock.backends"
    path.write_text("111\n", encoding="utf-8")
    with (
        # The POSIX branch: Windows checks identity inside the pinned kill,
        # which test_windows_reap_uses_the_handle_pinned_kill_off_the_loop covers.
        patch.object(platform_compat, "IS_WINDOWS", False),
        patch.object(platform_compat, "get_process_start_id", return_value="anything"),
        patch.object(platform_compat, "kill_process_group") as kill,
        patch.object(platform_compat, "kill_process_tree_pinned") as kill_win,
    ):
        assert await backend_record.reap(path, reason="test") == 0
    kill.assert_not_called()
    kill_win.assert_not_called()


@pytest.mark.asyncio
async def test_reap_skips_a_pid_that_is_gone(tmp_path: Path) -> None:
    path = tmp_path / "gw.sock.backends"
    path.write_text("111 s\n", encoding="utf-8")
    with (
        # The POSIX branch: Windows checks identity inside the pinned kill,
        # which test_windows_reap_uses_the_handle_pinned_kill_off_the_loop covers.
        patch.object(platform_compat, "IS_WINDOWS", False),
        patch.object(platform_compat, "get_process_start_id", return_value=None),
        patch.object(platform_compat, "kill_process_group") as kill,
        patch.object(platform_compat, "kill_process_tree_pinned") as kill_win,
    ):
        signalled = await backend_record.reap(path, reason="test")
    assert signalled == 0
    # The record describes a generation that is gone, so it is dropped either way.
    assert not path.exists()
    kill.assert_not_called()
    kill_win.assert_not_called()


@pytest.mark.asyncio
async def test_reap_sigkills_a_matching_pid(tmp_path: Path) -> None:
    path = tmp_path / "gw.sock.backends"
    path.write_text("111 s1\n222 s2\n", encoding="utf-8")
    with (
        patch.object(platform_compat, "IS_WINDOWS", False),
        patch.object(
            process_identity, "isolated_group_of", side_effect=lambda pid, start: pid
        ) as pinned,
        patch.object(platform_compat, "kill_process_group", return_value=True) as kill,
    ):
        assert await backend_record.reap(path, reason="test") == 2
    assert [c.args for c in pinned.call_args_list] == [(111, "s1"), (222, "s2")]
    assert [c.args for c in kill.call_args_list] == [
        (111, platform_compat.SIGKILL),
        (222, platform_compat.SIGKILL),
    ]


@pytest.mark.asyncio
async def test_reap_with_no_record_is_a_no_op(tmp_path: Path) -> None:
    assert await backend_record.reap(tmp_path / "absent.backends", reason="test") == 0


@pytest.mark.asyncio
async def test_reap_reads_the_record_off_the_event_loop(tmp_path: Path) -> None:
    """The manager reaps on the dashboard gateway's loop, and the record sits
    beside the socket in the data home: a read that blocks there would stall
    every session, so it runs on a worker thread."""
    import threading

    path = tmp_path / "gw.sock.backends"
    path.write_text("111 s1\n", encoding="utf-8")
    loop_thread = threading.get_ident()
    read_threads: list[int] = []
    real_read_text = Path.read_text

    def _read_text(self: Path, *args, **kwargs) -> str:
        read_threads.append(threading.get_ident())
        return real_read_text(self, *args, **kwargs)

    with (
        patch.object(Path, "read_text", _read_text),
        patch.object(platform_compat, "IS_WINDOWS", False),
        patch.object(process_identity, "isolated_group_of", side_effect=lambda pid, start: pid),
        patch.object(platform_compat, "kill_process_group", return_value=True) as kill,
    ):
        assert await backend_record.reap(path, reason="test") == 1
    assert len(read_threads) == 1
    assert read_threads[0] != loop_thread
    assert [c.args for c in kill.call_args_list] == [(111, platform_compat.SIGKILL)]
    assert not path.exists()


@pytest.mark.asyncio
async def test_reap_of_an_unreadable_record_is_a_no_op(tmp_path: Path) -> None:
    """An OSError from the worker-thread read still reaches the caller as 0,
    never as a raise, and leaves the record in place."""
    path = tmp_path / "gw.sock.backends"
    path.write_text("111 s1\n", encoding="utf-8")
    with (
        patch.object(Path, "read_text", side_effect=PermissionError("denied")),
        patch.object(platform_compat, "kill_process_group") as kill,
    ):
        assert await backend_record.reap(path, reason="test") == 0
    kill.assert_not_called()
    assert path.exists()


@posix_only
@pytest.mark.asyncio
async def test_reap_kills_a_real_term_ignoring_group_and_spares_a_recycled_one(
    tmp_path: Path,
) -> None:
    """On real processes: the matching group goes, children included; a live
    process whose start id differs from the record is left untouched."""
    async with _spawned() as spawned:
        ours = await _session_leader_with_child(spawned)
        stranger = await _session_leader_with_child(spawned)
        path = tmp_path / "gw.sock.backends"
        path.write_text(
            _record_for(ours.pid) + f"\n{stranger.pid} not-its-start-id\n",
            encoding="utf-8",
        )
        assert await backend_record.reap(path, reason="test") == 1
        assert await _group_gone(ours.pid), "the recorded group, child included, is reaped"
        os.killpg(stranger.pid, 0)  # raises if the stranger's group was signalled
        assert stranger.returncode is None


@posix_only
@pytest.mark.asyncio
async def test_group_gone_polls_past_the_eperm_macos_gives_a_zombie_only_group() -> None:
    """macOS answers signal 0 to a group whose members are all zombies with EPERM.
    The helper must keep polling through it and still demand ESRCH."""
    real_killpg = os.killpg
    probes: list[int] = []

    def killpg(pgid: int, sig: int) -> None:
        if sig == 0:
            probes.append(pgid)
            if len(probes) <= 3:
                raise PermissionError(1, "Operation not permitted")
        real_killpg(pgid, sig)

    async with _spawned() as spawned:
        leader = await _session_leader_with_child(spawned)
        real_killpg(leader.pid, signal.SIGKILL)
        with patch.object(os, "killpg", killpg):
            assert await _group_gone(leader.pid)
        assert len(probes) > 3, "the EPERM answers were polled past, not skipped"


# ── who reaps ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_supervisor_reap_goes_through_the_identity_check(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    from kiro_crew.mcp_gateway import manager as mgr

    sock = tmp_path / "gateway.sock"
    backend_record.record_path(sock).write_text("111 old\n", encoding="utf-8")
    manager = object.__new__(mgr.GatewayManager)
    manager._spec = MagicMock()
    manager._spec.socket_path = str(sock)
    with (
        # The POSIX branch: Windows checks identity inside the pinned kill,
        # which test_windows_reap_uses_the_handle_pinned_kill_off_the_loop covers.
        patch.object(platform_compat, "IS_WINDOWS", False),
        patch.object(platform_compat, "get_process_start_id", return_value="recycled"),
        patch.object(platform_compat, "kill_process_group") as kill,
        patch.object(platform_compat, "kill_process_tree_pinned") as kill_win,
    ):
        await manager._reap_orphaned_backends()
    assert not backend_record.record_path(sock).exists()
    kill.assert_not_called()
    kill_win.assert_not_called()


@posix_only
@pytest.mark.asyncio
async def test_a_new_daemon_reaps_the_record_a_crashed_one_left(
    tmp_path: Path,
    short_sock_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The crash / OOM-kill / outside-SIGKILL path: no supervisor kills
    anything, a replacement daemon simply starts on the same socket. It must
    reap the previous generation's backends before its first heartbeat
    overwrites the record that names them."""
    from kiro_crew.mcp_gateway import gatewayd as gw
    from kiro_crew.mcp_gateway import transport

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    sock = short_sock_dir / "gw.sock"
    async with _spawned() as spawned:
        orphan = await _session_leader_with_child(spawned)
        backend_record.record_path(sock).write_text(_record_for(orphan.pid), encoding="utf-8")
        env = {**os.environ, "PYTHONPATH": str(Path(gw.__file__).resolve().parents[2])}
        daemon = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import sys, asyncio\n"
            "from kiro_crew.mcp_gateway import gatewayd as g\n"
            "sys.exit(asyncio.run(g._amain(sys.argv[1:])))",
            "--socket",
            str(sock),
            "--idle-timeout-secs",
            "60",
            "--max-backends",
            "1",
            "--owner-pid",
            str(os.getpid()),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            cwd=str(home),
            start_new_session=True,
        )
        spawned.append(daemon)

        async def _sock_bound() -> bool:
            assert daemon.returncode is None, "daemon exited before binding"
            return await asyncio.to_thread(transport.endpoint_exists, sock)

        await async_wait_until(_sock_bound)
        assert await _group_gone(
            orphan.pid
        ), "the crashed generation's backend survived the new daemon's boot"


@posix_only
@pytest.mark.asyncio
async def test_a_daemon_that_loses_the_lock_leaves_the_record_alone(short_sock_dir: Path) -> None:
    """The record belongs to whichever daemon holds the lock. A loser that
    reaped it would kill the backends of a daemon that is serving."""
    from kiro_crew.mcp_gateway import gatewayd as gw
    from kiro_crew.mcp_gateway import transport

    sock = short_sock_dir / "gw.sock"
    await asyncio.to_thread(transport.prepare_dir, sock)
    async with _spawned() as spawned:
        held = transport.acquire_singleton_lock(sock)
        assert held is not None
        try:
            live = await _session_leader_with_child(spawned)
            record = backend_record.record_path(sock)
            record.write_text(_record_for(live.pid), encoding="utf-8")
            await asyncio.wait_for(
                gw.run_gatewayd(
                    sock, max_backends=1, idle_timeout_secs=60, stop_event=asyncio.Event()
                ),
                timeout=30,
            )
            assert record.exists()
            os.killpg(live.pid, 0)
            assert live.returncode is None
        finally:
            os.close(held)


# ── detach: the boot reap runs off the bind path ─────────────────────────


def test_detach_renames_the_record_aside_without_rewriting_it(tmp_path: Path) -> None:
    path = tmp_path / "gw.sock.backends"
    path.write_text("111 s\n", encoding="utf-8")
    inode = path.stat().st_ino
    (pending,) = backend_record.detach(path)
    assert pending.name.startswith("gw.sock.backends.reaping.")
    assert not path.exists(), "the heartbeat must find no record to overwrite"
    assert pending.stat().st_ino == inode, "a rename, not a copy: no bytes are rewritten"
    assert backend_record.parse(pending.read_text(encoding="utf-8")) == [(111, "s")]


def test_detach_returns_an_unfinished_reap_beside_the_new_record(tmp_path: Path) -> None:
    path = tmp_path / "gw.sock.backends"
    unfinished = tmp_path / "gw.sock.backends.reaping.1.1"
    unfinished.write_text("111 a\n", encoding="utf-8")
    path.write_text("222 b\n", encoding="utf-8")
    pending = backend_record.detach(path)
    assert unfinished in pending and len(pending) == 2
    assert (
        unfinished.read_text(encoding="utf-8") == "111 a\n"
    ), "an earlier record is never rewritten"


def test_a_failed_detach_leaves_the_record_exactly_as_it_was(tmp_path: Path) -> None:
    path = tmp_path / "gw.sock.backends"
    unfinished = tmp_path / "gw.sock.backends.reaping.1.1"
    unfinished.write_text("111 a\n", encoding="utf-8")
    path.write_text("222 b\n", encoding="utf-8")
    with (
        patch.object(backend_record.os, "replace", side_effect=OSError(28, "No space left")),
        pytest.raises(OSError),
    ):
        backend_record.detach(path)
    assert path.read_text(encoding="utf-8") == "222 b\n"
    assert unfinished.read_text(encoding="utf-8") == "111 a\n"


@posix_only
@pytest.mark.asyncio
async def test_a_daemon_that_cannot_move_the_record_aside_refuses_to_start(
    tmp_path: Path, short_sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Serving over a record still at its path would overwrite it on the first
    heartbeat, or unlink it on a clean shutdown. The daemon must not bind, must
    leave the record as it was, and must release the lock for a retry."""
    from kiro_crew.mcp_gateway import gatewayd as gw
    from kiro_crew.mcp_gateway import transport

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    sock = short_sock_dir / "gw.sock"
    await asyncio.to_thread(transport.prepare_dir, sock)
    record = backend_record.record_path(sock)
    record.write_text("222 b\n", encoding="utf-8")
    with (
        patch.object(backend_record.os, "replace", side_effect=OSError(28, "No space left")),
        pytest.raises(OSError),
    ):
        await asyncio.wait_for(
            gw.run_gatewayd(sock, max_backends=1, idle_timeout_secs=60, stop_event=asyncio.Event()),
            timeout=30,
        )
    assert record.read_text(encoding="utf-8") == "222 b\n"
    assert not await asyncio.to_thread(transport.endpoint_exists, sock)
    lock = transport.acquire_singleton_lock(sock)
    assert lock is not None, "the refused daemon must release the singleton lock"
    os.close(lock)


def test_detach_with_nothing_left_behind(tmp_path: Path) -> None:
    assert backend_record.detach(tmp_path / "gw.sock.backends") == []


@posix_only
@pytest.mark.asyncio
async def test_a_slow_boot_reap_does_not_delay_the_bind(
    tmp_path: Path, short_sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manager gives a fresh daemon a few seconds to bind. A reap that is
    slow (one taskkill per entry on Windows) must not spend that budget, and a
    reap interrupted by shutdown must leave its list for the next boot."""
    from kiro_crew.mcp_gateway import gatewayd as gw
    from kiro_crew.mcp_gateway import transport

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    sock = short_sock_dir / "gw.sock"
    await asyncio.to_thread(transport.prepare_dir, sock)
    backend_record.record_path(sock).write_text(f"{os.getpid()} s\n", encoding="utf-8")
    started = asyncio.Event()

    async def _hang(*_a: object, **_kw: object) -> bool:
        started.set()
        await asyncio.Event().wait()  # never set: hangs until shutdown cancels it
        return True

    stop = asyncio.Event()
    with (patch.object(backend_record, "_kill_pinned", side_effect=_hang),):
        daemon = asyncio.create_task(
            gw.run_gatewayd(sock, max_backends=1, idle_timeout_secs=60, stop_event=stop)
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=30)

            async def _sock_bound() -> bool:
                assert not daemon.done(), "daemon exited before binding"
                return await asyncio.to_thread(transport.endpoint_exists, sock)

            await async_wait_until(_sock_bound)
        finally:
            stop.set()
            await asyncio.wait_for(daemon, timeout=60)
    record = backend_record.record_path(sock)
    assert list(record.parent.glob(f"{record.name}.reaping.*")), "an interrupted reap is kept"


@pytest.mark.asyncio
async def test_windows_reap_uses_the_handle_pinned_kill_off_the_loop(tmp_path: Path) -> None:
    """On Windows the identity check and the terminate must be one pinned
    operation (a compare followed by a pid-addressed taskkill leaves a recycle
    window), and it blocks, so it runs off the event loop."""
    import threading

    path = tmp_path / "gw.sock.backends"
    path.write_text("111 1234\n", encoding="utf-8")
    seen: list[tuple[int, str, int, bool]] = []
    loop_thread = threading.get_ident()

    def _pinned(pid: int, start: str, sig: int) -> bool:
        seen.append((pid, start, sig, threading.get_ident() != loop_thread))
        return True

    with (
        patch.object(platform_compat, "IS_WINDOWS", True),
        patch.object(platform_compat, "kill_process_tree_pinned", side_effect=_pinned),
        patch.object(platform_compat, "kill_process_group") as group,
    ):
        assert await backend_record.reap(path, reason="test") == 1
    assert seen == [(111, "1234", platform_compat.SIGKILL, True)]
    group.assert_not_called()


def test_pool_publishes_each_backends_spawn_identity_across_all_three_sets() -> None:
    """Pooled, draining and connection-private backends all outlive a SIGKILLed
    daemon, so all three reach the record, each with its own spawn identity."""
    from types import SimpleNamespace

    from kiro_crew.mcp_gateway.pool import BackendPool

    pool = BackendPool(max_backends=4)
    p1, p2, p3 = unallocatable_pids(3)
    pooled = SimpleNamespace(pid=p1, start_id="a")
    draining = SimpleNamespace(pid=p2, start_id="b")
    private = SimpleNamespace(pid=p3, start_id=None)
    gone = SimpleNamespace(pid=None, start_id="x")
    pool._backends["k"] = pooled  # type: ignore[assignment]
    pool._backends["g"] = gone  # type: ignore[assignment]
    pool._draining.append(SimpleNamespace(backend=draining))  # type: ignore[arg-type]
    pool._exclusive["t"] = private  # type: ignore[assignment]
    with patch.object(platform_compat, "get_process_start_id", side_effect=AssertionError):
        assert pool.live_backend_identities() == [(p1, "a"), (p2, "b"), (p3, None)]
