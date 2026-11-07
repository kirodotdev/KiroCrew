"""R47-R52: shared implementations must preserve host and vendor boundaries."""
from __future__ import annotations

from dataclasses import replace

from kiro_byo.config import ProviderSpec
from kiro_byo.mcp_client import McpManager, ToolBinding
from kiro_byo.providers import cohere_v2, gemini_interactions, responses_api
from kiro_byo.providers.chat_completions_common import headers as chat_headers
from kiro_byo.providers.http_sse import standard_json_headers


def _provider(kind: str, *, headers: dict[str, str] | None = None, stream: bool = True) -> ProviderSpec:
    return ProviderSpec(
        id="test", name="Test", type=kind,
        base_url="https://example.invalid", api_key="secret",
        models=(), stream=stream, headers=headers or {},
    )


def _binding(server: str, name: str) -> ToolBinding:
    return ToolBinding(
        public_name=f"mcp__{server}__{name}", server_name=server, tool_name=name,
        description="", input_schema={"type": "object", "properties": {}},
    )


def test_crew_tool_visibility_requires_live_core_provenance_and_explicit_permission() -> None:
    manager = McpManager([])
    try:
        for server, name in (
            ("kirocrew-core", "browser"), ("kirocrew-core", "skill_search"),
            ("kirocrew-core", "artifact_get"), ("kirocrew-core", "artifact_delete"),
            ("untrusted", "browser"), ("untrusted", "skill_search"),
        ):
            binding = _binding(server, name)
            manager.bindings[binding.public_name] = binding
        # Persisted schema alone never grants host privileges.
        assert manager.crew_browser_tool_names() == ()
        assert manager.crew_skill_tool_names() == ()
        assert manager.crew_artifact_tool_names() == ()
        manager._live_verified_servers.add("kirocrew-core")
        assert manager.crew_browser_tool_names() == ("mcp__kirocrew-core__browser",)
        assert manager.crew_skill_tool_names() == ("mcp__kirocrew-core__skill_search",)
        assert manager.crew_artifact_tool_names() == ("mcp__kirocrew-core__artifact_get",)
        # Agent policy cannot be bypassed even if Crew has verified the MCP.
        denied = lambda b: b.tool_name not in {"browser", "artifact_get"}
        assert manager.crew_browser_tool_names(allowed=denied) == ()
        assert manager.crew_artifact_tool_names(allowed=denied) == ()
        assert manager.crew_skill_tool_names(allowed=denied) == ("mcp__kirocrew-core__skill_search",)
    finally:
        manager.close()


def test_native_headers_preserve_provider_overrides_and_auth_variants() -> None:
    p = _provider("cohere-v2", headers={"Accept": "application/custom", "Authorization": "Private x", "X-Extra": "ok"})
    expected = standard_json_headers(p)
    assert chat_headers(p) == expected == cohere_v2._headers(p)
    assert expected["Authorization"] == "Private x"
    assert expected["Accept"] == "application/custom"
    assert expected["X-Extra"] == "ok"
    assert "User-Agent" in expected
    assert standard_json_headers(replace(p, stream=False, headers={}))["Accept"] == "application/json"

    gemini = _provider("gemini-interactions", headers={"Api-Revision": "testing", "x-goog-api-key": "override"})
    native = gemini_interactions._headers(gemini)
    assert native["Api-Revision"] == "testing"
    assert native["x-goog-api-key"] == "override"
    assert "Authorization" not in native
    assert gemini_interactions._headers(replace(gemini, headers={}))["Api-Revision"] == "2026-05-20"

    responses = _provider("openai-responses", headers={"X-Extra": "custom"})
    wire = responses_api._headers(responses)
    assert wire["Authorization"] == "Bearer secret"
    assert wire["X-Extra"] == "custom"


def test_responses_text_and_reasoning_remain_distinct_and_robust() -> None:
    items = [
        {"type": "message", "content": [{"type": "output_text", "text": "A"}, {"type": "text", "text": "B"}, {"type": "summary_text", "text": "ignore"}]},
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "C"}, {"type": "text", "text": "D"}, {"type": "output_text", "text": "ignore"}]},
        {"type": "message", "content": "not a list"},
        None,
        {"type": "message", "content": [{"type": "text", "text": 42}]},
    ]
    assert responses_api._output_text(items) == "AB"
    assert responses_api._reasoning_summary(items) == "CD"
    assert responses_api._output_text(None) == ""
    assert responses_api._reasoning_summary({}) == ""
