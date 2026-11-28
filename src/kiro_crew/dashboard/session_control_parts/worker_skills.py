"""``session_create(skills=...)``: the create route's check of a worker's trigger list.

The list's meaning and its turn-time rule live in :mod:`kiro_crew.worker_trigger_skills`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.dashboard.session_control_parts.caller_access import SessionControlError

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState
    from kiro_crew.execution_context import ExecutionContext


async def trigger_skills_for_child(
    state: DashboardState,
    raw: object,
    child_execution: ExecutionContext,
    project_dir: str | Path | None,
    backend: str | None,
) -> tuple[str, ...] | None:
    """The parsed *raw* list for the child *child_execution* describes, or ``None``.

    Refused (:class:`SessionControlError`) when the list is malformed, when the
    child is not a worker (``skills_not_for_agent``), when its skill catalog is
    still loading (``skills_catalog_building``), or when a key is outside the
    scope that child will have (``unknown_skills``, naming each one).
    """
    # Function-local, so the session_control facade serves none of these names.
    from kiro_crew.worker_trigger_skills import (
        TRIGGER_LIST_AGENTS,
        TriggerSkillsRefused,
        parse_trigger_skills,
        unknown_keys,
    )

    try:
        keys = parse_trigger_skills(raw)
    except TriggerSkillsRefused as exc:
        raise SessionControlError(str(exc), code=exc.code) from None
    if keys is None:
        return None
    template = child_execution.template_id
    if template not in TRIGGER_LIST_AGENTS:
        raise SessionControlError(
            f"skills is taken only for a worker ({', '.join(sorted(TRIGGER_LIST_AGENTS))}); "
            f"the new session runs {template!r}; nothing was created",
            code="skills_not_for_agent",
        )
    # Lazy: only a create that carries a list pays for the driver and loader imports.
    from kiro_crew.agent_sdk.drivers.acp import provider_label_for_backend
    from kiro_crew.dashboard.handlers._shared import _get_skills

    # shortcut: with no explicit backend pick the scope is read as on a harness
    # that lends no inherited trees, so an unmapped Bot worker that will run on
    # kiro-cli may accept a key it cannot load. Harmless, because every turn holds
    # the list to the session's real scope; read the Bot's harness here if that
    # ever misleads a conductor.
    provider = provider_label_for_backend(backend) if backend is not None else None
    try:
        unknown = await asyncio.to_thread(
            unknown_keys,
            keys,
            _get_skills(state),
            template=template,
            member_id=child_execution.member_id,
            project_dir=project_dir or None,
            provider_type=provider,
        )
    except TriggerSkillsRefused as exc:
        raise SessionControlError(str(exc), code=exc.code, status=503) from None
    if unknown:
        raise SessionControlError(
            "skills names keys the new worker cannot load: "
            f"{', '.join(repr(key) for key in unknown)}. Name each skill by the key "
            "skill_search shows for it; nothing was created",
            code="unknown_skills",
        )
    return keys
