"""The detectors behind the credential rule table.

Each function turns one rule into the spans of a text it claims. None of them
decides by a value's shape alone: a pattern rule needs its vendor prefix or
structural markers (and, for ``key_id_pair``, a key ID nearby), and the
``labelled`` and ``url_param`` detectors decide by the field or parameter NAME a
value sits under. Every regex here is bounded, so a scan is linear in the text.
"""

from __future__ import annotations

import bisect
import logging
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

from kiro_crew.security.redaction_rules.model import Detector, Hit
from kiro_crew.security.redaction_rules.rules import (
    MAX_VALUE_LEN,
    Rule,
    RuleTable,
    normalise_name,
)

logger = logging.getLogger(__name__)

#: Values shorter than this under a name-keyed detector are ignored: they are
#: flags, booleans and enum words far more often than secrets.
MIN_NAMED_VALUE_LEN = 4

_N = MAX_VALUE_LEN

_URL_RE = re.compile(
    rf"(?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]{{0,31}}://[^\s\"'<>`]{{1,{_N}}}"
)
#: The part of a URL the labelled detector leaves to the URL rules. It ends at
#: ``;``: in a connection string (``Endpoint=sb://host/;Key=...``,
#: ``jdbc:sqlserver://db;password=...``) the pairs after it are not part of the
#: URL. Query parameters are still read over the whole URL.
_URL_HEAD_RE = re.compile(
    rf"(?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]{{0,31}}://[^\s\"'<>`;]{{1,{_N}}}"
)
_QUERY_START_RE = re.compile(r"[?#]")
_PARAM_RE = re.compile(rf"[?&#](?P<name>[^=&#?\s]{{1,128}})=(?P<val>[^&#?\s\"'<>`)\]]{{0,{_N}}})")

_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9_.\-])(?P<dash>--?)?(?P<key>[A-Za-z_][A-Za-z0-9_.\-]{0,63})(?![A-Za-z0-9_.\-])"
)
#: ``key = value`` / ``key: value`` / ``"key": value`` / ``key => value``, with
#: an optional (escaped) closing quote on the key. ``::``, ``://`` and ``==``
#: are not separators.
_SEP_RE = re.compile(r"(?:\\?[\"'])?[ \t]{0,8}(?:=>|:(?![:/])|=(?!=))[ \t]{0,8}")
#: ``--flag value``: a CLI flag also takes its value after plain whitespace,
#: unless the next word is another flag.
_FLAG_SEP_RE = re.compile(r"[ \t]{1,8}(?=[^\s\-])")

_SCHEME_RE = re.compile(r"(?i:(bearer|basic|token|digest|aws4-hmac-sha256))[ \t]{1,8}")
#: Schemes whose credentials are a parameter list: only the named parameter is
#: secret, and the rest (an AWS key ID and credential scope, a Digest realm) is
#: kept.
_SCHEME_PARAM = {
    scheme: re.compile(
        rf"(?i)(?<![A-Za-z0-9_]){param}[ \t]{{0,8}}=[ \t]{{0,8}}\"?([^\s\",;]{{1,{_N}}})"
    )
    for scheme, param in (("digest", "response"), ("aws4-hmac-sha256", "signature"))
}
_QUOTED_RE = {q: re.compile(rf"{q}((?:[^{q}\\\n]|\\.){{0,{_N}}}){q}") for q in "\"'"}
_ESCAPED_QUOTED_RE = {q: re.compile(rf"\\{q}([^{q}\\\n]{{0,{_N}}})\\{q}") for q in "\"'"}
_UNQUOTED_RE = re.compile(rf"[^\s\"'`,;&)\]}}]{{1,{_N}}}")
_FLAG_VALUE_RE = re.compile(rf"[^\s\"'`]{{1,{_N}}}")

#: Value shapes: a line-oriented assignment, a CLI flag, or a pair nested in
#: other text.
LINE = "line"
FLAG = "flag"
NESTED = "nested"
#: What may precede a key that opens a line-oriented assignment: indentation, a
#: shell ``export``/``set``/``declare``, or a YAML list dash.
_LINE_LEAD_RE = re.compile(r"[ \t]{0,64}(?:(?:export|set|setx|declare)[ \t]{1,8}|-[ \t]{1,8})?")
_LEAD_MAX = 80
#: A template placeholder (``${VAR}``, ``{{ var }}``) is read whole, so the
#: table's allow entries see it as written instead of cut at its brace.
_TEMPLATE_RE = re.compile(r"\$\{[^{}\r\n]{1,200}\}|\{\{[^\r\n]{0,200}?\}\}")
_LINE_RE = re.compile(rf"[^\r\n]{{0,{_N}}}")
#: ``<password>value</password>``: the tag name is the key, the text up to the
#: matching close tag is the value.
_XML_ELEMENT_RE = re.compile(
    rf"<(?P<key>[A-Za-z_][A-Za-z0-9_.\-]{{0,63}})(?:[ \t][^<>\r\n]{{0,500}})?>"
    rf"(?P<val>[^<\r\n]{{1,{_N}}})</(?P=key)[ \t]{{0,8}}>"
)
#: A ``name``/``key`` field naming the label, as JSON, YAML or an XML
#: attribute; its ``value`` field must follow within the same object.
_NAME_FIELD_RE = re.compile(
    r"(?<![A-Za-z0-9_\-])[\"']?(?:name|key)[\"']?[ \t]{0,8}[:=][ \t]{0,8}"
    r"[\"']?(?P<name>[A-Za-z_][A-Za-z0-9_.\-]{0,63})[\"']?"
)
_VALUE_FIELD_RE = re.compile(
    r"[^{}<>\[\]]{0,200}?(?<![A-Za-z0-9_\-])[\"']?value[\"']?[ \t]{0,8}[:=][ \t]{0,8}"
)
#: A value opening one of these is a nested structure or an existing
#: placeholder, not a scalar to judge.
_NOT_A_VALUE_START = frozenset("\r\n[{(")


@dataclass(frozen=True)
class _Named:
    """A ``name -> value`` pair a name-keyed detector judges."""

    name: str
    start: int
    end: int


class _Spans:
    """Sorted, non-overlapping spans with an O(log n) containment test."""

    def __init__(self, spans: Sequence[tuple[int, int]]) -> None:
        self._starts = [s for s, _ in spans]
        self._ends = [e for _, e in spans]

    def contains(self, pos: int) -> bool:
        i = bisect.bisect_right(self._starts, pos) - 1
        return i >= 0 and pos < self._ends[i]


def _line_end(text: str, i: int) -> int:
    m = _LINE_RE.match(text, i)
    return m.end() if m else i


def _value_region(text: str, i: int) -> tuple[int, int, bool] | None:
    """Where a value starting at ``i`` may lie: ``(start, end, quoted)``.

    An unterminated quote runs to the end of its line, so a value cut off by a
    truncated log line is still judged.
    """
    if i >= len(text):
        return None
    if text[i] == "\\" and text[i + 1 : i + 2] in ('"', "'"):
        m = _ESCAPED_QUOTED_RE[text[i + 1]].match(text, i)
        if m:
            return m.start(1), m.end(1), True
        i += 1
        if i >= len(text):
            return None
    if text[i] in "\"'":
        m = _QUOTED_RE[text[i]].match(text, i)
        if m:
            return m.start(1), m.end(1), True
        return i + 1, _line_end(text, i + 1), True
    if text[i] in _NOT_A_VALUE_START:
        return None
    return i, _line_end(text, i), False


def labelled_value(text: str, i: int, shape: str = NESTED) -> tuple[int, int] | None:
    """The span of the value after a separator ending at ``i``, auth scheme stripped.

    ``shape`` decides where an unquoted value ends. A ``LINE`` assignment (env,
    INI, YAML, an HTTP header) runs to the end of its line, as those formats
    read it, so a password holding ``,`` ``;`` or a space is replaced whole. A
    ``FLAG`` value runs to the next blank. A ``NESTED`` pair (a cookie, an
    inline object, a pair inside another value) stops at its container's
    delimiters.
    """
    region = _value_region(text, i)
    if region is None:
        return None
    lo, hi, quoted = region
    start = lo
    scheme = _SCHEME_RE.match(text, lo, hi)
    if scheme:
        param = _SCHEME_PARAM.get(scheme.group(1).lower())
        if param is not None:
            m = param.search(text, scheme.end(), hi)
            return m.span(1) if m else None
        start = scheme.end()
        if start < hi and text[start] in _NOT_A_VALUE_START:
            return None
    if quoted or shape == LINE:
        end = len(text[start:hi].rstrip()) + start
    else:
        value_re = _FLAG_VALUE_RE if shape == FLAG else _UNQUOTED_RE
        m = _TEMPLATE_RE.match(text, start, hi) or value_re.match(text, start, hi)
        if m is None:
            return None
        end = m.end()
    return (start, end) if end - start >= MIN_NAMED_VALUE_LEN else None


def _key_shape(text: str, key_start: int, dash: bool) -> str:
    """How the pair whose key starts at ``key_start`` reads its value."""
    if dash:
        return FLAG
    line_start = text.rfind("\n", max(0, key_start - _LEAD_MAX), key_start) + 1
    if line_start == 0 and key_start > _LEAD_MAX:
        return NESTED
    return LINE if _LINE_LEAD_RE.fullmatch(text, line_start, key_start) else NESTED


def label_candidates(
    text: str, urls: _Spans, wanted: Callable[[str], bool] = lambda name: True
) -> list[_Named]:
    """Every ``key <sep> value`` pair outside a URL whose key ``wanted`` accepts.

    Covers ``key = value`` shapes, XML elements (``<password>...</password>``)
    and name/value pairs (``{"name": "password", "value": "..."}``,
    ``<add key="password" value="..."/>``). The key is judged before its value
    is read, so text full of unrelated pairs costs one key match per pair. The
    scan resumes right after each separator rather than after the value, so a
    pair inside another pair's value (``-H "X-Api-Key: ..."``,
    ``Set-Cookie: api_key=...``) is found too.
    """
    found: list[_Named] = []
    pos = 0
    while True:
        m = _KEY_RE.search(text, pos)
        if m is None:
            break
        sep = _SEP_RE.match(text, m.end())
        if sep is None and m.group("dash"):
            sep = _FLAG_SEP_RE.match(text, m.end())
        if sep is None:
            pos = m.end()
            continue
        pos = sep.end()
        name = normalise_name(m.group("key"))
        if not wanted(name) or urls.contains(m.start()):
            continue
        shape = _key_shape(text, m.start(), bool(m.group("dash")))
        span = labelled_value(text, sep.end(), shape)
        if span is not None:
            found.append(_Named(name, *span))
    found.extend(_xml_elements(text, urls, wanted))
    found.extend(_name_value_pairs(text, urls, wanted))
    return found


def _xml_elements(text: str, urls: _Spans, wanted: Callable[[str], bool]) -> Iterator[_Named]:
    for m in _XML_ELEMENT_RE.finditer(text):
        name = normalise_name(m.group("key"))
        if not wanted(name) or urls.contains(m.start()):
            continue
        start, end = m.span("val")
        raw = text[start:end]
        start += len(raw) - len(raw.lstrip())
        end -= len(raw) - len(raw.rstrip())
        if end - start >= MIN_NAMED_VALUE_LEN:
            yield _Named(name, start, end)


def _name_value_pairs(text: str, urls: _Spans, wanted: Callable[[str], bool]) -> Iterator[_Named]:
    for m in _NAME_FIELD_RE.finditer(text):
        name = normalise_name(m.group("name"))
        if not wanted(name) or urls.contains(m.start()):
            continue
        value = _VALUE_FIELD_RE.match(text, m.end())
        if value is None:
            continue
        span = labelled_value(text, value.end())
        if span is not None:
            yield _Named(name, *span)


def url_parameters(text: str, url_spans: Sequence[tuple[int, int]]) -> list[_Named]:
    """Every query and fragment parameter of every URL in ``text``.

    ``?`` and ``#`` inside a value also start a parameter, so a URL nested in a
    ``redirect_uri`` does not hide its own parameters.
    """
    found: list[_Named] = []
    for start, end in url_spans:
        q = _QUERY_START_RE.search(text, start, end)
        if q is None:
            continue
        for m in _PARAM_RE.finditer(text, q.start(), end):
            if m.end("val") - m.start("val") >= MIN_NAMED_VALUE_LEN:
                found.append(_Named(normalise_name(m.group("name")), *m.span("val")))
    return found


def _name_matches(name: str, labels: frozenset[str], exclude: frozenset[str], suffix: bool) -> bool:
    if not name or name in exclude:
        return False
    if suffix:
        if any(name.endswith(x) for x in exclude):
            return False
        return any(name.endswith(label) for label in labels)
    return name in labels


def _allowed(table: RuleTable, rule: Rule, value: str) -> bool:
    return table.allow.permits(value) or rule.allow.permits(value)


def _named_hits(
    table: RuleTable, rule: Rule, text: str, pairs: Sequence[_Named], suffix: bool
) -> Iterator[Hit]:
    for pair in pairs:
        if not _name_matches(pair.name, rule.labels, table.label_exclude, suffix):
            continue
        if _allowed(table, rule, text[pair.start : pair.end]):
            continue
        yield Hit(rule.id, rule.tier, rule.detector, pair.start, pair.end)


class _Anchors:
    """Pair-anchor spans, in text order, for the ``key_id_pair`` window test."""

    def __init__(self, spans: Sequence[tuple[int, int]]) -> None:
        self._spans = spans
        self._ends = [e for _, e in spans]

    def near(self, start: int, end: int, window: int) -> bool:
        """An anchor not overlapping ``[start, end)`` lies within ``window`` of it."""
        i = bisect.bisect_left(self._ends, start - window)
        while i < len(self._spans) and self._spans[i][0] <= end + window:
            a_start, a_end = self._spans[i]
            if a_end <= start or a_start >= end:
                return True
            i += 1
        return False


def _pattern_hits(table: RuleTable, rule: Rule, text: str) -> Iterator[Hit]:
    if rule.pattern is None:
        return
    anchors = None
    if rule.pair_with is not None:
        spans = [m.span() for m in rule.pair_with.pattern.finditer(text)]
        if not spans:
            return
        anchors = _Anchors(spans)
    has_value = "v" in rule.pattern.groupindex
    for m in rule.pattern.finditer(text):
        vs, ve = m.span("v") if has_value else m.span()
        if vs < 0 or ve <= vs:
            continue
        value = text[vs:ve]
        if rule.validate is not None and not rule.validate(value):
            continue
        if _allowed(table, rule, value):
            continue
        if (
            rule.pair_with is not None
            and anchors is not None
            and not anchors.near(vs, ve, rule.pair_with.window)
        ):
            continue
        start, end = (vs, ve) if rule.replace == "value" else m.span()
        yield Hit(rule.id, rule.tier, rule.detector, start, end)


def detect(table: RuleTable, text: str) -> list[Hit]:
    """Every hit every rule makes on ``text``, overlaps unresolved."""
    url_spans = [m.span() for m in _URL_RE.finditer(text)]
    labelled: list[_Named] | None = None
    params: list[_Named] | None = None
    found: list[Hit] = []
    label_rules = [r for r in table.rules if r.detector is Detector.LABELLED]
    seen: dict[str, bool] = {}

    def wanted(name: str) -> bool:
        if name not in seen:
            seen[name] = any(
                _name_matches(name, r.labels, table.label_exclude, suffix=True) for r in label_rules
            )
        return seen[name]

    for rule in table.rules:
        if rule.detector is Detector.LABELLED:
            if labelled is None:
                heads = [m.span() for m in _URL_HEAD_RE.finditer(text)]
                labelled = label_candidates(text, _Spans(heads), wanted)
            found.extend(_named_hits(table, rule, text, labelled, suffix=True))
        elif rule.detector is Detector.URL_PARAM:
            if params is None:
                params = url_parameters(text, url_spans)
            found.extend(_named_hits(table, rule, text, params, suffix=False))
        else:
            found.extend(_pattern_hits(table, rule, text))
    return found
