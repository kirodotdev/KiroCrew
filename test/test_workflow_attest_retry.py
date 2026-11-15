"""Every provenance attestation in a workflow gets exactly one paused retry.

``actions/attest-build-provenance`` mints its attestation over the network
(Sigstore Fulcio and Rekor) and has no retry of its own, so a transient DNS or
connection error fails a whole release lane. Each attest step is therefore
written as three steps, the shape ``dependency-review.yml`` already uses:

1. the attestation, with an ``id`` and ``continue-on-error: true``;
2. a pause, run only when step 1's outcome is ``failure``;
3. the identical attestation again, same action pin and same ``with``, run on
   the same condition and WITHOUT ``continue-on-error``, so a second failure
   fails the job.

Steps 2 and 3 repeat step 1's own ``if`` inside theirs, so a gate that keeps
the attestation off a promoted or un-staged artifact stays readable on the
retry too. This test scans every workflow, so a new attest step that lacks the
retry, or a retry that drifts from its first attempt, fails here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

_ATTEST_ACTIONS = ("actions/attest-build-provenance@", "actions/attest@")


def _is_attest(step: dict[str, Any]) -> bool:
    return str(step.get("uses") or "").startswith(_ATTEST_ACTIONS)


def _bare(condition: object) -> str:
    text = str(condition).strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
    return text


def _retry_condition(first: dict[str, Any]) -> str:
    outcome = f"steps.{first['id']}.outcome == 'failure'"
    if "if" not in first:
        return f"${{{{ {outcome} }}}}"
    return f"${{{{ ({_bare(first['if'])}) && {outcome} }}}}"


def _violations_in_steps(where: str, steps: list[Any]) -> list[str]:
    found: list[str] = []
    retries: set[int] = set()
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or not _is_attest(step) or index in retries:
            continue
        name = f"{where}: {step.get('name', step['uses'])!r}"
        if step.get("continue-on-error") is not True or not step.get("id"):
            found.append(f"{name} needs an id and continue-on-error: true, then a paused retry")
            continue
        if index + 2 >= len(steps):
            found.append(f"{name} has no pause and retry after it")
            continue
        pause, retry = steps[index + 1], steps[index + 2]
        expected = _retry_condition(step)
        if not isinstance(pause, dict) or pause.get("if") != expected:
            found.append(f"{name}: the next step must be the pause, if: {expected}")
        elif not str(pause.get("run", "")).strip().startswith("sleep "):
            found.append(f"{name}: the pause must run `sleep <seconds>`")
        if not isinstance(retry, dict) or not _is_attest(retry):
            found.append(f"{name}: the step after the pause must be the retry attestation")
            continue
        retries.add(index + 2)
        if retry.get("if") != expected:
            found.append(f"{name}: the retry must run if: {expected}")
        if "continue-on-error" in retry:
            found.append(f"{name}: the retry must not continue on error, or two failures pass")
        if retry.get("uses") != step.get("uses") or retry.get("with") != step.get("with"):
            found.append(f"{name}: the retry must use the same action pin and the same with")
    return found


def _violations(workflows: Path = WORKFLOWS) -> tuple[list[str], int]:
    found: list[str] = []
    attests = 0
    for path in sorted(workflows.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            continue
        for job_id, job in (doc.get("jobs") or {}).items():
            steps = job.get("steps") if isinstance(job, dict) else None
            if not isinstance(steps, list):
                continue
            attests += sum(1 for s in steps if isinstance(s, dict) and _is_attest(s))
            found.extend(_violations_in_steps(f"{path.name}:{job_id}", steps))
    return found, attests


def test_every_attestation_has_one_paused_retry() -> None:
    found, attests = _violations()
    assert attests, "no attest step found: the scanner no longer sees the workflows"
    assert not found, "\n".join(found)


def _first(**overrides: Any) -> dict[str, Any]:
    step = {
        "name": "Attest",
        "id": "attest_x",
        "if": "env.HAS_SIGNING_SECRETS && !inputs.promote",
        "continue-on-error": True,
        "uses": "actions/attest-build-provenance@abc # v4",
        "with": {"subject-path": "dist/x"},
    }
    step.update(overrides)
    return step


_GATE = "${{ (env.HAS_SIGNING_SECRETS && !inputs.promote) && steps.attest_x.outcome == 'failure' }}"


def _pause() -> dict[str, Any]:
    return {"name": "Pause", "if": _GATE, "run": "sleep 45"}


def _retry(**overrides: Any) -> dict[str, Any]:
    step = {
        "name": "Attest (retry)",
        "if": _GATE,
        "uses": "actions/attest-build-provenance@abc # v4",
        "with": {"subject-path": "dist/x"},
    }
    step.update(overrides)
    return step


def test_the_three_step_shape_passes() -> None:
    assert _violations_in_steps("w", [_first(), _pause(), _retry()]) == []


def test_a_bare_attestation_is_refused() -> None:
    bare = _first()
    del bare["continue-on-error"]
    assert _violations_in_steps("w", [bare])


def test_a_retry_that_continues_on_error_is_refused() -> None:
    assert _violations_in_steps("w", [_first(), _pause(), _retry(**{"continue-on-error": True})])


def test_a_retry_with_different_subjects_is_refused() -> None:
    other = _retry(**{"with": {"subject-path": "dist/other"}})
    assert _violations_in_steps("w", [_first(), _pause(), other])


def test_a_retry_that_drops_the_first_gate_is_refused() -> None:
    loose = "${{ steps.attest_x.outcome == 'failure' }}"
    assert _violations_in_steps("w", [_first(), _pause(), _retry(**{"if": loose})])


def test_a_first_attempt_without_a_retry_is_refused() -> None:
    assert _violations_in_steps("w", [_first()])
