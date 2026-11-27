"""The credential plan reads through default-ignorable characters.

A zero-width or other default-ignorable character inside a credential renders as
nothing, so the text still reads as the credential. The plan must redact it the
same as the plain spelling, and replace the whole run, the invisible characters
included. Identifiers stay visible: an AWS key ID is never redacted, but the
40-character secret paired with it is, even when the ID is split.
"""

from __future__ import annotations

import base64
import json
from urllib.parse import unquote

import pytest
from redaction_structured_cases import B64, PEM_BEGIN, PEM_BEGIN_PLAIN, PEM_END, PEM_END_PLAIN

from kiro_crew.history import ConversationLog
from kiro_crew.security.exfil import scan_exfiltration_urls
from kiro_crew.security.redaction import (
    REDACTED_CREDENTIAL_TAG,
    _contains_fixed_credential,
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


def _stream(text: str, size: int) -> tuple[str, int]:
    """The joined output of a stream fed *size* characters at a time, and its largest
    held buffer."""
    stream = StreamRedactor()
    out, peak = [], 0
    for i in range(0, len(text), size):
        out.append(stream.feed(text[i : i + size]))
        peak = max(peak, len(stream._buf))
    out.append(stream.flush())
    return "".join(out), peak


PAIRING_CHARS = ["\u200b", "\u00ad"]
PROSE = "\nand then some more prose follows here to keep the stream going for a while\n"


@pytest.mark.parametrize("size", [1, 4, 16, 64])
@pytest.mark.parametrize("between", ["", " and then its secret"], ids=["adjacent", "words_between"])
@pytest.mark.parametrize("char", PAIRING_CHARS, ids=lambda c: f"U+{ord(c):04X}")
def test_a_split_key_id_pairs_with_its_secret_on_the_stream(char, between, size):
    text = f"keys: {_split(KEY_ID, 4, char)}{between} {SECRET} done{PROSE}"
    out, _ = _stream(text, size)
    assert SECRET[:12] not in out
    assert SECRET[-12:] not in out
    assert out == redact_credentials(text)[0]


@pytest.mark.parametrize("size", [1, 4, 16, 64])
def test_a_split_secret_whose_split_key_id_follows_a_commit_id_does_not_stream(size):
    # As written, only the commit id reads as a unit and holds the stream there; the
    # secret before it is decided by the key ID after it, which that window lacks.
    secret, key_id = _split(SECRET, 20, "\u200b"), _split(KEY_ID, 4, "\u200b")
    commit = synth(40, "0123456789abcdef", 77)
    text = f"secret {secret} at commit {commit} key id {key_id} done{PROSE}"
    out, _ = _stream(text, size)
    assert SECRET[:12] not in out
    assert SECRET[-12:] not in out
    assert out == redact_credentials(text)[0]


def _pem_block(begin: str, lines: int, terminated: bool) -> tuple[str, list[str]]:
    rows = [f"M{k:05d}Q" + synth(56, B64, 900 + k) for k in range(lines)]
    tail = f"\n{PEM_END}\nafter text\n" if terminated else "\n"
    return f"key:\n{begin}\n" + "\n".join(rows) + tail, rows


def _split_begin() -> str:
    return _split(PEM_BEGIN, 16, "\u200b")


@pytest.mark.parametrize("size", [4, 16, 64])
def test_a_terminated_block_with_a_split_header_does_not_stream_its_body(size):
    text, rows = _pem_block(_split_begin(), 4, terminated=True)
    out, _ = _stream(text, size)
    assert [row for row in rows if row in out] == []
    assert "after text" in out


# Larger chunks than the terminated case: every feed re-reads a buffer held up to the
# block's bound, so small chunks cost seconds for either header without testing more.
@pytest.mark.parametrize("size", [64, 512])
def test_an_unterminated_block_with_a_split_header_is_dropped_like_a_plain_one(size):
    # Past the block's bound (16000 characters of body), so the stream decides it running.
    text, rows = _pem_block(_split_begin(), 300, terminated=False)
    out, peak = _stream(text, size)
    assert [row for row in rows if row in out] == []
    assert out == f"key:\n{_split_begin()}\n{REDACTED_CREDENTIAL_TAG}"
    _, plain_peak = _stream(_pem_block(PEM_BEGIN, 300, terminated=False)[0], size)
    assert peak <= plain_peak


#: A certificate after the key, as a key and certificate bundle carries one.
CERT = (
    "-----BEGIN " "CERTIFICATE-----\nMIIC" + synth(60, B64, 7) + "\n-----END " "CERTIFICATE-----\n"
)


# A body past the stream's 512-character floor, and chunks that carry the END line on
# into the next header: the floor must not cut a block only one reading of its header ends.
@pytest.mark.parametrize("size", [16, 24, 48])
@pytest.mark.parametrize("header", ["split", "plain"])
def test_a_split_header_key_before_another_header_does_not_stream_its_body(header, size):
    head, rows = _pem_block(_split_begin() if header == "split" else PEM_BEGIN, 10, False)
    text = f"{head}{PEM_END}\n{CERT}"
    out, _ = _stream(text, size)
    assert [row for row in rows if row in out] == []
    assert out == redact_credentials(text)[0]


# A block only one reading ends must not move the stream's key scan past a BEGIN
# whose body it has not committed yet.
@pytest.mark.parametrize("size", [1, 4, 16, 64])
@pytest.mark.parametrize("shape", ["decoy_around_the_key", "split_body_row", "between_two_decoys"])
def test_a_key_after_a_split_header_does_not_stream_its_body(shape, size):
    if shape == "decoy_around_the_key":
        decoy_begin, decoy_end = _split(PEM_BEGIN, 19, "\u200b"), _split(PEM_END, 17, "\u200b")
        head, rows = _pem_block(f"{decoy_begin}\n{PEM_BEGIN}", 3, terminated=False)
        text = f"{head}{decoy_end}\nfiller\n{PEM_END}\nafter\n"
    elif shape == "between_two_decoys":
        begin, end = _split(PEM_BEGIN_PLAIN, 13, "\u200b"), _split(PEM_END_PLAIN, 11, "\u200b")
        rows = [f"M{k:05d}Q" + synth(56, B64, 600 + k) for k in range(16)]
        a, b, c, d = ("\n".join(rows[i : i + 4]) for i in range(0, 16, 4))
        text = f"x\n{begin}\n{a}\n{PEM_BEGIN}\n{b}\n{end}\n{begin}\n{c}\n{PEM_END}\n{d}\n{end}\nafter\n"
    else:
        text, rows = _pem_block(_split(PEM_BEGIN, 18, "\u00ad"), 3, terminated=True)
        text = text.replace(rows[0], _split(rows[0], 40, "\u200b"))
    out, _ = _stream(text, size)
    assert [row for row in rows if row[8:40] in out] == []
    assert out == redact_credentials(text)[0]


# Two keys of one type, the first END line split: read as written they are one block,
# read without the character two. The live view still shows the whole reply.
@pytest.mark.parametrize("size", [1, 4, 16, 64])
def test_two_keys_whose_first_end_line_is_split_stream_like_the_saved_text(size):
    head, rows = _pem_block(PEM_BEGIN, 4, terminated=False)
    second = [f"M{k:05d}Q" + synth(56, B64, 950 + k) for k in range(4)]
    split_end = _split(PEM_END, 12, "\u200b")
    text = f"{head}{split_end}\n{PEM_BEGIN}\n" + "\n".join(second) + f"\n{PEM_END}\nafter text\n"
    out, _ = _stream(text, size)
    assert [row for row in rows + second if row[8:40] in out] == []
    assert out == redact_credentials(text)[0]


# Each block begins inside the one before, alternating readings. Held past the key
# bound, the chain is dropped from its first BEGIN, as a key with no END is.
def test_a_chain_of_overlapping_blocks_past_the_key_bound_is_dropped():
    begin, end = _split(PEM_BEGIN_PLAIN, 13, "\u200b"), _split(PEM_END_PLAIN, 11, "\u200b")
    rows = [f"M{k:05d}Q" + synth(56, B64, 700 + k) for k in range(160)]
    links = [
        f"{end}\n{begin}\n{a}\n{PEM_END}\n{PEM_BEGIN}\n{b}\n"
        for a, b in zip(rows[2::2], rows[3::2])
    ]
    head = f"x\n{begin}\n{rows[0]}\n{PEM_BEGIN}\n{rows[1]}\n"
    text = head + "".join(links) + f"{end}\n{PEM_END}\nafter\n"
    out, peak = _stream(text, 512)
    assert out == f"x\n{REDACTED_CREDENTIAL_TAG}"
    _, plain_peak = _stream(_pem_block(PEM_BEGIN, 300, terminated=False)[0], 512)
    assert peak <= plain_peak + len(links[0])


# A split-header key padded with ignorables: the view counts the key's bound without
# them, so the stream drops it once it holds more of them than the longest key block.
def test_a_split_header_key_padded_with_ignorables_is_dropped_within_a_key_bound():
    body = "\n".join(f"M{k:05d}Q" + synth(56, B64, 970 + k) for k in range(8))
    padded = "".join(c + "\u200b" * 80 for c in body)
    out, peak = _stream(f"key:\n{_split_begin()}\n{padded}\nafter text\n", 512)
    assert out == f"key:\n{REDACTED_CREDENTIAL_TAG}"
    _, plain_peak = _stream(_pem_block(PEM_BEGIN, 300, terminated=False)[0], 512)
    assert peak <= plain_peak + len(body) + 512


# A URL longer than any key, after an emoji joiner or with one in its scheme: the text as
# written holds it too, so it streams whole as the saved text keeps it.
@pytest.mark.parametrize(
    "lead", ["Family \U0001f468\u200d\U0001f469 link: https", "link: h\u200bttps"]
)
def test_a_long_url_near_an_ignorable_streams_like_the_saved_text(lead):
    query = "".join(f"w{i % 97}+" for i in range(4500))
    text = f"{lead}://example.com/search?q={query}\nand more words\n"
    out, _ = _stream(text, 512)
    assert out == redact_credentials(text)[0]
    assert out.endswith("\nand more words\n")


@pytest.mark.parametrize("size", [4, 16, 64])
def test_an_encoded_credential_split_inside_its_run_is_dropped_from_its_start(size):
    # Past the stream's 4 KiB run hold, split after the label and 38 of the secret's
    # 40 characters: read as written, the run only starts after the U+200B.
    label = "[default]\naws_secret_access_key = "
    run = base64.b64encode(f"{label}{SECRET}{PROSE * 60}".encode()).decode()
    text = "key: " + _split(run, len(label + SECRET[:38]) * 4 // 3, "\u200b") + PROSE
    out, _ = _stream(text, size)
    assert run[:16] not in out
    assert out == redact_credentials(text)[0]


#: 16 KB of base64 text, past the stream's 4 KiB hold, so the stream drops it.
LONG_RUN = base64.b64encode((PROSE * 210).encode()).decode()[:16000]


def test_a_long_run_split_near_its_start_is_held_no_longer_than_the_plain_run():
    # Off the run's 4-character grid, so read as written the rest is no text run.
    text = "data: " + _split(LONG_RUN, 41, "\u200b") + "\nafter the run\n"
    out, peak = _stream(text, 64)
    plain_out, plain_peak = _stream(text.replace("\u200b", ""), 64)
    assert peak <= plain_peak
    assert [i for i in range(49) if LONG_RUN[i : i + 16] in out] == []
    assert out == plain_out


# The U+200B arrives once the drop has started, off the run's 4-character grid, so read
# as written the rest of the run is no run the stream would drop again.
@pytest.mark.parametrize("size", [64, 500])
def test_an_ignorable_in_a_later_chunk_of_a_dropped_run_does_not_end_the_drop(size):
    text = "data: " + _split(LONG_RUN, 6001, "\u200b") + "\nafter the run\n"
    out, _ = _stream(text, size)
    rest = LONG_RUN[6001:]
    assert [i for i in range(0, len(rest) - 16, 16) if rest[i : i + 16] in out] == []
    assert out == _stream(text.replace("\u200b", ""), size)[0]


@pytest.mark.parametrize("escaped", [False, True], ids=["raw", "percent_escaped"])
@pytest.mark.parametrize("char", PAIRING_CHARS, ids=lambda c: f"U+{ord(c):04X}")
def test_a_split_token_in_a_link_is_a_fixed_credential(char, escaped):
    spelled = "".join(f"%{b:02X}" for b in char.encode()) if escaped else char
    url = "https://collector.example/c?d=" + _split(GITHUB, 4, spelled)
    assert scan_exfiltration_urls("see " + url)
    # The outbound check reads each percent-decoded layer of the link.
    assert _contains_fixed_credential(unquote(url))


@pytest.mark.parametrize("size", [1, 4, 16, 64])
def test_a_blank_free_split_token_still_streams_as_its_tag(size):
    out, _ = _stream(f"before {_split(GITHUB, 4, chr(0x200B))} after\n", size)
    assert out == f"before {REDACTED_CREDENTIAL_TAG} after\n"


ORDINARY = {
    "zwj_emoji": "family \U0001f468\u200d\U0001f469\u200d\U0001f467 photo, plus more words\n",
    "soft_hyphen": "a long Donau\u00addampf\u00adschiff\u00adfahrt word in prose, and the rest\n",
    "rtl_mark": "\u05e9\u05dc\u05d5\u05dd\u200f (shalom) is a word here\n",
    "arabic_mark": "\u0645\u0631\u062d\u0628\u0627\u061c means hello in prose\n",
    "bom": "\ufeffa file that starts with a byte order mark and goes on\n",
}


@pytest.mark.parametrize("size", [1, 5, 64])
@pytest.mark.parametrize("name", list(ORDINARY))
def test_ordinary_text_with_ignorables_streams_unchanged(name, size):
    text = ORDINARY[name]
    assert _stream(text, size)[0] == text


@pytest.mark.parametrize("name", list(ORDINARY))
def test_ordinary_words_with_ignorables_in_a_link_are_not_flagged(name):
    word = next(w for w in ORDINARY[name].split() if not w.isascii())
    assert scan_exfiltration_urls("see https://docs.example/p?q=" + word) == []
