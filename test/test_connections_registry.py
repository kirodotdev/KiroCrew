"""Contract tests for the curated Connections launch registry."""

import json
import time
import warnings
from datetime import date, datetime, timedelta, timezone

import pytest

from kiro_crew.connections import (
    RegistryValidationError,
    get_all_providers,
    get_all_registry_providers,
    get_visible_providers,
    stale_l0_baselines,
)
from kiro_crew.connections.registry import (
    L0_VERIFICATION_MAX_AGE_DAYS,
    L0_VERIFICATION_WARN_AGE_DAYS,
    REVOKE_VERIFICATION_MAX_AGE_DAYS,
    _load_registry,
    utc_today,
)

EXPECTED_LAUNCH_REGISTRY = {
    "atlassian",
    "github",
    "gitlab",
    "linear",
    "notion",
    "stripe",
    "superhuman",
    "vercel",
}
# Registered so the OAuth banner allowlist stays registry-derived, but held back
# from the Connect grid: GitHub until the Kiro app is registered, Superhuman until
# a logged-in check records its revoke surface and walks the consent flow.
LAUNCH_GATED = {"github", "superhuman"}


def test_registry_contains_only_the_agreed_launch_set():
    assert {provider["slug"] for provider in get_all_providers()} == EXPECTED_LAUNCH_REGISTRY


def test_probe_accessor_includes_every_entry_even_when_launch_gated():
    providers = get_all_registry_providers()
    assert {provider["slug"] for provider in providers} == EXPECTED_LAUNCH_REGISTRY
    gated = {p["slug"] for p in providers if p["launch_gate_passed"] is False}
    assert gated == LAUNCH_GATED


def test_only_gated_launch_services_are_visible():
    assert {provider["slug"] for provider in get_visible_providers()} == (
        EXPECTED_LAUNCH_REGISTRY - LAUNCH_GATED
    )


def test_every_registry_mcp_authorization_server_is_banner_allowlisted():
    """A registry entry whose consent URL the banner gate rejects cannot be connected.

    The gate exempts OAuth entropy only at an exact ``(host, path)`` in the
    builtin set, so every provider's advertised authorization server must have
    an entry there -- gated providers included, because the gate is what a
    hand-configured MCP server for the same provider hits today.
    """
    from urllib.parse import urlsplit

    from kiro_crew.security import _OAUTH_AUTHORIZATION_ENDPOINTS

    allowlisted_hosts = {host for host, _path in _OAUTH_AUTHORIZATION_ENDPOINTS}
    for provider in get_all_registry_providers():
        issuer_host = urlsplit(provider["l0_expectations"]["authorization_server"]).hostname
        assert issuer_host in allowlisted_hosts, provider["slug"]


def test_linear_installs_its_read_only_endpoint():
    """Linear's card promises read access; the installed URL must match."""
    (linear,) = [p for p in get_all_providers() if p["slug"] == "linear"]
    assert linear["mcp_url"] == "https://mcp.linear.app/mcp/readonly"


def test_gitlab_installs_the_official_remote_endpoint():
    """The MCP URL is the resource GitLab's own RFC 9728 metadata advertises."""
    (gitlab,) = [p for p in get_all_providers() if p["slug"] == "gitlab"]
    assert gitlab["mcp_url"] == "https://gitlab.com/api/v4/mcp"
    assert gitlab["l0_expectations"]["authorization_server"] == "https://gitlab.com"


def test_gitlab_card_copy_admits_its_only_scope_can_write():
    """GitLab ships no read-only scope: `mcp` also opens issues and merge
    requests and runs pipelines. A card that implied read-only would lie, so the
    single scope and the disclosure are pinned together."""
    (gitlab,) = [p for p in get_all_providers() if p["slug"] == "gitlab"]
    assert gitlab["recommended_scopes"] == ["mcp"]
    assert "can write" in gitlab["gotcha_copy"]


def test_client_id_is_optional_and_unset_for_every_launch_provider():
    """GitHub is the intended first consumer, still pending its app registration."""
    assert all("client_id" not in provider for provider in get_all_registry_providers())


def test_client_id_is_accepted_when_a_provider_declares_one(tmp_path):
    payload = get_all_registry_providers()
    github = next(p for p in payload if p["slug"] == "github")
    github["client_id"] = "Iv1.public-identifier"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = {p["slug"]: p for p in _load_registry(registry_path)}
    assert loaded["github"]["client_id"] == "Iv1.public-identifier"


@pytest.mark.parametrize("bad", ["", "   ", 42, None, ["id"]])
def test_client_id_must_be_a_non_empty_string_when_present(tmp_path, bad):
    payload = get_all_registry_providers()
    payload[0]["client_id"] = bad
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RegistryValidationError, match="client_id"):
        _load_registry(registry_path)


def test_unknown_fields_are_still_rejected(tmp_path):
    """Optional client_id must not widen the schema to arbitrary keys."""
    payload = get_all_registry_providers()
    payload[0]["client_secret"] = "nope"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RegistryValidationError, match="unknown fields: client_secret"):
        _load_registry(registry_path)


@pytest.mark.parametrize(
    "expectations",
    [
        {"dcr": True},
        {
            "authorization_server": "https://auth.example.com",
            "dcr": True,
            "pkce": True,
            "verified_on": "2026-01-01",
            "unexpected": False,
        },
        # verified_on is required: an entry nobody has re-derived is not tracked.
        {
            "authorization_server": "https://auth.example.com",
            "dcr": True,
            "pkce": True,
        },
        {
            "authorization_server": "https://auth.example.com",
            "dcr": "yes",
            "pkce": True,
            "verified_on": "2026-01-01",
        },
        {
            "authorization_server": "https://auth.example.com:not-a-port",
            "dcr": True,
            "pkce": True,
            "verified_on": "2026-01-01",
        },
        {
            "authorization_server": "http://auth.example.com",
            "dcr": True,
            "pkce": True,
            "verified_on": "2026-01-01",
        },
        # The issuer is the one URL the recorder may fetch, so an IP literal or a
        # local name is refused outright rather than merely discouraged.
        {
            "authorization_server": "https://127.0.0.1",
            "dcr": True,
            "pkce": True,
            "verified_on": "2026-01-01",
        },
        {
            "authorization_server": "https://10.0.0.5/tenant",
            "dcr": True,
            "pkce": True,
            "verified_on": "2026-01-01",
        },
        {
            "authorization_server": "https://[::1]/tenant",
            "dcr": True,
            "pkce": True,
            "verified_on": "2026-01-01",
        },
        {
            "authorization_server": "https://metadata.internal/token",
            "dcr": True,
            "pkce": True,
            "verified_on": "2026-01-01",
        },
        {
            "authorization_server": "https://auth.example.com?tenant=a",
            "dcr": True,
            "pkce": True,
            "verified_on": "2026-01-01",
        },
        {
            "authorization_server": "https://auth.example.com",
            "dcr": True,
            "pkce": True,
            "verified_on": "not-a-date",
        },
        # A real-looking date that is not a real date.
        {
            "authorization_server": "https://auth.example.com",
            "dcr": True,
            "pkce": True,
            "verified_on": "2026-02-31",
        },
    ],
)
def test_l0_expectations_are_exact_and_valid(tmp_path, expectations):
    payload = get_all_registry_providers()
    payload[0]["l0_expectations"] = expectations
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RegistryValidationError, match="l0_expectations"):
        _load_registry(registry_path)


def test_an_issuer_may_carry_a_path():
    """Three of seven providers put a tenant or realm path in their issuer.

    Storing only the origin is what let a realm substitution pass before, so a
    path is not merely tolerated here -- it is the point.
    """
    by_slug = {p["slug"]: p for p in get_all_registry_providers()}

    assert by_slug["stripe"]["l0_expectations"]["authorization_server"] == (
        "https://access.stripe.com/mcp"
    )
    assert by_slug["github"]["l0_expectations"]["authorization_server"] == (
        "https://github.com/login/oauth"
    )


def test_l0_baseline_cannot_be_stamped_in_the_future(tmp_path):
    """A future stamp is the one way a hand-typed baseline could evade the gate."""

    payload = get_all_registry_providers()
    payload[0]["l0_expectations"]["verified_on"] = (
        utc_today() + timedelta(days=1)
    ).isoformat()
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RegistryValidationError, match="must not be in the future"):
        _load_registry(registry_path)


#: ``time.tzset`` is POSIX-only. Without it, setting ``TZ`` does not move the
#: interpreter's local zone at all, so the two tests below would assert against
#: an unshifted clock and pass no matter what ``utc_today`` read -- a vacuous
#: green that is worse than an honest skip. The behaviour under test is
#: platform-independent, so POSIX coverage is enough to catch a regression.
_needs_tzset = pytest.mark.skipif(
    not hasattr(time, "tzset"), reason="shifting the local zone needs POSIX time.tzset"
)


@_needs_tzset
def test_utc_today_is_the_utc_date_whatever_the_local_zone(monkeypatch):
    """The guard must read UTC, not the local clock.

    Deterministic at any hour: UTC-12 puts the local date a day BEHIND whenever
    the UTC hour is under 12, and UTC+14 puts it a day AHEAD whenever the UTC hour
    is 10 or more. Those windows cover the whole clock, so at least one zone here
    always disagrees with UTC -- which is what makes this catch a local-date
    regression rather than passing by luck.
    """

    expected = datetime.now(timezone.utc).date()

    for zone in ("Etc/GMT+12", "Etc/GMT-14", "UTC"):
        monkeypatch.setenv("TZ", zone)
        time.tzset()
        try:
            assert utc_today() == expected, zone
        finally:
            monkeypatch.undo()
            time.tzset()


@_needs_tzset
@pytest.mark.parametrize("zone", ["Etc/GMT+12", "Etc/GMT-14"])
def test_a_utc_stamp_is_valid_from_a_shifted_timezone(tmp_path, monkeypatch, zone):
    """A baseline --record just wrote must validate wherever the user sits.

    ``--record`` stamps the UTC date. When the guard compared against the LOCAL
    date, a user west of UTC got a stamp that read as the future and the registry
    refused to load until local midnight caught up.
    """

    monkeypatch.setenv("TZ", zone)
    time.tzset()
    try:
        payload = get_all_registry_providers()
        payload[0]["l0_expectations"]["verified_on"] = utc_today().isoformat()
        registry_path = tmp_path / "registry.json"
        registry_path.write_text(json.dumps(payload), encoding="utf-8")

        # Must not raise: the stamp is today in UTC, which is what wrote it.
        _load_registry(registry_path)
    finally:
        monkeypatch.undo()
        time.tzset()


def test_the_warn_tier_fires_before_the_hard_tier():
    """The lead time is the whole point: warn must land strictly earlier."""

    assert L0_VERIFICATION_WARN_AGE_DAYS < L0_VERIFICATION_MAX_AGE_DAYS


@pytest.mark.parametrize(
    ("age_days", "expected_stale"),
    [(89, False), (90, False), (91, True)],
)
def test_a_baseline_is_stale_only_strictly_past_the_window(age_days, expected_stale):
    as_of = date(2026, 6, 1)
    providers = [
        {
            "slug": "example",
            "l0_expectations": {"verified_on": (as_of - timedelta(days=age_days)).isoformat()},
        }
    ]

    stale = stale_l0_baselines(providers, as_of=as_of, max_age_days=90)

    assert bool(stale) is expected_stale


def test_the_hard_tier_cannot_fire_on_a_dark_provider():
    """Scoping is what keeps a stale baseline from failing every PR in the repo.

    The remedy for a stale baseline is ``l0_probe --record``, which needs live
    network that CI does not have, so the hard tier is deliberately narrow: only
    providers actually shipped to users. GitHub is launch-gated today, so it must
    appear in the all-entries view and NOT in the visible one.
    """
    far_future = utc_today() + timedelta(days=10_000)

    everything = stale_l0_baselines(
        get_all_registry_providers(), as_of=far_future, max_age_days=1
    )
    visible_only = stale_l0_baselines(
        get_visible_providers(), as_of=far_future, max_age_days=1
    )

    assert "github" in everything
    assert "github" not in visible_only
    assert set(visible_only) < set(everything)


def test_l0_baselines_due_for_re_recording_are_surfaced_without_failing():
    """WARN tier. This test MUST NOT fail on age -- that is the whole design.

    A hard age threshold on every PR would be a scheduled repo-wide outage: the
    nightly cannot refresh the stamps (it is read-only by design) so nothing
    resets the timer, and CI has no network to run the remedy. So the early
    signal is a warning here plus a red NIGHTLY, which is where someone who can
    act will see it.
    """
    aging = stale_l0_baselines(
        get_all_registry_providers(),
        as_of=utc_today(),
        max_age_days=L0_VERIFICATION_WARN_AGE_DAYS,
    )
    if aging:
        warnings.warn(
            "L0 baselines are due for re-recording: "
            f"{aging}. Run `python -m kiro_crew.connections.l0_probe --record` "
            "on a networked machine and commit the result.",
            stacklevel=2,
        )


def test_visible_provider_l0_baselines_have_not_aged_out():
    """HARD tier, visible providers only, after the warn tier's lead time.

    Failing here means running ``python -m kiro_crew.connections.l0_probe
    --record`` on a networked machine and committing the result -- not widening
    the window or hand-editing dates.
    """
    stale = stale_l0_baselines(
        get_visible_providers(),
        as_of=utc_today(),
        max_age_days=L0_VERIFICATION_MAX_AGE_DAYS,
    )
    assert not stale, f"visible-provider L0 baselines need re-recording: {stale}"


def test_every_visible_provider_revoke_link_was_verified_recently():
    """A shipped revoke link must carry a check no older than the max age.

    The link is the user's route to undoing a grant, and providers move these
    settings pages. Failing here means re-checking the page (logged in, on the
    real account) and refreshing ``revoke_verified_on`` -- not widening the
    window.
    """
    oldest_allowed = date.today() - timedelta(days=REVOKE_VERIFICATION_MAX_AGE_DAYS)
    stale = {
        provider["slug"]: provider["revoke_verified_on"]
        for provider in get_visible_providers()
        if date.fromisoformat(provider["revoke_verified_on"]) < oldest_allowed
    }
    assert not stale, f"revoke links need re-verification: {stale}"


def test_every_registry_entry_records_how_its_revoke_link_was_verified():
    for provider in get_all_registry_providers():
        assert provider["revoke_verified_note"].strip()


@pytest.mark.parametrize("bad", ["", "2026-8-7", "07-08-2026", "2026-02-31", "yesterday", 20260807])
def test_revoke_verified_on_must_be_a_real_iso_date(tmp_path, bad):
    payload = get_all_registry_providers()
    payload[0]["revoke_verified_on"] = bad
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RegistryValidationError, match="revoke_verified_on"):
        _load_registry(registry_path)


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_revoke_verified_note_must_be_a_non_empty_string(tmp_path, bad):
    payload = get_all_registry_providers()
    payload[0]["revoke_verified_note"] = bad
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RegistryValidationError, match="revoke_verified_note"):
        _load_registry(registry_path)


def test_notion_card_copy_names_the_surface_that_actually_lists_the_grant():
    notion = next(p for p in get_visible_providers() if p["slug"] == "notion")
    # Notion's grant list is a settings-modal page with no address of its own, so
    # two rounds of deep-link guessing both landed users somewhere the grant is
    # not. The link is the workspace home and the path carries the navigation --
    # and www.notion.so is not that home: it bounces a logged-in user out to the
    # notion.com marketing site.
    assert notion["revoke_page_url"] == "https://app.notion.com"
    assert "127.0.0.1" in notion["gotcha_copy"]
    assert "Notion MCP" in notion["gotcha_copy"]
