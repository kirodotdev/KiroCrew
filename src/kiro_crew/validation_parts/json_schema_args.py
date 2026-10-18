"""Validating untrusted tool arguments against a declared MCP ``inputSchema``.

:func:`validate_mcp_tool_arguments` enforces a fail-closed JSON Schema subset for
callers that are not the model (an MCP App iframe relaying ``tools/call``); a schema
``pattern`` is matched in a killable child process.

Callers import these names from :mod:`kiro_crew.validation`, which serves them; this
module never imports that facade.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

from kiro_crew.validation_parts.field_engine import ValidationError

# ── MCP inputSchema validation (JSON Schema subset, fail-closed) ──

#: Hard caps so an adversarial payload/schema cannot DoS the validator.
_MCP_ARGS_MAX_DEPTH = 16
_MCP_ARGS_MAX_STRING = 1_000_000  # 1MB per string leaf

#: Bounds for evaluating a schema ``pattern`` against an untrusted value. The
#: PATTERN is server-controlled and the VALUE is app-controlled — a
#: pathological pair (e.g. ``(a+)+$`` + a long near-match) is a classic ReDoS.
_PATTERN_MAX_LEN = 512
_PATTERN_VALUE_MAX_LEN = 4096
_PATTERN_TIMEOUT_SECS = 2.0

#: Child source for the sandboxed match. CPython's ``re`` engine holds the GIL
#: for the entire match, so an in-process timeout (thread + join) cannot stop a
#: catastrophic backtrack — the abandoned worker would starve the whole
#: process. A subprocess is the only stdlib mechanism that both bounds the
#: wall clock AND kills the runaway CPU (``subprocess.run`` kills the child on
#: timeout). Exit codes: 0 = matched, 1 = no match, 3 = invalid pattern.
_PATTERN_CHILD_SRC = (
    "import json,re,sys\n"
    "d=json.load(sys.stdin)\n"
    "try:\n"
    "    sys.exit(0 if re.search(d['p'], d['v']) else 1)\n"
    "except re.error:\n"
    "    sys.exit(3)\n"
)


def _bounded_pattern_search(pattern: str, value: str) -> bool | None:
    """``re.search`` with hard input-size caps and a killable wall-clock bound.

    Returns True/False for a completed match check, or ``None`` when the
    check could not be performed safely (oversized pattern/value, invalid
    pattern, or timeout) — callers treat ``None`` as fail-closed. The match
    runs in a short-lived subprocess that is KILLED on timeout, so a
    server-supplied ReDoS pattern cannot burn CPU beyond the bound (an
    in-process thread cannot be stopped and would hold the GIL for the whole
    catastrophic match). Pattern checks happen at app-interaction rate, so
    the interpreter-startup cost is acceptable.
    """
    if len(pattern) > _PATTERN_MAX_LEN or len(value) > _PATTERN_VALUE_MAX_LEN:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, own interpreter
            [sys.executable, "-I", "-c", _PATTERN_CHILD_SRC],
            input=json.dumps({"p": pattern, "v": value}).encode("utf-8"),
            capture_output=True,
            timeout=_PATTERN_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        return None  # runaway match killed — fail closed
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None  # invalid pattern (or unexpected child failure) — fail closed


_JSON_SCHEMA_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "object": dict,
    "array": list,
    "null": type(None),
}


def _json_type_ok(value: Any, type_name: str) -> bool:
    expected = _JSON_SCHEMA_TYPES.get(type_name)
    if expected is None:
        # Unknown type name in the schema — fail closed rather than admit.
        return False
    if not isinstance(value, expected):
        return False
    # bool is a subclass of int: keep number/integer honest.
    if isinstance(value, bool) and type_name in ("number", "integer"):
        return False
    return True


# Validation keywords the subset ENFORCES. A schema using any validation
# keyword outside this set (oneOf, allOf, $ref, patternProperties, if/then,
# multipleOf, contains, …) is REJECTED fail-closed rather than partially
# enforced — ignoring a constraint the tool author wrote would forward values
# the tool declared forbidden.
_SUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "type",
        "required",
        "properties",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "pattern",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
    }
)

# Annotation-only keywords (non-validating per JSON Schema draft 2020-12 —
# ``format`` is annotation-only unless the format-assertion vocabulary is
# explicitly enabled, which MCP does not require). Safe to ignore.
_ANNOTATION_SCHEMA_KEYWORDS = frozenset(
    {
        "title",
        "description",
        "default",
        "examples",
        "format",
        "$schema",
        "$id",
        "$comment",
        "deprecated",
        "readOnly",
        "writeOnly",
    }
)


def _reject_unsupported_keywords(schema: dict, path: str) -> None:
    unsupported = [
        k
        for k in schema
        if k not in _SUPPORTED_SCHEMA_KEYWORDS and k not in _ANNOTATION_SCHEMA_KEYWORDS
    ]
    if unsupported:
        raise ValidationError(
            path,
            "schema uses unsupported validation keyword(s) "
            f"{sorted(unsupported)}; failing closed",
        )


def _json_equal(a: Any, b: Any) -> bool:
    """JSON-semantics equality for ``enum``/``const``/``uniqueItems``.

    Python conflates ``True == 1`` and ``False == 0``, so a schema
    ``{"enum": [1]}`` or ``{"const": 1}`` would wrongly admit boolean ``true``
    (and vice-versa), forwarding a value the declared contract forbids. JSON
    keeps booleans and numbers as distinct types, so a boolean may equal ONLY a
    boolean. Numeric ``1 == 1.0`` is preserved (JSON has one number type).
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    return a == b


def _reject_malformed_keyword_shapes(schema: dict[str, Any], _path: str) -> None:
    """Reject a keyword present with the WRONG value shape (e.g. ``"type": 7``).

    Fail-closed: a malformed constraint must never be silently skipped and
    treated as "no constraint". Only shape is checked here — semantics are
    enforced by the main validator.
    """

    def _bad(key: str) -> None:
        raise ValidationError(_path, f"malformed `{key}` keyword shape")

    t = schema.get("type")
    if "type" in schema and not (
        isinstance(t, str) or (isinstance(t, list) and all(isinstance(x, str) for x in t))
    ):
        _bad("type")
    if "enum" in schema and not isinstance(schema["enum"], list):
        _bad("enum")
    if "required" in schema and not (
        isinstance(schema["required"], list) and all(isinstance(x, str) for x in schema["required"])
    ):
        _bad("required")
    if "properties" in schema and not isinstance(schema["properties"], dict):
        _bad("properties")
    if isinstance(schema.get("properties"), dict):
        # Every property VALUE must be a subschema (object) or a boolean
        # subschema. A malformed value (e.g. the string "false") would otherwise
        # recurse to the "no subschema → return" branch and admit the field
        # UNVALIDATED — a fail-open hole. Reject the schema instead.
        for _pk, _pv in schema["properties"].items():
            if not isinstance(_pv, (dict, bool)):
                _bad("properties")
    if "additionalProperties" in schema and not isinstance(
        schema["additionalProperties"], (dict, bool)
    ):
        # Same fail-open risk for a malformed additionalProperties subschema.
        _bad("additionalProperties")
    if "items" in schema and not isinstance(schema["items"], (dict, bool)):
        _bad("items")
    if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
        _bad("uniqueItems")
    if "pattern" in schema and not isinstance(schema["pattern"], str):
        _bad("pattern")
    for k in ("minLength", "maxLength", "minItems", "maxItems"):
        if k in schema and (not isinstance(schema[k], int) or isinstance(schema[k], bool)):
            _bad(k)
    for k in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if k in schema and (not isinstance(schema[k], (int, float)) or isinstance(schema[k], bool)):
            _bad(k)


def validate_mcp_tool_arguments(
    arguments: Any, input_schema: Any, *, _path: str = "arguments", _depth: int = 0
) -> None:
    """Validate untrusted tool ``arguments`` against a declared MCP
    ``inputSchema`` (a JSON Schema subset). Raises :class:`ValidationError`.

    This is the shared boundary check for tool inputs that arrive from
    UNTRUSTED callers (e.g. an embedded MCP App iframe relaying
    ``tools/call``) rather than from the model. Fail-closed semantics —
    deliberately stricter than vanilla JSON Schema:

    * A tool that declares no usable ``inputSchema`` accepts ONLY ``{}``.
    * When ``properties`` is declared, unknown keys are rejected unless the
      schema explicitly sets ``additionalProperties`` to ``true`` or a schema.
    * Unknown ``type`` names in the schema reject rather than admit.
    * A schema using a validation keyword this subset does not enforce
      (``oneOf``, ``$ref``, ``multipleOf``, …) REJECTS the call outright —
      constraints are never silently dropped. Annotation-only keywords
      (``title``, ``description``, ``format``, …) are ignored.

    Enforced keywords: ``type`` (str or list), ``required``, ``properties``,
    ``additionalProperties``, ``items``, ``enum``, ``const``, ``pattern``,
    ``minimum``/``maximum`` (+ exclusive forms), ``minLength``/``maxLength``,
    ``minItems``/``maxItems``, ``uniqueItems``. Transport-level caps bound
    depth and string size regardless of schema.
    """
    if _depth > _MCP_ARGS_MAX_DEPTH:
        raise ValidationError(_path, "exceeds max nesting depth")
    if isinstance(arguments, str) and len(arguments) > _MCP_ARGS_MAX_STRING:
        raise ValidationError(_path, "string exceeds max length")

    # JSON Schema boolean subschemas: `true` accepts anything, `false` accepts
    # nothing. Handle explicitly — otherwise a `false` subschema (e.g.
    # `properties.x: false`, or `items: false`) falls through the dict gate and
    # is silently treated as "no constraint", forwarding a forbidden value.
    if input_schema is True:
        return
    if input_schema is False:
        raise ValidationError(_path, "value forbidden by `false` schema")

    if _depth == 0:
        if not isinstance(arguments, dict):
            raise ValidationError(_path, "must be a JSON object")
        if not isinstance(input_schema, dict):
            # No declared contract → only the empty call is admissible.
            if arguments:
                raise ValidationError(
                    _path, "tool declares no inputSchema; only empty arguments allowed"
                )
            return

    schema = input_schema if isinstance(input_schema, dict) else None
    if schema is None:
        return  # nested position with no subschema — nothing more to check
    _reject_unsupported_keywords(schema, _path)
    _reject_malformed_keyword_shapes(schema, _path)

    declared_type = schema.get("type")
    if isinstance(declared_type, str):
        if not _json_type_ok(arguments, declared_type):
            raise ValidationError(
                _path, f"expected {declared_type}, got {type(arguments).__name__}"
            )
    elif isinstance(declared_type, list):
        if not any(isinstance(t, str) and _json_type_ok(arguments, t) for t in declared_type):
            raise ValidationError(
                _path,
                f"expected one of {declared_type}, got {type(arguments).__name__}",
            )

    enum = schema.get("enum")
    if isinstance(enum, list) and not any(_json_equal(arguments, e) for e in enum):
        raise ValidationError(_path, "not in enum")
    if "const" in schema and not _json_equal(arguments, schema["const"]):
        raise ValidationError(_path, "does not equal const")

    if isinstance(arguments, str):
        min_len = schema.get("minLength")
        max_len = schema.get("maxLength")
        if isinstance(min_len, int) and len(arguments) < min_len:
            raise ValidationError(_path, f"shorter than minLength {min_len}")
        if isinstance(max_len, int) and len(arguments) > max_len:
            raise ValidationError(_path, f"exceeds maxLength {max_len}")
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            verdict = _bounded_pattern_search(pattern, arguments)
            if verdict is None:
                # Oversized input, invalid pattern, or wall-clock timeout
                # (possible ReDoS) — fail closed rather than forward a value
                # whose declared constraint could not be safely checked.
                raise ValidationError(_path, "pattern constraint could not be safely evaluated")
            if not verdict:
                raise ValidationError(_path, "does not match pattern")

    if isinstance(arguments, (int, float)) and not isinstance(arguments, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and arguments < minimum:
            raise ValidationError(_path, f"must be >= {minimum}")
        if isinstance(maximum, (int, float)) and arguments > maximum:
            raise ValidationError(_path, f"must be <= {maximum}")
        exc_min = schema.get("exclusiveMinimum")
        exc_max = schema.get("exclusiveMaximum")
        if isinstance(exc_min, (int, float)) and arguments <= exc_min:
            raise ValidationError(_path, f"must be > {exc_min}")
        if isinstance(exc_max, (int, float)) and arguments >= exc_max:
            raise ValidationError(_path, f"must be < {exc_max}")

    if isinstance(arguments, list):
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(arguments) > max_items:
            raise ValidationError(_path, f"exceeds maxItems {max_items}")
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(arguments) < min_items:
            raise ValidationError(_path, f"fewer than minItems {min_items}")
        if schema.get("uniqueItems") is True:
            # O(n) via canonical JSON keys instead of O(n^2) ``in`` scans (an
            # app-controlled array could otherwise saturate a validation
            # worker). ``json.dumps`` renders ``true``/``1`` distinctly, so this
            # ALSO preserves JSON boolean-vs-number semantics (``True != 1``).
            seen_keys: set[str] = set()
            for item in arguments:
                try:
                    key = json.dumps(item, sort_keys=True, separators=(",", ":"))
                except (TypeError, ValueError):
                    key = repr(item)
                if key in seen_keys:
                    raise ValidationError(_path, "items not unique")
                seen_keys.add(key)
        items = schema.get("items")
        if isinstance(items, (dict, bool)):
            # dict subschema OR a boolean schema (`items: false` rejects every
            # element; `items: true` accepts). Recursion handles all three.
            for i, item in enumerate(arguments):
                validate_mcp_tool_arguments(item, items, _path=f"{_path}[{i}]", _depth=_depth + 1)
        else:
            # No item schema: still bound depth/size of each element.
            for i, item in enumerate(arguments):
                validate_mcp_tool_arguments(item, {}, _path=f"{_path}[{i}]", _depth=_depth + 1)

    if isinstance(arguments, dict):
        properties = schema.get("properties")
        props = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                if isinstance(key, str) and key not in arguments:
                    raise ValidationError(f"{_path}.{key}", "required")
        additional = schema.get("additionalProperties")
        allow_extra = additional is True or isinstance(additional, dict)
        for key, value in arguments.items():
            if not isinstance(key, str):
                raise ValidationError(_path, "object keys must be strings")
            sub = props.get(key)
            if sub is not None:
                validate_mcp_tool_arguments(value, sub, _path=f"{_path}.{key}", _depth=_depth + 1)
            elif additional is False or (isinstance(properties, dict) and not allow_extra):
                # Fail-closed: an explicit ``additionalProperties: false``
                # rejects every undeclared key even when the schema declares
                # NO ``properties`` map at all (i.e. it accepts only ``{}``),
                # and schemas that enumerate properties reject unlisted keys
                # unless additionalProperties opts in.
                raise ValidationError(f"{_path}.{key}", "unknown field")
            elif isinstance(additional, dict):
                validate_mcp_tool_arguments(
                    value, additional, _path=f"{_path}.{key}", _depth=_depth + 1
                )
            else:
                # No properties map at all: bound depth/size of the blob.
                validate_mcp_tool_arguments(value, {}, _path=f"{_path}.{key}", _depth=_depth + 1)
