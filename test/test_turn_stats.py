"""Tests for per-turn stats (elapsed / credits) attached to assistant messages.

Covers ``chat_runner._attach_turn_stats``: the helper that mirrors
``_flush_file_changes`` by stashing ``turn_stats`` meta on the last assistant
message of a completed turn, so the dashboard footer can show the same
end-of-turn elapsed/credits line kiro-cli prints natively.
"""

import time

import pytest

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import _attach_owned_turn_stats, _attach_turn_stats
from kiro_crew.dashboard.handlers import usage
from kiro_crew.dashboard.state import _ChatSlot


def _make_slot_with_assistant_message() -> _ChatSlot:
    slot = _ChatSlot("test-turn-stats")
    slot.append("assistant", "done.", "msg msg-a", broadcast=False)
    return slot


def test_footer_reads_the_auto_aware_model_helper():
    """The footer must use ``read_turn_model``, not ``read_effective_model``.

    Both readers exist and differ only on the Auto path: the latter reports ""
    there, which the footer renders as nothing — indistinguishable from a turn
    with no model information at all. Binding the footer to the wrong one is a
    silent regression, since every pinned-model assertion still passes.
    """
    assert chat_runner.read_turn_model is usage.read_turn_model


class TestAttachTurnStats:
    def test_attaches_elapsed_and_credits(self):
        slot = _make_slot_with_assistant_message()
        _attach_turn_stats(slot, 12345, 1.25, 0.0)
        meta = slot.messages[-1]["meta"]
        assert meta["turn_stats"] == {"elapsed_ms": 12345, "credits": 1.25}

    def test_zero_credits_key_omitted(self):
        # claude_code bills cost_usd, not credits — credits key must not appear.
        slot = _make_slot_with_assistant_message()
        _attach_turn_stats(slot, 8000, 0.0, 0.0231)
        stats = slot.messages[-1]["meta"]["turn_stats"]
        assert "credits" not in stats
        assert stats["cost_usd"] == 0.0231
        assert stats["elapsed_ms"] == 8000

    def test_zero_cost_key_omitted(self):
        slot = _make_slot_with_assistant_message()
        _attach_turn_stats(slot, 5000, 2.5, 0.0)
        stats = slot.messages[-1]["meta"]["turn_stats"]
        assert "cost_usd" not in stats
        assert stats["credits"] == 2.5

    def test_no_elapsed_is_noop(self):
        # elapsed_ms=0 means EVENT_COMPLETE never arrived (aborted turn) —
        # nothing should be attached.
        slot = _make_slot_with_assistant_message()
        _attach_turn_stats(slot, 0, 1.0, 0.0)
        assert "turn_stats" not in slot.messages[-1].get("meta", {})

    def test_no_assistant_message_is_noop(self):
        # Error-only turns have no assistant message; the helper must not
        # fabricate one (unlike _flush_file_changes, stats alone aren't worth
        # a synthetic bubble).
        slot = _ChatSlot("test-no-assistant")
        slot.append("error", "boom", "msg msg-err", broadcast=False)
        _attach_turn_stats(slot, 9000, 0.5, 0.0)
        assert len(slot.messages) == 1
        assert "turn_stats" not in slot.messages[0].get("meta", {})

    def test_attaches_to_last_assistant_not_earlier(self):
        slot = _ChatSlot("test-multi")
        slot.append("assistant", "first segment", "msg msg-a", broadcast=False)
        slot.append("tool", "ran a tool", "msg msg-tool", broadcast=False)
        slot.append("assistant", "final answer", "msg msg-a", broadcast=False)
        _attach_turn_stats(slot, 4000, 0.75, 0.0)
        assert "turn_stats" not in slot.messages[0].get("meta", {})
        assert slot.messages[2]["meta"]["turn_stats"]["credits"] == 0.75

    def test_credits_rounded(self):
        slot = _make_slot_with_assistant_message()
        _attach_turn_stats(slot, 1000, 0.123456789, 0.0)
        assert slot.messages[-1]["meta"]["turn_stats"]["credits"] == 0.1235

    def test_model_included_when_resolved(self):
        # The served model id (read_turn_model at EVENT_COMPLETE) rides along so
        # the footer can confirm what a pinned session actually ran on.
        slot = _make_slot_with_assistant_message()
        _attach_turn_stats(slot, 3000, 1.0, 0.0, model="claude-sonnet-4.6")
        stats = slot.messages[-1]["meta"]["turn_stats"]
        assert stats["model"] == "claude-sonnet-4.6"

    def test_auto_sentinel_is_carried_like_any_other_value(self):
        # An Auto turn arrives as the literal "auto" — not a model id, but the
        # only true answer available, and it must reach the footer rather than
        # being filtered back into the omitted-key case below.
        slot = _make_slot_with_assistant_message()
        _attach_turn_stats(slot, 3000, 1.0, 0.0, model="auto")
        assert slot.messages[-1]["meta"]["turn_stats"]["model"] == "auto"

    def test_model_omitted_when_unattributable(self):
        # No model information at all yields "" — the key must be absent, not
        # empty, so the frontend renders nothing rather than a blank chip.
        slot = _make_slot_with_assistant_message()
        _attach_turn_stats(slot, 3000, 1.0, 0.0, model="")
        assert "model" not in slot.messages[-1]["meta"]["turn_stats"]

    def test_error_only_turn_does_not_overwrite_previous_turn(self):
        # Regression (Codex HIGH): turn 1 completes with an assistant message
        # and stats; turn 2 fails producing only an error message. Without the
        # boundary, the reverse scan would walk into turn 1's assistant message
        # and overwrite its stats with turn 2's numbers.
        slot = _ChatSlot("test-boundary")
        slot.append("assistant", "turn 1 answer", "msg msg-a", broadcast=False)
        _attach_turn_stats(slot, 5000, 1.0, 0.0, turn_boundary=0)
        assert slot.messages[0]["meta"]["turn_stats"]["elapsed_ms"] == 5000

        # Turn 2 starts: boundary = current message count. Only an error lands.
        boundary = len(slot.messages)
        slot.append("error", "boom", "msg msg-err", broadcast=False)
        _attach_turn_stats(slot, 99_000, 9.9, 0.0, turn_boundary=boundary)

        # Turn 1's stats are untouched; the error message got nothing.
        assert slot.messages[0]["meta"]["turn_stats"] == {
            "elapsed_ms": 5000,
            "credits": 1.0,
        }
        assert "turn_stats" not in slot.messages[1].get("meta", {})

    def test_boundary_scopes_to_current_turn_assistant(self):
        # Assistant from a prior turn + assistant from this turn: stats land
        # on this turn's message only.
        slot = _ChatSlot("test-boundary-2")
        slot.append("assistant", "old turn", "msg msg-a", broadcast=False)
        boundary = len(slot.messages)
        slot.append("assistant", "this turn", "msg msg-a", broadcast=False)
        _attach_turn_stats(slot, 3000, 0.5, 0.0, turn_boundary=boundary)
        assert "turn_stats" not in slot.messages[0].get("meta", {})
        assert slot.messages[1]["meta"]["turn_stats"]["credits"] == 0.5

    def test_reply_mids_skips_a_row_another_writer_appended(self):
        # A workflow result lands after this turn's reply: with reply_mids the
        # stats stay on the reply the runner appended.
        from kiro_crew.dashboard.state import row_mid

        slot = _ChatSlot("test-reply-mids")
        reply = slot.append("assistant", "this turn", "msg msg-a", broadcast=False)
        slot.append("assistant", "workflow result", "msg msg-a", broadcast=False)
        _attach_owned_turn_stats(
            slot,
            3000,
            0.5,
            0.0,
            turn_boundary=0,
            model="",
            ttft_ms=0,
            reply_mids=[row_mid(reply)],
        )
        assert slot.messages[0]["meta"]["turn_stats"]["credits"] == 0.5
        assert "turn_stats" not in slot.messages[1].get("meta", {})

    def test_boundary_reset_after_clear_attaches_to_confirmation(self):
        # Regression (Codex MEDIUM): a clear-conversation turn empties
        # slot.messages then appends a "Conversation cleared" confirmation.
        # The clear handler resets the turn boundary to 0; simulate that here
        # and confirm the completed turn's stats still land on the
        # confirmation message rather than being dropped.
        slot = _ChatSlot("test-clear-boundary")
        # Prior turn(s) left several messages; boundary captured before clear.
        slot.append("user", "hello", "msg msg-u", broadcast=False)
        slot.append("assistant", "prior answer", "msg msg-a", broadcast=False)
        pre_clear_boundary = len(slot.messages)

        # Clear handler: empties the list, resets boundary, appends confirmation.
        slot.messages.clear()
        reset_boundary = 0
        slot.append("assistant", "🗑️ Conversation cleared.", "msg msg-a", broadcast=False)

        # With the stale (pre-clear) boundary the scan slice would be empty;
        # the reset boundary keeps the confirmation in scope.
        assert len(slot.messages[pre_clear_boundary:]) == 0
        _attach_turn_stats(slot, 2500, 0.3, 0.0, turn_boundary=reset_boundary)
        assert slot.messages[-1]["meta"]["turn_stats"] == {
            "elapsed_ms": 2500,
            "credits": 0.3,
        }

    def test_preserves_existing_meta(self):
        # turn_stats must coexist with other meta (e.g. file_changes).
        slot = _make_slot_with_assistant_message()
        slot.messages[-1]["meta"] = {"file_changes": [{"path": "/tmp/x"}]}
        _attach_turn_stats(slot, 2000, 1.0, 0.0)
        meta = slot.messages[-1]["meta"]
        assert meta["file_changes"] == [{"path": "/tmp/x"}]
        assert meta["turn_stats"]["elapsed_ms"] == 2000


class TestTurnStatsTtft:
    """``ttft_ms`` lands in ``turn_stats`` so latency is readable with telemetry off."""

    def test_attach_reports_whether_a_row_received_the_stats(self):
        slot = _make_slot_with_assistant_message()
        assert _attach_turn_stats(slot, 9000, 1.0, 0.0) is True
        assert _attach_turn_stats(slot, 0, 1.0, 0.0) is False
        empty = _ChatSlot("no-reply")
        assert _attach_turn_stats(empty, 9000, 1.0, 0.0) is False

    def test_ttft_ms_attached_when_measured(self):
        slot = _make_slot_with_assistant_message()
        _attach_turn_stats(slot, 9000, 1.0, 0.0, ttft_ms=2345)
        assert slot.messages[-1]["meta"]["turn_stats"] == {
            "elapsed_ms": 9000,
            "credits": 1.0,
            "ttft_ms": 2345,
        }

    def test_ttft_ms_omitted_when_unmeasured(self):
        # A synthetic or nested prompt never starts the clock, so it reports 0.
        slot = _make_slot_with_assistant_message()
        _attach_turn_stats(slot, 9000, 1.0, 0.0)
        assert "ttft_ms" not in slot.messages[-1]["meta"]["turn_stats"]

    def test_clock_stops_at_first_non_empty_broadcast(self):
        # A redactor that withholds the first chunk feeds "" first; the clock
        # must keep running until real output reaches the wire.
        now = [11.0]
        clock = chat_runner._FirstVisibleClock(10.0, clock=lambda: now[0])
        clock.mark("")
        assert clock.ms == 0
        now[0] = 12.5
        clock.mark("Hel")
        now[0] = 30.0
        clock.mark("lo")
        assert clock.ms == 2500

    def test_clock_without_start_never_measures(self):
        clock = chat_runner._FirstVisibleClock(None)
        clock.mark("text")
        assert clock.ms == 0

    def test_tool_only_turn_keeps_the_clock_running_into_the_continuation(self):
        # The first turn broadcast nothing; its clock still runs from the user
        # turn's start and stops at the continuation's first output.
        slot = _make_slot_with_assistant_message()
        now = [11.0]
        first = chat_runner._turn_clock(slot, 10.0, top_level=True, recovery_turn=False)
        first._clock = lambda: now[0]
        assert slot._carried_ttft_clock is first
        cont = chat_runner._turn_clock(slot, None, top_level=True, recovery_turn=True)
        assert cont is first
        now[0] = 14.0
        cont.mark("reply")
        assert cont.ms == 4000

    def test_carried_clock_survives_a_requeued_recovery_turn(self):
        slot = _make_slot_with_assistant_message()
        carried = chat_runner._FirstVisibleClock(None)
        carried.ms = 1800
        slot._carried_ttft_clock = carried
        assert chat_runner._turn_clock(slot, None, top_level=True, recovery_turn=True).ms == 1800
        assert chat_runner._turn_clock(slot, None, top_level=True, recovery_turn=True).ms == 1800
        assert slot._carried_ttft_clock is carried

    def test_carried_clock_is_replaced_by_a_new_user_turn(self):
        slot = _make_slot_with_assistant_message()
        old = chat_runner._FirstVisibleClock(None)
        old.ms = 900
        slot._carried_ttft_clock = old
        fresh = chat_runner._turn_clock(slot, 5.0, top_level=True, recovery_turn=False)
        assert slot._carried_ttft_clock is fresh
        assert fresh.ms == 0

    def test_first_recovery_with_no_stored_clock_stores_its_own(self):
        slot = _make_slot_with_assistant_message()
        first = chat_runner._turn_clock(slot, None, top_level=True, recovery_turn=True)
        assert slot._carried_ttft_clock is first
        assert chat_runner._turn_clock(slot, None, top_level=True, recovery_turn=True) is first

    def test_nested_prompt_never_takes_the_carried_clock(self):
        slot = _make_slot_with_assistant_message()
        carried = chat_runner._FirstVisibleClock(None)
        slot._carried_ttft_clock = carried
        nested = chat_runner._turn_clock(slot, None, top_level=False, recovery_turn=True)
        assert nested is not carried
        assert slot._carried_ttft_clock is carried


@pytest.mark.asyncio
async def test_steer_cut_of_a_withheld_first_chunk_still_records_ttft(tmp_path, monkeypatch):
    """The redactor can hold back the whole first chunk; a steer then persists it
    without a broadcast. That persisted text is the first output the user sees, so
    the clock must stop there rather than leave ``ttft_ms`` out."""
    from test_dashboard_chat import TestRunChatTransientRetry as _Suite

    from kiro_crew.acp.types import TurnUsage
    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
    from kiro_crew.security import StreamRedactor

    assert StreamRedactor().feed("Hello") == "", "precondition: the chunk is withheld"
    state = _Suite._make_state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    slot._titled = True

    async def _stream(msg):
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Hello")
        slot._steer_segment_cut()
        # The quietly persisted text shows only at the turn-end refresh, so the
        # time spent after the cut belongs in the measurement.
        time.sleep(0.08)
        yield LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(duration_ms=4000, credits=0.5))

    _Suite._wire_sessions(state, _Suite._client(_stream))
    await _run_chat(state, slot, "hi")
    await _Suite._drain_bg(state)

    stats = [
        m["meta"]["turn_stats"] for m in slot.messages if (m.get("meta") or {}).get("turn_stats")
    ]
    assert stats, "the turn attached no stats"
    assert stats[-1].get("ttft_ms", 0) >= 80


@pytest.mark.asyncio
async def test_recovery_turn_without_a_carried_clock_stores_no_ttft(tmp_path, monkeypatch):
    """A recovery that re-sends the ORIGINAL payload must not time from its own
    dispatch: the user message it answers belongs to an earlier turn."""
    from test_dashboard_chat import TestRunChatTransientRetry as _Suite

    from kiro_crew.acp.types import TurnUsage
    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    state = _Suite._make_state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    slot._titled = True
    assert slot._carried_ttft_clock is None

    async def _stream(msg):
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done. ")
        yield LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(duration_ms=4000, credits=0.5))

    _Suite._wire_sessions(state, _Suite._client(_stream))
    await _run_chat(state, slot, "do the thing", _synthetic_recovery_turn=True)
    await _Suite._drain_bg(state)

    stats = [
        m["meta"]["turn_stats"] for m in slot.messages if (m.get("meta") or {}).get("turn_stats")
    ]
    assert stats, "the turn attached no stats"
    assert "ttft_ms" not in stats[-1]
