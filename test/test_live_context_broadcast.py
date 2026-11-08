"""Tests for chat_runner._broadcast_live_context.

A ``usage_update`` moves the provider's context counts while a turn runs and
emits no event of its own. The stream loop calls ``_broadcast_live_context``
once per event so the meter follows those counts; these tests pin its throttle
and its refusal to send an unmeasured reading mid-turn.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from kiro_crew.dashboard.chat_runner import _broadcast_live_context
from kiro_crew.dashboard.chat_turn.turn_stats import _LIVE_CONTEXT_MIN_INTERVAL_S


class _Client:
    def __init__(self, used: int, window: int, pct: float) -> None:
        self.used, self.window, self.pct = used, window, pct

    def context_usage_pct(self) -> float:
        return self.pct

    def context_used_tokens(self) -> int:
        return self.used

    def context_window_tokens(self) -> int:
        return self.window


def _turn(client: object) -> SimpleNamespace:
    return SimpleNamespace(
        client=client,
        state=MagicMock(),
        slot=SimpleNamespace(key="dashboard:1"),
        _live_context_reading=None,
        _live_context_at=float("-inf"),
    )


def test_a_measured_reading_is_broadcast_once_until_it_changes() -> None:
    client = _Client(used=120390, window=262144, pct=45.9)
    turn = _turn(client)
    _broadcast_live_context(turn, clock=lambda: 100.0)
    turn.state.broadcast_context_usage.assert_called_once_with(
        "dashboard:1",
        {"slot": "dashboard:1", "pct": 45.9, "used_tokens": 120390, "window_tokens": 262144},
    )
    # The same reading on a later event sends nothing, however late.
    _broadcast_live_context(turn, clock=lambda: 500.0)
    assert turn.state.broadcast_context_usage.call_count == 1


def test_a_changed_reading_waits_out_the_throttle() -> None:
    client = _Client(used=1000, window=10000, pct=10.0)
    turn = _turn(client)
    _broadcast_live_context(turn, clock=lambda: 100.0)
    client.used, client.pct = 2000, 20.0
    _broadcast_live_context(turn, clock=lambda: 100.0 + _LIVE_CONTEXT_MIN_INTERVAL_S / 2)
    assert turn.state.broadcast_context_usage.call_count == 1
    _broadcast_live_context(turn, clock=lambda: 100.0 + _LIVE_CONTEXT_MIN_INTERVAL_S)
    assert turn.state.broadcast_context_usage.call_count == 2
    assert turn.state.broadcast_context_usage.call_args.args[1]["used_tokens"] == 2000


def test_an_unmeasured_reading_is_never_sent_mid_turn() -> None:
    # Its payload would be a reset that wipes the stored counts; the turn-end
    # frame stays the one that may reset them.
    for used, window in ((0, 0), (0, 200000), (5000, 0)):
        turn = _turn(_Client(used=used, window=window, pct=0.0))
        _broadcast_live_context(turn, clock=lambda: 100.0)
        turn.state.broadcast_context_usage.assert_not_called()


def test_a_failing_provider_never_breaks_the_stream() -> None:
    client = MagicMock()
    client.context_window_tokens.side_effect = RuntimeError("runtime gone")
    turn = _turn(client)
    _broadcast_live_context(turn, clock=lambda: 100.0)
    turn.state.broadcast_context_usage.assert_not_called()
