"""Coverage tests for :mod:`kiro_crew.cli_commands`: learn, memory, artifact, telemetry, eval.

The knowledge-store subcommands (``learn``, ``memory``, ``artifact``), pod
dispatch, the telemetry beacon switches and the ``kirocrew eval`` runner. Same
conventions as ``test_cli_commands_coverage.py``: handlers are called directly
with an ``argparse.Namespace``, HTTP goes through a patched
``kiro_crew.cli_commands._gateway_send``, and every write lands in ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cli_commands_coverage_helpers import _FakeResponse, _http_error, _ns

from kiro_crew import cli_commands as cc
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.eval.scenario import AssertionType
from kiro_crew.vector_memory import LessonWriteOutcome, LessonWriteResult

# ── learn subcommands ──


class _LearnHarness:
    """Patches ``_learn``'s two stores and exposes the mocks."""

    def __init__(self) -> None:
        self.vs = MagicMock()
        self.jsonl = MagicMock()
        self._patches = [
            patch("kiro_crew.cli_commands.VectorMemoryStore", return_value=self.vs),
            patch("kiro_crew.cli_commands.LessonStore", return_value=self.jsonl),
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
        ]

    def __enter__(self) -> _LearnHarness:
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self._patches):
            p.stop()


class TestLearnCli:
    def test_add_prefers_vector_store(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _LearnHarness() as h:
            h.vs.write_lesson.return_value = LessonWriteResult(LessonWriteOutcome.INSERTED)
            cc._learn(_ns(learn_action="add", rule="do x", category="tool", negative="not y"))
        h.vs.write_lesson.assert_called_once_with("do x", "tool", "not y")
        h.jsonl.save.assert_not_called()
        h.vs.close.assert_called_once()
        assert "Saved: do x (not y) [tool]" in capsys.readouterr().out

    def test_add_does_not_write_jsonl_when_the_store_declines(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """This replaces a test that PINNED the defect.

        It asserted the JSONL fallback fires whenever the vector store returns a
        falsy value -- which is most often "the lesson is already stored exactly as
        submitted". So the old contract was: write a duplicate record for a lesson
        that was fine, and print "Saved:" when nothing needed saving. The declining
        outcomes are now distinguished, and none of them routes to the other store.
        """
        with _LearnHarness() as h:
            h.vs.write_lesson.return_value = LessonWriteResult(LessonWriteOutcome.UNCHANGED)
            cc._learn(_ns(learn_action="add", rule="do x", category="knowledge", negative=None))
        h.jsonl.save_or_enrich.assert_not_called()
        h.jsonl.save.assert_not_called()
        out = capsys.readouterr().out
        assert "Already stored, nothing written: do x" in out
        # The store keeps the stored category on a re-submit, so echoing the submitted
        # one would show a value it may not hold.
        assert "[knowledge]" not in out

    def test_add_refusal_exits_non_zero_without_writing_jsonl(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A refusal stored nothing anywhere, so the command must fail loudly.

        Routing it to the JSONL store was the sharper half of the defect: that store
        validates no content, so a value the vector store rejected landed there and
        the context builder reads it whenever the vector store holds no lessons.
        """
        with _LearnHarness() as h:
            h.vs.write_lesson.return_value = LessonWriteResult(
                LessonWriteOutcome.REFUSED, "injection_blocked"
            )
            with pytest.raises(SystemExit) as exc:
                cc._learn(_ns(learn_action="add", rule="do x", category="knowledge", negative=None))
        assert exc.value.code == 1
        h.jsonl.save_or_enrich.assert_not_called()
        assert "NOT saved" in capsys.readouterr().err

    def test_list_from_vector_store(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _LearnHarness() as h:
            h.vs.get_lessons.return_value = [{"value_json": json.dumps({"rule": "r1"})}]
            cc._learn(_ns(learn_action="list"))
        assert "[knowledge]" in capsys.readouterr().out

    def test_list_falls_back_to_jsonl(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _LearnHarness() as h:
            h.vs.get_lessons.return_value = []
            h.jsonl.load_all.return_value = [
                SimpleNamespace(category="tool", rule="r", negative="n", repo_scope=None)
            ]
            cc._learn(_ns(learn_action="list"))
        assert "[tool] r — n" in capsys.readouterr().out

    def test_list_jsonl_fallback_normalizes_blank_category(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The JSONL fallback branch applies the same display policy as the
        vector-store branch and the dashboard: a blank/legacy category renders
        the store's own "knowledge" default, never a bare []."""
        with _LearnHarness() as h:
            h.vs.get_lessons.return_value = []
            h.jsonl.load_all.return_value = [
                SimpleNamespace(category="", rule="legacy row", negative=None, repo_scope=None)
            ]
            cc._learn(_ns(learn_action="list"))
        out = capsys.readouterr().out
        assert "[knowledge] legacy row" in out
        assert "[]" not in out

    def test_list_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _LearnHarness() as h:
            h.vs.get_lessons.return_value = []
            h.jsonl.load_all.return_value = []
            cc._learn(_ns(learn_action="list"))
        assert "No lessons." in capsys.readouterr().out

    def test_remove_via_vector_store(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _LearnHarness() as h:
            h.vs.get_lessons.return_value = [{"value_json": "{}"}]
            h.vs.delete_lesson.return_value = True
            cc._learn(_ns(learn_action="remove", query="q"))
        assert "Removed lessons matching: q" in capsys.readouterr().out

    def test_remove_via_jsonl_store(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _LearnHarness() as h:
            h.vs.get_lessons.return_value = []
            h.jsonl.remove.return_value = True
            cc._learn(_ns(learn_action="remove", query="q"))
        assert "Removed lessons matching: q" in capsys.readouterr().out

    def test_remove_no_match(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _LearnHarness() as h:
            h.vs.get_lessons.return_value = []
            h.jsonl.remove.return_value = False
            cc._learn(_ns(learn_action="remove", query="q"))
        assert "No lessons match: q" in capsys.readouterr().out

    def test_remove_forwards_repo_scope_to_vector_store(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The scope selector must reach the store, or a scoped and a global
        # lesson sharing rule text cannot be deleted independently.
        with _LearnHarness() as h:
            h.vs.get_lessons.return_value = [{"value_json": "{}"}]
            h.vs.delete_lesson.return_value = True
            cc._learn(_ns(learn_action="remove", query="q", repo_scope="src/pkg"))
        h.vs.delete_lesson.assert_called_once_with("q", "src/pkg")
        h.jsonl.remove.assert_not_called()
        assert capsys.readouterr().out == "Removed lessons matching: q\n"

    def test_remove_forwards_repo_scope_to_jsonl_store(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with _LearnHarness() as h:
            h.vs.get_lessons.return_value = []
            h.jsonl.remove.return_value = True
            cc._learn(_ns(learn_action="remove", query="q", repo_scope="src/pkg"))
        h.jsonl.remove.assert_called_once_with("q", "src/pkg")
        h.vs.delete_lesson.assert_not_called()
        assert capsys.readouterr().out == "Removed lessons matching: q\n"

    def test_remove_without_the_flag_passes_none_scope(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Absent flag -> None -> the historical every-scope match, unchanged.
        with _LearnHarness() as h:
            h.vs.get_lessons.return_value = []
            h.jsonl.remove.return_value = True
            cc._learn(_ns(learn_action="remove", query="q"))
        h.jsonl.remove.assert_called_once_with("q", None)
        assert capsys.readouterr().out == "Removed lessons matching: q\n"

    def test_remove_refuses_a_selector_naming_no_usable_scope(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # "/" names nothing; "/src/pkg" is the absolute spelling the write
        # surface refuses, and canonical folding would land it on the stored
        # "src/pkg" rows. The command exits non-zero before either store is
        # consulted.
        with _LearnHarness() as h:
            with pytest.raises(SystemExit):
                cc._learn(_ns(learn_action="remove", query="q", repo_scope="/"))
            with pytest.raises(SystemExit):
                cc._learn(_ns(learn_action="remove", query="q", repo_scope="/src/pkg"))
            h.vs.delete_lesson.assert_not_called()
            h.jsonl.remove.assert_not_called()
        assert "does not name a usable scope" in capsys.readouterr().err

    def test_unknown_action_prints_usage_and_closes_store(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with _LearnHarness() as h:
            cc._learn(_ns(learn_action=None))
        h.vs.close.assert_called_once()
        assert "Usage: kirocrew learn" in capsys.readouterr().out


# ── memory subcommands ──


class _MemHarness:
    def __init__(self) -> None:
        self.store = MagicMock()
        self._patches = [
            patch("kiro_crew.cli_commands.declared_store", return_value=self.store),
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
        ]

    def __enter__(self) -> _MemHarness:
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self._patches):
            p.stop()


class TestMemoryCli:
    def test_list_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _MemHarness() as h:
            h.store.get_all_semantic.return_value = []
            cc._memory_cmd(_ns(mem_action="list"))
        assert "No semantic memory entries." in capsys.readouterr().out

    def test_list_renders_json_and_raw_values(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _MemHarness() as h:
            h.store.get_all_semantic.return_value = [
                {"key": "a", "value_json": '{"x": 1}', "confidence": 0.9, "source": "cli"},
                {"key": "b", "value_json": "not-json", "confidence": 0.1, "source": "cli"},
            ]
            cc._memory_cmd(_ns(mem_action="list"))
        out = capsys.readouterr().out
        assert "a: {'x': 1}" in out and "b: not-json" in out

    def test_search_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _MemHarness() as h:
            h.store.search_episodic.return_value = []
            cc._memory_cmd(_ns(mem_action="search", query="q"))
        assert "No episodic memories found." in capsys.readouterr().out

    def test_search_handles_str_and_list_tags(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _MemHarness() as h:
            h.store.search_episodic.return_value = [
                {"text": "first", "importance": 0.5, "tags": '["t1"]'},
                {"text": "second", "importance": 0.2, "tags": ["t2"]},
                {"text": "third"},
            ]
            cc._memory_cmd(_ns(mem_action="search", query="q"))
        out = capsys.readouterr().out
        assert "tags: t1" in out and "tags: t2" in out and "third" in out

    def test_stats(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _MemHarness() as h:
            h.store.memory_stats.return_value = {
                "semantic_active": 3,
                "semantic_deleted": 1,
                "episodic_active": 7,
                "episodic_deleted": 2,
                "faiss_index_size": 10,
                "events_count": 4,
                "embedded_count": 7,
                "faiss_available": True,
            }
            cc._memory_cmd(_ns(mem_action="stats"))
        out = capsys.readouterr().out
        assert "Semantic: 3 active, 1 deleted" in out
        assert "Embedded: 7/7" in out
        assert "FAISS accelerator: 10 vectors indexed" in out
        assert "re-embed sweep" not in out

    def test_stats_names_what_fills_unembedded_rows(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A bare 0/185 after an import must not read as a broken store."""
        with _MemHarness() as h:
            h.store.memory_stats.return_value = {
                "semantic_active": 384,
                "semantic_deleted": 0,
                "episodic_active": 185,
                "episodic_deleted": 0,
                "faiss_index_size": 0,
                "events_count": 0,
                "embedded_count": 0,
                "faiss_available": True,
            }
            cc._memory_cmd(_ns(mem_action="stats"))
        out = capsys.readouterr().out
        assert "Embedded: 0/185" in out
        assert "185 row(s) wait for the gateway's background re-embed sweep" in out

    def test_stats_reports_read_volume_labelled_as_this_process(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The CLI builds its own store, so the totals must not read as lifetime."""
        with _MemHarness() as h:
            h.store.memory_stats.return_value = {
                "semantic_active": 3,
                "semantic_deleted": 0,
                "episodic_active": 7,
                "episodic_deleted": 0,
                "faiss_index_size": 0,
                "events_count": 4,
                "embedded_count": 7,
                "faiss_available": False,
            }
            h.store.read_counters.return_value = {
                "statements_executed": 9,
                "rows_read": 40,
                "semantic_rows_read": 12,
                "semantic_full_scans": 2,
                "episodic_rows_read": 21,
                "episodic_full_scans": 3,
            }
            cc._memory_cmd(_ns(mem_action="stats"))
        out = capsys.readouterr().out
        assert "Reads (this process): 40 rows over 9 statements" in out
        assert "population scans: semantic 2 (12 rows), episodic 3 (21 rows)" in out

    def test_stats_without_faiss_reports_fallback_not_zero_vectors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No accelerator must not read as "nothing indexed" when embeddings exist."""
        with _MemHarness() as h:
            h.store.memory_stats.return_value = {
                "semantic_active": 3,
                "semantic_deleted": 1,
                "episodic_active": 228,
                "episodic_deleted": 2,
                "faiss_index_size": 0,
                "events_count": 4,
                "embedded_count": 228,
                "faiss_available": False,
            }
            cc._memory_cmd(_ns(mem_action="stats"))
        out = capsys.readouterr().out
        assert "Embedded: 228/228" in out
        assert "FAISS accelerator: not installed — stdlib cosine fallback (exact)" in out
        assert "0 vectors" not in out

    def test_audit_with_and_without_findings(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _MemHarness(), patch("kiro_crew.cli_commands.scan_memory", return_value=[]):
            cc._memory_cmd(_ns(mem_action="audit"))
        assert "No suspicious content in memory." in capsys.readouterr().out

        findings = [{"type": "semantic", "key": "k", "warning": "w", "value": "v"}]
        with _MemHarness(), patch("kiro_crew.cli_commands.scan_memory", return_value=findings):
            cc._memory_cmd(_ns(mem_action="audit"))
        assert "1 suspicious entries" in capsys.readouterr().out

    def test_export_to_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _MemHarness() as h:
            h.store.get_all_semantic.return_value = [{"key": "a"}]
            h.store.get_episodic_list.return_value = []
            h.store.get_events.return_value = []
            cc._memory_cmd(_ns(mem_action="export", output=None))
        assert json.loads(capsys.readouterr().out)["semantic"] == [{"key": "a"}]

    def test_export_to_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        out_file = tmp_path / "dump.json"
        with _MemHarness() as h:
            h.store.get_all_semantic.return_value = []
            h.store.get_episodic_list.return_value = []
            h.store.get_events.return_value = []
            cc._memory_cmd(_ns(mem_action="export", output=str(out_file)))
        assert json.loads(out_file.read_text())["episodic"] == []
        assert "Exported to" in capsys.readouterr().out

    def test_migrate_prints_counts(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _MemHarness() as h:
            h.store.migrate_from_markdown.return_value = {
                "semantic": 1,
                "episodic": 2,
                "skipped": 3,
            }
            cc._memory_cmd(_ns(mem_action="migrate"))
        out = capsys.readouterr().out
        assert "Migration complete" in out and "Skipped:  3" in out

    def test_import_requires_a_file_argument(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _MemHarness():
            cc._memory_cmd(_ns(mem_action="import", file=None))
        assert "Usage: kirocrew memory import" in capsys.readouterr().out

    def test_import_missing_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        with _MemHarness():
            cc._memory_cmd(_ns(mem_action="import", file=str(tmp_path / "absent.json")))
        assert "File not found" in capsys.readouterr().out

    def test_import_reads_through_safe_read_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = tmp_path / "in.json"
        src.write_text(json.dumps({"semantic": []}), encoding="utf-8")
        with _MemHarness() as h:
            h.store.import_memory.return_value = {"semantic": 5, "episodic": 0, "skipped": 1}
            cc._memory_cmd(_ns(mem_action="import", file=str(src)))
        h.store.import_memory.assert_called_once_with({"semantic": []})
        assert "Import complete" in capsys.readouterr().out

    def test_import_names_pending_embeddings(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Imported episodes land without vectors; the summary must say so."""
        src = tmp_path / "in.json"
        src.write_text(json.dumps({"episodic": []}), encoding="utf-8")
        with _MemHarness() as h:
            h.store.import_memory.return_value = {"semantic": 0, "episodic": 185, "skipped": 0}
            h.store.has_pending_embeddings.return_value = True
            cc._memory_cmd(_ns(mem_action="import", file=str(src)))
        out = capsys.readouterr().out
        assert "embedding vectors are not built by this command" in out
        assert "keyword-searchable only" in out

    def test_import_omits_embedding_note_when_nothing_pending(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = tmp_path / "in.json"
        src.write_text(json.dumps({"episodic": []}), encoding="utf-8")
        with _MemHarness() as h:
            h.store.import_memory.return_value = {"semantic": 0, "episodic": 3, "skipped": 0}
            h.store.has_pending_embeddings.return_value = False
            cc._memory_cmd(_ns(mem_action="import", file=str(src)))
        assert "embedding vectors are not built" not in capsys.readouterr().out

    def test_unknown_action_prints_usage_and_closes(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with _MemHarness() as h:
            cc._memory_cmd(_ns(mem_action="bogus"))
        h.store.close.assert_called_once()
        assert "Usage: kirocrew memory" in capsys.readouterr().out


# ── artifact subcommands ──


class _ArtifactHarness:
    """Patches config/dashboard resolution so ``_artifact`` talks to a fake HTTP layer."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses
        self._patches: list[Any] = []
        self.urlopen: MagicMock = MagicMock()

    def __enter__(self) -> _ArtifactHarness:
        self._patches = [
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
            patch("kiro_crew.cli_commands.parse_dashboard_url", return_value=("localhost", 5476)),
            patch("kiro_crew.cli_commands._internal_secret", return_value="s"),
            patch("kiro_crew.cli_commands._gateway_send", side_effect=self._responses),
        ]
        started = [p.start() for p in self._patches]
        self.urlopen = started[-1]
        return self

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self._patches):
            p.stop()


class TestArtifactCli:
    def test_list_renders_rows(self, capsys: pytest.CaptureFixture[str]) -> None:
        payload = {
            "artifacts": [
                {"slug": "s1", "version": 3, "kind": "widget", "tags": ["a"], "name": "One"},
                {"slug": "s2", "version": 1, "kind": "markdown", "name": "Two"},
            ]
        }
        with _ArtifactHarness([_FakeResponse(payload)]):
            cc._artifact(_ns(artifact_action="list", tag="ops", kind=None, q="One"))
        out = capsys.readouterr().out
        assert "s1" in out and "[a]" in out and "s2" in out

    def test_list_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _ArtifactHarness([_FakeResponse({"artifacts": []})]):
            cc._artifact(_ns(artifact_action="list", tag=None, kind=None, q=None))
        assert "No artifacts." in capsys.readouterr().out

    def test_list_server_error_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        err = _http_error(500, json.dumps({"error": "db down"}).encode())
        with _ArtifactHarness([err]), pytest.raises(SystemExit) as exc:
            cc._artifact(_ns(artifact_action="list", tag=None, kind=None, q=None))
        assert exc.value.code == 1
        assert "db down" in capsys.readouterr().err

    def test_list_transport_error_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            _ArtifactHarness([urllib.error.URLError("refused")]),
            pytest.raises(SystemExit),
        ):
            cc._artifact(_ns(artifact_action="list", tag=None, kind=None, q=None))
        assert "Error:" in capsys.readouterr().err

    def test_show_prints_content(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _ArtifactHarness([_FakeResponse({"content": "<p>hi</p>"})]):
            cc._artifact(_ns(artifact_action="show", slug="s1", version=None, meta=False))
        assert "<p>hi</p>" in capsys.readouterr().out

    def test_show_meta_strips_content(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _ArtifactHarness([_FakeResponse({"content": "x", "slug": "s1", "version": 2})]):
            cc._artifact(_ns(artifact_action="show", slug="s1", version=2, meta=True))
        data = json.loads(capsys.readouterr().out)
        assert "content" not in data and data["slug"] == "s1"

    def test_show_error_exits_1(self) -> None:
        err = _http_error(404, json.dumps({"error": "no such artifact"}).encode())
        with _ArtifactHarness([err]), pytest.raises(SystemExit):
            cc._artifact(_ns(artifact_action="show", slug="ghost", version=None, meta=False))

    def test_save_with_inline_content_and_tags(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _ArtifactHarness([_FakeResponse({"slug": "new", "version": 1})]):
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="New",
                    content="body",
                    content_file=None,
                    tags="a, b ,",
                    kind="widget",
                    description="d",
                )
            )
        assert "Saved: slug=new version=1" in capsys.readouterr().out

    def test_save_warns_when_the_slug_was_suffixed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `save` has no --slug, so a colliding name silently lands a NEW
        # artifact at a suffixed slug while the canonical one keeps its old
        # content. The warning has to name both the taken slug and the verb
        # that versions in place.
        with _ArtifactHarness(
            [
                _FakeResponse(
                    {
                        "slug": "run-summary-2",
                        "version": 1,
                        "slug_collided_with": "run-summary",
                    }
                )
            ]
        ):
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="Run Summary",
                    content="corrected",
                    content_file=None,
                    tags=None,
                    kind=None,
                    description=None,
                )
            )
        captured = capsys.readouterr()
        assert "Saved: slug=run-summary-2 version=1" in captured.out
        assert "run-summary" in captured.err
        assert "kirocrew artifact update run-summary" in captured.err

    def test_save_is_silent_when_the_slug_was_free(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with _ArtifactHarness([_FakeResponse({"slug": "fresh", "version": 1})]):
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="Fresh",
                    content="body",
                    content_file=None,
                    tags=None,
                    kind=None,
                    description=None,
                )
            )
        captured = capsys.readouterr()
        assert "Saved: slug=fresh version=1" in captured.out
        assert captured.err == ""

    def test_save_forwards_an_explicit_slug(self) -> None:
        with _ArtifactHarness([_FakeResponse({"slug": "chosen", "version": 1})]) as h:
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="Chosen",
                    slug="chosen-by-hand",
                    content="body",
                    content_file=None,
                    tags=None,
                    kind=None,
                    description=None,
                )
            )
        body = json.loads(h.urlopen.call_args[0][0].data.decode())
        assert body["slug"] == "chosen-by-hand"

    def test_save_forwards_an_empty_explicit_slug(self) -> None:
        # "" is a slug the caller named, not a request to derive one. Filtering it
        # out would silently route an explicit save into the derive-and-suffix
        # branch — the exact asymmetry this flag exists to close.
        with _ArtifactHarness([_FakeResponse({"slug": "x", "version": 1})]) as h:
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="Empty Slug",
                    slug="",
                    content="body",
                    content_file=None,
                    tags=None,
                    kind=None,
                    description=None,
                )
            )
        body = json.loads(h.urlopen.call_args[0][0].data.decode())
        assert "slug" in body
        assert body["slug"] == ""

    def test_save_with_an_empty_slug_surfaces_the_refusal(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Forwarding "" is only worth anything if the refusal reaches the caller.
        # The store rejects it via _validate_slug and the handler answers 400, so
        # it arrives on the CLI's existing error path — no new error surface.
        err = _http_error(
            400,
            json.dumps({"error": "invalid slug '': must match ^[a-z0-9]..."}).encode(),
        )
        with _ArtifactHarness([err]), pytest.raises(SystemExit) as exc:
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="Empty Slug",
                    slug="",
                    content="body",
                    content_file=None,
                    tags=None,
                    kind=None,
                    description=None,
                )
            )
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "invalid slug" in captured.err
        assert captured.out == ""

    def test_save_omits_the_slug_key_when_none_was_given(self) -> None:
        # An ABSENT flag must send no key at all. Forwarding it as "" would hand
        # the store a slug it refuses, so every plain `save` would start failing;
        # only an explicitly-passed value may reach the request body.
        with _ArtifactHarness([_FakeResponse({"slug": "derived", "version": 1})]) as h:
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="Derived",
                    slug=None,
                    content="body",
                    content_file=None,
                    tags=None,
                    kind=None,
                    description=None,
                )
            )
        body = json.loads(h.urlopen.call_args[0][0].data.decode())
        # Positive control: this is the save request body, so the absence below
        # is a fact about the field and not about a request that never went out.
        assert body["name"] == "Derived"
        assert "slug" not in body

    def test_save_with_a_taken_slug_reports_the_conflict(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The store refuses an explicit slug rather than suffixing it, so the
        # handler answers 409. It has to reach the caller as the named condition
        # and a non-zero exit, never a traceback.
        err = _http_error(409, json.dumps({"error": "artifact already exists: taken"}).encode())
        with _ArtifactHarness([err]), pytest.raises(SystemExit) as exc:
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="Taken",
                    slug="taken",
                    content="body",
                    content_file=None,
                    tags=None,
                    kind=None,
                    description=None,
                )
            )
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "artifact already exists: taken" in captured.err
        assert captured.out == ""

    def test_save_with_an_explicit_slug_emits_no_suffix_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The warning advises `artifact update`, which is wrong guidance for a
        # caller who named the slug: they get a 409, never a rename. The CLI must
        # therefore key it on the server's field alone and not on the flag being
        # set. The server half — an explicit slug reporting no collision — is
        # pinned in test_artifacts_handlers.py.
        with _ArtifactHarness([_FakeResponse({"slug": "chosen-by-hand", "version": 1})]):
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="Run Summary",
                    slug="chosen-by-hand",
                    content="body",
                    content_file=None,
                    tags=None,
                    kind=None,
                    description=None,
                )
            )
        captured = capsys.readouterr()
        assert "Saved: slug=chosen-by-hand version=1" in captured.out
        assert captured.err == ""

    def test_save_relays_the_theme_contrast_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The gateway computes the verdict (same relay pattern as
        # slug_collided_with); the CLI must surface it — this direct-POST
        # path is the one the motivating incident traveled, which the
        # MCP-layer hint alone never covered.
        with _ArtifactHarness(
            [_FakeResponse({"slug": "perf", "version": 1, "theme_contrast_warning": True})]
        ):
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="Perf",
                    content='<div style="color:#111">x</div>',
                    content_file=None,
                    tags=None,
                    kind=None,
                    description=None,
                )
            )
        captured = capsys.readouterr()
        assert "Saved: slug=perf version=1" in captured.out
        assert "theme variables" in captured.err

    def test_update_relays_the_theme_contrast_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with _ArtifactHarness(
            [_FakeResponse({"slug": "perf", "version": 2, "theme_contrast_warning": True})]
        ):
            cc._artifact(
                _ns(
                    artifact_action="update",
                    slug="perf",
                    content='<body style="background:#fffbe6">x</body>',
                    content_file=None,
                    name=None,
                    description=None,
                    tags=None,
                )
            )
        captured = capsys.readouterr()
        assert "Updated: slug=perf version=2" in captured.out
        assert "theme variables" in captured.err

    def test_save_reads_content_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        f = tmp_path / "body.html"
        f.write_text("from-file", encoding="utf-8")
        with _ArtifactHarness([_FakeResponse({"slug": "s", "version": 1})]):
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="N",
                    content=None,
                    content_file=str(f),
                    tags=None,
                    kind=None,
                    description=None,
                )
            )
        assert "Saved:" in capsys.readouterr().out

    def test_save_refuses_sensitive_content_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        f = tmp_path / "creds"
        f.write_text("secret", encoding="utf-8")
        with (
            _ArtifactHarness([]),
            patch("kiro_crew.cli_commands.is_sensitive_path", return_value=True),
            pytest.raises(SystemExit) as exc,
        ):
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="N",
                    content=None,
                    content_file=str(f),
                    tags=None,
                    kind=None,
                    description=None,
                )
            )
        assert exc.value.code == 1
        assert "sensitive path" in capsys.readouterr().err

    def test_save_error_exits_1(self) -> None:
        err = _http_error(400, json.dumps({"error": "bad name"}).encode())
        with _ArtifactHarness([err]), pytest.raises(SystemExit):
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="N",
                    content="c",
                    content_file=None,
                    tags=None,
                    kind=None,
                    description=None,
                )
            )

    def test_update_metadata_only_does_not_touch_content(self) -> None:
        with (
            _ArtifactHarness([_FakeResponse({"slug": "s", "version": 4})]) as h,
            patch("sys.stdin.isatty", return_value=True),
        ):
            cc._artifact(
                _ns(
                    artifact_action="update",
                    slug="s",
                    content=None,
                    content_file=None,
                    name="Renamed",
                    description="d",
                    tags="x",
                )
            )
        req = h.urlopen.call_args[0][0]
        body = json.loads(req.data.decode())
        assert "content" not in body
        assert body == {"name": "Renamed", "description": "d", "tags": ["x"]}

    def test_update_with_content_sends_it(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _ArtifactHarness([_FakeResponse({"slug": "s", "version": 5})]):
            cc._artifact(
                _ns(
                    artifact_action="update",
                    slug="s",
                    content="new body",
                    content_file=None,
                    name=None,
                    description=None,
                    tags=None,
                )
            )
        assert "Updated: slug=s version=5" in capsys.readouterr().out

    def test_update_with_nothing_to_change_exits_1(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            _ArtifactHarness([]),
            patch("sys.stdin.isatty", return_value=True),
            pytest.raises(SystemExit) as exc,
        ):
            cc._artifact(
                _ns(
                    artifact_action="update",
                    slug="s",
                    content=None,
                    content_file=None,
                    name=None,
                    description=None,
                    tags=None,
                )
            )
        assert exc.value.code == 1
        assert "provide content/--name" in capsys.readouterr().err

    def test_update_error_exits_1(self) -> None:
        err = _http_error(409, json.dumps({"error": "conflict"}).encode())
        with _ArtifactHarness([err]), pytest.raises(SystemExit):
            cc._artifact(
                _ns(
                    artifact_action="update",
                    slug="s",
                    content="c",
                    content_file=None,
                    name=None,
                    description=None,
                    tags=None,
                )
            )

    def test_delete_reports_slug(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _ArtifactHarness([_FakeResponse(b"")]):
            cc._artifact(_ns(artifact_action="delete", slug="gone"))
        assert "Deleted: gone" in capsys.readouterr().out

    def test_delete_error_exits_1(self) -> None:
        err = _http_error(404, b"not json")
        with _ArtifactHarness([err]), pytest.raises(SystemExit):
            cc._artifact(_ns(artifact_action="delete", slug="gone"))

    def test_versions_lists_numbers(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _ArtifactHarness([_FakeResponse({"versions": [1, 2, 3]})]):
            cc._artifact(_ns(artifact_action="versions", slug="s"))
        assert "v1, v2, v3" in capsys.readouterr().out

    def test_versions_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _ArtifactHarness([_FakeResponse({"versions": []})]):
            cc._artifact(_ns(artifact_action="versions", slug="s"))
        assert "No versions for s." in capsys.readouterr().out

    def test_versions_error_exits_1(self) -> None:
        with _ArtifactHarness([_http_error(500, b"")]), pytest.raises(SystemExit):
            cc._artifact(_ns(artifact_action="versions", slug="s"))

    def test_unknown_action_exits_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _ArtifactHarness([]), pytest.raises(SystemExit) as exc:
            cc._artifact(_ns(artifact_action="bogus"))
        assert exc.value.code == 2
        assert "Usage: kirocrew artifact" in capsys.readouterr().err


class TestPodDispatch:
    def test_pod_delegates_to_verb_layer(self) -> None:
        args = _ns(pod_action="ls")
        with patch("kiro_crew.pod.cli.dispatch") as dispatch:
            cc._pod(args)
        dispatch.assert_called_once_with(args)


# ── telemetry subcommand ──


class TestTelemetryCli:
    def test_status_is_read_only(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
            patch("kiro_crew.cli_commands.beacon") as beacon,
        ):
            beacon.format_status.return_value = "beacon: OFF"
            cc._telemetry(_ns(telemetry_action="status"))
        assert "beacon: OFF" in capsys.readouterr().out

    def test_missing_action_defaults_to_status(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
            patch("kiro_crew.cli_commands.beacon") as beacon,
        ):
            beacon.format_status.return_value = "default-status"
            cc._telemetry(_ns(telemetry_action=None))
        assert "default-status" in capsys.readouterr().out

    def test_unknown_action_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
            pytest.raises(SystemExit) as exc,
        ):
            cc._telemetry(_ns(telemetry_action="frobnicate"))
        assert exc.value.code == 1
        assert "Unknown telemetry action" in capsys.readouterr().err

    def test_disable_writes_config_and_acks_privacy(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"agent": {"model": "auto"}}), encoding="utf-8")
        effective = KiroCrewConfig()
        effective.telemetry.beacon_enabled = False
        with (
            patch.object(KiroCrewConfig, "load", return_value=effective),
            patch("kiro_crew.cli_commands.config_path", return_value=path),
            patch("kiro_crew.cli_commands.beacon") as beacon,
        ):
            beacon.DISABLE_ENV = "KIROCREW_NO_BEACON"
            beacon.INSTALL_ID_FILE = "install_id"
            cc._telemetry(_ns(telemetry_action="disable"))
        data = json.loads(path.read_text())
        assert data["telemetry"]["beacon_enabled"] is False
        assert data["dashboard"]["privacy_acked"] is True
        # Unrelated sections survive the rewrite.
        assert data["agent"] == {"model": "auto"}
        assert "DISABLED" in capsys.readouterr().out

    def test_enable_creates_config_when_absent(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "config.json"
        effective = KiroCrewConfig()
        effective.telemetry.beacon_enabled = True
        with (
            patch.object(KiroCrewConfig, "load", return_value=effective),
            patch("kiro_crew.cli_commands.config_path", return_value=path),
            patch("kiro_crew.cli_commands.beacon") as beacon,
        ):
            beacon.is_governance_pinned_off.return_value = False
            cc._telemetry(_ns(telemetry_action="enable"))
        assert json.loads(path.read_text())["telemetry"]["beacon_enabled"] is True

    def test_enable_refused_when_governance_pins_off(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "config.json"
        with (
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
            patch("kiro_crew.cli_commands.config_path", return_value=path),
            patch("kiro_crew.cli_commands.beacon") as beacon,
            pytest.raises(SystemExit) as exc,
        ):
            beacon.is_governance_pinned_off.return_value = True
            cc._telemetry(_ns(telemetry_action="enable"))
        assert exc.value.code == 1
        assert not path.exists(), "must not write a setting that would have no effect"
        assert "pinned OFF" in capsys.readouterr().err

    def test_unreadable_config_exits_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "config.json"
        path.write_text("{not json", encoding="utf-8")
        with (
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
            patch("kiro_crew.cli_commands.config_path", return_value=path),
            patch("kiro_crew.cli_commands.beacon"),
            pytest.raises(SystemExit) as exc,
        ):
            cc._telemetry(_ns(telemetry_action="disable"))
        assert exc.value.code == 1
        assert "Could not read" in capsys.readouterr().err

    def test_non_object_config_is_refused_not_overwritten(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "config.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        with (
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
            patch("kiro_crew.cli_commands.config_path", return_value=path),
            patch("kiro_crew.cli_commands.beacon"),
            pytest.raises(SystemExit),
        ):
            cc._telemetry(_ns(telemetry_action="disable"))
        assert path.read_text() == "[1, 2, 3]", "a toggle must never be a data-loss path"
        assert "refusing to overwrite" in capsys.readouterr().err

    def test_non_object_section_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "config.json"
        original = json.dumps({"telemetry": "yes-please"})
        path.write_text(original, encoding="utf-8")
        with (
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
            patch("kiro_crew.cli_commands.config_path", return_value=path),
            patch("kiro_crew.cli_commands.beacon"),
            pytest.raises(SystemExit),
        ):
            cc._telemetry(_ns(telemetry_action="disable"))
        assert path.read_text() == original
        assert 'non-object "telemetry" value' in capsys.readouterr().err

    def test_overlay_shadowing_the_write_is_reported_as_failure(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A false promise on a privacy control is worse than an error."""
        path = tmp_path / "config.json"
        path.write_text("{}", encoding="utf-8")
        still_on = KiroCrewConfig()
        still_on.telemetry.beacon_enabled = True
        with (
            patch.object(KiroCrewConfig, "load", return_value=still_on),
            patch("kiro_crew.cli_commands.config_path", return_value=path),
            patch("kiro_crew.cli_commands.beacon") as beacon,
            pytest.raises(SystemExit) as exc,
        ):
            beacon.DISABLE_ENV = "KIROCREW_NO_BEACON"
            cc._telemetry(_ns(telemetry_action="disable"))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "still ENABLED" in err and "config.local.json" in err

    def test_write_failure_exits_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "config.json"
        path.write_text("{}", encoding="utf-8")
        with (
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
            patch("kiro_crew.cli_commands.config_path", return_value=path),
            patch("kiro_crew.cli_commands.beacon"),
            patch("kiro_crew.config.loader.atomic_write", side_effect=OSError("disk full")),
            pytest.raises(SystemExit) as exc,
        ):
            cc._telemetry(_ns(telemetry_action="disable"))
        assert exc.value.code == 1
        assert "Could not write" in capsys.readouterr().err

    def test_effective_check_failure_does_not_mask_the_write(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A broken diagnostic read must not turn a successful write into an error."""
        path = tmp_path / "config.json"
        path.write_text("{}", encoding="utf-8")
        with (
            patch.object(
                KiroCrewConfig,
                "load",
                side_effect=[KiroCrewConfig(), RuntimeError("bad config")],
            ),
            patch("kiro_crew.cli_commands.config_path", return_value=path),
            patch("kiro_crew.cli_commands.beacon") as beacon,
        ):
            beacon.INSTALL_ID_FILE = "install_id"
            cc._telemetry(_ns(telemetry_action="disable"))
        assert "DISABLED" in capsys.readouterr().out


# ── eval runner (`kirocrew eval`) ──


def _scenario(name: str = "smoke", *, turns: int = 2, judge_criteria: str = "") -> Any:
    sessions = [SimpleNamespace(turns=[object()] * turns)]
    return SimpleNamespace(
        name=name,
        description=f"{name} description",
        judge_criteria=judge_criteria,
        sessions=sessions,
    )


def _scenario_result(passed: bool = True, *, turn: Any | None = None) -> Any:
    turns = [turn] if turn is not None else []
    return SimpleNamespace(
        passed=passed,
        sessions=[SimpleNamespace(turns=turns)],
        summary=lambda: {"passed": passed},
    )


class _EvalHarness:
    """Stubs every collaborator ``_run_eval`` touches, so no provider is built,
    no model is called, and results land under the (chdir'd) tmp dir."""

    def __init__(self, results: list[Any]) -> None:
        self.results = results
        self.runner = MagicMock()
        self.runner.run_scenarios = AsyncMock(return_value=results)
        self.judge = MagicMock()
        self.judge.pass_threshold = 3
        self.judge.start = AsyncMock()
        self.judge.shutdown = AsyncMock()
        self.judge.judge_turn = AsyncMock(
            return_value=SimpleNamespace(score=5, reason="looks right")
        )
        self._patches: list[Any] = []

    def __enter__(self) -> _EvalHarness:
        self._patches = [
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
            patch("kiro_crew.cli_commands.build_provider_factory", return_value=MagicMock()),
            patch("kiro_crew.cli_commands.EvalRunner", return_value=self.runner),
            patch("kiro_crew.cli_commands.LLMJudge", return_value=self.judge),
            patch("kiro_crew.cli_commands.format_results", return_value="## Report"),
            patch("kiro_crew.cli_commands.score_by_dimension", return_value={}),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self._patches):
            p.stop()


class TestRunEval:
    """``_run_eval`` writes its report into ``Path.cwd()``, so every test chdirs
    into ``tmp_path`` first -- nothing is written outside the temp dir."""

    @pytest.mark.asyncio
    async def test_default_runs_smoke_test_and_writes_both_reports(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        with (
            _EvalHarness([_scenario_result(True)]),
            patch("kiro_crew.cli_commands.load_scenario", return_value=_scenario()) as loader,
        ):
            await cc._run_eval(_ns(all_scenarios=False, scenarios=None, judge=False))
        assert loader.call_args[0][0].name == "smoke_test.json"
        results_dir = tmp_path / "eval_results"
        assert len(list(results_dir.iterdir())) == 2
        report = next(p for p in results_dir.iterdir() if p.suffix == ".md")
        assert report.read_text() == "## Report\n"
        dump = next(p for p in results_dir.iterdir() if p.suffix == ".json")
        payload = json.loads(dump.read_text())
        assert payload["overall_passed"] == 1 and payload["overall_total"] == 1
        out = capsys.readouterr().out
        assert "Running: smoke (2 turns)" in out and "Overall: 1/1 scenarios passed" in out

    @pytest.mark.asyncio
    async def test_all_scenarios_loads_the_whole_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        scenarios = [_scenario("a"), _scenario("b")]
        with (
            _EvalHarness([_scenario_result(True), _scenario_result(False)]) as h,
            patch("kiro_crew.cli_commands.load_scenarios", return_value=scenarios) as loader,
        ):
            await cc._run_eval(_ns(all_scenarios=True, scenarios=None, judge=False))
        assert loader.call_args[0][0].name == "scenarios"
        h.runner.run_scenarios.assert_awaited_once_with(scenarios)

    @pytest.mark.asyncio
    async def test_named_scenario_is_resolved_by_extension(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        with (
            _EvalHarness([_scenario_result(True)]),
            patch("kiro_crew.cli_commands.load_scenario", return_value=_scenario("named")) as ld,
        ):
            await cc._run_eval(
                _ns(all_scenarios=False, scenarios=["memory_recall_basic"], judge=False)
            )
        assert ld.call_args[0][0].name == "memory_recall_basic.json"

    @pytest.mark.asyncio
    async def test_unknown_scenario_lists_available_and_returns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        with _EvalHarness([]) as h:
            await cc._run_eval(_ns(all_scenarios=False, scenarios=["does-not-exist"], judge=False))
        out = capsys.readouterr().out
        assert "scenario 'does-not-exist' not found" in out
        assert "smoke_test" in out
        h.runner.run_scenarios.assert_not_awaited()
        assert not (tmp_path / "eval_results").exists()

    #: Imports the helper and writes a report holding the formatter's own glyph.
    _WRITE_CHILD = """
import locale, sys
from pathlib import Path
from kiro_crew.cli_commands import write_eval_artifacts

print("encoding=" + locale.getencoding())
report_path, json_path = write_eval_artifacts(
    Path(sys.argv[1]), "20260101_000000", "## \\u2705 ok", {"overall_passed": 1}
)
print("wrote=" + report_path.name + "," + json_path.name)
"""

    def test_the_artifacts_survive_a_non_utf8_default_codec(self, tmp_path: Path) -> None:
        """The report is written as UTF-8, not the host's locale codec.

        Run in a CHILD PROCESS on purpose, against this file's usual convention:
        the default codec ``open()`` picks is fixed when the interpreter starts,
        so it cannot be substituted in-process — patching ``locale`` does not
        reach the C-level lookup ``io`` actually performs.

        Without ``encoding="utf-8"`` the write raises ``UnicodeEncodeError`` under
        cp1252/cp950/cp932, and it raises after the eval has already run, so both
        artifacts are lost.
        """
        env = dict(os.environ)
        # PEP 540 off, PEP 538 coercion off, C locale: a non-UTF-8 default on
        # every platform -- the Windows ANSI code page, or ASCII on POSIX.
        env["PYTHONUTF8"] = "0"
        env["PYTHONCOERCECLOCALE"] = "0"
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
        env.pop("PYTHONIOENCODING", None)
        # The child is anchored outside the checkout, so hand it this
        # interpreter's own import path rather than relying on an installed copy.
        env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
        env["KIROCREW_HOME"] = str(tmp_path / "home")
        out_dir = tmp_path / "eval_results"
        proc = subprocess.run(
            [sys.executable, "-c", self._WRITE_CHILD, str(out_dir)],
            env=env,
            capture_output=True,
            text=True,
            # This process's own capture is UTF-8 regardless of the codec the
            # CHILD was forced onto; only the child's file write is under test.
            encoding="utf-8",
            errors="replace",
            # The child imports the installed package; anchor it outside the
            # checkout so nothing it writes relatively lands in the repo.
            cwd=tmp_path,
        )
        encoding = next(
            (
                line.split("=", 1)[1].strip()
                for line in proc.stdout.splitlines()
                if line.startswith("encoding=")
            ),
            "",
        )
        if not encoding:
            pytest.fail(f"the child never started:\n{proc.stdout}\n{proc.stderr}")
        if encoding.lower().replace("-", "") in {"utf8", "utf8mb4"}:
            pytest.skip(f"this host's default codec stayed UTF-8 ({encoding}); nothing to prove")

        assert proc.returncode == 0, f"the save died under {encoding}:\n{proc.stderr}"
        report = next(p for p in out_dir.iterdir() if p.suffix == ".md")
        # Decode strictly: the bytes on disk have to BE UTF-8, whatever the host
        # codec was. (Line endings are the text layer's, so compare by line.)
        assert report.read_bytes().decode("utf-8").splitlines() == ["## ✅ ok"]

    @pytest.mark.asyncio
    async def test_dimension_summary_marks_pass_and_fail_rates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        dims = {
            "memory": {"rate": 0.8, "passed": 4, "total": 5},
            "tools": {"rate": 0.5, "passed": 1, "total": 2},
        }
        with (
            _EvalHarness([_scenario_result(True)]),
            patch("kiro_crew.cli_commands.load_scenario", return_value=_scenario()),
            patch("kiro_crew.cli_commands.score_by_dimension", return_value=dims),
        ):
            await cc._run_eval(_ns(all_scenarios=False, scenarios=None, judge=False))
        out = capsys.readouterr().out
        assert "✅ memory: 4/5 (80%)" in out
        assert "❌ tools: 1/2 (50%)" in out

    @pytest.mark.asyncio
    async def test_judge_scores_judge_assertions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assertion = SimpleNamespace(type=AssertionType.JUDGE, value="was it helpful")
        turn = SimpleNamespace(
            assertion_results=[(assertion, False)],
            user_message="hi",
            agent_response="hello",
        )
        with (
            _EvalHarness([_scenario_result(True, turn=turn)]) as h,
            patch("kiro_crew.cli_commands.load_scenario", return_value=_scenario()),
        ):
            await cc._run_eval(_ns(all_scenarios=False, scenarios=None, judge=True))
        h.judge.start.assert_awaited_once()
        h.judge.shutdown.assert_awaited_once()
        # score 5 >= pass_threshold 3, so the assertion flips to passing.
        assert turn.assertion_results[0][1] is True
        assert "Judge: 5/5" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_judge_failure_marks_assertion_failed_without_aborting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assertion = SimpleNamespace(type=AssertionType.JUDGE, value=None)
        turn = SimpleNamespace(
            assertion_results=[(assertion, True)],
            user_message="hi",
            agent_response="hello",
        )
        with (
            _EvalHarness([_scenario_result(True, turn=turn)]) as h,
            patch(
                "kiro_crew.cli_commands.load_scenario",
                return_value=_scenario(judge_criteria="be terse"),
            ),
        ):
            h.judge.judge_turn.side_effect = RuntimeError("judge offline")
            await cc._run_eval(_ns(all_scenarios=False, scenarios=None, judge=True))
        assert turn.assertion_results[0][1] is False
        assert "Judge failed for turn: judge offline" in capsys.readouterr().out
        h.judge.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_judge_assertions_are_left_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assertion = SimpleNamespace(type=AssertionType.CONTAINS, value="x")
        turn = SimpleNamespace(
            assertion_results=[(assertion, False)],
            user_message="hi",
            agent_response="hello",
        )
        with (
            _EvalHarness([_scenario_result(True, turn=turn)]) as h,
            patch("kiro_crew.cli_commands.load_scenario", return_value=_scenario()),
        ):
            await cc._run_eval(_ns(all_scenarios=False, scenarios=None, judge=True))
        h.judge.judge_turn.assert_not_awaited()
        assert turn.assertion_results[0][1] is False
