"""Persistent task identity is read off-loop and carried through later awaits."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import make_mocked_request
from test_taskrunner_execution_context import make_runner, member_execution

from kiro_crew.dashboard.handlers import taskrunner as handlers
from kiro_crew.execution_context import bind_session_execution, read_session_execution
from kiro_crew.history import ConversationLog
from kiro_crew.task_models import Project, Task

ORIGIN = "capture-source"


@pytest.fixture
def metadata_reads(monkeypatch):
    """Observe the real persisted reader, including its actual result."""
    reads = []
    real_read = ConversationLog.get_metadata_status

    def read(log, key):
        result = real_read(log, key)
        if key == ORIGIN:
            reads.append((threading.get_ident(), result))
        return result

    monkeypatch.setattr(ConversationLog, "get_metadata_status", read)
    return reads


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["start", "plan", "from_chat", "refine"])
@pytest.mark.parametrize("live", ["persistent", "temporary"])
async def test_http_capture_reads_off_loop_and_survives_body_await(
    tmp_path, monkeypatch, metadata_reads, entry, live
):
    alpha = member_execution()
    await asyncio.to_thread(bind_session_execution, ORIGIN, alpha)
    runner = make_runner(tmp_path)
    live_modes = {ORIGIN: live}
    runner._ctx = SimpleNamespace(
        _session_memory_modes={}, live_memory_mode_for_session=live_modes.get
    )
    state = SimpleNamespace(task_runner=runner, _refine_task=None, _background_tasks=set())
    request = make_mocked_request(
        "POST", "/", headers={"X-Session-Key": ORIGIN}, app={"state": state}
    )
    request["internal_auth"] = True
    captured = []
    real_capture = runner._capture_execution

    def capture(key):
        execution = real_capture(key)
        captured.append(execution)
        live_modes.clear()
        return execution

    monkeypatch.setattr(runner, "_capture_execution", capture)
    spec = tmp_path / "spec.md"
    spec.write_text("synthetic task", encoding="utf-8")

    async def body(*args, **kwargs):
        await asyncio.to_thread(
            bind_session_execution, ORIGIN, member_execution("beta"), replace_existing=True
        )
        return {"input": "task", "spec": str(spec), "steps": [{"title": "step"}]}, None

    monkeypatch.setattr(handlers, "read_bounded_json", body)
    monkeypatch.setattr(handlers, "_gate_auto_approve", AsyncMock(return_value=False))
    run = Project("", "", task_id="synthetic", tasks=[])
    runner.plan = AsyncMock(return_value=run)
    runner.start_background = AsyncMock(return_value=run.task_id)

    async def update(task_id, steps):
        return runner._runs[task_id]

    runner.update_plan = AsyncMock(side_effect=update)
    refine = AsyncMock()
    monkeypatch.setattr(handlers, "_run_refine", refine)
    metadata_reads.clear()
    loop_thread = threading.get_ident()
    response = await getattr(handlers, f"api_taskrunner_{entry}")(request)
    if state._refine_task:
        await asyncio.wait_for(state._refine_task, timeout=5)
    assert response.status == 200
    assert len(captured) == 1
    assert captured[0] == alpha
    if entry == "start":
        forwarded = runner.start_background.await_args.kwargs["execution_context"]
    elif entry == "plan":
        forwarded = runner.plan.await_args.kwargs["execution_context"]
    elif entry == "from_chat":
        forwarded = next(iter(runner._runs.values())).execution_context
    else:
        forwarded = refine.await_args.args[2]
    assert forwarded == alpha.with_mode(live)
    if live == "persistent":
        assert forwarded is captured[0]
    assert metadata_reads
    assert all(thread != loop_thread for thread, _ in metadata_reads)
    assert metadata_reads[0][1][0]["execution_context"] == alpha.to_record()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["plan", "start_background"])
@pytest.mark.parametrize("supplied", [False, True])
@pytest.mark.parametrize("live", ["persistent", "temporary"])
async def test_runner_capture_off_loop_or_supplied_unchanged(
    tmp_path, monkeypatch, metadata_reads, entry, supplied, live
):
    alpha = member_execution()
    await asyncio.to_thread(bind_session_execution, ORIGIN, alpha)
    runner = make_runner(tmp_path)
    live_modes = {ORIGIN: live}
    runner._ctx = SimpleNamespace(
        _session_memory_modes={}, live_memory_mode_for_session=live_modes.get
    )
    runner._decompose = AsyncMock(return_value=[Task(1, "step", "synthetic step")])
    runner.run = AsyncMock()
    captured = []
    real_capture = runner._capture_execution

    def capture(key):
        assert not supplied, "a supplied carrier must bypass session lookup"
        execution = real_capture(key)
        captured.append(execution)
        live_modes.clear()
        return execution

    monkeypatch.setattr(runner, "_capture_execution", capture)
    real_bind = runner._bind_run_execution
    capture_reads = []

    async def bind(run, key):
        capture_reads.extend(metadata_reads)
        await asyncio.to_thread(
            bind_session_execution, ORIGIN, member_execution("beta"), replace_existing=True
        )
        await real_bind(run, key)

    monkeypatch.setattr(runner, "_bind_run_execution", bind)
    kwargs = {"session_key": ORIGIN}
    if supplied:
        kwargs["execution_context"] = alpha
    metadata_reads.clear()
    loop_thread = threading.get_ident()
    if entry == "plan":
        run = await runner.plan("synthetic task", **kwargs)
    else:
        task_id = await runner.start_background(
            tmp_path / "spec.md", input_content="synthetic task", **kwargs
        )
        await asyncio.wait_for(asyncio.gather(*runner._tasks.values()), timeout=5)
        run = runner._runs[task_id]
    if live == "persistent":
        assert run.execution_context is (alpha if supplied else captured[0])
    expected = alpha.with_mode(live)
    assert run.execution_context == expected
    assert (
        await asyncio.to_thread(read_session_execution, f"taskrunner:{run.task_id}:runtime")
        == expected
    )
    if supplied:
        assert not capture_reads
    else:
        assert len(captured) == 1
        assert capture_reads
        assert all(thread != loop_thread for thread, _ in capture_reads)
        assert capture_reads[0][1][0]["execution_context"] == alpha.to_record()
