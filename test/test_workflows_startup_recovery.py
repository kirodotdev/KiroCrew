"""Startup must not block the loop or discard unrelated recovery records."""

import asyncio
import json
import threading
from unittest.mock import MagicMock

import pytest

from kiro_crew.taskrunner import TaskRunner
from kiro_crew.workflow_memory import task_snapshot_path
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


@pytest.mark.parametrize(
    "failure", ["missing-sidecar", "missing-binding", "bad-sidecar", "oserror"]
)
def test_private_failure_preserves_public_registry(tmp_path, monkeypatch, caplog, failure):
    from kiro_crew import workflow_memory

    path = tmp_path / "runs.json"
    reference = {"task_id": "private-task", "private_payload": True}
    public = {"task_id": "public-task", "spec_path": "", "status": "completed"}
    path.write_text(json.dumps([reference, public]), encoding="utf-8")
    hidden = task_snapshot_path(path)
    hidden.parent.mkdir(parents=True, exist_ok=True)
    original = [
        {
            "task_id": "private-task",
            "spec_path": "",
            "status": "completed",
            "spec_content": "PRIVATE_PAYLOAD",
        }
    ]
    if failure != "missing-sidecar":
        hidden.write_text(
            "{PRIVATE_PAYLOAD" if failure == "bad-sidecar" else json.dumps(original),
            encoding="utf-8",
        )
    if failure == "oserror":
        real_read = workflow_memory._private_task_rows

        def unavailable(candidate):
            if candidate == hidden:
                raise OSError("PRIVATE_PAYLOAD")
            return real_read(candidate)

        monkeypatch.setattr(workflow_memory, "_private_task_rows", unavailable)
    before = hidden.read_bytes() if hidden.exists() else None
    runner = TaskRunner(sessions=MagicMock(), auto_test=False, work_dir=tmp_path)
    assert set(runner._runs) == {"public-task"}
    assert path.exists() and not path.with_suffix(".json.corrupt").exists()
    assert reference in json.loads(path.read_text(encoding="utf-8"))
    runner._persist_runs()
    assert reference in json.loads(path.read_text(encoding="utf-8"))
    assert (hidden.read_bytes() if hidden.exists() else None) == before
    assert "PRIVATE_PAYLOAD" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records if "snapshot" in record.message)


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


@pytest.mark.parametrize("reference_marker", [True, False])
@pytest.mark.parametrize(
    "failure",
    ["missing-spec", "bad-status", "missing-title", "bad-revision", "bad-attempts"],
)
def test_private_structure_failure_isolated_and_retained(
    tmp_path, monkeypatch, caplog, failure, reference_marker
):
    """Real snapshot I/O with an authority-reader double, not a kernel E2E test."""
    from kiro_crew import member_memory_auth, workflow_memory

    monkeypatch.setattr(
        member_memory_auth,
        "read_private_session_store",
        lambda key: "member-store" if key == "taskrunner:private-task:runtime" else None,
    )
    reference = {"task_id": "private-task", "private_payload": True}
    # Hidden state and protected identity must still win if the public marker is removed.
    public_private_row = reference if reference_marker else {"task_id": "private-task"}
    public = {"task_id": "public-task", "spec_path": "", "status": "completed"}
    path = tmp_path / "runs.json"
    path.write_text(json.dumps([public_private_row, public]), encoding="utf-8")
    task = {"index": 1, "title": "PRIVATE_BODY", "status": "passed"}
    private = {
        "task_id": "private-task",
        "spec_path": "",
        "status": "completed",
        "spec_content": "PRIVATE_BODY",
        "task_details": [task],
    }
    if failure == "missing-spec":
        del private["spec_path"]
    elif failure == "bad-status":
        task["status"] = "PRIVATE_BAD_VALUE"
    elif failure == "missing-title":
        del task["title"]
    elif failure == "bad-revision":
        private["workflow_revision"] = "PRIVATE_BAD_VALUE"
    else:
        private["status"] = "running"
        task.update(status="in_progress", attempts="PRIVATE_BAD_VALUE")
    hidden = task_snapshot_path(path)
    hidden.parent.mkdir(parents=True, exist_ok=True)
    hidden.write_text(json.dumps([private], indent=2) + "\n", encoding="utf-8")
    before = hidden.read_bytes()
    # The authorization/hydration layer accepts this JSON; construction must isolate it.
    assert json.loads(workflow_memory.read_task_snapshot(path))[0] == private

    runner = TaskRunner(sessions=MagicMock(), auto_test=False, work_dir=tmp_path)
    assert set(runner._runs) == {"public-task"}
    assert runner._unavailable_run_refs == [reference]
    assert path.exists() and not path.with_suffix(".json.corrupt").exists()
    runner._runs["public-task"].name = "saved after recovery"
    runner._persist_runs()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved[0] == reference
    assert saved[1]["name"] == "saved after recovery"
    assert hidden.read_bytes() == before
    restarted = TaskRunner(sessions=MagicMock(), auto_test=False, work_dir=tmp_path)
    assert set(restarted._runs) == {"public-task"}
    assert restarted._unavailable_run_refs == [reference]
    assert "PRIVATE_BODY" not in caplog.text
    assert "PRIVATE_BAD_VALUE" not in caplog.text
    errors = [record for record in caplog.records if record.levelname == "ERROR"]
    assert errors, "A rejected record must leave a safe diagnostic"
    assert all(record.exc_info is None and record.exc_text is None for record in errors)
