from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Final

PROJECTION_INPUT_SCHEMA: Final[str] = "agent-session-intelligence.projection-input/2.0"
MAX_SOURCE_ORDER: Final[int] = (1 << 63) - 2
_ACTOR_MAP: Final[dict[str, str]] = {
    "owner_user": "user",
    "direct_assistant": "assistant",
    "system_injected": "system",
    "tool": "assistant",
    "lifecycle": "system",
    "subagent": "assistant",
}
_EVENT_MAP: Final[dict[str, str]] = {
    "message": "message",
    "tool_call": "tool_call",
    "tool_outcome": "tool_outcome",
    "recovery": "recovery",
    "artifact": "artifact",
    "code_change": "artifact",
    "subagent_dispatch": "spawn",
    "subagent_completion": "lifecycle",
    "other": "lifecycle",
}
_COMPATIBLE: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("owner_user", "message"),
        ("direct_assistant", "message"),
        ("direct_assistant", "artifact"),
        ("direct_assistant", "code_change"),
        ("direct_assistant", "tool_call"),
        ("direct_assistant", "subagent_dispatch"),
        ("tool", "tool_call"),
        ("tool", "tool_outcome"),
        ("tool", "subagent_dispatch"),
        ("tool", "subagent_completion"),
        ("lifecycle", "recovery"),
        ("lifecycle", "other"),
        ("system_injected", "other"),
    }
)
_TOOL_EVENTS: Final[frozenset[str]] = frozenset({"tool_call", "tool_outcome"})
_TOOL_CLASSES: Final[frozenset[str]] = frozenset(
    {
        "file_read",
        "file_write",
        "code_intelligence",
        "search",
        "build",
        "test",
        "version_control",
        "browser",
        "communication",
        "task_management",
        "cloud",
        "other",
        "unknown",
    }
)
_OPERATION_CLASSES: Final[frozenset[str]] = frozenset(
    {
        "read",
        "write",
        "execute",
        "search",
        "create",
        "update",
        "delete",
        "query",
        "dispatch",
        "other",
        "unknown",
    }
)
_STATUSES: Final[frozenset[str]] = frozenset(
    {"success", "error", "timeout", "cancelled", "unknown"}
)
_ERROR_CLASSES: Final[frozenset[str | None]] = frozenset(
    {"schema", "permission", "network", "timeout", "not_found", "validation", "unknown", None}
)
_GROUP_FIELDS: Final[frozenset[str]] = frozenset(
    {"group", "groups", "event_group", "event_groups", "root_event_ids", "event_ids"}
)
_TARGET_RE: Final[re.Pattern[str]] = re.compile(r"te-1-[0-9a-f]{64}\Z")
_OPAQUE_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")


class AdapterError(Exception):
    pass


class AdapterGroupRejected(AdapterError):
    pass


@dataclass(frozen=True)
class SourceRecord:
    source_event_id: str
    actor_class: str
    event_class: str
    visibility_class: str
    sequence: int
    responds_to: str | None = None
    tool_call_ref: str | None = None
    recovery_of: str | None = None
    spawn_of: str | None = None
    tool: dict[str, Any] | None = None
    extra: dict[str, Any] | None = None


@dataclass(frozen=True)
class ClassifiedEvent:
    raw_event_id: str
    actor_class: str
    event_class: str
    genuine_human: bool
    source_order: int
    responds_to_raw_id: str | None
    tool_call_raw_id: str | None
    recovery_of_raw_id: str | None
    spawn_of_raw_id: str | None
    tool: dict[str, Any] | None

    def to_wire(self) -> dict[str, Any]:
        return {
            "raw_event_id": self.raw_event_id,
            "actor_class": self.actor_class,
            "event_class": self.event_class,
            "genuine_human": self.genuine_human,
            "source_order": self.source_order,
            "responds_to_raw_id": self.responds_to_raw_id,
            "tool_call_raw_id": self.tool_call_raw_id,
            "recovery_of_raw_id": self.recovery_of_raw_id,
            "spawn_of_raw_id": self.spawn_of_raw_id,
            "tool": self.tool,
        }


@dataclass(frozen=True)
class ProjectionInput:
    source_id: str
    events: tuple[ClassifiedEvent, ...]
    conformance_expectation: dict[str, int] | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "schema_version": PROJECTION_INPUT_SCHEMA,
            "source_id": self.source_id,
            "events": [event.to_wire() for event in self.events],
            "conformance_expectation": self.conformance_expectation,
        }

    def to_json_bytes(self) -> bytes:
        return json.dumps(self.to_wire(), ensure_ascii=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class ToolMappingCoverage:
    concrete_fraction: float
    other_fraction: float
    unknown_fraction: float
    non_degenerate: bool
    above_floor: bool


@dataclass(frozen=True)
class AdaptResult:
    projection_input: ProjectionInput
    omitted: tuple[tuple[str, str], ...]
    abstained_predicates: tuple[str, ...]
    coverage: ToolMappingCoverage


def _opaque(value: str | None) -> bool:
    return isinstance(value, str) and _OPAQUE_RE.fullmatch(value) is not None


def _optional_nonnegative(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AdapterError(f"{name} must be null or a non-negative integer")
    return value


def _validate_tool(payload: dict[str, Any]) -> dict[str, Any]:
    if _GROUP_FIELDS.intersection(payload):
        raise AdapterGroupRejected("adapter input may not supply group membership")
    required = {
        "tool_class",
        "operation_class",
        "status",
        "error_class",
        "operation_shape_id",
        "target_equivalence_id",
    }
    optional = {"duration_ms", "output_size_bytes"}
    if not required.issubset(payload) or set(payload) - required - optional:
        raise AdapterError("tool structure fields are not strict")
    tool_class = payload["tool_class"]
    operation_class = payload["operation_class"]
    status = payload["status"]
    error_class = payload["error_class"]
    if tool_class not in _TOOL_CLASSES:
        raise AdapterError("tool_class is not a closed enum value")
    if operation_class not in _OPERATION_CLASSES:
        raise AdapterError("operation_class is not a closed enum value")
    if status not in _STATUSES:
        raise AdapterError("status is not a closed enum value")
    if error_class not in _ERROR_CLASSES:
        raise AdapterError("error_class is not a closed enum value")
    if error_class is not None and status not in {"error", "timeout"}:
        raise AdapterError("error_class requires error or timeout status")
    shape = payload["operation_shape_id"]
    if not _opaque(shape):
        raise AdapterError("operation_shape_id is not an opaque identifier")
    target = payload["target_equivalence_id"]
    if target is not None and (not isinstance(target, str) or _TARGET_RE.fullmatch(target) is None):
        raise AdapterError("target_equivalence_id is not a versioned opaque digest")
    return {
        "tool_class": tool_class,
        "operation_class": operation_class,
        "status": status,
        "error_class": error_class,
        "duration_ms": _optional_nonnegative(payload.get("duration_ms"), "duration_ms"),
        "output_size_bytes": _optional_nonnegative(
            payload.get("output_size_bytes"), "output_size_bytes"
        ),
        "operation_shape_id": shape,
        "target_equivalence_id": target,
    }


def _reject_group_fields(record: SourceRecord) -> None:
    if record.extra and _GROUP_FIELDS.intersection(record.extra):
        raise AdapterGroupRejected("adapter input may not supply group membership")
    if record.tool and _GROUP_FIELDS.intersection(record.tool):
        raise AdapterGroupRejected("adapter input may not supply group membership")


def _event_class(record: SourceRecord) -> str:
    if record.event_class == "tool_call" and record.extra:
        if record.extra.get("certified_spawn") is True:
            return "spawn"
    if record.event_class == "tool_outcome" and record.extra:
        if record.extra.get("certified_spawn") is True:
            return "lifecycle"
    mapped = _EVENT_MAP.get(record.event_class)
    if mapped is None:
        raise AdapterError("event class is not mapped")
    return mapped


def _coverage(concrete: int, other: int, unknown: int, floor: float) -> ToolMappingCoverage:
    if not isinstance(floor, (int, float)) or isinstance(floor, bool):
        raise AdapterError("mapping floor is invalid")
    if floor < 0 or floor > 1:
        raise AdapterError("mapping floor is outside zero to one")
    total = concrete + other + unknown
    if total == 0:
        return ToolMappingCoverage(0.0, 0.0, 0.0, False, False)
    concrete_fraction = concrete / total
    return ToolMappingCoverage(
        concrete_fraction,
        other / total,
        unknown / total,
        concrete > 0,
        concrete > 0 and concrete_fraction >= floor,
    )


def build_projection_input(
    source_id: str,
    records: list[SourceRecord],
    mapping_floor: float,
) -> AdaptResult:
    if not _opaque(source_id):
        raise AdapterError("projection input requires an opaque source id")
    ordered = sorted(records, key=lambda record: record.sequence)
    seen_ids: set[str] = set()
    seen_orders: set[int] = set()
    for record in ordered:
        _reject_group_fields(record)
        if not _opaque(record.source_event_id) or record.source_event_id in seen_ids:
            raise AdapterError("raw event id is missing, invalid, or duplicated")
        if (
            not isinstance(record.sequence, int)
            or isinstance(record.sequence, bool)
            or record.sequence < 1
            or record.sequence > MAX_SOURCE_ORDER
            or record.sequence in seen_orders
        ):
            raise AdapterError("source order is invalid or duplicated")
        seen_ids.add(record.source_event_id)
        seen_orders.add(record.sequence)
    compatible: dict[str, SourceRecord] = {}
    omitted: list[tuple[str, str]] = []
    for record in ordered:
        if record.visibility_class not in {"user_visible", "metadata_only", "hidden"}:
            omitted.append((record.source_event_id, "visibility_class_unknown"))
            continue
        if record.event_class == "message" and record.visibility_class != "user_visible":
            omitted.append((record.source_event_id, "message_visibility_incompatible"))
            continue
        if (record.actor_class, record.event_class) not in _COMPATIBLE:
            omitted.append((record.source_event_id, "actor_event_incompatible"))
            continue
        compatible[record.source_event_id] = record
    projected: list[ClassifiedEvent] = []
    projected_types: dict[str, str] = {}
    abstained: set[str] = set()
    concrete = 0
    other = 0
    unknown = 0
    for record in ordered:
        if record.source_event_id not in compatible:
            continue
        actor = _ACTOR_MAP[record.actor_class]
        event_class = _event_class(record)
        tool = None
        certified_spawn = bool(
            record.extra
            and record.extra.get("certified_spawn") is True
            and record.event_class in {"tool_call", "tool_outcome"}
        )
        if event_class in _TOOL_EVENTS:
            if record.tool is None:
                omitted.append((record.source_event_id, "tool_structure_missing"))
                continue
            tool = _validate_tool(record.tool)
            if tool["tool_class"] == "unknown":
                unknown += 1
            elif tool["tool_class"] == "other":
                other += 1
            else:
                concrete += 1
        elif certified_spawn:
            if record.tool is None:
                omitted.append((record.source_event_id, "tool_structure_missing"))
                continue
            _validate_tool(record.tool)
        elif record.tool is not None:
            omitted.append((record.source_event_id, "unexpected_tool_structure"))
            continue
        references = (
            record.responds_to,
            record.tool_call_ref,
            record.recovery_of,
            record.spawn_of,
        )
        if any(reference is not None and reference not in compatible for reference in references):
            omitted.append((record.source_event_id, "dangling_reference"))
            continue
        if event_class == "tool_outcome":
            call = compatible.get(record.tool_call_ref or "")
            if call is None or _event_class(call) != "tool_call":
                omitted.append((record.source_event_id, "unpaired_tool_outcome"))
                abstained.add("tool_outcome")
                continue
        if record.event_class == "subagent_completion" or (
            record.event_class == "tool_outcome"
            and record.extra
            and record.extra.get("certified_spawn") is True
        ):
            dispatch = compatible.get(record.spawn_of or record.tool_call_ref or "")
            if dispatch is None or _event_class(dispatch) != "spawn":
                omitted.append((record.source_event_id, "unpaired_subagent_completion"))
                abstained.add("uses_parallel_delegation")
                continue
        spawn_reference = record.spawn_of
        if event_class == "lifecycle" and record.extra:
            if record.extra.get("certified_spawn") is True:
                spawn_reference = record.tool_call_ref
        projected.append(
            ClassifiedEvent(
                raw_event_id=record.source_event_id,
                actor_class=actor,
                event_class=event_class,
                genuine_human=record.actor_class == "owner_user",
                source_order=record.sequence,
                responds_to_raw_id=record.responds_to,
                tool_call_raw_id=record.tool_call_ref if event_class == "tool_outcome" else None,
                recovery_of_raw_id=record.recovery_of,
                spawn_of_raw_id=spawn_reference if event_class == "lifecycle" else record.spawn_of,
                tool=tool,
            )
        )
        projected_types[record.source_event_id] = event_class
    dispatches = {key for key, value in projected_types.items() if value == "spawn"}
    completions = {
        event.spawn_of_raw_id
        for event in projected
        if event.event_class == "lifecycle" and event.spawn_of_raw_id is not None
    }
    if dispatches - completions:
        abstained.add("uses_parallel_delegation")
    return AdaptResult(
        ProjectionInput(source_id, tuple(projected)),
        tuple(omitted),
        tuple(sorted(abstained)),
        _coverage(concrete, other, unknown, mapping_floor),
    )


def classified_content_digest(projection_input: ProjectionInput) -> str:
    return hashlib.sha256(projection_input.to_json_bytes()).hexdigest()


def behavior_signal_present(
    behavior_event_ids: set[str], deterministic_signal_ids: set[str]
) -> bool:
    return bool(behavior_event_ids and behavior_event_ids.intersection(deterministic_signal_ids))
