"""Cost guards for hand-added folder knowledge sources.

A folder source ingests with one LLM extraction call per chunk plus one summary
call per file, on a pool of billed sessions. Pointing the Library at a handful of
source repositories therefore spends real money unattended unless the ingestion
is paced and its scale is shown before it starts. These tests pin both guards,
and the invariant that pacing never drops a file.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.knowledge import add_source, confirm_source
from kiro_crew.knowledge.folder_watcher import (
    DEFAULT_MAX_FILES,
    FolderWatcher,
    estimate_scan_cost,
    folder_chunk_budget,
    max_files_prop,
    walk_filters,
)
from kiro_crew.knowledge.store import KnowledgeStore


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "cost.db"))
    yield s
    s.close()


def _cfg(folder_budget: int):
    """A loaded-config stand-in carrying just the knob under test."""
    cfg = MagicMock()
    cfg.knowledge.folder_ingest_chunk_budget = folder_budget
    return cfg


class TestFolderChunkBudgetResolution:
    def test_configured_default_applies_with_no_property(self):
        with patch("kiro_crew.config.loader.KiroCrewConfig.load",
                   return_value=_cfg(300)):
            assert folder_chunk_budget({}) == 300

    def test_per_source_property_wins_over_the_default(self):
        with patch("kiro_crew.config.loader.KiroCrewConfig.load",
                   return_value=_cfg(300)):
            assert folder_chunk_budget({"chunk_budget": 25}) == 25

    def test_per_source_zero_disables_the_bound(self):
        # The explicit opt-out for a user who does want the folder in one burst.
        with patch("kiro_crew.config.loader.KiroCrewConfig.load",
                   return_value=_cfg(300)):
            assert folder_chunk_budget({"chunk_budget": 0}) is None

    def test_configured_zero_disables_the_bound(self):
        with patch("kiro_crew.config.loader.KiroCrewConfig.load",
                   return_value=_cfg(0)):
            assert folder_chunk_budget({}) is None

    @pytest.mark.parametrize("bad", ["300", None, True, False, [], {}, -5, 1.5, 300.0])
    def test_junk_property_falls_back_to_the_default(self, bad: object):
        # properties is user-editable JSON; a bad value must not remove the guard.
        # True/False are checked explicitly: bool is an int subclass, so True would
        # otherwise read as a budget of 1.
        with patch("kiro_crew.config.loader.KiroCrewConfig.load",
                   return_value=_cfg(300)):
            assert folder_chunk_budget({"chunk_budget": bad}) == 300

    @pytest.mark.parametrize("bad", [1e309, -1e309, float("inf"), float("-inf"),
                                     float("nan")])
    def test_non_finite_property_falls_back_instead_of_raising(self, bad: float):
        # 1e309 parses to inf, which survives a >= 0 test and then raises in int().
        # Reached from the add-source request body, so a raise here is an HTTP 500.
        with patch("kiro_crew.config.loader.KiroCrewConfig.load",
                   return_value=_cfg(300)):
            assert folder_chunk_budget({"chunk_budget": bad}) == 300

    def test_unreadable_config_does_not_raise_into_a_scan(self):
        with patch("kiro_crew.config.loader.KiroCrewConfig.load",
                   side_effect=OSError("boom")):
            assert folder_chunk_budget({}) is None


class TestBudgetIsWiredToHandAddedSources:
    @pytest.mark.asyncio
    async def test_sweep_passes_the_folder_budget(self, store, tmp_path):
        from kiro_crew.knowledge.watcher import KnowledgeWatcher

        folder = tmp_path / "repo"
        folder.mkdir()
        (folder / "a.md").write_text("body")
        store.add_source(name="Mine", source_type="local_folder", uri=str(folder),
                         properties={"sync_status": "active"})
        w = KnowledgeWatcher(store, MagicMock())
        w._folder_watcher = MagicMock()
        w._folder_watcher.scan_source = AsyncMock(return_value={})
        w._discover_drop_folder = AsyncMock()
        w._discover_project_docs = AsyncMock()
        w._maybe_dedup_sweep = AsyncMock()
        w._maybe_reembed_stale = AsyncMock()
        with patch("kiro_crew.config.loader.KiroCrewConfig.load",
                   return_value=_cfg(120)):
            await w._scan()
        assert w._folder_watcher.scan_source.await_args.kwargs["chunk_budget"] == 120

    @pytest.mark.asyncio
    async def test_confirm_scan_is_budgeted(self, store, tmp_path):
        # The confirm scan is the largest burst: nothing is ingested yet, so every
        # discovered file is new.
        folder = tmp_path / "repo"
        folder.mkdir()
        sid = store.add_source("Mine", "local_folder", str(folder),
                               properties={"sync_status": "pending_confirmation"})
        watcher = MagicMock()
        watcher._folder_watcher = MagicMock()
        watcher._folder_watcher.scan_source = AsyncMock(return_value={"new": 0})

        app = web.Application()
        state = MagicMock()
        state.knowledge_store = store
        app["state"] = state
        app["knowledge_watcher"] = watcher
        app.router.add_post("/api/knowledge/sources/{id}/confirm", confirm_source)
        async with TestClient(TestServer(app)) as client:
            with patch("kiro_crew.config.loader.KiroCrewConfig.load",
                       return_value=_cfg(90)):
                resp = await client.post(f"/api/knowledge/sources/{sid}/confirm")
                assert resp.status == 200
                for task in app.get("_scan_tasks", set()).copy():
                    await task
        assert watcher._folder_watcher.scan_source.await_args.kwargs["chunk_budget"] == 90


class TestPacingLosesNoFiles:
    @pytest.mark.asyncio
    async def test_budgeted_sweeps_eventually_ingest_every_file(self, store, tmp_path):
        folder = tmp_path / "repo"
        folder.mkdir()
        for i in range(6):
            (folder / f"d{i}.md").write_text(f"body {i}")
        sid = store.add_source(name="Mine", source_type="local_folder", uri=str(folder),
                               properties={"sync_status": "active"})
        source = {"id": sid, "uri": str(folder), "source_type": "local_folder",
                  "properties": json.dumps({"sync_status": "active"})}

        pipe = MagicMock()
        pipe._dedup_enabled = False
        fw = FolderWatcher(store, pipe)
        ingested: list[str] = []

        async def _ingest(file_path, source_id, namespace, props, old_ids, root=""):
            ingested.append(file_path)
            return ["i-" + file_path], "done"  # 1 chunk per file

        fw._ingest_file = _ingest  # type: ignore[assignment]

        budget = folder_chunk_budget({"chunk_budget": 2})
        first = await fw.scan_source(source, chunk_budget=budget)
        assert first["budget_reached"] == 1
        assert len(ingested) == 2  # paced, not everything at once

        # Later sweeps resume from the files the budget stopped at.
        for _ in range(3):
            await fw.scan_source(source, chunk_budget=budget)
        assert sorted(ingested) == sorted(str(folder / f"d{i}.md") for i in range(6))
        done = store.db.execute(
            "SELECT COUNT(*) FROM folder_file_state "
            "WHERE source_id = ? AND status = 'done'", (sid,)).fetchone()[0]
        assert done == 6


class TestExistingSourcesIngestTheSameFiles:
    def test_no_extension_allowlist_is_imposed_by_default(self):
        # Pacing plus visibility is the guard; narrowing what is ingested stays an
        # explicit user choice, and None must stay distinct from an empty set.
        assert walk_filters({})["include_extensions"] is None
        assert walk_filters({"include_extensions": []})["include_extensions"] == set()

    def test_walk_filters_match_what_a_scan_applies(self, tmp_path):
        props = {"include_extensions": [".md"], "min_file_bytes": 4,
                 "ignore_patterns": ["skip/*"], "extra_skip_dirs": ["notes"]}
        filters = walk_filters(props)
        assert filters["include_extensions"] == {".md"}
        assert filters["min_size"] == 4
        assert filters["ignore_patterns"] == ["skip/*"]
        assert "notes" in filters["extra_skip_dirs"]

    def test_obsidian_skip_dirs_survive_the_shared_builder(self):
        assert ".obsidian" in walk_filters({}, "obsidian_vault")["extra_skip_dirs"]

    @pytest.mark.asyncio
    async def test_a_budgeted_scan_takes_the_same_file_set(self, store, tmp_path):
        # The budget decides HOW FAST, never WHAT. The union over budgeted sweeps
        # must equal what one unbudgeted sweep takes.
        folder = tmp_path / "repo"
        folder.mkdir()
        (folder / "keep.md").write_text("body")
        (folder / "code.py").write_text("print(1)")
        (folder / "data.json").write_text("{}")
        source = {"id": "s1", "uri": str(folder), "source_type": "local_folder",
                  "properties": "{}"}
        fw = FolderWatcher(store, MagicMock())
        unbudgeted = {p for p, _ in fw._walk(str(folder), **walk_filters({}))}
        assert unbudgeted == {str(folder / "keep.md"), str(folder / "code.py"),
                              str(folder / "data.json")}

        store.add_source(name="Mine", source_type="local_folder", uri=str(folder))
        pipe = MagicMock()
        pipe._dedup_enabled = False
        fw2 = FolderWatcher(store, pipe)
        seen: list[str] = []

        async def _ingest(file_path, source_id, namespace, props, old_ids, root=""):
            seen.append(file_path)
            return ["i-" + file_path], "done"

        fw2._ingest_file = _ingest  # type: ignore[assignment]
        source["id"] = store.get_source_by_uri(str(folder))["id"]
        for _ in range(4):
            await fw2.scan_source(source, chunk_budget=1)
        assert set(seen) == unbudgeted


class TestAddFolderResponseCarriesTheEstimate:
    def _app(self, store, watcher):
        from kiro_crew.knowledge.connectors.local_folder import LocalFolderConnector

        app = web.Application()
        state = MagicMock()
        state.knowledge_store = store
        app["state"] = state
        sync = MagicMock()
        sync.get_connector = lambda t: (
            LocalFolderConnector() if t in ("local_folder", "obsidian_vault") else None)
        app["knowledge_sync"] = sync
        app["knowledge_watcher"] = watcher
        app.router.add_post("/api/knowledge/sources", add_source)
        return app

    @pytest.mark.asyncio
    async def test_response_reports_scale_and_pacing(self, store, tmp_path):
        folder = tmp_path / "repo"
        folder.mkdir()
        # Two chunks' worth of bytes, so the estimate is not trivially 1.
        (folder / "big.md").write_text("word " * 2000)
        (folder / "small.md").write_text("hi")

        watcher = MagicMock()
        watcher._folder_watcher = FolderWatcher(store, MagicMock())
        async with TestClient(TestServer(self._app(store, watcher))) as client:
            with patch("kiro_crew.config.loader.KiroCrewConfig.load",
                       return_value=_cfg(300)):
                resp = await client.post("/api/knowledge/sources", json={
                    "name": "repo", "source_type": "local_folder", "uri": str(folder)})
            assert resp.status == 201
            data = await resp.json()
        assert data["file_count"] == 2
        assert data["estimated_chunks"] >= 3
        # One extraction call per chunk plus one summary call per file.
        assert data["estimated_llm_calls"] == data["estimated_chunks"] + 2
        assert data["chunk_budget_per_sweep"] == 300
        assert data["capped_file_count"] == 0

    @pytest.mark.asyncio
    async def test_response_reports_an_unbounded_budget_as_zero(self, store, tmp_path):
        folder = tmp_path / "repo"
        folder.mkdir()
        (folder / "a.md").write_text("body")
        watcher = MagicMock()
        watcher._folder_watcher = FolderWatcher(store, MagicMock())
        async with TestClient(TestServer(self._app(store, watcher))) as client:
            with patch("kiro_crew.config.loader.KiroCrewConfig.load",
                       return_value=_cfg(0)):
                resp = await client.post("/api/knowledge/sources", json={
                    "name": "repo", "source_type": "local_folder", "uri": str(folder)})
            data = await resp.json()
        assert data["chunk_budget_per_sweep"] == 0

    @pytest.mark.asyncio
    async def test_hostile_numeric_properties_do_not_500(self, store, tmp_path):
        # The reported crash path: 1e309 parses to inf, and int(inf) raises. Every
        # numeric property here reaches an int() coercion from this request body.
        folder = tmp_path / "repo"
        folder.mkdir()
        (folder / "a.md").write_text("body")
        watcher = MagicMock()
        watcher._folder_watcher = FolderWatcher(store, MagicMock())
        async with TestClient(TestServer(self._app(store, watcher))) as client:
            with patch("kiro_crew.config.loader.KiroCrewConfig.load",
                       return_value=_cfg(300)):
                resp = await client.post("/api/knowledge/sources", json={
                    "name": "repo", "source_type": "local_folder", "uri": str(folder),
                    "properties": {"chunk_budget": 1e309, "max_files": 1e309,
                                   "min_file_bytes": 1e309}})
            assert resp.status == 201
            data = await resp.json()
        # Each malformed value fell back to its default rather than erroring.
        assert data["chunk_budget_per_sweep"] == 300
        assert data["file_count"] == 1
        assert data["capped_file_count"] == 0

    @pytest.mark.asyncio
    async def test_files_beyond_the_cap_are_reported_separately(self, store, tmp_path):
        folder = tmp_path / "repo"
        folder.mkdir()
        for i in range(4):
            (folder / f"d{i}.md").write_text("body")
        watcher = MagicMock()
        watcher._folder_watcher = FolderWatcher(store, MagicMock())
        async with TestClient(TestServer(self._app(store, watcher))) as client:
            with patch("kiro_crew.config.loader.KiroCrewConfig.load",
                       return_value=_cfg(300)):
                resp = await client.post("/api/knowledge/sources", json={
                    "name": "repo", "source_type": "local_folder", "uri": str(folder),
                    "properties": {"max_files": 2}})
            data = await resp.json()
        assert data["capped_file_count"] == 2
        # The estimate describes the files that get ingested, not everything seen.
        assert data["estimated_llm_calls"] == data["estimated_chunks"] + 2


class TestEstimator:
    def test_empty_walk_costs_nothing(self):
        assert estimate_scan_cost([]) == {"files": 0, "capped": 0, "chunks": 0,
                                          "llm_calls": 0}

    def test_a_vanished_file_contributes_nothing(self, tmp_path):
        # The scan will not find it either, so it must not inflate the estimate.
        cost = estimate_scan_cost([(str(tmp_path / "gone.md"), 1.0)])
        assert cost["files"] == 1
        assert cost["chunks"] == 0

    def test_estimate_is_capped_per_file(self, tmp_path):
        # A single huge file cannot exceed the chunker's own per-file ceiling.
        from kiro_crew.knowledge.chunker import MAX_CHUNKS_PER_FILE

        huge = tmp_path / "huge.md"
        huge.write_text("word " * 400_000)
        assert estimate_scan_cost([(str(huge), 1.0)])["chunks"] == MAX_CHUNKS_PER_FILE

    def test_newest_files_are_the_ones_kept_under_the_cap(self, tmp_path):
        new = tmp_path / "new.md"
        old = tmp_path / "old.md"
        new.write_text("word " * 2000)
        old.write_text("x")
        cost = estimate_scan_cost([(str(old), 1.0), (str(new), 99.0)], max_files=1)
        assert cost["files"] == 1 and cost["capped"] == 1
        assert cost["chunks"] == estimate_scan_cost([(str(new), 99.0)])["chunks"]

    @pytest.mark.parametrize("bad", ["5000", None, True, 0, -1, [],
                                     1e309, float("inf"), float("nan")])
    def test_junk_max_files_falls_back_to_the_default(self, bad: object):
        assert max_files_prop({"max_files": bad}) == DEFAULT_MAX_FILES

    def test_max_files_reads_a_usable_value(self):
        assert max_files_prop({"max_files": 12}) == 12
