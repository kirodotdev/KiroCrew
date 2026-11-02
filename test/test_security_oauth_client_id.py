"""The OAuth banner gate and dynamically registered ``client_id`` values.

RFC 7591 sets no format for a ``client_id``, so a provider that registers
clients dynamically (PostHog: 40 alphanumeric characters) mints values that read
as markerless AWS secrets. At a TRUSTED consent endpoint (builtin, the operator's
``oauth_endpoints.json``, or one verified by MCP discovery) a ``client_id`` made
only of RFC 3986 unreserved characters, 8 to 128 long, skips the markerless
scan. Everything else about the gate stays as it was, and these tests pin both
halves.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import pytest
from oauth_dcr_fixture import (
    CHALLENGE,
    POSTHOG_CLIENT_ID,
    POSTHOG_HOST,
    POSTHOG_PATH,
    SCOPE,
    STATE,
)
from oauth_dcr_fixture import posthog_consent_url as consent_url

from kiro_crew import security
from kiro_crew.security import oauth_url_contains_credential

BUILTIN_AUTHORIZE = "https://mcp.linear.app/authorize"
UNTRUSTED_AUTHORIZE = "https://auth.untrusted-idp.example/oauth/authorize/"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
AWS_SECRET_ALNUM = "wJalrXUtnFEMIxK7MDEN" "GybPxRfiCYEXAMPLEKEY"


def test_the_fixture_has_the_shape_that_was_rejected() -> None:
    assert len(POSTHOG_CLIENT_ID) == 40 and POSTHOG_CLIENT_ID.isalnum()
    assert security._text_contains_bare_secret(POSTHOG_CLIENT_ID)
    assert 180 <= len(SCOPE.split()) <= 200
    assert len(consent_url().partition("?")[2]) > 3000


class TestTrustedEndpointsAcceptADcrClientId:
    def test_builtin_endpoint(self) -> None:
        url = consent_url(BUILTIN_AUTHORIZE)
        assert security.diagnose_oauth_url_credential(url) is None
        assert oauth_url_contains_credential(url) is False

    def test_operator_file_endpoint(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setattr(security, "_OAUTH_EXTENSION_AUDITED", set())
        monkeypatch.setattr(security, "_OAUTH_EXTENSION_MEMO", {})
        (tmp_path / "oauth_endpoints.json").write_text(
            json.dumps(
                {
                    "additional_authorization_endpoints": [
                        {"host": POSTHOG_HOST, "path": POSTHOG_PATH}
                    ]
                }
            ),
            encoding="utf-8",
        )
        assert oauth_url_contains_credential(consent_url()) is False

    def test_the_rejection_is_endpoint_exemptible_so_discovery_runs(self) -> None:
        # Discovery only runs for a URL the allowlist would clear; before the
        # fix the client_id rejection survived the assumption, so it never did.
        assert oauth_url_contains_credential(consent_url()) is True
        assert security.oauth_rejection_is_endpoint_exemptible(consent_url()) is True

    @pytest.mark.parametrize(
        "client_id",
        [
            "Iv1.a1b2c3d4e5f6g7h8",
            "mcp_7f3a9c2e41b84d0f9e6a5c3b2d1e0f4a",
            "0oabcde12345FGHIJ697",
            "abc~def-ghi_jkl.mno" + AWS_SECRET_ALNUM,
            AWS_SECRET_ALNUM,
            "A" * 8,
            (AWS_SECRET_ALNUM * 4)[:128],
        ],
        ids=[
            "github-dotted",
            "prefixed",
            "okta",
            "every-unreserved",
            "40-alnum",
            "min-8",
            "max-128",
        ],
    )
    def test_url_safe_client_ids_pass(self, client_id: str) -> None:
        assert oauth_url_contains_credential(consent_url(BUILTIN_AUTHORIZE, client_id)) is False


class TestWhatStaysRejected:
    def test_untrusted_endpoint(self) -> None:
        url = consent_url(UNTRUSTED_AUTHORIZE)
        assert oauth_url_contains_credential(url) is True
        assert security.diagnose_oauth_url_credential(url) is not None

    def test_untrusted_endpoint_with_a_short_scope(self) -> None:
        # The long scope alone trips the query-length rule off the allowlist,
        # so check the client_id itself is still judged there.
        url = f"{UNTRUSTED_AUTHORIZE}?response_type=code&client_id={POSTHOG_CLIENT_ID}"
        diagnostic = security.diagnose_oauth_url_credential(url)
        assert diagnostic is not None
        assert diagnostic.rule == "credential_scan_bare_secret_raw"
        assert diagnostic.parameter == "client_id"

    @pytest.mark.parametrize("written", ["/", "+", "%2F", "%2B", "%2f", "%2b"])
    def test_base64_standard_alphabet(self, written: str) -> None:
        # Spliced into the URL as written, so "%2F" is one encoding layer.
        url = consent_url(BUILTIN_AUTHORIZE, "PLACEHOLDER0").replace(
            "PLACEHOLDER0", AWS_SECRET.replace("/", written), 1
        )
        assert oauth_url_contains_credential(url) is True

    @pytest.mark.parametrize("encoded", ["%2F", "%252F", "%2B", "%252B", "%25252F", "%2525252F"])
    def test_no_decode_depth_earns_the_exemption(self, encoded: str) -> None:
        # Judged at the predicate: whether the markerless scan then flags a
        # doubly encoded run is its own, pre-existing property (it decodes the
        # URL once), the same for every parameter and every endpoint.
        value = AWS_SECRET.replace("/", encoded)
        assert security._oauth_entropy_value_is_protocol_shaped("client_id", value) is False

    @pytest.mark.parametrize(
        "client_id",
        [
            "A" * 7,
            "A" * 129,
            "abc:def0123456",
            "abc%41def0123456",
            "abc=def0123456",
            "abc def0123456",
        ],
        ids=["7-chars", "129-chars", "colon", "percent", "equals", "space"],
    )
    def test_outside_the_shape_is_not_exempt(self, client_id: str) -> None:
        assert security._oauth_entropy_value_is_protocol_shaped("client_id", client_id) is False

    @pytest.mark.parametrize("first", [True, False], ids=["secret-first", "secret-second"])
    def test_a_repeated_client_id_keeps_every_value_in_the_scan(self, first: bool) -> None:
        url = consent_url(BUILTIN_AUTHORIZE, AWS_SECRET_ALNUM)
        assert oauth_url_contains_credential(url) is False
        extra = f"&client_id={POSTHOG_CLIENT_ID if first else AWS_SECRET_ALNUM}"
        duplicated = consent_url(BUILTIN_AUTHORIZE, AWS_SECRET_ALNUM if first else "plain-id-1")
        diagnostic = security.diagnose_oauth_url_credential(duplicated + extra)
        assert diagnostic is not None
        # The diagnostic names the first raw segment of that shape, which can
        # be the (blanked) state, so pin the rule rather than the parameter.
        assert diagnostic.rule == "credential_scan_bare_secret_raw"

    def test_a_repeated_benign_client_id_is_still_accepted(self) -> None:
        # The duplicate is not itself a rejection: values that never look like
        # a secret pass the scan they are now held to.
        url = consent_url(BUILTIN_AUTHORIZE, "plain-id-1") + "&client_id=plain-id-2"
        assert oauth_url_contains_credential(url) is False

    def test_over_long_alnum_client_id_is_rejected(self) -> None:
        url = consent_url(BUILTIN_AUTHORIZE, AWS_SECRET_ALNUM * 4)
        assert oauth_url_contains_credential(url) is True

    @pytest.mark.parametrize("key", ["AKIA" "IOSFODNN7EXAMPLE", "ASIA" "IOSFODNN7EXAMPLE"])
    def test_aws_access_key_in_client_id(self, key: str) -> None:
        url = consent_url(BUILTIN_AUTHORIZE, f"prefix{key}suffix")
        assert oauth_url_contains_credential(url) is True

    def test_github_token_in_client_id(self) -> None:
        url = consent_url(BUILTIN_AUTHORIZE, "ghp_" "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef12")
        assert oauth_url_contains_credential(url) is True

    def test_secret_in_an_unknown_parameter(self) -> None:
        url = consent_url(BUILTIN_AUTHORIZE) + f"&session_blob={AWS_SECRET_ALNUM}"
        assert oauth_url_contains_credential(url) is True

    def test_secret_in_the_path(self) -> None:
        url = consent_url(f"{BUILTIN_AUTHORIZE}/{AWS_SECRET_ALNUM}")
        assert oauth_url_contains_credential(url) is True

    def test_a_dcr_shaped_value_in_another_entropy_field_is_unchanged(self) -> None:
        # Only client_id moved: a 40-char value is still not an S256 challenge.
        url = consent_url(BUILTIN_AUTHORIZE).replace(CHALLENGE, POSTHOG_CLIENT_ID, 1)
        assert oauth_url_contains_credential(url) is True

    @pytest.mark.parametrize("length", [42, 44])
    def test_code_challenge_still_needs_exactly_43(self, length: int) -> None:
        value = (POSTHOG_CLIENT_ID * 2)[:length]
        url = consent_url(BUILTIN_AUTHORIZE).replace(CHALLENGE, value, 1)
        assert oauth_url_contains_credential(url) is True

    def test_state_keeps_its_rule(self) -> None:
        url = consent_url(BUILTIN_AUTHORIZE).replace(STATE, quote(AWS_SECRET, safe=""), 1)
        assert oauth_url_contains_credential(url) is True

    def test_general_redactors_do_not_inherit_the_exemption(self) -> None:
        text = f"see {consent_url(BUILTIN_AUTHORIZE, AWS_SECRET_ALNUM)}"
        for redactor in (security.redact_credentials, security.redact_exfiltration_urls):
            cleaned, warnings = redactor(text)
            assert AWS_SECRET_ALNUM not in cleaned
            assert warnings
