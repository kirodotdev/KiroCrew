"""Memory must survive its own file being destroyed.

Memory is the only data here that cannot be rebuilt from another source, and until
these backups existed its whole durability story was a manual ``kirocrew snapshot``.
An operator who never ran it had no copy — which is how a 36 MB store became 29 bytes
with nothing to restore from.

The properties that make a backup a backup, rather than a scheduled file copy that
happens to usually work, and each is pinned below:

* CONSISTENT UNDER A LIVE WRITER. The gateway holds the store open under WAL, so a
  plain copy of ``memory.db`` alone takes a file whose committed tail is in a ``-wal``
  sibling it did not take. That result PARSES, so nothing complains — it is simply
  missing recent writes. SQLite's online backup API is what makes the copy whole.
* BOUNDED ON DISK, and never emptied by its own retention.
* ATOMIC — an interrupted run leaves no file that looks like a backup.
* NON-DESTRUCTIVE ON RESTORE. A restore is done by someone who has already lost data
  once; what it displaces may be the last copy of something.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from conftest import make_dir_link
from kiro_crew import memory_backup as mb
from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE, resolve_store_path
from kiro_crew.vector_memory import VectorMemoryStore

pytestmark = pytest.mark.xdist_group("memory_backup")

_FIN = "fin"

#: ``memory_stores`` is an OBJECT keyed by name. An ARRAY silently degrades every store
#: onto the default one, which would make every isolation assertion here vacuous.
_CONFIG = {
    "memory_stores": {DEFAULT_MEMORY_STORE: {}, _FIN: {}},
    "default_memory_store": DEFAULT_MEMORY_STORE,
}


#: Rows every populated store here holds. Asserted as ``20`` throughout the file.
_SEED_ROWS = 20


def _point_home_at(monkeypatch: pytest.MonkeyPatch, data_home: Path) -> None:
    """Make *data_home* the data home, with the default store and one silo declared.

    The config is written to the SAME path the loader resolves ``KIROCREW_HOME`` to
    (``Path.resolve()``), not to the raw ``data_home`` the fixture was handed. On
    Windows the two can differ -- short-name (8.3) components, drive-letter casing,
    or a long parametrized temp path -- so writing to the raw path leaves the loader
    reading a home with no ``config.json``, degrading the load to the default store
    alone; the declared ``fin`` silo is then unknown.

    The three resolution memos are dropped alongside the home switch so a stale entry
    keyed on an earlier case's home cannot answer for this one. ``config_dir()`` keys
    on ``_resolved_home`` identity, ``_declared_stores`` memoizes on the config
    fingerprint, and the loaded config is cached on that same fingerprint; a coarse
    filesystem clock can leave two different homes sharing a fingerprint, so each is
    reset explicitly rather than relied on to invalidate itself.
    """
    import kiro_crew.config.paths as paths
    from kiro_crew import memory_stores
    from kiro_crew.config.loader import _invalidate_config_cache

    resolved_home = data_home.resolve()
    (resolved_home / "config.json").write_text(json.dumps(_CONFIG), encoding="utf-8")
    monkeypatch.setenv("KIROCREW_HOME", str(resolved_home))

    monkeypatch.setattr(paths, "_resolved_home", None, raising=False)
    monkeypatch.setattr(paths, "_config_dir_memo", None, raising=False)
    monkeypatch.setattr(memory_stores, "_DECLARED_MEMO", None, raising=False)
    _invalidate_config_cache()


def _write_seed_rows(store: VectorMemoryStore) -> None:
    """The one spelling of the populated store's contents, written through *store*."""
    for i in range(_SEED_ROWS):
        store.set_semantic(f"project.p{i}", f"v{i}", 1.0, "user_explicit")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data home declaring the default store and one silo.

    Returns the RESOLVED home -- the spelling the loader uses -- so paths the test
    composes from it (candidate databases, superseded-sibling globs) sit beside the
    stores the loader resolves rather than on a divergent Windows spelling.
    """
    _point_home_at(monkeypatch, tmp_path)
    return tmp_path.resolve()


@pytest.fixture(scope="module")
def seeded_memory_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The populated default store, built ONCE per module and closed.

    Every ``set_semantic`` is two durable commits (the row and its audit event), and a
    fresh ``init`` is four more, so building the 20-row store costs 44 fsyncs. The
    Windows shard prices an fsync in whole seconds when its disk is contended, and
    the shard's per-test budget is 180 s counted from setup, so paying those 44 once
    per parametrized case is what tipped a case over the cap and took the worker
    down with it. Built here in its own home so the per-case fixture only copies it.
    """
    seed_home = tmp_path_factory.mktemp("memory-backup-seed")
    with pytest.MonkeyPatch.context() as monkeypatch:
        _point_home_at(monkeypatch, seed_home)
        store = VectorMemoryStore()
        store.init()
        try:
            _write_seed_rows(store)
        finally:
            store.close()
        db_file = resolve_store_path(DEFAULT_MEMORY_STORE)
    # Closing the last handle checkpointed the WAL, so the one file IS the database.
    # A sidecar left behind would mean the copy below silently drops rows.
    assert not Path(f"{db_file}-wal").exists()
    assert _rows(db_file) == _SEED_ROWS
    return db_file


@pytest.fixture
def live_store(home: Path, seeded_memory_db: Path):
    """A populated default store, left OPEN — the state a real backup runs against.

    Held open deliberately: a backup taken while nothing has the file is the easy case
    and not the one that loses data.

    Populated by copying :func:`seeded_memory_db` into place and opening it, which is
    one commit per case instead of 44. Its rows therefore sit in the main file, not the
    WAL; a test whose property IS "the committed tail is in the WAL" needs the rows
    written through the open handle, which :class:`TestABackupIsConsistentUnderALiveWriter`
    does with its own ``live_store``.
    """
    shutil.copyfile(seeded_memory_db, resolve_store_path(DEFAULT_MEMORY_STORE))
    store = VectorMemoryStore()
    store.init()
    try:
        yield store
    finally:
        store.close()


def _rows(db_file: Path) -> int:
    conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM semantic_memory WHERE is_deleted = 0").fetchone()[
            0
        ]
    finally:
        conn.close()


def _integrity(db_file: Path) -> str:
    conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    try:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        conn.close()


def _as_legacy_wal_backup(backup: Path | None) -> Path:
    """Give *backup* the WAL header every backup carried before rollback-journal publishing."""
    assert backup is not None
    conn = sqlite3.connect(backup)
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        Path(f"{backup}{suffix}").unlink(missing_ok=True)
    return backup


class TestABackupIsConsistentUnderALiveWriter:
    @pytest.fixture
    def live_store(self, home: Path):
        """The rows written THROUGH the open handle, so the committed tail is in the WAL.

        Overrides the module fixture for this class only. The property under test is
        that a backup captures rows a plain copy of ``memory.db`` would miss, and a
        store seeded by file copy has nothing in its WAL for a plain copy to miss --
        every test here would pass against a file copy. This is the one class that
        pays the per-row writes, and it pays them for that reason.
        """
        store = VectorMemoryStore()
        store.init()
        try:
            _write_seed_rows(store)
            yield store
        finally:
            store.close()

    def test_it_captures_every_committed_row_while_the_store_is_open(
        self, live_store: VectorMemoryStore
    ) -> None:
        """The property a file copy does not have."""
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        out = mb.backup_store(src)
        assert out is not None and out.is_file()
        assert _integrity(out) == "ok"
        assert _rows(out) == len(live_store.get_all_semantic()) == 20

    def test_the_backup_is_self_contained_with_no_wal_to_pair(
        self, live_store: VectorMemoryStore
    ) -> None:
        """No ``-wal``/``-shm`` sibling, so the file alone is the whole database.

        A backup that needs a sidecar is one restore-by-copy away from silent loss,
        because whoever moves it will move the ``.db`` and leave the rest.
        """
        out = mb.backup_store(resolve_store_path(DEFAULT_MEMORY_STORE))
        assert out is not None
        assert not Path(f"{out}-wal").exists()
        assert not Path(f"{out}-shm").exists()

    def test_a_run_leaves_only_the_published_backup_in_the_directory(
        self, live_store: VectorMemoryStore
    ) -> None:
        """The staged copy's verify probe must not leave ``.partial-wal``/``-shm`` behind."""
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        out = mb.backup_store(src)
        assert out is not None
        assert sorted(p.name for p in mb.backup_dir_for(src).iterdir()) == [out.name]

    def test_reading_the_backup_creates_no_sidecars(self, live_store: VectorMemoryStore) -> None:
        """A published backup is a rollback-journal file, so opening it pairs with nothing."""
        out = mb.backup_store(resolve_store_path(DEFAULT_MEMORY_STORE))
        assert out is not None
        assert _integrity(out) == "ok" and _rows(out) == 20
        with sqlite3.connect(out) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert not Path(f"{out}-wal").exists()
        assert not Path(f"{out}-shm").exists()

    def test_a_write_after_the_backup_is_not_in_it(self, live_store: VectorMemoryStore) -> None:
        """Pins the snapshot boundary, so "consistent" is not confused with "live"."""
        out = mb.backup_store(resolve_store_path(DEFAULT_MEMORY_STORE))
        assert out is not None
        live_store.set_semantic("project.after", "later", 1.0, "user_explicit")
        assert _rows(out) == 20
        assert len(live_store.get_all_semantic()) == 21

    def test_the_backup_never_writes_to_the_store(self, live_store: VectorMemoryStore) -> None:
        """Opened read-only, so a backup can never be what corrupts the thing it copies."""
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        before = src.stat().st_mtime_ns
        mb.backup_store(src)
        assert src.stat().st_mtime_ns == before

    def test_it_is_owner_only(self, live_store: VectorMemoryStore) -> None:
        """A backup holds the same memory the store does, so it needs the same posture."""
        out = mb.backup_store(resolve_store_path(DEFAULT_MEMORY_STORE))
        assert out is not None
        import os

        if os.name == "posix":
            assert out.stat().st_mode & 0o777 == 0o600

    def test_two_backups_at_the_same_instant_preserve_both_recovery_points(
        self, live_store: VectorMemoryStore
    ) -> None:
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        stamp = datetime(2026, 9, 9, 12, 34, 56, 789012, tzinfo=timezone.utc)
        first = mb.backup_store(src, now=stamp)
        assert first is not None and _rows(first) == 20
        live_store.set_semantic("project.after_first", "later", 1.0, "user_explicit")
        second = mb.backup_store(src, now=stamp)
        assert second is not None and _rows(second) == 21
        assert first != second and first.exists() and second.exists()
        assert _rows(first) == 20
        assert set(mb.list_backups(src)) == {first, second}
        assert mb.snapshot_time(first) == stamp == mb.snapshot_time(second)

    def test_concurrent_backups_publish_distinct_snapshots_of_the_same_store(
        self, live_store: VectorMemoryStore
    ) -> None:
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        stamp = datetime(2026, 9, 9, 12, 34, 56, 789012, tzinfo=timezone.utc)
        first_ready = threading.Event()
        publish_barrier = threading.Barrier(2)
        publish_lock = threading.Lock()
        publish_count = 0
        real_publish = mb.replace_with_retry

        def publish(stage: Path, target: Path) -> None:
            nonlocal publish_count
            with publish_lock:
                publish_count += 1
                if publish_count == 1:
                    first_ready.set()
            publish_barrier.wait(timeout=10)
            real_publish(stage, target)

        with mock.patch.object(mb, "replace_with_retry", side_effect=publish):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first_result = executor.submit(mb.backup_store, src, now=stamp)
                assert first_ready.wait(timeout=10)
                live_store.set_semantic(
                    "project.between_backups", "committed", 1.0, "user_explicit"
                )
                second_result = executor.submit(mb.backup_store, src, now=stamp)
                first, second = first_result.result(timeout=10), second_result.result(timeout=10)

        assert first is not None and second is not None and first != second
        assert {_rows(first), _rows(second)} == {20, 21}
        assert set(mb.list_backups(src)) == {first, second}

    def test_one_failed_backup_cannot_remove_a_concurrent_backup_stage(
        self, live_store: VectorMemoryStore
    ) -> None:
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        stamp = datetime(2026, 9, 9, 12, 34, 56, tzinfo=timezone.utc)
        first_ready = threading.Event()
        second_ready = threading.Event()
        let_second_finish = threading.Event()
        stages: list[Path] = []
        stages_lock = threading.Lock()
        real_restrict = mb.platform_compat.restrict_to_owner
        backup_dir = mb.backup_dir_for(src)

        def restrict(stage: Path | str) -> None:
            # This module is shared with atomic config writes and directory setup.
            # Inject failure only into this test's backup stages.
            stage_path = Path(stage)
            if stage_path.parent != backup_dir or stage_path.suffix != ".partial":
                real_restrict(stage)
                return
            with stages_lock:
                index = len(stages)
                stages.append(stage_path)
            if index == 0:
                first_ready.set()
                assert second_ready.wait(timeout=10)
                raise OSError("first backup interrupted")
            second_ready.set()
            assert let_second_finish.wait(timeout=10)
            real_restrict(stage)

        with mock.patch.object(mb.platform_compat, "restrict_to_owner", side_effect=restrict):
            # An unrelated config write must not consume either barrier participant.
            mb.platform_compat.restrict_to_owner(src.parent / "config.json")
            mb.platform_compat.restrict_to_owner(str(src.parent / "config.json"))
            assert stages == []
            with ThreadPoolExecutor(max_workers=2) as executor:
                failed = executor.submit(mb.backup_store, src, now=stamp)
                assert first_ready.wait(timeout=10)
                surviving = executor.submit(mb.backup_store, src, now=stamp)
                try:
                    assert second_ready.wait(timeout=10)
                    with pytest.raises(mb.MemoryBackupFailed, match="first backup interrupted"):
                        failed.result(timeout=10)
                finally:
                    let_second_finish.set()
                backup = surviving.result(timeout=10)

        assert backup is not None and backup.exists()
        assert len(stages) == 2 and stages[0] != stages[1]
        assert mb.list_backups(src) == [backup]
        assert not list(mb.backup_dir_for(src).glob("*.partial"))


class TestEveryDeclaredStoreIsCovered:
    def test_both_the_default_store_and_a_silo_are_backed_up(self, home: Path) -> None:
        from kiro_crew.memory_stores import ensure_memory_store_dir

        ensure_memory_store_dir(_FIN)
        default = VectorMemoryStore()
        default.init()
        silo = VectorMemoryStore(db_path=resolve_store_path(_FIN))
        silo.init()
        try:
            default.set_semantic("project.a", "1", 1.0, "user_explicit")
            silo.set_semantic("project.b", "2", 1.0, "user_explicit")
            result = mb.back_up_all_stores(keep=3)
        finally:
            default.close()
            silo.close()

        assert result == {"backed_up": 2, "skipped": 0, "pruned": 0, "failed": 0}
        # Beside the store it came from, so a silo's backups inherit that silo's fence.
        assert mb.list_backups(resolve_store_path(_FIN))[0].parent.parent.name == _FIN

    def test_an_undeclared_store_directory_is_not_backed_up(self, home: Path) -> None:
        """Enumerated from the DECLARED config, never a glob.

        A glob would adopt a restored or abandoned directory the operator never
        declared, and then copy it forever.
        """
        from kiro_crew.memory_stores import memory_stores_root

        stray = memory_stores_root() / "abandoned"
        stray.mkdir(parents=True)
        (stray / "memory.db").write_bytes(b"")
        default = VectorMemoryStore()
        default.init()
        try:
            default.set_semantic("project.a", "1", 1.0, "user_explicit")
            mb.back_up_all_stores(keep=3)
        finally:
            default.close()
        assert list(stray.iterdir()) == [stray / "memory.db"]

    def test_one_unreadable_store_does_not_cost_another_its_backup(self, home: Path) -> None:
        """Fail soft PER STORE. The whole point of the loop being inside the module."""
        default = VectorMemoryStore()
        default.init()
        try:
            default.set_semantic("project.a", "1", 1.0, "user_explicit")
        finally:
            default.close()

        real = mb.backup_store

        def _explode(path: Path, **kw):
            if path.parent.name == _FIN:
                raise OSError("unreadable silo")
            return real(path, **kw)

        with mock.patch.object(mb, "backup_store", side_effect=_explode):
            with mock.patch.object(
                mb,
                "_stores_to_back_up",
                return_value=[
                    resolve_store_path(DEFAULT_MEMORY_STORE),
                    mb.Path(str(home / "memory_stores" / _FIN / "memory.db")),
                ],
            ):
                result = mb.back_up_all_stores(keep=3)

        assert result["backed_up"] == 1
        assert result["failed"] == 1
        assert mb.list_backups(resolve_store_path(DEFAULT_MEMORY_STORE))

    def test_an_empty_or_absent_store_is_skipped_not_failed(self, home: Path) -> None:
        """A store nobody opened yet is not an error, so it must not raise the count."""
        absent = home / "never-opened.db"
        assert mb.backup_store(absent) is None
        empty = home / "empty.db"
        empty.write_bytes(b"")
        assert mb.backup_store(empty) is None

    @pytest.mark.parametrize("missing", [True, False], ids=["absent", "empty"])
    def test_no_copy_is_counted_without_pruning_history_or_stopping_other_stores(
        self, home: Path, missing: bool
    ) -> None:
        from kiro_crew.memory_stores import ensure_memory_store_dir

        start = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
        global_path = resolve_store_path(DEFAULT_MEMORY_STORE)
        default = VectorMemoryStore()
        default.init()
        try:
            default.set_semantic("project.original", "preserve me", 1.0, "user_explicit")
            for days in (0, 1):
                assert mb.backup_store(global_path, now=start + timedelta(days=days)) is not None
        finally:
            default.close()
        retained = {path: path.read_bytes() for path in mb.list_backups(global_path)}
        assert len(retained) == 2
        if missing:
            global_path.unlink()
        else:
            global_path.write_bytes(b"")

        ensure_memory_store_dir(_FIN)
        silo_path = resolve_store_path(_FIN)
        silo = VectorMemoryStore(db_path=silo_path)
        silo.init()
        try:
            silo.set_semantic("project.healthy", "copy me", 1.0, "user_explicit")
            result = mb.back_up_all_stores(keep=1, now=start + timedelta(days=3))
        finally:
            silo.close()

        assert result == {"backed_up": 1, "skipped": 1, "pruned": 0, "failed": 0}
        assert {path: path.read_bytes() for path in mb.list_backups(global_path)} == retained
        if missing:
            assert not global_path.exists()
        else:
            assert global_path.read_bytes() == b""
        copied = mb.list_backups(silo_path)
        assert len(copied) == 1
        assert _integrity(copied[0]) == "ok"
        assert _rows(copied[0]) == 1


class TestARestartCannotShrinkTheRetentionWindow:
    def test_a_second_pass_inside_the_interval_skips_instead_of_copying(
        self, live_store: VectorMemoryStore
    ) -> None:
        """The heartbeat's tick counter is PER PROCESS and resets on every gateway start.

        Without an interval guard the pass runs once per restart, so a gateway restarted
        five times a day writes five copies and the ``keep`` window collapses from seven
        days to a day and a half. The retention window IS the feature.
        """
        base = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
        assert mb.back_up_all_stores(keep=7, now=base)["backed_up"] == 1
        for hours in (1, 6, mb.MIN_BACKUP_INTERVAL_HOURS - 1):
            result = mb.back_up_all_stores(keep=7, now=base + timedelta(hours=hours))
            # Global is fresh; the declared finance store has never been opened.
            assert result == {"backed_up": 0, "skipped": 2, "pruned": 0, "failed": 0}, hours
        assert len(mb.list_backups(resolve_store_path(DEFAULT_MEMORY_STORE))) == 1

    def test_a_pass_past_the_interval_copies_again(self, live_store: VectorMemoryStore) -> None:
        base = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
        mb.back_up_all_stores(keep=7, now=base)
        later = base + timedelta(hours=mb.MIN_BACKUP_INTERVAL_HOURS + 1)
        assert mb.back_up_all_stores(keep=7, now=later)["backed_up"] == 1
        assert len(mb.list_backups(resolve_store_path(DEFAULT_MEMORY_STORE))) == 2

    def test_the_age_is_read_from_the_stamped_name_not_the_mtime(
        self, live_store: VectorMemoryStore
    ) -> None:
        """A restored or copied file carries a new mtime while its name still tells the truth.

        Touching a backup to look brand new must not suppress the next real one.
        """
        import os

        base = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
        mb.back_up_all_stores(keep=7, now=base)
        only = mb.list_backups(resolve_store_path(DEFAULT_MEMORY_STORE))[0]
        os.utime(only, None)  # The name retains the original backup date.
        later = base + timedelta(hours=mb.MIN_BACKUP_INTERVAL_HOURS + 1)
        assert mb.back_up_all_stores(keep=7, now=later)["backed_up"] == 1

    def test_an_unparseable_name_does_not_suppress_a_backup(
        self, live_store: VectorMemoryStore
    ) -> None:
        """Erring toward TAKING a copy is the safe direction for a name we cannot read."""
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        out_dir = mb.backup_dir_for(src)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{src.stem}.not-a-timestamp.db").write_bytes(b"")
        assert mb.back_up_all_stores(keep=7)["backed_up"] == 1


class TestTheManualVerbTakesTheCopyItWasAskedFor:
    """``kirocrew memory backup`` is an operator asking for a copy NOW.

    The interval guard exists for the heartbeat, whose per-process tick counter would
    otherwise take a copy on every restart. An operator running the verb by hand is
    usually about to do something risky -- a restore, an out-of-band edit -- and needs
    the store as it is now, not as it was up to a day ago. The dashboard's
    "back up now" and the pre-update copy already bypass the guard; the CLI verb must
    too, and the two sides of that asymmetry are pinned here so a refactor cannot
    collapse them in either direction.
    """

    @staticmethod
    def _run_cli_backup(keep: int = 7) -> None:
        from types import SimpleNamespace

        from kiro_crew.cli_commands import _memory_backup_cmd

        _memory_backup_cmd("backup", SimpleNamespace(keep=keep))

    def test_the_cli_verb_copies_even_when_a_fresh_backup_exists(
        self, live_store: VectorMemoryStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        assert mb.back_up_all_stores(keep=7)["backed_up"] == 1  # the sweep, seconds ago
        assert len(mb.list_backups(src)) == 1

        self._run_cli_backup()

        assert len(mb.list_backups(src)) == 2
        # The declared finance store has never been opened: nothing to copy, and that
        # is said rather than folded into three zeros.
        assert capsys.readouterr().out == (
            "Backed up 1 store(s); removed 0 old; 0 failed; 1 skipped (nothing to copy).\n"
        )

    @pytest.mark.asyncio
    async def test_the_timer_still_declines_inside_the_interval(
        self, live_store: VectorMemoryStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The other half of the asymmetry: the scheduled pass keeps the guard.

        Forcing it too would reintroduce the restart loop the interval exists to stop,
        so the heartbeat's own pass is driven here, against the same fresh copy the
        CLI test copies over, and must leave the directory as it found it.

        The pass FAILS SOFT -- ``_back_up_memory`` logs and swallows any exception --
        so an unchanged directory alone would also be what a pass that never ran leaves.
        The warning it logs on that path is asserted absent, which is what makes the
        unchanged count evidence of a decline rather than of a crash.
        """
        import logging
        from unittest.mock import MagicMock

        from kiro_crew.heartbeat import HeartbeatService

        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        assert mb.back_up_all_stores(keep=7)["backed_up"] == 1  # the sweep, seconds ago
        service = HeartbeatService(MagicMock(), consolidator=MagicMock())
        try:
            with caplog.at_level(logging.WARNING, logger="kiro_crew.heartbeat"):
                await service._back_up_memory()
        finally:
            service.stop()

        assert not [r for r in caplog.records if r.name == "kiro_crew.heartbeat"], caplog.text
        assert len(mb.list_backups(src)) == 1

    def test_the_summary_line_reports_the_skipped_count(
        self, home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A pass that copied nothing must say so, not print three reassuring zeros.

        ``home`` declares two stores and opens neither, so there is nothing to copy; a
        line reading only ``0 ... 0 ... 0`` has to carry the count that explains it.
        """
        self._run_cli_backup()

        assert capsys.readouterr().out == (
            "Backed up 0 store(s); removed 0 old; 0 failed; 2 skipped (nothing to copy).\n"
        )


class TestAFailedCopyIsCountedAndNotMistakenForASkip:
    def test_a_copy_failure_reaches_the_failed_counter(self, live_store: VectorMemoryStore) -> None:
        """The counter was unreachable while "nothing to copy" and "copy failed" both
        answered ``None`` — so a store failing every single day logged nothing at all."""
        attempted = len(mb._stores_to_back_up())
        with mock.patch.object(mb, "backup_store", side_effect=mb.MemoryBackupFailed("disk full")):
            result = mb.back_up_all_stores(keep=7)
        # EVERY attempted store is counted, not just the first: fail-soft per store means
        # the loop carries on, and a count that stopped at one would understate how much
        # of the install has lost its backup.
        assert attempted >= 1
        assert result["failed"] == attempted
        assert result["backed_up"] == 0

    def test_nothing_to_copy_is_not_counted_as_a_failure(self, home: Path) -> None:
        """The routine case stays routine: an unopened store is neither copied nor failed."""
        result = mb.back_up_all_stores(keep=7)
        assert result == {"backed_up": 0, "skipped": 2, "pruned": 0, "failed": 0}


class TestRetentionIsBoundedAndCannotEmptyItself:
    def test_only_the_newest_keep_survive(self, live_store: VectorMemoryStore) -> None:
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        base = datetime(2026, 1, 10, tzinfo=timezone.utc)
        for day in range(6):
            mb.backup_store(src, now=base + timedelta(days=day))
        assert len(mb.list_backups(src)) == 6

        assert mb.prune_backups(src, keep=3) == 3
        survivors = [p.name for p in mb.list_backups(src)]
        assert len(survivors) == 3
        # Newest first, and it is the LATEST three that survived.
        assert survivors[0] > survivors[1] > survivors[2]
        assert "20260115" in survivors[0]

    def test_a_pruned_legacy_backup_takes_its_sidecars_with_it(
        self, live_store: VectorMemoryStore
    ) -> None:
        """Reading a WAL-header backup leaves ``-wal``/``-shm`` that only prune can reclaim."""
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        base = datetime(2026, 1, 10, tzinfo=timezone.utc)
        old = _as_legacy_wal_backup(mb.backup_store(src, now=base))
        mb.backup_store(src, now=base + timedelta(days=1))
        assert _integrity(old) == "ok"
        assert Path(f"{old}-shm").exists()

        assert mb.prune_backups(src, keep=1) == 1
        assert [p for p in mb.backup_dir_for(src).iterdir() if p.name.startswith(old.name)] == []

    @pytest.mark.parametrize("keep", [0, -1])
    def test_a_keep_below_one_is_clamped_rather_than_emptying_the_directory(
        self, live_store: VectorMemoryStore, keep: int
    ) -> None:
        """Retention bounds disk; a policy that can delete everything is not retention."""
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        base = datetime(2026, 1, 10, tzinfo=timezone.utc)
        for day in range(3):
            mb.backup_store(src, now=base + timedelta(days=day))
        mb.prune_backups(src, keep=keep)
        assert len(mb.list_backups(src)) == 1

    def test_listing_is_ordered_by_the_stamp_not_the_mtime(
        self, live_store: VectorMemoryStore
    ) -> None:
        """A copied or restored file carries a new mtime while its name still tells the truth."""
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        base = datetime(2026, 1, 10, tzinfo=timezone.utc)
        old = mb.backup_store(src, now=base)
        new = mb.backup_store(src, now=base + timedelta(days=1))
        assert old is not None and new is not None
        import os

        # Touch the OLD one so mtime order is the reverse of stamp order.
        os.utime(old, (10**9, 10**9 + 500))
        assert mb.list_backups(src)[0] == new

    def test_current_and_legacy_names_share_timestamp_ordering_and_retention(
        self, live_store: VectorMemoryStore
    ) -> None:
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        base = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)
        generated = mb.backup_store(src, now=base)
        assert generated is not None
        legacy = generated.with_name(f"{src.stem}.{base.strftime(mb._STAMP_FORMAT)}.db")
        generated.replace(legacy)
        current_1 = mb.backup_store(src, now=base + timedelta(microseconds=1))
        current_2 = mb.backup_store(src, now=base + timedelta(microseconds=2))
        assert current_1 is not None and current_2 is not None

        assert mb.list_backups(src) == [current_2, current_1, legacy]
        assert mb.snapshot_time(legacy) == base
        assert mb.prune_backups(src, keep=2) == 1
        assert mb.list_backups(src) == [current_2, current_1]
        assert not legacy.exists()


class TestAnInterruptedBackupLeavesNothingThatLooksLikeOne:
    def test_a_failure_mid_copy_leaves_no_partial_and_no_target(
        self, live_store: VectorMemoryStore
    ) -> None:
        """Written to ``.partial`` and RENAMED, so a truncated file is never mistaken for a backup.

        The failure is injected AFTER the copy and BEFORE the rename, which is the exact
        window a ``.partial`` exists in — a failure during the copy would leave nothing
        either way, so it would not test the rename at all.
        """
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        # RAISES rather than answering None: "there was nothing to copy" and "the copy
        # failed" need opposite handling, and folding them together is what made the
        # caller's failure counter unreachable.
        with mock.patch.object(
            mb.platform_compat, "restrict_to_owner", side_effect=OSError("interrupted")
        ):
            with pytest.raises(mb.MemoryBackupFailed):
                mb.backup_store(src)
        out_dir = mb.backup_dir_for(src)
        assert mb.list_backups(src) == []
        assert list(out_dir.glob("*.partial")) == []
        assert list(out_dir.glob(".*.partial*")) == []

    def test_prune_reclaims_orphaned_staging_sidecars_only(
        self, live_store: VectorMemoryStore
    ) -> None:
        """A ``.partial-wal``/``-shm`` with no ``.partial`` beside it belongs to no live run."""
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        out = mb.backup_store(src)
        assert out is not None
        out_dir = mb.backup_dir_for(src)
        orphan = out_dir / f".{src.stem}.20260101T000000000000Z-{'a' * 32}.db.{'b' * 32}.partial"
        live = out_dir / f".{src.stem}.20260102T000000000000Z-{'c' * 32}.db.{'d' * 32}.partial"
        live.write_bytes(b"")
        for stage in (orphan, live):
            for suffix in ("-wal", "-shm"):
                Path(f"{stage}{suffix}").write_bytes(b"")

        assert mb.prune_backups(src) == 0
        assert not Path(f"{orphan}-wal").exists()
        assert not Path(f"{orphan}-shm").exists()
        assert Path(f"{live}-wal").exists() and Path(f"{live}-shm").exists()
        assert out.exists()

    def test_prune_reclaims_a_stage_left_by_a_killed_run(
        self, live_store: VectorMemoryStore
    ) -> None:
        """A run killed before its cleanup leaves the ``.partial`` itself; age reclaims it."""
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        out = mb.backup_store(src)
        assert out is not None
        out_dir = mb.backup_dir_for(src)
        stale = out_dir / f".{src.stem}.20260101T000000000000Z-{'e' * 32}.db.{'f' * 32}.partial"
        fresh = out_dir / f".{src.stem}.20260102T000000000000Z-{'1' * 32}.db.{'2' * 32}.partial"
        old = datetime.now(timezone.utc).timestamp() - mb.STALE_STAGE_SECONDS - 60
        for stage in (stale, fresh):
            for suffix in ("", "-wal", "-shm"):
                Path(f"{stage}{suffix}").write_bytes(b"x")
        os.utime(stale, (old, old))

        mb.prune_backups(src)
        assert [p for p in out_dir.iterdir() if p.name.startswith(stale.name)] == []
        assert all(Path(f"{fresh}{suffix}").exists() for suffix in ("", "-wal", "-shm"))
        assert out.exists()

    def test_a_stage_exactly_at_the_stale_age_is_kept(self, live_store: VectorMemoryStore) -> None:
        """Only a stage OLDER than ``STALE_STAGE_SECONDS`` goes; the boundary itself stays."""
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        out_dir = mb.backup_dir_for(src)
        out_dir.mkdir(parents=True, exist_ok=True)
        now = 1_800_000_000
        boundary = out_dir / f".{src.stem}.20260101T000000000000Z-{'3' * 32}.db.{'4' * 32}.partial"
        older = out_dir / f".{src.stem}.20260101T000000000000Z-{'5' * 32}.db.{'6' * 32}.partial"
        for stage, mtime in (
            (boundary, now - mb.STALE_STAGE_SECONDS),
            (older, now - mb.STALE_STAGE_SECONDS - 1),
        ):
            stage.write_bytes(b"x")
            os.utime(stage, ns=(mtime * 10**9, mtime * 10**9))

        mb._prune_orphaned_stages(src, out_dir, now=now)
        assert boundary.exists()
        assert not older.exists()

    def test_a_run_removes_its_sidecars_before_its_partial(
        self, live_store: VectorMemoryStore
    ) -> None:
        """The prune sweep reads a sidecar without its ``.partial`` as an ended run."""
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        removed: list[str] = []
        original = Path.unlink

        def recording_unlink(self: Path, missing_ok: bool = False) -> None:
            if ".partial" in self.name:
                removed.append(self.name)
            original(self, missing_ok=missing_ok)

        with (
            mock.patch.object(
                mb.platform_compat, "restrict_to_owner", side_effect=OSError("interrupted")
            ),
            mock.patch.object(Path, "unlink", recording_unlink),
        ):
            with pytest.raises(mb.MemoryBackupFailed):
                mb.backup_store(src)
        stage = [name for name in removed if name.endswith(".partial")]
        assert len(stage) == 1
        assert removed[-1] == stage[0]
        assert {f"{stage[0]}-wal", f"{stage[0]}-shm"} <= set(removed)


class TestRestoreIsNonDestructive:
    @pytest.mark.parametrize("store_name", [DEFAULT_MEMORY_STORE, _FIN])
    @pytest.mark.parametrize("source_kind", ["v2", "owner_only", "empty", "unrelated"])
    def test_wrong_sqlite_source_is_refused_without_staging_or_displacing_memory(
        self, live_store, home, store_name, source_kind
    ):
        from kiro_crew import memory_schema
        from kiro_crew.memory_stores import ensure_memory_store_dir

        source = resolve_store_path(DEFAULT_MEMORY_STORE)
        if store_name != DEFAULT_MEMORY_STORE:
            ensure_memory_store_dir(store_name)
            snapshot = mb.backup_store(source)
            assert snapshot is not None
            resolve_store_path(store_name).write_bytes(snapshot.read_bytes())
        target = resolve_store_path(store_name)
        candidate = home / f"{source_kind}-candidate.db"
        db = sqlite3.connect(candidate)
        try:
            if source_kind in ("v2", "owner_only"):
                db.executescript(memory_schema.CREW_SCHEMA_SQL)
                db.executescript(memory_schema.MEMBER_SCHEMA_SQL)
                if source_kind == "v2":
                    db.execute("INSERT INTO member_database VALUES (1, 1, 'alice', 'member-alice')")
                db.commit()
            elif source_kind == "unrelated":
                db.execute("CREATE TABLE unrelated (value TEXT)")
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            db.close()
        candidate_before = candidate.read_bytes()
        current_before = {
            path: path.read_bytes()
            for suffix in ("", "-wal", "-shm")
            if (path := Path(str(target) + suffix)).exists()
        }

        error = (
            "V1 restore cannot install a member memory database"
            if source_kind in ("v2", "owner_only") and store_name != DEFAULT_MEMORY_STORE
            else "V1 restore requires a V1 memory database"
        )
        with pytest.raises(ValueError, match=error):
            mb.restore_from_backup(candidate, store_name)

        assert candidate.read_bytes() == candidate_before
        assert all(path.read_bytes() == content for path, content in current_before.items())
        assert not mb.pending_restore_status(target)["pending"]
        assert not list(mb.backup_dir_for(target).glob("restore-*.db"))
        assert not list(target.parent.glob("memory.db.superseded.*"))
        assert _rows(target) == 20
        live_store.set_semantic("project.after_refusal", "retained", 1.0, "user_explicit")
        assert len(live_store.get_all_semantic()) == 21

    def test_a_v1_backup_restores_a_named_v1_store(self, live_store):
        from kiro_crew.memory_stores import ensure_memory_store_dir

        source = resolve_store_path(DEFAULT_MEMORY_STORE)
        snapshot = mb.backup_store(source)
        assert snapshot is not None
        ensure_memory_store_dir(_FIN)
        target = resolve_store_path(_FIN)
        target.write_bytes(snapshot.read_bytes())

        mb.restore_from_backup(snapshot, _FIN)
        assert mb.pending_restore_status(target)["pending"]
        assert _FIN in mb.apply_pending_member_restores()
        assert _rows(target) == 20
        assert len(live_store.get_all_semantic()) == 20

    def test_markerless_crew_schema_restores_only_to_named_legacy_v1(self, home):
        from kiro_crew import memory_schema
        from kiro_crew.memory_stores import ensure_memory_store_dir

        ensure_memory_store_dir(_FIN)
        target = resolve_store_path(_FIN)
        legacy = VectorMemoryStore(db_path=target)
        legacy.init()
        try:
            assert legacy._lineage == memory_schema.LINEAGE_CREW
            assert legacy._memory_version == 1
            legacy.set_semantic("project.before", "keep", 1.0, "user_explicit")
            snapshot = mb.backup_store(target)
            assert snapshot is not None and snapshot.suffix == ".db"
            mb.restore_from_backup(snapshot, _FIN)
            legacy.set_semantic("project.after", "preserve too", 1.0, "user_explicit")
        finally:
            legacy.close()
        restored = mb.apply_pending_member_restores()
        assert _rows(target) == 1
        assert _rows(target.with_name(restored[_FIN])) == 2
        with pytest.raises(ValueError, match="V1 restore requires a V1 memory database"):
            mb.restore_from_backup(snapshot, DEFAULT_MEMORY_STORE)
        assert not mb.pending_restore_status(resolve_store_path(DEFAULT_MEMORY_STORE))["pending"]

    def test_named_legacy_restore_can_cancel_an_unreadable_unactivated_journal(self, home):
        from kiro_crew.memory_stores import ensure_memory_store_dir

        ensure_memory_store_dir(_FIN)
        target = resolve_store_path(_FIN)
        legacy = VectorMemoryStore(db_path=target)
        legacy.init()
        try:
            legacy.set_semantic("project.before", "keep", 1.0, "user_explicit")
            snapshot = mb.backup_store(target)
            assert snapshot is not None
            mb.restore_from_backup(snapshot, _FIN)
            out = mb.backup_dir_for(target)
            (out / mb._V1_PENDING).write_bytes(b"unreadable journal")
            assert mb.cancel_pending_restore(target)
            assert not mb.pending_restore_status(target)["pending"]
            assert _rows(target) == 1
            assert snapshot.is_file()
        finally:
            legacy.close()

    def test_journal_permission_failure_precedes_content_and_allows_retry(
        self, live_store, monkeypatch
    ):
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        backup = mb.backup_store(src)
        out = backup.parent
        real_restrict = mb.platform_compat.restrict_to_owner
        sizes_at_restriction = []

        def refuse_journal_permission(path):
            path = Path(path)
            if path.parent == out and path.suffix in (".tmp", ".partial"):
                sizes_at_restriction.append(path.stat().st_size)
                raise OSError("injected journal permission failure")
            return real_restrict(path)

        with monkeypatch.context() as patcher:
            patcher.setattr(mb.platform_compat, "restrict_to_owner", refuse_journal_permission)
            with pytest.raises(OSError, match="journal permission failure"):
                mb.restore_from_backup(backup)
        assert sizes_at_restriction == [0]
        assert not mb.pending_restore_status(src)["pending"]
        assert not list(out.glob("restore-*.db"))
        assert not list(out.glob("*.tmp")) and not list(out.glob("*.partial"))
        live_store.set_semantic("project.after_failure", "retained", 1.0, "user_explicit")
        assert len(live_store.get_all_semantic()) == 21
        mb.restore_from_backup(backup)
        assert mb.pending_restore_status(src)["pending"]
        assert len(live_store.get_all_semantic()) == 21 and _rows(backup) == 20

    def test_staging_keeps_live_wal_writes_until_startup(self, live_store):
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        backup = mb.backup_store(src)
        live_store.set_semantic("project.after_backup", "committed", 1.0, "user_explicit")
        mb.restore_from_backup(backup)
        live_store.set_semantic("project.after_staging", "also committed", 1.0, "user_explicit")
        assert len(live_store.get_all_semantic()) == 22
        assert mb.pending_restore_status(src)["restart_required"]
        assert list(src.parent.glob("memory.db.superseded.*")) == []

        # Recreate a closed, uncheckpointed database without a child process:
        # capture its stable WAL pair, close the owner, then restore those bytes.
        main_bytes = src.read_bytes()
        wal = Path(f"{src}-wal")
        wal_bytes = wal.read_bytes()
        assert wal_bytes
        live_store.close()
        src.write_bytes(main_bytes)
        wal.write_bytes(wal_bytes)
        applied = mb.apply_pending_member_restores()
        aside = src.with_name(applied[DEFAULT_MEMORY_STORE])
        assert _rows(src) == 20
        assert _rows(aside) == 22
        assert _integrity(aside) == "ok"
        assert mb.apply_pending_member_restores() == {}

    def test_failed_install_rolls_back_and_keeps_the_stage(self, live_store, monkeypatch):
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        backup = mb.backup_store(src)
        live_store.set_semantic("project.latest", "keep this", 1.0, "user_explicit")
        mb.restore_from_backup(backup)
        live_store.close()
        real_replace = mb.replace_with_retry

        def fail_install(source, target):
            if Path(source).name.startswith("restore-") and Path(target) == src:
                raise OSError("injected installation failure")
            return real_replace(source, target)

        with monkeypatch.context() as patcher:
            patcher.setattr(mb, "replace_with_retry", fail_install)
            with pytest.raises(mb.MemoryBackupFailed, match="installation failure"):
                mb.apply_pending_member_restores()
        assert _rows(src) == 21
        assert mb.pending_restore_status(src)["pending"]
        aside = src.with_name(mb.apply_pending_member_restores()[DEFAULT_MEMORY_STORE])
        assert _rows(aside) == 21 and _rows(src) == 20

    def test_completed_install_recovers_when_journal_removal_failed(self, live_store, monkeypatch):
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        backup = mb.backup_store(src)
        live_store.set_semantic("project.latest", "keep this", 1.0, "user_explicit")
        mb.restore_from_backup(backup)
        live_store.close()
        unlink = Path.unlink

        def fail_journal_removal(path, *args, **kwargs):
            if path.name == mb._V1_PENDING:
                raise OSError("injected journal cleanup failure")
            return unlink(path, *args, **kwargs)

        with monkeypatch.context() as patcher:
            patcher.setattr(Path, "unlink", fail_journal_removal)
            with pytest.raises(mb.MemoryBackupFailed, match="journal cleanup failure"):
                mb.apply_pending_member_restores()
        aside = src.with_name(mb.apply_pending_member_restores()[DEFAULT_MEMORY_STORE])
        assert _rows(aside) == 21 and _rows(src) == 20
        assert not mb.pending_restore_status(src)["pending"]

    def test_retry_after_rollback_and_sqlite_wal_checkpoint(self, live_store, monkeypatch):
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        backup = mb.backup_store(src)
        live_store.set_semantic("project.latest", "keep this", 1.0, "user_explicit")
        mb.restore_from_backup(backup)
        main_bytes = src.read_bytes()
        wal = Path(f"{src}-wal")
        wal_bytes = wal.read_bytes()
        assert wal_bytes
        live_store.close()
        src.write_bytes(main_bytes)
        wal.write_bytes(wal_bytes)
        real_replace = mb.replace_with_retry

        def fail_install(source, target):
            if Path(source).name.startswith("restore-") and Path(target) == src:
                raise OSError("injected installation failure")
            return real_replace(source, target)

        with monkeypatch.context() as patcher:
            patcher.setattr(mb, "replace_with_retry", fail_install)
            with pytest.raises(mb.MemoryBackupFailed, match="installation failure"):
                mb.apply_pending_member_restores()
        assert wal.exists()
        # A normal diagnostic connection checkpoints when its last handle closes.
        connection = sqlite3.connect(src)
        try:
            assert connection.execute("SELECT COUNT(*) FROM semantic_memory").fetchone()[0] == 21
        finally:
            connection.close()
        assert not wal.exists()
        aside = src.with_name(mb.apply_pending_member_restores()[DEFAULT_MEMORY_STORE])
        assert _rows(aside) == 21 and _rows(src) == 20
        assert not mb.pending_restore_status(src)["pending"]

    def test_retry_preserves_inventory_after_a_partial_move(self, live_store):
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        backup = mb.backup_store(src)
        live_store.set_semantic("project.latest", "keep this", 1.0, "user_explicit")
        mb.restore_from_backup(backup)
        live_store.close()
        out, journal = mb._v1_journal(src)
        journal["prior_files"] = [""]
        mb._write_v1_journal(out, journal)
        aside = src.with_name(journal["aside"])
        # Simulate a process crash after the previous main file moved aside.
        src.replace(aside)

        assert mb.apply_pending_member_restores() == {DEFAULT_MEMORY_STORE: aside.name}
        assert _rows(aside) == 21 and _rows(src) == 20
        assert not mb.pending_restore_status(src)["pending"]

    def test_cancelled_stage_cannot_activate_and_does_not_block_a_new_restore(self, live_store):
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        backup = mb.backup_store(src)
        mb.restore_from_backup(backup)
        with pytest.raises(ValueError, match="already pending"):
            mb.restore_from_backup(backup)
        assert mb.cancel_pending_restore(src)
        assert not mb.cancel_pending_restore(src)
        assert mb.apply_pending_member_restores() == {}
        assert len(live_store.get_all_semantic()) == 20
        mb.restore_from_backup(backup)
        assert mb.pending_restore_status(src)["pending"]

    def test_the_displaced_file_is_kept_aside(self, live_store: VectorMemoryStore) -> None:
        """Even a file that looks worthless may be the last copy of something."""
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        backup = mb.backup_store(src)
        assert backup is not None
        live_store.close()
        src.write_bytes(b"this is not a sqlite database")

        mb.restore_from_backup(backup, DEFAULT_MEMORY_STORE)
        mb.apply_pending_member_restores()
        aside = list(src.parent.glob("memory.db.superseded.*"))
        assert len(aside) == 1
        assert aside[0].read_bytes() == b"this is not a sqlite database"

    def test_a_corrupt_store_is_recovered_with_its_rows(
        self, live_store: VectorMemoryStore
    ) -> None:
        """The scenario this feature exists for, end to end."""
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        mb.backup_store(src)
        live_store.close()
        src.write_bytes(b"this is not a sqlite database")

        newest = mb.newest_backup(DEFAULT_MEMORY_STORE)
        assert newest is not None
        mb.restore_from_backup(newest, DEFAULT_MEMORY_STORE)
        mb.apply_pending_member_restores()
        assert _integrity(src) == "ok"
        assert _rows(src) == 20

    def test_a_corrupt_BACKUP_is_refused_before_anything_is_displaced(
        self, live_store: VectorMemoryStore
    ) -> None:
        """Restoring damage over damage leaves the operator strictly worse off."""
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        good = mb.backup_store(src)
        assert good is not None
        good.write_bytes(b"not a database either")

        with pytest.raises(Exception):
            mb.restore_from_backup(good, DEFAULT_MEMORY_STORE)

        # Nothing displaced, and the store still works.
        assert list(src.parent.glob("memory.db.superseded.*")) == []
        assert _integrity(src) == "ok"

    def test_restoring_a_legacy_wal_header_backup_leaves_no_stage_sidecars(
        self, live_store: VectorMemoryStore
    ) -> None:
        """A backup taken before rollback-journal publishing still restores without residue."""
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        legacy = _as_legacy_wal_backup(mb.backup_store(src))
        out_dir = mb.backup_dir_for(src)

        mb.restore_from_backup(legacy, DEFAULT_MEMORY_STORE)
        assert list(out_dir.glob("restore-*.db-*")) == []
        live_store.close()
        mb.apply_pending_member_restores()
        assert _rows(src) == 20
        assert list(out_dir.glob("restore-*")) == []

    def test_the_stale_wal_of_the_displaced_file_is_removed(
        self, live_store: VectorMemoryStore
    ) -> None:
        """A leftover WAL describes the database that the restore replaces.

        SQLite would try to apply it to the restored file, which is how a restore can
        end up corrupting the thing it just recovered.
        """
        src = resolve_store_path(DEFAULT_MEMORY_STORE)
        backup = mb.backup_store(src)
        assert backup is not None
        live_store.close()
        Path(f"{src}-wal").write_bytes(b"stale wal")

        mb.restore_from_backup(backup, DEFAULT_MEMORY_STORE)
        mb.apply_pending_member_restores()
        # Absent is the ordinary outcome and is what "removed" means; what must never
        # survive is the stale content, which would be replayed onto the restored file.
        wal = Path(f"{src}-wal")
        assert not wal.exists() or wal.read_bytes() != b"stale wal"

    def test_restoring_a_missing_backup_raises_rather_than_no_ops(self, home: Path) -> None:
        """A restore is explicit; a silent no-op is the one outcome that must be impossible."""
        with pytest.raises(FileNotFoundError):
            mb.restore_from_backup(home / "nope.db", DEFAULT_MEMORY_STORE)


def test_bad_v1_journal_can_be_quarantined_only_with_intact_current_memory(live_store):
    path = live_store._db_path
    backup = mb.backup_store(path)
    mb.restore_from_backup(backup)
    out = mb.backup_dir_for(path)
    pending = out / mb._V1_PENDING
    staged = out / json.loads(pending.read_text())["stage"]
    bad = b"unreadable V1 restore journal"
    pending.write_bytes(bad)
    status = mb.pending_restore_status(path)
    assert status["pending"] and status["restore_error"]
    assert mb.cancel_pending_restore(path)
    quarantined = list(out.glob("cancelled-v1-restore-*.json"))
    assert len(quarantined) == 1 and quarantined[0].read_bytes() == bad
    assert staged.exists() and backup.exists()
    assert _rows(path) == 20


def test_bad_v1_journal_can_be_cancelled_through_a_linked_default_home(tmp_path, monkeypatch):
    from kiro_crew.config import paths

    physical_home = tmp_path / "physical-home"
    data_home = physical_home / ".kiro" / "crew"
    data_home.mkdir(parents=True)
    (data_home / "config.json").write_text(json.dumps(_CONFIG), encoding="utf-8")
    monkeypatch.setenv("KIROCREW_HOME", str(data_home))
    monkeypatch.setattr(paths, "_resolved_home", None)
    monkeypatch.setattr(paths, "_config_dir_memo", None)
    store = VectorMemoryStore()
    try:
        store.init()
        store.set_semantic("project.keep", "retained", 1.0, "user_explicit")
        backup = mb.backup_store(store._db_path)
        assert backup is not None
        mb.restore_from_backup(backup)
    finally:
        store.close()
    out = data_home / "backups"
    pending = out / mb._V1_PENDING
    staged = out / json.loads(pending.read_text())["stage"]
    bad = b"unreadable V1 restore journal"
    pending.write_bytes(bad)
    before = (data_home / "memory.db").read_bytes()
    backup_before, stage_before = backup.read_bytes(), staged.read_bytes()

    alias_home = tmp_path / "linked-home"
    make_dir_link(alias_home, physical_home)
    monkeypatch.setenv("HOME", str(alias_home))
    monkeypatch.setenv("USERPROFILE", str(alias_home))
    monkeypatch.delenv("KIROCREW_HOME")
    monkeypatch.setattr(paths, "_resolved_home", None)
    monkeypatch.setattr(paths, "_config_dir_memo", None)
    path = resolve_store_path(DEFAULT_MEMORY_STORE)
    assert path == alias_home / ".kiro" / "crew" / "memory.db"
    assert path.resolve() != path

    assert mb.cancel_pending_restore(path)

    quarantined = list(out.glob("cancelled-v1-restore-*.json"))
    assert len(quarantined) == 1 and quarantined[0].read_bytes() == bad
    assert not pending.exists()
    assert path.read_bytes() == before
    assert backup.read_bytes() == backup_before and staged.read_bytes() == stage_before
    assert _integrity(path) == "ok" and _rows(path) == 1


@pytest.mark.parametrize("redirected", ["database", "journal"])
def test_bad_v1_journal_cancellation_refuses_redirected_leaves(live_store, redirected):
    path = live_store._db_path
    backup = mb.backup_store(path)
    assert backup is not None
    mb.restore_from_backup(backup)
    live_store.close()
    out = mb.backup_dir_for(path)
    pending = out / mb._V1_PENDING
    staged = out / json.loads(pending.read_text())["stage"]
    bad = b"unreadable V1 restore journal"
    pending.write_bytes(bad)
    leaf = path if redirected == "database" else pending
    preserved = leaf.with_name("preserved-" + leaf.name)
    leaf.rename(preserved)
    before = preserved.read_bytes()
    if os.name == "nt":
        # A junction exercises leaf redirection without Windows symlink privileges.
        target = out / "redirect-target"
        target.mkdir()
        make_dir_link(leaf, target)
    else:
        leaf.symlink_to(preserved)

    with pytest.raises(ValueError, match="outside its canonical store"):
        mb.cancel_pending_restore(path)

    assert preserved.read_bytes() == before
    assert not list(out.glob("cancelled-v1-restore-*.json"))
    assert backup.exists() and staged.exists()
    if redirected == "database":
        assert pending.read_bytes() == bad
        assert _rows(preserved) == 20
    else:
        assert _rows(path) == 20


@pytest.mark.parametrize("ambiguous", ["missing_current", "prior_copy"])
def test_bad_v1_journal_never_replaces_ambiguous_state_with_empty_memory(live_store, ambiguous):
    path = live_store._db_path
    backup = mb.backup_store(path)
    mb.restore_from_backup(backup)
    out = mb.backup_dir_for(path)
    pending = out / mb._V1_PENDING
    bad = b"unreadable V1 restore journal"
    pending.write_bytes(bad)
    live_store.close()
    if ambiguous == "missing_current":
        path.unlink()
    else:
        path.with_name(path.name + ".superseded.example-wal").write_bytes(b"previous WAL")
    status = mb.pending_restore_status(path)
    assert status["pending"] and status["recovery"]["staged_copies"]
    with pytest.raises(ValueError):
        mb.cancel_pending_restore(path)
    assert pending.read_bytes() == bad
    assert not list(out.glob("cancelled-v1-restore-*.json"))
    assert backup.exists()
    if ambiguous == "missing_current":
        assert not path.exists()
