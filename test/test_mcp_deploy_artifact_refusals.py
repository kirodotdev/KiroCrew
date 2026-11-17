"""``deploy_artifact`` refusals as they arrive over the real gateway client.

The deploy route answers a scan block and its other refusals with 4xx bodies, so
the tool sees them only after ``mcp_core._send`` has decoded the ``HTTPError``.
Mocking ``_post`` to return the route's dict skips that decoding; these tests raise
the HTTP error at ``loopback_urlopen`` instead.
"""

from __future__ import annotations

import email.message
import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from kiro_crew import mcp_core
from kiro_crew.mcp_core import _call_tool


def _http_error(status: int, payload: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://localhost:5476/api/deploy/deploy",
        status,
        "Conflict",
        email.message.Message(),
        io.BytesIO(json.dumps(payload).encode()),
    )


@pytest.fixture(autouse=True)
def _pinned_identity(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:chat-1")
    _floor_monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "dashboard:chat-1")
    _floor_monkeypatch.setattr(mcp_core, "_internal_secret", lambda: "s3cr3t")


def test_a_scan_block_is_offered_to_a_human_override() -> None:
    blocked = {
        "blocked": True,
        "reason": "scan",
        "findings": "internal-host reference",
        "count": 1,
        "content_digest": "abc123",
        "profile": "p",
        "region": "us-east-1",
    }
    with (
        patch("kiro_crew.mcp_core.loopback_urlopen", side_effect=_http_error(409, blocked)),
        patch("kiro_crew.deploy.pending.add_pending") as add_pending,
    ):
        result = _call_tool("deploy_artifact", {"site_id": "my-app", "local_dir": "/tmp/site"})

    assert not result.startswith("Error:")
    assert "Pending confirmations" in result
    assert add_pending.call_count == 1
    entry = add_pending.call_args[0][0]
    assert entry["override_scan_required"] is True
    assert entry["content_digest"] == "abc123"


def test_a_credential_scan_block_stays_a_hard_block() -> None:
    blocked = {
        "blocked": True,
        "reason": "scan",
        "findings": "key found",
        "count": 1,
        "credential": True,
    }
    with (
        patch("kiro_crew.mcp_core.loopback_urlopen", side_effect=_http_error(409, blocked)),
        patch("kiro_crew.deploy.pending.add_pending") as add_pending,
    ):
        result = _call_tool("deploy_artifact", {"site_id": "my-app", "local_dir": "/tmp/site"})

    assert "Deploy BLOCKED by scan" in result
    assert add_pending.call_count == 0


def test_a_staging_refusal_keeps_its_reason() -> None:
    blocked = {
        "blocked": True,
        "reason": "scan",
        "credential": True,
        "message": "symlink-in-tree: 1 link(s) found",
    }
    with (
        patch("kiro_crew.mcp_core.loopback_urlopen", side_effect=_http_error(409, blocked)),
        patch("kiro_crew.deploy.pending.add_pending") as add_pending,
    ):
        result = _call_tool("deploy_artifact", {"site_id": "my-app", "local_dir": "/tmp/site"})

    assert "Deploy BLOCKED by scan" in result
    assert "symlink-in-tree: 1 link(s) found" in result
    assert add_pending.call_count == 0


def test_a_refusal_keeps_its_details_and_command() -> None:
    refusal = {
        "error": "This account has no cleanup stack.",
        "code": "reaper_required",
        "details": "Use ttl_hours=0 for a permanent site.",
        "remediation": "kirocrew deploy reaper install",
    }
    with patch("kiro_crew.mcp_core.loopback_urlopen", side_effect=_http_error(409, refusal)):
        result = _call_tool(
            "deploy_artifact", {"site_id": "my-app", "artifact_slug": "a", "ttl_hours": 24}
        )

    assert result.startswith("Error: This account has no cleanup stack.")
    assert "Details: Use ttl_hours=0 for a permanent site." in result
    assert "Command: kirocrew deploy reaper install" in result
