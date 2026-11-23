"""CodeQL's `init` does not lint its workflow file; this file does.

`github/codeql-action/init` lints the workflow it runs from and prints a
warning for each finding. To find the file it asks the API twice
(`GET actions/runs/<id>`, then the workflow), with the job's token, which
draws on the installation's shared hourly REST pool. Every job that runs
`init` sets `CODEQL_ACTION_SKIP_WORKFLOW_VALIDATION` to `true`, which `init`
reads before it asks (`checkWorkflow` in the action's `src/workflow.ts`), so
neither request is made.

Two things follow, and each has its tests here:

- The action's source describes the variable as "intended for internal use".
  A version that stops reading it makes both requests again and says nothing.
  So the variable is held to the action commit its reader was last read at:
  a bump of the pin fails here until someone reads `checkWorkflow` in the new
  source and moves `SKIP_READ_AT`.
- The warnings `init` no longer prints are four checks of the workflow file
  (`getWorkflowErrors`, same source file). `findings` applies the same four
  to every workflow that uses the action, so a finding fails a test instead
  of printing a warning.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
ACTION = "github/codeql-action/"
SKIP = "CODEQL_ACTION_SKIP_WORKFLOW_VALIDATION"

# The `github/codeql-action` commit at which `checkWorkflow` was read and found
# to skip the lint, and the two API requests under it, when SKIP is "true".
# Moving the pin in a workflow means reading it again at the new commit:
#   src/workflow.ts     checkWorkflow -- the `EnvVar.SKIP_WORKFLOW_VALIDATION`
#                       test that guards `validateWorkflow`
#   src/environment.ts  SKIP_WORKFLOW_VALIDATION -- the variable's name
# If the new source no longer reads the variable, delete it from the workflow
# along with these tests' claim on it; the lint below stays either way.
SKIP_READ_AT = "6f2ac81f5cd81fe351a197ba0b00d91d03cd2aee"

# The CodeQL CLI's own language aliases (`codeql resolve languages
# --extractor-include-aliases`, CLI 2.27.5): two matrix entries that name one
# extractor scan one language twice. The CLI is not installed where this test
# runs, so its table is restated; a name absent from it is its own extractor.
EXTRACTOR_OF = {
    "c": "cpp",
    "c++": "cpp",
    "c-c++": "cpp",
    "c-cpp": "cpp",
    "c#": "csharp",
    "java-kotlin": "java",
    "kotlin": "java",
    "javascript-typescript": "javascript",
    "typescript": "javascript",
}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _steps(job: dict) -> list[dict]:
    steps = job.get("steps")
    return [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []


def _action_steps(job: dict) -> list[dict]:
    return [s for s in _steps(job) if str(s.get("uses", "")).startswith(ACTION)]


def _scanning_workflows() -> Iterator[tuple[str, dict]]:
    for path in sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]):
        doc = _load(path)
        jobs = doc.get("jobs") or {}
        if any(_action_steps(job) for job in jobs.values()):
            yield path.name, doc


def _init_jobs() -> Iterator[tuple[str, str, dict]]:
    for name, doc in _scanning_workflows():
        for job_id, job in doc["jobs"].items():
            if any(s["uses"].startswith(f"{ACTION}init@") for s in _action_steps(job)):
                yield name, job_id, job


SCANNING = dict(_scanning_workflows())
INIT_JOBS = list(_init_jobs())


def _triggers(doc: dict) -> set[str]:
    # YAML 1.1 reads the bare key `on` as the boolean true.
    on = doc.get("on", doc.get(True))
    if isinstance(on, str):
        return {on}
    return set(on or ())


def findings(doc: dict) -> set[str]:
    """The codes `getWorkflowErrors` would report for this workflow, with the
    per-job checks applied to every job instead of the one job that is running."""
    found: set[str] = set()
    refs: set[str] = set()
    for job in (doc.get("jobs") or {}).values():
        languages = ((job.get("strategy") or {}).get("matrix") or {}).get("language")
        if isinstance(languages, list):
            extractors = [EXTRACTOR_OF.get(str(lang), str(lang)) for lang in languages]
            if len(set(extractors)) != len(extractors):
                found.add("DuplicateLanguageInMatrix")
        if any(str(s.get("run", "")).strip() == "git checkout HEAD^2" for s in _steps(job)):
            found.add("CheckoutWrongHead")
        refs.update(s["uses"].split("@")[-1] for s in _action_steps(job) if "@" in s["uses"])
    if len(refs) > 1:
        found.add("InconsistentActionVersion")
    triggers = _triggers(doc)
    if "pull_request" in triggers and not triggers & {"push", "workflow_call"}:
        found.add("MissingPushHook")
    return found


def test_some_workflow_runs_the_scan() -> None:
    """Every test below ranges over these; an empty range would pass them all."""
    assert INIT_JOBS
    assert {name for name, _, _ in INIT_JOBS} <= set(SCANNING)


@pytest.mark.parametrize(
    ("name", "job_id", "job"), INIT_JOBS, ids=lambda v: v if isinstance(v, str) else ""
)
def test_every_job_that_runs_init_turns_its_workflow_lint_off(
    name: str, job_id: str, job: dict
) -> None:
    """On the job, so that it reaches `init` whichever step runs it; the action
    compares with the string `true` and nothing else."""
    assert (job.get("env") or {}).get(SKIP) == "true", f"{name}:{job_id}"
    for step in _steps(job):
        assert SKIP not in (step.get("env") or {}), step


@pytest.mark.parametrize(
    ("name", "job_id", "job"), INIT_JOBS, ids=lambda v: v if isinstance(v, str) else ""
)
def test_the_variable_is_read_by_the_action_commit_the_job_pins(
    name: str, job_id: str, job: dict
) -> None:
    """A new pin has not been read. See SKIP_READ_AT for what to read and what
    to do with the answer."""
    pins = {s["uses"].split("@")[-1] for s in _action_steps(job)}
    assert pins == {SKIP_READ_AT}, f"{name}:{job_id} pins {sorted(pins)}"


@pytest.mark.parametrize("name", sorted(SCANNING))
def test_a_workflow_that_scans_has_none_of_the_findings_init_would_warn_of(name: str) -> None:
    assert findings(SCANNING[name]) == set()


def _first_scanning_job(doc: dict) -> dict:
    return next(job for job in doc["jobs"].values() if _action_steps(job))


def _one_step_on_another_version(doc: dict) -> None:
    step = _action_steps(_first_scanning_job(doc))[-1]
    step["uses"] = step["uses"].split("@")[0] + "@" + "0" * 40


def _checkout_of_the_pr_head(doc: dict) -> None:
    _first_scanning_job(doc)["steps"].insert(1, {"run": "git checkout HEAD^2\n"})


def _pull_request_as_the_only_trigger(doc: dict) -> None:
    doc.pop(True, None)
    doc["on"] = ["pull_request"]


def _one_language_under_two_names(doc: dict) -> None:
    job = _first_scanning_job(doc)
    job.setdefault("strategy", {}).setdefault("matrix", {})["language"] = [
        "typescript",
        "python",
        "javascript",
    ]


BREAKS: dict[str, Callable[[dict], None]] = {
    "InconsistentActionVersion": _one_step_on_another_version,
    "CheckoutWrongHead": _checkout_of_the_pr_head,
    "MissingPushHook": _pull_request_as_the_only_trigger,
    "DuplicateLanguageInMatrix": _one_language_under_two_names,
}


@pytest.mark.parametrize("code", sorted(BREAKS))
@pytest.mark.parametrize("name", sorted(SCANNING))
def test_each_finding_is_reported_when_the_workflow_is_given_it(name: str, code: str) -> None:
    """The clean result above means something only if each check can fire on
    this very file."""
    broken = copy.deepcopy(SCANNING[name])
    BREAKS[code](broken)
    assert findings(broken) == {code}


def test_a_trigger_is_found_in_every_shape_the_key_takes() -> None:
    for on in ("pull_request", ["pull_request"], {"pull_request": {"branches": ["main"]}}):
        assert findings({"on": on, "jobs": {}}) == {"MissingPushHook"}, on
    for on in (
        ["pull_request", "push"],
        {"pull_request": None, "workflow_call": None},
        "push",
        None,
    ):
        assert findings({"on": on, "jobs": {}}) == set(), on
