"""Streamed output equals the batch pass's output.

``StreamRedactor`` holds back each unit the detectors have not decided yet
(``redaction_structured.compat.stream_read``) and reads each committed window
after the raw text before it, so however the text is cut into chunks the stream
writes what one batch call over the whole text writes. Every text the redaction
tests share is checked at fixed chunk sizes and at seeded random cuts. Every
credential here is synthetic and assembled at run time.
"""

from __future__ import annotations

import base64
import random
import urllib.parse

import pytest
from redaction_structured_cases import (
    B64,
    aws_key_id,
    aws_secret,
    benign_cases,
    github_classic,
    github_fine_grained,
    jwt,
    secret_cases,
    synth,
)
from redaction_verifier_corpus import AMBIGUOUS, BENIGN, LEAKS

from kiro_crew.security import StreamRedactor, redact
from kiro_crew.security.redaction_structured.encoded import TEXT_HOLD_MAX
from kiro_crew.testing.synthetic_credentials import ALNUM


def _pem(lines: int = 4) -> str:
    body = "\n".join(synth(64, B64, 10 + i) for i in range(lines))
    return "-----BEGIN RSA " + f"PRIVATE KEY-----\n{body}\n-----END RSA " + "PRIVATE KEY-----"


FIXTURES = {
    "github classic": "token " + github_classic() + " end",
    "github fine-grained": "use " + github_fine_grained() + "\nnext line",
    "jwt": "auth " + jwt() + " done",
    "pem block": "key:\n" + _pem() + "\nafter",
    "aws pair after the key id": (
        "id " + aws_key_id() + " and some words between them, then " + aws_secret() + " end"
    ),
    "aws pair before the key id": "s " + aws_secret(seed=7) + " for " + aws_key_id(seed=8) + " ok",
    "aws labelled secret": "aws_secret_access_key = " + aws_secret(seed=3) + " words",
    "aws labelled secret in json": '{"SecretAccessKey": "' + aws_secret(seed=9) + '"} next',
    "aws labelled session token": "aws_session_token=" + synth(360, B64, 15) + "\nnext",
    "aws labelled key id": "AccessKeyId: " + aws_key_id(seed=16) + " kept",
    "oauth url param": (
        "go https://auth.example.test/cb?state=abc&client_" + "secret=" + synth(32, seed=12)
    )
    + "&x=1 done",
    "arn": "role arn:aws:iam::123456789012:role/" + synth(20, seed=13) + " ok",
    "git object name": "commit " + synth(40, "0123456789abcdef", 14) + " merged",
    "prose": "Nothing secret here, just words: on a line.\nAnd another one.",
}


def _stream(chunks: list[str]) -> str:
    redactor = StreamRedactor()
    return "".join(redactor.feed(c) for c in chunks) + redactor.flush()


@pytest.mark.parametrize("name", sorted(FIXTURES))
@pytest.mark.parametrize("size", [1, 3, 7, 64, 600])
def test_fixed_chunks_equal_the_batch_pass(name: str, size: int) -> None:
    text = FIXTURES[name]
    chunks = [text[i : i + size] for i in range(0, len(text), size)]
    assert _stream(chunks) == redact(text)


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_every_two_chunk_cut_equals_the_batch_pass(name: str) -> None:
    text = FIXTURES[name]
    expected = redact(text)
    for cut in range(1, len(text)):
        assert _stream([text[:cut], text[cut:]]) == expected, cut


def test_an_unterminated_private_key_block_is_dropped_to_the_end() -> None:
    head = "key:\n-----BEGIN RSA " + "PRIVATE KEY-----\n"
    body = "\n".join(synth(64, B64, 20 + i) for i in range(400))
    redactor = StreamRedactor()
    out = "".join(redactor.feed(c) for c in (head, body[:9000], body[9000:]))
    out += redactor.flush()
    assert body[:64] not in out and body[-64:] not in out
    assert out.startswith("key:\n")


def test_an_anchorless_run_keeps_the_holdback_bound() -> None:
    # A long run of credential-class letters with no anchor holds no unit the
    # detectors are still deciding, so the holdback floor still bounds the hold.
    from kiro_crew.security.text_redaction import _STREAM_HOLDBACK_MAX

    piece = "abcdefghijklmnopqrstuvwxyz" * 9 + "abcdefghijklmnopqrstuv"
    assert len(piece) == 256
    redactor = StreamRedactor()
    emitted = 0
    for _ in range(40):
        emitted += len(redactor.feed(piece))
        assert len(redactor.held) <= _STREAM_HOLDBACK_MAX
    assert emitted == 40 * 256 - len(redactor.held)
    assert emitted + len(redactor.flush()) == 40 * 256


_SENTENCE = "The quick brown fox jumps over the lazy dog. "
_ENCODED_TAG = "[REDACTED: encoded credential]"
_TOKEN = synth(40, ALNUM, 33)


def _bearer_prose(sentences: int, end: str = "") -> str:
    """Prose that ends in an ``Authorization: Bearer`` header."""
    return _SENTENCE * sentences + "Authorization: Bearer " + _TOKEN + end


def _b64(plain: str) -> str:
    return base64.b64encode(plain.encode()).decode()


def _chunked(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


#: Runs far longer than the stream's 512-character floor whose decode ends in a
#: credential: the batch pass masks each run whole, so a stream commits none of it.
ENCODED = {
    "base64": _b64(_bearer_prose(60)),
    "base64 in prose": "payload " + _b64(_bearer_prose(60)) + " end",
    "url-safe base64": (
        "q " + base64.urlsafe_b64encode(_bearer_prose(60, "?>").encode()).decode() + " end"
    ),
    "percent-encoded": "q " + urllib.parse.quote(_bearer_prose(20), safe="") + " end",
    "base64 past the stream's hold": "q " + _b64(_bearer_prose(100)) + " end",
    "kubeconfig client key": "  user:\n    client-key-data: " + _b64(_pem(30) + "\n") + "\n",
}


@pytest.mark.parametrize("name", sorted(ENCODED))
def test_an_encoded_credential_longer_than_the_floor_streams_masked(name: str) -> None:
    """A run whose head is text is held until it ends (withheld past ``TEXT_HOLD_MAX``),
    so a cut past the floor commits none of it and the run is masked whole."""
    text = ENCODED[name]
    expected = redact(text)
    assert expected.count(_ENCODED_TAG) == 1 and len(expected) < 64
    for cut in (750, len(text) // 2, len(text) - 3):
        assert _stream([text[:cut], text[cut:]]) == expected, cut
    for size in (7, 40, 600):
        assert _stream(_chunked(text, size)) == expected, size


def test_the_url_safe_case_is_read_in_the_url_safe_alphabet() -> None:
    run = ENCODED["url-safe base64"].split()[1]
    assert "-" in run or "_" in run
    assert len(ENCODED["base64 past the stream's hold"]) > TEXT_HOLD_MAX


def test_a_text_run_past_the_hold_is_withheld_and_one_under_it_streams_as_written() -> None:
    """Past ``TEXT_HOLD_MAX`` a run whose head is text is replaced and its rest dropped,
    even when its decode holds no credential: the hold stays bounded and none of it
    is committed as written. A shorter one is kept, as the batch pass keeps it."""
    short = "q " + _b64(_SENTENCE * 60) + " end"
    assert len(short) < TEXT_HOLD_MAX and redact(short) == short
    assert _stream(_chunked(short, 40)) == short
    text = "q " + _b64(_SENTENCE * 100) + " end of the reply"
    redactor = StreamRedactor()
    out = ""
    for chunk in _chunked(text, 40):
        out += redactor.feed(chunk)
        assert len(redactor.held) <= TEXT_HOLD_MAX + 40
    out += redactor.flush()
    assert out == "q " + _ENCODED_TAG + " end of the reply"


def test_an_escape_after_a_committed_head_withholds_the_rest_of_the_run() -> None:
    """A percent run with no escape yet is released at the floor like any word. When an
    escape arrives after its head is committed, the batch pass decodes the head with
    it, so the stream withholds the rest of the run: the credential never streams."""
    text = "q " + "word." * 120 + urllib.parse.quote(_bearer_prose(0), safe="") + " end"
    assert redact(text) == "q " + _ENCODED_TAG + " end"
    for size in (1, 7, 40, 600):
        out = _stream(_chunked(text, size))
        assert _TOKEN not in out and out.endswith(_ENCODED_TAG + " end"), size


#: Every text the redaction tests share (the rule table's secret and benign cases, the
#: independent verifier's corpus, the fixtures above), and one encoded credential.
CORPUS = (
    [(f"secret:{name}", text) for name, text, _expected, _rules in secret_cases()]
    + [(f"benign:{name}", text) for name, text in benign_cases()]
    + [(iid, text) for iid, _cat, text in BENIGN]
    + [(iid, text) for iid, _cat, text, _values in LEAKS]
    + [(iid, text) for iid, _cat, text, _note in AMBIGUOUS]
    + [(f"fixture:{name}", text) for name, text in sorted(FIXTURES.items())]
    + [("encoded:base64", ENCODED["base64"])]
)


@pytest.mark.parametrize(("name", "text"), CORPUS, ids=[name for name, _text in CORPUS])
def test_every_shared_text_streams_as_the_batch_pass_writes_it(name: str, text: str) -> None:
    """At chunk sizes 1, 7 and 40, and at seeded random cuts into two, three and four
    chunks, the joined stream output and its flush equal one batch call."""
    expected = redact(text)
    for size in (1, 7, 40):
        assert _stream(_chunked(text, size)) == expected, size
    rng = random.Random(name)
    for cuts in (1, 2, 3):
        if len(text) <= cuts:
            continue
        for _ in range(3):
            points = sorted(rng.sample(range(1, len(text)), cuts))
            bounds = [0, *points, len(text)]
            chunks = [text[a:b] for a, b in zip(bounds, bounds[1:])]
            assert _stream(chunks) == expected, points
