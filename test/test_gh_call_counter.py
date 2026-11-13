"""Behavioural tests for the measurement-only ``gh`` call counter.

pr-readiness and the same-repo AI review lanes install a shim as their first
step: every later ``gh`` resolves to it, it appends one ``<kind> <n>`` line to a
counter file, then ``exec``s the real gh. Their last step folds that file into
one ``rest-calls:`` log line. These tests run both steps for real against a
stub ``gh`` and pin that the shim is invisible to the caller: same stdout,
stderr, stdin and exit code, and no failure when the counter cannot be written.

Skipped where bash is unavailable (the Windows leg of the matrix).
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
INSTALL = "Count GitHub API calls (measurement only)"
REPORT = "Report GitHub API call counts"
BASH = shutil.which("bash") or "bash"

# Every job in these files that runs `gh`. security-scope-review's validate and
# adjudicate jobs make no gh call and are deliberately not instrumented.
INSTRUMENTED = {
    "pr-readiness.yml": ["readiness"],
    "design-review.yml": ["design-review"],
    "ux-review.yml": ["ux-review"],
    "first-principles-review.yml": ["first-principles-review"],
    "codex-review.yml": ["codex-review"],
    "claude-review.yml": ["claude-review"],
    "security-scope-review.yml": ["generate", "publish"],
}

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None or shutil.which("awk") is None,
    reason="requires a POSIX bash and awk",
)

# Echoes its argv and stdin, writes a fixed stderr line, exits with $STUB_RC.
GH_STUB = """#!/usr/bin/env bash
printf 'argv:'
printf ' [%s]' "$@"
printf '\\n'
if [ -n "${STUB_READ_STDIN:-}" ]; then printf 'stdin:%s\\n' "$(cat)"; fi
echo "stub-stderr" >&2
exit "${STUB_RC:-0}"
"""


def _steps(workflow: str, job: str) -> list[dict]:
    doc = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    return doc["jobs"][job]["steps"]


def _run_text(name: str) -> str:
    for step in _steps("pr-readiness.yml", "readiness"):
        if step.get("name") == name:
            return step["run"]
    raise AssertionError(f"no step {name!r}")


class Runner:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.stubs = root / "stubs"
        self.stubs.mkdir()
        gh = self.stubs / "gh"
        gh.write_text(GH_STUB, encoding="utf-8")
        gh.chmod(0o755)
        self.temp = root / "runner-temp"
        self.temp.mkdir()
        self.path_file = root / "github_path"
        self.path_file.write_text("", encoding="utf-8")
        self.base_path = f"{self.stubs}{os.pathsep}{os.environ['PATH']}"
        self.env = {
            **os.environ,
            "PATH": self.base_path,
            "RUNNER_TEMP": str(self.temp),
            "GITHUB_PATH": str(self.path_file),
            "GITHUB_JOB": "readiness",
            "GITHUB_WORKFLOW_REF": "kirodotdev/KiroCrew/.github/workflows/pr-readiness.yml@refs/heads/main",
        }

    def bash(self, script: str, stdin: str | None = None, **extra: str):
        return subprocess.run(  # noqa: S603 - fixed argv, test-local stubs
            [BASH, "-e", "-c", script],
            cwd=self.root,
            env={**self.env, **extra},
            input=stdin,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=30,
        )

    def install(self) -> None:
        done = self.bash(_run_text(INSTALL))
        assert done.returncode == 0, done.stderr
        added = [p for p in self.path_file.read_text(encoding="utf-8").splitlines() if p]
        self.env["PATH"] = os.pathsep.join([*added, self.base_path])

    @property
    def counter(self) -> Path:
        return self.temp / "gh-call-count" / "calls"


@pytest.fixture
def runner(tmp_path: Path) -> Runner:
    return Runner(tmp_path)


FORK_GUARD = "github.event.pull_request.head.repo.full_name == github.repository"


def test_every_job_that_calls_gh_carries_identical_steps_first_and_last() -> None:
    install_run = report_run = None
    for workflow, jobs in INSTRUMENTED.items():
        doc = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
        for job_name, job in doc["jobs"].items():
            names = [s.get("name") for s in job["steps"]]
            if job_name not in jobs:
                assert INSTALL not in names and REPORT not in names, (workflow, job_name)
                continue
            assert names.count(INSTALL) == 1 and names.count(REPORT) == 1
            first = job["steps"][0]
            report = job["steps"][names.index(REPORT)]
            assert first["name"] == INSTALL, (workflow, job_name)
            if workflow == "pr-readiness.yml":
                # The budget log keeps the last slot; the count sits just ahead.
                assert names[-2:] == [REPORT, "Log the remaining REST budget"]
                assert "if" not in first
                assert report["if"] == "always()"
            else:
                # Same-repo lanes gate every step on the fork guard.
                assert names[-1] == REPORT, (workflow, job_name)
                assert first["if"] == FORK_GUARD
                assert report["if"] == f"always() && {FORK_GUARD}"
            assert first["continue-on-error"] is True and report["continue-on-error"] is True
            # Neither step needs a token: the counter reads and writes local files only.
            assert "env" not in first and "env" not in report
            install_run = install_run or first["run"]
            report_run = report_run or report["run"]
            assert first["run"] == install_run, f"{workflow}/{job_name}: install step drifted"
            assert report["run"] == report_run, f"{workflow}/{job_name}: report step drifted"


def test_the_shim_preserves_stdout_stderr_and_exit_code(runner: Runner) -> None:
    direct = runner.bash('gh api "repos/o/r/pulls/1" --jq ".x y"', STUB_RC="3")
    runner.install()
    shimmed = runner.bash('gh api "repos/o/r/pulls/1" --jq ".x y"', STUB_RC="3")
    assert shimmed.returncode == direct.returncode == 3
    assert shimmed.stdout == direct.stdout
    assert shimmed.stderr == direct.stderr == "stub-stderr\n"
    assert runner.bash("command -v gh").stdout.strip() == str(runner.temp / "gh-call-count" / "gh")


def test_the_shim_passes_stdin_through(runner: Runner) -> None:
    runner.install()
    done = runner.bash(
        "gh api --method POST repos/o/r/issues/1/labels --input -",
        stdin='{"labels":["x"]}',
        STUB_READ_STDIN="1",
    )
    assert done.returncode == 0
    assert 'stdin:{"labels":["x"]}' in done.stdout


def test_calls_are_classified_by_kind_and_report_counts_only(runner: Runner) -> None:
    runner.install()
    script = "\n".join(
        [
            "gh api repos/o/r/pulls/1",
            "gh api --paginate 'repos/o/r/issues/1/comments'",
            "gh api graphql -f query='{ viewer { login } }'",
            "gh api rate_limit --jq .resources.core",
            "gh pr comment 1 --body-file body.md",
            "gh pr view 1 --repo o/r --json headRefOid",
            "gh label list --repo o/r --limit 200",
            "gh label create x --repo o/r",
            "gh run list --limit 1",
        ]
    )
    assert runner.bash(script).returncode == 0
    lines = runner.counter.read_text(encoding="utf-8").splitlines()
    assert all(re.fullmatch(r"(rest|graphql|paginate|other|free) [0-9]+", line) for line in lines)
    report = runner.bash(_run_text(REPORT))
    assert report.returncode == 0
    assert report.stdout == (
        "rest-calls: workflow=pr-readiness.yml job=readiness rest=3 graphql=5 "
        "paginated=1 other=1 basis=gh-invocations\n"
    )
    # Nothing an argument carried reaches the log.
    for secret in ("repos/o/r", "viewer", "body.md", "headRefOid"):
        assert secret not in report.stdout


def test_an_unwritable_counter_never_reaches_the_caller(runner: Runner) -> None:
    runner.install()
    runner.counter.unlink()
    runner.counter.mkdir()  # appending to a directory fails
    done = runner.bash("gh api repos/o/r", STUB_RC="0")
    assert done.returncode == 0
    assert done.stderr == "stub-stderr\n"
    assert done.stdout.startswith("argv: [api] [repos/o/r]")


def test_an_unwritable_runner_temp_skips_the_install(runner: Runner) -> None:
    runner.temp.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        done = runner.bash(_run_text(INSTALL))
    finally:
        runner.temp.chmod(stat.S_IRWXU)
    assert done.returncode == 0
    assert runner.path_file.read_text(encoding="utf-8") == ""


def test_no_gh_on_path_is_a_no_op(runner: Runner) -> None:
    (runner.stubs / "gh").unlink()
    # Only the (now empty) stub directory: a runner image ships gh in /usr/bin.
    done = runner.bash(_run_text(INSTALL), PATH=str(runner.stubs))
    assert done.returncode == 0
    assert runner.path_file.read_text(encoding="utf-8") == ""


def test_a_second_install_does_not_wrap_the_shim(runner: Runner) -> None:
    runner.install()
    again = runner.bash(_run_text(INSTALL))
    assert again.returncode == 0
    assert runner.path_file.read_text(encoding="utf-8").count("gh-call-count") == 1
    assert runner.bash("gh api x").stdout.startswith("argv: [api] [x]")


def test_report_without_a_counter_says_so_and_succeeds(runner: Runner) -> None:
    done = runner.bash(_run_text(REPORT))
    assert done.returncode == 0
    assert done.stdout == "rest-calls: workflow=pr-readiness.yml job=readiness counter=absent\n"
