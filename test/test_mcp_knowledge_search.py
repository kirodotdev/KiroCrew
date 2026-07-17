"""Tests for local_knowledge_search tool in mcp_core."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew.validation import ValidationError


@pytest.fixture
def mock_db_exists(tmp_path):
    """Create a fake knowledge.db so the path check passes."""
    db_dir = tmp_path / "workspace" / "knowledge"
    db_dir.mkdir(parents=True)
    (db_dir / "knowledge.db").touch()
    return tmp_path


class TestKnowledgeSearchDBMissing:
    @patch("kiro_crew.mcp_core.config_dir")
    def test_returns_not_configured_when_db_missing(self, mock_config_dir, tmp_path):
        mock_config_dir.return_value = tmp_path  # no knowledge.db here
        from kiro_crew.mcp_core import _call_tool_inner

        result = _call_tool_inner("local_knowledge_search", {"query": "auth"})
        assert "not configured" in result


class TestKnowledgeSearchValidation:
    def test_empty_query_raises_validation_error(self):
        from kiro_crew.mcp_core import _call_tool_inner

        with pytest.raises(ValidationError):
            _call_tool_inner("local_knowledge_search", {"query": ""})

    def test_whitespace_query_raises_validation_error(self):
        from kiro_crew.mcp_core import _call_tool_inner

        with pytest.raises(ValidationError):
            _call_tool_inner("local_knowledge_search", {"query": "   "})

    def test_limit_over_max_raises_validation_error(self):
        from kiro_crew.mcp_core import _call_tool_inner

        with pytest.raises(ValidationError):
            _call_tool_inner("local_knowledge_search", {"query": "test", "limit": 99})


class TestKnowledgeSearchResults:
    @patch("kiro_crew.mcp_core.config_dir")
    @patch("kiro_crew.mcp_core.HybridRetriever")
    @patch("kiro_crew.mcp_core.KnowledgeStore")
    @patch("kiro_crew.mcp_core.create_embedder_from_config")
    def test_no_results_returns_message(
        self, mock_embedder, mock_store_cls, mock_retriever_cls, mock_config_dir, mock_db_exists
    ):
        mock_config_dir.return_value = mock_db_exists
        mock_embedder.return_value = None
        mock_retriever_cls.return_value.search.return_value = []
        from kiro_crew.mcp_core import _call_tool_inner

        result = _call_tool_inner("local_knowledge_search", {"query": "nonexistent"})
        assert "No relevant knowledge found" in result

    @patch("kiro_crew.mcp_core.config_dir")
    @patch("kiro_crew.mcp_core.HybridRetriever")
    @patch("kiro_crew.mcp_core.KnowledgeStore")
    @patch("kiro_crew.mcp_core.create_embedder_from_config")
    def test_low_score_filtered_out(
        self, mock_embedder, mock_store_cls, mock_retriever_cls, mock_config_dir, mock_db_exists
    ):
        mock_config_dir.return_value = mock_db_exists
        mock_embedder.return_value = None
        mock_retriever_cls.return_value.search.return_value = [
            {"title": "Weak Match", "content": "low relevance", "score": 0.005, "source": "s1"}
        ]
        from kiro_crew.mcp_core import _call_tool_inner

        result = _call_tool_inner("local_knowledge_search", {"query": "test"})
        assert "No relevant knowledge found" in result

    @patch("kiro_crew.mcp_core.config_dir")
    @patch("kiro_crew.mcp_core.HybridRetriever")
    @patch("kiro_crew.mcp_core.KnowledgeStore")
    @patch("kiro_crew.mcp_core.create_embedder_from_config")
    def test_formatted_output(
        self, mock_embedder, mock_store_cls, mock_retriever_cls, mock_config_dir, mock_db_exists
    ):
        mock_config_dir.return_value = mock_db_exists
        mock_embedder.return_value = None
        mock_retriever_cls.return_value.search.return_value = [
            {
                "title": "Auth Design",
                "content": "JWT tokens with 15min expiry.",
                "score": 0.035,
                "source": "src-1",
                "source_type": "upload",
                "source_name": "design-docs/auth.md",
                "source_uri": "/uploads/design-docs/auth.md",
            }
        ]

        from kiro_crew.mcp_core import _call_tool_inner

        result = _call_tool_inner("local_knowledge_search", {"query": "auth"})

        assert "Auth Design" in result
        assert "design-docs/auth.md" in result
        assert "[upload]" in result
        assert "JWT tokens" in result
        assert "Knowledge Library" in result
        # No score or relevance metadata exposed
        assert "0.035" not in result

    @patch("kiro_crew.mcp_core.config_dir")
    @patch("kiro_crew.mcp_core.HybridRetriever")
    @patch("kiro_crew.mcp_core.KnowledgeStore")
    @patch("kiro_crew.mcp_core.create_embedder_from_config")
    def test_default_limit_is_3(
        self, mock_embedder, mock_store_cls, mock_retriever_cls, mock_config_dir, mock_db_exists
    ):
        mock_config_dir.return_value = mock_db_exists
        mock_embedder.return_value = None
        mock_retriever_cls.return_value.search.return_value = []

        from kiro_crew.mcp_core import _call_tool_inner

        _call_tool_inner("local_knowledge_search", {"query": "test"})
        mock_retriever_cls.return_value.search.assert_called_once_with("test", limit=3)

    @patch("kiro_crew.mcp_core.config_dir")
    @patch("kiro_crew.mcp_core.HybridRetriever")
    @patch("kiro_crew.mcp_core.KnowledgeStore")
    @patch("kiro_crew.mcp_core.create_embedder_from_config")
    def test_citation_with_section_and_uri(
        self, mock_embedder, mock_store_cls, mock_retriever_cls, mock_config_dir, mock_db_exists
    ):
        # A folder result surfaces [source_type] name + section + line range,
        # and the per-file path as the File locator (not the folder root).
        mock_config_dir.return_value = mock_db_exists
        mock_embedder.return_value = None
        mock_retriever_cls.return_value.search.return_value = [
            {
                "title": "Auth Design",
                "content": "JWT tokens with 15min expiry.",
                "score": 0.035,
                "source": "src-1",
                "source_type": "local_folder",
                "source_name": "Opportunity Planner",
                "source_uri": "/home/nrb/projects/op/src/",
                "section_title": "Token Lifecycle",
                "chunk_range": "10-25",
                "file_path": "/home/nrb/projects/op/src/auth.md",
            }
        ]

        from kiro_crew.mcp_core import _call_tool_inner

        result = _call_tool_inner("local_knowledge_search", {"query": "auth"})

        assert "[local_folder] Opportunity Planner" in result
        assert "Token Lifecycle" in result
        assert "lines 10-25" in result
        assert "**File:** /home/nrb/projects/op/src/auth.md" in result
        # A specific file path supersedes the folder-root uri Link.
        assert "**Link:**" not in result

    @patch("kiro_crew.mcp_core.config_dir")
    @patch("kiro_crew.mcp_core.HybridRetriever")
    @patch("kiro_crew.mcp_core.KnowledgeStore")
    @patch("kiro_crew.mcp_core.create_embedder_from_config")
    def test_artifact_citation_uses_slug(
        self, mock_embedder, mock_store_cls, mock_retriever_cls, mock_config_dir, mock_db_exists
    ):
        # An artifact result names the artifact and surfaces its slug (for a
        # /artifacts/<slug> deep link) rather than the aggregate source name.
        mock_config_dir.return_value = mock_db_exists
        mock_embedder.return_value = None
        mock_retriever_cls.return_value.search.return_value = [
            {
                "title": "OP Vision",
                "content": "vision content",
                "score": 0.05,
                "source": "art-src",
                "source_type": "artifact",
                "source_name": "Artifacts",
                "source_uri": "artifact://aggregate",
                "artifact_slug": "op-vision",
                "artifact_name": "OP Vision Plan",
            }
        ]

        from kiro_crew.mcp_core import _call_tool_inner

        result = _call_tool_inner("local_knowledge_search", {"query": "vision"})

        # Artifact name sits in the name slot (before the section), mirroring the
        # folder citation; the slug is the standalone locator line.
        assert "[artifact] OP Vision Plan" in result
        assert "**Artifact:** op-vision" in result
        assert "(slug:" not in result
        assert "Artifacts \u2014" not in result
        assert "artifact://aggregate" not in result

    @patch("kiro_crew.mcp_core.config_dir")
    @patch("kiro_crew.mcp_core.HybridRetriever")
    @patch("kiro_crew.mcp_core.KnowledgeStore")
    @patch("kiro_crew.mcp_core.create_embedder_from_config")
    def test_internal_uri_survives_redaction(
        self, mock_embedder, mock_store_cls, mock_retriever_cls, mock_config_dir, mock_db_exists
    ):
        # Citations must survive redact_exfiltration_urls/redact_credentials --
        # ordinary internal links (wiki/quip/folder paths) are not exfil blobs.
        mock_config_dir.return_value = mock_db_exists
        mock_embedder.return_value = None
        mock_retriever_cls.return_value.search.return_value = [
            {
                "title": "Plan",
                "content": "body",
                "score": 0.05,
                "source": "s1",
                "source_type": "quip",
                "source_name": "OP Vision",
                "source_uri": "https://quip-amazon.com/AbCdEfGhIjKl",
            }
        ]

        from kiro_crew.mcp_core import _call_tool_inner

        result = _call_tool_inner("local_knowledge_search", {"query": "vision"})

        assert "https://quip-amazon.com/AbCdEfGhIjKl" in result
        assert "[REDACTED_URL]" not in result


class TestKnowledgeSearchToolDefinition:
    def test_tool_listed(self):
        from kiro_crew.mcp_core import _list_tools

        tools = _list_tools()
        names = [t["name"] for t in tools]
        assert "local_knowledge_search" in names

    def test_tool_has_required_query(self):
        from kiro_crew.mcp_core import _list_tools

        tools = _list_tools()
        tool = next(t for t in tools if t["name"] == "local_knowledge_search")
        assert "query" in tool["inputSchema"]["required"]
