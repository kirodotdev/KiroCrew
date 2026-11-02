"""AWS key-value credentials keep their label: only the value is redacted.

The three AWS key-value branches of the credential alternation match the key
name, separator, optional quote and value together. Pass 1 keeps the label in
place and replaces only the value, so a JSON or config document that holds an
AWS credential still parses after redaction, and the record a placeholder
carries names no label because the reader already sees it.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.security import redact_credentials
from kiro_crew.security.redaction import redact_credentials_with_records
from kiro_crew.security.text_redaction import StreamRedactor, redact

TAG = "[REDACTED: credential]"
ENCODED_TAG = "[REDACTED: encoded credential]"
SECRET = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"

KEYS = (
    ("aws_secret_access_key", "aws_secret_access_key"),
    ("SecretAccessKey", "aws_secret_access_key"),
    ("aws_session_token", "aws_session_token"),
    ("SessionToken", "aws_session_token"),
    ("aws_access_key_id", "aws_access_key_id"),
    ("AccessKeyId", "aws_access_key_id"),
)
KEY_NAMES = [key for key, _ in KEYS]


def _stream(text: str, chunk: int | None) -> str:
    redactor = StreamRedactor()
    if chunk is None:
        out = redactor.feed(text)
    else:
        out = "".join(redactor.feed(text[i : i + chunk]) for i in range(0, len(text), chunk))
    return out + redactor.flush()


@pytest.mark.parametrize("separators", [(", ", ": "), (",", ":")], ids=["spaced", "compact"])
@pytest.mark.parametrize("key", KEY_NAMES)
def test_a_json_pair_keeps_its_key_and_the_document_parses(
    key: str, separators: tuple[str, str]
) -> None:
    text = json.dumps({key: SECRET, "region": "x"}, separators=separators)
    result, warnings = redact_credentials(text)
    assert SECRET not in result
    assert json.loads(result) == {key: TAG, "region": "x"}
    assert len(warnings) == 1


@pytest.mark.parametrize("key", KEY_NAMES)
@pytest.mark.parametrize(
    ("form", "expected"),
    [
        ("{key}={value}", "{key}=" + TAG),
        ("{key} = {value}", "{key} = " + TAG),
        ("{key}: '{value}'", "{key}: '" + TAG + "'"),
    ],
    ids=["equals", "spaced-equals", "yaml-quoted"],
)
def test_a_config_line_keeps_its_label(key: str, form: str, expected: str) -> None:
    result, _ = redact_credentials(form.format(key=key, value=SECRET))
    assert result == expected.format(key=key)


def test_an_access_key_id_value_is_still_redacted_after_its_label() -> None:
    text = json.dumps({"AccessKeyId": "AKIA" + "IOSFODNN7EXAMPLE"})
    result, warnings = redact_credentials(text)
    assert json.loads(result) == {"AccessKeyId": TAG}
    assert len(warnings) == 1


@pytest.mark.parametrize("key", KEY_NAMES)
@pytest.mark.parametrize(
    "form",
    ['{{"{key}": "{value}", "region": "x"}}', "{key}={value}", "{key}: '{value}' tail"],
)
def test_redaction_is_a_fixed_point_on_its_own_output(key: str, form: str) -> None:
    once, warnings_once = redact_credentials(form.format(key=key, value=SECRET))
    twice, warnings_twice = redact_credentials(once)
    assert TAG in once
    assert twice == once
    assert warnings_twice == []
    assert len(warnings_once) == 1


@pytest.mark.parametrize(("key", "rule"), KEYS)
def test_the_record_names_the_rule_and_no_label(key: str, rule: str) -> None:
    text = json.dumps({key: SECRET})
    cleaned, _, records = redact_credentials_with_records(text)
    assert json.loads(cleaned) == {key: TAG}
    [record] = records
    assert record.rule == rule
    assert record.label == ""
    assert record.value == SECRET


@pytest.mark.parametrize(
    "text",
    [
        f"aws_secret_access_key={TAG}",
        f'{{"SessionToken": "{TAG}", "region": "x"}}',
        f"AccessKeyId={ENCODED_TAG} next",
        "SecretAccessKey:[REDACTED",
        "SecretAccessKey:[R",
        "SecretAccessKey:[REDACTED: cre",
        "SecretAccessKey:[REDACTED:credential]",
        "SecretAccessKey:[REDACTED:cred",
    ],
    ids=[
        "whole",
        "json",
        "encoded",
        "cut-head",
        "cut-short",
        "cut-after-space",
        "joined",
        "joined-cut",
    ],
)
def test_a_value_made_of_a_tags_bytes_is_left_alone(text: str) -> None:
    assert redact_credentials(text) == (text, [])


@pytest.mark.parametrize(
    "value",
    ["[REDACTED" + SECRET, "[REDACTED:" + SECRET, TAG.replace(" ", "") + SECRET],
    ids=["lookalike-head", "lookalike-colon", "joined-tag-then-secret"],
)
def test_a_value_that_only_starts_like_a_tag_is_redacted(value: str) -> None:
    result, warnings = redact_credentials(f"aws_secret_access_key={value} done")
    assert SECRET not in result
    assert result == f"aws_secret_access_key={TAG} done"
    assert len(warnings) == 1


def test_a_label_glued_to_a_bare_secret_run_hides_both_secrets() -> None:
    """A key glued to the front of the label forms one base64 run with the
    label's leading ``aws``; the bare-secret pass removes that run whole, and the
    value after the label is still removed by pass 1."""
    text = f"D{SECRET}ZGHUT8aws_secret_access_key={SECRET}"
    result, _ = redact_credentials(text)
    assert SECRET not in result
    assert result == f"{TAG}_secret_access_key={TAG}"


@pytest.mark.parametrize("chunk", [None, 1, 7, 64])
@pytest.mark.parametrize(
    "form",
    ['{{"{key}":"{value}","region":"x"}}', "{key}={value} done", "{key}={value}"],
    ids=["json", "line", "end"],
)
@pytest.mark.parametrize("key", ["aws_secret_access_key", "SessionToken", "AccessKeyId"])
def test_a_value_past_the_stream_ceiling_keeps_its_label_like_the_batch_pass(
    key: str, form: str, chunk: int | None
) -> None:
    """A value longer than the stream's ceiling fails closed to the same text the
    batch pass writes: the label stays and only the value is dropped."""
    value = "Ab1" * 1800
    text = form.format(key=key, value=value)
    out = _stream(text, chunk)
    assert out == redact(text)
    assert out.startswith(form.split("{value}")[0].format(key=key))
    assert "Ab1Ab1" not in out


def test_a_value_inside_the_stream_window_keeps_its_label() -> None:
    text = json.dumps({"aws_secret_access_key": SECRET, "region": "x"})
    for chunk in (None, 1, 5):
        assert json.loads(_stream(text, chunk)) == {"aws_secret_access_key": TAG, "region": "x"}


@pytest.mark.parametrize("chunk", [None, 1, 7, 64, 5400])
@pytest.mark.parametrize("key", ["SessionToken", "aws_secret_access_key", "AccessKeyId"])
def test_a_closed_value_past_the_stream_ceiling_keeps_its_closing_quote(
    key: str, chunk: int | None
) -> None:
    """A value past the ceiling whose closing quote ends one chunk fails closed to the
    batch text: label, opening quote, tag and closing quote, so the JSON parses."""
    head = '{"' + key + '":"' + "Ab1" * 1800 + '"'
    redactor = StreamRedactor()
    if chunk is None:
        out = redactor.feed(head)
    else:
        out = "".join(redactor.feed(head[i : i + chunk]) for i in range(0, len(head), chunk))
    out += redactor.feed("}") + redactor.flush()
    assert out == redact(head + "}")
    assert json.loads(out) == {key: TAG}
