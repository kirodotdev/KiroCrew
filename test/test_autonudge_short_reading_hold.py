"""A sustained run of one SHORT pull-request reading costs a bounded number of turns.

A short reading -- the fetch reached the pull request and left part of it unread -- is
never screened quiet on the half that arrived. While a forge is degraded the fetch
returns the same short reading every tick, and each one delivered a full turn carrying
nothing the previous turn did not. The gate now holds a repeat of the short reading the
last delivered turn was decided on, up to the quiet floor, and says so on every held
tick.

These tests pin the COST directly: they drive N simulated ticks through the real gate
and the real fire cycle and count the turns that were delivered. A classification-only
pin would pass whether or not any tick was held.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from kiro_crew import autonudge as _an
from kiro_crew import autonudge_judge as judge
from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.autonudge_service import gate as _gate
from kiro_crew.monitoring.models import (
    MonitorState,
    monitor_state_from_dict,
    monitor_state_to_dict,
)
from kiro_crew.probes import gh_pr

_CEILING = _gate._SHORT_READING_HOLD_CEILING
_SHORT_PAGE = ("check runs page 2 unread",)


def _reading(
    *,
    status: str = gh_pr.STATUS_PARTIAL,
    incomplete: tuple[str, ...] = _SHORT_PAGE,
    checks: tuple[Any, ...] = (),
    remarks: tuple[Any, ...] = (),
    observed_at: float = 1_000.0,
) -> gh_pr.PrObservation:
    """One pull-request reading, short unless told otherwise."""
    return gh_pr.PrObservation(
        repo="acme/widgets",
        pr=42,
        host="github.com",
        status=status,
        observed_at=observed_at,
        incomplete=incomplete if status == gh_pr.STATUS_PARTIAL else (),
        state="OPEN",
        head="a" * 40,
        checks=checks,
        checks_complete=status != gh_pr.STATUS_PARTIAL,
        remarks=remarks,
        remarks_total=len(remarks),
    )


def _whole() -> gh_pr.PrObservation:
    return _reading(status=gh_pr.STATUS_OK)


class _Forge:
    """The stubbed poll: hands the gate one reading per tick, as the real fetcher does."""

    def __init__(self, readings: list[gh_pr.PrObservation]) -> None:
        self.readings = list(readings)
        self.last: gh_pr.PrObservation | None = None

    def poll(self, _identity: str, _message: str, probe: Any) -> Any:
        # The fetcher publishes its reading on the probe and raises no observations,
        # so the kernel's own verdict is QUIET -- the shape a short reading really has.
        if self.readings:
            self.last = self.readings.pop(0)
        probe.observation = self.last
        return _an.irq.Verdict(_an.irq.Outcome.QUIET, "fetched", ())


class _Harness:
    """One service, one gated pull-request loop, and a count of delivered turns."""

    def __init__(
        self,
        tmp_path: Any,
        monkeypatch: Any,
        readings: list[gh_pr.PrObservation],
        *,
        judge_answer: bool | None = None,
        message: str = "watch https://github.com/acme/widgets/pull/42 until green",
        delivers: list[bool] | None = None,
    ) -> None:
        self.delivered: list[str] = []
        self.refused = 0
        self.judge_calls = 0
        self.notices: list[str] = []
        self._delivers = list(delivers or [])
        self.forge = _Forge(readings)
        monkeypatch.setattr(_an.irq, "poll", self.forge.poll)

        async def on_fire(loop: NudgeLoop) -> bool:
            if self._delivers and not self._delivers.pop(0):
                self.refused += 1
                return False
            self.delivered.append(loop.id)
            return True

        async def notice(loop: NudgeLoop, line: str) -> None:
            self.notices.append(line)

        self.service = AutoNudgeService(
            base_dir=tmp_path, on_fire=on_fire, emit_judge_notice=notice
        )

        async def screen(loop: NudgeLoop) -> bool | None:
            # ``None`` is the judge-less path; ``False`` is what a judge answers for a
            # tick whose pull request it could not read whole.
            self.judge_calls += 1
            return judge_answer

        self.service._judge_tick_is_quiet = screen  # type: ignore[method-assign]
        self.loop = NudgeLoop(
            id="short-reading",
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

    @property
    def monitor(self) -> MonitorState:
        assert self.loop.monitor is not None
        return self.loop.monitor

    async def tick(self) -> bool:
        """One interval: the gate decides, and a tick it does not hold is fired."""
        quiet = await self.service._monitor_tick_is_quiet(self.loop)
        if not quiet:
            await self.service._run_fire_cycle(self.loop)
        return quiet

    async def ticks(self, count: int) -> None:
        for _ in range(count):
            await self.tick()

    def stop(self) -> None:
        self.service.stop()


@pytest.fixture(autouse=True)
def _enable(_floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.mark.asyncio
@pytest.mark.parametrize("judge_answer", [None, False], ids=["no-judge", "judge-blind"])
async def test_a_sustained_run_of_one_short_reading_costs_a_bounded_number_of_turns(
    tmp_path, monkeypatch, judge_answer
):
    """The acceptance: N short ticks deliver ceil(N / ceiling) turns, not N."""
    intervals = 3 * _CEILING
    harness = _Harness(
        tmp_path,
        monkeypatch,
        [_reading() for _ in range(intervals)],
        judge_answer=judge_answer,
    )
    try:
        await harness.ticks(intervals)
        assert len(harness.delivered) < intervals, "a degraded forge must not cost a turn per tick"
        assert len(harness.delivered) == 3, "one turn per ceiling's worth of repeated short ticks"
        assert harness.judge_calls == 3, "a held tick does not ask the judge"
        assert len(harness.notices) == intervals - 3, "every held tick says the reading is short"
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_a_held_run_still_delivers_at_the_ceiling(tmp_path, monkeypatch):
    """The unread half gets a turn at the same bound the quiet floor gives every loop."""
    harness = _Harness(tmp_path, monkeypatch, [_reading() for _ in range(_CEILING + 1)])
    try:
        assert await harness.tick() is False, "the first short reading delivers"
        for held in range(1, _CEILING):
            assert await harness.tick() is True, f"repeat {held} is held"
        assert await harness.tick() is False, "the ceiling delivers anyway"
        assert harness.monitor.short_reading_held == 0, "and the delivery starts a new window"
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_a_lane_that_turns_red_in_the_read_half_delivers_at_once(tmp_path, monkeypatch):
    """Holding is for a reading with nothing new in it, never for a reading that moved."""
    red = (gh_pr.CheckRow("CI / Lint", "Lint", "failing", "2026-09-25T01:00:00Z"),)
    harness = _Harness(
        tmp_path, monkeypatch, [_reading(), _reading(), _reading(checks=red), _reading(checks=red)]
    )
    try:
        assert await harness.tick() is False
        assert await harness.tick() is True, "an unchanged repeat is held"
        assert await harness.tick() is False, "a failing check in what WAS read delivers"
        assert await harness.tick() is True, "and a repeat of that new reading is held again"
        assert len(harness.delivered) == 2
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_a_new_remark_in_a_short_reading_delivers_at_once(tmp_path, monkeypatch):
    remark = gh_pr.Remark(
        kind="comment",
        ident="comment:C1",
        author="a-reviewer",
        at="2026-09-25T06:00:00Z",
        age_s=60.0,
        verdict="",
        body="please guard the windows branch",
    )
    harness = _Harness(tmp_path, monkeypatch, [_reading(), _reading(remarks=(remark,))])
    try:
        await harness.ticks(2)
        assert len(harness.delivered) == 2, "a reviewer's new remark is never held"
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_a_different_reason_for_coming_back_short_delivers(tmp_path, monkeypatch):
    """Two short readings are not interchangeable: each names what it is missing."""
    harness = _Harness(
        tmp_path,
        monkeypatch,
        [_reading(), _reading(incomplete=("statuses page 1 unread",))],
    )
    try:
        await harness.ticks(2)
        assert len(harness.delivered) == 2, "a different loss is a different reading"
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_a_whole_reading_ends_the_run(tmp_path, monkeypatch):
    """Consecutive means consecutive: a short reading after a whole one starts over."""
    harness = _Harness(
        tmp_path, monkeypatch, [_reading(), _reading(), _whole(), _reading(), _reading()]
    )
    try:
        await harness.tick()
        assert await harness.tick() is True
        await harness.tick()
        assert harness.monitor.short_reading_digest == "", "a whole reading clears the baseline"
        assert await harness.tick() is False, "the next short reading opens a new run"
        assert await harness.tick() is True
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_a_refused_delivery_earns_no_hold(tmp_path, monkeypatch):
    """Only a turn the owner actually received can make a repeat skippable."""
    harness = _Harness(
        tmp_path, monkeypatch, [_reading() for _ in range(4)], delivers=[False, True]
    )
    try:
        assert await harness.tick() is False
        assert harness.refused == 1
        assert harness.monitor.short_reading_digest == "", "a refused fire records nothing"
        # The refused fire's retry is the gate's ordinary follow-up bypass, and it is
        # the turn that lands.
        assert await harness.tick() is False
        assert harness.delivered == [harness.loop.id]
        assert harness.monitor.short_reading_digest, "the delivered turn is the baseline"
        assert await harness.tick() is True
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_a_death_before_delivery_holds_nothing(tmp_path, monkeypatch):
    """A tick that decided to deliver and never reached its fire leaves no baseline."""
    harness = _Harness(tmp_path, monkeypatch, [_reading() for _ in range(3)])
    try:
        assert await harness.service._monitor_tick_is_quiet(harness.loop) is False
        # No fire cycle: the process stopped between the decision and the turn.
        assert await harness.service._monitor_tick_is_quiet(harness.loop) is False
    finally:
        harness.stop()


def _stored_monitor(tmp_path: Any, loop_id: str) -> dict:
    """What the record on disk holds for this loop's monitor."""
    rows = json.loads((tmp_path / "autonudge.json").read_text(encoding="utf-8"))["loops"]
    return next(row for row in rows if row["id"] == loop_id)["monitor"]


@pytest.mark.asyncio
async def test_a_held_count_is_on_disk_before_the_tick_returns(tmp_path, monkeypatch):
    """A hold suppresses a turn, so the count that bounds it is durable first."""
    harness = _Harness(tmp_path, monkeypatch, [_reading(), _reading()])
    try:
        await harness.tick()
        assert await harness.tick() is True
        stored = _stored_monitor(tmp_path, harness.loop.id)
        assert stored["short_reading_held"] == 1
        assert stored["short_reading_digest"] == harness.monitor.short_reading_digest
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_a_hold_whose_count_will_not_persist_delivers(tmp_path, monkeypatch):
    """A hold the record does not carry would let a restart reopen the window."""
    harness = _Harness(tmp_path, monkeypatch, [_reading(), _reading()])
    real = harness.service._write_monitor_snapshot_locked

    async def _refuse_the_hold(payload: dict | None = None) -> None:
        if payload is None and harness.monitor.short_reading_held:
            raise OSError("disk full")
        await real(payload)

    try:
        await harness.tick()
        harness.service._write_monitor_snapshot_locked = _refuse_the_hold  # type: ignore[method-assign]
        assert await harness.tick() is False, "a refused hold write delivers instead"
        assert harness.monitor.short_reading_held == 0, "and the count is put back"
        assert len(harness.delivered) == 2
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_a_whole_reading_clears_the_baseline_in_the_reading_s_own_write(
    tmp_path, monkeypatch
):
    """A process that stops after a whole reading is kept must not keep the old baseline.

    The judge is awaited after the reading is published, and a gateway that stops
    inside that await would otherwise restart with the whole reading on disk beside a
    baseline from before it -- and hold the next matching short reading against it.
    """
    harness = _Harness(tmp_path, monkeypatch, [_reading(), _whole()])
    try:
        await harness.tick()
        assert _stored_monitor(tmp_path, harness.loop.id)["short_reading_digest"]

        async def _dies(loop: NudgeLoop) -> bool | None:
            raise asyncio.CancelledError()

        harness.service._judge_tick_is_quiet = _dies  # type: ignore[method-assign]
        with pytest.raises(asyncio.CancelledError):
            await harness.service._monitor_tick_is_quiet(harness.loop)
        stored = _stored_monitor(tmp_path, harness.loop.id)
        assert stored["last_observation"]["observation_status"] == gh_pr.STATUS_OK
        assert (stored["short_reading_digest"], stored["short_reading_held"]) == ("", 0)
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_a_loop_that_also_watches_a_session_is_never_held(tmp_path, monkeypatch):
    """Holding the tick would hold the session's news too, which the reading cannot show."""
    harness = _Harness(
        tmp_path,
        monkeypatch,
        [_reading() for _ in range(4)],
        message=(
            "watch https://github.com/acme/widgets/pull/42 until green and wake on "
            "chat-7-1791000000"
        ),
    )
    try:
        await harness.ticks(4)
        assert len(harness.delivered) == 4
    finally:
        harness.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "targets",
    [
        [
            "https://github.com/acme/widgets/pull/42",
            "https://github.com/acme/widgets/pull/43",
        ],
        ["https://github.com/acme/widgets/pull/43"],
        [],
    ],
    ids=["a-second-pull-request", "only-another-pull-request", "no-target"],
)
async def test_a_brief_naming_anything_but_the_watched_pull_request_is_never_held(
    tmp_path, monkeypatch, targets
):
    """The judge would count every other target as unread, so the tick is not held."""
    harness = _Harness(tmp_path, monkeypatch, [_reading() for _ in range(4)])
    harness.loop.judge = {"wake_when": "a review asks for a change", "targets": targets}
    try:
        await harness.ticks(4)
        assert len(harness.delivered) == 4
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_a_brief_naming_only_the_watched_pull_request_is_held(tmp_path, monkeypatch):
    harness = _Harness(tmp_path, monkeypatch, [_reading(), _reading()])
    harness.loop.judge = {
        "wake_when": "a review asks for a change",
        "targets": ["https://github.com/acme/widgets/pull/42"],
    }
    try:
        await harness.tick()
        assert await harness.tick() is True
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_an_owed_judge_wake_is_never_held(tmp_path, monkeypatch):
    harness = _Harness(tmp_path, monkeypatch, [_reading() for _ in range(2)])
    try:
        await harness.tick()
        harness.loop.judge_wake_pending = True
        assert await harness.tick() is False, "a turn already owed is delivered, never folded"
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_a_held_tick_leaves_the_judge_baseline_alone(tmp_path, monkeypatch):
    """No verdict was reached, so nothing the judge would compare against may move."""
    harness = _Harness(tmp_path, monkeypatch, [_reading(), _reading()], judge_answer=False)

    async def _commit(*_args: Any, **_kwargs: Any) -> bool:
        commits.append(True)
        return True

    commits: list[bool] = []
    harness.service._commit_judge_pr_seen = _commit  # type: ignore[method-assign]
    try:
        await harness.tick()
        assert commits == [True], "the delivered tick reached a verdict and committed"
        assert await harness.tick() is True
        assert commits == [True], "the held tick committed nothing"
        assert harness.monitor.quiet_ticks == 0, "and is not counted as a quiet observation"
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_the_notice_names_why_the_reading_is_short(tmp_path, monkeypatch):
    harness = _Harness(tmp_path, monkeypatch, [_reading(), _reading()])
    try:
        await harness.ticks(2)
        assert len(harness.notices) == 1
        assert "check runs page 2 unread" in harness.notices[0]
        assert f"next turn in {_CEILING - 1} tick(s)" in harness.notices[0]
    finally:
        harness.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"message": "watch https://github.com/acme/widgets/pull/42 until green, then summarize"},
        {"judge": {"wake_when": "a review asks for a change", "quiet_when": "nothing did"}},
    ],
    ids=["reworded-instruction", "replaced-brief"],
)
async def test_re_aiming_the_loop_delivers_the_next_short_reading(tmp_path, monkeypatch, change):
    """The held turn was delivered under the question being replaced."""
    harness = _Harness(tmp_path, monkeypatch, [_reading(), _reading(), _reading()])
    try:
        await harness.tick()
        assert await harness.tick() is True
        monitor = harness.monitor
        await harness.service.update(harness.loop.id, **change)
        assert harness.loop.monitor is monitor, "the same subject keeps its monitor"
        assert (monitor.short_reading_digest, monitor.short_reading_held) == ("", 0)
        assert await harness.tick() is False, "a re-aimed loop is handed the repeat"
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_a_re_aim_during_an_in_flight_turn_is_not_undone_by_its_delivery(
    tmp_path, monkeypatch
):
    """The turn in flight answered the old question, so it earns no hold under the new one."""
    harness = _Harness(tmp_path, monkeypatch, [_reading(), _reading(), _reading()])
    real_fire = harness.service._on_fire

    async def _re_aimed_mid_turn(loop: NudgeLoop) -> bool:
        await harness.service.update(
            loop.id,
            message="watch https://github.com/acme/widgets/pull/42 until green, then summarize",
        )
        assert real_fire is not None
        return await real_fire(loop)

    try:
        await harness.tick()
        assert await harness.tick() is True
        harness.service._on_fire = _re_aimed_mid_turn
        # The ceiling's delivery is in flight when the owner rewords the instruction.
        harness.monitor.short_reading_held = _CEILING - 1
        assert await harness.tick() is False
        assert harness.monitor.short_reading_digest == "", "the re-aim's clear stands"
        harness.service._on_fire = real_fire
        assert await harness.tick() is False, "the reworded loop is handed the repeat"
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_a_re_aim_landing_before_the_hold_commits_wins(tmp_path, monkeypatch):
    """A hold earned against the old instruction is never committed after the new one."""
    harness = _Harness(tmp_path, monkeypatch, [_reading()])
    try:
        await harness.tick()
        loop, monitor = harness.loop, harness.monitor
        staged_spec, staged_message = judge.spec_of(loop), loop.message
        loop.message = "watch https://github.com/acme/widgets/pull/42 and summarize"
        committed = await _gate._commit_short_reading_hold(
            harness.service,
            loop,
            monitor,
            staged_for_spec=staged_spec,
            staged_for_message=staged_message,
        )
        assert committed is False
        assert monitor.short_reading_held == 0
    finally:
        harness.stop()


def test_the_ceiling_is_the_quiet_floor():
    assert _CEILING == _an._MAX_QUIET_STREAK


def test_the_hold_survives_a_round_trip_through_the_record():
    state = MonitorState(
        kind="gh-pr",
        target="acme/widgets#42",
        objective="review_ready",
        created_ts=1_000.0,
        short_reading_digest="0123456789abcdef",
        short_reading_held=3,
    )
    loaded = monitor_state_from_dict(monitor_state_to_dict(state))
    assert loaded.short_reading_digest == "0123456789abcdef"
    assert loaded.short_reading_held == 3


@pytest.mark.parametrize(
    "fields",
    [
        {"short_reading_digest": 7, "short_reading_held": 3},
        {"short_reading_digest": "f" * 65, "short_reading_held": 3},
        {"short_reading_digest": "0123456789abcdef", "short_reading_held": -1},
        {"short_reading_digest": "0123456789abcdef", "short_reading_held": True},
        {"short_reading_digest": "0123456789abcdef", "short_reading_held": "3"},
    ],
)
def test_an_unreadable_hold_loads_as_no_baseline(fields):
    """Every unreadable shape resolves toward delivering the next short reading."""
    raw = monitor_state_to_dict(
        MonitorState(
            kind="gh-pr", target="acme/widgets#42", objective="review_ready", created_ts=1.0
        )
    )
    raw.update(fields)
    loaded = monitor_state_from_dict(raw)
    assert loaded.short_reading_digest == ""
    assert isinstance(loaded.short_reading_held, int) and loaded.short_reading_held >= 0


def test_a_record_written_before_the_hold_loads_with_none():
    raw = monitor_state_to_dict(
        MonitorState(
            kind="gh-pr", target="acme/widgets#42", objective="review_ready", created_ts=1.0
        )
    )
    raw.pop("short_reading_digest")
    raw.pop("short_reading_held")
    loaded = monitor_state_from_dict(raw)
    assert (loaded.short_reading_digest, loaded.short_reading_held) == ("", 0)
