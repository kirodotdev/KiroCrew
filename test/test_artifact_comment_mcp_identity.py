"""The agent MCP comment tools, end to end, still store agent comments.

``artifact_post_comment`` and ``artifact_reply_comment`` reach the dashboard
routes through ``mcp_core._post``, and the routes read who wrote a comment from
the request's X-Internal-Secret header. This module runs the real tools and the
real ``_post`` header builder, swapping only ``mcp_core._send`` (the socket)
for a direct handler call, so the headers ``_post`` builds are the ones the
route sees. It is listed in conftest's ``_REAL_MCP_POST_MODULES`` for that
reason; the real network transport stays refused.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from body_stream_helpers import attach_json_body

from kiro_crew import artifacts as art_mod
from kiro_crew import mcp_core
from kiro_crew.artifacts import ArtifactStore
from kiro_crew.dashboard.handlers import artifacts as h
from kiro_crew.mcp_tools import artifacts as mcp_artifacts

_POST = re.compile(r"/api/artifacts/([^/]+)/comments")
_REPLY = re.compile(r"/api/artifacts/([^/]+)/comments/([^/]+)/reply")


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> ArtifactStore:
    s = ArtifactStore(root=tmp_path / "artifacts")
    s.create(name="Doc", content="# Hello\n\nsome body text", slug="doc", kind="markdown")
    monkeypatch.setattr(art_mod, "_default_store", s)
    monkeypatch.setattr(h, "_is_restricted_session", lambda state, request: False)
    return s


@pytest.fixture
def routed(monkeypatch) -> list[dict]:
    """Route ``mcp_core._send`` into the comment handlers; return the headers seen."""
    monkeypatch.setattr(mcp_core, "_internal_secret", lambda: "s3cret")
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "slot-abc")
    seen: list[dict] = []

    def fake_send(path, *, data=None, headers, method="GET", **_kw):
        seen.append(dict(headers))
        req = MagicMock()
        req.headers = dict(headers)
        req.query = {}
        req.app = {"state": MagicMock()}
        attach_json_body(req, data or b"{}")
        reply = _REPLY.fullmatch(path)
        post = _POST.fullmatch(path)
        if reply:
            req.match_info = {"slug": reply[1], "comment_id": reply[2]}
            handler = h.api_artifact_reply_comment
        else:
            assert post, path
            req.match_info = {"slug": post[1]}
            handler = h.api_artifact_post_comment
        resp = asyncio.run(handler(req))
        assert (method, resp.status) == ("POST", 201), resp.body
        return json.loads(resp.body)

    monkeypatch.setattr(mcp_core, "_send", fake_send)
    return seen


def test_mcp_post_and_reply_store_agent_comments(store, routed):
    out = mcp_artifacts.artifact_post_comment(
        "artifact_post_comment", {"slug": "doc", "text": "from the agent"}
    )
    assert out.startswith("Comment posted"), out
    root = store.list_comments("doc")[0]
    out = mcp_artifacts.artifact_reply_comment(
        "artifact_reply_comment", {"slug": "doc", "parent_id": root.id, "text": "agent reply"}
    )
    assert out.startswith("Reply posted"), out

    assert len(routed) == 2
    assert all(hd.get("X-Internal-Secret") == "s3cret" for hd in routed)
    by_body = {c.body: c for c in store.list_comments("doc")}
    assert (by_body["from the agent"].is_agent, by_body["from the agent"].author) == (True, "agent")
    assert (by_body["agent reply"].is_agent, by_body["agent reply"].author) == (True, "agent")
