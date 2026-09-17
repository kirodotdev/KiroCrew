"""Regression: a reaped subagent is reported EXACTLY once and frees EXACTLY one slot.

Two paths finish a subagent — ``_force_reap`` and ``_run``'s ``finally`` — and
between them there are FOUR distinct one-time concerns. Earlier revisions tried
to arbitrate them with two flags (``reaped`` and ``done``) and each attempt
satisfied two while breaking a third:

===============================  ==========================================
``reaped`` set after teardown    duplicate delivery
``reaped`` set before teardown   outcome lost if the reaper is cancelled
+ hand the claim back on cancel  outcome lost if ``_run`` already exited
separate ``_finalized`` claim    outcome lost if the claimer is cancelled
claim gated on ``not done``      NO reporter *and* a leaked slot
===============================  ==========================================

The design under test separates all of them:

* ``info.reaped``      — CLASSIFICATION (was this a deliberate reap?). The
  cancel-recovery scheduler reads it; the marker must precede the intentional
  cancel or an unexpected-cancel respawn fires.
* ``if not info.done`` — the terminal RECORD (error/stat/tombstone/cost),
  first-arrival-wins.
* ``_release_slot``    — SLOT accounting, its own one-shot token, so
  ``_running_count`` is decremented exactly once regardless of report or record
  ordering.
* ``_claim_finalize``  — REPORT ownership (``subagent_done`` + ``_on_done``),
  deliberately independent of ``done``, and executed under ``asyncio.shield`` so
  an interrupted claimer cannot strand the outcome.
"""
from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.subagent import (
    SubagentInfo,
    SubagentManager,
    stage_boundary_owner_for_run,
)


def _make_manager(max_concurrent: int = 4) -> SubagentManager:
    mgr = SubagentManager(
        sessions=MagicMock(), ctx_builder=MagicMock(), max_concurrent=max_concurrent
    )
    mgr._fire_event = AsyncMock()
    mgr._write_tombstone = MagicMock()
    mgr._record_cost = MagicMock()
    mgr._on_done = AsyncMock()
    return mgr


def _info(**overrides) -> SubagentInfo:
    info = SubagentInfo(id="a1b2c3d4", task="t", agent="")
    for k, v in overrides.items():
        setattr(info, k, v)
    return info


def _wire_report_boundaries(
    manager: SubagentManager,
    *scopes: tuple[str, str],
):
    from kiro_crew.dashboard.state import StageBoundary

    boundaries = {
        scope: StageBoundary(stage=1, generation=scope[1]) for scope in scopes
    }
    manager._stage_boundary_for_scope = lambda parent, owner: boundaries.get((parent, owner))
    return boundaries


def _done_events(mgr: SubagentManager) -> list:
    """The `subagent_done` fire_event calls only — `_fire_event` carries others."""
    return [c for c in mgr._fire_event.call_args_list if c.args and c.args[0] == "subagent_done"]


async def _noop_reset(session_key):
    await asyncio.sleep(0)


async def _schedule_recovery(mgr: SubagentManager, info: SubagentInfo) -> None:
    """Arm cancel-recovery and let it run to completion.

    `_schedule_cancel_recovery` captures ``asyncio.current_task()`` and its
    respawn waits for that task's teardown to finish, so it must be armed from a
    task that then EXITS — arming it from the test body would sit through the
    real ``_RESET_TIMEOUT + 60`` handshake.
    """
    async def _arm() -> None:
        mgr._schedule_cancel_recovery(info)

    await asyncio.create_task(_arm())
    for _ in range(4):
        await asyncio.gather(*list(mgr._tasks.values()), return_exceptions=True)
        await asyncio.sleep(0)


# ── the claim and the slot token in isolation ────────────────────────


def test_report_claim_granted_exactly_once():
    mgr = _make_manager()
    info = _info()
    assert mgr._claim_finalize(info) is True
    assert mgr._claim_finalize(info) is False


def test_report_claim_ignores_done():
    """The round-5 defect: gating the claim on `done` let both paths decline."""
    mgr = _make_manager()
    assert mgr._claim_finalize(_info(done=True)) is True


def test_report_claim_withheld_but_open_while_recovering():
    mgr = _make_manager()
    info = _info(_recovering=True)
    assert mgr._claim_finalize(info) is False
    assert info._finalized is False, "claim must stay OPEN for the respawn"
    info._recovering = False
    assert mgr._claim_finalize(info) is True


def test_slot_token_granted_exactly_once():
    mgr = _make_manager()
    info = _info()
    assert mgr._release_slot(info) is True
    assert mgr._release_slot(info) is False


def test_slot_token_independent_of_reaped_and_done():
    mgr = _make_manager()
    assert mgr._release_slot(_info(reaped=True, done=True)) is True


# ── the round-5 regression: done set mid-teardown ────────────────────


@pytest.mark.asyncio
async def test_done_set_during_reap_teardown_still_reports_and_frees_one_slot():
    """THE round-5 regression.

    ``_run_inner`` sets ``info.done`` while ``_force_reap`` is suspended in the
    session reset. A reaper gated on ``not info.done`` would then decline the
    report claim, still set ``reaped``, and let ``_run``'s finally skip BOTH its
    claim and its slot decrement — so nothing is reported and ``_running_count``
    stays inflated, starving the spawn queue.
    """
    mgr = _make_manager()
    info = _info(_session_sharing=False)
    mgr._running_count = 1

    async def _reset_then_finish(session_key):
        # A concurrently-finishing _run_inner marking the record terminal.
        info.done = True
        await asyncio.sleep(0)

    mgr._sessions.reset = _reset_then_finish

    await mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="reaped")

    assert len(_done_events(mgr)) == 1, "outcome was not reported exactly once"
    assert mgr._on_done.await_count == 1, "completion never reached the parent"
    assert mgr._running_count == 0, (
        f"slot not freed exactly once (_running_count={mgr._running_count})"
    )


@pytest.mark.asyncio
async def test_reap_after_run_released_slot_does_not_double_decrement():
    """Reverse order: `_run` already freed the slot; the reap must not re-free."""
    mgr = _make_manager()
    info = _info(_session_sharing=False)
    mgr._running_count = 2
    # Stand in for _run's finally having already released.
    assert mgr._release_slot(info) is True
    mgr._running_count -= 1

    mgr._sessions.reset = _noop_reset
    await mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="reaped")

    assert mgr._running_count == 1, "slot double-decremented"


# ── exactly-once reporting across the racing paths ───────────────────


@pytest.mark.asyncio
async def test_run_claiming_during_reap_teardown_yields_one_report():
    """A `_run` finishing inside the reap's teardown window claims first."""
    mgr = _make_manager()
    info = _info(_session_sharing=False)
    mgr._running_count = 1
    reports: list[str] = []

    async def _reset_with_race(session_key):
        if mgr._claim_finalize(info):
            reports.append("run")
        await asyncio.sleep(0)

    mgr._sessions.reset = _reset_with_race

    await mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="reaped")

    assert reports == ["run"]
    # The reap lost the claim, so it must not have reported.
    assert len(_done_events(mgr)) == 0
    assert mgr._on_done.await_count == 0


@pytest.mark.asyncio
async def test_uncontested_reap_reports_once():
    mgr = _make_manager()
    info = _info(_session_sharing=False)
    mgr._running_count = 1
    mgr._sessions.reset = _noop_reset

    await mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="reaped")

    assert len(_done_events(mgr)) == 1
    assert mgr._on_done.await_count == 1
    assert info.done is True and info.reaped is True
    mgr._write_tombstone.assert_called_once()


# ── the shield: an interrupted claimer still delivers ────────────────


@pytest.mark.asyncio
async def test_cancel_during_subagent_done_still_delivers_once():
    """Cancelling the claimer while it fires `subagent_done`.

    The report runs on a shielded task, so it COMPLETES (delivery reaches the
    parent) even though the caller receives CancelledError. Asserts completion,
    not mere invocation — a cancelled-mid-flight injection is invoked but never
    completes, which is how an earlier revision's test passed against broken
    code.
    """
    mgr = _make_manager()
    info = _info(_session_sharing=False)
    mgr._running_count = 1
    mgr._sessions.reset = _noop_reset
    delivered: list[str] = []

    # Deterministic rendezvous instead of a wall-clock bet: `entered` tells the
    # test exactly when the shielded report is in flight (so cancelling before
    # that would race a step that hasn't started, and cancelling after it
    # completes wouldn't be a cancel-mid-flight at all), and `release` lets the
    # test decide exactly when the shielded report is allowed to finish, so its
    # completion can be asserted separately from the awaiter's cancellation.
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _slow_event(name, _info, _payload=None):
        if name == "subagent_done":
            entered.set()
            await release.wait()

    async def _record_delivery(_info):
        delivered.append("done")

    mgr._fire_event = AsyncMock(side_effect=_slow_event)
    mgr._on_done = AsyncMock(side_effect=_record_delivery)

    task = asyncio.ensure_future(
        mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="reaped")
    )
    await asyncio.wait_for(entered.wait(), timeout=2)  # now inside the shielded report
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The shielded report survives its awaiter's cancellation -- release it
    # and confirm it still completes delivery.
    release.set()
    await asyncio.wait_for(
        asyncio.gather(*[t for t in mgr._report_tasks if not t.done()]), timeout=2
    )
    assert delivered == ["done"], "shielded report did not complete delivery"


@pytest.mark.asyncio
async def test_cancel_during_on_done_produces_no_second_delivery():
    mgr = _make_manager()
    info = _info(_session_sharing=False)
    mgr._running_count = 1
    mgr._sessions.reset = _noop_reset
    calls: list[str] = []
    entered = asyncio.Event()

    async def _slow_on_done(_info):
        calls.append("start")
        entered.set()
        await asyncio.sleep(0.05)
        calls.append("end")

    mgr._on_done = AsyncMock(side_effect=_slow_on_done)

    task = asyncio.ensure_future(
        mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="reaped")
    )
    # Wait until _on_done is actually executing before cancelling — avoids a
    # race under heavy xdist load where asyncio.sleep(0.01) could expire after
    # _force_reap already completed, so the cancel would be a no-op.
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(
        asyncio.gather(*[t for t in mgr._report_tasks if not t.done()]), timeout=2
    )

    # Exactly one injection, and the claim is consumed so no other path retries.
    assert calls == ["start", "end"]
    assert info._finalized is True


# ── the classification contract is untouched ─────────────────────────


@pytest.mark.asyncio
async def test_reaped_precedes_intentional_cancel():
    """A cancel seen with `reaped is False` is read as an UNEXPECTED external
    cancel and the run is respawned — so the marker must still be set first."""
    mgr = _make_manager()
    info = _info(_session_sharing=False)
    mgr._running_count = 1
    mgr._sessions.reset = _noop_reset
    seen: dict[str, bool] = {}

    async def _never():
        await asyncio.sleep(3600)

    task = asyncio.ensure_future(_never())
    mgr._tasks["a1b2c3d4"] = task
    real_cancel = mgr._cancel_task_intentionally

    def _spy(t, i, *, reason=""):
        seen["reaped_at_cancel"] = i.reaped
        return real_cancel(t, i, reason=reason)

    mgr._cancel_task_intentionally = _spy

    await mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="reaped")

    assert seen["reaped_at_cancel"] is True
    task.cancel()


# ── reap during _recovering must not strand the outcome ──────────────


@pytest.mark.asyncio
async def test_reap_during_recovering_still_reports_and_supersedes_respawn():
    """A user Stop / reap landing inside the cancel-recovery window.

    `_claim_finalize` withholds the claim while `_recovering` so a pending
    respawn is not reported done prematurely. But a reap is DEFINITIVELY
    terminal: a reap that only refused the claim would do its teardown, set
    `reaped=True` and report nothing — and `_resume`'s `reaped` abort path
    bare-returns, so no path ever reported. The agent sat unfinished until the
    reaper's wall-clock deadline.
    """
    mgr = _make_manager()
    info = _info(_session_sharing=False, _recovering=True)
    mgr._running_count = 1
    mgr._sessions.reset = _noop_reset

    await mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="reaped")

    assert len(_done_events(mgr)) == 1, "reap during recovery reported nothing"
    assert mgr._on_done.await_count == 1, "completion never reached the parent"
    assert info._recovering is False, "a killed agent must not stay pending a respawn"
    assert info.done is True
    assert mgr._running_count == 0


@pytest.mark.asyncio
async def test_reap_cancels_pending_recovery_task():
    """The superseded respawn task is cancelled, not left in its ~90s wait."""
    mgr = _make_manager()
    info = _info(_session_sharing=False, _recovering=True)
    mgr._running_count = 1
    mgr._sessions.reset = _noop_reset

    async def _long_wait():
        await asyncio.sleep(3600)

    recovery = asyncio.ensure_future(_long_wait())
    mgr._tasks["a1b2c3d4:recovery"] = recovery

    await mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="reaped")

    assert recovery.cancelled() or recovery.done(), "recovery task left running"
    assert "a1b2c3d4:recovery" not in mgr._tasks


@pytest.mark.asyncio
async def test_run_path_claim_still_withheld_during_recovering():
    """The override is scoped to terminal callers only — `_run`'s finally must
    still leave the claim OPEN for the respawned run."""
    mgr = _make_manager()
    info = _info(_recovering=True)
    assert mgr._claim_finalize(info) is False
    assert info._finalized is False
    assert info._recovering is True, "the non-terminal path must not clear it"


# ── terminal report outcomes must reach parent settlement ────────────


@pytest.mark.asyncio
async def test_terminal_report_failure_reaches_parent_barrier():
    """A handled announce failure is still a failed boundary-owned delivery."""
    from kiro_crew.subagent import SubagentReportDeliveryError

    mgr = _make_manager()
    owner = "stage-owner"
    info = _info(parent_session_key="dashboard:parent", _stage_boundary_owner=owner)
    mgr._on_done = AsyncMock(side_effect=RuntimeError("parent delivery failed"))
    await mgr._spawn_terminal_report(
        info,
        source="test",
        injection_timeout_reason="delivery failed",
        mark_delivered_on_success=False,
    )
    with pytest.raises(SubagentReportDeliveryError):
        await mgr.wait_for_parent_reports(info.parent_session_key, owner)


@pytest.mark.asyncio
async def test_unrelated_unowned_report_failure_neither_halts_nor_advances_stage():
    """An ordinary report cannot poison or bypass a later stage barrier."""
    mgr = _make_manager()
    parent, owner = "dashboard:parent", "later-stage-owner"
    mgr._latch_report_failure(_info(parent_session_key=parent))
    assert mgr._boundary_report_payloads == {}

    release = asyncio.Event()
    report = asyncio.create_task(release.wait())
    mgr._report_owners[report] = _info(
        parent_session_key=parent, _stage_boundary_owner=owner
    )
    waiter = asyncio.create_task(mgr.wait_for_parent_reports(parent, owner))
    await asyncio.sleep(0)
    assert not waiter.done(), "unrelated failure halted or advanced the owned barrier"
    release.set()
    assert await waiter is True


@pytest.mark.asyncio
async def test_saturated_report_scope_blocks_only_itself_until_discard(monkeypatch):
    """A saturated boundary cannot block or retain debt for another boundary."""
    import kiro_crew.subagent as mod
    from kiro_crew.subagent import SubagentReportDeliveryError

    monkeypatch.setattr(mod, "_REPORT_FAILURES_PER_PARENT_CAP", 1)
    manager = _make_manager()
    other_scope = ("dashboard:other", "other-stage")
    saturated_scope = ("dashboard:saturated", "saturated-stage")
    boundaries = _wire_report_boundaries(manager, other_scope, saturated_scope)
    other = _info(
        id="other-report",
        parent_session_key=other_scope[0],
        _stage_boundary_owner=other_scope[1],
    )
    saturated = [
        _info(
            id=f"saturated-{index}",
            parent_session_key=saturated_scope[0],
            _stage_boundary_owner=saturated_scope[1],
        )
        for index in range(2)
    ]
    manager._latch_report_failure(other)
    for info in saturated:
        manager._latch_report_failure(info)
    manager._run_terminal_report = AsyncMock(return_value=True)

    with pytest.raises(SubagentReportDeliveryError, match="row cap"):
        await manager.wait_for_parent_reports(*saturated_scope)
    assert await manager.wait_for_parent_reports(*other_scope) is True
    assert manager._boundary_report_payloads == {}
    assert boundaries[saturated_scope].report_retention_refused == "row_cap"
    assert boundaries[other_scope].report_retention_refused is None

    manager.discard_report_failures(*saturated_scope)
    assert boundaries[saturated_scope].report_retention_refused is None
    assert await manager.wait_for_parent_reports(*saturated_scope) is False


@pytest.mark.asyncio
async def test_report_failure_byte_budget_rejects_only_the_new_row(monkeypatch):
    """A budget-rejected boundary fails alone while older rows still settle."""
    import kiro_crew.subagent as mod
    from kiro_crew.subagent import SubagentReportDeliveryError

    monkeypatch.setattr(mod, "_REPORT_FAILURE_BYTE_BUDGET", 1_000_000, raising=False)
    monkeypatch.setattr(mod, "_REPORT_FAILURES_PER_PARENT_CAP", 1)
    manager = _make_manager()
    scopes = [(f"dashboard:scope-{index}", f"owner-{index}") for index in range(3)]
    boundaries = _wire_report_boundaries(manager, *scopes)
    for index, (parent, owner) in enumerate(scopes[:2]):
        manager._latch_report_failure(
            _info(
                id=f"scope-{index}",
                parent_session_key=parent,
                _stage_boundary_owner=owner,
            )
        )
    retained_before_rejection = manager._retained_report_failure_bytes
    monkeypatch.setattr(mod, "_REPORT_FAILURE_BYTE_BUDGET", retained_before_rejection)
    manager._latch_report_failure(
        _info(
            id="scope-2",
            task="oversized" * 100,
            parent_session_key=scopes[2][0],
            _stage_boundary_owner=scopes[2][1],
        )
    )
    manager._run_terminal_report = AsyncMock(return_value=True)

    with pytest.raises(SubagentReportDeliveryError, match="byte budget"):
        await manager.wait_for_parent_reports(*scopes[-1])
    assert boundaries[scopes[-1]].report_retention_refused == "byte_budget"
    assert manager._retained_report_failure_bytes == retained_before_rejection
    assert await manager.wait_for_parent_reports(*scopes[0]) is True
    assert await manager.wait_for_parent_reports(*scopes[1]) is True
    assert manager._retained_report_failure_bytes == 0
    assert manager._boundary_report_payloads == {}

    manager.discard_report_failures(*scopes[-1])
    assert boundaries[scopes[-1]].report_retention_refused is None


@pytest.mark.parametrize("linked_parent", ["", "slack:C123:1712345678.900"])
@pytest.mark.asyncio
async def test_slot_teardown_discards_exact_failure_scopes_for_its_boundary(
    tmp_path,
    monkeypatch,
    linked_parent,
):
    """Dashboard and channel slot teardown release every owned failure scope."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat_handlers import close_slot
    from kiro_crew.history import ConversationLog

    monkeypatch.setattr("kiro_crew.autonudge._INSTANCE", None)
    state = _make_state(tmp_path)
    state.conversation_log = ConversationLog()
    state.sessions.get_provider.return_value = None
    state.sessions.remove = AsyncMock()
    slot = state.get_or_create_slot("failure-scope-owner")
    slot.linked_session_key = linked_parent
    dashboard_parent = f"dashboard:{slot.key}"
    owned_parents = {dashboard_parent}
    if linked_parent:
        owned_parents.add(linked_parent)
    slot.stage_boundary.arm(1)
    slot.stage_boundary.parent_session_keys.update(owned_parents)
    owned_owner = slot.stage_boundary.owner
    assert owned_owner

    manager = _make_manager()
    state.subagents = manager
    for parent_index, parent in enumerate(sorted(owned_parents)):
        manager._latch_report_failure(
            _info(
                id=f"owned-{parent_index}",
                parent_session_key=parent,
                _stage_boundary_owner=owned_owner,
            )
        )
    foreign_scope = ("dashboard:foreign", "foreign-owner")
    manager._latch_report_failure(
        _info(
            id="foreign",
            parent_session_key=foreign_scope[0],
            _stage_boundary_owner=foreign_scope[1],
        )
    )
    assert len(manager._boundary_report_payloads) > 1
    retained_before_close = manager._retained_report_failure_bytes
    assert retained_before_close > 0

    await close_slot(state, slot, slot.key)

    assert set(manager._boundary_report_payloads) == {foreign_scope}
    assert 0 < manager._retained_report_failure_bytes < retained_before_close


@pytest.mark.asyncio
async def test_slot_teardown_preserves_sibling_alias_failure_scope(tmp_path, monkeypatch):
    """Closing alias A cannot discard alias B's exact boundary debt."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat_handlers import close_slot
    from kiro_crew.history import ConversationLog

    monkeypatch.setattr("kiro_crew.autonudge._INSTANCE", None)
    state = _make_state(tmp_path)
    state.conversation_log = ConversationLog()
    state.sessions.get_provider.return_value = None
    state.sessions.remove = AsyncMock()
    parent = "slack:C123:1712345678.901"
    first = state.get_or_create_slot("alias-a")
    second = state.get_or_create_slot("alias-b")
    for slot in (first, second):
        slot.linked_session_key = parent
        slot.stage_boundary.arm(1)
        slot.stage_boundary.parent_session_keys.add(parent)

    first_scope = (parent, first.stage_boundary.owner or "")
    second_scope = (parent, second.stage_boundary.owner or "")
    manager = _make_manager()
    state.subagents = manager
    manager._latch_report_failure(
        _info(
            id="alias-a-report",
            parent_session_key=first_scope[0],
            _stage_boundary_owner=first_scope[1],
        )
    )
    manager._latch_report_failure(
        _info(
            id="alias-b-report",
            parent_session_key=second_scope[0],
            _stage_boundary_owner=second_scope[1],
        )
    )

    await close_slot(state, first, first.key)

    assert set(manager._boundary_report_payloads) == {second_scope}


@pytest.mark.asyncio
async def test_aborted_slot_teardown_preserves_failure_scopes(tmp_path, monkeypatch):
    """A failed history commit restores both the slot and its failure scope."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.history import ConversationLog

    monkeypatch.setattr("kiro_crew.autonudge._INSTANCE", None)
    state = _make_state(tmp_path)
    state.conversation_log = ConversationLog()
    state.sessions.get_provider.return_value = None
    state.sessions.remove = AsyncMock()
    slot = state.get_or_create_slot("failed-scope-close")
    slot.stage_boundary.arm(1)
    scope = (f"dashboard:{slot.key}", slot.stage_boundary.owner or "")
    manager = _make_manager()
    state.subagents = manager
    manager._latch_report_failure(
        _info(
            id="failed-close-report",
            parent_session_key=scope[0],
            _stage_boundary_owner=scope[1],
        )
    )
    retained_before_close = manager._retained_report_failure_bytes
    monkeypatch.setattr(
        chat_handlers,
        "save_slot_off_loop",
        AsyncMock(side_effect=OSError("history unavailable")),
    )

    with pytest.raises(chat_handlers.SlotCloseError):
        await chat_handlers.close_slot(state, slot, slot.key)

    assert state.get_slot(slot.key) is slot
    assert set(manager._boundary_report_payloads) == {scope}
    assert manager._retained_report_failure_bytes == retained_before_close


@pytest.mark.asyncio
async def test_parent_pending_work_includes_live_followup_watcher():
    """A parent-owned follow-up watcher holds stage settlement open."""
    manager = _make_manager()
    parent = "dashboard:parent"
    info = _info(id="followup", done=True, parent_session_key=parent)
    release = asyncio.Event()
    watcher = asyncio.create_task(release.wait())
    manager._agents = {info.id: info}
    manager._followup_watchers = {info.id: watcher}
    manager._followup_watcher_parents = {info.id: parent}
    manager._queued_depth = MagicMock(return_value=0)
    manager._queued_depth_async = AsyncMock(return_value=0)

    try:
        assert manager.has_pending_work_for(parent) is True
        assert await manager.has_pending_work_for_async(parent) is True
        assert manager.has_pending_work_for("dashboard:other") is False
    finally:
        release.set()
        await watcher


@pytest.mark.asyncio
async def test_resident_report_failure_redelivers_from_boundary_payload():
    """A failed payload is boundary-owned before its record reaches the cap."""
    manager = _make_manager()
    parent, owner = "dashboard:resident", "resident-stage"
    info = _info(
        id="resident-failure",
        done=True,
        parent_session_key=parent,
        _stage_boundary_owner=owner,
    )
    manager._agents = {info.id: info}
    manager._latch_report_failure(info)
    manager._run_terminal_report = AsyncMock(return_value=True)

    assert await manager.wait_for_parent_reports(parent, owner) is True
    manager._run_terminal_report.assert_awaited_once()
    redelivered = manager._run_terminal_report.await_args.args[0]
    assert redelivered is not info
    assert redelivered.id == info.id
    assert redelivered.parent_session_key == parent
    assert stage_boundary_owner_for_run(redelivered) == owner
    assert info.id in manager._agents
    assert info._report_failure_latched is False
    assert manager._boundary_report_payloads == {}


@pytest.mark.asyncio
async def test_retained_report_payload_caps_text_bytes_and_redelivers(monkeypatch):
    """A bounded snapshot preserves delivery text and terminal metadata."""
    import kiro_crew.subagent as mod
    from kiro_crew.subagent import SubagentInfo, _ReportFailureSnapshot

    cap = 96
    monkeypatch.setattr(mod, "_REPORT_FAILURE_PAYLOAD_MAX_BYTES", cap, raising=False)
    manager = _make_manager()
    parent, owner = "dashboard:oversized", "oversized-owner"
    info = _info(
        id="oversized-report",
        done=True,
        parent_session_key=parent,
        _stage_boundary_owner=owner,
        _stage_boundary_cancelled=True,
        result="result🙂" * 100,
        result_path="/runs/oversized-report/result.txt",
        result_truncated=True,
        task="task🙂" * 100,
        error="error🙂" * 100,
        user_stopped=True,
        partial=True,
        agent="reviewer",
        silent=True,
        conversation_key="subagent:origin",
        model="requested-direct",
        requested_model="requested-effective",
        resolved_model="served-model",
        stop_reason="cancelled",
        stop_class="cancelled",
        batch_id="wave42",
        batch_total=7,
        _digest_held=True,
        _digest_flush_only=True,
        _digest_settle_ids=["held-a", "held-b"],
        _delivery_queued=True,
    )
    info.history = ["history" * 100_000]
    info.messages = ["message" * 100_000]

    manager._latch_report_failure(info)
    retained = manager._boundary_report_payloads[(parent, owner)][info.id]

    assert isinstance(retained, _ReportFailureSnapshot)
    assert not isinstance(retained, SubagentInfo)
    assert retained is not info
    assert set(retained.__dataclass_fields__) == _terminal_report_consumer_fields()
    assert not hasattr(retained, "history")
    assert not hasattr(retained, "messages")
    assert retained.id == info.id
    assert retained.parent_session_key == parent
    assert retained.outcome == info.outcome == "stopped"
    assert retained.user_stopped is info.user_stopped is True
    assert retained._stage_boundary_cancelled is info._stage_boundary_cancelled is True
    assert retained.partial is info.partial is True
    assert retained.result_path == info.result_path
    assert retained.result_truncated is info.result_truncated is True
    assert retained.agent == info.agent
    assert retained.silent is info.silent is True
    assert retained.conversation_key == info.conversation_key
    assert retained.model == info.model
    assert retained.requested_model == info.requested_model
    assert retained.resolved_model == info.resolved_model
    assert retained.stop_reason == info.stop_reason
    assert retained.stop_class == info.stop_class
    assert retained.batch_id == info.batch_id
    assert retained.batch_total == info.batch_total
    assert retained._digest_held is info._digest_held is True
    assert retained._digest_flush_only is info._digest_flush_only is True
    assert retained._digest_settle_ids == tuple(info._digest_settle_ids)
    assert retained._delivery_queued is info._delivery_queued is True
    for field in ("result", "task", "error"):
        value = getattr(retained, field)
        assert len(value.encode("utf-8")) <= cap
        assert "[truncated " in value
        assert " bytes]" in value

    manager._run_terminal_report = AsyncMock(return_value=True)
    assert await manager.wait_for_parent_reports(parent, owner) is True
    redelivered = manager._run_terminal_report.await_args.args[0]
    assert redelivered is not info
    assert redelivered.outcome == info.outcome
    assert redelivered.user_stopped is info.user_stopped
    assert redelivered._stage_boundary_cancelled is info._stage_boundary_cancelled
    assert redelivered.partial is info.partial
    assert redelivered.result_path == info.result_path
    assert redelivered.result_truncated is info.result_truncated
    assert redelivered.agent == info.agent
    assert redelivered.silent is info.silent
    assert redelivered.conversation_key == info.conversation_key
    assert redelivered.model == info.model
    assert redelivered.requested_model == info.requested_model
    assert redelivered.resolved_model == info.resolved_model
    assert redelivered.stop_reason == info.stop_reason
    assert redelivered.stop_class == info.stop_class
    assert redelivered.batch_id == info.batch_id
    assert redelivered.batch_total == info.batch_total
    assert redelivered._digest_held is info._digest_held
    assert redelivered._digest_flush_only is info._digest_flush_only
    assert redelivered._digest_settle_ids == info._digest_settle_ids
    assert redelivered._delivery_queued is info._delivery_queued
    for field in ("result", "task", "error"):
        assert "[truncated " in getattr(redelivered, field)
    assert len(info.task.encode("utf-8")) > cap
    assert manager._boundary_report_payloads == {}


def test_report_failure_counts_are_derived_from_payload_rows():
    """No count-only state exists outside exact per-scope payload buckets."""
    manager = _make_manager()
    parent, owner = "dashboard:derived", "derived-stage"
    infos = [
        _info(
            id=f"derived-{index}",
            parent_session_key=parent,
            _stage_boundary_owner=owner,
        )
        for index in range(2)
    ]

    assert not hasattr(manager, "_report_failures")
    for info in infos:
        manager._latch_report_failure(info)
    assert manager._peek_report_failures(parent, owner) == 2

    manager._clear_report_failure(infos[0])
    assert manager._peek_report_failures(parent, owner) == 1
    assert list(manager._boundary_report_payloads[(parent, owner)]) == [infos[1].id]


def test_report_failure_payload_rows_are_bounded_per_scope(monkeypatch):
    """Each boundary admits its own capped snapshot rows."""
    import kiro_crew.subagent as mod

    monkeypatch.setattr(mod, "_REPORT_FAILURES_PER_PARENT_CAP", 2)
    manager = _make_manager()
    infos = [
        _info(id="first-0", parent_session_key="first", _stage_boundary_owner="first"),
        _info(id="first-1", parent_session_key="first", _stage_boundary_owner="first"),
        _info(id="second-0", parent_session_key="second", _stage_boundary_owner="second"),
        _info(id="third-0", parent_session_key="third", _stage_boundary_owner="third"),
        _info(id="fourth-0", parent_session_key="fourth", _stage_boundary_owner="fourth"),
    ]
    for info in infos:
        manager._latch_report_failure(info)

    assert set(manager._boundary_report_payloads) == {
        ("first", "first"),
        ("second", "second"),
        ("third", "third"),
        ("fourth", "fourth"),
    }
    assert sum(len(rows) for rows in manager._boundary_report_payloads.values()) == 5
    assert all(info._report_failure_latched for info in infos)


@pytest.mark.asyncio
async def test_saturated_report_payload_boundary_stays_blocked_until_discard(monkeypatch):
    """An over-cap report blocks only its boundary until explicit discard."""
    import kiro_crew.subagent as mod
    from kiro_crew.subagent import SubagentReportDeliveryError

    monkeypatch.setattr(mod, "_REPORT_FAILURES_PER_PARENT_CAP", 1)
    manager = _make_manager()
    scope = ("dashboard:saturated", "saturated-stage")
    boundary = _wire_report_boundaries(manager, scope)[scope]
    infos = [
        _info(
            id=f"saturated-{index}",
            parent_session_key=scope[0],
            _stage_boundary_owner=scope[1],
        )
        for index in range(3)
    ]
    for info in infos:
        manager._latch_report_failure(info)

    assert boundary.report_retention_refused == "row_cap"
    assert list(manager._boundary_report_payloads[scope]) == [infos[0].id]
    assert infos[-1]._report_failure_latched is False
    manager._run_terminal_report = AsyncMock(return_value=True)

    with pytest.raises(SubagentReportDeliveryError, match="row cap"):
        await manager.wait_for_parent_reports(*scope)
    assert manager._run_terminal_report.await_count == 1
    assert manager._boundary_report_payloads == {}
    assert boundary.report_retention_refused == "row_cap"

    manager.discard_report_failures(*scope)
    assert boundary.report_retention_refused is None
    assert await manager.wait_for_parent_reports(*scope) is False


@pytest.mark.asyncio
async def test_report_failure_refusals_are_boundary_local(monkeypatch):
    """Many saturated boundaries store only one flag on each live boundary."""
    import kiro_crew.subagent as mod
    from kiro_crew.subagent import SubagentReportDeliveryError

    monkeypatch.setattr(mod, "_REPORT_FAILURES_PER_PARENT_CAP", 0)
    manager = _make_manager()
    scopes = [(f"parent-{index}", f"owner-{index}") for index in range(8)]
    boundaries = _wire_report_boundaries(manager, *scopes)
    for index, (parent, owner) in enumerate(scopes):
        for attempt in range(2):
            manager._latch_report_failure(
                _info(
                    id=f"saturated-{index}-{attempt}",
                    parent_session_key=parent,
                    _stage_boundary_owner=owner,
                )
            )

    assert manager._boundary_report_payloads == {}
    assert all(boundary.report_retention_refused == "row_cap" for boundary in boundaries.values())

    with pytest.raises(SubagentReportDeliveryError, match="row cap"):
        await manager.wait_for_parent_reports(*scopes[0])
    manager.discard_report_failures(*scopes[0])
    assert boundaries[scopes[0]].report_retention_refused is None
    assert all(
        boundaries[scope].report_retention_refused == "row_cap"
        for scope in scopes[1:]
    )
    assert await manager.wait_for_parent_reports(*scopes[0]) is False
    with pytest.raises(SubagentReportDeliveryError, match="row cap"):
        await manager.wait_for_parent_reports(*scopes[1])


@pytest.mark.asyncio
async def test_settle_before_delete_waits_for_inflight_terminal_report():
    """Deletion cannot remove a run while its claimed report is still active."""
    manager = _make_manager()
    info = _info(
        id="delete-reporting",
        done=True,
        parent_session_key="dashboard:delete-reporting",
        _stage_boundary_owner="delete-report-owner",
    )
    manager._agents[info.id] = info
    manager._tasks[info.id] = MagicMock()
    started = asyncio.Event()
    release = asyncio.Event()

    async def _report(*_args, **_kwargs) -> bool:
        started.set()
        await release.wait()
        return True

    manager._report_terminal = AsyncMock(side_effect=_report)
    report = manager._spawn_terminal_report(
        info,
        source="test",
        injection_timeout_reason="test",
        mark_delivered_on_success=False,
    )
    await started.wait()
    deletion = asyncio.create_task(
        manager.settle_before_delete(info.id, info._stage_boundary_owner)
    )
    await asyncio.sleep(0)
    waited = not deletion.done()
    stayed_registered = manager._agents.get(info.id) is info

    release.set()
    assert await report is True
    assert await deletion == "delivered"
    assert waited is True
    assert stayed_registered is True
    assert info.id not in manager._agents
    assert info.id not in manager._tasks


@pytest.mark.asyncio
async def test_settle_before_delete_keeps_run_until_report_delivery_succeeds():
    """Delete settlement removes a finished run only after delivery succeeds."""
    manager = _make_manager()
    parent, owner = "dashboard:delete", "delete-owner"
    info = _info(
        id="delete-pending",
        done=True,
        parent_session_key=parent,
        _stage_boundary_owner=owner,
    )
    manager._agents[info.id] = info
    manager._tasks[info.id] = MagicMock()
    manager._latch_report_failure(info)
    manager._run_terminal_report = AsyncMock(side_effect=[False, True])

    assert await manager.settle_before_delete(info.id, owner) == "pending"
    assert manager._agents[info.id] is info
    assert info.id in manager._tasks
    assert info._report_failure_latched is True

    assert await manager.settle_before_delete(info.id, owner) == "delivered"
    assert info.id not in manager._agents
    assert info.id not in manager._tasks
    assert info._report_failure_latched is False


@pytest.mark.asyncio
async def test_settle_before_delete_discards_debt_for_an_inactive_boundary():
    """Deleting a finished run drops debt after its boundary is gone."""
    manager = _make_manager()
    info = _info(
        id="delete-gone",
        done=True,
        parent_session_key="dashboard:gone",
        _stage_boundary_owner="gone-owner",
    )
    manager._agents[info.id] = info
    manager._tasks[info.id] = MagicMock()
    manager._latch_report_failure(info)

    assert await manager.settle_before_delete(info.id, "") == "delivered"
    assert manager._boundary_report_payloads == {}
    assert info.id not in manager._agents
    assert info.id not in manager._tasks
    assert info._report_failure_latched is False


def test_report_failure_payloads_bound_each_scope_bucket(monkeypatch):
    """Every scope caps retained rows without inventing delivery success."""
    import kiro_crew.subagent as mod

    monkeypatch.setattr(mod, "_REPORT_FAILURES_PER_PARENT_CAP", 2)
    mgr = _make_manager()
    for index in range(5):
        mgr._latch_report_failure(
            _info(
                id=f"first-{index}",
                parent_session_key="first",
                _stage_boundary_owner="first",
            )
        )
    for key in ("second", "third"):
        mgr._latch_report_failure(
            _info(id=key, parent_session_key=key, _stage_boundary_owner=key)
        )

    assert len(mgr._boundary_report_payloads) == 3
    assert all(len(rows) <= 3 for rows in mgr._boundary_report_payloads.values())
    settled = _info(
        id="third-extra",
        parent_session_key="third",
        _stage_boundary_owner="third",
    )
    mgr._latch_report_failure(settled)
    assert settled._report_failure_latched is True
    assert mgr._peek_report_failures("third", "third") == 2
    mgr._clear_report_failure(settled)
    assert settled._report_failure_latched is False
    assert mgr._peek_report_failures("third", "third") == 1


# ── the shutdown drain must not abandon stragglers ───────────────────


@pytest.mark.asyncio
async def test_cancel_all_cancels_reports_that_exceed_the_drain_timeout(monkeypatch):
    """`asyncio.wait` returns on timeout WITHOUT touching pending tasks.

    Leaving them pending is worse than not shielding: shutdown proceeds while
    they keep invoking `_on_done` against tearing-down state, then they die when
    the loop closes. They must be cancelled and gathered.
    """
    import kiro_crew.subagent as mod

    monkeypatch.setattr(mod, "_REPORT_DRAIN_TIMEOUT", 0.05)
    mgr = _make_manager()

    started = asyncio.Event()

    async def _hangs_forever():
        started.set()
        await asyncio.sleep(3600)

    task = asyncio.ensure_future(_hangs_forever())
    mgr._report_tasks.add(task)
    await started.wait()

    await mgr.cancel_all()

    assert task.done(), "straggler report was abandoned, not cancelled"
    assert task.cancelled(), "straggler should have been cancelled"


@pytest.mark.asyncio
async def test_cancel_all_readmits_an_undelivered_report_to_orphan_recovery(monkeypatch):
    """A report cancelled by shutdown must not become an unrecoverable loss.

    The terminal RECORD (including the tombstone) is written before delivery is
    attempted, and `list_orphans()` uses the tombstone to EXCLUDE a folder from
    the next start's reconciliation. So cancelling a still-pending report leaves
    an outcome that was never injected AND is invisible to the only path that
    could still inject it. Shutdown must clear the tombstone in that case.
    """
    import kiro_crew.subagent as mod

    monkeypatch.setattr(mod, "_REPORT_DRAIN_TIMEOUT", 0.05)
    mgr = _make_manager()
    info = _info()
    cleared: list[str] = []
    monkeypatch.setattr(mod, "clear_tombstone", lambda aid: (cleared.append(aid), True)[1])

    started = asyncio.Event()

    async def _wedged_delivery(_info):
        started.set()
        await asyncio.sleep(3600)

    mgr._on_done = AsyncMock(side_effect=_wedged_delivery)
    assert mgr._claim_finalize(info) is True
    task = mgr._spawn_terminal_report(
        info,
        source="test",
        injection_timeout_reason="r",
        mark_delivered_on_success=True,
    )
    await started.wait()

    await mgr.cancel_all()

    assert task.cancelled(), "straggler should have been cancelled"
    assert cleared == [info.id], (
        "undelivered completion was left tombstoned — unrecoverable on restart"
    )


@pytest.mark.asyncio
async def test_cancel_all_keeps_the_tombstone_when_delivery_already_happened(monkeypatch):
    """The converse: a report cancelled AFTER `_on_done` returned is delivered.

    Re-admitting it would make the next start inject the same completion a
    second time — the duplicate delivery this PR exists to remove. Only
    `_reported_to_parent == False` may be re-admitted.
    """
    import kiro_crew.subagent as mod

    monkeypatch.setattr(mod, "_REPORT_DRAIN_TIMEOUT", 0.05)
    mgr = _make_manager()
    info = _info()
    cleared: list[str] = []
    monkeypatch.setattr(mod, "clear_tombstone", lambda aid: (cleared.append(aid), True)[1])

    delivered = asyncio.Event()

    async def _on_done(_info):
        delivered.set()

    mgr._on_done = AsyncMock(side_effect=_on_done)
    # A teardown gate that never opens, so the report is cancelled in the wait
    # that follows a SUCCESSFUL delivery.
    never = asyncio.Event()
    assert mgr._claim_finalize(info) is True
    mgr._spawn_terminal_report(
        info,
        source="test",
        injection_timeout_reason="r",
        mark_delivered_on_success=True,
        teardown_done=never,
    )
    await delivered.wait()
    await asyncio.sleep(0)

    await mgr.cancel_all()

    assert info._reported_to_parent is True, "delivery marker not set after _on_done"
    assert cleared == [], "a delivered completion was re-admitted — restart will duplicate it"


# ── every reporter goes through the claim, including recovery failure ─


@pytest.mark.asyncio
async def test_recovery_failure_reports_through_the_claim():
    """A failed cancel-recovery respawn must report via `_claim_finalize`.

    This site must not fire `subagent_done` and `_on_done` DIRECTLY, gated only
    on `done`/`reaped` — a fourth reporter outside the claim, so it could
    deliver on top of a concurrent reaper. Here the claim is already spent (as
    a reaper would leave it) and the respawn is forced to fail, so a compliant
    implementation stays silent.
    """
    mgr = _make_manager()
    info = _info(_session_sharing=False)
    # Someone (the reaper) already owns and completed the report.
    assert mgr._claim_finalize(info) is True

    def _boom(_info):
        raise RuntimeError("respawn failed")

    mgr._run = _boom  # type: ignore[assignment]
    await _schedule_recovery(mgr, info)

    assert _done_events(mgr) == [], "recovery failure reported over a spent claim"
    assert mgr._on_done.await_count == 0, "duplicate delivery reached the parent"


@pytest.mark.asyncio
async def test_recovery_failure_still_reports_when_it_owns_the_claim():
    """The converse: with the claim OPEN the failure must still be delivered.

    Guards against 'fixing' the duplicate above by making this path silent —
    the UI must never be left on a running card.
    """
    mgr = _make_manager()
    # Pin `started` into the past: on Windows `time.time()` has ~16ms
    # granularity, so a same-tick elapsed computes to exactly 0.0 and a bare
    # `> 0` assertion is a false failure rather than a real signal.
    info = _info(_session_sharing=False, started=time.time() - 5.0)

    def _boom(_info):
        raise RuntimeError("respawn failed")

    mgr._run = _boom  # type: ignore[assignment]
    await _schedule_recovery(mgr, info)

    assert len(_done_events(mgr)) == 1, "recovery failure never reported"
    assert mgr._on_done.await_count == 1, "parent never heard about the failure"
    assert info.done is True
    assert info.elapsed >= 5.0, "report carried no elapsed"
    assert info._recovering is False


@pytest.mark.asyncio
async def test_reap_suppression_marker_is_set_before_the_teardown_await():
    """The RESPAWN-suppression marker must be visible during teardown.

    The marker and the recovery-task cancel must not sit AFTER the session reset
    await. A recovery task whose bounded handshake expired inside that window
    respawned the run being killed — tools running after a user Stop. Asserting
    from inside the reset proves the ordering.

    Note this is `_reap_started`, not `reaped`: see
    `test_run_woken_by_reaper_reset_still_synthesizes_its_error` for why setting
    `reaped` this early causes a false success instead.
    """
    mgr = _make_manager()
    info = _info(_session_sharing=False)
    mgr._running_count = 1
    seen: dict[str, bool] = {}

    async def _observing_reset(session_key):
        seen["suppression_during_teardown"] = info._reap_started
        # `.cancelled()` only flips once the task runs, so assert the
        # observable that ordering guarantees: it is already de-registered
        # (popped + cancel requested) before teardown suspends.
        seen["recovery_deregistered"] = "a1b2c3d4:recovery" not in mgr._tasks
        await asyncio.sleep(0)

    async def _long_wait():
        await asyncio.sleep(3600)

    recovery = asyncio.ensure_future(_long_wait())
    mgr._tasks["a1b2c3d4:recovery"] = recovery
    mgr._sessions.reset = _observing_reset

    await mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="reaped")

    assert seen["suppression_during_teardown"] is True, (
        "respawn suppression was not set while teardown was suspended — a "
        "recovery handshake expiring here would respawn the run being killed"
    )
    assert seen["recovery_deregistered"] is True, (
        "recovery task was still registered while teardown was suspended"
    )
    await asyncio.sleep(0)
    assert recovery.cancelled(), "recovery task was never actually cancelled"


@pytest.mark.asyncio
async def test_recovery_respawn_releases_its_fresh_slot():
    """The respawn's slot token must be re-armed, and then actually spent.

    The interrupted run's `finally` consumed this info's one-shot slot token, so
    without re-arming (`info._slot_released = False`) the respawned run's own
    release no-ops and `_running_count` stays inflated forever, starving the
    spawn queue. Reverting the re-arm line must fail here: the test drives the
    respawn to completion and asserts the count returns to its pre-spawn value.
    """
    mgr = _make_manager()
    info = _info(_session_sharing=False)
    # State the interrupted run leaves behind: its finally already released.
    assert mgr._release_slot(info) is True
    mgr._running_count = 0

    ran = asyncio.Event()

    async def _fake_run(_info):
        ran.set()
        # Whatever the respawned run does, its finally releases the slot.
        if mgr._release_slot(_info):
            mgr._running_count = max(0, mgr._running_count - 1)

    mgr._run = _fake_run  # type: ignore[assignment]
    await _schedule_recovery(mgr, info)

    assert ran.is_set(), "respawn never ran"
    assert mgr._running_count == 0, (
        "slot token was not re-armed for the respawn — `_running_count` "
        f"permanently inflated at {mgr._running_count}"
    )


# ── the reap marker is split: early for respawn, late for records ────


@pytest.mark.asyncio
async def test_run_woken_by_reaper_reset_still_synthesizes_its_error():
    """A run woken by the REAPER's own session reset must not report success.

    `reaped` carries two incompatible requirements. The recovery scheduler needs
    it set before the teardown awaits (or it respawns the run being killed), so
    an earlier revision hoisted it to the top of `_force_reap`. But `_run` skips
    its error synthesis when `reaped` is set — so a run woken by the reaper's
    reset (the reset kills the provider, `_run_inner` raises) fell through with
    NO error, claimed the report first while the reaper was still tearing down,
    and delivered a FALSE SUCCESS the reaper could not correct.

    The flag is therefore split: `_reap_started` early (respawn suppression),
    `reaped` late (record/teardown ownership).
    """
    mgr = _make_manager()
    info = _info(_session_sharing=False, started=time.time() - 5.0)
    mgr._running_count = 1
    observed: dict[str, bool] = {}

    async def _reset_wakes_the_run(session_key):
        # Exactly the window under test: the reap is in flight and suspended in
        # teardown. A run waking here must still see `reaped == False`.
        observed["reaped"] = info.reaped
        observed["reap_started"] = info._reap_started
        await asyncio.sleep(0)

    mgr._sessions.reset = _reset_wakes_the_run

    await mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="timeout")

    assert observed["reap_started"] is True, (
        "recovery suppression marker not set before teardown — a pending "
        "respawn would relaunch the run being killed"
    )
    assert observed["reaped"] is False, (
        "`reaped` was already set while the reaper was still tearing down: a run "
        "woken by this very reset would skip error synthesis and report success"
    )
    # And the reap still owns the record by the time it writes one.
    assert info.reaped is True
    assert info.error, "reaped agent recorded no error"


@pytest.mark.asyncio
async def test_cancelled_teardown_still_releases_slot_and_gate():
    """A cancellation at a teardown await must not skip the bookkeeping.

    Every statement in the teardown awaits, and `CancelledError` is not caught by
    the `except Exception` arms — so it propagated straight out of `_run`'s
    `finally`, skipping the slot release, the task pop and the teardown gate.
    That leaks a concurrency slot (the bug this PR exists to fix) and leaves an
    injected result unmarked, so restart reconciliation re-injects it.
    """
    mgr = _make_manager()
    info = _info(_session_sharing=False)
    mgr._running_count = 1
    info._slot_released = False  # the run owns an unspent slot token

    async def _cancelled_teardown(_info, _key):
        raise asyncio.CancelledError()

    mgr._teardown_run_session = _cancelled_teardown  # type: ignore[assignment]

    async def _run_inner_ok(_info, _key):
        _info.result = "done"

    mgr._run_inner = _run_inner_ok  # type: ignore[assignment]

    task = asyncio.ensure_future(mgr._run(info))
    with pytest.raises(asyncio.CancelledError):
        await task

    assert mgr._running_count == 0, (
        f"cancelled teardown leaked the slot (_running_count={mgr._running_count})"
    )
    assert "a1b2c3d4" not in mgr._tasks, "cancelled teardown left the task registered"


@pytest.mark.asyncio
async def test_run_does_not_block_on_its_report_during_shutdown():
    """`_run` must hand its report to the bounded drain once shutting down.

    `_run`'s `CancelledError` arm does not re-raise, so by the time it reaches
    `_await_report` the cancellation has been CONSUMED — `shield` would then wait
    out the full `_ON_DONE_TIMEOUT` injection cap and hold `cancel_all()`'s
    gather for it. (An earlier version of this test kept the cancellation pending
    and so measured a path the real `_run` never takes.)
    """
    mgr = _make_manager()
    info = _info(_session_sharing=False)
    mgr._running_count = 1
    mgr._shutting_down = True

    async def _run_inner_ok(_info, _key):
        _info.result = "done"

    async def _noop_teardown(_info, _key):
        await asyncio.sleep(0)

    mgr._run_inner = _run_inner_ok  # type: ignore[assignment]
    mgr._teardown_run_session = _noop_teardown  # type: ignore[assignment]

    wedged = asyncio.Event()

    async def _wedged_on_done(_info):
        wedged.set()
        await asyncio.sleep(3600)

    mgr._on_done = _wedged_on_done

    # Must return promptly even though the injection is wedged.
    await asyncio.wait_for(mgr._run(info), timeout=5)

    assert wedged.is_set(), "report never started"
    pending = [t for t in mgr._report_tasks if not t.done()]
    assert pending, "report should still be pending, owned by cancel_all's drain"
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_user_stop_during_pending_recovery_is_not_recorded_as_failure():
    """A neutral user Stop must not be persisted as a failure by the recovery arm.

    `_force_reap` cancels a pending recovery task BEFORE setting `reaped` (which
    must stay false until the reaper owns the record). `_resume_guarded`'s
    CancelledError arm consulted only `reaped`, so it won that race and wrote
    `error="cancelled"` plus a failure stat over a neutral stop — an outcome the
    reaper could not correct. It must consult `_reap_started`.
    """
    mgr = _make_manager()
    info = _info(_session_sharing=False, user_stopped=True, started=time.time() - 5.0)
    mgr._running_count = 1
    mgr._sessions.reset = _noop_reset

    failures: list[int] = []

    async def _arm() -> None:
        mgr._schedule_cancel_recovery(info)

    await asyncio.create_task(_arm())
    recovery = mgr._tasks.get("a1b2c3d4:recovery")
    assert recovery is not None, "recovery task was not registered"

    import kiro_crew.subagent as mod

    class _Stats:
        def inc_subagent_failed(self):
            failures.append(1)

    orig = mod.Stats
    mod.Stats = lambda: _Stats()  # type: ignore[assignment]
    try:
        await mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="user_stop")
        await asyncio.gather(recovery, return_exceptions=True)
    finally:
        mod.Stats = orig  # type: ignore[assignment]

    assert failures == [], (
        "a neutral user Stop was counted as a subagent failure by the "
        "cancelled-recovery arm"
    )
    assert info.error == "", f"user stop synthesized an error: {info.error!r}"


@pytest.mark.asyncio
async def test_stop_during_pending_spawn_approval_reports_and_releases_once():
    """The spawn-approval rejection path is a FIFTH terminal site.

    It set `done`, decremented `_running_count` with a bare decrement and
    announced via `_safe_announce` — outside both one-shot tokens. A user Stop
    funnels into `_force_reap` and can land while the approval is still pending
    (a human prompt has no deadline), so the reap released the slot and reported,
    then the rejection path released and reported AGAIN: a negative concurrency
    count and a duplicate completion.
    """
    mgr = _make_manager()
    info = _info(_session_sharing=False, started=time.time() - 5.0)
    mgr._agents["a1b2c3d4"] = info
    mgr._running_count = 1
    mgr._sessions.reset = _noop_reset

    announced: list[str] = []

    async def _safe_announce(_info):
        announced.append(_info.id)

    mgr._safe_announce = _safe_announce  # type: ignore[assignment]

    release_stop = asyncio.Event()

    async def _pending_then_denied(_rid, _preview, _parent):
        await release_stop.wait()
        return False

    mgr._on_spawn_approval = _pending_then_denied  # type: ignore[assignment]

    approval_task = asyncio.ensure_future(mgr._spawn_with_approval(info))
    await asyncio.sleep(0)

    # User Stop lands while the approval is still outstanding.
    await mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="user_stop")
    release_stop.set()
    await approval_task

    assert mgr._running_count == 0, (
        f"slot released twice (_running_count={mgr._running_count}); a negative "
        "count permanently inflates apparent capacity"
    )
    total_reports = len(_done_events(mgr)) + len(announced)
    assert total_reports == 1, (
        f"terminal outcome delivered {total_reports} times, expected exactly once"
    )


@pytest.mark.asyncio
async def test_report_retention_refusal_is_boundary_local(monkeypatch):
    """Refusal state belongs to one boundary and clears only with its discard."""
    import kiro_crew.subagent as mod
    from kiro_crew.dashboard.state import StageBoundary
    from kiro_crew.subagent import SubagentReportDeliveryError

    byte_boundary = StageBoundary()
    byte_boundary.arm(1)
    row_boundary = StageBoundary()
    row_boundary.arm(1)
    byte_scope = ("dashboard:byte-refused", byte_boundary.owner or "")
    row_scope = ("dashboard:row-refused", row_boundary.owner or "")
    boundaries = {byte_scope: byte_boundary, row_scope: row_boundary}
    manager = _make_manager()
    manager._stage_boundary_for_scope = lambda parent, owner: boundaries.get((parent, owner))

    monkeypatch.setattr(mod, "_REPORT_FAILURE_BYTE_BUDGET", 0)
    manager._latch_report_failure(
        _info(
            id="byte-refused",
            parent_session_key=byte_scope[0],
            _stage_boundary_owner=byte_scope[1],
        )
    )
    monkeypatch.setattr(mod, "_REPORT_FAILURE_BYTE_BUDGET", 1_000_000)
    monkeypatch.setattr(mod, "_REPORT_FAILURES_PER_PARENT_CAP", 0)
    manager._latch_report_failure(
        _info(
            id="row-refused",
            parent_session_key=row_scope[0],
            _stage_boundary_owner=row_scope[1],
        )
    )

    assert byte_boundary.report_retention_refused == "byte_budget"
    assert row_boundary.report_retention_refused == "row_cap"
    assert manager._boundary_report_payloads == {}
    for scope in (byte_scope, row_scope):
        with pytest.raises(SubagentReportDeliveryError):
            await manager.wait_for_parent_reports(*scope)

    manager.discard_report_failures("dashboard:unrelated", "other-owner")
    assert byte_boundary.report_retention_refused == "byte_budget"
    assert row_boundary.report_retention_refused == "row_cap"

    manager.discard_report_failures(*byte_scope)
    assert byte_boundary.report_retention_refused is None
    assert row_boundary.report_retention_refused == "row_cap"
    assert await manager.wait_for_parent_reports(*byte_scope) is False
    with pytest.raises(SubagentReportDeliveryError):
        await manager.wait_for_parent_reports(*row_scope)

    manager.discard_report_failures(*row_scope)
    assert row_boundary.report_retention_refused is None
    assert await manager.wait_for_parent_reports(*row_scope) is False


def _terminal_report_consumer_fields() -> set[str]:
    """Fields read by terminal reporting and the production completion callback."""
    root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
    targets = {
        "subagent_manager/terminal.py": {
            "_record_crew_log_terminal",
            "_report_terminal_impl",
            "notify_injection_failed_impl",
        },
        "subagent_manager/waves.py": {"_settle_digest_holds_impl"},
        "slack/gateway.py": {
            "_defer_queued_delivery",
            "_broadcast_subagent_status",
            "_subagent_done",
        },
    }
    fields: set[str] = set()
    for relative, names in targets.items():
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        functions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in names
        }
        assert set(functions) == names
        for function in functions.values():
            fields.update(
                node.attr
                for node in ast.walk(function)
                if isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "info"
                and isinstance(node.ctx, ast.Load)
            )
            fields.update(
                node.args[1].value
                for node in ast.walk(function)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "info"
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            )
    # The exact owner is consumed through stage_boundary_owner_for_run(info).
    fields.add("_stage_boundary_owner")
    return fields


def test_report_failure_snapshot_fields_match_terminal_consumers() -> None:
    """A new terminal consumer field cannot bypass failed-report redelivery."""
    from kiro_crew.subagent import _ReportFailureSnapshot

    assert set(_ReportFailureSnapshot.__dataclass_fields__) == _terminal_report_consumer_fields()
