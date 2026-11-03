"""Codex keeps Crew isolation while routing privileged actions to its gate."""

from kiro_crew.acp.harness.codex import CodexHarness
from kiro_crew.acp.types import ACP_BACKEND_CODEX


class TestExternalSandboxRouting:
    def test_host_opts_in_to_the_adapter_mode(self):
        env = {"CODEX_ACP_EXTERNAL_SANDBOX": "false"}
        CodexHarness().apply_spawn_env(env)
        assert env["CODEX_ACP_EXTERNAL_SANDBOX"] == "true"

    def test_required_mode_preserves_the_host_sandbox(self):
        from kiro_crew.agent_sdk.backends import permission_config_for

        assert permission_config_for(ACP_BACKEND_CODEX) == ("mode", "external-sandbox")

    def test_an_adapter_without_external_sandbox_is_refused(self):
        from kiro_crew import acp_tool_gate

        options = [{"id": "mode", "options": [{"value": "read-only"}]}]
        assert "external-sandbox" in acp_tool_gate.session_config_issue(ACP_BACKEND_CODEX, options)
