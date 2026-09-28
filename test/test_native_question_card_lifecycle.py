"""The native ``AskUserQuestion`` card has a server-side lifecycle owner.

kiro-cli raises this card mid-turn. Without a server record it would be a bare
``question_card`` frame with neither ``ask_id`` nor ``card_id``, and the client
would have to mint its own identity and retire the card itself. These tests pin
that the native card rides the same owner the MCP ``ask_question`` card uses --
a minted ``card_id`` on the frame, a ``_question_pending`` record on the slot,
and retirement announced by ``question_card_resolved`` on the next user row or
consumed steer -- so the client keeps no lifecycle of its own for this kind.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.chat_runner import _post_native_question_card
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


class _Delivered:
    """Awaitable stand-in for ``deliver_ws_owners`` that records each frame."""

    def __init__(self) -> None:
        self.frames: list[tuple[str, dict]] = []

    async def __call__(self, kind: str, payload: dict) -> int:
        self.frames.append((kind, payload))
        return 1


def _state(*slot_keys: str) -> DashboardState:
    st = DashboardState.__new__(DashboardState)
    st._pending_questions = {}
    st._question_futures = {}
    st._slots = {k: _ChatSlot(k) for k in slot_keys}
    for slot in st._slots.values():
        slot._on_question_retired = st._broadcast_question_retired
    st.deliver_ws_owners = _Delivered()  # type: ignore[method-assign]
    st.broadcasts: list[tuple[str, dict]] = []  # type: ignore[attr-defined]
    st.broadcast_ws_owners = lambda kind, payload: st.broadcasts.append(  # type: ignore[assignment,attr-defined]
        (kind, payload)
    )
    st.broadcast_ws = MagicMock()  # type: ignore[method-assign]
    st.push_slots_update = MagicMock()  # type: ignore[method-assign]
    st._log = MagicMock()
    return st


def _owner_request(st: DashboardState) -> MagicMock:
    """A fake owner request, wired the way ``is_owner_dashboard_request`` reads it
    (an explicit empty app claim plus the local bootstrap subject)."""
    request = MagicMock()
    request.app = {"state": st}
    claims = {"app": "", "user": "local-app"}
    request.__contains__.side_effect = lambda k: k in claims
    request.__getitem__.side_effect = lambda k: claims[k]
    request.get = lambda k, d="": claims.get(k, d)
    return request


def _tool_input_questions(question: str = "Which approach?") -> list[dict]:
    return [
        {
            "header": "SCOPE",
            "question": question,
            "options": [
                {"label": "Option A", "description": "the safe one"},
                {"label": "Option B", "description": ""},
            ],
            "multiSelect": False,
        }
    ]


def _tool_input(question: str = "Which approach?") -> str:
    return json.dumps({"questions": _tool_input_questions(question)})


@pytest.mark.asyncio
async def test_native_card_frame_carries_a_server_card_id() -> None:
    st = _state("chat-1")
    assert await _post_native_question_card(st, "chat-1", _tool_input()) is True
    frames = st.deliver_ws_owners.frames  # type: ignore[attr-defined]
    assert [kind for kind, _ in frames] == ["question_card"]
    payload = frames[0][1]
    assert payload["slot"] == "chat-1"
    assert payload["card_id"].startswith("card-")
    assert "ask_id" not in payload
    assert payload["questions"][0]["question"] == "Which approach?"
    # The one thing that tells this card apart from the MCP ``ask_question``
    # card: the client steers a native card's answer into the live turn.
    assert payload["native"] is True
    # The card is owner-addressed like every other question card, so the
    # all-clients channel the old bare broadcast used stays silent.
    st.broadcast_ws.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_native_card_records_needs_input_on_the_slot() -> None:
    st = _state("chat-1")
    await _post_native_question_card(st, "chat-1", _tool_input())
    payload = st.deliver_ws_owners.frames[0][1]  # type: ignore[attr-defined]
    record = st._slots["chat-1"]._question_pending[payload["card_id"]]
    assert record["blocking"] is False
    assert record["native"] is True
    # The stored questions are what a reload rehydrates from /pending.
    assert record["questions"] == payload["questions"]
    assert st._slots["chat-1"].to_dict()["needs_input"] is True
    st.push_slots_update.assert_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_pending_endpoint_rehydrates_the_native_marker() -> None:
    """A reload must route the answer the way the live frame did, so the
    marker rides ``GET /api/ask-question/pending`` with the card."""
    from kiro_crew.dashboard.handlers.ask_question import api_ask_question_pending

    st = _state("chat-1")
    await _post_native_question_card(st, "chat-1", _tool_input())
    await st.post_question_card("chat-1", _tool_input_questions("MCP card?"))
    # The MCP card superseded the native one (one stateless card per slot).
    request = _owner_request(st)
    rows = json.loads((await api_ask_question_pending(request)).text)
    assert [(r["questions"][0]["question"], r["native"]) for r in rows] == [("MCP card?", False)]

    await _post_native_question_card(st, "chat-1", _tool_input("Native again?"))
    rows = json.loads((await api_ask_question_pending(request)).text)
    assert [(r["questions"][0]["question"], r["native"]) for r in rows] == [("Native again?", True)]


@pytest.mark.asyncio
async def test_the_mcp_card_is_not_marked_native() -> None:
    st = _state("chat-1")
    await st.post_question_card("chat-1", _tool_input_questions())
    payload = st.deliver_ws_owners.frames[0][1]  # type: ignore[attr-defined]
    assert payload["native"] is False
    assert "native" not in st._slots["chat-1"]._question_pending[payload["card_id"]]


@pytest.mark.asyncio
async def test_next_user_row_retires_the_native_card_and_announces_it() -> None:
    st = _state("chat-1")
    await _post_native_question_card(st, "chat-1", _tool_input())
    card_id = st.deliver_ws_owners.frames[0][1]["card_id"]  # type: ignore[attr-defined]
    st._slots["chat-1"].append("user", "Option A", broadcast=True)
    assert card_id not in st._slots["chat-1"]._question_pending
    assert st._slots["chat-1"].to_dict()["needs_input"] is False
    assert ("question_card_resolved", {"card_id": card_id, "slot": "chat-1"}) in st.broadcasts  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_consumed_steer_retires_the_native_card_and_announces_it() -> None:
    """The mid-turn answer path: the runner clears non-blocking records on a
    consumed steer (``clear_question_pending(slot, blocking=False)``)."""
    st = _state("chat-1")
    await _post_native_question_card(st, "chat-1", _tool_input())
    card_id = st.deliver_ws_owners.frames[0][1]["card_id"]  # type: ignore[attr-defined]
    assert st.clear_question_pending("chat-1", blocking=False) is True
    assert st._slots["chat-1"]._question_pending == {}
    assert ("question_card_resolved", {"card_id": card_id, "slot": "chat-1"}) in st.broadcasts  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_second_native_card_supersedes_the_first() -> None:
    st = _state("chat-1")
    await _post_native_question_card(st, "chat-1", _tool_input("First?"))
    await _post_native_question_card(st, "chat-1", _tool_input("Second?"))
    frames = st.deliver_ws_owners.frames  # type: ignore[attr-defined]
    first, second = frames[0][1]["card_id"], frames[1][1]["card_id"]
    assert first != second
    assert list(st._slots["chat-1"]._question_pending) == [second]


@pytest.mark.asyncio
async def test_native_card_text_is_redacted_by_the_shared_pass() -> None:
    """Redaction lives in the coordinator now, not in the runner's tool branch."""
    st = _state("chat-1")
    secret = "AKIAIOSFODNN7EXAMPLE"
    raw = _tool_input(f"Use key {secret}?")
    await _post_native_question_card(st, "chat-1", raw)
    payload = st.deliver_ws_owners.frames[0][1]  # type: ignore[attr-defined]
    assert secret not in json.dumps(payload)
    assert secret not in json.dumps(st._slots["chat-1"]._question_pending)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_input",
    [
        "not json",
        json.dumps({"questions": []}),
        json.dumps(["not", "an", "object"]),
        json.dumps({"questions": [{"question": "x", "options": []}]}),
    ],
)
async def test_unusable_input_posts_nothing_and_does_not_raise(tool_input: str) -> None:
    st = _state("chat-1")
    assert await _post_native_question_card(st, "chat-1", tool_input) is False
    assert st.deliver_ws_owners.frames == []  # type: ignore[attr-defined]
    assert st._slots["chat-1"]._question_pending == {}


@pytest.mark.asyncio
async def test_unknown_slot_posts_nothing_and_does_not_raise() -> None:
    st = _state()
    # Delivery still happens (the coordinator addresses owners, not a slot), but
    # no record can be kept and nothing may propagate to the turn.
    assert await _post_native_question_card(st, "chat-404", _tool_input()) is True
    assert st._slots == {}


def test_the_runner_has_no_bare_question_card_broadcast_left() -> None:
    """The native tool branch was the last bare ``question_card`` emitter in the
    runner. With the helper in place there must be no literal left, otherwise a
    second identity-less card path has quietly returned."""
    import inspect

    from kiro_crew.dashboard import chat_runner

    src = inspect.getsource(chat_runner._run_chat)
    branch = src.index('event.title == "AskUserQuestion"')
    assert (
        "await _post_native_question_card(state, slot.key, event.tool_input)"
        in src[branch : branch + 400]
    )
    assert '"question_card"' not in inspect.getsource(chat_runner)
