"""A stdio MCP server whose ``env`` references an unset variable is named at session start.

Such a server starts with the literal ``${NAME}`` text as the value. The usual cause
is a launch that never read the shell profile exporting it, such as a desktop app
started from Finder or the Dock.
"""

from __future__ import annotations

import logging
import os
from types import SimpleNamespace

from kiro_crew.acp import mcp_ref_guard
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.agent_sdk.backends import ACP_BACKEND_GOOSE, ACP_BACKEND_KAS, ACP_BACKEND_KIRO


def test_an_unset_placeholder_is_named_and_no_value_is(tmp_path, monkeypatch, caplog):
    home = tmp_path / "kc4970-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("KC4970_UNSET", raising=False)
    spec = {
        "mcpServers": {
            "github": {
                "command": "npx",
                "env": {
                    "TOKEN": "${KC4970_UNSET}",
                    "HOME_DIR": "${HOME}",
                    "MODE": "${KC4970_UNSET:-kc4970default}",
                },
            }
        }
    }

    with caplog.at_level(logging.WARNING, logger=mcp_ref_guard.__name__):
        found = mcp_ref_guard.warn_unset_env_placeholders(
            spec, [], backend=ACP_BACKEND_KIRO, agent="kirocrew", child_env=os.environ
        )
    assert found == (["github ('KC4970_UNSET')"], 0)
    records = [r for r in caplog.records if r.name == mcp_ref_guard.__name__]
    assert len(records) == 1
    text = records[0].getMessage()
    assert "github" in text and "KC4970_UNSET" in text
    assert str(home) not in text and "kc4970default" not in text

    # Both session-start call sites record it; the reading a model gets leaves it
    # out, because the server did start.
    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_KIRO)
    client._mcp_ref_spec = spec
    client._child_env_names = frozenset({"HOME"})
    client._begin_session_report([])
    client._guard_unresolved_mcp_refs([])
    runtime_report = McpSessionReport()
    runtime = SimpleNamespace(
        acp_backend=ACP_BACKEND_KIRO,
        _mcp_gateway_overlay=None,
        _expect_mcp_reports=False,
        _child_env_names=frozenset({"HOME"}),
    )
    handle = SimpleNamespace(mcp_session_report=lambda: runtime_report)
    AcpRuntime._guard_unresolved_mcp_refs(runtime, handle, spec, "kirocrew", [])
    for report in (client.mcp_session_report(), runtime_report):
        assert "github ('KC4970_UNSET')" in report.problem_summary()
        assert "KC4970_UNSET" not in report.problem_summary(include_reasons=False)

    # A server the attempt pruned as unstartable never starts, so it is not named.
    pruned = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_GOOSE)
    pruned._mcp_ref_spec = spec
    pruned._begin_session_report([])
    pruned._mcp_pruned_names = ("github",)
    pruned._guard_unresolved_mcp_refs([])
    assert "KC4970_UNSET" not in pruned.mcp_session_report().problem_summary()

    # kiro-cli expands its own spec server from the child's environment, which
    # agent.env feeds. A KAS spec server is judged only when the array carries it,
    # against the routed view, which agent.env feeds too (asserted below).
    child = {*os.environ, "KC4970_UNSET"}
    assert mcp_ref_guard.warn_unset_env_placeholders(
        spec, [], backend=ACP_BACKEND_KIRO, agent="k", child_env=child
    ) == ([], 0)
    assert mcp_ref_guard.warn_unset_env_placeholders(
        spec, [], backend=ACP_BACKEND_KAS, agent="k", child_env=os.environ
    ) == ([], 0)
    assert mcp_ref_guard.warn_unset_env_placeholders(
        spec, [{"name": "github"}], backend=ACP_BACKEND_KAS, agent="k", child_env=child
    ) == (["github ('KC4970_UNSET')"], 0)

    # Set only in agent.env: the routed view resolves it, so nothing is named.
    configured = SimpleNamespace(agent=SimpleNamespace(env={"KC4970_UNSET": "from-agent-env"}))
    monkeypatch.setattr(
        "kiro_crew.config.KiroCrewConfig.load", staticmethod(lambda: configured), raising=True
    )
    assert mcp_ref_guard.warn_unset_env_placeholders(
        spec, [{"name": "github"}], backend=ACP_BACKEND_KAS, agent="k", child_env=child
    ) == ([], 0)

    monkeypatch.setenv("KC4970_UNSET", "x")
    assert mcp_ref_guard.warn_unset_env_placeholders(
        spec, [], backend=ACP_BACKEND_KIRO, agent="k", child_env=os.environ
    ) == ([], 0)
