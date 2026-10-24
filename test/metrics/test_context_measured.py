"""The engine-measured half of the Context breakdown: ``context/measured`` end to end.

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
        phase=PHASE_PER_TURN,
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


def _usage() -> dict[str, Any]:
    assert_drained()
    crew_log.forget_slot_folds()
    return crew_log.read_slot_projection(SLOT, "usage").value


# --------------------------------------------------------------------------- #
# The page reads what the engine measured, next to Crew's own blocks
# --------------------------------------------------------------------------- #


def test_a_measured_turn_shows_the_engine_categories_beside_crews_blocks():
    _open()
    _turn(1, 4_000, _real_profile(user=4_000))
    row = _trace()["turns"][0]
    # Crew's own composition is unchanged: its blocks, its total.
    assert row["blocks"] == {USER_LABEL: 40, "lessons": 3_960}
    assert row["total_chars"] == 4_000
    engine = row["engine"]
    assert engine["total_chars"] == 59_333
    assert engine["model_calls"] == 1
    assert set(engine["categories"]) == set(kas_profile.KNOWN_CATEGORIES)
    assert engine["categories"]["steering"] == 18_077
    # Crew's share of the engine's user text is Crew's own count of what it sent.
    assert engine["crew_chars"] == 4_000
    assert engine["user_other_chars"] == 0
    assert engine["mismatch_chars"] == 0
    assert row["ttft_ms"] == {
        "total_ms": 2_870,
        "setup_ms": 59,
        "model_ms": 1_534,
    }


def test_crews_share_accumulates_over_every_turn_still_in_context():
    """The engine's ``history.user`` grows by each turn's whole prompt, so Crew's share
    is the running sum of what it composed -- the captured session's 4033, 8142, 12173."""
    _open()
    for turn, (chars, user) in enumerate([(4_033, 4_033), (4_109, 8_142), (4_031, 12_200)], 1):
        _turn(turn, chars, _real_profile(user=user))
    engines = [t["engine"] for t in _trace()["turns"]]
    assert [e["crew_chars"] for e in engines] == [4_033, 8_142, 12_173]
    # 27 user characters Crew did not compose (a steering message, an engine wake-up).
    assert [e["user_other_chars"] for e in engines] == [0, 0, 27]


def test_existing_credits_and_occupancy_are_untouched_by_a_measurement():
    """``turn/completed`` billing and occupancy are folded as before; the engine's
    characters are never divided by the provider's token window."""
    _open()
    _turn(1, 4_000, _real_profile(user=4_000, total_chars=900_000), used=123_456)
    usage = _usage()
    assert usage["credits"] == pytest.approx(0.1)
    assert usage["turns"]["completed"] == 1
    trace = _trace()
    assert trace["peak_context_used"] == 123_456
    assert trace["context_window"] == 1_000_000
    row = trace["turns"][0]
    assert row["context_used"] == 123_456
    assert not any("pct" in key or "ratio" in key for key in row["engine"])


# --------------------------------------------------------------------------- #
# Edge cases
# --------------------------------------------------------------------------- #


def test_compaction_restarts_the_running_sum():
    """A changed ``compaction_summary`` says the engine's context now starts from a
    summary, so only this turn's prompt is still user text in it."""
    _open()
    _turn(1, 4_000, _real_profile(user=4_000))
    _turn(2, 4_000, _real_profile(user=8_000))
    _turn(3, 3_000, _real_profile(user=3_000, compaction_summary=2_500))
    _turn(4, 2_000, _real_profile(user=5_000, compaction_summary=2_500))
    engines = [t["engine"] for t in _trace()["turns"]]
    assert [e["crew_chars"] for e in engines] == [4_000, 8_000, 3_000, 5_000]
    assert [e["mismatch_chars"] for e in engines] == [0, 0, 0, 0]
    measured = [t["measured"] for t in _usage()["context"]["turns"]]
    assert [m["reset"] for m in measured] == [False, False, True, False]


def test_a_first_reading_that_already_carries_a_summary_restarts_the_sum():
    """The engine compacted before this fold saw any measurement (an older session
    resumed under this build): its context starts from that summary, not from the
    unit's first prompt."""
    _open()
    _turn(1, 4_000, None)
    _turn(2, 3_000, _real_profile(user=3_000, compaction_summary=900))
    engine = _trace()["turns"][1]["engine"]
    assert engine["crew_chars"] == 3_000
    assert engine["mismatch_chars"] == 0


def test_a_rerun_turn_marks_the_split_approximate_until_the_sum_restarts():
    """A regenerate composes a closed turn again. Crew did send it twice, so the sum
    counts both, and the rows say the split may count an attempt the engine dropped."""
    _open()
    _turn(1, 4_000, _real_profile(user=4_000))
    _turn(2, 1_000, _real_profile(user=5_000))
    _turn(2, 1_000, _real_profile(user=5_000))
    _turn(3, 1_000, _real_profile(user=6_000))
    _turn(4, 1_000, _real_profile(user=1_000, compaction_summary=700))
    engines = [t["engine"] for t in _trace()["turns"]]
    assert [e["approximate"] for e in engines] == [False, False, True, True, False]
    assert [e["crew_chars"] for e in engines] == [4_000, 5_000, 6_000, 7_000, 1_000]
    assert engines[3]["mismatch_chars"] == 1_000


def test_a_compaction_during_a_rerun_restarts_from_that_attempt_alone():
    """Regenerating the newest turn, and the engine compacting during the new attempt:
    the engine's user text then starts at that attempt's prompt, so the sum must not
    carry the first attempt too."""
    _open()
    _turn(1, 4_000, _real_profile(user=4_000))
    _turn(2, 1_000, _real_profile(user=5_000))
    _turn(2, 1_200, _real_profile(user=1_200, compacted=True))
    engine = _trace()["turns"][-1]["engine"]
    assert engine["crew_chars"] == 1_200
    assert engine["mismatch_chars"] == 0


def test_a_failed_turn_retried_under_a_new_number_marks_the_split_approximate():
    """A throttled turn is rewound out of the engine and retried as the next turn
    number. Its composition stays in the sum, unconfirmed, so the rows say the split
    is approximate until the engine's context restarts."""
    _open()
    _turn(1, 1_000, _real_profile(user=1_000))
    crew_log_emit.on_context_composed(UNIT, 2, blocks={USER_LABEL: 1_000})
    crew_log_emit.on_turn_failed(UNIT, 2, error="Throttled")
    _turn(3, 1_000, _real_profile(user=2_000))
    _turn(4, 500, _real_profile(user=500, compaction_summary=300))
    engines = [t["engine"] for t in _trace()["turns"] if t["engine"]]
    assert [e["approximate"] for e in engines] == [False, True, False]
    assert engines[1]["crew_chars"] == 3_000
    assert engines[1]["mismatch_chars"] == 1_000


def test_a_rewind_to_an_earlier_turn_number_is_a_rerun_too():
    _open()
    _turn(1, 4_000, _real_profile(user=4_000))
    _turn(2, 1_000, _real_profile(user=5_000))
    _turn(1, 4_000, _real_profile(user=4_000))
    assert [t["engine"]["approximate"] for t in _trace()["turns"]] == [False, False, True]


def test_several_compositions_of_one_open_turn_are_not_a_rerun():
    _open()
    crew_log_emit.on_context_composed(
        UNIT, 1, blocks={USER_LABEL: 3_000}, phase=PHASE_SESSION_START
    )
    crew_log_emit.on_context_composed(UNIT, 1, blocks={USER_LABEL: 1_000}, phase=PHASE_PER_TURN)
    crew_log_emit.on_context_measured(UNIT, 1, profile=_real_profile(user=4_000))
    crew_log_emit.on_turn_completed(UNIT, 1, model="model-a")
    engine = [t["engine"] for t in _trace()["turns"] if t["engine"]][0]
    assert engine["crew_chars"] == 4_000
    assert engine["approximate"] is False


def test_a_mid_turn_compaction_restarts_it_too():
    _open()
    _turn(1, 4_000, _real_profile(user=4_000))
    _turn(2, 3_000, _real_profile(user=3_000, compacted=True))
    engine = _trace()["turns"][1]["engine"]
    assert engine["compacted"] is True
    assert engine["crew_chars"] == 3_000


def test_a_new_unit_is_a_new_engine_context_and_a_resume_is_not():
    _open()
    _turn(1, 4_000, _real_profile(user=4_000))
    # A re-attachment picks the same engine session up again: the sum carries on.
    _open(resumed=True)
    _turn(2, 4_000, _real_profile(user=8_000))
    # A new unit is a new engine session: it starts from nothing.
    _open("acp-measured-2")
    _turn(1, 1_000, _real_profile(user=1_000), unit="acp-measured-2")
    engines = [t["engine"] for t in _trace()["turns"]]
    assert [e["crew_chars"] for e in engines] == [4_000, 8_000, 1_000]


def test_a_duplicate_measurement_of_the_same_engine_turn_changes_nothing():
    """A footer seen twice (the same ``turn_id``) is folded once."""
    _open()
    profile = _real_profile(user=4_000, categories={"aNewCategory": 50})
    crew_log_emit.on_context_composed(UNIT, 1, blocks={USER_LABEL: 4_000})
    crew_log_emit.on_context_measured(UNIT, 1, profile=profile)
    crew_log_emit.on_context_measured(
        UNIT, 1, profile=_real_profile(user=1, categories={"aNewCategory": 50})
    )
    crew_log_emit.on_turn_completed(UNIT, 1, model="model-a")
    usage = _usage()
    assert usage["context"]["turns"][0]["measured"]["history"]["user"] == 4_000
    assert usage["context"]["measured_unknown"] == {"aNewCategory": {"chars": 50, "turns": 1}}


def test_a_second_footer_of_the_turn_replaces_the_first_and_keeps_its_ttft():
    """A turn whose execution ran twice (a steering continuation) closes with two
    footers: the later request is the one that describes the context now."""
    _open()
    first = _real_profile(user=4_000)
    later = _real_profile(user=4_000, total_chars=70_000, no_ttft=True)
    later["turn_id"] = "a-later-run"
    crew_log_emit.on_context_composed(UNIT, 1, blocks={USER_LABEL: 4_000})
    crew_log_emit.on_context_measured(UNIT, 1, profile=first)
    crew_log_emit.on_context_measured(UNIT, 1, profile=later)
    crew_log_emit.on_turn_completed(UNIT, 1, model="model-a")
    row = _trace()["turns"][0]
    assert row["engine"]["total_chars"] == 70_000
    assert row["ttft_ms"]["total_ms"] == 2_870


def test_a_turn_without_a_measurement_reads_unmeasured_not_zero():
    """An older engine, a non-KAS backend, an error, a cancel or a crash before the
    footer: the closer has no profile and writes nothing, and the row says so."""
    _open()
    _turn(1, 4_000, None)
    _turn(2, 4_000, _real_profile(user=8_000))
    rows = _trace()["turns"]
    assert rows[0]["engine"] is None and rows[0]["ttft_ms"] is None
    assert rows[1]["engine"]["crew_chars"] == 8_000


def test_a_ttft_only_measurement_shows_ttft_and_no_breakdown():
    _open()
    profile = {"ttft": {"total_ms": 900}}
    _turn(1, 4_000, profile)
    row = _trace()["turns"][0]
    assert row["engine"] is None
    assert row["ttft_ms"] == {"total_ms": 900}


def test_a_negative_remainder_is_clamped_and_reported():
    """Crew counted more than the engine still holds (a rewind dropped a turn the sum
    still carries): the remainder is clamped at zero and the gap is shown."""
    _open()
    _turn(1, 4_000, _real_profile(user=4_000))
    _turn(2, 4_000, _real_profile(user=5_000))
    engine = _trace()["turns"][1]["engine"]
    assert engine["user_other_chars"] == 0
    assert engine["mismatch_chars"] == 3_000
    assert engine["crew_chars"] == 8_000


def test_a_model_switch_keeps_each_turns_own_numbers():
    """The engine's figures are characters, which a model switch does not rescale: each
    row keeps the measurement of its own turn, and the running sum carries across."""
    _open()
    _turn(1, 4_000, _real_profile(user=4_000, total_chars=60_000))
    crew_log_emit.on_request_configured(
        UNIT, 2, model="model-b", provider="acp", context_window=200_000
    )
    _turn(2, 4_000, _real_profile(user=8_000, total_chars=70_000), window=200_000)
    rows = _trace()["turns"]
    assert [t["model"] for t in rows] == ["model-a", "model-b"]
    assert [t["engine"]["total_chars"] for t in rows] == [60_000, 70_000]
    assert rows[1]["engine"]["crew_chars"] == 8_000


def test_a_category_the_engine_adds_later_is_kept_by_name():
    _open()
    _turn(1, 4_000, _real_profile(user=4_000, categories={"futureThing": 321}))
    trace = _trace()
    assert trace["turns"][0]["engine"]["unknown_chars"] == 321
    assert trace["engine_unknown"] == {"futureThing": {"chars": 321, "turns": 1}}


def test_an_unknown_schema_is_recorded_and_marked():
    _open()
    _turn(1, 4_000, _real_profile(user=4_000, schema=2))
    assert _trace()["turns"][0]["engine"]["schema_known"] is False


def test_with_the_crew_log_off_nothing_is_written(monkeypatch):
    monkeypatch.setenv("KIROCREW_CREW_LOG", "0")
    crew_log_emit.reset_caches()
    _open()
    _turn(1, 4_000, _real_profile(user=4_000))
    trace = _trace()
    assert trace["turns"] == []
    assert trace["recording"] is False


def test_a_measurement_with_no_row_of_its_own_attaches_nowhere():
    """Turn 2 composed nothing, so its measurement must not land on turn 1's row."""
    _open()
    _turn(1, 4_000, _real_profile(user=4_000))
    crew_log_emit.on_context_measured(UNIT, 2, profile=_real_profile(user=9_999))
    usage = _usage()
    assert usage["context"]["turns"][0]["measured"]["history"]["user"] == 4_000


def test_a_session_recorded_before_this_change_still_renders():
    """No ``context/measured`` anywhere in the log: every row reads unmeasured."""
    _open()
    crew_log_emit.on_context_composed(UNIT, 1, blocks={USER_LABEL: 10, "memory": 90})
    crew_log_emit.on_turn_completed(UNIT, 1, model="model-a", context_used=5, context_window=10)
    trace = _trace()
    assert [t["blocks"] for t in trace["turns"]] == [{USER_LABEL: 10, "memory": 90}]
    assert trace["turns"][0]["engine"] is None
    assert trace["engine_unknown"] == {}


# --------------------------------------------------------------------------- #
# O(1) per entry, bounded state
# --------------------------------------------------------------------------- #


class _CountingRows(list):
    """A context window that counts element reads and refuses to be walked."""

    reads = 0

    def __getitem__(self, index):  # type: ignore[override]
        type(self).reads += 1
        return super().__getitem__(index)

    def __iter__(self):
        raise AssertionError("the measured step walked the window")


def _fold_turns(turns: int) -> dict[str, Any]:
    """Fold *turns* composed-and-measured turns through the bare step functions."""
    from kiro_crew.crew_log.projection_usage import _usage_start, _usage_step

    state = _usage_start()
    seq = 0

    def step(entry_type: str, data: dict[str, Any]) -> None:
        nonlocal seq
        seq += 1
        _usage_step(
            state,
            Entry(type=entry_type, seq=seq, time=1_700_000_000_000 + seq, src="gateway", data=data),
        )

    step("session/opened", {})
    profile = _real_profile(user=4_000)
    for turn in range(1, turns + 1):
        step(
            "context/composed",
            {"turn": turn, "chars": 4_000, "sources": [{"kind": USER_LABEL, "chars": 4_000}]},
        )
        step(
            "context/measured",
            {"turn": turn, **{k: profile[k] for k in ("breakdown", "ttft")}, "turn_id": f"t{turn}"},
        )
        step("turn/completed", {"turn": turn})
    return state


def test_folding_a_measurement_reads_only_the_tail_row():
    for history in (10, 150):
        state = _fold_turns(history)
        rows = _CountingRows(state["context_turns"])
        state["context_turns"] = rows
        _CountingRows.reads = 0
        # The composition appends a row (the existing fold); the measurement is the
        # step under test, and it must touch the tail and nothing else.
        rows.append({"turn": history + 1, "unit": state["context_unit"], "measured": None})
        _CountingRows.reads = 0
        measured_mod.measured_step(
            state,
            Entry(
                type="context/measured",
                seq=1,
                time=1,
                src="gateway",
                data={"turn": history + 1, "ttft": {"total_ms": 5}},
            ),
        )
        assert _CountingRows.reads == 1, history


def test_the_measured_state_does_not_grow_with_the_session():
    """Fold N and 10N turns: every key this change adds holds the same shape, and the
    running sum is one integer whatever the history length."""
    small, large = _fold_turns(20), _fold_turns(200)
    for key in measured_mod.measured_start():
        assert type(small[key]) is type(large[key]), key
        if isinstance(small[key], (dict, str)):
            assert len(repr(large[key])) <= len(repr(small[key])) + 16, key
    assert large["context_crew_chars"] == 4_000 * 200


def test_a_negative_composed_size_cannot_drop_a_rows_measurement():
    """``context/composed`` chars are read leniently elsewhere; a negative one must not
    reach the encoded row, where it would make the whole measurement unreadable."""
    from kiro_crew.crew_log.projection_usage import _usage_start, _usage_step

    state = _usage_start()
    profile = _real_profile(user=4_000)
    for seq, (entry_type, data) in enumerate(
        [
            ("session/opened", {}),
            (
                "context/composed",
                {"turn": 1, "chars": -500, "sources": [{"kind": USER_LABEL, "chars": 1}]},
            ),
            (
                "context/measured",
                {"turn": 1, "breakdown": profile["breakdown"], "ttft": profile["ttft"]},
            ),
        ],
        1,
    ):
        _usage_step(
            state,
            Entry(type=entry_type, seq=seq, time=1_700_000_000_000 + seq, src="gateway", data=data),
        )
    decoded = measured_mod.decode_measured(state["context_turns"][-1]["measured"])
    assert decoded is not None
    assert decoded["crew_chars"] == 0
    assert decoded["total_chars"] == 59_333


@pytest.mark.parametrize(
    "text",
    [
        "m1," + ",".join(["\u00b2"] * measured_mod.MEASURED_FIELDS),
        "m1,-5" + "," * (measured_mod.MEASURED_FIELDS - 1),
        "m0",
        5,
        None,
    ],
)
def test_an_unreadable_row_string_decodes_to_none_rather_than_raising(text):
    assert measured_mod.decode_measured(text) is None
