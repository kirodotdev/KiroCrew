"""Credential records: one per placeholder, paired by ordinal, never persisting the value."""

from __future__ import annotations

from redaction_structured_cases import session_token

from kiro_crew.security.redaction import (
    redact_credentials,
    redact_credentials_with_records,
)
from kiro_crew.testing.synthetic_credentials import SAMPLE_AWS_SECRET

# Built at runtime so no literal key shape sits in the source.
_SECRET = SAMPLE_AWS_SECRET
_GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def test_text_and_warnings_match_plain_redactor() -> None:
    text = f"aws_secret_access_key = {_SECRET}\ntoken {_GH}\n"
    cleaned, warnings, _ = redact_credentials_with_records(text)
    assert (cleaned, warnings) == redact_credentials(text)


def test_label_rule_and_value_for_labelled_aws_secret() -> None:
    cleaned, _, matches = redact_credentials_with_records(f"aws_secret_access_key = {_SECRET}")
    assert len(matches) == 1
    m = matches[0]
    # With the structured detectors on, the label stays in the text and only
    # the value is redacted, so the record carries no label.
    assert (m.ordinal, m.rule, m.label, m.value) == (0, "aws_secret_access_key", "", _SECRET)
    assert cleaned.startswith("aws_secret_access_key = ")
    assert _SECRET not in cleaned


def test_session_token_label() -> None:
    # A session token value is a long base64 run; a short stub is not one.
    token = session_token()
    cleaned, _, matches = redact_credentials_with_records("aws_session_token=" + token)
    assert matches[0].rule == "aws_session_token"
    # The label stays in the text; only the value is redacted.
    assert matches[0].label == ""
    assert cleaned.startswith("aws_session_token=")
    assert token not in cleaned


def test_unlabelled_rule_ids() -> None:
    _, _, matches = redact_credentials_with_records(f"here {_GH} and {_SECRET} end")
    assert [m.rule for m in matches] == ["github_token", "bare_aws_secret"]
    assert all(m.label == "" for m in matches)


def test_ordinal_counts_tags_already_in_the_input() -> None:
    text = f"[REDACTED: credential] then {_GH}"
    cleaned, _, matches = redact_credentials_with_records(text)
    assert cleaned.count("[REDACTED: credential]") == 2
    assert [m.ordinal for m in matches] == [1]


def test_no_match_returns_no_records() -> None:
    assert redact_credentials_with_records("nothing here") == ("nothing here", [], [])
