"""``install.sh`` must say which release lane a from-source install follows.

``install.sh`` builds the git clone it sits in, so the install tracks that
clone's checked-out branch -- and ``main`` is the Nightly lane. A box set up by ``git clone`` + ``bash install.sh``
on the assumption that this is the end-user install lands on ``0.X.0.dev``
builds while the desktop app sits on Stable, unless the installer says so.

These tests lift ``source_checkout_ref`` / ``source_lane_notice`` (and the tiny
print helpers they call) verbatim out of the real script and run them against
throwaway git clones, rather than executing the whole installer.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"
CLI_SH_URL = "https://download.crew.kiro.dev/cli.sh"
CLI_SH_LINE = f"curl -fsSL {CLI_SH_URL} | sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="POSIX shell installer; not exercised on native Windows"
)


def _function(text: str, name: str) -> str:
    start = text.index(f"{name}() {{")
    end = text.index("\n}", start) + len("\n}")
    return text[start:end]


def _one_liner(text: str, name: str) -> str:
    start = text.index(f"{name}()")
    return text[start : text.index("\n", start)]


def _helper_source() -> str:
    """The lane helpers plus the print helpers they call, lifted verbatim."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    parts = [
        'BOLD="" RESET="" DIM="" YELLOW="" GREEN="" CYAN=""',
        _one_liner(text, "warn"),
        _one_liner(text, "detail"),
        _function(text, "source_checkout_ref"),
        _function(text, "source_lane_notice"),
    ]
    return "\n".join(parts) + "\n"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        timeout=30,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    )


def _clone(tmp_path: Path) -> Path:
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    return repo


def _notice(app_dir: Path) -> tuple[str, str]:
    """Return (source_checkout_ref, source_lane_notice) output for *app_dir*."""
    if shutil.which("git") is None or shutil.which("bash") is None:  # pragma: no cover
        pytest.skip("git and bash are required")
    script = _helper_source() + 'source_checkout_ref; echo "---"; source_lane_notice\n'
    result = subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "KIROCREW_APP_DIR": str(app_dir)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    ref, _, notice = result.stdout.partition("---\n")
    return ref.strip(), notice


def test_main_checkout_is_named_as_the_nightly_lane(tmp_path: Path) -> None:
    ref, notice = _notice(_clone(tmp_path))
    assert ref == "branch:main"
    assert "Nightly" in notice
    assert CLI_SH_URL in notice


def test_feature_branch_is_named_and_not_called_nightly(tmp_path: Path) -> None:
    repo = _clone(tmp_path)
    _git(repo, "switch", "-q", "-c", "release/0.10")
    ref, notice = _notice(repo)
    assert ref == "branch:release/0.10"
    assert "release/0.10" in notice
    assert "Nightly" not in notice
    assert CLI_SH_URL in notice


def test_detached_tag_is_named_as_pinned(tmp_path: Path) -> None:
    repo = _clone(tmp_path)
    _git(repo, "tag", "v0.10.0")
    _git(repo, "checkout", "-q", "--detach", "v0.10.0")
    ref, notice = _notice(repo)
    assert ref == "detached:v0.10.0"
    assert "pinned at v0.10.0" in notice
    assert CLI_SH_URL in notice


def test_not_a_git_tree_still_points_at_the_wheel_install(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    ref, notice = _notice(plain)
    assert ref == "unknown"
    assert "from-source install" in notice
    assert CLI_SH_URL in notice


def test_notice_runs_at_start_and_end_of_the_install() -> None:
    """Called at preflight (before minutes of work) and again beside the summary."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    body = text[text.index("# ── Pre-flight ──") :]
    assert body.count("\nsource_lane_notice\n") == 2
    # The header names the end-user install too, for anyone reading the script.
    header = text[: text.index("set -euo pipefail")]
    assert CLI_SH_LINE in header
    assert "Nightly" in header


def test_update_hint_is_printed_only_for_a_branch_checkout() -> None:
    """A detached checkout cannot be updated, so it never gets the reset hint."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    footer = text[text.rindex('case "$(source_checkout_ref)" in') :]
    footer = footer[: footer.index("\nesac")]
    branch_arm, _, detached_arm = footer.partition("detached:*)")
    assert "branch:*)" in branch_arm
    assert "Update anytime" in branch_arm and "--force" in branch_arm
    assert "Update anytime" not in detached_arm and "--force" not in detached_arm
    assert text.count("Update anytime") == 1
