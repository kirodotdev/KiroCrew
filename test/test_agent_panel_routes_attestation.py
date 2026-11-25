"""The panel publish routes take ``X-Session-Key`` only behind an attestation.

The internal secret proves only that a caller is local. Over TCP loopback there is
no kernel peer check, so a process holding the secret could name another session's
key and publish into that session's crew panel. The routes accept the key on the
Unix-socket peer attestation or on the signed per-session token, and refuse it
otherwise.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from agent_panel_routes_helpers import (  # noqa: F401 -- fixtures are used by name
    SLUG,
    _client,
    _crew_log_on,
    vetted_fixture,
)

from kiro_crew import agent_panel, mcp_core, mcp_panel
from kiro_crew.dashboard.handlers import agent_panel as routes
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.session_token_sig import publish_session_token, verify_session_token

pytestmark = pytest.mark.asyncio

#: The crew's own DM session, the one the panel tool is mounted on.
CREW_KEY = "dashboard:chat-1"
#: A token minted for the crew's session, and one minted for some other session.
CREW_TOKEN = "c" * 64
OTHER_TOKEN = "d" * 64


@pytest.fixture
def tokens(monkeypatch, sel_private_root) -> None:
    """Publish both token mappings in the per-test data home, signed for real.

    Signed with the trust root of a SEL instance private to this test
    (``sel_private_root`` creates its key), so the worker's shared root is
    never touched.
    """
    monkeypatch.delenv(STUB_SESSION_TOKEN_ENV, raising=False)
    monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
    publish_session_token(CREW_TOKEN, CREW_KEY)
    publish_session_token(OTHER_TOKEN, "dashboard:chat-someone-else")
    # The verifier must agree, or every assertion below proves nothing.
    assert verify_session_token(CREW_TOKEN) == CREW_KEY


@pytest.fixture
def denials(monkeypatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    class _Sel:
        def log_api_access(self, **kw):
            events.append(kw)

    monkeypatch.setattr(routes, "sel", lambda: _Sel())
    return events


async def test_a_peer_attested_key_publishes(vetted):
    """The Unix-socket shape: the kernel peer check already bound the key."""
    async with _client(attested=True) as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"cycle": 1}},
            headers={"X-Session-Key": CREW_KEY},
        )
        assert resp.status == 200, await resp.text()
    assert agent_panel.read(SLUG)["data"] == {"cycle": 1}


@pytest.mark.parametrize(
    "extra",
    [{}, {"X-Session-Token": OTHER_TOKEN}, {"X-Session-Token": "e" * 64}],
    ids=["no-token", "another-sessions-token", "unpublished-token"],
)
async def test_an_unattested_tcp_key_naming_another_session_is_refused(
    vetted, tokens, denials, extra
):
    """The TCP shape with the secret and a named key the transport cannot vouch for."""
    async with _client(attested=False) as c:
        resp = await c.post(
            "/api/agent-panel/publish",
            json={"data": {"forged": True}},
            headers={"X-Session-Key": CREW_KEY, **extra},
        )
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == "session_unattested"
        listed = await c.get(
            "/api/agent-panel/templates", headers={"X-Session-Key": CREW_KEY, **extra}
        )
        assert listed.status == 403, await listed.text()
        assert (await listed.json())["code"] == "session_unattested"
    assert agent_panel.read(SLUG) is None, "a refused caller reached the store"
    refused = [e for e in denials if e.get("error") == "session key not attested"]
    assert [e["operation"] for e in refused] == ["agent_panel_publish", "agent_panel_templates"]
    assert all(e["outcome"] == "denied" and e["caller"] == CREW_KEY for e in refused)


async def test_the_real_panel_tool_call_carries_its_token_and_publishes(
    vetted, tokens, monkeypatch
):
    """``kirocrew-panel``'s own request, replayed over the TCP shape, is accepted.

    The tool resolves its key strictly from the token in its environment and sends
    that token with the key, which is what lets a TCP request through.
    """
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, CREW_TOKEN)
    sent: list[dict[str, Any]] = []

    def _send(path, *, data=None, headers, method="GET", **_kw):
        sent.append({"path": path, "data": data, "headers": dict(headers), "method": method})
        return {"ok": True, "panel": {"template": "default"}}

    monkeypatch.setattr(mcp_core, "_send", _send)
    monkeypatch.setattr(mcp_core, "_internal_secret", lambda: "test-secret")
    await asyncio.to_thread(mcp_panel._call_tool_inner, "panel_publish", {"data": {"cycle": 2}})
    assert len(sent) == 1, sent
    request = sent[0]
    assert request["headers"]["X-Session-Key"] == CREW_KEY
    assert request["headers"]["X-Session-Token"] == CREW_TOKEN
    async with _client(attested=False) as c:
        resp = await c.request(
            request["method"], request["path"], data=request["data"], headers=request["headers"]
        )
        assert resp.status == 200, await resp.text()
    assert agent_panel.read(SLUG)["data"] == {"cycle": 2}
