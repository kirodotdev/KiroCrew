"""CodeQL's `init` does not lint its workflow file, and so does not ask the API for it.

`github/codeql-action/init` lints the workflow it runs from and prints a
warning for each finding. To find the file it asks the API twice
(`GET actions/runs/<id>`, then the workflow), with the job's token, which
draws on the installation's shared hourly REST pool. The job that runs `init`
sets `CODEQL_ACTION_SKIP_WORKFLOW_VALIDATION` to `true`, which `init` reads
before it asks (`checkWorkflow` in the action's `src/workflow.ts`), so neither
request is made.

The action's source describes the variable as "intended for internal use". A
version that stops reading it makes both requests again and says nothing. So
the variable is held to the action commit its reader was last read at: a bump
of the pin fails here until someone reads `checkWorkflow` in the new source and
moves `SKIP_READ_AT`.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CODEQL_FILE = "codeql.yml"
ACTION = "github/codeql-action/"
INIT = f"{ACTION}init@"
SKIP = "CODEQL_ACTION_SKIP_WORKFLOW_VALIDATION"

# The `github/codeql-action` commit at which `checkWorkflow` was read and found
# to skip the lint, and the two API requests under it, when SKIP is "true".
# Moving the pin in the workflow means reading it again at the new commit:
#   src/workflow.ts     checkWorkflow -- the `EnvVar.SKIP_WORKFLOW_VALIDATION`
#                       test that guards `validateWorkflow`
#   src/environment.ts  SKIP_WORKFLOW_VALIDATION -- the variable's name
# If the new source does not read the variable, delete it from the workflow
# along with this file.
SKIP_READ_AT = "6f2ac81f5cd81fe351a197ba0b00d91d03cd2aee"


def _action_steps(job: dict) -> list[dict]:
    return [s for s in job.get("steps") or [] if str(s.get("uses", "")).startswith(ACTION)]


JOBS = yaml.safe_load((WORKFLOWS / CODEQL_FILE).read_text(encoding="utf-8"))["jobs"]
INIT_JOBS = {
    job_id: job
    for job_id, job in JOBS.items()
    if any(s["uses"].startswith(INIT) for s in _action_steps(job))
}


def test_init_runs_in_this_workflow_and_no_other() -> None:
    """The tests below read one file. A second workflow that runs `init` would
    make its own two requests unseen by them."""
    assert INIT_JOBS
    users = {
        path.name
        for path in [*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]
        if INIT in path.read_text(encoding="utf-8")
    }
    assert users == {CODEQL_FILE}


def test_every_job_that_runs_init_turns_its_workflow_lint_off() -> None:
    """On the job, so that it reaches `init` whichever step runs it; the action
    compares with the string `true` and nothing else."""
    for job_id, job in INIT_JOBS.items():
        assert (job.get("env") or {}).get(SKIP) == "true", job_id
        for step in job["steps"]:
            assert SKIP not in (step.get("env") or {}), step


def test_the_variable_is_read_by_the_action_commit_the_job_pins() -> None:
    """A new pin has not been read. See SKIP_READ_AT for what to read and what
    to do with the answer."""
    for job_id, job in INIT_JOBS.items():
        pins = {s["uses"].split("@")[-1] for s in _action_steps(job)}
        assert pins == {SKIP_READ_AT}, f"{job_id} pins {sorted(pins)}"
