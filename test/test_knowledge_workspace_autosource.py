"""Tests for auto-discovery of the workspace documents (drop) folder.

Covers ``knowledge.autosource`` and the recurring discovery step the
``KnowledgeWatcher`` sweep runs: the folder is never created, it is picked up
when it appears later, and registration is idempotent.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.knowledge.autosource import (
    DEFAULT_DROP_DIRNAME,
    DROP_SOURCE_NAME,
    auto_source_still_contained,
    discover_and_register,
    ensure_drop_source,
    resolve_drop_folder,
)
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.knowledge.watcher import KnowledgeWatcher

_AUTOSOURCE = "kiro_crew.knowledge.autosource"
_WATCHER = "kiro_crew.knowledge.watcher"


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "test.db"))
    yield s
    s.close()


def _sources(store) -> list[dict]:
    return [dict(r) for r in store.db.execute("SELECT * FROM sources").fetchall()]


def _props(store, uri) -> dict:
    raw = store.get_source_by_uri(uri)["properties"]
    return json.loads(raw) if isinstance(raw, str) else (raw or {})


def _cfg(enabled: bool = True, dirname: str = DEFAULT_DROP_DIRNAME):
    cfg = MagicMock()
    cfg.knowledge.auto_discover_folder = enabled
    cfg.knowledge.auto_discover_dirname = dirname
    return cfg


def _watcher(store) -> KnowledgeWatcher:
    pipeline = MagicMock()
    # No embedder configured -> _scan skips the self-heal re-embed branch.
    pipeline.embedder = None
    return KnowledgeWatcher(store=store, pipeline=pipeline)


class TestResolveDropFolder:
    def test_returns_path_when_folder_exists(self, tmp_path):
        (tmp_path / DEFAULT_DROP_DIRNAME).mkdir()
        assert resolve_drop_folder(str(tmp_path)) == str(tmp_path / DEFAULT_DROP_DIRNAME)

    def test_returns_empty_when_folder_absent(self, tmp_path):
        assert resolve_drop_folder(str(tmp_path)) == ""

    def test_does_not_create_the_folder(self, tmp_path):
        resolve_drop_folder(str(tmp_path))
        assert not (tmp_path / DEFAULT_DROP_DIRNAME).exists()

    def test_returns_empty_for_a_file_of_the_same_name(self, tmp_path):
        (tmp_path / DEFAULT_DROP_DIRNAME).write_text("not a dir")
        assert resolve_drop_folder(str(tmp_path)) == ""

    def test_returns_empty_when_base_is_empty(self):
        assert resolve_drop_folder("") == ""

    def test_empty_folder_still_resolves(self, tmp_path):
        """An empty drop folder is a valid, registerable source."""
        (tmp_path / DEFAULT_DROP_DIRNAME).mkdir()
        assert resolve_drop_folder(str(tmp_path)) != ""

    def test_rejects_sensitive_path(self, tmp_path):
        (tmp_path / DEFAULT_DROP_DIRNAME).mkdir()
        with patch(f"{_AUTOSOURCE}.is_sensitive_path", return_value=True):
            assert resolve_drop_folder(str(tmp_path)) == ""

    @pytest.mark.parametrize("bad", ["../escape", "a/b", "..", ".", "/abs"])
    def test_rejects_dirname_that_escapes_the_workspace(self, tmp_path, bad):
        assert resolve_drop_folder(str(tmp_path), bad) == ""

    def test_backslash_dirname_stays_confined(self, tmp_path):
        """POSIX treats '\\' as a literal filename char, Windows as a separator.

        Either way the result must not escape: on POSIX it resolves to a literal
        file inside the workspace (not a dir -> ""), on Windows the segment guard
        rejects it.
        """
        assert resolve_drop_folder(str(tmp_path), "a\\b") == ""

    def test_symlink_pointing_outside_workspace_is_rejected(self, tmp_path):
        """The segment guard only constrains the config STRING, not the FS."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("private")
        (workspace / DEFAULT_DROP_DIRNAME).symlink_to(outside, target_is_directory=True)
        assert resolve_drop_folder(str(workspace)) == ""

    def test_symlink_inside_workspace_is_allowed(self, tmp_path):
        """Containment, not symlink-phobia: a link that stays inside is fine."""
        workspace = tmp_path / "ws"
        (workspace / "real").mkdir(parents=True)
        (workspace / DEFAULT_DROP_DIRNAME).symlink_to(
            workspace / "real", target_is_directory=True
        )
        assert resolve_drop_folder(str(workspace)) == str((workspace / "real").resolve())

    def test_sibling_prefix_directory_is_not_treated_as_contained(self, tmp_path):
        """Guards against a naive startswith: /ws-evil must not pass for /ws."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        evil = tmp_path / "ws-evil"
        evil.mkdir()
        (workspace / DEFAULT_DROP_DIRNAME).symlink_to(evil, target_is_directory=True)
        assert resolve_drop_folder(str(workspace)) == ""

    def test_unreadable_folder_still_resolves(self, tmp_path):
        """is_dir() stats the entry, not its contents -- pin the behaviour."""
        drop = tmp_path / DEFAULT_DROP_DIRNAME
        drop.mkdir(mode=0o000)
        try:
            assert resolve_drop_folder(str(tmp_path)) == str(drop.resolve())
        finally:
            drop.chmod(0o755)

    def test_honours_custom_dirname(self, tmp_path):
        (tmp_path / "docs").mkdir()
        assert resolve_drop_folder(str(tmp_path), "docs") == str(tmp_path / "docs")


class TestEnsureDropSource:
    def test_creates_folder_source(self, store, tmp_path):
        source_id, created = ensure_drop_source(store, str(tmp_path))
        assert created is True
        row = store.get_source_by_uri(str(tmp_path))
        assert row["id"] == source_id
        assert row["source_type"] == "local_folder"
        assert row["name"] == DROP_SOURCE_NAME

    def test_seeded_active_so_the_watcher_scans_it(self, store, tmp_path):
        ensure_drop_source(store, str(tmp_path))
        status = _props(store, str(tmp_path))["sync_status"]
        assert status == "active"
        assert status not in ("paused", "pending_confirmation")

    def test_marks_source_auto_added(self, store, tmp_path):
        ensure_drop_source(store, str(tmp_path))
        assert _props(store, str(tmp_path))["auto_added"] is True

    def test_second_call_reuses_the_row(self, store, tmp_path):
        first, created_first = ensure_drop_source(store, str(tmp_path))
        second, created_second = ensure_drop_source(store, str(tmp_path))
        assert (created_first, created_second) == (True, False)
        assert first == second
        assert len(_sources(store)) == 1

    def test_unique_uri_race_is_treated_as_pre_existing(self, store, tmp_path):
        uri = str(tmp_path)
        winner = store.add_source(
            name="raced", source_type="local_folder", uri=uri, properties={},
        )
        real_get = store.get_source_by_uri
        calls = {"n": 0}

        def flaky_get(u):
            calls["n"] += 1
            return None if calls["n"] == 1 else real_get(u)

        with patch.object(store, "get_source_by_uri", side_effect=flaky_get):
            source_id, created = ensure_drop_source(store, uri)
        assert (source_id, created) == (winner, False)

    def test_user_registered_folder_is_not_shadowed(self, store, tmp_path):
        existing = store.add_source(
            name="My Docs", source_type="local_folder", uri=str(tmp_path),
            properties={"sync_status": "paused"},
        )
        source_id, created = ensure_drop_source(store, str(tmp_path))
        assert (source_id, created) == (existing, False)
        assert _sources(store)[0]["name"] == "My Docs"

    def test_existing_row_of_a_different_source_type_is_reused_not_duplicated(
        self, store, tmp_path
    ):
        """get_source_by_uri matches on uri alone; pin that we never double-add."""
        existing = store.add_source(
            name="My Vault", source_type="obsidian_vault", uri=str(tmp_path),
            properties={},
        )
        source_id, created = ensure_drop_source(store, str(tmp_path))
        assert (source_id, created) == (existing, False)
        rows = _sources(store)
        assert len(rows) == 1
        assert rows[0]["source_type"] == "obsidian_vault"


class TestDiscoverAndRegister:
    def test_registers_when_folder_present(self, store, tmp_path):
        (tmp_path / DEFAULT_DROP_DIRNAME).mkdir()
        assert discover_and_register(store, str(tmp_path)) is not None
        assert len(_sources(store)) == 1

    def test_no_op_when_folder_absent(self, store, tmp_path):
        assert discover_and_register(store, str(tmp_path)) is None
        assert _sources(store) == []

    def test_returns_none_once_already_registered(self, store, tmp_path):
        (tmp_path / DEFAULT_DROP_DIRNAME).mkdir()
        assert discover_and_register(store, str(tmp_path)) is not None
        assert discover_and_register(store, str(tmp_path)) is None
        assert len(_sources(store)) == 1


class TestDismissal:
    """A deleted auto-source must stay deleted while the folder still exists."""

    def test_dismissed_folder_is_not_re_registered(self, store, tmp_path):
        drop = tmp_path / DEFAULT_DROP_DIRNAME
        drop.mkdir()
        discover_and_register(store, str(tmp_path))
        # Simulate the delete handler: cascade removes the row, tombstone stays.
        source_id = store.get_source_by_uri(str(drop.resolve()))["id"]
        store.delete_source_cascade(source_id)
        store.dismiss_auto_source(str(drop.resolve()))
        assert discover_and_register(store, str(tmp_path)) is None
        assert _sources(store) == []

    def test_delete_without_dismissal_would_resurrect(self, store, tmp_path):
        """Pins WHY the tombstone exists: the row is the only other marker."""
        drop = tmp_path / DEFAULT_DROP_DIRNAME
        drop.mkdir()
        discover_and_register(store, str(tmp_path))
        source_id = store.get_source_by_uri(str(drop.resolve()))["id"]
        store.delete_source_cascade(source_id)
        # No dismissal recorded -> rediscovered.
        assert discover_and_register(store, str(tmp_path)) is not None

    def test_dismissal_survives_cascade(self, store, tmp_path):
        uri = str(tmp_path)
        source_id = store.add_source(
            name="x", source_type="local_folder", uri=uri, properties={},
        )
        store.dismiss_auto_source(uri)
        store.delete_source_cascade(source_id)
        assert store.is_auto_source_dismissed(uri) is True

    def test_undismiss_re_opts_in(self, store, tmp_path):
        drop = tmp_path / DEFAULT_DROP_DIRNAME
        drop.mkdir()
        store.dismiss_auto_source(str(drop.resolve()))
        assert discover_and_register(store, str(tmp_path)) is None
        store.undismiss_auto_source(str(drop.resolve()))
        assert discover_and_register(store, str(tmp_path)) is not None

    def test_dismiss_is_idempotent(self, store, tmp_path):
        store.dismiss_auto_source(str(tmp_path))
        store.dismiss_auto_source(str(tmp_path))
        assert store.is_auto_source_dismissed(str(tmp_path)) is True

    def test_unrelated_uri_is_not_dismissed(self, store, tmp_path):
        store.dismiss_auto_source(str(tmp_path / "a"))
        assert store.is_auto_source_dismissed(str(tmp_path / "b")) is False


class TestDeleteDiscoveryAtomicity:
    """The tombstone and the delete must land in ONE transaction.

    Discovery runs on a recurring sweep, so a tombstone written *after* the
    cascade commits leaves a window where a sweep sees neither a source row nor
    a tombstone and re-creates the source the user just deleted.
    """

    def test_cascade_writes_tombstone_in_same_transaction(self, store, tmp_path):
        uri = str(tmp_path)
        sid = store.add_source(
            name="Workspace Documents", source_type="local_folder", uri=uri,
            properties={"sync_status": "active", "auto_added": True},
        )
        store.delete_source_cascade(sid, dismiss_uri=uri)
        assert store.get_source_by_uri(uri) is None
        assert store.is_auto_source_dismissed(uri) is True

    def test_cascade_without_dismiss_uri_leaves_no_tombstone(self, store, tmp_path):
        uri = str(tmp_path)
        sid = store.add_source(
            name="My Docs", source_type="local_folder", uri=uri, properties={},
        )
        store.delete_source_cascade(sid)
        assert store.is_auto_source_dismissed(uri) is False

    def test_tombstone_is_written_inside_the_delete_transaction(self, store, tmp_path):
        """Trace the real statement stream: BEGIN -> tombstone -> ... -> COMMIT.

        The tombstone must be inside the transaction and precede the source
        delete, so no committed state ever has the row gone without a tombstone
        -- which is what a concurrent sweep would exploit to resurrect it.
        """
        uri = str(tmp_path)
        sid = store.add_source(
            name="Workspace Documents", source_type="local_folder", uri=uri,
            properties={"sync_status": "active", "auto_added": True},
        )
        stmts: list[str] = []
        store.db.set_trace_callback(lambda s: stmts.append(" ".join(s.split()).upper()))
        try:
            store.delete_source_cascade(sid, dismiss_uri=uri)
        finally:
            store.db.set_trace_callback(None)

        def index_of(needle: str) -> int:
            for i, s in enumerate(stmts):
                if needle in s:
                    return i
            raise AssertionError(f"{needle!r} not traced; got {stmts}")

        begin = index_of("BEGIN IMMEDIATE")
        tombstone = index_of("INSERT OR REPLACE INTO DISMISSED_AUTO_SOURCES")
        src_delete = index_of("DELETE FROM SOURCES WHERE ID")
        commit = index_of("COMMIT")
        assert begin < tombstone < src_delete < commit

    def test_failed_delete_rolls_back_the_tombstone_too(self, store, tmp_path):
        """One transaction means a failure leaves neither effect behind."""
        uri = str(tmp_path)
        sid = store.add_source(
            name="Workspace Documents", source_type="local_folder", uri=uri,
            properties={"sync_status": "active", "auto_added": True},
        )
        # An unsupported bind type fails the cascade partway through.
        with pytest.raises(Exception):
            store.delete_source_cascade(sid, dismiss_uri=object())  # type: ignore[arg-type]
        assert store.get_source_by_uri(uri) is not None
        assert store.is_auto_source_dismissed(uri) is False

    def test_discovery_refuses_a_dismissed_uri_atomically(self, store, tmp_path):
        drop = tmp_path / DEFAULT_DROP_DIRNAME
        drop.mkdir()
        uri = str(drop.resolve())
        store.dismiss_auto_source(uri)
        # The atomic store path must refuse, not just the caller-side check.
        source_id, created = store.create_auto_source_unless_dismissed(
            name="Workspace Documents", source_type="local_folder", uri=uri,
            properties={"sync_status": "active", "auto_added": True},
        )
        assert (source_id, created) == (None, False)
        assert discover_and_register(store, str(tmp_path)) is None
        assert _sources(store) == []

    def test_atomic_create_reuses_existing_row(self, store, tmp_path):
        uri = str(tmp_path)
        existing = store.add_source(
            name="My Docs", source_type="local_folder", uri=uri, properties={},
        )
        source_id, created = store.create_auto_source_unless_dismissed(
            name="Workspace Documents", source_type="local_folder", uri=uri,
            properties={},
        )
        assert (source_id, created) == (existing, False)

    def test_atomic_create_inserts_when_clean(self, store, tmp_path):
        source_id, created = store.create_auto_source_unless_dismissed(
            name="Workspace Documents", source_type="local_folder",
            uri=str(tmp_path), properties={"sync_status": "active"},
        )
        assert created is True
        assert store.get_source_by_uri(str(tmp_path))["id"] == source_id


class TestWatcherDiscovery:
    @pytest.mark.asyncio
    async def test_flag_off_registers_nothing(self, store, tmp_path):
        (tmp_path / DEFAULT_DROP_DIRNAME).mkdir()
        with patch(f"{_WATCHER}.KiroCrewConfig.load", return_value=_cfg(enabled=False)), \
                patch(f"{_WATCHER}.default_project_dir", return_value=str(tmp_path)):
            await _watcher(store)._discover_drop_folder()
        assert _sources(store) == []

    @pytest.mark.asyncio
    async def test_registers_existing_folder(self, store, tmp_path):
        (tmp_path / DEFAULT_DROP_DIRNAME).mkdir()
        with patch(f"{_WATCHER}.KiroCrewConfig.load", return_value=_cfg()), \
                patch(f"{_WATCHER}.default_project_dir", return_value=str(tmp_path)):
            await _watcher(store)._discover_drop_folder()
        assert len(_sources(store)) == 1

    @pytest.mark.asyncio
    async def test_folder_created_later_is_picked_up_on_a_later_sweep(self, store, tmp_path):
        """The point of recurring discovery: no restart needed."""
        w = _watcher(store)
        with patch(f"{_WATCHER}.KiroCrewConfig.load", return_value=_cfg()), \
                patch(f"{_WATCHER}.default_project_dir", return_value=str(tmp_path)):
            await w._discover_drop_folder()
            assert _sources(store) == []          # sweep 1: folder absent
            (tmp_path / DEFAULT_DROP_DIRNAME).mkdir()
            await w._discover_drop_folder()
            assert len(_sources(store)) == 1      # sweep 2: folder appeared

    @pytest.mark.asyncio
    async def test_repeated_sweeps_do_not_duplicate(self, store, tmp_path):
        (tmp_path / DEFAULT_DROP_DIRNAME).mkdir()
        w = _watcher(store)
        with patch(f"{_WATCHER}.KiroCrewConfig.load", return_value=_cfg()), \
                patch(f"{_WATCHER}.default_project_dir", return_value=str(tmp_path)):
            for _ in range(4):
                await w._discover_drop_folder()
        assert len(_sources(store)) == 1

    @pytest.mark.asyncio
    async def test_no_workspace_dir_is_a_no_op(self, store):
        with patch(f"{_WATCHER}.KiroCrewConfig.load", return_value=_cfg()), \
                patch(f"{_WATCHER}.default_project_dir", return_value=""):
            await _watcher(store)._discover_drop_folder()
        assert _sources(store) == []

    @pytest.mark.asyncio
    async def test_blank_dirname_falls_back_to_default(self, store, tmp_path):
        (tmp_path / DEFAULT_DROP_DIRNAME).mkdir()
        with patch(f"{_WATCHER}.KiroCrewConfig.load", return_value=_cfg(dirname="")), \
                patch(f"{_WATCHER}.default_project_dir", return_value=str(tmp_path)):
            await _watcher(store)._discover_drop_folder()
        assert len(_sources(store)) == 1

    @pytest.mark.asyncio
    async def test_discovery_failure_does_not_break_the_sweep(self, store, tmp_path):
        """Registered sources must still be scanned when discovery blows up."""
        drop = tmp_path / "already-registered"
        drop.mkdir()
        store.add_source(
            name="Existing", source_type="local_folder", uri=str(drop),
            properties={"sync_status": "active"},
        )
        w = _watcher(store)
        w._folder_watcher = MagicMock()
        w._folder_watcher.scan_source = AsyncMock(return_value={})
        with patch(f"{_WATCHER}.KiroCrewConfig.load", side_effect=RuntimeError("boom")):
            await w._scan()
        # Discovery raised, but the pre-existing source was still scanned.
        w._folder_watcher.scan_source.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_repeated_identical_failures_log_once(self, store):
        w = _watcher(store)
        with patch(f"{_WATCHER}.KiroCrewConfig.load", side_effect=RuntimeError("boom")), \
                patch(f"{_WATCHER}.logger") as log:
            await w._discover_drop_folder()
            await w._discover_drop_folder()
            await w._discover_drop_folder()
        assert log.warning.call_count == 1

    @pytest.mark.asyncio
    async def test_scan_runs_discovery_before_scanning_sources(self, store):
        """Discovery is wired into the sweep, not just callable in isolation."""
        w = _watcher(store)
        w._discover_drop_folder = AsyncMock()
        await w._scan()
        w._discover_drop_folder.assert_awaited_once()


class TestScanTimeContainment:
    """Registration-time containment is not enough -- re-check every sweep.

    The persisted URI is a literal path when the drop folder is a real
    directory, so swapping it for a symlink to an external tree afterwards would
    otherwise have os.walk follow it out of the workspace.
    """

    def test_contained_folder_passes(self, tmp_path):
        ws = tmp_path / "ws"
        drop = ws / DEFAULT_DROP_DIRNAME
        drop.mkdir(parents=True)
        assert auto_source_still_contained(str(drop), str(ws)) is True

    def test_folder_swapped_for_external_symlink_fails(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        drop = ws / DEFAULT_DROP_DIRNAME
        drop.symlink_to(outside, target_is_directory=True)
        assert auto_source_still_contained(str(drop), str(ws)) is False

    def test_sibling_prefix_fails(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        evil = tmp_path / "ws-evil"
        evil.mkdir()
        assert auto_source_still_contained(str(evil), str(ws)) is False

    def test_sensitive_target_fails(self, tmp_path):
        ws = tmp_path / "ws"
        drop = ws / DEFAULT_DROP_DIRNAME
        drop.mkdir(parents=True)
        with patch(f"{_AUTOSOURCE}.is_sensitive_path", return_value=True):
            assert auto_source_still_contained(str(drop), str(ws)) is False

    def test_empty_base_fails(self, tmp_path):
        assert auto_source_still_contained(str(tmp_path), "") is False

    @pytest.mark.asyncio
    async def test_sweep_skips_auto_source_that_escaped(self, store, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("private")
        drop = ws / DEFAULT_DROP_DIRNAME
        drop.symlink_to(outside, target_is_directory=True)
        store.add_source(
            name=DROP_SOURCE_NAME, source_type="local_folder", uri=str(drop),
            properties={"sync_status": "active", "auto_added": True},
        )
        w = _watcher(store)
        w._folder_watcher = MagicMock()
        w._folder_watcher.scan_source = AsyncMock(return_value={})
        w._discover_drop_folder = AsyncMock()
        with patch(f"{_WATCHER}.default_project_dir", return_value=str(ws)):
            await w._scan()
        w._folder_watcher.scan_source.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sweep_still_scans_a_contained_auto_source(self, store, tmp_path):
        ws = tmp_path / "ws"
        drop = ws / DEFAULT_DROP_DIRNAME
        drop.mkdir(parents=True)
        store.add_source(
            name=DROP_SOURCE_NAME, source_type="local_folder", uri=str(drop),
            properties={"sync_status": "active", "auto_added": True},
        )
        w = _watcher(store)
        w._folder_watcher = MagicMock()
        w._folder_watcher.scan_source = AsyncMock(return_value={})
        w._discover_drop_folder = AsyncMock()
        with patch(f"{_WATCHER}.default_project_dir", return_value=str(ws)):
            await w._scan()
        w._folder_watcher.scan_source.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hand_added_source_outside_workspace_is_untouched(self, store, tmp_path):
        """A user-registered folder outside the workspace is their choice."""
        ws = tmp_path / "ws"
        ws.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        store.add_source(
            name="My Docs", source_type="local_folder", uri=str(elsewhere),
            properties={"sync_status": "active"},
        )
        w = _watcher(store)
        w._folder_watcher = MagicMock()
        w._folder_watcher.scan_source = AsyncMock(return_value={})
        w._discover_drop_folder = AsyncMock()
        with patch(f"{_WATCHER}.default_project_dir", return_value=str(ws)):
            await w._scan()
        w._folder_watcher.scan_source.assert_awaited_once()


class TestPauseSurvivesRestart:
    """An empty paused auto-source must not be orphan-cleaned on restart.

    ``_migrate()`` runs on every KnowledgeStore construction (every gateway
    start) and deletes sources with no items. An empty drop folder has none, so
    without the folder-source carve-out a paused row would be deleted and then
    re-created as ``active`` by discovery -- silently un-pausing it.
    """

    def test_empty_paused_auto_source_survives_reopen(self, tmp_path):
        db = str(tmp_path / "k.db")
        drop = tmp_path / DEFAULT_DROP_DIRNAME
        drop.mkdir()
        s1 = KnowledgeStore(db)
        try:
            sid = s1.add_source(
                name=DROP_SOURCE_NAME, source_type="local_folder", uri=str(drop),
                properties={"sync_status": "paused", "auto_added": True,
                            "scan_paused": True},
            )
        finally:
            s1.close()
        # Reopening runs the orphan cleanup, as a gateway restart would.
        s2 = KnowledgeStore(db)
        try:
            row = s2.get_source_by_uri(str(drop))
            assert row is not None, "paused empty folder source was orphan-deleted"
            assert row["id"] == sid
            props = json.loads(row["properties"])
            assert props["sync_status"] == "paused"
        finally:
            s2.close()

    def test_empty_active_folder_source_also_survives(self, tmp_path):
        db = str(tmp_path / "k.db")
        s1 = KnowledgeStore(db)
        try:
            s1.add_source(
                name="My Docs", source_type="local_folder", uri=str(tmp_path),
                properties={"sync_status": "active"},
            )
        finally:
            s1.close()
        s2 = KnowledgeStore(db)
        try:
            assert s2.get_source_by_uri(str(tmp_path)) is not None
        finally:
            s2.close()

    def test_non_folder_orphan_is_still_cleaned(self, tmp_path):
        """The carve-out is folder-only; other empty sources still get reaped."""
        db = str(tmp_path / "k.db")
        s1 = KnowledgeStore(db)
        try:
            s1.add_source(
                name="stale", source_type="local_file",
                uri=str(tmp_path / "gone.md"), properties={},
            )
        finally:
            s1.close()
        s2 = KnowledgeStore(db)
        try:
            assert s2.get_source_by_uri(str(tmp_path / "gone.md")) is None
        finally:
            s2.close()


class TestConfigContract:
    """The two new knowledge.* keys parse, clamp, and round-trip.

    The config-level parsing tests live in ``test/test_config_loader.py``
    alongside the other ``knowledge.*`` key tests (they need that module's
    ``_load_from_dict`` helper). What is asserted here is only the contract this
    module depends on: the default dirname constant agrees with the config
    default, so a config-less install discovers the documented folder.
    """

    def test_module_default_matches_config_default(self):
        from dataclasses import fields

        from kiro_crew.config.loader import KnowledgeConfig

        field = next(f for f in fields(KnowledgeConfig) if f.name == "auto_discover_dirname")
        assert field.default == DEFAULT_DROP_DIRNAME

    def test_discovery_is_off_by_default(self):
        from dataclasses import fields

        from kiro_crew.config.loader import KnowledgeConfig

        field = next(f for f in fields(KnowledgeConfig) if f.name == "auto_discover_folder")
        assert field.default is False
