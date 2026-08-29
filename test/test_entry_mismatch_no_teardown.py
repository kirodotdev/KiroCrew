"""An entry-time incarnation mismatch must refuse before any teardown seam runs."""

from __future__ import annotations

import asyncio
import json

import pytest

from kiro_crew.dashboard import chat_handlers


class _Slot:
    def __init__(self, incarnation: str, app: str = "", key: str = "chat-1") -> None:
        self.incarnation = incarnation
        self._app = app
        self._closes_in_flight = 0
        self.key = key
        self._approval_futures: dict[str, object] = {}
        self.messages: list[object] = []
        self.agent = ""
        self.title = ""


class _State:
    def __init__(self, slots: dict[str, object]) -> None:
        self._slots = slots

    def get_slot(self, name: str):
        return self._slots.get(name)

    def push_slots_update(self) -> None:
        pass


class _Request:
    def __init__(self, state: _State, name: str, incarnation: str) -> None:
        self.app = {"state": state}
        self.match_info = {"slot": name}
        self.query = {"incarnation": incarnation}

    def get(self, key: str, default: str = "") -> str:
        return default


@pytest.fixture()
def teardown_spies(monkeypatch):
    calls = {"retire": 0, "hook": 0}

    async def _retire(name: str):
        calls["retire"] += 1
        return None

    async def _notify(app_id: str, name: str) -> bool:
        calls["hook"] += 1
        return True

    monkeypatch.setattr(chat_handlers, "_retire_slot_nudge_loop", _retire)
    teardown = pytest.importorskip("kiro_crew.apps.teardown")
    monkeypatch.setattr(teardown, "notify_slot_closed", _notify)
    return calls


def test_entry_time_mismatch_tears_nothing_down(teardown_spies):
    held = _Slot("B-incarnation", app="some-app")
    state = _State({"chat-1": held})
    request = _Request(state, "chat-1", "A-incarnation")

    response = asyncio.run(chat_handlers.api_chat_slot_delete(request))
    body = json.loads(response.body.decode())

    assert response.status == 409
    assert body.get("code") == "target_replaced"
    assert teardown_spies["retire"] == 0
    assert teardown_spies["hook"] == 0
    assert state._slots.get("chat-1") is held
