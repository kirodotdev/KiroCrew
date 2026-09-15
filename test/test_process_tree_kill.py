"""Tests for process tree killing in session.reset() and subagent._sigkill_session().

Covers the killpg + escaped child sweep logic added to fix orphaned
kiro-cli sessions.
"""
from __future__ import annotations

import os
import signal
from contextlib import contextmanager
from threading import Thread
from types import FunctionType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager
from kiro_crew.subagent import SubagentManager

# ── Helpers ──


@contextmanager
def _private_process_api():
    """Run the real POSIX functions with private syscall bindings.

    Patching ``session.os.kill`` changes stdlib os for EVERY thread, including
    unrelated liveness probes. Rebinding platform_compat.os alone still catches
    those probes. Copy only this call's function globals instead: the original
    bytecode (including the broadcast guard) runs, but cannot reach host signals.
    """
    from kiro_crew import platform_compat as pc

    names = (
        "pid_exists",
        "kill_pid",
        "kill_process_tree",
        "kill_pid_async",
        "kill_process_tree_async",
    )
    shared = {name: getattr(os, name, None) for name in ("kill", "killpg", "getpgid")}
    syscalls = SimpleNamespace(
        kill=MagicMock(name="kill"),
        killpg=MagicMock(name="killpg"),
        getpgid=MagicMock(name="getpgid", side_effect=lambda pid: pid),
    )
    namespace = dict(vars(pc), os=syscalls, IS_POSIX=True)
    for name in names:
        original = getattr(pc, name)
        function = FunctionType(
            original.__code__,
            namespace,
            original.__name__,
            original.__defaults__,
            original.__closure__,
        )
        function.__kwdefaults__ = original.__kwdefaults__
        namespace[name] = function
    api = SimpleNamespace(**namespace)
    try:
        with (
            patch("kiro_crew.session.platform_compat", api),
            patch("kiro_crew.subagent.platform_compat", api),
        ):
            yield api
    finally:
        for name, original in shared.items():
            assert getattr(os, name, None) is original, f"shared os.{name} was modified"
        assert pc.os is os


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

        with _private_process_api() as api:
            await mgr.reset("t1")

        api.os.kill.assert_not_called()
        api.os.killpg.assert_not_called()
        api.os.getpgid.assert_not_called()
        provider.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reset_skips_kill_for_zero_pid(self, cfg):
        """reset() skips kill logic when _pid is 0 (kernel scheduler)."""
        provider = _make_provider(pid=0)
        mgr = SessionManager(cfg, provider_factory=_provider_factory(provider))
        await mgr.get_or_create("t1")

        with _private_process_api() as api:
            await mgr.reset("t1")

        api.os.kill.assert_not_called()
        api.os.killpg.assert_not_called()
        api.os.getpgid.assert_not_called()
        provider.shutdown.assert_awaited_once()


# ── subagent._sigkill_session() tests ──


class TestSigkillSessionProcessTree:
    """Tests for SubagentManager._sigkill_session() process tree cleanup."""

    def _make_manager(
        self, pid: int, child_pids: dict[int, int | None] | None = None, start_time: int | None = 100
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
            _private_process_api() as api,
            patch.object(api.os, "killpg", side_effect=ProcessLookupError) as mock_killpg,
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("kiro_crew.acp.client._get_start_time", return_value=100),
            patch("kiro_crew.acp.client._is_our_child", return_value=True),
        ):
            await mgr._sigkill_session("subagent:test1")

        api.os.getpgid.assert_called_once_with(54321)
        mock_killpg.assert_called_once_with(54321, signal.SIGKILL)
        api.os.kill.assert_called_once_with(54321, signal.SIGKILL)

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


@pytest.mark.parametrize("sig", [0, signal.SIGKILL])
def test_private_process_api_records_every_signal_without_changing_shared_os(sig):
    """An unrelated probe uses its own inert backend while this call is active."""
    from kiro_crew import platform_compat as pc

    original_kill = os.kill
    original_probe = pc.pid_exists
    background_kill = MagicMock(name="background_kill")
    observed = []
    background_probe = FunctionType(
        pc.pid_exists.__code__,
        dict(vars(pc), IS_POSIX=True, os=SimpleNamespace(kill=background_kill)),
    )

    def probe():
        observed.append((os.kill is original_kill, pc.os is os, pc.pid_exists is original_probe))
        observed.append(background_probe(987654321))

    with _private_process_api() as api:
        for name in (
            "pid_exists",
            "kill_pid",
            "kill_process_tree",
            "kill_pid_async",
            "kill_process_tree_async",
        ):
            assert getattr(api, name).__code__ is getattr(pc, name).__code__
        worker = Thread(target=probe)
        worker.start()
        try:
            api.kill_pid(54321, sig)
        finally:
            worker.join(5)
        assert not worker.is_alive()
        assert observed == [(True, True, True), True]
        background_kill.assert_called_once_with(987654321, 0)
        # No signal filtering: even signal 0 in the SUBJECT is fully observable.
        assert api.os.kill.call_args_list == [call(54321, sig)]
        api.os.killpg.assert_not_called()

    assert os.kill is original_kill
    assert pc.pid_exists is original_probe


def test_private_process_api_keeps_the_real_broadcast_guard():
    with _private_process_api() as api:
        for pid in (0, 1, -1, "54321", MagicMock()):
            with pytest.raises(ValueError, match="refusing non-int/reserved pid"):
                api.kill_process_tree(pid, signal.SIGKILL)
        api.os.kill.assert_not_called()
        api.os.killpg.assert_not_called()
        api.os.getpgid.assert_not_called()


def test_private_process_api_detects_a_shared_os_patch():
    """Negative control: the old patch shape must fail the isolation assertion."""
    original_kill = os.kill
    with pytest.MonkeyPatch.context() as patched:
        with pytest.raises(AssertionError, match="shared os.kill was modified"):
            with _private_process_api():
                patched.setattr(os, "kill", MagicMock(name="incorrect_shared_patch"))
    assert os.kill is original_kill
