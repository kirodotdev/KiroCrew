"""Tests for the tag_session MCP tool (kiro_crew.mcp_tools.sessions.tag_session)."""

from __future__ import annotations

import unittest.mock
from typing import Any

import pytest

from kiro_crew.mcp_tools.sessions import tag_session

TAGS_VOCAB = [
    {"id": "planned", "name": "Planned", "color": "#6b7280", "order": 0, "status": True},
    {"id": "todo", "name": "ToDo", "color": "#3b82f6", "order": 1, "status": True},
    {"id": "implementation", "name": "Implementation", "color": "#8b5cf6", "order": 2, "status": True},
    {"id": "review", "name": "Review", "color": "#f59e0b", "order": 3, "status": True},
    {"id": "done", "name": "Done", "color": "#10b981", "order": 4, "status": True},
    {"id": "urgent", "name": "Urgent", "color": "#ef4444", "order": 5, "status": False},
]

SLOT_KEY = "chat-46-1786668000"
SESSION_KEY = f"dashboard:{SLOT_KEY}"


@pytest.fixture
def mock_mcp(monkeypatch):
    """Patch mcp_core helpers used by tag_session."""
    import kiro_crew.mcp_core as mcp_core

    get_responses: dict[str, Any] = {
        "/api/chat/tags": TAGS_VOCAB,
        "/api/chat/slots": [{"key": SLOT_KEY, "name": "chat-46", "tags": ["planned"]}],
    }

    def _fake_get(path: str) -> Any:
        return get_responses.get(path, {"error": "not found"})

    put_calls: list[tuple[str, Any]] = []

    def _fake_put(path: str, body: Any) -> dict:
        put_calls.append((path, body))
        return {"ok": True, "tags": body.get("tags", [])}

    def _fake_resolve_session_key() -> str:
        return SESSION_KEY

    sel_mock = unittest.mock.MagicMock()

    def _fake_sel():
        return sel_mock

    monkeypatch.setattr(mcp_core, "_get", _fake_get)
    monkeypatch.setattr(mcp_core, "_put", _fake_put)
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", _fake_resolve_session_key)
    monkeypatch.setattr(mcp_core, "sel", _fake_sel)

    return {
        "get_responses": get_responses,
        "put_calls": put_calls,
        "sel_mock": sel_mock,
    }


def test_tag_session_advances_status(mock_mcp):
    """Advancing from planned -> implementation succeeds."""
    result = tag_session("tag_session", {"tag": "implementation"})
    assert mock_mcp["put_calls"], "PUT should have been called"
    path, body = mock_mcp["put_calls"][0]
    assert body["tags"] == ["implementation"]
    assert "Tagged" in result
    assert "implementation" in result.lower() or "Implementation" in result


def test_tag_session_regression_blocked(mock_mcp):
    """Regressing from implementation -> planned is blocked without force."""
    # Slot currently at implementation
    mock_mcp["get_responses"]["/api/chat/slots"] = [
        {"key": SLOT_KEY, "name": "chat-46", "tags": ["implementation"]}
    ]
    result = tag_session("tag_session", {"tag": "planned", "force": False})
    assert not mock_mcp["put_calls"], "PUT should NOT have been called"
    assert "regression" in result.lower() or "blocked" in result.lower()


def test_tag_session_force_regression(mock_mcp):
    """Forcing regression from implementation -> planned succeeds."""
    mock_mcp["get_responses"]["/api/chat/slots"] = [
        {"key": SLOT_KEY, "name": "chat-46", "tags": ["implementation"]}
    ]
    _result = tag_session("tag_session", {"tag": "planned", "force": True})  # noqa: F841
    assert mock_mcp["put_calls"], "PUT should have been called with force"
    path, body = mock_mcp["put_calls"][0]
    assert body["tags"] == ["planned"]


def test_tag_session_unknown_tag(mock_mcp):
    """Unknown tag name returns error listing available tags."""
    result = tag_session("tag_session", {"tag": "nonexistent"})
    assert not mock_mcp["put_calls"]
    assert "nonexistent" in result
    # Should list available tag names
    assert "Planned" in result
    assert "Done" in result


def test_tag_session_case_insensitive(mock_mcp):
    """Tag lookup is case-insensitive."""
    _result = tag_session("tag_session", {"tag": "DONE"})  # noqa: F841
    assert mock_mcp["put_calls"], "PUT should be called for case-insensitive match"
    path, body = mock_mcp["put_calls"][0]
    assert "done" in body["tags"]


def test_tag_session_non_status_tag_added(mock_mcp):
    """Non-status tag is added alongside existing status tag."""
    mock_mcp["get_responses"]["/api/chat/slots"] = [
        {"key": SLOT_KEY, "name": "chat-46", "tags": ["planned"]}
    ]
    _result = tag_session("tag_session", {"tag": "urgent"})  # noqa: F841
    assert mock_mcp["put_calls"], "PUT should be called"
    path, body = mock_mcp["put_calls"][0]
    assert body["tags"] == ["planned", "urgent"]


def test_tag_session_non_status_already_present(mock_mcp):
    """Non-status tag already present is a no-op."""
    mock_mcp["get_responses"]["/api/chat/slots"] = [
        {"key": SLOT_KEY, "name": "chat-46", "tags": ["planned", "urgent"]}
    ]
    result = tag_session("tag_session", {"tag": "urgent"})
    assert not mock_mcp["put_calls"], "PUT should NOT be called"
    assert "already" in result.lower()


def test_tag_session_slot_not_found(mock_mcp):
    """Explicit slot_key that doesn't exist returns error."""
    result = tag_session("tag_session", {"tag": "planned", "slot_key": "nonexistent-slot"})
    assert not mock_mcp["put_calls"]
    assert "not found" in result.lower() or "nonexistent-slot" in result


def test_tag_session_defaults_to_own_slot(mock_mcp):
    """Without slot_key, handler resolves own session and strips dashboard: prefix."""
    _result = tag_session("tag_session", {"tag": "implementation"})  # noqa: F841
    # The PUT should target the slot derived from stripping 'dashboard:' from SESSION_KEY
    assert mock_mcp["put_calls"], "PUT should be called"
    path, _ = mock_mcp["put_calls"][0]
    assert SLOT_KEY in path
    assert "dashboard:" not in path


def test_tag_session_subagent_no_identity(mock_mcp, monkeypatch):
    """Subagent with no session identity and no explicit slot_key fails closed."""
    import kiro_crew.mcp_core as mcp_core

    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
    result = tag_session("tag_session", {"tag": "done"})
    assert not mock_mcp["put_calls"], "PUT should NOT be called"
    assert "cannot determine" in result.lower() or "no session identity" in result.lower()
