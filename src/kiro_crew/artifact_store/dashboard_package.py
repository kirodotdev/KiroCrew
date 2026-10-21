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

#: Cap on an agent-written ``view.html`` page, in bytes of UTF-8.
#:
#: The FRAME's own ceiling, :data:`kiro_crew.dashboard_frame.MAX_PAGE_BYTES`, so a page
#: a package can hold is always a page the frame will draw. Mirrored rather than
#: imported, because this module is on the artifact write path and must stay cheap to
#: import; ``test_the_page_cap_is_the_frames_own`` pins the two equal.
#:
#: The cap bounds what a page COSTS to store, send and draw. It does not detect values
#: baked into the layout: a page that inlines a client-side library is several MB with
#: no data in it, so size cannot tell the two apart. The structural rules are what keep
#: values out: every field names its source, every binding must be a declared field,
#: the controller pushes only declared fields, and value-shaped keys are refused.
MAX_VIEW_HTML_BYTES = 3 * 1024 * 1024

#: Hard cap on a stored package, in bytes of its STORED form, well under the artifact
#: content cap.
#:
#: Sized so a page at :data:`MAX_VIEW_HTML_BYTES` fits once escaped. The stored form is
#: JSON, which writes a quote, a backslash or a line break in the page as two bytes, so
#: the page can take up to twice its own size there (other control characters escape
#: longer, and HTML has no use for them). The 128 KiB on top is the room for the rest
#: of the package -- model, theme and structure -- as a page had beside it before. The
#: whole package is still measured against this one number, so a model and theme near
#: their own caps can need more, and then the refusal below names the limit. This cap
#: also bounds the decode, which runs before any validator. The reader applies the same
#: number to a stored package, so the cap can be raised but not lowered while packages
#: written under a larger one are still stored: those would stop loading.
MAX_PACKAGE_BYTES = 2 * MAX_VIEW_HTML_BYTES + 128 * 1024

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
    # The two a PAGE needs and a host-drawn block never did. A fold's value is often a
    # list of rows or a nested record -- `worktree.boards`, `work.items` -- and the
    # catalog without them can express a count of a tree but not the tree, so the first
    # page that draws one has no field to bind. Admitted with no shape keys: the write
    # path does not check a fold's own structure, and a shape declared here would be a
    # promise about bytes nothing validates.
    FieldType(name="array", summary="a list of rows, walked by the page's own script"),
    FieldType(name="object", summary="a nested record, walked by the page's own script"),
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
# Validation
# --------------------------------------------------------------------------- #


def _refuse(path: str, reason: str) -> ArtifactValidationError:
    return ArtifactValidationError(f"dashboard package: {path}: {reason}")


def _refuse_credential_shaped_name(name: str) -> None:
    """Refuse a field name the credential redactor would rewrite.

    A field NAME travels further than its value. Values are scrubbed on the way
    out; the names are served in the package's ``fields`` list and written into
    the page's data island, so a name shaped like a token is published in clear
    by the one surface that redacts everything else. :data:`_FIELD_NAME_RE` does
    not stop that -- a lower-case fine-grained-PAT shape fits inside
    ``[a-z][a-z0-9_]{0,63}`` with room to spare.

    The gate is the REDACTOR ITSELF rather than a second copy of its patterns:
    the name goes through the same shim every egress site uses, and a name that
    comes back changed is refused. So a loaded companion's extra credential and
    cookie patterns apply here too, and a pattern added there cannot leave this
    check behind.

    Refused at VALIDATION, which is upstream of every reader: the serving path,
    the data island and the field push all take the package as given, so one
    refusal here covers all of them without each having to remember.

    The refusal names the field by its REDACTED spelling. An error string is
    logged, and echoing back the thing just identified as credential-shaped
    would put it exactly where this check exists to keep it out of.
    """
    from kiro_crew.platform.context import redact_via_context

    redacted = redact_via_context(name)
    if redacted == name:
        return
    raise _refuse(
        f"model.types.{redacted}",
        "is shaped like a credential, so it is refused as a field NAME. Field "
        "names are served in the package's field list and written into the "
        "page's data island, where only values are redacted -- rename the field",
    )


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
    """The crew-log folds a PACKAGE field may read.

    Imported lazily and read through this one function rather than copied: a fold
    added to ``crew_log.projection`` has to be readable from a package without a
    second edit here, and the reader's translation into a ``TemplateManifest``
    hands the name straight back to the same consumer. The import is deferred
    because ``dashboard_templates.manifest`` reaches the projection and the
    template parity checker, and the artifact store should not carry that graph
    just to validate a string.

    ``PACKAGE_FOLD_NAMES`` rather than the template set, because the two surfaces
    have different readers and this gate is about reachability: a package's page is
    drawn by ``member_dashboard_fields.LivePage``, which READS a tree-keyed fold, and
    a template is rendered by a feed that reaches a fold only by its bus scope.
    """
    from kiro_crew.dashboard_templates.manifest import PACKAGE_FOLD_NAMES

    return frozenset(PACKAGE_FOLD_NAMES)


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


def binding_refusal(bound_to: Any, *, crew: str = "", slot: str = "") -> str:
    """``""`` when this caller owns the dashboard *bound_to* names; else why it does not.

    THE OWNER GATE, as a comparison with no request and no store read, so every write
    path can ask it and a test can enumerate the answers. The caller's identity is
    resolved by whoever holds the request -- the crew comes from the session's own
    binding and never from a body -- and only the comparison is here. A crewmate
    binding resolves the caller's slug through the roster, because the binding stores a
    slug and a name is not one.

    A refusal is RETURNED rather than raised, and it is a sentence rather than a code,
    because the party that reads it is a worker agent that can act on it: the useful
    thing to say is whose dashboard this is and what to do instead, and an exception
    would be rendered as a 500 by every caller that answers a validation error with a
    400.

    Both binding scopes are answered, and a caller that can prove neither identity is
    refused rather than allowed: a gate whose default is "no identity, no objection"
    is not a gate.
    """
    # EVERY NAME IN THIS SENTENCE IS REDACTED FIRST, because the sentence is returned
    # to a caller and served as an HTTP body.
    #
    # `_BOUND_TO_RE` accepts a name shaped like a GitLab PAT or a Slack bot token, so a
    # package whose binding is such a string has that string put back into the refusal
    # and sent out. Done HERE, in the one function that composes these messages, rather
    # than at each route that returns one: the write gate, the named GET and the bound
    # GET all read this string, and a guard added per call site is how the second and
    # third of them would have been missed.
    from kiro_crew.platform.context import redact_via_context

    def safe(value: str) -> str:
        return redact_via_context(value) if value else value

    try:
        binding = validate_bound_to(bound_to)
    except ArtifactValidationError as exc:
        # The validation error quotes the offending value, so it is redacted too: a
        # binding can fail validation for a reason that has nothing to do with its
        # shape -- a missing scope -- while still being credential-shaped.
        return safe(str(exc))
    scope, _, name = binding.partition(":")
    if scope == "session":
        if slot and slot == name:
            return ""
        return (
            f"this dashboard belongs to the session {safe(name)!r} and you are writing "
            f"from {safe(slot) or 'no session'}. Report your work with work_report; the "
            "fold carries it to the page."
        )
    # A crewmate binding. The slug is compared, not the display name, because the
    # binding stores a slug and slugification is lossy -- comparing names would let
    # two configured names that differ only in case each pass for the other's page.
    #
    if crew:
        from kiro_crew import members as members_mod

        try:
            # EXACTLY ONE CREWMATE MAY OWN THIS SLUG, asked before the comparison below
            # can pick between two of them.
            #
            # Slugification is lossy, so a hand-edited roster holding both `Atlas` and
            # `atlas` derives one slug from two names -- and comparing the caller's slug
            # to the binding's then authorizes EITHER of them for the other's page.
            #
            # `_member_names_for_slug` is the roster's own answer to this question, the
            # one `_crewmate_binding_refusal` and `member_dashboard._resolve` already
            # ask, so the count and the refusal here are not a fourth implementation of
            # the rule. Ambiguity REFUSES and never picks: the remedy is a person
            # renaming one of them, which is what the serve-time refusal already says.
            from kiro_crew.config.loader import KiroCrewConfig
            from kiro_crew.dashboard.handlers.members import _member_names_for_slug

            cfg = KiroCrewConfig.load()
            owners = _member_names_for_slug(cfg, name)
            if len(owners) > 1:
                return (
                    f"the dashboard binding {safe(name)!r} is a slug more than one "
                    "crewmate resolves to, so no writer can be shown to own it and a "
                    "read of it is refused 'dashboard_slug_ambiguous'. Nothing was "
                    "written: ask the person to rename one of them."
                )
            if owners and members_mod.member_slug(crew, cfg) == name:
                return ""
        except Exception:
            # An unslugifiable caller, or a roster this process could not read, cannot
            # be shown to own anything. That is the same answer as a mismatch and is
            # reported as one -- a read that failed authorizes nothing.
            pass
    return (
        f"this dashboard belongs to {safe(name)!r} and you are "
        f"{safe(crew) or 'not a crew member'}. A dashboard is written by the crewmate "
        "it is bound to; report your work with work_report instead, and the fold "
        "carries it to the page."
    )


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
        _refuse_credential_shaped_name(name)
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


#: A text binding in an agent-written page, in either quote an author may have typed.
#:
#: MIRRORS :data:`kiro_crew.dashboard_frame.BINDING_ATTRIBUTE`, pinned equal by test.
#: Matched with a regex rather than parsed, because this check runs on the artifact
#: write path and pulling in the html parser there would put it behind every caller of
#: :mod:`kiro_crew.artifacts`. The cost is a page that MENTIONS the attribute in its own
#: prose being read as binding it -- a refusal the author can act on, where the parse's
#: own cost would be paid by every artifact write in the product.
_HTML_BINDING_RE = re.compile(
    r"data-dashboard-field\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'>=`]+))"
)


def _validate_view_html(raw: Any, declared_fields: Mapping[str, Any]) -> str:
    """One agent-written page, or raise saying why it is not one.

    What is NOT checked here is the point of the shape: the markup is free, the script
    is free, and nothing in this function looks for a dangerous construct. Containment
    is :mod:`kiro_crew.dashboard_frame` -- ``default-src 'none'``, ``connect-src
    'none'``, no dynamic-exec primitive, ``allow-scripts`` and nothing else over an
    opaque origin -- and a page cannot loosen a policy by being written differently. A
    denylist here would be a second, weaker gate that an author would read as the real
    one.

    What IS checked is the author's own mistake, while the author is still present.
    """
    if not isinstance(raw, str):
        raise _refuse("view.html", f"must be a string of HTML, got {type(raw).__name__}")
    if not raw.strip():
        raise _refuse(
            "view.html",
            "is empty: write the page's inline HTML, binding each field with "
            "data-dashboard-field or reading it from window.kirocrew.subscribe",
        )
    size = len(raw.encode("utf-8"))
    if size > MAX_VIEW_HTML_BYTES:
        raise _refuse(
            "view.html",
            f"is {size} bytes, over the {MAX_VIEW_HTML_BYTES}-byte limit for a page -- "
            "inline less into it, and keep values out of it: they belong in the crew "
            "log and reach the page through the fold / bus / controller path",
        )
    # The frame's own refusals, asked here so the author meets them at save time rather
    # than seeing a page that renders wrong. Imported lazily: this module is imported by
    # `kiro_crew.artifacts` and must stay cheap.
    from kiro_crew import dashboard_frame

    try:
        dashboard_frame.validate_page(raw)
    except dashboard_frame.PageError as exc:
        raise _refuse("view.html", str(exc)) from None
    bound = sorted({(a or b or c) for a, b, c in _HTML_BINDING_RE.findall(raw)})
    unknown = [name for name in bound if name not in declared_fields]
    if unknown:
        # REDACTED BEFORE IT IS ECHOED, the same rule a DECLARED name gets in
        # `_refuse_credential_shaped_name`. A name reaches this branch precisely
        # because the model does not declare it, so it never passed that check -- and
        # this refusal is returned to its sender as an HTTP error body, which is one of
        # the places echoing a credential-shaped name would put it. Redaction is
        # identity on an ordinary name, so the author still reads which field to fix.
        from kiro_crew.platform.context import redact_via_context

        safe = sorted(redact_via_context(name) for name in unknown)
        raise _refuse(
            "view.html",
            f"binds {safe}, which model.types does not declare "
            f"(declared: {sorted(declared_fields)}). The model is also the list of "
            "fields the controller will push, so a name only the page knows is a cell "
            "nothing ever fills",
        )
    return raw


def _validate_view(raw: Any, declared_fields: Mapping[str, Any]) -> dict[str, Any]:
    view = _an_object(raw, "view")
    _check_keys(view, "view", ("blocks", "html"))
    # EXACTLY ONE. Two views has no answer to "what does this page look like", and
    # drawing one while ignoring the other picks for the author silently -- when the
    # half that was ignored is the half they were editing. None is the same question
    # with no answer at all.
    if ("html" in view) == ("blocks" in view):
        raise _refuse(
            "view",
            "must hold exactly one of 'html' (a page its owner wrote) or 'blocks' (a "
            f"list of block types out of the catalog); this one holds {sorted(view)}",
        )
    if "html" in view:
        return {"html": _validate_view_html(view["html"], declared_fields)}
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


def theme_stylesheet(raw: Any) -> str:
    """The package's ``theme`` as one stylesheet: its tokens on ``:root``, then its css.

    Checked by the same validator the write gate runs, so what reaches a ``<style>``
    element is only what the gate accepts: a token value holds no ``;``, ``{``, ``}``,
    ``<`` or ``>``, and the css holds no ``</style``, so neither can close the element
    or open a rule of its own. Raises :class:`~kiro_crew.artifact_store.model.ArtifactValidationError` for a theme the
    gate would refuse, such as one written under an older validator.
    """
    theme = _validate_theme(raw)
    tokens = "".join(f"{name}:{value};" for name, value in theme["tokens"].items())
    parts = [f":root{{{tokens}}}"] if tokens else []
    if theme.get("css"):
        parts.append(theme["css"])
    return "\n".join(parts)


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


#: How a field's source is spelled for a reader: the fold and the dotted path it walks,
#: or the key the owner writes itself.
SOURCE_FOLD_PREFIX = "fold:"
SOURCE_CUSTOM_PREFIX = "custom:"


def subscribed_fields(package: Mapping[str, Any]) -> dict[str, str]:
    """Every field this page may be sent, by name, each with where its value comes from.

    THE ALLOWLIST, and the reason it is one function rather than a rule each caller
    applies: the controller asks it what may be pushed, and the write gate has already
    validated the page's bindings against the same table. A field the model does not
    declare therefore cannot reach the page by either route -- it is not pushed, and a
    binding naming it is refused at save time.

    The source is a STRING for a reader -- a log line, a refusal, an evidence file --
    spelled ``fold:<fold>.<path>`` or ``custom:<key>``. The caller that needs to
    subscribe reads ``package["model"]["types"]`` itself; collapsing a fold and a path
    into one string and splitting it again would be a second grammar for something the
    package already says in two fields.

    *package* must be CANONICAL -- the output of :func:`validate_package` or
    :func:`parse_package` -- because a field with no validated ``source`` has no answer
    here and this function does not re-validate one.
    """
    types = (package.get("model") or {}).get("types")
    if not isinstance(types, Mapping):
        return {}
    out: dict[str, str] = {}
    for name in sorted(types):
        spec = types[name]
        source = spec.get("source") if isinstance(spec, Mapping) else None
        if not isinstance(source, Mapping):
            continue
        if source.get("agentic") is True:
            # Keyed by the FIELD NAME, which is what the agentic fold stores a value
            # under. There is no second key to carry, so a reader seeing
            # ``custom:boards`` knows both the source kind and the key.
            out[name] = f"{SOURCE_CUSTOM_PREFIX}{name}"
            continue
        fold = str(source.get("fold") or "")
        path = str(source.get("path") or "")
        if fold and path:
            out[name] = f"{SOURCE_FOLD_PREFIX}{fold}.{path}"
    return out


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
        # come apart: a few thousand ``[`` are small enough to pass the cap and
        # deep enough that the decoder runs out of stack. ``RecursionError``
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
    package = parse_package(content)
    _require_html_for_a_crewmate(package)
    return dump_package(package)


#: The scope prefix of a crewmate's own page in ``bound_to``.
_CREWMATE_BINDING_PREFIX = "crewmate:"


def _require_html_for_a_crewmate(package: Mapping[str, Any]) -> None:
    """Refuse a crewmate page whose view is a block layout. Write gate only.

    A crewmate's Dashboard tab frames the package's own ``view.html`` and nothing
    else (``member_dashboard_fields.render_package_page``), so a block layout would
    store, apply and then draw as an empty page. Checked on writes and applies,
    not on reads: a block package already stored still parses, and is told this
    the next time it is applied.
    """
    bound = str(package.get("bound_to") or "")
    view = package.get("view") or {}
    if bound.startswith(_CREWMATE_BINDING_PREFIX) and "blocks" in view:
        raise _refuse(
            "view",
            "a crewmate's page is drawn from view.html only, so a view of blocks "
            'cannot be shown: send view as {"html": "..."} with one '
            'data-dashboard-field="<name>" element per field the model declares, '
            "which the host fills as text",
        )


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


class BindingAmbiguous(BindingLookupIncomplete):
    """Two stored packages name the same binding, so WHICH one is unproven.

    A SUBCLASS, and the inheritance is the point rather than taxonomy: every caller
    that already handles "I could not prove there is no package here" needs the same
    answer for "I found two", and both are the absence of a single provable one. So no
    caller changes, and a caller that wants to tell them apart can.

    Raised rather than picking the first seen. The choice would be arbitrary -- the
    listing's order is the store's, not a statement about which package is the live one
    -- and a reader cannot tell an arbitrary pick from a correct one. The two packages
    are bound to the same crewmate, so neither shows a stranger's work; what it costs is
    determinism, and a dashboard that silently swaps page when a library is relisted is
    worse than one that says the binding is ambiguous and names both.
    """


def resolve_bound_slug(bound_to: str, *, store: "ArtifactStore | None" = None) -> str | None:
    """The slug of the dashboard artifact bound to ``bound_to``, or ``None``.

    The binding half of the lookup on its own, for a caller that is keyed by a member
    but needs the artifact. Reads the ``kind="dashboard"`` artifacts newest-first,
    capped at :data:`MAX_BINDING_SCAN`; there is one package per crewmate or slot, so
    this ends on its first or second read in practice.

    ``None`` means absent as far as the LISTING goes: every dashboard record the store
    listed was opened, every envelope was readable, and none of them names this
    binding. Three things leave even that unproven and raise
    :class:`BindingLookupIncomplete` instead -- a scan the cap cut short, a record this
    process could not read, and an envelope it could not decode. None of the three may
    answer ``None``, because a caller reads ``None`` as "this member has no package":
    the write path then validates against a DIFFERENT Model and a cell declared
    ``text`` by the member's real package is replaced by an array. All three name a
    record the listing PROVED is ``kind="dashboard"``, so one of them really may be the
    package bound here.

    What ``None`` does NOT rule out is a record whose own ``meta.json`` will not read.
    ``ArtifactStore.list`` drops such a record with a warning -- the right default,
    since one corrupt file must not take a library down -- so it never reaches the scan
    at all, and its ``kind`` is precisely what could not be read. Counting those would
    answer the same thing this function already answers for them, since a caller whose
    page is drawn from a template is right to go on using it, so the warning ``list``
    already emits is where that fact lives. A caller that must distinguish them needs
    the store to report them, which is for the change that has such a caller.

    The binding is matched off the stored ENVELOPE, not off a validated package, so a
    layout the current validator rejects -- an older schema, or a write that did not
    finish -- is still FOUND and reported broken by the caller that reads its content.

    THE SCAN DOES NOT STOP AT THE FIRST MATCH. One binding may name one package, and
    proving that takes reading the rest: a second claimant raises
    :class:`BindingAmbiguous`, and an unreadable record met AFTER a match raises
    :class:`BindingLookupIncomplete`, because that record could be the second claimant
    and so uniqueness is exactly what went unproven. An unreadable record met BEFORE
    the match is the same fact for the same reason, which is why the count is kept
    across the whole scan rather than reset.

    A LIVE-BACKED record is not a candidate at all. Its bytes are a file's current
    contents, which nothing in the artifact API gates, so it must not be considered
    this crewmate's page even to the extent of claiming the binding -- a page an
    ungated writer controls would otherwise take the binding away from the real one and
    turn a resolvable dashboard into an ambiguous or refused read. A COPY with
    provenance (``source_copy_only``) owns its bytes and is an ordinary candidate.
    """
    bound_to = validate_bound_to(bound_to)
    if store is None:
        from kiro_crew.artifacts import get_default_store

        store = get_default_store()
    from kiro_crew.artifacts import ArtifactError

    records = store.list(kind=DASHBOARD_KIND)
    unread = 0
    found = ""
    for art in records[:MAX_BINDING_SCAN]:
        # A LIVE POINTER is skipped before it is even opened: the artifact serves a
        # file's current bytes, which no artifact-API gate stands in front of, so this
        # record cannot be the package bound anywhere. Skipped rather than counted as
        # unread, because nothing about it is unknown -- what it is is disqualifying.
        if str(getattr(art, "source_path", "") or "") and not bool(
            getattr(art, "source_copy_only", False)
        ):
            continue
        try:
            loaded = store.get(art.slug)
            raw = _decode_package(loaded.content or "")
        except (ArtifactError, OSError):
            # Counted rather than skipped. A record that cannot be opened or decoded
            # may BE the one bound here -- that is precisely what could not be read --
            # and once a match is in hand it may be the SECOND one, which is the same
            # unproven answer seen from the other side.
            unread += 1
            continue
        # The query is validated, so an equal string is a well-formed binding: a
        # record whose own ``bound_to`` is malformed cannot match one and needs no
        # separate check here.
        if isinstance(raw, Mapping) and raw.get("bound_to") == bound_to:
            if found:
                raise BindingAmbiguous(
                    f"dashboard package: binding: {found!r} and {loaded.slug!r} are both "
                    f"bound to {bound_to!r}, so which one this names is unproven. There "
                    "is one package per crewmate or slot: delete or rebind one of them"
                )
            found = loaded.slug
    if len(records) > MAX_BINDING_SCAN:
        raise BindingLookupIncomplete(
            f"dashboard package: binding: {len(records)} dashboard artifacts are stored "
            f"and this lookup opens the newest {MAX_BINDING_SCAN}, so a package bound to "
            f"{bound_to!r} behind them was not seen. There is one package per crewmate "
            "or slot, so this many is a library to prune, not a lookup to widen"
        )
    if unread:
        # ONE SENTENCE FOR TWO READINGS, because the fact is one: a record that could
        # not be read may be the package bound here (so ``None`` is unproven) or a
        # second one bound here (so the match is unproven). Which of the two it would
        # have been is precisely what could not be determined.
        raise BindingLookupIncomplete(
            f"dashboard package: binding: {unread} of {len(records)} dashboard "
            f"artifacts could not be read, so nothing proves "
            + (
                f"{found!r} is the only one bound to {bound_to!r}"
                if found
                else f"none of them is bound to {bound_to!r}"
            )
        )
    return found or None
