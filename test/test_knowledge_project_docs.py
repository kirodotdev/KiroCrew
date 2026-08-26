"""Tests for project-document auto-registration (knowledge/project_docs.py)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.knowledge.doc_filter import DOC_EXTENSIONS
from kiro_crew.knowledge.project_docs import (
    PROJECT_DOCS_KIND,
    SOURCE_KIND_PROP,
    discover_and_register,
    ensure_project_doc_source,
    is_project_doc_source,
    project_source_properties,
    project_source_still_valid,
    resolve_repo_root,
)
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.knowledge.watcher import KnowledgeWatcher


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "k.db"))
    yield s
    s.close()


def _repo(base, name="proj"):
    root = base / name
    (root / ".git").mkdir(parents=True)
    (root / "docs").mkdir()
    return root


class TestResolveRepoRoot:
    def test_finds_nearest_git_ancestor(self, tmp_path):
        root = _repo(tmp_path)
        assert resolve_repo_root(str(root / "docs")) == str(root)

    def test_falls_back_to_the_directory_itself(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert resolve_repo_root(str(plain)) == str(plain)

    def test_missing_directory_is_refused(self, tmp_path):
        assert resolve_repo_root(str(tmp_path / "nope")) == ""

    def test_empty_input_refused(self):
        assert resolve_repo_root("") == ""

    def test_file_is_not_a_project_dir(self, tmp_path):
        f = tmp_path / "a.md"
        f.write_text("x")
        assert resolve_repo_root(str(f)) == ""

    def test_home_directory_repo_root_is_refused(self, tmp_path):
        # A dotfiles repo in $HOME would otherwise make ANY project dir under it
        # resolve to the whole home directory -- and register it.
        home = tmp_path / "home"
        (home / ".git").mkdir(parents=True)
        (home / "work" / "thing").mkdir(parents=True)
        with patch("kiro_crew.knowledge.project_docs.Path.home", return_value=home):
            assert resolve_repo_root(str(home / "work" / "thing")) == ""

    def test_sensitive_root_is_refused(self, tmp_path):
        root = _repo(tmp_path)
        with patch("kiro_crew.knowledge.project_docs.is_sensitive_path",
                   return_value=True):
            assert resolve_repo_root(str(root)) == ""


class TestProperties:
    def test_seeded_active_without_confirmation(self):
        props = project_source_properties()
        assert props["sync_status"] == "active"
        assert props["auto_added"] is True
        assert props[SOURCE_KIND_PROP] == PROJECT_DOCS_KIND

    def test_carries_the_document_filter(self):
        props = project_source_properties()
        assert set(props["include_extensions"]) == DOC_EXTENSIONS
        assert props["min_file_bytes"] > 0
        assert props["ignore_patterns"]
        assert props["extra_skip_dirs"]

    def test_kind_predicate(self):
        assert is_project_doc_source(project_source_properties())
        assert not is_project_doc_source({"auto_added": True})
        assert not is_project_doc_source({SOURCE_KIND_PROP: PROJECT_DOCS_KIND})


class TestRegistration:
    def test_registers_as_active_folder_source(self, store, tmp_path):
        root = _repo(tmp_path)
        sid, created = ensure_project_doc_source(store, str(root))
        assert created and sid
        row = store.db.execute(
            "SELECT source_type, uri, properties FROM sources WHERE id = ?", (sid,)
        ).fetchone()
        assert row["source_type"] == "local_folder"
        assert row["uri"] == str(root)
        assert json.loads(row["properties"])["sync_status"] == "active"

    def test_second_call_reuses_the_row(self, store, tmp_path):
        root = _repo(tmp_path)
        sid, _ = ensure_project_doc_source(store, str(root))
        again, created = ensure_project_doc_source(store, str(root))
        assert again == sid and created is False

    def test_hand_added_folder_is_reused_not_shadowed(self, store, tmp_path):
        root = _repo(tmp_path)
        manual = store.add_source(name="Mine", source_type="local_folder",
                                  uri=str(root), properties={"sync_status": "paused"})
        sid, created = ensure_project_doc_source(store, str(root))
        assert sid == manual and created is False
        # Their explicit choice survives: properties are not overwritten.
        props = json.loads(store.db.execute(
            "SELECT properties FROM sources WHERE id = ?", (sid,)).fetchone()["properties"])
        assert props["sync_status"] == "paused"

    def test_dismissed_root_is_not_re_registered(self, store, tmp_path):
        root = _repo(tmp_path)
        sid, _ = ensure_project_doc_source(store, str(root))
        store.delete_source_cascade(sid, dismiss_uri=str(root))
        assert ensure_project_doc_source(store, str(root)) == (None, False)

    def test_discover_registers_each_distinct_root_once(self, store, tmp_path):
        root = _repo(tmp_path, "a")
        other = _repo(tmp_path, "b")
        # Two slots inside the SAME repo plus one in another.
        created = discover_and_register(
            store, [str(root / "docs"), str(root), str(other)])
        assert len(created) == 2
        assert discover_and_register(store, [str(root), str(other)]) == []

    def test_discover_skips_unusable_dirs(self, store, tmp_path):
        assert discover_and_register(store, ["", str(tmp_path / "nope")]) == []


class TestScanTimeValidation:
    def test_registered_path_still_valid(self, tmp_path):
        root = _repo(tmp_path)
        assert project_source_still_valid(str(root))

    def test_path_swapped_for_a_link_elsewhere_is_refused(self, tmp_path):
        root = _repo(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        import shutil
        shutil.rmtree(root)
        root.symlink_to(outside, target_is_directory=True)
        assert not project_source_still_valid(str(root))

    def test_missing_path_refused(self, tmp_path):
        assert not project_source_still_valid(str(tmp_path / "gone"))

    def test_sensitive_path_refused(self, tmp_path):
        root = _repo(tmp_path)
        with patch("kiro_crew.knowledge.project_docs.is_sensitive_path",
                   return_value=True):
            assert not project_source_still_valid(str(root))

    def test_empty_refused(self):
        assert not project_source_still_valid("")


def _watcher(store, project_dirs=None):
    pipeline = MagicMock()
    pipeline._dedup_enabled = False
    w = KnowledgeWatcher(store=store, pipeline=pipeline, project_dirs=project_dirs)
    w._folder_watcher = MagicMock()
    w._folder_watcher.scan_source = AsyncMock(return_value={})
    w._maybe_reembed_stale = AsyncMock()
    return w


def _n_sources(store):
    return store.db.execute("SELECT COUNT(*) FROM sources").fetchone()[0]


class TestWatcherDiscovery:
    @pytest.mark.asyncio
    async def test_flag_off_registers_nothing(self, store, tmp_path):
        root = _repo(tmp_path)
        w = _watcher(store, lambda: [str(root)])
        cfg = MagicMock()
        cfg.knowledge.auto_register_project_docs = False
        with patch("kiro_crew.knowledge.watcher.KiroCrewConfig.load", return_value=cfg):
            await w._discover_project_docs()
        assert _n_sources(store) == 0

    @pytest.mark.asyncio
    async def test_registers_and_sel_logs(self, store, tmp_path):
        root = _repo(tmp_path)
        w = _watcher(store, lambda: [str(root)])
        cfg = MagicMock()
        cfg.knowledge.auto_register_project_docs = True
        sel_spy = MagicMock()
        with patch("kiro_crew.knowledge.watcher.KiroCrewConfig.load", return_value=cfg), \
                patch("kiro_crew.knowledge.watcher.sel", lambda: sel_spy):
            await w._discover_project_docs()
        assert _n_sources(store) == 1
        # Spending LLM extraction on the user's files is an auditable mutation.
        assert sel_spy.log_tool_invocation.call_count == 1
        kwargs = sel_spy.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "knowledge.source.auto_add"

    @pytest.mark.asyncio
    async def test_no_resolver_is_a_noop(self, store):
        w = _watcher(store, None)
        cfg = MagicMock()
        cfg.knowledge.auto_register_project_docs = True
        with patch("kiro_crew.knowledge.watcher.KiroCrewConfig.load", return_value=cfg):
            await w._discover_project_docs()
        assert _n_sources(store) == 0

    @pytest.mark.asyncio
    async def test_repeated_sweeps_do_not_duplicate(self, store, tmp_path):
        root = _repo(tmp_path)
        w = _watcher(store, lambda: [str(root)])
        cfg = MagicMock()
        cfg.knowledge.auto_register_project_docs = True
        with patch("kiro_crew.knowledge.watcher.KiroCrewConfig.load", return_value=cfg):
            await w._discover_project_docs()
            await w._discover_project_docs()
        assert _n_sources(store) == 1

    @pytest.mark.asyncio
    async def test_failure_does_not_break_the_sweep_and_logs_once(self, store, tmp_path):
        w = _watcher(store, lambda: ["/x"])
        cfg = MagicMock()
        cfg.knowledge.auto_register_project_docs = True
        with patch("kiro_crew.knowledge.watcher.KiroCrewConfig.load", return_value=cfg), \
                patch("kiro_crew.knowledge.watcher.discover_project_docs",
                      side_effect=RuntimeError("boom")), \
                patch("kiro_crew.knowledge.watcher.logger") as log:
            await w._discover_project_docs()
            await w._discover_project_docs()
        assert log.warning.call_count == 1

    @pytest.mark.asyncio
    async def test_project_docs_errors_do_not_mask_drop_folder_errors(self, store):
        # Separate signatures: sharing one would let a failure in either suppress
        # the FIRST log of a failure in the other.
        w = _watcher(store, lambda: ["/x"])
        w._discover_error_sig = "RuntimeError:boom"
        cfg = MagicMock()
        cfg.knowledge.auto_register_project_docs = True
        with patch("kiro_crew.knowledge.watcher.KiroCrewConfig.load", return_value=cfg), \
                patch("kiro_crew.knowledge.watcher.discover_project_docs",
                      side_effect=RuntimeError("boom")), \
                patch("kiro_crew.knowledge.watcher.logger") as log:
            await w._discover_project_docs()
        assert log.warning.call_count == 1

    @pytest.mark.asyncio
    async def test_scan_awaits_project_discovery(self, store):
        w = _watcher(store)
        w._discover_drop_folder = AsyncMock()
        w._discover_project_docs = AsyncMock()
        w._maybe_dedup_sweep = AsyncMock()
        await w._scan()
        w._discover_project_docs.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_project_source_outside_workspace_is_still_scanned(self, store, tmp_path):
        # The drop folder must stay inside the workspace; a project repo root is
        # outside it by design. Applying workspace containment to both would skip
        # every project source with a 'denied' audit event.
        root = _repo(tmp_path)
        sid, _ = ensure_project_doc_source(store, str(root))
        w = _watcher(store)
        w._discover_drop_folder = AsyncMock()
        w._discover_project_docs = AsyncMock()
        w._maybe_dedup_sweep = AsyncMock()
        with patch("kiro_crew.knowledge.watcher.default_project_dir",
                   return_value="/somewhere/else"):
            await w._scan()
        assert w._folder_watcher.scan_source.await_count == 1
        assert w._folder_watcher.scan_source.await_args.args[0]["id"] == sid

    @pytest.mark.asyncio
    async def test_swapped_project_source_is_skipped(self, store, tmp_path):
        root = _repo(tmp_path)
        ensure_project_doc_source(store, str(root))
        w = _watcher(store)
        w._discover_drop_folder = AsyncMock()
        w._discover_project_docs = AsyncMock()
        w._maybe_dedup_sweep = AsyncMock()
        with patch("kiro_crew.knowledge.watcher.project_source_still_valid",
                   return_value=False):
            await w._scan()
        w._folder_watcher.scan_source.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_project_source_gets_the_chunk_budget(self, store, tmp_path):
        root = _repo(tmp_path)
        ensure_project_doc_source(store, str(root))
        w = _watcher(store)
        w._discover_drop_folder = AsyncMock()
        w._discover_project_docs = AsyncMock()
        w._maybe_dedup_sweep = AsyncMock()
        with patch.object(KnowledgeWatcher, "_chunk_budget", staticmethod(lambda: 77)):
            await w._scan()
        assert w._folder_watcher.scan_source.await_args.kwargs["chunk_budget"] == 77

    @pytest.mark.asyncio
    async def test_hand_added_folder_gets_the_folder_budget(self, store, tmp_path):
        # A hand-added folder is paced too: it is still ingested in full, but
        # across sweeps rather than as one unbounded burst.
        root = _repo(tmp_path)
        store.add_source(name="Mine", source_type="local_folder", uri=str(root),
                         properties={"sync_status": "active"})
        w = _watcher(store)
        w._discover_drop_folder = AsyncMock()
        w._discover_project_docs = AsyncMock()
        w._maybe_dedup_sweep = AsyncMock()
        with patch.object(KnowledgeWatcher, "_chunk_budget", staticmethod(lambda: 77)), \
                patch("kiro_crew.knowledge.watcher.folder_chunk_budget",
                      return_value=42):
            await w._scan()
        assert w._folder_watcher.scan_source.await_args.kwargs["chunk_budget"] == 42


class TestWatcherResolverWiring:
    """The dashboard owns chat-slot state; the watcher must not import it."""

    @pytest.mark.asyncio
    async def test_start_watcher_injects_a_slot_dir_resolver(self, store, tmp_path):
        from aiohttp import web

        from kiro_crew.dashboard.handlers.knowledge import _start_watcher_async

        root = _repo(tmp_path)
        app = web.Application()
        state = MagicMock()
        state.knowledge_store = store
        state._slots = {"chat-1": MagicMock(project=str(root))}
        app["state"] = state
        app["knowledge_pipeline"] = MagicMock()
        try:
            await _start_watcher_async(app)
            watcher = app["knowledge_watcher"]
            assert watcher._project_dirs is not None
            assert watcher._project_dirs() == [str(root)]
        finally:
            task = app.get("_knowledge_watcher_task")
            if task:
                task.cancel()
            w = app.get("knowledge_watcher")
            if w:
                await w.stop()


class TestScheduledDedup:
    @pytest.mark.asyncio
    async def test_first_pass_previews_then_later_passes_apply(self, store):
        # A scheduled sweep deletes unattended, with no human command per pass, so
        # the first pass only logs what it would do -- that makes a backlog collapse
        # on an existing Library observable before anything is removed.
        w = _watcher(store)
        cfg = MagicMock()
        cfg.knowledge.dedup_every_n_sweeps = 1
        with patch("kiro_crew.knowledge.watcher.KiroCrewConfig.load", return_value=cfg), \
                patch("kiro_crew.knowledge.watcher.dedup_sweep",
                      return_value=[{"loser": "a", "winner": "b", "reason": "exact"}]) as sweep:
            w._sweep_count += 1
            await w._maybe_dedup_sweep()
            assert sweep.call_args.kwargs["apply"] is False
            w._sweep_count += 1
            await w._maybe_dedup_sweep()
            assert sweep.call_args.kwargs["apply"] is True

    @pytest.mark.asyncio
    async def test_runs_on_the_cadence(self, store):
        w = _watcher(store)
        w._dedup_applied_once = True
        cfg = MagicMock()
        cfg.knowledge.dedup_every_n_sweeps = 3
        with patch("kiro_crew.knowledge.watcher.KiroCrewConfig.load", return_value=cfg), \
                patch("kiro_crew.knowledge.watcher.dedup_sweep",
                      return_value=[{"loser": "a"}]) as sweep:
            for _ in range(3):
                w._sweep_count += 1
                await w._maybe_dedup_sweep()
        assert sweep.call_count == 1

    @pytest.mark.asyncio
    async def test_zero_disables(self, store):
        w = _watcher(store)
        cfg = MagicMock()
        cfg.knowledge.dedup_every_n_sweeps = 0
        with patch("kiro_crew.knowledge.watcher.KiroCrewConfig.load", return_value=cfg), \
                patch("kiro_crew.knowledge.watcher.dedup_sweep") as sweep:
            w._sweep_count += 1
            await w._maybe_dedup_sweep()
        sweep.assert_not_called()

    @pytest.mark.asyncio
    async def test_failure_is_contained(self, store):
        w = _watcher(store)
        cfg = MagicMock()
        cfg.knowledge.dedup_every_n_sweeps = 1
        with patch("kiro_crew.knowledge.watcher.KiroCrewConfig.load", return_value=cfg), \
                patch("kiro_crew.knowledge.watcher.dedup_sweep",
                      side_effect=RuntimeError("boom")):
            w._sweep_count += 1
            await w._maybe_dedup_sweep()  # must not raise
