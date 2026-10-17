"""Fixtures and spec writers the session-array MCP test modules share.

``test_acp_session_mcp.py`` and ``test_acp_session_mcp_client_seam.py`` import them
by name. Three autouse fixtures pin the kiro-cli release the spec ``permissions``
gate sees, the installed claude-agent-acp version and per-test seed provenance;
``agents_dir`` (declared ``@pytest.fixture(name=...)`` and imported under its
function name) points the agent-spec resolver at a temp directory, and the writers
put a spec there or in a project checkout.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.acp import session_mcp
from kiro_crew.acp.client import AcpClient
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION

_CORE = {"command": "/opt/kirocrew", "args": ["mcp-core"]}
_CRON = {"command": "/opt/kirocrew", "args": ["mcp-cron"]}


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the session-array MCP modules' autouse kiro-cli release pin, moved verbatim; tracked for a de-flake pass
def _pinned_kiro_cli_version(monkeypatch):
    """Pin the kiro-cli release the spec ``permissions`` gate believes is installed.

    A client start here materialises the agent spec (``ensure_agent_materialized``
    -> ``rebuild_agent_config`` -> ``_write_derived_permissions``), which reads
    ``installed_kiro_cli_version`` function-locally from ``kiro_crew.kiro_cli``:
    one real ``kiro-cli --version`` spawn per binary identity, process-cached, so
    whichever test in the worker starts first pays it against the HOST's install
    with the checkout as the child's cwd. Pinned to the floor release, as
    ``test_agent.py`` and the generated-writer suites pin it.
    """
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version",
        lambda: SPEC_PERMISSIONS_MIN_VERSION,
    )


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the session-array MCP modules' autouse claude adapter version pin, moved verbatim; tracked for a de-flake pass
def _installed_claude_adapter_version(monkeypatch):
    """The installed claude-agent-acp version the spawn arm would have read.

    The settings writer applies the ``settingSources`` floor before the
    handshake from ``_claude_adapter_disk_version``; tests that drive the writer
    directly never run the spawn arm, so the floor release is set as the class
    default. A test about the floor sets its own.
    """
    monkeypatch.setattr(AcpClient, "_claude_adapter_disk_version", "0.84.0", raising=False)


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the session-array MCP modules' autouse per-test seed provenance, moved verbatim; tracked for a de-flake pass
def isolated_seed_provenance(monkeypatch):
    """Per-test provenance state, mirroring test_acp_seed_provenance.py.

    ``_RECORDS``, ``_LIVE`` and ``_SHARERS`` are process-wide runtime state;
    without this, a client authored in one test leaves a live claim that a
    LATER test's client (pytest truncates long test names to one shared
    tmp-dir prefix, so paths can even collide) is correctly refused against.
    """
    from kiro_crew.acp import seed_provenance

    monkeypatch.setattr(seed_provenance, "_RECORDS", {})
    monkeypatch.setattr(seed_provenance, "_LIVE", {})
    monkeypatch.setattr(seed_provenance, "_SHARERS", {})


@pytest.fixture(name="agents_dir")
def agents_dir_fixture(tmp_path, monkeypatch):
    """Point the agent-spec resolver at a temp agents directory."""
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", d)
    # The global settings file is a second source of per-tool restrictions;
    # these tests supply it (or leave it absent) rather than read the machine's.
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "settings-mcp.json")
    # Materialization would try to REBUILD the managed default from bundled
    # defaults; these tests supply the spec themselves.
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: True)
    monkeypatch.setattr(
        session_mcp,
        "managed_mcp_spec_entry",
        lambda name: {"kirocrew-core": dict(_CORE), "kirocrew-cron": dict(_CRON)}.get(name),
    )
    # Registry mode reads the effective config; pinned off (the default for a
    # personal install) so the symmetric filter is deterministic here. The tests
    # that care flip it explicitly.
    monkeypatch.setattr(session_mcp, "_mcp_registry_mode", lambda: False)
    return d


def _write_spec(
    agents_dir: Path, *, servers: dict, tools: list | None, name: str = "kirocrew"
) -> None:
    spec: dict = {"name": name, "mcpServers": servers}
    if tools is not None:
        spec["tools"] = tools
    (agents_dir / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


def _write_project_spec(project_dir: Path, *, servers: dict, tools: list | None) -> None:
    """A spec in the checkout kiro-cli resolves ``--agent`` against first."""
    agents = project_dir / ".kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    spec: dict = {"name": "kirocrew", "mcpServers": servers}
    if tools is not None:
        spec["tools"] = tools
    (agents / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")


def _by_name(elements: list[dict]) -> dict[str, dict]:
    return {e["name"]: e for e in elements}
