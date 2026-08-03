"""Contract tests for the curated Connections launch registry."""

import json

import pytest

from kiro_crew.connections import (
    RegistryValidationError,
    get_all_providers,
    get_all_registry_providers,
    get_visible_providers,
)
from kiro_crew.connections.registry import _load_registry

EXPECTED_LAUNCH_REGISTRY = {
    "atlassian",
    "github",
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
