"""Tests for the memory graph API endpoint."""

from __future__ import annotations

import importlib
import json
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.state import DashboardState


def _get_handler():
    """Import api_memory_graph from handlers (works after brazil-build)."""
    mod = importlib.import_module("kiro_crew.dashboard.handlers")
    return mod.api_memory_graph


# ---------------------------------------------------------------------------
# Unit tests — pure logic, no HTTP
# ---------------------------------------------------------------------------


class TestMemoryGraphNodeExtraction:
    """Unit tests for node extraction from different memory sources."""

    def _make_state(self, tmp_path, prefs="", projects="", history=""):
        """Create a minimal DashboardState with mocked memory."""
        mem = MagicMock()
        mem.read_preferences.return_value = prefs
        mem.read_projects.return_value = projects
        mem.read_recent_history.return_value = history
        mem.vector_store = None

        cb = MagicMock()
        cb.memory = mem

        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            context_builder=cb,
        )
        return state

    @pytest.mark.asyncio
    async def test_empty_memory_returns_empty_graph(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._make_state(tmp_path)
        request = MagicMock()
        request.app = {"state": state}

        resp = await _get_handler()(request)
        data = json.loads(resp.body)

        assert data["nodes"] == []
        assert data["edges"] == []

    @pytest.mark.asyncio
    async def test_preferences_become_nodes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._make_state(tmp_path, prefs="- Prefers dark mode\n- Uses vim keybindings")
        request = MagicMock()
        request.app = {"state": state}

        resp = await _get_handler()(request)
        data = json.loads(resp.body)

        pref_nodes = [n for n in data["nodes"] if n["group"] == "preference"]
        assert len(pref_nodes) == 2
        assert any("dark mode" in n["title"] for n in pref_nodes)
        assert any("vim" in n["title"] for n in pref_nodes)

    @pytest.mark.asyncio
    async def test_short_preferences_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._make_state(tmp_path, prefs="- OK\n- Yes\n- Prefers concise output")
        request = MagicMock()
        request.app = {"state": state}

        resp = await _get_handler()(request)
        data = json.loads(resp.body)

        pref_nodes = [n for n in data["nodes"] if n["group"] == "preference"]
        assert len(pref_nodes) == 1  # Only "Prefers concise output" (>5 chars)

    @pytest.mark.asyncio
    async def test_comments_and_headings_skipped_in_prefs(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._make_state(
            tmp_path,
            prefs="# User Preferences\n<!-- comment -->\n- Prefers dark mode",
        )
        request = MagicMock()
        request.app = {"state": state}

        resp = await _get_handler()(request)
        data = json.loads(resp.body)

        pref_nodes = [n for n in data["nodes"] if n["group"] == "preference"]
        assert len(pref_nodes) == 1

    @pytest.mark.asyncio
    async def test_projects_create_parent_and_detail_nodes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._make_state(
            tmp_path,
            projects="## KiroCrew\n- Repository: ssh://git.example.com\n- Branch: main",
        )
        request = MagicMock()
        request.app = {"state": state}

        resp = await _get_handler()(request)
        data = json.loads(resp.body)

        proj_nodes = [n for n in data["nodes"] if n["group"] == "project"]
        assert len(proj_nodes) == 3  # parent + 2 details
        assert any(n["label"] == "KiroCrew" for n in proj_nodes)
        # Detail nodes should have edges to parent
        assert len(data["edges"]) == 2

    @pytest.mark.asyncio
    async def test_history_headings_become_nodes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._make_state(
            tmp_path,
            history="# 2026-03-25\n#### 06:47 UTC\n[2026-03-25 01:38] Did some work on the feature",
        )
        request = MagicMock()
        request.app = {"state": state}

        resp = await _get_handler()(request)
        data = json.loads(resp.body)

        hist_nodes = [n for n in data["nodes"] if n["group"] == "history"]
        assert len(hist_nodes) >= 2  # heading + bracketed entry

    @pytest.mark.asyncio
    async def test_lessons_become_nodes(self, tmp_path, monkeypatch):
        from kiro_crew.learn import Lesson

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        lessons = [
            Lesson(rule="Always check for existing CRs", category="tool", ts="2026-03-25"),
            Lesson(
                rule="Use brazil-build release for tests", category="knowledge", ts="2026-03-25"
            ),
        ]
        state = self._make_state(tmp_path)
        state.lessons.load_all.return_value = lessons
        request = MagicMock()
        request.app = {"state": state}

        resp = await _get_handler()(request)
        data = json.loads(resp.body)

        lesson_nodes = [n for n in data["nodes"] if n["group"] == "lesson"]
        assert len(lesson_nodes) == 2

    @pytest.mark.asyncio
    async def test_semantic_memory_becomes_nodes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._make_state(tmp_path)
        vs = MagicMock()
        vs.get_all_semantic.return_value = [
            {"key": "pref.editor", "value_json": '"vim"'},
            {"key": "project.name", "value_json": '"KiroCrew"'},
        ]
        vs.get_lessons.return_value = []
        state.context_builder.memory.vector_store = vs
        request = MagicMock()
        request.app = {"state": state}

        resp = await _get_handler()(request)
        data = json.loads(resp.body)

        sem_nodes = [n for n in data["nodes"] if n["group"] == "semantic"]
        assert len(sem_nodes) == 2
        assert any("pref.editor" in n["label"] for n in sem_nodes)

    @pytest.mark.asyncio
    async def test_semantic_raw_string_value_json_does_not_crash(self, tmp_path, monkeypatch):
        """value_json can contain raw strings (URLs, plain text) that are not
        valid JSON. The handler must not crash — it should treat them as-is."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._make_state(tmp_path)
        vs = MagicMock()
        vs.get_all_semantic.return_value = [
            {"key": "pref.editor", "value_json": '"vim"'},
            {"key": "project.url", "value_json": "https://tracker.example.com/tasks/123"},
            {"key": "project.repo", "value_json": "ssh://git.example.com/pkg/Foo"},
        ]
        vs.get_lessons.return_value = []
        state.context_builder.memory.vector_store = vs
        request = MagicMock()
        request.app = {"state": state}

        resp = await _get_handler()(request)
        data = json.loads(resp.body)

        sem_nodes = [n for n in data["nodes"] if n["group"] == "semantic"]
        assert len(sem_nodes) == 3
        # Raw URL should appear in the title as-is, not cause a crash
        url_node = [n for n in sem_nodes if "project.url" in n["label"]]
        assert len(url_node) == 1
        assert "https://tracker" in url_node[0]["title"]

    @pytest.mark.asyncio
    async def test_graph_redacts_credentials_in_semantic_nodes(self, tmp_path, monkeypatch):
        """Memory graph endpoint must redact credentials in node labels/titles."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._make_state(tmp_path)
        vs = MagicMock()
        vs.get_all_semantic.return_value = [
            {"key": "secret.key", "value_json": "AKIAIOSFODNN7EXAMPLE"},
        ]
        vs.get_lessons.return_value = []
        state.context_builder.memory.vector_store = vs
        request = MagicMock()
        request.app = {"state": state}

        resp = await _get_handler()(request)
        data = json.loads(resp.body)

        sem_nodes = [n for n in data["nodes"] if n["group"] == "semantic"]
        assert len(sem_nodes) == 1
        assert "AKIAIOSFODNN7EXAMPLE" not in sem_nodes[0]["title"]


class TestMemoryEndpointRedaction:
    """Tests that memory API endpoints redact credentials and exfiltration URLs."""

    @pytest.mark.asyncio
    async def test_semantic_redacts_credential_in_key(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mod = importlib.import_module("kiro_crew.dashboard.handlers")
        vs = MagicMock()
        vs.get_all_semantic.return_value = [
            {"key": "AKIAIOSFODNN7EXAMPLE", "value_json": "ok"},
        ]
        state = MagicMock()
        state.context_builder.memory.vector_store = vs
        req = MagicMock()
        req.app = {"state": state}
        resp = await mod.api_memory_semantic(req)
        data = json.loads(resp.body)
        assert "AKIAIOSFODNN7EXAMPLE" not in data["entries"][0]["key"]

    @pytest.mark.asyncio
    async def test_semantic_redacts_credential_in_value_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mod = importlib.import_module("kiro_crew.dashboard.handlers")
        vs = MagicMock()
        vs.get_all_semantic.return_value = [
            {"key": "k1", "value_json": "AKIAIOSFODNN7EXAMPLE"},
        ]
        state = MagicMock()
        state.context_builder.memory.vector_store = vs
        req = MagicMock()
        req.app = {"state": state}
        resp = await mod.api_memory_semantic(req)
        data = json.loads(resp.body)
        assert "AKIAIOSFODNN7EXAMPLE" not in data["entries"][0]["value_json"]

    @pytest.mark.asyncio
    async def test_semantic_redacts_tags_as_list(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mod = importlib.import_module("kiro_crew.dashboard.handlers")
        vs = MagicMock()
        vs.get_all_semantic.return_value = [
            {"key": "k1", "value_json": "ok", "tags": ["safe", "AKIAIOSFODNN7EXAMPLE"]},
        ]
        state = MagicMock()
        state.context_builder.memory.vector_store = vs
        req = MagicMock()
        req.app = {"state": state}
        resp = await mod.api_memory_semantic(req)
        data = json.loads(resp.body)
        tags = data["entries"][0]["tags"]
        assert "safe" in tags[0]
        assert "AKIAIOSFODNN7EXAMPLE" not in tags[1]

    @pytest.mark.asyncio
    async def test_episodic_search_redacts_text(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mod = importlib.import_module("kiro_crew.dashboard.handlers")
        vs = MagicMock()
        vs.embed_fn = None
        vs.search_episodic.return_value = [
            {"id": "1", "text": "secret AKIAIOSFODNN7EXAMPLE here", "tags": "t"},
        ]
        state = MagicMock()
        state.context_builder.memory.vector_store = vs
        req = MagicMock()
        req.app = {"state": state}
        req.query = {"q": "test", "limit": "10", "tags": ""}
        resp = await mod.api_memory_episodic_search(req)
        data = json.loads(resp.body)
        assert "AKIAIOSFODNN7EXAMPLE" not in data["results"][0]["text"]

    @pytest.mark.asyncio
    async def test_episodic_list_redacts_tags_list(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mod = importlib.import_module("kiro_crew.dashboard.handlers")
        vs = MagicMock()
        vs.get_episodic_list.return_value = [
            {"id": "1", "text": "ok", "tags": ["AKIAIOSFODNN7EXAMPLE"]},
        ]
        state = MagicMock()
        state.context_builder.memory.vector_store = vs
        req = MagicMock()
        req.app = {"state": state}
        req.query = {"limit": "10", "offset": "0", "tags": ""}
        resp = await mod.api_memory_episodic_list(req)
        data = json.loads(resp.body)
        assert "AKIAIOSFODNN7EXAMPLE" not in data["entries"][0]["tags"][0]

    @pytest.mark.asyncio
    async def test_semantic_redacts_all_string_fields(self, tmp_path, monkeypatch):
        """Defense-in-depth: credentials in ANY string field are redacted."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mod = importlib.import_module("kiro_crew.dashboard.handlers")
        vs = MagicMock()
        vs.get_all_semantic.return_value = [
            {"key": "k1", "value_json": "ok", "source": "AKIAIOSFODNN7EXAMPLE"},
        ]
        state = MagicMock()
        state.context_builder.memory.vector_store = vs
        req = MagicMock()
        req.app = {"state": state}
        resp = await mod.api_memory_semantic(req)
        data = json.loads(resp.body)
        assert "AKIAIOSFODNN7EXAMPLE" not in data["entries"][0]["source"]

    @pytest.mark.asyncio
    async def test_episodic_search_redacts_all_string_fields(self, tmp_path, monkeypatch):
        """Defense-in-depth: credentials in ANY episodic field are redacted."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mod = importlib.import_module("kiro_crew.dashboard.handlers")
        vs = MagicMock()
        vs.embed_fn = None
        vs.search_episodic.return_value = [
            {"id": "1", "text": "ok", "conversation_id": "AKIAIOSFODNN7EXAMPLE"},
        ]
        state = MagicMock()
        state.context_builder.memory.vector_store = vs
        req = MagicMock()
        req.app = {"state": state}
        req.query = {"q": "test", "limit": "10", "tags": ""}
        resp = await mod.api_memory_episodic_search(req)
        data = json.loads(resp.body)
        assert "AKIAIOSFODNN7EXAMPLE" not in data["results"][0]["conversation_id"]

    @pytest.mark.asyncio
    async def test_episodic_list_redacts_all_string_fields(self, tmp_path, monkeypatch):
        """Defense-in-depth: credentials in ANY episodic field are redacted."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mod = importlib.import_module("kiro_crew.dashboard.handlers")
        vs = MagicMock()
        vs.get_episodic_list.return_value = [
            {"id": "1", "text": "ok", "conversation_id": "AKIAIOSFODNN7EXAMPLE"},
        ]
        state = MagicMock()
        state.context_builder.memory.vector_store = vs
        req = MagicMock()
        req.app = {"state": state}
        req.query = {"limit": "10", "offset": "0", "tags": ""}
        resp = await mod.api_memory_episodic_list(req)
        data = json.loads(resp.body)
        assert "AKIAIOSFODNN7EXAMPLE" not in data["entries"][0]["conversation_id"]

    @pytest.mark.asyncio
    async def test_redaction_preserves_non_string_fields(self, tmp_path, monkeypatch):
        """Numeric and other non-string fields pass through unchanged."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mod = importlib.import_module("kiro_crew.dashboard.handlers")
        vs = MagicMock()
        vs.get_all_semantic.return_value = [
            {"key": "k1", "value_json": "v", "confidence": 0.95, "is_deleted": 0},
        ]
        state = MagicMock()
        state.context_builder.memory.vector_store = vs
        req = MagicMock()
        req.app = {"state": state}
        resp = await mod.api_memory_semantic(req)
        data = json.loads(resp.body)
        assert data["entries"][0]["confidence"] == 0.95
        assert data["entries"][0]["is_deleted"] == 0

    @pytest.mark.asyncio
    async def test_redaction_does_not_mutate_store_objects(self, tmp_path, monkeypatch):
        """Redaction must operate on copies, never mutating the store's records."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mod = importlib.import_module("kiro_crew.dashboard.handlers")
        original = {
            "key": "k1",
            "value_json": "AKIAIOSFODNN7EXAMPLE",
            "tags": ["AKIAIOSFODNN7EXAMPLE"],
        }
        vs = MagicMock()
        vs.get_all_semantic.return_value = [original]
        state = MagicMock()
        state.context_builder.memory.vector_store = vs
        req = MagicMock()
        req.app = {"state": state}
        await mod.api_memory_semantic(req)
        # Original dict must be untouched
        assert original["value_json"] == "AKIAIOSFODNN7EXAMPLE"
        assert original["tags"] == ["AKIAIOSFODNN7EXAMPLE"]


class TestMemoryGraphEdgeDetection:
    """Unit tests for automatic edge detection between nodes."""

    def _make_state(self, tmp_path, prefs="", projects=""):
        mem = MagicMock()
        mem.read_preferences.return_value = prefs
        mem.read_projects.return_value = projects
        mem.read_recent_history.return_value = ""
        mem.vector_store = None

        cb = MagicMock()
        cb.memory = mem

        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            context_builder=cb,
        )
        return state

    @pytest.mark.asyncio
    async def test_preference_referencing_project_creates_edge(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._make_state(
            tmp_path,
            prefs="- Uses KiroCrew for automation",
            projects="## KiroCrew\n- Local path: /home/user/kirocrew",
        )
        request = MagicMock()
        request.app = {"state": state}

        resp = await _get_handler()(request)
        data = json.loads(resp.body)

        # Should have: project parent edge to detail + pref->project edge
        cross_edges = [
            e
            for e in data["edges"]
            if any(n["group"] == "preference" and n["id"] == e["from"] for n in data["nodes"])
        ]
        assert len(cross_edges) >= 1

    @pytest.mark.asyncio
    async def test_no_self_edges(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._make_state(
            tmp_path,
            prefs="- Uses TestProject for everything",
            projects="## TestProject\n- Some detail",
        )
        request = MagicMock()
        request.app = {"state": state}

        resp = await _get_handler()(request)
        data = json.loads(resp.body)

        for edge in data["edges"]:
            assert edge["from"] != edge["to"], "Self-edges should not exist"


class TestMemoryGraphResponseFormat:
    """Tests for the API response structure."""

    @pytest.mark.asyncio
    async def test_response_has_required_fields(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mem = MagicMock()
        mem.read_preferences.return_value = "- Test preference value"
        mem.read_projects.return_value = ""
        mem.read_recent_history.return_value = ""
        mem.vector_store = None
        cb = MagicMock()
        cb.memory = mem
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            context_builder=cb,
        )
        request = MagicMock()
        request.app = {"state": state}

        resp = await _get_handler()(request)
        data = json.loads(resp.body)

        assert "nodes" in data
        assert "edges" in data
        assert isinstance(data["nodes"], list)
        assert isinstance(data["edges"], list)

        # Each node has required fields
        for node in data["nodes"]:
            assert "id" in node
            assert "label" in node
            assert "group" in node
            assert "title" in node

    @pytest.mark.asyncio
    async def test_node_labels_truncated_at_60(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        long_pref = "- " + "A" * 200
        mem = MagicMock()
        mem.read_preferences.return_value = long_pref
        mem.read_projects.return_value = ""
        mem.read_recent_history.return_value = ""
        mem.vector_store = None
        cb = MagicMock()
        cb.memory = mem
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            context_builder=cb,
        )
        request = MagicMock()
        request.app = {"state": state}

        resp = await _get_handler()(request)
        data = json.loads(resp.body)

        for node in data["nodes"]:
            assert len(node["label"]) <= 60

    @pytest.mark.asyncio
    async def test_node_ids_are_deterministic(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mem = MagicMock()
        mem.read_preferences.return_value = "- Prefers dark mode"
        mem.read_projects.return_value = ""
        mem.read_recent_history.return_value = ""
        mem.vector_store = None
        cb = MagicMock()
        cb.memory = mem
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            context_builder=cb,
        )
        request = MagicMock()
        request.app = {"state": state}

        resp1 = await _get_handler()(request)
        resp2 = await _get_handler()(request)
        data1 = json.loads(resp1.body)
        data2 = json.loads(resp2.body)

        assert data1["nodes"][0]["id"] == data2["nodes"][0]["id"]

    @pytest.mark.asyncio
    async def test_no_duplicate_nodes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mem = MagicMock()
        mem.read_preferences.return_value = "- Same pref\n- Same pref"
        mem.read_projects.return_value = ""
        mem.read_recent_history.return_value = ""
        mem.vector_store = None
        cb = MagicMock()
        cb.memory = mem
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            context_builder=cb,
        )
        request = MagicMock()
        request.app = {"state": state}

        resp = await _get_handler()(request)
        data = json.loads(resp.body)

        ids = [n["id"] for n in data["nodes"]]
        assert len(ids) == len(set(ids)), "Node IDs must be unique"


class TestMemoryGraphErrorHandling:
    """Tests for graceful error handling."""

    @pytest.mark.asyncio
    async def test_vector_store_error_doesnt_crash(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mem = MagicMock()
        mem.read_preferences.return_value = ""
        mem.read_projects.return_value = ""
        mem.read_recent_history.return_value = ""
        vs = MagicMock()
        vs.get_all_semantic.side_effect = Exception("DB error")
        vs.get_lessons.side_effect = Exception("DB error")
        mem.vector_store = vs
        cb = MagicMock()
        cb.memory = mem
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            context_builder=cb,
        )
        request = MagicMock()
        request.app = {"state": state}

        resp = await _get_handler()(request)
        data = json.loads(resp.body)

        # Should still return valid response, just without vector data
        assert "nodes" in data
        assert "edges" in data

    @pytest.mark.asyncio
    async def test_vector_store_error_falls_back_to_file_lessons(self, tmp_path, monkeypatch):
        from kiro_crew.learn import Lesson

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mem = MagicMock()
        mem.read_preferences.return_value = ""
        mem.read_projects.return_value = ""
        mem.read_recent_history.return_value = ""
        vs = MagicMock()
        vs.get_all_semantic.side_effect = Exception("DB error")
        vs.get_lessons.side_effect = Exception("DB error")
        mem.vector_store = vs
        cb = MagicMock()
        cb.memory = mem
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(
                load_all=MagicMock(
                    return_value=[
                        Lesson(rule="Fallback lesson rule here", category="tool", ts="2026-01-01"),
                    ]
                )
            ),
            start_time=0.0,
            context_builder=cb,
        )
        request = MagicMock()
        request.app = {"state": state}

        resp = await _get_handler()(request)
        data = json.loads(resp.body)

        lesson_nodes = [n for n in data["nodes"] if n["group"] == "lesson"]
        assert (
            len(lesson_nodes) == 1
        ), "File-based lessons should still load when vector store errors"


# ---------------------------------------------------------------------------
# Integration tests — real HTTP server via aiohttp TestClient
# ---------------------------------------------------------------------------


class TestMemoryGraphHTTPIntegration:
    """Integration tests hitting the actual /api/memory/graph endpoint."""

    def _make_state(self, tmp_path, prefs="", projects="", history=""):
        mem = MagicMock()
        mem.read_preferences.return_value = prefs
        mem.read_projects.return_value = projects
        mem.read_recent_history.return_value = history
        mem.vector_store = None
        cb = MagicMock()
        cb.memory = mem
        return DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            context_builder=cb,
        )

    def _make_app(self, state):
        from aiohttp import web as _web

        handler = _get_handler()
        app = _web.Application()
        app["state"] = state
        app.router.add_get("/api/memory/graph", handler)
        return app

    @pytest.mark.asyncio
    async def test_endpoint_returns_200_with_json(self, tmp_path, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._make_state(tmp_path, prefs="- Prefers dark mode")
        async with TestClient(TestServer(self._make_app(state))) as client:
            resp = await client.get("/api/memory/graph")
            assert resp.status == 200
            data = await resp.json()
            assert "nodes" in data
            assert "edges" in data
            assert len(data["nodes"]) > 0

    @pytest.mark.asyncio
    async def test_endpoint_returns_empty_graph(self, tmp_path, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._make_state(tmp_path)
        async with TestClient(TestServer(self._make_app(state))) as client:
            resp = await client.get("/api/memory/graph")
            assert resp.status == 200
            data = await resp.json()
            assert data["nodes"] == []
            assert data["edges"] == []

    @pytest.mark.asyncio
    async def test_endpoint_with_all_memory_types(self, tmp_path, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.learn import Lesson

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._make_state(
            tmp_path,
            prefs="- Prefers dark mode",
            projects="## KiroCrew\n- Local path: /home/user/mc",
            history="# 2026-03-25\n[2026-03-25 10:00] Did some work",
        )
        state.lessons.load_all.return_value = [
            Lesson(rule="Always check CRs first", category="tool", ts="2026-03-25"),
        ]
        async with TestClient(TestServer(self._make_app(state))) as client:
            resp = await client.get("/api/memory/graph")
            assert resp.status == 200
            data = await resp.json()
            groups = {n["group"] for n in data["nodes"]}
            assert "preference" in groups
            assert "project" in groups
            assert "history" in groups
            assert "lesson" in groups
            assert len(data["edges"]) > 0  # project parent->detail edges at minimum
