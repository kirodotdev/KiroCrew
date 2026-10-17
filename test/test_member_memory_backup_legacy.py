"""Snapshots and pending restores written by the pre-identity member layout.

0.7.0-insider.1 to .5 named a member store's owner by alias (``owner_member``)
in the snapshot manifest and the pending-restore journal, bundled
``lessons.jsonl`` and ``memory/history/*.md``, and left ``memory.db`` without a
``member_database`` row. After the start-of-process store upgrade the store and
member carry an identity, and those files must still restore -- or, for a staged
restore, still activate or cancel -- instead of leaving the store failed.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
from member_memory_helpers import forget_declared_stores
from test_member_memory_upgrade import LESSON_KEY, _write_legacy_database

from kiro_crew import member_memory_backup as member_backup
from kiro_crew import memory_backup, memory_schema, memory_stores
from kiro_crew.config import loader
from kiro_crew.vector_memory import (
    create_member_database,
    open_member_database,
    read_member_database_identity,
)

pytestmark = pytest.mark.xdist_group("member_memory_backup")

STORE = "member-reviewer"
ALIAS = "reviewer"
MEMBER_ID = "reviewer-7f3a"


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A member store as the store upgrade leaves it: alias and identity both recorded."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    config = {
        "memory_stores": {
            "default": {},
            STORE: {"memory_version": 2, "owner_member": ALIAS, "owner_member_id": MEMBER_ID},
        },
        "agents": {
            "default": {},
            ALIAS: {"memory_store": STORE, "member_id": MEMBER_ID},
            "peer": {"memory_store": "default"},
        },
    }
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    forget_declared_stores(monkeypatch)
    path = tmp_path / "memory_stores" / STORE / "memory.db"
    path.parent.mkdir(parents=True)
    create_member_database(path, member_id=MEMBER_ID, store_id=STORE)
    try:
        yield path
    finally:
        loader._invalidate_config_cache()


def _legacy_files(scratch: Path, *, store: str = STORE, owner: str = ALIAS) -> dict[str, bytes]:
    scratch.mkdir(parents=True, exist_ok=True)
    database = scratch / f"legacy-{uuid4().hex}.db"
    _write_legacy_database(database, store=store, owner=owner)
    return {
        "memory.db": database.read_bytes(),
        "memory/preferences.md": b"Prefer short answers",
        "lessons.jsonl": b'{"rule": "old lesson"}\n',
        "memory/history/2026-09-17.md": b"Old history",
    }


def _legacy_manifest(files: dict[str, bytes], *, owner: str = ALIAS) -> dict:
    return {
        "format": member_backup.BUNDLE_FORMAT,
        "version": member_backup.BUNDLE_VERSION,
        "store": STORE,
        "owner_member": owner,
        "created_at": "20260917T000000Z",
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
    }


def _legacy_backup(path: Path, *, owner: str = ALIAS, db_owner: str = ALIAS) -> Path:
    files = _legacy_files(path.parent.parent / "scratch", owner=db_owner)
    out = member_backup.backup_directory(path)
    out.mkdir(parents=True, exist_ok=True)
    backup = out / "memory.20260917T000000Z.zip"
    with zipfile.ZipFile(backup, "w") as bundle:
        for name, data in files.items():
            bundle.writestr(name, data)
        bundle.writestr(member_backup.MANIFEST, json.dumps(_legacy_manifest(files, owner=owner)))
    return backup


def _legacy_pending(path: Path) -> dict:
    """What the old build's ``stage_restore`` left: a stage tree and an alias journal."""
    files = _legacy_files(path.parent.parent / "scratch")
    out = member_backup.backup_directory(path)
    stage = out / ("restore-" + uuid4().hex)
    for name, data in files.items():
        (stage / name).parent.mkdir(parents=True, exist_ok=True)
        (stage / name).write_bytes(data)
    (stage / member_backup.MANIFEST).write_text(json.dumps(_legacy_manifest(files)))
    (stage / memory_stores.LEGACY_MEMBER_MANIFEST).write_text(
        json.dumps({"owner_member": ALIAS, "memory_version": 2})
    )
    journal = {
        "store": STORE,
        "owner_member": ALIAS,
        "stage": stage.name,
        "aside": "superseded-" + uuid4().hex,
        "prior_existed": True,
        "backup_name": "memory.20260917T000000Z.zip",
        "staged_at": "2026-09-17T00:00:00+00:00",
    }
    (out / member_backup.PENDING).write_text(json.dumps(journal))
    return journal


def _assert_restored_legacy(path: Path) -> None:
    assert read_member_database_identity(path) == (MEMBER_ID, STORE)
    tier = open_member_database(path, member_id=MEMBER_ID, store_id=STORE)
    try:
        rows = tier._db.execute(
            "SELECT key FROM memory_items WHERE id=?", (memory_schema.semantic_item_id(LESSON_KEY),)
        ).fetchall()
    finally:
        tier.close()
    assert [tuple(row) for row in rows] == [(LESSON_KEY,)]
    assert (path.parent / "memory" / "preferences.md").read_text() == "Prefer short answers"
    assert (path.parent / "memory" / "projects.md").exists()


def test_a_pre_identity_snapshot_restores_and_gains_its_member_identity(live):
    backup = _legacy_backup(live)
    memory_backup.restore_from_backup(backup, STORE)
    assert member_backup.pending_restore_status(live)["pending"]
    restored = memory_backup.apply_pending_member_restores()
    assert set(restored) == {STORE}
    _assert_restored_legacy(live)
    assert not (member_backup.backup_directory(live) / member_backup.PENDING).exists()


def test_a_restore_staged_by_the_old_build_activates(live):
    _legacy_pending(live)
    status = member_backup.pending_restore_status(live)
    assert status["pending"] and "restore_error" not in status
    assert set(memory_backup.apply_pending_member_restores()) == {STORE}
    _assert_restored_legacy(live)


def test_a_restore_staged_by_the_old_build_can_be_cancelled(live):
    journal = _legacy_pending(live)
    out = member_backup.backup_directory(live)
    assert member_backup.cancel_pending_restore(live)
    assert not (out / member_backup.PENDING).exists()
    assert not (out / journal["stage"]).exists()
    assert read_member_database_identity(live) == (MEMBER_ID, STORE)


def test_a_completion_that_fails_at_activation_leaves_live_memory_in_place(live, monkeypatch):
    """A restore the old build staged is completed in its stage, before the swap.

    So a completion that cannot succeed refuses activation with the live tree
    untouched and the restore still cancellable, as the same journal was refused
    before any rename by a build that could not read it at all.
    """
    journal = _legacy_pending(live)
    out = member_backup.backup_directory(live)
    before = live.read_bytes()

    def fails(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(memory_stores, "complete_legacy_member_directory", fails)
    for _ in range(2):
        with pytest.raises(ValueError, match="could not be upgraded"):
            member_backup.apply_pending_restore(live)
    assert live.read_bytes() == before
    assert read_member_database_identity(live) == (MEMBER_ID, STORE)
    assert not (out / journal["aside"]).exists()
    assert (out / journal["stage"]).is_dir()
    assert (out / member_backup.PENDING).exists()
    assert member_backup.cancel_pending_restore(live)
    assert not (out / journal["stage"]).exists()
    assert live.read_bytes() == before


def test_an_activation_interrupted_after_the_stage_completion_resumes(live, monkeypatch):
    _legacy_pending(live)
    real = member_backup.replace_with_retry

    def interrupted(*args, **kwargs):
        raise OSError("interrupted")

    monkeypatch.setattr(member_backup, "replace_with_retry", interrupted)
    with pytest.raises(OSError):
        member_backup.apply_pending_restore(live)
    assert read_member_database_identity(live) == (MEMBER_ID, STORE)
    monkeypatch.setattr(member_backup, "replace_with_retry", real)
    assert member_backup.apply_pending_restore(live)
    _assert_restored_legacy(live)


def _completes_the_database_then_fails(directory, *, member_id, store):
    """The database is finished; the documents step after it is not."""
    memory_stores._complete_legacy_member_database(
        directory / memory_stores.MEMORY_DB_FILE, member_id=member_id, store=store
    )
    raise OSError("disk full")


def _power_lost_before_writing(name, real):
    def write(path, *args, **kwargs):
        if Path(path).name == name:
            raise OSError("power loss")
        return real(path, *args, **kwargs)

    return write


@pytest.mark.parametrize("stop", ["completion", member_backup.MANIFEST, member_backup.PENDING])
def test_an_activation_stopped_after_the_database_completed_retries(live, monkeypatch, stop):
    """The old build's stage is never written, so a retry reads it against its own checksums.

    Stopping between the completed database and the current-layout journal
    must leave every staged file matching the manifest that names it; a
    mismatch would refuse the restore as changed content on every later start.
    """
    journal = _legacy_pending(live)
    out = member_backup.backup_directory(live)
    legacy_stage = out / journal["stage"]
    staged = (legacy_stage / "memory.db").read_bytes()
    before = live.read_bytes()
    # A scoped patch: undo() would also drop the fixture's KIROCREW_HOME.
    with monkeypatch.context() as patch:
        if stop == "completion":
            patch.setattr(
                memory_stores,
                "complete_legacy_member_directory",
                _completes_the_database_then_fails,
            )
            expected: type[Exception] = ValueError
        else:
            patch.setattr(
                member_backup,
                "atomic_write",
                _power_lost_before_writing(stop, member_backup.atomic_write),
            )
            expected = OSError
        with pytest.raises(expected):
            member_backup.apply_pending_restore(live)
    assert (legacy_stage / "memory.db").read_bytes() == staged
    assert json.loads((out / member_backup.PENDING).read_text()) == journal
    assert live.read_bytes() == before
    if stop != member_backup.PENDING:
        assert sorted(path.name for path in out.glob("restore-*")) == [legacy_stage.name]
    assert member_backup.apply_pending_restore(live) == journal["aside"]
    _assert_restored_legacy(live)
    assert not legacy_stage.exists()


def test_an_old_build_stage_is_switched_to_a_current_layout_stage_and_journal(live, monkeypatch):
    """Once switched over, a restart reads only the current layout."""
    journal = _legacy_pending(live)
    out = member_backup.backup_directory(live)

    def interrupted(*args, **kwargs):
        raise OSError("interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(member_backup, "replace_with_retry", interrupted)
        with pytest.raises(OSError):
            member_backup.apply_pending_restore(live)
    current = json.loads((out / member_backup.PENDING).read_text())
    assert current["member_id"] == MEMBER_ID and "owner_member" not in current
    assert current["aside"] == journal["aside"] and current["stage"] != journal["stage"]
    assert not (out / journal["stage"]).exists()
    stage = out / current["stage"]
    manifest = json.loads((stage / member_backup.MANIFEST).read_text())
    assert manifest["member_id"] == MEMBER_ID and "owner_member" not in manifest
    assert (stage / memory_stores.LEGACY_MEMBER_MANIFEST).is_file()
    assert member_backup.cancel_pending_restore(live)
    assert not stage.exists()
    assert read_member_database_identity(live) == (MEMBER_ID, STORE)


def test_a_tree_the_old_build_activated_before_an_interruption_is_completed(live):
    """Both renames done by the old build, journal left: the live tree is completed."""
    journal = _legacy_pending(live)
    out = member_backup.backup_directory(live)
    live.parent.rename(out / journal["aside"])
    (out / journal["stage"]).rename(live.parent)
    assert member_backup.apply_pending_restore(live) == journal["aside"]
    assert not (out / member_backup.PENDING).exists()
    _assert_restored_legacy(live)


@pytest.mark.parametrize("field", ["manifest", "database"])
def test_a_pre_identity_snapshot_of_another_member_is_refused(live, field):
    backup = (
        _legacy_backup(live, owner="peer")
        if field == "manifest"
        else _legacy_backup(live, db_owner="peer")
    )
    with pytest.raises(ValueError, match="another member|different member"):
        memory_backup.restore_from_backup(backup, STORE)
    assert not (member_backup.backup_directory(live) / member_backup.PENDING).exists()
    assert read_member_database_identity(live) == (MEMBER_ID, STORE)


def test_legacy_only_files_are_refused_in_a_current_manifest(live):
    manifest = {
        "format": member_backup.BUNDLE_FORMAT,
        "version": member_backup.BUNDLE_VERSION,
        "store": STORE,
        "member_id": MEMBER_ID,
        "files": {"memory.db": "0" * 64, "lessons.jsonl": "0" * 64},
    }
    with pytest.raises(ValueError, match="unsafe path"):
        member_backup._manifest_valid(manifest, STORE, MEMBER_ID)


def test_cancel_refuses_cleanly_when_the_live_database_is_unreadable(live):
    journal = _legacy_pending(live)
    out = member_backup.backup_directory(live)
    live.write_bytes(b"not a database" * 512)
    with pytest.raises(ValueError, match="unreadable"):
        member_backup.cancel_pending_restore(live)
    assert (out / member_backup.PENDING).exists()
    assert (out / journal["stage"]).is_dir()


def test_the_legacy_completion_uses_the_fts5_capable_driver(live, monkeypatch):
    """Completion creates the member FTS5 table, which the stdlib driver may lack.

    The store upgrade and a restore activation can run under different
    interpreters, so both legacy helpers must open the database through the
    driver every other memory path uses, never the bare standard library.
    """
    from kiro_crew import _sqlite_compat

    real = _sqlite_compat.sqlite3
    opened: list[str] = []

    class Recording:
        def __getattr__(self, name):
            return getattr(real, name)

        def connect(self, *args, **kwargs):
            opened.append(str(args[0]))
            return real.connect(*args, **kwargs)

    scratch = live.parent.parent / "scratch"
    scratch.mkdir()
    database = scratch / "legacy.db"
    _write_legacy_database(database, store=STORE, owner=ALIAS)
    monkeypatch.setattr(_sqlite_compat, "sqlite3", Recording())
    assert memory_stores._legacy_member_database_identity(database, STORE, ALIAS) == ""
    memory_stores._complete_legacy_member_database(database, member_id=MEMBER_ID, store=STORE)
    assert len([path for path in opened if "legacy.db" in path]) == 2
    monkeypatch.setattr(_sqlite_compat, "sqlite3", real)
    assert read_member_database_identity(database) == (MEMBER_ID, STORE)


def test_a_pre_identity_snapshot_is_staged_in_the_current_layout(live):
    """Completed in its stage, so activation never finishes a database after the swap."""
    memory_backup.restore_from_backup(_legacy_backup(live), STORE)
    out = member_backup.backup_directory(live)
    journal = json.loads((out / member_backup.PENDING).read_text())
    assert journal["member_id"] == MEMBER_ID and "owner_member" not in journal
    stage = out / journal["stage"]
    manifest = json.loads((stage / member_backup.MANIFEST).read_text())
    assert manifest["member_id"] == MEMBER_ID and "owner_member" not in manifest
    assert set(manifest["files"]) <= member_backup._FILES
    assert read_member_database_identity(stage / "memory.db") == (MEMBER_ID, STORE)
    assert (stage / "lessons.jsonl").is_file()


def test_a_pre_identity_snapshot_that_cannot_be_completed_is_refused_before_staging(
    live, monkeypatch
):
    def fails(*_args, **_kwargs):
        raise memory_stores.UnknownMemoryStore("no such module: fts5")

    monkeypatch.setattr(memory_stores, "complete_legacy_member_directory", fails)
    with pytest.raises(ValueError, match="could not be upgraded"):
        memory_backup.restore_from_backup(_legacy_backup(live), STORE)
    out = member_backup.backup_directory(live)
    assert not (out / member_backup.PENDING).exists()
    assert not any(path.name.startswith("restore-") for path in out.iterdir())
    assert read_member_database_identity(live) == (MEMBER_ID, STORE)
