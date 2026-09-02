"""Tests for the slot's ``needs_input`` status — "the agent asked you something".

A question card is a websocket broadcast with no transcript row, so before this
status it was invisible anywhere outside the tab that received it — and a
BLOCKING card parks the turn, so the sidebar, the sessions board and the command
palette all showed a session that reported ``running`` with nothing able to
advance it.

That is the whole scope: the status corrects a status that would otherwise be
wrong. It is NOT raised for a turn that merely ended, including one ending in an
``[OPTIONS:]`` tag — every finished turn is waiting on the user, which is why
``waiting_for_input`` cannot carry a badge either. It is separate from
``pending_approval`` (a tool gate, answered allow/deny). These tests pin that
boundary in both directions, plus the record's whole lifecycle: who sets it, and
every path that must retire it.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.state import DashboardState, _ChatSlot


def _state(*slot_keys: str) -> DashboardState:
    """A partially-constructed DashboardState owning real slots.

    Only the attributes the question paths touch are wired, matching the
    fixture style of the other question suites; ``push_slots_update`` is a mock
    so a status change can be asserted to have been PUSHED, not merely stored.
    """
    st = DashboardState.__new__(DashboardState)
    st._pending_questions = {}
    st._question_futures = {}
    st._slots = {k: _ChatSlot(k) for k in slot_keys}
    st.broadcast_ws_owners = MagicMock()  # type: ignore[method-assign]
    st.deliver_ws_owners = _AsyncReturn(1)  # type: ignore[method-assign]
    st.push_slots_update = MagicMock()  # type: ignore[method-assign]
    st._log = MagicMock()
    return st


class _AsyncReturn:
    """Awaitable stub standing in for ``deliver_ws_owners``'s client count."""

    def __init__(self, value: int) -> None:
        self.value = value
        self.calls: list[tuple] = []

    async def __call__(self, *args, **kwargs) -> int:
        self.calls.append((args, kwargs))
        return self.value


def _questions() -> list[dict]:
    return [
        {
            "question": "Which approach?",
            "options": [{"label": "Option A", "description": ""}],
            "multiSelect": False,
        }
    ]


def _turn(slot: _ChatSlot, *rows: tuple[str, str]) -> _ChatSlot:
    """Append LIVE rows (broadcast=True), the shape a real turn produces.

    The flag is load-bearing: a `broadcast=False` append is a transcript replay,
    which deliberately does NOT retire a question. `_on_message` is unset on a
    bare slot, so nothing is actually sent.
    """
    for role, content in rows:
        slot.append(role, content, broadcast=True)
    return slot


# ── NOT derived from message state: an ended turn is not an ask ──


def test_options_tag_does_not_report_needs_input() -> None:
    """A turn that ended offering choices is an ordinary finished turn.

    Every finished turn is waiting on the user, so a status raised here lights on
    most of the sidebar and says nothing — and it would outrank the row's live
    turn status while the next turn runs. The row's last message and unread dot
    already carry this state.
    """
    slot = _turn(
        _ChatSlot("chat-1"),
        ("user", "which one?"),
        ("assistant", "Both work.\n\n[OPTIONS: Merge it now | Show me the diff]"),
    )
    payload = slot.to_dict()
    assert payload["needs_input"] is False
    # The options themselves are still published — the Board's pills read them.
    assert payload["has_options"] is True
    # `waiting_for_input` excludes an options turn by its own definition, so no
    # field claims this row: that is the point. The sidebar shows the message.
    assert payload["waiting_for_input"] is False


def test_plain_finished_turn_does_not_report_needs_input() -> None:
    """An ordinary reply is not an ask — otherwise the status lights always."""
    slot = _turn(
        _ChatSlot("chat-1"), ("user", "do the thing"), ("assistant", "done, 3 files changed")
    )
    payload = slot.to_dict()
    assert payload["needs_input"] is False
    assert payload["waiting_for_input"] is True


def test_empty_slot_reports_nothing() -> None:
    payload = _ChatSlot("chat-1").to_dict()
    assert payload["needs_input"] is False


# ── The recorded question card ──


def test_question_record_reports_needs_input_and_outranks_options() -> None:
    slot = _turn(
        _ChatSlot("chat-1"), ("assistant", "Pick one.\n\n[OPTIONS: Yes | No]")
    )
    slot._question_pending = {"card-a": {"ts": 0.0, "blocking": False}}
    payload = slot.to_dict()
    assert payload["needs_input"] is True


def test_question_record_survives_further_assistant_output() -> None:
    """A card posted mid-turn must not be retired by the agent's own next line."""
    slot = _ChatSlot("chat-1")
    slot._question_pending = {"ask-1": {"ts": 0.0, "blocking": True}}
    _turn(slot, ("assistant", "meanwhile, here is what I found"))
    assert slot.to_dict()["needs_input"] is True


def test_user_message_retires_a_stateless_question_record() -> None:
    """Any turn-consuming input answers the ask, whatever entrance it came from."""
    slot = _ChatSlot("chat-1")
    slot._question_pending = {"card-a": {"ts": 0.0, "blocking": False}}
    _turn(slot, ("user", "option A"))
    assert slot._question_pending == {}
    assert slot.to_dict()["needs_input"] is False


def test_user_message_leaves_a_BLOCKING_record_standing() -> None:
    """Nothing a user row does resolves a parked wait.

    A channel-replayed reply, a queued message or a nudge all land as `user`
    rows while `request_question` is still blocked on its future. Clearing the
    record there would report the agent as working while its tool call cannot
    move — the record is the round-trip's to retire.
    """
    slot = _ChatSlot("chat-1")
    slot._question_pending = {"ask-1": {"ts": 0.0, "blocking": True}}
    _turn(slot, ("user", "unrelated reply from Slack"))
    assert slot.to_dict()["needs_input"] is True


def test_needs_input_is_not_gated_on_running() -> None:
    """A blocking ask parks the turn: the slot is running AND waiting on the user."""
    slot = _ChatSlot("chat-1")
    # `running` is derived from the slot's task, so a live turn is simulated with
    # an unfinished one rather than by assigning the property.
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    assert slot.running is True
    slot._question_pending = {"ask-1": {"ts": 0.0, "blocking": True}}
    assert slot.to_dict()["needs_input"] is True


# ── mark / clear ──


@pytest.mark.asyncio
async def test_post_question_card_records_the_status_and_broadcasts_its_id() -> None:
    st = _state("chat-1")
    delivered = await st.post_question_card("chat-1", _questions())
    assert delivered == 1
    assert st._slots["chat-1"].to_dict()["needs_input"] is True
    st.push_slots_update.assert_called()  # type: ignore[attr-defined]
    # The client needs the record's identity to dismiss it, so it rides the card.
    (args, _kwargs) = st.deliver_ws_owners.calls[0]  # type: ignore[attr-defined]
    payload = args[1]
    assert payload["card_id"] in st._slots["chat-1"]._question_pending
    assert payload["card_id"]


@pytest.mark.asyncio
async def test_card_with_no_client_attached_still_records_the_status() -> None:
    """Zero clients means no tab is open, not that the ask went away."""
    st = _state("chat-1")
    st.deliver_ws_owners = _AsyncReturn(0)  # type: ignore[method-assign]
    assert await st.post_question_card("chat-1", _questions()) == 0
    assert st._slots["chat-1"].to_dict()["needs_input"] is True


@pytest.mark.asyncio
async def test_status_is_recorded_before_delivery_is_awaited() -> None:
    """Ordering, not just the end state: the mark must precede the await.

    Websocket delivery can park on a backpressured socket. A user row landing in
    that window would find no record to retire, and a mark applied afterwards
    would then strand an already-answered session in needs_input. Asserted by
    observing the slot from INSIDE the delivery await.
    """
    st = _state("chat-1")
    seen: dict[str, object] = {}

    async def slow_delivery(*_args, **_kwargs) -> int:
        seen["asking"] = st._slots["chat-1"].to_dict()["needs_input"]
        return 1

    st.deliver_ws_owners = slow_delivery  # type: ignore[method-assign]
    await st.post_question_card("chat-1", _questions())
    assert seen["asking"] is True


@pytest.mark.asyncio
async def test_mark_ignores_an_unknown_slot() -> None:
    st = _state()
    st.mark_question_pending("chat-404", blocking=False, card_id="card-a")  # must not raise
    assert st.clear_question_pending("chat-404") is False


def test_clear_reports_whether_anything_was_pending() -> None:
    st = _state("chat-1")
    assert st.clear_question_pending("chat-1") is False
    st.mark_question_pending("chat-1", blocking=False, card_id="card-a")
    assert st.clear_question_pending("chat-1") is True
    assert st.clear_question_pending("chat-1") is False


def test_a_second_stateless_card_supersedes_the_first() -> None:
    """The frontend shows ONE card per slot, so the record map must agree.

    Keeping both would leave the replaced card unreachable — nothing to answer or
    dismiss — while its entry held the status up indefinitely.
    """
    st = _state("chat-1")
    st.mark_question_pending("chat-1", blocking=False, card_id="card-A")
    st.mark_question_pending("chat-1", blocking=False, card_id="card-B")

    assert list(st._slots["chat-1"]._question_pending) == ["card-B"]
    # Dismissing the card actually on screen therefore clears the status.
    assert st.clear_question_pending("chat-1", blocking=False, card_id="card-B") is True
    assert st._slots["chat-1"].to_dict()["needs_input"] is False


def test_a_stateless_card_does_not_disturb_parked_asks() -> None:
    """Superseding is scoped to stateless entries; each parked ask keeps its own."""
    st = _state("chat-1")
    st.mark_question_pending("chat-1", blocking=True, card_id="ask-A")
    st.mark_question_pending("chat-1", blocking=True, card_id="ask-B")
    st.mark_question_pending("chat-1", blocking=False, card_id="card-C")

    assert sorted(st._slots["chat-1"]._question_pending) == ["ask-A", "ask-B", "card-C"]


def test_replayed_user_rows_do_not_retire_the_status() -> None:
    """A transcript replay is not an answer.

    Rotation recovery (`channel_slots._rebuild_window`), forks and session
    transfers re-append historical rows with `broadcast=False`. Retiring on those
    would clear — and broadcast the clearing of — a card asked moments ago because
    of a message the user sent hours earlier.
    """
    slot = _ChatSlot("chat-1")
    slot._question_pending = {"card-a": {"ts": 0.0, "blocking": False}}
    slot.append("user", "a line from the transcript being replayed", broadcast=False)
    assert slot.to_dict()["needs_input"] is True
    # A live row still retires it.
    slot.append("user", "answering now", broadcast=True)
    assert slot.to_dict()["needs_input"] is False


def test_a_user_message_announces_the_retirement() -> None:
    """A retirement that only mutates state is invisible to other clients.

    A second window — or this window's own /pending response, already in flight —
    would re-render a card whose answer has just been sent, and submitting that
    card appends a duplicate turn. The slot therefore announces which card_ids it
    retired, the same way it announces messages.
    """
    slot = _ChatSlot("chat-1")
    announced: list[tuple[str, list[str]]] = []
    slot._on_question_retired = lambda key, ids: announced.append((key, list(ids)))
    slot._question_pending = {
        "card-a": {"ts": 0.0, "blocking": False},
        "ask-b": {"ts": 0.0, "blocking": True},
    }
    _turn(slot, ("user", "answering the card"))
    # Only the stateless id is announced — the parked ask is not retired at all.
    assert announced == [("chat-1", ["card-a"])]


def test_a_replayed_user_row_announces_nothing() -> None:
    slot = _ChatSlot("chat-1")
    announced: list[tuple[str, list[str]]] = []
    slot._on_question_retired = lambda key, ids: announced.append((key, list(ids)))
    slot._question_pending = {"card-a": {"ts": 0.0, "blocking": False}}
    slot.append("user", "a replayed transcript line", broadcast=False)
    assert announced == []
    assert slot.to_dict()["needs_input"] is True


def test_a_failing_announcer_cannot_break_the_append() -> None:
    """The message must still land: the announcement is decoration on the state."""
    slot = _ChatSlot("chat-1")

    def boom(_key: str, _ids: list[str]) -> None:
        raise RuntimeError("socket gone")

    slot._on_question_retired = boom
    slot._question_pending = {"card-a": {"ts": 0.0, "blocking": False}}
    _turn(slot, ("user", "answering the card"))
    assert slot._question_pending == {}
    assert slot.messages[-1]["content"] == "answering the card"


def test_clear_announces_the_retirement() -> None:
    st = _state("chat-1")
    st.broadcast_ws_owners = MagicMock()  # type: ignore[method-assign]
    st.mark_question_pending("chat-1", blocking=False, card_id="card-a")
    assert st.clear_question_pending("chat-1", blocking=False, card_id="card-a") is True
    st.broadcast_ws_owners.assert_any_call(  # type: ignore[attr-defined]
        "question_card_resolved", {"card_id": "card-a", "slot": "chat-1"}
    )


def test_a_live_nudge_row_retires_a_stateless_card() -> None:
    """An auto-nudge cycle starts the next turn, so it consumes the answer channel.

    The frontend drops the card on a `nudge` frame; a server that kept the record
    would leave the session reporting needs_input with nothing on screen, and a
    later rehydration would re-render a card whose answer channel is gone.
    """
    slot = _ChatSlot("chat-1")
    announced: list[list[str]] = []
    slot._on_question_retired = lambda _key, ids: announced.append(list(ids))
    slot._question_pending = {"card-a": {"ts": 0.0, "blocking": False}}
    slot.append("nudge", "[auto-nudge] keep going", broadcast=True)
    assert slot._question_pending == {}
    assert announced == [["card-a"]]


def test_the_retiring_role_sets_agree_across_the_stack() -> None:
    """The backend's set and the frontend's must name the same roles.

    Either half alone produces a visible defect: a role the client retires but
    the server keeps strands the status; the reverse re-renders a dead card.
    """
    from kiro_crew.dashboard import state as state_mod

    slice_src = (
        Path(state_mod.__file__).resolve().parents[3]
        / "website/src/store/chatSlice.ts"
    ).read_text(encoding="utf-8")
    match = re.search(r"QUESTION_RETIRING_ROLES = new Set\(\[([^\]]*)\]\)", slice_src)
    assert match, "frontend QUESTION_RETIRING_ROLES not found — did it move?"
    frontend = {tok.strip().strip("'\"") for tok in match.group(1).split(",") if tok.strip()}
    assert frontend == set(state_mod._QUESTION_RETIRING_ROLES)


def test_overlapping_asks_each_keep_their_own_record() -> None:
    """One ask resolving must not take the other's status with it.

    A single slot-wide record let the second ask overwrite the first, so whichever
    resolved first cleared the only entry — and the still-parked ask went dark.
    """
    st = _state("chat-1")
    st.mark_question_pending("chat-1", blocking=True, card_id="ask-A")
    st.mark_question_pending("chat-1", blocking=True, card_id="ask-B")

    assert st.clear_question_pending("chat-1", blocking=True, card_id="ask-B") is True
    assert st._slots["chat-1"].to_dict()["needs_input"] is True  # A is still parked

    assert st.clear_question_pending("chat-1", blocking=True, card_id="ask-A") is True
    assert st._slots["chat-1"].to_dict()["needs_input"] is False


def test_a_user_message_retires_only_the_stateless_entries() -> None:
    """A mixed slot: the card goes, the parked round-trip stays."""
    st = _state("chat-1")
    st.mark_question_pending("chat-1", blocking=True, card_id="ask-A")
    st.mark_question_pending("chat-1", blocking=False, card_id="card-B")

    _turn(st._slots["chat-1"], ("user", "answering the card"))

    assert list(st._slots["chat-1"]._question_pending) == ["ask-A"]
    assert st._slots["chat-1"].to_dict()["needs_input"] is True


def test_records_are_only_ever_dropped_by_a_retirement() -> None:
    """No capacity eviction: dropping a parked ask's record reports it as idle.

    Size is bounded by construction instead — one stateless entry per slot
    (superseding), and one blocking entry per in-flight ask_question request.
    """
    st = _state("chat-1")
    for i in range(20):
        st.mark_question_pending("chat-1", blocking=True, card_id=f"ask-{i}")
    pending = st._slots["chat-1"]._question_pending
    assert len(pending) == 20
    assert "ask-0" in pending  # the oldest parked ask still has its status

    # Stateless cards cannot accumulate: each supersedes its predecessor.
    for i in range(5):
        st.mark_question_pending("chat-1", blocking=False, card_id=f"card-{i}")
    stateless = [cid for cid, rec in pending.items() if not rec.get("blocking")]
    assert stateless == ["card-4"]
    assert len(pending) == 21


def test_clear_filters_on_the_blocking_flag() -> None:
    """The dismiss route may not retire a status whose tool call is still parked."""
    st = _state("chat-1")
    st.mark_question_pending("chat-1", blocking=True, card_id="ask-1")
    assert st.clear_question_pending("chat-1", blocking=False) is False
    assert st._slots["chat-1"].to_dict()["needs_input"] is True
    assert st.clear_question_pending("chat-1", blocking=True) is True


def test_clear_filters_on_the_card_identity() -> None:
    """A dismissal names the card it was clicked on, never just the slot.

    The request is a round-trip, so a newer ask can replace the card in between.
    A slot-only clear would retire the NEW card's status and leave it unanswered
    with nothing on any surface to say so.
    """
    st = _state("chat-1")
    st.mark_question_pending("chat-1", blocking=False, card_id="card-new")
    assert st.clear_question_pending("chat-1", blocking=False, card_id="card-old") is False
    assert st._slots["chat-1"].to_dict()["needs_input"] is True
    assert st.clear_question_pending("chat-1", blocking=False, card_id="card-new") is True


# ── The blocking round-trip's lifecycle ──


async def _await_registered(st: DashboardState, count: int) -> None:
    for _ in range(50):
        if len(st._question_futures) == count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"only {len(st._question_futures)} question(s) registered")


@pytest.mark.asyncio
async def test_blocking_question_marks_then_retires_on_answer() -> None:
    st = _state("chat-1")
    task = asyncio.ensure_future(
        st.request_question("a1", "chat-1", _questions(), timeout=30)
    )
    await _await_registered(st, 1)
    assert st._slots["chat-1"].to_dict()["needs_input"] is True

    st.resolve_question("a1", {"Which approach?": "Option A"})
    assert await task == {"Which approach?": "Option A"}
    assert st._slots["chat-1"].to_dict()["needs_input"] is False


@pytest.mark.asyncio
async def test_blocking_question_retires_on_timeout() -> None:
    st = _state("chat-1")
    assert await st.request_question("a2", "chat-1", _questions(), timeout=1) is None
    assert st._slots["chat-1"].to_dict()["needs_input"] is False


@pytest.mark.asyncio
async def test_one_question_exiting_leaves_another_ask_status_standing() -> None:
    """Two asks on one slot: retiring the first must not report the second as answered."""
    st = _state("chat-1")
    first = asyncio.ensure_future(
        st.request_question("a1", "chat-1", _questions(), timeout=30)
    )
    second = asyncio.ensure_future(
        st.request_question("a2", "chat-1", _questions(), timeout=30)
    )
    await _await_registered(st, 2)

    st.resolve_question("a1", {"Which approach?": "Option A"})
    await first
    assert st._slots["chat-1"].to_dict()["needs_input"] is True

    st.resolve_question("a2", {"Which approach?": "Option A"})
    await second
    assert st._slots["chat-1"].to_dict()["needs_input"] is False
