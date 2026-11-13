"""Unit tests for the MCP-entry URL validator in ``kiro_crew.apps.manifest``.

These functions classify an ``mcpServers`` entry so the plugin converter and the
agent-config writers can refuse an endpoint none of them should dial. The classes
each test pins are the ones the validator exists to separate: a stdio entry, a
remote or loopback server, and the invalid shapes (mistyped
field, ``${VAR}`` reference, cleartext remote http, malformed url).
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.manifest import (
    _is_loopback_host,
    mcp_entry_references_env,
    mcp_url_kind,
    mcp_url_violation,
    mistyped_mcp_server_fields,
    references_env,
)


@pytest.mark.parametrize(
    "host,expected",
    [
        ("localhost", True),
        ("LocalHost", True),
        ("127.0.0.1", True),
        ("127.5.6.7", True),
        ("::1", True),
        ("0.0.0.0", True),
        ("::", True),
        # IPv4-mapped IPv6 forms route to the IPv4 address they carry.
        ("::ffff:127.0.0.1", True),
        ("::ffff:0.0.0.0", True),
        ("::ffff:192.168.0.1", False),
        ("example.com", False),
        ("10.0.0.5", False),
        ("", False),
        ("not-an-ip", False),
    ],
)
def test_is_loopback_host(host: str, expected: bool) -> None:
    assert _is_loopback_host(host) is expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("${TOKEN}", True),
        ("prefix ${env:LEGACY} suffix", True),
        ("${a}${b}", True),
        ("no placeholder here", False),
        ("${}", False),
        ("${unclosed", False),
        ("plain${", False),
    ],
)
def test_references_env(value: str, expected: bool) -> None:
    assert references_env(value) is expected


def test_references_env_stays_linear_on_unclosed_openers() -> None:
    # The regex form this replaced backtracks quadratically on a run of unclosed
    # ``${`` openers; the linear scan returns at once. The strings are built in the
    # body, not parametrized, so a huge value never becomes a test id.
    assert references_env("${" * 100_000) is False
    assert references_env("${" * 100_000 + "x}") is True


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://example.com/mcp", True),
        ("http://localhost:8080/mcp", True),
        ("https://example.com:65535/", True),
        ("", False),
        ("   ", False),
        ("ftp://example.com", False),
        ("https:///nohost", False),
        ("https://user:pass@example.com", False),
        ("https://example.com/#frag", False),
        ("https://example.com:99999", False),
        ("https://exa\\mple.com", False),
        ("https://example.com/\x01", False),
    ],
)
def test_mcp_url_violation(url: str, ok: bool) -> None:
    # ``ok`` means the url is admissible, i.e. the violation string is empty.
    assert (mcp_url_violation(url) == "") is ok


@pytest.mark.parametrize(
    "config,expected",
    [
        ({"url": "https://example.com"}, False),
        ({"url": "https://example.com/${TOKEN}"}, True),
        ({"command": "server", "args": ["--key", "${API_KEY}"]}, True),
        ({"command": "server", "env": {"K": "${SECRET}"}}, True),
        ({"url": "https://x", "headers": {"Authorization": "Bearer ${T}"}}, True),
        ({"command": "server", "cwd": "${HOME}/x"}, True),
        ({"command": "server", "env": {"env:LEGACY": "${env:LEGACY}"}}, True),
        ({"command": "server", "args": ["--plain"]}, False),
    ],
)
def test_mcp_entry_references_env(config: dict, expected: bool) -> None:
    assert mcp_entry_references_env(config) is expected


@pytest.mark.parametrize(
    "config,bad",
    [
        ({"command": "server", "args": ["a", "b"]}, []),
        ({"url": "https://x", "headers": {"A": "b"}}, []),
        ({"command": 7}, ["command"]),
        ({"args": 7}, ["args"]),
        ({"args": ["ok", 2]}, ["args"]),
        ({"env": {"A": 1}}, ["env"]),
        ({"env": [["A", "b"]]}, ["env"]),
        ({"type": 1, "url": 2}, ["type", "url"]),
    ],
)
def test_mistyped_mcp_server_fields(config: dict, bad: list[str]) -> None:
    assert mistyped_mcp_server_fields(config) == bad


@pytest.mark.parametrize(
    "cfg,expected",
    [
        # Not an object at all.
        ("nope", "invalid"),
        (7, "invalid"),
        # Stdio entry (no url key), well-typed.
        ({"command": "server", "args": ["--x"]}, ""),
        # Mistyped field is invalid whatever the transport.
        ({"command": "server", "args": 7}, "invalid"),
        ({"url": 7}, "invalid"),
        # ${VAR} anywhere is invalid.
        ({"url": "https://x/${T}"}, "invalid"),
        ({"command": "s", "env": {"K": "${S}"}}, "invalid"),
        # Remote https.
        ({"url": "https://example.com/mcp"}, "remote"),
        # Loopback is a self-managed server on a fixed port: remote.
        ({"url": "http://localhost:8080/mcp"}, "remote"),
        ({"url": "http://127.0.0.1:9000/"}, "remote"),
        # An IPv4-mapped IPv6 loopback literal is loopback, not a remote host.
        ({"url": "http://[::ffff:127.0.0.1]:8080/mcp"}, "remote"),
        # Cleartext http to a non-loopback host is refused.
        ({"url": "http://example.com/mcp"}, "invalid"),
        # Malformed url is refused.
        ({"url": "https://user:pass@example.com"}, "invalid"),
        ({"url": "ftp://example.com"}, "invalid"),
    ],
)
def test_mcp_url_kind(cfg: object, expected: str) -> None:
    assert mcp_url_kind(cfg) == expected
