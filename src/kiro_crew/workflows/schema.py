"""Structured-output validation + bounded retry for ``ctx.agent(schema=...)`` (GATES C1–C3).

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

Leaf module within the workflows package (no intra-package sibling imports) so
it sits at the bottom of the layering (GATE F1); ``runner`` imports it. Prose
recovery is delegated to the shared ``kiro_crew.llm_helpers`` extractor — an
external import, like the optional adapters' — the same scanner the spine and
task-planner call sites use (a few bespoke span-scans remain elsewhere, e.g.
dashboard/chat_summary and knowledge/extractor; consolidating them is tracked
separately). Pure functions + one async retry helper, all unit-testable against
a stub text producer (never a real agent).

Gates: C1 valid object returned · C2 malformed→retry→success, all-malformed→None ·
C3 schema-violating object rejected. See ``docs/system-specs/modules/workflow-gates.md``.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Optional

from kiro_crew.llm_helpers import _extract_json_of_type

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

    # Container sub-checks key on the VALUE's runtime type, not the ``type``
    # spelling: any type mismatch already early-returned above, so a dict here
    # is schema-admissible whether ``type`` said "object", a union list
    # admitting object, or nothing. Keying on ``expected_type == "object"``
    # missed the union spelling, so ``["object", "null"]`` skipped
    # ``required``/``properties`` entirely and an invalid object validated
    # (GPT review, #4974 round 2).
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required property '{key}'")
        props = schema.get("properties", {})
        for key, subschema in props.items():
            if key in value:
                errors.extend(validate_against_schema(value[key], subschema, path=f"{path}.{key}"))

    if isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(value):
                errors.extend(validate_against_schema(item, item_schema, path=f"{path}[{i}]"))

    return errors


def parse_json(text: str, *, prefer: Callable[[Any], bool] | None = None) -> Any:
    """Tolerantly parse model output as JSON; raises ``ValueError`` if not JSON.

    Handles the common cases of a fenced ```json block or surrounding prose.
    Prose recovery delegates to the shared ``llm_helpers._extract_json_of_type``
    scanner (stdlib ``raw_decode`` over successive ``{`` / ``[`` offsets), which
    skips stray braces/brackets in the prose — the previous outermost-span
    (``find``/``rfind``) approach corrupted the whole span on any stray
    delimiter. Positional scanning preserves the old guarantee that a
    prose-wrapped array yields the outer array, not an inner object.

    *prefer* narrows prose recovery when the caller knows the expected shape
    (see ``coerce_and_validate``); it is a disambiguator among prose-embedded
    candidates, not a validator — the strict whole-text parse ignores it, and
    schema validation stays the caller's job. Never executes anything (no
    ``eval``).
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
        result = _extract_json_of_type(stripped, (dict, list), prefer=prefer)
        if result is None:
            raise ValueError("model output is not valid JSON")
        return result


def _prefer_dict(value: Any) -> bool:
    """Prefer predicate for ``schema.type == "object"``: select the dict."""
    return isinstance(value, dict)


def _prefer_list(value: Any) -> bool:
    """Prefer predicate for ``schema.type == "array"``: select the list."""
    return isinstance(value, list)


# Schema ``type`` → prefer predicate for prose recovery. Only the two container
# shapes matter: scalars never survive prose recovery anyway (the scanner only
# decodes at ``{`` / ``[``).
_SHAPE_PREFERENCE: dict[str, Callable[[Any], bool]] = {
    "object": _prefer_dict,
    "array": _prefer_list,
}


def _shape_preference(expected: Any) -> Callable[[Any], bool] | None:
    """The prefer predicate a schema ``type`` implies: the single container shape
    it admits. A union list counts only its container members — ``"null"`` and
    scalar members add no prose-recoverable shape, so ``["object", "null"]``
    still prefers the object (GPT review: routing unions to no-preference let a
    worked example win first-match instead of triggering ambiguity refusal). A
    union admitting BOTH containers has no single shape to prefer."""
    if isinstance(expected, str):
        return _SHAPE_PREFERENCE.get(expected)
    if isinstance(expected, list):
        containers = {t for t in expected if isinstance(t, str) and t in _SHAPE_PREFERENCE}
        if len(containers) == 1:
            return _SHAPE_PREFERENCE[containers.pop()]
    return None


def coerce_and_validate(text: str, schema: dict) -> tuple[Optional[Any], list[str]]:
    """Parse ``text`` as JSON and validate against ``schema``.

    Returns ``(value, [])`` on success or ``(None, errors)`` when parsing fails or
    the value violates the schema. When the schema names a container ``type``,
    prose recovery prefers a value of that shape, so an object schema selects the
    object even when stray prose parses as an array first (and vice versa).
    """
    expected = schema.get("type")
    prefer = _shape_preference(expected)
    try:
        value = parse_json(text, prefer=prefer)
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
