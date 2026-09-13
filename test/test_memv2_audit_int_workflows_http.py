"""Workflow HTTP admission keeps private caller identity ahead of run identity."""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from member_memory_helpers import env as _member_env
from member_memory_helpers import make_request
from member_memory_helpers import member_proof as _member_proof

from kiro_crew import member_memory_auth as auth
from kiro_crew.dashboard.handlers import workflows
from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS, _STRICT_INTERNAL_API_PATHS
from kiro_crew.dashboard.token_auth import token_auth_middleware

pytestmark = pytest.mark.xdist_group("memory_workflow_http")
env = _member_env
member_proof = _member_proof

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
@pytest.mark.parametrize(
    "caller,session,status",
    [
        ("proof", "dashboard:alice", 409),
        ("peer", "dashboard:alice", 409),
        ("proof", None, 403),
        ("proof", "dashboard:global", 403),
        ("peer", None, 403),
        ("peer", "dashboard:global", 403),
        ("unknown", None, 403),
        ("v1", "dashboard:global", 200),
        ("v1", None, 200),
        ("v1", "dashboard:alice", 403),
    ],
)
async def test_workflow_http_private_boundary(
    env, member_proof, monkeypatch, entry, path, body, method, caller, session, status
):
    # Only kernel peer discovery is synthetic; signed proofs, protected records,
    # store ownership, middleware and handler admission use their real paths.
    peer = os.getpid() if caller in {"peer", "v1"} else None
    monkeypatch.setattr(auth, "_request_peer_pid", lambda request: peer)
    if caller == "v1":
        auth.publish_member_session_pid(os.getpid(), "dashboard:global", memory_store="")
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
    if caller == "proof":
        headers[auth.PROOF_HEADER] = member_proof
    async with TestClient(TestServer(app, host="127.0.0.1")) as client:
        response = await client.post(path, json=body, headers=headers)
        payload = await response.json()
        assert response.status == status, payload
    if status != 200:
        expected = (
            "workflow_private_memory_unsupported" if status == 409 else "member_session_unverified"
        )
        assert payload["code"] == expected
        for call in vars(service).values():
            call.assert_not_called()
    else:
        getattr(service, method).assert_awaited_once()
        if method == "rerun_subtree":
            assert service.rerun_subtree.await_args.args[0] == "global-run"
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
