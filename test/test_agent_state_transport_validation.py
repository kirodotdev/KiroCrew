"""Tests for ``agent_state.capability_transport_fields_valid``.

An invalid transport in one mcpServers entry makes the Capabilities API
reject the WHOLE agent spec with ``invalid_spec`` (see
``agent_capabilities._rows``), so this contract must stay precise: an absent
optional field is fine, a present-but-wrong-typed field is not. ``url`` is
meaningless for a stdio transport (one with a ``command``), so a present
empty string there must validate the same as the field being absent. A
remote-only transport (no ``command``) has no such fallback: an empty
``url`` there stays invalid, the same as it would be on ``command``/``type``.
"""

from __future__ import annotations

from kiro_crew.agent_state import capability_transport_fields_valid


def test_stdio_transport_without_url_is_valid():
    transport = {
        "type": "stdio",
        "command": "/usr/local/bin/example-tool",
        "args": ["--serve"],
    }

    assert capability_transport_fields_valid(transport) is True


def test_stdio_transport_with_empty_url_is_valid():
    """Regression: an empty ``url`` string on a stdio transport is not a
    present-but-wrong value -- stdio does not use ``url`` at all, so it must
    validate the same as if the field were absent.
    """
    transport = {
        "type": "stdio",
        "command": "/usr/local/bin/example-tool",
        "url": "",
    }

    assert capability_transport_fields_valid(transport) is True


def test_http_transport_with_nonempty_url_is_valid():
    transport = {"type": "http", "url": "https://example.test/mcp"}

    assert capability_transport_fields_valid(transport) is True


def test_http_transport_with_empty_url_is_invalid():
    """A remote transport has no ``command`` fallback, so an empty ``url``
    there is a genuinely malformed server with nothing to connect to, not
    an absent field. The stdio exemption must not leak to this shape.
    """
    transport = {"type": "http", "url": ""}

    assert capability_transport_fields_valid(transport) is False


def test_bare_empty_url_without_command_is_invalid():
    """Same as the http case, but without even a ``type`` -- the exemption
    keys off ``command`` being present, not off ``type`` being absent.
    """
    transport = {"url": ""}

    assert capability_transport_fields_valid(transport) is False


def test_empty_command_is_invalid():
    transport = {"type": "stdio", "command": ""}

    assert capability_transport_fields_valid(transport) is False


def test_empty_type_is_invalid():
    transport = {"type": "", "command": "/usr/local/bin/example-tool"}

    assert capability_transport_fields_valid(transport) is False


def test_non_string_command_is_invalid():
    transport = {"type": "stdio", "command": 123}

    assert capability_transport_fields_valid(transport) is False


def test_non_dict_value_is_invalid():
    assert capability_transport_fields_valid("not-a-dict") is False
    assert capability_transport_fields_valid(None) is False
    assert capability_transport_fields_valid([]) is False
