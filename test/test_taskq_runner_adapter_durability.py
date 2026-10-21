"""Runner-adapter rows when the store refuses a write or a read, and when the gateway restarts.

Pins ``_requeue_unstarted`` when the store refuses its write or only its read-back, and the
``queued`` row that ``claim_only``'s ``None`` leaves for the next boot's sweep; the adopt
sweep that settles what a dead incarnation left (resume, park, fail, cancel, paging past its
scan size, the lost-owner limit) and the rows it leaves alone; the factory over the
manager's store and cap; the store as the only admission path; terminal writes kept through
an outage, replayed, committed before the slot is released and settled by reconcile after a
crash; and an accepted answer read from its persisted wake event rather than from memory.
Lanes, ``accept``, ``admit``, the claim/start boundary, the stop-reason recovery and the
dependency and input waits are pinned in ``test_taskq_runner_adapter.py``; the shared
fixtures and store helpers live in ``taskq_runner_adapter_helpers.py``.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from overload_fakes import Clock, settle_store_writes
from taskq_runner_adapter_helpers import (  # noqa: F401 -- fixtures are requested by name
    _RUNNER_LOGGER,
    _admission,
    _locked_store,
    clock_fixture,
    store_fixture,
)

from kiro_crew.taskq import model as m
from kiro_crew.taskq.adapters import runner as r
from kiro_crew.taskq.reconcile import reconcile_on_boot
from kiro_crew.taskq.store import TaskStore, TaskStoreUnavailable
from kiro_crew.testing.wait import await_until


def _delete_mode_store(tmp_path: Path, clock: Clock) -> TaskStore:
    """A store in the journal mode a data home on a network filesystem gets.

    ``journal_mode=delete`` is what the store auto-detects over NFS/SMB and what
    ``agent.task_store_journal_mode=delete`` selects, and it is the mode in which
    a competing writer blocks READERS -- so a bare ``SELECT`` there is exactly as
    failure-prone as the transaction before it. WAL, the local default, does not
    block readers, which is why no other test in this file reaches this.
    """
    return TaskStore(
        tmp_path / "tasks.db", clock=clock, network_fs=True, busy_timeout_secs=0.05
    ).open()


def _claimed_unstarted(store: TaskStore, task_id: str = "taskrunner:r:task1") -> int:
    store.accept_one(m.TaskRecord(id=task_id, kind=m.KIND_TASKRUNNER_STEP))
    claimed = store.claim(task_id)
    assert claimed is not None and store.state_of(task_id) == m.ADMITTED
    return claimed.generation


def _one_warning(caplog: pytest.LogCaptureFixture) -> str:
    """The single WARNING the runner adapter emitted -- and only the adapter's.

    ``caplog`` captures the ROOT logger, so a WARNING from any other module that
    happens to fire during this test lands in ``caplog.records`` too: a
    maintenance sweep an earlier test's ``SessionManager`` handed to the shared
    executor thread finishes minutes later, in whichever test is running then.
    Counting unfiltered records made this helper assert on that thread's timing.
    The callers already scope ``caplog.at_level(..., logger=_RUNNER_LOGGER)``; the
    count is scoped to the same logger.
    """
    said = [
        rec.getMessage()
        for rec in caplog.records
        if rec.name == _RUNNER_LOGGER and rec.levelno >= logging.WARNING
    ]
    assert len(said) == 1, said
    return said[0]


def test_a_locked_read_after_the_requeue_is_the_stores_typed_error(
    tmp_path: Path, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """The refusal where ONLY ``_requeue_unstarted``'s read-back is refused.

    The rival takes its lock after ``transition`` has RETURNED, so the requeue
    commits and the row is ``queued`` while the read raises. An
    ``except TaskStoreUnavailable`` arm cannot fire for a raw
    ``sqlite3.OperationalError``, so the store's own read boundary is the only
    thing between this and a database error raised into ``admit`` -- and because
    the committed row is ``queued`` here and ``admitted`` in the sibling case
    below, the one ``None`` may not have either state written under it as fact.
    """
    store = _delete_mode_store(tmp_path, clock)
    task_id = "taskrunner:r:task1"
    try:
        assert store.journal_mode == "delete"
        generation = _claimed_unstarted(store, task_id)
        rival = sqlite3.connect(str(store.path), timeout=0, check_same_thread=False)
        committing = store.transition

        def _commit_then_let_the_rival_in(*args: object, **kw: object) -> bool:
            ok = bool(committing(*args, **kw))
            rival.execute("BEGIN EXCLUSIVE")
            rival.execute("UPDATE tasks SET updated_at=1 WHERE id=?", (task_id,))
            return ok

        store.transition = _commit_then_let_the_rival_in  # type: ignore[method-assign]
        try:
            with caplog.at_level(logging.WARNING, logger=_RUNNER_LOGGER):
                assert r.RunnerAdmission._requeue_unstarted(store, task_id, generation) is None
        finally:
            store.transition = committing  # type: ignore[method-assign]
            rival.execute("ROLLBACK")
            rival.close()
        assert store.state_of(task_id) == m.QUEUED, "the requeue this None reports on committed"
        said = _one_warning(caplog)
        assert m.QUEUED in said and m.ADMITTED in said, "one refusal's row was stated as fact"
    finally:
        store.close()


def test_a_locked_requeue_answers_the_same_none_over_a_row_that_stayed_admitted(
    tmp_path: Path, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """The other refusal behind that one ``None``: the rival already holds the
    lock, so the requeue itself never commits and the row keeps ``admitted``
    under a lapsing lease. Both refusals are unstarted and claimable, which is
    all the warning may claim -- the row's state is the half it cannot know."""
    store = _delete_mode_store(tmp_path, clock)
    task_id = "taskrunner:r:task1"
    try:
        generation = _claimed_unstarted(store, task_id)
        rival = sqlite3.connect(str(store.path), timeout=0, check_same_thread=False)
        rival.execute("BEGIN EXCLUSIVE")
        rival.execute("UPDATE tasks SET updated_at=1 WHERE id=?", (task_id,))
        try:
            with caplog.at_level(logging.WARNING, logger=_RUNNER_LOGGER):
                assert r.RunnerAdmission._requeue_unstarted(store, task_id, generation) is None
        finally:
            rival.execute("ROLLBACK")
            rival.close()
        assert store.state_of(task_id) == m.ADMITTED
        said = _one_warning(caplog)
        assert m.QUEUED in said and m.ADMITTED in said, "one refusal's row was stated as fact"
    finally:
        store.close()


def test_claim_only_leaves_a_queued_row_the_next_boots_sweep_owns(
    tmp_path: Path, clock: Clock
) -> None:
    """``claim_only``'s ``None`` is a fence, and this is what it costs.

    The requeued ``queued`` container row has no dispatcher (``KIND_SUBAGENT`` is
    the only kind ever fetched) and the boot reconciler is ACTIVE-only, so it
    lingers for the rest of THIS gateway's life -- and THIS incarnation's own
    adopt sweep skips it too, because ``params.accepted_by`` names the
    incarnation that is still running. Bounded, not forever: the next boot's
    ``adopt_orphaned_rows`` cancels it as never started.
    """
    store = _locked_store(tmp_path, clock)
    try:
        adm, _ = _admission(store, clock)
        rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r")
        assert rec is not None and adm.claim_only(rec.id) is None
        assert store.state_of(rec.id) == m.QUEUED
        assert reconcile_on_boot(store).examined == 0, "queued is not an ACTIVE row"
        assert store.fetch_dispatchable(m.KIND_SUBAGENT, limit=8) == []
        mine = r.adopt_orphaned_rows(store, kinds=(m.KIND_TASKRUNNER_STEP,))
        assert mine.cancelled == [], "this incarnation cancelled a row it may still be admitting"
        assert store.state_of(rec.id) == m.QUEUED
    finally:
        store.close()
    reopened = TaskStore(tmp_path / "tasks.db", clock=clock, network_fs=False).open()
    try:
        assert r.adopt_orphaned_rows(reopened, kinds=(m.KIND_TASKRUNNER_STEP,)).cancelled == [
            "taskrunner:r"
        ]
        assert reopened.state_of("taskrunner:r") == m.CANCELLED
    finally:
        reopened.close()


# ── adopt (the recovery adapter) ─────────────────────────────────────────────


def _crashed_row(store: TaskStore, task_id: str, *, safe: bool, parent: str | None = None) -> None:
    rec = m.TaskRecord(
        id=task_id,
        kind=m.KIND_TASKRUNNER_STEP,
        parent_id=parent,
        params={"task_id": "run1", r.PARAM_SAFE_RETRY: safe},
    )
    store.accept_one(rec)
    store.claim(task_id, owner="dead-incarnation")
    store.transition(task_id, m.STARTING)
    store.transition(task_id, m.RUNNING)


def test_adopt_resumes_safe_rows_and_parks_unsafe_ones(store: TaskStore, clock: Clock) -> None:
    _crashed_row(store, "taskrunner:run1", safe=True)
    _crashed_row(store, "taskrunner:run1:task2", safe=True, parent="taskrunner:run1")
    _crashed_row(store, "taskrunner:run2", safe=False)
    _crashed_row(store, "taskrunner:run2:task1", safe=False, parent="taskrunner:run2")
    resumed: list[str] = []

    report = r.adopt_orphaned_rows(store, resume=lambda rec: resumed.append(rec.id) or True)

    assert report.examined == 4
    assert resumed == ["taskrunner:run1"] and report.resumed == ["taskrunner:run1"]
    assert store.get("taskrunner:run1").state == m.RECOVERING  # claimable for the resume
    assert store.get("taskrunner:run1:task2").state == m.FAILED  # re-run from the checkpoint
    assert store.get("taskrunner:run2").state == m.UNKNOWN_SIDE_EFFECT
    assert store.get("taskrunner:run2:task1").state == m.UNKNOWN_SIDE_EFFECT
    assert sorted(report.unknown_side_effect) == ["taskrunner:run2", "taskrunner:run2:task1"]
    # Idempotent: nothing left to adopt.
    again = r.adopt_orphaned_rows(store, resume=lambda rec: True)
    assert again.examined == 1 and again.resumed == ["taskrunner:run1"]


def test_adopt_skips_rows_this_incarnation_still_owns(store: TaskStore, clock: Clock) -> None:
    rec = m.TaskRecord(
        id="taskrunner:live", kind=m.KIND_TASKRUNNER_STEP, params={r.PARAM_SAFE_RETRY: True}
    )
    store.accept_one(rec)
    store.claim(rec.id)  # this incarnation, fresh lease
    store.transition(rec.id, m.STARTING)
    store.transition(rec.id, m.RUNNING)
    report = r.adopt_orphaned_rows(store, resume=lambda _r: True)
    assert report.skipped == ["taskrunner:live"] and store.get(rec.id).state == m.RUNNING


def test_adopt_pages_past_its_scan_size_so_no_queued_row_is_stranded(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backlog larger than one page is settled in full, not to the page size.

    ``queued`` is the one claimable state no other sweep reaches -- it is not
    ``ACTIVE``, so the boot reconciler does not see it, and a runner kind has no
    fair-dispatch fetch -- so a row this sweep does not reach stays accepted with
    nothing that will ever run it. A cap on the SCAN is therefore a cap on what a
    restart can recover, which is the loss the store exists to prevent. The page
    size is shrunk here so the paging is what is measured rather than the volume.
    """
    monkeypatch.setattr(r, "_ADOPT_SCAN_PAGE", 3)
    ids = [f"taskrunner:orphan{i}" for i in range(7)]
    first = TaskStore(tmp_path / "tasks.db", window=8, clock=clock, network_fs=False).open()
    try:
        adm, _ = _admission(first, clock)
        for task_id in ids:
            assert adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id=task_id) is not None
            assert first.state_of(task_id) == m.QUEUED
    finally:
        first.close()

    reopened = TaskStore(tmp_path / "tasks.db", window=8, clock=clock, network_fs=False).open()
    try:
        report = r.adopt_orphaned_rows(reopened, kinds=(m.KIND_TASKRUNNER_STEP,))
        assert sorted(report.cancelled) == sorted(ids), "the sweep stopped at a page boundary"
        assert not any(reopened.state_of(task_id) == m.QUEUED for task_id in ids)
    finally:
        reopened.close()


def test_a_claim_between_two_pages_does_not_hide_a_queued_orphan(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep runs BESIDE a live gateway, so OFFSET alone proves nothing.

    A claim removes a row from ``queued`` and every row behind it shifts one
    place, so the row that lands where the cursor already is is never examined --
    one row lost per claim, left ``queued`` with no dispatcher and no reconciler.
    The pass therefore repeats while it discovers ids it had not read: the
    displaced row is EARLIER than the cursor, so the next pass reads it.
    """
    monkeypatch.setattr(r, "_ADOPT_SCAN_PAGE", 3)
    ids = [f"taskrunner:orphan{i}" for i in range(7)]
    first = TaskStore(tmp_path / "tasks.db", window=8, clock=clock, network_fs=False).open()
    try:
        adm, _ = _admission(first, clock)
        for task_id in ids:
            assert adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id=task_id) is not None
    finally:
        first.close()

    reopened = TaskStore(tmp_path / "tasks.db", window=8, clock=clock, network_fs=False).open()
    try:
        real_list_rows = reopened.list_rows
        pages: list[int] = []
        claimed: list[str] = []

        def _claim_between_pages(**kw: object) -> list:
            page = real_list_rows(**kw)  # type: ignore[arg-type]
            pages.append(len(page))
            if len(pages) == 1:
                assert reopened.claim(ids[0]) is not None
                claimed.append(ids[0])
            return page

        monkeypatch.setattr(reopened, "list_rows", _claim_between_pages)
        report = r.adopt_orphaned_rows(reopened, kinds=(m.KIND_TASKRUNNER_STEP,))
        monkeypatch.setattr(reopened, "list_rows", real_list_rows)
        assert claimed == [ids[0]], "the interleaving never ran, so this pin measured nothing"
        stranded = [t for t in ids if reopened.state_of(t) == m.QUEUED]
        assert stranded == [], "a queued orphan the shift displaced was never examined"
        assert sorted(report.cancelled) == sorted(ids[1:])
        # The claimed row is the live gateway's; the sweep leaves it alone.
        assert reopened.state_of(ids[0]) == m.ADMITTED
    finally:
        reopened.close()


def test_adopt_declined_resume_fails_the_row(store: TaskStore, clock: Clock) -> None:
    _crashed_row(store, "taskrunner:gone", safe=True)
    report = r.adopt_orphaned_rows(store, resume=lambda _r: False)
    assert report.failed == ["taskrunner:gone"]
    assert store.get("taskrunner:gone").state == m.FAILED


def _adopt_on_every_boot(
    path: Path, clock: Clock, *, claim_after_resume: bool
) -> tuple[int, r.AdoptReport]:
    """Boot, reconcile and adopt until a sweep does not resume the row.

    Returns the resumes and the last sweep's report: the one that ended the row
    once its recoveries were spent.

    With *claim_after_resume* the run's resume claims the row and runs, and the
    same step takes the gateway down; without it the gateway dies before the
    resume claims the row, which leaves it ``recovering``.
    """
    resumes = 0
    report = r.AdoptReport()
    for _ in range(m.RECOVERY_MAX_ATTEMPTS + 4):
        store = _reopen(path, clock)
        try:
            reconcile_on_boot(store)
            report = r.adopt_orphaned_rows(
                store, kinds=(m.KIND_TASKRUNNER_STEP,), resume=lambda _rec: True
            )
            if report.resumed != ["taskrunner:loops"]:
                return resumes, report
            resumes += 1
            if claim_after_resume:
                claim = store.claim("taskrunner:loops")
                assert claim is not None
                store.transition("taskrunner:loops", m.STARTING, generation=claim.generation)
                store.transition("taskrunner:loops", m.RUNNING, generation=claim.generation)
        finally:
            store.close()  # the gateway stops
    return resumes, report


@pytest.mark.parametrize("claim_after_resume", [True, False], ids=["runs-again", "dies-first"])
def test_a_run_that_takes_its_gateway_down_is_adopted_a_bounded_number_of_times(
    tmp_path: Path, clock: Clock, claim_after_resume: bool
) -> None:
    """Every gateway life that adopts and resumes a runner row spends one of the
    row's lost-owner recoveries (``model.RECOVERY_MAX_ATTEMPTS``, the boot
    reconciler's limit), whether the row was running or still ``recovering``
    when its gateway stopped. The next boot ends it ``failed`` with the reason
    instead of resuming it again, and that sweep reports the row in
    ``AdoptReport.lost_owner_limit``, so a run whose step kills its gateway
    cannot restart that gateway forever."""
    path = tmp_path / "tasks.db"
    store = _reopen(path, clock)
    _crashed_row(store, "taskrunner:loops", safe=True)
    store.close()
    resumes, report = _adopt_on_every_boot(path, clock, claim_after_resume=claim_after_resume)
    store = _reopen(path, clock)
    try:
        rec = store.get("taskrunner:loops")
        assert (resumes, rec.state) == (m.RECOVERY_MAX_ATTEMPTS, m.FAILED)
        last = store.events("taskrunner:loops")[-1]
        assert (last.kind, last.data["to"]) == ("transition", m.FAILED)
        assert last.data["adopted"] == "lost_owner_limit"
        assert last.data["lost_owner_recoveries"] == m.RECOVERY_MAX_ATTEMPTS
        assert "gateway" in last.data["error"]
        # A settled row stays settled on the next boot.
        assert r.adopt_orphaned_rows(store, resume=lambda _rec: True).examined == 0
        # The sweep that spent the row's recoveries listed it, once.
        assert report.lost_owner_limit == ["taskrunner:loops"]
    finally:
        store.close()


def test_repeated_sweeps_in_one_gateway_life_spend_one_recovery(
    store: TaskStore, clock: Clock
) -> None:
    """A second sweep in the same gateway life is not a lost owner: the row is
    resumed again and its recoveries are spent only by earlier lives."""
    _crashed_row(store, "taskrunner:once", safe=True)
    for _ in range(m.RECOVERY_MAX_ATTEMPTS + 2):
        report = r.adopt_orphaned_rows(store, resume=lambda _rec: True)
        assert report.resumed == ["taskrunner:once"]
    assert store.lost_owner_recoveries("taskrunner:once") == 0
    assert store.get("taskrunner:once").state == m.RECOVERING


def test_a_gateway_life_that_sweeps_twice_spends_one_recovery(tmp_path: Path, clock: Clock) -> None:
    """Recoveries are counted per gateway life, not per sweep: the two lives that
    each swept the row twice have spent two, so the next life still resumes it."""
    path = tmp_path / "tasks.db"
    store = _reopen(path, clock)
    _crashed_row(store, "taskrunner:twice", safe=True)
    store.close()
    for _ in range(m.RECOVERY_MAX_ATTEMPTS - 1):
        store = _reopen(path, clock)
        try:
            reconcile_on_boot(store)
            for _ in range(2):
                report = r.adopt_orphaned_rows(
                    store, kinds=(m.KIND_TASKRUNNER_STEP,), resume=lambda _rec: True
                )
                assert report.resumed == ["taskrunner:twice"]
        finally:
            store.close()
    store = _reopen(path, clock)
    try:
        reconcile_on_boot(store)
        assert store.lost_owner_recoveries("taskrunner:twice") == m.RECOVERY_MAX_ATTEMPTS - 1
        report = r.adopt_orphaned_rows(
            store, kinds=(m.KIND_TASKRUNNER_STEP,), resume=lambda _rec: True
        )
        assert report.resumed == ["taskrunner:twice"]
        assert store.get("taskrunner:twice").state == m.RECOVERING
    finally:
        store.close()


def test_a_resume_that_takes_the_gateway_down_is_already_counted(
    tmp_path: Path, clock: Clock
) -> None:
    """The recovery is recorded before the row is handed to resume, so a resume
    that stops the gateway has already spent it."""
    path = tmp_path / "tasks.db"
    store = _reopen(path, clock)
    _crashed_row(store, "taskrunner:down", safe=True)
    store.close()
    store = _reopen(path, clock)
    try:
        reconcile_on_boot(store)

        def _gateway_stops(_rec: m.TaskRecord) -> bool:
            raise SystemExit("the gateway stops during the resume")

        with pytest.raises(SystemExit):
            r.adopt_orphaned_rows(store, kinds=(m.KIND_TASKRUNNER_STEP,), resume=_gateway_stops)
    finally:
        store.close()
    store = _reopen(path, clock)
    try:
        assert store.lost_owner_recoveries("taskrunner:down") == 1
    finally:
        store.close()


def _reopen(path: Path, clock: Clock) -> TaskStore:
    """A NEW incarnation on the same file -- what a gateway restart is."""
    return TaskStore(path, window=8, clock=clock, network_fs=False).open()


@pytest.mark.asyncio
async def test_adopt_ends_the_rows_a_dead_incarnation_accepted_or_admitted(
    tmp_path: Path, clock: Clock
) -> None:
    """Crash after accept (``queued``), after the claim (``admitted``) and after
    the ``starting`` write.

    The boot reconciler examines only ACTIVE rows and requeues an ``admitted``
    one; ``workflow_agent`` has no dispatcher, so both claimable rows are the
    sweep's alone. Repeated sweeps settle nothing twice.
    """
    from kiro_crew.taskq.reconcile import reconcile_on_boot

    path = tmp_path / "tasks.db"
    store_a = _reopen(path, clock)
    adm_a, _ = _admission(store_a, clock, cap=2)
    started = adm_a.accept(kind=m.KIND_WORKFLOW_AGENT, task_id="workflow:old:agent1")
    queued = adm_a.accept(kind=m.KIND_WORKFLOW_AGENT, task_id="workflow:old:agent2")
    admitted = adm_a.accept(kind=m.KIND_WORKFLOW_AGENT, task_id="workflow:old:agent3")
    await adm_a.admit(started.id, kind=m.KIND_WORKFLOW_AGENT)
    assert store_a.claim(admitted.id) is not None  # claimed, never started
    assert store_a.state_of(queued.id) == m.QUEUED
    store_a.close()  # -- crash: no row has an owner any more

    store_b = _reopen(path, clock)
    try:
        reconcile_on_boot(store_b, now=clock())
        assert store_b.state_of(queued.id) == m.QUEUED  # ACTIVE-only, by design
        assert store_b.state_of(admitted.id) == m.QUEUED  # requeued: no runtime, no effect
        report = r.adopt_orphaned_rows(
            store_b, kinds=(m.KIND_WORKFLOW_AGENT,), resume=lambda _rec: False
        )
        assert sorted(report.cancelled) == [queued.id, admitted.id]
        assert store_b.state_of(queued.id) == m.CANCELLED
        assert store_b.state_of(admitted.id) == m.CANCELLED
        assert store_b.state_of(started.id) == m.UNKNOWN_SIDE_EFFECT
        # The two numbers the leak corrupted: nothing is left waiting.
        assert store_b.oldest_wait_secs() == 0.0
        assert m.QUEUED not in store_b.count_by_state()
        again = r.adopt_orphaned_rows(
            store_b, kinds=(m.KIND_WORKFLOW_AGENT,), resume=lambda _rec: False
        )
        assert again.examined == 0 and again.cancelled == []
    finally:
        store_b.close()


@pytest.mark.asyncio
async def test_adopt_leaves_a_row_this_incarnation_just_accepted_alone(
    store: TaskStore, clock: Clock
) -> None:
    """The regression pin for the live-``admit`` race: a queued row whose owner
    coroutine is parked in ``admit`` is NOT an orphan. ``params.accepted_by``
    is what says so -- a queued row has no lease to read."""
    adm, _ = _admission(store, clock, cap=1)
    holder = adm.accept(kind=m.KIND_WORKFLOW_AGENT, task_id="workflow:live:agent1")
    waiting = adm.accept(kind=m.KIND_WORKFLOW_AGENT, task_id="workflow:live:agent2")
    assert waiting.params[r.PARAM_ACCEPTED_BY] == store.incarnation
    h = await adm.admit(holder.id, kind=m.KIND_WORKFLOW_AGENT)
    parked = asyncio.create_task(adm.admit(waiting.id, kind=m.KIND_WORKFLOW_AGENT))
    await settle_store_writes(store)
    await settle_store_writes(store)
    assert store.state_of(waiting.id) == m.QUEUED

    report = r.adopt_orphaned_rows(store, kinds=(m.KIND_WORKFLOW_AGENT,), resume=lambda _rec: False)

    assert report.cancelled == [] and store.state_of(waiting.id) == m.QUEUED
    assert h.done()
    # flake-ok: pre-existing unbounded await of the admit task parked above, moved verbatim; tracked for a de-flake pass
    live = await parked  # the slot freed and the live admission claimed it
    assert live.generation == 1 and live.done()


def test_a_re_accepted_queued_row_names_the_new_incarnation_as_its_owner(
    tmp_path: Path, clock: Clock
) -> None:
    """A row RE-accepted after a restart belongs to THIS incarnation.

    ``accept`` hands an existing claimable row back instead of duplicating it --
    that is how a resume re-attaches -- and the queued-orphan sweep's owner test
    is ``params.accepted_by``. A returned row that still names the dead
    incarnation is cancelled by the sweep while the live ``admit`` is parked on
    it, which is a queue place lost for as long as the store lives.
    """
    path = tmp_path / "tasks.db"
    store_a = _reopen(path, clock)
    adm_a, _ = _admission(store_a, clock, cap=1)
    first = adm_a.accept(kind=m.KIND_WORKFLOW_AGENT, task_id="workflow:resumed:agent1")
    assert first is not None and first.params[r.PARAM_ACCEPTED_BY] == store_a.incarnation
    store_a.close()  # -- crash while the row was still queued

    store_b = _reopen(path, clock)
    try:
        assert store_b.incarnation != first.params[r.PARAM_ACCEPTED_BY]
        adm_b, _ = _admission(store_b, clock, cap=1)
        again = adm_b.accept(kind=m.KIND_WORKFLOW_AGENT, task_id="workflow:resumed:agent1")
        assert again is not None and again.state == m.QUEUED  # re-attached, not duplicated
        # The handed-back record AND the row on disk name the new owner.
        assert again.params[r.PARAM_ACCEPTED_BY] == store_b.incarnation
        assert store_b.get(again.id).params[r.PARAM_ACCEPTED_BY] == store_b.incarnation
        # Nothing else on the row moved: same state, same generation.
        assert store_b.get(again.id).generation == 0

        report = r.adopt_orphaned_rows(
            store_b, kinds=(m.KIND_WORKFLOW_AGENT,), resume=lambda _rec: False
        )
        assert report.cancelled == [] and store_b.state_of(again.id) == m.QUEUED
    finally:
        store_b.close()


def test_adopt_never_touches_a_claimable_subagent_row(tmp_path: Path, clock: Clock) -> None:
    """A queued ``subagent`` row is the window refill's, and the sweep is
    kind-scoped so it can never be reached."""
    path = tmp_path / "tasks.db"
    store_a = _reopen(path, clock)
    store_a.accept_one(m.TaskRecord(id="sub-1", kind=m.KIND_SUBAGENT, params={"prompt": "p"}))
    store_a.close()
    store_b = _reopen(path, clock)
    try:
        report = r.adopt_orphaned_rows(store_b, resume=lambda _rec: False)
        assert report.cancelled == [] and report.examined == 0
        assert store_b.state_of("sub-1") == m.QUEUED
    finally:
        store_b.close()


@pytest.mark.asyncio
async def test_adopt_leaves_a_retry_wait_row_carrying_an_answer_alone(
    tmp_path: Path, clock: Clock
) -> None:
    """``retry_wait`` is claimable but is NOT swept: the operator's answer is on
    THIS row's wake event, and a re-run under a ``~N`` id could not read it.
    ``test_answer_is_restored_from_the_wake_event_after_a_rebuild`` is the other
    half -- the row is meant to survive the restart and be re-admitted."""
    path = tmp_path / "tasks.db"
    store_a = _reopen(path, clock)
    adm_a, _ = _admission(store_a, clock, cap=1)
    rec = adm_a.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:ask")
    h = await adm_a.admit(rec.id)
    h.running()
    wait = asyncio.create_task(adm_a.waiting_input(h, tool_call_id="q1", reason="password?"))
    await settle_store_writes(store_a)
    assert adm_a.answer_input(rec.id, "hunter2") is True
    wait.cancel()  # -- crash before the re-admission
    with pytest.raises(asyncio.CancelledError):
        # flake-ok: pre-existing await of the input-wait task cancelled just above, moved verbatim; tracked for a de-flake pass
        await wait
    assert store_a.state_of(rec.id) == m.RETRY_WAIT
    store_a.close()

    store_b = _reopen(path, clock)
    try:
        report = r.adopt_orphaned_rows(
            store_b, kinds=(m.KIND_TASKRUNNER_STEP,), resume=lambda _rec: False
        )
        assert report.cancelled == [] and store_b.state_of(rec.id) == m.RETRY_WAIT
        adm_b, _ = _admission(store_b, clock, cap=1)
        assert adm_b.recorded_answer(rec.id) == "hunter2"
    finally:
        store_b.close()


def test_legacy_import_row_is_adopted(store: TaskStore, clock: Clock) -> None:
    from kiro_crew.taskq.migrate import legacy_taskrunner_records

    runs = Path(store.path).parent / "runs.json"
    runs.write_text(
        '[{"task_id": "old_1", "status": "paused", "name": "n", "spec_path": "s.md"}]',
        encoding="utf-8",
    )
    for rec in legacy_taskrunner_records(runs, now=clock.t):
        store.insert_if_absent(rec)
    row = store.get("taskrunner:old_1")
    assert row.state == m.RECOVERING and row.side_effect_class == m.SIDE_EFFECT_UNKNOWN
    # An import carries no safe_retry: it is NOT re-run blind.
    report = r.adopt_orphaned_rows(store, resume=lambda _r: True)
    assert report.unknown_side_effect == ["taskrunner:old_1"]


# ── factory ──────────────────────────────────────────────────────────────────


def test_runner_admission_for_follows_the_manager_cap(store: TaskStore) -> None:
    manager = SimpleNamespace(_taskq=store, max_concurrent=5)
    cfg = SimpleNamespace(
        agent=SimpleNamespace(adaptive_concurrency_mode="aimd", admit_wait_secs=7)
    )
    adm = r.runner_admission_for(manager, cfg=cfg, pressure=lambda: SimpleNamespace(admitted=True))
    assert adm.store is store and adm.lane.effective == 5
    manager.max_concurrent = 2  # the controller's set_effective_cap landed
    assert adm.lane.effective == 2
    # Tracking the cap is not a WAKE: with a waiter parked, a raise has to be
    # pushed in as pump(). Pinned by test_subagent_config_hot_reload.py
    # ::TestCapRaiseReachesTheRunnerLane.
    cfg.agent.adaptive_concurrency_mode = "fixed"
    manager.max_concurrent = 1
    assert adm.lane.mode == r.MODE_FIXED and adm.lane.effective == 1
    assert adm._admit_wait == 7.0


# ── the durable state machine is the only path ───────────────────────────────


@pytest.mark.asyncio
async def test_required_store_missing_refuses_instead_of_lane_only(clock: Clock) -> None:
    """With ``agent.task_queue_enabled`` on, no store means REFUSE (typed):
    lane-only admission would hand out generation-0 handles a restart forgets."""
    adm, _ = _admission(
        None, clock, cap=1, require_store=True, store_error=lambda: "tasks.db: disk I/O error"
    )
    with pytest.raises(r.RunnerAdmissionRefused, match="disk I/O error"):
        adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    with pytest.raises(r.RunnerAdmissionRefused):
        await adm.admit("taskrunner:r:task1")
    assert adm.lane.running == 0
    # The legacy queue-off shape is unchanged: lane-only, generation 0.
    legacy, _ = _admission(None, clock, cap=1)
    h = await legacy.admit("legacy")
    assert h.generation == 0 and h.done()


@pytest.mark.asyncio
async def test_failed_claim_never_starts_generation_zero(
    store: TaskStore, clock: Clock, monkeypatch
) -> None:
    adm, _ = _admission(store, clock, cap=1)
    rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")

    def _boom(_task_id):
        raise TaskStoreUnavailable("locked")

    monkeypatch.setattr(store, "claim", _boom)
    with pytest.raises(r.RunnerAdmissionRefused):
        await adm.admit(rec.id)
    assert adm.lane.running == 0
    assert store.state_of(rec.id) == m.QUEUED  # still dispatchable, never started


@pytest.mark.asyncio
async def test_settle_marks_settled_only_after_the_terminal_write_or_a_durable_retry(
    store: TaskStore, clock: Clock, monkeypatch
) -> None:
    adm, _ = _admission(store, clock, cap=1)
    rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    h = await adm.admit(rec.id)
    h.running()
    real_finish = store.finish
    outage = {"on": True}

    def _flaky(*a, **kw):
        if outage["on"]:
            raise TaskStoreUnavailable("store down")
        return real_finish(*a, **kw)

    monkeypatch.setattr(store, "finish", _flaky)
    assert h.done() is False
    # The row is still live in the store, the write is owned by the retry,
    # the slot is released so the lane is not leaked -- and nothing forgot
    # the task while its terminal write is outstanding.
    assert store.state_of(rec.id) == m.RUNNING
    assert adm.stats()["pending_terminal_writes"] == 1
    assert adm.lane.running == 0
    assert h.done() is False  # idempotent: no second write attempt is queued
    outage["on"] = False
    assert adm.retry_terminal_writes() == 1
    assert store.state_of(rec.id) == m.DONE
    assert adm.stats()["pending_terminal_writes"] == 0
    # tick() replays too, and a fenced row (another owner ended it) is dropped.
    rec2 = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task2")
    adm.defer_terminal_write(rec2.id, 99, m.DONE, result_ref=None, error=None)
    adm.tick()
    assert adm.stats()["pending_terminal_writes"] == 0


@pytest.mark.asyncio
async def test_the_next_admit_replays_a_terminal_write_the_store_kept(
    store: TaskStore, clock: Clock, monkeypatch
) -> None:
    """The run's own next start is what commits a kept terminal write.

    ``settle`` keeps the write rather than losing it, but only a REPLAYER makes
    that a promise, and a run that carries on reaches ``admit`` and nothing else:
    the pump's off-loop sweep never calls ``tick``, and the gateway drops the
    ``tick`` hook entirely once a dependency coordinator attaches. Without a
    replayer on this path the finished step's row stays ``running`` and leased for
    the life of the process, and the next boot reconciles it as an interrupted
    run -- a step that succeeded, recorded as one that may not have.
    """
    adm, _ = _admission(store, clock, cap=2)
    first = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    second = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task2")
    h1 = await adm.admit(first.id)
    h1.running()
    real_finish = store.finish
    outage = {"on": True}

    def _flaky(*a, **kw):
        if outage["on"]:
            raise TaskStoreUnavailable("store down")
        return real_finish(*a, **kw)

    monkeypatch.setattr(store, "finish", _flaky)
    assert await h1.done_async() is False
    assert store.state_of(first.id) == m.RUNNING
    assert store.get(first.id).lease_owner == store.incarnation
    assert adm.stats()["pending_terminal_writes"] == 1
    outage["on"] = False
    # The run carries on. No tick, no direct retry call: the next step's own
    # admission is the replayer.
    h2 = await adm.admit(second.id)
    assert store.state_of(first.id) == m.DONE, "the finished step's row was never settled"
    assert store.get(first.id).lease_owner is None
    assert adm.stats()["pending_terminal_writes"] == 0
    assert await h2.done_async() is True


# ── a deferred terminal write is durable by reconcile ───────────────────────


@pytest.mark.asyncio
async def test_crash_during_a_deferred_terminal_write_reconciles_the_row(
    tmp_path: Path, clock: Clock, monkeypatch
) -> None:
    """The store refuses the terminal write; the process crashes before the
    retry lands. Nothing is lost: the row is still ``running`` under the dead
    incarnation's lease (the retry never releases it), so the next boot's
    reconcile settles it -- ``unknown_side_effect`` for the default class, a
    claimable ``recovering`` row for side-effect-free (retry-safe) work. The pending write is
    therefore durable BY RECONCILE, not by a second persisted record."""
    from kiro_crew.taskq.reconcile import reconcile_on_boot

    path = tmp_path / "tasks.db"
    store_a = TaskStore(path, window=8, clock=clock, network_fs=False).open()
    adm, _ = _admission(store_a, clock, cap=2)
    unknown = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:unknown")
    safe = adm.accept(
        kind=m.KIND_TASKRUNNER_STEP,
        task_id="taskrunner:r:safe",
        side_effect_class=m.SIDE_EFFECT_NONE,
    )
    h1 = await adm.admit(unknown.id)
    h2 = await adm.admit(safe.id)
    h1.running()
    h2.running()

    def _down(*a, **kw):
        raise TaskStoreUnavailable("store down")

    monkeypatch.setattr(store_a, "finish", _down)
    assert h1.done() is False and h2.done() is False
    assert adm.stats()["pending_terminal_writes"] == 2
    # The lease is still the dead owner's: a running row is not claimable, so
    # nothing in the live process could start a second copy meanwhile.
    for rec in (store_a.get(unknown.id), store_a.get(safe.id)):
        assert rec is not None and rec.state == m.RUNNING
        assert rec.lease_owner == store_a.incarnation
    # -- crash: the retry never runs; a new incarnation boots on the same file.
    store_a.close()
    store_b = TaskStore(path, window=8, clock=clock, network_fs=False).open()
    report = reconcile_on_boot(store_b, now=clock())
    assert report.examined == 2 and report.awaiting_adapter == 2
    # The boot reconciler drops the dead owner's lease and hands runner kinds
    # to the runner adapter, which settles them by class.
    resumed: list[str] = []
    adopted = r.adopt_orphaned_rows(store_b, resume=lambda rec: resumed.append(rec.id) or True)
    assert unknown.id in adopted.unknown_side_effect
    assert store_b.state_of(unknown.id) == m.UNKNOWN_SIDE_EFFECT
    assert resumed == [safe.id]
    assert store_b.state_of(safe.id) == m.RECOVERING  # claimable for the runner's own admit
    adm_b, _ = _admission(store_b, clock, cap=2)
    clock.advance(3600)
    again = await adm_b.admit(safe.id)
    assert again.generation > h2.generation
    store_b.close()


# ── an accepted answer survives a crash before the grant ─────────────────────


@pytest.mark.asyncio
async def test_answer_is_restored_from_the_wake_event_after_a_rebuild(
    tmp_path: Path, clock: Clock
) -> None:
    """answer -> crash before the lane grant -> rebuild: the resumed step reads
    the answer from the persisted wake event, not from RAM."""
    path = tmp_path / "tasks.db"
    store_a = TaskStore(path, window=8, clock=clock, network_fs=False).open()
    adm, _ = _admission(store_a, clock, cap=1)
    rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:ask")
    h = await adm.admit(rec.id)
    h.running()
    wait = asyncio.create_task(adm.waiting_input(h, tool_call_id="q1", reason="password?"))
    await settle_store_writes(store_a)
    assert store_a.state_of(rec.id) == m.WAITING_INPUT
    assert adm.answer_input(rec.id, "hunter2") is True
    # -- crash before the coroutine is re-admitted: RAM is gone.
    wait.cancel()
    with pytest.raises(asyncio.CancelledError):
        # flake-ok: pre-existing await of the input-wait task cancelled just above, moved verbatim; tracked for a de-flake pass
        await wait
    assert store_a.state_of(rec.id) == m.RETRY_WAIT  # claimable, no lease
    store_a.close()
    store_b = TaskStore(path, window=8, clock=clock, network_fs=False).open()
    from kiro_crew.taskq.reconcile import reconcile_on_boot

    reconcile_on_boot(store_b, now=clock())
    assert store_b.state_of(rec.id) == m.RETRY_WAIT  # boot leaves a claimable row alone
    adm_b, _ = _admission(store_b, clock, cap=1)
    assert adm_b.recorded_answer(rec.id) == "hunter2"
    h_b = await adm_b.admit(rec.id)
    h_b.running()
    # The resumed step takes the answer once; a later rebuild does not replay it.
    assert adm_b.recorded_answer(rec.id) == "hunter2"
    assert adm_b.consume_answer(rec.id, "q1") is True
    assert adm_b.recorded_answer(rec.id) is None
    store_b.close()


@pytest.mark.asyncio
async def test_waiting_input_falls_back_to_the_persisted_answer(
    store: TaskStore, clock: Clock
) -> None:
    """A wake that arrives without this process's RAM copy of the answer (the
    ledger woke the row on another instance's behalf) still returns it."""
    adm, _ = _admission(store, clock, cap=1)
    rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:ask2")
    h = await adm.admit(rec.id)
    h.running()
    wait = asyncio.create_task(adm.waiting_input(h, tool_call_id="q2"))
    await settle_store_writes(store)
    from kiro_crew.taskq.waits import WaitLedger

    # Another instance records the answer on the wake event and wakes the row.
    assert WaitLedger(store, clock=clock).wake(
        rec.id, reason="input answered", detail={"answer": "yes"}
    )
    adm.on_wake(rec.id)
    assert await await_until(wait) == "yes"


@pytest.mark.asyncio
async def test_terminal_decision_is_persisted_before_capacity_is_released(
    tmp_path: Path, clock: Clock, monkeypatch
) -> None:
    """``Admitted.settle`` writes the terminal state FIRST and releases the lane
    slot only afterwards; a crash between the committed write and the local
    ``_settled`` bookkeeping leaves a TERMINAL row, which neither the boot
    reconciler nor the runner adapter touches -- a cancelled retry-safe row is
    never re-run. Only an unreachable store (``TaskStoreUnavailable``) leaves
    the decision to reconcile."""
    from kiro_crew.taskq.reconcile import reconcile_on_boot

    path = tmp_path / "tasks.db"
    store_a = TaskStore(path, window=8, clock=clock, network_fs=False).open()
    adm, _ = _admission(store_a, clock, cap=1)
    rec = adm.accept(
        kind=m.KIND_TASKRUNNER_STEP,
        task_id="taskrunner:r:safe-cancel",
        side_effect_class=m.SIDE_EFFECT_NONE,
    )
    h = await adm.admit(rec.id)
    h.running()
    order: list[str] = []
    real_finish = store_a.finish

    def _finish(*a, **kw):
        order.append("finish")
        return real_finish(*a, **kw)

    def _release():
        order.append("release_slot")
        # -- crash here: the write is committed, the local bookkeeping is not.
        raise SystemExit("simulated crash after the terminal write")

    monkeypatch.setattr(store_a, "finish", _finish)
    monkeypatch.setattr(h, "release_slot", _release)
    with pytest.raises(SystemExit):
        h.cancel("operator stop")
    assert order == ["finish", "release_slot"], "the terminal write precedes the slot release"
    assert store_a.state_of(rec.id) == m.CANCELLED
    store_a.close()
    store_b = TaskStore(path, window=8, clock=clock, network_fs=False).open()
    report = reconcile_on_boot(store_b, now=clock())
    assert report.examined == 0  # a terminal row is not an active row
    resumed: list[str] = []
    adopted = r.adopt_orphaned_rows(store_b, resume=lambda rec: resumed.append(rec.id) or True)
    assert adopted.examined == 0 and resumed == []
    assert store_b.state_of(rec.id) == m.CANCELLED
    store_b.close()
