"""An agent-socket call is credited to the agent on the artifact activity routes.

The agent socket sends a capability and no ``X-Internal-Secret`` header; the auth
middleware marks the request ``internal_auth`` instead. Each route that tags who did
something must read that mark too (``route_plumbing._is_agent_call``), or the agent's
work lands on the timeline as the person's.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from artifacts_handlers_helpers import (  # noqa: F401 -- fixtures are requested by name
    _json_body,
    _request,
    isolated_store_fixture,
    patch_restricted_fixture,
)
from test_artifacts_handlers import linkable_project  # noqa: F401 -- fixture

from kiro_crew.dashboard.handlers import artifacts as art_handlers
from kiro_crew.dashboard.handlers.artifacts import (
    api_artifact_detail,
    api_artifact_publish,
    api_artifact_record_event,
    api_artifact_update,
    api_artifacts_create,
)


def _socket_agent_request(**kwargs) -> MagicMock:
    """A request from the agent socket: marked ``internal_auth``, no secret header."""
    req = _request(**kwargs)
    assert "X-Internal-Secret" not in req.headers
    marks = {"internal_auth": True}
    req.get = lambda key, default=None: marks.get(key, default)
    return req


@pytest.mark.asyncio
async def test_socket_agent_dedup_resave_is_iterated_by_agent(
    isolated_store, patch_restricted, linkable_project  # noqa: F811
) -> None:
    src = linkable_project / "brd.md"
    src.write_text("# v1", encoding="utf-8")
    body = {
        "name": "brd",
        "content": "# v1",
        "kind": "markdown",
        "source": "manual",
        "source_path": str(src),
    }
    first = await api_artifacts_create(_request(body=body))
    assert first.status == 201
    second = await api_artifacts_create(
        _socket_agent_request(body={**body, "content": "# v2 from agent"})
    )
    assert second.status == 200
    latest = isolated_store.get(_json_body(second)["slug"]).events[-1]
    assert (latest["type"], latest["by"]) == ("iterated", "agent")


@pytest.mark.asyncio
async def test_socket_agent_update_is_iterated_by_agent(isolated_store, patch_restricted) -> None:
    isolated_store.create(name="x", content="v1", slug="x")
    resp = await api_artifact_update(
        _socket_agent_request(body={"content": "v2"}, match={"slug": "x"})
    )
    assert resp.status == 200
    assert _json_body(resp)["version"] == 2  # agent update snapshots
    latest = isolated_store.get("x").events[-1]
    assert (latest["type"], latest["by"]) == ("iterated", "agent")


@pytest.mark.asyncio
async def test_socket_agent_referenced_event_is_by_agent(isolated_store, patch_restricted) -> None:
    isolated_store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
    resp = await api_artifact_record_event(
        _socket_agent_request(match={"slug": "x"}, body={"type": "referenced"})
    )
    assert resp.status == 200
    assert _json_body(resp)["event"]["by"] == "agent"


@pytest.mark.asyncio
async def test_socket_agent_publish_passes_agent_actor(
    isolated_store, patch_restricted, monkeypatch
) -> None:
    isolated_store.create(name="Doc", content="hi", slug="doc", kind="markdown")
    monkeypatch.setattr(art_handlers, "_publish_governance_denied", lambda *_a, **_k: None)
    publish = AsyncMock(return_value=None)
    monkeypatch.setattr(art_handlers.publish_sync, "publish", publish)
    resp = await api_artifact_publish(
        _socket_agent_request(match={"slug": "doc"}, body={"visibility": "PRIVATE"})
    )
    assert resp.status == 200
    assert publish.await_args.kwargs["actor"] == "agent"


@pytest.mark.asyncio
async def test_socket_agent_read_leaves_referenced_breadcrumb(
    isolated_store, patch_restricted
) -> None:
    isolated_store.create(name="x", content="<x/>", slug="x")
    resp = await api_artifact_detail(
        _socket_agent_request(match={"slug": "x"}, session_key="chat-7")
    )
    assert resp.status == 200
    events = [e for e in isolated_store.get("x").events if e.get("type") == "referenced"]
    assert [(e["session_id"], e["by"]) for e in events] == [("chat-7", "agent")]
