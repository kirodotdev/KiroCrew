"""Policy distribution: what the tier may say, log or hand an agent about its source.

The source URL can be the fleet's credential, and ``kirocrew policy fetch``, the log
ring and the dashboard are all reachable by the agent. So no audit label, redirect
refusal or re-raised boot error names the endpoint, a transport error that names only
the host is redacted too, text the endpoint authored is sanitised, the distribution
variables are denied to every agent subprocess, and the read-only posture reports the
scheme alone. The failure detail and the log line themselves are pinned by
``TestTheDetailNeverNamesTheEndpoint`` in ``test_governance_distribution.py``.

One topic of the ``test_governance_distribution*.py`` family; the constants, document
builders, fake fetchers and fixtures the family shares come from
``governance_distribution_helpers.py``. Nothing here touches the network either: see
``test_governance_distribution.py``'s docstring for how the transport is faked.
"""

from __future__ import annotations

import json
import logging
import urllib.error

import pytest
from governance_distribution_helpers import _hermetic_governance_globals  # noqa: F401
from governance_distribution_helpers import install_ceiling_fixture  # noqa: F401
from governance_distribution_helpers import transport_fixture  # noqa: F401
from governance_distribution_helpers import (
    _body,
    _doc,
    _failing_fetcher,
    _static_fetcher,
    pd_format_exc,
)

from kiro_crew.platform import governance
from kiro_crew.platform import policy_distribution as pd
from kiro_crew.platform.context import PlatformCompositionError


class _FakeRequest:
    """The one attribute ``_NoRedirects.redirect_request`` reads off a urllib request."""

    def __init__(self, url: str) -> None:
        self.full_url = url


class TestTheDeniedEnvListCoversEveryVariable:
    """One owner for the set, and two consumers that match it differently.

    ``sandbox`` scrubs by ``startswith``, but ``cron_script._CRON_ENV_DENY`` tests exact
    membership and ``mcp_cron`` builds ``\\b``-anchored regexes — so a PREFIX entry
    silently matches nothing in either of those, which is how a bearer token would reach
    an agent-authored cron script.
    """

    def test_every_distribution_variable_is_denied_to_agents_by_exact_name(self):
        from kiro_crew import sandbox

        denied = set(sandbox._AGENT_DENIED_ENV_KEYS)
        missing = [v for v in pd.POLICY_DISTRIBUTION_ENV_VARS if v not in denied]
        assert not missing, (
            "these policy variables are not denied to agent subprocesses by exact "
            f"name: {missing}"
        )

    def test_the_cron_scrub_actually_drops_them(self, monkeypatch):
        """The consumer with the exact-match rule, exercised rather than assumed."""
        from kiro_crew import cron_script

        for var in pd.POLICY_DISTRIBUTION_ENV_VARS:
            monkeypatch.setenv(var, "secret-value")
        scrubbed = cron_script._clean_cron_env()
        leaked = [v for v in pd.POLICY_DISTRIBUTION_ENV_VARS if v in scrubbed]
        assert not leaked, f"a cron script would still read: {leaked}"


class TestTheHostnameAloneIsRedacted:
    """Plenty of transport errors never quote the whole URL."""

    @pytest.mark.parametrize(
        "message",
        [
            "hostname 'config.corp.example' doesn't match either of 'a', 'b'",
            "[Errno -2] Name or service not known: config.corp.example",
            "connection to config.corp.example timed out",
        ],
        ids=["tls-mismatch", "dns", "timeout"],
    )
    def test_a_host_only_message_is_redacted(self, message):
        source = "https://config.corp.example/kirocrew/security_policy.json"
        redacted = pd._redact_source(message, source)
        assert "config.corp.example" not in redacted
        assert "policy source" in redacted

    def test_a_full_url_still_gets_the_more_informative_substitution(self):
        source = "https://config.corp.example/p.json"
        redacted = pd._redact_source(f"could not fetch {source}: down", source)
        assert source not in redacted
        assert "<the https policy source>" in redacted


class TestEndpointAuthoredTextIsSanitised:
    """The bytes in a parser error are not ours.

    ``json``'s errors quote the offending text, so a document that echoes back the
    request's ``Authorization`` header — an endpoint reflecting its own request, a proxy
    error page — would carry that credential into ``kirocrew policy fetch`` and into
    ``GET /api/logs``.
    """

    def test_a_credential_echoed_by_the_endpoint_does_not_reach_the_detail(
        self, transport, install_ceiling
    ):
        leaky = b'{"Authorization": "Bearer AKIAIOSFODNN7EXAMPLE" oops'
        source = transport(_static_fetcher(pd.FetchedPolicy(body=leaky)))
        install_ceiling(governance.parse_policy(_doc("running", distribution={"source": source})))
        outcome = pd.refresh_now()
        assert outcome.status == pd.REFRESH_REJECTED
        assert "AKIAIOSFODNN7EXAMPLE" not in outcome.detail

    def test_the_sanitiser_runs_both_passes(self):
        """The source elision AND the shared credential chain, not one or the other."""
        source = "https://config.corp.example/p.json"
        text = f"fetching {source} returned AKIAIOSFODNN7EXAMPLE"
        clean = pd._sanitize_detail(text, source)
        assert source not in clean
        assert "config.corp.example" not in clean
        assert "AKIAIOSFODNN7EXAMPLE" not in clean


class TestTheSignatureAuditLabelIsRedacted:
    """The signature verifier records its label in the audit trail, and it does so inside
    ``governance`` — so a caller sanitising this module's *raised* text cannot reach it.
    For the file tiers that label is the operator's own path and belongs in the record;
    for this tier it is a URL that may be the credential, so it is redacted before it
    ever crosses over.
    """

    def test_an_unverified_signature_does_not_log_the_url(self, caplog):
        secret = "https://signed.corp.example/p.json?X-Amz-Signature=deadbeefcafe"
        body = _body("central", identity={"issuer": "corp", "signature": "not-a-signature"})

        with caplog.at_level(logging.DEBUG):
            try:
                pd.parse_distributed_policy(body, source=secret)
            except PlatformCompositionError:
                pass

        emitted = "\n".join(r.getMessage() + (pd_format_exc(r) or "") for r in caplog.records)
        assert secret not in emitted
        assert "X-Amz-Signature" not in emitted
        assert "signed.corp.example" not in emitted

    def test_the_agent_reachable_log_line_carries_neither_label_nor_reason(self, caplog):
        """The log ring is served by ``GET /api/logs``, which the agent's own browser
        tooling can drive; the SEL record is on the keystone and is not reachable that
        way. So the reason and the policy label belong in the record, and the warning
        carries only the state plus a pointer to it."""
        from kiro_crew.platform import governance as gov

        with caplog.at_level(logging.WARNING, logger="kiro_crew.platform.governance"):
            gov._audit_policy_signature(
                gov.SIGNATURE_UNVERIFIED,
                "no trust key for issuer 'corp'",
                "https://signed.corp.example/p.json?X-Amz-Signature=deadbeefcafe",
            )

        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "an unverified signature must still be visible to an operator"
        joined = "\n".join(warnings)
        assert "UNVERIFIED" in joined
        assert "signed.corp.example" not in joined
        assert "X-Amz-Signature" not in joined
        assert "corp" not in joined, "the issuer the DOCUMENT claimed is not ours to echo"

    def test_the_raised_text_carries_no_url_either(self):
        secret = "https://signed.corp.example/p.json?X-Amz-Signature=deadbeefcafe"
        with pytest.raises(PlatformCompositionError) as caught:
            pd.parse_distributed_policy(b"{ not json", source=secret)
        assert secret not in str(caught.value)
        assert "signed.corp.example" not in str(caught.value)
        assert "https" in str(caught.value), "the scheme still says which tier failed"


class TestARedirectRefusalNamesNoTarget:
    def test_only_the_scheme_of_the_target_is_reported(self):
        """The target comes from the ENDPOINT's ``Location`` header, so it is neither ours
        to publish nor covered by ``_redact_source`` — and a redirect to a pre-signed URL
        would carry its signature into the boot abort and the log ring."""
        target = "https://evil.corp.example/p.json?X-Amz-Signature=deadbeefcafe"
        handler = pd._NoRedirects()

        with pytest.raises(urllib.error.HTTPError) as caught:
            handler.redirect_request(
                _FakeRequest("https://config.corp.example/p.json"), None, 302, "Found", {}, target
            )

        message = str(caught.value)
        assert target not in message
        assert "X-Amz-Signature" not in message
        assert "evil.corp.example" not in message
        assert "https" in message, "the scheme still distinguishes a downgrade from a move"


class TestTheBootReRaiseIsSanitised:
    @pytest.mark.parametrize(
        "fetcher",
        [
            _static_fetcher(pd.FetchedPolicy(body=b"")),
            _failing_fetcher(PlatformCompositionError("policy source names scheme 'htps'")),
        ],
        ids=["empty-document", "config-error"],
    )
    def test_a_configuration_error_does_not_name_the_endpoint(self, transport, fetcher):
        """A boot abort's text reaches stderr and any supervisor capturing it.

        The module's rule is that the URL is emitted nowhere, and a re-raise is not an
        exception to it.
        """
        source = transport(fetcher)
        with pytest.raises(PlatformCompositionError) as caught:
            pd.load_distributed_policy(governance.PolicyDistribution(source=source))
        assert source not in str(caught.value)
        assert "policy.example" not in str(caught.value)


# ──────────────────────────────────────────────────────────────────────────
# Read-only posture
# ──────────────────────────────────────────────────────────────────────────


class TestPosture:
    def test_an_unconfigured_host_reports_nothing_configured(self):
        posture = pd.distribution_posture()
        assert posture["configured"] is False
        assert posture["error_code"] == ""
        assert posture["source_scheme"] == ""

    def test_the_url_appears_nowhere_in_the_serialized_posture(self, install_ceiling):
        """The dashboard is reachable by the agent's own browser tooling.

        The endpoint is the fleet's control plane, so naming it would tell a
        prompt-injected agent exactly where to aim. The scheme alone answers what
        an operator asks: is this host centrally governed, and is it encrypted.
        """
        host = "config.corp.example"
        source = f"https://{host}/kirocrew/policy.json"
        install_ceiling(
            governance.parse_policy(
                _doc(
                    "running",
                    distribution={
                        "source": source,
                        "refresh_interval_secs": 900,
                        "max_cache_age_secs": 86400,
                    },
                )
            )
        )
        posture = pd.distribution_posture()
        assert posture["configured"] is True
        assert posture["source_scheme"] == "https"
        assert posture["refresh_interval_seconds"] == 900
        assert posture["max_cache_age_seconds"] == 86400
        assert posture["error_code"] == ""
        serialized = json.dumps(posture)
        assert host not in serialized
        assert source not in serialized

    def test_a_misconfigured_source_reports_a_code_and_still_no_url(self, monkeypatch):
        """A status panel must not raise, and an exception's prose leaks the URL."""
        host = "config.corp.example"
        monkeypatch.setenv(pd.POLICY_URL_ENV, f"https://{host}/kirocrew/policy.json")
        monkeypatch.setenv(pd.POLICY_REFRESH_ENV, "15m")
        posture = pd.distribution_posture()
        assert posture["error_code"] == pd.POSTURE_ERROR_MISCONFIGURED
        assert posture["configured"] is False
        assert host not in json.dumps(posture)
