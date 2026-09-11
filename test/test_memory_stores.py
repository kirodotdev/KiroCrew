"""Named memory stores: the resolvers, the shape rule, the fence, and inertness.

The acceptance property of the whole unit is that a DEFAULT-store user sees no
change, so the ratchets here assert the default answers as much as the new ones.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

import pytest

from conftest import make_dir_link
from kiro_crew import security
from kiro_crew.config.loader import KiroCrewConfig, config_dir, workspace_dir_for
from kiro_crew.config.paths import ensure_data_home
from kiro_crew.memory import INDEX_DB_FILE, MemoryStore, workspace_dir
from kiro_crew.memory_stores import (
    DEFAULT_MEMORY_STORE,
    MEMORY_DB_FILE,
    MEMORY_STORE_NAME_MAX,
    MEMORY_STORES_DIR_NAME,
    UnknownMemoryStore,
    ensure_memory_store_dir,
    memory_index_path_for,
    memory_store_dir_for,
    memory_store_name_defect,
    memory_stores_root,
    resolve_store_path,
    usable_store_names,
    validate_memory_store_name,
)

_IS_POSIX = os.name == "posix"


def _write_config(payload: dict) -> None:
    """Write ``config.json`` into the per-test data home and drop the cache."""
    from kiro_crew.config import loader as loader_mod

    (config_dir() / "config.json").write_text(json.dumps(payload), encoding="utf-8")
    loader_mod._invalidate_config_cache()


# ---------------------------------------------------------------------------
# The two roots (items 2 and 3)
# ---------------------------------------------------------------------------


class TestDefaultStoreRootsAreUnmoved:
    def test_markdown_root_is_the_workspace_dir_on_an_empty_home(self) -> None:
        """No ``config.json`` at all must still resolve, and to the SAME tree.

        The raw config dict carries no ``memory_stores`` key until a write-back
        migration adds one, so a resolver reading it would report ``"default"``
        as undeclared on every fresh install. Resolving off the loaded config is
        what makes this answer exist.
        """
        assert not (config_dir() / "config.json").exists()
        assert memory_store_dir_for(DEFAULT_MEMORY_STORE) == workspace_dir()
        assert memory_store_dir_for(DEFAULT_MEMORY_STORE) == config_dir() / "workspace"

    def test_vector_file_is_the_root_memory_db_on_an_empty_home(self) -> None:
        assert not (config_dir() / "config.json").exists()
        assert resolve_store_path(DEFAULT_MEMORY_STORE) == config_dir() / MEMORY_DB_FILE

    def test_vector_file_is_byte_exact_with_the_vector_store_default(self) -> None:
        """The default answer must be the path ``VectorMemoryStore()`` picks."""
        from kiro_crew.vector_memory import VectorMemoryStore

        assert resolve_store_path(DEFAULT_MEMORY_STORE) == VectorMemoryStore()._db_path

    def test_a_degraded_config_still_resolves_the_default_store(self) -> None:
        """An unreadable config must not cost the default store its paths."""
        (config_dir() / "config.json").write_text("{not json", encoding="utf-8")
        from kiro_crew.config import loader as loader_mod

        loader_mod._invalidate_config_cache()
        assert memory_store_dir_for(DEFAULT_MEMORY_STORE) == workspace_dir()
        assert resolve_store_path(DEFAULT_MEMORY_STORE) == config_dir() / MEMORY_DB_FILE


class TestNamedStoreRoots:
    def test_a_declared_name_gets_its_own_subtree(self) -> None:
        _write_config({"memory_stores": {"default": {}, "work": {}}})
        assert memory_store_dir_for("work") == memory_stores_root() / "work"
        assert memory_stores_root() == config_dir() / MEMORY_STORES_DIR_NAME

    def test_a_declared_name_gets_its_own_vector_file(self) -> None:
        _write_config({"memory_stores": {"default": {}, "work": {}}})
        assert resolve_store_path("work") == memory_stores_root() / "work" / MEMORY_DB_FILE

    def test_the_two_roots_are_not_the_same_path(self) -> None:
        """The sharpest hazard: the markdown root is NOT the data home.

        A bare ``!=`` between the two answers proves nothing — a directory
        differs from a file inside it under any nesting, so conflating the
        markdown root with the data home (``config_dir()`` instead of
        ``config_dir()/"workspace"``) keeps every inequality true. Pin each
        answer against the home instead: the markdown root is one level BELOW
        it, the vector file sits directly IN it, and the markdown root is
        therefore not the vector file's parent.
        """
        _write_config({"memory_stores": {"work": {}}})
        home = config_dir()
        md = memory_store_dir_for(DEFAULT_MEMORY_STORE)
        vec = resolve_store_path(DEFAULT_MEMORY_STORE)
        assert md == home / "workspace"
        assert md.parent == home
        assert vec == home / MEMORY_DB_FILE
        assert vec.parent == home
        assert vec.parent != md

        # A named store nests the other way round: the vector file lives INSIDE
        # the markdown root, so there the parent relation is the assertion.
        md_work = memory_store_dir_for("work")
        vec_work = resolve_store_path("work")
        assert md_work == memory_stores_root() / "work"
        assert vec_work.parent == md_work
        assert vec_work.name == MEMORY_DB_FILE


# ---------------------------------------------------------------------------
# The two-step degrade (item 4)
# ---------------------------------------------------------------------------


class TestUndeclaredNameFailsClosed:
    @pytest.mark.parametrize("configured_default", ["default", "work", "also-missing", "Bad_Name"])
    def test_unknown_store_never_uses_a_fallback(self, configured_default):
        _write_config({"memory_stores": {"work": {}}, "default_memory_store": configured_default})
        with pytest.raises(UnknownMemoryStore, match="not declared"):
            memory_store_dir_for("no-such-store")


class TestShapeRuleRaises:
    @pytest.mark.parametrize(
        "name",
        [
            "",
            "Work",
            "WORK",
            "a_b",
            "-lead",
            "trail-",
            "a b",
            "a/b",
            "a\\b",
            "../work",
            "..",
            ".",
            "ab.",
            "ab ",
            "con",
            "nul",
            "aux",
            "prn",
            "com1",
            "com9",
            "lpt1",
            "lpt9",
            "wörk",
        ],
        ids=repr,
    )
    def test_a_malformed_name_raises_rather_than_degrading(self, name: str) -> None:
        """Degrading a malformed name is what would merge two crews' memory."""
        with pytest.raises(UnknownMemoryStore):
            validate_memory_store_name(name)
        with pytest.raises(UnknownMemoryStore):
            memory_store_dir_for(name)
        with pytest.raises(UnknownMemoryStore):
            resolve_store_path(name)

    def test_the_length_cap_is_inclusive(self) -> None:
        assert validate_memory_store_name("a" * MEMORY_STORE_NAME_MAX)
        with pytest.raises(UnknownMemoryStore):
            validate_memory_store_name("a" * (MEMORY_STORE_NAME_MAX + 1))

    @pytest.mark.parametrize("name", ["default", "work", "a", "0", "a-b-c", "team2"])
    def test_a_well_formed_name_passes(self, name: str) -> None:
        assert validate_memory_store_name(name) == name
        assert memory_store_name_defect(name) is None

    def test_a_non_string_is_a_defect_not_a_crash(self) -> None:
        assert memory_store_name_defect(None) == "not a string"
        assert memory_store_name_defect(7) == "not a string"

    def test_a_symlinked_store_name_is_refused_after_composition(self, tmp_path) -> None:
        """Validation and use are separated by a call boundary.

        The name passes the shape rule, so only a containment re-check AFTER
        composition can catch a store directory that has been replaced with a
        link pointing outside the fenced root.
        """
        _write_config({"memory_stores": {"work": {}}})
        root = memory_stores_root()
        root.mkdir(parents=True, exist_ok=True)
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        make_dir_link(root / "work", outside)
        with pytest.raises(UnknownMemoryStore):
            memory_store_dir_for("work")
        with pytest.raises(UnknownMemoryStore):
            resolve_store_path("work")

    def test_config_load_reports_a_malformed_store_name(self, caplog) -> None:
        """Reported at boot, and kept verbatim so a save cannot erase it."""
        _write_config({"memory_stores": {"Bad_Name": {}}})
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            cfg = KiroCrewConfig.load()
        assert "Bad_Name" in cfg.memory_stores
        assert any("Bad_Name" in r.getMessage() for r in caplog.records)


class TestMalformedAndDeclaredIsUndeclaredForResolution:
    """The load KEEPS a malformed name, so declaredness alone is not usability.

    Without the filter the two membership tests disagree: the binding hands a
    crew a name the config declares, and the resolver then raises on it at the
    first memory write. That makes the runtime raise reachable from a config that
    merely loaded with a warning.
    """

    def test_a_malformed_declared_name_is_dropped_from_the_resolvable_set(self) -> None:
        declared = usable_store_names({"Bad_Name": None, "work": None})
        assert "work" in declared
        assert "Bad_Name" not in declared
        # Filtering ONLY: the floor belongs to the resolvers, which must answer
        # for it on a home with no config.json, and NOT to a crew's binding,
        # where adding it would change which store an existing config lands on.
        assert DEFAULT_MEMORY_STORE not in declared

    @pytest.mark.parametrize("store", ["Bad_Name", "../escape"])
    def test_malformed_member_binding_fails_before_any_memory_read(self, store):
        from kiro_crew.config.loader import resolve_agent_bindings

        _write_config(
            {
                "agents": {"crew": {"kiro_agent": "kirocrew", "memory_store": store}},
                "default_agent": "crew",
                "memory_stores": {store: {}, "work": {}},
                "default_memory_store": "work",
            }
        )
        cfg = KiroCrewConfig.load()
        assert store in cfg.memory_stores
        with pytest.raises(UnknownMemoryStore):
            resolve_agent_bindings(cfg, agent_name="crew")

    def test_a_malformed_name_still_raises_when_handed_in_directly(self) -> None:
        """Declaring it does not buy it a path; the fail-closed posture stands."""
        _write_config({"memory_stores": {"Bad_Name": {}}})
        with pytest.raises(UnknownMemoryStore):
            memory_store_dir_for("Bad_Name")
        with pytest.raises(UnknownMemoryStore):
            resolve_store_path("Bad_Name")
        with pytest.raises(UnknownMemoryStore):
            memory_index_path_for("Bad_Name")


# ---------------------------------------------------------------------------
# workspace_dir_for's fall-through (item 6)
# ---------------------------------------------------------------------------


class TestWorkspaceFallThrough:
    def test_an_unmapped_name_logs_its_fall_through(self, caplog) -> None:
        """Two distinct names silently sharing <home>/workspace is the defect."""
        _write_config(
            {
                "workspaces": {"default": {"dir": "workspace"}},
                "default_workspace": "default",
            }
        )
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            resolved = workspace_dir_for("nope")
        assert resolved == config_dir() / "workspace"
        assert any("nope" in r.getMessage() for r in caplog.records)

    def test_an_unmapped_name_never_reaches_the_default_workspaces_directory(
        self, tmp_path: Path
    ) -> None:
        """An unrecognized name stays inside the data home, whatever the default declares.

        This answer becomes a filesystem search root, and one caller takes the name
        straight from a request (``dashboard/handlers/files.py``'s ``?workspace=``)
        without the ``is_sensitive_path`` check its ``?project=`` sibling applies. So
        the fall-through must land on the BASE workspace directory rather than hop to
        ``default_workspace`` — a hop would let any unrecognized name reach whatever
        absolute directory that workspace declares.
        """
        # Absolute and outside the data home, from the fixture rather than a literal:
        # a POSIX-rooted string is not absolute on Windows, so a hardcoded one would
        # quietly stop testing the boundary there.
        outside = tmp_path / "declared-elsewhere"
        _write_config(
            {
                "workspaces": {"main": {"dir": str(outside)}},
                "default_workspace": "main",
            }
        )
        # The declared name still honours its absolute dir; only the fall-through is bounded.
        assert workspace_dir_for("main") == outside
        resolved = workspace_dir_for("not-declared")
        assert resolved == config_dir() / "workspace"
        assert outside not in resolved.parents and resolved != outside

    def test_it_agrees_with_resolve_agent_bindings(self) -> None:
        """One table, one answer: both resolvers now read ``cfg.workspaces``.

        Including for a config the schema repairs. A legacy FLAT
        ``{"name": "dir"}`` entry is a type mismatch the validator removes before
        the loader sees it, so the loaded table has no such workspace — and
        ``resolve_agent_bindings`` has always answered from that table. Reading
        the raw bytes instead is what let the two disagree.
        """
        from kiro_crew.config.loader import resolve_agent_bindings

        _write_config({"workspaces": {"alt": "alt-tree"}, "default_workspace": "alt"})
        cfg = KiroCrewConfig.load()
        assert "alt" not in cfg.workspaces
        bindings = resolve_agent_bindings(cfg)
        assert workspace_dir_for("alt") == config_dir() / bindings.workspace_dir

    def test_a_declared_absolute_dir_is_returned_as_is(self, tmp_path) -> None:
        _write_config(
            {
                "workspaces": {"abs": {"dir": str(tmp_path / "elsewhere")}},
                "default_workspace": "abs",
            }
        )
        assert workspace_dir_for("abs") == tmp_path / "elsewhere"

    def test_it_never_raises_on_an_empty_home(self) -> None:
        """``default_project_dir`` and the workspace-identity block ride on this."""
        assert not (config_dir() / "config.json").exists()
        assert workspace_dir_for() == config_dir() / "workspace"

    def test_it_never_raises_on_an_unreadable_config(self) -> None:
        (config_dir() / "config.json").write_text("{{{", encoding="utf-8")
        from kiro_crew.config import loader as loader_mod

        loader_mod._invalidate_config_cache()
        assert workspace_dir_for("anything") == config_dir() / "workspace"


# ---------------------------------------------------------------------------
# One index per store (item 7)
# ---------------------------------------------------------------------------


class TestIndexIsPerStore:
    """The DEFAULT store's index does not move; a NAMED store's is its own.

    The default index stays in the data-home root because that root-relative
    spelling is baked into every off-store consumer — the snapshot ``memory``
    component's ``files`` tuple, ``portability``'s export/import zip and
    ``scripts/sync-to-remote.sh``. Relocating it beside the markdown tree drops
    it from every backup silently.
    """

    def test_the_default_stores_index_is_in_the_data_home_root(self) -> None:
        assert memory_index_path_for(DEFAULT_MEMORY_STORE) == config_dir() / INDEX_DB_FILE
        # NOT the markdown root, and that asymmetry is the point.
        assert memory_index_path_for(DEFAULT_MEMORY_STORE).parent != memory_store_dir_for(
            DEFAULT_MEMORY_STORE
        )

    def test_it_is_the_path_the_snapshot_memory_component_declares(self) -> None:
        """The ratchet against moving it: snapshot names the file root-relative."""
        from kiro_crew.snapshot import COMPONENTS

        assert INDEX_DB_FILE in COMPONENTS["memory"].files
        rel = memory_index_path_for(DEFAULT_MEMORY_STORE).relative_to(config_dir())
        assert str(rel) == INDEX_DB_FILE

    def test_a_bare_memory_store_indexes_to_the_default_stores_path(self) -> None:
        assert MemoryStore()._index_db == memory_index_path_for(DEFAULT_MEMORY_STORE)

    def test_a_named_store_gets_its_own_index_beside_its_markdown(self) -> None:
        _write_config({"memory_stores": {"default": {}, "work": {}}})
        assert memory_index_path_for("work") == memory_stores_root() / "work" / INDEX_DB_FILE
        assert memory_index_path_for("work").parent == memory_store_dir_for("work")
        assert memory_index_path_for("work") != memory_index_path_for(DEFAULT_MEMORY_STORE)

    def test_the_store_aware_caller_passes_the_index_path_in(self, tmp_path) -> None:
        """``MemoryStore`` holds no branch on which store it serves.

        Location is store policy and lives in ``memory_index_path_for``; the
        store takes the resolved path, so the two cannot answer differently.
        """
        _write_config({"memory_stores": {"default": {}, "work": {}}})
        store = MemoryStore(
            workspace=memory_store_dir_for("work"),
            index_db=memory_index_path_for("work"),
        )
        assert store._index_db == memory_stores_root() / "work" / INDEX_DB_FILE

    def test_the_two_default_construction_forms_still_disagree(self) -> None:
        """A pre-existing quirk, pinned rather than silently reintroduced.

        A bare ``MemoryStore()`` and ``MemoryStore(workspace=workspace_dir())``
        share one ``_workspace`` and one markdown tree yet name two different
        index files, and both forms are live (``cli.py`` takes the first,
        ``context.py`` the second). Neither answer is wrong — ``rebuild_index``
        reads no index state, so the cost is a duplicated rebuild — but only the
        root copy is in the snapshot. Closing it means giving every caller a
        store name, which belongs to the step that wires stores in; this test
        exists so that step is a deliberate change and not a surprise.
        """
        implicit = MemoryStore()
        explicit = MemoryStore(workspace=workspace_dir())
        assert implicit._workspace == explicit._workspace
        assert implicit._index_db == config_dir() / INDEX_DB_FILE
        assert explicit._index_db == workspace_dir() / INDEX_DB_FILE
        assert implicit._index_db != explicit._index_db

    def test_two_workspaces_get_two_indexes(self, tmp_path) -> None:
        a = MemoryStore(workspace=tmp_path / "a")
        b = MemoryStore(workspace=tmp_path / "b")
        assert a._index_db != b._index_db

    def test_a_rebuild_needs_only_the_markdown_files(self, tmp_path) -> None:
        """The index is DERIVED, which is why losing it costs no memory.

        ``rebuild_index`` regenerates from preferences.md / projects.md /
        history/*.md and reads no index state.
        """
        store = MemoryStore(workspace=tmp_path / "ws")
        store.init()
        store.write_preferences("# User Preferences\n\n- likes Python\n")
        assert store.rebuild_index() >= 1
        assert store.search("Python")
        # Delete the index outright: a rebuild reconstructs it from the markdown.
        store._index_db.unlink(missing_ok=True)
        assert store.rebuild_index() >= 1
        assert store.search("Python")


class TestARealStoreSurvivesSnapshotAndRestore:
    """END-TO-END, driving a real ``MemoryStore`` rather than a synthetic index.

    Every other snapshot/memory test writes ``memory_index.db`` at the root by
    hand, so all of them pass whichever path ``MemoryStore`` actually picks —
    they assert the staging and install of a file the test itself created. That
    makes the whole existing suite blind to the index moving, which is why this
    one lets the store choose its own path and then asks the RESTORED home a
    search question that only an installed index can answer.
    """

    def test_search_answers_the_restored_content(self, tmp_path, monkeypatch) -> None:
        from kiro_crew import pinned_fs
        from kiro_crew.snapshot import restore_main, snapshot_main

        # ``_staging_is_pinned`` refuses rather than falling back where the
        # platform has no directory descriptors, so say what an operator there
        # has to say — and nothing extra where pinning works, so Linux still
        # exercises the shipping path.
        unpinnable = [] if pinned_fs.supports_pinned_tree_walk() else ["--allow-unpinned-staging"]
        monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")

        source = tmp_path / "source-home"
        monkeypatch.setenv("KIROCREW_HOME", str(source))
        store = MemoryStore()
        store.init()
        store.write_preferences("# User Preferences\n\n- deploys with a canary stage first\n")
        assert store.rebuild_index() >= 1
        assert store.search("canary"), "the source store must be searchable to begin with"

        out = tmp_path / "out"
        assert snapshot_main([str(out), "--components", "memory"] + unpinnable) == 0
        tarballs = sorted(out.glob("kirocrew-snapshot-*.tar.gz"))
        assert tarballs, "no snapshot tarball was produced"

        fresh = tmp_path / "fresh-home"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        assert (
            restore_main(
                [str(tarballs[-1]), "--mode", "replace", "--components", "memory", "--force"]
                + unpinnable
            )
            == 0
        )

        # No path assertion here on purpose: the SEARCH is the assertion, because
        # a search answering is the only thing that proves the index the store
        # opens is the index the bundle carried.
        assert MemoryStore().search("canary"), "the restored home cannot answer a memory search"


# ---------------------------------------------------------------------------
# Directory permissions (item 8)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _IS_POSIX, reason="mode bits are POSIX; Windows uses a DACL")
class TestOwnerOnlyDirectories:
    def test_the_data_home_is_owner_only_without_a_default_memory_db(self) -> None:
        """The guarantee must not depend on the default store's memory.db.

        ``VectorMemoryStore.init`` tightens its db path's PARENT, which is the
        data home only while the DEFAULT store's file sits in it — so a home
        whose crews all use named stores would never have been tightened.
        """
        home = config_dir()
        assert not (home / MEMORY_DB_FILE).exists()
        # The host-isolation fixture pins this home under tmp_path. A permissive
        # starting mode proves ensure_data_home repairs it to owner-only below.
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(home, 0o755)
        assert ensure_data_home() == home
        assert stat.S_IMODE(os.stat(home).st_mode) == 0o700

    def test_ensure_memory_store_dir_tightens_the_root_and_the_store(self) -> None:
        _write_config({"memory_stores": {"work": {}}})
        created = ensure_memory_store_dir("work")
        assert created == memory_stores_root() / "work"
        assert stat.S_IMODE(os.stat(memory_stores_root()).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(created).st_mode) == 0o700

    def test_ensure_memory_store_dir_leaves_the_default_store_untouched(self) -> None:
        """The default root belongs to ``MemoryStore.init()``, not to this call."""
        assert ensure_memory_store_dir(DEFAULT_MEMORY_STORE) == workspace_dir()
        assert not workspace_dir().exists()
        assert not memory_stores_root().exists()


# ---------------------------------------------------------------------------
# The fence (item 9)
# ---------------------------------------------------------------------------


class TestNamedStoresAreFenced:
    def _store_path(self) -> str:
        return str(Path.home() / ".kiro" / "crew" / MEMORY_STORES_DIR_NAME / "work")

    def test_the_stores_root_is_a_keystone_leaf_under_every_home_prefix(self) -> None:
        assert MEMORY_STORES_DIR_NAME in security._CREW_SECRET_LEAVES
        for prefix in security.crew_home_prefixes():
            assert f"{prefix}/{MEMORY_STORES_DIR_NAME}" in security.sensitive_home_dirs()

    @pytest.mark.parametrize(
        "leaf",
        ["", "/memory.db", "/memory/preferences.md", f"/{INDEX_DB_FILE}"],
        ids=["root", "vector", "markdown", "index"],
    )
    def test_read_and_write_are_both_refused(self, leaf: str) -> None:
        target = self._store_path() + leaf
        assert security.is_sensitive_path(target) is True
        assert security.is_sensitive_write_path(target) is True

    @pytest.mark.parametrize(
        "cmd_template",
        [
            "echo pwned > {store}/memory/preferences.md",
            'sqlite3 {store}/memory.db "delete from semantic"',
            "cat {store}/memory/preferences.md",
            "tar -xzf /tmp/x.tgz -C {store}",
        ],
        ids=["redirect", "sqlite3", "read", "tar"],
    )
    def test_the_shell_text_matcher_deliberately_does_not_fence_a_store(
        self, cmd_template: str
    ) -> None:
        """A store path in COMMAND TEXT is not what fences a store, on purpose.

        ``is_sensitive_bash_command`` documents that it does not match paths at all:
        a text matcher over ``cat <fenced path>`` adds nothing on top of the controls
        that cannot be talked around, and it denied ordinary read-only commands
        whenever a fenced spelling appeared as DATA -- a grep pattern, a commit
        message, a note. A keystone read through the shell is permitted there by
        design.

        Asserted rather than deleted so the boundary is recorded where someone
        looking for it will find it. What actually fences a store is two things, both
        covered above and below: :func:`is_sensitive_path` /
        :func:`is_sensitive_write_path` refuse every RESOLVED path the agent's file
        tools open, and the OS sandbox confines the agent's process tree. If this test
        starts failing because a path matcher came back, that is a decision to make
        deliberately -- with the false-denial class in mind -- not a regression to fix
        by making this assertion pass.
        """
        cmd = cmd_template.format(store=self._store_path())
        assert security.is_sensitive_bash_command(cmd) is None

    def test_the_legacy_home_prefix_is_fenced_too(self) -> None:
        legacy = str(Path.home() / ".kirocrew" / MEMORY_STORES_DIR_NAME / "work")
        assert security.is_sensitive_path(legacy) is True
        assert security.is_sensitive_path(legacy + "/memory.db") is True


class TestDefaultStoreStaysReadable:
    """The RATCHET. The asymmetry is deliberate; do not "tidy" it away.

    The default store IS the agent's own memory, and reading it is the product
    working. Fencing it would be a default-path behaviour change, which the
    coexistence constraint forbids.
    """

    @pytest.mark.parametrize(
        "leaf",
        [MEMORY_DB_FILE, INDEX_DB_FILE, "workspace/memory/preferences.md"],
    )
    def test_the_default_stores_own_files_are_not_sensitive(self, leaf: str) -> None:
        target = str(Path.home() / ".kiro" / "crew" / leaf)
        assert security.is_sensitive_path(target) is False

    def test_the_default_stores_files_are_not_write_fenced_either(self) -> None:
        target = str(Path.home() / ".kiro" / "crew" / MEMORY_DB_FILE)
        assert security.is_sensitive_write_path(target) is False

    def test_reading_the_default_memory_db_is_not_a_blocked_command(self) -> None:
        target = str(Path.home() / ".kiro" / "crew" / MEMORY_DB_FILE)
        assert security.is_sensitive_bash_command(f"cat {target}") is None
