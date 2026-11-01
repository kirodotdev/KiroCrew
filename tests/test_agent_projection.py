from __future__ import annotations

import unittest

from kiro_byo.agent_projection import AgentProjection, from_session_params


class AgentProjectionTests(unittest.TestCase):
    def test_v3_tags_control_visibility_before_permission(self) -> None:
        projection = AgentProjection(
            agent_id="restricted",
            prompt="x",
            tools=("read", "write"),
            tools_declared=True,
            excluded_tools=("edit",),
        )
        available = {
            "read", "read_files", "list_directory", "glob", "grep", "code_symbols", "code_query",
            "write", "edit", "delete_file", "shell", "execute_shell_process", "control_shell_process",
            "web_fetch", "web_search",
        }
        self.assertEqual(
            projection.allowed_native_names(available),
            {"read", "read_files", "list_directory", "glob", "grep", "code_symbols", "code_query", "write", "delete_file"},
        )


    def test_web_tag_is_separate_from_read_tag(self) -> None:
        projection = AgentProjection(
            agent_id="research",
            prompt="x",
            tools=("read", "web"),
            tools_declared=True,
            excluded_tools=("web_search",),
        )
        available = {"read", "read_files", "glob", "web_fetch", "web_search", "shell"}
        self.assertEqual(
            projection.allowed_native_names(available),
            {"read", "read_files", "glob", "web_fetch"},
        )


    def test_read_and_shell_tags_expand_to_new_harness_owned_tools(self) -> None:
        projection = AgentProjection(
            agent_id="coding",
            prompt="x",
            tools=("read", "shell"),
            tools_declared=True,
        )
        available = {
            "read", "glob", "code_symbols", "code_query",
            "shell", "execute_shell_process", "control_shell_process",
            "write", "web_fetch",
        }
        self.assertEqual(
            projection.allowed_native_names(available),
            {
                "read", "glob", "code_symbols", "code_query",
                "shell", "execute_shell_process", "control_shell_process",
            },
        )

    def test_explicit_empty_tools_means_no_tools(self) -> None:
        projection = AgentProjection(
            agent_id="none",
            prompt="x",
            tools=(),
            tools_declared=True,
        )
        self.assertEqual(projection.allowed_native_names({"read", "write", "shell"}), set())
        self.assertFalse(projection.allows_any_mcp())

    def test_missing_tools_on_saved_agent_does_not_widen_visibility(self) -> None:
        # KiroCrew 0.10 KAS wire: absent tools means no tools. This must also
        # apply to legacy persisted Agents with tools_declared=False.
        projection = AgentProjection(agent_id="legacy", prompt="x")
        self.assertEqual(projection.allowed_native_names({"read", "write"}), set())
        self.assertFalse(projection.allows_any_mcp())
        self.assertFalse(projection.allows_mcp(
            server_name="kirocrew-core", tool_name="browser",
            public_name="mcp__kirocrew-core__browser"))
        default = AgentProjection()
        self.assertEqual(default.allowed_native_names({"read", "write"}), {"read", "write"})
        self.assertTrue(default.allows_any_mcp())

    def test_mcp_server_and_tool_selectors_and_exclusions(self) -> None:
        projection = AgentProjection(
            agent_id="mcp",
            prompt="x",
            tools=("@core", "@other/ping"),
            tools_declared=True,
            excluded_tools=("@core/delete",),
        )
        self.assertTrue(projection.allows_mcp(server_name="core", tool_name="search", public_name="mcp__core__search"))
        self.assertFalse(projection.allows_mcp(server_name="core", tool_name="delete", public_name="mcp__core__delete"))
        self.assertTrue(projection.allows_mcp(server_name="other", tool_name="ping", public_name="mcp__other__ping"))
        self.assertFalse(projection.allows_mcp(server_name="other", tool_name="write", public_name="mcp__other__write"))


    def test_allowed_tools_exact_and_glob_match_native_without_expanding_tags(self) -> None:
        projection = AgentProjection(
            agent_id="trusted",
            prompt="x",
            allowed_tools=("read", "web_*", "execute_bash_process"),
        )
        self.assertTrue(projection.auto_approves_native("read"))
        self.assertTrue(projection.auto_approves_native("web_search"))
        self.assertTrue(projection.auto_approves_native("execute_shell_process"))
        # ``shell`` is an exact built-in grant, not a visibility tag expansion.
        self.assertFalse(projection.auto_approves_native("control_shell_process"))
        self.assertFalse(projection.auto_approves_native("write"))

    def test_allowed_tools_match_mcp_server_tool_and_glob(self) -> None:
        projection = AgentProjection(
            agent_id="trusted",
            prompt="x",
            allowed_tools=("@kirocrew-core", "@git/read_*", "@other-?/ping"),
        )
        self.assertTrue(projection.auto_approves_mcp(
            server_name="kirocrew-core", tool_name="skill_search",
            public_name="mcp__kirocrew-core__skill_search",
        ))
        self.assertTrue(projection.auto_approves_mcp(
            server_name="git", tool_name="read_file", public_name="mcp__git__read_file",
        ))
        self.assertTrue(projection.auto_approves_mcp(
            server_name="other-a", tool_name="ping", public_name="mcp__other-a__ping",
        ))
        self.assertFalse(projection.auto_approves_mcp(
            server_name="kirocrew-computer", tool_name="computer_click",
            public_name="mcp__kirocrew-computer__computer_click",
        ))

    def test_session_projection_consumes_allowed_tools_from_trusted_custom_agent(self) -> None:
        projection = from_session_params({
            "_meta": {"kiro": {
                "modeId": "default",
                "customAgents": [{
                    "id": "default",
                    "prompt": "A",
                    "tools": ["@kirocrew-core", "read"],
                    "allowedTools": ["@kirocrew-core", "read"],
                }],
            }}
        })
        self.assertEqual(projection.allowed_tools, ("@kirocrew-core", "read"))
        self.assertTrue(projection.auto_approves_mcp(
            server_name="kirocrew-core", tool_name="skill_search",
            public_name="mcp__kirocrew-core__skill_search",
        ))
        self.assertTrue(projection.auto_approves_native("read"))

    def test_session_projection_prefers_requested_mode(self) -> None:
        projection = from_session_params({
            "_meta": {"kiro": {
                "modeId": "b",
                "customAgents": [
                    {"id": "a", "prompt": "A", "tools": ["read"]},
                    {"id": "b", "prompt": "B", "tools": ["shell"], "excludedTools": ["shell"]},
                ],
            }}
        })
        self.assertEqual(projection.agent_id, "b")
        self.assertEqual(projection.prompt, "B")
        self.assertEqual(projection.allowed_native_names({"read", "shell"}), set())


if __name__ == "__main__":
    unittest.main()
