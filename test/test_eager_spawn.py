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
from eager_spawn_helpers import (  # noqa: F401 -- fixtures are used by name
    _bindings,
    _cfg,
    _isolate_armed_registry,
    _mock_state,
    _pin_prewarm_allowance,
    _prime,
    _stub_factory,
)

from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    MemoryStoreConfig,
    ResolvedBindings,
)
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import _eager_spawn, schedule_eager_spawn
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.session import FirstTurnState
from kiro_crew.start_priority import StartPriority
from kiro_crew.testing.wait import until_parked


def _cfg_with_store(name: str, *, version: int, owner: str = "") -> KiroCrewConfig:
    cfg = KiroCrewConfig(
        agents={"default": KiroCrewAgentConfig()},
        memory_stores={
            "default": MemoryStoreConfig(),
            name: MemoryStoreConfig(memory_version=version, owner_member=owner),
        },
    )
    cfg.session.eager_spawn = True
    return cfg


def _private_alice_cfg() -> KiroCrewConfig:
    return _cfg_with_store("member-alice", version=2, owner="alice")


def _private_default_member_cfg() -> KiroCrewConfig:
    cfg = _private_alice_cfg()
    cfg.agents["alice"] = KiroCrewAgentConfig(memory_store="member-alice")
    cfg.default_agent = "alice"
    return cfg


def _alice_bindings() -> ResolvedBindings:
    return _bindings(agent="alice-agent", alias="alice", memory_store="member-alice")


def _unresolved_bindings() -> ResolvedBindings:
    bindings = _bindings()
    bindings.requested_resolved = False
    return bindings


def _unresolved_member_cfg() -> KiroCrewConfig:
    return _cfg(True).return_value


# id -> (slot agent, restored slot store, config factory, resolver-result factory):
# every row must stand the eager path down without touching the provider.
_NO_SPECULATION_CASES = {
    "private-v2-fresh": ("alice", "", _private_alice_cfg, _alice_bindings),
    "private-v2-restored": ("alice", "member-alice", _private_alice_cfg, _alice_bindings),
    "empty-slot-private-default": ("", "", _private_default_member_cfg, _alice_bindings),
    "restored-store-mismatch": ("missing-member", "member-alice", _private_alice_cfg, _bindings),
    "unresolved-member": ("missing-member", "", _unresolved_member_cfg, _unresolved_bindings),
}


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
        """The snapshot carries OFF and the disk copy is pinned to ON.

        A gate reading the wrong source would arm a task here, so the opposing
        values are what make this assertion discriminate.
        """
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        _prime(False)
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
            schedule_eager_spawn(state, slot)
        assert slot._eager_spawn_task is None

    @pytest.mark.asyncio
    async def test_newer_signal_cancels_older_task(self):
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        _prime(True)
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
        bindings = _bindings(agent="wfe-oncall", alias="wfe-oncall")
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)),
            patch.object(chat_runner, "resolve_agent_bindings", return_value=bindings),
        ):
            await _eager_spawn(state, slot)
        state.sessions.get_or_create.assert_awaited_once()
        kwargs = state.sessions.get_or_create.await_args.kwargs
        assert kwargs["agent"] == "wfe-oncall"
        assert kwargs["crew_agent"] == "wfe-oncall"
        assert kwargs["cwd"] == str(tmp_path)
        # The per-session semaphore acquired by get_or_create MUST be released
        # here: no turn follows, and a held semaphore would deadlock the first
        # real message.
        key = state.sessions.get_or_create.await_args.args[0]
        state.sessions.release.assert_called_once_with(key)
        assert (
            await asyncio.to_thread(chat_runner.session_agent_selection_kind, key, slot.agent)
            == "member"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("allow_resume", [False, True])
    @pytest.mark.parametrize(
        ("agent", "restored_store", "make_cfg", "make_bindings"),
        list(_NO_SPECULATION_CASES.values()),
        ids=list(_NO_SPECULATION_CASES),
    )
    async def test_private_or_unresolved_member_leaves_provider_allocation_to_first_turn(
        self, agent, restored_store, make_cfg, make_bindings, allow_resume
    ):
        """Neither a fresh nor a resume prefetch may pre-register an ordinary
        provider when the real turn pins a protected V2 store (fresh, restored,
        or whatever the resolver answers for an empty slot), when a restored
        store disagrees with today's resolver, or when an explicit member is
        unavailable. Classification follows resolved bindings, never the
        resolver's Global fallback. An empty slot is resolved as itself -- the
        default TEMPLATE, never the default crewmate alias."""
        slot = _ChatSlot("t1")
        slot.agent = agent
        slot.memory_store = restored_store
        state = _mock_state(slot)
        cfg = make_cfg()
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", return_value=cfg),
            patch.object(
                chat_runner, "resolve_agent_bindings", return_value=make_bindings()
            ) as resolve,
        ):
            await _eager_spawn(state, slot, allow_resume=allow_resume)
        resolve.assert_called_once_with(cfg, agent, validate_memory_files=False)
        state.sessions.get_or_create.assert_not_awaited()
        state.sessions.release.assert_not_called()
        state.sessions.remove.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_valid_named_v1_keeps_speculative_spawn(self):
        """The private guard is version-specific; legacy named V1 keeps its
        existing startup behavior and does not inherit V2's first-turn delay."""
        slot = _ChatSlot("t1")
        slot.agent = "legacy"
        slot.memory_store = "legacy-v1"
        state = _mock_state(slot)
        cfg = _cfg_with_store("legacy-v1", version=1)
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", return_value=cfg),
            patch.object(
                chat_runner,
                "resolve_agent_bindings",
                return_value=_bindings(
                    agent="legacy-agent", alias="legacy", memory_store="legacy-v1"
                ),
            ),
        ):
            await _eager_spawn(state, slot)
        state.sessions.get_or_create.assert_awaited_once()
        key = state.sessions.get_or_create.await_args.args[0]
        state.sessions.release.assert_called_once_with(key)

    @pytest.mark.asyncio
    async def test_private_assignment_blocks_legacy_speculation_without_slot_metadata(self):
        from kiro_crew.member_memory_auth import bind_private_session_store
        from kiro_crew.memory_stores import provision_member_memory

        slot = _ChatSlot("private-history")
        slot.agent = "legacy"
        state = _mock_state(slot)
        key = chat_runner.effective_session_key(slot)

        def seed():
            cfg = KiroCrewConfig.load()
            cfg.session.eager_spawn = True
            cfg.agents["writer"] = KiroCrewAgentConfig()
            cfg.agents["legacy"] = KiroCrewAgentConfig()
            store = provision_member_memory(cfg, "writer")
            cfg.save()
            bind_private_session_store(key, store)

        await asyncio.to_thread(seed)
        await _eager_spawn(state, slot)

        state.sessions.get_or_create.assert_not_awaited()
        assert slot.memory_store == ""

    @pytest.mark.asyncio
    async def test_store_change_during_selection_record_stands_down_before_allocation(self):
        """Store identity is part of the pre-allocation snapshot. An agent
        switch landing during the awaited agent-selection write must win without
        creating a provider from the old binding."""
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        real_to_thread = asyncio.to_thread
        model_lookups = []

        async def _switch_store(_func, *_args, **_kwargs):
            if _func is not chat_runner.record_agent_selection:
                return await real_to_thread(_func, *_args, **_kwargs)
            model_lookups.append(_func)
            slot.memory_store = "member-new"
            return None

        with (
            patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)),
            patch.object(chat_runner.asyncio, "to_thread", side_effect=_switch_store),
        ):
            await _eager_spawn(state, slot)
        assert len(model_lookups) == 1
        state.sessions.get_or_create.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("change", ["none", "store", "replacement", "turn"])
    async def test_binding_lookup_is_off_loop_and_rechecks_before_eager_allocation(self, change):
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        loop = asyncio.get_running_loop()
        original = chat_runner.resolve_agent_bindings
        calls = []

        def resolve(cfg, agent, **kwargs):
            with pytest.raises(RuntimeError, match="no running event loop"):
                asyncio.get_running_loop()
            calls.append(agent)
            result = original(cfg, agent, **kwargs)
            if change == "store":
                loop.call_soon_threadsafe(setattr, slot, "memory_store", "member-new")
            elif change == "replacement":
                loop.call_soon_threadsafe(setattr, state.get_slot, "return_value", _ChatSlot("t1"))
            elif change == "turn":
                loop.call_soon_threadsafe(
                    setattr, slot, "task", MagicMock(done=MagicMock(return_value=False))
                )
            return result

        with (
            patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)),
            patch.object(chat_runner, "resolve_agent_bindings", side_effect=resolve),
        ):
            await _eager_spawn(state, slot)
        assert calls == [""]
        if change == "none":
            state.sessions.get_or_create.assert_awaited_once()
            state.sessions.release.assert_called_once()
        else:
            state.sessions.get_or_create.assert_not_awaited()
            state.sessions.remove.assert_not_awaited()

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
        state.sessions.reset.assert_awaited_once_with("dashboard:t1", skip_if_busy=True)
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
        # handshake completes, get_slot does not return this slot object.
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
        assert mgr._sessions[key].first_turn is FirstTurnState.FRESH  # still armed
        # Real first turn claims via the fast path and consumes.
        _, was_new, _ = await mgr.get_or_create(key)
        mgr.release(key)
        assert was_new is True
        assert mgr._sessions[key].first_turn is FirstTurnState.NOTHING_ARMED
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
        assert mgr._sessions[key].first_turn is FirstTurnState.FRESH

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
        assert (
            mgr._sessions[key].first_turn is FirstTurnState.NOTHING_ARMED
        )  # consumed by the real turn

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
        await until_parked(mgr._sessions[key].semaphore)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        mgr.release(key)  # eager creator finishes
        # The cancelled waiter must not have consumed the observation.
        assert mgr._sessions[key].first_turn is FirstTurnState.FRESH
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
        # The switch handler re-probes the live session in a no-await window
        # before the teardown (state.sessions.get_provider); sessions is an
        # instance attr spec= does not synthesize. None = no live session, so
        # the re-probe passes and the committed switch proceeds.
        state.sessions = MagicMock()
        state.sessions.get_provider = MagicMock(return_value=None)
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/agent", api_chat_slot_agent)
        with (
            patch(
                "kiro_crew.dashboard.chat_handlers._reset_slot_session_or_warn",
                new=AsyncMock(return_value=True),
            ),
            patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", new=AsyncMock()),
            patch("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn") as sched,
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/chat/slots/t1/agent", json={"agent": "kirocrew"})
                assert resp.status == 200
            # No owner identity on this request, so BACKGROUND (test_start_priority
            # pins the owner case).
            sched.assert_called_once_with(state, slot, start_priority=StartPriority.BACKGROUND)

    @pytest.mark.asyncio
    async def test_project_change_schedules_eager_spawn(self, tmp_path):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.chat import api_chat_slot_project

        slot = _ChatSlot("t1")
        state = MagicMock(spec=DashboardState)
        state._slots = {slot.key: slot}
        state.push_slots_update = MagicMock()
        # The arm guard probes the live session before arming the
        # deferred reset; sessions is an instance attr spec= does not
        # synthesize. A live provider makes the arm condition hold, so this
        # test still exercises the eager-spawn wiring it is about.
        state.sessions = MagicMock()
        state.sessions.get_provider = MagicMock(return_value=object())
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
            # No owner identity on this request, so BACKGROUND (test_start_priority
            # pins the owner case).
            sched.assert_called_once_with(state, slot, start_priority=StartPriority.BACKGROUND)

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
    RESUMED first-turn handover, on the real get_or_create paths."""

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
        # Still armed for the real turn — and armed as RESUMED, since the
        # load restored the transcript.
        assert mgr._sessions[key].first_turn is FirstTurnState.RESUMED

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
        assert sess.first_turn is FirstTurnState.RESUMED

        # Real first turn: receives BOTH observations and consumes them.
        _, was_new, was_resumed = await mgr.get_or_create(key)
        mgr.release(key)
        assert (was_new, was_resumed) == (True, True)
        assert sess.first_turn is FirstTurnState.NOTHING_ARMED

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
        mgr._sessions[key].first_turn = FirstTurnState.RESUMED  # as a resume prefetch would set
        _, is_new, resumed = await mgr.get_or_create(key, speculative=True)
        mgr.release(key)
        assert (is_new, resumed) == (True, True)
        assert mgr._sessions[key].first_turn is FirstTurnState.RESUMED

    @pytest.mark.asyncio
    async def test_fresh_speculative_create_does_not_arm_resumed(self, cfg):
        """No mapping → the speculative creator starts fresh and must not
        claim a resume it never performed."""
        from kiro_crew.session import SessionManager

        mgr = SessionManager(cfg, provider_factory=_stub_factory())
        key = "dashboard:prefetch-d"
        await mgr.get_or_create(key, speculative=True)
        mgr.release(key)
        # Armed for the real turn, but as FRESH — not a resume it never did.
        assert mgr._sessions[key].first_turn is FirstTurnState.FRESH


class TestIllegalFirstTurnStateUnrepresentable:
    """The refactor's justification: the old ``is_new``/``resumed_armed``
    boolean pair's fourth combination — a resume marker armed on an
    already-claimed session (``is_new=False, resumed_armed=True``), which
    would silently skip history injection — must have no spelling in the
    single-field shape."""

    def test_no_state_derives_to_claimed_but_resumed(self) -> None:
        """Every representable state satisfies the invariant the boolean pair
        enforced by convention only: resumed implies armed (is_new)."""
        for state in FirstTurnState:
            assert not (state.resumed and not state.is_new), (
                f"{state!r} derives to (is_new=False, resumed=True) — the "
                "illegal combination the enum exists to make unrepresentable"
            )

    def test_the_boolean_pair_has_no_spelling_left(self) -> None:
        """The old fields are gone from ``_Session``: with one field there is
        no second marker to desynchronize, and the old constructor kwargs are
        rejected rather than silently accepted."""
        from kiro_crew.session import _Session

        sess = _Session(provider=AsyncMock())
        assert not hasattr(sess, "is_new")
        assert not hasattr(sess, "resumed_armed")
        with pytest.raises(TypeError):
            _Session(provider=AsyncMock(), is_new=False, resumed_armed=True)  # type: ignore[call-arg]
