"""A prompt turn records its late children while its leader is still alive.

The teardown refresh walks the leader's tree only while the leader reads back
as this run's process. A leader that exits on its own -- a crash, or an exit
after a turn forked a long-lived child -- leaves nothing to walk once teardown
starts, so late children were never recorded and the group they kept alive was
reported ``kill failed`` and never reaped. ``record_live_children_after_turn``
runs the same guarded walk at the END of each prompt turn, where the leader is
still live, so the kill handle a later teardown takes already carries them.

The POSIX case walks a real leader that forks a child and then exits before
teardown: after the turn's record, the recorded child still vouches for the
leaderless group it keeps alive. The helper's own guards (bound, swallow a
timeout or error, propagate a cancel, no-op on a dead leader) and the per-turn
wiring in both providers are pinned with lighter doubles.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import signal
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
    record_live_children_after_turn,
)
from kiro_crew.testing.wait import await_until

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="process groups are POSIX; Windows uses the tree pin"
)

#: Forks a child that stays the leader's descendant (the leader waits, alive).
_FORK_CHILD = "read go; sleep 60 & echo $!; wait"


def _client_for(process: asyncio.subprocess.Process) -> AcpClient:
    """An ACP client that owns *process* the way ``_spawn`` leaves it, with an empty record."""
    client = AcpClient.__new__(AcpClient)
    client._process = process
    client._pid = process.pid
    client._start_time = platform_compat.get_process_start_id(process.pid)
    client._child_pids = {}
    return client


def _handle_for(client: AcpClient) -> ProcessHandle:
    """The kill handle a teardown would take from a session running *client*."""
    session = SimpleNamespace(provider=SimpleNamespace(_client=client))
    return process_handle_of(session)


def _leader_gone(handle: ProcessHandle) -> ProcessHandle:
    """The same handle with a root whose identity fails to read back as this run's process."""
    return dataclasses.replace(handle, start_id="not-this-run")


@pytest.fixture
def _tracking_file(tmp_path: Path) -> Iterator[Path]:
    """The record persists like the startup snapshot; keep that file in tmp."""
    with patch("kiro_crew.session_pid.config_dir", return_value=tmp_path):
        yield tmp_path


@asynccontextmanager
async def _leader(script: str) -> AsyncIterator[asyncio.subprocess.Process]:
    """A leader in its own session running *script*; its whole group is killed on exit."""
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
        await await_until(asyncio.ensure_future(process.wait()), what="the leader's reap")


async def _fork_now(process: asyncio.subprocess.Process) -> int:
    """Have the leader fork now; return the new child pid."""
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(b"go\n")
    await process.stdin.drain()
    line = await await_until(
        asyncio.ensure_future(process.stdout.readline()), what="the leader's fork report"
    )
    return int(line.strip())


@posix_only
class TestATurnRecordsItsLateChildrenBeforeTheLeaderExits:
    @pytest.mark.asyncio
    async def test_a_turn_record_lets_a_late_child_vouch_once_the_leader_is_gone(
        self, _tracking_file: Path
    ) -> None:
        """Acceptance: a turn forks a long-lived child, then the leader exits.

        The per-turn walk records the child while the leader is alive, so the
        kill handle a later teardown takes (with the group id read while the
        group still stands) has a member that vouches for the leaderless group
        -- the leader's own exit is modelled by a root identity that fails to
        read back, the exact check that lets the root vouch.
        """
        async with _leader(_FORK_CHILD) as leader:
            client = _client_for(leader)
            child = await _fork_now(leader)

            # The turn ends here: record the late child while the leader lives.
            await record_live_children_after_turn(client)
            handle = _handle_for(client)

            assert child in handle.child_pids, "the late child is missing from the kill handle"
            assert handle.pgid == leader.pid
            assert (
                _group_member_vouches(_leader_gone(handle)) is True
            ), "a leaderless group the run's own late child keeps alive was refused"
            tracked = (_tracking_file / "kiro_pids.txt").read_text(encoding="utf-8")
            assert f"{child}:{leader.pid}:" in tracked, "the record did not persist the child"

    @pytest.mark.asyncio
    async def test_without_the_turn_record_the_group_cannot_vouch_once_the_leader_is_gone(
        self,
    ) -> None:
        """Pins what the turn record exists for: the startup record alone is empty."""
        async with _leader(_FORK_CHILD) as leader:
            client = _client_for(leader)
            await _fork_now(leader)

            handle = _handle_for(client)
            assert handle.child_pids == {}
            assert _group_member_vouches(_leader_gone(handle)) is False


class TestTheHelperIsGuardedAndBestEffort:
    @pytest.mark.asyncio
    async def test_a_record_that_hangs_is_abandoned_within_the_timeout(
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
            await record_live_children_after_turn(_Hangs())
        assert cancelled.is_set(), "the bound returned without ending the hung record"
        assert "timed out" in caplog.text

    @pytest.mark.asyncio
    async def test_a_record_that_raises_does_not_fail_the_turn(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class _Raises:
            async def refresh_process_tree(self) -> None:
                raise OSError("proc unreadable")

        await record_live_children_after_turn(_Raises())
        assert "failed" in caplog.text

    @pytest.mark.asyncio
    async def test_a_source_without_an_async_refresh_is_a_quiet_no_op(self) -> None:
        await record_live_children_after_turn(SimpleNamespace())
        await record_live_children_after_turn(SimpleNamespace(refresh_process_tree=None))
        await record_live_children_after_turn(SimpleNamespace(refresh_process_tree=lambda: None))

    @pytest.mark.asyncio
    async def test_a_cancellation_propagates_so_the_teardown_path_is_unchanged(self) -> None:
        class _Cancels:
            async def refresh_process_tree(self) -> None:
                raise asyncio.CancelledError

        # Propagated, not swallowed: the teardown caller releases its lease and
        # records the undecided kill in its own except-CancelledError handler,
        # and the turn-end caller runs this last in its finally after releasing,
        # so there is nothing left for a swallow to protect.
        with pytest.raises(asyncio.CancelledError):
            await record_live_children_after_turn(_Cancels())


class TestBothProvidersRecordAtTurnEnd:
    """Each provider's prompt turn runs the recorder for its own leader."""

    @pytest.mark.asyncio
    async def test_the_shared_provider_ends_the_turn_before_it_records(self) -> None:
        from kiro_crew.acp.session_provider import AcpSessionProvider

        order: list[str] = []

        async def _record(source: Any) -> None:
            order.append("record")

        async def _stream(*_a: Any, **_k: Any) -> AsyncIterator[Any]:
            if False:  # pragma: no cover - empty turn
                yield None

        provider = AcpSessionProvider.__new__(AcpSessionProvider)
        provider.essential_delivery = SimpleNamespace(stream=_stream)  # type: ignore[attr-defined]
        provider._handle = SimpleNamespace(prompt=lambda *a, **k: None)  # type: ignore[attr-defined]
        provider._claim_shared_turn = lambda: None  # type: ignore[method-assign]
        provider.reclaim = lambda: None  # type: ignore[method-assign]

        async def _end(_claim: Any) -> None:
            order.append("end")

        provider._end_shared_turn = _end  # type: ignore[method-assign]

        with patch(
            "kiro_crew.acp.session_provider.record_live_children_after_turn",
            side_effect=_record,
        ):
            async for _e in provider.stream("hi"):
                pass

        assert order == ["end", "record"], (
            "the shared turn must release its tenancy before it records, so the "
            "release is never delayed or skipped by the bounded walk"
        )

    @pytest.mark.asyncio
    async def test_the_dedicated_provider_records_its_client_at_turn_end(self) -> None:
        from kiro_crew.providers.acp import AcpProvider

        calls: list[Any] = []

        async def _record(source: Any) -> None:
            calls.append(source)

        async def _stream(*_a: Any, **_k: Any) -> AsyncIterator[Any]:
            if False:  # pragma: no cover - empty turn
                yield None

        client = SimpleNamespace(stream_events=lambda *a, **k: None)
        provider = AcpProvider.__new__(AcpProvider)
        provider._client = client  # type: ignore[attr-defined]
        provider.essential_delivery = SimpleNamespace(stream=_stream)  # type: ignore[attr-defined]

        async def _ready() -> None:
            return None

        provider._ready_confirmed_session = _ready  # type: ignore[method-assign]

        with patch(
            "kiro_crew.providers.acp_turns.record_live_children_after_turn",
            side_effect=_record,
        ):
            async for _e in provider.stream("hi"):
                pass

        assert calls == [client], "the dedicated turn did not record its own client"
