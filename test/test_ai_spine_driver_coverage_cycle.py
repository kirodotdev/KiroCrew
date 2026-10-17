"""Coverage for the auto-improvement spine :mod:`driver` from one proposal to the CLI.

Covers one proposal through its track, the perf and bug verdicts, the per-cycle
workflow, the durable loop and the CLI. The guards, the push and review gates, the
direct push and the staging, commit and rollback paths are in
``test_ai_spine_driver_coverage.py``; the fakes, builders and fixtures both modules
use are in ``ai_spine_driver_coverage_helpers.py``.
"""

from __future__ import annotations

import types

import pytest
from ai_spine_driver_coverage_helpers import _isolate_env  # noqa: F401
from ai_spine_driver_coverage_helpers import git_fixture  # noqa: F401
from ai_spine_driver_coverage_helpers import (
    DIFF,
    LOG,
    _Clock,
    _Gate,
    _Keeper,
    _make,
    _measurement,
    _Measurer,
    _Pipeline,
    _Profile,
    _proposal,
    _Proposer,
    _Ruler,
    _SlottedRuler,
    _stub_preflight,
)

from kiro_crew.apps.builtins.auto_improvement.spine import driver as drv
from kiro_crew.apps.builtins.auto_improvement.spine import ledger as L
from kiro_crew.apps.builtins.auto_improvement.spine.contracts import (
    BUG_FAILED_BUILD,
    BUG_FILED,
    BUG_NOT_GREEN,
    TRACK_BUG,
    TRACK_PERF,
    BugGateResult,
    Candidate,
    DiscoveryResult,
    GateResult,
    Verdict,
)
from kiro_crew.apps.builtins.auto_improvement.spine.keeper import DISCARD_NOISE, KEPT
from kiro_crew.apps.builtins.auto_improvement.spine.pr_pipeline import CrOutcome

# ─────────────────────────── one proposal through its track ───────────────────────────


def test_a_skipped_proposal_records_its_own_terminal_status(tmp_path, git):
    d = _make(tmp_path)
    prop = _proposal(skipped=True, skip_status=L.STATUS_NO_DEFECT, skip_reason="nothing found")
    d._work_one_proposal(
        prop,
        base_sha="b",
        cycle=1,
        proposals=[prop],
        perf_survivors=[],
        bug_winners=[],
        gated_sha={},
    )
    fp = L.fingerprint(kind=TRACK_PERF, target="mod.py::sym")
    assert d.ledger._seen[fp].status == L.STATUS_NO_DEFECT
    assert d.stats.errors == 0


def test_a_skipped_proposal_from_a_real_error_counts_as_one(tmp_path, git):
    d = _make(tmp_path)
    prop = _proposal(skipped=True, skip_status=L.STATUS_ERROR, skip_reason="")
    d._work_one_proposal(
        prop,
        base_sha="b",
        cycle=1,
        proposals=[prop],
        perf_survivors=[],
        bug_winners=[],
        gated_sha={},
    )
    assert d.stats.errors == 1
    fp = L.fingerprint(kind=TRACK_PERF, target="mod.py::sym")
    assert d.ledger._seen[fp].note == "no diff produced"


def test_an_accepted_bug_fix_becomes_a_winner(tmp_path, git):
    d = _make(tmp_path)
    d.gate = _Gate(bug_result=BugGateResult(passed=True, reason=BUG_FILED))
    winners: list = []
    prop = _proposal(kind=TRACK_BUG)
    d._work_one_proposal(
        prop,
        base_sha="b",
        cycle=1,
        proposals=[prop],
        perf_survivors=[],
        bug_winners=winners,
        gated_sha={},
    )
    assert [p.cand_id for p, _ in winners] == ["c1"]


@pytest.mark.parametrize(
    "reason,expected_status,counter",
    [
        (BUG_FAILED_BUILD, L.STATUS_FAILED_GATE, "gated_out"),
        (BUG_NOT_GREEN, L.STATUS_FAILED_VERIFY, "not_kept"),
    ],
)
def test_a_rejected_bug_fix_maps_onto_the_shared_ledger_vocabulary(
    tmp_path, git, reason, expected_status, counter
):
    d = _make(tmp_path)
    d.gate = _Gate(bug_result=BugGateResult(passed=False, reason=reason, detail="why"))
    prop = _proposal(kind=TRACK_BUG)
    d._work_one_proposal(
        prop,
        base_sha="b",
        cycle=1,
        proposals=[prop],
        perf_survivors=[],
        bug_winners=[],
        gated_sha={},
    )
    fp = L.fingerprint(kind=TRACK_BUG, target="mod.py::sym")
    assert d.ledger._seen[fp].status == expected_status
    assert getattr(d.stats, counter) == 1


def test_a_perf_candidate_failing_the_gate_never_measures(tmp_path, git):
    d = _make(tmp_path)
    d.gate = _Gate(result=GateResult(passed=False, detail="tests red"))
    d.measurer = _Measurer()
    survivors: list = []
    prop = _proposal()
    d._work_one_proposal(
        prop,
        base_sha="b",
        cycle=1,
        proposals=[prop],
        perf_survivors=survivors,
        bug_winners=[],
        gated_sha={},
    )
    assert survivors == []
    assert d.stats.gated_out == 1


def test_a_gated_perf_candidate_is_measured_and_pinned_to_its_sha(tmp_path, git):
    d = _make(tmp_path)
    d.gate = _Gate(result=GateResult(passed=True, commit_sha="gated-1"))
    d.measurer = _Measurer()
    survivors: list = []
    gated: dict = {}
    prop = _proposal()
    d._work_one_proposal(
        prop,
        base_sha="b",
        cycle=1,
        proposals=[prop],
        perf_survivors=survivors,
        bug_winners=[],
        gated_sha=gated,
    )
    assert gated == {"c1": "gated-1"}
    assert len(survivors) == 1


# ─────────────────────────── the perf verdict ───────────────────────────


def test_no_keep_archives_every_survivor_with_its_real_discard_reason(tmp_path, git):
    d = _make(tmp_path)
    d.measurer = _Measurer()
    prop = _proposal()
    meas = _measurement(delta=-0.5)
    verdict = Verdict(keep=False, status="no_keep", reason="inside the band")
    assert d._apply_verdict(1, "base", verdict, [(prop, DISCARD_NOISE, meas)], 2, {}) == 2
    fp = L.fingerprint(kind=TRACK_PERF, target="mod.py::sym")
    assert d.ledger._seen[fp].status == L.STATUS_DISCARDED_NOISE
    assert d.stats.not_kept == 1
    assert (tmp_path / "results" / "candidates" / "c1.diff").exists()


def test_a_keep_whose_diff_will_not_apply_is_recorded_as_an_error(tmp_path, git):
    git.script("apply", 1)
    d = _make(tmp_path)
    d.measurer = _Measurer()
    prop = _proposal(diff=DIFF)
    meas = _measurement()
    verdict = Verdict(keep=True, status=KEPT, winner=prop, measurement=meas, reason="win")
    assert d._apply_verdict(1, "base", verdict, [(prop, KEPT, meas)], 1, {"c1": "g"}) == 1
    fp = L.fingerprint(kind=TRACK_PERF, target="mod.py::sym", signature="sig")
    assert d.ledger._seen[fp].note == "winner diff did not apply to the working branch"


def test_a_filed_perf_win_advances_the_branch_and_announces_the_cr(tmp_path, git):
    events: list = []
    d = _make(tmp_path, on_progress=events.append)
    d.measurer = _Measurer()
    d.pr_pipeline = _Pipeline(
        CrOutcome(fp="fp-perf", status="filed", cr="CR-9", filed=True, reproduce=_measurement(-4.0))
    )
    git.script("rev-parse --short HEAD", (0, "kept1\n", ""))
    prop = _proposal(diff="")
    meas = _measurement()
    verdict = Verdict(keep=True, status=KEPT, winner=prop, measurement=meas, reason="win")

    assert d._apply_verdict(4, "basesha", verdict, [(prop, KEPT, meas)], 1, {"c1": "gated"}) == 1

    assert d.stats.kept == 1
    assert d.stats.filed == 1
    assert d.pr_pipeline.perf_kwargs["gated_commit_sha"] == "gated"
    assert d.pr_pipeline.perf_kwargs["base_anchor"] == "auto_improvement/feature @ basesha"
    filed = [e for e in events if "cr_filed" in e]
    assert filed[0]["cr_filed"]["cr"] == "CR-9"
    assert filed[0]["cr_filed"]["base_ref"] == "origin/feature"


def test_an_unreproduced_perf_keep_rolls_the_branch_back(tmp_path, git):
    d = _make(tmp_path)
    d.measurer = _Measurer()
    d.pr_pipeline = _Pipeline(CrOutcome(fp="fp", status="failed_verify", filed=False))
    git.script("rev-parse HEAD", (0, "presha\n", ""), (0, "postsha\n", ""))
    prop = _proposal(diff="")
    meas = _measurement()
    verdict = Verdict(keep=True, status=KEPT, winner=prop, measurement=meas, reason="win")
    d._apply_verdict(1, "base", verdict, [(prop, KEPT, meas)], 1, {})
    assert d.stats.kept == 0  # the keep did not become a reproduced win
    assert git.seen("checkout -f -B auto_improvement/feature presha")


def test_a_direct_committed_perf_win_records_the_landed_sha(tmp_path, git):
    d = _make(tmp_path, direct_commit=True)
    d.measurer = _Measurer()
    d.pr_pipeline = _Pipeline(
        CrOutcome(fp="fp-c", status="committed", committed_ready=True, reproduce=_measurement())
    )
    git.script("rev-parse --short HEAD", (0, "shortsha\n", ""))
    d._direct_push = lambda **kwargs: True
    d.pushed_sha = "landed123"
    prop = _proposal(diff="")
    meas = _measurement()
    verdict = Verdict(keep=True, status=KEPT, winner=prop, measurement=meas, reason="win")

    d._apply_verdict(1, "base", verdict, [(prop, KEPT, meas)], 1, {})

    entry = d.ledger._seen["fp-c"]
    assert entry.status == L.STATUS_COMMITTED
    assert entry.cr == "landed123"
    assert d.stats.filed == 1


def test_a_refused_direct_push_rolls_the_commit_back_and_unwinds_the_keep(tmp_path, git):
    d = _make(tmp_path, direct_commit=True)
    d.measurer = _Measurer()
    d.pr_pipeline = _Pipeline(CrOutcome(fp="fp-r", status="error", committed_ready=True))
    git.script("rev-parse HEAD", (0, "presha\n", ""), (0, "postsha\n", ""))
    git.script("rev-parse --short HEAD", (0, "short\n", ""))
    d._direct_push = lambda **kwargs: False
    prop = _proposal(diff="")
    meas = _measurement()
    verdict = Verdict(keep=True, status=KEPT, winner=prop, measurement=meas, reason="win")

    d._apply_verdict(1, "base", verdict, [(prop, KEPT, meas)], 1, {})

    assert d.stats.kept == 0
    assert d.stats.filed == 0
    assert git.seen("checkout -f -B auto_improvement/feature presha")


# ─────────────────────────── the bug verdict ───────────────────────────


def test_a_bug_fix_that_will_not_apply_is_recorded_as_an_error(tmp_path, git):
    git.script("apply", 1)
    git.script("apply --3way", 1)
    d = _make(tmp_path)
    d._apply_bug_winner(
        1, _proposal(kind=TRACK_BUG, diff=DIFF), BugGateResult(passed=True, reason=BUG_FILED)
    )
    fp = L.fingerprint(kind=TRACK_BUG, target="mod.py::sym", signature="sig")
    assert d.ledger._seen[fp].note == "bug fix diff did not apply to the working branch"


def test_a_filed_bug_fix_files_then_returns_head_to_where_it_started(tmp_path, git):
    events: list = []
    d = _make(tmp_path, on_progress=events.append)
    d.pr_pipeline = _Pipeline(CrOutcome(fp="fp-bug", status="filed", cr="CR-3", filed=True))
    git.script("rev-parse HEAD", (0, "presha\n", ""), (0, "postsha\n", ""))
    git.script("rev-parse --short HEAD", (0, "bugshort\n", ""))

    d._apply_bug_winner(
        2, _proposal(kind=TRACK_BUG, diff=""), BugGateResult(passed=True, reason=BUG_FILED)
    )

    assert d.stats.kept == 1
    assert d.stats.filed == 1
    assert d.pr_pipeline.bug_kwargs["base_anchor"] == "auto_improvement/feature @ presha"
    assert [e for e in events if "cr_filed" in e][0]["cr_filed"]["kind"] == "bug"
    assert git.seen("checkout -f -B auto_improvement/feature presha")


def test_a_direct_committed_bug_fix_records_committed_once(tmp_path, git):
    d = _make(tmp_path, direct_commit=True)
    d.pr_pipeline = _Pipeline(CrOutcome(fp="fp-bc", status="committed", committed_ready=True))
    git.script("rev-parse HEAD", (0, "e" * 40 + "\n", ""))
    d._direct_push = lambda **kwargs: True
    d.pushed_sha = ""

    d._apply_bug_winner(
        1, _proposal(kind=TRACK_BUG, diff=""), BugGateResult(passed=True, reason=BUG_FILED)
    )

    entry = d.ledger._seen["fp-bc"]
    assert entry.status == L.STATUS_COMMITTED
    assert entry.cr == "e" * 40  # fell back to the finalizer's full head id
    assert (d.stats.kept, d.stats.filed) == (1, 1)


def test_a_refused_bug_push_rolls_back_without_touching_the_keep_counter(tmp_path, git):
    d = _make(tmp_path, direct_commit=True)
    d.pr_pipeline = _Pipeline(CrOutcome(fp="fp-br", status="error", committed_ready=True))
    git.script("rev-parse HEAD", (0, "presha\n", ""), (0, "postsha\n", ""))
    git.script("rev-parse --short HEAD", (0, "short\n", ""))
    d._direct_push = lambda **kwargs: False

    d._apply_bug_winner(
        1, _proposal(kind=TRACK_BUG, diff=""), BugGateResult(passed=True, reason=BUG_FILED)
    )

    assert d.stats.kept == 0  # never incremented, so never decremented
    assert git.seen("checkout -f -B auto_improvement/feature presha")


def test_an_unfiled_bug_fix_leaves_head_where_it_was(tmp_path, git):
    d = _make(tmp_path)
    d.pr_pipeline = _Pipeline(CrOutcome(fp="fp", status="duplicate", filed=False))
    git.script("rev-parse HEAD", (0, "presha\n", ""), (0, "postsha\n", ""))
    d._apply_bug_winner(
        1, _proposal(kind=TRACK_BUG, diff=""), BugGateResult(passed=True, reason=BUG_FILED)
    )
    assert d.stats.filed == 0
    assert git.seen("checkout -f -B auto_improvement/feature presha")


# ─────────────────────────── the per-cycle workflow ───────────────────────────


def test_a_cycle_with_no_candidates_returns_zero_fresh(tmp_path, git):
    events: list = []
    d = _make(tmp_path, on_progress=events.append)
    d.proposer = _Proposer()
    assert d.run_cycle(1) == 0
    assert d.profile.discover_kwargs["base_sha"] == ""
    assert events[-1]["fresh"] == 0


def test_an_already_terminal_locus_is_deduped_before_any_expensive_work(tmp_path, git):
    cand = Candidate(kind=TRACK_PERF, target="mod.py::hot")
    d = _make(tmp_path, profile=_Profile(discovery=DiscoveryResult(candidates=[cand])))
    d.proposer = _Proposer()
    fp = L.fingerprint(kind=TRACK_PERF, target="mod.py::hot")
    d.ledger.record(
        L.LedgerEntry(fp=fp, kind=TRACK_PERF, target="mod.py::hot", status=L.STATUS_FILED)
    )
    assert d.run_cycle(1) == 0
    assert d.stats.deduped == 1
    assert d.proposer.fan_out_kwargs == {}


def test_the_skip_list_is_a_cost_optimization_and_never_fatal(tmp_path, git):
    d = _make(tmp_path)
    d.proposer = _Proposer()
    d.ledger.terminal_targets = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("no"))
    assert d.run_cycle(1) == 0


def test_the_discovery_rotation_and_skip_list_reach_the_profile(tmp_path, git):
    d = _make(tmp_path)
    d.proposer = _Proposer()
    d.run_cycle(7)
    assert d.profile._discovery_rotate == 7
    assert d.profile._skip_targets == []


def test_one_bad_candidate_never_kills_the_cycle(tmp_path, git):
    cand = Candidate(kind=TRACK_PERF, target="mod.py::hot")
    prop = _proposal("cX", target="mod.py::hot")
    d = _make(tmp_path, profile=_Profile(discovery=DiscoveryResult(candidates=[cand])))
    d.proposer = _Proposer([prop])
    d.keeper = _Keeper()
    d.measurer = _Measurer()

    def _boom(*args, **kwargs):
        raise ValueError("gate exploded")

    d._work_one_proposal = _boom
    assert d.run_cycle(1) == 1
    assert d.stats.errors == 1
    fp = L.fingerprint(kind=TRACK_PERF, target="mod.py::hot")
    assert d.ledger._seen[fp].status == L.STATUS_ERROR
    assert d.ledger._seen[fp].note.startswith("ValueError:")
    assert d.proposer.torn_down == ["cX"]


def test_a_stop_request_aborts_the_proposal_loop(tmp_path, git):
    cand = Candidate(kind=TRACK_PERF, target="mod.py::hot")
    props = [_proposal("c1", target="mod.py::hot"), _proposal("c2", target="mod.py::hot")]
    d = _make(tmp_path, profile=_Profile(discovery=DiscoveryResult(candidates=[cand])))
    d.proposer = _Proposer(props)
    d.keeper = _Keeper()
    d.measurer = _Measurer()
    worked: list = []
    d._work_one_proposal = lambda prop, **kwargs: worked.append(prop.cand_id)
    d.request_stop()
    d.run_cycle(1)
    assert worked == []
    assert sorted(d.proposer.torn_down) == ["c1", "c2"]


def test_two_bug_winners_on_one_locus_file_once(tmp_path, git):
    cand = Candidate(kind=TRACK_BUG, target="mod.py::bug")
    props = [
        _proposal("b1", kind=TRACK_BUG, target="mod.py::bug"),
        _proposal("b2", kind=TRACK_BUG, target="mod.py::bug"),
    ]
    d = _make(
        tmp_path,
        profile=_Profile(track=TRACK_BUG, discovery=DiscoveryResult(candidates=[cand])),
    )
    d.proposer = _Proposer(props)
    d.gate = _Gate(bug_result=BugGateResult(passed=True, reason=BUG_FILED))
    d.keeper = _Keeper()
    d.measurer = _Measurer()
    applied: list = []
    d._apply_bug_winner = lambda cycle, prop, bug_res: applied.append(prop.cand_id)

    d.run_cycle(1)

    assert applied == ["b1"]
    assert d.stats.deduped == 1


def test_the_keeper_is_told_the_rulers_improving_direction(tmp_path, git):
    cand = Candidate(kind=TRACK_PERF, target="mod.py::hot")
    d = _make(
        tmp_path,
        profile=_Profile(
            ruler=_Ruler(direction="maximize"), discovery=DiscoveryResult(candidates=[cand])
        ),
    )
    d.proposer = _Proposer([_proposal("c1", target="mod.py::hot")])
    d.gate = _Gate()
    d.measurer = _Measurer()
    d.keeper = _Keeper()
    d.run_cycle(1)
    assert d.keeper.direction == "maximize"


# ─────────────────────────── the durable loop ───────────────────────────


def _loop_driver(tmp_path, git, *, caps=None, profile=None, keeps=0, **kw):
    d = _make(tmp_path, caps=caps, profile=profile, **kw)
    d.run_cycle = lambda cycle: keeps
    return d


def test_a_dry_run_exercises_exactly_one_cycle(tmp_path, git):
    d = _loop_driver(tmp_path, git)
    cycles: list = []
    d.run_cycle = lambda cycle: cycles.append(cycle) or 0
    stats = d.run(dry_run=True)
    assert stats.cycles == 1
    assert cycles == [1]
    meta = (tmp_path / "results" / "run.meta.json").read_text()
    assert '"profile_id": "fake-profile"' in meta


def test_the_run_meta_reads_head_when_the_clone_is_a_real_repo(tmp_path, git):
    git.script("rev-parse HEAD", (0, "basesha\n", ""))
    d = _loop_driver(tmp_path, git)
    (d.clone / ".git").mkdir()
    d.run(dry_run=True)
    assert '"base_sha": "basesha"' in (tmp_path / "results" / "run.meta.json").read_text()


def test_a_stop_request_before_the_loop_runs_no_cycle(tmp_path, git):
    d = _loop_driver(tmp_path, git)
    d.request_stop()
    assert d.run(dry_run=True).cycles == 0


def test_the_time_budget_ends_the_run_cleanly(tmp_path, git, monkeypatch):
    monkeypatch.setattr(drv, "time", _Clock([0.0, 40000.0]))
    d = _loop_driver(tmp_path, git, caps=drv.BudgetCaps(max_hours=1.0))
    assert d.run(dry_run=False, preflight=False).cycles == 0


def test_the_cost_budget_ends_the_run_cleanly(tmp_path, git, monkeypatch):
    monkeypatch.setattr(drv, "time", _Clock([0.0]))
    d = _loop_driver(tmp_path, git, caps=drv.BudgetCaps(max_cost_usd=5.0), cost_meter=lambda: 99.0)
    stats = d.run(dry_run=False, preflight=False)
    assert stats.cycles == 0
    assert stats.cost_usd == 99.0


def test_quiescence_stops_a_mined_out_run(tmp_path, git, monkeypatch):
    monkeypatch.setattr(drv, "time", _Clock([0.0]))
    events: list = []
    d = _loop_driver(
        tmp_path,
        git,
        caps=drv.BudgetCaps(max_cycles=9, quiesce_after=2),
        on_progress=events.append,
    )
    stats = d.run(dry_run=False, preflight=False)
    assert stats.cycles == 2
    quiesce = [e for e in events if "quiescence" in e]
    assert quiesce[-1]["quiescence"] == {"cyclesSinceKeep": 2, "stopAt": 2}
    assert quiesce[-1]["budget"]["cycles_used"] == 2


def test_a_non_positive_quiesce_after_never_quiesces(tmp_path, git, monkeypatch):
    clock = _Clock([0.0])
    monkeypatch.setattr(drv, "time", clock)
    d = _loop_driver(
        tmp_path, git, caps=drv.BudgetCaps(max_cycles=3, quiesce_after=0, cycle_gap_s=0.25)
    )
    assert d.run(dry_run=False, preflight=False).cycles == 3
    assert clock.slept == [0.25, 0.25, 0.25]


def test_a_keep_resets_the_no_keep_streak(tmp_path, git, monkeypatch):
    monkeypatch.setattr(drv, "time", _Clock([0.0]))
    d = _loop_driver(tmp_path, git, caps=drv.BudgetCaps(max_cycles=2, quiesce_after=1))

    def _cycle(cycle):
        d.stats.kept += 1
        return 1

    d.run_cycle = _cycle
    assert d.run(dry_run=False, preflight=False).cycles == 2


def test_a_real_run_proves_the_ruler_before_the_loop(tmp_path, git, monkeypatch):
    monkeypatch.setattr(drv, "time", _Clock([0.0]))
    _stub_preflight(monkeypatch)
    events: list = []
    profile = _Profile(ruler=_Ruler(baselines={"boot": 120.0}))
    d = _loop_driver(
        tmp_path,
        git,
        caps=drv.BudgetCaps(max_cycles=1),
        profile=profile,
        on_progress=events.append,
    )
    d.run(dry_run=False)
    pf = [e for e in events if "preflight" in e][0]["preflight"]
    assert pf == {
        "noise_band": 7.5,
        "baseline_n": 5,
        "canary_delta": -30.0,
        "guardrail_baselines": {"boot": 120.0},
    }


def test_a_ruler_without_baselines_still_reports_the_band(tmp_path, git, monkeypatch):
    monkeypatch.setattr(drv, "time", _Clock([0.0]))
    _stub_preflight(monkeypatch)
    events: list = []
    d = _loop_driver(
        tmp_path,
        git,
        caps=drv.BudgetCaps(max_cycles=1),
        profile=_Profile(ruler=_SlottedRuler()),
        on_progress=events.append,
    )
    d.run(dry_run=False)
    pf = [e for e in events if "preflight" in e][0]["preflight"]
    assert pf["guardrail_baselines"] == {}


def test_the_bug_track_skips_the_ruler_preflight(tmp_path, git, monkeypatch):
    monkeypatch.setattr(drv, "time", _Clock([0.0]))

    def _never(*args, **kwargs):
        raise AssertionError("the bug track must not calibrate a noise band")

    monkeypatch.setattr(drv.PF, "calibrate_and_prove", _never)
    d = _loop_driver(
        tmp_path,
        git,
        caps=drv.BudgetCaps(max_cycles=1),
        profile=_Profile(track=TRACK_BUG),
    )
    assert d.run(dry_run=False, preflight=True).cycles == 1


def test_preflight_can_be_forced_off_on_a_real_run(tmp_path, git, monkeypatch):
    monkeypatch.setattr(drv, "time", _Clock([0.0]))
    monkeypatch.setattr(
        drv.PF,
        "calibrate_and_prove",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    d = _loop_driver(tmp_path, git, caps=drv.BudgetCaps(max_cycles=1))
    assert d.run(dry_run=False, preflight=False).cycles == 1


def test_the_cycle_index_resumes_from_the_archive(tmp_path, git):
    d = _loop_driver(tmp_path, git)
    d.archive.append_row({"cycle": 11, "cand_id": "old", "status": "kept"})
    seen: list = []
    d.run_cycle = lambda cycle: seen.append(cycle) or 0
    d.run(dry_run=True)
    assert seen == [12]


# ─────────────────────────── the CLI ───────────────────────────


def test_the_bare_cli_prints_a_dry_plan(capsys):
    assert drv.main([]) == 0
    out = capsys.readouterr().out
    assert "DRY PLAN" in out
    assert "DRAFT (unpublished) CR" in out


def test_go_without_a_profile_explains_itself(capsys):
    assert drv.main(["--go"]) == 0
    assert "requires a configured Target Profile" in capsys.readouterr().out


def test_the_cli_forwards_budget_flags_into_the_dry_run(monkeypatch):
    seen: dict = {}

    def _dry(args, caps, log):
        seen.update({"caps": caps, "clone": args.clone})
        return 7

    monkeypatch.setattr(drv, "_run_dry", _dry)
    rc = drv.main(["--dry-run", "--max-cycles", "3", "--max-hours", "0.5", "--quiesce", "1"])
    assert rc == 7
    assert seen["caps"].max_cycles == 3
    assert seen["caps"].max_hours == 0.5
    assert seen["caps"].quiesce_after == 1


def test_the_cli_reads_sys_argv_when_given_no_list(monkeypatch):
    monkeypatch.setattr(drv.sys, "argv", ["driver", "--dry-run"])
    monkeypatch.setattr(drv, "_run_dry", lambda args, caps, log: 3)
    assert drv.main(None) == 3


def test_build_logger_is_idempotent():
    first = drv._build_logger()
    assert len(first.handlers) == 1
    assert drv._build_logger() is first
    assert len(first.handlers) == 1


def test_run_dry_builds_a_throwaway_clone_and_honors_data_dir(tmp_path, git, monkeypatch, capsys):
    root = tmp_path / "ephemeral"
    root.mkdir()
    monkeypatch.setattr(drv, "tempfile", types.SimpleNamespace(mkdtemp=lambda prefix="": str(root)))
    monkeypatch.setattr(drv.Driver, "run", lambda self, **kwargs: drv.Stats(cycles=1))
    args = types.SimpleNamespace(data_dir=tmp_path / "data")

    assert drv._run_dry(args, drv.BudgetCaps(max_cycles=1), LOG) == 0

    assert (root / "clone" / "src" / "mesh_pkg" / "__init__.py").exists()
    assert git.seen("init -q -b auto_improvement/trunk-base")
    assert git.seen("remote add origin DISABLED_NO_PUSH")
    out = capsys.readouterr().out
    assert str(tmp_path / "data" / "results") in out


def test_run_dry_falls_back_to_an_ephemeral_data_dir(tmp_path, git, monkeypatch, capsys):
    root = tmp_path / "ephemeral2"
    root.mkdir()
    monkeypatch.setattr(drv, "tempfile", types.SimpleNamespace(mkdtemp=lambda prefix="": str(root)))
    monkeypatch.setattr(drv.Driver, "run", lambda self, **kwargs: drv.Stats())
    args = types.SimpleNamespace(data_dir=None)

    assert drv._run_dry(args, drv.BudgetCaps(), LOG) == 0
    assert str(root / "data" / "results") in capsys.readouterr().out
