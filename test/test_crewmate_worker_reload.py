# SPDX-License-Identifier: Apache-2.0
"""A capability approved mid-task reaches the SAME running worker (option A).

Every capability a crewmate's file approves is mounted on every one of its
workers, so a card applied (or the user adding one in advanced settings) changes
what the next spawn of each worker mounts. The gateway then reloads that same
worker session onto the rebuilt spec, conversation kept, and opens no new one
(``dashboard/crewmate_worker_reload.py``). Only an idle worker is reloaded; a
busy one is reloaded once its turn ends. The card that lets a crewmate ask for
tools (``mate_tool_access``) reloads its conductor onto the request tools and
wakes it to raise the capabilities card.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web

from kiro_crew import change_card_catalog as catalog
from kiro_crew import crewmate_prompts
from kiro_crew.dashboard import crewmate_worker_reload as reload_mod


class _Provider:
    def __init__(self) -> None:
        self.busy = False

    def has_active_turn(self) -> bool:
        return self.busy


class _Sessions:
    def __init__(self) -> None:
        self.providers: dict[str, _Provider] = {}
        self.created: list[str] = []

    def get_provider(self, key: str):
        return self.providers.get(key)

    def create(self, key: str):  # a new session would land here
        self.created.append(key)


class _Slot:
    """Like ``_ChatSlot``: ``__slots__``, so no attribute can be added to it."""

    __slots__ = ("key", "_queue")

    def __init__(self, key: str) -> None:
        self.key = key
        self._queue: list = []


def _state(*keys: str):
    state = SimpleNamespace(_slots={}, sessions=_Sessions())
    for key in keys:
        state._slots[key] = _Slot(key)
        state.sessions.providers["dashboard_" + key] = _Provider()
    return state


@pytest.fixture
def reloads(monkeypatch):
    """Record each teardown the reload runs, as the tab menu's Reload session would."""
    from kiro_crew.dashboard import chat_handlers

    seen: list[tuple[str, object]] = []

    async def fake_reload(state, slot, name, *, still_ours, denied, busy, notice):
        assert still_ours(False) and denied(name) is None
        if busy("dashboard_" + name):
            return web.json_response({"code": "turn_in_flight"}, status=409)
        seen.append((name, slot))
        return web.json_response({"ok": True})

    monkeypatch.setattr(chat_handlers, "reload_slot_session", fake_reload)
    monkeypatch.setattr(chat_handlers, "_switch_target_busy", lambda *a: False)
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_utils.effective_session_key", lambda slot: "dashboard_" + slot.key
    )
    monkeypatch.setattr(reload_mod, "RETRY_SECS", 0)
    return seen


def test_an_applied_card_reloads_the_same_worker_and_opens_none(reloads, monkeypatch):
    state = _state("w1", "w2", "other")
    monkeypatch.setattr(
        reload_mod, "_workers_of", lambda st, member: [st._slots["w1"], st._slots["w2"]]
    )
    out = asyncio.run(reload_mod.reload_workers(state, "Scout"))
    assert out == {"w1": reload_mod.RELOADED, "w2": reload_mod.RELOADED}
    # The SAME slot objects, by their own keys: the conversation lives on.
    assert reloads == [("w1", state._slots["w1"]), ("w2", state._slots["w2"])]
    assert state.sessions.created == [] and set(state._slots) == {"w1", "w2", "other"}


def test_a_worker_not_running_is_left_to_its_next_spawn(reloads):
    state = _state("w1")
    state.sessions.providers.clear()
    assert asyncio.run(reload_mod.reload_if_idle(state, state._slots["w1"])) == (
        reload_mod.NOT_RUNNING
    )
    assert reloads == []


def test_a_worker_still_cold_starting_keeps_the_reload_owed(reloads, monkeypatch):
    """No provider yet, but its first turn is in flight: it spawns on the old kit."""
    from kiro_crew.dashboard import chat_handlers

    state = _state("w1")
    state.sessions.providers.clear()
    monkeypatch.setattr(chat_handlers, "_switch_target_busy", lambda *a: True)
    slot = state._slots["w1"]
    assert asyncio.run(reload_mod.reload_if_idle(state, slot)) == reload_mod.BUSY
    assert reload_mod.is_owed(state, slot)
    assert reloads == []


def test_a_busy_worker_is_reloaded_once_its_turn_ends(reloads):
    state = _state("w1")
    slot = state._slots["w1"]
    provider = state.sessions.providers["dashboard_w1"]
    provider.busy = True
    assert asyncio.run(reload_mod.reload_if_idle(state, slot)) == reload_mod.BUSY
    assert reloads == [] and reload_mod.is_owed(state, slot) is True

    async def turn_ends_then_settles():
        reload_mod.note_turn_end(state, slot)
        await asyncio.sleep(0)
        provider.busy = False  # the turn has let go of the slot
        await asyncio.gather(*list(state._crewmate_reload_tasks))

    asyncio.run(turn_ends_then_settles())
    assert reloads == [("w1", slot)]
    assert reload_mod.is_owed(state, slot) is False

    # Nothing owed: a later turn end reloads nothing more.
    async def later():
        reload_mod.note_turn_end(state, slot)
        await asyncio.sleep(0)

    asyncio.run(later())
    assert reloads == [("w1", slot)]


def test_the_capabilities_route_schedules_the_reload(monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers import agent_capabilities as route

    scheduled: list[str] = []

    class _Service:
        def put(self, name, body):
            return {"mode": "mate", "name": name}

    async def owner(_request, _operation):
        return None

    monkeypatch.setattr(route, "require_owner_dashboard_request", owner)
    monkeypatch.setattr(
        "kiro_crew.dashboard.crewmate_worker_reload.schedule_reload_workers",
        lambda state, member: scheduled.append(member),
    )

    async def go():
        app = web.Application()
        app["state"] = SimpleNamespace(push_refresh=lambda *_a: None, sessions=None)
        app[route._SERVICE] = _Service()
        app.router.add_put("/api/agents/{name}/capabilities", route.api_member_capabilities)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/agents/scout/capabilities", json={"operations": []})
            assert resp.status == 200, await resp.text()

    asyncio.run(go())
    assert scheduled == ["scout"]


# ── card 1 (ask for tools) applied -> the conductor can raise card 2 ──


def _complete(monkeypatch, kind, params):
    from kiro_crew.dashboard import goal_setup
    from kiro_crew.dashboard.handlers import change_cards

    nudged: list[tuple[str, str]] = []
    store = SimpleNamespace(complete_apply=lambda rec, **kw: None)
    monkeypatch.setattr(change_cards, "card_store_for", lambda state: store)

    async def read_state(*a, **k):
        return {}

    monkeypatch.setattr(change_cards.cards, "read_state", read_state)
    monkeypatch.setattr(change_cards.cards, "after_shows_the_write", lambda *a: True)
    monkeypatch.setattr(catalog, "applied_write_count", lambda *a: 1)
    monkeypatch.setattr(catalog, "build_undo", lambda *a: (None, "x"))
    monkeypatch.setattr(
        goal_setup,
        "schedule_tool_access_nudge",
        lambda s, k, *, app, session_key, card_member: nudged.append(
            ("tool_access", k, session_key, card_member)
        ),
    )
    monkeypatch.setattr(
        goal_setup,
        "schedule_capabilities_nudge",
        lambda s, k, t, *, card_member: nudged.append(("caps", k, card_member)),
    )
    rec = {
        "id": "c1",
        "kind": kind,
        "params": params,
        "before": {},
        "evidence": [],
        "plan": {"apply": []},
        "slot_key": "member-scout",
        "session_key": "dashboard:member-scout",
        "title": "t",
    }
    asyncio.run(change_cards._complete(SimpleNamespace(app={"state": None}), rec, partial=False))
    return nudged


def test_applying_card_1_wakes_the_conductor_for_card_2(monkeypatch):
    fields = {"dynamic_capabilities": True}
    got = _complete(monkeypatch, catalog.KIND_CREWMATE_UPDATE, {"name": "scout", "fields": fields})
    assert got == [("tool_access", "member-scout", "dashboard:member-scout", "scout")]
    # Any other crewmate update wakes nobody.
    other = {"name": "scout", "fields": {"model": "x"}}
    assert _complete(monkeypatch, catalog.KIND_CREWMATE_UPDATE, other) == []


def test_a_card_turning_on_another_crewmate_wakes_nobody_in_this_thread(monkeypatch):
    """Scout raised a card for Briar: Scout's held request and session stay as they are."""
    from kiro_crew.dashboard import goal_setup
    from kiro_crew.dashboard.handlers import member_self

    conductor = SimpleNamespace(key="member-scout", agent="scout")
    state = SimpleNamespace(get_slot=lambda key: conductor if key == "member-scout" else None)

    async def never(*_a):
        raise AssertionError("raised a held request")

    monkeypatch.setattr(member_self, "continue_after_opt_in", never)
    monkeypatch.setattr(
        "kiro_crew.dashboard.mate_thread_wakes.is_crewmate_thread", lambda slot: True
    )

    async def go():
        goal_setup.schedule_tool_access_nudge(
            state,
            "member-scout",
            app=object(),
            session_key="dashboard:member-scout",
            card_member="briar",
        )
        assert not goal_setup._tasks(state)

    asyncio.run(go())


@pytest.mark.parametrize("held_raised", [True, False], ids=["raised", "not_raised"])
def test_turn_on_raises_the_held_request_then_reloads_and_wakes(monkeypatch, held_raised):
    from kiro_crew.dashboard import goal_setup
    from kiro_crew.dashboard.handlers import member_self

    order: list[str] = []
    conductor = SimpleNamespace(key="member-scout", agent="scout")
    state = SimpleNamespace(get_slot=lambda key: conductor if key == "member-scout" else None)

    async def fake_reload(st, slot):
        order.append("reload:" + slot.key)
        return reload_mod.RELOADED

    async def fake_continue(app, slot_key, sk, member):
        order.append(f"raise:{slot_key}:{sk}:{member}")
        return "" if held_raised else "not_on_gateway"

    monkeypatch.setattr(member_self, "continue_after_opt_in", fake_continue)

    def fake_schedule(st, key, slug, member, build, label):
        order.append("wake:" + build({}, True))

    monkeypatch.setattr(reload_mod, "reload_if_idle", fake_reload)
    monkeypatch.setattr(goal_setup, "_schedule", fake_schedule)
    monkeypatch.setattr(
        "kiro_crew.dashboard.mate_thread_wakes.is_crewmate_thread", lambda slot: True
    )
    monkeypatch.setattr("kiro_crew.members.member_slug", lambda m: m)

    async def go():
        goal_setup.schedule_tool_access_nudge(
            state,
            "member-scout",
            app=object(),
            session_key="dashboard:member-scout",
            card_member="scout",
        )
        await asyncio.gather(*list(goal_setup._tasks(state).values()))

    asyncio.run(go())
    assert order[0] == "raise:member-scout:dashboard:member-scout:scout"
    assert order[1] == "reload:member-scout"
    assert "[Tool access on]" in order[2] and "still needs their approval" in order[2]
    if held_raised:
        assert "do not raise it again" in order[2]
    else:
        assert "request_capabilities" in order[2] and "find_capabilities" in order[2]


def test_card_2_is_the_unchanged_capabilities_card(monkeypatch):
    """Card 2 is ``request_capabilities``' own card: its kind and its nudge, untouched."""
    from kiro_crew import crewmate_dynamic_capabilities as dyn

    dyn._override = True
    got = _complete(monkeypatch, catalog.KIND_CREWMATE_CAPABILITIES, {"member": "scout"})
    assert got == [("caps", "member-scout", "scout")]
    text = crewmate_prompts.text("goal_setup.capabilities_added")
    assert "SAME worker" in text and "session_send" in text and "session_create" not in text


def test_a_turn_that_starts_inside_the_teardown_leaves_the_reload_owed(reloads, monkeypatch):
    """The fast probe said idle, the teardown's own check saw a turn: owed, not done."""
    from kiro_crew.dashboard import chat_handlers

    async def refused(state, slot, name, **_kw):
        return web.json_response({"code": "turn_in_flight"}, status=409)

    monkeypatch.setattr(chat_handlers, "reload_slot_session", refused)
    state = _state("w1")
    slot = state._slots["w1"]
    assert asyncio.run(reload_mod.reload_if_idle(state, slot)) == reload_mod.BUSY
    assert reload_mod.is_owed(state, slot) is True


# ── the edges: a replaced slot, a failed or refused reload, the roster read ──


def test_a_slot_replaced_under_the_same_key_is_gone(reloads):
    state = _state("w1")
    stale = _Slot("w1")
    assert asyncio.run(reload_mod.reload_if_idle(state, stale)) == reload_mod.GONE
    assert reloads == []


def test_a_reload_that_raises_or_is_refused_leaves_nothing_owed(reloads, monkeypatch):
    from kiro_crew.dashboard import chat_handlers

    state = _state("w1")
    slot = state._slots["w1"]

    async def broken(*_a, **_k):
        raise RuntimeError("teardown failed")

    monkeypatch.setattr(chat_handlers, "reload_slot_session", broken)
    assert asyncio.run(reload_mod.reload_if_idle(state, slot)) == reload_mod.GONE

    async def refused(*_a, **_k):
        return web.json_response({"code": "forbidden"}, status=403)

    monkeypatch.setattr(chat_handlers, "reload_slot_session", refused)
    assert asyncio.run(reload_mod.reload_if_idle(state, slot)) == reload_mod.GONE
    assert reload_mod.is_owed(state, slot) is False


def test_a_queued_message_counts_as_busy(reloads):
    state = _state("w1")
    slot = state._slots["w1"]
    slot._queue.append("next")
    assert asyncio.run(reload_mod.reload_if_idle(state, slot)) == reload_mod.BUSY
    assert reloads == [] and reload_mod.is_owed(state, slot)


def test_the_workers_of_a_crewmate_are_its_own_worker_records(monkeypatch):
    from kiro_crew import execution_context
    from kiro_crew.config import loader
    from kiro_crew.dashboard import chat_utils
    from kiro_crew.mate_worker import MATE_WORKER_AGENT

    state = _state("mine", "theirs", "chat", "broken", "none")
    records = {
        "mine": SimpleNamespace(template_id=MATE_WORKER_AGENT, member_id="m-scout"),
        "theirs": SimpleNamespace(template_id=MATE_WORKER_AGENT, member_id="m-briar"),
        "chat": SimpleNamespace(template_id="kirocrew", member_id="m-scout"),
        "none": None,
    }

    def read(key):
        if key == "broken":
            raise OSError("unreadable record")
        return records[key]

    roster = SimpleNamespace(
        agents={"Scout": SimpleNamespace(member_id="m-scout"), "Nemo": SimpleNamespace()}
    )
    monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda cls: roster))
    monkeypatch.setattr(chat_utils, "slot_history_key", lambda slot: slot.key)
    monkeypatch.setattr(execution_context, "read_session_execution", read)
    assert reload_mod._workers_of(state, "Scout") == [state._slots["mine"]]
    # No member id, or no record: no workers.
    assert reload_mod._workers_of(state, "Nemo") == []
    assert reload_mod._workers_of(state, "Ghost") == []


def test_an_unreadable_roster_reloads_nobody(monkeypatch):
    def unreadable(*_a):
        raise OSError("config unreadable")

    monkeypatch.setattr(reload_mod, "_workers_of", unreadable)
    assert asyncio.run(reload_mod.reload_workers(_state("w1"), "Scout")) == {}


def test_the_scheduled_reload_runs_as_a_task_and_is_tracked(reloads, monkeypatch):
    state = _state("w1")
    monkeypatch.setattr(reload_mod, "_workers_of", lambda st, member: [st._slots["w1"]])
    # Nothing to do without a state, a crewmate, or a running loop.
    assert reload_mod.schedule_reload_workers(None, "Scout") is None
    assert reload_mod.schedule_reload_workers(state, "") is None
    assert reload_mod.schedule_reload_workers(state, "Scout") is None

    async def go():
        task = reload_mod.schedule_reload_workers(state, "Scout")
        assert task is not None and task in state._crewmate_reload_tasks
        return await task

    assert asyncio.run(go()) == {"w1": reload_mod.RELOADED}
    assert state._crewmate_reload_tasks == set()


def test_a_worker_busy_through_every_retry_stays_owed(reloads, monkeypatch):
    state = _state("w1")
    slot = state._slots["w1"]
    state.sessions.providers["dashboard_w1"].busy = True
    tries: list[str] = []
    real = reload_mod.reload_if_idle

    async def counted(st, sl):
        tries.append(sl.key)
        return await real(st, sl)

    monkeypatch.setattr(reload_mod, "reload_if_idle", counted)
    reload_mod._mark(state, slot, True)
    # Outside a loop the turn end schedules nothing.
    reload_mod.note_turn_end(state, slot)
    assert tries == []

    async def go():
        reload_mod.note_turn_end(state, slot)
        await asyncio.gather(*list(state._crewmate_reload_tasks))

    asyncio.run(go())
    assert tries == ["w1"] * reload_mod.RETRIES
    assert reloads == [] and reload_mod.is_owed(state, slot)
