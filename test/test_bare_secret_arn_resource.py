"""An ARN's resource id survives the bare-secret pass; a key beside or posing as one does not."""

from __future__ import annotations

import pytest
from redaction_structured_cases import github_classic

from kiro_crew.security import redact_credentials
from kiro_crew.security import redaction as _redaction
from kiro_crew.testing.synthetic_credentials import SAMPLE_AWS_SECRET

# A synthetic 40-char AWS secret key (not the documentation example), never a live credential.
KEY = SAMPLE_AWS_SECRET
TAG = "[REDACTED: credential]"

# The issue's shape: a mixed-case, digit-bearing target-group name glued by `/`
# to its hex id, which is one run the heuristic judges a key on its own.
TARGET_GROUP_RESOURCE = "targetgroup/MyProdWebAppTargetGroupV2/73e2d6bc24d8a067"
TARGET_GROUP_ARN = "arn:aws:elasticloadbalancing:us-east-1:123456789012:" + TARGET_GROUP_RESOURCE


def test_the_fixtures_are_what_they_claim() -> None:
    assert len(KEY) == 40
    assert _redaction._looks_like_secret_key(KEY)
    assert _redaction._text_contains_bare_secret(TARGET_GROUP_RESOURCE)


@pytest.mark.parametrize(
    "template",
    [
        "{}",
        '"TargetGroupArn": "{}"',
        "--target-group-arn={}",
        "TargetGroupArn: {}",
        "<{}>",
        "({})",
    ],
)
def test_the_target_group_arn_is_left_intact_with_no_warning(template: str) -> None:
    text = template.format(TARGET_GROUP_ARN)
    assert redact_credentials(text) == (text, [])


@pytest.mark.parametrize(
    "arn",
    [
        "arn:aws-us-gov:elasticloadbalancing:us-gov-west-1:123456789012:" + TARGET_GROUP_RESOURCE,
        "arn:aws-cn:elasticloadbalancing:cn-north-1:123456789012:" + TARGET_GROUP_RESOURCE,
    ],
)
def test_other_partitions_are_exempt(arn: str) -> None:
    assert redact_credentials(arn) == (arn, [])


def test_labelled_and_fixed_shape_credentials_inside_an_arn_are_still_redacted() -> None:
    # A fixed-shape credential is found by its own shape, not the ARN's.
    secret = github_classic(seed=31)
    text = f"arn:aws:iam::123456789012:user/{secret}"
    cleaned, warnings = redact_credentials(text)
    assert secret not in cleaned
    assert warnings


def test_the_exemption_leaves_the_exfil_scan_unchanged() -> None:
    assert _redaction._text_contains_bare_secret(TARGET_GROUP_ARN)


# ── a streamed reply whose delta boundary splits the ARN prefix ──


def _flush_streamed(deltas: list[str]) -> str:
    """Stream *deltas* through the per-delta removers as the run loop does, then flush."""
    from kiro_crew.dashboard import chat_runner
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("arn-flush")
    text = ""
    for delta in deltas:
        shown, removed = chat_runner._redact_delta(delta)
        chat_runner._accumulate_segment_raw(slot, delta, shown, removed)
        text += shown
    return chat_runner._redact_segment(slot, text)[0]


@pytest.mark.parametrize("cut", [10, 30, 49])
def test_a_delta_split_target_group_arn_saves_the_reply_not_the_withheld_notice(cut: int) -> None:
    from kiro_crew.dashboard.chat_turn.segment_flush import _SEGMENT_UNCHECKED_TEXT

    # The later delta cannot see the `arn:` prefix and redacts the run, so the
    # save falls back to main's verdict instead of withholding the whole reply.
    text = f"Target: {TARGET_GROUP_ARN} done."
    saved = _flush_streamed([text[:cut], text[cut:]])
    assert saved != _SEGMENT_UNCHECKED_TEXT
    assert saved.startswith("Target: ") and saved.endswith(" done.")


def test_an_unsplit_streamed_target_group_arn_is_saved_whole() -> None:
    text = f"Target: {TARGET_GROUP_ARN} done."
    assert _flush_streamed([text]) == text
