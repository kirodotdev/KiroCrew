"""NotificationGate — leading-edge merge window, quiet mode, priority bypass.

The properties pinned here are the gate's contract with notify_user:

* Leading edge: the FIRST notification in a quiet stretch is never delayed.
* Bursts coalesce: arrivals during the open window flush as one merged
  summary per source when the window closes.
* ``critical`` bypasses both the window and quiet mode.
* Quiet mode holds the backlog and flushes it merged on resume or expiry.
* Eviction under pressure prefers the oldest ``low`` item.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.apps.builtins.mochi.notification_gate import (
    MAX_BUFFER_SIZE,
    MERGE_WINDOW_MS,
    NotificationGate,
)

T0 = 1_000_000


def _notify(summary: str, **extra: Any) -> dict[str, Any]:
    return {"action": "notify", "summary": summary, **extra}


class _Spy:
    def __init__(self) -> None:
        self.delivered: list[dict[str, Any]] = []

    def __call__(self, action: dict[str, Any]) -> None:
        self.delivered.append(action)


class TestLeadingEdge:
    def test_first_notification_delivers_immediately(self):
        spy = _Spy()
        gate = NotificationGate(spy)
        gate.push(_notify("hello"), T0)
        assert [a["summary"] for a in spy.delivered] == ["hello"]

    def test_lone_notification_adds_no_followup(self):
        """A single notify opens a window that closes empty — no second delivery."""
        spy = _Spy()
        gate = NotificationGate(spy)
        gate.push(_notify("only"), T0)
        gate.tick(T0 + MERGE_WINDOW_MS)
        assert len(spy.delivered) == 1

    def test_burst_coalesces_after_window(self):
        spy = _Spy()
        gate = NotificationGate(spy)
        gate.push(_notify("first", source="watch"), T0)
        gate.push(_notify("second", source="watch"), T0 + 100)
        gate.push(_notify("third", source="watch"), T0 + 200)
        # Only the leading delivery so far.
        assert len(spy.delivered) == 1
        gate.tick(T0 + MERGE_WINDOW_MS)
        assert len(spy.delivered) == 2
        merged = spy.delivered[1]
        assert merged["summary"] == "second\nthird"

    def test_window_reopens_after_flush(self):
        """A notify after the window closed is a fresh leading edge."""
        spy = _Spy()
        gate = NotificationGate(spy)
        gate.push(_notify("a"), T0)
        gate.tick(T0 + MERGE_WINDOW_MS)
        gate.push(_notify("b"), T0 + MERGE_WINDOW_MS + 1)
        assert [a["summary"] for a in spy.delivered] == ["a", "b"]


class TestMergedDelivery:
    def _flush(self, notifs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        spy = _Spy()
        gate = NotificationGate(spy)
        gate.push(notifs[0], T0)
        for i, n in enumerate(notifs[1:]):
            gate.push(n, T0 + 1 + i)
        gate.tick(T0 + MERGE_WINDOW_MS)
        return spy.delivered

    def test_reminder_group_over_three_summarises(self):
        out = self._flush([_notify(f"r{i}", source="reminder") for i in range(5)])
        # leading + merged(4)
        assert out[1]["summary"] == "⏰ 4 reminders"

    def test_watch_all_low_summarises_quietly(self):
        out = self._flush([_notify(f"w{i}", source="watch", priority="low") for i in range(4)])
        assert out[1]["summary"] == "👀 Checked 3 watch items — all unchanged"

    def test_approval_group_counts(self):
        out = self._flush([_notify(f"a{i}", source="approval") for i in range(4)])
        assert out[1]["summary"] == "🔧 3 operations waiting for approval"

    def test_default_group_over_two_counts(self):
        out = self._flush([_notify(f"n{i}", source="system") for i in range(5)])
        assert out[1]["summary"] == "4 notifications"

    def test_sources_flush_separately(self):
        out = self._flush(
            [
                _notify("lead", source="system"),
                _notify("w1", source="watch"),
                _notify("w2", source="watch"),
                _notify("r1", source="reminder"),
            ]
        )
        # leading + merged watch pair + lone reminder
        assert len(out) == 3
        assert out[1]["summary"] == "w1\nw2"
        assert out[2]["summary"] == "r1"

    def test_merged_flags_and_mood(self):
        out = self._flush(
            [
                _notify("lead", source="x"),
                _notify("a", source="x"),
                _notify("b", source="x", sticky=True, mood="excited"),
                _notify("c", source="x", pushToChat=True, chatMessage="detail c"),
            ]
        )
        merged = out[1]
        assert merged["sticky"] is True
        assert merged["pushToChat"] is True
        # First-with-mood wins; chat detail joins every constituent.
        assert merged["mood"] == "excited"
        assert merged["chatMessage"] == "a\nb\ndetail c"


class TestCriticalBypass:
    def test_critical_skips_open_window(self):
        spy = _Spy()
        gate = NotificationGate(spy)
        gate.push(_notify("lead"), T0)
        gate.push(_notify("urgent", priority="critical"), T0 + 100)
        assert [a["summary"] for a in spy.delivered] == ["lead", "urgent"]

    def test_critical_skips_quiet_mode(self):
        spy = _Spy()
        gate = NotificationGate(spy)
        gate.silence(T0, 60)
        gate.push(_notify("urgent", priority="critical"), T0 + 100)
        assert [a["summary"] for a in spy.delivered] == ["urgent"]


class TestQuietMode:
    def test_silence_holds_and_resume_flushes_merged(self):
        spy = _Spy()
        gate = NotificationGate(spy)
        gate.silence(T0, 60)
        gate.push(_notify("a", source="x"), T0 + 1)
        gate.push(_notify("b", source="x"), T0 + 2)
        assert spy.delivered == []
        assert gate.pending_count == 2
        gate.unsilence(T0 + 3)
        assert len(spy.delivered) == 1
        assert spy.delivered[0]["summary"] == "a\nb"
        assert gate.silent_until == 0

    def test_expiry_observed_by_tick_flushes(self):
        spy = _Spy()
        gate = NotificationGate(spy)
        gate.silence(T0, 1)
        gate.push(_notify("held"), T0 + 1)
        gate.tick(T0 + 30_000)  # still quiet
        assert spy.delivered == []
        gate.tick(T0 + 60_000)  # deadline passed
        assert [a["summary"] for a in spy.delivered] == ["held"]
        assert gate.silent_until == 0

    def test_is_silent_is_a_plain_predicate(self):
        """Reading silence state must not flush (unlike the removed port)."""
        spy = _Spy()
        gate = NotificationGate(spy)
        gate.silence(T0, 1)
        gate.push(_notify("held"), T0 + 1)
        assert gate.is_silent(T0 + 120_000) is False
        assert spy.delivered == []  # only tick/unsilence flush
        assert gate.pending_count == 1


class TestEviction:
    def test_prefers_oldest_low(self):
        spy = _Spy()
        gate = NotificationGate(spy)
        gate.silence(T0, 60)
        gate.push(_notify("normal-0", source="x"), T0)
        gate.push(_notify("low-0", source="x", priority="low"), T0 + 1)
        for i in range(MAX_BUFFER_SIZE - 1):
            gate.push(_notify(f"n{i}", source="x"), T0 + 2 + i)
        assert gate.pending_count == MAX_BUFFER_SIZE
        summaries = [n["summary"] for n in gate._buffer]
        assert "low-0" not in summaries
        assert "normal-0" in summaries

    def test_falls_back_to_oldest_when_no_low(self):
        spy = _Spy()
        gate = NotificationGate(spy)
        gate.silence(T0, 60)
        for i in range(MAX_BUFFER_SIZE + 1):
            gate.push(_notify(f"n{i}", source="x"), T0 + i)
        summaries = [n["summary"] for n in gate._buffer]
        assert "n0" not in summaries
        assert len(summaries) == MAX_BUFFER_SIZE
