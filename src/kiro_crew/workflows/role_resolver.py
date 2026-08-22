"""Workflow-local Role resolver (Phase 1).

The resolver is intentionally a host/native-workflow helper, not a new ``ctx``
method. It resolves a validated :class:`RoleDefinition` to existing agent and
JSON-schema resources, then delegates execution to ``ctx.agent()`` so the normal
budget, retry, event, checkpoint, audit, and resume paths remain authoritative.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from kiro_crew.crew_catalog import RoleDefinition

from . import AgentResult, WorkflowContext

ROLE_COMPLETED = "completed"
ROLE_BLOCKED = "blocked"


class RoleResolutionError(ValueError):
    """Raised before execution when a role or its resources cannot be resolved."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        message = f"{code}: {detail}" if detail else code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ResolvedRole:
    """Execution-ready role metadata and the schemas it references."""

    role: RoleDefinition
    crew_id: str
    workflow_id: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    event_label: str
    event_phase: str


@dataclass(frozen=True, slots=True)
class RoleHandoff:
    """A structured, validated output envelope returned to the owning workflow."""

    handoff_id: str
    crew_id: str
    workflow_id: str
    source_role: str
    source_session: str
    artifact_type: str
    schema_version: str
    payload: Any
    created_at: str
    quality_status: str


@dataclass(frozen=True, slots=True)
class RoleInvocation:
    """Machine-readable result of one role execution."""

    resolved: ResolvedRole
    status: str
    result: AgentResult
    handoff: RoleHandoff | None
    blocked_reason: str = ""


def _required_identity(value: str, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RoleResolutionError(code)
    return value.strip()


def _lookup_schema(
    schemas: Mapping[str, Mapping[str, Any]], reference: str, field: str
) -> dict[str, Any]:
    schema = schemas.get(reference)
    if not isinstance(schema, Mapping):
        raise RoleResolutionError(f"role.{field}.unavailable", reference)
    return dict(schema)


def resolve_role(
    role: RoleDefinition,
    *,
    crew_id: str,
    workflow_id: str,
    schemas: Mapping[str, Mapping[str, Any]],
) -> ResolvedRole:
    """Resolve one validated role to its declared agent and schema resources.

    Resolution is fail-closed: both the input and output schema references must
    exist before a model call is attempted. This function grants no tools or
    permissions; governance remains the authority for effective capabilities.
    """

    if not isinstance(role, RoleDefinition):
        raise RoleResolutionError("role.invalid")
    crew = _required_identity(crew_id, "crew_id.required")
    workflow = _required_identity(workflow_id, "workflow_id.required")
    if not isinstance(schemas, Mapping):
        raise RoleResolutionError("schemas.invalid")

    input_schema = _lookup_schema(schemas, role.input_schema, "input_schema")
    output_schema = _lookup_schema(schemas, role.output_schema, "output_schema")
    return ResolvedRole(
        role=role,
        crew_id=crew,
        workflow_id=workflow,
        input_schema=input_schema,
        output_schema=output_schema,
        event_label=f"crew:{crew} workflow:{workflow} role:{role.id}@{role.version}",
        event_phase=f"role:{role.id}",
    )


def build_role_prompt(resolved: ResolvedRole, prompt: str) -> str:
    """Add bounded role context without changing the caller's task semantics."""

    if not isinstance(prompt, str) or not prompt.strip():
        raise RoleResolutionError("role.prompt.required")
    role = resolved.role
    skills = ", ".join(role.skills) or "(none)"
    scopes = ", ".join(role.tool_scopes) or "(none)"
    gates = ", ".join(role.quality_gates) or "(none)"
    return (
        "[Kiro Crew role]\n"
        f"Role: {role.id}\n"
        f"Role contract: {role.version}\n"
        f"Mission: {role.mission}\n"
        f"Profile: {role.profile}\n"
        f"Declared skills: {skills}\n"
        f"Declared tool scopes: {scopes}\n"
        f"Quality gates: {gates}\n\n"
        f"Task:\n{prompt.strip()}"
    )


async def execute_role(
    ctx: WorkflowContext,
    resolved: ResolvedRole,
    *,
    prompt: str,
    handoff_id: str,
    handoff_schema_version: str,
    source_session: str = "",
    model: str | None = None,
    effort: str | None = None,
    cwd: str | None = None,
    session: str | None = None,
    nudge: dict[str, Any] | None = None,
) -> RoleInvocation:
    """Execute a role through ``ctx.agent`` and return a structured handoff.

    ``BudgetExceeded`` is deliberately not caught: it remains the workflow
    runner's one control-flow exception. All other agent failures are already
    normalized by ``ctx.agent`` to ``None`` and become a machine-readable blocked
    invocation here.
    """

    handoff = _required_identity(handoff_id, "handoff_id.required")
    schema_version = _required_identity(handoff_schema_version, "handoff_schema_version.required")
    source = source_session.strip() if isinstance(source_session, str) else ""
    role_prompt = build_role_prompt(resolved, prompt)
    result = await ctx.agent(
        role_prompt,
        label=resolved.event_label,
        phase=resolved.event_phase,
        schema=resolved.output_schema,
        model=model,
        agent=resolved.role.agent,
        effort=effort,
        cwd=cwd,
        session=session,
        nudge=nudge,
    )
    if result is None:
        return RoleInvocation(
            resolved=resolved,
            status=ROLE_BLOCKED,
            result=None,
            handoff=None,
            blocked_reason="agent_returned_no_result",
        )

    return RoleInvocation(
        resolved=resolved,
        status=ROLE_COMPLETED,
        result=result,
        handoff=RoleHandoff(
            handoff_id=handoff,
            crew_id=resolved.crew_id,
            workflow_id=resolved.workflow_id,
            source_role=resolved.role.id,
            source_session=source,
            artifact_type=resolved.role.handoff,
            schema_version=schema_version,
            payload=result,
            created_at=ctx.now,
            quality_status="schema_validated",
        ),
    )


__all__ = [
    "ROLE_BLOCKED",
    "ROLE_COMPLETED",
    "ResolvedRole",
    "RoleHandoff",
    "RoleInvocation",
    "RoleResolutionError",
    "build_role_prompt",
    "execute_role",
    "resolve_role",
]
