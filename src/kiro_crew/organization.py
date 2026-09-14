"""Gateway-owned organization, staffing and delegation records.

The database is hidden from agent processes. Callers of this module are trusted
gateway code: HTTP handlers resolve the actor from a protected private-memory
binding, never from an argument supplied by an agent. Every mutation is one
SQLite transaction, including capacity checks and its audit event.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

OWNER = "owner"
ORGANIZATION_AGENT_PREFIX = "kirocrew-org-"
_SCHEMA_VERSION = 1
ROLES = ("conductor", "manager", "engineer", "researcher")
COORDINATORS = frozenset(("conductor", "manager"))
OPEN_TASK_STATES = ("queued", "working", "blocked", "review")
TERMINAL_TASK_STATES = ("accepted", "cancelled")

# Capability summaries for authenticated org_inbox LLM data. organization_policy.role_spec
# compiles the enforced tool ceilings, composed with existing governance.
ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "conductor": ("team", "delegate", "review", "message", "memory", "read"),
    "manager": ("team", "delegate", "review", "message", "memory", "read"),
    "engineer": ("team", "message", "memory", "read", "write", "execute"),
    "researcher": ("team", "message", "memory", "read", "research"),
}
DEFAULT_STAFFING: dict[str, dict[str, int]] = {
    "conductor": {"manager": 2, "researcher": 2},
    "manager": {"engineer": 3, "researcher": 1},
    "engineer": {},
    "researcher": {},
}


class OrganizationError(ValueError):
    """A stable, actionable refusal suitable for an API response."""

    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.status = status


def _text(value: Any, field: str, *, maximum: int = 12000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise OrganizationError("invalid_value", f"{field} must be non-empty text.", 400)
    return value.strip()


def _integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise OrganizationError(
            "invalid_value", f"{field} must be between {minimum} and {maximum}.", 400
        )
    return value


def _id() -> str:
    return uuid.uuid4().hex


def organization_path() -> Path:
    from kiro_crew.config.paths import config_dir

    return config_dir() / "organizations" / "organization.sqlite3"


class OrganizationStore:
    """One personal organization with immutable member and assignment IDs.

    Construct per operation; no process-global caller state or open connection.
    The path can be supplied for isolated tests and gateway lifecycle ownership.
    """

    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else organization_path()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, _SCHEMA_VERSION):
                raise OrganizationError(
                    "unsupported_schema_version",
                    f"Organization schema version {version} is not supported "
                    f"(expected {_SCHEMA_VERSION}). Open it with a compatible Kiro Crew version.",
                )
            if version == 0:
                # Adopt the current unversioned prototype without rewriting records.
                # Execute fixed DDL separately: executescript commits a pending transaction.
                for statement in """
                CREATE TABLE IF NOT EXISTS settings (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    revision INTEGER NOT NULL, concurrency INTEGER NOT NULL,
                    enabled INTEGER NOT NULL, staffing TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS members (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                    memory_store TEXT NOT NULL UNIQUE, role TEXT NOT NULL,
                    manager_id TEXT REFERENCES members(id),
                    state TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, parent_id TEXT REFERENCES tasks(id),
                    sender TEXT NOT NULL, recipient TEXT NOT NULL REFERENCES members(id),
                    title TEXT NOT NULL, acceptance TEXT NOT NULL,
                    state TEXT NOT NULL, report TEXT NOT NULL DEFAULT '',
                    decision TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
                    updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, sender TEXT NOT NULL,
                    recipient TEXT NOT NULL, text TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS owner_chat_tasks (
                    request_id TEXT PRIMARY KEY,
                    member_id TEXT NOT NULL REFERENCES members(id),
                    task_id TEXT NOT NULL REFERENCES tasks(id)
                );
                CREATE TABLE IF NOT EXISTS hiring (
                    id TEXT PRIMARY KEY, manager_id TEXT NOT NULL REFERENCES members(id),
                    role TEXT NOT NULL, state TEXT NOT NULL, member_id TEXT,
                    created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, member_id TEXT NOT NULL REFERENCES members(id),
                    state TEXT NOT NULL, reason TEXT NOT NULL,
                    created REAL NOT NULL, started REAL, finished REAL,
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_pending_wake
                    ON runs(member_id) WHERE state = 'queued';
                CREATE UNIQUE INDEX IF NOT EXISTS one_running_turn
                    ON runs(member_id) WHERE state = 'running';
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL,
                    action TEXT NOT NULL, target TEXT NOT NULL, created REAL NOT NULL
                );
                """.split(";"):
                    if statement.strip():
                        conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            conn.execute(
                "INSERT OR IGNORE INTO settings VALUES (1, 0, 3, 0, ?)",
                (json.dumps(DEFAULT_STAFFING),),
            )
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @staticmethod
    def _event(conn: sqlite3.Connection, actor: str, action: str, target: str) -> None:
        conn.execute(
            "INSERT INTO events(actor,action,target,created) VALUES (?,?,?,?)",
            (actor, action, target, time.time()),
        )
        if action in {
            "configure",
            "enroll",
            "reserve_hire",
            "hire_failed",
            "move_member",
            "retire",
        }:
            conn.execute("UPDATE settings SET revision = revision + 1")

    @staticmethod
    def _member(conn: sqlite3.Connection, member_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
        if row is None:
            raise OrganizationError(
                "member_not_found", "The member is not in this organization.", 404
            )
        if row["state"] != "active":
            raise OrganizationError("member_retired", "This member has been retired.")
        return dict(row)

    @staticmethod
    def _task(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise OrganizationError("task_not_found", "The assignment does not exist.", 404)
        return dict(row)

    @staticmethod
    def _owner(actor: str) -> None:
        if actor != OWNER:
            raise OrganizationError(
                "owner_only", "Only the owner can change organization policy.", 403
            )

    @staticmethod
    def _settings(conn: sqlite3.Connection) -> dict[str, Any]:
        row = dict(conn.execute("SELECT * FROM settings WHERE singleton = 1").fetchone())
        row["staffing"] = json.loads(row["staffing"])
        row["enabled"] = bool(row["enabled"])
        del row["singleton"]
        return row

    @staticmethod
    def _wake(conn: sqlite3.Connection, member_id: str, reason: str) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO runs(id,member_id,state,reason,created) VALUES (?,?,?,?,?)",
            (_id(), member_id, "queued", reason, time.time()),
        )

    @staticmethod
    def _adjacent(conn: sqlite3.Connection, actor: str, recipient: str) -> None:
        if actor == OWNER:
            if recipient != OWNER:
                OrganizationStore._member(conn, recipient)
            return
        source = OrganizationStore._member(conn, actor)
        if recipient == OWNER and source["manager_id"] is None:
            return
        if recipient == OWNER:
            raise OrganizationError(
                "outside_reporting_line", "Send this message to your manager.", 403
            )
        target = OrganizationStore._member(conn, recipient)
        if actor == recipient or (
            source["manager_id"] != recipient and target["manager_id"] != actor
        ):
            raise OrganizationError(
                "outside_reporting_line",
                "Members can contact their manager and direct reports.",
                403,
            )

    @staticmethod
    def _capacity(
        conn: sqlite3.Connection, manager_id: str, role: str, *, reserved: str = ""
    ) -> None:
        manager = OrganizationStore._member(conn, manager_id)
        staffing = OrganizationStore._settings(conn)["staffing"]
        limit = staffing.get(manager["role"], {}).get(role, 0)
        occupied = conn.execute(
            "SELECT COUNT(*) FROM members WHERE manager_id=? AND role=? "
            "AND state IN ('active','provisioning')",
            (manager_id, role),
        ).fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM hiring WHERE manager_id=? AND role=? "
            "AND state='reserved' AND id<>?",
            (manager_id, role, reserved),
        ).fetchone()[0]
        if occupied + pending >= limit:
            raise OrganizationError(
                "staffing_limit", "This manager has no available places for that role."
            )

    def snapshot(self, actor: str = OWNER) -> dict[str, Any]:
        with self._transaction() as conn:
            if actor != OWNER:
                self._member(conn, actor)
            members = [
                dict(row) for row in conn.execute("SELECT * FROM members ORDER BY created,id")
            ]
            if actor != OWNER:
                me = next(row for row in members if row["id"] == actor)
                visible = {actor, me["manager_id"]}
                visible.update(row["id"] for row in members if row["manager_id"] == actor)
                members = [row for row in members if row["id"] in visible]
            # Memory bindings are authorization material, not a directory of
            # another member's store names. Only gateway resolution uses them.
            for member in members:
                member.pop("memory_store")
                member["permissions"] = list(ROLE_PERMISSIONS[member["role"]])
            task_sql = "SELECT * FROM tasks"
            msg_sql = "SELECT * FROM messages"
            params: tuple[str, ...] = ()
            if actor != OWNER:
                task_sql += " WHERE sender=? OR recipient=?"
                msg_sql += " WHERE sender=? OR recipient=?"
                params = (actor, actor)
            tasks = [
                dict(row)
                for row in conn.execute(
                    f"WITH visible AS ({task_sql}) "
                    "SELECT * FROM visible WHERE state NOT IN ('accepted','cancelled') "
                    "UNION ALL SELECT * FROM "
                    "(SELECT * FROM visible WHERE state IN ('accepted','cancelled') "
                    "ORDER BY created DESC,id DESC LIMIT 200) ORDER BY created DESC,id DESC",
                    params,
                )
            ]
            messages = [
                dict(row)
                for row in conn.execute(msg_sql + " ORDER BY created DESC LIMIT 200", params)
            ]
            run_sql = "SELECT * FROM runs"
            run_params: tuple[str, ...] = ()
            if actor != OWNER:
                run_sql += (
                    " WHERE member_id=? OR member_id IN "
                    "(SELECT id FROM members WHERE manager_id=?)"
                )
                run_params = (actor, actor)
            return {
                "settings": self._settings(conn),
                "actor": actor,
                "members": members,
                "tasks": tasks,
                "messages": messages,
                "runs": [
                    dict(row)
                    for row in conn.execute(
                        run_sql + " ORDER BY created DESC LIMIT 100", run_params
                    )
                ],
            }

    def member_for_store(self, memory_store: str) -> dict[str, Any] | None:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM members WHERE memory_store=?", (memory_store,)
            ).fetchone()
            return dict(row) if row else None

    def configure(
        self,
        actor: str,
        *,
        revision: int,
        concurrency: int,
        enabled: bool,
        staffing: dict[str, dict[str, int]],
    ) -> dict[str, Any]:
        self._owner(actor)
        _integer(concurrency, "concurrency", 1, 16)
        if type(enabled) is not bool or not isinstance(staffing, dict):
            raise OrganizationError("invalid_value", "Invalid organization settings.", 400)
        if set(staffing) != set(ROLES):
            raise OrganizationError("invalid_value", "Staffing must define every role.", 400)
        for role, limits in staffing.items():
            if not isinstance(limits, dict) or any(target not in ROLES for target in limits):
                raise OrganizationError("invalid_value", "Unknown staffing role.", 400)
            if role not in COORDINATORS and limits:
                raise OrganizationError("invalid_value", "This role cannot hire reports.", 400)
            for target, limit in limits.items():
                _integer(limit, "staffing limit", 0, 16)
                if target == "conductor":
                    raise OrganizationError(
                        "invalid_value", "The conductor reports to the owner.", 400
                    )
        with self._transaction() as conn:
            if self._settings(conn)["revision"] != revision:
                raise OrganizationError(
                    "revision_conflict", "The organization changed. Reload and try again."
                )
            for row in conn.execute(
                "SELECT m.role AS manager_role,c.role AS child_role,COUNT(*) AS n "
                "FROM members c JOIN members m ON c.manager_id=m.id "
                "WHERE c.state IN ('active','provisioning') GROUP BY m.id,c.role"
            ):
                if row["n"] > staffing[row["manager_role"]].get(row["child_role"], 0):
                    raise OrganizationError(
                        "staffing_in_use", "Retire or move reports before reducing this limit."
                    )
            if conn.execute("SELECT 1 FROM hiring WHERE state='reserved'").fetchone():
                raise OrganizationError("hiring_in_progress", "Wait for member creation to finish.")
            conn.execute(
                "UPDATE settings SET concurrency=?,enabled=?,staffing=?",
                (concurrency, int(enabled), json.dumps(staffing)),
            )
            self._event(conn, actor, "configure", "")
        return self.snapshot(actor)

    def enroll(
        self,
        actor: str,
        *,
        name: str,
        memory_store: str,
        role: str,
        manager_id: str | None,
        reservation: str = "",
        provisioning: bool = False,
    ) -> str:
        # Gateway service verifies owned V2 memory before calling this method.
        if not reservation:
            self._owner(actor)
        name = _text(name, "name", maximum=128)
        memory_store = _text(memory_store, "memory store", maximum=128)
        if role not in ROLES:
            raise OrganizationError("invalid_role", "Choose a supported role.", 400)
        with self._transaction() as conn:
            if reservation:
                reserved = conn.execute(
                    "SELECT * FROM hiring WHERE id=?", (reservation,)
                ).fetchone()
                if (
                    reserved is None
                    or reserved["state"] != "reserved"
                    or reserved["manager_id"] != actor
                    or manager_id != actor
                    or reserved["role"] != role
                ):
                    raise OrganizationError(
                        "invalid_reservation", "The hiring reservation is not valid.", 403
                    )
            if manager_id is None:
                if (
                    role != "conductor"
                    or conn.execute(
                        "SELECT 1 FROM members WHERE manager_id IS NULL "
                        "AND state IN ('active','provisioning')"
                    ).fetchone()
                ):
                    raise OrganizationError(
                        "invalid_root", "An organization has one conductor reporting to the owner."
                    )
            else:
                self._capacity(conn, manager_id, role, reserved=reservation)
            member_id = _id()
            try:
                conn.execute(
                    "INSERT INTO members VALUES (?,?,?,?,?,?,?)",
                    (
                        member_id,
                        name,
                        memory_store,
                        role,
                        manager_id,
                        "provisioning" if provisioning else "active",
                        time.time(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise OrganizationError(
                    "member_exists", "That member or memory already belongs to the organization."
                ) from exc
            if reservation:
                conn.execute(
                    "UPDATE hiring SET state='completed',member_id=? WHERE id=?",
                    (member_id, reservation),
                )
            self._event(conn, actor, "enroll", member_id)
            return member_id

    def finish_provisioning(self, member_id: str, *, succeeded: bool) -> None:
        """Trusted publication step; a partial member never executes unguarded."""
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE members SET state=? WHERE id=? AND state='provisioning'",
                ("active" if succeeded else "failed", member_id),
            ).rowcount
            if changed:
                self._event(conn, OWNER, "enroll", member_id)

    def reserve_hire(self, actor: str, role: str) -> dict[str, str]:
        """Prefer an idle persistent report; reserve headcount atomically."""
        with self._transaction() as conn:
            self._member(conn, actor)
            existing = conn.execute(
                "SELECT m.id FROM members m WHERE m.manager_id=? AND m.role=? "
                "AND m.state='active' AND NOT EXISTS "
                "(SELECT 1 FROM tasks t WHERE t.recipient=m.id "
                "AND t.state NOT IN ('accepted','cancelled')) AND NOT EXISTS "
                "(SELECT 1 FROM runs r WHERE r.member_id=m.id "
                "AND r.state IN ('queued','running')) ORDER BY m.created LIMIT 1",
                (actor, role),
            ).fetchone()
            if existing:
                return {"member_id": existing["id"], "reservation": ""}
            self._capacity(conn, actor, role)
            reservation = _id()
            conn.execute(
                "INSERT INTO hiring VALUES (?,?,?,'reserved',NULL,?)",
                (reservation, actor, role, time.time()),
            )
            self._event(conn, actor, "reserve_hire", reservation)
            return {"member_id": "", "reservation": reservation}

    def abandon_hire(self, reservation: str) -> None:
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE hiring SET state='failed' WHERE id=? AND state='reserved'", (reservation,)
            ).rowcount
            if changed:
                self._event(conn, OWNER, "hire_failed", reservation)

    def move_member(self, actor: str, member_id: str, manager_id: str | None) -> None:
        self._owner(actor)
        with self._transaction() as conn:
            member = self._member(conn, member_id)
            if member["manager_id"] == manager_id:
                return
            self._require_idle(conn, member_id)
            if manager_id is None:
                raise OrganizationError(
                    "invalid_root", "Only the existing conductor reports to the owner."
                )
            ancestor: str | None = manager_id
            while ancestor:
                if ancestor == member_id:
                    raise OrganizationError(
                        "reporting_cycle", "A member cannot report to its own descendant."
                    )
                ancestor = self._member(conn, ancestor)["manager_id"]
            self._capacity(conn, manager_id, member["role"])
            conn.execute("UPDATE members SET manager_id=? WHERE id=?", (manager_id, member_id))
            self._event(conn, actor, "move_member", member_id)

    @staticmethod
    def _require_idle(conn: sqlite3.Connection, member_id: str) -> None:
        busy = conn.execute(
            "SELECT 1 FROM tasks WHERE (sender=? OR recipient=?) "
            "AND state NOT IN ('accepted','cancelled') LIMIT 1",
            (member_id, member_id),
        ).fetchone()
        running = conn.execute(
            "SELECT 1 FROM runs WHERE member_id=? AND state='running'",
            (member_id,),
        ).fetchone()
        if busy or running:
            raise OrganizationError(
                "member_busy", "Finish or cancel this member's work before changing its position."
            )

    def retire(self, actor: str, member_id: str) -> None:
        self._owner(actor)
        with self._transaction() as conn:
            self._member(conn, member_id)
            self._require_idle(conn, member_id)
            if conn.execute(
                "SELECT 1 FROM members WHERE manager_id=? AND state IN ('active','provisioning')",
                (member_id,),
            ).fetchone():
                raise OrganizationError("has_reports", "Move or retire direct reports first.")
            conn.execute("UPDATE members SET state='retired' WHERE id=?", (member_id,))
            conn.execute(
                "UPDATE runs SET state='cancelled',finished=? WHERE member_id=? AND state='queued'",
                (time.time(), member_id),
            )
            self._event(conn, actor, "retire", member_id)

    def assign(
        self,
        actor: str,
        recipient: str,
        *,
        title: str,
        acceptance: str,
        parent_id: str | None = None,
    ) -> str:
        title, acceptance = _text(title, "title", maximum=2000), _text(acceptance, "acceptance")
        with self._transaction() as conn:
            target = self._member(conn, recipient)
            if actor != OWNER:
                source = self._member(conn, actor)
                if source["role"] not in COORDINATORS or target["manager_id"] != actor:
                    raise OrganizationError(
                        "delegation_denied", "Delegate to one of your direct reports.", 403
                    )
                if not parent_id:
                    raise OrganizationError(
                        "parent_required", "Delegate as part of an assignment you own.", 400
                    )
            if parent_id:
                parent = self._task(conn, parent_id)
                if parent["recipient"] != actor or parent["state"] not in (
                    "queued",
                    "working",
                    "blocked",
                ):
                    raise OrganizationError(
                        "parent_not_owned",
                        "The parent assignment is not active work assigned to you.",
                        403,
                    )
            task_id, now = _id(), time.time()
            conn.execute(
                "INSERT INTO tasks(id,parent_id,sender,recipient,title,acceptance,state,created,updated) "
                "VALUES (?,?,?,?,?,?,'queued',?,?)",
                (task_id, parent_id, actor, recipient, title, acceptance, now, now),
            )
            self._wake(conn, recipient, "assignment")
            self._event(conn, actor, "assign", task_id)
            return task_id

    def start_task_from_chat(
        self, member_id: str, request_id: str, *, title: str, acceptance: str
    ) -> str:
        """Record an owner request admitted by the live chat gateway.

        The request ID comes from the active turn, never from tool arguments.
        Its durable mapping makes retries within that turn atomic and idempotent.
        """
        request_id = _text(request_id, "owner request", maximum=32)
        title, acceptance = _text(title, "title", maximum=2000), _text(acceptance, "acceptance")
        with self._transaction() as conn:
            self._member(conn, member_id)
            previous = conn.execute(
                "SELECT member_id,task_id FROM owner_chat_tasks WHERE request_id=?", (request_id,)
            ).fetchone()
            if previous:
                if previous["member_id"] != member_id:
                    raise OrganizationError(
                        "owner_request_mismatch",
                        "This owner request belongs to another member.",
                        403,
                    )
                return str(previous["task_id"])
            task_id, now = _id(), time.time()
            conn.execute(
                "INSERT INTO tasks(id,parent_id,sender,recipient,title,acceptance,state,created,updated) "
                "VALUES (?,NULL,?,?,?,?,'queued',?,?)",
                (task_id, OWNER, member_id, title, acceptance, now, now),
            )
            conn.execute(
                "INSERT INTO owner_chat_tasks VALUES (?,?,?)", (request_id, member_id, task_id)
            )
            self._wake(conn, member_id, "owner chat assignment")
            self._event(conn, member_id, "start_task_from_owner_chat", task_id)
            return task_id

    def message(self, actor: str, recipient: str, text: str) -> str:
        text = _text(text, "message")
        with self._transaction() as conn:
            self._adjacent(conn, actor, recipient)
            message_id = _id()
            conn.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?)",
                (message_id, actor, recipient, text, time.time()),
            )
            if recipient != OWNER:
                self._wake(conn, recipient, "message")
            self._event(conn, actor, "message", message_id)
            return message_id

    def report(self, actor: str, task_id: str, status: str, text: str) -> None:
        text = _text(text, "report")
        states = {
            "progress": "working",
            "blocked": "blocked",
            "question": "blocked",
            "done": "review",
        }
        if status not in states:
            raise OrganizationError(
                "invalid_status", "Use progress, blocked, question or done.", 400
            )
        with self._transaction() as conn:
            member = self._member(conn, actor)
            task = self._task(conn, task_id)
            if task["recipient"] != actor or task["state"] in TERMINAL_TASK_STATES:
                raise OrganizationError(
                    "report_denied", "Report only on your own open assignment.", 403
                )
            if status == "done":
                children = conn.execute(
                    "SELECT state FROM tasks WHERE parent_id=?", (task_id,)
                ).fetchall()
                if member["role"] in COORDINATORS and not any(
                    c["state"] == "accepted" for c in children
                ):
                    raise OrganizationError(
                        "delegation_required",
                        "Delegate the work and accept a report before completing it.",
                    )
                if any(c["state"] not in TERMINAL_TASK_STATES for c in children):
                    raise OrganizationError(
                        "reports_pending", "Review outstanding delegated work first."
                    )
            conn.execute(
                "UPDATE tasks SET state=?,report=?,updated=? WHERE id=?",
                (states[status], text, time.time(), task_id),
            )
            if task["sender"] != OWNER:
                self._wake(conn, task["sender"], "report")
            self._event(conn, actor, "report:" + status, task_id)

    def review(self, actor: str, task_id: str, verdict: str, text: str) -> None:
        text = _text(text, "decision")
        if verdict not in ("accept", "revise", "cancel"):
            raise OrganizationError("invalid_verdict", "Use accept, revise or cancel.", 400)
        with self._transaction() as conn:
            task = self._task(conn, task_id)
            if actor != OWNER:
                self._member(conn, actor)
            if task["sender"] != actor:
                raise OrganizationError(
                    "review_denied", "Only the assigning manager can review this work.", 403
                )
            if task["state"] in TERMINAL_TASK_STATES or (
                verdict != "cancel" and task["state"] != "review"
            ):
                raise OrganizationError("not_in_review", "This assignment is not awaiting review.")
            if verdict == "cancel":
                children = conn.execute(
                    "WITH RECURSIVE descendants(id) AS (SELECT id FROM tasks WHERE parent_id=? "
                    "UNION ALL SELECT t.id FROM tasks t JOIN descendants d ON t.parent_id=d.id) "
                    "SELECT id FROM descendants",
                    (task_id,),
                ).fetchall()
                for child in children:
                    conn.execute(
                        "UPDATE tasks SET state='cancelled',decision=?,updated=? WHERE id=? "
                        "AND state NOT IN ('accepted','cancelled')",
                        (text, time.time(), child["id"]),
                    )
            conn.execute(
                "UPDATE tasks SET state=?,decision=?,updated=? WHERE id=?",
                (
                    {"accept": "accepted", "revise": "working", "cancel": "cancelled"}[verdict],
                    text,
                    time.time(),
                    task_id,
                ),
            )
            if verdict == "revise":
                self._wake(conn, task["recipient"], "revision")
            self._event(conn, actor, "review:" + verdict, task_id)

    def claim_run(self, *, busy_names: tuple[str, ...] = ()) -> dict[str, Any] | None:
        """Claim one turn under both the organization and per-member limits."""
        with self._transaction() as conn:
            settings = self._settings(conn)
            if not settings["enabled"]:
                return None
            running = conn.execute("SELECT COUNT(*) FROM runs WHERE state='running'").fetchone()[0]
            if running >= settings["concurrency"]:
                return None
            busy_filter = ""
            if busy_names:
                busy_filter = " AND m.name NOT IN (" + ",".join("?" for _ in busy_names) + ")"
            row = conn.execute(
                "SELECT r.* FROM runs r JOIN members m ON r.member_id=m.id "
                "WHERE r.state='queued' AND m.state='active' AND NOT EXISTS "
                "(SELECT 1 FROM runs active WHERE active.member_id=r.member_id AND active.state='running') "
                + busy_filter
                + " ORDER BY r.created,r.id LIMIT 1",
                busy_names,
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE runs SET state='running',started=? WHERE id=?", (time.time(), row["id"])
            )
            self._event(conn, OWNER, "run_started", row["id"])
            return {**dict(row), "state": "running", "member": self._member(conn, row["member_id"])}

    def finish_run(self, run_id: str, error: str = "") -> None:
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE runs SET state=?,error=?,finished=? WHERE id=? AND state='running'",
                ("failed" if error else "completed", error[:2000], time.time(), run_id),
            ).rowcount
            if changed:
                self._event(conn, OWNER, "run_finished", run_id)
                if error:
                    parent = conn.execute(
                        "SELECT m.manager_id FROM members m JOIN runs r ON r.member_id=m.id "
                        "WHERE r.id=? AND m.manager_id IS NOT NULL AND EXISTS "
                        "(SELECT 1 FROM tasks t WHERE t.recipient=m.id "
                        "AND t.state NOT IN ('accepted','cancelled'))",
                        (run_id,),
                    ).fetchone()
                    if parent:
                        self._wake(conn, parent["manager_id"], "report_failed")

    def return_run(self, run_id: str) -> None:
        """Give back admission when the member is already talking to its owner."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT member_id FROM runs WHERE id=? AND state='running'", (run_id,)
            ).fetchone()
            if row is None:
                return
            pending = conn.execute(
                "SELECT 1 FROM runs WHERE member_id=? AND state='queued'", (row["member_id"],)
            ).fetchone()
            conn.execute(
                "UPDATE runs SET state=?,started=NULL WHERE id=?",
                ("coalesced" if pending else "queued", run_id),
            )

    def recover_runs(self) -> int:
        """Boot-only recovery: interrupted turns need an explicit retry.

        A crash cannot establish whether a side effect completed. Do not
        silently replay an assignment or recreate a member after that boundary.
        """
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE runs SET state='interrupted',finished=?,error=? WHERE state='running'",
                (time.time(), "The gateway stopped during this turn. Review and retry."),
            ).rowcount
            conn.execute("UPDATE hiring SET state='interrupted' WHERE state='reserved'")
            if changed:
                self._event(conn, OWNER, "recover_runs", str(changed))
            return changed

    def retry(self, actor: str, member_id: str) -> None:
        with self._transaction() as conn:
            member = self._member(conn, member_id)
            if actor not in (OWNER, member_id, member["manager_id"]):
                raise OrganizationError(
                    "retry_denied", "Retry only your own turn or a direct report.", 403
                )
            if actor != OWNER:
                recent = conn.execute(
                    "SELECT state FROM runs WHERE member_id=? "
                    "AND state IN ('completed','failed','interrupted') "
                    "ORDER BY created DESC LIMIT 3",
                    (member_id,),
                ).fetchall()
                if len(recent) == 3 and all(row["state"] != "completed" for row in recent):
                    raise OrganizationError(
                        "retry_limit",
                        "Three attempts did not finish successfully. Ask the owner to inspect and retry.",
                    )
            self._wake(conn, member_id, "retry")
            self._event(conn, actor, "retry", member_id)
