"""The keyed-value fixture is the one oracle every copy of the value scanner answers to.

``test/redaction_keyed_value_fixture.py`` generates it from the canonical redactor;
this file pins (1) that the committed JSON equals a fresh generation, so the file
cannot drift from the code, (2) that every row holds on the redactor, on the stream
at six chunk sizes, on the hard URL floor and on the packaging scan's vendored
scanner, and (3) that the scanner is linear in its input by construction. The
chat mirror reads the same file in
``website/src/test/sanitizeCredentials.fixture.test.ts``.
"""

from __future__ import annotations

import json
import re
import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from redaction_keyed_value_fixture import FIXTURE_PATH, TAG, VALUE, build_rows, load_rows, render

ROWS = load_rows()
_IDS = [f"{row['key']}-{row['shape']}" for row in ROWS]
_SIZES = (1, 3, 7, 50, 200, 513)
#: Shapes whose value, or the word the redactor's anchor claims, stands on the
#: line AFTER the separator: a line-by-line reader sees a key with no value there.
_VALUE_ON_THE_NEXT_LINE = frozenset(
    {
        "empty-value",
        "yaml-next-line-value",
        "json-next-line-value",
        "yaml-block-empty-assignment-then-key",
    }
)


def test_the_committed_fixture_is_a_fresh_generation() -> None:
    """Regenerate with ``PYTHONPATH=src python test/redaction_keyed_value_fixture.py``.
    The file is the compact encoding (``encode_rows``); what the suites read is the
    decoded row list, equal row for row to a fresh generation."""
    assert FIXTURE_PATH.read_text(encoding="utf-8") == render()
    assert build_rows() == ROWS
    assert len(ROWS) > 150


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_the_redactor_answers_the_row(row: dict) -> None:
    from kiro_crew.security import credential_matches, redact_credentials

    once, warnings = redact_credentials(row["text"])
    assert once == row["expected"], row["shape"]
    assert len(warnings) == row["warnings"], row["shape"]
    assert redact_credentials(once) == (once, []), row["shape"]
    assert (next(credential_matches(row["text"]), None) is not None) is row["live"], row["shape"]


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_the_stream_equals_the_batch_pass_at_every_chunk_size(row: dict) -> None:
    from kiro_crew.security import StreamRedactor

    text, expected = row["text"], row["expected"]
    for size in _SIZES:
        redactor = StreamRedactor()
        out = "".join(redactor.feed(text[i : i + size]) for i in range(0, len(text), size))
        assert out + redactor.flush() == expected, (row["shape"], size)


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_the_hard_floor_reads_the_row_as_the_redactor_does(row: dict) -> None:
    from kiro_crew.security import hard_credential_hit

    assert hard_credential_hit(row["text"]) is row["live"], row["shape"]
    assert hard_credential_hit(row["expected"]) is False, row["shape"]


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_the_packaging_scans_vendored_scanner_reads_the_row_as_the_redactor_does(
    row: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps.builtins.aws_control.crew.packaging.pipeline import scan as pkg_scan

    # The labelled matcher alone (the canonical detector and redactor masked off),
    # line by line as the scan runs it; access-key-id spellings have no labelled
    # entry in the packaging scan, which reads secrets and session tokens.
    if "access_key_id" in row["key"].lower() or row["key"] == "AccessKeyId":
        pytest.skip("the packaging scan's labelled entry covers secrets and session tokens")
    matcher = dict(pkg_scan._HARD_PATTERNS)["aws-secret-labelled"]
    found = any(matcher.search(line) is not None for line in row["text"].splitlines())
    if row["shape"] in _VALUE_ON_THE_NEXT_LINE:
        # The redactor's anchor runs `\s*` across the line break after the
        # separator and reads the next line's first word: the value a pretty
        # printer or a YAML author wrote there, or after an EMPTY assignment the
        # next key's name (the base-era rule, an over-redaction and never a
        # leak); the packaging scan reads one line at a time, where `key=` alone
        # is a key with no value.
        assert found is False, row["shape"]
    else:
        assert found is row["live"], row["shape"]
    assert all(matcher.search(line) is None for line in row["expected"].splitlines()), row["shape"]
    # And the vendored claim is byte-identical to the canonical one on every line.
    from kiro_crew.security import scan_keyed_value

    for line in row["text"].splitlines():
        for anchor in pkg_scan._LABEL_RE.finditer(line):
            vendored = pkg_scan._scan_value(line, anchor.end())
            canonical = scan_keyed_value(line, anchor.end())
            assert vendored == (
                canonical.start,
                canonical.end,
                canonical.closes,
                canonical.opener,
            ), (
                row["shape"],
                line,
            )


def test_the_scanner_is_linear_in_its_input() -> None:
    """One token per step: the time to scan grows with the text, not with its square.

    Adversarial shapes for a backtracking grammar -- a run of backslashes, a run of
    doubled quotes, a run of escaped-whitespace heads, an unterminated quote over a
    long line -- cost the same per byte as prose. Measured at two sizes a factor of
    eight apart; a quadratic scanner would show ~64x, a linear one ~8x (the bound
    below leaves room for timer noise, as ``test_security_regex_linearity.py`` does).
    """
    from kiro_crew.security import scan_keyed_value

    def shapes(n: int) -> list[str]:
        return [
            "k=" + "\\\\" * n,
            'k="' + '""' * n + "x",
            'k="' + "\\t" * n + VALUE + '"',
            'k="' + "a" * n,
            'k=\\"' + "\\\\" * n + '\\"',
            "k=" + (TAG * (n // len(TAG) + 1)),
            # A run of apostrophe candidates before a far close, and before none:
            # an other-kind quote is a byte of the value, read once.
            'k="' + "' " * n + VALUE + '"',
            'k="' + "' " * n + VALUE,
        ]

    def cost(n: int) -> float:
        best = float("inf")
        for _ in range(3):
            t0 = time.perf_counter()
            for text in shapes(n):
                scan_keyed_value(text, 2)
            best = min(best, time.perf_counter() - t0)
        return best

    small, large = cost(2_000), cost(16_000)
    assert large < small * 24, (small, large)


def test_the_look_back_is_linear_in_the_line() -> None:
    """The look-back along the line runs once per traversal, not once per anchor:
    a line of many key-anchored pairs after a long prefix of quotes and escapes
    costs the redactor work proportional to the line, measured as executed lines
    of ``redaction`` (``conftest.assert_linear_work``), not as time."""
    from conftest import assert_linear_work
    from kiro_crew import security as _security
    from kiro_crew.security import redact_credentials

    def text(n: int) -> str:
        prefix = "'a' \"b\" \\\" 'c''d' " * (n // 20)
        pairs = "".join(f"aws_secret_access_key={VALUE}{i} " for i in range(n // 40))
        return prefix + '{"template":"' + pairs + '","keep":1}\n'

    assert_linear_work(_security.redaction, text, redact_credentials, sizes=(2_000, 4_000, 8_000))


# ─────────────────────────────────────────────────────────────────────────────
# the document property: a redacted JSON document is still that document
# ─────────────────────────────────────────────────────────────────────────────

_CREDENTIAL_KEY_WORDS = ("secretaccesskey", "sessiontoken", "accesskeyid", "bearer")


def _executable_lines(func) -> set[int]:
    """The line numbers that carry bytecode in *func* and the code objects nested
    in it (comprehensions), minus the ``def`` line, which fires no LINE event."""
    lines: set[int] = set()
    stack = [func.__code__]
    while stack:
        code = stack.pop()
        lines.update(line for _start, _end, line in code.co_lines() if line)
        stack.extend(const for const in code.co_consts if hasattr(const, "co_lines"))
    lines.discard(func.__code__.co_firstlineno)
    return lines


def _lines_reached_in(module, run) -> set[int]:
    """The set of *module* lines executed while ``run()`` runs, read with
    ``sys.monitoring`` on a tool id of its own (``conftest.lines_executed_in``
    counts them; this pin needs which ones)."""
    import sys

    monitoring = sys.monitoring
    tool = next(i for i in range(6) if monitoring.get_tool(i) is None)
    target = module.__file__
    seen: set[int] = set()

    def on_line(code, line):
        if code.co_filename != target:
            return monitoring.DISABLE
        seen.add(line)
        return None

    monitoring.use_tool_id(tool, "scanner-lines-reached")
    monitoring.register_callback(tool, monitoring.events.LINE, on_line)
    monitoring.set_events(tool, monitoring.events.LINE)
    try:
        run()
    finally:
        monitoring.set_events(tool, 0)
        monitoring.register_callback(tool, monitoring.events.LINE, None)
        monitoring.free_tool_id(tool)
        monitoring.restart_events()
    return seen


def test_every_scanner_rule_is_reached_by_a_fixture_row() -> None:
    """The three copies of the value grammar -- the canonical scanner, the
    packaging scan's copy and the chat mirror -- are held in step by this
    fixture: the parity tests above run both copies over every row. That holds
    only while every RULE of the grammar has a row: a rule added to the canonical
    scanner with no row to show it passes the parity tests whatever the copies
    do. So every executable line of the grammar's functions must be reached by
    some row, through the batch pass, the hard floor, a scan that reads the
    enclosing literal back itself, and the stream at two chunk sizes. A line
    this lists has no row; add the row, and the two copies are held to it."""
    import inspect

    from kiro_crew.security import StreamRedactor, hard_credential_hit, redact_credentials
    from kiro_crew.security import redaction as _redaction

    grammar = (
        _redaction._advance_line_state,
        _redaction._enclosing_at,
        _redaction._innermost,
        _redaction._opener_at,
        _redaction._inner_token,
        _redaction._value_head_end,
        _redaction._prefix_run_end,
        _redaction._no_value_opens_at,
        _redaction._tag_run_end,
        _redaction.scan_keyed_value,
    )

    def run() -> None:
        for row in ROWS:
            text = row["text"]
            redact_credentials(text)
            hard_credential_hit(text)
            for match in _redaction._CREDENTIAL_PATTERNS.finditer(text):
                if match.lastindex is not None:
                    at, start, end = _redaction._scan_at(text, match)
                    if start == end:
                        _redaction.scan_keyed_value(text, start)
            for size in (1, 7):
                redactor = StreamRedactor()
                for i in range(0, len(text), size):
                    redactor.feed(text[i : i + size])
                redactor.flush()

    reached = _lines_reached_in(_redaction, run)
    unreached: list[str] = []
    for func in grammar:
        first = func.__code__.co_firstlineno
        source = inspect.getsource(func).splitlines()
        for line in sorted(_executable_lines(func) - reached):
            unreached.append(f"{func.__qualname__} line {line}: {source[line - first].strip()}")
    assert not unreached, "scanner rules no fixture row reaches:\n" + "\n".join(unreached)


def _mentions_a_credential_key(text: str) -> bool:
    lowered = text.lower().replace("_", "")
    return any(word in lowered for word in _CREDENTIAL_KEY_WORDS)


def _shape(doc: object) -> object:
    """The document with every string leaf replaced by ``"s"``: its structure."""
    if isinstance(doc, dict):
        return {key: _shape(value) for key, value in doc.items()}
    if isinstance(doc, list):
        return [_shape(item) for item in doc]
    return "s" if isinstance(doc, str) else doc


def _leaves(doc: object, under_credential_key: bool = False) -> list[tuple[str, bool]]:
    """Every string leaf with whether a credential key names it or an ancestor."""
    if isinstance(doc, dict):
        found: list[tuple[str, bool]] = []
        for key, value in doc.items():
            found.extend(_leaves(value, under_credential_key or _mentions_a_credential_key(key)))
        return found
    if isinstance(doc, list):
        return [leaf for item in doc for leaf in _leaves(item, under_credential_key)]
    return [(doc, under_credential_key)] if isinstance(doc, str) else []


def _assert_still_the_document(text: str, out: str) -> None:
    """*out* parses as JSON, has *text*'s structure, and differs from it only in
    leaves that carry a credential key or sit under one."""
    before, after = json.loads(text), json.loads(out)
    assert _shape(before) == _shape(after)
    for (was, under), (now, _) in zip(_leaves(before), _leaves(after), strict=True):
        if not under and not _mentions_a_credential_key(was):
            assert now == was


def _wrappers(text: str) -> list[str]:
    """*text* as the string value of generated JSON documents: compact, indented,
    non-ASCII escaped and not, nested in an object and in an array."""
    return [
        json.dumps({"template": text, "keep": 1}, separators=(",", ":")),
        json.dumps({"template": text, "keep": 1}, indent=2),
        json.dumps({"template": text, "keep": 1}, ensure_ascii=False),
        json.dumps({"outer": {"template": text}, "keep": [1, text]}),
        json.dumps([text, {"keep": 1}]),
    ]


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


_JSON_ROWS = [row for row in ROWS if _is_json(row["text"])]


@pytest.mark.parametrize("row", _JSON_ROWS, ids=[f"{r['key']}-{r['shape']}" for r in _JSON_ROWS])
def test_every_json_document_in_the_fixture_stays_a_document(row: dict) -> None:
    """A row that IS a JSON document redacts to a JSON document of the same
    structure, every leaf that names no credential key byte-identical. The
    scanner's rules are all stopping rules, so an unknown spelling costs an
    over-redaction inside a value and never a byte of the document around it.
    The one way that promise breaks is the scanner mistaking an enclosing
    literal's boundary for the value's (a bare opener inside a JSON string, an
    interior apostrophe, an empty assignment before the enclosing close), and
    the look-back along the line is what ends the class."""
    from kiro_crew.security import redact_credentials

    _assert_still_the_document(row["text"], row["expected"])
    assert redact_credentials(row["expected"]) == (row["expected"], [])


_ALREADY_ESCAPED = (
    "embedded",
    "escaped-json",
    "wrapped-json",
    "tag-filled-embedded",
    "enclosing-yaml-sq-escaped",
    "enclosing-yaml-sq-sq-value",
)


def _one_level_deep(row: dict) -> bool:
    """Whether wrapping the row in a quoted literal escapes it ONCE. The shapes
    written in the escaped encoding already are one level deep, and the scanner
    reads one level: a doubly escaped quote is not an opener to it."""
    return not row["shape"].startswith(_ALREADY_ESCAPED)


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_every_row_wrapped_as_a_json_string_stays_a_document(row: dict) -> None:
    """Every row's text as the string value of a JSON document -- the pair in the
    escaped encoding inside a literal the look-back finds -- redacts to a parsing
    document with the sibling field intact and no secret byte left, through the
    redactor and the stream alike."""
    from kiro_crew.security import StreamRedactor, redact_credentials

    if not _one_level_deep(row):
        pytest.skip("already in the escaped encoding; wrapped it would be two levels deep")
    for text in _wrappers(row["text"]):
        out, _warnings = redact_credentials(text)
        _assert_still_the_document(text, out)
        if not row["shape"].startswith("empty-") and row["shape"] not in _VALUE_ON_THE_NEXT_LINE:
            # A value on the line after its separator, written INSIDE a JSON
            # string, follows the escape pair `\n`, which is not whitespace to the
            # anchor (nor to the base's), so it is not read as the pair's value;
            # the document stays intact and the residual is disclosed in the PR.
            assert VALUE not in out, row["shape"]
        assert redact_credentials(out) == (out, []), row["shape"]
        redactor = StreamRedactor()
        streamed = "".join(redactor.feed(text[i : i + 7]) for i in range(0, len(text), 7))
        assert streamed + redactor.flush() == out, row["shape"]


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_every_row_wrapped_in_a_yaml_scalar_keeps_its_sibling_key(row: dict) -> None:
    """The YAML wrappers: the row as a block scalar (every row), and as a
    double-quoted and a single-quoted scalar (the rows not already in the escaped
    encoding), each with a sibling key after it. The redacted document still
    loads, the sibling key is untouched, and the scalar holds no secret byte."""
    import yaml

    from kiro_crew.security import redact_credentials

    body = row["text"].rstrip("\n")
    wrappers = ["text: |\n" + "".join(f"  {line}\n" for line in body.split("\n")) + "keep: 1\n"]
    if "\n" not in body and _one_level_deep(row):
        wrappers.append(yaml.safe_dump({"text": body, "keep": 1}, default_style='"'))
        wrappers.append(yaml.safe_dump({"text": body, "keep": 1}, default_style="'"))
    for text in wrappers:
        out, _warnings = redact_credentials(text)
        loaded = yaml.safe_load(out)
        assert isinstance(loaded, dict) and loaded["keep"] == 1, (row["shape"], out)
        if not row["shape"].startswith("empty-"):
            assert VALUE not in str(loaded["text"]), (row["shape"], out)


def _assert_a_fixed_point(out: str, redact) -> None:
    """A second pass over the redactor's output changes no byte, and a third pass
    answers exactly as the second did, warnings included. The second pass may
    still warn: a tag-filled value whose quote never closes is left as it is and
    warned on every pass by design (a presence-only reader must not be told the
    line is clean), so the fixed point is the text plus a stable warning list."""
    second = redact(out)
    assert second[0] == out
    assert redact(second[0]) == second


#: The one residual the sweep names rather than asserts: an EMPTY assignment at a
#: line's end (`key=` with nothing after the separator) followed by a line that
#: starts with a key. The separator's whitespace crosses the line break, as the
#: base's does so that a value written on the line below its key is still
#: redacted, and the next line's first word is taken as the empty pair's value:
#: a fail-closed over-redaction the base has too (`text: |` + `  key=` + `keep: 1`
#: loses `keep`). Kept for parity with the base and disclosed in the PR; the
#: block-scalar wrapper, the one whose next line is key-shaped, is skipped for
#: exactly these leaves and no other.
_EMPTY_ASSIGNMENT_AT_LINE_END = re.compile(r"[:=]\s*\Z")


def _assert_documents_survive(leaf: str) -> None:
    """*leaf* as the string value of JSON documents (compact, indented, non-ASCII
    kept, nested in an object and an array, and ONCE MORE as a JSON string inside
    a JSON string) and of YAML documents (a block scalar; a double- and a
    single-quoted scalar when the leaf has no raw line break): every redacted
    document still loads, keeps its structure and sibling key, changes only
    leaves that carry a credential key or sit under one, and is a fixed point.
    The block scalar is skipped for the residual ``_EMPTY_ASSIGNMENT_AT_LINE_END``
    names."""
    import yaml

    from kiro_crew.security import redact_credentials

    nested = json.dumps({"t": leaf})
    for text in _wrappers(leaf) + [json.dumps({"text": nested, "keep": 1})]:
        out, _warnings = redact_credentials(text)
        _assert_still_the_document(text, out)
        _assert_a_fixed_point(out, redact_credentials)
    yaml_texts = []
    if not _EMPTY_ASSIGNMENT_AT_LINE_END.search(leaf):
        yaml_texts.append(
            "text: |\n" + "".join(f"  {line}\n" for line in leaf.split("\n")) + "keep: 1\n"
        )
    if "\n" not in leaf:
        yaml_texts.append(yaml.safe_dump({"text": leaf, "keep": 1}, default_style='"'))
        yaml_texts.append(yaml.safe_dump({"text": leaf, "keep": 1}, default_style="'"))
    for text in yaml_texts:
        out, _warnings = redact_credentials(text)
        loaded = yaml.safe_load(out)
        assert isinstance(loaded, dict) and loaded["keep"] == 1, (text, out)
        _assert_a_fixed_point(out, redact_credentials)


#: The shape of a key-anchored pair as a leaf string: what stands BEFORE the key
#: (prose, a quote opening a literal the key then sits inside, a JSON fragment),
#: the separator, the value's opener, the value's TAIL (the bytes before its
#: close: escape runs of one to four backslashes, an escaped quote, a quote of
#: the other kind, a tag, nothing), the closer (matching, missing, the other
#: kind) and what follows. The three boundary shapes the scanner is pinned
#: against are points of this product: an empty assignment before the enclosing
#: close ``("", "=", "", "", "", "")``, an escaped value ending in an inner
#: backslash before it ``('"', '": ', '"', "x\\", "", "")``, and a bare-quoted
#: value inside an escaped inner literal ending the same way
#: ``('"', "=", "'", "x\\", "", "")``.
_PAIR_PREFIXES = ["", '"', "'", 'x="', "x='", '{"']
_PAIR_SEPARATORS = ["=", '": ', ": "]
_PAIR_OPENERS = ["", "'", '"']
_PAIR_TAILS = [
    "",
    "x",
    "x\\",
    "x\\\\",
    "x\\\\\\",
    "x\\\\\\\\",
    'x\\"',
    "x\\'",
    "x'",
    'x"',
    TAG,
    VALUE + "\\",
]
_PAIR_CLOSERS = ["", "'", '"']
_PAIR_SUFFIXES = ["", '"', "'", ', "k": 1', "}", " more"]


def _pair_leaf(
    prefix: str, key: str, sep: str, opener: str, tail: str, closer: str, suffix: str
) -> str:
    return prefix + key + sep + opener + tail + closer + suffix


@pytest.mark.parametrize("prefix", _PAIR_PREFIXES, ids=repr)
@pytest.mark.parametrize("opener", _PAIR_OPENERS, ids=repr)
def test_every_pair_shape_in_the_sweep_keeps_its_documents(prefix: str, opener: str) -> None:
    """The deterministic sweep: every point of the pair-shape product, for one
    bare and one JSON-spelled key, inside the JSON and YAML documents of
    :func:`_assert_documents_survive`. It holds the three boundary findings by
    construction (no seed finds them: they are enumerated), and every neighbour
    of each: an escape run of any length before the inner close, before the
    enclosing close, with and without the value's close, a key inside an inner
    literal and a value quoted the other way."""
    for key in ("aws_secret_access_key", "SecretAccessKey"):
        for sep in _PAIR_SEPARATORS:
            for tail in _PAIR_TAILS:
                for closer in _PAIR_CLOSERS:
                    for suffix in _PAIR_SUFFIXES:
                        leaf = _pair_leaf(prefix, key, sep, opener, tail, closer, suffix)
                        try:
                            _assert_documents_survive(leaf)
                        except AssertionError as failure:
                            raise AssertionError(f"leaf {leaf!r}: {failure}") from failure


_PAIR_ATOMS = [
    "x",
    "ab",
    VALUE,
    TAG,
    "'",
    '"',
    "\\",
    "\\\\",
    "\\\\\\",
    "\\\\\\\\",
    "\\n",
    "\\t",
    '\\"',
    "\\'",
    ",",
    "}",
    " ",
    "=",
    "/",
    "\n",
]

_pair_leaves = st.builds(
    _pair_leaf,
    st.sampled_from(_PAIR_PREFIXES),
    st.sampled_from(
        ["aws_secret_access_key", "SecretAccessKey", "aws_session_token", "AccessKeyId", "Bearer"]
    ),
    st.sampled_from(_PAIR_SEPARATORS + [" "]),
    st.sampled_from(_PAIR_OPENERS),
    # The value's body: atoms in any order, so an escape run of one to four
    # backslashes, an escaped quote, a quote of either kind, a tag or the secret
    # stands at EVERY position of the value, its last one included.
    st.lists(st.sampled_from(_PAIR_ATOMS), max_size=5).map("".join),
    st.sampled_from(_PAIR_CLOSERS),
    st.sampled_from(_PAIR_SUFFIXES + ["\n", "\\"]),
)


@settings(max_examples=1500, deadline=None, derandomize=True)
@given(leaf=_pair_leaves)
def test_any_generated_pair_keeps_its_documents(leaf: str) -> None:
    """The property behind the sweep, over GENERATED pairs: the same shape with
    the value's body drawn from atoms in any order (escape runs of one to four
    backslashes, escaped and bare quotes of both kinds, inner line breaks,
    structural bytes, the secret, a tag), five keys, and the leaf written into
    the JSON documents, nested once more as JSON in a string, and into the YAML
    scalars. Derandomized so CI sees one fixed sequence; the sweep above holds
    the known findings whatever this draws."""
    _assert_documents_survive(leaf)


@settings(max_examples=400, deadline=None, derandomize=True)
@given(
    documents=st.recursive(
        st.dictionaries(
            st.sampled_from(
                ["template", "note", "SecretAccessKey", "aws_session_token", "AccessKeyId"]
            ),
            st.tuples(
                st.lists(
                    st.sampled_from(
                        list("ab=:,'\" \\/{}[]\n\t")
                        + [VALUE, TAG, "aws_secret_access_key=", "Bearer ", '"SecretAccessKey": "']
                    ),
                    max_size=8,
                ).map("".join),
                # The leaf's TAIL: an escape run of every parity and mix, so a
                # value ends in one, two or three backslashes, in an escaped
                # quote, or in a quote-backslash pair before the literal's close.
                st.sampled_from(
                    ["", "\\", "\\\\", "\\\\\\", '\\"', '"\\', '\\"\\', '\\\\"\\\\', "\\\n"]
                ),
            ).map("".join),
            max_size=4,
        ),
        lambda inner: st.dictionaries(
            st.sampled_from(["outer", "items"]),
            st.one_of(inner, st.lists(inner, max_size=2)),
            max_size=2,
        ),
        max_leaves=6,
    ),
    indent=st.sampled_from([None, 2]),
    ensure_ascii=st.booleans(),
)
def test_any_json_document_redacts_to_a_json_document(
    documents: dict, indent: int | None, ensure_ascii: bool
) -> None:
    """The property behind the fixture's documents, over GENERATED ones: keys
    plain and credential, values made of quotes, apostrophes, backslashes,
    separators, line breaks, the synthetic secret, a redaction tag, a bare pair
    and a JSON-spelled pair, each value ending in an escape run of any parity
    (a value whose last byte is a backslash, written inside a JSON string, is
    where the enclosing close follows a complete escape pair), serialized
    compact and indented. The output parses, keeps the structure, changes only
    leaves that carry a credential key or sit under one, and is a fixed point."""
    from kiro_crew.security import redact_credentials

    text = json.dumps(documents, indent=indent, ensure_ascii=ensure_ascii)
    out, _warnings = redact_credentials(text)
    _assert_still_the_document(text, out)
    _assert_a_fixed_point(out, redact_credentials)


#: The bytes a value may OPEN on in the leak oracle: the structural bytes the
#: scanner treats specially where a value would start, both quotes, a backslash,
#: a bracket, a parenthesis and whitespace, the line breaks among it. A value
#: opening on one of them and going on is still the value, and its token must
#: vanish. The JSON wrappers write a raw line break or tab as the escaped pair
#: (``\n``, ``\r``, ``\t``) inside the string literal, so the same opening byte
#: reaches the scanner raw in the YAML and base64 documents and escaped in the
#: JSON ones: the escaped break heading a value inside a JSON string was a
#: shape no generator reached, and it leaked.
_LEAK_OPENING_BYTES = ["]", "}", ",", "'", '"', "\\", "[", "(", " ", "\t", "\n", "\r", ""]
#: Prefix RUNS a value may open on: a structural byte (``]``, ``}``, ``,``)
#: followed by one or two more structural bytes or backslashes, every pair of
#: them and the runs of three in which a backslash stands first, inside and
#: last among the structural bytes. The base's value class admits ``]`` and a
#: backslash, so the run and the secret after it are its value; a judgement of
#: the run's first byte alone read ``]`` after ``]`` as nothing value-like, and
#: the secret after it stood in plaintext in every wrapper. A backslash never
#: heads a run here: a value opening on a backslash is the single ``\\`` opening
#: above, read as the base reads it.
_LEAK_PREFIX_RUNS = [head + tail for head in "]}," for tail in "]},\\"] + [
    "]]]",
    "}}}",
    ",,,",
    "]\\]",
    ",\\,",
    "],\\",
    ",]\\",
    "}]\\",
]
_LEAK_OPENINGS = _LEAK_OPENING_BYTES + _LEAK_PREFIX_RUNS
#: The whitespace and backslash tokens a token may CARRY inside itself, so a
#: value's body holds an escaped line break, tab or backslash (as the JSON
#: wrappers write them) at a position that is not its head, and the LITERAL
#: two-byte spellings ``\n``, ``\t``, ``\r`` (a backslash and a letter, as a
#: percent-decoded URL path or a bare line carries them), which are bytes of the
#: value to the base's grammar and to this scanner's.
_LEAK_INNER_TOKENS = ["", "\n", "\r", "\t", "\\", "\\n", "\\t", "\\r"]
_LEAK_KEYS = ["aws_secret_access_key", "SecretAccessKey", "aws_session_token", "AccessKeyId"]
_LEAK_SEED = 20261009


def _random_tokens(count: int) -> list[str]:
    """Derandomized value tokens: letters and digits only, so the token itself
    carries no byte the scanner reads as a delimiter; what it OPENS on, and the
    one whitespace or backslash token it may carry inside, are the oracle's
    variables."""
    import random
    import string

    rng = random.Random(_LEAK_SEED)
    alphabet = string.ascii_letters + string.digits
    return ["".join(rng.choice(alphabet) for _ in range(24)) for _ in range(count)]


def _leak_values(opening: str, token: str) -> list[str]:
    """The values the oracles read for one *token*: opening on *opening*, and
    with each inner token of :data:`_LEAK_INNER_TOKENS` carried at its middle."""
    half = len(token) // 2
    return [opening + token[:half] + inner + token[half:] for inner in _LEAK_INNER_TOKENS]


def _leak_documents(leaf: str) -> list[tuple[str, str]]:
    """The documents the leak and stream oracles read *leaf* through, named: the
    JSON wrappers, JSON in a string (two levels deep), the YAML block scalar, and
    the leaf base64-encoded inside a JSON string, the shape a stream decodes."""
    import base64

    encoded = base64.b64encode(leaf.encode()).decode()
    docs = [(f"json-{i}", text) for i, text in enumerate(_wrappers(leaf))]
    docs.append(("json-in-string", json.dumps({"text": json.dumps({"t": leaf}), "keep": 1})))
    docs.append(
        (
            "yaml-block",
            "text: |\n" + "".join(f"  {line}\n" for line in leaf.split("\n")) + "keep: 1\n",
        )
    )
    docs.append(("base64-in-json", '{"text": "log ' + encoded + ' "}'))
    return docs


def _token_stands_outside_the_pair(prefix: str, sep: str, opener: str, opening: str) -> bool:
    """The residuals the leak oracle NAMES rather than asserts: shapes in which the
    token never is the pair's value under a literal-aware reading, and which the
    base's branches match nothing on either. (1) A doubled quote where the value
    opens (`key=''<t>`) is an empty quoted value with bytes after it. (2) The
    opener is the close of the literal the key sits in (`'key='<t>`): the pair's
    value is empty and the token is outside the literal. (3) The opening byte is
    that literal's close (`"key='"<t>`): same, one byte later. (4) A label's
    closing quote with no opening one (`key": '<t>`), read inside a string
    literal as an escaped quote opening an inner literal. Each is disclosed in
    the PR; every other shape must redact the token in every wrapper."""
    if opening and opening == opener:
        return True
    if opener and prefix.endswith(opener):
        return True
    if prefix[-1:] in _QUOTE_CHARS and opening == prefix[-1]:
        return True
    return sep == '": ' and not prefix.endswith('"')


_QUOTE_CHARS = ("'", '"')
#: The documents that hand the leaf to the scanner RAW: the YAML block scalar and
#: the base64 payload a stream decodes. The JSON wrappers write its line breaks
#: and tabs as escaped pairs instead.
_RAW_DOCUMENTS = ("yaml-block", "base64-in-json")


def _raw_break_after_a_quote_opener(opener: str, opening: str) -> bool:
    """The residual the leak oracle names for the RAW documents only: a quote
    opener followed by a raw line break (``key='`` then the token on the next
    line). A quoted value ends at its line in the base's grammar and in this
    scanner's, so the next line's token is not the pair's value to either, and
    the base's branches match nothing on it; the JSON wrappers write the same
    break escaped, inside the string literal, and there the token IS the value
    and must vanish."""
    return opener in _QUOTE_CHARS and opening in ("\n", "\r")


#: The literal two-byte spellings of escaped whitespace among the inner tokens.
_LITERAL_WHITESPACE_PAIRS = ("\\n", "\\t", "\\r")


def _literal_pair_is_the_enclosing_literals_whitespace(prefix: str, inner: str) -> bool:
    """The other residual named for the RAW documents only: when the key sits
    inside a ``"`` literal the prefix opened (``"key=<t>``, ``x="key=<t>``,
    ``{"key=<t>``), the two bytes ``\\n`` inside the value are that literal's
    escaped line break (``\\t`` its tab), read as one token in the literal's
    encoding, and whitespace ends an unquoted value: the bytes after it are the
    next line's, to this scanner as to a reader of the decoded string. The value's
    head is still claimed; only the tail is not asserted. In a bare line or a
    ``'`` literal the same two bytes are the value's own, and in the JSON wrappers
    they arrive as an escaped backslash and a letter, bytes of the value, so the
    tail is asserted everywhere else."""
    return inner in _LITERAL_WHITESPACE_PAIRS and prefix.endswith('"')


@pytest.mark.parametrize("opening", _LEAK_OPENINGS, ids=repr)
def test_no_generated_value_survives_redaction_in_any_wrapper(opening: str) -> None:
    """The LEAK oracle: a document that stays valid can still carry the secret if
    the claim was empty, which the document oracle cannot see. Every value here is
    a random token opening on *opening*, alone and carrying one inner token (an
    escaped line break, tab or backslash as the JSON wrappers write it) at its
    middle; after redaction the token's bytes before any inner whitespace appear
    in no wrapper's output, encoded or decoded, a token carrying no whitespace
    appears in none whole or by its tail (a stop inside the value leaves the
    remainder standing), and the output is a fixed point. Whitespace inside an
    unquoted value ends it in the base's grammar and in this scanner's (the bytes
    after a tab are the next word), so the remainder is not asserted. The leaf is
    read in every pair shape of the sweep's product (prefix, separator, opener,
    closer, suffix)."""
    import base64

    from kiro_crew.security import redact_credentials

    tokens = _random_tokens(len(_LEAK_KEYS))
    for key, token in zip(_LEAK_KEYS, tokens, strict=True):
        encoded = base64.b64encode(token.encode()).decode()
        head = token[: len(token) // 2]
        tail = token[len(token) // 2 :]
        for prefix in _PAIR_PREFIXES:
            for sep in _PAIR_SEPARATORS:
                for opener in _PAIR_OPENERS:
                    if _token_stands_outside_the_pair(prefix, sep, opener, opening):
                        continue
                    for closer in _PAIR_CLOSERS:
                        for suffix in _PAIR_SUFFIXES:
                            for inner, value in zip(
                                _LEAK_INNER_TOKENS, _leak_values(opening, token), strict=True
                            ):
                                leaf = _pair_leaf(prefix, key, sep, opener, value, closer, suffix)
                                for name, text in _leak_documents(leaf):
                                    out, _warnings = redact_credentials(text)
                                    if name in _RAW_DOCUMENTS and _raw_break_after_a_quote_opener(
                                        opener, opening
                                    ):
                                        _assert_a_fixed_point(out, redact_credentials)
                                        continue
                                    if name != "json-in-string":
                                        # Two levels deep the scanner reads one: a
                                        # value quoted at depth two is not an opener
                                        # to it (the document stays intact; the
                                        # residual is disclosed in the PR).
                                        assert head not in out, (leaf, text, out)
                                        if not inner.isspace():
                                            assert token not in out, (leaf, text, out)
                                            assert encoded not in out, (leaf, text, out)
                                            if not (
                                                name in _RAW_DOCUMENTS
                                                and _literal_pair_is_the_enclosing_literals_whitespace(
                                                    prefix, inner
                                                )
                                            ):
                                                assert tail not in out, (leaf, text, out)
                                    _assert_a_fixed_point(out, redact_credentials)


def _random_chunkings(text: str, rng, count: int) -> list[list[int]]:
    """*count* chunkings of *text* at random boundaries (two to five cuts each),
    plus the cut right after every base64 run's first byte and right before its
    last, so a chunk ends mid-encoding."""
    cuts: list[list[int]] = []
    n = len(text)
    if n < 3:
        return [[]]
    for _ in range(count):
        k = rng.randint(2, 5)
        cuts.append(sorted(rng.sample(range(1, n), min(k, n - 1))))
    for match in re.finditer(r"[A-Za-z0-9+/]{40,}={0,2}", text):
        cuts.append([match.start() + 1, match.end() - 1])
    return cuts


def _stream_chunks(text: str, cuts: list[int]) -> list[str]:
    from kiro_crew.security import StreamRedactor

    redactor = StreamRedactor()
    chunks = []
    last = 0
    for cut in cuts + [len(text)]:
        chunks.append(redactor.feed(text[last:cut]))
        last = cut
    chunks.append(redactor.flush())
    return chunks


@pytest.mark.parametrize("opening", _LEAK_OPENINGS, ids=repr)
def test_the_stream_equals_the_batch_on_randomly_chunked_documents(opening: str) -> None:
    """The STREAM/BATCH oracle: the same documents as the leak oracle, fed to the
    stream in chunks cut at random boundaries (mid-key, mid-quote, mid-escape,
    mid-base64), derandomized. The concatenated stream output equals the batch
    output, and no committed chunk carries the token, encoded or decoded."""
    import base64
    import random

    from kiro_crew.security import redact_credentials

    rng = random.Random(_LEAK_SEED + len(opening) + sum(map(ord, opening)))
    tokens = _random_tokens(len(_LEAK_KEYS))
    for key, token in zip(_LEAK_KEYS, tokens, strict=True):
        encoded = base64.b64encode(token.encode()).decode()
        head = token[: len(token) // 2]
        for prefix in _PAIR_PREFIXES[::2]:
            for sep in _PAIR_SEPARATORS:
                for opener in _PAIR_OPENERS:
                    if _token_stands_outside_the_pair(prefix, sep, opener, opening):
                        continue
                    for closer in _PAIR_CLOSERS:
                        # The token (alone and carrying each inner token), and
                        # the tag alone: a tag-filled value whose quote never
                        # closes is live, and a decoded chunk judged under the
                        # raw stream's carried quote state read it as closed and
                        # exempt, so the encoded chunk passed.
                        for value in _leak_values(opening, token) + [TAG]:
                            leaf = _pair_leaf(prefix, key, sep, opener, value, closer, "")
                            for name, text in _leak_documents(leaf):
                                if name == "json-in-string":
                                    continue
                                raw_residual = name in _RAW_DOCUMENTS and (
                                    _raw_break_after_a_quote_opener(opener, opening)
                                )
                                expected, _warnings = redact_credentials(text)
                                for cuts in _random_chunkings(text, rng, 2):
                                    chunks = _stream_chunks(text, cuts)
                                    assert "".join(chunks) == expected, (leaf, cuts, chunks)
                                    assert raw_residual or all(
                                        head not in c and encoded not in c for c in chunks
                                    ), (
                                        leaf,
                                        cuts,
                                    )
