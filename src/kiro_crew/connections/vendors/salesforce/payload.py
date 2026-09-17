"""Typed payload parsing against a describe model.

Given an :class:`~kiro_crew.connections.vendors.salesforce.describe.ObjectDescribe` and a
raw record dict (as a Salesforce REST query/retrieve returns), this module
produces typed field values. It is deliberately conservative: it maps only the
Salesforce field-type discriminators whose runtime JSON shape is corroborated,
and it NEVER silently coerces an unrecognized or mistyped value -- an
unexpected shape raises :class:`PayloadParseError` so a caller sees the vendor
drift rather than a quietly wrong value.

The parser does not invent field metadata: a scalar field absent from the
describe is rejected (the describe is the contract), and a field whose describe
``type`` is ``UNKNOWN`` is passed through verbatim rather than coerced, because
the core has no corroborated type to coerce it to.

Two SOQL shapes are NOT plain scalar fields of the queried object and are
handled explicitly rather than rejected as "undescribed":

* **Relationship traversal** (``SELECT Owner.Name FROM Account``) nests a child
  record under the relationship key: ``{"Owner": {"attributes": {"type":
  "User", ...}, "Name": ...}}``. The child is a record of a DIFFERENT object,
  identified by its own ``attributes.type``. The offline core has no describe
  for that related object, so it recurses under ``UNKNOWN`` semantics -- it
  preserves the child's ``attributes`` and returns each of the child's own
  values verbatim, never guessing the related object's field types and never
  silently coercing a value. When a describe for the related object IS supplied
  (``related`` argument), the child is parsed against it. A **nullable** parent
  relationship is legitimately ``null`` (``{"Parent__r": null}`` for an empty
  optional lookup); because the describe declares the relationship name, that
  ``null`` is returned as ``None`` rather than mistaken for a field-name typo.
* **Parent-to-child subquery** (``SELECT Id, (SELECT Id FROM Contacts) FROM
  Account``) nests a child-query result envelope under the child-relationship
  key: ``{"Contacts": {"totalSize": N, "done": bool, "records": [...]}}``. Its
  ``records`` are parsed one by one (each against its own ``attributes.type``);
  ``totalSize`` / ``done`` / ``nextRecordsUrl`` are envelope metadata passed
  through verbatim.
* **Aggregate aliases** (``SELECT COUNT(Id) FROM Account`` -> ``{"expr0": 12}``)
  are computed columns with no field of their own; an ``exprN`` key is passed
  through verbatim as a distinct, testable case.

None of these shapes widens the general rule: a plain scalar key that is not in
the describe, is not an ``exprN`` alias, is not a declared relationship name,
and is not a nested record / child-query envelope still raises, so a genuine
field-name typo is still caught.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from kiro_crew.connections.vendors.salesforce.describe import UNKNOWN, ObjectDescribe


class PayloadParseError(ValueError):
    """A record payload did not match the shape its describe declares."""


#: Salesforce names an aggregate-query column with no explicit alias ``exprN``
#: (``expr0``, ``expr1``, ...): ``SELECT COUNT(Id), MAX(CreatedDate) FROM Account``
#: returns ``{"attributes": ..., "expr0": 12, "expr1": "..."}``. Such a key is a
#: computed aggregate, not a field of the queried object, so it has no describe
#: entry by construction. It is passed through verbatim (see :func:`parse_record`)
#: rather than parsed as a typed field or rejected as an unknown field -- but the
#: match is anchored to this exact shape so it cannot mask a genuine field-name
#: typo (which stays a :class:`PayloadParseError`).
_AGGREGATE_ALIAS_RE = re.compile(r"^expr\d+$")


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


def _is_relationship_subobject(value: Any) -> bool:
    """Whether ``value`` is a nested relationship record (has its own envelope).

    A relationship traversal (``SELECT Owner.Name``) nests a child RECORD -- a
    mapping carrying its own ``attributes`` envelope with a ``type`` -- under the
    relationship key. That is structurally distinct from a scalar value or a
    plain JSON object, so recognizing it here does NOT widen the undescribed-
    scalar-field rule: only a value that is itself a record is treated as a
    relationship subobject.
    """

    if not isinstance(value, Mapping):
        return False
    attributes = value.get("attributes")
    return isinstance(attributes, Mapping) and isinstance(attributes.get("type"), str)


def _is_child_query_envelope(value: Any) -> bool:
    """Whether ``value`` is a parent-to-child subquery result envelope.

    ``SELECT Id, (SELECT Id FROM Contacts) FROM Account`` nests a child-query
    result under the child-relationship key: ``{"Contacts": {"totalSize": N,
    "done": bool, "records": [...]}}``. It is recognized structurally by the
    ``records`` list plus the ``done`` flag the REST query envelope always
    carries, so it is distinct from a single related record (which has
    ``attributes``, not ``records``).
    """

    if not isinstance(value, Mapping):
        return False
    return isinstance(value.get("records"), list) and isinstance(value.get("done"), bool)


def _parse_child_query_envelope(
    value: Mapping[str, Any],
    related: Mapping[str, ObjectDescribe] | None,
) -> dict[str, Any]:
    """Parse a parent-to-child subquery envelope, record by record.

    ``totalSize`` / ``done`` / ``nextRecordsUrl`` are envelope metadata and pass
    through verbatim. Each entry of ``records`` is a child record parsed by
    :func:`_parse_related_record` -- against the child object's describe when one
    is supplied in ``related``, otherwise verbatim under ``UNKNOWN`` semantics.
    An envelope has no single ``attributes.type``, so its records' types are read
    per-record from each record's own ``attributes.type``.
    """

    out: dict[str, Any] = {}
    for key, sub in value.items():
        if key == "records":
            parsed_records = []
            for rec in sub:
                if _is_relationship_subobject(rec):
                    parsed_records.append(_parse_related_record(rec, related))
                else:
                    # A child record with no attributes envelope: preserve
                    # verbatim rather than guess its object type.
                    parsed_records.append(rec)
            out[key] = parsed_records
        else:
            # totalSize / done / nextRecordsUrl and any other envelope metadata.
            out[key] = sub
    return out


def _parse_related_record(
    value: Mapping[str, Any],
    related: Mapping[str, ObjectDescribe] | None,
) -> dict[str, Any]:
    """Parse a nested relationship record.

    If a describe for the child's ``attributes.type`` is supplied in ``related``,
    the child is parsed against it (recursively, so ``related`` also covers the
    child's own relationships). Otherwise the child is returned under ``UNKNOWN``
    semantics: its ``attributes`` envelope is preserved and each of its own
    values is passed through VERBATIM -- the core has no describe for the related
    object, so it neither guesses a type nor coerces a value.
    """

    child_type = value["attributes"]["type"]
    child_describe = related.get(child_type) if related else None
    if child_describe is not None:
        return parse_record(child_describe, value, related=related)
    # No describe for the related object -> UNKNOWN semantics: preserve verbatim,
    # but keep recursing so a deeper relationship the caller DID supply a describe
    # for is still parsed, and a nested aggregate/relationship/child-query shape
    # is handled by the same rules rather than special-cased away.
    out: dict[str, Any] = {}
    for key, sub in value.items():
        if key == "attributes":
            out[key] = sub
        elif _AGGREGATE_ALIAS_RE.match(key):
            out[key] = sub
        elif _is_relationship_subobject(sub):
            out[key] = _parse_related_record(sub, related)
        elif _is_child_query_envelope(sub):
            out[key] = _parse_child_query_envelope(sub, related)
        else:
            out[key] = sub
    return out


def parse_record(
    describe: ObjectDescribe,
    record: Mapping[str, Any],
    *,
    related: Mapping[str, ObjectDescribe] | None = None,
) -> dict[str, Any]:
    """Parse a whole record dict against a describe, field by field.

    * The Salesforce ``attributes`` envelope (``{"type": ..., "url": ...}``) that
      every REST record carries is passed through unparsed under its own key --
      it is metadata, not a described field.
    * A key that names a **relationship the describe declares** (a reference
      field's parent ``relationship_name``, or a parent-to-child
      ``child_relationship``) is a relationship key. Its value may legitimately
      be ``None`` -- a nullable parent lookup that is empty -- in which case it
      is returned as ``None`` rather than rejected; a nested record under it is a
      relationship traversal, and a ``{records, done}`` value is a child-query
      envelope. Knowing the declared relationship names is what tells a
      legitimately-``null`` optional lookup apart from a field-name typo.
    * A **relationship subobject** (a nested record with its own ``attributes``)
      or a **child-query envelope** (``{totalSize, done, records}``) is parsed
      structurally even when its key was not matched as a declared relationship
      name -- a describe may omit ``relationshipName`` / ``childRelationships``.
    * An **aggregate alias** (an ``exprN`` key) is passed through verbatim.
    * Every other key is routed through :func:`parse_typed_field`, so a mistyped
      OR undescribed scalar field surfaces as a :class:`PayloadParseError` rather
      than a silently wrong value -- this is what keeps field-name-typo detection
      intact; the shapes above are recognized structurally or by the describe's
      own declared relationship names, not by relaxing this rule.
    """

    relationship_names = describe.relationship_names()
    out: dict[str, Any] = {}
    for key, value in record.items():
        if key == "attributes":
            out[key] = value
        elif _AGGREGATE_ALIAS_RE.match(key):
            # Computed aggregate column (expr0, expr1, ...): no field of its own,
            # passed through verbatim rather than typed or rejected.
            out[key] = value
        elif key in relationship_names:
            # A relationship the describe declares: null (empty optional lookup),
            # a nested related record, or a child-query envelope are all valid.
            if value is None:
                out[key] = None
            elif _is_child_query_envelope(value):
                out[key] = _parse_child_query_envelope(value, related)
            elif _is_relationship_subobject(value):
                out[key] = _parse_related_record(value, related)
            else:
                # A declared relationship key carrying something that is neither
                # null, a child-query envelope, nor a related record is vendor
                # drift the caller must see.
                raise PayloadParseError(
                    f"{describe.name}.{key}: relationship key carried an "
                    f"unexpected shape ({type(value).__name__}); expected null, a "
                    "related record, or a child-query envelope"
                )
        elif _is_child_query_envelope(value):
            # Child-query envelope whose key the describe did not declare as a
            # childRelationship (describe may omit childRelationships).
            out[key] = _parse_child_query_envelope(value, related)
        elif _is_relationship_subobject(value):
            # Relationship traversal whose key the describe did not declare as a
            # reference relationshipName (describe may omit it).
            out[key] = _parse_related_record(value, related)
        else:
            out[key] = parse_typed_field(describe, key, value)
    return out
