"""Contract tests for .github/workflows/scheduled-failure-watch.yml.

A red ``schedule:`` run notifies nobody unless something files an issue for it.
The watcher listens for the completion of every scheduled workflow that does not
surface its own failures and opens (or bumps) one fixed-title tracking issue per
source workflow. Three properties are pinned rather than assumed:

* COVERAGE -- every workflow with a ``schedule:`` trigger is either watched or
  carries its own failure-issue step, and never both.  A scheduled workflow
  added without a line in the watcher's list is exactly the silent failure this
  watcher exists to close, and a workflow that is both watched and self-reporting
  would file every red run twice.
* GATING -- the job runs only for a scheduled run that failed or timed out
  (never a cancelled one: the watched workflows cancel their own scheduled runs
  by design), with ``issues: write`` as its only permission and no checkout.
* DEDUPE -- executed for real with ``gh`` stubbed: a first failure creates an
  issue with the fixed title, a repeat comments on the open one instead, a
  failed dedupe query aborts rather than filing blind, and the selector that
  picks the open issue matches the fixed title exactly (evaluated with ``jq``),
  so a human-filed issue that merely contains the phrase cannot absorb it.

Skipped where the POSIX toolchain the script needs is unavailable, matching the
guards in test_memory_benchmark_workflow.py.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
WATCHER = WORKFLOWS / "scheduled-failure-watch.yml"

_FAILURE_ONLY_CONDITIONS = ("failure()", "cancelled()", "always()")
# One ``gh`` invocation per record in the stub's log; the issue body carries
# newlines, so a line-per-call log would split a single call across lines.
_RECORD_SEPARATOR = "\x1e"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(document: dict) -> dict:
    # PyYAML reads a bare ``on:`` key as the boolean True.
    return document.get("on") or document.get(True) or {}


def _scheduled_workflows() -> dict[str, Path]:
    """``name:`` -> file, for every workflow with a ``schedule:`` trigger."""
    found: dict[str, Path] = {}
    for path in sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]):
        document = _load(path)
        if "schedule" in _triggers(document):
            found[document["name"]] = path
    return found


def _files_an_issue_on_failure(document: dict) -> bool:
    """True when some step still runs after a failure and calls ``gh issue create``.

    The three self-reporting workflows spell their condition differently
    (``failure()``, ``always() && (failure() || cancelled())``, and an
    ``always()`` guarded on a non-PASS verdict), so the check is for a
    failure-reachable ``if:`` plus the create call, not one exact spelling.
    """
    for job in (document.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            condition = str(step.get("if", ""))
            if "gh issue create" in str(step.get("run", "")) and any(
                marker in condition for marker in _FAILURE_ONLY_CONDITIONS
            ):
                return True
    return False


def _watched_names() -> list[str]:
    return list(_triggers(_load(WATCHER))["workflow_run"]["workflows"])


# ── Coverage: the list cannot fall behind the tree ───────────────────────────


def test_every_scheduled_workflow_is_watched_or_self_reporting() -> None:
    scheduled = _scheduled_workflows()
    assert scheduled, "no schedule: workflows found; this test must not pass vacuously"
    self_reporting = {
        name for name, path in scheduled.items() if _files_an_issue_on_failure(_load(path))
    }
    watched = set(_watched_names())

    silent = set(scheduled) - watched - self_reporting
    assert not silent, (
        f"these schedule: workflows neither appear in {WATCHER.name}'s list nor file "
        f"their own failure issue, so a red run would notify nobody: {sorted(silent)}"
    )
    doubled = watched & self_reporting
    assert not doubled, (
        f"these workflows already file their own failure issue; watching them too would "
        f"file every red run twice: {sorted(doubled)}"
    )
    unknown = watched - set(scheduled)
    assert not unknown, (
        f"{WATCHER.name} lists names that match no schedule: workflow's `name:` -- a "
        f"workflow_run trigger on a misspelt name silently never fires: {sorted(unknown)}"
    )


def test_the_three_self_reporting_workflows_are_the_ones_left_out() -> None:
    """The exclusions are a measured fact about the tree, pinned so a workflow
    that drops its own issue step is picked up here, not by its next red run."""
    scheduled = _scheduled_workflows()
    self_reporting = {
        name for name, path in scheduled.items() if _files_an_issue_on_failure(_load(path))
    }
    assert self_reporting == {"Add Contributor", "Fix Loop Analysis", "GUI User Test"}


def test_the_watched_list_has_no_duplicates() -> None:
    names = _watched_names()
    assert len(names) == len(set(names)), names


# ── Gating and least privilege ───────────────────────────────────────────────


def test_the_watcher_fires_only_on_completion() -> None:
    assert _triggers(_load(WATCHER))["workflow_run"]["types"] == ["completed"]


def test_the_job_is_gated_on_a_red_or_timed_out_scheduled_run() -> None:
    (job,) = _load(WATCHER)["jobs"].values()
    condition = " ".join(str(job["if"]).split())
    assert "github.event.workflow_run.event == 'schedule'" in condition
    assert "github.event.workflow_run.conclusion == 'failure'" in condition
    # A workflow-level `timeout-minutes` reports `timed_out`, not `failure`; a
    # gate that names only `failure` lets a hung nightly stay silent.
    assert "github.event.workflow_run.conclusion == 'timed_out'" in condition
    assert "'success'" not in condition


def test_a_cancelled_scheduled_run_is_not_a_failure() -> None:
    """Watched workflows cancel their own scheduled runs on purpose --
    `pr-merge-conflict-label.yml` shares a `cancel-in-progress: true` group
    with its push trigger, `nightly.yml` and `memory-benchmark.yml` single-flight
    -- so a `cancelled` conclusion is a by-design collapse and filing on it would
    turn every merge to main during a sweep into a false "is failing" issue."""
    (job,) = _load(WATCHER)["jobs"].values()
    assert "cancelled" not in str(job["if"])
    # The premise is measured, not assumed: at least one watched workflow really
    # does cancel its own in-flight run.
    self_cancelling = {
        name
        for name, path in _scheduled_workflows().items()
        if (_load(path).get("concurrency") or {}).get("cancel-in-progress") is True
    }
    assert self_cancelling & set(_watched_names()), self_cancelling


def test_issues_write_is_the_only_permission() -> None:
    document = _load(WATCHER)
    assert document["permissions"] == {"issues": "write"}
    for job in document["jobs"].values():
        assert "permissions" not in job


def test_the_watcher_never_checks_out_or_runs_source_code() -> None:
    document = _load(WATCHER)
    for job in document["jobs"].values():
        for step in job["steps"]:
            assert "uses" not in step, step
        assert job["runs-on"] == "ubuntu-latest"


def test_a_running_watcher_is_never_killed_by_the_next_completion() -> None:
    """One watcher per source workflow at a time, and the incumbent is never
    cancelled: two watchers for one workflow running side by side is how a dedupe
    check passes twice and files two issues. GitHub keeps only one PENDING run
    per group and evicts the older pending one, which costs nothing here -- the
    newest completion still runs after the incumbent and finds its issue."""
    concurrency = _load(WATCHER)["concurrency"]
    assert concurrency["cancel-in-progress"] is False
    assert "github.event.workflow_run.name" in concurrency["group"]


# ── Dedupe, executed for real ────────────────────────────────────────────────


def _has_gnu_date() -> bool:
    """The step's 6-hour window uses GNU ``date -d``; BSD ``date`` (macOS) has no
    such flag, so the behavioural tests skip there instead of failing on the
    tool rather than on the workflow."""
    if shutil.which("date") is None:
        return False
    probe = subprocess.run(
        ["date", "-u", "-d", "6 hours ago", "+%Y-%m-%dT%H:%M:%SZ"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return probe.returncode == 0


pytestmark_posix = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None or not _has_gnu_date(),
    reason="requires a POSIX bash and GNU date to execute the step script",
)


def _step_script() -> str:
    (job,) = _load(WATCHER)["jobs"].values()
    (step,) = job["steps"]
    return step["run"]


def _dedupe_selector() -> str:
    """The ``--jq`` expression the step hands ``gh issue list``."""
    match = re.search(r"--jq '([^']+)'", _step_script())
    assert match, "the dedupe step no longer passes a --jq selector; update this test"
    return match.group(1)


@pytest.mark.skipif(shutil.which("jq") is None, reason="requires jq to evaluate the selector")
@pytest.mark.parametrize(
    ("candidates", "expected"),
    [
        # The tracking issue is chosen by title EQUALITY, not by search rank: an
        # older human-filed issue whose title merely contains the phrase must not
        # absorb the comment while the real tracking issue never opens.
        (
            [
                {"number": 7, "title": "Nightly Build scheduled run is failing on macOS"},
                {"number": 9, "title": "Nightly Build scheduled run is failing"},
            ],
            "9",
        ),
        ([{"number": 7, "title": "Nightly Build scheduled run is failing on macOS"}], ""),
        ([], ""),
    ],
)
def test_the_dedupe_selector_matches_the_exact_title_only(
    candidates: list[dict[str, object]], expected: str
) -> None:
    out = subprocess.run(
        ["jq", "-r", _dedupe_selector()],
        input=json.dumps(candidates),
        env={**os.environ, "TITLE": "Nightly Build scheduled run is failing"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    assert out.stdout.strip() == expected


def _run_step(
    tmp_path: Path,
    *,
    open_reply: str,
    closed_reply: str = "",
    list_exit: int = 0,
    label_exit: int = 0,
    comments_json: str = "[]",
    conclusion: str = "failure",
) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run the step with a ``gh`` stub.

    ``issue list --state open`` answers ``open_reply`` and ``--state closed``
    answers ``closed_reply`` (both with ``list_exit``); ``label create`` exits
    ``label_exit``; ``api .../comments?since=...`` answers ``comments_json`` and
    the step's own ``--jq`` filter is then evaluated over it with ``jq``, so the
    bot-only bump count is exercised for real. Every ``gh`` invocation is
    recorded as one ``"$*"`` string.
    """
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    log = tmp_path / "gh.log"
    (tmp_path / "comments.json").write_text(comments_json, encoding="utf-8")
    gh = stubs / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s{_RECORD_SEPARATOR}\' "$*" >> "{log}"\n'
        'if [ "$1 $2" = "label create" ]; then\n'
        f"  exit {label_exit}\n"
        "fi\n"
        'if [ "$1 $2" = "issue list" ]; then\n'
        '  case " $* " in\n'
        f"    *\" --state open \"*) printf '%s' '{open_reply}' ;;\n"
        f"    *\" --state closed \"*) printf '%s' '{closed_reply}' ;;\n"
        "  esac\n"
        f"  exit {list_exit}\n"
        "fi\n"
        'if [ "$1" = "api" ]; then\n'
        "  # Honour the step's own --jq filter over the canned comment list.\n"
        '  while [ "$#" -gt 0 ]; do\n'
        '    if [ "$1" = "--jq" ]; then filter="$2"; break; fi\n'
        "    shift\n"
        "  done\n"
        f'  jq -r "$filter" < "{tmp_path / "comments.json"}"\n'
        "fi\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "GH_TOKEN": "stub",
            "REPO": "example/repo",
            "SOURCE_NAME": "Nightly Build",
            "SOURCE_PATH": ".github/workflows/nightly.yml",
            "SOURCE_CONCLUSION": conclusion,
            "RUN_URL": "https://example.invalid/actions/runs/1",
        }
    )
    out = subprocess.run(
        ["bash", "-c", _step_script()],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    raw = log.read_text(encoding="utf-8") if log.exists() else ""
    calls = [record for record in raw.split(_RECORD_SEPARATOR) if record]
    return out, calls


def _bot_bump(
    body: str = "Still failing (failure): https://example.invalid/actions/runs/0",
) -> dict:
    return {"user": {"login": "github-actions[bot]"}, "body": body}


def _human_comment(body: str = "looking at this") -> dict:
    return {"user": {"login": "someone"}, "body": body}


pytestmark_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="requires jq")


@pytestmark_posix
@pytestmark_jq
def test_a_first_failure_opens_an_issue_with_the_fixed_title(tmp_path: Path) -> None:
    out, calls = _run_step(tmp_path, open_reply="")
    assert out.returncode == 0, out.stderr
    assert len(calls) == 4, calls
    assert calls[0].startswith("label create scheduled-failure --repo example/repo --force")
    assert calls[1].startswith(
        "issue list --repo example/repo --state open --label scheduled-failure --limit 100"
    )
    assert "--json number,title --jq" in calls[1]
    assert calls[2].startswith(
        "issue list --repo example/repo --state closed --label scheduled-failure --limit 100"
    )
    assert "--json number,title,closedAt" in calls[2]
    assert calls[3].startswith(
        "issue create --repo example/repo --title Nightly Build scheduled run is failing"
    )
    assert "--label scheduled-failure" in calls[3]
    assert ".github/workflows/nightly.yml" in calls[3]
    assert "conclusion `failure`" in calls[3]
    assert "https://example.invalid/actions/runs/1" in calls[3]


def test_the_dedupe_lookup_never_goes_through_the_search_index() -> None:
    """``--search`` answers from an index that lags a create by minutes; two
    watched workflows tick every 10 and 15 minutes, so a search-based check
    could miss the issue filed one tick earlier and open a duplicate. Both
    lookups read the issues API directly, narrowed by the watcher's label."""
    script = _step_script()
    list_lines = [line for line in script.splitlines() if "gh issue list" in line]
    assert len(list_lines) == 2, list_lines
    assert "--search" not in script
    assert all("--label scheduled-failure" in line for line in list_lines)


@pytestmark_posix
@pytestmark_jq
def test_a_repeat_failure_comments_instead_of_opening_a_second_issue(tmp_path: Path) -> None:
    out, calls = _run_step(tmp_path, open_reply="4242", conclusion="timed_out")
    assert out.returncode == 0, out.stderr
    assert len(calls) == 4, calls
    assert calls[2].startswith("api repos/example/repo/issues/4242/comments?since=")
    assert "&per_page=100" in calls[2]
    assert calls[3].startswith(
        "issue comment 4242 --repo example/repo --body Still failing (timed_out):"
    )
    assert "https://example.invalid/actions/runs/1" in calls[3]
    assert not any(call.startswith("issue create") for call in calls)


@pytestmark_posix
@pytestmark_jq
def test_a_bump_within_six_hours_is_skipped(tmp_path: Path) -> None:
    """Two watched lanes tick every 10 and 15 minutes; without a throttle one
    persistent break appends ~144 comments a day and buries the run links."""
    out, calls = _run_step(tmp_path, open_reply="4242", comments_json=json.dumps([_bot_bump()]))
    assert out.returncode == 0, out.stderr
    assert len(calls) == 3, calls
    assert calls[2].startswith("api repos/example/repo/issues/4242/comments?since=")
    assert not any(call.startswith("issue comment") for call in calls)
    assert not any(call.startswith("issue create") for call in calls)
    assert "not bumping again" in out.stdout


@pytestmark_posix
@pytestmark_jq
def test_a_human_comment_does_not_silence_the_bump(tmp_path: Path) -> None:
    """Only the watcher's own bumps count against the window: a maintainer
    saying "looking at this" must not drop the next six hours of run links."""
    out, calls = _run_step(
        tmp_path,
        open_reply="4242",
        comments_json=json.dumps([_human_comment(), _human_comment("Still failing? no, fixed")]),
    )
    assert out.returncode == 0, out.stderr
    assert any(call.startswith("issue comment 4242") for call in calls), calls


@pytestmark_posix
@pytestmark_jq
def test_an_issue_closed_within_six_hours_is_not_refiled(tmp_path: Path) -> None:
    """A maintainer who closes the issue while a 10-minute lane is still red
    must not get a fresh issue every tick; the first red run after the window
    files anew."""
    out, calls = _run_step(tmp_path, open_reply="", closed_reply="4241")
    assert out.returncode == 0, out.stderr
    assert not any(call.startswith("issue create") for call in calls), calls
    assert not any(call.startswith("issue comment") for call in calls), calls
    assert "closed within the last 6 hours" in out.stdout


@pytestmark_posix
@pytestmark_jq
def test_the_throttle_window_is_a_real_timestamp(tmp_path: Path) -> None:
    out, calls = _run_step(tmp_path, open_reply="4242", comments_json=json.dumps([_bot_bump()]))
    assert out.returncode == 0, out.stderr
    since = re.search(r"since=([^&]+)&", calls[2])
    assert since, calls[2]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", since.group(1)), since.group(1)


@pytestmark_posix
@pytestmark_jq
def test_a_refused_label_write_does_not_stop_the_filing(tmp_path: Path) -> None:
    """The label ensure is a convenience; refusing it must not turn the one
    messenger for twelve lanes into a silent red run of its own."""
    out, calls = _run_step(tmp_path, open_reply="", label_exit=1)
    assert out.returncode == 0, out.stderr
    assert "Could not ensure the scheduled-failure label" in out.stdout
    assert any(call.startswith("issue create") for call in calls), calls


@pytestmark_posix
@pytestmark_jq
def test_a_failed_dedupe_query_aborts_instead_of_filing_blind(tmp_path: Path) -> None:
    """``set -euo pipefail``: when ``gh issue list`` fails the step must stop,
    not fall through to ``gh issue create`` and open a duplicate."""
    out, calls = _run_step(tmp_path, open_reply="", list_exit=1)
    assert out.returncode != 0
    assert len(calls) == 2, calls
    assert calls[0].startswith("label create")
    assert calls[1].startswith("issue list")
