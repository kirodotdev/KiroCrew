"""AutoNudgeService tests: arming a loop, and the gate a monitor arms with.

The idle-timer basics, terminal-notification delivery, which instructions arm a gated
monitor (one PR, a work ledger) and which arm ungated, the opt-out, and reviving a gated
monitor after a restart.
"""

from __future__ import annotations

import json

import pytest
from autonudge_helpers import _enable  # noqa: F401
from autonudge_helpers import svc_fixture  # noqa: F401
from autonudge_helpers import _structured_monitor

from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.monitoring.models import MONITOR_STATE_VERSION, MonitorOutcome


@pytest.mark.asyncio
async def test_terminal_notification_delivery_is_persisted_for_exact_terminal(svc):
    loop = NudgeLoop(
        id="monitor1",
        slot_key="chat-1-123",
        message="watch it",
        active=False,
        monitor=_structured_monitor(
            outcome=MonitorOutcome.SUCCESS,
            stopped_at=1_250.0,
        ),
    )
    svc._loops[loop.id] = loop

    assert await svc.mark_terminal_notification_delivered(
        loop.id,
        MonitorOutcome.SUCCESS,
        1_250.0,
    )

    restored = AutoNudgeService(base_dir=svc._base_dir)
    restored._load()
    assert restored._loops[loop.id].monitor is not None
    assert restored._loops[loop.id].monitor.terminal_notification_delivered
    assert not await svc.mark_terminal_notification_delivered(
        loop.id,
        MonitorOutcome.BLOCKED,
        1_250.0,
    )


@pytest.mark.asyncio
async def test_terminal_notification_delivery_survives_opaque_monitor_reload(svc):
    monitor = _structured_monitor(
        outcome=MonitorOutcome.SUCCESS,
        stopped_at=1_250.0,
    )
    monitor.version = MONITOR_STATE_VERSION + 1
    loop = NudgeLoop(
        id="monitor-future",
        slot_key="chat-1-123",
        message="watch it",
        active=False,
        monitor=monitor,
    )
    svc._loops[loop.id] = loop
    svc._save()
    raw_monitor = json.loads(svc._path.read_text())["loops"][0]["monitor"]

    first_reload = AutoNudgeService(base_dir=svc._base_dir)
    first_reload._load()
    assert await first_reload.mark_terminal_notification_delivered(
        loop.id,
        MonitorOutcome.BLOCKED,
        0.0,
    )
    assert json.loads(svc._path.read_text())["loops"][0]["monitor"] == raw_monitor

    second_reload = AutoNudgeService(base_dir=svc._base_dir)
    second_reload._load()
    assert not await second_reload.mark_terminal_notification_delivered(
        loop.id,
        MonitorOutcome.BLOCKED,
        0.0,
    )


@pytest.mark.asyncio
async def test_add_and_fire_on_idle(svc, monkeypatch):
    """Arming a timer and letting it elapse triggers the fire callback."""
    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)
        return True

    svc._on_fire = on_fire
    # Patch asyncio.sleep inside the service's _timer to a no-op so the
    # test exercises the real fire path without waiting _MIN_IDLE_SECS.
    import kiro_crew.autonudge as _an

    async def _nosleep(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15)
    # The timer task was created on add(); await it to completion.
    await svc._timers[loop.id]
    assert len(fired) == 1
    assert fired[0].id == loop.id
    # cycle_count should have been bumped by _timer.
    assert svc._loops[loop.id].cycle_count == 1


@pytest.mark.asyncio
async def test_user_input_cancels_timer(svc):
    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)

    svc._on_fire = on_fire
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15)
    assert loop.id in svc._timers
    svc.notify_user_input("chat-1-123")
    assert loop.id not in svc._timers


@pytest.mark.asyncio
async def test_notify_turn_complete_rearms(svc):
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15)
    svc._cancel_timer(loop.id)
    assert loop.id not in svc._timers
    svc.notify_turn_complete("chat-1-123")
    assert loop.id in svc._timers


@pytest.mark.asyncio
async def test_persistence_across_restart(tmp_path):
    svc1 = AutoNudgeService(base_dir=tmp_path)
    await svc1.start()
    loop = await svc1.add(slot_key="chat-1-123", message="go", idle_secs=15, max_cycles=5)
    await svc1.shutdown()

    # New instance reads the same file and restores loops.
    svc2 = AutoNudgeService(base_dir=tmp_path)
    await svc2.start()
    restored = svc2.get_by_slot("chat-1-123")
    assert restored is not None
    assert restored.id == loop.id
    assert restored.message == "go"
    assert restored.max_cycles == 5
    assert loop.id in svc2._timers  # timer re-armed
    await svc2.shutdown()


@pytest.mark.asyncio
async def test_arming_a_loop_that_names_one_pr_gates_it_without_any_parameter(tmp_path):
    """The default-on property, stated as a test -- at the surface that owns it.

    No caller passes a target, a kind, or an enable flag: the subject comes out of
    the instruction the caller had already written. Every earlier version of this
    saving was an opt-in and measured zero adoption; there is nothing to opt into.

    The default lives at the ARMING SURFACES (the monitor_start tool, its directive,
    the REST route), not in ``AutoNudgeService.add``. That distinction is the point
    rather than a detail: the service also arms loops whose work is NOT a pull
    request -- a goal loop, an app's own timer -- and inferring a monitor from any
    message that merely mentions one PR would throttle those and, if the PR is
    already merged, deactivate them before their first turn. The surfaces' own
    defaults are pinned where they live: ``test_monitor_start_ack`` asserts the
    tool's directive payload carries ``gate: true`` when the caller passed nothing,
    and ``test_autonudge_handlers_cov80`` asserts the REST route passes ``gate=True``
    on an absent field. What THIS test owns is the other half -- that a gated arming
    needs no target, kind or enable flag to find its subject.
    """
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(
        "chat-9-999",
        "Babysit https://github.com/acme/widgets/pull/42 until the checks are green.",
        idle_secs=300,
        gate=True,
    )
    try:
        assert loop.monitor is not None
        assert loop.monitor.kind == "gh-pr"
        assert loop.monitor.target == "acme/widgets#42"
        assert loop.monitor.quiet_ticks == 0
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_arming_a_loop_with_no_observable_subject_stays_exactly_as_before(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(
        "chat-9-998",
        "Keep checking the canary until the deployment settles.",
        idle_secs=300,
    )
    try:
        assert loop.monitor is None
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_an_ambiguous_instruction_arms_ungated_rather_than_guessing(tmp_path):
    """Two PRs named -- gating the wrong one would silence the right one."""
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(
        "chat-9-997",
        "Drive acme/widgets#42; it is blocked on acme/widgets#7 merging first.",
        idle_secs=300,
    )
    try:
        assert loop.monitor is None
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_work_ledger_watch_arms_through_the_service_and_gates_on_itself(tmp_path):
    """``watch='work-ledger'`` arms a monitor whose subject is the arming session.

    This is the service-level counterpart to the probe-level
    ``test_inference_names_the_sessions_own_ledger_only_when_it_is_asked``: the
    arming surfaces thread ``watch`` and ``slot_key`` through ``add`` into the
    monitor construction, where an explicit ``watch`` gates on its own without
    ``gate`` (``gate=bool(gate or watch)``). Without this the field would work
    from ``monitor_start`` and do nothing when a goal/app loop arms itself, and
    ``_monitor_tick_is_quiet`` would then refuse to poll a loop that looks armed.
    The subject is the ``slot_key``, which no reading of the message can recover.
    """
    from kiro_crew.autonudge_service.subject import loop_subject

    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(
        "chat-conductor",
        "Dispatch the queue; keep an eye on the ledger.",
        idle_secs=300,
        gate=False,
        watch="work-ledger",
    )
    try:
        assert loop.monitor is not None
        assert loop.monitor.kind == "work-ledger"
        assert loop.monitor.target == "chat-conductor"
        # The watch gates the loop even though ``gate`` was False, so the tick
        # will actually poll it rather than treat it as an ungated timer.
        assert loop.gate is True
        # The stored loop re-derives the same subject from its own two strings,
        # reading the watch kind back off the persisted monitor.
        rederived = loop_subject(loop)
        assert rederived is not None
        assert (rederived.kind, rederived.subject) == ("work-ledger", "chat-conductor")
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_watch_with_no_slot_key_subject_arms_ungated_like_before(tmp_path):
    """A work-ledger watch needs a session to be about; absent one, no monitor.

    ``slot_key`` is empty only on a path that never carries one, so the watch
    cannot invent a subject and the loop arms as an ordinary ungated timer --
    the same safe fallback as an instruction naming no observable subject.
    """
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(
        "",
        "Dispatch the queue; keep an eye on the ledger.",
        idle_secs=300,
        gate=False,
        watch="work-ledger",
    )
    try:
        assert loop.monitor is None
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_gated_monitor_survives_a_restart_and_re_arms(tmp_path):
    """A persisted monitor keeps its active intent across a gateway restart.

    Deactivating it on load would be wrong: with no delivery gate the
    legacy timer would have injected a prompt without a decision. With the tick
    gate in place, deactivating would instead end every watch at the next
    restart -- and silently, since a stopped watch and a quiet one look the same
    from outside. No turn is fired here: arming is not firing.
    """
    store = {
        "version": 1,
        "loops": [
            {
                "id": "monitor01",
                "slot_key": "chat-1-123",
                "message": "Babysit https://github.com/acme/widgets/pull/42",
                "idle_secs": 15,
                "active": True,
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
    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)
        return True

    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    await service.start()
    try:
        loop = service._loops["monitor01"]
        assert loop.active
        assert loop.id in service._timers
        assert fired == []
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "monitor",
    (
        _structured_monitor(version=99),
        _structured_monitor(outcome=MonitorOutcome.BLOCKED),
    ),
)
async def test_generic_update_cannot_reactivate_a_settled_or_future_monitor(tmp_path, monitor):
    """A finished monitor, and one this gateway cannot interpret, stay off.

    The tick gate declines to observe a monitor that carries an outcome, so
    reviving one would fire ungated prompts at a subject that is already done.
    """
    service = AutoNudgeService(base_dir=tmp_path)
    loop = NudgeLoop(
        id="monitor02",
        slot_key="chat-1-123",
        message="must not dispatch",
        active=False,
        monitor=monitor,
    )
    service._loops[loop.id] = loop

    updated = await service.update(loop.id, active=True)

    assert updated is loop
    assert not loop.active
    assert loop.id not in service._timers


@pytest.mark.asyncio
async def test_a_current_gated_monitor_can_be_resumed_by_the_user(tmp_path):
    """Pausing and resuming a watch from the goal popover must work.

    Refusing here is what made a monitor loop unrecoverable once stopped: the
    only surface that can resume a loop is this generic path.
    """
    service = AutoNudgeService(base_dir=tmp_path)
    loop = NudgeLoop(
        id="monitor02b",
        slot_key="chat-1-123",
        message="Babysit https://github.com/acme/widgets/pull/42",
        active=False,
        monitor=_structured_monitor(kind="gh-pr", target="acme/widgets#42"),
        gate=True,
    )
    service._loops[loop.id] = loop

    await service.update(loop.id, active=True)

    assert loop.active
    assert loop.id in service._timers
    service.stop()


@pytest.mark.asyncio
async def test_a_settled_work_ledger_reaches_the_terminal_branch_and_deactivates(
    tmp_path, monkeypatch
):
    """A finished work-ledger loop settles, though it exposes no ``probe.observation``.

    The work-ledger probe is not a fetcher: it never sets ``probe.observation``, so
    ``_pr_observation_of`` finds nothing and the observation-based half of the
    terminal gate is false for this kind forever. Its finish surfaces the OTHER way
    -- the kernel attributes a ``Severity.TERMINAL`` observation, which the tick sees
    as ``verdict.outcome is TERMINAL`` carrying the probe's own success key
    (``all-accepted``). Gating on the observation alone left a settled ledger with
    ``terminal`` false, so it never deactivated and re-polled its own finished ledger
    every interval. The gate must honour the kernel's typed terminal, and the finish
    must record as a SUCCESS from ``terminal_succeeded`` (not from ``merged``, which
    only a pull request ever sets).
    """
    import kiro_crew.autonudge as _an

    async def on_fire(loop):
        return True

    def _ledger_settled(identity, message, probe):
        # Faithful to the real work-ledger probe: NO ``probe.observation`` is set,
        # and the terminal verdict rides the kernel's own Outcome.TERMINAL with the
        # ledger's ``all-accepted`` success key.
        return _an.irq.Verdict(_an.irq.Outcome.TERMINAL, "all items accepted", ("all-accepted",))

    monkeypatch.setattr(_an.irq, "poll", _ledger_settled)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    loop = NudgeLoop(
        id="monitor-wl",
        slot_key="chat-1-123",
        message="wake the conductor from its work ledger",
        idle_secs=30,
        # A work-ledger watch's subject IS its slot -- ``targets.work_ledger_target``
        # derives the subject from ``slot_key``, so the monitor's canonical target
        # must equal it for ``loop_subject`` to bind and the tick to reach the poll.
        monitor=_structured_monitor(kind="work-ledger", target="chat-1-123"),
        gate=True,
    )
    service._loops[loop.id] = loop

    try:
        # A terminal tick delivers no turn, so the tick reports quiet.
        assert await service._monitor_tick_is_quiet(loop) is True
        assert loop.monitor is not None
        assert loop.monitor.outcome is not None, "a settled ledger must reach the terminal branch"
        assert loop.monitor.outcome.value == "success", "an all-accepted ledger finished well"
        assert loop.active is False, "and the watch deactivates rather than polling forever"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_work_ledger_recheck_confirms_from_the_verdict_not_an_observation(
    tmp_path, monkeypatch
):
    """A channel work-ledger watch must SETTLE after its terminal turn, not redeliver forever.

    A channel loop defers its settlement as a ``terminal_pending`` debt and revalidates it
    with ``_terminal_still_holds`` before deactivating. The work-ledger probe exposes no
    ``probe.observation``, so the recheck's observation-only classification returned False
    for this kind forever -- the debt was dropped, the loop stayed active, and every
    interval re-observed TERMINAL and redelivered the terminal turn. The recheck must honour
    the kernel's typed terminal: classify from ``verdict.outcome``/``verdict.keys`` (an
    ``all-accepted`` ledger is a success) so the owed settlement is confirmed.
    """
    import kiro_crew.autonudge as _an

    def _ledger_settled(identity, message, probe):
        # The real work-ledger probe sets NO observation; the finish rides the kernel's
        # Outcome.TERMINAL with the ledger's own success key.
        return _an.irq.Verdict(_an.irq.Outcome.TERMINAL, "all items accepted", ("all-accepted",))

    monkeypatch.setattr(_an.irq, "poll", _ledger_settled)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=lambda loop: True)
    monitor = _structured_monitor(kind="work-ledger", target="slack:C123:1700000000.1")
    monitor.terminal_delivered = "success"
    loop = NudgeLoop(
        id="monitor-wl-recheck",
        slot_key="slack:C123:1700000000.1",
        message="wake the conductor from its work ledger",
        idle_secs=30,
        monitor=monitor,
        gate=True,
    )
    service._loops[loop.id] = loop
    try:
        assert (
            await service._terminal_still_holds(loop, monitor) == "holds"
        ), "an all-accepted work-ledger terminal confirms the owed success"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_work_ledger_recheck_with_a_changed_class_drops_the_owed_debt(
    tmp_path, monkeypatch
):
    """A ledger that finished DIFFERENTLY from the owed marker drops the debt, keeps the watch.

    Mirrors the gh-pr classification-change guard: an owed ``success`` that now revalidates
    as a non-success terminal must not settle under the stale marker.
    """
    import kiro_crew.autonudge as _an

    def _ledger_rejected(identity, message, probe):
        # Terminal but NOT all-accepted -> terminal_succeeded() is False -> "blocked".
        return _an.irq.Verdict(_an.irq.Outcome.TERMINAL, "an item was rejected", ("rejected",))

    monkeypatch.setattr(_an.irq, "poll", _ledger_rejected)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=lambda loop: True)
    monitor = _structured_monitor(kind="work-ledger", target="slack:C123:1700000000.1")
    monitor.terminal_delivered = "success"
    loop = NudgeLoop(
        id="monitor-wl-recheck-2",
        slot_key="slack:C123:1700000000.1",
        message="wake the conductor from its work ledger",
        idle_secs=30,
        monitor=monitor,
        gate=True,
    )
    service._loops[loop.id] = loop
    try:
        assert (
            await service._terminal_still_holds(loop, monitor) == "gone"
        ), "a different ending is not the owed one"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_work_ledger_recheck_keeps_a_still_running_watch_alive(tmp_path, monkeypatch):
    """A non-terminal recheck (the ledger is still open) must not settle the watch."""
    import kiro_crew.autonudge as _an

    def _still_open(identity, message, probe):
        return _an.irq.Verdict(_an.irq.Outcome.QUIET, "still working", ())

    monkeypatch.setattr(_an.irq, "poll", _still_open)
    service = AutoNudgeService(base_dir=tmp_path, on_fire=lambda loop: True)
    monitor = _structured_monitor(kind="work-ledger", target="slack:C123:1700000000.1")
    monitor.terminal_delivered = "success"
    loop = NudgeLoop(
        id="monitor-wl-recheck-3",
        slot_key="slack:C123:1700000000.1",
        message="wake the conductor from its work ledger",
        idle_secs=30,
        monitor=monitor,
        gate=True,
    )
    service._loops[loop.id] = loop
    try:
        assert (
            await service._terminal_still_holds(loop, monitor) == "gone"
        ), "an unfinished ledger keeps the watch alive"
    finally:
        service.stop()
        service.stop()


@pytest.mark.asyncio
async def test_a_direct_service_call_is_not_gated(tmp_path):
    """The service also arms loops whose work is NOT the pull request.

    A goal loop, or an app's own timer, can easily MENTION one PR in the message it
    was given -- "drive PR #42 to green" is the ordinary phrasing. Gating by default
    at this layer would throttle such a loop to the quiet-streak floor, and if that
    PR is already merged or closed it would deactivate the loop before its first
    agent turn. The evidence for gating is about monitor_start specifically, so the
    default belongs to that surface and not to everyone who calls ``add``.
    """
    service = AutoNudgeService(base_dir=tmp_path)
    try:
        goal = await service.add(
            "chat-4-400",
            "Keep working the backlog; the tracking PR is "
            "https://github.com/acme/widgets/pull/42",
            idle_secs=300,
        )
        assert goal.gate is False, "a direct call must not inherit the surface default"
        assert goal.monitor is None, "and no monitor may be inferred from the mention"
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored, note",
    [
        ({"gate": None}, "a null is not a decision"),
        ({"gate": "false"}, "and neither is a string"),
        ({}, "and an absent key is a loop nobody chose to gate"),
    ],
)
async def test_every_uncertain_stored_gate_resolves_to_ungated(tmp_path, stored, note):
    """Only an explicit boolean true gates. Everything else stays ungated.

    Two earlier tests here asserted the OPPOSITE -- that a corrupt value and an absent
    key both decode to gated -- on the grounds that reading corrupt data as an opt-out
    would ungate loops nobody chose to ungate. That had the asymmetry backwards:
    gating is the state that can
    silently STOP a loop, because a gated loop whose subject is merged or closed
    DEACTIVATES. Being wrong toward ungated costs a turn per interval, which is what
    today already costs. Being wrong toward gated stops a recurring task because its
    instruction happened to mention a pull request -- and the absent-key case is
    exactly the legacy generic loop that predates this field.
    """
    import json

    async def on_fire(loop):
        return True

    record = {
        "id": "monitor28",
        "slot_key": "chat-1-123",
        "message": "watch https://github.com/acme/widgets/pull/42",
        "idle_secs": 300,
        "active": True,
    }
    record.update(stored)
    (tmp_path / "autonudge.json").write_text(json.dumps({"loops": [record]}), encoding="utf-8")
    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    try:
        await service.start()
        loop = service._loops.get("monitor28")
        assert loop is not None, "the loop must load"
        assert loop.gate is False, note
        assert loop.monitor is None, "and no monitor is inferred for it"
        # A later edit must not gate it either -- that is where a falsy-but-untrusted
        # value can bite, by looking like an opt-out nobody recorded.
        await service.update(loop.id, message="watch https://github.com/acme/widgets/pull/99")
        assert loop.monitor is None, "an edit must not gate a loop that was never gated"
    finally:
        service.stop()


# The corrupt-stored-gate case lives in
# ``test_every_uncertain_stored_gate_resolves_to_ungated`` above, parametrised over
# null, a string and an absent key. A separate test here asserted the opposite
# answer for the same input and was removed rather than left contradicting it.


@pytest.mark.asyncio
async def test_the_opt_out_survives_an_instruction_edit(tmp_path):
    """An explicit opt-out must not be revoked by the documented way to revise.

    The instruction IS the target, so editing it re-infers the subject. A loop
    armed with gate=False would otherwise be silently re-gated by the next wording
    change -- and an ungated loop and a re-gated one look identical until the turns
    stop arriving.
    """

    async def on_fire(loop):
        return True

    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    try:
        loop = await service.add(
            "chat-1-123",
            "Keep the heartbeat fresh while https://github.com/acme/widgets/pull/42 is open",
            idle_secs=30,
            gate=False,
        )
        assert loop.monitor is None

        await service.update(
            loop.id,
            message="Keep the heartbeat fresh while https://github.com/acme/widgets/pull/42 is open",
        )
        assert loop.monitor is None, "an edit must not re-gate an opted-out loop"
        assert loop.gate is False, "and the decision must still be remembered"
    finally:
        service.stop()

    # The decision is persisted, so a fresh service reading the same store agrees.
    revived = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    try:
        await revived.start()
        stored = revived._loops.get(loop.id)
        assert stored is not None and stored.gate is False
    finally:
        revived.stop()


@pytest.mark.asyncio
async def test_the_opt_out_arms_an_ungated_loop(tmp_path):
    """gate=False must reach the SCHEDULER, not just the acknowledgement text.

    If the flag stopped at the ack the loop would be armed gated anyway, and the
    disclosure would be wrong in the other direction -- an act-on-quiet loop told
    it was exempt and then slowed regardless.
    """

    async def on_fire(loop):
        return True

    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    try:
        gated = await service.add(
            "chat-1-123",
            "Watch https://github.com/acme/widgets/pull/42",
            idle_secs=30,
            gate=True,
        )
        assert gated.monitor is not None, "the surface's decision must still gate"

        ungated = await service.add(
            "chat-1-124",
            "Watch https://github.com/acme/widgets/pull/42",
            idle_secs=30,
            gate=False,
        )
        assert ungated.monitor is None, "the opt-out must arm exactly as before the gate"
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_an_unsupported_monitor_version_is_never_armed(tmp_path, monkeypatch):
    """A record from a newer gateway must go inert WITHOUT losing its intent.

    The load path synthesises a BLOCKED outcome for such a record but deliberately
    leaves ``active`` alone, because that flag belongs to the gateway that wrote
    it. Inertness therefore has to come from the arm refusing, and this asserts
    the ABSENCE of a timer -- the bug it guards would have injected the raw
    message every interval with no gate decision at all.
    """
    from kiro_crew.monitoring.models import MONITOR_STATE_VERSION

    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)
        return True

    service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    monitor = _structured_monitor(kind="gh-pr", target="acme/widgets#42")
    monitor.version = MONITOR_STATE_VERSION + 1
    loop = NudgeLoop(
        id="monitor09",
        slot_key="chat-1-123",
        message="Watch https://github.com/acme/widgets/pull/42",
        idle_secs=30,
        monitor=monitor,
        active=True,
    )
    service._loops[loop.id] = loop

    try:
        service._arm_from_deadline(loop)
        assert loop.id not in service._timers, "an unsupported version must get no timer"
        assert loop.active is True, "the newer gateway's intent must survive untouched"
        assert fired == []
    finally:
        service.stop()
