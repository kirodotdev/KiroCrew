"""Typed payload parsing against a describe model.

Given an :class:`~kiro_crew.connections.vendors.salesforce.describe.ObjectDescribe` and a
raw record dict (as a Salesforce REST query/retrieve returns), this module
produces typed field values. It is deliberately conservative: it maps only the
Salesforce field-type discriminators whose runtime JSON shape is corroborated,
and it NEVER silently coerces an unrecognized or mistyped value -- an
unexpected shape raises :class:`PayloadParseError` so a caller sees the vendor
drift rather than a quietly wrong value.

The parser does not invent field metadata: a field absent from the describe is
rejected (the describe is the contract), and a field whose describe ``type`` is
``UNKNOWN`` is passed through verbatim rather than coerced, because the core has
no corroborated type to coerce it to.
"""

from __future__ import annotations

from typing import Any, Mapping

from kiro_crew.connections.vendors.salesforce.describe import UNKNOWN, ObjectDescribe


class PayloadParseError(ValueError):
    """A record payload did not match the shape its describe declares."""


#: Salesforce field-type discriminators the core maps to a Python runtime type,
#: with the JSON type it is corroborated to arrive as. Anything not in this map
#: is passed through untouched (see :func:`parse_typed_field`) -- the core does
#: not fabricate a coercion for a type it has not corroborated.
_JSON_TYPE_BY_FIELD_TYPE: dict[str, type] = {
    "string": str,
    "picklist": str,
    "id": str,
    "reference": str,
    "textarea": str,
    "url": str,
    "email": str,
    "phone": str,
    "boolean": bool,
    "int": int,
    "double": float,
    "currency": float,
    "percent": float,
    "date": str,  # ISO-8601 date string; not parsed to a date object offline.
    "datetime": str,  # ISO-8601 datetime string; kept as string.
}


def parse_typed_field(describe: ObjectDescribe, field_name: str, raw_value: Any) -> Any:
    """Parse one field value against its describe entry.

    * A ``None`` value is returned as ``None`` -- Salesforce uses JSON null for
      an empty field, and the field's describe ``nillable`` governs whether that
      is legal, which is a validation concern left to the caller, not a parse
      error here.
    * A field whose describe ``type`` is a mapped discriminator must arrive as
      the corresponding JSON type; a mismatch raises :class:`PayloadParseError`.
    * A field whose describe ``type`` is ``UNKNOWN`` or an unmapped discriminator
      is returned verbatim -- the core has no corroborated coercion for it.

    Raises :class:`PayloadParseError` if ``field_name`` is not in the describe:
    the describe is the authoritative field set, and a value for an undescribed
    field is unmodelable.
    """

    fd = describe.fields.get(field_name)
    if fd is None:
        raise PayloadParseError(
            f"{describe.name}.{field_name}: field is not in the object's describe"
        )
    if raw_value is None:
        return None
    ftype = fd.field_type
    if ftype is UNKNOWN or not isinstance(ftype, str) or ftype not in _JSON_TYPE_BY_FIELD_TYPE:
        # No corroborated coercion; preserve the vendor value as-is.
        return raw_value
    expected = _JSON_TYPE_BY_FIELD_TYPE[ftype]
    # bool is an int subclass in Python; guard so a JSON boolean does not pass
    # for an int/double field and vice-versa.
    if expected is int:
        if isinstance(raw_value, bool) or not isinstance(raw_value, int):
            raise PayloadParseError(
                f"{describe.name}.{field_name}: expected int for type '{ftype}', "
                f"got {type(raw_value).__name__}"
            )
        return raw_value
    if expected is float:
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise PayloadParseError(
                f"{describe.name}.{field_name}: expected number for type '{ftype}', "
                f"got {type(raw_value).__name__}"
            )
        # A numeric field accepts either a JSON integer or a JSON float. An int
        # is returned verbatim -- coercing it to float would silently corrupt any
        # integer above 2**53 (float's exact-integer ceiling), which is exactly
        # the kind of unrecoverable, no-error data loss the core must not
        # introduce. A float stays a float.
        return raw_value
    if not isinstance(raw_value, expected):
        raise PayloadParseError(
            f"{describe.name}.{field_name}: expected {expected.__name__} for type "
            f"'{ftype}', got {type(raw_value).__name__}"
        )
    return raw_value


def parse_record(describe: ObjectDescribe, record: Mapping[str, Any]) -> dict[str, Any]:
    """Parse a whole record dict against a describe, field by field.

    The Salesforce ``attributes`` envelope (``{"type": ..., "url": ...}``) that
    every REST record carries is passed through unparsed under its own key -- it
    is metadata, not a described field. Every other key is routed through
    :func:`parse_typed_field`, so a mistyped field anywhere in the record
    surfaces as a :class:`PayloadParseError` rather than a silently wrong value.
    """

    out: dict[str, Any] = {}
    for key, value in record.items():
        if key == "attributes":
            out[key] = value
            continue
        out[key] = parse_typed_field(describe, key, value)
    return out
