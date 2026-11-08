"""An ARN's resource id survives the bare-secret pass; a key beside or posing as one does not."""

from __future__ import annotations

import pytest

from kiro_crew.security import redact_credentials
from kiro_crew.security import redaction as _redaction

# The canonical AWS documentation example secret key, never a live credential.
KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
TAG = "[REDACTED: credential]"

# The issue's shape: a mixed-case, digit-bearing target-group name glued by `/`
# to its hex id, which is one run the heuristic judges a key on its own.
TARGET_GROUP_RESOURCE = "targetgroup/MyProdWebAppTargetGroupV2/73e2d6bc24d8a067"
TARGET_GROUP_ARN = "arn:aws:elasticloadbalancing:us-east-1:123456789012:" + TARGET_GROUP_RESOURCE


def test_the_fixtures_are_what_they_claim() -> None:
    assert len(KEY) == 40
    assert _redaction._looks_like_secret_key(KEY)
    assert _redaction._text_contains_bare_secret(TARGET_GROUP_RESOURCE)


def test_without_the_exemption_the_target_group_arn_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_redaction, "_arn_resource_spans", lambda text: [])
    cleaned, warnings = redact_credentials(TARGET_GROUP_ARN)
    assert TAG in cleaned
    assert warnings


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


@pytest.mark.parametrize(
    "text",
    [
        f"{TARGET_GROUP_ARN} {KEY}",
        f"{KEY} {TARGET_GROUP_ARN}",
        f'"{TARGET_GROUP_ARN}","{KEY}"',
        f"{TARGET_GROUP_ARN},{KEY}",
        f"{TARGET_GROUP_ARN}={KEY}",
    ],
)
def test_a_bare_key_on_the_same_line_is_still_redacted(text: str) -> None:
    cleaned, warnings = redact_credentials(text)
    assert KEY not in cleaned
    assert TARGET_GROUP_ARN in cleaned
    assert warnings == ["Redacted bare secret key (40 chars)"]


@pytest.mark.parametrize(
    "prefix",
    [
        "myarn:aws:elasticloadbalancing:us-east-1:123456789012:",
        "xarn:aws:s3:us-east-1:123456789012:",
        "arn:aws:elasticloadbalancing:us-east-1:12345678901:",
        "arn:aws:elasticloadbalancing:us-east-1:1234567890123:",
        "arn:aws:s3:::",
        "arn:notaws:elasticloadbalancing:us-east-1:123456789012:",
        "ARN:AWS:elasticloadbalancing:us-east-1:123456789012:",
        # A real ARN whose whole resource is one key: the run IS a key.
        "arn:aws:elasticloadbalancing:us-east-1:123456789012:",
    ],
)
def test_a_key_after_a_lookalike_prefix_is_still_redacted(prefix: str) -> None:
    cleaned, warnings = redact_credentials(prefix + KEY)
    assert KEY not in cleaned
    assert cleaned.endswith(TAG)
    assert warnings == ["Redacted bare secret key (40 chars)"]


def test_a_key_after_a_lookalike_prefix_beside_a_real_arn_is_still_redacted() -> None:
    text = f"{TARGET_GROUP_ARN} myarn:aws:elasticloadbalancing:us-east-1:123456789012:{KEY}"
    cleaned, _ = redact_credentials(text)
    assert KEY not in cleaned
    assert TARGET_GROUP_ARN in cleaned


@pytest.mark.parametrize(
    "text",
    [
        # Glued into the resource of a real ARN that is not a target group.
        f"arn:aws:iam::123456789012:user/{KEY}",
        f"arn:aws:secretsmanager:us-east-1:123456789012:secret/{KEY}",
        f"arn:aws:lambda:us-east-1:123456789012:function:fn/{KEY}",
        # Glued after a real target-group id: the shape is closed at the id.
        f"{TARGET_GROUP_ARN}/{KEY}",
        f"{TARGET_GROUP_ARN}{KEY}",
        # A target-group shape that breaks the service's rules.
        f"arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/{KEY}/73e2d6bc24d8a067",
        f"arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/x/{KEY}",
    ],
)
def test_a_key_glued_into_any_other_arn_resource_is_still_redacted(text: str) -> None:
    # Only the closed target-group shape is exempt, so every place a key can be
    # glued into an ARN is judged exactly as on main.
    cleaned, warnings = redact_credentials(text)
    assert KEY not in cleaned
    assert warnings


def test_the_exemption_matches_only_the_target_group_resource() -> None:
    assert _redaction._arn_resource_spans(f"x {TARGET_GROUP_ARN} y") == [
        (2 + TARGET_GROUP_ARN.index("targetgroup/"), 2 + len(TARGET_GROUP_ARN))
    ]
    for other in (
        "arn:aws:iam::123456789012:role/" + TARGET_GROUP_RESOURCE,
        "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/x/50dc6c495c0c9188",
        f"{TARGET_GROUP_ARN}0",
    ):
        assert _redaction._arn_resource_spans(other) == [], other


def test_labelled_and_fixed_shape_credentials_inside_an_arn_are_still_redacted() -> None:
    # Passes 1 and 2 do not consult the ARN shape.
    text = "arn:aws:iam::123456789012:user/AKIAIOSFODNN7EXAMPLE"
    cleaned, warnings = redact_credentials(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in cleaned
    assert warnings


def test_the_exemption_leaves_the_exfil_scan_unchanged() -> None:
    assert _redaction._text_contains_bare_secret(TARGET_GROUP_ARN)


def test_many_arns_are_scanned_once() -> None:
    calls = 0
    real = _redaction._arn_resource_spans

    def counting(text: str) -> list[tuple[int, int]]:
        nonlocal calls
        calls += 1
        return real(text)

    text = "\n".join([TARGET_GROUP_ARN] * 500)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_redaction, "_arn_resource_spans", counting)
        assert redact_credentials(text) == (text, [])
    assert calls == 1


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


def test_a_resource_holding_a_judged_value_is_judged_like_any_run() -> None:
    # The segment save passes the values its deltas removed; a target-group
    # resource holding one keeps that delta's verdict, and every other is kept.
    other = TARGET_GROUP_ARN.replace("MyProdWebAppTargetGroupV2", "OtherWebAppTargetGroupV3")
    text = f"{TARGET_GROUP_ARN} {other}"
    cleaned, _, _ = _redaction.redact_credentials_with_records(
        text, judged_sri_values=(TARGET_GROUP_RESOURCE,)
    )
    assert TARGET_GROUP_RESOURCE not in cleaned
    assert TAG in cleaned
    assert other in cleaned


def test_a_judged_value_shorter_than_a_key_never_lifts_the_exemption() -> None:
    cleaned, _, _ = _redaction.redact_credentials_with_records(
        TARGET_GROUP_ARN, judged_sri_values=("73e2d6bc24d8a067",)
    )
    assert cleaned == TARGET_GROUP_ARN
