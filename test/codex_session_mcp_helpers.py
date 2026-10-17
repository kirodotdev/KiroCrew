"""Shared fixtures for the codex session-MCP tests.

The agent-spec seam, the spec writer and the assembly of the array a codex
``session/new`` carries, used by both ``test_codex_session_mcp_projection.py`` and
``test_codex_session_mcp_tool_refusal.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.acp import session_mcp
from kiro_crew.acp.harness.codex import CodexHarness
from kiro_crew.providers.mirrors.codex import codex_projection

#: What codex-acp 1.11.0's ``initialize`` actually answered. Used as the live
#: advertisement in these tests so they exercise the same input the client feeds
#: the filter, rather than a shape no adapter returns.
_CODEX_1_11_CAPS = {"acp": False, "http": True, "sse": False}


_CORE = {"command": "/opt/kirocrew", "args": ["mcp-core"]}


_CRON = {"command": "/opt/kirocrew", "args": ["mcp-cron"]}


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """Point the agent-spec resolver at a temp agents directory.

    Same seam as ``test_acp_session_mcp.py``: materialization would rebuild the
    managed default from bundled defaults, and these tests supply the spec.
    """
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", d)
    # The global settings file is a second source of per-tool restrictions;
    # these tests supply it (or leave it absent) rather than read the machine's.
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "settings-mcp.json")
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: True)
    managed = {"kirocrew-core": dict(_CORE), "kirocrew-cron": dict(_CRON)}
    monkeypatch.setattr(
        session_mcp,
        "managed_mcp_spec_entry",
        lambda name, **kwargs: dict(managed[name]) if name in managed else None,
    )
    monkeypatch.setattr(session_mcp, "_mcp_registry_mode", lambda: False)
    return d


def _write_spec(agents_dir: Path, *, servers: dict, tools: list | None) -> None:
    spec: dict = {"name": "kirocrew", "mcpServers": servers}
    if tools is not None:
        spec["tools"] = tools
    (agents_dir / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")


def _by_name(elements: list[dict]) -> dict[str, dict]:
    return {e["name"]: e for e in elements}


def _codex_session_array(
    *,
    session_key: str = "",
    channel_id: str = "",
    caps: dict | None = None,
    stub_names: tuple[str, ...] = (),
    stubs: list[dict] | None = None,
) -> tuple:
    """The array a codex ``session/new`` carries, assembled the way a session is.

    Two owners, in the order the runtime asks them. The MIRROR turns the agent spec
    into elements, places the pooled broker stubs beside them and withholds what it
    must; the HARNESS then narrows the result against the ``mcpCapabilities`` THIS
    session's ``initialize`` answered. The split is the point -- the transport rule
    cannot read a live advertisement from the spawn path, where the adapter process
    does not exist yet -- so a test that means "what reaches the adapter" runs both.

    ``caps`` is that advertisement. ``None`` stands for a session whose adapter said
    nothing, which the harness passes through untouched rather than emptying.

    Returns the projection beside the narrowed array, because the deny set and the
    array are two halves of one answer and a caller usually wants both.
    """
    projection = codex_projection(
        "kirocrew",
        session_key=session_key,
        channel_id=channel_id,
        stub_server_names=stub_names,
        stub_elements=list(stubs or []),
    )
    agent_capabilities = {} if caps is None else {"mcpCapabilities": dict(caps)}
    kept = CodexHarness().session_mcp_servers(
        list(projection.params["mcpServers"]), agent_capabilities=agent_capabilities
    )
    return projection, kept
