"""AutoNudgeService tests: settling a watch whose subject is terminal, and retargeting one.

Revalidation before deactivation, the owed terminal turn, write failures on the way, and
how a retarget drops the old subject's claims and history.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from autonudge_helpers import _enable  # noqa: F401
from autonudge_helpers import _structured_monitor

from kiro_crew import autonudge as _an
from kiro_crew.autonudge import AutoNudgeService, NudgeLoop

#: Lost-run ceiling for synchronization the test itself must release. It is a
#: hang guard whose expiry fails at the awaited line, never a performance
#: assertion, and is at most half the repository's ``--timeout=120``.
_LOST_RUN_SECS = 30.0


def _writes_the_settlement(payload, loop_id: str) -> bool:
    """Whether a snapshot payload carries *loop_id*'s staged settlement row.

    The settlement stages the deactivated row and writes it while the live loop is
    still active, so the write is told apart from the marker writes by the row it
    carries, never by live state.
    """
    if payload is None:
        return False
    return any(row.get("id") == loop_id and row.get("active") is False for row in payload["loops"])


def _reading(state: str = "MERGED", *, status: str = "ok", head: str = "a" * 40):
    """One published reading, in the shape the fetcher puts on the probe.

    The terminal decision is the core's, taken from a typed fact, so a test that
    wants a terminal tick has to supply the FACT rather than a verdict. Pinning only
    the kernel verdict would test a state the code cannot reach: the fetcher emits no
    terminal observation, so no kernel verdict is ever TERMINAL.
    """
    from kiro_crew.probes import gh_pr

    return gh_pr.PrObservation(
        repo="acme/widgets",
        pr=42,
        host="github.com",
        status=status,
        observed_at=1_000.0,
        state=state,
        merged_at="2026-09-25T00:00:00Z" if state == "MERGED" else "",
        head=head,
    )


def _terminal_poll(state: str = "MERGED", body: str = "read"):
    """A poll fake that publishes a terminal reading, the way ``observe`` does."""

    def _poll(identity, message, probe):
        probe.observation = _reading(state)
        # The real gh-pr probe is a FETCHER: it returns ``observations=[]`` and never
        # attributes a TERMINAL key, so the kernel's verdict carries no keys on this
        # path. Emitting a key here would feed the code a signal the real probe never
        # produces and mask the merged-versus-blocked decision, which for a pull
        # request comes from ``observation.merged``, not from ``verdict.keys``.
        return _an.irq.Verdict(_an.irq.Outcome.QUIET, body)

    return _poll


def _live_poll(outcome=None, body: str = "read", status: str = "ok"):
    """A poll fake that publishes a LIVE reading: open, nothing ended."""
    resolved = _an.irq.Outcome.QUIET if outcome is None else outcome

    def _poll(identity, message, probe):
        probe.observation = _reading("OPEN", status=status)
        return _an.irq.Verdict(resolved, body)

    return _poll


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state, expected",
    [
        ("MERGED", "success"),
        ("CLOSED", "blocked"),
    ],
)
async def test_only_a_merged_subject_is_recorded_as_a_success(
    tmp_path, monkeypatch, state, expected
):
    """Reaching the end is not the same as ending well.

    A subject CLOSED WITHOUT MERGING is a watch that stopped on a question --
    reopen or abandon -- and recording it as a success tells the user "no action
    needed" about the one case that needs them most. The reading distinguishes the
    two, so the core reads its typed facts rather than delivered prose.
    """
    import kiro_crew.autonudge as _an

    async def on_fire(loop):
        return True

    monkeypatch.setattr(_an.irq, "poll", _terminal_poll(state))
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor17",
        slot_key="chat-1-123",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    try:
        assert await service._monitor_tick_is_quiet(loop) is True
        assert loop.monitor is not None
        assert loop.monitor.outcome is not None
        assert loop.monitor.outcome.value == expected
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_the_marker_writes_do_not_release_the_lock_mid_write(tmp_path, monkeypatch):
    """The terminal marker writes use the writer that never releases ``_lock`` mid-write.

    ``_persist_locked`` releases ``_lock`` on cancel while the executor write is in
    flight, so a stale marker snapshot could land on top of a newer update.
    """
    import kiro_crew.autonudge as _an

    async def on_fire(loop):
        return True

    monkeypatch.setattr(
        _an.irq,
        "poll",
        _terminal_poll(),
    )
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor27",
        slot_key="slack:C0123456:1700000000.1",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    releasing = {"n": 0}
    real_releasing = service._persist_locked

    async def _count_releasing(**kwargs):
        releasing["n"] += 1
        await real_releasing(**kwargs)

    monkeypatch.setattr(service, "_persist_locked", _count_releasing)

    try:
        assert await service._monitor_tick_is_quiet(loop) is False
        assert loop.monitor is not None
        assert loop.monitor.terminal_pending == "success", "the flow must reach both markers"
        assert (
            releasing["n"] == 0
        ), "no marker may go through the writer that releases the lock mid-write"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_the_revalidation_does_not_consume_the_ticks_dedupe_identity(tmp_path, monkeypatch):
    """A poll whose answer is discarded must not be able to swallow a signal.

    ``identity`` is the kernel's dedupe key. Sharing the tick's key would mark a fresh
    comment seen here as reported, and the next real tick would read it as unchanged.
    """
    import kiro_crew.autonudge as _an

    seen: list[str] = []

    async def on_fire(loop):
        return True

    def _record_identity(identity, *_a, **_k):
        seen.append(identity)
        return _an.irq.Verdict(_an.irq.Outcome.WAKE, "reopened with a comment", ())

    monkeypatch.setattr(_an.irq, "poll", _record_identity)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    monitor = _structured_monitor(kind="gh-pr", target="acme/widgets#42")
    monitor.terminal_pending = "success"
    loop = NudgeLoop(
        id="monitor44",
        slot_key="slack:C123:1700000000.1",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=monitor,
        gate=True,
    )
    service._loops[loop.id] = loop

    try:
        await service._run_fire_cycle(loop)
        assert seen, "the settlement revalidated"
        tick_identity = f"{loop.id}:github.com"
        assert tick_identity not in seen, "the discarded poll must not spend the tick's key"
        assert all(i.startswith(tick_identity) and i != tick_identity for i in seen)
        assert loop.active is True, "and a reopened subject is not settled"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_changed_terminal_classification_does_not_settle(tmp_path, monkeypatch):
    """A pull request can be closed, reopened and MERGED inside one channel turn.

    Settling under the stale "blocked" marker would announce the merge as an unmerged
    close. A changed classification drops the debt and keeps the watch alive.
    """
    import kiro_crew.autonudge as _an

    async def on_fire(loop):
        return True

    def _merged_now(*_a, **_k):
        return _an.irq.Verdict(_an.irq.Outcome.TERMINAL, "merged after reopen", ("merged",))

    monkeypatch.setattr(_an.irq, "poll", _merged_now)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    monitor = _structured_monitor(kind="gh-pr", target="acme/widgets#42")
    monitor.terminal_pending = "blocked"
    loop = NudgeLoop(
        id="monitor43",
        slot_key="slack:C123:1700000000.1",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=monitor,
        gate=True,
    )
    service._loops[loop.id] = loop

    try:
        await service._run_fire_cycle(loop)
        assert loop.monitor is not None
        assert loop.active is True, "a different ending is not the owed one"
        assert loop.monitor.outcome is None, "so no outcome is persisted from the stale marker"
        assert loop.monitor.terminal_pending == "", "and the owed one is dropped, not kept"
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("state,stays_alive", [("OPEN", True), ("MERGED", False)])
async def test_a_settlement_revalidates_before_it_deactivates(
    tmp_path, monkeypatch, state, stays_alive
):
    """The settlement re-asks before it deactivates; only a fresh terminal reading settles.

    A channel turn runs inline and can take minutes, long enough for a reopen, and the
    other reopen guards run only on the next tick.
    """
    import kiro_crew.autonudge as _an

    async def on_fire(loop):
        return True

    def _poll(identity, message, probe):
        probe.observation = _reading(state)
        return _an.irq.Verdict(_an.irq.Outcome.QUIET, "state")

    monkeypatch.setattr(_an.irq, "poll", _poll)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    monitor = _structured_monitor(kind="gh-pr", target="acme/widgets#42")
    monitor.terminal_pending = "success"
    loop = NudgeLoop(
        id="monitor41",
        slot_key="slack:C123:1700000000.1",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=monitor,
        gate=True,
    )
    service._loops[loop.id] = loop

    try:
        await service._run_fire_cycle(loop)
        assert loop.monitor is not None
        assert loop.active is stays_alive
        assert loop.monitor.terminal_pending == "", "the debt is resolved either way"
        if stays_alive:
            assert loop.monitor.outcome is None, "a live subject keeps no settled outcome"
        else:
            assert loop.monitor.outcome is not None
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_an_unobservable_subject_does_not_get_settled(tmp_path, monkeypatch):
    """Absence of evidence must not retire a watch.

    A failed fetch or probe cannot CONFIRM the subject is finished.
    """
    import kiro_crew.autonudge as _an

    async def on_fire(loop):
        return True

    def _explode(*_a, **_k):
        raise RuntimeError("gh unavailable")

    monkeypatch.setattr(_an.irq, "poll", _explode)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    monitor = _structured_monitor(kind="gh-pr", target="acme/widgets#42")
    monitor.terminal_pending = "success"
    loop = NudgeLoop(
        id="monitor42",
        slot_key="slack:C123:1700000000.1",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=monitor,
        gate=True,
    )
    service._loops[loop.id] = loop

    try:
        await service._run_fire_cycle(loop)
        assert loop.active is True, "an unobserved subject is not a finished one"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_retarget_does_not_overwrite_a_future_version_monitor(tmp_path):
    """A record this gateway cannot read must not be replaced by it either.

    The future-version monitor belongs to the newer gateway. The revival guard runs
    after the retarget, so the retarget needs the same rule.
    """
    from kiro_crew.monitoring.models import MONITOR_STATE_VERSION

    async def on_fire(loop):
        return True

    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    monitor = _structured_monitor(kind="gh-pr", target="acme/widgets#42")
    monitor.version = MONITOR_STATE_VERSION + 1
    loop = NudgeLoop(
        id="monitor39",
        slot_key="chat-1-123",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=monitor,
        gate=True,
    )
    service._loops[loop.id] = loop

    try:
        await service.update(loop.id, message="watch https://github.com/acme/widgets/pull/99")
        assert loop.monitor is monitor, "the newer gateway's record must survive"
        assert loop.monitor.target == "acme/widgets#42"
        assert loop.monitor.version == MONITOR_STATE_VERSION + 1
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_retarget_releases_the_floor_claim_too(tmp_path):
    """A floor claim earned by the OLD subject must not charge the new one.

    An in-flight floor delivery landing after a retarget would charge ``floor_ticks`` on
    a monitor that never hit a floor.
    """

    async def on_fire(loop):
        return True

    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor40",
        slot_key="chat-1-123",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop
    service._pending_floor_tick.add(loop.id)

    try:
        await service.update(loop.id, message="watch https://github.com/acme/widgets/pull/99")
        assert loop.monitor is not None and loop.monitor.target == "acme/widgets#99"
        assert loop.id not in service._pending_floor_tick, "the old subject's claim is gone"
        await service._run_fire_cycle(loop)
        assert loop.monitor.floor_ticks == 0, "so the new monitor is not charged for it"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_retarget_drops_the_labelled_judge_history(tmp_path):
    """Labels earned answering the old instruction must not judge the new one.

    A message-only update resets the judge history too: a reworded instruction is a
    changed question whether or not the subject changed.
    """

    service = AutoNudgeService(base_dir=tmp_path)
    loop = NudgeLoop(
        id="monitor41",
        slot_key="chat-1-123",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    loop.judge_quiet_streak = 4
    loop.judge_cursors = {"gh-pr": "cursor-42"}
    loop.judge_last_verdict = {"outcome": "quiet", "age_s": 12.0}
    loop.judge_recent_verdicts = [
        {"outcome": "quiet", "at": 1.0, "suppressed": True, "answered": True, "missed": True},
        {"outcome": "wake", "at": 2.0, "delivered": True, "answered": True, "owner_acted": True},
    ]
    service._loops[loop.id] = loop

    try:
        await service.update(loop.id, message="watch https://github.com/acme/widgets/pull/99")

        assert loop.judge_recent_verdicts == [], "the old instruction's labels are gone"
        assert loop.judge_quiet_streak == 0
        assert loop.judge_cursors == {}
        assert loop.judge_last_verdict == {}
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_failed_clear_write_leaves_the_debt_cleared(tmp_path, monkeypatch):
    """A failed clearing write does not restore a debt a live observation disproved.

    Restoring it would let the next turn settle a live subject. Memory saying "no debt"
    keeps the watch running; after a restart the forced re-observation clears it again.
    """
    import kiro_crew.autonudge as _an

    async def on_fire(loop):
        return True

    def _verdict(identity, message, probe):
        probe.observation = _reading("OPEN")
        return _an.irq.Verdict(_an.irq.Outcome.WAKE, "open again with news", ())

    monkeypatch.setattr(_an.irq, "poll", _verdict)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    monitor = _structured_monitor(kind="gh-pr", target="acme/widgets#42")
    monitor.terminal_pending = "success"
    loop = NudgeLoop(
        id="monitor36",
        slot_key="slack:C123:1700000000.1",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=monitor,
        gate=True,
    )
    service._loops[loop.id] = loop

    real_write = service._write_monitor_snapshot_locked
    calls: list[int] = []

    async def _explode_after_the_first(*a, **k):
        # The doubt marker's own durable write comes first, and when THAT fails the
        # tick fires without polling -- so failing every write would never reach the
        # clearing this test is about.
        calls.append(1)
        if len(calls) == 1:
            return await real_write(*a, **k)
        raise OSError("disk full")

    monkeypatch.setattr(service, "_write_monitor_snapshot_locked", _explode_after_the_first)

    try:
        await service._monitor_tick_is_quiet(loop)
        assert loop.monitor is not None
        assert loop.monitor.terminal_pending == "", "a disproved debt must not come back"
        await service._run_fire_cycle(loop)
        assert loop.active is True, "so the delivered turn cannot settle a live subject"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_cancelled_settlement_still_counts_the_delivered_turn(tmp_path, monkeypatch):
    """A cancelled settlement write still counts the turn that was delivered.

    The terminal write re-raises a cancellation, so the delivery accounting runs before
    the settlement.
    """

    import kiro_crew.autonudge as _an

    async def on_fire(loop):
        return True

    def _still_terminal(identity, message, probe):
        # The settlement revalidates before deactivating; confirm the terminal, or the
        # settlement is skipped and the cancellation never happens.
        probe.observation = _reading("MERGED")
        return _an.irq.Verdict(_an.irq.Outcome.QUIET, "merged")

    monkeypatch.setattr(_an.irq, "poll", _still_terminal)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    monitor = _structured_monitor(kind="gh-pr", target="acme/widgets#42")
    monitor.terminal_pending = "success"
    loop = NudgeLoop(
        id="monitor35",
        slot_key="slack:C123:1700000000.1",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=monitor,
        gate=True,
    )
    service._loops[loop.id] = loop
    before = loop.cycle_count

    writes = {"n": 0}
    real_write = service._write_monitor_snapshot_locked

    async def _cancel_mid_write(*_a, **_k):
        writes["n"] += 1
        if writes["n"] == 1:
            return await real_write(*_a, **_k)
        raise asyncio.CancelledError()

    monkeypatch.setattr(service, "_write_monitor_snapshot_locked", _cancel_mid_write)

    try:
        with pytest.raises(asyncio.CancelledError):
            await service._run_fire_cycle(loop)
        assert loop.cycle_count == before + 1, "the delivered turn must be counted"
        assert loop.last_fire_ts > 0, "and its timestamp recorded"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_terminal_debt_is_re_observed_rather_than_spending_a_free_tick(tmp_path, monkeypatch):
    """The follow-up allowance must not jump over the reopened-subject check.

    A subject with terminal debt has no work in progress to protect, and the clearing
    for a reopen lives after the poll, so the debt is re-observed.
    """
    import kiro_crew.autonudge as _an

    polls: list[str] = []

    async def on_fire(loop):
        return True

    def _verdict(identity, message, probe):
        polls.append("polled")
        probe.observation = _reading("OPEN")
        return _an.irq.Verdict(_an.irq.Outcome.QUIET, "open again", ())

    monkeypatch.setattr(_an.irq, "poll", _verdict)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    monitor = _structured_monitor(kind="gh-pr", target="acme/widgets#42")
    monitor.terminal_pending = "success"
    monitor.followup_ticks = 1
    loop = NudgeLoop(
        id="monitor34",
        slot_key="slack:C123:1700000000.1",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=monitor,
        gate=True,
    )
    service._loops[loop.id] = loop

    try:
        await service._monitor_tick_is_quiet(loop)
        assert polls == ["polled"], "the debt must be re-observed, not assumed"
        assert loop.monitor is not None
        assert loop.monitor.terminal_pending == "", "and a live verdict clears it"
        assert loop.monitor.followup_ticks == 1, "the allowance is not spent on this path"
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,expect_cleared",
    [
        ("QUIET", True),
        ("WAKE", True),
        ("FALLBACK", False),
    ],
)
async def test_a_reopened_subject_clears_the_owed_terminal_turn(
    tmp_path, monkeypatch, outcome, expect_cleared
):
    """A channel loop's terminal debt is durable, so a reopened subject clears it.

    Otherwise the next delivered turn would deactivate a live watch. A FALLBACK means
    nothing was observed, so it keeps the debt rather than lose the terminal news.
    """
    import kiro_crew.autonudge as _an

    async def on_fire(loop):
        return True

    def _verdict(identity, message, probe):
        probe.observation = _reading("OPEN")
        return _an.irq.Verdict(getattr(_an.irq.Outcome, outcome), "still open", ())

    monkeypatch.setattr(_an.irq, "poll", _verdict)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    monitor = _structured_monitor(kind="gh-pr", target="acme/widgets#42")
    monitor.terminal_pending = "blocked"
    loop = NudgeLoop(
        id="monitor32",
        slot_key="slack:C123:1700000000.1",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=monitor,
        gate=True,
    )
    service._loops[loop.id] = loop

    try:
        assert _an.is_channel_key(loop.slot_key), "the deferral only applies to a channel loop"
        await service._monitor_tick_is_quiet(loop)
        if expect_cleared:
            assert loop.monitor is not None and loop.monitor.terminal_pending == ""
            on_disk = json.loads((tmp_path / "autonudge.json").read_text(encoding="utf-8"))
            stored = [row for row in on_disk["loops"] if row["id"] == loop.id][0]
            assert stored["monitor"]["terminal_pending"] == "", "and the clearing must be durable"
        else:
            assert loop.monitor is not None and loop.monitor.terminal_pending == "blocked"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_failed_retarget_write_hands_the_wake_claim_back(tmp_path, monkeypatch):
    """A failed retarget write hands back the wake claim the retarget dropped.

    The retarget did not happen, so the claim belongs to the loop again.
    """

    async def on_fire(loop):
        return True

    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor30",
        slot_key="chat-1-123",
        message="watch https://github.com/acme/widgets/pull/42 for failures",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop
    # The claim carries the kernel's wake text, so the rollback has to restore BOTH:
    # a claim handed back without its briefs leaves the retried delivery announcing a
    # cycle and naming nothing.
    service._pending_monitor_wake[loop.id] = "[wake] acme/widgets#42 went red"

    def _explode(_payload):
        raise OSError("disk full")

    monkeypatch.setattr(service, "_write_state", _explode)

    try:
        with pytest.raises(OSError):
            await service.update(
                loop.id, message="watch https://github.com/acme/widgets/pull/99 for failures"
            )
        assert loop.id in service._pending_monitor_wake, "the claim must come back"
        assert (
            service.pending_wake_briefs(loop.id) == "[wake] acme/widgets#42 went red"
        ), "and the wake text it was holding"
        assert loop.monitor is not None
        assert loop.monitor.target == "acme/widgets#42", "and the old subject with it"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_retarget_during_lock_acquisition_does_not_crash_the_settlement(
    tmp_path, monkeypatch
):
    """Waiting for the lock is an await, so the monitor can vanish in that gap.

    An instruction edited to name no subject clears ``loop.monitor``; dereferencing it
    would raise and leave the loop ACTIVE with no timer.
    """
    import kiro_crew.autonudge as _an

    async def on_fire(loop):
        return True

    monkeypatch.setattr(
        _an.irq,
        "poll",
        _terminal_poll(),
    )
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor29",
        slot_key="slack:C0123456:1700000000.1",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    real_acquire = service._acquire_mutation_lock

    async def _clear_the_monitor_then_acquire(loop_id, **kwargs):
        lock = await asyncio.wait_for(real_acquire(loop_id, **kwargs), timeout=_LOST_RUN_SECS)
        if loop.monitor is not None and (
            loop.monitor.terminal_pending or loop.monitor.terminal_delivered
        ):
            # Stands in for a retarget landing while we waited for this lock.
            loop.monitor = None
        return lock

    monkeypatch.setattr(service, "_acquire_mutation_lock", _clear_the_monitor_then_acquire)

    try:
        await service._timer(loop, delay=0)  # must not raise
        assert loop.monitor is None
        assert loop.active is True, "the retargeted loop must keep its timer"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_failed_settlement_write_leaves_the_terminal_turn_owed(tmp_path, monkeypatch):
    """Announce only what the record holds: persist the settlement before the emit.

    A failed write after the emit would report a finish the record lacks, and a restart
    would deliver the final turn twice.
    """
    import kiro_crew.autonudge as _an

    events: list[str] = []

    async def on_fire(loop):
        return True

    monkeypatch.setattr(
        _an.irq,
        "poll",
        _terminal_poll(),
    )
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor26",
        slot_key="slack:C0123456:1700000000.1",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop
    monkeypatch.setattr(service, "_emit", lambda event, _loop: events.append(event))

    real_writer = service._write_monitor_snapshot_locked
    settlement_write_failed = False

    async def _fail_the_settlement_write(payload=None, **kwargs):
        nonlocal settlement_write_failed
        # Two marker writes share this writer, so pick the settlement by the state only
        # it has: its staged row deactivates the loop. Counting calls hits a marker.
        if _writes_the_settlement(payload, loop.id):
            settlement_write_failed = True
            raise OSError("disk full")
        await real_writer(payload, **kwargs)

    monkeypatch.setattr(service, "_write_monitor_snapshot_locked", _fail_the_settlement_write)

    try:
        await service._timer(loop, delay=0)
        assert loop.monitor is not None
        assert "expired" not in events, "a finish must not be announced unpersisted"
        assert loop.monitor.terminal_delivered == "success", "the settlement stays owed"
        assert loop.monitor.outcome is None
        assert loop.active is True, "and the watch stays live so it retries"
        assert settlement_write_failed, "the staged settlement selector must fire"
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("delivers", [True, False])
async def test_the_owed_terminal_turn_settles_only_once_it_lands(tmp_path, monkeypatch, delivers):
    """Settle on confirmed delivery, and stay live when it is refused.

    A busy thread is the ordinary case, so a refused final fire must leave the loop
    watchable: the probe re-raises a terminal state on every tick (it is not
    deduped), which is what makes the retry converge instead of announcing forever.
    """
    import kiro_crew.autonudge as _an

    async def on_fire(loop):
        return delivers

    monkeypatch.setattr(
        _an.irq,
        "poll",
        _terminal_poll(),
    )
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor25",
        slot_key="slack:C0123456:1700000000.1",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop
    locks: list[str] = []
    real_acquire = service._acquire_mutation_lock

    async def _tracking_acquire(loop_id, **kwargs):
        locks.append(loop_id)
        return await asyncio.wait_for(real_acquire(loop_id, **kwargs), timeout=_LOST_RUN_SECS)

    monkeypatch.setattr(service, "_acquire_mutation_lock", _tracking_acquire)

    try:
        await service._timer(loop, delay=0)
        assert loop.monitor is not None
        if delivers:
            assert loop.active is False, "the turn landed, so the watch closes"
            assert loop.monitor.outcome is not None
            assert loop.monitor.terminal_pending == ""
            assert locks == [loop.id], "the settlement must take the lock update takes"
        else:
            assert loop.active is True, "a refused turn must leave the watch alive"
            assert loop.monitor.outcome is None, "and nothing may be recorded as final"
            assert loop.monitor.terminal_pending == "success", "the debt is still owed"
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "slot_key, expect_quiet",
    [("chat-1-123", True), ("slack:C0123456:1700000000.1", False)],
)
async def test_a_channel_loop_gets_a_final_turn_but_a_dashboard_loop_does_not(
    tmp_path, monkeypatch, slot_key, expect_quiet
):
    """The expiry notification only reaches the dashboard bell.

    So a channel loop gets one final turn to report into its thread, and a dashboard
    loop does not pay for one.
    """
    import kiro_crew.autonudge as _an

    async def on_fire(loop):
        return True

    monkeypatch.setattr(
        _an.irq,
        "poll",
        _terminal_poll(),
    )
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor23",
        slot_key=slot_key,
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    try:
        assert await service._monitor_tick_is_quiet(loop) is expect_quiet
        assert loop.monitor is not None
        if expect_quiet:
            # Dashboard: the bell already carries the news, so the watch settles now.
            assert loop.active is False
            assert loop.monitor.outcome is not None
            assert loop.monitor.terminal_pending == ""
        else:
            # Channel: the news rides a delivered TURN, so the loop stays LIVE until it
            # lands; an inactive loop has nothing to re-arm when the thread is busy.
            # No outcome yet, so a restart here finds a plain live loop.
            assert loop.active is True
            assert loop.monitor.outcome is None
            assert loop.monitor.terminal_pending == "success"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_the_terminal_settlement_is_serialized_against_update(tmp_path, monkeypatch):
    """A retarget must not land a new subject onto a just-deactivated loop.

    The settlement takes the MAINTENANCE lock ``update`` holds; ``_lock`` alone is not
    the lock ``update`` contends for.
    """
    import kiro_crew.autonudge as _an

    async def on_fire(loop):
        return True

    monkeypatch.setattr(
        _an.irq,
        "poll",
        _terminal_poll(),
    )
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor22",
        slot_key="chat-1-123",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    held: list[str] = []
    real_acquire = service._acquire_mutation_lock

    async def _tracking_acquire(loop_id, **kwargs):
        held.append(loop_id)
        return await asyncio.wait_for(real_acquire(loop_id, **kwargs), timeout=_LOST_RUN_SECS)

    monkeypatch.setattr(service, "_acquire_mutation_lock", _tracking_acquire)

    try:
        assert await service._monitor_tick_is_quiet(loop) is True
        assert held == [loop.id], "the settlement must take the same lock update takes"
        assert loop.active is False
        assert loop.monitor is not None
        assert loop.monitor.outcome is not None
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_the_terminal_transition_is_on_disk_before_the_user_is_told(tmp_path, monkeypatch):
    """Announcing first would promise a finish the record does not have.

    If the durable write fails after the notification, the user has been told the
    watch finished while the stored loop still says it is running. The emit must
    therefore follow the persist -- while still preceding the deactivation, whose
    cancel reaches this very task.
    """
    import kiro_crew.autonudge as _an

    order: list[str] = []

    async def on_fire(loop):
        return True

    monkeypatch.setattr(
        _an.irq,
        "poll",
        _terminal_poll(),
    )
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    real_persist = service._write_monitor_snapshot_locked

    async def _tracking_persist(payload=None, **kwargs):
        order.append("persist")
        await real_persist(payload, **kwargs)

    monkeypatch.setattr(service, "_write_monitor_snapshot_locked", _tracking_persist)
    monkeypatch.setattr(service, "_emit", lambda event, loop: order.append(f"emit:{event}"))
    loop = NudgeLoop(
        id="monitor18",
        slot_key="chat-1-123",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    try:
        await service._monitor_tick_is_quiet(loop)
        assert "persist" in order and "emit:expired" in order
        assert order.index("persist") < order.index(
            "emit:expired"
        ), f"the finish was announced before it was durable: {order}"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_retarget_does_not_hand_the_old_wake_to_the_new_subject(tmp_path):
    """The claim is keyed by loop, so a replaced monitor would inherit it.

    A wake claimed while watching one subject, then retargeted mid-turn, would be
    charged to a monitor that has observed nothing -- and would grant it a
    follow-up allowance it never earned.
    """

    async def on_fire(loop):
        return True

    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor19",
        slot_key="chat-1-123",
        message="watch https://github.com/acme/widgets/pull/42 for failures",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop
    service._pending_monitor_wake[loop.id] = "[wake] acme/widgets#42 went red"

    try:
        await service.update(
            loop.id, message="watch https://github.com/acme/widgets/pull/99 for failures"
        )
        assert loop.monitor is not None
        assert loop.monitor.target == "acme/widgets#99", "the new subject must be bound"
        assert loop.id not in service._pending_monitor_wake
        assert (
            service.pending_wake_briefs(loop.id) == ""
        ), "the old subject's wake text must not reach the new one either"
        assert loop.monitor.wakes == 0
        assert loop.monitor.followup_ticks == 0
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_failed_terminal_write_keeps_the_watch_alive(tmp_path, monkeypatch):
    """A write failure must not take the timer down with the loop still active.

    ``_timer`` awaits the gate unguarded, so an escape would leave a dead watch that
    looks calm. The marks are undone and the tick fires instead.
    """
    import kiro_crew.autonudge as _an

    async def on_fire(loop):
        return True

    monkeypatch.setattr(
        _an.irq,
        "poll",
        _terminal_poll(),
    )
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)

    async def _explode(payload=None, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(service, "_write_monitor_snapshot_locked", _explode)
    loop = NudgeLoop(
        id="monitor20",
        slot_key="chat-1-123",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    try:
        assert await service._monitor_tick_is_quiet(loop) is False, "it must fire, not raise"
        assert loop.active is True, "the watch must stay live"
        assert loop.monitor is not None
        assert loop.monitor.outcome is None, "an uncommitted finish must not be recorded"
        assert loop.monitor.stopped_reason in (None, "")
        # The LOOP's reason too: the fallback delivery would persist a terminal tag on a
        # watch that is still running.
        assert loop.stopped_reason in (None, ""), "the loop must not stay tagged terminal"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_retarget_under_a_terminal_verdict_is_not_settled(tmp_path, monkeypatch):
    """An old subject's finish must not deactivate a watch that has moved on.

    The verdict is about the monitor that was observed. If a retarget replaced it
    while the probe was in flight, the new subject has been observed by nothing,
    so settling the loop on that verdict would stop a live watch.
    """
    import kiro_crew.autonudge as _an

    async def on_fire(loop):
        return True

    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor21",
        slot_key="chat-1-123",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    def _retarget_then_terminal(*_a, **_k):
        loop.monitor = _structured_monitor(kind="gh-pr", target="acme/widgets#99")
        return _an.irq.Verdict(_an.irq.Outcome.TERMINAL, "merged", ("merged",))

    monkeypatch.setattr(_an.irq, "poll", _retarget_then_terminal)

    try:
        assert await service._monitor_tick_is_quiet(loop) is False
        assert loop.active is True, "the retargeted watch must stay live"
        assert loop.monitor is not None
        assert loop.monitor.outcome is None
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("write_secs", [0.0, 0.5], ids=["fast-disk", "slow-disk"])
async def test_a_terminal_watch_notifies_even_though_it_cancels_its_own_timer(
    tmp_path, monkeypatch, write_secs
):
    """The finish must reach the user, not be lost to a self-cancel.

    ``update`` cancels this timer from a separate shielded task, so the notification
    after the await can be dropped. It follows two fsync'd writes, slow on Windows
    runners; ``slow-disk`` holds each write so a fixed wait would misread it.
    """
    import kiro_crew.autonudge as _an

    events: list[str] = []
    reasons: list[str] = []

    async def on_fire(loop):
        return True

    monkeypatch.setattr(
        _an.irq,
        "poll",
        _terminal_poll(),
    )
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    real_write_state = service._store.write_state

    def _slow_write_state(payload):
        time.sleep(write_secs)
        real_write_state(payload)

    monkeypatch.setattr(service._store, "write_state", _slow_write_state)

    def _record(event, loop):
        events.append(event)
        # Captured AT EMIT TIME: the observer picks its wording from this, so a
        # reason set only by the update that follows leaves the terminal case
        # unreachable and the user is told a cycle cap fired.
        reasons.append(getattr(loop, "stopped_reason", ""))

    monkeypatch.setattr(service, "_emit", _record)
    loop = NudgeLoop(
        id="monitor13",
        slot_key="chat-1-123",
        message="Watch https://github.com/acme/widgets/pull/42",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    try:
        # Driven through the REAL timer, not by calling the gate directly: the
        # defect lives in update() cancelling the timer task that awaits it, so a
        # direct call registers no timer and cannot see it.
        service._arm_timer(loop, delay=0)
        timer = service._timers[loop.id]
        # Wait for the timer task to end, not a fixed time; the timeout is a hang guard.
        await asyncio.wait({timer}, timeout=30)
        assert timer.done(), "the terminal tick must finish, not hang"
        assert "expired" in events, "the user must be told the watch finished"
        assert reasons and reasons[0] == _an.MONITOR_TERMINAL_REASON, (
            "the reason must be readable at emit time or the wording falls through "
            "to the cycle-cap branch"
        )
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_verdict_is_discarded_when_the_loop_was_retargeted_mid_poll(tmp_path, monkeypatch):
    """The poll awaits, so the subject can change under it.

    A terminal verdict about the OLD pull request must not stop a watch that has
    just been pointed at a live one.
    """
    import kiro_crew.autonudge as _an

    async def on_fire(loop):
        return True

    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor10",
        slot_key="chat-1-123",
        message="Watch https://github.com/acme/widgets/pull/42",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    def _retarget_then_terminal(*_a, **_k):
        # Stands in for update(message=...) landing while gh was running.
        assert loop.monitor is not None
        loop.monitor.target = "acme/widgets#99"
        return _an.irq.Verdict(_an.irq.Outcome.TERMINAL, "merged")

    monkeypatch.setattr(_an.irq, "poll", _retarget_then_terminal)

    try:
        quiet = await service._monitor_tick_is_quiet(loop)
        assert quiet is False, "a stale verdict must fire as usual, not be acted on"
        assert loop.monitor is not None
        assert loop.monitor.outcome is None, "the retargeted watch must not be settled"
        assert loop.monitor.stopped_reason != _an.MONITOR_TERMINAL_REASON
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_terminal_verdict_leaves_no_timer_armed(tmp_path, monkeypatch):
    """ "Do not spend a turn" and "keep watching" are different answers.

    The terminal verdict returns the first while having just deactivated the
    loop, so a re-arm on that path would poll a merged pull request forever and
    re-emit its expiry notification every tick.
    """
    import kiro_crew.autonudge as _an

    async def on_fire(loop):  # pragma: no cover - must not be reached
        raise AssertionError("a terminal verdict must not fire")

    monkeypatch.setattr(
        _an.irq,
        "poll",
        _terminal_poll(body="the PR merged"),
    )
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor07",
        slot_key="chat-1-123",
        message="Babysit https://github.com/acme/widgets/pull/42",
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    await service._timer(loop, delay=0)
    try:
        assert not loop.active
        assert loop.id not in service._timers, "a finished subject must not stay armed"
        assert loop.next_due_ts == 0.0
        # The finish is recorded on the MONITOR too, so the record does not read
        # as merely paused. Without that, the generic resume path would re-arm
        # this watch onto an already-merged pull request.
        assert loop.monitor is not None
        assert loop.monitor.outcome is not None
        await service.update(loop.id, active=True)
        assert not loop.active, "a finished watch must not be revivable by a generic save"
        assert loop.id not in service._timers
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_retargeting_the_instruction_retargets_the_probe(tmp_path):
    """A changed instruction can change the subject, so the monitor must follow.

    Otherwise the loop keeps polling the PR it was armed on: the new subject is
    never watched, and the old one merging retires the loop while the work it was
    retargeted to sits unobserved.
    """
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(
        "chat-9-990", "Babysit https://github.com/acme/widgets/pull/42", idle_secs=300, gate=True
    )
    try:
        assert loop.monitor is not None and loop.monitor.target == "acme/widgets#42"

        await service.update(
            loop.id, message="Babysit https://github.com/acme/widgets/pull/77 instead"
        )
        assert loop.monitor is not None
        assert loop.monitor.target == "acme/widgets#77"

        # Refining the wording for the SAME subject keeps the existing monitor,
        # so its counters and follow-up allowance are not silently reset.
        loop.monitor.quiet_ticks = 5
        await service.update(
            loop.id,
            message="Babysit https://github.com/acme/widgets/pull/77 -- read the log first",
        )
        assert loop.monitor is not None
        assert loop.monitor.quiet_ticks == 5

        # An instruction that stops naming one subject returns the loop to a
        # plain timer rather than leaving it bound to a stale target.
        await service.update(loop.id, message="watch the canary deployment instead")
        assert loop.monitor is None
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_terminal_subject_stops_the_loop_without_a_turn(tmp_path, monkeypatch):
    import kiro_crew.autonudge as _an

    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)
        return True

    monkeypatch.setattr(
        _an.irq,
        "poll",
        _terminal_poll(body="the PR merged"),
    )
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor04c",
        slot_key="chat-1-123",
        message="Babysit https://github.com/acme/widgets/pull/42",
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    await service._timer(loop, delay=0)

    assert fired == []
    assert not loop.active
    assert loop.stopped_reason == _an.MONITOR_TERMINAL_REASON
