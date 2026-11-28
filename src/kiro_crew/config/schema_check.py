"""A small Draft-07 checker for the config schema, so no process imports jsonschema.

Every Kiro Crew process validates its config when it loads it, MCP servers
included. ``jsonschema`` and the stack it pulls in (``referencing``, ``rpds``,
``attrs``) cost about 3 MB RSS per process, so the 7 servers of a default chat
paid about 21 MB for it. The config schema (``config.schema.JSON_SCHEMA``) uses
only ``type``, ``properties``, ``additionalProperties``, ``items``, ``enum``,
``maxLength`` and ``pattern``, which this module checks the way jsonschema 4.x's
``Draft7Validator`` does: the same errors, at the same paths, with the same
messages, in the same order. ``validation.validate_config_data`` deletes fields
while it walks the errors, so the order is part of the contract.
``test/test_config_schema_check_parity.py`` compares both on random documents.

Fail closed: :func:`check_schema` raises :class:`ValueError` for any keyword
this module does not implement (``$ref``, ``oneOf``, ``minimum`` ...), so a
schema that grows one fails loudly at the first load instead of being skipped.
Teach the keyword here, and to the parity test, when that happens.
"""

from __future__ import annotations

import numbers
import re
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

DRAFT_07 = "http://json-schema.org/draft-07/schema#"

_TYPES = frozenset({"array", "boolean", "integer", "null", "number", "object", "string"})

#: Draft-07 keywords that assert nothing, which jsonschema skips as well.
_ANNOTATIONS = frozenset(
    {"$comment", "$schema", "default", "description", "examples", "readOnly", "title", "writeOnly"}
)


class ValidationError:
    """One failed assertion, with the attributes of ``jsonschema.ValidationError``
    that ``validate_config_data`` reads."""

    __slots__ = ("absolute_path", "instance", "message", "schema", "validator")

    def __init__(
        self, validator: str | None, message: str, instance: Any, schema: Any, path: tuple
    ) -> None:
        self.validator = validator
        self.message = message
        self.instance = instance
        self.schema = schema
        self.absolute_path = deque(path)

    def __repr__(self) -> str:
        return (
            f"<ValidationError {self.validator!r} at {list(self.absolute_path)!r}: {self.message}>"
        )


def _is_type(value: Any, name: str) -> bool:
    """jsonschema's draft-07 type checker: a bool is not a number, ``1.0`` is an integer."""
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    if isinstance(value, bool):
        return False
    if name == "integer":
        return isinstance(value, int) or (isinstance(value, float) and value.is_integer())
    if name == "number":
        return isinstance(value, numbers.Number)
    raise ValueError(f"unknown JSON type {name!r}")


def _equal(one: Any, two: Any) -> bool:
    """jsonschema's ``_utils.equal``: ``True`` is not ``1``, at any depth."""
    if one is two:
        return True
    if isinstance(one, str) or isinstance(two, str):
        return one == two
    if isinstance(one, Sequence) and isinstance(two, Sequence):
        return len(one) == len(two) and all(_equal(a, b) for a, b in zip(one, two))
    if isinstance(one, Mapping) and isinstance(two, Mapping):
        return len(one) == len(two) and all(
            key in two and _equal(value, two[key]) for key, value in one.items()
        )
    if isinstance(one, bool) or isinstance(two, bool):
        return False
    return one == two


def check_schema(schema: Any, _root: bool = True) -> None:
    """Raise :class:`ValueError` unless every part of *schema* is one this module checks
    exactly as Draft-07 does. Stands in for ``Draft7Validator.check_schema``."""
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        raise ValueError(f"a schema must be an object or a boolean, got {schema!r}")
    for key, value in schema.items():
        if key == "$schema":
            if not _root or value != DRAFT_07:
                raise ValueError(f"only a root $schema of {DRAFT_07!r} is supported, got {value!r}")
        elif key == "type":
            names = [value] if isinstance(value, str) else value
            if (
                not isinstance(names, list)
                or not names
                or not all(isinstance(n, str) for n in names)
            ):
                raise ValueError(f"bad type {value!r}")
            if len(set(names)) != len(names) or not set(names) <= _TYPES:
                raise ValueError(f"bad type {value!r}")
        elif key == "properties":
            if not isinstance(value, dict):
                raise ValueError(f"properties must be an object, got {value!r}")
            for sub in value.values():
                check_schema(sub, False)
        elif key in ("additionalProperties", "items"):
            # A list ``items`` (the tuple form) is not implemented: refused here.
            check_schema(value, False)
        elif key == "enum":
            if not isinstance(value, list):
                raise ValueError(f"enum must be an array, got {value!r}")
        elif key == "maxLength":
            if isinstance(value, bool) or not _is_type(value, "integer") or value < 0:
                raise ValueError(f"maxLength must be a non-negative integer, got {value!r}")
        elif key == "pattern":
            if not isinstance(value, str):
                raise ValueError(f"pattern must be a string, got {value!r}")
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"pattern {value!r} does not compile: {exc}") from exc
        elif key in _ANNOTATIONS or key.startswith("x-"):
            continue
        else:
            raise ValueError(
                f"schema keyword {key!r} is not supported by kiro_crew.config.schema_check"
            )


class SchemaChecker:
    """Stands in for ``Draft7Validator(schema)``; call :func:`check_schema` first."""

    def __init__(self, schema: dict | bool) -> None:
        self.schema = schema

    def is_valid(self, instance: Any) -> bool:
        return next(self.iter_errors(instance), None) is None

    def iter_errors(self, instance: Any) -> Iterator[ValidationError]:
        """Lazily, like jsonschema: a caller that edits *instance* between errors sees
        the walk read the edited document, just as it did with jsonschema."""
        return self._descend(instance, self.schema, ())

    def _descend(self, instance: Any, schema: Any, path: tuple) -> Iterator[ValidationError]:
        if schema is True:
            return
        if schema is False:
            # jsonschema's ``descend`` yields this error before it adds its own path
            # segment, so the path names the PARENT of the refused value. Kept as is:
            # the cleaning step must see what it saw under jsonschema.
            message = f"False schema does not allow {instance!r}"
            yield ValidationError(None, message, instance, schema, path[:-1])
            return
        for key, value in schema.items():
            if key == "type":
                names = [value] if isinstance(value, str) else value
                if not any(_is_type(instance, n) for n in names):
                    reprs = ", ".join(repr(n) for n in names)
                    yield ValidationError(
                        "type", f"{instance!r} is not of type {reprs}", instance, schema, path
                    )
            elif key == "properties":
                if isinstance(instance, dict):
                    for prop, sub in value.items():
                        if prop in instance:
                            yield from self._descend(instance[prop], sub, path + (prop,))
            elif key == "additionalProperties":
                if not isinstance(instance, dict):
                    continue
                props = schema.get("properties", {})
                # A set, as in jsonschema: its order is the order jsonschema walks.
                extras = set(prop for prop in instance if prop not in props)
                if isinstance(value, dict):
                    for extra in extras:
                        yield from self._descend(instance[extra], value, path + (extra,))
                elif not value and extras:
                    names = sorted(extras, key=str)
                    verb = "was" if len(names) == 1 else "were"
                    joined = ", ".join(repr(name) for name in names)
                    msg = f"Additional properties are not allowed ({joined} {verb} unexpected)"
                    yield ValidationError("additionalProperties", msg, instance, schema, path)
            elif key == "items":
                if isinstance(instance, list):
                    for index, item in enumerate(instance):
                        yield from self._descend(item, value, path + (index,))
            elif key == "enum":
                if all(not _equal(each, instance) for each in value):
                    yield ValidationError(
                        "enum", f"{instance!r} is not one of {value!r}", instance, schema, path
                    )
            elif key == "maxLength":
                if isinstance(instance, str) and len(instance) > value:
                    msg = "is expected to be empty" if value == 0 else "is too long"
                    yield ValidationError(
                        "maxLength", f"{instance!r} {msg}", instance, schema, path
                    )
            elif key == "pattern":
                if isinstance(instance, str) and not re.search(value, instance):
                    yield ValidationError(
                        "pattern", f"{instance!r} does not match {value!r}", instance, schema, path
                    )
