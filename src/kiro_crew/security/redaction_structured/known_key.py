"""Detector A: known keys in parsed inputs, with a value-shape gate.

The text is lowered once (ASCII only, so indices match the original); ``str.find``
locates each spelling of a known key and the key token's own boundaries are read by set
membership. The token names a row when its normalised name EQUALS one of the row's
keys (design open question M10: equality, not suffix): ASCII case folded and ``_``,
``-``, ``.`` and blanks dropped, so ``DB_PASSWORD``, ``dbPassword`` and
``--db-password`` are one name. A name no row lists is not read.

The input the key sits in decides how the value is read:

- XML element ``<SecretAccessKey>value</SecretAccessKey>``;
- JSON / dict / object member ``"key": value``, also with escaped quotes
  (``\\"key\\": \\"value\\"``), whose value ends before the escaped closing quote;
- CLI flag ``--aws-secret-access-key value`` or ``=value``;
- assignment at a line start (an optional unified-diff marker ``+``, ``-`` or space,
  blanks, optional ``export``/``set``/``setx``/``declare`` word, YAML ``- ``, Markdown
  ``*``), separator ``=`` or ``:``; ``KEY=value`` glued after a blank, ``;``, ``&``,
  ``|`` or ``(``; ``KEY = value`` after a blank on a longer line; or, for a gate that
  reads a fixed shape (``aws_secret_40``, ``b64_run``), ``KEY: value`` or ``KEY=value``
  anywhere on a line (a table cell ``| *SecretAccessKey:* ...``, a URL query). An assignment inside a quoted
  string on a longer line is not read (owner ruling: the parser is not widened for it).

The value gate then decides: ``aws_secret_40`` takes a run of exactly 40
AWS-secret-alphabet characters ending the value; ``b64_run`` takes a base64 run of at
least 100 characters (padding included); ``secret_literal`` takes a written value (the
text inside its quotes, or an unquoted word) of 8 to 512 characters that is not a
placeholder, does not read as code and does not read as words (:func:`_literal_gate`).
Only that run is replaced.
"""

from __future__ import annotations

from .charsets import ALNUM, B64STD, DIGITS, ascii_lower, cached, run_end, run_start
from .checks import is_placeholder, reads_as_example_words
from .model import PLAN_TAGS, TAIL_B64RUN, Hit
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
LITERAL_MIN = 8
LITERAL_MAX = 512
# An unquoted literal ends at a blank, a separator another field starts after, or a quote.
_LITERAL_STOPS = frozenset(" \t\r\n;&,\"'")
# An unquoted value holding one of these is an expression (a call, an index, a block).
_CODE_CHARS = frozenset("()[]{}")
# An unquoted value starting with one of these is a reference or an operator, not a literal.
_CODE_HEADS = frozenset("$%@&*")
_IDENT_CHARS = frozenset(ALNUM + "_")
_DIGIT_SET = frozenset(DIGITS)
# A value that opens an armour line is a key block, whose body the armour detector reads.
_ARMOUR_HEAD = "-----"
# How an AWS SigV4 credential scope ends, plain or URL-encoded, read with its ``_``
# dropped as a renderer may (``Credential=<key ID>/<date>/<region>/s3/aws4_request``):
# a key ID, an Identifier, and the scope it signs for.
_SCOPE_TAILS = ("/aws4request", "%2faws4request")
# A type annotation names a type, and a type's name is shorter than a generated value.
_TYPE_NAME_MAX = 20
_BLANKS = " \t"
_CONT = frozenset(ALNUM + "_-.=/+~%")
_LEAD_WORDS = frozenset({"", "export", "set", "setx", "declare", "-"})
_DIFF_MARKS = frozenset("+- ")
_INLINE_BEFORE = frozenset(" \t;&|(")
# Where a key a fixed-shape gate reads may start on a longer line: a URL query or a path
# segment included.
_MIDLINE_BEFORE = frozenset(" \t*|(,;?&/")
_QUOTES = "\"'"
# How the text around an unquoted value reads (:func:`_reads_as_code`): code that names
# variables, a file of written values, or a line-start mapping (YAML, ini).
_CODE = "code"
_LITERAL = "literal"
_YAML = "yaml"
_INI = "ini"


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


_Prepared = tuple[tuple[str, ...], dict[str, KnownKeyRow]]
# Search strings and lookups depend only on the rows, so they are built once per rows
# tuple (the loaded table's, in practice), not on every call.
_PREPARED: dict[int, tuple[tuple[KnownKeyRow, ...], _Prepared]] = {}
_PREPARED_MAX = 8


def _prepared(rows: tuple[KnownKeyRow, ...]) -> _Prepared:
    got = _PREPARED.get(id(rows))
    if got is not None and got[0] is rows:
        return got[1]
    if len(_PREPARED) >= _PREPARED_MAX:
        _PREPARED.clear()
    every = spellings(rows)
    # A spelling holding another one is found by that one in the same key token.
    search = tuple(sp for sp in every if not any(o != sp and o in sp for o in every))
    known = {normalise(k): row for row in rows for k in row.keys}
    prepared: _Prepared = (search, known)
    _PREPARED[id(rows)] = (rows, prepared)
    return prepared


def gate_words(rows: tuple[KnownKeyRow, ...]) -> tuple[str, ...]:
    """Lower-case words one of which every search string holds.

    A spelling joins the key's words with one separator (or none), so each word appears
    whole in it; the longest word is the most selective test.
    """
    return tuple(sorted({max(_words(key), key=len) for row in rows for key in row.keys}))


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


def _reads_as_code(value: str, key: str, context: str) -> bool:
    """Whether an UNQUOTED value is a code expression rather than a written literal.

    A call, an index or a block (``getpass()``, ``os.environ["X"]``), a reference
    (``$X``, ``%X%``, ``@x``), a dotted name (``settings.DB_PASSWORD``), the key's own name
    (``password=password``), or, where code names a variable, a bare identifier
    (``password = user_password``, ``f(password=pw)``). A context that names no variable
    takes a bare word as written (*context* ``literal``): an UPPER-case environment key
    (``DB_PASSWORD=...``), a command-line flag (``--password``), a ``key=value`` line with
    no blank around the ``=`` (an env, ini or properties file), and a field of a
    ``;``-separated connection string. A line-start mapping takes a bare word holding a
    digit as written (*context* ``yaml``, ``password: ...``, and ``ini``,
    ``password = ...``), and so does YAML a word longer than a type's name: a type
    annotation (``password: SecretStr``) names a type, which holds no digit.
    """
    if value[0] in _CODE_HEADS or any(ch in _CODE_CHARS for ch in value):
        return True
    parts = value.split(".")
    if len(parts) > 1 and all(p and set(p) <= _IDENT_CHARS and not p[0].isdigit() for p in parts):
        return True
    if normalise(value) == normalise(key):
        return True
    if context == _LITERAL or key.isupper() or key.startswith("-"):
        return False
    if context in (_YAML, _INI) and not _DIGIT_SET.isdisjoint(value):
        return False
    if context == _YAML and len(value) > _TYPE_NAME_MAX:
        return False
    return set(value) <= _IDENT_CHARS


def _closes(rd: _Reader, e: int, closer: str) -> bool:
    return rd.at(e) == closer[0] and (len(closer) == 1 or rd.at(e + 1) == closer[1])


def _literal_gate(
    rd: _Reader, vs: int, closer: str, key: str, context: str = _CODE
) -> tuple[int, int, str] | None:
    """A written credential value after a credential label (gate ``secret_literal``)."""
    text, n = rd.text, rd.n
    if closer:
        e = vs
        while True:
            ch = rd.at(e)
            if ch in ("", "\n", "\r") or e - vs > LITERAL_MAX or _closes(rd, e, closer):
                break
            e += 1
        if not _closes(rd, e, closer):
            return None
        value = text[vs:e]
        if value.startswith(("$(", "`")):
            return None
    else:
        e = vs
        while e < n and text[e] not in _LITERAL_STOPS and e - vs <= LITERAL_MAX:
            e += 1
        if e == n and not rd.final:
            raise _More
        value = text[vs:e]
        if not value or _reads_as_code(value, key, context):
            return None
    if not LITERAL_MIN <= len(value) <= LITERAL_MAX or is_placeholder(value):
        return None
    if any(text.startswith(tag, vs) for tag in PLAN_TAGS):
        return None  # already replaced, byte for byte: redaction is a fixed point
    if value.startswith(_ARMOUR_HEAD) or reads_as_example_words(value):
        return None
    if ascii_lower(value).replace("_", "").endswith(_SCOPE_TAILS):
        return None
    return vs, e, ""


def _gate(
    rd: _Reader, vs: int, gate: str, closer: str, key: str = "", context: str = _CODE
) -> tuple[int, int, str] | None:
    text, n = rd.text, rd.n
    if gate == "secret_literal":
        return _literal_gate(rd, vs, closer, key, context)
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
            # A full stop is outside the secret's alphabet, so it ends the value: a
            # sentence's end, or a file suffix (``...=<secret>.txt``).
            if nxt == ".":
                nxt = ""
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


#: Gates that read a value up to its closing quote.
_QUOTED_GATES = frozenset({"aws_secret_40", "secret_literal"})


def _value_after_sep(
    rd: _Reader, j: int, gate: str, key: str = "", context: str = _CODE
) -> tuple[int, int, str] | None:
    """Read an optional quote, then gate the value."""
    ch = rd.at(j)
    if ch and ch in _QUOTES:
        return _gate(rd, j + 1, gate, ch if gate in _QUOTED_GATES else "", key)
    if ch == "\\" and rd.at(j + 1) == '"':
        # An escaped quote closes the value too: the escape stays with it.
        return _gate(rd, j + 2, gate, '\\"' if gate in _QUOTED_GATES else "", key)
    return _gate(rd, j, gate, "", key, context)


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
        if gate == "secret_literal":
            # A written value ends at its own closing tag (``<token>`` in prose is no
            # element).
            found = _gate(rd, ke + 1, gate, "<", key)
            if found is None:
                return None
            for k, ch in enumerate(close):
                if rd.at(found[1] + k) != ch:
                    return None
            return found
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
            return _value_after_sep(rd, j, gate, key)
    # CLI flag.
    if "cli_flag" in row.inputs and key.startswith("--"):
        if nxt == "=":
            return _value_after_sep(rd, ke + 1, gate, key)
        j = rd.blanks(ke, at_least=1)
        if j is None:
            return None
        return _value_after_sep(rd, j, gate, key)
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
        sep_at = j
        j += 1
        if sep == ":" and after == "=":
            j += 1
        k = j
        while rd.at(k) == "*" and k - j < 3:
            k += 1
        k = rd.blanks(k)
        if k is None:
            return None
        flush = sep == "=" and sep_at == ke and k == j
        context = _LITERAL if flush else (_YAML if sep == ":" else _INI)
        return _value_after_sep(rd, k, gate, key, context)
    # ``KEY=value`` glued after a blank or a shell separator.
    if prev in _INLINE_BEFORE and nxt == "=" and rd.at(ke + 1) != "=":
        return _value_after_sep(rd, ke + 1, gate, key, _LITERAL if prev == ";" else _CODE)
    # ``aws configure set <key> <value>``: the CLI's own way to write a key.
    if prev in _BLANKS and prev and text[max(0, ks - 64) : ks].rstrip().endswith("configure set"):
        j = rd.blanks(ke, at_least=1)
        if j is not None:
            return _value_after_sep(rd, j, gate, key, _LITERAL)
    # ``KEY = value`` after a blank on a longer line (``User: password = ...``).
    if prev in _BLANKS and prev:
        j = rd.blanks(ke, at_least=1)
        if j is not None and rd.at(j) == "=" and rd.at(j + 1) not in ("=", ""):
            k = rd.blanks(j + 1, at_least=1)
            if k is not None:
                return _value_after_sep(rd, k, gate, key)
    # ``KEY: value`` on a longer line, for a gate whose fixed shape decides the value.
    if gate != "secret_literal" and prev and prev in _MIDLINE_BEFORE:
        j = rd.blanks(ke)
        if j is not None and rd.at(j) in (":", "=") and rd.at(j + 1) not in (":", "=", "/"):
            k = j + 1
            while rd.at(k) == "*" and k - j <= 3:
                k += 1
            k = rd.blanks(k)
            if k is not None:
                return _value_after_sep(rd, k, gate, key)
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
    # Gate: no key word in the text means no search string can be there either.
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
