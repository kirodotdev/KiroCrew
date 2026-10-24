# SPDX-License-Identifier: Apache-2.0
"""The Preview opt-in card a crewmate's first ``request_capabilities`` raises while its flag is off.

``request_capabilities`` stays mounted with a crewmate's Dynamic capabilities off
(``kiro_crew.mate_tool_access``). Its first call raises ONE ``crewmate.update``
card writing ``dynamic_capabilities: true`` for that crewmate alone, instead of
the capabilities card, and holds the request. Turn on raises the held request as
its own capabilities card; Not now leaves the flag off, and every later call is
refused with no second card. With the flag on the request is its card, as ever.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from guide_route_helpers import FakeState
from member_goals_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    ORDINARY,
    PROPOSE,
    _call,
    _owner_ok,
    _quiet_sel,
    _seed,
    _thread,
)

from kiro_crew import change_card_catalog as catalog
from kiro_crew import crewmate_dynamic_capabilities as dyn
from kiro_crew import crewmate_prompts, mate_tool_access
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard.handlers import member_self as self_routes

DRAFT = {"operations": [{"section": "skills", "id": "triage", "action": "set", "value": True}]}
REQUEST = {"request": "capabilities", "params": {"draft": DRAFT}, "reason": "the task triages"}
OPT_IN = {
    "kind": catalog.KIND_CREWMATE_UPDATE,
    "params": {"name": ORDINARY, "fields": {"dynamic_capabilities": True}},
}


@pytest.fixture
def off():
    """This crewmate's flag off."""
    dyn._override = False


@pytest.fixture
def proposals(_floor_monkeypatch):
    """Capture each card proposed; the store answers the status *statuses* holds.

    A card id *statuses* does not hold is one the store has pruned; one it maps
    to ``None`` is a store that cannot be read.
    """
    from kiro_crew.dashboard.handlers import change_cards as card_routes

    seen: list[dict] = []
    statuses: dict[str, str] = {}

    async def fake_propose(_request, slot_key, sk, *, kind, params, reason):
        card_id = f"c{len(seen) + 1:08d}"
        seen.append({"kind": kind, "params": params, "id": card_id, "slot": slot_key})
        statuses[card_id] = "pending"
        return web.json_response({"id": card_id, "status": "pending", "kind": kind})

    async def fake_status(_request, card_id):
        return statuses[card_id] if card_id in statuses else self_routes._CARD_GONE

    _floor_monkeypatch.setattr(card_routes, "propose_for_caller", fake_propose)
    _floor_monkeypatch.setattr(self_routes, "_card_status", fake_status)
    return seen, statuses


def _request(state, slot) -> tuple[int, dict]:
    return _call(state, PROPOSE, REQUEST, slot.key)


def _kinds(seen) -> list[str]:
    return [c["kind"] for c in seen]


# ── off: the first call raises the opt-in card, not the capabilities card ──


@pytest.mark.parametrize("user_sent", [True, False], ids=["user_turn", "worker_report"])
def test_off_the_first_request_raises_the_preview_opt_in(off, proposals, user_sent):
    seen, _ = proposals
    _seed()
    state = FakeState()
    slot = _thread(state, ORDINARY, user_sent=user_sent)
    if not user_sent:
        # A raw operations draft stays user-turn only: off or on, it is refused the same.
        status, body = _request(state, slot)
        assert (status, body["code"]) == (403, "not_user_turn")
        assert seen == [] and mate_tool_access.entry(ORDINARY) is None
        return
    status, body = _request(state, slot)
    assert status == 200, body
    assert body["opt_in"] is True
    assert [{k: c[k] for k in ("kind", "params")} for c in seen] == [OPT_IN]
    found = mate_tool_access.entry(ORDINARY)
    assert found["state"] == mate_tool_access.STATE_OFFERED
    assert found["held"] == {"params": {"draft": DRAFT}, "reason": "the task triages"}


def test_turn_on_raises_the_held_request_as_its_card(off, proposals):
    seen, statuses = proposals
    _seed()
    state = FakeState()
    slot = _thread(state, ORDINARY)
    assert _request(state, slot)[0] == 200
    # The user pressed Turn on: the card applied and wrote the flag on.
    statuses[seen[0]["id"]] = "applied"
    dyn._override = True
    why = asyncio.run(
        self_routes.continue_after_opt_in(object(), slot.key, "dashboard:" + slot.key, ORDINARY)
    )
    assert why == ""
    assert _kinds(seen) == [catalog.KIND_CREWMATE_UPDATE, catalog.KIND_CREWMATE_CAPABILITIES]
    assert seen[1]["params"] == {"member": ORDINARY, "draft": DRAFT}
    assert seen[1]["slot"] == slot.key
    # Taken once: a retried apply raises nothing more.
    again = asyncio.run(
        self_routes.continue_after_opt_in(object(), slot.key, "dashboard:" + slot.key, ORDINARY)
    )
    assert again == "no_request_held" and len(seen) == 2


def test_the_turn_on_apply_hands_the_app_and_thread_to_the_nudge(monkeypatch):
    """``_complete`` passes what ``continue_after_opt_in`` needs to raise the held request."""
    import inspect

    from kiro_crew.dashboard import goal_setup
    from kiro_crew.dashboard.handlers import change_cards as card_routes

    source = inspect.getsource(card_routes._complete)
    assert "schedule_tool_access_nudge(" in source
    assert "app=request.app" in source and "session_key=" in source
    params = inspect.signature(goal_setup.schedule_tool_access_nudge).parameters
    assert {"app", "session_key", "card_member"} <= set(params)
    assert "card_member=" in source


@pytest.mark.parametrize(
    "card_status,state_name",
    [("cancelled", "declined"), ("expired", "expired"), ("applied", "applied")],
)
def test_not_now_refuses_and_never_raises_a_second_card(off, proposals, card_status, state_name):
    seen, statuses = proposals
    _seed()
    state = FakeState()
    slot = _thread(state, ORDINARY)
    assert _request(state, slot)[0] == 200
    statuses[seen[0]["id"]] = card_status
    if card_status == "applied":
        # Turned on and then switched off again by hand: still never asked twice.
        dyn._override = False
    for _ in range(2):
        status, body = _request(state, slot)
        assert (status, body["code"]) == (403, "dynamic_capabilities_off")
        assert "advanced settings" in body["error"]
    assert mate_tool_access.entry(ORDINARY)["state"] == state_name
    assert len(seen) == 1


def test_a_waiting_opt_in_is_not_raised_twice(off, proposals):
    seen, _ = proposals
    _seed()
    state = FakeState()
    slot = _thread(state, ORDINARY)
    assert _request(state, slot)[0] == 200
    status, body = _request(state, slot)
    assert (status, body["code"]) == (409, "opt_in_pending")
    assert len(seen) == 1


def test_the_opt_in_is_per_crewmate(off, proposals):
    seen, statuses = proposals
    _seed()
    cfg = KiroCrewConfig.load()
    cfg.agents["briar"] = KiroCrewAgentConfig(kiro_agent=cfg.default_agent)
    cfg.save()
    state = FakeState()
    assert _request(state, _thread(state, ORDINARY))[0] == 200
    statuses[seen[0]["id"]] = "cancelled"
    assert _request(state, _thread(state, ORDINARY))[1]["code"] == "dynamic_capabilities_off"
    # Another crewmate was never asked, so its first request raises its own opt-in.
    assert _request(state, _thread(state, "briar"))[0] == 200
    assert [c["params"]["name"] for c in seen] == [ORDINARY, "briar"]


# ── on: unchanged ──


def test_on_the_request_is_its_capabilities_card(proposals):
    seen, _ = proposals
    dyn._override = True
    _seed()
    state = FakeState()
    slot = _thread(state, ORDINARY)
    status, body = _request(state, slot)
    assert status == 200 and "opt_in" not in body
    assert _kinds(seen) == [catalog.KIND_CREWMATE_CAPABILITIES]
    assert seen[0]["params"] == {"member": ORDINARY, "draft": DRAFT}
    assert mate_tool_access.entry(ORDINARY) is None


def test_an_offer_that_cannot_be_recorded_raises_no_card(off, proposals, monkeypatch):
    seen, _ = proposals

    def unwritable(*_a):
        raise OSError("read-only")

    monkeypatch.setattr(mate_tool_access, "_write", unwritable)
    _seed()
    state = FakeState()
    slot = _thread(state, ORDINARY)
    status, body = _request(state, slot)
    assert (status, body["code"]) == (503, "opt_in_unrecorded")
    assert seen == []


def test_a_card_that_is_not_raised_takes_its_record_back(off, proposals, monkeypatch):
    from kiro_crew.dashboard.handlers import change_cards as card_routes

    seen, _ = proposals

    async def refused(_request, slot_key, sk, *, kind, params, reason):
        return web.json_response({"error": "store busy", "code": "busy"}, status=503)

    monkeypatch.setattr(card_routes, "propose_for_caller", refused)
    _seed()
    state = FakeState()
    slot = _thread(state, ORDINARY)
    assert _request(state, slot)[1]["code"] == "busy"
    assert mate_tool_access.entry(ORDINARY) is None


def test_the_record_is_written_before_the_card(off, proposals, monkeypatch):
    """The card's id is filled in after; a lost second write keeps the held request."""
    from kiro_crew.dashboard.handlers import change_cards as card_routes

    seen, statuses = proposals
    real = card_routes.propose_for_caller

    async def look_first(*a, **k):
        if k["kind"] == catalog.KIND_CREWMATE_UPDATE:
            found = mate_tool_access.entry(ORDINARY)
            assert found is not None and found["held"]["reason"] == "the task triages"
        return await real(*a, **k)

    monkeypatch.setattr(card_routes, "propose_for_caller", look_first)
    real_write = mate_tool_access._write
    writes: list[int] = []

    def second_write_lost(data):
        writes.append(1)
        if len(writes) > 1:
            raise OSError("disk full")
        real_write(data)

    monkeypatch.setattr(mate_tool_access, "_write", second_write_lost)
    _seed()
    state = FakeState()
    slot = _thread(state, ORDINARY)
    assert _request(state, slot)[0] == 200
    monkeypatch.setattr(mate_tool_access, "_write", real_write)
    # No card id recorded yet: still waiting, never a second card.
    assert _request(state, slot)[1]["code"] == "opt_in_pending"
    assert len(seen) == 1
    statuses[seen[0]["id"]] = "applied"
    dyn._override = True
    why = asyncio.run(
        self_routes.continue_after_opt_in(object(), slot.key, "dashboard:" + slot.key, ORDINARY)
    )
    assert why == "" and len(seen) == 2


def test_a_reservation_whose_card_never_came_ages_out(off, proposals, monkeypatch):
    from kiro_crew.testing.clock import ManualClock

    seen, _ = proposals
    clock = ManualClock()
    clock.install(monkeypatch, mate_tool_access)
    _seed()
    mate_tool_access.reserve_offer(ORDINARY, {"params": {}, "reason": None})
    state = FakeState()
    slot = _thread(state, ORDINARY)
    assert _request(state, slot)[1]["code"] == "opt_in_pending"
    clock.advance(mate_tool_access.RESERVATION_GRACE_SECS + 1)
    assert _request(state, slot)[1]["code"] == "dynamic_capabilities_off"
    assert seen == []


def test_an_offer_is_reserved_once():
    assert mate_tool_access.reserve_offer(ORDINARY, {"params": {}}) is True
    assert mate_tool_access.reserve_offer(ORDINARY, {"params": {"x": 1}}) is False
    assert mate_tool_access.entry(ORDINARY)["held"] == {"params": {}}


def test_a_held_request_waits_for_its_own_crewmates_flag(off, proposals):
    """Raised from an apply, so the caller's flag check is made here."""
    seen, statuses = proposals
    _seed()
    state = FakeState()
    slot = _thread(state, ORDINARY)
    assert _request(state, slot)[0] == 200
    why = asyncio.run(
        self_routes.continue_after_opt_in(object(), slot.key, "dashboard:" + slot.key, ORDINARY)
    )
    assert why == "dynamic_capabilities_off"
    assert len(seen) == 1
    assert mate_tool_access.entry(ORDINARY)["held"]


def test_a_pruned_opt_in_card_counts_as_expired(off, proposals):
    """A card the store has pruned is not waiting: refused, and no second card."""
    seen, statuses = proposals
    _seed()
    state = FakeState()
    slot = _thread(state, ORDINARY)
    assert _request(state, slot)[0] == 200
    del statuses[seen[0]["id"]]
    for _ in range(2):
        status, body = _request(state, slot)
        assert (status, body["code"]) == (403, "dynamic_capabilities_off")
    assert mate_tool_access.entry(ORDINARY)["state"] == mate_tool_access.STATE_EXPIRED
    assert len(seen) == 1


def test_an_unreadable_card_store_leaves_the_opt_in_waiting(off, proposals):
    seen, statuses = proposals
    _seed()
    state = FakeState()
    slot = _thread(state, ORDINARY)
    assert _request(state, slot)[0] == 200
    statuses[seen[0]["id"]] = None
    status, body = _request(state, slot)
    assert (status, body["code"]) == (409, "opt_in_pending")
    assert mate_tool_access.entry(ORDINARY)["state"] == mate_tool_access.STATE_OFFERED
    assert len(seen) == 1


def test_card_status_tells_a_gone_card_from_an_unreadable_store(monkeypatch):
    from kiro_crew.dashboard import change_cards

    class Store:
        def __init__(self, error):
            self.error = error

        async def warm(self):
            return None

        def get(self, _card_id):
            raise self.error

    request = type("R", (), {"app": {"state": object()}})()
    for error, want in (
        (change_cards.CardError(404, "not_found", "no such card"), self_routes._CARD_GONE),
        (change_cards.CardError(503, "store_unavailable", "later"), None),
    ):
        monkeypatch.setattr(change_cards, "card_store_for", lambda _s, e=error: Store(e))
        assert asyncio.run(self_routes._card_status(request, "c00000001")) == want


# ── the card: one field, this crewmate, the user's words ──


def test_the_card_writes_only_this_crewmates_flag():
    params = catalog.validate_params(
        catalog.KIND_CREWMATE_UPDATE,
        {"name": ORDINARY, "fields": {"dynamic_capabilities": True}},
    )
    snapshot = {"exists": True, "member_id": "scout", "fields": {"dynamic_capabilities": False}}
    out = catalog.build_preview(catalog.KIND_CREWMATE_UPDATE, params, snapshot, {"label": "Scout"})
    assert out["title"] == (
        "Allow Scout to ask you for tools its worker needs? Each one still needs your approval."
    )
    assert [s["path"] for s in out["apply"]] == [f"/api/agents/{ORDINARY}"]
    assert out["apply"][0]["body"] == {"dynamic_capabilities": True}
    assert out["scope"] == {"members": ["Scout"], "count": 1, "only_this_member": True}


def test_the_flag_is_a_strict_bool_on_the_card():
    with pytest.raises(catalog.CardCatalogError):
        catalog.validate_params(
            catalog.KIND_CREWMATE_UPDATE,
            {"name": ORDINARY, "fields": {"dynamic_capabilities": "true"}},
        )


def test_the_off_prompts_ask_with_request_capabilities_as_before_the_preview():
    for key in ("worker_report.missing_capability_manual", "conductor.mate_tools_identity_manual"):
        text = crewmate_prompts.text(key)
        assert "request_capabilities" in text and "find_capabilities" not in text
        assert "ask_tool_access" not in text
