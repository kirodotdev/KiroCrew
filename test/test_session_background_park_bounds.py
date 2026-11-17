"""Bounds on the parked ``_bg`` runtimes and on what starts one.

A displaced ``_bg`` runtime that still reports a session is parked to drain.
Without bounds, a probe that keeps answering busy keeps a multi-GB process alive
until the gateway restarts while each replacement grows and is parked in turn.
These pin the bounds: a park limit, a parked cap, an immediate kill for the
Windows handle-count staleness (the one stale reason where the process itself is
the danger; RSS still parks), and the empty folder a keyless runtime starts in.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_runtime_helpers import _make_runtime

from kiro_crew import platform_compat, platform_compat_metrics
from kiro_crew.acp import runtime_liveness
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.paths import KEYLESS_WORK_DIR_NAME, config_dir
from kiro_crew.kiro_prerequisite_spawn_identity import mark_identity_parked
from kiro_crew.session import SessionManager
from kiro_crew.session_background import (
    _BG_PARKED_AT_ATTR,
    BG_PARK_MAX_SECS,
    BG_PARKED_MAX,
    _mark_parked,
    _session_count,
)


@pytest.fixture
def mgr():
    cfg = KiroCrewConfig()
    cfg.session.timeout_secs = 2
    return SessionManager(cfg, provider_factory=MagicMock())


def _busy_runtime(stale_reason: str | None = None) -> AsyncMock:
    rt = AsyncMock()
    rt.is_alive = lambda: True
    rt.has_active_or_initializing_sessions = lambda: True
    rt._is_stale = AsyncMock(return_value=stale_reason)
    rt.kill = AsyncMock()
    rt.pid = None
    rt.create_session = AsyncMock(return_value=object())
    return rt


def _fresh_runtime() -> AsyncMock:
    rt = AsyncMock()
    rt.spawn = AsyncMock()
    rt.is_alive = lambda: True
    rt.create_session = AsyncMock(return_value=object())
    return rt


def _parked(parked_at: float) -> AsyncMock:
    rt = _busy_runtime()
    setattr(rt, _BG_PARKED_AT_ATTR, parked_at)
    return rt


async def _reap(mgr: SessionManager) -> None:
    async with mgr._bg_runtime_lock:
        await mgr._reap_drained_bg_runtimes_locked()


class TestParkLimit:
    @pytest.mark.asyncio
    async def test_a_runtime_still_busy_past_the_park_limit_is_killed(self, mgr):
        overdue = _parked(time.monotonic() - BG_PARK_MAX_SECS - 1.0)
        mgr._draining_bg_runtimes = [overdue]

        await _reap(mgr)

        overdue.kill.assert_awaited_once()
        assert mgr._draining_bg_runtimes == []
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_busy_runtime_inside_the_park_limit_stays_parked(self, mgr):
        recent = _parked(time.monotonic())
        mgr._draining_bg_runtimes = [recent]

        await _reap(mgr)

        recent.kill.assert_not_awaited()
        assert mgr._draining_bg_runtimes == [recent]
        mgr._draining_bg_runtimes = []
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_raising_busy_probe_does_not_outlive_the_park_limit(self, mgr):
        overdue = _parked(time.monotonic() - BG_PARK_MAX_SECS - 1.0)
        overdue.has_active_or_initializing_sessions = MagicMock(side_effect=RuntimeError)
        mgr._draining_bg_runtimes = [overdue]

        await _reap(mgr)

        overdue.kill.assert_awaited_once()
        assert mgr._draining_bg_runtimes == []
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_the_park_limit_kill_logs_the_session_count(self, mgr, caplog):
        overdue = _parked(time.monotonic() - BG_PARK_MAX_SECS - 1.0)
        overdue._session_queues = {"a": None, "b": None}
        overdue._session_inits_in_flight = 1
        mgr._draining_bg_runtimes = [overdue]

        with caplog.at_level("WARNING"):
            await _reap(mgr)

        assert "ending its 3 session(s)" in caplog.text
        await mgr.close_all()

    def test_a_re_park_keeps_the_first_stamp(self):
        rt = _busy_runtime()
        setattr(rt, _BG_PARKED_AT_ATTR, 1.0)
        _mark_parked(rt)
        assert getattr(rt, _BG_PARKED_AT_ATTR) == 1.0


class TestParkedCap:
    @pytest.mark.asyncio
    async def test_the_oldest_beyond_the_cap_is_killed(self, mgr):
        now = time.monotonic()
        parked = [_parked(now - 3.0), _parked(now - 2.0), _parked(now - 1.0)]
        assert len(parked) == BG_PARKED_MAX + 1
        mgr._draining_bg_runtimes = list(parked)

        await _reap(mgr)

        parked[0].kill.assert_awaited_once()
        parked[1].kill.assert_not_awaited()
        parked[2].kill.assert_not_awaited()
        assert mgr._draining_bg_runtimes == parked[1:]
        mgr._draining_bg_runtimes = []
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_the_cap_kill_logs_the_session_count_read_before_the_kill(self, mgr, caplog):
        now = time.monotonic()
        parked = [_parked(now - 3.0), _parked(now - 2.0), _parked(now - 1.0)]
        parked[0]._session_queues = {"a": None}
        parked[0]._session_inits_in_flight = 0

        async def _kill_clears(*_args, **_kwargs):
            parked[0]._session_queues = {}

        parked[0].kill = AsyncMock(side_effect=_kill_clears)
        mgr._draining_bg_runtimes = list(parked)

        with caplog.at_level("WARNING"):
            await _reap(mgr)

        assert "ended its 1 session(s)" in caplog.text
        mgr._draining_bg_runtimes = []
        await mgr.close_all()

    def test_a_runtime_without_session_counts_logs_unknown(self):
        assert _session_count(object()) == "unknown"

    @pytest.mark.asyncio
    async def test_a_failed_cap_kill_keeps_the_runtime_and_caps_the_next_one(self, mgr):
        now = time.monotonic()
        parked = [_parked(now - 3.0), _parked(now - 2.0), _parked(now - 1.0)]
        parked[0].kill = AsyncMock(side_effect=RuntimeError("tree drain pending"))
        mgr._draining_bg_runtimes = list(parked)

        await _reap(mgr)

        parked[0].kill.assert_awaited_once()
        parked[1].kill.assert_awaited_once()
        parked[2].kill.assert_not_awaited()
        assert mgr._draining_bg_runtimes == [parked[0], parked[2]]
        mgr._draining_bg_runtimes = []
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_runtime_inside_its_identity_grace_is_not_capped(self, mgr):
        now = time.monotonic()
        parked = [_parked(now - 3.0), _parked(now - 2.0), _parked(now - 1.0)]
        mark_identity_parked(parked[0], now)
        mgr._draining_bg_runtimes = list(parked)

        await _reap(mgr)

        parked[0].kill.assert_not_awaited()
        parked[1].kill.assert_awaited_once()
        parked[2].kill.assert_not_awaited()
        assert mgr._draining_bg_runtimes == [parked[0], parked[2]]
        mgr._draining_bg_runtimes = []
        await mgr.close_all()


class TestKillNowStaleReasons:
    @pytest.mark.asyncio
    async def test_a_busy_runtime_stale_by_handles_is_killed_not_parked(self, mgr):
        stale = _busy_runtime(stale_reason="handles")
        mgr._bg_runtime = stale
        fresh = _fresh_runtime()

        with patch("kiro_crew.acp.runtime.AcpRuntime", return_value=fresh):
            result = await mgr.get_bg_session()

        stale.kill.assert_awaited_once()
        assert stale not in mgr._draining_bg_runtimes
        assert mgr._bg_runtime is fresh
        assert result is fresh.create_session.return_value
        await mgr.close_all()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", ["age", "rss"])
    async def test_a_busy_runtime_stale_by_age_or_rss_is_parked(self, mgr, reason):
        # A healthy kiro-cli sits above the 500 MiB recycle threshold, so an RSS
        # kill-now would end live background turns every few minutes.
        stale = _busy_runtime(stale_reason=reason)
        mgr._bg_runtime = stale
        fresh = _fresh_runtime()

        with patch("kiro_crew.acp.runtime.AcpRuntime", return_value=fresh):
            await mgr.get_bg_session()

        stale.kill.assert_not_awaited()
        assert mgr._draining_bg_runtimes == [stale]
        assert isinstance(getattr(stale, _BG_PARKED_AT_ATTR), float)
        mgr._draining_bg_runtimes = []
        await mgr.close_all()


class TestHandleStaleness:
    @staticmethod
    def _old_runtime():
        rt, _, _ = _make_runtime()
        rt._max_age_secs = 6 * 3600
        rt._spawn_monotonic = time.monotonic() - 600.0
        rt._max_rss_mb = 500.0
        return rt

    @pytest.mark.asyncio
    async def test_a_runtime_over_the_handle_ceiling_is_stale_by_handles(self, monkeypatch):
        rt = self._old_runtime()
        monkeypatch.setattr(
            platform_compat,
            "proc_handle_count_for_pid",
            lambda pid: runtime_liveness._MAX_HANDLES + 1,
        )
        monkeypatch.setattr("kiro_crew.acp.runtime._get_rss_tree_mb", lambda pid, depth=None: 10.0)
        assert await rt._is_stale() == "handles"

    @pytest.mark.asyncio
    async def test_a_runtime_under_the_ceiling_falls_through_to_rss(self, monkeypatch):
        rt = self._old_runtime()
        monkeypatch.setattr(
            platform_compat,
            "proc_handle_count_for_pid",
            lambda pid: runtime_liveness._MAX_HANDLES,
        )
        monkeypatch.setattr("kiro_crew.acp.runtime._get_rss_tree_mb", lambda pid, depth=None: 10.0)
        assert await rt._is_stale() is None

    @pytest.mark.asyncio
    async def test_no_handle_reading_falls_through_to_rss(self, monkeypatch):
        rt = self._old_runtime()
        monkeypatch.setattr(platform_compat, "proc_handle_count_for_pid", lambda pid: None)
        monkeypatch.setattr("kiro_crew.acp.runtime._get_rss_tree_mb", lambda pid, depth=None: 900.0)
        assert await rt._is_stale() == "rss"


class TestHandleCountProbe:
    def test_it_answers_none_off_windows(self, monkeypatch):
        opened: list[object] = []
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(
            platform_compat, "_open_process_query_handle", lambda p: opened.append(p) or 7
        )
        assert platform_compat_metrics.proc_handle_count_for_pid(1) is None
        assert opened == []

    def test_it_answers_none_for_an_unopenable_pid(self, monkeypatch):
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(platform_compat, "_open_process_query_handle", lambda pid: None)
        assert platform_compat_metrics.proc_handle_count_for_pid(1) is None

    def test_it_answers_none_when_the_query_raises(self, monkeypatch):
        closed: list[int] = []
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(platform_compat, "_open_process_query_handle", lambda pid: 7)
        monkeypatch.setattr(platform_compat, "_close_process_handle", closed.append)
        monkeypatch.setattr(
            platform_compat_metrics.ctypes,
            "WinDLL",
            MagicMock(side_effect=OSError("no kernel32")),
            raising=False,
        )
        assert platform_compat_metrics.proc_handle_count_for_pid(1) is None
        assert closed == [7]

    @pytest.mark.parametrize("pid", [0, -1, True])
    def test_it_refuses_a_pid_that_is_not_a_positive_int(self, monkeypatch, pid):
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        opened: list[object] = []
        monkeypatch.setattr(
            platform_compat, "_open_process_query_handle", lambda p: opened.append(p)
        )
        assert platform_compat_metrics.proc_handle_count_for_pid(pid) is None
        assert opened == []

    @pytest.mark.skipif(not platform_compat.IS_WINDOWS, reason="Windows handle count")
    def test_it_reads_a_live_process_on_windows(self):
        import os

        count = platform_compat_metrics.proc_handle_count_for_pid(os.getpid())
        assert isinstance(count, int) and count > 0


class TestKeylessWorkDir:
    def test_a_keyless_runtime_starts_in_the_empty_bg_runtime_folder(self):
        from kiro_crew.acp.runtime import AcpRuntime

        rt = AcpRuntime()
        assert rt._work_dir == config_dir() / KEYLESS_WORK_DIR_NAME
        assert KEYLESS_WORK_DIR_NAME == "bg-runtime"

    def test_a_keyless_client_starts_in_the_empty_bg_runtime_folder(self):
        from kiro_crew.acp.client import AcpClient

        client = AcpClient()
        assert client._work_dir == config_dir() / KEYLESS_WORK_DIR_NAME

    def test_an_explicit_work_dir_is_kept(self, tmp_path):
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp.runtime import AcpRuntime

        assert AcpRuntime(work_dir=tmp_path)._work_dir == tmp_path
        assert AcpClient(work_dir=tmp_path)._work_dir == tmp_path
