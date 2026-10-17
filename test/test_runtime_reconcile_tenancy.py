"""Tests for the tenancy table, the identity pinned across a kill, and the barrier on
every gate-then-kill path.

Every tenancy claim the reconciler tests make is in this module, so the leak scan
over this module's own source sees all of them, and the positive control at its end
reads the table those claims act on.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from runtime_reconcile_helpers import agent_root_fixture  # noqa: F401
from runtime_reconcile_helpers import (
    _CLEARED_A,
    _CLEARED_B,
    _EARLY,
    _LATE,
    _TENANTED,
    _UNCLAIMED,
    _cleanup,
    _reconciler,
)

from kiro_crew import runtime_ownership as ro
from kiro_crew import runtime_reconcile as rr

# The rootdir conftest wipes runtime_ownership's tables on both sides of every
# test. This file does its own intra-file isolation with explicit
# ``ro._reset_for_tests()`` calls, and its positive control at the end reads the
# table to prove the earlier tests left it clean -- a wipe at ANY test's teardown
# would empty it first and make that read vacuous. Module-wide, not per test,
# because the predecessor's teardown is the wipe that matters.
pytestmark = pytest.mark.keep_runtime_ownership_tables


# ── tenancy: the table that speaks for a process nobody owns ──────────────────


@pytest.mark.asyncio
async def test_the_scope_reaper_does_not_stop_a_unit_holding_a_tenanted_pid(
    tmp_path: Path,
) -> None:
    """MUTATION TARGET: the pre-stop survey reads TENANCIES, not leases alone.

    A lease says a session may end the runtime. A tenancy says somebody who does
    not own the process is mid-flight on it -- and it SURVIVES the last lease
    release, which is exactly the moment a co-tenant becomes undefended. At
    ``cap=1`` an acquisition cannot join an occupied runtime, so a session-sharing
    sub-agent holds no lease at all and the tenancy table is the only thing that
    represents it.

    Reading leases alone therefore called a scope holding a live shared turn
    abandoned, and stopping the unit SIGKILLs the whole cgroup. A destroyed turn is
    not retried.
    """
    from unittest.mock import patch

    from kiro_crew import session_scope_reap as reap

    ro._reset_for_tests()
    scope = tmp_path / "run-tenant.scope"
    scope.mkdir()
    # The tenanted pid is SECOND on purpose. The barrier below the survey also
    # refuses for a live tenant, so with the tenanted pid first the two guards
    # reject the same input and neither pins the other. Ordered this way, the
    # survey's own answer is observable: it refuses before ANY barrier is taken,
    # where the barrier path would have committed and released the first member.
    (scope / "cgroup.procs").write_text(f"{_UNCLAIMED}\n{_TENANTED}\n", encoding="utf-8")

    stopped: list[str] = []
    signalled: list[tuple[int, int]] = []
    refusals: list[str] = []
    committed: list[int] = []

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

    # A tenancy and no lease -- the state the mechanism exists for.
    claim = ro.claim_runtime_tenancy(_TENANTED, holder="a shared sub-agent turn")
    assert claim is not None, "precondition: the tenancy was taken"
    assert ro.outstanding_leases(_TENANTED) == 0, "precondition: and no lease is held"
    real_commit = reap.commit_runtime_teardown
    try:
        monkey = patch.object(
            reap,
            "commit_runtime_teardown",
            lambda pid, epoch: (committed.append(pid), real_commit(pid, epoch))[1],
        )
        monkey.start()
        cleared = reap._reclaim_scope(
            scope,
            "run-tenant.scope",
            proc_root=tmp_path,
            stop_unit=lambda unit: stopped.append(unit) or True,
            signal_owned=signal_owned,
            sleep=lambda secs: None,
            on_refusal=refusals.append,
        )
    finally:
        monkey.stop()
        ro.release_runtime_tenancy(claim)

    assert stopped == [], "the unit is never stopped while a member carries a tenancy"
    assert signalled == [], "and its unclaimed neighbour is not signalled either"
    assert cleared is False
    assert refusals == ["still leased"]
    assert committed == [], (
        "the survey refuses before any barrier is taken, so an obviously unreclaimable "
        f"scope never disturbs the tenancy table; {committed}"
    )


@pytest.mark.asyncio
async def test_a_tenant_arriving_after_the_survey_abandons_the_whole_reclaim(
    tmp_path: Path,
) -> None:
    """MUTATION TARGET: the committed barriers between the survey and ``stop_unit``.

    The survey is a statement about the past: it runs on a maintenance thread while
    a shared turn takes its tenancy on the event loop. The barrier is what makes the
    verdict current -- a member that gained a tenant in between fails its own commit
    -- and a failed commit must abandon the WHOLE reclaim, because the stop takes
    the entire cgroup.
    """
    from unittest.mock import patch

    from kiro_crew import session_scope_reap as reap

    ro._reset_for_tests()
    scope = tmp_path / "run-late.scope"
    scope.mkdir()
    (scope / "cgroup.procs").write_text(f"{_EARLY}\n{_LATE}\n", encoding="utf-8")

    stopped: list[str] = []
    refusals: list[str] = []
    released: list[int] = []
    real_release = reap.release_runtime_teardown

    def _late_tenant(pid: int, epoch: int) -> bool:
        # The second member is claimed between the survey and its own commit.
        return pid != _LATE

    with (
        patch.object(reap, "commit_runtime_teardown", _late_tenant),
        patch.object(
            reap,
            "release_runtime_teardown",
            lambda pid: (released.append(pid), real_release(pid))[1],
        ),
    ):
        cleared = reap._reclaim_scope(
            scope,
            "run-late.scope",
            proc_root=tmp_path,
            stop_unit=lambda unit: stopped.append(unit) or True,
            signal_owned=lambda *a: (True, ""),
            sleep=lambda secs: None,
            on_refusal=refusals.append,
        )

    assert stopped == [], "one member gaining a tenant abandons the whole reclaim"
    assert cleared is False
    assert refusals == ["still leased"]
    assert released == [_EARLY], (
        "and the barrier already taken is released, or that pid can never be claimed "
        f"again; {released}"
    )


def test_the_scope_reaper_releases_every_barrier_after_a_successful_stop(
    tmp_path: Path,
) -> None:
    """The other half of the barrier rule: a barrier left standing is permanent.

    A committed pid refuses every tenancy claim for the life of the gateway, so the
    success path must release exactly what it took.
    """
    from unittest.mock import patch

    from kiro_crew import session_scope_reap as reap

    ro._reset_for_tests()
    scope = tmp_path / "run-clear.scope"
    scope.mkdir()
    procs = scope / "cgroup.procs"
    procs.write_text(f"{_CLEARED_A}\n{_CLEARED_B}\n", encoding="utf-8")

    released: list[int] = []
    real_release = reap.release_runtime_teardown

    def _stop(unit: str) -> bool:
        procs.write_text("", encoding="utf-8")  # the cgroup is empty after the stop
        return True

    with patch.object(
        reap,
        "release_runtime_teardown",
        lambda pid: (released.append(pid), real_release(pid))[1],
    ):
        cleared = reap._reclaim_scope(
            scope,
            "run-clear.scope",
            proc_root=tmp_path,
            stop_unit=_stop,
            signal_owned=lambda *a: (True, ""),
            sleep=lambda secs: None,
        )

    assert cleared is True
    expected = [_CLEARED_A, _CLEARED_B]
    assert sorted(released) == expected, f"every barrier taken is dropped; {released}"
    # Released in a finally: the table is a process-wide singleton, and an int target
    # has no liveness probe, so a discarded handle stays live for the worker's whole
    # life and refuses every later barrier on this pid -- in this file and in others.
    proof = ro.claim_runtime_tenancy(_CLEARED_A, holder="a later turn")
    try:
        assert proof is not None, "and the pid can be claimed again afterwards"
    finally:
        ro.release_runtime_tenancy(proof)


def test_a_tenant_claiming_after_the_gate_abandons_the_reconcilers_kill() -> None:
    """MUTATION TARGET: the reconciler commits a teardown barrier before signalling.

    The gate's verdict and the signal are separated by the kill seam's own
    descendant walk and its per-child marker reads, on a thread that does not own
    the tenancy table. A claim landing in that window is invisible to the verdict,
    so the epoch is read right after the allow and committed immediately before the
    signal -- the barrier the sibling kill path already takes.

    The population this matters for is precisely a sub-agent's: an orphaned shared
    runtime with no lease and no record is what this reconciler kills, and what a
    shared turn claims a tenancy on.
    """
    killed: list[int] = []
    released: list[int] = []
    reconciler = _reconciler(
        kernel={9001},
        recorded=set(),
        alive={9001},
        ours={9001},
        killed=killed,
    )
    # Two passes: the first only confirms, the second would kill.
    reconciler.run_once()
    reconciler._commit_teardown = lambda pid, epoch: False
    reconciler._release_teardown = released.append
    reading = reconciler.run_once()

    assert killed == [], "a claim after the allow abandons the kill, not narrows it"
    assert reading.killed == 0, reading.as_counter_fields()
    assert released == [], (
        "and no barrier is released for a commit that never took one; " f"{released}"
    )


def test_the_reconciler_releases_its_barrier_even_when_the_kill_raises() -> None:
    """The barrier's `finally`: a pid left committed is one no tenant can ever claim.

    Without this, the release could sit on the success path only, and a kill seam
    that raises would strand the barrier for the gateway's life.
    """
    released: list[int] = []

    def _raises(pid: int, expected: str | None = None) -> int:
        raise OSError("killpg refused")

    reconciler = _reconciler(
        kernel={9002},
        recorded=set(),
        alive={9002},
        ours={9002},
    )
    reconciler._kill_tree = _raises
    reconciler._release_teardown = released.append
    reconciler.run_once()
    reading = reconciler.run_once()

    assert reading.killed == 0
    assert released == [9002], f"the barrier is dropped on the raising path too; {released}"


def test_the_reconcilers_barrier_seams_are_the_real_tenancy_table() -> None:
    """POSITIVE CONTROL: without this, defaults that answered True for everything
    would satisfy both tests above and mean nothing."""
    ro._reset_for_tests()
    reconciler = rr.build_reconciler(active_pids=lambda: set(), notify_dead=lambda pid: None)

    pid = os.getpid()
    epoch = reconciler._epoch_of(pid)
    claim = ro.claim_runtime_tenancy(pid, holder="a shared turn")
    try:
        assert reconciler._epoch_of(pid) == epoch + 1, "a claim advances the epoch"
        assert (
            reconciler._commit_teardown(pid, epoch) is False
        ), "a stale epoch is refused by the real table"
    finally:
        ro.release_runtime_tenancy(claim)
        reconciler._release_teardown(pid)

    fresh = reconciler._epoch_of(pid)
    assert reconciler._commit_teardown(pid, fresh) is True, "and a current one is granted"
    reconciler._release_teardown(pid)


def test_a_pid_holding_only_a_tenancy_is_never_counted_unowned() -> None:
    """MUTATION TARGET: the unowned shield reads BOTH tables, not leases alone.

    The gate asks two questions, so a shield that asks only the first is not the
    gate's shield. A session-sharing sub-agent's turn and a Connect OAuth mint child
    hold a TENANCY and no lease -- at ``cap=1`` an acquisition cannot join an
    occupied runtime, so there is no lease for them to hold.

    The gate refuses their kill either way. What a lease-only shield breaks is the
    READING: every in-flight shared turn is published as a leak in the number an
    operator and the SLI both act on, and each one drags a gate refusal and an audit
    row behind it on every pass, forever.
    """
    ro._reset_for_tests()
    pid = 424242
    killed: list[int] = []

    def build() -> rr.RuntimeReconciler:
        return rr.RuntimeReconciler(
            slice_pids=lambda: {pid},
            recorded_pids=lambda: set(),
            is_alive=lambda p: True,
            identity_of=lambda p: "id",
            is_ours=lambda p: True,
            is_managed=lambda p: True,  # host-independent: this pid is invented
            kill_tree=lambda p, expected=None: (killed.append(p), 1)[1],
            forget=lambda p: "not-mine",
            notify_dead=lambda p: None,
            age_secs=lambda p: 10_000.0,
        )

    held = build()
    claim = ro.claim_runtime_tenancy(pid, holder="a shared sub-agent turn")
    assert claim is not None, "precondition: the tenancy was taken"
    assert ro.outstanding_leases(pid) == 0, "precondition: and no lease is held"
    try:
        held.run_once()
        reading = held.run_once()
    finally:
        ro.release_runtime_tenancy(claim)

    assert reading.unowned_alive == 0, (
        "a pid carrying only a tenancy is CLAIMED, so it never enters the reading; "
        f"{reading.as_counter_fields()}"
    )
    assert killed == [], "and it is never a candidate at all"

    # CONTROL: once the turn ends and the claim goes, the same pid is unowned and
    # killable. Without this the assertion above would pass for a shield that simply
    # never counted anything.
    freed = build()
    freed.run_once()
    after = freed.run_once()
    assert after.unowned_alive == 1, after.as_counter_fields()
    assert killed == [pid], f"the released pid is reclaimed normally; {killed}"


def test_an_unreadable_tenancy_table_means_claimed() -> None:
    """The fail-closed half: this list decides a kill, so an unknown is a claim."""
    ro._reset_for_tests()

    def _boom(pid: int) -> int:
        raise RuntimeError("the tenancy table could not be read")

    rec = _reconciler(kernel={424243}, recorded=set())
    rec._claims_on = _boom
    rec.run_once()
    reading = rec.run_once()

    assert reading.unowned_alive == 0, reading.as_counter_fields()
    assert reading.killed == 0, "an unreadable table never authorizes a signal"


# ── the identity pinned ACROSS the walk, not only up to its door ──────────────


def test_an_identity_that_changes_during_the_descendant_walk_spares_the_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTATION TARGET: the identity re-read immediately before the ROOT signal.

    A caller verifies the pid and then hands it to the kill seam, whose descendant
    walk is a ``pgrep`` -- unbounded in time. A candidate that exits inside that walk
    frees its number, and a managed runtime taking the number next passes the argv
    gate, because it genuinely is one of ours. The root signal then lands on an
    unrelated live session, and there is no recovery: the victim learns only from the
    dead direction's notice, and its turn is gone.

    Checking at the door is not enough, which is the rule the sibling teardown states
    for itself: before EVERY signal, not once up front.
    """
    from kiro_crew import platform_compat, session_pid

    live = {"token": "THE-PROCESS-WE-VERIFIED"}
    signalled: list[int] = []

    def _signal(pid: int, sig: int) -> None:
        signalled.append(pid)
        # The number changes hands while the child loop runs: the list WAS ours, so
        # its members are legitimately signalled, and only the root is spared.
        live["token"] = "A-REPLACEMENT-RUNTIME"

    monkeypatch.setattr("kiro_crew.acp.client._get_child_pids", lambda pid: [5511])
    monkeypatch.setattr(session_pid, "_is_managed_agent_process", lambda pid: True)
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: live["token"])
    # The child really is our descendant in this scenario, so the ancestry edge the
    # capture validates has to be faked too -- otherwise the capture rejects an
    # invented pid and this test passes for the wrong reason.
    monkeypatch.setattr(platform_compat, "get_ppid", lambda pid: 5510 if pid == 5511 else 1)
    monkeypatch.setattr(platform_compat, "kill_pid", _signal)

    total, root_killed = session_pid._kill_pid_tree(5510, expected_start="THE-PROCESS-WE-VERIFIED")

    assert root_killed is False, "the root is not signalled once its identity has moved"
    assert 5510 not in signalled, f"and no signal reached the number itself; {signalled}"
    assert signalled == [5511], "the children found while it was still ours are killed"
    assert total == 1


def test_a_pinned_identity_that_differs_at_the_door_signals_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first of the two checks: a pid that already moved is not walked at all."""
    from kiro_crew import platform_compat, session_pid

    signalled: list[int] = []
    monkeypatch.setattr(
        "kiro_crew.acp.client._get_child_pids",
        lambda pid: pytest.fail("a pid that already moved must not even be walked"),
    )
    monkeypatch.setattr(session_pid, "_is_managed_agent_process", lambda pid: True)
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: "SOMEBODY-ELSE")
    monkeypatch.setattr(platform_compat, "kill_pid", lambda pid, sig: signalled.append(pid))

    assert session_pid._kill_pid_tree(5520, expected_start="OURS") == (0, False)
    assert signalled == []


def test_an_unreadable_identity_withholds_a_pinned_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed: this comparison authorizes a SIGNAL, so an unknown withholds it.

    Elsewhere in the module an unreadable token is never a mismatch, because there it
    decides whether to RETRACT a record. Here it decides whether to kill, and the two
    directions cannot share an answer.
    """
    from kiro_crew import platform_compat, session_pid

    signalled: list[int] = []
    monkeypatch.setattr("kiro_crew.acp.client._get_child_pids", lambda pid: [])
    monkeypatch.setattr(session_pid, "_is_managed_agent_process", lambda pid: True)
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: None)
    monkeypatch.setattr(platform_compat, "kill_pid", lambda pid, sig: signalled.append(pid))

    assert session_pid._kill_pid_tree(5530, expected_start="OURS") == (0, False)
    assert signalled == []


def test_an_unpinned_kill_behaves_exactly_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONTROL: every existing caller passes no identity and must be unaffected.

    Without this, the pin could have become a fail-closed default that silently
    stopped four older sweeps from killing anything.
    """
    from kiro_crew import platform_compat, session_pid

    signalled: list[int] = []
    monkeypatch.setattr("kiro_crew.acp.client._get_child_pids", lambda pid: [5541])
    monkeypatch.setattr(session_pid, "_is_managed_agent_process", lambda pid: True)
    # Unreadable on purpose: a caller with no captured identity has nothing to
    # compare, so this must not be read as a mismatch.
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: None)
    monkeypatch.setattr(platform_compat, "kill_pid", lambda pid, sig: signalled.append(pid))
    # The root goes through a DIFFERENT primitive on Windows -- taskkill /T, because
    # the per-child loop is empty there -- so patching only the pid-scoped one leaves
    # the real call to run against an invented number and report a failed kill.
    monkeypatch.setattr(
        platform_compat, "kill_process_tree", lambda pid, sig: signalled.append(pid)
    )

    total, root_killed = session_pid._kill_pid_tree(5540)

    assert root_killed is True, "an unpinned kill still signals its root"
    assert sorted(signalled) == [5540, 5541], (
        "the root and the child the fixture supplies are both signalled, whichever "
        f"root primitive this platform uses; {signalled}"
    )
    assert total == 2


def test_the_reconciler_hands_the_captured_identity_to_the_kill_seam() -> None:
    """The wiring: without it the seam grows a pin nothing ever passes.

    The identity handed down is the one the pass CLASSIFIED the candidate with, so
    the seam compares against the same process the two-pass confirmation, the marker
    read and the age floor all agreed about.
    """
    seen: list[tuple[int, str | None]] = []
    rec = _reconciler(kernel={5550}, recorded=set())
    rec._kill_tree = lambda pid, expected=None: (seen.append((pid, expected)), 1)[1]

    rec.run_once()
    rec.run_once()

    assert seen == [(5550, "id-5550")], (
        "the seam is called with the identity this pass captured, not with the pid "
        f"alone; {seen}"
    )


def test_an_identity_that_changes_while_descendants_are_read_spares_the_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTATION TARGET: the identity re-read AFTER the descendant read, before the child loop.

    The read itself is a window -- one procfs children read on Linux, a ``pgrep``
    spawn on macOS -- and the children are signalled under the argv gate alone, with
    no identity pin of their own. So a pid recycled during that read hands over
    somebody else's child list, and those children get SIGKILL before anything
    re-checks the root. The pre-root-signal check cannot help: the child signals are
    already out, and nothing recalls a signal.

    Distinct from its sibling by WHEN the number changes hands: there, after the list
    was already ours, so the children found while it was ours are still killed. Here,
    during the read, so the list itself belongs to a stranger and nothing is killed.
    """
    from kiro_crew import platform_compat, session_pid

    live = {"token": "OURS"}
    signalled: list[int] = []

    def _children(pid: int) -> list[int]:
        # The number changes hands DURING the read, so this list is a stranger's.
        live["token"] = "A-REPLACEMENT-RUNTIME"
        return [5611, 5612]

    monkeypatch.setattr("kiro_crew.acp.client._get_child_pids", _children)
    monkeypatch.setattr(session_pid, "_is_managed_agent_process", lambda pid: True)
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: live["token"])
    # Both children ARE under the root, so the capture's ancestry guard admits them and
    # the post-read root check is the only thing that can spare them. Without this the
    # two guards reject the same input and neither pins the other.
    monkeypatch.setattr(platform_compat, "get_ppid", lambda pid: 5610 if pid in (5611, 5612) else 1)
    monkeypatch.setattr(platform_compat, "kill_pid", lambda pid, sig: signalled.append(pid))
    monkeypatch.setattr(
        platform_compat, "kill_process_tree", lambda pid, sig: signalled.append(pid)
    )

    total, root_killed = session_pid._kill_pid_tree(5610, expected_start="OURS")

    assert signalled == [], (
        "a child list read after the number changed hands belongs to somebody else's "
        f"runtime; nothing in it may be signalled; {signalled}"
    )
    assert (total, root_killed) == (0, False)


def test_a_child_whose_identity_changes_before_its_signal_is_spared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTATION TARGET: each CHILD is pinned to its own discovery identity.

    The child loop signals under the argv gate alone, and that gate answers from
    cmdline: it says a pid is the KIND of process we manage, never that it is the one
    just discovered. A child that exits between the descendant read and its own signal
    frees its number, and another managed runtime taking it satisfies the argv gate
    exactly -- so the SIGKILL lands on an unrelated live process.

    The root's three checks cannot cover this: they are about the root's number.
    """
    from kiro_crew import platform_compat, session_pid

    tokens = {5710: "ROOT", 5711: "CHILD-ONE", 5712: "CHILD-TWO"}
    signalled: list[int] = []

    def _signal(cpid: int, sig: int) -> None:
        signalled.append(cpid)
        # 5712 is signalled first (the loop runs in reverse), and its number changes
        # hands right after -- which is 5711's moment, not the root's.
        tokens[5711] = "A-DIFFERENT-PROCESS"

    monkeypatch.setattr("kiro_crew.acp.client._get_child_pids", lambda pid: [5711, 5712])
    monkeypatch.setattr(session_pid, "_is_managed_agent_process", lambda pid: True)
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: tokens.get(pid))
    # Both children are genuinely under the root here; the capture's ancestry check
    # is a separate guard with its own test below.
    monkeypatch.setattr(platform_compat, "get_ppid", lambda pid: 5710 if pid in (5711, 5712) else 1)
    monkeypatch.setattr(platform_compat, "kill_pid", _signal)
    monkeypatch.setattr(
        platform_compat, "kill_process_tree", lambda pid, sig: signalled.append(pid)
    )

    total, root_killed = session_pid._kill_pid_tree(5710, expected_start="ROOT")

    assert 5711 not in signalled, (
        "a child whose number changed hands after discovery must not be signalled; " f"{signalled}"
    )
    assert signalled == [5712, 5710], f"its sibling and the root still are; {signalled}"
    assert (total, root_killed) == (2, True)


def test_a_child_with_no_readable_identity_is_spared_when_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed per child: this comparison authorizes a signal, so unknown withholds."""
    from kiro_crew import platform_compat, session_pid

    signalled: list[int] = []
    monkeypatch.setattr("kiro_crew.acp.client._get_child_pids", lambda pid: [5721])
    monkeypatch.setattr(session_pid, "_is_managed_agent_process", lambda pid: True)
    # The root reads, the child does not.
    monkeypatch.setattr(
        platform_compat, "get_process_start_id", lambda pid: "ROOT" if pid == 5720 else None
    )
    # Ancestry holds; the identity is the only thing missing, which is what this pins.
    monkeypatch.setattr(platform_compat, "get_ppid", lambda pid: 5720 if pid == 5721 else 1)
    monkeypatch.setattr(platform_compat, "kill_pid", lambda pid, sig: signalled.append(pid))
    monkeypatch.setattr(
        platform_compat, "kill_process_tree", lambda pid, sig: signalled.append(pid)
    )

    total, root_killed = session_pid._kill_pid_tree(5720, expected_start="ROOT")

    assert signalled == [5720], f"the unreadable child is withheld, the root is not; {signalled}"
    assert (total, root_killed) == (1, True)


# ── the barrier on every gate-then-kill path ──────────────────────────────────


@pytest.mark.asyncio
async def test_the_periodic_sweep_does_not_hand_a_late_tenants_pid_to_the_kill_phase() -> None:
    """MUTATION TARGET: the teardown barrier around the sweep's kill phase.

    The gate's verdict and the signal are separated by a thread hop, a pid-file read,
    token reads and a descendant walk. A shared turn claiming a tenancy in there is
    invisible to BOTH of the kill phase's own re-checks: a start token is unchanged by
    a new tenant on the same process, and the live-pid shield was read into a set
    before the claimant registered its own.
    """
    from unittest.mock import patch

    ro._reset_for_tests()
    service, recorded = _cleanup(candidates=[5801, 5802])

    real_commit = ro.commit_runtime_teardown

    def _late_tenant_on_5801(pid: int, epoch: int) -> bool:
        return pid != 5801 and real_commit(pid, epoch)

    with patch("kiro_crew.process_identity.commit_runtime_teardown", _late_tenant_on_5801):
        await service._sweep_periodic_pids()

    assert recorded["pid_kills"] == [5802], (
        "the pid that gained a tenant after the gate is left for the next tick; "
        f"{recorded['pid_kills']}"
    )


@pytest.mark.asyncio
async def test_the_periodic_sweep_releases_every_barrier_it_took() -> None:
    """The other half: a barrier left standing refuses that pid's tenancies forever."""
    ro._reset_for_tests()
    service, recorded = _cleanup(candidates=[5811])

    await service._sweep_periodic_pids()

    assert recorded["pid_kills"] == [5811]
    # Released in a finally, for the reason above: a claim on an int target never reads
    # as dead, so a discarded handle is a permanent refusal on this pid.
    proof = ro.claim_runtime_tenancy(5811, holder="a later turn")
    try:
        assert (
            proof is not None
        ), "the pid can be claimed again after the sweep, so its barrier was released"
    finally:
        ro.release_runtime_tenancy(proof)


@pytest.mark.asyncio
async def test_the_cron_reaper_reports_a_tenant_that_claimed_after_the_gate() -> None:
    """MUTATION TARGET: the barrier on the cron teardown's own kill.

    The verified kill re-reads the start id, resolves the group and walks the
    descendants before its first signal. A tenant claiming in there is reported as the
    thing that stopped the kill, so the run is never recorded as reaped over a live turn.
    """
    from unittest.mock import patch

    from kiro_crew.cron import CronService
    from kiro_crew.process_identity import ProcessHandle

    ro._reset_for_tests()
    handle = ProcessHandle(pid=5821, start_id="abc", pgid=5821, child_pids={})

    class _Sessions:
        def tearing_down(self, key: str) -> list[object]:
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
        patch("kiro_crew.process_identity.commit_runtime_teardown", lambda pid, epoch: False),
    ):
        failure = await CronService._sigkill_session(_Svc(), "cron:job", handle, who="Reaper")

    assert killed == [], "a tenant claiming after the gate stops the signal"
    assert failure == "a tenant claimed the runtime after the gate allowed it", failure


def test_a_child_whose_ancestry_cannot_be_proven_is_never_captured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTATION TARGET: the capture validates each child's ancestry, not just its identity.

    The identity capture is itself downstream of the enumeration. A child that exits
    between the two frees its number, and a replacement is then captured under its OWN
    identity -- which matches at signal time, so the identity check passes and the
    stranger is killed. Identity alone cannot see this, because the wrong process was
    measured.

    Walking the parent chain back to the root is what rules it out: whatever is
    captured was provably in this tree at that moment.
    """
    from kiro_crew import platform_compat, session_pid

    signalled: list[int] = []
    monkeypatch.setattr("kiro_crew.acp.client._get_child_pids", lambda pid: [5911, 5912])
    monkeypatch.setattr(session_pid, "_is_managed_agent_process", lambda pid: True)
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: f"id-{pid}")
    # 5911 is still ours. 5912's number belongs to a process outside this tree by the
    # time of the capture, so its parent chain does not reach the root.
    monkeypatch.setattr(platform_compat, "get_ppid", lambda pid: 5910 if pid == 5911 else 1)
    monkeypatch.setattr(platform_compat, "kill_pid", lambda pid, sig: signalled.append(pid))
    monkeypatch.setattr(
        platform_compat, "kill_process_tree", lambda pid, sig: signalled.append(pid)
    )

    total, root_killed = session_pid._kill_pid_tree(5910, expected_start="id-5910")

    assert 5912 not in signalled, (
        "a pid whose parent chain no longer reaches the root is not in this tree and "
        f"must not be signalled; {signalled}"
    )
    assert signalled == [5911, 5910], f"the real descendant and the root still are; {signalled}"
    assert (total, root_killed) == (2, True)


def test_the_ancestry_walk_follows_a_chain_and_refuses_a_cycle() -> None:
    """The helper's own two answers, because the guard above rests on both.

    A grandchild is still ours -- the walk follows parent links, it does not require a
    direct edge. A parent chain that loops proves nothing and answers no rather than
    spinning.
    """
    from unittest.mock import patch

    from kiro_crew import session_pid

    chain = {9003: 9002, 9002: 9001, 9001: 1}
    with patch("kiro_crew.platform_compat.get_ppid", lambda pid: chain.get(pid, -1)):
        assert session_pid._is_our_descendant(9003, 9001) is True, "a grandchild is ours"
        assert session_pid._is_our_descendant(9003, 9999) is False, "a stranger's root is not"

    loop = {9011: 9012, 9012: 9011}
    reads: list[int] = []

    def _looping(pid: int) -> int:
        reads.append(pid)
        return loop.get(pid, -1)

    with patch("kiro_crew.platform_compat.get_ppid", _looping):
        assert session_pid._is_our_descendant(9011, 9001) is False, "a cycle answers no"
    # The walk limit alone would also answer no, so what the cycle guard adds is
    # STOPPING: two reads, not one per allowed step. Asserting only the verdict would
    # let the guard be removed with the test still green.
    assert reads == [9011, 9012], f"a cycle is detected on its second link; {reads}"

    with patch("kiro_crew.platform_compat.get_ppid", lambda pid: -1):
        assert session_pid._is_our_descendant(9021, 9001) is False, "an unreadable link answers no"


@pytest.mark.asyncio
async def test_the_orphan_reconcile_audits_a_barrier_refusal_as_refused(
    agent_root: Path,
) -> None:
    """MUTATION TARGET: a barrier refusal is not a kill.

    Two things refuse this kill and both must read the same in the audit: the ownership
    gate declining, and the teardown barrier declining because a tenant arrived after
    it allowed. The second leaves the failure field empty -- no signal was attempted at
    all -- which by that field alone is indistinguishable from a clean kill. This row is
    the only place the process's fate is recorded, so "killed" here is a claim about a
    process still running.
    """
    from unittest.mock import MagicMock, patch

    from kiro_crew.subagent import SubagentManager
    from kiro_crew.subagent_persistence import create_agent_folder, update_state

    ro._reset_for_tests()
    rows: list[str] = []

    class _Sel:
        def log_tool_invocation(self, **kw: Any) -> None:
            rows.append(str(kw.get("outcome")))

    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    create_agent_folder("barriered-orphan", task="a shared runtime")
    update_state("barriered-orphan", pid=5931)

    with (
        # ``kiro_crew.subagent.sel``, not ``kiro_crew.sel.sel``: every ``*_impl`` is
        # rebound onto ``subagent``'s globals, so that is where the name resolves.
        patch("kiro_crew.subagent.sel", lambda: _Sel()),
        patch("kiro_crew.process_identity.commit_runtime_teardown", lambda pid, epoch: False),
        patch.object(manager, "_is_pid_alive", return_value=True),
        patch.object(manager, "_is_orphan_process", return_value=True),
        patch.object(manager, "_kill_orphan_pid") as mock_kill,
        patch.object(manager, "_notify_orphan", return_value=None),
    ):
        await manager._reconcile_orphans()

    mock_kill.assert_not_called(), "the barrier declined, so nothing was signalled"
    assert rows == ["refused"], (
        "and the only row recording this process's fate says refused, not killed; " f"{rows}"
    )


def test_every_tenancy_claim_in_this_file_is_bound_and_released() -> None:
    """MUTATION TARGET: no claim in this file may leak into another test.

    The tenancy table is a process-wide singleton, and ``RuntimeTenancy._alive``
    answers True unconditionally for an ``int`` target -- there is no pid to probe. So
    a claim whose handle is discarded stays live for the worker's whole life, and every
    later barrier on that pid refuses. Nothing resets the singleton between files;
    only the explicit ``ro._reset_for_tests()`` calls do. The failure is silent where it
    is caused and surfaces as an order-dependent failure somewhere else -- a literal pid
    shared with ``test_cron_reaper.py``, which never resets the table, inherits the claim there.

    A source scan rather than a runtime check, because the leak is invisible at
    runtime: the claim succeeds, the test passes, and the damage lands elsewhere.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    # Assembled from parts so this assertion's own body is not the first thing it
    # reports as an offender.
    claim_call = "claim_runtime_tenancy" + "("
    release_call = "release_runtime_" + "tenancy" + "("

    claims = [
        (n, ln.strip())
        for n, ln in enumerate(source.splitlines(), start=1)
        if claim_call in ln and "release_" not in ln
    ]
    unbound = [(n, ln) for n, ln in claims if "=" not in ln.split(claim_call)[0]]
    assert unbound == [], (
        "every tenancy claim must bind its handle so a finally can release it; "
        f"unbound at {[n for n, _ in unbound]}"
    )
    releases = source.count(release_call)
    assert releases >= len(claims), (
        f"{len(claims)} claims but only {releases} releases -- one of them leaks into "
        "another test"
    )


@pytest.mark.asyncio
async def test_the_tenancy_table_is_empty_for_this_files_pids_at_the_end() -> None:
    """POSITIVE CONTROL for the scan: the pids this file claims are free afterwards.

    Reads the table as the earlier tests in this file left it (the module-level
    ``keep_runtime_ownership_tables`` mark keeps the rootdir conftest from wiping it
    at every test boundary); wiped, the assertion below would hold against an empty
    table and prove nothing.

    The scan reads text; this reads the table, so a scan that matched nothing -- a
    renamed accessor, a typo in the needle -- cannot pass while every claim leaks.

    What it does NOT do is catch a leak from a test that has a later
    ``ro._reset_for_tests()`` after it, because that reset wipes the singleton and this
    runs at the end of the file. The scan is what covers those, which is why the leak
    guard is a source scan rather than this. Read together: the scan proves every claim
    is bound and released in the text, and this proves the table those calls act on is
    the real one and is clean when the file finishes.
    """
    for pid in (_TENANTED, _CLEARED_A, 5811, 424242):
        assert ro.RUNTIME_TENANCY.claims_on_pid(pid) == 0, (
            f"pid {pid} still carries a claim from an earlier test in this file, which "
            "refuses every later barrier on it"
        )
