"""``diff_signals.py --check-body`` and the Proof line (exit 24).

CI's check is a warning until ``ENFORCE_FROM`` names a date, so the local run
prints the same findings and still exits 0. Once the base ref's script turns red,
the findings are exit 24. Locally the script also compares a ``PROVEN`` line with
the record ``prove.py`` wrote for the same tree, which CI cannot see. The script
is read from ``origin/<base>`` (a branch cannot weaken it) and a base without it is
skipped. The line rules live in test_proof_line_check.py, the record in
test_prepare_pr_prove_line.py.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from git_maintenance_helpers import git_maintenance_off_entries
from skill_script_helpers import load_skill_script

from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS

ROOT = Path(__file__).resolve().parent.parent
PREPARE_PR = ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "kirocrew-prepare-pr"
SCRIPT = PREPARE_PR / "scripts" / "diff_signals.py"
PROVE = PREPARE_PR / "scripts" / "prove.py"
PROOF = ROOT / ".github" / "scripts" / "proof_line_check.py"

ds = load_skill_script("prepare_pr_diff_signals_proof_under_test", SCRIPT)

BODY_HEAD = (
    "## What changed\n\nmod.py reads the last char. test_mod.py pins it. .github is weakened.\n\n"
    # A fix body CI's PR Hygiene accepts, so only the proof check decides the exit.
    "## Pattern harvest\n\nNot generalizable: test fixture\n\n"
)
LINE_RE = re.compile(r"^Proof: PROVEN .+ \(head [0-9a-f]{12}\)$", re.MULTILINE)


def _git_env() -> dict[str, str]:
    # Location variables and a parent's `git -c key=value` (GIT_CONFIG_PARAMETERS, e.g. a
    # hooks path) must not reach the throwaway repositories' commits.
    drop = _GIT_LOCATION_VARS | {"GIT_PREFIX", "GIT_CONFIG_PARAMETERS"}
    env = {k: v for k, v in os.environ.items() if k not in drop}
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
    out = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_git_env(),
    )
    return out.stdout.strip()


def _make_repo(
    tmp_path: Path, *, enforce_from: str | None, subject: str = "fix: last char", script=True
) -> Path:
    """origin/main holds a buggy module; the branch fixes it with a test, then
    weakens its own copy of the check."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "mod.py").write_text("def last_char(s):\n    return s[len(s) - 2]\n")
    (repo / "test_mod.py").write_text(
        "from mod import last_char\n\n\ndef test_smoke():\n    assert last_char('abc') == 'b'\n"
    )
    if script:
        (repo / ".github" / "scripts").mkdir(parents=True)
        text = PROOF.read_text(encoding="utf-8")
        if enforce_from:
            text = text.replace(
                "ENFORCE_FROM: str | None = None", f'ENFORCE_FROM: str | None = "{enforce_from}"'
            )
            assert enforce_from in text
        (repo / ".github" / "scripts" / PROOF.name).write_text(text, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "mod.py").write_text("def last_char(s):\n    return s[len(s) - 1]\n")
    (repo / "test_mod.py").write_text(
        "from mod import last_char\n\n\ndef test_last():\n    assert last_char('abc') == 'c'\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", subject)
    if script:
        (repo / ".github" / "scripts" / PROOF.name).write_text("import sys\nsys.exit(0)\n")
        _git(repo, "commit", "-q", "-am", "weaken")
    return repo


def _prove(repo: Path) -> str:
    """Run the real prove.py and return the Proof line it prints."""
    proc = subprocess.run(
        [sys.executable, "-B", str(PROVE), "--base", "origin/main"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_git_env(),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    match = LINE_RE.search(proc.stdout)
    assert match, proc.stdout
    return match.group(0)


def _run(repo: Path, body: str) -> tuple[int, str]:
    (repo / ".git" / ds.BODY_FILENAME).write_text(body, encoding="utf-8")
    env = _git_env()
    env.pop("GITHUB_ACTIONS", None)
    proc = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "main", "--check-body"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _tests(line: str) -> str:
    return f"## Tests\n\ntest_mod.py pins the boundary.\n\n{line}\n"


# ------------------------------------------------------------ green path


def test_enforced_a_line_pasted_from_prove_is_exit_zero(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, enforce_from="2000-01-01")
    line = _prove(repo)
    rc, out = _run(repo, BODY_HEAD + _tests(line))
    assert rc == 0, out
    assert "Proof line present." in out


def test_enforced_a_not_provable_line_is_exit_zero(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, enforce_from="2000-01-01")
    rc, out = _run(repo, BODY_HEAD + _tests("Proof: not provable -- test-only change"))
    assert rc == 0, out


def test_enforced_but_not_a_fix_is_exit_zero(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, enforce_from="2000-01-01", subject="feat: last char")
    rc, out = _run(repo, BODY_HEAD + "## Tests\n\nx\n")
    assert rc == 0, out


def test_base_without_the_script_skips_the_check(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, enforce_from=None, script=False)
    body = (
        "## What changed\n\nmod.py and test_mod.py.\n\n## Tests\n\nx\n\n"
        "## Pattern harvest\n\nNot generalizable: test fixture\n"
    )
    rc, out = _run(repo, body)
    assert rc == 0, out
    assert "PROOF LINE" not in out and "Proof line" not in out


# -------------------------------------------------------------- red path


def test_warn_mode_prints_findings_and_exits_zero(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, enforce_from=None)
    rc, out = _run(repo, BODY_HEAD + "## Tests\n\nAdded tests.\n")
    assert rc == 0, out
    assert "PROOF LINE (warning):" in out


def test_enforced_missing_line_is_exit_twenty_four(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, enforce_from="2000-01-01")
    rc, out = _run(repo, BODY_HEAD + "## Tests\n\nAdded tests.\n")
    assert rc == 24, out
    assert "PROOF LINE (error):" in out


def test_enforced_line_outside_tests_is_exit_twenty_four(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, enforce_from="2000-01-01")
    line = _prove(repo)
    rc, out = _run(repo, BODY_HEAD + f"{line}\n\n## Tests\n\nAdded tests.\n")
    assert rc == 24, out
    assert "outside '## Tests'" in out


def test_enforced_invented_proven_line_is_exit_twenty_four(tmp_path: Path) -> None:
    """The shape is fine and CI would pass it; the record for this tree does not exist."""
    repo = _make_repo(tmp_path, enforce_from="2000-01-01")
    head = _git(repo, "rev-parse", "HEAD")[:12]
    rc, out = _run(repo, BODY_HEAD + _tests(f"Proof: PROVEN test_mod.py::test_last (head {head})"))
    assert rc == 24, out
    assert "no prove.py record" in out


def test_enforced_stale_proven_line_is_exit_twenty_four(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, enforce_from="2000-01-01")
    line = _prove(repo)
    (repo / "mod.py").write_text("def last_char(s):\n    return s[-1]\n")
    _git(repo, "commit", "-q", "-am", "rewrite")
    rc, out = _run(repo, BODY_HEAD + _tests(line))
    assert rc == 24, out
    assert "different tree" in out


def test_warn_mode_still_prints_a_stale_line_and_exits_zero(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, enforce_from=None)
    head = _git(repo, "rev-parse", "HEAD")[:12]
    rc, out = _run(repo, BODY_HEAD + _tests(f"Proof: PROVEN test_mod.py::test_last (head {head})"))
    assert rc == 0, out
    assert "no prove.py record" in out


def test_a_lower_code_wins_over_the_proof_code(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, enforce_from="2000-01-01")
    rc, out = _run(repo, "## What changed\n\nnothing named here.\n\n## Tests\n\nx\n")
    assert rc == 20, out
    assert "PROOF LINE (error):" in out  # still printed: all checks run before any exit


# ---------------------------------------------------------------- wiring


def test_the_exit_code_is_documented_where_agents_read_it() -> None:
    assert "24 Proof line" in ds.__doc__
    skill = (PREPARE_PR / "SKILL.md").read_text(encoding="utf-8")
    assert "23 sibling list · 24 proof line" in skill
    assert "**24**: fix or re-run `prove.py`" in skill
    assert "11 frontend tests" in skill


def test_the_script_and_record_names_match_what_prove_writes() -> None:
    prove = load_skill_script("prove_for_diff_signals_proof_test", PROVE)
    assert ds.PROOF_CHECK == ".github/scripts/proof_line_check.py"
    assert (ROOT / ds.PROOF_CHECK).is_file()
    assert ds.PROVE_RECORD == prove.RECORD_FILENAME
