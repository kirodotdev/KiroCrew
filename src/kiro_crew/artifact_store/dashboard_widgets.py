"""The optional ``widgets`` list of a dashboard package: home-screen widgets on a phone.

A widget is LAYOUT, like the rest of the package: an ordered list of typed blocks,
each bound by name to a field the package's ``model.types`` already declares. The
gateway resolves a widget into values later, through the same field gate the page
uses, so a widget can only ask for what the Bot already declared for its page.

The rules enforced here, every one a refusal in :func:`_refuse`'s shape:

* at most :data:`MAX_WIDGETS` widgets, each of 1 to :data:`MAX_BLOCKS` blocks;
* a widget ``id`` matches :data:`_WIDGET_ID_RE`, is unique in the package, and is
  never :data:`RESERVED_WIDGET_ID` (the gateway's own default widget);
* titles and labels are 1 to :data:`MAX_WIDGET_LABEL_LEN` characters, a unit 0 to
  :data:`MAX_UNIT_LEN`;
* ``sizes`` is a non-empty subset of :data:`WIDGET_SIZES`;
* ``tap.open`` is ``bot`` only;
* a block binds only a declared field of a type its block type accepts, and never
  a field read from a tree-keyed fold (``widget_tree_fold``): the widget read path
  reads slot folds and the agentic projection, not tree folds;
* the canonical ``widgets`` JSON is at most :data:`MAX_WIDGETS_BYTES` bytes.

A package with no ``widgets`` (or an empty list) is stored without the key, so it
canonicalizes to exactly the bytes it did before widgets existed.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any, Final

from kiro_crew.artifact_store.dashboard_package import _an_object, _check_keys, _refuse
from kiro_crew.artifact_store.model import ArtifactValidationError

#: The package key this module owns.
WIDGETS_KEY: Final = "widgets"

# Limits: design choices, sized well under what a large phone widget can draw.
MAX_WIDGETS: Final = 4
MAX_BLOCKS: Final = 6
MAX_WIDGET_LABEL_LEN: Final = 40
MAX_UNIT_LEN: Final = 8
MAX_WIDGETS_BYTES: Final = 16 * 1024
MAX_TONES: Final = 8
#: The largest integer a phone's JSON number (an IEEE double) holds exactly: 2**53.
MAX_SAFE_INTEGER: Final = 2**53
LIST_MAX_ITEMS_RANGE: Final = (1, 5)
LIST_MAX_ITEMS_DEFAULT: Final = 3
SPARKLINE_MAX_POINTS_RANGE: Final = (2, 48)
SPARKLINE_MAX_POINTS_DEFAULT: Final = 24

#: The id the gateway's built-in "Status" widget uses; an authored widget may not take it.
RESERVED_WIDGET_ID: Final = "status"
#: Sizes in their canonical order.
WIDGET_SIZES: Final = ("small", "medium", "large")
TAP_TARGETS: Final = ("bot",)
TONES: Final = ("ok", "warn", "bad", "idle")
METRIC_FORMATS: Final = ("integer", "decimal", "percent", "duration_s", "text")

#: The refusal code a block bound to a tree-keyed fold's field carries.
TREE_FOLD_CODE: Final = "widget_tree_fold"

_WIDGET_ID_RE = re.compile(r"\A[a-z][a-z0-9-]{0,31}\Z")
#: A row key inside an array field's rows (``item_text``, ``item_detail``, ``point``).
_ROW_KEY_RE = re.compile(r"\A[A-Za-z0-9_]{1,64}\Z")

#: Per block type: required keys, optional keys, and the field types ``field`` accepts.
#: ``title`` is special-cased: exactly one of ``text`` or ``field``.
_BLOCKS: Final[Mapping[str, tuple[tuple[str, ...], tuple[str, ...], frozenset[str]]]] = {
    "title": ((), ("text", "field"), frozenset({"text", "enum"})),
    "metric": (
        ("label", "field"),
        ("format", "unit", "warn_at", "bad_at", "higher_is_worse"),
        frozenset({"number", "text"}),
    ),
    "list": (
        ("label", "field", "item_text"),
        ("item_detail", "max_items"),
        frozenset({"array"}),
    ),
    "progress": (("label", "field"), ("total", "total_field"), frozenset({"number"})),
    "sparkline": (("label", "field"), ("point", "max_points"), frozenset({"array"})),
    "status": (("label", "field"), ("tones",), frozenset({"enum", "bool", "text"})),
}


def _a_short_text(raw: Any, path: str, *, low: int, high: int) -> str:
    """A single-line string of *low* to *high* characters (code points)."""
    if not isinstance(raw, str):
        raise _refuse(path, f"must be a string, got {type(raw).__name__}")
    if "\n" in raw or "\r" in raw:
        raise _refuse(path, "must be a single line")
    if low and not raw.strip():
        raise _refuse(path, "must not be blank")
    if len(raw) > high:
        raise _refuse(path, f"must be at most {high} characters (this one has {len(raw)})")
    return raw


def _a_number(raw: Any, path: str) -> int | float:
    # An int is checked by magnitude, never through ``math.isfinite``: JSON decodes
    # ``1e400``-sized integers exactly, and converting one to float raises
    # OverflowError, which no write path answers as a 400.
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise _refuse(path, f"must be a finite number, got {type(raw).__name__}")
    if isinstance(raw, int) and abs(raw) > MAX_SAFE_INTEGER:
        raise _refuse(path, f"must be a finite number within +/-{MAX_SAFE_INTEGER}")
    if isinstance(raw, float) and not math.isfinite(raw):
        raise _refuse(path, f"must be a finite number, got {raw!r}")
    return raw


def _an_int_in(raw: Any, path: str, bounds: tuple[int, int]) -> int:
    low, high = bounds
    if isinstance(raw, bool) or not isinstance(raw, int) or not low <= raw <= high:
        raise _refuse(path, f"must be a whole number from {low} to {high}, got {raw!r}")
    return raw


def _a_row_key(raw: Any, path: str) -> str:
    if not isinstance(raw, str) or _ROW_KEY_RE.fullmatch(raw) is None:
        raise _refuse(path, f"{raw!r} is not a row key: letters, digits and '_', 1 to 64")
    return raw


def _tree_fold_names() -> frozenset[str]:
    """The tree-keyed folds, read from the catalogue so a new one is refused too."""
    from kiro_crew.crew_log.projection_catalogue import TREE_PROJECTION_NAMES

    return frozenset(TREE_PROJECTION_NAMES)


def _a_bound_field(
    raw: Any, path: str, declared: Mapping[str, Any], accepts: frozenset[str]
) -> str:
    """A declared field of an accepted type that the widget read path can resolve."""
    if not isinstance(raw, str) or raw not in declared:
        raise _refuse(
            path,
            f"binds {raw!r}, which model.types does not declare: a widget block binds "
            f"only the package's own fields, here {sorted(declared)}",
        )
    spec = declared[raw]
    if spec["type"] not in accepts:
        raise _refuse(
            path,
            f"binds {raw!r}, a {spec['type']!r} field; this block takes {sorted(accepts)}",
        )
    fold = spec["source"].get("fold")
    if fold in _tree_fold_names():
        raise _refuse(
            path,
            f"{TREE_FOLD_CODE}: binds {raw!r}, read from the tree-keyed fold {fold!r}, "
            "which only the live page resolves; a widget cannot show it",
        )
    return raw


def _validate_tones(raw: Any, path: str, field_spec: Mapping[str, Any]) -> dict[str, str]:
    # Not ``_an_object``: a tone's KEYS are field values (an enum's choices), and a
    # choice spelled like a data key ("items") is a value here, not a leaked one.
    if not isinstance(raw, dict) or not raw:
        raise _refuse(path, "must be a non-empty object of value to tone")
    if len(raw) > MAX_TONES:
        raise _refuse(path, f"holds {len(raw)} entries; at most {MAX_TONES} are allowed")
    choices = field_spec.get("choices") if field_spec["type"] == "enum" else None
    out: dict[str, str] = {}
    for key in sorted(raw, key=str):
        if not isinstance(key, str) or not key.strip():
            raise _refuse(path, "every key must be a non-blank string")
        if choices is not None and key not in choices:
            raise _refuse(f"{path}.{key}", f"is not one of the field's choices {choices}")
        tone = raw[key]
        if tone not in TONES:
            raise _refuse(f"{path}.{key}", f"is {tone!r}; a tone is one of {list(TONES)}")
        out[key] = tone
    return out


def _validate_block(raw: Any, path: str, declared: Mapping[str, Any]) -> dict[str, Any]:
    block = _an_object(raw, path)
    kind = block.get("type")
    if not isinstance(kind, str) or kind not in _BLOCKS:
        raise _refuse(f"{path}.type", f"is {kind!r}; a block type is one of {sorted(_BLOCKS)}")
    required, optional, accepts = _BLOCKS[kind]
    _check_keys(block, path, ("type", *required, *optional))
    for key in required:
        if key not in block:
            raise _refuse(f"{path}.{key}", f"is required by a {kind!r} block")
    out: dict[str, Any] = {"type": kind}
    if kind == "title":
        if ("text" in block) == ("field" in block):
            raise _refuse(path, "a 'title' block takes exactly one of 'text' or 'field'")
        if "text" in block:
            out["text"] = _a_short_text(
                block["text"], f"{path}.text", low=1, high=MAX_WIDGET_LABEL_LEN
            )
        else:
            out["field"] = _a_bound_field(block["field"], f"{path}.field", declared, accepts)
        return out
    out["label"] = _a_short_text(block["label"], f"{path}.label", low=1, high=MAX_WIDGET_LABEL_LEN)
    out["field"] = _a_bound_field(block["field"], f"{path}.field", declared, accepts)
    if kind == "metric":
        fmt = block.get("format", "integer")
        if fmt not in METRIC_FORMATS:
            raise _refuse(
                f"{path}.format", f"is {fmt!r}; a format is one of {list(METRIC_FORMATS)}"
            )
        out["format"] = fmt
        out["unit"] = _a_short_text(block.get("unit", ""), f"{path}.unit", low=0, high=MAX_UNIT_LEN)
        for key in ("warn_at", "bad_at"):
            if key in block:
                out[key] = _a_number(block[key], f"{path}.{key}")
        worse = block.get("higher_is_worse", True)
        if not isinstance(worse, bool):
            raise _refuse(f"{path}.higher_is_worse", f"must be true or false, got {worse!r}")
        out["higher_is_worse"] = worse
    elif kind == "list":
        out["item_text"] = _a_row_key(block["item_text"], f"{path}.item_text")
        if "item_detail" in block:
            out["item_detail"] = _a_row_key(block["item_detail"], f"{path}.item_detail")
        out["max_items"] = _an_int_in(
            block.get("max_items", LIST_MAX_ITEMS_DEFAULT),
            f"{path}.max_items",
            LIST_MAX_ITEMS_RANGE,
        )
    elif kind == "progress":
        if ("total" in block) == ("total_field" in block):
            raise _refuse(path, "a 'progress' block takes exactly one of 'total' or 'total_field'")
        if "total" in block:
            out["total"] = _a_number(block["total"], f"{path}.total")
        else:
            out["total_field"] = _a_bound_field(
                block["total_field"], f"{path}.total_field", declared, accepts
            )
    elif kind == "sparkline":
        if "point" in block:
            out["point"] = _a_row_key(block["point"], f"{path}.point")
        out["max_points"] = _an_int_in(
            block.get("max_points", SPARKLINE_MAX_POINTS_DEFAULT),
            f"{path}.max_points",
            SPARKLINE_MAX_POINTS_RANGE,
        )
    elif kind == "status" and "tones" in block:
        out["tones"] = _validate_tones(block["tones"], f"{path}.tones", declared[out["field"]])
    return out


def _validate_sizes(raw: Any, path: str) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise _refuse(path, f"must be a non-empty list drawn from {list(WIDGET_SIZES)}")
    for size in raw:
        if size not in WIDGET_SIZES:
            raise _refuse(path, f"{size!r} is not a size; sizes are {list(WIDGET_SIZES)}")
    if len(set(raw)) != len(raw):
        raise _refuse(path, "lists a size twice")
    return [size for size in WIDGET_SIZES if size in raw]


def _validate_tap(raw: Any, path: str) -> dict[str, str]:
    tap = _an_object(raw, path)
    _check_keys(tap, path, ("open",))
    target = tap.get("open", "bot")
    if target not in TAP_TARGETS:
        raise _refuse(
            f"{path}.open",
            f"is {target!r}; in this version a widget tap opens only its own Bot ('bot')",
        )
    return {"open": target}


def _validate_widget(raw: Any, path: str, declared: Mapping[str, Any]) -> dict[str, Any]:
    widget = _an_object(raw, path)
    _check_keys(widget, path, ("id", "title", "sizes", "blocks", "tap"))
    for key in ("id", "title", "sizes", "blocks"):
        if key not in widget:
            raise _refuse(f"{path}.{key}", "is required")
    wid = widget["id"]
    if not isinstance(wid, str) or _WIDGET_ID_RE.fullmatch(wid) is None:
        raise _refuse(
            f"{path}.id",
            f"{wid!r} is not a widget id: lowercase letters, digits and '-', starting "
            "with a letter, at most 32 characters",
        )
    if wid == RESERVED_WIDGET_ID:
        raise _refuse(
            f"{path}.id",
            f"{wid!r} is reserved for the default widget every Bot gets; pick another id",
        )
    blocks = widget["blocks"]
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= MAX_BLOCKS:
        count = len(blocks) if isinstance(blocks, list) else type(blocks).__name__
        raise _refuse(f"{path}.blocks", f"must be a list of 1 to {MAX_BLOCKS} blocks, got {count}")
    return {
        "id": wid,
        "title": _a_short_text(widget["title"], f"{path}.title", low=1, high=MAX_WIDGET_LABEL_LEN),
        "sizes": _validate_sizes(widget["sizes"], f"{path}.sizes"),
        "blocks": [
            _validate_block(block, f"{path}.blocks[{i}]", declared)
            for i, block in enumerate(blocks)
        ],
        "tap": _validate_tap(widget.get("tap", {}), f"{path}.tap"),
    }


def validate_widgets(raw: Any, declared: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the canonical ``widgets`` list, or raise saying why it is refused.

    *declared* is the CANONICAL ``model.types`` (validated already), so every field
    spec here has a ``type`` and a ``source``. An empty list comes back empty; the
    package validator then stores no ``widgets`` key at all.

    Every refusal is REDACTED before it leaves: the messages quote what the author
    wrote (an id, a field name, a tone key, an unknown key), the error string is
    logged and returned as an HTTP body, and a credential-shaped value would
    otherwise travel in clear. Done once here, so no single message can miss it.
    """
    from kiro_crew.platform.context import redact_via_context

    try:
        return _validate_widgets(raw, declared)
    except ArtifactValidationError as exc:
        raise ArtifactValidationError(redact_via_context(str(exc))) from None


def _validate_widgets(raw: Any, declared: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise _refuse(WIDGETS_KEY, f"must be a list of widgets, got {type(raw).__name__}")
    if len(raw) > MAX_WIDGETS:
        raise _refuse(WIDGETS_KEY, f"holds {len(raw)} widgets; at most {MAX_WIDGETS} are allowed")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        widget = _validate_widget(item, f"{WIDGETS_KEY}[{i}]", declared)
        if widget["id"] in seen:
            raise _refuse(f"{WIDGETS_KEY}[{i}].id", f"{widget['id']!r} is used by two widgets")
        seen.add(widget["id"])
        out.append(widget)
    # Measured on the indented form the store writes, not on the submitted text.
    size = len(json.dumps(out, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    if size > MAX_WIDGETS_BYTES:
        raise _refuse(
            WIDGETS_KEY,
            f"is {size} bytes stored, over the {MAX_WIDGETS_BYTES}-byte limit for widgets",
        )
    return out
