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


def test_terminal_state_cannot_skip_running_attempt_or_step(tmp_path: Path):
    """Storage guards reject out-of-order terminal transitions before any write."""
    import pytest

    store = DurableRunStore(tmp_path / "runs")
    run = store.create_run("s", lease_owner="owner")
    run_id = run["run_id"]
    step = store.start_step(run_id, kind="turn", owner_token="owner")
    attempt = store.start_attempt(run_id, step["step_id"], owner_token="owner")
    baseline = store.get(run_id)
    with pytest.raises(ValueError, match="running attempt"):
        store.finish_step(run_id, step["step_id"], status="completed", owner_token="owner")
    with pytest.raises(ValueError, match="running step or attempt"):
        store.finish_run(run_id, status="completed", owner_token="owner")
    assert store.get(run_id) == baseline

    store.finish_attempt(run_id, step["step_id"], attempt["attempt_id"], status="completed", owner_token="owner")
    before = store.get(run_id)
    with pytest.raises(ValueError, match="running step or attempt"):
        store.finish_run(run_id, status="completed", owner_token="owner")
    assert store.get(run_id) == before
    store.finish_step(run_id, step["step_id"], status="completed", owner_token="owner")
    store.finish_run(run_id, status="completed", owner_token="owner")
    assert store.get(run_id)["status"] == "completed"


def test_finalize_turn_is_one_atomic_transaction_on_disk(tmp_path: Path, monkeypatch):
    """A failed terminal fsync/replace must not partially commit the attempt."""
    import pytest

    store = DurableRunStore(tmp_path / "runs")
    run = store.create_run("session", lease_owner="owner")
    step = store.start_step(run["run_id"], kind="turn", owner_token="owner")
    attempt = store.start_attempt(run["run_id"], step["step_id"], owner_token="owner")
    snapshot = store.get(run["run_id"])
    original_write = store._write

    def fails(_record):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(store, "_write", fails)
    with pytest.raises(OSError, match="disk failure"):
        store.finalize_turn(
            run["run_id"], step_id=step["step_id"], attempt_id=attempt["attempt_id"],
            status="completed", owner_token="owner",
        )
    assert store.get(run["run_id"]) == snapshot
    monkeypatch.setattr(store, "_write", original_write)
    saved = store.finalize_turn(
        run["run_id"], step_id=step["step_id"], attempt_id=attempt["attempt_id"],
        status="completed", owner_token="owner", provider_id="provider", model_id="model",
    )
    assert saved["revision"] == snapshot["revision"] + 1
    assert saved["steps"][0]["attempts"][0]["status"] == "completed"
    assert saved["steps"][0]["status"] == "completed"
    assert saved["status"] == "completed"
    assert saved["lease"]["expires_at"] == 0


def test_atomic_finalization_respects_lease_and_pending_tool_receipts(tmp_path: Path):
    import pytest

    store = DurableRunStore(tmp_path / "runs")
    run = store.create_run("session", lease_owner="owner")
    step = store.start_step(run["run_id"], kind="turn", owner_token="owner")
    attempt = store.start_attempt(run["run_id"], step["step_id"], owner_token="owner")
    store.checkpoint(
        run["run_id"], step_id=step["step_id"], attempt_id=attempt["attempt_id"],
        name="tool_batch_admitted", data={"batch_id": "batch1", "tool_count": 1,
                                          "side_effect_risk": True}, owner_token="owner",
    )
    kwargs = dict(step_id=step["step_id"], attempt_id=attempt["attempt_id"], status="completed")
    before = store.get(run["run_id"])
    with pytest.raises(ValueError, match="lease"):
        store.finalize_turn(run["run_id"], owner_token="other", **kwargs)
    with pytest.raises(ValueError, match="unresolved tool batch"):
        store.finalize_turn(run["run_id"], owner_token="owner", **kwargs)
    with pytest.raises(ValueError, match="running attempt"):
        store.finalize_turn(run["run_id"], owner_token="owner", **{**kwargs, "attempt_id": "absent"})
    assert store.get(run["run_id"]) == before
    store.checkpoint(
        run["run_id"], step_id=step["step_id"], attempt_id=attempt["attempt_id"],
        name="tool_batch_completed", data={"batch_id": "batch1"}, owner_token="owner",
    )
    assert store.finalize_turn(run["run_id"], owner_token="owner", **kwargs)["status"] == "completed"


def test_telemetry_does_not_claim_turn_completed_if_ledger_commit_fails():
    from types import SimpleNamespace
    from kiro_byo.provider_loop_terminal import ProviderLoopTerminalMixin
    from kiro_byo.turn_loop_types import _ProviderLoopState

    emitted, errors, plan_finalizations = [], [], []

    class Harness(ProviderLoopTerminalMixin):
        def _finish_durable_run(self, *_a, **_kw):
            return False
        def _finalize_agent_plan(self, *_a, **_kw):
            plan_finalizations.append(True)
        def _runtime_event(self, event, *_a, **_kw):
            emitted.append(event)
        def emit_runtime_error(self, _session_id, message):
            errors.append(message)

    state = _ProviderLoopState(
        provider=None, model=None, mode_fallbacks=[], relay=None,
        turn_effort="auto", turn_thinking="auto", turn_run_id="run", turn_trace_id="trace",
    )
    session = SimpleNamespace(session_id="session", events=[], cancel_source="")
    Harness()._record_provider_turn_telemetry(session, state, outcome="success", t_prompt=0.0)
    assert state.telemetry_recorded
    assert "turn.completed" not in emitted
    assert not plan_finalizations
    assert session.events == []  # no turn/end or terminal state on failed commit
    assert len(errors) == 1 and "durable completion" in errors[0]


def test_terminal_event_and_agent_plan_wait_for_durable_commit():
    """The ledger commits first; only then can the session/Agent project completion."""
    from types import SimpleNamespace
    from kiro_byo.provider_loop_terminal import ProviderLoopTerminalMixin
    from kiro_byo.turn_loop_types import _ProviderLoopState

    order = []

    class Harness(ProviderLoopTerminalMixin):
        def _finish_durable_run(self, *_a, **_kw):
            order.append("ledger")
            return True

        def _finalize_agent_plan(self, *_a, **_kw):
            assert order == ["ledger"]
            order.append("plan")

        def _runtime_event(self, event, *_a, **_kw):
            if event == "turn.completed":
                order.append("completed")

        def _persist_session(self, _session):
            order.append("persist")

        def trace(self, _message):
            pass

        @staticmethod
        def _reasoning_summary(_values):
            return "default"

        telemetry = SimpleNamespace(record=lambda _value: order.append("telemetry"))

    state = _ProviderLoopState(
        provider=None, model=None, mode_fallbacks=[], relay=None,
        turn_effort="auto", turn_thinking="auto", turn_run_id="run", turn_trace_id="trace",
    )
    session = SimpleNamespace(session_id="s", events=[], cancel_source="", model="m")
    Harness()._record_provider_turn_telemetry(session, state, outcome="success", t_prompt=0.0)
    assert order == ["ledger", "plan", "telemetry", "completed", "persist"]
    assert [row["type"] for row in session.events] == ["turn/end"]
    assert session.events[-1]["data"]["outcome"] == "success"


def test_failed_ledger_rejects_late_execution_mutations_but_allows_recovery_annotation(tmp_path: Path):
    """Legacy/inconsistent failed ledgers cannot be revived by a delayed callback."""
    import pytest

    store = DurableRunStore(tmp_path / "runs")
    run = store.create_run("session")
    step = store.start_step(run["run_id"], kind="turn")
    attempt = store.start_attempt(run["run_id"], step["step_id"])

    # Older records can have leftover live execution state even after their
    # parent was marked failed. Simulate that historical on-disk shape.
    damaged = store.get(run["run_id"])
    damaged["status"] = "failed"
    damaged["recovery_state"] = "retryable"
    store._write(damaged)
    before = store.get(run["run_id"])

    late_writes = [
        lambda: store.checkpoint(run["run_id"], name="late_callback"),
        lambda: store.finish_attempt(run["run_id"], step["step_id"], attempt["attempt_id"], status="completed"),
        lambda: store.finish_step(run["run_id"], step["step_id"], status="completed"),
        lambda: store.finish_run(run["run_id"], status="completed"),
        lambda: store.finalize_turn(run["run_id"], step_id=step["step_id"], attempt_id=attempt["attempt_id"], status="completed"),
    ]
    for callback in late_writes:
        with pytest.raises(ValueError, match="terminal"):
            callback()
        assert store.get(run["run_id"]) == before

    # Failed runs may still receive recovery classification, which is separate
    # from resuming execution and must not change their terminal state.
    annotated = store.mark_recovery(run["run_id"], "pending_restart")
    assert annotated["status"] == "failed"
    assert annotated["recovery_state"] == "pending_restart"


def test_pending_receipt_cannot_be_sealed_before_batch_completion(tmp_path: Path):
    """Do not strand a tool receipt by closing its owning attempt/step."""
    import pytest

    store = DurableRunStore(tmp_path / "runs")
    run = store.create_run("s", lease_owner="owner")
    rid = run["run_id"]
    step = store.start_step(rid, kind="turn", owner_token="owner")
    attempt = store.start_attempt(rid, step["step_id"], owner_token="owner")
    ids = {"step_id": step["step_id"], "attempt_id": attempt["attempt_id"]}
    store.checkpoint(
        rid, name="tool_batch_admitted", data={"batch_id": "batch-safe", "tool_count": 1},
        owner_token="owner", **ids,
    )
    before = store.get(rid)
    with pytest.raises(ValueError, match="unresolved tool batch receipt"):
        store.finish_attempt(rid, status="completed", owner_token="owner", **ids)
    assert store.get(rid) == before

    store.checkpoint(
        rid, name="tool_batch_completed", data={"batch_id": "batch-safe"},
        owner_token="owner", **ids,
    )
    store.finish_attempt(rid, status="completed", owner_token="owner", **ids)
    store.finish_step(rid, step["step_id"], status="completed", owner_token="owner")
    saved = store.finish_run(rid, status="completed", owner_token="owner")
    assert saved["status"] == "completed"
    assert saved["tool_batches"]["batch-safe"]["status"] == "completed"


def test_malformed_receipt_cannot_be_interpreted_as_success(tmp_path: Path):
    import pytest

    store = DurableRunStore(tmp_path / "runs")
    run = store.create_run("s")
    damaged = store.get(run["run_id"])
    damaged["tool_batches"] = {"strange": {"status": "unknown"}}
    store._write(damaged)
    baseline = store.get(run["run_id"])
    with pytest.raises(ValueError, match="unresolved tool batch receipt"):
        store.finish_run(run["run_id"], status="completed")
    assert store.get(run["run_id"]) == baseline


def test_late_checkpoint_from_completed_attempt_cannot_change_active_run(tmp_path: Path):
    import pytest

    store = DurableRunStore(tmp_path / "runs")
    run = store.create_run("s")
    rid = run["run_id"]
    step = store.start_step(rid, kind="turn")
    attempt = store.start_attempt(rid, step["step_id"])
    store.finish_attempt(rid, step["step_id"], attempt["attempt_id"], status="failed")
    original = store.get(rid)
    with pytest.raises(ValueError, match="running attempt"):
        store.checkpoint(
            rid, step_id=step["step_id"], attempt_id=attempt["attempt_id"],
            name="late", data={"side_effect_risk": True}, session_event_seq=999,
        )
    assert store.get(rid) == original

    # Legacy run-level checkpoints with no step/attempt anchor are compatible.
    assert store.checkpoint(rid, name="run_diagnostic") is not None
