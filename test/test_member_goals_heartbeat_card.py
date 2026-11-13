"""The Heartbeat checkbox on a crewmate's goal card: one press sets the goal and its Heartbeat.

What these tests pin:

* while the Heartbeat is off and was never declined, a ``crewmate.goal`` card
  carries a Heartbeat row, checked by default, naming how often it runs;
* applying it checked sets the goal AND turns the Heartbeat on, through the same
  hooked schedules route a ``crewmate.check_ins`` card writes, and its Undo turns
  the Heartbeat off before it takes the goal back;
* applying it unchecked sets only the goal and remembers the decline, so no
  later goal card offers the Heartbeat again;
* with the Heartbeat already on the card carries no checkbox at all;
* a goal card stored before the checkbox existed still rebuilds unchanged;
* either answer stops the goal-setup turn from asking about the Heartbeat in chat.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from member_goals_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    ORDINARY,
    _owner,
    _owner_ok,
    _quiet_sel,
    _reminder_state,
    _schedules,
    _seed,
    _slug,
)
from test_member_goals_cards import _CardState, _propose_card, _run_card_plan

from kiro_crew import change_card_catalog as catalog
from kiro_crew import member_goals
from kiro_crew.dashboard import goal_setup

KIND = catalog.KIND_CREWMATE_GOAL


@pytest.fixture
def _card_owner(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _offer_row(rec: dict[str, Any]) -> dict[str, Any] | None:
    rows = [c for c in rec["changes"] if c.get("field") == "heartbeat_offer"]
    assert len(rows) <= 1
    return rows[0] if rows else None


def _record(slug: str) -> dict[str, Any]:
    return member_goals.read_goals(slug, ORDINARY)


def test_a_checked_goal_card_sets_the_goal_and_turns_the_heartbeat_on(
    tmp_path: Path, _card_owner
) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _CardState(tmp_path)
    rec = _propose_card(state, KIND, {"member": ORDINARY, "long_term": "Ship the docs"})
    row = _offer_row(rec)
    assert row is not None
    # Checked by default, at the mate's own interval (the record's default, 60).
    assert (row["after"], row["interval"], row["per_day"]) == (True, 60, 24)
    assert "credits" in row["label"]
    goal_step, beat_step = rec["plan"]["apply"]
    assert goal_step["body"]["heartbeat_offer"] == "accepted"
    assert beat_step["path"] == f"/api/members/{slug}/goals/schedules"
    assert beat_step["body"] == {
        "member": ORDINARY,
        "heartbeat": True,
        "expected": {"heartbeat": False, "interval": 60},
    }

    assert _run_card_plan(state, rec, "apply") == [200, 200]
    stored = state._change_card_store.get(rec["id"])
    assert stored["status"] == "applied", stored.get("error")
    after = _record(slug)
    assert [g["text"] for g in after["goals"]] == ["Ship the docs"]
    assert after["schedules"]["heartbeat"] is True
    assert after["heartbeat_offer"] == "accepted"
    assert len(_schedules(state, slug, "heartbeat")) == 1

    # Undo: the Heartbeat off first, while the goal it needs is still there.
    beat_undo, goal_undo = stored["plan"]["undo"]
    assert beat_undo["body"] == {
        "member": ORDINARY,
        "heartbeat": False,
        "expected": {"heartbeat": True},
    }
    assert goal_undo["path"] == f"/api/members/{slug}/goals/long-term"
    assert _run_card_plan(state, stored, "undo") == [200, 200]
    undone = _record(slug)
    assert undone["goals"] == []
    assert undone["schedules"]["heartbeat"] is False
    assert _schedules(state, slug, "heartbeat") == []


def test_undo_still_takes_the_goal_back_after_the_heartbeat_was_turned_off_by_hand(
    tmp_path: Path, _card_owner
) -> None:
    """The Heartbeat step's target already holds: it succeeds and the goal Undo runs."""
    _seed()
    slug = _slug(ORDINARY)
    state = _CardState(tmp_path)
    rec = _propose_card(state, KIND, {"member": ORDINARY, "long_term": "Ship the docs"})
    assert _run_card_plan(state, rec, "apply") == [200, 200]
    member_goals.set_schedules(slug, ORDINARY, {"heartbeat": False})
    stored = state._change_card_store.get(rec["id"])
    assert _run_card_plan(state, stored, "undo") == [200, 200]
    after = _record(slug)
    assert after["goals"] == []
    assert after["schedules"]["heartbeat"] is False
    # Outside a goal card's Undo a write that already holds is still a conflict.
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    member_goals.set_schedules(slug, ORDINARY, {"heartbeat": True})
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.set_schedules(
            slug, ORDINARY, {"heartbeat": True}, expected={"heartbeat": False}
        )
    assert exc.value.code == "changed_meanwhile"


def test_apply_never_claims_a_heartbeat_the_user_turned_on_before_it(
    tmp_path: Path, _card_owner
) -> None:
    """Turned on by hand between preview and Apply: the card's step is refused, so its
    Undo has no Heartbeat-off step and leaves the user's Heartbeat on."""
    _seed()
    slug = _slug(ORDINARY)
    state = _CardState(tmp_path)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    rec = _propose_card(state, KIND, {"member": ORDINARY, "long_term": "Hire two people"})
    assert len(rec["plan"]["apply"]) == 2
    member_goals.set_schedules(slug, ORDINARY, {"heartbeat": True})
    assert _run_card_plan(state, rec, "apply") == [200, 409]
    stored = state._change_card_store.get(rec["id"])
    undo = (stored.get("plan") or {}).get("undo") or []
    assert [s["path"] for s in undo] == [f"/api/members/{slug}/goals/long-term"]
    assert _run_card_plan(state, stored, "undo") == [200]
    after = _record(slug)
    assert [g["text"] for g in after["goals"]] == ["Ship"]
    assert after["schedules"]["heartbeat"] is True


def test_an_unchecked_goal_card_sets_only_the_goal_and_is_not_offered_again(
    tmp_path: Path, _card_owner
) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _CardState(tmp_path)
    rec = _propose_card(
        state, KIND, {"member": ORDINARY, "long_term": "Ship the docs", "heartbeat": False}
    )
    row = _offer_row(rec)
    assert row is not None and row["after"] is False
    (goal_step,) = rec["plan"]["apply"]
    assert goal_step["body"]["heartbeat_offer"] == "declined"
    assert _run_card_plan(state, rec, "apply") == [200]
    after = _record(slug)
    assert [g["text"] for g in after["goals"]] == ["Ship the docs"]
    assert after["schedules"]["heartbeat"] is False
    assert after["heartbeat_offer"] == "declined"
    assert _schedules(state, slug, "heartbeat") == []

    # The decline is remembered: a later goal card offers nothing, whatever it asks.
    later = _propose_card(
        state, KIND, {"member": ORDINARY, "long_term": "Hire two people", "heartbeat": True}
    )
    assert _offer_row(later) is None
    (only,) = later["plan"]["apply"]
    assert "heartbeat_offer" not in only["body"]


def test_no_checkbox_while_the_heartbeat_is_already_on(tmp_path: Path, _card_owner) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _CardState(tmp_path)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    member_goals.set_schedules(slug, ORDINARY, {"heartbeat": True, "interval": 120})
    rec = _propose_card(state, KIND, {"member": ORDINARY, "long_term": "Hire two people"})
    assert _offer_row(rec) is None
    (only,) = rec["plan"]["apply"]
    assert "heartbeat_offer" not in only["body"]


def test_the_checkbox_names_the_mates_own_interval(tmp_path: Path, _card_owner) -> None:
    """A Heartbeat that was on at 4 hr and turned off comes back at 4 hr, and says so."""
    _seed()
    slug = _slug(ORDINARY)
    state = _CardState(tmp_path)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    member_goals.set_schedules(slug, ORDINARY, {"heartbeat": True, "interval": 240})
    member_goals.set_schedules(slug, ORDINARY, {"heartbeat": False})
    rec = _propose_card(state, KIND, {"member": ORDINARY, "long_term": "Hire two people"})
    row = _offer_row(rec)
    assert row is not None and (row["interval"], row["per_day"]) == (240, 6)
    assert rec["plan"]["apply"][1]["body"]["expected"] == {"heartbeat": False, "interval": 240}


def test_the_checkbox_is_an_editable_field_of_the_card() -> None:
    old = {"member": ORDINARY, "long_term": "Ship"}
    catalog.check_editable(KIND, old, {**old, "heartbeat": False})
    with pytest.raises(catalog.CardCatalogError):
        catalog.check_editable(KIND, old, {**old, "member": "someone-else"})
    with pytest.raises(catalog.CardCatalogError):
        catalog.validate_params(KIND, {**old, "heartbeat": "yes"})


def test_a_goal_card_stored_before_the_checkbox_still_rebuilds(tmp_path: Path, _card_owner) -> None:
    """Its snapshot has no ``heartbeat`` and its editable list is the old one."""
    from kiro_crew.dashboard.change_cards_records import rebuild_record

    _seed()
    state = _CardState(tmp_path)
    rec = _propose_card(state, KIND, {"member": ORDINARY, "long_term": "Ship"})
    legacy = {
        **rec,
        "before": {k: v for k, v in rec["before"].items() if k != "heartbeat"},
        "editable": ["long_term"],
        "changes": [c for c in rec["changes"] if c.get("field") != "heartbeat_offer"],
        "plan": {**rec["plan"], "apply": rec["plan"]["apply"][:1]},
    }
    legacy["plan"]["apply"][0] = {
        **legacy["plan"]["apply"][0],
        "body": {
            k: v for k, v in legacy["plan"]["apply"][0]["body"].items() if k != "heartbeat_offer"
        },
    }
    rebuilt = rebuild_record(legacy)
    assert rebuilt is not None
    assert len(rebuilt["plan"]["apply"]) == 1


@pytest.mark.parametrize("answer", ["accepted", "declined"])
def test_an_answered_checkbox_keeps_the_goal_setup_turn_from_asking(answer: str) -> None:
    goal = {"id": "g0000000a", "text": "Ship"}
    rec = {"goals": [goal], "key_results": [], "schedules": {}, "heartbeat_offer": answer}
    text = goal_setup.kickoff_prompt(rec, goal["id"], has_mate_tools=True)
    assert text is not None
    assert "Heartbeat" not in text and "set_check_ins" not in text
    assert "Ask nothing else yet." in text
    asked = goal_setup.kickoff_prompt({**rec, "heartbeat_offer": None}, goal["id"])
    assert asked is not None and "set_check_ins" in asked


def test_the_goal_route_refuses_an_unknown_checkbox_answer(tmp_path: Path) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    path = f"/api/members/{slug}/goals/long-term"
    status, body = _owner(
        state, "PUT", path, {"member": ORDINARY, "long_term": "Ship", "heartbeat_offer": "maybe"}
    )
    assert (status, body["code"]) == (400, "invalid_heartbeat_offer")
    assert _record(slug)["goals"] == []
    status, _ = _owner(
        state,
        "PUT",
        path,
        {"member": ORDINARY, "long_term": "Ship", "heartbeat_offer": "declined"},
    )
    assert status == 200
    assert _record(slug)["heartbeat_offer"] == "declined"


def test_a_record_keeps_only_a_known_answer() -> None:
    assert member_goals.empty_record()["heartbeat_offer"] is None
    assert member_goals._normalize({"heartbeat_offer": "declined"})["heartbeat_offer"] == "declined"
    assert member_goals._normalize({"heartbeat_offer": "sure"})["heartbeat_offer"] is None
