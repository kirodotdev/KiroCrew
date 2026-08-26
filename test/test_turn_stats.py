"""Tests for per-turn stats (elapsed / credits) attached to assistant messages.

Covers ``chat_runner._attach_turn_stats``: the helper that mirrors
``_flush_file_changes`` by stashing ``turn_stats`` meta on the last assistant
message of a completed turn, so the dashboard footer can show the same
end-of-turn elapsed/credits line kiro-cli prints natively.
"""
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import _attach_turn_stats
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
            "elapsed_ms": 5000, "credits": 1.0,
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
        slot.append("assistant", "🗑️ Conversation cleared.", "msg msg-a",
                    broadcast=False)

        # With the stale (pre-clear) boundary the scan slice would be empty;
        # the reset boundary keeps the confirmation in scope.
        assert len(slot.messages[pre_clear_boundary:]) == 0
        _attach_turn_stats(slot, 2500, 0.3, 0.0, turn_boundary=reset_boundary)
        assert slot.messages[-1]["meta"]["turn_stats"] == {
            "elapsed_ms": 2500, "credits": 0.3,
        }

    def test_preserves_existing_meta(self):
        # turn_stats must coexist with other meta (e.g. file_changes).
        slot = _make_slot_with_assistant_message()
        slot.messages[-1]["meta"] = {"file_changes": [{"path": "/tmp/x"}]}
        _attach_turn_stats(slot, 2000, 1.0, 0.0)
        meta = slot.messages[-1]["meta"]
        assert meta["file_changes"] == [{"path": "/tmp/x"}]
        assert meta["turn_stats"]["elapsed_ms"] == 2000
