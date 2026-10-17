"""Per-task overrides in ``spawn_run`` ``tasks[]``.

A ``tasks`` entry may be an object ``{task, model?, reasoning_effort?}``
whose fields win over the call's batch-wide value for that task only, so one
wave can run the same prompt on two models and deliver the results together.
Plain string entries keep their exact old behaviour.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.validation import SPAWN_RUN_SCHEMA, ValidationError, validate_tool_args

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


def _run_tool(args: dict[str, Any], responses: list[dict] | None = None) -> tuple[list[dict], str]:
    """Run spawn_run against a fake gateway; return (POSTed bodies, result text)."""
    from kiro_crew import mcp_core

    bodies: list[dict] = []
    answers = iter(responses or [])

    def _fake_post(path: str, body: dict) -> dict:
        if path == "/api/spawn":
            bodies.append(body)
            return next(answers, {"id": f"a{len(bodies)}"})
        return {"id": "a1"}

    with (
        patch.object(mcp_core, "_post", side_effect=_fake_post),
        patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
        patch.object(mcp_core, "sel", MagicMock()),
    ):
        result = mcp_core._call_tool("spawn_run", args)
    return bodies, result


class TestSchema:
    def test_object_entry_is_accepted_and_cleaned(self):
        cleaned = validate_tool_args(
            {"tasks": ["plain", {"task": "p", "model": "claude-haiku-4.5"}]}, SPAWN_RUN_SCHEMA
        )
        assert cleaned["tasks"] == ["plain", {"task": "p", "model": "claude-haiku-4.5"}]

    def test_unknown_key_in_object_is_refused_with_its_path(self):
        with pytest.raises(ValidationError) as exc:
            validate_tool_args({"tasks": [{"task": "p", "cwd": "/tmp"}]}, SPAWN_RUN_SCHEMA)
        assert exc.value.field == "tasks[0].cwd"

    def test_object_without_task_is_refused(self):
        with pytest.raises(ValidationError) as exc:
            validate_tool_args({"tasks": [{"model": "claude-haiku-4.5"}]}, SPAWN_RUN_SCHEMA)
        assert exc.value.field == "tasks[0].task"

    @pytest.mark.parametrize(
        "item",
        [{"task": "p", "reasoning_effort": "ultra"}, {"task": "p", "model": "bad model!"}],
    )
    def test_object_fields_use_the_top_level_rules(self, item):
        with pytest.raises(ValidationError):
            validate_tool_args({"tasks": [item]}, SPAWN_RUN_SCHEMA)

    @pytest.mark.parametrize("bad", [1, None, ["x"], True])
    def test_other_item_types_are_still_refused(self, bad):
        with pytest.raises(ValidationError, match="expected str or dict"):
            validate_tool_args({"tasks": ["ok", bad]}, SPAWN_RUN_SCHEMA)


class TestForwarding:
    def test_per_task_model_overrides_batch_wide_in_one_wave(self):
        bodies, _ = _run_tool(
            {
                "tasks": [{"task": "review", "model": "gpt-6"}, "review"],
                "model": "claude-opus-5.5",
            }
        )
        assert [b["model"] for b in bodies] == ["gpt-6", "claude-opus-5.5"]
        # One wave: both members share a batch id, so one digest delivers both.
        assert bodies[0]["batch_id"] and bodies[0]["batch_id"] == bodies[1]["batch_id"]
        assert all(b["batch_total"] == 2 for b in bodies)

    def test_per_task_effort_overrides_and_agents_still_apply(self):
        bodies, _ = _run_tool(
            {
                "tasks": [{"task": "a", "reasoning_effort": "max"}, "b"],
                "agents": ["kirocrew", ""],
                "reasoning_effort": "low",
            }
        )
        assert [b["reasoning_effort"] for b in bodies] == ["max", "low"]
        assert [b["agent"] for b in bodies] == ["kirocrew", ""]

    def test_unset_override_falls_back_and_omits_when_nothing_set(self):
        bodies, _ = _run_tool({"tasks": [{"task": "a"}, {"task": "b", "model": "gpt-6"}]})
        assert "model" not in bodies[0]
        assert bodies[1]["model"] == "gpt-6"

    def test_string_entries_are_unchanged(self):
        bodies, _ = _run_tool({"tasks": ["t1", "t2"], "model": "m1"})
        assert [(b["task"], b["model"]) for b in bodies] == [("t1", "m1"), ("t2", "m1")]

    def test_agent_is_not_a_task_object_field(self):
        with pytest.raises(ValidationError) as exc:
            validate_tool_args({"tasks": [{"task": "a", "agent": "kirocrew"}]}, SPAWN_RUN_SCHEMA)
        assert exc.value.field == "tasks[0].agent"

    def test_effort_verdict_names_each_tasks_own_level(self):
        _, result = _run_tool(
            {"tasks": [{"task": "a", "reasoning_effort": "max"}, "b"], "reasoning_effort": "low"},
            responses=[
                {"id": "s1", "effort_dropped": "model auto"},
                {"id": "s2", "effort_dropped": "model auto"},
            ],
        )
        assert "reasoning_effort='max' dropped for s1: model auto" in result
        assert "reasoning_effort='low' dropped for s2: model auto" in result


class TestDeferredSubmission:
    @pytest.fixture
    def gateway(self, monkeypatch):
        from kiro_crew import mcp_core

        post = MagicMock(return_value={"id": "a1"})
        monkeypatch.setattr(mcp_core, "_post", post)
        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:owner")
        monkeypatch.setattr(mcp_core, "sel", MagicMock())
        return post

    def test_handler_returns_before_any_post_and_single_task_settles_in_one_step(self, gateway):
        from kiro_crew.mcp_tools.spawn import _SpawnRunStep, spawn_run

        step = spawn_run("spawn_run", {"task": "one"})
        assert isinstance(step, _SpawnRunStep)
        assert step.phase == "submit"
        gateway.assert_not_called()
        assert step.abandon() is None
        text = step.step()
        assert gateway.call_count == 1
        assert step.phase == "done"
        assert text.startswith("Spawned 1 subagent(s).")

    def test_each_step_posts_one_task_and_large_batch_stays_progressing(
        self, gateway, monkeypatch, manual_clock
    ):
        from kiro_crew import mcp_core
        from kiro_crew.mcp_gateway.backend import PROGRESS_WEDGE_CEILING_SECS
        from kiro_crew.mcp_shared import _DeferredEntry
        from kiro_crew.mcp_tools.spawn import spawn_run

        manual_clock.install(monkeypatch, mcp_core)
        count = 128  # An eight-second POST per member outlives the progress ceiling.

        def submit(path, body, **_kwargs):
            assert path == "/api/spawn"
            manual_clock.advance(8)
            return {"id": f"a{gateway.call_count}"}

        gateway.side_effect = submit
        step = spawn_run("spawn_run", {"tasks": [f"task {i}" for i in range(count)]})
        entry = _DeferredEntry("call", "spawn_run", None, "", step)
        start = manual_clock.monotonic()
        for index in range(count):
            text = step.step()
            assert gateway.call_count == index + 1
            assert gateway.call_args.args[1]["task"] == f"task {index}"
            assert step.due_at() == manual_clock.monotonic()
            assert entry.progressing(manual_clock.monotonic())
            assert (text is not None) == (index == count - 1)
        assert manual_clock.monotonic() - start > PROGRESS_WEDGE_CEILING_SECS
        assert len(step.agent_ids) == count
        assert f"Spawned {count} subagent(s)." in text

    def test_refused_name_skips_only_its_siblings_and_reconciles_each(self, gateway):
        from kiro_crew.mcp_tools.spawn import spawn_run
        from kiro_crew.subagent import AGENT_NOT_FOUND_CODE

        gateway.side_effect = [
            {"error": "unknown ghost", "code": AGENT_NOT_FOUND_CODE},
            {},
            {},
            {"id": "good"},
        ]
        step = spawn_run(
            "spawn_run", {"tasks": ["a", "b", "c"], "agents": ["ghost", "ghost", "scout"]}
        )
        assert step.step() is None
        assert [c.args[0] for c in gateway.call_args_list] == ["/api/spawn", "/api/spawn/lost"]
        assert step.step() is None
        assert gateway.call_args.args[0] == "/api/spawn/lost"
        text = step.step()
        assert [c.args[0] for c in gateway.call_args_list].count("/api/spawn") == 2
        lost = [c.args[1] for c in gateway.call_args_list if c.args[0] == "/api/spawn/lost"]
        assert len(lost) == 2 and all(b["batch_total"] == 3 for b in lost)
        assert len({b["batch_id"] for b in lost}) == 1
        assert "Spawned 1 subagent(s)." in text
        assert "b: not dispatched - agent 'ghost' refused above" in text

    @pytest.mark.parametrize("queued", [False, True])
    def test_abandon_and_cancel_preserve_accepted_ids_and_reconcile_unsubmitted(
        self, gateway, queued
    ):
        from kiro_crew.mcp_tools.spawn import spawn_run

        gateway.side_effect = [
            {"id": "accepted", "status": "queued" if queued else "spawned"},
            {"error": "refused", "counted": True},
        ]
        step = spawn_run("spawn_run", {"tasks": ["one", "two", "three"]})
        assert step.abandon() is None
        gateway.assert_not_called()
        assert step.step() is None
        assert step.step() is None
        before = gateway.call_count
        step.cancel()
        text = step.abandon()
        step.on_settled(text)
        after = [c.args for c in gateway.call_args_list[before:]]
        assert [path for path, _ in after] == ["/api/spawn/lost"]
        assert after[0][1]["batch_total"] == 3
        assert "accepted: one" in text
        assert "Do not spawn them again" in text
        assert "1 task(s) were NOT submitted" in text
        assert "digest does not wait on them" in text
        assert "two: refused" in text
        assert step.phase == "abandoned"

    @staticmethod
    def _lost_reasons(gateway):
        return [
            c.args[1]["reason"] for c in gateway.call_args_list if c.args[0] == "/api/spawn/lost"
        ]

    @pytest.mark.parametrize("exit_hook", ["cancel", "abandon"])
    def test_ending_mid_submission_reconciles_each_unsubmitted_task_once(self, gateway, exit_hook):
        from kiro_crew.mcp_tools.spawn import spawn_run

        gateway.side_effect = [{"id": "a0"}, {"id": "a1"}] + [{}] * 10
        step = spawn_run("spawn_run", {"tasks": [f"t{i}" for i in range(5)]})
        assert step.step() is None and step.step() is None
        before = gateway.call_count
        getattr(step, exit_hook)()
        after = gateway.call_args_list[before:]
        assert [c.args[0] for c in after] == ["/api/spawn/lost"] * 3
        assert {c.args[1]["batch_total"] for c in after} == {5}
        assert all("not submitted" in r for r in self._lost_reasons(gateway))

    def test_an_unanswering_gateway_costs_the_loop_one_bounded_lost_report(self, gateway):
        """``cancel()`` runs on the dispatch loop's thread: when the first lost
        report fails in transport the rest are left to the stuck-wave sweep,
        and the one attempt carries the short ``_LOST_REPORT_TIMEOUT_SECS``."""
        from kiro_crew.mcp_tools.spawn import _LOST_REPORT_TIMEOUT_SECS, spawn_run

        def answer(path, body, **kwargs):
            if path == "/api/spawn/lost":
                # ``_post`` reports a dead gateway as an error dict, not an exception.
                return {"error": "gateway down", "transport_error": True}
            return {"id": "a0"}

        gateway.side_effect = answer
        step = spawn_run("spawn_run", {"tasks": [f"t{i}" for i in range(6)]})
        assert step.step() is None
        before = gateway.call_count
        step.cancel()
        lost = [c for c in gateway.call_args_list[before:] if c.args[0] == "/api/spawn/lost"]
        assert len(lost) == 1
        assert lost[0].kwargs.get("timeout") == _LOST_REPORT_TIMEOUT_SECS
        step.abandon()
        assert gateway.call_count == before + 1  # closed once; no second round of attempts

    def test_cancel_then_abandon_reconciles_each_unsubmitted_task_once(self, gateway):
        from kiro_crew.mcp_tools.spawn import spawn_run

        gateway.side_effect = [{"id": "a0"}] + [{}] * 10
        step = spawn_run("spawn_run", {"tasks": ["a", "b", "c", "d"]})
        assert step.step() is None
        step.cancel()
        text = step.abandon()
        step.cancel()
        assert len(self._lost_reasons(gateway)) == 3
        assert "3 task(s) were NOT submitted" in text
        assert [c.args[0] for c in gateway.call_args_list].count("/api/spawn") == 1

    def test_refused_only_abandon_stays_retryable_and_still_closes_the_wave(self, gateway):
        from kiro_crew.mcp_tools.spawn import spawn_run

        gateway.side_effect = [{"error": "refused"}] + [{}] * 10
        step = spawn_run("spawn_run", {"tasks": ["a", "b", "c"]})
        assert step.step() is None
        assert step.abandon() is None
        # One loss for the refusal itself, then one for each unsubmitted task.
        assert len(self._lost_reasons(gateway)) == 3

    def test_fully_submitted_or_never_started_step_reconciles_nothing(self, gateway):
        from kiro_crew.mcp_tools.spawn import spawn_run

        untouched = spawn_run("spawn_run", {"tasks": ["a", "b"]})
        untouched.cancel()
        assert untouched.abandon() is None
        gateway.assert_not_called()
        gateway.side_effect = [{"id": "a0"}, {"id": "a1"}]
        step = spawn_run("spawn_run", {"tasks": ["a", "b"]})
        assert step.step() is None
        assert step.step() is not None
        step.cancel()
        step.abandon()
        assert [c.args[0] for c in gateway.call_args_list] == ["/api/spawn", "/api/spawn"]

    def test_all_refused_is_retryable_and_empty_tasks_settle_without_post(self, gateway):
        from kiro_crew.mcp_tools.spawn import spawn_run

        gateway.return_value = {"error": "refused", "counted": True}
        step = spawn_run("spawn_run", {"task": "one"})
        assert step.step().startswith("Error:")
        assert step.abandon() is None
        gateway.reset_mock()
        empty = spawn_run("spawn_run", {"tasks": [" "]})
        assert empty.step() == "Error: no subagents were started."
        gateway.assert_not_called()

    def test_receipt_keeps_exact_background_output(self):
        _, text = _run_tool({"tasks": ["one", "two"]}, responses=[{"id": "s1"}, {"id": "s2"}])
        assert text == (
            "Spawned 2 subagent(s). Results will arrive as completion events:\n"
            "  s1: one\n  s2: two\n\n"
            "END YOUR TURN now: this caller has no confirmed parent-work delivery "
            "boundary. Wait for the [Subagent completion event] messages. "
            "Dispatch is not completion."
        )

    @pytest.mark.parametrize("field", ["tasks", "agents"])
    def test_batch_member_limit_is_checked_before_any_post(self, gateway, field):
        """The parked step retains the member list until submission finishes, so
        the count is bounded where it is retained -- by the one name both spawn
        tools share -- and refused before any deferred state is built."""
        from kiro_crew import mcp_core
        from kiro_crew.validation import SPAWN_BATCH_MEMBERS_MAX, SPAWN_RUN_SCHEMA

        values = ["task" if field == "tasks" else "scout"] * SPAWN_BATCH_MEMBERS_MAX
        assert validate_tool_args({field: values}, SPAWN_RUN_SCHEMA)[field] == values
        text = mcp_core._call_tool("spawn_run", {field: values + [values[0]]})
        assert f"exceeds max items {SPAWN_BATCH_MEMBERS_MAX}" in text
        gateway.assert_not_called()

    @pytest.mark.parametrize("kind", ["errors", "queued_reasons", "transport_errors", "sibling"])
    def test_each_retained_gateway_line_is_bounded(self, gateway, monkeypatch, kind):
        from kiro_crew.mcp_tools import spawn
        from kiro_crew.subagent import AGENT_NOT_FOUND_CODE

        long_line = "x" * (spawn._SA_ERROR_MAX_CHARS * 2)
        if kind == "queued_reasons":
            gateway.return_value = {"id": "queued", "status": "queued", "reason_detail": long_line}
        else:
            gateway.return_value = {
                "error": long_line,
                "counted": True,
                "transport_error": kind == "transport_errors",
                "code": AGENT_NOT_FOUND_CODE,
            }
        receipt = MagicMock(wraps=spawn._spawn_run_receipt)
        monkeypatch.setattr(spawn, "_spawn_run_receipt", receipt)
        args = {"task": "one"}
        if kind == "sibling":
            args = {"tasks": ["one", "two"], "agent": "a" * 64}
        step = spawn.spawn_run("spawn_run", args)
        text = step.step()
        if kind == "sibling":
            assert text is None
            text = step.step()
        assert isinstance(text, str)
        stored = receipt.call_args.kwargs["errors" if kind == "sibling" else kind]
        lines = list(stored.values()) if isinstance(stored, dict) else stored
        assert lines and all(len(line) <= spawn._SA_ERROR_MAX_CHARS for line in lines)
        assert len(lines[0]) == spawn._SA_ERROR_MAX_CHARS

    def test_abandon_preserves_unknown_acceptance_without_confirmed_ids(self, gateway):
        from kiro_crew.mcp_tools.spawn import spawn_run

        gateway.return_value = {"error": "response timed out", "transport_error": True}
        step = spawn_run("spawn_run", {"tasks": ["uncertain", "unsubmitted"]})
        assert step.abandon() is None
        assert step.step() is None
        assert step.agent_ids == []
        text = step.abandon()
        assert "acceptance status is unknown" in text
        assert "uncertain: response timed out" in text
        assert "Do not retry automatically" in text
        assert "1 task(s) were NOT submitted" in text
        assert step.phase == "abandoned"
        # The transport-unknown member is left to the stuck-wave sweep; only the
        # task never posted is reported lost.
        assert [c.args[0] for c in gateway.call_args_list] == ["/api/spawn", "/api/spawn/lost"]
