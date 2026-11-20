"""Format checks a row names by id, and the placeholder / allow checks (REQ-F3)."""

from __future__ import annotations

import base64
import binascii
import bisect
import json
import zlib
from array import array
from collections.abc import Callable
from typing import Any

from .charsets import ALPHABETS

_BASE62 = ALPHABETS["base62"]
_GITHUB_PREFIX_LEN = 4
_GITHUB_BODY_LEN = 30
_GITHUB_CHECK_LEN = 6
_DECODE_CAP = 16384


def base62_crc32(body: str) -> str:
    """The six-character base62 CRC32 a GitHub token appends to its body."""
    crc = zlib.crc32(body.encode("ascii"))
    digits = ""
    while crc:
        crc, rem = divmod(crc, 62)
        digits = _BASE62[rem] + digits
    return digits.rjust(_GITHUB_CHECK_LEN, "0")


def github_crc32_ok(token: str) -> bool:
    body = token[_GITHUB_PREFIX_LEN : _GITHUB_PREFIX_LEN + _GITHUB_BODY_LEN]
    check = token[_GITHUB_PREFIX_LEN + _GITHUB_BODY_LEN :]
    if len(body) != _GITHUB_BODY_LEN or len(check) != _GITHUB_CHECK_LEN:
        return False
    return base62_crc32(body) == check


CHECKSUMS = {"github_crc32": github_crc32_ok}


def b64url_json_object(segment: str) -> dict[str, Any] | None:
    """Decode a base64url segment (size-capped) to a JSON object, else ``None``."""
    if len(segment) > _DECODE_CAP:
        return None
    try:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        decoded = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, RecursionError):
        return None
    return decoded if isinstance(decoded, dict) else None


# ── Placeholders and allow entries ──

ALLOW_WORDS = frozenset(
    {
        "changeme",
        "placeholder",
        "example",
        "true",
        "false",
        "null",
        "none",
        "required",
        "optional",
        "redacted",
        "unset",
        "empty",
        "string",
        "secret",
        "password",
    }
)
#: Words a documented example writes where the value goes (``sk_test_YOUR_KEY_HERE``,
#: ``Bearer your-token-here``), read as whole parts of the value.
PLACEHOLDER_PARTS = frozenset({"your", "here", "placeholder", "changeme"})
#: The same for a token-format body after its prefix (``xoxb-slack-bot-token``).
BODY_PLACEHOLDER_PARTS = PLACEHOLDER_PARTS | {"token"}
#: A run of one mask character this long marks an elided value, and a ``your`` with one
#: of these words after it a written-out one, even where a renderer dropped the
#: separators (``ghpxxxxxxxx``, ``sktestYOURKEYHERE``).
MASK_RUN_MIN = 8
_YOUR_TAILS = ("here", "key", "token", "secret", "password")
_EXAMPLE_SUFFIXES = ("EXAMPLEKEY", "EXAMPLE")
_MASK_CHARS = frozenset("*xX.\u2026")
_ENV_HEAD = frozenset(ALPHABETS["upper_digit"].replace("0123456789", "") + "_")
_ENV_TAIL = frozenset(ALPHABETS["upper_digit"] + "_")
_IDENT = frozenset(ALPHABETS["b64url"])
_REF_HEAD = frozenset(ALPHABETS["base62"].replace("0123456789", "") + "_")
_REF_TAIL = frozenset(ALPHABETS["base62"] + "_")
# Where a value is cut into parts: ``-``, ``_``, ``.`` and a blank.
_PART_BREAKS = str.maketrans("-_.", "   ")
_LOWER = frozenset("abcdefghijklmnopqrstuvwxyz")
_WORD_CHARS = _LOWER | frozenset("-_. ")
_VOWELS = frozenset("aeiouy")
#: Longer than most words a config or a document writes; a lower-case part longer than
#: this reads as generated (a 16-letter app password).
WORD_MAX = 14


def _braced(value: str) -> bool:
    if value.startswith("${") and value.endswith("}") and len(value) > 3:
        inner = value[2:-1]
        return "{" not in inner and "}" not in inner and "\n" not in inner
    body = value[1:] if value.startswith("$") else value
    if body.startswith("{{") and body.endswith("}}") and len(body) > 4:
        inner = body[2:-2]
        return "}}" not in inner and "\n" not in inner
    return False


def is_placeholder(value: str) -> bool:
    """A documented example value or placeholder, recognised without any pattern."""
    v = value.strip()
    if len(v) < 4:
        return True
    folded = v.casefold()
    if folded in ALLOW_WORDS:
        return True
    if v.endswith(_EXAMPLE_SUFFIXES):
        return True
    if folded.startswith(("your-", "your_")):
        return True
    if v[0] == "<" and v[-1] == ">" and not (set(v[1:-1]) & {"<", ">", "\n", "\r"}):
        return True
    if _braced(v):
        return True
    if v[0] == "$" and len(v) > 1 and v[1] in _ENV_HEAD and set(v[2:]) <= _ENV_TAIL:
        return True
    # A shell or template variable of any case (``"$password"``).
    if v[0] == "$" and len(v) > 1 and v[1] in _REF_HEAD and set(v[2:]) <= _REF_TAIL:
        return True
    if v[0] == "%" == v[-1] and len(v) > 2 and set(v[1:-1]) <= _IDENT:
        return True
    if len(v) >= 3 and len(set(v)) == 1 and v[0] in _MASK_CHARS:
        return True
    if any(ch * MASK_RUN_MIN in folded for ch in "x*\u2026"):
        return True
    your = folded.find("your")
    if your != -1 and any(tail in folded[your + 4 :] for tail in _YOUR_TAILS):
        return True
    return _has_placeholder_part(folded, PLACEHOLDER_PARTS)


def _has_placeholder_part(folded: str, words: frozenset[str]) -> bool:
    """Whether a part of *folded* is a placeholder word or a mask run (``ghp_xxxx``)."""
    for part in folded.translate(_PART_BREAKS).split():
        if part in words or (len(part) >= 3 and len(set(part)) == 1 and part[0] in _MASK_CHARS):
            return True
    return False


#: The mask characters :func:`is_placeholder` keys its mask-run paths on, as a string a
#: scanner can pass to ``str.find``. A value free of every one cannot be a mask run of any
#: kind. Derived from :data:`_MASK_CHARS` so the two cannot drift apart.
MASK_CHARS = "".join(sorted(_MASK_CHARS))
#: The ASCII-folded words a value's parts (and the ``your <tail>`` form) are read for. A
#: value free of every one keys none of :func:`is_placeholder`'s word paths. These are
#: exactly the placeholder part words (:data:`PLACEHOLDER_PARTS`); the ``your <tail>`` form
#: also needs ``your``, which is already one of them, so no tail word is needed here.
PLACEHOLDER_TRIGGER_WORDS = tuple(sorted(PLACEHOLDER_PARTS))
#: A value this long or longer is never one of the short all-value forms (an allow word,
#: an env/brace reference, a single mask run), so those need no check once it is reached.
_SHORTCUT_MIN = 33
#: A value shorter than this is read by the full :func:`is_placeholder` even when it holds
#: a feature: that is ``O(value)`` and a value this short is bounded, so a line of them stays
#: linear without the per-line cost of building a :class:`_ValueScan`. The shared scan pays
#: off only for a long value repeated across many anchors (the quadratic shape), where one
#: value runs for much of the text. Real session cookies fall under it, so an ordinary page
#: builds no scan. Any constant keeps the work linear; this one just trades a little
#: ``is_placeholder`` work for far fewer scan builds.
_SCAN_MIN = 512
_EXAMPLE_SUFFIX_MAX = max(len(s) for s in _EXAMPLE_SUFFIXES)
_MAX_ALLOW = max(len(w) for w in ALLOW_WORDS)
_MAX_PART_WORD = max(len(w) for w in PLACEHOLDER_PARTS)
#: First characters that key a whole-value placeholder form (``<...>``, ``${...}``,
#: ``{{...}}``, ``%ENV%``, ``$ENV``). A value starting with none of them keys no such form.
_FIRST_CHAR_FORMS = frozenset("<${%")
#: Mask characters a run of :data:`MASK_RUN_MIN` of which marks a value (lower ``x``, the
#: folded form of ``X``, ``*`` and ``…``). ``.`` is not one, matching ``is_placeholder``.
_MASK_RUN_CHARS = ("x", "*", "\u2026")


def _is_break(ch: str) -> bool:
    """A character ``is_placeholder`` cuts a value into parts on (``str.split`` after the
    ``-_.`` → space translation): ``-``, ``_``, ``.`` and any whitespace."""
    return ch in "-_." or ch.isspace()


# ── Cookie name classification (shared with the header reader) ──

#: A name holding one of these grants nothing on its own, so it is not a session cookie.
_FORGERY_WORDS = ("csrf", "xsrf")
_SESSION_WORDS = ("session", "sess", "token", "auth")
#: ``session`` contains ``sess``, so for a long name (no exact match possible) the session
#: test reduces to these three substrings.
_SESSION_SUBWORDS = ("sess", "token", "auth")
_SESSION_NAMES = frozenset({"sid", "phpsessid", "jsessionid"})
#: A name at most this long is classified by reading it whole (bounded, and only a short
#: name can be an exact session name); a longer one is answered from shared positions.
_SESSION_NAME_MAX = 64


def session_name(name: str) -> bool:
    """Whether a cookie ``name`` marks a session or token (not a bare forgery token)."""
    low = name.lower()
    if any(word in low for word in _FORGERY_WORDS):
        return False
    return low in _SESSION_NAMES or any(word in low for word in _SESSION_WORDS)


def _word_hits(s: str, words: tuple[str, ...]) -> "tuple[list[int], list[int]]":
    """Sorted start positions of every occurrence of any of *words* in *s*, with a
    suffix-minimum of the occurrence ends, so "is any word fully within ``[a, b)``" is one
    ``bisect`` then one lookup."""
    hits = sorted((k, k + len(w)) for w in words for k in _all_finds(s, w))
    starts = [s0 for s0, _ in hits]
    suffix_min = [len(s) + 1] * (len(hits) + 1)
    for i in range(len(hits) - 1, -1, -1):
        suffix_min[i] = min(suffix_min[i + 1], hits[i][1])
    return starts, suffix_min


def _any_in(starts: list[int], suffix_min_end: list[int], a: int, b: int) -> bool:
    """Whether any word occurrence recorded in *starts* lies fully within ``[a, b)``."""
    k = bisect.bisect_left(starts, a)
    return suffix_min_end[k] <= b


class _ValueScan:
    """Answers ``is_placeholder(text[vs:ve])`` for a cookie value in ``O(log n)`` after one
    pass over *text*, so a line that repeats a cookie anchor costs linear work even when the
    values hold mask characters or placeholder words (which the cheap proof cannot settle).

    It mirrors :func:`is_placeholder` branch for branch, in the same order; the only
    difference is HOW each "does the value hold feature X anywhere" question is answered —
    from positions found once over the whole text, never by re-reading the value at every
    anchor. The folded value for ``[vs, ve)`` is ``folded[fstart[vs]:fstart[ve]]`` because
    ``str.casefold`` folds each character on its own, so a per-character offset map is exact.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        n = len(text)
        # ``array('l')`` not a list of ints: these are one entry per character and the scan
        # is built for a value that runs for much of a (possibly multi-megabyte) line, so
        # eight bytes an entry instead of a boxed int keeps the memory bounded.
        fstart = array("l", [0]) * (n + 1)
        parts: list[str] = []
        pos = 0
        for i, ch in enumerate(text):
            fstart[i] = pos
            f = ch.casefold()
            parts.append(f)
            pos += len(f)
        fstart[n] = pos
        folded = "".join(parts)
        self.fstart = fstart
        self.folded = folded
        m = len(folded)
        # First non-whitespace at or after i, and last non-whitespace before i: the two
        # ends str.strip() would move to, in O(1) per value.
        fnw = array("l", [0]) * (n + 1)
        nxt = n
        for i in range(n - 1, -1, -1):
            if not text[i].isspace():
                nxt = i
            fnw[i] = nxt
        fnw[n] = n
        self.fnw = fnw
        lnw = array("l", [0]) * (n + 1)
        last = -1
        for i in range(n):
            lnw[i] = last
            if not text[i].isspace():
                last = i
        lnw[n] = last
        self.lnw = lnw
        # End of the run of the character at i (raw and folded): a single-character value
        # and a same-character part are read from it without scanning the run again.
        self.rend = _run_ends(text, n)
        self.frend = _run_ends(folded, m)
        # Positions of every feature is_placeholder looks for "anywhere" in a value.
        self.eights = _starts_of_runs(folded, _MASK_RUN_CHARS, MASK_RUN_MIN)
        self.yours = _all_finds(folded, "your")
        tails = sorted((k, k + len(w)) for w in _YOUR_TAILS for k in _all_finds(folded, w))
        self.tail_starts = [s for s, _ in tails]
        suffix_min = [m + 1] * (len(tails) + 1)
        for i in range(len(tails) - 1, -1, -1):
            suffix_min[i] = min(suffix_min[i + 1], tails[i][1])
        self.tail_suffix_min_end = suffix_min
        self.breaks = [i for i, c in enumerate(folded) if _is_break(c)]
        self.pos_part_starts = _positive_part_starts(folded, m)
        # Built on first use (an env/ref/%% value, or a long cookie name): a backward
        # "first character outside the set" array per charset, and a ``str.lower`` fold map
        # with the cookie session/forgery word positions.
        self._not_in: dict[int, "array[int]"] = {}
        self.lstart: "array[int] | None" = None
        # Positions of the characters the whole-value form branches look inside a value for,
        # so each is answered by bisect instead of a scan that a repeated ``{{``/``<`` value
        # would make quadratic. ``\n``/``\r`` need no list: they are cookie-value stops, so
        # the value never holds one and ``is_placeholder``'s "no newline inside" is always met.
        self.lt = _all_finds(text, "<")
        self.gt = _all_finds(text, ">")
        self.lbrace = _all_finds(text, "{")
        self.rbrace = _all_finds(text, "}")
        self.dbrace = _all_finds(text, "}}")

    def is_placeholder_at(self, vs: int, ve: int) -> bool:
        """``is_placeholder(text[vs:ve])``, mirrored branch for branch."""
        text = self.text
        folded = self.folded
        fstart = self.fstart
        svs = self.fnw[vs]
        last = self.lnw[ve]
        sve = last + 1 if last >= svs else svs
        if sve - svs < 4:  # len(v) < 4 (strip leaves fewer than four characters)
            return True
        a, b = fstart[svs], fstart[sve]
        if b - a <= _MAX_ALLOW and folded[a:b] in ALLOW_WORDS:
            return True
        if text[max(svs, sve - _EXAMPLE_SUFFIX_MAX) : sve].endswith(_EXAMPLE_SUFFIXES):
            return True
        if b - a >= 5 and folded[a : a + 5] in ("your-", "your_"):
            return True
        if (
            text[svs] == "<"
            and text[sve - 1] == ">"
            and not self._in(self.lt, svs + 1, sve - 1)
            and not self._in(self.gt, svs + 1, sve - 1)
        ):
            return True
        if self._braced(svs, sve):
            return True
        if text[svs] == "$" and sve - svs > 1:
            c1 = text[svs + 1]
            if c1 in _ENV_HEAD and self._raw_all_in(svs + 2, sve, _ENV_TAIL):
                return True
            if c1 in _REF_HEAD and self._raw_all_in(svs + 2, sve, _REF_TAIL):
                return True
        if (
            text[svs] == "%" == text[sve - 1]
            and sve - svs > 2
            and self._raw_all_in(svs + 1, sve - 1, _IDENT)
        ):
            return True
        if sve - svs >= 3 and text[svs] in _MASK_CHARS and self.rend[svs] >= sve:
            return True
        if self._has_eight(a, b):
            return True
        your = self._first_your(a, b)
        if your != -1 and self._tail_in(your + 4, b):
            return True
        return self._has_positive_part(a, b)

    @staticmethod
    def _in(positions: list[int], i: int, j: int) -> bool:
        """Whether any precomputed position lies in ``[i, j)``."""
        k = bisect.bisect_left(positions, i)
        return k < len(positions) and positions[k] < j

    def _raw_all_in(self, i: int, j: int, charset: frozenset[str]) -> bool:
        # "Every character in [i, j) is in charset" = "the first one outside it is at or
        # after j", answered from a backward array so a long env/ref value is not re-scanned
        # per anchor.
        nb = self._not_in.get(id(charset))
        if nb is None:
            text = self.text
            n = len(text)
            nb = array("l", [0]) * (n + 1)
            nxt = n
            for p in range(n - 1, -1, -1):
                if text[p] not in charset:
                    nxt = p
                nb[p] = nxt
            nb[n] = n
            self._not_in[id(charset)] = nb
        return nb[i] >= j

    def _ensure_lower(self) -> None:
        if self.lstart is not None:
            return
        text = self.text
        n = len(text)
        lstart = array("l", [0]) * (n + 1)
        parts: list[str] = []
        pos = 0
        for i, ch in enumerate(text):
            lstart[i] = pos
            low = ch.lower()
            parts.append(low)
            pos += len(low)
        lstart[n] = pos
        self.lstart = lstart
        lowered = "".join(parts)
        self._session_starts, self._session_min = _word_hits(lowered, _SESSION_SUBWORDS)
        self._forgery_starts, self._forgery_min = _word_hits(lowered, _FORGERY_WORDS)

    def session_name_at(self, i: int, eq: int) -> bool:
        """``session_name(text[i:eq].strip())``, without reading the name whole when it is
        long (the ``cookie: … cookie: …=`` shape makes every anchor's name reach a far ``=``).

        ``str.lower`` differs from a per-character lower only on a final Greek sigma, which
        folds to ``ς``/``σ`` — neither an ASCII letter — so it cannot form any of the ASCII
        session or forgery words; the per-character fold map therefore finds exactly the
        words ``name.lower()`` would. A short name (after strip) is still read whole, which
        also covers the exact-name case (``sid``/``phpsessid``/``jsessionid``)."""
        si = self.fnw[i]
        last = self.lnw[eq]
        se = last + 1 if last >= si else si
        if se - si <= _SESSION_NAME_MAX:
            return session_name(self.text[si:se])
        self._ensure_lower()
        assert self.lstart is not None
        a, b = self.lstart[si], self.lstart[se]
        if _any_in(self._forgery_starts, self._forgery_min, a, b):
            return False
        return _any_in(self._session_starts, self._session_min, a, b)

    def _braced(self, svs: int, sve: int) -> bool:
        # Mirrors checks._braced, minus the "\n not in inner" test: a cookie value never
        # holds a newline (it is a stop), so that test is always met.
        text = self.text
        if text[svs : svs + 2] == "${" and text[sve - 1] == "}" and sve - svs > 3:
            return not self._in(self.lbrace, svs + 2, sve - 1) and not self._in(
                self.rbrace, svs + 2, sve - 1
            )
        bs = svs + 1 if text[svs] == "$" else svs
        if text[bs : bs + 2] == "{{" and text[sve - 2 : sve] == "}}" and sve - bs > 4:
            # ``}}`` is two characters, so a match inside the inner ``[bs+2, sve-2)`` must
            # START by ``sve-4``; the exclusive bound is therefore ``sve-3``, not ``sve-2``.
            return not self._in(self.dbrace, bs + 2, sve - 3)
        return False

    def _has_eight(self, a: int, b: int) -> bool:
        e = self.eights
        k = bisect.bisect_left(e, a)
        return k < len(e) and e[k] + MASK_RUN_MIN <= b

    def _first_your(self, a: int, b: int) -> int:
        y = self.yours
        k = bisect.bisect_left(y, a)
        if k < len(y) and y[k] + 4 <= b:
            return y[k]
        return -1

    def _tail_in(self, x: int, b: int) -> bool:
        k = bisect.bisect_left(self.tail_starts, x)
        return self.tail_suffix_min_end[k] <= b

    def _has_positive_part(self, a: int, b: int) -> bool:
        breaks = self.breaks
        i = bisect.bisect_left(breaks, a)
        if i == len(breaks) or breaks[i] >= b:
            return self._part_positive(a, b)  # no break inside: the value is one part
        fb = breaks[i]
        if a < fb and self._part_positive(a, fb):  # the value's first (cut) part
            return True
        lb = breaks[bisect.bisect_left(breaks, b) - 1]
        if lb + 1 < b and self._part_positive(lb + 1, b):  # the value's last (cut) part
            return True
        starts = self.pos_part_starts  # a whole part, positive, strictly inside (fb, lb)
        k = bisect.bisect_right(starts, fb)
        return k < len(starts) and starts[k] < lb

    def _part_positive(self, s: int, e: int) -> bool:
        folded = self.folded
        if e - s <= _MAX_PART_WORD and folded[s:e] in PLACEHOLDER_PARTS:
            return True
        return e - s >= 3 and folded[s] in _MASK_CHARS and self.frend[s] >= e


def _run_ends(s: str, n: int) -> "array[int]":
    """``out[i]`` is the first index after *i* whose character differs from ``s[i]``."""
    out = array("l", [n]) * n if n else array("l")
    for i in range(n - 2, -1, -1):
        out[i] = i + 1 if s[i] != s[i + 1] else out[i + 1]
    return out


def _all_finds(s: str, sub: str) -> list[int]:
    out: list[int] = []
    k = s.find(sub)
    while k != -1:
        out.append(k)
        k = s.find(sub, k + 1)
    return out


def _starts_of_runs(s: str, chars: tuple[str, ...], length: int) -> list[int]:
    """Sorted start positions of every window of *length* equal characters of *chars*."""
    out: list[int] = []
    for ch in chars:
        out.extend(_all_finds(s, ch * length))
    out.sort()
    return out


def _positive_part_starts(folded: str, m: int) -> list[int]:
    """Start positions of the parts of *folded* that are a placeholder word or a mask run
    (``str.split``-delimited by ``-_.`` and whitespace)."""
    out: list[int] = []
    i = 0
    while i < m:
        if _is_break(folded[i]):
            i += 1
            continue
        j = i
        while j < m and not _is_break(folded[j]):
            j += 1
        part = folded[i:j]
        if (len(part) <= _MAX_PART_WORD and part in PLACEHOLDER_PARTS) or (
            len(part) >= 3 and len(set(part)) == 1 and part[0] in _MASK_CHARS
        ):
            out.append(i)
        i = j
    return out


def cookie_value_is_placeholder(
    text: str,
    vs: int,
    ve: int,
    has_mask: bool,
    has_trigger: bool,
    classify: "Callable[[int, int], bool]",
) -> bool:
    """Exactly ``is_placeholder(text[vs:ve])`` for a cookie value, but without reading the
    value whole at every anchor.

    Three paths, each giving the same answer the full check would:

    * A value shorter than :data:`_SHORTCUT_MIN` goes to the full check — it is bounded,
      and only a short value can be an allow word or a short reference form.
    * A long value with no mask character (*has_mask*), no placeholder word (*has_trigger*),
      no boundary whitespace, no form-keying first character and no example suffix cannot be
      a placeholder, so it is settled ``False`` with no scan at all.
    * Any other long value — one that does hold such a feature — is settled by *classify*, a
      :class:`_ValueScan` over the value's own line, built once per line and shared across
      its anchors, answering in ``O(log n)``.
    """
    if ve - vs < _SHORTCUT_MIN:
        return is_placeholder(text[vs:ve])
    if (
        not has_mask
        and not has_trigger
        and not text[vs].isspace()
        and not text[ve - 1].isspace()
        and text[vs] not in _FIRST_CHAR_FORMS
        and not text[max(vs, ve - _EXAMPLE_SUFFIX_MAX) : ve].endswith(_EXAMPLE_SUFFIXES)
    ):
        return False
    if ve - vs < _SCAN_MIN:
        return is_placeholder(text[vs:ve])
    return classify(vs, ve)


def is_placeholder_body(body: str) -> bool:
    """A token-format body a documented example writes (``xoxb-your-bot-token``)."""
    return is_placeholder(body) or _has_placeholder_part(body.casefold(), BODY_PLACEHOLDER_PARTS)


def reads_as_words(value: str) -> bool:
    """Lower-case words with no digit (``my-secret-pw``, ``kerberos``, ``see the vault``).

    Prose or a documented example, not a generated value: a part longer than
    :data:`WORD_MAX`, or a part of four letters or more with no vowel, reads as generated.
    """
    if not set(value) <= _WORD_CHARS:
        return False
    for part in value.translate(_PART_BREAKS).split():
        if len(part) > WORD_MAX or (len(part) > 3 and _VOWELS.isdisjoint(part)):
            return False
    return True


#: Words a documented example or a sentence writes in a value (``my-secret-pw``).
EXAMPLE_WORDS = frozenset(
    {"secret", "password", "passwd", "pass", "pw", "pwd", "example", "sample", "dummy"}
    | {"test", "demo", "changeme", "your", "here", "value", "token", "key", "default"}
)


def reads_as_example_words(value: str) -> bool:
    """A labelled value that is a sentence or a documented example, not a password.

    Lower-case words (:func:`reads_as_words`) that are prose (a blank between them) or
    that name what the value stands for (``my-secret-pw``). One word alone, or words
    with no such part (``correct-horse-battery``), can be a real password and is not
    this.
    """
    if not reads_as_words(value):
        return False
    parts = value.translate(_PART_BREAKS).split()
    if len(parts) < 2:
        return False
    return " " in value.strip() or any(part in EXAMPLE_WORDS for part in parts)
