"""A two-segment link token is redacted the same wherever it starts.

The two-segment alternative in the credential pattern has a left boundary that
keeps `eyJ` from matching inside a longer word (`keyJson.get`) or right after a
`.`. After a `.` it starts a match only when the token also carries a `-` or a
payload shaped as base64url of ASCII JSON, which every minted token has and no
identifier chain does, so a token there is redacted as at the start of a text
while a dotted member chain of the same lengths is kept.
"""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import pytest

from kiro_crew.security import redact_credentials

#: A neutral token of the two-segment shape: `eyJ` plus a 100-char payload in the
#: base64url alphabet, a `.`, and a 43-char signature. Built from repeated filler,
#: so it decodes to nothing meaningful.
_TOKEN = "eyJ" + "a-b_" * 25 + "." + "c" * 21 + "-" + "d" * 21
_PLACEHOLDER = "[REDACTED: credential]"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (_TOKEN, _PLACEHOLDER),
        ("prefix " + _TOKEN, "prefix " + _PLACEHOLDER),
        ("prefix." + _TOKEN, "prefix." + _PLACEHOLDER),
        ("see ." + _TOKEN, "see ." + _PLACEHOLDER),
    ],
    ids=["start", "after-space", "after-word-dot", "after-lone-dot"],
)
def test_link_token_is_redacted_wherever_it_starts(text, expected):
    result, warnings = redact_credentials(text)

    assert result == expected
    assert len(warnings) == 1


def test_link_token_after_a_sentence_dot_is_redacted():
    """A token that follows the end of a sentence with no space is removed whole."""
    result, _ = redact_credentials(f"Open the link.{_TOKEN} to continue")

    assert result == f"Open the link.{_PLACEHOLDER} to continue"


@pytest.mark.parametrize(
    "text",
    [
        "obj.eyJsonReader.readValueFromInputStream(x)",
        "config.eyJsonSerializerConfigurationFactoryBuilderRegistryProviderDelegating"
        "InterceptorFactoryAdapterHandler.deserializeFromStringValueUsing"
        "ConfiguredObjectMapperInstance(x)",
        "cache.eyJ2IjoxfQ.json",
    ],
    ids=["short-member", "long-member", "dotted-filename"],
)
def test_dotted_identifiers_after_a_dot_are_kept(text):
    """Short dotted members after a `.` are kept by the segment lengths."""
    result, warnings = redact_credentials(text)

    assert result == text
    assert warnings == []


#: A member named `eyJ` plus a long identifier, then a 43-char member: the
#: two-segment shape at full length, built only from identifier characters.
_LONG_MEMBER = (
    "eyJsonSerializerConfigurationFactoryBuilderRegistryProviderDelegating"
    "InterceptorFactoryAdapterHandlerImpl"
)
_MEMBER_43 = "deserializeFromStringValueUsingMapperConfig"


@pytest.mark.parametrize("before", ["config.", "self.cache.", "."])
@pytest.mark.parametrize("after", ["(x)", "", ".close()", " = None"])
def test_a_full_length_dotted_member_chain_is_kept(before, after):
    """A dotted chain whose members have the token's lengths is ordinary code."""
    assert len(_LONG_MEMBER) - 3 >= 96 and len(_MEMBER_43) == 43
    text = f"{before}{_LONG_MEMBER}.{_MEMBER_43}{after}"

    result, warnings = redact_credentials(text)

    assert result == text
    assert warnings == []


def test_minted_link_tokens_after_a_dot_are_redacted():
    """Every token the generator mints is removed after a `.`, whatever it holds."""
    from kiro_crew.dashboard.token_auth import generate_token

    for n in range(200):
        token = generate_token(f"user-{n}", 300, register_nonce=False)
        result, _ = redact_credentials(f"obj.{token}(x)")
        assert result == f"obj.{_PLACEHOLDER}(x)", token


def _fixed_dashless_token() -> str:
    """A fixed claim set in the generator's own encoding, signature without `-`."""
    claims = {
        "sub": "local-app",
        "exp": 1700000300.0,
        "session_exp": 1700086400.0,
        "iat": 1700000000.0,
        "nonce": "0123456789abcdef",
        "gen": 0,
    }
    raw = json.dumps(claims, separators=(",", ":")).encode()
    payload = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    token = f"{payload}.{'Ab1_' * 10}Abc"
    assert "-" not in token and len(payload) - 3 >= 96
    return token


def test_an_ascii_json_payload_without_a_dash_is_redacted_after_a_dot():
    """The payload shape alone starts a match after a `.`, with no `-` anywhere.

    The token is fixed, so the case does not depend on a random nonce or key.
    """
    token = _fixed_dashless_token()

    result, warnings = redact_credentials(f"obj.{token}(x)")

    assert result == f"obj.{_PLACEHOLDER}(x)"
    assert len(warnings) == 1


def _review_script_redact():
    """`redact` from the prepare-pr skill's stdlib copy of the pattern."""
    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "kiro_crew"
        / "builtin_skills"
        / "kirocrew-dev"
        / "kirocrew-prepare-pr"
        / "scripts"
        / "_review_contract.py"
    )
    spec = importlib.util.spec_from_file_location("_dot_boundary_review_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.redact


@pytest.mark.parametrize("extra", ["", "a", "ab", "abc"])
@pytest.mark.parametrize("after", ["(x)", "", ".close()"])
def test_the_review_script_copy_keeps_the_same_dotted_chains(extra, after):
    """The skill's copy keeps a dotted chain the backend keeps.

    That copy sits in a case-insensitive regex; the member lengths cover every
    remainder mod 4, which is what the payload-shape check is sensitive to.
    """
    redact = _review_script_redact()
    text = f"config.{_LONG_MEMBER}{extra}.{_MEMBER_43}{after}"

    assert redact_credentials(text) == (text, [])
    assert redact(text) == text


def test_the_review_script_copy_redacts_a_token_after_a_dot():
    """The skill's copy still removes a fixed token after a `.`."""
    redact = _review_script_redact()

    assert redact(f"obj.{_fixed_dashless_token()}(x)") == "obj.[REDACTED](x)"


@pytest.mark.timeout(60)
@pytest.mark.parametrize("filler", ["a", "o", "a-", "aaao"])
def test_long_runs_after_a_dot_scan_in_linear_time(filler):
    """A long run after `.eyJ` that never completes a token is scanned once.

    Sized so a scan that re-walks the run from each position cannot finish
    inside the timeout.
    """
    text = (".eyJ" + filler * (400_000 // len(filler)) + ".x ") * 2

    result, warnings = redact_credentials(text)

    assert result == text
    assert warnings == []
