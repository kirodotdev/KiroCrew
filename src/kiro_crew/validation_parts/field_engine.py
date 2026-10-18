"""``ValidationError`` and the declarative ``FieldSpec`` / ``ToolSchema`` validator.

:func:`validate_tool_args` checks a call against a :class:`ToolSchema`;
:func:`validate_field` checks one value against a :class:`FieldSpec`. The MCP entry
points repair an int that reached a string field with :func:`coerce_mcp_tool_args`
first.

Callers import these names from :mod:`kiro_crew.validation`, which serves them; this
module never imports that facade.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.validation_parts.text_sanitizer import sanitize_string

# ── Exceptions ──


class ValidationError(Exception):
    """Raised when input validation fails."""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


# Named by the path callers import it from: a traceback renders a raised class as
# ``module.qualname``, so the owner's name must not reach it.
ValidationError.__module__ = "kiro_crew.validation"


# ── Field Validators ──

#: Stamped onto a value truncated by :func:`clamp_to_max_len`. It reports what the
#: CLAMP dropped, not the caller's original input length: the value reaching the
#: clamp has already been through :func:`sanitize_string`, so an "original length"
#: here would silently attribute the sanitizer's removals to the truncation. Kept
#: short so it costs almost none of the field's budget.
_CLAMP_NOTE = " [... truncated, dropped {n} chars]"

#: Reads :data:`_CLAMP_NOTE` back off a clamped value. Derived FROM that constant
#: rather than spelled a second time, so the stamp and its reader cannot drift
#: apart when the wording changes.
_CLAMP_NOTE_RE = re.compile(re.escape(_CLAMP_NOTE).replace(r"\{n\}", r"(\d+)") + r"\Z")


def clamp_report(value: str) -> tuple[int, int] | None:
    """For a value stamped by :func:`clamp_to_max_len`, return ``(before, kept)``.

    ``None`` when the value carries no stamp, which is the common case. ``before``
    is the length the clamp saw and ``kept`` the length of the caller's own text
    that survived, excluding the stamp itself — the two numbers a caller needs to
    be told what happened to its field in the same round-trip that accepted it.

    Reading the stamp back is what lets a tool whose reply does NOT echo the
    applied value still report the cut (see ``mcp_work.work_report``). A caller
    whose own text happens to end in the stamp's exact shape would be described
    as clamped when it was not; the cost is one inaccurate advisory line, which
    is why no decision is keyed off this.
    """
    match = _CLAMP_NOTE_RE.search(value)
    if not match:
        return None
    kept = len(value) - (match.end() - match.start())
    return kept + int(match.group(1)), kept


@dataclass
class FieldSpec:
    """Declarative field specification for validation."""

    name: str
    type: type | tuple[type, ...]  # expected Python type(s)
    required: bool = False
    max_len: int = 0  # 0 = no limit
    min_val: float | None = None  # for numeric fields
    max_val: float | None = None
    allowed: frozenset[str] | None = None  # enum allow-list
    pattern: re.Pattern[str] | None = None  # regex pattern
    default: Any = None
    item_type: type | tuple[type, ...] | None = None  # type: ignore[valid-type]  # for list fields: expected type(s) of each element
    item_max_len: int = 0  # for list fields: max length of each string element
    item_pattern: re.Pattern[str] | None = None  # for list fields: regex for each string element
    max_items: int = 0  # for list fields: max number of items (0 = no limit)
    # For list fields whose items may be objects: the ToolSchema each dict item
    # is validated against (unknown keys refused, like a top-level call). Only
    # consulted when ``item_type`` admits ``dict``.
    item_schema: Any = None
    # Opt-in, and DELIBERATELY narrow: when a string field is over ``max_len``,
    # truncate it to the cap instead of rejecting the whole call. For a field
    # whose only job is to EXPLAIN a request — ``autonudge_stop`` /
    # ``monitor_stop`` ``reason`` — the length of the explanation must not be
    # able to defeat the request itself. Never set this on a field the
    # handler acts on: a truncated control input is a wrong control input, and
    # rejecting is the only safe answer there.
    #
    # The truncation is NOT silent: :func:`clamp_to_max_len` stamps the value
    # itself with how much it dropped, so every place the value travels — the
    # applied outcome the model reads back, the SEL audit row, the persisted
    # stop reason — says so without any layer having to plumb a second flag.
    clamp_to_max: bool = False


@dataclass
class ToolSchema:
    """Schema for a tool's input arguments."""

    tool_name: str
    fields: list[FieldSpec] = field(default_factory=list)
    custom_validator: Any = None  # Optional callable(cleaned_args) -> None; raises ValidationError


def clamp_to_max_len(value: str, max_len: int) -> str:
    """Truncate *value* to *max_len* chars, stamping it with what was dropped.

    Used only for a :class:`FieldSpec` that opted into ``clamp_to_max``. The
    note is what makes this a clamp rather than a silent mutation: the returned
    string carries its own provenance, so the model reading the applied outcome
    and the operator reading the audit row both see that the text was cut, with
    no extra return channel and no per-tool plumbing.

    The note counts what THIS call dropped. It deliberately does not claim to
    report the caller's original length: by the time a field reaches here it has
    already been sanitized, so one number cannot honestly stand for both
    removals (see :data:`_CLAMP_NOTE`).

    The result is always ``<= max_len``. A cap too small to hold the note at all
    degrades to a plain cut rather than to a value that is only a note.
    """
    # Solve for the note that describes the cut the note itself is part of: the
    # note occupies budget, so the dropped count includes it. Computed directly
    # rather than iterated -- keep is what survives, everything else is dropped.
    keep = max_len - len(_CLAMP_NOTE.format(n=len(value)))
    if keep <= 0:
        return value[:max_len]
    head = value[:keep].rstrip()
    return head + _CLAMP_NOTE.format(n=len(value) - len(head))


# An int above this magnitude cannot be a value a numeric-looking string
# argument was meant to carry: JavaScript numbers (and JSON parsers that back
# them with IEEE-754 doubles) only represent integers exactly up to 2**53, so a
# larger int that reached a string field was either never a round-trippable id
# (it already lost precision upstream) or is an outright wrong value. The PR
# rejects a float on a string field for exactly this reason — a value that
# cannot be the author's original text is a louder, safer failure than a
# silently-wrong coercion — and this applies the same bound to the int case.
_MAX_EXACT_INT = 2**53


def _coerce_number_to_string_for_string_field(value: Any, spec: FieldSpec) -> Any:
    """Repair an integer that reached a string field as a number.

    Some agent runtimes defer a tool's schema and load it on demand; a few of
    those, when they later marshal the model's ``arguments``, re-type a
    top-level argument whose VALUE looks like a number (``"42"``) into a JSON
    number — even though the loaded tool schema declared that field a string.
    By the time the call reaches a validator the type has already been lost
    upstream, so a field that says ``str`` sees an ``int`` and the call is
    rejected when the author clearly meant a string.

    This repair belongs ONLY at the MCP tool-call entry points (where that
    upstream re-typing happens), NOT in the shared ``validate_field`` — the
    dashboard HTTP endpoints also validate through that function and never see
    the re-typing, so coercing there would silently change their contract and
    let an int slip through a field a caller reads from the raw body.

    This is a NARROW, EXACT repair, not a general coercion:

    * It fires ONLY for a field whose declared type is EXACTLY ``str`` (not a
      tuple like ``(int, float)`` that legitimately accepts a number) — a field
      that wanted a number keeps getting one.
    * It converts an ``int`` to its string form and NOTHING else: a ``str``
      stays a ``str``, a ``bool`` (an ``int`` subclass) is left for the normal
      type check to reject, and a ``float`` / list / dict / ``None`` is
      untouched.
    * An ``int`` whose magnitude exceeds ``2**53`` is REFUSED (``ValidationError``)
      rather than coerced: past that bound it is a precision-loss or
      outright-wrong id, the same hazard this repair declines to paper over for
      floats.

    A ``float`` is DELIBERATELY not converted. ``str(float)`` is the shortest
    round-tripping form, which is NOT the author's original text whenever that
    text carried a digit the float cannot distinguish: ``"1790284307.156620"``
    becomes the float ``1790284307.15662`` and then ``"1790284307.15662"`` — a
    DIFFERENT, still-schema-valid value (it still matches a ``^\\d+\\.\\d+$``
    timestamp pattern), so a Slack reply would silently go to the wrong thread
    instead of failing loudly. For a value the author meant as a string, a loud
    ``expected str`` is strictly safer than a silently-wrong one, so a float on
    a string field is left to the ordinary type error. Only ``int`` round-trips
    exactly (``str(42) == "42"``, within the exact-integer bound), so only
    ``int`` is repaired here.
    """
    if spec.type is not str:
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > _MAX_EXACT_INT:
            raise ValidationError(
                spec.name,
                f"integer {value} exceeds the exact-integer bound ({_MAX_EXACT_INT}); "
                "a numeric id this large cannot be a precise string value",
            )
        return str(value)
    return value


def coerce_mcp_tool_args(args: dict[str, Any], schema: ToolSchema) -> dict[str, Any]:
    """Return ``args`` with int-on-string-field values repaired, for MCP entry points.

    Apply this at an MCP tool-call entry point (``mcp_core._validate_args`` and
    the other MCP servers' equivalents) BEFORE :func:`validate_tool_args`. It
    walks the schema's string-typed fields and runs the narrow int->str repair
    (see :func:`_coerce_number_to_string_for_string_field`) on each present
    argument, leaving every other value — and every argument the schema does
    not name — untouched. An out-of-range int raises ``ValidationError`` here,
    so the entry point rejects it exactly as the schema's own checks would.

    This is intentionally NOT part of :func:`validate_field`: the dashboard HTTP
    endpoints validate through the same function and must keep their original
    contract, so the repair lives only where the upstream re-typing occurs.
    """
    if not isinstance(args, dict):
        return args
    repaired = dict(args)
    for spec in schema.fields:
        if spec.name in repaired:
            repaired[spec.name] = _coerce_number_to_string_for_string_field(
                repaired[spec.name], spec
            )
    return repaired


def coerce_mcp_tool_args_json_schema(args: dict[str, Any], input_schema: Any) -> dict[str, Any]:
    """JSON-Schema counterpart of :func:`coerce_mcp_tool_args`, for MCP servers
    that validate through :func:`validate_mcp_tool_arguments`.

    Repair a top-level argument an upstream deferred-schema runtime re-typed
    from a numeric-looking string ("42" -> 42) back to its string form, BEFORE
    validation, for a property the inputSchema types EXACTLY ``"string"``. The
    same narrow rules as the ``FieldSpec`` path apply: only an ``int`` is
    coerced, a ``bool`` and a ``float`` are left for the type check, and an int
    beyond the exact-integer bound raises ``ValidationError``.

    Only top-level properties are considered — the re-typing hits top-level
    arguments, and walking nested subschemas would risk changing values the
    schema never typed as string.
    """
    if not isinstance(args, dict) or not isinstance(input_schema, dict):
        return args
    props = input_schema.get("properties")
    if not isinstance(props, dict):
        return args
    repaired = dict(args)
    for key, value in args.items():
        sub = props.get(key)
        if isinstance(sub, dict) and sub.get("type") == "string":
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                if abs(value) > _MAX_EXACT_INT:
                    raise ValidationError(
                        key,
                        f"integer {value} exceeds the exact-integer bound ({_MAX_EXACT_INT}); "
                        "a numeric id this large cannot be a precise string value",
                    )
                repaired[key] = str(value)
    return repaired


def validate_field(value: Any, spec: FieldSpec) -> Any:
    """Validate and normalize a single field value. Returns cleaned value."""
    if value is None:
        if spec.required:
            raise ValidationError(spec.name, "required")
        return spec.default

    # Type check
    if not isinstance(value, spec.type):
        raise ValidationError(
            spec.name,
            f"expected {spec.type.__name__ if isinstance(spec.type, type) else spec.type}, "
            f"got {type(value).__name__}",
        )

    # bool is a subclass of int, so isinstance(True, int) is True — a bool
    # would otherwise slip through an int field (and pass min/max range checks
    # since True == 1). Reject bool unless it is an explicitly allowed type.
    allowed_types = spec.type if isinstance(spec.type, tuple) else (spec.type,)
    if isinstance(value, bool) and bool not in allowed_types:
        raise ValidationError(
            spec.name,
            f"expected {spec.type.__name__ if isinstance(spec.type, type) else spec.type}, "
            f"got {type(value).__name__}",
        )

    # String validation
    if isinstance(value, str):
        value = sanitize_string(value)
        if not value and spec.required:
            raise ValidationError(spec.name, "required (empty after sanitization)")
        if spec.max_len and len(value) > spec.max_len:
            if spec.clamp_to_max:
                # Explanatory field: cut it and carry on (see FieldSpec.clamp_to_max).
                value = clamp_to_max_len(value, spec.max_len)
            else:
                # Report the actual length + overshoot so a caller (e.g. the LLM
                # composing a learn_add rule) can trim by the exact amount in one
                # pass instead of guessing and re-submitting repeatedly.
                raise ValidationError(
                    spec.name,
                    f"exceeds max length {spec.max_len} "
                    f"(got {len(value)}, trim {len(value) - spec.max_len} chars)",
                )
        if spec.allowed and value not in spec.allowed:
            raise ValidationError(spec.name, f"must be one of: {', '.join(sorted(spec.allowed))}")
        if spec.pattern and value and not spec.pattern.match(value):
            raise ValidationError(spec.name, "invalid format")

    # Numeric validation
    if isinstance(value, (int, float)):
        if spec.min_val is not None and value < spec.min_val:
            raise ValidationError(spec.name, f"must be >= {spec.min_val}")
        if spec.max_val is not None and value > spec.max_val:
            raise ValidationError(spec.name, f"must be <= {spec.max_val}")

    # List item validation
    if isinstance(value, list):
        if spec.max_items and len(value) > spec.max_items:
            raise ValidationError(spec.name, f"exceeds max items {spec.max_items}")
        if spec.item_type:
            for i, item in enumerate(value):
                if not isinstance(item, spec.item_type):
                    item_types: tuple[type, ...] = (
                        spec.item_type if isinstance(spec.item_type, tuple) else (spec.item_type,)
                    )
                    expected = " or ".join(t.__name__ for t in item_types)
                    raise ValidationError(
                        spec.name,
                        f"item[{i}]: expected {expected}, got {type(item).__name__}",
                    )
                if isinstance(item, dict) and spec.item_schema is not None:
                    try:
                        value[i] = validate_tool_args(item, spec.item_schema)
                    except ValidationError as exc:
                        raise ValidationError(f"{spec.name}[{i}].{exc.field}", exc.message) from exc
                    continue
                if isinstance(item, str):
                    item = sanitize_string(item)
                    value[i] = item
                    if spec.item_max_len and len(item) > spec.item_max_len:
                        raise ValidationError(
                            spec.name, f"item[{i}]: exceeds max length {spec.item_max_len}"
                        )
                    if spec.item_pattern and item and not spec.item_pattern.fullmatch(item):
                        raise ValidationError(spec.name, f"item[{i}]: invalid format")

    return value


def validate_tool_args(args: dict[str, Any], schema: ToolSchema) -> dict[str, Any]:
    """Validate all tool arguments against a schema. Returns cleaned args dict."""
    if not isinstance(args, dict):
        raise ValidationError("args", "must be a JSON object")

    cleaned: dict[str, Any] = {}
    known_fields = {s.name for s in schema.fields}

    # Reject unknown fields
    for key in args:
        if key not in known_fields:
            raise ValidationError(key, f"unknown field for tool '{schema.tool_name}'")

    for spec in schema.fields:
        # Only process fields that are explicitly in args OR are required
        if spec.name in args:
            raw = args[spec.name]
            cleaned[spec.name] = validate_field(raw, spec)
        elif spec.required:
            # Required field missing - validate_field will raise error
            cleaned[spec.name] = validate_field(None, spec)
        elif spec.default is not None:
            # Field not in args, but has a default - include it
            cleaned[spec.name] = spec.default

    if schema.custom_validator:
        schema.custom_validator(cleaned)

    return cleaned
