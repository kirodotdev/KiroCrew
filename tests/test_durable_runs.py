from pathlib import Path

from kiro_byo.durable_runs import AgentStateStore, DurableRunStore


def test_durable_run_lifecycle_and_checkpoint(tmp_path: Path):
    store = DurableRunStore(tmp_path / "runs")
    run = store.create_run(
        "session-1", plan_id="plan-1", mode="multi", selection="kiro-byo-type:tiga:multi",
        session_event_seq=4, metadata={"provider_id": "p1", "model_id": "m1"},
    )
    step = store.start_step(run["run_id"], kind="turn", objective="execute")
    assert step is not None
    attempt = store.start_attempt(run["run_id"], step["step_id"], provider_id="p1", model_id="m1")
    assert attempt is not None
    cp = store.checkpoint(
        run["run_id"], step_id=step["step_id"], attempt_id=attempt["attempt_id"],
        name="tool_batch_completed", session_event_seq=9,
        agent_tasks=[{"task_id": "a1", "status": "completed"}],
        data={"tool_calls_total": 2},
    )
    assert cp is not None
    store.finish_attempt(
        run["run_id"], step["step_id"], attempt["attempt_id"], status="completed",
        provider_id="p1", model_id="m1",
    )
    store.finish_step(run["run_id"], step["step_id"], status="completed")
    store.finish_run(run["run_id"], status="completed", session_event_seq=12)

    saved = store.get(run["run_id"])
    assert saved is not None
    assert saved["status"] == "completed"
    assert saved["session_event_seq"] == 12
    assert saved["steps"][0]["attempts"][0]["status"] == "completed"
    assert saved["checkpoints"][0]["name"] == "tool_batch_completed"
    assert store.pending(session_id="session-1") == []


def test_incomplete_run_is_discoverable_and_marked_for_recovery(tmp_path: Path):
    store = DurableRunStore(tmp_path / "runs")
    run = store.create_run("session-2", mode="glitter")
    assert [row["run_id"] for row in store.pending(session_id="session-2")] == [run["run_id"]]
    store.mark_recovery(run["run_id"], "pending_restart")
    assert store.get(run["run_id"])["recovery_state"] == "pending_restart"


def test_agent_state_projection_is_content_bounded_and_marks_restart(tmp_path: Path):
    store = AgentStateStore(tmp_path / "agents.json")
    store.upsert({
        "task_id": "task-1", "plan_id": "plan", "session_id": "s", "name": "reader-1",
        "role": "worker", "objective": "read files", "status": "running",
        "provider_id": "p", "model_id": "m", "created_at": 1.0, "started_at": 2.0,
        "generation": 0, "continuation_count": 0, "pending_messages": 1,
        "cancel_requested": False,
        "result": {"status": "completed", "summary": "SECRET TRANSCRIPT", "duration_ms": 10.0},
    }, event="running")
    rows = store.list(session_id="s")
    assert len(rows) == 1
    row = rows[0]
    assert row["task_id"] == "task-1"
    assert row["controls"]["cancel"] is True
    assert "summary" not in row
    assert "SECRET TRANSCRIPT" not in str(row)

    assert store.mark_session_stale("s") == 1
    stale = store.list(session_id="s")[0]
    assert stale["status"] == "paused"
    assert stale["controls"]["resume"] is False
    assert "lost" in stale["controls"]["note"]


def test_turn_runner_run_helpers_create_and_close_real_control_boundary(tmp_path: Path):
    from types import SimpleNamespace
    from kiro_byo.agent_runtime import AgentRegistry
    from kiro_byo.turn_runner import TurnRunnerMixin

    class Harness(TurnRunnerMixin):
        runtime_trace = None
        def trace(self, _message):
            pass

    h = Harness()
    h._kiro_durable_run_store = DurableRunStore(tmp_path / "runs")
    h._kiro_agent_state_store = AgentStateStore(tmp_path / "agents.json")
    session = SimpleNamespace(
        session_id="s-turn", events=[], agent_registry=AgentRegistry(),
        active_agent_plan_id="", active_run_id="", active_run_step_id="", active_run_attempt_id="",
    )
    run_id = h._begin_durable_run(
        session, None, selection="model-1", provider_id="p", model_id="m"
    )
    assert run_id
    assert h._kiro_durable_run_store.get(run_id)["status"] == "running"
    h._finish_durable_run(
        session, outcome="error", error_kind="ProviderTransportError", error_category="transport",
        provider_id="p", model_id="m", side_effect_risk=True,
    )
    saved = h._kiro_durable_run_store.get(run_id)
    assert saved["status"] == "failed"
    assert saved["recovery_state"] == "unknown_side_effect"
    assert saved["steps"][0]["attempts"][0]["status"] == "failed"
    assert session.active_run_id == ""
