"""PR Readiness does not fetch, one by one, runs it already holds a page of.

``gh_head_runs`` answers "which runs of <event> are on this head". When the
listing for that event is empty it does not take the empty answer on trust: it
reads the head's check-runs and fetches each run they name, keeping the ones
whose event is the one asked for. A caller that already holds a page of the
head's runs for ANOTHER event hands it over, and the runs on it are not
fetched, because a run on a page listed for another event cannot be one of
<event>.

The lookup this matters for is CodeQL's default-setup (``dynamic``) run, made
on every evaluation and again before every success is published. Where default
setup is off that listing is always empty, so the fallback runs every time.
Both callers hold two pages by then -- the head's ``pull_request`` runs and its
``pull_request_target`` runs -- and both hand over both.

Three things are held here, on the workflow's own scripts:

* the helper fetches exactly the named runs that are on none of the pages it
  was given, and its answer does not depend on which pages it was given;
* each caller hands over both pages, so no run on either is fetched, while a
  run on neither still is;
* what each caller publishes is what it publishes when it hands over the
  ``pull_request`` page alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import test_pr_readiness_evaluate as evaluate
from test_pr_readiness_evaluate import pytestmark  # noqa: F401 - same POSIX bash + jq skip
from test_pr_readiness_publish import (
    SHA,
    Result,
)
from test_pr_readiness_publish import Runner as StepRunner
from test_pr_readiness_publish import (
    _recheck_runner,
    _step,
)

PUBLISH_STEP = "Publish status and label"
REPO = "kirodotdev/KiroCrew"

# Calls the helper the way a step does, with one known page per fixture file
# in name order. An empty file is a page the caller does not hold.
LOOKUP = r"""
set -euo pipefail
source "$RUNNER_TEMP/gh-retry.sh"
pages=()
for f in "$FIXTURES"/known_*.json; do
  [ -e "$f" ] || continue
  pages+=("$(cat "$f")")
done
gh_head_runs "$REPO" "$SHA" dynamic ${pages[@]+"${pages[@]}"}
"""

PR_RUNS = (21, 22)
PT_RUNS = (23, 24)
REVIEW_RUN = 25
DYNAMIC_RUN = 26
EVENT_OF = {
    **dict.fromkeys(PR_RUNS, "pull_request"),
    **dict.fromkeys(PT_RUNS, "pull_request_target"),
    REVIEW_RUN: "pull_request_review",
    DYNAMIC_RUN: "dynamic",
}


def _run(run_id: int, event: str) -> dict[str, object]:
    return {
        "id": run_id,
        "event": event,
        "head_sha": SHA,
        "status": "completed",
        "conclusion": "success",
    }


def _page(*run_ids: int) -> str:
    return json.dumps({"workflow_runs": [_run(i, EVENT_OF.get(i, "push")) for i in run_ids]})


def _named(run_id: int) -> dict[str, object]:
    """A check-run of a job in workflow run `run_id`."""
    return {
        "id": 9000 + run_id,
        "name": f"job of {run_id}",
        "status": "completed",
        "conclusion": "success",
        "details_url": f"https://github.com/{REPO}/actions/runs/{run_id}/job/{9000 + run_id}",
    }


def _fetched(result: Result) -> set[int]:
    """The runs read one by one."""
    out = set()
    for call in result.calls:
        _, sep, rest = call.partition("/actions/runs/")
        head = rest.split()[0].split("?")[0].split("/")[0] if sep else ""
        if head.isdigit():
            out.add(int(head))
    return out


@pytest.fixture
def lookup(tmp_path: Path) -> StepRunner:
    """A head with no `dynamic` listing whose check-runs name six runs."""
    runner = StepRunner(tmp_path, LOOKUP)
    (runner.fixtures / "codeql_runs.json").write_text(json.dumps({"workflow_runs": []}))
    (runner.fixtures / "fork_checks.json").write_text(
        json.dumps({"check_runs": [_named(i) for i in EVENT_OF]})
    )
    for run_id, event in EVENT_OF.items():
        (runner.fixtures / f"run_{run_id}.json").write_text(json.dumps(_run(run_id, event)))
    return runner


def _hold(runner: StepRunner, pages: tuple[str, ...]) -> None:
    for n, page in enumerate(pages):
        (runner.fixtures / f"known_{n}.json").write_text(page)


PAGE_SETS = {
    "none": (),
    "one page the caller does not hold": ("",),
    "pull_request": (_page(*PR_RUNS),),
    "pull_request_target": (_page(*PT_RUNS),),
    "both": (_page(*PR_RUNS), _page(*PT_RUNS)),
    "both, the other way round": (_page(*PT_RUNS), _page(*PR_RUNS)),
    "both, around a page not held": (_page(*PR_RUNS), "", _page(*PT_RUNS)),
    "both on one page": (_page(*PR_RUNS, *PT_RUNS),),
    "both and a third": (_page(*PR_RUNS), _page(*PT_RUNS), _page(REVIEW_RUN)),
    "a page of runs the check-runs do not name": (_page(97, 98),),
}


def _held(pages: tuple[str, ...]) -> set[int]:
    return {r["id"] for page in pages if page for r in json.loads(page)["workflow_runs"]}


@pytest.mark.parametrize("pages", PAGE_SETS.values(), ids=PAGE_SETS.keys())
def test_only_the_named_runs_on_no_held_page_are_fetched(
    lookup: StepRunner, pages: tuple[str, ...]
) -> None:
    _hold(lookup, pages)
    result = lookup.run()
    assert result.ok, result.proc.stderr
    assert _fetched(result) == set(EVENT_OF) - _held(pages)


@pytest.mark.parametrize("pages", PAGE_SETS.values(), ids=PAGE_SETS.keys())
def test_the_answer_does_not_depend_on_the_pages_held(
    lookup: StepRunner, pages: tuple[str, ...]
) -> None:
    """No held page lists the `dynamic` run, so it is fetched and it is the
    whole answer, whatever else was or was not fetched."""
    _hold(lookup, pages)
    result = lookup.run()
    assert result.ok, result.proc.stderr
    assert json.loads(result.proc.stdout) == {"workflow_runs": [_run(DYNAMIC_RUN, "dynamic")]}


@pytest.mark.parametrize("position", [0, 1, 2])
def test_a_held_page_that_cannot_be_parsed_fails_the_lookup(
    lookup: StepRunner, position: int
) -> None:
    """An unreadable page is not an empty one: nothing is known about the runs
    it listed, so the lookup answers nothing at all."""
    pages = [_page(*PR_RUNS), _page(*PT_RUNS), _page(REVIEW_RUN)]
    pages[position] = '{"workflow_runs": [{"id": 2'
    _hold(lookup, tuple(pages))
    result = lookup.run()
    assert not result.ok
    assert result.proc.stdout == ""
    assert _fetched(result) == set()


def test_a_run_on_no_held_page_that_cannot_be_read_fails_the_lookup(lookup: StepRunner) -> None:
    _hold(lookup, PAGE_SETS["both"])
    (lookup.fixtures / f"run_{REVIEW_RUN}.json").unlink()
    (lookup.fixtures / "fail_lane_run").touch()
    result = lookup.run()
    assert not result.ok
    assert result.proc.stdout == ""


def test_a_listing_that_answers_is_taken_and_nothing_else_is_read(lookup: StepRunner) -> None:
    listed = {"workflow_runs": [_run(DYNAMIC_RUN, "dynamic")]}
    (lookup.fixtures / "codeql_runs.json").write_text(json.dumps(listed))
    _hold(lookup, PAGE_SETS["both"])
    result = lookup.run()
    assert result.ok, result.proc.stderr
    assert json.loads(result.proc.stdout) == listed
    assert _fetched(result) == set()
    assert not [c for c in result.calls if "/check-runs" in c]


# --- the two callers ---------------------------------------------------------


def _one_page_only(script: str, second_page: str) -> str:
    """`script` with its `dynamic` lookup handed the `pull_request` page alone:
    the caller this change replaces, rebuilt from the workflow's own text."""
    assert script.count(second_page) == 1, second_page
    return script.replace(second_page, "")


def _recheck(tmp_path: Path, script: str) -> tuple[StepRunner, int]:
    """A green same-repo head, default setup off, whose check-runs name its
    Sensitive Change Review run and a review-approval run."""
    runner = _recheck_runner(StepRunner(tmp_path, script), "completed", codeql_runs_present=False)
    (pt_run,) = json.loads((runner.fixtures / "pt_runs.json").read_text())["workflow_runs"]
    checks = json.loads((runner.fixtures / "fork_checks.json").read_text())
    checks["check_runs"] += [_named(pt_run["id"]), _named(REVIEW_RUN)]
    (runner.fixtures / "fork_checks.json").write_text(json.dumps(checks))
    for run in ({**pt_run, "head_sha": SHA}, _run(REVIEW_RUN, "pull_request_review")):
        (runner.fixtures / f"run_{run['id']}.json").write_text(json.dumps(run))
    return runner, pt_run["id"]


def test_the_re_check_fetches_no_run_on_either_page_it_holds(tmp_path: Path) -> None:
    runner, pt_run = _recheck(tmp_path, _step(PUBLISH_STEP))
    held = {
        r["id"] for r in json.loads((runner.fixtures / "runs.json").read_text())["workflow_runs"]
    }
    result = runner.run()
    assert result.published is not None
    assert result.published["state"] == "success"
    assert _fetched(result) == {REVIEW_RUN}
    assert not _fetched(result) & (held | {pt_run})


def test_the_re_check_publishes_what_it_did_with_one_page(tmp_path: Path) -> None:
    script = _step(PUBLISH_STEP)
    runner, pt_run = _recheck(tmp_path / "two", script)
    before, _ = _recheck(tmp_path / "one", _one_page_only(script, ' "$pt_page"'))
    now, then = runner.run(), before.run()
    assert pt_run in _fetched(then) and pt_run not in _fetched(now)
    assert now.published == then.published
    assert now.published is not None


SCR_RUN = 88100


def _evaluation(tmp_path: Path) -> evaluate.Runner:
    """A green same-repo head, default setup off and the checked-in scan
    green, whose check-runs name its Sensitive Change Review run and a
    review-approval run."""
    runner = evaluate.Runner(tmp_path)
    (runner.fixtures / "codeql_runs.json").write_text(json.dumps({"workflow_runs": []}))
    (runner.fixtures / "codeql_pr_runs.json").write_text(
        json.dumps(
            {
                "workflow_runs": [
                    {
                        "id": 88200,
                        "run_attempt": 1,
                        "head_repository": {"full_name": REPO},
                        "head_branch": "feat/x",
                        "path": ".github/workflows/codeql.yml",
                        "status": "completed",
                        "conclusion": "success",
                        "created_at": "2026-08-11T00:00:00Z",
                        "run_started_at": "2026-08-11T00:00:00Z",
                    }
                ]
            }
        )
    )
    (runner.fixtures / "pt_runs.json").write_text(
        evaluate._runs_json(
            "sensitive-change-review.yml",
            [{"id": SCR_RUN, "run_attempt": 1, "status": "completed", "conclusion": "success"}],
        )
    )
    checks = json.loads((runner.fixtures / "check_runs.json").read_text())
    checks["check_runs"] += [_named(SCR_RUN), _named(REVIEW_RUN)]
    (runner.fixtures / "check_runs.json").write_text(json.dumps(checks))
    sha = runner.env["SHA"]
    for run_id, event in ((SCR_RUN, "pull_request_target"), (REVIEW_RUN, "pull_request_review")):
        (runner.fixtures / f"run_{run_id}.json").write_text(
            json.dumps({**_run(run_id, event), "head_sha": sha})
        )
    return runner


def _evaluation_fetched(runner: evaluate.Runner) -> set[int]:
    out = set()
    for call in (runner.fixtures / "calls").read_text().splitlines():
        _, sep, rest = call.partition("/actions/runs/")
        head = rest.split("?")[0].split("/")[0] if sep else ""
        if head.isdigit():
            out.add(int(head))
    return out


def test_the_evaluation_fetches_no_run_on_either_page_it_holds(tmp_path: Path) -> None:
    runner = _evaluation(tmp_path)
    proc, outputs = runner.evaluate()
    assert proc.returncode == 0, proc.stderr
    assert outputs["status_state"] == "success"
    assert _evaluation_fetched(runner) == {REVIEW_RUN}


def test_the_evaluation_scores_what_it_did_with_one_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now_runner, then_runner = _evaluation(tmp_path / "two"), _evaluation(tmp_path / "one")
    now_proc, now = now_runner.evaluate()
    one_page = _one_page_only(evaluate._evaluate_script(), ' "$pt_runs"')
    monkeypatch.setattr(evaluate, "_evaluate_script", lambda: one_page)
    then_proc, then = then_runner.evaluate()
    assert now_proc.returncode == 0 and then_proc.returncode == 0, then_proc.stderr
    assert SCR_RUN in _evaluation_fetched(then_runner)
    assert SCR_RUN not in _evaluation_fetched(now_runner)
    assert now == then
    assert evaluate._lane_log(now_proc) == evaluate._lane_log(then_proc)
