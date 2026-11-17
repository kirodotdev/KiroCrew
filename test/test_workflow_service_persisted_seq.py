"""The run-id sequence recovered from stored runs survives an odd stored id.

``WorkflowService`` reads the highest ``wf_NNNNNN`` among the runs it reloads, so
new ids start past it. A stored id it cannot count must be skipped, not raised on:
a raise at that point stops the workflow service from starting, and a floor past
the allocator's range makes every new run fail.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.workflow_memory import _RUN_ID_MAX
from kiro_crew.workflows.registry import RunHandle, RunRegistry
from kiro_crew.workflows.service import WorkflowService


def _seq_over(*run_ids: str) -> int:
    registry = RunRegistry()
    for run_id in ("wf_000003", *run_ids):
        registry._runs[run_id] = RunHandle.from_store_json({"run_id": run_id, "status": "done"})
    return WorkflowService._max_persisted_seq(SimpleNamespace(registry=registry))


def test_ascii_digit_ids_still_set_the_floor():
    assert _seq_over("wf_000007") == 7


@pytest.mark.parametrize(
    "odd_id",
    [
        "wf_²",  # isdigit() is True, int() raises
        "wf_\uff11\uff12",  # fullwidth digits: int() reads 12
        f"wf_{_RUN_ID_MAX + 1}",  # past the range the allocator accepts
        "wf_" + "9" * 5000,  # longer than int() will parse
        "wf_" + "0" * 30 + "7",  # leading zeros past the range's width
        "wf_",
        "run-7",
    ],
)
def test_an_id_the_sequence_cannot_count_is_skipped(odd_id):
    assert _seq_over(odd_id) == 3


def test_the_largest_id_the_allocator_accepts_still_counts():
    assert _seq_over(f"wf_{_RUN_ID_MAX}") == _RUN_ID_MAX
