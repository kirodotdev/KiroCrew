"""A crew's own surfaces as slot folds: ``panel``, a crew's own webview, and the dynamic
dashboard's ``agentic`` values and ``mistakes`` book.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Final

from kiro_crew.crew_log.entry_types import (
    DASHBOARD_AGENTIC_ENTRY_TYPE,
    DASHBOARD_CODE_LIMIT,
    DASHBOARD_FIELD_LIMIT,
    DASHBOARD_REASON_LIMIT,
    DASHBOARD_REFUSED_ENTRY_TYPE,
    DASHBOARD_TYPE_LIMIT,
    DASHBOARD_VALUE_BYTES,
    DASHBOARD_VALUE_LIMIT,
    MISTAKE_CORRECTED_CODE,
    MISTAKE_CORRECTION_LIMIT,
    MISTAKE_GROUP_LIMIT,
    PANEL_CREW_KEY_LIMIT,
    PANEL_ENTRY_TYPE,
    PANEL_HISTORY_LIMIT,
    PANEL_OWNER_LIMIT,
    PANEL_TEMPLATE_LIMIT,
    PANEL_TITLE_LIMIT,
)
from kiro_crew.crew_log.projection_values import _as_int, _rows_in
from kiro_crew.crew_log.schema import Entry

# panel -- a crew's own webview, keyed by the publishing member's slot
# --------------------------------------------------------------------------- #

#: Describes the panel RECORD, not where it lives. Equal to the store's own version
#: so the shape a drawer consumes is unchanged by the record moving into this log; a
#: consumer of a panel does not branch on which file held it.
PANEL_SCHEMA_VERSION: Final[int] = 1


def _panel_iso(stamp_ms: int) -> str:
    """An entry's epoch-millisecond ``time`` as the record's UTC ``+00:00`` spelling.

    Derived from the envelope rather than written into the entry: one clock, and no
    way for an entry to claim a publish time the log disagrees with.

    Offset-CARRYING, because this value leaves the host. The drawer hands
    ``published_at`` to ``new Date()``, which reads an offset-free string as the
    BROWSER's local time -- invisible on the loopback dashboard, where the same clock
    wrote it, and skewed by the whole offset from a remote browser in another zone.

    A stamp outside the range a ``datetime`` can hold answers ``""``, the same thing
    an absent one answers: these are bytes a reader does not control, so a damaged or
    planted ``time`` would otherwise turn every read of that slot into a crash,
    permanently, since the line stays on disk and nothing rewrites it. Losing one
    stamp costs a reader a display value; raising costs it the whole panel.
    """
    try:
        return datetime.fromtimestamp(stamp_ms / 1000, tz=timezone.utc).isoformat(
            timespec="seconds"
        )
    except (OverflowError, OSError, ValueError):
        return ""


def _panel_text(value: Any, limit: int) -> str:
    """*value* as a clamped string, or ``""``. The fold's own shape gate."""
    if not isinstance(value, str):
        return ""
    return value[:limit]


def _panel_owner_start() -> dict[str, Any]:
    """One owner's panel state: the record it last published, and its past titles."""
    return {
        "template": "",
        "title": "",
        "crew": "",
        "crew_key": "",
        "data": {},
        "published_at": "",
        "history": [],
        "publishes": 0,
        # Overflow is COUNTED, not silently dropped: a trimmed tail otherwise reads
        # exactly like a crew that never published those cycles, so a reader cannot
        # tell a bounded history from a complete one.
        "history_omitted": 0,
        # Fold order, not a timestamp: this decides which owner is evicted, and
        # ``published_at`` is empty for an entry whose ``time`` no ``datetime`` can
        # hold, so a stamp key would rank a damaged record against real ones.
        "seq": 0,
    }


def _panel_start() -> dict[str, Any]:
    # Keyed by OWNERSHIP DIGEST rather than holding one record, because one slot can
    # carry two crews: the slot is the member slug's, and a crew whose persisted
    # ``member_id`` is another crew's name-derived slug lands on the same one. With a
    # single record the later publisher's panel would be the only one the fold could
    # answer with, so the other crew's own drawer showed nothing while its entries sat
    # in this very log. ``newest`` names the owner that published last, which is what
    # a reader with no digest of its own is answered with.
    return {"owners": {}, "newest": "", "owners_omitted": 0, "seq": 0}


def _panel_step(state: dict[str, Any], entry: Entry) -> None:
    if entry.type != PANEL_ENTRY_TYPE:
        return
    data = entry.data
    # WHOLE-DOCUMENT REPLACEMENT, which is where this fold parts company with the
    # ledger's. A publish replaces the panel, so a present ``template`` and ``data``
    # are what make an entry a publish at all; an entry missing either is a damaged
    # or planted line, and applying its half over a good panel would splice two
    # cycles' state together -- exactly the mixed record whole-replacement exists to
    # prevent. Skipped rather than partially applied.
    template = _panel_text(data.get("template"), PANEL_TEMPLATE_LIMIT)
    payload = data.get("data")
    if not template or not isinstance(payload, Mapping):
        return
    stamp = _panel_iso(entry.time)
    key = _panel_text(data.get("crew_key"), PANEL_CREW_KEY_LIMIT)
    owners: dict[str, Any] = state["owners"]
    own = owners.get(key)
    if own is None:
        # Bounded before the insert, and the LEAST RECENTLY PUBLISHED owner is what
        # goes: a slot that somehow sees many owners keeps the ones publishing now,
        # and a reader whose record aged out is answered the same way a reader with no
        # record is -- an empty panel, never another crew's.
        #
        # Ranked on the fold-order ``seq`` rather than on ``published_at``, because a
        # stamp is empty for an entry whose ``time`` no ``datetime`` can hold: a
        # string key would sort every such record first and evict a live crew's panel
        # on the strength of one planted line.
        if len(owners) >= PANEL_OWNER_LIMIT:
            oldest = min(owners, key=lambda k: _as_int(owners[k].get("seq")))
            del owners[oldest]
            # Said out loud, not silently dropped: without it the slot reports no
            # truncation at all. It counts evictions and names none of them -- the
            # deletion above removes the only thing that could identify one -- so it
            # cannot tell an evicted crew from one that never published here.
            state["owners_omitted"] = _as_int(state.get("owners_omitted")) + 1
        own = owners[key] = _panel_owner_start()
    # The SUPERSEDED panel becomes a history row, before the new one overwrites it,
    # so the row describes the panel that is being replaced rather than the one
    # replacing it. Nothing is appended for the first publish: there is no earlier
    # panel to record, and a row describing the empty start state would read as a
    # publish that never happened. Per owner, because a crew's history is its own.
    if own["template"]:
        rows: list[dict[str, str]] = own["history"]
        rows.append(
            {
                "at": own["published_at"],
                "title": own["title"],
                "template": own["template"],
            }
        )
        # Bounded like every other fold state here: the oldest superseded panel ages
        # out so a crew publishing every cycle cannot grow the record without limit.
        # What ages out is COUNTED, for the same reason the owner eviction is.
        if len(rows) > PANEL_HISTORY_LIMIT:
            dropped = len(rows) - PANEL_HISTORY_LIMIT
            del rows[:dropped]
            own["history_omitted"] = _as_int(own.get("history_omitted")) + dropped
    own["template"] = template
    own["data"] = dict(payload)
    own["title"] = _panel_text(data.get("title"), PANEL_TITLE_LIMIT)
    own["crew"] = _panel_text(data.get("crew"), PANEL_TITLE_LIMIT)
    own["crew_key"] = key
    own["published_at"] = stamp
    own["publishes"] += 1
    state["seq"] = _as_int(state.get("seq")) + 1
    own["seq"] = state["seq"]
    state["newest"] = key


def _panel_owner_record(own: Mapping[str, Any]) -> dict[str, Any]:
    """One owner's record, in the shape the drawer already consumes.

    ``history_omitted`` is the bound speaking: it is how a reader tells a history
    trimmed at its cap from one that holds every cycle the crew ever published. It
    belongs here because it is the OWNER's own bound. The slot's eviction count is
    not: eviction deletes an owner's entry, so the crew that count is about has no
    record here to read it from, and a copy on each surviving owner's record would
    answer a question none of them is asking.
    """
    return {
        "schema": PANEL_SCHEMA_VERSION,
        "template": own["template"],
        "title": own["title"],
        "crew": own["crew"],
        "crew_key": own["crew_key"],
        "data": dict(own["data"]),
        "published_at": own["published_at"],
        "history": [dict(row) for row in own["history"]],
        "publishes": own["publishes"],
        "history_omitted": _as_int(own.get("history_omitted")),
    }


def _panel_render(state: dict[str, Any]) -> dict[str, Any]:
    """The panel RECORD, in the shape the drawer already consumes.

    Deliberately the same keys the store's document carried when it was a file of
    its own, so the read route, the composer and the drawer did not have to learn a
    new shape to stop being a second copy of the truth. ``history`` and ``publishes``
    are what the file could not hold: one overwritable document has no past, which is
    the whole reason the record moved into this log.

    The top level is the NEWEST publish on this slot, and ``owners`` carries one
    record per publishing crew keyed by its ownership digest. A reader that knows
    which crew it is asking about reads its own entry there; the top level is for a
    reader that does not, and on an uncontested slot the two are the same record.

    An empty ``template`` is how a reader tells "this crew has published nothing"
    from "this crew published an empty panel": the store refuses a publish that names
    no template, so no real record has one.

    ``owners_omitted`` is the owner bound speaking, and it sits on THIS record alone
    rather than on each owner's, because the crew it is about has none: eviction
    deletes the owner's entry. So it speaks only about the fold. It rises on the
    eviction of any owner with no reference to any particular key, so a non-zero value
    says the slot truncated and cannot say whom -- a crew that never published on a
    slot four others filled reads exactly what a genuinely evicted crew reads. ``0``
    says this fold recorded no eviction, which is NOT the same as "this crew never
    published": the append is best-effort, so a publish whose entry never landed
    leaves no record here while the store's file still holds that crew's panel.
    """
    owners: dict[str, Any] = state["owners"]
    newest = owners.get(state["newest"])
    record = _panel_owner_record(newest if newest is not None else _panel_owner_start())
    record["owners"] = {key: _panel_owner_record(own) for key, own in owners.items()}
    record["owners_omitted"] = _as_int(state.get("owners_omitted"))
    return record


# -------------------------------------------------------------------------- #
# the dynamic dashboard: agentic values, and the mistake book
# -------------------------------------------------------------------------- #


def _dashboard_value_fits(value: Any) -> bool:
    """Whether one agentic value is small enough to retain.

    RE-APPLIED here rather than trusted from the writer, like every other ceiling
    in this file: this runs over bytes off a file the reader does not control, so a
    planted or damaged line is exactly the input that ignores the writer's clamp.

    Measured on the serialized form because that is what the ceiling is about, and
    an unserializable value is refused by the same test -- a value the fold cannot
    encode is one no reader can be handed.
    """
    try:
        blob = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return len(blob.encode("utf-8")) <= DASHBOARD_VALUE_BYTES


def _dashboard_type_holds(declared: str, value: Any) -> bool:
    """Whether *value* is of the manifest type *declared*.

    THE SAME FIVE TYPES the manifest declares (``manifest.FIELD_TYPES``), checked
    the same way the write path checks them. Two copies of one rule, and that is
    deliberate rather than an oversight: this module must not import
    ``dashboard_templates`` -- the manifest imports THIS one, for the fold names --
    so the shared helper cannot live there without closing the cycle. A test pins
    the two against one table of cases.

    A bool is NOT a number, which is the one case worth stating: Python says
    ``isinstance(True, int)``, so without the exclusion a crewmate writing ``true``
    into a ``number`` field would pass the check and the page would render ``True``
    where a count belongs.
    """
    if declared == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if declared == "string":
        return isinstance(value, str)
    if declared == "boolean":
        return isinstance(value, bool)
    if declared == "array":
        return isinstance(value, list)
    if declared == "object":
        return isinstance(value, dict)
    return False


def _agentic_start() -> dict[str, Any]:
    """One slot's agentic values: the latest per FIELD, and what was evicted.

    Keyed by field rather than holding one document, because the fields are
    independent cells: a crewmate that learns one number writes that one, and a
    whole-document fold would make it restate the other twenty-three to report it.
    That is the one place this fold parts company with ``panel``, whose entry
    replaces a whole published panel.
    """
    return {"fields": {}, "fields_omitted": 0, "wrote": 0}


def _agentic_step(state: dict[str, Any], entry: Entry) -> None:
    if entry.type != DASHBOARD_AGENTIC_ENTRY_TYPE:
        return
    data = entry.data
    name = _panel_text(data.get("field"), DASHBOARD_FIELD_LIMIT)
    declared = _panel_text(data.get("type"), DASHBOARD_TYPE_LIMIT)
    if not name or not declared:
        # A line naming no field or no type cannot be applied to a cell: a damaged
        # or planted entry, skipped rather than half-applied, for the reason
        # ``_panel_step`` skips one missing its template.
        return
    wrapper = data.get("value")
    # THROUGH THE WRAPPER's single key. The entry declares ``value`` as an object
    # because this registry has no any-type (see the entry type's own note); the
    # real value is ``v``. A line with no wrapper, or one carrying no ``v``, names
    # no value at all and is skipped like one naming no field.
    if not isinstance(wrapper, Mapping) or "v" not in wrapper:
        return
    value = wrapper["v"]
    # THE TYPE IS RE-CHECKED, not taken on the writer's word. The write path checked
    # it against the live manifest, but the entry is on disk and the reader is a
    # different process at a different time: a line whose declared type and value
    # disagree is exactly the corruption this re-check exists to drop, and applying
    # it would put a string in a cell a chart reads as a number.
    if not _dashboard_type_holds(declared, value) or not _dashboard_value_fits(value):
        return
    fields: dict[str, Any] = state["fields"]
    if name not in fields and len(fields) >= DASHBOARD_VALUE_LIMIT:
        # Bounded before the insert, and the LEAST RECENTLY WRITTEN field is what
        # goes: a slot that has seen many field sets keeps the ones being written
        # now. Ranked on the fold-order counter rather than on a stamp, because a
        # stamp is empty for an entry whose ``time`` no ``datetime`` can hold --
        # the same reason ``_panel_step`` ranks its owners that way.
        oldest = min(fields, key=lambda key: _as_int(fields[key].get("order")))
        del fields[oldest]
        state["fields_omitted"] = _as_int(state.get("fields_omitted")) + 1
    state["wrote"] = _as_int(state.get("wrote")) + 1
    fields[name] = {
        # UNWRAPPED for the reader. The wrapper is an artefact of what the entry
        # registry can declare, and a reader of this fold should not have to know
        # about it: a template's field is a value, not a value in a box.
        "value": value,
        "type": declared,
        "at": _panel_iso(entry.time),
        "instance_version": _as_int(data.get("instance_version")),
        "crew_key": _panel_text(data.get("crew_key"), PANEL_CREW_KEY_LIMIT),
        "order": state["wrote"],
    }


def _agentic_render(state: dict[str, Any]) -> dict[str, Any]:
    """The agentic values, one row per field.

    ``order`` is dropped: it is the fold's own eviction ranking and means nothing
    to a reader, who gets ``at`` for the same question. ``wrote`` counts ACCEPTED
    writes over the slot's whole life, which is what tells a reader "this crewmate
    is writing and the cell is still empty" from "this crewmate has never written".
    """
    fields: dict[str, Any] = state["fields"]
    return {
        "fields": {
            name: {key: row[key] for key in row if key != "order"} for name, row in fields.items()
        },
        "fields_omitted": _as_int(state.get("fields_omitted")),
        "wrote": _as_int(state.get("wrote")),
    }


def _agentic_rows(state: Mapping[str, Any]) -> int:
    """``agentic``: one row per field, each holding one capped value."""
    return _rows_in(state.get("fields"))


def _mistake_key(code: str, field: str) -> str:
    """The group key, ``(code, field)`` flattened.

    Flattened because fold state is JSON on disk and a tuple key is not, and
    separated by a character neither half can contain: a refusal code is a bare
    identifier and a field name matches ``manifest._FIELD``, so neither carries a
    space. Without that the two halves could be split wrongly on read-back and two
    different mistakes would land on one group.
    """
    return f"{code} {field}"


def _mistakes_start() -> dict[str, Any]:
    """The mistake book: one group per ``(code, field)``, plus what was evicted."""
    return {"groups": {}, "groups_omitted": 0, "refused": 0, "seen": 0}


def _mistakes_step(state: dict[str, Any], entry: Entry) -> None:
    if entry.type != DASHBOARD_REFUSED_ENTRY_TYPE:
        return
    data = entry.data
    code = _panel_text(data.get("code"), DASHBOARD_CODE_LIMIT)
    if not code:
        # A line that cannot say what it is cannot be applied to a group. The append
        # validator refuses one too (``code`` is required on the type), so reaching
        # here means a damaged or planted line -- skipped rather than counted, for
        # the reason ``_panel_step`` skips one missing its template.
        return
    field = _panel_text(data.get("field"), DASHBOARD_FIELD_LIMIT)
    stamp = _panel_iso(entry.time)
    corrects = data.get("corrects")
    # A CORRECTION, which an ACCEPTED write appends and a refusal never does: it is
    # not a refusal, it is the answer to one. It names the fields an earlier refusal
    # reached wrongly, and the fix is the field the accepted write actually used.
    #
    # Decided on the CODE, which is the one key always present, rather than on the
    # presence of ``corrects``: an earlier draft branched on the list and gave a
    # correction no code at all, which the append validator refused outright -- so
    # every correction was dropped at the door and the book stayed a list of errors
    # with no answers in it.
    if code == MISTAKE_CORRECTED_CODE:
        if not isinstance(corrects, list):
            return
        for raw in corrects:
            wrong = _panel_text(raw, DASHBOARD_FIELD_LIMIT)
            if not wrong:
                continue
            for key, group in state["groups"].items():
                if key.endswith(f" {wrong}"):
                    fixes: list[str] = group.setdefault("corrected_to", [])
                    if field and field not in fixes:
                        fixes.append(field)
                        # NEWEST KEPT, oldest dropped: the useful answer is the
                        # latest one, and a long list would make the reader choose
                        # -- which is the problem this fold exists to remove.
                        del fixes[:-MISTAKE_CORRECTION_LIMIT]
                    group["corrected_at"] = stamp
        return
    state["refused"] = _as_int(state.get("refused")) + 1
    key = _mistake_key(code, field)
    groups: dict[str, Any] = state["groups"]
    group = groups.get(key)
    if group is None:
        if len(groups) >= MISTAKE_GROUP_LIMIT:
            # Least recently SEEN goes, so the groups kept are the mistakes still
            # being made rather than the first ones ever made.
            oldest = min(groups, key=lambda other: _as_int(groups[other].get("order")))
            del groups[oldest]
            state["groups_omitted"] = _as_int(state.get("groups_omitted")) + 1
        group = {
            "code": code,
            "field": field,
            "count": 0,
            "first_seen": stamp,
            "reason": "",
            "corrected_to": [],
            "corrected_at": "",
        }
        groups[key] = group
    state["seen"] = _as_int(state.get("seen")) + 1
    group["count"] = _as_int(group.get("count")) + 1
    group["last_seen"] = stamp
    group["order"] = state["seen"]
    # The LATEST wording wins. The sentence is what the agent was told, so an
    # updated refusal message should be what it reads back -- handing it the first
    # wording would teach a correction the gateway has since improved.
    group["reason"] = _panel_text(data.get("reason"), DASHBOARD_REASON_LIMIT)
    group["crew_key"] = _panel_text(data.get("crew_key"), PANEL_CREW_KEY_LIMIT)


def _mistakes_render(state: dict[str, Any]) -> dict[str, Any]:
    """The mistake book, worst group first.

    Ordered by COUNT rather than by recency, because the reader is an agent with
    one short list to read before it writes: a mistake made five times is the one
    most worth not repeating, and recency is already on each row as ``last_seen``.
    """
    groups: list[dict[str, Any]] = [
        {key: value for key, value in group.items() if key != "order"}
        for group in state["groups"].values()
    ]
    groups.sort(key=lambda group: (-_as_int(group.get("count")), str(group.get("field"))))
    return {
        "groups": groups,
        "groups_omitted": _as_int(state.get("groups_omitted")),
        "refused": _as_int(state.get("refused")),
    }


def _mistakes_rows(state: Mapping[str, Any]) -> int:
    """``mistakes``: one row per group, each a small fixed record."""
    return _rows_in(state.get("groups"))
