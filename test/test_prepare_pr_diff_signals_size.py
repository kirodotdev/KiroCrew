"""``diff_signals.py`` size and age signals: printed, never an exit code.

The thresholds are CI's ``PR Size`` thresholds (``.github/scripts/pr_size.py``), so
the same pull request reads the same on the author's machine and in CI. A test keeps
the two spellings equal, as it does for ``WORD_LIMIT``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from git_maintenance_helpers import git_maintenance_off_entries
from skill_script_helpers import load_skill_script

from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS

ROOT = Path(__file__).resolve().parent.parent
PREPARE_PR = ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "kirocrew-prepare-pr"
SCRIPT = PREPARE_PR / "scripts" / "diff_signals.py"
PR_SIZE = ROOT / ".github" / "scripts" / "pr_size.py"

ds = load_skill_script("prepare_pr_diff_signals_size_under_test", SCRIPT)
pr_size = load_skill_script("pr_size_for_diff_signals_parity", PR_SIZE)


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


def test_thresholds_equal_the_ci_check_thresholds() -> None:
    assert ds.SIZE_HINT_FILES == pr_size.FILES_HINT
    assert ds.SIZE_STOP_FILES == pr_size.FILES_STOP
    assert ds.AGE_HOURS == pr_size.HOURS_AGING


@pytest.mark.parametrize("count", [0, 1, 49])
def test_under_the_hint_prints_nothing(count: int) -> None:
    assert ds.size_signal(count) is None


@pytest.mark.parametrize("count", [50, 79])
def test_the_hint_names_the_limit_and_the_remedy(count: int) -> None:
    line = ds.size_signal(count)
    assert line is not None
    assert line.startswith("! SIZE: {} files".format(count))
    assert "limit 80" in line
    assert "/pr-size approve" not in line


@pytest.mark.parametrize("count", [80, 400])
def test_at_the_limit_the_signal_names_the_owner_override(count: int) -> None:
    line = ds.size_signal(count)
    assert line is not None
    assert "{} files".format(count) in line
    assert "/pr-size approve: <reason>" in line


def test_hours_open_reads_github_timestamps() -> None:
    now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    assert ds.hours_open("2026-10-21T10:30:00Z", now) == 1
    assert ds.hours_open("2026-10-14T12:00:00Z", now) == 168
    assert ds.hours_open("2026-10-21T13:00:00+00:00", now) == 0  # a skewed clock is never negative
    assert ds.hours_open("2026-10-14T12:00:00", now) == 168  # no zone means UTC


def test_age_signal_starts_at_the_threshold() -> None:
    assert ds.age_signal(167) is None
    line = ds.age_signal(168)
    assert line is not None
    assert line.startswith("! AGE: open 168 h (7+ days)")
    assert "merge, split or close" in line


def _wide_repo(tmp_path: Path, files: int) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "pkg").mkdir()
    for index in range(files):
        (repo / "pkg" / "f{}.txt".format(index)).write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "wide")
    return repo


def _run(repo: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "main", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_git_env(),
    )
    return proc.returncode, proc.stdout + proc.stderr


@pytest.mark.parametrize(
    ("files", "expected"),
    [(49, None), (50, "! SIZE: 50 files"), (80, "! SIZE: 80 files")],
)
def test_end_to_end_size_line_never_changes_the_exit_code(
    tmp_path: Path, files: int, expected: str | None
) -> None:
    repo = _wide_repo(tmp_path, files)
    rc, out = _run(repo)
    assert rc == 0
    if expected is None:
        assert "! SIZE" not in out
    else:
        assert expected in out
        assert "(no notable structural signals" not in out


def test_age_is_opt_in_and_reads_the_pull_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without ``--age`` nothing leaves the machine; with it, an old PR prints ``! AGE``."""
    repo = _wide_repo(tmp_path, 3)
    calls: list[list[str]] = []
    real_run = ds.run

    def fake_run(args):
        if args[:1] == ["gh"]:
            calls.append(list(args))
            return 0, "2026-10-01T00:00:00Z\n", ""
        return real_run(args)

    monkeypatch.setattr(ds, "run", fake_run)
    monkeypatch.chdir(repo)

    assert ds.main(["diff_signals.py", "main"]) == 0
    assert calls == []
    assert "! AGE" not in capsys.readouterr().out

    assert ds.main(["diff_signals.py", "main", "--age"]) == 0
    assert len(calls) == 1
    assert "! AGE: open" in capsys.readouterr().out


def test_age_with_no_pull_request_prints_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _wide_repo(tmp_path, 3)
    real_run = ds.run
    monkeypatch.setattr(
        ds, "run", lambda args: (1, "", "no pull request") if args[:1] == ["gh"] else real_run(args)
    )
    monkeypatch.chdir(repo)
    assert ds.main(["diff_signals.py", "main", "--age"]) == 0
    assert "! AGE" not in capsys.readouterr().out
