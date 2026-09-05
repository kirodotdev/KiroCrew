"""``session_send target="user"`` — escalating to the human as a peer.

Pins the contract in ``docs/system-specs/modules/session-control.md`` §
"Escalating to the human":

* delivery lands an ``escalation`` row in the OWNING member's DM thread and
  never starts a turn (non-blocking);
* the conversation index gains a pending record, ``needs_you`` is projected
  on the member slot and the roster, and clears on the human's reply or when
  the deadline passes;
* the deadline window is validated, the card is mirrored onto the bell bus
  with a per-goal ``group_key``, and one SEL line is written.

Member-slot file IO goes to ``$KIROCREW_HOME`` isolated by the autouse
``_isolate_kirocrew_home`` fixture.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew import crew_conversation as conv
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.dashboard.slot_projection import _member_needs_you
from kiro_crew.members import member_slot_key

MEMBER = "Radar"
SLUG = "radar"


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture(autouse=True)
def _no_turns(monkeypatch):
    """Escalation must never start a turn: make any attempt loud."""

    async def _boom(*_a, **_k):  # pragma: no cover - the assertion is that it is not called
        raise AssertionError("escalation started a turn")

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _boom)


def _member_slot(state):
    return state.get_or_create_slot(member_slot_key(SLUG), agent=MEMBER, mode="member")


def _key(slot) -> str:
    return slot_history_key(slot)


def _escalate(state, caller, **kw):
    async def _drive():
        out = await sc.send_to_target(
            state,
            caller_session_key=_key(caller),
            target=kw.pop("target", "user"),
            message=kw.pop(
                "message", "## Blocked on prod access\n\nTried X and Y.\n\nNeed: grant me role Z."
            ),
            **kw,
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return out

    return asyncio.run(_drive())


# ── Delivery ─────────────────────────────────────────────────────────────────


def test_member_escalation_lands_in_its_own_thread_as_a_card(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    before = len(member.messages)

    out = _escalate(
        state,
        member,
        deadline="30m",
        default_action="Push A",
        options=["Push A", "Hold"],
        goal="triage",
    )

    assert out["ok"] is True
    assert out["target"] == member.key
    assert out["escalation_id"].startswith("esc-")
    assert out["deadline"]
    assert "started" not in out  # nothing ran
    row = member.messages[-1]
    assert len(member.messages) == before + 1
    assert row["role"] == sc.ESCALATION_ROLE
    assert row["cls"] == sc.ESCALATION_CLS
    assert row["content"].startswith("## Blocked on prod access")
    meta = row["meta"]
    assert meta["kind"] == "escalation"
    assert meta["from_session"] == member.key
    assert meta["deadline"] == out["deadline"]
    assert meta["default_action"] == "Push A"
    assert meta["options"] == ["Push A", "Hold"]
    assert meta["goal"] == "triage"
    assert meta["state"] == "pending"
    assert meta["mid"]


def test_target_user_is_case_and_space_insensitive(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    out = _escalate(state, member, target="  User ")
    assert out["target"] == member.key


def test_session_titled_user_makes_the_reserved_target_ambiguous(tmp_path):
    """Title resolution is a documented way to address a session; the reserved
    word must not silently swallow a message meant for a session called 'user'."""
    state = _make_state(tmp_path)
    member = _member_slot(state)
    other = state.get_or_create_slot("chat-2")
    other.title = "user"
    with pytest.raises(sc.SessionControlError) as exc:
        _escalate(state, member)
    assert exc.value.code == "ambiguous_target"
    assert exc.value.status == 409
    assert member.messages == [] and other.messages == []


def test_reserved_target_collision_is_checked_only_after_the_caller_is_authorized(tmp_path):
    """The ambiguity refusal's shape reveals that a session titled 'user'
    exists; an ineligible caller gets its own refusal and learns nothing."""
    state = _make_state(tmp_path)
    other = state.get_or_create_slot("chat-2")
    other.title = "user"
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(
            sc.send_to_target(
                state, caller_session_key="dashboard:ghost", target="user", message="x"
            )
        )
    assert exc.value.code == "caller_unidentified"


def test_worker_created_by_a_member_escalates_into_that_members_thread(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    worker = state.get_or_create_slot("chat-7")
    worker._created_by = member.key

    out = _escalate(state, worker)

    assert out["target"] == member.key
    assert out["member"] == MEMBER
    assert member.messages[-1]["role"] == sc.ESCALATION_ROLE
    assert member.messages[-1]["meta"]["from_session"] == "chat-7"
    assert worker.messages == []


def test_plain_session_escalates_into_its_own_transcript_without_member_index(tmp_path):
    state = _make_state(tmp_path)
    plain = state.get_or_create_slot("chat-3")

    out = _escalate(state, plain)

    assert out["target"] == "chat-3"
    assert out["member"] == ""
    assert plain.messages[-1]["role"] == sc.ESCALATION_ROLE
    assert not conv.conversation_path(SLUG).exists()


def test_unknown_caller_is_refused(tmp_path):
    state = _make_state(tmp_path)
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(
            sc.send_to_target(
                state, caller_session_key="dashboard:ghost", target="user", message="x"
            )
        )
    assert exc.value.code == "caller_unidentified"


def test_caller_gates_apply_like_any_other_send(tmp_path, monkeypatch):
    """The human is not a session, but the CALLER still is: every caller-side
    refusal of the peer path holds here with the same code."""
    state = _make_state(tmp_path)
    plain = state.get_or_create_slot("chat-3")

    monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
    with pytest.raises(sc.SessionControlError) as exc:
        _escalate(state, plain)
    assert exc.value.code == "session_control_disabled"
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)

    # A member DM slot keeps its bypass of the config switch.
    monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
    member = _member_slot(state)
    assert _escalate(state, member)["ok"] is True
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)

    ephemeral = state.get_or_create_slot("chat-4")
    ephemeral.memory_mode = "incognito"
    with pytest.raises(sc.SessionControlError) as exc:
        _escalate(state, ephemeral)
    assert exc.value.code == "ephemeral_caller"
    assert ephemeral.messages == []

    app_slot = state.get_or_create_slot("chat-5")
    app_slot._app = "some-app"
    with pytest.raises(sc.SessionControlError) as exc:
        _escalate(state, app_slot)
    assert exc.value.code == "app_scoped_caller"


def test_caller_denials_are_audited_under_escalate(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    logged = []
    fake_sel = MagicMock()
    fake_sel.log_api_access = lambda **kw: logged.append(kw)
    monkeypatch.setattr(sc, "sel", lambda: fake_sel)
    with pytest.raises(sc.SessionControlError):
        asyncio.run(
            sc.send_to_target(
                state, caller_session_key="dashboard:ghost", target="user", message="x"
            )
        )
    (entry,) = [kw for kw in logged if kw["operation"] == "session_control.escalate"]
    assert entry["outcome"] == "denied"
    assert entry["resources"] == "target=user:caller_unidentified"


def test_post_authorization_refusals_are_audited_too(tmp_path, monkeypatch):
    """Routing and post-await refusals go through the same audited door as the
    caller gates: a workspace_mismatch or target_gone leaves a SEL denial."""
    state = _make_state(tmp_path)
    member = _member_slot(state)
    worker = state.get_or_create_slot("chat-7")
    worker._created_by = member.key
    worker.workspace = "other"
    logged = []
    fake_sel = MagicMock()
    fake_sel.log_api_access = lambda **kw: logged.append(kw)
    monkeypatch.setattr(sc, "sel", lambda: fake_sel)
    with pytest.raises(sc.SessionControlError) as exc:
        _escalate(state, worker)
    assert exc.value.code == "workspace_mismatch"
    codes = [kw["resources"] for kw in logged if kw["operation"] == "session_control.escalate"]
    assert codes == ["target=user:workspace_mismatch"]


def test_index_record_exists_before_the_card_is_surfaced(tmp_path, monkeypatch):
    """A reply that races the card must find the record pending: the index write
    precedes the append, under the id the row will carry."""
    state = _make_state(tmp_path)
    member = _member_slot(state)
    order: list[str] = []
    real_append = type(member).append

    def _spy_append(self, *a, **kw):
        record = conv.read_conversation(SLUG)
        order.append(f"append(pending={len(conv.pending_escalations(record))})")
        return real_append(self, *a, **kw)

    monkeypatch.setattr(type(member), "append", _spy_append)
    out = _escalate(state, member)
    assert order == ["append(pending=1)"]
    (entry,) = conv.read_conversation(SLUG)["entries"]
    assert entry["mid"] == member.messages[-1]["meta"]["mid"]
    assert entry["id"] == out["escalation_id"]


def test_worker_result_says_where_the_reply_lands(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    worker = state.get_or_create_slot("chat-7")
    worker._created_by = member.key
    assert _escalate(state, worker)["reply_in_caller_thread"] is False
    assert _escalate(state, member)["reply_in_caller_thread"] is True


def test_worker_in_another_workspace_cannot_reach_its_creators_thread(tmp_path):
    """Same boundary the peer path enforces (`workspace_mismatch`)."""
    state = _make_state(tmp_path)
    member = _member_slot(state)
    worker = state.get_or_create_slot("chat-7")
    worker._created_by = member.key
    worker.workspace = "other"
    with pytest.raises(sc.SessionControlError) as exc:
        _escalate(state, worker)
    assert exc.value.code == "workspace_mismatch"
    assert member.messages == []
    assert worker.messages == []
    assert not conv.conversation_path(SLUG).exists()


def test_worker_whose_owner_thread_is_closed_is_refused_not_misrouted(tmp_path):
    """A card in a worker nobody reads, with no index record and no badge,
    would look delivered and be lost — refuse instead."""
    state = _make_state(tmp_path)
    worker = state.get_or_create_slot("chat-7")
    worker._created_by = member_slot_key(SLUG)  # member thread is not open
    with pytest.raises(sc.SessionControlError) as exc:
        _escalate(state, worker)
    assert exc.value.code == "target_gone"
    assert worker.messages == []


def test_two_chip_replies_drained_as_one_row_answer_both_records(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    a = _escalate(state, member, goal="a")
    b = _escalate(state, member, goal="b")
    # The queue drain merges two chip replies into one user row carrying both ids.
    member.append(
        "user",
        "Close it\n\nKeep it open",
        "msg msg-u",
        meta={"escalation_ids": [a["escalation_id"], b["escalation_id"]], "human_reply": True},
    )
    states = {e["id"]: e["state"] for e in conv.read_conversation(SLUG)["entries"]}
    assert states == {a["escalation_id"]: "answered", b["escalation_id"]: "answered"}
    assert _member_needs_you(member) is False


def test_peer_delivered_user_row_does_not_answer(tmp_path):
    """A peer's session_send lands as a user row too; it is not the human."""
    state = _make_state(tmp_path)
    member = _member_slot(state)
    _escalate(state, member)
    member.append("user", sc._SEND_PROVENANCE.format(caller="chat-9") + "hello", "msg msg-u")
    assert _member_needs_you(member) is True
    assert conv.read_conversation(SLUG)["entries"][0]["state"] == "pending"


def test_automated_prompt_user_row_does_not_answer(tmp_path):
    """A heartbeat / cron ``prompt:`` into the member slot appends a plain user
    row with no human-reply provenance; it must not retire the escalation."""
    state = _make_state(tmp_path)
    member = _member_slot(state)
    _escalate(state, member)
    member.append("user", "[heartbeat] anything to report?", "msg msg-u")
    member.append("user", "cron says hi", "msg msg-u", meta={"injectKind": "cron"})
    assert _member_needs_you(member) is True
    assert conv.read_conversation(SLUG)["entries"][0]["state"] == "pending"
    # A client cannot mint the provenance itself: the handler stamps it, and the
    # hook trusts only a literal True.
    member.append("user", "spoof", "msg msg-u", meta={"human_reply": "yes"})
    assert _member_needs_you(member) is True


def test_human_reply_provenance_requires_owner_dashboard_identity():
    """Two positive signals earn the stamp: a validated dashboard credential AND
    the owner identity. An internal-secret caller (no app, is_dashboard_user
    False/absent) is automation; a non-owner dashboard user (an allowed Slack
    user holding a dashboard credential) may post but is not the human the
    escalation was raised to."""
    from types import SimpleNamespace

    from kiro_crew.dashboard.chat_handlers import _human_reply_provenance

    class _Req(dict):
        def __init__(self, owner_id, **kw):
            super().__init__(**kw)
            self.app = {"state": SimpleNamespace(owner_id=owner_id)}

    assert _human_reply_provenance(_Req("alice", is_dashboard_user=True, app="", user="alice"))
    assert not _human_reply_provenance(_Req("alice", is_dashboard_user=True, app="", user="bob"))
    assert not _human_reply_provenance(_Req("alice", is_dashboard_user=False, app="", user="alice"))
    assert not _human_reply_provenance(_Req("alice", app="", user="alice"))
    assert not _human_reply_provenance(_Req("alice", is_dashboard_user="yes", app="", user="alice"))
    # Owner identity without the dashboard-user signal is not enough either.
    assert not _human_reply_provenance(_Req("alice", app="", user="alice"))


def test_free_text_counts_only_records_pending_when_it_was_typed(tmp_path):
    """The reply's candidates are snapshotted at append time: a record that
    lands after the reply (executor ordering) is neither counted nor answered."""
    state = _make_state(tmp_path)
    member = _member_slot(state)
    first = _escalate(state, member)
    # Simulate the race: a second record reaches the index before the deferred
    # mark for a reply that was typed while only the first was pending.
    conv.record_escalation(
        SLUG,
        member=MEMBER,
        session_key=member.key,
        mid="m-late",
        escalation_id="esc-late",
        from_session=member.key,
    )
    moved = conv.mark_answered(SLUG, candidates=[first["escalation_id"]])
    assert moved == 1
    states = {e["id"]: e["state"] for e in conv.read_conversation(SLUG)["entries"]}
    assert states[first["escalation_id"]] == "answered"
    assert states["esc-late"] == "pending"


def test_append_failure_retracts_the_index_record(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    member = _member_slot(state)

    def _boom(*_a, **_k):
        raise RuntimeError("append exploded")

    monkeypatch.setattr(type(member), "append", _boom)
    with pytest.raises(RuntimeError):
        _escalate(state, member)
    assert conv.read_conversation(SLUG)["entries"] == []
    assert _member_needs_you(member) is False


def test_reply_that_does_not_persist_does_not_answer(tmp_path):
    """The answered mark derives from the reply row, so the row is made durable
    first; a save that does not commit leaves the record pending."""
    state = _make_state(tmp_path)
    member = _member_slot(state)
    _escalate(state, member)
    calls: list[str] = []

    async def _no_save():
        calls.append("persist")
        return False

    member._persist_for_escalation = _no_save
    member.append("user", "Go with A", "msg msg-u", meta={"human_reply": True})
    assert calls == ["persist"]
    assert conv.read_conversation(SLUG)["entries"][0]["state"] == "pending"
    assert _member_needs_you(member) is True

    async def _saves():
        calls.append("persist")
        return True

    member._persist_for_escalation = _saves
    member.append("user", "Go with A, really", "msg msg-u", meta={"human_reply": True})
    assert conv.read_conversation(SLUG)["entries"][0]["state"] == "answered"


def test_thread_closing_during_delivery_retracts_and_refuses(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    real_record = conv.record_escalation

    def _record_then_close(*a, **kw):
        out = real_record(*a, **kw)
        state._slots.pop(member.key, None)  # the thread closes mid-delivery
        return out

    monkeypatch.setattr(sc.conv, "record_escalation", _record_then_close)
    with pytest.raises(sc.SessionControlError) as exc:
        _escalate(state, member)
    assert exc.value.code == "target_gone"
    assert conv.read_conversation(SLUG)["entries"] == []


# ── Index, needs_you, lifecycle ──────────────────────────────────────────────


def test_index_gets_a_pending_record_and_needs_you_projects(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    assert _member_needs_you(member) is False

    out = _escalate(state, member, goal="triage")

    record = conv.read_conversation(SLUG)
    (entry,) = record["entries"]
    assert entry["type"] == "escalation"
    assert entry["id"] == out["escalation_id"]
    assert entry["mid"] == member.messages[-1]["meta"]["mid"]
    assert entry["session_key"] == member.key
    assert entry["state"] == "pending"
    assert record["participants"][1] == {"kind": "member", "slug": SLUG, "name": MEMBER}
    assert _member_needs_you(member) is True


def test_human_reply_in_the_thread_clears_needs_you(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    _escalate(state, member)
    assert _member_needs_you(member) is True

    # The composer path: a live user row appended to the member DM slot.
    member.append("user", "Go with A", "msg msg-u", meta={"human_reply": True})

    assert _member_needs_you(member) is False
    assert conv.read_conversation(SLUG)["entries"][0]["state"] == "answered"


def test_replayed_user_row_does_not_answer(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    _escalate(state, member)
    member.append("user", "old row", "msg msg-u", broadcast=False, meta={"human_reply": True})
    assert _member_needs_you(member) is True


def test_free_text_with_two_pending_answers_neither_but_a_scoped_reply_answers_one(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    first = _escalate(state, member, goal="a")
    second = _escalate(state, member, goal="b")

    member.append("user", "how is it going?", "msg msg-u", meta={"human_reply": True})
    states = {e["id"]: e["state"] for e in conv.read_conversation(SLUG)["entries"]}
    assert states == {first["escalation_id"]: "pending", second["escalation_id"]: "pending"}
    assert _member_needs_you(member) is True

    # The option chip carries the id on the user row's meta.
    member.append(
        "user",
        "Keep it open",
        "msg msg-u",
        meta={"escalation_id": second["escalation_id"], "human_reply": True},
    )
    states = {e["id"]: e["state"] for e in conv.read_conversation(SLUG)["entries"]}
    assert states[second["escalation_id"]] == "answered"
    assert states[first["escalation_id"]] == "pending"
    assert _member_needs_you(member) is True


def test_reply_pushes_a_slots_update_only_when_something_cleared(tmp_path):
    """The badge must clear WITH the reply, not at the next unrelated push."""
    state = _make_state(tmp_path)
    member = _member_slot(state)
    pushed = MagicMock()
    member._on_escalation_answered = pushed

    member.append("user", "nothing pending yet", "msg msg-u", meta={"human_reply": True})
    pushed.assert_not_called()

    _escalate(state, member)
    member.append("user", "Go with A", "msg msg-u", meta={"human_reply": True})
    pushed.assert_called_once()


def test_non_member_slot_never_needs_you(tmp_path):
    state = _make_state(tmp_path)
    plain = state.get_or_create_slot("chat-3")
    _escalate(state, plain)
    assert _member_needs_you(plain) is False


def test_deadline_passing_clears_needs_you(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    _escalate(state, member, deadline="60s", default_action="Push A")
    assert _member_needs_you(member) is True

    from datetime import datetime, timedelta, timezone

    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    assert conv.needs_you(SLUG, now=later) is False
    record = conv.read_conversation(SLUG)
    conv.sweep_deadlines(record, now=later)
    assert record["entries"][0]["state"] == "defaulted"


# ── Validation ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["10s", "9d", "soon"])
def test_bad_deadline_is_refused_before_anything_lands(tmp_path, bad):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    with pytest.raises(sc.SessionControlError) as exc:
        _escalate(state, member, deadline=bad)
    assert exc.value.code == "deadline_invalid"
    assert exc.value.status == 400
    assert member.messages == []
    assert not conv.conversation_path(SLUG).exists()


def test_too_many_options_refused(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    with pytest.raises(sc.SessionControlError) as exc:
        _escalate(state, member, options=[f"o{i}" for i in range(7)])
    assert exc.value.code == "options_too_many"


def test_options_are_deduplicated_and_blank_dropped(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    _escalate(state, member, options=["A", " ", "A", "B"])
    assert member.messages[-1]["meta"]["options"] == ["A", "B"]


def test_no_deadline_means_open_ended(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    out = _escalate(state, member)
    assert out["deadline"] is None
    assert member.messages[-1]["meta"]["deadline"] is None


# ── Mirror + audit ───────────────────────────────────────────────────────────


def test_mirror_goes_to_the_bell_bus_with_a_per_goal_group_key(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    pushed = []
    state.notification_bus = MagicMock(push=lambda p: pushed.append(p))

    _escalate(state, member, goal="triage", deadline="30m", default_action="Push A")

    (payload,) = pushed
    assert payload.channel == "system.agent"
    assert payload.kind == "escalation"
    assert payload.group_key == f"escalation:{SLUG}:triage"
    assert payload.title == f"{MEMBER} needs you"
    assert payload.url == f"/members?member={MEMBER}"
    assert "Blocked on prod access" in payload.body
    assert "Push A" in payload.body


def test_mirror_failure_does_not_fail_delivery(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    state.notification_bus = MagicMock(push=MagicMock(side_effect=RuntimeError("bus down")))
    out = _escalate(state, member)
    assert out["ok"] is True
    assert member.messages[-1]["role"] == sc.ESCALATION_ROLE


def test_index_write_failure_refuses_and_surfaces_no_card(tmp_path, monkeypatch):
    """The index IS the lifecycle: without it the card would report a delivery
    whose badge, deadline and reply tracking never exist. Refuse instead."""
    state = _make_state(tmp_path)
    member = _member_slot(state)

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(sc.conv, "record_escalation", _boom)
    with pytest.raises(sc.SessionControlError) as exc:
        _escalate(state, member)
    assert exc.value.code == "escalation_index_unavailable"
    assert exc.value.status == 500
    assert member.messages == []
    assert _member_needs_you(member) is False


def test_one_sel_line_names_the_escalation(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    logged = []
    fake_sel = MagicMock()
    fake_sel.log_tool_invocation = lambda **kw: logged.append(kw)
    monkeypatch.setattr(sc, "sel", lambda: fake_sel)

    out = _escalate(state, member, deadline="30m", default_action="Push A", options=["A"])

    (entry,) = [kw for kw in logged if kw["tool_name"] == "session_escalate"]
    assert entry["outcome"] == "allowed"
    assert entry["resources"] == f"target={member.key}"
    assert entry["metadata"]["escalation_id"] == out["escalation_id"]
    assert entry["metadata"]["has_default"] is True
    assert entry["metadata"]["options"] == 1
    assert entry["metadata"]["member"] == SLUG


# ── Route + roster ───────────────────────────────────────────────────────────


def test_route_passes_escalation_fields_through(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    req = MagicMock()
    req.app = {"state": state}
    req.headers = {"X-Session-Key": _key(member)}
    req.get = lambda k, d=None: True if k == "internal_auth" else d

    async def _json():
        return {
            "target": "user",
            "message": "need a decision",
            "deadline": "2h",
            "default_action": "keep going",
            "options": ["yes", "no"],
            "goal": "g1",
        }

    req.json = _json
    resp = asyncio.run(handlers_sc.api_session_control_send(req))
    body = json.loads(resp.body)
    assert resp.status == 200
    assert body["escalation_id"]
    meta = member.messages[-1]["meta"]
    assert meta["options"] == ["yes", "no"]
    assert meta["goal"] == "g1"
    assert meta["default_action"] == "keep going"


def test_route_surfaces_deadline_refusal_as_400(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    req = MagicMock()
    req.app = {"state": state}
    req.headers = {"X-Session-Key": _key(member)}
    req.get = lambda k, d=None: True if k == "internal_auth" else d

    async def _json():
        return {"target": "user", "message": "x", "deadline": "5s"}

    req.json = _json
    resp = asyncio.run(handlers_sc.api_session_control_send(req))
    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "deadline_invalid"


def test_queued_reply_carries_the_escalation_id_onto_the_queue_entry(tmp_path):
    """A member that just escalated is usually mid-turn, so the chip reply goes
    through the queue; the id must ride the entry so the drained row answers
    the right record."""
    from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

    state = _make_state(tmp_path)
    member = _member_slot(state)
    state.broadcast_ws = MagicMock()
    queue_for_next_turn(state, member, "Keep it open", escalation_id="esc-0123456789abcdef")
    entry = member._queue[-1]
    assert entry["meta"]["escalation_id"] == "esc-0123456789abcdef"


@pytest.mark.asyncio
async def test_queue_merge_does_not_launder_a_non_owner_chip_id(tmp_path):
    """Two queued entries drain as ONE user row. An entry that is not itself the
    human's (no ``human_reply``) contributes no escalation id, and the merged
    row is stamped ``human_reply`` only when every merged entry was — one
    automated or non-owner entry in the batch and the row answers nothing."""
    from unittest.mock import patch

    from kiro_crew.dashboard import chat_runner

    state = _make_state(tmp_path)
    member = _member_slot(state)
    state.broadcast_ws = MagicMock()
    a = await sc.send_to_target(
        state, caller_session_key=member.key, target="user", message="Blocked A", goal="a"
    )
    b = await sc.send_to_target(
        state, caller_session_key=member.key, target="user", message="Blocked B", goal="b"
    )
    await asyncio.sleep(0)
    # Owner's chip for A, then a non-owner's chip for B (no provenance).
    member.queue_append("Ship it", meta={"escalation_id": a["escalation_id"], "human_reply": True})
    member.queue_append("Hold it", meta={"escalation_id": b["escalation_id"]})
    cfg = MagicMock()
    cfg.dashboard.merge_queued_messages = True
    with (
        patch.object(chat_runner.KiroCrewConfig, "load", return_value=cfg),
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
        patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
    ):
        assert await chat_runner._start_next_queued_turn(state, member) is True
    await asyncio.sleep(0)
    row = next(m for m in reversed(member.messages) if m["role"] == "user")
    meta = row.get("meta") or {}
    assert meta.get("escalation_id") == a["escalation_id"]  # owner's id survives
    assert "escalation_ids" not in meta and b["escalation_id"] not in str(meta)
    assert "human_reply" not in meta  # one non-owner entry -> not the human's row
    states = {e["id"]: e["state"] for e in conv.read_conversation(SLUG)["entries"]}
    assert states == {a["escalation_id"]: "pending", b["escalation_id"]: "pending"}

    # All-owner batch: both ids ride, the row is the human's, both answered.
    member.queue_append("Ship it", meta={"escalation_id": a["escalation_id"], "human_reply": True})
    member.queue_append("Hold it", meta={"escalation_id": b["escalation_id"], "human_reply": True})
    with (
        patch.object(chat_runner.KiroCrewConfig, "load", return_value=cfg),
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
        patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
    ):
        assert await chat_runner._start_next_queued_turn(state, member) is True
    for _ in range(3):
        await asyncio.sleep(0)
    row = next(m for m in reversed(member.messages) if m["role"] == "user")
    assert row["meta"].get("human_reply") is True
    assert sorted(row["meta"].get("escalation_ids", [])) == sorted(
        [a["escalation_id"], b["escalation_id"]]
    )


def test_live_reply_is_judged_against_the_deadline_at_the_reply_row(tmp_path):
    """The hook waits for a durable save before it marks anything; that save
    can cross the deadline. The reply is judged as of its own row timestamp,
    so a timely reply never turns into ``defaulted`` — and ``answered_ts`` is
    the row's, not the write's."""
    from datetime import datetime, timedelta, timezone

    state = _make_state(tmp_path)
    member = _member_slot(state)
    out = _escalate(state, member, deadline="2m", default_action="proceed")
    now = datetime.now(timezone.utc)
    # The deadline as recorded; the reply row is stamped 30s BEFORE it, while
    # the write happens "now" — after it.
    record = conv.read_conversation(SLUG)
    entry = next(e for e in record["entries"] if e["id"] == out["escalation_id"])
    entry["deadline"] = (now - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conv._write_conversation(SLUG, record)
    conv.prime(SLUG)
    reply_ts = (now - timedelta(seconds=35)).strftime("%Y-%m-%dT%H:%M:%SZ")
    member.append("user", "Go with A", "msg msg-u", ts=reply_ts, meta={"human_reply": True})
    entry = next(
        e for e in conv.read_conversation(SLUG)["entries"] if e["id"] == out["escalation_id"]
    )
    assert entry["state"] == "answered"
    assert entry["answered_ts"] == reply_ts


def test_requeued_steer_keeps_the_escalation_id(tmp_path):
    """A steer the turn's teardown requeues must not lose the record it names,
    and keeps exactly the provenance the handler validated: a human steer stays
    a human reply, an internal caller's steer is never promoted to one."""
    from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

    state = _make_state(tmp_path)
    member = _member_slot(state)
    state.broadcast_ws = MagicMock()
    member._pending_steers.append("Keep it open")
    member._steer_escalation_ids["Keep it open"] = "esc-0123456789abcdef"
    member._steer_human_reply.add("Keep it open")
    member._pending_steers.append("automation says hi")  # internal caller, no provenance
    _requeue_unconsumed_steers(state, member)
    assert len(member._queue) == 2, "steers were not requeued"
    human, automation = member._queue
    assert human["meta"]["escalation_id"] == "esc-0123456789abcdef"
    assert human["meta"]["human_reply"] is True
    assert "human_reply" not in automation["meta"]
    assert "Keep it open" not in member._steer_escalation_ids
    assert "Keep it open" not in member._steer_human_reply


def test_caller_that_becomes_ineligible_during_delivery_is_refused_and_retracted(
    tmp_path, monkeypatch
):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    worker = state.get_or_create_slot("chat-7")
    worker._created_by = member.key
    real_record = conv.record_escalation

    def _record_then_close_worker(*a, **kw):
        out = real_record(*a, **kw)
        state._slots.pop(worker.key, None)  # the worker closes mid-delivery
        return out

    monkeypatch.setattr(sc.conv, "record_escalation", _record_then_close_worker)
    with pytest.raises(sc.SessionControlError) as exc:
        _escalate(state, worker)
    assert exc.value.code == "target_gone"
    assert member.messages == []
    assert conv.read_conversation(SLUG)["entries"] == []


@pytest.mark.asyncio
async def test_roster_reports_needs_you_and_conversation_endpoint(tmp_path):
    from unittest.mock import patch

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.config.loader import KiroCrewAgentConfig
    from kiro_crew.dashboard.handlers.members import api_member_conversation, api_members
    from kiro_crew.members import write_dm_binding

    state = _make_state(tmp_path)
    member = _member_slot(state)
    write_dm_binding(SLUG, member=MEMBER, slot_key=member.key)
    conv.record_escalation(
        SLUG,
        member=MEMBER,
        session_key=member.key,
        mid="m-1",
        escalation_id="esc-1",
        from_session=member.key,
    )

    cfg = MagicMock()
    cfg.agents = {MEMBER: KiroCrewAgentConfig(kiro_agent="kirocrew", workspace="default")}
    cfg.agent.default_agent = MEMBER

    @web.middleware
    async def _auth(request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_get("/api/members", api_members)
    app.router.add_get("/api/members/{slug}/conversation", api_member_conversation)

    with (
        patch("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=cfg),
        patch(
            "kiro_crew.dashboard.handlers._shared.require_owner_dashboard_request",
            new=AsyncMock(return_value=None),
        ),
    ):
        async with TestClient(TestServer(app)) as client:
            roster = await (await client.get("/api/members")).json()
            row = {r["name"]: r for r in roster["members"]}[MEMBER]
            assert row["needs_you"] is True
            assert row["pending_escalations"] == 1

            detail = await client.get(f"/api/members/{SLUG}/conversation")
            assert detail.status == 200
            view = await detail.json()
            assert view["conversation_id"] == f"dm:{SLUG}"
            assert view["needs_you"] is True
            assert view["entries"][0]["id"] == "esc-1"

            bad = await client.get("/api/members/Not%20A%20Slug/conversation")
            assert bad.status == 400


def _row(role, mid=None, *, ts="", **meta):
    m = dict(meta)
    if mid:
        m["mid"] = mid
    return {"role": role, "content": "x", "ts": ts, "meta": m}


def test_reconcile_retracts_orphans_defers_young_and_applies_durable_answers(tmp_path):
    """Recovery: the transcript is the truth, both ways. A pending record whose
    card row is not in the transcript and is older than the in-flight grace is
    retracted; a younger one is deferred (not retracted, reported); a record
    whose card row IS present and that a later durable human reply answers
    under the live rule becomes answered."""
    from datetime import datetime, timedelta, timezone

    _make_state(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(seconds=conv.ORPHAN_GRACE_SECS + 5)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    for eid, mid in (("esc-orphan", "m-gone"), ("esc-kept", "m-here"), ("esc-chip", "m-chip")):
        conv.record_escalation(
            SLUG,
            member=MEMBER,
            session_key=f"member-{SLUG}",
            mid=mid,
            escalation_id=eid,
            from_session="chat-1",
            created_ts=old,
        )
    conv.record_escalation(
        SLUG,
        member=MEMBER,
        session_key=f"member-{SLUG}",
        mid="m-inflight",
        escalation_id="esc-young",
        from_session="chat-1",
    )
    transcript = [
        _row("escalation", "m-here"),
        _row("escalation", "m-chip"),
        # Two pending at this point: free text answers nothing.
        _row("user", "m-u1", ts="2026-09-05T05:00:00Z", human_reply=True),
        # A chip names its record.
        _row("user", "m-u2", ts="2026-09-05T05:01:00Z", human_reply=True, escalation_id="esc-chip"),
        # Now exactly one is pending: free text answers it. An automated row
        # (no human_reply) in between must not.
        _row("user", "m-auto"),
        _row("user", "m-u3", ts="2026-09-05T05:02:00Z", human_reply=True),
    ]
    out = conv.reconcile_with_transcript(SLUG, transcript)
    assert out == {"retracted": ["esc-orphan"], "answered": ["esc-chip", "esc-kept"], "deferred": 1}
    entries = {e["id"]: e for e in conv.read_conversation(SLUG)["entries"]}
    assert entries["esc-orphan"]["state"] == "retracted"
    assert entries["esc-orphan"]["retracted_reason"] == "orphan"
    assert entries["esc-chip"]["state"] == "answered"
    assert entries["esc-chip"]["answered_ts"] == "2026-09-05T05:01:00Z"
    assert entries["esc-kept"]["state"] == "answered"
    assert entries["esc-kept"]["answered_ts"] == "2026-09-05T05:02:00Z"
    assert entries["esc-young"]["state"] == "pending"
    assert conv.pending_ids(SLUG) == ["esc-young"]
    # Idempotent: nothing left to move, the young record is still deferred.
    assert conv.reconcile_with_transcript(SLUG, transcript) == {
        "retracted": [],
        "answered": [],
        "deferred": 1,
    }


def test_reconcile_replays_timely_replies_before_sweeping_deadlines(tmp_path):
    """A reply persisted before the deadline, a crash before the index write,
    recovery after the deadline: the timely answer wins — it is never recorded
    as defaulted. A reply that came AFTER the deadline answers nothing, as on
    the live path."""
    from datetime import datetime, timedelta, timezone

    _make_state(tmp_path)
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    old = (now - timedelta(seconds=conv.ORPHAN_GRACE_SECS + 5)).strftime(fmt)
    due = (now - timedelta(seconds=60)).strftime(fmt)  # deadline already passed at recovery
    for eid, mid in (("esc-timely", "m-t"), ("esc-late", "m-l")):
        conv.record_escalation(
            SLUG,
            member=MEMBER,
            session_key=f"member-{SLUG}",
            mid=mid,
            escalation_id=eid,
            from_session="chat-1",
            created_ts=old,
            deadline=due,
            default_action="proceed",
        )
    transcript = [
        _row("escalation", "m-t"),
        _row("escalation", "m-l"),
        # Before the deadline: answers esc-timely.
        _row(
            "user",
            "m-u1",
            ts=(now - timedelta(seconds=90)).strftime(fmt),
            human_reply=True,
            escalation_id="esc-timely",
        ),
        # After the deadline: answers nothing; esc-late defaults instead.
        _row(
            "user",
            "m-u2",
            ts=(now - timedelta(seconds=30)).strftime(fmt),
            human_reply=True,
            escalation_id="esc-late",
        ),
    ]
    out = conv.reconcile_with_transcript(SLUG, transcript, now=now)
    assert out["answered"] == ["esc-timely"]
    states = {e["id"]: e["state"] for e in conv.read_conversation(SLUG)["entries"]}
    assert states == {"esc-timely": "answered", "esc-late": "defaulted"}


def test_unprimed_cache_is_not_an_empty_cache(tmp_path):
    """A member slot restored before any roster read has no primed view; the
    reply hook must fall back to the file, not read "nothing cached" as
    "nothing pending" and drop the answer."""
    state = _make_state(tmp_path)
    member = _member_slot(state)
    _escalate(state, member)
    conv._PENDING_CACHE.clear()  # simulate a fresh process: index on disk, view unprimed
    assert conv.is_primed(SLUG) is False
    assert conv.pending_ids(SLUG) == []
    member.append("user", "Go with A", "msg msg-u", meta={"human_reply": True})
    assert conv.read_conversation(SLUG)["entries"][0]["state"] == "answered"
    assert conv.is_primed(SLUG) is True


@pytest.mark.asyncio
async def test_restore_sweeps_orphaned_pending_records(tmp_path, monkeypatch):
    """A gateway exit between the index write and the slot's flush leaves a
    pending record with no card; the first roster read after restart retracts
    it instead of lighting a badge nobody can act on. Records whose row IS in
    the transcript survive."""
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.config.loader import KiroCrewAgentConfig
    from kiro_crew.dashboard.handlers import members as members_mod
    from kiro_crew.members import write_dm_binding

    monkeypatch.setattr(members_mod, "_RECONCILED", {})
    state = _make_state(tmp_path)
    member = _member_slot(state)
    write_dm_binding(SLUG, member=MEMBER, slot_key=member.key)
    # The real card whose row made it into the transcript, aged past the grace.
    kept = await sc.send_to_target(
        state, caller_session_key=member.key, target="user", message="Blocked: need Z", goal="kept"
    )
    await asyncio.sleep(0)
    # Persist it, then drop it from the live window: after a restart the window
    # is only a TAIL of the transcript, and an older card lives on disk alone.
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

    assert await save_slot_off_loop(state, member, force=True, best_effort=False)
    kept_mid = member.messages[-1]["meta"]["mid"]
    member.messages[:] = []
    assert kept_mid in {
        (m.get("meta") or {}).get("mid")
        for m in state.conversation_log.read_messages_chained_full(f"dashboard:{member.key}")
    }
    old = (datetime.now(timezone.utc) - timedelta(seconds=conv.ORPHAN_GRACE_SECS + 5)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    # The orphan: index written, row never flushed (nothing in the window).
    conv.record_escalation(
        SLUG,
        member=MEMBER,
        session_key=member.key,
        mid="m-lost",
        escalation_id="esc-lost",
        from_session="chat-1",
        created_ts=old,
    )
    record = conv.read_conversation(SLUG)
    for e in record["entries"]:
        if e["id"] == kept["escalation_id"]:
            e["created_ts"] = old
    conv._write_conversation(SLUG, record)
    # A record still inside the in-flight grace: neither retracted nor trusted
    # as final — the member must be reconciled again on the next read.
    conv.record_escalation(
        SLUG,
        member=MEMBER,
        session_key=member.key,
        mid="m-young",
        escalation_id="esc-young",
        from_session="chat-1",
    )
    assert set(conv.pending_ids(SLUG)) == {kept["escalation_id"], "esc-lost", "esc-young"}

    cfg = MagicMock()
    cfg.agents = {MEMBER: KiroCrewAgentConfig(kiro_agent="kirocrew", workspace="default")}
    cfg.agent.default_agent = MEMBER

    @web.middleware
    async def _auth(request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_get("/api/members", members_mod.api_members)

    with (
        patch("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=cfg),
        patch(
            "kiro_crew.dashboard.handlers._shared.require_owner_dashboard_request",
            new=AsyncMock(return_value=None),
        ),
    ):
        async with TestClient(TestServer(app)) as client:
            roster = await (await client.get("/api/members")).json()
            row = {r["name"]: r for r in roster["members"]}[MEMBER]
            assert row["pending_escalations"] == 2  # kept + young; the orphan is gone
            assert row["needs_you"] is True
            states = {e["id"]: e["state"] for e in conv.read_conversation(SLUG)["entries"]}
            assert states == {
                kept["escalation_id"]: "pending",
                "esc-lost": "retracted",
                "esc-young": "pending",
            }
            # The young record was deferred, so the member is NOT marked done.
            assert SLUG not in members_mod._RECONCILED

            # A durable human reply the live hook never recorded (the gateway
            # exited between the reply's transcript save and the index write):
            # the chip row is on disk, the index still says pending.
            conv._PENDING_CACHE.clear()
            member.messages[:] = [
                {
                    "role": "user",
                    "content": "Go with A",
                    "cls": "msg msg-u",
                    "ts": "2026-09-05T06:00:00Z",
                    "meta": {
                        "mid": "m-reply",
                        "human_reply": True,
                        "escalation_id": kept["escalation_id"],
                    },
                }
            ]
            roster = await (await client.get("/api/members")).json()
            row = {r["name"]: r for r in roster["members"]}[MEMBER]
            assert row["pending_escalations"] == 1  # only the young one remains
            states = {e["id"]: e["state"] for e in conv.read_conversation(SLUG)["entries"]}
            assert states == {
                kept["escalation_id"]: "answered",
                "esc-lost": "retracted",
                "esc-young": "pending",
            }

            # Recovery is keyed to the transcript, not the process: a rewind that
            # removes a card row must be seen. Age the young record so it is no
            # longer deferred, let one read record the generation, then rewind.
            record = conv.read_conversation(SLUG)
            for e in record["entries"]:
                if e["id"] == "esc-young":
                    e["created_ts"] = old
            conv._write_conversation(SLUG, record)
            member.messages.append(
                {"role": "escalation", "content": "young", "cls": "", "meta": {"mid": "m-young"}}
            )
            assert await save_slot_off_loop(state, member, force=True, best_effort=False)
            roster = await (await client.get("/api/members")).json()
            assert {r["name"]: r for r in roster["members"]}[MEMBER]["pending_escalations"] == 1
            assert SLUG in members_mod._RECONCILED  # nothing deferred any more
            member.messages[:] = []  # the rewind drops the card row...
            state.conversation_log.rewrite_session(f"dashboard:{member.key}", [])
            roster = await (await client.get("/api/members")).json()
            row = {r["name"]: r for r in roster["members"]}[MEMBER]
            assert row["pending_escalations"] == 0  # ...and the record follows it
            assert row["needs_you"] is False
    states = {e["id"]: e["state"] for e in conv.read_conversation(SLUG)["entries"]}
    assert states == {
        kept["escalation_id"]: "answered",
        "esc-lost": "retracted",
        "esc-young": "retracted",
    }
    assert _member_needs_you(member) is False
