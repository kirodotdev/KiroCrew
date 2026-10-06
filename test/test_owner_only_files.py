"""Everything Kiro Crew creates in the data home is owner-only: files 0600, directories 0700.

Every test runs under a ``022`` umask, the usual default, which is the condition
the defect needed: a file created without an explicit mode lands at ``0644`` and
a directory at ``0755``. Three layers are pinned separately because each one has
to hold on its own (``kiro_crew.owner_only_files`` explains why):

* creation -- the helpers and the stores that use them create with the mode,
  including SQLite's ``-wal``/``-shm``/``-journal`` sidecars;
* the root -- the data home itself is ``0700``;
* the startup sweep -- a fixed list of Kiro Crew's stores is tightened without
  following a link, touching a hard-linked file, failing on ``EPERM``/``EROFS``
  or changing anything not on the list, on a worker thread kicked once the
  gateway's listener serves.
"""

from __future__ import annotations

import ast
import asyncio
import errno
import inspect
import json
import logging
import os
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    import fcntl
except ImportError:  # Windows, where pytestmark skips the whole module
    fcntl = None  # type: ignore[assignment]

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


@pytest.mark.parametrize("code", [errno.EPERM, errno.EACCES, errno.EROFS])
def test_mkdirs_owner_only_accepts_an_existing_directory_whatever_mkdir_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """Like ``Path.mkdir(exist_ok=True)``: a sandbox may answer another errno ahead of EEXIST."""
    existing = tmp_path / "there"
    existing.mkdir()
    missing = tmp_path / "absent"
    real_mkdir = os.mkdir

    def refusing_mkdir(path: str, *args: object, **kwargs: object) -> None:
        if os.fspath(path) in (str(existing), str(missing)):
            raise OSError(code, os.strerror(code), path)
        real_mkdir(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "mkdir", refusing_mkdir)
    oof.mkdirs_owner_only(existing)  # accepted: it is a directory
    with pytest.raises(OSError) as raised:
        oof.mkdirs_owner_only(missing)  # not a directory: the refusal stands
    assert raised.value.errno == code


def test_ensure_directory_is_owner_only_in_the_home_and_plain_outside(
    home: Path, tmp_path: Path
) -> None:
    oof.ensure_directory(home / "artifacts" / "x")
    assert _mode(home / "artifacts") == 0o700
    assert _mode(home / "artifacts" / "x") == 0o700
    outside = tmp_path / "project" / "work"
    oof.ensure_directory(outside)
    assert _mode(outside) == 0o755  # a user's own directory keeps the umask default


def test_an_owner_only_check_does_not_resolve_the_default_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writers peek on every write; a peek that filled the cache would make the
    first ``data_home()`` skip creating the home and its recovery breadcrumb."""
    from kiro_crew.config import paths

    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(paths, "_resolved_home", None)
    monkeypatch.setattr(paths, "_config_dir_memo", None)

    assert oof.is_owner_only_target(tmp_path / ".kiro" / "crew" / "x.json")
    assert paths._resolved_home is None

    assert paths.data_home() == tmp_path / ".kiro" / "crew"
    assert (tmp_path / paths.RECOVERY_BREADCRUMB_NAME).is_file()


def test_is_owner_only_target_is_lexical_containment(home: Path, tmp_path: Path) -> None:
    assert oof.is_owner_only_target(home)
    assert oof.is_owner_only_target(home / "sessions" / "x.jsonl")
    assert not oof.is_owner_only_target(tmp_path / "elsewhere")
    assert not oof.is_owner_only_target(Path(str(home) + "-sibling") / "x")


def test_the_containment_test_does_not_resolve_another_spelling(home: Path, tmp_path: Path) -> None:
    """Deliberate: no syscall on a hot write path. Writers build paths from the home.

    A path that reaches the home through a symlink alias falls back to the
    writer's previous mode, which the home's own 0700 and the startup sweep
    still cover.
    """
    alias = tmp_path / "alias-of-home"
    alias.symlink_to(home, target_is_directory=True)
    assert oof.is_owner_only_target(home / "x", home=home)
    assert not oof.is_owner_only_target(alias / "x", home=home)


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


_LOCK_PROBE = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    print("held")
else:
    print("free")
"""


def test_tightening_releases_no_lock_this_process_holds(tmp_path: Path) -> None:
    """Closing ANY descriptor on a file drops this process's record locks on it.

    SQLite locks a database and its ``-shm`` that way, so a store preparing an
    old ``0644`` database while another connection of the same process holds it
    must not ``open`` + ``close`` it. A second process is the only observer that
    can see whether this process still holds its lock.
    """
    db = tmp_path / "in-use.db"
    db.write_bytes(b"")
    db.chmod(0o644)
    holder = os.open(db, os.O_RDWR)
    try:
        fcntl.lockf(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        oof.prepare_owner_only_sqlite(db)
        assert _mode(db) == 0o600
        probe = subprocess.run(
            [sys.executable, "-c", _LOCK_PROBE, str(db)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=True,
        )
        assert probe.stdout.strip() == "held"
    finally:
        os.close(holder)


def test_an_owner_only_database_is_only_stat_ed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "tight.db"
    oof.prepare_owner_only_sqlite(db)
    wal = tmp_path / "tight.db-wal"
    wal.write_bytes(b"")
    wal.chmod(0o600)
    opened: list[str] = []
    real_open = os.open

    def _recording_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        opened.append(os.fspath(path))  # type: ignore[arg-type]
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", _recording_open)
    oof.prepare_owner_only_sqlite(db)
    assert oof._tighten_entry(str(db)) is False

    assert [name for name in opened if "tight.db" in name] == []


def test_a_new_database_is_published_without_opening_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A creation descriptor closed on the name could drop a lock taken meanwhile.

    Once the database name exists, another connection of this process may open
    and lock it, and closing any descriptor on that inode would then release
    those locks. So the file is created under a private staging name and
    published with ``link``; no descriptor is ever opened on the name itself.
    """
    db = tmp_path / "fresh.db"
    opened: list[str] = []
    real_open = os.open

    def _recording_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        opened.append(os.path.basename(os.fspath(path)))  # type: ignore[arg-type]
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", _recording_open)
    oof.prepare_owner_only_sqlite(db)

    assert "fresh.db" not in opened
    st = os.lstat(db)
    assert stat.S_IMODE(st.st_mode) == 0o600
    assert (st.st_nlink, st.st_size) == (1, 0)
    assert [p.name for p in tmp_path.iterdir()] == ["fresh.db"]  # no staging file left


def test_a_database_created_meanwhile_is_kept_not_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "raced.db"
    header = b"SQLite format 3\x00"
    real_link = os.link

    def _link_after_another_creator(src: object, dst: object, **kwargs: object) -> None:
        db.write_bytes(header)  # another connection created the database first
        db.chmod(0o644)
        real_link(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "link", _link_after_another_creator)
    oof.prepare_owner_only_sqlite(db)

    assert db.read_bytes() == header
    assert _mode(db) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["raced.db"]


def test_without_hard_links_the_database_is_left_for_sqlite_to_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _no_hard_links(*_a: object, **_k: object) -> None:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", _no_hard_links)
    oof.prepare_owner_only_sqlite(tmp_path / "nolinks.db")

    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the O_PATH re-check is Linux's")
def test_a_name_that_no_longer_names_the_checked_inode_is_refused(tmp_path: Path) -> None:
    planted = tmp_path / "f"
    planted.write_text("x", encoding="utf-8")
    planted.chmod(0o644)
    st = os.lstat(planted)
    another_inode = os.stat_result((st.st_mode, st.st_ino + 1) + tuple(st)[2:10])

    assert oof._chmod_file_to_owner(str(planted), another_inode) is False
    assert _mode(planted) == 0o644


def test_a_symlinked_directory_below_the_home_is_left_alone(home: Path, tmp_path: Path) -> None:
    """A link planted under the home must not redirect a create or a chmod elsewhere."""
    outside = tmp_path / "outside"
    outside.mkdir()
    old = outside / "old.db"
    old.write_bytes(b"")
    old.chmod(0o644)
    real = home / "real"
    real.mkdir()
    (real / "planted").symlink_to(outside, target_is_directory=True)

    oof.prepare_owner_only_sqlite(real / "planted" / "old.db")
    oof.prepare_owner_only_sqlite(real / "planted" / "new.db")

    assert _mode(old) == 0o644
    assert not (outside / "new.db").exists()
    oof.prepare_owner_only_sqlite(real / "store.db")  # the control: no link, created 0600
    assert _mode(real / "store.db") == 0o600


def test_a_dot_dot_path_through_a_link_is_left_alone(home: Path, tmp_path: Path) -> None:
    """``link/..`` is the link target's parent to the kernel but the home to ``abspath``.

    Folding it lexically would create or chmod a database the caller's connect
    never opens, and leave the one it does open untouched.
    """
    outside = tmp_path / "outside"
    (outside / "child").mkdir(parents=True)
    behind = outside / "x.db"
    behind.write_bytes(b"")
    behind.chmod(0o644)
    in_home = home / "x.db"
    in_home.write_bytes(b"")
    in_home.chmod(0o644)
    (home / "link").symlink_to(outside / "child", target_is_directory=True)

    oof.prepare_owner_only_sqlite(home / "link" / ".." / "x.db")
    oof.prepare_owner_only_sqlite(home / "link" / ".." / "new.db")

    assert _mode(behind) == 0o644
    assert _mode(in_home) == 0o644
    assert not (outside / "new.db").exists()
    assert not (home / "new.db").exists()


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


def test_artifact_files_are_owner_only(home: Path) -> None:
    from kiro_crew.artifacts import ArtifactStore

    store = ArtifactStore(root=home / "artifacts")
    art = store.create(name="Report", content="<p>private</p>", kind="html")
    store.update(art.slug, content="<p>v2</p>")

    assert _readable_by_others(home / "artifacts") == []
    assert any((home / "artifacts").rglob("current.html"))


def test_artifact_files_under_a_symlinked_home_are_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The writer resolves its path for the fence; the mode is decided on the home's spelling."""
    from kiro_crew.artifacts import ArtifactStore

    real = tmp_path / "real-home"
    real.mkdir(mode=0o700)
    alias = tmp_path / "alias-home"
    alias.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(oof, "_policy_home", lambda: alias)

    store = ArtifactStore(root=alias / "artifacts")
    art = store.create(name="Report", content="<p>private</p>", kind="html")
    store.update(art.slug, content="<p>v2</p>")

    assert any((real / "artifacts").rglob("current.html"))
    assert _readable_by_others(real / "artifacts") == []


def test_a_stale_world_readable_artifact_tmp_is_not_published_at_its_mode(home: Path) -> None:
    """``os.open`` keeps an existing file's mode, so a ``.tmp`` a crash left is narrowed."""
    from kiro_crew.artifacts import ArtifactStore

    store = ArtifactStore(root=home / "artifacts")
    art = store.create(name="Report", content="<p>private</p>", kind="html")
    current = next((home / "artifacts").rglob("current.html"))
    stale = current.with_suffix(current.suffix + ".tmp")
    stale.write_text("left by a crash", encoding="utf-8")
    stale.chmod(0o644)

    store.update(art.slug, content="<p>v2</p>")

    assert not stale.exists()
    assert _mode(current) == 0o600
    assert _readable_by_others(home / "artifacts") == []


def test_observed_channel_history_is_owner_only(home: Path) -> None:
    from kiro_crew.channel_history import ChannelHistory

    history = ChannelHistory(history_dir=home / "channel-history")
    history.set_observe("C123")
    history.push("C123", "U1", "a private message", msg_ts="1.0")

    files = list((home / "channel-history").rglob("*.jsonl"))
    assert files, "the observe-mode history must be persisted"
    assert _readable_by_others(home / "channel-history") == []


def test_observed_channel_history_outside_the_home_keeps_the_umask_default(
    tmp_path: Path,
) -> None:
    from kiro_crew.channel_history import ChannelHistory

    history = ChannelHistory(history_dir=tmp_path / "shared-history")
    history.set_observe("C123")
    history.push("C123", "U1", "a message", msg_ts="1.0")

    files = list((tmp_path / "shared-history").rglob("*.jsonl"))
    assert files
    assert {_mode(f) for f in files} == {0o644}


def test_a_meeting_transcript_is_owner_only_in_the_home_and_plain_outside(
    home: Path, tmp_path: Path
) -> None:
    from kiro_crew.apps.builtins.meetings.backend import store

    inside = home / "apps" / "meetings" / "data"
    store.append_transcript("m1", "inside", "speech", inside)
    store.append_transcript("m1", "outside", "speech", tmp_path / "shared")

    assert _mode(store.transcript_path("m1", inside)) == 0o600
    assert _readable_by_others(home / "apps") == []
    assert _mode(store.transcript_path("m1", tmp_path / "shared")) == 0o644


def test_session_workspace_results_are_owner_only(home: Path) -> None:
    from kiro_crew import session_workspace as sw

    sw.append_history("s1", {"role": "user", "text": "x"})
    sw.write_result("s1", "a1", "result")
    sw.append_result("s1", "a2", "chunk")

    assert _readable_by_others(sw.workspace_dir("s1")) == []
    assert _mode(sw.workspace_dir("s1")) == 0o700


def test_research_campaign_files_are_owner_only(home: Path) -> None:
    from kiro_crew.apps.builtins.auto_research.campaign import storage

    campaign = "abcd1234"
    storage.write_status(campaign, "running")
    storage.write_guidance(campaign, "focus on X")
    directory = storage.research_dir() / campaign
    storage._write_new_cycle_files([(directory / "findings" / "cycle_001.json", "{}")])

    assert _mode(directory / "status.json") == 0o600
    assert _mode(directory / "guidance.txt") == 0o600
    assert _readable_by_others(storage.research_dir()) == []


def test_heartbeat_queue_and_its_lock_are_owner_only(home: Path) -> None:
    from kiro_crew.heartbeat import append_heartbeat_task, heartbeat_lock_path

    target = home / "workspace" / "HEARTBEAT.md"
    append_heartbeat_task("- check the thing", path=target)

    assert _mode(target) == 0o600
    assert _mode(heartbeat_lock_path(target)) == 0o600
    assert _mode(target.parent) == 0o700


def test_auto_improvement_run_archive_is_owner_only(home: Path) -> None:
    """Through the root the supervisor passes (``store.results_dir()``), not a tmp dir."""
    from kiro_crew.apps.builtins.auto_improvement.backend import store
    from kiro_crew.apps.builtins.auto_improvement.spine.archive import Archive

    root = store.results_dir()
    assert home in root.parents
    archive = Archive(root)
    archive.write_meta({"run": 1})
    archive.save_candidate(cand_id="c1", diff="--- a\n+++ b\n", detail={"k": "v"})
    archive.append_row({"status": "kept"})
    archive.write_drift(1, {"note": "x"})

    assert _readable_by_others(home / "apps") == []
    assert _mode(root / "results.tsv") == 0o600
    assert _mode(root / "candidates.jsonl") == 0o600
    assert _mode(root / "candidates" / "c1.diff") == 0o600


def test_a_crewmate_dashboard_instance_is_owner_only(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``members/<slug>/dashboard/``, its versions, lock and record, from a fresh adopt."""
    from kiro_crew.dashboard_templates import catalog, instance

    builtin = tmp_path / "builtin" / "fixture-board"
    builtin.mkdir(parents=True)
    manifest = {
        "id": "fixture-board",
        "version": 1,
        "title": "Fixture board",
        "description": "A template this test owns.",
        "source": "builtin",
        "fields": {"phase": {"type": "string", "source": {"agentic": True}}},
    }
    (builtin / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (builtin / "template.html").write_text(
        '<div><i data-dashboard-field="phase"></i></div>', encoding="utf-8"
    )
    monkeypatch.setattr(catalog, "builtin_dir", lambda: builtin.parent)

    instance.adopt("fleet", "fixture-board")

    members = home / "members"
    assert instance.instance_dir("fleet").parent.parent == members
    assert _mode(members) == 0o700
    assert _readable_by_others(members) == []


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


def _sweep(root: Path, stores: tuple[str, ...] = ("*",), **kw: object) -> oof.TightenReport:
    """Sweep *stores* below *root*; the default, ``*``, makes every top-level name a store."""
    kw.setdefault("max_seconds", 30.0)
    kw.setdefault("max_entries", 10_000)
    return oof.tighten_stores_to_owner(root, stores, **kw)  # type: ignore[arg-type]


def _plant(home: Path, relative: str, mode: int = 0o644) -> Path:
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    path.chmod(mode)
    return path


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


def _refuse_mode_changes(monkeypatch: pytest.MonkeyPatch, err: int) -> list[object]:
    """Make every ``chmod``/``fchmod`` fail with *err*, the way the volume would.

    Patches the OS calls themselves, not a helper of the module, so the sweep's
    real error path is what runs. Returns the list of attempted targets.
    """
    calls: list[object] = []

    def _chmod(path: object, mode: int, *_a: object, **_k: object) -> None:
        calls.append(path)
        raise OSError(err, os.strerror(err))

    def _fchmod(fd: int, mode: int) -> None:
        calls.append(fd)
        raise OSError(err, os.strerror(err))

    monkeypatch.setattr(os, "chmod", _chmod)
    monkeypatch.setattr(os, "fchmod", _fchmod)
    # The by-name fallback is chosen by membership in os.supports_*, which a
    # replaced os.chmod is not in: keep the platform's real mechanism selected.
    monkeypatch.setattr(oof, "_file_chmod_supported", lambda **_k: True)
    return calls


def test_the_sweep_survives_eperm(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    planted = home / "notes.md"
    planted.write_text("x", encoding="utf-8")
    planted.chmod(0o644)

    _refuse_mode_changes(monkeypatch, errno.EPERM)
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

    calls = _refuse_mode_changes(monkeypatch, errno.EROFS)
    report = _sweep(home)

    assert not report.complete
    assert report.stopped == "read-only filesystem"
    assert len(calls) == 1  # no point trying the rest of the volume


def test_the_sweep_stops_after_a_run_of_refusals(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(oof._MAX_CONSECUTIVE_REFUSALS + 10):
        (home / f"f{index}").write_text("x", encoding="utf-8")
        (home / f"f{index}").chmod(0o644)

    calls = _refuse_mode_changes(monkeypatch, errno.EPERM)
    report = _sweep(home)

    assert report.stopped == "permission refused repeatedly"
    assert len(calls) == oof._MAX_CONSECUTIVE_REFUSALS


def test_a_refused_startup_sweep_logs_once_not_once_per_entry(
    home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A volume that refuses every chmod must not flood ``gateway.log``."""
    for index in range(20):
        _plant(home, f"sessions/f{index}.jsonl")
    _refuse_mode_changes(monkeypatch, errno.EPERM)

    with caplog.at_level(logging.DEBUG):
        oof.tighten_data_home(home)

    assert [r.levelno for r in caplog.records if r.levelno >= logging.WARNING] == [logging.WARNING]


def test_without_a_lock_safe_chmod_a_file_is_left_alone(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``open`` + ``fchmod`` fallback: that is the lock-releasing path."""
    planted = home / "notes.md"
    planted.write_text("x", encoding="utf-8")
    planted.chmod(0o644)
    monkeypatch.setattr(oof, "_file_chmod_supported", lambda **_k: False)
    monkeypatch.setattr(oof, "_proc_fd_chmod_available", lambda: False)

    assert oof._tighten_entry(str(planted)) is False
    report = _sweep(home)

    assert report.complete
    assert report.unsupported >= 1
    assert _mode(planted) == 0o644


def test_the_startup_wrapper_reports_files_it_could_not_tighten_safely(
    home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    for name in ("a.jsonl", "b.jsonl"):
        _plant(home, f"sessions/{name}")
    monkeypatch.setattr(oof, "_file_chmod_supported", lambda **_k: False)
    monkeypatch.setattr(oof, "_proc_fd_chmod_available", lambda: False)

    with caplog.at_level(logging.WARNING, logger=oof.logger.name):
        report = oof.tighten_data_home(home)

    assert report.unsupported >= 2
    warnings = [r for r in caplog.records if "lock-safe" in r.getMessage()]
    assert len(warnings) == 1


def test_the_sweep_does_not_cross_into_another_filesystem(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mounted = home / "mounted"
    mounted.mkdir()
    mounted.chmod(0o755)
    (mounted / "f").write_text("x", encoding="utf-8")
    (mounted / "f").chmod(0o644)
    real_stat = os.stat

    def _stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        st = real_stat(path, *args, **kwargs)  # type: ignore[arg-type]
        if path == "mounted":
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1) + tuple(st)[3:10])
        return st

    monkeypatch.setattr(os, "stat", _stat)
    report = _sweep(home)

    assert report.skipped_foreign == 1
    assert _mode(mounted) == 0o755
    assert _mode(mounted / "f") == 0o644


def test_the_sweep_stops_descending_at_the_depth_cap(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(oof, "MAX_SWEEP_DEPTH", 2)
    deep = home / "a" / "b" / "c"
    deep.mkdir(parents=True)
    for directory in (home / "a", home / "a" / "b", deep):
        directory.chmod(0o755)
    (deep / "f").write_text("x", encoding="utf-8")
    (deep / "f").chmod(0o644)

    report = _sweep(home)

    assert report.complete
    assert _mode(home / "a") == 0o700
    assert _mode(home / "a" / "b") == 0o700
    assert _mode(deep) == 0o755  # level 3 is never entered
    assert _mode(deep / "f") == 0o644


def test_the_sweep_reads_directories_incrementally(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whole-directory ``listdir`` would cost a huge directory before any budget check."""
    (home / "f").write_text("x", encoding="utf-8")
    (home / "f").chmod(0o644)

    def _no_listdir(*_a: object, **_k: object) -> list[str]:
        raise AssertionError("the sweep must not materialise a whole directory")

    monkeypatch.setattr(os, "listdir", _no_listdir)
    report = _sweep(home)

    assert report.complete
    assert _mode(home / "f") == 0o600


def test_the_startup_sweep_touches_only_the_listed_stores(home: Path) -> None:
    """Whatever else is kept in the data home keeps its modes, wherever it sits.

    A project in ``workspace/``, a session workspace root ``kirocrew setup``
    placed in the home, the home itself used as a working directory, scratch
    space, the skill trees and an app's own files: none is on the list, so the
    sweep never has to recognise one to spare it.
    """
    stores = [
        _plant(home, "sessions/old.jsonl"),
        _plant(home, "artifacts/a1/index.html"),
        _plant(home, "workspace/memory/history/2026-01-01.md"),
        _plant(home, "workspace/HEARTBEAT.md"),
        _plant(home, "apps/meetings/data/m1/transcript.md"),
        _plant(home, "gateway.log.prev"),
        _plant(home, "config.json"),
        _plant(home, "memory.db-wal"),
    ]
    left = [
        _plant(home, "workspace/site/index.html"),
        _plant(home, "workspace/Dockerfile"),
        _plant(home, "session-workspaces/session-1/index.html"),
        _plant(home, "Dockerfile"),
        _plant(home, "scratch/runtime-1/build.log"),
        _plant(home, "skills/some-builtin/SKILL.md"),
        _plant(home, "apps/dev-app/Makefile"),
        _plant(home, "crons/job.py", 0o755),
    ]
    directories = ("workspace", "workspace/site", "apps", "apps/meetings", "session-workspaces")
    for directory in (*directories, "sessions", "workspace/memory"):
        (home / directory).chmod(0o755)
    before = {path: _mode(path) for path in left}

    report = oof.tighten_data_home(home)

    assert report.complete, report
    assert [str(p.relative_to(home)) for p in stores if _mode(p) != 0o600] == []
    assert {path: _mode(path) for path in left} == before
    assert _mode(home / "sessions") == 0o700
    assert _mode(home / "workspace" / "memory") == 0o700
    # The directory a store sits in is not itself a store.
    assert {d: _mode(home / d) for d in directories} == {d: 0o755 for d in directories}


def test_a_store_reached_through_a_symlinked_directory_is_left_alone(
    home: Path, tmp_path: Path
) -> None:
    """The directories leading to a store are opened ``O_NOFOLLOW``, never followed."""
    elsewhere = tmp_path / "elsewhere"
    note = _plant(elsewhere, "memory/notes.md")
    (home / "apps").mkdir(exist_ok=True)
    (home / "apps" / "meetings").symlink_to(elsewhere, target_is_directory=True)
    workspace = home / "workspace"
    if workspace.is_dir():
        workspace.rmdir()  # the test home's empty base workspace, if it made one
    workspace.symlink_to(elsewhere, target_is_directory=True)

    report = oof.tighten_data_home(home)

    assert report.complete, report
    assert report.skipped_links == 2
    assert _mode(note) == 0o644
    assert _mode(note.parent) == 0o755


def test_a_pattern_store_selects_every_matching_name(home: Path) -> None:
    logs = [_plant(home, name) for name in ("gateway.log", "gateway.log.prev", "gateway.log.1")]
    other = _plant(home, "other.log")

    report = _sweep(home, ("gateway.log*", "gateway.log"))

    assert report.complete
    assert report.tightened == 3
    assert [_mode(path) for path in logs] == [0o600, 0o600, 0o600]
    assert _mode(other) == 0o644


def test_stores_an_install_never_created_are_not_errors(tmp_path: Path) -> None:
    report = _sweep(tmp_path, oof.STARTUP_SWEEP_STORES)

    assert report.complete
    assert (report.errors, report.tightened, report.skipped_links) == (0, 0, 0)


@pytest.mark.parametrize(
    "store", ["", "/etc/hosts", "../outside", "a/../b", "work*/memory", "a\\b"]
)
def test_a_store_must_be_a_relative_path_with_a_pattern_only_last(store: str) -> None:
    """The directories leading to a store are named, so the sweep never lists one to find it."""
    with pytest.raises(ValueError):
        oof._store_groups([store])


def test_the_store_list_matches_the_stores_constants() -> None:
    from kiro_crew import heartbeat, members, memory, session_workspace
    from kiro_crew.apps.builtins.meetings.backend.constants import APP_NAME as MEETINGS
    from kiro_crew.config.sections import WorkspaceConfig
    from kiro_crew.crew_log.store import _ROOT_LEAF as CREW_LOG
    from kiro_crew.dashboard.state import _NOTIFICATIONS_FILE
    from kiro_crew.memory_stores import MEMORY_DB_FILE

    stores = set(oof.STARTUP_SWEEP_STORES)
    workspace = WorkspaceConfig().dir
    assert workspace == "workspace"
    assert {
        session_workspace._SESSIONS_DIR,
        members.MEMBERS_DIR_NAME,
        CREW_LOG,
        _NOTIFICATIONS_FILE,
        f"{MEMORY_DB_FILE}*",
        f"{memory.INDEX_DB_FILE}*",
        f"{workspace}/{memory.MEMORY_DIR_NAME}",
        f"{workspace}/{memory.INDEX_DB_FILE}*",
        f"{workspace}/{heartbeat.HEARTBEAT_FILE}",
        f"apps/{MEETINGS}/data",
    } <= stores
    assert len(stores) == len(oof.STARTUP_SWEEP_STORES)
    assert oof._store_groups(oof.STARTUP_SWEEP_STORES)  # every entry is a valid store path


def test_mkdirs_owner_only_passes_no_mode_off_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows a ``0o700`` mkdir is a protected DACL that drops the home's owner grant."""
    monkeypatch.setattr(oof.platform_compat, "IS_POSIX", False)
    oof.mkdirs_owner_only(tmp_path / "a" / "b")
    assert _mode(tmp_path / "a") == 0o755  # created with no mode argument (umask 022)
    assert _mode(tmp_path / "a" / "b") == 0o755


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


def test_listing_a_pattern_store_spends_the_budget_per_entry(home: Path) -> None:
    """A directory full of names the pattern never selects still stops the sweep."""
    for index in range(50):
        (home / f"other{index}").write_text("x", encoding="utf-8")

    by_entries = _sweep(home, stores=("gateway.log*",), max_entries=10)
    assert not by_entries.complete
    assert "entry budget" in by_entries.stopped

    checks: list[int] = []

    def _stop_after_five() -> bool:
        checks.append(1)
        return len(checks) > 5

    by_shutdown = _sweep(home, stores=("gateway.log*",), should_stop=_stop_after_five)
    assert not by_shutdown.complete
    assert by_shutdown.stopped == oof.SWEEP_STOPPED_FOR_SHUTDOWN


def test_the_sweep_reports_an_unopenable_root_instead_of_raising(tmp_path: Path) -> None:
    report = _sweep(tmp_path / "missing")
    assert not report.complete
    assert report.errors == 1


def test_a_root_that_cannot_be_listed_is_not_reported_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_scandir = os.scandir

    def refusing_scandir(target: object = ".") -> object:
        if isinstance(target, int):
            raise OSError(errno.EMFILE, os.strerror(errno.EMFILE))
        return real_scandir(target)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "scandir", refusing_scandir)
    report = _sweep(tmp_path)

    assert not report.complete
    assert report.stopped == "cannot list the data home"
    assert report.errors == 1


def test_the_startup_sweep_leaves_the_installed_skill_trees_alone(home: Path) -> None:
    """``skills/`` is fingerprinted by mode, so it is not on ``STARTUP_SWEEP_STORES``."""
    skill = home / "skills" / "some-builtin"
    skill.mkdir(parents=True)
    (home / "skills").chmod(0o755)
    (skill / "SKILL.md").write_text("x", encoding="utf-8")
    (skill / "SKILL.md").chmod(0o644)
    other = home / "sessions" / "note.jsonl"
    other.parent.mkdir()
    other.write_text("x", encoding="utf-8")
    other.chmod(0o644)

    report = oof.tighten_data_home(home)

    assert report.complete
    assert _mode(home / "skills") == 0o755
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
    report = _sweep(tmp_path, max_entries=100)
    assert report.stopped == "not a POSIX platform"
    assert _mode(planted) == 0o644


def test_the_startup_wrapper_never_raises(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> oof.TightenReport:
        raise RuntimeError("boom")

    monkeypatch.setattr(oof, "tighten_stores_to_owner", _boom)
    assert oof.tighten_data_home(home).stopped == "failed"


def test_the_cli_prologue_never_walks_the_data_home() -> None:
    """The sweep's cost grows with the user's files, so it stays off the boot path.

    ``no-new-work-on-gateway-boot-path`` covers ``cli.py``'s gateway command
    before ``asyncio.run()``. Read from the source because ``main()`` boots a
    whole gateway; any call into the sweep from ``cli.py`` would be on that path.
    """
    tree = ast.parse((REPO_ROOT / "src" / "kiro_crew" / "cli.py").read_text(encoding="utf-8"))
    called = {
        getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert not called & {"tighten_data_home", "tighten_stores_to_owner"}


@pytest.mark.parametrize(
    ("entrypoint", "serving"),
    [("start_dashboard", "await site.start()"), ("start_api_server", "await _start_site(")],
)
def test_both_gateway_entrypoints_kick_the_sweep_once_the_listener_serves(
    entrypoint: str, serving: str
) -> None:
    from kiro_crew.dashboard import server as srv

    source = inspect.getsource(getattr(srv, entrypoint))
    kick = "_kick_owner_only_sweep(state)"
    assert source.count(kick) == 1
    assert source.index(serving) < source.index(kick)


def test_the_kick_runs_the_sweep_on_a_worker_thread_as_a_tracked_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.dashboard import server as srv

    calls: list[tuple[Path, int, object]] = []

    def _recording_sweep(root: Path, *, should_stop: object) -> oof.TightenReport:
        calls.append((Path(root), threading.get_ident(), should_stop))
        return oof.TightenReport(complete=True)

    monkeypatch.setattr(srv, "tighten_data_home", _recording_sweep)
    state = SimpleNamespace(_background_tasks=set())

    async def _run() -> int:
        srv._kick_owner_only_sweep(state)  # type: ignore[arg-type]
        assert len(state._background_tasks) == 1
        await asyncio.wait_for(asyncio.gather(*state._background_tasks), timeout=30)
        return threading.get_ident()

    loop_thread = asyncio.run(_run())

    [(root, worker_thread, should_stop)] = calls
    assert root == Path(srv.data_home())
    assert worker_thread != loop_thread, "the sweep ran on the event-loop thread"
    assert should_stop == srv.shutdown_event.is_set
    assert state._background_tasks == set()


def test_a_failing_kicked_sweep_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from kiro_crew.dashboard import server as srv

    def _boom(*_a: object, **_k: object) -> oof.TightenReport:
        raise RuntimeError("boom")

    monkeypatch.setattr(srv, "tighten_data_home", _boom)
    state = SimpleNamespace(_background_tasks=set())

    async def _run() -> None:
        srv._kick_owner_only_sweep(state)  # type: ignore[arg-type]
        await asyncio.wait_for(asyncio.gather(*state._background_tasks), timeout=30)

    with caplog.at_level(logging.WARNING, logger=srv.logger.name):
        asyncio.run(_run())
    assert any("owner-only sweep" in r.getMessage() for r in caplog.records)


def test_should_stop_ends_the_sweep_at_the_next_entry(home: Path) -> None:
    for index in range(5):
        (home / f"f{index}").write_text("x", encoding="utf-8")
        (home / f"f{index}").chmod(0o644)

    report = _sweep(home, should_stop=lambda: True)

    assert report.stopped == oof.SWEEP_STOPPED_FOR_SHUTDOWN
    assert report.tightened == 0
    assert _mode(home / "f0") == 0o644


def test_a_sweep_ended_by_shutdown_logs_no_warning(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (home / "f").write_text("x", encoding="utf-8")
    (home / "f").chmod(0o644)

    with caplog.at_level(logging.WARNING, logger=oof.logger.name):
        report = oof.tighten_data_home(home, should_stop=lambda: True)

    assert report.stopped == oof.SWEEP_STOPPED_FOR_SHUTDOWN
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
