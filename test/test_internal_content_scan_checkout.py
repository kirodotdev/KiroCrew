"""Internal Content Scan diffs the same lines from a two-commit checkout as from a full clone.

`internal-content-scan.yml` checks out at `fetch-depth: 2` and its "Compute the
diff" step fetches whichever end of the two-dot range is missing. A full-history
checkout downloads every branch with every blob, and on a slow runner that
outlasted the job's 10-minute budget.

The invariant: for every event shape the callee serves (push, a branch's first
push, pull_request, merge_group), the step's real `run:` body writes the same
`change.diff` a full clone gives, and an end it cannot fetch fails the step
instead of producing a smaller diff.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from git_maintenance_helpers import git_global_config_text, git_maintenance_off_entries

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "internal-content-scan.yml"
DIFF_STEP = "Compute the diff"
ZERO = "0" * 40

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None or shutil.which("git") is None,
    reason="runs the workflow's bash step against real git; Linux/macOS runners cover it",
)


def _steps() -> list[dict]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["scan"]["steps"]


def _diff_step() -> dict:
    (step,) = [s for s in _steps() if s.get("name") == DIFF_STEP]
    return step


def test_the_checkout_is_two_commits_and_the_diff_step_comes_right_after() -> None:
    steps = _steps()
    assert str(steps[0].get("uses", "")).startswith("actions/checkout@")
    assert steps[0]["with"] == {"persist-credentials": False, "fetch-depth": 2}
    assert steps[1].get("name") == DIFF_STEP
    # No partial-clone filter anywhere: a lazily fetched blob that fails to
    # arrive would read as a smaller diff.
    assert "filter" not in steps[0]["with"]
    assert "--filter" not in _diff_step()["run"]


class _Repo:
    """An origin with history on main, a feature branch and a merge commit."""

    def __init__(self, tmp: Path) -> None:
        gitconfig = tmp / "gitconfig"
        gitconfig.write_text(
            git_global_config_text() + "[uploadpack]\n\tallowReachableSHA1InWant = true\n",
            encoding="utf-8",
        )
        self.env = dict(
            os.environ,
            HOME=str(tmp),
            GIT_CONFIG_GLOBAL=str(gitconfig),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="t",
            GIT_AUTHOR_EMAIL="t@example.invalid",
            GIT_COMMITTER_NAME="t",
            GIT_COMMITTER_EMAIL="t@example.invalid",
        )
        self.env.pop("GIT_CONFIG_COUNT", None)
        self.env.update(git_maintenance_off_entries(self.env))
        self.tmp = tmp
        self.work = tmp / "work"
        self.git(tmp, "init", "-q", "-b", "main", str(self.work))
        self.commits: dict[str, str] = {}
        for i in range(6):
            (self.work / f"f{i}.txt").write_text(f"line {i}\n" * (i + 1), encoding="utf-8")
            self.commits[f"m{i}"] = self.commit(f"main {i}")
        self.git(self.work, "checkout", "-q", "-b", "feature", self.commits["m2"])
        (self.work / "feature.txt").write_text("added by the feature\n", encoding="utf-8")
        (self.work / "f0.txt").write_text("changed by the feature\n", encoding="utf-8")
        self.commits["feature"] = self.commit("feature")
        self.git(self.work, "checkout", "-q", "main")
        self.git(self.work, "merge", "-q", "--no-ff", "-m", "merge", "feature")
        self.commits["merge"] = self.rev("HEAD")
        self.origin = tmp / "origin.git"
        self.git(tmp, "clone", "-q", "--bare", str(self.work), str(self.origin))

    def git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout

    def rev(self, ref: str) -> str:
        return self.git(self.work, "rev-parse", ref).strip()

    def commit(self, msg: str) -> str:
        self.git(self.work, "add", "-A")
        self.git(self.work, "commit", "-q", "-m", msg)
        return self.rev("HEAD")

    def checkout_like_actions(self, name: str, sha: str) -> Path:
        """What actions/checkout does at fetch-depth 2 for one commit."""
        dest = self.tmp / name
        self.git(self.tmp, "init", "-q", str(dest))
        self.git(dest, "remote", "add", "origin", f"file://{self.origin}")
        self.git(dest, "fetch", "-q", "--no-tags", "--depth=2", "origin", sha)
        self.git(dest, "checkout", "-q", "--detach", sha)
        return dest

    def run_step(self, checkout: Path, event: dict[str, str]) -> subprocess.CompletedProcess:
        runner_temp = checkout.parent / f"{checkout.name}-temp"
        runner_temp.mkdir()
        env = {
            **self.env,
            "RUNNER_TEMP": str(runner_temp),
            "GH_TOKEN": "",
            "MG_BASE": "",
            "MG_HEAD": "",
            "PR_BASE": "",
            "PUSH_BEFORE": "",
            **event,
        }
        return subprocess.run(
            ["bash", "-eo", "pipefail", "-c", _diff_step()["run"]],
            cwd=checkout,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )

    def full_clone_diff(self, base: str, head: str) -> str:
        return self.git(self.work, "diff", "--unified=0", base, head)


@pytest.fixture
def repo(tmp_path: Path) -> _Repo:
    return _Repo(tmp_path)


def _cases(r: _Repo) -> dict[str, tuple[str, dict[str, str], str]]:
    c = r.commits
    return {
        # A push to main several commits past `before`: neither the parent nor
        # the checkout holds `before`, so the step must fetch it.
        "push": (c["m5"], {"PUSH_BEFORE": c["m1"], "PUSH_HEAD": c["m5"]}, c["m1"]),
        "first-push": (c["m5"], {"PUSH_BEFORE": ZERO, "PUSH_HEAD": c["m5"]}, c["m4"]),
        # `github.sha` is the merge commit; base.sha is older than its first parent.
        "pull_request": (
            c["merge"],
            {"PR_BASE": c["m1"], "PUSH_HEAD": c["merge"]},
            c["m1"],
        ),
        "merge_group": (
            c["merge"],
            {"MG_BASE": c["m0"], "MG_HEAD": c["merge"], "PUSH_HEAD": c["merge"]},
            c["m0"],
        ),
    }


@pytest.mark.parametrize("shape", ["push", "first-push", "pull_request", "merge_group"])
def test_the_diff_matches_a_full_clone(repo: _Repo, shape: str) -> None:
    sha, event, base = _cases(repo)[shape]
    checkout = repo.checkout_like_actions(shape, sha)
    assert repo.git(checkout, "rev-parse", "--is-shallow-repository").strip() == "true"
    result = repo.run_step(checkout, event)
    assert result.returncode == 0, result.stdout + result.stderr
    got = (checkout.parent / f"{shape}-temp" / "change.diff").read_text(encoding="utf-8")
    want = repo.full_clone_diff(base, sha)
    assert want.strip(), "the case must diff something"
    assert got == want


def test_an_end_that_cannot_be_fetched_fails_the_step(repo: _Repo) -> None:
    c = repo.commits
    checkout = repo.checkout_like_actions("missing", c["m5"])
    missing = "1" * 40
    result = repo.run_step(checkout, {"PUSH_BEFORE": missing, "PUSH_HEAD": c["m5"]})
    assert result.returncode != 0
    assert f"could not fetch {missing}; failing closed." in result.stdout
    assert not (checkout.parent / "missing-temp" / "change.diff").exists()
