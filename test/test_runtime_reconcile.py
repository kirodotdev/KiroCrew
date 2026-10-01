"""Behaviour of the two-way runtime reconciler and of the lease gate on every sweep.

Two rules are under test, and they are opposites of each other.

The reconciler must ACT on a disagreement between the kernel and this gateway's
records, because a disagreement nobody resolves is a process that survives every
sweep and every restart. It must also be extremely reluctant to act in the one
direction that ends a process, because absence from a record is evidence that
something is unclaimed and not evidence that it is abandoned -- the unowned
population on a healthy host is mostly processes with good owners that no
*session* record describes.

The sweeps must REFUSE to signal a pid a session still holds a lease on. Each of
them already carries a shield, and the shield is a set gathered before the
candidate scan and an event-loop hop before the signal; the gate is asked about
one pid at the decision point, which is the only place the answer cannot already
be stale.

Every guard added by this change is named here with the test that goes red when
it is removed, so a later reader can verify the guard still earns its place:

* the spawn-marker condition -> ``test_a_process_without_our_marker_is_never_killed``
* the two-pass confirmation -> ``test_one_pass_only_counts_an_unowned_process``
* the age floor -> ``test_a_process_younger_than_the_floor_is_never_killed``
* the last-moment gate -> ``test_the_gate_still_refuses_after_every_other_condition``
* the kill budget -> ``test_one_pass_spends_a_bounded_number_of_kills``
* the unreadable-registry refusal -> ``test_an_unreadable_registry_refuses_the_whole_pass``
* the unsignalable-pid rule -> ``test_an_unsignalable_pid_is_not_a_dead_one``
* the recycle check -> ``test_a_record_whose_pid_now_names_a_stranger_counts_as_dead``
* its fail-closed answer -> ``test_an_unreadable_identity_is_not_a_stranger``
* the periodic pid sweep's gate -> ``test_the_periodic_pid_sweep_withholds_a_leased_pid``
* the untracked-MCP sweep's gate -> ``test_the_untracked_mcp_sweep_withholds_a_leased_pid``
* the scope reaper's gate -> ``test_the_scope_reaper_does_not_signal_a_leased_pid``
* the orphan reconcile's gate -> ``test_the_orphan_reconcile_withholds_a_leased_pid``
* the reset ladder's gate -> ``test_the_reset_ladder_withholds_the_kill_and_the_shared_child_sweep``
* the cron reaper's gate -> ``test_the_cron_reaper_reports_a_leased_runtime_instead_of_killing_it``

Every one of those pairings is executed, not asserted in prose: the harness named
in the pull request re-applies each mutation and requires the named test to fail.

Three positive controls sit beside them, because a refusal test passes for free
when the thing it refuses never happens: the spawn-marker read against a fixture
process table, the recycle seam against a registry file in the product's own
format, and the lease seam against the real ownership table.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import runtime_ownership as ro
from kiro_crew import runtime_reconcile as rr

# The rootdir conftest wipes runtime_ownership's tables on both sides of every
# test. This file does its own intra-file isolation with explicit
# ``ro._reset_for_tests()`` calls, and its positive control at the end reads the
# table to prove the earlier tests left it clean -- a wipe at ANY test's teardown
# would empty it first and make that read vacuous. Module-wide, not per test,
# because the predecessor's teardown is the wipe that matters.
pytestmark = pytest.mark.keep_runtime_ownership_tables

# ── the reconciler core ───────────────────────────────────────────────────────


def _reconciler(
    *,
    kernel: set[int],
    recorded: set[int],
    alive: set[int] | None = None,
    recycled: set[int] | None = None,
    ours: set[int] | None = None,
    managed: set[int] | None = None,
    leased: set[int] | None = None,
    authorize: bool = True,
    age: float = 10_000.0,
    # The shipped budget, which is what these tests exercise: the conditions under
    # test only mean something when the arm is allowed to signal. The observe-only
    # behaviour an operator selects by setting 0 has its own tests, which pass 0
    # explicitly.
    max_kills: int = rr.DEFAULT_MAX_KILLS,
    killed: list[int] | None = None,
    forgotten: list[int] | None = None,
    notified: list[int] | None = None,
    audited: list[tuple[int, str, str]] | None = None,
) -> rr.RuntimeReconciler:
    """A reconciler over a fake kernel: no real processes, no real signals.

    The identity seam is faked too, and must be. Left at its default it reads the
    HOST for these invented pid numbers, so whether a test passes depends on
    whether the host happens to have a process at pid 100 -- true on a developer
    box, where those numbers are kernel threads, and false on a CI container,
    where the identity reads as unreadable and the kill arm fail-closes. Tests
    about identity build their own reconciler and pass their own seam.
    """
    live = kernel if alive is None else alive
    return rr.RuntimeReconciler(
        slice_pids=lambda: set(kernel),
        recorded_pids=lambda: set(recorded),
        is_alive=lambda pid: pid in live,
        was_recycled=lambda pid: pid in (recycled or set()),
        is_ours=lambda pid: pid in (kernel if ours is None else ours),
        # Faked for the reason the identity seam is: left at its default it reads the
        # HOST's argv for these invented numbers, so every kill test would depend on
        # whether the box happens to run a harness at pid 100. Tests about the argv
        # condition pass their own set.
        is_managed=lambda pid: pid in (kernel if managed is None else managed),
        leases_on=lambda pid: 1 if pid in (leased or set()) else 0,
        authorize=lambda pid, reason: authorize,
        identity_of=lambda pid: f"id-{pid}",
        kill_tree=lambda pid, expected=None: (killed if killed is not None else []).append(pid)
        or 1,
        # "retracted" is the seam's real answer for a row it removed; the tri-state is
        # what lets a record nobody can retract stop being reported as a failure.
        forget=lambda pid: ((forgotten if forgotten is not None else []).append(pid), "retracted")[
            1
        ],
        notify_dead=lambda pid: (notified if notified is not None else []).append(pid),
        age_secs=lambda pid: age,
        max_kills=max_kills,
        audit=lambda pid, outcome, why: (audited if audited is not None else []).append(
            (pid, outcome, why)
        ),
    )


def _refusals(audited: list[tuple[int, str, str]]) -> tuple[str, ...]:
    """The reasons a pass held pids back, read off the audit rows it wrote.

    The hold reason has one consumer, and it is the operator: a SEL row and a log
    line. Asserting it here rather than through a field on the reading means a
    mutation that corrupts the reason also fails the test, and the reading carries
    only the five counts the SLI publishes.
    """
    return tuple(why for _pid, outcome, why in audited if outcome == "refused")


def test_a_live_process_no_record_claims_is_counted() -> None:
    """The reading is the point even when nothing is killed: a leak an operator
    can see is a leak that gets fixed."""
    killed: list[int] = []
    reading = _reconciler(kernel={100, 200}, recorded={100}, killed=killed).run_once()
    assert reading.unowned_alive == 1
    assert reading.owned_alive == 1
    assert killed == [], "nothing dies on the pass that first notices it"


def test_one_pass_only_counts_an_unowned_process() -> None:
    """MUTATION TARGET: the two-pass confirmation.

    A record is published AFTER the process it describes exists, so every spawn
    has a window in which it is unowned. Killing on one sighting kills inside
    that window.
    """
    killed: list[int] = []
    audited: list[tuple[int, str, str]] = []
    rec = _reconciler(kernel={100, 200}, recorded={100}, killed=killed, audited=audited)
    first = rec.run_once()
    assert first.killed == 0
    assert ("first pass unowned",) == _refusals(audited)
    second = rec.run_once()
    assert second.killed == 1 and killed == [200]


def test_a_process_without_our_marker_is_never_killed() -> None:
    """MUTATION TARGET: the spawn-marker condition.

    The marker is read out of the kernel's exec-time copy, which a same-uid
    process cannot forge. Without it the reconciler would end a stranger's
    process that merely happened to be inside the slice.
    """
    killed: list[int] = []
    audited: list[tuple[int, str, str]] = []
    rec = _reconciler(kernel={100, 200}, recorded={100}, ours=set(), killed=killed, audited=audited)
    rec.run_once()
    audited.clear()
    reading = rec.run_once()
    assert reading.killed == 0 and killed == []
    assert ("no spawn marker",) == _refusals(audited)


def test_a_process_younger_than_the_floor_is_never_killed() -> None:
    """MUTATION TARGET: the age floor. The same registration window, seen from
    the process's side rather than from the record's."""
    killed: list[int] = []
    audited: list[tuple[int, str, str]] = []
    rec = _reconciler(kernel={100, 200}, recorded={100}, age=1.0, killed=killed, audited=audited)
    rec.run_once()
    audited.clear()
    reading = rec.run_once()
    assert reading.killed == 0 and killed == []
    assert ("younger than the age floor",) == _refusals(audited)


def test_the_gate_still_refuses_after_every_other_condition() -> None:
    """MUTATION TARGET: the last-moment ownership gate.

    Every condition above is about the process. This one is about who is using
    it, and it is asked last because a lease can be taken between the scan and
    the signal.
    """
    killed: list[int] = []
    audited: list[tuple[int, str, str]] = []
    rec = _reconciler(
        kernel={100, 200}, recorded={100}, authorize=False, killed=killed, audited=audited
    )
    rec.run_once()
    audited.clear()
    reading = rec.run_once()
    assert reading.killed == 0 and killed == []
    assert ("refused by the ownership gate",) == _refusals(audited)


def test_a_leased_pid_is_never_unowned() -> None:
    """A lease IS a record. A pid with one outstanding is claimed, whatever the
    session map and the backend pidfile happen to say."""
    reading = _reconciler(kernel={100, 200}, recorded=set(), leased={100, 200}).run_once()
    assert reading.unowned_alive == 0


def test_one_pass_spends_a_bounded_number_of_kills() -> None:
    """MUTATION TARGET: the kill budget.

    A reconciler that has misjudged a whole population should be wrong slowly
    enough for the reading to be noticed before the population is gone.
    """
    kernel = set(range(100, 120))
    killed: list[int] = []
    audited: list[tuple[int, str, str]] = []
    rec = _reconciler(kernel=kernel, recorded=set(), max_kills=3, killed=killed, audited=audited)
    rec.run_once()
    audited.clear()
    reading = rec.run_once()
    assert reading.killed == 3 and len(killed) == 3
    assert "kill budget spent" in set(_refusals(audited))


def test_a_record_naming_a_dead_process_is_retracted_and_its_holder_told() -> None:
    """The other direction. Forgetting costs nothing -- the process is already
    gone -- and the holder learns why instead of waiting out a timeout."""
    forgotten: list[int] = []
    notified: list[int] = []
    reading = _reconciler(
        kernel=set(),
        recorded={100, 200},
        alive={100},
        forgotten=forgotten,
        notified=notified,
    ).run_once()
    assert reading.owned_dead == 1 and reading.owned_alive == 1
    assert forgotten == [200] and notified == [200]


def test_a_record_whose_pid_now_names_a_stranger_counts_as_dead() -> None:
    """MUTATION TARGET: the recycle check in the dead direction.

    A live pid whose start identity differs from the recorded one is not our
    process. Counting it healthy is what leaves a sweep signalling by pid aimed
    at somebody else, so it joins the dead population, its record is retracted,
    and the stranger itself is never signalled.
    """
    killed: list[int] = []
    forgotten: list[int] = []
    notified: list[int] = []
    reading = _reconciler(
        kernel={200},
        recorded={200},
        alive={200},
        recycled={200},
        killed=killed,
        forgotten=forgotten,
        notified=notified,
    ).run_once()
    assert reading.owned_dead == 1 and reading.owned_alive == 0
    assert forgotten == [200] and notified == [200]
    assert killed == [], "the current holder of a recycled pid is not ours to end"


def test_a_recycled_pid_is_never_also_counted_as_unowned() -> None:
    """One process, one population. A recycled pid is in a record, so it is a
    disagreement about that record and never a second finding in the kernel
    direction -- where it would be eligible for a kill."""
    killed: list[int] = []
    rec = _reconciler(kernel={200}, recorded={200}, alive={200}, recycled={200}, killed=killed)
    rec.run_once()
    reading = rec.run_once()
    assert reading.unowned_alive == 0
    assert killed == []


def test_an_unreadable_identity_is_not_a_stranger() -> None:
    """MUTATION TARGET: the recycle check's fail-closed answer.

    The start token is subtractive only: an identity that cannot be read on
    either side is an unknown, never a mismatch. A live runtime called a stranger
    loses the record that is the only thing able to find it again.
    """
    forgotten: list[int] = []

    def raising_check(pid: int) -> bool:
        raise OSError("cannot read the identity")

    rec = rr.RuntimeReconciler(
        slice_pids=lambda: set(),
        recorded_pids=lambda: {100},
        is_alive=lambda pid: True,
        was_recycled=raising_check,
        kill_tree=lambda pid, expected=None: 0,
        forget=lambda pid: forgotten.append(pid) is None,
        notify_dead=lambda pid: None,
    )
    reading = rec.run_once()
    assert reading.owned_dead == 0 and reading.owned_alive == 1
    assert forgotten == []


def test_an_unsignalable_pid_is_not_a_dead_one() -> None:
    """MUTATION TARGET: the liveness probe's tri-state handling.

    An unreadable probe is an unknown process. Retracting a record on that
    answer is how a live runtime loses the only thing that can find it again.
    """
    forgotten: list[int] = []

    def raising_probe(pid: int) -> bool:
        raise PermissionError("cannot probe")

    rec = rr.RuntimeReconciler(
        slice_pids=lambda: set(),
        recorded_pids=lambda: {100},
        is_alive=raising_probe,
        kill_tree=lambda pid, expected=None: 0,
        forget=lambda pid: forgotten.append(pid) is None,
        notify_dead=lambda pid: None,
    )
    reading = rec.run_once()
    assert reading.owned_dead == 0 and forgotten == []


def test_an_unreadable_registry_refuses_the_whole_pass() -> None:
    """MUTATION TARGET: the registry-read refusal.

    A registry that cannot be read makes EVERY live process look unowned. That
    single input is the one that turns a pass into a massacre, so the pass is
    abandoned rather than half-applied.
    """
    killed: list[int] = []

    def no_registry() -> set[int]:
        raise OSError("gatewayd pidfile is gone")

    rec = rr.RuntimeReconciler(
        slice_pids=lambda: {100, 200, 300},
        recorded_pids=no_registry,
        is_alive=lambda pid: True,
        kill_tree=lambda pid, expected=None: killed.append(pid) or 1,
        forget=lambda pid: True,
        notify_dead=lambda pid: None,
    )
    reading = rec.run_once()
    assert reading.supported is False and "registry" in reading.reason
    assert reading.unowned_alive == 0 and killed == []


def test_an_unreadable_slice_refuses_the_pass() -> None:
    def no_slice() -> set[int]:
        raise OSError("cgroup is not delegated")

    rec = rr.RuntimeReconciler(
        slice_pids=no_slice,
        recorded_pids=lambda: {100},
        is_alive=lambda pid: True,
        kill_tree=lambda pid, expected=None: 0,
        forget=lambda pid: True,
        notify_dead=lambda pid: None,
    )
    reading = rec.run_once()
    assert reading.supported is False and "slice" in reading.reason


def test_the_reading_publishes_the_two_liveness_sli_names() -> None:
    """The counts are consumed by the liveness SLI under these exact names."""
    fields = _reconciler(kernel={100, 200}, recorded={100}).run_once().as_counter_fields()
    assert fields["unowned_alive"] == 1
    assert "owned_dead" in fields


def test_the_gateway_never_reports_itself_as_unowned() -> None:
    """The reconciler runs inside the very slice it reads."""
    mine = os.getpid()
    reading = _reconciler(kernel={mine, 1}, recorded=set()).run_once()
    assert reading.unowned_alive == 0, "our own pid and init are never candidates"


def test_the_marker_read_answers_from_a_fixture_process_table(tmp_path: Path) -> None:
    """POSITIVE CONTROL for the marker: without this, ``is_ours`` returning False
    everywhere would satisfy every refusal test above and mean nothing."""
    proc = tmp_path / "4242"
    proc.mkdir()
    (proc / "environ").write_bytes(b"PATH=/usr/bin\0KIROCREW_SPAWNED=1\0")
    assert rr.process_is_ours(4242, proc_root=tmp_path) is True

    other = tmp_path / "4243"
    other.mkdir()
    (other / "environ").write_bytes(b"PATH=/usr/bin\0")
    assert rr.process_is_ours(4243, proc_root=tmp_path) is False


def test_the_wiring_reads_a_recycled_identity_out_of_the_real_registry_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSITIVE CONTROL for the recycle seam: without this, ``was_recycled``
    never firing in production would satisfy the unit cases above and mean
    nothing.

    Writes the session file the product writes, in the product's own
    ``<gw>:<pid>:<token>`` form, then answers the live identity lookup with a
    different token for one pid and the recorded token for the other.
    """
    from kiro_crew import platform_compat, session_pid

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(session_pid, "config_dir", lambda: home)
    gw = os.getpid()
    (home / "kiro_session_pids.txt").write_text(
        f"{gw}:5001:TOKEN-AS-RECORDED\n{gw}:5002:TOKEN-AS-RECORDED\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        session_pid,
        "_pid_start_token",
        lambda pid: "TOKEN-AS-RECORDED" if pid == 5001 else "A-DIFFERENT-PROCESS",
    )
    monkeypatch.setattr(rr, "_mcp_backend_pids", lambda: set())

    forgotten: list[str] = []
    real_retract = rr._retract_session_rows

    def _recording_retract(rows: object) -> None:
        # Records the ROWS and does the real work: the retraction count is an
        # observed disappearance, so a fake that only recorded would report zero.
        forgotten.extend(sorted(rows))  # type: ignore[arg-type]
        real_retract(rows)  # type: ignore[arg-type]

    monkeypatch.setattr(rr, "_retract_session_rows", _recording_retract)
    monkeypatch.setattr("kiro_crew.session_scope_reap.instance_slice_pids", lambda: {5001, 5002})
    monkeypatch.setattr(
        "kiro_crew.platform_compat.pid_liveness", lambda pid: platform_compat.PID_ALIVE
    )

    reconciler = rr.build_reconciler(active_pids=lambda: set(), notify_dead=lambda pid: None)
    reading = reconciler.run_once()

    assert reading.owned_dead == 1, "the pid whose live identity differs is the dead one"
    assert reading.owned_alive == 1
    # The VERBATIM row, never the bare number: that is what keeps a replacement's
    # row under the same reused pid out of the retraction.
    assert forgotten == [f"{gw}:5002:TOKEN-AS-RECORDED"], forgotten
    assert reading.unowned_alive == 0, "both pids are in a record, so neither is unowned"


@pytest.mark.asyncio
async def test_the_lease_seam_reads_the_real_ownership_table() -> None:
    """POSITIVE CONTROL for the lease seam: without this, the production default
    answering 0 for everything would satisfy every refusal test above and mean
    nothing.

    The reconciler starts from the kernel's list of process ids, so a pid is all
    it has and the table's pid accessor is the whole answer. Asked here through
    the same public surface the production default uses.
    """
    ro._reset_for_tests()
    assert rr._leases_on_pid(4321) == 0

    holder = _LeaseHolder(4321)
    await holder.take()
    try:
        assert rr._leases_on_pid(4321) == 1, "a held lease is visible to the seam"
    finally:
        await holder.give_back()

    assert rr._leases_on_pid(4321) == 0, "releasing it is visible too"


def test_a_kill_that_signalled_nothing_is_not_counted_as_one() -> None:
    """MUTATION TARGET: reading the tree-kill seam's count.

    The seam re-applies its own recycle guard and signals nothing for a pid that
    does not look like a managed agent process -- which is most of what an unowned
    reading is made of. Counting an unsignalled call as a kill would spend the
    whole per-pass budget on the same lowest pids every pass, forever, while the
    reading reported kills and nothing changed.
    """
    audited: list[tuple[int, str, str]] = []
    rec = rr.RuntimeReconciler(
        slice_pids=lambda: {100, 200, 300},
        recorded_pids=lambda: set(),
        is_alive=lambda pid: True,
        is_ours=lambda pid: True,
        is_managed=lambda pid: True,  # host-independent: these pids are invented
        leases_on=lambda pid: 0,
        authorize=lambda pid, reason: True,
        identity_of=lambda pid: f"id-{pid}",  # host-independent: these pids are invented
        kill_tree=lambda pid, expected=None: 0,  # the guard inside refused: nothing signalled
        forget=lambda pid: True,
        notify_dead=lambda pid: None,
        age_secs=lambda pid: 10_000.0,
        audit=lambda pid, outcome, why: audited.append((pid, outcome, why)),
    )
    rec.run_once()
    audited.clear()
    reading = rec.run_once()
    assert reading.killed == 0, "an unsignalled call is not a kill"
    assert ("kill signalled nothing",) * 3 == _refusals(audited)


def test_the_holders_of_dead_runtimes_are_told_once_not_every_pass() -> None:
    """MUTATION TARGET: the notify-once memory.

    Retraction does not always remove the pid from every record -- one held in the
    manager's own live union is not in the file the untracker rewrites -- so the
    same disagreement is rediscovered on every pass. The COUNT must stay a fresh
    reading, but the notification reaches the user's chat, and one per pass per
    cleanup interval would append to it for as long as the gateway runs.
    """
    notified: list[int] = []
    rec = rr.RuntimeReconciler(
        slice_pids=lambda: set(),
        recorded_pids=lambda: {100},
        is_alive=lambda pid: False,
        kill_tree=lambda pid, expected=None: 0,
        forget=lambda pid: True,  # the rewrite lands; the pid stays in another record
        notify_dead=notified.append,
    )
    first = rec.run_once()
    second = rec.run_once()
    assert notified == [100], f"told once, not once per pass; got {notified}"
    assert first.owned_dead == 1 and second.owned_dead == 1, "the count stays a fresh reading"


def test_an_absent_backend_pidfile_is_an_empty_set_not_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION TARGET: absence and unreadability are different answers.

    No broker runs at all under the default empty stub configuration, gatewayd
    unlinks the file on a clean shutdown, and it appears only a heartbeat after
    start. Treating any of those as a refusal leaves both directions and the
    liveness reading permanently inert behind a single debug line -- the quietest
    possible way for this module to do nothing at all.
    """
    missing = tmp_path / "gateway.sock"
    monkeypatch.setattr("kiro_crew.runtime_reconcile.configured_socket_path", lambda: str(missing))
    assert rr._mcp_backend_pids() == set(), "an absent pidfile means nothing is hosted"

    # A file that EXISTS and cannot be read is still a refusal: presenting every
    # live backend as unowned is the one input that makes a pass dangerous.
    unreadable = tmp_path / "gateway.sock.backends"
    unreadable.mkdir()
    with pytest.raises(OSError):
        rr._mcp_backend_pids()


def test_a_recycled_pid_does_not_inherit_the_previous_passs_confirmation() -> None:
    """MUTATION TARGET: the two-pass memory is keyed on identity, not just number.

    Keyed on the number alone, a candidate that exits between passes hands its
    confirmation to whatever process the kernel gives the number to next -- so the
    replacement is eligible on its first sighting, which is exactly what the
    two-pass rule exists to prevent.
    """
    killed: list[int] = []
    identities = {200: "as-classified"}

    audited: list[tuple[int, str, str]] = []
    rec = rr.RuntimeReconciler(
        slice_pids=lambda: {200},
        recorded_pids=lambda: set(),
        is_alive=lambda pid: True,
        identity_of=lambda pid: identities[pid],
        is_ours=lambda pid: True,
        is_managed=lambda pid: True,  # host-independent: these pids are invented
        leases_on=lambda pid: 0,
        authorize=lambda pid, reason: True,
        kill_tree=lambda pid, expected=None: killed.append(pid) or 1,
        forget=lambda pid: True,
        notify_dead=lambda pid: None,
        age_secs=lambda pid: 10_000.0,
        audit=lambda pid, outcome, why: audited.append((pid, outcome, why)),
    )
    rec.run_once()  # classified once; nothing dies on first sighting
    identities[200] = "a-different-process"  # exited, and the kernel reused 200
    audited.clear()
    rec.run_once()

    assert killed == [], "the replacement is on its own first pass, not the victim's second"
    assert ("first pass unowned",) == _refusals(audited)

    # And once the replacement has its OWN two passes, it is eligible.
    third = rec.run_once()
    assert killed == [200] and third.killed == 1


def test_an_identity_that_changes_before_the_signal_withholds_the_kill() -> None:
    """MUTATION TARGET: the identity re-check in the instant before the signal.

    Every check before it -- the two-pass memory, the marker, the age floor, the
    gate -- inspected a pid NUMBER, and the kernel can hand that number to another
    process after any of them. The last-instant re-read is what keeps the signal
    aimed at the process this pass actually classified.

    Simulated where it really happens: WITHIN one pass, between classification and
    the signal, by answering the identity read differently the second time.
    """
    killed: list[int] = []
    reads: dict[int, int] = {200: 0}

    def identity_of(pid: int) -> str | None:
        reads[pid] += 1
        # Stable while the two-pass memory is being built, then changed on the
        # read that happens immediately before the signal.
        return "as-classified" if reads[pid] <= 2 else "a-different-process"

    audited: list[tuple[int, str, str]] = []
    rec = rr.RuntimeReconciler(
        slice_pids=lambda: {200},
        recorded_pids=lambda: set(),
        is_alive=lambda pid: True,
        identity_of=identity_of,
        is_ours=lambda pid: True,
        is_managed=lambda pid: True,  # host-independent: these pids are invented
        leases_on=lambda pid: 0,
        authorize=lambda pid, reason: True,
        kill_tree=lambda pid, expected=None: killed.append(pid) or 1,
        forget=lambda pid: True,
        notify_dead=lambda pid: None,
        age_secs=lambda pid: 10_000.0,
        audit=lambda pid, outcome, why: audited.append((pid, outcome, why)),
    )
    rec.run_once()
    audited.clear()
    rec.run_once()

    assert killed == [], "a pid that is no longer the classified process is not signalled"
    assert ("process identity changed since classification",) == _refusals(audited)


def test_an_unreadable_identity_withholds_the_kill() -> None:
    """The fail-closed half of the same check: a process this pass cannot identify
    is one it cannot claim to have inspected, so the kill waits for a pass that
    can. Cheap -- the next pass retries."""
    killed: list[int] = []

    def no_identity(pid: int) -> str | None:
        raise OSError("cannot read the identity")

    audited: list[tuple[int, str, str]] = []
    rec = rr.RuntimeReconciler(
        slice_pids=lambda: {200},
        recorded_pids=lambda: set(),
        is_alive=lambda pid: True,
        identity_of=no_identity,
        is_ours=lambda pid: True,
        is_managed=lambda pid: True,  # host-independent: these pids are invented
        leases_on=lambda pid: 0,
        authorize=lambda pid, reason: True,
        kill_tree=lambda pid, expected=None: killed.append(pid) or 1,
        forget=lambda pid: True,
        notify_dead=lambda pid: None,
        age_secs=lambda pid: 10_000.0,
        audit=lambda pid, outcome, why: audited.append((pid, outcome, why)),
    )
    rec.run_once()
    audited.clear()
    rec.run_once()
    assert killed == []
    assert ("process identity changed since classification",) == _refusals(audited)


def test_an_identity_unreadable_at_the_last_moment_withholds_the_kill() -> None:
    """MUTATION TARGET: the fail-closed answer in the last-instant re-read itself.

    Distinct from the case where the identity was never captured: here
    classification succeeded and the re-read in the instant before the signal is
    what fails. A process whose identity cannot be confirmed at the moment of the
    signal is one this pass cannot claim to be signalling, so the kill waits.
    """
    killed: list[int] = []
    reads: dict[int, int] = {200: 0}

    def identity_of(pid: int) -> str | None:
        reads[pid] += 1
        if reads[pid] > 2:  # the read immediately before the signal
            raise OSError("cannot read the identity now")
        return "as-classified"

    audited: list[tuple[int, str, str]] = []
    rec = rr.RuntimeReconciler(
        slice_pids=lambda: {200},
        recorded_pids=lambda: set(),
        is_alive=lambda pid: True,
        identity_of=identity_of,
        is_ours=lambda pid: True,
        is_managed=lambda pid: True,  # host-independent: these pids are invented
        leases_on=lambda pid: 0,
        authorize=lambda pid, reason: True,
        kill_tree=lambda pid, expected=None: killed.append(pid) or 1,
        forget=lambda pid: True,
        notify_dead=lambda pid: None,
        age_secs=lambda pid: 10_000.0,
        audit=lambda pid, outcome, why: audited.append((pid, outcome, why)),
    )
    rec.run_once()
    audited.clear()
    rec.run_once()
    assert killed == []
    assert ("process identity changed since classification",) == _refusals(audited)


def test_process_age_comes_from_the_process_start_not_the_procfs_inode() -> None:
    """MUTATION TARGET: which clock the age floor reads.

    ``/proc/<pid>``'s own ``st_ctime`` is assigned when procfs instantiates the
    inode, which can be long after the process started. An age taken from it reads
    every candidate as younger than the floor, so the kill arm never reclaims
    anything and the whole population grows unchecked while the reading looks
    healthy.

    Pinned against the repository's own helper rather than a recomputation of it,
    and against this live process, whose age is genuinely non-zero.
    """
    from kiro_crew import platform_compat
    from kiro_crew.session_pid import _pid_age_seconds

    mine = os.getpid()
    if platform_compat.IS_WINDOWS:
        # No procfs and no start clock there, which the helper documents by
        # returning None. The contract that matters on Windows is the fail-closed
        # one: an unreadable age is too young to touch, so the age floor withholds
        # every kill rather than reading each candidate as ancient.
        assert _pid_age_seconds(mine) is None
        assert rr.process_age_secs(mine) == 0.0
        assert rr.process_age_secs(2**31 - 1) == 0.0
        return

    expected = _pid_age_seconds(mine)
    assert expected is not None, "this process's own age must be readable"
    measured = rr.process_age_secs(mine)
    assert measured > 0.0, "a live process is not zero seconds old"
    # Tight on purpose: the two readings are of the same clock microseconds apart,
    # so any real disagreement is the wrong clock. A loose tolerance would accept a
    # constant, because the pytest process is itself only seconds old.
    assert abs(measured - expected) < 0.5, f"age {measured} disagrees with the helper {expected}"

    # Fail-closed: an unreadable pid is too young to touch, never old enough.
    assert rr.process_age_secs(2**31 - 1) == 0.0


def test_a_retraction_the_untracker_refused_is_not_counted_as_one() -> None:
    """MUTATION TARGET: reading the untracker's answer.

    ``_untrack_session_pid`` returns False when its rewrite of the tracking file
    could not land, which leaves the stale entry in place. Counting that as a
    retraction reports work that did not happen; the count of DEAD records stays
    honest either way, because the disagreement is still there and is re-detected
    on the next pass.
    """
    rec = rr.RuntimeReconciler(
        slice_pids=lambda: set(),
        recorded_pids=lambda: {100},
        is_alive=lambda pid: False,
        kill_tree=lambda pid, expected=None: 0,
        forget=lambda pid: False,  # the rewrite was refused
        notify_dead=lambda pid: None,
    )
    reading = rec.run_once()
    assert reading.owned_dead == 1, "the record is still stale, and the reading says so"
    assert reading.forgotten == 0, "but nothing was retracted, so nothing is counted"


def test_every_kill_decision_is_audited(tmp_path: Path) -> None:
    """MUTATION TARGET: the SEL audit on each outcome.

    Every other reap path in this gateway audits the processes it ends. A kill arm
    that did not would be the one place a process is signalled with no record of
    who decided it. The refusal is audited too, because "we decided not to" is what
    an operator needs when a leak reading stays non-zero.
    """
    events: list[tuple[int, str, str]] = []

    def audit(pid: int, outcome: str, reason: str) -> None:
        events.append((pid, outcome, reason))

    rec = rr.RuntimeReconciler(
        slice_pids=lambda: {200, 300},
        recorded_pids=lambda: set(),
        is_alive=lambda pid: True,
        identity_of=lambda pid: "stable",
        is_ours=lambda pid: pid == 200,  # 300 lacks the marker and is withheld
        is_managed=lambda pid: True,  # host-independent: these pids are invented
        leases_on=lambda pid: 0,
        authorize=lambda pid, reason: True,
        audit=audit,
        kill_tree=lambda pid, expected=None: 1,
        forget=lambda pid: True,
        notify_dead=lambda pid: None,
        age_secs=lambda pid: 10_000.0,
    )
    rec.run_once()
    events.clear()
    rec.run_once()

    by_outcome = {outcome for _pid, outcome, _why in events}
    assert by_outcome == {"killed", "refused"}, f"both outcomes are audited; got {by_outcome}"
    assert (200, "killed") in [(p, o) for p, o, _ in events]
    assert (300, "refused") in [(p, o) for p, o, _ in events]


def test_the_audit_seam_reaches_sel_and_cannot_break_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POSITIVE CONTROL for the audit, plus its fail-open rule.

    Without the control, an audit function that silently did nothing would satisfy
    the test above. And an audit that raised must not stop a reconciliation pass:
    losing one record is bad, losing the sweep is worse.
    """
    logged: list[dict[str, Any]] = []

    class _Sel:
        def log_tool_invocation(self, **kwargs: Any) -> None:
            logged.append(kwargs)

    monkeypatch.setattr("kiro_crew.sel.sel", lambda: _Sel())
    rr._sel_reconcile_kill(4242, "killed", "because")
    assert logged and logged[0]["tool_name"] == "runtime_reconcile"
    assert logged[0]["tool_kind"] == "process_kill"
    assert logged[0]["outcome"] == "killed"
    assert "4242" in logged[0]["resources"]

    def boom() -> Any:
        raise RuntimeError("sel is down")

    monkeypatch.setattr("kiro_crew.sel.sel", boom)
    rr._sel_reconcile_kill(4242, "killed", "because")  # must not raise


def test_a_withheld_pid_is_audited_on_a_reason_change_not_every_pass() -> None:
    """MUTATION TARGET: the reason-transition memory on the audit.

    Most of the unowned population is withheld permanently -- every MCP server and
    sandbox helper in the slice sits at the same reason forever -- so one event per
    pid per pass writes thousands of identical rows a day into a log with a finite
    rotation ceiling, evicting the history an operator needs. A transition is the
    event; a steady state is not.
    """
    events: list[tuple[int, str]] = []
    ours = {200}

    rec = rr.RuntimeReconciler(
        slice_pids=lambda: {200},
        recorded_pids=lambda: set(),
        is_alive=lambda pid: True,
        identity_of=lambda pid: "stable",
        is_ours=lambda pid: pid in ours,
        is_managed=lambda pid: True,  # host-independent: these pids are invented
        leases_on=lambda pid: 0,
        authorize=lambda pid, reason: False,  # always withheld, same reason
        audit=lambda pid, outcome, why: events.append((pid, why)),
        kill_tree=lambda pid, expected=None: 0,
        forget=lambda pid: True,
        notify_dead=lambda pid: None,
        age_secs=lambda pid: 10_000.0,
    )
    rec.run_once()  # first pass: reason is "first pass unowned"
    first = list(events)
    rec.run_once()  # now "refused by the ownership gate" -- a transition
    rec.run_once()  # same reason again -- no event
    rec.run_once()  # and again
    transitions = [why for _pid, why in events]

    assert first, "the first sighting is itself a transition and is audited"
    assert transitions.count("refused by the ownership gate") == 1, (
        "the steady state is audited once, not once per pass; " f"got {transitions}"
    )


def test_the_gate_is_not_asked_for_a_pid_the_earlier_checks_withhold() -> None:
    """MUTATION TARGET: asking the gate LAST.

    The gate's allow path writes the kill attribution, so asking it before the
    remaining checks records a kill of every pid those checks then withhold. The
    identity re-check is the one that runs before it.
    """
    asked: list[int] = []
    reads: dict[int, int] = {200: 0}

    def identity_of(pid: int) -> str | None:
        reads[pid] += 1
        return "as-classified" if reads[pid] <= 2 else "a-different-process"

    rec = rr.RuntimeReconciler(
        slice_pids=lambda: {200},
        recorded_pids=lambda: set(),
        is_alive=lambda pid: True,
        identity_of=identity_of,
        is_ours=lambda pid: True,
        is_managed=lambda pid: True,  # host-independent: these pids are invented
        leases_on=lambda pid: 0,
        authorize=lambda pid, reason: asked.append(pid) is None,
        kill_tree=lambda pid, expected=None: 1,
        forget=lambda pid: True,
        notify_dead=lambda pid: None,
        age_secs=lambda pid: 10_000.0,
    )
    rec.run_once()
    rec.run_once()
    assert asked == [], "a pid withheld on identity never reaches the attributing gate"


# ── the argv condition, and the kill budget ───────────────────────────────────


def _armed(
    *,
    kernel: set[int],
    managed: set[int],
    asked: list[int],
    killed: list[int] | None = None,
    audited: list[tuple[int, str, str]] | None = None,
    max_kills: int = rr.DEFAULT_MAX_KILLS,
) -> rr.RuntimeReconciler:
    """A reconciler past every condition except the argv one, recording gate calls."""
    return rr.RuntimeReconciler(
        slice_pids=lambda: set(kernel),
        recorded_pids=lambda: set(),
        is_alive=lambda pid: True,
        identity_of=lambda pid: f"id-{pid}",
        is_ours=lambda pid: True,
        is_managed=lambda pid: pid in managed,
        leases_on=lambda pid: 0,
        claims_on=lambda pid: 0,
        authorize=lambda pid, reason: asked.append(pid) is None,
        kill_tree=lambda pid, expected=None: (killed if killed is not None else []).append(pid)
        or 1,
        forget=lambda pid: "retracted",
        notify_dead=lambda pid: None,
        age_secs=lambda pid: 10_000.0,
        max_kills=max_kills,
        audit=lambda pid, outcome, why: (audited if audited is not None else []).append(
            (pid, outcome, why)
        ),
    )


def test_a_process_whose_argv_is_not_a_harness_never_reaches_the_gate() -> None:
    """MUTATION TARGET: the argv condition sits BEFORE the attributing gate.

    The gate's allow path writes ``runtime kill pid=N caller=runtime_reconcile``, and
    that line is the record a maintainer reads to answer "did we signal somebody
    else's process". The unowned population is dominated by processes that inherited
    our spawn marker and are not harnesses -- a chromium tree, an ``mcp
    start-server`` broker, a sibling install's interpreter -- and the kill seam
    declines every one of them. Asked after the gate, each first collects an
    attribution for a kill that never happens: 3151 such lines for 181 pids against
    4 real kills over 6.5 hours on one host.
    """
    asked: list[int] = []
    killed: list[int] = []
    audited: list[tuple[int, str, str]] = []
    rec = _armed(
        kernel={200, 300, 400},
        managed={300},
        asked=asked,
        killed=killed,
        audited=audited,
    )
    rec.run_once()
    audited.clear()
    reading = rec.run_once()

    assert asked == [300], f"only the harness reaches the gate; got {asked}"
    assert killed == [300], f"and only it is signalled; got {killed}"
    assert reading.killed == 1
    assert sorted(_refusals(audited)) == [
        "not a managed agent process",
        "not a managed agent process",
    ], f"the other two are withheld by name; got {_refusals(audited)}"


def test_an_unreadable_argv_withholds_the_kill() -> None:
    """The fail-closed half: a process whose argv cannot be read is not shown to be
    a harness, so it is withheld and never attributed."""
    asked: list[int] = []
    killed: list[int] = []
    audited: list[tuple[int, str, str]] = []

    def no_argv(pid: int) -> bool:
        raise OSError("cannot read the command line")

    rec = _armed(kernel={200}, managed=set(), asked=asked, killed=killed, audited=audited)
    rec._is_managed = no_argv  # type: ignore[method-assign]
    rec.run_once()
    audited.clear()
    rec.run_once()

    assert killed == [] and asked == []
    assert _refusals(audited) == ("not a managed agent process",)


def test_the_configured_ceiling_cannot_exceed_the_shipped_budget() -> None:
    """MUTATION TARGET: the field is SUBTRACTIVE.

    ``config.json`` is agent-writable and never passes the dashboard's write gate,
    and this value governs host-side signals. With the ceiling equal to the shipped
    budget every reachable setting is at or below what the product already does, so
    a write can withhold signals and cannot authorize one the arm would not already
    send. A ceiling above the default would make the field an arming surface.
    """
    from kiro_crew.config import sections

    assert sections.RECONCILE_MAX_KILLS_MAX == rr.DEFAULT_MAX_KILLS
    assert rr.DEFAULT_MAX_KILLS > 0, "and the shipped budget is a real one"


def test_the_config_default_matches_the_module_default() -> None:
    """The two spellings of the shipped default are pinned equal.

    ``config.sections`` cannot import this module -- config is a leaf package and the
    import would be a cycle -- so the value is written in both places. A test is the
    only place they can be held together.
    """
    from kiro_crew.config import sections

    assert sections.DEFAULT_RECONCILE_MAX_KILLS == rr.DEFAULT_MAX_KILLS
    assert sections.SessionConfig().reconcile_max_kills == rr.DEFAULT_MAX_KILLS


@pytest.mark.parametrize(
    ("stored", "loaded"),
    [
        (0, 0),
        (5, 5),
        # The ceiling equals the shipped budget, so a hand-edited value above it
        # cannot raise the arm past what the product already does.
        (999, 5),
        # A numeric STRING reaches the coercion rather than the raw-dict clamp
        # sweep, so the clamp has to live at the coercion site too.
        ("999", 5),
        # Negative clamps DOWN to the floor, which disables the arm rather than
        # enabling it -- the safe direction for a value that authorizes signals.
        (-1, 0),
        # Not a number at all reads as unset, so it takes the shipped default. That
        # is the same budget an unconfigured host runs, never more.
        ("plenty", 5),
        (True, 5),
    ],
)
def test_the_configured_budget_is_clamped_on_load(stored: object, loaded: int) -> None:
    """MUTATION TARGET: the loader's bounds on ``session.reconcile_max_kills``.

    ``config.json`` is agent-writable and never passes the dashboard's write gate, so
    the load path is the only place a hand-edited kill budget is bounded.
    """
    from kiro_crew.config import loader

    cfg = loader._build_session_config({"reconcile_max_kills": stored})
    assert cfg.reconcile_max_kills == loaded


def test_a_zero_budget_signals_nothing_and_audits_what_it_would_have_killed() -> None:
    """MUTATION TARGET: the observe-only arm.

    Zero budget must not mean zero information. A candidate that satisfied every
    local condition is counted in ``would_kill`` and audited under that outcome, so
    an operator who turned the budget down can see what turning it back up would
    take -- which ``unowned_alive`` cannot tell them, because it counts the whole
    unclaimed population.
    """
    asked: list[int] = []
    killed: list[int] = []
    audited: list[tuple[int, str, str]] = []
    rec = _armed(
        kernel={200, 300},
        managed={300},
        asked=asked,
        killed=killed,
        audited=audited,
        max_kills=0,  # observe-only, as an operator sets it
    )
    rec.run_once()
    audited.clear()
    reading = rec.run_once()

    assert killed == [], "nothing is signalled"
    assert asked == [], "and the gate is never asked, so no kill is attributed"
    assert reading.killed == 0
    assert reading.would_kill == 1, f"the candidate is counted; {reading.as_counter_fields()}"
    assert (
        300,
        "would_kill",
        "observing only: the kill budget is zero",
    ) in audited, f"and audited under its own outcome; got {audited}"
    assert reading.as_counter_fields()["would_kill"] == 1


def test_an_armed_budget_kills() -> None:
    """CONTROL for the test above: without this, an arm incapable of killing at all
    would satisfy it. An armed budget signals, attributes and counts."""
    asked: list[int] = []
    killed: list[int] = []
    rec = _armed(kernel={200, 300}, managed={200, 300}, asked=asked, killed=killed)
    rec.run_once()
    reading = rec.run_once()

    assert killed == [200, 300]
    assert asked == [200, 300], "and each one is attributed, because each one is signalled"
    assert reading.killed == 2 and reading.would_kill == 0


def test_the_budget_is_adopted_before_every_pass_not_frozen_at_construction() -> None:
    """MUTATION TARGET: :meth:`set_max_kills`.

    The instance is retained for the gateway's life -- the two-pass confirmation is
    its state -- while the budget is live config. Frozen at construction, turning it
    down and back up would need a restart.
    """
    asked: list[int] = []
    killed: list[int] = []
    rec = _armed(kernel={200}, managed={200}, asked=asked, killed=killed, max_kills=0)
    rec.run_once()
    observed = rec.run_once()
    assert killed == [] and observed.would_kill == 1

    rec.set_max_kills(rr.DEFAULT_MAX_KILLS)
    armed = rec.run_once()
    assert killed == [200], "the same retained instance kills once the budget is restored"
    assert armed.killed == 1 and armed.would_kill == 0

    rec.set_max_kills(-5)
    assert rec._max_kills == 0, "a negative budget reads as observe-only, never as unbounded"


def test_the_kill_line_names_the_count_signalled_not_the_root_alone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """MUTATION TARGET: the kill log reports what the seam actually did.

    The seam's answer is a COUNT over the tree it walked: it signals every managed
    descendant it discovered and can still withhold the root's own signal. A line
    naming only the root reported one pid as killed twice an hour apart on a live
    host -- it was never signalled either time, and both counts came from a
    descendant -- and a reader chasing that could not tell it from a process that
    survived a kill.
    """
    asked: list[int] = []
    audited: list[tuple[int, str, str]] = []
    rec = _armed(kernel={200}, managed={200}, asked=asked, audited=audited)
    rec._kill_tree = lambda pid, expected=None: 3  # type: ignore[method-assign]
    rec.run_once()
    audited.clear()
    with caplog.at_level(logging.WARNING, logger="kiro_crew.runtime_reconcile"):
        rec.run_once()

    lines = [r.getMessage() for r in caplog.records]
    assert any(
        "signalled 3 process(es) in the tree at pid=200" in line for line in lines
    ), f"the line names the count and the tree; got {lines}"
    assert not any(
        "killed pid=200" in line for line in lines
    ), "and never claims the root itself was killed"
    killed_rows = [why for pid, outcome, why in audited if outcome == "killed"]
    assert (
        killed_rows and "3 signalled" in killed_rows[0]
    ), f"the audit carries the count too; got {killed_rows}"


def test_the_wiring_claims_pids_tracked_by_any_gateway_on_this_data_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION TARGET: membership read at the same scope as the kernel question.

    The agent slice is named from a hash of the data home, so a SECOND process on
    that home -- a ``kirocrew chat`` doing agent work in-process, or this gateway's
    own namespace-sandbox children whose tracked pid is the launcher parent -- puts
    live runtimes in the very slice this reconciler reads. Their records are filed
    under a different gateway pid, or in the descendant pid file, and the lease
    table is per-process memory that cannot see them at all.

    Asked only for THIS process's session entries, every one of them is unclaimed
    by construction, passes the inherited marker test, ages past the floor, and is
    signalled. So membership comes from both pid files across every gateway pid.
    """
    from kiro_crew import platform_compat, session_pid

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(session_pid, "config_dir", lambda: home)
    mine, other = os.getpid(), os.getpid() + 1
    # 5101 is ours; 5202 belongs to another gateway pid on the same home; 5303 is a
    # sandbox child, recorded only as a descendant in the other file.
    (home / "kiro_session_pids.txt").write_text(
        f"{mine}:5101:TOKEN-A\n{other}:5202:TOKEN-B\n", encoding="utf-8"
    )
    (home / "kiro_pids.txt").write_text(f"5303:{mine}\n", encoding="utf-8")
    monkeypatch.setattr(rr, "_mcp_backend_pids", lambda: set())
    # Patched on THIS module: the enumerator is a hoisted module-scope import here,
    # so patching its source module would leave this reference untouched and the
    # reconciler would read the host's real slice.
    monkeypatch.setattr(rr, "instance_slice_pids", lambda: {5101, 5202, 5303})
    monkeypatch.setattr(
        "kiro_crew.platform_compat.pid_liveness", lambda pid: platform_compat.PID_ALIVE
    )
    monkeypatch.setattr(session_pid, "_pid_start_token", lambda pid: None)

    killed: list[int] = []
    monkeypatch.setattr(session_pid, "_kill_pid_tree", lambda pid: (killed.append(pid) or 1, True))
    monkeypatch.setattr(session_pid, "_untrack_session_pid", lambda pid: True)

    reconciler = rr.build_reconciler(active_pids=lambda: set(), notify_dead=lambda pid: None)
    reading = reconciler.run_once()
    reconciler.run_once()

    assert reading.unowned_alive == 0, (
        "a pid any record on this data home claims is owned, whichever gateway "
        "filed it and whichever file it is in"
    )
    assert killed == [], "and nothing is signalled"


def test_an_incomplete_tracked_pid_snapshot_refuses_the_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION TARGET: the completeness requirement on membership.

    A partial read of the tracking files is the one input that makes a live runtime
    look unowned, so it refuses the pass rather than authorizing a kill on
    incomplete membership -- the same requirement the scope reaper imposes on every
    kill-authorizing caller.
    """
    from kiro_crew import session_pid

    monkeypatch.setattr(rr, "_mcp_backend_pids", lambda: set())
    monkeypatch.setattr(session_pid, "_session_pid_entry_index", lambda gw: {})
    monkeypatch.setattr(session_pid, "_read_tracked_agent_pids", lambda: ({5101}, False))
    monkeypatch.setattr(rr, "instance_slice_pids", lambda: {5101, 9999})

    reconciler = rr.build_reconciler(active_pids=lambda: set(), notify_dead=lambda pid: None)
    reading = reconciler.run_once()
    assert reading.supported is False
    assert "registry" in reading.reason, reading.reason


def test_a_foreign_gateways_dead_record_is_counted_but_not_claimed_retracted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION TARGET: carrying each record's OWNER, not just its identity.

    Membership spans every gateway's rows on this data home, so a row filed by a
    concurrent CLI or a predecessor gateway reaches the dead direction. The
    untracker matches on the CALLING process's own prefix and cannot remove such a
    row -- and an unchanged rewrite still returns True, so calling it would report a
    retraction that never happened, every pass, forever.
    """
    from kiro_crew import platform_compat, session_pid

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(session_pid, "config_dir", lambda: home)
    mine, foreign = os.getpid(), os.getpid() + 1
    (home / "kiro_session_pids.txt").write_text(
        f"{mine}:5001:TOK-A\n{foreign}:5002:TOK-B\n", encoding="utf-8"
    )
    monkeypatch.setattr(rr, "_mcp_backend_pids", lambda: set())
    monkeypatch.setattr(rr, "instance_slice_pids", lambda: set())
    monkeypatch.setattr(
        "kiro_crew.platform_compat.pid_liveness", lambda pid: platform_compat.PID_DEAD
    )
    untracked: list[str] = []
    real_retract = rr._retract_session_rows

    def _recording_retract(rows: object) -> None:
        # Records the call AND does the real work: a fake that only recorded would
        # leave the row in place, and the retraction count is an observed
        # disappearance, so it would report zero and hide what this test measures.
        untracked.extend(sorted(rows))  # type: ignore[arg-type]
        real_retract(rows)  # type: ignore[arg-type]

    monkeypatch.setattr(rr, "_retract_session_rows", _recording_retract)

    reading = rr.build_reconciler(
        active_pids=lambda: set(), notify_dead=lambda pid: None
    ).run_once()

    assert reading.owned_dead == 2, "both dead rows are real and both are counted"
    assert reading.forgotten == 1, "only the row this gateway can remove is claimed retracted"
    assert untracked == [
        f"{mine}:5001:TOK-A"
    ], f"only this gateway's own row is handed to the remover; {untracked}"


def test_the_entry_reader_carries_every_gateways_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSITIVE CONTROL for the reader: without it, a reader that silently returned
    nothing would satisfy the test above by making both rows invisible."""
    from kiro_crew import session_pid

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(session_pid, "config_dir", lambda: home)
    (home / "kiro_session_pids.txt").write_text(
        "111:5001:TOK-A\n222:5002\ngarbage\n333:5003:TOK-C\n", encoding="utf-8"
    )
    owners = rr._session_pid_entry_owners()
    # Owner, recorded identity AND the verbatim row. The row is the third thing the
    # reader owes its caller: a retraction is allowed to remove that exact line and
    # nothing else, because the untracker's own prefix match would also take out a
    # replacement's row under the same reused number.
    assert owners == {
        5001: (111, "TOK-A", "111:5001:TOK-A"),
        5002: (222, None, "222:5002"),
        5003: (333, "TOK-C", "333:5003:TOK-C"),
    }, owners


def test_the_reconcile_pass_reads_the_pid_union_on_the_loop_not_in_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTATION TARGET: the union is snapshotted on the loop before dispatch.

    ``_pool_pids`` drains the warm pool's ``asyncio.Queue`` with ``get_nowait``
    and puts every entry back, which is safe only on the loop that owns the queue.
    Read from the maintenance executor it races the refill check, and a refill
    that sees a momentarily empty pool spawns runtimes to fill one that was never
    empty -- so the union must be gathered here and handed over frozen.
    """
    from kiro_crew import session_cleanup as sc

    union_threads: list[int] = []
    pass_threads: list[int] = []
    seen: list[set[int]] = []

    class _Fake:
        def __init__(self, active_pids: Any) -> None:
            self._active_pids = active_pids

        def set_max_kills(self, budget: int) -> None:
            return None

        def run_once(self) -> rr.ReconcileReading:
            pass_threads.append(threading.get_ident())
            seen.append(self._active_pids())
            return rr.ReconcileReading(supported=True)

    monkeypatch.setattr(sc, "build_reconciler", lambda active_pids, notify_dead: _Fake(active_pids))
    cleanup, _recorded = _cleanup(candidates=[], active={4242}, union_threads=union_threads)

    asyncio.run(cleanup._reconcile_runtimes_hook())

    assert seen == [{4242}], f"the pass reads the snapshot this tick took; {seen}"
    assert union_threads == [
        threading.get_ident()
    ], f"the union is read on the calling thread only; {union_threads}"
    assert (
        pass_threads and pass_threads[0] != threading.get_ident()
    ), "and the pass itself really did run off the loop, or this proves nothing"


def test_an_incomplete_pid_union_skips_the_reconcile_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTATION TARGET: the completeness flag on the union is read, not discarded.

    The same rule the membership read inside the pass imposes on itself. A partial
    union makes a live runtime look unowned, which is the one input that turns this
    reconciler into the leak it exists to report.
    """
    from kiro_crew import session_cleanup as sc

    ran: list[int] = []

    class _Fake:
        def __init__(self, active_pids: Any) -> None:
            self._active_pids = active_pids

        def set_max_kills(self, budget: int) -> None:
            return None

        def run_once(self) -> rr.ReconcileReading:
            ran.append(1)
            return rr.ReconcileReading(supported=True)

    monkeypatch.setattr(sc, "build_reconciler", lambda active_pids, notify_dead: _Fake(active_pids))
    cleanup, _recorded = _cleanup(candidates=[], active={4242}, union_complete=False)

    asyncio.run(cleanup._reconcile_runtimes_hook())

    assert ran == [], "an incomplete union defers the whole pass to the next tick"


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (0, 0),
        (5, 5),
        # config.json is agent-writable and this value authorizes signals, so
        # anything that is not a plain non-negative int reads as observe-only.
        (-3, 0),
        (True, 0),
        ("5", 0),
        (None, 0),
    ],
)
def test_the_hook_hands_the_configured_kill_budget_to_every_pass(
    monkeypatch: pytest.MonkeyPatch, configured: object, expected: int
) -> None:
    """MUTATION TARGET: ``session.reconcile_max_kills`` reaches the pass.

    Without this the field is inert: the reconciler would keep whatever budget it was
    built with, and an operator arming it would see nothing change.
    """
    from kiro_crew import session_cleanup as sc

    budgets: list[int] = []

    class _Fake:
        def __init__(self, active_pids: Any) -> None:
            self._active_pids = active_pids

        def set_max_kills(self, budget: int) -> None:
            budgets.append(budget)

        def run_once(self) -> rr.ReconcileReading:
            return rr.ReconcileReading(supported=True)

    monkeypatch.setattr(sc, "build_reconciler", lambda active_pids, notify_dead: _Fake(active_pids))
    cleanup, _recorded = _cleanup(candidates=[], active={4242}, reconcile_max_kills=configured)

    asyncio.run(cleanup._reconcile_runtimes_hook())
    asyncio.run(cleanup._reconcile_runtimes_hook())

    assert budgets == [
        expected,
        expected,
    ], f"every pass adopts the current value, not just the first; got {budgets}"


def test_the_configured_budget_is_re_read_on_the_next_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write between two ticks reaches the second one.

    The reconciler instance is built once and kept, so this is the only thing that
    makes arming the budget take effect without a gateway restart.
    """
    from kiro_crew import session_cleanup as sc

    budgets: list[int] = []

    class _Fake:
        def __init__(self, active_pids: Any) -> None:
            self._active_pids = active_pids

        def set_max_kills(self, budget: int) -> None:
            budgets.append(budget)

        def run_once(self) -> rr.ReconcileReading:
            return rr.ReconcileReading(supported=True)

    monkeypatch.setattr(sc, "build_reconciler", lambda active_pids, notify_dead: _Fake(active_pids))
    cleanup, _recorded = _cleanup(candidates=[], active={4242}, reconcile_max_kills=0)

    asyncio.run(cleanup._reconcile_runtimes_hook())
    cleanup._owner._cfg.session.reconcile_max_kills = rr.DEFAULT_MAX_KILLS
    asyncio.run(cleanup._reconcile_runtimes_hook())

    assert budgets == [0, rr.DEFAULT_MAX_KILLS], f"the second tick sees the write; got {budgets}"


def test_the_real_wiring_defaults_to_the_shipped_budget() -> None:
    """POSITIVE CONTROL on :func:`build_reconciler`'s own default.

    A caller that does not thread the config through gets the budget the arm already
    shipped with, so adding the field changes no construction path's behaviour.
    """
    reconciler = rr.build_reconciler(active_pids=lambda: set(), notify_dead=lambda pid: None)
    assert reconciler._max_kills == rr.DEFAULT_MAX_KILLS == 5


def test_a_record_in_neither_tracking_file_is_counted_dead_but_not_claimed_retracted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION TARGET: a retraction is scoped to the source that holds the record.

    Membership is wider than the session file. A pid the manager's own union names,
    or one known only to the MCP backend pidfile, has no row in either tracking
    file -- so rewriting the session file removes nothing, and the rewrite still
    succeeds. Counting that as a retraction reports work that did not happen on
    every pass for as long as the record lives.
    """
    from kiro_crew import platform_compat, session_pid

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(session_pid, "config_dir", lambda: home)
    monkeypatch.setattr(rr, "_mcp_backend_pids", lambda: set())
    monkeypatch.setattr(rr, "instance_slice_pids", lambda: set())
    monkeypatch.setattr(
        "kiro_crew.platform_compat.pid_liveness", lambda pid: platform_compat.PID_DEAD
    )
    handed: list[int] = []
    monkeypatch.setattr(session_pid, "_untrack_session_pid", lambda pid: handed.append(pid) is None)

    reading = rr.build_reconciler(
        active_pids=lambda: {5701}, notify_dead=lambda pid: None
    ).run_once()

    assert reading.owned_dead == 1, "the record is real and the process is gone"
    assert reading.forgotten == 0, "but no row exists to remove, so nothing is retracted"
    assert handed == [], f"and the session untracker is never asked for it; {handed}"


def test_a_descendant_only_record_is_retracted_from_the_descendant_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION TARGET: each record is removed from ITS OWN source.

    This gateway's namespace-sandbox children are filed only in ``kiro_pids.txt``,
    because the pid that is tracked and leased is the launcher parent. Their record
    is not in the session file at all, so the session untracker cannot retract it.
    """
    from kiro_crew import platform_compat, session_pid

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(session_pid, "config_dir", lambda: home)
    pid_file = home / "kiro_pids.txt"
    pid_file.write_text(f"5501:{os.getpid()}:TOK-D\n5502:{os.getpid()}:TOK-E\n", encoding="utf-8")
    monkeypatch.setattr(rr, "_mcp_backend_pids", lambda: set())
    monkeypatch.setattr(rr, "instance_slice_pids", lambda: set())
    monkeypatch.setattr(
        "kiro_crew.platform_compat.pid_liveness",
        lambda pid: platform_compat.PID_DEAD if pid == 5501 else platform_compat.PID_ALIVE,
    )
    # The live sibling must also pass the recycle check, so its identity is pinned
    # to the token its row records. Left to the real probe, 5502 can answer with a
    # STRANGER's start time: Windows ignores the low two bits of a pid on
    # ``OpenProcess``, so 5502 opens whatever process holds pid 5500 on the runner
    # and the reconciler retracts the sibling's row as recycled.
    monkeypatch.setattr(
        session_pid, "_pid_start_token", lambda pid: {5501: "TOK-D", 5502: "TOK-E"}.get(pid)
    )

    reading = rr.build_reconciler(
        active_pids=lambda: set(), notify_dead=lambda pid: None
    ).run_once()

    assert (
        reading.owned_dead == 1 and reading.forgotten == 1
    ), f"the dead descendant's own row is removed; {reading.as_counter_fields()}"
    rows = pid_file.read_text(encoding="utf-8")
    assert "5501" not in rows, f"the row is gone from the file that held it; {rows!r}"
    assert "5502" in rows, f"and the live sibling's row is untouched; {rows!r}"


def test_a_legacy_bare_descendant_row_is_retracted_by_the_bare_line_remover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two row grammars in one file need different removers, and the remover for
    a parented row matches on its child field -- which a bare line has no form of."""
    from kiro_crew import platform_compat, session_pid

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(session_pid, "config_dir", lambda: home)
    pid_file = home / "kiro_pids.txt"
    pid_file.write_text("5601\n", encoding="utf-8")
    monkeypatch.setattr(rr, "_mcp_backend_pids", lambda: set())
    monkeypatch.setattr(rr, "instance_slice_pids", lambda: set())
    monkeypatch.setattr(
        "kiro_crew.platform_compat.pid_liveness", lambda pid: platform_compat.PID_DEAD
    )

    reading = rr.build_reconciler(
        active_pids=lambda: set(), notify_dead=lambda pid: None
    ).run_once()

    assert reading.forgotten == 1, f"the legacy row is retracted too; {reading.as_counter_fields()}"
    assert "5601" not in pid_file.read_text(encoding="utf-8")


def test_a_session_row_the_remover_left_in_place_is_not_claimed_retracted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION TARGET: the answer is an observed disappearance, not the remover's
    return value.

    No remover of these files reports whether a row MATCHED -- each reports whether
    its rewrite succeeded, which an unchanged rewrite also does. So a row still
    present after the retraction must count as nothing removed.
    """
    from kiro_crew import platform_compat, session_pid

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(session_pid, "config_dir", lambda: home)
    (home / "kiro_session_pids.txt").write_text(f"{os.getpid()}:5801:TOK-F\n", encoding="utf-8")
    monkeypatch.setattr(rr, "_mcp_backend_pids", lambda: set())
    monkeypatch.setattr(rr, "instance_slice_pids", lambda: set())
    monkeypatch.setattr(
        "kiro_crew.platform_compat.pid_liveness", lambda pid: platform_compat.PID_DEAD
    )
    # Returns nothing and changes nothing -- the shape of a rewrite that matched no
    # row, which is indistinguishable from a successful one by its answer alone.
    monkeypatch.setattr(rr, "_retract_session_rows", lambda rows: None)

    reading = rr.build_reconciler(
        active_pids=lambda: set(), notify_dead=lambda pid: None
    ).run_once()

    assert reading.owned_dead == 1
    assert (
        reading.forgotten == 0
    ), f"the row survived, so nothing is counted; {reading.as_counter_fields()}"


def test_the_descendant_row_reader_names_only_a_pids_own_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSITIVE CONTROL for the reader: without it, a reader that returned nothing
    would satisfy the tests above by making every descendant record invisible.

    A pid appearing only as the PARENT field is not a record of itself -- that is the
    field the reapers deliberately ignore, and the field no remover matches on.
    """
    from kiro_crew import session_pid

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(session_pid, "config_dir", lambda: home)
    (home / "kiro_pids.txt").write_text(
        "5901:7001:TOK-G\n5902:7002\n5903\ngarbage\n\n", encoding="utf-8"
    )
    rows = rr._descendant_pid_rows()
    assert rows == {
        5901: ("5901:7001:TOK-G",),
        5902: ("5902:7002",),
        5903: ("5903",),
    }, f"each row is kept VERBATIM, since the pid alone does not identify it; {rows}"
    assert 7001 not in rows and 7002 not in rows, f"a parent field is not its own record; {rows}"


def test_a_pid_carrying_two_rows_keeps_both_so_neither_is_retracted_by_the_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION TARGET: the reader accumulates a pid's rows instead of keeping one.

    The tracking file dedupes an append on the ``child:parent`` prefix, not on the
    child, so one pid legitimately carries a row per parent. A reader that kept only
    the last would hand the retraction a set that omits rows it inspected.
    """
    from kiro_crew import session_pid

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(session_pid, "config_dir", lambda: home)
    (home / "kiro_pids.txt").write_text("6101:7101:TOK-H\n6101:7102:TOK-I\n", encoding="utf-8")

    rows = rr._descendant_pid_rows()

    assert rows == {
        6101: ("6101:7101:TOK-H", "6101:7102:TOK-I")
    }, f"both of the pid's rows are named; {rows}"


def test_a_replacement_row_under_a_reused_pid_survives_the_dead_rows_retraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION TARGET: the retraction removes the ROWS the pass captured.

    The pid number is reused. A replacement descendant registers its own row under
    the same number at any point after the snapshot -- the concurrent writers do not
    coordinate with this pass -- and a pid-keyed removal takes that row out with the
    dead one. The live descendant would then be tracked in neither file: leaked past
    every reaper that keys off it, and read as unowned by the next pass.
    """
    from kiro_crew import platform_compat, session_pid

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(session_pid, "config_dir", lambda: home)
    pid_file = home / "kiro_pids.txt"
    dead_row = f"6201:{os.getpid()}:TOK-DEAD"
    pid_file.write_text(dead_row + "\n", encoding="utf-8")
    monkeypatch.setattr(rr, "_mcp_backend_pids", lambda: set())
    monkeypatch.setattr(rr, "instance_slice_pids", lambda: set())

    replacement = f"6201:{os.getpid()}:TOK-LIVE"

    def register_replacement_mid_pass(pid: int) -> int:
        """The concurrent writer, landing after the snapshot and before the removal.

        The liveness probe is the pass's last read before it retracts, so appending
        here puts the replacement's row in the file at exactly the moment the race
        needs it.
        """
        if pid == 6201 and replacement not in pid_file.read_text(encoding="utf-8"):
            with pid_file.open("a", encoding="utf-8") as handle:
                handle.write(replacement + "\n")
        return platform_compat.PID_DEAD

    monkeypatch.setattr("kiro_crew.platform_compat.pid_liveness", register_replacement_mid_pass)

    reading = rr.build_reconciler(
        active_pids=lambda: set(), notify_dead=lambda pid: None
    ).run_once()

    rows = pid_file.read_text(encoding="utf-8")
    assert replacement in rows, f"the live replacement's row is still tracked; {rows!r}"
    assert dead_row not in rows, f"and the dead record it replaced is gone; {rows!r}"
    assert reading.forgotten == 1, f"the captured row did disappear; {reading.as_counter_fields()}"


# ── the sweep gates ──────────────────────────────────────────────────────────


class _Session:
    def __init__(self, pid: int | None = None) -> None:
        self.provider = object()
        self.semaphore = asyncio.BoundedSemaphore(1)
        self.last_used = 0.0
        self._pid = pid


class _Owner:
    """The slice of ``SessionManager`` the cleanup service reaches through."""

    def __init__(self) -> None:
        self._cfg = _Cfg()
        self._sessions: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._draining_bg_runtimes: list[Any] = []
        self._bg_runtime_lock = asyncio.Lock()
        self.on_session_expire = None
        self.on_stuck_turn = None
        self.recycled: list[tuple[str, str]] = []

    def get_pid(self, key: str) -> int | None:
        return getattr(self._sessions.get(key), "_pid", None)

    def _pool_pids(self) -> set[int]:
        return set()

    def _in_flight_pids(self) -> set[int]:
        return set()

    def _companion_runtime_pids(self) -> set[int]:
        return set()

    async def _fire_recycle_callback(self, key: str, *, reason: str) -> None:
        self.recycled.append((key, reason))

    async def _cleanup_loop(self) -> None:  # pragma: no cover - not driven here
        return None

    async def _expire_idle(self, timeout_secs: int) -> None:  # pragma: no cover
        return None

    async def _reap_drained_bg_runtimes_locked(self) -> None:  # pragma: no cover
        return None

    async def _reap_idle_stale_bg_runtime(self) -> bool:  # pragma: no cover
        return False

    async def reset(self, key: str, **kwargs: Any) -> bool:  # pragma: no cover
        return False


class _SessionCfg:
    timeout_secs = 0
    watchdog_rss_max_mb = 0
    #: 0, not the shipped default: these fixtures assert what the HOOK hands over,
    #: so the value has to be one no default could produce by accident.
    reconcile_max_kills = 0


class _Cfg:
    def __init__(self) -> None:
        # Per instance, not a shared class attribute: the fixture writes
        # ``reconcile_max_kills`` on it, and one shared object would carry a test's
        # kill budget into every test that ran after it.
        self.session = _SessionCfg()


def _cleanup(
    *,
    candidates: list[int],
    mcp_candidates: list[int] | None = None,
    active: set[int] | None = None,
    union_complete: bool = True,
    union_threads: list[int] | None = None,
    reconcile_max_kills: object = 0,
) -> tuple[Any, dict[str, list[int]]]:
    """A ``SessionCleanup`` whose sweeps report *candidates* and record kills.

    *union_threads* records the thread each active-pid read ran on, which is how
    the reconcile hook's snapshot-on-the-loop rule is checked.

    *reconcile_max_kills* is what ``session.reconcile_max_kills`` holds for this
    service, as a raw object rather than an int: the hook reads it off a config file
    an agent can write, so the tests need to hand it the values a hand edit produces.
    """
    from kiro_crew.session_cleanup import CleanupDeps, CleanupState, SessionCleanup
    from kiro_crew.watchdog import SessionWatchdog

    recorded: dict[str, list[int]] = {"pid_kills": [], "mcp_kills": []}
    executor = ThreadPoolExecutor(max_workers=1)

    class _Shutdown:
        def is_set(self) -> bool:
            return True

        def wait(self) -> Any:
            fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
            return fut

    def kill_confirmed(gateway_pid: int, confirmed: list[int], dead: set[str]) -> int:
        recorded["pid_kills"].extend(confirmed)
        return len(confirmed)

    def kill_mcps(pids: list[int]) -> int:
        recorded["mcp_kills"].extend(pids)
        return len(pids)

    def collect_active(sessions: Any) -> tuple[set[int], bool]:
        if union_threads is not None:
            union_threads.append(threading.get_ident())
        return set(active or set()), union_complete

    deps = CleanupDeps(
        logger=logging.getLogger("test.w4"),
        get_shutdown_signal=_Shutdown,
        get_maintenance_executor=lambda: executor,
        get_subprocess_executor=lambda: executor,
        cleanup_orphaned_mcp_servers=lambda: 0,
        cleanup_orphaned_session_roots=lambda: 0,
        cleanup_stale_sandbox_profiles=lambda: 0,
        prune_session_pid_mappings=lambda: 0,
        prune_member_pid_bindings=lambda: 0,
        rotate_shell_audit_log=lambda: False,
        prune_pycache=lambda: (0, 0),
        collect_active_pids=collect_active,
        periodic_pid_sweep=lambda gw, pids: (set(), list(candidates)),
        kill_confirmed_and_writeback=kill_confirmed,
        find_orphan_mcp_candidates=lambda pids: list(
            candidates if mcp_candidates is None else mcp_candidates
        ),
        kill_orphan_mcps=kill_mcps,
        reap_agent_scopes=lambda pids: None,
        build_child_map=dict,
        rss_mb_from_tree=lambda pid, child_map: 0,
        get_session_rss_mb=lambda pid: 0,
        is_windows=lambda: False,
        getpid=lambda: 1,
        monotonic=lambda: 0.0,
        stats_factory=lambda: _Stats(),
        sel_factory=lambda: _Sel(),
        provider_has_active_turn=lambda provider: False,
        emit_counter=lambda event, dims: None,
        get_persistent_keys=frozenset,
        get_channel_prefix=lambda: "channel:",
        get_stuck_turn_report_secs=lambda: 1e9,
        get_pycache_gc_interval_secs=lambda: 1e9,
        get_session_idle_expired_event=lambda: "idle",
    )
    state = CleanupState(watchdog=SessionWatchdog([]))
    owner = _Owner()
    owner._cfg.session.reconcile_max_kills = reconcile_max_kills
    return SessionCleanup(owner, deps, state=state), recorded


class _Stats:
    def inc_session_cleaned(self) -> None:
        return None


class _Sel:
    def log_api_access(self, **kwargs: Any) -> None:
        return None


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


class _LeaseHolder:
    """One session's claim on a runtime with a given pid."""

    def __init__(self, pid: int) -> None:
        self._runtime = _PidRuntime(pid)
        self._lease: str | None = None

    async def take(self) -> None:
        acquisition = await ro.RUNTIME_OWNERSHIP.acquire(
            ("test", self._runtime.pid),
            f"session:{self._runtime.pid}",
            self._spawn,
        )
        self._lease = acquisition.lease

    @property
    def lease(self) -> str:
        """The lease id, for a test that must hand it to a provider stand-in."""
        assert self._lease is not None, "take() first"
        return self._lease

    async def give_back(self) -> None:
        if self._lease is not None:
            await ro.RUNTIME_OWNERSHIP.release(self._lease)
            self._lease = None

    async def _spawn(self) -> Any:
        return self._runtime


class _PidRuntime:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def is_alive(self) -> bool:
        return True


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
    (scope / "cgroup.procs").write_text("7171\n7272\n", encoding="utf-8")

    stopped: list[str] = []
    signalled: list[tuple[int, int]] = []
    attributed: list[int] = []

    def signal_owned(
        pid: int, sig: int, members: list[int], scope_dir: Path, proc_root: Path
    ) -> tuple[bool, str]:
        signalled.append((pid, sig))
        return True, ""

    refusals: list[str] = []
    holder = _LeaseHolder(7171)
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
    (scope / "cgroup.procs").write_text("7171\n7272\n", encoding="utf-8")
    signalled: list[int] = []
    reap._reclaim_scope(
        scope,
        "run-plain.scope",
        proc_root=tmp_path,
        stop_unit=lambda unit: True,
        signal_owned=lambda pid, sig, members, d, p: (signalled.append(pid) or (True, "")),
        sleep=lambda secs: None,
    )
    assert set(signalled) == {7171, 7272}


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


@pytest.fixture()
def agent_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point subagent persistence at a registry below this test's temp directory."""
    root = tmp_path / "subagents"
    root.mkdir()
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", root)
    return root


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


# ── the retraction removes the row, never the number ──────────────────────────


def test_a_replacements_session_row_survives_the_dead_records_retraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION TARGET: retracting the CAPTURED session row, not the pid.

    ``session_pid._untrack_session_pid`` drops every line whose ``<gw>:<pid>``
    prefix matches, at any start token, and the decision to retract is made from a
    snapshot taken before the pass's own awaits. A pid is reused: a teardown can
    remove the dead row and a replacement runtime can track itself under the same
    number in that window. A pid-wide removal then takes the replacement's row out
    with the dead one, and its LIVE runtime is tracked nowhere -- past every reaper
    that keys off this file, and read as unowned by the next pass.
    """
    from kiro_crew import platform_compat, session_pid

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(session_pid, "config_dir", lambda: home)
    gw = os.getpid()
    session_file = home / "kiro_session_pids.txt"
    # What the pass reads: the dead record, under its own recorded identity.
    session_file.write_text(f"{gw}:7101:DEAD-TOKEN\n", encoding="utf-8")
    monkeypatch.setattr(rr, "_mcp_backend_pids", lambda: set())
    monkeypatch.setattr(rr, "instance_slice_pids", lambda: set())
    monkeypatch.setattr(
        "kiro_crew.platform_compat.pid_liveness", lambda pid: platform_compat.PID_DEAD
    )

    real_retract = rr._retract_session_rows

    def _replacement_lands_first(rows: object) -> None:
        # The window this guard exists for: between the snapshot and the retraction a
        # replacement runtime took the same number and appended its own row.
        session_file.write_text(
            f"{gw}:7101:DEAD-TOKEN\n{gw}:7101:LIVE-REPLACEMENT\n", encoding="utf-8"
        )
        real_retract(rows)  # type: ignore[arg-type]

    monkeypatch.setattr(rr, "_retract_session_rows", _replacement_lands_first)

    reading = rr.build_reconciler(
        active_pids=lambda: set(), notify_dead=lambda pid: None
    ).run_once()

    surviving = session_file.read_text(encoding="utf-8")
    assert "LIVE-REPLACEMENT" in surviving, (
        "the replacement's row must survive a retraction aimed at the dead record; "
        f"file now {surviving!r}"
    )
    assert "DEAD-TOKEN" not in surviving, f"the dead row is gone; {surviving!r}"
    assert reading.forgotten == 1, reading.as_counter_fields()


def test_the_session_row_remover_matches_whole_lines_in_both_grammars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSITIVE CONTROL for the remover: it must remove what it is given, and only that.

    Without this, a remover that quietly removed nothing would satisfy the guard
    above by leaving every row in place.
    """
    from kiro_crew import session_pid

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(session_pid, "config_dir", lambda: home)
    session_file = home / "kiro_session_pids.txt"
    session_file.write_text("111:7201:TOK\n111:7201\n222:7202:TOK\n", encoding="utf-8")

    rr._retract_session_rows(["111:7201:TOK", "111:7201"])

    left = session_file.read_text(encoding="utf-8").split()
    assert left == ["222:7202:TOK"], left


def test_a_recycled_descendant_with_no_session_row_counts_as_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION TARGET: the recycle check reads BOTH files' recorded identities.

    A namespace-sandbox child has a descendant row and no session row at all,
    because the pid that is tracked and leased is the launcher parent. Comparing
    only the session file leaves such a pid ``owned_alive`` after its number is
    reused: its dead row is never retracted, so a record goes on naming somebody
    else's process for the gateway's life.
    """
    from kiro_crew import platform_compat, session_pid

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(session_pid, "config_dir", lambda: home)
    (home / "kiro_pids.txt").write_text("7301:4242:DESCENDANT-AS-RECORDED\n", encoding="utf-8")
    monkeypatch.setattr(session_pid, "_pid_start_token", lambda pid: "SOMEBODY-ELSE-ENTIRELY")
    monkeypatch.setattr(rr, "_mcp_backend_pids", lambda: set())
    monkeypatch.setattr(rr, "instance_slice_pids", lambda: set())
    monkeypatch.setattr(
        "kiro_crew.platform_compat.pid_liveness", lambda pid: platform_compat.PID_ALIVE
    )

    reading = rr.build_reconciler(
        active_pids=lambda: set(), notify_dead=lambda pid: None
    ).run_once()

    assert reading.owned_dead == 1, (
        "a live pid whose only recorded identity is a descendant row that disagrees "
        f"is a stranger; {reading.as_counter_fields()}"
    )
    assert reading.owned_alive == 0, reading.as_counter_fields()
    assert reading.forgotten == 1, "and its stale row is retracted"


def test_a_descendant_row_whose_identity_still_matches_is_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The subtractive half: one recorded identity that MATCHES means ours is answering.

    Without this, reading both files could become "any disagreement is a stranger",
    which would retract the record of a live runtime whose other row is stale.
    """
    from kiro_crew import platform_compat, session_pid

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(session_pid, "config_dir", lambda: home)
    # Two rows for one pid -- the file's append dedupes on child:parent, so a pid
    # legitimately carries one per parent. One is stale, one is current.
    (home / "kiro_pids.txt").write_text(
        "7401:4242:AN-OLDER-START\n7401:4343:STILL-RUNNING\n", encoding="utf-8"
    )
    monkeypatch.setattr(session_pid, "_pid_start_token", lambda pid: "STILL-RUNNING")
    monkeypatch.setattr(rr, "_mcp_backend_pids", lambda: set())
    monkeypatch.setattr(rr, "instance_slice_pids", lambda: set())
    monkeypatch.setattr(
        "kiro_crew.platform_compat.pid_liveness", lambda pid: platform_compat.PID_ALIVE
    )

    reading = rr.build_reconciler(
        active_pids=lambda: set(), notify_dead=lambda pid: None
    ).run_once()

    assert reading.owned_alive == 1, reading.as_counter_fields()
    assert reading.owned_dead == 0, reading.as_counter_fields()


# ── the notification memory is identity-keyed, pruned and bounded ─────────────


def test_a_recycled_pids_death_is_announced_to_its_own_holder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTATION TARGET: keying the notification memory on pid AND identity.

    The notice is owed once per dead runtime, not once per NUMBER. Keyed on the pid
    alone, once pid P has been reported dead the next session whose runtime lands on
    P and dies gets no notice at all and learns only from its own turn timeout --
    the two sibling memories in this class are identity-keyed for exactly this
    reason.
    """
    notified: list[int] = []
    identity = {"now": "FIRST-PROCESS"}

    reconciler = rr.RuntimeReconciler(
        slice_pids=lambda: set(),
        recorded_pids=lambda: {7501},
        is_alive=lambda pid: True,
        identity_of=lambda pid: identity["now"],
        was_recycled=lambda pid: True,  # a live stranger on the number
        kill_tree=lambda pid, expected=None: 0,
        forget=lambda pid: "not-mine",
        notify_dead=lambda pid: notified.append(pid),
    )

    reconciler.run_once()
    reconciler.run_once()
    assert notified == [7501], f"the same dead runtime is announced once; {notified}"

    identity["now"] = "THE-REPLACEMENT"
    reconciler.run_once()
    assert notified == [7501, 7501], (
        "a different process on the same number is a different death and is owed its "
        f"own notice; {notified}"
    )


def test_the_notification_memory_is_pruned_against_the_recorded_population() -> None:
    """MUTATION TARGET: the per-pass prune.

    A runtime whose record is gone can never be rediscovered as dead, so its entry
    can only grow a memory nothing removes from. The prune is what keeps this bounded
    by the slice rather than by the gateway's uptime.
    """
    recorded = {7601}
    reconciler = rr.RuntimeReconciler(
        slice_pids=lambda: set(),
        recorded_pids=lambda: set(recorded),
        is_alive=lambda pid: False,
        identity_of=lambda pid: None,
        kill_tree=lambda pid, expected=None: 0,
        forget=lambda pid: "not-mine",
        notify_dead=lambda pid: None,
    )
    reconciler.run_once()
    assert len(reconciler._notified_dead) == 1

    recorded.clear()
    reconciler.run_once()
    assert reconciler._notified_dead == {}, (
        "an entry for a pid no record claims any more is dropped; " f"{reconciler._notified_dead}"
    )


def test_the_notification_memory_bound_evicts_and_counts_the_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTATION TARGET: the named bound and its overflow accounting.

    The prune normally holds this down; the bound is the backstop for a population
    that churns faster than the prune reads it. An eviction means a holder can be
    told twice about one death, so it is COUNTED rather than silent.
    """
    monkeypatch.setattr(rr, "MAX_NOTIFIED_DEAD", 3)
    pids = {7701, 7702, 7703, 7704, 7705}
    reconciler = rr.RuntimeReconciler(
        slice_pids=lambda: set(),
        recorded_pids=lambda: set(pids),
        is_alive=lambda pid: False,
        identity_of=lambda pid: None,
        kill_tree=lambda pid, expected=None: 0,
        forget=lambda pid: "not-mine",
        notify_dead=lambda pid: None,
    )
    reconciler.run_once()

    assert len(reconciler._notified_dead) == 3, reconciler._notified_dead
    assert reconciler._notified_dead_overflow == 2, (
        "every eviction is counted, because each one costs a duplicate notice; "
        f"{reconciler._notified_dead_overflow}"
    )


# ── "not mine to retract" is a steady state, not a fault ─────────────────────


def test_a_record_that_is_not_ours_to_retract_is_not_warned_about_every_pass(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """MUTATION TARGET: the three-valued retraction answer.

    A row owned by another gateway, and a pid known only to the MCP pidfile or to
    the manager's in-memory union, have no row this pass can remove and never will.
    Folding them into the same answer a real failure gets publishes one WARNING per
    stale pid per cleanup tick for as long as the gateway runs -- a steady state
    reported as a fault.
    """
    reconciler = rr.RuntimeReconciler(
        slice_pids=lambda: set(),
        recorded_pids=lambda: {7801},
        is_alive=lambda pid: False,
        identity_of=lambda pid: None,
        kill_tree=lambda pid, expected=None: 0,
        forget=lambda pid: "not-mine",
        notify_dead=lambda pid: None,
    )
    with caplog.at_level(logging.DEBUG, logger="kiro_crew.runtime_reconcile"):
        reconciler.run_once()

    warnings = [
        r for r in caplog.records if r.levelno >= logging.WARNING and "7801" in r.getMessage()
    ]
    assert (
        warnings == []
    ), f"not ours to retract is not a warning; {[r.getMessage() for r in warnings]}"
    debugs = [r for r in caplog.records if "not ours to retract" in r.getMessage()]
    assert debugs, "but it is still recorded, at debug"


def test_a_retraction_that_was_ours_and_did_not_land_is_still_warned_about(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other side: ``failed`` is the answer worth waking somebody for.

    Without this, making "not mine" quiet could become making every failed
    retraction quiet, which is how a real leak stops being visible.
    """
    reconciler = rr.RuntimeReconciler(
        slice_pids=lambda: set(),
        recorded_pids=lambda: {7802},
        is_alive=lambda pid: False,
        identity_of=lambda pid: None,
        kill_tree=lambda pid, expected=None: 0,
        forget=lambda pid: "failed",
        notify_dead=lambda pid: None,
    )
    with caplog.at_level(logging.DEBUG, logger="kiro_crew.runtime_reconcile"):
        reconciler.run_once()

    warnings = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "could not be retracted" in r.getMessage()
    ]
    assert warnings, "a retraction this pass owed and did not land stays a warning"


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
    (scope / "cgroup.procs").write_text("8282\n8181\n", encoding="utf-8")

    stopped: list[str] = []
    signalled: list[tuple[int, int]] = []
    refusals: list[str] = []
    committed: list[int] = []

    def signal_owned(
        pid: int, sig: int, members: list[int], scope_dir: Path, proc_root: Path
    ) -> tuple[bool, str]:
        signalled.append((pid, sig))
        return True, ""

    # A tenancy and no lease -- the state the mechanism exists for.
    claim = ro.claim_runtime_tenancy(8181, holder="a shared sub-agent turn")
    assert claim is not None, "precondition: the tenancy was taken"
    assert ro.outstanding_leases(8181) == 0, "precondition: and no lease is held"
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
    (scope / "cgroup.procs").write_text("8383\n8484\n", encoding="utf-8")

    stopped: list[str] = []
    refusals: list[str] = []
    released: list[int] = []
    real_release = reap.release_runtime_teardown

    def _late_tenant(pid: int, epoch: int) -> bool:
        # The second member is claimed between the survey and its own commit.
        return pid != 8484

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
    assert released == [8383], (
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
    procs.write_text("8585\n8686\n", encoding="utf-8")

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
    assert sorted(released) == [8585, 8686], f"every barrier taken is dropped; {released}"
    # Released in a finally: the table is a process-wide singleton, and an int target
    # has no liveness probe, so a discarded handle stays live for the worker's whole
    # life and refuses every later barrier on this pid -- in this file and in others.
    proof = ro.claim_runtime_tenancy(8585, holder="a later turn")
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
    is caused and surfaces as an order-dependent failure somewhere else -- pid 8585 is
    also the subject of ``test_cron_reaper.py``, which never resets the table.

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
    for pid in (8181, 8585, 5811, 424242):
        assert ro.RUNTIME_TENANCY.claims_on_pid(pid) == 0, (
            f"pid {pid} still carries a claim from an earlier test in this file, which "
            "refuses every later barrier on it"
        )


def _cleanup_with_clock(
    clock: list[float],
    *,
    logger: logging.Logger,
) -> Any:
    """A reconcile hook harness whose ``build_reconciler`` and clock the caller drives.

    Returns the ``SessionCleanup`` with a mutable ``clock`` (its ``monotonic``
    reads ``clock[0]``) and a named ``logger`` a ``caplog`` fixture can capture,
    so the warn-once ledger for a refused pass can be exercised across ticks.
    """
    import dataclasses

    cleanup, _recorded = _cleanup(candidates=[], active={4242})
    cleanup._deps = dataclasses.replace(cleanup._deps, monotonic=lambda: clock[0], logger=logger)
    return cleanup


def _drive_reconcile(cleanup: Any, monkeypatch: pytest.MonkeyPatch, reading: Any) -> None:
    """Run one reconcile hook tick whose pass returns *reading*."""
    from kiro_crew import session_cleanup as sc

    class _Fake:
        def __init__(self, active_pids: Any) -> None:
            self._active_pids = active_pids

        def set_max_kills(self, budget: int) -> None:
            return None

        def run_once(self) -> Any:
            return reading

    monkeypatch.setattr(sc, "build_reconciler", lambda active_pids, notify_dead: _Fake(active_pids))
    # The hook retains its reconciler across ticks (its two-pass memory). Each
    # driven tick wants its OWN reading, so drop the retained one first.
    cleanup.state.runtime_reconciler = None
    asyncio.run(cleanup._reconcile_runtimes_hook())


def test_a_refused_pass_is_reported_at_warning_not_only_debug(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """MUTATION TARGET: a refused reconcile pass surfaces above debug.

    ``supported=False`` reclaims nothing and publishes no counts. Reported only at
    debug, the reconciler goes silently inert while its source stays unreadable --
    the defect this fixes. The fact must reach WARNING.
    """
    logger = logging.getLogger("test.reconcile.refusal.warn")
    clock = [100.0]
    cleanup = _cleanup_with_clock(clock, logger=logger)

    with caplog.at_level(logging.WARNING, logger=logger.name):
        _drive_reconcile(
            cleanup,
            monkeypatch,
            rr.ReconcileReading(supported=False, reason="cannot read the registry: boom"),
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a refused pass must warn, not stay at debug"
    assert "cannot read the registry: boom" in warnings[0].getMessage()
    assert cleanup.state.reconcile_refusal_reason == "cannot read the registry: boom"


def test_a_persistent_refusal_warns_once_not_every_tick(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """MUTATION TARGET: the re-warn floor is honored while the reason is unchanged.

    The reconciler ticks on the cleanup cadence, so a condition that persists for
    minutes must not write one WARNING per tick -- that is the noise the floor
    exists to prevent.
    """
    logger = logging.getLogger("test.reconcile.refusal.once")
    clock = [0.0]
    cleanup = _cleanup_with_clock(clock, logger=logger)
    same = rr.ReconcileReading(supported=False, reason="the tracked-pid snapshot is incomplete")

    with caplog.at_level(logging.WARNING, logger=logger.name):
        _drive_reconcile(cleanup, monkeypatch, same)
        clock[0] = 60.0  # well within RECONCILE_REFUSAL_WARN_INTERVAL_SECS
        _drive_reconcile(cleanup, monkeypatch, same)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"a steady refusal warns once, not per tick; {warnings}"


def test_a_new_refusal_reason_re_warns_at_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """MUTATION TARGET: a CHANGE of reason bypasses the re-warn floor.

    A different unreadable source is a different event and must not be swallowed by
    a floor armed for the previous one.
    """
    logger = logging.getLogger("test.reconcile.refusal.newreason")
    clock = [0.0]
    cleanup = _cleanup_with_clock(clock, logger=logger)

    with caplog.at_level(logging.WARNING, logger=logger.name):
        _drive_reconcile(
            cleanup, monkeypatch, rr.ReconcileReading(supported=False, reason="reason A")
        )
        clock[0] = 1.0  # far inside the floor
        _drive_reconcile(
            cleanup, monkeypatch, rr.ReconcileReading(supported=False, reason="reason B")
        )

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(messages) == 2, f"a new reason warns immediately; {messages}"
    assert any("reason A" in m for m in messages) and any("reason B" in m for m in messages)


def test_a_recovered_pass_logs_recovery_and_re_arms(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """MUTATION TARGET: a supported pass after a refusal clears the ledger and re-arms.

    Without the clear the first outage after boot consumes the only WARNING, and a
    later outage inside the floor is silent -- the original defect in a subtler
    form.
    """
    logger = logging.getLogger("test.reconcile.refusal.recover")
    clock = [0.0]
    cleanup = _cleanup_with_clock(clock, logger=logger)

    with caplog.at_level(logging.WARNING, logger=logger.name):
        _drive_reconcile(
            cleanup, monkeypatch, rr.ReconcileReading(supported=False, reason="reason A")
        )
        clock[0] = 1.0
        # A supported pass: recovery logged, ledger cleared.
        _drive_reconcile(cleanup, monkeypatch, rr.ReconcileReading(supported=True))
        assert cleanup.state.reconcile_refusal_reason is None, "the ledger is cleared on recovery"
        clock[0] = 2.0
        # A fresh outage inside the old floor window re-warns immediately.
        _drive_reconcile(
            cleanup, monkeypatch, rr.ReconcileReading(supported=False, reason="reason A")
        )

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("resumed reclaiming" in m for m in messages), f"recovery is announced; {messages}"
    # First refusal + recovery + second refusal = 3 WARNING lines despite the floor.
    assert len(messages) == 3, f"the re-arm lets the next outage warn at once; {messages}"


def test_a_supported_pass_with_no_prior_refusal_logs_no_recovery(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A steady healthy reconciler is silent: recovery fires only after a refusal."""
    logger = logging.getLogger("test.reconcile.refusal.quiet")
    clock = [0.0]
    cleanup = _cleanup_with_clock(clock, logger=logger)

    with caplog.at_level(logging.WARNING, logger=logger.name):
        _drive_reconcile(cleanup, monkeypatch, rr.ReconcileReading(supported=True))

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert messages == [], f"a healthy pass says nothing; {messages}"


def test_an_incomplete_union_skip_surfaces_through_the_same_warn_once_ledger(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """MUTATION TARGET: the hook's OTHER silent-inert skip is not left at debug.

    The incomplete-active-pid-union skip produces no reading and publishes no
    counts, exactly as a ``run_once`` refusal does, so it must go through the same
    warn-once ledger rather than a bare debug line -- otherwise the boundary covers
    only ``run_once`` refusals, not every path that leaves the reconciler inert. It
    is its own reason, so a distinct transition, and it recovers on the next
    complete pass.
    """
    import dataclasses

    from kiro_crew import session_cleanup as sc

    logger = logging.getLogger("test.reconcile.refusal.union")
    clock = [0.0]
    # union_complete=False makes the hook take the incomplete-union skip before it
    # ever builds or calls a reconciler.
    cleanup, _recorded = _cleanup(candidates=[], active={4242}, union_complete=False)
    cleanup._deps = dataclasses.replace(cleanup._deps, monotonic=lambda: clock[0], logger=logger)

    class _Fake:
        def __init__(self, active_pids: Any) -> None:
            self._active_pids = active_pids

        def set_max_kills(self, budget: int) -> None:
            return None

        def run_once(self) -> rr.ReconcileReading:
            raise AssertionError("the pass must not run while the union is incomplete")

    monkeypatch.setattr(sc, "build_reconciler", lambda active_pids, notify_dead: _Fake(active_pids))

    with caplog.at_level(logging.WARNING, logger=logger.name):
        asyncio.run(cleanup._reconcile_runtimes_hook())

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "the incomplete-union skip must warn, not stay at debug"
    assert "incomplete" in warnings[0].getMessage()
    assert cleanup.state.reconcile_refusal_reason == "the active-pid union is incomplete"
