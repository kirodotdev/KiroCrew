"""Tests for local_knowledge_search tool in mcp_core."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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
        mock_retriever_cls.return_value.search.assert_called_once_with(
            "test", limit=3, source_id=None
        )

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
                "source_uri": "/home/alice/projects/op/src/",
                "section_title": "Token Lifecycle",
                "chunk_range": "10-25",
                "file_path": "/home/alice/projects/op/src/auth.md",
            }
        ]

        from kiro_crew.mcp_core import _call_tool_inner

        result = _call_tool_inner("local_knowledge_search", {"query": "auth"})

        assert "[local_folder] Opportunity Planner" in result
        assert "Token Lifecycle" in result
        assert "lines 10-25" in result
        assert "**File:** /home/alice/projects/op/src/auth.md" in result
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

    def test_source_id_is_optional(self):
        from kiro_crew.mcp_core import _list_tools

        tools = _list_tools()
        tool = next(t for t in tools if t["name"] == "local_knowledge_search")
        assert "source_id" in tool["inputSchema"]["properties"]
        assert "source_id" not in tool["inputSchema"]["required"]

    def test_list_sources_tool_listed(self):
        from kiro_crew.mcp_core import _list_tools

        tools = _list_tools()
        names = [t["name"] for t in tools]
        assert "knowledge_list_sources" in names


class TestKnowledgeSearchSourceFilter:
    def test_source_id_rejects_non_string(self):
        from kiro_crew.mcp_core import _call_tool_inner

        with pytest.raises(ValidationError):
            _call_tool_inner("local_knowledge_search", {"query": "q", "source_id": 7})

    def test_source_id_rejects_overlong_value(self):
        from kiro_crew.mcp_core import _call_tool_inner

        with pytest.raises(ValidationError):
            _call_tool_inner("local_knowledge_search", {"query": "q", "source_id": "x" * 65})

    @patch("kiro_crew.mcp_core.config_dir")
    @patch("kiro_crew.mcp_core.HybridRetriever")
    @patch("kiro_crew.mcp_core._get_knowledge_search")
    def test_source_id_passed_through_to_retriever(
        self, mock_get_search, mock_retriever_cls, mock_config_dir, mock_db_exists
    ):
        mock_config_dir.return_value = mock_db_exists
        mock_store = MagicMock()
        mock_store.db.execute.return_value.fetchone.return_value = (1,)  # source exists
        mock_get_search.return_value = (mock_store, None)
        mock_retriever_cls.return_value.search.return_value = []

        from kiro_crew.mcp_core import _call_tool_inner

        _call_tool_inner("local_knowledge_search", {"query": "auth", "source_id": "src-1"})
        mock_retriever_cls.return_value.search.assert_called_once_with(
            "auth", limit=3, source_id="src-1"
        )

    @patch("kiro_crew.mcp_core.config_dir")
    @patch("kiro_crew.mcp_core._get_knowledge_search")
    def test_unknown_source_id_names_discovery_tool(
        self, mock_get_search, mock_config_dir, mock_db_exists
    ):
        # A nonexistent source id gets guidance, not an exception and not a
        # silent empty search.
        mock_config_dir.return_value = mock_db_exists
        mock_store = MagicMock()
        mock_store.db.execute.return_value.fetchone.return_value = None
        mock_get_search.return_value = (mock_store, None)

        from kiro_crew.mcp_core import _call_tool_inner

        result = _call_tool_inner(
            "local_knowledge_search", {"query": "auth", "source_id": "no-such-id"}
        )
        assert "knowledge_list_sources" in result
        assert "no-such-id" in result

    @patch("kiro_crew.mcp_core.sel")
    @patch("kiro_crew.mcp_core.config_dir")
    @patch("kiro_crew.mcp_core._get_knowledge_search")
    def test_unknown_source_audit_redacts_query(
        self, mock_get_search, mock_config_dir, mock_sel, mock_db_exists
    ):
        # SEL is persisted and dashboard-readable: a credential-bearing query
        # must be redacted before the unknown_source audit write.
        mock_config_dir.return_value = mock_db_exists
        mock_store = MagicMock()
        mock_store.db.execute.return_value.fetchone.return_value = None
        mock_get_search.return_value = (mock_store, None)

        from kiro_crew.mcp_core import _call_tool_inner

        _call_tool_inner(
            "local_knowledge_search",
            {"query": "key AKIAIOSFODNN7EXAMPLE leak", "source_id": "no-such-id"},
        )
        logged = mock_sel.return_value.log_tool_invocation.call_args.kwargs
        assert logged["outcome"] == "unknown_source"
        assert "AKIAIOSFODNN7EXAMPLE" not in logged["metadata"]["query"]


class TestKnowledgeListSources:
    @patch("kiro_crew.mcp_core.config_dir")
    def test_returns_not_configured_when_db_missing(self, mock_config_dir, tmp_path):
        mock_config_dir.return_value = tmp_path  # no knowledge.db here
        from kiro_crew.mcp_core import _call_tool_inner

        result = _call_tool_inner("knowledge_list_sources", {})
        assert "not configured" in result

    @patch("kiro_crew.mcp_core.config_dir")
    def test_lists_sources_with_active_item_counts(self, mock_config_dir, tmp_path):
        from kiro_crew.knowledge.store import KnowledgeStore

        db_dir = tmp_path / "workspace" / "knowledge"
        db_dir.mkdir(parents=True)
        store = KnowledgeStore(str(db_dir / "knowledge.db"))
        src_a = store.add_source("Design Docs", "local_folder", "/tmp/a")
        src_b = store.add_source("Runbooks", "local_folder", "/tmp/b")
        doc1 = store.add_item("Doc 1", "content", "note", source_id=src_a)
        store.add_item("Doc 2", "content two", "note", source_id=src_a)
        gone = store.add_item("Doc 3", "content three", "note", source_id=src_b)
        # Non-active items are outside what search can return, so they must not
        # inflate the advertised count.
        store.db.execute("UPDATE items SET status = 'deleted' WHERE id = ?", (gone,))
        store.db.commit()
        # A dedup survivor owned by A but located in B counts for B too —
        # membership matches the retriever's scoped seed queries.
        store.add_source_location(doc1, src_b)
        store.close()
        mock_config_dir.return_value = tmp_path

        from kiro_crew.mcp_core import _call_tool_inner

        result = _call_tool_inner("knowledge_list_sources", {})
        assert f"Design Docs — id: {src_a} (2 item(s))" in result
        assert f"Runbooks — id: {src_b} (1 item(s))" in result

    @patch("kiro_crew.mcp_core.config_dir")
    def test_empty_library_reports_no_sources(self, mock_config_dir, tmp_path):
        from kiro_crew.knowledge.store import KnowledgeStore

        db_dir = tmp_path / "workspace" / "knowledge"
        db_dir.mkdir(parents=True)
        KnowledgeStore(str(db_dir / "knowledge.db")).close()
        mock_config_dir.return_value = tmp_path

        from kiro_crew.mcp_core import _call_tool_inner

        result = _call_tool_inner("knowledge_list_sources", {})
        assert "no sources yet" in result

    def test_rejects_unknown_fields(self):
        from kiro_crew.mcp_core import _call_tool_inner

        with pytest.raises(ValidationError):
            _call_tool_inner("knowledge_list_sources", {"filter": "x"})
