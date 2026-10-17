"""Streamed AWS key-value pairs whose value is longer than the stream floor.

The batch AWS key-value branches redact a value only while its key label is in
the same ``redact()`` call. The stream redactor holds a label and its value
together up to ``_STREAM_HOLDBACK_JWT_MAX`` and fails closed past it.
"""

from __future__ import annotations

import random

import pytest

from kiro_crew.security import (
    _CRED_CLASS,
    _STREAM_HOLDBACK_JWT_MAX,
    REDACTED_CREDENTIAL_TAG,
    StreamRedactor,
    redact,
    stream_aws_anchor,
)


def _natural_cut(buf: str) -> int:
    cut = len(buf)
    while cut > 0 and buf[cut - 1] in _CRED_CLASS:
        cut -= 1
    return cut


def _stream(text: str, chunk: int) -> str:
    redactor = StreamRedactor()
    out = "".join(redactor.feed(text[i : i + chunk]) for i in range(0, len(text), chunk))
    return out + redactor.flush()


@pytest.mark.parametrize(
    "template",
    [
        "aws_secret_access_key={v}\nnext",
        'aws_session_token = "{v}"\nnext',
        '{{"SessionToken": "{v}"}}\nnext',
        "SecretAccessKey: {v}\nnext",
        "AccessKeyId={v} next",
    ],
)
@pytest.mark.parametrize("length", [600, 3000, 5000])
@pytest.mark.parametrize("chunk", [1, 7, 64, 600])
def test_aws_keyed_value_past_the_floor_never_streams_raw(
    template: str, length: int, chunk: int
) -> None:
    """The value has no 40-character base64 run, so only the key-value branch can
    redact it. Up to the ceiling it is redacted whole; past it the stream fails
    closed. Either way no piece of the value reaches the output."""
    pieces = [f"v{i:09d}k-" for i in range(length // 12)]
    value = "".join(pieces)
    out = _stream("hello " + template.format(v=value), chunk)
    assert [p for p in pieces if p in out] == []
    assert out.startswith("hello ")
    assert out.rstrip().endswith("next")


@pytest.mark.parametrize("chunk", [1, 7, 64])
def test_aws_keyed_value_under_the_ceiling_streams_as_the_batch_pass_writes_it(
    chunk: int,
) -> None:
    value = "".join(f"v{i:09d}k-" for i in range(100))
    text = 'cfg {"SessionToken": "' + value + '", "region": "x"} end'
    assert _stream(text, chunk) == redact(text)


def test_oversized_aws_keyed_continuation_is_discarded() -> None:
    redactor = StreamRedactor()
    head = "aws_secret_access_key=" + "S" * (_STREAM_HOLDBACK_JWT_MAX + 500)
    continuation = "tail_u7-Qp9_" * 24
    joined = redactor.feed(head) + redactor.feed(continuation + " done") + redactor.flush()
    assert all(
        continuation[start : start + 32] not in joined for start in range(len(continuation) - 31)
    )
    assert joined.count(REDACTED_CREDENTIAL_TAG) == 1
    assert joined.endswith(" done")


def test_aws_keyed_discard_stops_at_the_value_terminator() -> None:
    redactor = StreamRedactor()
    head = "SecretAccessKey: " + "S" * (_STREAM_HOLDBACK_JWT_MAX + 500)
    assert redactor.feed(head).endswith(REDACTED_CREDENTIAL_TAG)
    assert redactor.discarding
    assert redactor.feed("SecretTail, done") + redactor.flush() == ", done"


@pytest.mark.parametrize(
    "label",
    ["SessionToken:", "SessionToken: ", 'SessionToken"', "SessionToken", "aws_session_token ="],
)
def test_a_label_padded_past_the_ceiling_still_hides_its_quoted_value(label: str) -> None:
    """The discard walks the rest of the label grammar before value bytes: the
    opening quote that follows the padding cannot end it."""
    redactor = StreamRedactor()
    value = "tail_u7-Qp9_" * 90
    out = redactor.feed(label + " " * (_STREAM_HOLDBACK_JWT_MAX + 100))
    if not label.rstrip().endswith((":", "=")):
        out += redactor.feed(": ")
    out += redactor.feed('"' + value + '" done') + redactor.flush()
    assert value[:24] not in out
    assert out.endswith(" done")


def test_a_nested_token_parameter_does_not_end_the_outer_aws_value() -> None:
    redactor = StreamRedactor()
    head = "SessionToken=" + "v0-" * 10 + "?token=" + "v1-" * 1400
    tail = "tail_u7-Qp9_" * 90
    out = redactor.feed(head) + redactor.feed("&" + tail + " done") + redactor.flush()
    assert tail[:24] not in out
    assert out.endswith(" done")


def test_a_closed_value_past_the_ceiling_keeps_its_suffix() -> None:
    value = "".join(f"v{i:09d}k-" for i in range(400))
    out = _stream('SessionToken: "' + value + '"abc, done', 7)
    assert value[:24] not in out
    assert out.endswith(", done")


def test_a_padded_key_with_no_separator_is_released_unchanged() -> None:
    text = (
        "SessionToken"
        + " " * (_STREAM_HOLDBACK_JWT_MAX + 100)
        + "is a field name, not a credential"
    )
    redactor = StreamRedactor()
    out = redactor.feed(text[: len(text) - 33]) + redactor.feed(text[-33:]) + redactor.flush()
    assert out == text


def test_a_repeated_label_run_takes_few_anchored_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The anchor is found without rescanning the value once per label."""
    calls: list[int] = []
    real = stream_aws_anchor.anchor_re(_CRED_CLASS)

    class _Counting:
        def match(self, buf: str, pos: int = 0) -> object:
            calls.append(pos)
            return real.match(buf, pos)

    monkeypatch.setattr(stream_aws_anchor, "anchor_re", lambda _cred: _Counting())
    buf = "SessionToken=x" * 8500 + " "
    stream_aws_anchor.find(buf, _CRED_CLASS, _natural_cut(buf))
    assert len(calls) <= 4


@pytest.mark.parametrize("seed", range(40))
def test_the_linear_anchor_finds_what_the_regex_search_finds(seed: int) -> None:
    rng = random.Random(seed)
    parts = ["SessionToken", "aws_session_token", "AccessKeyId", ":", "=", " ", '"', "'"]
    parts += [",", "}", "x", "ab-", "\n", "?token=", "&"]
    for _ in range(200):
        buf = "".join(rng.choice(parts) for _ in range(rng.randint(0, 14)))
        want = stream_aws_anchor.anchor_re(_CRED_CLASS).search(buf)
        got = stream_aws_anchor.find(buf, _CRED_CLASS, _natural_cut(buf))
        assert (want and want.span()) == (got and got.span()), repr(buf)


def test_a_key_mentioned_in_prose_is_released() -> None:
    text = "set aws_session_token before you run it, then retry"
    assert _stream(text, 1) == text
