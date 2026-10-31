"""Unit tests for the MCP-entry URL validator in ``kiro_crew.apps.manifest``.

These functions classify an ``mcpServers`` entry so the plugin converter and the
agent-config writers can refuse an endpoint none of them should dial. The classes
each test pins are the ones the validator exists to separate: a stdio entry, the
app's own loopback backend, a remote server, and the invalid shapes (mistyped
field, ``${VAR}`` reference, cleartext remote http, malformed url).
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.manifest import (
    is_loopback_host,
    mcp_entry_references_env,
    mcp_url_kind,
    mcp_url_violation,
    mistyped_mcp_server_fields,
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
        ("example.com", False),
        ("10.0.0.5", False),
        ("", False),
        ("not-an-ip", False),
    ],
)
def test_is_loopback_host(host: str, expected: bool) -> None:
    assert is_loopback_host(host) is expected


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
    "cfg,gateway_backend,expected",
    [
        # Not an object at all.
        ("nope", False, "invalid"),
        (7, False, "invalid"),
        # Stdio entry (no url key), well-typed.
        ({"command": "server", "args": ["--x"]}, False, ""),
        # Mistyped field is invalid whatever the transport.
        ({"command": "server", "args": 7}, False, "invalid"),
        ({"url": 7}, False, "invalid"),
        # ${VAR} anywhere is invalid, backend exemption included.
        ({"url": "https://x/${T}"}, True, "invalid"),
        ({"command": "s", "env": {"K": "${S}"}}, False, "invalid"),
        # Remote https: a plain converted plugin has no gateway backend.
        ({"url": "https://example.com/mcp"}, False, "remote"),
        # Loopback without a gateway backend is a self-managed server: remote.
        ({"url": "http://localhost:8080/mcp"}, False, "remote"),
        # Loopback WITH a gateway backend is the app's own backend.
        ({"url": "http://localhost:8080/mcp"}, True, "backend"),
        ({"url": "http://127.0.0.1:9000/"}, True, "backend"),
        # Cleartext http to a non-loopback host is refused.
        ({"url": "http://example.com/mcp"}, False, "invalid"),
        ({"url": "http://example.com/mcp"}, True, "invalid"),
        # Malformed url is refused.
        ({"url": "https://user:pass@example.com"}, False, "invalid"),
        ({"url": "ftp://example.com"}, False, "invalid"),
    ],
)
def test_mcp_url_kind(cfg: object, gateway_backend: bool, expected: str) -> None:
    assert mcp_url_kind(cfg, gateway_backend) == expected
