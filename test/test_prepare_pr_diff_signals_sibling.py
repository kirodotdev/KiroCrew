"""``diff_signals.py --check-body`` and the Sibling sweep (exit 23).

CI's check is a warning until ``ENFORCE_FROM`` names a date, so the local run
prints the same findings and still exits 0. Once the base ref's script turns red
for the pull request, the findings are exit 23. The script is read from
``origin/<base>`` (a branch cannot weaken it) and a base without it is skipped.
The unit-level rules of the check live in test_sibling_list_check.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from git_maintenance_helpers import git_maintenance_off_entries
from skill_script_helpers import load_skill_script

from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS

ROOT = Path(__file__).resolve().parent.parent
PREPARE_PR = ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "kirocrew-prepare-pr"
SCRIPT = PREPARE_PR / "scripts" / "diff_signals.py"
SIBLING = ROOT / ".github" / "scripts" / "sibling_list_check.py"

ds = load_skill_script("prepare_pr_diff_signals_sibling_under_test", SCRIPT)

BODY_HEAD = (
    "## What changed\n\nsend.py sends once. .github is weakened.\n\n"
    "## Tests\n\nx\n\n"
    # A fix body CI's PR Hygiene accepts, so only the sibling check decides the exit.
    "## Pattern harvest\n\nNot generalizable: test fixture\n\n"
)
GOOD_SWEEP = "## Sibling sweep\n\nSibling: src/kiro_crew/chat/other.py -- tracked in #5\n"
BAD_SWEEP = "## Sibling sweep\n\nSibling: src/kiro_crew/chat/ghost.py -- tracked in #5\n"


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


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=_git_env())


def _make_repo(tmp_path: Path, *, enforce_from: str | None, subject: str, with_script: bool = True):
    repo = tmp_path / "repo"
    (repo / "src" / "kiro_crew" / "chat").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "src" / "kiro_crew" / "chat" / "other.py").write_text("x = 1\n")
    (repo / "src" / "kiro_crew" / "chat" / "send.py").write_text("x = 1\n")
    if with_script:
        (repo / ".github" / "scripts").mkdir(parents=True)
        text = SIBLING.read_text(encoding="utf-8")
        if enforce_from:
            text = text.replace(
                "ENFORCE_FROM: str | None = None", f'ENFORCE_FROM: str | None = "{enforce_from}"'
            )
            assert enforce_from in text
        (repo / ".github" / "scripts" / SIBLING.name).write_text(text, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "src" / "kiro_crew" / "chat" / "send.py").write_text("x = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", subject)
    if with_script:
        # a branch that weakens its own copy must not weaken the check
        (repo / ".github" / "scripts" / SIBLING.name).write_text("import sys\nsys.exit(0)\n")
        _git(repo, "commit", "-q", "-am", "weaken")
    return repo


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


def test_warn_mode_prints_findings_and_exits_zero(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, enforce_from=None, subject="fix: send once")
    rc, out = _run(repo, BODY_HEAD)
    assert rc == 0, out
    assert "SIBLING SWEEP (warning):" in out
    assert "## Sibling sweep" in out


def test_enforced_missing_section_is_exit_twenty_three(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, enforce_from="2000-01-01", subject="fix: send once")
    rc, out = _run(repo, BODY_HEAD)
    assert rc == 23, out
    assert "SIBLING SWEEP (error):" in out
    assert "weaken" not in out


def test_enforced_invented_path_is_exit_twenty_three(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, enforce_from="2000-01-01", subject="fix: send once")
    rc, out = _run(repo, BODY_HEAD + BAD_SWEEP)
    assert rc == 23, out
    assert "ghost.py" in out


def test_enforced_good_list_is_exit_zero(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, enforce_from="2000-01-01", subject="fix: send once")
    rc, out = _run(repo, BODY_HEAD + GOOD_SWEEP)
    assert rc == 0, out
    assert "Sibling sweep section present." in out


def test_enforced_none_line_is_exit_zero(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, enforce_from="2000-01-01", subject="fix: send once")
    body = BODY_HEAD + "## Sibling sweep\n\nSiblings: none -- one call site, grep found it\n"
    rc, _ = _run(repo, body)
    assert rc == 0


def test_enforced_but_not_a_fix_is_exit_zero(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, enforce_from="2000-01-01", subject="feat: send once")
    rc, out = _run(repo, BODY_HEAD)
    assert rc == 0, out


def test_base_without_the_script_skips_the_check(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, enforce_from=None, subject="fix: send once", with_script=False)
    rc, out = _run(repo, BODY_HEAD)
    assert rc == 0, out
    assert "SIBLING" not in out and "Sibling sweep" not in out


def test_a_lower_code_wins_over_the_sibling_code(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, enforce_from="2000-01-01", subject="fix: send once")
    rc, out = _run(repo, "## What changed\n\nnothing named here.\n")
    assert rc == 20, out
    assert "SIBLING SWEEP (error):" in out  # still printed: all checks run before any exit


def test_the_exit_code_is_documented_where_agents_read_it() -> None:
    assert "23 Sibling sweep" in ds.__doc__
    skill = (PREPARE_PR / "SKILL.md").read_text(encoding="utf-8")
    assert "22 template section · 23 sibling list" in skill
    contract = (PREPARE_PR / "references" / "pr-description-contract.md").read_text(
        encoding="utf-8"
    )
    assert "exit 23" in contract  # the prose lives there, not in SKILL.md


@pytest.mark.parametrize("name", ["SIBLING_CHECK"])
def test_the_script_path_names_the_ci_script(name: str) -> None:
    assert getattr(ds, name) == ".github/scripts/sibling_list_check.py"
    assert (ROOT / getattr(ds, name)).is_file()
