"""Crew Mode migration edge cases (edge review R1: E1-E4, E6, E10).

One config moves every root when it is idle: a refusal that clears on its own is
retried, a permanent one never advances the recorded name, a cancel never leaves a
root half switched or unvisited, two runs never move one root twice, and a name
that cannot run Crew Mode moves nothing.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew import crew_mode
from kiro_crew.dashboard import crew_mode_migration as _mig
from kiro_crew.testing.wait import async_wait_until, until_parked

#: The real name check, kept so one test can put it back after the fixture stubs it.
_REAL_USABLE = _mig._usable


@pytest.fixture(autouse=True)
def _clean(_floor_monkeypatch, tmp_path):
    from kiro_crew.config import paths

    _floor_monkeypatch.setattr(crew_mode, "_known_roots", set())
    _floor_monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {})
    _floor_monkeypatch.setattr(crew_mode, "_live_cfg", lambda: None)
    _floor_monkeypatch.setattr(crew_mode, "_configured", crew_mode.DEFAULT_CREW_MODE_AGENT)
    _floor_monkeypatch.setattr(crew_mode, "_unusable", {})
    _floor_monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    _floor_monkeypatch.setattr(_mig, "_pending", set())
    _floor_monkeypatch.setattr(_mig, "_failed", {})
    _floor_monkeypatch.setattr(_mig, "_lock", None)
    _floor_monkeypatch.setattr(_mig, "_usable", lambda name: "")
    yield


def _slot(key: str, agent: str = "kirocrew-conductor", **over: Any) -> SimpleNamespace:
    base = dict(
        key=key,
        agent=agent,
        agent_kind="template",
        crew_mode_agent="",
        mode="",
        memory_mode="persistent",
        _created_by="",
        _app="",
        instance_id="",
        running=False,
        turn_running=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


class _State:
    def __init__(self, *slots: Any) -> None:
        self._slots = {s.key: s for s in slots}
        self.notices: list[tuple[str, str, str]] = []
        self.conversation_log = None
        self._background_tasks: set[Any] = set()
        # The dashboard's start-up restore ran and finished.
        self._slot_list_rebuild_began = 1.0
        self.slot_list_rebuilding = False

    def push_slots_update(self) -> None:
        pass

    def notify(self, category: str, title: str, body: str) -> None:
        self.notices.append((category, title, body))


def _answer(code: str | None, status: int = 409):
    if code is None:
        return None
    return web.json_response({"error": code, "code": code}, status=status)


@pytest.fixture
def switch(monkeypatch):
    """The migration's switch. ``refuse[key]`` answers that code instead of moving."""
    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard import crew_mode_migration as mig

    calls: list[tuple[str, str]] = []
    refuse: dict[str, tuple[str, int]] = {}
    gate: dict[str, asyncio.Event] = {}

    async def fake_switch(state, slot, name, agent, kind, caller):
        calls.append((name, agent))
        if name in gate:
            await gate[name].wait()
        if name in refuse:
            code, status = refuse[name]
            return _answer(code, status)
        slot.agent = agent
        slot.crew_mode_agent = agent
        return web.json_response({"ok": True, "agent": agent})

    monkeypatch.setattr(chat_handlers, "switch_slot_agent", fake_switch)
    monkeypatch.setattr(mig, "_audit", lambda *a, **k: None)
    return SimpleNamespace(calls=calls, refuse=refuse, gate=gate, mig=mig)


def _target(monkeypatch, name: str = "better-conductor") -> None:
    monkeypatch.setattr(crew_mode, "_configured", name)


@pytest.mark.asyncio
async def test_E1_attached_subagents_keep_the_root_owed_and_the_name_unadvanced(
    switch, monkeypatch
):
    switch.mig.write_applied("kirocrew-conductor")
    _target(monkeypatch)
    root = _slot("r")
    state = _State(root)
    switch.refuse["r"] = ("slot_subagents_running", 409)
    out = await switch.mig.reconcile(state)
    assert out == {"moved": [], "pending": ["r"]}
    assert switch.mig.read_applied() == "kirocrew-conductor"
    # Idle again: the retry loop's reconcile moves it, then advances the name.
    del switch.refuse["r"]
    out = await switch.mig.reconcile(state, retry_failed=False)
    assert out == {"moved": ["r"], "pending": []}
    assert root.agent == "better-conductor"
    assert switch.mig.read_applied() == "better-conductor"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["session_rebound", "history_unavailable"])
async def test_E1_other_retryable_refusals_are_retried(switch, monkeypatch, code):
    switch.mig.write_applied("kirocrew-conductor")
    _target(monkeypatch)
    state = _State(_slot("r"))
    switch.refuse["r"] = (code, 503 if code == "history_unavailable" else 409)
    out = await switch.mig.reconcile(state)
    assert out["pending"] == ["r"]
    assert switch.mig._failed == {}


@pytest.mark.asyncio
async def test_E1_a_permanent_refusal_never_advances_the_name_and_tells_the_person(
    switch, monkeypatch
):
    switch.mig.write_applied("kirocrew-conductor")
    _target(monkeypatch)
    ok = _slot("ok")
    bad = _slot("bad")
    state = _State(ok, bad)
    switch.refuse["bad"] = ("agent_choice_unavailable", 409)
    out = await switch.mig.reconcile(state)
    assert out == {"moved": ["ok"], "pending": []}
    assert switch.mig._failed == {"bad": "agent_choice_unavailable"}
    assert switch.mig.read_applied() == "kirocrew-conductor"
    assert len(state.notices) == 1 and "bad (agent_choice_unavailable)" in state.notices[0][2]
    # The idle loop does not hammer a permanent refusal, nor repeat the notice.
    n = len(switch.calls)
    await switch.mig.reconcile(state, retry_failed=False)
    assert len(switch.calls) == n and len(state.notices) == 1
    # A config change (or restart) tries it again.
    del switch.refuse["bad"]
    await switch.mig.reconcile(state)
    assert bad.agent == "better-conductor"
    assert switch.mig.read_applied() == "better-conductor"


@pytest.mark.asyncio
async def test_E2_the_migration_runs_off_the_applier_and_is_never_cancelled_by_its_bound(
    switch, monkeypatch
):
    switch.mig.write_applied("kirocrew-conductor")
    _target(monkeypatch)
    root = _slot("r")
    state = _State(root)
    switch.gate["r"] = asyncio.Event()
    task = switch.mig.schedule_on_config_change(state)
    assert task in state._background_tasks
    # The applier returned at once; the watcher's 10 s bound has nothing to cancel.
    await asyncio.sleep(0)
    assert not task.done()
    switch.gate["r"].set()
    await task
    assert root.agent == "better-conductor"
    assert switch.mig.read_applied() == "better-conductor"


def test_E2_config_watch_schedules_the_migration_instead_of_awaiting_it():
    from kiro_crew.dashboard.server_runtime import config_watch

    source = open(config_watch.__file__, encoding="utf-8").read()
    assert "crew_mode_migration.schedule_on_config_change(" in source
    assert "await crew_mode_migration.on_config_change(" not in source


@pytest.mark.asyncio
async def test_E2_a_cancel_inside_the_reset_rolls_the_switch_back():
    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

    slot = _ChatSlot("t1")
    prior_agent, prior_kind = slot.agent, slot.agent_kind
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.push_slots_update = MagicMock()
    state.conversation_log = MagicMock()
    state.sessions = MagicMock()
    state.sessions.get_provider = MagicMock(return_value=None)
    caller = chat_handlers.SlotAgentSwitchCaller(app="", remote="t", owner=True)
    with (
        patch(
            "kiro_crew.dashboard.chat_handlers._reset_slot_session_or_warn",
            new=AsyncMock(side_effect=asyncio.CancelledError),
        ),
        patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", new=AsyncMock()),
    ):
        with pytest.raises(asyncio.CancelledError):
            await chat_handlers.switch_slot_agent(state, slot, "t1", "kirocrew", "template", caller)
    assert slot.agent == prior_agent and slot.agent_kind == prior_kind
    state.conversation_log.update_metadata.assert_not_called()


@pytest.mark.asyncio
async def test_E3_a_cancel_before_the_second_root_leaves_it_owed_and_the_loop_moves_it(
    switch, monkeypatch
):
    switch.mig.write_applied("kirocrew-conductor")
    _target(monkeypatch)
    r1, r2 = _slot("r1"), _slot("r2")
    state = _State(r1, r2)
    switch.gate["r1"] = asyncio.Event()
    task = asyncio.create_task(switch.mig.migrate(state))
    # The first root's switch is parked on its gate: the pass has visited r1 and owes r2.
    await until_parked(switch.gate["r1"])
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert switch.mig._pending == {"r1", "r2"}
    assert switch.mig.read_applied() == "kirocrew-conductor"
    switch.gate.clear()
    await switch.mig.reconcile(state, retry_failed=False)
    assert r1.agent == r2.agent == "better-conductor"
    assert switch.mig.read_applied() == "better-conductor"


@pytest.mark.asyncio
async def test_E3_the_idle_loop_retries_when_the_name_is_not_yet_applied(switch, monkeypatch):
    switch.mig.write_applied("kirocrew-conductor")
    _target(monkeypatch)
    root = _slot("r")
    state = _State(root)
    monkeypatch.setattr(switch.mig, "RETRY_SECS", 0)

    async def stop_after(state):  # the start pass does nothing here
        return None

    monkeypatch.setattr(switch.mig, "on_start", stop_after)
    task = asyncio.create_task(switch.mig.run(state, start_delay=0))
    await async_wait_until(lambda: root.agent == "better-conductor")
    task.cancel()
    assert root.agent == "better-conductor"


@pytest.mark.asyncio
async def test_E4_two_concurrent_migrations_switch_one_root_once(switch, monkeypatch):
    switch.mig.write_applied("kirocrew-conductor")
    _target(monkeypatch)
    state = _State(_slot("r"))
    await asyncio.gather(switch.mig.reconcile(state), switch.mig.migrate(state))
    assert switch.calls == [("r", "better-conductor")]


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["agent_not_found", "agent_unresolved", "invalid_agent_name"])
async def test_E6_an_unusable_name_moves_nothing_and_keeps_the_applied_conductor(
    switch, monkeypatch, why
):
    switch.mig.write_applied("kirocrew-conductor")
    _target(monkeypatch, "typo-conductr")
    monkeypatch.setattr(switch.mig, "_usable", lambda name: why)
    root = _slot("r")
    state = _State(root)
    assert await switch.mig.reconcile(state) is None
    assert switch.calls == [] and root.agent == "kirocrew-conductor"
    assert switch.mig.read_applied() == "kirocrew-conductor"
    assert len(state.notices) == 1 and "typo-conductr" in state.notices[0][2]
    # Every reader keeps the applied conductor, so new chats still turn Crew Mode on.
    assert crew_mode.crew_mode_agent() == "kirocrew-conductor"
    assert "typo-conductr" not in crew_mode.conductor_agents()
    # The idle loop does not repeat the notice.
    await switch.mig.reconcile(state, retry_failed=False)
    assert len(state.notices) == 1
    # A later usable name clears it and moves the roots.
    _target(monkeypatch, "better-conductor")
    monkeypatch.setattr(switch.mig, "_usable", lambda name: "")
    await switch.mig.on_config_change(state)
    assert root.agent == "better-conductor"
    assert crew_mode.crew_mode_agent() == "better-conductor"


@pytest.mark.asyncio
async def test_F3_a_name_changed_mid_pass_leaves_the_rest_pending_with_no_notice(
    switch, monkeypatch
):
    switch.mig.write_applied("kirocrew-conductor")
    _target(monkeypatch, "a-conductor")
    r1, r2 = _slot("r1"), _slot("r2")
    state = _State(r1, r2)
    # The lock answers crew_mode_locked once the config names B.
    switch.refuse["r1"] = ("crew_mode_locked", 409)

    real_move = switch.mig._move

    async def move_then_change(state, slot, target):
        out = await real_move(state, slot, target)
        _target(monkeypatch, "b-conductor")
        return out

    monkeypatch.setattr(switch.mig, "_move", move_then_change)
    out = await switch.mig.migrate(state, "a-conductor")
    # Neither root is failed and nobody is told a chat did not move.
    assert switch.mig._failed == {}
    assert state.notices == []
    assert set(out["pending"]) == {"r1", "r2"}
    # r2 was never switched onto the stale A.
    assert ("r2", "a-conductor") not in switch.calls
    # The task that owns B moves both.
    del switch.refuse["r1"]
    monkeypatch.setattr(switch.mig, "_move", real_move)
    await switch.mig.migrate(state)
    assert r1.agent == r2.agent == "b-conductor"


@pytest.mark.asyncio
async def test_F3_a_template_installed_later_heals_without_a_config_edit(switch, monkeypatch):
    switch.mig.write_applied("kirocrew-conductor")
    _target(monkeypatch, "new-conductor")
    verdict = {"why": "agent_not_found"}
    monkeypatch.setattr(switch.mig, "_usable", lambda name: verdict["why"])
    root = _slot("r")
    state = _State(root)
    assert await switch.mig.reconcile(state) is None
    assert "until it is installed" in state.notices[0][2]
    # Installed now; the next idle tick checks the name again and moves the root.
    verdict["why"] = ""
    await switch.mig.reconcile(state, retry_failed=False)
    assert root.agent == "new-conductor"
    assert crew_mode.crew_mode_agent() == "new-conductor"


def test_E6_usable_asks_the_template_namespace_and_refuses_an_unknown_template(monkeypatch):
    """The resolver is asked in the ``template`` namespace, the one the migration's
    switch names: a name that is only a crewmate's does not resolve there, and a
    crewmate that shares its name with an installed template (an upgraded install can
    keep such an entry) does not make the template unusable."""
    from kiro_crew.config import KiroCrewConfig, loader_agents
    from kiro_crew.dashboard import crew_mode_migration as mig

    monkeypatch.setattr(mig, "_usable", _REAL_USABLE)  # the autouse fixture stubbed it
    cfg = SimpleNamespace(agents={"buddy": object(), "kirocrew-conductor": object()})
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    asked: list[str] = []

    def _resolve(c, name, p, **k):
        asked.append(k.get("selection_kind", ""))
        return SimpleNamespace(requested_resolved=name in ("real-conductor", "kirocrew-conductor"))

    monkeypatch.setattr(loader_agents, "resolve_agent_bindings", _resolve)
    assert mig._usable("buddy") == "agent_not_found"
    assert mig._usable("nope-conductor") == "agent_not_found"
    assert mig._usable("real-conductor") == ""
    assert mig._usable("kirocrew-conductor") == ""
    assert asked and set(asked) == {"template"}
    assert mig._usable("../x") == "invalid_agent_name"


def test_E10_a_migration_switch_counts_no_turned_on():
    from kiro_crew.dashboard.chat_api import agent_switch

    source = open(agent_switch.__file__, encoding="utf-8").read()
    i = source.index("crew_turned_on = (")
    block = source[i : source.index("\n        )\n", i)]
    assert "not caller.crew_mode_migration" in block
    assert "prior_crew_stamp" in block


class _Restoring:
    """A state whose start-up restore is still rebuilding the slot set for a few reads."""

    def __init__(self, reads_until_done: int) -> None:
        self.reads = 0
        self._until = reads_until_done
        self._slots: dict[str, Any] = {}
        self._slot_list_rebuild_began = 1.0

    @property
    def slot_list_rebuilding(self) -> bool:
        self.reads += 1
        return self.reads < self._until


@pytest.mark.asyncio
async def test_E11_the_start_pass_waits_for_the_restore_to_rebuild_the_slots(monkeypatch):
    """A reconcile over the half-restored slot set would record the name against an
    empty set, and the roots restored after it would never move."""
    monkeypatch.setattr(_mig, "RESTORE_POLL_SECS", 0.001)
    state = _Restoring(reads_until_done=4)
    await _mig._await_restore(state)
    assert state.reads == 4
    # Bounded: a restore that never finishes does not hold the migration forever.
    stuck = _Restoring(reads_until_done=10**9)
    await _mig._await_restore(stuck, limit=0.01)
    assert stuck.reads >= 1
    # The session tree's seed is waited for the same way: with the crew log on and the
    # lineage not yet settled the wait holds, and ends when it settles.
    monkeypatch.setattr("kiro_crew.crew_log.emit.enabled", lambda: True)
    seeded = iter([False, False, True])
    monkeypatch.setattr(crew_mode, "lineage_is_settled", lambda: next(seeded, True))
    await _mig._await_restore(_Restoring(reads_until_done=1))
    assert next(seeded, "spent") == "spent"


def test_E11_run_awaits_the_restore_before_the_start_pass():
    source = open(_mig.__file__, encoding="utf-8").read()
    body = source.split("async def run(", 1)[1]
    assert body.index("await _await_restore(state)") < body.index("await on_start(state)")


@pytest.mark.asyncio
async def test_E11_a_config_change_during_the_restore_waits_for_it(monkeypatch):
    # The watcher's applier reaches the reconcile without the start pass; it must
    # wait for the same restore, or it records the name against the half-built set.
    monkeypatch.setattr(_mig, "RESTORE_POLL_SECS", 0.001)
    seen: list[int] = []

    async def _reconcile(state, **kw):
        seen.append(state.reads)
        return None

    monkeypatch.setattr(_mig, "reconcile", _reconcile)
    state = _Restoring(reads_until_done=3)
    await _mig.on_config_change(state)
    assert seen == [3], "reconcile ran only once the restore had finished"


@pytest.mark.asyncio
async def test_E12_an_adopted_conductor_is_not_learned_as_a_closed_root(monkeypatch):
    # Its saved line says "no creator", but the session tree holds the adoption edge.
    monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {"adopted": "root"})

    class _Log:
        def list_sessions(self) -> list[dict]:
            return [
                {"key": "dashboard_root", "agent": "kirocrew-conductor"},
                {"key": "dashboard_adopted", "agent": "kirocrew-conductor"},
            ]

        def get_metadata(self, key: str) -> dict:
            return {"agent": "kirocrew-conductor", "crew_mode_agent": "kirocrew-conductor"}

    state = _State()
    state.conversation_log = _Log()
    await _mig.learn_closed_roots(state)
    assert "root" in crew_mode.known_roots()
    assert "adopted" not in crew_mode.known_roots()


@pytest.mark.asyncio
async def test_E13_the_reconcile_defers_while_the_restore_runs_and_on_a_headless_start(
    switch, monkeypatch
):
    """Over a half-built or absent slot set every root is trivially on the target, so
    nothing is read or written until the dashboard's restore has finished; a gateway
    that never began a restore (headless) records nothing either."""
    switch.mig.write_applied("kirocrew-conductor")
    _target(monkeypatch)
    root = _slot("r", "kirocrew-conductor")
    still = _State(root)
    still.slot_list_rebuilding = True
    assert await switch.mig.reconcile(still) is None
    assert root.agent == "kirocrew-conductor" and switch.calls == []
    headless = _State()
    headless._slot_list_rebuild_began = 0.0
    assert await switch.mig.reconcile(headless) is None
    assert switch.mig.read_applied() == "kirocrew-conductor"
    # With the slots rebuilt but the session tree still seeding, the root rule would
    # fall back to the birth stamps, which an adopted worker lacks, so the reconcile
    # still waits; it runs once the lineage is settled (or the crew log is off).
    still.slot_list_rebuilding = False
    monkeypatch.setattr("kiro_crew.crew_log.emit.enabled", lambda: True)
    monkeypatch.setattr(crew_mode, "lineage_is_settled", lambda: False)
    assert await switch.mig.reconcile(still) is None
    assert root.agent == "kirocrew-conductor" and switch.calls == []
    monkeypatch.setattr(crew_mode, "lineage_is_settled", lambda: True)
    out = await switch.mig.reconcile(still)
    assert out == {"moved": ["r"], "pending": []}
    assert switch.mig.read_applied() == "better-conductor"
