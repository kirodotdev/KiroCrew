"""Guide ownership and server-only mutation completion, with no live gateway."""

import dataclasses

import pytest

from kiro_crew import guide_catalog
from kiro_crew.dashboard.guide_runs import GuideError, GuideStore


@pytest.fixture
def rig(monkeypatch):
    # The store walks whatever steps the catalog declares. The shipped
    # ``crewmate.create`` is a single commit step; this rig gives it two UI
    # steps ahead of the commit so moves around a commit (forward into it,
    # back out of it, never across a pending save) stay covered.
    action = guide_catalog.ACTIONS["crewmate.create"]
    monkeypatch.setitem(
        guide_catalog.ACTIONS,
        "crewmate.create",
        dataclasses.replace(
            action,
            steps=(
                guide_catalog.StepDef("first", guide_catalog.STEP_UI),
                guide_catalog.StepDef("second", guide_catalog.STEP_UI),
                *action.steps,
            ),
        ),
    )
    now = [1000.0]
    store = GuideStore(clock=lambda: now[0])
    guide = store.start(
        slot_key="chat-fixture",
        session_key="dashboard:chat-fixture",
        actions=[{"id": "crewmate.create", "params": {"name": "Scout"}}],
    )
    return store, guide, now


def claim(store, guide, tab="tab-one", **extra):
    return store.claim(guide_id=guide["guide_id"], tab_id=tab, revision=guide["revision"], **extra)


def progress(store, guide, **overrides):
    args = dict(
        guide_id=guide["guide_id"],
        tab_id=guide["owner_tab"],
        revision=guide["revision"],
        action_index=guide["action_index"],
        step_index=guide["step_index"],
        outcome="observed",
    )
    return store.progress(**(args | overrides))


def at_commit(store, guide):
    guide = claim(store, guide)
    return progress(store, progress(store, guide))


def begin(store, guide):
    return store.begin_commit(
        guide_id=guide["guide_id"],
        tab_id=guide["owner_tab"],
        revision=str(guide["revision"]),
        kind="crewmate.create",
    )


def test_a_new_offer_supersedes_the_unfinished_one_after_it_validates(rig):
    store, guide, _ = rig
    assert guide["status"] == "offered"
    assert guide["owner_tab"] is None
    # An invalid offer is refused and leaves the current guide alone.
    with pytest.raises(GuideError):
        store.start(slot_key="chat-fixture", session_key="dashboard:chat-fixture", actions=[])
    assert store.pending()[0]["guide_id"] == guide["guide_id"]
    assert store.pending()[0]["status"] == "offered"

    # A claimed (active) guide is replaced too: the agent never cancels first.
    active = claim(store, guide)
    new, superseded = store.start_superseding(
        slot_key="chat-fixture",
        session_key="dashboard:chat-fixture",
        actions=[{"id": "crewmate.create", "params": {"name": "Scout"}}],
    )
    (old,) = superseded
    assert old["guide_id"] == active["guide_id"]
    assert (old["status"], old["reason"]) == ("cancelled", "superseded")
    assert old["revision"] > active["revision"]
    assert [g["guide_id"] for g in store.pending()] == [new["guide_id"]]
    # The superseded guide's tab cannot move it.
    with pytest.raises(GuideError):
        progress(store, active)


def test_a_new_offer_never_supersedes_another_conversations_guide(rig):
    store, guide, _ = rig
    _, superseded = store.start_superseding(
        slot_key="chat-other",
        session_key="dashboard:chat-other",
        actions=[{"id": "crewmate.create", "params": {"name": "Scout"}}],
    )
    assert superseded == []
    assert store.status_for_caller(slot_key="chat-fixture")["status"] == "offered"


def test_a_guide_mid_save_is_not_superseded(rig):
    store, guide, _ = rig
    waiting = at_commit(store, guide)
    assert begin(store, waiting)
    with pytest.raises(GuideError) as exc:
        store.start(
            slot_key="chat-fixture",
            session_key="dashboard:chat-fixture",
            actions=[{"id": "crewmate.create", "params": {"name": "Scout"}}],
        )
    assert exc.value.code == "guide_saving"
    assert store.status_for_caller(slot_key="chat-fixture")["guide_id"] == guide["guide_id"]


def test_tab_claim_is_compare_and_set_and_requires_explicit_takeover(rig):
    store, guide, _ = rig
    active = claim(store, guide)
    with pytest.raises(GuideError):
        claim(store, guide, "tab-two")
    with pytest.raises(GuideError):
        claim(store, active, "tab-two")
    owned = claim(store, active, "tab-two", take_over=True)
    with pytest.raises(GuideError):
        progress(store, owned, tab_id="tab-one")
    assert owned["owner_tab"] == "tab-two"


def test_browser_observation_cannot_complete_a_mutation(rig):
    store, guide, _ = rig
    waiting = at_commit(store, guide)
    with pytest.raises(GuideError) as exc:
        progress(store, waiting)
    assert exc.value.code == "commit_step_requires_server_evidence"
    assert store.pending()[0]["step_index"] == 2


def test_only_an_associated_commit_advances_and_reports_actual_identity(rig):
    store, guide, _ = rig
    waiting = at_commit(store, guide)
    assert store.finish_commit("unknown-token", {"member_id": "wrong"}) is None
    token = begin(store, waiting)
    assert token
    assert begin(store, waiting) is None
    result = store.finish_commit(token, {"member_id": "immutable-fixture-id"})
    assert result["status"] == "completed"
    assert result["actions"][0]["result"]["member_id"] == "immutable-fixture-id"
    assert store.finish_commit(token, {"member_id": "other"}) is None


@pytest.mark.parametrize("retire", ["cancel", "expire"])
def test_late_commit_cannot_revive_a_retired_guide(rig, retire):
    store, guide, now = rig
    waiting = at_commit(store, guide)
    token = begin(store, waiting)
    if retire == "cancel":
        store.cancel_by_tab(
            guide_id=waiting["guide_id"], tab_id="tab-one", revision=waiting["revision"]
        )
    else:
        now[0] = waiting["expires_at"] + 1
    assert store.finish_commit(token, {"member_id": "late"}) is None
    current = store.status_for_caller(slot_key="chat-fixture", guide_id=guide["guide_id"])
    assert current["status"] == ("cancelled" if retire == "cancel" else "expired")


def test_lease_expiry_requires_fresh_claim(rig):
    store, guide, now = rig
    active = claim(store, guide)
    now[0] = active["lease_expires_at"] + 1
    pending = store.pending()[0]
    assert pending["status"] == "offered"
    assert pending["owner_tab"] is None
    with pytest.raises(GuideError):
        progress(store, active)
    assert claim(store, pending, "tab-two")["owner_tab"] == "tab-two"


def test_foreign_slot_cannot_read_or_cancel(rig):
    store, guide, _ = rig
    for operation in (store.status_for_caller, store.cancel_by_caller):
        with pytest.raises(GuideError) as exc:
            operation(slot_key="foreign", guide_id=guide["guide_id"])
        assert exc.value.status == 404


def test_closed_slot_is_retired_and_a_late_save_cannot_complete_it(rig):
    store, guide, _ = rig
    waiting = at_commit(store, guide)
    token = begin(store, waiting)
    assert store.retire_closed_slots(lambda slot: slot == "chat-fixture") == []
    retired = store.retire_closed_slots(lambda _slot: False)
    assert retired[0]["status"] == "cancelled"
    assert retired[0]["reason"] == "slot_closed"
    assert store.pending() == []
    assert store.finish_commit(token, {"member_id": "late"}) is None


def _cancel(store, guide):
    return store.cancel_by_tab(
        guide_id=guide["guide_id"], tab_id="tab-one", revision=guide["revision"]
    )


def test_an_ended_guide_stays_in_pending_for_its_chat_result_line(rig):
    """A reload reads the result line back: the ended guide is still served."""
    store, guide, now = rig
    ended = _cancel(store, guide)
    assert ended["finished_at"] == now[0] and ended["dismissed"] is False
    assert [(g["guide_id"], g["status"]) for g in store.pending("chat-fixture")] == [
        (guide["guide_id"], "cancelled")
    ]
    # Shown for the change card's 24h window, kept (readable by status) for 7d.
    now[0] += 24 * 60 * 60
    assert store.pending() == []
    assert store.status_for_caller(slot_key="chat-fixture")["status"] == "cancelled"
    now[0] += 6 * 24 * 60 * 60
    store.sweep()
    assert store._guides == {}


def test_only_the_newest_ended_guide_per_slot_is_served(rig):
    store, guide, now = rig
    _cancel(store, guide)
    now[0] += 1
    second = store.start(
        slot_key="chat-fixture",
        session_key="dashboard:chat-fixture",
        actions=[{"id": "crewmate.create", "params": {"name": "Scout"}}],
    )
    assert {g["guide_id"] for g in store.pending()} == {second["guide_id"]}
    now[0] += 1
    _cancel(store, second)
    assert [g["guide_id"] for g in store.pending()] == [second["guide_id"]]


def test_dismiss_hides_an_ended_guide_and_refuses_a_live_one(rig):
    store, guide, _ = rig
    with pytest.raises(GuideError) as exc:
        store.dismiss(guide_id=guide["guide_id"])
    assert exc.value.status == 409
    ended = _cancel(store, guide)
    dismissed = store.dismiss(guide_id=guide["guide_id"])
    assert dismissed["dismissed"] is True and dismissed["revision"] > ended["revision"]
    assert store.pending() == []
    # Idempotent: a second dismiss from another tab is not a new revision.
    assert store.dismiss(guide_id=guide["guide_id"])["revision"] == dismissed["revision"]


def _show_me(store):
    return store.start(
        slot_key="chat-show",
        session_key="dashboard:chat-show",
        actions=[{"id": "settings.show", "params": {"setting_id": "chat.link-previews"}}],
    )


def test_a_completed_show_me_guide_can_be_walked_again():
    now = [1000.0]
    store = GuideStore(clock=lambda: now[0])
    done = progress(store, claim(store, _show_me(store)))
    assert done["status"] == "completed"
    again = store.replay(guide_id=done["guide_id"], revision=done["revision"])
    assert again["status"] == "offered"
    assert (again["action_index"], again["step_index"]) == (0, 0)
    assert again["finished_at"] is None and again["revision"] > done["revision"]
    assert claim(store, again)["status"] == "active"


def test_replay_refuses_a_live_guide_and_one_that_saved_a_change(rig):
    store, guide, _ = rig
    with pytest.raises(GuideError, match="only a finished guide"):
        store.replay(guide_id=guide["guide_id"], revision=guide["revision"])
    g = at_commit(store, guide)
    token = begin(store, g)
    finished = store.finish_commit(token, {"member_id": "m1", "name": "Scout"})
    assert finished is not None and finished["status"] == "completed"
    with pytest.raises(GuideError) as exc:
        store.replay(guide_id=finished["guide_id"], revision=finished["revision"])
    assert exc.value.code == "guide_not_replayable"


def test_a_missing_target_found_again_resumes_the_same_step(rig):
    store, guide, _ = rig
    active = progress(store, claim(store, guide))
    assert active["step_index"] == 1
    missing = progress(store, active, outcome="target_missing")
    assert missing["status"] == "target_missing"
    found = progress(store, missing, outcome="target_found")
    # Back on the same step, not past it: recovery is not progress.
    assert (found["status"], found["step_index"], found["reason"]) == ("active", 1, "target_found")
    assert found["revision"] == missing["revision"] + 1
    # A repeat on the now-active guide is answered as is, without a bump that
    # would make the owner's next write stale.
    again = progress(store, found, outcome="target_found")
    assert again["revision"] == found["revision"] and again["status"] == "active"
    # And it can go missing again, and recover again.
    assert progress(store, again, outcome="target_missing")["status"] == "target_missing"


def test_target_found_follows_a_restarted_form_back_never_forward(rig):
    store, guide, _ = rig
    missing = progress(store, at_commit(store, guide), outcome="target_missing")
    assert missing["step_index"] == 2
    with pytest.raises(GuideError) as exc:
        progress(store, missing, outcome="target_found", resume_step_index=3)
    assert exc.value.code == "invalid_resume_step"
    back = progress(store, missing, outcome="target_found", resume_step_index=0)
    assert (back["status"], back["action_index"], back["step_index"]) == ("active", 0, 0)
    # Ignored unless the guide is missing: an active guide never moves back.
    same = progress(store, back, outcome="target_found", resume_step_index=0)
    assert same["revision"] == back["revision"]


def test_target_found_never_moves_back_across_a_pending_save(rig):
    store, guide, _ = rig
    g = at_commit(store, guide)
    begin(store, g)
    current = store.status_for_caller(slot_key="chat-fixture")
    missing = progress(store, current, outcome="target_missing")
    with pytest.raises(GuideError) as exc:
        progress(store, missing, outcome="target_found", resume_step_index=0)
    assert exc.value.code == "invalid_resume_step"


def test_target_found_needs_the_owner_tab_and_never_revives_an_ended_guide(rig):
    store, guide, _ = rig
    missing = progress(store, claim(store, guide), outcome="target_missing")
    with pytest.raises(GuideError) as exc:
        progress(store, missing, outcome="target_found", tab_id="tab-two")
    assert exc.value.code == "not_owner_tab"
    with pytest.raises(GuideError) as exc:
        progress(store, missing, outcome="target_found", step_index=1)
    assert exc.value.code == "wrong_step"
    ended = _cancel(store, missing)
    assert ended["status"] == "cancelled"
    with pytest.raises(GuideError) as exc:
        progress(store, ended | {"owner_tab": "tab-one"}, outcome="target_found")
    assert exc.value.code == "guide_finished"
    assert store.status_for_caller(slot_key="chat-fixture")["status"] == "cancelled"


def test_target_found_on_a_commit_step_resumes_without_completing_it(rig):
    store, guide, _ = rig
    waiting = at_commit(store, guide)
    found = progress(
        store, progress(store, waiting, outcome="target_missing"), outcome="target_found"
    )
    assert (found["status"], found["step_index"]) == ("active", 2)


def test_a_tab_may_end_a_guide_saying_its_save_went_through_without_it(rig):
    store, guide, _ = rig
    g = claim(store, guide)
    ended = store.cancel_by_tab(
        guide_id=g["guide_id"],
        tab_id="tab-one",
        revision=g["revision"],
        reason="saved_without_guide",
    )
    assert (ended["status"], ended["reason"]) == ("cancelled", "saved_without_guide")


def test_a_cancel_reason_outside_the_allowlist_is_refused(rig):
    store, guide, _ = rig
    g = claim(store, guide)
    with pytest.raises(GuideError) as exc:
        store.cancel_by_tab(
            guide_id=g["guide_id"], tab_id="tab-one", revision=g["revision"], reason="completed"
        )
    assert exc.value.code == "invalid_reason"
    assert _cancel(store, g)["reason"] == "cancelled_by_user"
