"""Codex runs inside Crew's sandbox without nesting its own.

codex-acp's ``read-only`` mode makes Codex build a second OS sandbox inside Crew's,
which cannot start there. With ``CODEX_ACP_EXTERNAL_SANDBOX=true`` an adapter that
supports it offers ``external-sandbox``; Crew writes that value when it is offered
and keeps ``read-only`` otherwise, on both drivers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import acp_tool_gate
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness.codex import CodexHarness
from kiro_crew.acp.types import ACP_BACKEND_CODEX
from kiro_crew.agent_sdk.backends import permission_value_for


def _options(*values: str) -> list[dict]:
    return [{"id": "mode", "options": [{"value": v} for v in values]}]


_NEW_ADAPTER = _options("read-only", "agent", "external-sandbox")
_OLD_ADAPTER = _options("read-only", "agent")


class TestTheHostOptsIn:
    def test_the_spawn_env_turns_the_adapter_mode_on(self):
        env = {"CODEX_ACP_EXTERNAL_SANDBOX": "false"}
        CodexHarness().apply_spawn_env(env)
        assert env["CODEX_ACP_EXTERNAL_SANDBOX"] == "true"


class TestTheValueWritten:
    def test_external_sandbox_is_written_when_the_adapter_offers_it(self):
        assert permission_value_for(ACP_BACKEND_CODEX, _NEW_ADAPTER) == "external-sandbox"

    def test_an_older_adapter_keeps_read_only(self):
        assert permission_value_for(ACP_BACKEND_CODEX, _OLD_ADAPTER) == "read-only"

    def test_unreadable_options_keep_read_only(self):
        assert permission_value_for(ACP_BACKEND_CODEX, None) == "read-only"
        assert permission_value_for(ACP_BACKEND_CODEX, [{"id": "other"}]) == "read-only"

    def test_the_preferred_value_on_another_option_is_ignored(self):
        options = [{"id": "model", "options": [{"value": "external-sandbox"}]}]
        assert permission_value_for(ACP_BACKEND_CODEX, options) == "read-only"

    def test_a_backend_without_a_preference_gets_its_required_value(self):
        options = [{"id": "allow_all", "options": [{"value": "off"}, {"value": "on"}]}]
        assert permission_value_for("copilot", options) == "off"

    def test_an_older_adapter_is_still_routed(self):
        """The fallback is the existing floor: no issue, so the session is not refused."""
        assert acp_tool_gate.session_config_issue(ACP_BACKEND_CODEX, _OLD_ADAPTER) == ""
        assert acp_tool_gate.session_config_issue(ACP_BACKEND_CODEX, _NEW_ADAPTER) == ""


def _client(tmp_path: Path, options: list[dict], writes: list) -> AcpClient:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CODEX)
    client._acp_config_options = options

    async def _write(config_id: str, value: str) -> None:
        writes.append((config_id, value))

    client.set_config_option = _write  # type: ignore[method-assign]
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "expected"),
    [(_NEW_ADAPTER, "external-sandbox"), (_OLD_ADAPTER, "read-only")],
)
async def test_the_client_driver_writes_the_resolved_value(tmp_path, options, expected):
    writes: list = []
    await _client(tmp_path, options, writes)._apply_session_permission_routing()
    assert writes == [("mode", expected)]
