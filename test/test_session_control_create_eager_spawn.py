"""``session_create`` starts the child's session ahead of its first message.

Without an eager spawn, a worker made by ``session_create`` starts its agent
when the caller's first ``session_send`` lands, so the whole handshake (spawn,
boot, ``session/new``, managed MCP wait) sits on the send path. The sidebar's
own create, agent switch and project switch arm ``schedule_eager_spawn``, and
``session_create`` arms it the same way.

Invariants:

* a committed create arms exactly one eager spawn, on the new child, after the
  child carries the agent, model and effort the call asked for and its birth
  record has landed -- so the spawn bakes in those, not defaults;
* it is armed at BACKGROUND priority: an agent asked for it, no person waits;
* a dry run or a refused create arms nothing;
* end to end, the spawn allocates with the child's model and effort, and stands
  down when a turn (the seed) or a close beats it.
"""

from __future__ import annotations

import asyncio
import dataclasses
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state
from eager_spawn_helpers import (  # noqa: F401 -- autouse fixtures register by import
    _cfg,
    _isolate_armed_registry,
    _pin_prewarm_allowance,
    _prime,
)
from session_control_helpers import (  # noqa: F401 -- autouse fixtures register by import
    _agent_resolves,
    _busy,
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _slot,
)

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.start_priority import StartPriority

_MODEL = "claude-sonnet-4.6"
_EFFORT = "low"


def _world(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _agent_resolves(monkeypatch, caller.workspace)
    return state, caller


def _record_arms(monkeypatch, state):
    """Replace the scheduler with a recorder of what the child looked like then."""
    arms: list[dict] = []

    def _arm(st, slot, **kwargs):
        arms.append(
            {
                "state": st,
                "slot": slot,
                "kwargs": kwargs,
                "agent": slot.agent,
                "model": slot.model,
                "effort": slot.reasoning_effort,
                "persisted": bool(state.conversation_log.get_metadata(slot_history_key(slot))),
            }
        )

    monkeypatch.setattr(chat_runner, "schedule_eager_spawn", _arm)
    return arms


def _create(state, caller, **kwargs):
    return asyncio.run(sc.create_session(state, caller_session_key=_key(caller), **kwargs))


def test_a_created_session_arms_one_background_eager_spawn_with_its_bindings(tmp_path, monkeypatch):
    """Mutation guard: drop the call and nothing is armed; move it above the
    model/effort pins or the birth write and the recorded snapshot is wrong."""
    state, caller = _world(tmp_path, monkeypatch)
    arms = _record_arms(monkeypatch, state)

    created = _create(state, caller, agent="kirocrew", model=_MODEL, reasoning_effort=_EFFORT)

    child = state.get_slot(created["target"])
    assert len(arms) == 1
    arm = arms[0]
    assert arm["state"] is state
    assert arm["slot"] is child
    assert arm["agent"] == "kirocrew"
    assert arm["model"] == _MODEL
    assert arm["effort"] == _EFFORT
    assert arm["persisted"], "armed before the birth record landed"
    # BACKGROUND is the scheduler's default; naming any priority here would
    # claim a person is waiting on an agent's dispatch.
    assert "start_priority" not in arm["kwargs"]
    assert "allow_resume" not in arm["kwargs"]


def test_a_create_with_no_pins_still_arms(tmp_path, monkeypatch):
    """Nearby input: no agent, model or effort -- the default bindings still warm."""
    state, caller = _world(tmp_path, monkeypatch)
    arms = _record_arms(monkeypatch, state)

    created = _create(state, caller)

    assert [a["slot"] for a in arms] == [state.get_slot(created["target"])]


def test_a_dry_run_arms_nothing(tmp_path, monkeypatch):
    state, caller = _world(tmp_path, monkeypatch)
    arms = _record_arms(monkeypatch, state)

    assert _create(state, caller, dry_run=True) == {"dry_run": True}

    assert arms == []


def test_a_refused_create_arms_nothing(tmp_path, monkeypatch):
    state, caller = _world(tmp_path, monkeypatch)
    arms = _record_arms(monkeypatch, state)
    real = sc.resolve_agent_bindings
    monkeypatch.setattr(
        sc,
        "resolve_agent_bindings",
        lambda cfg, agent_name=None, project_dir=None, **kw: dataclasses.replace(
            real(cfg, None, project_dir), requested_resolved=False
        ),
    )

    with pytest.raises(sc.SessionControlError):
        _create(state, caller, agent="no-such-agent")

    assert arms == []


def test_a_failed_birth_write_arms_nothing(tmp_path, monkeypatch):
    """A retracted child must not leave a spawn behind it."""
    state, caller = _world(tmp_path, monkeypatch)
    arms = _record_arms(monkeypatch, state)
    monkeypatch.setattr(
        state.conversation_log, "update_metadata", MagicMock(side_effect=OSError("disk full"))
    )

    with pytest.raises(OSError):
        _create(state, caller)

    assert arms == []


class TestEndToEnd:
    """The real scheduler and spawn body, with the session manager mocked."""

    @pytest.fixture(autouse=True)
    def _no_debounce(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)

    def _sessions(self, state):
        state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
        state.sessions.release = MagicMock()
        state.sessions.remove = AsyncMock()
        state.sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        state.sessions.resumable_hint = MagicMock(return_value=False)

    async def _create_child(self, state, caller, **kwargs):
        _prime(True)
        created = await sc.create_session(state, caller_session_key=_key(caller), **kwargs)
        child = state.get_slot(created["target"])
        assert child._eager_spawn_task is not None, "no eager spawn armed"
        return child

    @pytest.mark.asyncio
    async def test_the_spawn_uses_the_childs_model_and_effort(self, tmp_path, monkeypatch):
        state, caller = _world(tmp_path, monkeypatch)
        self._sessions(state)
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
            child = await self._create_child(state, caller, model=_MODEL, reasoning_effort=_EFFORT)
            await child._eager_spawn_task

        state.sessions.get_or_create.assert_awaited_once()
        kwargs = state.sessions.get_or_create.await_args.kwargs
        assert kwargs["model"] == _MODEL
        assert kwargs["reasoning_effort_override"] == _EFFORT
        assert kwargs["speculative"] is True
        assert kwargs["start_priority"] is StartPriority.BACKGROUND
        key = state.sessions.get_or_create.await_args.args[0]
        state.sessions.release.assert_called_once_with(key)

    @pytest.mark.asyncio
    async def test_a_seed_that_starts_first_owns_the_start(self, tmp_path, monkeypatch):
        """The seed's turn is running before the spawn wakes: no second start."""
        state, caller = _world(tmp_path, monkeypatch)
        self._sessions(state)
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
            child = await self._create_child(state, caller)
            _busy(child)
            await child._eager_spawn_task

        state.sessions.get_or_create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_child_closed_before_the_spawn_starts_nothing(self, tmp_path, monkeypatch):
        state, caller = _world(tmp_path, monkeypatch)
        self._sessions(state)
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
            child = await self._create_child(state, caller)
            state._slots.pop(child.key)
            await child._eager_spawn_task

        state.sessions.get_or_create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_child_closed_mid_handshake_is_torn_down(self, tmp_path, monkeypatch):
        state, caller = _world(tmp_path, monkeypatch)
        self._sessions(state)

        async def _create_then_close(*_a, **_kw):
            state._slots.pop(child.key, None)
            return (MagicMock(), True, False)

        state.sessions.get_or_create = AsyncMock(side_effect=_create_then_close)
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
            child = await self._create_child(state, caller)
            await child._eager_spawn_task

        key = state.sessions.get_or_create.await_args.args[0]
        state.sessions.remove.assert_awaited_once_with(key)
