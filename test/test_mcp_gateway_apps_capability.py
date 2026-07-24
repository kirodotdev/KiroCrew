"""Tests for MCP Apps ui-capability injection (SEP-1865) in the gateway.

The gateway — not kiro-cli — is the MCP Apps host, so it advertises
``capabilities.extensions["io.modelcontextprotocol/ui"]`` on the initialize
frame it forwards to backends. Injection is gated by KIROCREW_MCP_APPS and
must be a byte-identical no-op when the flag is off.
"""

import copy

import pytest

from kiro_crew.mcp_gateway.backend import (
    MCP_APPS_ENV_FLAG,
    MCP_APPS_EXTENSION_KEY,
    MCP_APPS_MIME_TYPE,
    _inject_client_extensions,
)


def _init_frame(capabilities=None):
    params = {
        "protocolVersion": "2025-06-18",
        "clientInfo": {"name": "kiro-cli", "version": "1.0.0"},
    }
    if capabilities is not None:
        params["capabilities"] = capabilities
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv(MCP_APPS_ENV_FLAG, "1")


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.delenv(MCP_APPS_ENV_FLAG, raising=False)


class TestFlagOff:
    def test_returns_same_object_untouched(self, flag_off):
        msg = _init_frame(capabilities={})
        before = copy.deepcopy(msg)
        out = _inject_client_extensions(msg)
        assert out is msg
        assert msg == before

    def test_falsy_flag_values_stay_off(self, monkeypatch):
        for value in ("", "0", "false", "no", "off"):
            monkeypatch.setenv(MCP_APPS_ENV_FLAG, value)
            msg = _init_frame(capabilities={})
            assert _inject_client_extensions(msg) is msg


class TestFlagOn:
    def test_injects_ui_extension_with_mime(self, flag_on):
        out = _inject_client_extensions(_init_frame(capabilities={}))
        ext = out["params"]["capabilities"]["extensions"]
        assert ext[MCP_APPS_EXTENSION_KEY] == {"mimeTypes": [MCP_APPS_MIME_TYPE]}

    def test_injects_when_capabilities_absent(self, flag_on):
        out = _inject_client_extensions(_init_frame(capabilities=None))
        ext = out["params"]["capabilities"]["extensions"]
        assert MCP_APPS_EXTENSION_KEY in ext

    def test_preserves_existing_capabilities_and_extensions(self, flag_on):
        caps = {"roots": {"listChanged": True}, "extensions": {"vendor/x": {"a": 1}}}
        out = _inject_client_extensions(_init_frame(capabilities=caps))
        got = out["params"]["capabilities"]
        assert got["roots"] == {"listChanged": True}
        assert got["extensions"]["vendor/x"] == {"a": 1}
        assert got["extensions"][MCP_APPS_EXTENSION_KEY] == {"mimeTypes": [MCP_APPS_MIME_TYPE]}

    def test_existing_ui_entry_left_untouched(self, flag_on):
        deliberate = {"mimeTypes": ["text/html;profile=custom"]}
        caps = {"extensions": {MCP_APPS_EXTENSION_KEY: deliberate}}
        out = _inject_client_extensions(_init_frame(capabilities=caps))
        assert out["params"]["capabilities"]["extensions"][MCP_APPS_EXTENSION_KEY] == deliberate

    def test_input_frame_never_mutated(self, flag_on):
        """Copy discipline: the stub's captured frame must not be aliased."""
        msg = _init_frame(capabilities={"roots": {}})
        before = copy.deepcopy(msg)
        out = _inject_client_extensions(msg)
        assert msg == before
        assert out is not msg
        assert out["params"] is not msg["params"]
        assert out["params"]["capabilities"] is not msg["params"]["capabilities"]

    def test_malformed_params_passthrough(self, flag_on):
        for params in (None, "bogus", 7, ["list"]):
            msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}
            assert _inject_client_extensions(msg) is msg

    def test_non_dict_capabilities_replaced_safely(self, flag_on):
        out = _inject_client_extensions(_init_frame(capabilities="corrupt"))
        assert MCP_APPS_EXTENSION_KEY in out["params"]["capabilities"]["extensions"]
