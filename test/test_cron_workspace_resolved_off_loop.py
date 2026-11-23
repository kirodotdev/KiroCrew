"""A cron fire resolves the crew's workspace folder off the gateway event loop.

The resolver resolves the folder on the filesystem and judges it against the sensitive-path
list, which can block on a stalled mount, so the fire hands it to a worker thread instead of
calling it from the loop.
"""

from __future__ import annotations

import threading

import pytest
from slack_gateway_cron_exec_coverage_helpers import (
    _cron_message_cb,
    _job,
    _make_orchestrator,
    _message_arm_sessions,
    _mock_dashboard_state,
)

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, WorkspaceConfig


@pytest.mark.asyncio
async def test_the_workspace_of_an_alias_is_resolved_on_a_worker_thread(
    tmp_path, _floor_monkeypatch
) -> None:
    from kiro_crew.slack import gateway as _gw

    _floor_monkeypatch.setattr(_gw.live, "snapshot", lambda: None)
    ws_dir = str(tmp_path / "in-3d-ws")
    orch = _make_orchestrator()
    orch._cfg = KiroCrewConfig(
        agents={
            "in-3d": KiroCrewAgentConfig(
                kiro_agent="kirocrew", workspace="in-3d", memory_store="default"
            )
        },
        workspaces={"in-3d": WorkspaceConfig(dir=ws_dir)},
    )
    orch.sessions = _message_arm_sessions()
    orch.dashboard_state = _mock_dashboard_state()
    real = orch._resolve_cron_agent
    threads: list[int] = []

    def spy(*args, **kwargs):
        threads.append(threading.get_ident())
        return real(*args, **kwargs)

    orch._resolve_cron_agent = spy
    loop_thread = threading.get_ident()
    job = _job(id="off1", name="off-loop probe", agent_id="in-3d")

    async with _cron_message_cb(orch, result_text="ok") as callback:
        await callback(job)

    assert threads, "the resolver was never called"
    assert loop_thread not in threads
    assert orch.sessions.get_or_create.await_args.kwargs.get("cwd") == ws_dir
