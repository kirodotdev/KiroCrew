"""HELLO's ``heartbeat_interval`` becomes a bounded heartbeat period.

The interval goes straight to ``asyncio.sleep`` in the heartbeat loop. A NaN
never returns, so the op-1 heartbeat stops and Discord drops the session; zero
or a negative spins the loop. A missing, non-finite or non-positive value takes
Discord's documented 41.25s, and any other value is clamped to [1s, 300s].

No network: ``_start_heartbeat`` is replaced by a recorder, so the asserted
value is the period the client would heartbeat at.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.discord.client import _OP_HELLO, _OP_IDENTIFY, DiscordClient

# Spelled as literals so the test reads the same against a tree without the bounds.
_DEFAULT_HEARTBEAT_SECS = 41.25
_MIN_HEARTBEAT_SECS = 1.0
_MAX_HEARTBEAT_SECS = 300.0


class _FakeWS:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


async def _interval_for(hello: Any, monkeypatch: pytest.MonkeyPatch) -> list[float]:
    client = DiscordClient(token="bot-secret")
    intervals: list[float] = []
    monkeypatch.setattr(client, "_start_heartbeat", lambda ws, interval: intervals.append(interval))
    ws = _FakeWS()
    await client._handle_frame(ws, {"op": _OP_HELLO, "d": hello})
    assert ws.sent and ws.sent[0]["op"] == _OP_IDENTIFY, "HELLO still identifies"
    return intervals


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [float("nan"), "nan", float("inf"), float("-inf"), 0, -41250, "not-a-number", None, True],
)
async def test_an_unusable_interval_heartbeats_at_discords_default(
    raw: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await _interval_for({"heartbeat_interval": raw}, monkeypatch) == [
        _DEFAULT_HEARTBEAT_SECS
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("hello", [{}, None, "garbage"])
async def test_a_hello_without_an_interval_heartbeats_at_discords_default(
    hello: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await _interval_for(hello, monkeypatch) == [_DEFAULT_HEARTBEAT_SECS]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (41250, 41.25),
        ("45000", 45.0),
        (1, _MIN_HEARTBEAT_SECS),
        (10**9, _MAX_HEARTBEAT_SECS),
    ],
)
async def test_a_usable_interval_is_honored_within_bounds(
    raw: Any, expected: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await _interval_for({"heartbeat_interval": raw}, monkeypatch) == [expected]
