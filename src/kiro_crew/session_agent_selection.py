"""Session selection is part of the canonical execution record."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace as dataclass_replace
from typing import Any

from kiro_crew.agent_spec_format import NATIVE_SKILL_ALIAS_PREFIX
from kiro_crew.config.loader import (
    ResolvedBindings,
    dispatch_kiro_agent,
    resolve_agent_bindings,
)
from kiro_crew.execution_context import (
    ExecutionContext,
    MemoryStoreRef,
    adopt_removed_synced_crewmate,
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


def _source_of_view(name: str) -> str:
    """The agent a stored skill-view name was built from, or *name* unchanged.

    A conversation recorded while the native skill projection was on can hold a
    generated ``kirocrew-skill-view-<digest>`` name as its agent. That name is a
    file the projection wrote, and a boot drain or an operator may have removed
    it since, so resolving it by name finds nothing and the turn is refused.
    The projection records which agent each view was built from (its ownership
    sidecar, or the view ledger), and :func:`source_agent_name` reads that record
    even after the view file is gone. A view nothing records stays as it is, so
    it resolves to nothing and is refused rather than guessed.

    Blocking: it may read one sidecar. Every caller of
    :func:`resolve_session_agent_bindings` runs it off the event loop.
    """
    if not name.startswith(NATIVE_SKILL_ALIAS_PREFIX):
        return name
    # Deferred: the driver module pulls in the ACP stack, which a plain agent
    # name never needs.
    from kiro_crew.agent_sdk.drivers import acp as acp_driver

    source = acp_driver.skill_view_source_agent(name)
    return name if source is None else source


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
    selected = _source_of_view(selected)
    if execution is not None:
        # The decoder already re-reads a record bound to a pruned synced
        # crewmate as its template; answering against the CALLER's config
        # snapshot keeps this resolve consistent with the config it is given.
        execution = adopt_removed_synced_crewmate(execution, config)
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
        # Both recorded kinds can need a skill-view repair. Only a MEMBER's
        # template id came from a crewmate row that may hold a package filename;
        # a template id is already the provider selection and must stay exact.
        kiro_agent = _source_of_view(execution.template_id)
        if execution.selection_kind == "member":
            kiro_agent = dispatch_kiro_agent(kiro_agent)
        bindings.kiro_agent = kiro_agent
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
        # A session with no member id (the ordinary chat, recorded as the default
        # alias with kind "member") now selects the TEMPLATE, so its kind must say
        # so too. Left as "member", the next turn resolves the template name in
        # the member namespace, where an agent that is only a spec file (not a
        # config.agents key) is unresolvable, and the turn is refused.
        unowned = prior.member_id is None
        selected.execution_context = dataclass_replace(
            prior,
            template_id=selected.kiro_agent,
            selection_name=new_agent if unowned else prior.selection_name,
            selection_kind="template" if unowned else prior.selection_kind,
        )
    # Carrying the owner over means carrying it out of the session's OWN record,
    # which the session can rewrite, so this publication must not vouch for it.
    # Without `vouch`, a caller that forges its record to name a peer's store and
    # then triggers a template switch gets that store vouched here, and the
    # own-store admission's two independent sources become one it controls. With no
    # prior there is nothing carried and the store is the resolved one, so it stands.
    return record_agent_selection(session_key, new_agent, selected, vouch=prior is None)


def record_agent_selection(
    session_key, agent_name, bindings, *, replace=False, memory_mode=None, vouch=False
):
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
                # Same carried owner as the provider-switch path above, same reason
                # not to vouch for it.
                execution = dataclass_replace(prior, template_id=bindings.kiro_agent)
                vouch = False
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
        # Nothing to publish, and deliberately nothing vouched either. A session's
        # own-store authority is held only in this process, so a restart drops it
        # while the durable record survives -- and re-establishing it HERE cannot be
        # done safely, because every value reachable on this path resolves through
        # something the session itself can influence. The record is written by the
        # session. The slot's store is rehydrated from that record. `execution` is
        # built from it on the provider-switch path. And config is looked up by the
        # record's own ``member_id`` a few lines above, so a config re-read agrees
        # with a forged record by construction instead of checking it.
        #
        # A restart therefore drops the own-store admission until the owner
        # re-selects the agent, which binds afresh through the durable path. That is
        # the fail-closed direction, and a regression test pins the refusal so it is
        # a stated property rather than something rediscovered later.
        return None
    execution = dataclass_replace(execution, selection_revision=uuid.uuid4().hex)
    # The comparison above is backed by the session record CAS during publication.
    bind_session_execution(
        session_key, execution, replace_existing=True, expected=prior, vouch=vouch
    )
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
