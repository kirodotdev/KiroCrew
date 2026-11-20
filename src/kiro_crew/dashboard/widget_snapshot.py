"""One phone widget of a Bot, resolved into the snapshot the phone stores and draws.

A widget is layout in the Bot's dashboard package (``artifact_store.dashboard_widgets``):
typed blocks, each bound by name to a field ``model.types`` declares. This module turns
one widget into the wire snapshot of the mobile widgets design (section 3.2.3):

1. **Select the package** with :func:`member_dashboard.package_for_reader`, the page
   route's own selection. A staged page is never used. No package in force, or one
   whose bytes or binding cannot be proved, leaves the Bot only :data:`DEFAULT_WIDGET`.
2. **Cut the manifest** to the fields the widget binds, so the read subscribes only
   their folds and nothing else the package declares can reach the snapshot.
3. **Read** through :func:`member_dashboard.read_fields`, the read the page and the
   agent share.
4. **Shape** each block, with its freshness taken from the source's own timestamps.

TWO SEAMS ARE LEFT OPEN ON PURPOSE. ``redact`` runs on every resolved value before a
block reads it, and ``clean`` on every string a block emits. Both default to the
identity here: the widget boundary's redaction and its text sanitizer are separate
steps, and each replaces one default without touching the shaping.

FRESHNESS IS NEVER INVENTED. Each block's ``freshness`` names its ``source``:

* ``agentic``: ``at`` is the agentic fold's own write stamp for the field and
  ``stale_after`` is ``dashboard_agentic.stale_after_map`` over it.
* ``record``: a ``custom.latest.<type>`` field. ``at``, ``stale_after`` and
  ``failed_at`` are fields of that record, which the gateway stamped.
* ``fold``: any other fold field. The ``goal`` and ``waiting`` folds render the time
  of the newest entry they applied (:data:`_STAMPED_FOLDS`), and a field of one of them
  takes that as ``at``. The feed keeps no clock for any other fold, so ``at`` is
  ``null`` there and the block is ``current`` as of the moment the gateway read it (the
  snapshot's ``generated_at``). A fold field never goes stale on a time it does not
  have.
* ``none``: a title block with literal text.

A field the read reports missing is ``missing`` with every time ``null``, whatever its
source.

No HTTP and no state between calls; the routes and their owner check are elsewhere.
Blocking IO in :func:`resolve_snapshot` runs off the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Final

from kiro_crew.artifact_store.dashboard_widgets import RESERVED_WIDGET_ID

logger = logging.getLogger(__name__)

#: The snapshot wire version.
SNAPSHOT_V: Final = 1

STATE_CURRENT: Final = "current"
STATE_STALE: Final = "stale"
STATE_FAILED: Final = "failed"
STATE_MISSING: Final = "missing"

SOURCE_AGENTIC: Final = "agentic"
SOURCE_RECORD: Final = "record"
SOURCE_FOLD: Final = "fold"
SOURCE_NONE: Final = "none"

#: The fold a script cron's records land in, as ``latest.<type>`` rows.
_CUSTOM_FOLD: Final = "custom"
#: A companion field name the cut manifest reads a ``custom`` row's own stamps through.
#: A leading ``_`` is outside the declared-field grammar, so it never names a real field.
_ROW_PREFIX: Final = "_row_"
#: A companion field name for a stamped fold's own write time (:data:`_STAMPED_FOLDS`).
_STAMP_PREFIX: Final = "_at_"

#: Folds whose rendered value carries, at this path, the time of the newest crew-log
#: entry the fold applied (``projection_crewmate``: ``state["updated_at"] =
#: _panel_iso(entry.time)``). A field of one of these folds takes that as its ``at``.
#: Listed rather than probed, because a root ``updated_at`` in another fold could mean
#: something else; a fold not listed here has no clock in its value.
_STAMPED_FOLDS: Final[Mapping[str, str]] = {"goal": "updated_at", "waiting": "updated_at"}

#: The gateway's own model for the default "Status" widget (design 3.2.4).
DEFAULT_TYPES: Final[Mapping[str, Mapping[str, Any]]] = {
    "waiting_count": {"type": "number", "source": {"fold": "waiting", "path": "count"}},
    "waiting_items": {"type": "array", "source": {"fold": "waiting", "path": "items"}},
    "waiting_omitted": {"type": "number", "source": {"fold": "waiting", "path": "omitted"}},
    "workers_total": {"type": "number", "source": {"fold": "workers", "path": "total"}},
    "milestones_total": {
        "type": "number",
        "source": {"fold": "goal", "path": "milestones_total"},
    },
}

#: The default "Status" widget every Bot has, package or not. Its title block's text
#: is the Bot's display name, filled in by :func:`resolve_snapshot`.
DEFAULT_WIDGET: Final[Mapping[str, Any]] = {
    "id": RESERVED_WIDGET_ID,
    "title": "Status",
    "sizes": ["small", "medium"],
    "blocks": [
        {"type": "title", "text": ""},
        {
            "type": "metric",
            "label": "Waiting on you",
            "field": "waiting_count",
            "format": "integer",
            "unit": "",
            "warn_at": 1,
            "higher_is_worse": True,
        },
        {
            "type": "metric",
            "label": "Workers",
            "field": "workers_total",
            "format": "integer",
            "unit": "",
            "higher_is_worse": True,
        },
        {
            "type": "list",
            "label": "Waiting",
            "field": "waiting_items",
            "item_text": "text",
            "max_items": 3,
            # The waiting fold keeps a capped list and counts the rows it cut; added to
            # this block's own cut so shown plus omitted is every waiting item.
            "omitted_field": "waiting_omitted",
        },
    ],
    "tap": {"open": "bot"},
}


def _identity(value: Any) -> Any:
    return value


# --------------------------------------------------------------------------
# package selection
# --------------------------------------------------------------------------


async def package_in_force(slug: str, member: str) -> tuple[dict[str, Any] | None, int]:
    """``(canonical package, layout_version)`` in force for the Bot, or ``(None, 0)``.

    The selection is :func:`member_dashboard.package_for_reader`; this only reads the
    selected bytes and checks them. TOTAL: every failure is "no package", which leaves
    the Bot its default widget, the same answer the page gives an unprovable binding.
    """
    from kiro_crew.dashboard.handlers import member_dashboard as md

    try:
        package_slug, pinned = await md.package_for_reader(slug, member)
    except Exception:
        logger.debug("widgets: no package selection for %r", slug, exc_info=True)
        return None, 0
    if not package_slug or md._name_unsafe(package_slug):
        return None, 0
    if pinned is not None:
        version, content = pinned.version, pinned.content
    else:
        loaded = await asyncio.to_thread(_applied_bytes, package_slug)
        if loaded is None:
            return None, 0
        version, content = loaded
    return await asyncio.to_thread(_checked_package, content, version, member)


def _applied_bytes(package_slug: str) -> tuple[int, str] | None:
    """An older record's bound package page, through the tab's own checks, or ``None``.

    :func:`member_dashboard.applied_package_page`, shared with the agent panel's
    snapshot; any refusal or store error is ``None``. Blocking IO.
    """
    from kiro_crew.dashboard.handlers import member_dashboard as md

    try:
        return md.applied_package_page(package_slug)
    except Exception:
        logger.debug("widgets: package %r could not be read", package_slug, exc_info=True)
        return None


def _checked_package(content: str, version: int, member: str) -> tuple[dict[str, Any] | None, int]:
    """The parsed package if it loads and is bound to *member*, else ``(None, 0)``."""
    from kiro_crew.artifact_store.dashboard_package import binding_refusal, parse_package

    try:
        package = parse_package(content)
        if binding_refusal(package.get("bound_to"), crew=member):
            return None, 0
    except Exception:
        logger.debug("widgets: the package in force does not load", exc_info=True)
        return None, 0
    return package, int(version)


def widgets_of(package: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    """The Bot's widgets: the package's authored ones, then the default widget."""
    authored = package.get("widgets") if isinstance(package, Mapping) else None
    out = [w for w in authored if isinstance(w, Mapping)] if isinstance(authored, list) else []
    return [*out, DEFAULT_WIDGET]


def types_of(package: Mapping[str, Any] | None, widget: Mapping[str, Any]) -> Mapping[str, Any]:
    """The field table *widget* binds against: the gateway's own for the default widget."""
    if widget.get("id") == RESERVED_WIDGET_ID:
        return DEFAULT_TYPES
    model = package.get("model") if isinstance(package, Mapping) else None
    types = model.get("types") if isinstance(model, Mapping) else None
    return types if isinstance(types, Mapping) else {}


# --------------------------------------------------------------------------
# the manifest cut
# --------------------------------------------------------------------------


def bound_fields(widget: Mapping[str, Any]) -> list[str]:
    """Every field name *widget*'s blocks bind, in first-use order."""
    names: list[str] = []
    for block in widget.get("blocks") or ():
        for key in ("field", "total_field", "omitted_field"):
            name = block.get(key) if isinstance(block, Mapping) else None
            if isinstance(name, str) and name not in names:
                names.append(name)
    return names


def _custom_row_path(spec: Mapping[str, Any]) -> str:
    """``latest.<type>`` for a field read from a ``custom`` record, else ``""``."""
    source = spec.get("source")
    if not isinstance(source, Mapping) or source.get("fold") != _CUSTOM_FOLD:
        return ""
    parts = str(source.get("path") or "").split(".")
    if len(parts) >= 2 and parts[0] == "latest" and parts[1]:
        return f"latest.{parts[1]}"
    return ""


def cut_types(types: Mapping[str, Any], widget: Mapping[str, Any]) -> dict[str, Any]:
    """*types* cut to the fields *widget* binds, plus one row companion per ``custom`` field.

    A companion reads the ``custom.latest.<type>`` record the field lives in, from the
    same fold, so the block can carry the record's own ``at``, ``stale_after`` and
    ``failed_at``. Companions feed freshness only; their values never reach a block.
    """
    cut: dict[str, Any] = {}
    for name in bound_fields(widget):
        spec = types.get(name)
        if not isinstance(spec, Mapping):
            continue
        cut[name] = spec
        row = _custom_row_path(spec)
        if row:
            cut[_ROW_PREFIX + name] = {
                "type": "object",
                "source": {"fold": _CUSTOM_FOLD, "path": row},
            }
            continue
        fold = _stamped_fold(spec)
        if fold:
            cut[_STAMP_PREFIX + fold] = {
                "type": "text",
                "source": {"fold": fold, "path": _STAMPED_FOLDS[fold]},
            }
    return cut


def _stamped_fold(spec: Mapping[str, Any] | None) -> str:
    """The fold of a fold field whose rendered value carries its own write stamp."""
    source = spec.get("source") if isinstance(spec, Mapping) else None
    fold = source.get("fold") if isinstance(source, Mapping) else None
    return fold if isinstance(fold, str) and fold in _STAMPED_FOLDS else ""


def cut_manifest(
    types: Mapping[str, Any], widget: Mapping[str, Any], *, slug: str, version: int
) -> Any:
    """The feed manifest for exactly the fields *widget* binds (and their companions)."""
    from kiro_crew.dashboard.handlers.member_dashboard_fields import package_manifest

    return package_manifest(
        {"model": {"types": cut_types(types, widget)}}, slug=slug, version=version
    )


# --------------------------------------------------------------------------
# freshness
# --------------------------------------------------------------------------


def _parse_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def iso(moment: datetime) -> str:
    """*moment* as the wire spells a time: UTC, whole seconds, ``Z``."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def freshness(
    *,
    source: str,
    at: Any = None,
    stale_after: Any = None,
    failed_at: Any = None,
    missing: bool = False,
    now: datetime,
) -> dict[str, Any]:
    """One block's ``freshness``, with the dashboard skill's rule at *now*.

    ``failed`` when ``failed_at`` is present; ``stale`` when *now* is past
    ``stale_after``; a record exactly at ``stale_after`` is still ``current``. An
    unparseable time is ``null``: it is no evidence either way.
    """
    if missing:
        return {
            "state": STATE_MISSING,
            "at": None,
            "stale_after": None,
            "failed_at": None,
            "source": source,
        }
    at_t, stale_t, failed_t = _parse_time(at), _parse_time(stale_after), _parse_time(failed_at)
    if failed_t is not None:
        state = STATE_FAILED
    elif stale_t is not None and now > stale_t:
        state = STATE_STALE
    else:
        state = STATE_CURRENT
    return {
        "state": state,
        "at": iso(at_t) if at_t else None,
        "stale_after": iso(stale_t) if stale_t else None,
        "failed_at": iso(failed_t) if failed_t else None,
        "source": source,
    }


def _field_freshness(
    name: str,
    spec: Mapping[str, Any] | None,
    read: Any,
    stale_after: Mapping[str, str],
    now: datetime,
) -> dict[str, Any]:
    source_spec = spec.get("source") if isinstance(spec, Mapping) else None
    agentic = isinstance(source_spec, Mapping) and source_spec.get("agentic") is True
    row_path = _custom_row_path(spec) if isinstance(spec, Mapping) else ""
    source = SOURCE_AGENTIC if agentic else SOURCE_RECORD if row_path else SOURCE_FOLD
    fields = getattr(read, "fields", {}) or {}
    if name not in fields:
        return freshness(source=source, missing=True, now=now)
    if agentic:
        written = getattr(read, "written_at", {}) or {}
        return freshness(
            source=source, at=written.get(name), stale_after=stale_after.get(name), now=now
        )
    if row_path:
        row = fields.get(_ROW_PREFIX + name)
        row = row if isinstance(row, Mapping) else {}
        return freshness(
            source=source,
            at=row.get("at"),
            stale_after=row.get("stale_after"),
            failed_at=row.get("failed_at"),
            now=now,
        )
    fold = _stamped_fold(spec)
    stamp = fields.get(_STAMP_PREFIX + fold) if fold else None
    return freshness(source=source, at=stamp, now=now)


#: Worst last: a block reading two fields takes the worse of their two states.
_STATE_ORDER: Final = (STATE_CURRENT, STATE_STALE, STATE_FAILED, STATE_MISSING)


def merge_freshness(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    """One freshness for a block that reads two fields (``progress`` with ``total_field``).

    The worse state, with that input's ``source``; for each time, the earlier one, so a
    fresh numerator can never hide a stale or failed denominator. Ties keep *first*.
    """
    worse = (
        second
        if _STATE_ORDER.index(second["state"]) > _STATE_ORDER.index(first["state"])
        else first
    )

    def earlier(key: str) -> Any:
        times = [t for t in (first.get(key), second.get(key)) if t]
        return min(times) if times else None

    if worse["state"] == STATE_MISSING:
        return dict(worse)
    return {
        "state": worse["state"],
        "at": earlier("at"),
        "stale_after": earlier("stale_after"),
        "failed_at": earlier("failed_at"),
        "source": worse["source"],
    }


# --------------------------------------------------------------------------
# block shapes
# --------------------------------------------------------------------------


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _scalar_text(value: Any, clean: Callable[[str], str]) -> str | None:
    """A cell value as text: strings and numbers; ``None`` for anything else."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return clean(value)
    if _number(value) is not None:
        return clean(str(value))
    return None


def metric_tone(block: Mapping[str, Any], value: Any) -> str:
    """``bad`` / ``warn`` / ``ok`` against the block's thresholds, inclusive; else ``idle``.

    ``higher_is_worse`` (default true) reads a threshold as a floor; false as a ceiling.
    No threshold, or a value that is not a number, is ``idle``.
    """
    number = _number(value)
    warn_at, bad_at = _number(block.get("warn_at")), _number(block.get("bad_at"))
    if number is None or (warn_at is None and bad_at is None):
        return "idle"
    worse = block.get("higher_is_worse", True) is not False

    def crossed(limit: int | float | None) -> bool:
        if limit is None:
            return False
        return number >= limit if worse else number <= limit

    if crossed(bad_at):
        return "bad"
    if crossed(warn_at):
        return "warn"
    return "ok"


def _list_items(
    block: Mapping[str, Any], value: Any, clean: Callable[[str], str]
) -> tuple[list, int]:
    rows = value if isinstance(value, list) else []
    item_text, item_detail = block.get("item_text"), block.get("item_detail")
    items: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, Mapping):
            text = _scalar_text(row.get(item_text), clean)
            detail = _scalar_text(row.get(item_detail), clean) if item_detail else None
        else:
            text, detail = _scalar_text(row, clean), None
        if text is None:
            continue
        item: dict[str, Any] = {"text": text}
        if item_detail:
            item["detail"] = detail
        items.append(item)
    limit = int(block.get("max_items") or 3)
    return items[:limit], max(0, len(items) - limit)


def _sparkline_points(block: Mapping[str, Any], value: Any) -> list[int | float]:
    rows = value if isinstance(value, list) else []
    point = block.get("point")
    points: list[int | float] = []
    for row in rows:
        raw = row.get(point) if isinstance(row, Mapping) and point else row
        number = _number(raw)
        if number is not None:
            points.append(number)
    limit = int(block.get("max_points") or 24)
    return points[-limit:]


def shape_block(
    block: Mapping[str, Any],
    values: Mapping[str, Any],
    fresh: Callable[[str], dict[str, Any]],
    *,
    now: datetime,
    clean: Callable[[str], str] = _identity,
) -> dict[str, Any]:
    """One block on the wire. *values* are the resolved (and redacted) field values.

    A field with no value is ``null``, drawn as a dash, never as ``0``.
    """
    kind = str(block.get("type") or "")
    field = block.get("field")
    value = values.get(field) if isinstance(field, str) else None
    out: dict[str, Any] = {"type": kind}
    if kind == "title":
        if isinstance(field, str):
            out["text"] = _scalar_text(value, clean)
            out["freshness"] = fresh(field)
        else:
            out["text"] = clean(str(block.get("text") or ""))
            out["freshness"] = freshness(source=SOURCE_NONE, now=now)
        return out
    out["label"] = clean(str(block.get("label") or ""))
    if kind == "metric":
        fmt = str(block.get("format") or "integer")
        number = _number(value)
        out["value"] = number if number is not None else _scalar_text(value, clean)
        out["format"] = fmt
        out["unit"] = clean(str(block.get("unit") or ""))
        out["tone"] = metric_tone(block, value)
    elif kind == "progress":
        out["value"] = _number(value)
        total_field = block.get("total_field")
        out["total"] = (
            _number(values.get(total_field))
            if isinstance(total_field, str)
            else _number(block.get("total"))
        )
    elif kind == "list":
        out["items"], out["omitted"] = _list_items(block, value, clean)
        omitted_field = block.get("omitted_field")
        already_cut = _number(values.get(omitted_field)) if isinstance(omitted_field, str) else None
        if already_cut is not None and already_cut > 0:
            out["omitted"] += int(already_cut)
    elif kind == "sparkline":
        out["points"] = _sparkline_points(block, value)
    elif kind == "status":
        text = _scalar_text(value, clean)
        raw_tones = block.get("tones")
        tones: Mapping[str, Any] = raw_tones if isinstance(raw_tones, Mapping) else {}
        out["text"] = text
        out["tone"] = tones.get(text, "idle") if text is not None else "idle"
    if not isinstance(field, str):
        out["freshness"] = freshness(source=SOURCE_NONE, now=now)
    elif kind == "progress" and isinstance(block.get("total_field"), str):
        out["freshness"] = merge_freshness(fresh(field), fresh(block["total_field"]))
    else:
        out["freshness"] = fresh(field)
    return out


def shape_snapshot(
    widget: Mapping[str, Any],
    types: Mapping[str, Any],
    read: Any,
    manifest: Any,
    *,
    bot: Mapping[str, str],
    layout_version: int,
    now: datetime,
    redact: Callable[[Any], Any] = _identity,
    clean: Callable[[str], str] = _identity,
) -> dict[str, Any]:
    """The 3.2.3 snapshot of *widget* from one field *read*. Pure: no IO.

    Only the fields the widget binds are looked at, so a value the read happens to hold
    for any other name never reaches the body. *redact* sees each of those values once,
    before any block reads it.
    """
    from kiro_crew.dashboard_agentic import stale_after_map

    fields = getattr(read, "fields", {}) or {}
    bound = bound_fields(widget)
    values = {name: redact(fields[name]) for name in bound if name in fields}
    stale_after = stale_after_map(manifest, getattr(read, "written_at", {}) or {})

    def fresh(name: str) -> dict[str, Any]:
        return _field_freshness(name, types.get(name), read, stale_after, now)

    default = widget.get("id") == RESERVED_WIDGET_ID
    blocks = []
    for block in widget.get("blocks") or ():
        if default and block.get("type") == "title" and "field" not in block:
            block = {**block, "text": bot.get("name", "")}
        blocks.append(shape_block(block, values, fresh, now=now, clean=clean))
    return {
        "v": SNAPSHOT_V,
        "bot": {key: str(bot.get(key, "")) for key in ("slug", "member", "name")},
        "widget": {
            "id": widget.get("id"),
            "title": clean(str(widget.get("title") or "")),
            "sizes": list(widget.get("sizes") or ()),
            "default": default,
        },
        "layout_version": 0 if default else int(layout_version),
        "generated_at": iso(now),
        "blocks": blocks,
        "tap": {"open": "bot", "value": str(bot.get("slug", ""))},
    }


# --------------------------------------------------------------------------
# the resolver
# --------------------------------------------------------------------------


async def resolve_snapshot(
    slug: str,
    member: str,
    widget_id: str,
    *,
    name: str = "",
    now: datetime | None = None,
    redact: Callable[[Any], Any] = _identity,
    clean: Callable[[str], str] = _identity,
) -> dict[str, Any] | None:
    """The snapshot of the Bot's widget *widget_id*, or ``None`` when it has no such widget.

    The read is blocking IO and runs off the loop.
    """
    from kiro_crew.dashboard.handlers import member_dashboard as md

    package, layout_version = await package_in_force(slug, member)
    widget = next((w for w in widgets_of(package) if w.get("id") == widget_id), None)
    if widget is None:
        return None
    types = types_of(package, widget)
    manifest = cut_manifest(types, widget, slug=slug, version=layout_version)
    read = await asyncio.to_thread(md.read_fields, slug, member, manifest)
    return shape_snapshot(
        widget,
        types,
        read,
        manifest,
        bot={"slug": slug, "member": member, "name": name or member},
        layout_version=layout_version,
        now=now or datetime.now(timezone.utc),
        redact=redact,
        clean=clean,
    )
