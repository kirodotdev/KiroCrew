"""AutoNudgeService tests: the probe tick of a gated monitor.

Quiet and waking ticks, the quiet-streak floor and its owed fire, wake claims and how a
refused or cancelled fire is charged, and the stored PR baseline the judge reads.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from autonudge_helpers import _enable  # noqa: F401
from autonudge_helpers import _structured_monitor

from kiro_crew import autonudge as _an
from kiro_crew.autonudge import AutoNudgeService, NudgeLoop

#: Lost-run ceiling for synchronization the test itself must release. It is a
#: hang guard whose expiry fails at the awaited line, never a performance
#: assertion, and is at most half the repository's ``--timeout=120``.
_LOST_RUN_SECS = 30.0


@pytest.mark.asyncio
async def test_a_long_quiet_streak_is_delivered_anyway(tmp_path, monkeypatch):
    """Gating must SLOW an act-on-quiet loop, never silence it.

    The gate sees only the subject, so a loop whose duty is to act while the
    subject is quiet -- refresh a heartbeat, chase a silent reviewer, rebase onto
    a moving base -- is invisible to it. Inference cannot read that intent out of
    the wording, so the bound is what keeps the design honest.
    """
    import kiro_crew.autonudge as _an

    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)
        return True

    monkeypatch.setattr(
        _an.irq, "poll", lambda *a, **k: _an.irq.Verdict(_an.irq.Outcome.QUIET, "nothing new")
    )
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor08",
        slot_key="chat-1-123",
        message="If https://github.com/acme/widgets/pull/42 still has no review, ping the reviewer",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    try:
        for _ in range(_an._MAX_QUIET_STREAK - 1):
            await service._timer(loop, delay=0)
        assert fired == [], "the saving must hold below the floor"

        await service._timer(loop, delay=0)
        assert len(fired) == 1, "the floor must deliver a turn"
        assert loop.monitor is not None
        assert loop.monitor.floor_ticks == 1
        assert loop.monitor.quiet_streak == 0, "the streak must reset after delivery"
        # Counted apart from wakes, so a periodic delivery is never reported as
        # a real signal.
        assert loop.monitor.wakes == 0
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_an_unusable_instruction_mid_poll_discards_the_verdict(tmp_path, monkeypatch):
    """The verdict is about the config the poll ran with, so the tick re-derives it.

    This replaces a test for a HOST change mid-poll -- an instruction edited from a
    bare ``owner/name#123`` to the same subject as a URL, which left kind and target
    identical while changing which SERVER was observed. Requiring an explicit
    pull-request URL removes that case entirely: a shorthand does not infer, and an
    enterprise URL is refused, so no inferable spelling can change the host. Keeping
    a test for an unreachable path would only look like protection.

    What is still reachable is an edit that stops naming a subject at all. The
    verdict must not settle the watch then either.
    """
    import kiro_crew.autonudge as _an

    async def on_fire(loop):
        return True

    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor16",
        slot_key="chat-1-123",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    def _unbind_then_terminal(*_a, **_k):
        # Stands in for an edit landing while gh was still running.
        loop.message = "watch the canary deployment instead"
        return _an.irq.Verdict(_an.irq.Outcome.TERMINAL, "merged", ("merged",))

    monkeypatch.setattr(_an.irq, "poll", _unbind_then_terminal)

    try:
        assert await service._monitor_tick_is_quiet(loop) is False
        assert loop.monitor is not None
        assert loop.monitor.outcome is None, "a verdict about the old subject must not settle it"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_the_tick_recovers_the_host_from_the_instruction(tmp_path, monkeypatch):
    """The stored target is a SHORTHAND, which carries no host.

    Re-inferring the probe config from it would discard the github.com pin a
    URL-armed watch is entitled to, and on a machine configured for an enterprise
    server the probe would resolve the slug there -- where a same-numbered pull
    request being merged would falsely terminate a live public watch.
    """
    import json

    import kiro_crew.autonudge as _an

    seen: list[str] = []

    async def on_fire(loop):
        return True

    def _capture(identity, message, probe):
        seen.append(message)
        return _an.irq.Verdict(_an.irq.Outcome.QUIET, "nothing new")

    monkeypatch.setattr(_an.irq, "poll", _capture)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor14",
        slot_key="chat-1-123",
        message="Watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    try:
        assert await service._monitor_tick_is_quiet(loop) is True
        assert seen, "the probe must have been driven"
        assert json.loads(seen[0])["host"] == "github.com"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_drifted_instruction_fires_instead_of_observing(tmp_path, monkeypatch):
    """If the instruction and the bound monitor name different subjects, fire.

    They should never disagree -- the retarget path rebinds both -- but resolving a
    disagreement by guessing which one is right is how a watch ends up observing
    the wrong pull request.
    """
    import kiro_crew.autonudge as _an

    polled: list[str] = []

    async def on_fire(loop):
        return True

    monkeypatch.setattr(
        _an.irq,
        "poll",
        lambda i, m, p: (polled.append(m), _an.irq.Verdict(_an.irq.Outcome.QUIET, "q"))[1],
    )
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor15",
        slot_key="chat-1-123",
        message="Watch https://github.com/acme/widgets/pull/99 until green",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    try:
        assert await service._monitor_tick_is_quiet(loop) is False
        assert polled == [], "a drifted subject must not be observed at all"
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_a_re_owed_claim_survives_the_backoff_re_arm(tmp_path, monkeypatch):
    """The accounting fix was being defeated by the cleanup meant to protect it.

    A refused fire re-owes its wake so the retried delivery still charges it. But the
    refusal path then re-arms with a backoff, ``_arm_timer`` cancels before it creates,
    and the cancel path dropped both claims -- erasing, one statement later, the claim
    the refusal had just re-owed. So the wake was undercounted and its follow-up turn
    lost, in the ordinary case of a busy slot.

    Replacing a timer is not cancelling a cycle. An explicit cancel still drops the
    claim, which the sibling test pins and which is a deliberate trade: an undelivered
    observation is lost rather than charged to a later turn that did not carry it.
    """
    import kiro_crew.autonudge as _an

    async def on_fire(loop):
        return False

    def _wake(*_a, **_k):
        return _an.irq.Verdict(_an.irq.Outcome.WAKE, "new red", ())

    monkeypatch.setattr(_an.irq, "poll", _wake)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor45",
        slot_key="chat-1-123",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    try:
        assert await service._monitor_tick_is_quiet(loop) is False
        # A timer must EXIST, or ``_cancel_timer`` returns before the code under test:
        # the refusal path re-arms with a backoff, and it is that re-arm's cancel which
        # would erase the claim. Without this the test cannot fail.
        service._arm_timer(loop, delay=3600)
        assert loop.id in service._timers
        await service._run_fire_cycle(loop)
        assert loop.monitor is not None and loop.monitor.wakes == 0, "a refusal charges nothing"
        assert loop.id in service._pending_monitor_wake, "and the re-arm must not erase the debt"
        # An explicit CANCEL still drops it, which is the trade the sibling test pins:
        # an undelivered observation is lost rather than charged to a turn that did not
        # carry it. Only REPLACING a timer is exempt.
        service._cancel_timer(loop.id)
        assert loop.id not in service._pending_monitor_wake
        assert loop.id not in service._pending_floor_tick
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_floor_tick_is_charged_only_when_its_delivery_lands(tmp_path, monkeypatch):
    """The floor counter must describe turns that ran, like the wake counter.

    The floor decides to deliver in the QUIET branch, and the fire it asks for can still
    be refused by a busy slot. Charging at the decision reported a turn that never ran,
    and because the honest free-tick figure is ``quiet_ticks`` minus ``floor_ticks``, an
    over-counted floor understates the saving.

    The prescribed remedy was to revert the counter until it could be charged after
    delivery. That was declined -- the counter is the subtraction that separates a quiet
    verdict from a free tick -- and the substance adopted instead.
    """
    import kiro_crew.autonudge as _an

    outcomes = [False, True]

    async def on_fire(loop):
        return outcomes.pop(0)

    def _verdict(*_a, **_k):
        return _an.irq.Verdict(_an.irq.Outcome.QUIET, "nothing yet", ())

    monkeypatch.setattr(_an.irq, "poll", _verdict)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    monitor = _structured_monitor(kind="gh-pr", target="acme/widgets#42")
    monitor.quiet_streak = _an._MAX_QUIET_STREAK - 1
    loop = NudgeLoop(
        id="monitor38",
        slot_key="chat-1-123",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=monitor,
        gate=True,
    )
    service._loops[loop.id] = loop

    try:
        # The floor trips and asks for a turn, but the slot refuses it.
        assert await service._monitor_tick_is_quiet(loop) is False
        await service._run_fire_cycle(loop)
        assert loop.monitor is not None
        assert loop.monitor.floor_ticks == 0, "a refused floor delivery spent nothing"
        assert loop.id in service._pending_floor_tick, "so the charge stays owed"
        assert loop.monitor.floor_fire_pending is True, "durably as well as in memory"

        # The retry lands, and only now is it charged -- exactly once.
        await service._run_fire_cycle(loop)
        assert loop.monitor.floor_ticks == 1
        assert loop.id not in service._pending_floor_tick
        assert loop.monitor.floor_fire_pending is False, "and the debt is discharged with it"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_death_between_the_floor_decision_and_its_turn_leaves_the_fire_owed(
    tmp_path, monkeypatch
):
    """The decision to deliver is durable while the claim carrying it is not.

    The floor's decision publishes a reset ``quiet_streak`` through a scheduled write,
    and the claim recording the owed turn lives in a process-local set. A gateway that
    stops between the decision and the turn landing therefore keeps the half that
    suppresses and loses the half that delivers: the reloaded loop reads an unchanged
    subject against a baseline written for a turn nobody received, answers quiet, and
    the forced delivery moves a whole floor away with nothing saying one was due.

    The post-wake allowance cannot cover it. That one answers a fire the slot REFUSED,
    and a process that stops refuses nothing.
    """
    polls: list[str] = []

    async def on_fire(loop):
        return True

    def _calm(identity, *_a, **_k):
        polls.append(identity)
        return _an.irq.Verdict(_an.irq.Outcome.QUIET, "nothing yet", ())

    monkeypatch.setattr(_an.irq, "poll", _calm)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    monitor = _structured_monitor(kind="gh-pr", target="acme/widgets#42")
    monitor.quiet_streak = _an._MAX_QUIET_STREAK - 1
    loop = NudgeLoop(
        id="monitor46",
        slot_key="chat-1-123",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=monitor,
        gate=True,
    )
    service._loops[loop.id] = loop

    try:
        # This tick DECIDES to deliver. No fire cycle follows it, which is the death.
        assert await service._monitor_tick_is_quiet(loop) is False
        assert loop.monitor is not None
        assert loop.monitor.quiet_streak == 0, "the baseline the next tick reads is published"
        assert loop.id in service._pending_floor_tick
        # The write that decision scheduled is what a restart reads, so let it land.
        await asyncio.wait_for(
            asyncio.gather(*tuple(service._inflight_adds)), timeout=_LOST_RUN_SECS
        )
    finally:
        service.stop()

    restarted = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    restarted._load()
    try:
        revived = restarted._loops["monitor46"]
        assert revived.monitor is not None
        assert revived.monitor.quiet_streak == 0, "the reset survived, so the subject reads calm"
        assert revived.monitor.floor_fire_pending is True, "and the owed turn survives with it"
        assert revived.id not in restarted._pending_floor_tick, "while the claim set starts empty"
        observed_before = revived.monitor.quiet_ticks

        polls.clear()
        # The probe here answers exactly what suppresses the turn: nothing has changed.
        assert await restarted._monitor_tick_is_quiet(revived) is False, "the owed turn fires"
        assert polls == [], "without spending another observation on an answer it cannot trust"
        assert revived.id in restarted._pending_floor_tick, "and the claim is re-taken to charge it"
        assert (
            revived.monitor.quiet_ticks == observed_before
        ), "an unobserved tick is not a quiet one"

        await restarted._run_fire_cycle(revived)
        assert revived.monitor.floor_ticks == 1, "the recovered turn is charged exactly once"
        assert revived.monitor.floor_fire_pending is False, "and the debt is discharged"
    finally:
        restarted.stop()


@pytest.mark.asyncio
async def test_a_refused_floor_fire_then_a_restart_delivers_exactly_one_turn(tmp_path, monkeypatch):
    """Two credits for one owed turn must not each buy a turn.

    A refused fire grants the post-wake allowance so the NEXT tick retries the
    delivery, and the floor debt records that the delivery is still owed. Both
    survive a restart while the in-process claim does not, so a debt served behind
    the allowance is served twice: the bypass fires with no claim to charge, then the
    debt fires on the tick after. The debt is therefore served first and consumes the
    allowance it duplicates.
    """
    fired: list[str] = []
    outcomes = [False, True]

    async def on_fire(loop):
        fired.append(loop.id)
        return outcomes.pop(0) if outcomes else True

    def _calm(*_a, **_k):
        return _an.irq.Verdict(_an.irq.Outcome.QUIET, "nothing yet", ())

    monkeypatch.setattr(_an.irq, "poll", _calm)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    monitor = _structured_monitor(kind="gh-pr", target="acme/widgets#42")
    monitor.quiet_streak = _an._MAX_QUIET_STREAK - 1
    loop = NudgeLoop(
        id="monitor50",
        slot_key="chat-1-123",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=monitor,
        gate=True,
    )
    service._loops[loop.id] = loop

    try:
        # The floor trips and the slot refuses the turn, so both credits stand.
        assert await service._monitor_tick_is_quiet(loop) is False
        await service._run_fire_cycle(loop)
        assert loop.monitor is not None
        assert loop.monitor.floor_fire_pending is True
        assert loop.monitor.followup_ticks == _an._WAKE_FOLLOWUP_TICKS
        assert fired == [loop.id], "the refused fire is the only one so far"
        await asyncio.wait_for(
            asyncio.gather(*tuple(service._inflight_adds)), timeout=_LOST_RUN_SECS
        )
    finally:
        service.stop()

    restarted = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    restarted._load()
    try:
        revived = restarted._loops["monitor50"]
        assert revived.monitor is not None
        assert revived.monitor.floor_fire_pending is True, "the debt crossed the restart"
        assert revived.monitor.followup_ticks == _an._WAKE_FOLLOWUP_TICKS, "so did the allowance"

        fired.clear()
        assert await restarted._monitor_tick_is_quiet(revived) is False, "the debt fires"
        assert revived.id in restarted._pending_floor_tick, "and this fire carries the claim"
        assert revived.monitor.followup_ticks == 0, "the duplicate allowance is consumed"
        await restarted._run_fire_cycle(revived)
        assert fired == [revived.id], "one turn, not two"
        assert revived.monitor.floor_ticks == 1
        assert revived.monitor.floor_fire_pending is False

        # The next tick observes instead of spending a second uncharged bypass.
        assert await restarted._monitor_tick_is_quiet(revived) is True
        assert fired == [revived.id], "still one turn for one owed delivery"
        assert revived.monitor.quiet_streak == 1
    finally:
        restarted.stop()


@pytest.mark.asyncio
async def test_a_delivered_floor_turn_stops_owing_and_lets_observation_resume(
    tmp_path, monkeypatch
):
    """The owed fire is a debt, not a latch.

    A marker that forces a turn is only safe if the confirmed delivery clears it. Left
    set, it would bypass the probe on every following tick and turn a gated watch back
    into a plain timer -- which is the saving this gate exists to make.
    """
    fired: list[str] = []

    async def on_fire(loop):
        fired.append(loop.id)
        return True

    def _calm(*_a, **_k):
        return _an.irq.Verdict(_an.irq.Outcome.QUIET, "nothing yet", ())

    monkeypatch.setattr(_an.irq, "poll", _calm)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    monitor = _structured_monitor(kind="gh-pr", target="acme/widgets#42")
    monitor.quiet_streak = _an._MAX_QUIET_STREAK - 1
    loop = NudgeLoop(
        id="monitor47",
        slot_key="chat-1-123",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=monitor,
        gate=True,
    )
    service._loops[loop.id] = loop

    try:
        assert await service._monitor_tick_is_quiet(loop) is False, "the floor fires"
        assert loop.monitor is not None and loop.monitor.floor_fire_pending is True
        await service._run_fire_cycle(loop)
        assert fired == [loop.id], "the turn landed"
        assert loop.monitor.floor_ticks == 1
        assert loop.monitor.floor_fire_pending is False

        # And the next tick observes again rather than firing on a spent debt.
        assert await service._monitor_tick_is_quiet(loop) is True, "the saving is back"
        assert loop.monitor.quiet_streak == 1
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_an_unreadable_owed_floor_fire_resolves_toward_spending(tmp_path):
    """A stored value this gateway cannot read means a turn MAY be owed.

    ``bool("")`` would clear the fail-safe and suppress a turn that was due, so an
    unreadable marker is normalised toward doubt exactly as the in-flight poll marker
    is. The cost is one turn, and the delivery it asks for clears it.
    """
    store = {
        "version": 1,
        "loops": [
            {
                "id": "monitor48",
                "slot_key": "chat-1-123",
                "message": "watch https://github.com/acme/widgets/pull/42 until green",
                "idle_secs": 30,
                "active": True,
                "gate": True,
                "monitor": {
                    "kind": "gh-pr",
                    "target": "acme/widgets#42",
                    "objective": "review_ready",
                    "created_ts": 1_000.0,
                    "floor_fire_pending": "",
                },
            }
        ],
    }
    (tmp_path / "autonudge.json").write_text(json.dumps(store), encoding="utf-8")

    service = AutoNudgeService(base_dir=tmp_path)
    service._load()
    try:
        loop = service._loops["monitor48"]
        assert loop.monitor is not None
        assert loop.monitor.floor_fire_pending is True
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_record_written_before_the_owed_floor_marker_owes_nothing(tmp_path):
    """Absent means fresh, so an upgraded store does not invent a forced turn."""
    store = {
        "version": 1,
        "loops": [
            {
                "id": "monitor49",
                "slot_key": "chat-1-123",
                "message": "watch https://github.com/acme/widgets/pull/42 until green",
                "idle_secs": 30,
                "active": True,
                "gate": True,
                "monitor": {
                    "kind": "gh-pr",
                    "target": "acme/widgets#42",
                    "objective": "review_ready",
                    "created_ts": 1_000.0,
                },
            }
        ],
    }
    (tmp_path / "autonudge.json").write_text(json.dumps(store), encoding="utf-8")

    service = AutoNudgeService(base_dir=tmp_path)
    service._load()
    try:
        loop = service._loops["monitor49"]
        assert loop.monitor is not None
        assert loop.monitor.floor_fire_pending is False
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_refused_wake_is_charged_once_when_its_retry_delivers(tmp_path, monkeypatch):
    """A refused fire accounts for nothing, so the wake must stay owed.

    The claim was discarded on the assumption that reaching the end of the fire cycle
    meant the wake had been accounted for. A refused fire does not account for it: the
    next tick takes the observation-free follow-up bypass, DELIVERS the turn, and finds
    no claim to charge -- so a wake that really happened and really woke the agent was
    missing from ``wakes`` entirely, which makes the saving look better than it is.

    Exactly once is the property: the refusal charges nothing, the retry charges one.
    """
    import kiro_crew.autonudge as _an

    outcomes = [False, True]

    async def on_fire(loop):
        return outcomes.pop(0)

    def _verdict(*_a, **_k):
        return _an.irq.Verdict(_an.irq.Outcome.WAKE, "new red", ())

    monkeypatch.setattr(_an.irq, "poll", _verdict)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor37",
        slot_key="chat-1-123",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    try:
        # First tick: the probe wakes, the fire is REFUSED.
        assert await service._monitor_tick_is_quiet(loop) is False
        await service._run_fire_cycle(loop)
        assert loop.monitor is not None
        assert loop.monitor.wakes == 0, "a refused fire charges nothing"
        assert loop.monitor.followup_ticks == _an._WAKE_FOLLOWUP_TICKS

        # Second tick: the follow-up bypass retries the delivery, which lands.
        assert await service._monitor_tick_is_quiet(loop) is False
        await service._run_fire_cycle(loop)
        assert loop.monitor.wakes == 1, "the delivered retry charges the owed wake"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_an_oversized_stored_pr_baseline_is_bounded_by_the_loader(tmp_path):
    """The write path's clip constrains only what THIS build wrote.

    This store is writable by an auto-approved agent shell, so an oversized value would
    otherwise be retained and re-serialized on every persist for the life of the loop.
    The bound has to be applied where the row is read, beside the four sibling judge maps
    that are already rebuilt there.
    """
    from kiro_crew import autonudge as _an

    async def on_fire(loop):
        return True

    record = {
        "id": "monitor90",
        "slot_key": "chat-1-123",
        "message": "watch https://github.com/acme/widgets/pull/42",
        "idle_secs": 300,
        "active": True,
        "judge_pr_seen": {
            "digest": "d" * 9_000,
            "remarks": [f"comment:{i}" for i in range(5_000)],
            "prose": "x" * 9_000,
        },
    }
    (tmp_path / "autonudge.json").write_text(json.dumps({"loops": [record]}), encoding="utf-8")
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    try:
        await service.start()
        loop = service._loops.get("monitor90")
        assert loop is not None, "the loop must still load"
        seen = loop.judge_pr_seen
        assert len(seen["remarks"]) == _an._JUDGE_PR_SEEN_REMARKS, "the id list is capped"
        assert len(seen["digest"]) == _an._JUDGE_PR_SEEN_DIGEST_CHARS, "the digest is clipped"
        assert set(seen) == {"digest", "remarks"}, "an unknown key is not retained"
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [5, True, "comment:1", {"comment:1": 1}, None])
async def test_a_malformed_pr_baseline_never_disables_the_loop(tmp_path, stored):
    """The loop must survive a baseline the store can hold but the writer never emits.

    Iterating the stored value directly raises ``TypeError`` for a non-iterable, and the
    loader has no handler for it, so the whole record is skipped and an active watch is
    never rearmed -- losing the watch, not merely the baseline. A string or a mapping is
    worse than a crash: both iterate, so their characters or keys are retained as remark
    ids and a real remark then reads as already seen. The container is checked for the
    one type the writer emits, matching the sibling boundaries in this same function.
    """
    from kiro_crew import autonudge as _an

    async def on_fire(loop):
        return True

    record = {
        "id": "monitor91",
        "slot_key": "chat-1-123",
        "message": "watch https://github.com/acme/widgets/pull/42",
        "idle_secs": 300,
        "active": True,
        "judge_pr_seen": {"digest": "abc123", "remarks": stored},
    }
    (tmp_path / "autonudge.json").write_text(json.dumps({"loops": [record]}), encoding="utf-8")
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    try:
        await service.start()
        loop = service._loops.get("monitor91")
        assert loop is not None, "a malformed baseline must not cost the loop"
        assert loop.active, "and the watch stays armed"
        seen = loop.judge_pr_seen
        assert seen["remarks"] == [], "nothing is retained from a non-list"
        assert seen["digest"] == "abc123", "while the valid field beside it survives"
        assert _an._bounded_judge_pr_seen({"remarks": stored})["remarks"] == []
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,held_across_fire",
    [
        ("WAKE", True),
        ("FALLBACK", False),
        ("QUIET", False),
    ],
)
async def test_a_wake_keeps_the_interrupted_poll_doubt_until_delivery_settles(
    tmp_path, monkeypatch, outcome, held_across_fire
):
    """The doubt guards DELIVERY for a wake, not the poll returning.

    The kernel commits "reported" for what it saw, so a process that dies between the
    verdict and the turn landing leaves a signal nothing will re-raise: a fresh
    observation reads the same state as unchanged. The in-process refusal is covered by
    ``followup_ticks``; only a marker that OUTLIVES the fire covers a death. Clearing it
    when the poll returned made the protection depend on whether a debounced write won a
    race against the shutdown, and a crash-safety property that holds only when a race
    falls one way is not a property.

    Other outcomes discharge it immediately: QUIET delivers nothing that could be lost,
    and a FALLBACK observed nothing, so the kernel committed nothing to dedupe against.
    """
    import kiro_crew.autonudge as _an

    seen_during_fire: list[bool] = []

    async def on_fire(loop):
        seen_during_fire.append(loop.monitor.poll_in_flight if loop.monitor else False)
        return True

    def _verdict(*_a, **_k):
        return _an.irq.Verdict(getattr(_an.irq.Outcome, outcome), "moved", ())

    monkeypatch.setattr(_an.irq, "poll", _verdict)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor33",
        slot_key="chat-1-123",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    try:
        quiet = await service._monitor_tick_is_quiet(loop)
        assert loop.monitor is not None
        if held_across_fire:
            assert quiet is False, "a wake fires"
            assert loop.monitor.poll_in_flight is True, "the doubt must outlive the verdict"
            await service._run_fire_cycle(loop)
            assert seen_during_fire == [True], "and must still be set DURING the fire"
            assert loop.monitor.poll_in_flight is False, "then discharged once it settles"
        else:
            assert loop.monitor.poll_in_flight is False, "nothing was owed, so nothing is held"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_an_upgraded_record_with_a_monitor_but_no_gate_is_not_polled(tmp_path, monkeypatch):
    """The stored decision wins over the presence of the monitor object.

    Flipping the legacy default to ungated created a state the two checks disagree
    about: a record with a monitor dict but no ``gate`` key -- armed while the default
    was True, or upgraded from an earlier build -- decodes to ``gate=False`` with its
    monitor intact. Reading only ``monitor is None`` would poll it anyway and let a
    terminal verdict DEACTIVATE it, which is the harm the opt-out exists to prevent.
    An opt-out honoured by only some paths is worse than none.
    """
    import kiro_crew.autonudge as _an

    polled: list[str] = []

    async def on_fire(loop):
        return True

    def _should_never_run(*_a, **_k):
        polled.append("polled")
        return _an.irq.Verdict(_an.irq.Outcome.TERMINAL, "merged", ("merged",))

    monkeypatch.setattr(_an.irq, "poll", _should_never_run)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor31",
        slot_key="chat-1-123",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=False,
    )
    service._loops[loop.id] = loop

    try:
        assert await service._monitor_tick_is_quiet(loop) is False, "it must fire, not gate"
        assert polled == [], "and the probe must not run at all"
        assert loop.active is True, "so nothing can deactivate it"
        assert loop.monitor is not None and loop.monitor.outcome is None
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_an_interrupted_poll_fires_on_the_next_tick(tmp_path, monkeypatch):
    """A cancelled poll may already have been consumed on disk.

    The kernel commits its dedupe state BEFORE raising a wake, which is right for
    the cron driver -- there the raise IS the delivery. For a driver that awaits the
    verdict the two come apart: a gateway shutdown landing mid-poll leaves the
    observation recorded as reported while no turn was dispatched, and re-observing
    would read the same state as unchanged. So the next tick fires, and the doubt is
    consumed once rather than latching.
    """
    import kiro_crew.autonudge as _an

    async def on_fire(loop):
        return True

    polled: list[str] = []

    def _should_not_be_trusted(*_a, **_k):
        polled.append("polled")
        return _an.irq.Verdict(_an.irq.Outcome.QUIET, "no change")

    monkeypatch.setattr(_an.irq, "poll", _should_not_be_trusted)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    monitor = _structured_monitor(kind="gh-pr", target="acme/widgets#42")
    monitor.poll_in_flight = True  # as a restart would find it
    loop = NudgeLoop(
        id="monitor24",
        slot_key="chat-1-123",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=30,
        monitor=monitor,
        gate=True,
    )
    service._loops[loop.id] = loop

    try:
        assert await service._monitor_tick_is_quiet(loop) is False, "the wake is owed a turn"
        assert polled == [], "and the stale observation must not be re-consulted"
        assert loop.monitor is not None
        assert loop.monitor.poll_in_flight is False, "the doubt must not latch"
        assert loop.monitor.gate_fallbacks == 1, "and it is metered as a fallback"
        # The NEXT tick has no doubt to consume, so the gate works normally again.
        assert await service._monitor_tick_is_quiet(loop) is True
        assert polled == ["polled"]
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_cancelled_cycle_does_not_bequeath_its_wake(tmp_path, monkeypatch):
    """A wake claim belongs to the tick that took it.

    A wake is claimed at observation and charged where delivery is confirmed. If
    the cycle is cancelled in between -- an update(), a deactivation, a shutdown --
    the claim must die with it. Otherwise the loop's next delivered fire, which
    may be a fallback or a floor tick that observed nothing, inherits the claim
    and is counted as a wake no observation ever made.
    """
    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)
        return True

    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor12",
        slot_key="chat-1-123",
        message="Watch https://github.com/acme/widgets/pull/42",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    async def _never() -> None:
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing never-ending task body, moved verbatim; it is cancelled, never awaited as a barrier
        await asyncio.sleep(3600)

    try:
        # A tick observed a wake and is now in flight toward its fire.
        service._pending_monitor_wake[loop.id] = "[wake] acme/widgets#42 went red"
        service._timers[loop.id] = asyncio.create_task(_never())
        await asyncio.sleep(0)

        service._cancel_timer(loop.id)
        assert (
            loop.id not in service._pending_monitor_wake
        ), "a cancelled cycle must not leave a claim for a later fire to inherit"
        assert (
            service.pending_wake_briefs(loop.id) == ""
        ), "nor the wake text that claim was carrying"

        # And the later fire, which observed nothing, charges nothing.
        await service._run_fire_cycle(loop)
        assert loop.monitor is not None
        assert loop.monitor.wakes == 0
        assert loop.monitor.followup_ticks == 0
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_refused_fire_charges_no_wake(tmp_path, monkeypatch):
    """Counters must describe turns that happened.

    A wake is a delivered turn. If the fire is refused -- busy slot, callback
    error -- charging it would report a turn that never ran and would hand out
    the follow-up allowance for it, so the next tick would skip its observation
    to protect work that was never started.
    """
    import kiro_crew.autonudge as _an

    async def refuse(loop):
        return False

    monkeypatch.setattr(
        _an.irq, "poll", lambda *a, **k: _an.irq.Verdict(_an.irq.Outcome.WAKE, "new red")
    )
    service = AutoNudgeService(base_dir=tmp_path, on_fire=refuse)
    loop = NudgeLoop(
        id="monitor11",
        slot_key="chat-1-123",
        message="Watch https://github.com/acme/widgets/pull/42",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    try:
        await service._timer(loop, delay=0)
        assert loop.monitor is not None
        assert loop.monitor.wakes == 0, "a refused fire is not a wake"
        assert loop.id in service._pending_monitor_wake, "but the wake is still OWED"
        # The claim must NOT be RELEASED here -- the second wrong belief
        # this one test has recorded. The reasoning then treated the claim as a record
        # of a turn that had happened, so releasing it looked like the way to avoid
        # charging a wake that did not. It is really a record of a wake that is OWED:
        # the next tick takes the gate-free bypass below, DELIVERS the turn, and found
        # no claim to charge -- so a wake that really woke the agent was missing from
        # the counter, which makes the saving look better than it is. Keeping it owed
        # cannot double-charge, because the charge happens only where delivery is
        # confirmed and the claim is discarded at that same point.
        # It DOES buy a gate-free retry, and an earlier version of this test
        # asserted the opposite. The reasoning then was "a refused fire earns
        # nothing"; the reasoning now is that the kernel has already deduped the
        # observation this fire was carrying, so without a bypassed tick the next
        # one re-observes an unchanged subject, calls it quiet, and the signal is
        # gone until the streak floor. Not counting it as a wake and not letting
        # it retry are different things.
        assert loop.monitor.followup_ticks == _an._WAKE_FOLLOWUP_TICKS
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_quiet_tick_re_arms_its_own_timer(tmp_path, monkeypatch):
    """A quiet tick must schedule the next one, or the watch dies on tick one.

    The delivered paths re-arm elsewhere -- notify_turn_complete for a dashboard
    slot, the fire cycle's own exit for a channel key -- and a quiet tick reaches
    neither. Asserting only "no turn was spent" is what let this through the
    first time: a dead watch and a calm watch both spend nothing.
    """
    import kiro_crew.autonudge as _an

    async def on_fire(loop):  # pragma: no cover - must not be reached
        raise AssertionError("a quiet tick must not fire")

    monkeypatch.setattr(
        _an.irq, "poll", lambda *a, **k: _an.irq.Verdict(_an.irq.Outcome.QUIET, "pending")
    )
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor06",
        slot_key="chat-1-123",
        message="Babysit https://github.com/acme/widgets/pull/42",
        idle_secs=30,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    await service._timer(loop, delay=0)
    try:
        assert loop.id in service._timers, "the quiet tick left no timer armed"
        assert loop.next_due_ts > 0, "the quiet tick left no deadline"
        assert loop.active
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_wake_buys_exactly_one_follow_up_turn_then_gating_resumes(tmp_path, monkeypatch):
    """The stall guard, and its bound.

    The probe watches the subject, so an agent that was woken and has not pushed
    yet is invisible to it. One unconditional follow-up turn after each wake
    keeps that work moving; the bound is what stops the guard from quietly
    disabling the gate altogether.
    """
    import kiro_crew.autonudge as _an

    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)
        return True

    verdicts = iter(
        (
            _an.irq.Verdict(_an.irq.Outcome.WAKE, "red: Build"),
            _an.irq.Verdict(_an.irq.Outcome.QUIET, "checks running"),
        )
    )
    monkeypatch.setattr(_an.irq, "poll", lambda *a, **k: next(verdicts))
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor05",
        slot_key="chat-1-123",
        message="Babysit https://github.com/acme/widgets/pull/42",
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    # 1) the wake fires and grants the allowance
    await service._timer(loop, delay=0)
    assert len(fired) == 1
    assert loop.monitor is not None
    assert loop.monitor.followup_ticks == 1

    # 2) the next tick spends it WITHOUT observing -- the probe iterator is not
    #    advanced, which is what proves the gate was bypassed rather than passed
    await service._timer(loop, delay=0)
    assert len(fired) == 2
    assert loop.monitor.followup_ticks == 0

    # 3) gating resumes: the queued QUIET verdict is now consumed and no turn
    #    is spent, so the allowance cannot silently disable the saving
    await service._timer(loop, delay=0)
    assert len(fired) == 2
    assert loop.monitor.quiet_ticks == 1
    # A QUIET verdict RE-ARMS, by design -- so this test has left a pending timer
    # task and must stop the service, or the leak lands in whatever test runs next.
    service.stop()


@pytest.mark.asyncio
async def test_a_quiet_probe_tick_dispatches_zero_turns(tmp_path, monkeypatch):
    """The saving, stated as a test: nothing changed, so no turn is spent.

    The loop must stay ACTIVE. Skipping a turn is not stopping -- a gate that
    deactivated on a quiet tick would end the watch on the first uneventful
    observation, which is every watch's normal state.
    """
    import kiro_crew.autonudge as _an

    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)
        return True

    monkeypatch.setattr(
        _an.irq, "poll", lambda *a, **k: _an.irq.Verdict(_an.irq.Outcome.QUIET, "checks running")
    )
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor03",
        slot_key="chat-1-123",
        message="Babysit https://github.com/acme/widgets/pull/42",
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    await service._timer(loop, delay=0)

    assert fired == []
    assert loop.active
    assert loop.monitor is not None
    assert loop.monitor.quiet_ticks == 1
    assert loop.monitor.wakes == 0
    # The quiet tick re-armed itself -- that is the behaviour under test -- so the
    # pending timer has to be cancelled here rather than left for the next test.
    service.stop()


@pytest.mark.asyncio
async def test_a_judge_overriding_a_quiet_probe_is_not_metered_as_a_free_tick(
    tmp_path, monkeypatch
):
    """A tick that DELIVERS must not be booked as one that saved a turn.

    The probe finds nothing and the judge then answers on evidence the probe cannot
    read, so this tick spends a turn. Charging the free-tick counter and the streak
    before asking would overstate the saving this feature exists to report, and it
    would walk a loop that keeps delivering toward a forced floor tick it never earned.
    """
    import kiro_crew.autonudge as _an

    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)
        return True

    monkeypatch.setattr(
        _an.irq, "poll", lambda *a, **k: _an.irq.Verdict(_an.irq.Outcome.QUIET, "checks running")
    )
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor03j",
        slot_key="chat-1-123",
        message="Babysit https://github.com/acme/widgets/pull/42",
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    loop.monitor.quiet_streak = 3
    service._loops[loop.id] = loop

    async def judge_fires(_loop):
        return False

    service._judge_tick_is_quiet = judge_fires

    await service._timer(loop, delay=0)

    assert len(fired) == 1, "the judge's answer spends the turn"
    assert loop.monitor is not None
    assert loop.monitor.quiet_ticks == 0, "a delivered tick is not a free one"
    assert loop.monitor.quiet_streak == 0, "and it does not walk toward the floor"
    service.stop()


@pytest.mark.asyncio
async def test_a_waking_probe_tick_dispatches_exactly_one_turn(tmp_path, monkeypatch):
    import kiro_crew.autonudge as _an

    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)
        return True

    monkeypatch.setattr(
        _an.irq, "poll", lambda *a, **k: _an.irq.Verdict(_an.irq.Outcome.WAKE, "red: Build")
    )
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor03b",
        slot_key="chat-1-123",
        message="Babysit https://github.com/acme/widgets/pull/42",
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    await service._timer(loop, delay=0)

    assert fired == [loop]
    assert loop.monitor is not None
    assert loop.monitor.wakes == 1
    assert loop.monitor.quiet_ticks == 0
    # A DELIVERED tick re-arms too, through the fire cycle's own exit -- so this
    # leaks a pending timer as surely as a quiet one does.
    service.stop()


@pytest.mark.asyncio
async def test_a_monitor_whose_kind_has_no_probe_fires_exactly_as_before(tmp_path):
    """No observer for the subject must degrade to today's timer, not to silence.

    This is the direction the whole gate is biased toward: a monitor we cannot
    observe keeps the behaviour it had before this feature existed. The fixture's
    kind deliberately has no probe registered.
    """
    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)
        return True

    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor04",
        slot_key="chat-1-123",
        message="watch something we have no probe for",
        monitor=_structured_monitor(),
        gate=True,
    )
    service._loops[loop.id] = loop

    await service._timer(loop, delay=0)

    assert fired == [loop]
    service.stop()  # the FALLBACK fire re-armed; do not leak its timer


@pytest.mark.asyncio
async def test_a_probe_that_raises_still_fires_rather_than_going_silent(tmp_path, monkeypatch):
    import kiro_crew.autonudge as _an

    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)
        return True

    def _boom(*_a, **_k):
        raise RuntimeError("probe defect")

    monkeypatch.setattr(_an.irq, "poll", _boom)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor04b",
        slot_key="chat-1-123",
        message="Babysit https://github.com/acme/widgets/pull/42",
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    await service._timer(loop, delay=0)

    assert fired == [loop]
    service.stop()  # the FALLBACK fire re-armed; do not leak its timer
