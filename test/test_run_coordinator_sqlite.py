"""SQLite-specific durability and initialization tests."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

import kiro_crew.executors as executors_module
import kiro_crew.run_coordinator.sqlite as sqlite_module
from kiro_crew.run_coordinator import (
    CommandOperation,
    CoordinatorDecision,
    MemoryRunCoordinator,
    ShadowRunCoordinator,
    SQLiteRunCoordinator,
    SubmitRun,
)


def _request(
    *,
    run_id: str = "run-1",
    command_id: str = "command-1",
    idempotency_key: str = "key-1",
) -> SubmitRun:
    return SubmitRun(
        run_id=run_id,
        command_id=command_id,
        idempotency_key=idempotency_key,
        payload_json='{"task":"compare","version":1}',
        payload_hash="hash-1",
        parent_session="dashboard:parent",
        agent="researcher",
        task="compare",
        conversation_key="",
        operation=CommandOperation.SPAWN,
        accepted=True,
        rejection_reason="",
    )


@pytest.mark.asyncio
async def test_sqlite_reopen_preserves_idempotent_submission(tmp_path: Path) -> None:
    path = tmp_path / "coordinator.db"
    created = await SQLiteRunCoordinator(path).submit(_request())
    replay = await SQLiteRunCoordinator(path).submit(_request())

    assert created.decision is CoordinatorDecision.APPLIED
    assert replay.decision is CoordinatorDecision.UNCHANGED
    assert replay.value is not None
    assert replay.value.run.run_id == "run-1"
    assert replay.value.command.payload_json == _request().payload_json

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT payload_json FROM commands WHERE command_id = 'command-1'"
        ).fetchone() == (_request().payload_json,)


@pytest.mark.asyncio
async def test_sqlite_schema_enables_durability_pragmas(tmp_path: Path) -> None:
    path = tmp_path / "coordinator.db"
    coordinator = SQLiteRunCoordinator(path)
    await coordinator.get_run("missing")

    with sqlite3.connect(path) as connection:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()

    def inspect_owned_connection() -> tuple[object, object, object, object]:
        connection, owned_path = coordinator._connect()
        try:
            return (
                connection.execute("PRAGMA journal_mode").fetchone()[0],
                connection.execute("PRAGMA synchronous").fetchone()[0],
                connection.execute("PRAGMA foreign_keys").fetchone()[0],
                connection.execute("PRAGMA busy_timeout").fetchone()[0],
            )
        finally:
            connection.close()
            coordinator._secure_existing_database_files(owned_path)

    journal_mode, synchronous, foreign_keys, busy_timeout = await asyncio.to_thread(
        inspect_owned_connection
    )

    assert version == ("3",)
    assert journal_mode == "wal"
    assert synchronous == 2
    assert foreign_keys == 1
    assert busy_timeout == 5000


@pytest.mark.asyncio
async def test_sqlite_refuses_newer_schema_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "coordinator.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO metadata VALUES ('schema_version', '999')")
        journal_mode_before = connection.execute("PRAGMA journal_mode").fetchone()
    files_before = {candidate.name for candidate in tmp_path.iterdir()}

    coordinator = SQLiteRunCoordinator(path)
    with pytest.raises(RuntimeError, match="newer schema"):
        await coordinator.get_run("missing")

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone() == ("999",)
        assert connection.execute("PRAGMA journal_mode").fetchone() == journal_mode_before
    assert {candidate.name for candidate in tmp_path.iterdir()} == files_before


@pytest.mark.asyncio
async def test_concurrent_same_key_submission_has_one_creator(tmp_path: Path) -> None:
    path = tmp_path / "coordinator.db"

    results = await asyncio.gather(
        *(SQLiteRunCoordinator(path).submit(_request()) for _ in range(8))
    )

    assert sum(result.decision is CoordinatorDecision.APPLIED for result in results) == 1
    assert sum(result.decision is CoordinatorDecision.UNCHANGED for result in results) == 7


@pytest.mark.asyncio
async def test_default_path_resolution_and_io_run_off_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew-home"))
    event_loop_thread = threading.get_ident()
    resolving_threads: list[int] = []

    def resolved_coordinator_dir() -> Path:
        resolving_threads.append(threading.get_ident())
        return tmp_path / "crew-home/run-coordinator"

    with patch(
        "kiro_crew.run_coordinator.sqlite.canonical_run_coordinator_dir",
        side_effect=resolved_coordinator_dir,
    ):
        coordinator = SQLiteRunCoordinator()
        await coordinator.get_run("missing")

    assert resolving_threads
    assert all(thread_id != event_loop_thread for thread_id in resolving_threads)
    assert (tmp_path / "crew-home/run-coordinator/coordinator.db").exists()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX mode assertion")
async def test_database_and_directory_are_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "private/run-coordinator/coordinator.db"
    await SQLiteRunCoordinator(path).get_run("missing")

    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_corrupt_database_is_refused_without_deletion(tmp_path: Path) -> None:
    path = tmp_path / "coordinator.db"
    corrupt = b"not a sqlite database"
    path.write_bytes(corrupt)

    with pytest.raises(sqlite3.DatabaseError):
        await SQLiteRunCoordinator(path).get_run("missing")

    assert path.read_bytes() == corrupt


@pytest.mark.asyncio
async def test_v1_database_migrates_to_v3_once(tmp_path: Path) -> None:
    path = tmp_path / "coordinator.db"
    coordinator = SQLiteRunCoordinator(path)
    await coordinator.submit(_request())

    # Rebuild the commands table in the exact v1 shape, then mark the database
    # as v1. This models a store created by the prior schema without depending
    # on private production DDL in the test.
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("""
            CREATE TABLE commands_v1 (
                command_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                operation TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                owner_id TEXT NOT NULL,
                lease_epoch INTEGER NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                rejection_reason TEXT NOT NULL
            )
            """)
        connection.execute("""
            INSERT INTO commands_v1
            SELECT command_id, idempotency_key, run_id, operation,
                   payload_hash, status, attempt, owner_id, lease_epoch,
                   created_at, updated_at, rejection_reason
            FROM commands
            """)
        connection.execute("DROP TABLE commands")
        connection.execute("ALTER TABLE commands_v1 RENAME TO commands")
        connection.execute("UPDATE metadata SET value = '1' WHERE key = 'schema_version'")
        connection.execute("DELETE FROM metadata WHERE key = 'migration.2.applied_at'")
        connection.execute("DELETE FROM metadata WHERE key = 'migration.3.applied_at'")

    migrated = await SQLiteRunCoordinator(path).submit(_request())
    reopened = await SQLiteRunCoordinator(path).submit(_request())

    assert migrated.value is not None
    assert migrated.value.command.payload_json == ""
    assert reopened.value is not None
    assert reopened.value.command.payload_json == ""
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone() == ("3",)
        assert connection.execute(
            "SELECT COUNT(*) FROM metadata WHERE key = 'migration.2.applied_at'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM metadata WHERE key = 'migration.3.applied_at'"
        ).fetchone() == (1,)
        command_columns = {row[1] for row in connection.execute("PRAGMA table_info(commands)")}
        assert {"claim_expires_at", "claim_epoch", "result_json"} <= command_columns
        assert connection.execute("PRAGMA foreign_key_list(commands)").fetchall() == []


@pytest.mark.asyncio
async def test_missing_schema_metadata_is_recovered_from_physical_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "coordinator.db"
    coordinator = SQLiteRunCoordinator(path)
    created = await coordinator.submit(_request())
    assert created.value is not None
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM metadata WHERE key = 'schema_version'")

    replay = await SQLiteRunCoordinator(path).submit(_request())

    assert replay.decision is CoordinatorDecision.UNCHANGED
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone() == (str(sqlite_module._SCHEMA_VERSION),)


@pytest.mark.asyncio
async def test_failed_migration_rolls_back_and_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "coordinator.db"
    await SQLiteRunCoordinator(path).get_run("missing")
    migrations = sqlite_module._MIGRATIONS
    monkeypatch.setattr(sqlite_module, "_SCHEMA_VERSION", 4)
    monkeypatch.setattr(
        sqlite_module,
        "_MIGRATIONS",
        migrations
        + (
            (
                4,
                (
                    "CREATE TABLE migration_probe (value TEXT NOT NULL)",
                    "THIS IS NOT SQL",
                ),
            ),
        ),
    )

    with pytest.raises(sqlite3.DatabaseError):
        await SQLiteRunCoordinator(path).get_run("missing")

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone() == ("3",)
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master "  # wokeignore:rule=master
                "WHERE name = 'migration_probe'"
            ).fetchone()
            is None
        )

    monkeypatch.setattr(
        sqlite_module,
        "_MIGRATIONS",
        migrations + ((4, ("CREATE TABLE migration_probe (value TEXT NOT NULL)",)),),
    )
    await SQLiteRunCoordinator(path).get_run("missing")
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone() == ("4",)
        assert connection.execute(
            "SELECT name FROM sqlite_master "  # wokeignore:rule=master
            "WHERE name = 'migration_probe'"
        ).fetchone() == ("migration_probe",)


@pytest.mark.asyncio
async def test_existing_database_files_are_restricted_before_sqlite_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "run-coordinator/coordinator.db"
    path.parent.mkdir()
    candidates = [path, Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal")]
    for candidate in candidates:
        candidate.touch()

    restricted: set[Path] = set()
    real_restrict = sqlite_module.restrict_to_owner
    real_connect = sqlite_module.sqlite3.connect

    def record_restriction(candidate: str | Path) -> None:
        restricted.add(Path(candidate))
        real_restrict(candidate)

    def checked_connect(*args, **kwargs):
        assert set(candidates) <= restricted
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite_module, "restrict_to_owner", record_restriction)
    monkeypatch.setattr(sqlite_module.sqlite3, "connect", checked_connect)

    await SQLiteRunCoordinator(path).get_run("missing")


def test_vanished_sqlite_sidecar_does_not_break_concurrent_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "coordinator.db"
    sidecar = Path(f"{path}-wal")
    sidecar.touch()

    def vanish_then_fail(candidate: str | Path) -> None:
        candidate = Path(candidate)
        if candidate == sidecar:
            candidate.unlink()
            raise OSError("sidecar vanished")
        sqlite_module.restrict_to_owner(candidate)

    monkeypatch.setattr(sqlite_module, "restrict_to_owner", vanish_then_fail)

    SQLiteRunCoordinator._secure_existing_database_files(path)


@pytest.mark.asyncio
async def test_sidecar_link_is_rejected_before_sqlite_opens(tmp_path: Path) -> None:
    path = tmp_path / "run-coordinator/coordinator.db"
    path.parent.mkdir()
    target = tmp_path / "outside"
    target.write_text("do not touch", encoding="utf-8")
    sidecar = Path(f"{path}-wal")
    try:
        sidecar.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("file symlink creation is unavailable")

    with pytest.raises(OSError, match="cannot be a link"):
        await SQLiteRunCoordinator(path).get_run("missing")

    assert target.read_text(encoding="utf-8") == "do not touch"


@pytest.mark.asyncio
async def test_failed_write_transaction_leaves_prior_rows_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "coordinator.db"
    coordinator = SQLiteRunCoordinator(path)
    await coordinator.submit(_request())
    real_save = coordinator._save_memory

    def fail_after_writes(connection, memory) -> None:
        real_save(connection, memory)
        raise sqlite3.OperationalError("injected write failure")

    monkeypatch.setattr(coordinator, "_save_memory", fail_after_writes)
    with pytest.raises(sqlite3.OperationalError, match="injected write failure"):
        await coordinator.submit(
            _request(run_id="run-2", command_id="command-2", idempotency_key="key-2")
        )

    assert await SQLiteRunCoordinator(path).get_run("run-1") is not None
    assert await SQLiteRunCoordinator(path).get_run("run-2") is None


@pytest.mark.asyncio
async def test_acl_failure_rolls_back_uncommitted_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "coordinator.db"
    coordinator = SQLiteRunCoordinator(path)
    real_secure = coordinator._secure_existing_database_files
    secure_calls = 0

    def fail_final_acl_check(owned_path: Path) -> None:
        nonlocal secure_calls
        secure_calls += 1
        if secure_calls == 2:
            raise OSError("injected ACL failure")
        real_secure(owned_path)

    monkeypatch.setattr(coordinator, "_secure_existing_database_files", fail_final_acl_check)
    with pytest.raises(OSError, match="injected ACL failure"):
        await coordinator.submit(_request())

    assert secure_calls == 2
    assert await SQLiteRunCoordinator(path).get_run("run-1") is None


@pytest.mark.asyncio
async def test_sqlite_worker_itself_runs_off_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = SQLiteRunCoordinator(tmp_path / "coordinator.db")
    event_loop_thread = threading.get_ident()
    worker_threads: list[int] = []
    real_invoke = coordinator._invoke

    def record_worker(*args, **kwargs):
        worker_threads.append(threading.get_ident())
        return real_invoke(*args, **kwargs)

    monkeypatch.setattr(coordinator, "_invoke", record_worker)
    await coordinator.get_run("missing")

    assert worker_threads
    assert all(thread_id != event_loop_thread for thread_id in worker_threads)
    assert all(
        thread.name.startswith("mc-coordinator")
        for thread in threading.enumerate()
        if thread.ident in worker_threads
    )


@pytest.mark.asyncio
async def test_locked_shadow_burst_uses_bounded_pool_without_starving_default_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "coordinator.db"
    observer = SQLiteRunCoordinator(path)
    await observer.get_run("missing")
    monkeypatch.setattr(sqlite_module, "_BUSY_TIMEOUT_MS", 1000)

    blocker = sqlite3.connect(path)
    blocker.execute("BEGIN IMMEDIATE")
    active = 0
    peak_active = 0
    lock = threading.Lock()
    entered = threading.Event()
    timed_out = 0
    real_invoke = observer._invoke

    def tracked_invoke(*args, **kwargs):
        nonlocal active, peak_active, timed_out
        with lock:
            active += 1
            peak_active = max(peak_active, active)
            entered.set()
        try:
            return real_invoke(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc):
                with lock:
                    timed_out += 1
            raise
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(observer, "_invoke", tracked_invoke)
    shadow = ShadowRunCoordinator(MemoryRunCoordinator(), observer)
    burst_size = executors_module._MAX_COORDINATOR_WORKERS + 2
    loop = asyncio.get_running_loop()
    default_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-default")
    loop.set_default_executor(default_pool)

    tasks = [
        asyncio.create_task(
            shadow.submit(
                _request(
                    run_id=f"run-{index}",
                    command_id=f"command-{index}",
                    idempotency_key=f"key-{index}",
                )
            )
        )
        for index in range(burst_size)
    ]
    try:
        for _ in range(300):
            if entered.is_set() and peak_active == executors_module._MAX_COORDINATOR_WORKERS:
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        assert peak_active == executors_module._MAX_COORDINATOR_WORKERS

        probe_thread = await asyncio.wait_for(
            asyncio.to_thread(lambda: threading.current_thread().name), timeout=0.5
        )
        assert probe_thread.startswith("test-default")
        await asyncio.gather(*tasks)
    finally:
        blocker.rollback()
        blocker.close()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        default_pool.shutdown(wait=True, cancel_futures=True)

    assert timed_out == burst_size
    assert peak_active <= executors_module._MAX_COORDINATOR_WORKERS
