"""Detector A: exact known keys in parsed inputs, with a value-shape gate.

The text is lowered once (ASCII only, so indices match the original); ``str.find``
locates each spelling of a known key; the key token's own boundaries are read by set
membership and its normalised name must EQUAL a known key (design open question M10:
equality, not suffix). The input the key sits in decides how the value is read:

- XML element ``<SecretAccessKey>value</SecretAccessKey>``;
- JSON / dict / object member ``"key": value`` (also ``\\"key\\": \\"value\\"``);
- CLI flag ``--aws-secret-access-key value`` or ``=value``;
- assignment at a line start (an optional unified-diff marker ``+``, ``-`` or space,
  blanks, optional ``export``/``set``/``setx``/``declare`` word, YAML ``- ``, Markdown
  ``*``), separator ``=`` or ``:``; or ``KEY=value`` glued after a blank, ``;``, ``&``,
  ``|`` or ``(``. An assignment inside a quoted string on a longer line is not read
  (owner ruling: the parser is not widened for it).

The value-shape gate (owner decision O1b) then decides: ``aws_secret_40`` takes a run
of exactly 40 AWS-secret-alphabet characters ending the value; ``b64_run`` takes a
base64 run of at least 100 characters (padding included). Only that run is replaced.
"""

from __future__ import annotations

from .charsets import ALNUM, B64STD, ascii_lower, cached, run_end, run_start
from .checks import is_placeholder
from .model import TAIL_B64RUN, Hit
from .table import KnownKeyRow

KEY_CHARS = ALNUM + "_-."
KEY_MAX = 64
LEAD_MAX = 64
MAX_BLANKS = 8
SECRET_LEN = 40
SESSION_MIN = 100
# A session-token run still running after this many characters is decided a Secret
# (fail closed); the stream then discards the rest of the run.
SESSION_FAIL_CLOSED = 4096
_BLANKS = " \t"
_CONT = frozenset(ALNUM + "_-.=/+~%")
_LEAD_WORDS = frozenset({"", "export", "set", "setx", "declare", "-"})
_DIFF_MARKS = frozenset("+- ")
_INLINE_BEFORE = frozenset(" \t;&|(")
_QUOTES = "\"'"


class _More(Exception):
    """The text ends before the unit is decided (non-final scans only)."""


def normalise(name: str) -> str:
    """Casefold ASCII only and drop ``_``, ``-``, ``.`` and blanks."""
    return "".join(ch for ch in ascii_lower(name) if ch not in "_-. ")


def _words(name: str) -> list[str]:
    if "_" in name:
        return [w.lower() for w in name.split("_") if w]
    words: list[str] = []
    for ch in name:
        if ch.isupper() or not words:
            words.append(ch.lower())
        else:
            words[-1] += ch
    return words


def spellings(rows: tuple[KnownKeyRow, ...]) -> tuple[str, ...]:
    """Lower-case search spellings: each key's words joined by ``_``, ``-``, ``.`` or none."""
    out: set[str] = set()
    for row in rows:
        for key in row.keys:
            words = _words(key)
            for sep in ("_", "-", ".", ""):
                out.add(sep.join(words))
    # Searching the longest first lets a shorter spelling inside it be deduplicated.
    return tuple(sorted(out, key=len, reverse=True))


# Search spellings and the normalised-name lookup depend only on the rows, so they are
# built once per rows tuple (the loaded table's, in practice), not on every call.
_PREPARED: dict[int, tuple[tuple[KnownKeyRow, ...], tuple[str, ...], dict[str, KnownKeyRow]]] = {}
_PREPARED_MAX = 8


def _prepared(rows: tuple[KnownKeyRow, ...]) -> tuple[tuple[str, ...], dict[str, KnownKeyRow]]:
    got = _PREPARED.get(id(rows))
    if got is not None and got[0] is rows:
        return got[1], got[2]
    if len(_PREPARED) >= _PREPARED_MAX:
        _PREPARED.clear()
    search = spellings(rows)
    known = {normalise(k): row for row in rows for k in row.keys}
    _PREPARED[id(rows)] = (rows, search, known)
    return search, known


def gate_words(rows: tuple[KnownKeyRow, ...]) -> tuple[str, ...]:
    """One lower-case word per key that every search spelling of that key contains.

    A spelling joins the key's words with one separator (or none), so each word appears
    whole in it; the longest word is the most selective test.
    """
    words = {max(_words(key), key=len) for row in rows for key in row.keys}
    return tuple(sorted(words))


class _Reader:
    def __init__(self, text: str, final: bool) -> None:
        self.text = text
        self.n = len(text)
        self.final = final

    def at(self, j: int) -> str:
        if j < self.n:
            return self.text[j]
        if not self.final:
            raise _More
        return ""

    def blanks(self, j: int, at_least: int = 0) -> int | None:
        k = j
        while self.at(k) in _BLANKS and self.at(k) != "":
            k += 1
            if k - j > MAX_BLANKS:
                return None
        return k if k - j >= at_least else None


def _gate(rd: _Reader, vs: int, gate: str, closer: str) -> tuple[int, int, str] | None:
    text, n = rd.text, rd.n
    if gate == "aws_secret_40":
        e = run_end(text, vs, B64STD, SECRET_LEN + 1)
        if e == n and not rd.final:
            raise _More
        if e - vs != SECRET_LEN:
            return None
        if closer:
            for k, ch in enumerate(closer):
                if rd.at(e + k) != ch:
                    return None
        else:
            nxt = rd.at(e)
            if nxt and nxt in _CONT:
                return None
        if is_placeholder(text[vs:e]):
            return None
        return vs, e, ""
    e = run_end(text, vs, B64STD)
    if e == n and not rd.final:
        if e - vs >= SESSION_FAIL_CLOSED:
            return vs, n, TAIL_B64RUN
        raise _More
    if e - vs < SESSION_MIN:
        return None
    p = e
    while p - e < 2 and rd.at(p) == "=":
        p += 1
    if is_placeholder(text[vs:e]):
        return None
    return vs, p, ""


def _value_after_sep(rd: _Reader, j: int, gate: str) -> tuple[int, int, str] | None:
    """Read an optional quote, then gate the value."""
    ch = rd.at(j)
    if ch and ch in _QUOTES:
        return _gate(rd, j + 1, gate, ch if gate == "aws_secret_40" else "")
    if ch == "\\" and rd.at(j + 1) == '"':
        return _gate(rd, j + 2, gate, '\\"' if gate == "aws_secret_40" else "")
    return _gate(rd, j, gate, "")


def _lead_core(lead: str) -> str:
    return lead.strip(_BLANKS).rstrip("*").strip(_BLANKS)


def _line_lead_ok(text: str, ks: int) -> bool:
    lo = max(0, ks - LEAD_MAX)
    nl = text.rfind("\n", lo, ks)
    if nl == -1 and lo > 0:
        return False
    lead = text[nl + 1 : ks].replace("\r", "")
    if _lead_core(lead) in _LEAD_WORDS:
        return True
    # A unified-diff line marker (``+``, ``-`` or one space) before the usual lead.
    return lead[:1] in _DIFF_MARKS and _lead_core(lead[1:]) in _LEAD_WORDS


def _read(rd: _Reader, ks: int, ke: int, key: str, row: KnownKeyRow) -> tuple[int, int, str] | None:
    """The gated value span for the key token ``text[ks:ke]``, from the input it sits in."""
    j: int | None
    k: int | None
    text = rd.text
    prev = text[ks - 1] if ks > 0 else ""
    nxt = rd.at(ke)
    gate = row.gate
    # XML element.
    if "xml_element" in row.inputs and prev == "<" and nxt == ">":
        close = "</" + key + ">"
        return _gate(rd, ke + 1, gate, close if gate == "aws_secret_40" else "")
    # JSON / dict member, plain or with escaped quotes.
    if "json" in row.inputs and prev and prev in _QUOTES:
        if ks >= 2 and text[ks - 2] == "\\" and nxt == "\\" and rd.at(ke + 1) == prev:
            j = ke + 2
        elif nxt == prev:
            j = ke + 1
        else:
            j = -1
        if j != -1:
            j = rd.blanks(j)
            if j is None or rd.at(j) != ":":
                return None
            j = rd.blanks(j + 1)
            if j is None:
                return None
            return _value_after_sep(rd, j, gate)
    # CLI flag.
    if "cli_flag" in row.inputs and key.startswith("--"):
        if nxt == "=":
            return _value_after_sep(rd, ke + 1, gate)
        j = rd.blanks(ke, at_least=1)
        if j is None:
            return None
        return _value_after_sep(rd, j, gate)
    if "assignment" not in row.inputs:
        return None
    # Assignment at a line start.
    if _line_lead_ok(text, ks):
        j = rd.blanks(ke)
        if j is None:
            return None
        sep = rd.at(j)
        if sep not in ("=", ":") or sep == "":
            return None
        after = rd.at(j + 1)
        if after == sep or (sep == ":" and after == "/"):
            return None
        j += 1
        if sep == ":" and after == "=":
            j += 1
        k = j
        while rd.at(k) == "*" and k - j < 3:
            k += 1
        k = rd.blanks(k)
        if k is None:
            return None
        return _value_after_sep(rd, k, gate)
    # ``KEY=value`` glued after a blank or a shell separator.
    if prev in _INLINE_BEFORE and nxt == "=" and rd.at(ke + 1) != "=":
        return _value_after_sep(rd, ke + 1, gate)
    return None


def detect_known_keys(
    text: str, rows: tuple[KnownKeyRow, ...], final: bool, low: str | None = None
) -> tuple[list[Hit], list[int]]:
    hits: list[Hit] = []
    opens: list[int] = []
    if not rows:
        return hits, opens
    if low is None:
        low = ascii_lower(text)
    # Gate: no key word in the text means no spelling can be there either.
    for word in cached(rows, gate_words):
        if word in low:
            break
    else:
        return hits, opens
    search, known = _prepared(rows)
    rd = _Reader(text, final)
    n = len(text)
    seen: set[int] = set()
    for sp in search:
        i = low.find(sp)
        while i != -1:
            ks = run_start(text, i, KEY_CHARS, KEY_MAX + 1)
            ke = run_end(text, i + len(sp), KEY_CHARS, KEY_MAX + 1)
            if ks in seen or ke - ks > KEY_MAX:
                i = low.find(sp, i + 1)
                continue
            if ke == n and not final:
                opens.append(max(0, ks - 2))
                i = low.find(sp, i + 1)
                continue
            seen.add(ks)
            key = text[ks:ke]
            row = known.get(normalise(key))
            if row is not None:
                try:
                    found = _read(rd, ks, ke, key, row)
                except _More:
                    opens.append(max(0, ks - 2))
                    found = None
                if found is not None:
                    vs, ve, tail = found
                    hits.append(Hit(vs, ve, row.id, row.tier, "known_key", "known_key", tail=tail))
            i = low.find(sp, i + 1)
    return hits, opens
