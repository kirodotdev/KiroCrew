"""Immutable memory routing owned by each durable execution record.

These values route built-in operations. They are not a same-host confidentiality
boundary and do not grant transport, app, owner or governance permissions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from threading import RLock
from typing import Any, Literal, Mapping, overload

EXECUTION_CONTEXT_KEY = "execution_context"
MEMORY_MODES = ("persistent", "incognito", "temporary")
# Restricted sessions own their record in memory for their lifetime.
_LIVE_EXECUTIONS: dict[tuple[str, str], ExecutionContext] = {}
_EXECUTION_LOCK = RLock()


def _live_key(session_key: str) -> tuple[str, str]:
    from kiro_crew.config.paths import data_home

    return str(data_home()), session_key


def clear_session_execution(
    session_key: str, *, expected: ExecutionContext | None | object = ...
) -> None:
    with _EXECUTION_LOCK:
        key = _live_key(session_key)
        if expected is ... or _LIVE_EXECUTIONS.get(key) == expected:
            _LIVE_EXECUTIONS.pop(key, None)


def _unavailable(message: str):
    from kiro_crew.memory_stores import UnknownMemoryStore

    return UnknownMemoryStore(f"Execution memory is unavailable: {message}; Global was not used")


def stricter_memory_mode(*modes: str) -> str:
    if not modes or any(mode not in MEMORY_MODES for mode in modes):
        raise _unavailable("invalid privacy mode")
    return max(modes, key=MEMORY_MODES.index)


@dataclass(frozen=True)
class MemoryStoreRef:
    store_id: str
    member_id: str | None = None

    def __post_init__(self) -> None:
        from kiro_crew.memory_stores import validate_memory_store_name

        validate_memory_store_name(self.store_id)
        if self.member_id is not None:
            from kiro_crew.members import validate_slug

            validate_slug(self.member_id)
            if self.store_id == "default":
                raise _unavailable("a member cannot use the Global store")

    @property
    def legacy_name(self) -> str:
        return "" if self.store_id == "default" else self.store_id


@dataclass(frozen=True)
class ExecutionContext:
    member_id: str | None
    store: MemoryStoreRef
    selection_kind: str
    template_id: str
    memory_mode: str = "persistent"
    app: str = ""
    selection_name: str = ""
    selection_revision: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.store, MemoryStoreRef) or self.store.member_id != self.member_id:
            raise _unavailable("member and store identity disagree")
        if self.selection_kind not in ("member", "template"):
            raise _unavailable("invalid agent namespace")
        if not isinstance(self.template_id, str) or (
            self.member_id is not None and not self.template_id
        ):
            raise _unavailable("missing execution template")
        if (
            not isinstance(self.app, str)
            or not isinstance(self.selection_name, str)
            or not isinstance(self.selection_revision, str)
        ):
            raise _unavailable("invalid execution attribution")
        stricter_memory_mode(self.memory_mode)

    def to_record(self) -> dict[str, Any]:
        return asdict(self)

    def with_mode(self, mode: str) -> ExecutionContext:
        return replace(self, memory_mode=stricter_memory_mode(self.memory_mode, mode))


@overload
def execution_from_record(
    record: Mapping[str, Any], *, required: Literal[True] = True
) -> ExecutionContext: ...


@overload
def execution_from_record(
    record: Mapping[str, Any], *, required: Literal[False]
) -> ExecutionContext | None: ...


@overload
def execution_from_record(
    record: Mapping[str, Any], *, required: bool
) -> ExecutionContext | None: ...


def execution_from_record(
    record: Mapping[str, Any], *, required: bool = True
) -> ExecutionContext | None:
    """Decode the owner's canonical field; never infer membership from a name."""
    if not isinstance(record, Mapping):
        raise _unavailable("execution record is not an object")
    payload = record.get(EXECUTION_CONTEXT_KEY)
    if payload is None and EXECUTION_CONTEXT_KEY not in record and not required:
        return None
    if not isinstance(payload, dict):
        raise _unavailable("missing or malformed execution context")
    try:
        store = payload["store"]
        if not isinstance(store, dict):
            raise ValueError("invalid store")
        return ExecutionContext(
            member_id=payload["member_id"],
            store=MemoryStoreRef(store_id=store["store_id"], member_id=store["member_id"]),
            selection_kind=payload["selection_kind"],
            template_id=payload["template_id"],
            memory_mode=payload["memory_mode"],
            app=payload["app"],
            selection_name=payload.get("selection_name", ""),
            selection_revision=payload.get("selection_revision", ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _unavailable("malformed execution context") from exc


def member_config_for_id(config: Any, member_id: str) -> tuple[str, Any]:
    """Find the unique persisted ID; names and slug candidates are never identity."""
    matches = [
        (name, member)
        for name, member in config.agents.items()
        if getattr(member, "member_id", "") == member_id
    ]
    if not member_id or len(matches) != 1:
        raise _unavailable("member identity is missing or ambiguous")
    return matches[0]


def resolve_member_execution(
    config: Any,
    member: str,
    *,
    memory_mode: str = "persistent",
    app: str = "",
    validate_memory_files: bool = False,
) -> ExecutionContext:
    """Capture an explicitly selected existing member and its store together."""
    from kiro_crew.memory_stores import require_member_memory_store

    alias, agent = (
        (member, config.agents[member])
        if member in config.agents
        else member_config_for_id(config, member)
    )
    store = require_member_memory_store(config, alias, require_directory=validate_memory_files)
    declaration = config.memory_stores.get(store)
    member_id = getattr(agent, "member_id", "") or None
    if getattr(declaration, "memory_version", 1) == 2 and not member_id:
        raise _unavailable("member has no persisted identity")
    return ExecutionContext(
        member_id=member_id,
        store=MemoryStoreRef(store, member_id),
        selection_kind="member",
        template_id=agent.kiro_agent or "kirocrew",
        memory_mode=memory_mode,
        app=app,
        selection_name=alias,
    )


def execution_for_store(
    store: str, *, memory_mode: str = "persistent", app: str = "", template_id: str = ""
) -> ExecutionContext:
    """Admission adapter for a store already selected by trusted gateway code."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.memory_stores import require_memory_store

    if not isinstance(store, str):
        raise _unavailable("memory identity is malformed")
    config = KiroCrewConfig.load()
    name = require_memory_store(store or "default", config=config, require_directory=False)
    declaration = config.memory_stores.get(name)
    if getattr(declaration, "memory_version", 1) == 2:
        member_id = getattr(declaration, "owner_member_id", "")
        alias, _ = member_config_for_id(config, member_id)
        resolved = resolve_member_execution(config, alias, memory_mode=memory_mode, app=app)
        if resolved.store.store_id != name:
            raise _unavailable("member store binding changed")
        return resolved
    return ExecutionContext(
        None, MemoryStoreRef(name), "template", template_id, memory_mode, app, template_id
    )


def validate_execution(
    execution: ExecutionContext, *, validate_memory_files: bool = True
) -> ExecutionContext:
    """Check only the captured store, never re-resolve a live member selection."""
    from kiro_crew.memory_stores import MEMORY_DB_FILE, _named_store_dir

    if execution.member_id is not None:
        path = _named_store_dir(execution.store.store_id) / MEMORY_DB_FILE
        if not validate_memory_files:
            return execution
        from kiro_crew.vector_memory import read_member_database_identity, sqlite3

        try:
            identity = read_member_database_identity(path)
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise _unavailable("member database is unreadable") from exc
        if identity != (execution.member_id, execution.store.store_id):
            raise _unavailable("stored database identity changed")
    return execution


def derive_execution(
    parent: ExecutionContext,
    *,
    target_member: str | None = None,
    config: Any = None,
    requested_mode: str | None = None,
) -> ExecutionContext:
    """Inherit by default; an explicit target is resolved by the admitted caller."""
    mode = stricter_memory_mode(parent.memory_mode, requested_mode or parent.memory_mode)
    if target_member is None:
        return parent.with_mode(mode)
    if not target_member:
        raise _unavailable("target member must be explicit")
    if config is None:
        from kiro_crew.config.loader import KiroCrewConfig

        config = KiroCrewConfig.load()
    return resolve_member_execution(config, target_member, memory_mode=mode, app=parent.app)


def read_live_session_execution(session_key: str) -> ExecutionContext | None:
    """Snapshot the live carrier for generation-safe restricted-session cleanup."""
    with _EXECUTION_LOCK:
        return _LIVE_EXECUTIONS.get(_live_key(session_key))


@overload
def read_session_execution(session_key: str, *, required: Literal[True]) -> ExecutionContext: ...


@overload
def read_session_execution(
    session_key: str, *, required: Literal[False] = False
) -> ExecutionContext | None: ...


@overload
def read_session_execution(session_key: str, *, required: bool) -> ExecutionContext | None: ...


def read_session_execution(session_key: str, *, required: bool = False) -> ExecutionContext | None:
    from kiro_crew.history import ConversationLog

    with _EXECUTION_LOCK:
        live = _LIVE_EXECUTIONS.get(_live_key(session_key))
    if live is not None:
        return live
    if not session_key:
        if required:
            raise _unavailable("missing session")
        return None
    if session_key.startswith("subagent:"):
        from kiro_crew.subagent_persistence import read_run_execution, read_state

        record = read_state(session_key.split(":", 1)[1])
        if record is not None:
            return read_run_execution(session_key.split(":", 1)[1])
    record, readable = ConversationLog().get_metadata_status(session_key)
    if not readable:
        raise _unavailable("session record is unreadable")
    execution = execution_from_record(record, required=required)
    if execution is None:
        if record.get("member_id") or record.get("selection_kind") == "member":
            raise _unavailable("session has no canonical member identity")
        store = record.get("memory_store")
        if store and store != "default":
            from kiro_crew.memory_stores import memory_store_version

            if memory_store_version(store) == 2:
                raise _unavailable("session has no canonical member identity")
    return execution


def capture_session_execution(session_key: str, *, template_id: str = "") -> ExecutionContext:
    """Capture an existing carrier, or an explicitly ordinary V1 session."""
    existing = read_session_execution(session_key)
    if existing is not None:
        return existing
    from kiro_crew.history import ConversationLog

    metadata, readable = (
        ConversationLog().get_metadata_status(session_key) if session_key else ({}, True)
    )
    if not readable:
        raise _unavailable("session metadata is unreadable")
    mode = metadata.get("memory_mode", "persistent")
    store = metadata.get("memory_store", "")
    if not isinstance(store, str):
        raise _unavailable("invalid V1 store")
    if store and store != "default":
        return execution_for_store(store, memory_mode=mode, template_id=template_id)
    return ExecutionContext(None, MemoryStoreRef("default"), "template", template_id, mode)


def bind_session_execution(
    session_key: str,
    execution: ExecutionContext,
    *,
    replace_existing: bool = False,
    expected: ExecutionContext | None | object = ...,
) -> None:
    """Publish inside the session's own record, preserving concurrent identity."""
    from kiro_crew.history import ConversationLog

    if not session_key:
        raise _unavailable("missing session")
    log = ConversationLog()
    current = read_session_execution(session_key)
    if expected is not ... and current != expected:
        raise _unavailable("session changed during admission")
    if current is not None:
        execution = execution.with_mode(current.memory_mode)
    if current is not None and not replace_existing:
        if replace(current, memory_mode=execution.memory_mode) != execution:
            raise _unavailable("session already belongs to another execution")
    if session_key.startswith("subagent:"):
        from kiro_crew.subagent_persistence import update_execution_context

        update_execution_context(session_key.split(":", 1)[1], execution, expected=current)
        return
    if execution.memory_mode != "persistent":
        metadata, readable = log.get_metadata_status(session_key)
        if not readable:
            raise _unavailable("session record is unreadable")
        durable = execution_from_record(metadata, required=False)
        if durable is not None:
            # Only retained identity/mode metadata is tightened. Never write a
            # new restricted selection or body merely to keep routing alive.
            retained = durable.with_mode(execution.memory_mode)
            if retained != durable and not log.update_metadata_if(
                session_key,
                {EXECUTION_CONTEXT_KEY: retained.to_record(), "memory_mode": retained.memory_mode},
                lambda meta: meta.get(EXECUTION_CONTEXT_KEY) == durable.to_record(),
            ):
                raise _unavailable("session changed during privacy tightening")
        elif metadata:
            retained_mode = stricter_memory_mode(
                metadata.get("memory_mode", "persistent"), execution.memory_mode
            )
            if not log.update_metadata_if(
                session_key,
                {"memory_mode": retained_mode},
                lambda meta: meta == metadata,
            ):
                raise _unavailable("session changed during privacy tightening")
        with _EXECUTION_LOCK:
            latest = _LIVE_EXECUTIONS.get(_live_key(session_key))
            if latest is not None and latest != current:
                raise _unavailable("session changed during admission")
            _LIVE_EXECUTIONS[_live_key(session_key)] = execution
        return
    expected = current.to_record() if current is not None else None
    fields = {
        EXECUTION_CONTEXT_KEY: execution.to_record(),
        "memory_store": execution.store.legacy_name,
        "memory_mode": execution.memory_mode,
    }
    if not log.update_metadata_if(
        session_key, fields, lambda meta: meta.get(EXECUTION_CONTEXT_KEY) == expected
    ):
        raise _unavailable("session changed during admission")


def restore_live_session_execution(session_key: str, prior, published) -> bool:
    """CAS rollback a restricted admission; False means use the durable owner."""
    with _EXECUTION_LOCK:
        key = _live_key(session_key)
        current = _LIVE_EXECUTIONS.get(key)
        if current is None:
            return False
        if current.to_record() == published:
            if prior is None:
                _LIVE_EXECUTIONS.pop(key, None)
            else:
                previous = execution_from_record({EXECUTION_CONTEXT_KEY: prior})
                _LIVE_EXECUTIONS[key] = previous.with_mode(current.memory_mode)
        return True
