"""Restricted workflow bodies remain live-only through every checkpoint seam."""

import pytest

from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
from kiro_crew.workflows import WorkflowEvent
from kiro_crew.workflows.registry import RunHandle, RunRegistry, start_background_run
from kiro_crew.workflows.store import WorkflowRunStore


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_restricted_run_never_writes_registration_events_or_result(tmp_path, mode):
    store = WorkflowRunStore(tmp_path / "workflow-records")
    registry = RunRegistry(store=store)
    execution = ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew", mode)

    async def run(record):
        for seq in range(5):
            record(
                WorkflowEvent(
                    run_id="wf_private",
                    seq=seq,
                    ts="now",
                    type="log",
                    data={"message": "secret event"},
                )
            )
        return "secret result", "finished", None, {0: "secret call"}

    await start_background_run(
        registry,
        run,
        run_id="wf_private",
        name="private",
        source="secret source",
        args={"secret": "argument"},
        execution_context=execution,
        memory_mode=mode,
    )
    handle = registry.get("wf_private")
    await handle.task
    assert handle.result == "secret result"
    assert handle.events[0].data["message"] == "secret event"
    assert not store.runs_dir.exists()
    assert registry.status("wf_private").get("error_code") is None


@pytest.mark.parametrize("mode", ["incognito", "temporary"])
def test_storage_boundary_refuses_restricted_snapshot_even_without_registry(tmp_path, mode):
    store = WorkflowRunStore(tmp_path / "workflow-records")
    execution = ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew", mode)
    # A mistakenly permissive scalar cannot relax the captured execution.
    handle = RunHandle("wf_private", "private", source="secret source", execution_context=execution)
    store.save(handle.run_id, handle.to_store_json())
    assert not store.runs_dir.exists()
