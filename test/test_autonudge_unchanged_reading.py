"""An unchanged pull-request board asks no judge and starts no turn.

A gated pull-request loop reads its subject on every tick. When that reading is
WHOLE and byte-identical to the one the last verdict was reached on, and every
target the loop's judge would read is that pull request, nothing the judge could be
shown has moved, so no criterion about the subject can have become true. The gate
then asks no judge and fires no turn; the tick is quiet, and the quiet floor still
delivers every ``_MAX_QUIET_STREAK``th one. A judge that cannot answer -- a timeout
is a fallback, and a fallback fires -- therefore costs no turn on an unchanged board.

Nearby inputs keep their path: a changed board, a short reading, a reading that
never reached the subject, an owed judge wake, a brief that names anything beyond
the watched pull request, and a lowered ``decisions.nudge_wake.quiet_streak_floor``
all still go to the judge.

These tests drive ticks through the real gate and the real fire cycle and count the
turns delivered and the judge calls made, so they pin the cost and not a label.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew import autonudge as _an
from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.autonudge_service import gate as _gate
from kiro_crew.monitoring.models import MonitorState
from kiro_crew.probes import gh_pr

_FLOOR = _gate._MAX_QUIET_STREAK
_URL = "https://github.com/acme/widgets/pull/42"


def _reading(**fields: Any) -> gh_pr.PrObservation:
    """One whole reading of acme/widgets#42 unless *fields* say otherwise."""
    base: dict[str, Any] = {
        "repo": "acme/widgets",
        "pr": 42,
        "host": "github.com",
        "status": gh_pr.STATUS_OK,
        "observed_at": 1_000.0,
        "state": "OPEN",
        "head": "a" * 40,
    }
    base.update(fields)
    return gh_pr.PrObservation(**base)


def _short() -> gh_pr.PrObservation:
    return _reading(
        status=gh_pr.STATUS_PARTIAL,
        incomplete=("check runs page 2 unread",),
        checks_complete=False,
    )


class _Harness:
    """One service, one gated pull-request loop, a judge spy and a count of turns."""

    def __init__(
        self,
        tmp_path: Any,
        monkeypatch: Any,
        readings: list[gh_pr.PrObservation],
        *,
        judge_answer: bool | None = False,
        message: str = f"watch {_URL} until green",
    ) -> None:
        self.delivered: list[str] = []
        self.judge_calls = 0
        self._readings = list(readings)
        self._last: gh_pr.PrObservation | None = None
        monkeypatch.setattr(_an.irq, "poll", self._poll)

        async def on_fire(loop: NudgeLoop) -> bool:
            self.delivered.append(loop.id)
            return True

        self.service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)

        async def screen(loop: NudgeLoop) -> bool | None:
            # ``False`` is what a judge that timed out answers (a fallback fires);
            # ``None`` is the judge-less path.
            self.judge_calls += 1
            return judge_answer

        self.service._judge_tick_is_quiet = screen  # type: ignore[method-assign]
        self.loop = NudgeLoop(
            id="unchanged-board",
            slot_key="chat-1-123",
            message=message,
            idle_secs=300,
            monitor=MonitorState(
                kind="gh-pr",
                target="acme/widgets#42",
                objective="review_ready",
                created_ts=1_000.0,
            ),
            gate=True,
        )
        self.service._loops[self.loop.id] = self.loop

    def _poll(self, _identity: str, _message: str, probe: Any) -> Any:
        # The fetcher publishes its reading on the probe and raises no observations,
        # so the kernel's own verdict is QUIET, the shape every live reading has.
        if self._readings:
            self._last = self._readings.pop(0)
        probe.observation = self._last
        return _an.irq.Verdict(_an.irq.Outcome.QUIET, "fetched", ())

    @property
    def monitor(self) -> MonitorState:
        assert self.loop.monitor is not None
        return self.loop.monitor

    async def tick(self) -> bool:
        """One interval: the gate decides, and a tick it does not keep quiet is fired."""
        quiet = await self.service._monitor_tick_is_quiet(self.loop)
        if not quiet:
            await self.service._run_fire_cycle(self.loop)
        return quiet

    def stop(self) -> None:
        self.service.stop()


@pytest.fixture(autouse=True)
def _enable(_floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.mark.asyncio
@pytest.mark.parametrize("judge_answer", [False, None], ids=["judge-cannot-answer", "no-judge"])
async def test_an_unchanged_board_asks_no_judge_and_starts_no_turn(
    tmp_path, monkeypatch, judge_answer
):
    harness = _Harness(
        tmp_path, monkeypatch, [_reading() for _ in range(3)], judge_answer=judge_answer
    )
    try:
        assert await harness.tick() is False, "the first reading has nothing to compare against"
        assert harness.judge_calls == 1
        assert await harness.tick() is True, "an unchanged board is a quiet tick"
        assert await harness.tick() is True
        assert harness.judge_calls == 1, "an unchanged board asks no judge"
        assert harness.delivered == [harness.loop.id], "and starts no turn"
        assert harness.monitor.quiet_ticks == 2
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_the_quiet_floor_still_delivers_an_unchanged_board(tmp_path, monkeypatch):
    """Silence stays bounded: the floor's turn arrives without a judge call."""
    harness = _Harness(tmp_path, monkeypatch, [_reading() for _ in range(_FLOOR + 1)])
    try:
        assert await harness.tick() is False
        for held in range(1, _FLOOR):
            assert await harness.tick() is True, f"unchanged tick {held} is quiet"
        assert await harness.tick() is False, "the floor delivers anyway"
        assert harness.delivered == [harness.loop.id, harness.loop.id]
        assert harness.judge_calls == 1, "the floor's turn asks no judge either"
        assert harness.monitor.floor_ticks == 1
    finally:
        harness.stop()


_CHANGES = {
    "a-lane-turns-red": {
        "checks": (gh_pr.CheckRow("CI / Lint", "Lint", "failing", "2026-09-25T01:00:00Z"),),
    },
    "a-reviewer-comments": {
        "remarks": (
            gh_pr.Remark(
                kind="comment",
                ident="comment:C1",
                author="a-reviewer",
                at="2026-09-25T06:00:00Z",
                age_s=60.0,
                verdict="",
                body="please guard the windows branch",
            ),
        ),
        "remarks_total": 1,
    },
    "the-head-moves": {"head": "b" * 40},
    "the-base-conflicts": {"mergeability": "CONFLICTING"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("judge_answer", [False, None], ids=["judge-cannot-answer", "no-judge"])
@pytest.mark.parametrize("change", sorted(_CHANGES))
async def test_a_changed_board_wakes_on_the_tick_that_sees_it(
    tmp_path, monkeypatch, change, judge_answer
):
    readings = [_reading(), _reading(), _reading(**_CHANGES[change])]
    harness = _Harness(tmp_path, monkeypatch, readings, judge_answer=judge_answer)
    try:
        await harness.tick()
        assert await harness.tick() is True
        assert await harness.tick() is False, "the tick that sees the change wakes the chat"
        assert harness.judge_calls == 2, "and the judge is asked about it"
        assert len(harness.delivered) == 2
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_a_short_reading_after_an_unchanged_board_goes_to_the_judge(tmp_path, monkeypatch):
    harness = _Harness(tmp_path, monkeypatch, [_reading(), _reading(), _short()])
    try:
        await harness.tick()
        assert await harness.tick() is True
        assert await harness.tick() is False, "half a board is never an unchanged one"
        assert harness.judge_calls == 2
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_a_short_reading_matching_the_judged_digest_still_goes_to_the_judge(
    tmp_path, monkeypatch
):
    """Two identical partial readings agree only about the half that was read."""
    short = _short()
    harness = _Harness(tmp_path, monkeypatch, [short])
    harness.loop.judge_pr_seen = {"digest": _an._pr_facts_digest(short.as_facts()), "remarks": []}
    try:
        assert await harness.tick() is False
        assert harness.judge_calls == 1
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_a_reading_that_never_reached_the_subject_goes_to_the_judge(tmp_path, monkeypatch):
    unread = _reading(status=gh_pr.STATUS_UNAVAILABLE, state="", head="")
    harness = _Harness(tmp_path, monkeypatch, [_reading(), _reading(), unread])
    try:
        await harness.tick()
        assert await harness.tick() is True
        assert await harness.tick() is False, "a failed read is no evidence of calm"
        assert harness.judge_calls == 2
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_an_owed_judge_wake_is_delivered_on_an_unchanged_board(tmp_path, monkeypatch):
    harness = _Harness(tmp_path, monkeypatch, [_reading(), _reading()])
    try:
        await harness.tick()
        harness.loop.judge_wake_pending = True
        assert await harness.tick() is False, "a turn already owed is delivered"
        assert harness.judge_calls == 2
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_a_lowered_judge_floor_keeps_every_tick_judged(tmp_path, monkeypatch):
    """``decisions.nudge_wake.quiet_streak_floor`` counts judge verdicts, so a skip would
    stretch an operator's shorter bound on silence to the probe's."""
    harness = _Harness(tmp_path, monkeypatch, [_reading() for _ in range(3)], judge_answer=True)
    harness.service._judge_quiet_streak_floor = lambda: 3  # type: ignore[method-assign]
    try:
        for _ in range(3):
            await harness.tick()
        assert harness.judge_calls == 3, "with the knob lowered, an unchanged board is judged"
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_a_loop_that_also_watches_a_session_is_still_judged(tmp_path, monkeypatch):
    """The session's transcript is evidence the board cannot show."""
    harness = _Harness(
        tmp_path,
        monkeypatch,
        [_reading() for _ in range(3)],
        message=f"watch {_URL} until green and wake on chat-7-1791000000",
    )
    try:
        for _ in range(3):
            await harness.tick()
        assert harness.judge_calls == 3
        assert len(harness.delivered) == 3
    finally:
        harness.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "targets",
    [
        [_URL, "https://github.com/acme/widgets/pull/43"],
        ["https://github.com/acme/widgets/pull/43"],
        [],
    ],
    ids=["a-second-pull-request", "only-another-pull-request", "no-target"],
)
async def test_a_brief_naming_anything_but_the_watched_pull_request_spends_every_tick(
    tmp_path, monkeypatch, targets
):
    harness = _Harness(tmp_path, monkeypatch, [_reading() for _ in range(3)])
    harness.loop.judge = {"wake_when": "a review asks for a change", "targets": targets}
    try:
        for _ in range(3):
            await harness.tick()
        assert len(harness.delivered) == 3
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_a_brief_naming_only_the_watched_pull_request_skips_the_judge(tmp_path, monkeypatch):
    harness = _Harness(tmp_path, monkeypatch, [_reading(), _reading()])
    harness.loop.judge = {"wake_when": "a review asks for a change", "targets": [_URL]}
    try:
        await harness.tick()
        assert await harness.tick() is True
        assert harness.judge_calls == 1
    finally:
        harness.stop()
