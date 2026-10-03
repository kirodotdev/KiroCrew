"""Session selection is part of the canonical execution record."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field as dataclass_field
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
    restore_live_session_execution,
)
from kiro_crew.history import (
    BINDING_FIELDS,
    METADATA_LINE_CORRUPT,
    METADATA_LINE_READABLE,
    ConversationLog,
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
        selected.execution_context = dataclass_replace(
            prior,
            template_id=selected.kiro_agent,
            selection_name=new_agent if prior.member_id is None else prior.selection_name,
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


@dataclass
class BindingSnapshot:
    """A session's binding as it was before a sequence of selection writes.

    ``fields`` holds the raw values of :data:`BINDING_FIELDS` that the metadata
    line carried, absent ones omitted, so a legacy record with ``memory_store``
    and ``memory_mode`` but no ``execution_context`` is restored as that record.
    ``durable`` is False when the line was corrupt: whatever write follows heals
    it, and the unknown original cannot be put back. ``published`` collects every execution
    record the writes are known to have committed, the compare-and-set set for
    the restore, and ``in_flight`` is set while a publishing write runs, so one
    that raised after committing is accounted for. ``owns_stub`` is cleared once
    the session key may belong to another writer (a slot that replaced the one
    the writes ran for): a transcript that did not exist before is then theirs
    as much as these writes', and the restore leaves it.
    """

    session_key: str
    fields: dict[str, Any]
    execution: dict[str, Any] | None
    had_log: bool
    durable: bool = True
    published: list[dict[str, Any]] = dataclass_field(default_factory=list)
    in_flight: bool = False
    owns_stub: bool = True

    @contextmanager
    def publishing(self) -> Iterator[None]:
        """Mark a publishing write as running for the duration of the block.

        Cleared only when the block completes: a write that raised may have
        committed first, so ``in_flight`` stays set and the restore counts the
        record current then as that write's.
        """
        self.in_flight = True
        yield
        self.in_flight = False


def current_execution_record(session_key: str) -> dict[str, Any] | None:
    """The session's current execution record, or None when it has none."""
    current = read_session_execution(session_key)
    return current.to_record() if current is not None else None


def snapshot_session_binding(session_key: str) -> BindingSnapshot:
    """Read the binding later selection writes may overwrite. Blocking.

    A line that cannot be read right now (``METADATA_LINE_TRANSIENT``) is
    refused before anything is published. A corrupt line, which no retry will
    read, is not: a pick that publishes nothing must still open the session, and
    one that does publish is refused by its own ``read_session_execution``. Such a
    snapshot is marked not durably restorable, so the restore leaves the line to
    whatever healed it.
    """
    log = ConversationLog()
    with log._locked(session_key):
        had_log = log.has_log(session_key)
        # One read for both: a second read could answer READABLE for a line the
        # first read failed on, leaving ``metadata`` empty for a line that holds
        # a binding.
        metadata, state = (
            log._read_metadata_state(session_key) if had_log else ({}, METADATA_LINE_READABLE)
        )
    if state == METADATA_LINE_CORRUPT:
        return BindingSnapshot(session_key, {}, None, had_log, durable=False)
    if state != METADATA_LINE_READABLE:
        raise OSError(f"session record for {session_key} cannot be read right now")
    fields = {name: metadata[name] for name in BINDING_FIELDS if name in metadata}
    return BindingSnapshot(session_key, fields, current_execution_record(session_key), had_log)


def note_published(snapshot: BindingSnapshot, record: dict[str, Any] | None) -> None:
    """Record that a selection write committed *record*."""
    if record is not None and record != snapshot.execution and record not in snapshot.published:
        snapshot.published.append(record)


def _record_left_by_raised_write(snapshot: BindingSnapshot) -> dict[str, Any] | None:
    """The execution record a publishing write that raised may have committed.

    For a snapshot taken of a corrupt line (``durable`` False), a write that
    committed replaced the whole metadata line, so a line that is still corrupt
    was never written: that write published nothing, and the restore goes on
    with the rest. Any other read failure is raised, since whether the write
    landed cannot be told.
    """
    key = snapshot.session_key
    try:
        return current_execution_record(key)
    except Exception:
        if (
            not snapshot.durable
            and ConversationLog().metadata_line_state(key) == METADATA_LINE_CORRUPT
        ):
            return None
        raise


def snapshot_from_selection_change(
    session_key: str, change: SelectionChange | None
) -> BindingSnapshot | None:
    """The snapshot one ``record_agent_selection`` write implies, for its restore.

    For a caller that took no :func:`snapshot_session_binding` before its write:
    the binding fields come from the ``prior`` execution record the write
    replaced, and the write's own record is the only one published. The
    transcript is treated as pre-existing, so the restore never deletes it.
    None when the write published nothing.
    """
    if change is None:
        return None
    prior, published = change
    fields: dict[str, Any] = {}
    if prior is not None:
        store = prior["store"]["store_id"]
        fields = {
            "execution_context": prior,
            "memory_store": "" if store == "default" else store,
            "memory_mode": prior["memory_mode"],
        }
    return BindingSnapshot(session_key, fields, prior, had_log=True, published=[published])


def restore_session_binding(snapshot: BindingSnapshot | None) -> None:
    """Put back the binding a snapshot recorded. Blocking. The one binding restore.

    *snapshot* comes from :func:`snapshot_session_binding` (taken before the
    writes) or :func:`snapshot_from_selection_change` (built from one write's
    change); None restores nothing. Compare-and-set: the durable record is
    restored only while its ``execution_context`` is one the snapshotted writes
    published, so a binding someone else wrote since is left alone. A publishing
    write that raised may have committed before it failed; the record current
    now counts as its write, which holds for a caller that keeps the session's
    switch lock across the writes and this restore. On a snapshot of a corrupt
    line, a line still corrupt means that write published nothing. The restored
    metadata line carries exactly the snapshot's values of :data:`BINDING_FIELDS`,
    so a legacy record keeps its ``memory_store`` and ``memory_mode``. A
    restricted session's live carrier and any vouch for a published record are
    rolled back by ``restore_live_session_execution``. When the transcript did
    not exist before and the snapshot still owns it, the metadata-only stub the
    writes created is removed, provided it still holds no messages. Both durable
    parts run through :meth:`ConversationLog.restore_binding_fields`, the
    history module's own writer for the metadata line, under one transcript hold.
    """
    if snapshot is None:
        return
    key = snapshot.session_key
    if snapshot.in_flight:
        note_published(snapshot, _record_left_by_raised_write(snapshot))
    published = list(snapshot.published)
    live = False
    for record in published:
        # Every candidate, not the first match: each one withdraws its own vouch.
        live = restore_live_session_execution(key, snapshot.execution, record) or live
    restore = bool(published) and not live and snapshot.durable
    remove_stub = not snapshot.had_log and snapshot.owns_stub
    if not restore and not remove_stub:
        return
    # A refused stub delete raises, so the rollback keeps the reservation
    # rather than reporting success.
    ConversationLog().restore_binding_fields(
        key,
        snapshot.fields,
        published=published if restore else (),
        remove_empty_stub=remove_stub,
    )
