"""The stream redactor's AWS key-value anchor and its multi-carrier discard.

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


#: The label tail every batch AWS rule ends with. A rule of another shape fails
#: ``test_every_aws_label_rule_has_the_shape_the_stream_anchor_reads``, not the
#: import, so a reshaped rule cannot stop the package from loading.
_LABEL_TAIL_RE = _KEY_QUOTE_RE + _SEPARATOR_RE


def _key_name(label: re.Pattern[str]) -> str:
    """The key name alternation of a batch AWS label rule."""
    return label.pattern.removesuffix(_LABEL_TAIL_RE)


_KEY_NAMES_RE = "(?:" + "|".join(_key_name(label) for label, _ in redaction._AWS_LABEL_RULES) + ")"


def _literal_names(alternation: str) -> tuple[str, ...]:
    """The plain key names of one ``(?:a|b)`` alternation, for a substring prefilter."""
    return tuple(alternation.removeprefix("(?:").removesuffix(")").split("|"))


#: Every match starts with one of these, so a buffer naming none of them has no
#: anchor. Substring checks keep the common no-AWS stream at C speed. That the
#: names are plain literals is pinned by the same shape test as the label tail.
_KEY_LITERALS = tuple(
    name for label, _ in redaction._AWS_LABEL_RULES for name in _literal_names(_key_name(label))
)


def names_a_key(buf: str) -> bool:
    """Whether *buf* contains any AWS key name, by substring check."""
    return any(name in buf for name in _KEY_LITERALS)


_KEY_NAME_TAIL_RE = re.compile(_KEY_NAMES_RE + r"\Z")
_LABEL_HEAD_RE = re.compile(_KEY_NAMES_RE + _KEY_QUOTE_RE + "[:=]")


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
    if not names_a_key(buf):
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


def has_separator(anchor: re.Match[str]) -> bool:
    """Whether a label has reached its separator, so a value follows it.

    Only such a label is STRONG enough to fail closed; a bare key name may still
    be prose."""
    return anchor.group("sep") is not None


#: A credential label whose value follows whitespace: an AWS key name or an
#: ``Authorization`` header name. The AWS value class covers ``:`` and ``=``, so
#: a discard would otherwise run through such a label glued to the dropped value.
_GLUED_LABEL_RE = re.compile(rf"(?P<aws>{_KEY_NAMES_RE})|(?i:authorization)")
_LABEL_SEPARATOR_RE = re.compile(r"[\"']?\s*[:=]")
# The batch pass reads an Authorization value only after whitespace (`: Bearer x`),
# so a value glued to its separator stays part of the dropped run.
_AUTH_SEPARATOR_RE = re.compile(r"[\"']?\s*[:=]\s")
#: Bytes a fully consumed discard keeps for the next pass: the longest label
#: name, so a label split across chunks is whole there. The carried bytes are
#: dropped with the rest unless a label starts inside them.
LABEL_CARRY = max(len("authorization"), *(len(name) for name in _KEY_LITERALS))


def label_backoff(buf: str, run: int, start: int = 0) -> int:
    """Where a drop of ``buf[start:run]`` must stop so a glued label survives.

    A name counts only once its separator is in *buf* (an AWS key name's
    ``["']?\\s*[:=]``, or ``Authorization``'s separator plus whitespace); a name
    at the end of a fully consumed chunk stays in the carried tail and is judged
    on the next pass, so a bare name never ends the drop.
    """
    for match in _GLUED_LABEL_RE.finditer(buf, start, run):
        sep = _LABEL_SEPARATOR_RE if match.group("aws") is not None else _AUTH_SEPARATOR_RE
        if sep.match(buf, match.end()):
            return match.start()
    return run


_SEPARATOR_WAIT = 8


def label_awaiting_separator(buf: str, run: int) -> int | None:
    """Start of an AWS key name ending at *run* whose separator may still come.

    True only when everything after the name is at most a few whitespace bytes
    reaching the buffer end; longer padding is the released padded-label case.
    """
    tail = buf[run:]
    if not tail or len(tail) > _SEPARATOR_WAIT or not tail.isspace():
        return None
    return _key_ending_at(buf, run)


def _run_end(pattern: re.Pattern[str], text: str, pos: int) -> int:
    """End of *pattern*'s match at *pos*; every discard pattern can match empty."""
    match = pattern.match(text, pos)
    assert match is not None
    return match.end()


def discard_run(
    kinds: tuple[str, ...], text: str, run_res: dict[str, re.Pattern[str]]
) -> tuple[int, tuple[str, ...]]:
    """The longest run any armed kind consumes, and the kinds still inside it.

    *run_res* maps each kind to the pattern of its value bytes. Nested carriers
    (a `?token=` inside an AWS value) each end at their own terminator; dropping
    until the LAST of them ends keeps the outer value hidden.
    """
    results = [(_run_end(run_res[kind], text, 0), kind) for kind in kinds]
    run = max(r for r, _kind in results)
    return run, tuple(k for r, k in results if r == run)
