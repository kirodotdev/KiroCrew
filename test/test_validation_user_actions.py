"""Simulate real user actions through MCP tool paths to verify no behavioral regression.

Tests the exact call patterns that kiro-cli sends when the LLM invokes MCP tools,
plus dashboard API patterns. Every test here represents a real user action that
MUST work identically before and after validation was added.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ── MCP Core: simulate kiro-cli calling tools via JSON-RPC ──


class TestMcpCoreUserActions:
    """Simulate the exact JSON-RPC calls kiro-cli sends to kirocrew-core."""

    def _simulate_tool_call(self, tool_name: str, arguments: dict) -> str:
        """Simulate what kiro-cli does: JSON-RPC tools/call → _call_tool."""
        from kiro_crew.mcp_core import _call_tool

        return _call_tool(tool_name, arguments)

    # -- spawn_run: user says "search docs for X in parallel" --

    def test_spawn_fire_and_forget(self):
        with patch("kiro_crew.mcp_core._post") as mock_post:
            mock_post.return_value = {"id": "abc12345"}
            result = self._simulate_tool_call(
                "spawn_run",
                {"task": "search the codebase for uses of SessionManager"},
            )
        assert "abc12345" in result
        assert "Spawned" in result

    def test_spawn_batch_tasks(self):
        with patch("kiro_crew.mcp_core._post") as mock_post:
            mock_post.side_effect = [{"id": "a1"}, {"id": "b2"}]
            result = self._simulate_tool_call(
                "spawn_run",
                {"tasks": ["search for SessionManager", "count test files"]},
            )
        assert "2 subagent" in result
        assert "a1" in result
        assert "b2" in result

    def test_spawn_default_returns_immediately(self):
        """spawn_run always returns immediately — fire-and-forget."""
        with patch("kiro_crew.mcp_core._post") as mock_post:
            mock_post.return_value = {"id": "ghi789"}
            result = self._simulate_tool_call("spawn_run", {"task": "quick check"})
        assert "Spawned" in result
        assert "completion event" in result.lower()

    # -- learn_add: user says "remember to always use dark mode" --

    def test_learn_preference(self):
        with patch("kiro_crew.mcp_core._post") as mock_post:
            mock_post.return_value = {"status": "ok"}
            result = self._simulate_tool_call(
                "learn_add",
                {
                    "rule": "Always use dark mode for code examples",
                    "category": "preference",
                },
            )
        assert "Saved lesson" in result
        mock_post.assert_called_once_with(
            "/api/lessons",
            {
                "rule": "Always use dark mode for code examples",
                "category": "preference",
                "scope": "global",
            },
        )

    def test_learn_with_negative(self):
        """The NOT-clause must reach the payload, not just the tool schema.

        Regression guard: this test used to supply ``negative`` and assert only
        that the call succeeded, so it passed while ``_call_tool`` built the body
        as ``{rule, category, scope}`` and dropped the clause client-side -- the
        very field whose ``rule`` description tells the model to prefer it over
        inlining "-- NOT: ...". Assert the whole dict: the sibling
        ``test_learn_preference`` pins the no-negative shape, so together they
        lock the key in when a clause is supplied and out when it is not.
        """
        with patch("kiro_crew.mcp_core._post") as mock_post:
            mock_post.return_value = {"status": "ok"}
            result = self._simulate_tool_call(
                "learn_add",
                {
                    "rule": "Use pytest for testing",
                    "category": "tool",
                    "negative": "Do not use unittest directly",
                },
            )
        assert "Saved lesson" in result
        mock_post.assert_called_once_with(
            "/api/lessons",
            {
                "rule": "Use pytest for testing",
                "category": "tool",
                "scope": "global",
                "negative": "Do not use unittest directly",
            },
        )

    def test_learn_category_defaults_to_knowledge(self):
        """LLM might omit category — should default to 'knowledge'."""
        with patch("kiro_crew.mcp_core._post") as mock_post:
            mock_post.return_value = {"status": "ok"}
            result = self._simulate_tool_call(
                "learn_add",
                {
                    "rule": "The project uses Python 3.10",
                },
            )
        assert "Saved lesson" in result
        call_body = mock_post.call_args[0][1]
        assert call_body["category"] == "knowledge"

    # -- learn_list: user says "what have I taught you?" --

    def test_learn_list(self):
        with patch("kiro_crew.mcp_core._get") as mock_get:
            mock_get.return_value = {
                "lessons": [
                    {"rule": "use dark mode", "category": "preference"},
                    {"rule": "prefer pytest", "category": "tool"},
                ]
            }
            result = self._simulate_tool_call("learn_list", {})
        assert "dark mode" in result
        assert "pytest" in result

    def test_learn_list_empty(self):
        with patch("kiro_crew.mcp_core._get") as mock_get:
            mock_get.return_value = {"lessons": []}
            result = self._simulate_tool_call("learn_list", {})
        assert "No lessons" in result

    # -- learn_remove: user says "forget the dark mode rule" --

    def test_learn_remove(self):
        with patch("kiro_crew.mcp_core._delete") as mock_del:
            mock_del.return_value = {"removed": 1}
            result = self._simulate_tool_call(
                "learn_remove",
                {
                    "query": "dark mode",
                },
            )
        assert "Removed" in result

    # -- spawn_list: user says "what's running in the background?" --

    def test_spawn_list_empty(self):
        with patch("kiro_crew.mcp_core._get") as mock_get:
            mock_get.return_value = {"agents": []}
            result = self._simulate_tool_call("spawn_list", {})
        assert "No subagents" in result

    # -- spawn_status: user says "get the full output from that subagent" --

    def test_spawn_status_returns_full_result(self):
        with patch("kiro_crew.mcp_core._get") as mock_get:
            mock_get.return_value = {"result": "A" * 5000}
            result = self._simulate_tool_call("spawn_status", {"agent_id": "abc123"})
        assert len(result) == 5000
        mock_get.assert_called_with("/api/spawn/abc123")

    def test_spawn_status_not_found(self):
        with patch("kiro_crew.mcp_core._get") as mock_get:
            mock_get.return_value = {"error": "not found"}
            result = self._simulate_tool_call("spawn_status", {"agent_id": "bad"})
        assert "Error" in result

    def test_spawn_status_missing_id(self):
        result = self._simulate_tool_call("spawn_status", {})
        assert "required" in result.lower()

    def test_spawn_status_non_string_id(self):
        result = self._simulate_tool_call("spawn_status", {"agent_id": 123})
        assert "Error" in result

    def test_spawn_status_rejects_non_alnum_id(self):
        result = self._simulate_tool_call("spawn_status", {"agent_id": "../../etc"})
        assert "invalid" in result.lower()

    def test_spawn_status_redacts_credentials(self):
        with patch("kiro_crew.mcp_core._get") as mock_get:
            mock_get.return_value = {"result": "Found key AKIAIOSFODNN7EXAMPLE in output"}
            result = self._simulate_tool_call("spawn_status", {"agent_id": "abc123"})
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "[REDACTED" in result

    # -- task_run: user says "run this task spec" --

    def test_task_run_from_file(self):
        with patch("kiro_crew.mcp_core._post") as mock_post:
            mock_post.return_value = {"task_id": "my-task_123"}
            result = self._simulate_tool_call(
                "task_run",
                {
                    "spec": "/path/to/TASK.md",
                },
            )
        assert "Task runner started" in result

    def test_task_run_inline(self):
        with patch("kiro_crew.mcp_core._post") as mock_post:
            mock_post.return_value = {"task_id": "inline_123"}
            result = self._simulate_tool_call(
                "task_run",
                {
                    "spec": "__inline__:# Goal\nRefactor the session module",
                },
            )
        assert "Task runner started" in result

    # -- unknown tool: should return clean error --

    def test_unknown_tool(self):
        result = self._simulate_tool_call("nonexistent_tool", {"x": 1})
        assert "Unknown tool" in result


# ── MCP Cron: simulate kiro-cli calling cron tools ──


class TestMcpCronUserActions:
    """Simulate the exact JSON-RPC calls kiro-cli sends to kirocrew-cron."""

    def _simulate_tool_call(self, tool_name: str, arguments: dict) -> str:
        from kiro_crew.mcp_cron import _call_tool

        return _call_tool(tool_name, arguments)

    # -- cron_add: user says "check my pipeline every 5 minutes" --

    def test_add_every_interval(self, tmp_path):
        with patch("kiro_crew.mcp_cron.CronService") as mock_svc:
            svc = mock_svc.return_value
            job = MagicMock()
            job.id = "abc123"
            job.name = "pipeline check"
            job.schedule = MagicMock()
            job.schedule.kind = "every"
            job.schedule.every_secs = 300
            job.schedule.cron_expr = None
            job.schedule.at_ts = None
            job.agent_id = ""
            svc.add_job.return_value = job
            result = self._simulate_tool_call(
                "cron_add",
                {
                    "name": "pipeline check",
                    "message": "check the status of my deployment pipeline",
                    "every": 300,
                },
            )
        assert "abc123" in result
        assert "pipeline check" in result

    # -- cron_add with cron expression: "weekdays at 9am" --

    def test_add_cron_expression(self):
        with patch("kiro_crew.mcp_cron.CronService") as mock_svc:
            svc = mock_svc.return_value
            job = MagicMock()
            job.id = "def456"
            job.name = "standup"
            job.schedule = MagicMock()
            job.schedule.kind = "cron"
            job.schedule.cron_expr = "0 9 * * 1-5"
            job.schedule.every_secs = None
            job.schedule.at_ts = None
            job.agent_id = ""
            svc.add_job.return_value = job
            result = self._simulate_tool_call(
                "cron_add",
                {
                    "name": "standup",
                    "message": "summarize yesterday's work",
                    "cron_expr": "0 9 * * 1-5",
                },
            )
        assert "def456" in result

    # -- cron_add with agent: "use customer360 agent for this job" --

    def test_add_with_agent(self):
        with patch("kiro_crew.mcp_cron.CronService") as mock_svc:
            svc = mock_svc.return_value
            job = MagicMock()
            job.id = "ghi789"
            job.name = "c360 check"
            job.schedule = MagicMock()
            job.schedule.kind = "every"
            job.schedule.every_secs = 600
            job.schedule.cron_expr = None
            job.schedule.at_ts = None
            job.agent_id = ""
            svc.add_job.return_value = job
            result = self._simulate_tool_call(
                "cron_add",
                {
                    "name": "c360 check",
                    "message": "check c360 pipeline",
                    "every": 600,
                    "agent": "customer360-code-agent",
                },
            )
        assert "ghi789" in result
        # #391: the field is now folded into add_job's single locked save, not
        # mutated onto the job afterward -- assert it was passed INTO add_job.
        assert svc.add_job.call_args.kwargs["agent_id"] == "customer360-code-agent"

    # -- cron_add with approval_mode: "auto-approve tools for this cron" --

    def test_add_with_approval_mode_auto(self):
        with patch("kiro_crew.mcp_cron.CronService") as mock_svc:
            svc = mock_svc.return_value
            job = MagicMock()
            job.id = "appr001"
            job.name = "auto review"
            job.schedule = MagicMock()
            job.schedule.kind = "cron"
            job.schedule.cron_expr = "0 16 * * 1-5"
            job.schedule.every_secs = None
            job.schedule.at_ts = None
            job.agent_id = ""
            job.approval_mode = ""
            svc.add_job.return_value = job
            result = self._simulate_tool_call(
                "cron_add",
                {
                    "name": "auto review",
                    "message": "review CRs",
                    "cron_expr": "0 16 * * 1-5",
                    "agent": "gaia-cr-review",
                    "approval_mode": "auto",
                },
            )
        assert "appr001" in result
        # #391: folded into add_job's single locked save (not post-hoc mutation).
        assert svc.add_job.call_args.kwargs["agent_id"] == "gaia-cr-review"
        assert svc.add_job.call_args.kwargs["approval_mode"] == "auto"

    def test_add_with_approval_mode_empty(self):
        with patch("kiro_crew.mcp_cron.CronService") as mock_svc:
            svc = mock_svc.return_value
            job = MagicMock()
            job.id = "appr002"
            job.name = "default approval"
            job.schedule = MagicMock()
            job.schedule.kind = "every"
            job.schedule.every_secs = 300
            job.schedule.cron_expr = None
            job.schedule.at_ts = None
            job.agent_id = ""
            job.approval_mode = ""
            svc.add_job.return_value = job
            result = self._simulate_tool_call(
                "cron_add",
                {
                    "name": "default approval",
                    "message": "check stuff",
                    "every": 300,
                },
            )
        assert "appr002" in result
        # approval_mode should remain empty (not set)
        assert job.approval_mode == ""

    # -- cron_add without agent (most common): should work fine --

    def test_add_without_agent(self):
        with patch("kiro_crew.mcp_cron.CronService") as mock_svc:
            svc = mock_svc.return_value
            job = MagicMock()
            job.id = "noagent1"
            job.name = "basic"
            job.schedule = MagicMock()
            job.schedule.kind = "every"
            job.schedule.every_secs = 120
            job.schedule.cron_expr = None
            job.schedule.at_ts = None
            job.agent_id = ""
            svc.add_job.return_value = job
            result = self._simulate_tool_call(
                "cron_add",
                {
                    "name": "basic",
                    "message": "hello",
                    "every": 120,
                },
            )
        assert "noagent1" in result
        # agent_id should NOT have been set
        assert job.agent_id == ""

    # -- cron_list: user says "what cron jobs do I have?" --

    def test_list_jobs(self):
        with patch("kiro_crew.mcp_cron.CronService") as mock_svc:
            svc = mock_svc.return_value
            job = MagicMock()
            job.id = "list1"
            job.name = "my job"
            job.message = "do stuff"
            job.enabled = True
            job.schedule = MagicMock()
            job.schedule.kind = "every"
            job.schedule.every_secs = 300
            job.schedule.cron_expr = None
            job.schedule.at_ts = None
            svc.list_jobs.return_value = [job]
            result = self._simulate_tool_call("cron_list", {})
        assert "my job" in result
        assert "list1" in result

    def test_list_empty(self):
        with patch("kiro_crew.mcp_cron.CronService") as mock_svc:
            svc = mock_svc.return_value
            svc.list_jobs.return_value = []
            result = self._simulate_tool_call("cron_list", {})
        assert "No cron jobs" in result

    # -- cron_remove/pause/resume --

    def test_remove_job(self):
        with patch("kiro_crew.mcp_cron.CronService") as mock_svc:
            svc = mock_svc.return_value
            svc.remove_job.return_value = True
            result = self._simulate_tool_call("cron_remove", {"job_id": "abc12345"})
        assert "Removed" in result

    def test_pause_job(self):
        with patch("kiro_crew.mcp_cron.CronService") as mock_svc:
            svc = mock_svc.return_value
            svc.enable_job.return_value = True
            result = self._simulate_tool_call("cron_pause", {"job_id": "abc12345"})
        assert "Paused" in result

    def test_resume_job(self):
        with patch("kiro_crew.mcp_cron.CronService") as mock_svc:
            svc = mock_svc.return_value
            svc.enable_job.return_value = True
            result = self._simulate_tool_call("cron_resume", {"job_id": "abc12345"})
        assert "Resumed" in result

    # -- cron_remove_all --

    def test_remove_all(self):
        with patch("kiro_crew.mcp_cron.CronService") as mock_svc, patch.dict(
            "os.environ", {"KIROCREW_CLI": "1"}, clear=False
        ) as env:
            env.pop("KIROCREW_SESSION_KEY", None)
            svc = mock_svc.return_value
            job = MagicMock()
            job.id = "x"
            job.session_key = ""
            svc.list_jobs.return_value = [job]
            svc.remove_job.return_value = True
            result = self._simulate_tool_call("cron_remove_all", {})
        assert "Removed 1" in result


# ── JSON-RPC Envelope: simulate kiro-cli protocol ──


class TestJsonRpcProtocol:
    """Verify the JSON-RPC envelope handling matches kiro-cli's expectations."""

    def test_initialize_handshake(self):
        """kiro-cli sends initialize as the first message."""
        from kiro_crew.validation import validate_jsonrpc_request

        method, rid, params = validate_jsonrpc_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "clientInfo": {"name": "kiro-cli", "version": "1.0.0"},
                },
            }
        )
        assert method == "initialize"
        assert rid == 1

    def test_tools_call(self):
        """kiro-cli sends tools/call with name and arguments."""
        from kiro_crew.validation import validate_jsonrpc_request

        method, rid, params = validate_jsonrpc_request(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "cron_add",
                    "arguments": {"name": "test", "message": "hi", "every": 60},
                },
            }
        )
        assert method == "tools/call"

    def test_notification_no_id(self):
        """kiro-cli sends notifications/initialized with no id."""
        from kiro_crew.validation import validate_jsonrpc_request

        method, rid, params = validate_jsonrpc_request(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }
        )
        assert method == "notifications/initialized"
        assert rid is None


# ── Validation: verify bad inputs are caught without affecting good ones ──


class TestBadInputsCaught:
    """Verify that malicious/malformed inputs are rejected cleanly."""

    def _core_call(self, name: str, args: dict) -> str:
        from kiro_crew.mcp_core import _call_tool

        return _call_tool(name, args)

    def _cron_call(self, name: str, args: dict) -> str:
        from kiro_crew.mcp_cron import _call_tool

        return _call_tool(name, args)

    def test_spawn_empty_task(self):
        result = self._core_call("spawn_run", {"task": ""})
        assert "Error" in result

    def test_spawn_task_with_hidden_unicode(self):
        """Zero-width chars should be stripped, not cause errors."""
        with patch("kiro_crew.mcp_core._post") as mock_post:
            mock_post.return_value = {"id": "clean1"}
            result = self._core_call(
                "spawn_run",
                {"task": "search\u200b for\u200d files"},
            )
        assert "clean1" in result
        # Verify the API received cleaned text
        call_body = mock_post.call_args[0][1]
        assert "\u200b" not in call_body["task"]
        # ZWJ between two ASCII characters shapes nothing, so it is stripped
        # here too — that is what stops an invisible from hiding a credential
        # from redaction. It survives only beside non-ASCII text (emoji
        # sequences, Arabic / Persian / Indic), covered in test_validation.py.
        assert "\u200d" not in call_body["task"]

    def test_learn_invalid_category(self):
        result = self._core_call(
            "learn_add",
            {
                "rule": "test",
                "category": "evil_category",
            },
        )
        assert "Error" in result
        assert "must be one of" in result

    def test_cron_interval_too_small(self):
        result = self._cron_call(
            "cron_add",
            {
                "name": "spam",
                "message": "flood",
                "every": 5,  # below 60s minimum
            },
        )
        assert "Error" in result
        assert ">= 60" in result

    def test_extra_fields_rejected(self):
        result = self._core_call(
            "spawn_run",
            {
                "task": "test",
                "injected_field": "malicious",
            },
        )
        assert "Error" in result
        assert "unknown field" in result

    def test_wrong_type_rejected(self):
        result = self._core_call(
            "spawn_run",
            {
                "task": 12345,  # should be string
            },
        )
        assert "Error" in result

    def test_oversized_response_truncated(self):
        """Responses > 100K are truncated at the MCP protocol layer."""
        large_text = "x" * 200_000
        from kiro_crew.validation import build_tool_response

        response = build_tool_response(large_text)
        assert len(response["content"][0]["text"]) < 150_000
        assert "truncated" in response["content"][0]["text"]


# ── Dashboard API body validation helpers ──


class TestDashboardApiPatterns:
    """Simulate dashboard REST API input patterns."""

    def test_lesson_create_body(self):
        """POST /api/lessons body validation."""
        from kiro_crew.validation import (
            ALLOWED_LESSON_CATEGORIES,
            validate_api_body,
            validate_string_field,
        )

        body = validate_api_body({"rule": "use dark mode", "category": "preference"})
        rule = validate_string_field(body, "rule", required=True, max_len=500)
        cat = validate_string_field(body, "category", allowed=ALLOWED_LESSON_CATEGORIES)
        assert rule == "use dark mode"
        assert cat == "preference"

    def test_cron_create_body(self):
        """POST /api/crons body validation."""
        from kiro_crew.validation import validate_api_body, validate_string_field

        body = validate_api_body(
            {
                "name": "check pipeline",
                "message": "check deployment status",
                "every": 300,
            }
        )
        name = validate_string_field(body, "name", required=True, max_len=500)
        msg = validate_string_field(body, "message", required=True, max_len=5000)
        assert name == "check pipeline"
        assert msg == "check deployment status"

    def test_chat_message_body(self):
        """POST /api/chat body validation."""
        from kiro_crew.validation import validate_api_body, validate_string_field

        body = validate_api_body({"message": "what's the status of my pipeline?"})
        msg = validate_string_field(body, "message", required=True, max_len=50_000)
        assert msg == "what's the status of my pipeline?"

    def test_skill_create_body(self):
        """POST /api/skills body validation."""
        from kiro_crew.validation import validate_api_body, validate_string_field

        body = validate_api_body(
            {
                "name": "my-skill",
                "content": "---\nname: my-skill\n---\n# My Skill\nDo stuff.",
            }
        )
        name = validate_string_field(body, "name", required=True, max_len=100)
        content = validate_string_field(body, "content", required=True, max_len=50_000)
        assert name == "my-skill"
        assert "My Skill" in content
