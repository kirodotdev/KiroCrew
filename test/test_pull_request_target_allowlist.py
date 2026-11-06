"""Pin the set of workflows allowed to use the ``pull_request_target`` trigger.

GitHub blocks ``pull_request_target`` by default in public repositories unless
a repository Actions policy allows it for named workflow paths.
The policy lists exactly the workflows below, so a NEW workflow that adds the
trigger would be blocked silently on GitHub, or would widen the set of jobs
that run with a write token in the base context for fork pull requests. This
test makes either case fail CI instead: adding a workflow here is a deliberate
change that also needs the repository policy updated by an admin (see
``docs/ci/ci-and-reviews.md``, "Fork PRs").

It also pins the property that makes the trigger safe in these five files: no
step checks out the pull request's head. A ``pull_request_target`` job holds a
write token and runs base-branch code; checking out or running head code there
hands that token to a fork.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

# Must match the repository Actions policy that allows pull_request_target.
ALLOWED = frozenset(
    {
        "fork-pr-description.yml",
        "fork-pr-label.yml",
        "fork-workflow-guard.yml",
        "pr-readiness.yml",
        "sensitive-change-review.yml",
    }
)

# Expressions that name the PR head; none may reach a checkout's ref/repository.
HEAD_REFERENCES = (
    "github.event.pull_request.head",
    "github.head_ref",
    "refs/pull/",
)


def _triggers(doc: dict) -> set[str]:
    # PyYAML (YAML 1.1) reads the bare key `on` as boolean True.
    on = doc.get("on", doc.get(True))
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(item) for item in on}
    if isinstance(on, dict):
        return {str(key) for key in on}
    return set()


def _load(path: Path) -> dict:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return doc if isinstance(doc, dict) else {}


def _workflow_files() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def _uses_pull_request_target(path: Path) -> bool:
    return "pull_request_target" in _triggers(_load(path))


def test_trigger_parser_reads_every_on_shape() -> None:
    assert _triggers(yaml.safe_load("on: pull_request_target")) == {"pull_request_target"}
    assert _triggers(yaml.safe_load("on: [push, pull_request_target]")) == {
        "push",
        "pull_request_target",
    }
    assert _triggers(yaml.safe_load("on:\n  pull_request_target:\n    types: [opened]\n")) == {
        "pull_request_target"
    }
    assert _triggers(yaml.safe_load("'on': {schedule: []}")) == {"schedule"}


def test_only_the_allowed_workflows_use_pull_request_target() -> None:
    found = {path.name for path in _workflow_files() if _uses_pull_request_target(path)}
    unexpected = found - ALLOWED
    assert not unexpected, (
        f"{sorted(unexpected)} use pull_request_target but are not in the allowlist. "
        "GitHub blocks the trigger unless the repository Actions policy allows "
        "that workflow path; prefer pull_request or workflow_run instead."
    )
    gone = ALLOWED - found
    assert not gone, (
        f"{sorted(gone)} no longer use pull_request_target: drop them from ALLOWED "
        "and from the repository Actions policy."
    )


def _checkout_steps(doc: dict):
    for job in (doc.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            if str((step or {}).get("uses", "")).startswith("actions/checkout@"):
                yield step


@pytest.mark.parametrize("name", sorted(ALLOWED))
def test_allowed_workflows_never_check_out_the_pull_request_head(name: str) -> None:
    doc = _load(WORKFLOWS / name)
    for step in _checkout_steps(doc):
        params = step.get("with") or {}
        for key in ("ref", "repository"):
            value = str(params.get(key, ""))
            for marker in HEAD_REFERENCES:
                assert marker not in value, f"{name}: checkout {key}={value!r} names the PR head"
