"""Guide ownership and server-only mutation completion, with no live gateway."""

import pytest

from kiro_crew.dashboard.guide_runs import GuideError, GuideStore


@pytest.fixture
def rig():
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


def test_offer_is_not_active_and_start_never_replaces_work(rig):
    store, guide, _ = rig
    assert guide["status"] == "offered"
    assert guide["owner_tab"] is None
    with pytest.raises(GuideError, match="already has a guide"):
        store.start(slot_key="chat-fixture", session_key="dashboard:chat-fixture", actions=[])
    assert store.pending()[0]["guide_id"] == guide["guide_id"]


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
