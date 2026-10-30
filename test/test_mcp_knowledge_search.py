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
    @pytest.mark.parametrize("query", ["", "   "], ids=["empty", "whitespace"])
    def test_blank_query_raises_validation_error(self, query):
        from kiro_crew.mcp_core import _call_tool_inner

        with pytest.raises(ValidationError):
            _call_tool_inner("local_knowledge_search", {"query": query})

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

        result = _call_tool_inner("local_knowledge_search", {"query": "test"})
        mock_retriever_cls.return_value.search.assert_called_once_with(
            "test", limit=3, source_id=None, namespace=None
        )
        assert "No relevant knowledge found" in result

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
    @pytest.mark.parametrize("name", ["local_knowledge_search", "knowledge_list_sources"])
    def test_tool_listed(self, name):
        from kiro_crew.mcp_core import _list_tools

        assert name in [t["name"] for t in _list_tools()]

    def test_tool_has_required_query(self):
        from kiro_crew.mcp_core import _list_tools

        tools = _list_tools()
        tool = next(t for t in tools if t["name"] == "local_knowledge_search")
        assert "query" in tool["inputSchema"]["required"]

    @pytest.mark.parametrize("field", ["source_id", "namespace"])
    def test_filter_field_is_optional(self, field):
        from kiro_crew.mcp_core import _list_tools

        tools = _list_tools()
        tool = next(t for t in tools if t["name"] == "local_knowledge_search")
        assert field in tool["inputSchema"]["properties"]
        assert field not in tool["inputSchema"]["required"]


class TestKnowledgeSearchSourceFilter:
    @pytest.mark.parametrize("field", ["source_id", "namespace"])
    def test_filter_rejects_non_string(self, field):
        from kiro_crew.mcp_core import _validate_args

        # At the MCP entry point: a genuinely wrong type (list) on a string
        # field is still rejected, while a number is defensively converted to
        # its string form (the re-typed-numeric-string repair).
        with pytest.raises(ValidationError):
            _validate_args("local_knowledge_search", {"query": "q", field: [7]})
        coerced = _validate_args("local_knowledge_search", {"query": "q", field: 7})
        assert coerced[field] == "7"

    @pytest.mark.parametrize("field", ["source_id", "namespace"])
    def test_filter_rejects_overlong_value(self, field):
        from kiro_crew.mcp_core import _call_tool_inner

        with pytest.raises(ValidationError):
            _call_tool_inner("local_knowledge_search", {"query": "q", field: "x" * 65})

    # The store mock answers the source_id existence probe; a namespace needs no
    # such probe (it does not have to exist as a source) and flows straight through.
    @pytest.mark.parametrize(
        "extra,expected",
        [
            ({"source_id": "src-1"}, {"source_id": "src-1", "namespace": None}),
            ({"namespace": "client-a"}, {"source_id": None, "namespace": "client-a"}),
        ],
        ids=["source_id", "namespace"],
    )
    @patch("kiro_crew.mcp_core.config_dir")
    @patch("kiro_crew.mcp_core.HybridRetriever")
    @patch("kiro_crew.mcp_core._get_knowledge_search")
    def test_filter_passed_through_to_retriever(
        self, mock_get_search, mock_retriever_cls, mock_config_dir, mock_db_exists, extra, expected
    ):
        mock_config_dir.return_value = mock_db_exists
        mock_store = MagicMock()
        mock_store.db.execute.return_value.fetchone.return_value = (1,)
        mock_get_search.return_value = (mock_store, None)
        mock_retriever_cls.return_value.search.return_value = []

        from kiro_crew.mcp_core import _call_tool_inner

        result = _call_tool_inner("local_knowledge_search", {"query": "auth", **extra})
        mock_retriever_cls.return_value.search.assert_called_once_with("auth", limit=3, **expected)
        assert "No relevant knowledge found" in result

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


# The correlated reference shape: IN (SELECT ...) inside the JOIN. It is the
# answer the linear query must agree with, and the shape the plan guard must
# recognise.
_CORRELATED_COUNTS_SQL = (
    "SELECT s.id, s.name, COUNT(DISTINCT i.id) AS item_count "
    "FROM sources s LEFT JOIN items i ON i.status = 'active' AND ("
    "  i.source_id = s.id"
    "  OR i.id IN (SELECT sl.item_id FROM source_locations sl WHERE sl.source_id = s.id)"
    ") GROUP BY s.id, s.name ORDER BY s.name"
)


class TestKnowledgeListSourcesCountQuery:
    """The per-source count is linear and matches the correlated answer."""

    @staticmethod
    def _seed(path, seed: int):
        import random

        from kiro_crew.knowledge.store import KnowledgeStore

        rng = random.Random(seed)
        store = KnowledgeStore(str(path))
        sources = [
            store.add_source(f"S{n}", "local_folder", f"/tmp/s{seed}-{n}")
            for n in range(rng.randint(1, 5))
        ]
        items = []
        for n in range(rng.randint(0, 25)):
            owner = rng.choice(sources + [None])
            items.append(store.add_item(f"I{n}", f"body {seed} {n}", "note", source_id=owner))
        for item in items:
            if rng.random() < 0.3:
                store.db.execute("UPDATE items SET status = 'deleted' WHERE id = ?", (item,))
            for src in sources:
                if rng.random() < 0.3:
                    store.add_source_location(item, src)
        store.db.commit()
        return store

    def test_item_owned_and_located_in_same_source_counts_once(self, tmp_path):
        from kiro_crew.knowledge.store import KnowledgeStore
        from kiro_crew.mcp_tools.knowledge import _SOURCE_COUNTS_SQL

        store = KnowledgeStore(str(tmp_path / "k.db"))
        try:
            src = store.add_source("Only", "local_folder", "/tmp/only")
            item = store.add_item("Doc", "content", "note", source_id=src)
            store.add_source_location(item, src)
            rows = [tuple(r) for r in store.db.execute(_SOURCE_COUNTS_SQL)]
        finally:
            store.close()
        assert rows == [(src, "Only", 1)]

    @pytest.mark.parametrize("seed", range(12))
    def test_matches_correlated_query_on_random_libraries(self, tmp_path, seed):
        from kiro_crew.mcp_tools.knowledge import _SOURCE_COUNTS_SQL

        store = self._seed(tmp_path / "k.db", seed)
        try:
            new = [tuple(r) for r in store.db.execute(_SOURCE_COUNTS_SQL)]
            old = [tuple(r) for r in store.db.execute(_CORRELATED_COUNTS_SQL)]
        finally:
            store.close()
        assert new == old

    def test_query_plan_has_no_correlated_subquery(self, tmp_path):
        from kiro_crew.mcp_tools.knowledge import _SOURCE_COUNTS_SQL

        store = self._seed(tmp_path / "k.db", 0)
        try:

            def plan(sql: str) -> str:
                return " | ".join(str(r[-1]) for r in store.db.execute(f"EXPLAIN QUERY PLAN {sql}"))

            # Probe: this SQLite reports the old shape as correlated, so the
            # guard below can see the regression it is meant to catch.
            assert "CORRELATED" in plan(_CORRELATED_COUNTS_SQL)
            assert "CORRELATED" not in plan(_SOURCE_COUNTS_SQL)
        finally:
            store.close()
