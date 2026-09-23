"""The two verbs that move a session in the tree: ``session_adopt`` / ``session_release``.

Split from the other session-control tests because what they can get wrong is
different. The send and stop verbs act on a target's WORK; these act on where the
sidebar puts it, so the failures worth catching are an authorization that admits the
wrong caller and a record that says the tree moved when it did not.

Every refusal is asserted against the REAL slot objects, the same rule the rest of the
session-control suite follows: the guards read ``memory_mode`` / ``workspace`` / ``_app``
off the production class, and a permissive double would let a dead guard look alive.
"""

from __future__ import annotations

import asyncio
import itertools

import pytest
from chat_test_helpers import _make_state

from kiro_crew.crew_log import emit
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log.session_tree import EdgeRecord, OpenedRecord
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    """The shipped state (enabled), without reading config."""
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture(autouse=True)
def _tree_on(tmp_path, monkeypatch):
    """A recorded tree, folded in memory, with no write armed on the real pool.

    Both verbs refuse outright when the crew log is off -- there would be nowhere to
    record the edge -- so every test here needs the flag on and the projection seeded
    for THIS home. The projection is dropped on both sides because it is bound to one
    store.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crewhome"))
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    monkeypatch.setattr(
        "kiro_crew.executors.maintenance_executor",
        lambda: type("_NoPool", (), {"submit": staticmethod(lambda *a, **k: None)}),
    )
    stp.reset_for_tests()
    stp.projection().ensure_seeded()
    _SIDS.clear()
    sc._tree_pending_slots.clear()
    yield
    stp.reset_for_tests()
    _SIDS.clear()
    # The fence is keyed by slot and lives for the life of an unsettled append, so a test
    # whose stub never settles (the wedged-writer case) leaves one standing on purpose.
    # Cleared on both sides so that is contained to the test that wants it.
    sc._tree_pending_slots.clear()


#: Session ids per harness state, cleared between tests by the fixture above.
_SIDS: dict[int, dict[str, str]] = {}


def _slot(state, name: str, **kwargs):
    return state.get_or_create_slot(name, **kwargs)


def _key(slot) -> str:
    return slot_history_key(slot)


def _live(state, slot, sid: str):
    """Map *slot* to an ACP session id, which is the log the entry is written into.

    Through the session map, because that is where the verb reads it: the slot's in-turn
    ACP client is cleared between turns, and the sessions a takeover is aimed at are the
    idle ones. The harness's ``state.sessions`` is a mock, so the lookup is given a real
    dictionary to answer from -- a bare mock returns a mock, which the verb correctly
    refuses as "no session id", and every test here would then pass for the wrong
    reason.
    """
    mapping = _SIDS.setdefault(id(state), {})
    mapping[f"dashboard:{slot.key}"] = sid
    # Assigned on EVERY call, never behind a "has it been set yet" probe: reading an
    # attribute off a mock creates it, so such a probe never answers None and the stub
    # would never be installed at all.
    state.sessions._session_map.mapped_sid.side_effect = lambda key: mapping.get(key, "")
    return slot


def _hold(slot: str, parent: str, *, at: int = 100, sid: str = "") -> None:
    """Put *slot* under *parent* in the fold, as a prior adoption would have."""
    proj = stp.projection()
    proj.apply(OpenedRecord(sid=sid or f"sid-{slot}", slot=slot, created_at=1))
    proj.apply(OpenedRecord(sid=f"sid-{parent}", slot=parent, created_at=2))
    proj.apply_edge(EdgeRecord(slot=slot, parent_slot=parent, at=at, sid=sid or f"sid-{slot}"))


def _run(coro):
    return asyncio.run(coro)


def _emitted(monkeypatch, *, landed: bool = True) -> list[dict]:
    """Capture what the verb hands the emitter, instead of draining the writer.

    The emitter's own path is covered where the entry's bytes matter
    (``test_crew_log_session_tree_adopt``). What these tests are about is WHICH call the
    verb makes and with what -- and a refusal must make none at all.

    The stub SETTLES the append, because the verb waits for it: the real emitter reports
    the durable outcome through ``on_settled`` and a stub that recorded the call and
    stayed silent would leave every success path sitting on the wait until it timed out.
    Pass ``landed=False`` to stand in for a write the writer gave up on.
    """
    calls: list[dict] = []

    def _record(op: str, sid: str, kw: dict) -> None:
        settle = kw.pop("on_settled", None)
        # Recorded WITHOUT the hook: these tests compare the whole call against the
        # payload the verb is supposed to send, and the completion callback is wiring
        # rather than part of the entry.
        calls.append({"op": op, "sid": sid, **kw})
        if settle is not None:
            settle(landed)

    monkeypatch.setattr(
        emit,
        "on_session_adopted",
        lambda sid, **kw: _record("adopt", sid, kw),
    )
    monkeypatch.setattr(
        emit,
        "on_session_released",
        lambda sid, **kw: _record("release", sid, kw),
    )
    return calls


# ── adopt ────────────────────────────────────────────────────────────────────


def test_adopting_records_the_caller_as_the_parent(tmp_path, monkeypatch):
    """The adopter is the CALLER, never an argument: a tool that let one session
    nominate the parent would let it rearrange another session's tree with nothing in
    the record showing which of them asked."""
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-1"), "sid-caller")
    _live(state, _slot(state, "chat-2"), "sid-target")
    result = _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-2"))
    assert result["target"] == "chat-2"
    assert result["parent"] == "chat-1"
    assert calls == [
        {
            "op": "adopt",
            "sid": "sid-target",
            "slot": "chat-2",
            "parent_slot": "chat-1",
            "parent_sid": "sid-caller",
            "previous_parent_slot": "",
            "previous_parent_sid": "",
        }
    ]


def test_a_takeover_records_the_parent_it_replaced(tmp_path, monkeypatch):
    """The case the verb exists for: one conductor taking over another's workers.

    Adopting a session that already has a parent is ALLOWED, and the parent it had is
    recorded -- so the log says who held the session before, which the fold does not.
    """
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-new"), "sid-new")
    _live(state, _slot(state, "chat-worker"), "sid-worker")
    _live(state, _slot(state, "chat-old"), "sid-old")
    _hold("chat-worker", "chat-old", sid="sid-worker")
    result = _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-worker"))
    assert result["previous_parent"] == "chat-old"
    assert calls[0]["previous_parent_slot"] == "chat-old"
    assert calls[0]["previous_parent_sid"] == "sid-old"


def test_a_refused_takeover_is_recorded_as_a_denial(tmp_path, monkeypatch):
    """A permission decision has to leave a trail whichever way it went.

    A refusal LEAVES the verb by raising, so the audit call at the end is simply not
    reached -- which means a caller probing the tree's boundary produced a run of refusals
    and no record of any of them. ``backend-security-controls`` requires a SEL event for
    every permission decision, and this is the shape most worth having one for.
    """
    audited: list[dict[str, object]] = []
    monkeypatch.setattr(sc, "_audit", lambda **kw: audited.append(kw))
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    _live(state, _slot(state, "chat-top"), "sid-top")
    caller = _live(state, _slot(state, "chat-mid"), "sid-mid")
    _hold("chat-mid", "chat-top", sid="sid-mid")
    with pytest.raises(sc.SessionControlError) as excinfo:
        _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-top"))
    assert excinfo.value.code == "would_cycle"
    assert calls == [], "a refused takeover must write nothing"
    assert [(row["operation"], row["outcome"], row["slot_key"]) for row in audited] == [
        ("adopt", "denied", "chat-top")
    ]
    assert audited[0]["detail"] == {"code": "would_cycle"}


def test_a_refused_release_is_recorded_as_a_denial(tmp_path, monkeypatch):
    """The same for the release verb, which has its own refusals and shared the gap."""
    audited: list[dict[str, object]] = []
    monkeypatch.setattr(sc, "_audit", lambda **kw: audited.append(kw))
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    target = _live(state, _slot(state, "chat-2"), "sid-2")
    caller = _live(state, _slot(state, "chat-1"), "sid-caller")
    _hold("chat-2", "chat-other", sid="sid-2")
    with pytest.raises(sc.SessionControlError) as excinfo:
        _run(sc.release_target(state, caller_session_key=_key(caller), target=target.key))
    assert excinfo.value.code == "not_parent"
    assert calls == []
    assert [(row["operation"], row["outcome"]) for row in audited] == [("release", "denied")]
    assert audited[0]["detail"] == {"code": "not_parent"}


def test_adopting_a_session_already_above_the_caller_is_refused(tmp_path, monkeypatch):
    """The loop the tree cannot present.

    A cycle marks every slot on it and nests none of them, so allowing this would
    silently FLATTEN a whole branch rather than produce the takeover the caller asked
    for.
    """
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    _live(state, _slot(state, "chat-top"), "sid-top")
    caller = _live(state, _slot(state, "chat-mid"), "sid-mid")
    _hold("chat-mid", "chat-top", sid="sid-mid")
    with pytest.raises(sc.SessionControlError) as excinfo:
        _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-top"))
    assert excinfo.value.code == "would_cycle"
    assert calls == []


def test_adopting_yourself_is_refused_by_the_shared_gate(tmp_path, monkeypatch):
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-1"), "sid-caller")
    with pytest.raises(sc.SessionControlError) as excinfo:
        _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-1"))
    assert excinfo.value.code == "self_target"
    assert calls == []


def test_adopting_a_session_that_is_not_open_is_refused(tmp_path, monkeypatch):
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-1"), "sid-caller")
    with pytest.raises(sc.SessionControlError) as excinfo:
        _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-404"))
    assert excinfo.value.code == "target_not_found"
    assert calls == []


def test_an_unattended_caller_cannot_adopt(tmp_path, monkeypatch):
    """A scheduled run rearranging the person's sidebar is exactly the shape the
    unattended refusal exists for."""
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "workflow-7"), "sid-workflow")
    _live(state, _slot(state, "chat-2"), "sid-target")
    with pytest.raises(sc.SessionControlError) as excinfo:
        _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-2"))
    assert excinfo.value.code == "unattended_caller"
    assert calls == []


def test_adopting_across_a_workspace_is_refused(tmp_path, monkeypatch):
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-1"), "sid-caller")
    _live(state, _slot(state, "chat-2", workspace="other"), "sid-target")
    with pytest.raises(sc.SessionControlError) as excinfo:
        _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-2"))
    assert excinfo.value.code == "workspace_mismatch"
    assert calls == []


def test_adopting_refuses_when_the_tree_is_not_being_recorded(tmp_path, monkeypatch):
    """The record IS the edge, so a verb whose record cannot be written has done
    nothing -- and says so instead of reporting a success the sidebar will not show."""
    calls = _emitted(monkeypatch)
    monkeypatch.setenv(emit.CREW_LOG_ENV, "0")
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-1"), "sid-caller")
    _live(state, _slot(state, "chat-2"), "sid-target")
    with pytest.raises(sc.SessionControlError) as excinfo:
        _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-2"))
    assert excinfo.value.code == "tree_unavailable"
    assert calls == []


def test_adopting_a_session_with_no_live_log_is_refused(tmp_path, monkeypatch):
    """No live ACP session means no log to append the edge to."""
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-1"), "sid-caller")
    _slot(state, "chat-2")
    with pytest.raises(sc.SessionControlError) as excinfo:
        _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-2"))
    assert excinfo.value.code == "tree_unavailable"
    assert calls == []


# ── release ──────────────────────────────────────────────────────────────────


def test_a_parent_can_release_what_it_holds(tmp_path, monkeypatch):
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-1"), "sid-caller")
    _live(state, _slot(state, "chat-2"), "sid-target")
    _hold("chat-2", "chat-1", sid="sid-target")
    result = _run(sc.release_target(state, caller_session_key=_key(caller), target="chat-2"))
    assert result["previous_parent"] == "chat-1"
    assert calls == [
        {
            "op": "release",
            "sid": "sid-target",
            "slot": "chat-2",
            "previous_parent_slot": "chat-1",
            "previous_parent_sid": "sid-caller",
        }
    ]


def test_a_session_can_release_itself(tmp_path, monkeypatch):
    """A session taken over must not need its holder's cooperation to get out: a
    conductor that has stopped running would otherwise pin its workers under it."""
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    _live(state, _slot(state, "chat-parent"), "sid-parent")
    caller = _live(state, _slot(state, "chat-child"), "sid-child")
    _hold("chat-child", "chat-parent", sid="sid-child")
    result = _run(sc.release_target(state, caller_session_key=_key(caller), target="chat-child"))
    assert result["previous_parent"] == "chat-parent"
    assert calls[0]["slot"] == "chat-child"


def test_an_agent_created_session_can_still_release_itself(tmp_path, monkeypatch):
    """The ownership fence bounds a caller to what it CREATED, and the session it is
    itself was never another session's to protect. Without the waiver a worker could
    never get out from under a stopped conductor."""
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    _live(state, _slot(state, "chat-parent"), "sid-parent")
    caller = _live(state, _slot(state, "chat-child"), "sid-child")
    caller._created_by = "chat-parent"
    _hold("chat-child", "chat-parent", sid="sid-child")
    result = _run(sc.release_target(state, caller_session_key=_key(caller), target="chat-child"))
    assert result["target"] == "chat-child"
    assert calls[0]["op"] == "release"


def test_a_third_session_cannot_release_someone_elses_child(tmp_path, monkeypatch):
    """Only the holder and the held one, which is what keeps the verb from being a way
    to rearrange a tree the caller has no part in."""
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-bystander"), "sid-bystander")
    _live(state, _slot(state, "chat-parent"), "sid-parent")
    _live(state, _slot(state, "chat-child"), "sid-child")
    _hold("chat-child", "chat-parent", sid="sid-child")
    with pytest.raises(sc.SessionControlError) as excinfo:
        _run(sc.release_target(state, caller_session_key=_key(caller), target="chat-child"))
    assert excinfo.value.code == "not_parent"
    assert calls == []


def test_releasing_a_session_that_has_no_parent_is_refused(tmp_path, monkeypatch):
    """Nothing to release: writing the entry anyway would put a record of a change into
    a log where nothing changed."""
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-1"), "sid-caller")
    _live(state, _slot(state, "chat-2"), "sid-target")
    with pytest.raises(sc.SessionControlError) as excinfo:
        _run(sc.release_target(state, caller_session_key=_key(caller), target="chat-2"))
    assert excinfo.value.code == "already_root"
    assert calls == []


def test_releasing_refuses_when_the_tree_is_not_being_recorded(tmp_path, monkeypatch):
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-1"), "sid-caller")
    _live(state, _slot(state, "chat-2"), "sid-target")
    _hold("chat-2", "chat-1", sid="sid-target")
    monkeypatch.setenv(emit.CREW_LOG_ENV, "0")
    with pytest.raises(sc.SessionControlError) as excinfo:
        _run(sc.release_target(state, caller_session_key=_key(caller), target="chat-2"))
    assert excinfo.value.code == "tree_unavailable"
    assert calls == []


def test_adopting_refuses_while_the_tree_cannot_be_read(tmp_path, monkeypatch):
    """The cycle guard is the only thing between a takeover and a loop, and a guard
    handed no tree admits everything -- so an unreadable tree stops the write.

    This is every gateway between boot and the first lineage seed, so it recurs on each
    restart rather than being exotic, and the loop it would admit is not repaired by the
    fold: that flattens the branch instead.
    """
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-1"), "sid-caller")
    _live(state, _slot(state, "chat-2"), "sid-target")
    monkeypatch.setattr(
        "kiro_crew.crew_log.session_tree_projection.SessionTreeProjection."
        "seeded_for_current_store",
        property(lambda _self: False),
    )
    with pytest.raises(sc.SessionControlError) as excinfo:
        _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-2"))
    assert excinfo.value.code == "tree_not_ready"
    assert calls == []


def test_releasing_refuses_while_the_tree_cannot_be_read(tmp_path, monkeypatch):
    """The mirror reason: this verb decides WHO may call it from the parent the tree
    reports, so an unreadable tree cannot tell "you are not the parent" from "I cannot
    see who is"."""
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-1"), "sid-caller")
    _live(state, _slot(state, "chat-2"), "sid-target")
    _hold("chat-2", "chat-1", sid="sid-target")
    monkeypatch.setattr(
        "kiro_crew.crew_log.session_tree_projection.SessionTreeProjection."
        "seeded_for_current_store",
        property(lambda _self: False),
    )
    with pytest.raises(sc.SessionControlError) as excinfo:
        _run(sc.release_target(state, caller_session_key=_key(caller), target="chat-2"))
    assert excinfo.value.code == "tree_not_ready"
    assert calls == []


# ── durability and serialization ─────────────────────────────────────────────


def test_an_adoption_the_writer_gave_up_on_is_refused_not_reported(tmp_path, monkeypatch):
    """A verb whose append never landed must not answer "adopted".

    The emitter hands the entry to the crew-log writer and returns, so without waiting
    for the durable outcome a filesystem that refused the append would still have told
    the caller the takeover happened -- and the next read of the tree would disagree with
    the answer the caller already acted on.
    """
    calls = _emitted(monkeypatch, landed=False)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-1"), "sid-caller")
    _live(state, _slot(state, "chat-2"), "sid-target")
    with pytest.raises(sc.SessionControlError) as refused:
        _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-2"))
    assert refused.value.code == "tree_write_failed"
    # The call WAS made -- the entry was handed over and lost, which is a different
    # thing from a refusal that writes nothing.
    assert [c["op"] for c in calls] == ["adopt"]


def test_a_release_the_writer_gave_up_on_is_refused_not_reported(tmp_path, monkeypatch):
    """The mirror, for the verb that takes an edge away: a release reported but not
    written would leave the session under a parent the caller believes let it go."""
    calls = _emitted(monkeypatch, landed=False)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-1"), "sid-caller")
    _live(state, _slot(state, "chat-2"), "sid-target")
    _hold("chat-2", "chat-1", sid="sid-target")
    with pytest.raises(sc.SessionControlError) as refused:
        _run(sc.release_target(state, caller_session_key=_key(caller), target="chat-2"))
    assert refused.value.code == "tree_write_failed"
    assert [c["op"] for c in calls] == ["release"]


def test_two_takeovers_of_one_session_serialize(tmp_path, monkeypatch):
    """The second takeover decides from the tree the first one COMMITTED.

    Two conductors adopting the same session read the tree, then write to it. Unless the
    read and the commit are one region, both read "no parent" and the second's record
    claims it took the session from nobody -- so the log of who held it loses a step, and
    a release authorized against the first parent can land after the second adoption and
    return the session to a conductor that had already handed it on.

    The stub advances the projection the way the writer's own job does, on settle, so the
    tree this asserts against moves exactly when a real append would move it.
    """
    seq = itertools.count(1)
    applied: list[str] = []

    def _settle_into_the_tree(sid: str, **kw):
        settle = kw.pop("on_settled", None)
        applied.append(kw.get("previous_parent_slot") or "none")

        def _landed() -> None:
            # Applied when the append SETTLES, not when it is handed over, because that
            # is when the real writer advances the projection -- its job records the
            # decision after ``log.append`` returns. Applying it at hand-over would make
            # the in-memory tree move before the write lands, and the second verb would
            # then read the first one's decision whether or not the two were serialized:
            # the read and the emit have no await between them, so it is the WAIT that
            # the lock has to cover.
            stp.projection().apply_edge(
                EdgeRecord(
                    slot=kw["slot"],
                    parent_slot=kw["parent_slot"],
                    at=200,
                    sid=sid,
                    seq=next(seq),
                )
            )
            if settle is not None:
                settle(True)

        asyncio.get_running_loop().call_later(0.01, _landed)

    monkeypatch.setattr(emit, "on_session_adopted", _settle_into_the_tree)
    state = _make_state(tmp_path)
    first = _live(state, _slot(state, "chat-a"), "sid-a")
    second = _live(state, _slot(state, "chat-b"), "sid-b")
    _live(state, _slot(state, "chat-t"), "sid-target")
    # The three logs, with no edge between them: a decision for a slot the fold has no
    # record of changes nothing, so the tree has to know these slots before an applied
    # adoption can be read back off it.
    proj = stp.projection()
    for slot, sid, created in (
        ("chat-a", "sid-a", 1),
        ("chat-b", "sid-b", 2),
        ("chat-t", "sid-target", 3),
    ):
        proj.apply(OpenedRecord(sid=sid, slot=slot, created_at=created))

    async def _both():
        return await asyncio.gather(
            sc.adopt_target(state, caller_session_key=_key(first), target="chat-t"),
            sc.adopt_target(state, caller_session_key=_key(second), target="chat-t"),
        )

    results = _run(_both())
    # One of them found it unheld and the other found the first one holding it; which
    # went first is the scheduler's business, so the assertion is on the PAIR.
    assert sorted(applied) == ["chat-a", "none"] or sorted(applied) == ["chat-b", "none"]
    assert {r["previous_parent"] or "none" for r in results} == set(applied)


# ── the tool surface ─────────────────────────────────────────────────────────


def test_both_verbs_are_gated_and_blocked_like_the_other_session_control_tools():
    """Three places must agree on the tool set, and spelling it out per site is how
    ``session_create`` came to be identity-gated and reachable from a channel."""
    from kiro_crew.channel import CHANNEL_AGENT_BLOCKED_TOOLS
    from kiro_crew.mcp_dashboard import SESSION_CONTROL_TOOLS, _tool_definitions

    advertised = {d["name"] for d in _tool_definitions()}
    for tool in ("session_adopt", "session_release"):
        assert tool in advertised
        assert tool in SESSION_CONTROL_TOOLS
        assert tool in CHANNEL_AGENT_BLOCKED_TOOLS


def test_neither_verb_is_auto_granted_to_an_unattended_conductor():
    """The grant invariant: a granted verb may CREATE or READ, never MUTATE workspace
    state that already exists and is not the agent's own. An adoption moves where
    another session sits in the person's sidebar, and takes its subtree along."""
    from kiro_crew.agent import _CONDUCTOR_DASHBOARD_GRANTS, _MEMBER_DASHBOARD_GRANTS

    for tool in ("session_adopt", "session_release"):
        assert f"@kirocrew-dashboard/{tool}" not in _CONDUCTOR_DASHBOARD_GRANTS
        assert f"@kirocrew-dashboard/{tool}" not in _MEMBER_DASHBOARD_GRANTS


def test_the_routes_are_registered_and_carry_the_internal_secret():
    """A path missing from the internal-auth set falls through to the general branch,
    which honors only cookie/query tokens -- so the MCP caller's header is ignored and
    the tool is unreachable in production while handler tests still pass."""
    from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS

    assert "/api/session-control/adopt" in _STRICT_INTERNAL_API_PATHS
    assert "/api/session-control/release" in _STRICT_INTERNAL_API_PATHS


# ── the gate is re-read where it is acted on ─────────────────────────────────


def test_an_authorization_that_lapsed_during_the_lock_wait_does_not_write(tmp_path, monkeypatch):
    """The decision that gates the write is taken INSIDE the lock, not before it.

    The wait for the mutation lock is unbounded: the verb ahead awaits its own append, so
    anything the gate reads can move meanwhile -- the target can be closed, the caller's
    grant withdrawn. A verb that wrote on the answer it got before the wait would record a
    takeover nobody was entitled to at the moment it landed.

    The lock is held by the TEST rather than by a first verb, so the target stops being
    addressable in the one window that matters: after the pre-lock gate has already said
    yes, and before the write. A pop before that window is caught by the pre-lock gate and
    would prove nothing about the one inside it.
    """
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-a"), "sid-a")
    target = _slot(state, "chat-t")
    _live(state, target, "sid-target")
    proj = stp.projection()
    proj.apply(OpenedRecord(sid="sid-a", slot="chat-a", created_at=1))
    proj.apply(OpenedRecord(sid="sid-target", slot="chat-t", created_at=2))

    async def _lapse():
        lock = sc._tree_mutation_lock()
        await lock.acquire()
        try:
            call = asyncio.ensure_future(
                sc.adopt_target(state, caller_session_key=_key(caller), target="chat-t")
            )
            # Enough turns for it to clear the pre-lock gate and park on the lock. It
            # must NOT be finished: a verb that completed here never waited, and the
            # window this test is about would not exist.
            for _ in range(8):
                await asyncio.sleep(0)
            assert not call.done()
            state._slots.pop(target.key, None)
        finally:
            lock.release()
        return await asyncio.gather(call, return_exceptions=True)

    (outcome,) = _run(_lapse())
    assert isinstance(outcome, sc.SessionControlError), outcome
    assert outcome.code == "target_not_found"
    # Nothing was handed to the writer: the refusal is at the point of writing, so there
    # is no record of an adoption the caller is not entitled to make.
    assert calls == []


def test_adopting_refuses_on_a_fold_that_could_not_read_every_unit(tmp_path, monkeypatch):
    """An INCOMPLETE fold is treated as no tree, because this guard DECIDES on an edge.

    ``TreeReading.incomplete`` means a unit's bytes could not be read or the population
    was capped, so an edge the cycle check needs can simply be absent from an otherwise
    well-formed set of nodes. Admitting the adoption then is a confident wrong answer, and
    the shape it admits is the loop the guard exists to refuse.
    """
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-1"), "sid-caller")
    _live(state, _slot(state, "chat-2"), "sid-target")
    proj = stp.projection()
    proj.apply(OpenedRecord(sid="sid-caller", slot="chat-1", created_at=1))
    proj.apply(OpenedRecord(sid="sid-target", slot="chat-2", created_at=2))
    original = stp.SessionTreeProjection.reading

    def _incomplete(self):
        reading = original(self)
        return type(reading)(nodes=reading.nodes, incomplete=True, records=reading.records)

    monkeypatch.setattr(stp.SessionTreeProjection, "reading", _incomplete)
    with pytest.raises(sc.SessionControlError) as excinfo:
        _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-2"))
    assert excinfo.value.code == "tree_not_ready"
    assert calls == []


def test_both_verbs_warm_the_config_read_off_the_loop_before_every_gate(tmp_path, monkeypatch):
    """``authorize_target`` is synchronous and its enabled check re-reads the config file
    on the first call after an edit, so an unwarmed gate blocks the shared loop for every
    other session.

    Two gates per verb -- the cheap pre-lock refusal and the in-lock re-check -- and the
    lock wait between them is exactly the suspension that invalidates the first warm, so
    each one owes its own. Asserted as a COUNT for that reason.
    """
    _emitted(monkeypatch)
    warms: list[int] = []

    async def _count():
        warms.append(1)

    monkeypatch.setattr(sc, "prewarm_enabled_check", _count)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-1"), "sid-caller")
    _live(state, _slot(state, "chat-2"), "sid-target")
    _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-2"))
    assert len(warms) == 2
    _hold("chat-2", "chat-1", sid="sid-target")
    warms.clear()
    _run(sc.release_target(state, caller_session_key=_key(caller), target="chat-2"))
    assert len(warms) == 2


def test_a_timed_out_append_fences_the_slot_against_the_next_verb(tmp_path, monkeypatch):
    """A verb that times out waiting for its own append leaves that append QUEUED.

    The timeout raises out of the mutation lock, which releases it, so without a fence the
    next verb reads a tree the writer is about to change and commits against it -- the
    former parent's release lands authorized by a parent the queued adoption is about to
    replace, and the two commit in the wrong order. Holding the lock until settlement
    instead would pin the serialized region to a wedged filesystem, which is the thing the
    timeout exists to avoid, so the SLOT is fenced and the loop stays free.
    """
    handed: list[str] = []

    def _never_settles(sid: str, **kw):
        # Handed to the writer and never settled, which is the wedged-writer state.
        handed.append(kw.get("slot") or "")

    monkeypatch.setattr(emit, "on_session_adopted", _never_settles)
    monkeypatch.setattr(emit, "on_session_released", _never_settles)
    monkeypatch.setattr(sc, "_TREE_APPEND_TIMEOUT", 0.05)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-a"), "sid-a")
    other = _live(state, _slot(state, "chat-b"), "sid-b")
    _live(state, _slot(state, "chat-t"), "sid-target")
    proj = stp.projection()
    for slot, sid, created in (
        ("chat-a", "sid-a", 1),
        ("chat-b", "sid-b", 2),
        ("chat-t", "sid-target", 3),
    ):
        proj.apply(OpenedRecord(sid=sid, slot=slot, created_at=created))

    with pytest.raises(sc.SessionControlError) as first:
        _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-t"))
    assert first.value.code == "tree_write_pending"
    assert handed == ["chat-t"]

    # The lock is free again, so this verb gets in -- and must be refused on the slot
    # rather than allowed to decide from a tree the queued append is about to move.
    with pytest.raises(sc.SessionControlError) as second:
        _run(sc.adopt_target(state, caller_session_key=_key(other), target="chat-t"))
    assert second.value.code == "tree_write_pending"
    # Refused BEFORE reaching the writer: one append is queued, not two.
    assert handed == ["chat-t"]


def test_the_fence_clears_when_the_append_settles(tmp_path, monkeypatch):
    """The fence is self-clearing, so a slow-but-successful append does not lock a session
    out of the tree for the rest of the gateway's life."""
    calls = _emitted(monkeypatch)
    state = _make_state(tmp_path)
    caller = _live(state, _slot(state, "chat-a"), "sid-a")
    _live(state, _slot(state, "chat-t"), "sid-target")
    proj = stp.projection()
    proj.apply(OpenedRecord(sid="sid-a", slot="chat-a", created_at=1))
    proj.apply(OpenedRecord(sid="sid-target", slot="chat-t", created_at=2))

    _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-t"))
    assert [c["op"] for c in calls] == ["adopt"]
    assert "chat-t" not in sc._tree_pending_slots
    # A second verb on the same slot is not refused by a fence that should have cleared.
    _run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-t"))
    assert [c["op"] for c in calls] == ["adopt", "adopt"]
