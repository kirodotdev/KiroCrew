"""Protection masks: only Identifier values, never a whole container, never over a Secret."""

from __future__ import annotations

import pathlib

import pytest
from redaction_structured_cases import B64, B64URL, HEX, aws_key_id, aws_secret, synth

import kiro_crew.security.redaction_structured as rs
from kiro_crew.security.redaction_structured import TableError, build_table, protect, redact


def _masked(text: str) -> list[tuple[str, str]]:
    _, _, masks = protect(text)
    return [(text[s:e], rule) for s, e, rule in masks]


def test_protect_text_equals_redact() -> None:
    text = f"key {aws_key_id()} secret {aws_secret()} and arn:aws:iam::123456789012:role/R"
    assert protect(text)[0] == redact(text)[0]


def test_a_url_path_is_not_masked() -> None:
    text = "see https://hooks.example.test/services/T000/B000/" + synth(24, B64URL, 1) + " now"
    assert _masked(text) == []


def test_a_url_userinfo_and_host_are_not_masked() -> None:
    assert _masked("https://user:pw@host.example.test/a/b?x=1") == []


def test_a_commit_permalink_masks_only_the_sha() -> None:
    sha = synth(40, HEX, 6)
    text = f"see https://github.com/org/repo/commit/{sha} for it"
    assert _masked(text) == [(sha, "commit.permalink")]


def test_a_blob_permalink_masks_only_the_sha_segment() -> None:
    sha = synth(40, HEX, 7)
    text = f"https://git.example.test/org/repo/blob/{sha}/src/a.py"
    assert _masked(text) == [(sha, "commit.permalink")]


def test_an_arn_is_not_masked() -> None:
    """An ARN is left to the later passes, which keep their own ARN handling."""
    assert _masked("role arn:aws:iam::123456789012:role/Deploy-Role here") == []


def test_only_an_image_data_uri_payload_is_masked() -> None:
    payload = synth(80, B64, 9)
    assert _masked(f"see data:image/png;base64,{payload} ok") == [(payload, "data_uri")]
    assert _masked(f"see data:text/plain;base64,{payload} ok") == []


def test_an_x_amz_id_2_value_is_masked() -> None:
    value = synth(76, B64, 2)
    assert _masked(f"x-amz-id-2: {value}\n") == [(value, "aws.x_amz_id_2")]


def test_oauth_public_parameters_are_reported_but_not_masked() -> None:
    """A ``state`` or ``code_challenge`` is free text a credential can be put in, so
    main's passes still read every OAuth parameter value."""
    state = synth(43, B64URL, 3)
    text = (
        "https://auth.example.test/authorize?client_id=abc123def456"
        f"&state={state}&code_challenge_method=S256&scope=openid"
    )
    _, resolved, masks = protect(text)
    assert any(h.rule_id == "oauth.public_param" for h in resolved)
    assert masks == []


def test_an_oauth_form_body_is_reported_but_not_masked() -> None:
    secret = synth(40, B64URL, 8)
    text = f"grant_type=authorization_code&client_id=app1&client_secret={secret}&code=c0de1"
    _, resolved, masks = protect(text)
    assert any(h.rule_id == "oauth.form_param" for h in resolved)
    assert masks == []


def test_a_placeholder_is_reported_but_not_masked() -> None:
    """Main's passes keep their own template exemption, which reads ``${``."""
    _, resolved, masks = protect("token=${API_TOKEN} next")
    assert any(h.rule_id == "placeholder" for h in resolved)
    assert masks == []


def test_a_public_value_holding_a_secret_is_never_masked() -> None:
    # The pairing Secret sits inside a public parameter: resolve rule 1 keeps the
    # parameter as an Identifier, but its span holds a Secret-tier match.
    text = f"{aws_key_id()} https://auth.example.test/cb?state={aws_secret()}&x=1"
    _, resolved, masks = protect(text)
    assert any(h.rule_id == "oauth.public_param" for h in resolved)
    assert [m for m in masks if m[2] == "oauth.public_param"] == []


def test_a_value_overlapping_a_replaced_secret_is_not_masked() -> None:
    secret = aws_secret()
    text = f"{aws_key_id()} x-amz-id-2: {secret}\n"
    out, _, masks = protect(text)
    assert secret not in out
    start = text.find(secret)
    assert all(e <= start or s >= start + len(secret) for s, e, _ in masks)


def test_masks_never_overlap_and_are_in_order() -> None:
    sha = synth(40, HEX, 1)
    text = (
        f"arn:aws:s3:::bucket https://github.com/o/r/tree/{sha} "
        f"{aws_key_id()} ${{{{ secrets.X }}}} x-amz-id-2: {synth(76, B64, 2)}"
    )
    _, _, masks = protect(text)
    assert masks == sorted(masks)
    assert all(a[1] <= b[0] for a, b in zip(masks, masks[1:], strict=False))


def _row(**extra: object) -> dict[str, object]:
    row: dict[str, object] = {"id": "x.y", "tier": "identifier", "detector": "structure",
                              "kind": "url_query", "params": ["state"], "max_length": 9}  # fmt: skip
    row.update(extra)
    return row


@pytest.mark.parametrize(
    "param", ["client_secret", "code", "access_token", "refresh_token", "id_token", "password"]
)
def test_loader_refuses_a_credential_parameter(param: str) -> None:
    with pytest.raises(TableError, match="credential parameter"):
        build_table([_row(params=["state", param])])


def test_loader_refuses_an_unknown_mask() -> None:
    with pytest.raises(TableError, match="unknown mask"):
        build_table([_row(mask="path")])


def test_loader_refuses_a_sha_segment_mask_outside_a_permalink() -> None:
    with pytest.raises(TableError, match="sha_segment"):
        build_table([_row(mask="sha_segment")])


def test_package_has_no_character_mix_check() -> None:
    # Owner ruling: no character-mix or character-class-presence heuristics.
    pkg = pathlib.Path(rs.__file__).parent
    for path in sorted(pkg.glob("*.py")):
        assert "upper_lower_digit" not in path.read_text(encoding="utf-8"), path.name


@pytest.mark.parametrize("media", ["svg+xml", "x-icon"])
def test_a_non_bitmap_image_data_uri_payload_is_still_judged(media):
    """Only a bitmap payload is kept; an SVG is a document a credential can sit in."""
    payload = synth(120, B64, 81)
    assert _masked(f'<image href="data:image/{media};base64,{payload}"/>') == []
