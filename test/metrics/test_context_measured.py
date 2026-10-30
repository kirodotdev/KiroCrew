"""The context part of the ``usage`` fold, end to end: one fixed-size state.

Each test drives the REAL emitter into a real crew log, folds it with the real ``usage``
fold and reads it back through ``context_trace`` -- the writer, the fold and the reader
together, as the page sees them. The measurement itself is built by the real parser
from a captured engine frame (``test/fixtures/acp_frames/kas/profile/``), so the
numbers are the engine's own.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from crew_log_drain import assert_drained

from kiro_crew.acp import kas_profile
from kiro_crew.context_blocks import PHASE_PER_TURN, PHASE_SESSION_START, USER_LABEL
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import projection_usage_measured as measured_mod
from kiro_crew.crew_log.schema import Entry
from kiro_crew.dashboard.handlers import usage as usage_mod

SLOT = "chat-measured"
UNIT = "acp-measured"
FRAMES = Path(__file__).parent.parent / "fixtures" / "acp_frames" / "kas" / "profile"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    _floor_monkeypatch.setattr(crew_log_eager, "note_commit", lambda *a, **k: None)
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()
    yield
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()


def _real_profile(**changes: Any) -> dict[str, Any]:
    """The profile the handle builds from the captured summary frame and TTFT chunk.

    *changes* edits the breakdown (``total_chars``, ``history`` parts, categories) so a
    test can state one number while every other stays the engine's.
    """
    stats = type("Stats", (), {"kas_profile": None})()
    completion = json.loads((FRAMES / "turn_completion.summary.json").read_text("utf-8"))
    chunk = json.loads((FRAMES / "ttft_chunk.json").read_text("utf-8"))
    kas_profile.record_ttft(stats, chunk["params"]["update"]["_meta"]["kiro"])
    kas_profile.record_breakdown(stats, completion["params"]["update"]["_meta"]["kiro"])
    profile = copy.deepcopy(stats.kas_profile)
    breakdown = profile["breakdown"]
    for key, value in changes.items():
        if key in ("user", "assistant", "thinking", "compaction_summary"):
            breakdown["history"][key] = value
        elif key == "categories":
            breakdown["categories"].update(value)
        elif key == "no_ttft":
            profile.pop("ttft")
        else:
            breakdown[key] = value
    return profile


def _open(unit: str = UNIT, *, resumed: bool = False) -> None:
    crew_log_emit.on_session_opened(unit, slot=SLOT, resumed=resumed)
    crew_log_emit.on_request_configured(
        unit, 1, model="model-a", provider="acp", context_window=1_000_000
    )


_ENGINE_TURNS = 0


def _turn(
    turn: int,
    chars: int,
    profile: dict[str, Any] | None,
    *,
    unit: str = UNIT,
    used: int = 50_000,
    window: int = 1_000_000,
    phase: str = PHASE_PER_TURN,
) -> None:
    """One turn as the dashboard writes it: compose, measure, close."""
    global _ENGINE_TURNS
    if profile is not None:
        # Each run of a turn is a new engine turn, with an id of its own.
        _ENGINE_TURNS += 1
        profile = {**profile, "turn_id": f"engine-turn-{_ENGINE_TURNS}"}
    crew_log_emit.on_context_composed(
        unit,
        turn,
        blocks={USER_LABEL: 40, "lessons": chars - 40},
        phase=phase,
    )
    crew_log_emit.on_context_measured(unit, turn, profile=profile)
    crew_log_emit.on_turn_completed(
        unit,
        turn,
        model="model-a",
        credits=0.1,
        context_used=used,
        context_window=window,
        stop_reason="end_turn",
    )


def _trace() -> dict[str, Any]:
    assert_drained()
    crew_log.forget_slot_folds()
    return usage_mod.context_trace(SLOT, 14)


def _turn_read(ordinal: int) -> dict[str, Any]:
    """One kept turn's whole panel, as ``?turn=<ordinal>`` serves it."""
    assert_drained()
    crew_log.forget_slot_folds()
    read = usage_mod.context_turn_detail(SLOT, ordinal)
    assert read is not None
    return read["turn"]


def _usage() -> dict[str, Any]:
    assert_drained()
    crew_log.forget_slot_folds()
    return crew_log.read_slot_projection(SLOT, "usage").value


# --------------------------------------------------------------------------- #
# The newest turn's one breakdown
# --------------------------------------------------------------------------- #


def _latest() -> dict[str, Any]:
    latest = _trace()["latest"]
    assert latest is not None
    return latest


def test_a_measured_turn_is_crews_five_sums_plus_the_engines_own_parts():
    _open()
    _turn(1, 4_000, _real_profile(user=4_000))
    latest = _latest()
    engine = latest["engine"]
    assert engine["total_chars"] == 59_333
    assert set(engine["categories"]) == set(kas_profile.KNOWN_CATEGORIES)
    assert latest["ours"] == {"message": 40, "memory": 0, "rules": 3_960, "skills": 0, "other": 0}
    assert engine["crew_split"] == latest["ours"]
    assert engine["user_other_chars"] == 0
    # The agent loop's part is the engine's total less Crew's share, measured.
    assert latest["loop_chars"] == 59_333 - 4_000
    assert latest["loop_estimated"] is False
    assert latest["ttft_ms"]["total_ms"] == 2_870


def test_a_measured_turn_carries_the_engines_shares_counts_splits_window_and_media():
    _open()
    _turn(1, 4_000, _real_profile(user=4_000, media={"images": 1, "image_bytes": 34_000}))
    engine = _latest()["engine"]
    assert engine["percent"]["steering"] == 30.5
    assert round(sum(engine["percent"].values()), 1) == 100.0
    assert engine["counts"]["skillsIndex"] == 28
    assert engine["parts"]["toolIO"] == {"input": 20, "output": 82}
    assert engine["parts"]["workspace"]["environment"] == 939
    assert engine["window"] == {"size_tokens": 1_000_000, "usage_percent": 2.867}
    assert engine["media"] == {"images": 1, "image_bytes": 34_000}


def test_crews_sums_run_over_every_turn_still_in_context():
    _open()
    for turn, (chars, user) in enumerate([(4_033, 4_033), (4_109, 8_142), (4_031, 12_200)], 1):
        _turn(turn, chars, _real_profile(user=user))
    trace = _trace()
    assert [sum(r["ours"].values()) for r in trace["trend"]] == [4_033, 8_142, 12_173]
    assert trace["latest"]["engine"]["user_other_chars"] == 27


def test_compaction_restarts_the_sums_from_the_compacting_turn():
    _open()
    _turn(1, 4_000, _real_profile(user=4_000))
    _turn(2, 4_000, _real_profile(user=8_000))
    _turn(3, 3_000, _real_profile(user=3_000, compaction_summary=2_500))
    _turn(4, 2_000, _real_profile(user=5_000, compaction_summary=2_500))
    sums = [sum(r["ours"].values()) for r in _trace()["trend"]]
    assert sums == [4_000, 8_000, 3_000, 5_000]


def test_a_new_unit_restarts_the_sums_and_a_resume_does_not():
    _open()
    _turn(1, 4_000, _real_profile(user=4_000))
    _open(resumed=True)
    _turn(2, 4_000, _real_profile(user=8_000))
    _open("acp-measured-2")
    _turn(1, 1_000, _real_profile(user=1_000), unit="acp-measured-2")
    sums = [sum(r["ours"].values()) for r in _trace()["trend"]]
    assert sums == [4_000, 8_000, 1_000]


def test_an_in_place_compaction_keeps_only_what_was_composed_after_it():
    """kiro-cli compacts in place and measures nothing: without the reset the next
    prompt adds onto 40,000 summarized-away characters and the rest of the context
    reads as 0."""
    _open()
    _turn(1, 40_000, None, used=12_000)
    crew_log_emit.on_compaction_applied(UNIT, pct_before=85.0, pct_after=20.0)
    _turn(2, 1_000, None, used=1_500)
    latest = _latest()
    assert sum(latest["ours"].values()) == 1_000
    assert latest["loop_estimated"] is True
    assert latest["loop_chars"] == int(1_500 * measured_mod.CHARS_PER_TOKEN) - 1_000
    assert latest["loop_chars"] > 0


def test_a_deferred_compaction_verdict_keeps_the_prompt_composed_before_it_arrived():
    """The verdict settles at the next reading, while that turn is still open, so the
    prompt composed after the compaction and before the verdict stays resident."""
    _open()
    _turn(1, 40_000, None, used=12_000)
    crew_log_emit.on_context_composed(
        UNIT, 2, blocks={USER_LABEL: 40, "lessons": 960}, phase=PHASE_PER_TURN
    )
    crew_log_emit.on_compaction_applied(UNIT, pct_before=85.0, pct_after=21.0)
    crew_log_emit.on_turn_completed(
        UNIT,
        2,
        model="model-a",
        credits=0.1,
        context_used=1_500,
        context_window=1_000_000,
        stop_reason="end_turn",
    )
    latest = _latest()
    assert sum(latest["ours"].values()) == 1_000
    assert latest["loop_chars"] == int(1_500 * measured_mod.CHARS_PER_TOKEN) - 1_000


def test_crews_share_is_clamped_to_what_the_engine_holds():
    _open()
    _turn(1, 4_000, _real_profile(user=4_000))
    _turn(2, 4_000, _real_profile(user=5_000))
    latest = _latest()
    assert sum(latest["ours"].values()) == 5_000
    assert latest["engine"]["mismatch_chars"] == 3_000


def test_an_unmeasured_turn_estimates_the_agent_loop_from_tokens_at_the_composers_ratio():
    from kiro_crew.crew_log.emit_turn import _EST_CHARS_PER_TOKEN

    _open()
    _turn(1, 4_000, None, used=20_000)
    trace = _trace()
    latest = trace["latest"]
    assert latest["engine"] is None
    assert latest["loop_estimated"] is True
    assert trace["chars_per_token"] == _EST_CHARS_PER_TOKEN
    assert latest["loop_chars"] == int(20_000 * _EST_CHARS_PER_TOKEN) - 4_000
    # The trend chart shows no estimate: an unmeasured turn's row is Crew's alone.
    assert trace["trend"][-1]["loop"] == 0


def test_an_older_measurement_is_not_shown_for_a_newer_unmeasured_turn():
    _open()
    _turn(1, 4_000, _real_profile(user=4_000))
    _turn(2, 1_000, None, used=10_000)
    latest = _latest()
    assert latest["engine"] is None
    assert latest["loop_estimated"] is True


# --------------------------------------------------------------------------- #
# The trend ring and the start row
# --------------------------------------------------------------------------- #


def test_a_past_turn_keeps_its_own_blocks_and_its_own_engine_breakdown():
    """Every kept turn opens whole: its Kiro Crew blocks by name, the engine's parts
    when it measured that turn, and its time to first output; a turn the engine did
    not measure keeps its own reading for the estimated view."""
    _open()
    crew_log_emit.on_context_composed(
        UNIT, 1, blocks={USER_LABEL: 40, "lessons": 3_000, "skill_index": 960}, phase=PHASE_PER_TURN
    )
    crew_log_emit.on_context_measured(
        UNIT, 1, profile={**_real_profile(user=4_000), "turn_id": "t-a"}
    )
    crew_log_emit.on_turn_completed(
        UNIT,
        1,
        model="model-a",
        credits=0.1,
        context_used=30_000,
        context_window=1_000_000,
        stop_reason="end_turn",
    )
    _turn(2, 500, None, used=31_000)
    _turn(3, 500, None, used=32_000)
    first = _trace()["trend"][0]
    assert first["turn"] == 1
    detail = _turn_read(1)
    assert detail["blocks"] == {USER_LABEL: 40, "lessons": 3_000, "skill_index": 960}
    assert detail["engine"]["total_chars"] == 59_333
    assert set(detail["engine"]["categories"]) == set(kas_profile.KNOWN_CATEGORIES)
    assert detail["engine"]["parts"]["toolIO"] == {"input": 20, "output": 82}
    assert detail["engine"]["counts"]["skillsIndex"] == 28
    assert detail["ttft_ms"]["total_ms"] == 2_870
    assert detail["loop_estimated"] is False
    # The unmeasured turn 2 shows its own reading, estimated, and its own blocks.
    second = _turn_read(2)
    assert second["engine"] is None
    assert second["context_used"] == 31_000
    assert second["loop_estimated"] is True
    assert second["blocks"] == {USER_LABEL: 40, "lessons": 460}
    # The page and the pushed value carry only the chart's numbers per row.
    assert set(_trace()["trend"][0]) == {"turn", "ours", "loop", "used", "version"}
    # Each row's own reading, the harness total its column is drawn up to.
    assert [row["used"] for row in _trace()["trend"]] == [30_000, 31_000, 32_000]


def test_the_session_tree_draws_what_each_turn_sent_not_the_running_sum():
    """The Session Breakdown tree draws one bar per ``turns`` entry under a per-turn
    caption: three turns injecting 1,000 each are 1,000 each, while the panel's
    resident view still sums them."""
    _open()
    for turn in (1, 2, 3):
        _turn(turn, 1_000, None, used=5_000 + turn)
    trace = _trace()
    assert [t["total_chars"] for t in trace["turns"]] == [1_000, 1_000, 1_000]
    assert trace["turns"][0]["blocks"] == {USER_LABEL: 40, "lessons": 960}
    assert [sum(r["ours"].values()) for r in trace["trend"]] == [1_000, 2_000, 3_000]


def test_a_row_keeps_at_most_the_composers_blocks_and_counts_the_rest():
    _open()
    blocks = {f"block_{i:03d}": 10 for i in range(measured_mod.BLOCKS_PER_TURN + 6)}
    crew_log_emit.on_context_composed(UNIT, 1, blocks=blocks, phase=PHASE_PER_TURN)
    crew_log_emit.on_turn_completed(UNIT, 1, model="model-a", stop_reason="end_turn")
    detail = _turn_read(1)
    assert len(detail["blocks"]) == measured_mod.BLOCKS_PER_TURN
    assert detail["blocks_omitted"] == 6
    # Every character still counts in the five sums.
    assert sum(detail["ours"].values()) == 10 * (measured_mod.BLOCKS_PER_TURN + 6)


def test_the_ring_keeps_the_newest_thirty_turns_and_counts_the_rest_exactly():
    _open()
    for turn in range(1, 36):
        _turn(turn, 1_000, None, used=1_000 + turn)
    trace = _trace()
    assert len(trace["trend"]) == measured_mod.TREND_TURNS
    assert [r["turn"] for r in trace["trend"]] == list(range(6, 36))
    assert trace["earlier"] == 5
    assert trace["turn_count"] == 35


def test_the_newest_session_start_turn_is_one_row_of_its_own():
    _open()
    _turn(1, 30_000, None, used=40_000, phase=PHASE_SESSION_START)
    _turn(2, 500, None, used=41_000)
    _turn(3, 500, None, used=42_000)
    trace = _trace()
    assert trace["start"]["turn"] == 1
    assert sum(trace["start"]["ours"].values()) == 30_000
    assert [r["turn"] for r in trace["trend"]] == [2, 3]
    assert trace["earlier"] == 1


def test_every_row_keeps_its_own_ordinal_across_several_session_starts():
    for index, unit in enumerate(("acp-a", "acp-b", "acp-c")):
        _open(unit)
        _turn(1, 30_000 + index, None, unit=unit, used=40_000, phase=PHASE_SESSION_START)
        _turn(2, 500 + index, None, unit=unit, used=41_000)
    trace = _trace()
    # Starts at ordinals 1, 3 and 5; only the newest is listed above the chart.
    assert trace["start"]["turn"] == 5
    assert sum(trace["start"]["ours"].values()) == 30_002
    assert [r["turn"] for r in trace["trend"]] == [2, 4, 6]
    # Each session's sums restart and run over its own start turn.
    assert [sum(r["ours"].values()) for r in trace["trend"]] == [30_500, 30_502, 30_504]
    assert trace["earlier"] == 1


def test_a_new_session_shows_no_breakdown_from_the_previous_session():
    """A measured turn, then a new (not resumed) session opens: read before its first
    composition, the panel shows no engine breakdown or time to first output left over
    from the previous engine session."""
    _open()
    _turn(1, 4_000, _real_profile(user=4_000))
    assert _latest()["engine"] is not None
    _open("acp-measured-2")
    latest = _latest()
    assert latest["engine"] is None
    assert latest["ttft_ms"] is None


def test_a_new_session_does_not_show_the_previous_sessions_occupancy():
    _open()
    _turn(1, 4_000, None, used=150_000, window=200_000)
    _open("acp-measured-2")
    # A cancelled first turn reports no occupancy, so the turn carries no reading.
    _turn(1, 1_000, None, unit="acp-measured-2", used=0, window=0)
    latest = _latest()
    assert latest["context_used"] == 0
    assert latest["context_window"] == 0
    assert latest["loop_chars"] == 0
    assert latest["loop_estimated"] is True


def test_a_regenerated_session_start_that_composes_per_turn_moves_out_of_the_start_row():
    """Regenerate cuts history back, so turn 1 comes again composed per turn. Its row
    moves into the ring; the start row it had is cleared, so 'Turn 1' shows once."""
    _open()
    _turn(1, 30_000, None, used=40_000, phase=PHASE_SESSION_START)
    _turn(1, 900, None, used=41_000)
    trace = _trace()
    assert trace["start"] is None
    assert [r["turn"] for r in trace["trend"]] == [1]
    assert trace["turn_count"] == 1


def test_a_resent_ring_turn_that_composes_as_a_session_start_leaves_the_ring():
    """The reverse: turn 2 is in the ring, then sent again composed as a session start.
    The conversation was cut back to turn 2, so turn 3 goes too; turn 2 becomes the
    start row and no two rows share ordinal 2."""
    _open()
    _turn(1, 1_000, None, used=5_000)
    _turn(2, 2_000, None, used=6_000)
    _turn(3, 3_000, None, used=7_000)
    _turn(2, 20_000, None, used=9_000, phase=PHASE_SESSION_START)
    trace = _trace()
    assert trace["start"]["turn"] == 2
    assert [r["turn"] for r in trace["trend"]] == [1]
    assert trace["turn_count"] == 2
    ordinals = [r["turn"] for r in trace["trend"]] + [trace["start"]["turn"]]
    assert len(ordinals) == len(set(ordinals))


def test_a_rewound_turn_reuses_its_ordinal_not_a_new_one():
    """An edit-resend truncates the conversation, so a turn already counted recurs at a
    lower turn number in the same unit. It keeps its ORIGINAL ordinal, and the turns
    after it go: turns 1, 2, 3 close, turn 2 is sent again, and the ring holds 1, 2,
    with turn 2's row now the resend's."""
    _open()
    _turn(1, 1_000, None, used=5_000)
    _turn(2, 2_000, None, used=6_000)
    _turn(3, 3_000, None, used=7_000)
    _turn(2, 700, None, used=6_500)
    trace = _trace()
    assert [r["turn"] for r in trace["trend"]] == [1, 2]
    assert trace["turn_count"] == 2
    assert trace["latest"]["turn"] == 2
    # Turn 2's row is the resend's: the running sums at that point (1,000 + 2,000 +
    # 3,000 + 700), not the first attempt's 3,000.
    by_turn = {r["turn"]: sum(r["ours"].values()) for r in trace["trend"]}
    assert by_turn[2] == 6_700


def _ring_after(numbers: list[int]) -> tuple[list[int], int]:
    _open()
    for number in numbers:
        _turn(number, 1_000, None, used=5_000 + number)
    trace = _trace()
    return [r["turn"] for r in trace["trend"]], trace["turn_count"]


def test_a_turn_after_an_edit_resend_continues_from_the_resent_turn():
    """1, 2, 3, then turn 2 resent, then one more message: that message is turn 3
    again (the row count after the cut), and it is the third turn, not a fourth."""
    assert _ring_after([1, 2, 3, 2, 3]) == ([1, 2, 3], 3)


def test_rewinding_to_the_first_turn_drops_every_later_one():
    assert _ring_after([1, 2, 3, 1]) == ([1], 1)


def _forty_turns_then(step: int, resend: int) -> dict[str, Any]:
    """Forty turns numbered as the dashboard numbers them (the durable row count,
    so ``step`` apart), the first a session start, then turn *resend* sent again."""
    _open()
    for k in range(1, 41):
        phase = PHASE_SESSION_START if k == 1 else PHASE_PER_TURN
        _turn(step * (k - 1) + 1, 1_000, None, used=5_000 + k, phase=phase)
    _turn(step * (resend - 1) + 1, 1_000, None, used=9_000)
    return _trace()


@pytest.mark.parametrize("step", [2, 3])
def test_an_edit_resend_inside_the_ring_keeps_its_ordinal_with_real_turn_numbers(step):
    trace = _forty_turns_then(step, 39)
    assert [r["turn"] for r in trace["trend"]] == list(range(11, 40))
    assert trace["turn_count"] == 39
    assert trace["start"]["turn"] == 1
    assert trace["earlier"] == 10


@pytest.mark.parametrize("step", [2, 3])
def test_an_edit_resend_older_than_the_ring_keeps_its_ordinal_and_the_start(step):
    """Turn 5's row left the ring, but the ordinal memo still holds its number, so it
    keeps ordinal 5: every ring row after it goes, the start row (turn 1, before it)
    stays, and the count is the real one."""
    trace = _forty_turns_then(step, 5)
    assert [r["turn"] for r in trace["trend"]] == [5]
    assert trace["start"]["turn"] == 1
    assert trace["turn_count"] == 5
    assert trace["earlier"] == 4


def test_the_count_grows_again_past_a_rewind():
    assert _ring_after([1, 2, 3, 2, 3, 4]) == ([1, 2, 3, 4], 4)


def test_a_new_attempt_drops_the_previous_attempts_measurement():
    """Regenerate composes the same turn again; until that attempt reports its own
    profile, the old breakdown, ttft and engine turn id describe a request that is
    gone."""
    _open()
    _turn(1, 4_000, _real_profile(user=4_000))
    assert _latest()["engine"] is not None
    crew_log_emit.on_context_composed(
        UNIT, 1, blocks={USER_LABEL: 40, "lessons": 3_960}, phase=PHASE_PER_TURN
    )
    latest = _latest()
    assert latest["engine"] is None
    assert latest["ttft_ms"] is None
    assert latest["turn"] == 1
    # The new attempt's own profile is taken, not refused as a duplicate.
    crew_log_emit.on_context_measured(UNIT, 1, profile=_real_profile(user=8_000))
    assert _latest()["engine"]["history"]["user"] == 8_000


def test_a_regenerated_turn_with_the_same_counts_has_a_new_row_version():
    """A turn sent again can keep its own numbers and change only its measurement
    (here its time to first output). Its row is written again, so its version moves and the
    panel's cache key for that turn's read moves with it; the read is the new one."""
    _open()
    fast = _real_profile()
    fast["ttft"] = {**fast["ttft"], "total_ms": 200}
    slow = _real_profile()
    slow["ttft"] = {**slow["ttft"], "total_ms": 900}
    _turn(1, 1_000, None, used=5_000)
    _turn(2, 2_000, fast, used=6_000)
    before = {r["turn"]: r for r in _trace()["trend"]}
    assert _turn_read(2)["ttft_ms"]["total_ms"] == 200
    _turn(2, 2_000, slow, used=6_000)
    after = {r["turn"]: r for r in _trace()["trend"]}
    # The turn's own reading is the same, the version is not.
    assert after[2]["used"] == before[2]["used"]
    assert after[2]["version"] != before[2]["version"]
    assert after[1]["version"] == before[1]["version"]
    assert _turn_read(2)["ttft_ms"]["total_ms"] == 900


def test_a_retried_turn_replaces_its_row_rather_than_adding_one():
    _open()
    _turn(1, 1_000, None, used=5_000)
    _turn(1, 1_200, None, used=6_000)
    trace = _trace()
    assert len(trace["trend"]) == 1
    assert trace["turn_count"] == 1


# --------------------------------------------------------------------------- #
# Bounded, O(1) state
# --------------------------------------------------------------------------- #


def _entry(seq: int, entry_type: str, data: dict[str, Any]) -> Entry:
    return Entry(type=entry_type, seq=seq, time=1_700_000_000_000 + seq, src="gateway", data=data)


def _fold_turns(turns: int) -> dict[str, Any]:
    """The usage state after *turns* measured turns, folded through the real step."""
    from kiro_crew.crew_log.projection_usage import _usage_start, _usage_step

    profile = _real_profile(user=4_000)
    state = _usage_start()
    seq = 0
    _usage_step(state, _entry(seq, "session/opened", {}))
    for turn in range(1, turns + 1):
        sources = [{"kind": USER_LABEL, "chars": 40}, {"kind": "lessons", "chars": 3_960}]
        for entry_type, data in (
            ("context/composed", {"turn": turn, "chars": 4_000, "sources": sources}),
            (
                "context/measured",
                # Fixed-width ids, so a string's length does not change with the count.
                {
                    "turn": turn,
                    "turn_id": f"t{turn:07d}",
                    "breakdown": profile["breakdown"],
                    "ttft": profile["ttft"],
                },
            ),
            (
                "turn/completed",
                {
                    "turn": turn,
                    "model": "model-a",
                    "stop_reason": "end_turn",
                    "context": {"used": 50_000, "window": 1_000_000},
                },
            ),
        ):
            seq += 1
            _usage_step(state, _entry(seq, entry_type, data))
    return state


def _shape(obj: Any) -> int:
    """How many values *obj* holds, every container and every leaf counted once."""
    if isinstance(obj, dict):
        return 1 + sum(_shape(k) + _shape(v) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return 1 + sum(_shape(v) for v in obj)
    return 1


def _string_chars(obj: Any) -> int:
    if isinstance(obj, str):
        return len(obj)
    if isinstance(obj, dict):
        return sum(_string_chars(k) + _string_chars(v) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return sum(_string_chars(v) for v in obj)
    return 0


def test_the_usage_state_is_the_same_size_after_210_and_after_1000_turns():
    """Once the ring and the peak readings are full the state stops growing: each row
    holds one turn whole, so a longer session replaces rows rather than adding any."""
    small, large = _fold_turns(measured_mod.PEAK_READINGS + 10), _fold_turns(1_000)
    assert _shape(small) == _shape(large)
    # Only the ordinal memo's one string grows, and it is bounded on its own.
    small["ctx"].pop("turn_memo"), large["ctx"].pop("turn_memo")
    assert _string_chars(small) == _string_chars(large)
    assert large["ctx"]["turns"] == 1_000
    assert large["ctx"]["trend_rows"] == measured_mod.TREND_TURNS


def test_an_entry_the_context_part_does_not_read_returns_the_same_state():
    ctx = measured_mod.context_start()
    for entry_type in (
        "step/completed",
        "tool/started",
    ):
        assert measured_mod.context_apply(ctx, _entry(1, entry_type, {"turn": 1})) is ctx
    # A resume is the same unit: nothing to change either.
    assert measured_mod.context_apply(ctx, _entry(2, "session/opened", {"resumed": True})) is ctx
    # A configuration with no window states nothing to keep.
    zero = _entry(3, "request/configured", {"turn": 1, "context_window": 0})
    assert measured_mod.context_apply(ctx, zero) is ctx


def test_the_configured_window_is_the_newest_stated_in_this_unit():
    ctx = measured_mod.context_start()
    for seq, window in enumerate((200_000, 0, 1_000_000, 0), start=1):
        ctx = measured_mod.context_apply(
            ctx, _entry(seq, "request/configured", {"turn": 1, "context_window": window})
        )
    assert ctx["conf_window"] == 1_000_000
    ctx = measured_mod.context_apply(ctx, _entry(9, "session/opened", {"resumed": True}))
    assert ctx["conf_window"] == 0


def test_apply_never_edits_the_state_it_was_given():
    state = _fold_turns(3)
    before = copy.deepcopy(state["ctx"])
    held = state["ctx"]
    from kiro_crew.crew_log.projection_usage import _usage_step

    _usage_step(state, _entry(99, "context/composed", {"turn": 4, "chars": 10, "sources": []}))
    assert held == before
    assert state["ctx"] is not held


def _deep_bytes(obj: Any, seen: set[int] | None = None) -> int:
    """Bytes *obj* holds, counting each shared object once (as ``test_context_trace``)."""
    import sys

    seen = set() if seen is None else seen
    if id(obj) in seen:
        return 0
    seen.add(id(obj))
    size = sys.getsizeof(obj)
    if isinstance(obj, dict):
        size += sum(_deep_bytes(k, seen) + _deep_bytes(v, seen) for k, v in obj.items())
    elif isinstance(obj, (list, tuple)):
        size += sum(_deep_bytes(v, seen) for v in obj)
    return size


#: The slot-fold cache's ceiling for one member, stated in ``projection.py``.
CACHE_MEMBER_CEILING = 995_342


def worst_case_context_bytes() -> int:
    """Bytes the context state holds at its largest (:func:`worst_case_context`)."""
    return _deep_bytes(worst_case_context())


def worst_case_context() -> dict[str, Any]:
    """The context state at its largest: every number distinct at the cap, every string
    at the longest a kept name can be, and every one of the 31 rows full: its
    :data:`BLOCKS_PER_TURN` blocks by distinct names and a whole engine measurement.
    Built by hand because the shape is fixed: there is nothing a longer session could
    add."""
    from kiro_crew.crew_log.projection_values import TEXT_LIMIT
    from kiro_crew.engine_context import MAX_COUNT

    n = iter(range(1_000_000))

    def big() -> int:
        return MAX_COUNT - next(n)

    def name() -> str:
        # The longest name a row keeps: one under ``TEXT_LIMIT`` (see ``_keyable``).
        return f"{next(n):08d}".ljust(TEXT_LIMIT - 1, "b")

    def blocks() -> list[list[Any]]:
        return [[name(), big()] for _ in range(measured_mod.BLOCKS_PER_TURN)]

    def row() -> list[Any]:
        return [
            *(big() for _ in range(measured_mod.ROW_FIELDS)),
            blocks(),
            [big() for _ in range(measured_mod.MEASURED_FIELDS)],
        ]

    ctx = measured_mod.context_start()
    ctx.update(
        {
            key: big()
            for key, value in ctx.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
    )
    ctx["ours"] = [big() for _ in range(5)]
    ctx["ours_read"] = [big() for _ in range(5)]
    ctx["turn_ours"] = [big() for _ in range(5)]
    ctx["turn_blocks"] = blocks()
    # The ordinal memo full: CONTEXT_ORDINAL_MEMO_LIMIT turn numbers at the cap's width.
    ctx["turn_memo"] = "," + "".join(
        f"{MAX_COUNT - i}," for i in range(measured_mod.CONTEXT_ORDINAL_MEMO_LIMIT)
    )
    ctx["turn_phase"] = "p" * TEXT_LIMIT
    ctx["engine_id"] = "i" * TEXT_LIMIT
    ctx["engine"] = [big() for _ in range(measured_mod.MEASURED_FIELDS)]
    # Measured, so the newest turn's whole engine view is in the pushed value.
    flags = measured_mod._INDEX["flags"]
    ctx["engine"][flags] = int(ctx["engine"][flags]) | measured_mod.FLAG_BREAKDOWN
    ctx["trend"] = [row() for _ in range(measured_mod.TREND_TURNS)]
    ctx["start"] = row()
    ctx["peaks"] = [[big(), big(), big()] for _ in range(measured_mod.PEAK_READINGS)]
    ctx["rerun"] = True
    # The newest turn measured, so the pushed value carries its whole engine view.
    ctx["engine_unit"], ctx["engine_no"] = ctx["turn_unit"], ctx["turn_no"]
    ctx["closed_unit"] = ctx["closed_no"] = 0
    return ctx


#: The context state's bound in bytes: 31 full rows, the newest turn's own blocks, a
#: full ordinal memo and a full set of peak readings. Measured (929,529), then rounded
#: up; the cache member ceiling above is the hard limit.
CONTEXT_STATE_BOUND = 950_000


def test_the_worst_case_context_state_is_bounded_under_the_cache_ceiling():
    measured = worst_case_context_bytes()
    assert measured < CONTEXT_STATE_BOUND, f"the context state measures {measured:,} bytes"
    assert measured < CACHE_MEMBER_CEILING


#: The pushed context value's bound in JSON bytes: the newest turn whole (64 blocks at
#: the longest name, a full engine view), plus the chart's numbers for 30 ring rows and
#: the start row. Measured (20,824), then rounded up. A real session pushes about 6 KB.
CONTEXT_PUSH_BOUND = 22_000


def test_the_worst_case_pushed_value_is_a_few_kb():
    """Every row in the state is whole, but the pushed value carries only the newest
    turn whole: a past turn is read on demand, so a push stays small."""
    import json

    view = measured_mod.context_render(worst_case_context())
    pushed = len(json.dumps(view, separators=(",", ":")))
    assert pushed < CONTEXT_PUSH_BOUND, f"the pushed context value is {pushed:,} bytes"
    assert view["latest"]["engine"] is not None
    assert len(view["latest"]["blocks"]) == measured_mod.BLOCKS_PER_TURN


def test_the_pushed_value_carries_no_per_row_detail():
    """MUTATION-SENSITIVE: a ring row is the chart's numbers only; the start row adds
    the size its panel heads with; the newest turn alone is whole."""
    _open()
    _turn(1, 6_000, _real_profile(), phase=PHASE_SESSION_START)
    for turn in (2, 3, 4):
        _turn(turn, 500, _real_profile())
    context = _usage()["context"]
    assert [set(row) for row in context["trend"]] == [
        {"turn", "ours", "loop", "used", "version"}
    ] * 3
    assert set(context["start"]) == {"turn", "ours", "loop", "used", "version", "figure"}
    assert context["start"]["figure"] == {"n": 59_333, "unit": "chars"}
    assert context["latest"]["engine"]["total_chars"] == 59_333
    assert context["latest"]["blocks"] == {USER_LABEL: 40, "lessons": 460}


def test_a_kept_turn_reads_whole_and_one_that_left_the_ring_reads_as_none():
    _open()
    for turn in range(1, 36):
        _turn(turn, 1_000 + turn, None, used=2_000 + turn)
    assert [row["turn"] for row in _trace()["trend"]][0] == 6
    # Turn 20 is kept: its own blocks and its own reading.
    detail = _turn_read(20)
    assert detail["turn"] == 20
    assert detail["blocks"] == {USER_LABEL: 40, "lessons": 980}
    assert detail["context_used"] == 2_020 and detail["loop_estimated"] is True
    # Turn 5 left the ring; turn 99 never was.
    assert usage_mod.context_turn_detail(SLOT, 5) is None
    assert usage_mod.context_turn_detail(SLOT, 99) is None
    assert usage_mod.context_turn_detail(SLOT, 0) is None


def test_a_measurement_with_no_open_turn_attaches_nowhere():
    """Cron, sub-agent and task turns open no crew-log turn (no ``context/composed``,
    no ``turn/started``), so a ``context/measured`` written for one has nothing to
    attach to: the fold returns the same state, and a parent's own breakdown is never
    overwritten by a child's numbers."""
    breakdown = _real_profile()["breakdown"]
    ctx = measured_mod.context_start()
    ctx = measured_mod.context_apply(ctx, _entry(1, "session/opened", {}))
    # No composed turn yet: refused.
    assert (
        measured_mod.context_apply(
            ctx, _entry(2, "context/measured", {"turn": 1, "breakdown": breakdown})
        )
        is ctx
    )
    # A parent turn that already closed: refused as well, so its row stays its own.
    ctx = measured_mod.context_apply(ctx, _entry(3, "context/composed", {"turn": 1, "sources": []}))
    ctx = measured_mod.context_apply(
        ctx, _entry(4, "turn/completed", {"turn": 1, "stop_reason": "end_turn"})
    )
    assert (
        measured_mod.context_apply(
            ctx, _entry(5, "context/measured", {"turn": 1, "breakdown": breakdown})
        )
        is ctx
    )


# --------------------------------------------------------------------------- #
# Rewinds, any sequence
# --------------------------------------------------------------------------- #

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402


@settings(max_examples=200, deadline=None)
@given(
    st.sampled_from([1, 2, 3]),
    st.lists(
        st.one_of(st.just(0), st.integers(min_value=1, max_value=60)),
        min_size=1,
        max_size=80,
    ),
)
def test_any_rewind_sequence_keeps_ordinals_unique_and_equal_to_one_through_turns(step, steps):
    """Each step either sends a new message (0) or cuts the conversation back to turn k
    and sends it again. Turn numbers are the durable row count, ``step`` apart, as the
    dashboard writes them. A rewind to a turn still in the ring leaves the newest turns
    1..turns, each once; one to an older turn is unknown and takes a new ordinal. In
    every case ordinals are unique, rise left to right, and end at ``turns``."""
    ctx = measured_mod.context_start()
    seq = 0

    def fold(entry_type: str, data: dict[str, Any]) -> None:
        nonlocal ctx, seq
        seq += 1
        ctx = measured_mod.context_apply(ctx, _entry(seq, entry_type, data))

    fold("session/opened", {})
    count = 0  # turns in the conversation, as the user sees them
    expected = 0  # the fold's ordinal count, which an unknown rewind moves on
    dense = True  # every rewind here is within the memo's range
    for choice in steps:
        known = 0 < choice <= count
        if choice == 0 or choice > count:
            count += 1
            expected += 1
        elif known:
            expected -= count - choice
            count = choice
        else:
            count = choice
            expected += 1
            dense = False
        number = step * (count - 1) + 1
        sources = [{"kind": USER_LABEL, "chars": 40}]
        fold("context/composed", {"turn": number, "chars": 40, "sources": sources})
        fold("turn/completed", {"turn": number, "stop_reason": "end_turn"})
        if not dense:
            # Past an unknown rewind the old rows' numbers can match again; the model
            # stops predicting the count there and checks only uniqueness below.
            break
    view = measured_mod.context_render(ctx)
    ordinals = [row["turn"] for row in view["trend"]]
    assert len(ordinals) == len(set(ordinals))
    assert ordinals == sorted(ordinals)
    assert ordinals and ordinals[-1] == view["turns"]
    assert view["earlier"] == ordinals[0] - 1
    if dense:
        assert view["turns"] == count == expected
        assert ordinals == list(range(count - len(ordinals) + 1, count + 1))
        assert len(ordinals) == min(count, measured_mod.TREND_TURNS)
