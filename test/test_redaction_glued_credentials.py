"""Two credentials written with nothing between them both redact whole.

A format row's boundary keeps a token from being read out of a longer word, so a
token glued to the next credential failed its boundary and was kept. A boundary
character that belongs to another credential does not block. Every value here is
synthetic.
"""

from __future__ import annotations

import random

import pytest
from redaction_structured_cases import (
    PEM_BEGIN,
    PEM_BODY,
    PEM_END,
    assert_linear_work,
    b64url_json,
    jwt,
    synth,
)

from kiro_crew.security import redact_credentials
from kiro_crew.security.redaction_structured import TAG, StreamRedactor, redact
from kiro_crew.testing.synthetic_credentials import ALNUM, github_classic

_SK = "sk-" + synth(20, ALNUM, 31) + "T3Blbk" + "FJ" + synth(20, ALNUM, 32)
_HF = "hf_" + synth(34, ALNUM, 33)
_HF2 = "hf_" + synth(34, ALNUM, 34)
_SLACK = "xoxb-1234567890-1234567890123-" + synth(24, ALNUM, 35)
_GLPAT = "glpat-" + synth(20, ALNUM, 36)
_GH = github_classic(seed=37)
_PEM = PEM_BEGIN + PEM_BODY + PEM_END

GLUED = [
    pytest.param([_HF, _HF2], id="same-rule"),
    pytest.param([_SK, jwt()], id="key-then-jwt"),
    pytest.param([_GH, _SK], id="checksum-then-key"),
    pytest.param([_SK, _GH], id="key-then-checksum"),
    pytest.param([_GH, _SLACK], id="checksum-then-slack"),
    pytest.param([_GH, _GLPAT], id="checksum-then-gitlab"),
    pytest.param([_SK, _PEM], id="key-then-private-key"),
    pytest.param([_PEM, _SK], id="private-key-then-key"),
    pytest.param([_HF, _SLACK, _SK], id="three"),
]


def _kept(parts: list[str], out: str) -> list[str]:
    """Each part with any 10 characters of its secret still in *out*.

    A private key's BEGIN and END lines are kept by design; only its body is secret.
    """
    secrets = [PEM_BODY.strip() if p == _PEM else p for p in parts]
    return [p for p in secrets if any(p[i : i + 10] in out for i in range(3, len(p) - 10))]


@pytest.mark.parametrize("parts", GLUED)
def test_glued_credentials_all_redact(parts: list[str]) -> None:
    out, _ = redact_credentials("tokens: " + "".join(parts) + " end")
    assert out.startswith("tokens: ") and out.endswith(" end") and TAG in out
    assert _kept(parts, out) == []


@pytest.mark.parametrize("parts", GLUED)
def test_each_credential_alone_still_redacts(parts: list[str]) -> None:
    for part in parts:
        assert _kept([part], redact_credentials(part)[0]) == []


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("task" + _SK, id="word-then-key"),
        pytest.param(_SK + "abc", id="key-then-word"),
        pytest.param("x" + _HF + "y", id="word-key-word"),
    ],
)
def test_a_token_glued_to_a_plain_word_is_still_kept(text: str) -> None:
    assert redact_credentials(text)[0] == text


@pytest.mark.parametrize("parts", GLUED)
def test_stream_equals_whole_at_every_split(parts: list[str]) -> None:
    text = "tokens: " + "".join(parts) + " end"
    whole = redact(text)
    for cut in range(len(text) + 1):
        s = StreamRedactor()
        out = s.feed(text[:cut]) + s.feed(text[cut:]) + s.flush()
        assert (out, s.spans) == whole, f"split at {cut}"


def _long_jwt() -> str:
    head = b64url_json({"alg": "HS256", "typ": "JWT"})
    return f"{head}.{b64url_json({'sub': 'fixture', 'd': 'x' * 8900})}." + synth(43, ALNUM, 40)


@pytest.mark.parametrize(
    "parts",
    [
        pytest.param(["hf_" + "eyJ" * 8 + synth(10, ALNUM, 41), jwt()], id="tries-spent"),
        pytest.param(["hf_eyJ" + synth(31, ALNUM, 42), _long_jwt()], id="long-jwt-later"),
    ],
)
def test_a_glued_jwt_behind_many_eyj_starts_still_redacts(parts: list[str]) -> None:
    assert _kept([parts[1]], redact_credentials(parts[1])[0]) == []
    assert _kept(parts, redact_credentials("tokens: " + "".join(parts) + " end")[0]) == []


@pytest.mark.parametrize(
    "outer",
    [
        pytest.param(
            "glpat-" + synth(10, ALNUM, 50) + "hf_" + synth(34, ALNUM, 51) + synth(15, ALNUM, 52),
            id="glpat",
        ),
        pytest.param(
            "sk-proj-" + synth(10, ALNUM, 53) + "hf_" + synth(34, ALNUM, 54) + synth(15, ALNUM, 55),
            id="sk-proj",
        ),
    ],
)
def test_a_token_shape_nested_in_a_longer_key_keeps_the_key_whole(outer: str) -> None:
    out = redact_credentials("key: " + outer + " end")[0]
    assert out == f"key: {TAG} end" or (outer[-15:] not in out and _kept([outer], out) == [])


def test_the_stream_holds_no_oversized_glued_jose_run() -> None:
    from kiro_crew import security

    text = "x " + _HF + "eyJ" + synth(30, ALNUM, 56) + "....." + synth(40000, ALNUM, 57) + " end"
    stream = security.StreamRedactor()
    held = 0
    for i in range(0, len(text), 64):
        stream.feed(text[i : i + 64])
        held = max(held, len(stream._buf))
    stream.flush()
    # The hold stays within the longest JOSE token, as for a plain JWT run.
    assert held <= 17000


def test_a_long_glued_run_costs_linear_work() -> None:
    unit = _HF + _SK + jwt() + " "
    assert_linear_work(lambda n: (unit * (n // len(unit) + 1))[:n], redact)


def _streamed(stream: object, text: str, size: int) -> str:
    chunks = [text[i : i + size] for i in range(0, len(text), size)]
    return "".join(stream.feed(c) for c in chunks) + stream.flush()  # type: ignore[attr-defined]


@pytest.mark.parametrize("size", [1, 7, 64, 500])
def test_a_glued_chain_longer_than_the_look_behind_streams_redacted(size: int) -> None:
    """The chain's first token leaves the look-behind; every later one still redacts."""
    parts = ["hf_" + synth(34, ALNUM, 100 + k) for k in range(60)]
    text = "tokens: " + "".join(parts) + " end"
    assert _kept(parts, _streamed(StreamRedactor(), text, size)) == []


@pytest.mark.parametrize("size", [1, 7, 64, 500])
@pytest.mark.parametrize(
    "parts",
    [
        pytest.param(["hf_" + synth(34, ALNUM, 100 + k) for k in range(30)], id="hf-chain"),
        pytest.param([github_classic(seed=k) for k in range(20)], id="checksum-chain"),
        pytest.param(
            ["hf_" + synth(33, ALNUM, 100 + k) + "x" for k in range(30)]
            + ["123456789:" + synth(35, ALNUM, 43)],
            id="chain-then-telegram",
        ),
    ],
)
def test_the_chat_stream_redacts_a_glued_chain_past_its_hold(size: int, parts: list[str]) -> None:
    """Past the 512-character hold the chat stream drops the run, failing closed."""
    from kiro_crew import security

    text = "tokens: " + "".join(parts) + " end"
    out = _streamed(security.StreamRedactor(), text, size)
    assert _kept(parts, out) == []
    assert out.startswith("tokens: ") and out.endswith(" end")


@pytest.mark.parametrize("size", [1, 7, 64])
@pytest.mark.parametrize("label", ["Authorization:", "authorization:"])
def test_the_chat_stream_keeps_a_label_after_a_dropped_chain(size: int, label: str) -> None:
    from kiro_crew import security

    parts = ["hf_" + synth(34, ALNUM, 100 + k) for k in range(30)]
    token = synth(30, ALNUM, 300)
    text = "tokens: " + "".join(parts) + "/" + label + " " * 1500 + "Bearer " + token + " end"
    out = _streamed(security.StreamRedactor(), text, size)
    assert token not in out
    assert _kept(parts, out) == []


@pytest.mark.parametrize("size", [7, 64])
def test_the_chat_stream_keeps_the_text_after_a_dropped_chain(size: int) -> None:
    from kiro_crew import security

    parts = ["hf_" + synth(34, ALNUM, 100 + k) for k in range(14)]
    text = "tokens: " + "".join(parts) + " hello world " + _PEM + " bye"
    out = _streamed(security.StreamRedactor(), text, size)
    assert _kept(parts, out) == []
    assert " hello world " + PEM_BEGIN[:16] in out and out.endswith(" bye")
    assert PEM_BODY.strip()[:20] not in out


@pytest.mark.parametrize("size", [1, 7, 64])
def test_the_chat_stream_floor_never_cuts_through_a_token(size: int) -> None:
    """A long unspaced run of separated tokens: the floor cut lands between tokens."""
    from kiro_crew import security

    rng = random.Random(5)  # distinct bodies: synth's strides share substrings
    parts = ["hf_" + "".join(rng.choice(ALNUM) for _ in range(34)) for _ in range(15)]
    text = "tokens: " + ":".join(parts) + " end"
    out = _streamed(security.StreamRedactor(), text, size)
    assert [p for p in parts if p[3:20] in out] == []


def test_the_chat_stream_keeps_json_after_a_dropped_chain() -> None:
    from kiro_crew import security

    for count in (14, 30):
        parts = ["hf_" + synth(34, ALNUM, 100 + k) for k in range(count)]
        text = '{"t":"' + "".join(parts) + '","message":"keep-this"} end'
        for size in (7, 64):
            out = _streamed(security.StreamRedactor(), text, size)
            assert _kept(parts, out) == []
            assert out.startswith('{"t":"') and out.endswith('","message":"keep-this"} end')


def test_the_chat_stream_hold_stays_bounded_on_overlapping_glued_tokens() -> None:
    """Glued tokens the plan merges into one span still leave the hold."""
    from kiro_crew import security

    parts = ["glpat-" + synth(20, ALNUM, 200 + k) for k in range(400)]
    text = "tokens: " + "".join(parts) + " end"
    stream = security.StreamRedactor()
    out, held = "", 0
    for i in range(0, len(text), 64):
        out += stream.feed(text[i : i + 64])
        held = max(held, len(stream._buf))
    out += stream.flush()
    assert held <= 4096
    assert _kept(parts, out) == []


@pytest.mark.parametrize("size", [1, 7, 64, 500])
def test_the_chat_stream_redacts_a_glued_chain(size: int) -> None:
    """``security.StreamRedactor``; ten tokens stay inside its 512-character hold."""
    from kiro_crew import security

    parts = ["hf_" + synth(34, ALNUM, 100 + k) for k in range(10)]
    text = "tokens: " + "".join(parts) + " end"
    assert _kept(parts, _streamed(security.StreamRedactor(), text, size)) == []
