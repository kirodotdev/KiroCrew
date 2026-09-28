"""Startup must not block the loop or discard unrelated recovery records."""

import asyncio
import json
import threading
from unittest.mock import MagicMock

import pytest

from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
from kiro_crew.taskrunner import TaskRunner
from kiro_crew.workflows.service import WorkflowService
from kiro_crew.workflows.store import WorkflowRunStore


@pytest.mark.asyncio
async def test_service_restore_yields_until_complete(tmp_path, monkeypatch):
    store = WorkflowRunStore(base_dir=tmp_path / "workflows")
    store.runs_dir.mkdir(parents=True)
    for index in (1, 2):
        (store.runs_dir / f"wf_{index:06d}.json").write_text(
            json.dumps({"run_id": f"wf_{index:06d}", "name": "saved", "status": "finished"}),
            encoding="utf-8",
        )
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    observed = []
    real_load = store.load_all

    def slow_load():
        loop.call_soon_threadsafe(entered.set)
        observed.append(release.wait(2))
        return real_load()

    monkeypatch.setattr(store, "load_all", slow_load)

    async def create():
        if hasattr(WorkflowService, "create"):
            return await WorkflowService.create(sessions=MagicMock(), store=store)
        return WorkflowService(sessions=MagicMock(), store=store)

    startup = asyncio.create_task(create())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert not startup.done(), "Service was published before the loop could run during restore"
    finally:
        release.set()
        service = await asyncio.wait_for(startup, 3)
    assert observed == [True], "Disk recovery blocked the event loop"
    assert {row["run_id"] for row in service.list_runs()} == {"wf_000001", "wf_000002"}
    assert await service._new_run_id() == "wf_000003"


@pytest.mark.parametrize("failure", ["missing-identity", "bad-context", "oserror"])
def test_snapshot_read_failure_preserves_original_registry(tmp_path, monkeypatch, caplog, failure):
    from kiro_crew import workflow_memory

    path = tmp_path / "runs.json"
    execution = ExecutionContext(
        "alice", MemoryStoreRef("alice-store", "alice"), "member", "kirocrew"
    )
    member = {
        "task_id": "member-task",
        "spec_path": "",
        "status": "completed",
        "spec_content": "MEMBER_PAYLOAD",
        "execution_context": execution.to_record(),
    }
    if failure == "missing-identity":
        del member["execution_context"]["member_id"]
    elif failure == "bad-context":
        member["execution_context"] = "invalid"
    public = {"task_id": "public-task", "spec_path": "", "status": "completed"}
    path.write_text(json.dumps([member, public]), encoding="utf-8")
    before = path.read_bytes()
    if failure == "oserror":

        def unavailable(candidate):
            raise OSError("registry unavailable")

        monkeypatch.setattr(workflow_memory, "read_task_registry", unavailable)
    runner = TaskRunner(sessions=MagicMock(), auto_test=False, work_dir=tmp_path)
    assert runner._snapshot_recovery_incomplete
    runner._persist_runs()
    assert path.read_bytes() == before
    assert not path.with_suffix(".json.corrupt").exists()
    assert "MEMBER_PAYLOAD" not in caplog.text


def test_bad_public_json_still_quarantined(tmp_path):
    path = tmp_path / "runs.json"
    path.write_text("[{broken", encoding="utf-8")
    runner = TaskRunner(sessions=MagicMock(), auto_test=False, work_dir=tmp_path)
    assert runner._runs == {}
    assert not path.exists()
    assert path.with_suffix(".json.corrupt").read_text(encoding="utf-8") == "[{broken"


def test_synchronous_service_restores_before_return(tmp_path):
    store = WorkflowRunStore(base_dir=tmp_path / "workflows")
    store.runs_dir.mkdir(parents=True)
    (store.runs_dir / "wf_000009.json").write_text(
        json.dumps({"run_id": "wf_000009", "name": "saved", "status": "finished"}),
        encoding="utf-8",
    )
    service = WorkflowService(sessions=MagicMock(), store=store)
    assert service.registry.get("wf_000009") is not None
    assert service._seq == 9


@pytest.mark.asyncio
async def test_async_restore_keeps_handles_on_loop_and_eviction_off_loop(tmp_path, monkeypatch):
    from kiro_crew.workflows.registry import RunHandle, RunRegistry

    store = WorkflowRunStore(base_dir=tmp_path / "workflows")
    registry = RunRegistry(max_runs=1, store=store)
    owner = threading.get_ident()
    calls = []
    real_restore = RunHandle.from_store_json

    def load():
        calls.append(("load", threading.get_ident() != owner))
        return [
            {"run_id": f"wf_{index:06d}", "name": "saved", "status": "finished"} for index in (1, 2)
        ]

    def restore(row):
        calls.append(("hydrate", threading.get_ident() == owner))
        return real_restore(row)

    def delete(run_id):
        calls.append(("delete", threading.get_ident() != owner))
        assert run_id == "wf_000001"

    monkeypatch.setattr(store, "load_all", load)
    monkeypatch.setattr(store, "delete", delete)
    monkeypatch.setattr(RunHandle, "from_store_json", restore)
    assert await registry.load_persisted_async() == 2
    assert [row["run_id"] for row in registry.list()] == ["wf_000002"]
    assert calls == [("load", True), ("hydrate", True), ("hydrate", True), ("delete", True)]


@pytest.mark.parametrize(
    "failure", ["missing-spec", "bad-status", "missing-title", "bad-revision", "bad-attempts"]
)
def test_bad_project_isolated_in_memory_and_original_snapshot_retained(tmp_path, caplog, failure):
    task = {"index": 1, "title": "MEMBER_BODY", "status": "passed"}
    member = {
        "task_id": "member-task",
        "spec_path": "",
        "status": "completed",
        "spec_content": "MEMBER_BODY",
        "task_details": [task],
        "execution_context": ExecutionContext(
            "alice", MemoryStoreRef("alice-store", "alice"), "member", "kirocrew"
        ).to_record(),
    }
    if failure == "missing-spec":
        del member["spec_path"]
    elif failure == "bad-status":
        task["status"] = "BAD_VALUE"
    elif failure == "missing-title":
        del task["title"]
    elif failure == "bad-revision":
        member["workflow_revision"] = "BAD_VALUE"
    else:
        member["status"] = "running"
        task.update(status="in_progress", attempts="BAD_VALUE")
    public = {"task_id": "public-task", "spec_path": "", "status": "completed"}
    path = tmp_path / "runs.json"
    path.write_text(json.dumps([member, public]), encoding="utf-8")
    before = path.read_bytes()
    runner = TaskRunner(sessions=MagicMock(), auto_test=False, work_dir=tmp_path)
    assert set(runner._runs) == {"public-task"}
    assert runner._snapshot_recovery_incomplete
    runner._runs["public-task"].name = "pending change"
    runner._persist_runs()
    assert path.read_bytes() == before
    assert "MEMBER_BODY" not in caplog.text
    assert "BAD_VALUE" not in caplog.text
    errors = [record for record in caplog.records if record.levelname == "ERROR"]
    assert errors
    assert all(record.exc_info is None and record.exc_text is None for record in errors)
