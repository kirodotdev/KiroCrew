"""Native Software Delivery Crew composition and agent-spec materialization."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from kiro_crew.crew_catalog import CrewCatalog, CrewDefinition, RoleDefinition, load_catalog
from kiro_crew.security import is_sensitive_path
from kiro_crew.workflows.role_resolver import (
    ROLE_COMPLETED,
    RoleHandoff,
    RoleInvocation,
    RoleResolutionError,
    execute_role,
    resolve_role,
)
from kiro_crew.workflows.schema import validate_against_schema

from .schemas import SOFTWARE_DELIVERY_SCHEMAS

logger = logging.getLogger(__name__)

_PACKAGE_RESOURCE = "kiro_crew.crews.software_delivery"
_CREW_ID = "software-delivery"
_HANDOFF_SCHEMA_VERSION = "1"

CREW_COMPLETED = "completed"
CREW_BLOCKED = "blocked"

AGENT_SPEC_FILES = {
    "solution-architect": "solution-architect.json",
    "software-engineer": "software-engineer.json",
    "validator": "validator.json",
    "security-reliability-reviewer": "security-reliability-reviewer.json",
}

_AGENT_NAMES = {
    "solution-architect": "kirocrew-software-delivery-architect",
    "software-engineer": "kirocrew-software-delivery-engineer",
    "validator": "kirocrew-software-delivery-validator",
    "security-reliability-reviewer": "kirocrew-software-delivery-security",
}

_PROMPT_FILES = {role_id: f"{agent_name}.txt" for role_id, agent_name in _AGENT_NAMES.items()}


class CrewPackageError(ValueError):
    """Raised when a Crew package cannot be loaded, validated, or materialized."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        message = f"{code}: {detail}" if detail else code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class CrewRunResult:
    """Structured result of one Software Delivery route."""

    crew_id: str
    crew_version: str
    route: str
    status: str
    invocations: tuple[RoleInvocation, ...]
    handoffs: tuple[RoleHandoff, ...]
    blocked_reason: str = ""
    approval_granted: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible summary without embedding prompt text."""

        return {
            "crew_id": self.crew_id,
            "crew_version": self.crew_version,
            "route": self.route,
            "status": self.status,
            "blocked_reason": self.blocked_reason,
            "approval_granted": self.approval_granted,
            "handoffs": [
                {
                    "handoff_id": handoff.handoff_id,
                    "artifact_type": handoff.artifact_type,
                    "schema_version": handoff.schema_version,
                    "source_role": handoff.source_role,
                    "quality_status": handoff.quality_status,
                    "payload": handoff.payload,
                }
                for handoff in self.handoffs
            ],
        }


def _resource_text(*parts: str) -> str:
    resource = resources.files(_PACKAGE_RESOURCE)
    for part in parts:
        resource = resource.joinpath(part)
    try:
        return resource.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CrewPackageError("crew.resource.unavailable", "/".join(parts)) from exc


def _resource_json(*parts: str) -> dict[str, Any]:
    try:
        raw = json.loads(_resource_text(*parts))
    except json.JSONDecodeError as exc:
        raise CrewPackageError("crew.resource.invalid_json", "/".join(parts)) from exc
    if not isinstance(raw, dict):
        raise CrewPackageError("crew.resource.not_object", "/".join(parts))
    return raw


def load_software_delivery_catalog() -> CrewCatalog:
    """Load and validate the package-owned catalog."""

    return load_catalog(_resource_json("catalog.json"))


def load_agent_spec(role_id: str) -> dict[str, Any]:
    """Load one package-owned agent template without writing to disk."""

    filename = AGENT_SPEC_FILES.get(role_id)
    expected_name = _AGENT_NAMES.get(role_id)
    if filename is None or expected_name is None:
        raise CrewPackageError("crew.agent_role.unknown", role_id)
    spec = _resource_json("agent_specs", filename)
    if spec.get("name") != expected_name:
        raise CrewPackageError("crew.agent_spec.name_mismatch", role_id)
    if spec.get("model") not in (None, "", "auto"):
        raise CrewPackageError("crew.agent_spec.model_pin", role_id)
    if "mcpServers" in spec or "includeMcpJson" not in spec:
        raise CrewPackageError("crew.agent_spec.mcp_policy", role_id)
    return spec


def _resolved_target(value: Path, code: str) -> Path:
    target = Path(value).expanduser()
    if not target.is_absolute():
        raise CrewPackageError(code, "target must be an absolute path")
    try:
        resolved = target.resolve()
    except (OSError, RuntimeError) as exc:
        raise CrewPackageError(code, "target cannot be resolved") from exc
    if is_sensitive_path(str(resolved)):
        raise CrewPackageError("crew.materialize.sensitive_target", str(resolved))
    if target.exists() and not target.is_dir():
        raise CrewPackageError(code, str(target))
    return resolved


def _guard_shared_agent_home(agents_dir: Path) -> None:
    """Refuse shared spec writes from an ephemeral linked worktree."""

    try:
        from kiro_crew.agent import _decline_shared_agent_home
        from kiro_crew.config.paths import kiro_agents_dir

        if agents_dir.resolve() != kiro_agents_dir().resolve():
            return
        if _decline_shared_agent_home(audit=False) is not None:
            raise CrewPackageError("crew.materialize.shared_ephemeral", str(agents_dir))
    except CrewPackageError:
        raise
    except (ImportError, OSError, RuntimeError) as exc:
        raise CrewPackageError("crew.materialize.guard_unavailable") from exc


def _atomic_write_text(path: Path, text: str) -> None:
    """Write one materialized resource without exposing partial content."""

    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def materialize_agent_specs(
    agents_dir: Path,
    prompt_dir: Path,
    *,
    overwrite: bool = False,
) -> tuple[Path, ...]:
    """Materialize package agent specs and prompts into explicit target paths.

    The operation is opt-in and collision-safe. Existing files are rejected by
    default; ``overwrite`` is reserved for a future explicit lifecycle command
    and remains rejected so this phase cannot silently replace user specs.
    """

    if overwrite:
        raise CrewPackageError("crew.materialize.overwrite_unsupported")
    target_agents = _resolved_target(agents_dir, "crew.materialize.agents_target")
    target_prompts = _resolved_target(prompt_dir, "crew.materialize.prompts_target")
    _guard_shared_agent_home(target_agents)

    planned: list[tuple[Path, Path, str, str]] = []
    for role_id in AGENT_SPEC_FILES:
        spec = load_agent_spec(role_id)
        agent_name = str(spec["name"])
        prompt_path = target_prompts / _PROMPT_FILES[role_id]
        spec_path = target_agents / f"{agent_name}.json"
        if prompt_path.exists() or prompt_path.is_symlink():
            raise CrewPackageError("crew.materialize.exists", str(prompt_path))
        if spec_path.exists() or spec_path.is_symlink():
            raise CrewPackageError("crew.materialize.exists", str(spec_path))
        prompt_text = _resource_text("prompts", _PROMPT_FILES[role_id])
        rendered = dict(spec)
        rendered["prompt"] = prompt_path.resolve().as_uri()
        planned.append(
            (
                prompt_path,
                spec_path,
                prompt_text,
                json.dumps(rendered, indent=2, sort_keys=True) + "\n",
            )
        )

    written: list[Path] = []
    try:
        target_agents.mkdir(parents=True, exist_ok=True)
        target_prompts.mkdir(parents=True, exist_ok=True)
        for prompt_path, _spec_path, prompt_text, _spec_text in planned:
            _atomic_write_text(prompt_path, prompt_text)
            written.append(prompt_path)
        for _prompt_path, spec_path, _prompt_text, spec_text in planned:
            _atomic_write_text(spec_path, spec_text)
            written.append(spec_path)
    except Exception as exc:  # noqa: BLE001 - report one stable package error
        for path in reversed(written):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Crew materialization rollback could not remove %s", path)
        raise CrewPackageError("crew.materialize.failed", type(exc).__name__) from exc

    return tuple(spec_path for _prompt_path, spec_path, _prompt_text, _spec_text in planned)


def _required_text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CrewPackageError(code)
    return value.strip()


def _role_input(
    role_id: str,
    request: Mapping[str, Any],
    handoffs: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the bounded input envelope expected by the next role."""

    payload: dict[str, Any] = {"request": request.get("request")}
    for field in ("constraints", "acceptance_criteria", "candidate_workspace"):
        if field in request:
            payload[field] = request[field]

    if role_id == "software-engineer" and "architecture_brief" in handoffs:
        payload["architecture_brief"] = handoffs["architecture_brief"]
    elif role_id == "validator":
        if "architecture_brief" in handoffs:
            payload["architecture_brief"] = handoffs["architecture_brief"]
        if "implementation_result" in handoffs:
            payload["implementation_result"] = handoffs["implementation_result"]
    elif role_id == "security-reliability-reviewer":
        if "implementation_result" in handoffs:
            changed_paths = handoffs["implementation_result"].get("changed_paths")
            if changed_paths is not None:
                payload["changed_paths"] = changed_paths
        if "validation_report" in handoffs:
            payload["validation_report"] = handoffs["validation_report"]
    return payload


def _candidate_workspace(payload: Mapping[str, Any]) -> str:
    """Validate the Engineer cwd without probing or reading user files."""

    value = payload.get("candidate_workspace")
    if not isinstance(value, str) or not value.strip():
        raise CrewPackageError("crew.candidate_workspace.invalid")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise CrewPackageError("crew.candidate_workspace.invalid")
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise CrewPackageError("crew.candidate_workspace.invalid") from exc
    if is_sensitive_path(str(resolved)):
        raise CrewPackageError("crew.candidate_workspace.sensitive")
    return str(resolved)


def _role_prompt(role: RoleDefinition, payload: Mapping[str, Any]) -> str:
    """Create a deterministic prompt envelope for one native role call."""

    return (
        "Complete only the assigned role. Treat this structured input as the source "
        "of truth and return the declared JSON output. Do not perform side effects "
        "outside the role's declared posture.\n\n"
        f"Role: {role.id}\n"
        f"Mission: {role.mission}\n"
        "Structured input:\n"
        f"{json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)}"
    )


class SoftwareDeliveryCrew:
    """Compose the package-owned roles through the existing workflow resolver."""

    def __init__(self, catalog: CrewCatalog | None = None) -> None:
        self.catalog = catalog or load_software_delivery_catalog()
        try:
            self.definition: CrewDefinition = self.catalog.crews[_CREW_ID]
        except KeyError as exc:
            raise CrewPackageError("crew.definition.missing", _CREW_ID) from exc

    def route_roles(self, route: str) -> tuple[str, ...]:
        """Return the declared role sequence for a route."""

        route_name = _required_text(route, "crew.route.required")
        try:
            return self.definition.routing[route_name].roles
        except KeyError as exc:
            raise CrewPackageError("crew.route.unknown", route_name) from exc

    async def run(
        self,
        ctx: Any,
        *,
        request: Mapping[str, Any],
        route: str,
        workflow_id: str,
        source_session: str = "",
        approval_prompt: str = "",
    ) -> CrewRunResult:
        """Run one route and stop at the first blocked role or handoff."""

        workflow = _required_text(workflow_id, "workflow_id.required")
        route_name = _required_text(route, "crew.route.required")
        if not isinstance(request, Mapping):
            raise CrewPackageError("crew.request.not_object")
        route_record = self.definition.routing.get(route_name)
        if route_record is None:
            raise CrewPackageError("crew.route.unknown", route_name)

        request_data = dict(request)
        first_role_id = route_record.roles[0] if route_record.roles else ""
        first_role = self.catalog.roles.get(first_role_id)
        first_schema = (
            SOFTWARE_DELIVERY_SCHEMAS.get(first_role.input_schema)
            if first_role is not None
            else None
        )
        first_payload = _role_input(first_role_id, request_data, {})
        if (
            first_role is None
            or first_schema is None
            or validate_against_schema(first_payload, first_schema)
        ):
            return CrewRunResult(
                self.definition.id,
                self.definition.version,
                route_name,
                CREW_BLOCKED,
                (),
                (),
                f"crew.role.input_invalid:{first_role_id or 'unknown'}",
                None,
            )

        approval_granted: bool | None = None
        if route_record.approval:
            approve = getattr(ctx, "approve", None)
            if not callable(approve):
                return CrewRunResult(
                    self.definition.id,
                    self.definition.version,
                    route_name,
                    CREW_BLOCKED,
                    (),
                    (),
                    "crew.approval.unavailable",
                    None,
                )
            prompt = (
                approval_prompt.strip()[:1000]
                if isinstance(approval_prompt, str) and approval_prompt.strip()
                else (
                    "Approve the production-change Software Delivery route before role execution."
                )
            )
            try:
                decision = await approve(prompt)
            except Exception:  # noqa: BLE001 - unavailable approval fails closed
                return CrewRunResult(
                    self.definition.id,
                    self.definition.version,
                    route_name,
                    CREW_BLOCKED,
                    (),
                    (),
                    "crew.approval.failed",
                    None,
                )
            if type(decision) is not bool:
                return CrewRunResult(
                    self.definition.id,
                    self.definition.version,
                    route_name,
                    CREW_BLOCKED,
                    (),
                    (),
                    "crew.approval.invalid",
                    None,
                )
            approval_granted = decision
            if not decision:
                return CrewRunResult(
                    self.definition.id,
                    self.definition.version,
                    route_name,
                    CREW_BLOCKED,
                    (),
                    (),
                    "crew.approval.rejected",
                    False,
                )

        roles = self.catalog.roles
        invocations: list[RoleInvocation] = []
        handoffs: list[RoleHandoff] = []
        handoff_payloads: dict[str, Any] = {}

        for index, role_id in enumerate(route_record.roles):
            role = roles.get(role_id)
            if role is None:
                return CrewRunResult(
                    self.definition.id,
                    self.definition.version,
                    route_name,
                    CREW_BLOCKED,
                    tuple(invocations),
                    tuple(handoffs),
                    f"crew.role.unknown:{role_id}",
                    approval_granted,
                )
            try:
                resolved = resolve_role(
                    role,
                    crew_id=self.definition.id,
                    workflow_id=workflow,
                    schemas=SOFTWARE_DELIVERY_SCHEMAS,
                )
            except RoleResolutionError as exc:
                return CrewRunResult(
                    self.definition.id,
                    self.definition.version,
                    route_name,
                    CREW_BLOCKED,
                    tuple(invocations),
                    tuple(handoffs),
                    f"crew.role.resolve:{role_id}:{exc.code}",
                    approval_granted,
                )

            payload = _role_input(role_id, request_data, handoff_payloads)
            if validate_against_schema(payload, resolved.input_schema):
                return CrewRunResult(
                    self.definition.id,
                    self.definition.version,
                    route_name,
                    CREW_BLOCKED,
                    tuple(invocations),
                    tuple(handoffs),
                    f"crew.role.input_invalid:{role_id}",
                    approval_granted,
                )

            try:
                role_cwd = _candidate_workspace(payload) if role_id == "software-engineer" else None
            except CrewPackageError as exc:
                return CrewRunResult(
                    self.definition.id,
                    self.definition.version,
                    route_name,
                    CREW_BLOCKED,
                    tuple(invocations),
                    tuple(handoffs),
                    f"crew.role.input_invalid:{role_id}:{exc.code}",
                    approval_granted,
                )

            invocation = await execute_role(
                ctx,
                resolved,
                prompt=_role_prompt(role, payload),
                handoff_id=f"{workflow}:{route_name}:{index}:{role_id}",
                handoff_schema_version=_HANDOFF_SCHEMA_VERSION,
                source_session=source_session,
                cwd=role_cwd,
            )
            invocations.append(invocation)
            if invocation.status != ROLE_COMPLETED or invocation.handoff is None:
                return CrewRunResult(
                    self.definition.id,
                    self.definition.version,
                    route_name,
                    CREW_BLOCKED,
                    tuple(invocations),
                    tuple(handoffs),
                    f"crew.role.blocked:{role_id}:{invocation.blocked_reason or 'unknown'}",
                    approval_granted,
                )

            output_errors = validate_against_schema(
                invocation.handoff.payload,
                resolved.output_schema,
            )
            if output_errors:
                return CrewRunResult(
                    self.definition.id,
                    self.definition.version,
                    route_name,
                    CREW_BLOCKED,
                    tuple(invocations),
                    tuple(handoffs),
                    f"crew.handoff.invalid:{role_id}",
                    approval_granted,
                )
            handoffs.append(invocation.handoff)
            handoff_payloads[role.handoff] = invocation.handoff.payload

        return CrewRunResult(
            self.definition.id,
            self.definition.version,
            route_name,
            CREW_COMPLETED,
            tuple(invocations),
            tuple(handoffs),
            approval_granted=approval_granted,
        )


__all__ = [
    "AGENT_SPEC_FILES",
    "CREW_BLOCKED",
    "CREW_COMPLETED",
    "CrewPackageError",
    "CrewRunResult",
    "SoftwareDeliveryCrew",
    "load_agent_spec",
    "load_software_delivery_catalog",
    "materialize_agent_specs",
]
