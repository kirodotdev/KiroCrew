"""Per-call context readings from kiro-cli's ``_kiro.dev/metadata`` frames.

kiro-cli sends one metadata frame after every model call, carrying the context
percentage that call left, and closes the turn with a frame whose
``meteringUsage`` lists one credit entry per call and repeats the last
percentage. A session also sends one frame when it opens, before its first
call. The sequence below is that capture (``kiro-cli acp`` 2.27.1, one turn with
three tool calls, so four model calls).

These tests pin how ``AcpPromptStats`` turns the sequence into the turn's model
call count and context sum, that the ``AcpClient`` and ``AcpSessionHandle``
metadata trackers report the same turn, and that the usage row and the retry
sum carry both numbers.
"""

import math
from datetime import datetime, timezone

import pytest
from acp_runtime_helpers import _make_runtime, _register

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.runtime import AcpSessionHandle
from kiro_crew.acp.types import AcpPromptStats, JsonRpcMessage, TurnUsage
from kiro_crew.dashboard.handlers import usage as usage_mod
from kiro_crew.llm_helpers import _sum_usage

WINDOW = 1_000_000
# A fixed clock for the usage row: no assertion reads the timestamp, and a
# test must not read the wall clock to build one.
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
SESSION_OPEN_PCT = 4.2585
CALL_PCTS = [1.9034, 1.9158, 1.9282, 1.9323]
CALL_CREDITS = [0.0693, 0.0432, 0.0434, 0.0345]
EXPECTED_SUM = sum(round(WINDOW * pct / 100.0) for pct in CALL_PCTS)


def _turn_frames(*, session_open: bool = True, metered: bool = True) -> list[dict]:
    frames = [{"contextUsagePercentage": SESSION_OPEN_PCT}] if session_open else []
    frames += [{"contextUsagePercentage": pct} for pct in CALL_PCTS]
    if metered:
        frames.append(
            {
                "contextUsagePercentage": CALL_PCTS[-1],
                "meteringUsage": [
                    {"value": v, "unit": "credit", "unitPlural": "credits"} for v in CALL_CREDITS
                ],
            }
        )
    return frames


def _feed_stats(stats: AcpPromptStats, frames: list[dict]) -> None:
    """Apply frames the way both trackers do: sanitize, then note the frame."""
    for params in frames:
        pct = stats.sanitize_pct(params.get("contextUsagePercentage"))
        stats.note_metadata_frame(pct, params.get("meteringUsage"))


def _stats() -> AcpPromptStats:
    return AcpPromptStats(context_window_tokens=WINDOW)


class TestNoteMetadataFrame:
    def test_each_call_frame_records_the_context_it_left(self):
        stats = _stats()
        _feed_stats(stats, _turn_frames(session_open=False, metered=False))
        usage = stats.to_turn_usage()
        assert usage.num_turns == len(CALL_PCTS)
        assert usage.context_sum_tokens == EXPECTED_SUM

    def test_turn_end_frame_counts_calls_without_recording_its_repeat(self):
        stats = _stats()
        _feed_stats(stats, _turn_frames(session_open=False))
        assert stats.call_readings == len(CALL_PCTS)
        assert stats.metered_calls == len(CALL_CREDITS)
        usage = stats.to_turn_usage()
        assert usage.num_turns == len(CALL_PCTS)
        assert usage.context_sum_tokens == EXPECTED_SUM

    def test_session_open_reading_is_left_out_by_the_metered_count(self):
        stats = _stats()
        _feed_stats(stats, _turn_frames())
        # Recorded, because nothing in the frame tells it apart from a call...
        assert stats.call_readings == len(CALL_PCTS) + 1
        assert stats.first_call_reading == round(WINDOW * SESSION_OPEN_PCT / 100.0)
        # ...and dropped, because the turn metered exactly one call fewer.
        usage = stats.to_turn_usage()
        assert usage.num_turns == len(CALL_PCTS)
        assert usage.context_sum_tokens == EXPECTED_SUM

    def test_any_other_mismatch_keeps_every_reading(self):
        stats = _stats()
        _feed_stats(stats, _turn_frames(session_open=False, metered=False))
        stats.note_metadata_frame(None, [{"value": 0.1, "unit": "credit"}] * 2)
        usage = stats.to_turn_usage()
        # Two readings too many is not the session-open frame, so nothing is
        # dropped; the billed count still says how many calls there were.
        assert usage.num_turns == 2
        assert usage.context_sum_tokens == EXPECTED_SUM

    def test_unknown_window_counts_calls_but_sums_no_context(self):
        stats = AcpPromptStats()
        _feed_stats(stats, _turn_frames())
        usage = stats.to_turn_usage()
        assert usage.num_turns == len(CALL_CREDITS)
        assert usage.context_sum_tokens == 0

    def test_authoritative_usage_update_records_no_readings(self):
        stats = _stats()
        stats.context_tokens_from_usage = True
        _feed_stats(stats, _turn_frames(metered=False))
        assert stats.call_readings == 0
        assert stats.to_turn_usage().context_sum_tokens == 0

    def test_only_credit_entries_count_as_calls(self):
        stats = _stats()
        stats.note_metadata_frame(
            None,
            [
                {"value": 0.1, "unit": "credit"},
                {"value": 4096, "unit": "token"},
                "not-an-entry",
            ],
        )
        assert stats.metered_calls == 1

    def test_next_turn_starts_with_no_readings(self):
        stats = _stats()
        _feed_stats(stats, _turn_frames())
        nxt = stats.carry_over()
        assert (nxt.call_context_sum, nxt.call_readings, nxt.first_call_reading) == (0, 0, 0)
        assert nxt.metered_calls == 0
        usage = nxt.to_turn_usage()
        assert (usage.num_turns, usage.context_sum_tokens) == (0, 0)
        # The window is session state and survives, so the next turn can read.
        assert nxt.context_window_tokens == WINDOW


def _client_tracker(tmp_path):
    client = AcpClient(work_dir=tmp_path)
    client.last_prompt_stats.context_window_tokens = WINDOW
    return client


def _handle_tracker(tmp_path):
    rt, _, _ = _make_runtime()
    queues = _register(rt, "sA")
    handle = AcpSessionHandle("sA", queues["sA"], rt)
    handle.last_prompt_stats.context_window_tokens = WINDOW
    # A turn is in flight: an idle handle files a frame's credits as
    # between-turn spend, outside this turn's window.
    handle._turn_done.clear()
    return handle


@pytest.mark.parametrize(
    "make_tracker", [_client_tracker, _handle_tracker], ids=["client", "handle"]
)
def test_both_trackers_report_the_captured_turn(make_tracker, tmp_path):
    tracker = make_tracker(tmp_path)
    for params in _turn_frames():
        tracker._track_metadata(JsonRpcMessage(method="_kiro.dev/metadata", params=params))
    usage = tracker.last_prompt_stats.to_turn_usage()
    assert usage.num_turns == len(CALL_PCTS)
    assert usage.context_sum_tokens == EXPECTED_SUM
    assert math.isclose(usage.credits, sum(CALL_CREDITS))


class TestUsageRow:
    def test_row_carries_the_call_count_and_context_sum(self):
        event = TurnUsage(credits=0.19, num_turns=4, context_sum_tokens=EXPECTED_SUM)
        rec = usage_mod._build_token_record("chat-1", "auto", event, "acp", NOW)
        assert rec["turns"] == 4
        assert rec["context_sum"] == EXPECTED_SUM

    def test_row_reads_zero_when_the_turn_had_no_readings(self):
        rec = usage_mod._build_token_record("chat-1", "auto", TurnUsage(credits=1.0), "acp", NOW)
        assert rec["turns"] == 0
        assert rec["context_sum"] == 0


def test_retry_sum_keeps_both_attempts_calls_and_context():
    total = _sum_usage(
        TurnUsage(credits=1.0, num_turns=2, context_sum_tokens=100),
        TurnUsage(credits=2.0, num_turns=3, context_sum_tokens=50),
    )
    assert total.num_turns == 5
    assert total.context_sum_tokens == 150
    assert math.isclose(total.credits, 3.0)


def test_between_turn_frame_records_no_reading(tmp_path):
    # A frame read after the turn billed (compaction drain) belongs to no turn's
    # window, so it adds no reading or metered call, just as it adds no credits.
    client = _client_tracker(tmp_path)
    client._track_metadata(
        JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={
                "contextUsagePercentage": 40.0,
                "meteringUsage": [{"value": 0.1, "unit": "credit"}],
            },
        ),
        between_turns=True,
    )
    stats = client.last_prompt_stats
    assert (stats.call_readings, stats.metered_calls, stats.credits) == (0, 0, 0)
