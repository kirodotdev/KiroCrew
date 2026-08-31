"""A command-shaped OPTIONS click must resolve to the same linked slot as any message.

``maybe_route_linked_thread`` lets a ``!``-bang command fall through to normal handling so
a user can still run one inside a linked thread. An OPTIONS label is MODEL-AUTHORED, so
falling through would persist the turn under the dashboard key while the linked slot's
transcript never saw it -- the click would land in a different slot than an ordinary
message from the same thread.
"""

from __future__ import annotations

import pytest

from kiro_crew.slack import handler


class _Slot:
    """Only what the router touches past the bang check."""

    key = "chat-1-1"
    running = True  # take the queue branch, so no real chat task is spawned
    task = None

    def __init__(self) -> None:
        self.queued: list[str] = []

    def queue_append(self, text, **_kw):
        self.queued.append(text)


class _State:
    def __init__(self) -> None:
        self.slot = _Slot()
        self.pushes = 0
        self._background_tasks: set = set()

    def get_linked_slot(self, _reply_ts):
        return self.slot

    def push_slots_update(self):
        self.pushes += 1


class _Slack:
    def __init__(self) -> None:
        self.posted: list[str] = []

    async def post_message(self, _channel, text, _ts=None, **_kw):
        self.posted.append(text)
        return {"ts": "1.0"}


@pytest.fixture
def linked(monkeypatch):
    state = _State()
    monkeypatch.setattr(handler, "_dashboard_state", state, raising=False)
    monkeypatch.setattr(handler, "is_allowed_user", lambda _uid: True, raising=False)
    monkeypatch.setattr(handler, "append_and_surface", lambda *a, **k: None, raising=False)
    return state


async def _route(state, text: str, *, interpret_commands: bool) -> bool:
    return await handler.maybe_route_linked_thread(
        text,
        "slack:1.0",
        "U1",
        "C1",
        _Slack(),
        "1.0",
        interpret_commands=interpret_commands,
    )


BANGS = ["!yolo on", "!agent atlas", "!stop", "!dashboard", "!restart"]


class TestAModelAuthoredBangLabelStaysInTheLinkedSlot:
    @pytest.mark.parametrize("text", BANGS)
    @pytest.mark.asyncio
    async def test_it_does_not_fall_through(self, linked, text):
        assert await _route(linked, text, interpret_commands=False) is True
        assert linked.slot.queued == [text]

    @pytest.mark.asyncio
    async def test_an_ordinary_label_routes_the_same_way(self, linked):
        # The parity the finding asks for: same slot for command-shaped and plain text.
        assert await _route(linked, "Merge it now", interpret_commands=False) is True
        assert linked.slot.queued == ["Merge it now"]


class TestAUserTypedBangStillReachesTheCommandHandler:
    @pytest.mark.parametrize("text", BANGS)
    @pytest.mark.asyncio
    async def test_it_still_falls_through(self, linked, text):
        assert await _route(linked, text, interpret_commands=True) is False
        assert linked.slot.queued == []
