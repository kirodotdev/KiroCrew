"""Contract tests for the curated Connections launch registry."""

import json
from datetime import date, timedelta

import pytest

from kiro_crew.connections import (
    RegistryValidationError,
    get_all_providers,
    get_all_registry_providers,
    get_visible_providers,
)
from kiro_crew.connections.registry import REVOKE_VERIFICATION_MAX_AGE_DAYS, _load_registry

EXPECTED_LAUNCH_REGISTRY = {
    "atlassian",
    "github",
    "gitlab",
    "linear",
    "notion",
    "stripe",
    "vercel",
}


def test_registry_contains_only_the_agreed_launch_set():
    assert {provider["slug"] for provider in get_all_providers()} == EXPECTED_LAUNCH_REGISTRY


def test_probe_accessor_includes_every_entry_even_when_launch_gated():
    providers = get_all_registry_providers()
    assert {provider["slug"] for provider in providers} == EXPECTED_LAUNCH_REGISTRY
    github = next(provider for provider in providers if provider["slug"] == "github")
    assert github["launch_gate_passed"] is False


def test_only_gated_launch_services_are_visible():
    assert {provider["slug"] for provider in get_visible_providers()} == (
        EXPECTED_LAUNCH_REGISTRY - {"github"}
    )


def test_linear_installs_its_read_only_endpoint():
    """Linear's card promises read access; the installed URL must match."""
    (linear,) = [p for p in get_all_providers() if p["slug"] == "linear"]
    assert linear["mcp_url"] == "https://mcp.linear.app/mcp/readonly"


def test_gitlab_installs_the_official_remote_endpoint():
    """The MCP URL is the resource GitLab's own RFC 9728 metadata advertises."""
    (gitlab,) = [p for p in get_all_providers() if p["slug"] == "gitlab"]
    assert gitlab["mcp_url"] == "https://gitlab.com/api/v4/mcp"
    assert gitlab["l0_expectations"]["authorization_server_origin"] == "https://gitlab.com"


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
            "authorization_server_origin": "https://auth.example.com",
            "dcr": True,
            "pkce": True,
            "unexpected": False,
        },
        {
            "authorization_server_origin": "https://auth.example.com",
            "dcr": "yes",
            "pkce": True,
        },
        {
            "authorization_server_origin": "https://auth.example.com/path",
            "dcr": True,
            "pkce": True,
        },
        {
            "authorization_server_origin": "https://auth.example.com:not-a-port",
            "dcr": True,
            "pkce": True,
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
