"""Versioned Role/Crew contracts for the Phase 0 catalog.

This module deliberately contains no execution, provider, App Kit, or domain-app
integration. It validates JSON-compatible records so later workflow integration
can consume an explicit contract without conflating it with ``agent.role_models``
or Issue Radar's domain-owned ``crew_id``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

CATALOG_SCHEMA_VERSION = 1

ROLE_SIDE_EFFECTS = frozenset({"none", "candidate-write", "approval-required"})
TARGET_WRITE_POLICIES = frozenset({"none", "candidate_workspace_only", "human_approval_only"})
PUSH_POLICIES = frozenset({"none", "human_approval_only"})

_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


class CatalogValidationError(ValueError):
    """Raised when a catalog, role, or Crew record is not safe to load."""

    def __init__(self, errors: Iterable[str]) -> None:
        normalized = tuple(str(error) for error in errors if str(error))
        self.errors = normalized or ("catalog.invalid",)
        super().__init__("catalog validation failed: " + "; ".join(self.errors))


@dataclass(frozen=True, slots=True)
class RoleDefinition:
    """A bounded professional capability contract."""

    id: str
    version: str
    mission: str
    agent: str
    skills: tuple[str, ...]
    tool_scopes: tuple[str, ...]
    profile: str
    input_schema: str
    output_schema: str
    handoff: str
    quality_gates: tuple[str, ...]
    side_effects: str


@dataclass(frozen=True, slots=True)
class CrewRoute:
    """One named route through a Crew's role composition."""

    name: str
    roles: tuple[str, ...]
    approval: bool


@dataclass(frozen=True, slots=True)
class CrewDefinition:
    """A validated composition of roles, routes, handoffs, and policies."""

    id: str
    version: str
    roles: tuple[str, ...]
    routing: Mapping[str, CrewRoute]
    handoffs: tuple[str, ...]
    policies: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class CrewCatalog:
    """The validated role and Crew definitions for one catalog document."""

    schema: int
    roles: Mapping[str, RoleDefinition]
    crews: Mapping[str, CrewDefinition]


def _required_text(raw: Mapping[str, Any], key: str, errors: list[str], prefix: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{prefix}.{key}.required")
        return ""
    return value.strip()


def _text_list(
    raw: Mapping[str, Any],
    key: str,
    errors: list[str],
    prefix: str,
    *,
    required: bool = True,
) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list):
        errors.append(f"{prefix}.{key}.not_list")
        return ()
    if required and not value:
        errors.append(f"{prefix}.{key}.empty")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{prefix}.{key}[{index}].required")
            continue
        result.append(item.strip())
    return tuple(result)


def _validate_id(value: str, path: str, errors: list[str]) -> None:
    if value and not _ID_RE.fullmatch(value):
        errors.append(f"{path}.invalid")


def _validate_version(value: str, path: str, errors: list[str]) -> None:
    if value and not _VERSION_RE.fullmatch(value):
        errors.append(f"{path}.invalid")


def _duplicate_values(values: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def validate_role_definition(raw: object) -> list[str]:
    """Return stable error codes for one role record."""

    if not isinstance(raw, Mapping):
        return ["role.not_object"]

    prefix = "role"
    errors: list[str] = []
    role_id = _required_text(raw, "id", errors, prefix)
    version = _required_text(raw, "version", errors, prefix)
    for key, value in (("id", role_id), ("version", version)):
        if key == "id":
            _validate_id(value, f"{prefix}.{key}", errors)
        else:
            _validate_version(value, f"{prefix}.{key}", errors)

    for key in (
        "mission",
        "agent",
        "profile",
        "input_schema",
        "output_schema",
        "handoff",
    ):
        _required_text(raw, key, errors, prefix)

    _text_list(raw, "skills", errors, prefix, required=False)
    _text_list(raw, "tool_scopes", errors, prefix, required=False)
    _text_list(raw, "quality_gates", errors, prefix, required=False)

    side_effects = _required_text(raw, "side_effects", errors, prefix)
    if side_effects and side_effects not in ROLE_SIDE_EFFECTS:
        errors.append(f"{prefix}.side_effects.invalid")

    return errors


def parse_role_definition(raw: object) -> RoleDefinition:
    """Validate and parse one role record."""

    errors = validate_role_definition(raw)
    if errors:
        raise CatalogValidationError(errors)
    assert isinstance(raw, Mapping)  # narrowed by the validator above
    return RoleDefinition(
        id=str(raw["id"]).strip(),
        version=str(raw["version"]).strip(),
        mission=str(raw["mission"]).strip(),
        agent=str(raw["agent"]).strip(),
        skills=_text_list(raw, "skills", [], "role", required=False),
        tool_scopes=_text_list(raw, "tool_scopes", [], "role", required=False),
        profile=str(raw["profile"]).strip(),
        input_schema=str(raw["input_schema"]).strip(),
        output_schema=str(raw["output_schema"]).strip(),
        handoff=str(raw["handoff"]).strip(),
        quality_gates=_text_list(raw, "quality_gates", [], "role", required=False),
        side_effects=str(raw["side_effects"]).strip(),
    )


def validate_crew_definition(
    raw: object,
    role_definitions: Mapping[str, RoleDefinition] | None = None,
) -> list[str]:
    """Return stable error codes for one Crew record.

    ``role_definitions`` is optional so a standalone Crew shape can be checked;
    when supplied, role references and side-effect policy combinations are also
    checked.
    """

    if not isinstance(raw, Mapping):
        return ["crew.not_object"]

    prefix = "crew"
    errors: list[str] = []
    crew_id = _required_text(raw, "id", errors, prefix)
    version = _required_text(raw, "version", errors, prefix)
    _validate_id(crew_id, f"{prefix}.id", errors)
    _validate_version(version, f"{prefix}.version", errors)

    crew_roles = _text_list(raw, "roles", errors, prefix)
    for role_id in crew_roles:
        _validate_id(role_id, f"{prefix}.roles", errors)
    for duplicate in sorted(_duplicate_values(crew_roles)):
        errors.append(f"{prefix}.roles.duplicate:{duplicate}")

    if role_definitions is not None:
        for role_id in crew_roles:
            if role_id not in role_definitions:
                errors.append(f"{prefix}.roles.unknown:{role_id}")

    routing = raw.get("routing")
    if not isinstance(routing, Mapping):
        errors.append(f"{prefix}.routing.not_object")
        routing = {}
    elif not routing:
        errors.append(f"{prefix}.routing.empty")

    for route_name, route_raw in routing.items():
        if not isinstance(route_name, str) or not route_name.strip():
            errors.append(f"{prefix}.routing.name.required")
            continue
        route_path = f"{prefix}.routing.{route_name}"
        if not isinstance(route_raw, Mapping):
            errors.append(f"{route_path}.not_object")
            continue
        route_roles = _text_list(route_raw, "roles", errors, route_path)
        for role_id in route_roles:
            if role_id not in crew_roles:
                errors.append(f"{route_path}.roles.not_declared:{role_id}")
        for duplicate in sorted(_duplicate_values(route_roles)):
            errors.append(f"{route_path}.roles.duplicate:{duplicate}")
        approval = route_raw.get("approval")
        if not isinstance(approval, bool):
            errors.append(f"{route_path}.approval.required")

        if role_definitions is not None and approval is False:
            for role_id in route_roles:
                role = role_definitions.get(role_id)
                if role is not None and role.side_effects == "approval-required":
                    errors.append(f"{route_path}.approval.required_for:{role_id}")

    handoffs = _text_list(raw, "handoffs", errors, prefix)
    for duplicate in sorted(_duplicate_values(handoffs)):
        errors.append(f"{prefix}.handoffs.duplicate:{duplicate}")

    policies = raw.get("policies")
    if not isinstance(policies, Mapping):
        errors.append(f"{prefix}.policies.not_object")
        policies = {}
    target_write = _required_text(policies, "target_write", errors, f"{prefix}.policies")
    push = _required_text(policies, "push", errors, f"{prefix}.policies")
    if target_write and target_write not in TARGET_WRITE_POLICIES:
        errors.append(f"{prefix}.policies.target_write.invalid")
    if push and push not in PUSH_POLICIES:
        errors.append(f"{prefix}.policies.push.invalid")

    if role_definitions is not None:
        referenced_roles = [
            role_definitions[role_id] for role_id in crew_roles if role_id in role_definitions
        ]
        has_candidate_write = any(
            role.side_effects == "candidate-write" for role in referenced_roles
        )
        has_approval_required = any(
            role.side_effects == "approval-required" for role in referenced_roles
        )
        if has_candidate_write and target_write == "none":
            errors.append(f"{prefix}.policies.target_write.insufficient")
        if has_approval_required and target_write != "human_approval_only":
            errors.append(f"{prefix}.policies.target_write.approval_required")

    return errors


def parse_crew_definition(
    raw: object,
    role_definitions: Mapping[str, RoleDefinition] | None = None,
) -> CrewDefinition:
    """Validate and parse one Crew record."""

    errors = validate_crew_definition(raw, role_definitions)
    if errors:
        raise CatalogValidationError(errors)
    assert isinstance(raw, Mapping)  # narrowed by the validator above

    raw_routing = raw["routing"]
    assert isinstance(raw_routing, Mapping)
    routing: dict[str, CrewRoute] = {}
    for route_name, route_raw in raw_routing.items():
        assert isinstance(route_name, str)
        assert isinstance(route_raw, Mapping)
        route_roles = _text_list(route_raw, "roles", [], f"crew.routing.{route_name}")
        routing[route_name] = CrewRoute(
            name=route_name.strip(),
            roles=route_roles,
            approval=bool(route_raw["approval"]),
        )

    raw_policies = raw["policies"]
    assert isinstance(raw_policies, Mapping)
    policies = {
        "target_write": str(raw_policies["target_write"]).strip(),
        "push": str(raw_policies["push"]).strip(),
    }
    return CrewDefinition(
        id=str(raw["id"]).strip(),
        version=str(raw["version"]).strip(),
        roles=_text_list(raw, "roles", [], "crew"),
        routing=MappingProxyType(routing),
        handoffs=_text_list(raw, "handoffs", [], "crew"),
        policies=MappingProxyType(policies),
    )


def _record_list(raw: Mapping[str, Any], key: str, errors: list[str]) -> list[object]:
    value = raw.get(key)
    if not isinstance(value, list):
        errors.append(f"catalog.{key}.not_list")
        return []
    return value


def _prefix_errors(prefix: str, errors: Iterable[str]) -> list[str]:
    return [f"{prefix}.{error}" for error in errors]


def load_catalog(raw: object) -> CrewCatalog:
    """Validate and parse a complete catalog document."""

    if not isinstance(raw, Mapping):
        raise CatalogValidationError(["catalog.not_object"])

    errors: list[str] = []
    schema = raw.get("schema")
    if type(schema) is not int or schema != CATALOG_SCHEMA_VERSION:
        errors.append("catalog.schema.unsupported")

    role_records = _record_list(raw, "roles", errors)
    crew_records = _record_list(raw, "crews", errors)
    roles: dict[str, RoleDefinition] = {}
    for index, record in enumerate(role_records):
        role_errors = validate_role_definition(record)
        if role_errors:
            errors.extend(_prefix_errors(f"roles[{index}]", role_errors))
            continue
        assert isinstance(record, Mapping)
        role_id = str(record["id"]).strip()
        if role_id in roles:
            errors.append(f"roles[{index}].role.id.duplicate:{role_id}")
            continue
        roles[role_id] = parse_role_definition(record)

    crews: dict[str, CrewDefinition] = {}
    for index, record in enumerate(crew_records):
        crew_errors = validate_crew_definition(record, roles)
        if crew_errors:
            errors.extend(_prefix_errors(f"crews[{index}]", crew_errors))
            continue
        assert isinstance(record, Mapping)
        crew_id = str(record["id"]).strip()
        if crew_id in crews:
            errors.append(f"crews[{index}].crew.id.duplicate:{crew_id}")
            continue
        crews[crew_id] = parse_crew_definition(record, roles)

    if errors:
        raise CatalogValidationError(errors)
    return CrewCatalog(
        schema=CATALOG_SCHEMA_VERSION,
        roles=MappingProxyType(roles),
        crews=MappingProxyType(crews),
    )


__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "PUSH_POLICIES",
    "ROLE_SIDE_EFFECTS",
    "TARGET_WRITE_POLICIES",
    "CatalogValidationError",
    "CrewCatalog",
    "CrewDefinition",
    "CrewRoute",
    "RoleDefinition",
    "load_catalog",
    "parse_crew_definition",
    "parse_role_definition",
    "validate_crew_definition",
    "validate_role_definition",
]
