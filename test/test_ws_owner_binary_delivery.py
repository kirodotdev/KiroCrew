"""``deliver_ws_owners_bytes``: one binary frame to owner sockets only, same drop rule as text."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.dashboard import state as state_facade
from kiro_crew.dashboard import websocket_hub as wh
from kiro_crew.dashboard.state_parts import dashboard_state, slots_push

pytestmark = pytest.mark.asyncio


class _Socket(dict):
    """A ``WebSocketResponse`` stand-in: records what was sent, by kind."""

    def __init__(self, *, closed: bool = False, fail: bool = False) -> None:
        super().__init__()
        self.closed = closed
        self.sent_bytes: list[bytes] = []
        self.sent_text: list[str] = []
        self._fail = fail

    # A dict is unhashable; the hub keeps sockets in a set, so identity hashing
    # stands in for ``WebSocketResponse``'s.
    __hash__ = object.__hash__

    def __eq__(self, other: object) -> bool:
        return self is other

    async def send_bytes(self, data: bytes) -> None:
        if self._fail:
            raise ConnectionResetError("gone")
        self.sent_bytes.append(bytes(data))

    async def send_str(self, data: str) -> None:
        self.sent_text.append(data)


def _hub() -> tuple[wh.WebSocketHub, Any]:
    owner = SimpleNamespace(
        _ws_clients=[],
        _owner_ws_clients=set(),
        _ws_log_subscribers=set(),
        _ws_subagent_subscribers=set(),
        _background_tasks=set(),
        _flush_task=None,
    )
    hub = wh.WebSocketHub(
        owner,
        serving_loop_provider=lambda: None,
        logger_provider=lambda: logging.getLogger("test"),
    )
    return hub, owner


async def test_binary_goes_to_owner_sockets_only_as_one_binary_message() -> None:
    hub, owner = _hub()
    owner_ws, viewer_ws = _Socket(), _Socket()
    owner._ws_clients.extend([owner_ws, viewer_ws])
    owner._owner_ws_clients.add(owner_ws)
    delivered = await hub.deliver_ws_owners_bytes(b"\x00\x00\x00\x02{}\xff\xd8")
    assert delivered == 1
    assert owner_ws.sent_bytes == [b"\x00\x00\x00\x02{}\xff\xd8"] and owner_ws.sent_text == []
    assert viewer_ws.sent_bytes == [] and viewer_ws.sent_text == []


async def test_a_closed_or_failing_owner_socket_is_not_counted_and_is_removed() -> None:
    hub, owner = _hub()
    live, closed, failing = _Socket(), _Socket(closed=True), _Socket(fail=True)
    owner._ws_clients.extend([live, closed, failing])
    owner._owner_ws_clients.update({live, closed, failing})
    assert await hub.deliver_ws_owners_bytes(b"frame") == 1
    assert live.sent_bytes == [b"frame"]
    assert owner._owner_ws_clients == {live}


async def test_no_owner_socket_means_nothing_sent() -> None:
    hub, owner = _hub()
    owner._ws_clients.append(_Socket())
    assert await hub.deliver_ws_owners_bytes(b"frame") == 0


async def test_text_delivery_still_uses_send_str() -> None:
    hub, owner = _hub()
    ws = _Socket()
    owner._ws_clients.append(ws)
    owner._owner_ws_clients.add(ws)
    assert await hub.deliver_ws_owners("browser_live", {"state": "live"}) == 1
    assert ws.sent_bytes == [] and '"type": "browser_live"' in ws.sent_text[0]


async def test_the_state_facade_forwards_to_the_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[bytes] = []

    class _Hub:
        async def deliver_ws_owners_bytes(self, payload: bytes) -> int:
            sent.append(payload)
            return 1

    state = SimpleNamespace()
    assert (
        dashboard_state.DashboardState.deliver_ws_owners_bytes is slots_push.deliver_ws_owners_bytes
    )
    # Through the facade, so every owner that binds the name sees the stand-in.
    monkeypatch.setattr(state_facade, "_websocket_for", lambda _s: _Hub())
    assert await slots_push.deliver_ws_owners_bytes(state, b"x") == 1  # type: ignore[arg-type]
    assert sent == [b"x"]
