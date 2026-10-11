"""The ``kind="dashboard"`` artifact: a layout package, and the rules that keep data out of it.

A dashboard artifact's content is not a document but a PACKAGE: a JSON object
holding ``bound_to`` (whose dashboard this is), ``model`` (the data types and
field shapes the page may render), ``view`` (the blocks that render them) and
``theme`` (tokens, optional CSS). Values never live here. They stay in the crew
log and reach the page through the fold / bus / controller path, so a package is
small, stable, and versioned only when the layout itself changes.

Three rules follow from that and are enforced here rather than at each caller:

* **One gate.** :func:`canonical_package_content` is the only way a package
  becomes stored content, and :meth:`kiro_crew.artifacts.ArtifactStore.create` /
  :meth:`~kiro_crew.artifacts.ArtifactStore.update` call it for every
  ``kind="dashboard"`` write. Both the dashboard tool path and a plain
  ``artifact_update`` funnel through the store, so neither can write a package
  the other would refuse.
* **Closed vocabularies.** A model field's ``type`` must be in
  :func:`data_type_catalog` and a view block's ``type`` in
  :func:`view_block_catalog`; every key inside either is named by its catalog
  entry and an unknown one is refused. Both catalogs are STUBS with a starter
  set -- the data line owns the first, the display line the second -- and
  replacing a stub moves the validator with it.
* **A version means a layout change.** :func:`layout_changed` compares the
  canonical ``model`` + ``view`` + ``theme`` of two packages, and the store
  snapshots a dashboard write only when that comparison says yes. ``bound_to``
  is deliberately outside the comparison: a rebind says WHERE the dashboard
  hangs, not what it looks like. :func:`revert_package` is the other half --
  reverting takes layout from the target version and keeps the LIVE binding, so
  a rollback can never hand a crewmate's page to a different crewmate.

Nothing here reads or writes the filesystem; the validators raise
:class:`~kiro_crew.artifact_store.model.ArtifactValidationError`, which every
artifact write path already renders as a 400 rather than a 500.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as _dc_field
from typing import TYPE_CHECKING, Any

from kiro_crew.artifact_store.model import ArtifactValidationError

if TYPE_CHECKING:  # pragma: no cover - a type-only import, so no cycle at run time
    from kiro_crew.artifacts import ArtifactStore

#: The artifact kind this module owns. Added to
#: :data:`kiro_crew.artifact_store.rules.ALLOWED_KINDS`, and deliberately NOT to
#: ``USER_SELECTABLE_KINDS``: a package is composed by an agent, and a hand-flip
#: of a prose document to this kind would store content no reader can parse.
DASHBOARD_KIND = "dashboard"

#: Hard cap on a stored package, well under the artifact content cap. A package
#: is layout: a figure this size is already a sign that values leaked into it.
MAX_PACKAGE_BYTES = 256 * 1024

MAX_MODEL_FIELDS = 64
MAX_VIEW_BLOCKS = 48
MAX_THEME_TOKENS = 64
MAX_THEME_CSS_BYTES = 32 * 1024
#: Cap on a human-readable string inside the package (a label, a title, a unit).
MAX_LABEL_LEN = 120

#: ``crewmate:<slug>`` or ``session:<slot key>`` -- one string, so the data line
#: passes a single value and two bindings can never be confused for each other.
#: A slot key carries letters, digits, ``.``, ``_`` and ``-`` (``chat-7-17910``,
#: ``slack_1785370133.085469``); the scope prefix here is this module's, not the
#: session key's own, so ``session:dashboard:chat-7`` is refused -- pass the
#: bare slot name the dashboard uses.
_BOUND_TO_RE = re.compile(r"\A(?:crewmate|session):[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
#: A model field name: an identifier the view and the fold both spell the same way.
_FIELD_NAME_RE = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")
#: A view block id: unique within the view, and the key the controller pushes to.
_BLOCK_ID_RE = re.compile(r"\A[a-z][a-z0-9_-]{0,63}\Z")
#: A theme token: a CSS custom property name, so the page can inject it as one.
_THEME_TOKEN_RE = re.compile(r"\A--[a-z][a-z0-9-]{0,63}\Z")
#: A theme token's value: printable, single-line, no CSS statement punctuation --
#: a token is a value, and ``;``/``{``/``}`` would let one close the declaration
#: it is injected into and open another.
_THEME_VALUE_RE = re.compile(r"\A[^\n\r;{}<>]{1,120}\Z")

#: Keys that mean VALUES. Refused anywhere in a package, with their own message:
#: strict key checking would refuse them as unknown, but the reason a reader
#: needs is the rule ("data stays in the crew log"), not "unknown key".
_DATA_KEYS = frozenset(
    {
        "data",
        "value",
        "values",
        "rows",
        "items",
        "series",
        "points",
        "samples",
        "readings",
        "records",
        "entries",
    }
)

#: A fold path: dotted keys walking the rendered fold value. The SAME grammar as
#: ``dashboard_templates.manifest._PATH``, because the translated manifest hands
#: this exact string to the same walker.
_PATH_RE = re.compile(r"\A[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*\Z")

#: The keys every model field carries, whatever its type.
_UNIVERSAL_FIELD_KEYS = ("type", "source", "label", "description")
#: The keys every view block carries, whatever its type.
_UNIVERSAL_BLOCK_KEYS = ("id", "type", "fields", "title")


# --------------------------------------------------------------------------- #
# Small value validators. Each raises ValueError with a plain reason; the
# caller that knows the path wraps it into an ArtifactValidationError.
# --------------------------------------------------------------------------- #


def _a_label(v: Any) -> str:
    if not isinstance(v, str):
        raise ValueError(f"must be a string, got {type(v).__name__}")
    if not v.strip():
        raise ValueError("must not be blank")
    if len(v) > MAX_LABEL_LEN:
        raise ValueError(f"must be at most {MAX_LABEL_LEN} characters (this one has {len(v)})")
    if "\n" in v or "\r" in v:
        raise ValueError("must be a single line")
    return v


def _a_count(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"must be a whole number, got {type(v).__name__}")
    if v < 0 or v > 10_000:
        raise ValueError("must be between 0 and 10000")
    return v


def _a_choice_list(v: Any) -> list[str]:
    if not isinstance(v, list) or not v:
        raise ValueError("must be a non-empty list of strings")
    if len(v) > 32:
        raise ValueError(f"must hold at most 32 choices (this one has {len(v)})")
    out: list[str] = []
    for choice in v:
        if not isinstance(choice, str) or not choice.strip():
            raise ValueError("every choice must be a non-blank string")
        if len(choice) > MAX_LABEL_LEN:
            raise ValueError(f"a choice is at most {MAX_LABEL_LEN} characters")
        if choice in out:
            raise ValueError(f"choice {choice!r} is listed twice")
        out.append(choice)
    return out


def _a_span(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"must be a whole number of columns, got {type(v).__name__}")
    if v < 1 or v > 12:
        raise ValueError("must be between 1 and 12 columns")
    return v


# --------------------------------------------------------------------------- #
# The two catalogs. STUBS: a starter set so the package line is testable end to
# end. Each is ONE function, so filling it in is a single edit and the validator
# follows -- see data/INTERFACE.md.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FieldType:
    """One admissible ``model.types[*].type`` and the keys its field may carry.

    ``required`` and ``optional`` map a key name to a validator that returns the
    stored value or raises ``ValueError`` saying why. A key in neither (and not
    in :data:`_UNIVERSAL_FIELD_KEYS`) is refused, which is what makes the
    catalog a closed vocabulary rather than a suggestion.
    """

    name: str
    summary: str
    required: Mapping[str, Callable[[Any], Any]] = _dc_field(default_factory=dict)
    optional: Mapping[str, Callable[[Any], Any]] = _dc_field(default_factory=dict)


@dataclass(frozen=True)
class BlockType:
    """One admissible ``view.blocks[*].type``: what it reads, and what it can draw.

    ``accepts`` is the set of :func:`data_type_catalog` type names this block has
    a rendering for, and it is a GATE, not documentation: ``_validate_view``
    refuses a block that names a field whose type is not in it. That is the one
    thing that keeps the catalog and
    :mod:`kiro_crew.dashboard_package_render` in step -- a block type with no
    renderer for a type it admits would render a blank cell with every gate
    green, and a renderer for a type the block refuses is dead code. Both
    directions are pinned by enumerating this table rather than a hand-written
    list, in ``test_dashboard_package_render.py``.

    ``requires`` names types the block cannot do without: a ``timeline`` with no
    ``timestamp`` among its fields has nothing to order by, so it is refused at
    write time rather than drawn as an unordered list.
    """

    name: str
    summary: str
    min_fields: int = 1
    max_fields: int = 16
    accepts: frozenset[str] = frozenset()
    requires: frozenset[str] = frozenset()
    optional: Mapping[str, Callable[[Any], Any]] = _dc_field(default_factory=dict)


#: STUB -- the data line (conductor chat-2611) owns this table.
_STUB_DATA_TYPES: tuple[FieldType, ...] = (
    FieldType(
        name="number",
        summary="a single numeric reading",
        optional={"unit": _a_label, "precision": _a_count},
    ),
    FieldType(
        name="text",
        summary="a short single-line string",
        optional={"max_len": _a_count},
    ),
    FieldType(name="timestamp", summary="an ISO-8601 instant"),
    FieldType(
        name="enum",
        summary="one of a fixed set of labels",
        required={"choices": _a_choice_list},
    ),
    FieldType(name="bool", summary="a yes/no flag"),
)

#: Every data type in the catalog above, DERIVED rather than listed again.
#:
#: A block whose ``accepts`` is this set draws whatever the model can declare, so
#: a type the data line adds is admitted into those blocks with no edit here --
#: and the renderer's enumerating test immediately names it as a type with no
#: rendering, which is the failure a hand-written copy of these five names would
#: have hidden.
_EVERY_DATA_TYPE: frozenset[str] = frozenset(t.name for t in _STUB_DATA_TYPES)

#: Keys every block may carry on top of :data:`_UNIVERSAL_BLOCK_KEYS`.
#:
#: ``span`` is the block's width in a 12-column grid; ``caption`` is one line
#: under it, for the sentence a number needs and a label has no room for.
_COMMON_BLOCK_KEYS: Mapping[str, Callable[[Any], Any]] = {
    "span": _a_span,
    "caption": _a_label,
}

#: The block types a view may place. Every entry has a renderer in
#: :mod:`kiro_crew.dashboard_package_render`, and every type in its ``accepts``
#: has a rendering inside that block.
#:
#: The shapes come from what the data model can actually hold. A model field is
#: ONE scalar -- a number, a line of text, an instant, one of a fixed set of
#: labels, or a flag -- so there is no series in a package and no block here
#: pretends there is. ``table`` therefore reads one row per field (its label and
#: its value) rather than one column per field, which over scalars would be a
#: table with exactly one row.
#:
#: Four blocks are narrower than "everything": ``bars``, ``gauge`` and ``orbit``
#: compare magnitudes and so take numbers only, and ``pills`` draws a state and
#: so takes the two types that ARE a state. Saying that in ``accepts`` is what
#: lets the write gate refuse a bar chart over a text field at compose time,
#: when the agent is still there to fix it, instead of drawing an empty bar.
_BLOCK_TYPES: tuple[BlockType, ...] = (
    BlockType(
        name="stat",
        summary="one field as the page's largest figure, with its label and caption",
        min_fields=1,
        max_fields=1,
        accepts=_EVERY_DATA_TYPE,
        optional=_COMMON_BLOCK_KEYS,
    ),
    BlockType(
        name="stat_band",
        summary="two to six fields as a divided band of figures, for the answers read at a glance",
        min_fields=2,
        max_fields=6,
        accepts=_EVERY_DATA_TYPE,
        optional=_COMMON_BLOCK_KEYS,
    ),
    BlockType(
        name="table",
        summary="one aligned row per field: its label, then its value in tabular figures",
        min_fields=1,
        max_fields=24,
        accepts=_EVERY_DATA_TYPE,
        optional=_COMMON_BLOCK_KEYS,
    ),
    BlockType(
        name="list",
        summary="fields stacked label-above-value, for text that needs room to read",
        min_fields=1,
        max_fields=16,
        accepts=_EVERY_DATA_TYPE,
        optional=_COMMON_BLOCK_KEYS,
    ),
    BlockType(
        name="note",
        summary="a callout: the title as the sentence, up to four values set inside it",
        min_fields=1,
        max_fields=4,
        accepts=_EVERY_DATA_TYPE,
        optional=_COMMON_BLOCK_KEYS,
    ),
    BlockType(
        name="bars",
        summary="numbers as horizontal bars, each a share of the largest",
        min_fields=1,
        max_fields=12,
        accepts=frozenset({"number"}),
        optional=_COMMON_BLOCK_KEYS,
    ),
    BlockType(
        name="gauge",
        summary="one number as a ring; a second number, when given, is the total it fills",
        min_fields=1,
        max_fields=2,
        accepts=frozenset({"number"}),
        optional=_COMMON_BLOCK_KEYS,
    ),
    BlockType(
        name="pills",
        summary="states as chips, each carrying a shape and a word so colour is never the only signal",
        min_fields=1,
        max_fields=16,
        accepts=frozenset({"enum", "bool"}),
        optional=_COMMON_BLOCK_KEYS,
    ),
    BlockType(
        name="timeline",
        summary="events in time order; needs at least one timestamp to order by",
        min_fields=1,
        max_fields=8,
        accepts=frozenset({"timestamp", "text", "enum"}),
        requires=frozenset({"timestamp"}),
        optional=_COMMON_BLOCK_KEYS,
    ),
    BlockType(
        name="orbit",
        summary="numbers as a turnable 3D ring, each field a body sized by its value, with the same numbers as text",
        min_fields=2,
        max_fields=12,
        accepts=frozenset({"number"}),
        optional=_COMMON_BLOCK_KEYS,
    ),
)


def data_type_catalog() -> Mapping[str, FieldType]:
    """The data types a ``model`` field may declare, by type name.

    THE one place the admissible types live: validation reads this and nothing
    else, so the data line fills the catalog and the write gate moves with it.
    The current table is a stub starter set (``number`` / ``text`` /
    ``timestamp`` / ``enum`` / ``bool``) -- see ``data/INTERFACE.md``.
    """
    return {t.name: t for t in _STUB_DATA_TYPES}


def view_block_catalog() -> Mapping[str, BlockType]:
    """The block types a ``view`` may place, by type name.

    Same contract as :func:`data_type_catalog`, for the other half of the
    package, and the same single point of truth: validation, the JSON Schema and
    the renderer's dispatch table all read THIS function.
    :mod:`kiro_crew.dashboard_package_render` is the other half -- a type here
    with no renderer, or a renderer with no type here, is a test failure rather
    than a surprise on somebody's dashboard.
    """
    return {b.name: b for b in _BLOCK_TYPES}


# --------------------------------------------------------------------------- #
# The JSON schema. A DOCUMENT for consumers (the skill, the tool description,
# a frontend type generator) built from the same catalogs the validator reads,
# so the two cannot drift -- test_dashboard_package pins that equality.
# --------------------------------------------------------------------------- #


def package_json_schema() -> dict[str, Any]:
    """The JSON Schema (draft 2020-12) of a dashboard package.

    Derived from the live catalogs rather than written out beside them: the
    enums here ARE :func:`data_type_catalog` and :func:`view_block_catalog`, so
    a type added to a catalog appears in the schema with no second edit. Callers
    use it to document and to pre-check; the authority is still
    :func:`validate_package`, which checks the cross-references a schema cannot
    (a block naming a field the model does not declare).
    """
    types = data_type_catalog()
    blocks = view_block_catalog()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://kirocrew.dev/schemas/artifact-dashboard-package.json",
        "title": "Dashboard artifact package",
        "description": (
            'The content of a kind="dashboard" artifact: layout only. Values are '
            "never stored here -- they stay in the crew log and reach the page "
            "through the fold / bus / controller path."
        ),
        "type": "object",
        "required": ["kind", "bound_to", "model", "view", "theme"],
        "additionalProperties": False,
        "properties": {
            "kind": {"const": DASHBOARD_KIND},
            "bound_to": {
                "type": "string",
                "pattern": _BOUND_TO_RE.pattern.replace("\\A", "^").replace("\\Z", "$"),
                "description": (
                    "crewmate:<slug> for a crewmate's page, session:<slot key> for a "
                    "root session slot's. Outside the layout fingerprint: a rebind is "
                    "not a layout change and does not create a version."
                ),
            },
            "model": {
                "type": "object",
                "required": ["types"],
                "additionalProperties": False,
                "properties": {
                    "types": {
                        "type": "object",
                        "description": "Field name -> field shape. The page may render these and nothing else.",
                        "maxProperties": MAX_MODEL_FIELDS,
                        "propertyNames": {
                            "pattern": _FIELD_NAME_RE.pattern.replace("\\A", "^").replace(
                                "\\Z", "$"
                            )
                        },
                        "additionalProperties": {
                            "type": "object",
                            "required": ["type", "source"],
                            "properties": {
                                "type": {
                                    "enum": sorted(types),
                                    "description": "A type name from data_type_catalog().",
                                },
                                "source": {
                                    "description": (
                                        "Where the value comes from. A fold source is read "
                                        "from the crew log and no agent can write it; an "
                                        "agentic source is one the crewmate writes itself."
                                    ),
                                    "oneOf": [
                                        {
                                            "type": "object",
                                            "required": ["fold", "path"],
                                            "additionalProperties": False,
                                            "properties": {
                                                "fold": {"enum": sorted(fold_names())},
                                                "path": {
                                                    "type": "string",
                                                    "pattern": _PATH_RE.pattern.replace(
                                                        "\\A", "^"
                                                    ).replace("\\Z", "$"),
                                                },
                                            },
                                        },
                                        {
                                            "type": "object",
                                            "required": ["agentic"],
                                            "additionalProperties": False,
                                            "properties": {"agentic": {"const": True}},
                                        },
                                    ],
                                },
                                "label": {"type": "string", "maxLength": MAX_LABEL_LEN},
                                "description": {"type": "string", "maxLength": MAX_LABEL_LEN},
                            },
                        },
                    }
                },
            },
            "view": {
                "type": "object",
                "required": ["blocks"],
                "additionalProperties": False,
                "properties": {
                    "blocks": {
                        "type": "array",
                        "maxItems": MAX_VIEW_BLOCKS,
                        "items": {
                            "type": "object",
                            "required": ["id", "type", "fields"],
                            "properties": {
                                "id": {
                                    "type": "string",
                                    "pattern": _BLOCK_ID_RE.pattern.replace("\\A", "^").replace(
                                        "\\Z", "$"
                                    ),
                                },
                                "type": {
                                    "enum": sorted(blocks),
                                    "description": "A block type from view_block_catalog().",
                                },
                                "fields": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": (
                                        "Model field names this block renders. Every name "
                                        "must be declared in model.types; this is also the "
                                        "block's subscription set for the bus."
                                    ),
                                },
                                "title": {"type": "string", "maxLength": MAX_LABEL_LEN},
                            },
                        },
                    }
                },
            },
            "theme": {
                "type": "object",
                "required": ["tokens"],
                "additionalProperties": False,
                "properties": {
                    "tokens": {
                        "type": "object",
                        "maxProperties": MAX_THEME_TOKENS,
                        "propertyNames": {
                            "pattern": _THEME_TOKEN_RE.pattern.replace("\\A", "^").replace(
                                "\\Z", "$"
                            )
                        },
                        "additionalProperties": {"type": "string", "maxLength": 120},
                    },
                    "css": {"type": "string", "maxLength": MAX_THEME_CSS_BYTES},
                },
            },
        },
    }


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def _refuse(path: str, reason: str) -> ArtifactValidationError:
    return ArtifactValidationError(f"dashboard package: {path}: {reason}")


#: An unpaired UTF-16 surrogate. ``"\ud800"`` is a well-formed JSON escape, so
#: the decoder keeps it as a lone code point that no UTF-8 encoder accepts.
_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def _refuse_lone_surrogates(raw: Any, path: str = "package") -> None:
    """Refuse a package holding an unpaired surrogate in ANY string, at any depth.

    The invariant this establishes is what makes every ``.encode("utf-8")``
    further down safe by construction: once parsing has rejected unpaired
    surrogates everywhere, no validator, no size check and no file write can
    meet one. Enforcing it per encode site instead means each site is its own
    chance to raise ``UnicodeEncodeError`` -- which no caller handles, so it
    surfaces as a 500 rather than the 400 a bad package deserves.

    The walk is iterative. A package's depth is bounded only AFTER validation,
    and this runs before it, so recursion here would add a second stack limit
    to the one :func:`parse_package` already handles.
    """
    stack: list[tuple[Any, str]] = [(raw, path)]
    while stack:
        node, where = stack.pop()
        if isinstance(node, str):
            found = _LONE_SURROGATE_RE.search(node)
            if found:
                raise _refuse(
                    where,
                    f"holds an unpaired surrogate code point ({found.group()!r} at "
                    f"index {found.start()}) that UTF-8 cannot encode; write a "
                    "paired character or drop it",
                )
        elif isinstance(node, Mapping):
            # Children are pushed in reverse so the LIFO visits them in document
            # order, which makes "the first one found" the same on every run.
            for key, value in reversed(list(node.items())):
                child = f"{where}.{key}" if isinstance(key, str) else where
                stack.append((value, child))
                if isinstance(key, str):
                    # A key is a string the serializer encodes too, and it is
                    # named separately because the path cannot point inside it.
                    stack.append((key, f"{where}: key {key!r}"))
        elif isinstance(node, (list, tuple)):
            for index, value in reversed(list(enumerate(node))):
                stack.append((value, f"{where}[{index}]"))


def _an_object(raw: Any, path: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _refuse(path, f"must be an object, got {type(raw).__name__}")
    for key in raw:
        if not isinstance(key, str):
            raise _refuse(path, f"keys must be strings, got {type(key).__name__}")
    _refuse_data_keys(raw, path)
    return raw


def _refuse_data_keys(obj: Mapping[str, Any], path: str) -> None:
    """Refuse a values-shaped key, naming the rule rather than 'unknown key'."""
    for key in obj:
        if key.lower() in _DATA_KEYS:
            raise _refuse(
                f"{path}.{key}",
                "a package holds layout only -- values stay in the crew log and reach "
                "the page through the fold / bus / controller path",
            )


def _check_keys(obj: Mapping[str, Any], path: str, allowed: Sequence[str]) -> None:
    unknown = sorted(k for k in obj if k not in allowed)
    if unknown:
        raise _refuse(
            path,
            f"unknown key(s) {unknown}: allowed here are {sorted(allowed)}",
        )


def _apply(
    spec: Mapping[str, Callable[[Any], Any]],
    obj: Mapping[str, Any],
    out: dict[str, Any],
    path: str,
    *,
    required: bool,
) -> None:
    for key, validator in spec.items():
        if key not in obj:
            if required:
                raise _refuse(f"{path}.{key}", "is required by this type")
            continue
        try:
            out[key] = validator(obj[key])
        except ValueError as exc:
            raise _refuse(f"{path}.{key}", str(exc)) from None


def fold_names() -> frozenset[str]:
    """The crew-log folds a field may read, as the template manifest defines them.

    Imported lazily and read through this one function rather than copied: a fold
    added to ``crew_log.projection`` has to be readable from a package without a
    second edit here, and the reader's translation into a ``TemplateManifest``
    hands the name straight back to the same consumer. The import is deferred
    because ``dashboard_templates.manifest`` reaches the projection and the
    template parity checker, and the artifact store should not carry that graph
    just to validate a string.
    """
    from kiro_crew.dashboard_templates.manifest import FOLD_NAMES

    return frozenset(FOLD_NAMES)


def _validate_source(raw: Any, path: str) -> dict[str, Any]:
    """Where a field's value comes from: a crew-log fold, or the agent itself.

    Exactly the two cases ``dashboard_templates.manifest`` already has, spelled
    the same way, because the reader translates this straight into a
    ``FieldSpec``. Without it a package would declare a field's shape and not
    say who fills it, and a translation could only guess -- and the only guess
    available, ``agentic``, would make every field on the page agent-writable
    and lose the fold-backed half entirely.
    """
    source = _an_object(raw, path)
    if source.get("agentic") is True:
        _check_keys(source, path, ("agentic",))
        return {"agentic": True}
    if "agentic" in source:
        # Not "unknown key": the author reached for the right key and wrote the
        # wrong value, and there is no third kind of source to fall back to.
        raise _refuse(
            f"{path}.agentic",
            f"is {source['agentic']!r}: an agentic source is written exactly "
            "{'agentic': true}, and a field the agent does not write declares "
            "{'fold': <name>, 'path': <dotted keys>} instead",
        )
    _check_keys(source, path, ("fold", "path"))
    fold = source.get("fold")
    known = fold_names()
    if not isinstance(fold, str) or fold not in known:
        raise _refuse(
            f"{path}.fold",
            f"{fold!r} is not a crew-log fold: the folds are {sorted(known)}. "
            "A field the agent writes itself declares {'agentic': true} instead",
        )
    walk = source.get("path")
    if not isinstance(walk, str) or _PATH_RE.fullmatch(walk) is None:
        raise _refuse(
            f"{path}.path",
            f"{walk!r} is required and must be dotted keys walking the fold value, "
            "e.g. 'summary.open_prs'",
        )
    return {"fold": fold, "path": walk}


def validate_bound_to(raw: Any) -> str:
    """Return a well-formed ``bound_to``, or raise saying why it is not one."""
    if not isinstance(raw, str):
        raise _refuse("bound_to", f"must be a string, got {type(raw).__name__}")
    if _BOUND_TO_RE.fullmatch(raw) is None:
        raise _refuse(
            "bound_to",
            f"{raw!r} is not a binding: write 'crewmate:<slug>' or 'session:<slot key>'",
        )
    return raw


def _validate_model(raw: Any) -> dict[str, Any]:
    model = _an_object(raw, "model")
    _check_keys(model, "model", ("types",))
    if "types" not in model:
        raise _refuse("model.types", "is required")
    raw_types = _an_object(model["types"], "model.types")
    if not raw_types:
        raise _refuse("model.types", "must declare at least one field")
    if len(raw_types) > MAX_MODEL_FIELDS:
        raise _refuse(
            "model.types",
            f"declares {len(raw_types)} fields; at most {MAX_MODEL_FIELDS} are allowed",
        )
    catalog = data_type_catalog()
    fields: dict[str, Any] = {}
    for name in sorted(raw_types):
        path = f"model.types.{name}"
        if _FIELD_NAME_RE.fullmatch(name) is None:
            raise _refuse(
                path,
                "is not a field name: lowercase letters, digits and '_', "
                "starting with a letter, at most 64 characters",
            )
        spec = _an_object(raw_types[name], path)
        type_name = spec.get("type")
        if not isinstance(type_name, str) or not type_name:
            raise _refuse(f"{path}.type", "is required and must be a type name")
        entry = catalog.get(type_name)
        if entry is None:
            raise _refuse(
                f"{path}.type",
                f"unknown data type {type_name!r}: the catalog holds {sorted(catalog)}",
            )
        allowed = (*_UNIVERSAL_FIELD_KEYS, *entry.required, *entry.optional)
        _check_keys(spec, path, allowed)
        if "source" not in spec:
            raise _refuse(
                f"{path}.source",
                "is required: a field must say where its value comes from -- "
                "{'fold': <name>, 'path': <dotted keys>} or {'agentic': true}",
            )
        out: dict[str, Any] = {
            "type": type_name,
            "source": _validate_source(spec["source"], f"{path}.source"),
        }
        for key in ("label", "description"):
            if key in spec:
                try:
                    out[key] = _a_label(spec[key])
                except ValueError as exc:
                    raise _refuse(f"{path}.{key}", str(exc)) from None
        _apply(entry.required, spec, out, path, required=True)
        _apply(entry.optional, spec, out, path, required=False)
        fields[name] = out
    return {"types": fields}


def _validate_view(raw: Any, declared_fields: Mapping[str, Any]) -> dict[str, Any]:
    view = _an_object(raw, "view")
    _check_keys(view, "view", ("blocks",))
    raw_blocks = view.get("blocks")
    if not isinstance(raw_blocks, list):
        raise _refuse("view.blocks", f"must be a list, got {type(raw_blocks).__name__}")
    if not raw_blocks:
        raise _refuse("view.blocks", "must place at least one block")
    if len(raw_blocks) > MAX_VIEW_BLOCKS:
        raise _refuse(
            "view.blocks",
            f"places {len(raw_blocks)} blocks; at most {MAX_VIEW_BLOCKS} are allowed",
        )
    catalog = view_block_catalog()
    blocks: list[dict[str, Any]] = []
    seen_ids: list[str] = []
    for index, raw_block in enumerate(raw_blocks):
        path = f"view.blocks[{index}]"
        block = _an_object(raw_block, path)
        block_id = block.get("id")
        if not isinstance(block_id, str) or _BLOCK_ID_RE.fullmatch(block_id) is None:
            raise _refuse(
                f"{path}.id",
                "is required and must be lowercase letters, digits, '_' or '-', "
                "starting with a letter",
            )
        if block_id in seen_ids:
            raise _refuse(f"{path}.id", f"{block_id!r} is used by an earlier block")
        seen_ids.append(block_id)
        type_name = block.get("type")
        if not isinstance(type_name, str) or not type_name:
            raise _refuse(f"{path}.type", "is required and must be a block type name")
        entry = catalog.get(type_name)
        if entry is None:
            raise _refuse(
                f"{path}.type",
                f"unknown block type {type_name!r}: the catalog holds {sorted(catalog)}",
            )
        _check_keys(block, path, (*_UNIVERSAL_BLOCK_KEYS, *entry.optional))
        raw_names = block.get("fields")
        if not isinstance(raw_names, list):
            raise _refuse(
                f"{path}.fields",
                f"is required and must be a list of model field names, got "
                f"{type(raw_names).__name__}",
            )
        names: list[str] = []
        for name in raw_names:
            if not isinstance(name, str):
                raise _refuse(
                    f"{path}.fields", f"every name must be a string, got {type(name).__name__}"
                )
            if name not in declared_fields:
                raise _refuse(
                    f"{path}.fields",
                    f"names {name!r}, which model.types does not declare "
                    f"(declared: {sorted(declared_fields)})",
                )
            if name in names:
                raise _refuse(f"{path}.fields", f"names {name!r} twice")
            names.append(name)
        if not (entry.min_fields <= len(names) <= entry.max_fields):
            raise _refuse(
                f"{path}.fields",
                f"a {type_name!r} block reads between {entry.min_fields} and "
                f"{entry.max_fields} fields (this one names {len(names)})",
            )
        # The block type says which data types it can DRAW, and a block that
        # names a field it cannot draw is refused here rather than rendered as a
        # blank cell. The renderer's dispatch table is enumerated against the
        # same ``accepts`` sets, so the two halves cannot disagree.
        placed = {name: declared_fields[name]["type"] for name in names}
        for name in names:
            if placed[name] not in entry.accepts:
                raise _refuse(
                    f"{path}.fields",
                    f"names {name!r}, a {placed[name]!r} field, and a {type_name!r} "
                    f"block draws {sorted(entry.accepts)}",
                )
        absent = sorted(entry.requires - set(placed.values()))
        if absent:
            raise _refuse(
                f"{path}.fields",
                f"a {type_name!r} block needs at least one field of type "
                f"{absent} among the fields it names",
            )
        out: dict[str, Any] = {"id": block_id, "type": type_name, "fields": names}
        if "title" in block:
            try:
                out["title"] = _a_label(block["title"])
            except ValueError as exc:
                raise _refuse(f"{path}.title", str(exc)) from None
        _apply(entry.optional, block, out, path, required=False)
        blocks.append(out)
    return {"blocks": blocks}


def _validate_theme(raw: Any) -> dict[str, Any]:
    theme = _an_object(raw, "theme")
    _check_keys(theme, "theme", ("tokens", "css"))
    raw_tokens = theme.get("tokens")
    if raw_tokens is None:
        raise _refuse("theme.tokens", "is required (an empty object is fine)")
    tokens_obj = _an_object(raw_tokens, "theme.tokens")
    if len(tokens_obj) > MAX_THEME_TOKENS:
        raise _refuse(
            "theme.tokens",
            f"holds {len(tokens_obj)} tokens; at most {MAX_THEME_TOKENS} are allowed",
        )
    tokens: dict[str, str] = {}
    for name in sorted(tokens_obj):
        path = f"theme.tokens.{name}"
        if _THEME_TOKEN_RE.fullmatch(name) is None:
            raise _refuse(
                path,
                "is not a theme token: a CSS custom property like '--panel-bg' "
                "(lowercase letters, digits and '-')",
            )
        value = tokens_obj[name]
        if not isinstance(value, str):
            raise _refuse(path, f"must be a string, got {type(value).__name__}")
        if _THEME_VALUE_RE.fullmatch(value) is None:
            raise _refuse(
                path,
                "must be a single printable line of at most 120 characters with no "
                "';', '{', '}', '<' or '>'",
            )
        tokens[name] = value
    out: dict[str, Any] = {"tokens": tokens}
    if "css" in theme:
        css = theme["css"]
        if not isinstance(css, str):
            raise _refuse("theme.css", f"must be a string, got {type(css).__name__}")
        if len(css.encode("utf-8")) > MAX_THEME_CSS_BYTES:
            raise _refuse("theme.css", f"must be at most {MAX_THEME_CSS_BYTES} bytes of UTF-8")
        # The page iframe runs under CSP ``default-src 'none'`` with no network,
        # so a fetching construct cannot load -- it would fail silently and read
        # as a style that does not apply. Refusing it here says why instead.
        lowered = css.lower()
        for construct in ("@import", "url(", "<script", "</style", "javascript:"):
            if construct in lowered:
                raise _refuse(
                    "theme.css",
                    f"must not contain {construct!r}: the page iframe has no network "
                    "and takes no agent-authored code",
                )
        out["css"] = css
    return out


def validate_package(raw: Any) -> dict[str, Any]:
    """Return the CANONICAL form of a dashboard package, or raise saying why not.

    Canonical means: the five top-level keys in a fixed order, model fields and
    theme tokens in sorted key order, each field and block holding only the keys
    its catalog entry names, and view blocks in the order the author placed them
    (that order is the layout). Two packages that mean the same thing therefore
    serialize to the same bytes, which is what makes
    :func:`layout_changed` able to answer by comparison.
    """
    package = _an_object(raw, "package")
    _check_keys(package, "package", ("kind", "bound_to", "model", "view", "theme"))
    kind = package.get("kind")
    if kind != DASHBOARD_KIND:
        raise _refuse("kind", f"must be {DASHBOARD_KIND!r}, got {kind!r}")
    bound_to = validate_bound_to(package.get("bound_to"))
    if "model" not in package:
        raise _refuse("model", "is required")
    if "view" not in package:
        raise _refuse("view", "is required")
    if "theme" not in package:
        raise _refuse("theme", "is required (an empty 'tokens' object is fine)")
    model = _validate_model(package["model"])
    view = _validate_view(package["view"], model["types"])
    theme = _validate_theme(package["theme"])
    return {
        "kind": DASHBOARD_KIND,
        "bound_to": bound_to,
        "model": model,
        "view": view,
        "theme": theme,
    }


def _decode_package(content: str) -> Any:
    """Stored dashboard content as plain decoded JSON, or raise.

    The half of :func:`parse_package` that runs BEFORE any validator: the empty
    check, the surrogate scan, the byte cap, the decode and its two failure
    modes. It is split out because one caller needs the envelope and must not
    need the layout inside it to be valid -- see :func:`resolve_bound_slug`.

    Everything here guards the decode itself rather than the package's shape,
    so a caller that skips validation still cannot be made to exhaust the
    stack, encode an unpaired surrogate or read an unbounded string.
    """
    if not isinstance(content, str) or not content.strip():
        raise _refuse("package", "is empty: a dashboard artifact stores a JSON package")
    # The submitted text is scanned before it is MEASURED, because measuring it
    # encodes it. A surrogate reaches a package two ways and each is caught at
    # the point it becomes a code point: written literally by a Python caller it
    # is already one here, and written as the escape ``\\ud800`` it is six ASCII
    # characters until the decoder below turns it into one.
    _refuse_lone_surrogates(content)
    if len(content.encode("utf-8")) > MAX_PACKAGE_BYTES:
        raise _refuse(
            "package",
            f"is larger than {MAX_PACKAGE_BYTES} bytes, which a layout is not -- "
            "check whether values leaked into it",
        )
    try:
        raw = json.loads(content)
    except ValueError as exc:
        raise _refuse("package", f"is not valid JSON: {exc}") from None
    except RecursionError:
        # The byte cap above bounds a package's SIZE, not its DEPTH, and the two
        # come apart: a quarter-megabyte of ``[`` is small enough to pass the cap
        # and deep enough that the decoder runs out of stack. ``RecursionError``
        # is a ``RuntimeError``, so the ``ValueError`` arm does not see it, and
        # letting it escape turns a refusable package into a 500 at every caller
        # that answers an ``ArtifactValidationError`` with a 400.
        raise _refuse(
            "package",
            "nests deeper than the JSON decoder can follow: a layout is a few "
            "levels of objects and arrays, so this is not one",
        ) from None
    # Before any validator touches a string: a validator that measures a string
    # in bytes, and the serializer that writes one, both encode, and an encode
    # that meets an unpaired surrogate raises where nothing catches it.
    _refuse_lone_surrogates(raw)
    return raw


def parse_package(content: str) -> dict[str, Any]:
    """Parse stored dashboard content into a canonical package, or raise."""
    return validate_package(_decode_package(content))


def dump_package(package: Mapping[str, Any]) -> str:
    """Serialize an already-canonical package to its stored bytes, or raise.

    The package cap lives HERE rather than in the callers, because this is the
    only place that holds the bytes the store actually writes, and
    :func:`canonical_package_content` and :func:`revert_package` both end in it.
    A cap measured anywhere upstream is measured on a different string: the
    stored form is indented, which costs roughly 12% on a package of nested
    objects, so text that arrives under :data:`MAX_PACKAGE_BYTES` can leave it
    over -- and an oversized package is one its own reader refuses, which shows
    up as a page that silently loses its layout.

    The encode below cannot raise: every string in a package has passed
    :func:`_refuse_lone_surrogates`, which is the one place that invariant is
    enforced.
    """
    text = json.dumps(package, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    written = text.encode("utf-8")
    if len(written) > MAX_PACKAGE_BYTES:
        raise _refuse(
            "package",
            f"is {len(written)} bytes once stored, over the {MAX_PACKAGE_BYTES}-byte "
            "limit. The stored form is indented, so the limit applies to that form "
            "and not to the text submitted -- shrink the layout, or check whether "
            "values leaked into it",
        )
    return text


def canonical_package_content(content: str) -> str:
    """THE write gate: validated, canonical bytes for a ``kind="dashboard"`` artifact.

    Every dashboard write goes through here -- the store calls it from
    ``create`` and from ``update``, which is what the dashboard tool path and a
    plain ``artifact_update`` both reach. Storing the canonical bytes rather
    than the caller's keeps the stored form one thing: the layout comparison
    and the version history then read a package the author's whitespace cannot
    perturb.
    """
    return dump_package(parse_package(content))


# --------------------------------------------------------------------------- #
# Versioning: a version means a layout change
# --------------------------------------------------------------------------- #


def _layout_fingerprint(package: Mapping[str, Any]) -> str:
    """sha256 over the canonical ``model`` + ``view`` + ``theme`` of a package.

    ``bound_to`` is NOT in it, on purpose: a rebind moves the dashboard, it does
    not restyle it, and a version exists to let someone go back to a layout.
    Values are not in it because they were never in the package.
    """
    layout = {
        "model": package.get("model"),
        "view": package.get("view"),
        "theme": package.get("theme"),
    }
    blob = json.dumps(layout, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def layout_changed(stored_content: str, new_content: str) -> bool:
    """True when the new package's layout differs from the stored one's.

    The store asks this on every dashboard content write and snapshots a new
    version only when the answer is yes. Unparseable stored content -- the
    first write, a record from before this kind existed -- counts as changed,
    so the first package always lands with a version behind it.
    """
    try:
        stored = parse_package(stored_content)
    except ArtifactValidationError:
        return True
    return _layout_fingerprint(stored) != _layout_fingerprint(parse_package(new_content))


def revert_package(stored_content: str, target_content: str) -> str:
    """Canonical bytes for a revert: layout from the target version, binding from live.

    "Revert restores layout only" has two halves. Values are the easy half:
    they were never in the package, so a rollback cannot touch them. The
    binding is the half that needs saying -- ``bound_to`` lives in the package,
    and a version from before a rebind carries the OLD binding, so a plain
    content restore would quietly hand this crewmate's page to whoever that
    stale binding names. The live binding therefore wins, and only
    ``model`` / ``view`` / ``theme`` come back from the target.

    Unparseable live content is REFUSED rather than fallen back on. The only
    binding available then is the target version's, and adopting it is the very
    move this function exists to prevent -- a silent rebind to whoever a stale
    copy named, arrived at through an ordinary revert. A corrupt live package is
    recoverable by writing one explicitly, which states the binding rather than
    inheriting it.
    """
    target = parse_package(target_content)
    try:
        live = parse_package(stored_content)
    except ArtifactValidationError:
        raise _refuse(
            "package",
            "cannot be reverted while the live package does not parse: the binding "
            "would have to come from the target version, which is a silent rebind -- "
            "write the package you want instead, so it states its own bound_to",
        ) from None
    target["bound_to"] = live["bound_to"]
    return dump_package(target)


#: Cap on how many dashboard artifacts :func:`resolve_bound_slug` will open while
#: looking for a binding. There is one package per crewmate or slot, so a scan past
#: this is a library problem, not a lookup that deserves more reads.
MAX_BINDING_SCAN = 256


class BindingLookupIncomplete(ArtifactValidationError):
    """The binding scan could not see every stored package, so "absent" is unproven.

    Raised instead of answering ``None``, because the two are different facts and a
    caller that cannot tell them apart acts on the wrong one: "this member has no
    package" sends the write path to a DIFFERENT Model, and a cell declared ``text``
    by the member's real package can then be replaced by an array.
    """


@dataclass(frozen=True)
class DashboardModel:
    """The Model of the package currently stored for one ``bound_to``.

    What the data line needs in order to decide whether a value it is about to
    write has a home, and which blocks should hear about it:

    * ``fields`` -- field name -> its canonical shape (``type`` plus that
      type's own keys). A value whose field name is absent here has no home on
      this page and must not be written.
    * ``subscriptions`` -- block id -> the field names that block renders, which
      is what a first load names its blocks from. Narrowing a PUSH to the blocks
      one fold moved is the renderer's own
      ``dashboard_package_render.blocks_reading``, not a second lookup here.
    * ``layout_fingerprint`` -- the layout this Model came from, so a caller
      holding one can tell whether the page has been recomposed under it
      without re-reading the whole package.
    """

    slug: str
    version: int
    bound_to: str
    layout_fingerprint: str
    fields: Mapping[str, Mapping[str, Any]]
    subscriptions: Mapping[str, tuple[str, ...]]

    def agentic_fields(self) -> Mapping[str, Mapping[str, Any]]:
        """The fields the crewmate may write itself, by name.

        A fold-backed field is NOT here: its value is read from the crew log and
        no agent can write it. The same split
        ``dashboard_agentic.agentic_fields`` makes over a template manifest.
        """
        return {
            name: spec
            for name, spec in self.fields.items()
            if spec.get("source", {}).get("agentic") is True
        }

    @property
    def folds(self) -> frozenset[str]:
        """The crew-log folds this page subscribes to."""
        return frozenset(
            fold
            for spec in self.fields.values()
            if (fold := spec.get("source", {}).get("fold")) is not None
        )


def model_of(package: Mapping[str, Any], *, slug: str = "", version: int = 0) -> DashboardModel:
    """Project a canonical package into the :class:`DashboardModel` view of it."""
    fields = dict(package["model"]["types"])
    subscriptions = {block["id"]: tuple(block["fields"]) for block in package["view"]["blocks"]}
    return DashboardModel(
        slug=slug,
        version=version,
        bound_to=package["bound_to"],
        layout_fingerprint=_layout_fingerprint(package),
        fields=fields,
        subscriptions=subscriptions,
    )


def resolve_bound_slug(bound_to: str, *, store: "ArtifactStore | None" = None) -> str | None:
    """The slug of the dashboard artifact bound to ``bound_to``, or ``None``.

    The binding half of the lookup on its own, for a caller that is keyed by a member
    but needs the artifact. Reads the ``kind="dashboard"`` artifacts newest-first,
    capped at :data:`MAX_BINDING_SCAN`; there is one package per crewmate or slot, so
    this ends on its first or second read in practice.

    ``None`` means CONFIRMED absent: every stored dashboard artifact was opened, every
    envelope was readable, and none of them names this binding. Anything that leaves
    that unproven raises :class:`BindingLookupIncomplete` instead -- a scan the cap cut
    short, a record this process could not read, an envelope it could not decode, or a
    record the listing never showed because its METADATA would not read. That last one
    does not appear in the scan at all: ``ArtifactStore.list`` drops it with a warning,
    and a dropped record's ``kind`` is precisely what could not be read, so it may be
    the dashboard bound here.
    None of the three may answer ``None``, because a caller reads ``None`` as "this
    member has no package": the write path then validates against a DIFFERENT Model and
    a cell declared ``text`` by the member's real package is replaced by an array.

    The binding is matched off the stored ENVELOPE, not off a validated package, so a
    layout the current validator rejects -- an older schema, or a write that did not
    finish -- is still FOUND and reported broken by the caller that reads its content.

    A match returns as soon as it is seen, so an unreadable record never hides a
    package that was found after it.
    """
    bound_to = validate_bound_to(bound_to)
    if store is None:
        from kiro_crew.artifacts import get_default_store

        store = get_default_store()
    from kiro_crew.artifacts import ArtifactError

    records = store.list(kind=DASHBOARD_KIND)
    unread = 0
    for art in records[:MAX_BINDING_SCAN]:
        try:
            loaded = store.get(art.slug)
            raw = _decode_package(loaded.content or "")
        except (ArtifactError, OSError):
            # Counted rather than skipped. A record that cannot be opened or decoded
            # may BE the one bound here -- that is precisely what could not be read.
            unread += 1
            continue
        # The query is validated, so an equal string is a well-formed binding: a
        # record whose own ``bound_to`` is malformed cannot match one and needs no
        # separate check here.
        if isinstance(raw, Mapping) and raw.get("bound_to") == bound_to:
            return loaded.slug
    if len(records) > MAX_BINDING_SCAN:
        raise BindingLookupIncomplete(
            f"dashboard package: binding: {len(records)} dashboard artifacts are stored "
            f"and this lookup opens the newest {MAX_BINDING_SCAN}, so a package bound to "
            f"{bound_to!r} behind them was not seen. There is one package per crewmate "
            "or slot, so this many is a library to prune, not a lookup to widen"
        )
    if unread:
        raise BindingLookupIncomplete(
            f"dashboard package: binding: {unread} of {len(records)} dashboard "
            f"artifacts could not be read, so nothing proves none of them is bound to "
            f"{bound_to!r}"
        )
    # Asked only once the scan has found nothing, because a MATCH needs no inventory:
    # the record that answers is in hand either way. A skipped record is invisible to
    # the loop above, so this is the one check the loop cannot make for itself.
    skipped = store.unreadable_record_count()
    if skipped:
        raise BindingLookupIncomplete(
            f"dashboard package: binding: {skipped} stored record(s) have metadata that "
            f"will not read, so the listing never showed them and their kind is unknown; "
            f"nothing proves none of them is bound to {bound_to!r}"
        )
    return None
