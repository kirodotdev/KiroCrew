"""``memory export`` / ``memory import`` reaching a NAMED store, in both directions.

Both verbs shared one open that was hardwired to the default store's path, so a
named store's rows were unreachable from either end while ``backups``, ``restore``
and ``carve`` already took ``--store``. The subject here is the three ways a
store-scoped verb goes wrong:

* **A dropped scope reads the WRONG store.** ``resolve_store_path`` answers the
  DEFAULT store's file for a name it does not know, so a verb that skips the
  ownership guard prints the operator's global memory under the heading they
  typed -- and ``import`` would WRITE there. Every test that claims a scope works
  therefore plants a row in the other store and asserts it did not move.
* **A refusal that looks like an empty answer is worse than an error.** A named
  store's markdown root is under the fenced ``memory_stores/`` subtree, and
  ``_guarded_entry`` shapes that refusal exactly like a missing file. So
  ``--include-markdown`` on a named store is refused rather than reported as an
  empty tree, and the refusal is asserted to write no payload AND no file.
* **The default store must not move.** An omitted ``--store`` resolves the same
  path ``VectorMemoryStore()`` derives for itself, so the no-flag behaviour is
  pinned alongside the new one rather than assumed.

Store fixtures follow ``test_memory_v2_facet_read.py``: declare the silos first,
resolve second, close every connection at teardown.
"""

from __future__ import annotations

import argparse
import contextlib
import json
from pathlib import Path

import pytest

from kiro_crew import cli_commands as cc
from kiro_crew.config import loader as loader_mod
from kiro_crew.config.loader import config_dir
from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE, resolve_store_path
from kiro_crew.vector_memory import VectorMemoryStore

# Each test writes ``config.json`` into its own data home and drops the process-wide
# config cache, so two workers racing that global is a flake.
pytestmark = pytest.mark.xdist_group("memory_cli_store_scope")

_ACME = "acme"
_BETA = "beta"

#: One key per store, so "the right store answered" and "some store answered" are
#: different assertions at every call site.
_ACME_KEY = "project.acme.owner"
_BETA_KEY = "project.beta.owner"
_GLOBAL_KEY = "project.global.only"

_ACME_EPISODE = "The acme crew rotated the ledger signing key."


def _declare_silos(*names: str) -> None:
    """Declare *names* as memory stores in the per-test data home.

    ``memory_stores`` is an OBJECT keyed by store name; spelled as an ARRAY the
    schema drops the whole entry and every name degrades to the default, so a test
    written that way would assert against the operator-shaped file.
    """
    payload = {
        "memory_stores": {
            DEFAULT_MEMORY_STORE: {},
            **{name: {"owner_member": "", "memory_version": 1} for name in names},
        },
        "default_memory_store": DEFAULT_MEMORY_STORE,
    }
    (config_dir() / "config.json").write_text(json.dumps(payload), encoding="utf-8")
    loader_mod._invalidate_config_cache()


@pytest.fixture
def stores():
    """Open vector stores, and let a test release them before touching the files.

    Each holds a live SQLite connection. On Windows an open handle blocks both
    ``unlink`` and ``rmtree``, so a test that DELETES a store's database has to
    close it first -- on POSIX the delete succeeds with the handle open, so the
    divergence shows up only on the Windows shards. ``close_all`` is what those
    tests call; teardown calls it too, so a test that fails mid-way still does
    not leave a handle blocking temp-dir cleanup.
    """
    opened: list[VectorMemoryStore] = []

    class _Stores:
        def __call__(self, db_path: Path) -> VectorMemoryStore:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            store = VectorMemoryStore(db_path=db_path)
            opened.append(store)
            store.init()
            return store

        def close_all(self) -> None:
            """Release every handle opened through this fixture. Idempotent."""
            while opened:
                opened.pop().close()

    helper = _Stores()
    try:
        yield helper
    finally:
        helper.close_all()


@pytest.fixture
def seeded(stores):
    """``acme`` holds rows, ``beta`` is empty, and the default store holds its own.

    The empty destination and the populated bystander are both load-bearing:
    without them an import that landed in the wrong file, or a read that answered
    from the default store, would still satisfy a single-store assertion.
    """
    _declare_silos(_ACME, _BETA)
    acme = stores(resolve_store_path(_ACME))
    acme.set_semantic(_ACME_KEY, "raymond", 0.9, "test")
    assert acme.write_episodic(_ACME_EPISODE, source="test", importance=0.7) is True
    stores(resolve_store_path(_BETA))
    default = stores(resolve_store_path(DEFAULT_MEMORY_STORE))
    default.set_semantic(_GLOBAL_KEY, "in-the-default-store", 0.9, "test")
    return None


def _ns(**kw: object) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def _semantic_keys(name: str) -> list[str]:
    """The live semantic keys in *name*'s own vector file, read directly."""
    store = VectorMemoryStore(db_path=resolve_store_path(name))
    store.init()
    try:
        return sorted(str(row["key"]) for row in store.get_all_semantic())
    finally:
        store.close()


class TestExportReadsTheStoreItWasGiven:
    def test_a_named_store_exports_its_own_rows_and_not_the_default_stores(
        self, seeded, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cc._memory_cmd(_ns(mem_action="export", output=None, store=_ACME))
        payload = json.loads(capsys.readouterr().out)
        assert [row["key"] for row in payload["semantic"]] == [_ACME_KEY]
        assert [row["text"] for row in payload["episodic"]] == [_ACME_EPISODE]

    def test_an_omitted_store_still_exports_the_default_store(
        self, seeded, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The no-flag path must resolve exactly where it always did.

        ``resolve_store_path(DEFAULT_MEMORY_STORE)`` and ``VectorMemoryStore()``'s
        own fallback both compose ``config_dir()/memory.db``; this pins that they
        still agree, because a divergence would silently retarget every existing
        invocation of a verb nobody edited.
        """
        cc._memory_cmd(_ns(mem_action="export", output=None, store=None))
        payload = json.loads(capsys.readouterr().out)
        assert [row["key"] for row in payload["semantic"]] == [_GLOBAL_KEY]

    def test_an_undeclared_store_is_refused_and_answers_with_no_rows(
        self, seeded, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Named, not degraded.

        The refusal is asserted together with the ABSENCE of the default store's
        rows, because "it printed an error" and "it printed no rows" are different
        claims -- and a resolver that answered the default store's file for an
        unknown name would satisfy only the first.
        """
        cc._memory_cmd(_ns(mem_action="export", output=None, store="nosuchstore"))
        out = capsys.readouterr().out
        assert "'nosuchstore' is not declared" in out
        assert _GLOBAL_KEY not in out
        assert '"semantic"' not in out


class TestADeletedStoreIsRefusedRatherThanRecreatedEmpty:
    """Admission runs BEFORE resolution, and that ordering is the whole guard.

    Both resolvers compose a path by shape and check nothing exists, while
    ``VectorMemoryStore.init`` creates what is missing -- ``make_owner_only_dir``
    makes the parents, then a fresh empty SQLite file. Resolving first therefore
    turns a deleted store into an empty one, and ``export`` ships
    ``{"semantic": [], ...}`` which reads as "this store holds nothing" rather
    than as a loss. There is no recovery path from that, which is why the refusal
    is asserted alongside the store NOT being recreated.
    """

    def test_export_refuses_a_declared_store_whose_directory_was_deleted(
        self, seeded, stores, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import shutil

        db_path = resolve_store_path(_ACME)
        assert _semantic_keys(_ACME) == [_ACME_KEY]
        # Release the fixture's handles first: on Windows an open SQLite handle
        # blocks rmtree, so deleting with them open passes on POSIX and fails
        # there. The deletion is the SUBJECT of this test, not incidental setup.
        stores.close_all()
        shutil.rmtree(db_path.parent)

        cc._memory_cmd(_ns(mem_action="export", output=None, store=_ACME))
        out = capsys.readouterr().out
        assert "is missing or unreadable" in out
        assert '"semantic"' not in out
        # The recreation is the harm, so its absence is asserted directly rather
        # than inferred from the message above.
        assert not db_path.parent.exists()
        assert not db_path.exists()

    def test_import_refuses_a_declared_store_whose_directory_was_deleted(
        self, seeded, stores, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Worse in this direction: the rows would land in a store nobody restored."""
        import shutil

        dump = tmp_path / "rows.json"
        dump.write_text(json.dumps({"semantic": [{"key": _BETA_KEY, "value": "x"}]}), "utf-8")
        db_path = resolve_store_path(_BETA)
        stores.close_all()
        shutil.rmtree(db_path.parent)

        cc._memory_cmd(_ns(mem_action="import", file=str(dump), store=_BETA))
        assert "is missing or unreadable" in capsys.readouterr().out
        assert not db_path.parent.exists()
        assert _semantic_keys(DEFAULT_MEMORY_STORE) == [_GLOBAL_KEY]

    def test_the_default_store_is_still_admitted_with_no_database_yet(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A fresh install must keep working, so the guard is not a blanket one.

        The default store's absence is a normal pre-first-write state and
        ``require_memory_store`` admits it on the startup barrier alone; a DECLARED
        named store's absence is not. A guard that failed to separate those two
        would break every existing `kirocrew memory` verb on a new install.
        """
        _declare_silos()
        assert not resolve_store_path(DEFAULT_MEMORY_STORE).exists()
        cc._memory_cmd(_ns(mem_action="export", output=None, store=None))
        assert json.loads(capsys.readouterr().out)["semantic"] == []

    def test_export_refuses_a_named_store_whose_database_alone_was_deleted(
        self, seeded, stores, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The directory surviving its database is what admission cannot see.

        For a V1 named store ``require_memory_store`` routes through
        ``_require_legacy_store_files``, which ``return``s on a missing ``memory.db``
        -- absent evidence is not private-ownership evidence. So this state passes
        admission, and ``init()`` would write a fresh empty database: the read would
        both misreport a loss as an empty store and destroy the evidence it was a
        loss. A read must not create.
        """
        db_path = resolve_store_path(_ACME)
        assert _semantic_keys(_ACME) == [_ACME_KEY]
        # Same Windows reason as the sibling above: unlink is refused while a
        # handle is open, and the WAL/shm siblings are handles too.
        stores.close_all()
        for stray in db_path.parent.glob("memory.db*"):
            stray.unlink()
        assert db_path.parent.exists() and not db_path.exists()

        cc._memory_cmd(_ns(mem_action="export", output=None, store=_ACME))
        out = capsys.readouterr().out
        assert "refusing to create one on a read" in out
        assert '"semantic"' not in out
        # The recreation is the harm; its absence is asserted rather than inferred.
        assert not db_path.exists()

    def test_import_may_still_create_a_named_stores_database(
        self, stores, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        """The read guard must not block the verb whose whole job is to populate.

        Importing into a freshly declared, never-written store is the primary use
        case for `--store`, so a guard spelled "refuse a missing database" rather
        than "refuse a missing database on a READ" would remove the capability this
        PR adds.
        """
        from kiro_crew.memory_stores import ensure_memory_store_dir

        _declare_silos(_BETA)
        ensure_memory_store_dir(_BETA)
        db_path = resolve_store_path(_BETA)
        assert not db_path.exists()
        dump = tmp_path / "rows.json"
        dump.write_text(json.dumps({"semantic": [{"key": _BETA_KEY, "value": "x"}]}), "utf-8")

        cc._memory_cmd(_ns(mem_action="import", file=str(dump), store=_BETA))
        assert "Import complete" in capsys.readouterr().out
        assert _semantic_keys(_BETA) == [_BETA_KEY]


class TestAFailedImportLeavesNoDatabaseBehind:
    """``import`` may create, so its INPUT must be settled before it does.

    The read-refusal guard deliberately excludes ``import`` -- populating a freshly
    declared store is the whole verb -- but ``store.init()`` creates ``memory.db``
    the moment it runs. Validating the input afterwards meant a missing or malformed
    file still left a fresh empty database on a store whose directory had survived
    while its database had not, and that empty file made a later ``export --store``
    answer with rows instead of the refusal. A failed import would have silently
    erased the evidence of a real loss, so every rejected input is asserted here to
    leave the destination exactly as absent as it was.
    """

    def _deleted_db_store(self) -> Path:
        """A declared store whose directory survived but whose database did not."""
        from kiro_crew.memory_stores import ensure_memory_store_dir

        _declare_silos(_ACME)
        ensure_memory_store_dir(_ACME)
        db_path = resolve_store_path(_ACME)
        assert db_path.parent.exists() and not db_path.exists()
        return db_path

    def test_a_missing_file_argument_creates_nothing(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = self._deleted_db_store()
        cc._memory_cmd(_ns(mem_action="import", file=None, store=_ACME))
        assert "Usage: kirocrew memory import" in capsys.readouterr().out
        assert not db_path.exists()

    def test_an_absent_file_creates_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = self._deleted_db_store()
        cc._memory_cmd(_ns(mem_action="import", file=str(tmp_path / "absent.json"), store=_ACME))
        assert "File not found" in capsys.readouterr().out
        assert not db_path.exists()

    def test_malformed_json_creates_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = self._deleted_db_store()
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        cc._memory_cmd(_ns(mem_action="import", file=str(bad), store=_ACME))
        assert "could not be read as JSON" in capsys.readouterr().out
        assert not db_path.exists()

    def test_a_non_object_payload_creates_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``import_memory`` reads ``data.get(...)``, so a list would raise inside
        the write path -- after the store was opened."""
        db_path = self._deleted_db_store()
        listed = tmp_path / "list.json"
        listed.write_text(json.dumps([{"key": "k"}]), encoding="utf-8")
        cc._memory_cmd(_ns(mem_action="import", file=str(listed), store=_ACME))
        assert "is not an export object" in capsys.readouterr().out
        assert not db_path.exists()

    def test_the_refusal_preserves_the_export_side_loss_signal(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The property the ordering exists for, asserted end to end.

        A failed import followed by an export must still report the loss. If the
        failed import had created the database, the export would answer with rows.
        """
        self._deleted_db_store()
        cc._memory_cmd(_ns(mem_action="import", file=str(tmp_path / "absent.json"), store=_ACME))
        capsys.readouterr()
        cc._memory_cmd(_ns(mem_action="export", output=None, store=_ACME))
        out = capsys.readouterr().out
        assert "refusing to create one on a read" in out
        assert '"semantic"' not in out

    def test_a_null_collection_creates_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Dict-ness alone was not enough, and the gap was narrow.

        ``{"semantic": null}`` HAS the key, so ``data.get("semantic", [])`` answers
        ``None`` rather than the default, and ``import_memory`` iterates it outside
        its per-entry ``try``/``except`` -- so the ``TypeError`` landed after
        ``store.init()`` had already created the database.
        """
        db_path = self._deleted_db_store()
        nulled = tmp_path / "nulled.json"
        nulled.write_text(json.dumps({"semantic": None}), encoding="utf-8")
        cc._memory_cmd(_ns(mem_action="import", file=str(nulled), store=_ACME))
        assert "is not a list" in capsys.readouterr().out
        assert not db_path.exists()

    @pytest.mark.parametrize("collection", ["semantic", "episodic"])
    def test_each_iterated_collection_is_checked(
        self, collection: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = self._deleted_db_store()
        payload = tmp_path / f"{collection}.json"
        payload.write_text(json.dumps({collection: {"not": "a list"}}), encoding="utf-8")
        cc._memory_cmd(_ns(mem_action="import", file=str(payload), store=_ACME))
        assert f"{collection!r} collection that is not a list" in capsys.readouterr().out
        assert not db_path.exists()

    def test_the_checked_collections_are_the_ones_import_memory_iterates(self) -> None:
        """The correspondence, pinned at its source rather than trusted.

        A collection ``_IMPORTED_COLLECTIONS`` misses is one whose bad type raises
        AFTER the store was created -- exactly the hole the null case above was. So
        the list is compared against the keys ``import_memory`` actually reads,
        which is what makes a future third collection fail here instead of silently
        going unchecked.
        """
        import inspect
        import re

        source = inspect.getsource(VectorMemoryStore.import_memory)
        iterated = set(re.findall(r'data\.get\(\s*"([a-z_]+)"', source))
        assert iterated, "import_memory no longer reads data.get(...); revisit this pin"
        assert iterated == set(cc._IMPORTED_COLLECTIONS), (iterated, cc._IMPORTED_COLLECTIONS)


class TestAnUnresolvablePathIsRefusedRatherThanRedirected:
    """The `db_path is None` branch, which no other test reaches.

    ``owned_store_path`` answers ``None`` when a name resolves to something that is
    not that store's own file -- a symlink redirecting one store's directory at
    another's, or a store undeclared between admission and resolution. Without the
    branch the fall-through would open whatever path was composed, so the guard is
    pinned by forcing the ``None`` rather than left to a race nobody can schedule.
    """

    def test_export_refuses_when_the_path_is_not_the_stores_own(
        self, seeded, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(cc, "owned_store_path", lambda store: None)
        cc._memory_cmd(_ns(mem_action="export", output=None, store=_ACME))
        out = capsys.readouterr().out
        assert "did not resolve to its own file" in out
        assert '"semantic"' not in out

    def test_import_refuses_when_the_path_is_not_the_stores_own(
        self, seeded, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """The dangerous direction: a redirect here would WRITE to another store."""
        dump = tmp_path / "rows.json"
        dump.write_text(json.dumps({"semantic": [{"key": _BETA_KEY, "value": "x"}]}), "utf-8")
        monkeypatch.setattr(cc, "owned_store_path", lambda store: None)
        cc._memory_cmd(_ns(mem_action="import", file=str(dump), store=_BETA))
        assert "did not resolve to its own file" in capsys.readouterr().out
        assert _semantic_keys(DEFAULT_MEMORY_STORE) == [_GLOBAL_KEY]
        assert _BETA_KEY not in _semantic_keys(_BETA)


class TestAFacetBearingDestinationIsRefusedRatherThanFlattened:
    """``import_memory`` cannot carry a V2 row's attribution, so it must not try.

    It rebuilds every row through ``set_semantic`` and ``write_episodic`` and passes
    no ``facets=``, while a V2 export reads the CANONICAL relation rather than the
    compatibility view precisely so the facets come out. A V2 destination would
    therefore receive rows stripped of scope, surface, crew, session_key and
    derived_from, with nothing to reconstruct them from -- attribution flattened by
    an operation reporting success. Nothing that existed is lost by refusing: before
    ``--store``, ``import`` could only target the default store.
    """

    def test_a_v2_destination_is_refused_and_receives_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A REAL V2 member store, not a name pattern, is the subject here.

        Built through ``member_memory_helpers.write_member_home``, which lays down
        the manifest, the ``memory_stores`` record and the owning agent binding that
        ``require_memory_store`` validates -- so the store both opens as V2 and
        passes admission, leaving the refusal below as the only thing under test. A
        version-shaped fake would have skipped, and a skip scores as a pass.
        """
        from member_memory_helpers import forget_declared_stores, write_member_home

        from kiro_crew import memory_schema

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        write_member_home(tmp_path, "alice")
        forget_declared_stores(monkeypatch)
        store_name = "member-alice"

        target = VectorMemoryStore(db_path=resolve_store_path(store_name))
        target.init()
        try:
            assert target.algorithm_version == "v2"
        finally:
            target.close()

        dump = tmp_path / "rows.json"
        dump.write_text(json.dumps({"semantic": [{"key": _BETA_KEY, "value": "x"}]}), "utf-8")
        cc._memory_cmd(_ns(mem_action="import", file=str(dump), store=store_name))
        out = capsys.readouterr().out
        assert "cannot carry the scope, surface, crew, session and derivation facets" in out
        assert "Import complete" not in out
        assert _BETA_KEY not in _semantic_keys(store_name)
        # The axes the refusal protects, read from the schema rather than restated.
        assert memory_schema.FACET_NAMES == (
            "scope",
            "surface",
            "crew",
            "session_key",
            "derived_from",
        )

    def test_import_memory_carries_no_facets_which_is_why_the_refusal_exists(self) -> None:
        """The premise, pinned at its source rather than asserted in prose.

        ``import_memory`` rebuilds rows through ``set_semantic`` and
        ``write_episodic`` and passes no ``facets=``. If it ever learns to carry
        them, this fails and the refusal above should be reconsidered rather than
        silently kept.
        """
        import inspect

        source = inspect.getsource(VectorMemoryStore.import_memory)
        assert "facets" not in source

    def test_a_v1_destination_still_accepts_an_import(
        self, seeded, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """V1 holds no facets to lose, so the refusal must not reach it."""
        dump = tmp_path / "rows.json"
        dump.write_text(json.dumps({"semantic": [{"key": _BETA_KEY, "value": "x"}]}), "utf-8")
        cc._memory_cmd(_ns(mem_action="import", file=str(dump), store=_BETA))
        assert "Import complete" in capsys.readouterr().out
        assert _semantic_keys(_BETA) == [_BETA_KEY]


class TestImportWritesToTheStoreItWasGiven:
    def test_rows_land_in_the_named_store_and_nowhere_else(
        self, seeded, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The copy an operator could not perform at all: acme's rows into beta."""
        dump = tmp_path / "acme.json"
        cc._memory_cmd(_ns(mem_action="export", output=str(dump), store=_ACME))
        capsys.readouterr()

        cc._memory_cmd(_ns(mem_action="import", file=str(dump), store=_BETA))
        assert "Import complete" in capsys.readouterr().out
        assert _semantic_keys(_BETA) == [_ACME_KEY]
        # The bystanders are the point: an import that resolved the wrong path
        # would satisfy the line above only if it also disturbed one of these.
        assert _semantic_keys(DEFAULT_MEMORY_STORE) == [_GLOBAL_KEY]
        assert _semantic_keys(_ACME) == [_ACME_KEY]

    def test_an_undeclared_store_is_refused_before_anything_is_written(
        self, seeded, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dump = tmp_path / "rows.json"
        dump.write_text(json.dumps({"semantic": [{"key": _BETA_KEY, "value": "x"}]}), "utf-8")
        cc._memory_cmd(_ns(mem_action="import", file=str(dump), store="nosuchstore"))
        assert "is not declared" in capsys.readouterr().out
        # The degraded resolution's target, checked explicitly: a refusal that
        # still wrote would have put the row here.
        assert _semantic_keys(DEFAULT_MEMORY_STORE) == [_GLOBAL_KEY]


class TestTheMarkdownLayerIsRefusedRatherThanReportedEmpty:
    def test_include_markdown_on_a_named_store_writes_no_payload(
        self, seeded, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A refusal must not wear the shape of an empty tree.

        A named store's markdown root is under ``memory_stores/``, a keystone leaf
        in ``security._CREW_SECRET_LEAVES``. ``markdown_snapshot`` reaches a
        NON-private store through ``hooks.safe_read_file_bytes_nolink``, which
        refuses that subtree and answers None, and ``_guarded_entry`` renders that
        as ``content: ""`` -- identical to a file that is genuinely empty. An
        operator moving a store would read it as "there was nothing to carry".
        """
        from kiro_crew.memory_stores import memory_store_dir_for

        markdown = memory_store_dir_for(_ACME) / "memory"
        markdown.mkdir(parents=True, exist_ok=True)
        (markdown / "preferences.md").write_text("- acme prefers tabs\n", encoding="utf-8")

        cc._memory_cmd(_ns(mem_action="export", output=None, store=_ACME, include_markdown=True))
        out = capsys.readouterr().out
        assert "cannot be exported here" in out
        assert '"semantic"' not in out
        assert "acme prefers tabs" not in out

    def test_the_refusal_leaves_no_file_behind(
        self, seeded, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No half-file, because a half-file is what gets mistaken for the copy."""
        out_file = tmp_path / "acme.json"
        cc._memory_cmd(
            _ns(
                mem_action="export",
                output=str(out_file),
                store=_ACME,
                include_markdown=True,
            )
        )
        assert "cannot be exported here" in capsys.readouterr().out
        assert not out_file.exists()

    def test_include_markdown_still_works_on_the_default_store(
        self, seeded, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The refusal is scoped to a NAMED store; the default path is untouched."""
        from kiro_crew.memory import workspace_dir

        markdown = workspace_dir() / "memory"
        markdown.mkdir(parents=True, exist_ok=True)
        (markdown / "preferences.md").write_text("- global prefers spaces\n", encoding="utf-8")

        cc._memory_cmd(_ns(mem_action="export", output=None, store=None, include_markdown=True))
        payload = json.loads(capsys.readouterr().out)
        assert "global prefers spaces" in payload["markdown"]["preferences"]["content"]


class TestTheFlagIsReachableFromTheCommandLine:
    """The handler above is reached through argparse, which is a separate surface.

    A handler that honours ``args.store`` while the parser never defines the flag
    is unreachable in exactly the way the defect was, so the rendered help is read
    the way ``test_cli_help.py`` reaches the parser.
    """

    @pytest.mark.parametrize("verb", ["export", "import"])
    def test_both_verbs_offer_store(
        self, verb: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
    ) -> None:
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr("sys.argv", ["kirocrew", "memory", verb, "--help"])
        from kiro_crew.cli import main

        with pytest.raises(SystemExit):
            main()
        assert "--store" in capsys.readouterr().out

    def test_export_help_no_longer_claims_all_memory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
    ) -> None:
        """ "all memory" was never a thing one file held: a store is a separate silo."""
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr("sys.argv", ["kirocrew", "memory", "--help"])
        from kiro_crew.cli import main

        with pytest.raises(SystemExit):
            main()
        assert "Export all memory" not in capsys.readouterr().out


class TestCarveTakesTheSameAdmission:
    """`carve` is a READ of a named store, so the same guard has to cover it.

    Its own open composed the path itself and called ``init()`` unguarded, which is
    the same silent recreation the export guard exists to prevent -- the guard was
    at one call site while the cause is in ``VectorMemoryStore.init``. These pin the
    behaviour at the OTHER site, so the extraction cannot be undone at one of them.
    """

    def _carve(self, store: str | None) -> None:
        cc._memory_cmd(
            _ns(
                mem_action="carve",
                store=store,
                scope=None,
                surface=None,
                crew=None,
                session_key=None,
                derived_from=None,
                kind=None,
                count_by="kind",
                limit=50,
                offset=0,
            )
        )

    def test_carve_refuses_a_declared_store_whose_directory_was_deleted(
        self, seeded, stores, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import shutil

        db_path = resolve_store_path(_ACME)
        stores.close_all()
        shutil.rmtree(db_path.parent)

        self._carve(_ACME)
        assert "is missing or unreadable" in capsys.readouterr().out
        # The recreation is the harm, asserted directly rather than inferred.
        assert not db_path.parent.exists()

    def test_carve_refuses_a_named_store_whose_database_alone_was_deleted(
        self, seeded, stores, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A surviving directory is what lets this state pass admission."""
        db_path = resolve_store_path(_ACME)
        stores.close_all()
        db_path.unlink()

        self._carve(_ACME)
        out = capsys.readouterr().out
        assert "refusing to create one on a read" in out
        assert not db_path.exists()

    def test_carve_refuses_an_undeclared_store_without_reading_the_default(
        self, seeded, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Belt and braces, NOT coverage of the guard: it passes with the guard
        removed, because ``resolve_declared_store`` above already raises on an
        undeclared name. Kept so a rewrite that drops that call is caught too.
        """
        self._carve("nosuchstore")
        out = capsys.readouterr().out
        assert "'nosuchstore' is not declared" in out
        # A refusal that answered from global memory would be the real defect.
        assert _GLOBAL_KEY not in out

    def test_carve_still_reads_a_live_declared_store(
        self, seeded, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Control, deliberately mutation-insensitive: it passes with the guard
        removed as well, which is the point -- it exists to catch a guard that
        refuses a store it should have admitted.
        """
        self._carve(_ACME)
        out = capsys.readouterr().out
        assert "is missing or unreadable" not in out
        assert "refusing to create one on a read" not in out


class TestAnImportThatWritesNothingLeavesNoDatabase:
    """The invariant, which is why these are shape-independent.

    The set of payloads that reach ``import_memory`` and write nothing is open-ended:
    ``[null]``, ``[{}]`` and ``[{"nokey": 1}]`` are all list-shaped and are all
    skipped by that method's per-entry ``except Exception``. So the property asserted
    here is the OUTCOME -- an import that creates a named store's database and then
    writes nothing must leave the store as it found it, because a later read has to
    keep reporting the loss.
    """

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"semantic": [None]}, id="null-entry"),
            pytest.param({"semantic": [{}]}, id="empty-dict-entry"),
            pytest.param({"semantic": [{"nokey": 1}]}, id="entry-missing-key"),
            pytest.param({"episodic": [{"notext": 1}]}, id="episodic-missing-text"),
            pytest.param({"semantic": [], "episodic": []}, id="empty-collections"),
        ],
    )
    def test_a_created_database_is_removed_when_no_row_landed(
        self, seeded, stores, tmp_path: Path, payload, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = resolve_store_path(_ACME)
        stores.close_all()
        db_path.unlink()  # the directory survives; this is the state the guard is for
        dump = tmp_path / "rows.json"
        dump.write_text(json.dumps(payload), encoding="utf-8")

        cc._memory_cmd(_ns(mem_action="import", file=str(dump), store=_ACME))
        out = capsys.readouterr().out
        assert f"still has no {db_path.name}" in out
        # The whole point: the loss signal survives, so a later read still refuses.
        assert not db_path.exists()

    def test_the_refusal_leaves_the_read_guard_reporting_the_loss(
        self, seeded, stores, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """End to end, because the two guards only matter together."""
        db_path = resolve_store_path(_ACME)
        stores.close_all()
        db_path.unlink()
        dump = tmp_path / "rows.json"
        dump.write_text(json.dumps({"semantic": [None]}), encoding="utf-8")

        cc._memory_cmd(_ns(mem_action="import", file=str(dump), store=_ACME))
        capsys.readouterr()
        cc._memory_cmd(_ns(mem_action="export", output=None, store=_ACME))
        out = capsys.readouterr().out
        assert "refusing to create one on a read" in out
        assert '"semantic"' not in out

    def test_an_import_that_lands_one_row_keeps_the_database_it_created(
        self, seeded, stores, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The guard must not cost `import` the verb's whole purpose."""
        db_path = resolve_store_path(_ACME)
        stores.close_all()
        db_path.unlink()
        dump = tmp_path / "rows.json"
        dump.write_text(
            json.dumps({"semantic": [{"key": _ACME_KEY, "value": "kept"}, None]}), "utf-8"
        )

        cc._memory_cmd(_ns(mem_action="import", file=str(dump), store=_ACME))
        out = capsys.readouterr().out
        assert "still has no" not in out
        assert db_path.exists()
        assert _semantic_keys(_ACME) == [_ACME_KEY]

    def test_an_all_skipped_import_into_a_live_store_removes_nothing(
        self, seeded, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Scoped to a database THIS import created, or it would delete real rows."""
        db_path = resolve_store_path(_ACME)
        dump = tmp_path / "rows.json"
        dump.write_text(json.dumps({"semantic": [None]}), encoding="utf-8")

        cc._memory_cmd(_ns(mem_action="import", file=str(dump), store=_ACME))
        out = capsys.readouterr().out
        assert "still has no" not in out
        assert db_path.exists()
        assert _semantic_keys(_ACME) == [_ACME_KEY]

    def test_an_unreadable_file_is_one_line_and_creates_nothing(
        self, seeded, stores, tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`safe_read_file` re-raises OSError, so the refusal has to catch it.

        The refusal is INJECTED rather than provoked with ``chmod(0o000)``: on Windows
        ``os.chmod`` only toggles the read-only flag, so the read succeeds there and a
        permission-based version of this test asserts nothing on that platform. What
        is under test is the ``except (OSError, ValueError)`` branch, and the contract
        is the same whichever errno reaches it.
        """
        db_path = resolve_store_path(_ACME)
        stores.close_all()
        db_path.unlink()
        payload = tmp_path / "rows.json"
        payload.write_text("{}", encoding="utf-8")

        def refuse(_path: str) -> str:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(cc, "safe_read_file", refuse)
        cc._memory_cmd(_ns(mem_action="import", file=str(payload), store=_ACME))
        out = capsys.readouterr().out
        assert "could not be read as JSON" in out
        assert "Permission denied" in out
        assert not db_path.exists()


class TestCreationIsExclusivelyOwned:
    """The removal and the absence check have to be one hold, not two observations.

    Two imports can otherwise each see a named store's database absent, one write
    rows, and the other's empty-import cleanup remove the populated file while
    printing success. The lock is what makes that interleaving impossible, and it is
    invisible to the other tests here -- they pass with it and without it -- so it
    needs its own assertion.
    """

    def _spy(self, monkeypatch) -> list[str]:
        """Record the order of lock entry, store writes and unlink."""
        events: list[str] = []
        real_lock = cc.memory_store_namespace_lock

        @contextlib.contextmanager
        def spy_lock(*a, **kw):
            events.append("lock-enter")
            with real_lock(*a, **kw):
                yield
            events.append("lock-exit")

        monkeypatch.setattr(cc, "memory_store_namespace_lock", spy_lock)
        real_unlink = Path.unlink

        def spy_unlink(self, *a, **kw):
            events.append("unlink")
            return real_unlink(self, *a, **kw)

        monkeypatch.setattr(Path, "unlink", spy_unlink)
        real_require = cc.require_memory_store

        def spy_require(*a, **kw):
            events.append("admit")
            return real_require(*a, **kw)

        monkeypatch.setattr(cc, "require_memory_store", spy_require)
        real_version = cc.memory_store_version

        def spy_version(*a, **kw):
            events.append("version")
            return real_version(*a, **kw)

        monkeypatch.setattr(cc, "memory_store_version", spy_version)
        return events

    def test_the_empty_import_unlinks_inside_the_lock(
        self, seeded, stores, tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = resolve_store_path(_ACME)
        stores.close_all()
        db_path.unlink()
        dump = tmp_path / "rows.json"
        dump.write_text(json.dumps({"semantic": [None]}), encoding="utf-8")
        events = self._spy(monkeypatch)

        cc._memory_cmd(_ns(mem_action="import", file=str(dump), store=_ACME))
        capsys.readouterr()

        assert "lock-enter" in events, events
        assert "unlink" in events, events
        # The ordering IS the property: an unlink after the release would be the same
        # unprotected window, so entry-before and exit-after are both asserted.
        assert events.index("lock-enter") < events.index("unlink") < events.index("lock-exit")
        # Admission and the version check must be INSIDE the hold too, or they answer
        # about a store a concurrent replace can swap out before the open.
        assert events.index("lock-enter") < events.index("admit") < events.index("lock-exit")
        assert events.index("lock-enter") < events.index("version") < events.index("lock-exit")

    def test_a_read_verb_takes_no_namespace_lock(
        self, seeded, monkeypatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Scoped deliberately: serializing every memory verb is not the fix."""
        events = self._spy(monkeypatch)

        cc._memory_cmd(_ns(mem_action="export", output=None, store=_ACME))
        capsys.readouterr()

        assert "lock-enter" not in events, events


class TestAFailedRemovalIsReportedAsOne:
    """The refusal is written from what the filesystem holds, not from intent.

    Removing the empty database is what restores the loss signal, so a removal that
    did not happen cannot be reported as one: the file is still there and a later
    read answers with no rows.
    """

    def test_an_unlink_that_fails_says_the_database_is_still_there(
        self, seeded, stores, tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = resolve_store_path(_ACME)
        stores.close_all()
        db_path.unlink()
        dump = tmp_path / "rows.json"
        dump.write_text(json.dumps({"semantic": [None]}), encoding="utf-8")

        real_unlink = Path.unlink

        def refuse_once(self, *a, **kw):
            if self.name == db_path.name:
                raise OSError(16, "Device or resource busy")
            return real_unlink(self, *a, **kw)

        monkeypatch.setattr(Path, "unlink", refuse_once)
        cc._memory_cmd(_ns(mem_action="import", file=str(dump), store=_ACME))
        out = capsys.readouterr().out

        assert "could NOT be removed" in out
        assert "Remove" in out and str(db_path) in out
        # The false claim is the defect being pinned, so assert it is absent.
        assert "was removed" not in out
        assert db_path.exists()


class TestCleanupTouchesOnlyWhatThisImportCreated:
    """`memory.db-*` is not a set of files this code owns.

    An operator's preserved ``memory.db-recovery`` matches that glob exactly as
    SQLite's own ``-wal`` and ``-shm`` do, so undoing an empty import must not sweep
    it away: that copy is the thing a restore would read.
    """

    def test_a_preserved_recovery_sidecar_survives_the_cleanup(
        self, seeded, stores, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = resolve_store_path(_ACME)
        stores.close_all()
        db_path.unlink()
        recovery = db_path.parent / (db_path.name + "-recovery")
        recovery.write_bytes(b"operator's copy")
        dump = tmp_path / "rows.json"
        dump.write_text(json.dumps({"semantic": [None]}), encoding="utf-8")

        cc._memory_cmd(_ns(mem_action="import", file=str(dump), store=_ACME))
        out = capsys.readouterr().out

        assert f"still has no {db_path.name}" in out
        assert not db_path.exists()
        # The point of the test: the file the operator kept is still readable.
        assert recovery.exists()
        assert recovery.read_bytes() == b"operator's copy"


class TestFacetsDroppedOnImportAreReported:
    """A V1 destination cannot hold facets, so the drop has to be said out loud.

    A V2 export reads the canonical relation, so its rows carry scope, surface, crew,
    session_key and derived_from, and ``import_memory`` writes none of them. The rows
    still land -- that is what makes a V1 store the recovery destination -- but an
    operation reporting only success would make the attribution loss invisible.
    """

    def test_a_facet_bearing_payload_reports_what_the_destination_cannot_hold(
        self, seeded, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dump = tmp_path / "rows.json"
        dump.write_text(
            json.dumps(
                {
                    "semantic": [
                        {"key": "k1", "value": "v1", "scope": "repo:x", "crew": "acme"},
                        {"key": "k2", "value": "v2"},
                    ]
                }
            ),
            encoding="utf-8",
        )

        cc._memory_cmd(_ns(mem_action="import", file=str(dump), store=_BETA))
        out = capsys.readouterr().out

        assert "Import complete:" in out
        # One row carried facets, not both: the count is the load-bearing part.
        assert "1 payload row(s) carried carve facets" in out
        assert "the export file still holds it" in out

    def test_a_payload_without_facets_says_nothing(
        self, seeded, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Control: the note must not fire on an ordinary V1-to-V1 copy."""
        dump = tmp_path / "rows.json"
        dump.write_text(json.dumps({"semantic": [{"key": "k1", "value": "v1"}]}), "utf-8")

        cc._memory_cmd(_ns(mem_action="import", file=str(dump), store=_BETA))
        out = capsys.readouterr().out

        assert "Import complete:" in out
        assert "carried carve facets" not in out

    def test_the_facet_key_list_matches_the_schema(self) -> None:
        """The constant is spelled out to keep `memory_schema` off the boot path.

        That trade only holds if drift is caught, so this is the check that catches
        it: a sixth facet added to the schema fails here rather than going silently
        unreported by the import.
        """
        from kiro_crew import memory_schema

        assert cc._IMPORT_FACET_KEYS == tuple(memory_schema.FACET_NAMES)


class TestAnInterruptedImportIsReportedNotDeleted:
    """The cleanup runs on every exit, and an abort is not a claim that nothing landed.

    An interrupt during ``init()`` or the write loop leaves the same empty database a
    completed empty import would, so a cleanup that only ran on the success path never
    saw it. But rows may have been committed before the abort, so that path reports
    what is there instead of deleting it.
    """

    def test_an_interrupted_import_leaves_the_database_and_says_so(
        self, seeded, stores, tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = resolve_store_path(_ACME)
        stores.close_all()
        db_path.unlink()
        dump = tmp_path / "rows.json"
        dump.write_text(json.dumps({"semantic": [{"key": "k", "value": "v"}]}), "utf-8")

        real_import = cc.VectorMemoryStore.import_memory

        def interrupted(self, data):  # the abort lands after the store exists
            real_import(self, data)
            raise KeyboardInterrupt

        monkeypatch.setattr(cc.VectorMemoryStore, "import_memory", interrupted)
        with pytest.raises(KeyboardInterrupt):
            cc._memory_cmd(_ns(mem_action="import", file=str(dump), store=_ACME))
        out = capsys.readouterr().out

        assert "did not finish" in out
        assert str(db_path) in out
        # Rows written before the abort must survive, so the file stays.
        assert db_path.exists()

    def test_an_interrupt_during_init_removes_the_database_it_created(
        self, seeded, stores, tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A failure before the write loop is a PROVABLE zero, so the file goes.

        `init()` is itself a creation step, and nothing can have been imported when it
        raises -- which is what separates this from an abort inside the write loop.
        """
        db_path = resolve_store_path(_ACME)
        stores.close_all()
        db_path.unlink()
        dump = tmp_path / "rows.json"
        dump.write_text(json.dumps({"semantic": [{"key": "k", "value": "v"}]}), "utf-8")

        real_init = cc.VectorMemoryStore.init

        def interrupted_init(self, *a, **kw):
            real_init(self, *a, **kw)  # the file exists by the time this returns
            raise KeyboardInterrupt

        monkeypatch.setattr(cc.VectorMemoryStore, "init", interrupted_init)
        with pytest.raises(KeyboardInterrupt):
            cc._memory_cmd(_ns(mem_action="import", file=str(dump), store=_ACME))
        out = capsys.readouterr().out

        assert f"still has no {db_path.name}" in out
        assert "did not finish" not in out
        assert not db_path.exists()

    def test_an_empty_store_name_is_refused_and_never_falls_back(
        self, seeded, stores, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--store ''` is a mistake to report, not a request for the default store.

        A shell expanding an unset variable produces it, and defaulting a WRITE on it
        would land another store's rows in the default one.
        """
        dump = tmp_path / "rows.json"
        dump.write_text(json.dumps({"semantic": [{"key": "intruder", "value": "v"}]}), "utf-8")
        before = _semantic_keys(DEFAULT_MEMORY_STORE)

        for verb, kwargs in (
            ("import", {"file": str(dump)}),
            ("export", {"file": str(tmp_path / "out.json")}),
        ):
            cc._memory_cmd(_ns(mem_action=verb, store="", **kwargs))
            out = capsys.readouterr().out
            assert "invalid memory store name" in out, verb
            assert "empty" in out, verb

        assert _semantic_keys(DEFAULT_MEMORY_STORE) == before
        assert "intruder" not in _semantic_keys(DEFAULT_MEMORY_STORE)
        assert not (tmp_path / "out.json").exists()
