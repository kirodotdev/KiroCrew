"""How the XR room reads a Bot's 3D dashboard layout, in Python.

The room's own reader is ``readLayout`` in ``website/src/pages/xr/workspace/dash3d.ts``.
This module reads the same document by the same rules, so the gateway says how a write
READS (never that it is drawn: the room may be shut). It runs nothing and fetches
nothing. Both readers are held to one file of cases,
``test/fixtures/xr_layout_cases.json``: ``dash3d.test.ts`` reads it against
``readLayout`` and ``test/test_dashboard_xr_layout.py`` against :func:`read_layout`.

:func:`verdict` is the line the write's reply carries for a Bot that writes its layout
(``agent_panel_dashboard.api_dashboard_write``, D103 5), and :func:`field_verdict` the
line for a write of a number that a part reads (D169). Both are made of FIXED words
only: the room's own lines for what is wrong (:data:`WHY_LINES`, the tab's English
catalogue word for word), a part's place number, its kind from :data:`DASH_KINDS`, and
what it shows now (:func:`part_says`): numbers the page's fields hold, a unit from
:data:`DASH_UNITS`, and words of :data:`SAYS`. They repeat none of the Bot's text, so a
name, a field's name or a text value the Bot wrote cannot come back through them.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

#: The field of a Bot's Dashboard that holds its layout (``DASH_FIELD``).
DASH_FIELD = "xr_layout"
DASH_KINDS = ("number", "bar", "ring", "lamps")
DASH_UNITS = ("%", "$", "ms", "/s")
VERSION = 1
MOST_PARTS = 6
NAME_MAX = 16
MARKS_MOST = 2
PATH_KEYS = 4
KEY_MAX = 64
#: The raw name and the raw path are cut by their length before any work, in code points,
#: as the tab does (``DASH.nameRawMax``, ``DASH.pathRawMax``, M74).
NAME_RAW_MAX = 256
PATH_RAW_MAX = 259

_FIELD = re.compile(r"[a-z][a-z0-9_]{0,63}")
_KEY = re.compile(r"[A-Za-z0-9_]{1,%d}" % KEY_MAX)
#: The room's own state glyphs, which a name never carries (``text.ts`` ``STATE_GLYPHS``).
_STATE_GLYPHS = re.compile(
    "[\u25cf\u25cb\u25c9\u2713\u2714\u2715\u2716\u26a0\u23f3\U0001f7e0-\U0001f7eb\u2b24]"
)


@dataclass(frozen=True)
class Part:
    kind: str
    name: str
    field: str
    path: tuple[str, ...]
    #: A bar's or a ring's limit: a number, or the name of the field that holds it.
    max: float | str | None
    key: str
    unit: str
    #: False when the name holds a sign this table does not know (:func:`_unknown`): the
    #: tab may count it otherwise, so whether the room draws the name is not known here.
    name_checked: bool = True


@dataclass(frozen=True)
class Socket:
    """A part that is wrong, drawn as an empty socket; ``broken`` says why (``DashWhy``)."""

    broken: str


@dataclass(frozen=True)
class Layout:
    parts: tuple[Part | Socket, ...]
    #: Why the whole document is no layout; "" when it is one.
    why: str


def _is_record(v: Any) -> bool:
    return isinstance(v, dict)


def _is_number(v: Any) -> bool:
    # JSON true is no number here, as in the tab.
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _unseen(c: str) -> bool:
    return unicodedata.category(c) in ("Cc", "Cf")


def _mark(c: str) -> bool:
    return unicodedata.category(c).startswith("M")


def _unknown(c: str) -> bool:
    """A sign this table does not know (``Cn``). The tab's table is newer (node 22:
    Unicode 17; Python 3.12: 15.0), so such a sign may be a letter there (an emoji, a
    Garay letter, a sign of Extension J) or a mark (U+0897). It counts as a letter here,
    as the tab counts an unassigned sign, and its part's name is not checked (M76)."""
    return unicodedata.category(c) == "Cn"


def clean_name(v: Any) -> str | None:
    """A name as the room draws it: no control or format character, none of the room's
    glyphs, no mark with no letter under it, at most ``MARKS_MOST`` marks on a letter."""
    if not isinstance(v, str):
        return None
    text = _STATE_GLYPHS.sub("", "".join(c for c in v if not _unseen(c)))
    out = []
    marks = MARKS_MOST
    for c in unicodedata.normalize("NFD", text).strip():
        if not _mark(c):
            out.append(c)
            marks = 0
        elif marks < MARKS_MOST:
            out.append(c)
            marks += 1
    return unicodedata.normalize("NFC", "".join(out))


def letters(s: str) -> int:
    """How many letters a name has: the marks on a letter are part of it."""
    return sum(1 for c in unicodedata.normalize("NFD", s) if not _mark(c))


def read_part(raw: Any) -> Part | Socket:
    if not _is_record(raw):
        return Socket("part")
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in DASH_KINDS:
        return Socket("kind")
    raw_name = raw.get("name")
    if isinstance(raw_name, str) and len(raw_name) > NAME_RAW_MAX:
        return Socket("name")
    name = clean_name(raw_name)
    unknown = sum(1 for c in unicodedata.normalize("NFD", name or "") if _unknown(c))
    # The letters this table knows decide alone only when they are too many already, or
    # when there is no unknown sign; with one the name is read and left unchecked.
    known = letters(name or "") - unknown
    if not name or known > NAME_MAX or (unknown == 0 and known == 0):
        return Socket("name")
    field = raw.get("field")
    if not isinstance(field, str) or not _FIELD.fullmatch(field):
        return Socket("field")
    raw_path = raw.get("path")
    if isinstance(raw_path, str) and len(raw_path) > PATH_RAW_MAX:
        return Socket("path")
    path: list[str] | None
    if raw_path is None and "path" not in raw:
        path = []
    elif isinstance(raw_path, str):
        path = raw_path.split(".")
    else:
        path = None
    if path is None or len(path) > PATH_KEYS or not all(_KEY.fullmatch(k) for k in path):
        return Socket("path")
    limit: float | str | None = None
    if kind in ("bar", "ring"):
        m = raw.get("max")
        if _is_number(m):
            # Finite AS A FLOAT, as the tab reads a JSON number: a whole number of 400
            # digits is Infinity there, so it is a bad limit here too.
            try:
                as_float = float(m)
            except OverflowError:
                return Socket("max")
            if not (math.isfinite(as_float) and as_float > 0):
                return Socket("max")
        elif not (isinstance(m, str) and _FIELD.fullmatch(m)):
            return Socket("max")
        limit = m
    # A lamp's key only: on another kind it is read by nothing.
    raw_key = raw.get("key") if kind == "lamps" else None
    if (
        kind == "lamps"
        and "key" in raw
        and not (isinstance(raw_key, str) and _KEY.fullmatch(raw_key))
    ):
        return Socket("key")
    raw_unit = raw.get("unit")
    unit = raw_unit if isinstance(raw_unit, str) and raw_unit in DASH_UNITS else ""
    return Part(
        kind=kind,
        name=name,
        field=field,
        path=tuple(path),
        max=limit,
        key=raw_key if isinstance(raw_key, str) else "state",
        unit=unit,
        name_checked=unknown == 0,
    )


def read_layout(raw: Any) -> Layout:
    """A layout out of whatever the field holds. Never raises; what is wrong is named."""
    if not _is_record(raw):
        return Layout((), "not_layout")
    v = raw.get("v")
    if not _is_number(v) or v != VERSION:
        return Layout((), "version")
    parts = raw.get("parts")
    if not isinstance(parts, list):
        return Layout((), "not_layout")
    if not parts:
        return Layout((), "no_parts")
    if len(parts) > MOST_PARTS:
        return Layout((), "too_many")
    return Layout(tuple(read_part(p) for p in parts), "")


#: The room's line for each thing that is wrong, as the taken form writes it on its lip
#: (``xr.dash3d_why_*`` in ``website/src/i18n/locales/en.json``; a test holds the two equal).
WHY_LINES: dict[str, str] = {
    "not_layout": "layout: not a layout",
    "version": "layout: unknown version",
    "no_parts": "layout: no parts",
    "too_many": "layout: too many parts",
    "part": "layout: not a part",
    "kind": "layout: unknown part",
    "name": "layout: bad name",
    "field": "layout: bad field",
    "path": "layout: bad path",
    "max": "layout: bad limit",
    "key": "layout: bad key",
}


#: The words of the verdict line: a CLOSED list (REVIEW-41 5.3). One line per reason
#: (:data:`WHY_LINES`), one word per kind, and these three. A part is named by its
#: place (1 to :data:`MOST_PARTS`), never by its name, field or number.
KIND_WORDS: dict[str, str] = {k: k for k in DASH_KINDS}
NO_DASHBOARD = "In the XR room: no 3D dashboard."
#: A part whose name holds a sign this table does not know: never said to be good (M76).
NAME_NOT_CHECKED = "name not checked"
GREY_TRAY = "In the XR room this layout is an empty grey tray:"
READS = "In the XR room this layout reads:"


def _part_word(p: Part | Socket) -> str:
    if isinstance(p, Socket):
        return WHY_LINES[p.broken]
    return KIND_WORDS[p.kind] if p.name_checked else NAME_NOT_CHECKED


def room_layout(raw: Any) -> Layout | None:
    """What the room makes of the field: nothing for no value or an empty object (a
    cleared layout, ``useDash3d.ts`` ``layoutOf``), else :func:`read_layout`."""
    if raw is None or (isinstance(raw, dict) and not raw):
        return None
    return read_layout(raw)


#: The lamp states the room draws in a colour of their own (``LAMP_STATES``); any other
#: word is grey.
LAMP_STATES = ("idle", "working", "done", "failed")
MOST_LAMPS = 12
#: The words that say what a part shows (``DASH_SAYS`` in ``dash3d.ts``, word for word;
#: the cases file holds the two readers to the same lines).
SAYS: dict[str, str] = {
    "no_value": "a dash: its field holds no value",
    "no_path": "a dash: its path finds nothing",
    "not_list": "a dash: not a list",
    "not_number": "a dash: not a number",
    "no_limit": "a dash: its limit is no number above 0",
    "big": "a number of more than 12 digits",
    "over": "over its limit",
    "under": "under 0",
    "drawn": "drawn",
    "grey": "grey",
}
#: A line for one written field names its parts with this head.
SHOWS = "In the XR room:"

#: The signs that count as a space round a plain number, written out (``PLAIN_SPACES`` in
#: ``glance.ts`` holds the same list; a unit in each holds it to the cases file). Not
#: ``\s``: the two languages do not agree on it (the tab's takes U+FEFF, Python's takes
#: U+001C to U+001F and U+0085). These are the signs the tab took, since the room is what
#: he sees.
PLAIN_SPACES: tuple[int, ...] = (
    0x09,
    0x0A,
    0x0B,
    0x0C,
    0x0D,
    0x20,
    0xA0,
    0x1680,
    *range(0x2000, 0x200B),
    0x2028,
    0x2029,
    0x202F,
    0x205F,
    0x3000,
    0xFEFF,
)
_SPACES = "".join(map(chr, PLAIN_SPACES))
#: The number itself, with no space in it: digits, and groups of a point or a comma and digits.
_PLAIN_CORE = re.compile(r"-?[0-9]+(?:[.,][0-9]+)*")
#: A number written with thousands commas: one to three digits, then groups of a comma
#: and exactly three digits, then at most one point and digits. Every comma goes out.
_GROUPED = re.compile(r"-?[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?")
#: For every other text: a comma before three digits goes out, and matches do not
#: overlap, so of two such commas in a row only the first does.
_THOUSANDS = re.compile(r"([0-9]),([0-9]{3})")


def _plain_number(text: str) -> float | None:
    """A text that is a plain number, as the tab reads one (``plainNumber`` in ``glance.ts``).

    Spaces before it and after it, one percent sign after it, and spaces round that sign.
    The spaces are cut off from each end once, so the time is the text's length whatever
    it holds: a pattern that tried each way to split a run of spaces round the sign took
    the square of it (M175: 228 ms for one text of 8 kB).

    Commas: a text whose commas ALL stand before groups of exactly three digits, after
    a first group of one to three, is a number with thousands commas, and they all go
    out ("12,345,678.9" is 12345678.9). In any other text a comma before three digits
    goes out and the first comma left is the decimal point ("1,23" is 1.23).
    """
    core = text.strip(_SPACES)
    if core.endswith("%"):
        core = core[:-1].rstrip(_SPACES)
    if not _PLAIN_CORE.fullmatch(core):
        return None
    if _GROUPED.fullmatch(core):
        # "1,234,567" is 1234567: all its commas stand before groups of three.
        bare = core.replace(",", "")
    else:
        bare = _THOUSANDS.sub(r"\1\2", core).replace(",", ".", 1)
    try:
        n = float(bare)
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def _number_of(v: Any) -> float | None:
    """A reading as a number: a finite number, or text that is a plain number (``numberOf``)."""
    if _is_number(v):
        try:
            n = float(v)
        except OverflowError:
            return None
        return n if math.isfinite(n) else None
    return _plain_number(v) if isinstance(v, str) else None


def say_number(n: float) -> str:
    """A number in plain signs, as ``sayNumber`` writes it: whole when it is whole, else
    two places after the point, a half rounded away from 0, no zeros at its end."""
    if abs(n) >= 1e12:
        return SAYS["big"]
    s = str(Decimal(n).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    s = s.rstrip("0").rstrip(".") if "." in s else s
    return "0" if s in ("-0", "") else s


def part_says(part: Part, fields: Mapping[str, Any] | None) -> str:
    """What a part shows now, in fixed words (``partSays`` in ``dash3d.ts``).

    Its kind, then the number it reads, or a dash and why. Made of the kind, the unit
    (one of :data:`DASH_UNITS`), numbers, and words of :data:`SAYS` and
    :data:`LAMP_STATES`: never the part's name and never a text that a field holds, so
    nothing the Bot wrote as text comes back through it. Never raises.
    """

    def dash(why: str) -> str:
        return f"{part.kind}, {SAYS[why]}"

    if not isinstance(fields, Mapping) or fields.get(part.field) is None:
        return dash("no_value")
    v: Any = fields[part.field]
    for k in part.path:
        if not _is_record(v) or k not in v:
            return dash("no_path")
        v = v[k]
    if part.kind == "lamps":
        if not isinstance(v, list):
            return dash("not_list")
        drawn = v[:MOST_LAMPS]
        states = [(row.get(part.key) if _is_record(row) else row) for row in drawn]
        known = [s if isinstance(s, str) and s in LAMP_STATES else SAYS["grey"] for s in states]
        each = [
            f"{known.count(word)} {word}"
            for word in (*LAMP_STATES, SAYS["grey"])
            if known.count(word)
        ]
        more = f", {len(drawn)} {SAYS['drawn']}" if len(v) > len(drawn) else ""
        return f"lamps {len(v)}{more}" + (f" ({', '.join(each)})" if each else "")
    n = _number_of(v)
    if n is None:
        return dash("not_number")
    unit = f" {part.unit}" if part.unit else ""
    if part.kind == "number":
        return f"number {say_number(n)}{unit}"
    limit = (
        _number_of(part.max)
        if _is_number(part.max)
        else _number_of(fields.get(part.max)) if isinstance(part.max, str) else None
    )
    if limit is None or not limit > 0:
        return dash("no_limit")
    past = f", {SAYS['over']}" if n > limit else f", {SAYS['under']}" if n < 0 else ""
    if part.kind == "ring":
        return f"ring {say_number(n)}/{say_number(limit)}{past}"
    return f"bar {say_number(n)}{unit} of {say_number(limit)}{past}"


def _place_word(p: Part | Socket, fields: Mapping[str, Any] | None) -> str:
    """One part's words in the line: its reason, or what it shows when the values are known."""
    if isinstance(p, Socket) or fields is None:
        return _part_word(p)
    says = part_says(p, fields)
    return says if p.name_checked else f"{NAME_NOT_CHECKED}, {says}"


def verdict(raw: Any, fields: Mapping[str, Any] | None = None) -> str:
    """The one line the write's reply carries: what the room will make of the layout.

    A cleared layout (``{}``) and ``null`` say "no 3D dashboard", as the room draws none
    (``layoutOf``). A wrong layout is answered, never refused (D99). Bounded: at most
    :data:`MOST_PARTS` parts are read, and a longer list is one line.

    With *fields* (the page's values by name) each good part also says what it shows
    now (:func:`part_says`); without them (the values could not be read) its kind only.
    """
    layout = room_layout(raw)
    if layout is None:
        return NO_DASHBOARD
    if layout.why:
        return f"{GREY_TRAY} {WHY_LINES[layout.why]}."
    each = [f"part {i}: {_place_word(p, fields)}" for i, p in enumerate(layout.parts, start=1)]
    return READS + " " + "; ".join(each) + "."


def field_verdict(field: str, fields: Mapping[str, Any] | None) -> str:
    """The line for a write of one number: what the parts that read *field* show now.

    "" when the page's values hold no layout, the layout is no layout, or no good part
    reads the field (as its own field or as its limit's): the reply then says nothing of
    the room. A wrong value is answered here too, never refused.
    """
    if not isinstance(fields, Mapping):
        return ""
    layout = room_layout(fields.get(DASH_FIELD))
    # A document that is no layout has no parts: nothing reads the field.
    if layout is None:
        return ""
    each = [
        f"part {i}: {_place_word(p, fields)}"
        for i, p in enumerate(layout.parts, start=1)
        if isinstance(p, Part) and field in (p.field, p.max if isinstance(p.max, str) else None)
    ]
    return SHOWS + " " + "; ".join(each) + "." if each else ""


__all__ = [
    "DASH_FIELD",
    "Layout",
    "Part",
    "Socket",
    "GREY_TRAY",
    "KIND_WORDS",
    "LAMP_STATES",
    "NAME_NOT_CHECKED",
    "NO_DASHBOARD",
    "READS",
    "SAYS",
    "SHOWS",
    "WHY_LINES",
    "clean_name",
    "field_verdict",
    "letters",
    "part_says",
    "read_layout",
    "room_layout",
    "say_number",
    "verdict",
]
