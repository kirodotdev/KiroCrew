"""An Issue Radar crew arm queued behind a closing tab's final retirement is refused.

The close-versus-arm race on the dashboard slot-delete handler is pinned for the
core's own loops in ``test/test_slot_close_nudge_race.py``, whose harness this
reuses. This is the same race driven through the app's ``crew_runtime.launch_crew``:
its arm lands while the close's empty loop table is being written, and must be
refused rather than outlive the tab it was armed for.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock

import pytest
from test_slot_close_nudge_race import NAME, _Req, _service, _state_with_slot

from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime
from kiro_crew.autonudge import NudgeAdmissionRefused
from kiro_crew.dashboard import chat_handlers as handlers


@pytest.mark.asyncio
async def test_issue_radar_arm_queued_behind_final_retirement_is_refused(
    tmp_path, monkeypatch
) -> None:
    state = _state_with_slot(tmp_path)
    slot = state._slots[NAME]
    slot._app = "issue-radar"
    svc = await _service(tmp_path, monkeypatch)
    hook_entered = asyncio.Event()
    release_hook = asyncio.Event()

    async def _hook(_app: str, _slot_key: str) -> bool:
        hook_entered.set()
        await release_hook.wait()
        return True

    monkeypatch.setattr("kiro_crew.apps.teardown.notify_slot_closed", _hook)
    monkeypatch.setattr(crew_runtime, "ensure_crew_session", AsyncMock(return_value=slot))
    monkeypatch.setattr(
        crew_runtime,
        "compose_turn_prompt_async",
        AsyncMock(return_value="replacement arm"),
    )
    write_entered = threading.Event()
    release_write = threading.Event()
    original_write = svc._write_state

    def _stall_empty_write(payload: dict[str, object]) -> None:
        if not payload["loops"]:
            write_entered.set()
            assert release_write.wait(timeout=5)
        original_write(payload)

    monkeypatch.setattr(svc, "_write_state", _stall_empty_write)
    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await hook_entered.wait()
    await svc.add(NAME, "mid-hook arm", idle_secs=300)
    release_hook.set()
    assert await asyncio.to_thread(write_entered.wait, 5)
    launch = asyncio.create_task(
        crew_runtime.launch_crew(
            state,
            "owner",
            "repo",
            {"id": "crew-1"},
            tmp_path,
        )
    )
    try:
        for _ in range(10):
            await asyncio.sleep(0)
            if svc._inflight_adds:
                break
        assert svc._inflight_adds
        assert not launch.done()
        release_write.set()
        response = await close
        launch_result = await asyncio.gather(launch, return_exceptions=True)

        assert response.status == 200
        assert NAME not in state._slots
        assert isinstance(launch_result[0], NudgeAdmissionRefused)
        assert svc.get_by_slot(NAME) is None
    finally:
        release_hook.set()
        release_write.set()
        for task in (launch, close):
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        svc.stop()
