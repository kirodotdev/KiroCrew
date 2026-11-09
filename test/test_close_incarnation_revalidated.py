"""A DELETE that names an incarnation must not archive a different session under the key.

Keys are reusable, so a same-key resume before the request arrives leaves the lookup
resolving to a replacement the caller never asked to archive. The expected incarnation
travels with the DELETE and is checked at entry, before any teardown step.
"""

from __future__ import annotations

import asyncio
import json

from kiro_crew.dashboard import chat_handlers


class _Slot:
    def __init__(self, incarnation: str) -> None:
        self._dashboard_card_identity = incarnation
        self._app = ""


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


def test_omitting_the_incarnation_leaves_a_pre_arrival_replacement_unprotected(
    monkeypatch,
) -> None:
    """The accepted residual, pinned so weakening it further is deliberate. The handler's
    own lookup resolves to whatever holds the key, so identity cannot tell a replacement
    that arrived FIRST from the row the caller meant; only the caller can. Compare the
    same setup in the first test, where naming an instance refuses this exact close."""
    replacement = _Slot("incarnation-B")
    state = _State({"chat-1": replacement})
    monkeypatch.setattr(
        chat_handlers, "close_slot", _pop_at_the_point_of_no_return(state, "chat-1")
    )

    response = asyncio.run(chat_handlers.api_chat_slot_delete(_Request(state, "chat-1")))

    assert response.status == 200
    assert "chat-1" not in state._slots
