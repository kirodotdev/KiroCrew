"""Tests for kiro_crew.security: exfiltration-URL redaction and the links it must judge.

``redact_exfiltration_urls`` / ``scan_exfiltration_urls`` on payload shape, URL paths and raw-IP
hosts, and the exact-host exemption a companion credential policy supplies; then the links that
meet the redactor: Kiro Crew's own Slack app-create link, a dashboard access token in every host
form, and the GitHub issue-prefill link, redacted when a model writes it and delivered whole
through the trusted structured-field channel instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kiro_crew import security
from kiro_crew.security import (
    oauth_url_contains_credential,
    redact_exfiltration_urls,
    scan_exfiltration_urls,
)


class TestRedactExfiltrationUrls:
    """Tests for redact_exfiltration_urls — domain-agnostic payload detection."""

    def test_substitution_is_built_from_the_exported_prefix(self) -> None:
        """The URL tag must start with ``EXFILTRATION_REDACTION_TAG_PREFIX``.

        The dashboard chat notice prefix-counts that constant in
        the persisted text to tell the user a URL was rewritten -- the tag
        interpolates the domain, so unlike the constant credential tags it
        cannot be equality-compared. Driving the REAL redactor here pins the
        substitution to the exported constant: if the f-string ever drifts from
        the prefix, this goes red instead of the notice silently never firing.
        """
        from kiro_crew.security import (
            EXFILTRATION_REDACTION_TAG_PREFIX,
            redact_exfiltration_urls,
        )

        url = "https://evil.example.com/steal?data=" + "A" * 250
        result, warnings = redact_exfiltration_urls(f"Link: {url}")
        assert warnings, "fixture no longer trips the redactor; pick another URL"
        assert EXFILTRATION_REDACTION_TAG_PREFIX in result
        assert f"{EXFILTRATION_REDACTION_TAG_PREFIX}evil.example.com]" in result

    def test_url_tag_prefix_does_not_collide_with_credential_tags(self) -> None:
        """Prefix-counting the URL tag must never double-count a credential tag.

        The notice sums ``CREDENTIAL_REDACTION_TAGS`` exact counts and the URL
        prefix count over the same text. That is only safe while neither side
        matches the other's substitution: the prefix must not appear inside any
        credential tag, no credential tag may start with the prefix, and the
        prefix stays OUT of the tuple (it is a prefix, not a full tag -- see the
        tuple's docstring).
        """
        from kiro_crew.security import (
            CREDENTIAL_REDACTION_TAGS,
            EXFILTRATION_REDACTION_TAG_PREFIX,
        )

        assert EXFILTRATION_REDACTION_TAG_PREFIX not in CREDENTIAL_REDACTION_TAGS
        for tag in CREDENTIAL_REDACTION_TAGS:
            assert EXFILTRATION_REDACTION_TAG_PREFIX not in tag
            assert not tag.startswith(EXFILTRATION_REDACTION_TAG_PREFIX)
            assert tag not in EXFILTRATION_REDACTION_TAG_PREFIX

    def test_external_long_query_redacted(self) -> None:
        """External domains with long query strings are still redacted."""
        from kiro_crew.security import redact_exfiltration_urls

        url = "https://evil.com/steal?data=" + "A" * 250
        result, warnings = redact_exfiltration_urls(f"Link: {url}")
        assert "[REDACTED" in result
        assert len(warnings) == 1

    def test_long_query_redacted_domain_agnostic(self) -> None:
        """Long query strings are redacted regardless of domain (no allowlist)."""
        from kiro_crew.security import redact_exfiltration_urls

        # Detection is domain-agnostic: there is no trusted-domain allowlist,
        # so even a long multi-param query on any host is flagged.
        params = "&".join(f"p{i}=value{i}" for i in range(30))
        url = f"https://app.example.com/app/?mode=CODE&{params}"
        assert len(url.split("?", 1)[1]) >= 200  # confirm query > threshold
        result, warnings = redact_exfiltration_urls(f"Link: {url}")
        assert "[REDACTED" in result
        assert len(warnings) == 1

    def test_heavy_url_encoding_redacted(self) -> None:
        """Heavily URL-encoded destinations are redacted regardless of domain."""
        from kiro_crew.security import redact_exfiltration_urls

        url = (
            "https://sso.example.com/federate?account=123456"
            "789012"
            "&destination=https%3A%2F%2Fus-east-1.console.example.com"
            "%2Fcloudwatch%2Fhome%3Fregion%3Dus-east-1%23logsV2%3A"
            "log-groups%2Flog-group%2F%252Faws%252Flambda%252Fmy-func"
            "%2Flog-events%3FfilterPattern%3DERROR"
        )
        result, warnings = redact_exfiltration_urls(f"Logs: {url}")
        assert "[REDACTED" in result
        assert len(warnings) == 1

    def test_short_query_not_redacted_domain_agnostic(self) -> None:
        """Short, benign query strings are not redacted on any domain."""
        from kiro_crew.security import redact_exfiltration_urls

        url = "https://console.example.com/page?k0=val0&k1=val1&k2=val2"
        result, warnings = redact_exfiltration_urls(f"Link: {url}")
        assert "[REDACTED" not in result
        assert len(warnings) == 0

    def test_safe_domain_credential_still_redacted(self) -> None:
        """Credential patterns on safe domains are still redacted."""
        from kiro_crew.security import redact_exfiltration_urls

        url = "https://example.com/api?key=AKIAIOSFODNN7EXAMPLE1234"
        result, warnings = redact_exfiltration_urls(f"Link: {url}")
        assert "[REDACTED" in result
        assert len(warnings) == 1

    def test_short_query_no_redaction(self) -> None:
        """Short query strings on any domain are not redacted."""
        from kiro_crew.security import redact_exfiltration_urls

        url = "https://example.com/page?id=123&name=test"
        result, warnings = redact_exfiltration_urls(f"Link: {url}")
        assert "[REDACTED" not in result
        assert len(warnings) == 0

    def test_amazonaws_not_safe(self) -> None:
        """amazonaws.com is NOT allowlisted — anyone can provision endpoints."""
        from kiro_crew.security import redact_exfiltration_urls

        params = "&".join(f"d{i}=stolen{i}" for i in range(30))
        url = f"https://attacker-bucket.s3.amazonaws.com/exfil?{params}"
        result, warnings = redact_exfiltration_urls(f"Link: {url}")
        assert "[REDACTED" in result
        assert len(warnings) == 1

    def test_s3_presigned_url_preserved(self) -> None:
        """S3 presigned URLs on amazonaws.com are NOT redacted."""
        from kiro_crew.security import redact_exfiltration_urls

        url = (
            "https://my-bucket.s3.us-east-1.amazonaws.com/results/abc.csv"
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=ASIA"
            "QWERTYUIOP123456%2F20260430%2Fus-east-1%2Fs3%2Faws4_request"
            "&X-Amz-Date=20260430T150000Z"
            "&X-Amz-Expires=3600"
            "&X-Amz-SignedHeaders=host"
            "&X-Amz-Signature="
            "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        )
        result, warnings = redact_exfiltration_urls(f"Download: {url}")
        assert "[REDACTED" not in result
        assert len(warnings) == 0

    def test_s3_presigned_url_scan_clean(self) -> None:
        """scan_exfiltration_urls returns no warnings for S3 presigned URLs."""
        from kiro_crew.security import scan_exfiltration_urls

        url = (
            "https://bucket.s3.amazonaws.com/file.csv"
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=ASIA"
            "QWERTYUIOP123456%2F20260430%2Fus-east-1%2Fs3%2Faws4_request"
            "&X-Amz-Date=20260430T150000Z"
            "&X-Amz-Expires=3600"
            "&X-Amz-SignedHeaders=host"
            "&X-Amz-Signature="
            "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        )
        warnings = scan_exfiltration_urls(f"Link: {url}")
        assert len(warnings) == 0

    def test_amazonaws_non_presigned_still_redacted(self) -> None:
        """amazonaws.com URLs without presigned params are still redacted."""
        from kiro_crew.security import redact_exfiltration_urls

        url = "https://evil.s3.amazonaws.com/steal" "?data=" + "A" * 250
        result, warnings = redact_exfiltration_urls(f"Link: {url}")
        assert "[REDACTED" in result
        assert len(warnings) == 1

    def test_spoofed_presigned_params_still_redacted(self) -> None:
        """Spoofed presigned param names with dummy values are still redacted."""
        from kiro_crew.security import redact_exfiltration_urls

        url = (
            "https://attacker.s3.amazonaws.com/exfil"
            "?X-Amz-Algorithm=a&X-Amz-Credential=a"
            "&X-Amz-Expires=a&X-Amz-Signature=&stolen=AKIA"
            "XXXXXXXXXXXXXXXX"
        )
        result, warnings = redact_exfiltration_urls(f"Link: {url}")
        assert "[REDACTED" in result

    def test_presigned_url_with_slack_token_still_redacted(self) -> None:
        """Presigned URL that also contains a Slack token is still redacted."""
        from kiro_crew.security import redact_exfiltration_urls

        url = (
            "https://bucket.s3.amazonaws.com/file.csv"
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=ASIA"
            "QWERTYUIOP123456%2F20260430%2Fus-east-1%2Fs3%2Faws4_request"
            "&X-Amz-Date=20260430T150000Z"
            "&X-Amz-Expires=3600"
            "&X-Amz-SignedHeaders=host"
            "&X-Amz-Signature="
            "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
            "&leak=xoxb-1234567890-abcdefghij"
        )
        result, warnings = redact_exfiltration_urls(f"Link: {url}")
        assert "[REDACTED" in result

    def test_presigned_url_with_extra_exfil_params_still_redacted(self) -> None:
        """Presigned URL with extra non-standard params is still redacted."""
        from kiro_crew.security import redact_exfiltration_urls

        url = (
            "https://attacker.s3.amazonaws.com/file.csv"
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=ASIA"
            "QWERTYUIOP123456%2F20260430%2Fus-east-1%2Fs3%2Faws4_request"
            "&X-Amz-Date=20260430T150000Z"
            "&X-Amz-Expires=3600"
            "&X-Amz-SignedHeaders=host"
            "&X-Amz-Signature="
            "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
            "&exfil=" + "A" * 250
        )
        result, warnings = redact_exfiltration_urls(f"Link: {url}")
        assert "[REDACTED" in result

    def test_redact_presigned_url_survives_alongside_bad_url(self) -> None:
        """Presigned URL is preserved even when another URL triggers redaction.

        This exercises the _is_safe_presigned check inside redact_exfiltration_urls
        (not just scan), because the bad URL causes scan to return warnings,
        so redact doesn't early-return.
        """
        from kiro_crew.security import redact_exfiltration_urls

        bad_url = "https://evil.com/steal?data=" + "A" * 250
        good_url = (
            "https://my-bucket.s3.us-east-1.amazonaws.com/results.csv"
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=ASIA"
            "QWERTYUIOP123456%2F20260430%2Fus-east-1%2Fs3%2Faws4_request"
            "&X-Amz-Date=20260430T150000Z"
            "&X-Amz-Expires=3600"
            "&X-Amz-SignedHeaders=host"
            "&X-Amz-Signature="
            "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        )
        text = f"Bad: {bad_url} Good: {good_url}"
        result, warnings = redact_exfiltration_urls(text)
        assert "[REDACTED" in result
        # Good presigned URL should survive
        assert "my-bucket.s3.us-east-1.amazonaws.com" in result
        assert "X-Amz-Signature=" in result

    def test_presigned_url_with_sts_security_token_preserved(self) -> None:
        """Presigned URL with realistic base64 STS session token is preserved."""
        from kiro_crew.security import scan_exfiltration_urls

        # Realistic 200+ char base64 STS token (matches _EXFIL_PATTERNS blob pattern)
        sts_token = "IQoJb3JpZ2luX2VjE" + "A" * 180 + "=="
        url = (
            "https://my-bucket.s3.us-east-1.amazonaws.com/results.csv"
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=ASIA"
            "QWERTYUIOP123456%2F20260430%2Fus-east-1%2Fs3%2Faws4_request"
            "&X-Amz-Date=20260430T150000Z"
            "&X-Amz-Expires=3600"
            "&X-Amz-SignedHeaders=host"
            "&X-Amz-Signature="
            "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
            f"&X-Amz-Security-Token={sts_token}"
        )
        warnings = scan_exfiltration_urls(f"Link: {url}")
        assert len(warnings) == 0, "STS token in Security-Token should not trigger warning"

    def test_presigned_url_with_exfil_in_allowed_param_redacted(self) -> None:
        """Exfil payload in an allowed param value is caught by value scanning."""
        from kiro_crew.security import scan_exfiltration_urls

        url = (
            "https://evil.s3.us-east-1.amazonaws.com/out.csv"
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=ASIA"
            "QWERTYUIOP123456%2F20260430%2Fus-east-1%2Fs3%2Faws4_request"
            "&X-Amz-Date=20260430T150000Z"
            "&X-Amz-Expires=3600"
            "&X-Amz-SignedHeaders=xoxb-1234567890-abcdefghij"
            "&X-Amz-Signature=abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        )
        warnings = scan_exfiltration_urls(f"Link: {url}")
        assert len(warnings) > 0, "Exfil payload in allowed param value should be flagged"

    def test_presigned_url_with_exfil_in_credential_scope_redacted(self) -> None:
        """Arbitrary data in credential scope is caught by structural validation."""
        from kiro_crew.security import scan_exfiltration_urls

        url = (
            "https://evil.s3.us-east-1.amazonaws.com/out.csv"
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=ASIA"
            "QWERTYUIOP123456%2Fexfiltrated-secret-data"
            "&X-Amz-Date=20260430T150000Z"
            "&X-Amz-Expires=3600"
            "&X-Amz-SignedHeaders=host"
            "&X-Amz-Signature=abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        )
        warnings = scan_exfiltration_urls(f"Link: {url}")
        assert len(warnings) > 0, "Exfil data in credential scope should be flagged"

    def test_presigned_url_with_fake_security_token_redacted(self) -> None:
        """Non-STS payload in Security-Token is caught by structural validation."""
        from kiro_crew.security import scan_exfiltration_urls

        url = (
            "https://evil.s3.us-east-1.amazonaws.com/out.csv"
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=ASIA"
            "QWERTYUIOP123456%2F20260430%2Fus-east-1%2Fs3%2Faws4_request"
            "&X-Amz-Date=20260430T150000Z"
            "&X-Amz-Expires=3600"
            "&X-Amz-SignedHeaders=host"
            "&X-Amz-Signature=abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
            "&X-Amz-Security-Token=xoxb-"
            "1234567890-abcdefghijklmnop"
        )
        warnings = scan_exfiltration_urls(f"Link: {url}")
        assert len(warnings) > 0, "Non-STS token in Security-Token should be flagged"


class TestExfilUrlPathAndRawIp:
    """Secrets embedded in the URL PATH (no ``?``) and raw-IP /
    IPv6 literal hosts must be scanned/redacted — both otherwise bypass
    scan_exfiltration_urls (query-only scan + letter-TLD-only host regex)."""

    def test_credential_in_path_no_query_flagged(self) -> None:
        # A secret in the path with NO query string was skipped entirely before.
        text = "exfil to http://evil.com/upload/AKIAIOSFODNN7EXAMPLE/x"
        assert scan_exfiltration_urls(text), "path-embedded AWS key must be flagged"
        result, warnings = redact_exfiltration_urls(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert warnings

    def test_raw_ipv4_host_scanned(self) -> None:
        # A raw-IP host (incl. IMDS 169.254.169.254) never matched _URL_RE before.
        text = "curl http://169.254.169.254/AKIAIOSFODNN7EXAMPLE"
        assert scan_exfiltration_urls(text), "raw-IPv4 host with secret must be flagged"

    def test_raw_ipv4_query_secret_scanned(self) -> None:
        text = "http://192.168.1.5/collect?k=AKIAIOSFODNN7EXAMPLE"
        assert scan_exfiltration_urls(text)

    def test_bracketed_ipv6_host_scanned(self) -> None:
        text = "http://[fd00::1]/x/hook/xoxb-123456789-abcdefghij"
        assert scan_exfiltration_urls(text), "IPv6-literal host with token must be flagged"

    def test_ipv4_mapped_ipv6_imds_host_scanned(self) -> None:
        # IPv4-mapped IPv6 literal (dotted-quad suffix) must match _URL_RE — a
        # concrete IMDS bypass otherwise.
        text = "curl http://[::ffff:169.254.169.254]/latest/AKIAIOSFODNN7EXAMPLE"
        assert scan_exfiltration_urls(text), "IPv4-mapped IPv6 IMDS host must be flagged"

    def test_slack_token_in_path_flagged(self) -> None:
        assert scan_exfiltration_urls("http://evil.io/hook/xoxb-123456789-abcdefghij")

    def test_benign_base64_path_not_flagged(self) -> None:
        # A long base64-ish PATH segment (CDN asset id, git object hash) has no
        # hard-credential marker and must NOT be flagged — the blob/length
        # heuristics stay query-only to avoid this false positive.
        for text in [
            "https://cdn.example.com/a/aGVsbG93b3JsZGZvb2JhcmJhemJsYWgxMjM0NTY3ODkw.js",
            "https://github.com/o/r/blob/da39a3ee5e6b4b0d3255bfef95601890afd80709/f.py",
            "https://example.com/docs/page?id=42",
        ]:
            assert not scan_exfiltration_urls(text), text

    def test_s3_presigned_still_exempt(self) -> None:
        # The path-scan must not break the S3-presigned exemption (AKIA lives in
        # X-Amz-Credential legitimately).
        url = (
            "https://my-bucket.s3.amazonaws.com/key?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20260714%2Fus-east-1%2Fs3%2Faws4_request"
            "&X-Amz-Date=20260714T000000Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host"
            "&X-Amz-Signature=" + "a" * 64
        )
        result, _ = redact_exfiltration_urls(url)
        assert "REDACTED" not in result

    # ── Query directly after host, with NO path segment ──
    # _URL_RE's third group only matched a path/query beginning with "/", so a
    # URL of the form ``https://host?query`` (query, no path) yielded group(3)=
    # None. Both scan_exfiltration_urls and redact_exfiltration_urls then bailed
    # on ``qmark == -1`` and never inspected the query — a real exfil bypass.

    def test_credential_in_query_no_path_flagged(self) -> None:
        # AWS key in a query with no path segment must be flagged + redacted.
        text = "leak via https://attacker.io?leak=AKIAIOSFODNN7EXAMPLE"
        assert scan_exfiltration_urls(text), "host?query AWS key must be flagged"
        result, warnings = redact_exfiltration_urls(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert warnings

    def test_long_query_no_path_flagged(self) -> None:
        # A long (>=200 char) query with no path segment must trip the length
        # heuristic just like the ``/path?query`` form does.
        text = "https://attacker.io?d=" + "A" * 250
        assert scan_exfiltration_urls(text), "host?<long query> must be flagged"
        result, warnings = redact_exfiltration_urls(text)
        assert "[REDACTED" in result
        assert warnings

    def test_short_query_no_path_not_flagged(self) -> None:
        # A benign short query with no path must NOT be flagged (no regression
        # to the existing short-query behaviour when the "/" is absent).
        text = "open https://example.com?id=42&tab=logs"
        assert not scan_exfiltration_urls(text), text
        result, warnings = redact_exfiltration_urls(text)
        assert "[REDACTED" not in result
        assert not warnings


class TestExfilExactHostExemption:
    """Exact-host heuristic exemption for exfiltration redaction (CredentialPolicy).

    A companion CredentialPolicy may supply a set of EXACT trusted-tenant hosts
    whose URLs skip ONLY the base64-blob / query-length heuristics (which
    false-positive on legitimate long base64 document pointers).  The
    hard-credential floor (S3-presigned fast-path + unconditional
    ``_HARD_CREDENTIAL_RE`` path+query scan) is UNCONDITIONAL — an exempted host
    with a real AWS key / bare secret / token is still redacted.

    NEUTRAL PLACEHOLDER HOSTS ONLY — the companion's real tenant host list never
    appears in the public repo (it is companion CredentialPolicy adapter data).
    """

    # Placeholder trusted-tenant hosts (no real tenant names).
    _EXEMPT = frozenset({"contoso.sharepoint.com", "trusted.example.com"})

    class _StubCredentialPolicy:
        """CredentialPolicy stub exposing a caller-supplied exempt-host set."""

        def __init__(self, hosts: "frozenset[str]"):
            self._hosts = hosts

        def redact(self, text: str) -> str:
            from kiro_crew.security import redact

            return redact(text)

        def exempt_exact_hosts(self) -> "frozenset[str]":
            return self._hosts

    def _install_exempt_hosts(self, hosts: "frozenset[str]") -> None:
        import dataclasses

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.platform.bootstrap import build_default_context
        from kiro_crew.platform.context import set_context

        base = build_default_context(KiroCrewConfig())
        stub = self._StubCredentialPolicy(hosts)
        set_context(dataclasses.replace(base, credentials=stub))

    def _long_nav_url(self, host: str) -> str:
        """URL with a long base64 ``nav=`` pointer (>200 char query).

        This trips BOTH the query-length heuristic and the base64-blob pattern —
        exactly what an exact-host exemption is meant to skip.
        """
        url = (
            f"https://{host}/:fl:/r/contentstorage/CSP_x/Document%20Library/"
            "AppData/doc.loop?d=wabc&csf=1&web=1&e=ABCdef&nav=eyJ" + "A" * 220
        )
        assert len(url.split("?", 1)[1]) >= 200  # confirm query > threshold
        return url

    def test_default_context_redacts_long_query(self) -> None:
        """Standalone default (empty exempt set) still redacts the long nav URL.

        Byte-identical to today: with no exemptions every host runs the
        heuristics, so a long base64 query is redacted regardless of host.
        """
        from kiro_crew.security import redact_exfiltration_urls

        url = self._long_nav_url("contoso.sharepoint.com")
        result, warnings = redact_exfiltration_urls(f"Doc: {url}")
        assert "[REDACTED" in result
        assert len(warnings) == 1

    def test_exempted_host_long_query_preserved(self) -> None:
        """An exact-member host's long base64 nav URL is NOT redacted."""
        from kiro_crew.security import redact_exfiltration_urls

        self._install_exempt_hosts(self._EXEMPT)
        url = self._long_nav_url("contoso.sharepoint.com")
        result, warnings = redact_exfiltration_urls(f"Doc: {url}")
        assert "[REDACTED" not in result
        assert len(warnings) == 0

    def test_second_exempted_host_preserved(self) -> None:
        """A different exact-member host is also exempt (whole set honored)."""
        from kiro_crew.security import redact_exfiltration_urls

        self._install_exempt_hosts(self._EXEMPT)
        url = self._long_nav_url("trusted.example.com")
        result, warnings = redact_exfiltration_urls(f"Doc: {url}")
        assert "[REDACTED" not in result
        assert len(warnings) == 0

    def test_exempted_host_scan_clean(self) -> None:
        """scan_exfiltration_urls returns no warnings for an exempted host URL."""
        from kiro_crew.security import scan_exfiltration_urls

        self._install_exempt_hosts(self._EXEMPT)
        url = self._long_nav_url("contoso.sharepoint.com")
        assert len(scan_exfiltration_urls(f"Doc: {url}")) == 0

    def test_mixed_case_exempted_host_preserved(self) -> None:
        """Hostnames are case-insensitive — a mixed-case host (as Office apps
        emit, e.g. ``Contoso.SharePoint.com``) whose lowercase form is in the
        exempt set is NOT redacted. Guards against a case-sensitive ``in`` check
        that would wrongly redact a legitimate document pointer."""
        from kiro_crew.security import redact_exfiltration_urls, scan_exfiltration_urls

        self._install_exempt_hosts(self._EXEMPT)
        url = self._long_nav_url("Contoso.SharePoint.com")
        assert len(scan_exfiltration_urls(f"Doc: {url}")) == 0
        result, warnings = redact_exfiltration_urls(f"Doc: {url}")
        assert "[REDACTED" not in result
        assert len(warnings) == 0

    def test_mixed_case_exempt_member_preserved(self) -> None:
        """Symmetric to the above: a mixed-case MEMBER of the exempt set still
        matches a lowercase host (both sides normalized to lowercase)."""
        from kiro_crew.security import redact_exfiltration_urls

        self._install_exempt_hosts(frozenset({"Contoso.SharePoint.com"}))
        url = self._long_nav_url("contoso.sharepoint.com")
        result, warnings = redact_exfiltration_urls(f"Doc: {url}")
        assert "[REDACTED" not in result
        assert len(warnings) == 0

    def test_exempted_host_percent_encoding_still_redacted(self) -> None:
        """The heavy percent-encoding detector is NOT part of the exempted
        base64/length heuristics — a URL-encoded payload to an exempted host is
        still flagged and redacted."""
        from kiro_crew.security import (
            _EXFIL_QUERY_MIN_LEN,
            redact_exfiltration_urls,
            scan_exfiltration_urls,
        )

        self._install_exempt_hosts(self._EXEMPT)
        # 25 consecutive percent-encoded octets (>20) trips _EXFIL_PERCENT_RE
        # but the short query does NOT trip the length heuristic.
        url = "https://contoso.sharepoint.com/doc?p=" + "%41" * 25
        assert len(url.split("?", 1)[1]) < _EXFIL_QUERY_MIN_LEN
        assert scan_exfiltration_urls(f"Doc: {url}")
        result, warnings = redact_exfiltration_urls(f"Doc: {url}")
        assert "[REDACTED" in result
        assert len(warnings) == 1

    def test_non_exempted_tenant_still_redacted(self) -> None:
        """A non-member host is NOT exempt (exact match only, not suffix)."""
        from kiro_crew.security import redact_exfiltration_urls

        self._install_exempt_hosts(self._EXEMPT)
        # Same registrable domain family, different subdomain — must NOT match.
        url = self._long_nav_url("attacker.sharepoint.com")
        result, warnings = redact_exfiltration_urls(f"Doc: {url}")
        assert "[REDACTED" in result
        assert len(warnings) == 1

    def test_exempted_host_credential_query_still_redacted(self) -> None:
        """A hard AWS key in the QUERY on an exempted host is still redacted."""
        from kiro_crew.security import redact_exfiltration_urls

        self._install_exempt_hosts(self._EXEMPT)
        url = "https://contoso.sharepoint.com/doc?key=AKIAIOSFODNN7EXAMPLE1234"
        result, warnings = redact_exfiltration_urls(f"Doc: {url}")
        assert "[REDACTED" in result
        assert len(warnings) == 1

    def test_exempted_host_akia_in_path_still_redacted(self) -> None:
        """BINDING: an exempted host with an AKIA key in the URL PATH is still
        redacted — the exemption narrows only the heuristics, never the
        unconditional path+query hard-credential floor."""
        from kiro_crew.security import redact_exfiltration_urls, scan_exfiltration_urls

        self._install_exempt_hosts(self._EXEMPT)
        url = "https://contoso.sharepoint.com/upload/AKIAIOSFODNN7EXAMPLE/report"
        assert scan_exfiltration_urls(f"Doc: {url}")
        result, warnings = redact_exfiltration_urls(f"Doc: {url}")
        assert "[REDACTED" in result
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert len(warnings) == 1

    def test_exempted_host_base64_encoded_credential_still_flagged(self) -> None:
        """A hard credential base64-ENCODED into the query on an EXEMPT host is
        still flagged: the unconditional decode-and-scan runs for every host, so
        an encoded AWS key can't ride the exemption out (the raw hard-credential
        regex would miss the encoded form, and the raw base64-blob heuristic is
        skipped for exempt hosts — decode-and-scan closes that gap)."""
        import base64

        from kiro_crew.security import scan_exfiltration_urls

        self._install_exempt_hosts(self._EXEMPT)
        # An AWS key wrapped in base64 — the raw AKIA regex won't see it, and the
        # host is exempt from the raw blob heuristic; only decode-and-scan catches it.
        blob = base64.b64encode(b"AKIAIOSFODNN7EXAMPLE secret payload").decode()
        url = f"https://contoso.sharepoint.com/doc?d={blob}"
        assert scan_exfiltration_urls(f"Doc: {url}")

    def test_exempted_host_base64_document_still_exempt(self) -> None:
        """A legitimate base64 DOCUMENT pointer (decodes to printable non-credential
        text) on an exempt host is still exempt — decode-and-scan only fires on
        an encoded credential, so the false-positive the exemption exists to avoid
        stays avoided."""
        import base64

        from kiro_crew.security import scan_exfiltration_urls

        self._install_exempt_hosts(self._EXEMPT)
        # 60+ char base64 of plain readable text: trips the raw blob heuristic
        # (which is exempted) but decodes to a non-credential document → clean.
        blob = base64.b64encode(b"the quick brown fox jumps over the lazy dog again").decode()
        url = f"https://contoso.sharepoint.com/doc?ref={blob}"
        assert scan_exfiltration_urls(f"Doc: {url}") == []

    def test_exempted_host_bare_secret_value_redacted(self) -> None:
        """A bare ``SecretAccessKey=<base64>`` value (no AKIA prefix) on an
        exempted host is redacted at the URL level, not silently skipped."""
        from kiro_crew.security import redact_exfiltration_urls

        self._install_exempt_hosts(self._EXEMPT)
        secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        url = f"https://trusted.example.com/doc?SecretAccessKey={secret}"
        result, warnings = redact_exfiltration_urls(f"Doc: {url}")
        assert "[REDACTED" in result
        assert secret not in result
        assert len(warnings) == 1

    def test_unbooted_path_does_no_context_resolution(self) -> None:
        """The unbooted path must not RESOLVE a context -- not even once.

        ``current_context()`` loads config and discovers plugin entry points
        before it decides, and on a non-standalone profile it never memoizes its
        fail-closed verdict, so a per-line caller (``_pump_stderr`` redacting
        backend stderr) would re-pay that synchronous I/O for every single line
        on the gateway event loop.  Pin that this lookup never reaches it: the
        answer for "no context installed" is the same empty set the standalone
        default would give, so resolving is pure cost.
        """
        import pytest as _pytest

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.platform import context as context_mod
        from kiro_crew.platform.context import reset_context
        from kiro_crew.security import redact

        calls: list[str] = []
        real_current = context_mod.current_context
        real_load = KiroCrewConfig.load

        with _pytest.MonkeyPatch.context() as mp:
            mp.setenv("KIROCREW_PROFILE", "enterprise")
            reset_context()

            def _spy_current():  # type: ignore[no-untyped-def]
                calls.append("current_context")
                return real_current()

            def _spy_load(*a, **k):  # type: ignore[no-untyped-def]
                calls.append("config_load")
                return real_load(*a, **k)

            mp.setattr(context_mod, "current_context", _spy_current)
            mp.setattr(KiroCrewConfig, "load", _spy_load)
            try:
                # Redact many lines, as a stderr drain would.
                for _ in range(25):
                    redact("boot line https://example.com/mcp")
                assert calls == [], f"unbooted path resolved a context: {calls}"
            finally:
                reset_context()

    def test_composition_error_degrades_to_full_redaction(self) -> None:
        """PlatformCompositionError from the adapter degrades to the empty set =
        full redaction, and MUST NOT propagate: this lookup can only ever RELAX
        the heuristics, so the empty set is already the strictest answer.
        Propagation aborted the calling operation -- every pooled MCP
        backend spawn in gatewayd died building its own log line."""
        import dataclasses

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.platform.bootstrap import build_default_context
        from kiro_crew.platform.context import PlatformCompositionError, set_context
        from kiro_crew.security import redact_exfiltration_urls

        class _RaisingCredentialPolicy(self._StubCredentialPolicy):
            def exempt_exact_hosts(self) -> "frozenset[str]":
                raise PlatformCompositionError("no companion")

        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, credentials=_RaisingCredentialPolicy(frozenset())))
        url = self._long_nav_url("contoso.sharepoint.com")
        result, warnings = redact_exfiltration_urls(f"Doc: {url}")
        assert "[REDACTED" in result
        assert len(warnings) == 1

    def test_unbooted_nonstandalone_profile_still_redacts(self) -> None:
        """``redact()`` in an UNBOOTED worker under a
        non-standalone profile must not raise.

        ``gatewayd`` never installs a ``PlatformContext``; under
        ``KIROCREW_PROFILE=enterprise`` ``current_context()`` fail-closes, and
        the exempt-host lookup inside ``redact()`` must not propagate that error,
        killing every pooled MCP backend spawn while it built the spawn log
        line.  The lookup must degrade to the empty set (maximum redaction)
        instead: the log line is still fully redacted, the operation survives.
        """
        import pytest as _pytest

        from kiro_crew.platform.context import (
            PlatformCompositionError,
            current_context,
            reset_context,
        )
        from kiro_crew.security import redact

        with _pytest.MonkeyPatch.context() as mp:
            mp.setenv("KIROCREW_PROFILE", "enterprise")
            reset_context()
            try:
                # Precondition: the context itself still fail-closes (that
                # contract is unchanged; only the exempt-host lookup degrades).
                with _pytest.raises(PlatformCompositionError):
                    current_context()
                # The gatewayd spawn-log call shape: must not raise. Compare the
                # WHOLE line rather than asking whether it contains the host --
                # equality proves nothing was redacted away, and a bare host
                # substring test is the incomplete-URL-sanitization pattern.
                line = "cmd --flag https://example.com"
                assert redact(line) == line
                # Heuristic-tripping URL is still redacted (empty exempt set =
                # maximum strictness, never fail-open).
                url = self._long_nav_url("contoso.sharepoint.com")
                assert "[REDACTED" in redact(f"Doc: {url}")
            finally:
                reset_context()

    def test_adapter_failure_degrades_to_full_redaction(self) -> None:
        """A transient (non-composition) adapter failure degrades to the empty
        set = MORE redaction (the safe direction), never fewer exemptions."""
        import dataclasses

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.platform.bootstrap import build_default_context
        from kiro_crew.platform.context import set_context
        from kiro_crew.security import redact_exfiltration_urls

        class _BrokenCredentialPolicy(self._StubCredentialPolicy):
            def exempt_exact_hosts(self) -> "frozenset[str]":
                raise RuntimeError("adapter broke")

        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, credentials=_BrokenCredentialPolicy(frozenset())))
        url = self._long_nav_url("contoso.sharepoint.com")
        result, warnings = redact_exfiltration_urls(f"Doc: {url}")
        assert "[REDACTED" in result
        assert len(warnings) == 1

    def test_pre_method_adapter_degrades_to_empty(self) -> None:
        """A pre-method companion adapter (no ``exempt_exact_hosts``) degrades to
        the empty set via getattr rather than raising — full redaction stands."""
        import dataclasses

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.platform.bootstrap import build_default_context
        from kiro_crew.platform.context import set_context
        from kiro_crew.security import redact_exfiltration_urls

        class _LegacyCredentialPolicy:
            def redact(self, text: str) -> str:
                return text

        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, credentials=_LegacyCredentialPolicy()))
        url = self._long_nav_url("contoso.sharepoint.com")
        result, warnings = redact_exfiltration_urls(f"Doc: {url}")
        assert "[REDACTED" in result
        assert len(warnings) == 1


class TestKiroCrewSlackAppCreateLink:
    """Kiro Crew's OWN Slack app-create deep link survives the exfil redactor.

    ``kirocrew manifest --url`` and ``GET /api/slack/manifest`` emit
    ``https://api.slack.com/apps?new_app=1&manifest_yaml=<encoded manifest>``.
    The encoded manifest is ~1.9 KB, so the aggregate query-length heuristic
    classified the whole link as exfiltration and the user was shown
    ``[REDACTED: suspicious URL to api.slack.com]`` instead of the link the
    setup guide tells them to click.

    The exemption is granted by VALIDATION, not by destination: the payload must
    reproduce the bundled template rendered with one alias. Every test below that
    perturbs the link asserts it goes back to being redacted, because the value
    of this carve-out is precisely that it cannot carry anything else.
    """

    def _payload(self, alias: str = "someone") -> str:
        """The deep-link payload as the REAL emitters build it."""
        from kiro_crew import slack_manifest

        return slack_manifest.render(alias, strip_comments=True)

    def _link(self, alias: str = "someone", **over: str) -> str:
        from urllib.parse import quote

        from kiro_crew import slack_manifest

        if not over:
            # Default case goes through the actual emitter, so a change to its
            # render/strip/encode procedure fails HERE rather than silently
            # reintroducing the redaction bug for users.
            return slack_manifest.deep_link(alias)
        payload = over.get("payload", self._payload(alias))
        scheme = over.get("scheme", "https")
        host = over.get("host", "api.slack.com")
        path = over.get("path", "/apps")
        new_app = over.get("new_app", "1")
        extra = over.get("extra", "")
        return (
            f"{scheme}://{host}{path}?new_app={new_app}"
            f"&manifest_yaml={quote(payload, safe='')}{extra}"
        )

    def test_the_real_emitters_produce_an_unredacted_link(self) -> None:
        """Both emitted links pass — driven through the emitters, not a rebuild.

        Design review calls this out: rebuilding the payload inside
        the test would let an emitter drift away from the validator with the tests
        still green, which is the same "no test exercised the real URL" failure
        that hid the original bug.
        """
        from kiro_crew import slack_manifest
        from kiro_crew.security import redact_exfiltration_urls, scan_exfiltration_urls

        url = slack_manifest.deep_link("someone")
        assert len(url.split("?", 1)[1]) >= 200  # premise: over the threshold
        assert scan_exfiltration_urls(url) == []
        assert redact_exfiltration_urls(url)[0] == url

    def test_manifest_link_is_not_redacted(self) -> None:
        """The real emitted link passes the general text scanner untouched."""
        from kiro_crew.security import redact_exfiltration_urls, scan_exfiltration_urls

        url = self._link()
        assert len(url.split("?", 1)[1]) >= 200
        assert scan_exfiltration_urls(url) == []
        cleaned, warnings = redact_exfiltration_urls(url)
        assert cleaned == url
        assert warnings == []

    def test_alias_shapes_accepted(self) -> None:
        """Any alias the emitters permit (alnum, hyphen, underscore) is accepted."""
        from kiro_crew.security import scan_exfiltration_urls

        for alias in ("a", "user99", "first-last", "with_underscore", "A1_b-2"):
            assert scan_exfiltration_urls(self._link(alias)) == [], alias

    def test_secret_shaped_alias_is_still_redacted(self) -> None:
        """A credential parked in the alias slot does NOT ride through.

        Guards the blocking finding: the exemption once zeroed
        the heuristic payload, and the alias slot accepted 64 chars of
        `[A-Za-z0-9_-]` — wide enough for a 40-char alphanumeric secret, which is
        exactly the run length `_EXFIL_PATTERNS` needs to fire. Two independent
        guards now cover it: `ALIAS_MAX` makes a 40-char run impossible, and the
        alias that does fit stays under the heuristics.
        """
        from urllib.parse import quote

        from kiro_crew import slack_manifest
        from kiro_crew.security import scan_exfiltration_urls

        # Over ALIAS_MAX — the derived pattern refuses it, so no exemption.
        secret40 = "wJalrXUtnFEMIK7MDENG" "bPxRfiCYEXAMPLEKEYXY"
        assert len(secret40) == 40 > slack_manifest.ALIAS_MAX
        payload = slack_manifest.stripped_template().replace(
            slack_manifest.ALIAS_PLACEHOLDER, secret40
        )
        url = "https://api.slack.com/apps?new_app=1&manifest_yaml=" + quote(payload, safe="")
        assert scan_exfiltration_urls(url) != []

        # Within ALIAS_MAX but a recognised credential shape — caught on the
        # alias itself, because the alias is what the heuristics still see.
        for hostile in ("AKIAIOSFODNN7EXAMPLE", "xoxb-123456789012-abcdef"):
            assert len(hostile) <= slack_manifest.ALIAS_MAX, hostile
            assert scan_exfiltration_urls(self._link(hostile)) != [], hostile

    def test_mismatched_aliases_redacted(self) -> None:
        """The manifest names the alias twice; they must be the SAME alias."""
        from kiro_crew import slack_manifest
        from kiro_crew.security import scan_exfiltration_urls

        tampered = (
            slack_manifest.stripped_template()
            .replace(slack_manifest.ALIAS_PLACEHOLDER, "real", 1)
            .replace(slack_manifest.ALIAS_PLACEHOLDER, "other")
        )
        assert scan_exfiltration_urls(self._link(payload=tampered)) != []

    def test_arbitrary_payload_redacted(self) -> None:
        """A long payload that is not the template stays redacted."""
        from kiro_crew.security import scan_exfiltration_urls

        assert scan_exfiltration_urls(self._link(payload="x" * 900)) != []

    def test_credential_in_payload_still_redacted(self) -> None:
        """A secret appended to an otherwise-valid manifest is still caught.

        The unconditional hard-credential scan runs BEFORE the heuristic-query
        selection, so the carve-out cannot shield a credential even at the
        approved endpoint.
        """
        from kiro_crew.security import scan_exfiltration_urls

        payload = self._payload("someone") + "\nAKIAIOSFODNN7EXAMPLE\n"
        warnings = scan_exfiltration_urls(self._link(payload=payload))
        assert warnings != []
        assert "credential" in warnings[0]

    def test_extra_parameter_redacted(self) -> None:
        """An extra query parameter refuses the exemption (exact param set)."""
        from kiro_crew.security import scan_exfiltration_urls

        assert scan_exfiltration_urls(self._link(extra="&exfil=" + "z" * 300)) != []

    def test_tampered_new_app_redacted(self) -> None:
        """``new_app`` must be exactly ``1``."""
        from kiro_crew.security import scan_exfiltration_urls

        assert scan_exfiltration_urls(self._link(new_app="2")) != []

    def test_neighbouring_endpoints_redacted(self) -> None:
        """Only the exact https host+path is eligible — no scheme/host/path drift."""
        from kiro_crew.security import scan_exfiltration_urls

        assert scan_exfiltration_urls(self._link(scheme="http")) != []
        assert scan_exfiltration_urls(self._link(path="/apps2")) != []
        assert scan_exfiltration_urls(self._link(host="api.slack.com.evil.example")) != []
        assert scan_exfiltration_urls(self._link(host="api.slack.com:8443")) != []

    def test_unrelated_slack_url_unaffected(self) -> None:
        """A long-query URL at the same host but another path stays redacted.

        Guards the documented invariant that query-length detection has no host
        allowlist: this carve-out keys on a validated payload, not on Slack.
        """
        from kiro_crew.security import scan_exfiltration_urls

        url = "https://api.slack.com/api/chat.postMessage?blob=" + "A" * 250
        assert scan_exfiltration_urls(url) != []

    def test_unreadable_template_fails_closed(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """If the packaged template cannot be read, the link is redacted again.

        Failing closed matters more than the convenience: an install that cannot
        prove what its own manifest looks like must not exempt a 1.9 KB payload.
        """
        import kiro_crew.security as sec

        url = self._link()
        monkeypatch.setattr(sec, "_slack_manifest_re_slot", [None])
        assert sec.scan_exfiltration_urls(url) != []


class TestDashboardLinkTokenAcrossHostForms:
    """A dashboard access token is redacted whatever host form carries it.

    This pins the OUTCOME, not the mechanism, because the mechanism today is an
    accident worth insulating against. `_URL_RE` requires a dot plus a letter
    TLD, so a bare `localhost` URL is never matched by the URL scanner at all,
    while `127.0.0.1` (raw IPv4) and a dotted host (a dev desktop, a tailnet
    name) ARE. Nobody chose that split for dashboard links — it falls out of the
    host pattern — so `redact_credentials` is what must catch the token on every
    form, and that is what these assertions hold to.

    Two ways this could regress silently: `_URL_RE` grows to match `localhost`
    (the exfil path starts firing on loopback URLs), or the credential patterns
    narrow (the token stops being caught where the URL scanner never looked).
    The token shape mirrors `dashboard.token_auth.generate_token` —
    `base64url(payload).base64url(hmac)`, i.e. TWO segments, which is the case
    that falls through to the bare-secret heuristic and survives ~74%
    of the time (see the link-token alternative in `_CREDENTIAL_PATTERNS`).
    """

    # 43 chars is exactly HMAC-SHA256 base64url-unpadded, per token_auth._sign.
    _TOKEN = "eyJ" + "a" * 180 + "." + "b" * 43

    HOST_FORMS = (
        "localhost:7778",
        "127.0.0.1:7778",
        "dev-dsk-someone.example.com:7778",
        "host.tail1234.ts.net",
    )

    def test_token_is_redacted_on_every_host_form(self) -> None:
        from kiro_crew.security import redact_credentials

        for host in self.HOST_FORMS:
            cleaned, _ = redact_credentials(f"http://{host}/?token={self._TOKEN}")
            assert self._TOKEN not in cleaned, host
            # The signature must not survive on its own either — a URL that still
            # looks complete but does not authenticate is the failure mode the
            # two-segment alternative was added for.
            assert "b" * 43 not in cleaned, host

    def test_localhost_is_invisible_to_the_url_scanner(self) -> None:
        """Documents the dot-TLD accident so a change to it is a loud diff.

        Not an endorsement: if `_URL_RE` later matches `localhost`, this test
        fails and whoever changed it gets to confirm the credential path still
        covers loopback links (the test above) rather than discovering later that
        redaction depended on the host pattern.
        """
        from kiro_crew.security import scan_exfiltration_urls

        assert scan_exfiltration_urls(f"http://localhost:7778/?token={self._TOKEN}") == []
        assert scan_exfiltration_urls(f"http://127.0.0.1:7778/?token={self._TOKEN}") != []


_ISSUE_HOST = "github.com"
_ISSUE_PATH = "/kirodotdev/KiroCrew/issues/new"
# Percent-encoded prose, deliberately over _EXFIL_QUERY_MIN_LEN so the LENGTH
# signal is the one in play, and with no 40-char run in [A-Za-z0-9+/=] and no 20
# consecutive octets so no PATTERN signal fires. Both properties are ASSERTED by
# the two guard tests below rather than assumed: a fixture that tripped a pattern
# would make the positive case pass for the wrong reason, and one under 200 chars
# would make it pass without exercising the carve-out at all.
_ISSUE_QUERY = (
    "title=Narrow%20the%20suspicious%20URL%20heuristic"
    "&body=The%20aggregate%20query%20length%20check%20fires%20on%20ordinary%20links"
    "%20and%20drops%20them%20from%20the%20rendered%20message%20so%20they%20cannot"
    "%20be%20clicked%20or%20copied&labels=bug"
)


def _issue_link(
    *,
    scheme: str = "https",
    host: str = _ISSUE_HOST,
    port: str = "",
    path: str = _ISSUE_PATH,
    query: str = _ISSUE_QUERY,
) -> str:
    return f"{scheme}://{host}{port}{path}?{query}"


class TestPrefilledGitHubIssueUrl:
    """A model-authored GitHub issue-prefill link IS redacted — no shape earns a waiver.

    An earlier version pinned the opposite. Two waivers were tried and both
    removed: one keyed to the prefill SHAPE, one additionally pinned to this
    project's own tracker. Both are exfiltration primitives, because what
    ``redact_exfiltration_urls`` sanitizes is MODEL-AUTHORED text —

      injected content steers the model into emitting a prefill URL whose ``body``
      carries percent-encoded private context; the waiver skips aggregate query
      length; the link renders as the familiar "file an issue" affordance; the user
      submits it; and the issue is PUBLIC, so the attacker reads it.

    Pinning the repository does not close that, because this project's tracker is
    world-readable by design. A URL's shape says nothing about who authored it, and
    an in-band marker travels in the channel the injection controls — so provenance
    has to come from a different channel. It already does: ``diagnostics._issue_url``
    assembles the prefill link from STRUCTURED fields and the dashboard renders its
    own anchor from ``BundleResult.github_issue_url``, a JSON field no redactor
    scans. ``TestTrustedIssueLinkChannel`` below pins that seam.
    """

    def _assert_redacted(self, url: str) -> None:
        cleaned, warnings = redact_exfiltration_urls(f"see {url} for detail")
        assert "[REDACTED: suspicious URL to" in cleaned, cleaned
        assert url not in cleaned, cleaned
        assert warnings

    # ── guards: keep the positive case from passing for the wrong reason ──

    def test_the_fixture_query_is_long_enough_to_reach_the_length_gate(self) -> None:
        """Under _EXFIL_QUERY_MIN_LEN the case would pass for the wrong reason."""
        assert len(_ISSUE_QUERY) >= security._EXFIL_QUERY_MIN_LEN

    def test_no_pattern_signal_fires_on_the_fixture(self) -> None:
        """The fixture must isolate LENGTH: no base64 run, no percent run.

        Without this the URL would be redacted by a pattern rule and the test would
        say nothing about the length gate, which is the rule the waivers waived.
        """
        assert not security._EXFIL_PERCENT_RE.search(_ISSUE_QUERY)
        assert (
            max((len(m) for m in re.findall(r"[A-Za-z0-9+/=]{40,}", _ISSUE_QUERY)), default=0) == 0
        )

    # ── the exfiltration case, and it is the CANONICAL tracker ──

    def test_a_prefill_link_to_this_projects_own_tracker_is_redacted(self) -> None:
        """The finding that removed the second waiver, pinned as a regression.

        This URL is maximally trustworthy by shape AND by destination: exact
        ``https``, host exactly ``github.com``, no port, the path is literally this
        repository's ``issues/new``, and every query key is one GitHub documents.
        It is still redacted, because none of that establishes that the model was
        not steered into emitting it, and a submitted issue here is public.
        """
        self._assert_redacted(_issue_link())

    def test_a_prefill_link_to_an_attacker_owned_repository_is_redacted(self) -> None:
        self._assert_redacted(_issue_link(path="/attacker/exfil-sink/issues/new"))

    def test_a_cased_host_does_not_change_the_verdict(self) -> None:
        """RFC 4343 leaves DNS case insignificant; with no waiver it changes nothing."""
        self._assert_redacted(_issue_link(host="GitHub.com"))

    # ── the other signals are independent of this change and must still fire ──

    def test_a_credential_in_a_documented_parameter_is_redacted(self) -> None:
        """The unconditional credential floor is unchanged by removing the waiver."""
        self._assert_redacted(_issue_link(query=f"{_ISSUE_QUERY}%20AKIAIOSFODNN7EXAMPLE"))

    def test_a_base64_blob_in_a_documented_parameter_is_redacted(self) -> None:
        blob = "A" * 30 + "b3Rvb2xvbmdibG9i"
        self._assert_redacted(_issue_link(query=f"title=x&body={blob}&labels=bug"))

    def test_heavy_percent_encoding_in_a_documented_parameter_is_redacted(self) -> None:
        self._assert_redacted(_issue_link(query=f"title=x&body={'%41' * 21}&labels=bug"))

    def test_the_length_gate_is_the_rule_that_fires(self) -> None:
        """Names the RULE, so a future waiver cannot pass this class by accident.

        The three tests above would still pass if the length gate were waived and a
        pattern rule caught the URL instead. This one asserts the classification
        came from ``exfil_query_length`` on a fixture that trips nothing else.
        """
        rules: list[str] = []
        assert (
            security._exfil_url_warning(
                _ISSUE_HOST,
                f"{_ISSUE_PATH}?{_ISSUE_QUERY}",
                frozenset(),
                _rule_out=rules,
            )
            is not None
        )
        assert rules == ["exfil_query_length"]

    def test_the_predicate_takes_no_waiver_parameter(self) -> None:
        """A reintroduced escape hatch fails here even if every case above is kept."""
        import inspect

        params = inspect.signature(security._exfil_url_warning).parameters
        assert "allow_prefilled_issue" not in params
        assert not hasattr(security, "_is_prefilled_issue_url")

    # ── the authentication admission gate must not move ──

    def test_the_oauth_banner_gate_still_rejects_the_link(self) -> None:
        """``oauth_url_contains_credential`` ADMITS an OAuth banner URL.

        It shares this classifier, so it was the one call site the waiver was gated
        OFF for. With no waiver anywhere the gate needs no opt-out, and this pins
        that removing the plumbing did not loosen it.
        """
        assert oauth_url_contains_credential(_issue_link()) is True

    def test_a_generic_long_query_url_is_redacted_the_same_way(self) -> None:
        """The other host in the report. It gets the same verdict as the prefill link.

        Both were false positives in the report and both stay redacted: the fix for
        a long legitimate URL is to narrow this heuristic for every host on its own
        merits, not to carve out one shape.
        """
        self._assert_redacted(
            "https://metrics.example.com/metrics?namespace=AWS/SageMaker"
            "&metricName=Invocations&dimensions=EndpointName%3Dmy-endpoint"
            "&startTime=2026-09-01T00%3A00%3A00Z&endTime=2026-09-07T00%3A00%3A00Z"
            "&period=300&stat=Sum&region=us-west-2&accountId=123456789012&view=timeSeries"
        )


class TestTrustedIssueLinkChannel:
    """The prefilled link the report wanted, delivered without a redactor waiver.

    Provenance cannot be recovered from model prose, so it comes from a different
    channel: ``diagnostics._issue_url`` assembles the query from STRUCTURED fields
    and the dashboard renders its own anchor from the ``github_issue_url`` JSON
    field (``ReportProblemModal``, ``ReportProblemCard``). Nothing on that path is
    text a model wrote, and no redactor scans a JSON response body — which is why
    the link survives there while the same URL in chat does not.

    These tests pin the two halves of that claim that live in Python.
    """

    @pytest.fixture(autouse=True)
    # flake-ok: pre-existing class autouse stub that keeps the kiro-cli version probe off a spawn; moved verbatim
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse stub moved verbatim; no test in this class calls monkeypatch.undo()
    def _stub_host_probe(self, monkeypatch) -> None:
        """Keep the builder off the real `kiro-cli --version` subprocess.

        Every test here calls ``_issue_url`` or ``terminal_issue_url``, and both
        interpolate ``_kiro_cli_version()`` into the ``context`` field, which shells
        out to the installed binary. That makes the suite depend on whether kiro-cli
        is on the host and how long it takes to answer. Autouse rather than
        per-test so a case added later cannot reintroduce the spawn. Same stub
        ``test_diagnostics.py::_isolate`` uses.

        The other two host reads in that field are pure: ``platform.platform()`` is
        stdlib and ``beacon.distribution()`` reads a baked constant or an env var.
        """
        from kiro_crew import diagnostics

        monkeypatch.setattr(diagnostics, "_kiro_cli_version", lambda: "kiro-cli 2.14.2")

    def _bundle(self) -> object:
        from kiro_crew import diagnostics

        return diagnostics.BundleResult(
            zip_path=Path("/tmp/kirocrew-diagnostics-20260907.zip"),
            filename="kirocrew-diagnostics-20260907.zip",
            redaction_summary={"kirocrew.log": 3},
        )

    def test_the_trusted_builder_assembles_the_prefill_from_structured_fields(self) -> None:
        """Built by code from named fields, never parsed out of prose."""
        from kiro_crew import diagnostics

        url = diagnostics._issue_url(self._bundle(), "chat drops long links")
        assert url.startswith(f"https://github.com/{diagnostics._ISSUE_REPO}/issues/new?")
        assert "what-happened=chat%20drops%20long%20links" in url
        assert "context=" in url and "version=" in url
        # Also proves `_stub_host_probe` is actually wired: without it this reads the
        # real `kiro-cli --version`, so a passing suite would say nothing about
        # whether the spawn was avoided.
        assert "kiro-cli%202.14.2" in url

    def test_that_builders_output_is_what_the_dashboard_field_carries(self) -> None:
        """``github_issue_url`` is the prefilled variant, so the feature still works."""
        from kiro_crew import diagnostics

        result = self._bundle()
        result.github_issue_url = diagnostics._issue_url(result, "note")
        assert "context=" in result.as_dict()["github_issue_url"]

    def test_the_prefilled_variant_would_not_survive_model_prose(self) -> None:
        """The reason the trusted channel is needed rather than a waiver.

        The same URL the dashboard renders intact IS redacted once it travels as
        text, and that is now true with no exception. Pinned so nobody 'fixes' the
        asymmetry by reaching back into the redactor.
        """
        from kiro_crew import diagnostics

        url = diagnostics._issue_url(self._bundle(), "chat drops long links")
        cleaned, warnings = redact_exfiltration_urls(f"file it here: {url}")
        assert url not in cleaned
        assert warnings

    def test_the_terminal_variant_is_the_bounded_alternative_for_prose(self) -> None:
        """``terminal_issue_url`` drops the free-form fields so it survives unwaived.

        This is the shape the codebase already chose for paths that DO get relayed
        through prose, and it is why removing the waiver strands nothing.
        """
        from kiro_crew import diagnostics

        url = diagnostics.terminal_issue_url(self._bundle(), "note")
        cleaned, warnings = redact_exfiltration_urls(f"file it here: {url}")
        assert url in cleaned, cleaned
        assert warnings == []
