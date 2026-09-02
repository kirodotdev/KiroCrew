"""The document title the agent supplies is caller data, not trusted text.

A title is free-form, so it can carry a credential. It reaches the SEL audit log
AND the tool result that is rendered into chat and persisted in the transcript --
a wider audience than the audit log. Both take the redacted form.

Also pins the auth wiring for the tool's gateway leg: the route must sit on the
internal-secret allowlist or every MCP call falls through to cookie auth and
403s with "Token required".
"""
from __future__ import annotations

from unittest.mock import patch

from kiro_crew.mcp_core import _call_tool_inner

_LEAKY = "notes AKIAIOSFODNN7EXAMPLE"


def _add(title: str) -> str:
    with patch("kiro_crew.mcp_core._post", return_value={"status": "ok", "items": 3}), \
         patch("kiro_crew.mcp_core.sel"):
        return _call_tool_inner("knowledge_add_document",
                                {"title": title, "content": "body text",
                                 "source_uri": "chat://x"})


def test_a_credential_in_the_title_does_not_reach_the_chat_result():
    out = _add(_LEAKY)
    assert "AKIAIOSFODNN7EXAMPLE" not in out, (
        "the tool result is rendered into chat and persisted -- redact the title")
    assert "REDACTED" in out or "notes" in out


def test_an_ordinary_title_is_returned_unchanged():
    """Redaction must be a no-op for a normal name, or the message gets worse."""
    out = _add("Design Review Notes")
    assert "Design Review Notes" in out
    assert "3 chunk(s)" in out


def test_a_denied_gateway_response_surfaces_a_clear_error():
    """When no usable credential reaches the gateway, the tool must say so
    plainly instead of pretending the document was added.

    The allowlist wiring that makes the gateway accept the tool's handshake is
    pinned in test_token_auth.py (test_knowledge_agent_document_is_registered_
    internal_path), next to the middleware it exercises.
    """
    with patch("kiro_crew.mcp_core._post",
               return_value={"error": "Token required"}) as post, \
            patch("kiro_crew.mcp_core.sel"):
        out = _call_tool_inner("knowledge_add_document",
                               {"title": "Runbook", "content": "body text",
                                "source_uri": "chat://x"})
    assert out == "Could not add the document: Token required"
    # The write goes through _post, the helper that attaches the
    # X-Internal-Secret handshake — the authenticated client, not a bare one.
    post.assert_called_once()
    assert post.call_args.args[0] == "/api/knowledge/agent-document"
