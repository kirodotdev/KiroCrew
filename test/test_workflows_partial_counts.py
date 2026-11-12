"""A failed or cancelled run's preserved results cover only the calls that did not fail.

A failed call keeps its slot in ``agent_results`` (the resume cache), but the
snapshot must not report it as a preserved result: the completion message would
then promise results that are null. A finished run's per-call record is a
separate contract, pinned by ``test_workflows_lifecycle_evidence.py``.
"""

from __future__ import annotations

from kiro_crew.dashboard.workflow_inject import _summarize
from kiro_crew.workflows.registry import RunHandle


def _handle(status: str, outcomes: list[str | None]) -> RunHandle:
    """A run whose calls settled with *outcomes*; ``None`` marks a failed call."""
    h = RunHandle(run_id="wf_1", name="demo", session_key="dashboard:x")
    for index, outcome in enumerate(outcomes):
        # What RunRegistry.record_agent_result stores for each kind of call.
        h.agent_results[index] = outcome
        if outcome is None:
            h.agent_errors[index] = "agent returned no result"
    h.status = status
    if status == "failed":
        h.error = "workflow timed out"
    return h


def test_a_run_whose_every_call_failed_reports_no_preserved_result() -> None:
    h = _handle("failed", [None, None, None])
    compact = h.snapshot(include_events=False)
    assert "partial_result_count" not in compact
    assert compact["agent_error_count"] == 3
    message = _summarize(compact)
    assert "preserved" not in message
    assert "3 agent call(s) failed" in message
    assert "partial_results" not in h.snapshot(include_events=True)


def test_only_the_calls_that_did_not_fail_are_preserved() -> None:
    h = _handle("failed", ["a", None, "c"])
    compact = h.snapshot(include_events=False)
    assert compact["partial_result_count"] == 2
    assert compact["agent_error_count"] == 1
    assert h.snapshot(include_events=True)["partial_results"] == {"0": "a", "2": "c"}
    message = _summarize(compact)
    assert "2 agent result(s) finished before the run ended and were preserved" in message
    assert "1 agent call(s) failed" in message


def test_the_resume_cache_keeps_every_call() -> None:
    h = _handle("failed", ["a", None])
    h.snapshot(include_events=True)
    assert h.agent_results == {0: "a", 1: None}
