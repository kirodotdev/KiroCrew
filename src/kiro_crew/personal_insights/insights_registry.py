from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from kiro_crew.personal_insights.insights_ontology import (
    BehaviorKey,
    is_known_predicate,
    is_known_subject,
    is_valid_scope,
    polarity_compatible,
)
from kiro_crew.personal_insights.insights_placeholders import (
    declared_literal_overlaps_placeholder,
)

REGISTRY_SCHEMA_VERSION: Final[str] = "kiro.personal-insights.capability/3.0"
REGISTRY_DIGEST: Final[str] = "36338e05a7f7672112381cb784eb9928caaabca92380592cbad7e094715cc25d"
REGISTRY_PATH: Final[Path] = (
    Path(__file__).resolve().parent / "data" / "capability-registry-p1.json"
)

KNOWN_PLATFORMS: Final[frozenset[str]] = frozenset({"linux", "darwin", "windows"})

DOCUMENTATION_SOURCES: Final[frozenset[str]] = frozenset(
    {
        "docs/system-specs/modules/memory-skills-hooks.md",
        "docs/system-specs/modules/learn-cron-dashboard.md",
        "docs/system-specs/modules/cli.md",
    }
)

UNDO_NONE: Final[str] = "undo.none"

VERIFICATION_TEMPLATE_KINDS: Final[dict[str, str]] = {
    "verify.future-observation.context-setup": "future_observation",
    "verify.state-readback.lesson": "state_readback",
    "verify.state-readback.steering": "state_readback",
    "verify.read-only-argv.app-list": "read_only_argv",
    "verify.future-observation.no-action": "future_observation",
}

CLASS_PROMPT: Final[str] = "prompt"
CLASS_LESSON: Final[str] = "lesson_proposal"
CLASS_STEERING: Final[str] = "steering_patch"
CLASS_EXISTING: Final[str] = "existing_capability"
CLASSES: Final[frozenset[str]] = frozenset(
    {CLASS_PROMPT, CLASS_LESSON, CLASS_STEERING, CLASS_EXISTING}
)

TEMPLATE_KINDS: Final[frozenset[str]] = frozenset(
    {"text", "structured_tool", "static_argv", "unified_diff"}
)
RISK_CLASSES: Final[frozenset[str]] = frozenset({"copy_only", "local_state_proposal"})
EFFORT_RANKS: Final[frozenset[str]] = frozenset({"low", "medium", "high"})
REVERSIBILITY_RANKS: Final[frozenset[str]] = frozenset(
    {"fully_reversible", "reversible_with_undo", "irreversible"}
)
LATENCY_RANKS: Final[frozenset[str]] = frozenset({"immediate", "deferred", "manual"})
PREREQUISITE_CODES: Final[frozenset[str]] = frozenset({"repository_scope", "base_digest"})

_DYNAMIC_ARGV_MARKERS: Final[tuple[str, ...]] = ("{", "}", "$", "<", ">")

_TOP_FIELDS: Final[frozenset[str]] = frozenset(
    {"schema_version", "minimum_compatible_version", "capabilities"}
)
_ENTRY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "capability_id",
        "behavior_keys",
        "class",
        "selection_priority",
        "description",
        "template",
        "prerequisites",
        "supported_platforms",
        "implementation_effort",
        "reversibility",
        "verification_latency",
        "risk_class",
        "verification_template",
        "undo_template",
        "documentation_source",
        "version",
    }
)
_TEMPLATE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "kind",
        "template_id",
        "template_version",
        "declared_literals",
        "max_output_bytes",
        "argv_tokens",
    }
)


class RegistryError(Exception):
    pass


@dataclass(frozen=True)
class Template:
    kind: str
    template_id: str
    template_version: str
    declared_literals: tuple[str, ...]
    max_output_bytes: int
    argv_tokens: tuple[str, ...]


@dataclass(frozen=True)
class Capability:
    capability_id: str
    behavior_keys: tuple[str, ...]
    cls: str
    selection_priority: int
    description: str
    template: Template
    prerequisites: tuple[str, ...]
    supported_platforms: tuple[str, ...]
    implementation_effort: str
    reversibility: str
    verification_latency: str
    risk_class: str
    verification_template: str
    undo_template: str
    documentation_source: str
    version: str


@dataclass(frozen=True)
class Registry:
    schema_version: str
    minimum_compatible_version: str
    capabilities: tuple[Capability, ...]

    def by_id(self, capability_id: str) -> Capability:
        for capability in self.capabilities:
            if capability.capability_id == capability_id:
                return capability
        raise RegistryError(f"unknown capability id: {capability_id}")


def _reject_unknown(raw: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    extra = set(raw) - allowed
    if extra:
        raise RegistryError(f"unknown field(s) in {where}: {sorted(extra)}")


def _require_int(value: Any, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RegistryError(f"{where} must be an integer")
    return value


def _require_list(value: Any, where: str) -> list:
    if not isinstance(value, list):
        raise RegistryError(f"{where} must be a list")
    return value


def _require_non_empty_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or value == "":
        raise RegistryError(f"{where} must be a non-empty string")
    return value


def _validate_behavior_key_string(key: str) -> None:
    parts = key.split("|")
    if len(parts) != 4:
        raise RegistryError(f"malformed behavior key: {key}")
    subject, predicate, polarity, scope = parts
    if not is_known_subject(subject):
        raise RegistryError(f"unknown behavior key subject: {subject}")
    if not is_known_predicate(predicate):
        raise RegistryError(f"unknown behavior key predicate: {predicate}")
    if not polarity_compatible(predicate, polarity):
        raise RegistryError(f"incompatible behavior key polarity: {key}")
    if not is_valid_scope(scope):
        raise RegistryError(f"invalid behavior key scope: {scope}")


def _parse_template(raw: dict[str, Any]) -> Template:
    _reject_unknown(raw, _TEMPLATE_FIELDS, "template")
    kind = raw["kind"]
    if kind not in TEMPLATE_KINDS:
        raise RegistryError(f"unknown template kind: {kind}")
    _require_non_empty_str(raw["template_id"], "template_id")
    _require_non_empty_str(raw["template_version"], "template_version")
    declared_literals = tuple(_require_list(raw["declared_literals"], "declared_literals"))
    for literal in declared_literals:
        if not isinstance(literal, str):
            raise RegistryError("declared literal must be a string")
        if declared_literal_overlaps_placeholder(literal):
            raise RegistryError("declared literal overlaps a placeholder marker")
    argv_tokens = tuple(_require_list(raw["argv_tokens"], "argv_tokens"))
    for token in argv_tokens:
        if not isinstance(token, str) or token == "":
            raise RegistryError("empty or non-string argv token is rejected")
        if any(marker in token for marker in _DYNAMIC_ARGV_MARKERS):
            raise RegistryError("dynamic argv token source is rejected")
    max_output_bytes = _require_int(raw["max_output_bytes"], "max_output_bytes")
    if max_output_bytes < 0:
        raise RegistryError("max_output_bytes must not be negative")
    if kind == "static_argv":
        if not argv_tokens:
            raise RegistryError("static_argv template requires argv tokens")
        if max_output_bytes <= 0:
            raise RegistryError("static_argv template requires a positive output bound")
    if kind != "static_argv" and argv_tokens:
        raise RegistryError("only static_argv templates may declare argv tokens")
    return Template(
        kind=kind,
        template_id=raw["template_id"],
        template_version=raw["template_version"],
        declared_literals=declared_literals,
        max_output_bytes=max_output_bytes,
        argv_tokens=argv_tokens,
    )


def _validate_verification_undo(
    cls: str,
    risk_class: str,
    template_kind: str,
    verification_template: str,
    undo_template: str,
) -> None:
    verification_kind = verification_kind_for_template(verification_template)
    if template_kind == "static_argv" and verification_kind != "read_only_argv":
        raise RegistryError("static_argv entry requires read_only_argv verification")
    if risk_class == "local_state_proposal":
        if undo_template == UNDO_NONE or not undo_template:
            raise RegistryError("local_state_proposal entry requires an exact undo template")


def verification_kind_for_template(verification_template: str) -> str:
    kind = VERIFICATION_TEMPLATE_KINDS.get(verification_template)
    if kind is None:
        raise RegistryError(f"unknown verification template: {verification_template}")
    return kind


def _parse_capability(raw: dict[str, Any]) -> Capability:
    _reject_unknown(raw, _ENTRY_FIELDS, "capability")
    if raw["schema_version"] != REGISTRY_SCHEMA_VERSION:
        raise RegistryError("capability schema version mismatch")
    _require_non_empty_str(raw["capability_id"], "capability_id")
    _require_non_empty_str(raw["description"], "description")
    _require_non_empty_str(raw["verification_template"], "verification_template")
    _require_non_empty_str(raw["undo_template"], "undo_template")
    _require_non_empty_str(raw["version"], "version")
    documentation_source = _require_non_empty_str(
        raw["documentation_source"], "documentation_source"
    )
    if documentation_source not in DOCUMENTATION_SOURCES:
        raise RegistryError(f"unknown documentation source: {documentation_source}")
    cls = raw["class"]
    if cls not in CLASSES:
        raise RegistryError(f"unknown capability class: {cls}")
    behavior_keys = tuple(_require_list(raw["behavior_keys"], "behavior_keys"))
    for key in behavior_keys:
        if not isinstance(key, str):
            raise RegistryError("behavior key must be a string")
        _validate_behavior_key_string(key)
    prerequisites = tuple(_require_list(raw["prerequisites"], "prerequisites"))
    for code in prerequisites:
        if code not in PREREQUISITE_CODES:
            raise RegistryError(f"unknown prerequisite: {code}")
    supported_platforms = tuple(_require_list(raw["supported_platforms"], "supported_platforms"))
    for platform in supported_platforms:
        if platform not in KNOWN_PLATFORMS:
            raise RegistryError(f"unknown platform: {platform}")
    if raw["implementation_effort"] not in EFFORT_RANKS:
        raise RegistryError("unknown implementation effort rank")
    if raw["reversibility"] not in REVERSIBILITY_RANKS:
        raise RegistryError("unknown reversibility rank")
    if raw["verification_latency"] not in LATENCY_RANKS:
        raise RegistryError("unknown verification latency rank")
    if raw["risk_class"] not in RISK_CLASSES:
        raise RegistryError("unknown risk class")
    selection_priority = _require_int(raw["selection_priority"], "selection_priority")
    if selection_priority < 0:
        raise RegistryError("selection_priority must not be negative")
    template = _parse_template(raw["template"])
    _validate_verification_undo(
        cls, raw["risk_class"], template.kind, raw["verification_template"], raw["undo_template"]
    )
    return Capability(
        capability_id=raw["capability_id"],
        behavior_keys=behavior_keys,
        cls=cls,
        selection_priority=selection_priority,
        description=raw["description"],
        template=template,
        prerequisites=prerequisites,
        supported_platforms=supported_platforms,
        implementation_effort=raw["implementation_effort"],
        reversibility=raw["reversibility"],
        verification_latency=raw["verification_latency"],
        risk_class=raw["risk_class"],
        verification_template=raw["verification_template"],
        undo_template=raw["undo_template"],
        documentation_source=documentation_source,
        version=raw["version"],
    )


def _validate_priority_uniqueness(capabilities: tuple[Capability, ...]) -> None:
    seen: dict[tuple[str, int], str] = {}
    for capability in capabilities:
        keys = capability.behavior_keys or ("",)
        for key in keys:
            composite = (key, capability.selection_priority)
            if composite in seen:
                raise RegistryError("duplicate selection_priority within one behavior/scope")
            seen[composite] = capability.capability_id


def parse_registry(data: bytes) -> Registry:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise RegistryError("registry is not valid json") from exc
    _reject_unknown(payload, _TOP_FIELDS, "registry")
    if payload["schema_version"] != REGISTRY_SCHEMA_VERSION:
        raise RegistryError("registry schema version mismatch")
    if payload["minimum_compatible_version"] != REGISTRY_SCHEMA_VERSION:
        raise RegistryError("registry minimum compatible version mismatch")
    capabilities = tuple(_parse_capability(raw) for raw in payload["capabilities"])
    ids = [capability.capability_id for capability in capabilities]
    if len(set(ids)) != len(ids):
        raise RegistryError("capability ids must be unique")
    present = {capability.cls for capability in capabilities}
    for required in CLASSES:
        if required not in present:
            raise RegistryError(f"registry missing an entry for class: {required}")
    if not any(capability.capability_id == "no-action" for capability in capabilities):
        raise RegistryError("registry missing the no-action entry")
    _validate_priority_uniqueness(capabilities)
    return Registry(
        schema_version=payload["schema_version"],
        minimum_compatible_version=payload["minimum_compatible_version"],
        capabilities=capabilities,
    )


def _read_registry_bytes() -> bytes:
    sidecar = REGISTRY_PATH.with_suffix(REGISTRY_PATH.suffix + ".sha256")
    if sidecar.exists():
        raise RegistryError("sidecar digest file is forbidden")
    try:
        data = REGISTRY_PATH.read_bytes()
    except FileNotFoundError as exc:
        raise RegistryError("registry file is absent") from exc
    if not data:
        raise RegistryError("registry file is empty")
    return data


def _verify_digest(data: bytes) -> None:
    if hashlib.sha256(data).hexdigest() != REGISTRY_DIGEST:
        raise RegistryError("registry digest mismatch")


def load_registry() -> Registry:
    data = _read_registry_bytes()
    _verify_digest(data)
    return parse_registry(data)


def behavior_key_matches(capability: Capability, key: BehaviorKey) -> bool:
    return key.canonical() in capability.behavior_keys
