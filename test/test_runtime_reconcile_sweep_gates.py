"""Tests for the lease gate on every sweep that signals a process.

The periodic pid sweep, the untracked-MCP sweep, the scope reaper, the subagent orphan
reconcile with the finished-result signal beside it, the reset ladder, the cron reaper
and a teardown whose scope already unwound each ask the gate about one pid at the
decision point. The kill phase's own row and the lock-open failure handler are here too.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from runtime_reconcile_helpers import agent_root_fixture  # noqa: F401
from runtime_reconcile_helpers import (
    _MEMBER_A,
    _MEMBER_B,
    _PLAIN_A,
    _PLAIN_B,
    _cleanup,
    _LeaseHolder,
)

from kiro_crew import runtime_ownership as ro
from kiro_crew import runtime_reconcile as rr

# Module-wide, like the other runtime reconciler test modules: the rootdir conftest
# leaves runtime_ownership's tables as this module's own tests leave them, and the
# tests that need a clean table call ``ro._reset_for_tests()`` themselves.
pytestmark = pytest.mark.keep_runtime_ownership_tables


# ── the sweep gates ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_periodic_pid_sweep_withholds_a_leased_pid() -> None:
    """MUTATION TARGET: ``_kill_authorized`` in ``_sweep_periodic_pids``.

    The shield cannot cover this: it is gathered before the candidate scan and an
    event-loop hop before the kill, so a lease taken in between is invisible to
    it and visible here.
    """
    ro._reset_for_tests()
    service, recorded = _cleanup(candidates=[4242, 4343])
    holder = _LeaseHolder(4242)
    await holder.take()
    try:
        await service._sweep_periodic_pids()
    finally:
        await holder.give_back()
    assert recorded["pid_kills"] == [4343], "the leased pid must not reach the killer"


@pytest.mark.asyncio
async def test_the_periodic_pid_sweep_still_kills_an_unleased_orphan() -> None:
    """CONTROL: without this, a gate that refused everything would satisfy the
    test above and stop the sweep working at all."""
    ro._reset_for_tests()
    service, recorded = _cleanup(candidates=[4242, 4343])
    await service._sweep_periodic_pids()
    assert sorted(recorded["pid_kills"]) == [4242, 4343]


@pytest.mark.asyncio
async def test_the_untracked_mcp_sweep_withholds_a_leased_pid() -> None:
    """MUTATION TARGET: ``_kill_authorized`` in ``_sweep_untracked_mcps``."""
    ro._reset_for_tests()
    service, recorded = _cleanup(candidates=[], mcp_candidates=[5151, 5252])
    holder = _LeaseHolder(5151)
    await holder.take()
    try:
        await service._sweep_untracked_mcps()
    finally:
        await holder.give_back()
    assert recorded["mcp_kills"] == [5252]


@pytest.mark.asyncio
async def test_an_unanswerable_gate_withholds_the_pid() -> None:
    """MUTATION TARGET: the fail-closed answer inside ``_kill_authorized``.

    A gate that raises is a refusal: deferring a housekeeping kill one tick costs
    nothing that killing a live runtime would not cost more.

    Two candidates, and the gate raises for only one of them, because the two
    wrong behaviours are otherwise indistinguishable. A refusal that is converted
    to ``False`` withholds that pid and sweeps the other; an exception that
    escapes instead aborts the whole pass, which also leaves the first pid alive
    and would satisfy a single-candidate assertion while skipping every remaining
    pid and everything after the loop.
    """
    from unittest.mock import patch

    ro._reset_for_tests()
    service, recorded = _cleanup(candidates=[6161, 6262])

    def raising_for_one(pid: int, *, reason: str, caller: str) -> bool:
        if pid == 6161:
            raise RuntimeError("gate unavailable")
        return True

    with patch("kiro_crew.session_cleanup.authorize_runtime_kill", raising_for_one):
        await service._sweep_periodic_pids()

    assert recorded["pid_kills"] == [6262], (
        "the unanswerable pid is withheld and the answerable one is still swept; "
        f"got {recorded['pid_kills']}"
    )


# ── the scope reaper's gate ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_scope_reaper_does_not_stop_a_unit_holding_a_leased_pid(
    tmp_path: Path,
) -> None:
    """MUTATION TARGET: the ownership pass BEFORE ``stop_unit``.

    Stopping the unit is itself the kill: systemd terminates the whole cgroup. So a
    gate consulted only on the processes that survived the stop is consulted after
    the thing it exists to prevent, and a leased runtime is already dead by then.

    The pre-stop pass QUERIES ownership rather than calling the gate, because the
    gate's allow path writes a kill attribution: a survey that ends in an abort
    would otherwise record a kill of every unleased member that was never
    signalled. So the gate must not be called at all on this path.
    """
    from unittest.mock import patch

    from kiro_crew import session_scope_reap as reap

    ro._reset_for_tests()
    scope = tmp_path / "run-test.scope"
    scope.mkdir()
    (scope / "cgroup.procs").write_text(f"{_MEMBER_A}\n{_MEMBER_B}\n", encoding="utf-8")

    stopped: list[str] = []
    signalled: list[tuple[int, int]] = []
    attributed: list[int] = []

    def signal_owned(
        pid: int,
        sig: int,
        members: list[int],
        scope_dir: Path,
        proc_root: Path,
        pinned: Any,
    ) -> tuple[bool, str]:
        signalled.append((pid, sig))
        return True, ""

    refusals: list[str] = []
    holder = _LeaseHolder(_MEMBER_A)
    await holder.take()
    real_gate = reap.authorize_runtime_kill
    try:
        with patch.object(
            reap,
            "authorize_runtime_kill",
            lambda pid, **kw: (attributed.append(pid) or real_gate(pid, **kw)),
        ):
            cleared = reap._reclaim_scope(
                scope,
                "run-test.scope",
                proc_root=tmp_path,
                stop_unit=lambda unit: stopped.append(unit) or True,
                signal_owned=signal_owned,
                sleep=lambda secs: None,
                on_refusal=refusals.append,
            )
    finally:
        await holder.give_back()

    assert stopped == [], "the unit is never stopped while one of its members is leased"
    assert signalled == [], "and its unleased neighbour is not signalled either"
    assert cleared is False, "the scope was not reclaimed"
    assert refusals == ["still leased"], "the caller can record a refusal, not a failure"
    assert attributed == [], (
        "the survey must not call the attributing gate, or the abort leaves a kill "
        f"recorded for a process nothing signalled; got {attributed}"
    )


def test_the_scope_reaper_signals_every_member_when_none_is_leased(tmp_path: Path) -> None:
    """CONTROL for the test above, on every platform.

    Runs unskipped because the ladder resolves its signal numbers through
    ``platform_compat``, which defines both everywhere. A control that a refusal test
    depends on is worth least on the platform where it does not run: the refusal would
    pass for free there, which is the whole failure mode the control exists to rule out.
    """
    from kiro_crew import session_scope_reap as reap

    ro._reset_for_tests()
    scope = tmp_path / "run-plain.scope"
    scope.mkdir()
    (scope / "cgroup.procs").write_text(f"{_PLAIN_A}\n{_PLAIN_B}\n", encoding="utf-8")
    signalled: list[int] = []
    reap._reclaim_scope(
        scope,
        "run-plain.scope",
        proc_root=tmp_path,
        stop_unit=lambda unit: True,
        signal_owned=lambda pid, *_rest: (signalled.append(pid) or (True, "")),
        sleep=lambda secs: None,
    )
    assert set(signalled) == {_PLAIN_A, _PLAIN_B}


def test_the_slice_enumerator_reads_every_scope_under_the_slice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kernel truth is the union of the slice's own members and every scope's."""
    from kiro_crew import session_scope_reap as reap

    slice_dir = tmp_path / "kirocrew-agents-tok.slice"
    slice_dir.mkdir()
    (slice_dir / "cgroup.procs").write_text("11\n", encoding="utf-8")
    for name, body in (("a.scope", "22\n33\n"), ("b.scope", "44\n")):
        scope = slice_dir / name
        scope.mkdir()
        (scope / "cgroup.procs").write_text(body, encoding="utf-8")
    # A sibling that is not a scope must not contribute.
    other = slice_dir / "nested.slice"
    other.mkdir()
    (other / "cgroup.procs").write_text("99\n", encoding="utf-8")

    monkeypatch.setattr(reap, "_instance_scope_dir", lambda: (slice_dir, ""))
    assert reap.instance_slice_pids() == {11, 22, 33, 44}


def test_an_unresolvable_slice_reads_as_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty is the honest answer, and the reconciler's registry refusal is what
    stops an empty kernel reading from being acted on as an empty install."""
    from kiro_crew import session_scope_reap as reap

    monkeypatch.setattr(reap, "_instance_scope_dir", lambda: (None, "no cgroup dir"))
    assert reap.instance_slice_pids() == set()


# ── the subagent orphan reconcile ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_orphan_reconcile_withholds_a_leased_pid(agent_root: Path) -> None:
    """MUTATION TARGET: the gate in ``_reconcile_orphans_impl``.

    ``state.json`` is a record this run wrote before the restart and says nothing
    about who is on the process now. On a shared runtime the parent and every
    sibling are on the same pid, so a per-run file naming it is not authority to
    end it. The tombstone is still written -- this run IS over -- which is the
    difference between reporting the run finished and killing the process.
    """
    from unittest.mock import MagicMock, patch

    from kiro_crew.subagent import SubagentManager
    from kiro_crew.subagent_persistence import create_agent_folder, update_state

    ro._reset_for_tests()
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    create_agent_folder("leased-orphan", task="a shared runtime")
    update_state("leased-orphan", pid=8181)

    holder = _LeaseHolder(8181)
    await holder.take()
    try:
        with (
            patch.object(manager, "_is_pid_alive", return_value=True),
            patch.object(manager, "_is_orphan_process", return_value=True),
            patch.object(manager, "_kill_orphan_pid") as mock_kill,
            patch.object(manager, "_notify_orphan", return_value=None),
        ):
            await manager._reconcile_orphans()
    finally:
        await holder.give_back()

    mock_kill.assert_not_called()
    assert (
        agent_root / "leased-orphan" / "tombstone.json"
    ).exists(), "the run is over either way; only the signal is withheld"


# ── the finished-result completeness signal ──────────────────────────────────


def test_a_flagged_result_is_the_agents_answer(agent_root: Path) -> None:
    """MUTATION TARGET: the ``result_is_whole`` clause in ``tombstone_recovery_action``.

    ``result.txt`` streams in chunk by chunk, so bytes on disk prove only that a
    token arrived. A run records ``result_complete`` after it rewrote that file
    whole from memory, which makes the flag the proof: a flagged result is the
    agent's answer, ``result_available``, never a fragment.
    """
    from kiro_crew.subagent_manager.monitoring import tombstone_recovery_action
    from kiro_crew.subagent_persistence import create_agent_folder, write_result_chunk

    create_agent_folder("flagged-finished", task="a finished answer")
    write_result_chunk("flagged-finished", "the whole answer, every byte of it", fresh=True)
    state = {"id": "flagged-finished", "result_complete": True}

    assert (
        tombstone_recovery_action("flagged-finished", state) == "result_available"
    ), "a whole answer the run flagged is the agent's answer, not a fragment"


def test_an_unflagged_result_still_under_claims_as_a_fragment(agent_root: Path) -> None:
    """The safe direction: bytes the run never flagged are a fragment.

    A run interrupted mid-stream wrote result bytes but never recorded
    ``result_complete``. That genuinely partial result must still be announced
    as cut off, never promoted to a whole answer.
    """
    from kiro_crew.subagent_manager.monitoring import tombstone_recovery_action
    from kiro_crew.subagent_persistence import create_agent_folder, write_result_chunk

    create_agent_folder("truly-partial", task="an interrupted stream")
    write_result_chunk("truly-partial", "an opening sentence the restart", fresh=True)
    state = {"id": "truly-partial"}  # never completed: no flag

    assert tombstone_recovery_action("truly-partial", state) == "partial_result", (
        "the run never flagged its result, so the bytes are a genuine fragment "
        "and must stay under-claimed"
    )


@pytest.mark.asyncio
async def test_the_reconcile_announces_a_finished_run_as_finished(agent_root: Path) -> None:
    """End to end: a run that finished before the restart is announced as finished.

    Drives the real orphan reconcile over a run whose whole answer is on disk
    and flagged. The notification must read ``finished before gateway restart``
    ("Use the read tool to retrieve it"), NOT ``cut off mid-turn`` ("read it as
    an unfinished fragment"), and the terminal ``recovery_action`` written to
    the tombstone must be ``result_available``.
    """
    from unittest.mock import MagicMock, patch

    from kiro_crew.subagent import SubagentManager
    from kiro_crew.subagent_persistence import (
        create_agent_folder,
        read_tombstone,
        update_state,
        write_result_chunk,
    )

    ro._reset_for_tests()
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    create_agent_folder("window-orphan", task="a finished answer caught by a restart")
    write_result_chunk(
        "window-orphan", "the whole answer, written minutes before the crash", fresh=True
    )
    # A dead pid so the reconcile tombstones without a kill, and an empty
    # parent_session so the real notifier falls through to the digest DM where
    # its message is observable.
    update_state("window-orphan", pid=424242, parent_session="", result_complete=True)

    captured: list[str] = []

    async def _capture_dm(digest: str) -> None:
        captured.append(digest)

    with (
        patch.object(manager, "_is_pid_alive", return_value=False),
        patch.object(manager, "_send_orphan_slack_dm", side_effect=_capture_dm),
    ):
        await manager._reconcile_orphans()

    assert captured, "the finished orphan must produce a notification"
    note = captured[0]
    assert (
        "✅ finished before gateway restart" in note
    ), f"a finished answer is a whole result; got {note!r}"
    assert "cut off mid-turn" not in note, "the finished answer must not be called a fragment"
    tomb = read_tombstone("window-orphan")
    assert (
        tomb and tomb.get("recovery_action") == "result_available"
    ), f"the terminal recovery_action must record a whole result; got {tomb!r}"


# ── the subagent reset ladder ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_reset_ladder_withholds_the_kill_and_the_shared_child_sweep() -> None:
    """MUTATION TARGET: the gate in ``_sigkill_session_impl``.

    The recycle check inside the verified kill answers a different question --
    whether this pid is still the process we recorded -- and a yes to that is not
    a yes to this. With session sharing on, the parent and every sibling live on
    the same root, so killing its tree over one hung reset ends work nobody asked
    to end.

    The escaped-children sweep is withheld WITH the root's signal, not alongside
    it. The recorded child set is the shared root's whole descendant tree, so
    sweeping it after sparing the root would spare the process and kill the
    processes it depends on -- worse than either ending it or leaving it alone.
    The surviving tree is the reconciler's to count, and the refusal is RETURNED
    so the caller's record cannot say the run was reaped.
    """
    from unittest.mock import MagicMock, patch

    from kiro_crew.process_identity import ProcessHandle
    from kiro_crew.subagent import SubagentManager

    ro._reset_for_tests()
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    handle = ProcessHandle(pid=7171, start_id="start-identity", pgid=7171, child_pids={7172: 7172})
    # The lease belongs to a co-tenant, not to this teardown: tearing_down names
    # nothing, so the subject's own release finds nothing to give up and the
    # surviving lease is somebody else's.
    manager._sessions.tearing_down = lambda key: []

    holder = _LeaseHolder(7171)
    await holder.take()
    swept: list[Any] = []
    try:
        with (
            patch("kiro_crew.acp.client._kill_escaped_children", side_effect=swept.append),
            patch("kiro_crew.subagent.kill_verified_process") as mock_verified_kill,
        ):
            failure = await manager._sigkill_session("hung", handle)
    finally:
        await holder.give_back()

    mock_verified_kill.assert_not_called()
    assert swept == [], "the recorded child set is the SHARED tree; sparing the root spares it"
    assert failure and "lease" in failure, (
        "the refusal is the caller's record of a tree left standing; " f"got {failure!r}"
    )


class _TeardownProvider:
    """A provider stand-in the ownership module recognises as a lease holder.

    Recognised by its lease SLOT (`_runtime_lease`, None or a string), which is
    how ``runtime_ownership._lease_holder`` identifies one -- deliberately not by
    having the methods, because a MagicMock answers every hasattr. The release
    delegates to the real table, so this exercises the production
    ``release_session_lease`` seam rather than a reimplementation of it.
    """

    def __init__(self, lease: str) -> None:
        self._runtime_lease: str | None = lease

    async def release_runtime_lease(self) -> None:
        if self._runtime_lease is not None:
            await ro.RUNTIME_OWNERSHIP.release(self._runtime_lease)
            self._runtime_lease = None


@pytest.mark.asyncio
async def test_a_teardown_is_not_refused_by_the_lease_it_is_tearing_down() -> None:
    """MUTATION TARGET: releasing the subject's own lease before asking the gate.

    A reset releases the lease inside ``provider.shutdown()``, so every await
    before it is a point where the teardown can hang and land on this path with
    the lease still held. Asking the gate then lets the session being destroyed
    refuse its own last-resort kill: the wedged process survives, and its pid goes
    on being refused by every other sweep for the gateway's life while the run
    records a false "leased by another tenant".

    The lease here belongs to the very session under teardown, reachable through
    ``tearing_down`` after its pop, so the kill MUST proceed.
    """
    from unittest.mock import patch

    from kiro_crew.cron import CronService
    from kiro_crew.process_identity import ProcessHandle

    ro._reset_for_tests()
    handle = ProcessHandle(pid=9292, start_id="abc", pgid=9292, child_pids={})
    holder = _LeaseHolder(9292)
    await holder.take()
    assert rr._leases_on_pid(9292) == 1, "precondition: the subject holds the only lease"

    provider = _TeardownProvider(holder.lease)
    entry = type(
        "_Entry",
        (),
        {"session": type("_S", (), {"provider": provider})(), "handle": handle},
    )()

    class _Sessions:
        def tearing_down(self, key: str) -> list[object]:
            return [entry]

    class _Svc:
        _sessions = _Sessions()

    killed: list[int] = []

    async def fake_kill(h: Any, *, who: str, key: str, child_helpers: Any) -> None:
        killed.append(h.pid)
        return None

    with (
        patch("kiro_crew.cron.kill_verified_process", fake_kill),
        patch("kiro_crew.session.child_process_helpers", lambda: (None, None, None)),
    ):
        failure = await CronService._sigkill_session(_Svc(), "cron:job", handle, who="Reaper")

    assert killed == [9292], (
        "the teardown's own lease must not withhold its last-resort kill; " f"failure={failure!r}"
    )
    assert failure is None


@pytest.mark.asyncio
async def test_another_tenants_lease_still_withholds_the_cron_kill() -> None:
    """The other side of the same rule: what survives the subject's own release is
    a lease held by a DIFFERENT tenant, and that one still refuses.

    Without this, releasing before the gate could be 'release everything and
    always kill', which is the original bug with extra steps.
    """
    from kiro_crew.cron import CronService
    from kiro_crew.process_identity import ProcessHandle

    ro._reset_for_tests()
    handle = ProcessHandle(pid=9393, start_id="abc", pgid=9393, child_pids={})
    holder = _LeaseHolder(9393)
    await holder.take()

    class _Sessions:
        def tearing_down(self, key: str) -> list[object]:
            return []  # the lease belongs to someone this teardown never popped

    class _Svc:
        _sessions = _Sessions()

    try:
        failure = await CronService._sigkill_session(_Svc(), "cron:job", handle, who="Reaper")
    finally:
        await holder.give_back()

    assert failure == "runtime still leased by another tenant"


@pytest.mark.asyncio
async def test_only_the_lease_of_the_process_being_killed_is_released() -> None:
    """MUTATION TARGET: matching the teardown entry on pid AND on start identity.

    A key can hold several teardowns at once -- a run's own finally reset popped a
    session and hung, then a successor's reset popped its session and hung too --
    which is the case ``tearing_down`` exists for. Releasing every entry's lease
    would hand this kill permission over a sibling's live process.

    Two siblings, each differing from the handle under the knife in exactly ONE
    field, so neither match test can hide behind the other:

    * same start identity, different pid -- only the pid test rejects it, and its
      lease must survive on its own pid;
    * same pid, different start identity -- only the identity test rejects it, and
      because it is a lease on the pid being killed, releasing it wrongly would let
      the kill proceed.
    """
    from kiro_crew.cron import CronService
    from kiro_crew.process_identity import ProcessHandle

    ro._reset_for_tests()
    killing = ProcessHandle(pid=9494, start_id="mine", pgid=9494, child_pids={})
    other_pid = ProcessHandle(pid=9595, start_id="mine", pgid=9595, child_pids={})
    other_identity = ProcessHandle(pid=9494, start_id="theirs", pgid=9494, child_pids={})

    by_pid = _LeaseHolder(9595)
    await by_pid.take()
    by_identity = _LeaseHolder(9494)
    await by_identity.take()

    def _entry(handle: Any, lease: str) -> Any:
        return type(
            "_Entry",
            (),
            {
                "session": type("_S", (), {"provider": _TeardownProvider(lease)})(),
                "handle": handle,
            },
        )()

    entries = [_entry(other_pid, by_pid.lease), _entry(other_identity, by_identity.lease)]

    class _Svc:
        _sessions = type("_S", (), {"tearing_down": lambda self, key: entries})()

    try:
        failure = await CronService._sigkill_session(_Svc(), "cron:job", killing, who="Reaper")
        assert failure == "runtime still leased by another tenant", (
            "a lease on this pid held by a DIFFERENT process's teardown still refuses; "
            f"got {failure!r}"
        )
        assert rr._leases_on_pid(9595) == 1, "the sibling's lease was not this teardown's to give"
    finally:
        await by_pid.give_back()
        await by_identity.give_back()


@pytest.mark.asyncio
async def test_the_reset_ladder_kills_when_the_only_lease_is_its_own_subject() -> None:
    """MUTATION TARGET: the ladder's own subject-lease release, matched on handle.

    Same rule as the cron path, and reachable for the same reason: the graceful
    reset that hung is the one holding the lease, because a reset releases it
    inside ``provider.shutdown()``. A ladder that asked the gate first would let
    the sub-agent session refuse the kill of the very runtime it wedged.
    """
    from unittest.mock import MagicMock, patch

    from kiro_crew.process_identity import ProcessHandle
    from kiro_crew.subagent import SubagentManager

    ro._reset_for_tests()
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    handle = ProcessHandle(pid=7373, start_id="start-identity", pgid=7373, child_pids={})
    holder = _LeaseHolder(7373)
    await holder.take()
    entry = type(
        "_Entry",
        (),
        {
            "session": type("_S", (), {"provider": _TeardownProvider(holder.lease)})(),
            "handle": handle,
        },
    )()
    manager._sessions.tearing_down = lambda key: [entry]

    with patch("kiro_crew.subagent.kill_verified_process", return_value=None) as mock_verified_kill:
        failure = await manager._sigkill_session("hung", handle)

    mock_verified_kill.assert_called_once()
    assert failure is None, f"the subject's own lease must not withhold the kill; got {failure!r}"


@pytest.mark.asyncio
async def test_the_sweep_gate_audits_both_outcomes() -> None:
    """MUTATION TARGET: the SEL audit on the cleanup sweep's gate decisions.

    The ownership gate writes a log line and nothing else, so an allow that is not
    audited leaves a signalled process with no record of who decided it, and a
    refusal that is not audited leaves an operator reading a non-zero leak count
    with nothing saying why nothing was done.
    """
    from unittest.mock import patch

    ro._reset_for_tests()
    service, recorded = _cleanup(candidates=[6161, 6262])
    events: list[tuple[int, str]] = []

    holder = _LeaseHolder(6161)
    await holder.take()
    try:
        with patch(
            "kiro_crew.session_cleanup.audit_kill_decision",
            lambda pid, outcome, reason, *, tool_name: events.append((pid, outcome)),
        ):
            await service._sweep_periodic_pids()
    finally:
        await holder.give_back()

    assert recorded["pid_kills"] == [6262], "the leased pid is withheld"
    assert (6161, "refused") in events, f"the refusal is audited; got {events}"
    # ``allowed``, never ``killed``: the kill phase that follows this decision
    # re-judges every candidate against the file as it reads then and prunes rather
    # than kills, so a ``killed`` row written here would claim kills of processes
    # still running. The phase emits its own row once the signal's result is known.
    assert (6262, "allowed") in events, f"the allow is audited as a decision; got {events}"
    assert (6262, "killed") not in events, f"no kill is claimed at the decision point; {events}"


def test_the_shared_audit_emitter_reaches_sel_and_cannot_break_a_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POSITIVE CONTROL for the shared emitter, plus its fail-open rule."""
    logged: list[dict[str, Any]] = []

    class _Sel:
        def log_tool_invocation(self, **kwargs: Any) -> None:
            logged.append(kwargs)

    from kiro_crew import process_identity

    monkeypatch.setattr("kiro_crew.sel.sel", lambda: _Sel())
    process_identity.audit_kill_decision(4242, "refused", "because", tool_name="a-caller")
    assert logged and logged[0]["tool_name"] == "a-caller"
    assert logged[0]["tool_kind"] == "process_kill"
    assert logged[0]["outcome"] == "refused"
    assert "4242" in logged[0]["resources"]

    def boom() -> Any:
        raise RuntimeError("sel is down")

    monkeypatch.setattr("kiro_crew.sel.sel", boom)
    process_identity.audit_kill_decision(4242, "killed", "because", tool_name="a-caller")


# ── the cron reaper ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_cron_reaper_reports_a_leased_runtime_instead_of_killing_it() -> None:
    """MUTATION TARGET: the gate in ``CronService._sigkill_session``.

    A cron runtime is not a chat-pool tenant, but the sub-agents a cron turn
    dispatched run on it and take their own leases -- and this path fires when the
    graceful reset hung, which is exactly when one of them is still working.

    The refusal is RETURNED, not swallowed, so the run is never recorded as reaped
    over a process tree that is still standing.
    """
    from kiro_crew.cron import CronService
    from kiro_crew.process_identity import ProcessHandle

    ro._reset_for_tests()
    handle = ProcessHandle(pid=9191, start_id="abc", pgid=9191, child_pids={})

    class _Svc:
        _sessions = type("_S", (), {"tearing_down": lambda self, key: []})()

    holder = _LeaseHolder(9191)
    await holder.take()
    try:
        failure = await CronService._sigkill_session(_Svc(), "cron:job", handle, who="Reaper")
    finally:
        await holder.give_back()
    assert failure is not None and "leased" in failure


# ── the teardown whose scope already unwound ──────────────────────────────────


@pytest.mark.asyncio
async def test_a_cancelled_resets_own_lease_is_still_released_from_the_captured_pop() -> None:
    """MUTATION TARGET: the captured pop as a second source for the subject's lease.

    The teardown scope is a context manager, and its release drops the entry on
    "return, raise or cancellation alike". So a reset the caller abandoned on its
    timeout has already unwound the scope by the time the last-resort kill runs,
    leaving ``tearing_down`` EMPTY -- while the lease is still held, because
    ``provider.shutdown()`` is the only releaser and it sits past the awaits the
    cancellation interrupted.

    That is the exact case the last-resort kill exists for, so a release that reads
    ``tearing_down`` alone leaves the subject refusing its own kill in the one
    situation it was written to cover: the wedged process survives and its pid is
    then refused by every sweep for the gateway's life. The caller's captured pop is
    what no unwinding can take away.
    """
    from unittest.mock import patch

    from kiro_crew.cron import CronService
    from kiro_crew.process_identity import ProcessHandle

    ro._reset_for_tests()
    handle = ProcessHandle(pid=9494, start_id="abc", pgid=9494, child_pids={})
    holder = _LeaseHolder(9494)
    await holder.take()
    assert rr._leases_on_pid(9494) == 1, "precondition: the subject holds the only lease"

    provider = _TeardownProvider(holder.lease)
    popped_session = type("_S", (), {"provider": provider})()

    class _Sessions:
        def tearing_down(self, key: str) -> list[object]:
            # The scope unwound on the cancellation, so the table names nothing.
            return []

    class _Svc:
        _sessions = _Sessions()

    killed: list[int] = []

    async def fake_kill(h: Any, *, who: str, key: str, child_helpers: Any) -> None:
        killed.append(h.pid)
        return None

    with (
        patch("kiro_crew.cron.kill_verified_process", fake_kill),
        patch("kiro_crew.session.child_process_helpers", lambda: (None, None, None)),
    ):
        failure = await CronService._sigkill_session(
            _Svc(), "cron:job", handle, who="Reaper", popped=[(popped_session, handle)]
        )

    assert killed == [9494], (
        "a reset cancelled before provider.shutdown() must not have its own lease "
        f"withhold the last-resort kill; failure={failure!r}"
    )
    assert failure is None


@pytest.mark.asyncio
async def test_the_captured_pop_does_not_release_another_tenants_lease() -> None:
    """The other side of the same rule: the pop is matched on pid AND start identity.

    Without this, forwarding the pop could become "release whatever the caller
    happens to be holding", which would strip a co-tenant's lease and turn the gate
    into an always-kill. Here the popped session's handle names a DIFFERENT process
    start on the same number, so its lease is not this teardown's to release.
    """
    from kiro_crew.cron import CronService
    from kiro_crew.process_identity import ProcessHandle

    ro._reset_for_tests()
    handle = ProcessHandle(
        pid=9595, start_id="the-process-under-the-knife", pgid=9595, child_pids={}
    )
    sibling = ProcessHandle(pid=9595, start_id="a-different-start", pgid=9595, child_pids={})
    holder = _LeaseHolder(9595)
    await holder.take()

    provider = _TeardownProvider(holder.lease)
    other_session = type("_S", (), {"provider": provider})()

    class _Sessions:
        def tearing_down(self, key: str) -> list[object]:
            return []

    class _Svc:
        _sessions = _Sessions()

    try:
        failure = await CronService._sigkill_session(
            _Svc(), "cron:job", handle, who="Reaper", popped=[(other_session, sibling)]
        )
    finally:
        await holder.give_back()

    assert failure == "runtime still leased by another tenant", failure


@pytest.mark.asyncio
async def test_a_provider_reached_through_both_sources_is_released_once() -> None:
    """MUTATION TARGET: the identity dedupe across the two sources.

    The common case reaches the same provider through the torn-down table AND the
    caller's pop. Releasing twice is not harmful to the lease table, but the release
    is counted and logged per teardown, so a duplicate would report two teardowns
    where there was one.
    """
    from kiro_crew.process_identity import ProcessHandle, release_teardown_lease

    handle = ProcessHandle(pid=9696, start_id="abc", pgid=9696, child_pids={})
    releases: list[object] = []

    class _Provider:
        pass

    provider = _Provider()
    session = type("_S", (), {"provider": provider})()
    entry = type("_Entry", (), {"session": session, "handle": handle})()

    class _Sessions:
        def tearing_down(self, key: str) -> list[object]:
            return [entry]

    import kiro_crew.process_identity as pi

    real = pi.release_session_lease
    try:

        async def _recording(p: object) -> None:
            releases.append(p)

        pi.release_session_lease = _recording  # type: ignore[assignment]
        await release_teardown_lease(
            _Sessions(), "cron:job", handle, who="Reaper", popped=[(session, handle)]
        )
    finally:
        pi.release_session_lease = real  # type: ignore[assignment]

    assert releases == [provider], f"one release for one teardown; {releases}"


# ── the kill phase owns the killed row ────────────────────────────────────────


def test_the_kill_phase_reports_what_it_actually_killed() -> None:
    """MUTATION TARGET: the phase-level audit row.

    A decision row says a signal was permitted; only the phase knows whether one
    happened. The phase re-judges every candidate against the file as it reads then
    -- "prune, never kill" -- so a phase handed candidates and killing none is a real
    shape an operator needs when a leak reading will not come down.
    """
    from kiro_crew.process_identity import audit_kill_phase

    rows: list[tuple[str, str]] = []

    import kiro_crew.process_identity as pi

    real = pi._audit_kill
    try:
        pi._audit_kill = (  # type: ignore[assignment]
            lambda outcome, reason, resources, *, tool_name: rows.append((outcome, resources))
        )
        audit_kill_phase(allowed=3, killed=2, reason="r", tool_name="t")
        audit_kill_phase(allowed=3, killed=0, reason="r", tool_name="t")
        audit_kill_phase(allowed=0, killed=0, reason="r", tool_name="t")
    finally:
        pi._audit_kill = real  # type: ignore[assignment]

    assert rows == [
        ("killed", "allowed=3 killed=2"),
        ("failed", "allowed=3 killed=0"),
    ], f"a phase that permitted nothing has nothing to report; {rows}"


@pytest.mark.asyncio
async def test_the_periodic_sweep_emits_one_phase_row_after_its_kill() -> None:
    """The wiring for the row above: without it the emitter exists and nothing calls it."""
    from unittest.mock import patch

    ro._reset_for_tests()
    service, recorded = _cleanup(candidates=[7901, 7902])
    phases: list[tuple[int, int]] = []

    with patch(
        "kiro_crew.session_cleanup.audit_kill_phase",
        lambda *, allowed, killed, reason, tool_name: phases.append((allowed, killed)),
    ):
        await service._sweep_periodic_pids()

    assert recorded["pid_kills"] == [7901, 7902]
    assert phases == [(2, 2)], f"one row, naming both counts; {phases}"


# ── the handler that reads a name the lock's failure never bound ──────────────


def test_a_lock_that_fails_to_open_is_reported_and_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """MUTATION TARGET: resolving the path BEFORE the try.

    The lock's own ``__enter__`` does a mkdir and opens the lock file, so it can
    raise OSError -- and a handler whose message reads a name assigned INSIDE the
    ``with`` body then raises UnboundLocalError out of itself, losing this warning
    and the caller's too, leaving one uninformative debug line behind.
    """
    from contextlib import contextmanager

    from kiro_crew import session_pid

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(session_pid, "config_dir", lambda: home)

    @contextmanager
    def _failing_lock():  # type: ignore[no-untyped-def]
        raise OSError("the lock file could not be opened")
        yield  # pragma: no cover

    monkeypatch.setattr(session_pid, "_pid_file_lock", _failing_lock)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.runtime_reconcile"):
        rr._retract_descendant_rows(["8001:4242:TOK"])  # must not raise

    messages = [r.getMessage() for r in caplog.records if "could not retract" in r.getMessage()]
    assert messages, "the failure is reported"
    assert "kiro_pids.txt" in messages[0], f"and the message names the file; {messages[0]}"
