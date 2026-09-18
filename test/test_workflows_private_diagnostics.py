"""Private task diagnostics exclude payloads without changing public logging."""

import asyncio
import logging

import pytest
from test_workflows_private_execution import world as _world

from kiro_crew.workflow_memory import private_task_operation

world = _world


@pytest.mark.asyncio
async def test_private_exception_and_child_logs_are_identifier_only(world, caplog):
    logger = logging.getLogger("kiro_crew.task_executor")
    caplog.set_level(logging.INFO)

    class Task:
        @private_task_operation
        async def plan(self, *, session_key):
            async def child():
                logger.info("PRIVATE_TASK_TITLE %s", "PRIVATE_ARGUMENT")
                try:
                    raise RuntimeError("PRIVATE_ERROR_BODY")
                except RuntimeError:
                    logger.exception("PRIVATE_TRACEBACK_MESSAGE")

            return asyncio.create_task(child())

    child = await Task().plan(session_key="dashboard:alice")
    await child
    assert "Private task" in caplog.text
    assert "RuntimeError" in caplog.text
    for text in (
        "PRIVATE_TASK_TITLE",
        "PRIVATE_ARGUMENT",
        "PRIVATE_ERROR_BODY",
        "PRIVATE_TRACEBACK_MESSAGE",
    ):
        assert text not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    caplog.clear()
    child = await Task().plan(session_key="dashboard:global")
    await child
    assert "PRIVATE_TASK_TITLE" in caplog.text
    assert "PRIVATE_ERROR_BODY" in caplog.text


@pytest.mark.asyncio
async def test_private_error_is_preserved_only_in_hidden_snapshot(world, tmp_path):
    import json

    from kiro_crew.workflow_memory import WorkflowScope, task_snapshot_path, write_task_snapshot

    scope = await WorkflowScope.admit("wf_log_payload", world.builder, "dashboard:alice")
    await scope.prepare(world.builder, "taskrunner:log-payload:runtime")
    public = tmp_path / "runs.json"
    write_task_snapshot(
        public, json.dumps([{"task_id": "log-payload", "error": "PRIVATE_FAILURE_DETAIL"}])
    )
    log_path = tmp_path / "gateway.log"
    handler = logging.FileHandler(log_path, encoding="utf-8")
    logger = logging.getLogger("kiro_crew.taskrunner")
    logger.addHandler(handler)
    try:

        class Task:
            @private_task_operation
            async def execute_plan(self, task_id):
                logger.error("PRIVATE_FAILURE_DETAIL")

        await Task().execute_plan("log-payload")
        handler.flush()
        assert "PRIVATE_FAILURE_DETAIL" not in log_path.read_text(encoding="utf-8")
        assert "Private task" in log_path.read_text(encoding="utf-8")
        assert "PRIVATE_FAILURE_DETAIL" not in public.read_text(encoding="utf-8")
        assert "PRIVATE_FAILURE_DETAIL" in task_snapshot_path(public).read_text(encoding="utf-8")
        await Task().execute_plan("global-task")
        handler.flush()
        assert "PRIVATE_FAILURE_DETAIL" in log_path.read_text(encoding="utf-8")
    finally:
        logger.removeHandler(handler)
        handler.close()
