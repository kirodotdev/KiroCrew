"""Schema registry for KiroCrew configuration.

Generates a nested JSON Schema (Draft-07) from the Python dataclass hierarchy
and flattens it into a list of ``ConfigEntry`` records for API consumption and
baseline generation.

Both ``JSON_SCHEMA`` and ``SCHEMA_REGISTRY`` are built once at import time.
"""

from __future__ import annotations

import dataclasses
import typing
from dataclasses import dataclass, fields

from kiro_crew.config.loader import KiroCrewConfig

# ---------------------------------------------------------------------------
# Type mapping: Python type annotation → JSON Schema type string
# ---------------------------------------------------------------------------

_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    set: "array",
    dict: "object",
}


def _python_type_to_json(tp: type) -> str:
    """Map a Python type annotation to a JSON Schema type string.

    Handles generic aliases (``list[str]``, ``dict[str, str]``) by
    extracting the origin type.  Falls back to ``"string"`` for
    unrecognised types.
    """
    origin = typing.get_origin(tp)
    if origin is not None:
        # e.g. list[str] → list, dict[str, str] → dict
        tp = origin

    return _TYPE_MAP.get(tp, "string")


# ---------------------------------------------------------------------------
# ConfigEntry dataclass
# ---------------------------------------------------------------------------


@dataclass
class ConfigEntry:
    """A single flat record describing one config path."""

    path: str  # dot-separated, e.g. "agent.provider"
    kind: str  # "core" (future: "plugin")
    type: str  # "string" | "integer" | "number" | "boolean" | "array" | "object"
    required: bool
    deprecated: bool
    sensitive: bool
    tags: list[str]
    label: str
    help: str
    has_children: bool
    enum_values: list | None
    default_value: object  # JSON-serializable or None
    nullable: bool = False  # True when the underlying JSON Schema type allows null


# ---------------------------------------------------------------------------
# build_json_schema — dataclass hierarchy → nested JSON Schema
# ---------------------------------------------------------------------------


def _is_dataclass_type(tp: type) -> bool:
    """Return True if *tp* is a dataclass class (not an instance)."""
    origin = typing.get_origin(tp)
    if origin is not None:
        return False
    return dataclasses.is_dataclass(tp) and isinstance(tp, type)


def _extract_item_type(tp: type) -> type | None:
    """For ``list[X]`` return X, else None."""
    origin = typing.get_origin(tp)
    if origin is list:
        args = typing.get_args(tp)
        if args:
            return args[0]
    return None


def _extract_value_type(tp: type) -> type | None:
    """For ``dict[K, V]`` return V, else None."""
    origin = typing.get_origin(tp)
    if origin is dict:
        args = typing.get_args(tp)
        if len(args) >= 2:
            return args[1]
    return None


def _optional_inner(tp: type) -> tuple[type, bool]:
    """If *tp* is ``Optional[X]`` / ``X | None``, return ``(X, True)``.

    Otherwise return ``(tp, False)``.  Used so ``dict[str, str | None]``
    generates a JSON Schema ``additionalProperties`` of
    ``{"type": ["string", "null"]}`` rather than ``{"type": "string"}``,
    which would reject legitimate ``null`` suppression sentinels.
    """
    origin = typing.get_origin(tp)
    if origin is typing.Union or (
        # ``X | None`` (PEP 604) has origin ``types.UnionType`` on 3.10+
        origin is not None and getattr(origin, "__name__", "") == "UnionType"
    ):
        args = [a for a in typing.get_args(tp) if a is not type(None)]  # noqa: E721
        if len(args) == 1 and len(typing.get_args(tp)) == 2:
            return args[0], True
    return tp, False


def _json_type_for_value(tp: type) -> str | list[str]:
    """JSON Schema ``type`` for a dict/list value annotation.

    Returns ``["<base>", "null"]`` for ``Optional[X]`` to allow null sentinels,
    otherwise a single type string.
    """
    inner, is_optional = _optional_inner(tp)
    base = _python_type_to_json(inner)
    return [base, "null"] if is_optional else base


def _default_for_field(f: dataclasses.Field) -> object:  # type: ignore[type-arg]
    """Extract the JSON-serializable default value from a dataclass field."""
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        val = f.default_factory()  # type: ignore[misc]
        return sorted(val) if isinstance(val, set) else val
    return None


def _build_field_schema(
    f: dataclasses.Field,  # type: ignore[type-arg]
    resolved_type: type | None,
) -> dict:
    """Build a JSON Schema property dict for a single dataclass field.

    *resolved_type* is the field's concrete runtime type as resolved by the
    caller via ``typing.get_type_hints()`` on the owning class. It is ``None``
    only if hint resolution failed for the whole class, in which case we fall
    back to ``str`` (matching the historical ``eval``-failure fallback).
    """
    meta: dict = dict(f.metadata) if f.metadata else {}
    label: str = meta.get("label", f.name)
    help_text: str = meta.get("help", "")
    tags: list[str] = meta.get("tags", [])
    sensitive: bool = meta.get("sensitive", False)
    deprecated: bool = meta.get("deprecated", False)
    enum_values: list | None = meta.get("enum", None)

    tp: type = resolved_type if resolved_type is not None else str
    # A field annotated ``X | None`` / ``Optional[X]`` maps to its base type
    # plus ``"null"``. Without the unwrap, the union itself reaches
    # ``_python_type_to_json`` and falls through to ``"string"`` with
    # ``nullable=False`` — making "unset means inherit the downstream default"
    # unexpressible for numeric fields. The ``nullable`` metadata flag remains
    # the way to allow ``null`` on a field whose annotation is non-optional
    # (disable-sentinel fields the loader normalizes, e.g.
    # session.archive_retention_days: null → "disable cleanup").
    tp, annotation_nullable = _optional_inner(tp)
    nullable = annotation_nullable or bool(meta.get("nullable"))
    schema: dict = {}

    if _is_dataclass_type(tp):
        # Nested dataclass → recurse
        schema = _build_object_schema(tp)
        if nullable:
            schema["type"] = ["object", "null"]
    else:
        json_type = _python_type_to_json(tp)
        # Emitting the plain single-type form when the field is not nullable
        # matters: a ``["integer", "null"]`` schema lets jsonschema accept a
        # null the loader would otherwise strip, silently reverting the field
        # to its default — the opposite of the sentinel intent.
        if nullable:
            schema["type"] = [json_type, "null"]
        else:
            schema["type"] = json_type

        if json_type == "array":
            item_tp = _extract_item_type(tp)
            if item_tp and _is_dataclass_type(item_tp):
                schema["items"] = _build_object_schema(item_tp)
            elif item_tp:
                schema["items"] = {"type": _json_type_for_value(item_tp)}
            else:
                schema["items"] = {}

        elif json_type == "object":
            val_tp = _extract_value_type(tp)
            if val_tp and _is_dataclass_type(val_tp):
                schema["additionalProperties"] = _build_object_schema(val_tp)
            elif val_tp:
                schema["additionalProperties"] = {
                    "type": _json_type_for_value(val_tp),
                }
            else:
                schema["additionalProperties"] = True
            # A plain-dict field may DECLARE known sub-keys via
            # ``_meta(..., properties={...})`` (JSON-Schema property nodes with
            # their own x-meta). They flatten into first-class ConfigEntry
            # paths — which is what lets a Settings control carry a configKey
            # for a key inside a dict field — while additionalProperties above
            # keeps every undeclared key valid, so declaring some keys never
            # invalidates the rest of the dict.
            declared_props = meta.get("properties")
            if isinstance(declared_props, dict) and declared_props:
                schema["properties"] = declared_props

    default = _default_for_field(f)
    if default is not None:
        schema["default"] = default

    if enum_values is not None:
        schema["enum"] = enum_values

    schema["x-meta"] = {
        "label": label,
        "help": help_text,
        "tags": tags,
        "sensitive": sensitive,
        "deprecated": deprecated,
    }

    return schema


def _build_object_schema(cls: type) -> dict:
    """Build a JSON Schema ``object`` node for a dataclass type.

    Field annotations are strings under ``from __future__ import annotations``,
    so we resolve them with ``typing.get_type_hints(cls)`` — which evaluates
    each annotation against *the dataclass's own module globals + localns*. This
    keeps the resolution self-contained (no reaching into the loader module's
    namespace) and lets schema generation consume the DTOs one-directionally.

    Failure mode note: ``get_type_hints`` resolves a class's annotations as a
    unit, so if any single annotation is unresolvable the whole call raises and
    every field in *this* class degrades to ``str`` (the ``except`` below) —
    broader than the prior per-field annotation-string fallback, which dropped
    only the offending field. This is acceptable because such a failure signals
    a genuine annotation bug (a forward ref to a name that does not exist in the
    class's module), which should be fixed rather than silently half-resolved;
    and resolution never reaches across modules the way the old loader-namespace
    approach did. All config DTOs resolve cleanly today.
    """
    try:
        hints = typing.get_type_hints(cls)
    except Exception:
        # A bad annotation strings-out this class's schema rather than crashing
        # generation. See the failure-mode note above.
        hints = {}

    props: dict = {}
    for f in fields(cls):
        # Private fields (leading underscore) are internal bookkeeping, not
        # user-facing config — e.g. KiroCrewConfig._extra_sections, which
        # round-trips unknown/edition-contributed top-level sections. They are
        # not part of the JSON schema or the flattened ConfigEntry baseline.
        if f.name.startswith("_"):
            continue
        props[f.name] = _build_field_schema(f, hints.get(f.name))

    return {
        "type": "object",
        "properties": props,
    }


def build_json_schema(root_cls: type) -> dict:
    """Walk ``dataclasses.fields()`` recursively and produce a nested JSON Schema.

    Returns a Draft-07 compatible dict with custom ``x-meta`` extensions
    for label, help, tags, sensitive, and deprecated.
    """
    schema = _build_object_schema(root_cls)
    schema["$schema"] = "http://json-schema.org/draft-07/schema#"

    # Attach x-meta from root class metadata if available
    # (KiroCrewConfig itself doesn't have field metadata since it's the root)
    return schema


# ---------------------------------------------------------------------------
# flatten_to_entries — nested JSON Schema → flat ConfigEntry list
# ---------------------------------------------------------------------------


def flatten_to_entries(
    json_schema: dict,
    prefix: str = "",
) -> list[ConfigEntry]:
    """DFS-flatten a nested JSON Schema into a flat list of ``ConfigEntry``.

    Path construction follows the same convention as OpenClaw's
    ``collectConfigDocBaselineEntries``:

    * ``properties.key`` → append ``.key``
    * ``additionalProperties`` (dynamic keys) → append ``.*``
    * ``items`` (array elements) → append ``.*``
    """
    entries: list[ConfigEntry] = []
    _flatten_recurse(json_schema, prefix, entries)
    return entries


def _flatten_recurse(
    node: dict,
    path: str,
    out: list[ConfigEntry],
) -> None:
    """Recursive DFS helper for ``flatten_to_entries``."""
    raw_type = node.get("type", "object")
    # A JSON Schema ``type`` may be a list like ``["string", "null"]`` when
    # the field accepts null.  Normalize to a single base type string plus a
    # ``nullable`` flag so downstream consumers (baseline emitter, UI, etc.)
    # continue to see a scalar type.
    if isinstance(raw_type, list):
        non_null = [t for t in raw_type if t != "null"]
        nullable = "null" in raw_type
        node_type = non_null[0] if non_null else "null"
    else:
        node_type = raw_type
        nullable = False
    x_meta = node.get("x-meta", {})

    label: str = x_meta.get("label", path.rsplit(".", 1)[-1] if path else "")
    help_text: str = x_meta.get("help", "")
    tags: list[str] = x_meta.get("tags", [])
    sensitive: bool = x_meta.get("sensitive", False)
    deprecated: bool = x_meta.get("deprecated", False)
    enum_values: list | None = node.get("enum", None)
    default_value: object = node.get("default", None)

    has_children = node_type in ("object", "array")

    # Emit an entry for this node if it has a path (skip the root)
    if path:
        entries_entry = ConfigEntry(
            path=path,
            kind="core",
            type=node_type,
            required=False,
            deprecated=deprecated,
            sensitive=sensitive,
            tags=list(tags),
            label=label,
            help=help_text,
            has_children=has_children,
            enum_values=list(enum_values) if enum_values is not None else None,
            default_value=default_value,
            nullable=nullable,
        )
        out.append(entries_entry)

    # Recurse into properties (object with named keys)
    properties = node.get("properties", {})
    for key, child_schema in properties.items():
        child_path = f"{path}.{key}" if path else key
        _flatten_recurse(child_schema, child_path, out)

    # Recurse into additionalProperties (dynamic keys)
    additional = node.get("additionalProperties")
    if isinstance(additional, dict) and additional.get("type"):
        child_path = f"{path}.*" if path else "*"
        _flatten_recurse(additional, child_path, out)

    # Recurse into items (array elements)
    items = node.get("items")
    if isinstance(items, dict) and items.get("type"):
        child_path = f"{path}.*" if path else "*"
        _flatten_recurse(items, child_path, out)


# ---------------------------------------------------------------------------
# config_entry_to_dict — ConfigEntry → JSON-compatible dict
# ---------------------------------------------------------------------------


def config_entry_to_dict(entry: ConfigEntry) -> dict:
    """Serialize a ``ConfigEntry`` to a JSON-compatible dict.

    Output keys use camelCase (``hasChildren``, ``enumValues``,
    ``defaultValue``) for compatibility with OpenClaw's baseline format.
    """
    return {
        "path": entry.path,
        "kind": entry.kind,
        "type": entry.type,
        "required": entry.required,
        "deprecated": entry.deprecated,
        "sensitive": entry.sensitive,
        "tags": entry.tags,
        "label": entry.label,
        "help": entry.help,
        "hasChildren": entry.has_children,
        "enumValues": entry.enum_values,
        "defaultValue": entry.default_value,
        **({"nullable": True} if entry.nullable else {}),
    }


# ---------------------------------------------------------------------------
# Module-level singletons — built once at import time
# ---------------------------------------------------------------------------

JSON_SCHEMA: dict = build_json_schema(KiroCrewConfig)
"""Nested JSON Schema (Draft-07) for the full config hierarchy."""

SCHEMA_REGISTRY: list[ConfigEntry] = flatten_to_entries(JSON_SCHEMA)
"""Flat list of ``ConfigEntry`` records for all config paths."""
