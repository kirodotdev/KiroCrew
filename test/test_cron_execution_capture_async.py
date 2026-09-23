"""Cron fallback reads run off-loop without reinterpreting queued selectors."""

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import MemoryStoreConfig
from kiro_crew.cron import CronJob
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef, read_session_execution


class ReachedProvider(BaseException):
    """Stop after real capture and session binding, before any provider work."""


async def _callback(monkeypatch):
    from kiro_crew.slack import gateway

    gw = gateway.GatewayOrchestrator.__new__(gateway.GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.sessions.get_or_create = AsyncMock(side_effect=ReachedProvider)
    gw.ctx_builder = MagicMock()
    gw.slack = None
    gw.conv_log = None
    gw.dashboard_state = None
    gw._owner_id = "synthetic-owner"
    gw.subagent_mgr = None
    gw._cron_injecting = {}
    gw._no_crons = False
    callbacks = []

    async def create(on_job=None, **kwargs):
        callbacks.append(on_job)
        service = MagicMock()
        service.start = AsyncMock()
        return service

    monkeypatch.setattr(gateway.CronService, "create", create)
    monkeypatch.setattr(
        gateway, "_await_cron_fire_time_gate", AsyncMock(return_value=(None, False))
    )
    await gw._init_cron()
    return gw, callbacks[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("store", ["", "legacy"])
@pytest.mark.parametrize("sequence", [False, True])
async def test_legacy_capture_reads_off_loop_and_freezes_selectors(monkeypatch, store, sequence):
    cfg = KiroCrewConfig.load()
    cfg.memory_stores["legacy"] = MemoryStoreConfig()
    cfg.save()
    gw, callback = await _callback(monkeypatch)
    job = CronJob(
        id="capture",
        name="synthetic",
        message="original task",
        memory_store=store,
        agent_id="original-template",
        agent_sequence=["first", "second"] if sequence else [],
    )
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    original_load = KiroCrewConfig.load
    readers = []

    def read_config(cls):
        readers.append(threading.get_ident())
        assert threading.get_ident() != loop_thread, "cron config read blocked the gateway loop"
        if len(readers) == 1:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "test did not release config reader"
        return original_load()

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(read_config))
    task = asyncio.create_task(callback(job))
    ready = asyncio.create_task(entered.wait())
    try:
        done, _ = await asyncio.wait({task, ready}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
        assert ready in done
        job.memory_store = "missing-replacement"
        job.member_id = "replacement-member"
        job.agent_id = "replacement-template"
        job.agent_sequence[:] = ["replacement-first", "replacement-second"]
        release.set()
        with pytest.raises(ReachedProvider):
            await task
    finally:
        release.set()
        ready.cancel()
        await asyncio.gather(task, ready, return_exceptions=True)
    assert readers and all(thread != loop_thread for thread in readers)
    call = gw.sessions.get_or_create.call_args
    assert call.kwargs["agent"] == ("first" if sequence else "original-template")
    execution = await asyncio.to_thread(read_session_execution, call.args[0], required=True)
    assert execution.store.legacy_name == store
    assert execution.template_id == "original-template"
    assert execution.member_id is None
    assert execution.memory_mode == "persistent"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["persistent", "incognito", "temporary"])
async def test_supplied_cron_carrier_does_not_reload_configuration(monkeypatch, mode):
    from kiro_crew import execution_context, vector_memory
    from kiro_crew.slack import gateway

    monkeypatch.setattr(execution_context, "_LIVE_EXECUTIONS", {})
    gw, callback = await _callback(monkeypatch)
    from kiro_crew.memory_stores import memory_stores_root

    vector_memory.create_member_database(
        memory_stores_root() / "member-store" / "memory.db",
        member_id="original-member",
        store_id="member-store",
    )
    captured = ExecutionContext(
        "original-member",
        MemoryStoreRef("member-store", "original-member"),
        "member",
        "original",
        mode,
        "app",
    )
    job = CronJob(
        id="supplied", name="synthetic", message="task", execution_context=captured.to_record()
    )

    async def later_gate(*args, **kwargs):
        job.execution_context["member_id"] = "replacement"
        job.execution_context["app"] = "replacement-app"
        job.execution_context["memory_mode"] = "persistent"
        job.agent_id = "replacement-template"
        return None, False

    monkeypatch.setattr(gateway, "_await_cron_fire_time_gate", later_gate)
    monkeypatch.setattr(
        KiroCrewConfig, "load", MagicMock(side_effect=AssertionError("carrier reread config"))
    )
    with pytest.raises(ReachedProvider):
        await callback(job)
    call = gw.sessions.get_or_create.call_args
    assert call.kwargs["agent"] == "original"
    assert await asyncio.to_thread(read_session_execution, call.args[0], required=True) == captured


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["member", "v2-store", "missing-store"])
async def test_uncaptured_member_or_invalid_store_refuses_without_global(monkeypatch, kind):
    cfg = KiroCrewConfig.load()
    cfg.memory_stores["v2"] = MemoryStoreConfig(memory_version=2, owner_member_id="member")
    cfg.save()
    gw, callback = await _callback(monkeypatch)
    job = CronJob(
        id="invalid",
        name="synthetic",
        message="task",
        member_id="member" if kind == "member" else "",
        memory_store="v2" if kind == "v2-store" else "missing" if kind == "missing-store" else "",
    )
    with pytest.raises(ValueError):
        await callback(job)
    gw.sessions.get_or_create.assert_not_called()


@pytest.mark.asyncio
async def test_uncaptured_legacy_v1_member_record_keeps_its_configured_store(monkeypatch):
    cfg = KiroCrewConfig.load()
    cfg.memory_stores["legacy-member"] = MemoryStoreConfig(memory_version=1)
    cfg.save()
    gw, callback = await _callback(monkeypatch)
    job = CronJob(
        id="legacy-member",
        name="synthetic",
        message="task",
        member_id="legacy-member",
        memory_store="legacy-member",
        agent_id="legacy-template",
    )
    with pytest.raises(ReachedProvider):
        await callback(job)
    call = gw.sessions.get_or_create.call_args
    assert call.kwargs["agent"] == "legacy-template"
    execution = await asyncio.to_thread(read_session_execution, call.args[0], required=True)
    assert execution.store.legacy_name == "legacy-member"
    assert execution.member_id is None


@pytest.mark.asyncio
async def test_usage_fallback_keeps_captured_template_after_job_edit(monkeypatch):
    from kiro_crew.slack import gateway

    gw, callback = await _callback(monkeypatch)
    gw.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    gw.sessions.reset = AsyncMock()
    gw.ctx_builder.build_message.return_value = ("synthetic prompt", None)
    gw._interactive_approval = MagicMock()
    job = CronJob(id="usage", name="synthetic", message="task", agent_id="original-template")

    async def stream(*args, **kwargs):
        job.agent_id = "replacement-template"
        return "done"

    persist = AsyncMock(side_effect=ReachedProvider)
    monkeypatch.setattr(gateway, "stream_and_collect", stream)
    monkeypatch.setattr(gateway, "read_effective_agent", lambda client: "")
    monkeypatch.setattr(gateway, "persist_token_record_async", persist)
    with pytest.raises(ReachedProvider):
        await callback(job)
    assert persist.await_args.kwargs["agent"] == "original-template"
