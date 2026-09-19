"""Session selection is part of the canonical execution record."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace as dataclass_replace
from typing import Any

from kiro_crew.config.loader import ResolvedBindings, resolve_agent_bindings
from kiro_crew.execution_context import (
    ExecutionContext,
    MemoryStoreRef,
    bind_session_execution,
    member_config_for_id,
    read_session_execution,
    resolve_member_execution,
)
from kiro_crew.memory_stores import UnknownMemoryStore

SelectionChange = tuple[dict[str, Any] | None, dict[str, Any]]


def _revision(execution: ExecutionContext | None) -> str:
    return (
        hashlib.sha256(json.dumps(execution.to_record(), sort_keys=True).encode()).hexdigest()
        if execution
        else ""
    )


def session_agent_selection_name(session_key: str) -> str | None:
    execution = read_session_execution(session_key)
    return (execution.selection_name or execution.template_id) if execution else None


def session_agent_selection_kind(session_key: str, agent_name: str) -> str:
    execution = read_session_execution(session_key)
    return (
        execution.selection_kind
        if execution and agent_name == (execution.selection_name or execution.template_id)
        else ""
    )


def resolve_session_agent_bindings(
    resolver, config, session_key: str, agent_name: str | None, *project_dir
) -> ResolvedBindings:
    execution = read_session_execution(session_key)
    selected = agent_name or config.default_agent
    if execution is not None:
        # Member display labels may change; the durable ID and store do not.
        if execution.member_id is not None and execution.selection_kind == "member":
            try:
                selected, _ = member_config_for_id(config, execution.member_id)
            except UnknownMemoryStore:
                selected = execution.selection_name or execution.member_id
        else:
            selected = execution.selection_name or execution.template_id
    try:
        bindings = resolver(
            config,
            selected,
            *project_dir,
            validate_memory_files=False,
            **(
                {"selection_kind": execution.selection_kind, "execution_context": execution}
                if execution
                else {}
            ),
        )
    except StopIteration as exc:
        raise UnknownMemoryStore("Conversation agent selection is unavailable") from exc
    if execution is not None:
        bindings.memory_store_name = execution.store.store_id
        bindings.kiro_agent = execution.template_id
        bindings.execution_context = execution
    bindings.selection_revision = _revision(execution)
    return bindings


def record_provider_agent_switch(config, session_key, prior_agent, new_agent, project_dir):
    prior = read_session_execution(session_key)
    selected = resolve_agent_bindings(config, new_agent, project_dir, selection_kind="template")
    if not selected.requested_resolved:
        raise UnknownMemoryStore("Conversation agent selection is unavailable")
    selected.selection_revision = _revision(prior)
    if prior is not None:
        # A provider template event changes behavior, never the memory owner.
        selected.execution_context = dataclass_replace(
            prior,
            template_id=selected.kiro_agent,
            selection_name=new_agent if prior.member_id is None else prior.selection_name,
        )
    return record_agent_selection(session_key, new_agent, selected)


def record_agent_selection(session_key, agent_name, bindings, *, replace=False, memory_mode=None):
    kind = getattr(bindings, "selection_kind", "")
    selected = agent_name or bindings.resolved_alias
    if kind not in ("member", "template") or not selected or not bindings.requested_resolved:
        return None
    prior = read_session_execution(session_key)
    if not replace and _revision(prior) != (getattr(bindings, "selection_revision", "") or ""):
        raise UnknownMemoryStore("Conversation selection changed during preparation")
    execution = getattr(bindings, "execution_context", None)
    if not isinstance(execution, ExecutionContext):
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.execution_context import stricter_memory_mode

        mode = stricter_memory_mode(
            prior.memory_mode if prior else "persistent", memory_mode or "persistent"
        )
        app = prior.app if prior else ""
        if kind == "member":
            execution = resolve_member_execution(
                KiroCrewConfig.load(),
                selected,
                memory_mode=mode,
                app=app,
                validate_memory_files=False,
            )
        else:
            if prior and prior.member_id:
                execution = dataclass_replace(prior, template_id=bindings.kiro_agent)
            else:
                execution = ExecutionContext(
                    None,
                    MemoryStoreRef(bindings.memory_store_name or "default"),
                    "template",
                    bindings.kiro_agent,
                    mode,
                    app,
                    selected,
                )
    if memory_mode is not None:
        execution = execution.with_mode(memory_mode)
    if prior == execution and not replace:
        return None
    execution = dataclass_replace(execution, selection_revision=uuid.uuid4().hex)
    # The comparison above is backed by the session record CAS during publication.
    bind_session_execution(session_key, execution, replace_existing=True, expected=prior)
    bindings.execution_context = execution
    return prior.to_record() if prior else None, execution.to_record()


def restore_agent_selection(session_key: str, change: SelectionChange | None) -> None:
    if change is None:
        return
    from kiro_crew.atomic_write import atomic_write
    from kiro_crew.history import ConversationLog

    prior, published = change
    from kiro_crew.execution_context import restore_live_session_execution

    if restore_live_session_execution(session_key, prior, published):
        return
    log = ConversationLog()
    with log._locked(session_key):
        metadata, readable = log._read_metadata_status(session_key)
        if not readable or metadata.get("execution_context") != published:
            return
        if prior is not None:
            log._update_metadata_locked(
                session_key,
                {
                    "execution_context": prior,
                    "memory_store": (
                        ""
                        if prior["store"]["store_id"] == "default"
                        else prior["store"]["store_id"]
                    ),
                    "memory_mode": prior["memory_mode"],
                },
            )
            return
        path = log._path(session_key)
        rows = path.read_text(encoding="utf-8").splitlines(keepends=True)
        for field in ("execution_context", "memory_store", "memory_mode"):
            metadata.pop(field, None)
        rows[0] = json.dumps(metadata, ensure_ascii=False) + "\n"
        atomic_write(path, "".join(rows))
