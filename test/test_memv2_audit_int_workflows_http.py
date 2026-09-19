"""Workflow HTTP admission keeps private caller identity ahead of run identity."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from member_memory_helpers import env as _member_env
from member_memory_helpers import make_request

from kiro_crew import member_memory_auth as auth
from kiro_crew.dashboard.handlers import workflows
from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS, _STRICT_INTERNAL_API_PATHS
from kiro_crew.dashboard.token_auth import token_auth_middleware
from kiro_crew.mcp_core import _post as _real_mcp_post

pytestmark = pytest.mark.xdist_group("memory_workflow_http")
env = _member_env

_ENTRY_POINTS = [
    ("author", "/api/workflows/author", {"intent": "summarize"}, "author"),
    ("run", "/api/workflows/run", {"source": "source"}, "start"),
    ("run_intent", "/api/workflows/run_intent", {"intent": "summarize"}, "start_from_intent"),
    ("definition_run", "/api/workflows/definitions/saved/run", {}, "start_definition"),
    ("run_rerun", "/api/workflows/runs/global-run/rerun", {}, "rerun_subtree"),
]


def _service():
    return SimpleNamespace(
        **{name: AsyncMock(return_value={"run_id": "new-run"}) for *_, name in _ENTRY_POINTS}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("entry,path,body,method", _ENTRY_POINTS)
@pytest.mark.parametrize("session", ["dashboard:alice", "dashboard:global", None])
async def test_workflow_http_ordinary_auth_and_canonical_routing(
    env, entry, path, body, method, session
):
    service = _service()
    env.state.workflow_service = service
    app = web.Application(
        middlewares=[
            token_auth_middleware(
                internal_paths=_STRICT_INTERNAL_API_PATHS,
                mixed_internal_paths=_MIXED_INTERNAL_API_PATHS,
                internal_secret="test-workflow-secret",
            )
        ]
    )
    app["state"] = env.state
    route = path.replace("global-run", "{run_id}").replace("/saved/", "/{workflow_ref}/")
    app.router.add_post(route, getattr(workflows, f"api_workflow_{entry}"))
    headers = {"X-Internal-Secret": "test-workflow-secret"}
    if session is not None:
        headers["X-Session-Key"] = session
    async with TestClient(TestServer(app, host="127.0.0.1")) as client:
        response = await client.post(path, json=body, headers=headers)
        payload = await response.json()
        assert response.status == (409 if session is None else 200), payload
        unauthorized = await client.post(
            path, json=body, headers={"X-Internal-Secret": "wrong-secret"}
        )
        assert unauthorized.status in (401, 403)
    if session is None:
        getattr(service, method).assert_not_called()
    else:
        getattr(service, method).assert_awaited_once()
    assert auth.read_private_session_store("dashboard:alice") == "member-alice"


@pytest.mark.asyncio
@pytest.mark.parametrize("entry,path,body,method", _ENTRY_POINTS)
async def test_owner_browser_workflow_dispatch_unchanged(env, entry, path, body, method):
    service = _service()
    env.state.workflow_service = service
    request = make_request(
        env.state,
        path,
        method="POST",
        body=body,
        owner=True,
        session="dashboard:global",
        match_info={"run_id": "global-run", "workflow_ref": "saved"},
    )
    response = await getattr(workflows, f"api_workflow_{entry}")(request)
    assert response.status == 200
    getattr(service, method).assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,args,path",
    [
        ("workflow_author", {"intent": "test"}, "/api/workflows/author"),
        ("workflow_run", {"source": "test"}, "/api/workflows/run"),
        ("workflow_run", {"intent": "test"}, "/api/workflows/run_intent"),
        ("workflow_run", {"workflow": "saved"}, "/api/workflows/definitions/saved/run"),
        ("workflow_cancel", {"run_id": "wf_000001"}, "/api/workflows/runs/wf_000001/cancel"),
        ("workflow_rerun_subtree", {"run_id": "wf_000001"}, "/api/workflows/runs/wf_000001/rerun"),
    ],
)
async def test_mcp_workflow_real_transport_pins_authenticated_path(
    env, monkeypatch, tool, args, path
):
    import asyncio

    from kiro_crew import mcp_core
    from kiro_crew.mcp_tools import workflows as tools

    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:global")
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:wrong-parent")
    monkeypatch.setattr(mcp_core, "_post", _real_mcp_post)
    monkeypatch.setattr(mcp_core, "_internal_secret", lambda: "test-workflow-secret")
    seen = []

    async def endpoint(request):
        seen.append((request.path, request.headers["X-Session-Key"], await request.json()))
        return web.json_response({"ok": True, "run_id": "wf_000002", "source": "test"})

    app = web.Application(
        middlewares=[
            token_auth_middleware(
                internal_paths=_STRICT_INTERNAL_API_PATHS,
                mixed_internal_paths=_MIXED_INTERNAL_API_PATHS,
                internal_secret="test-workflow-secret",
            )
        ]
    )
    app["state"] = env.state
    app.router.add_post(path, endpoint)
    async with TestServer(app, host="127.0.0.1") as server:
        monkeypatch.setattr(mcp_core, "_resolve_api_target", lambda: (str(server.make_url("")), ""))
        result = await asyncio.to_thread(getattr(tools, tool), tool, args)
        assert "failed" not in result.lower(), result
        assert len(seen) == 1
        assert seen[0][:2] == (path, "dashboard:global")
        # A valid secret does not let an unresolved caller reach the transport.
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
        result = await asyncio.to_thread(getattr(tools, tool), tool, args)
        assert "Cannot verify" in result
        assert len(seen) == 1
