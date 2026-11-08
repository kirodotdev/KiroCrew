"""A judge's QUIET never silences a blocker the owner has not been shown.

A gated pull-request loop screens each tick through a wake judge. The judge reads
prose and typed tallies and answers whether the owner must act -- but a narrow custom
brief can answer QUIET on a tick where a check has just gone red, or the branch has
just started conflicting, because the brief's own words did not mention that lane. The
typed classifier already holds a failing check as ACTIONABLE, but on a gated loop the
fetcher raises no observation, so once a judge runs nothing else gets a say, and the
owner sleeps through the blocker until the quiet floor.

The gate now wakes on a blocker the LAST DELIVERED turn did not carry, whatever the
judge said. A red check the owner was already shown (the same failing set as the last
delivery) still stays quiet -- a PR under repair reports the same red lane every tick.

These tests drive ticks through the real gate and the real fire cycle and count the
turns delivered, and each pins that the wake is the override's doing by removing it.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew import autonudge as _an
from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.autonudge_service import gate as _gate
from kiro_crew.monitoring.models import (
    MonitorState,
    monitor_state_from_dict,
    monitor_state_to_dict,
)
from kiro_crew.probes import gh_pr

_LINT_RED = (gh_pr.CheckRow("CI / Lint", "Lint", "failing", "2026-09-25T01:00:00Z"),)
_TYPE_RED = (gh_pr.CheckRow("CI / Types", "Types", "failing", "2026-09-25T02:00:00Z"),)
_BOTH_RED = _LINT_RED + _TYPE_RED


def _reading(
    *,
    status: str = gh_pr.STATUS_OK,
    checks: tuple[Any, ...] = (),
    mergeability: str = "",
    merge_state: str = "",
    observed_at: float = 1_000.0,
) -> gh_pr.PrObservation:
    """One WHOLE pull-request reading, with whatever checks / mergeability it carries."""
    return gh_pr.PrObservation(
        repo="acme/widgets",
        pr=42,
        host="github.com",
        status=status,
        observed_at=observed_at,
        incomplete=(),
        state="OPEN",
        head="a" * 40,
        checks=checks,
        checks_complete=True,
        mergeability=mergeability,
        merge_state=merge_state,
        remarks=(),
        remarks_total=0,
    )


class _Forge:
    """The stubbed poll: one reading per tick, published on the probe, kernel QUIET."""

    def __init__(self, readings: list[gh_pr.PrObservation]) -> None:
        self.readings = list(readings)
        self.last: gh_pr.PrObservation | None = None

    def poll(self, _identity: str, _message: str, probe: Any) -> Any:
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
        judge_answer: bool | None,
    ) -> None:
        self.delivered: list[str] = []
        self.judge_calls = 0
        self.notices: list[str] = []
        # The FIRST screen answers WAKE so the opening reading is delivered and becomes
        # the basis every later tick is measured against; every tick after it answers
        # *judge_answer* (QUIET or the judge-less ``None``). This models a watch armed
        # on a PR that then holds: the owner's first look lands, and the gap this
        # repairs is a blocker that appears AFTER it, which a quiet judge would sleep
        # through.
        self._first = True
        self.forge = _Forge(readings)
        monkeypatch.setattr(_an.irq, "poll", self.forge.poll)

        async def on_fire(loop: NudgeLoop) -> bool:
            self.delivered.append(loop.id)
            return True

        async def notice(loop: NudgeLoop, line: str) -> None:
            self.notices.append(line)

        self.service = AutoNudgeService(
            base_dir=tmp_path, on_fire=on_fire, emit_judge_notice=notice
        )

        async def screen(loop: NudgeLoop) -> bool | None:
            # ``True`` is the judge answering QUIET; ``None`` is the judge-less path;
            # ``False`` is the judge waking (used only for the opening delivery).
            self.judge_calls += 1
            if self._first:
                self._first = False
                return False
            return judge_answer

        self.service._judge_tick_is_quiet = screen  # type: ignore[method-assign]
        self.loop = NudgeLoop(
            id="newly-failing",
            slot_key="chat-1-123",
            message="watch https://github.com/acme/widgets/pull/42 until green",
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
@pytest.mark.parametrize("judge_answer", [True, None], ids=["judge-quiet", "no-judge"])
async def test_a_newly_failing_check_wakes_past_a_quiet_verdict(
    tmp_path, monkeypatch, judge_answer
):
    """ACCEPTANCE (4a): judge QUIET + a check not failing at the last delivery -> WAKE."""
    harness = _Harness(
        tmp_path,
        monkeypatch,
        [_reading(), _reading(checks=_LINT_RED)],
        judge_answer=judge_answer,
    )
    try:
        assert await harness.tick() is False, "the first reading is delivered (first look)"
        assert await harness.tick() is False, "a check that just went red wakes the loop"
        assert len(harness.delivered) == 2
        assert harness.monitor.delivered_failed_checks == [_LINT_RED[0].name]
    finally:
        harness.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("judge_answer", [True, None], ids=["judge-quiet", "no-judge"])
async def test_the_wake_is_the_overrides_doing(tmp_path, monkeypatch, judge_answer):
    """ACCEPTANCE (4a, fix removed): with the override stubbed off, the same tick is QUIET."""
    harness = _Harness(
        tmp_path,
        monkeypatch,
        [_reading(), _reading(checks=_LINT_RED)],
        judge_answer=judge_answer,
    )
    # The fix removed: the gate finds nothing new to wake on, so the judge (or the
    # earned judge-less quiet on an unchanged board) decides alone.
    monkeypatch.setattr(_gate, "_newly_blocked", lambda _monitor: [])
    try:
        assert await harness.tick() is False, "the first reading is delivered"
        # A judge QUIET now suppresses; the judge-less path still delivers a CHANGED
        # reading (a red check is a change), so the no-judge arm is not a fair
        # fix-removed control -- only the judge-quiet arm proves suppression.
        quiet = await harness.tick()
        if judge_answer is True:
            assert quiet is True, "without the override a quiet judge sleeps through the red"
            assert len(harness.delivered) == 1
    finally:
        harness.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("judge_answer", [True, None], ids=["judge-quiet", "no-judge"])
async def test_the_same_failing_set_as_the_last_delivery_stays_quiet(
    tmp_path, monkeypatch, judge_answer
):
    """ACCEPTANCE (4b): judge QUIET + the same failing set the owner saw -> QUIET."""
    harness = _Harness(
        tmp_path,
        monkeypatch,
        [_reading(checks=_LINT_RED), _reading(checks=_LINT_RED), _reading(checks=_LINT_RED)],
        judge_answer=judge_answer,
    )
    try:
        assert await harness.tick() is False, "the first reading delivers and records the red"
        assert harness.monitor.delivered_failed_checks == [_LINT_RED[0].name]
        # The same red lane, tick after tick: a PR under repair, nothing new to show.
        assert await harness.tick() is True, "the same failing set the owner saw stays quiet"
        assert await harness.tick() is True, "and keeps staying quiet"
        assert len(harness.delivered) == 1
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_a_second_check_going_red_wakes_even_with_one_already_seen(tmp_path, monkeypatch):
    """A new red lane wakes even while an old red lane the owner saw is still red."""
    harness = _Harness(
        tmp_path,
        monkeypatch,
        [_reading(checks=_LINT_RED), _reading(checks=_LINT_RED), _reading(checks=_BOTH_RED)],
        judge_answer=True,
    )
    try:
        assert await harness.tick() is False, "first delivery records lint red"
        assert await harness.tick() is True, "the same lint red is quiet"
        assert await harness.tick() is False, "a SECOND lane going red is a new blocker"
        assert len(harness.delivered) == 2
    finally:
        harness.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("judge_answer", [True, None], ids=["judge-quiet", "no-judge"])
async def test_a_newly_conflicting_branch_wakes(tmp_path, monkeypatch, judge_answer):
    """ACCEPTANCE (4c): the PR going CONFLICTING since the last delivery -> WAKE."""
    harness = _Harness(
        tmp_path,
        monkeypatch,
        [_reading(mergeability="MERGEABLE"), _reading(merge_state="DIRTY")],
        judge_answer=judge_answer,
    )
    try:
        assert await harness.tick() is False, "the first reading (mergeable) is delivered"
        assert harness.monitor.delivered_conflicting is False
        assert await harness.tick() is False, "a branch that just started conflicting wakes"
        assert harness.monitor.delivered_conflicting is True
        assert len(harness.delivered) == 2
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_a_newly_conflicting_wake_is_the_overrides_doing(tmp_path, monkeypatch):
    """ACCEPTANCE (4c, fix removed): with the override stubbed off, a judge QUIET suppresses."""
    harness = _Harness(
        tmp_path,
        monkeypatch,
        [_reading(mergeability="MERGEABLE"), _reading(merge_state="DIRTY")],
        judge_answer=True,
    )
    monkeypatch.setattr(_gate, "_newly_blocked", lambda _monitor: [])
    try:
        assert await harness.tick() is False, "the first reading is delivered"
        assert await harness.tick() is True, "without the override a quiet judge sleeps through it"
        assert len(harness.delivered) == 1
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_a_still_conflicting_branch_stays_quiet(tmp_path, monkeypatch):
    """A conflict the owner was already shown is not re-woken every tick."""
    harness = _Harness(
        tmp_path,
        monkeypatch,
        [
            _reading(merge_state="DIRTY"),
            _reading(merge_state="DIRTY"),
            _reading(merge_state="DIRTY"),
        ],
        judge_answer=True,
    )
    try:
        assert await harness.tick() is False, "the first conflicting reading delivers"
        assert harness.monitor.delivered_conflicting is True
        assert await harness.tick() is True, "the same conflict is quiet"
        assert await harness.tick() is True, "and keeps staying quiet"
        assert len(harness.delivered) == 1
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
async def test_a_same_pr_re_aim_keeps_the_delivered_basis(tmp_path, monkeypatch, change):
    """Re-aiming a loop that keeps the SAME pull request must not re-wake on an old red.

    The blockers the owner was already shown belong to the pull request, not to the
    brief, so a reworded instruction or a replaced brief on the same PR keeps them. A
    still-red check is then not new, and the new brief's QUIET stands -- the regression
    the Opus review caught, where clearing the basis fired a false "not present at the
    last delivery" wake.
    """
    harness = _Harness(
        tmp_path,
        monkeypatch,
        [_reading(checks=_LINT_RED), _reading(checks=_LINT_RED), _reading(checks=_LINT_RED)],
        judge_answer=True,
    )
    try:
        assert await harness.tick() is False, "first delivery records lint red"
        assert harness.monitor.delivered_failed_checks == [_LINT_RED[0].name]
        monitor = harness.monitor
        await harness.service.update(harness.loop.id, **change)
        assert harness.loop.monitor is monitor, "the same subject keeps its monitor"
        assert harness.monitor.delivered_failed_checks == [
            _LINT_RED[0].name
        ], "a same-PR re-aim keeps the delivered-blocker basis"
        assert await harness.tick() is True, "so the same lint red is not a new blocker"
        assert len(harness.delivered) == 1
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_a_retarget_to_a_different_pr_starts_from_an_empty_basis(tmp_path, monkeypatch):
    """A true retarget replaces the monitor, whose basis starts empty -- a first look."""
    harness = _Harness(
        tmp_path,
        monkeypatch,
        [_reading(checks=_LINT_RED)],
        judge_answer=True,
    )
    try:
        assert await harness.tick() is False, "first delivery records lint red on PR 42"
        old_monitor = harness.monitor
        await harness.service.update(
            harness.loop.id,
            message="watch https://github.com/acme/widgets/pull/99 until green",
        )
        # A different PR gets a fresh MonitorState, so the basis is empty, not carried.
        assert harness.loop.monitor is not old_monitor, "a new subject gets a new monitor"
        assert harness.monitor.delivered_failed_checks == []
        assert harness.monitor.delivered_conflicting is False
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_a_judge_wake_is_left_alone(tmp_path, monkeypatch):
    """The override only ever TURNS a quiet into a wake; it never touches a judge WAKE."""
    harness = _Harness(
        tmp_path,
        monkeypatch,
        [_reading(), _reading(checks=_LINT_RED)],
        judge_answer=False,  # the judge itself wakes
    )
    try:
        assert await harness.tick() is False
        assert await harness.tick() is False
        assert len(harness.delivered) == 2
    finally:
        harness.stop()


def test_a_same_pr_re_aim_mid_flight_still_records_the_blocker_basis():
    """A reword that lands between basis capture and the record must not strand the basis.

    The delivered-blocker basis is subject-dependent, so it is recorded whenever the
    monitor identity still matches -- even if the instruction or brief was reworded on
    the same PR while the turn was in flight. The short-reading hold is
    question-dependent and is NOT recorded in that case. Pins the GPT 6.1 finding:
    guarding the basis on message/spec left it stale after a same-PR mid-flight reword.
    """
    monitor = MonitorState(
        kind="gh-pr",
        target="acme/widgets#42",
        objective="review_ready",
        created_ts=1_000.0,
    )
    loop = NudgeLoop(
        id="mid-flight",
        slot_key="chat-1-123",
        message="watch https://github.com/acme/widgets/pull/42, reworded",
        idle_secs=300,
        monitor=monitor,
        gate=True,
    )
    # The basis was captured before dispatch under the OLD message; the loop's message
    # changed while the turn was in flight (a same-PR reword), monitor unchanged.
    basis = (
        monitor,
        "watch https://github.com/acme/widgets/pull/42",  # the pre-reword message
        {},
        "deadbeefdeadbeef",  # a short-reading digest that must NOT be recorded
        [_LINT_RED[0].name],
        True,
    )
    _gate._record_delivered_reading(loop, basis)
    assert monitor.delivered_failed_checks == [_LINT_RED[0].name], "the basis is recorded"
    assert monitor.delivered_conflicting is True
    assert monitor.short_reading_digest == "", "the question-dependent hold is NOT recorded"


def test_a_true_retarget_mid_flight_records_nothing_to_the_old_monitor():
    """When the monitor itself was replaced, nothing is written to the old record."""
    old = MonitorState(
        kind="gh-pr", target="acme/widgets#42", objective="review_ready", created_ts=1_000.0
    )
    new = MonitorState(
        kind="gh-pr", target="acme/widgets#99", objective="review_ready", created_ts=1_000.0
    )
    loop = NudgeLoop(
        id="retargeted",
        slot_key="chat-1-123",
        message="watch https://github.com/acme/widgets/pull/99",
        idle_secs=300,
        monitor=new,
        gate=True,
    )
    basis = (old, loop.message, {}, "", [_LINT_RED[0].name], True)
    _gate._record_delivered_reading(loop, basis)
    assert old.delivered_failed_checks == [], "the old subject's record is untouched"
    assert new.delivered_failed_checks == [], "and nothing leaks onto the new monitor"


@pytest.mark.asyncio
async def test_the_wake_notice_names_the_new_blocker(tmp_path, monkeypatch):
    """A woken owner is told which check appeared, not merely that they were woken."""
    harness = _Harness(
        tmp_path,
        monkeypatch,
        [_reading(), _reading(checks=_LINT_RED)],
        judge_answer=True,
    )
    try:
        await harness.tick()
        await harness.tick()
        assert any(_LINT_RED[0].name in line for line in harness.notices)
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_the_conflict_wake_notice_does_not_claim_a_check(tmp_path, monkeypatch):
    """When the only new blocker is a conflict, the notice must not say a check failed.

    The notice lead-in is neutral and each blocker names its own kind, so a conflict
    wake reads as a conflict, not a (non-existent) failing check.
    """
    harness = _Harness(
        tmp_path,
        monkeypatch,
        [_reading(mergeability="MERGEABLE"), _reading(merge_state="DIRTY")],
        judge_answer=True,
    )
    try:
        await harness.tick()
        await harness.tick()
        conflict_notices = [n for n in harness.notices if "conflict" in n.lower()]
        assert conflict_notices, "the conflict wake emits a notice naming the conflict"
        assert all(
            "check" not in n.lower() for n in conflict_notices
        ), "a conflict-only wake must not claim a check was failing"
    finally:
        harness.stop()


@pytest.mark.asyncio
async def test_the_basis_is_anchored_to_deliveries_not_every_reading(tmp_path, monkeypatch):
    """A blocker is "new" against what the owner was DELIVERED, not against the last tick.

    A lint failure the owner saw at delivery, a held-quiet green tick, then the same
    lint failure again: the owner never received a turn saying it cleared, so the
    return is not news. The basis moves only when a turn lands, which is exactly what
    keeps a PR under repair from re-waking every time a lane flaps.
    """
    harness = _Harness(
        tmp_path,
        monkeypatch,
        [_reading(checks=_LINT_RED), _reading(), _reading(checks=_LINT_RED)],
        judge_answer=True,
    )
    try:
        assert await harness.tick() is False, "first delivery: lint red recorded"
        assert harness.monitor.delivered_failed_checks == [_LINT_RED[0].name]
        assert await harness.tick() is True, "a green tick the judge holds delivers nothing"
        assert harness.monitor.delivered_failed_checks == [
            _LINT_RED[0].name
        ], "so the basis still names lint -- the owner was never told it cleared"
        assert await harness.tick() is True, "the same lint red is not new against that basis"
        assert len(harness.delivered) == 1
    finally:
        harness.stop()


# ---- The basis survives the record, and an unreadable one fails safe. ----


def test_the_delivered_basis_survives_a_round_trip():
    state = MonitorState(
        kind="gh-pr",
        target="acme/widgets#42",
        objective="review_ready",
        created_ts=1_000.0,
        delivered_failed_checks=("CI / Lint", "CI / Types"),
        delivered_conflicting=True,
    )
    loaded = monitor_state_from_dict(monitor_state_to_dict(state))
    assert loaded.delivered_failed_checks == ["CI / Lint", "CI / Types"]
    assert loaded.delivered_conflicting is True


@pytest.mark.parametrize(
    "fields",
    [
        {"delivered_failed_checks": "CI / Lint"},
        {"delivered_failed_checks": [1, 2]},
        {"delivered_failed_checks": 7},
        {"delivered_conflicting": "yes"},
        {"delivered_conflicting": 1},
    ],
)
def test_an_unreadable_basis_loads_as_the_empty_basis(fields):
    """An unreadable basis is empty, so every live blocker reads as new -- the safe way."""
    raw = monitor_state_to_dict(
        MonitorState(
            kind="gh-pr", target="acme/widgets#42", objective="review_ready", created_ts=1.0
        )
    )
    raw.update(fields)
    loaded = monitor_state_from_dict(raw)
    assert loaded.delivered_failed_checks == []
    assert loaded.delivered_conflicting is False


def test_a_record_written_before_the_basis_loads_empty():
    raw = monitor_state_to_dict(
        MonitorState(
            kind="gh-pr", target="acme/widgets#42", objective="review_ready", created_ts=1.0
        )
    )
    raw.pop("delivered_failed_checks", None)
    raw.pop("delivered_conflicting", None)
    loaded = monitor_state_from_dict(raw)
    assert loaded.delivered_failed_checks == []
    assert loaded.delivered_conflicting is False


def test_reading_blockers_reads_the_published_facts():
    """The helper the gate measures with reads the same typed facts the classifier does."""
    facts = _reading(checks=_BOTH_RED, merge_state="DIRTY").as_facts()
    failed, conflicting = _gate._reading_blockers(facts)
    assert set(failed) == {_LINT_RED[0].name, _TYPE_RED[0].name}
    assert conflicting is True
    # A mergeable, green reading carries no blocker.
    failed, conflicting = _gate._reading_blockers(_reading().as_facts())
    assert failed == []
    assert conflicting is False
    # An unreadable reading is no evidence of a blocker.
    assert _gate._reading_blockers(None) == ([], False)
    assert _gate._reading_blockers("not a mapping") == ([], False)
