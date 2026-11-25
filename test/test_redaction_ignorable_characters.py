"""The credential plan reads through default-ignorable characters.

A zero-width or other default-ignorable character inside a credential renders as
nothing, so the text still reads as the credential. The plan must redact it the
same as the plain spelling, and replace the whole run, the invisible characters
included. Identifiers stay visible: an AWS key ID is never redacted, but the
40-character secret paired with it is, even when the ID is split.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.history import ConversationLog
from kiro_crew.security.redaction import (
    REDACTED_CREDENTIAL_TAG,
    redact_credentials,
    redact_credentials_and_spans,
    redact_credentials_with_records,
)
from kiro_crew.security.text_redaction import StreamRedactor
from kiro_crew.testing.synthetic_credentials import ALNUM, aws_secret, github_classic, synth

IGNORABLES = ["\u200b", "\u200c", "\u200d", "\ufeff", "\u2060", "\u00ad", "\u180e"]

KEY_ID = "AKIA" + synth(16, "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567", 3)
SECRET = aws_secret()
GITHUB = github_classic()
OPENAI = "sk-" + "proj-" + synth(40, ALNUM, 6)

#: (kind, token, length of its prefix)
TOKENS = [("aws_key_id", KEY_ID, 4), ("github", GITHUB, 4), ("openai", OPENAI, 3)]


def _split(token: str, at: int, char: str) -> str:
    return token[:at] + char + token[at:]


def _text(kind: str, token: str) -> str:
    if kind == "aws_key_id":
        return f"keys: {token} {SECRET} done"
    return f"token {token} done"


@pytest.mark.parametrize("char", IGNORABLES, ids=lambda c: f"U+{ord(c):04X}")
@pytest.mark.parametrize("where", ["after_prefix", "mid_token"])
@pytest.mark.parametrize(("kind", "token", "prefix"), TOKENS, ids=[t[0] for t in TOKENS])
def test_a_credential_split_by_an_ignorable_is_redacted(kind, token, prefix, where, char):
    at = prefix if where == "after_prefix" else len(token) // 2
    split = _split(token, at, char)
    out, warnings = redact_credentials(_text(kind, split))
    assert warnings
    if kind == "aws_key_id":
        # The key ID is an Identifier and stays as written; its paired secret goes.
        assert out == f"keys: {split} {REDACTED_CREDENTIAL_TAG} done"
    else:
        # The whole run is replaced, the invisible character with it.
        assert out == f"token {REDACTED_CREDENTIAL_TAG} done"


@pytest.mark.parametrize(("kind", "token", "prefix"), TOKENS, ids=[t[0] for t in TOKENS])
def test_the_plain_spelling_is_unchanged(kind, token, prefix):
    out, _ = redact_credentials(_text(kind, token))
    if kind == "aws_key_id":
        assert out == f"keys: {token} {REDACTED_CREDENTIAL_TAG} done"
        assert redact_credentials(f"id {token} only") == (f"id {token} only", [])
    else:
        assert out == f"token {REDACTED_CREDENTIAL_TAG} done"


@pytest.mark.parametrize(
    "text",
    [
        "soft\u00adhyphen and zero\u200bwidth, no secret here",
        "family \U0001f468\u200d\U0001f469\u200d\U0001f467 and \ufeffBOM",
        "\u200b\u200b\u200b",
    ],
)
def test_ignorables_without_a_credential_are_left_as_written(text):
    assert redact_credentials(text) == (text, [])
    assert redact_credentials_with_records(text) == (text, [], [])


def test_spans_and_records_line_up_with_the_original_text():
    split = _split(GITHUB, 4, "\u200b")
    text = f"a {split} b {GITHUB} c"
    clean, _w, records = redact_credentials_with_records(text)
    assert clean == f"a {REDACTED_CREDENTIAL_TAG} b {REDACTED_CREDENTIAL_TAG} c"
    assert [r.value for r in records] == [split, GITHUB]
    masked, _ = redact_credentials_and_spans(text, [(0, 1)], "[X]")
    assert masked == f"[X] {REDACTED_CREDENTIAL_TAG} b {REDACTED_CREDENTIAL_TAG} c"


def test_a_credential_joined_to_text_by_an_ignorable_stays_redacted():
    # Removing the character would glue the token to the word after it; the
    # plain-text reading still sees the token whole and keeps redacting it.
    out, _ = redact_credentials(f"x {GITHUB}\u200bmore y")
    assert out == f"x {REDACTED_CREDENTIAL_TAG}\u200bmore y"


@pytest.mark.parametrize("size", [1, 5, 64])
def test_a_split_credential_does_not_stream_out(size):
    split = _split(GITHUB, 4, "\u200b")
    text = f"before {split} after\n"
    stream = StreamRedactor()
    out = "".join(stream.feed(text[i : i + size]) for i in range(0, len(text), size))
    out += stream.flush()
    assert GITHUB[8:] not in out
    assert out == f"before {REDACTED_CREDENTIAL_TAG} after\n"


def test_a_split_credential_never_reaches_the_transcript_file(tmp_path):
    split = _split(GITHUB, 4, "\u200b")
    ConversationLog(base_dir=tmp_path).append("t1", "assistant", f"here {split} it is")
    raw = (tmp_path / "t1.jsonl").read_text(encoding="utf-8")
    assert GITHUB[8:] not in raw
    row = json.loads(raw.splitlines()[-1])
    assert row["content"] == f"here {REDACTED_CREDENTIAL_TAG} it is"


def test_a_failed_read_without_ignorables_replaces_the_whole_text(monkeypatch):
    from kiro_crew.security.redaction_structured import compat

    plain = compat.credential_plan

    def fail_on_the_view(text, *args):
        return plain(None if "\u200b" not in text else text, *args)

    monkeypatch.setattr(compat, "credential_plan", fail_on_the_view)
    text = f"\u200bab {_split(GITHUB, 4, chr(0x200B))} done\u200b"
    out, warnings = redact_credentials(text)
    assert out == "[REDACTED: redaction unavailable]"
    assert warnings == ["Redaction unavailable: whole text replaced"]
