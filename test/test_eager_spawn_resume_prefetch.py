"""The resume prefetch: the TTL backstop's conditional removal, chat_runner's ``allow_resume``
wiring, the Tool Search replay gate checked before a spawn, and the sid a speculative resume
keeps when it falls back.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eager_spawn_helpers import (  # noqa: F401 -- fixtures are used by name
    _cfg,
    _isolate_armed_registry,
    _mock_state,
    _native_resume_cfg,
    _pin_prewarm_allowance,
    _stub_factory,
)

from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.state import _ChatSlot


class TestRemoveIfUnclaimed:
    """The TTL backstop's conditional removal."""

    @pytest.fixture
    def cfg(self):
        from kiro_crew.config.loader import KiroCrewConfig

        c = KiroCrewConfig()
        c.agent.provider = "acp"
        c.session.pool_size = 0
        return c

    @pytest.mark.asyncio
    async def test_removes_armed_idle_session_and_preserves_map(self, cfg):
        from kiro_crew.session import SessionManager

        mgr = SessionManager(cfg, provider_factory=_stub_factory())
        key = "dashboard:ttl-a"
        provider, _, _ = await mgr.get_or_create(key, speculative=True)
        mgr.release(key)
        mgr._session_map.set(key, "sid-ttl-a")
        assert await mgr.remove_if_unclaimed(key) is True
        assert key not in mgr._sessions
        provider.shutdown.assert_awaited()
        # The mapping survives so the next open resumes normally. Bypass
        # SessionMap.get's transcript-existence pruning — only presence in
        # the store matters here.
        assert mgr._session_map._data.get(key) is not None

    @pytest.mark.asyncio
    async def test_noops_after_real_claim(self, cfg):
        from kiro_crew.session import SessionManager

        mgr = SessionManager(cfg, provider_factory=_stub_factory())
        key = "dashboard:ttl-b"
        await mgr.get_or_create(key, speculative=True)
        mgr.release(key)
        await mgr.get_or_create(key)  # real turn consumes the marker
        mgr.release(key)
        assert await mgr.remove_if_unclaimed(key) is False
        assert key in mgr._sessions

    @pytest.mark.asyncio
    async def test_noops_while_semaphore_held(self, cfg):
        """A claimant mid-acquire (semaphore held) must never lose the
        session under it."""
        from kiro_crew.session import SessionManager

        mgr = SessionManager(cfg, provider_factory=_stub_factory())
        key = "dashboard:ttl-c"
        await mgr.get_or_create(key, speculative=True)  # release NOT called
        assert await mgr.remove_if_unclaimed(key) is False
        assert key in mgr._sessions
        mgr.release(key)

    @pytest.mark.asyncio
    async def test_noops_on_missing_key(self, cfg):
        from kiro_crew.session import SessionManager

        mgr = SessionManager(cfg, provider_factory=_stub_factory())
        assert await mgr.remove_if_unclaimed("dashboard:absent") is False


class TestResumePrefetchWiring:
    """chat_runner's allow_resume path: flag pass-through and the TTL arm."""

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module calls monkeypatch.undo()
    def _no_debounce(self, monkeypatch):
        monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)

    @pytest.mark.asyncio
    async def test_allow_resume_passes_speculative_resume(self):
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        with patch.object(chat_runner.KiroCrewConfig, "load", _native_resume_cfg()):
            await chat_runner._eager_spawn(state, slot, allow_resume=True)
        kwargs = state.sessions.get_or_create.await_args.kwargs
        assert kwargs["speculative"] is True
        assert kwargs["speculative_resume"] is True

    @pytest.mark.asyncio
    async def test_default_path_does_not_opt_in(self):
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
            await chat_runner._eager_spawn(state, slot)
        assert state.sessions.get_or_create.await_args.kwargs["speculative_resume"] is False

    @pytest.mark.asyncio
    async def test_resumed_prefetch_arms_ttl(self):
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, True))
        with patch.object(chat_runner.KiroCrewConfig, "load", _native_resume_cfg()):
            await chat_runner._eager_spawn(state, slot, allow_resume=True)
        ttl = getattr(slot, "_prefetch_ttl_task", None)
        assert ttl is not None and not ttl.done()
        ttl.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ttl

    @pytest.mark.asyncio
    async def test_fresh_spawn_does_not_arm_ttl(self):
        """A non-resumed session holds no prior transcript's native lock —
        the idle sweep alone owns its lifetime."""
        slot = _ChatSlot("t1")
        state = _mock_state(slot)  # get_or_create returns resumed=False
        with patch.object(chat_runner.KiroCrewConfig, "load", _native_resume_cfg()):
            await chat_runner._eager_spawn(state, slot, allow_resume=True)
        assert getattr(slot, "_prefetch_ttl_task", None) is None

    @pytest.mark.asyncio
    async def test_prefetch_ttl_removes_unclaimed_session(self, monkeypatch):
        monkeypatch.setattr(chat_runner, "_RESUME_PREFETCH_TTL_SECS", 0)
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        state.sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        await chat_runner._prefetch_ttl(state, slot, "dashboard:t1")
        state.sessions.remove_if_unclaimed.assert_awaited_once_with("dashboard:t1")

    @pytest.mark.asyncio
    async def test_prefetch_ttl_bails_when_slot_replaced(self, monkeypatch):
        """A DIFFERENT slot object under the same key owns the key now."""
        monkeypatch.setattr(chat_runner, "_RESUME_PREFETCH_TTL_SECS", 0)
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        state.get_slot = MagicMock(return_value=_ChatSlot("t1"))  # replaced
        state.sessions.remove_if_unclaimed = AsyncMock()
        await chat_runner._prefetch_ttl(state, slot, "dashboard:t1")
        state.sessions.remove_if_unclaimed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_prefetch_ttl_still_reaps_after_slot_deletion(self, monkeypatch):
        """Slot DELETION must not skip the reap: the delete handler removes
        the slot-key-derived history session, not a linked session key a
        channel-born slot's prefetch registered under — returning early would
        leak that process holding the native lock. The conditional removal is
        safe to run: it no-ops on an already-removed key and never touches a
        claimed session."""
        monkeypatch.setattr(chat_runner, "_RESUME_PREFETCH_TTL_SECS", 0)
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        state.get_slot = MagicMock(return_value=None)  # slot deleted
        state.sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        await chat_runner._prefetch_ttl(state, slot, "slack:12345.678")
        state.sessions.remove_if_unclaimed.assert_awaited_once_with("slack:12345.678")


class TestToolSearchReplayPrefetchGate:
    """The resume prefetch asks the provider's replay predicate before it spawns.

    With Tool Search on, the kiro backend answers a direct dashboard resume with
    a fresh session plus conversation replay instead of ``session/load``, so the
    speculative load comes back ``resumed=False`` and is refused -- after a
    whole runtime was spawned and torn down. ``_eager_spawn`` reads the same
    predicate the provider does and leaves such a slot to its first turn.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module calls monkeypatch.undo()
    def _no_debounce(self, monkeypatch):
        monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)

    @pytest.mark.asyncio
    async def test_replayed_resume_spawns_nothing(self, caplog):
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)),
            caplog.at_level("INFO", logger=chat_runner.logger.name),
        ):
            await chat_runner._eager_spawn(state, slot, allow_resume=True)
        state.sessions.get_or_create.assert_not_awaited()
        reasons = [r.getMessage() for r in caplog.records if "left to first turn" in r.getMessage()]
        assert reasons == ["Eager spawn: dashboard:t1 left to first turn (tool-search replay)"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "native",
        [
            pytest.param({"tool_search": False}, id="tool-search-off"),
            pytest.param({"acp_backend": ACP_BACKEND_KAS}, id="non-kiro-backend"),
        ],
    )
    async def test_native_resume_still_prefetches(self, native):
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        loader = _cfg(True)
        for name, value in native.items():
            setattr(loader.return_value.agent, name, value)
        with patch.object(chat_runner.KiroCrewConfig, "load", loader):
            await chat_runner._eager_spawn(state, slot, allow_resume=True)
        assert state.sessions.get_or_create.await_args.kwargs["speculative_resume"] is True


class TestSpecResumeFallbackMapGuard:
    """A speculative resume that fell back must not overwrite the sid."""

    @pytest.fixture
    def cfg(self):
        from kiro_crew.config.loader import KiroCrewConfig

        c = KiroCrewConfig()
        c.agent.provider = "acp"
        c.session.pool_size = 0
        return c

    @pytest.mark.asyncio
    async def test_fallback_keeps_original_sid(self, cfg, tmp_path, monkeypatch):
        from kiro_crew.session import SessionManager

        old_sid = "real-transcript-sid"
        sessions_dir = tmp_path / "kiro-sessions"
        sessions_dir.mkdir()
        monkeypatch.setattr("kiro_crew.session_map._kiro_sessions_dir", lambda: sessions_dir)
        (sessions_dir / f"{old_sid}.json").write_text("{}")
        (sessions_dir / f"{old_sid}.jsonl").write_text("x" * 32)

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.shutdown = AsyncMock()
            m.context_usage_pct = lambda: 0.0
            m.is_alive.return_value = True
            m.is_process_alive = lambda: True
            m.cwd = "/tmp"
            m.client = MagicMock()
            m.client.resumed = False  # the load FELL BACK to a fresh session
            m.client._session_id = "empty-fallback-sid"
            return m

        monkeypatch.setattr("kiro_crew.providers.acp.AcpProvider", object)
        mgr = SessionManager(cfg, provider_factory=factory)
        key = "dashboard:fallback-a"
        mgr._session_map.set(key, old_sid)

        from kiro_crew.session import SpeculativeResumeRefused

        # A failed speculative resume is rejected BEFORE registration: no
        # claimable fallback session may ever exist — a real turn queued
        # during the load would claim it and strand its exchanges behind the
        # preserved old sid on the next reopen.
        with pytest.raises(SpeculativeResumeRefused):
            await mgr.get_or_create(key, speculative=True, speculative_resume=True)
        # Nothing registered; the pointer to the real transcript survives.
        assert key not in mgr._sessions
        assert mgr._session_map.get(key) == old_sid

    @pytest.mark.asyncio
    async def test_provider_switch_fallback_never_persists_empty_sid(
        self, cfg, tmp_path, monkeypatch
    ):
        """The switch branch mutates ``resume_sid`` to
        None, so a classification keyed on ``resume_sid`` misreads the
        provider-switch fallback as a normal fresh session and persists the
        EMPTY speculative sid — the next real open would resume that empty
        session (resumed=True) and skip the history replay, losing the prior
        context. Classification must key on the caller's ``speculative_resume``
        opt-in, which no branch mutates."""
        from kiro_crew.session import SessionManager

        old_sid = "real-transcript-sid"
        sessions_dir = tmp_path / "kiro-sessions"
        sessions_dir.mkdir()
        monkeypatch.setattr("kiro_crew.session_map._kiro_sessions_dir", lambda: sessions_dir)
        (sessions_dir / f"{old_sid}.json").write_text("{}")
        (sessions_dir / f"{old_sid}.jsonl").write_text("x" * 32)

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.shutdown = AsyncMock()
            m.context_usage_pct = lambda: 0.0
            m.is_alive.return_value = True
            m.is_process_alive = lambda: True
            m.cwd = "/tmp"
            m.client = MagicMock()
            m.client.resumed = False  # no session/load ran: sid was discarded
            m.client._session_id = "empty-fallback-sid"
            return m

        monkeypatch.setattr("kiro_crew.providers.acp.AcpProvider", object)
        # Force the switch branch: resume_sid is cleared and the stored sid
        # wiped, exactly the mutation the classification must be immune to.
        monkeypatch.setattr("kiro_crew.session.detect_provider_switch", lambda *a: True)
        mgr = SessionManager(cfg, provider_factory=factory)
        key = "dashboard:fallback-switch"
        mgr._session_map.set(key, old_sid)

        from kiro_crew.session import SpeculativeResumeRefused

        # The provider-switch branch clears resume_sid mid-flight; the
        # rejection must key on the caller's opt-in and fire anyway, so the
        # empty speculative sid can never be persisted or claimed.
        with pytest.raises(SpeculativeResumeRefused):
            await mgr.get_or_create(key, speculative=True, speculative_resume=True)
        assert key not in mgr._sessions
        # The empty speculative sid must never land in the map: the next real
        # open cold-starts fresh (resumed=False) and injects history normally.
        assert mgr._session_map.get(key) != "empty-fallback-sid"

    @pytest.mark.asyncio
    async def test_refusal_kills_provider_via_nonblocking_dispatch(
        self, cfg, tmp_path, monkeypatch
    ):
        """The refusal raise lands in the post-start BaseException handler,
        which is ROUTINE under resume prefetch (every failed load passes
        through it). It must kill the orphaned provider via the executor
        dispatch, never inline — _sync_kill_provider blocks the event loop
        (os.waitpid / taskkill)."""
        from kiro_crew.session import SessionManager, SpeculativeResumeRefused

        old_sid = "real-transcript-sid"
        sessions_dir = tmp_path / "kiro-sessions"
        sessions_dir.mkdir()
        monkeypatch.setattr("kiro_crew.session_map._kiro_sessions_dir", lambda: sessions_dir)
        (sessions_dir / f"{old_sid}.json").write_text("{}")
        (sessions_dir / f"{old_sid}.jsonl").write_text("x" * 32)

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.shutdown = AsyncMock()
            m.context_usage_pct = lambda: 0.0
            m.is_alive.return_value = True
            m.is_process_alive = lambda: True
            m.cwd = "/tmp"
            m.client = MagicMock()
            m.client.resumed = False  # the load FELL BACK
            m.client._session_id = "empty-fallback-sid"
            return m

        monkeypatch.setattr("kiro_crew.providers.acp.AcpProvider", object)
        mgr = SessionManager(cfg, provider_factory=factory)
        key = "dashboard:kill-dispatch"
        mgr._session_map.set(key, old_sid)

        with patch.object(SessionManager, "_dispatch_hard_kill") as dispatch:
            with pytest.raises(SpeculativeResumeRefused):
                await mgr.get_or_create(key, speculative=True, speculative_resume=True)
        dispatch.assert_called_once()
