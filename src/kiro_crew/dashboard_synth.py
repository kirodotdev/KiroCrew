"""A synthetic copy of a dashboard read: the record's shape, none of its content.

An agent-written page is checked before any person has approved it, and the dynamic
dashboard RFC says a page nobody approved never receives a value. So the check never
gives it one. The real read is taken once, inside the gateway process, only to be
copied: :func:`synthesize` returns a new read of the same shape in which every free value
is invented, and that copy is all a browser is ever handed.

What the copy keeps, so the page takes the same paths through its code as on the record:

* every field the package declares, every list's length, every ``null`` and every absent
  field, and the keys of an object that the dashboard type catalog lists as its own;
* every other key that the page itself names, as a word or in quotes, unless it is an id:
  a page can only branch on a key it spells out, and the keys of the values the owner and
  the publishing crew wrote are their own words. A page can therefore confirm that a key it
  guessed exists in such a value; it cannot learn one it did not spell. In a list of a
  fold's rows only a row's own keys are kept this way: the catalog does not describe rows,
  and some hold a map keyed by data, so the keys of an object nested in a row are invented;
* each value's type, and a boolean's value;
* a string's length, except that a link shorter than ``https://example.invalid/`` takes
  that length, and the kinds of its characters: ASCII letters by case, digits, CJK and
  the other scripts, spaces, line breaks and punctuation;
* a number's sign, its order of magnitude and whether it is whole; ``0`` stays ``0``;
* the formats of dates and ids: an ISO timestamp stays a valid ISO timestamp in the same
  form, ``it_`` and eight hex digits stay that shape, a UUID stays a UUID, and a link
  becomes ``https://example.invalid/...``;
* the words of a closed vocabulary that the crew-log entry schema declares (state, status,
  verdict, kind ...), wherever a value is one of them;
* which values were equal and which were not: one real value always becomes the same
  invented value, and different ones get different ones (the numbers of one size are shared
  out without a repeat, a text another text already has is drawn again), so a page that
  joins rows on an id still joins them.

Everything else is replaced: free text, numbers, ids, times and links, an ``enum`` field's
value (it shows one of the labels the package declares, drawn at random), every key the
page does not name, and always the keys of a map the fold keys by a runtime id (a model,
a tool, a subagent).

The generator is seeded from the operating system's entropy on every call, never from a
value of the record. A replacement is drawn WITHOUT looking at the value it replaces, or at
any other value of the record: excluding the real letter, digit or number would leave it as
the one thing never shown, and a page that is saved again and again would find it. So a
draw equals the real value at the rate it equals any other value of its kind, which for
anything longer than a few characters is never.

Two linear walks over a read that is already bounded: one to count it, and one to build
the copy. A read past :data:`MAX_NODES` nodes, :data:`MAX_CHARS` characters or
:data:`MAX_DEPTH` levels is refused rather than copied, so the work this does is bounded
too.
"""

from __future__ import annotations

import datetime as _dt
import functools
import math
import os
import random
import re
import unicodedata
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Final, NamedTuple

__all__ = [
    "MAX_CHARS",
    "MAX_DEPTH",
    "MAX_NODES",
    "SynthesisRefused",
    "fixed_vocabulary",
    "names_in",
    "synthesize",
]

#: Values (and keys) a read may hold before it is too large to copy here.
MAX_NODES: Final[int] = 200_000

#: Characters of text, over all strings and keys, a read may hold.
MAX_CHARS: Final[int] = 2_000_000

#: Levels of nesting a read may have.
MAX_DEPTH: Final[int] = 32

#: Tries at a replacement string that is not already another value's, before the last one is
#: taken; and the tries for a string so short that few replacements exist.
_TRIES: Final[int] = 8
_SHORT_TRIES: Final[int] = 64

#: The keys of a read that name fields or carry a flag or a tag from a closed set, and so
#: are structure. Every other key of a read is walked as a value, so a key added later
#: cannot carry a real value through.
_STRUCTURE_KEYS: Final[frozenset[str]] = frozenset(
    {"agentic", "missing", "stale", "locale", "subject"}
)

#: Where a value sits, which decides how its keys are treated. A fold's own object; a row (an
#: element of a list in a fold); anything nested inside a row, which the catalog does not
#: describe and some of which is keyed by data; a document the owner or the publishing crew
#: wrote.
_FOLD, _ROW, _NESTED, _OWNER = range(4)

#: The context of what is inside a value of each context: of a mapping's values, and of a
#: list's elements.
_BELOW_MAPPING: Final[tuple[int, ...]] = (_FOLD, _NESTED, _NESTED, _OWNER)
_BELOW_LIST: Final[tuple[int, ...]] = (_ROW, _NESTED, _NESTED, _OWNER)

_LINK_PREFIX: Final[str] = "https://example.invalid/"

_LOWER: Final[str] = "abcdefghijklmnopqrstuvwxyz"
_UPPER: Final[str] = _LOWER.upper()
_DIGITS: Final[str] = "0123456789"
_LOWER_HEX: Final[str] = "0123456789abcdef"
_UPPER_HEX: Final[str] = "0123456789ABCDEF"

#: An ASCII letter or digit -> the alphabet it is replaced from.
_ALPHABET_OF: Final[dict[str, str]] = {
    ch: alphabet for alphabet in (_LOWER, _UPPER, _DIGITS) for ch in alphabet
}

#: Scripts whose letters are replaced by letters of the same script, as inclusive code
#: point ranges.
_SCRIPTS: Final[tuple[tuple[int, int], ...]] = (
    (0x4E00, 0x9FA5),  # CJK ideographs
    (0x3041, 0x3096),  # Hiragana
    (0x30A1, 0x30FA),  # Katakana
    (0xAC00, 0xD7A3),  # Hangul syllables
    (0x0410, 0x042F),  # Cyrillic capitals
    (0x0430, 0x044F),  # Cyrillic small letters
    (0x0391, 0x03A1),  # Greek capitals
    (0x03B1, 0x03C9),  # Greek small letters
    (0x00C0, 0x00D6),  # Latin-1 capitals
    (0x00E0, 0x00F6),  # Latin-1 small letters
)

#: What a symbol that is not ASCII (a star, an emoji) is replaced with: one of its own
#: width, so a copy keeps the symbol and not the record's choice of it.
_SYMBOLS: Final[str] = "".join(
    chr(code) for code in range(0x2600, 0x2640) if unicodedata.category(chr(code)) == "So"
)
_PICTOGRAPHS: Final[str] = "".join(chr(code) for code in range(0x1F600, 0x1F640))
#: What a combining mark is replaced with.
_MARKS: Final[str] = "\u0300\u0301\u0302\u0303\u0304\u0306\u0307\u0308\u030a\u030c"


_URL: Final[re.Pattern[str]] = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://\S*\Z")
_UUID_TEXT: Final[str] = (
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_UUID: Final[re.Pattern[str]] = re.compile(_UUID_TEXT + r"\Z")

#: A key that is an id of the runtime's (a work item, a UUID, a digest, a number) and is data
#: wherever it is found, even when a page names it.
_ID_KEY: Final[re.Pattern[str]] = re.compile(
    r"(?:it_[0-9a-f]{8}|" + _UUID_TEXT + r"|[0-9a-f]{7,}|[0-9]+)\Z", re.I
)
_ISO: Final[re.Pattern[str]] = re.compile(
    r"(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})"
    r"(?:(?P<sep>[Tt ])(?P<h>\d{2}):(?P<mi>\d{2})"
    r"(?::(?P<s>\d{2})(?:(?P<fs>[.,])(?P<f>\d{1,9}))?)?"
    r"(?P<tz>[Zz]|[+-]\d{2}(?::?\d{2})?)?)?\Z"
)
#: Words and quoted strings a page spells out, and how many of each are kept.
_WORD: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_QUOTED: Final[re.Pattern[str]] = re.compile(
    r"""'([^'\\\n]{1,80})'|"([^"\\\n]{1,80})"|`([^`\\\n]{1,80})`"""
)
_MAX_NAMES: Final[int] = 200_000

#: A work item's id, the one id the folds mint with a fixed word in front of it: ``it_`` and
#: eight lowercase hex digits (``work_ledger.mint_item_id``). Any other word in front of a
#: separator is the record's own text, so it is replaced with the rest of the string.
_ITEM_ID: Final[re.Pattern[str]] = re.compile(r"it_[0-9a-f]{8}\Z")
_HEX: Final[re.Pattern[str]] = re.compile(r"[0-9a-fA-F]{7,}\Z")
_WHOLE: Final[re.Pattern[str]] = re.compile(r"[0-9]+\Z")

#: How far back from now a replacement timestamp may reach.
_TIMESTAMP_SPREAD_SECONDS: Final[float] = 365 * 24 * 3600.0


class SynthesisRefused(Exception):
    """The read is too large, too deep or not an object, so no copy was made."""


class _Field(NamedTuple):
    """What the package says about one field: its labels, if it is an ``enum``, and its
    place in a fold."""

    choices: tuple[str, ...]
    shape: Mapping[str, Any] | None
    #: Whether the value is a document the owner or the publishing crew wrote.
    owner: bool = False


#: Where the catalog has a map keyed by a name the owner or the publishing crew chose: a
#: crew's published panel document and the owner's own agentic fields.
_OWNER_KEYED_PATHS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("panel", ("data",)),
    ("agentic", ("fields",)),
)


@functools.lru_cache(maxsize=1)
def fixed_vocabulary() -> frozenset[str]:
    """The words of the closed vocabularies the crew-log entry schema declares.

    Every enum the schema marks closed, nested fields included (an item's state, a
    verdict, a worker's status, a card's outcome ...), taken from the schema itself so a
    value added to one appears here with nothing edited. A rendered fold value that is one
    of these words is kept, because a page branches on it.
    """
    from kiro_crew.crew_log.entry_types import ENTRY_TYPES

    words: set[str] = set()

    def walk(fields: Any) -> None:
        for field in fields:
            if field.enum and field.enum_closed:
                words.update(field.enum)
            if field.fields:
                walk(field.fields)

    for by_type in ENTRY_TYPES.values():
        for entry in by_type.values():
            walk(entry.fields)
    return frozenset(words)


@functools.lru_cache(maxsize=1)
def _fold_shapes() -> Mapping[str, Mapping[str, Any]]:
    """Each fold's shape tree, as the dashboard type catalog describes it."""
    from kiro_crew import dashboard_types

    return {entry.name: entry.shape for entry in dashboard_types.catalog()}


def _shape_at(source: Any) -> Mapping[str, Any] | None:
    """The shape node a field's ``{'fold', 'path'}`` source points at, if it is known."""
    return _walk_source(source)[0]


def _walk_source(source: Any) -> tuple[Mapping[str, Any] | None, bool]:
    """``(shape node, owner)`` for a field's source: the node it points at when the catalog
    knows it, and whether the value is a document the owner or the publishing crew wrote (an
    agentic field, or anything at or below a map they keyed)."""
    if not isinstance(source, Mapping):
        return None, False
    if source.get("agentic"):
        return None, True
    fold, path = source.get("fold"), source.get("path")
    if not isinstance(fold, str) or not isinstance(path, str):
        return None, False
    node: Mapping[str, Any] | None = _fold_shapes().get(fold)
    for step in path.split("."):
        if node is not None and any(node is owned for owned in _owner_keyed()):
            return None, True
        properties = node.get("properties") if node else None
        node = properties.get(step) if isinstance(properties, Mapping) else None
        if node is None:
            return None, False
    return node, any(node is owned for owned in _owner_keyed())


def _fields_of(model: Any) -> dict[str, _Field]:
    types = model.get("types") if isinstance(model, Mapping) else None
    if not isinstance(types, Mapping):
        return {}
    out: dict[str, _Field] = {}
    for name, spec in types.items():
        if not isinstance(name, str) or not isinstance(spec, Mapping):
            continue
        choices: tuple[str, ...] = ()
        if spec.get("type") == "enum" and isinstance(spec.get("choices"), (list, tuple)):
            choices = tuple(c for c in spec["choices"] if isinstance(c, str))
        shape, owner = _walk_source(spec.get("source"))
        out[name] = _Field(choices, shape, owner)
    return out


def names_in(page: str) -> frozenset[str]:
    """The keys a page can branch on: every word it writes and every short string it quotes.

    A page reaches a key of the data only by spelling it (``row.title``, ``row["title"]``,
    ``"title"`` in a list it loops over), so the copy keeps exactly these keys and invents
    the rest. Bounded by :data:`_MAX_NAMES`, in one pass over the page.
    """
    names: set[str] = set()
    for found in _WORD.finditer(page):
        names.add(found.group())
        if len(names) >= _MAX_NAMES:
            return frozenset(names)
    for quoted in _QUOTED.finditer(page):
        names.add(next(group for group in quoted.groups() if group is not None))
        if len(names) >= _MAX_NAMES:
            break
    return frozenset(names)


@functools.lru_cache(maxsize=1)
def _owner_keyed() -> tuple[Mapping[str, Any], ...]:
    """The catalog's opaque nodes whose keys the owner or the publishing crew chose.

    Their keys are that agent's own words and the page is that agent's own page, so they
    follow the same rule as any key the page names. Every other opaque node is keyed by
    something the runtime minted (a model, a tool, a subagent) and is always invented.
    """
    shapes = _fold_shapes()
    nodes = []
    for fold, path in _OWNER_KEYED_PATHS:
        node: Any = shapes.get(fold)
        for step in path:
            node = (node or {}).get("properties", {}).get(step)
        if isinstance(node, Mapping) and node.get("opaque"):
            nodes.append(node)
    return tuple(nodes)


def _fresh_rng() -> random.Random:
    """A generator seeded from the operating system, never from a value of the record."""
    return random.Random(int.from_bytes(os.urandom(32), "big"))


def _block_of(code: int) -> tuple[int, int] | None:
    for low, high in _SCRIPTS:
        if low <= code <= high:
            return low, high
    return None


class _Synth:
    """One copy: the memory of what was replaced by what, and the walks."""

    def __init__(
        self,
        rng: random.Random,
        vocabulary: frozenset[str],
        fields: dict[str, _Field],
        now: _dt.datetime,
        names: frozenset[str],
    ) -> None:
        self._rng = rng
        self._now = now
        self._names = names | frozenset(fields)
        self._rnd = rng.random
        self._vocabulary = vocabulary
        self._fields = fields
        self._strings: dict[str, str] = {}
        self._numbers: dict[int | float, int | float] = {}
        self._used_strings: set[str] = set()
        #: Per kind of number, how many replacements were given out and which slots the
        #: lazy shuffle has moved (see :meth:`_draw_index`).
        self._given_count: dict[tuple[Any, ...], int] = {}
        self._given_moved: dict[tuple[Any, ...], dict[int, int]] = {}

    # -- the first walk: how large it is -----------------------------------------------

    def survey(self, read: Mapping[str, Any]) -> None:
        """Count the read, and refuse one that is too large."""
        nodes = chars = 0
        stack: list[tuple[Any, int]] = [(read, 0)]
        while stack:
            value, depth = stack.pop()
            nodes += 1
            if nodes > MAX_NODES:
                raise SynthesisRefused("the read has too many values to copy")
            if depth > MAX_DEPTH:
                raise SynthesisRefused("the read is nested too deeply to copy")
            if isinstance(value, str):
                chars += len(value)
            elif isinstance(value, Mapping):
                for key, inner in value.items():
                    if isinstance(key, str):
                        chars += len(key)
                    stack.append((inner, depth + 1))
            elif isinstance(value, (list, tuple)):
                stack.extend((inner, depth + 1) for inner in value)
            if chars > MAX_CHARS:
                raise SynthesisRefused("the read holds too much text to copy")

    # -- the second walk: the copy -----------------------------------------------------

    def copy(self, read: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        top = read.get("fields")
        if isinstance(top, Mapping):
            # The fields the read carries are the model's own names (the manifest grammar
            # admits nothing else), and ``written_at`` is keyed by the same names.
            self._names = self._names | frozenset(k for k in top if isinstance(k, str))
        for key, value in read.items():
            if key in _STRUCTURE_KEYS:
                out[key] = _clone(value)
            elif key == "fields" and isinstance(value, Mapping):
                out[key] = {name: self._field(name, inner) for name, inner in value.items()}
            else:
                out[key] = self._value(value, None, _FOLD)
        return out

    def _field(self, name: Any, value: Any) -> Any:
        spec = self._fields.get(name) if isinstance(name, str) else None
        if spec is None:
            return self._value(value, None, _FOLD)
        if spec.choices and isinstance(value, str) and value:
            # One of the labels the package declares, drawn without looking at the value:
            # keeping the value when the package lists it would let a package guess it.
            return spec.choices[int(self._rnd() * len(spec.choices))]
        return self._value(value, spec.shape, _OWNER if spec.owner else _FOLD)

    def _value(self, value: Any, shape: Mapping[str, Any] | None, context: int) -> Any:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, str):
            return self._string(value)
        if isinstance(value, int):
            return self._integer(value)
        if isinstance(value, float):
            return self._real(value)
        if isinstance(value, Mapping):
            return self._mapping(value, shape, context)
        if isinstance(value, (list, tuple)):
            below = _BELOW_LIST[context]
            return [self._value(inner, None, below) for inner in value]
        # A type JSON cannot carry has no shape to keep.
        return None

    def _key(self, key: Any, properties: Any, runtime_keyed: bool) -> Any:
        """The key a copy carries for *key*: kept when it is structure, invented when it is
        the record's own text."""
        if not isinstance(key, str):
            if isinstance(key, (int, float)) and not isinstance(key, bool):
                return self._number(key)
            return key
        if runtime_keyed:
            return self._replace(key)
        if isinstance(properties, Mapping) and key in properties:
            return key
        if key in self._names and not _ID_KEY.match(key):
            return key
        return self._replace(key)

    def _mapping(
        self, value: Mapping[str, Any], shape: Mapping[str, Any] | None, context: int
    ) -> dict[Any, Any]:
        properties = shape.get("properties") if shape else None
        owned = shape is not None and any(shape is node for node in _owner_keyed())
        runtime_keyed = context == _NESTED or (bool(shape and shape.get("opaque")) and not owned)
        below = _OWNER if owned else _BELOW_MAPPING[context]
        out: dict[Any, Any] = {}
        for key, inner in value.items():
            new_key = self._key(key, properties, runtime_keyed)
            if new_key in out:
                # Two keys became one. Take another, so the map keeps its size.
                for _ in range(_TRIES):
                    new_key = self._invent(str(key), force=True)
                    if new_key not in out:
                        break
                else:
                    raise SynthesisRefused("two keys of the read could not be told apart")
            child = (
                properties.get(key)
                if isinstance(key, str) and isinstance(properties, Mapping)
                else None
            )
            out[new_key] = self._value(inner, child, below)
        return out

    # -- numbers -----------------------------------------------------------------------

    def _integer(self, number: int) -> int:
        return self._number(number)  # type: ignore[return-value]

    def _real(self, number: float) -> float:
        return self._number(number)  # type: ignore[return-value]

    def _number(self, number: int | float) -> int | float:
        """The one replacement of *number* in this copy, of its sign, its size and its type.

        Equal numbers share one (``101`` and ``101.0`` are one number to a page), and no
        two different numbers of one size share one: each draw is a number of that size
        nobody has been given, taken uniformly without looking at the one it replaces.
        """
        if number == 0 or (isinstance(number, float) and not math.isfinite(number)):
            return number
        known = self._numbers.get(number)
        if known is None:
            known = self._numbers[number] = self._draw_number(number)
        if isinstance(number, float):
            try:
                return float(known)
            except OverflowError:
                return math.copysign(1.0, number)
        return int(known)

    def _draw_number(self, number: int | float) -> int | float:
        sign = -1 if number < 0 else 1
        if isinstance(number, int) or number.is_integer():
            try:
                size = len(str(abs(int(number))))
            except ValueError:
                raise SynthesisRefused("a number in the read is too large to copy") from None
            low = 10 ** (size - 1)
            return sign * (low + self._draw_index(("whole", sign, size), 9 * low))
        _sign, digits, exponent = Decimal(repr(abs(number))).normalize().as_tuple()
        count = len(digits)
        room = 9 if count == 1 else 81 * 10 ** (count - 2)
        index = self._draw_index(("part", sign, count, int(exponent)), room)
        value = float(Decimal(_mantissa(index, count)).scaleb(int(exponent)))
        return sign * (value if math.isfinite(value) else 1.0)

    def _draw_index(self, kind: tuple[Any, ...], room: int) -> int:
        """An index in ``[0, room)`` that nobody of this *kind* has been given, uniform over
        the ones left. A lazy shuffle: one draw and a few dictionary steps, however many
        have been given, so the copy stays linear. Only the count of values already given
        matters to it, never the value being replaced."""
        count = self._given_count.get(kind, 0)
        if count >= room:
            return self._rng.randrange(room)
        moved = self._given_moved.setdefault(kind, {})
        chosen = self._rng.randrange(count, room)
        index = moved.get(chosen, chosen)
        moved[chosen] = moved.get(count, count)
        self._given_count[kind] = count + 1
        return index

    # -- strings -----------------------------------------------------------------------

    def _string(self, text: str) -> str:
        """A value: a word of a closed vocabulary stays, anything else is replaced."""
        if not text or text in self._vocabulary:
            return text
        return self._replace(text)

    def _replace(self, text: str) -> str:
        """The one replacement of *text* in this copy, so equal texts stay equal."""
        known = self._strings.get(text)
        if known is None:
            known = self._strings[text] = self._invent(text)
        return known

    def _invent(self, text: str, *, force: bool = False) -> str:
        if not force and not _replaceable(text):
            # Punctuation and spaces only: nothing in it is the record's.
            return text
        pick = text
        short = len(text) <= 3
        for _ in range(_SHORT_TRIES if short else _TRIES):
            pick = self._draft(text)
            if self._is_new(pick):
                self._used_strings.add(pick)
                return pick
        if short:
            pick = self._nearby_new(pick) or pick
        self._used_strings.add(pick)
        return pick

    def _is_new(self, pick: str) -> bool:
        return pick not in self._used_strings and pick not in self._vocabulary

    def _nearby_new(self, pick: str) -> str | None:
        """A string that differs from *pick* in one letter or digit and that no other value
        has been given, for a short value whose draws all met another value's."""
        places = [i for i, char in enumerate(pick) if char in _ALPHABET_OF]
        self._rng.shuffle(places)
        for place in places:
            alphabet = _ALPHABET_OF[pick[place]]
            start = self._rng.randrange(len(alphabet))
            for step in range(len(alphabet)):
                candidate = (
                    pick[:place] + alphabet[(start + step) % len(alphabet)] + pick[place + 1 :]
                )
                if self._is_new(candidate):
                    return candidate
        return None

    def _draft(self, text: str) -> str:
        if _URL.match(text):
            return self._link(len(text))
        moment = _ISO.match(text)
        if moment:
            stamp = self._timestamp(moment)
            if stamp is not None:
                return stamp
        if _UUID.match(text):
            return self._uuid(text)
        if _ITEM_ID.match(text):
            return "it_" + self._hex(8, False)
        if _WHOLE.match(text):
            return self._whole(text)
        if _HEX.match(text):
            return self._hex(len(text), text != text.lower())
        return self._scramble(text)

    def _link(self, length: int) -> str:
        tail = max(0, length - len(_LINK_PREFIX))
        pool = _LOWER + _DIGITS
        return _LINK_PREFIX + "".join(pool[int(self._rnd() * len(pool))] for _ in range(tail))

    def _hex(self, length: int, upper: bool) -> str:
        pool = _UPPER_HEX if upper else _LOWER_HEX
        return "".join(pool[int(self._rnd() * 16)] for _ in range(length))

    def _uuid(self, text: str) -> str:
        upper = text != text.lower()
        return "-".join(self._hex(len(group), upper) for group in text.split("-"))

    def _whole(self, text: str) -> str:
        zeros = len(text) - len(text.lstrip("0"))
        rest = len(text) - zeros
        if rest == 0:
            return text
        head = str(1 + int(self._rnd() * 9))
        return "0" * zeros + head + "".join(_DIGITS[int(self._rnd() * 10)] for _ in range(rest - 1))

    def _timestamp(self, found: re.Match[str]) -> str | None:
        parts = found.groupdict()
        try:
            _dt.date(int(parts["y"]), int(parts["mo"]), int(parts["d"]))
            if parts["h"] is not None and (
                int(parts["h"]) > 23
                or int(parts["mi"]) > 59
                or (parts["s"] is not None and int(parts["s"]) > 59)
            ):
                return None
        except ValueError:
            return None
        moment = self._now - _dt.timedelta(seconds=self._rnd() * _TIMESTAMP_SPREAD_SECONDS)
        out = f"{moment:%Y-%m-%d}"
        if parts["h"] is None:
            return out
        out += parts["sep"] + f"{moment:%H:%M}"
        if parts["s"] is not None:
            out += f":{moment:%S}"
            if parts["f"] is not None:
                out += parts["fs"] + "".join(
                    _DIGITS[int(self._rnd() * 10)] for _ in range(len(parts["f"]))
                )
        zone = parts["tz"]
        if zone:
            out += self._zone(zone)
        return out

    def _zone(self, zone: str) -> str:
        if zone in ("Z", "z"):
            return zone
        sign = "+" if self._rnd() < 0.5 else "-"
        hours = int(self._rnd() * 13)
        minutes = 30 if self._rnd() < 0.2 else 0
        if len(zone) == 3:
            return f"{sign}{hours:02d}"
        joiner = ":" if ":" in zone else ""
        return f"{sign}{hours:02d}{joiner}{minutes:02d}"

    def _scramble(self, text: str) -> str:
        """*text* with every letter, digit and ideograph replaced by one of its own kind,
        drawn without looking at the character it replaces; spaces, line breaks and
        punctuation stay."""
        rnd = self._rnd
        out: list[str] = []
        for char in text:
            alphabet = _ALPHABET_OF.get(char)
            if alphabet is not None:
                out.append(alphabet[int(rnd() * len(alphabet))])
                continue
            code = ord(char)
            if code < 128 or char.isspace():
                out.append(char)
                continue
            block = _block_of(code)
            if block is not None:
                low, high = block
                out.append(chr(low + int(rnd() * (high - low + 1))))
                continue
            kind = unicodedata.category(char)[0]
            if kind == "L":
                alphabet = _UPPER if char.isupper() else _LOWER
                out.append(alphabet[int(rnd() * 26)])
            elif kind == "N":
                out.append(_DIGITS[int(rnd() * 10)])
            elif kind == "S":
                pool = _PICTOGRAPHS if code > 0xFFFF else _SYMBOLS
                out.append(pool[int(rnd() * len(pool))])
            elif kind == "M":
                out.append(_MARKS[int(rnd() * len(_MARKS))])
            elif kind in "PZ":
                out.append(char)
            else:
                # Format characters, private use and unassigned code points can spell text
                # that cannot be seen (a run of tag characters is ASCII).
                out.append("_")
        return "".join(out)


def _mantissa(index: int, count: int) -> int:
    """The *index*-th number of *count* digits whose last digit is not zero (so it keeps
    its length once the zeros are dropped), counting from the smallest."""
    if count == 1:
        return index + 1
    return (10 ** (count - 2) + index // 9) * 10 + index % 9 + 1


def _replaceable(text: str) -> bool:
    """Whether :meth:`_Synth._scramble` would change any character of *text*."""
    for char in text:
        if char in _ALPHABET_OF:
            return True
        if ord(char) >= 128 and not char.isspace():
            if _block_of(ord(char)) is not None or unicodedata.category(char)[0] not in "PZ":
                return True
    return False


def _clone(value: Any) -> Any:
    """A copy of a structure that is kept as it is: names, flags and tags."""
    if isinstance(value, Mapping):
        return {key: _clone(inner) for key, inner in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clone(inner) for inner in value]
    return value


def synthesize(
    read: Mapping[str, Any],
    *,
    model: Mapping[str, Any] | None = None,
    page: str = "",
    rng: random.Random | None = None,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    """A copy of *read* with the same shape and invented values.

    *read* is the object a page receives as ``window.kirocrew`` (see
    :func:`kiro_crew.dashboard_frame.read_payload`) and is not changed. *model* is the
    package's ``model``: it names the choices of each ``enum`` field, which are kept, and
    where each field sits in a fold, which is how a map keyed by a runtime id is found
    and its keys invented. Without it only the schema's vocabulary is kept.

    *page* is the page's text: a key it names is kept (see :func:`names_in`), and every other
    key that the type catalog does not list is invented.

    *rng* and *now* are for tests. Left out, the generator is seeded from the operating
    system on every call, and *now* is the clock read once for the whole copy: the instant
    a replacement timestamp is drawn back from.

    Raises :class:`SynthesisRefused` for a read that is not an object or is too large to
    copy within the check's budget.
    """
    if not isinstance(read, Mapping):
        raise SynthesisRefused("the read is not an object")
    moment = now or _dt.datetime.now(_dt.timezone.utc)
    synth = _Synth(
        rng or _fresh_rng(), fixed_vocabulary(), _fields_of(model), moment, names_in(page)
    )
    synth.survey(read)
    return synth.copy(read)
