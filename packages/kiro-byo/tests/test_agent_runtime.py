from __future__ import annotations

from kiro_byo.agent_runtime import AgentRegistry, AgentResult, AgentSpec, TaskProfile


def test_agent_registry_tracks_stable_role_identity_and_result() -> None:
    registry = AgentRegistry(max_plans=4)
    specs = (
        AgentSpec("lead", "quality", tier_pool="strong", authority="writer", required=True),
        AgentSpec("worker", "gather", tier_pool="fast", tool_policy="read_only"),
    )
    plan_id, tasks = registry.begin_plan("session-1", specs)
    assert len(tasks) == 2
    lead = registry.task_for_role(plan_id, "lead")
    worker = registry.task_for_role(plan_id, "worker")
    assert lead is not None and worker is not None
    assert lead.task_id != worker.task_id
    registry.transition(lead.task_id, "running", provider_id="p1", model_id="strong")
    registry.transition(
        lead.task_id,
        "completed",
        result=AgentResult(status="completed", summary="done", provider_id="p1", model_id="strong"),
    )
    lead_again = registry.task_for_role(plan_id, "lead")
    assert lead_again is not None
    assert lead_again.status == "completed"
    assert lead_again.result is not None and lead_again.result.summary == "done"
    assert worker.status == "planned"


def test_agent_registry_bounds_old_plan_history() -> None:
    registry = AgentRegistry(max_plans=4)
    plans: list[str] = []
    for index in range(6):
        plan_id, _ = registry.begin_plan("s", (AgentSpec("lead", f"task-{index}"),))
        plans.append(plan_id)
    assert registry.list(plan_id=plans[0]) == ()
    assert registry.list(plan_id=plans[1]) == ()
    assert registry.task_for_role(plans[-1], "lead") is not None


def test_task_profile_public_shape_is_bounded() -> None:
    class Signals:
        complexity = 2.0
        breadth = -1.0
        parallelizability = 0.7
        risk = 0.5
        tool_intensity = 0.8
        write_intensity = 0.1
        context_demand = 0.4
        verification_value = 0.9
        latency_sensitivity = 0.3
        task_profile = "tool"
        tools_required = True
        estimated_input_tokens = 500

    profile = TaskProfile.from_signals(Signals())
    assert profile.complexity == 1.0
    assert profile.breadth == 0.0
    assert profile.parallelizability == 0.7
    assert profile.task_class == "tool"
    assert set(profile.public_dict()) == {
        "complexity", "breadth", "parallelizability", "risk", "tool_intensity",
        "write_intensity", "context_demand", "verification_value",
        "latency_sensitivity", "task_class",
    }


def test_agent_registry_supports_multiple_workers_and_continuation_mailbox() -> None:
    registry = AgentRegistry(max_plans=4)
    specs = (
        AgentSpec("lead", "own final answer", name="lead", authority="writer"),
        AgentSpec("worker", "inspect primary evidence", name="worker-1", tool_policy="read_only"),
        AgentSpec("worker", "inspect edge cases", name="worker-2", tool_policy="read_only"),
    )
    plan_id, tasks = registry.begin_plan("session-1", specs)
    workers = registry.tasks_for_role(plan_id, "worker")
    assert len(workers) == 2
    assert {task.spec.agent_name for task in workers} == {"worker-1", "worker-2"}
    task = workers[0]
    registry.attach_context(task.task_id, ({"role": "user", "content": "seed"},))
    registry.pin_model(task.task_id, "p1", "fast-1")
    registry.transition(task.task_id, "completed", result=AgentResult(status="completed", summary="first"))
    registry.send_message(task.task_id, "check one more edge case")
    resumed = registry.resume(task.task_id)
    assert resumed is not None
    assert resumed.task_id == task.task_id
    assert resumed.generation == 1
    assert resumed.status == "planned"
    assert registry.consume_messages(task.task_id) == ("check one more edge case",)
    assert registry.context_snapshot(task.task_id)[0]["content"] == "seed"
    assert resumed.provider_id == "p1"
    assert resumed.model_id == "fast-1"


def test_agent_runtime_can_run_read_only_children_concurrently() -> None:
    import threading

    from kiro_byo.agent_runtime import AgentRuntime

    registry = AgentRegistry(max_plans=4)
    plan_id, _ = registry.begin_plan(
        "s",
        (
            AgentSpec("worker", "a", name="worker-1", tool_policy="read_only"),
            AgentSpec("worker", "b", name="worker-2", tool_policy="read_only"),
        ),
    )
    tasks = registry.tasks_for_role(plan_id, "worker")
    barrier = threading.Barrier(2)
    runtime = AgentRuntime(registry, max_workers=2)
    try:
        def runner(task):
            barrier.wait(timeout=1.0)
            return AgentResult(status="completed", summary=task.spec.agent_name)

        completed = runtime.run_group(tasks, runner, max_parallel=2)
    finally:
        runtime.shutdown(wait=True)
    assert [result.summary for _task, result in completed] == ["worker-1", "worker-2"]
    assert all(task.status == "completed" for task in tasks)
