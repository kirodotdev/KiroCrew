"""What one crew log entry-type declaration is written in.

The JSON value types a declared field may hold, the frozen :class:`Field` and
:class:`EntryType` records the registry is built from, and ``_turn``, the one field
builder two families share. :mod:`kiro_crew.crew_log.entry_types` re-exports every
public name here, and callers import them from there.

This is the one module both the registry and every family owner import, which is why it
stands alone: an owner never imports the registry module, and a family owner never
imports another family.
"""

from __future__ import annotations

from dataclasses import dataclass

#: JSON types a declared field may hold. ``int`` and ``float`` are separate
#: because the wire format's numbers are separate to a reader: a count is not a
#: measurement. ``float`` accepts an int, since JSON has one number type and 0 is
#: a legal reading of a percentage; ``int`` does not accept a float, because a
#: fractional token count or millisecond is a bug at the site that built it.
JSON_STRING = "string"
JSON_INT = "int"
JSON_FLOAT = "float"
JSON_BOOL = "bool"
JSON_OBJECT = "object"
JSON_ARRAY = "array"

JSON_TYPES: frozenset[str] = frozenset(
    {JSON_STRING, JSON_INT, JSON_FLOAT, JSON_BOOL, JSON_OBJECT, JSON_ARRAY}
)


@dataclass(frozen=True)
class Field:
    """One declared key of an entry's ``data``.

    ``fields`` describes the members of an object -- either this field's own, when
    ``json_type`` is :data:`JSON_OBJECT`, or its ELEMENTS', when ``json_type`` is
    :data:`JSON_ARRAY` and ``item_type`` is :data:`JSON_OBJECT`. One attribute
    serves both because the rules applied to a member and to an element's member
    are the same rules.
    """

    name: str
    json_type: str
    required: bool = False
    enum: tuple[str, ...] = ()
    enum_closed: bool = False
    item_type: str = ""
    fields: tuple["Field", ...] = ()
    note: str = ""

    def __post_init__(self) -> None:
        # A declaration is repo data, so a wrong one is a programming error rather
        # than a refused write: it is caught here, at import, instead of becoming a
        # check that silently passes everything.
        if self.json_type not in JSON_TYPES:
            raise ValueError(f"field {self.name!r} declares unknown json type {self.json_type!r}")
        if self.json_type == JSON_ARRAY and self.item_type not in JSON_TYPES:
            raise ValueError(f"array field {self.name!r} must declare an item_type")
        if self.fields and not (
            self.json_type == JSON_OBJECT
            or (self.json_type == JSON_ARRAY and self.item_type == JSON_OBJECT)
        ):
            raise ValueError(f"field {self.name!r} declares members but holds no object")
        if self.enum_closed and not self.enum:
            raise ValueError(f"field {self.name!r} is a closed enum with no values")


@dataclass(frozen=True)
class EntryType:
    """One declared entry type: what it says, and what its ``data`` carries."""

    type: str
    summary: str
    fields: tuple[Field, ...] = ()
    ignorable: bool = False
    note: str = ""

    @property
    def required_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.fields if item.required)


def _turn(note: str = "Turn ordinal.") -> Field:
    return Field("turn", JSON_INT, required=True, note=note)
