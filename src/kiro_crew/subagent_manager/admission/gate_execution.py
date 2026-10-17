"""The execution a spawn runs under: its memory store, identity, template and app.

``resolve_spawn_execution`` captures it before the spawn queues or awaits anything,
from the stored context of a re-entry, the record of the run a continuation resumes, or
the parent session; ``spawn_impl`` and the facade's off-loop admission both call it.

A function here whose first parameter is ``self`` is a ``_GateMixin`` method: the class
binds it under the same name in ``admission/gate.py``. It does not end in ``_impl``, so
it runs on this module's own globals and imports every name it uses inside its body.

The manager fields this module uses. Writes: none. Reads: ``_sessions``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...execution_context import ExecutionContext
    from .gate import _GateMixin


def resolve_spawn_execution(
    self: _GateMixin,
    *,
    parent_session_key="",
    agent="",
    conversation_key="",
    memory_store="",
    app="",
    crew="",
    target_member=None,
    _memory_mode="persistent",
    _execution_context=None,
    _record=...,
    _inherited_selection=None,
) -> ExecutionContext:
    """Resolve routing on-loop; async admission supplies its off-loop record read."""
    from dataclasses import replace

    from kiro_crew.config.loader import KiroCrewConfig, member_template_id
    from kiro_crew.execution_context import (
        ExecutionContext,
        derive_execution,
        execution_for_store,
        execution_from_record,
        read_session_execution,
    )

    execution: ExecutionContext | None
    if _execution_context is not None:
        execution = execution_from_record({"execution_context": _execution_context})
    elif conversation_key:
        from kiro_crew.subagent_persistence import read_run_execution

        execution = (
            read_run_execution(conversation_key.removeprefix("subagent:"))
            if _record is ...
            else _record
        )
        if execution.selection_kind == "member" and not agent:
            # Refresh ordinary persona/capability selection for this new
            # invocation, while retaining the original memory identity.
            from kiro_crew.execution_context import member_config_for_id

            config = KiroCrewConfig.load()
            if execution.member_id is not None:
                alias, selected = member_config_for_id(config, execution.member_id)
            else:
                alias = execution.selection_name
                selected = config.agents.get(alias)
                if selected is None:
                    raise ValueError("selected member is unavailable")
            execution = replace(
                execution,
                template_id=member_template_id(selected),
                selection_name=alias,
            )
    else:
        execution = read_session_execution(parent_session_key) if _record is ... else _record
        if execution is None:
            inherited = ("template", "")
            if parent_session_key and not agent:
                inherited = (
                    self._manager._sessions.get_agent_selection(parent_session_key)
                    if _inherited_selection is None
                    else _inherited_selection
                )
                if (
                    not isinstance(inherited, tuple)
                    or len(inherited) != 2
                    or inherited[0] not in ("template", "member")
                    or not isinstance(inherited[1], str)
                ):
                    raise ValueError("effective agent template is invalid")
            execution = execution_for_store(
                memory_store,
                memory_mode=_memory_mode,
                app=app,
                template_id=agent or inherited[1],
            )
            if inherited[0] == "member":
                from kiro_crew.crewmate_prune_migration import (
                    removed_crewmate_names as _removed_crewmate_names,
                )

                selected = KiroCrewConfig.load().agents.get(inherited[1])
                if selected is not None:
                    execution = replace(
                        execution,
                        selection_kind="member",
                        selection_name=inherited[1],
                        template_id=member_template_id(selected),
                    )
                elif (
                    execution.store.store_id != "default"
                    or execution.member_id is not None
                    or inherited[1] not in _removed_crewmate_names()
                ):
                    raise ValueError("selected member is unavailable")
                # Otherwise the member is a synced crewmate the startup prune
                # removed: same rule ``adopt_removed_synced_crewmate`` applies
                # to records, so the run keeps its template on the shared store.
        execution = derive_execution(
            execution,
            target_member=target_member or crew or None,
            requested_mode=_memory_mode,
        )
    execution = execution.with_mode(_memory_mode)
    if agent and not conversation_key:
        if not crew and not target_member:
            # The delegate split: the parent's store and identity, the selected
            # template's namespace. A member with no persisted id has no
            # identity field, so its child is a plain template run on the
            # parent's store (the `session_create` arm keeps that member's
            # selection instead; the record cannot say "this member, under
            # that template" on either path).
            execution = execution.with_template(agent, agent)
        else:
            execution = replace(execution, template_id=agent)
    if execution.app and app and execution.app != app:
        raise ValueError("subagent app ownership does not match its parent")
    app = execution.app or app
    if execution.app != app:
        execution = replace(execution, app=app)
    return execution
