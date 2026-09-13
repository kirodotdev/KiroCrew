"""A key-only DELETE must not archive whatever holds the key when the pop arrives.

Keys are reusable, so a same-key resume during the close's awaits leaves the pop landing on
a replacement session the caller never asked to archive. The expected incarnation travels
with the DELETE and is revalidated SYNCHRONOUSLY at the point of no return, which is the only
place where nothing can change between the check and the pop.
"""

from __future__ import annotations

import asyncio
import json

from kiro_crew.dashboard import chat_handlers


class _Slot:
    def __init__(self, incarnation: str) -> None:
        self.incarnation = incarnation
        self._app = ""
        self._closes_in_flight = 0


class _State:
    def __init__(self, slots: dict[str, object]) -> None:
        self._slots = slots


class _Request:
    def __init__(self, state: _State, name: str, incarnation: str = "") -> None:
        self.app = {"state": state}
        self.match_info = {"slot": name}
        self.query = {"incarnation": incarnation} if incarnation else {}

    def get(self, key: str, default: str = "") -> str:
        return default


def _pop_at_the_point_of_no_return(state: _State, name: str):
    async def _close(_state, _slot, _name, *, pre_pop_check=None) -> None:
        if pre_pop_check is not None:
            pre_pop_check()
        state._slots.pop(name, None)

    return _close


def _body(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def test_a_replacement_resumed_under_the_reused_key_survives_the_close(monkeypatch) -> None:
    replacement = _Slot("incarnation-B")
    state = _State({"chat-1": replacement})
    monkeypatch.setattr(
        chat_handlers, "close_slot", _pop_at_the_point_of_no_return(state, "chat-1")
    )

    response = asyncio.run(
        chat_handlers.api_chat_slot_delete(_Request(state, "chat-1", "incarnation-A"))
    )

    assert response.status == 409
    assert _body(response)["code"] == "target_replaced"
    assert state._slots["chat-1"] is replacement


def test_the_refusal_states_that_aiming_it_again_is_not_safe(monkeypatch) -> None:
    state = _State({"chat-1": _Slot("incarnation-B")})
    monkeypatch.setattr(
        chat_handlers, "close_slot", _pop_at_the_point_of_no_return(state, "chat-1")
    )

    body = _body(
        asyncio.run(chat_handlers.api_chat_slot_delete(_Request(state, "chat-1", "incarnation-A")))
    )

    assert body["definitive"] is True


def test_the_close_lands_when_the_incarnation_still_matches(monkeypatch) -> None:
    state = _State({"chat-1": _Slot("incarnation-A")})
    monkeypatch.setattr(
        chat_handlers, "close_slot", _pop_at_the_point_of_no_return(state, "chat-1")
    )

    response = asyncio.run(
        chat_handlers.api_chat_slot_delete(_Request(state, "chat-1", "incarnation-A"))
    )

    assert response.status == 200
    assert "chat-1" not in state._slots


def test_a_swapped_object_is_refused_even_with_no_incarnation_sent(monkeypatch) -> None:
    state = _State({"chat-1": _Slot("incarnation-A")})
    original = state._slots["chat-1"]
    replacement = _Slot("incarnation-A")

    async def _close(_state, _slot, _name, *, pre_pop_check=None) -> None:
        state._slots["chat-1"] = replacement
        if pre_pop_check is not None:
            pre_pop_check()
        state._slots.pop("chat-1", None)

    monkeypatch.setattr(chat_handlers, "close_slot", _close)

    response = asyncio.run(chat_handlers.api_chat_slot_delete(_Request(state, "chat-1")))

    assert response.status == 409
    assert state._slots["chat-1"] is replacement
    assert original is not replacement
