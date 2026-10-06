"""Everything Kiro Crew creates in the data home is owner-only: files 0600, directories 0700.

Every test runs under a ``022`` umask, the usual default, which is the condition
the defect needed: a file created without an explicit mode lands at ``0644`` and
a directory at ``0755``. Three layers are pinned separately because each one has
to hold on its own (``kiro_crew.owner_only_files`` explains why):

* creation -- the helpers and the stores that use them create with the mode,
  including SQLite's ``-wal``/``-shm``/``-journal`` sidecars;
* the root -- the data home itself is ``0700``;
* the startup sweep -- an existing tree is tightened without following a link,
  touching a hard-linked file, or failing on ``EPERM``/``EROFS``.
"""

from __future__ import annotations

import ast
import errno
import json
import logging
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from kiro_crew import owner_only_files as oof
from kiro_crew import platform_compat
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir, ensure_data_home

pytestmark = pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX mode bits")

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _umask_022() -> Iterator[None]:
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture
def home() -> Path:
    """The test's (isolated) data home, created the way production creates it."""
    return config_dir()


def _mode(path: Path | str) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _readable_by_others(root: Path) -> list[str]:
    """Every file or directory under *root* with a group/other bit, links excluded."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath, name)
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                continue
            if stat.S_IMODE(st.st_mode) & 0o077:
                found.append(f"{oct(stat.S_IMODE(st.st_mode))} {path.relative_to(root)}")
    return found


# ── the root ─────────────────────────────────────────────────────────────────


def test_a_new_data_home_is_created_0700(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fresh = tmp_path / "parent" / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(fresh))
    assert config_dir() == fresh.resolve()
    assert _mode(fresh) == 0o700


def test_ensure_data_home_tightens_an_existing_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = tmp_path / "home"
    existing.mkdir(mode=0o755)
    monkeypatch.setenv("KIROCREW_HOME", str(existing))
    ensure_data_home()
    assert _mode(existing) == 0o700


# ── creation helpers ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("open_mode", ["a", "w", "x", "ab", "wb"])
def test_the_opener_creates_0600(tmp_path: Path, open_mode: str) -> None:
    path = tmp_path / f"f-{open_mode}"
    with open(path, open_mode, opener=oof.owner_only_opener):
        pass
    assert _mode(path) == 0o600


def test_the_opener_leaves_an_existing_file_s_mode_alone(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_text("x", encoding="utf-8")
    path.chmod(0o640)
    with open(path, "a", encoding="utf-8", opener=oof.owner_only_opener) as handle:
        handle.write("y")
    assert _mode(path) == 0o640  # the sweep's job, not the opener's


def test_mkdirs_owner_only_creates_every_missing_level_0700(tmp_path: Path) -> None:
    tmp_path.chmod(0o755)
    oof.mkdirs_owner_only(tmp_path / "a" / "b" / "c")
    for level in ("a", "a/b", "a/b/c"):
        assert _mode(tmp_path / level) == 0o700, level
    assert _mode(tmp_path) == 0o755  # an existing directory is not changed
    oof.mkdirs_owner_only(tmp_path / "a" / "b")  # idempotent


def test_mkdirs_owner_only_refuses_a_file_at_the_name(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("x", encoding="utf-8")
    with pytest.raises(FileExistsError):
        oof.mkdirs_owner_only(tmp_path / "f")
    with pytest.raises(OSError):
        oof.mkdirs_owner_only(tmp_path / "f" / "below")


def test_ensure_directory_is_owner_only_in_the_home_and_plain_outside(
    home: Path, tmp_path: Path
) -> None:
    oof.ensure_directory(home / "artifacts" / "x")
    assert _mode(home / "artifacts") == 0o700
    assert _mode(home / "artifacts" / "x") == 0o700
    outside = tmp_path / "project" / "work"
    oof.ensure_directory(outside)
    assert _mode(outside) == 0o755  # a user's own directory keeps the umask default


def test_is_owner_only_target_is_lexical_containment(home: Path, tmp_path: Path) -> None:
    assert oof.is_owner_only_target(home)
    assert oof.is_owner_only_target(home / "sessions" / "x.jsonl")
    assert not oof.is_owner_only_target(tmp_path / "elsewhere")
    assert not oof.is_owner_only_target(Path(str(home) + "-sibling") / "x")


# ── atomic_write's default inside the data home ──────────────────────────────


def test_atomic_write_defaults_to_owner_only_inside_the_home(home: Path) -> None:
    target = home / "nested" / "deeper" / "state.json"
    atomic_write(target, "{}")
    assert _mode(target) == 0o600
    assert _mode(home / "nested") == 0o700
    assert _mode(home / "nested" / "deeper") == 0o700


def test_atomic_write_outside_the_home_keeps_the_umask_default(tmp_path: Path) -> None:
    target = tmp_path / "repo" / "file.txt"
    atomic_write(target, "x")
    assert _mode(target) == 0o644
    assert _mode(tmp_path / "repo") == 0o755


def test_an_explicit_mode_still_wins_inside_the_home(home: Path) -> None:
    target = home / "bin" / "tool.sh"
    atomic_write(target, "#!/bin/sh\n", mode=0o700)
    assert _mode(target) == 0o700


# ── SQLite ───────────────────────────────────────────────────────────────────


def _write_some(db: Path, journal_mode: str) -> None:
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        conn.execute(f"PRAGMA journal_mode={journal_mode}")
        conn.execute("CREATE TABLE IF NOT EXISTS t (x)")
        conn.execute("BEGIN")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.execute("COMMIT")
    finally:
        conn.close()


def test_sqlite_gives_every_sidecar_the_prepared_database_s_mode(tmp_path: Path) -> None:
    """Checked against the driver, not assumed: WAL, SHM and rollback journal."""
    wal_db = tmp_path / "wal.db"
    oof.prepare_owner_only_sqlite(wal_db)
    conn = sqlite3.connect(str(wal_db), isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (1)")
        for name in ("wal.db", "wal.db-wal", "wal.db-shm"):
            assert _mode(tmp_path / name) == 0o600, name  # while the sidecars exist
    finally:
        conn.close()

    journal_db = tmp_path / "journal.db"
    oof.prepare_owner_only_sqlite(journal_db)
    _write_some(journal_db, "PERSIST")  # PERSIST keeps the journal on disk to inspect
    assert _mode(journal_db) == 0o600
    assert _mode(tmp_path / "journal.db-journal") == 0o600


def test_without_preparation_sqlite_uses_the_umask(tmp_path: Path) -> None:
    """The defect itself, so the test above cannot pass for an unrelated reason."""
    db = tmp_path / "plain.db"
    _write_some(db, "WAL")
    assert _mode(db) == 0o644


def test_an_existing_database_and_its_sidecars_are_tightened(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x)")
    try:
        for name in ("old.db", "old.db-wal", "old.db-shm"):
            assert _mode(tmp_path / name) == 0o644, name
        oof.prepare_owner_only_sqlite(db)
        for name in ("old.db", "old.db-wal", "old.db-shm"):
            assert _mode(tmp_path / name) == 0o600, name
    finally:
        conn.close()


def test_a_symlinked_database_and_its_target_are_left_alone(tmp_path: Path) -> None:
    target = tmp_path / "target.db"
    target.write_bytes(b"")
    target.chmod(0o644)
    link = tmp_path / "link.db"
    link.symlink_to(target)
    oof.prepare_owner_only_sqlite(link)
    assert link.is_symlink()
    assert _mode(target) == 0o644

    dangling = tmp_path / "dangling.db"
    dangling.symlink_to(tmp_path / "nowhere.db")
    oof.prepare_owner_only_sqlite(dangling)
    assert not (tmp_path / "nowhere.db").exists()  # never created through a link


def test_a_hard_linked_database_is_not_chmodded(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere.db"
    elsewhere.write_bytes(b"")
    elsewhere.chmod(0o644)
    os.link(elsewhere, tmp_path / "store.db")
    oof.prepare_owner_only_sqlite(tmp_path / "store.db")
    assert _mode(elsewhere) == 0o644


def test_memory_and_uri_names_are_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    oof.prepare_owner_only_sqlite(":memory:")
    oof.prepare_owner_only_sqlite("file:x.db?mode=ro")
    assert list(tmp_path.iterdir()) == []


# ── the stores ───────────────────────────────────────────────────────────────


def test_the_knowledge_library_is_owner_only(home: Path) -> None:
    from kiro_crew.knowledge.store import KnowledgeStore

    directory = home / "workspace" / "knowledge"
    oof.mkdirs_owner_only(directory)
    store = KnowledgeStore(str(directory / "knowledge.db"))
    try:
        store.db.execute("SELECT 1").fetchone()
        assert _readable_by_others(home) == []
        assert (directory / "knowledge.db").is_file()
    finally:
        store._close_all_for_tests()


def test_a_transcript_and_its_directory_are_owner_only(home: Path) -> None:
    from kiro_crew.history import ConversationLog, _archive_lines

    store = ConversationLog(home / "sessions")
    store.append("dashboard:chat-1", "user", "something private")
    store.append("dashboard:chat-1", "assistant", "a reply")
    _archive_lines("dashboard:chat-1", ['{"role": "user"}\n'], "test", base=home / "sessions")
    files = [p for p in (home / "sessions").rglob("*") if p.is_file()]
    assert files, "nothing was written"
    assert _readable_by_others(home) == []


def test_the_session_search_index_is_owner_only(home: Path) -> None:
    from kiro_crew.history_index import SessionSearchIndex

    index = SessionSearchIndex(home / "sessions" / ".index" / "session_index.db")
    try:
        index._ensure_open()
        assert (home / "sessions" / ".index" / "session_index.db").is_file()
        assert _readable_by_others(home) == []
    finally:
        index.close()


def test_the_research_campaign_store_is_owner_only(home: Path) -> None:
    from kiro_crew.apps.builtins.auto_research.campaign import storage

    conn = storage._get_db()
    try:
        assert storage.db_path().is_file()
        assert _readable_by_others(home) == []
    finally:
        conn.close()


def test_notification_history_is_owner_only(home: Path) -> None:
    from kiro_crew.dashboard.state import _notifications_path, _persist_notification

    assert _persist_notification({"id": "n1", "title": "t", "body": "b"})
    assert _mode(_notifications_path()) == 0o600
    _notifications_path().unlink()
    from kiro_crew.dashboard.state import _rewrite_notifications

    _rewrite_notifications([{"id": "n2", "title": "t", "body": "b"}])
    assert _mode(_notifications_path()) == 0o600


def test_a_config_write_narrows_an_old_world_readable_config(home: Path) -> None:
    from kiro_crew.config.loader import write_config_atomically

    config = home / "config.json"
    config.write_text("{}", encoding="utf-8")
    config.chmod(0o644)
    write_config_atomically(config, {"agent": {}})
    assert _mode(config) == 0o600


def test_a_config_symlinked_out_of_the_home_keeps_its_target_s_mode(
    home: Path, tmp_path: Path
) -> None:
    from kiro_crew.config.loader import write_config_atomically

    target = tmp_path / "dotfiles" / "config.json"
    target.parent.mkdir()
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o644)
    (home / "config.json").symlink_to(target)
    write_config_atomically(home / "config.json", {"agent": {}})
    assert (home / "config.json").is_symlink()
    assert _mode(target) == 0o644  # the user's own file, outside the home
    assert json.loads(target.read_text(encoding="utf-8")) == {"agent": {}}


def test_gateway_log_is_created_and_rolled_over_owner_only(home: Path) -> None:
    from kiro_crew.cli import _OwnerOnlyRotatingFileHandler

    log = home / "gateway.log"
    handler = _OwnerOnlyRotatingFileHandler(log, maxBytes=64, backupCount=2, encoding="utf-8")
    try:
        record = logging.LogRecord("kiro_crew.t", logging.WARNING, __file__, 1, "x" * 80, (), None)
        for _ in range(4):
            handler.emit(record)
    finally:
        handler.close()
    assert (home / "gateway.log.1").is_file(), "no rollover happened"
    assert _readable_by_others(home) == []


# ── the startup sweep ────────────────────────────────────────────────────────


def _sweep(root: Path, **kw: object) -> oof.TightenReport:
    kw.setdefault("max_seconds", 30.0)
    kw.setdefault("max_entries", 10_000)
    return oof.tighten_tree_to_owner(root, **kw)  # type: ignore[arg-type]


def test_the_sweep_tightens_a_planted_file_and_directory(home: Path) -> None:
    sessions = home / "sessions"
    sessions.mkdir(mode=0o755)
    sessions.chmod(0o755)
    transcript = sessions / "old.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    transcript.chmod(0o644)
    script = home / "crons" / "job.py"
    script.parent.mkdir(mode=0o755)
    script.parent.chmod(0o775)
    script.write_text("pass\n", encoding="utf-8")
    script.chmod(0o755)

    report = _sweep(home)

    assert report.complete, report
    assert _mode(sessions) == 0o700
    assert _mode(transcript) == 0o600
    assert _mode(script.parent) == 0o700
    assert _mode(script) == 0o700  # owner bits, the execute bit included, are kept
    assert report.tightened == 4
    assert _readable_by_others(home) == []
    assert _sweep(home).tightened == 0  # a tight tree costs nothing to re-check


def test_the_sweep_leaves_a_symlink_and_its_target_alone(home: Path, tmp_path: Path) -> None:
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("x", encoding="utf-8")
    outside_file.chmod(0o644)
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    outside_dir.chmod(0o755)
    (outside_dir / "inner.txt").write_text("x", encoding="utf-8")
    (outside_dir / "inner.txt").chmod(0o644)
    (home / "file-link").symlink_to(outside_file)
    (home / "dir-link").symlink_to(outside_dir)

    report = _sweep(home)

    assert report.complete
    assert report.skipped_links == 2
    assert (home / "file-link").is_symlink() and (home / "dir-link").is_symlink()
    assert _mode(outside_file) == 0o644
    assert _mode(outside_dir) == 0o755
    assert _mode(outside_dir / "inner.txt") == 0o644


def test_the_sweep_skips_a_hard_linked_file(home: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "user-file.txt"
    elsewhere.write_text("x", encoding="utf-8")
    elsewhere.chmod(0o644)
    os.link(elsewhere, home / "hard-link.txt")

    report = _sweep(home)

    assert report.skipped_hard_linked == 1
    assert _mode(elsewhere) == 0o644


def test_the_sweep_never_opens_a_fifo(home: Path) -> None:
    fifo = home / "pipe"
    os.mkfifo(fifo, 0o644)
    report = _sweep(home)  # would hang on a blocking open
    assert report.complete
    assert _mode(fifo) == 0o644


def test_the_sweep_survives_eperm(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    planted = home / "notes.md"
    planted.write_text("x", encoding="utf-8")
    planted.chmod(0o644)

    def _refuse(fd: int, mode: int) -> None:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(platform_compat, "fchmod_safe", _refuse)
    report = _sweep(home)  # must not raise

    assert report.errors >= 1
    assert report.tightened == 0
    assert _mode(planted) == 0o644


def test_the_sweep_stops_on_a_read_only_filesystem(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("a", "b"):
        (home / name).write_text("x", encoding="utf-8")
        (home / name).chmod(0o644)

    calls = []

    def _erofs(fd: int, mode: int) -> None:
        calls.append(fd)
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(platform_compat, "fchmod_safe", _erofs)
    report = _sweep(home)

    assert not report.complete
    assert report.stopped == "read-only filesystem"
    assert len(calls) == 1  # no point trying the rest of the volume


def test_the_sweep_is_bounded_by_entries_and_time(home: Path) -> None:
    for index in range(5):
        (home / f"f{index}").write_text("x", encoding="utf-8")

    by_entries = _sweep(home, max_entries=2)
    assert not by_entries.complete
    assert "entry budget" in by_entries.stopped

    ticks = iter(range(1000))
    by_time = _sweep(home, max_seconds=2.5, clock=lambda: float(next(ticks)))
    assert not by_time.complete
    assert "time budget" in by_time.stopped


def test_the_sweep_reports_an_unopenable_root_instead_of_raising(tmp_path: Path) -> None:
    report = _sweep(tmp_path / "missing")
    assert not report.complete
    assert report.errors == 1


def test_the_startup_sweep_leaves_the_installed_skill_trees_alone(home: Path) -> None:
    """``skills/`` is fingerprinted by mode (``owner_only_files.STARTUP_SWEEP_SKIPPED``)."""
    skill = home / "skills" / "some-builtin"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("x", encoding="utf-8")
    (skill / "SKILL.md").chmod(0o644)
    other = home / "workspace" / "note.md"
    other.parent.mkdir()
    other.write_text("x", encoding="utf-8")
    other.chmod(0o644)

    report = oof.tighten_data_home(home)

    assert report.complete
    assert _mode(skill / "SKILL.md") == 0o644
    assert _mode(other) == 0o600


def test_off_posix_the_mode_helpers_change_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows keeps its previous behaviour: mode bits are not its access control."""
    planted = tmp_path / "f.txt"
    planted.write_text("x", encoding="utf-8")
    planted.chmod(0o644)
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)

    oof.prepare_owner_only_sqlite(tmp_path / "new.db")
    assert not (tmp_path / "new.db").exists()
    assert oof.tighten_file_to_owner(planted) is False
    report = oof.tighten_tree_to_owner(tmp_path, max_seconds=30.0, max_entries=100)
    assert report.stopped == "not a POSIX platform"
    assert _mode(planted) == 0o644


def test_the_startup_wrapper_never_raises(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> oof.TightenReport:
        raise RuntimeError("boom")

    monkeypatch.setattr(oof, "tighten_tree_to_owner", _boom)
    assert oof.tighten_data_home(home).stopped == "failed"


def test_the_gateway_prologue_runs_the_sweep_after_the_root_is_tightened() -> None:
    """``kirocrew gateway`` tightens the home, then sweeps it, before any service starts.

    Read from the source because ``main()`` boots a whole gateway: the order is
    the property (the sweep needs the home to exist and must finish before a
    store opens), and it is visible in the statements of ``main`` itself.
    """
    tree = ast.parse((REPO_ROOT / "src" / "kiro_crew" / "cli.py").read_text(encoding="utf-8"))
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")

    def _first_call(name: str) -> ast.Call:
        for node in ast.walk(main):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == name:
                return node
        raise AssertionError(f"main() never calls {name}")

    ensure = _first_call("ensure_data_home")
    sweep = _first_call("tighten_data_home")
    boot = _first_call("boot_platform")
    assert ensure.lineno < sweep.lineno < boot.lineno

    guard = next(
        n
        for n in ast.walk(main)
        if isinstance(n, ast.If) and any(c is sweep for b in n.body for c in ast.walk(b))
    )
    assert ast.unparse(guard.test) == "args.command == 'gateway'"
