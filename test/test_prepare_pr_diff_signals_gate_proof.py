"""``diff_signals.py --check-body`` exit 25: a ``Gate proof:`` line the proof contradicts.

The check is advisory (RFC decision D7): a body with no line never fails, and a line
that matches the recorded proof for the tree passes. Only a line that is present and
wrong, or present with no proof behind it, returns 25.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from git_maintenance_helpers import git_maintenance_off_entries
from skill_script_helpers import load_skill_script

from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = (
    ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "kirocrew-prepare-pr"
    / "scripts"
)
SCRIPT = SCRIPTS / "diff_signals.py"
ds = load_skill_script("prepare_pr_diff_signals_gate_proof_under_test", SCRIPT)
gate = load_skill_script("prepare_pr_pre_push_gate_for_diff_signals", SCRIPTS / "pre_push_gate.py")

BODY = "## What changed\n\nmine.py sets x.\n\n## Tests\n\n{}\n"


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}
    env.update(
        {
            "GIT_TEMPLATE_DIR": "",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
    )
    # Pointing GIT_CONFIG_GLOBAL away drops the suite's global file, so the
    # maintenance entries go on the env this helper built.
    env.update(git_maintenance_off_entries(env))
    return env


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_git_env(),
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "mine.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "change")
    return repo


def _check(repo: Path, body: str) -> tuple[int, str]:
    (repo / ".git" / ds.BODY_FILENAME).write_text(body, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "main", "--check-body"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_git_env(),
    )
    return proc.returncode, proc.stdout + proc.stderr


def _record(repo: Path, **over) -> dict:
    proof = {
        "version": 1,
        "tree": _git(repo, "rev-parse", "HEAD^{tree}"),
        "ran": ["black", "isort"],
        "skipped": [],
        **over,
    }
    (repo / ".git" / gate.PROOF_FILENAME).write_text(json.dumps(proof), encoding="utf-8")
    return proof


def test_a_body_with_no_gate_proof_line_passes(repo: Path) -> None:
    rc, out = _check(repo, BODY.format("Ran the tests by hand."))
    assert rc == 0, out
    assert "GATE PROOF" not in out


def test_a_body_with_no_line_passes_even_when_a_proof_exists(repo: Path) -> None:
    _record(repo)
    rc, _ = _check(repo, BODY.format("Ran the tests by hand."))
    assert rc == 0


def test_a_line_that_matches_the_recorded_proof_passes(repo: Path) -> None:
    line = gate.proof_line(_record(repo))
    rc, out = _check(repo, BODY.format(line))
    assert rc == 0, out


def test_a_line_that_contradicts_the_recorded_proof_exits_twenty_five(repo: Path) -> None:
    _record(repo)
    rc, out = _check(repo, BODY.format("Gate proof: tree aaaaaaaaaaaa; ran 9; skipped none"))
    assert rc == 25
    assert "GATE PROOF:" in out and "the recorded proof says" in out


def test_a_line_with_no_recorded_proof_exits_twenty_five(repo: Path) -> None:
    rc, out = _check(repo, BODY.format("Gate proof: tree aaaaaaaaaaaa; ran 2; skipped none"))
    assert rc == 25
    assert "no recorded proof for this tree" in out


def test_a_proof_for_an_older_tree_does_not_back_a_line(repo: Path) -> None:
    line = gate.proof_line(_record(repo))
    (repo / "mine.py").write_text("x = 2\n")
    _git(repo, "commit", "-qam", "again")
    rc, _ = _check(repo, BODY.format(line))
    assert rc == 25


def test_the_lower_codes_win_over_twenty_five_and_the_gate_proof_line_still_prints(
    repo: Path,
) -> None:
    """20 (an area the body never names) is returned, and the 25 finding is still printed."""
    _record(repo)
    rc, out = _check(repo, "## Tests\n\nGate proof: tree aaaaaaaaaaaa; ran 9; skipped none\n")
    assert rc == 20
    assert "UNACCOUNTED" in out
    assert "GATE PROOF:" in out and "the recorded proof says" in out


def test_a_too_long_body_and_a_contradicting_line_both_print_and_21_wins(repo: Path) -> None:
    _record(repo)
    prose = "mine.py sets x. " + "word " * ds.WORD_LIMIT
    line = "Gate proof: tree aaaaaaaaaaaa; ran 9; skipped none"
    rc, out = _check(repo, "## What changed\n\n{}\n\n## Tests\n\n{}\n".format(prose, line))
    assert rc == 21
    assert "TOO LONG" in out and "GATE PROOF:" in out


def test_a_template_failure_and_a_contradicting_line_both_print_and_22_wins(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The template check is CI's script, read from the base; a stub that fails stands in."""
    _record(repo)
    body = (
        "## What changed\n\nmine.py sets x.\n\nGate proof: tree aaaaaaaaaaaa; ran 9; skipped none\n"
    )
    (repo / ".git" / ds.BODY_FILENAME).write_text(body, encoding="utf-8")

    def failing_template(body: str, base: str) -> int:
        print("PR template: stub failure")
        return 22

    monkeypatch.setattr(ds, "template_check", failing_template)
    monkeypatch.chdir(repo)
    for name, value in _git_env().items():
        monkeypatch.setenv(name, value)
    assert ds.main(["diff_signals.py", "main", "--check-body"]) == 22
    out = capsys.readouterr().out
    assert "stub failure" in out and "GATE PROOF:" in out


def test_the_script_documents_exit_twenty_five() -> None:
    assert "25 a Gate proof: line" in SCRIPT.read_text(encoding="utf-8")
