"""One invariant for the shutdown obligations the notification bridge adds.

A shutdown obligation is a message the gateway has promised and not delivered: a
queued ``follow_up`` whose watcher is cancelled, or a terminal report whose
injection is abandoned. ``cancel_all`` is the only place that discharges one, and
three of its mechanisms are each defensible alone and lossy together. The
invariant that binds them:

    An obligation is discharged ONLY against a check that the mechanism which
    must act on it can actually act.

Read once per mechanism:

* **Drain ownership.** The producer runs ahead of the drain and the drain consumes
  its output, so the in-memory record (``pending_followups``) is dropped on the one
  branch where the parent has been told. An unconditional drop discharges the
  obligation against nothing.
* **Cancellation budget.** A phase may abandon WAITING. It may not abandon
  COMPENSATING: the state write and the re-admission after it are what make an
  abandoned obligation readable at the next start, so a bound wrapped around those
  cancels the remedy rather than the wait.
* **Re-admission proof.** Persisting the queue into ``state.json`` is not
  recoverability. ``list_orphans`` SKIPS any folder holding ``tombstone.json``, so
  the obligation is recoverable exactly when no tombstone remains. Whether an
  unlink removed a file is a different question, and it answers False for two
  opposite states: a folder that never carried a tombstone (recoverable) and a
  folder whose tombstone survived the attempt (not recoverable). Keying
  recoverability on the unlink is therefore wrong in both directions -- it claims a
  survivor is recovered, and it reports a clean folder as lost.
* **The round trip closes.** A written record nothing reads discharges nothing. The
  start that recovers the folder reports the persisted queue to the parent BEFORE
  tombstoning it, and a notice that did not get out leaves the folder visible, since
  the folder is the only place those messages still exist. Symmetrically, the
  announcement that shutdown relies on reports whether the parent was told, because
  it contains its own failures and a caller cannot learn them from an exception.
* **Per-entry provenance.** A note's governance subjects are the INTERSECTION of
  every party that could own it, and an untrusted value off ``meta`` only ever
  APPENDS a subject. Appending can withhold a delivery and cannot route one, so a
  forged owner tightens the decision instead of redirecting it.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import pathlib
import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.subagent import SubagentInfo, SubagentManager


def _make_manager() -> SubagentManager:
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock(), max_concurrent=4)
    mgr._fire_event = AsyncMock()
    mgr._write_tombstone = MagicMock()
    mgr._record_cost = MagicMock()
    mgr._on_done = AsyncMock()
    return mgr


def _info() -> SubagentInfo:
    return SubagentInfo(id="a1b2c3d4", task="t", agent="")


async def _arm_undelivered_followups(mgr, info, tmp_path, monkeypatch, queued):
    """Leave *info* holding *queued* follow-ups that ``cancel_all`` cannot announce.

    The watcher is already finished, so the drain reaches the announcement with the
    queue still set, and the announcement is wedged so the compensation path runs.
    """
    import kiro_crew.subagent as mod
    from kiro_crew.subagent_persistence import create_agent_folder

    root = tmp_path / "subagents"
    root.mkdir()
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", root)
    monkeypatch.setattr(mod, "_REPORT_DRAIN_TIMEOUT", 3600.0)

    info.pending_followups = list(queued)
    create_agent_folder(info.id, task="t")

    finished = asyncio.create_task(asyncio.sleep(0))
    await finished
    mgr._followup_watchers[info.id] = finished
    mgr._followup_watcher_infos[info.id] = info
    mgr._audit_followup = lambda *a, **k: None

    async def _wedged_announce(*_a, **_k):
        await asyncio.sleep(3600)

    mgr._announce_followup_failure = AsyncMock(side_effect=_wedged_announce)
    return root


@pytest.mark.asyncio
async def test_a_cancellation_at_the_handover_write_still_re_admits_the_run(
    monkeypatch, tmp_path, caplog
):
    """An outer cancellation may take the write's ANSWER; it may not take the re-admission.

    The handover write is shielded and drained by ``_write_state_off_loop``, so the
    queue can already be on disk when the caller's ``wait_for`` expires. What the
    cancellation removes is the returned answer, and with it the ``clear_tombstone``
    that the answer gates -- the step that makes a persisted queue reachable, since
    ``list_orphans`` skips any folder holding a tombstone. Skipping it therefore
    converts a recoverable handover into a silent, permanent loss.

    Driven as a real round trip: the write blocks, an outer ``wait_for`` expires on it,
    and recoverability is read from ``list_orphans`` rather than from a log line.
    """
    from kiro_crew.subagent_persistence import list_orphans, write_tombstone

    mgr = _make_manager()
    info = _info()
    queued = ["a queued follow_up whose handover write is cancelled"]
    await _arm_undelivered_followups(mgr, info, tmp_path, monkeypatch, queued)

    # The terminal record for a completed run carries one, and it is what excludes the
    # folder from the next start's reconciliation.
    write_tombstone(info.id, cause="timeout", recovery_action="report")
    assert info.id not in {
        o["id"] for o in list_orphans()
    }, "the fixture must start from a run orphan recovery cannot see"

    reached_write = asyncio.Event()

    async def _blocks_until_cancelled(*_a, **_kw):
        reached_write.set()
        await asyncio.sleep(3600)

    mgr._write_state_off_loop = AsyncMock(side_effect=_blocks_until_cancelled)

    with caplog.at_level("WARNING"):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(mgr.cancel_all(cancellation_budget=0.0), timeout=0.2)

    assert reached_write.is_set(), (
        "the cancellation did not land on the handover write, so this test proves "
        "nothing about that window"
    )
    visible = {o["id"] for o in list_orphans()}
    assert info.id in visible, (
        "the handover write was cancelled and the run stayed hidden behind its "
        "tombstone, so the queued follow_up is unrecoverable"
    )
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert (
        "re-admitted to orphan recovery" in messages
    ), f"the run was re-admitted but nothing said so: {messages}"


def test_every_cancellable_wait_before_a_re_admission_re_admits_on_cancel():
    """Each await that a cancellation can land on ahead of a re-admission carries it.

    Three awaits in ``cancel_all`` sit between an outstanding obligation and the
    ``clear_tombstone`` that discharges it: the handover write, the terminal-report
    drain, and the straggler gather. ``CancelledError`` is not an ``Exception``, so an
    ``except Exception`` guard does not see it and the re-admission after the await is
    simply skipped.

    Read structurally because the property is about every such handler rather than one
    path: a fourth await added later in front of a re-admission is the same defect, and
    a test that drives only the paths known today cannot fail on it. Each handler must
    also re-raise -- swallowing a cancellation would leave shutdown believing this phase
    completed.
    """
    from kiro_crew.subagent_manager import cancellation as cancel_mod

    source = textwrap.dedent(inspect.getsource(cancel_mod.CancellationCoordinator))
    tree = ast.parse(source)
    impl = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "cancel_all_impl"
    )

    def _is_cancelled_error(node) -> bool:
        return ast.unparse(node).replace("'", '"').endswith("CancelledError")

    handlers = [
        handler
        for node in ast.walk(impl)
        if isinstance(node, ast.Try)
        for handler in node.handlers
        if handler.type is not None and _is_cancelled_error(handler.type)
    ]
    readmitting = [
        handler
        for handler in handlers
        if any(
            isinstance(call.func, ast.Name) and call.func.id.startswith("_readmit")
            for call in ast.walk(handler)
            if isinstance(call, ast.Call)
        )
    ]
    assert len(readmitting) >= 3, (
        "cancel_all has three awaits a cancellation can land on ahead of a tombstone "
        f"re-admission; only {len(readmitting)} of its CancelledError handlers re-admit"
    )
    for handler in readmitting:
        assert any(
            isinstance(node, ast.Raise) and node.exc is None for node in ast.walk(handler)
        ), "a CancelledError handler re-admitted the run but swallowed the cancellation"


@pytest.mark.asyncio
async def test_a_surviving_tombstone_is_reported_as_unrecoverable(monkeypatch, tmp_path, caplog):
    """A tombstone that outlives the removal attempt hides the run, so say so.

    ``list_orphans`` skips the folder, which means the persisted queue is never read
    back. The operator log is the only remaining signal, so it has to carry the
    outcome the folder is actually in rather than the one the code attempted.
    """
    from kiro_crew.subagent_persistence import list_orphans, write_tombstone

    mgr = _make_manager()
    info = _info()
    queued = ["a queued follow_up behind a tombstone that cannot be removed"]
    await _arm_undelivered_followups(mgr, info, tmp_path, monkeypatch, queued)

    write_tombstone(info.id, cause="timeout", recovery_action="report")

    real_unlink = pathlib.Path.unlink

    def _unlink_refusing_the_tombstone(self, *args, **kwargs):
        if self.name == "tombstone.json":
            raise PermissionError("the tombstone is held open")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", _unlink_refusing_the_tombstone)

    with caplog.at_level("WARNING"):
        await mgr.cancel_all(cancellation_budget=0.0)

    visible = {o["id"] for o in list_orphans()}
    assert (
        info.id not in visible
    ), "the fixture did not reproduce a hidden run, so this test proves nothing"
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "re-admitted to orphan recovery" not in messages, (
        "the run is invisible to orphan recovery, yet the log claims it was "
        f"re-admitted: {messages}"
    )
    assert (
        "not recoverable" in messages
    ), f"a permanently lost follow_up queue was not reported as lost: {messages}"


@pytest.mark.asyncio
async def test_a_folder_that_never_had_a_tombstone_counts_as_recoverable(
    monkeypatch, tmp_path, caplog
):
    """The other False: nothing to remove means orphan recovery can already see it.

    This is the common shape -- a run cancelled before any terminal record exists --
    so a check that demands a removed file would report every ordinary shutdown as
    data loss and bury the one case that is.
    """
    from kiro_crew.subagent_persistence import list_orphans

    mgr = _make_manager()
    info = _info()
    queued = ["a queued follow_up on a folder with no tombstone"]
    await _arm_undelivered_followups(mgr, info, tmp_path, monkeypatch, queued)

    with caplog.at_level("WARNING"):
        await mgr.cancel_all(cancellation_budget=0.0)

    recovered = {o["id"]: o for o in list_orphans()}
    assert (
        recovered.get(info.id, {}).get("pending_followups") == queued
    ), f"the next start cannot read the queue back: {recovered.get(info.id)}"
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert (
        "not recoverable" not in messages
    ), f"a recoverable queue was reported as lost: {messages}"


def test_an_unreadable_folder_is_reported_as_not_visible(monkeypatch, tmp_path):
    """Fail closed: a check that cannot answer must not answer yes.

    The predicate exists so a caller can CLAIM reachability. A stat that errors
    leaves reachability unknown, and treating unknown as reachable is the one
    direction that loses the obligation silently.
    """
    import pathlib as _pathlib

    from kiro_crew.subagent_persistence import create_agent_folder, orphan_recovery_can_see

    root = tmp_path / "subagents"
    root.mkdir()
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", root)
    create_agent_folder("b2c3d4e5", task="t")

    assert orphan_recovery_can_see("b2c3d4e5") is True

    def _exists_that_cannot_answer(self):
        raise OSError("the registry is unreadable")

    monkeypatch.setattr(_pathlib.Path, "exists", _exists_that_cannot_answer)
    assert orphan_recovery_can_see("b2c3d4e5") is False


@pytest.mark.asyncio
async def test_a_queue_shutdown_could_not_announce_is_reported_by_the_next_start(
    monkeypatch, tmp_path
):
    """The round trip, end to end: persisted is not discharged until something reports it.

    Shutdown cannot always reach the parent, so it writes the queue down. That write is
    only worth making if a later start reads it back and tells the parent, because the
    parent was promised a completion event per queued message and nothing else downstream
    carries them. This drives both halves against one temp registry with the real writers.
    """
    import kiro_crew.subagent as mod
    from kiro_crew.subagent_persistence import create_agent_folder, list_orphans, update_state

    root = tmp_path / "subagents"
    root.mkdir()
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", root)
    monkeypatch.setattr(mod, "_REPORT_DRAIN_TIMEOUT", 3600.0)

    queued = ["the follow_up nobody dispatched"]

    # --- shutdown half: the announcement's own _on_done raises, which it swallows ---
    mgr = _make_manager()
    info = _info()
    info.parent_session_key = "dashboard:default"
    info.pending_followups = list(queued)
    create_agent_folder(info.id, task="t", parent_session="dashboard:default")
    update_state(info.id, pid=99999)

    async def _raising_on_done(_synthetic):
        raise RuntimeError("the parent's slot is gone")

    mgr._on_done = AsyncMock(side_effect=_raising_on_done)
    finished = asyncio.create_task(asyncio.sleep(0))
    await finished
    mgr._followup_watchers[info.id] = finished
    mgr._followup_watcher_infos[info.id] = info
    mgr._audit_followup = lambda *a, **k: None

    await mgr.cancel_all(cancellation_budget=30.0)

    assert info.pending_followups == queued, (
        "a swallowed announcement failure read as delivery and cleared the queue: "
        f"{info.pending_followups}"
    )
    recovered = {o["id"]: o for o in list_orphans()}
    assert (
        recovered.get(info.id, {}).get("pending_followups") == queued
    ), f"the queue did not reach disk where the next start reads it: {recovered.get(info.id)}"

    # --- recovery half: the next start must tell the parent before tombstoning ---
    told: list[str] = []
    mgr2 = _make_manager()
    monkeypatch.setattr(mod, "has_dashboard_surface", lambda _key: True)

    async def _capture(_session, message, _meta):
        told.append(message)
        return True

    mgr2._try_inject_orphan_notification = _capture
    monkeypatch.setattr(mgr2, "_is_pid_alive", lambda _pid: False)

    await mgr2._reconcile_orphans()

    assert told, "the next start told the parent nothing about the recovered run"
    assert queued[0] in " ".join(
        told
    ), f"the notice does not name the undispatched follow_up: {told}"
    assert (
        root / info.id / "tombstone.json"
    ).exists(), "the run was never tombstoned, so every later start re-reports it"


@pytest.mark.asyncio
async def test_an_undeliverable_notice_leaves_the_run_visible(monkeypatch, tmp_path):
    """The other direction: a tombstone written over an unreported queue loses it.

    The folder is the only place the messages still exist, and the tombstone is what hides
    it, so a notice that did not get out has to leave the folder for the next start. A run
    with nothing queued is tombstoned either way -- keeping it would re-report a courtesy
    notice on every start.
    """
    import kiro_crew.subagent as mod
    from kiro_crew.subagent_persistence import create_agent_folder, update_state

    root = tmp_path / "subagents"
    root.mkdir()
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", root)

    create_agent_folder("owed1", task="t", parent_session="dashboard:default")
    update_state("owed1", pid=99999, pending_followups=["still owed"])
    create_agent_folder("owed0", task="t", parent_session="dashboard:default")
    update_state("owed0", pid=99999)

    mgr = _make_manager()

    async def _cannot_notify(*_a, **_k):
        raise RuntimeError("no surface reachable")

    mgr._notify_orphan = _cannot_notify
    monkeypatch.setattr(mgr, "_is_pid_alive", lambda _pid: False)
    monkeypatch.setattr(mod, "has_dashboard_surface", lambda _key: False)

    await mgr._reconcile_orphans()

    assert not (
        root / "owed1" / "tombstone.json"
    ).exists(), "the queue was tombstoned away behind an undelivered notice"
    assert (
        root / "owed0" / "tombstone.json"
    ).exists(), "a run owing nothing was left visible, so it is re-reported on every start"


@pytest.mark.asyncio
async def test_a_delivered_notice_is_recorded_as_delivered_not_as_still_pending(
    monkeypatch, tmp_path
):
    """The recovery's own record has to agree with what the recovery did.

    Injection records the delivery, then the scan writes the run's tombstone. That write
    replaces the whole record instead of merging into it, so a pending action asserted
    over a delivered notice reads back as a run still owed one -- the folder is hidden
    either way, so nothing re-delivers it and the mark is the only surviving evidence.

    Both directions matter. A delivered notice must not read as pending, and a notice
    that never got out must not read as delivered: the pending value is what a later
    reader uses to tell the two apart, so a record that always says ``delivered``
    discharges the obligation exactly as wrongly as one that never says it.
    """
    import kiro_crew.subagent as mod
    from kiro_crew.subagent_persistence import (
        create_agent_folder,
        update_state,
        write_result_chunk,
    )

    root = tmp_path / "subagents"
    root.mkdir()
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", root)

    # A complete result makes the pending action "result_available", a value the
    # delivered mark cannot be confused with.
    create_agent_folder("told", task="t", parent_session="dashboard:default")
    write_result_chunk("told", "an answer")
    update_state("told", pid=99999, result_complete=True)

    mgr = _make_manager()
    monkeypatch.setattr(mod, "has_dashboard_surface", lambda _key: True)
    monkeypatch.setattr(mgr, "_is_pid_alive", lambda _pid: False)

    async def _injects(_session, _message, _meta):
        return True

    mgr._try_inject_orphan_notification = _injects

    await mgr._reconcile_orphans()

    told_tombstone = json.loads((root / "told" / "tombstone.json").read_text(encoding="utf-8"))
    assert told_tombstone["recovery_action"] == "delivered", (
        "the parent was told, but the record says the notice is still owed: "
        f"{told_tombstone['recovery_action']}"
    )

    # The other direction, on a second run whose injection does not get out.
    create_agent_folder("untold", task="t", parent_session="dashboard:default")
    write_result_chunk("untold", "an answer")
    update_state("untold", pid=99999, result_complete=True)

    mgr2 = _make_manager()
    monkeypatch.setattr(mgr2, "_is_pid_alive", lambda _pid: False)

    async def _does_not_inject(_session, _message, _meta):
        return False

    mgr2._try_inject_orphan_notification = _does_not_inject
    mgr2._send_orphan_slack_dm = AsyncMock(return_value=False)

    await mgr2._reconcile_orphans()

    untold_tombstone = json.loads((root / "untold" / "tombstone.json").read_text(encoding="utf-8"))
    assert untold_tombstone["recovery_action"] == "result_available", (
        "nothing was delivered, but the record claims it was: "
        f"{untold_tombstone['recovery_action']}"
    )


@pytest.mark.asyncio
async def test_no_discharge_is_written_against_a_delivery_that_did_not_happen(
    monkeypatch, tmp_path
):
    """Both discharge sites, one invariant: the record that hides a run waits for delivery.

    A tombstone is the discharge -- ``list_orphans`` skips any folder holding one -- so
    writing it while the thing that delivers has not delivered loses the outcome for good.
    Two sites can do that, and each is defensible read alone:

    * Recovery defers a notice to the end-of-scan digest. Counting that DEFERRAL as having
      told someone tombstones the folder before the digest runs at all, and the digest can
      fail -- which the DM path reports only if it answers whether it reached anyone.
    * Shutdown gives up WAITING for a run's teardown, which is allowed, so the run has not
      reached the ``finally`` that spawns its terminal report. No report task exists for the
      drain to find, so the straggler compensation cannot cover it, while the run's own
      cancel arm is still able to write a terminal tombstone.

    Both halves run against the real writers, and the third case here is the guard against
    over-correcting: a notice that WAS delivered must still discharge, or every start
    re-reports the same run forever.
    """
    import kiro_crew.subagent as mod
    from kiro_crew.subagent_persistence import create_agent_folder, update_state

    root = tmp_path / "subagents"
    root.mkdir()
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", root)
    monkeypatch.setattr(mod, "_REPORT_DRAIN_TIMEOUT", 3600.0)
    # No tab anywhere, so every notice takes the deferred digest path rather than injection.
    monkeypatch.setattr(mod, "has_dashboard_surface", lambda _key: False)

    # --- recovery half: the digest never reached anyone, so nothing may be discharged ---
    create_agent_folder("owed1", task="t", parent_session="dashboard:default")
    update_state("owed1", pid=99999, pending_followups=["still owed"])
    create_agent_folder("owed0", task="t", parent_session="dashboard:default")
    update_state("owed0", pid=99999)

    mgr = _make_manager()
    mgr._on_orphan_dm = None  # unwired DM: the fallback logs and reaches no one
    monkeypatch.setattr(mgr, "_is_pid_alive", lambda _pid: False)

    await mgr._reconcile_orphans()

    assert not (root / "owed1" / "tombstone.json").exists(), (
        "a notice only QUEUED for the digest was treated as delivered, so the run holding "
        "undispatched follow_up(s) was tombstoned and its queue is unrecoverable"
    )
    assert (
        root / "owed0" / "tombstone.json"
    ).exists(), "a run owing nothing was left visible, so it is re-reported on every start"

    # --- the same site, delivered: the discharge must still happen ---
    create_agent_folder("owed2", task="t", parent_session="dashboard:default")
    update_state("owed2", pid=99999, pending_followups=["still owed"])
    mgr2 = _make_manager()
    mgr2._on_orphan_dm = AsyncMock(return_value=True)
    monkeypatch.setattr(mgr2, "_is_pid_alive", lambda _pid: False)

    await mgr2._reconcile_orphans()

    assert (root / "owed2" / "tombstone.json").exists(), (
        "the digest DM reported delivery and the run was still left visible, so every "
        "later start re-reports it"
    )

    # --- shutdown half: a run abandoned mid-teardown owns no registered report ---
    mgr3 = _make_manager()
    info = _info()
    info.parent_session_key = "dashboard:default"
    create_agent_folder(info.id, task="t", parent_session="dashboard:default")

    started = asyncio.Event()
    release = asyncio.Event()

    async def _resists_its_cancel() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # Exactly the run this phase is bounded against: it does not come down, so it
            # never reaches the finally that would spawn its terminal report.
            await release.wait()

    task = asyncio.create_task(_resists_its_cancel())
    await started.wait()
    mgr3._agents[info.id] = info
    mgr3._tasks[info.id] = task

    # A spent budget: every phase is allowed to wait zero seconds.
    await mgr3.cancel_all(cancellation_budget=0.0)

    release.set()
    await task

    assert info._shutdown_outcome_abandoned, (
        "shutdown gave up on this run's teardown before its terminal report was "
        "registered and recorded nothing, so the drain cannot see it, the straggler "
        "re-admission cannot cover it, and its own cancel arm will tombstone the outcome"
    )

    # The mark is only worth setting if the discharge site reads it, so assert the guard
    # where the write lives rather than trusting the flag to be honoured.
    from kiro_crew.subagent_manager.run import RunEventCoordinator

    run_tree = ast.parse(textwrap.dedent(inspect.getsource(RunEventCoordinator._run_impl)))
    guarded = False
    for node in ast.walk(run_tree):
        if not isinstance(node, ast.If):
            continue
        if not any(
            isinstance(sub, ast.Attribute) and sub.attr == "_shutdown_outcome_abandoned"
            for sub in ast.walk(node.test)
        ):
            continue
        if any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "_write_tombstone"
            and len(call.args) > 1
            and isinstance(call.args[1], ast.Constant)
            and call.args[1].value == "cancelled"
            for call in ast.walk(node)
        ):
            guarded = True
            break
    assert guarded, (
        "the cancelled-run tombstone is not gated on _shutdown_outcome_abandoned, so a run "
        "shutdown abandoned still hides its own undelivered outcome from orphan recovery"
    )


@pytest.mark.asyncio
async def test_the_followup_queue_is_bounded_where_it_grows_and_where_it_is_written(
    monkeypatch, tmp_path
):
    """One pair of bounds owns the queue's whole life, and overflow leaves evidence.

    A bound applied where the value is DISPLAYED leaves the store unbounded: the notice
    slices scale with the message count, so N messages buy N times the budget while the
    list and the ``state.json`` holding it keep growing. So admission refuses past a count
    and a per-message size, and the shutdown handover writes under the same two -- with the
    part that does not fit carried as a COUNT, because a bare slice there would drop the
    evidence that anything was dropped.
    """
    import kiro_crew.subagent as mod
    from kiro_crew.subagent_persistence import create_agent_folder, list_orphans

    root = tmp_path / "subagents"
    root.mkdir()
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", root)
    monkeypatch.setattr(mod, "_REPORT_DRAIN_TIMEOUT", 3600.0)

    # --- admission: refuse, never accept-and-shorten ---
    mgr = _make_manager()
    info = _info()
    mgr._agents[info.id] = info
    monkeypatch.setattr(mgr, "_arm_followup_watcher", lambda _info: None)

    ok, detail = await mgr.follow_up_run(info.id, "x" * (mod._MAX_FOLLOWUP_MESSAGE_CHARS + 1))
    assert not ok and "too_long" in detail, f"an oversized follow_up was admitted: {detail}"
    assert info.pending_followups == [], (
        "the refused message was queued anyway, so the per-message bound shortens nothing "
        f"and bounds nothing: {info.pending_followups}"
    )

    for index in range(mod._MAX_PENDING_FOLLOWUPS):
        ok, detail = await mgr.follow_up_run(info.id, f"m{index}")
        assert ok, f"message {index} was refused below the cap: {detail}"
    ok, detail = await mgr.follow_up_run(info.id, "one too many")
    assert not ok and "queue_full" in detail, f"the count bound did not hold: {detail}"
    assert len(info.pending_followups) == mod._MAX_PENDING_FOLLOWUPS, (
        "the queue grew past its own cap: " f"{len(info.pending_followups)}"
    )

    # --- persistence: a queue already above the cap is written WITH its overflow count ---
    mgr2 = _make_manager()
    over = _info()
    over.parent_session_key = "dashboard:default"
    above_cap = mod._MAX_PENDING_FOLLOWUPS + 5
    over.pending_followups = [f"q{i}" for i in range(above_cap)]
    create_agent_folder(over.id, task="t", parent_session="dashboard:default")

    # The watcher is already finished, so the drain reaches the announcement with the queue
    # still set, and the announcement reports NOT delivered -- which is what sends the
    # handover down the persistence path this half is about.
    finished = asyncio.create_task(asyncio.sleep(0))
    await finished
    mgr2._followup_watchers[over.id] = finished
    mgr2._followup_watcher_infos[over.id] = over
    mgr2._audit_followup = lambda *a, **k: None
    mgr2._announce_followup_failure = AsyncMock(return_value=False)

    await mgr2.cancel_all(cancellation_budget=30.0)

    written = {o["id"]: o for o in list_orphans()}.get(over.id, {})
    persisted = written.get("pending_followups") or []
    assert len(persisted) == mod._MAX_PENDING_FOLLOWUPS, (
        "the handover wrote an unbounded queue into state.json, which is the store the "
        f"bound exists to hold: {len(persisted)}"
    )
    assert written.get("pending_followups_overflow") == above_cap - mod._MAX_PENDING_FOLLOWUPS, (
        "the messages that did not fit were sliced away with no record that anything was "
        f"dropped: {written.get('pending_followups_overflow')!r}"
    )


def test_no_shutdown_phase_bounds_a_cancelled_task_with_wait_for():
    """Cancellation budget: a phase must bound how long IT waits.

    Every gather in this method is over tasks the method has already cancelled.
    ``wait_for`` around one cancels them a second time and then awaits the result, so
    a task slow to honour its cancel holds the phase for as long as it likes -- the
    bound reads as a bound and binds nothing. A phase that must bound its wait uses
    ``asyncio.wait``, which returns on timeout; a phase that must run to completion
    (the straggler compensation) uses a bare gather on purpose. ``wait_for`` around a
    coroutine the site starts itself is untouched by this rule: cancelling that
    coroutine does stop it.
    """
    from kiro_crew.subagent_manager.cancellation import CancellationCoordinator

    tree = ast.parse(textwrap.dedent(inspect.getsource(CancellationCoordinator.cancel_all_impl)))

    def _is_asyncio(node, name):
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == name
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "asyncio"
        )

    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if _is_asyncio(node, "wait_for") and node.args and _is_asyncio(node.args[0], "gather")
    ]
    assert not offenders, (
        "a shutdown phase bounds a gather of already-cancelled tasks with wait_for "
        f"(offset line(s) {offenders}); use asyncio.wait so the bound is on this site's wait"
    )

    # The rule is only worth having if the method still HAS bounded phases to get wrong.
    bounded = [node for node in ast.walk(tree) if _is_asyncio(node, "wait")]
    assert bounded, "no bounded wait remains in the shutdown path; this test is stale"


def test_the_queue_is_dropped_only_on_the_branch_that_announced_it():
    """Drain ownership, asserted on the source rather than on one exercised path.

    A behavioural test sees the branch it drives. The guarantee is about every
    branch: no path may drop the in-memory queue without the parent having been
    told, because the queue is the last record of what went undispatched.
    """
    from kiro_crew.subagent_manager.cancellation import CancellationCoordinator

    tree = ast.parse(textwrap.dedent(inspect.getsource(CancellationCoordinator.cancel_all_impl)))

    def _clears(node):
        found = []
        for child in ast.walk(node):
            if not isinstance(child, ast.Assign):
                continue
            for target in child.targets:
                if isinstance(target, ast.Attribute) and target.attr == "pending_followups":
                    found.append(child)
        return found

    all_clears = _clears(tree)
    assert all_clears, "the drain no longer clears the queue anywhere; this test is stale"

    announced_clears = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name):
            if node.test.id == "announce_failure":
                for branch_node in node.orelse:
                    announced_clears.extend(_clears(branch_node))

    assert len(announced_clears) == len(all_clears), (
        "the queue is cleared somewhere other than the announce-succeeded branch, "
        "so a bounded announcement that gives up can destroy it and not report it"
    )


def test_an_untrusted_owner_can_only_be_appended_to_the_subject_list():
    """Per-entry provenance: appending withholds a delivery and cannot route one.

    The value arrives on the note and the producer does not own it, so the safety
    comes from the polarity of the mutation. An assignment or a removal would let a
    forged owner replace the subjects that must permit, turning a tightening into a
    redirect.
    """
    from kiro_crew.notifications.bridge import BridgeDispatcher

    tree = ast.parse(textwrap.dedent(inspect.getsource(BridgeDispatcher._vet)))

    initialisers = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == "subjects":
                    initialisers += 1
                if isinstance(target, ast.Subscript):
                    value = target.value
                    assert not (isinstance(value, ast.Name) and value.id == "subjects"), (
                        "a subject slot is overwritten, so an untrusted owner can "
                        "displace one that must permit"
                    )

    assert initialisers == 1, (
        f"the subject list is bound {initialisers} times; only the initialiser may bind it, "
        "or an untrusted owner can replace the subjects that must permit"
    )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if isinstance(owner, ast.Name) and owner.id == "subjects":
            assert node.func.attr == "append", (
                f"the subject list is mutated with .{node.func.attr}(), which can remove "
                "or reorder a subject that must permit"
            )
