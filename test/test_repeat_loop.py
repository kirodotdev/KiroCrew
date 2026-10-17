"""The repeat-loop tracker: same call, same result, told once."""

from __future__ import annotations

import asyncio

from kiro_crew.dashboard.chat_runner import _steer_repeat_loop_notice
from kiro_crew.repeat_loop import _MAX_TRACKED, REPEAT_LOOP_THRESHOLD, RepeatLoopTracker


def _run(
    tracker: RepeatLoopTracker, n: int, *, cmd: str = "make test", output: str = "boom"
) -> list[str]:
    notices = []
    for i in range(n):
        cid = f"{cmd}-{i}"
        tracker.note_call(cid, "shell", cmd, f"Run {cmd}")
        notices.append(tracker.note_result(cid, status="failed", output=output))
    return notices


def test_threshold_identical_results_send_one_notice() -> None:
    notices = _run(RepeatLoopTracker(), REPEAT_LOOP_THRESHOLD + 3)
    sent = [n for n in notices if n]
    assert len(sent) == 1
    assert notices[REPEAT_LOOP_THRESHOLD - 1] == sent[0]
    assert "Run make test" in sent[0]
    assert f"{REPEAT_LOOP_THRESHOLD} times" in sent[0]


def test_below_threshold_is_silent() -> None:
    assert not any(_run(RepeatLoopTracker(), REPEAT_LOOP_THRESHOLD - 1))


def test_changing_result_resets_the_streak() -> None:
    tracker = RepeatLoopTracker()
    for i in range(REPEAT_LOOP_THRESHOLD * 2):
        tracker.note_call(f"c{i}", "shell", "make test", "Run make test")
        assert tracker.note_result(f"c{i}", status="failed", output=f"error {i % 2}") == ""


def test_different_input_is_a_different_call() -> None:
    tracker = RepeatLoopTracker()
    for i in range(REPEAT_LOOP_THRESHOLD * 2):
        tracker.note_call(f"c{i}", "shell", f"make test-{i}", "Run make test")
        assert tracker.note_result(f"c{i}", status="failed", output="boom") == ""


def test_interleaved_calls_still_count_per_call() -> None:
    tracker = RepeatLoopTracker()
    sent = []
    for i in range(REPEAT_LOOP_THRESHOLD):
        for cmd in ("a", "b"):
            cid = f"{cmd}{i}"
            tracker.note_call(cid, "shell", cmd, cmd)
            sent.append(tracker.note_result(cid, status="failed", output="no"))
    assert len([n for n in sent if n]) == 2


def test_output_from_an_earlier_frame_is_kept() -> None:
    """Output and terminal status may arrive in separate frames."""
    tracker = RepeatLoopTracker()
    for i in range(REPEAT_LOOP_THRESHOLD * 2):
        tracker.note_call(f"c{i}", "shell", "make", "make")
        assert tracker.note_result(f"c{i}", status="", output=f"out {i}", terminal=False) == ""
        assert tracker.note_result(f"c{i}", status="completed", output="") == ""


def test_unknown_or_inputless_call_is_ignored() -> None:
    tracker = RepeatLoopTracker()
    for i in range(REPEAT_LOOP_THRESHOLD * 2):
        tracker.note_call(f"c{i}", "shell", "", "empty")
        assert tracker.note_result(f"c{i}", status="failed", output="x") == ""
        assert tracker.note_result("never-seen", status="failed", output="x") == ""


def test_retained_fields_are_bounded() -> None:
    tracker = RepeatLoopTracker()
    huge_id, huge_title = "i" * 100_000, "t" * 100_000
    tracker.note_call(huge_id, "shell", "make", huge_title)
    ((key, (_, title)),) = tracker._calls.items()
    assert len(key) == 64 and len(title) <= 200


def test_pending_calls_are_capped() -> None:
    tracker = RepeatLoopTracker()
    for i in range(_MAX_TRACKED):
        tracker.note_call(f"c{i}", "shell", f"cmd {i}", "t")
    before = dict(tracker._calls)
    tracker.note_call("c0", "shell", "refined", "t")
    assert tracker._calls != before
    tracker.note_call("overflow", "shell", "cmd", "t")
    assert len(tracker._calls) == _MAX_TRACKED
    assert tracker.note_result("overflow", status="failed", output="x") == ""
    assert len(tracker._calls) == _MAX_TRACKED


def test_streak_signatures_are_capped() -> None:
    tracker = RepeatLoopTracker()
    for i in range(_MAX_TRACKED):
        tracker.note_call(f"c{i}", "shell", f"cmd {i}", "t")
        tracker.note_result(f"c{i}", status="failed", output="x")
    assert len(tracker._streak) == _MAX_TRACKED
    notices = _run(tracker, REPEAT_LOOP_THRESHOLD, cmd="new")
    assert notices == [""] * REPEAT_LOOP_THRESHOLD
    assert len(tracker._streak) == _MAX_TRACKED


class _Client:
    def __init__(self, capable: bool) -> None:
        self.supports_refusal_steer = capable
        self.sent: list[str] = []

    async def steer(self, text: str) -> bool:
        self.sent.append(text)
        return True


def test_steer_is_gated_on_capability() -> None:
    capable, plain = _Client(True), _Client(False)
    assert asyncio.run(_steer_repeat_loop_notice(capable, "note")) is True
    assert asyncio.run(_steer_repeat_loop_notice(plain, "note")) is False
    assert capable.sent == ["note"] and plain.sent == []
