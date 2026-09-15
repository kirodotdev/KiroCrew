"""Describe-driven Salesforce object/field model.

This module turns a Salesforce *describe* response (``GET .../sobjects/{name}/
describe``) into a typed, offline model. It is the L1 core's single source of
truth for what an object and its fields look like; every other module in this
package consumes these dataclasses rather than re-parsing raw describe JSON.

Two access axes are kept **strictly separate** and are never collapsed into one
boolean:

* **Field-level security (FLS)** -- the per-field ``createable`` / ``updateable``
  / ``accessible`` flags Salesforce returns on each field in a describe. Per
  Salesforce's own documented semantics (search-snippet corroborated; the
  official ``DescribeFieldResult`` page rejects automated fetches with HTTP 403),
  these per-field flags already fold in BOTH the object's security and the
  field's own security for the calling user -- they are a two-tier result, not a
  raw field-only signal. They live on :class:`FieldLevelSecurity`.
* **Org-level object permissions** -- whether the object as a whole is
  createable / readable (queryable) / updateable / deletable, which Salesforce
  exposes as object-level flags on the describe result and, authoritatively, via
  the ``ObjectPermissions`` sObject. These live on :class:`ObjectPermissions`.

A field's FLS ``createable`` and an object's ``createable`` answer different
questions ("may this user set THIS FIELD on create" vs. "may this user create
THIS OBJECT at all"), and merging them would silently lose the distinction a
governed dispatch needs. A validator/consumer reads each independently.

Evidence: all field semantics here are ``search_snippet_corroborated``. Any
value a describe response omits is kept as :data:`UNKNOWN` (a named sentinel),
never guessed and never defaulted to ``True``/``False``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


class SourceKind(str, enum.Enum):
    """Provenance of a piece of encoded knowledge.

    Mirrors the connector-capability-manifest ``source_kind`` closed enum. The
    only value the offline core legitimately asserts for a Salesforce fact is
    :attr:`SEARCH_SNIPPET_CORROBORATED`; :attr:`OFFICIAL_DOCS` is present for
    completeness but is not claimed by this module, because no Salesforce doc
    page rendered for automated fetching.
    """

    OFFICIAL_DOCS = "official_docs"
    REPO_PATH = "repo_path"
    FORMAT_SPEC = "format_spec"
    SEARCH_SNIPPET_CORROBORATED = "search_snippet_corroborated"
    USER_STATED = "user_stated"
    NOT_YET_SOURCED = "not_yet_sourced"


class _Unknown:
    """A named sentinel for a value the vendor response did not carry.

    Distinct from ``None`` (which a Salesforce describe uses as a legitimate
    JSON value for some fields) and from ``False`` (a real, asserted denial).
    ``UNKNOWN`` means "the source did not say", and the core preserves it rather
    than inventing a boolean. Singleton: identity comparison (``is UNKNOWN``) is
    the intended check.
    """

    _instance: Optional["_Unknown"] = None

    def __new__(cls) -> "_Unknown":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "UNKNOWN"

    def __bool__(self) -> bool:
        # Guard against a caller accidentally treating an unknown as a denial.
        raise TypeError(
            "UNKNOWN is not truth-valued; test `x is UNKNOWN` explicitly rather "
            "than relying on its boolean-ness (it is neither True nor False)."
        )


#: The singleton unknown-value sentinel. See :class:`_Unknown`.
UNKNOWN = _Unknown()

#: A three-valued flag: a real ``bool``, or :data:`UNKNOWN` when unsourced.
Ternary = Any  # bool | _Unknown -- kept loose so `is UNKNOWN` is the contract.


def _ternary(raw: Mapping[str, Any], key: str) -> Ternary:
    """Read ``key`` as a strict bool, or :data:`UNKNOWN` when absent.

    A present-but-non-bool value is a malformed describe and is surfaced as
    :data:`UNKNOWN` too (the core never coerces ``"true"`` / ``1`` into a
    boolean -- Salesforce describe uses real JSON booleans, so anything else is
    unsourced-shaped, not a value to trust).
    """

    if key not in raw:
        return UNKNOWN
    value = raw[key]
    if isinstance(value, bool):
        return value
    return UNKNOWN


@dataclass(frozen=True)
class FieldLevelSecurity:
    """Per-field access flags from a describe -- the FLS ∩ object-security view.

    These are the field's own ``createable`` / ``updateable`` / ``accessible``
    booleans as Salesforce returns them, each of which already incorporates the
    object's security and the field's security for the calling user (documented
    two-tier semantics, search-snippet corroborated). They are NOT the org-level
    object permissions -- see :class:`ObjectPermissions`.
    """

    createable: Ternary
    updateable: Ternary
    accessible: Ternary


@dataclass(frozen=True)
class ObjectPermissions:
    """Org-level object permissions -- whether the object itself is C/R/U/D-able.

    Sourced from the object-level flags on a describe result (and,
    authoritatively at runtime, from the ``ObjectPermissions`` sObject, which the
    offline core does not query). Kept separate from every field's
    :class:`FieldLevelSecurity` so a consumer never confuses "can create this
    object" with "can set this field on create".
    """

    createable: Ternary
    queryable: Ternary
    updateable: Ternary
    deletable: Ternary


@dataclass(frozen=True)
class FieldDescribe:
    """One field of an sObject, as modeled from its describe entry."""

    name: str
    soap_type: Optional[str]
    #: Salesforce field type discriminator (e.g. ``string``, ``reference``,
    #: ``boolean``, ``datetime``). ``UNKNOWN`` when the describe omitted it.
    field_type: Any
    nillable: Ternary
    #: Per-field access; never merged with the object's own permissions.
    fls: FieldLevelSecurity


@dataclass(frozen=True)
class ObjectDescribe:
    """A described sObject: its name, its object-level permissions, its fields.

    ``source_kind`` records the provenance of THIS model instance; for every
    Salesforce object modeled offline in L1 it is
    :attr:`SourceKind.SEARCH_SNIPPET_CORROBORATED`.
    """

    name: str
    label: Optional[str]
    object_permissions: ObjectPermissions
    fields: Mapping[str, FieldDescribe] = field(default_factory=dict)
    source_kind: SourceKind = SourceKind.SEARCH_SNIPPET_CORROBORATED

    def field_names(self) -> tuple[str, ...]:
        return tuple(self.fields.keys())


def parse_field_describe(raw: Mapping[str, Any]) -> FieldDescribe:
    """Parse one field entry from a describe ``fields`` array.

    Reads the FLS flags into :class:`FieldLevelSecurity` and everything else
    onto :class:`FieldDescribe`. A missing flag becomes :data:`UNKNOWN`, never a
    fabricated boolean. Raises :class:`ValueError` only when the entry has no
    ``name`` -- a field with no name is not a field.
    """

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("field describe entry missing a non-empty 'name'")
    fls = FieldLevelSecurity(
        createable=_ternary(raw, "createable"),
        updateable=_ternary(raw, "updateable"),
        accessible=_ternary(raw, "accessible"),
    )
    ftype = raw.get("type", UNKNOWN)
    return FieldDescribe(
        name=name,
        soap_type=raw.get("soapType") if isinstance(raw.get("soapType"), str) else None,
        field_type=ftype if isinstance(ftype, str) else UNKNOWN,
        nillable=_ternary(raw, "nillable"),
        fls=fls,
    )


def parse_object_describe(raw: Mapping[str, Any]) -> ObjectDescribe:
    """Parse a full sObject describe response into an :class:`ObjectDescribe`.

    Object-level permission flags are read into :class:`ObjectPermissions`,
    kept strictly apart from the per-field :class:`FieldLevelSecurity`. Fields
    are parsed in order; a malformed field entry (no name) aborts the parse
    rather than being silently dropped -- a described object with a nameless
    field is a describe the core cannot faithfully model.
    """

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("object describe missing a non-empty 'name'")
    permissions = ObjectPermissions(
        createable=_ternary(raw, "createable"),
        queryable=_ternary(raw, "queryable"),
        updateable=_ternary(raw, "updateable"),
        deletable=_ternary(raw, "deletable"),
    )
    fields: dict[str, FieldDescribe] = {}
    for entry in raw.get("fields", []) or []:
        if not isinstance(entry, Mapping):
            raise ValueError("each 'fields' entry must be an object")
        parsed = parse_field_describe(entry)
        fields[parsed.name] = parsed
    label = raw.get("label")
    return ObjectDescribe(
        name=name,
        label=label if isinstance(label, str) else None,
        object_permissions=permissions,
        fields=fields,
    )
