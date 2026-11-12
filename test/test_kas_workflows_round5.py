"""Edge cases of the KAS workflow HTTP routes.

The step transcript route sends its whole body through the same recursive
key-and-value redaction as every other KAS read, so a credential-shaped value
in a field the transcript builder passes through raw (a tool name, a session's
model id) never reaches the browser.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.dashboard.handlers.kas_workflows as kas_handlers
from kiro_crew.dashboard import server
from kiro_crew.kas_workflows import router as router_mod

pytestmark = pytest.mark.asyncio

#: AWS's documented example access key: credential-shaped, not a credential.
_KEY = "AKIAIOSFODNN7EXAMPLE"


class _TranscriptRouter:
    async def step_transcript(self, workflow_id, node_id, path=None):
        return {
            "workflowId": workflow_id,
            "nodeId": node_id,
            "session": {"modelId": f"model-{_KEY}", "agentMode": "a"},
            "entries": [{"type": "tool", "name": f"probe {_KEY}", "kind": "execute"}],
        }


@pytest.fixture
def app(monkeypatch):
    async def no_refusal(_request, _operation):
        return None

    monkeypatch.setattr(router_mod, "_ROUTER", _TranscriptRouter())
    monkeypatch.setattr(router_mod, "get_router", lambda: router_mod._ROUTER)
    monkeypatch.setattr(kas_handlers, "_refusal", no_refusal)
    application = web.Application()
    application["state"] = SimpleNamespace(get_slot=lambda _k: None)
    server._register_mcp_routes(application)
    return application


async def test_the_step_transcript_body_is_redacted_whole(app):
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/workflows/kas/runs/wf_1/steps/a/transcript")
        assert resp.status == 200, await resp.text()
        text = await resp.text()
    assert _KEY not in text
    assert '"workflowId": "wf_1"' in text or '"workflowId":"wf_1"' in text
