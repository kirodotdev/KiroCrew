"""Behavioural tests for .github/workflows/fork-review-heal.yml.

The heal sweep's entire decision logic lives in one `run:` block of shell + jq
that no other test touches. These tests extract that script and execute it for
real with `gh` replaced by a stub, so the re-dispatch CONDITIONS are verified
rather than assumed -- and the condition is the whole point: too narrow and a
never-fired fork-review lane stays frozen pending forever, too broad and every
fork PR re-dispatches its reviewers on every sweep.

The freeze this sweep rescues: a fork PR's Stage-2 reviewers trigger on the
`workflow_run: completed` event of CI, which GitHub silently drops under load.
When that event is dropped after CI concludes success, no fork-review check-run
is ever posted, `pr-readiness.yml` reads the lane as "(not started)" / "the real
review has not posted yet" -> pending, and nothing can recompute it. The sweep
finds those lanes and re-dispatches only them, keyed to the head SHA.

Skipped where the POSIX toolchain the script needs (bash, jq, GNU `date -d`) is
unavailable, which is the case on the Windows leg of the matrix. Mirrors the
explicit nt guard in test_pr_readiness_sweep.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

try:
    import pytest
except ModuleNotFoundError:  # pragma: no cover - only when run as the __main__ driver
    # The authoring sandbox cannot install pytest (INTEGRATIONS_ONLY blocks
    # PyPI), so the standalone driver at the bottom of this file runs the same
    # assertions without it. CI has pytest and uses the test functions directly.
    pytest = None  # type: ignore[assignment]

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - PyYAML is absent in the sandbox
    yaml = None  # type: ignore[assignment]

WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "fork-review-heal.yml"
)

LANES = [
    "Opus 4.8 Review",
    "GPT 5.6 Review",
    "Design Review",
    "UX Review",
    "First Principles Review",
]


def _gnu_date() -> bool:
    """GNU `date -d` is required; BSD date uses -j -f and would silently differ."""
    return (
        subprocess.run(
            ["date", "-u", "-d", "2026-01-01T00:00:00Z", "+%s"],
            capture_output=True,
        ).returncode
        == 0
    )


if pytest is not None:
    pytestmark = pytest.mark.skipif(
        not WORKFLOW.exists()
        or os.name == "nt"
        or shutil.which("bash") is None
        or shutil.which("jq") is None
        or not _gnu_date(),
        reason="requires the workflow plus a POSIX bash, jq and GNU date",
    )

# `gh` stub. Four shapes are served, keyed on the subcommand/URL:
#   pr list                          -> the fixture open-PR list
#   api .../workflows/ci.yml/runs    -> the fixture CI runs for the head SHA
#   api .../check-runs               -> the fixture head-SHA check-runs
#   workflow run                     -> RECORD the dispatch instead of firing it
GH_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
if [ "$1 ${2:-}" = "pr list" ]; then
  cat "$FIXTURES/prs.json"
  exit 0
fi
if [ "$1 ${2:-}" = "workflow run" ]; then
  # Record the workflow file and every -f key=value so the test can assert which
  # lane was re-dispatched and that head_sha was passed through.
  printf '%s\n' "$*" >> "$FIXTURES/dispatched.txt"
  exit 0
fi
if [ "$1" = "api" ]; then
  for arg in "$@"; do
    case "$arg" in
      *"/workflows/ci.yml/runs"*) cat "$FIXTURES/ci_runs.json"; exit 0 ;;
      *"/check-runs"*) cat "$FIXTURES/check_runs.json"; exit 0 ;;
    esac
  done
fi
echo "gh stub: unhandled: $*" >&2
exit 90
"""


def _extract_run_blocks_stdlib(text: str) -> list[str]:
    """PyYAML-free `run: |` literal-block extractor for the __main__ driver.

    The sandbox that authors this change cannot install PyYAML, so the standalone
    driver falls back to this. It handles ONLY the `run: |` literal-block style
    these workflows use; the authoritative extraction is the PyYAML path CI runs.
    """
    lines = text.splitlines()
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("run: |"):
            key_indent = len(lines[i]) - len(lines[i].lstrip(" "))
            body: list[str] = []
            body_indent: int | None = None
            j = i + 1
            while j < len(lines):
                ln = lines[j]
                if ln.strip() == "":
                    body.append("")
                    j += 1
                    continue
                ind = len(ln) - len(ln.lstrip(" "))
                if ind <= key_indent:
                    break
                if body_indent is None:
                    body_indent = ind
                body.append(ln[body_indent:] if len(ln) >= body_indent else ln.lstrip(" "))
                j += 1
            while body and body[-1] == "":
                body.pop()
            blocks.append("\n".join(body))
            i = j
            continue
        i += 1
    return blocks


def _script() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    if yaml is not None:
        spec = yaml.safe_load(text)
        steps = spec["jobs"]["heal"]["steps"]
        runs = [s["run"] for s in steps if "run" in s]
    else:  # pragma: no cover - the sandbox's PyYAML-free fallback
        runs = _extract_run_blocks_stdlib(text)
    assert len(runs) == 1, f"heal step count changed: {len(runs)}"
    return runs[0]


def _fixture(*args, **kwargs):
    """`pytest.fixture` under pytest; a passthrough decorator for the driver."""
    if pytest is not None:
        return pytest.fixture(*args, **kwargs)

    def _identity(fn):  # pragma: no cover - only the __main__ driver hits this
        return fn

    # Support both `@_fixture` and `@_fixture(scope=...)` call styles.
    if len(args) == 1 and callable(args[0]) and not kwargs:
        return args[0]
    return _identity


@_fixture(scope="module")
def script() -> str:
    return _script()


class Runner:
    """Executes the heal sweep's one step against one fixture repository state."""

    def __init__(self, root: Path, script: str) -> None:
        self.script = script
        self.fixtures = root / "fixtures"
        self.work = root / "work"
        bindir = root / "bin"
        for d in (self.fixtures, self.work, bindir):
            d.mkdir(parents=True)
        stub = bindir / "gh"
        stub.write_text(GH_STUB)
        stub.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FIXTURES": str(self.fixtures),
            "REPO": "kirodotdev/KiroCrew",
            "STALE_MINUTES": "20",
            "MAX_DISPATCH": "200",
            "PR_LIST_LIMIT": "900",
        }

    def sweep(
        self,
        *,
        is_fork: bool = True,
        ci_status: str | None = "completed",
        ci_conclusion: str = "success",
        ci_completed_at: str = "2020-01-01T00:00:00Z",
        present_lanes: dict[str, str] | None = None,
        pr: int = 7553,
        sha: str = "abc123def456",
        max_dispatch: str = "200",
    ) -> list[str]:
        """Run the heal over ONE fork PR; return the dispatch lines recorded.

        `present_lanes` maps a lane name to the check-run STATE that already
        exists on the head SHA: "in_progress", "success", "failure", "neutral",
        or "skipped". A lane absent from the map has NO check-run at all. Only a
        lane with no check-run, or only a completed `skipped` one, is missing.
        `ci_status=None` means the head SHA carries NO CI run at all.
        """
        (self.fixtures / "prs.json").write_text(
            json.dumps(
                [
                    {
                        "number": pr,
                        "headRefOid": sha,
                        "isCrossRepository": is_fork,
                    }
                ]
            )
        )
        ci_runs = (
            []
            if ci_status is None
            else [
                {
                    "id": 100,
                    "status": ci_status,
                    "conclusion": ci_conclusion,
                    "updated_at": ci_completed_at,
                    "created_at": ci_completed_at,
                }
            ]
        )
        (self.fixtures / "ci_runs.json").write_text(
            json.dumps({"workflow_runs": ci_runs})
        )
        runs = []
        for name, state in (present_lanes or {}).items():
            if state == "in_progress":
                runs.append({"name": name, "status": "in_progress", "conclusion": None})
            else:
                runs.append(
                    {"name": name, "status": "completed", "conclusion": state}
                )
        # Slurped shape: an outer array of PAGES, each `{"check_runs": [...]}`.
        (self.fixtures / "check_runs.json").write_text(
            json.dumps([{"check_runs": runs}])
        )
        applied = self.fixtures / "dispatched.txt"
        applied.unlink(missing_ok=True)

        proc = subprocess.run(  # noqa: S603 - fixed argv, test-local stub
            ["bash", "-c", self.script],
            cwd=self.work,
            env={**self.env, "MAX_DISPATCH": max_dispatch},
            text=True,
            capture_output=True,
        )
        # The sweep must never fail a run: a dispatch it cannot make is not an error.
        assert proc.returncode == 0, proc.stderr
        self.last_stdout = proc.stdout
        if not applied.exists():
            return []
        return applied.read_text().splitlines()


@_fixture
def runner(tmp_path: Path, script: str) -> Runner:
    return Runner(tmp_path, script)


def _lanes_dispatched(lines: list[str]) -> set[str]:
    """Return the set of workflow files that were re-dispatched."""
    files = set()
    for line in lines:
        for tok in line.split():
            if tok.endswith(".yml"):
                files.add(tok)
    return files


# ── Scenario (a): CI success + missing lanes -> dispatch each missing lane once ─


def test_ci_success_with_all_lanes_missing_dispatches_each_once(runner: Runner) -> None:
    """The #7553 incident, reduced: CI passed, no fork-review lane ever fired.

    All five lanes are missing, so each corresponding reviewer is re-dispatched
    exactly once, keyed to the head SHA.
    """
    dispatched = runner.sweep(present_lanes=None)
    assert len(dispatched) == 5
    assert _lanes_dispatched(dispatched) == {
        "fork-opus-review.yml",
        "fork-gpt-review.yml",
        "fork-design-review.yml",
        "fork-ux-review.yml",
        "fork-first-principles-review.yml",
    }
    for line in dispatched:
        assert "head_sha=abc123def456" in line


def test_only_missing_lanes_are_dispatched(runner: Runner) -> None:
    """A lane that already posted a real verdict is left alone; only the ones that
    never fired are re-dispatched."""
    dispatched = runner.sweep(
        present_lanes={
            "Opus 4.8 Review": "success",
            "GPT 5.6 Review": "failure",
            "Design Review": "neutral",
        }
    )
    assert _lanes_dispatched(dispatched) == {
        "fork-ux-review.yml",
        "fork-first-principles-review.yml",
    }


def test_a_skipped_only_lane_counts_as_never_fired(runner: Runner) -> None:
    """A lane whose only check-run is the same-repo twin's `skipped` one has not
    had its real review post yet -- exactly the pending state pr-readiness.yml
    describes -- so it is re-dispatched."""
    dispatched = runner.sweep(
        present_lanes={name: "skipped" for name in LANES}
    )
    assert len(dispatched) == 5
    assert _lanes_dispatched(dispatched) == {
        "fork-opus-review.yml",
        "fork-gpt-review.yml",
        "fork-design-review.yml",
        "fork-ux-review.yml",
        "fork-first-principles-review.yml",
    }


# ── Scenario (b): CI success + all lanes present/complete -> no dispatch ──────


def test_ci_success_with_all_lanes_present_dispatches_none(runner: Runner) -> None:
    """A fully-reviewed fork PR is never re-dispatched."""
    assert runner.sweep(present_lanes={name: "success" for name in LANES}) == []


def test_an_in_progress_lane_is_not_re_dispatched(runner: Runner) -> None:
    """The self-termination property: the instant a reviewer starts it opens an
    in_progress check-run keyed to the head SHA, so the next sweep sees the lane
    as present and does not re-dispatch it. Without this a still-running review
    would be fired again on every sweep."""
    assert runner.sweep(present_lanes={name: "in_progress" for name in LANES}) == []


# ── Scenario (c): CI has not passed -> no dispatch, pending by design ─────────


def test_ci_not_passed_dispatches_nothing_and_attributes_pending_to_ci(
    runner: Runner,
) -> None:
    """A fork PR whose CI failed is NOT dispatched and NOT forced to pass; the
    log attributes the pending to CI, distinct from a missing review."""
    dispatched = runner.sweep(
        ci_status="completed", ci_conclusion="failure", present_lanes=None
    )
    assert dispatched == []
    assert "pending by design (CI has not passed)" in runner.last_stdout


def test_ci_still_running_dispatches_nothing(runner: Runner) -> None:
    """CI in flight means Stage 2 is correctly not eligible yet."""
    dispatched = runner.sweep(
        ci_status="in_progress", ci_conclusion="", present_lanes=None
    )
    assert dispatched == []
    assert "pending by design (CI has not passed)" in runner.last_stdout


def test_no_ci_run_at_all_dispatches_nothing(runner: Runner) -> None:
    """No CI run on the head SHA yet -> pending by design (CI has not started)."""
    dispatched = runner.sweep(ci_status=None, present_lanes=None)
    assert dispatched == []
    assert "pending by design (CI has not started)" in runner.last_stdout


def test_a_fresh_ci_success_is_left_to_fan_out(runner: Runner) -> None:
    """Inside STALE_MINUTES of the CI success the fork reviewers may genuinely
    still be fanning out via the workflow_run event, so nothing is dispatched."""
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    dispatched = runner.sweep(ci_completed_at=recent, present_lanes=None)
    assert dispatched == []
    assert "may still be fanning out" in runner.last_stdout


# ── A same-repo PR is out of scope ───────────────────────────────────────────


def test_a_same_repo_pr_is_ignored(runner: Runner) -> None:
    """Only fork PRs run the fork reviewers; a same-repo PR runs the same-repo
    lanes and must never be dispatched here."""
    assert runner.sweep(is_fork=False, present_lanes=None) == []


# ── Scenario (d): idempotency across two consecutive sweeps ───────────────────


def test_dispatch_is_self_terminating_across_two_sweeps(runner: Runner) -> None:
    """First sweep re-dispatches the never-fired lanes; the reviewers then open
    their in_progress check-runs, so the second sweep -- over that resulting
    state -- dispatches nothing."""
    first = runner.sweep(present_lanes=None)
    assert len(first) == 5
    # After the dispatch, every reviewer has opened an in_progress check-run
    # keyed to the head SHA. The next sweep sees the lanes as present.
    second = runner.sweep(present_lanes={name: "in_progress" for name in LANES})
    assert second == []


# ── The runaway backstop applies ─────────────────────────────────────────────


def test_max_dispatch_caps_the_sweep(runner: Runner) -> None:
    """The cap bounds a single sweep against a pathological run."""
    assert runner.sweep(present_lanes=None, max_dispatch="0") == []


def test_max_dispatch_of_two_dispatches_only_two_lanes(runner: Runner) -> None:
    dispatched = runner.sweep(present_lanes=None, max_dispatch="2")
    assert len(dispatched) == 2


# ── It only ever nudges; it never publishes a verdict ────────────────────────


def test_the_sweep_never_writes_a_check_run_or_status(script: str) -> None:
    """The heal may only dispatch the reviewers; it must never manufacture a
    verdict itself, which would be a second source of truth for a required
    check."""
    assert "gh workflow run" in script
    for forbidden in ("check-runs\" -f", "--method POST", "-X POST", "-f status="):
        assert forbidden not in script, f"heal must not write results: {forbidden}"


def test_the_sweep_never_checks_out_or_executes_fork_code(script: str) -> None:
    """The fork trust model: the sweep only reads and dispatches."""
    for forbidden in ("actions/checkout", "git clone", "git fetch", "npm ", "pip "):
        assert forbidden not in script, f"heal must not touch fork code: {forbidden}"


# ── Standalone driver so the same assertions run without pytest ──────────────
# The sandbox that authors this change cannot install pytest (INTEGRATIONS_ONLY
# blocks PyPI), so `python3.12 test/test_fork_review_heal.py` runs the behavioural
# checks directly against a plain-bash execution of the extracted step. CI runs
# the pytest form above; this __main__ is the local substitute.
if __name__ == "__main__":
    import tempfile

    if (
        os.name == "nt"
        or shutil.which("bash") is None
        or shutil.which("jq") is None
        or not _gnu_date()
        or not WORKFLOW.exists()
    ):
        print("SKIP: requires the workflow plus a POSIX bash, jq and GNU date")
        raise SystemExit(0)

    script = _script()
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"{'ok  ' if cond else 'FAIL'} {name}")
        if not cond:
            failures.append(name)

    with tempfile.TemporaryDirectory() as td:
        # Scenario (a): all lanes missing -> five dispatches.
        r = Runner(Path(td) / "a", script)
        d = r.sweep(present_lanes=None)
        check("ci_success_all_missing_dispatches_five", len(d) == 5)
        check(
            "dispatch_carries_head_sha",
            all("head_sha=abc123def456" in line for line in d),
        )

        # Only missing lanes dispatched.
        r = Runner(Path(td) / "a2", script)
        d = r.sweep(
            present_lanes={
                "Opus 4.8 Review": "success",
                "GPT 5.6 Review": "failure",
                "Design Review": "neutral",
            }
        )
        check(
            "only_missing_lanes_dispatched",
            _lanes_dispatched(d)
            == {"fork-ux-review.yml", "fork-first-principles-review.yml"},
        )

        # skipped-only counts as never fired.
        r = Runner(Path(td) / "a3", script)
        d = r.sweep(present_lanes={n: "skipped" for n in LANES})
        check("skipped_only_counts_as_missing", len(d) == 5)

        # Scenario (b): all present -> none.
        r = Runner(Path(td) / "b", script)
        check(
            "all_present_dispatches_none",
            r.sweep(present_lanes={n: "success" for n in LANES}) == [],
        )
        r = Runner(Path(td) / "b2", script)
        check(
            "in_progress_not_re_dispatched",
            r.sweep(present_lanes={n: "in_progress" for n in LANES}) == [],
        )

        # Scenario (c): CI not passed -> none, attributed to CI.
        r = Runner(Path(td) / "c", script)
        d = r.sweep(ci_conclusion="failure", present_lanes=None)
        check(
            "ci_failed_dispatches_none_attributed_to_ci",
            d == [] and "pending by design (CI has not passed)" in r.last_stdout,
        )
        r = Runner(Path(td) / "c2", script)
        d = r.sweep(ci_status=None, present_lanes=None)
        check(
            "no_ci_run_dispatches_none",
            d == [] and "pending by design (CI has not started)" in r.last_stdout,
        )

        # Same-repo PR ignored.
        r = Runner(Path(td) / "c3", script)
        check(
            "same_repo_pr_ignored",
            r.sweep(is_fork=False, present_lanes=None) == [],
        )

        # Scenario (d): idempotency across two sweeps.
        r = Runner(Path(td) / "d", script)
        first = r.sweep(present_lanes=None)
        r2 = Runner(Path(td) / "d2", script)
        second = r2.sweep(present_lanes={n: "in_progress" for n in LANES})
        check(
            "idempotent_across_two_sweeps",
            len(first) == 5 and second == [],
        )

        # Cap.
        r = Runner(Path(td) / "e", script)
        check("max_dispatch_caps", r.sweep(present_lanes=None, max_dispatch="0") == [])

    if failures:
        print(f"\n{len(failures)} check(s) FAILED: {failures}")
        raise SystemExit(1)
    print("\nall checks passed")
