"""Teardown re-walks the live leader's tree before it takes its kill handle.

The ACP client records its descendants once, after session initialise. A turn
that runs a command which forks its own children adds processes that record
never saw, and a leaderless group may only be signalled while a RECORDED member
vouches for it (``process_identity._group_member_vouches``). So when the leader
exited first, a group only late children kept alive was reported as a failed
kill and never reaped. ``AcpClient.refresh_process_tree`` plus
``process_identity.refresh_live_process_tree`` record those children just
before the handle is taken; the vouching rule itself is unchanged.

The POSIX cases walk real processes: a leader in its own session that forks a
child only after the client's record was read, and an orphan in the same group
that the leader cannot reach. The leader's exit is modelled by a root identity
that fails to read back -- the exact check that lets the root vouch --
rather than by killing it: on some sandboxed hosts the session's members are
taken down with its leader, which would leave nothing to vouch for.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import signal
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp.client import AcpClient
from kiro_crew.process_identity import (
    ProcessHandle,
    _group_member_vouches,
    process_handle_of,
    refresh_live_process_tree,
)
from kiro_crew.testing.wait import async_wait_until, await_until

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="process groups are POSIX; Windows uses the tree pin"
)

_KEY = "subagent:late-children"

#: Forks a child that stays the leader's descendant.
_FORK_CHILD = "read go; sleep 60 & echo $!; wait"
#: Forks a grandchild whose parent exits at once: an orphan in the leader's
#: group that no walk from the leader reaches.
_FORK_ORPHAN = "read go; sh -c 'sleep 60 & echo $!'; exec sleep 60"


def _client_for(process: asyncio.subprocess.Process) -> AcpClient:
    """An ACP client that owns *process* the way ``_spawn`` leaves it, with an empty record."""
    client = AcpClient.__new__(AcpClient)
    client._process = process
    client._pid = process.pid
    client._start_time = platform_compat.get_process_start_id(process.pid)
    client._child_pids = {}
    return client


def _sessions_holding(client: Any) -> SimpleNamespace:
    """A session map with one live session under ``_KEY`` whose provider runs *client*."""
    session = SimpleNamespace(provider=SimpleNamespace(_client=client))
    return SimpleNamespace(_sessions={_KEY: session})


def _leader_gone(handle: ProcessHandle) -> ProcessHandle:
    """The same handle with a root whose identity fails to read back as this run's process."""
    return dataclasses.replace(handle, start_id="not-this-run")


@pytest.fixture
def _tracking_file(tmp_path: Path) -> Iterator[Path]:
    """The refresh persists its records like the startup snapshot; keep that file in tmp."""
    with patch("kiro_crew.session_pid.config_dir", return_value=tmp_path):
        yield tmp_path


@asynccontextmanager
async def _leader(script: str) -> AsyncIterator[asyncio.subprocess.Process]:
    """A leader in its own session running *script*; its whole group is killed on exit.

    An ``async with`` helper, not an async fixture, so the process lives and is
    reaped on the test's own event loop.
    """
    process = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        script,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        yield process
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        await process.wait()


async def _fork_now(process: asyncio.subprocess.Process) -> int:
    """Have the leader fork now, after the client's record was read; the new pid."""
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(b"go\n")
    await process.stdin.drain()
    line = await await_until(
        asyncio.ensure_future(process.stdout.readline()), what="the leader's fork report"
    )
    return int(line.strip())


async def _until_leader_runs(process: asyncio.subprocess.Process, name: str) -> None:
    """Wait until the leader's command name reads *name* (it has exec'd)."""

    def _runs_name() -> bool:
        ps = subprocess.run(
            ["ps", "-o", "comm=", "-p", str(process.pid)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return os.path.basename(ps.stdout.strip()) == name

    await async_wait_until(
        _runs_name, off_loop=True, describe=lambda: f"leader {process.pid} exec'd into {name}"
    )


@posix_only
class TestLateChildrenVouchAfterARefresh:
    @pytest.mark.asyncio
    async def test_a_child_started_after_the_first_snapshot_vouches_once_refreshed(
        self, _tracking_file: Path
    ) -> None:
        async with _leader(_FORK_CHILD) as leader:
            sessions = _sessions_holding(_client_for(leader))
            child = await _fork_now(leader)

            await refresh_live_process_tree(sessions, _KEY)
            handle = process_handle_of(sessions._sessions[_KEY])

            assert child in handle.child_pids, "the late child is missing from the kill handle"
            assert handle.pgid == leader.pid
            assert (
                _group_member_vouches(_leader_gone(handle)) is True
            ), "a leaderless group the run's own late child keeps alive was refused"
            tracked = (_tracking_file / "kiro_pids.txt").read_text(encoding="utf-8")
            assert f"{child}:{leader.pid}:" in tracked, "the refresh did not persist the child"

    @pytest.mark.asyncio
    async def test_without_the_refresh_the_same_group_is_refused(self) -> None:
        """Pins what the refresh exists for: the startup record alone cannot vouch."""
        async with _leader(_FORK_CHILD) as leader:
            sessions = _sessions_holding(_client_for(leader))
            await _fork_now(leader)

            handle = process_handle_of(sessions._sessions[_KEY])

            assert handle.child_pids == {}
            assert _group_member_vouches(_leader_gone(handle)) is False

    @pytest.mark.asyncio
    async def test_a_group_member_the_leader_cannot_reach_is_not_recorded_and_cannot_vouch(
        self, _tracking_file: Path
    ) -> None:
        async with _leader(_FORK_ORPHAN) as leader:
            sessions = _sessions_holding(_client_for(leader))
            orphan = await _fork_now(leader)
            assert os.getpgid(orphan) == leader.pid
            # The leader execs only after the intermediate shell has exited, so
            # from then on the orphan is out of the leader's reach.
            await _until_leader_runs(leader, "sleep")

            await refresh_live_process_tree(sessions, _KEY)
            handle = process_handle_of(sessions._sessions[_KEY])

            assert orphan not in handle.child_pids, "a pid the leader cannot reach was recorded"
            assert _group_member_vouches(_leader_gone(handle)) is False


class _Process:
    def __init__(self, pid: int, returncode: int | None) -> None:
        self.pid = pid
        self.returncode = returncode


def _fake_client(*, returncode: int | None, start: str = "111") -> AcpClient:
    client = AcpClient.__new__(AcpClient)
    client._process = _Process(4242, returncode)  # type: ignore[assignment]
    client._pid = 4242
    client._start_time = start
    client._child_pids = {}
    return client


class TestRefreshOnlyWalksAVerifiedLiveLeader:
    @pytest.mark.asyncio
    async def test_an_exited_leader_gets_no_walk(self) -> None:
        client = _fake_client(returncode=0)
        # The pid still reads back as ours: only the exit status may stop the walk.
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="111"),
            patch("kiro_crew.acp.runtime_process_tree._get_child_pids") as walk,
        ):
            await client.refresh_process_tree()
        walk.assert_not_called()
        assert client._child_pids == {}

    @pytest.mark.asyncio
    async def test_a_recycled_leader_pid_gets_no_walk(self) -> None:
        client = _fake_client(returncode=None)
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="999"),
            patch("kiro_crew.acp.runtime_process_tree._get_child_pids") as walk,
        ):
            await client.refresh_process_tree()
        walk.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_leader_recycled_during_the_walk_records_nothing(self) -> None:
        client = _fake_client(returncode=None)
        reads = iter(["111", "999"])
        with (
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=lambda pid: next(reads) if pid == 4242 else "c",
            ),
            patch("kiro_crew.acp.runtime_process_tree._get_child_pids", return_value=[5151]),
            patch(
                "kiro_crew.acp.runtime_process_tree._capture_child_records",
                return_value={5151: ("c", b"sleep")},
            ),
            patch("kiro_crew.acp.client._replace_child_pids") as track,
        ):
            await client.refresh_process_tree()
        assert client._child_pids == {}, "children walked from a recycled pid were recorded"
        track.assert_not_called()


class TestRefreshDropsAChildWhosePidWasReused:
    """A descendant that exits between the walk and the capture is never recorded."""

    @staticmethod
    async def _refresh(walks: list[list[int]], child_start: list[str]) -> tuple[AcpClient, Any]:
        client = _fake_client(returncode=None)
        walk_answers = iter(walks)
        child_reads = iter(child_start)
        with (
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=lambda pid: "111" if pid == 4242 else next(child_reads),
            ),
            patch(
                "kiro_crew.acp.runtime_process_tree._get_child_pids",
                side_effect=lambda _pid: next(walk_answers),
            ),
            patch(
                "kiro_crew.acp.runtime_process_tree._capture_child_records",
                return_value={5151: ("stranger", b"sleep")},
            ),
            patch("kiro_crew.acp.client._replace_child_pids") as track,
        ):
            await client.refresh_process_tree()
        return client, track

    @pytest.mark.asyncio
    async def test_a_pid_missing_from_the_second_walk_is_dropped(self) -> None:
        client, track = await self._refresh([[5151], []], ["stranger"])
        assert client._child_pids == {}, "a pid that left the leader's tree was recorded"
        track.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_pid_whose_start_id_changed_since_the_capture_is_dropped(self) -> None:
        client, track = await self._refresh([[5151], [5151]], ["ours-now"])
        assert client._child_pids == {}, "a capture that does not read back was recorded"
        track.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_child_confirmed_twice_is_recorded_with_its_captured_id(self) -> None:
        client, track = await self._refresh([[5151], [5151]], ["stranger"])
        assert client._child_pids == {5151: ("stranger", b"sleep")}
        track.assert_called_once()
        assert track.call_args.args[0] == {5151: ("stranger", b"sleep")}
        assert track.call_args.kwargs == {"parent_pid": 4242}


class TestRefreshPersistsBeforeItPublishes:
    """A child is recorded in memory only once its tracking line is written."""

    @pytest.mark.asyncio
    async def test_a_respawn_during_the_write_publishes_nothing(self) -> None:
        client = _fake_client(returncode=None)

        def _write(*_a: Any, **_k: Any) -> bool:
            client._process = _Process(4242, None)  # type: ignore[assignment]
            return True

        with (
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=lambda pid: "111" if pid == 4242 else "c",
            ),
            patch("kiro_crew.acp.runtime_process_tree._get_child_pids", return_value=[5151]),
            patch(
                "kiro_crew.acp.runtime_process_tree._capture_child_records",
                return_value={5151: ("c", b"sleep")},
            ),
            patch("kiro_crew.acp.client._replace_child_pids", side_effect=_write),
        ):
            await client.refresh_process_tree()
        assert client._child_pids == {}, "a respawned client gained the old process's child"

    @pytest.mark.asyncio
    async def test_a_recorded_pid_the_tree_reused_is_captured_again(self) -> None:
        client = _fake_client(returncode=None)
        client._child_pids = {5151: ("old", b"node")}
        with (
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=lambda pid: "111" if pid == 4242 else "new",
            ),
            patch("kiro_crew.acp.runtime_process_tree._get_child_pids", return_value=[5151]),
            patch(
                "kiro_crew.acp.runtime_process_tree._capture_child_records",
                return_value={5151: ("new", b"sleep")},
            ),
            patch("kiro_crew.acp.client._replace_child_pids", return_value=True),
        ):
            await client.refresh_process_tree()
        assert client._child_pids == {5151: ("new", b"sleep")}, "the stale record was kept"

    @staticmethod
    async def _refresh(client: AcpClient, written: bool) -> Any:
        with (
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=lambda pid: "111" if pid == 4242 else "c",
            ),
            patch("kiro_crew.acp.runtime_process_tree._get_child_pids", return_value=[5151]),
            patch(
                "kiro_crew.acp.runtime_process_tree._capture_child_records",
                return_value={5151: ("c", b"sleep")},
            ),
            patch("kiro_crew.acp.client._replace_child_pids", return_value=written) as track,
        ):
            await client.refresh_process_tree()
        return track

    @pytest.mark.asyncio
    async def test_a_failed_write_leaves_the_child_unrecorded_and_retried(self) -> None:
        client = _fake_client(returncode=None)

        track = await self._refresh(client, written=False)
        track.assert_called_once()
        assert client._child_pids == {}, "a child whose tracking write failed was published"

        await self._refresh(client, written=True)
        assert client._child_pids == {5151: ("c", b"sleep")}, "the next refresh did not retry it"


class TestARefreshNeverStallsTeardown:
    @pytest.mark.asyncio
    async def test_a_refresh_that_hangs_is_abandoned_within_the_timeout(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cancelled = asyncio.Event()

        class _Hangs:
            async def refresh_process_tree(self) -> None:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

        with patch("kiro_crew.process_identity.TREE_REFRESH_TIMEOUT", 0.05):
            await refresh_live_process_tree(_sessions_holding(_Hangs()), _KEY)
        assert cancelled.is_set(), "the bound returned without ending the hung refresh"
        assert "timed out" in caplog.text

    @pytest.mark.asyncio
    async def test_a_refresh_that_raises_does_not_fail_teardown(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class _Raises:
            async def refresh_process_tree(self) -> None:
                raise OSError("proc unreadable")

        await refresh_live_process_tree(_sessions_holding(_Raises()), _KEY)
        assert "refresh" in caplog.text and "failed" in caplog.text

    @pytest.mark.asyncio
    async def test_no_live_session_or_no_client_is_a_quiet_no_op(self) -> None:
        await refresh_live_process_tree(SimpleNamespace(_sessions={}), _KEY)
        await refresh_live_process_tree(SimpleNamespace(), _KEY)
        await refresh_live_process_tree(_sessions_holding(None), _KEY)


class TestTheKiroBackendRefreshReachesTheRuntime:
    """A kiro-backend session's handle reads the runtime's record, so the runtime is walked."""

    @pytest.mark.asyncio
    async def test_the_provider_hands_the_refresh_to_its_runtime(self) -> None:
        from kiro_crew.acp.runtime import AcpRuntime
        from kiro_crew.acp.session_provider import AcpSessionProvider

        runtime = AcpRuntime.__new__(AcpRuntime)
        runtime._child_pids = {}

        async def _snapshot() -> None:
            runtime._child_pids[5151] = ("c", b"sleep")

        provider = AcpSessionProvider.__new__(AcpSessionProvider)
        provider._runtime = runtime
        with patch.object(runtime, "_snapshot_descendants", side_effect=_snapshot) as snap:
            await refresh_live_process_tree(_sessions_holding(provider), _KEY)

        snap.assert_awaited_once_with()
        assert provider._child_pids == {
            5151: ("c", b"sleep")
        }, "the kiro-backend handle does not carry the runtime's refreshed child"
