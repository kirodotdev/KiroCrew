"""Tests for what a reconcile pass reads and what it writes back.

The argv condition and the kill budget, the pid union across every gateway on the
data home, the hook that hands the budget to each pass, the retraction of a dead
record's rows from the tracking files, the identity-keyed notification memory, and
the warn-once ledger for a refused pass.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Any

import pytest
from runtime_reconcile_helpers import _cleanup, _refusals

from kiro_crew import runtime_reconcile as rr

# Module-wide, like the other runtime reconciler test modules: the rootdir conftest
# leaves runtime_ownership's tables as this module's own tests leave them, and the
# tests that need a clean table call ``ro._reset_for_tests()`` themselves.
pytestmark = pytest.mark.keep_runtime_ownership_tables


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
    # Compare each row's OWN pid field. The parent field is this test process's pid,
    # and on a runner that pid can itself be 5501, so a substring check misreads it.
    recorded = {line.split(":", 1)[0] for line in rows.splitlines() if line}
    assert "5501" not in recorded, f"the row is gone from the file that held it; {rows!r}"
    assert "5502" in recorded, f"and the live sibling's row is untouched; {rows!r}"


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


def _dead_summary_levels(forget: Any, caplog: pytest.LogCaptureFixture) -> list[int]:
    """Run one pass over two dead records answered by *forget*; the summary's levels."""
    reconciler = rr.RuntimeReconciler(
        slice_pids=lambda: set(),
        recorded_pids=lambda: {7811, 7812},
        is_alive=lambda pid: False,
        identity_of=lambda pid: None,
        kill_tree=lambda pid, expected=None: 0,
        forget=forget,
        notify_dead=lambda pid: None,
    )
    with caplog.at_level(logging.DEBUG, logger="kiro_crew.runtime_reconcile"):
        reading = reconciler.run_once()
    assert reading.owned_dead == 2, "both records are counted dead whatever the answer"
    return [r.levelno for r in caplog.records if "records named processes" in r.getMessage()]


def test_a_dead_count_that_is_all_not_mine_is_summarised_below_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """MUTATION TARGET: the dead-direction summary's level in the steady state.

    Records nobody here can retract are the same reading every tick. Summarised at
    WARNING, the gateway log carried that line every five minutes forever.
    """
    levels = _dead_summary_levels(lambda pid: "not-mine", caplog)
    assert levels == [logging.DEBUG], f"the steady state is a debug line; got {levels}"


def test_a_dead_count_with_a_retraction_is_summarised_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other side: a pass that retracted a record changed state, so it warns."""
    levels = _dead_summary_levels(lambda pid: "retracted" if pid == 7811 else "not-mine", caplog)
    assert levels == [logging.WARNING], f"a retraction is worth a warning; got {levels}"


def test_a_dead_count_with_a_failed_retraction_is_summarised_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A retraction this pass owed and could not make keeps the summary at WARNING."""
    levels = _dead_summary_levels(lambda pid: "failed" if pid == 7811 else "not-mine", caplog)
    assert levels == [logging.WARNING], f"a failed retraction is worth a warning; got {levels}"


# ── the reconcile hook's refusal ledger: warn once, re-arm on recovery ─────────


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


def test_a_failing_scope_reap_hook_warns_rate_limited_and_re_arms(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An escape from the agent-scope reaper skips every scope, so it reaches WARNING.

    Once per interval while it keeps failing, and again at once after a clean pass:
    a DEBUG-only escape is how the reaper once went inert with nothing to see.
    """
    import dataclasses

    logger = logging.getLogger("test.scope_reap_hook.warn")
    clock = [100.0]
    cleanup = _cleanup_with_clock(clock, logger=logger)
    outcome: list[Any] = [RuntimeError("boom")]

    def reap(_pids: set[int]) -> Any:
        if isinstance(outcome[0], Exception):
            raise outcome[0]
        return outcome[0]

    cleanup._deps = dataclasses.replace(cleanup._deps, reap_agent_scopes=reap)
    interval = cleanup.PROBE_FAILURE_WARN_INTERVAL_SECS

    def warnings() -> int:
        return sum("Agent-scope reap failed" in m for m in caplog.messages)

    with caplog.at_level(logging.WARNING, logger=logger.name):
        asyncio.run(cleanup._reap_agent_scopes_hook())
        clock[0] += 1.0
        asyncio.run(cleanup._reap_agent_scopes_hook())
        assert warnings() == 1
        clock[0] += interval
        asyncio.run(cleanup._reap_agent_scopes_hook())
        assert warnings() == 2
        outcome[0] = None
        asyncio.run(cleanup._reap_agent_scopes_hook())
        outcome[0] = RuntimeError("again")
        clock[0] += 1.0
        asyncio.run(cleanup._reap_agent_scopes_hook())
        assert warnings() == 3


def _session_summary_levels(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    reading: Any,
    name: str,
) -> list[int]:
    """Drive one hook tick returning *reading*; the levels of its summary line."""
    logger = logging.getLogger(f"test.reconcile.summary.{name}")
    cleanup = _cleanup_with_clock([0.0], logger=logger)
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        _drive_reconcile(cleanup, monkeypatch, reading)
    return [
        r.levelno for r in caplog.records if "Runtime reconcile: unowned_alive" in r.getMessage()
    ]


def test_a_steady_reading_is_summarised_below_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """MUTATION TARGET: the hook's summary level when the pass did nothing.

    A permanently withheld unowned population and dead records nobody here can
    retract read the same every tick; at WARNING that was one line per tick for
    the gateway's life.
    """
    reading = rr.ReconcileReading(unowned_alive=20, owned_dead=10)
    levels = _session_summary_levels(monkeypatch, caplog, reading, "steady")
    assert levels == [logging.DEBUG], f"the steady state is a debug line; got {levels}"


@pytest.mark.parametrize(
    "acted",
    [{"killed": 1}, {"would_kill": 1}, {"forgotten": 1}],
    ids=["killed", "would_kill", "retracted"],
)
def test_a_reading_that_acted_is_summarised_at_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    acted: dict[str, int],
) -> None:
    """A kill, a withheld kill or a retraction keeps the summary at WARNING."""
    reading = rr.ReconcileReading(unowned_alive=20, owned_dead=10, **acted)
    levels = _session_summary_levels(monkeypatch, caplog, reading, next(iter(acted)))
    assert levels == [logging.WARNING], f"{acted} is worth a warning; got {levels}"


def test_the_reconcile_counter_name_passes_the_core_metric_name_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTATION TARGET: the per-pass counter is named so the recorder accepts it.

    The recorder refuses a core name outside ``kirocrew.`` / ``gen_ai.`` and only
    logs a warning, so a bad name drops the counter on every pass with no failure.
    """
    import dataclasses

    from kiro_crew.metrics.schema import validate_name

    emitted: list[tuple[str, dict[str, Any]]] = []
    cleanup, _recorded = _cleanup(candidates=[], active={4242})
    cleanup._deps = dataclasses.replace(
        cleanup._deps, emit_counter=lambda name, dims: emitted.append((name, dims))
    )

    _drive_reconcile(cleanup, monkeypatch, rr.ReconcileReading(supported=True))

    assert len(emitted) == 1, emitted
    name, dims = emitted[0]
    try:
        validate_name(name)
    except ValueError as exc:
        pytest.fail(f"the recorder would refuse the reconcile counter: {exc}")
    assert set(dims) == set(rr.ReconcileReading(supported=True).as_counter_fields())


def test_the_reconcile_counter_attributes_are_bounded_flags_not_raw_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTATION TARGET: no per-pass count becomes an attribute value.

    The recorder keeps one series per attribute set for the process's life, so a
    raw byte sum or pid count would open a new series on nearly every pass.
    """
    import dataclasses

    emitted: list[dict[str, Any]] = []
    cleanup, _recorded = _cleanup(candidates=[], active={4242})
    cleanup._deps = dataclasses.replace(
        cleanup._deps, emit_counter=lambda name, dims: emitted.append(dims)
    )
    reading = rr.ReconcileReading(
        supported=True, unowned_alive=3, leaked_untracked=2, leaked_rss_bytes=123_456_789
    )

    _drive_reconcile(cleanup, monkeypatch, reading)

    assert len(emitted) == 1, emitted
    assert all(type(v) is bool for v in emitted[0].values()), emitted[0]
    assert emitted[0]["unowned_alive"] is True
    assert emitted[0]["leaked_rss_bytes"] is True
    assert emitted[0]["killed"] is False
