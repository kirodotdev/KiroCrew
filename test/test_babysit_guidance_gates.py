"""The guidance an agent copies must demonstrate a form that actually gates.

Every place that teaches an agent to arm a babysit loop must show the subject
as a full PR URL. Inference refuses a bare number, so a loop armed
from the example stayed on the plain timer and every interval spent a turn -- the
saving read as zero while the mechanism worked perfectly. Measured on a live
gateway: of six loops that asked to be gated, five had written a bare number and
only the one that wrote a URL was gated.

These tests assert the shipped text against the real inference function rather
than against a spelling, so they fail if an example is ever reworded back into a
form that cannot select a subject -- including the ``<owner>/<repo>`` placeholder
style, which reads fine to a human and does not infer.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from kiro_crew.autonudge_authz import authorize_and_add_nudge
from kiro_crew.monitoring import github_pull_request
from kiro_crew.monitoring.github_pull_request import _normalize_checks
from kiro_crew.probes.targets import infer

ROOT = Path(__file__).resolve().parents[1]
PROMPT = ROOT / "src" / "kiro_crew" / "config" / "prompt.md"
BABYSIT_SKILL = (
    ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "babysit" / "SKILL.md"
)
SPEC = ROOT / "docs" / "system-specs" / "modules" / "babysit-pr-watch.md"
MONITOR_SPEC = ROOT / "docs" / "system-specs" / "modules" / "monitor-architecture.md"

_URL = re.compile(r"https://github\.com/\S+?/pull/\d+")


def _gating_urls(text: str) -> list[str]:
    """Every pull-request URL in ``text`` that inference actually accepts."""
    return [url for url in _URL.findall(text) if infer("Check %s now." % url) is not None]


def test_the_monitor_start_guidance_demonstrates_a_gating_subject() -> None:
    body = PROMPT.read_text(encoding="utf-8")
    start = body.index("**Using monitor_start:**")
    block = body[start : start + 1200]
    assert _gating_urls(block), (
        "the monitor_start guidance in prompt.md must show the subject as a pull-request "
        "URL that inference accepts; a bare 'PR #123' leaves the loop ungated"
    )


def test_the_babysit_example_message_names_its_subject_by_url() -> None:
    body = BABYSIT_SKILL.read_text(encoding="utf-8")
    # Anchor on the Example section: ``monitor_start(`` also appears far above it
    # as the tool's signature in the Overview, which carries no subject at all.
    example = body.index("## Example")
    start = body.index("monitor_start(", example)
    message = body[start : start + 400]
    assert _gating_urls(message), (
        "the babysit skill's worked example must name the pull request by a URL "
        "inference accepts, since the armed message is what agents copy"
    )


def test_a_bare_number_is_still_refused_so_the_ratchet_means_something() -> None:
    # Guards the guard: if inference ever started accepting a bare reference, the
    # two tests above would pass on the old wording and stop protecting anything.
    assert infer("Babysit PR #8184 (kirodotdev/KiroCrew), branch fix/x") is None
    assert infer("Check https://github.com/<owner>/<repo>/pull/123 now.") is None


def test_the_monitor_spec_names_the_function_that_enforces_the_collapse() -> None:
    """A rule whose named enforcer does not exist is a rule enforced nowhere.

    That section's own framing is that each rule is code, or says in its own text
    where an implementation still diverges. So it names the function carrying this
    one: a rename leaves the sentence pointing at nothing while still reading as an
    enforced guarantee, and a note that the two implementations diverge sends an
    agent to reconcile by hand what the engine settles for it.
    """
    spec = " ".join(MONITOR_SPEC.read_text(encoding="utf-8").split())

    assert "_collapse_superseded_rows" in spec
    assert callable(github_pull_request._collapse_superseded_rows)
    assert "diverge on both halves of the rule" in spec, (
        "a divergence declared for one half sends a reader to reconcile the wrong one; "
        "the sibling differs on identity AND on ordering"
    )
    assert (
        "On ordering, it takes the newest by the check row's `startedAt`" in spec
    ), "the ordering half has to name the field, or the declaration cannot be checked"
    skill = " ".join(BABYSIT_SKILL.read_text(encoding="utf-8").split())
    assert "does NOT yet follow this rule" in skill, (
        "an agent reading the bundled tool's output has to know it can drop a live "
        "failure the typed provider keeps"
    )
    assert "workflow DEFINITION plus the check name" in spec, (
        "the spec has to name the identity the engine keys on; a reader told only "
        "that it collapses cannot tell a label-keyed collapse from this one"
    )


def test_the_skill_states_the_collapse_rule_the_typed_provider_implements() -> None:
    """The collapse rule and the provider must not disagree in silence.

    A hand-read rollup collapses re-run attempts to the newest per identity, and
    the structured provider does the same, keyed on the workflow RUN rather than on
    the display label: a label cannot prove two rows are one dispatch retried, but
    two different run ids can. Guidance saying the typed provider skips the collapse
    sends an agent chasing a row the provider already dropped, so the skill's
    sentence and the engine have to state one rule. This asserts both halves of it
    against the real normalization rather than against a spelling.
    """
    superseded = {
        "__typename": "CheckRun",
        "name": "test",
        "workflowName": "CI",
        "workflowDefinitionId": 7,
        "workflowRunEvent": "pull_request",
        "workflowRunId": 100,
        "workflowRunConclusion": "CANCELLED",
        "status": "COMPLETED",
        "conclusion": "CANCELLED",
        "workflowRunCreatedAt": "2026-08-21T00:00:00Z",
    }
    replacement = {
        **superseded,
        "workflowRunId": 200,
        "workflowRunConclusion": "SUCCESS",
        "conclusion": "SUCCESS",
        "workflowRunCreatedAt": "2026-08-22T00:00:00Z",
    }
    same_run_publisher = {
        **superseded,
        "conclusion": "SUCCESS",
        "workflowRunCreatedAt": "2026-08-21T00:00:02Z",
    }
    live_run_cancelled_row = {
        **superseded,
        "workflowRunConclusion": "FAILURE",
    }

    across_runs = _normalize_checks([superseded, replacement])
    within_one_run = _normalize_checks([superseded, same_run_publisher])
    live_run = _normalize_checks([live_run_cancelled_row, replacement])

    assert sorted(check.state for check in across_runs) == ["passed"], (
        "a newer run displaces the CANCELLED attempt it replaced, so that row must "
        "not survive to wake the session; a row that reached a verdict is kept"
    )
    assert sorted(check.state for check in within_one_run) == ["failed", "passed"], (
        "two rows of ONE run are concurrent, so collapsing them by start time "
        "would erase a live failure the job actually reported"
    )
    assert sorted(check.state for check in live_run) == ["failed", "passed"], (
        "the row's own cancellation is not displacement: its RUN concluded FAILURE, "
        "so the row is live and dropping it would report a failed run as ready"
    )
    body = BABYSIT_SKILL.read_text(encoding="utf-8")
    collapsed = " ".join(body.split())
    assert "keyed on the workflow DEFINITION's id plus the check name" in collapsed
    assert "Two rows of ONE run are not a retry and both stay" in collapsed
    assert "any completed row of a replaced round still reads as live" in collapsed
    assert "its own RUN concluded CANCELLED" in collapsed, (
        "displacement is the RUN's cancellation, so the skill must not tell an agent "
        "to read the row's own conclusion as proof a newer run replaced it"
    )


def test_the_spec_states_the_arming_gate_default_the_chokepoint_implements() -> None:
    """The ungated default is the one a new caller inherits by writing nothing.

    Only the two ``monitor_start`` surfaces ask for the gate. Every other caller
    reaches ``authorize_and_add_nudge`` without naming a value and inherits its
    parameter default, so the spec has to say which way that resolves: gating is
    the state that can silently stop work, so an unnamed value spends a turn per
    interval instead of arming a watch that deactivates itself.
    """
    default = inspect.signature(authorize_and_add_nudge).parameters["gate"].default

    assert default is False, (
        "the shared arming chokepoint defaults ungated; flipping it silently "
        "changes what every non-monitor_start caller arms"
    )
    spec = " ".join(SPEC.read_text(encoding="utf-8").split())
    assert "defaults every other caller UNGATED" in spec
    assert "gating is the state that can silently stop work" in spec.lower()


def test_the_skill_answers_the_irreproducible_verdict_it_warns_about() -> None:
    """A named hazard with no response is half a rule.

    The skill tells a reader that identical trees may receive different verdicts,
    which makes a finding's third raise ambiguous: either the rebuttal missed it,
    or the lane is not reproducible. Re-running that lane on the unchanged head is
    the only thing that separates those, so the hazard and its response have to
    ship together.
    """
    body = " ".join(BABYSIT_SKILL.read_text(encoding="utf-8").split())

    assert "identical trees may receive different verdicts" in body, (
        "the hazard this response exists for is gone; drop the response too, or "
        "restore the hazard"
    )
    assert "re-run that lane on the unchanged head" in body
