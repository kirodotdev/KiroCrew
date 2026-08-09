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
