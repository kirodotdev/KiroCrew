"""Contract tests for the curated Connections launch registry."""

from kiro_crew.connections import get_all_providers, get_visible_providers

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


def test_only_gated_launch_services_are_visible():
    assert {provider["slug"] for provider in get_visible_providers()} == (
        EXPECTED_LAUNCH_REGISTRY - {"github"}
    )


def test_linear_installs_its_read_only_endpoint():
    """Linear's card promises read access; the installed URL must match."""
    (linear,) = [p for p in get_all_providers() if p["slug"] == "linear"]
    assert linear["mcp_url"] == "https://mcp.linear.app/mcp/readonly"
