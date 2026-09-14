"""Owner cookie/WS/HTTP approval with the real spawn gate, not a trust override."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from chat_test_helpers import _make_state
from e2e.test_gateway_boot_matrix import _Client
from e2e.test_private_workflow_memory import _nested_with_owner_approval
from test_subagent_turn_resilience import (
    _complete_event,
    _mock_ctx_builder,
    _mock_sessions,
    _text_event,
)

from kiro_crew.dashboard.handlers.sessions import api_approval_resolve
from kiro_crew.dashboard.token_auth import generate_token, token_auth_middleware
from kiro_crew.dashboard.ws import api_ws
from kiro_crew.providers.base import LLMEvent
from kiro_crew.slack.gateway import GatewayOrchestrator
from kiro_crew.subagent import SubagentManager
from kiro_crew.testing.workflow_memory_scenario import spawn_progress_summary

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


@pytest.mark.asyncio
async def test_owner_socket_approves_only_returned_spawn_once(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    monkeypatch.setattr(state, "status_snapshot", lambda **_kwargs: {})
    gateway = SimpleNamespace(
        slack=None,
        _owner_id="",
        dashboard_state=state,
        _cfg=SimpleNamespace(hooks={}),
        _approval_mode="reads",
        autonudge_svc=None,
        sessions=None,
    )
    gateway._dashboard_client_attached = lambda: GatewayOrchestrator._dashboard_client_attached(
        gateway
    )
    callback = GatewayOrchestrator._interactive_approval(
        gateway, "subagent", raise_when_unreachable=True
    )

    async def approval(request_id, description, parent):
        return await callback(
            LLMEvent(kind="permission_request", request_id=request_id, title=description), parent
        )

    async def stream(_message):
        yield _text_event('{"write": {"ok": true}, "recall": "complete"}')
        yield _complete_event()

    sessions = _mock_sessions(stream)
    sessions.get_approval_policy.return_value = "ask"
    sessions._provider.context_window_tokens = lambda: 0
    sessions._provider.context_used_tokens = lambda: 0
    builder = _mock_ctx_builder()
    builder.hooks.auto_approve_subagent_spawn = False
    manager = SubagentManager(sessions=sessions, ctx_builder=builder, on_spawn_approval=approval)
    manager._should_use_session_sharing = lambda _info: False
    manager._spawn_stagger_secs = 0
    state.subagents = manager
    first = manager.spawn("[[WF_E2E:WORK:A]]", keep=True)
    assert first is not None
    await asyncio.wait_for(manager._tasks[first.id], 10)
    assert (
        spawn_progress_summary({"done": first.done, "error": first.error})["terminal_reason"]
        == "no_approval_surface"
    )
    sessions.get_or_create.assert_not_called()
    spawned = []
    run_tasks = []
    other_approvals = []

    async def status(_request):
        return web.json_response({"ok": True})

    async def start(_request):
        assert state.dashboard_user_ws_count() == 1
        sessions.get_or_create.assert_not_called()
        other_approvals.append(
            asyncio.create_task(
                state.request_approval(
                    "spawn:unrelated", "subagent", "spawn_run(unrelated)", is_background=True
                )
            )
        )
        info = manager.spawn("[[WF_E2E:WORK:A]]", keep=True)
        spawned.append(info)
        run_tasks.append(manager._tasks[info.id])
        return web.json_response({"run_id": "wf_1"})

    async def finished(_request):
        info = spawned[0]
        return web.json_response(
            {
                "status": "finished",
                "result": json.dumps(
                    {
                        "nested_spawn": {"content": [{"text": f"  {info.id}: [[WF_E2E:WORK:A]]"}]},
                    }
                ),
            }
        )

    app = web.Application(middlewares=[token_auth_middleware()])
    app["state"] = state
    app["allowed_origins"] = set()
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/ws", api_ws)
    app.router.add_post("/api/approvals/{id}/{action}", api_approval_resolve)
    app.router.add_post("/api/workflows/run", start)
    app.router.add_get("/api/workflows/runs/{run_id}", finished)
    try:
        async with TestServer(app, host="127.0.0.1") as server:
            client = await asyncio.to_thread(_Client, server.port, generate_token("local-app"))
            _, spawn_id = await asyncio.wait_for(
                _nested_with_owner_approval(client, "dashboard:owner", "synthetic workflow"), 20
            )
            await asyncio.wait_for(run_tasks[0], 10)
            assert spawn_id == spawned[0].id
            assert "spawn:unrelated" in state._pending_approvals
            assert not other_approvals[0].done()
            assert spawned[0].done and not spawned[0].error
            assert json.loads(spawned[0].result) == {"write": {"ok": True}, "recall": "complete"}
            sessions.get_or_create.assert_awaited_once()
            assert sessions.get_or_create.call_args.kwargs["approval_policy"] == "ask"
            with pytest.raises(AssertionError, match="HTTP 404"):
                await asyncio.to_thread(client.post, f"/api/approvals/spawn:{spawn_id}/approve", {})
            # A fresh spawn still needs approval; allow-once did not promote trust.
            again = manager.spawn("[[WF_E2E:WORK:A]]", keep=True)
            task = manager._tasks[again.id]
            await asyncio.wait_for(task, 10)
            assert again.done and again.error.startswith("spawn rejected")
            sessions.get_or_create.assert_awaited_once()
    finally:
        for task in other_approvals:
            task.cancel()
        await asyncio.gather(*other_approvals, return_exceptions=True)
        await manager.cancel_all()
