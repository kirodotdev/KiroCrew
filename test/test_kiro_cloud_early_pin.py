"""A Kiro Cloud start with an early model pin never leaves its sandbox unaccounted for.

On a KAS start the configured model is sent while the MCP start wait runs
(``providers/kas_start_pin.py``), and the start then waits for that write. The new
cloud session is recorded before that wait, so a Stop landing in it cannot leave the
sandbox both unrecorded and undeleted. When the record itself fails, the pin's task
is stopped and settled around the shielded delete, never before it, so a second
cancel cannot skip the delete. Runs against the KAS stub of ``test_kiro_cloud_session``.
"""

from __future__ import annotations

import sys

import pytest
from test_kiro_cloud_session import (  # noqa: F401 -- fixtures, used by name
    _choice_rows,
    _developer_flag_on,
    _fast_no_report_ceiling,
    _governed,
    _requests,
    crew_agent,
    handlers,
    section,
    stub,
)

from kiro_crew.acp.harness.kas_cloud import KasCloudSandboxHarness
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.types import ACP_BACKEND_KAS


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
async def test_a_cancel_while_waiting_for_the_pin_keeps_the_cloud_session_recorded(
    handlers, stub, crew_agent, tmp_path, monkeypatch  # noqa: F811
):
    import asyncio

    from kiro_crew.coder import session_launch as sl
    from kiro_crew.providers import acp, acp_runtime_start
    from kiro_crew.session_environment import forget_remote_workspace
    from kiro_crew.testing.wait import await_until, finished

    key = "dashboard:cloud-pin-cancel"
    choices = handlers._choices()
    choices.set(key, "cloud", sl.KIRO_CLOUD_PROVIDER_ID)
    _governed(monkeypatch, None)
    pin_waiting, release_pin = asyncio.Event(), asyncio.Event()

    async def wait_for_pin(_tasks):
        pin_waiting.set()
        await await_until(release_pin, what="the test releasing the early pin")
        return False, None

    monkeypatch.setattr(acp_runtime_start, "finish_early_pin", wait_for_pin)
    provider = acp.AcpProvider(
        agent="kirocrew",
        model="pinned-model",
        session_key=key,
        acp_backend=ACP_BACKEND_KAS,
        sandbox_mode="off",
    )
    runtimes = []
    original = AcpRuntime.create_session

    async def capture_runtime(runtime, *args, **kwargs):
        handle = await finished(original(runtime, *args, **kwargs), "the cloud session creation")
        runtimes.append(runtime)
        return handle

    monkeypatch.setattr(AcpRuntime, "create_session", capture_runtime)
    start = asyncio.create_task(provider._start_kiro_runtime_impl({}, {}))
    try:
        await await_until(pin_waiting, what="the start waiting for its early pin")
        start.cancel()
        with pytest.raises(asyncio.CancelledError):
            await finished(start, "the cancelled cloud start")
        assert _choice_rows(tmp_path)[key].get("cloud_session") == "cloud-session"
        assert _requests(stub, KasCloudSandboxHarness().teardown.method) == []
    finally:
        release_pin.set()
        if not start.done():
            start.cancel()
            await finished(
                asyncio.gather(start, return_exceptions=True), "the cancelled start cleanup"
            )
        for runtime in runtimes:
            await finished(runtime.kill(), "the cloud runtime cleanup")
        await finished(provider.shutdown(), "the provider cleanup")
        forget_remote_workspace(key)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
async def test_a_cancel_while_settling_the_pin_still_deletes_an_unrecorded_sandbox(
    handlers, stub, crew_agent, tmp_path, monkeypatch  # noqa: F811
):
    import asyncio

    from kiro_crew.coder import session_launch as sl
    from kiro_crew.providers import acp, acp_runtime_start
    from kiro_crew.session_environment import forget_remote_workspace
    from kiro_crew.testing.wait import await_until, finished

    key = "dashboard:cloud-pin-cleanup-cancel"
    handlers._choices().set(key, "cloud", sl.KIRO_CLOUD_PROVIDER_ID)
    _governed(monkeypatch, None)

    async def unrecordable(*_args, **_kwargs):
        raise RuntimeError("the choice row could not be written")

    monkeypatch.setattr(sl, "record_cloud_session", unrecordable)
    settle_release = asyncio.Event()
    pin_tasks = []
    start = None

    async def cancel_while_settling(tasks):
        assert tasks
        pin_tasks.extend(tasks)
        for task in tasks:
            task.cancel()
        current = asyncio.current_task()
        assert current is start
        asyncio.get_running_loop().call_soon(current.cancel)
        await await_until(settle_release, what="the test releasing the pin settle")

    monkeypatch.setattr(acp_runtime_start, "drop_early_pin", cancel_while_settling)
    provider = acp.AcpProvider(
        agent="kirocrew",
        model="pinned-model",
        session_key=key,
        acp_backend=ACP_BACKEND_KAS,
        sandbox_mode="off",
    )
    runtimes = []
    original = AcpRuntime.create_session

    async def capture_runtime(runtime, *args, **kwargs):
        handle = await finished(original(runtime, *args, **kwargs), "the cloud session creation")
        runtimes.append(runtime)
        return handle

    monkeypatch.setattr(AcpRuntime, "create_session", capture_runtime)
    start = asyncio.create_task(provider._start_kiro_runtime_impl({}, {}))
    try:
        with pytest.raises(asyncio.CancelledError):
            await finished(start, "the cloud start cancelled while settling its pin")
        teardown = KasCloudSandboxHarness().teardown.method
        assert [row.get("sessionId") for row in _requests(stub, teardown)] == ["cloud-session"]
    finally:
        settle_release.set()
        if not start.done():
            start.cancel()
            await finished(
                asyncio.gather(start, return_exceptions=True), "the cancelled start cleanup"
            )
        if pin_tasks:
            await finished(
                asyncio.gather(*pin_tasks, return_exceptions=True), "the early pin cleanup"
            )
        for runtime in runtimes:
            await finished(runtime.kill(), "the cloud runtime cleanup")
        await finished(provider.shutdown(), "the provider cleanup")
        forget_remote_workspace(key)
