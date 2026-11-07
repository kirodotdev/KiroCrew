"""A project evidence refusal must stop both real Driver keep paths."""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ..spine import candidate_admission as A
from ..spine import driver as D
from ..spine import ledger as L
from ..spine.contracts import BugGateResult, Candidate, Proposal
from ..spine.keeper import KEPT


@pytest.fixture
def checker(tmp_path: Path) -> Path:
    script = tmp_path / "checker.py"
    script.write_text(
        "import hashlib,json,sys\n"
        "raw=sys.stdin.buffer.read()\n"
        "request=json.loads(raw)\n"
        "mode=sys.argv[1]\n"
        "response={'schema_version':'candidate-admission-result-v1',"
        "'request_sha256':hashlib.sha256(raw).hexdigest(),"
        "'status':'admitted','assessment_id':'a'*64,'reason':'calibrated'}\n"
        "if mode in {'uncalibrated','stale','invalid_report','scope_changed'}:\n"
        " response.update(status='rejected',reason=mode,assessment_id=None)\n"
        "elif mode=='cached': response['request_sha256']='0'*64\n"
        "elif mode=='no_identity': response['assessment_id']=None\n"
        "elif mode=='bad_json': print('broken');sys.exit(0)\n"
        "elif mode=='nonzero': sys.exit(1)\n"
        "elif mode=='oversize': print('x'*65537);sys.exit(0)\n"
        "elif mode=='boolean_only': response={'allowed':True}\n"
        'elif mode==\'duplicate\': print(\'{"status":"admitted","status":"rejected"}\');sys.exit(0)\n'
        "print(json.dumps(response))\n",
        encoding="utf-8",
    )
    return script


def make_driver(tmp_path: Path, checker: Path, mode: str, monkeypatch):
    driver = D.Driver.__new__(D.Driver)
    driver.clone = tmp_path / "clone"
    worktree = tmp_path / "candidate"
    driver.clone.mkdir()
    worktree.mkdir()
    driver.branch = "feature/test"
    driver.profile = SimpleNamespace(
        id="quality-fixture",
        ruler=SimpleNamespace(primary_name="stage_fixture", direction="maximize"),
    )
    driver.head_sha = Mock(return_value="b" * 40)
    driver.candidate_admission = A.CandidateAdmission.from_argv(
        [sys.executable, "-I", str(checker), mode]
    )
    driver.stats = D.Stats()
    driver.ledger = L.Ledger(tmp_path / "ledger.jsonl")
    driver.log = logging.getLogger(__name__)
    driver.archive = Mock()
    driver.archive.save_candidate.return_value = "fixture.diff"
    driver.measurer = SimpleNamespace(reps=2)
    driver.pr_pipeline = Mock()
    driver._publishing_is_halted = Mock(return_value=False)
    driver._commit_winner_provisional = Mock(side_effect=ReachedKeepBoundary)
    driver._commit_bug_winner_provisional = Mock(side_effect=ReachedKeepBoundary)
    git = Mock(return_value=SimpleNamespace(stdout="b" * 40))
    monkeypatch.setattr(D, "_git", git)
    winner = Proposal(
        "candidate", Candidate("perf", "module::function"), worktree, "candidate", "fixture", "diff"
    )
    return driver, winner, git


class ReachedKeepBoundary(Exception):
    """Stop an admitted fixture before any Git mutation."""


def apply_winner(driver, winner, track):
    winner.candidate.kind = track
    if track == "perf":
        return driver._apply_verdict(
            1,
            "b" * 40,
            SimpleNamespace(keep=True, winner=winner, reason="fixture", measurement=None),
            [(winner, KEPT, None)],
            1,
            {},
        )
    return driver._apply_bug_winner(1, winner, BugGateResult(passed=True, reason="bug_filed"))


@pytest.mark.parametrize("track", ["perf", "bug"])
@pytest.mark.parametrize("reason", ["uncalibrated", "stale", "invalid_report", "scope_changed"])
def test_project_refusal_never_reaches_commit_or_draft(
    tmp_path, checker, monkeypatch, track, reason
):
    driver, winner, git = make_driver(tmp_path, checker, reason, monkeypatch)
    apply_winner(driver, winner, track)
    driver._commit_winner_provisional.assert_not_called()
    driver._commit_bug_winner_provisional.assert_not_called()
    driver.pr_pipeline.emit_perf.assert_not_called()
    driver.pr_pipeline.emit_bug.assert_not_called()
    git.assert_not_called()
    assert driver.stats.kept == 0
    assert driver.stats.not_kept == 1
    entries = [json.loads(line) for line in driver.ledger.path.read_text().splitlines()]
    assert entries[-1]["status"] == L.STATUS_FAILED_VERIFY
    assert reason in entries[-1]["note"] and "request=" in entries[-1]["note"]
    if track == "perf":
        assert driver.archive.append_row.call_args.args[0]["status"] == L.STATUS_FAILED_VERIFY
    else:
        driver.archive.append_row.assert_not_called()


@pytest.mark.parametrize("track", ["perf", "bug"])
@pytest.mark.parametrize("configured", [True, False])
def test_admitted_or_unconfigured_gate_preserves_existing_keep_path(
    tmp_path, checker, monkeypatch, track, configured
):
    driver, winner, _ = make_driver(tmp_path, checker, "admitted", monkeypatch)
    if not configured:
        driver.candidate_admission = None
    with pytest.raises(ReachedKeepBoundary):
        apply_winner(driver, winner, track)
    commit = (
        driver._commit_winner_provisional
        if track == "perf"
        else driver._commit_bug_winner_provisional
    )
    commit.assert_called_once_with(winner)


@pytest.mark.parametrize(
    "mode",
    ["cached", "no_identity", "bad_json", "nonzero", "oversize", "boolean_only", "duplicate"],
)
def test_invalid_checker_output_is_not_admission(tmp_path, checker, monkeypatch, mode):
    driver, winner, _ = make_driver(tmp_path, checker, mode, monkeypatch)
    assert not A.admit_candidate(driver, winner)
    driver._commit_winner_provisional.assert_not_called()


def test_each_check_binds_fresh_request_and_actual_candidate(tmp_path, checker, monkeypatch):
    driver, winner, _ = make_driver(tmp_path, checker, "admitted", monkeypatch)
    captured = []
    run = A.run_limited

    def capture(*args, **kwargs):
        captured.append(json.loads(kwargs["input"]))
        return run(*args, **kwargs)

    monkeypatch.setattr(A, "run_limited", capture)
    assert A.admit_candidate(driver, winner)
    driver.head_sha.return_value = "c" * 40
    winner.diff = "changed candidate"
    assert A.admit_candidate(driver, winner)
    assert captured[0]["request_id"] != captured[1]["request_id"]
    assert captured[1]["base_sha"] == "c" * 40
    assert captured[1]["patch_sha256"] == hashlib.sha256(winner.diff.encode()).hexdigest()
    assert captured[1]["metric"] == "stage_fixture"


def test_timeout_and_checker_exception_cannot_enable_keep(tmp_path, checker, monkeypatch):
    driver, winner, _ = make_driver(tmp_path, checker, "admitted", monkeypatch)
    monkeypatch.setattr(
        A, "run_limited", Mock(side_effect=subprocess.TimeoutExpired("checker", 60))
    )
    assert not A.admit_candidate(driver, winner)
    driver.candidate_admission = SimpleNamespace(
        check=Mock(side_effect=RuntimeError("unavailable"))
    )
    assert not A.admit_candidate(driver, winner)


def test_checker_cannot_run_candidate_owned_inputs(tmp_path, checker, monkeypatch):
    driver, winner, _ = make_driver(tmp_path, checker, "admitted", monkeypatch)
    driver.candidate_admission = A.CandidateAdmission.from_argv(
        [sys.executable, "-I", str(winner.worktree / "checker.py")]
    )
    run = Mock()
    monkeypatch.setattr(A, "run_limited", run)
    assert not A.admit_candidate(driver, winner)
    run.assert_not_called()


def test_head_change_during_checker_execution_is_refused(tmp_path, checker, monkeypatch):
    driver, winner, _ = make_driver(tmp_path, checker, "admitted", monkeypatch)
    driver.head_sha.side_effect = ["b" * 40, "c" * 40]
    assert not A.admit_candidate(driver, winner)
    driver._commit_winner_provisional.assert_not_called()


@pytest.mark.parametrize("configured", [True, False])
def test_backend_passes_operator_checker_to_driver(tmp_path, checker, monkeypatch, configured):
    from ..backend import runner as R

    supervisor = R.RunSupervisor.__new__(R.RunSupervisor)
    supervisor._build_runner = Mock(return_value=None)
    monkeypatch.setattr(R, "_refuse_without_gate_pytest", lambda: None)
    profile = SimpleNamespace(
        clone_path=tmp_path, isolation=SimpleNamespace(push_disabled=lambda: True)
    )
    command = [sys.executable, "-I", str(checker), "admitted"]
    config = {"candidateAdmissionCommand": command} if configured else {}
    build_driver = Mock(return_value="driver")
    assert (
        supervisor._build_driver_locked(config, lambda _cfg: profile, D.BudgetCaps, build_driver)
        == "driver"
    )
    check = build_driver.call_args.kwargs["candidate_admission"]
    assert (check.argv if check is not None else None) == (tuple(command) if configured else None)


@pytest.mark.parametrize(
    "value", ["checker", [], ["python", "checker.py"], [None], ["/python", "\0"]]
)
def test_invalid_operator_configuration_is_refused(value):
    with pytest.raises(ValueError):
        A.CandidateAdmission.from_argv(value)
