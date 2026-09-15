"""Tests for spawn_sub_agents MCP tool handler."""

from __future__ import annotations

from unittest.mock import patch

from kiro_crew.mcp_core import _call_tool


class TestSpawnSubAgents:
    def test_spawns_agents_and_collects_results(self):
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "sess1"}):
            mock_post.return_value = {"id": "a1"}
            mock_get.return_value = {"done": True, "agent": "worker", "result": "ok"}

            result = _call_tool("spawn_sub_agents", {
                "agents": [{"agent_or_mode": "worker", "prompt": "do task"}],
            })

            assert '"completed"' in result
            assert '"worker"' in result

    def test_returns_error_for_empty_agents(self):
        with patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            result = _call_tool("spawn_sub_agents", {"agents": []})
            assert "Error" in result

    def test_rejects_non_dict_entries_via_schema(self):
        with patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            result = _call_tool("spawn_sub_agents", {
                "agents": ["invalid", {"prompt": "real task"}],
            })

            assert "expected dict" in result

    def test_skips_empty_prompt(self):
        with patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            result = _call_tool("spawn_sub_agents", {
                "agents": [{"prompt": ""}],
            })
            assert "no valid agent entries" in result

    def test_reports_spawn_errors(self):
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.return_value = {"error": "capacity reached"}

            result = _call_tool("spawn_sub_agents", {
                "agents": [{"prompt": "task1"}],
            })

            assert "Error spawning" in result
            assert "capacity" in result

    def test_mixed_success_and_spawn_error(self):
        # One agent spawns OK, another fails to spawn: results must include the
        # completed agent AND a spawn_errors entry.
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.side_effect = [{"id": "a1"}, {"error": "capacity reached"}]
            mock_get.return_value = {"done": True, "agent": "w", "result": "ok"}

            result = _call_tool("spawn_sub_agents", {
                "agents": [{"prompt": "ok task"}, {"prompt": "doomed task"}],
            })

            assert '"completed"' in result
            assert '"spawn_errors"' in result
            assert "capacity reached" in result

    def test_reports_spawn_with_no_agent_id(self):
        # /api/spawn returns neither error nor id — must not append an empty
        # id (which would poll /api/spawn/), but record an error instead.
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.return_value = {}  # no "error", no "id"

            result = _call_tool("spawn_sub_agents", {
                "agents": [{"prompt": "task1"}],
            })

            assert "Error spawning" in result
            assert "no agent id" in result
            # The poll endpoint must never be hit with an empty id.
            assert mock_get.call_count == 0

    def test_reports_timed_out_agents(self):
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.time") as mock_time, \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.return_value = {"id": "a1"}
            mock_get.return_value = {"done": False, "agent": "slow"}
            # Deadline now uses time.monotonic(): calls are
            # (1) deadline init, (2) _next_ping init, (3) loop guard.
            # Make the loop-guard value exceed the deadline to time out at once.
            mock_time.monotonic.side_effect = [0, 0, 999999]
            mock_time.sleep = lambda _: None

            result = _call_tool("spawn_sub_agents", {
                "agents": [{"prompt": "long task"}],
            })

            assert '"timed_out"' in result

    def test_pings_session_keepalive_during_long_poll(self):
        """Finding 1: the poll loop must ping /api/session-keepalive so the
        gateway does not SIGTERM the ACP subprocess mid-poll."""
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.time") as mock_time, \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.return_value = {"id": "a1"}
            mock_get.return_value = {"done": True, "agent": "w", "result": "ok"}
            # monotonic calls: deadline init(0), next_ping init(0), loop guard(10),
            # keepalive check(70 >= 60 -> ping), next_ping reset(70).
            mock_time.monotonic.side_effect = [0, 0, 10, 70, 70]
            mock_time.sleep = lambda _: None

            _call_tool("spawn_sub_agents", {"agents": [{"prompt": "slow task"}]})

            assert any(
                call.args and call.args[0] == "/api/session-keepalive"
                for call in mock_post.call_args_list
            ), "expected a /api/session-keepalive ping during the poll loop"

    def test_errored_agent_settles_loop_without_spinning(self):
        """Finding 1: an agent that reports error (never done) must settle the
        poll loop instead of spinning until max_wait."""
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.return_value = {"id": "a1"}
            # done is False but error is set — must be treated as settled.
            mock_get.return_value = {"done": False, "error": "crashed", "agent": "bad"}

            result = _call_tool("spawn_sub_agents", {"agents": [{"prompt": "task"}]})

            assert '"error"' in result
            assert "crashed" in result

    def test_max_wait_configurable_via_env(self):
        """Finding 1: max_wait is configurable and clamped."""
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.time") as mock_time, \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s",
                                       "KIROCREW_SPAWN_SUB_AGENTS_MAX_WAIT": "120"}):
            mock_post.return_value = {"id": "a1"}
            mock_get.return_value = {"done": False, "agent": "slow"}
            # deadline = 0 + 120 = 120; loop guard at 200 exceeds it -> time out.
            mock_time.monotonic.side_effect = [0, 0, 200]
            mock_time.sleep = lambda _: None

            result = _call_tool("spawn_sub_agents", {"agents": [{"prompt": "t"}]})

            assert '"timed_out"' in result

    def test_reports_errored_agents(self):
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.return_value = {"id": "a1"}
            mock_get.return_value = {"done": True, "error": "crashed", "agent": "bad"}

            result = _call_tool("spawn_sub_agents", {
                "agents": [{"prompt": "task"}],
            })

            assert '"error"' in result
            assert "crashed" in result

    def test_redacts_agent_name_in_output(self):
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.return_value = {"id": "a1"}
            mock_get.return_value = {
                "done": True,
                "agent": "agent AKIAIOSFODNN7EXAMPLE here",
                "result": "ok",
            }

            result = _call_tool("spawn_sub_agents", {
                "agents": [{"prompt": "task"}],
            })

            assert "AKIAIOSFODNN7EXAMPLE" not in result

    def test_redacts_result_text(self):
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.return_value = {"id": "a1"}
            mock_get.return_value = {
                "done": True,
                "agent": "w",
                "result": "key AKIAIOSFODNN7EXAMPLE found",
            }

            result = _call_tool("spawn_sub_agents", {
                "agents": [{"prompt": "task"}],
            })

            assert "AKIAIOSFODNN7EXAMPLE" not in result

    def test_passes_cwd_to_spawn(self):
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.return_value = {"id": "a1"}
            mock_get.return_value = {"done": True, "agent": "", "result": ""}

            _call_tool("spawn_sub_agents", {
                "agents": [{"prompt": "task"}],
                "cwd": "/workspace/project",
            })

            # The spawn call is the first _post; mark-collected is the last.
            spawn_call = mock_post.call_args_list[0]
            body = spawn_call[0][1]
            assert body["cwd"] == "/workspace/project"

    def test_multiple_agents_spawned_in_parallel(self):
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.side_effect = [{"id": "a1"}, {"id": "a2"}, {}]
            mock_get.return_value = {"done": True, "agent": "w", "result": "done"}

            result = _call_tool("spawn_sub_agents", {
                "agents": [
                    {"agent_or_mode": "coder", "prompt": "code it"},
                    {"agent_or_mode": "reviewer", "prompt": "review it"},
                ],
            })

            # 2 spawn calls + 1 mark-collected call = 3 total
            assert mock_post.call_count == 3
            assert result.count('"completed"') == 2

    def test_truncates_oversized_prompt(self):
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.return_value = {"id": "a1"}
            mock_get.return_value = {"done": True, "agent": "", "result": ""}

            long_prompt = "x" * 10000
            _call_tool("spawn_sub_agents", {
                "agents": [{"prompt": long_prompt}],
            })

            # The spawn call is the first _post; mark-collected is the last.
            spawn_call = mock_post.call_args_list[0]
            body = spawn_call[0][1]
            assert len(body["task"]) <= 5000

    def test_truncates_oversized_agent_or_mode(self):
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.return_value = {"id": "a1"}
            mock_get.return_value = {"done": True, "agent": "w", "result": "ok"}

            _call_tool("spawn_sub_agents", {
                "agents": [{"agent_or_mode": "x" * 5000, "prompt": "task"}],
            })

            # The spawn call is the first _post; mark-collected is the last.
            spawn_call = mock_post.call_args_list[0]
            body = spawn_call[0][1]
            assert len(body["agent"]) < 5000  # truncated to MAX_SHORT_STRING


class TestSpawnSubAgentsSummarization:
    """Tests for the result summarization when results exceed COMPLETION_KEEP_DEFAULT_CHARS."""

    def test_short_result_inlined_verbatim(self):
        """Results under the threshold are returned as-is without summarization."""
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.return_value = {"id": "a1"}
            short_result = "This is a short result under 3K chars."
            mock_get.return_value = {"done": True, "agent": "w", "result": short_result}

            result = _call_tool("spawn_sub_agents", {"agents": [{"prompt": "task"}]})

            assert short_result in result
            assert "Full transcript:" not in result

    def test_large_result_summarized_with_path(self):
        """Results exceeding the threshold are summarized with first+last words and a disk path."""
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.sel"), \
             patch("kiro_crew.mcp_core.summarize_result") as mock_summarize, \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.return_value = {"id": "agent123"}
            # Generate a result that exceeds 3000 chars
            large_result = "word " * 1000  # ~5000 chars, well over 3K
            mock_get.return_value = {"done": True, "agent": "w", "result": large_result}
            mock_summarize.return_value = (
                "Full transcript: /home/user/.kirocrew/subagents/agent123/result.txt\n"
                "Preview (first+last 100 words):\nword word word...\n\n"
                "The full result is on disk."
            )

            result = _call_tool("spawn_sub_agents", {"agents": [{"prompt": "task"}]})

            # summarize_result should have been called
            mock_summarize.assert_called_once()
            assert "Full transcript:" in result
            assert "on disk" in result

    def test_large_result_uses_summarize_result_with_correct_path(self):
        """Verify summarize_result is called with the agent's result.txt path."""
        from kiro_crew.context_management import COMPLETION_KEEP_DEFAULT_CHARS

        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.sel"), \
             patch("kiro_crew.mcp_core.summarize_result") as mock_summarize, \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.return_value = {"id": "abc123"}
            large_result = "x " * 2000  # ~4000 chars, over 3K threshold
            mock_get.return_value = {"done": True, "agent": "w", "result": large_result}
            mock_summarize.return_value = "summarized content"

            _call_tool("spawn_sub_agents", {"agents": [{"prompt": "task"}]})

            # summarize_result must have been called with the result text and a path
            # containing the agent id
            assert mock_summarize.called
            call_args = mock_summarize.call_args
            assert len(call_args[0][0]) > COMPLETION_KEEP_DEFAULT_CHARS
            assert "abc123" in call_args[0][1]
            assert "result.txt" in call_args[0][1]

    def test_agent_dir_failure_falls_back_to_full_result(self):
        """If _agent_dir raises, the full result is inlined (graceful fallback)."""
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.return_value = {"id": "../bad-id"}
            large_result = "fallback " * 600  # over 3K
            mock_get.return_value = {"done": True, "agent": "w", "result": large_result}

            # _agent_dir will raise ValueError for "../bad-id" due to path traversal check
            result = _call_tool("spawn_sub_agents", {"agents": [{"prompt": "task"}]})

            # Should still complete without error — falls back to full text
            assert '"completed"' in result
            # The result should contain the original text (not summarized)
            # because _agent_dir raised and result_path is empty
            assert "Full transcript:" not in result

    def test_mixed_short_and_large_results(self):
        """When multiple agents return, only large results get summarized."""
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.sel"), \
             patch("kiro_crew.mcp_core.summarize_result") as mock_summarize, \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.side_effect = [{"id": "short1"}, {"id": "long1"}]
            short_result = "brief answer"
            large_result = "detailed " * 600  # over 3K

            def _get_side(url):
                if "short1" in url:
                    return {"done": True, "agent": "fast", "result": short_result}
                return {"done": True, "agent": "thorough", "result": large_result}

            mock_get.side_effect = _get_side
            mock_summarize.return_value = "summarized long result"

            result = _call_tool("spawn_sub_agents", {
                "agents": [{"prompt": "quick"}, {"prompt": "deep dive"}],
            })

            # Short result inlined verbatim
            assert "brief answer" in result
            # Long result was summarized
            assert mock_summarize.call_count == 1
            assert "summarized long result" in result

    def test_result_exactly_at_threshold_not_summarized(self):
        """A result exactly at COMPLETION_KEEP_DEFAULT_CHARS is NOT summarized."""
        from kiro_crew.context_management import COMPLETION_KEEP_DEFAULT_CHARS

        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.sel"), \
             patch("kiro_crew.mcp_core.summarize_result") as mock_summarize, \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.return_value = {"id": "a1"}
            # Exactly at the threshold (not over)
            exact_result = "x" * COMPLETION_KEEP_DEFAULT_CHARS
            mock_get.return_value = {"done": True, "agent": "w", "result": exact_result}

            result = _call_tool("spawn_sub_agents", {"agents": [{"prompt": "task"}]})

            # Should NOT summarize — threshold is >, not >=
            mock_summarize.assert_not_called()
            assert '"completed"' in result

    def test_invalid_max_wait_env_falls_back(self):
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {
                 "KIROCREW_SESSION_KEY": "s",
                 "KIROCREW_SPAWN_SUB_AGENTS_MAX_WAIT": "not-a-number",
             }):
            mock_post.return_value = {"id": "a1"}
            mock_get.return_value = {"done": True, "agent": "w", "result": "ok"}

            result = _call_tool("spawn_sub_agents", {"agents": [{"prompt": "task"}]})

            # Bad env must not raise; the agent still completes.
            assert '"completed"' in result

    def test_keepalive_ping_failure_is_swallowed(self):
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.time") as mock_time, \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            # spawn returns an id; the keepalive ping raises and must be swallowed.
            def _post_side(url, body=None):
                if url == "/api/session-keepalive":
                    raise RuntimeError("network down")
                return {"id": "a1"}
            mock_post.side_effect = _post_side
            mock_get.return_value = {"done": True, "agent": "w", "result": "ok"}
            # Trigger the ping branch (70 >= 60).
            mock_time.monotonic.side_effect = [0, 0, 10, 70, 70]
            mock_time.sleep = lambda _: None

            result = _call_tool("spawn_sub_agents", {"agents": [{"prompt": "task"}]})

            assert '"completed"' in result

    def test_poll_waits_then_completes(self):
        import itertools
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.time") as mock_time, \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.return_value = {"id": "a1"}
            # First poll: not done -> loop sleeps and re-polls; then done.
            states = [{"done": False, "agent": "w"}]

            def _get_side(url):
                return states.pop(0) if states else {"done": True, "agent": "w", "result": "ok"}
            mock_get.side_effect = _get_side
            mock_time.monotonic.side_effect = itertools.count(0, 5)
            mock_time.sleep = lambda _: None

            result = _call_tool("spawn_sub_agents", {"agents": [{"prompt": "task"}]})

            assert '"completed"' in result
            # Slept at least once because the first poll was not-done.
            assert mock_get.call_count >= 2


class TestSpawnList:
    def test_spawn_list_renders_running_and_done_agents(self):
        with patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.list_agents", return_value=[]), \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_get.return_value = {"agents": [
                {"id": "a1", "done": False, "task": "explore",
                 "turns": 3, "last_tool": "shell", "elapsed": 12},
                {"id": "a2", "done": True, "task": "summarize"},
            ]}

            result = _call_tool("spawn_list", {})

            assert "a1" in result and "[running]" in result
            assert "a2" in result and "[done]" in result
            assert "shell" in result  # progress detail rendered

    def test_spawn_list_empty(self):
        with patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.list_agents", return_value=[]), \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_get.return_value = {"agents": []}

            result = _call_tool("spawn_list", {})

            assert "No subagents running" in result


class TestSpawnSubAgentsAuditOwner:
    """The audit trail must distinguish a LOST owner from an absent one.

    ``_resolve_session_key`` returns ``""`` when every identity source fails,
    which is the same value a spawn with genuinely no owning session carries.
    Recording that empty string leaves the run's audit entries naming no
    session and the two cases indistinguishable afterwards, so a failed
    resolution is recorded under an explicit unresolved marker instead.
    """

    @staticmethod
    def _audit_owners(mock_sel):
        """Every ``session_key`` the tool wrote to the audit trail, in order."""
        return [
            call.kwargs["session_key"]
            for call in mock_sel.return_value.log_tool_invocation.call_args_list
        ]

    def test_unresolved_owner_is_marked_in_audit_records(self):
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core._resolve_session_key", return_value=""), \
             patch("kiro_crew.mcp_core.sel") as mock_sel:
            mock_post.return_value = {"id": "a1"}
            mock_get.return_value = {"done": True, "agent": "w", "result": "ok"}

            _call_tool("spawn_sub_agents", {"agents": [{"prompt": "task"}]})

            owners = self._audit_owners(mock_sel)
            # Both the attempt and the outcome record are written.
            assert len(owners) == 2
            for owner in owners:
                assert owner != ""
                # Wire format an audit reader filters on. Never presented as
                # trustworthy attribution -- the prefix says it is not.
                assert owner.startswith("unresolved:")
                assert owner.removeprefix("unresolved:").isdigit()

    def test_resolved_owner_is_recorded_verbatim(self):
        # The marker must not displace a real owner: attribution that resolves
        # is still recorded exactly as resolved.
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core._resolve_session_key", return_value="dashboard:tab7"), \
             patch("kiro_crew.mcp_core.sel") as mock_sel:
            mock_post.return_value = {"id": "a1"}
            mock_get.return_value = {"done": True, "agent": "w", "result": "ok"}

            _call_tool("spawn_sub_agents", {"agents": [{"prompt": "task"}]})

            assert self._audit_owners(mock_sel) == ["dashboard:tab7", "dashboard:tab7"]

    def test_unresolved_owner_does_not_reach_the_spawn_request(self):
        # Producer scope only: the marker is an audit value. The run's
        # parent_session_key still carries the empty owner, because it feeds
        # per-slot frame routing and a synthetic key there would address a
        # slot that does not exist.
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core._resolve_session_key", return_value=""), \
             patch("kiro_crew.mcp_core.sel"):
            mock_post.return_value = {"id": "a1"}
            mock_get.return_value = {"done": True, "agent": "w", "result": "ok"}

            _call_tool("spawn_sub_agents", {"agents": [{"prompt": "task"}]})

            spawn_bodies = [
                call.args[1] for call in mock_post.call_args_list
                if call.args and call.args[0] == "/api/spawn"
            ]
            assert spawn_bodies
            for body in spawn_bodies:
                assert body["parent_session"] == ""
