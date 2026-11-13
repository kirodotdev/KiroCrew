"""Record codec shared by every Shared Crew contract.

Every persisted or wire record carries ``schema_version`` and round-trips the
keys it does not own. A reader older than the writer keeps the newer fields it
cannot interpret, so a rewrite by an older gateway never deletes data a newer
one recorded. A record whose ``schema_version`` is newer than
:data:`SCHEMA_VERSION` is still readable for the fields this build knows; it is
refused only when a required field is missing or malformed.

This version is the Shared Crew record version. It is unrelated to the platform
``CONTRACT_VERSION`` in ``kiro_crew.platform.context``.
"""

from __future__ import annotations

import enum
import math
from typing import Any, Mapping, TypeVar

#: Version written on every Shared Crew record this build produces.
SCHEMA_VERSION = 1

E = TypeVar("E", bound=enum.Enum)


class ContractError(ValueError):
    """A record that cannot be decoded into its contract type.

    ``code`` is a stable machine-readable identifier; ``field`` names the key
    that failed so a caller can log it without echoing the value.
    """

    def __init__(self, code: str, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.code = code
        self.field = field


def require_mapping(raw: object, field: str) -> Mapping[str, Any]:
    """Return *raw* as a mapping or raise ``ContractError``."""
    if not isinstance(raw, Mapping):
        raise ContractError("malformed_record", field, "expected an object")
    return raw


def split_extra(raw: Mapping[str, Any], known: frozenset[str]) -> dict[str, Any]:
    """Return the keys of *raw* this build does not own, preserved verbatim."""
    return {k: v for k, v in raw.items() if k not in known}


def read_schema_version(raw: Mapping[str, Any]) -> int:
    """Return the record's schema version; a missing one is version 1."""
    value = raw.get("schema_version", 1)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractError("malformed_record", "schema_version", "expected a positive integer")
    return value


def req_str(raw: Mapping[str, Any], field: str, *, allow_empty: bool = False) -> str:
    """Return a required string field."""
    value = raw.get(field)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ContractError("malformed_record", field, "expected a non-empty string")
    return value


def opt_str(raw: Mapping[str, Any], field: str) -> str | None:
    """Return an optional string field; ``None`` and absence are equivalent."""
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContractError("malformed_record", field, "expected a string or null")
    return value


def opt_float(raw: Mapping[str, Any], field: str) -> float | None:
    """Return an optional finite number field."""
    value = raw.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError("malformed_record", field, "expected a number or null")
    try:
        number = float(value)
    except OverflowError:
        raise ContractError("malformed_record", field, "expected a finite number") from None
    if not math.isfinite(number):
        raise ContractError("malformed_record", field, "expected a finite number")
    return number


def req_float(raw: Mapping[str, Any], field: str) -> float:
    """Return a required finite number field."""
    value = opt_float(raw, field)
    if value is None:
        raise ContractError("malformed_record", field, "expected a number")
    return value


def opt_int(raw: Mapping[str, Any], field: str, *, minimum: int = 0) -> int | None:
    """Return an optional integer field no smaller than *minimum*."""
    value = raw.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError("malformed_record", field, f"expected an integer >= {minimum}")
    return value


def req_int(raw: Mapping[str, Any], field: str, *, minimum: int = 0) -> int:
    """Return a required integer field no smaller than *minimum*."""
    value = opt_int(raw, field, minimum=minimum)
    if value is None:
        raise ContractError("malformed_record", field, "expected an integer")
    return value


def req_enum(raw: Mapping[str, Any], field: str, enum_type: type[E]) -> E:
    """Return a required enum field, refusing a value this build does not define."""
    value = raw.get(field)
    try:
        return enum_type(value)
    except ValueError:
        raise ContractError("unknown_enum_value", field, f"not a {enum_type.__name__}") from None


def enum_or(raw: Mapping[str, Any], field: str, enum_type: type[E], fallback: E) -> E:
    """Return an enum field, or *fallback* when absent or not defined here.

    Used for fields where a newer writer may add a value: an older reader maps
    it to the explicit fallback (normally an ``UNKNOWN`` member) instead of
    refusing the whole record.
    """
    value = raw.get(field)
    try:
        return enum_type(value)
    except ValueError:
        return fallback


def merge_extra(body: dict[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    """Return *body* with preserved unknown keys added; owned keys win."""
    out = dict(extra)
    out.update(body)
    return out
