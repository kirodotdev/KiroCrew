"""The ten reviewer lanes scrub their published text through the base-owned redactor.

A copied ``perl -i -pe`` scrub per lane is wrong on every surface it guards when
it is wrong, and a program inside a ``run:`` body cannot be tested. So each lane
calls ``scripts/scope_redact.py`` staged from the BASE commit. These pins hold
the four properties that make that true: no copy survives, the program comes from
the base and not from the change under review, every call refuses rather than
publishing raw text, and the fork lanes still redact the inherited credential
VALUES. The marker spellings themselves are pinned in ``test_scope_redact.py``.
"""

from __future__ import annotations

import pytest
import yaml
from ai_review_workflows_helpers import SCRUB_CALL, WORKFLOWS, _workflow

REVIEWER_LANES = (
    "claude-review.yml",
    "codex-review.yml",
    "design-review.yml",
    "first-principles-review.yml",
    "ux-review.yml",
    "fork-design-review.yml",
    "fork-first-principles-review.yml",
    "fork-gpt-review.yml",
    "fork-opus-review.yml",
    "fork-ux-review.yml",
)
#: The lanes that check out the merge ref at depth 2. The fork lanes check out the
#: base commit itself, so it is always present there.
SAME_REPO_LANES = tuple(n for n in REVIEWER_LANES if not n.startswith("fork-"))
#: The lanes whose reviewer runs with AWS credentials in its environment.
VALUE_LANES = ("fork-gpt-review.yml", "fork-opus-review.yml")
STAGED = "${{ steps.redactor.outputs.redactor }}"
CREDENTIAL_ENV = ("AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_ACCESS_KEY_ID")


def _steps(name: str) -> list[dict]:
    (job,) = yaml.safe_load(_workflow(name))["jobs"].values()
    return job["steps"]


def _users(name: str) -> list[tuple[int, dict]]:
    return [(i, s) for i, s in enumerate(_steps(name)) if '"$REDACTOR"' in (s.get("run") or "")]


def test_no_copied_scrub_remains_under_workflows() -> None:
    copies = [
        p.name
        for p in sorted(WORKFLOWS.glob("*.yml"))
        if "perl -i -pe" in p.read_text(encoding="utf-8")
    ]
    assert copies == []


@pytest.mark.parametrize("name", REVIEWER_LANES)
def test_the_redactor_is_staged_from_the_base_commit(name: str) -> None:
    (stage,) = [s for s in _steps(name) if s.get("id") == "redactor"]
    run = stage["run"]
    assert 'git show "$BASE_SHA:scripts/scope_redact.py" > "$dest"' in run
    assert "Refusing to publish" in run
    assert "exit 1" in run
    # A copy from the workspace would be the change under review's own program.
    assert "cp scripts/scope_redact.py" not in run


@pytest.mark.parametrize("name", SAME_REPO_LANES)
def test_a_shallow_lane_fetches_the_base_commit_before_staging(name: str) -> None:
    """A depth-2 checkout may not hold ``base.sha``, and ``git show`` then finds nothing."""
    steps = _steps(name)
    stage_at = next(i for i, s in enumerate(steps) if s.get("id") == "redactor")
    fetches = [
        i
        for i, s in enumerate(steps[:stage_at])
        if 'need "$BASE_SHA"' in (s.get("run") or "")
        and (s.get("env") or {}).get("BASE_SHA") == "${{ github.event.pull_request.base.sha }}"
    ]
    assert fetches, name


@pytest.mark.parametrize("name", REVIEWER_LANES)
def test_every_scrubbing_step_runs_after_staging_and_reads_the_staged_copy(name: str) -> None:
    steps = _steps(name)
    stage_at = next(i for i, s in enumerate(steps) if s.get("id") == "redactor")
    users = _users(name)
    assert users, name
    for index, step in users:
        assert index > stage_at, (name, step["name"])
        assert (step.get("env") or {}).get("REDACTOR") == STAGED, (name, step["name"])


@pytest.mark.parametrize("name", REVIEWER_LANES)
def test_every_call_declares_text_mode_and_refuses_on_failure(name: str) -> None:
    """A failed scrub removes the file and fails the step, so nothing publishes it raw."""
    for _, step in _users(name):
        lines = step["run"].splitlines()
        calls = [i for i, ln in enumerate(lines) if SCRUB_CALL in ln]
        assert calls, (name, step["name"])
        for i in calls:
            guard = lines[i - 1].strip()
            assert guard == 'if [ -z "${REDACTOR:-}" ] || [ ! -s "$REDACTOR" ] \\', (name, guard)
            assert lines[i].endswith("; then"), (name, lines[i])
            target = lines[i].removesuffix("; then").split()[-1]
            assert lines[i + 2].strip() == f"rm -f {target}", (name, lines[i + 2])
            assert lines[i + 3].strip() == "exit 1", (name, lines[i + 3])


@pytest.mark.parametrize("name", VALUE_LANES)
def test_the_credential_lanes_redact_the_inherited_values(name: str) -> None:
    calls = [
        ln
        for _, step in _users(name)
        for ln in step["run"].splitlines()
        if SCRUB_CALL in ln and "--redact-env" in ln
    ]
    assert len(calls) == 1, (name, calls)
    for var in CREDENTIAL_ENV:
        assert f"--redact-env {var}" in calls[0], (name, var)
