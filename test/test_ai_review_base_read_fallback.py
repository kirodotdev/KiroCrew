"""A base-commit read that FAILS never falls back to the pull request's copy.

Seven review-lane sites stage a file from the base commit so a pull request
cannot change the file that judges it, and use the pull request's copy only on
the bootstrap round: the round whose base commit has no such file. These cases
run each site's REAL step body against a local repository and pin the three
answers that matter:

- the base has the file: the base's copy is staged;
- the base tree has no such path: the pull request's copy is staged, and the
  step says it is bootstrapping;
- the base commit cannot be read (an object the checkout does not have): the
  step fails closed, stages nothing from the pull request, and does NOT claim a
  bootstrap -- which a `git show ... || true; [ ! -s ]` or a `git cat-file -e`
  test cannot tell apart from the absent case.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from git_maintenance_helpers import git_maintenance_off_entries

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None or shutil.which("git") is None,
    reason="runs the lanes' bash steps against real git; Linux/macOS runners cover it",
)

#: A commit id no fixture repository holds: the read of it fails for a reason
#: other than "the path is absent", which is the case the issue is about.
UNREADABLE_SHA = "0123456789abcdef0123456789abcdef01234567"


@dataclass(frozen=True)
class Site:
    workflow: str
    job: str
    step: str
    #: The base-commit path the step stages.
    path: str
    #: Where the step stages it, relative to the work tree; `$RUNNER_TEMP` and
    #: `$HARNESS` are expanded by the step itself.
    staged: str
    #: The step body is cut before the first line holding this text, so the
    #: case runs the staging block and nothing that needs a live runner. None
    #: runs the whole body.
    cut: str | None = None


SITES = (
    Site(
        "codex-review.yml",
        "codex-review",
        "Write review prompt",
        ".github/review-prompts/gpt-preamble.md",
        ".review-prompts-gpt/gpt-preamble.md",
        cut="# gpt-block-adjudication.md is the contract",
    ),
    Site(
        "security-scope-review.yml",
        "generate",
        "Materialize the base-owned contract",
        ".github/review-prompts/security-scope.md",
        ".review-prompts-scope/security-scope.md",
        cut="# The contract carries __HEAD_SHA__",
    ),
    Site(
        "security-scope-review.yml",
        "validate",
        "Materialize the base-owned outbound redactor",
        "scripts/scope_redact.py",
        "runner-temp/scope-redact/scope_redact.py",
    ),
    Site(
        "security-scope-review.yml",
        "validate",
        "Validate the candidates against the base corpus",
        "scripts/scope_candidates.py",
        "harness/scope_candidates.py",
        cut='if [ -n "$bootstrap" ]; then',
    ),
    Site(
        "security-scope-review.yml",
        "publish",
        "Materialize the base-owned outbound redactor",
        "scripts/scope_redact.py",
        "runner-temp/scope-redact/scope_redact.py",
    ),
    Site(
        "security-scope-review.yml",
        "publish",
        "Fold the per-platform reports into one verdict",
        "scripts/scope_candidates.py",
        "harness/scope_candidates.py",
        cut='if [ -n "$bootstrap" ]; then',
    ),
    Site(
        "denial-differential.yml",
        "denial-differential",
        "Run the denial differential",
        "src/kiro_crew/builtin_skills/security-conductor/golden-paths.json",
        "corpus.json",
        cut="python scripts/deny_diff.py",
    ),
)

#: Every file any site stages, so each commit below carries the full set.
_STAGED_PATHS = sorted(
    {s.path for s in SITES} | {"scripts/deny_diff.py"},
)


def _run_body(site: Site) -> str:
    doc = yaml.safe_load((WORKFLOWS / site.workflow).read_text(encoding="utf-8"))
    steps = [s for s in doc["jobs"][site.job]["steps"] if s.get("name") == site.step]
    assert len(steps) == 1, (site, len(steps))
    body = steps[0]["run"]
    if site.cut is not None:
        lines = body.splitlines()
        cut_at = next(i for i, line in enumerate(lines) if site.cut in line)
        body = "\n".join(lines[:cut_at]) + "\n"
    return body


def _git(repo: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, dict[str, str], str, str]:
    """A work tree checked out at the PR head, with an older base that lacks every file.

    Returns (work tree, env, base sha that HAS the files, base sha that has NONE).
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=str(tmp_path / "gitconfig"),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    env.update(git_maintenance_off_entries(env))
    work = tmp_path / "work"
    work.mkdir()
    _git(work, env, "init", "-q", "-b", "main")
    _git(work, env, "config", "commit.gpgsign", "false")
    # The steps' own `git fetch` of an absent commit must fail, as it does for an
    # object the runner cannot get, not reach any real remote.
    _git(work, env, "remote", "add", "origin", (tmp_path / "gone").as_uri())
    (work / "README").write_text("x\n", encoding="utf-8")
    _git(work, env, "add", "README")
    _git(work, env, "commit", "-q", "-m", "before any staged file")
    empty_base = _git(work, env, "rev-parse", "HEAD")
    for label in ("BASE", "HEAD"):
        for rel in _STAGED_PATHS:
            p = work / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"{label} copy of {rel}\n", encoding="utf-8")
        _git(work, env, "add", "-A")
        _git(work, env, "commit", "-q", "-m", label)
        if label == "BASE":
            full_base = _git(work, env, "rev-parse", "HEAD")
    # The Validate step refuses before staging when the reviewer wrote nothing.
    (work / "candidates.json").write_text("[]\n", encoding="utf-8")
    return work, env, full_base, empty_base


def _run(site: Site, work: Path, env: dict[str, str], base: str):
    head = _git(work, env, "rev-parse", "HEAD")
    out = work / "github-output"
    out.write_text("", encoding="utf-8")
    script = work.parent / "step.sh"
    script.write_text(_run_body(site), encoding="utf-8", newline="\n")
    (work / "runner-temp").mkdir(exist_ok=True)
    step_env = dict(env)
    step_env.update(
        BASE_SHA=base,
        HEAD_SHA=head,
        HEAD=head,
        GITHUB_OUTPUT=str(out),
        GITHUB_STEP_SUMMARY=str(work.parent / "summary"),
        RUNNER_TEMP=str(work / "runner-temp"),
        HARNESS=str(work / "harness"),
    )
    # GitHub runs a `run:` body with no `shell:` as `bash -e {0}`.
    result = subprocess.run(
        ["bash", "-e", str(script)],
        cwd=work,
        env=step_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    staged = work / site.staged
    text = staged.read_text(encoding="utf-8") if staged.is_file() else ""
    return result, text, out.read_text(encoding="utf-8")


def _ids(site: Site) -> str:
    return f"{site.workflow}:{site.job}:{site.step}"


@pytest.mark.parametrize("site", SITES, ids=_ids)
def test_base_copy_is_staged_when_the_base_has_it(repo, site: Site) -> None:
    work, env, full_base, _ = repo
    result, staged, _ = _run(site, work, env, full_base)
    assert staged == f"BASE copy of {site.path}\n", result.stdout + result.stderr
    assert f"{site.path} is absent on the base commit" not in result.stdout


@pytest.mark.parametrize("site", SITES, ids=_ids)
def test_pr_copy_is_staged_only_on_the_bootstrap_round(repo, site: Site) -> None:
    work, env, _, empty_base = repo
    result, staged, _ = _run(site, work, env, empty_base)
    assert staged == f"HEAD copy of {site.path}\n", result.stdout + result.stderr
    if site.workflow != "denial-differential.yml":  # it names the bootstrap in its report
        assert f"{site.path} is absent on the base commit" in result.stdout


@pytest.mark.parametrize("site", SITES, ids=_ids)
def test_an_unreadable_base_fails_closed_instead_of_falling_back(repo, site: Site) -> None:
    work, env, _, _ = repo
    result, staged, outputs = _run(site, work, env, UNREADABLE_SHA)
    log = result.stdout + result.stderr
    assert "HEAD copy" not in staged, log
    assert "bootstrap" not in result.stdout, log
    # The fold step's fail-closed verdict is an output, not an exit code: the
    # step after it reds on `folded=error`.
    assert result.returncode != 0 or "folded=error" in outputs, log
    assert UNREADABLE_SHA in log, log
