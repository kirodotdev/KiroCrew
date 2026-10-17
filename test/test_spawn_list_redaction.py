"""Test 4.3: spawn_list redacts before truncating task strings."""

from __future__ import annotations

from unittest.mock import patch

from kiro_crew.mcp_core import _call_tool_inner


class TestSpawnListRedactBeforeTruncate:
    def test_credential_at_truncation_boundary_is_redacted(self):
        """A credential straddling the 60-char boundary must be fully redacted."""
        padding = "A" * 50
        secret = "AKIAIOSFODNN7EXAMPLE"
        task = padding + secret  # 70 chars total

        fake_response = {
            "agents": [
                {
                    "id": "agent-1",
                    "task": task,
                    "done": False,
                    "turns": 1,
                    "last_tool": "shell",
                    "elapsed": 5,
                    "started": 0,
                }
            ]
        }

        with patch("kiro_crew.mcp_core._get", return_value=fake_response):
            result = _call_tool_inner("spawn_list", {})

        # The raw key must not appear (even partially) in the output
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "AKIA" not in result


class TestSpawnListAgentNames:
    """Cover the Available agents line in spawn_list description."""

    def test_spawn_list_includes_agent_names(self):
        from unittest.mock import MagicMock

        fake_agent = MagicMock()
        fake_agent.name = "yolo-general"

        fake_response = {"agents": []}

        with patch("kiro_crew.mcp_core._get", return_value=fake_response), patch(
            "kiro_crew.mcp_core.list_agents", return_value=[fake_agent]
        ):
            result = _call_tool_inner("spawn_list", {})

        assert "yolo-general" in result
