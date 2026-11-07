"""Only the workflows the repository's Actions event policy allows use `pull_request_target`.

Why, and what adding one takes: docs/ci/ci-and-reviews.md.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

ALLOWED = {
    "fork-pr-description.yml",
    "fork-pr-label.yml",
    "fork-workflow-guard.yml",
    "pr-readiness.yml",
    "sensitive-change-review.yml",
}


def _triggers(path: Path) -> set[str]:
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    # `on` is a YAML 1.1 boolean, so PyYAML keys the trigger block on True.
    on = spec.get("on", spec.get(True))
    return {on} if isinstance(on, str) else set(on)


def test_only_the_policy_allowed_workflows_use_pull_request_target() -> None:
    users = {
        path.name
        for path in WORKFLOWS.iterdir()
        if path.suffix in {".yml", ".yaml"} and "pull_request_target" in _triggers(path)
    }

    assert (
        users == ALLOWED
    ), "a repository admin must change the Actions event policy's workflow paths to match"
