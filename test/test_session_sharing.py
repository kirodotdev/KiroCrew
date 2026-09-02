"""Tests for Phase 3 session sharing in SubagentManager.

Validates that when session_sharing=True and the parent session is
ACP/kiro-backed, subagents use a shared AcpRuntime instead of spawning
fresh processes — and that fallback to legacy path works correctly.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.runtime import AcpRuntimeDead
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import AcpEvent
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.run_coordinator import MemoryRunCoordinator
from kiro_crew.subagent import SubagentManager

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")

# Subagent-registry isolation is provided globally by the autouse
# ``_isolate_subagents_dir`` fixture in ``conftest.py`` — no per-file fixture needed.


async def _wait_until_done(info, *, timeout: float = 30.0) -> None:
    """Wait for a spawned subagent to finish, deterministically.

    ``manager.spawn`` runs the subagent as a background asyncio task that flips
    ``info.done`` once ``_run`` completes. Sleeping a fixed amount races that
    task under load — the source of the intermittent
    ``test_shared_session_cleanup_destroys_handle`` failure (``info.done`` still
    ``False`` when the assertion ran). Polling ``info.done`` against a deadline
    is race-free: it returns as soon as the task settles and only fails if the
    task genuinely never completes.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not info.done:
        if loop.time() >= deadline:
            raise AssertionError(f"subagent {info.id} did not complete within {timeout}s")
        await asyncio.sleep(0.01)


async def _wait_until_awaited(mock_attr, label: str, *, timeout: float = 30.0) -> None:
    """Wait for an async mock to have been awaited, deterministically.

    Companion to :func:`_wait_until_done`, for assertions about TEARDOWN rather
    than completion. ``info.done`` flips inside ``_run_inner``, but the
    shared-session teardown that reaches ``handle.destroy()`` runs strictly
    afterwards — the ``asyncio.wait_for`` future in ``_run`` has to resolve, then
    its cleanup awaits ``provider.shutdown()``. So ``_wait_until_done``
    returning does NOT imply teardown has happened, and asserting on it
    immediately races the completion task in exactly the way the fixed sleep did
    before #574 — that fix made the ``info.done`` half deterministic and left
    this half racing. Polling the await against a deadline closes it: it returns
    as soon as teardown lands, and still fails if teardown never happens.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not mock_attr.await_count:
        if loop.time() >= deadline:
            raise AssertionError(f"{label} was not awaited within {timeout}s")
        await asyncio.sleep(0.01)


def _mock_sessions(*, sharing_eligible: bool = True) -> MagicMock:
    """Create a mock SessionManager with session-sharing support."""
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_agent = MagicMock(return_value="kirocrew")
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.has_session = MagicMock(return_value=True)
    sessions.is_session_sharing_eligible = MagicMock(return_value=sharing_eligible)
    sessions.record_success = MagicMock()
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions._pool_cwd = "/home/user/.kirocrew/workspace"

    # Legacy get_or_create path
    legacy_provider = AsyncMock()
    legacy_provider.start = AsyncMock()
    legacy_provider.shutdown = AsyncMock()
    legacy_provider.context_usage_pct = lambda: 0.0
    legacy_provider.session_id = "legacy-session-id"

    async def _empty_stream(*_a, **_kw):
        return
        yield

    legacy_provider.stream = MagicMock(side_effect=lambda *a, **kw: _empty_stream())
    sessions.get_or_create = AsyncMock(return_value=(legacy_provider, True, False))

    # Session-sharing runtime
    mock_handle = MagicMock()
    mock_handle.session_id = "shared-session-abc"
    mock_handle.is_turn_active = False
    mock_handle.destroy = AsyncMock()

    async def _handle_prompt(msg):
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="shared response")
        yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    mock_handle.prompt = _handle_prompt
    mock_handle.last_prompt_stats = MagicMock(
        context_pct=10.0, context_used_tokens=1000, context_window_tokens=200000
    )

    mock_runtime = MagicMock()
    mock_runtime.is_alive.return_value = True
    mock_runtime.pid = 12345
    mock_runtime.create_session = AsyncMock(return_value=mock_handle)
    sessions.get_subagent_runtime = AsyncMock(return_value=mock_runtime)

    return sessions


def _mock_ctx_builder_auto() -> MagicMock:
    """Create a mock ContextBuilder with auto-approve."""
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    ctx.hooks.auto_approve_subagent_tools = True
    return ctx


def _cfg_patch(session_sharing: bool = True):
    """Patch KiroCrewConfig.load() to return session_sharing flag."""
    cfg = MagicMock()
    cfg.agent.session_sharing = session_sharing
    cfg.agent.spawn_min_memory_gb = 0  # disable memory check
    cfg.agent.subagent_cwd_allowed_roots = ["~/workspace", "~/workplace"]
    return patch("kiro_crew.subagent.KiroCrewConfig.load", return_value=cfg)


class TestSessionSharingDecision:
    """Tests for _should_use_session_sharing decision logic."""

    @pytest.mark.asyncio
    async def test_session_sharing_on_eligible_parent(self):
        """Session sharing is used when config=True and parent is eligible."""
        sessions = _mock_sessions(sharing_eligible=True)
        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder_auto(),
            is_yolo=lambda: True,
        )

        from kiro_crew.subagent import SubagentInfo

        info = SubagentInfo(id="test1", task="hello", parent_session_key="dashboard:slot1")
        with _cfg_patch(session_sharing=True):
            result = manager._should_use_session_sharing(info)
        assert result is True

    @pytest.mark.asyncio
    async def test_session_sharing_off_in_config(self):
        """Session sharing is NOT used when config=False."""
        sessions = _mock_sessions(sharing_eligible=True)
        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder_auto(),
            is_yolo=lambda: True,
        )

        from kiro_crew.subagent import SubagentInfo

        info = SubagentInfo(id="test2", task="hello", parent_session_key="dashboard:slot1")
        with _cfg_patch(session_sharing=False):
            result = manager._should_use_session_sharing(info)
        assert result is False

    @pytest.mark.asyncio
    async def test_session_sharing_ineligible_parent(self):
        """Session sharing is NOT used when parent is CC-backed."""
        sessions = _mock_sessions(sharing_eligible=False)
        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder_auto(),
            is_yolo=lambda: True,
        )

        from kiro_crew.subagent import SubagentInfo

        info = SubagentInfo(id="test3", task="hello", parent_session_key="dashboard:slot1")
        with _cfg_patch(session_sharing=True):
            result = manager._should_use_session_sharing(info)
        assert result is False

    @pytest.mark.asyncio
    async def test_session_sharing_skipped_for_cc_overrides(self):
        """Session sharing is NOT used when CC-specific kwargs are set."""
        sessions = _mock_sessions(sharing_eligible=True)
        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder_auto(),
            is_yolo=lambda: True,
        )

        from kiro_crew.subagent import SubagentInfo

        # model override forces CC path
        info = SubagentInfo(
            id="test4",
            task="hello",
            parent_session_key="dashboard:slot1",
            model="opus-4",
        )
        with _cfg_patch(session_sharing=True):
            result = manager._should_use_session_sharing(info)
        assert result is False

    @pytest.mark.asyncio
    async def test_session_sharing_skipped_no_parent(self):
        """Session sharing is NOT used when there's no parent session key."""
        sessions = _mock_sessions(sharing_eligible=True)
        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder_auto(),
            is_yolo=lambda: True,
        )

        from kiro_crew.subagent import SubagentInfo

        info = SubagentInfo(id="test5", task="hello", parent_session_key="")
        with _cfg_patch(session_sharing=True):
            result = manager._should_use_session_sharing(info)
        assert result is False


class TestSessionSharingSpawn:
    """Tests for session-sharing subagent spawn and cleanup."""

    @pytest.mark.asyncio
    async def test_shared_session_creates_on_runtime(self):
        """When session sharing is used, create_session is called on the runtime."""
        sessions = _mock_sessions(sharing_eligible=True)
        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder_auto(),
            is_yolo=lambda: True,
        )

        with (
            _cfg_patch(session_sharing=True),
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
        ):
            info = manager.spawn("test task", parent_session_key="dashboard:slot1")
            assert info is not None
            # Wait for the subagent to complete
            await _wait_until_done(info)

        # Verify runtime.create_session was called (not get_or_create)
        sessions.get_subagent_runtime.assert_awaited_once_with("dashboard:slot1")
        runtime = await sessions.get_subagent_runtime("dashboard:slot1")
        runtime.create_session.assert_awaited_once()
        # get_or_create should NOT have been called
        sessions.get_or_create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shared_session_sets_flags(self):
        """Session sharing sets _session_sharing=True and _shared_provider."""
        sessions = _mock_sessions(sharing_eligible=True)
        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder_auto(),
            is_yolo=lambda: True,
        )

        with (
            _cfg_patch(session_sharing=True),
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
        ):
            info = manager.spawn("test task", parent_session_key="dashboard:slot1")
            await _wait_until_done(info)

        assert info._session_sharing is True
        assert info._shared_provider is not None
        assert isinstance(info._shared_provider, AcpSessionProvider)

    @pytest.mark.asyncio
    async def test_shared_session_never_persists_kill_authority(self):
        """Recovery must not treat the parent runtime as this run's child."""
        from kiro_crew.subagent import SubagentInfo

        sessions = _mock_sessions(sharing_eligible=True)
        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder_auto(),
            is_yolo=lambda: True,
        )
        info = SubagentInfo(
            id="shared1",
            task="hello",
            parent_session_key="dashboard:slot1",
        )

        event_loop_thread = threading.get_ident()
        update_threads: list[int] = []
        with (
            patch(
                "kiro_crew.subagent.update_state",
                side_effect=lambda *_args, **_kwargs: update_threads.append(threading.get_ident()),
            ) as update,
            patch("kiro_crew.subagent.platform_compat.process_start_time") as process_start_time,
        ):
            provider = await manager._create_shared_session(info, "subagent:shared1", "kirocrew")

        process_start_time.assert_not_called()
        update.assert_called_once()
        assert update.call_args.args == ("shared1",)
        assert update.call_args.kwargs["pid"] == 12345
        assert update.call_args.kwargs["pid_start_id"] == ""
        assert update.call_args.kwargs["process_owned"] is False
        assert update_threads != [event_loop_thread]
        await provider.shutdown()

    @pytest.mark.asyncio
    async def test_shared_session_cleanup_destroys_handle(self):
        """On completion, shared session calls provider.shutdown() not reset()."""
        sessions = _mock_sessions(sharing_eligible=True)
        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder_auto(),
            is_yolo=lambda: True,
        )

        with (
            _cfg_patch(session_sharing=True),
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
        ):
            info = manager.spawn("test task", parent_session_key="dashboard:slot1")
            await _wait_until_done(info)

        assert info.done  # completed
        # reset should NOT have been called (shared path skips it)
        sessions.reset.assert_not_awaited()
        # release should NOT have been called
        sessions.release.assert_not_called()
        # The handle's destroy should have been called via shutdown().
        # Teardown runs AFTER info.done, so wait for it rather than assuming the
        # completion task already got there (see _wait_until_awaited).
        runtime = await sessions.get_subagent_runtime("dashboard:slot1")
        handle = await runtime.create_session()
        await _wait_until_awaited(handle.destroy, "handle.destroy")

    @pytest.mark.asyncio
    async def test_legacy_path_when_sharing_off(self):
        """When session_sharing=False, legacy get_or_create path is used."""
        sessions = _mock_sessions(sharing_eligible=True)
        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder_auto(),
            is_yolo=lambda: True,
        )

        with (
            _cfg_patch(session_sharing=False),
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
        ):
            info = manager.spawn("test task", parent_session_key="dashboard:slot1")
            await _wait_until_done(info)

        # Legacy path: get_or_create called, runtime NOT called
        sessions.get_or_create.assert_awaited()
        sessions.get_subagent_runtime.assert_not_awaited()
        assert info._session_sharing is False


class TestSessionSharingFallback:
    """Tests for fallback to legacy path when session sharing fails."""

    @pytest.mark.asyncio
    async def test_fallback_on_runtime_dead(self):
        """When runtime dies during create_session, falls back to legacy path."""
        sessions = _mock_sessions(sharing_eligible=True)
        # Make get_subagent_runtime raise AcpRuntimeDead
        sessions.get_subagent_runtime = AsyncMock(side_effect=AcpRuntimeDead("process died"))

        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder_auto(),
            is_yolo=lambda: True,
        )

        with (
            _cfg_patch(session_sharing=True),
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
        ):
            info = manager.spawn("test task", parent_session_key="dashboard:slot1")
            await _wait_until_done(info)

        # Should have fallen back to get_or_create
        sessions.get_or_create.assert_awaited()
        # Flags should be reset
        assert info._session_sharing is False
        assert info._shared_provider is None

    @pytest.mark.asyncio
    async def test_fallback_on_create_session_error(self):
        """When create_session fails, falls back to legacy path."""
        sessions = _mock_sessions(sharing_eligible=True)
        # Runtime is fine but create_session raises
        mock_runtime = MagicMock()
        mock_runtime.is_alive.return_value = True
        mock_runtime.pid = 99999
        mock_runtime.create_session = AsyncMock(side_effect=RuntimeError("session limit reached"))
        sessions.get_subagent_runtime = AsyncMock(return_value=mock_runtime)

        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder_auto(),
            is_yolo=lambda: True,
        )

        with (
            _cfg_patch(session_sharing=True),
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
        ):
            info = manager.spawn("test task", parent_session_key="dashboard:slot1")
            await _wait_until_done(info)

        # Should have fallen back to get_or_create
        sessions.get_or_create.assert_awaited()
        assert info._session_sharing is False


class TestSessionSharingReaper:
    """Tests for reaper behavior with session-sharing subagents."""

    @pytest.mark.asyncio
    async def test_force_reap_shared_calls_shutdown(self):
        """Reaper for session-sharing subagents calls shutdown, not reset."""
        sessions = _mock_sessions(sharing_eligible=True)
        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder_auto(),
            is_yolo=lambda: True,
        )

        from kiro_crew.subagent import SubagentInfo

        mock_provider = AsyncMock()
        mock_provider.shutdown = AsyncMock()
        info = SubagentInfo(
            id="reap1",
            task="long task",
            parent_session_key="dashboard:slot1",
            _session_sharing=True,
            _shared_provider=mock_provider,
        )
        manager._agents["reap1"] = info
        manager._tasks["reap1"] = MagicMock(done=MagicMock(return_value=False))
        manager._tasks["reap1"].cancel = MagicMock()

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            await manager._force_reap("reap1", info, 1800.0)

        # shutdown called on the shared provider
        mock_provider.shutdown.assert_awaited_once()
        # reset NOT called (no dedicated process to kill)
        sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_force_reap_legacy_calls_reset(self):
        """Reaper for legacy subagents still calls reset (kill process)."""
        sessions = _mock_sessions(sharing_eligible=True)
        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder_auto(),
            is_yolo=lambda: True,
        )

        from kiro_crew.subagent import SubagentInfo

        info = SubagentInfo(
            id="reap2",
            task="long task",
            parent_session_key="dashboard:slot1",
            _session_sharing=False,
        )
        manager._agents["reap2"] = info
        manager._tasks["reap2"] = MagicMock(done=MagicMock(return_value=False))
        manager._tasks["reap2"].cancel = MagicMock()

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            await manager._force_reap("reap2", info, 1800.0)

        # Legacy path: reset called
        sessions.reset.assert_awaited_once()


class TestSessionSharingMultiAgent:
    """Integration tests: multiple subagents on same shared runtime."""

    @pytest.mark.asyncio
    async def test_two_subagents_share_same_runtime(self):
        """Two subagents from same parent both use get_subagent_runtime."""
        sessions = _mock_sessions(sharing_eligible=True)
        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder_auto(),
            is_yolo=lambda: True,
            max_concurrent=4,
            coordinator=MemoryRunCoordinator(),
        )
        # Disable stagger so both spawns start immediately
        manager._spawn_stagger_secs = 0.0

        with (
            _cfg_patch(session_sharing=True),
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
        ):
            info1 = manager.spawn("task A", parent_session_key="dashboard:slot1")
            info2 = manager.spawn("task B", parent_session_key="dashboard:slot1")
            tasks = [manager._tasks[info1.id], manager._tasks[info2.id]]
            await asyncio.gather(*tasks)

        # Both should use session sharing
        assert info1._session_sharing is True
        assert info2._session_sharing is True
        # get_subagent_runtime called twice with same parent key
        assert sessions.get_subagent_runtime.await_count == 2
        calls = sessions.get_subagent_runtime.await_args_list
        assert calls[0].args[0] == "dashboard:slot1"
        assert calls[1].args[0] == "dashboard:slot1"

    @pytest.mark.asyncio
    async def test_two_subagents_different_parents_get_separate_runtimes(self):
        """Subagents from different parents call get_subagent_runtime with their own key."""
        sessions = _mock_sessions(sharing_eligible=True)
        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder_auto(),
            is_yolo=lambda: True,
            max_concurrent=4,
            coordinator=MemoryRunCoordinator(),
        )
        manager._spawn_stagger_secs = 0.0

        with (
            _cfg_patch(session_sharing=True),
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
        ):
            info1 = manager.spawn("task A", parent_session_key="dashboard:slot1")
            info2 = manager.spawn("task B", parent_session_key="dashboard:slot2")
            tasks = [manager._tasks[info1.id], manager._tasks[info2.id]]
            await asyncio.gather(*tasks)

        assert info1._session_sharing is True
        assert info2._session_sharing is True
        # get_subagent_runtime called with different parent keys
        keys = [c.args[0] for c in sessions.get_subagent_runtime.await_args_list]
        assert "dashboard:slot1" in keys
        assert "dashboard:slot2" in keys

    @pytest.mark.asyncio
    async def test_subagent_gets_unique_session_id(self):
        """Each subagent gets its own session from create_session."""
        sessions = _mock_sessions(sharing_eligible=True)
        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder_auto(),
            is_yolo=lambda: True,
        )

        with (
            _cfg_patch(session_sharing=True),
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
        ):
            info = manager.spawn("test task", parent_session_key="dashboard:slot1")
            await _wait_until_done(info)

        # The provider wraps the handle which has the session_id
        assert info._shared_provider is not None
        assert info._shared_provider.session_id == "shared-session-abc"


class TestSessionSharingParentReset:
    """Tests for cleanup when parent session is reset."""

    @pytest.mark.asyncio
    async def test_release_subagent_runtime_on_parent_reset(self):
        """Verify release_subagent_runtime is called during reset()."""
        # We can't easily test the full reset() without a real session,
        # but we can test that the cleanup code path works correctly
        # by verifying release_subagent_runtime kills the runtime.
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.session import SessionManager

        cfg = KiroCrewConfig.load()
        sm = SessionManager(cfg)

        # Simulate a subagent runtime being registered
        mock_runtime = MagicMock()
        mock_runtime.is_alive.return_value = True
        mock_runtime.kill = AsyncMock()
        sm._subagent_runtimes["dashboard:slot1"] = mock_runtime
        sm._subagent_runtime_locks["dashboard:slot1"] = asyncio.Lock()

        # Call release_subagent_runtime
        await sm.release_subagent_runtime("dashboard:slot1")

        # Runtime should be killed
        mock_runtime.kill.assert_awaited_once()
        # Entries should be removed
        assert "dashboard:slot1" not in sm._subagent_runtimes
        assert "dashboard:slot1" not in sm._subagent_runtime_locks

    @pytest.mark.asyncio
    async def test_release_subagent_runtime_noop_when_missing(self):
        """release_subagent_runtime is safe when no runtime exists."""
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.session import SessionManager

        cfg = KiroCrewConfig.load()
        sm = SessionManager(cfg)

        # Should not raise
        await sm.release_subagent_runtime("nonexistent:key")

    @pytest.mark.asyncio
    async def test_release_subagent_runtime_handles_kill_failure(self):
        """release_subagent_runtime handles errors from runtime.kill()."""
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.session import SessionManager

        cfg = KiroCrewConfig.load()
        sm = SessionManager(cfg)

        mock_runtime = MagicMock()
        mock_runtime.kill = AsyncMock(side_effect=OSError("process already dead"))
        sm._subagent_runtimes["dashboard:slot1"] = mock_runtime
        sm._subagent_runtime_locks["dashboard:slot1"] = asyncio.Lock()

        # Should not raise despite kill() failing
        await sm.release_subagent_runtime("dashboard:slot1")
        # Entry should still be cleaned up
        assert "dashboard:slot1" not in sm._subagent_runtimes

    @pytest.mark.asyncio
    async def test_get_subagent_runtime_reuses_alive_runtime(self):
        """get_subagent_runtime returns existing runtime if alive."""
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.session import SessionManager

        cfg = KiroCrewConfig.load()
        sm = SessionManager(cfg)

        # Pre-populate with an alive runtime
        mock_runtime = MagicMock()
        mock_runtime.is_alive.return_value = True
        sm._subagent_runtimes["dashboard:slot1"] = mock_runtime
        sm._subagent_runtime_locks["dashboard:slot1"] = asyncio.Lock()

        # Mock the _sessions dict for agent lookup
        mock_session = MagicMock()
        mock_session.agent = "kirocrew"
        sm._sessions["dashboard:slot1"] = mock_session

        result = await sm.get_subagent_runtime("dashboard:slot1")
        assert result is mock_runtime  # reused, not re-spawned

    @pytest.mark.asyncio
    async def test_get_subagent_runtime_retries_once_on_spawn_failure(self, monkeypatch):
        """get_subagent_runtime retries spawn once on AcpRuntimeDead (parity with
        get_bg_session): the first spawn dies, the second succeeds -> live runtime.
        Regression guard: the retry loop was previously dead code (spawn raised
        straight through without being caught, so max_retries had no effect)."""
        from kiro_crew.acp.runtime import AcpRuntimeDead
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.session import SessionManager

        sm = SessionManager(KiroCrewConfig.load())
        sm._get_session_agent = lambda k: "kirocrew"  # type: ignore[assignment]

        calls = {"n": 0}

        class _FlakyRuntime:
            def __init__(self, agent=None):
                self._alive = False

            async def spawn(self):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise AcpRuntimeDead("transient spawn failure")
                self._alive = True

            def is_alive(self):
                return self._alive

        # Inline import in get_subagent_runtime resolves AcpRuntime at call time,
        # so patching the source module is picked up.
        monkeypatch.setattr("kiro_crew.acp.runtime.AcpRuntime", _FlakyRuntime)
        rt = await sm.get_subagent_runtime("dashboard:slot1")
        assert calls["n"] == 2  # retried once after the transient failure
        assert rt.is_alive()
        assert sm._subagent_runtimes["dashboard:slot1"] is rt

    @pytest.mark.asyncio
    async def test_get_subagent_runtime_raises_after_retries_exhausted(self, monkeypatch):
        """When every spawn attempt fails, get_subagent_runtime raises
        AcpRuntimeDead (so the caller falls back to the legacy path) after the
        retry budget is exhausted, and records no runtime."""
        from kiro_crew.acp.runtime import AcpRuntimeDead
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.session import SessionManager

        sm = SessionManager(KiroCrewConfig.load())
        sm._get_session_agent = lambda k: "kirocrew"  # type: ignore[assignment]

        calls = {"n": 0}

        class _DeadRuntime:
            def __init__(self, agent=None):
                pass

            async def spawn(self):
                calls["n"] += 1
                raise AcpRuntimeDead("permanent spawn failure")

            def is_alive(self):
                return False

        monkeypatch.setattr("kiro_crew.acp.runtime.AcpRuntime", _DeadRuntime)
        with pytest.raises(AcpRuntimeDead):
            await sm.get_subagent_runtime("dashboard:slot1")
        assert calls["n"] == 2  # initial attempt + one retry
        assert "dashboard:slot1" not in sm._subagent_runtimes
