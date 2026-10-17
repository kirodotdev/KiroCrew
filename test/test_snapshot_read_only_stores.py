"""``kirocrew snapshot`` over a ``memory_stores/`` this process sees read-only.

A script cron's sandbox binds the stores tree read-only while the gateway keeps writing
through its own view. Reading it must still serialize against those writers, through
read-only descriptors on the lock files they take, and must copy a closed WAL database
that SQLite cannot open ``mode=ro`` without creating its index. Writers keep refusing.

The view answers the way a read-only mount does: every Python call that would write
under the tree fails with ``EROFS`` and ``statvfs`` reports ``ST_RDONLY``, while the
mode bits stay as they are. SQLite's own refusal is shown on a directory made unwritable
for real, since a mount cannot be made without privilege, and so is a store another
process holds open, whose -wal and -shm SQLite then reads without write access. The
choice of an immutable read is shown to follow the open's own result code by forcing
that code.
POSIX only: the locks are ``flock`` and the view is a mount flag.
"""

from __future__ import annotations

import errno
import logging
import os
import sqlite3
import stat
import subprocess
import sys
import tarfile
import types
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

import pytest
from test_snapshot import _setup_fake_kirocrew, unpinnable_argv

from kiro_crew import member_memory_backup, memory_stores, platform_compat
from kiro_crew import snapshot as snap
from kiro_crew.memory_stores import MEMBER_BACKUPS_DIR_NAME, MEMORY_DB_FILE, MEMORY_STORES_DIR_NAME

pytestmark = pytest.mark.skipif(
    not platform_compat.IS_POSIX, reason="flock locks and a read-only mount flag are POSIX-only"
)

STORE = "acme"
ROWS = 3
NAMESPACE_LOCK = Path(MEMBER_BACKUPS_DIR_NAME) / ".namespace.lock"
USE_LOCK = Path(MEMBER_BACKUPS_DIR_NAME) / STORE / ".store-use.lock"
# Only Linux confirms a read-only mount up front; elsewhere the reader learns it from
# the refused write and then takes the same read-only path.
MOUNT_FLAG_CONFIRMED = sys.platform == "linux"


@pytest.fixture(autouse=True)
def _no_gateway(_floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    _setup_fake_kirocrew(home)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return home


def _closed_wal_store(home: Path) -> Path:
    """A named store whose WAL database no connection holds open, so it has no ``-shm``."""
    db = home / MEMORY_STORES_DIR_NAME / STORE / MEMORY_DB_FILE
    db.parent.mkdir(parents=True)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE semantic_memory (key TEXT PRIMARY KEY)")
        conn.executemany(
            "INSERT INTO semantic_memory VALUES (?)", [(f"k{i}",) for i in range(ROWS)]
        )
        conn.commit()
    assert sorted(p.name for p in db.parent.iterdir()) == [MEMORY_DB_FILE]
    return db


def _create_lock_files(stores_root: Path) -> None:
    """What the gateway's writers leave behind: both lock files, created by a writable view."""
    with member_memory_backup.hold_stores_for_read(stores_root):
        pass
    assert (stores_root / NAMESPACE_LOCK).is_file()
    assert (stores_root / USE_LOCK).is_file()


class ReadOnlyView:
    """Every path under the sealed roots behaves as a read-only mount for this process."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.roots: list[str] = []
        self.write_attempts: list[str] = []
        self._real_open = os.open
        self._real_mkdir = os.mkdir
        self._real_chmod = os.chmod
        self._real_statvfs = os.statvfs
        monkeypatch.setattr(os, "open", self._open)
        monkeypatch.setattr(os, "mkdir", self._mkdir)
        monkeypatch.setattr(os, "chmod", self._chmod)
        monkeypatch.setattr(os, "statvfs", self._statvfs)

    def seal(self, root: Path) -> None:
        self.roots.append(os.path.abspath(root))

    def unseal(self) -> None:
        self.roots.clear()

    def real_open(self, path, flags, mode=0o777):
        return self._real_open(path, flags, mode)

    def _under(self, path) -> bool:
        if isinstance(path, int):
            return False
        p = os.path.abspath(os.fsdecode(path))
        return any(p == r or p.startswith(r + os.sep) for r in self.roots)

    def _refuse(self, path):
        self.write_attempts.append(os.fsdecode(path))
        raise OSError(errno.EROFS, os.strerror(errno.EROFS), os.fsdecode(path))

    def _open(self, path, flags, mode=0o777, *, dir_fd=None):
        writes = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC
        if dir_fd is None and flags & writes and self._under(path):
            self._refuse(path)
        return self._real_open(path, flags, mode, dir_fd=dir_fd)

    def _mkdir(self, path, mode=0o777, *, dir_fd=None):
        if dir_fd is None and self._under(path):
            if os.path.lexists(path):
                raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), os.fsdecode(path))
            self._refuse(path)
        return self._real_mkdir(path, mode, dir_fd=dir_fd)

    def _chmod(self, path, mode, *, dir_fd=None, follow_symlinks=True):
        if dir_fd is None and self._under(path):
            self._refuse(path)
        return self._real_chmod(path, mode, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    def _statvfs(self, path):
        result = self._real_statvfs(path)
        if self._under(path):
            return types.SimpleNamespace(f_flag=result.f_flag | os.ST_RDONLY)
        return result


@pytest.fixture
def view(monkeypatch):
    sealed = ReadOnlyView(monkeypatch)
    yield sealed
    sealed.unseal()


def _snapshot(home: Path, out: Path) -> int:
    return snap.snapshot_main([str(out), "--components", "memory"] + unpinnable_argv())


def _archived_rows(out: Path, tmp_path: Path) -> int:
    (tarball,) = out.glob("kirocrew-snapshot-*.tar.gz")
    extract = tmp_path / "extract"
    with tarfile.open(tarball) as tar:
        tar.extractall(extract, filter="data")
    (root,) = extract.iterdir()
    db = root / MEMORY_STORES_DIR_NAME / STORE / MEMORY_DB_FILE
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute("SELECT count(*) FROM semantic_memory").fetchone()[0]


def test_snapshot_of_a_read_only_stores_view_carries_the_store(home, view, tmp_path):
    stores_root = home / MEMORY_STORES_DIR_NAME
    _closed_wal_store(home)
    _create_lock_files(stores_root)
    view.seal(stores_root)

    rc = _snapshot(home, tmp_path / "out")
    view.unseal()

    assert rc == 0
    assert _archived_rows(tmp_path / "out", tmp_path) == ROWS
    if MOUNT_FLAG_CONFIRMED:
        # The read path asked the mount first, so it made no write attempt to fail.
        assert view.write_attempts == []


def test_the_read_holds_both_locks_through_read_only_descriptors(home, view):
    stores_root = home / MEMORY_STORES_DIR_NAME
    _closed_wal_store(home)
    _create_lock_files(stores_root)
    view.seal(stores_root)
    namespace = view.real_open(stores_root / NAMESPACE_LOCK, os.O_RDONLY)
    use = view.real_open(stores_root / USE_LOCK, os.O_RDONLY)
    try:
        with member_memory_backup.hold_stores_for_read(stores_root):
            # A second descriptor on the same inode is a competing holder for flock, so
            # these probes answer exactly what a writer in the gateway would meet.
            assert not platform_compat.try_acquire_lock(namespace, exclusive=True)
            assert not platform_compat.try_acquire_lock(use, exclusive=True)
            assert platform_compat.try_acquire_lock(use, exclusive=False)
            platform_compat.release_lock(use)
        assert platform_compat.try_acquire_lock(namespace, exclusive=True)
        platform_compat.release_lock(namespace)
        assert platform_compat.try_acquire_lock(use, exclusive=True)
        platform_compat.release_lock(use)
    finally:
        os.close(namespace)
        os.close(use)


def test_a_read_only_descriptor_waits_for_a_writer_holding_the_lock(home, view):
    stores_root = home / MEMORY_STORES_DIR_NAME
    _closed_wal_store(home)
    _create_lock_files(stores_root)
    writer = view.real_open(stores_root / NAMESPACE_LOCK, os.O_RDWR)
    view.seal(stores_root)
    try:
        platform_compat.acquire_lock(writer, exclusive=True)
        reader = memory_stores.open_existing_lock_read_only(
            stores_root / NAMESPACE_LOCK, refusal=ValueError, defect="not a lock"
        )
        assert reader is not None
        try:
            assert not platform_compat.try_acquire_lock(reader, exclusive=True)
            platform_compat.release_lock(writer)
            assert platform_compat.try_acquire_lock(reader, exclusive=True)
            platform_compat.release_lock(reader)
        finally:
            os.close(reader)
    finally:
        os.close(writer)


def test_writers_still_refuse_a_read_only_view(home, view):
    stores_root = home / MEMORY_STORES_DIR_NAME
    _closed_wal_store(home)
    _create_lock_files(stores_root)
    view.seal(stores_root)

    with pytest.raises(OSError) as namespace:
        with memory_stores.memory_store_namespace_lock(stores_root):
            pass
    assert namespace.value.errno == errno.EROFS
    with pytest.raises(OSError) as replace:
        with member_memory_backup.hold_stores_for_replace(stores_root, [STORE]):
            pass
    assert replace.value.errno == errno.EROFS


def test_a_read_only_view_with_no_lock_file_reads_without_it(home, view, caplog):
    stores_root = home / MEMORY_STORES_DIR_NAME
    _closed_wal_store(home)
    assert not (stores_root / MEMBER_BACKUPS_DIR_NAME).exists()
    view.seal(stores_root)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.memory_stores"):
        with member_memory_backup.hold_stores_for_read(stores_root):
            pass
    assert "reading the stores without it" in caplog.text
    if MOUNT_FLAG_CONFIRMED:
        assert view.write_attempts == []


def test_a_namespace_operation_during_a_lockless_read_refuses_it(home, view):
    stores_root = home / MEMORY_STORES_DIR_NAME
    _closed_wal_store(home)
    backups = stores_root / MEMBER_BACKUPS_DIR_NAME
    backups.mkdir()
    view.seal(stores_root)

    with pytest.raises(memory_stores.UnknownMemoryStore, match="changed while they were read"):
        with member_memory_backup.hold_stores_for_read(stores_root):
            # The gateway's writable view creates the lock while this read runs.
            os.close(view.real_open(backups / ".namespace.lock", os.O_RDWR | os.O_CREAT, 0o600))


def test_a_lockless_read_shares_its_hold_with_a_nested_namespace_operation(home, view):
    stores_root = home / MEMORY_STORES_DIR_NAME
    _closed_wal_store(home)
    view.seal(stores_root)

    with member_memory_backup.hold_stores_for_read(stores_root):
        with memory_stores.memory_store_namespace_lock(stores_root):
            pass


def test_a_read_only_view_with_no_use_lock_file_reads_the_store_with_a_warning(home, view, caplog):
    stores_root = home / MEMORY_STORES_DIR_NAME
    _closed_wal_store(home)
    _create_lock_files(stores_root)
    (stores_root / USE_LOCK).unlink()
    view.seal(stores_root)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.member_memory_backup"):
        with member_memory_backup.hold_stores_for_read(stores_root):
            pass
    assert "reading the store without it" in caplog.text
    assert str(stores_root / USE_LOCK) in caplog.text


@pytest.mark.parametrize("lock", [NAMESPACE_LOCK, USE_LOCK], ids=["namespace", "use"])
def test_a_lock_file_another_user_owns_is_refused_on_a_read_only_view(
    home, view, monkeypatch, lock
):
    stores_root = home / MEMORY_STORES_DIR_NAME
    _closed_wal_store(home)
    _create_lock_files(stores_root)
    foreign = (stores_root / lock).stat().st_ino
    real_fstat = os.fstat

    def fstat(fd):
        info = real_fstat(fd)
        if info.st_ino != foreign:
            return info
        fields = list(info[:10])
        fields[stat.ST_UID] = info.st_uid + 1
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", fstat)
    view.seal(stores_root)

    with pytest.raises(memory_stores.UnknownMemoryStore, match="not a private regular file"):
        with member_memory_backup.hold_stores_for_read(stores_root):
            pass


@pytest.mark.parametrize("lock", [NAMESPACE_LOCK, USE_LOCK], ids=["namespace", "use"])
def test_a_lock_the_read_refuses_ends_the_snapshot_with_its_reason(
    home, view, tmp_path, capsys, lock
):
    stores_root = home / MEMORY_STORES_DIR_NAME
    _closed_wal_store(home)
    _create_lock_files(stores_root)
    # A second name on the inode makes the lock other than a private regular file.
    os.link(stores_root / lock, tmp_path / "second-name")
    view.seal(stores_root)

    rc = _snapshot(home, tmp_path / "out")

    assert rc == 1
    assert "not a private regular file" in capsys.readouterr().out
    assert not list((tmp_path / "out").glob("kirocrew-snapshot-*"))


# Holds the store open the way the gateway does: WAL, no automatic checkpoint, so the
# rows it commits live only in the -wal until it says it is done. Released, it commits
# again and checkpoints the whole log, and exits non-zero when a reader still holds the
# log back.
_LIVE_WRITER = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("PRAGMA wal_autocheckpoint=0")
rows = int(sys.argv[2])
conn.executemany("INSERT INTO semantic_memory VALUES (?)", [(f"w{i}",) for i in range(rows)])
conn.commit()
print("committed", flush=True)
sys.stdin.read()
conn.executemany("INSERT INTO semantic_memory VALUES (?)", [(f"a{i}",) for i in range(rows)])
conn.commit()
busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
conn.close()
sys.exit(busy)
"""


class _ChangingSource:
    """The facade's ``sqlite3``, whose backups first move the source's mtime on."""

    def __init__(self, source: Path) -> None:
        self._source = source

    def __getattr__(self, name):
        return getattr(sqlite3, name)

    def connect(self, *args, **kwargs):
        return _Connection(sqlite3.connect(*args, **kwargs), before=self._move_mtime)

    def _move_mtime(self) -> None:
        info = os.stat(self._source)
        os.utime(self._source, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))


class _Connection:
    """A connection whose ``backup`` runs *before* first and hands *after* the copy.

    With *during*, the copy runs a page at a time and *during* runs once after the first
    page, while the rest of the file is still to be read.
    """

    def __init__(self, conn: sqlite3.Connection, *, before=None, after=None, during=None) -> None:
        self._conn = conn
        self._before = before
        self._after = after
        self._during = during

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def backup(self, target, *args, **kwargs):
        target = getattr(target, "_conn", target)
        if self._before is not None:
            self._before()
        if self._during is not None:
            pending = [self._during]

            def step(_status, remaining, _total):
                if remaining and pending:
                    pending.pop()()

            kwargs.update(pages=1, progress=step)
        result = self._conn.backup(target, *args, **kwargs)
        if self._after is not None:
            self._after(target)
        return result


@contextmanager
def _unwritable(directory: Path) -> Iterator[None]:
    mode = directory.stat().st_mode
    directory.chmod(0o500)
    try:
        yield
    finally:
        directory.chmod(mode)


needs_unprivileged = pytest.mark.skipif(
    platform_compat.IS_POSIX and os.geteuid() == 0,
    reason="root writes into a 0o500 directory, so SQLite would not refuse",
)


def _copy_store_database(home: Path, db: Path, dst: Path) -> str:
    return snap._copy_database_consistently(
        db,
        dst,
        root=home / MEMORY_STORES_DIR_NAME,
        rel_parts=(STORE, MEMORY_DB_FILE),
        require_database=True,
    )


@needs_unprivileged
def test_a_closed_wal_database_in_an_unwritable_directory_is_copied(home, tmp_path):
    db = _closed_wal_store(home)
    staged = tmp_path / "staged.db"
    with _unwritable(db.parent):
        # The premise: an ordinary read-only open needs the -shm SQLite cannot create.
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as conn:
                conn.execute("PRAGMA schema_version").fetchone()
        assert _copy_store_database(home, db, staged) == snap.DB_COPIED
        assert sorted(p.name for p in db.parent.iterdir()) == [MEMORY_DB_FILE]
    with closing(sqlite3.connect(staged)) as conn:
        assert conn.execute("SELECT count(*) FROM semantic_memory").fetchone()[0] == ROWS


@needs_unprivileged
def test_a_closed_database_changed_during_its_immutable_copy_is_refused(home, tmp_path):
    db = _closed_wal_store(home)
    with _unwritable(db.parent), pytest.MonkeyPatch.context() as mp:
        mp.setattr(snap, "sqlite3", _ChangingSource(db))
        with pytest.raises(snap.DatabaseCopyFailed, match="changed while it was being copied"):
            _copy_store_database(home, db, tmp_path / "staged.db")


@contextmanager
def _live_writer(db: Path, rows: int) -> Iterator[None]:
    """Another process holding *db* open after committing *rows* that only its -wal holds.

    On exit the writer commits *rows* more and checkpoints; a writer the read left blocked
    fails here, by its exit code or by not finishing.
    """
    child = subprocess.Popen(
        [sys.executable, "-c", _LIVE_WRITER, str(db), str(rows)],
        cwd=db.parent,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert child.stdout.readline().strip() == "committed"
        yield
    finally:
        child.stdin.close()
        try:
            child.wait(timeout=60)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        child.stdout.close()
    assert child.returncode == 0


def _assert_live_store_intact(db: Path, rows: int) -> None:
    """The live store holds every row both writes committed and passes its integrity check."""
    with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM semantic_memory").fetchone()[0] == rows


@contextmanager
def _read_only_files(files: list[Path]) -> Iterator[None]:
    modes = [f.stat().st_mode for f in files]
    for f in files:
        f.chmod(0o444)
    try:
        yield
    finally:
        for f, mode in zip(files, modes):
            f.chmod(mode)


@needs_unprivileged
def test_a_store_a_live_writer_holds_open_is_copied_from_an_unwritable_directory(home, tmp_path):
    """The gateway-running case: the store's -wal and -shm exist and a writer holds them.

    SQLite's own I/O cannot be routed through a monkeypatched view, so the store
    directory and the writer's -wal and -shm are made unwritable for real. SQLite then
    opens the -shm read-only, which it does whatever errno refused the writable open, as
    on a read-only mount.
    """
    db = _closed_wal_store(home)
    live_rows = 5
    staged = tmp_path / "staged.db"
    with _live_writer(db, live_rows):
        with closing(sqlite3.connect(f"{db.as_uri()}?immutable=1", uri=True)) as base:
            # The writer's rows are not in the main file yet.
            assert base.execute("SELECT count(*) FROM semantic_memory").fetchone()[0] == ROWS
        with _unwritable(db.parent), _read_only_files([Path(f"{db}-wal"), Path(f"{db}-shm")]):
            assert _copy_store_database(home, db, staged) == snap.DB_COPIED
    with closing(sqlite3.connect(staged)) as conn:
        assert (
            conn.execute("SELECT count(*) FROM semantic_memory").fetchone()[0] == ROWS + live_rows
        )
    _assert_live_store_intact(db, ROWS + 2 * live_rows)


@needs_unprivileged
def test_a_store_a_live_writer_holds_open_is_snapshotted_from_a_read_only_view(
    home, view, tmp_path
):
    """The whole command over the same store, with the view sealed.

    The store directory keeps its mode bits here, as it does on a read-only mount:
    staging copies a directory's mode, so a directory made unwritable by its mode would
    make the staged copy unwritable rather than test the read.
    """
    stores_root = home / MEMORY_STORES_DIR_NAME
    db = _closed_wal_store(home)
    _create_lock_files(stores_root)
    live_rows = 5
    with _live_writer(db, live_rows):
        with _read_only_files([Path(f"{db}-wal"), Path(f"{db}-shm")]):
            view.seal(stores_root)
            rc = _snapshot(home, tmp_path / "out")
            view.unseal()

    assert rc == 0
    assert _archived_rows(tmp_path / "out", tmp_path) == ROWS + live_rows
    _assert_live_store_intact(db, ROWS + 2 * live_rows)


class _RefusedOrdinaryOpen:
    """The facade's ``sqlite3``, whose ordinary opens of *source* fail with *code*.

    The directory stays writable, so nothing but the forced result code can select the
    immutable read. Every URI the copy opens the source with is recorded, and
    *after_backup* is handed each copy the immutable read makes.
    """

    def __init__(
        self, source: Path, code: int, message: str, *, after_backup=None, during_backup=None
    ) -> None:
        self._source_uri = source.absolute().as_uri()
        self._code = code
        self._message = message
        self._after_backup = after_backup
        self._during_backup = during_backup
        self.source_uris: list[str] = []

    def __getattr__(self, name):
        return getattr(sqlite3, name)

    def connect(self, database, *args, **kwargs):
        if isinstance(database, str) and database.startswith(self._source_uri + "?"):
            self.source_uris.append(database)
            if "immutable=1" not in database:
                refused = sqlite3.OperationalError(self._message)
                refused.sqlite_errorcode = self._code
                refused.sqlite_errorname = "forced"
                raise refused
            return _Connection(
                sqlite3.connect(database, *args, **kwargs),
                after=self._after_backup,
                during=self._during_backup,
            )
        return sqlite3.connect(database, *args, **kwargs)


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (sqlite3.SQLITE_READONLY_DIRECTORY, "attempt to write a readonly database"),
        (sqlite3.SQLITE_CANTOPEN, "unable to open database file"),
        (sqlite3.SQLITE_PERM, "access permission denied"),
    ],
)
def test_the_open_s_own_refusal_selects_the_immutable_read(home, tmp_path, code, message):
    db = _closed_wal_store(home)
    assert os.access(db.parent, os.W_OK)
    fake = _RefusedOrdinaryOpen(db, code, message)
    staged = tmp_path / "staged.db"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(snap, "sqlite3", fake)
        assert _copy_store_database(home, db, staged) == snap.DB_COPIED
    assert ["immutable=1" in uri for uri in fake.source_uris] == [False, True, True]
    with closing(sqlite3.connect(staged)) as conn:
        assert conn.execute("SELECT count(*) FROM semantic_memory").fetchone()[0] == ROWS


def test_an_open_refused_for_another_reason_is_not_read_immutable(home, tmp_path):
    db = _closed_wal_store(home)
    fake = _RefusedOrdinaryOpen(db, sqlite3.SQLITE_BUSY, "database is locked")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(snap, "sqlite3", fake)
        with pytest.raises(snap.DatabaseCopyFailed, match="database is locked"):
            _copy_store_database(home, db, tmp_path / "staged.db")
    assert ["immutable=1" in uri for uri in fake.source_uris] == [False]


@pytest.mark.parametrize("sidecar", ["-wal", "-journal"])
def test_a_refused_open_beside_a_log_keeps_its_refusal(home, tmp_path, sidecar):
    db = _closed_wal_store(home)
    Path(f"{db}{sidecar}").write_bytes(b"")
    fake = _RefusedOrdinaryOpen(
        db, sqlite3.SQLITE_READONLY_DIRECTORY, "attempt to write a readonly database"
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(snap, "sqlite3", fake)
        with pytest.raises(snap.DatabaseCopyFailed, match="readonly"):
            _copy_store_database(home, db, tmp_path / "staged.db")
    assert ["immutable=1" in uri for uri in fake.source_uris] == [False]


def _refused_immutable_fake(
    db: Path, *, after_backup=None, during_backup=None
) -> _RefusedOrdinaryOpen:
    return _RefusedOrdinaryOpen(
        db,
        sqlite3.SQLITE_READONLY_DIRECTORY,
        "attempt to write a readonly database",
        after_backup=after_backup,
        during_backup=during_backup,
    )


def test_a_log_opened_beside_the_database_during_its_immutable_copy_is_refused(home, tmp_path):
    db = _closed_wal_store(home)
    # A writer that opens the database while the copy runs leaves its log beside it,
    # even where the main file's size and timestamps do not move.
    fake = _refused_immutable_fake(db, after_backup=lambda _copy: Path(f"{db}-wal").touch())
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(snap, "sqlite3", fake)
        with pytest.raises(snap.DatabaseCopyFailed, match="changed while it was being copied"):
            _copy_store_database(home, db, tmp_path / "staged.db")


def _tear(copy: sqlite3.Connection) -> None:
    """Overwrite the table's root page in *copy*, as a write landing mid-read leaves it."""
    _seq, _name, path = copy.execute("PRAGMA database_list").fetchone()
    page_size = copy.execute("PRAGMA page_size").fetchone()[0]
    with open(path, "r+b") as f:
        f.seek(page_size)
        f.write(b"\xff" * page_size)


def test_a_torn_immutable_copy_is_refused_even_for_an_incidental_database(home, tmp_path):
    db = _closed_wal_store(home)
    fake = _refused_immutable_fake(db, after_backup=_tear)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(snap, "sqlite3", fake)
        with pytest.raises(snap.DatabaseCopyFailed):
            snap._copy_database_consistently(
                db,
                tmp_path / "staged.db",
                root=home / MEMORY_STORES_DIR_NAME,
                rel_parts=(STORE, MEMORY_DB_FILE),
                require_database=False,
            )


def _run_writer(script: str, db: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script, str(db)],
        cwd=db.parent,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )


# A WAL writer that runs its whole cycle in one go: it commits, checkpoints the log into
# the main file and closes, which removes the log when no reader holds the file.
_CHECKPOINTING_WRITER = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("UPDATE semantic_memory SET key = 'z' || key")
conn.commit()
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
conn.close()
"""

# A rollback-journal writer that gives up at once when its commit cannot lock the file.
_ROLLBACK_WRITER = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1], timeout=0)
try:
    conn.execute("UPDATE semantic_memory SET key = 'z' || key")
    conn.commit()
except sqlite3.OperationalError as e:
    conn.rollback()
    conn.close()
    print(e, flush=True)
    sys.exit(3)
conn.close()
"""

# A writer holding the file's exclusive lock until it is told to let go.
_EXCLUSIVE_WRITER = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("BEGIN EXCLUSIVE")
print("locked", flush=True)
sys.stdin.read()
conn.execute("ROLLBACK")
conn.close()
"""


def _closed_rollback_store(home: Path) -> Path:
    db = _closed_wal_store(home)
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    return db


def _timestamps_never_move(mp: pytest.MonkeyPatch, db: Path) -> None:
    """``os.lstat`` of *db* reports fixed timestamps, as a coarse-timestamp filesystem can."""
    real = os.lstat
    target = str(db.absolute())

    def lstat(path, *args, **kwargs):
        info = real(path, *args, **kwargs)
        if isinstance(path, int) or os.path.abspath(os.fsdecode(path)) != target:
            return info
        fixed = {"st_atime_ns": 0, "st_mtime_ns": 0, "st_ctime_ns": 0}
        return os.stat_result(tuple(info[:7]) + (0, 0, 0), fixed)

    mp.setattr(os, "lstat", lstat)


def test_a_writer_that_checkpoints_and_closes_mid_copy_is_found_by_its_log(home, tmp_path):
    """The reader lock keeps the writer's close from removing its log, so the copy is refused.

    The file's size is unchanged and its timestamps never move, so the log left beside it
    is the only sign that a writer ran while the copy did.
    """
    db = _closed_wal_store(home)
    writers: list[subprocess.CompletedProcess] = []
    fake = _refused_immutable_fake(
        db, during_backup=lambda: writers.append(_run_writer(_CHECKPOINTING_WRITER, db))
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(snap, "sqlite3", fake)
        _timestamps_never_move(mp, db)
        with pytest.raises(snap.DatabaseCopyFailed, match="changed while it was being copied"):
            _copy_store_database(home, db, tmp_path / "staged.db")
    assert [w.returncode for w in writers] == [0], writers


def test_a_rollback_writer_cannot_commit_while_the_immutable_copy_runs(home, tmp_path):
    db = _closed_rollback_store(home)
    writers: list[subprocess.CompletedProcess] = []
    fake = _refused_immutable_fake(
        db, during_backup=lambda: writers.append(_run_writer(_ROLLBACK_WRITER, db))
    )
    staged = tmp_path / "staged.db"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(snap, "sqlite3", fake)
        assert _copy_store_database(home, db, staged) == snap.DB_COPIED
    assert [(w.returncode, w.stdout.strip()) for w in writers] == [(3, "database is locked")]
    with closing(sqlite3.connect(staged)) as conn:
        keys = [k for (k,) in conn.execute("SELECT key FROM semantic_memory ORDER BY key")]
    assert keys == [f"k{i}" for i in range(ROWS)]


def test_a_writer_holding_the_file_refuses_the_immutable_copy(home, tmp_path):
    db = _closed_rollback_store(home)
    fake = _refused_immutable_fake(db)
    writer = subprocess.Popen(
        [sys.executable, "-c", _EXCLUSIVE_WRITER, str(db)],
        cwd=db.parent,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert writer.stdout.readline().strip() == "locked"
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(snap, "sqlite3", fake)
            with pytest.raises(snap.DatabaseCopyFailed, match="a writer holds the database"):
                _copy_store_database(home, db, tmp_path / "staged.db")
    finally:
        writer.stdin.close()
        writer.wait(timeout=60)
        writer.stdout.close()
    assert writer.returncode == 0


def test_an_immutable_copy_without_the_reader_lock_keeps_the_open_s_refusal(home, tmp_path):
    db = _closed_wal_store(home)
    fake = _refused_immutable_fake(db)

    def no_record_locks(*_args, **_kwargs):
        raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(snap, "sqlite3", fake)
        mp.setattr(platform_compat.fcntl, "lockf", no_record_locks)
        with pytest.raises(snap.DatabaseCopyFailed, match="readonly"):
            _copy_store_database(home, db, tmp_path / "staged.db")
