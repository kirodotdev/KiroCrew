"""Work a backend does between turns keeps its session off the idle sweep.

The defect these pin: ``last_used`` moves only when a turn is dispatched, so a
session whose backend (or a child it spawned) kept working between turns
looked idle and was expired mid-work.

The fix is backend-neutral: a transport stamps the last frame it routed for a
session, the provider reports it as ``session_activity_at``, and the sweep
measures idleness from the later of that and the turn clock. These drive the
real ``AcpRuntime`` reader and the real ``SessionManager`` sweep.
"""

import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_update_provider import _UNALLOCATABLE_PID

from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION, METHOD_SESSION_UPDATE
from kiro_crew.config import KiroCrewConfig
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.session import SessionManager


def _runtime(tmp_path: Path) -> tuple[AcpRuntime, asyncio.StreamReader]:
    rt = AcpRuntime(work_dir=str(tmp_path))
    reader = asyncio.StreamReader()
    proc = MagicMock()
    proc.stdout = reader
    proc.stdin = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = _UNALLOCATABLE_PID
    rt._process = proc
    rt._pid = _UNALLOCATABLE_PID
    rt._initialized = True
    return rt, reader


def _update(session_id: str) -> dict:
    return {
        "method": METHOD_SESSION_UPDATE,
        "params": {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "x"},
            },
        },
    }


async def _route(rt: AcpRuntime, reader: asyncio.StreamReader, *frames: dict) -> None:
    """Feed *frames*, then a sentinel for a session of its own, and wait for it.

    The sentinel is the barrier: the reader handles frames in order, so once its
    stamp lands every frame fed before it has been handled too. That makes a
    NEGATIVE read (nothing stamped) about the behaviour, not about timing.
    """
    rt._session_queues.setdefault("sentinel", asyncio.Queue())
    for frame in (*frames, _update("sentinel")):
        reader.feed_data((json.dumps(frame) + "\n").encode())
    task = asyncio.ensure_future(rt._reader_loop())
    try:
        async with asyncio.timeout(10):
            while rt.session_activity_at("sentinel") is None:
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class TestRuntimeActivityClock:
    @pytest.mark.asyncio
    async def test_a_session_frame_stamps_that_session_only(self, tmp_path):
        rt, reader = _runtime(tmp_path)
        rt._session_queues.update({"sA": asyncio.Queue(), "sB": asyncio.Queue()})

        await _route(rt, reader, _update("sA"))

        assert rt.session_activity_at("sA") is not None
        assert rt.session_activity_at("sB") is None

    @pytest.mark.asyncio
    async def test_a_child_frame_dropped_between_turns_still_stamps_its_owner(self, tmp_path):
        """The between-turn child frame is discarded, but the child IS working."""
        rt, reader = _runtime(tmp_path)
        rt._session_queues["owner"] = asyncio.Queue()
        rt._subagent_owner = "owner"
        rt._subagent_sessions = {"child"}

        # Only the owner may be registered for the child route, so the
        # sentinel barrier cannot be used here; wait on the owner's stamp.
        reader.feed_data((json.dumps(_update("child")) + "\n").encode())
        task = asyncio.ensure_future(rt._reader_loop())
        try:
            async with asyncio.timeout(10):
                while rt.session_activity_at("owner") is None:
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert rt._session_queues["owner"].empty()  # dropped, as before

    @pytest.mark.asyncio
    async def test_an_ownerless_broadcast_stamps_nobody(self, tmp_path):
        """One tenant's traffic must not keep every tenant alive."""
        rt, reader = _runtime(tmp_path)
        rt._session_queues.update({"sA": asyncio.Queue(), "sB": asyncio.Queue()})
        ownerless = {"method": METHOD_SESSION_UPDATE, "params": {"update": {}}}

        await _route(rt, reader, ownerless)

        assert rt.session_activity_at("sA") is None
        assert rt.session_activity_at("sB") is None

    @pytest.mark.asyncio
    async def test_looping_child_permissions_still_have_a_bounded_idle_clock(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.session_cleanup import _last_activity

        rt, reader = _runtime(tmp_path)
        rt._session_queues["owner"] = asyncio.Queue()
        rt._subagent_owner = "owner"
        rt._subagent_sessions = {"child"}
        answered = AsyncMock()
        monkeypatch.setattr(rt, "_spawn_answer_task", answered)
        for request_id in range(3):
            reader.feed_data(
                (
                    json.dumps(
                        {
                            "id": request_id,
                            "method": METHOD_REQUEST_PERMISSION,
                            "params": {"sessionId": "child"},
                        }
                    )
                    + "\n"
                ).encode()
            )
        task = asyncio.ensure_future(rt._reader_loop())
        try:
            async with asyncio.timeout(10):
                while answered.await_count != 3:
                    await asyncio.sleep(0.01)
            now = time.monotonic()
            entry = MagicMock()
            entry.last_used = now - 7_201
            entry.provider.session_activity_at = rt.session_activity_at("owner")
            assert entry.provider.session_activity_at is not None
            assert now - _last_activity(entry, 3600) > 3600
            assert rt._session_queues["owner"].empty()
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def test_unregistering_forgets_the_stamp(self, tmp_path):
        rt, _ = _runtime(tmp_path)
        rt._session_activity_at["sA"] = time.monotonic()

        rt.unregister_session("sA")

        assert rt.session_activity_at("sA") is None


def _provider(activity_at: object = None) -> AsyncMock:
    m = AsyncMock()
    m.context_usage_pct = lambda: 0.0
    m.has_active_turn = lambda: False
    m.session_activity_at = activity_at
    return m


@pytest.fixture
def providers():
    return []


@pytest.fixture
def manager(providers):
    cfg = KiroCrewConfig()
    cfg.session.timeout_secs = 2

    def factory(session_key=None, **kwargs):
        providers.append(_provider())
        return providers[-1]

    return SessionManager(cfg, provider_factory=factory)


async def _stale_session(mgr: SessionManager) -> None:
    await mgr.get_or_create("dashboard:tab1")
    mgr.release("dashboard:tab1")
    async with mgr._lock:
        mgr._sessions["dashboard:tab1"].last_used = time.monotonic() - 5_000


class TestIdleSweep:
    @pytest.mark.asyncio
    async def test_recent_backend_activity_keeps_a_stale_session(self, manager, providers, caplog):
        """The regression: a session working minutes ago was expired as idle.

        The scan itself must not elect it: the post-probe re-check reads the
        same clock and would rescue it, but only after logging it as expired.
        """
        await _stale_session(manager)
        providers[0].session_activity_at = time.monotonic()

        with caplog.at_level("INFO"):
            await manager._expire_idle(timeout_secs=3600)

        assert "dashboard:tab1" in manager._sessions
        assert "expired" not in caplog.text
        await manager.close_all()

    @pytest.mark.asyncio
    async def test_old_backend_activity_still_expires(self, manager, providers):
        await _stale_session(manager)
        providers[0].session_activity_at = time.monotonic() - 5_000

        await manager._expire_idle(timeout_secs=3600)

        assert "dashboard:tab1" not in manager._sessions
        await manager.close_all()

    @pytest.mark.asyncio
    async def test_a_non_number_leaves_the_turn_clock_in_charge(self, manager, providers):
        """A provider double's auto-attribute must not pin a session."""
        await _stale_session(manager)
        providers[0].session_activity_at = MagicMock()

        await manager._expire_idle(timeout_secs=3600)

        assert "dashboard:tab1" not in manager._sessions
        await manager.close_all()

    def test_acp_provider_forwards_its_transport_stamp(self):
        provider = AcpProvider.__new__(AcpProvider)
        runtime = AcpRuntime(work_dir=".")
        runtime._session_activity_at["owner"] = 123.0
        handle = AcpSessionHandle.__new__(AcpSessionHandle)
        handle._runtime = runtime
        handle._session_id = "owner"
        transport = AcpSessionProvider.__new__(AcpSessionProvider)
        transport._handle = handle
        provider._client = transport

        assert provider.session_activity_at == 123.0
        runtime.unregister_session("owner")
        assert provider.session_activity_at is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["dashboard:tab1", "cron:job", "api:request", "subagent:child"])
    async def test_continuous_activity_cannot_extend_past_two_windows(
        self, manager, providers, key
    ):
        await manager.get_or_create(key)
        manager.release(key)
        manager._sessions[key].last_used = time.monotonic() - 7_201
        providers[0].session_activity_at = time.monotonic()

        await manager._expire_idle(timeout_secs=3600)

        assert key not in manager._sessions
        await manager.close_all()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("age, survives", [(5_000, True), (7_201, False)])
    async def test_mid_probe_activity_respects_the_same_bound(
        self, manager, providers, monkeypatch, age, survives
    ):
        await _stale_session(manager)
        manager._sessions["dashboard:tab1"].last_used = time.monotonic() - age

        async def probe(key):
            providers[0].session_activity_at = time.monotonic()
            return False

        monkeypatch.setattr(manager._cleanup, "_has_attached_subagents", probe)
        await manager._expire_idle(timeout_secs=3600)

        assert ("dashboard:tab1" in manager._sessions) is survives
        await manager.close_all()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "activity, now, survives",
        [
            (900, 1200, True),
            (900, 5000, False),
            (6000, 6000, True),
            (8200, 8200, True),
            (8201, 8201, False),
            (None, 4601, False),
        ],
    )
    async def test_dispatch_and_backend_deadlines(
        self, manager, providers, activity, now, survives
    ):
        await _stale_session(manager)
        manager._sessions["dashboard:tab1"].last_used = 1000
        providers[0].session_activity_at = activity
        cleanup = manager._cleanup_boundary()
        cleanup._deps = replace(cleanup._deps, monotonic=lambda: now)

        await manager._expire_idle(timeout_secs=3600)

        assert ("dashboard:tab1" in manager._sessions) is survives
        await manager.close_all()
