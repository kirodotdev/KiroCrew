"""A script cron's spawn calls resolve the job's captured memory mode.

The script's tool calls present ``cron:<job id>``, and ``/api/spawn`` answers 409
``memory_unavailable`` for a key with no admitted mode. The script arm admits the
key with the capture's mode for the run alone, in memory.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from cron_gateway_integration_helpers import _make_gw, _make_script_job, _run_script_callback

from kiro_crew.dashboard.messaging_api.spawn import _spawn_request_memory_mode
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["persistent", "incognito"])
async def test_a_script_run_admits_its_cron_key_with_the_captured_mode_for_the_run_only(mode):
    gw = _make_gw()
    gw.ctx_builder._session_memory_modes = {}
    state = SimpleNamespace(context_builder=gw.ctx_builder, subagents=None)
    capture = ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew")
    job = _make_script_job(execution_context=capture.with_mode(mode).to_record())
    key = f"cron:{job.id}"
    request = SimpleNamespace(headers={"X-Session-Key": key})
    during_run: list[str] = []

    def script_calls_spawn(*_args, **_kwargs):
        # The script runs on a cron pool thread; the spawn gate is the route's own.
        during_run.append(asyncio.run(_spawn_request_memory_mode(state, request, key)))
        return {"status": "ok"}

    await _run_script_callback(gw, job, side_effect=script_calls_spawn)

    assert during_run == [mode], f"the script's spawn saw {during_run}, not the job's {mode!r}"
    assert job.last_status == "ok"
    with pytest.raises(ValueError):
        await _spawn_request_memory_mode(state, request, key)


@pytest.mark.asyncio
async def test_a_script_run_leaves_an_admission_the_message_arm_made_for_the_same_key():
    # A job edited from a message into a script keeps the message arm's entry.
    gw = _make_gw()
    job = _make_script_job()
    key = f"cron:{job.id}"
    gw.ctx_builder._session_memory_modes = {key: "temporary"}
    seen: list[str] = []

    def script_runs(*_args, **_kwargs):
        seen.append(gw.ctx_builder._session_memory_modes[key])
        return {"status": "ok"}

    await _run_script_callback(gw, job, side_effect=script_runs)

    assert seen == ["temporary"], "the script run relaxed a stricter admission it did not make"
    assert gw.ctx_builder._session_memory_modes == {key: "temporary"}
