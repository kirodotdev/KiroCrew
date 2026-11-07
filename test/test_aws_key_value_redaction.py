"""An AWS key-value credential keeps its key: only the value is redacted.

Pass 1 keeps the label of an AWS key-value match (key name, separator,
optional quote) in place and replaces only the value after it, so a JSON or
config document holding an AWS credential still parses after redaction.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.security import redact_credentials
from kiro_crew.security.redaction import redact_path_segments

TAG = "[REDACTED: credential]"
# Built at runtime so no literal key shape sits in the source.
SECRET = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"
KEYS = (
    "aws_secret_access_key",
    "SecretAccessKey",
    "aws_session_token",
    "SessionToken",
    "aws_access_key_id",
    "AccessKeyId",
)


@pytest.mark.parametrize("separators", [(", ", ": "), (",", ":")], ids=["spaced", "compact"])
@pytest.mark.parametrize("key", KEYS)
def test_a_json_pair_keeps_its_key_and_the_document_parses(
    key: str, separators: tuple[str, str]
) -> None:
    text = json.dumps({key: SECRET, "region": "x"}, separators=separators)
    result, warnings = redact_credentials(text)
    assert json.loads(result) == {key: TAG, "region": "x"}
    assert len(warnings) == 1


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("form", ['{{"{key}": "{value}"}}', "{key}={value}", "{key}: '{value}' x"])
def test_redacting_the_output_again_changes_nothing(key: str, form: str) -> None:
    once, _ = redact_credentials(form.format(key=key, value=SECRET))
    assert redact_credentials(once) == (once, [])


@pytest.mark.parametrize("key", KEYS)
def test_a_tag_with_more_bytes_glued_after_it_shields_nothing(key: str) -> None:
    # A value no other pass recognises, so only the AWS branch can redact it.
    result, _ = redact_credentials(f"{key}={TAG.replace(' ', '')}hunter2xyz")
    assert "hunter2xyz" not in result


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("follow", ["~cdc949b2723a.txt", "\\nregion=x", "\\t", "\\u00e9"])
def test_a_tag_stays_put_whatever_follows_it(key: str, follow: str) -> None:
    # The value class stops at the tag's space, so the bytes after the tag are not
    # part of the value and must not decide whether the tag is redacted again.
    text = f"{key}={TAG}{follow}"
    assert redact_credentials(text) == (text, [])


def test_a_json_string_holding_a_tag_and_an_escape_is_a_fixed_point() -> None:
    text = json.dumps({"c": f"aws_secret_access_key={TAG}\nregion=x"})
    assert redact_credentials(text) == (text, [])


def test_two_aws_labelled_sibling_paths_stay_distinct() -> None:
    first = redact_path_segments("models/SecretAccessKey=alpha.txt")
    second = redact_path_segments("models/SecretAccessKey=beta.txt")
    assert "alpha" not in first and "beta" not in second
    assert first != second
