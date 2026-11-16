"""The credential plan is the rule table: only a credential's VALUE is masked.

Pins what the plan reads (the rule table, the few fixed branches whose classes no row
reads, the token-parameter pass) and what it leaves as written (identifiers, public
material, near-miss tokens, whole URLs, code). Synthetic values only.
"""

from __future__ import annotations

import base64
import json
import string

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from redaction_structured_cases import benign_cases, secret_cases

from kiro_crew import security
from kiro_crew.acp import _frame_record
from kiro_crew.security import redaction, stream_aws_anchor, text_redaction
from kiro_crew.security.redaction_structured import compat
from kiro_crew.security.redaction_structured.table import TABLE
from kiro_crew.testing.synthetic_credentials import ALNUM, aws_secret, github_classic, synth

TAG = security.REDACTED_CREDENTIAL_TAG


def _streamed(text: str, size: int) -> str:
    sr = security.StreamRedactor()
    out = "".join(sr.feed(text[i : i + size]) for i in range(0, len(text), size))
    return out + sr.flush()


_BENIGN = [(name, text) for name, text in benign_cases()]


@pytest.mark.parametrize(("name", "text"), _BENIGN, ids=[n for n, _ in _BENIGN])
def test_every_benign_case_stays_as_written_on_every_batch_path(name, text):
    assert security.redact(text) == text
    assert security.redact_credentials(text)[0] == text
    assert _streamed(text, 7) == text


#: The plan writes its own tag for an encoded credential; the table's cases write one tag.
_ENCODED_TAG = "[REDACTED: encoded credential]"
_SECRETS = [
    (
        name,
        text,
        expected.replace(TAG, _ENCODED_TAG) if rules == ["encoded_credential"] else expected,
    )
    for name, text, expected, rules in secret_cases()
]


@pytest.mark.parametrize(("name", "text", "expected"), _SECRETS, ids=[s[0] for s in _SECRETS])
def test_every_secret_case_is_masked_as_the_table_masks_it(name, text, expected):
    """The plan's batch and stream output equal the rule table's own answer."""
    assert security.redact_credentials(text)[0] == expected
    assert _streamed(text, 7) == expected


def test_only_the_value_is_masked_and_the_label_stays():
    secret = aws_secret()
    assert security.redact(f"aws_secret_access_key = {secret}") == f"aws_secret_access_key = {TAG}"
    value = synth(16, ALNUM, 31)
    assert security.redact(f"DB_PASSWORD={value}") == f"DB_PASSWORD={TAG}"


def test_an_npm_token_and_a_bearer_token_mask_the_value():
    value = synth(36, ALNUM, 32)
    assert security.redact(f"token npm_{value} end") == f"token {TAG} end"
    bearer = synth(40, ALNUM, 33)
    header = f"Authorization: Bearer {bearer}"
    assert security.redact(header) == f"Authorization: Bearer {TAG}"


def test_a_documented_placeholder_bearer_stays():
    for text in ("Authorization: Bearer YOUR_TOKEN", "Authorization: Bearer $TOKEN"):
        assert security.redact(text) == text


def test_records_name_the_rule_and_carry_no_label():
    token = github_classic(seed=34)
    _out, _warnings, records = redaction.redact_credentials_with_records(f"x {token} y")
    assert [(r.rule, r.label, r.value) for r in records] == [("github_token", "", token)]


def test_an_encoded_credential_takes_the_encoded_tag():
    import base64

    blob = base64.b64encode(f"token={github_classic(seed=35)}".encode()).decode()
    out, warnings = security.redact_credentials(f"cfg {blob} end")
    assert out == "cfg [REDACTED: encoded credential] end"
    assert warnings and "base64-encoded" in warnings[0]


def _percent_escaped(value: str) -> str:
    """Every byte of *value* written as a ``%XX`` escape."""
    return "".join(f"%{byte:02X}" for byte in value.encode())


_URL_TOKEN = synth(32, ALNUM, 79)
#: ``(plain text, secret)`` for each format row that reads a class the fixed branches
#: once read (Discord, npm, OpenAI project, PyPI, Telegram), and for the ``token`` URL
#: parameter row. Built from pieces, so the source holds no credential.
_AFTER_TABLE_CREDENTIALS = {
    name: (value, value)
    for name, value in {
        "discord": "M"
        + synth(25, ALNUM, 74)
        + "."
        + synth(6, ALNUM, 75)
        + "."
        + synth(27, ALNUM, 76),
        "npm": "npm_" + synth(36, ALNUM, 71),
        "openai_project": "sk-proj-" + synth(48, ALNUM, 78),
        "pypi": "pypi-" + synth(60, ALNUM, 77),
        "telegram": synth(9, string.digits, 72) + ":" + synth(35, ALNUM + "_-", 73),
    }.items()
} | {"token_parameter": (f"https://hooks.example.test/run?token={_URL_TOKEN}", _URL_TOKEN)}
_ENCODINGS = {
    "base64": lambda value: base64.b64encode(value.encode()).decode(),
    "percent": _percent_escaped,
}


@pytest.mark.parametrize("encoding", sorted(_ENCODINGS))
@pytest.mark.parametrize("kind", sorted(_AFTER_TABLE_CREDENTIALS))
def test_an_encoded_credential_no_table_row_reads_is_masked_as_its_plain_form(kind, encoding):
    """With redaction on, a decoded run is read by every pass the plain text is read by,
    on the batch path and the stream, and the masked text is a fixed point."""
    plain, secret = _AFTER_TABLE_CREDENTIALS[kind]
    assert secret not in security.redact_credentials(f"x {plain} y")[0]
    text = f"cfg {_ENCODINGS[encoding](plain)} end"
    out = security.redact_credentials(text)[0]
    assert out == f"cfg {_ENCODED_TAG} end"
    assert security.redact_credentials(out)[0] == out
    assert _streamed(text, 7) == out


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.text(max_size=2000))
def test_the_plan_never_raises_and_its_spans_are_sorted_and_disjoint(text):
    spans, _warnings, rules = redaction._credential_redaction_plan(text)
    for (a, b, _t), (c, _d, _u) in zip(spans, spans[1:]):
        assert a < b <= c
    assert set(rules) <= {s for s, _e, _t in spans}


def test_a_detector_failure_replaces_the_whole_text(monkeypatch):
    def boom(*_a, **_k):
        raise ValueError("detectors broke")

    counted: list[int] = []
    monkeypatch.setattr(compat, "_read", boom)
    monkeypatch.setattr(compat, "_count_fail_closed", lambda: counted.append(1))
    out, warnings = security.redact_credentials("anything at all")
    assert out == redaction.REDACTION_UNAVAILABLE_TAG
    assert warnings == ["Redaction unavailable: whole text replaced"]
    assert counted == [1]


def test_the_stream_aws_anchor_reads_the_table_aws_label_keys():
    """The stream's AWS label hold names exactly the keys of the table's AWS label rows."""
    rows = [r for r in TABLE.known_keys if r.id.startswith("aws.")]
    assert sorted(sorted(r.keys) for r in rows) == sorted(
        sorted(names) for names in stream_aws_anchor.KEY_NAME_ALTERNATIONS
    )


@pytest.mark.parametrize("size", [1, 3, 7, 40])
def test_a_labelled_value_split_across_chunks_streams_as_the_batch(size):
    text = f"export DB_PASSWORD={synth(20, ALNUM, 36)}\nnext line"
    assert _streamed(text, size) == security.redact(text)


def test_the_cut_guard_moves_the_cut_out_of_every_batch_span(monkeypatch):
    """A cut snapped back to whitespace never lands inside a span the plan replaces."""
    import random

    rng = random.Random(6612)
    for _ in range(300):
        words = [rng.choice(["ab", "abc", "a"]) for _ in range(rng.randint(3, 30))]
        buf = rng.choice([" ", "  "]).join(words)
        spans = sorted(
            {
                (s, min(len(buf), s + rng.randint(1, 12)))
                for s in (rng.randrange(len(buf)) for _ in range(rng.randint(1, 8)))
            }
        )
        monkeypatch.setattr(text_redaction, "_batch_spans", lambda _b, spans=spans: spans)
        cut = rng.randint(1, len(buf))
        got = text_redaction._outside_batch_spans(buf, cut)
        assert not any(s < got < e for s, e in spans if s < cut)
        assert got <= cut


@pytest.fixture
def frames(monkeypatch):
    _frame_record._reset_for_tests()
    monkeypatch.delenv(_frame_record.ENV_RECORD_FRAMES, raising=False)


def test_a_field_name_key_under_a_credential_key_stays_legible(frames):
    out = json.loads(_frame_record.scrub_frame({"Authorization": {"value": "Basic dXNlcjpwYXNz"}}))
    assert out == {"Authorization": {"value": TAG}}


@pytest.mark.parametrize(
    ("name", "expected"),
    [("Authorization", True), ("proxy-authorization", True), ("authorizations", False)],
)
def test_names_authorization(name, expected):
    assert _frame_record._names_authorization(name) is expected


def test_an_encoded_credential_after_a_non_ascii_character_is_masked():
    import base64

    header = base64.b64encode(b"Authorization: Bearer opaque-7a9c2d4e6f8b1a3c5e7d9f0b").decode()
    out = security.redact_credentials("凭据：" + header)[0]
    assert header not in out and out.endswith(_ENCODED_TAG)


def test_an_encoded_credential_past_the_decode_budget_is_withheld():
    import base64

    from kiro_crew.security.redaction_structured import encoded

    header = base64.b64encode(b"Authorization: Bearer opaque-7a9c2d4e6f8b1a3c5e7d9f0b").decode()
    blob = base64.b64encode(b"x" * (encoded.DECODE_BUDGET + 1024)).decode()
    out = security.redact_credentials(f"{blob} {header}")[0]
    assert header not in out


def test_text_after_a_large_image_payload_is_not_withheld():
    import base64
    import hashlib

    image = base64.b64encode(
        b"".join(hashlib.sha256(str(i).encode()).digest() for i in range(9000))
    ).decode()
    sha = "4f2a1b9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a"
    text = f"data:image/png;base64,{image} commit {sha}"
    assert security.redact_credentials(text)[0] == text


def test_an_assignment_form_bearer_header_is_masked():
    out = security.redact_credentials("Authorization=Bearer opaque-7a9c2d4e6f8b1a3c5e7d")[0]
    assert out == f"Authorization=Bearer {TAG}"


def test_a_short_random_bearer_token_is_never_read_as_a_placeholder():
    """The placeholder rule (``checks.is_placeholder``) must not hide a real value: every
    random base62 token of 20 to 40 characters after Bearer is masked, in every form."""
    import random
    import string

    from kiro_crew.security.redaction_structured.checks import is_placeholder

    rng = random.Random(20261115)
    alphabet = string.ascii_letters + string.digits
    for _ in range(400):
        token = "".join(rng.choice(alphabet) for _ in range(rng.randint(20, 40)))
        assert not is_placeholder(token), token
        for text in (
            f"Authorization: Bearer {token}",
            f'{{"Authorization": "Bearer {token}"}}',
            f"curl -H 'Authorization: Bearer {token}' https://api.example.com/v1",
        ):
            out = security.redact_credentials(text)[0]
            assert token not in out, text
