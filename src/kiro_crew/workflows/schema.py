"""Structured-output validation + bounded retry for ``ctx.agent(schema=...)`` (M2, C1–C3).

The frozen contract says ``ctx.agent(prompt, schema=<JSON Schema>)`` returns a
*validated dict* (or ``None`` after retries). KiroCrew's provider layer has no
native schema enforcement, and the runtime ships neither ``jsonschema`` nor a
JSON-Schema-consuming pydantic path — so this module is a small, dependency-free
validator for the JSON-Schema **subset** the workflow DSL actually uses
(the constructs in ``docs/system-specs/modules/examples/workflows/*``):

    type: object | array | string | integer | number | boolean | null
    object: properties, required
    array:  items
    any:    enum

Leaf module (no intra-package imports) so it sits at the bottom of the layering
(GATE F1); ``runner`` imports it. Pure functions + one async retry helper, all
unit-testable against a stub text producer (never a real agent).

Gates: C1 valid object returned · C2 malformed→retry→success, all-malformed→None ·
C3 schema-violating object rejected. See ``docs/system-specs/modules/workflow-gates.md``.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Optional

# Bounded retries before ``ctx.agent(schema=)`` gives up and returns None
# (matches ``_SCHEMA_RETRIES`` in the module spec).
DEFAULT_SCHEMA_RETRIES = 2

_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    # bool is a subclass of int — exclude it from "integer"/"number" explicitly.
    "integer": int,
    "number": (int, float),
    "string": str,
    "boolean": bool,
    "null": type(None),
}


def _type_ok(value: Any, json_type: Any) -> bool:
    if isinstance(json_type, (list, tuple)):
        # JSON Schema allows ``type`` to be a union of names (e.g. ["object",
        # "null"]); the value is valid if it matches any listed member.
        return any(_type_ok(value, member) for member in json_type)
    if json_type in ("integer", "number") and isinstance(value, bool):
        return False  # bool is not a number for our purposes
    expected = _JSON_TYPES.get(json_type)
    if expected is None:
        return True  # unknown type keyword → don't reject on it
    return isinstance(value, expected)


def validate_against_schema(value: Any, schema: dict, *, path: str = "$") -> list[str]:
    """Return a list of human-readable validation errors ('' empty == valid).

    Supports the JSON-Schema subset documented in the module docstring. Unknown
    keywords are ignored (forward-compatible), so a richer schema still validates
    on the parts this understands.
    """
    errors: list[str] = []

    expected_type = schema.get("type")
    if expected_type is not None and not _type_ok(value, expected_type):
        errors.append(f"{path}: expected type '{expected_type}', got {type(value).__name__}")
        return errors  # type mismatch — deeper checks would be noise

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in enum {schema['enum']}")

    if expected_type == "object" or (expected_type is None and isinstance(value, dict)):
        if isinstance(value, dict):
            for key in schema.get("required", []):
                if key not in value:
                    errors.append(f"{path}: missing required property '{key}'")
            props = schema.get("properties", {})
            for key, subschema in props.items():
                if key in value:
                    errors.extend(
                        validate_against_schema(value[key], subschema, path=f"{path}.{key}")
                    )

    if expected_type == "array" or (expected_type is None and isinstance(value, list)):
        if isinstance(value, list):
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for i, item in enumerate(value):
                    errors.extend(validate_against_schema(item, item_schema, path=f"{path}[{i}]"))

    return errors


def parse_json(text: str) -> Any:
    """Tolerantly parse model output as JSON; raises ``ValueError`` if not JSON.

    Handles the common cases of a fenced ```json block or surrounding prose by
    extracting the outermost ``{...}`` / ``[...]`` span. Never executes anything
    (no ``eval`` — BSC12).
    """
    if not isinstance(text, str):
        raise ValueError("model output is not text")
    stripped = text.strip()
    # Strip a leading/trailing code fence if present.
    if stripped.startswith("```"):
        parts = stripped.split("```", 2)
        stripped = parts[1] if len(parts) >= 2 else ""
        if stripped.startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        # Fall back to the outermost object/array span. Try whichever delimiter
        # appears first so a prose-wrapped array containing an object yields the
        # outer array, not the inner object.
        candidates = []
        for open_c, close_c in (("{", "}"), ("[", "]")):
            start, end = stripped.find(open_c), stripped.rfind(close_c)
            if 0 <= start < end:
                candidates.append((start, end))
        for start, end in sorted(candidates):
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                continue
        raise ValueError("model output is not valid JSON")


def coerce_and_validate(text: str, schema: dict) -> tuple[Optional[Any], list[str]]:
    """Parse ``text`` as JSON and validate against ``schema``.

    Returns ``(value, [])`` on success or ``(None, errors)`` when parsing fails or
    the value violates the schema.
    """
    try:
        value = parse_json(text)
    except ValueError as exc:
        return None, [str(exc)]
    errors = validate_against_schema(value, schema)
    if errors:
        return None, errors
    return value, []


async def run_with_schema(
    produce: Callable[[str], Awaitable[str]],
    prompt: str,
    schema: dict,
    *,
    retries: int = DEFAULT_SCHEMA_RETRIES,
) -> Optional[Any]:
    """Drive an agent text-producer until it yields schema-valid JSON, or give up.

    ``produce(prompt)`` returns the agent's text for a prompt. On malformed/invalid
    output we re-ask up to ``retries`` more times, appending the validation errors
    to the prompt so the model can self-correct. Returns the validated value, or
    ``None`` after ``retries`` failures (the contract's "None on give-up").
    """
    attempt_prompt = _augment_prompt(prompt, schema)
    last_errors: list[str] = []
    for _attempt in range(retries + 1):
        text = await produce(attempt_prompt)
        value, errors = coerce_and_validate(text, schema)
        if not errors:
            return value
        last_errors = errors
        attempt_prompt = (
            f"{_augment_prompt(prompt, schema)}\n\nYour previous reply was invalid: "
            f"{'; '.join(last_errors)}. Reply ONLY with corrected JSON."
        )
    return None


def _augment_prompt(prompt: str, schema: dict) -> str:
    """Append the schema + a JSON-only instruction so the model returns parseable output."""
    return (
        f"{prompt}\n\nReply ONLY with a single JSON value matching this JSON Schema "
        f"(no prose, no code fence):\n{json.dumps(schema)}"
    )
