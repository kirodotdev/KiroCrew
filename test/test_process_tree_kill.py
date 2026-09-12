"""Tests for process tree killing in session.reset() and subagent._sigkill_session().

Covers the killpg + escaped child sweep logic added to fix orphaned
kiro-cli sessions.
"""

from __future__ import annotations

import contextlib
import signal
import sys
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager
from kiro_crew.subagent import SubagentManager

# ── Helpers ──


def _make_provider(
    pid: int, child_pids: dict[int, int | None] | None = None, start_time: int | None = 100
):
    """Create a mock provider with a _client that has _pid, _child_pids, _start_time."""
    provider = AsyncMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = MagicMock(return_value=0.0)
    client = MagicMock()
    client._pid = pid
    client._child_pids = child_pids or {}
    client._start_time = start_time
    provider._client = client
    return provider


def _provider_factory(provider: AsyncMock):
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        return provider

    return factory


def _mock_sessions_with_provider(provider: AsyncMock) -> MagicMock:
    sessions = MagicMock()
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions._sessions = {}
    return sessions


def _mock_ctx_builder() -> MagicMock:
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("msg", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = False
    return ctx


# ── session.reset() tests ──


class TestResetProcessTreeKill:
    """Tests for session.reset() process tree cleanup."""

    @pytest.fixture
    def cfg(self):
        c = KiroCrewConfig()
        c.session.timeout_secs = 2
        return c

    @pytest.mark.asyncio
    async def test_reset_killpg_on_surviving_process(self, cfg):
        """reset() uses killpg when root PID survives shutdown."""
        provider = _make_provider(pid=12345, child_pids={12346: 100, 12347: 200})
        mgr = SessionManager(cfg, provider_factory=_provider_factory(provider))
        await mgr.get_or_create("t1")

        with (
            patch("kiro_crew.session.os.kill") as mock_kill,
            patch("kiro_crew.session.os.killpg") as mock_killpg,
            patch("kiro_crew.session.os.getpgid", return_value=12345),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children") as mock_sweep,
        ):
            # os.kill(pid, 0) succeeds → process survived shutdown
            mock_kill.return_value = None
            mock_killpg.return_value = None
            await mgr.reset("t1")

        provider.shutdown.assert_awaited_once()
        mock_killpg.assert_called_once_with(12345, signal.SIGKILL)
        mock_sweep.assert_called_once()

    @pytest.mark.asyncio
    async def test_reset_fallback_kill_when_killpg_fails(self, cfg):
        """reset() falls back to os.kill when killpg raises OSError."""
        provider = _make_provider(pid=12345)
        mgr = SessionManager(cfg, provider_factory=_provider_factory(provider))
        await mgr.get_or_create("t1")

        with (
            patch("kiro_crew.session.os.kill") as mock_kill,
            patch("kiro_crew.session.os.killpg", side_effect=OSError),
            patch("kiro_crew.session.os.getpgid", return_value=12345),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
        ):
            mock_kill.return_value = None
            await mgr.reset("t1")

        # First call: os.kill(pid, 0) to check alive
        # Second call: os.kill(pid, SIGKILL) fallback
        kill_calls = [c for c in mock_kill.call_args_list if c[0][1] == signal.SIGKILL]
        assert len(kill_calls) == 1
        assert kill_calls[0][0][0] == 12345

    @pytest.mark.asyncio
    async def test_reset_merges_fresh_child_scan(self, cfg):
        """reset() merges stored _child_pids with fresh _get_child_pids scan."""
        provider = _make_provider(pid=12345, child_pids={12346: (100, b"node")})
        mgr = SessionManager(cfg, provider_factory=_provider_factory(provider))
        await mgr.get_or_create("t1")

        with (
            patch("kiro_crew.session.os.kill", side_effect=ProcessLookupError),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[12347, 12348]),
            patch("kiro_crew.acp.client._get_start_time", return_value=999),
            patch("kiro_crew.acp.client._read_basename", return_value=b"node"),
            patch("kiro_crew.acp.client._kill_escaped_children") as mock_sweep,
        ):
            await mgr.reset("t1")

        provider.shutdown.assert_awaited_once()
        # Sweep runs even when root PID is dead (ProcessLookupError) because
        # children in different PGIDs may outlive the root.
        mock_sweep.assert_called_once()
        swept = mock_sweep.call_args[0][0]
        assert 12346 in swept  # from stored _child_pids
        assert 12347 in swept  # from fresh scan
        assert 12348 in swept  # from fresh scan
        assert swept[12347] == (999, b"node")  # (start_time, basename) from fresh scan

    @pytest.mark.asyncio
    async def test_reset_skips_kill_for_non_int_pid(self, cfg):
        """reset() skips kill logic when _pid is not an int (mock objects)."""
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        # _client._pid is an AsyncMock (not int) — should be skipped
        mgr = SessionManager(cfg, provider_factory=_provider_factory(provider))
        await mgr.get_or_create("t1")

        with patch("kiro_crew.session.os.kill") as mock_kill:
            await mgr.reset("t1")

        mock_kill.assert_not_called()
        provider.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reset_skips_kill_for_zero_pid(self, cfg):
        """reset() skips kill logic when _pid is 0 (kernel scheduler)."""
        provider = _make_provider(pid=0)
        mgr = SessionManager(cfg, provider_factory=_provider_factory(provider))
        await mgr.get_or_create("t1")

        with patch("kiro_crew.session.os.kill") as mock_kill:
            await mgr.reset("t1")

        mock_kill.assert_not_called()
        provider.shutdown.assert_awaited_once()


# ── subagent._sigkill_session() tests ──


class TestSigkillSessionProcessTree:
    """Tests for SubagentManager._sigkill_session() process tree cleanup."""

    def _make_manager(
        self,
        pid: int,
        child_pids: dict[int, int | None] | None = None,
        start_time: int | None = 100,
    ):
        provider = _make_provider(pid, child_pids, start_time=start_time)
        sessions = _mock_sessions_with_provider(provider)
        # Put a session in the internal dict so _sigkill_session can find it
        mock_session = MagicMock()
        mock_session.provider = provider
        sessions._sessions = {"subagent:test1": mock_session}
        mgr = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder(),
            on_done=AsyncMock(),
            on_event=AsyncMock(),
            is_yolo=lambda: True,
        )
        return mgr

    @pytest.mark.asyncio
    async def test_sigkill_uses_killpg(self):
        """_sigkill_session uses killpg to kill the process group.

        This helper is async; on POSIX kill_process_tree_async dispatches
        inline to kill_process_tree -> os.killpg, so the os.killpg patch still
        exercises the real path.
        """
        mgr = self._make_manager(pid=54321, child_pids={54322: 100})

        with (
            patch("kiro_crew.subagent.os.killpg") as mock_killpg,
            patch("kiro_crew.subagent.os.getpgid", return_value=54321),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children") as mock_sweep,
            patch("kiro_crew.acp.client._get_start_time", return_value=100),
            patch("kiro_crew.acp.client._is_our_child", return_value=True),
        ):
            await mgr._sigkill_session("subagent:test1")

        mock_killpg.assert_called_once_with(54321, signal.SIGKILL)
        mock_sweep.assert_called_once()

    @pytest.mark.asyncio
    async def test_sigkill_fallback_on_killpg_failure(self):
        """_sigkill_session falls back to os.kill when killpg fails."""
        mgr = self._make_manager(pid=54321)

        with (
            patch("kiro_crew.subagent.os.killpg", side_effect=ProcessLookupError),
            patch("kiro_crew.subagent.os.kill") as mock_kill,
            patch("kiro_crew.subagent.os.getpgid", return_value=54321),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("kiro_crew.acp.client._get_start_time", return_value=100),
            patch("kiro_crew.acp.client._is_our_child", return_value=True),
        ):
            await mgr._sigkill_session("subagent:test1")

        mock_kill.assert_called_once_with(54321, signal.SIGKILL)

    @pytest.mark.asyncio
    async def test_sigkill_merges_child_pids(self):
        """_sigkill_session merges stored and fresh child PIDs."""
        mgr = self._make_manager(pid=54321, child_pids={54322: 100})

        with (
            patch("kiro_crew.subagent.os.killpg"),
            patch("kiro_crew.subagent.os.getpgid", return_value=54321),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[54323]),
            patch("kiro_crew.acp.client._get_start_time", return_value=200),
            patch("kiro_crew.acp.client._read_basename", return_value=b"node"),
            patch("kiro_crew.acp.client._is_our_child", return_value=True),
            patch("kiro_crew.acp.client._kill_escaped_children") as mock_sweep,
        ):
            await mgr._sigkill_session("subagent:test1")

        # Sweep should receive merged dict: stored 54322 + fresh 54323
        swept = mock_sweep.call_args[0][0]
        assert 54322 in swept
        assert 54323 in swept

    @pytest.mark.asyncio
    async def test_sigkill_skips_killpg_on_recycled_pid(self):
        """_sigkill_session skips killpg but sweeps stored children when PID recycled."""
        mgr = self._make_manager(pid=54321, child_pids={54322: 100})

        with (
            patch("kiro_crew.subagent.os.killpg") as mock_killpg,
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._get_start_time", return_value=100),
            patch("kiro_crew.acp.client._is_our_child", return_value=False),
            patch("kiro_crew.acp.client._kill_escaped_children") as mock_sweep,
        ):
            await mgr._sigkill_session("subagent:test1")

        mock_killpg.assert_not_called()
        mock_sweep.assert_called_once()
        assert 54322 in mock_sweep.call_args[0][0]  # stored children swept

    @pytest.mark.asyncio
    async def test_sigkill_sweeps_children_when_pid_already_dead(self):
        """_sigkill_session skips killpg but sweeps children when PID is dead."""
        mgr = self._make_manager(pid=54321, child_pids={54322: 100}, start_time=None)

        with (
            patch("kiro_crew.subagent.os.killpg") as mock_killpg,
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._get_start_time", return_value=None),
            patch("kiro_crew.acp.client._kill_escaped_children") as mock_sweep,
        ):
            await mgr._sigkill_session("subagent:test1")

        mock_killpg.assert_not_called()
        mock_sweep.assert_called_once()

    @pytest.mark.asyncio
    async def test_sigkill_noop_when_no_session(self):
        """_sigkill_session returns early when session not found."""
        sessions = MagicMock()
        sessions._sessions = {}
        mgr = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder(),
            on_done=AsyncMock(),
            on_event=AsyncMock(),
            is_yolo=lambda: True,
        )

        with patch("kiro_crew.subagent.os.killpg") as mock_killpg:
            await mgr._sigkill_session("subagent:nonexistent")

        mock_killpg.assert_not_called()

    @pytest.mark.asyncio
    async def test_sigkill_noop_when_no_pid(self):
        """_sigkill_session returns early when client has no PID."""
        provider = AsyncMock()
        provider._client = MagicMock()
        provider._client._pid = None
        sessions = MagicMock()
        mock_session = MagicMock()
        mock_session.provider = provider
        sessions._sessions = {"subagent:test1": mock_session}
        mgr = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder(),
            on_done=AsyncMock(),
            on_event=AsyncMock(),
            is_yolo=lambda: True,
        )

        with patch("kiro_crew.subagent.os.killpg") as mock_killpg:
            await mgr._sigkill_session("subagent:test1")

        mock_killpg.assert_not_called()


# ── Saved-group incarnation gate ──


@contextlib.contextmanager
def _leaderless(pgid: int, members: dict[int, tuple[str | None, int | None]]) -> Iterator[None]:
    """A populated process group whose leader has been reaped.

    ``members`` maps each LIVE pid to the start identity and process group it
    currently reads back as, which is what lets a caller express a genuine
    survivor, a recycled pid, a member that ``setsid``'d out of the group, and an
    unreadable one. ``pgid`` itself is never a member, so the leader reads dead --
    the shape our own surviving tree and a stranger's both have.
    """

    def _pid_exists(pid: int) -> bool:
        return pid in members

    def _start_id(pid: int) -> str | None:
        entry = members.get(pid)
        return entry[0] if entry else None

    def _pgroup_of(pid: int) -> int | None:
        entry = members.get(pid)
        return entry[1] if entry else None

    with (
        patch("kiro_crew.platform_compat.pgroup_exists", return_value=True),
        patch("kiro_crew.platform_compat.pid_exists", side_effect=_pid_exists),
        patch("kiro_crew.platform_compat.get_process_start_id", side_effect=_start_id),
        patch("kiro_crew.platform_compat.pgroup_of", side_effect=_pgroup_of),
    ):
        yield


@contextlib.contextmanager
def _leader_alive(
    pgid: int,
    leader_start_id: str | None,
    members: dict[int, tuple[str | None, int | None]],
    leader_pgroup: int | None = None,
) -> Iterator[list[int]]:
    """A group whose leader is alive and reads back *leader_start_id*.

    ``leader_start_id=None`` makes the leader's identity unreadable and a
    ``leader_pgroup`` other than *pgid* makes the live pid something other than its
    own group leader -- the two ways a live pid at that number fails to prove it is
    ours.

    Yields the list of candidate pids whose identity was actually read, so a test
    can assert a refused refresh never touched them.
    """
    read: list[int] = []

    def _pid_exists(pid: int) -> bool:
        return pid == pgid or pid in members

    def _start_id(pid: int) -> str | None:
        if pid == pgid:
            return leader_start_id
        read.append(pid)
        entry = members.get(pid)
        return entry[0] if entry else None

    def _pgroup_of(pid: int) -> int | None:
        if pid == pgid:
            return pgid if leader_pgroup is None else leader_pgroup
        entry = members.get(pid)
        return entry[1] if entry else None

    with (
        patch("kiro_crew.platform_compat.pgroup_exists", return_value=True),
        patch("kiro_crew.platform_compat.pid_exists", side_effect=_pid_exists),
        patch("kiro_crew.platform_compat.get_process_start_id", side_effect=_start_id),
        patch("kiro_crew.platform_compat.pgroup_of", side_effect=_pgroup_of),
    ):
        yield read


@contextlib.contextmanager
def _leader_dies_mid_scan(
    pgid: int,
    members: dict[int, tuple[str | None, int | None]],
    after: str = "exit",
) -> Iterator[None]:
    """A group whose leader stops being ours only AFTER the scan has run.

    The scan is unbounded, so the leader can be reaped, recycled or moved while
    it walks. ``after`` picks which of those the post-scan read sees: ``exit``
    (the pid is gone), ``recycle`` (a different start identity), ``move`` (a
    different current group) or ``unreadable`` (identity read fails).
    """
    ours = "77123"
    scanned: set[int] = set()

    def _leader_settled() -> bool:
        return bool(members) and scanned >= set(members)

    def _pid_exists(pid: int) -> bool:
        if pid == pgid:
            return not (_leader_settled() and after == "exit")
        return pid in members

    def _start_id(pid: int) -> str | None:
        if pid != pgid:
            scanned.add(pid)
            entry = members.get(pid)
            return entry[0] if entry else None
        if _leader_settled() and after == "recycle":
            return "99999"
        if _leader_settled() and after == "unreadable":
            return None
        return ours

    def _pgroup_of(pid: int) -> int | None:
        if pid == pgid:
            return 9999 if (_leader_settled() and after == "move") else pgid
        entry = members.get(pid)
        return entry[1] if entry else None

    with (
        patch("kiro_crew.platform_compat.pgroup_exists", return_value=True),
        patch("kiro_crew.platform_compat.pid_exists", side_effect=_pid_exists),
        patch("kiro_crew.platform_compat.get_process_start_id", side_effect=_start_id),
        patch("kiro_crew.platform_compat.pgroup_of", side_effect=_pgroup_of),
    ):
        yield


@contextlib.contextmanager
def _witness_changes_mid_read(
    pgid: int,
    member_pid: int,
    start_id: str,
    after: str = "exit",
) -> Iterator[None]:
    """A leaderless group whose sole witness changes between the gate's reads.

    The gate reads the witness's identity and its current group. ``after`` picks
    what the LAST read of that sequence sees: ``exit`` (the pid is gone),
    ``recycle`` (a different start identity), ``move`` (a different group), or
    ``stays`` (nothing changed -- the control).
    """
    reads = {"count": 0}

    def _pid_exists(pid: int) -> bool:
        if pid == pgid:
            return False
        return not (after == "exit" and reads["count"] >= 1)

    def _start_id(pid: int) -> str | None:
        if pid == pgid:
            return None
        reads["count"] += 1
        if reads["count"] >= 2:
            if after == "exit":
                return None
            if after == "recycle":
                return "99999"
        return start_id

    def _pgroup_of(pid: int) -> int | None:
        return 9999 if after == "move" else pgid

    with (
        patch("kiro_crew.platform_compat.pgroup_exists", return_value=True),
        patch("kiro_crew.platform_compat.pid_exists", side_effect=_pid_exists),
        patch("kiro_crew.platform_compat.get_process_start_id", side_effect=_start_id),
        patch("kiro_crew.platform_compat.pgroup_of", side_effect=_pgroup_of),
    ):
        yield


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
class TestPgroupMatchesIncarnation:
    """A saved pgid is a PID, so liveness alone cannot authorize a signal.

    Every case here decides whether ``kill_pgroup`` may fire against a group
    captured at spawn. The gate fails closed: only a provably-ours group passes.
    """

    def _group(self, pgid: int = 4242, witness: str = "77123"):
        return platform_compat.SpawnedProcessGroup(
            pgid, witness, (platform_compat.ProcessGroupMember(4300, "88456"),)
        )

    def test_same_incarnation_is_authorized(self) -> None:
        """Live leader whose start identity still matches: our own group."""
        with (
            patch("kiro_crew.platform_compat.pgroup_exists", return_value=True),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="77123"),
        ):
            assert platform_compat.pgroup_matches_incarnation(self._group()) is True

    def test_a_recycled_pgid_is_refused(self) -> None:
        """A live leader with a DIFFERENT start identity is a stranger.

        This is the data-loss case: the original group emptied, the kernel handed
        the id to an unrelated session leader, and signalling it would kill that
        session's whole tree.
        """
        with (
            patch("kiro_crew.platform_compat.pgroup_exists", return_value=True),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="99999"),
        ):
            assert platform_compat.pgroup_matches_incarnation(self._group()) is False

    def test_an_unreadable_live_leader_is_refused(self) -> None:
        """Identity unknown is not identity confirmed, so the signal is refused."""
        with (
            patch("kiro_crew.platform_compat.pgroup_exists", return_value=True),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
        ):
            assert platform_compat.pgroup_matches_incarnation(self._group()) is False

    def test_a_reaped_leader_over_a_witnessed_member_is_authorized(self) -> None:
        """No leader left, but an original member is still in the group.

        The case the saved group exists for. A pgid cannot be reallocated while
        any member of the original group remains, so a surviving witness proves
        the group never emptied and therefore was never handed to a stranger.
        """
        with _leaderless(4242, members={4300: ("88456", 4242)}):
            assert platform_compat.pgroup_matches_incarnation(self._group()) is True

    def test_a_recycled_leaderless_group_is_refused(self) -> None:
        """A populated group whose leader is reaped can still be a stranger's.

        This is the data-loss case liveness alone cannot see: our group emptied,
        the kernel handed the id to an unrelated session leader, that leader
        forked and then exited. ``pgroup_exists`` is True and ``pid_exists`` is
        False -- exactly the shape our own reaped-leader tree has -- so only an
        original member witness can tell the two apart.
        """
        with _leaderless(4242, members={}):
            assert platform_compat.pgroup_matches_incarnation(self._group()) is False

    def test_a_recycled_witness_pid_is_refused(self) -> None:
        """The witness pid is alive, but it is a different process now."""
        with _leaderless(4242, members={4300: ("99999", 4242)}):
            assert platform_compat.pgroup_matches_incarnation(self._group()) is False

    @pytest.mark.parametrize("after", ["exit", "recycle", "move"])
    def test_a_witness_that_changes_between_the_reads_is_refused(self, after: str) -> None:
        """The witness can stop being itself between the identity and group reads.

        A sole surviving witness is read twice -- its start identity and its
        current group -- and it can exit, be recycled, or leave the group in
        between. Reading the identity once and the group once mixes one
        incarnation's identity with another's membership, which is exactly the
        evidence that authorizes ``killpg`` against a whole group. The membership
        read is therefore bracketed by identity reads and all three must agree.
        """
        with _witness_changes_mid_read(4242, 4300, "88456", after=after):
            assert platform_compat.pgroup_matches_incarnation(self._group()) is False

    def test_a_witness_stable_across_the_reads_is_allowed(self) -> None:
        """The bracket is a gate on a real change, not a blanket refusal."""
        with _witness_changes_mid_read(4242, 4300, "88456", after="stays"):
            assert platform_compat.pgroup_matches_incarnation(self._group()) is True

    def test_a_witness_that_left_the_group_is_refused(self) -> None:
        """A ``setsid`` descendant proves nothing about the group it left."""
        with _leaderless(4242, members={4300: ("88456", 5555)}):
            assert platform_compat.pgroup_matches_incarnation(self._group()) is False

    def test_an_unreadable_witness_is_refused(self) -> None:
        """Identity unknown is not identity confirmed, so the group is refused."""
        with _leaderless(4242, members={4300: (None, 4242)}):
            assert platform_compat.pgroup_matches_incarnation(self._group()) is False

    def test_a_witness_whose_group_is_unreadable_is_refused(self) -> None:
        """``pgroup_of`` answering ``None`` is not membership."""
        with _leaderless(4242, members={4300: ("88456", None)}):
            assert platform_compat.pgroup_matches_incarnation(self._group()) is False

    def test_a_leaderless_group_with_no_witnesses_is_refused(self) -> None:
        """Nothing was ever witnessed, so nothing can authorize the group."""
        with _leaderless(4242, members={4300: ("88456", 4242)}):
            assert (
                platform_compat.pgroup_matches_incarnation(
                    platform_compat.SpawnedProcessGroup(4242, "77123")
                )
                is False
            )

    def test_one_surviving_witness_among_dead_ones_authorizes(self) -> None:
        """Members come and go; the gate needs any single survivor."""
        group = platform_compat.SpawnedProcessGroup(
            4242,
            "77123",
            (
                platform_compat.ProcessGroupMember(4300, "88456"),
                platform_compat.ProcessGroupMember(4301, "88457"),
            ),
        )
        with _leaderless(4242, members={4301: ("88457", 4242)}):
            assert platform_compat.pgroup_matches_incarnation(group) is True

    @pytest.mark.parametrize(
        "member",
        [
            ("plain", "tuple"),
            platform_compat.ProcessGroupMember(1, "88456"),
            platform_compat.ProcessGroupMember(4300, ""),
            platform_compat.ProcessGroupMember("4300", "88456"),
        ],
    )
    def test_a_malformed_witness_cannot_authorize(self, member: object) -> None:
        """A non-witness in the members slot never becomes authorization.

        A reserved pid, a missing start identity, a string pid and a bare tuple
        that is not a ``ProcessGroupMember`` at all are all refused rather than
        coerced -- the answer authorizes ``killpg`` against every process in a
        group, so an unrecognised shape has to fail closed.
        """
        group = platform_compat.SpawnedProcessGroup(4242, "77123", (member,))  # type: ignore[arg-type]
        with _leaderless(4242, members={4300: ("88456", 4242), 1: ("88456", 4242)}):
            assert platform_compat.pgroup_matches_incarnation(group) is False

    def test_a_mock_members_slot_is_refused(self) -> None:
        """A ``Mock`` in the members slot is iterable-looking, not a witness."""
        group = platform_compat.SpawnedProcessGroup(4242, "77123", MagicMock())
        with _leaderless(4242, members={4300: ("88456", 4242)}):
            assert platform_compat.pgroup_matches_incarnation(group) is False

    def test_an_empty_group_is_refused(self) -> None:
        """Nothing of ours is left, so the id is a bare recyclable PID."""
        with patch("kiro_crew.platform_compat.pgroup_exists", return_value=False):
            assert platform_compat.pgroup_matches_incarnation(self._group()) is False

    @pytest.mark.parametrize(
        "pgid, witness", [(0, "77123"), (1, "77123"), (-1, "77123"), (4242, "")]
    )
    def test_reserved_or_unwitnessed_groups_are_refused(self, pgid: int, witness: str) -> None:
        """A reserved id or a missing witness never reaches the liveness probe."""
        with patch("kiro_crew.platform_compat.pgroup_exists") as probe:
            assert (
                platform_compat.pgroup_matches_incarnation(
                    platform_compat.SpawnedProcessGroup(pgid, witness)
                )
                is False
            )

        probe.assert_not_called()

    def test_no_unrelated_group_is_signalled_end_to_end(self) -> None:
        """A recycled group reaches NO signal at all, through the real gate.

        Drives ``_signal_teardown_target`` rather than the gate directly, so the
        assertion covers the call site that would do the damage. ``pgid == pid``
        here, so a pid-scoped fallback would signal the very stranger the gate
        just rejected -- the group refusal has to end the attempt.
        """
        from kiro_crew.session_pid import _signal_teardown_target

        stranger = self._group(pgid=6000)
        with (
            patch("kiro_crew.platform_compat.pgroup_exists", return_value=True),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="different"),
            patch("kiro_crew.session_pid.platform_compat.kill_pgroup") as mock_kill_group,
            patch("kiro_crew.session_pid.platform_compat.kill_pid") as mock_kill_pid,
        ):
            assert _signal_teardown_target(6000, stranger, platform_compat.SIGTERM) is False

        mock_kill_group.assert_not_called()
        mock_kill_pid.assert_not_called()

    def test_a_recycled_leaderless_group_is_never_signalled_end_to_end(self) -> None:
        """A recycled leaderless group, driven through the real call site.

        A stranger's group with a reaped leader is indistinguishable from ours by
        liveness alone, so this asserts on the function that would deliver the
        signal rather than on the gate's return value.
        """
        from kiro_crew.session_pid import _signal_teardown_target

        with (
            _leaderless(6000, members={}),
            patch("kiro_crew.session_pid.platform_compat.kill_pgroup") as mock_kill_group,
            patch("kiro_crew.session_pid.platform_compat.kill_pid") as mock_kill_pid,
        ):
            assert (
                _signal_teardown_target(6000, self._group(pgid=6000), platform_compat.SIGTERM)
                is False
            )

        mock_kill_group.assert_not_called()
        mock_kill_pid.assert_not_called()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
class TestWitnessProcessGroupMembers:
    """Which pids may become a group's member witnesses."""

    def test_a_current_member_is_witnessed(self) -> None:
        with (
            patch("kiro_crew.platform_compat.pgroup_of", return_value=4242),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="88456"),
        ):
            assert platform_compat.witness_process_group_members(4242, [4300]) == (
                platform_compat.ProcessGroupMember(4300, "88456"),
            )

    def test_a_pid_outside_the_group_is_not_witnessed(self) -> None:
        """A descendant that left the group cannot witness it."""
        with (
            patch("kiro_crew.platform_compat.pgroup_of", return_value=5555),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="88456"),
        ):
            assert platform_compat.witness_process_group_members(4242, [4300]) == ()

    def test_an_unreadable_identity_is_not_witnessed(self) -> None:
        with (
            patch("kiro_crew.platform_compat.pgroup_of", return_value=4242),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
        ):
            assert platform_compat.witness_process_group_members(4242, [4300]) == ()

    def test_a_pid_recycled_mid_read_is_not_witnessed(self) -> None:
        """The two identity reads bracket the group read for exactly this case.

        A pid that exits between them would otherwise be recorded with one
        incarnation's identity and the other's group membership.
        """
        with (
            patch("kiro_crew.platform_compat.pgroup_of", return_value=4242),
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=["88456", "99999"],
            ),
        ):
            assert platform_compat.witness_process_group_members(4242, [4300]) == ()

    @pytest.mark.parametrize("pid", [0, 1, -1, True, "4300", None])
    def test_reserved_and_non_int_pids_are_skipped(self, pid: object) -> None:
        """``True`` is an ``int`` subclass that would coerce to pid 1."""
        with patch("kiro_crew.platform_compat.pgroup_of") as probe:
            assert platform_compat.witness_process_group_members(4242, [pid]) == ()  # type: ignore[list-item]

        probe.assert_not_called()

    @pytest.mark.parametrize("pgid", [0, 1, -1])
    def test_a_reserved_group_witnesses_nothing(self, pgid: int) -> None:
        with patch("kiro_crew.platform_compat.pgroup_of") as probe:
            assert platform_compat.witness_process_group_members(pgid, [4300]) == ()

        probe.assert_not_called()

    def test_the_witness_set_is_unbounded(self) -> None:
        """Every in-group descendant is recorded, with no cap.

        A fixed ceiling would silently drop the one descendant that ignores
        SIGTERM when it happens to sort past the cap, which is the exact leak
        the witness exists to close.
        """
        with (
            patch("kiro_crew.platform_compat.pgroup_of", return_value=4242),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="88456"),
        ):
            witnessed = platform_compat.witness_process_group_members(4242, range(5000, 5200))

        assert len(witnessed) == 200


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
class TestRecordProcessGroupMembers:
    """How a captured group accumulates witnesses across its lifetime."""

    def test_witnesses_are_added_to_the_group(self) -> None:
        with _leader_alive(4242, "77123", members={4300: ("88456", 4242)}):
            group = platform_compat.record_process_group_members(
                platform_compat.SpawnedProcessGroup(4242, "77123"), [4300]
            )

        assert group == platform_compat.SpawnedProcessGroup(
            4242, "77123", (platform_compat.ProcessGroupMember(4300, "88456"),)
        )

    def test_an_earlier_witness_is_kept(self) -> None:
        """A descendant reparented out of the tree is still a valid witness.

        The scan walks the leader's children, so a member that was reparented to
        init drops out of it -- and that is precisely the survivor a
        reaped-leader teardown depends on.
        """
        existing = platform_compat.SpawnedProcessGroup(
            4242, "77123", (platform_compat.ProcessGroupMember(4300, "88456"),)
        )
        with _leader_alive(4242, "77123", members={4301: ("88999", 4242)}):
            group = platform_compat.record_process_group_members(existing, [4301])

        assert group is not None
        assert set(group.members) == {
            platform_compat.ProcessGroupMember(4300, "88456"),
            platform_compat.ProcessGroupMember(4301, "88999"),
        }

    @pytest.mark.parametrize("after", ["exit", "recycle", "move", "unreadable"])
    def test_a_leader_that_stops_being_ours_mid_scan_discards_every_fresh_witness(
        self, after: str
    ) -> None:
        """The scan is unbounded, so the leader can change under it.

        Proving the leader once BEFORE the walk authorizes witnesses collected
        after that proof went stale: the pid can be reaped and its id handed to an
        unrelated session leader while the walk runs, and the pids read after that
        moment are the stranger's descendants. Every fresh witness is discarded
        together -- keeping the ones read early would need a per-pid ordering the
        scan does not record -- and the prior group is returned untouched.
        """
        existing = platform_compat.SpawnedProcessGroup(
            4242, "77123", (platform_compat.ProcessGroupMember(4300, "88456"),)
        )
        with _leader_dies_mid_scan(4242, members={4301: ("88999", 4242)}, after=after):
            assert platform_compat.record_process_group_members(existing, [4301]) == existing

    def test_a_leader_still_ours_after_the_scan_keeps_the_fresh_witnesses(self) -> None:
        """The post-scan proof is a gate on a real change, not a blanket refusal."""
        existing = platform_compat.SpawnedProcessGroup(4242, "77123")
        with _leader_dies_mid_scan(4242, members={4301: ("88999", 4242)}, after="stays"):
            group = platform_compat.record_process_group_members(existing, [4301])

        assert group == platform_compat.SpawnedProcessGroup(
            4242, "77123", (platform_compat.ProcessGroupMember(4301, "88999"),)
        )

    def test_a_re_witnessed_pid_takes_the_fresh_identity(self) -> None:
        """One pid cannot hold two identities; the current read wins."""
        existing = platform_compat.SpawnedProcessGroup(
            4242, "77123", (platform_compat.ProcessGroupMember(4300, "stale"),)
        )
        with _leader_alive(4242, "77123", members={4300: ("88456", 4242)}):
            group = platform_compat.record_process_group_members(existing, [4300])

        assert group is not None
        assert group.members == (platform_compat.ProcessGroupMember(4300, "88456"),)

    def test_every_in_group_descendant_is_recorded(self) -> None:
        """No cap: a tree wider than any fixed ceiling keeps all its witnesses."""
        members = {pid: ("88456", 4242) for pid in range(5000, 5200)}
        with _leader_alive(4242, "77123", members=members):
            group = platform_compat.record_process_group_members(
                platform_compat.SpawnedProcessGroup(4242, "77123"), sorted(members)
            )

        assert group is not None
        assert len(group.members) == 200

    def test_a_survivor_past_the_first_sixty_four_authorizes(self) -> None:
        """The one SIGTERM-resistant child can be anywhere in the tree.

        Truncating the witness set would drop it and leave the group refused,
        which is the leak the saved group exists to close.
        """
        members = {pid: ("88456", 4242) for pid in range(5000, 5200)}
        with _leader_alive(4242, "77123", members=members):
            group = platform_compat.record_process_group_members(
                platform_compat.SpawnedProcessGroup(4242, "77123"), sorted(members)
            )

        assert group is not None
        survivor = group.members[199]
        assert survivor.pid == 5199
        with _leaderless(4242, members={survivor.pid: ("88456", 4242)}):
            assert platform_compat.pgroup_matches_incarnation(group) is True

    def test_an_empty_scan_keeps_the_existing_witnesses(self) -> None:
        existing = platform_compat.SpawnedProcessGroup(
            4242, "77123", (platform_compat.ProcessGroupMember(4300, "88456"),)
        )
        assert platform_compat.record_process_group_members(existing, []) == existing

    def test_no_group_stays_no_group(self) -> None:
        """``None`` means nothing was ever captured, and that has to survive."""
        assert platform_compat.record_process_group_members(None, [4300]) is None

    def test_a_recycled_leader_records_nothing(self) -> None:
        """A live pid at the pgid with a DIFFERENT identity is a stranger.

        Recording that stranger's descendants would put witnesses a later
        teardown accepts into OUR group, and the kill would land on the
        unrelated session that inherited the id.
        """
        existing = platform_compat.SpawnedProcessGroup(
            4242, "77123", (platform_compat.ProcessGroupMember(4300, "88456"),)
        )
        with _leader_alive(4242, "99999", members={4301: ("88999", 4242)}) as reads:
            assert platform_compat.record_process_group_members(existing, [4301]) == existing

        assert reads == [], "a refused refresh must not read any candidate pid"

    def test_an_unreadable_leader_records_nothing(self) -> None:
        """Identity unknown is not identity confirmed, so nothing is accepted."""
        existing = platform_compat.SpawnedProcessGroup(
            4242, "77123", (platform_compat.ProcessGroupMember(4300, "88456"),)
        )
        with _leader_alive(4242, None, members={4301: ("88999", 4242)}):
            assert platform_compat.record_process_group_members(existing, [4301]) == existing

    def test_a_leaderless_group_records_nothing(self) -> None:
        """The refresh is only sound while the leader is alive to witness it."""
        existing = platform_compat.SpawnedProcessGroup(
            4242, "77123", (platform_compat.ProcessGroupMember(4300, "88456"),)
        )
        with _leaderless(4242, members={4301: ("88999", 4242)}):
            assert platform_compat.record_process_group_members(existing, [4301]) == existing

    def test_a_leader_that_left_its_own_group_records_nothing(self) -> None:
        """A live pid at the pgid that does not lead this group is not ours."""
        existing = platform_compat.SpawnedProcessGroup(
            4242, "77123", (platform_compat.ProcessGroupMember(4300, "88456"),)
        )
        with _leader_alive(4242, "77123", members={4301: ("88999", 4242)}, leader_pgroup=9999):
            assert platform_compat.record_process_group_members(existing, [4301]) == existing


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
class TestCaptureSpawnedProcessGroup:
    """What may become a saved group at spawn time."""

    def test_a_group_leader_is_witnessed(self) -> None:
        """The child of a start_new_session spawn leads its own group."""
        with (
            patch("kiro_crew.platform_compat.os.getpgid", return_value=4242),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="77123"),
        ):
            group = platform_compat.capture_spawned_process_group(4242)

        assert group == platform_compat.SpawnedProcessGroup(4242, "77123")

    def test_a_non_leader_is_refused(self) -> None:
        """pgid != pid means this process does not own the group."""
        with patch("kiro_crew.platform_compat.os.getpgid", return_value=999):
            assert platform_compat.capture_spawned_process_group(4242) is None

    def test_an_unwitnessable_leader_is_refused(self) -> None:
        """Without a start identity the group could never be authorized later."""
        with (
            patch("kiro_crew.platform_compat.os.getpgid", return_value=4242),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
        ):
            assert platform_compat.capture_spawned_process_group(4242) is None

    def test_a_leader_that_exits_before_the_group_read_is_refused(self) -> None:
        """A child can exit between the identity read and the group read.

        ``os.getpgid`` raising is the ordinary reaped-leader answer, and the
        identity read that precedes it does not make the group knowable.
        """
        with (
            patch("kiro_crew.platform_compat.os.getpgid", side_effect=ProcessLookupError("gone")),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="77123"),
        ):
            assert platform_compat.capture_spawned_process_group(4242) is None

    def test_a_pid_recycled_between_the_identity_reads_is_refused(self) -> None:
        """The pid can be reaped and reused while the capture runs.

        A single identity read pairs one incarnation's identity with whatever
        process holds the number by the time the group is read -- and a
        replacement leader of its own new session reads back ``pgid == pid`` just
        as ours does, so the shape alone cannot separate them. The group read is
        bracketed by identity reads and both must agree.
        """
        with (
            patch("kiro_crew.platform_compat.os.getpgid", return_value=4242),
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=["77123", "99999"],
            ),
        ):
            assert platform_compat.capture_spawned_process_group(4242) is None

    def test_a_leader_whose_identity_vanishes_after_the_group_read_is_refused(self) -> None:
        """An identity readable before and gone after is not a witness."""
        with (
            patch("kiro_crew.platform_compat.os.getpgid", return_value=4242),
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=["77123", None],
            ),
        ):
            assert platform_compat.capture_spawned_process_group(4242) is None

    def test_an_unreadable_identity_refuses_before_the_group_is_read(self) -> None:
        """No identity means no witness, so the group read is never reached."""
        with (
            patch("kiro_crew.platform_compat.os.getpgid") as getpgid,
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
        ):
            assert platform_compat.capture_spawned_process_group(4242) is None

        getpgid.assert_not_called()

    def test_a_raising_identity_read_is_refused(self) -> None:
        """An identity reader that raises refuses rather than propagating."""
        with (
            patch("kiro_crew.platform_compat.os.getpgid", return_value=4242),
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=OSError("proc unreadable"),
            ),
        ):
            assert platform_compat.capture_spawned_process_group(4242) is None

    def test_a_stable_leader_across_both_reads_is_captured(self) -> None:
        """The bracket is a gate on a real change, not a blanket refusal."""
        reads: list[int] = []

        def _start_id(pid: int) -> str:
            reads.append(pid)
            return "77123"

        with (
            patch("kiro_crew.platform_compat.os.getpgid", return_value=4242),
            patch("kiro_crew.platform_compat.get_process_start_id", side_effect=_start_id),
        ):
            group = platform_compat.capture_spawned_process_group(4242)

        assert group == platform_compat.SpawnedProcessGroup(4242, "77123")
        assert reads == [4242, 4242], "the group read is not bracketed by identity reads"

    @pytest.mark.parametrize("pid", [0, 1, -1])
    def test_reserved_pids_are_refused(self, pid: int) -> None:
        """pid <= 1 excludes the kill(0)/kill(-n) broadcast semantics outright."""
        assert platform_compat.capture_spawned_process_group(pid) is None


def test_windows_captures_no_group() -> None:
    """Windows has no process group in this sense: taskkill /T owns the tree."""
    with patch("kiro_crew.platform_compat.IS_POSIX", False):
        assert platform_compat.capture_spawned_process_group(4242) is None
        assert platform_compat.witness_process_group_members(4242, [4300]) == ()
        assert platform_compat.record_process_group_members(
            platform_compat.SpawnedProcessGroup(4242, "77123"), [4300]
        ) == platform_compat.SpawnedProcessGroup(4242, "77123")
        assert (
            platform_compat.pgroup_matches_incarnation(
                platform_compat.SpawnedProcessGroup(
                    4242, "77123", (platform_compat.ProcessGroupMember(4300, "88456"),)
                )
            )
            is False
        )
