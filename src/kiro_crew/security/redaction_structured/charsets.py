"""Named character sets and the scanning helpers built on them.

Every helper reads text with ``str`` methods only (``find``, ``lstrip``, ``rstrip``,
``startswith``), which run in C, so a scan costs a bounded number of linear passes.
"""

from __future__ import annotations

import string
from collections.abc import Callable
from typing import Any

DIGITS = string.digits
UPPER = string.ascii_uppercase
LOWER = string.ascii_lowercase
ALNUM = UPPER + LOWER + DIGITS

ALPHABETS: dict[str, str] = {
    "base62": DIGITS + UPPER + LOWER,
    "b64url": ALNUM + "-_",
    "b64std": ALNUM + "+/",
    "hex_lower": DIGITS + "abcdef",
    "hex_any": DIGITS + "abcdefABCDEF",
    "upper_digit": UPPER + DIGITS,
    "slack": ALNUM + "-",
    "digits": DIGITS,
    "upper_space": UPPER + " ",
    "hex_any_dash": DIGITS + "abcdefABCDEF-",
}

BOUNDARIES: dict[str, str] = {
    "word": ALNUM + "_",
    "word_dash": ALNUM + "_-",
    "word_dash_dot": ALNUM + "_.-",
    "aws_key_id_left": ALNUM + "+/_-",
    "aws_key_id_right": ALNUM + "+=_-",
    "b64std": ALNUM + "+/",
    "b64std_pad": ALNUM + "+/=",
    "b64url": ALNUM + "-_",
    "b64url_dot": ALNUM + "-_.",
}

B64STD = ALPHABETS["b64std"]
B64STD_SET = frozenset(B64STD)
B64URL = ALPHABETS["b64url"]
B64URL_SET = frozenset(B64URL)
B64URL_DOT = B64URL + "."
B64URL_DOT_SET = frozenset(B64URL_DOT)
WORD_DASH_SET = frozenset(BOUNDARIES["word_dash"])

_ASCII_LOWER = str.maketrans(UPPER, LOWER)

# Lookups derived from a rows tuple, built once per (rows, builder) and reused on every
# call; keyed by identity and checked against the rows object itself.
_CACHE: dict[Callable[..., Any], dict[int, tuple[object, Any]]] = {}
_CACHE_MAX = 16


#: The last (rows, value) each builder returned: the common case is one table, so one
#: lookup and an identity test answer most calls.
_LAST: dict[Callable[..., Any], tuple[object, Any]] = {}


def cached(rows: object, build: Callable[[Any], Any]) -> Any:
    last = _LAST.get(build)
    if last is not None and last[0] is rows:
        return last[1]
    value = _cached(rows, build)
    _LAST[build] = (rows, value)
    return value


def _cached(rows: object, build: Callable[[Any], Any]) -> Any:
    per = _CACHE.get(build)
    if per is None:
        per = _CACHE[build] = {}
    got = per.get(id(rows))
    if got is not None and got[0] is rows:
        return got[1]
    if len(per) >= _CACHE_MAX:
        per.clear()
    value = build(rows)
    per[id(rows)] = (rows, value)
    return value


def ascii_lower(text: str) -> str:
    """Lower ASCII letters only, so every index of the copy matches the original."""
    if text.isascii():
        # Same result for ASCII text, without a per-character table lookup.
        return text.lower()
    return text.translate(_ASCII_LOWER)


def run_end(text: str, start: int, chars: str, cap: int | None = None) -> int:
    """End of the run of ``chars`` that starts at ``start``, read at most ``cap`` long."""
    n = len(text)
    limit = n if cap is None else min(n, start + cap)
    pos = start
    while pos < limit:
        stop = min(limit, pos + 4096)
        chunk = text[pos:stop]
        rest = chunk.lstrip(chars)
        if rest:
            return pos + len(chunk) - len(rest)
        pos = stop
    return limit


def run_start(text: str, end: int, chars: str, cap: int) -> int:
    """Start of the run of ``chars`` that ends at ``end``, read at most ``cap`` long."""
    lo = max(0, end - cap)
    chunk = text[lo:end]
    return end - (len(chunk) - len(chunk.rstrip(chars)))


class StopScanner:
    """``first_of`` for increasing start positions, reusing the stop-free stretch read.

    Anchors inside a run already read start from where that read ended, so each
    character is searched a bounded number of times however many anchors share a run.
    """

    def __init__(self, text: str, stops: str) -> None:
        self._text = text
        self._stops = stops
        self._lo = -1
        self._hi = -1
        self._stopped = False

    def end(self, start: int, limit: int) -> int:
        n = len(self._text)
        limit = min(limit, n)
        if self._lo <= start <= self._hi:
            if self._stopped or self._hi >= limit:
                return min(self._hi, limit)
            begin = self._hi
        else:
            self._lo = start
            begin = start
        found = first_of(self._text, begin, self._stops, limit)
        self._hi = found
        self._stopped = found < limit or found >= n
        return found


def first_of(text: str, start: int, stops: str, limit: int) -> int:
    """Index of the first character of ``stops`` in ``text[start:limit]``, else ``limit``."""
    limit = min(limit, len(text))
    best = limit
    for ch in stops:
        i = text.find(ch, start, best)
        if i != -1:
            best = i
    return best
