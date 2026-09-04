"""Direct coverage of AWS-free AgentCore policy-field validators.

Governance parse re-exports these; later stack PRs import them from the AWS
extra. A host that never installed ``kirocrew[agentcore]`` still has to reject
a credentialed, fragmented, over-long, or whitespace-bearing Gateway URL
before it can land in Policy.json or a systemd Environment= line.
"""

from __future__ import annotations

import pytest

from kiro_crew.platform.agentcore_schema import (
    AGENTCORE_GATEWAY_URL_MAX,
    WORKLOAD_NAME_MAX,
    WORKLOAD_NAME_MIN,
    normalize_agentcore_gateway_url,
    normalize_agentcore_workload_name,
)

_VALID_URL = "https://gateway.example.test/mcp"


class TestNormalizeAgentcoreGatewayUrl:
    def test_empty_and_none_are_legal(self) -> None:
        assert normalize_agentcore_gateway_url(None) == ""
        assert normalize_agentcore_gateway_url("") == ""
        assert normalize_agentcore_gateway_url("   ") == ""

    def test_valid_https_url_is_stripped(self) -> None:
        assert normalize_agentcore_gateway_url(f"  {_VALID_URL}  ") == _VALID_URL

    @pytest.mark.parametrize(
        "url",
        (
            "http://gateway.example.test/mcp",
            "https://user:pass@gateway.example.test/mcp",
            "https://user@gateway.example.test/mcp",
            "https://gateway.example.test/mcp#frag",
            "https://",
            "not-a-url",
            "ftp://gateway.example.test/mcp",
        ),
    )
    def test_rejects_insecure_or_credentialed_urls(self, url: str) -> None:
        with pytest.raises(ValueError, match="https URL without credentials"):
            normalize_agentcore_gateway_url(url)

    def test_rejects_over_long_url(self) -> None:
        url = "https://gateway.example.test/" + ("a" * AGENTCORE_GATEWAY_URL_MAX)
        assert len(url) > AGENTCORE_GATEWAY_URL_MAX
        with pytest.raises(ValueError, match="exceeds"):
            normalize_agentcore_gateway_url(url)

    @pytest.mark.parametrize(
        "url",
        (
            "https://gateway.example.test/mcp\nExecStartPre=/bin/true",
            "https://gateway.example.test/mcp\t$(id)",
            "https://gateway.example.test/mcp extra",
        ),
    )
    def test_rejects_internal_whitespace(self, url: str) -> None:
        with pytest.raises(ValueError, match="https URL without credentials"):
            normalize_agentcore_gateway_url(url)


class TestNormalizeAgentcoreWorkloadName:
    def test_empty_and_none_are_legal(self) -> None:
        assert normalize_agentcore_workload_name(None) == ""
        assert normalize_agentcore_workload_name("") == ""
        assert normalize_agentcore_workload_name("   ") == ""

    def test_valid_name_is_stripped(self) -> None:
        assert normalize_agentcore_workload_name("  crew.one_2-3  ") == "crew.one_2-3"

    def test_rejects_too_short(self) -> None:
        with pytest.raises(ValueError, match="usable workload identity name"):
            normalize_agentcore_workload_name("ab")
        assert WORKLOAD_NAME_MIN == 3

    def test_rejects_too_long(self) -> None:
        with pytest.raises(ValueError, match="usable workload identity name"):
            normalize_agentcore_workload_name("a" * (WORKLOAD_NAME_MAX + 1))

    @pytest.mark.parametrize("name", ("has space", "slash/name", "at@name", "bang!"))
    def test_rejects_charset(self, name: str) -> None:
        with pytest.raises(ValueError, match="usable workload identity name"):
            normalize_agentcore_workload_name(name)
