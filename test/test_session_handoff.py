"""``session_handoff``: moving who may run a session, and nothing else with it.

Driven against real slots and the real gate (:func:`authorize_target`), with the
managers file in a temporary data home. The crew log is off, so the tree edge is not
moved here; that move is ``session_adopt``'s own append, tested with that verb.
"""

from __future__ import annotations

import asyncio

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- autouse fixtures apply by import
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _slot,
)

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import session_managers

#: Spelled out rather than read from the production set, so a change to it fails here.
WIDENED = ("send", "read", "summary", "stop", "close", "adopt", "release", "handoff")
#: Verbs that stay with the creator after a handoff.
CREATOR_ONLY = ("fork", "rename", "set_model")


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crewhome"))
    _floor_monkeypatch.setattr(sc, "manager_chain_enabled", lambda: False)
    asyncio.run(sc.refresh_manager_chain())
    session_managers.reset_cache()
    yield
    session_managers.reset_cache()


def _team(state):
    """A person's tab, a lead it started, and a conductor and worker the lead opened.

    The lead is agent-created, so it is fenced: the caller class the fence is about.
    """
    owner = _slot(state, "chat-owner")
    lead = _slot(state, "chat-lead")
    conductor = _slot(state, "chat-conductor")
    worker = _slot(state, "chat-worker")
    lead._created_by = owner.key
    conductor._created_by = lead.key
    worker._created_by = lead.key
    return lead, conductor, worker


def _chain_on(monkeypatch) -> None:
    """Turn the chain switch on and publish it, as the off-loop prewarm does."""
    monkeypatch.setattr(sc, "manager_chain_enabled", lambda: True)
    asyncio.run(sc.refresh_manager_chain())


def _reaches(state, caller, target, operation: str) -> bool:
    try:
        sc.authorize_target(
            state, caller_session_key=_key(caller), target=target.key, operation=operation
        )
    except sc.SessionControlError as exc:
        assert exc.code == "not_creator", exc.code
        return False
    return True


def _hand(state, caller, target, to):
    return asyncio.run(
        sc.handoff_target(state, caller_session_key=_key(caller), target=target.key, to=to.key)
    )


def _notices(slot) -> list[str]:
    return [m["content"] for m in slot.messages if m.get("role") == "notice"]


# ── nothing handed over ──────────────────────────────────────────────────────


def test_a_session_nobody_handed_over_is_run_by_its_creator(tmp_path):
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    assert session_managers.holder(worker) == lead.key
    for operation in WIDENED + CREATOR_ONLY:
        assert _reaches(state, lead, worker, operation), operation
    for operation in WIDENED:
        assert not _reaches(state, conductor, worker, operation), operation


# ── a handoff moves control ─────────────────────────────────────────────────


def test_a_handoff_moves_control_to_the_new_manager_and_away_from_the_giver(tmp_path):
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)

    result = _hand(state, lead, worker, conductor)

    assert result["manager"] == conductor.key
    assert result["previous_manager"] == lead.key
    for operation in WIDENED:
        assert _reaches(state, conductor, worker, operation), operation
        assert not _reaches(state, lead, worker, operation), operation


def test_created_by_does_not_move_so_fork_rename_and_model_stay_with_the_creator(tmp_path):
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)

    _hand(state, lead, worker, conductor)

    assert worker._created_by == lead.key
    for operation in CREATOR_ONLY:
        assert _reaches(state, lead, worker, operation), operation
        assert not _reaches(state, conductor, worker, operation), operation


def test_the_transcript_gets_one_note_per_handoff(tmp_path):
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)

    _hand(state, lead, worker, conductor)

    notes = _notices(worker)
    assert notes == [
        f"This session was handed over: {conductor.key} manages it now, in place of {lead.key}."
    ]


def test_a_refused_handoff_writes_no_note(tmp_path):
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hand(state, lead, worker, conductor)

    with pytest.raises(sc.SessionControlError):
        _hand(state, lead, worker, conductor)

    assert len(_notices(worker)) == 1


def test_a_handoff_moves_the_target_under_the_new_manager_in_the_tree(tmp_path, monkeypatch):
    from kiro_crew.crew_log import emit

    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    edges: list[dict] = []

    def _adopted(sid, *, on_settled, **kwargs):
        edges.append({"sid": sid, **kwargs})
        on_settled(True)

    async def _sid(_state, key):
        return f"sid-{key}"

    monkeypatch.setattr(emit, "enabled", lambda: True)
    monkeypatch.setattr(emit, "on_session_adopted", _adopted)
    monkeypatch.setattr(sc, "_live_sid_of", lambda _state, key: f"sid-{key}")
    monkeypatch.setattr(sc, "_slot_tree_parent", lambda key: (True, lead.key, {}))
    monkeypatch.setattr(sc, "_recorded_sid_of", _sid)

    result = _hand(state, lead, worker, conductor)

    assert result["tree_moved"] is True
    assert edges == [
        {
            "sid": f"sid-{worker.key}",
            "slot": worker.key,
            "parent_slot": conductor.key,
            "parent_sid": f"sid-{conductor.key}",
            "previous_parent_slot": lead.key,
            "previous_parent_sid": f"sid-{lead.key}",
        }
    ]


def test_a_handoff_that_cannot_be_saved_changes_nothing(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)

    def _fail(_changes):
        raise OSError("disk full")

    monkeypatch.setattr(session_managers, "write", _fail)
    with pytest.raises(sc.SessionControlError) as refused:
        _hand(state, lead, worker, conductor)

    assert refused.value.code == "handoff_write_failed"
    assert session_managers.holder(worker) == lead.key
    assert _notices(worker) == []


# ── who may hand over, and to whom ──────────────────────────────────────────


def test_only_the_current_manager_can_hand_a_session_on(tmp_path):
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    helper = _slot(state, "chat-helper")
    helper._created_by = conductor.key
    _hand(state, lead, worker, conductor)

    with pytest.raises(sc.SessionControlError) as refused:
        _hand(state, lead, worker, lead)
    assert refused.value.code == "not_creator"

    _hand(state, conductor, worker, helper)
    assert session_managers.holder(worker) == helper.key
    assert not _reaches(state, conductor, worker, "send")


def test_the_receiver_must_be_a_session_the_giver_manages(tmp_path):
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    stranger = _slot(state, "chat-stranger")
    stranger._created_by = "chat-someone-else"

    with pytest.raises(sc.SessionControlError) as refused:
        _hand(state, lead, worker, stranger)

    assert refused.value.code == "not_creator"
    assert session_managers.holder(worker) == lead.key
    assert session_managers.load() == {}


def test_an_agent_in_a_persons_tab_cannot_hand_over_another_persons_tab(tmp_path):
    """The person's own tabs have no manager. An unfenced agent in one of them must not
    be able to give another of them to a fenced session."""
    state = _make_state(tmp_path)
    lead, conductor, _worker = _team(state)
    owner = state.get_slot("chat-owner")
    other_tab = _slot(state, "chat-other-tab")
    mine = _slot(state, "chat-mine")
    mine._created_by = owner.key

    with pytest.raises(sc.SessionControlError) as refused:
        _hand(state, owner, other_tab, conductor)
    assert refused.value.code == "not_manager"
    assert not _reaches(state, conductor, other_tab, "send")

    # The person's tab still hands over what it created, to what it created.
    _hand(state, owner, mine, lead)
    assert _reaches(state, lead, mine, "send")


@pytest.mark.parametrize(
    ("to", "code"),
    [("worker", "self_manager"), ("lead", "already_manager"), ("child", "would_cycle")],
)
def test_a_handoff_that_changes_nothing_or_makes_a_loop_is_refused(tmp_path, monkeypatch, to, code):
    # The chain lets the lead name a session the worker manages, the one way to reach
    # a loop: handing the worker to it would put the worker above itself.
    _chain_on(monkeypatch)
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    child = _slot(state, "chat-child")
    child._created_by = lead.key
    _hand(state, lead, child, worker)
    receiver = {"worker": worker, "lead": lead, "child": child}[to]

    with pytest.raises(sc.SessionControlError) as refused:
        asyncio.run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=receiver.key
            )
        )

    assert refused.value.code == code
    assert session_managers.holder(worker) == lead.key


# ── the record survives a restart, and a bad one fails closed ───────────────


def test_a_handoff_survives_a_restart(tmp_path):
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hand(state, lead, worker, conductor)

    session_managers.reset_cache()

    assert session_managers.holder(worker) == conductor.key
    assert _reaches(state, conductor, worker, "send")
    assert not _reaches(state, lead, worker, "send")


def test_an_unreadable_record_file_falls_back_to_the_creator(tmp_path):
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hand(state, lead, worker, conductor)
    path = session_managers._path()
    path.write_text("{not json", "utf-8")
    session_managers.reset_cache()

    assert session_managers.holder(worker) == lead.key
    assert _reaches(state, lead, worker, "send")
    assert not _reaches(state, conductor, worker, "send")


@pytest.mark.parametrize(
    "record",
    [{"manager": "chat-conductor", "tab": "another-tab"}, {"manager": ""}, "chat-conductor"],
    ids=["other-tab", "no-manager", "not-an-object"],
)
def test_a_record_that_does_not_match_this_session_withholds_reach(tmp_path, record):
    """A record that names this key but cannot be trusted for this session decides for
    nobody. It must not fall back to the creator: that is how a giver who edits its
    tab id on the transcript line would win the session back."""
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    session_managers.write({worker.key: ("chat-conductor", worker._tab_id)})
    session_managers._cache[1][worker.key] = record

    assert session_managers.holder(worker) == ""
    assert not _reaches(state, lead, worker, "send")
    assert not _reaches(state, conductor, worker, "send")


def test_editing_the_tab_id_after_a_handoff_does_not_give_the_session_back(tmp_path):
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hand(state, lead, worker, conductor)

    worker._tab_id = "edited-by-the-giver"
    session_managers.reset_cache()

    assert not _reaches(state, lead, worker, "send")
    assert not _reaches(state, conductor, worker, "send")


# ── the manager chain switch ────────────────────────────────────────────────


def test_with_the_chain_off_the_lead_does_not_reach_a_worker_under_its_conductor(tmp_path):
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hand(state, lead, worker, conductor)

    assert not _reaches(state, lead, worker, "send")
    assert _reaches(state, lead, conductor, "send")


def test_with_the_chain_on_the_lead_reaches_the_line_under_it(tmp_path, monkeypatch):
    _chain_on(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hand(state, lead, worker, conductor)

    for operation in WIDENED:
        assert _reaches(state, lead, worker, operation), operation
        assert _reaches(state, conductor, worker, operation), operation
    # The chain is about running sessions; it widens nothing else.
    assert not _reaches(state, conductor, worker, "fork")
    stranger = _slot(state, "chat-stranger")
    stranger._created_by = "chat-someone-else"
    assert not _reaches(state, lead, stranger, "send")


def test_the_synchronous_close_recheck_never_loads_config(tmp_path, monkeypatch):
    """``close_target``'s ``pre_pop_check`` re-runs the gate with no suspension, so
    the chain switch must come from the off-loop snapshot, never a config load."""
    from kiro_crew.dashboard.session_control_parts import caller_access

    _chain_on(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hand(state, lead, worker, conductor)

    def _no_load(*_a, **_k):
        raise AssertionError("KiroCrewConfig.load ran inside the synchronous re-check")

    monkeypatch.setattr(caller_access.KiroCrewConfig, "load", _no_load)
    admitted = sc.authorize_target(
        state,
        caller_session_key=_key(lead),
        target=worker.key,
        operation="close",
        skip_enabled_check=True,
        precomputed_ownership_fenced=True,
    )
    assert admitted is worker


def test_with_the_chain_on_a_closed_conductor_hands_its_workers_up(tmp_path, monkeypatch):
    _chain_on(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hand(state, lead, worker, conductor)
    state._slots.pop(conductor.key)

    moved = asyncio.run(sc.hand_back_on_close(state, conductor))

    assert moved == [worker.key]
    assert session_managers.holder(worker) == lead.key
    session_managers.reset_cache()
    assert _reaches(state, lead, worker, "send")


def test_with_the_chain_on_a_closed_conductor_hands_up_the_workers_it_created(
    tmp_path, monkeypatch
):
    _chain_on(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, _worker = _team(state)
    own = _slot(state, "chat-own")
    own._created_by = conductor.key
    assert _reaches(state, lead, own, "send")
    state._slots.pop(conductor.key)

    assert asyncio.run(sc.hand_back_on_close(state, conductor)) == [own.key]
    assert _reaches(state, lead, own, "send")


def test_with_the_chain_off_a_closed_conductor_hands_nothing_back(tmp_path):
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hand(state, lead, worker, conductor)
    state._slots.pop(conductor.key)

    assert asyncio.run(sc.hand_back_on_close(state, conductor)) == []
    assert session_managers.holder(worker) == conductor.key
    assert not _reaches(state, lead, worker, "send")


def test_with_the_chain_on_closing_a_conductor_through_close_hands_its_workers_up(
    tmp_path, monkeypatch
):
    """The real close path, not a direct call: ``close_slot`` must run the hand-back."""
    _chain_on(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hand(state, lead, worker, conductor)

    asyncio.run(sc.close_target(state, caller_session_key=_key(lead), target=conductor.key))

    assert conductor.key not in state._slots
    assert session_managers.holder(worker) == lead.key


# ── the per-caller slot cap ─────────────────────────────────────────────────


def test_a_handed_over_session_counts_against_its_manager_and_its_creator(tmp_path):
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    before = state.creator_slot_count(lead.key)

    _hand(state, lead, worker, conductor)

    assert state.creator_slot_count(lead.key) == before
    assert state.creator_slot_count(conductor.key) == 1


def test_closing_the_manager_does_not_lower_the_creators_count(tmp_path):
    """With the chain off nothing is handed back, so the worker would count against
    nobody if the cap charged only the manager."""
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    before = state.creator_slot_count(lead.key)
    _hand(state, lead, worker, conductor)

    state._slots.pop(conductor.key)

    assert worker.key in state._slots
    assert state.creator_slot_count(lead.key) == before - 1


# ── handoffs are serialized ─────────────────────────────────────────────────


def test_crossing_handoffs_cannot_both_pass_and_save_a_loop(tmp_path, monkeypatch):
    """A to B and B to A at once: the second decides on what the first saved.

    The first handoff's write is held open until the second has either finished
    (no lock: it checked the old records) or parked on the handoff lock.
    """
    import threading

    from kiro_crew.dashboard.session_control_parts import session_handoff
    from kiro_crew.testing import wait

    _chain_on(monkeypatch)
    state = _make_state(tmp_path)
    lead, a, b = _team(state)
    real_write = session_managers.write
    entered, release = threading.Event(), threading.Event()

    def _held_write(changes):
        if not entered.is_set():
            entered.set()
            release.wait(30)
        real_write(changes)

    monkeypatch.setattr(session_managers, "write", _held_write)

    def _hand_async(target, to):
        return sc.handoff_target(state, caller_session_key=_key(lead), target=target.key, to=to.key)

    async def _both():
        first = asyncio.create_task(_hand_async(a, b))
        try:
            await wait.async_wait_until(entered.is_set)
            second = asyncio.create_task(_hand_async(b, a))
            loop = asyncio.get_running_loop()
            await wait.async_wait_until(
                lambda: second.done() or wait._waiters_of(session_handoff._HANDOFF_LOCK, loop)
            )
        finally:
            release.set()
        return await asyncio.gather(first, second, return_exceptions=True)

    first, second = asyncio.run(_both())

    assert first["manager"] == b.key
    assert isinstance(second, sc.SessionControlError), second
    assert second.code == "would_cycle"
    session_managers.reset_cache()
    assert session_managers.holder(b) == lead.key


def test_a_handoff_to_a_receiver_at_the_slot_cap_is_refused(tmp_path, monkeypatch):

    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    own = _slot(state, "chat-own")
    own._created_by = conductor.key
    monkeypatch.setattr(sc, "MAX_SLOTS_PER_CREATOR", 1)

    with pytest.raises(sc.SessionControlError) as err:
        _hand(state, lead, worker, conductor)

    assert err.value.code == "creator_slot_cap_reached"
    assert session_managers.holder(worker) == lead.key


def test_a_conductors_default_broadcast_audience_includes_the_workers_handed_to_it(tmp_path):
    from kiro_crew.dashboard.session_control_parts.session_messaging import broadcast_audience

    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hand(state, lead, worker, conductor)

    assert broadcast_audience(state, conductor.key) == [worker.key]
    assert worker.key not in broadcast_audience(state, lead.key)


def test_the_new_manager_can_revive_a_handed_over_session_after_it_is_archived(tmp_path):
    """The crew log is off here, so the tree lineage is unreadable: the managers
    record is what admits the conductor."""
    from session_control_revive_helpers import _archive, _revive

    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    worker.messages.append({"role": "user", "content": "m"})
    _hand(state, lead, worker, conductor)
    key = _archive(state, conductor, worker)

    _revive(state, conductor, key, caller_fenced=True)

    assert key in state._slots
    assert session_managers.holder(state._slots[key]) == conductor.key


def test_a_revived_handed_over_session_keeps_its_creator_from_the_creation_record(
    tmp_path, monkeypatch
):
    """The handoff moved the sidebar parent to the conductor. The creator comes from
    the creation record, so the revived worker is still the lead's, and fenced."""
    from session_control_revive_helpers import _archive, _revive

    from kiro_crew.dashboard.session_control_parts.caller_access import (
        _caller_is_ownership_fenced,
    )

    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    worker.messages.append({"role": "user", "content": "m"})
    _hand(state, lead, worker, conductor)
    key = _archive(state, conductor, worker)
    monkeypatch.setattr(sc, "_slot_tree_parent", lambda slot_key: (True, conductor.key, {}))
    monkeypatch.setattr(
        sc, "_creation_parent", lambda slot_key: lead.key if slot_key == key else ""
    )

    _revive(state, conductor, key, caller_fenced=True)

    revived = state._slots[key]
    assert revived._created_by == lead.key
    assert _caller_is_ownership_fenced(state, key)


def test_reopening_a_handed_over_session_from_history_keeps_its_manager(tmp_path):
    """The History tab's resume mints a new tab id; the record follows it."""
    from session_control_revive_helpers import _archive

    from kiro_crew.dashboard.chat_handlers import resume_slot_from_history

    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    worker.messages.append({"role": "user", "content": "m"})
    _hand(state, lead, worker, conductor)
    key = _archive(state, conductor, worker)

    # What the History tab sends: the slot name and the transcript's history key.
    outcome = asyncio.run(resume_slot_from_history(state, name=key, history_key=f"dashboard:{key}"))

    assert outcome.refusal is None, outcome.refusal
    reopened = state._slots[key]
    assert session_managers.holder(reopened) == conductor.key
    assert _reaches(state, conductor, reopened, "send")


def test_a_handed_over_session_reopened_from_history_stays_fenced(tmp_path):
    """A History reopen does not restore the creator. A blank creator reads as a
    person's tab, so the manager would drive a session that runs unfenced."""
    from session_control_revive_helpers import _archive

    from kiro_crew.dashboard.chat_handlers import resume_slot_from_history
    from kiro_crew.dashboard.session_control_parts.caller_access import (
        _caller_is_ownership_fenced,
    )

    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    worker.messages.append({"role": "user", "content": "m"})
    _hand(state, lead, worker, conductor)
    key = _archive(state, conductor, worker)

    outcome = asyncio.run(resume_slot_from_history(state, name=key, history_key=f"dashboard:{key}"))

    assert outcome.refusal is None, outcome.refusal
    reopened = state._slots[key]
    assert _caller_is_ownership_fenced(state, key)
    assert _reaches(state, conductor, reopened, "send")


def test_with_the_chain_on_a_title_two_reachable_workers_share_is_ambiguous(tmp_path, monkeypatch):
    """The lead reaches its own worker and, through the chain, the conductor's. A
    broadcast by their shared title is refused, not sent to the direct one."""
    _chain_on(monkeypatch)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    direct = _slot(state, "chat-direct")
    direct._created_by = lead.key
    _hand(state, lead, worker, conductor)
    worker.title = direct.title = "Same title"

    out = asyncio.run(
        sc.broadcast_to_targets(
            state,
            caller_session_key=_key(lead),
            message="hello",
            mode="queue",
            targets=["Same title"],
            caller_fenced=True,
        )
    )

    (row,) = out["results"]
    assert row["ok"] is False
    assert row["code"] == "ambiguous_target"
