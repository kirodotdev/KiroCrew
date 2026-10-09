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

:func:`read_dashboard_model` is the one function the data line calls: given a
``bound_to``, it returns the Model of the package currently stored for it.
Nothing here reads or writes the filesystem except through the store it is
handed; the validators raise
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

if TYPE_CHECKING:  # pragma: no cover -- import cycle: artifacts imports this module
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
#: Cap on how many dashboard artifacts :func:`read_dashboard_model` will open
#: while looking for a binding. There is one package per crewmate or slot, so a
#: scan past this is a library problem, not a lookup that deserves more reads.
MAX_BINDING_SCAN = 256

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

#: The keys every model field carries, whatever its type.
_UNIVERSAL_FIELD_KEYS = ("type", "label", "description")
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
    """One admissible ``view.blocks[*].type``, and how many model fields it reads."""

    name: str
    summary: str
    min_fields: int = 1
    max_fields: int = 16
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

#: STUB -- the display line owns this table.
_STUB_BLOCK_TYPES: tuple[BlockType, ...] = (
    BlockType(
        name="stat",
        summary="one field as a large number",
        min_fields=1,
        max_fields=1,
        optional={"span": _a_span},
    ),
    BlockType(
        name="table",
        summary="fields as columns of a table",
        optional={"span": _a_span},
    ),
    BlockType(
        name="list",
        summary="fields as rows of a plain list",
        optional={"span": _a_span},
    ),
    BlockType(
        name="timeline",
        summary="fields ordered by a timestamp",
        max_fields=8,
        optional={"span": _a_span},
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
    package: a stub starter set the display line replaces.
    """
    return {b.name: b for b in _STUB_BLOCK_TYPES}


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
                            "required": ["type"],
                            "properties": {
                                "type": {
                                    "enum": sorted(types),
                                    "description": "A type name from data_type_catalog().",
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
        out: dict[str, Any] = {"type": type_name}
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


def parse_package(content: str) -> dict[str, Any]:
    """Parse stored dashboard content into a canonical package, or raise."""
    if not isinstance(content, str) or not content.strip():
        raise _refuse("package", "is empty: a dashboard artifact stores a JSON package")
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
    return validate_package(raw)


def dump_package(package: Mapping[str, Any]) -> str:
    """Serialize an already-canonical package to its stored bytes."""
    return json.dumps(package, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


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


def layout_fingerprint(package: Mapping[str, Any]) -> str:
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
    return layout_fingerprint(stored) != layout_fingerprint(parse_package(new_content))


def revert_package(stored_content: str, target_content: str) -> str:
    """Canonical bytes for a revert: layout from the target version, binding from live.

    "Revert restores layout only" has two halves. Values are the easy half:
    they were never in the package, so a rollback cannot touch them. The
    binding is the half that needs saying -- ``bound_to`` lives in the package,
    and a version from before a rebind carries the OLD binding, so a plain
    content restore would quietly hand this crewmate's page to whoever the
    dashboard used to belong to. The live binding therefore wins, and only
    ``model`` / ``view`` / ``theme`` come back from the target.

    Unparseable live content (nothing valid stored yet) falls back to the
    target's own binding: there is no live binding to preserve.
    """
    target = parse_package(target_content)
    try:
        live = parse_package(stored_content)
    except ArtifactValidationError:
        return dump_package(target)
    target["bound_to"] = live["bound_to"]
    return dump_package(target)


# --------------------------------------------------------------------------- #
# The function the data line calls
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DashboardModel:
    """The Model of the package currently stored for one ``bound_to``.

    What the data line needs in order to decide whether a value it is about to
    write has a home, and which blocks should hear about it:

    * ``fields`` -- field name -> its canonical shape (``type`` plus that
      type's own keys). A value whose field name is absent here has no home on
      this page and must not be written.
    * ``subscriptions`` -- block id -> the field names that block renders. The
      bus pushes a fold only to the blocks that subscribe to a field it moved.
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

    def declares(self, field_name: str) -> bool:
        """True when this page has a home for ``field_name``."""
        return field_name in self.fields

    def subscribers(self, field_name: str) -> tuple[str, ...]:
        """The block ids that render ``field_name``, in view order."""
        return tuple(
            block_id for block_id, names in self.subscriptions.items() if field_name in names
        )


def model_of(package: Mapping[str, Any], *, slug: str = "", version: int = 0) -> DashboardModel:
    """Project a canonical package into the :class:`DashboardModel` view of it."""
    fields = dict(package["model"]["types"])
    subscriptions = {block["id"]: tuple(block["fields"]) for block in package["view"]["blocks"]}
    return DashboardModel(
        slug=slug,
        version=version,
        bound_to=package["bound_to"],
        layout_fingerprint=layout_fingerprint(package),
        fields=fields,
        subscriptions=subscriptions,
    )


def read_dashboard_model(
    bound_to: str, *, store: "ArtifactStore | None" = None
) -> DashboardModel | None:
    """The Model of the dashboard package bound to ``bound_to``, or ``None``.

    ``None`` is the empty state and is not an error: v3 has no default page, so
    nothing is stored until the agent composes a layout. A caller writing a
    value treats ``None`` as "this page cannot hold anything yet".

    ``bound_to`` is ``crewmate:<slug>`` or ``session:<slot key>``; a value that
    is not a binding raises rather than quietly matching nothing. ``store``
    defaults to the process-wide artifact store.

    The binding is resolved by reading the ``kind="dashboard"`` artifacts
    newest-first and returning the first whose package is bound here, capped at
    :data:`MAX_BINDING_SCAN` records. There is one package per crewmate or
    slot, so the scan ends on its first or second read in practice; a record
    whose content no longer parses is skipped rather than raising, because one
    corrupt package must not make every other page unreadable.
    """
    bound_to = validate_bound_to(bound_to)
    if store is None:
        from kiro_crew.artifacts import get_default_store

        store = get_default_store()
    from kiro_crew.artifacts import ArtifactError

    for art in store.list(kind=DASHBOARD_KIND)[:MAX_BINDING_SCAN]:
        try:
            loaded = store.get(art.slug)
            package = parse_package(loaded.content or "")
        except (ArtifactError, OSError):
            continue
        if package["bound_to"] == bound_to:
            return model_of(package, slug=loaded.slug, version=loaded.version)
    return None
