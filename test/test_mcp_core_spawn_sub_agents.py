"""Tests for spawn_sub_agents MCP tool handler."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import mcp_core
from kiro_crew.mcp_core import _call_tool, _call_tool_deferrable
from kiro_crew.mcp_shared import DeferredTool, _DeferredEntry, drive_deferred
from kiro_crew.mcp_tools.spawn import _SubAgentsStep, spawn_sub_agents


def _call_after_submission(args, clock, poll_at):
    """Start the wait clock after the last accepted submission, then advance it."""
    clock.monotonic.return_value = 0
    deferred = _call_tool_deferrable("spawn_sub_agents", args)
    assert isinstance(deferred, DeferredTool)
    for entry in args["agents"]:
        if entry.get("prompt", "").strip():
            assert deferred.step() is None
    clock.monotonic.return_value = poll_at
    return drive_deferred(deferred, clock=clock)


class TestDeferredSubmission:
    def test_handler_returns_before_any_submission(self, monkeypatch):
        post = MagicMock()
        audit = MagicMock()
        monkeypatch.setattr(mcp_core, "_post", post)
        monkeypatch.setattr(mcp_core, "sel", lambda: audit)
        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:owner")
        step = spawn_sub_agents("spawn_sub_agents", {"agents": [{"prompt": "task"}]})
        assert isinstance(step, DeferredTool)
        assert step.phase == "submit"
        assert step.sa_ids == [] and step.sa_errors == [] and step.sa_deferred == set()
        post.assert_not_called()
        audit.log_api_access.assert_called_once()
        assert audit.log_tool_invocation.call_args.kwargs["outcome"] == "attempt"

    def test_each_step_posts_one_member_and_retains_all_outcomes(self, monkeypatch, manual_clock):
        manual_clock.install(monkeypatch, mcp_core)
        responses = iter([{"id": "a1"}, {"id": "a2", "status": "queued"},
                          {"error": "capacity reached"}, {}])

        def submit(path, body, **_kwargs):
            manual_clock.advance(8)
            return next(responses)

        post = MagicMock(side_effect=submit)
        get = MagicMock()
        monkeypatch.setattr(mcp_core, "_post", post)
        monkeypatch.setattr(mcp_core, "_get", get)
        monkeypatch.setattr(mcp_core, "sel", MagicMock())
        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:owner")
        agents = [
            {"agent_or_mode": "coder", "prompt": " code it "},
            {"agent_or_mode": "reviewer", "prompt": "review it"},
            {"prompt": "doomed task"}, {"prompt": "missing id"}, {"prompt": " "},
        ]
        step = spawn_sub_agents("spawn_sub_agents", {
            "agents": agents, "cwd": "/workspace/project", "include_memory": False,
            "include_lessons": False, "include_project": False,
        })
        assert isinstance(step, _SubAgentsStep)
        for index in range(4):
            assert step.step() is None
            assert post.call_count == index + 1
            assert step.due_at() == manual_clock.monotonic()
            assert step.phase == ("poll" if index == 3 else "submit")
        assert step.sa_ids == ["a1", "a2"]
        assert step.sa_deferred == {"a2"}
        assert step.sa_errors == ["doomed task: capacity reached",
                                  "missing id: spawn returned no agent id"]
        for index, call in enumerate(post.call_args_list):
            assert call.args == ("/api/spawn", {
                "task": agents[index]["prompt"].strip(),
                "agent": agents[index].get("agent_or_mode", ""),
                "parent_session": "dashboard:owner", "cwd": "/workspace/project",
                "include_memory": False, "include_lessons": False, "include_project": False,
            })
        get.assert_not_called()
        assert step.deadline == manual_clock.monotonic() + step.max_wait
        assert step.hold.deadline == step.deadline
        assert step.next_ping == manual_clock.monotonic() + step.KEEPALIVE_SECS

    def test_large_batch_keeps_progress_past_the_gateway_ceiling(self, monkeypatch, manual_clock):
        from kiro_crew.mcp_gateway.backend import PROGRESS_WEDGE_CEILING_SECS

        members = 128
        manual_clock.install(monkeypatch, mcp_core)
        ids = iter(f"a{i}" for i in range(members))

        def submit(path, body, **_kwargs):
            manual_clock.advance(8)
            return {"id": next(ids)}

        post = MagicMock(side_effect=submit)
        monkeypatch.setattr(mcp_core, "_post", post)
        monkeypatch.setattr(mcp_core, "sel", MagicMock())
        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:owner")
        step = spawn_sub_agents("spawn_sub_agents", {
            "agents": [{"prompt": "task"} for _ in range(members)],
        })
        entry = _DeferredEntry("call", "spawn_sub_agents", None, "", step)
        started = manual_clock.monotonic()
        for index in range(members):
            assert step.step() is None
            assert post.call_count == index + 1
            assert entry.progressing(manual_clock.monotonic())
        assert manual_clock.monotonic() - started > PROGRESS_WEDGE_CEILING_SECS
        assert step.phase == "poll" and len(step.sa_ids) == members
        # The polling step retains no member prompts, and nothing reads as unsubmitted.
        assert step.agents_input == []
        assert "not_submitted" not in step.abandon()
        assert step.deadline == manual_clock.monotonic() + step.max_wait

    def test_a_member_keeps_only_its_two_fields_while_parked(self, monkeypatch):
        """The parked step retains every member until it is submitted, so the
        handler copies only ``prompt`` and ``agent_or_mode`` (clamped) into the
        step; any other key the caller sent is dropped, not retained and not
        refused -- the advertised schema never promised to refuse it."""
        from kiro_crew.validation import MAX_MEDIUM_STRING, MAX_SHORT_STRING

        post = MagicMock(return_value={"id": "a1"})
        monkeypatch.setattr(mcp_core, "_post", post)
        monkeypatch.setattr(mcp_core, "sel", lambda: MagicMock())
        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:owner")
        from kiro_crew.validation import SPAWN_SUB_AGENTS_SCHEMA, validate_tool_args

        agents = [
            {"prompt": "p" * 50_000, "agent_or_mode": "m" * 5_000, "extra": {"payload": "x" * 1024}},
            {"prompt": "do work", "name": "ignored"},
        ]
        # The advertised schema accepts the member as it always did.
        cleaned = validate_tool_args({"agents": agents}, SPAWN_SUB_AGENTS_SCHEMA)
        step = spawn_sub_agents("spawn_sub_agents", cleaned)
        assert isinstance(step, _SubAgentsStep)
        post.assert_not_called()
        assert step.agents_input == [
            {"prompt": "p" * MAX_MEDIUM_STRING, "agent_or_mode": "m" * MAX_SHORT_STRING},
            {"prompt": "do work", "agent_or_mode": ""},
        ]
        assert not any("extra" in m or "name" in m for m in step.agents_input)

    @pytest.mark.parametrize("agents, response, expected", [
        ([{"prompt": "task"}], {"error": "capacity reached"},
         "Error spawning sub-agents:\n  - task: capacity reached"),
        ([{"prompt": " "}], {}, "Error: no valid agent entries found in 'agents' array"),
    ])
    def test_early_errors_settle_without_collecting(self, monkeypatch, agents, response, expected):
        post = MagicMock(return_value=response)
        get = MagicMock()
        audit = MagicMock()
        monkeypatch.setattr(mcp_core, "_post", post)
        monkeypatch.setattr(mcp_core, "_get", get)
        monkeypatch.setattr(mcp_core, "sel", lambda: audit)
        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:owner")
        with patch("kiro_crew.mcp_shared.sel", return_value=audit):
            step = _call_tool_deferrable("spawn_sub_agents", {"agents": agents})
            assert isinstance(step, DeferredTool)
            post.assert_not_called()
            text = step.step()
            assert text == expected
            step.on_settled(text)
        assert post.call_count == (1 if agents[0]["prompt"].strip() else 0)
        get.assert_not_called()
        terminal = [call.kwargs for call in audit.log_tool_invocation.call_args_list
                    if call.kwargs["source"] == "mcp"]
        assert len(terminal) == 1 and terminal[0]["error"] in ("", "execution_failed")
        # A batch that settles from the submit phase ran no collect, so the tool's
        # own outcome row (completed/partial + tallies) is not written: the only
        # mcp_core row is the attempt.
        own = [call.kwargs for call in audit.log_tool_invocation.call_args_list
               if call.kwargs["source"] == "mcp_core"]
        assert [row["outcome"] for row in own] == ["attempt"]

    @pytest.mark.parametrize("submitted", [0, 1, 2])
    def test_abandon_during_submission_names_accepted_and_unsubmitted(self, monkeypatch, submitted):
        post = MagicMock(side_effect=[{"id": "a1"}, {"error": "capacity reached"}])
        get = MagicMock()
        monkeypatch.setattr(mcp_core, "_post", post)
        monkeypatch.setattr(mcp_core, "_get", get)
        monkeypatch.setattr(mcp_core, "sel", MagicMock())
        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:owner")
        step = spawn_sub_agents("spawn_sub_agents", {
            "agents": [{"prompt": "one"}, {"prompt": "two"}, {"prompt": "three"}],
        })
        for _ in range(submitted):
            assert step.step() is None
        step.cancel()  # accepted children keep running; cancellation sends no POST
        text = step.abandon()
        if submitted == 0:
            # Nothing accepted yet: the whole call is retryable, so no final text.
            assert text is None
            assert post.call_count == 0
            get.assert_not_called()
            return
        records = [json.loads(block) for block in text.split("\n\n")]
        assert records[0]["task_ids"] == ["a1"]
        assert "Do not spawn them again" in records[0]["note"]
        assert records[1]["status"] == "not_submitted"
        assert records[1]["count"] == 3 - submitted
        assert "NOT submitted or spawned" in records[1]["note"]
        if submitted == 2:
            assert records[2]["status"] == "spawn_errors"
        step.on_settled("abandoned")
        assert post.call_count == submitted
        get.assert_not_called()


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
                "solo_reason": "bulk_data",
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
                "solo_reason": "bulk_data",
            })

            assert "Error spawning" in result
            assert "no agent id" in result
            # The poll endpoint must never be hit with an empty id.
            assert mock_get.call_count == 0

    def test_wait_expiry_reports_still_running_never_failed(self):
        """The blocking wait ending is a fact about the CALL, not the children:
        they are reported still_running with their ids, states and how to poll,
        never marked timed out or failed, and never cancelled."""
        import json

        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.time") as mock_time, \
             patch("kiro_crew.mcp_core.sel") as mock_sel, \
             patch("kiro_crew.mcp_shared.sel", side_effect=lambda: mock_sel.return_value), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.return_value = {"id": "a1"}
            mock_get.return_value = {"done": False, "agent": "slow"}
            mock_time.sleep = lambda _: None

            result = _call_after_submission({
                "agents": [{"prompt": "long task"}],
                "solo_reason": "bulk_data",
            }, mock_time, 999999)

            assert '"timed_out"' not in result
            assert '"failed"' not in result
            envelope = json.loads(result.split("\n\n")[-1])
            assert envelope["status"] == "still_running"
            assert envelope["task_ids"] == ["a1"]
            assert envelope["states"] == {"a1": "running"}
            assert envelope["query"] == "spawn_status/spawn_list"
            assert envelope["waited_secs"] == 7200
            # nothing was cancelled or marked collected for the live child
            assert not any(
                call.args and "cancel" in str(call.args[0]) for call in mock_post.call_args_list
            )
            assert not any(
                call.args and call.args[0] == "/api/spawn/mark-collected"
                for call in mock_post.call_args_list
            )
            completed_rows = [
                call for call in mock_sel.return_value.log_tool_invocation.call_args_list
                if call.kwargs.get("outcome") == "completed"
            ]
            assert len(completed_rows) == 1
            assert completed_rows[0].kwargs["source"] == "mcp"

    def test_wait_expiry_reports_queued_and_permission_states(self):
        import json

        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.time") as mock_time, \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.side_effect = [{"id": "q1"}, {"id": "p1"}]
            by_id = {
                "/api/spawn/q1": {"done": False, "queued": True},
                "/api/spawn/p1": {"done": False, "awaiting_approval": True},
            }
            mock_get.side_effect = lambda path, *a, **k: by_id.get(path, {"done": False})
            mock_time.sleep = lambda _: None

            result = _call_after_submission(
                {"agents": [{"prompt": "a"}, {"prompt": "b"}]}, mock_time, 999999,
            )

            records = [json.loads(chunk) for chunk in result.split("\n\n")]
            still = [r for r in records if r.get("status") == "still_running"]
            assert still and still[0]["states"] == {"p1": "waiting_permission"}
            # A member queued and not started is its own record, never "running".
            queued = [r for r in records if r.get("status") == "queued"]
            assert queued and list(queued[0]["agents"]) == ["q1"]

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
            mock_time.sleep = lambda _: None

            _call_after_submission({
                "agents": [{"prompt": "slow task"}], "solo_reason": "bulk_data",
            }, mock_time, 70)

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

            result = _call_tool("spawn_sub_agents", {"agents": [{"prompt": "task"}], "solo_reason": "bulk_data"})

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
            mock_time.sleep = lambda _: None

            result = _call_after_submission({
                "agents": [{"prompt": "t"}], "solo_reason": "bulk_data",
            }, mock_time, 200)

            assert '"still_running"' in result
            assert '"waited_secs": 120' in result

    def test_reports_errored_agents(self):
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.return_value = {"id": "a1"}
            mock_get.return_value = {"done": True, "error": "crashed", "agent": "bad"}

            result = _call_tool("spawn_sub_agents", {
                "agents": [{"prompt": "task"}],
                "solo_reason": "bulk_data",
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
                "solo_reason": "bulk_data",
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
                "solo_reason": "bulk_data",
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
                "solo_reason": "bulk_data",
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
                "solo_reason": "bulk_data",
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

            result = _call_tool("spawn_sub_agents", {"agents": [{"prompt": "task"}], "solo_reason": "bulk_data"})

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

            result = _call_tool("spawn_sub_agents", {"agents": [{"prompt": "task"}], "solo_reason": "bulk_data"})

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

            _call_tool("spawn_sub_agents", {"agents": [{"prompt": "task"}], "solo_reason": "bulk_data"})

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
            result = _call_tool("spawn_sub_agents", {"agents": [{"prompt": "task"}], "solo_reason": "bulk_data"})

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

            result = _call_tool("spawn_sub_agents", {"agents": [{"prompt": "task"}], "solo_reason": "bulk_data"})

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

            result = _call_tool("spawn_sub_agents", {"agents": [{"prompt": "task"}], "solo_reason": "bulk_data"})

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
            mock_time.sleep = lambda _: None

            result = _call_after_submission({
                "agents": [{"prompt": "task"}], "solo_reason": "bulk_data",
            }, mock_time, 70)
            assert any(call.args[0] == "/api/session-keepalive"
                       for call in mock_post.call_args_list)
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

            result = _call_tool("spawn_sub_agents", {"agents": [{"prompt": "task"}], "solo_reason": "bulk_data"})

            assert '"completed"' in result
            # Slept at least once because the first poll was not-done.
            assert mock_get.call_count >= 2


class TestSpawnSubAgentsFailedChildTranscript:
    """A finished child that ended in error keeps the output it retained.

    On a finished run (``done`` is True) the server's ``error`` is the reason the
    run ended badly, sent next to the ``result`` the run retained. The reply
    carries that result as ``text``, with the same redaction and size cap as a
    completed child. A payload that is not a finished run (a failed poll) carries
    no result, and its block keeps its keys.
    """

    @staticmethod
    def _blocks(reply: str) -> list[dict]:
        import json

        return [json.loads(block) for block in reply.split("\n\n")]

    def _run(self, payload: dict, agent_id: str = "a1") -> list[dict]:
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.return_value = {"id": agent_id}
            mock_get.return_value = payload
            reply = _call_tool("spawn_sub_agents", {
                "agents": [{"prompt": "task"}],
                "solo_reason": "bulk_data",
            })
        return self._blocks(reply)

    def test_finished_child_that_ended_in_error_keeps_its_transcript(self):
        blocks = self._run({
            "done": True,
            "agent": "w",
            "error": "Timed out after 30 minutes",
            "result": "step 1 done; step 2 wrote notes.md",
        })

        assert blocks == [{
            "agent": "w",
            "status": "error",
            "error": "Timed out after 30 minutes",
            "text": "step 1 done; step 2 wrote notes.md",
        }]

    def test_failed_childs_transcript_is_redacted(self):
        blocks = self._run({
            "done": True,
            "agent": "w",
            "error": "crashed",
            "result": "key AKIAIOSFODNN7EXAMPLE found",
        })

        assert blocks[0]["status"] == "error"
        assert "found" in blocks[0]["text"]
        assert "AKIAIOSFODNN7EXAMPLE" not in blocks[0]["text"]

    def test_large_transcript_of_a_failed_child_is_summarized_with_its_path(self):
        from kiro_crew.context_management import COMPLETION_KEEP_DEFAULT_CHARS

        large = "word " * 1000
        assert len(large) > COMPLETION_KEEP_DEFAULT_CHARS
        with patch("kiro_crew.mcp_core.summarize_result") as mock_summarize:
            mock_summarize.return_value = "summary with path"
            blocks = self._run(
                {"done": True, "agent": "w", "error": "crashed", "result": large},
                agent_id="fail123",
            )

        mock_summarize.assert_called_once()
        text_arg, path_arg = mock_summarize.call_args[0]
        assert text_arg == large
        assert "fail123" in path_arg and path_arg.endswith("result.txt")
        assert blocks[0]["text"] == "summary with path"

    def test_failed_poll_of_an_unfinished_child_carries_no_text(self):
        blocks = self._run({"done": False, "agent": "w", "error": "HTTP 503"})

        assert blocks == [{"agent": "w", "status": "error", "error": "HTTP 503"}]

    def test_finished_child_that_ended_in_error_is_still_counted_as_errored(self):
        """The tool's own SEL row keeps the per-child tallies and the ``partial``
        outcome; the generic ``call_tool_with_logging`` row stays ``completed``."""
        with patch("kiro_crew.mcp_core._post") as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.sel") as mock_sel, \
             patch("kiro_crew.mcp_shared.sel", side_effect=lambda: mock_sel.return_value), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "s"}):
            mock_post.return_value = {"id": "a1"}
            mock_get.return_value = {
                "done": True, "agent": "w", "error": "crashed", "result": "partial",
            }
            result = _call_tool("spawn_sub_agents", {
                "agents": [{"prompt": "task"}],
                "solo_reason": "bulk_data",
            })
            assert '"status": "error"' in result
            assert '"text": "partial"' in result

        final = [
            c.kwargs for c in mock_sel.return_value.log_tool_invocation.call_args_list
            if c.kwargs.get("outcome") in ("completed", "partial")
        ]
        own = [c for c in final if c["source"] == "mcp_core"]
        assert len(own) == 1 and own[0]["outcome"] == "partial"
        assert own[0]["metadata"] == {
            "spawned": 1, "completed": 0, "still_running": 0, "errored": 1,
        }
        generic = [c for c in final if c["source"] == "mcp"]
        assert len(generic) == 1 and generic[0]["outcome"] == "completed"


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

            _call_tool("spawn_sub_agents", {"agents": [{"prompt": "task"}], "solo_reason": "bulk_data"})

            owners = self._audit_owners(mock_sel)
            # The tool's attempt carries the lost-owner marker; its terminal
            # invocation row belongs to the shared audited wrapper.
            assert len(owners) == 2  # the attempt row and the tool's own settle row
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

            _call_tool("spawn_sub_agents", {"agents": [{"prompt": "task"}], "solo_reason": "bulk_data"})

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

            _call_tool("spawn_sub_agents", {"agents": [{"prompt": "task"}], "solo_reason": "bulk_data"})

            spawn_bodies = [
                call.args[1] for call in mock_post.call_args_list
                if call.args and call.args[0] == "/api/spawn"
            ]
            assert spawn_bodies
            for body in spawn_bodies:
                assert body["parent_session"] == ""


class TestSpawnSubAgentsAbandon:
    """A pruned install exits while the parent waits on its children: the
    children were already POSTed and keep running, so the answer must name
    them and say not to spawn them again, never invite a retry."""

    def test_abandon_keeps_a_member_of_unknown_acceptance_from_being_resubmitted(self, monkeypatch):
        """A member whose POST failed in transport may have been accepted, so
        with no confirmed id the step still returns final text naming it under
        ``unknown_acceptance`` instead of ``None``; the retryable refusal would
        have the agent resubmit a child that may already run."""
        monkeypatch.setattr(
            mcp_core, "_post", MagicMock(return_value={"error": "timed out", "transport_error": True})
        )
        monkeypatch.setattr(mcp_core, "sel", MagicMock())
        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:owner")
        step = spawn_sub_agents("spawn_sub_agents", {"agents": [{"prompt": "a"}, {"prompt": "b"}]})
        assert step.step() is None  # member "a": transport error, acceptance unknown
        assert step.sa_ids == [] and len(step.sa_unknown) == 1
        text = step.abandon()
        assert text is not None
        records = [json.loads(block) for block in text.split("\n\n")]
        unknown = next(r for r in records if r["status"] == "unknown_acceptance")
        assert unknown["count"] == 1 and "Do not resubmit" in unknown["note"]
        assert any(r["status"] == "not_submitted" and r["count"] == 1 for r in records)

    def test_abandon_before_any_child_is_accepted_leaves_the_call_retryable(self, monkeypatch):
        """Turned away (table full, pool cannot start) before its first member
        was submitted, the step has nothing running: ``abandon()`` returns
        ``None`` so the loop's retryable refusal answers the call, instead of a
        'still running, do not spawn again' result with no ids."""
        monkeypatch.setattr(mcp_core, "_post", MagicMock(return_value={"id": "a1"}))
        monkeypatch.setattr(mcp_core, "sel", MagicMock())
        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:owner")
        step = spawn_sub_agents("spawn_sub_agents", {"agents": [{"prompt": "a"}, {"prompt": "b"}]})
        assert step.phase == "submit" and step.sa_ids == []
        assert step.abandon() is None
        assert step.phase == "submit", "a retryable call is not marked abandoned"
        # Once one child is accepted the final text names it and the one not submitted.
        assert step.step() is None
        text = step.abandon()
        assert text is not None and "a1" in text and '"count": 1' in text

    def test_abandon_names_every_spawned_child_and_forbids_a_respawn(self):
        import json

        from kiro_crew.mcp_tools.spawn import _SubAgentsStep, spawn_sub_agents

        ids = iter(["a1", "a2"])

        def _post(path, body, **_kw):
            if path == "/api/spawn":
                if body["task"] == "broken":
                    return {"error": "capacity reached"}
                return {"id": next(ids)}
            return {}

        with patch("kiro_crew.mcp_core._post", side_effect=_post) as mock_post, \
             patch("kiro_crew.mcp_core._get") as mock_get, \
             patch("kiro_crew.mcp_core.sel"), \
             patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "sess1"}):
            step = spawn_sub_agents("spawn_sub_agents", {
                "agents": [{"prompt": "one"}, {"prompt": "two"}, {"prompt": "broken"}],
            })
            assert isinstance(step, _SubAgentsStep)
            for _ in range(len(step.agents_input)):
                assert step.step() is None
            assert step.phase == "poll"
            posts_before = mock_post.call_count
            text = step.abandon()

        assert text is not None
        records = [json.loads(chunk) for chunk in text.split("\n\n")]
        running = records[0]
        assert running["status"] == "still_running"
        assert running["task_ids"] == ["a1", "a2"]
        assert "Do not spawn them again" in running["note"]
        assert "[Subagent completion event]" in running["note"]
        assert records[1]["status"] == "spawn_errors"
        assert "capacity reached" in records[1]["errors"][0]
        # Nothing is marked collected (or polled), so each child's completion
        # event still reaches the parent.
        assert mock_post.call_count == posts_before
        mock_get.assert_not_called()


class TestDeferredCollectionCommit:
    def test_collect_only_builds_text_and_settlement_marks_once(self, monkeypatch):
        from unittest.mock import MagicMock

        from kiro_crew import mcp_core
        from kiro_crew.mcp_tools.spawn import _SubAgentsStep

        post = MagicMock()
        audit = MagicMock()
        monkeypatch.setattr(mcp_core, "_post", post)
        monkeypatch.setattr(mcp_core, "sel", lambda: audit)
        monkeypatch.setattr(mcp_core, "_get", lambda path: {"done": True, "result": "ok"})
        step = _SubAgentsStep(
            sa_ids=["a1"], sa_deferred=set(), sa_errors=[], parent_session="dashboard:owner",
            max_wait=60, inline_result=lambda aid, text: text,
        )
        text = step._collect()
        assert '"completed"' in text
        post.assert_not_called()
        audit.log_tool_invocation.assert_not_called()
        step.on_settled(text)
        step.on_settled(text)
        # The tool's own row (tallies + outcome) is written once, at settle.
        audit.log_tool_invocation.assert_called_once()
        assert audit.log_tool_invocation.call_args.kwargs["outcome"] == "completed"
        assert audit.log_tool_invocation.call_args.kwargs["metadata"]["completed"] == 1
        post.assert_called_once_with(
            "/api/spawn/mark-collected",
            {"ids": ["a1"], "parent_session": "dashboard:owner"}, timeout=5,
        )

    def test_abandon_never_commits_a_collect_result(self, monkeypatch):
        from unittest.mock import MagicMock

        from kiro_crew import mcp_core
        from kiro_crew.mcp_tools.spawn import _SubAgentsStep

        post = MagicMock()
        monkeypatch.setattr(mcp_core, "_post", post)
        monkeypatch.setattr(mcp_core, "_get", lambda path: {"done": True, "result": "ok"})
        step = _SubAgentsStep(
            sa_ids=["a1"], sa_deferred=set(), sa_errors=[], parent_session="dashboard:owner",
            max_wait=60, inline_result=lambda aid, text: text,
        )
        text = step.abandon()
        step._collect()  # A stuck collect can finish after the abandonment decision.
        step.on_settled(text)
        post.assert_not_called()

    def test_batch_member_limit_is_checked_before_spawning(self, monkeypatch):
        """``agents`` shares ``SPAWN_BATCH_MEMBERS_MAX`` with ``spawn_run``: the
        parked step retains every member until submitted, so the count is bounded
        where it is retained and an oversized batch spawns nothing."""
        from unittest.mock import MagicMock

        from kiro_crew import mcp_core
        from kiro_crew.validation import (
            SPAWN_BATCH_MEMBERS_MAX,
            SPAWN_SUB_AGENTS_SCHEMA,
            validate_tool_args,
        )

        agents = [{"prompt": "task"} for _ in range(SPAWN_BATCH_MEMBERS_MAX)]
        cleaned = validate_tool_args({"agents": agents}, SPAWN_SUB_AGENTS_SCHEMA)["agents"]
        assert len(cleaned) == SPAWN_BATCH_MEMBERS_MAX
        post = MagicMock()
        monkeypatch.setattr(mcp_core, "_post", post)
        result = _call_tool("spawn_sub_agents", {"agents": agents + [{"prompt": "overflow"}]})
        assert f"exceeds max items {SPAWN_BATCH_MEMBERS_MAX}" in result
        post.assert_not_called()

    def test_a_parked_batch_bounds_each_refusal_line(self, monkeypatch):
        from unittest.mock import MagicMock

        from kiro_crew import mcp_core
        from kiro_crew.mcp_tools.spawn import _SA_ERROR_MAX_CHARS, _SubAgentsStep, spawn_sub_agents

        monkeypatch.setattr(mcp_core, "sel", lambda: MagicMock())
        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:owner")
        monkeypatch.setattr(mcp_core, "_post", MagicMock(side_effect=[
            {"id": "a1"}, {"error": "refused " * _SA_ERROR_MAX_CHARS}, {},
        ]))
        step = spawn_sub_agents("spawn_sub_agents", {"agents": [
            {"prompt": "running task"}, {"prompt": "refused task"}, {"prompt": "missing id"},
        ]})
        assert isinstance(step, _SubAgentsStep)
        for _ in range(len(step.agents_input)):
            assert step.step() is None
        assert step.phase == "poll"
        assert len(step.sa_errors) == 2
        assert len(step.sa_errors[0]) == _SA_ERROR_MAX_CHARS
        assert all(len(error) <= _SA_ERROR_MAX_CHARS for error in step.sa_errors)
