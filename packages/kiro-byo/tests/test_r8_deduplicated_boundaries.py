"""R8: shared protocol envelope and Crew adapter seam semantics."""
from __future__ import annotations

from copy import deepcopy

import pytest

from kiro_byo import __version__
from kiro_byo.integration_manager import (
    IntegrationTarget,
    _created_files,
    _generated_files,
)
from kiro_byo.mcp_transports import (
    MCP_MODERN_VERSION,
    HttpMcpServer,
    StdioMcpServer,
)


@pytest.mark.parametrize("transport", [StdioMcpServer, HttpMcpServer])
def test_modern_envelope_preserves_user_metadata_without_mutation(transport):
    original = {
        "name": "safe-tool", "arguments": {"x": 1},
        "_meta": {
            "trace": "my-trace",
            "io.modelcontextprotocol/protocolVersion": "untrusted",
            "io.modelcontextprotocol/clientInfo": {"name": "forged"},
            "io.modelcontextprotocol/clientCapabilities": {"unsafe": True},
        },
    }
    before = deepcopy(original)
    result = transport._modern_params(original)
    assert original == before
    assert result is not original
    assert result["arguments"] == {"x": 1}
    assert result["_meta"]["trace"] == "my-trace"
    assert result["_meta"]["io.modelcontextprotocol/protocolVersion"] == MCP_MODERN_VERSION
    assert result["_meta"]["io.modelcontextprotocol/clientInfo"] == {
        "name": "kiro-byo", "version": __version__,
    }
    assert result["_meta"]["io.modelcontextprotocol/clientCapabilities"] == {}


@pytest.mark.parametrize("transport", [StdioMcpServer, HttpMcpServer])
@pytest.mark.parametrize("meta", [None, "invalid", ["unexpected"]])
def test_modern_envelope_keeps_legacy_nonmapping_meta_behavior(transport, meta):
    params = {"_meta": meta}
    result = transport._modern_params(params)
    assert result["_meta"]["io.modelcontextprotocol/protocolVersion"] == MCP_MODERN_VERSION
    assert params["_meta"] is meta


def test_stdio_and_http_modern_envelope_identical():
    params = {"_meta": {"trace": "same"}, "region": "eu"}
    assert StdioMcpServer._modern_params(params) == HttpMcpServer._modern_params(params)


def test_crew_generated_created_paths_reuse_same_normalization():
    payload = {
        "generated_files": [r"src\kiro_crew\a.py", "src/kiro_crew/b.py", 3],
        "created_files": [r"src\kiro_crew\c.py", "src/kiro_crew/c.py"],
    }
    target = IntegrationTarget(
        crew_version="0.11.0", crew_package=None, site_packages=None,
        adapter_dir=None, baseline="", manifest={}, compatibility=payload,
    )
    assert target.generated_files == _generated_files(payload) == frozenset({
        "src/kiro_crew/a.py", "src/kiro_crew/b.py", "3",
    })
    assert target.created_files == _created_files(payload) == frozenset({"src/kiro_crew/c.py"})
    assert _generated_files({"generated_files": "bad"}) == frozenset()
    assert _created_files({"created_files": None}) == frozenset()
