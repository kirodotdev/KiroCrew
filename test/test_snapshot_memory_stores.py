"""Named memory stores ride the `memory` component, and their host-local half does not.

A crew member's private memory lives under ``memory_stores/<name>/`` and was outside
every backup path: ``kirocrew snapshot`` and the dashboard export both named the default
store's files root-relative and nothing else, while the manifest still declared `memory`
carried. These tests pin the three properties that close that:

* the tree rides -- markdown, vector file, index, lessons, ownership manifest -- through
  the snapshot AND the export, and comes back through both restore modes;
* the tree's host-local half never rides in EITHER direction: the member signing key,
  the execution logs and the local backup directories are excluded at staging and
  dropped at extraction, and an import strips them from a hand-built archive;
* replace mode does not delete live named stores to honour a bundle written before the
  tree was backed up, because that bundle's silence is not evidence the source had none.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tarfile
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest
from test_snapshot import _make_snapshot, _setup_fake_kirocrew, unpinnable_argv

from kiro_crew import member_memory_backup, platform_compat, portability
from kiro_crew import snapshot as snap
from kiro_crew import snapshot_redact as redact
from kiro_crew.memory import INDEX_DB_FILE
from kiro_crew.memory_stores import (
    EXECUTION_LOGS_DIR_NAME,
    MEMBER_API_KEY_FILE,
    MEMBER_BACKUPS_DIR_NAME,
    MEMBER_MEMORY_ARCHIVE_DIR,
    MEMORY_DB_FILE,
    MEMORY_STORES_DIR_NAME,
    STORE_BACKUP_DIR_NAME,
    is_host_local_store_state,
    named_store_product_file,
)
from kiro_crew.security import is_sensitive_path
from kiro_crew.snapshot import restore_main

STORE = "acme"
ROOT = MEMORY_STORES_DIR_NAME

#: Every file a store on disk holds that IS its memory, data-home-relative.
STORE_MEMORY_FILES = (
    f"{ROOT}/{STORE}/{MEMORY_DB_FILE}",
    f"{ROOT}/{STORE}/{INDEX_DB_FILE}",
    f"{ROOT}/{STORE}/memory/preferences.md",
    f"{ROOT}/{STORE}/memory/history/2026-01-01.md",
    f"{ROOT}/{STORE}/lessons.jsonl",
    f"{ROOT}/{STORE}/member-memory.json",
)

#: Everything under the tree that is THIS host's, and a bundle must never carry.
HOST_LOCAL_FILES = (
    f"{ROOT}/{MEMBER_API_KEY_FILE}",
    f"{ROOT}/{EXECUTION_LOGS_DIR_NAME}/member-abc/agent.log",
    f"{ROOT}/{MEMBER_BACKUPS_DIR_NAME}/{STORE}/pending-restore.json",
    f"{ROOT}/{MEMBER_MEMORY_ARCHIVE_DIR}/retired-store/archive.json",
    f"{ROOT}/{STORE}/{STORE_BACKUP_DIR_NAME}/memory.20260101T000000Z.db",
)


def _make_store_db(path: Path, rows: int) -> None:
    """A fresh store database holding *rows*, replacing any file already at *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    with closing(sqlite3.connect(str(path))) as c:
        c.executescript(
            "CREATE TABLE semantic_memory (key TEXT PRIMARY KEY, value_json TEXT NOT NULL,"
            " confidence REAL DEFAULT 0.5, source TEXT NOT NULL, created_at TEXT NOT NULL,"
            " updated_at TEXT NOT NULL, is_deleted INTEGER DEFAULT 0, embedding BLOB);"
        )
        for i in range(rows):
            c.execute(
                "INSERT INTO semantic_memory (key, value_json, confidence, source, created_at,"
                " updated_at) VALUES (?, '\"v\"', 0.5, 'test', '2026-01-01', '2026-01-01')",
                (f"{STORE}.key{i}",),
            )
        c.commit()


def _plant_named_store(home: Path, *, rows: int = 3) -> None:
    """A named store with every kind of file it can hold, plus the tree's host-local half."""
    store = home / ROOT / STORE
    _make_store_db(store / MEMORY_DB_FILE, rows)
    with closing(sqlite3.connect(str(store / INDEX_DB_FILE))) as c:
        c.execute("CREATE VIRTUAL TABLE memory_fts USING fts5(path, content)")
        c.execute("INSERT INTO memory_fts VALUES ('preferences.md', 'terse')")
        c.commit()
    (store / "memory" / "history").mkdir(parents=True)
    (store / "memory" / "preferences.md").write_text("- prefers terse answers\n", encoding="utf-8")
    (store / "memory" / "history" / "2026-01-01.md").write_text("# day\n", encoding="utf-8")
    (store / "lessons.jsonl").write_text('{"rule": "x"}\n', encoding="utf-8")
    (store / "member-memory.json").write_text('{"owner_member": "acme", "memory_version": 2}')
    # A stray sidecar: the backup API copy is self-contained, so this must not ride.
    (store / f"{MEMORY_DB_FILE}-wal").write_bytes(b"\x00" * 32)
    for rel in HOST_LOCAL_FILES:
        p = home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"host-local")


def _seed_store_generation(home: Path, name: str, version: int) -> Path:
    """Create and close a real store so merge tests can check its next initialization."""
    from member_memory_helpers import declare_v2_store

    from kiro_crew.vector_memory import VectorMemoryStore

    directory = home / ROOT / name
    if version == 2:
        declare_v2_store(home, name)
    db_path = directory / MEMORY_DB_FILE if version == 2 else home / MEMORY_DB_FILE
    with closing(VectorMemoryStore(db_path=db_path)) as store:
        store.init()
        assert store._memory_version == version
    if version == 1:
        import shutil

        directory.mkdir(parents=True)
        shutil.copy2(db_path, directory / MEMORY_DB_FILE)
    (directory / "memory").mkdir(exist_ok=True)
    (directory / "memory" / "preferences.md").write_text(f"generation {version}", encoding="utf-8")
    if version == 2:
        with closing(sqlite3.connect(str(directory / INDEX_DB_FILE))) as conn:
            conn.execute("CREATE VIRTUAL TABLE memory_fts USING fts5(path, content)")
            conn.commit()
    return directory


def _store_bytes(directory: Path) -> dict[str, bytes]:
    return {
        p.relative_to(directory).as_posix(): p.read_bytes()
        for p in directory.rglob("*")
        if p.is_file()
    }


def _per_file_store_merge(src: Path, dst: Path, **kwargs) -> list[str]:
    """Negative control: independently filling files mixes store generations."""
    dst.mkdir(parents=True, exist_ok=True)
    snap._copy_tree_no_overwrite(src, dst, **kwargs)
    return []


def _rows(db: Path) -> int:
    with closing(sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)) as c:
        return c.execute("SELECT count(*) FROM semantic_memory").fetchone()[0]


def _members(tarball: Path) -> set[str]:
    """Archive member names with the bundle root stripped, files only."""
    with tarfile.open(str(tarball)) as tar:
        return {"/".join(Path(m.name).parts[1:]) for m in tar.getmembers() if m.isfile()}


@pytest.fixture(autouse=True)
def _no_gateway(monkeypatch):
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")


@pytest.fixture
def src(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "src"
    home.mkdir()
    _setup_fake_kirocrew(home)
    _plant_named_store(home)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return home


class TestTheLayoutPredicates:
    @pytest.mark.parametrize("rel", HOST_LOCAL_FILES)
    def test_host_local_state_is_recognised_by_position(self, rel):
        assert is_host_local_store_state(Path(rel).parts)

    @pytest.mark.parametrize("rel", STORE_MEMORY_FILES)
    def test_memory_is_not_host_local(self, rel):
        assert not is_host_local_store_state(Path(rel).parts)

    def test_backups_is_only_host_local_at_the_store_level(self):
        """`backups` is an ordinary folder name everywhere but ``memory_stores/<store>/``."""
        assert not is_host_local_store_state(("workspace", "backups", "x"))
        assert not is_host_local_store_state((ROOT, STORE, "memory", "backups", "x"))
        assert is_host_local_store_state((ROOT, STORE, STORE_BACKUP_DIR_NAME))

    def test_a_store_database_is_recognised_by_shape(self):
        assert named_store_product_file((ROOT, STORE, MEMORY_DB_FILE)) == MEMORY_DB_FILE
        assert named_store_product_file((ROOT, STORE, INDEX_DB_FILE)) == INDEX_DB_FILE
        assert snap.is_product_tree_database(f"{ROOT}/{STORE}/{MEMORY_DB_FILE}")

    @pytest.mark.parametrize(
        "parts",
        [
            (ROOT, STORE, "memory", MEMORY_DB_FILE),  # too deep
            (ROOT, "default", MEMORY_DB_FILE),  # the unreachable spelling of the global store
            (ROOT, "Not Valid", MEMORY_DB_FILE),  # malformed name
            (ROOT, STORE, "other.db"),  # an operator's stray database
            ("workspace", STORE, MEMORY_DB_FILE),  # not under the tree
        ],
    )
    def test_only_a_well_formed_store_path_is_ours(self, parts):
        assert named_store_product_file(parts) == ""
        assert not snap.is_product_tree_database("/".join(parts))


class TestSnapshotCarriesNamedStores:
    def test_the_memory_component_declares_the_tree(self):
        assert ROOT in snap.COMPONENTS["memory"].trees
        assert ROOT in snap.COMPONENT_TREES["memory"]

    def test_the_stores_memory_rides_and_its_host_local_half_does_not(self, src, tmp_path):
        tarball = _make_snapshot(src, tmp_path / "out")
        members = _members(tarball)
        for rel in STORE_MEMORY_FILES:
            assert rel in members, rel
        for rel in HOST_LOCAL_FILES:
            assert rel not in members, rel
        assert not any(m.startswith(f"{ROOT}/{EXECUTION_LOGS_DIR_NAME}") for m in members)
        assert not any(m.startswith(f"{ROOT}/{MEMBER_BACKUPS_DIR_NAME}") for m in members)
        assert f"{ROOT}/{STORE}/{MEMORY_DB_FILE}-wal" not in members

    def test_the_manifest_counts_the_stores_and_says_which_format_it_is(self, src, tmp_path):
        tarball = _make_snapshot(src, tmp_path / "out")
        with tarfile.open(str(tarball)) as tar:
            mf = next(m for m in tar.getmembers() if m.name.endswith("/MANIFEST.json"))
            manifest = json.loads(tar.extractfile(mf).read())
        assert manifest["version"] == snap.MANIFEST_VERSION
        assert manifest["version"] >= snap._FIRST_VERSION_WITH_NAMED_STORES
        assert manifest["contents"]["memory_store_count"] == 1

    def test_a_memory_only_selection_carries_the_tree(self, src, tmp_path):
        tarball = _make_snapshot(src, tmp_path / "out", ["--components", "memory"])
        members = _members(tarball)
        assert f"{ROOT}/{STORE}/{MEMORY_DB_FILE}" in members
        assert f"{ROOT}/{STORE}/memory/preferences.md" in members

    def test_a_store_database_is_copied_consistently_not_as_bytes(self, src, tmp_path):
        """The restaged copy opens and holds every row; the stray -wal is not what made it so."""
        tarball = _make_snapshot(src, tmp_path / "out")
        extract = tmp_path / "x"
        with tarfile.open(str(tarball)) as tar:
            tar.extractall(extract, filter="data")
        root = next(extract.iterdir())
        assert _rows(root / ROOT / STORE / MEMORY_DB_FILE) == 3

    def test_a_corrupt_store_database_fails_the_snapshot(self, src, tmp_path):
        """Strict, like `memory.db` at the root and `knowledge.db` under a tree.

        Negative control alongside: an operator's stray ``.db`` in the same directory is
        incidental and still rides as bytes, so it is the SHAPE that decides.
        """
        (src / ROOT / STORE / "notes.db").write_bytes(b"not a database at all")
        _make_snapshot(src, tmp_path / "out-ok")
        (src / ROOT / STORE / MEMORY_DB_FILE).write_bytes(b"torn, not a database")
        with pytest.raises(snap.DatabaseCopyFailed) as excinfo:
            snap._build_snapshot(
                src,
                tmp_path / "out-bad",
                "corrupt-store",
                selected=["memory"],
                allow_unpinned=True,
            )
        assert MEMORY_DB_FILE in str(excinfo.value)

    def test_the_extraction_filter_drops_what_never_ships(self):
        for rel in HOST_LOCAL_FILES:
            info = tarfile.TarInfo(f"kirocrew-snapshot-x/{rel}")
            assert snap._data_filter(info) is None, rel
        kept = snap._data_filter(tarfile.TarInfo(f"kirocrew-snapshot-x/{STORE_MEMORY_FILES[0]}"))
        assert kept is not None
        assert kept.mode == 0o600, "a store's file lands owner-only, as provisioning makes it"
        d = tarfile.TarInfo(f"kirocrew-snapshot-x/{ROOT}/{STORE}")
        d.type = tarfile.DIRTYPE
        assert snap._data_filter(d).mode == 0o700
        # The rejection recorder does not count these as rejections: a deliberate drop in
        # a cleared tree must not abort the replace the way a hostile entry does.
        rejected: list[str] = []
        f = snap._rejection_recording_filter(rejected)
        for rel in HOST_LOCAL_FILES:
            f(tarfile.TarInfo(f"kirocrew-snapshot-x/{rel}"))
        assert rejected == []

    def test_replace_clears_the_tree_so_the_recorder_watches_it(self):
        assert ROOT in snap._tree_roots_replace_clears()


class TestRestoreBringsNamedStoresBack:
    def test_replace_onto_a_fresh_home(self, src, tmp_path, monkeypatch):
        tarball = _make_snapshot(src, tmp_path / "out")
        fresh = tmp_path / "fresh"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        assert restore_main([str(tarball), "--mode", "replace", "--force"] + unpinnable_argv()) == 0
        for rel in STORE_MEMORY_FILES:
            assert (fresh / rel).is_file(), rel
        assert _rows(fresh / ROOT / STORE / MEMORY_DB_FILE) == 3
        for rel in HOST_LOCAL_FILES:
            assert not (fresh / rel).exists(), rel

    def test_replace_makes_the_tree_match_the_archive(self, src, tmp_path, monkeypatch):
        """A store the archive does not carry is removed -- into the rollback set."""
        tarball = _make_snapshot(src, tmp_path / "out")
        dst = tmp_path / "dst"
        dst.mkdir()
        _setup_fake_kirocrew(dst)
        stale = dst / ROOT / "stale"
        _make_store_db(stale / MEMORY_DB_FILE, 1)
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        assert restore_main([str(tarball), "--mode", "replace", "--force"] + unpinnable_argv()) == 0
        assert (dst / ROOT / STORE / MEMORY_DB_FILE).is_file()
        assert not stale.exists()
        rollback = next((dst / ROOT / MEMBER_BACKUPS_DIR_NAME).glob("pre-restore-*"))
        assert (rollback / "stale" / MEMORY_DB_FILE).is_file()

    def test_replace_with_workspace_also_selected_still_replaces_the_stores(
        self, src, tmp_path, monkeypatch
    ):
        """The two workspace/ subtrees defer to the workspace pass; memory_stores/ cannot.

        The old selection logic dropped EVERY memory tree once `workspace` was selected,
        which for the new tree would mean a full restore neither saves nor replaces it.
        """
        tarball = _make_snapshot(src, tmp_path / "out", ["--components", "memory,workspace"])
        dst = tmp_path / "dst"
        dst.mkdir()
        _setup_fake_kirocrew(dst)
        _make_store_db(dst / ROOT / "stale" / MEMORY_DB_FILE, 1)
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        assert restore_main([str(tarball), "--mode", "replace", "--force"] + unpinnable_argv()) == 0
        assert (dst / ROOT / STORE / MEMORY_DB_FILE).is_file()
        assert not (dst / ROOT / "stale").exists()

    def test_merge_keeps_an_existing_store_whole(self, src, tmp_path, monkeypatch, capsys):
        tarball = _make_snapshot(src, tmp_path / "out")
        dst = tmp_path / "dst"
        dst.mkdir()
        _setup_fake_kirocrew(dst)
        # A second store the destination already has, with its OWN database.
        _plant_named_store(dst)
        _make_store_db(dst / ROOT / STORE / MEMORY_DB_FILE, 7)
        (dst / ROOT / STORE / "lessons.jsonl").unlink()
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        assert restore_main([str(tarball), "--mode", "merge", "--force"] + unpinnable_argv()) == 0
        # Kept, not overwritten -- and said so.
        assert _rows(dst / ROOT / STORE / MEMORY_DB_FILE) == 7
        assert "kept the existing store" in capsys.readouterr().out
        assert not (dst / ROOT / STORE / "lessons.jsonl").exists()

    def test_merge_installs_the_whole_store_when_the_destination_has_none(
        self, src, tmp_path, monkeypatch
    ):
        tarball = _make_snapshot(src, tmp_path / "out")
        dst = tmp_path / "dst"
        dst.mkdir()
        _setup_fake_kirocrew(dst)
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        assert restore_main([str(tarball), "--mode", "merge", "--force"] + unpinnable_argv()) == 0
        assert _rows(dst / ROOT / STORE / MEMORY_DB_FILE) == 3
        for rel in STORE_MEMORY_FILES:
            assert (dst / rel).is_file(), rel

    @pytest.mark.parametrize("existing", [True, False])
    @pytest.mark.parametrize("per_file_copy", [False, True])
    def test_merge_preserves_store_generations(
        self, tmp_path, monkeypatch, existing, per_file_copy
    ):
        from kiro_crew.vector_memory import VectorMemoryStore

        source, destination = tmp_path / "source", tmp_path / "destination"
        source.mkdir()
        destination.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(source))
        for name in (STORE, "other"):
            _seed_store_generation(source, name, 2)
        tarball = _make_snapshot(source, tmp_path / "out", ["--components", "memory"])
        monkeypatch.setenv("KIROCREW_HOME", str(destination))
        kept = destination / ROOT / STORE
        if existing:
            _seed_store_generation(destination, STORE, 1)
        before = _store_bytes(kept)
        if per_file_copy:
            monkeypatch.setattr(snap, "_merge_named_stores", _per_file_store_merge)
        assert restore_main([str(tarball), "--mode", "merge", "--force"] + unpinnable_argv()) == 0
        with closing(VectorMemoryStore(db_path=kept / MEMORY_DB_FILE)) as store:
            if existing and per_file_copy:
                assert (kept / "member-memory.json").exists()
                with pytest.raises(ValueError, match="private schema"):
                    store.init()
            else:
                if existing:
                    assert _store_bytes(kept) == before
                    assert not (kept / "member-memory.json").exists()
                    assert not (kept / INDEX_DB_FILE).exists()
                else:
                    for name in ("member-memory.json", MEMORY_DB_FILE, INDEX_DB_FILE):
                        assert (kept / name).is_file()
                store.init()
                assert store._memory_version == (1 if existing else 2)
        for name in ("member-memory.json", MEMORY_DB_FILE, INDEX_DB_FILE):
            assert (destination / ROOT / "other" / name).is_file()

    def test_a_corrupt_store_database_in_the_bundle_is_refused_before_mutation(
        self, src, tmp_path, monkeypatch
    ):
        tarball = _make_snapshot(src, tmp_path / "out")
        # Rewrite the archive with the store's database torn.
        broken = tmp_path / "broken.tar.gz"
        with tarfile.open(str(tarball)) as tar, tarfile.open(str(broken), "w:gz") as out:
            for m in tar.getmembers():
                data = tar.extractfile(m) if m.isfile() else None
                if m.name.endswith(f"/{ROOT}/{STORE}/{MEMORY_DB_FILE}"):
                    payload = b"torn, not a database"
                    m.size = len(payload)
                    out.addfile(m, io.BytesIO(payload))
                elif data is not None:
                    out.addfile(m, data)
                else:
                    out.addfile(m)
        dst = tmp_path / "dst"
        dst.mkdir()
        _setup_fake_kirocrew(dst)
        (dst / "workspace" / "memory" / "keep.md").write_text("live", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        assert restore_main([str(broken), "--mode", "replace", "--force"] + unpinnable_argv()) == 1
        assert (dst / "workspace" / "memory" / "keep.md").is_file(), "nothing moved"


class TestAPreTreeBundleLeavesLiveStoresAlone:
    """A bundle written before the tree existed is silent for a reason replace must not act on."""

    @staticmethod
    def _bundle_without_the_tree(src: Path, out: Path, *, version: int) -> Path:
        """A real snapshot, re-written without ``memory_stores/`` and with an older manifest."""
        real = _make_snapshot(src, out)
        rewritten = out / f"pre-tree-v{version}.tar.gz"
        with tarfile.open(str(real)) as tar, tarfile.open(str(rewritten), "w:gz") as dst:
            for m in tar.getmembers():
                parts = Path(m.name).parts
                if len(parts) > 1 and parts[1] == ROOT:
                    continue
                if m.name.endswith("/MANIFEST.json"):
                    manifest = json.loads(tar.extractfile(m).read())
                    manifest["version"] = version
                    manifest["contents"].pop("memory_store_count", None)
                    payload = json.dumps(manifest).encode()
                    m.size = len(payload)
                    dst.addfile(m, io.BytesIO(payload))
                elif m.isfile():
                    dst.addfile(m, tar.extractfile(m))
                else:
                    dst.addfile(m)
        return rewritten

    def test_the_predicate_reads_the_version(self, tmp_path):
        root = tmp_path / "snap"
        root.mkdir()
        mf = root / "MANIFEST.json"
        mf.write_text(json.dumps({"version": 3, "components": {"memory": "unresolved"}}))
        assert not snap._bundle_carries_named_stores(root)
        mf.write_text(json.dumps({"version": snap.MANIFEST_VERSION, "components": {}}))
        assert snap._bundle_carries_named_stores(root)
        mf.write_text(json.dumps({"version": "4"}))
        assert not snap._bundle_carries_named_stores(root), "a non-integer is older, not newer"
        mf.write_text(json.dumps({"version": 2, "format": "zip"}))
        assert not snap._bundle_carries_named_stores(root)
        mf.write_text(json.dumps({"version": snap.EXPORT_MANIFEST_VERSION, "format": "zip"}))
        assert snap._bundle_carries_named_stores(root)
        mf.unlink()
        assert not snap._bundle_carries_named_stores(root)

    def test_replace_from_a_v3_bundle_keeps_the_live_stores(
        self, src, tmp_path, monkeypatch, capsys
    ):
        bundle = self._bundle_without_the_tree(src, tmp_path / "out", version=3)
        dst = tmp_path / "dst"
        dst.mkdir()
        _setup_fake_kirocrew(dst)
        _plant_named_store(dst, rows=5)
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        assert restore_main([str(bundle), "--mode", "replace", "--force"] + unpinnable_argv()) == 0
        assert _rows(dst / ROOT / STORE / MEMORY_DB_FILE) == 5
        assert (dst / ROOT / MEMBER_API_KEY_FILE).is_file()
        assert "left as they are" in capsys.readouterr().out
        # The rest of the memory component WAS replaced -- this is a mixed result, on purpose.
        assert (dst / "memory.db").is_file()

    def test_replace_from_a_v4_bundle_without_the_tree_clears_it(self, src, tmp_path, monkeypatch):
        """Negative control: the SAME archive shape at v4 means the source had no stores."""
        bundle = self._bundle_without_the_tree(src, tmp_path / "out", version=snap.MANIFEST_VERSION)
        dst = tmp_path / "dst"
        dst.mkdir()
        _setup_fake_kirocrew(dst)
        _plant_named_store(dst, rows=5)
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        assert restore_main([str(bundle), "--mode", "replace", "--force"] + unpinnable_argv()) == 0
        assert not (dst / ROOT / STORE).exists()
        rollback = next((dst / ROOT / MEMBER_BACKUPS_DIR_NAME).glob("pre-restore-*"))
        assert (rollback / STORE / MEMORY_DB_FILE).is_file()

    def test_an_empty_tree_directory_is_not_payload(self, tmp_path):
        """A home whose memory_stores/ holds only runtime state stages an empty directory.

        Counting that directory as payload would let a bundle with no memory at all pass
        the declared-without-payload check and clear live memory with nothing to put back.
        """
        root = tmp_path / "snap"
        (root / ROOT).mkdir(parents=True)
        assert snap._component_payload_absent(root, "memory")
        (root / ROOT / STORE).mkdir()
        (root / ROOT / STORE / "lessons.jsonl").write_text("{}\n")
        assert not snap._component_payload_absent(root, "memory")


class TestRedactionKnowsAStoreDatabase:
    def test_the_store_index_is_derived_and_the_vector_file_is_payload(self):
        assert redact._is_derived_index(f"{ROOT}/{STORE}/{INDEX_DB_FILE}")
        assert redact._is_product_database(f"{ROOT}/{STORE}/{MEMORY_DB_FILE}")
        assert not redact._is_derived_index(f"{ROOT}/{STORE}/{MEMORY_DB_FILE}")
        assert not redact._is_product_database(f"{ROOT}/{STORE}/notes.db")
        # The fixed paths still answer as before.
        assert redact._is_derived_index("memory_index.db")
        assert redact._is_product_database("memory.db")


class TestTheDashboardExportCarriesNamedStores:
    @pytest.fixture
    def home(self, tmp_path, monkeypatch) -> Path:
        d = tmp_path / ".kirocrew"
        d.mkdir()
        _setup_fake_kirocrew(d)
        _plant_named_store(d)
        monkeypatch.setenv("KIROCREW_HOME", str(d))
        with patch("kiro_crew.portability.config_dir", return_value=d):
            yield d

    @staticmethod
    def _names(zip_bytes: bytes) -> tuple[str, set[str]]:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
        prefix = names[0].split("/", 1)[0]
        return prefix, {n.split("/", 1)[1] for n in names if "/" in n}

    def test_the_fence_would_have_refused_the_store_without_the_carve_out(self, home):
        """The premise of `fenced_ok`: a store path IS sensitive to the agent fence."""
        assert is_sensitive_path(str(home / ROOT / STORE / MEMORY_DB_FILE))

    def test_export_carries_the_memory_and_not_the_host_local_half(self, home):
        zip_bytes, manifest = portability.create_export_zip()
        _, names = self._names(zip_bytes)
        for rel in STORE_MEMORY_FILES:
            assert rel in names, rel
        for rel in HOST_LOCAL_FILES:
            assert rel not in names, rel
        assert f"{ROOT}/{STORE}/{MEMORY_DB_FILE}-wal" not in names
        assert manifest["version"] == portability.EXPORT_MANIFEST_VERSION
        assert manifest["contents"]["memory_store_count"] == 1

    def test_the_exported_store_database_is_a_consistent_copy(self, home, tmp_path):
        zip_bytes, _ = portability.create_export_zip()
        prefix, _ = self._names(zip_bytes)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            data = zf.read(f"{prefix}/{ROOT}/{STORE}/{MEMORY_DB_FILE}")
        copy = tmp_path / "copy.db"
        copy.write_bytes(data)
        assert _rows(copy) == 3

    def test_validate_accepts_the_new_version_and_refuses_a_newer_one(self, tmp_path):
        for version, ok in ((portability.EXPORT_MANIFEST_VERSION, True), (99, False), ("3", False)):
            z = tmp_path / f"v{version}.zip"
            with zipfile.ZipFile(z, "w") as zf:
                zf.writestr("export/MANIFEST.json", json.dumps({"version": version}))
            assert portability.validate_import_zip(z)[0] is ok, version

    def test_import_merge_installs_a_missing_store(self, home, tmp_path):
        zip_bytes, _ = portability.create_export_zip()
        archive = tmp_path / "export.zip"
        archive.write_bytes(zip_bytes)
        import shutil

        shutil.rmtree(home / ROOT)
        summary = portability.apply_import_zip(archive, mode="merge")
        assert f"{ROOT} (merged)" in summary["items"]
        assert _rows(home / ROOT / STORE / MEMORY_DB_FILE) == 3
        assert not (home / ROOT / MEMBER_API_KEY_FILE).exists()

    def test_import_merge_names_a_store_whose_database_it_kept(self, home, tmp_path):
        zip_bytes, _ = portability.create_export_zip()
        archive = tmp_path / "export.zip"
        archive.write_bytes(zip_bytes)
        _make_store_db(home / ROOT / STORE / MEMORY_DB_FILE, 9)
        summary = portability.apply_import_zip(archive, mode="merge")
        assert _rows(home / ROOT / STORE / MEMORY_DB_FILE) == 9
        assert any(
            item.startswith(f"{ROOT}/{STORE} (kept the existing store") for item in summary["items"]
        )

    def test_import_replace_installs_the_store(self, home, tmp_path):
        zip_bytes, _ = portability.create_export_zip()
        archive = tmp_path / "export.zip"
        archive.write_bytes(zip_bytes)
        _make_store_db(home / ROOT / "stale" / MEMORY_DB_FILE, 1)
        _make_store_db(home / ROOT / STORE / MEMORY_DB_FILE, 9)
        summary = portability.apply_import_zip(archive, mode="replace")
        assert "full replace" in summary["items"]
        assert _rows(home / ROOT / STORE / MEMORY_DB_FILE) == 3
        assert not (home / ROOT / "stale").exists()

    def test_import_strips_a_planted_signing_key_in_both_modes(self, home, tmp_path):
        """No export writes one, so one in an archive was put there by hand."""
        zip_bytes, _ = portability.create_export_zip()
        prefix, _ = self._names(zip_bytes)
        planted = tmp_path / "planted.zip"
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as src_zf, zipfile.ZipFile(planted, "w") as zf:
            for info in src_zf.infolist():
                zf.writestr(info, src_zf.read(info))
            zf.writestr(f"{prefix}/{ROOT}/{MEMBER_API_KEY_FILE}", b"attacker key")
            zf.writestr(f"{prefix}/{ROOT}/{EXECUTION_LOGS_DIR_NAME}/member-x/log", b"x")
        for mode in ("merge", "replace"):
            (home / ROOT / MEMBER_API_KEY_FILE).unlink(missing_ok=True)
            portability.apply_import_zip(planted, mode=mode)
            assert not (home / ROOT / MEMBER_API_KEY_FILE).exists(), mode
            assert not (home / ROOT / EXECUTION_LOGS_DIR_NAME / "member-x").exists(), mode

    def test_a_pre_tree_export_replaces_without_touching_live_stores(self, home, tmp_path):
        zip_bytes, _ = portability.create_export_zip()
        prefix, _ = self._names(zip_bytes)
        older = tmp_path / "older.zip"
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as src_zf, zipfile.ZipFile(older, "w") as zf:
            for info in src_zf.infolist():
                rel = info.filename.split("/", 1)[1]
                if rel.startswith(f"{ROOT}/"):
                    continue
                if rel == "MANIFEST.json":
                    manifest = json.loads(src_zf.read(info))
                    manifest["version"] = 2
                    zf.writestr(info.filename, json.dumps(manifest))
                else:
                    zf.writestr(info, src_zf.read(info))
        _make_store_db(home / ROOT / STORE / MEMORY_DB_FILE, 9)
        portability.apply_import_zip(older, mode="replace")
        assert _rows(home / ROOT / STORE / MEMORY_DB_FILE) == 9


class TestReplaceRefusesWhileAStoreIsOpen:
    """A store held open would survive the rmtree as an unlinked file and lose its writes."""

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="the lifetime lock is POSIX-only")
    def test_a_held_lifetime_lock_refuses_the_replace_before_any_mutation(
        self, src, tmp_path, monkeypatch, capsys
    ):
        tarball = _make_snapshot(src, tmp_path / "out")
        dst = tmp_path / "dst"
        dst.mkdir()
        _setup_fake_kirocrew(dst)
        _plant_named_store(dst, rows=5)
        (dst / "workspace" / "memory" / "keep.md").write_text("live", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        # What an open V2 store holds for its connection's lifetime.
        fd = member_memory_backup.acquire_store_use_lock(dst / ROOT / STORE / MEMORY_DB_FILE)
        try:
            rc = restore_main([str(tarball), "--mode", "replace", "--force"] + unpinnable_argv())
        finally:
            member_memory_backup.release_store_use_lock(fd)
        assert rc == 1
        out = capsys.readouterr().out
        assert "open right now" in out and STORE in out
        # Nothing moved: not the store, not the rest of memory, and no rollback set.
        assert _rows(dst / ROOT / STORE / MEMORY_DB_FILE) == 5
        assert (dst / "workspace" / "memory" / "keep.md").is_file()
        assert not any(d.name.startswith("pre-restore-") for d in dst.iterdir())
        # Negative control: the same replace goes through once the lock is released.
        assert restore_main([str(tarball), "--mode", "replace", "--force"] + unpinnable_argv()) == 0
        assert _rows(dst / ROOT / STORE / MEMORY_DB_FILE) == 3

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="the lifetime lock is POSIX-only")
    def test_the_barrier_holds_off_a_store_opened_while_it_is_up(self, src):
        """A cold store opened mid-replace must wait, not get unlinked with its writer attached."""
        root = src / ROOT
        # "cold": the archive brings this store and nothing has ever opened it.
        assert not (root / MEMBER_BACKUPS_DIR_NAME / "newcomer").exists()
        with member_memory_backup.hold_stores_for_replace(root, [STORE, "newcomer"]):
            for name in (STORE, "newcomer"):
                lock = root / MEMBER_BACKUPS_DIR_NAME / name / ".store-use.lock"
                assert lock.is_file(), "the barrier creates the lock the opener will contend on"
                fd = member_memory_backup._open_store_use_lock(root / name / MEMORY_DB_FILE)
                try:
                    assert not platform_compat.try_acquire_lock(fd, exclusive=False), name
                finally:
                    os.close(fd)
        # Released with the body: the opener's shared lock goes through now.
        fd = member_memory_backup.acquire_store_use_lock(root / STORE / MEMORY_DB_FILE)
        assert fd is not None
        member_memory_backup.release_store_use_lock(fd)

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="the lifetime lock is POSIX-only")
    def test_the_barrier_refuses_at_once_when_a_store_is_open(self, src):
        root = src / ROOT
        fd = member_memory_backup.acquire_store_use_lock(root / STORE / MEMORY_DB_FILE)
        try:
            with pytest.raises(member_memory_backup.StoresInUse) as excinfo:
                with member_memory_backup.hold_stores_for_replace(root, [STORE, "other"]):
                    pytest.fail("the body must not run")
            assert excinfo.value.names == [STORE]
        finally:
            member_memory_backup.release_store_use_lock(fd)

    def test_replace_keeps_the_host_local_half_and_the_lock_inode(self, src, tmp_path, monkeypatch):
        """The lock the replace holds is the file at `.member-backups/<store>/`; removing
        that directory would let a store opened mid-replace create a fresh, unheld lock.
        So the host-local entries stay across a replace, exactly as the default store's
        `<home>/backups/` does, while every store directory is replaced."""
        tarball = _make_snapshot(src, tmp_path / "out")
        dst = tmp_path / "dst"
        dst.mkdir()
        _setup_fake_kirocrew(dst)
        _plant_named_store(dst, rows=5)
        _make_store_db(dst / ROOT / "stale" / MEMORY_DB_FILE, 1)
        lock = dst / ROOT / MEMBER_BACKUPS_DIR_NAME / STORE / ".store-use.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_bytes(b"")
        before = lock.stat().st_ino
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        assert restore_main([str(tarball), "--mode", "replace", "--force"] + unpinnable_argv()) == 0
        assert _rows(dst / ROOT / STORE / MEMORY_DB_FILE) == 3, "the store was replaced"
        assert not (dst / ROOT / "stale").exists(), "a store the archive lacks is gone"
        for rel in HOST_LOCAL_FILES:
            kept = Path(rel).parts[1] != STORE  # the root-level entries stay in place
            assert (dst / rel).is_file() is kept, rel
        # A named V1 store's own backups/ sits INSIDE the store directory, so it goes with
        # the directory the archive replaces -- into the rollback set, not into nothing.
        rollback = next((dst / ROOT / MEMBER_BACKUPS_DIR_NAME).glob("pre-restore-*"))
        assert (rollback / STORE / STORE_BACKUP_DIR_NAME).is_dir()
        assert lock.stat().st_ino == before, "the held lock file is the same inode"


class TestImportValidatesStoreDatabasesBeforeMutating:
    @pytest.fixture
    def home(self, tmp_path, monkeypatch) -> Path:
        d = tmp_path / ".kirocrew"
        d.mkdir()
        _setup_fake_kirocrew(d)
        _plant_named_store(d)
        monkeypatch.setenv("KIROCREW_HOME", str(d))
        with patch("kiro_crew.portability.config_dir", return_value=d):
            yield d

    @staticmethod
    def _archive_with_torn_store_db(home: Path, tmp_path: Path) -> Path:
        zip_bytes, _ = portability.create_export_zip()
        prefix = zipfile.ZipFile(io.BytesIO(zip_bytes)).namelist()[0].split("/", 1)[0]
        torn = tmp_path / "torn.zip"
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as src_zf, zipfile.ZipFile(torn, "w") as zf:
            for info in src_zf.infolist():
                if info.filename == f"{prefix}/{ROOT}/{STORE}/{MEMORY_DB_FILE}":
                    zf.writestr(info.filename, b"torn, not a database")
                else:
                    zf.writestr(info, src_zf.read(info))
        return torn

    @pytest.mark.parametrize("mode", ["merge", "replace"])
    def test_a_torn_store_database_is_refused_with_nothing_written(self, home, tmp_path, mode):
        torn = self._archive_with_torn_store_db(home, tmp_path)
        import shutil

        # Merge installs a store the destination lacks -- the case that copied verbatim.
        shutil.rmtree(home / ROOT / STORE)
        (home / "workspace" / "memory" / "keep.md").write_text("live", encoding="utf-8")
        with pytest.raises(snap.SourceComponentUnsound) as excinfo:
            portability.apply_import_zip(torn, mode=mode)
        assert MEMORY_DB_FILE in str(excinfo.value)
        assert not (home / ROOT / STORE).exists()
        assert (home / "workspace" / "memory" / "keep.md").is_file()

    def test_a_sound_archive_still_imports(self, home, tmp_path):
        """Negative control for the validator: the same archive with a sound store passes."""
        zip_bytes, _ = portability.create_export_zip()
        archive = tmp_path / "ok.zip"
        archive.write_bytes(zip_bytes)
        import shutil

        shutil.rmtree(home / ROOT / STORE)
        portability.apply_import_zip(archive, mode="merge")
        assert _rows(home / ROOT / STORE / MEMORY_DB_FILE) == 3


@pytest.mark.asyncio
class TestThePortabilityRoutesAreOwnerOnly:
    """The export is the whole install, every member's private memory included."""

    @staticmethod
    def _request(method: str, path: str, *, user: str):
        from aiohttp import web
        from aiohttp.test_utils import make_mocked_request
        from dashboard_owner_helpers import NoConfiguredOwner

        app = web.Application()
        app["state"] = NoConfiguredOwner()
        req = make_mocked_request(method, path, app=app)
        req["user"] = user
        req["app"] = ""
        return req

    @pytest.fixture
    def handlers(self):
        import kiro_crew.dashboard.handlers.portability as ph

        events: list[dict] = []

        class _FakeSel:
            def log_api_access(self, **kw):
                events.append(kw)

        with patch.object(ph, "_sel", lambda: _FakeSel()):
            yield ph, events

    async def test_a_non_owner_cannot_export(self, handlers):
        ph, events = handlers
        called = []
        with patch.object(ph, "create_export_zip", lambda: called.append(1) or (b"x", {})):
            resp = await ph.api_portability_export(
                self._request("GET", "/api/portability/export", user="slack-participant")
            )
        assert resp.status == 403
        assert called == [], "the archive must not even be built for a non-owner"

    async def test_the_owner_can_export(self, handlers):
        ph, _ = handlers
        with patch.object(ph, "create_export_zip", lambda: (b"zip", {"created_at": "t"})):
            resp = await ph.api_portability_export(
                self._request("GET", "/api/portability/export", user="local-app")
            )
        assert resp.status == 200

    async def test_a_non_owner_cannot_import(self, handlers, tmp_path):
        ph, _ = handlers
        applied = []
        with patch.object(ph, "apply_import_zip", lambda p, m: applied.append(1)):
            resp = await ph.api_portability_import(
                self._request("POST", "/api/portability/import?mode=merge", user="guest")
            )
        assert resp.status == 403
        assert applied == []

    async def test_a_refused_import_answers_with_the_reason(self, handlers, tmp_path):
        ph, events = handlers
        upload = tmp_path / "upload.zip"
        upload.write_bytes(b"")

        async def _fake_read_upload(request):
            return upload, None

        def _refuse(p, m):
            raise snap.SourceComponentUnsound("memory_stores/acme/memory.db is torn")

        with (
            patch.object(ph, "_read_upload_file", _fake_read_upload),
            patch.object(ph, "validate_import_zip", lambda p: (True, "", {"version": 3})),
            patch.object(ph, "apply_import_zip", _refuse),
        ):
            resp = await ph.api_portability_import(
                self._request("POST", "/api/portability/import?mode=merge", user="local-app")
            )
        assert resp.status == 409
        assert "torn" in resp.text
        assert events[-1]["outcome"] == "denied"


class TestNamedStoreRollbackSafety:
    def test_rollback_stays_inside_the_store_fence(self, src, tmp_path, monkeypatch, capsys):
        tarball = _make_snapshot(src, tmp_path / "out")
        dst = tmp_path / "dst"
        dst.mkdir()
        _setup_fake_kirocrew(dst)
        _plant_named_store(dst, rows=5)
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        assert restore_main([str(tarball), "--mode", "replace", "--force"] + unpinnable_argv()) == 0
        assert not list(dst.glob(f"pre-restore-*/{ROOT}"))
        saved = next((dst / ROOT / MEMBER_BACKUPS_DIR_NAME).glob("pre-restore-*"))
        assert _rows(saved / STORE / MEMORY_DB_FILE) == 5
        assert is_sensitive_path(str(saved / STORE / MEMORY_DB_FILE))
        assert not (saved / MEMBER_API_KEY_FILE).exists()
        assert not (saved / MEMBER_BACKUPS_DIR_NAME).exists(), "do not copy into the copy itself"
        assert not (saved / EXECUTION_LOGS_DIR_NAME).exists()
        assert (saved / STORE / STORE_BACKUP_DIR_NAME).is_dir()
        assert str(saved) in capsys.readouterr().out
        # The ordinary rollback remains outside; the installed store has the incoming rows.
        assert list(dst.glob("pre-restore-*/memory.db"))
        assert _rows(dst / ROOT / STORE / MEMORY_DB_FILE) == 3

    def test_failed_replace_keeps_root_and_lock_inodes(self, src, tmp_path, monkeypatch):
        (src / "MANIFEST.json").write_text(json.dumps({"version": snap.MANIFEST_VERSION}))
        dst = tmp_path / "dst"
        dst.mkdir()
        _plant_named_store(dst, rows=5)
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        root = dst / ROOT
        lock = root / MEMBER_BACKUPS_DIR_NAME / STORE / ".store-use.lock"
        lock.write_bytes(b"")
        identities = (root.stat().st_ino, lock.stat().st_ino)
        copy = snap._copytree_safe
        attempts = []

        def fail_forward(source, target, **kwargs):
            if target == root:
                attempts.append(source)
                if source == src / ROOT:
                    assert not (root / STORE).exists(), "fail after the clear"
                    (root / "partial").mkdir()
                    (root / "partial" / "note").write_text("incomplete")
                    raise OSError("injected store copy failure")
                assert (root.stat().st_ino, lock.stat().st_ino) == identities
                if platform_compat.IS_POSIX:
                    fd = member_memory_backup._open_store_use_lock(root / STORE / MEMORY_DB_FILE)
                    try:
                        assert not platform_compat.try_acquire_lock(fd, exclusive=False)
                    finally:
                        os.close(fd)
            return copy(source, target, **kwargs)

        monkeypatch.setattr(snap, "_copytree_safe", fail_forward)
        with pytest.raises(OSError, match="injected store copy failure"):
            snap._do_replace(src, dst, ["memory"], allow_unpinned=True)
        assert len(attempts) == 2, "the saved tree is copied back after the failed install"
        assert _rows(root / STORE / MEMORY_DB_FILE) == 5
        assert not (root / "partial").exists()
        assert (root.stat().st_ino, lock.stat().st_ino) == identities
        for rel in HOST_LOCAL_FILES:
            assert (dst / rel).is_file(), rel
        # The barrier must be released even when the forward copy fails.
        with member_memory_backup.hold_stores_for_replace(root, [STORE]):
            pass

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="the lifetime lock is POSIX-only")
    def test_named_v1_store_holds_lock_until_close(self, tmp_path, monkeypatch):
        from kiro_crew.vector_memory import VectorMemoryStore

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        db = tmp_path / ROOT / STORE / MEMORY_DB_FILE
        store = VectorMemoryStore(db_path=db)
        try:
            store.init()
            assert store._memory_store_name == STORE
            assert store._memory_version == 1
            assert not (db.parent / "member-memory.json").exists()
            assert store._store_use_lock_fd is not None
            with pytest.raises(member_memory_backup.StoresInUse):
                with member_memory_backup.hold_stores_for_replace(tmp_path / ROOT, [STORE]):
                    pytest.fail("a named V1 connection must block replacement")
        finally:
            store.close()
        with member_memory_backup.hold_stores_for_replace(tmp_path / ROOT, [STORE]):
            pass
        # The global V1 store is not part of the named-store barrier.
        default = VectorMemoryStore(db_path=tmp_path / MEMORY_DB_FILE)
        try:
            default.init()
            assert default._memory_store_name == ""
            assert default._store_use_lock_fd is None
        finally:
            default.close()

    def test_a_linked_home_ancestor_keeps_rollback_inside_the_real_fence(
        self, src, tmp_path, monkeypatch
    ):
        from conftest import make_dir_link

        tarball = _make_snapshot(src, tmp_path / "out")
        real_parent = tmp_path / "real-parent"
        dst = real_parent / "home"
        dst.mkdir(parents=True)
        _plant_named_store(dst, rows=5)
        alias = tmp_path / "home-alias"
        make_dir_link(alias, real_parent)
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        with patch.object(snap, "_mc_dir", return_value=alias / "home"):
            assert (
                restore_main([str(tarball), "--mode", "replace", "--force"] + unpinnable_argv())
                == 0
            )
        saved = next((dst / ROOT / MEMBER_BACKUPS_DIR_NAME).glob("pre-restore-*"))
        assert _rows(saved / STORE / MEMORY_DB_FILE) == 5
        assert _rows(dst / ROOT / STORE / MEMORY_DB_FILE) == 3
        assert not list(dst.glob(f"pre-restore-*/{ROOT}"))

    def test_incomplete_rollback_reports_the_fenced_copy(self, src, tmp_path, monkeypatch):
        (src / "MANIFEST.json").write_text(json.dumps({"version": snap.MANIFEST_VERSION}))
        dst = tmp_path / "dst"
        dst.mkdir()
        _plant_named_store(dst, rows=5)
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        copy = snap._copytree_safe

        def fail_store_copy(source, target, **kwargs):
            if target == dst / ROOT:
                raise OSError("store copy unavailable")
            return copy(source, target, **kwargs)

        monkeypatch.setattr(snap, "_copytree_safe", fail_store_copy)
        with pytest.raises(snap.RollbackIncomplete) as excinfo:
            snap._do_replace(src, dst, ["memory"], allow_unpinned=True)
        saved = next((dst / ROOT / MEMBER_BACKUPS_DIR_NAME).glob("pre-restore-*"))
        assert excinfo.value.store_backup == saved
        assert _rows(saved / STORE / MEMORY_DB_FILE) == 5


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="shared store admission is POSIX-only")
@pytest.mark.parametrize("without_admission", [False, True])
@pytest.mark.asyncio
async def test_named_v1_markdown_write_cannot_be_lost_during_replace(
    src, tmp_path, monkeypatch, without_admission
):
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers._shared import (
        markdown_memory_for_store,
        release_markdown_memory_store,
    )

    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "config.json").write_text(
        json.dumps({"memory_stores": {STORE: {}}}), encoding="utf-8"
    )
    monkeypatch.setenv("KIROCREW_HOME", str(destination))
    if without_admission:
        monkeypatch.setattr(member_memory_backup, "acquire_store_use_lock", lambda path: None)
    state = SimpleNamespace()
    memory = await markdown_memory_for_store(state, STORE)
    portability._strip_host_local_store_state(src)
    assert memory.vector_store is None
    assert not (destination / ROOT / STORE / MEMORY_DB_FILE).exists()
    (src / "MANIFEST.json").write_text(json.dumps({"version": snap.MANIFEST_VERSION}))
    mutate = snap._do_replace_mutations
    acknowledged = []

    def write_after_backup(*args, **kwargs):
        acknowledged.append(memory.write_preferences("acknowledged write"))
        return mutate(*args, **kwargs)

    monkeypatch.setattr(snap, "_do_replace_mutations", write_after_backup)
    try:
        if without_admission:
            snap._do_replace(src, destination, ["memory"], allow_unpinned=True)
            assert acknowledged == [True]
            assert memory.read_preferences() != "acknowledged write"
            saved = next((destination / ROOT / MEMBER_BACKUPS_DIR_NAME).glob("pre-restore-*"))
            assert (saved / STORE / "memory/preferences.md").read_text() != "acknowledged write"
        else:
            with pytest.raises(snap.NamedStoresInUse):
                snap._do_replace(src, destination, ["memory"], allow_unpinned=True)
            assert acknowledged == []
            await release_markdown_memory_store(state, STORE)
            # A request retaining the object still owns its admission after cache removal.
            with pytest.raises(member_memory_backup.StoresInUse):
                with member_memory_backup.hold_stores_for_replace(destination / ROOT, [STORE]):
                    pass
    finally:
        await release_markdown_memory_store(state, STORE)
        memory = None
    import asyncio
    import gc

    await asyncio.to_thread(gc.collect)
    with member_memory_backup.hold_stores_for_replace(destination / ROOT, [STORE]):
        pass


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="shared store admission is POSIX-only")
@pytest.mark.parametrize("without_admission", [False, True])
@pytest.mark.parametrize("phase", ["snapshot-copy", "snapshot-database", "export"])
def test_backup_keeps_named_generation_for_the_whole_read(
    src, tmp_path, monkeypatch, without_admission, phase
):
    if without_admission:
        monkeypatch.setattr(member_memory_backup, "acquire_store_use_lock", lambda path: None)
    blocked = []

    def probe():
        try:
            with member_memory_backup.hold_stores_for_replace(src / ROOT, [STORE]):
                blocked.append(False)
        except member_memory_backup.StoresInUse:
            blocked.append(True)

    if phase == "export":
        original = portability._backup_sqlite

        def copy_database(source, output):
            if source.parent.name == STORE:
                probe()
            return original(source, output)

        monkeypatch.setattr(portability, "_backup_sqlite", copy_database)
        with patch.object(portability, "_mc_dir", return_value=src):
            portability.create_export_zip()
    else:
        method = "_copytree_safe" if phase == "snapshot-copy" else "_restage_databases"
        original = getattr(snap, method)

        def copy_tree(source, target, **kwargs):
            if source == src / ROOT:
                probe()
            return original(source, target, **kwargs)

        monkeypatch.setattr(snap, method, copy_tree)
        _make_snapshot(src, tmp_path / "out", ["--components", "memory"])
    assert blocked and all(value is not without_admission for value in blocked)
    with member_memory_backup.hold_stores_for_replace(src / ROOT, [STORE]):
        pass


@pytest.mark.parametrize("mode", ["snapshot", "zip"])
@pytest.mark.parametrize("leftover", [None, "memory/preferences.md", "lessons.jsonl"])
def test_merge_keeps_markdown_only_and_empty_store_directories(
    src, tmp_path, monkeypatch, mode, leftover, capsys
):
    if mode == "snapshot":
        archive = _make_snapshot(src, tmp_path / "out", ["--components", "memory"])
    else:
        archive = tmp_path / "export.zip"
        with patch.object(portability, "_mc_dir", return_value=src):
            archive.write_bytes(portability.create_export_zip()[0])
    destination = tmp_path / "destination"
    kept = destination / ROOT / STORE
    kept.mkdir(parents=True)
    if leftover:
        local = kept / leftover
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text("local memory", encoding="utf-8")
    before = _store_bytes(kept)
    monkeypatch.setenv("KIROCREW_HOME", str(destination))
    if mode == "snapshot":
        assert restore_main([str(archive), "--mode", "merge", "--force"] + unpinnable_argv()) == 0
        assert "kept the existing store" in capsys.readouterr().out
    else:
        result = portability.apply_import_zip(archive)
        assert any("kept the existing store" in item for item in result["items"])
    assert _store_bytes(kept) == before


@pytest.mark.parametrize("without_namespace", [False, True])
@pytest.mark.parametrize("fail_replace", [False, True])
def test_provisioning_waits_for_replace_and_rollback(
    src, tmp_path, monkeypatch, without_namespace, fail_replace
):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager

    from kiro_crew import memory_stores, pinned_fs
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig

    destination = tmp_path / "destination"
    destination.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(destination))
    config = KiroCrewConfig()
    config.agents["new-member"] = KiroCrewAgentConfig()
    portability._strip_host_local_store_state(src)
    (src / "MANIFEST.json").write_text(json.dumps({"version": snap.MANIFEST_VERSION}))
    pending = threading.Event()
    file_lock = platform_compat.file_lock
    main_thread = threading.get_ident()

    @contextmanager
    def observe_lock(fd, **kwargs):
        path = pinned_fs.fd_real_path(fd)
        if threading.get_ident() != main_thread and path and path.endswith(".namespace.lock"):
            pending.set()
        with file_lock(fd, **kwargs):
            yield

    monkeypatch.setattr(platform_compat, "file_lock", observe_lock)
    provision = memory_stores.provision_member_memory
    publish = memory_stores.persist_member_config
    if without_namespace:
        from kiro_crew.vector_memory import VectorMemoryStore

        provision = provision.__wrapped__
        publish = publish.__wrapped__
        monkeypatch.setattr(VectorMemoryStore, "init", VectorMemoryStore.init.__wrapped__)

    def create():
        name = provision(config, "new-member")
        publish(config, "new-member", create=True)
        return name

    mutate = snap._do_replace_mutations
    futures = []
    with ThreadPoolExecutor(max_workers=1) as worker:

        def begin_creation(*args, **kwargs):
            future = worker.submit(create)
            futures.append(future)
            if without_namespace:
                future.result(timeout=10)
            else:
                assert pending.wait(timeout=10), "creator must reach namespace admission"
            mutate(*args, **kwargs)
            if fail_replace:
                raise OSError("injected failure after store replacement")

        monkeypatch.setattr(snap, "_do_replace_mutations", begin_creation)
        if fail_replace:
            with pytest.raises(OSError, match="injected failure"):
                snap._do_replace(src, destination, ["memory"], allow_unpinned=True)
        else:
            snap._do_replace(src, destination, ["memory"], allow_unpinned=True)
        name = futures[0].result(timeout=10)
    store = destination / ROOT / name
    assert store.exists() is not without_namespace
    if not without_namespace:
        assert (store / MEMORY_DB_FILE).is_file()
        assert (store / "member-memory.json").is_file()
        assert KiroCrewConfig.load().agents["new-member"].memory_store == name


@pytest.mark.skipif(not platform_compat.IS_WINDOWS, reason="Windows native SQLite handle contract")
def test_windows_open_database_refuses_directory_removal(tmp_path):
    import shutil

    directory = tmp_path / "store"
    directory.mkdir()
    database = directory / MEMORY_DB_FILE
    with closing(sqlite3.connect(str(database))) as connection:
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.commit()
        with pytest.raises(OSError):
            shutil.rmtree(directory)
        assert database.exists()
        connection.execute("INSERT INTO sample VALUES ('still open')")
        connection.commit()
    with closing(sqlite3.connect(str(database))) as connection:
        assert connection.execute("SELECT value FROM sample").fetchall() == [("still open",)]


@pytest.mark.parametrize("without_admission", [False, True])
@pytest.mark.parametrize("fail_replace", [False, True])
@pytest.mark.parametrize("kind", ["preferences", "projects", "history", "lessons"])
def test_file_only_writes_serialize_with_replace(
    tmp_path, monkeypatch, without_admission, fail_replace, kind
):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager, nullcontext

    from kiro_crew import pinned_fs
    from kiro_crew.learn import Lesson, LessonStore
    from kiro_crew.memory import MemoryStore

    destination = tmp_path / "destination"
    source = tmp_path / "source"
    monkeypatch.setenv("KIROCREW_HOME", str(destination))
    memory = MemoryStore(workspace=destination / ROOT / STORE)
    memory.init()
    lessons = LessonStore(base_dir=destination / ROOT / STORE)
    incoming = source / ROOT / STORE / "memory"
    incoming.mkdir(parents=True)
    (incoming / "preferences.md").write_text("restored", encoding="utf-8")
    (source / "MANIFEST.json").write_text(json.dumps({"version": snap.MANIFEST_VERSION}))
    assert not (destination / ROOT / STORE / MEMORY_DB_FILE).exists()
    # Exercise the protocol without relying on SQLite or POSIX lifetime admission.
    monkeypatch.setattr(member_memory_backup, "acquire_store_use_lock", lambda path: None)
    monkeypatch.setattr(snap, "hold_stores_for_replace", lambda *args: nullcontext())
    target, method, args = {
        "preferences": (memory, "write_preferences", ("acknowledged",)),
        "projects": (memory, "write_projects", ("acknowledged",)),
        "history": (memory, "append_history", ("acknowledged",)),
        "lessons": (lessons, "save", (Lesson("2026-01-01", "acknowledged", "tool"),)),
    }[kind]
    if without_admission:
        import inspect

        # Disable every nested operation wrapper as well as the entry being tested.
        for cls in (MemoryStore, LessonStore):
            for name, value in list(vars(cls).items()):
                if getattr(value, "__wrapped__", None) is not None:
                    monkeypatch.setattr(cls, name, inspect.unwrap(value))
    pending = threading.Event()
    main_thread = threading.get_ident()
    lock = platform_compat.file_lock

    @contextmanager
    def observe(fd, **kwargs):
        path = pinned_fs.fd_real_path(fd)
        if threading.get_ident() != main_thread and path and path.endswith(".namespace.lock"):
            pending.set()
        with lock(fd, **kwargs):
            yield

    monkeypatch.setattr(platform_compat, "file_lock", observe)
    mutate = snap._do_replace_mutations
    futures = []
    with ThreadPoolExecutor(max_workers=1) as worker:

        def interleave(*call_args, **kwargs):
            future = worker.submit(getattr(target, method), *args)
            futures.append(future)
            if without_admission:
                future.result(timeout=10)
            else:
                assert pending.wait(timeout=10)
            mutate(*call_args, **kwargs)
            if fail_replace:
                raise OSError("injected replacement failure")

        monkeypatch.setattr(snap, "_do_replace_mutations", interleave)
        if fail_replace:
            with pytest.raises(OSError, match="injected replacement failure"):
                snap._do_replace(source, destination, ["memory"], allow_unpinned=True)
        else:
            snap._do_replace(source, destination, ["memory"], allow_unpinned=True)
        futures[0].result(timeout=10)
    files = list((destination / ROOT / STORE).rglob("*.md"))
    files += list((destination / ROOT / STORE).glob("*.jsonl"))
    survived = any("acknowledged" in path.read_text(encoding="utf-8") for path in files)
    assert survived is not without_admission


@pytest.mark.parametrize("without_admission", [False, True])
@pytest.mark.parametrize("kind", ["read_preferences", "read_projects", "lessons"])
def test_file_only_readers_hold_namespace(tmp_path, monkeypatch, kind, without_admission):
    import inspect
    from contextlib import contextmanager

    from kiro_crew import pinned_fs
    from kiro_crew.learn import Lesson, LessonStore
    from kiro_crew.memory import MemoryStore

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    directory = tmp_path / ROOT / STORE
    memory = MemoryStore(workspace=directory)
    memory.init()
    lessons = LessonStore(base_dir=directory)
    lessons.save(Lesson("2026-01-01", "stored", "tool"))
    lessons._cache = None
    if without_admission:
        for cls in (MemoryStore, LessonStore):
            for name, value in list(vars(cls).items()):
                if getattr(value, "__wrapped__", None) is not None:
                    monkeypatch.setattr(cls, name, inspect.unwrap(value))
    active = []
    reads = []
    lock = platform_compat.file_lock
    open_path = Path.open

    @contextmanager
    def observe_lock(fd, **kwargs):
        path = pinned_fs.fd_real_path(fd)
        namespace = bool(path and path.endswith(".namespace.lock"))
        with lock(fd, **kwargs):
            if namespace:
                active.append(True)
            try:
                yield
            finally:
                if namespace:
                    active.pop()

    def observe_read(path, *args, **kwargs):
        if path.is_relative_to(directory):
            reads.append(bool(active))
        return open_path(path, *args, **kwargs)

    monkeypatch.setattr(platform_compat, "file_lock", observe_lock)
    monkeypatch.setattr(Path, "open", observe_read)
    if kind == "lessons":
        lessons.load_all()
    else:
        getattr(memory, kind)()
    assert reads and all(reads) is not without_admission


def test_nested_named_memory_operations_release_namespace_on_failure(tmp_path, monkeypatch):
    from kiro_crew import memory_stores
    from kiro_crew.memory import MemoryStore

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    memory = MemoryStore(workspace=tmp_path / ROOT / STORE)
    memory.init()
    memory.add_preference("nested read and write")
    assert "nested read and write" in memory.read_preferences()
    with pytest.raises(RuntimeError, match="injected"):
        with memory_stores.memory_store_namespace_lock():
            with memory_stores.memory_store_namespace_lock():
                raise RuntimeError("injected")
    assert not memory_stores._NAMESPACE_LOCK_STATE.roots
    memory.add_preference("after failure")
    assert "after failure" in memory.read_preferences()


@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize("fail_replace", [False, True])
@pytest.mark.parametrize("without_preservation", [False, True])
def test_retirement_survives_replace_and_rollback(
    tmp_path, monkeypatch, late, fail_replace, without_preservation
):
    from kiro_crew import memory_stores

    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(source))
    _seed_store_generation(source, STORE, 2)
    portability._strip_host_local_store_state(source)
    (source / "MANIFEST.json").write_text(json.dumps({"version": snap.MANIFEST_VERSION}))
    monkeypatch.setenv("KIROCREW_HOME", str(destination))
    _seed_store_generation(destination, STORE, 2)
    if without_preservation:
        monkeypatch.setattr(
            memory_stores,
            "_HOST_LOCAL_ROOT_ENTRIES",
            memory_stores._HOST_LOCAL_ROOT_ENTRIES - {MEMBER_MEMORY_ARCHIVE_DIR},
        )
    if not late:
        memory_stores.archive_member_memory_store(STORE, STORE)
    mutate = snap._do_replace_mutations

    def retire_then_replace(*args, **kwargs):
        if late:
            memory_stores.archive_member_memory_store(STORE, STORE)
        mutate(*args, **kwargs)
        if fail_replace:
            raise OSError("injected replacement failure")

    monkeypatch.setattr(snap, "_do_replace_mutations", retire_then_replace)
    if fail_replace:
        with pytest.raises(OSError, match="injected replacement failure"):
            snap._do_replace(source, destination, ["memory"], allow_unpinned=True)
    else:
        snap._do_replace(source, destination, ["memory"], allow_unpinned=True)
    preserved = not without_preservation or (fail_replace and not late)
    if preserved:
        with pytest.raises(memory_stores.UnknownMemoryStore, match="archived"):
            memory_stores.require_member_memory_not_archived(STORE)
    else:
        memory_stores.require_member_memory_not_archived(STORE)


@pytest.mark.asyncio
@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("checked_phase", ["factory", "read"])
async def test_named_lessons_request_offloads_locked_operations(monkeypatch, inline, checked_phase):
    import asyncio
    import threading
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.dashboard.handlers import cron as handlers
    from kiro_crew.learn import Lesson

    loop_thread = threading.get_ident()
    seen = []

    def factory(**kwargs):
        if checked_phase == "factory":
            assert threading.get_ident() != loop_thread, "locked factory ran on event loop"
        seen.append("factory")
        return SimpleNamespace(vector_store=None)

    def read():
        if checked_phase == "read":
            assert threading.get_ident() != loop_thread, "locked read ran on event loop"
        seen.append("read")
        return [Lesson("2026-01-01", "stored lesson", "tool")]

    monkeypatch.setattr(handlers, "_blocks_reads_session", lambda *args: False)
    monkeypatch.setattr(
        handlers, "resolve_lesson_memory_store", AsyncMock(return_value=(STORE, None))
    )
    monkeypatch.setattr(handlers, "_prepare_private_lesson_store", AsyncMock(return_value=None))
    monkeypatch.setattr(handlers.ContextBuilder, "get_memory_for", factory)
    monkeypatch.setattr(
        handlers, "_lesson_jsonl_store", lambda *args: SimpleNamespace(load_all=read)
    )
    app = web.Application()
    app["state"] = SimpleNamespace()
    request = make_mocked_request("GET", "/api/lessons", app=app)
    if inline:

        async def on_loop(function, *args, **kwargs):
            return function(*args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", on_loop)
        with pytest.raises(AssertionError, match="ran on event loop"):
            await handlers.api_lessons(request)
    else:
        response = await handlers.api_lessons(request)
        assert response.status == 200
        assert json.loads(response.text)["lessons"][0]["rule"] == "stored lesson"
        assert seen == ["factory", "read"]


@pytest.mark.parametrize("fail_replace", [False, True])
@pytest.mark.parametrize(
    "without_namespace",
    [
        False,
        pytest.param(
            True,
            marks=pytest.mark.skipif(
                platform_compat.IS_WINDOWS,
                reason="unlinked open SQLite negative control is POSIX-only",
            ),
        ),
    ],
)
def test_cold_vector_open_waits_until_replace_or_rollback_finishes(
    src, tmp_path, monkeypatch, without_namespace, fail_replace
):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager

    from kiro_crew import memory_stores, pinned_fs
    from kiro_crew.vector_memory import VectorMemoryStore

    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "config.json").write_text(
        json.dumps({"memory_stores": {"cold": {}}}), encoding="utf-8"
    )
    monkeypatch.setenv("KIROCREW_HOME", str(destination))
    database = memory_stores.resolve_store_path("cold")
    assert not database.parent.exists()
    portability._strip_host_local_store_state(src)
    (src / "MANIFEST.json").write_text(json.dumps({"version": snap.MANIFEST_VERSION}))
    store = VectorMemoryStore(db_path=database)
    assert store._memory_store_name == "cold"
    if without_namespace:
        monkeypatch.setattr(VectorMemoryStore, "init", VectorMemoryStore.init.__wrapped__)
    started = threading.Event()
    owner = threading.get_ident()
    file_lock = platform_compat.file_lock

    @contextmanager
    def observe_lock(fd, **kwargs):
        path = pinned_fs.fd_real_path(fd)
        if threading.get_ident() != owner and path and path.endswith(".namespace.lock"):
            started.set()
        with file_lock(fd, **kwargs):
            yield

    monkeypatch.setattr(platform_compat, "file_lock", observe_lock)
    mutate = snap._do_replace_mutations
    futures = []

    def open_and_write():
        store.init()
        store.set_semantic("project.database", "acknowledged", 1.0, "user_explicit")
        assert store.get_semantic("project.database") is not None

    try:
        with ThreadPoolExecutor(max_workers=1) as worker:

            def interleave(*args, **kwargs):
                future = worker.submit(open_and_write)
                futures.append(future)
                if without_namespace:
                    future.result(timeout=10)
                else:
                    assert started.wait(timeout=10)
                    assert not database.parent.exists()
                mutate(*args, **kwargs)
                if fail_replace:
                    raise OSError("injected replace failure")

            monkeypatch.setattr(snap, "_do_replace_mutations", interleave)
            if fail_replace:
                with pytest.raises(OSError, match="injected replace failure"):
                    snap._do_replace(src, destination, ["memory"], allow_unpinned=True)
            else:
                snap._do_replace(src, destination, ["memory"], allow_unpinned=True)
            futures[0].result(timeout=10)
        assert database.exists() is not without_namespace
    finally:
        store.close()
    if not without_namespace:
        with closing(VectorMemoryStore(db_path=database)) as reopened:
            reopened.init()
            assert (
                json.loads(reopened.get_semantic("project.database")["value_json"])
                == "acknowledged"
            )


@pytest.mark.asyncio
async def test_ensure_store_refuses_a_declared_store_without_a_directory(tmp_path, monkeypatch):
    from kiro_crew import context, memory_stores

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"memory_stores": {"cold": {}}}), encoding="utf-8"
    )
    with pytest.raises(memory_stores.UnknownMemoryStore, match="missing or unreadable"):
        await context.ContextBuilder.ensure_store("cold")
    assert not (tmp_path / ROOT / "cold").exists()
