"""The stream redactor's AWS key-value anchor and its label-grammar discard.

The batch AWS key-value branches in ``redaction._CREDENTIAL_PATTERNS``
(``aws_secret_access_key=``, ``"SessionToken": "`` and the rest) redact a value
only while its key label is in the same ``redact()`` call, and nothing else
catches a value with no long base64 run. ``StreamRedactor`` therefore treats a
trailing label as STRONG evidence: it holds the label and its value together up
to ``_STREAM_HOLDBACK_JWT_MAX``, then fails closed. The key list is read off
``redaction._AWS_LABEL_RULES`` so the two cannot drift apart.

The anchor matches from a key whose separator has not arrived yet (that
whitespace is outside the stream's credential class too) through a value
already closed by its quote while the run after the quote is still
credential-class (the natural cut would otherwise walk back over the quote into
the value and cut it from its label).
"""

from __future__ import annotations

import functools
import re

import kiro_crew.security.redaction as redaction

#: Value class of the batch AWS key-value branches: a value ends at whitespace or
#: a JSON structural delimiter.
VALUE_CLASS = r"[^\s\"',}]"

_SEPARATOR_RE = r"[:=]\s*[\"']?"
_KEY_QUOTE_RE = r"[\"']?\s*"
#: Longer than every key name; bounds the backward key search.
_KEY_NAME_MAX = 64


def _key_name(label: re.Pattern[str]) -> str:
    """The key name alternation of a batch AWS label rule."""
    tail = _KEY_QUOTE_RE + _SEPARATOR_RE
    if not label.pattern.endswith(tail):
        raise RuntimeError(f"AWS label rule changed shape: {label.pattern!r}")
    return label.pattern[: -len(tail)]


_KEY_NAMES_RE = "(?:" + "|".join(_key_name(label) for label, _ in redaction._AWS_LABEL_RULES) + ")"


def _literal_names(alternation: str) -> tuple[str, ...]:
    """The plain key names of one ``(?:a|b)`` alternation, for a substring prefilter."""
    names = tuple(alternation.removeprefix("(?:").removesuffix(")").split("|"))
    if not all(re.fullmatch(r"\w+", name) for name in names):
        raise RuntimeError(f"AWS key names are no longer literals: {alternation!r}")
    return names


#: Every match starts with one of these, so a buffer naming none of them has no
#: anchor. Substring checks keep the common no-AWS stream at C speed.
_KEY_LITERALS = tuple(
    name for label, _ in redaction._AWS_LABEL_RULES for name in _literal_names(_key_name(label))
)
_KEY_NAME_TAIL_RE = re.compile(_KEY_NAMES_RE + r"\Z")
_LABEL_HEAD_RE = re.compile(_KEY_NAMES_RE + _KEY_QUOTE_RE + "[:=]")

#: After a separator-free key was released past the ceiling: the rest of its label.
PENDING_LABEL_RE = re.compile(r"(\s*[\"']?\s*)([:=])?")


@functools.lru_cache(maxsize=4)
def anchor_re(cred_class: frozenset[str]) -> re.Pattern[str]:
    """The trailing label-and-value pattern for the stream's credential class."""
    cred = "[" + "".join(re.escape(c) for c in sorted(cred_class)) + "]"
    return re.compile(
        _KEY_NAMES_RE
        + _KEY_QUOTE_RE
        + rf"(?:(?P<sep>[:=])\s*(?P<open>[\"']?)(?P<val>{VALUE_CLASS}*)"
        + rf"(?P<close>[\"']{cred}*)?)?\Z"
    )


def _skip_back(buf: str, pos: int, chars: str, *, space: bool = False, once: bool = False) -> int:
    """Walk *pos* back over *chars* (and whitespace when *space*), at most once if *once*."""
    while pos > 0 and (buf[pos - 1] in chars or (space and buf[pos - 1].isspace())):
        pos -= 1
        if once:
            break
    return pos


def _key_ending_at(buf: str, end: int) -> int | None:
    """Start of an AWS key name that ends exactly at *end*, if one does."""
    key = _KEY_NAME_TAIL_RE.search(buf, max(0, end - _KEY_NAME_MAX), end)
    return None if key is None else key.start()


_VALUE_RUN_RE = re.compile(VALUE_CLASS + "*")


def find(buf: str, cred_class: frozenset[str], cut: int) -> re.Match[str] | None:
    """The leftmost ``anchor_re(cred_class)`` match in *buf*, in linear time.

    A plain ``search`` retries every key occurrence and rescans the value to the
    end each time: quadratic on a long run of repeated labels, which would stall
    the event loop that feeds the stream. Every match ends at the buffer end, so
    its value starts at or after ``low``: the start of the value run that ends at
    the first quote of the trailing credential-class run (a closed value), or at
    the buffer end. Only these labels can start a match: one whose separator
    ends at ``low`` or is the first byte of the run, the first label head at or
    after ``low``, and a separator-free key at the tail. Each is confirmed by one
    anchored match.

    *cut* is the stream's natural cut: the start of the trailing run of
    *cred_class* bytes, which the caller has already computed.
    """
    if not any(name in buf for name in _KEY_LITERALS):
        return None
    n = len(buf)
    low = min((p for p in (buf.find('"', cut), buf.find("'", cut)) if p != -1), default=n)
    # Walk back over value-class bytes in C: the run can be the whole held value.
    low -= _run_end(_VALUE_RUN_RE, buf[low - 1 :: -1] if low else "", 0)
    starts: list[int] = []
    sep = _skip_back(buf, _skip_back(buf, low, "\"'", once=True), "", space=True)
    for sep_end in (sep, low + 1):
        # The separator ends the label either before the value run (whitespace
        # or a quote between them) or as the run's first byte, which is a
        # value-class byte itself (`Key\n:v`).
        if 0 < sep_end <= n and buf[sep_end - 1] in ":=":
            key_end = _skip_back(
                buf, _skip_back(buf, sep_end - 1, "", space=True), "\"'", once=True
            )
            if (start := _key_ending_at(buf, key_end)) is not None:
                starts.append(start)
    bare_end = _skip_back(buf, _skip_back(buf, n, "", space=True), "\"'", once=True)
    if (start := _key_ending_at(buf, bare_end)) is not None:
        starts.append(start)
    if (head := _LABEL_HEAD_RE.search(buf, low)) is not None:
        starts.append(head.start())
    pattern = anchor_re(cred_class)
    for start in sorted(starts):
        if (match := pattern.match(buf, start)) is not None:
            return match
    return None


# A discard armed on an AWS label before its value starts walks the rest of the
# label grammar first: "aws-key" still expects `["']?\s*[:=]`, "aws-sep" still
# expects `\s*["']?`, and only then "aws-keyed" drops value bytes. Without these
# states a label padded past the ceiling would arm a value discard that its own
# opening quote ends, and the value after that quote would stream raw.
_KEY_REST_RE = re.compile(r"[\"']?\s*")
_SEP_REST_RE = re.compile(r"\s*")


def discard_state(anchor: re.Match[str]) -> str:
    """The discard kind for an AWS label whose material reaches the buffer end."""
    if anchor.group("sep") is None:
        return "aws-key"
    if not anchor.group("val") and not anchor.group("open"):
        return "aws-sep"
    return "aws-keyed"


def _run_end(pattern: re.Pattern[str], text: str, pos: int) -> int:
    """End of *pattern*'s match at *pos*; every discard pattern can match empty."""
    match = pattern.match(text, pos)
    assert match is not None
    return match.end()


def _discard_run_one(kind: str, text: str, run_res: dict[str, re.Pattern[str]]) -> tuple[int, str]:
    """How much of *text* one discard kind consumes, and its kind afterwards."""
    pos = 0
    if kind == "aws-key":
        pos = _run_end(_KEY_REST_RE, text, pos)
        if pos == len(text) or text[pos] not in ":=":
            return pos, kind
        pos, kind = pos + 1, "aws-sep"
    if kind == "aws-sep":
        pos = _run_end(_SEP_REST_RE, text, pos)
        if pos == len(text):
            return pos, kind
        pos, kind = pos + (text[pos] in "\"'"), "aws-keyed"
    return _run_end(run_res[kind], text, pos), kind


def discard_run(
    kinds: tuple[str, ...], text: str, run_res: dict[str, re.Pattern[str]]
) -> tuple[int, tuple[str, ...]]:
    """The longest run any armed kind consumes, and the kinds still inside it.

    *run_res* maps each value kind to the pattern of its value bytes. Nested
    carriers (a `?token=` inside an AWS value) each end at their own terminator;
    dropping until the LAST of them ends keeps the outer value hidden.
    """
    results = [_discard_run_one(kind, text, run_res) for kind in kinds]
    run = max(r for r, _kind in results)
    return run, tuple(dict.fromkeys(k for r, k in results if r == run))
