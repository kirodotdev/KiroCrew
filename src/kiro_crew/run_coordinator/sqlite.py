"""SQLite-backed implementation of the run coordinator contract."""

from __future__ import annotations

import asyncio
import functools
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kiro_crew.executors import coordinator_executor
from kiro_crew.platform_compat import (
    is_link_or_junction,
    make_owner_only_dir,
    restrict_dir_to_owner,
    restrict_to_owner,
)
from kiro_crew.run_coordinator_anchor import canonical_run_coordinator_dir

from .memory import MemoryRunCoordinator
from .models import (
    CommandClaim,
    CommandFence,
    CommandOperation,
    CommandReceipt,
    CommandStatus,
    CoordinatorResult,
    DeliveryFence,
    DeliveryState,
    DesiredState,
    ObservedState,
    OutboxEvent,
    OwnerLease,
    RunCommand,
    RunCompletion,
    RunFence,
    RunOutcome,
    RunRecord,
    SubmitControl,
    SubmitReceipt,
    SubmitRun,
)

_SCHEMA_VERSION = 3
_BUSY_TIMEOUT_MS = 5000
_JOURNAL_MODE_LOCK = threading.Lock()
_DATABASE_NAME = "coordinator.db"

_SCHEMA_V1 = (
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        parent_session TEXT NOT NULL,
        agent TEXT NOT NULL,
        task TEXT NOT NULL,
        conversation_key TEXT NOT NULL,
        desired_state TEXT NOT NULL,
        observed_state TEXT NOT NULL,
        outcome TEXT,
        result_path TEXT NOT NULL,
        error TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        version INTEGER NOT NULL,
        owner_id TEXT NOT NULL,
        lease_expires_at REAL NOT NULL,
        lease_epoch INTEGER NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        terminal_at REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS commands (
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
    """,
    """
    CREATE TABLE IF NOT EXISTS outbox (
        event_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        run_version INTEGER NOT NULL,
        destination TEXT NOT NULL,
        event_type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL,
        available_at REAL NOT NULL,
        claim_owner TEXT NOT NULL,
        claim_expires_at REAL NOT NULL,
        claim_epoch INTEGER NOT NULL,
        created_at REAL NOT NULL,
        delivered_at REAL,
        UNIQUE(run_id, event_type)
    )
    """,
    "CREATE INDEX IF NOT EXISTS runs_state_lease ON runs(observed_state, lease_expires_at)",
    "CREATE INDEX IF NOT EXISTS commands_status_created ON commands(status, created_at)",
    "CREATE INDEX IF NOT EXISTS commands_run_created ON commands(run_id, created_at)",
    "CREATE INDEX IF NOT EXISTS outbox_status_available ON outbox(status, available_at, created_at)",
    "CREATE INDEX IF NOT EXISTS outbox_run ON outbox(run_id)",
)

_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (1, _SCHEMA_V1),
    (
        2,
        ("ALTER TABLE commands ADD COLUMN payload_json TEXT NOT NULL DEFAULT ''",),
    ),
    (
        3,
        (
            "ALTER TABLE commands RENAME TO commands_v2",
            """
            CREATE TABLE commands (
                command_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                owner_id TEXT NOT NULL,
                lease_epoch INTEGER NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                rejection_reason TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                claim_expires_at REAL NOT NULL,
                claim_epoch INTEGER NOT NULL,
                result_json TEXT NOT NULL
            )
            """,
            """
            INSERT INTO commands (
                command_id, idempotency_key, run_id, operation, payload_hash,
                status, attempt, owner_id, lease_epoch, created_at, updated_at,
                rejection_reason, payload_json, claim_expires_at, claim_epoch,
                result_json
            )
            SELECT command_id, idempotency_key, run_id, operation, payload_hash,
                   status, attempt, owner_id, lease_epoch, created_at, updated_at,
                   rejection_reason, payload_json, 0.0, 0, ''
            FROM commands_v2
            """,
            "DROP TABLE commands_v2",
            "CREATE INDEX commands_status_created ON commands(status, created_at)",
            "CREATE INDEX commands_run_created ON commands(run_id, created_at)",
        ),
    ),
)


class SQLiteRunCoordinator:
    """Durable coordinator using short transactions on fresh connections."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._path = Path(path) if path is not None else None
        self._resolved_path: Path | None = None
        self._path_resolution_lock = threading.Lock()
        self._clock = clock
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)

    def _database_path(self) -> Path:
        with self._path_resolution_lock:
            if self._resolved_path is not None:
                return self._resolved_path
            configured = (
                self._path
                if self._path is not None
                else canonical_run_coordinator_dir() / _DATABASE_NAME
            )
            if is_link_or_junction(configured.parent) or is_link_or_junction(configured):
                raise OSError("run coordinator database path cannot be a link")
            self._resolved_path = configured.resolve(strict=False)
            return self._resolved_path

    def _prepare_path(self) -> Path:
        path = self._database_path()
        parent = path.parent
        if is_link_or_junction(parent):
            raise OSError("run coordinator database path cannot be a link")
        make_owner_only_dir(parent)
        if is_link_or_junction(parent):
            raise OSError("run coordinator database path cannot be a link")
        restrict_dir_to_owner(parent)
        self._secure_existing_database_files(path)
        return path

    @staticmethod
    def _database_files(path: Path) -> tuple[Path, ...]:
        return (
            path,
            Path(f"{path}-wal"),
            Path(f"{path}-shm"),
            Path(f"{path}-journal"),
        )

    @classmethod
    def _secure_existing_database_files(cls, path: Path) -> None:
        for candidate in cls._database_files(path):
            if is_link_or_junction(candidate):
                raise OSError("run coordinator database path cannot be a link")
            if candidate.exists():
                try:
                    restrict_to_owner(candidate)
                except OSError:
                    # SQLite creates and removes WAL/journal sidecars while
                    # concurrent connections prepare the same database.  A
                    # vanished sidecar needs no ACL; a surviving file or the
                    # primary database must still fail closed.
                    if candidate == path or candidate.exists():
                        raise

    @staticmethod
    def _configure(connection: sqlite3.Connection) -> None:
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        # Changing journal mode takes a database-wide lock and does not reliably
        # honor busy_timeout when fresh in-process connections race to enable WAL.
        with _JOURNAL_MODE_LOCK:
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                connection.execute("PRAGMA journal_mode=WAL")
        connection.row_factory = sqlite3.Row

    def _connect(self) -> tuple[sqlite3.Connection, Path]:
        path = self._prepare_path()
        connection = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_MS / 1000)
        try:
            self._refuse_newer_schema(connection)
            self._configure(connection)
            restrict_to_owner(path)
        except BaseException:
            connection.close()
            raise
        return connection, path

    @staticmethod
    def _table_columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
        return frozenset(str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})"))

    @classmethod
    def _physical_schema_version(cls, connection: sqlite3.Connection) -> int:
        """Infer a shipped schema when only its metadata row was lost."""

        runs = cls._table_columns(connection, "runs")
        commands = cls._table_columns(connection, "commands")
        outbox = cls._table_columns(connection, "outbox")
        if not runs and not commands and not outbox:
            return 0
        if not runs or not commands or not outbox:
            raise RuntimeError("run coordinator database has an incomplete schema")
        if {"process_id", "process_start_id", "process_owned"} <= runs:
            return 5
        if "source_version" in runs:
            return 4
        if {"claim_expires_at", "claim_epoch", "result_json"} <= commands:
            return 3
        if "payload_json" in commands:
            return 2
        return 1

    @classmethod
    def _schema_version(cls, connection: sqlite3.Connection) -> int:
        metadata = connection.execute(
            "SELECT 1 FROM sqlite_master "  # wokeignore:rule=master
            "WHERE type = 'table' AND name = 'metadata'"
        ).fetchone()
        if metadata is None:
            return cls._physical_schema_version(connection)
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        return int(row[0]) if row is not None else cls._physical_schema_version(connection)

    @classmethod
    def _refuse_newer_schema(cls, connection: sqlite3.Connection) -> None:
        if cls._schema_version(connection) > _SCHEMA_VERSION:
            raise RuntimeError("run coordinator database has a newer schema")

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        current_version = self._schema_version(connection)
        if current_version > _SCHEMA_VERSION:
            raise RuntimeError("run coordinator database has a newer schema")

        versions = tuple(version for version, _statements in _MIGRATIONS)
        if versions != tuple(range(1, _SCHEMA_VERSION + 1)):
            raise RuntimeError("run coordinator migration catalog is not contiguous")

        if current_version:
            connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES ('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(current_version),),
            )

        for version, statements in _MIGRATIONS:
            if version <= current_version:
                continue
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES ('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(version),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES (?, ?)",
                (f"migration.{version}.applied_at", str(self._clock())),
            )

    def _load_memory(self, connection: sqlite3.Connection) -> MemoryRunCoordinator:
        memory = MemoryRunCoordinator(clock=self._clock, id_factory=self._id_factory)
        for row in connection.execute("SELECT * FROM runs"):
            record = RunRecord(
                run_id=row["run_id"],
                parent_session=row["parent_session"],
                agent=row["agent"],
                task=row["task"],
                conversation_key=row["conversation_key"],
                desired_state=DesiredState(row["desired_state"]),
                observed_state=ObservedState(row["observed_state"]),
                outcome=RunOutcome(row["outcome"]) if row["outcome"] is not None else None,
                result_path=row["result_path"],
                error=row["error"],
                attempt=row["attempt"],
                version=row["version"],
                owner_id=row["owner_id"],
                lease_expires_at=row["lease_expires_at"],
                lease_epoch=row["lease_epoch"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                terminal_at=row["terminal_at"],
            )
            memory._runs[record.run_id] = record
        for row in connection.execute("SELECT * FROM commands"):
            command = RunCommand(
                command_id=row["command_id"],
                idempotency_key=row["idempotency_key"],
                run_id=row["run_id"],
                operation=CommandOperation(row["operation"]),
                payload_hash=row["payload_hash"],
                status=CommandStatus(row["status"]),
                attempt=row["attempt"],
                owner_id=row["owner_id"],
                lease_epoch=row["lease_epoch"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                rejection_reason=row["rejection_reason"],
                payload_json=row["payload_json"],
                claim_expires_at=row["claim_expires_at"],
                claim_epoch=row["claim_epoch"],
                result_json=row["result_json"],
            )
            memory._commands[command.command_id] = command
            memory._command_by_key[command.idempotency_key] = command.command_id
        for row in connection.execute("SELECT * FROM outbox"):
            event = OutboxEvent(
                event_id=row["event_id"],
                run_id=row["run_id"],
                run_version=row["run_version"],
                destination=row["destination"],
                event_type=row["event_type"],
                payload_json=row["payload_json"],
                status=DeliveryState(row["status"]),
                attempts=row["attempts"],
                available_at=row["available_at"],
                claim_owner=row["claim_owner"],
                claim_expires_at=row["claim_expires_at"],
                claim_epoch=row["claim_epoch"],
                created_at=row["created_at"],
                delivered_at=row["delivered_at"],
            )
            memory._outbox[event.event_id] = event
            memory._outbox_by_run_type[(event.run_id, event.event_type)] = event.event_id
        return memory

    @staticmethod
    def _save_memory(connection: sqlite3.Connection, memory: MemoryRunCoordinator) -> None:
        connection.execute("DELETE FROM outbox")
        connection.execute("DELETE FROM commands")
        connection.execute("DELETE FROM runs")
        connection.executemany(
            """
            INSERT INTO runs VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            [
                (
                    run.run_id,
                    run.parent_session,
                    run.agent,
                    run.task,
                    run.conversation_key,
                    run.desired_state.value,
                    run.observed_state.value,
                    run.outcome.value if run.outcome is not None else None,
                    run.result_path,
                    run.error,
                    run.attempt,
                    run.version,
                    run.owner_id,
                    run.lease_expires_at,
                    run.lease_epoch,
                    run.created_at,
                    run.updated_at,
                    run.terminal_at,
                )
                for run in memory._runs.values()
            ],
        )
        connection.executemany(
            """
            INSERT INTO commands (
                command_id, idempotency_key, run_id, operation, payload_hash,
                status, attempt, owner_id, lease_epoch, created_at, updated_at,
                rejection_reason, payload_json, claim_expires_at, claim_epoch,
                result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    command.command_id,
                    command.idempotency_key,
                    command.run_id,
                    command.operation.value,
                    command.payload_hash,
                    command.status.value,
                    command.attempt,
                    command.owner_id,
                    command.lease_epoch,
                    command.created_at,
                    command.updated_at,
                    command.rejection_reason,
                    command.payload_json,
                    command.claim_expires_at,
                    command.claim_epoch,
                    command.result_json,
                )
                for command in memory._commands.values()
            ],
        )
        connection.executemany(
            """
            INSERT INTO outbox (
                event_id, run_id, run_version, destination, event_type,
                payload_json, status, attempts, available_at, claim_owner,
                claim_expires_at, claim_epoch, created_at, delivered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    event.event_id,
                    event.run_id,
                    event.run_version,
                    event.destination,
                    event.event_type,
                    event.payload_json,
                    event.status.value,
                    event.attempts,
                    event.available_at,
                    event.claim_owner,
                    event.claim_expires_at,
                    event.claim_epoch,
                    event.created_at,
                    event.delivered_at,
                )
                for event in memory._outbox.values()
            ],
        )

    def _invoke(self, method: str, *args: Any, persist: bool = True) -> Any:
        connection, path = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_schema(connection)
            memory = self._load_memory(connection)
            result = asyncio.run(getattr(memory, method)(*args))
            if persist:
                self._save_memory(connection, memory)
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("run coordinator database integrity check failed")
            self._secure_existing_database_files(path)
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def _offload(self, method: str, *args: Any, persist: bool = True) -> Any:
        loop = asyncio.get_running_loop()
        call = functools.partial(self._invoke, method, *args, persist=persist)
        return await loop.run_in_executor(coordinator_executor(), call)

    async def submit(self, request: SubmitRun) -> CoordinatorResult[SubmitReceipt]:
        return await self._offload("submit", request)

    async def submit_control(self, request: SubmitControl) -> CoordinatorResult[CommandReceipt]:
        return await self._offload("submit_control", request)

    async def get_command_by_key(self, idempotency_key: str) -> CommandReceipt | None:
        return await self._offload("get_command_by_key", idempotency_key, persist=False)

    async def claim_commands(self, owner: OwnerLease, limit: int) -> list[CommandClaim]:
        return await self._offload("claim_commands", owner, limit)

    async def claim_controls(
        self, owner: OwnerLease, limit: int, command_id: str = ""
    ) -> list[CommandClaim]:
        return await self._offload("claim_controls", owner, limit, command_id)

    async def claim_command(self, command_id: str, owner: OwnerLease) -> CommandClaim | None:
        return await self._offload("claim_command", command_id, owner)

    async def finish_command(
        self,
        fence: CommandFence,
        status: CommandStatus,
        rejection_reason: str = "",
        result_json: str = "",
    ) -> CoordinatorResult[RunCommand]:
        return await self._offload(
            "finish_command",
            fence,
            status,
            rejection_reason,
            result_json,
        )

    async def finish_control(
        self,
        fence: CommandFence,
        status: CommandStatus,
        rejection_reason: str = "",
        result_json: str = "",
    ) -> CoordinatorResult[RunCommand]:
        return await self._offload(
            "finish_control",
            fence,
            status,
            rejection_reason,
            result_json,
        )

    async def mark_starting(
        self, command: RunCommand, fence: RunFence, expected_version: int
    ) -> CoordinatorResult[RunRecord]:
        return await self._offload("mark_starting", command, fence, expected_version)

    async def mark_running(
        self, run_id: str, fence: RunFence, expected_version: int
    ) -> CoordinatorResult[RunRecord]:
        return await self._offload("mark_running", run_id, fence, expected_version)

    async def complete(
        self, completion: RunCompletion, fence: RunFence, expected_version: int
    ) -> CoordinatorResult[OutboxEvent]:
        return await self._offload("complete", completion, fence, expected_version)

    async def renew(self, run_id: str, fence: RunFence, until: float) -> bool:
        return await self._offload("renew", run_id, fence, until)

    async def claim_outbox(self, owner: OwnerLease, limit: int) -> list[OutboxEvent]:
        return await self._offload("claim_outbox", owner, limit)

    async def release_outbox(
        self, fence: DeliveryFence, available_at: float
    ) -> CoordinatorResult[OutboxEvent]:
        return await self._offload("release_outbox", fence, available_at)

    async def mark_delivered(self, fence: DeliveryFence) -> CoordinatorResult[OutboxEvent]:
        return await self._offload("mark_delivered", fence)

    async def get_run(self, run_id: str) -> RunRecord | None:
        return await self._offload("get_run", run_id, persist=False)
