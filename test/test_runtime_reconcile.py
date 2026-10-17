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

A finished-result signal sits beside the reconciler, because the direction that
PROTECTS a result is the opposite of the direction that ends a process. A run
records ``result_complete`` only after it rewrote ``result.txt`` whole, so the
flag is the one completeness fact (``result_is_whole``), and the reconcile must
neither demote a flagged answer to a fragment nor promote an unflagged one:

* the completeness rule -> ``test_a_flagged_result_is_the_agents_answer``
* its safe-direction complement -> ``test_an_unflagged_result_still_under_claims_as_a_fragment``
* end to end over the reconcile -> ``test_the_reconcile_announces_a_finished_run_as_finished``

Every one of those pairings is executed, not asserted in prose: the harness named
in the pull request re-applies each mutation and requires the named test to fail.

Three positive controls sit beside them, because a refusal test passes for free
when the thing it refuses never happens: the spawn-marker read against a fixture
process table, the recycle seam against a registry file in the product's own
format, and the lease seam against the real ownership table.

This module holds the reconciler core and the app backend table. The sweep gates and
the finished-result signal are in ``test_runtime_reconcile_sweep_gates.py``, a pass's
inputs and outputs in ``test_runtime_reconcile_wiring.py``, and tenancy with the
barrier in ``test_runtime_reconcile_tenancy.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from runtime_reconcile_helpers import _LeaseHolder, _reconciler, _refusals

from kiro_crew import runtime_ownership as ro
from kiro_crew import runtime_reconcile as rr
from kiro_crew.apps import backend as bmod

# The rootdir conftest wipes runtime_ownership's tables on both sides of every
# test. This module, like its _wiring, _sweep_gates and _tenancy siblings, opts out
# and isolates its tests with explicit ``ro._reset_for_tests()`` calls. The opt-out
# matters most in test_runtime_reconcile_tenancy.py, whose positive control at the
# end reads the table its earlier tests left: a wipe at any test's teardown would
# make that read vacuous.
pytestmark = pytest.mark.keep_runtime_ownership_tables


# ── the reconciler core ───────────────────────────────────────────────────────


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


# ── the app backend table ─────────────────────────────────────────────────────


class _Child:
    """The two members of a ``Popen`` handle the table read touches."""

    def __init__(self, pid: int, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


def test_a_backend_this_gateway_spawned_is_owned_and_never_reaches_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION TARGET: the app backend table is part of membership.

    An app backend runs inside the agent slice, carries the spawn marker and is
    long-lived by design, yet it is not a session, not a pooled MCP backend and in
    neither tracked pid file. The gateway's own table of what it spawned is the one
    thing that claims it; without it every backend is unowned on every pass and
    collects a gate allow, and a kill attribution naming it, whenever the argv check
    does not happen to decline it.

    The membership read is the REAL wiring's; every other seam is faked past its
    condition. Three neighbours keep the test honest. The unclaimed pid reaches the
    gate, which proves the arm is armed rather than inert. That same pid has a row in
    ``app_backends.pids.json``, which an agent can write, and the row claims nothing.
    And a backend whose child has exited is out of the answer, so it is not reported
    as a dead runtime.
    """
    from kiro_crew import session_pid

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(session_pid, "config_dir", lambda: home)
    monkeypatch.setattr(rr, "_mcp_backend_pids", lambda: set())
    planted = tmp_path / "app_backends.pids.json"
    planted.write_text(
        json.dumps({"planted": {"pid": 5402, "start_time": "ST-5402", "port": 9100}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(bmod, "_pidfile_path", lambda: planted)
    monkeypatch.setattr(
        bmod,
        "_processes",
        {
            "dev-fleet": bmod.AppProcess(app_name="dev-fleet", pid=5401, proc=_Child(5401)),
            "crashed": bmod.AppProcess(app_name="crashed", pid=5403, proc=_Child(5403, 1)),
        },
    )

    wired = rr.build_reconciler(active_pids=lambda: set(), notify_dead=lambda pid: None)
    asked: list[int] = []
    killed: list[int] = []
    dead: list[int] = []
    rec = rr.RuntimeReconciler(
        slice_pids=lambda: {5401, 5402},
        recorded_pids=wired._recorded_pids,
        is_alive=lambda pid: pid != 5403,
        identity_of=lambda pid: f"id-{pid}",
        is_ours=lambda pid: True,
        is_managed=lambda pid: True,
        leases_on=lambda pid: 0,
        claims_on=lambda pid: 0,
        authorize=lambda pid, reason: asked.append(pid) is None,
        kill_tree=lambda pid, expected=None: killed.append(pid) or 1,
        forget=lambda pid: "not-mine",
        notify_dead=dead.append,
        age_secs=lambda pid: 10_000.0,
        audit=lambda pid, outcome, why: None,
    )
    first = rec.run_once()
    rec.run_once()

    assert first.supported, first.reason
    assert first.unowned_alive == 1, "only the unclaimed neighbour is unowned"
    assert first.owned_alive == 1, "the backend the table holds is counted as owned"
    assert first.owned_dead == 0 and dead == [], "an exited backend is not a dead runtime"
    assert asked == [5402], f"the spawned backend never reaches the gate; got {asked}"
    assert killed == [5402], f"and is never signalled; got {killed}"


def test_a_forking_launchers_server_child_is_owned_through_the_real_membership_wiring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION TARGET: the children union reaches the reconciler's real membership.

    A Linux app backend is spawned through ``sandbox_launcher``, which forks: the
    tracked ``Popen`` root is the launcher parent and the real server is its forked
    child. BOTH run in the agent slice. Without the children union the server is
    unowned on every pass and collects a gate allow and a kill; with it, the record's
    ``forking_sandbox_launcher`` widens ownership to the root's direct children, so the
    whole backend is owned.

    The membership read is the REAL ``build_reconciler`` wiring; the children reader is
    faked so the test does not depend on a live ``/proc`` tree. The assertion that this
    FAILS without the union is the positive control below.
    """
    from kiro_crew import platform_compat, session_pid

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(session_pid, "config_dir", lambda: home)
    monkeypatch.setattr(rr, "_mcp_backend_pids", lambda: set())
    # Launcher parent 6001 (the tracked Popen root), forked server child 6002.
    monkeypatch.setattr(
        platform_compat, "_proc_children", lambda pid: [6002] if pid == 6001 else []
    )
    monkeypatch.setattr(
        bmod,
        "_processes",
        {
            "dev-fleet": bmod.AppProcess(
                app_name="dev-fleet",
                pid=6001,
                proc=_Child(6001),
                forking_sandbox_launcher=True,
            ),
        },
    )

    wired = rr.build_reconciler(active_pids=lambda: set(), notify_dead=lambda pid: None)
    asked: list[int] = []
    killed: list[int] = []
    rec = rr.RuntimeReconciler(
        slice_pids=lambda: {6001, 6002},
        recorded_pids=wired._recorded_pids,
        is_alive=lambda pid: True,
        identity_of=lambda pid: f"id-{pid}",
        is_ours=lambda pid: True,
        is_managed=lambda pid: True,
        leases_on=lambda pid: 0,
        claims_on=lambda pid: 0,
        authorize=lambda pid, reason: asked.append(pid) is None,
        kill_tree=lambda pid, expected=None: killed.append(pid) or 1,
        forget=lambda pid: "not-mine",
        notify_dead=lambda pid: None,
        age_secs=lambda pid: 10_000.0,
        audit=lambda pid, outcome, why: None,
    )
    first = rec.run_once()
    rec.run_once()

    assert first.supported, first.reason
    assert first.unowned_alive == 0, "both the launcher root and its server child are owned"
    assert first.owned_alive == 2, "the root and its direct child are both counted as owned"
    assert asked == [], f"nothing reaches the gate; got {asked}"
    assert killed == [], f"and nothing is signalled; got {killed}"


def test_without_the_children_union_the_server_child_would_be_unowned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSITIVE CONTROL for the test above: with the bool OFF, the server child leaks.

    Same slice and wiring, but the record is NOT marked ``forking_sandbox_launcher``.
    The membership read then names only the root 6001, so the forked server 6002 is
    unowned, reaches the gate and is signalled -- which is exactly the leak the union
    closes. If this test ever reports ``unowned_alive == 0`` the union has stopped
    being load-bearing and the test above proves nothing.
    """
    from kiro_crew import platform_compat, session_pid

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(session_pid, "config_dir", lambda: home)
    monkeypatch.setattr(rr, "_mcp_backend_pids", lambda: set())
    monkeypatch.setattr(
        platform_compat, "_proc_children", lambda pid: [6002] if pid == 6001 else []
    )
    monkeypatch.setattr(
        bmod,
        "_processes",
        {
            "dev-fleet": bmod.AppProcess(
                app_name="dev-fleet",
                pid=6001,
                proc=_Child(6001),
                forking_sandbox_launcher=False,
            ),
        },
    )

    wired = rr.build_reconciler(active_pids=lambda: set(), notify_dead=lambda pid: None)
    asked: list[int] = []
    killed: list[int] = []
    rec = rr.RuntimeReconciler(
        slice_pids=lambda: {6001, 6002},
        recorded_pids=wired._recorded_pids,
        is_alive=lambda pid: True,
        identity_of=lambda pid: f"id-{pid}",
        is_ours=lambda pid: True,
        is_managed=lambda pid: True,
        leases_on=lambda pid: 0,
        claims_on=lambda pid: 0,
        authorize=lambda pid, reason: asked.append(pid) is None,
        kill_tree=lambda pid, expected=None: killed.append(pid) or 1,
        forget=lambda pid: "not-mine",
        notify_dead=lambda pid: None,
        age_secs=lambda pid: 10_000.0,
        audit=lambda pid, outcome, why: None,
    )
    first = rec.run_once()
    rec.run_once()

    assert first.unowned_alive == 1, "the server child is unowned without the union"
    assert asked == [6002], f"only the unclaimed server child reaches the gate; got {asked}"
    assert killed == [6002], f"and it is the one signalled; got {killed}"


def test_the_app_backend_table_claims_only_a_running_child_this_gateway_spawned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTATION TARGET: which records of the table claim a pid.

    A pid is claimed only while this process holds the backend as a running child: a
    child that has not been reaped keeps its pid, so the number provably names the
    backend. An exited child is gone. An adopted backend was launched by somebody
    else, so the gateway vouches for none of its pids, and the STARTING placeholder
    has no process at all.

    A record whose ``forking_sandbox_launcher`` is set would also contribute its
    root's direct children -- but only while the root is RUNNING. An EXITED forking
    root contributes neither itself nor any child: it is dropped before the children
    reader is ever consulted, so a reused pid's current children can never be claimed
    on behalf of a dead launcher.
    """
    from kiro_crew import platform_compat

    children_calls: list[int] = []

    def _fake_children(pid: int) -> list[int]:
        children_calls.append(pid)
        return [9001]  # would be claimed if an exited root were walked

    monkeypatch.setattr(platform_compat, "_proc_children", _fake_children)
    monkeypatch.setattr(
        bmod,
        "_processes",
        {
            "running": bmod.AppProcess(app_name="running", pid=5501, proc=_Child(5501)),
            "exited": bmod.AppProcess(app_name="exited", pid=5502, proc=_Child(5502, 0)),
            "exited-forking": bmod.AppProcess(
                app_name="exited-forking",
                pid=5504,
                proc=_Child(5504, 0),
                forking_sandbox_launcher=True,
            ),
            "adopted": bmod.AppProcess(
                app_name="adopted",
                adopted_pids=[5503],
                adopted_start_times={5503: "ST-5503"},
            ),
            "starting": bmod.AppProcess(app_name="starting", starting=True),
        },
    )
    assert bmod.running_spawned_backend_pids() == {5501}
    assert children_calls == [], "an exited forking root is never walked for children"


def test_a_forking_launcher_claims_its_root_and_the_roots_direct_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTATION TARGET: a forking-launcher record widens to the root's DIRECT children.

    On Linux ``wrap_argv`` inserts ``sandbox_launcher``, which forks: the ``Popen``
    root is the launcher parent and the real server is its forked child, in the same
    slice and claimed by nothing. A record whose ``forking_sandbox_launcher`` is set
    therefore contributes the root AND that root's direct children (the server), so
    the server is owned rather than reading as a leak on every pass.
    """
    from kiro_crew import platform_compat

    monkeypatch.setattr(
        platform_compat, "_proc_children", lambda pid: [8801] if pid == 5601 else []
    )
    monkeypatch.setattr(
        bmod,
        "_processes",
        {
            "forking": bmod.AppProcess(
                app_name="forking",
                pid=5601,
                proc=_Child(5601),
                forking_sandbox_launcher=True,
            ),
        },
    )
    assert bmod.running_spawned_backend_pids() == {5601, 8801}


def test_a_grandchild_of_a_forking_launcher_root_is_not_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTATION TARGET: the widening is DIRECT children only, never grandchildren.

    The launcher's forked child IS the server; anything the server itself forks below
    it is the app's own doing and a frozen Not-a-goal. The reader asks for the root's
    direct children once and never walks the tree, so a grandchild (a child of the
    server, not of the root) is never in the answer.
    """
    from kiro_crew import platform_compat

    # 5602 -> [8802]; 8802 -> [9999]. A tree walk would reach 9999; a direct-children
    # read of the ROOT alone returns only 8802.
    tree = {5602: [8802], 8802: [9999]}

    def _children(pid: int) -> list[int]:
        return tree.get(pid, [])

    monkeypatch.setattr(platform_compat, "_proc_children", _children)
    monkeypatch.setattr(
        bmod,
        "_processes",
        {
            "forking": bmod.AppProcess(
                app_name="forking",
                pid=5602,
                proc=_Child(5602),
                forking_sandbox_launcher=True,
            ),
        },
    )
    assert bmod.running_spawned_backend_pids() == {5602, 8802}, "the grandchild 9999 is not claimed"


def test_a_non_forking_record_claims_only_its_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTATION TARGET: ``forking_sandbox_launcher`` False claims no children.

    A no-op wrap on an unconfined host, and the macOS seatbelt wrap that execs without
    forking, both leave the root as the server itself. The default-False record must
    contribute its root only, and the children reader must never be consulted for it.
    """
    from kiro_crew import platform_compat

    walked: list[int] = []

    def _children(pid: int) -> list[int]:
        walked.append(pid)
        return [8803]

    monkeypatch.setattr(platform_compat, "_proc_children", _children)
    monkeypatch.setattr(
        bmod,
        "_processes",
        {
            "plain": bmod.AppProcess(app_name="plain", pid=5603, proc=_Child(5603)),
        },
    )
    assert bmod.running_spawned_backend_pids() == {5603}
    assert walked == [], "a non-forking record never reaches the children reader"


def test_an_oserror_reading_children_leaves_the_forking_root_claimed_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTATION TARGET: an OSError from the children reader degrades to root-only.

    Reading ``/proc`` for one root's children can fail; when it does the root stays
    claimed on its own rather than the whole record being dropped (which would make a
    live launcher parent read as a leak).
    """
    from kiro_crew import platform_compat

    def _boom(pid: int) -> list[int]:
        raise OSError("proc read failed")

    monkeypatch.setattr(platform_compat, "_proc_children", _boom)
    monkeypatch.setattr(
        bmod,
        "_processes",
        {
            "forking": bmod.AppProcess(
                app_name="forking",
                pid=5604,
                proc=_Child(5604),
                forking_sandbox_launcher=True,
            ),
        },
    )
    assert bmod.running_spawned_backend_pids() == {5604}


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
    from kiro_crew.session_pid import _pid_age_seconds

    mine = os.getpid()
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


class _FrozenClock:
    """Stand-in for ``session_pid.time`` whose ``time()`` is frozen at *now*."""

    def __init__(self, now: float) -> None:
        self._now = now

    def time(self) -> float:
        return self._now


def _windows_filetime(unix_instant: float) -> str:
    """*unix_instant* encoded as ``get_process_start_id`` reports it on Windows."""
    from kiro_crew.session_pid import (
        _FILETIME_TICKS_PER_SECOND,
        _FILETIME_UNIX_EPOCH_OFFSET_SECONDS,
    )

    return str(
        int((unix_instant + _FILETIME_UNIX_EPOCH_OFFSET_SECONDS) * _FILETIME_TICKS_PER_SECOND)
    )


def test_windows_age_floor_reads_the_filetime_and_withholds_a_young_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTATION TARGET: the reconciler's age floor on Windows.

    ``process_age_secs`` reads the creation FILETIME there. A process younger
    than :data:`DEFAULT_MIN_AGE_SECS` must still be held by the floor, and an
    unreadable age must still read as too young to touch.
    """
    from kiro_crew import session_pid

    now = 1_700_000_000.0
    young_pid, old_pid, gone_pid = 4242, 4343, 4444
    starts = {young_pid: _windows_filetime(now - 60.0), old_pid: _windows_filetime(now - 600.0)}
    monkeypatch.setattr(session_pid.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(session_pid.platform_compat, "get_process_start_id", starts.get)
    monkeypatch.setattr(session_pid, "time", _FrozenClock(now))

    young = rr.process_age_secs(young_pid)
    assert abs(young - 60.0) < 0.01
    assert young < rr.DEFAULT_MIN_AGE_SECS
    assert abs(rr.process_age_secs(old_pid) - 600.0) < 0.01
    assert rr.process_age_secs(gone_pid) == 0.0

    killed: list[int] = []
    audited: list[tuple[int, str, str]] = []
    rec = _reconciler(kernel={young_pid}, recorded=set(), age=young, killed=killed, audited=audited)
    rec.run_once()
    audited.clear()
    reading = rec.run_once()
    assert reading.killed == 0 and killed == []
    assert ("younger than the age floor",) == _refusals(audited)


def test_windows_reconciler_has_no_kill_population_whatever_the_age(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """MUTATION TARGET: what keeps the reconciler's kill arms off Windows.

    A readable Windows age does not arm either kill path. The scheduled arm's
    population is the agent-slice cgroup, which resolves to nothing off Linux,
    and the confirmed reclaim refuses off Linux before reading any pid. Both hold
    even when every candidate reads far older than the age floor.
    """
    import sys

    from kiro_crew import sandbox
    from kiro_crew.session_scope_reap import instance_slice_pids

    assert rr.RECLAIM_PLATFORM is (sys.platform == "linux")

    # A populated agent slice, so the platform is the only thing that empties it.
    instance = "kirocrew-agents-t0k3n.slice"
    scope = tmp_path / "kirocrew.slice" / sandbox._CGROUP_AGENTS_SLICE / instance / "a.scope"
    scope.mkdir(parents=True)
    (scope / "cgroup.procs").write_text("4242\n", encoding="utf-8")
    monkeypatch.setattr(sandbox, "_USER_MANAGER_CGROUP_BASE", str(tmp_path))
    monkeypatch.setattr(sandbox, "_agents_slice_name", lambda: instance)
    monkeypatch.setattr(os, "getuid", lambda: 0, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    assert instance_slice_pids() == {4242}

    monkeypatch.setattr(sys, "platform", "win32")
    assert instance_slice_pids() == set()

    killed: list[int] = []
    rec = rr.RuntimeReconciler(
        slice_pids=instance_slice_pids,
        recorded_pids=set,
        is_alive=lambda pid: True,
        is_ours=lambda pid: True,
        is_managed=lambda pid: True,
        authorize=lambda pid, reason: True,
        identity_of=lambda pid: f"id-{pid}",
        kill_tree=lambda pid, expected=None: killed.append(pid) or 1,
        forget=lambda pid: "not-mine",
        notify_dead=lambda pid: None,
        age_secs=lambda pid: 10_000.0,
        untracked_pids=lambda: {4242},
        confirm_untracked=lambda: {4242},
        reclaim_platform=False,
    )
    rec.run_once()
    reading = rec.run_once()
    assert reading.unowned_alive == 0 and reading.killed == 0
    result = rec.reclaim_untracked()
    assert result.supported is False and result.killed == ()
    assert killed == []


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
