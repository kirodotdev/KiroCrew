"""PR Hygiene's green log stays quiet.

Every step has to assemble its `::error::` prefix at runtime. Actions echoes each
`run:` block's source into the log whether that branch executes or not, so a
literal annotation string is printed by passing runs too, where it reads as a real
failure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "pr-hygiene.yml"


def _hygiene_job() -> dict[str, Any]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return document["jobs"]["pr-hygiene"]


class TestTheGreenLogDoesNotImpersonateAFailure:
    def test_no_step_writes_an_annotation_literally(self) -> None:
        # The whole defect: a literal prefix is printed by a passing run too,
        # because Actions renders the script source regardless of what executes.
        offenders = [
            step.get("name")
            for step in _hygiene_job()["steps"]
            if "::error::" in (step.get("run") or "")
        ]
        assert offenders == []

    def test_every_failing_step_still_assembles_a_real_annotation(self) -> None:
        # Removing the literal must not mute the annotation. A step that can fail
        # and has no way to say so is the opposite defect.
        failing = [step for step in _hygiene_job()["steps"] if "exit 1" in (step.get("run") or "")]
        assert failing, "expected pr-hygiene steps that can fail"
        for step in failing:
            assert "err()" in step["run"], f"{step.get('name')} cannot emit an annotation"
