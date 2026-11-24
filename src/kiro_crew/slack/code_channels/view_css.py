"""What an html view's CSS may hold: quoted strings only where a browser never shows them.

A CSS string shows as text through ``content``, ``quotes``, ``list-style-type`` and
more, and a custom property carries one to any of them, so the text screen would
never read it. This scanner reads CSS the way a browser tokenizes it (CSS Syntax
Level 3: escapes, comments, strings, ``url(`` and brackets) and allows a non-empty
string only in a selector, the ``@charset`` rule, a ``url()`` (which must be
https:), or a declaration of a property in :data:`STRING_PROPERTIES`, none of
which shows text. The credential screen in ``tools.py`` still reads every string.
Anything it cannot read the way a browser does (an unmatched bracket, a string a
line break cut) is refused rather than guessed at.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

#: Properties whose quoted strings name things and never show as text.
STRING_PROPERTIES = frozenset(
    {
        "font-family", "font", "grid-template-areas", "grid-template", "grid",
        "font-feature-settings", "font-variation-settings",
    }
)  # fmt: skip
#: At-rules whose block, at a stylesheet's top level, holds only rules. Not
#: ``@scope``: declarations directly inside it apply to the scope root.
_GROUP_RULES = frozenset(
    {"media", "supports", "layer", "container", "document", "-moz-document",
     "starting-style", "keyframes", "-webkit-keyframes"}
)  # fmt: skip
#: At-rules refused outright, with what to do instead.
_REFUSED_AT_RULES = {
    "import": "@import; put the stylesheet in a <style> element",
    "font-face": "@font-face; name an installed font in font-family (e.g. system-ui, sans-serif)",
    "counter-style": "@counter-style; use a built-in list-style-type such as disc or decimal",
}
#: Functions that load a URL from their argument.
_URL_FUNCTIONS = frozenset({"url", "src"})
_URL_NOISE = re.compile(r"[\t\n\r]")
_URL_LEAD = re.compile(r"^[\x00-\x20]+")
_CLOSERS = {")": "(", "]": "[", "}": "{"}

_WS = " \t\n"
_HEX = "0123456789abcdefABCDEF"


def _prefix(why: str) -> str:
    return f"an html view's CSS {why}"


_UNMATCHED = _prefix("has an unmatched bracket; balance (), [] and {}")
_NOT_HTTPS = _prefix(
    "may load only https: URLs (no data: or file URLs), and the view loads nothing "
    "from outside the document anyway; drop the URL and style it with CSS colors, "
    "gradients and borders"
)


class _Refusal(Exception):
    pass


def _name_start(c: str) -> bool:
    return c.isascii() and (c.isalpha() or c == "_") or (c != "" and not c.isascii())


def _name_char(c: str) -> bool:
    return _name_start(c) or (c.isascii() and c.isdigit()) or c == "-"


def _valid_escape(c1: str, c2: str) -> bool:
    return c1 == "\\" and c2 != "\n"


def _starts_ident(c1: str, c2: str, c3: str) -> bool:
    if c1 == "-":
        return _name_start(c2) or c2 == "-" or _valid_escape(c2, c3)
    if c1 == "\\":
        return _valid_escape(c1, c2)
    return _name_start(c1)


def _starts_number(c1: str, c2: str, c3: str) -> bool:
    if c1 in "+-" and c1:
        return c2.isdigit() and c2.isascii() or (c2 == "." and c3.isdigit() and c3.isascii())
    if c1 == ".":
        return c2.isdigit() and c2.isascii()
    return c1 != "" and c1.isascii() and c1.isdigit()


class _Tokens:
    """A CSS Syntax Level 3 tokenizer, reduced to the tokens this screen reads.

    Tokens are ``(kind, value)``: ``ws``, ``ident``, ``function``, ``at``,
    ``string``, ``url``, ``(`` ``)`` ``[`` ``]`` ``{`` ``}`` ``;`` ``:``, and
    ``other`` for everything else (numbers, hashes, delimiters).
    """

    def __init__(self, css: str) -> None:
        css = css.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
        self.s = css.replace("\x00", "\ufffd")
        self.i = 0

    def _at(self, k: int = 0) -> str:
        j = self.i + k
        return self.s[j] if j < len(self.s) else ""

    def _escape(self) -> str:
        """After a ``\\``: the escaped code point."""
        c = self._at()
        if c == "":
            return "\ufffd"
        if c in _HEX:
            j = self.i
            while j < len(self.s) and j - self.i < 6 and self.s[j] in _HEX:
                j += 1
            point = int(self.s[self.i : j], 16)
            self.i = j
            if self._at() in _WS and self._at():
                self.i += 1
            return (
                chr(point) if 0 < point <= 0x10FFFF and not 0xD800 <= point <= 0xDFFF else "\ufffd"
            )
        self.i += 1
        return c

    def _ident(self) -> str:
        out: list[str] = []
        while True:
            c = self._at()
            if _name_char(c):
                out.append(c)
                self.i += 1
            elif _valid_escape(c, self._at(1)):
                self.i += 1
                out.append(self._escape())
            else:
                return "".join(out)

    def _string(self, quote: str) -> str:
        out: list[str] = []
        while True:
            c = self._at()
            self.i += 1
            if c == quote or c == "":
                return "".join(out)
            if c == "\n":
                raise _Refusal(
                    _prefix("has a string a line break cuts short; close it on its line")
                )
            if c == "\\":
                nxt = self._at()
                if nxt == "":
                    continue
                if nxt == "\n":
                    self.i += 1
                    continue
                out.append(self._escape())
            else:
                out.append(c)

    def _url(self) -> str:
        while self._at() in _WS and self._at():
            self.i += 1
        out: list[str] = []
        while True:
            c = self._at()
            self.i += 1
            if c in (")", ""):
                return "".join(out)
            if c in _WS:
                while self._at() in _WS and self._at():
                    self.i += 1
                if self._at() in (")", ""):
                    self.i += 1
                    return "".join(out)
                raise _Refusal(_prefix("has a url() it cannot read; quote the https: URL"))
            if c in "\"'(" or (ord(c) < 0x20 and c != "\t") or c == "\x7f":
                raise _Refusal(_prefix("has a url() it cannot read; quote the https: URL"))
            if c == "\\":
                if not _valid_escape(c, self._at()):
                    raise _Refusal(_prefix("has a url() it cannot read; quote the https: URL"))
                out.append(self._escape())
            else:
                out.append(c)

    def _number(self) -> None:
        if self._at() in "+-" and self._at():
            self.i += 1
        while self._at().isdigit() and self._at().isascii():
            self.i += 1
        if self._at() == "." and self._at(1).isdigit() and self._at(1).isascii():
            self.i += 1
            while self._at().isdigit() and self._at().isascii():
                self.i += 1
        c1, c2 = self._at(), self._at(1)
        if (
            c1 in "eE"
            and c1
            and (
                (c2.isdigit() and c2.isascii())
                or (c2 in "+-" and c2 and self._at(2).isdigit() and self._at(2).isascii())
            )
        ):
            self.i += 2
            while self._at().isdigit() and self._at().isascii():
                self.i += 1
        if _starts_ident(self._at(), self._at(1), self._at(2)):
            self._ident()  # a unit: a dimension, never a function
        elif self._at() == "%":
            self.i += 1

    def __iter__(self) -> Iterator[tuple[str, str]]:
        while self.i < len(self.s):
            c, c1, c2 = self._at(), self._at(1), self._at(2)
            if c == "/" and c1 == "*":
                end = self.s.find("*/", self.i + 2)
                self.i = len(self.s) if end < 0 else end + 2
                continue
            if c in _WS:
                while self._at() in _WS and self._at():
                    self.i += 1
                yield ("ws", "")
            elif c in "\"'":
                self.i += 1
                yield ("string", self._string(c))
            elif c in "()[]{};:":
                self.i += 1
                yield (c, c)
            elif c == "<" and self.s.startswith("<!--", self.i):
                self.i += 4
                yield ("ws", "")  # CDO: skipped, as at a stylesheet's top level
            elif c == "-" and c1 == "-" and c2 == ">":
                self.i += 3
                yield ("ws", "")  # CDC
            elif _starts_number(c, c1, c2):
                self._number()
                yield ("other", "")
            elif c == "@" and _starts_ident(c1, c2, self._at(3)):
                self.i += 1
                yield ("at", self._ident().lower())
            elif c == "#" and (_name_char(c1) or _valid_escape(c1, c2)):
                self.i += 1
                self._ident()
                yield ("other", "")
            elif _starts_ident(c, c1, c2):
                name = self._ident()
                if self._at() != "(":
                    yield ("ident", name.lower())
                    continue
                self.i += 1
                lower = name.lower()
                if lower == "url":
                    j = self.i
                    while j < len(self.s) and self.s[j] in _WS:
                        j += 1
                    if j >= len(self.s) or self.s[j] not in "\"'":
                        yield ("url", self._url())
                        continue
                yield ("function", lower)
            else:
                self.i += 1
                yield ("other", "")


def _https(url: str) -> bool:
    return _URL_LEAD.sub("", _URL_NOISE.sub("", url)).lower().startswith("https:")


def string_values(css: str) -> list[str]:
    """Every string and ``url(`` value in *css*, decoded as a browser reads it.

    A ``\\`` + newline inside a string is a line continuation a browser drops,
    so only this reading shows a value split across lines whole.
    """
    values: list[str] = []
    try:
        for kind, value in _Tokens(css):
            if kind in ("string", "url"):
                values.append(value)
    except _Refusal:
        pass  # refusal() refuses this CSS all the same
    return values


def refusal(css: str, *, inline: bool = False) -> str:
    """Why *css* could show author text or load something (``""`` when it can't).

    *inline* reads it as a ``style=`` value: a declaration list with no braces.
    """
    try:
        _scan(css, inline)
    except _Refusal as exc:
        return str(exc)
    return ""


@dataclass
class _Level:
    """One brace level: what it holds, and the statement being read in it."""

    rules: bool  # a rule list (top level, @media, ...) rather than declarations
    head: list[tuple[str, str]] = field(default_factory=list)  # first two tokens
    strings: bool = False  # the statement holds a non-empty string

    def close(self, *, by_block: bool) -> None:
        """End the statement; refuse it if a string there could show as text."""
        head, strings = self.head, self.strings
        self.head, self.strings = [], False
        if not strings:
            return
        kind, name = head[0] if head else ("", "")
        if kind == "at":
            if name != "charset" or not self.rules:
                raise _Refusal(_prefix(f"may not hold a quoted string in @{name}"))
            return
        if self.rules:
            return  # a selector (``input[type="text"]``) never shows its strings
        prop = name if kind == "ident" and len(head) > 1 and head[1][0] == ":" else ""
        # A nested rule's selector may hold strings. One that reads as
        # ``name:`` could also be a declaration with a block in its value (a
        # custom property's may reach ``content`` through var()), so it is held
        # to the property rule.
        if (by_block and not prop) or prop in STRING_PROPERTIES:
            return
        if prop.startswith("--"):
            raise _Refusal(
                _prefix(
                    f"may not hold a quoted string in a custom property ({prop}): var() "
                    "can carry it into content; write the value where it is used "
                    "(name fonts directly in font-family)"
                )
            )
        raise _Refusal(
            _prefix(
                f"may not hold a quoted string in {prop or 'this declaration'} (one there can "
                "show as text or name a file to load); put text to show in the HTML itself. "
                "Quoted strings are fine in "
                f"selectors, url() and {', '.join(sorted(STRING_PROPERTIES))}, and an empty "
                'one (content: "") anywhere'
            )
        )


def _scan(css: str, inline: bool) -> None:
    levels = [_Level(rules=not inline)]
    # Open ( [ and function brackets, and { blocks met inside them. A closer
    # pops only its own opener, so a stray one is refused, never skipped.
    brackets: list[str] = []
    url_arg = ""  # "url"/"src" when the next token is that function's argument

    for kind, value in _Tokens(css):
        if kind == "ws":
            continue
        level = levels[-1]
        if len(level.head) < 2 and not brackets and kind not in ";{}":
            level.head.append((kind, value))
        if kind == "at" and value in _REFUSED_AT_RULES:
            raise _Refusal(_prefix(f"may not use {_REFUSED_AT_RULES[value]}"))
        if kind == "function" and value == "attr":
            raise _Refusal(_prefix("may not use attr(); put text to show in the HTML itself"))
        if kind == "url" and not _https(value):
            raise _Refusal(_NOT_HTTPS)
        if url_arg:
            loader, url_arg = url_arg, ""
            if kind == "string":
                if not _https(value):
                    raise _Refusal(_NOT_HTTPS)
                continue
            if loader == "src":  # src() loads whatever its argument names
                raise _Refusal(_NOT_HTTPS)
        if kind == "function":
            brackets.append("(")
            url_arg = value if value in _URL_FUNCTIONS else ""
        elif kind == "string":
            # An empty string shows nothing (``content: ""`` draws a box).
            level.strings = level.strings or bool(value)
        elif kind in ("(", "["):
            brackets.append(kind)
        elif kind in (")", "]"):
            if not brackets or brackets[-1] != _CLOSERS[kind]:
                raise _Refusal(_UNMATCHED)
            brackets.pop()
        elif kind == "{":
            if brackets:
                # A block inside a function or bracket: read as declarations,
                # stricter than a browser, which keeps it inside the value.
                brackets.append("{")
                levels.append(_Level(rules=False))
                continue
            first = level.head[0] if level.head else ("", "")
            level.close(by_block=True)
            group = level.rules and first[0] == "at" and first[1] in _GROUP_RULES
            levels.append(_Level(rules=group))
        elif kind == "}":
            if brackets and brackets[-1] == "{":
                brackets.pop()
            elif brackets or len(levels) == 1:
                raise _Refusal(_UNMATCHED)
            levels.pop().close(by_block=False)
        elif kind == ";" and not brackets:
            # A declaration and an at-rule end at ``;``; a qualified rule's
            # prelude runs on past it to its block.
            if not level.rules or (level.head and level.head[0][0] == "at"):
                level.close(by_block=False)
    # A browser closes whatever is still open at the end.
    while levels:
        levels.pop().close(by_block=False)
