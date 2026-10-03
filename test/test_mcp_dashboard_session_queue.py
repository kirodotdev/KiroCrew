"""``session_queue``: a sender's handle on the messages it queued on a busy session.

Pinned here:

* the target gate is ``authorize_target`` plus a hard creator fence, so a
  session the caller did not create is refused whatever class of caller it is;
* ``cancel`` and ``move`` reach only entries the caller queued, judged by the
  sender stamp ``session_send`` writes (slot key AND tab identity), so a
  person's typed message and another session's entry are never touched;
* a move EARLIER may pass only the caller's own entries;
* the route is a strict internal-secret path and the tool posts the verified key.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.mcp_dashboard import _call_tool_inner, _render_session_queue
from kiro_crew.validation import SESSION_QUEUE_SCHEMA, ValidationError, validate_tool_args

_VERIFIED = "dashboard:chat-verified"


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture
def frames(monkeypatch):
    """Every WebSocket frame and awaited save the verb emits.

    ``frames[1]`` records each awaited save and ``frames[2]`` each background
    persist start, which only the undo path issues.
    """
    seen: list[tuple[str, dict]] = []
    saved: list[str] = []
    started: list[str] = []

    async def _durable(_state, slot):
        saved.append(slot.key)
        return True

    monkeypatch.setattr(sc, "_await_queue_durable", _durable)
    monkeypatch.setattr(sc, "start_queue_persist", lambda _state, slot: started.append(slot.key))
    return seen, saved, started


def _setup(tmp_path, frames=None):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    peer = state.get_or_create_slot("chat-3")
    target = state.get_or_create_slot("chat-2")
    target._created_by = "chat-1"
    if frames is not None:
        state.broadcast_ws = lambda event, data: frames[0].append((event, data))
    return state, caller, peer, target


def _mine(state, target, text: str) -> str:
    return target.queue_append(text, meta=sc.send_origin_meta(state, "chat-1"))


def _peer(state, target, text: str) -> str:
    return target.queue_append(text, meta=sc.send_origin_meta(state, "chat-3"))


def _person(target, text: str) -> str:
    return target.queue_append(text, directive_user_origin=True)


def _call(state, caller, **kwargs) -> dict:
    kwargs.setdefault("target", "chat-2")
    return asyncio.run(
        sc.queue_target(state, caller_session_key=slot_history_key(caller), **kwargs)
    )


def _ids(target) -> list[str]:
    return [item["id"] for item in target._queue]


# ── list ─────────────────────────────────────────────────────────────────────


def test_list_marks_who_queued_each_entry(tmp_path):
    state, caller, _peer_slot, target = _setup(tmp_path)
    a = _mine(state, target, "rerun the snapshot tests")
    b = _person(target, "stop after this one")
    c = _peer(state, target, "from another conductor")
    out = _call(state, caller)
    assert out["count"] == 3
    assert [(e["id"], e["position"], e["from"]) for e in out["entries"]] == [
        (a, 0, "yours"),
        (b, 1, "person"),
        (c, 2, "other"),
    ]
    assert out["entries"][0]["excerpt"] == "rerun the snapshot tests"


def test_list_redacts_and_cuts_every_excerpt(tmp_path):
    """A person's words reach an agent reader redacted, like any non-human surface."""
    state, caller, _peer_slot, target = _setup(tmp_path)
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    _person(target, f"key {secret}")
    _mine(state, target, "x" * 5000)
    out = _call(state, caller)
    assert secret not in json.dumps(out)
    assert out["entries"][1]["excerpt"].endswith("…[truncated]")


def test_list_is_bounded(tmp_path):
    state, caller, _peer_slot, target = _setup(tmp_path)
    for n in range(sc.MAX_QUEUE_LIST_ENTRIES + 4):
        _mine(state, target, f"m{n}")
    out = _call(state, caller)
    assert len(out["entries"]) == sc.MAX_QUEUE_LIST_ENTRIES
    assert out["omitted"] == 4
    assert out["count"] == sc.MAX_QUEUE_LIST_ENTRIES + 4


def test_an_oversized_restored_id_is_cut_to_the_argument_cap(tmp_path):
    """Mutation guard: echoing the id verbatim returns all 5000 characters."""
    state, caller, _peer_slot, target = _setup(tmp_path)
    target._queue.append({"id": "x" * 5000, "content": "restored", "kind": ""})
    out = _call(state, caller)
    entry_id = out["entries"][0]["id"]
    assert entry_id == "x" * (sc.QUEUE_ENTRY_ID_MAX_CHARS - 1) + "…"
    assert len(entry_id) == sc.QUEUE_ENTRY_ID_MAX_CHARS
    assert set(out["entries"][0]) == {"id", "position", "from", "excerpt"}


def test_a_stamp_from_a_previous_occupant_of_the_key_is_not_yours(tmp_path):
    """The key alone is not an identity: the tab id must match the live caller.

    Mutation guard: comparing ``send_origin_slot`` only reads this entry as yours.
    """
    state, caller, _peer_slot, target = _setup(tmp_path)
    target.queue_append(
        "from the old chat-1", meta={sc.SEND_ORIGIN_META_KEY: {"slot": "chat-1", "tab": "gone"}}
    )
    assert _call(state, caller)["entries"][0]["from"] == "other"


def test_a_person_entry_never_reads_as_yours_even_with_a_stamp(tmp_path):
    state, caller, _peer_slot, target = _setup(tmp_path)
    target.queue_append(
        "typed", meta=sc.send_origin_meta(state, "chat-1"), directive_user_origin=True
    )
    assert _call(state, caller)["entries"][0]["from"] == "person"


@pytest.mark.parametrize("shape", ["kind", "on_consumed", "on_irreversibly_consumed"])
def test_a_stamped_producer_entry_is_not_yours(tmp_path, shape):
    """A recovery requeue or stage delivery can inherit the stamp; it stays ``other``.

    Mutation guard: dropping the plain-entry check reads it as yours, and a cancel
    would strand the waiter its callback settles.
    """
    state, caller, _peer_slot, target = _setup(tmp_path)
    qid = _mine(state, target, "retry of my message")
    entry = target._queue[0]
    if shape == "kind":
        entry["kind"] = "recovery"
    else:
        entry[f"_{shape}"] = lambda *_a: None
    assert _call(state, caller)["entries"][0]["from"] == "other"
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, action="cancel", entry=qid)
    assert exc.value.code == "not_your_entry"


def test_a_requeued_steer_is_not_yours(tmp_path):
    """Mutation guard: without the steer check a requeued steer reads as yours.

    ``_requeue_unconsumed_steers`` carries the send's stamp and adds no kind or
    callback, so only ``steer_delivery_id`` tells it apart; cancelling it would
    leave its steer row promising a turn that never runs.
    """
    state, caller, _peer_slot, target = _setup(tmp_path)
    qid = target.queue_insert(
        0,
        "steer that missed the turn",
        meta={**sc.send_origin_meta(state, "chat-1"), "steer_delivery_id": "d" * 32},
        directive_user_origin=False,
    )
    assert _call(state, caller)["entries"][0]["from"] == "other"
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, action="cancel", entry=qid)
    assert exc.value.code == "not_your_entry"


def test_a_restored_id_is_redacted_before_it_is_cut(tmp_path):
    """Mutation guard: an id read off a hand-edited transcript reaches the agent raw."""
    state, caller, _peer_slot, target = _setup(tmp_path)
    _mine(state, target, "a")
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    target._queue[0]["id"] = secret
    out = _call(state, caller)
    assert secret not in json.dumps(out)


# ── the target gate ──────────────────────────────────────────────────────────


def test_the_gate_is_authorize_target(tmp_path, monkeypatch):
    state, caller, _peer_slot, _target = _setup(tmp_path)
    seen: list[dict] = []
    real = sc.authorize_target

    def _spy(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(sc, "authorize_target", _spy)
    _call(state, caller)
    assert [kw["operation"] for kw in seen] == ["queue"]
    assert seen[0]["target"] == "chat-2"


def test_a_session_the_caller_did_not_create_is_refused(tmp_path):
    """Even for an unfenced owner caller that ``authorize_target`` would admit.

    Mutation guard: dropping the creator check lists the person's queue.
    """
    state, caller, _peer_slot, target = _setup(tmp_path)
    target._created_by = ""
    _person(target, "my own words")
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller)
    assert exc.value.code == "not_creator"


def test_an_archived_session_is_not_found(tmp_path):
    state, caller, _peer_slot, _target = _setup(tmp_path)
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, target="chat-closed-long-ago")
    assert exc.value.code == "target_not_found"
    assert exc.value.status == 404


def test_an_incognito_target_is_refused(tmp_path):
    state, caller, _peer_slot, target = _setup(tmp_path)
    target.memory_mode = "incognito"
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller)
    assert exc.value.code == "ephemeral_target"


def test_a_session_cannot_manage_its_own_queue(tmp_path):
    state, caller, _peer_slot, _target = _setup(tmp_path)
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, target="chat-1")
    assert exc.value.code == "self_target"


# ── cancel ───────────────────────────────────────────────────────────────────


def test_cancel_removes_your_entry_and_tells_open_tabs(tmp_path, frames):
    state, caller, _peer_slot, target = _setup(tmp_path, frames)
    a = _mine(state, target, "stale instruction")
    b = _person(target, "keep me")
    out = _call(state, caller, action="cancel", entry=a)
    assert out["cancelled"] == a
    assert _ids(target) == [b]
    assert [e["id"] for e in out["entries"]] == [b]
    # The card's own frame, with no text for the composer to restore.
    assert ("queue_cancel", {"slot": "chat-2", "queue_id": a, "content": ""}) in frames[0]
    assert frames[1] == ["chat-2"]


@pytest.mark.parametrize("who", ["person", "peer"])
def test_cancel_refuses_an_entry_you_did_not_queue(tmp_path, frames, who):
    """Mutation guard: skipping the ownership check deletes someone else's message."""
    state, caller, _peer_slot, target = _setup(tmp_path, frames)
    other = _person(target, "mine, not yours") if who == "person" else _peer(state, target, "x")
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, action="cancel", entry=other)
    assert exc.value.code == "not_your_entry"
    assert _ids(target) == [other]
    assert frames == ([], [], [])


def test_cancel_of_an_unknown_entry_is_not_found(tmp_path):
    state, caller, _peer_slot, _target = _setup(tmp_path)
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, action="cancel", entry="nope")
    assert exc.value.code == "entry_not_found"
    assert exc.value.status == 404


def test_cancel_needs_an_entry(tmp_path):
    state, caller, _peer_slot, _target = _setup(tmp_path)
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, action="cancel")
    assert exc.value.code == "entry_required"


# ── move ─────────────────────────────────────────────────────────────────────


def test_move_earlier_past_your_own_entries(tmp_path, frames):
    state, caller, _peer_slot, target = _setup(tmp_path, frames)
    a = _mine(state, target, "old")
    b = _mine(state, target, "correction")
    out = _call(state, caller, action="move", entry=b, position=0)
    assert _ids(target) == [b, a]
    assert (out["moved"], out["position"]) == (b, 0)
    assert ("queue_reorder", {"slot": "chat-2", "order": [b, a]}) in frames[0]
    assert frames[1] == ["chat-2"]


@pytest.mark.parametrize("who", ["person", "peer"])
def test_move_earlier_may_not_pass_an_entry_you_did_not_queue(tmp_path, frames, who):
    """Mutation guard: dropping the passed-entries check jumps the queue."""
    state, caller, _peer_slot, target = _setup(tmp_path, frames)
    ahead = _person(target, "first") if who == "person" else _peer(state, target, "first")
    mine = _mine(state, target, "mine")
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, action="move", entry=mine, position=0)
    assert exc.value.code == "move_blocked"
    assert _ids(target) == [ahead, mine]
    assert frames == ([], [], [])


def test_move_later_is_always_allowed(tmp_path):
    state, caller, _peer_slot, target = _setup(tmp_path)
    mine = _mine(state, target, "can wait")
    p = _person(target, "person")
    q = _peer(state, target, "peer")
    _call(state, caller, action="move", entry=mine, position=5)
    assert _ids(target) == [p, q, mine]


def test_move_up_to_but_not_past_a_person(tmp_path):
    state, caller, _peer_slot, target = _setup(tmp_path)
    p = _person(target, "person")
    a = _mine(state, target, "a")
    b = _mine(state, target, "b")
    out = _call(state, caller, action="move", entry=b, position=1)
    assert _ids(target) == [p, b, a]
    assert out["position"] == 1


def test_move_refuses_an_entry_you_did_not_queue(tmp_path):
    state, caller, _peer_slot, target = _setup(tmp_path)
    p = _person(target, "person")
    _mine(state, target, "mine")
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, action="move", entry=p, position=1)
    assert exc.value.code == "not_your_entry"


def test_move_needs_a_position(tmp_path):
    state, caller, _peer_slot, target = _setup(tmp_path)
    a = _mine(state, target, "a")
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, action="move", entry=a)
    assert exc.value.code == "position_required"


def test_move_reseats_the_queued_rows(tmp_path):
    """The transcript's queued placeholders follow the new order too."""
    state, caller, _peer_slot, target = _setup(tmp_path)
    a = _mine(state, target, "a")
    b = _mine(state, target, "b")
    target.messages.extend(
        {"role": "queued", "content": t, "cls": json.dumps({"queue_id": q})}
        for t, q in (("a", a), ("b", b))
    )
    _call(state, caller, action="move", entry=b, position=0)
    queued = [m["content"] for m in target.messages if m.get("role") == "queued"]
    assert queued == ["b", "a"]


def test_move_reseats_an_app_twin_row_keyed_in_meta(tmp_path):
    """Mutation guard: a cls-only match leaves the app twin's row behind its peer."""
    state, caller, _peer_slot, target = _setup(tmp_path)
    a = _mine(state, target, "a")
    b = _mine(state, target, "b")
    target.messages.extend(
        [
            {"role": "queued", "content": "a", "cls": json.dumps({"queue_id": a})},
            {"role": "queued", "content": "b", "cls": "msg msg-queued", "meta": {"queueId": b}},
        ]
    )
    _call(state, caller, action="move", entry=b, position=0)
    queued = [m["content"] for m in target.messages if m.get("role") == "queued"]
    assert queued == ["b", "a"]


# ── durability ───────────────────────────────────────────────────────────────


@pytest.fixture
def save_fails(monkeypatch):
    """The change's save fails and the undo's save lands; frames and persist starts are recorded."""
    seen: list[tuple[str, dict]] = []
    started: list[str] = []
    calls: list[int] = []

    async def _not_durable(_state, _slot):
        calls.append(1)
        return len(calls) > 1

    monkeypatch.setattr(sc, "_await_queue_durable", _not_durable)
    monkeypatch.setattr(sc, "start_queue_persist", lambda _state, slot: started.append(slot.key))
    return seen, started


def _with_rows(target, *ids):
    target.messages.extend(
        {"role": "queued", "content": q, "cls": json.dumps({"queue_id": q})} for q in ids
    )


def _queued_rows(target) -> list[str]:
    return [m["content"] for m in target.messages if m.get("role") == "queued"]


def test_nothing_is_published_before_the_save_lands(tmp_path, monkeypatch):
    """Mutation guard: broadcasting first tells every tab about a cancel a restart undoes."""
    state, caller, _peer_slot, target = _setup(tmp_path)
    a = _mine(state, target, "a")
    order: list[str] = []
    state.broadcast_ws = lambda event, data: order.append(f"frame:{event}")

    async def _durable(_state, _slot):
        order.append("saved")
        return True

    monkeypatch.setattr(sc, "_await_queue_durable", _durable)
    _call(state, caller, action="cancel", entry=a)
    assert order == ["saved", "frame:queue_cancel"]


def test_a_cancel_that_is_not_saved_is_undone(tmp_path, save_fails):
    state, caller, _peer_slot, target = _setup(tmp_path, save_fails)
    a = _mine(state, target, "a")
    b = _mine(state, target, "b")
    _with_rows(target, a, b)
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, action="cancel", entry=a)
    assert exc.value.code == "queue_not_durable"
    assert _ids(target) == [a, b]
    assert _queued_rows(target) == [a, b]
    # No tab heard of it, and the restored queue was saved before the refusal.
    assert save_fails[0] == []
    assert save_fails[1] == []


def test_an_undo_that_is_not_saved_either_is_not_reported_as_nothing_changed(tmp_path, monkeypatch):
    """Mutation guard: claiming "nothing changed" while disk may still hold the cancel."""
    state, caller, _peer_slot, target = _setup(tmp_path)
    a = _mine(state, target, "a")
    b = _mine(state, target, "b")
    seen: list[tuple[str, dict]] = []
    state.broadcast_ws = lambda event, data: seen.append((event, data))
    started: list[str] = []
    saves: list[int] = []

    async def _never(_state, _slot):
        saves.append(1)
        return False

    monkeypatch.setattr(sc, "_await_queue_durable", _never)
    monkeypatch.setattr(sc, "start_queue_persist", lambda _state, slot: started.append(slot.key))
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, action="cancel", entry=a)
    assert exc.value.code == "queue_state_unknown"
    assert exc.value.status == 503
    assert saves == [1, 1]
    # Memory is back as it was, no tab heard of it, and the queue is still owed a write.
    assert _ids(target) == [a, b]
    assert seen == []
    assert started == ["chat-2"]


def test_a_move_that_is_not_saved_is_undone(tmp_path, save_fails):
    state, caller, _peer_slot, target = _setup(tmp_path, save_fails)
    a = _mine(state, target, "a")
    b = _mine(state, target, "b")
    _with_rows(target, a, b)
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, action="move", entry=b, position=0)
    assert exc.value.code == "queue_not_durable"
    assert _ids(target) == [a, b]
    assert _queued_rows(target) == [a, b]
    assert save_fails[0] == []


def test_a_move_frame_carries_the_order_as_of_the_broadcast(tmp_path, monkeypatch):
    """Mutation guard: the pre-save order would overwrite a person's drag made during the save."""
    state, caller, _peer_slot, target = _setup(tmp_path)
    a = _mine(state, target, "a")
    b = _mine(state, target, "b")
    p = _person(target, "person")
    seen: list[tuple[str, dict]] = []
    state.broadcast_ws = lambda event, data: seen.append((event, data))

    async def _person_drags_then_save(_state, slot):
        # The human reorder route mutates and broadcasts with no await of its own.
        slot._queue.insert(0, slot._queue.pop([i["id"] for i in slot._queue].index(p)))
        return True

    monkeypatch.setattr(sc, "_await_queue_durable", _person_drags_then_save)
    _call(state, caller, action="move", entry=b, position=0)
    assert _ids(target) == [p, b, a]
    assert seen == [("queue_reorder", {"slot": "chat-2", "order": [p, b, a]})]


def test_a_target_closed_during_the_save_reports_the_change_applied(tmp_path, monkeypatch, frames):
    """Mutation guard: the re-gate's own "not found" would say nothing happened."""
    state, caller, _peer_slot, target = _setup(tmp_path, frames)
    a = _mine(state, target, "a")
    audits: list[str] = []
    monkeypatch.setattr(sc, "_audit", lambda **kw: audits.append(kw["outcome"]))

    async def _close_then_save(_state, slot):
        del state._slots["chat-2"]
        return True

    monkeypatch.setattr(sc, "_await_queue_durable", _close_then_save)
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, action="cancel", entry=a)
    assert exc.value.code == "target_changed"
    assert frames[0][0][0] == "queue_cancel"
    # The committed change is audited as allowed, once.
    assert audits == ["allowed"]


def test_a_successful_change_is_audited_allowed_once(tmp_path, monkeypatch, frames):
    state, caller, _peer_slot, target = _setup(tmp_path, frames)
    a = _mine(state, target, "a")
    audits: list[str] = []
    monkeypatch.setattr(sc, "_audit", lambda **kw: audits.append(kw["outcome"]))
    _call(state, caller, action="cancel", entry=a)
    assert audits == ["allowed"]


def test_a_malformed_restored_queue_id_does_not_break_the_reorder():
    """Mutation guard: a list-valued id raised TypeError after ``_queue`` was re-seated."""
    from kiro_crew.dashboard.chat_utils import _reorder_queued_rows

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "queued", "content": "bad", "meta": {"queueId": ["x"]}},
        {"role": "queued", "content": "badcls", "cls": json.dumps({"queue_id": ["y"]})},
        {"role": "queued", "content": "b", "cls": json.dumps({"queue_id": "b"})},
        {"role": "queued", "content": "a", "meta": {"queueId": "a"}},
    ]
    _reorder_queued_rows(messages, ["a", "b"])
    assert [m["content"] for m in messages] == ["hi", "a", "b", "bad", "badcls"]


def test_the_undo_keeps_an_entry_queued_while_the_save_ran(tmp_path, monkeypatch):
    """The undo works on the entry, not a snapshot: a send that landed mid-save stays."""
    state, caller, _peer_slot, target = _setup(tmp_path)
    a = _mine(state, target, "a")
    late: list[str] = []

    async def _send_lands_then_fail(_state, slot):
        if not late:
            late.append(_person(slot, "late"))
        return False

    monkeypatch.setattr(sc, "_await_queue_durable", _send_lands_then_fail)
    monkeypatch.setattr(sc, "start_queue_persist", lambda _state, _slot: None)
    with pytest.raises(sc.SessionControlError):
        _call(state, caller, action="cancel", entry=a)
    assert _ids(target) == [a, late[0]]


def test_a_moved_entry_drained_during_a_failed_save_is_not_reported_undone(tmp_path, monkeypatch):
    """The drain ran the entry where the caller put it, so the move did take effect."""
    state, caller, _peer_slot, target = _setup(tmp_path)
    a = _mine(state, target, "a")
    b = _mine(state, target, "b")

    async def _drained_then_fail(_state, slot):
        slot.queue_remove_by_id(b)  # the drain starts the entry mid-save
        return False

    monkeypatch.setattr(sc, "_await_queue_durable", _drained_then_fail)
    monkeypatch.setattr(sc, "start_queue_persist", lambda _state, _slot: None)
    out = _call(state, caller, action="move", entry=b, position=0)
    assert out["moved"] == b
    assert _ids(target) == [a]


def test_the_listing_is_reauthorized_after_the_save(tmp_path, monkeypatch, frames):
    """Mutation guard: a slot swapped in during the save would get the old gate's listing."""
    state, caller, _peer_slot, target = _setup(tmp_path, frames)
    a = _mine(state, target, "a")
    _mine(state, target, "b")

    async def _swap_then_save(_state, slot):
        replacement = state.get_or_create_slot("chat-9")
        replacement._created_by = "chat-1"
        state._slots["chat-2"] = replacement
        return True

    monkeypatch.setattr(sc, "_await_queue_durable", _swap_then_save)
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, action="cancel", entry=a)
    assert exc.value.code == "target_changed"
    # The change was saved, so the tabs still hear it.
    assert frames[0][0][0] == "queue_cancel"


def test_the_second_gate_reads_a_warmed_config(tmp_path, monkeypatch, frames):
    """Mutation guard: a cold re-gate after the save loads the config on the loop."""
    state, caller, _peer_slot, target = _setup(tmp_path, frames)
    a = _mine(state, target, "a")
    calls: list[str] = []
    real_authorize = sc.authorize_target

    async def _prewarm():
        calls.append("prewarm")

    def _authorize(*args, **kwargs):
        calls.append("gate")
        return real_authorize(*args, **kwargs)

    monkeypatch.setattr(sc, "prewarm_enabled_check", _prewarm)
    monkeypatch.setattr(sc, "authorize_target", _authorize)
    _call(state, caller, action="cancel", entry=a)
    # The first gate is warmed by the handler; the one after the save by the verb.
    assert calls == ["gate", "prewarm", "gate"]


def test_an_undone_cancel_stays_behind_what_was_ahead_of_it(tmp_path, monkeypatch):
    """Mutation guard: restoring at the old index puts it ahead of a person's message.

    A recovery entry goes in at the front during the save. Index 1 is then the
    person's slot, so an index-based undo lands the caller's entry ahead of it.
    """
    state, caller, _peer_slot, target = _setup(tmp_path)
    p = _person(target, "person")
    a = _mine(state, target, "a")
    b = _mine(state, target, "b")
    front: list[str] = []

    async def _recovery_then_fail(_state, slot):
        if not front:
            front.append(slot.queue_insert(0, "recovery", directive_user_origin=False))
        return False

    monkeypatch.setattr(sc, "_await_queue_durable", _recovery_then_fail)
    monkeypatch.setattr(sc, "start_queue_persist", lambda _state, _slot: None)
    with pytest.raises(sc.SessionControlError):
        _call(state, caller, action="cancel", entry=a)
    assert _ids(target) == [front[0], p, a, b]


def test_an_undone_move_goes_back_between_its_old_neighbours(tmp_path, monkeypatch):
    state, caller, _peer_slot, target = _setup(tmp_path)
    p = _person(target, "person")
    a = _mine(state, target, "a")
    b = _mine(state, target, "b")
    front: list[str] = []

    async def _recovery_then_fail(_state, slot):
        if not front:
            front.append(slot.queue_insert(0, "recovery", directive_user_origin=False))
        return False

    monkeypatch.setattr(sc, "_await_queue_durable", _recovery_then_fail)
    monkeypatch.setattr(sc, "start_queue_persist", lambda _state, _slot: None)
    with pytest.raises(sc.SessionControlError):
        _call(state, caller, action="move", entry=b, position=1)
    assert _ids(target) == [front[0], p, a, b]


def test_an_undone_head_entry_still_follows_a_recovery_put_in_front(tmp_path, monkeypatch):
    """With nothing ahead of it, the entry goes back before its old successor."""
    state, caller, _peer_slot, target = _setup(tmp_path)
    a = _mine(state, target, "a")
    b = _mine(state, target, "b")
    front: list[str] = []

    async def _recovery_then_fail(_state, slot):
        if not front:
            front.append(slot.queue_insert(0, "recovery", directive_user_origin=False))
        return False

    monkeypatch.setattr(sc, "_await_queue_durable", _recovery_then_fail)
    monkeypatch.setattr(sc, "start_queue_persist", lambda _state, _slot: None)
    with pytest.raises(sc.SessionControlError):
        _call(state, caller, action="cancel", entry=a)
    assert _ids(target) == [front[0], a, b]


def test_the_durability_check_reads_the_committed_queue(tmp_path, monkeypatch):
    """A real save clears ``queue_persist_pending``; one that stays skipped does not."""
    monkeypatch.setattr(sc, "QUEUE_DURABLE_SKIP_WAIT_SECS", 0.1)
    state, _caller, _peer_slot, target = _setup(tmp_path)
    _mine(state, target, "a")
    target.messages.append({"role": "user", "content": "hello"})
    assert asyncio.run(sc._await_queue_durable(state, target)) is True
    assert not target.queue_persist_pending

    _mine(state, target, "b")
    target._metadata_persist_inflight = 1  # flush_slot_now skips this slot
    try:
        assert asyncio.run(sc._await_queue_durable(state, target)) is False
    finally:
        target._metadata_persist_inflight = 0


def test_an_undo_is_checked_against_a_real_transcript_save(tmp_path, monkeypatch):
    """The rollback path end to end on the real save: the change's writes are skipped,
    the undo's write lands, and only then is the call refused ``queue_not_durable``."""
    state, caller, _peer_slot, target = _setup(tmp_path)
    a = _mine(state, target, "a")
    b = _mine(state, target, "b")
    target.messages.append({"role": "user", "content": "hello"})
    assert asyncio.run(sc._await_queue_durable(state, target)) is True
    monkeypatch.setattr(sc, "QUEUE_DURABLE_RETRY_SECS", 0)
    real_flush = state.flush_slot_now
    calls: list[int] = []

    def _flush(slot):
        calls.append(1)
        if len(calls) <= sc.QUEUE_DURABLE_ATTEMPTS:
            return None  # the change's writes never land
        return real_flush(slot)

    state.flush_slot_now = _flush
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, action="cancel", entry=a)
    assert exc.value.code == "queue_not_durable"
    assert _ids(target) == [a, b]
    assert len(calls) == sc.QUEUE_DURABLE_ATTEMPTS + 1
    assert not target.queue_persist_pending


def test_a_write_skipped_by_a_metadata_guard_waits_instead_of_failing(tmp_path):
    """Mutation guard: a 0.1 s budget rolled back a healthy cancel during a folder move."""
    state, _caller, _peer_slot, target = _setup(tmp_path)
    _mine(state, target, "a")
    target.messages.append({"role": "user", "content": "hello"})
    target._metadata_persist_inflight = 1

    async def _run() -> bool:
        async def _clear_soon() -> None:
            await asyncio.sleep(0.3)  # longer than 3 attempts x 0.05 s of retries
            target._metadata_persist_inflight = 0

        clearer = asyncio.create_task(_clear_soon())
        try:
            return await sc._await_queue_durable(state, target)
        finally:
            await clearer

    assert asyncio.run(_run()) is True
    assert not target.queue_persist_pending


def test_a_slot_with_no_transcript_has_nothing_to_wait_for(tmp_path):
    state, _caller, _peer_slot, target = _setup(tmp_path)
    _mine(state, target, "a")
    assert target.messages == []
    assert asyncio.run(sc._await_queue_durable(state, target)) is True


def test_an_unknown_action_is_refused(tmp_path):
    state, caller, _peer_slot, _target = _setup(tmp_path)
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, action="edit")
    assert exc.value.code == "bad_action"


# ── schema ───────────────────────────────────────────────────────────────────


def test_the_schema_bounds_the_arguments():
    assert validate_tool_args({"target": "chat-2"}, SESSION_QUEUE_SCHEMA)["action"] == "list"
    for bad in (
        {"target": "chat-2", "action": "edit"},
        {"target": "chat-2", "entry": "has space"},
        {"target": "chat-2", "position": -1},
    ):
        with pytest.raises(ValidationError):
            validate_tool_args(bad, SESSION_QUEUE_SCHEMA)


# ── the route ────────────────────────────────────────────────────────────────


def _request(state, body: dict, *, internal: bool):
    caller = state.get_or_create_slot("chat-1")
    request = MagicMock()
    request.app = {"state": state}
    request.path = "/api/session-control/queue"
    request.method = "POST"
    request.headers = {"X-Session-Key": slot_history_key(caller)}

    async def _json():
        return body

    request.json = _json
    request.get = lambda key, default=None: (
        True if (key in ("internal_auth", "peer_verified") and internal) else default
    )
    return request


def test_the_route_refuses_a_cookie_only_caller(tmp_path):
    state, _caller, _peer_slot, _target = _setup(tmp_path)
    req = _request(state, {"target": "chat-2"}, internal=False)
    resp = asyncio.run(handlers_sc.api_session_control_queue(req))
    assert resp.status == 403


def test_the_route_serves_an_internal_caller(tmp_path, frames):
    state, _caller, _peer_slot, target = _setup(tmp_path, frames)
    a = _mine(state, target, "stale")
    req = _request(state, {"target": "chat-2", "action": "cancel", "entry": a}, internal=True)
    resp = asyncio.run(handlers_sc.api_session_control_queue(req))
    assert resp.status == 200
    body = json.loads(resp.body.decode())
    assert body["cancelled"] == a
    assert target._queue == []


def test_the_route_maps_a_refusal(tmp_path):
    state, _caller, _peer_slot, target = _setup(tmp_path)
    p = _person(target, "person")
    req = _request(state, {"target": "chat-2", "action": "cancel", "entry": p}, internal=True)
    resp = asyncio.run(handlers_sc.api_session_control_queue(req))
    assert resp.status == 403
    assert json.loads(resp.body.decode())["code"] == "not_your_entry"


def test_the_route_refuses_a_non_integer_position(tmp_path):
    state, _caller, _peer_slot, _target = _setup(tmp_path)
    req = _request(state, {"target": "chat-2", "action": "move", "position": True}, internal=True)
    resp = asyncio.run(handlers_sc.api_session_control_queue(req))
    assert resp.status == 400


def test_the_route_is_a_strict_internal_path():
    from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS

    assert "/api/session-control/queue" in _STRICT_INTERNAL_API_PATHS


# ── the MCP tool ─────────────────────────────────────────────────────────────


def test_the_tool_posts_the_verified_key_to_the_queue_route():
    body = {
        "target": "chat-2",
        "title": "w",
        "running": True,
        "count": 1,
        "cancelled": "q1",
        "entries": [{"id": "q2", "position": 0, "from": "person", "excerpt": "hi"}],
    }
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch("kiro_crew.mcp_dashboard._post", return_value=body) as post,
    ):
        out = _call_tool_inner(
            "session_queue", {"target": "chat-2", "action": "cancel", "entry": "q1"}
        )
    (path, sent), kwargs = post.call_args
    assert path == "/api/session-control/queue"
    assert sent == {"target": "chat-2", "action": "cancel", "entry": "q1"}
    assert kwargs["session_key"] == _VERIFIED
    assert "Cancelled queued entry q1." in out
    assert '0  q2  person  "hi"' in out


def test_the_tool_refuses_an_unverifiable_caller():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=""),
        patch("kiro_crew.mcp_dashboard._post") as post,
    ):
        out = _call_tool_inner("session_queue", {"target": "chat-2"})
    assert out.startswith("Error")
    post.assert_not_called()


def test_the_tool_reports_a_refusal_as_an_error():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch("kiro_crew.mcp_dashboard._post", return_value={"error": "not yours"}),
    ):
        out = _call_tool_inner(
            "session_queue", {"target": "chat-2", "action": "move", "entry": "q", "position": 0}
        )
    assert out == "Error: could not move that session's queue: not yours"


def test_render_an_empty_queue_and_the_omission_count():
    assert "The queue is empty." in _render_session_queue({"target": "t", "count": 0})
    out = _render_session_queue(
        {
            "target": "t",
            "count": 60,
            "omitted": 10,
            "entries": [{"id": "q", "position": 0, "from": "yours", "excerpt": "a\nb"}],
        }
    )
    assert '0  q  yours   "a b"' in out
    assert "(10 more queued entries not shown.)" in out


def test_render_keeps_each_entry_on_one_line_whatever_its_breaks():
    """Mutation guard: a CR or U+2028 in untrusted text forged an extra listing row."""
    forged = 'x\r1  q2  yours   "fake"\r\nz\u2028w'
    out = _render_session_queue(
        {
            "target": "t",
            "count": 1,
            "entries": [{"id": "q\r1", "position": 0, "from": "person", "excerpt": forged}],
        }
    )
    rows = out.split("\n")
    # One header plus exactly one entry row, however the text breaks.
    assert len(rows) == 2
    assert "\r" not in out and "\u2028" not in out
    assert rows[1].startswith("0  q 1  person")


def test_render_keeps_the_header_on_one_line_whatever_the_title_holds():
    """Mutation guard: a CR or newline in an editable title forged listing rows."""
    out = _render_session_queue(
        {
            "target": "t\r\n0  q  yours",
            "title": 'real\n0  forged  yours   "x"\u2028more',
            "count": 0,
        }
    )
    rows = out.split("\n")
    assert len(rows) == 2  # the header and the empty-queue line
    assert "\r" not in out and "\u2028" not in out
    assert rows[1] == "The queue is empty."


def test_a_listed_title_is_bounded(tmp_path):
    state, caller, _peer_slot, target = _setup(tmp_path)
    target.title = "t" * 5000
    target._titled = True
    out = _call(state, caller)
    assert len(out["title"]) == sc.MAX_SESSION_STATUS_TITLE_CHARS
