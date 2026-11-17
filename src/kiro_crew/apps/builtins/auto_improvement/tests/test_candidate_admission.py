"""The opt-in evidence checker is consulted on both keep paths before anything is committed."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

from kiro_crew.testing.ids import UNALLOCATABLE_PID

from ..spine import candidate_admission as A
from ..spine import driver as D
from ..spine import ledger as L
from ..spine.contracts import BugGateResult, Candidate, Proposal
from ..spine.driver_git import _git
from ..spine.keeper import KEPT

_CHECKER = """\
import hashlib, json, os, sys
if "GITHUB_TOKEN" in os.environ:
    sys.exit(9)
raw = sys.stdin.buffer.read()
mode = sys.argv[1]
if mode.startswith("writes_"):
    open(os.path.join(sys.argv[2], "report.txt"), "w").close()
    mode = mode.removeprefix("writes_")
if mode.startswith("corrupts_"):
    with open(os.path.join(sys.argv[2], ".git", "index"), "wb") as index:
        index.write(b"garbage")
    mode = mode.removeprefix("corrupts_")
answer = {"request_sha256": hashlib.sha256(raw).hexdigest(), "status": "admitted"}
if mode == "rejected":
    answer["status"] = "rejected"
elif mode == "cached":
    answer["request_sha256"] = "0" * 64
elif mode == "bad_json":
    answer = "not an object"
elif mode == "oversize":
    answer["padding"] = "x" * 70000
elif mode == "nonzero":
    sys.exit(3)
elif mode == "deep":
    sys.stdout.write("[" * 60000)
    sys.exit(0)
print(json.dumps(answer))
"""


_IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false")


class ReachedCommit(Exception):
    """The candidate got as far as the provisional commit."""


class _Stdin(io.BytesIO):
    """A checker's stdin that keeps what was written after it is closed."""

    def close(self) -> None:
        self.request = self.getvalue()
        super().close()


def _fake_checker(monkeypatch, events: list[str], *, exits: bool, on_exit=None) -> None:
    """Stand in for the checker process: record when it ends, is killed and is reaped."""

    class Proc:
        pid = UNALLOCATABLE_PID
        returncode = None

        def __init__(self, argv, *, stdout, **kwargs):
            self.kwargs, self.out, self.stdin = kwargs, stdout, _Stdin()

        def wait(self, timeout=None):
            events.append("reaped")
            self.returncode = 0 if exits else -9
            return self.returncode

    def exited(proc, seconds):
        assert seconds == A.TIMEOUT_S and proc.kwargs["start_new_session"] is True
        events.append("exited" if exits else "timed out")
        if exits and on_exit is not None:
            on_exit(proc.stdin.request, proc.out)
        return exits

    monkeypatch.setattr(A, "popen_limited", Proc)
    monkeypatch.setattr(A, "_exited_unreaped", exited)
    monkeypatch.setattr(A, "kill_popen_tree", lambda proc: events.append("group killed"))
    monkeypatch.setattr(A, "pgroup_exists", lambda pgid: False)


def _admit(request: bytes, stdout) -> None:
    digest = hashlib.sha256(request).hexdigest()
    stdout.write(json.dumps({"request_sha256": digest, "status": "admitted"}).encode())


def _record_order(monkeypatch, driver: D.Driver, events: list[str]) -> None:
    reads = A._clone_state

    def read_clone(d):
        events.append("clone read")
        return reads(d)

    monkeypatch.setattr(A, "_clone_state", read_clone)
    monkeypatch.setattr(
        driver, "_retire_if_unsafe", Mock(side_effect=lambda stage: events.append("probe"))
    )


@pytest.fixture
def spawns(tmp_path, monkeypatch) -> list[dict]:
    """Record each sandbox request and run the checker unwrapped (the sandbox is not the
    subject here; `test/test_spawn_audit.py` pins that the spawn is routed)."""
    calls: list[dict] = []

    def _passthrough(argv, mode="standard", **kwargs):
        calls.append({"mode": mode, **kwargs})
        cleanup = tmp_path / "launcher-cleanup"
        cleanup.touch()
        return list(argv), {"PATH": "/usr/bin:/bin", "GITHUB_TOKEN": "t"}, str(cleanup)

    monkeypatch.setattr(A, "sandboxed_spawn_argv", _passthrough)
    monkeypatch.setattr(A, "write_protected_targets", lambda: ("/masked",))
    return calls


def _driver(tmp_path: Path, monkeypatch, mode: str | None) -> tuple[D.Driver, Proposal]:
    clone = tmp_path / "clone"
    clone.mkdir()
    _git(["init", "-q", "-b", "feature/x"], clone)
    _git(["commit", "-q", "--allow-empty", "-m", "base"], clone, config=_IDENTITY)
    script = tmp_path / "checker.py"
    script.write_text(_CHECKER, encoding="utf-8")
    driver = D.Driver.__new__(D.Driver)
    driver.clone = clone
    driver.branch = "feature/x"
    driver.profile = SimpleNamespace(
        id="p", ruler=SimpleNamespace(primary_name="m", direction="minimize")
    )
    driver.candidate_admission = (
        None if mode is None else (sys.executable, "-I", str(script), mode, str(clone))
    )
    monkeypatch.setattr(driver, "head_sha", Mock(return_value="b" * 40))
    driver.stats = D.Stats()
    driver.ledger = L.Ledger(tmp_path / "ledger.jsonl")
    driver.log = logging.getLogger(__name__)
    driver.archive = Mock()
    monkeypatch.setattr(driver, "measurer", SimpleNamespace(reps=1), raising=False)
    monkeypatch.setattr(driver, "_publishing_is_halted", Mock(return_value=False))
    monkeypatch.setattr(driver, "_retire_if_unsafe", Mock(return_value=False))
    monkeypatch.setattr(driver, "_reset_provisional", Mock(return_value=True))
    monkeypatch.setattr(driver, "_commit_winner_provisional", Mock(side_effect=ReachedCommit))
    monkeypatch.setattr(driver, "_commit_bug_winner_provisional", Mock(side_effect=ReachedCommit))
    monkeypatch.setattr(D, "_git", Mock(return_value=SimpleNamespace(stdout="b" * 40)))
    winner = Proposal("c1", Candidate("perf", "mod::fn"), tmp_path, "b", "d", "diff")
    return driver, winner


def _keep(driver: D.Driver, winner: Proposal, track: str) -> None:
    winner.candidate.kind = track
    if track == "perf":
        verdict = SimpleNamespace(keep=True, winner=winner, reason="win", measurement=None)
        driver._apply_verdict(1, "b" * 40, verdict, [(winner, KEPT, None)], 1, {})
    else:
        driver._apply_bug_winner(1, winner, BugGateResult(passed=True, reason="bug_filed"))


def _tip(driver: D.Driver) -> str:
    return _git(["rev-parse", "refs/heads/feature/x"], driver.clone).stdout.strip()


def _note(driver: D.Driver, winner: Proposal) -> tuple[str, str]:
    row = driver.ledger._seen[L.fingerprint(kind=winner.candidate.kind, target="mod::fn")]
    return row.status, row.note


@pytest.mark.parametrize("track", ["perf", "bug"])
def test_unset_keeps_both_tracks_as_today(tmp_path, monkeypatch, spawns, track):
    driver, winner = _driver(tmp_path, monkeypatch, None)
    with pytest.raises(ReachedCommit):
        _keep(driver, winner, track)
    assert spawns == []


@pytest.mark.parametrize("track", ["perf", "bug"])
def test_an_admitted_candidate_reaches_the_commit_through_the_strict_sandbox(
    tmp_path, monkeypatch, spawns, track
):
    driver, winner = _driver(tmp_path, monkeypatch, "admitted")
    with pytest.raises(ReachedCommit):
        _keep(driver, winner, track)
    assert spawns == [{"mode": "strict", "extra_hidden_dirs": ("/masked",)}]
    assert not (tmp_path / "launcher-cleanup").exists()


@pytest.mark.parametrize("track", ["perf", "bug"])
@pytest.mark.parametrize(
    ("mode", "why"),
    [
        ("rejected", "rejected"),
        ("cached", "invalid response"),
        ("bad_json", "invalid response"),
        ("oversize", "invalid response"),
        ("nonzero", "exit 3"),
        ("deep", "invalid response"),
    ],
)
def test_a_refusal_never_reaches_the_commit(tmp_path, monkeypatch, spawns, track, mode, why):
    driver, winner = _driver(tmp_path, monkeypatch, mode)
    _keep(driver, winner, track)
    cast(Mock, driver._commit_winner_provisional).assert_not_called()
    cast(Mock, driver._commit_bug_winner_provisional).assert_not_called()
    assert (driver.stats.kept, driver.stats.not_kept) == (0, 1)
    assert _note(driver, winner) == (L.STATUS_FAILED_VERIFY, f"candidate admission refused: {why}")


@pytest.mark.parametrize("track", ["perf", "bug"])
def test_a_refused_winner_is_archived_as_failed_verify(tmp_path, monkeypatch, spawns, track):
    driver, winner = _driver(tmp_path, monkeypatch, "rejected")
    _keep(driver, winner, track)
    row = cast(Mock, driver.archive.append_row).call_args.args[0]
    detail = cast(Mock, driver.archive.save_candidate).call_args.kwargs["detail"]
    assert row["status"] == detail["status"] == L.STATUS_FAILED_VERIFY


def test_head_moving_during_the_check_refuses(tmp_path, monkeypatch, spawns):
    driver, winner = _driver(tmp_path, monkeypatch, "admitted")
    monkeypatch.setattr(driver, "head_sha", Mock(side_effect=["b" * 40, "c" * 40]))
    assert A.admit_candidate(driver, winner) is False
    assert (
        _note(driver, winner)[1]
        == "candidate admission refused: the clone changed during the check"
    )
    cast(Mock, driver._reset_provisional).assert_called_once_with(_tip(driver))


def test_a_branch_moved_during_the_check_refuses(tmp_path, monkeypatch, spawns):
    driver, winner = _driver(tmp_path, monkeypatch, "admitted")
    tip = _tip(driver)

    def commit_then_admit(request, stdout):
        _git(["commit", "-q", "--allow-empty", "-m", "x"], driver.clone, config=_IDENTITY)
        _admit(request, stdout)

    _fake_checker(monkeypatch, [], exits=True, on_exit=commit_then_admit)
    assert A.admit_candidate(driver, winner) is False
    assert (
        _note(driver, winner)[1]
        == "candidate admission refused: the clone changed during the check"
    )
    cast(Mock, driver._reset_provisional).assert_called_once_with(tip)


@pytest.mark.parametrize("mode", ["writes_admitted", "writes_rejected"])
def test_a_file_written_into_the_clone_refuses_whatever_the_answer(
    tmp_path, monkeypatch, spawns, mode
):
    driver, winner = _driver(tmp_path, monkeypatch, mode)
    assert A.admit_candidate(driver, winner) is False
    assert (
        _note(driver, winner)[1]
        == "candidate admission refused: the clone changed during the check"
    )
    cast(Mock, driver._reset_provisional).assert_called_once_with(_tip(driver))


def test_a_file_left_by_a_refused_check_refuses_the_next_keep(tmp_path, monkeypatch, spawns):
    driver, winner = _driver(tmp_path, monkeypatch, "writes_rejected")
    _keep(driver, winner, "bug")
    script = str(tmp_path / "checker.py")
    driver.candidate_admission = (sys.executable, "-I", script, "admitted", str(driver.clone))
    second = Proposal("c2", Candidate("bug", "mod::other"), tmp_path, "b", "d", "diff")
    _keep(driver, second, "bug")
    cast(Mock, driver._commit_bug_winner_provisional).assert_not_called()
    assert len(spawns) == 1
    row = driver.ledger._seen[L.fingerprint(kind="bug", target="mod::other")]
    assert row.note == "candidate admission refused: the clone has uncommitted changes"


def test_an_unsafe_clone_after_the_check_is_retired_before_any_more_git(
    tmp_path, monkeypatch, spawns
):
    driver, winner = _driver(tmp_path, monkeypatch, "admitted")

    def retire(stage):
        driver._repository_retired = True
        return True

    monkeypatch.setattr(driver, "_retire_if_unsafe", Mock(side_effect=retire))
    reads = Mock(side_effect=A._clone_state)
    monkeypatch.setattr(A, "_clone_state", reads)
    second = Proposal("c2", Candidate("bug", "mod::other"), tmp_path, "b", "d", "diff")
    for prop in (winner, second):
        _keep(driver, prop, "bug")
    cast(Mock, driver._commit_bug_winner_provisional).assert_not_called()
    cast(Mock, driver._reset_provisional).assert_not_called()
    cast(Mock, driver._retire_if_unsafe).assert_called_once_with("candidate admission")
    assert reads.call_count == 1
    assert len(spawns) == 1
    assert _note(driver, winner)[1] == (
        "candidate admission refused: the clone's git metadata became unsafe during the check"
    )
    row = driver.ledger._seen[L.fingerprint(kind="bug", target="mod::other")]
    assert row.note == "candidate admission refused: the clone was retired"


def test_a_check_that_leaves_git_unable_to_read_the_clone_refuses(tmp_path, monkeypatch, spawns):
    driver, winner = _driver(tmp_path, monkeypatch, "corrupts_admitted")
    tip = _tip(driver)
    assert A.admit_candidate(driver, winner) is False
    assert (
        _note(driver, winner)[1]
        == "candidate admission refused: the clone changed during the check"
    )
    cast(Mock, driver._reset_provisional).assert_called_once_with(tip)
    second = Proposal("c2", Candidate("perf", "mod::other"), tmp_path, "b", "d", "diff")
    assert A.admit_candidate(driver, second) is False
    assert len(spawns) == 1
    row = driver.ledger._seen[L.fingerprint(kind="perf", target="mod::other")]
    assert row.note == "candidate admission refused: git could not read the clone"


def test_a_checker_that_cannot_run_refuses(tmp_path, monkeypatch, spawns):
    driver, winner = _driver(tmp_path, monkeypatch, "admitted")
    monkeypatch.setattr(A, "popen_limited", Mock(side_effect=RuntimeError("no sandbox backend")))
    assert A.admit_candidate(driver, winner) is False
    assert _note(driver, winner)[1] == "candidate admission refused: could not run"


def test_a_timed_out_checker_group_is_killed_before_the_clone_is_checked(
    tmp_path, monkeypatch, spawns
):
    """A timeout of the launcher alone would leave the checker it forked running, with
    write access to the clone, while the clone is compared and rolled back."""
    driver, winner = _driver(tmp_path, monkeypatch, "admitted")
    events: list[str] = []
    _fake_checker(monkeypatch, events, exits=False)
    _record_order(monkeypatch, driver, events)
    assert A.admit_candidate(driver, winner) is False
    assert _note(driver, winner)[1] == "candidate admission refused: timed out"
    assert events == ["clone read", "timed out", "group killed", "reaped", "probe", "clone read"]


def test_an_admitting_checker_group_is_killed_before_the_clone_is_checked(
    tmp_path, monkeypatch, spawns
):
    """A checker that answers and exits can leave a process behind that writes into the
    clone after it was checked, into the keep's ``git add -A``."""
    driver, winner = _driver(tmp_path, monkeypatch, "admitted")
    events: list[str] = []
    _fake_checker(monkeypatch, events, exits=True, on_exit=_admit)
    _record_order(monkeypatch, driver, events)
    assert A.admit_candidate(driver, winner) is True
    assert events == ["clone read", "exited", "group killed", "reaped", "probe", "clone read"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_a_group_left_after_the_checker_was_reaped_is_killed(monkeypatch):
    proc = Mock(pid=UNALLOCATABLE_PID)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(A, "kill_popen_tree", Mock())
    monkeypatch.setattr(A, "pgroup_exists", lambda pgid: pgid == UNALLOCATABLE_PID)
    monkeypatch.setattr(A, "kill_process_group", lambda pgid, sig: killed.append((pgid, sig)))
    A._stop_group(proc)
    proc.wait.assert_called_once_with()
    assert killed == [(UNALLOCATABLE_PID, A.SIGKILL)]


@pytest.mark.skipif(not hasattr(os, "waitid"), reason="needs os.waitid")
def test_the_exit_wait_leaves_the_checker_unreaped(monkeypatch):
    """Unreaped, the checker's pid still names its group when that group is killed."""
    answers = iter([None, SimpleNamespace(si_pid=UNALLOCATABLE_PID)])
    calls: list[tuple[int, int, int]] = []

    def waitid(idtype, pid, options):
        calls.append((idtype, pid, options))
        return next(answers)

    monkeypatch.setattr(A.os, "waitid", waitid)
    proc = Mock(pid=UNALLOCATABLE_PID)
    assert A._exited_unreaped(proc, A.TIMEOUT_S) is True
    assert len(calls) == 2
    assert all(c[:2] == (os.P_PID, UNALLOCATABLE_PID) and c[2] & os.WNOWAIT for c in calls)
    proc.wait.assert_not_called()


def test_a_configured_checker_refuses_to_start_where_it_is_not_supported(monkeypatch):
    monkeypatch.setattr(A, "SUPPORTED", False)
    assert A.parse_command(None) is None
    with pytest.raises(ValueError, match="not supported on Windows"):
        A.parse_command([str(Path(sys.executable).resolve())])


@pytest.mark.parametrize("value", ["/bin/check", [], ["relative/check"], ["/bin/check", 1]])
def test_a_malformed_command_refuses_to_start(value, monkeypatch):
    monkeypatch.setattr(A, "SUPPORTED", True)
    with pytest.raises(ValueError, match="candidateAdmissionCommand"):
        A.parse_command(value)
