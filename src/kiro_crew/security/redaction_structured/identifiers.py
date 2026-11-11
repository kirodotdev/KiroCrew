"""Structural Identifier rules: ARNs, URLs, commit permalinks, public OAuth parameters.

These are never replaced. Resolve rule 1 lets one that fully contains a label or
pairing Secret hit win over it, so structure the task needs keeps its text.
"""

from __future__ import annotations

from .charsets import (
    ALNUM,
    DIGITS,
    LOWER,
    WORD_DASH_SET,
    StopScanner,
    ascii_lower,
    cached,
    first_of,
    run_start,
)
from .model import TAG, Hit
from .table import StructureRow, UrlParamRow

_ARN_STOPS = " \t\r\n\"'`<>,;()[]{}|\\"
_URL_STOPS = " \t\r\n\"'`<>\\"
_SCHEME_CHARS = ALNUM + "+.-"
_SCHEME_MAX = 32
_LETTERS = frozenset(ALNUM[:52])
_TRAILING = ".,;:!?)]}'"
_ARN_NAME = frozenset(LOWER + DIGITS + "-")
_HEX = frozenset(DIGITS + "abcdef")
_PERMALINK_KINDS = frozenset({"commit", "blob", "tree"})
_PERMALINK_READ = 512
_ALNUM_SET = frozenset(ALNUM)
# Query and fragment parameter separators (RFC 3986 and form encoding).
_PARAM_STOPS = "?#&"
_HEXDIGITS = frozenset(DIGITS + "abcdefABCDEF")
# An encoded key is at most three characters per decoded character.
_KEY_ENCODING = 3


def _by_kind(rows: tuple[StructureRow, ...]) -> dict[str, StructureRow]:
    return {row.kind: row for row in rows}


def _arn_ok(token: str) -> bool:
    parts = token.split(":", 5)
    if len(parts) != 6:
        return False
    _, partition, service, region, account, resource = parts
    if not partition.startswith("aws") or not set(partition) <= _ARN_NAME:
        return False
    if not service or not set(service) <= _ARN_NAME or not set(region) <= _ARN_NAME:
        return False
    if account and account != "aws" and not (len(account) == 12 and account.isdigit()):
        return False
    return bool(resource)


def detect_arns(text: str, row: StructureRow, final: bool) -> tuple[list[Hit], list[int]]:
    n = len(text)
    hits: list[Hit] = []
    opens: list[int] = []
    i = text.find("arn:")
    if i == -1:
        return hits, opens
    scanner = StopScanner(text, _ARN_STOPS)
    while i != -1:
        if i == 0 or text[i - 1] not in WORD_DASH_SET:
            e = scanner.end(i, i + row.max_length + 1)
            if not final and e == n and e - i <= row.max_length:
                opens.append(i)
            elif e - i <= row.max_length:
                token = text[i:e].rstrip(".")
                if _arn_ok(token):
                    hits.append(
                        Hit(i, i + len(token), row.id, row.tier, "structure", "structure", True)
                    )
        i = text.find("arn:", i + 1)
    return hits, opens


def _permalink(path: str) -> bool:
    segs = path.split("/")
    if len(segs) < 5:
        return False
    sha = segs[4]
    return segs[3] in _PERMALINK_KINDS and 7 <= len(sha) <= 40 and set(sha) <= _HEX


ObjectNamePlan = tuple[StructureRow, int, int, bytes, bytes]
_IN, _OUT = ord("h"), ord(" ")
_IN_BYTES, _OUT_BYTES = b"h", b" "


def object_name_plan(row: StructureRow) -> ObjectNamePlan:
    """The row, its shortest and longest length, and a byte table that maps each ASCII
    character of the alphabet to ``h`` and every other byte to a blank."""
    if not row.chars.isascii():
        raise ValueError(f"rule {row.id!r}: an object-name alphabet must be ASCII")
    table = bytes(_IN if chr(i) in row.chars and i < 128 else _OUT for i in range(256))
    lo = min(row.lengths)
    return row, lo, max(row.lengths), table, _IN_BYTES * lo


def _object_hit(text: str, a: int, b: int, row: StructureRow, final: bool, hits: list[Hit]) -> None:
    n = len(text)
    if (
        b - a in row.lengths
        and (b < n or final)
        and (a == 0 or text[a - 1] not in _ALNUM_SET)
        and (b == n or text[b] not in _ALNUM_SET)
    ):
        hits.append(Hit(a, b, row.id, row.tier, "structure", "structure", True))


def detect_object_names(
    text: str, plan: ObjectNamePlan, final: bool
) -> tuple[list[Hit], list[int]]:
    """Whole tokens of exactly ``row.lengths`` characters of ``row.chars``.

    No letter or digit may touch the run on either side. The text is encoded as
    Latin-1 with every other character written as ``?`` (one byte per character, so
    indexes match) and mapped through the plan's byte table, which turns each run of
    the (ASCII) alphabet into a run of ``h`` found with ``bytes.find``.
    """
    row, lo, hi, table, unit = plan
    n = len(text)
    if n < lo and final:
        return [], []
    cls = text.encode("latin-1", "replace").translate(table)
    i = cls.find(unit)
    if i == -1 and final:
        return [], []
    hits: list[Hit] = []
    opens: list[int] = []
    while i != -1:
        # ``find`` returns the first index of the run: the byte before it is a blank.
        b = cls.find(_OUT_BYTES, i + lo)
        b = n if b == -1 else b
        _object_hit(text, i, b, row, final, hits)
        i = cls.find(unit, b)
    if not final:
        # A run still running at the end of the text may yet become one.
        a = n - (len(cls) - len(cls.rstrip(_IN_BYTES)))
        if a < n and n - a <= hi and (a == 0 or text[a - 1] not in _ALNUM_SET):
            opens.append(a)
    return hits, opens


def _decoded_key(raw: str) -> str:
    """``raw`` percent-decoded (and ``+`` read as a space), with ASCII letters lowered."""
    if "%" not in raw and "+" not in raw:
        return ascii_lower(raw)
    out = bytearray()
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "%" and i + 2 < len(raw):
            pair = raw[i + 1 : i + 3]
            if pair[0] in _HEXDIGITS and pair[1] in _HEXDIGITS:
                out.append(int(pair, 16))
                i += 3
                continue
        out += b" " if ch == "+" else ch.encode("utf-8")
        i += 1
    return ascii_lower(out.decode("utf-8", "replace"))


def _key_plan(rows: tuple[UrlParamRow, ...]) -> tuple[dict[str, UrlParamRow], int]:
    keys = {row.key: row for row in rows}
    return keys, _KEY_ENCODING * max((len(k) for k in keys), default=0)


def _secret_param_hits(
    text: str,
    start: int,
    end: int,
    rows: tuple[UrlParamRow, ...],
    hits: list[Hit],
    anchor: int,
) -> None:
    """Values of the owner's OAuth keys in ``text[start:end]``, a query and/or fragment.

    The text is split on ``?``, ``#`` and ``&`` into parameters and each parameter at
    its first ``=``; the key is percent-decoded and compared case-insensitively. Only
    a non-empty value is a hit; the key and the separators stay.
    """
    keys, raw_max = cached(rows, _key_plan)
    pos = start + 1
    while pos < end:
        stop = first_of(text, pos, _PARAM_STOPS, end)
        eq = text.find("=", pos, stop)
        if eq != -1 and eq + 1 < stop and 0 < eq - pos <= raw_max:
            row = keys.get(_decoded_key(text[pos:eq]))
            # A value already replaced by this redactor's tag is not read again.
            if row is not None and not text.startswith(TAG, eq + 1):
                hits.append(
                    Hit(eq + 1, stop, row.id, row.tier, "url_param", "known_key", anchor=anchor)
                )
        pos = stop + 1


def detect_urls(
    text: str,
    rows: tuple[StructureRow, ...],
    final: bool,
    url_params: tuple[UrlParamRow, ...] = (),
) -> tuple[list[Hit], list[int]]:
    q = text.find("://")
    if q == -1:
        return [], []
    kinds = cached(rows, _by_kind)
    url_row = kinds.get("url")
    if url_row is None:
        return [], []
    permalink_row = kinds.get("commit_permalink")
    query_row = kinds.get("url_query")
    cap = url_row.max_length
    n = len(text)
    hits: list[Hit] = []
    opens: list[int] = []
    scanner = StopScanner(text, _URL_STOPS)
    path_scanner = StopScanner(text, "?#")
    query_done = -1
    params_done = -1
    trim_for, trimmed_to = -1, -1
    while q != -1:
        k = run_start(text, q, _SCHEME_CHARS, _SCHEME_MAX)
        while k < q and text[k] not in _LETTERS:
            k += 1
        if k < q:
            e = scanner.end(q + 3, k + cap + 1)
            if not final and e == n and e - k <= cap:
                opens.append(k)
            elif e - k <= cap:
                if e != trim_for:
                    trim_for, trimmed_to = e, _trim(text, q + 3, e)
                end = max(q + 3, trimmed_to)
                path_end = path_scanner.end(q + 3, end)
                if path_end > q + 3:
                    row = url_row
                    if permalink_row is not None and _permalink(
                        text[q + 3 : min(path_end, q + 3 + _PERMALINK_READ)]
                    ):
                        row = permalink_row
                    hits.append(Hit(k, path_end, row.id, row.tier, "structure", "structure", True))
                    if (
                        query_row is not None
                        and path_end < end
                        and text[path_end] == "?"
                        and path_end >= query_done
                    ):
                        query_done = _query_hits(text, path_end, end, query_row, hits, k)
                    if url_params and path_end < end and path_end >= params_done:
                        # A URL nested inside this one's query is read once, here.
                        _secret_param_hits(text, path_end, end, url_params, hits, k)
                        params_done = end
        q = text.find("://", q + 1)
    return hits, opens


def _trim(text: str, start: int, end: int) -> int:
    """Drop sentence punctuation that ends a URL, reading back a bounded stretch."""
    while end > start:
        lo = max(start, end - 64)
        chunk = text[lo:end]
        kept = chunk.rstrip(_TRAILING)
        end = lo + len(kept)
        if kept:
            break
    return end


def _query_hits(
    text: str, path_end: int, end: int, query_row: StructureRow, hits: list[Hit], anchor: int
) -> int:
    """Public OAuth parameter values in the query; returns where the query ends."""
    query_end = text.find("#", path_end, end)
    if query_end == -1:
        query_end = end
    pos = path_end + 1
    while pos < query_end:
        amp = text.find("&", pos, query_end)
        if amp == -1:
            amp = query_end
        eq = text.find("=", pos, amp)
        if eq != -1 and text[pos:eq] in query_row.params and eq + 1 < amp:
            hits.append(
                Hit(
                    eq + 1,
                    amp,
                    query_row.id,
                    query_row.tier,
                    "structure",
                    "structure",
                    True,
                    anchor=anchor,
                )
            )
        pos = amp + 1
    return query_end
