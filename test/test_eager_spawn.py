"""Tests for speculative session creation (session.eager_spawn).

Covers the schedule/debounce contract in ``chat_runner.schedule_eager_spawn``
and the re-validation ordering in ``_eager_spawn``: config gate, task
supersession, slot-liveness bail, the turn-in-flight bail that protects the
deferred project-reset killpg constraint, semaphore release after creation,
and the handler wiring on project set.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import _eager_spawn, schedule_eager_spawn
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


def _mock_state(slot: _ChatSlot) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state.get_slot = MagicMock(return_value=slot)
    state.sessions = MagicMock()
    state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.remove = AsyncMock()
    state.sessions.remove_if_unclaimed = AsyncMock(return_value=True)
    state.sessions.resumable_hint = MagicMock(return_value=True)
    return state


def _cfg(enabled: bool) -> MagicMock:
    cfg = MagicMock()
    cfg.session.eager_spawn = enabled
    bindings = MagicMock()
    bindings.kiro_agent = "kirocrew"
    bindings.model = ""
    cfg_loader = MagicMock(return_value=cfg)
    return cfg_loader


class TestScheduleEagerSpawn:
    def test_config_loader_parses_eager_spawn(self, tmp_path):
        """The loader's explicit SessionConfig construction must carry the flag.

        The dataclass field alone is not enough: KiroCrewConfig.load() builds
        SessionConfig with per-field parsing, and a field missing there is
        silently dropped on load — then the boot-time migration write-back
        saves the dataclass default over the user's setting. Caught live.
        With the default now True, the falsifying direction is an explicit
        FALSE surviving the round-trip; the empty config pins the default.
        """
        import json
        import unittest.mock

        def _load(data: dict, name: str):
            tmp = tmp_path / name
            tmp.write_text(json.dumps(data))
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                return chat_runner.KiroCrewConfig.load()

        assert _load({"session": {"eager_spawn": False}}, "off.json").session.eager_spawn is False
        assert _load({}, "empty.json").session.eager_spawn is True  # default on

    @pytest.mark.asyncio
    async def test_noop_when_flag_disabled(self):
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(False)):
            schedule_eager_spawn(state, slot)
        assert slot._eager_spawn_task is None

    @pytest.mark.asyncio
    async def test_noop_when_config_unreadable(self):
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        with patch.object(
            chat_runner.KiroCrewConfig, "load", MagicMock(side_effect=OSError("boom"))
        ):
            schedule_eager_spawn(state, slot)
        assert slot._eager_spawn_task is None

    @pytest.mark.asyncio
    async def test_newer_signal_cancels_older_task(self):
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
            schedule_eager_spawn(state, slot)
            first = slot._eager_spawn_task
            assert first is not None
            schedule_eager_spawn(state, slot)
            second = slot._eager_spawn_task
        assert second is not first
        # The older task must be cancelled — it holds the stale slot state.
        with pytest.raises(asyncio.CancelledError):
            await first
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second


class TestEagerSpawn:
    """_eager_spawn body, with debounce zeroed for test speed."""

    @pytest.fixture(autouse=True)
    def _no_debounce(self, monkeypatch):
        monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)

    @pytest.mark.asyncio
    async def test_creates_session_with_slot_bindings_and_releases(self, tmp_path):
        slot = _ChatSlot("t1")
        slot.agent = "wfe-oncall"
        slot.project = str(tmp_path)
        state = _mock_state(slot)
        bindings = MagicMock()
        bindings.kiro_agent = "wfe-oncall"
        bindings.model = ""
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)),
            patch.object(chat_runner, "resolve_agent_bindings", return_value=bindings),
        ):
            await _eager_spawn(state, slot)
        state.sessions.get_or_create.assert_awaited_once()
        kwargs = state.sessions.get_or_create.await_args.kwargs
        assert kwargs["agent"] == "wfe-oncall"
        assert kwargs["cwd"] == str(tmp_path)
        # The per-session semaphore acquired by get_or_create MUST be released
        # here: no turn follows, and a held semaphore would deadlock the first
        # real message.
        key = state.sessions.get_or_create.await_args.args[0]
        state.sessions.release.assert_called_once_with(key)

    @pytest.mark.asyncio
    async def test_bails_when_slot_replaced(self):
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        state.get_slot = MagicMock(return_value=_ChatSlot("t1"))  # different object
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
            await _eager_spawn(state, slot)
        state.sessions.get_or_create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bails_when_turn_running_without_consuming_reset(self, tmp_path):
        """The turn-in-flight bail must precede the pending-reset consume.

        Consuming the reset kills the session's process group; when the
        project-set call originated from the set_project MCP tool inside that
        session, a mid-turn consume would kill the caller.
        """
        slot = _ChatSlot("t1")
        # slot.running derives from slot.task being a live task.
        _turn = asyncio.get_running_loop().create_future()
        _task = asyncio.ensure_future(_turn)
        slot.task = _task
        slot._pending_reset_history_key = "dashboard:t1"
        state = _mock_state(slot)
        try:
            with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
                await _eager_spawn(state, slot)
        finally:
            _turn.set_result(None)
            await _task
        state.sessions.get_or_create.assert_not_awaited()
        state.sessions.reset.assert_not_awaited()
        assert slot._pending_reset_history_key == "dashboard:t1"

    @pytest.mark.asyncio
    async def test_consumes_pending_reset_when_idle(self, tmp_path):
        slot = _ChatSlot("t1")
        slot.project = str(tmp_path)
        slot._pending_reset_history_key = "dashboard:t1"
        state = _mock_state(slot)
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
            await _eager_spawn(state, slot)
        state.sessions.reset.assert_awaited_once_with("dashboard:t1")
        assert slot._pending_reset_history_key is None
        state.sessions.get_or_create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_or_create_failure_is_swallowed(self):
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        state.sessions.get_or_create = AsyncMock(side_effect=RuntimeError("spawn failed"))
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
            await _eager_spawn(state, slot)  # must not raise
        state.sessions.release.assert_not_called()

    @pytest.mark.asyncio
    async def test_passes_speculative_flag(self):
        """The eager path must create speculatively: the flag is what keeps
        the one-shot first-turn context injection armed for the real message
        (atomically, inside get_or_create — both local reviewers flagged the
        earlier rearm-after-release design as racy)."""
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
            await _eager_spawn(state, slot)
        assert state.sessions.get_or_create.await_args.kwargs["speculative"] is True

    @pytest.mark.asyncio
    async def test_resumable_key_refusal_is_clean(self):
        """SpeculativeResumeRefused is an expected outcome, not an error: the
        real first turn must be the one that resumes."""
        from kiro_crew.session import SpeculativeResumeRefused

        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        state.sessions.get_or_create = AsyncMock(
            side_effect=SpeculativeResumeRefused("dashboard:t1")
        )
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
            await _eager_spawn(state, slot)  # must not raise
        state.sessions.release.assert_not_called()
        state.sessions.remove.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_removes_session_when_slot_deleted_mid_handshake(self):
        """A slot deleted during the handshake must not leave an orphan
        session that a recreated slot with the same key would reuse with
        stale agent/cwd bindings."""
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        state.sessions.remove = AsyncMock()

        # Simulate deletion landing while get_or_create is in flight: after the
        # handshake completes, get_slot no longer returns this slot object.
        async def _create_then_delete(*a, **kw):
            state.get_slot = MagicMock(return_value=None)
            return (MagicMock(), True, False)

        state.sessions.get_or_create = AsyncMock(side_effect=_create_then_delete)
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
            await _eager_spawn(state, slot)
        key = state.sessions.get_or_create.await_args.args[0]
        state.sessions.remove.assert_awaited_once_with(key)
        # Semaphore still released before teardown.
        state.sessions.release.assert_called_once_with(key)

    @pytest.mark.asyncio
    async def test_removes_session_when_bindings_change_mid_handshake(self, tmp_path):
        """GPT BLOCKING — stale-workspace session. A switch handler (workspace,
        model, reasoning effort) firing mid-handshake resets a key that has
        nothing registered yet, so the reset no-ops; without the bindings
        snapshot the eager task would then register a session with the OLD cwd
        and the first real turn would run tools in the wrong workspace."""
        slot = _ChatSlot("t1")
        slot.project = str(tmp_path / "a")
        state = _mock_state(slot)
        state.sessions.remove = AsyncMock()

        # The workspace switch lands while get_or_create is in flight.
        async def _create_then_switch(*a, **kw):
            slot.project = str(tmp_path / "b")
            return (MagicMock(), True, False)

        state.sessions.get_or_create = AsyncMock(side_effect=_create_then_switch)
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
            await _eager_spawn(state, slot)
        key = state.sessions.get_or_create.await_args.args[0]
        state.sessions.remove.assert_awaited_once_with(key)
        # Semaphore still released before teardown.
        state.sessions.release.assert_called_once_with(key)

    @pytest.mark.asyncio
    async def test_unchanged_bindings_keep_the_session(self, tmp_path):
        """The bindings guard must not tear down the common case."""
        slot = _ChatSlot("t1")
        slot.project = str(tmp_path)
        state = _mock_state(slot)
        state.sessions.remove = AsyncMock()
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
            await _eager_spawn(state, slot)
        state.sessions.remove.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lost_race_never_removes_the_winning_session(self, tmp_path):
        """GPT BLOCKING — stale eager cleanup destroying the real turn's session.

        is_new=False from get_or_create means another creator won the same-key
        race: a real turn owns that runtime and may have unfinished background
        work attached. Even when a cleanup trigger fires (bindings changed
        mid-handshake here; slot-vanish is the same gate), the eager loser must
        leave the winner's session alone — removing it would terminate the
        winner mid-flight. The winner registered with its own current bindings,
        so no stale-bindings hazard exists on its session.
        """
        slot = _ChatSlot("t1")
        slot.project = str(tmp_path / "a")
        state = _mock_state(slot)
        state.sessions.remove = AsyncMock()

        # A real turn wins registration while our handshake runs (is_new=False),
        # AND a workspace switch lands — the pre-fix code removed the session.
        async def _lose_race_and_switch(*a, **kw):
            slot.project = str(tmp_path / "b")
            return (MagicMock(), False, False)

        state.sessions.get_or_create = AsyncMock(side_effect=_lose_race_and_switch)
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
            await _eager_spawn(state, slot)
        state.sessions.remove.assert_not_awaited()
        # Semaphore still released so the winner's next turn isn't blocked.
        key = state.sessions.get_or_create.await_args.args[0]
        state.sessions.release.assert_called_once_with(key)


class TestTtftMetric:
    """kirocrew.chat.first_token.duration — user message → first visible token."""

    def test_emits_histogram_with_attribution_attrs(self):
        rec = MagicMock()
        with patch("kiro_crew.metrics.provider.get_recorder", return_value=rec):
            chat_runner._emit_ttft_metric(0.0, "dashboard:chat-1-x", is_new=True, resumed=False)
        assert rec.histogram.call_count == 1
        name = rec.histogram.call_args.args[0]
        attrs = rec.histogram.call_args.kwargs["attrs"]
        assert name == "kirocrew.chat.first_token.duration"
        assert attrs["first_turn"] is True
        assert attrs["resumed"] is False
        assert rec.histogram.call_args.kwargs["unit"] == "ms"

    def test_recorder_failure_is_swallowed(self):
        """Best-effort: a metrics outage must never break the chat stream."""
        with patch("kiro_crew.metrics.provider.get_recorder", side_effect=RuntimeError("boom")):
            chat_runner._emit_ttft_metric(0.0, "dashboard:chat-1-x", is_new=False, resumed=True)


def _stub_factory():
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.context_usage_pct = lambda: 0.0
        m.is_alive.return_value = True
        m.is_process_alive = lambda: True
        return m

    return factory


class TestSpeculativeGetOrCreate:
    """SessionManager-level semantics of the speculative flag, on the real
    get_or_create paths (no mocks around the flag mechanics)."""

    @pytest.fixture
    def cfg(self):
        from kiro_crew.config.loader import KiroCrewConfig

        c = KiroCrewConfig()
        c.agent.provider = "acp"
        c.session.pool_size = 0
        return c

    @pytest.mark.asyncio
    async def test_speculative_create_leaves_first_turn_armed(self, cfg):
        """A speculative creator registers is_new=True; the next real
        get_or_create claims it (was_new=True) and consumes it. This is the
        end-to-end invariant that keeps first-turn context injection alive."""
        from kiro_crew.session import SessionManager

        mgr = SessionManager(cfg, provider_factory=_stub_factory())
        key = "dashboard:eager-x"
        _, is_new, resumed = await mgr.get_or_create(key, speculative=True)
        mgr.release(key)
        assert mgr._sessions[key].is_new is True  # still armed
        # Real first turn claims via the fast path and consumes.
        _, was_new, _ = await mgr.get_or_create(key)
        mgr.release(key)
        assert was_new is True
        assert mgr._sessions[key].is_new is False
        # A second real turn is not new.
        _, was_new2, _ = await mgr.get_or_create(key)
        mgr.release(key)
        assert was_new2 is False

    @pytest.mark.asyncio
    async def test_speculative_claim_does_not_consume(self, cfg):
        """A speculative call landing on an already-armed live session must
        read the flag without consuming it (repeat eager signals)."""
        from kiro_crew.session import SessionManager

        mgr = SessionManager(cfg, provider_factory=_stub_factory())
        key = "dashboard:eager-y"
        await mgr.get_or_create(key, speculative=True)
        mgr.release(key)
        await mgr.get_or_create(key, speculative=True)  # fast path, speculative
        mgr.release(key)
        assert mgr._sessions[key].is_new is True

    @pytest.mark.asyncio
    async def test_speculative_refuses_resumable_key(self, cfg, tmp_path, monkeypatch):
        """A key with a session-map entry raises instead of resuming, on the
        same map read that would drive the resume (no TOCTOU window).

        SessionMap.get self-prunes entries whose kiro transcript files are
        missing, so the mapping must be backed by a real .json + a >=10-byte
        .jsonl in the (patched, isolated) kiro sessions dir or the guard is
        never exercised.
        """
        from kiro_crew.session import SessionManager, SpeculativeResumeRefused

        sessions_dir = tmp_path / "kiro-sessions"
        sessions_dir.mkdir()
        monkeypatch.setattr("kiro_crew.session_map._kiro_sessions_dir", lambda: sessions_dir)
        sid = "prior-sid-1234"
        (sessions_dir / f"{sid}.json").write_text("{}")
        (sessions_dir / f"{sid}.jsonl").write_text("x" * 32)

        mgr = SessionManager(cfg, provider_factory=_stub_factory())
        key = "dashboard:eager-z"
        mgr._session_map.set(key, sid)
        with pytest.raises(SpeculativeResumeRefused):
            await mgr.get_or_create(key, speculative=True)
        assert key not in mgr._sessions

    @pytest.mark.asyncio
    async def test_real_turn_losing_race_to_speculative_winner_gets_the_flag(self, cfg):
        """Verifier race: eager and the first real turn cold-start
        concurrently and the eager call wins registration. The loser takes
        the won-race path and must receive was_new=True (the armed flag),
        not a hardcoded False."""
        import asyncio

        from kiro_crew.session import SessionManager

        mgr = SessionManager(cfg, provider_factory=_stub_factory())
        key = "dashboard:eager-race"

        results: dict[str, tuple] = {}

        async def eager():
            results["eager"] = await mgr.get_or_create(key, speculative=True)
            mgr.release(key)

        async def real():
            results["real"] = await mgr.get_or_create(key)
            mgr.release(key)

        await asyncio.gather(eager(), real())
        # Exactly one registration; whoever lost the race went through the
        # won-race (or fast) path. The REAL caller must end with the flag.
        _, real_is_new, _ = results["real"]
        assert real_is_new is True
        assert mgr._sessions[key].is_new is False  # consumed by the real turn

    @pytest.mark.asyncio
    async def test_cancelled_waiting_claimant_does_not_destroy_the_flag(self, cfg):
        """Verifier race: a real claimant that is CANCELLED while waiting on
        the session semaphore must not consume the first-turn flag. Ownership
        of the flag follows semaphore acquisition, so the next real claimant
        still receives was_new=True."""
        import asyncio

        from kiro_crew.session import SessionManager

        mgr = SessionManager(cfg, provider_factory=_stub_factory())
        key = "dashboard:eager-cancel"
        await mgr.get_or_create(key, speculative=True)
        # Speculative creator still holds the semaphore (release() not called
        # yet) — an arriving real claimant will block on acquire.
        waiter = asyncio.ensure_future(mgr.get_or_create(key))
        for _ in range(50):
            if mgr._sessions[key].semaphore.locked():
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.02)  # let the waiter park inside acquire()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        mgr.release(key)  # eager creator finishes
        # The cancelled waiter must not have consumed the flag.
        assert mgr._sessions[key].is_new is True
        _, was_new, _ = await mgr.get_or_create(key)
        mgr.release(key)
        assert was_new is True


class TestProjectSetWiring:
    @pytest.mark.asyncio
    async def test_agent_switch_schedules_eager_spawn(self):
        """The agent-switch reset destroys any eager session; the handler must
        re-arm the spawn for the new bindings (Design Review coverage gap)."""
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.chat import api_chat_slot_agent

        slot = _ChatSlot("t1")
        state = MagicMock(spec=DashboardState)
        state._slots = {slot.key: slot}
        state.push_slots_update = MagicMock()
        state.conversation_log = None  # instance attr; spec= does not provide it
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/agent", api_chat_slot_agent)
        with (
            patch(
                "kiro_crew.dashboard.chat_handlers._reset_slot_session",
                new=AsyncMock(),
            ),
            patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", new=AsyncMock()),
            patch("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn") as sched,
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/chat/slots/t1/agent", json={"agent": "kirocrew"})
                assert resp.status == 200
            sched.assert_called_once_with(state, slot)

    @pytest.mark.asyncio
    async def test_project_change_schedules_eager_spawn(self, tmp_path):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.chat import api_chat_slot_project

        slot = _ChatSlot("t1")
        state = MagicMock(spec=DashboardState)
        state._slots = {slot.key: slot}
        state.push_slots_update = MagicMock()
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
        with (
            patch("kiro_crew.dashboard.chat_handlers._save_recent_project"),
            patch("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn") as sched,
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/chat/slots/t1/project", json={"project": str(tmp_path)}
                )
                assert resp.status == 200
            sched.assert_called_once_with(state, slot)

    @pytest.mark.asyncio
    async def test_noop_project_set_does_not_schedule(self, tmp_path):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.chat import api_chat_slot_project

        slot = _ChatSlot("t1")
        slot.project = str(tmp_path)
        state = MagicMock(spec=DashboardState)
        state._slots = {slot.key: slot}
        state.push_slots_update = MagicMock()
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
        with (
            patch("kiro_crew.dashboard.chat_handlers._save_recent_project"),
            patch("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn") as sched,
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/chat/slots/t1/project", json={"project": str(tmp_path)}
                )
                assert resp.status == 200
            sched.assert_not_called()


class TestSpeculativeResumeHandover:
    """Resume prefetch: the speculative_resume opt-in and the one-shot
    resumed_armed handover, on the real get_or_create paths."""

    @pytest.fixture
    def cfg(self):
        from kiro_crew.config.loader import KiroCrewConfig

        c = KiroCrewConfig()
        c.agent.provider = "acp"
        c.session.pool_size = 0
        return c

    def _resumable(self, mgr, key, tmp_path, monkeypatch, sid="prior-sid-1234"):
        """Back a session-map entry with real transcript files so
        SessionMap.get does not self-prune it (mirrors the refusal test)."""
        sessions_dir = tmp_path / "kiro-sessions"
        sessions_dir.mkdir(exist_ok=True)
        monkeypatch.setattr("kiro_crew.session_map._kiro_sessions_dir", lambda: sessions_dir)
        (sessions_dir / f"{sid}.json").write_text("{}")
        (sessions_dir / f"{sid}.jsonl").write_text("x" * 32)
        mgr._session_map.set(key, sid)
        return sid

    @pytest.mark.asyncio
    async def test_opt_in_does_not_refuse_resumable_key(self, cfg, tmp_path, monkeypatch):
        """speculative_resume=True lifts the ENTRY refusal: the speculative
        creator performs the load and, when it actually resumes, registers
        with the first-turn flag armed. (A load that does NOT resume is
        rejected pre-registration — pinned by TestSpecResumeFallbackMapGuard.)"""
        from kiro_crew.session import SessionManager

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.shutdown = AsyncMock()
            m.context_usage_pct = lambda: 0.0
            m.is_alive.return_value = True
            m.is_process_alive = lambda: True
            m.cwd = "/tmp"
            m.client = MagicMock()
            m.client.resumed = True  # the load restored the transcript
            m.client._session_id = "prior-sid-a"
            return m

        monkeypatch.setattr("kiro_crew.providers.acp.AcpProvider", object)
        mgr = SessionManager(cfg, provider_factory=factory)
        key = "dashboard:prefetch-a"
        self._resumable(mgr, key, tmp_path, monkeypatch)
        _, is_new, _ = await mgr.get_or_create(key, speculative=True, speculative_resume=True)
        mgr.release(key)
        assert is_new is True
        assert mgr._sessions[key].is_new is True  # still armed for the real turn

    @pytest.mark.asyncio
    async def test_resumed_observation_armed_and_consumed_by_real_claimant(
        self, cfg, tmp_path, monkeypatch
    ):
        """The load-observed resumed=True is armed at registration and handed
        to the first real claimant exactly once — the invariant that keeps the
        real first turn's history-injection decision correct."""
        from kiro_crew.session import SessionManager

        sid = "prior-sid-9999"

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.shutdown = AsyncMock()
            m.context_usage_pct = lambda: 0.0
            m.is_alive.return_value = True
            m.is_process_alive = lambda: True
            m.cwd = "/tmp"
            m.client = MagicMock()
            m.client.resumed = True  # the session/load restored the transcript
            m.client._session_id = sid
            return m

        # get_or_create gates the resumed sample on isinstance(provider,
        # AcpProvider); widen the class so the stub passes without spawning a
        # real ACP process.
        monkeypatch.setattr("kiro_crew.providers.acp.AcpProvider", object)

        from kiro_crew.session import SessionManager  # noqa: F811

        mgr = SessionManager(cfg, provider_factory=factory)
        key = "dashboard:prefetch-b"
        self._resumable(mgr, key, tmp_path, monkeypatch, sid=sid)

        _, is_new, resumed = await mgr.get_or_create(key, speculative=True, speculative_resume=True)
        mgr.release(key)
        assert (is_new, resumed) == (True, True)
        sess = mgr._sessions[key]
        assert sess.is_new is True and sess.resumed_armed is True

        # Real first turn: receives BOTH observations and consumes them.
        _, was_new, was_resumed = await mgr.get_or_create(key)
        mgr.release(key)
        assert (was_new, was_resumed) == (True, True)
        assert sess.is_new is False and sess.resumed_armed is False

        # Second real turn: nothing armed.
        _, was_new2, was_resumed2 = await mgr.get_or_create(key)
        mgr.release(key)
        assert (was_new2, was_resumed2) == (False, False)

    @pytest.mark.asyncio
    async def test_speculative_claimant_reads_resumed_without_consuming(self, cfg):
        """A repeat speculative call on an armed session must not consume
        either marker (repeat focus signals)."""
        from kiro_crew.session import SessionManager

        mgr = SessionManager(cfg, provider_factory=_stub_factory())
        key = "dashboard:prefetch-c"
        await mgr.get_or_create(key, speculative=True)
        mgr.release(key)
        mgr._sessions[key].resumed_armed = True  # as a resume prefetch would set
        _, is_new, resumed = await mgr.get_or_create(key, speculative=True)
        mgr.release(key)
        assert (is_new, resumed) == (True, True)
        assert mgr._sessions[key].is_new is True
        assert mgr._sessions[key].resumed_armed is True

    @pytest.mark.asyncio
    async def test_fresh_speculative_create_does_not_arm_resumed(self, cfg):
        """No mapping → the speculative creator starts fresh and must not
        claim a resume it never performed."""
        from kiro_crew.session import SessionManager

        mgr = SessionManager(cfg, provider_factory=_stub_factory())
        key = "dashboard:prefetch-d"
        await mgr.get_or_create(key, speculative=True)
        mgr.release(key)
        assert mgr._sessions[key].resumed_armed is False


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
    def _no_debounce(self, monkeypatch):
        monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)

    @pytest.mark.asyncio
    async def test_allow_resume_passes_speculative_resume(self):
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
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
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
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
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
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


class TestArmedPrefetchCap:
    """_cap_armed_prefetches: population cap on live-but-unclaimed prefetches.

    Design Review concern on 64a5c5f89: the spawn semaphore bounds concurrent
    spawns, not accumulated live processes — after a restart restores many
    resumable tabs, flipping through them could stack one kiro-cli process per
    tab for the whole TTL. Arming beyond the cap evicts the OLDEST unclaimed
    prefetch via the conditional remove_if_unclaimed.
    """

    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        chat_runner._armed_prefetches.clear()
        yield
        chat_runner._armed_prefetches.clear()

    @pytest.mark.asyncio
    async def test_under_cap_evicts_nothing(self):
        sessions = MagicMock()
        sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        for i in range(chat_runner._RESUME_PREFETCH_MAX_LIVE):
            await chat_runner._cap_armed_prefetches(sessions, f"dashboard:k{i}")
        sessions.remove_if_unclaimed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_over_cap_evicts_the_oldest_unclaimed(self):
        sessions = MagicMock()
        sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        for i in range(chat_runner._RESUME_PREFETCH_MAX_LIVE + 1):
            await chat_runner._cap_armed_prefetches(sessions, f"dashboard:k{i}")
        sessions.remove_if_unclaimed.assert_awaited_once_with("dashboard:k0")
        assert "dashboard:k0" not in chat_runner._armed_prefetches
        assert len(chat_runner._armed_prefetches) == chat_runner._RESUME_PREFETCH_MAX_LIVE

    @pytest.mark.asyncio
    async def test_rearming_a_key_moves_it_to_newest(self):
        """Re-focusing a slot must not leave its key at the eviction front."""
        sessions = MagicMock()
        sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        for i in range(chat_runner._RESUME_PREFETCH_MAX_LIVE):
            await chat_runner._cap_armed_prefetches(sessions, f"dashboard:k{i}")
        await chat_runner._cap_armed_prefetches(sessions, "dashboard:k0")  # re-arm
        await chat_runner._cap_armed_prefetches(sessions, "dashboard:new")
        # k1 is now the oldest, not the re-armed k0.
        sessions.remove_if_unclaimed.assert_awaited_once_with("dashboard:k1")

    @pytest.mark.asyncio
    async def test_claimed_session_just_leaves_the_accounting(self):
        """remove_if_unclaimed returning False (claimed/gone) drops the entry
        without error — a claimed session is never touched."""
        sessions = MagicMock()
        sessions.remove_if_unclaimed = AsyncMock(return_value=False)
        for i in range(chat_runner._RESUME_PREFETCH_MAX_LIVE + 1):
            await chat_runner._cap_armed_prefetches(sessions, f"dashboard:k{i}")
        sessions.remove_if_unclaimed.assert_awaited_once_with("dashboard:k0")
        assert len(chat_runner._armed_prefetches) == chat_runner._RESUME_PREFETCH_MAX_LIVE


class TestResumableHint:
    """SessionMap.has_hint: the loop-safe membership probe."""

    @pytest.fixture
    def smap(self, tmp_path, monkeypatch):
        from kiro_crew.session_map import SessionMap

        sessions_dir = tmp_path / "kiro-sessions"
        sessions_dir.mkdir()
        monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.session_map._kiro_sessions_dir", lambda: sessions_dir)
        return SessionMap(), sessions_dir

    def test_hint_true_for_entry_even_without_files(self, smap):
        """The hint is membership only — a stale entry (files gone) still
        hints True. Callers tolerate the false positive: the pruning get()
        inside the resume path is the authority."""
        m, _ = smap
        m.set("dashboard:a", "sid-a")
        assert m.has_hint("dashboard:a") is True

    def test_hint_false_for_absent_key(self, smap):
        m, _ = smap
        assert m.has_hint("dashboard:missing") is False

    def test_hint_never_mutates_the_map(self, smap):
        """Unlike get(), has_hint must not prune or save — it runs on the
        event loop, and SessionMap is unlocked and loop-owned."""
        m, _ = smap
        m.set("dashboard:stale", "gone-sid")  # no session files exist
        assert m.has_hint("dashboard:stale") is True
        assert m.has_hint("dashboard:stale") is True  # still there — no prune
        assert m.get("dashboard:stale") is None  # get() DOES prune it
        assert m.has_hint("dashboard:stale") is False


class TestSlotFocusedFrame:
    """ws._handle_slot_focused: the slot-focused intent signal."""

    def _state(self, slot, *, has_session=False, resumable="prior-sid"):
        state = MagicMock(spec=DashboardState)
        state.get_slot = MagicMock(return_value=slot)
        state.sessions = MagicMock()
        state.sessions.has_session = MagicMock(return_value=has_session)
        state.sessions.resumable_hint = MagicMock(return_value=bool(resumable))
        return state

    @pytest.mark.asyncio
    async def test_resumable_focus_schedules_resume_prefetch(self):
        from kiro_crew.dashboard.ws import _handle_slot_focused

        slot = _ChatSlot("t1")
        state = self._state(slot)
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
            task = _handle_slot_focused(state, "t1", None, owner=True)
        assert task is not None
        assert slot._eager_spawn_task is task
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_focus_change_cancels_previous_prefetch(self):
        from kiro_crew.dashboard.ws import _handle_slot_focused

        slot = _ChatSlot("t1")
        state = self._state(slot)
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
            first = _handle_slot_focused(state, "t1", None, owner=True)
            second = _handle_slot_focused(state, "t1", first, owner=True)
        assert first is not None and second is not None
        with pytest.raises(asyncio.CancelledError):
            await first
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second

    @pytest.mark.asyncio
    async def test_blur_cancels_and_schedules_nothing(self):
        from kiro_crew.dashboard.ws import _handle_slot_focused

        slot = _ChatSlot("t1")
        state = self._state(slot)
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
            pending = _handle_slot_focused(state, "t1", None, owner=True)
            result = _handle_slot_focused(state, None, pending, owner=True)
        assert result is None
        assert pending is not None
        with pytest.raises(asyncio.CancelledError):
            await pending

    @pytest.mark.asyncio
    async def test_live_session_schedules_nothing(self):
        from kiro_crew.dashboard.ws import _handle_slot_focused

        slot = _ChatSlot("t1")
        state = self._state(slot, has_session=True)
        assert _handle_slot_focused(state, "t1", None, owner=True) is None

    @pytest.mark.asyncio
    async def test_non_resumable_slot_spawns_nothing(self, monkeypatch):
        """A non-resumable key never creates a session from the focus path.
        The probe is the loop-safe in-memory HINT (no disk, no pruning — the
        pruning ``resumable_sid`` lookup must not run off-loop against the
        unlocked, loop-owned SessionMap); the spawn task re-checks it after
        the debounce, and fresh eager spawn stays owned by the
        create/project/agent signals."""
        monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        state.sessions.resumable_hint = MagicMock(return_value=False)
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
            await chat_runner._eager_spawn(state, slot, allow_resume=True)
        state.sessions.get_or_create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_resumable_focus_preserves_pending_fresh_spawn(self):
        """Creating a slot focuses it, so the focus frame lands right behind
        the create signal. schedule_eager_spawn keeps ONE task per slot —
        routing a non-resumable focus through it would cancel the
        create-armed FRESH spawn and then no-op, silently gutting fresh eager
        spawn for every new slot. The handler must check the hint itself and
        leave the pending task untouched."""
        from kiro_crew.dashboard.ws import _handle_slot_focused

        slot = _ChatSlot("t1")
        state = self._state(slot, resumable=None)
        fresh = asyncio.get_running_loop().create_future()
        pending = asyncio.ensure_future(fresh)
        slot._eager_spawn_task = pending
        try:
            result = _handle_slot_focused(state, "t1", None, owner=True)
            assert result is None
            assert slot._eager_spawn_task is pending
            assert not pending.cancelled()
        finally:
            fresh.set_result(None)
            await pending

    @pytest.mark.asyncio
    async def test_running_turn_schedules_nothing(self):
        from kiro_crew.dashboard.ws import _handle_slot_focused

        slot = _ChatSlot("t1")
        # slot.running derives from slot.task being a live task.
        _turn = asyncio.get_running_loop().create_future()
        _task = asyncio.ensure_future(_turn)
        slot.task = _task
        state = self._state(slot)
        try:
            assert _handle_slot_focused(state, "t1", None, owner=True) is None
        finally:
            _turn.set_result(None)
            await _task

    @pytest.mark.asyncio
    async def test_non_owner_socket_schedules_nothing_and_cancels_nothing(self):
        """An app-scoped socket must not start owner-session processes or
        cancel another arm's prefetch — the frame is ignored entirely."""
        from kiro_crew.dashboard.ws import _handle_slot_focused

        slot = _ChatSlot("t1")
        state = self._state(slot)
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
            pending = _handle_slot_focused(state, "t1", None, owner=True)
            result = _handle_slot_focused(state, "t1", pending, owner=False)
        assert result is pending  # passed through untouched
        assert pending is not None and not pending.cancelled()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

    @pytest.mark.asyncio
    async def test_failed_speculative_load_leaves_nothing_behind(self):
        """A speculative resume whose load fell back is rejected BEFORE
        registration (SpeculativeResumeRefused): no claimable fallback session
        ever exists, so there is nothing to remove, no TTL to arm, and no
        semaphore to release — the first real message creates and maps the
        fallback itself with the normal F2 recovery."""
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        state.sessions.get_or_create = AsyncMock(
            side_effect=chat_runner.SpeculativeResumeRefused("dashboard:t1")
        )
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
            await chat_runner._eager_spawn(state, slot, allow_resume=True)
        state.sessions.remove_if_unclaimed.assert_not_awaited()
        state.sessions.remove.assert_not_awaited()
        state.sessions.release.assert_not_called()
        assert getattr(slot, "_prefetch_ttl_task", None) is None


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
        """GPT round-2 blocker: the switch branch mutates ``resume_sid`` to
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
