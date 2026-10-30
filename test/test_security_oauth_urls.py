"""Tests for kiro_crew.security: the OAuth authorization-URL credential gate.

Where OAuth entropy is exempt, how ``sanitized_oauth_endpoint`` and its display form name a
rejected endpoint without leaking it, whether the allowlist would fix a rejection, and the
operator's ``oauth_endpoints.json`` extension.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote

import pytest
from oauth_url_corpus import OPERATOR_EXTENSION_OAUTH_URLS

from kiro_crew import security
from kiro_crew.security import (
    is_sensitive_path,
    oauth_url_contains_credential,
    redact_credentials,
    redact_exfiltration_urls,
    sanitized_oauth_endpoint,
    sanitized_oauth_endpoint_display,
    scan_exfiltration_urls,
)


class TestOAuthAuthorizationUrlRedaction:
    """OAuth entropy is exempt only in the dedicated ACP banner-safety path."""

    STATE = "opaque-state-123"
    CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    BARE_AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    BARE_AWS_SECRET_ALNUM = "wJalrXUtnFEMIxK7MDEN" "GybPxRfiCYEXAMPLEKEY"
    GITHUB_TOKEN = "ghp_" "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef12"
    NOTION_URL = (
        "https://api.notion.com/v1/oauth/authorize"
        "?client_id=client123&response_type=code"
        f"&state={STATE}&code_challenge={CHALLENGE}"
        "&code_challenge_method=S256"
    )

    @staticmethod
    def _assert_general_redactors_remove_secret(url: str, secret: str) -> None:
        text = f"Model output: {url}"
        for redactor in (redact_credentials, redact_exfiltration_urls):
            cleaned, warnings = redactor(text)
            assert secret not in cleaned
            assert warnings

    def test_exact_notion_authorize_url_passes_banner_only(self) -> None:
        assert len(self.CHALLENGE) == 43
        assert oauth_url_contains_credential(self.NOTION_URL) is False

        # The generic URL redactor handles arbitrary model/agent text and does
        # not inherit the banner-only OAuth entropy carve-out.
        cleaned, warnings = redact_exfiltration_urls(self.NOTION_URL)
        assert cleaned != self.NOTION_URL
        assert warnings

    def test_diagnostic_identifies_long_query_parameter_shape(self) -> None:
        opaque_state = "Ab9_" * 64
        url = "https://id.example-idp.com/authorize?state=" + opaque_state

        diagnostic = security.diagnose_oauth_url_credential(url)

        assert diagnostic is not None
        assert diagnostic.rule == "exfil_query_length"
        assert diagnostic.component == "query_parameter"
        assert diagnostic.parameter == "state"
        assert diagnostic.shape.length == len(opaque_state)
        assert diagnostic.shape.ascii_uppercase == 64
        assert diagnostic.shape.ascii_lowercase == 64
        assert diagnostic.shape.digits == 64
        assert diagnostic.shape.symbols == 64

    def test_diagnostic_identifies_nonstandard_param_bare_secret_rule(self) -> None:
        url = self.NOTION_URL + f"&session_blob={self.BARE_AWS_SECRET_ALNUM}"

        diagnostic = security.diagnose_oauth_url_credential(url)

        assert diagnostic is not None
        assert diagnostic.rule == "credential_scan_bare_secret_raw"
        assert diagnostic.component == "query_parameter"
        assert diagnostic.parameter is None
        assert diagnostic.shape.length == len(self.BARE_AWS_SECRET_ALNUM)

    @pytest.mark.parametrize("parameter", ["state", "code_challenge", "client_id"])
    def test_recognized_oauth_entropy_does_not_hit_bare_secret_lottery(
        self, parameter: str
    ) -> None:
        digest = hashlib.sha256(b"synthetic-oauth-entropy-regression").digest()
        if parameter == "state":
            entropy = base64.b64encode(digest).decode()[:40]
            url = self.NOTION_URL.replace(self.STATE, entropy, 1)
        else:
            entropy = base64.urlsafe_b64encode(digest).decode().rstrip("=")
            target = self.CHALLENGE if parameter == "code_challenge" else "client123"
            url = self.NOTION_URL.replace(target, entropy, 1)

        # The fixed digest is deliberately one whose shape reaches the generic
        # bare-secret heuristic. OAuth entropy at an approved endpoint must not
        # inherit that probabilistic verdict.
        assert security._text_contains_bare_secret(entropy)
        assert security.diagnose_oauth_url_credential(url) is None
        assert oauth_url_contains_credential(url) is False

    @pytest.mark.parametrize("parameter", ["redirect_uri", "client_id"])
    def test_non_entropy_oauth_parameter_keeps_markerless_secret_scan(self, parameter: str) -> None:
        secret = self.BARE_AWS_SECRET_ALNUM
        url = self.NOTION_URL + f"&{parameter}={secret}"

        assert len(secret) == 40
        assert security._text_contains_bare_secret(secret)
        assert oauth_url_contains_credential(url) is True

    @pytest.mark.parametrize(
        ("label", "mutate"),
        [
            ("42-chars", lambda value: value[:42]),
            ("44-chars", lambda value: value + "Q"),
            ("standard-alphabet", lambda value: value[:41] + "+/"),
            ("percent-encoded-slash", lambda value: value[:40] + "%2F" + value[40:42]),
        ],
    )
    def test_client_id_exemption_only_covers_exact_base64url_shape(
        self, label: str, mutate: Callable[[str], str]
    ) -> None:
        digest = hashlib.sha256(b"synthetic-oauth-entropy-regression").digest()
        value = mutate(base64.urlsafe_b64encode(digest).decode().rstrip("="))
        url = self.NOTION_URL.replace("client123", value, 1)

        assert security._text_contains_bare_secret(unquote(value)), label
        assert oauth_url_contains_credential(url) is True

    def test_client_id_exemption_needs_an_approved_endpoint(self) -> None:
        digest = hashlib.sha256(b"synthetic-oauth-entropy-regression").digest()
        value = base64.urlsafe_b64encode(digest).decode().rstrip("=")
        url = self.NOTION_URL.replace("client123", value, 1).replace(
            "api.notion.com", "auth.attacker.example", 1
        )

        assert oauth_url_contains_credential(url) is True

    def test_entropy_exemption_does_not_cover_adversarial_url_shapes(self) -> None:
        digest = hashlib.sha256(b"synthetic-oauth-entropy-regression").digest()
        entropy = base64.b64encode(digest).decode()[:40]
        approved = self.NOTION_URL.replace(self.STATE, entropy, 1)
        adversarial_urls = {
            "http": approved.replace("https://", "http://", 1),
            "explicit-port": approved.replace("api.notion.com", "api.notion.com:443", 1),
            "host-suffix": approved.replace("api.notion.com", "api.notion.com.attacker.example", 1),
            "path-suffix": approved.replace("/v1/oauth/authorize", "/v1/oauth/authorize/extra", 1),
            "userinfo": self.NOTION_URL.replace("https://", f"https://{entropy}@", 1),
            "path": self.NOTION_URL.replace(
                "/v1/oauth/authorize", f"/v1/oauth/{entropy}/authorize", 1
            ),
            "path-params": self.NOTION_URL.replace(
                "/v1/oauth/authorize", "/v1/oauth/authorize;session=ok", 1
            ),
            "fragment": self.NOTION_URL + f"#{entropy}",
            "backslash": rf"https://evil.example\@api.notion.com/v1/oauth/authorize?state={entropy}",
            "unknown-param": self.NOTION_URL + f"&session_blob={entropy}",
        }

        for shape, url in adversarial_urls.items():
            assert security.diagnose_oauth_url_credential(url) is not None, shape
            assert oauth_url_contains_credential(url) is True, shape

    def test_credential_shaped_parameter_name_is_omitted(self) -> None:
        raw_name = self.GITHUB_TOKEN
        url = f"https://api.notion.com/v1/oauth/authorize?{raw_name}=x"

        diagnostic = security.diagnose_oauth_url_credential(url)

        assert diagnostic is not None
        assert diagnostic.parameter is None
        assert raw_name not in json.dumps(diagnostic.as_dict(), sort_keys=True)

    def test_unrecognized_parameter_name_is_omitted_from_diagnostic_and_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        raw_name = "opaqueCredentialLikeKey"
        opaque_value = "Ab9_" * 64
        url = f"https://id.example-idp.com/authorize?{raw_name}={opaque_value}"

        diagnostic = security.diagnose_oauth_url_credential(url)

        assert diagnostic is not None
        assert diagnostic.rule == "exfil_query_length"
        assert diagnostic.parameter is None
        with caplog.at_level("WARNING", logger="kiro_crew.security"):
            assert oauth_url_contains_credential(url) is True
        output = json.dumps(diagnostic.as_dict(), sort_keys=True) + "\n" + caplog.text
        assert raw_name not in output
        assert opaque_value not in output

    def test_diagnostic_and_log_never_disclose_parameter_value(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        raw_value = self.GITHUB_TOKEN
        url = self.NOTION_URL.replace(self.STATE, raw_value, 1)

        diagnostic = security.diagnose_oauth_url_credential(url)
        assert diagnostic is not None
        assert diagnostic.rule == "fixed_credential_raw"
        assert diagnostic.component == "query_parameter"
        assert diagnostic.parameter == "state"

        with caplog.at_level("WARNING", logger="kiro_crew.security"):
            assert oauth_url_contains_credential(url) is True

        serialized = json.dumps(diagnostic.as_dict(), sort_keys=True)
        logged = "\n".join(record.getMessage() for record in caplog.records)
        digest = hashlib.sha256(raw_value.encode()).hexdigest()
        for output in (serialized, repr(diagnostic), logged):
            assert url not in output
            assert raw_value not in output
            assert raw_value[:16] not in output
            assert raw_value[-16:] not in output
            assert digest not in output

    @pytest.mark.parametrize(
        "url",
        [
            NOTION_URL.replace("api.notion.com", "evil.example", 1),
            NOTION_URL.replace("api.notion.com", "api.notion.com.evil.example", 1),
            NOTION_URL.replace("/v1/oauth/authorize", "/v1/oauth/authorize/extra", 1),
            NOTION_URL.replace("api.notion.com", "api.notion.com:443", 1),
            NOTION_URL.replace("https://", "http://", 1),
        ],
        ids=[
            "unapproved-host",
            "suffix-host",
            "path-prefix",
            "explicit-port",
            "http-scheme",
        ],
    )
    def test_unapproved_endpoint_fails_closed(self, url: str) -> None:
        assert oauth_url_contains_credential(url) is True

    def test_userinfo_embedded_token_fails_closed(self) -> None:
        url = f"https://{self.GITHUB_TOKEN}@api.notion.com/v1/oauth/authorize" "?state=ok"
        assert oauth_url_contains_credential(url) is True
        cleaned, warnings = redact_credentials(url)
        assert self.GITHUB_TOKEN not in cleaned
        assert warnings

    def test_backslash_authority_spoof_fails_closed(self) -> None:
        url = r"https://evil.com\@api.notion.com/v1/oauth/authorize?state=ok"
        assert oauth_url_contains_credential(url) is True

    def test_bare_aws_secret_in_hostname_fails_closed(self) -> None:
        assert len(self.BARE_AWS_SECRET_ALNUM) == 40
        url = f"https://{self.BARE_AWS_SECRET_ALNUM}.example/oauth/authorize" "?state=ok"
        assert oauth_url_contains_credential(url) is True

    def test_bare_aws_secret_in_fragment_fails_closed(self) -> None:
        assert len(self.BARE_AWS_SECRET) == 40
        url = f"{self.NOTION_URL}#{self.BARE_AWS_SECRET}"
        assert oauth_url_contains_credential(url) is True

    @pytest.mark.parametrize(
        "suffix",
        [
            ";session=ok?state=ok",
            "?state=ok#continue",
        ],
        ids=["path-params", "fragment"],
    )
    def test_path_params_and_fragments_fail_closed(self, suffix: str) -> None:
        url = "https://api.notion.com/v1/oauth/authorize" + suffix
        assert oauth_url_contains_credential(url) is True

    def test_unknown_query_parameter_with_secret_fails_closed(self) -> None:
        url = self.NOTION_URL + f"&session_blob={self.GITHUB_TOKEN}"
        assert oauth_url_contains_credential(url) is True
        self._assert_general_redactors_remove_secret(url, self.GITHUB_TOKEN)

    def test_duplicate_value_in_standard_and_unknown_param_fails_closed(self) -> None:
        url = self.NOTION_URL + f"&session_blob={self.CHALLENGE}"
        assert oauth_url_contains_credential(url) is True
        cleaned, warnings = redact_exfiltration_urls(url)
        assert cleaned != url
        assert warnings

    @pytest.mark.parametrize("parameter", ["state", "code_challenge"])
    @pytest.mark.parametrize(
        "credential",
        [
            "AKIA" "IOSFODNN7EXAMPLE",
            GITHUB_TOKEN,
        ],
        ids=["aws-access-key", "github-token"],
    )
    def test_fixed_credential_inside_recognized_param_fails_closed(
        self, parameter: str, credential: str
    ) -> None:
        original = self.STATE if parameter == "state" else self.CHALLENGE
        url = self.NOTION_URL.replace(original, f"prefix{credential}suffix", 1)
        assert oauth_url_contains_credential(url) is True
        self._assert_general_redactors_remove_secret(url, credential)

    def test_once_percent_decoded_fixed_credential_fails_closed(self) -> None:
        encoded_token = "%67%68%70%5F" + self.GITHUB_TOKEN.removeprefix("ghp_")
        url = self.NOTION_URL.replace(self.STATE, encoded_token, 1)
        assert oauth_url_contains_credential(url) is True

    def test_base64_encoded_credential_inside_state_fails_closed(self) -> None:
        encoded = base64.b64encode(self.GITHUB_TOKEN.encode()).decode()
        url = self.NOTION_URL.replace(self.STATE, encoded, 1)
        assert oauth_url_contains_credential(url) is True
        self._assert_general_redactors_remove_secret(url, encoded)

    def test_bare_aws_secret_inside_state_fails_closed_everywhere(self) -> None:
        assert len(self.BARE_AWS_SECRET) == 40
        # A base64-standard-alphabet run is a shape base64url cannot emit, so it
        # never inherits the entropy exemption -- no `+`/`/` reaches the blanked
        # set at an approved endpoint.
        assert "/" in self.BARE_AWS_SECRET
        url = self.NOTION_URL.replace(self.STATE, self.BARE_AWS_SECRET, 1)
        assert oauth_url_contains_credential(url) is True
        self._assert_general_redactors_remove_secret(url, self.BARE_AWS_SECRET)

    def test_percent_encoded_secret_alphabet_cannot_buy_the_exemption(self) -> None:
        # The markerless scan runs on the raw and decoded URL, so the shape test
        # must too: `%2F` must not launder a base64-standard run into exemption.
        url = self.NOTION_URL.replace(self.STATE, self.BARE_AWS_SECRET.replace("/", "%2F"), 1)
        assert oauth_url_contains_credential(url) is True

    @pytest.mark.parametrize(
        "encoded_slash",
        ["%2F", "%252F", "%25252F", "%2525252F"],
        ids=["single", "double", "triple", "over-budget"],
    )
    def test_no_encoding_depth_earns_the_entropy_exemption(self, encoded_slash: str) -> None:
        # One decode pass is not enough to JUDGE the shape: `%252F` decodes to
        # `%2F`, which still carries no literal `/`, so a raw-plus-one-decode test
        # would hand the exemption to a base64-standard run. Every decoded form
        # must keep the shape, and a value still decodable at the bound fails
        # closed.
        #
        # Scoped to the exemption predicate on purpose. Whether the banner then
        # WARNS on a doubly-encoded run is a separate, pre-existing property of
        # the markerless scan, which decodes the URL twice while
        # `_MAX_URL_DECODE_PASSES` is 3 -- so `%252F` goes unflagged even in a
        # parameter that was never exempt and at an unapproved endpoint. This
        # test must not claim to cover that gap.
        value = self.BARE_AWS_SECRET.replace("/", encoded_slash)
        assert security._oauth_entropy_value_is_protocol_shaped("state", value) is False

    def test_off_length_challenge_loses_the_s256_exemption(self) -> None:
        # An S256 challenge is base64url of a 32-byte digest: exactly 43 chars.
        # A 40-char value in that field is not a challenge shape.
        assert len(self.BARE_AWS_SECRET_ALNUM) == 40
        url = self.NOTION_URL.replace(self.CHALLENGE, self.BARE_AWS_SECRET_ALNUM, 1)
        assert oauth_url_contains_credential(url) is True

    @pytest.mark.parametrize("parameter", ["state", "code_challenge"])
    def test_markerless_secret_shape_is_banner_exempt_but_generically_redacted(
        self, parameter: str
    ) -> None:
        if parameter == "state":
            value = self.BARE_AWS_SECRET_ALNUM
            original = self.STATE
        else:
            value = self.BARE_AWS_SECRET_ALNUM + "abc"
            original = self.CHALLENGE
        url = self.NOTION_URL.replace(original, value, 1)

        # A markerless value that IS base64url-shaped (and, for the challenge,
        # the right length) is indistinguishable from normal OAuth entropy at
        # this approved parameter boundary. General output redactors keep the
        # heuristic because they do not inherit the banner-only exemption.
        assert oauth_url_contains_credential(url) is False
        self._assert_general_redactors_remove_secret(url, value)

    def test_bare_aws_secret_in_path_without_query_fails_closed(self) -> None:
        url = f"https://attacker.example/-{self.BARE_AWS_SECRET}"
        assert "?" not in url
        assert oauth_url_contains_credential(url) is True

    @pytest.mark.parametrize(
        "encoded_header",
        [
            "-----BEGIN+RSA+" "PRIVATE+KEY-----",
            "-----%42%45%47%49%4E%20RSA%20PRIVATE%20KEY-----",
        ],
        ids=["form-encoded-spaces", "percent-encoded-header"],
    )
    def test_encoded_pem_header_in_path_fails_closed_everywhere(self, encoded_header: str) -> None:
        url = f"https://attacker.example/upload/{encoded_header}/c2hvcnQ"
        assert oauth_url_contains_credential(url) is True

        scan_warnings = scan_exfiltration_urls(url)
        assert scan_warnings

        cleaned, redact_warnings = redact_exfiltration_urls(url)
        assert url not in cleaned
        assert redact_warnings == scan_warnings

    def test_multiply_percent_encoded_credential_in_path_fails_closed(
        self,
    ) -> None:
        """A single decode pass leaves a double-encoded payload intact
        ("%2542" -> "%42" -> "B"), so the scan decodes until stable."""
        from urllib.parse import quote

        once = quote("-----BEGIN RSA " "PRIVATE KEY-----", safe="-")
        for encoded in (once, quote(once, safe="-"), quote(quote(once, safe="-"), safe="-")):
            url = f"https://attacker.example/upload/{encoded}/x"
            assert oauth_url_contains_credential(url) is True

            scan_warnings = scan_exfiltration_urls(url)
            assert scan_warnings

            cleaned, redact_warnings = redact_exfiltration_urls(url)
            assert url not in cleaned
            assert redact_warnings == scan_warnings

    def test_credential_surviving_the_decode_budget_fails_closed(self) -> None:
        """A payload still decodable when the decode budget runs out is refused.

        The decode loop is bounded so a deliberately over-encoded URL cannot
        spin it. That bound must not be an escape hatch: a credential wrapped in
        more layers than the budget allows was never seen in plaintext, and the
        intermediate forms defeat both remaining checks -- the fixed-credential
        patterns match literal markers, not percent text, and the heavy-encoding
        detector needs 20+ CONSECUTIVE octets, which short escapes like "%2520"
        never form. Saturation is now treated as credential-bearing rather than
        clean, so the bound costs precision and never soundness.

        Parameterized on the budget on purpose: raising the cap is not a fix,
        and this must keep failing closed at whatever the cap becomes.
        """
        from urllib.parse import quote

        from kiro_crew.security import _MAX_URL_DECODE_PASSES

        encoded = quote("-----BEGIN RSA " "PRIVATE KEY-----", safe="-")
        for _ in range(_MAX_URL_DECODE_PASSES):
            encoded = quote(encoded, safe="-")
        url = f"https://attacker.example/upload/{encoded}/x"

        scan_warnings = scan_exfiltration_urls(url)
        assert scan_warnings

        cleaned, redact_warnings = redact_exfiltration_urls(url)
        assert url not in cleaned
        assert redact_warnings == scan_warnings

    def test_a_benign_singly_encoded_url_is_left_alone(self) -> None:
        """The saturation guard must not redact ordinary encoded URLs.

        One decode pass reaches a stable payload here, so the budget is never
        exhausted and the guard stays silent. This is the positive control for
        the test above: a fail-closed rule that fires on normal traffic would
        be indistinguishable from over-redaction.
        """
        url = "https://docs.example.com/guide?path=%2Fhome%2Fuser%2Freport.pdf"

        assert scan_exfiltration_urls(url) == []
        cleaned, warnings = redact_exfiltration_urls(url)
        assert cleaned == url
        assert warnings == []

    def test_heavy_percent_encoding_in_standard_param_fails_closed(self) -> None:
        url = self.NOTION_URL.replace(self.STATE, "%41" * 25, 1)
        assert oauth_url_contains_credential(url) is True
        cleaned, warnings = redact_exfiltration_urls(url)
        assert cleaned != url
        assert warnings

    def test_miro_mcp_authorize_endpoint_is_approved(self) -> None:
        """mcp.miro.com/authorize is a reporter-verified RFC 8414 endpoint
        -- a real PKCE consent URL there must pass the banner gate."""
        url = self.NOTION_URL.replace(
            "https://api.notion.com/v1/oauth/authorize",
            "https://mcp.miro.com/authorize",
            1,
        )
        assert oauth_url_contains_credential(url) is False


class TestSanitizedOAuthEndpoint:
    """``sanitized_oauth_endpoint`` names a rejected endpoint without leaking.

    The boolean gate alone leaves the user unable to tell WHICH URL tripped the
    scanner; this helper surfaces host+path only. The invariant under
    test: query values, fragments, userinfo, and credential-bearing paths never
    appear in the returned tuple.
    """

    GITHUB_TOKEN = "ghp_" "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef12"

    def test_returns_host_and_path_only(self) -> None:
        result = sanitized_oauth_endpoint(
            "https://idp.example/realms/dev/authorize"
            "?state=topsecretstate&code_challenge=alsosecret"
        )
        assert result == ("idp.example", "/realms/dev/authorize")

    def test_query_values_never_echoed(self) -> None:
        result = sanitized_oauth_endpoint(
            f"https://idp.example/authorize?token={self.GITHUB_TOKEN}"
        )
        assert result is not None
        assert self.GITHUB_TOKEN not in "".join(result)

    def test_host_is_lowercased(self) -> None:
        assert sanitized_oauth_endpoint("https://IdP.Example/Authorize") == (
            "idp.example",
            "/Authorize",  # paths are case-sensitive, only the host normalizes
        )

    def test_empty_path_defaults_to_root(self) -> None:
        assert sanitized_oauth_endpoint("https://idp.example") == ("idp.example", "/")

    def test_userinfo_authority_returns_none(self) -> None:
        """A userinfo-bearing authority is never named — raw or percent-encoded
        (user%3Apass%40host hides inside what urlparse reports as the
        hostname), mirroring the rejection gate's own check."""
        assert (
            sanitized_oauth_endpoint(f"https://{self.GITHUB_TOKEN}@idp.example/authorize") is None
        )
        assert (
            sanitized_oauth_endpoint("https://user%3Apass%40idp.example/authorize?state=x") is None
        )
        # DOUBLE-encoded userinfo (%2540) survives one decode pass; the "@"
        # check runs at every decode layer like the rest of the scan.
        assert (
            sanitized_oauth_endpoint("https://user%253Apass%2540idp.example/authorize?state=x")
            is None
        )

    def test_fragment_never_echoed(self) -> None:
        result = sanitized_oauth_endpoint("https://idp.example/authorize#fragmentsecret")
        assert result == ("idp.example", "/authorize")

    def test_credential_in_path_is_redacted(self) -> None:
        result = sanitized_oauth_endpoint(f"https://idp.example/{self.GITHUB_TOKEN}/authorize")
        assert result is not None
        host, path = result
        assert host == "idp.example"
        assert self.GITHUB_TOKEN not in path
        assert path == security.REDACTED_CREDENTIAL_TAG

    def test_format_character_split_credential_in_path_is_redacted(self) -> None:
        """Invisible format characters (U+200B) split a credential so no
        substring pattern matches, yet the browser renders the fragments
        visually reassembled — presence of ANY category-Cf character in a
        component is disqualifying on its own."""
        split_token = "\u200b".join(
            self.GITHUB_TOKEN[i : i + 8] for i in range(0, len(self.GITHUB_TOKEN), 8)
        )
        result = sanitized_oauth_endpoint(f"https://idp.example/{split_token}/authorize?x=1")
        assert result is not None
        host, path = result
        assert host == "idp.example"
        assert "\u200b" not in path
        assert path == security.REDACTED_CREDENTIAL_TAG

    def test_percent_encoded_format_character_in_path_is_redacted(self) -> None:
        """%E2%80%8B only becomes U+200B after a decode pass — the format
        character check runs on every decode layer like the rest of the scan."""
        encoded_zwsp = "%E2%80%8B"
        result = sanitized_oauth_endpoint(f"https://idp.example/auth{encoded_zwsp}orize?state=x")
        assert result is not None
        host, path = result
        assert host == "idp.example"
        assert path == security.REDACTED_CREDENTIAL_TAG

    def test_format_character_in_host_returns_none(self) -> None:
        """A host carrying an invisible format character is not a nameable
        identity — the helper falls back to the unnamed message."""
        assert sanitized_oauth_endpoint("https://idp\u200bevil.example/authorize") is None

    def test_percent_encoded_credential_in_path_is_redacted(self) -> None:
        encoded = "%67%68%70%5F" + self.GITHUB_TOKEN.removeprefix("ghp_")
        result = sanitized_oauth_endpoint(f"https://idp.example/{encoded}/authorize")
        assert result is not None
        host, path = result
        assert self.GITHUB_TOKEN not in path
        assert encoded not in path
        assert path == security.REDACTED_CREDENTIAL_TAG

    def test_double_percent_encoded_credential_in_path_is_redacted(self) -> None:
        """The rejection gate decodes up to _MAX_URL_DECODE_PASSES, so it
        rejects a DOUBLE-encoded credential on a deeper pass — the sanitizer
        must not echo bytes the gate refused."""
        double_encoded = "%2567%2568%2570%255F" + self.GITHUB_TOKEN.removeprefix("ghp_")
        result = sanitized_oauth_endpoint(f"https://idp.example/{double_encoded}/authorize")
        assert result is not None
        _, path = result
        assert self.GITHUB_TOKEN not in path
        assert double_encoded not in path
        assert path == security.REDACTED_CREDENTIAL_TAG

    def test_path_still_decodable_past_budget_is_redacted(self) -> None:
        """A path that keeps yielding new decode layers past the budget cannot
        be fully scanned — fail closed to the tag, mirroring the gate."""
        nested = "%2525252541"  # "A" percent-encoded 5 layers deep
        result = sanitized_oauth_endpoint(f"https://idp.example/{nested}/authorize")
        assert result is not None
        _, path = result
        assert path == security.REDACTED_CREDENTIAL_TAG

    def test_plus_delimited_private_key_in_path_is_redacted(self) -> None:
        """Form-encoded material delimits with "+"; the scan must fold it to
        spaces (unquote_plus) or a plus-separated private-key header slips
        through every decode layer unmatched."""
        result = sanitized_oauth_endpoint("https://idp.example/BEGIN+RSA+PRIVATE+KEY/authorize")
        assert result is not None
        _, path = result
        assert path == security.REDACTED_CREDENTIAL_TAG

    def test_credential_in_hostname_returns_none(self) -> None:
        """A credential smuggled into a DNS label (hyphens are DNS-legal, so a
        Slack-token-shaped label parses as a hostname) must not be echoed —
        a host is an identity, so the whole helper bails."""
        url = "https://xoxb-1234567890-AbCdEfGhIjKl.evil.example/authorize?state=x"
        assert sanitized_oauth_endpoint(url) is None

    def test_non_ascii_host_is_surfaced_as_idna_alabel(self) -> None:
        """An internationalized host surfaces in punycode A-label form: defuses
        homoglyph spoofing and matches the ASCII-only oauth_endpoints.json
        entry shape."""
        result = sanitized_oauth_endpoint("https://bücher.example/authorize")
        assert result is not None
        host, path = result
        assert host == "xn--bcher-kva.example"
        assert host.isascii()
        assert path == "/authorize"

    def test_fullwidth_host_normalizing_into_a_credential_returns_none(self) -> None:
        """IDNA nameprep folds fullwidth characters to ASCII, so a token-shaped
        fullwidth host can NORMALIZE INTO a credential the pre-IDNA scan could
        not match — the surfaced form must be re-scanned after every transform."""
        fullwidth = "ｘｏｘｂ－１２３４５６７８９０－ａｂｃｄｅｆｇｈｉｊｋｌ"
        assert sanitized_oauth_endpoint(f"https://{fullwidth}.evil.example/authorize") is None

    def test_overlong_path_is_truncated(self) -> None:
        # Hyphenated segments: no 40+ run of the base64 alphabet, so the path
        # is benign-long rather than entropy-suspicious — it truncates, not
        # redacts.
        long_path = "/seg-ment" * 40
        result = sanitized_oauth_endpoint(f"https://idp.example{long_path}")
        assert result is not None
        _, path = result
        assert len(path) == security._SANITIZED_OAUTH_PATH_MAX_LEN + 1
        assert path.endswith("…")

    def test_overlong_host_is_capped_with_a_marker(self) -> None:
        # 30-char labels: below the 40-char bare-run floor, so the host is
        # benign-long — it caps, not bails, and the cap is VISIBLE: a chopped
        # host that reads as a whole hostname names an endpoint that does not
        # exist, which is worse than an obviously partial one.
        long_host = ".".join(["a" * 30] * 9) + ".example"
        result = sanitized_oauth_endpoint(f"https://{long_host}/authorize")
        assert result is not None
        host, _ = result
        assert len(host) == security._SANITIZED_OAUTH_HOST_MAX_LEN + 1
        assert host.endswith("…")
        assert host[:-1] == long_host[: security._SANITIZED_OAUTH_HOST_MAX_LEN]

    def test_a_host_at_the_cap_is_not_marked(self) -> None:
        # Exactly the DNS maximum is a legal hostname; only an EXCESS is chopped.
        # 30-char labels again: no 40-char run that reads as a bare secret.
        at_cap = ".".join(["b" * 30] * 8) + ".abcde"
        assert len(at_cap) == security._SANITIZED_OAUTH_HOST_MAX_LEN
        result = sanitized_oauth_endpoint(f"https://{at_cap}/authorize")
        assert result == (at_cap, "/authorize")

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "https://[bad-ipv6/x",
            "not a url at all",
            "https:///path-without-host",
        ],
        ids=["empty", "invalid-ipv6", "not-a-url", "no-host"],
    )
    def test_unparseable_urls_return_none(self, url: str) -> None:
        assert sanitized_oauth_endpoint(url) is None


def _long_state_query(*, extra: str = "") -> str:
    """A standard front-channel query whose opaque ``state`` pushes it past the
    long-query heuristic: rejected at any endpoint outside the allowlist, clean
    at an allowlisted one, because every parameter is a known OAuth name."""
    return (
        "?client_id=client123&response_type=code"
        "&redirect_uri=https%3A%2F%2Fexample.com%2Fcallback"
        "&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        "&code_challenge_method=S256"
        "&state=" + ("a1B2c3D4" * 16) + extra
    )


class TestOAuthRejectionIsEndpointExemptible:
    """``oauth_rejection_is_endpoint_exemptible`` answers "would the allowlist fix it?".

    The counterfactual (``diagnose_oauth_url_credential(url,
    assume_approved_endpoint=True)``) re-runs the real gate with the endpoint
    treated as approved and nothing else relaxed. The operator-extension corpus
    is the positive set by definition: each entry is a URL the field rejected
    until its operator added the endpoint.
    """

    @pytest.mark.parametrize(
        ("url", "endpoint"),
        [(url, endpoint) for _, url, endpoint in OPERATOR_EXTENSION_OAUTH_URLS],
        ids=[name for name, _, _ in OPERATOR_EXTENSION_OAUTH_URLS],
    )
    def test_corpus_urls_are_rejected_today_and_pass_once_allowlisted(
        self, url: str, endpoint: tuple[str, str]
    ) -> None:
        assert security.diagnose_oauth_url_credential(url) is not None
        assert security.diagnose_oauth_url_credential(url, assume_approved_endpoint=True) is None
        assert security.oauth_rejection_is_endpoint_exemptible(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://idp.example.com/authorize?access_token=AKIAIOSFODNN7EXAMPLE",
            "https://idp.example.com/authorize" + _long_state_query(extra="#frag"),
            "https://idp.example.com/authorize;v=1" + _long_state_query(),
            f"https://user:pw@idp.example.com/authorize{_long_state_query()}",
            "http://idp.example.com/authorize" + _long_state_query(),
            "https://idp.example.com:8443/authorize" + _long_state_query(),
            "https://idp.example.com/authorize?state=" + ("%41" * 80),
        ],
        ids=[
            "fixed-credential-in-query",
            "fragment",
            "path-params",
            "userinfo",
            "http-scheme",
            "explicit-port",
            "heavy-percent-encoding",
        ],
    )
    def test_unconditional_rules_still_reject_under_the_assumption(self, url: str) -> None:
        assert security.diagnose_oauth_url_credential(url) is not None
        assert (
            security.diagnose_oauth_url_credential(url, assume_approved_endpoint=True) is not None
        )
        assert security.oauth_rejection_is_endpoint_exemptible(url) is False

    def test_a_url_the_gate_accepts_has_nothing_to_exempt(self) -> None:
        clean = "https://idp.example.com/authorize?state=abc&code_challenge_method=S256"
        assert security.diagnose_oauth_url_credential(clean) is None
        assert security.oauth_rejection_is_endpoint_exemptible(clean) is False

    def test_the_assumption_does_not_consult_the_operator_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The counterfactual is a pure re-run of the gate: no allowlist lookup,
        so no ``oauth_endpoint_extension_used`` audit event can fire for an
        endpoint nobody actually approved."""
        looked_up: list[tuple[str, str]] = []

        def spy(host: str, path: str) -> bool:
            looked_up.append((host, path))
            return False

        monkeypatch.setattr(security, "_approved_oauth_authorization_endpoint", spy)
        url = OPERATOR_EXTENSION_OAUTH_URLS[0][1]
        assert security.diagnose_oauth_url_credential(url, assume_approved_endpoint=True) is None
        assert looked_up == []
        assert security.diagnose_oauth_url_credential(url) is not None
        assert looked_up != []

    def test_the_default_verdict_is_unchanged(self) -> None:
        """``assume_approved_endpoint`` defaults off, so the boolean gate every
        caller uses is byte-for-byte the old one."""
        url = OPERATOR_EXTENSION_OAUTH_URLS[0][1]
        assert security.oauth_url_contains_credential(url) is True


class TestSanitizedOAuthEndpointDisplay:
    """``sanitized_oauth_endpoint_display`` is the COPY-READY contract.

    The diagnostic pair may legitimately carry a component that is not
    pasteable (redaction tag, ``…`` cap) or that the ``oauth_endpoints.json``
    loader would refuse (``localhost``, an IP literal, a percent-escape in the
    path); and a URL may be rejected for a reason the allowlist cannot clear
    (a fixed credential, a fragment, path parameters, ``http``, a port). A card
    that says "add THIS to oauth_endpoints.json" must hand back a string only
    when adding it would actually work. Every signature the helper judges is
    enumerated here with its verdict, so a widened or narrowed rule shows up as
    a specific row rather than a vague failure.
    """

    GITHUB_TOKEN = "ghp_" "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef12"
    Q = _long_state_query()

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://idp.example.com/authorize" + Q, "idp.example.com/authorize"),
            (
                "https://IdP.Example.COM/Realms/Dev/Authorize" + Q,
                "idp.example.com/Realms/Dev/Authorize",
            ),
            ("https://idp.example.com" + Q, "idp.example.com/"),
            ("https://bücher.example/authorize" + Q, "xn--bcher-kva.example/authorize"),
            # Operator-extension shapes the loader exists for: an Okta org, an
            # Auth0 tenant, a Keycloak realm, a tenant-scoped Entra path.
            (
                "https://dev-123456.okta.com/oauth2/default/v1/authorize" + Q,
                "dev-123456.okta.com/oauth2/default/v1/authorize",
            ),
            ("https://acme.us.auth0.com/authorize" + Q, "acme.us.auth0.com/authorize"),
            (
                "https://idp.example.com/realms/dev/protocol/openid-connect/auth" + Q,
                "idp.example.com/realms/dev/protocol/openid-connect/auth",
            ),
            (
                "https://login.microsoftonline.com/tenant-id/oauth2/v2.0/authorize" + Q,
                "login.microsoftonline.com/tenant-id/oauth2/v2.0/authorize",
            ),
        ],
        ids=[
            "long-state-query-dropped",
            "host-lowercased-path-case-kept",
            "empty-path-becomes-slash",
            "idn-host-in-a-label-form",
            "okta-org",
            "auth0-tenant",
            "keycloak-realm",
            "entra-tenant-path",
        ],
    )
    def test_nameable_endpoints_come_back_as_host_slash_path(self, url: str, expected: str) -> None:
        assert sanitized_oauth_endpoint_display(url) == expected

    @pytest.mark.parametrize(
        ("name", "url", "endpoint"),
        OPERATOR_EXTENSION_OAUTH_URLS,
        ids=[name for name, _, _ in OPERATOR_EXTENSION_OAUTH_URLS],
    )
    def test_the_operator_extension_corpus_is_named_verbatim(
        self, name: str, url: str, endpoint: tuple[str, str]
    ) -> None:
        """The URLs the extension file exists for are exactly the ones the card
        must name, and the string it names is the entry that fixes them."""
        host, path = endpoint
        assert sanitized_oauth_endpoint_display(url) == f"{host}{path}"

    @pytest.mark.parametrize(
        "url",
        [
            # -- the diagnostic pair itself is None --
            "",
            "not a url at all",
            "https:///path-without-host",
            f"https://{GITHUB_TOKEN}@idp.example.com/authorize" + Q,
            "https://user%3Apass%40idp.example.com/authorize" + Q,
            "https://AKIAIOSFODNN7EXAMPLE.example.com/authorize" + Q,
            # -- pair is not pasteable: redaction tag / cap marker --
            "https://idp.example.com/AKIAIOSFODNN7EXAMPLE/authorize" + Q,
            "https://idp.example.com" + "/seg-ment" * 40 + Q,
            "https://" + ".".join(["a" * 30] * 9) + ".example/authorize" + Q,
            # -- the extension loader would refuse the host --
            "https://localhost/authorize" + Q,
            "https://10.0.0.1/authorize" + Q,
            "https://idp.example.com./authorize" + Q,
            "https://idp/authorize" + Q,
            # -- the extension loader would refuse the path --
            "https://idp.example.com/auth%20orize" + Q,
            "https://idp.example.com/../authorize" + Q,
            "https://idp.example.com/auth\\orize" + Q,
            # -- the allowlist could not clear the rejection --
            "https://idp.example.com/authorize?access_token=AKIAIOSFODNN7EXAMPLE",
            "https://idp.example.com/authorize" + _long_state_query(extra="#frag"),
            "https://idp.example.com/authorize;v=1" + Q,
            "http://idp.example.com/authorize" + Q,
            "https://idp.example.com:8443/authorize" + Q,
            "https://idp.example.com:443/authorize" + Q,
            # -- not rejected at all: nothing to name --
            "https://idp.example.com/authorize?state=x&code_challenge_method=S256",
        ],
        ids=[
            "empty",
            "not-a-url",
            "no-host",
            "userinfo",
            "encoded-userinfo",
            "credential-in-host",
            "credential-in-path-redacted",
            "overlong-path-capped",
            "overlong-host-capped",
            "localhost-no-letter-tld",
            "ipv4-literal",
            "trailing-dot-host",
            "single-label-host",
            "percent-escape-in-path",
            "dot-dot-in-path",
            "backslash-in-path",
            "fixed-credential-in-query",
            "fragment",
            "path-params",
            "http-scheme",
            "explicit-port",
            "explicit-default-port",
            "accepted-url",
        ],
    )
    def test_unnameable_endpoints_return_none(self, url: str) -> None:
        assert sanitized_oauth_endpoint_display(url) is None

    def test_the_display_string_is_exactly_what_the_loader_would_accept(self) -> None:
        """Contract closure: split the string back into (host, path) and run it
        through the SAME validators the extension loader applies, so the two
        can only drift together."""
        display = sanitized_oauth_endpoint_display(
            "https://idp.example.com/realms/dev/protocol/openid-connect/auth" + self.Q
        )
        assert display is not None
        host, _, rest = display.partition("/")
        path = "/" + rest
        assert security._OAUTH_EXTENSION_HOST_RE.fullmatch(host)
        assert security._valid_oauth_extension_path(path)
        assert security._validate_operator_oauth_entries(
            {"additional_authorization_endpoints": [{"host": host, "path": path}]}
        ) == frozenset({(host, path)})

    def test_the_raw_url_and_its_query_never_appear_in_the_display(self) -> None:
        url = "https://idp.example.com/authorize" + self.Q
        display = sanitized_oauth_endpoint_display(url)
        assert display == "idp.example.com/authorize"
        for secret in ("a1B2c3D4", "E9Melhoa2Owv", "client_id", "?", "https://"):
            assert secret not in display

    def test_a_redacted_path_is_refused_rather_than_joined(self) -> None:
        # The diagnostic pair is (host, tag): the tag must never be glued onto
        # the host as if it were a path a user could type.
        url = "https://idp.example.com/AKIAIOSFODNN7EXAMPLE/authorize" + self.Q
        assert sanitized_oauth_endpoint(url) == (
            "idp.example.com",
            security._REDACTED_CREDENTIAL_TAG,
        )
        assert sanitized_oauth_endpoint_display(url) is None


class TestOperatorOAuthEndpointExtension:
    """The keystone ``oauth_endpoints.json`` extends the OAuth endpoint set.

    The builtin ``_OAUTH_AUTHORIZATION_ENDPOINTS`` is deliberately code-owned;
    the operator's extension file is the only way to widen it, it fails soft to
    EMPTY on any defect, and every entry is strictly validated. HTTPS-only /
    no-explicit-port / exact-match semantics are identical to the builtin set
    and not relaxable via the file.
    """

    HOST = "acme.okta.com"
    PATH = "/oauth2/v1/authorize"
    CONSENT_URL = (
        "https://acme.okta.com/oauth2/v1/authorize"
        "?client_id=0oabcde12345FGHIJ697"
        "&response_type=code"
        "&scope=openid%20profile%20email%20offline_access"
        "&redirect_uri=https%3A%2F%2Fexample.com%2Fcallback"
        "&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        "&code_challenge_method=S256"
        "&state=" + ("Zx9yW8vU" * 12)
    )

    @staticmethod
    def _write_extension(home: Path, entries: object) -> None:
        (home / "oauth_endpoints.json").write_text(
            (
                json.dumps({"additional_authorization_endpoints": entries})
                if not isinstance(entries, str)
                else entries
            ),
            encoding="utf-8",
        )

    @pytest.fixture(autouse=True)
    # flake-ok: pre-existing class autouse reset of the OAuth audit memo moved verbatim; tracked for a de-flake pass
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse reset moved verbatim; no test in this class calls monkeypatch.undo()
    def _isolated_extension_state(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        """Fresh home + fresh process-global audit/memo state for EVERY test.

        The dedupe set and the file memo are process-global by design; without
        a reset, tests exercising the real emit path would depend on execution
        order.
        """
        from kiro_crew import security

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setattr(security, "_OAUTH_EXTENSION_AUDITED", set())
        monkeypatch.setattr(security, "_OAUTH_EXTENSION_MEMO", {})
        return tmp_path

    @pytest.fixture()
    def ext_home(self, _isolated_extension_state: Path) -> Path:
        return _isolated_extension_state

    # ── Loader: fail-soft postures ──

    def test_missing_file_yields_empty_set(self, ext_home: Path) -> None:
        from kiro_crew.security import _load_operator_oauth_endpoints

        assert _load_operator_oauth_endpoints() == frozenset()

    @pytest.mark.parametrize(
        "content",
        ["{not json", "[]", '"just a string"', '{"additional_authorization_endpoints": {}}'],
        ids=["corrupt", "non-object", "string", "key-not-list"],
    )
    def test_defective_file_yields_empty_set(self, ext_home: Path, content: str) -> None:
        from kiro_crew.security import _load_operator_oauth_endpoints

        self._write_extension(ext_home, content)
        assert _load_operator_oauth_endpoints() == frozenset()

    def test_valid_entry_accepted_and_host_lowercased(self, ext_home: Path) -> None:
        from kiro_crew.security import _load_operator_oauth_endpoints

        self._write_extension(ext_home, [{"host": "ACME.Okta.com", "path": self.PATH}])
        assert _load_operator_oauth_endpoints() == frozenset({(self.HOST, self.PATH)})

    def test_hand_edit_takes_effect_without_restart(self, ext_home: Path) -> None:
        """The check-time re-read contract: no gateway restart, no stale memo."""
        from kiro_crew.security import _load_operator_oauth_endpoints

        self._write_extension(ext_home, [{"host": self.HOST, "path": self.PATH}])
        assert _load_operator_oauth_endpoints() == frozenset({(self.HOST, self.PATH)})
        # Consult the memoized path once more before the edit.
        assert _load_operator_oauth_endpoints() == frozenset({(self.HOST, self.PATH)})

        self._write_extension(ext_home, [{"host": "other.idp.example", "path": "/authorize"}])
        # Force a distinct mtime even on filesystems with coarse timestamps.
        os.utime(
            ext_home / "oauth_endpoints.json",
            ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000),
        )
        assert _load_operator_oauth_endpoints() == frozenset({("other.idp.example", "/authorize")})

        (ext_home / "oauth_endpoints.json").unlink()
        assert _load_operator_oauth_endpoints() == frozenset()

    # ── Loader: hostile entries are individually SKIPPED ──

    @pytest.mark.parametrize(
        "host",
        [
            "*.okta.com",
            "https://acme.okta.com",
            "acme.okta.com:443",
            "user@acme.okta.com",
            "acme.%6fkta.com",
            "acme .okta.com",
            "acme.okta.com\t",
            "acme\\okta.com",
            ".acme.okta.com",
            "acme.okta.com.",
            "192.168.1.1",
            "[::1]",
            "nodots",
            "acme.okta.123",
            "",
            "a" * 260 + ".com",
        ],
        ids=[
            "wildcard",
            "scheme-prefix",
            "explicit-port",
            "userinfo",
            "percent-escape",
            "whitespace",
            "trailing-tab",
            "backslash",
            "leading-dot",
            "trailing-dot",
            "ipv4-literal",
            "ipv6-literal",
            "no-dot",
            "digit-tld",
            "empty",
            "over-length",
        ],
    )
    def test_hostile_host_skipped(self, ext_home: Path, host: str) -> None:
        from kiro_crew.security import _load_operator_oauth_endpoints

        self._write_extension(ext_home, [{"host": host, "path": self.PATH}])
        assert _load_operator_oauth_endpoints() == frozenset()

    @pytest.mark.parametrize(
        "path",
        [
            "authorize",
            "/authorize?x=1",
            "/authorize#frag",
            "/authorize;p=1",
            "/autho%72ize",
            "/auth orize",
            "/auth\\orize",
            "/../authorize",
            "/" + "x" * 513,
        ],
        ids=[
            "no-leading-slash",
            "query",
            "fragment",
            "path-param",
            "percent-escape",
            "whitespace",
            "backslash",
            "dotdot",
            "over-length",
        ],
    )
    def test_hostile_path_skipped(self, ext_home: Path, path: str) -> None:
        from kiro_crew.security import _load_operator_oauth_endpoints

        self._write_extension(ext_home, [{"host": self.HOST, "path": path}])
        assert _load_operator_oauth_endpoints() == frozenset()

    @pytest.mark.parametrize(
        "entry",
        [
            "not-a-dict",
            {"host": 1, "path": "/a"},
            {"host": "ok.example.com", "path": None},
            {"host": "ok.example.com"},
            {},
        ],
        ids=["string-entry", "int-host", "none-path", "missing-path", "empty-dict"],
    )
    def test_non_string_entry_skipped(self, ext_home: Path, entry: object) -> None:
        from kiro_crew.security import _load_operator_oauth_endpoints

        self._write_extension(ext_home, [entry])
        assert _load_operator_oauth_endpoints() == frozenset()

    def test_one_bad_entry_does_not_poison_the_rest(self, ext_home: Path) -> None:
        from kiro_crew.security import _load_operator_oauth_endpoints

        self._write_extension(
            ext_home,
            [{"host": "*.evil.example", "path": "/a"}, {"host": self.HOST, "path": self.PATH}],
        )
        assert _load_operator_oauth_endpoints() == frozenset({(self.HOST, self.PATH)})

    def test_entry_cap_bounds_both_acceptance_and_iteration(self, ext_home: Path) -> None:
        from kiro_crew.security import (
            _ENDPOINT_EXTENSION_CAP,
            _load_operator_oauth_endpoints,
        )

        # Over-cap valid entries: only the first CAP are accepted. A valid
        # entry placed BEYOND the cap must be ignored even when earlier slots
        # were wasted on invalid entries — the slice bounds the iteration
        # itself, so a mangled file cannot amplify into an unbounded walk.
        entries: list[dict] = [
            {"host": f"idp{i}.example.com", "path": "/authorize"}
            for i in range(_ENDPOINT_EXTENSION_CAP + 10)
        ]
        self._write_extension(ext_home, entries)
        assert len(_load_operator_oauth_endpoints()) == _ENDPOINT_EXTENSION_CAP

        invalid_padding: list[dict] = [
            {"host": "*.invalid.example", "path": "/a"}
        ] * _ENDPOINT_EXTENSION_CAP
        self._write_extension(ext_home, invalid_padding + [{"host": self.HOST, "path": self.PATH}])
        assert _load_operator_oauth_endpoints() == frozenset()

    # ── Gate: the extension widens exactly the builtin exemption, nothing more ──

    def test_extended_endpoint_passes_previously_rejected_consent_url(self, ext_home: Path) -> None:
        # Fails closed with no file (the pre-extension behavior) …
        assert oauth_url_contains_credential(self.CONSENT_URL) is True
        # … and passes once the operator allowlists the exact endpoint.
        self._write_extension(ext_home, [{"host": self.HOST, "path": self.PATH}])
        assert oauth_url_contains_credential(self.CONSENT_URL) is False

    @pytest.mark.parametrize(
        "credential",
        ["AKIA" "IOSFODNN7EXAMPLE", "xoxb-1234567890-abcdefghijkl"],
        ids=["aws-access-key", "slack-token"],
    )
    def test_credential_at_extended_endpoint_still_rejected(
        self, ext_home: Path, credential: str
    ) -> None:
        self._write_extension(ext_home, [{"host": self.HOST, "path": self.PATH}])
        url = self.CONSENT_URL.replace("state=", f"state={credential}", 1)
        assert oauth_url_contains_credential(url) is True

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda u: u.replace("https://", "http://", 1),
            lambda u: u.replace("acme.okta.com", "acme.okta.com:443", 1),
            lambda u: u.replace("acme.okta.com", "other.idp.example", 1),
            lambda u: u.replace("acme.okta.com", "acme.okta.com.attacker.example", 1),
            lambda u: u.replace("/oauth2/v1/authorize", "/oauth2/v1/authorize/extra", 1),
        ],
        ids=["http-scheme", "explicit-port", "unknown-host", "lookalike-suffix", "path-suffix"],
    )
    def test_non_matching_urls_still_fail_closed(self, ext_home: Path, mutate) -> None:
        self._write_extension(ext_home, [{"host": self.HOST, "path": self.PATH}])
        assert oauth_url_contains_credential(mutate(self.CONSENT_URL)) is True

    def test_general_redactors_ignore_the_extension(self, ext_home: Path) -> None:
        # The carve-out stays banner-only: arbitrary model/agent text keeps the
        # full heuristics even for an operator-approved endpoint.
        self._write_extension(ext_home, [{"host": self.HOST, "path": self.PATH}])
        cleaned, warnings = redact_exfiltration_urls(self.CONSENT_URL)
        assert cleaned != self.CONSENT_URL
        assert warnings
        assert scan_exfiltration_urls(self.CONSENT_URL)

    # ── SEL audit ──

    def test_extension_approval_emits_audit_event(
        self, ext_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import security

        self._write_extension(ext_home, [{"host": self.HOST, "path": self.PATH}])
        seen: list[tuple[str, str]] = []
        monkeypatch.setattr(
            security,
            "_emit_oauth_extension_used_event",
            lambda host, path: seen.append((host, path)),
        )
        assert oauth_url_contains_credential(self.CONSENT_URL) is False
        assert (self.HOST, self.PATH) in seen

    def test_builtin_approval_does_not_emit_audit_event(
        self, ext_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import security

        seen: list[tuple[str, str]] = []
        monkeypatch.setattr(
            security,
            "_emit_oauth_extension_used_event",
            lambda host, path: seen.append((host, path)),
        )
        url = (
            "https://github.com/login/oauth/authorize"
            "?client_id=Iv1.a1b2c3d4e5f6g7h8&state=xyz789randomstring"
        )
        assert oauth_url_contains_credential(url) is False
        assert seen == []

    def test_audit_event_deduped_per_endpoint_but_not_across_endpoints(
        self, ext_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import security
        from kiro_crew.security import exfil

        logged: list = []

        class _RecorderLog:
            def log(self, event: object) -> None:
                logged.append(event)

        monkeypatch.setattr(exfil, "SecurityEventLog", lambda: _RecorderLog())
        security._emit_oauth_extension_used_event(self.HOST, self.PATH)
        security._emit_oauth_extension_used_event(self.HOST, self.PATH)
        assert len(logged) == 1
        event = logged[0]
        assert event.event_type == "oauth_endpoint_extension_used"
        assert event.metadata["host"] == self.HOST
        assert event.metadata["path"] == self.PATH
        assert event.metadata["file"].endswith("oauth_endpoints.json")

        # A second DISTINCT endpoint still emits: dedupe is per (host, path).
        security._emit_oauth_extension_used_event("other.idp.example", "/authorize")
        assert len(logged) == 2

    def test_audit_failure_does_not_break_the_approval(
        self, ext_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.security import exfil

        class _BrokenLog:
            def log(self, event: object) -> None:
                raise RuntimeError("SEL unavailable")

        monkeypatch.setattr(exfil, "SecurityEventLog", lambda: _BrokenLog())
        self._write_extension(ext_home, [{"host": self.HOST, "path": self.PATH}])
        assert oauth_url_contains_credential(self.CONSENT_URL) is False

    # ── Keystone fence: the agent cannot widen its own trust boundary ──

    @pytest.mark.parametrize("prefix", [".kiro/crew", ".kirocrew"])
    def test_extension_file_is_sensitive_under_every_home_prefix(self, prefix: str) -> None:
        from kiro_crew.security import is_sensitive_write_path

        assert is_sensitive_path(f"~/{prefix}/oauth_endpoints.json") is True
        # The write gate is a superset of the read gate; assert it directly so
        # the file-edit tool path is pinned too.
        assert is_sensitive_write_path(f"~/{prefix}/oauth_endpoints.json") is True

    # ── Corpus contract: operator-extension URLs ──

    @pytest.mark.parametrize(
        "provider,url,endpoint",
        OPERATOR_EXTENSION_OAUTH_URLS,
        ids=[p for p, _, _ in OPERATOR_EXTENSION_OAUTH_URLS],
    )
    def test_operator_extension_corpus_default_config_rejects(
        self, ext_home: Path, provider: str, url: str, endpoint: tuple[str, str]
    ) -> None:
        # Without the operator file these endpoints are NOT exempt — this is
        # what keeps the list out of LEGIT_OAUTH_URLS.
        assert oauth_url_contains_credential(url) is True

    @pytest.mark.parametrize(
        "provider,url,endpoint",
        OPERATOR_EXTENSION_OAUTH_URLS,
        ids=[p for p, _, _ in OPERATOR_EXTENSION_OAUTH_URLS],
    )
    def test_operator_extension_corpus_passes_with_allowlisted_endpoint(
        self, ext_home: Path, provider: str, url: str, endpoint: tuple[str, str]
    ) -> None:
        host, path = endpoint
        self._write_extension(ext_home, [{"host": host, "path": path}])
        assert oauth_url_contains_credential(url) is False
