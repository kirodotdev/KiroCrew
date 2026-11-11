"""With the structured detectors on, streamed output equals the batch pass's output.

``StreamRedactor`` holds back each unit the detectors have not decided yet
(``redaction_structured.compat.stream_read``) and reads each committed window
after the raw text before it, so however the text is cut into chunks the stream
writes what one batch call over the whole text writes. Every credential here is
synthetic and assembled at run time.
"""

from __future__ import annotations

import pytest
from redaction_structured_cases import (
    B64,
    aws_key_id,
    aws_secret,
    github_classic,
    github_fine_grained,
    jwt,
    synth,
)

from kiro_crew.security import StreamRedactor, redact
from kiro_crew.security.redaction_structured import compat


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
    "oauth url param": (
        "go https://auth.example.test/cb?state=abc&client_" + "secret=" + synth(32, seed=12)
    )
    + "&x=1 done",
    "arn": "role arn:aws:iam::123456789012:role/" + synth(20, seed=13) + " ok",
    "git object name": "commit " + synth(40, "0123456789abcdef", 14) + " merged",
    "prose": "Nothing secret here, just words: on a line.\nAnd another one.",
}


@pytest.fixture(autouse=True)
def _table_on():
    before = compat.use_rule_table()
    compat.publish_use_rule_table(True)
    yield
    compat.publish_use_rule_table(before)


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


def test_off_holds_nothing_for_the_detectors() -> None:
    compat.publish_use_rule_table(False)
    assert compat.stream_read("", "token " + github_classic()) is None


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
