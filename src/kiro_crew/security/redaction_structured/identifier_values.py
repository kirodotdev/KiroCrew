"""Identifier rows for values a task needs and that look like secrets by shape.

These are never replaced, and they never override a Secret hit (they are not
structural, so resolve rule 1 does not apply to them). A caller can use their spans to
keep the values visible to a later, shape-based pass.

- ``labelled_value``: a value after a fixed label, read case-insensitively
  (``x-amz-id-2:``, ``"HostId":``, ``x-amz-request-id:``, ``SHA256:``);
- ``data_uri``: the payload of a ``data:<type>;base64,`` URI;
- ``image_b64``: a bare base64 run whose decoded head is an image file signature;
- ``exact``: one of the listed values as a whole token (the AWS documentation example);
- ``temp_path``: a macOS per-user temporary path under ``/var/folders/``;
- ``placeholder``: template references and masked values;
- ``form_query``: a public OAuth parameter in a form body or query outside a URL.

Every reader is bounded by the row's ``max_length``; with ``final=False`` a unit still
running at the end of the text is reported open, so a stream holds it.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable

from .charsets import (
    ALNUM,
    B64STD,
    DIGITS,
    LOWER,
    UPPER,
    ascii_lower,
    cached,
    first_of,
    run_end,
    run_start,
)
from .model import Hit
from .table import StructureRow

_BLANKS = " \t"
_MAX_BLANKS = 8
_QUOTES = "\"'"
_B64STD_SET = frozenset(B64STD)
_TOKEN_EDGE = frozenset(ALNUM + "+/_-")
_ALNUM_SET = frozenset(ALNUM)

_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"RIFF")

_MIME_CHARS = LOWER + DIGITS + ".+-/"
_MIME_PARAM_CHARS = LOWER + UPPER + DIGITS + "=.+-_"
_BASE64_MARK = ";base64,"

_TMP_PREFIX = "/var/folders/"
_TMP_SEG1 = LOWER + DIGITS + "_"
_TMP_SEG2 = ALNUM + "_+-"
_TMP_KINDS = frozenset("TC0")
_PATH_CHARS = ALNUM + "/._-+~@%"
_PATH_TRAIL = ".,;:"

_PH_NAME = ALNUM + "_.:-"
_PH_INNER_STOP = "{}\n\r"
_ENV_UPPER = UPPER + DIGITS + "_"
_ANGLE_INNER = ALNUM + "_ -"
_ANGLE_WORDS = frozenset({"redacted", "token", "secret", "password", "api-key", "api_key", "key"})
_MASK_RUN = 12
_STAR_RUN = 6
_MASK_TOKEN = ALNUM + "_-"
_PH_GATES = ("{{", "$", "<", "%", "x" * _MASK_RUN, "X" * _MASK_RUN, "*" * _STAR_RUN)

_QUERY_STOPS = "&#\"'<> \t\r\n`"
_FORM_LEADS = frozenset("&?\n")


def _labelled(text: str, low: str, row: StructureRow, final: bool) -> tuple[list[Hit], list[int]]:
    n = len(text)
    hits: list[Hit] = []
    opens: list[int] = []
    for label in row.labels:
        i = low.find(label)
        while i != -1:
            nxt = i + 1
            if i > 0 and label[0] in LOWER and text[i - 1] in _ALNUM_SET:
                i = low.find(label, nxt)
                continue
            j = i + len(label)
            k = j
            while k < n and text[k] in _BLANKS and k - j <= _MAX_BLANKS:
                k += 1
            if k < n and text[k] in _QUOTES:
                k += 1
            if k >= n:
                if not final:
                    opens.append(i)
                i = low.find(label, nxt)
                continue
            e = run_end(text, k, row.chars, row.max_length + 1)
            p = e
            while p < n and p - e < row.pad and text[p] == "=":
                p += 1
            if not final and p == n and e - k <= row.max_length:
                opens.append(i)
            elif row.min_length <= e - k <= row.max_length and (
                p == n or (text[p] not in row.chars and text[p] != "=")
            ):
                hits.append(Hit(k, p, row.id, row.tier, "structure", "structure"))
            i = low.find(label, nxt)
    return hits, opens


def _data_uris(text: str, low: str, row: StructureRow, final: bool) -> tuple[list[Hit], list[int]]:
    n = len(text)
    hits: list[Hit] = []
    opens: list[int] = []
    i = low.find("data:")
    while i != -1:
        if i > 0 and text[i - 1] in _ALNUM_SET:
            i = low.find("data:", i + 1)
            continue
        m = run_end(low, i + 5, _MIME_CHARS, 101)
        if m == i + 5 or m - i - 5 > 100:
            i = low.find("data:", i + 1)
            continue
        k = m
        steps = 0
        while k < n and text[k] == ";" and not low.startswith(_BASE64_MARK, k) and steps < 8:
            k = run_end(text, k + 1, _MIME_PARAM_CHARS, 101)
            steps += 1
        if k + len(_BASE64_MARK) > n and not final:
            opens.append(i)
        elif low.startswith(_BASE64_MARK, k):
            vs = k + len(_BASE64_MARK)
            e = run_end(text, vs, B64STD, row.max_length + 1)
            p = e
            while p < n and p - e < 2 and text[p] == "=":
                p += 1
            if not final and p == n and e - vs <= row.max_length:
                opens.append(i)
            elif 4 <= e - vs <= row.max_length:
                hits.append(Hit(vs, p, row.id, row.tier, "structure", "structure"))
        i = low.find("data:", i + 1)
    return hits, opens


def _image_head_ok(run: str) -> bool:
    try:
        head = base64.b64decode(run[:16], validate=True)
    except (binascii.Error, ValueError):
        return False
    return head.startswith(_IMAGE_MAGIC)


def _images(text: str, row: StructureRow, final: bool) -> tuple[list[Hit], list[int]]:
    n = len(text)
    hits: list[Hit] = []
    opens: list[int] = []
    for prefix in row.labels:
        i = text.find(prefix)
        while i != -1:
            if i > 0 and text[i - 1] in _B64STD_SET:
                i = text.find(prefix, i + 1)
                continue
            e = run_end(text, i, B64STD, row.max_length + 1)
            p = e
            while p < n and p - e < 2 and text[p] == "=":
                p += 1
            if not final and p == n and e - i <= row.max_length:
                opens.append(i)
            elif row.min_length <= e - i <= row.max_length and _image_head_ok(text[i:e]):
                hits.append(Hit(i, p, row.id, row.tier, "structure", "structure"))
            i = text.find(prefix, max(i + 1, e))
    return hits, opens


def _exact(text: str, row: StructureRow, final: bool) -> tuple[list[Hit], list[int]]:
    n = len(text)
    hits: list[Hit] = []
    opens: list[int] = []
    for value in row.values:
        i = text.find(value)
        while i != -1:
            e = i + len(value)
            if i == 0 or text[i - 1] not in _TOKEN_EDGE:
                if e == n and not final:
                    opens.append(i)
                elif e == n or text[e] not in _TOKEN_EDGE:
                    hits.append(Hit(i, e, row.id, row.tier, "structure", "structure"))
            i = text.find(value, i + 1)
    # A proper prefix of a value at the end of the text may still become one.
    if not final:
        for value in row.values:
            for cut in range(1, len(value)):
                if text.endswith(value[:cut]):
                    opens.append(n - cut)
                    break
    return hits, opens


def _temp_paths(text: str, row: StructureRow, final: bool) -> tuple[list[Hit], list[int]]:
    n = len(text)
    hits: list[Hit] = []
    opens: list[int] = []
    i = text.find(_TMP_PREFIX)
    while i != -1:
        start = i - 8 if text.startswith("/private", i - 8) and i >= 8 else i
        if start > 0 and text[start - 1] in _PATH_CHARS:
            i = text.find(_TMP_PREFIX, i + 1)
            continue
        j = i + len(_TMP_PREFIX)
        a = run_end(text, j, _TMP_SEG1, 3)
        b = run_end(text, a + 1, _TMP_SEG2, 65) if a < n and text[a] == "/" else a
        e = run_end(text, b, _PATH_CHARS, row.max_length + 1)
        if not final and e == n and e - start <= row.max_length:
            opens.append(start)
        elif (
            a - j == 2
            and a < n
            and text[a] == "/"
            and 1 <= b - a - 1 <= 64
            and b + 1 < n
            and text[b] == "/"
            and text[b + 1] in _TMP_KINDS
            and (b + 2 == n or text[b + 2] in "/" or text[b + 2] not in _PATH_CHARS)
            and e - start <= row.max_length
        ):
            end = e
            while end > b + 2 and text[end - 1] in _PATH_TRAIL:
                end -= 1
            hits.append(Hit(start, end, row.id, row.tier, "structure", "structure"))
        i = text.find(_TMP_PREFIX, i + 1)
    return hits, opens


def _closed_inner(text: str, start: int, close: str, cap: int) -> int | None:
    """End index just past ``close`` if it follows ``start`` within ``cap`` with no stop."""
    lim = min(len(text), start + cap + len(close))
    c = text.find(close, start, lim)
    if c == -1:
        return None
    for ch in _PH_INNER_STOP:
        if text.find(ch, start, c) != -1:
            return None
    return c + len(close)


def _placeholders(text: str, row: StructureRow, final: bool) -> tuple[list[Hit], list[int]]:
    n = len(text)
    cap = row.max_length
    hits: list[Hit] = []
    opens: list[int] = []

    def add(s: int, e: int) -> None:
        hits.append(Hit(s, e, row.id, row.tier, "structure", "structure"))

    def undecided(s: int) -> bool:
        # A form is decided once ``cap`` characters after it have been read.
        if not final and n - s <= cap + 4:
            opens.append(s)
            return True
        return False

    # ``{{ ... }}`` and ``${{ ... }}``.
    i = text.find("{{")
    while i != -1:
        s = i - 1 if i > 0 and text[i - 1] == "$" else i
        e = _closed_inner(text, i + 2, "}}", cap)
        if e is not None and e - i > 4:
            add(s, e)
            i = text.find("{{", e)
            continue
        if e is None and undecided(s):
            break
        i = text.find("{{", i + 1)
    # ``${NAME}``.
    i = text.find("${")
    while i != -1:
        if not text.startswith("${{", i):
            e = run_end(text, i + 2, _PH_NAME, 65)
            if e < n and text[e] == "}" and 1 <= e - i - 2 <= 64:
                add(i, e + 1)
            elif e == n:
                undecided(i)
        i = text.find("${", i + 1)
    # ``<your-token-here>``, ``<YOUR_API_KEY>``, ``<redacted>``.
    i = text.find("<")
    while i != -1:
        c = text.find(">", i + 1, i + 66)
        if c == -1:
            if not final and n - i <= 66:
                opens.append(i)
        else:
            inner = text[i + 1 : c]
            if 3 <= len(inner) and not inner.strip(_ANGLE_INNER):
                folded = inner.casefold()
                # An upper-case name such as ``<YOUR_API_KEY>``: upper letters, digits, ``_``.
                upper_name = not inner.strip(_ENV_UPPER) and inner[0] in UPPER
                if folded.startswith("your") or folded in _ANGLE_WORDS or upper_name:
                    add(i, c + 1)
        i = text.find("<", i + 1)
    # ``%NAME%`` and ``$NAME``.
    for mark in ("%", "$"):
        i = text.find(mark)
        while i != -1:
            if i > 0 and text[i - 1] in _ALNUM_SET:
                i = text.find(mark, i + 1)
                continue
            e = run_end(text, i + 1, _ENV_UPPER, 65)
            name = text[i + 1 : e]
            ok = 2 <= len(name) <= 64 and name[0] in UPPER + "_" and any(c in UPPER for c in name)
            if e == n and not final:
                opens.append(i)
            elif ok and mark == "%":
                if e < n and text[e] == "%":
                    add(i, e + 1)
            elif ok and (e == n or text[e] not in _ALNUM_SET):
                add(i, e)
            i = text.find(mark, i + 1)
    # Masked values: a token ending in a long run of one mask letter, or a run of ``*``.
    for unit in ("x" * _MASK_RUN, "X" * _MASK_RUN):
        i = text.find(unit)
        while i != -1:
            run_stop = run_end(text, i, unit[0], cap + 1)
            if run_stop - i > cap:
                # Longer than any masked value: decided, and never held.
                i = text.find(unit, run_end(text, run_stop, unit[0]))
                continue
            s = i
            while s > 0 and text[s - 1] in _MASK_TOKEN and i - s < cap:
                s -= 1
            e = run_end(text, run_stop, _MASK_TOKEN, cap)
            if not final and e == n and e - s <= cap:
                opens.append(s)
            elif e == run_stop and e - s <= cap and (s == 0 or text[s - 1] not in _MASK_TOKEN):
                add(s, e)
            i = text.find(unit, run_stop)
    i = text.find("*" * _STAR_RUN)
    while i != -1:
        e = run_end(text, i, "*", cap + 1)
        if not final and e == n:
            opens.append(i)
        elif e - i <= cap:
            add(i, e)
        i = text.find("*" * _STAR_RUN, max(e, i + 1))
    return hits, opens


def _param_plan(row: StructureRow) -> tuple[str, int]:
    """The characters parameter names use, and the longest name."""
    chars = "".join(sorted(set("".join(row.params))))
    return chars, max((len(p) for p in row.params), default=0)


def _form_params(text: str, row: StructureRow, final: bool) -> tuple[list[Hit], list[int]]:
    """``name=value`` after ``&``, ``?``, a line break or at the start, for a listed name.

    One pass over the ``=`` characters: the name before each is read back over the
    names' own characters and looked up in the row's set.
    """
    n = len(text)
    hits: list[Hit] = []
    opens: list[int] = []
    eq = text.find("=")
    if eq == -1:
        return hits, opens
    name_chars, name_max = cached(row, _param_plan)
    params = row.params
    while eq != -1:
        s = run_start(text, eq, name_chars, name_max + 1)
        if (
            0 < eq - s <= name_max
            and (s == 0 or text[s - 1] in _FORM_LEADS)
            and text[s:eq] in params
        ):
            vs = eq + 1
            e = first_of(text, vs, _QUERY_STOPS, vs + row.max_length + 1)
            if not final and e == n and e - vs <= row.max_length:
                opens.append(s if s == 0 or text[s - 1] == "\n" else s - 1)
            elif 1 <= e - vs <= row.max_length:
                hits.append(Hit(vs, e, row.id, row.tier, "structure", "structure"))
        eq = text.find("=", eq + 1)
    return hits, opens


def detect_identifier_values(
    text: str, rows: tuple[StructureRow, ...], final: bool, low: str | None = None
) -> tuple[list[Hit], list[int]]:
    hits: list[Hit] = []
    opens: list[int] = []
    for reader, row, on_low, anchors, always_open in cached(rows, _plan):
        if on_low:
            if low is None:
                low = ascii_lower(text)
            hay = low
        else:
            hay = text
        # Anchor gate: a row runs only when one of its literals is in the text. A row
        # whose open units do not need a whole anchor (``exact``) always runs mid-stream.
        if final or not always_open:
            for anchor in anchors:
                if anchor in hay:
                    break
            else:
                continue
        found, op = reader(text, low if on_low else hay, row, final)
        if found:
            hits += found
        if op:
            opens += op
    return hits, opens


_Reader = Callable[[str, str, StructureRow, bool], tuple[list[Hit], list[int]]]


def _plan(
    rows: tuple[StructureRow, ...],
) -> tuple[tuple[_Reader, StructureRow, bool, tuple[str, ...], bool], ...]:
    """Per value row: its reader, whether it reads lowered text, its anchors, and whether
    it may report an open unit before any whole anchor is present."""

    def plain(fn: Callable[[str, StructureRow, bool], tuple[list[Hit], list[int]]]) -> _Reader:
        return lambda text, _low, row, final: fn(text, row, final)

    out: list[tuple[_Reader, StructureRow, bool, tuple[str, ...], bool]] = []
    for row in rows:
        if row.kind == "labelled_value":
            out.append((_labelled, row, True, row.labels, False))
        elif row.kind == "data_uri":
            out.append((_data_uris, row, True, ("data:",), False))
        elif row.kind == "image_b64":
            out.append((plain(_images), row, False, row.labels, False))
        elif row.kind == "exact":
            out.append((plain(_exact), row, False, row.values, True))
        elif row.kind == "temp_path":
            out.append((plain(_temp_paths), row, False, (_TMP_PREFIX,), False))
        elif row.kind == "placeholder":
            out.append((plain(_placeholders), row, False, _PH_GATES, False))
        elif row.kind == "form_query":
            out.append((plain(_form_params), row, False, ("=",), False))
    return tuple(out)
