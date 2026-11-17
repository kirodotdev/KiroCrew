"""Detector H: credentials in HTTP headers, cookies, command lines and ``.netrc``.

Each reader anchors on a literal found in the lowered text with ``str.find`` and reads
the value after it by character sets; only the VALUE is reported, the header name, the
scheme, the cookie name, the flag, the keyword and every other field stay.

- ``header``: ``Authorization: Basic <base64>`` (a row's ``scheme``) and API-key
  headers (``x-api-key: <value>``), plain or JSON-quoted (``"x-api-key": "<value>"``);
  a Basic value whose decoded password is a placeholder (``username:password``), and a
  value the row allows (a published JWT example), is a documented example;
- ``cookie``: in a ``Cookie:`` header every ``name=value`` pair, in ``Set-Cookie:``
  the first, whose name marks a session or a token (a CSRF token grants nothing alone);
- ``curl_user``: the password of ``curl -u user:password`` (also ``--user``), only when
  ``curl`` is the command word on that line;
- ``mysql_password``: ``-p<password>`` glued to its flag (a blank after ``-p`` makes the
  client prompt, and the next word names a database) and ``--password=<password>``;
- ``docker_login``: ``-p <password>`` and ``--password <password>`` (or ``=``) of a
  ``login`` subcommand;
- ``netrc``: ``password <value>`` in a ``.netrc`` entry, after ``machine <host>`` or
  ``default`` with only ``login`` and ``account`` pairs between, on one line or across
  lines.

A command-line reader reads a flag only when one of its row's ``names`` is a word of
the same shell command before it (back to a line break, ``|``, ``;`` or ``&``, within
:data:`_LINE_MAX` characters). A value shorter than ``min_length``, a placeholder,
words (``checks.reads_as_words``) or one still running at the end of a stream window
(reported in ``opens``) is not a hit.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable

from .charsets import ALNUM, ascii_lower
from .checks import (
    _SHORTCUT_MIN,
    MASK_CHARS,
    PLACEHOLDER_TRIGGER_WORDS,
    _ValueScan,
    cookie_value_is_placeholder,
    is_placeholder,
    reads_as_words,
    session_name,
)
from .model import PLAN_TAGS, TAIL_LINE, Hit
from .table import HeaderRow

# A name is read where it is not the tail of a longer one: ``x-api-key`` is not ``api-key``,
# ``Set-Cookie`` not ``Cookie``. Punctuation before it is fine (``settled.Authorization``),
# and a scheme header may carry a prefix (``Proxy-Authorization``, ``X-Authorization``).
_NAME_CHARS = frozenset(ALNUM + "-_")
# A space between a scheme and its value: an ASCII blank, a no-break or any other
# Unicode space a copy from a rendered page can carry.
_BLANKS = " \t\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000"
_ASCII_BLANKS = " \t"
_SPACE = frozenset(" \t\r\n")
_QUOTES = "\"'"
_TOKEN = frozenset(ALNUM + "._~+/-=")
_COOKIE_VALUE_STOPS = frozenset(";, \t\r\n\"'")
_WORD_STOPS = frozenset(" \t\r\n\"'")
# A cookie name at most this long is read whole; a longer one (a far ``=`` makes every
# anchor's name reach it) is classified from the line scan without slicing it per anchor.
_NAME_DIRECT_MAX = 256
_CURL_FLAGS = ("-u", "--user")
_MYSQL_FLAGS = ("-p", "--password=")
_DOCKER_FLAGS = ("-p", "--password")
_DOCKER_SUBCOMMAND = "login"
# How far back a command-line reader looks for the command word, and where a shell
# command starts.
_LINE_MAX = 512
_COMMAND_STOPS = "\r\n|;&"
_NETRC_KEYWORD = "password"
_NETRC_ENTRY = "machine"
_NETRC_DEFAULT = "default"
_NETRC_PAIRS = frozenset({"login", "account"})
# Tokens read back from ``password``: ``machine``, its host, two more pairs.
_NETRC_TOKENS = 6
# A Basic value longer than this is not decoded to look for a placeholder.
_BASIC_DECODE_MAX = 512
# How many blanks and quotes a stream holds after a Cookie name while its separator
# has not arrived yet.
_SEPARATOR_MAX = 64
# How much of a running Cookie line a stream holds, from the header name: 8 KiB, a
# common server limit for one request header line.
COOKIE_LINE_HOLD = 8192


class _Open(Exception):
    """The text ends before the unit is decided (non-final scans only)."""


def _skip(text: str, i: int, chars: str) -> int:
    while i < len(text) and text[i] in chars:
        i += 1
    return i


def _after_name(text: str, end: int) -> int | None:
    """Where the value starts after a header name ending at *end*: ``: v``, ``": "v``,
    or an assignment ``= v`` / ``="v"`` (a header set from a variable or a config)."""
    i = end
    if i < len(text) and text[i] in _QUOTES:
        i += 1
    i = _skip(text, i, _BLANKS)
    if i >= len(text) or text[i] not in ":=":
        return None
    i = _skip(text, i + 1, _BLANKS)
    if i < len(text) and text[i] in _QUOTES:
        i += 1
    return i


def _value(text: str, start: int, stops: frozenset[str] | None, final: bool) -> int:
    """End of a value run from *start*: token characters, or up to one of *stops*."""
    i = start
    n = len(text)
    while i < n and (text[i] in _TOKEN if stops is None else text[i] not in stops):
        i += 1
    if i == n and not final:
        raise _Open
    return i


def _keep(text: str, start: int, end: int, row: HeaderRow) -> bool:
    if any(text.startswith(tag, start) for tag in PLAN_TAGS):
        return False
    return end - start >= row.min_length and not is_placeholder(text[start:end])


def _basic_placeholder(value: str) -> bool:
    """A Basic credential whose decoded password is a placeholder (``username:password``)."""
    if len(value) > _BASIC_DECODE_MAX:
        return False
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return False
    _user, colon, password = decoded.partition(":")
    return bool(colon) and is_placeholder(password)


def _header(
    text: str, low: str, at: int, name: str, row: HeaderRow, final: bool, walk: _Walk
) -> Hit | None:
    i = _after_name(text, at + len(name))
    if i is None:
        return None
    if row.scheme:
        if low.startswith(row.scheme, i):
            j = i + len(row.scheme)
            # Blanks, or a line break: a header logged with its value on the next line.
            k = _skip(text, j, _BLANKS)
            if k < len(text) and text[k] in "\r\n":
                k = _skip(text, k, "\r\n")
                k = _skip(text, k, _BLANKS)
            if k == j:
                return None
            i = k
        elif i + len(row.scheme) >= len(text) and not final:
            raise _Open
        else:
            return None
    # ``=`` and ``-`` are token characters, so on ``x-api-key=x-api-key=…`` every anchor's
    # value runs to the same far end: the shared finder and check read that run once.
    end = walk.tokens(i)
    if end == len(text) and not final:
        raise _Open
    if not _walk_keep(text, i, end, row, walk):
        return None
    # A scheme row's value starts after a blank, which ends any earlier value, so these
    # values never overlap and reading each one whole stays linear.
    if row.scheme == "basic" and _basic_placeholder(text[i:end]):
        return None
    if row.allow_values and text[i:end].rsplit(".", 1)[0] in row.allow_values:
        return None
    return Hit(i, end, row.id, row.tier, "header", "known_key")


class _Next:
    """The first index at or after ``i`` of any of *chars* (``len(text)`` if none).

    Each character keeps its last answer: asks come at rising anchors and walk forward,
    so most land inside a stretch already read, where that character is not, and cost
    nothing; a miss reads once from ``i`` with ``str.find``.
    """

    __slots__ = ("_asked", "_chars", "_found", "_text")

    def __init__(self, text: str, chars: str) -> None:
        self._text = text
        self._chars = chars
        self._asked = [-1] * len(chars)
        self._found = [-1] * len(chars)

    def __call__(self, i: int) -> int:
        best = len(self._text)
        for k, ch in enumerate(self._chars):
            if not 0 <= self._asked[k] <= i <= self._found[k]:
                at = self._text.find(ch, i)
                self._asked[k], self._found[k] = i, (len(self._text) if at == -1 else at)
            best = min(best, self._found[k])
        return best


class _WordNext:
    """The end (start + length) of the earliest of *words* beginning at or after ``i``
    in the lowered text (``len(low) + 1`` if none). ``cookie`` anchors rise, so each word
    keeps its last start and a question inside that stretch costs nothing.

    The words are ASCII (:data:`~.checks.PLACEHOLDER_TRIGGER_WORDS`), so the ASCII-lowered
    ``low`` the engine already built finds them wherever ``str.casefold`` would form one.
    A non-ASCII character can fold to a string holding one of these letters, but only with a
    combining or modifier mark beside it (``\u1e99`` -> ``y``-plus-mark, ``\u0149`` -> ``\u02bcn``),
    so none of these words ever folds together contiguously out of non-ASCII text; and where
    ``low`` differs from ``casefold`` it only misses a fold, which can only send a value to
    the full check, never settle it wrongly.
    """

    __slots__ = ("_asked", "_low", "_start", "_words")

    def __init__(self, low: str, words: tuple[str, ...]) -> None:
        self._low = low
        self._words = words
        self._asked = [-1] * len(words)
        self._start = [-1] * len(words)

    def __call__(self, i: int) -> int:
        n = len(self._low)
        best = n + 1
        for k, word in enumerate(self._words):
            if not 0 <= self._asked[k] <= i <= self._start[k]:
                at = self._low.find(word, i)
                self._asked[k], self._start[k] = i, (n if at == -1 else at)
            if self._start[k] < n:
                best = min(best, self._start[k] + len(word))
        return best


class _RunEnd:
    """The first index at or after ``i`` whose character is not one of *chars* (``_skip``).
    Queries come at rising positions, so the run between two of them is read once in total,
    not once per anchor — the per-anchor ``_skip`` of blanks was quadratic on a line of
    ``name:`` + a long blank run repeated, and the per-anchor token run of a header value on
    ``x-api-key=`` repeated."""

    __slots__ = ("_asked", "_chars", "_found", "_text")

    def __init__(self, text: str, chars: str | frozenset[str]) -> None:
        self._text = text
        self._chars = chars
        self._asked = -1
        self._found = -1

    def __call__(self, i: int) -> int:
        if not 0 <= self._asked <= i <= self._found:
            text = self._text
            chars = self._chars
            n = len(text)
            j = i
            while j < n and text[j] in chars:
                j += 1
            self._asked, self._found = i, j
        return self._found


class _Walk:
    """What one reader's scan (one row and name) shares between its anchors, so the scan
    stays linear. The cookie, header and ``curl -u`` readers use it.

    For a cookie, a walk from a pair start reads the same pairs whatever anchor reached it: the line
    end, the ``final`` flag and every step after are fixed by the position alone. Each
    pair start a walk reads is kept with how the walk ended (``True``: it raised
    :class:`_Open`). A later walk that reaches one stops there, or raises, as that walk
    did; the hits after it are already in the list.

    The finders (``breaks``, ``eqs``, ``semis``, ``stops``, ``masks``, ``words``,
    ``nonblank``, ``tokens``, ``colons``, ``word_stops``) and the per-line
    :class:`~.checks._ValueScan` answer every "read from this anchor to a far point"
    question — the value, the cookie name, the blanks, the command words — from shared
    state, so no part of the reader is re-read once per anchor.
    """

    __slots__ = (
        "_cur_hi",
        "_cur_lo",
        "_head_lo",
        "_head_words",
        "_scan",
        "_spent",
        "breaks",
        "colons",
        "eqs",
        "masks",
        "nonblank",
        "quoted",
        "semis",
        "stops",
        "text",
        "tokens",
        "walked",
        "word_stops",
        "words",
    )

    def __init__(self, text: str, low: str) -> None:
        self.text = text
        self.breaks = _Next(text, "\r\n")
        self.eqs = _Next(text, "=")
        self.semis = _Next(text, ";")
        self.stops = _Next(text, "".join(sorted(_COOKIE_VALUE_STOPS)))
        self.masks = _Next(text, MASK_CHARS)
        self.words = _WordNext(low, PLACEHOLDER_TRIGGER_WORDS)
        self.nonblank = _RunEnd(text, _BLANKS)
        self.tokens = _RunEnd(text, _TOKEN)
        self.colons = _Next(text, ":")
        self.word_stops = _Next(text, "".join(sorted(_WORD_STOPS)))
        self.quoted = {q: _Next(text, q + "\r\n") for q in _QUOTES}
        self.walked: dict[int, bool] = {}
        self._scan: _ValueScan | None = None
        self._spent = 0
        self._cur_lo = 0
        self._cur_hi = -1
        self._head_lo = -1
        self._head_words: list[tuple[int, int]] = []

    def _enter(self, pos: int) -> None:
        """Make the value's own line current. Lines are entered in rising order and never
        revisited, so the span is recomputed only on crossing into a new line: the forward
        end comes from the amortized ``breaks`` finder, and the backward line-start search is
        bounded below by the previous line's end, so each character is read at most once."""
        if not self._cur_lo <= pos < self._cur_hi:
            text = self.text
            # The previous line's end bounds the backward search only when *pos* is past it,
            # so a question out of line order still gets its own line start.
            lower = self._cur_hi if 0 < self._cur_hi <= pos else 0
            self._cur_lo = max(text.rfind("\n", lower, pos), text.rfind("\r", lower, pos)) + 1
            self._cur_hi = self.breaks(pos)
            self._scan = None
            self._spent = 0

    def _line_scan(self) -> _ValueScan:
        """The current line's scan, built once and shared across its anchors (a cookie value
        or name never crosses a line break). Only the current line's scan is kept, so memory
        stays bounded by the longest line."""
        if self._scan is None:
            self._scan = _ValueScan(self.text[self._cur_lo : self._cur_hi])
        return self._scan

    def after_name(self, end: int) -> int | None:
        """:func:`_after_name` for the cookie path, with the two blank skips bounded by the
        shared ``nonblank`` finder so ``name:`` + a long blank run repeated stays linear."""
        text = self.text
        i = end
        if i < len(text) and text[i] in _QUOTES:
            i += 1
        i = self.nonblank(i)
        if i >= len(text) or text[i] not in ":=":
            return None
        i = self.nonblank(i + 1)
        if i < len(text) and text[i] in _QUOTES:
            i += 1
        return i

    def session(self, i: int, eq: int) -> bool:
        """``session_name(text[i:eq].strip())``. A short name is read whole. A long one goes
        to the full ``session_name`` too (``O(name)`` once, what the pre-change code cost for
        one anchor) under the same per-line budget as :meth:`classify`: only when overlapping
        anchors re-read the same far name past the budget is the line scan built and the name
        answered from it. A scan already built for this line is used straight away."""
        if eq - i <= _NAME_DIRECT_MAX:
            return session_name(self.text[i:eq].strip())
        self._enter(i)
        if self._scan is not None:
            return self._scan.session_name_at(i - self._cur_lo, eq - self._cur_lo)
        self._spent += eq - i
        if self._spent <= 2 * (self._cur_hi - self._cur_lo):
            return session_name(self.text[i:eq].strip())
        return self._line_scan().session_name_at(i - self._cur_lo, eq - self._cur_lo)

    def classify(self, vs: int, ve: int) -> bool:
        """``is_placeholder(text[vs:ve])`` for a long value that holds a mask or placeholder
        feature.

        Values go to the full :func:`~.checks.is_placeholder` (``O(value)`` each, what the
        pre-change code cost) until the total value length checked on the current line passes
        twice the line's length. Values that do not overlap sum to at most the line's length,
        so an ordinary line never builds a scan and matches the old cost. Only overlapping
        values (the same stretch read again from a later anchor, the quadratic shape) push
        past the budget; by then the work spent is already ``O(line)``, so building the line
        scan once and reusing it keeps the whole line linear. A scan already built for this
        line (by a long cookie name) is used straight away."""
        self._enter(vs)
        if self._scan is not None:
            return self._scan.is_placeholder_at(vs - self._cur_lo, ve - self._cur_lo)
        self._spent += ve - vs
        if self._spent <= 2 * (self._cur_hi - self._cur_lo):
            return is_placeholder(self.text[vs:ve])
        return self._line_scan().is_placeholder_at(vs - self._cur_lo, ve - self._cur_lo)

    def runs_curl(self, at: int) -> bool:
        """Whether one of the first three words of ``text[line_start:at]`` (``|`` read as a
        blank) is ``curl`` or ends in ``/curl``, where ``line_start`` follows the last line
        break before *at*.

        Those words are the line's own first three words, the last one cut at *at*: a blank
        or ``|`` splits the head exactly where it splits the whole line. So the line's first
        three word spans are found once per line (one ``split``), and each anchor reads at
        most three spans instead of slicing and splitting its whole head."""
        self._enter(at)
        lo = self._cur_lo
        if self._head_lo != lo:
            line = self.text[lo : self._cur_hi].replace("|", " ")
            spans: list[tuple[int, int]] = []
            p = 0
            for word in line.split(None, 3)[:3]:
                # A word holds no blank and only blanks come before it, so its first match
                # at or after the previous word's end is where it starts.
                p = line.find(word, p)
                spans.append((lo + p, lo + p + len(word)))
                p += len(word)
            self._head_lo, self._head_words = lo, spans
        text = self.text
        for ws, we in self._head_words:
            if ws >= at:
                break
            e = min(we, at)
            if (e - ws == 4 and text.startswith("curl", ws)) or (
                e - ws >= 5 and text.startswith("/curl", e - 5)
            ):
                return True
        return False


def _walk_keep(text: str, start: int, end: int, row: HeaderRow, walk: _Walk) -> bool:
    """:func:`_keep` for a value with no line break in it (a cookie, header or ``curl -u``
    value), with the placeholder check bounded per anchor."""
    if any(text.startswith(tag, start) for tag in PLAN_TAGS):
        return False
    if end - start < row.min_length:
        return False
    if end - start < _SHORTCUT_MIN:
        # Short enough that the full check is cheap and bounded; the mask/word lookups it
        # would feed are not worth their per-anchor cost on a line of short cookies.
        return not is_placeholder(text[start:end])
    has_mask = walk.masks(start) < end
    has_trigger = walk.words(start) <= end
    return not cookie_value_is_placeholder(text, start, end, has_mask, has_trigger, walk.classify)


def opens_cookie_header(text: str, start: int, end: int) -> bool:
    """Whether a ``Cookie`` header name in ``text[start:end]`` reads as one, by the
    cookie reader's own grammar: not the tail of a longer name (``Set-Cookie``), and
    followed by its separator (:func:`_after_name`: a quote, blanks, ``:`` or ``=``)."""
    low = ascii_lower(text[start:end])
    at = low.find("cookie")
    while at != -1:
        pos = start + at
        left_ok = pos == 0 or text[pos - 1] not in _NAME_CHARS
        if left_ok and _after_name(text, pos + len("cookie")) is not None:
            return True
        at = low.find("cookie", at + 1)
    return False


def _cookies(text: str, at: int, name: str, row: HeaderRow, final: bool, walk: _Walk) -> list[Hit]:
    i = walk.after_name(at + len(name))
    if i is None:
        # The window ends between the name and its separator: the separator
        # still decides whether the line is a Cookie header, so hold the name.
        # The shared ``nonblank`` finder reads the run, so anchors stay linear.
        end = at + len(name)
        k = end + 1 if end < len(text) and text[end] in _QUOTES else end
        if not final and walk.nonblank(k) == len(text):
            if len(text) - end <= _SEPARATOR_MAX:
                raise _Open
            # Past the bound the separator is still undecided: fail closed and
            # drop the rest of the line rather than release the name.
            return [Hit(end, len(text), row.id, row.tier, "cookie", "known_key", tail=TAIL_LINE)]
        return []
    n = len(text)
    line_end = walk.breaks(i)
    hits: list[Hit] = []
    first_only = name == "set-cookie"
    if not final and line_end == n and not first_only and n - at >= COOKIE_LINE_HOLD:
        # A running Cookie line is held from its name for at most COOKIE_LINE_HOLD
        # characters; past that the rest of the line is a Secret (fail closed) and
        # the stream drops it to its CR or LF.
        return [Hit(i, n, row.id, row.tier, "cookie", "known_key", tail=TAIL_LINE)]
    # The header name decides a Cookie pair, so a stream keeps it uncommitted until
    # the pair is: a later window must read the pair from it again. Set-Cookie reads
    # only the pair right after its name, so it needs no anchor.
    anchor = -1 if first_only else at
    seen: list[int] = []
    try:
        while i < line_end:
            if not first_only:
                if i in walk.walked:
                    if walk.walked[i]:
                        raise _Open
                    break
                seen.append(i)
            eq = walk.eqs(i)
            if eq >= line_end:
                break
            vs = eq + 1
            ve = walk.stops(vs)
            if ve == n and not (final or line_end < n):
                raise _Open
            if walk.session(i, eq) and _walk_keep(text, vs, ve, row, walk):
                hits.append(Hit(vs, ve, row.id, row.tier, "cookie", "known_key", anchor=anchor))
            if first_only:
                break
            semi = walk.semis(ve)
            if semi >= line_end:
                break
            i = walk.nonblank(semi + 1)
        if not final and line_end == n and not first_only:
            # The header line is still running: a later pair is read from the header
            # name, so the name is held until the line ends.
            raise _Open
    except _Open:
        walk.walked.update(dict.fromkeys(seen, True))
        raise
    walk.walked.update(dict.fromkeys(seen, False))
    return hits


def _curl_user(text: str, at: int, row: HeaderRow, final: bool, walk: _Walk) -> Hit | None:
    """``curl ... -u user:password``: the flag at *at* follows ``curl`` on its line."""
    if not walk.runs_curl(at):
        return None
    flag_end = at + (2 if text.startswith("-u", at) else len("--user"))
    if flag_end < len(text) and text[flag_end] == "=":
        i = flag_end + 1
    else:
        i = _skip(text, flag_end, _BLANKS)
        if i == flag_end:
            return None
    quote = text[i] if i < len(text) and text[i] in _QUOTES else ""
    if quote:
        i += 1
    # Stops: the closing quote or a line break, else a blank or a quote. On ``-u=-u=…``
    # every anchor's value runs to the same far stop, so the shared finders read it once.
    end = (walk.quoted[quote] if quote else walk.word_stops)(i)
    if end == len(text) and not final:
        raise _Open
    colon = walk.colons(i)
    if colon >= end or not _walk_keep(text, colon + 1, end, row, walk):
        return None
    return Hit(colon + 1, end, row.id, row.tier, "curl_user", "known_key")


def _command_words(text: str, at: int) -> list[str]:
    """The words of the shell command before *at* (module docstring)."""
    lo = max(0, at - _LINE_MAX)
    start = max(text.rfind(ch, lo, at) for ch in _COMMAND_STOPS) + 1
    return text[max(start, lo) : at].split()


def _runs(words: list[str], names: tuple[str, ...]) -> bool:
    return any(w.rsplit("/", 1)[-1] in names for w in words)


def _word_value(text: str, i: int, row: HeaderRow, final: bool, detector: str) -> Hit | None:
    """The word at *i*, or the text inside the quotes there, as a Secret hit."""
    if i >= len(text):
        if not final:
            raise _Open
        return None
    quote = text[i] if text[i] in _QUOTES else ""
    if quote:
        i += 1
    end = _value(text, i, frozenset(quote + "\r\n") if quote else _WORD_STOPS, final)
    if not _keep(text, i, end, row) or reads_as_words(text[i:end]):
        return None
    return Hit(i, end, row.id, row.tier, detector, "known_key")


def _flag(text: str, at: int, flags: tuple[str, ...], final: bool) -> str | None:
    """The flag of *flags* at *at* (case as written), and the text after it is there."""
    for flag in flags:
        if text.startswith(flag, at):
            if at + len(flag) >= len(text):
                if not final:
                    raise _Open
                return None
            return flag
    return None


def _mysql_password(text: str, at: int, row: HeaderRow, final: bool) -> Hit | None:
    """``mysql ... -p<password>`` or ``--password=<password>`` (module docstring)."""
    flag = _flag(text, at, _MYSQL_FLAGS, final)
    if flag is None:
        return None
    i = at + len(flag)
    if text[i] in _SPACE or not _runs(_command_words(text, at), row.names):
        return None
    return _word_value(text, i, row, final, "cli_password")


def _docker_login(text: str, at: int, row: HeaderRow, final: bool) -> Hit | None:
    """``docker login ... -p <password>`` or ``--password <password>`` (also ``=``)."""
    flag = _flag(text, at, _DOCKER_FLAGS, final)
    if flag is None:
        return None
    i = at + len(flag)
    if text[i] == "=":
        i += 1
    elif text[i] in _ASCII_BLANKS:
        i = _skip(text, i, _ASCII_BLANKS)
    elif flag != "-p":
        return None  # a longer flag (``--password-stdin``)
    words = _command_words(text, at)
    if _DOCKER_SUBCOMMAND not in words or not _runs(words, row.names):
        return None
    if i < len(text) and text[i] in _SPACE:
        return None
    return _word_value(text, i, row, final, "cli_password")


def _netrc_entry(text: str, at: int) -> bool:
    """Whether the tokens before *at* are a ``.netrc`` entry's (module docstring)."""
    tokens = text[max(0, at - _LINE_MAX) : at].split()[-_NETRC_TOKENS:]
    k = len(tokens)
    while k > 0:
        if tokens[k - 1] == _NETRC_DEFAULT:
            return True
        if k < 2:
            return False
        keyword = tokens[k - 2]
        if keyword == _NETRC_ENTRY:
            return True
        if keyword not in _NETRC_PAIRS:
            return False
        k -= 2
    return False


def _netrc(text: str, at: int, row: HeaderRow, final: bool) -> Hit | None:
    """``password <value>`` in a ``.netrc`` entry (module docstring)."""
    if not text.startswith(_NETRC_KEYWORD, at) or (at > 0 and text[at - 1] not in _SPACE):
        return None
    i = at + len(_NETRC_KEYWORD)
    if i >= len(text):
        if not final:
            raise _Open
        return None
    if text[i] not in _ASCII_BLANKS:
        return None
    i = _skip(text, i, _ASCII_BLANKS)
    if not _netrc_entry(text, at):
        return None
    if i < len(text) and text[i] in _SPACE:
        return None
    return _word_value(text, i, row, final, "netrc")


_Reader = Callable[[str, int, HeaderRow, bool], "Hit | None"]
#: Readers that anchor on their own fixed literals, and those literals. ``curl_user`` also
#: anchors on its flags but shares a :class:`_Walk`, so ``detect_headers`` calls it itself.
_FIXED: dict[str, tuple[tuple[str, ...], _Reader]] = {
    "mysql_password": (_MYSQL_FLAGS, _mysql_password),
    "docker_login": (_DOCKER_FLAGS, _docker_login),
    "netrc": ((_NETRC_KEYWORD,), _netrc),
}
#: Readers that share one :class:`_Walk` between the anchors of a row's name.
_WALKED = frozenset({"header", "cookie", "curl_user"})


def detect_headers(
    text: str, rows: tuple[HeaderRow, ...], final: bool, low: str
) -> tuple[list[Hit], list[int]]:
    hits: list[Hit] = []
    opens: list[int] = []
    for row in rows:
        fixed = _FIXED.get(row.reader)
        names: tuple[str, ...]
        if row.reader == "curl_user":
            names = _CURL_FLAGS
        else:
            names = fixed[0] if fixed is not None else row.names
        for name in names:
            walk: _Walk | None = None
            at = low.find(name)
            while at != -1:
                left_ok = (
                    at == 0
                    or text[at - 1] not in _NAME_CHARS
                    or (bool(row.scheme) and text[at - 1] == "-")
                )
                try:
                    if left_ok and row.reader in _WALKED:
                        if walk is None:
                            walk = _Walk(text, low)
                        if row.reader == "cookie":
                            hits += _cookies(text, at, name, row, final, walk)
                        else:
                            found = (
                                _header(text, low, at, name, row, final, walk)
                                if row.reader == "header"
                                else _curl_user(text, at, row, final, walk)
                            )
                            if found is not None:
                                hits.append(found)
                    elif left_ok and fixed is not None:
                        found = fixed[1](text, at, row, final)
                        if found is not None:
                            hits.append(found)
                except _Open:
                    opens.append(at)
                at = low.find(name, at + 1)
    return hits, opens
