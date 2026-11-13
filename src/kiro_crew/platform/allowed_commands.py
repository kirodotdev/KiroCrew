"""kiro-cli ``toolsSettings.shell.allowedCommands`` entries, read and matched safely.

kiro-cli compiles each entry with Rust's ``regex`` crate, anchored at both ends, and
runs a command one matches without a prompt. Kiro Crew answers the same grant at
its own gate on KAS, and must neither grant more than kiro-cli would nor let a
pattern stall the gateway. Python's ``re`` does neither safely: it reads some Rust
syntax differently (a POSIX class such as ``[[:alpha:]]``) and backtracks without
bound on nested repetition.

So Kiro Crew reads Rust's syntax with its own parser, and matches it with its own
automaton, never with ``re``:

- literal characters (a ``]`` or ``}`` too), and a backslash-escaped ASCII
  punctuation character or space as a literal (in an entry using any syntax below,
  not ``<`` / ``>``, which Rust reads as word boundaries; a literal-plus-``.*``
  entry may escape those two, since a command holding either is never granted);
- ``.`` (any character);
- a bracket class: ``[abc]``, ranges such as ``[A-Za-z0-9_./-]``, negation
  ``[^;|&]``, escaped punctuation, the POSIX classes (``[[:alnum:]]``,
  ``[[:^space:]]``) and ``\\d \\w \\s`` with their negations inside it;
- ``\\d \\w \\s \\D \\W \\S``, the word boundaries ``\\b \\B``, and ``^ $ \\A \\z``
  anywhere in the entry;
- groups ``( )``, ``(?: )`` and named ``(?P<n> )`` / ``(?<n> )``, alternation
  ``|``, and the repeats ``*``, ``+``, ``?``, ``{n}``, ``{n,}`` and ``{n,m}``, lazy
  (``+?``) or stacked (``a**``): over a whole command a lazy repeat accepts what
  the greedy one does;
- the flags ``i``, ``m``, ``s`` and ``U``, as ``(?i)`` or ``(?i: )``, cleared with
  ``-``. Only ``i`` changes what a plain command matches: a command holds no
  newline, so ``m`` and ``s`` read the same, and ``U`` only swaps laziness.

Rust reads ``\\d \\w \\s \\b`` and case folding by Unicode, and Kiro Crew reads
them by ASCII, so an entry using one (``Pattern.ascii_only``) matches only an
all-ASCII command: exactly as Rust does there, and nothing else. Such an entry
holding a non-ASCII character is refused.

Everything else is refused (``None``, and :func:`refusal_reason` says why) and
allows nothing: a nested class or class operator (``&&``, ``--``, ``~~``), a ``]``
first in a class, a ``-`` that is neither first, last nor a range, any other
escape (``\\p{L}``, ``\\x41``, ``\\<``, ``\\b{start}``), the flags ``x``, ``u`` and
``R``, a look-around, and an entry past :data:`_MAX_PROGRAM_STATES` states (a
counted repeat in the thousands). So is what Rust refuses to compile, since
kiro-cli then allows nothing by the entry: two groups of one name, a repeat count
past :data:`_MAX_REPEAT_COUNT`, nesting past Rust's limit and a program past
Rust's size limit. Kiro Crew's own bounds on
nesting (:data:`_MAX_STACKED_REPEATS`) and size (:data:`_MAX_PROGRAM_WEIGHT`) sit
below Rust's, so it also refuses some entries Rust compiles and kiro-cli runs
(``a***``, ``\\w{100}``): such an entry is named, never granted. A whole entry
is matched against the whole command, so a top-level ``a|b`` grants exactly
``a`` or ``b``, never the wider prefix-or-suffix reading the Rust anchoring would
give it.

Matching is a Thompson automaton walked one character at a time: linear in the
command's length times the pattern's size, with a hard step budget on top, so no
pattern can stall the gateway. A literal-plus-``.*`` entry (the shape kiro-cli's
own documentation shows: ``git status``, ``npm run .*``) keeps a faster direct match.

A LEAF on purpose: stdlib only, so both the KAS projection and the gate import it.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass

#: Characters that make a regex more than a literal outside a class.
_REGEX_METACHARACTERS = frozenset(".^$*+?()[]{}|\\")

#: Bound on one entry, so the record a session carries stays small.
MAX_PATTERN_LENGTH = 1024

#: Characters that make a command more than one plain command: chaining, pipes,
#: redirection and substitution. A command holding any of them, quoted or not, is
#: never granted by an entry, however wide, so a ``git status .*`` grant cannot
#: carry ``git status; <anything>``.
UNGRANTABLE_COMMAND_CHARS = frozenset(";&|<>$`\n\r")

#: Grouping and brace characters. A command holding one OUTSIDE a quoted argument
#: is never granted (a subshell, a group, a brace expansion); inside a single- or
#: double-quoted argument they are plain text to every shell, so
#: ``git commit -m "fix(scope): x"`` can be granted. See :func:`plain_command`.
QUOTED_ONLY_COMMAND_CHARS = frozenset("(){}")

#: Commands longer than this are not matched against an entry at all.
MAX_COMMAND_LENGTH = 4096

#: Largest number a counted repeat may hold. Rust reads each count as a ``u32`` and
#: refuses the entry past it, so kiro-cli allows nothing by it, even where an outer
#: ``{0}`` would leave the repeat out of the compiled program.
_MAX_REPEAT_COUNT = 4_294_967_295

#: Deepest group nesting an entry may have.
_MAX_GROUP_DEPTH = 32

#: Most repeats one atom may carry (``a**`` carries two). Rust refuses an entry
#: nested more than 250 deep, counting each group, concatenation and repeat, and
#: kiro-cli then allows nothing by it; with two per atom and
#: :data:`_MAX_GROUP_DEPTH` groups an entry Kiro Crew reads stays under 170.
_MAX_STACKED_REPEATS = 2

#: Most automaton states one entry compiles to. A counted repeat is expanded into
#: copies of what it repeats; an entry without one stays well below this.
_MAX_PROGRAM_STATES = 2048

#: Most weight one entry's states may add up to. Rust refuses an entry whose
#: compiled program passes 10 MiB (``\\w{250}`` already does), and kiro-cli then
#: allows nothing by it, so each state weighs at least what Rust spends on it, in
#: KiB (:data:`_PERL_WEIGHTS`, :data:`_WIDE_WEIGHT`, one more per non-ASCII
#: character or range a class lists, any other state 1), and the bound sits at
#: 60% of Rust's.
_MAX_PROGRAM_WEIGHT = 6144

#: The weight of a class holding ``\\d \\w \\s`` or a negation of one. Rust compiles
#: them as Unicode classes: about 5 KiB for ``\\d``, 13 for ``\\D``, 50 for ``\\w``
#: and ``\\W``, 2 for ``\\S``, under 1 for ``\\s``.
_PERL_WEIGHTS = {"d": 16, "D": 16, "w": 64, "W": 64, "s": 1, "S": 2}

#: The weight of ``.`` and of a class that reaches past ASCII (a negation).
_WIDE_WEIGHT = 2

#: Most tree nodes one entry's compile visits. A repeated copy of an empty group
#: (``(?:){999999}``) adds no state, so the state bound alone would not end it.
_MAX_COMPILE_STEPS = 65_536

#: Automaton states one match may visit before it gives up and grants nothing.
_MAX_MATCH_STEPS = 1_000_000

#: One character test: (negated, single characters, inclusive ranges).
_CharSet = tuple[bool, frozenset[str], tuple[tuple[str, str], ...]]

_ANY: _CharSet = (True, frozenset("\n"), ())

#: ``.`` under the ``s`` flag.
_ANY_NEWLINE: _CharSet = (True, frozenset(), ())

#: The highest code point, for a class's complement.
_MAX_CHAR = "\U0010ffff"

_REPEAT_CHARS = frozenset("*+?{")
_DIGITS = frozenset("0123456789")

#: Rust's POSIX classes, all ASCII: (single characters, inclusive ranges).
_POSIX_CLASSES: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    "alnum": ("", (("0", "9"), ("A", "Z"), ("a", "z"))),
    "alpha": ("", (("A", "Z"), ("a", "z"))),
    "ascii": ("", (("\x00", "\x7f"),)),
    "blank": ("\t ", ()),
    "cntrl": ("\x7f", (("\x00", "\x1f"),)),
    "digit": ("", (("0", "9"),)),
    "graph": ("", (("!", "~"),)),
    "lower": ("", (("a", "z"),)),
    "print": ("", ((" ", "~"),)),
    "punct": ("", (("!", "/"), (":", "@"), ("[", "`"), ("{", "~"))),
    "space": ("\t\n\x0b\x0c\r ", ()),
    "upper": ("", (("A", "Z"),)),
    "word": ("_", (("0", "9"), ("A", "Z"), ("a", "z"))),
    "xdigit": ("", (("0", "9"), ("A", "F"), ("a", "f"))),
}

#: The two non-ASCII characters whose simple case fold is an ASCII letter: the
#: Kelvin sign folds with ``k`` and the long s with ``s``. Rust folds them, so
#: :func:`_fold` and the Unicode word class carry them, which keeps an ASCII
#: command's answer exact under negation and ``(?i)``.
_ASCII_FOLD_GROUPS = (("k", "K", "\u212a"), ("s", "S", "\u017f"))

#: ``\\d \\w \\s`` as Rust's Unicode classes hold them for an ASCII character (and
#: for the two of :data:`_ASCII_FOLD_GROUPS`, both letters): the uppercase escape
#: is the complement.
_PERL_CLASSES: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    "d": _POSIX_CLASSES["digit"],
    "w": ("_\u212a\u017f", _POSIX_CLASSES["alnum"][1]),
    "s": _POSIX_CLASSES["space"],
}


@dataclass(frozen=True)
class Pattern:
    """One accepted entry, ready to match.

    ``segments`` is set for a literal-plus-``.*`` entry: its literal pieces between
    the wildcards (``("git status",)`` is exact, ``("npm run ", "")`` is
    ``npm run .*``). ``program`` is set for every other accepted entry: the
    automaton :func:`matches` walks. ``ascii_only`` marks an entry using a
    construct Rust reads by Unicode (``\\d \\w \\s \\b``, ``(?i)``): it matches only
    an all-ASCII command.
    """

    segments: tuple[str, ...] | None = None
    program: tuple[tuple, ...] | None = None
    start: int = 0
    ascii_only: bool = False


def parse(raw: object) -> Pattern | None:
    """*raw* read as an entry Kiro Crew can match like kiro-cli, or ``None``."""
    read = _read(raw)
    return read if isinstance(read, Pattern) else None


def refusal_reason(raw: object) -> str | None:
    """Why :func:`parse` refuses *raw*, in a few words, or ``None`` when it reads it."""
    read = _read(raw)
    return None if isinstance(read, Pattern) else read


#: How many unreadable entries a refusal names before it counts the rest.
_NAMED_UNREADABLE = 3

#: Joins a ``toolsSettings.<key>.allowedCommands`` path to one entry it names in a
#: session-start notice item: ``toolsSettings.shell.allowedCommands: (?x)a (the x flag)``.
UNREADABLE_ENTRY_MARK = ".allowedCommands: "


def unreadable_note(raw_patterns: object) -> str:
    """The sentence a ``denyByDefault`` refusal ends with when some of *raw_patterns*
    cannot be read, ``""`` when every one can: each such entry allows nothing, so the
    person reading the refusal (a cron run's record included) learns which to fix."""
    entries = raw_patterns if isinstance(raw_patterns, (list, tuple)) else ()
    unread = [(raw, reason) for raw in entries if (reason := refusal_reason(raw)) is not None]
    if not unread:
        return ""
    named = "; ".join(f"{raw} ({reason})" for raw, reason in unread[:_NAMED_UNREADABLE])
    if len(unread) > _NAMED_UNREADABLE:
        named += f"; and {len(unread) - _NAMED_UNREADABLE} more"
    return (
        f". Kiro Crew cannot read these allowedCommands entries, so they allow nothing: "
        f"{named}. Run kirocrew doctor to list every one"
    )


#: How many distinct entry texts :func:`_read_text` keeps read. Four lists at the
#: longest a spec's ``allowedCommands`` is applied to; past it the least recently
#: used text is read again when it is next asked for.
_READ_CACHE_ENTRIES = 4096


def _read(raw: object) -> Pattern | str:
    """*raw* as a :class:`Pattern`, or the reason it cannot be one.

    Anything but a non-empty string within :data:`MAX_PATTERN_LENGTH` is refused
    here, on every call, and never reaches :func:`_read_text`'s cache."""
    if not isinstance(raw, str):
        return "not a string"
    if not raw:
        return "empty"
    if len(raw) > MAX_PATTERN_LENGTH:
        return f"longer than {MAX_PATTERN_LENGTH} characters"
    return _read_text(raw)


@functools.lru_cache(maxsize=_READ_CACHE_ENTRIES)
def _read_text(raw: str) -> Pattern | str:
    """:func:`_read` for one entry's text, read once per distinct text.

    The answer depends on *raw* alone and is immutable, so the cache is keyed on
    the entry's own text: a spec whose list changed asks for its new texts and
    cannot be handed another text's answer. The gate asks about every entry of an
    agent's list on each shell call, which this keeps to a lookup."""
    # Read verbatim, as kiro-cli reads it: no stripping, and no character a reader
    # could not see (control characters, any whitespace but the plain space).
    body = raw
    if not _plain_text(body):
        return "a control character, or whitespace other than a plain space"
    for prefix in ("\\A", "^"):
        # Kept when a repeat follows: ``^*`` repeats the anchor.
        if body.startswith(prefix) and body[len(prefix) : len(prefix) + 1] not in _REPEAT_CHARS:
            body = body[len(prefix) :]
            break
    for suffix in ("\\z", "$"):
        if body.endswith(suffix) and not body.endswith("\\" + suffix):
            body = body[: -len(suffix)]
            break
    segments = _literal_segments(body)
    if segments is not None:
        if not any(segments) and len(segments) == 1:
            return "it matches only an empty command"
        return Pattern(segments=segments)
    parser = _Parser(body)
    try:
        tree = parser.parse()
        if parser.ascii_only and not raw.isascii():
            raise _Refused("a non-ASCII character beside \\d, \\w, \\s, \\b or (?i)")
        program, start = _compile(tree)
    except _Refused as refused:
        return refused.reason
    return Pattern(program=program, start=start, ascii_only=parser.ascii_only)


def _literal_segments(body: str) -> tuple[str, ...] | None:
    """*body*'s literal pieces between ``.*`` wildcards, or ``None`` when it holds
    any other regex syntax."""
    segments: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if body.startswith(".*", i):
            segments.append("".join(current))
            current = []
            i += 2
        elif ch == "\\":
            nxt = body[i + 1] if i + 1 < len(body) else ""
            if not nxt or not nxt.isascii() or nxt.isalnum() or nxt.isspace():
                return None
            current.append(nxt)
            i += 2
        elif ch in _REGEX_METACHARACTERS:
            return None
        else:
            current.append(ch)
            i += 1
    segments.append("".join(current))
    return tuple(segments)


class _Refused(Exception):
    """The entry holds syntax outside what Kiro Crew reads like kiro-cli."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


#: What ``(?i)`` alone parses to: it sets flags and matches nothing, so no repeat
#: may follow it, as in Rust.
_FLAGS_ONLY: tuple = ("cat", ())


class _Parser:
    """Recursive descent over one entry's body (the two end anchors removed).

    Produces a tree of tuples: ``("set", charset, weight)``, ``("assert", kind)``,
    ``("cat", [..])``, ``("alt", [..])``, ``("star" | "plus" | "opt", node)`` and
    ``("repeat", node, low, high)`` (``high`` ``None`` for ``{n,}``). Raises
    :class:`_Refused` on anything outside the accepted syntax, and on what Rust
    refuses to compile. A flag set by ``(?i)`` holds to the end of the group it
    sits in, later branches included, as in Rust.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.depth = 0
        self.fold = False
        self.multi_line = False
        self.dot_newline = False
        self.ascii_only = False
        self.group_names: set[str] = set()

    def parse(self) -> tuple:
        node = self._alternation()
        if self.pos != len(self.text):
            raise _Refused("an unmatched )")
        return node

    def _peek(self, ahead: int = 0) -> str:
        at = self.pos + ahead
        return self.text[at] if at < len(self.text) else ""

    def _alternation(self) -> tuple:
        branches = [self._concat()]
        while self._peek() == "|":
            self.pos += 1
            branches.append(self._concat())
        return branches[0] if len(branches) == 1 else ("alt", branches)

    def _concat(self) -> tuple:
        items: list[tuple] = []
        while self.pos < len(self.text) and self._peek() not in "|)":
            items.append(self._repeat())
        return ("cat", items)

    def _repeat(self) -> tuple:
        if self._peek() in _REPEAT_CHARS:
            raise _Refused("a repeat with nothing to repeat")
        node = self._atom()
        stacked = 0
        while self._peek() in _REPEAT_CHARS:
            if node is _FLAGS_ONLY:
                raise _Refused("a repeat with nothing to repeat")
            stacked += 1
            if stacked > _MAX_STACKED_REPEATS:
                raise _Refused(f"more than {_MAX_STACKED_REPEATS} repeats on one atom")
            quant = self._peek()
            if quant == "{":
                low, high = self._counted()
                node = ("repeat", node, low, high)
            else:
                self.pos += 1
                node = ({"*": "star", "+": "plus", "?": "opt"}[quant], node)
            if self._peek() == "?":
                # Lazy: over a whole command it accepts what the greedy repeat does.
                self.pos += 1
        return node

    def _counted(self) -> tuple[int, int | None]:
        """``{n}``, ``{n,}`` or ``{n,m}`` at :attr:`pos`. Rust allows spaces around
        each number, but not between ``,`` and the ``}`` of ``{n,}``."""
        self.pos += 1
        low = self._count()
        high: int | None = low
        if self._peek() == ",":
            self.pos += 1
            high = None if self._peek() == "}" else self._count()
        if self._peek() != "}":
            raise _Refused("a malformed counted repeat")
        self.pos += 1
        if high is not None and high < low:
            raise _Refused("a counted repeat whose minimum is above its maximum")
        return low, high

    def _count(self) -> int:
        self._spaces()
        start = self.pos
        while self._peek() in _DIGITS:
            self.pos += 1
        if self.pos == start:
            raise _Refused("a malformed counted repeat")
        digits = self.text[start : self.pos]
        self._spaces()
        count = int(digits)
        if count > _MAX_REPEAT_COUNT:
            raise _Refused(f"a counted repeat past {_MAX_REPEAT_COUNT}")
        return count

    def _spaces(self) -> None:
        while self._peek() == " ":
            self.pos += 1

    def _atom(self) -> tuple:
        ch = self._peek()
        if ch == "(":
            return self._group()
        if ch == "\\":
            return self._escaped_atom()
        self.pos += 1
        if ch == "[":
            return ("set", *self._class())
        if ch == ".":
            return ("set", _ANY_NEWLINE if self.dot_newline else _ANY, _WIDE_WEIGHT)
        if ch == "^":
            return ("assert", "line_start" if self.multi_line else "start")
        if ch == "$":
            return ("assert", "line_end" if self.multi_line else "end")
        return ("set", self._literal(ch), 1)

    def _escaped_atom(self) -> tuple:
        """A class, an assertion or a literal spelled with ``\\`` at :attr:`pos`."""
        nxt = self._peek(1)
        if nxt.lower() in _PERL_CLASSES:
            self.pos += 2
            item = self._negated_if(nxt.isupper(), self._perl_class(nxt.lower()))
            return ("set", item, _PERL_WEIGHTS[nxt])
        if nxt in ("b", "B"):
            if self._peek(2) == "{":
                raise _Refused("a \\b{...} word boundary")
            self.pos += 2
            self.ascii_only = True
            return ("assert", "word" if nxt == "b" else "not_word")
        if nxt in ("A", "z"):
            self.pos += 2
            return ("assert", "start" if nxt == "A" else "end")
        return ("set", self._literal(self._escape()), 1)

    def _group(self) -> tuple:
        self.pos += 1
        saved = (self.fold, self.multi_line, self.dot_newline)
        if self._peek() == "?":
            self.pos += 1
            if self._peek() == ":":
                self.pos += 1
            elif self.text.startswith(("P<", "<"), self.pos):
                self.pos += 2 if self._peek() == "P" else 1
                self._group_name()
            elif self._peek() in ("=", "!"):
                raise _Refused("a look-around")
            elif self._flags():
                # ``(?i)``: the flags hold to the end of the enclosing group.
                return _FLAGS_ONLY
        self.depth += 1
        if self.depth > _MAX_GROUP_DEPTH:
            raise _Refused(f"groups nested deeper than {_MAX_GROUP_DEPTH}")
        inner = self._alternation()
        if self._peek() != ")":
            raise _Refused("an unclosed group")
        self.pos += 1
        self.depth -= 1
        self.fold, self.multi_line, self.dot_newline = saved
        return inner

    def _group_name(self) -> None:
        """A capture group's name and its ``>``: a letter or ``_``, then letters,
        digits and ``_ . [ ]``, as Rust allows, and not a name an earlier group of
        the entry holds, which Rust refuses."""
        start = self.pos
        while self._peek() not in (">", ""):
            self.pos += 1
        name = self.text[start : self.pos]
        if name[:1] in ("=", "!"):
            raise _Refused("a look-around")
        if (
            self._peek() != ">"
            or not name
            or not (name[0] == "_" or (name[0].isascii() and name[0].isalpha()))
            or not all(c.isascii() and (c.isalnum() or c in "_.[]") for c in name)
        ):
            raise _Refused("an invalid group name")
        if name in self.group_names:
            raise _Refused("two groups with one name")
        self.group_names.add(name)
        self.pos += 1

    def _flags(self) -> bool:
        """The flags of ``(?flags)`` or ``(?flags:``, applied; True for the first
        (no ``:``, so nothing follows inside the group)."""
        seen: set[str] = set()
        negated = flag_after_negation = False
        while self._peek() not in (":", ")"):
            ch = self._peek()
            if ch == "-" and not negated:
                negated = True
            elif ch in ("x", "u", "R"):
                raise _Refused(f"the {ch} flag")
            elif ch in ("i", "m", "s", "U") and ch not in seen:
                seen.add(ch)
                flag_after_negation = negated
                if ch == "i":
                    self.fold = not negated
                    self.ascii_only = self.ascii_only or self.fold
                elif ch == "m":
                    self.multi_line = not negated
                elif ch == "s":
                    self.dot_newline = not negated
            else:
                raise _Refused("an invalid flag group")
            self.pos += 1
        if not seen or (negated and not flag_after_negation):
            raise _Refused("an invalid flag group")
        closes = self._peek() == ")"
        self.pos += 1
        return closes

    def _escape(self) -> str:
        """The literal a ``\\`` + ASCII punctuation or space at :attr:`pos` names.

        Rust escapes any ASCII character but a letter or digit to a literal, and
        reads ``\\<`` / ``\\>`` as word boundaries; every other escape here
        (``\\x41``, ``\\p{L}``, a non-ASCII character, a trailing ``\\``) is
        refused.
        """
        nxt = self._peek(1)
        if not nxt:
            raise _Refused("a trailing backslash")
        if not nxt.isascii() or not nxt.isprintable() or nxt.isalnum() or nxt in "<>":
            raise _Refused(f"the escape \\{nxt}")
        self.pos += 2
        return nxt

    def _literal(self, ch: str) -> _CharSet:
        chars: frozenset[str] = frozenset(ch)
        ranges: tuple[tuple[str, str], ...] = ()
        if self.fold:
            chars, ranges = _fold(chars, ranges)
        return (False, chars, ranges)

    def _perl_class(self, name: str) -> tuple[frozenset[str], tuple[tuple[str, str], ...]]:
        """``\\d \\w \\s`` as characters and ranges; marks the entry ASCII-only."""
        self.ascii_only = True
        chars, ranges = _PERL_CLASSES[name]
        return frozenset(chars), ranges

    def _negated_if(
        self, negated: bool, item: tuple[frozenset[str], tuple[tuple[str, str], ...]]
    ) -> _CharSet:
        """*item*, case-folded under ``(?i)`` and then complemented when *negated*:
        the order Rust applies to a class item."""
        chars, ranges = item
        if self.fold:
            chars, ranges = _fold(chars, ranges)
        if negated:
            return (False, frozenset(), _complement(chars, ranges))
        return (False, chars, ranges)

    def _class(self) -> tuple[_CharSet, int]:
        """The bracket class at :attr:`pos` (after its ``[``) and its weight."""
        negated = False
        if self._peek() == "^":
            negated = True
            self.pos += 1
        first = self.pos
        weight = _WIDE_WEIGHT if negated else 1
        listed_wide = 0
        chars: set[str] = set()
        ranges: list[tuple[str, str]] = []
        while True:
            ch = self._peek()
            if not ch:
                raise _Refused("an unclosed class")
            if ch == "]":
                if self.pos == first:
                    raise _Refused("a ] first in a class")  # Rust reads it literally
                self.pos += 1
                break
            if self.text.startswith(("&&", "--", "~~"), self.pos):
                raise _Refused("a class operator (&&, --, ~~)")
            item = self._class_item()
            if item is not None:
                (_, item_chars, item_ranges), item_weight = item
                weight = max(weight, item_weight)
                chars.update(item_chars)
                ranges.extend(item_ranges)
                if self._peek() == "-" and self._peek(1) != "]":
                    raise _Refused("a range that starts at a class")
                continue
            if ch == "-":
                # Literal only first or last, where both engines agree.
                if self.pos != first and self._peek(1) != "]":
                    raise _Refused("a - in a class that is neither first, last nor a range")
                self.pos += 1
                chars.add("-")
                continue
            lo = hi = self._class_char()
            if self._peek() == "-" and self._peek(1) not in ("]", ""):
                self.pos += 1
                if self._peek() == "-":
                    raise _Refused("a class operator (&&, --, ~~)")
                if self._peek() in ("[", "]") or self._class_item_ahead():
                    raise _Refused("a range that ends at a class")
                hi = self._class_char()
                if hi < lo:
                    raise _Refused("a reversed range")
                ranges.append((lo, hi))
            else:
                chars.add(lo)
            # Rust compiles a non-ASCII character or range into its UTF-8 bytes.
            listed_wide += 0 if hi.isascii() else 1
        folded_chars, folded_ranges = frozenset(chars), tuple(ranges)
        if self.fold:
            folded_chars, folded_ranges = _fold(folded_chars, folded_ranges)
        return (negated, folded_chars, folded_ranges), weight + listed_wide

    def _class_item_ahead(self) -> bool:
        return self._peek() == "\\" and self._peek(1).lower() in _PERL_CLASSES

    def _class_item(self) -> tuple[_CharSet, int] | None:
        """A POSIX class (``[:alpha:]``, ``[:^alpha:]``) or ``\\d \\w \\s`` and their
        negations at :attr:`pos`, consumed, with its weight; ``None`` when none
        starts there. Any other ``[`` opens a nested class, which is refused."""
        if self._class_item_ahead():
            name = self._peek(1)
            self.pos += 2
            item = self._negated_if(name.isupper(), self._perl_class(name.lower()))
            return item, _PERL_WEIGHTS[name]
        if self._peek() != "[":
            return None
        end = self.text.find(":]", self.pos + 2)
        body = self.text[self.pos + 2 : end] if self.text.startswith("[:", self.pos) else ""
        negated = body.startswith("^")
        name = body[1:] if negated else body
        if end < 0 or name not in _POSIX_CLASSES:
            raise _Refused("a nested class")
        self.pos = end + 2
        posix_chars, posix_ranges = _POSIX_CLASSES[name]
        item = self._negated_if(negated, (frozenset(posix_chars), posix_ranges))
        return item, _WIDE_WEIGHT if negated else 1

    def _class_char(self) -> str:
        ch = self._peek()
        if ch == "\\":
            if self._peek(1) in ("b", "B", "A", "z"):
                raise _Refused(f"the escape \\{self._peek(1)} in a class")
            return self._escape()
        self.pos += 1
        return ch


def _fold(
    chars: frozenset[str], ranges: tuple[tuple[str, str], ...]
) -> tuple[frozenset[str], tuple[tuple[str, str], ...]]:
    """*chars* and *ranges* closed under Rust's simple case folding, exact for every
    ASCII character and the two of :data:`_ASCII_FOLD_GROUPS`."""
    extra = {c.swapcase() for c in chars if c.isascii() and c.isalpha()}
    extra_ranges: list[tuple[str, str]] = []
    for lo, hi in ranges:
        for first, last, shift in (("a", "z", -32), ("A", "Z", 32)):
            start, end = max(lo, first), min(hi, last)
            if start <= end:
                extra_ranges.append((chr(ord(start) + shift), chr(ord(end) + shift)))
    for group in _ASCII_FOLD_GROUPS:
        if any(c in chars or any(lo <= c <= hi for lo, hi in ranges) for c in group):
            extra.update(group)
    return chars | extra, ranges + tuple(extra_ranges)


def _complement(
    chars: frozenset[str], ranges: tuple[tuple[str, str], ...]
) -> tuple[tuple[str, str], ...]:
    """Every character outside *chars* and *ranges*, as inclusive ranges."""
    spans = sorted([(ord(c), ord(c)) for c in chars] + [(ord(lo), ord(hi)) for lo, hi in ranges])
    out: list[tuple[str, str]] = []
    nxt = 0
    for lo, hi in spans:
        if lo > nxt:
            out.append((chr(nxt), chr(lo - 1)))
        nxt = max(nxt, hi + 1)
    if nxt <= ord(_MAX_CHAR):
        out.append((chr(nxt), _MAX_CHAR))
    return tuple(out)


def _compile(tree: tuple) -> tuple[tuple[tuple, ...], int]:
    """*tree* as a Thompson program and its start state.

    States: ``("match",)``, ``("set", charset, next)``, ``("assert", kind, next)``
    and ``("split", a, b)``. Refused past :data:`_MAX_PROGRAM_STATES` states,
    :data:`_MAX_PROGRAM_WEIGHT` weight or :data:`_MAX_COMPILE_STEPS` nodes visited.
    """
    states: list[tuple] = [("match",)]
    steps = [0]
    weight = [0]

    def add(state: tuple, cost: int = 1) -> int:
        if len(states) >= _MAX_PROGRAM_STATES:
            raise _Refused("too large to match (a counted repeat in the thousands)")
        weight[0] += cost
        if weight[0] > _MAX_PROGRAM_WEIGHT:
            raise _Refused(
                "too large for Kiro Crew to read (a counted repeat of \\w or \\d), "
                "though kiro-cli may run it"
            )
        states.append(state)
        return len(states) - 1

    def emit(node: tuple, out: int) -> int:
        steps[0] += 1
        if steps[0] > _MAX_COMPILE_STEPS:
            raise _Refused("too large to match (a counted repeat in the thousands)")
        kind = node[0]
        if kind == "set":
            return add(("set", node[1], out), node[2])
        if kind == "assert":
            return add(("assert", node[1], out))
        if kind == "cat":
            for child in reversed(node[1]):
                out = emit(child, out)
            return out
        if kind == "alt":
            entries = [emit(child, out) for child in node[1]]
            entry = entries[-1]
            for other in reversed(entries[:-1]):
                entry = add(("split", other, entry))
            return entry
        if kind == "opt":
            return add(("split", emit(node[1], out), out))
        if kind == "repeat":
            _, child, low, high = node
            if high is None:
                entry = emit(("star", child), out)
            else:
                # Nested, ``(x(x)?)?``: once a copy is skipped the rest are, so a
                # step's closure holds one state per copy reached, not every copy.
                entry = out
                for _ in range(high - low):
                    entry = add(("split", emit(child, entry), out))
            for _ in range(low):
                entry = emit(child, entry)
            return entry
        # star / plus: a loop through one split state, patched once the body exists.
        loop = add(("split", -1, out))
        body = emit(node[1], loop)
        states[loop] = ("split", body, out)
        return loop if kind == "star" else body

    start = emit(tree, 0)
    return tuple(states), start


def _plain_text(text: str) -> bool:
    """Every character printable, and the plain space the only whitespace."""
    return all(ch == " " or (ch.isprintable() and not ch.isspace()) for ch in text)


def plain_command(command: object) -> bool:
    """Whether *command*, exactly as the host recovered it, may be granted at all.

    Not empty, at most :data:`MAX_COMMAND_LENGTH`, none of
    :data:`UNGRANTABLE_COMMAND_CHARS` anywhere, none of
    :data:`QUOTED_ONLY_COMMAND_CHARS` outside a quoted argument
    (:func:`_grouping_only_quoted`), and no control character or whitespace but
    the plain space: a non-breaking space or a tab names a different path to the
    shell while looking the same, so a grant for ``rm scratch`` never covers
    ``rm scratch`` plus an invisible character. Never stripped or rewritten: the
    command matched is the command that runs.
    """
    return (
        isinstance(command, str)
        and bool(command)
        and len(command) <= MAX_COMMAND_LENGTH
        and not UNGRANTABLE_COMMAND_CHARS.intersection(command)
        and _plain_text(command)
        and _grouping_only_quoted(command)
    )


#: The separators a chain of plain commands may use and still be granted, longest
#: first so ``&&`` and ``||`` are read before a lone ``|``.
CHAIN_SEPARATORS = ("&&", "||", ";", "|")

#: Most commands one chain may hold and still be granted.
MAX_CHAIN_COMMANDS = 16

#: Characters that make a chain ungrantable: each can move a separator into an
#: argument, so the split would not match what the shell runs. A non-ASCII
#: character refuses the chain too (:func:`chain_commands`): PowerShell reads the
#: typographic quotes U+2018 to U+201E as quotes.
_CHAIN_QUOTING_CHARS = frozenset("'\"\\")

#: PowerShell's stop-parsing token: everything after it, separators included, is
#: passed to the program as literal text, so a chain holding it is refused too.
_STOP_PARSING = "--%"


def chain_commands(command: object) -> tuple[str, ...] | None:
    """The plain commands *command* chains with :data:`CHAIN_SEPARATORS`, or ``None``.

    Split at every separator, with the spaces around it dropped. A chain holding a
    quote, a backslash or any non-ASCII character anywhere is ``None``: a quote
    (PowerShell's typographic ones included) can carry a separator into one shell
    argument, so the split parts would not be the commands that run.
    ``None`` too when a part is empty or not :func:`plain_command`
    (so a lone ``&``, a redirect, a substitution or a newline still refuses the
    whole command), when the command is too long, or past
    :data:`MAX_CHAIN_COMMANDS` parts. A plain command is its own one part.
    """
    if not isinstance(command, str) or not command or len(command) > MAX_COMMAND_LENGTH:
        return None
    parts: list[str] = []
    start = i = 0
    while i < len(command):
        sep = next((s for s in CHAIN_SEPARATORS if command.startswith(s, i)), "")
        if sep:
            parts.append(command[start:i])
            i += len(sep)
            start = i
        else:
            i += 1
    parts.append(command[start:])
    if len(parts) > 1:
        if (
            not command.isascii()
            or _CHAIN_QUOTING_CHARS.intersection(command)
            or _STOP_PARSING in command
        ):
            return None
        parts = [p.strip(" ") for p in parts]
    if len(parts) > MAX_CHAIN_COMMANDS or not all(plain_command(p) for p in parts):
        return None
    return tuple(parts)


def chain_matches_all(raw_patterns: object, command: object) -> bool:
    """Whether *command* is a plain command or a chain of them
    (:func:`chain_commands`) and one of *raw_patterns* matches EVERY part whole.

    kiro-cli asks about each command of a chain; a chain is granted only when each
    one would be. One :data:`_MAX_MATCH_STEPS` budget covers the whole chain."""
    parts = chain_commands(command)
    if parts is None:
        return False
    budget = [_MAX_MATCH_STEPS]
    patterns = [
        p
        for p in (
            parse(raw) for raw in (raw_patterns if isinstance(raw_patterns, (list, tuple)) else ())
        )
        if p is not None
    ]
    return all(any(_match(p, part, budget) for p in patterns) for part in parts)


#: What the person is told when an ``allowedCommands`` entry would match a command
#: but the command's shell composition keeps it from being granted.
COMPOSITION_NOTICE = (
    "This command matches allowedCommands, but a chain (&&, ||, ;, |) runs without a "
    "prompt only when every command in it matches and it holds no quote, backslash, "
    "non-ASCII character or --%, and >, <, $() or backticks always ask. Run the script alone, "
    "or move the pipeline into the script."
)

#: Characters that end the leading simple command of a composed one.
_COMPOSITION_BREAK_CHARS = UNGRANTABLE_COMMAND_CHARS | QUOTED_ONLY_COMMAND_CHARS


def composition_blocks_match(raw_patterns: object, command: object) -> bool:
    """Whether *command* is refused a grant only because of its shell composition.

    True when *command* is not :func:`plain_command` because it chains, pipes,
    redirects, substitutes or groups, and one of *raw_patterns* matches either the
    whole command or its leading simple command (the text before the first such
    character, without trailing spaces). Used only to EXPLAIN a prompt: it never
    grants, and a command it returns True for still asks.
    """
    if not isinstance(command, str) or not command or len(command) > MAX_COMMAND_LENGTH:
        return False
    if plain_command(command) or not _COMPOSITION_BREAK_CHARS.intersection(command):
        return False
    cut = min(command.index(ch) for ch in _COMPOSITION_BREAK_CHARS if ch in command)
    leading = command[:cut].rstrip(" ")
    candidates = [leading] if leading and _plain_text(leading) else []
    if _plain_text(command):
        candidates.append(command)
    return any(matches_any(raw_patterns, text) for text in candidates)


def _grouping_only_quoted(command: str) -> bool:
    """Whether every ``( ) { }`` of *command* sits inside a closed quoted argument.

    A command without one passes, as before. With one, the command must be ASCII
    (PowerShell also reads the typographic quotes U+2018 to U+201E as quotes, so
    one could close a string early) and hold no backslash (a ``\\"`` is a quote to
    one shell and a literal to another), every quote must close, and each such
    character must fall between single quotes or between double quotes. Read the
    same way by POSIX shells, zsh and PowerShell; every character that could still
    act inside double quotes (``$``, a backtick) is already refused by
    :data:`UNGRANTABLE_COMMAND_CHARS`.
    """
    if not QUOTED_ONLY_COMMAND_CHARS.intersection(command):
        return True
    if not command.isascii() or "\\" in command:
        return False
    quote = ""
    for ch in command:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
        elif ch in QUOTED_ONLY_COMMAND_CHARS:
            return False
    return not quote


def matches(pattern: Pattern, command: str) -> bool:
    """Whether *command* is the whole of what *pattern* describes.

    A literal-plus-``.*`` entry is matched directly; any other walks its automaton,
    which gives up (no match, so nothing is granted) past :data:`_MAX_MATCH_STEPS`.
    An ``ascii_only`` entry matches no command holding a non-ASCII character.
    """
    return _match(pattern, command, [_MAX_MATCH_STEPS])


def matches_any(raw_patterns: object, command: str) -> bool:
    """Whether one of *raw_patterns*, each read by :func:`parse`, matches *command*
    whole. Entries :func:`parse` refuses match nothing. Every automaton walk draws
    on ONE budget of :data:`_MAX_MATCH_STEPS`, so a long list of costly entries
    costs no more than one; once it is spent, the rest match nothing."""
    budget = [_MAX_MATCH_STEPS]
    for raw in raw_patterns if isinstance(raw_patterns, (list, tuple)) else ():
        parsed = parse(raw)
        if parsed is not None and _match(parsed, command, budget):
            return True
    return False


def _match(pattern: Pattern, command: str, budget: list[int], end: int | None = None) -> bool:
    """Whether *pattern* matches ``command[:end]`` whole (all of *command* when
    *end* is ``None``). An assertion at the end of that piece sees the character of
    *command* after it, as Rust sees it when the entry matches a leading piece."""
    if pattern.ascii_only and not command.isascii():
        return False
    stop = len(command) if end is None else end
    if pattern.segments is not None:
        return _segments_match(pattern.segments, command[:stop])
    assert pattern.program is not None
    return _run(pattern.program, pattern.start, command, stop, budget)


def _segments_match(segments: tuple[str, ...], command: str) -> bool:
    """Linear in the command's length times the segment count: the first segment is
    a prefix, the last a suffix, and each middle one is found left to right after
    the previous, which is exactly what a greedy ``.*`` between literals accepts."""
    if len(segments) == 1:
        return command == segments[0]
    first, *middle, last = segments
    if not command.startswith(first) or len(command) < len(first) + len(last):
        return False
    if not command.endswith(last):
        return False
    pos = len(first)
    end = len(command) - len(last)
    for seg in middle:
        found = command.find(seg, pos, end)
        if found < 0:
            return False
        pos = found + len(seg)
    return True


def _in_set(charset: _CharSet, ch: str) -> bool:
    negated, chars, ranges = charset
    inside = ch in chars or any(lo <= ch <= hi for lo, hi in ranges)
    return inside != negated


#: The characters ``\b`` and ``\B`` read as a word: ASCII is all an
#: ``ascii_only`` entry is matched against.
_WORD_CHARS = frozenset("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz")


def _holds(kind: str, before: str, after: str) -> bool:
    """Whether the assertion *kind* holds between *before* and *after* (``""`` at
    an end of the command)."""
    if kind == "start":
        return not before
    if kind == "end":
        return not after
    if kind == "line_start":
        return before in ("", "\n")
    if kind == "line_end":
        return after in ("", "\n")
    boundary = (before in _WORD_CHARS) != (after in _WORD_CHARS)
    return boundary if kind == "word" else not boundary


def _run(program: tuple[tuple, ...], start: int, command: str, end: int, budget: list[int]) -> bool:
    """Walk *program* over ``command[:end]``; ``False`` once *budget* (shared,
    decremented in place) runs out, so a too-costly match grants nothing."""

    def closure(seeds: list[int], at: int) -> set[int] | None:
        before = command[at - 1] if at > 0 else ""
        after = command[at] if at < len(command) else ""
        seen: set[int] = set()
        stack = list(seeds)
        while stack:
            state = stack.pop()
            if state in seen:
                continue
            budget[0] -= 1
            if budget[0] < 0:
                return None
            seen.add(state)
            node = program[state]
            if node[0] == "split":
                stack.append(node[2])
                stack.append(node[1])
            elif node[0] == "assert" and _holds(node[1], before, after):
                stack.append(node[2])
        return seen

    current = closure([start], 0)
    for at in range(end):
        if not current:
            return False
        ch = command[at]
        step: list[int] = []
        for state in current:
            node = program[state]
            if node[0] != "set":
                continue
            # A class test costs one step per range it compares, so a wide class
            # draws on the budget as hard as many states would.
            budget[0] -= 1 + len(node[1][2])
            if budget[0] < 0:
                return False
            if _in_set(node[1], ch):
                step.append(node[2])
        current = closure(step, at + 1)
    return current is not None and 0 in current


#: Spellings :func:`parse` reads narrower than kiro-cli's Rust engine does, which is
#: safe for a grant and unsafe for a deny: a ``|`` (``\\Aa|b\\z`` is "starts with a or
#: ends with b" in Rust, exactly ``a`` or ``b`` here) and the word boundaries ``\\<``,
#: ``\\>`` and ``\\b{`` (literal text here). Plain substring tests, never a pattern.
#: An ``ascii_only`` entry, the other narrower reading, is unknown for a command
#: holding a non-ASCII character (:func:`denied_match`).
_DENY_UNSAFE = ("|", "\\<", "\\>", "\\b{")


def parse_deny(raw: object) -> Pattern | None:
    """*raw* as a ``deniedCommands`` entry, or ``None`` when :func:`parse` cannot read
    it as wide as kiro-cli does (:data:`_DENY_UNSAFE`), so the caller treats it as
    unknown and grants nothing."""
    if not isinstance(raw, str) or any(piece in raw for piece in _DENY_UNSAFE):
        return None
    return parse(raw)


def denied_match(
    raw_patterns: object, command: object, *, word_bounded: bool = False
) -> bool | None:
    """Whether a ``deniedCommands`` entry names *command*, read WIDER than kiro-cli.

    True when one of *raw_patterns* matches the whole of *command* or any leading
    piece of it, so an entry kiro-cli reads as a prefix is never read narrower
    here. ``None`` -- unknown, so the caller grants nothing -- when the command is
    not :func:`plain_command`, when the match budget runs out, or when no entry
    matches and one could not judge the command: an entry outside the syntax
    :func:`parse` reads, or an ``ascii_only`` entry against a non-ASCII command.
    Such an entry never hides another that matches, so the refusal row still
    refuses what a readable entry names. False only when every entry judged every
    piece.

    *word_bounded* keeps only the pieces that end at a word: the whole command, or
    a prefix followed by a space. The refusal row asks for it, so ``make clean``
    refuses ``make clean now`` but not ``make cleanup``; the grant check reads every
    prefix, which only sends the call back to a prompt.
    """
    entries = list(raw_patterns) if isinstance(raw_patterns, (list, tuple)) else None
    if not isinstance(command, str) or not plain_command(command) or entries is None:
        return None
    parsed = [parse_deny(raw) for raw in entries]
    judging = [p for p in parsed if p is not None and (command.isascii() or not p.ascii_only)]
    budget = [_MAX_MATCH_STEPS]
    for end in range(len(command), 0, -1):
        if word_bounded and end < len(command) and command[end] != " ":
            continue
        for pattern in judging:
            if _match(pattern, command, budget, end):
                return True
            if budget[0] < 0:
                return None
    return False if len(judging) == len(parsed) else None
