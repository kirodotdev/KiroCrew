"""R9: an interrupted tool batch must not be recorded as fully executed."""
from __future__ import annotations

from threading import Lock
from types import SimpleNamespace

import pytest

from kiro_byo.durable_runs import DurableRunStore
from kiro_byo.turn_runner import TurnRunnerMixin, _LoopDirective, _ProviderLoopState


@pytest.mark.parametrize("cancelled", [True, False])
@pytest.mark.parametrize("effect", ["unknown_write", "read"])
def test_tool_batch_receipt_reflects_actual_execution_status(
    tmp_path, monkeypatch, cancelled: bool, effect: str,
) -> None:
    """Verify the durable ledger as well as the ACP response ordering."""
    from kiro_byo import turn_runner as turn_runner_module

    monkeypatch.setattr(turn_runner_module, "append_message", lambda *_: None)
    store = DurableRunStore(tmp_path / "runs")
    run = store.create_run("session", metadata={"user_event_seq": 0}, lease_owner="owner")
    run_id = run["run_id"]
    step = store.start_step(run_id, kind="turn", owner_token="owner")
    assert step is not None
    attempt = store.start_attempt(run_id, step["step_id"], owner_token="owner")
    assert attempt is not None
    observations = []

    class Harness(TurnRunnerMixin):
        def _durable_run_store(self):
            return store

        def _tool_effect_class(self, *_):
            return effect

        def _runtime_event(self, *a, **kw):
            pass

        def _advance_turn_phase(self, *a, **kw):
            pass

        def trace(self, *_):
            pass

        def _runtime_seams(self):
            class Executor:
                def execute(self, *a, **kw):
                    observations.append("dispatch")
                    return SimpleNamespace(
                        cancelled=cancelled, tool_ms=1, mcp_wait_ms=0,
                        native_calls=0, tool_search_calls=0, tool_search_loaded=0,
                        pending_replan_notice="", guard_reason="",
                        stop_requested=False, steered=False,
                        replan_requested=False,
                    )
            return SimpleNamespace(tool_executor=Executor())

        def _consume_queued_steers_for_turn(self, *a, **kw):
            return False

        def _recover_current_turn(self, *a, **kw):
            observations.append(("recovery", kw["reason"], kw["preserve_side_effects"]))

        def _record_provider_turn_telemetry(self, *a, **kw):
            observations.append(("telemetry", kw["outcome"]))

        def _provider_prompt_result(self, state, status):
            return {"stopReason": status}

        def result(self, req_id, payload):
            observations.append(("response", payload["stopReason"]))

    session = SimpleNamespace(
        session_id="session", state_lock=Lock(), events=[],
        active_run_id=run_id, active_run_step_id=step["step_id"],
        active_run_attempt_id=attempt["attempt_id"], active_run_lease_token="owner",
        active_turn_id="session:0", agent_registry=None,
    )
    state = _ProviderLoopState(
        provider=None, model=None, mode_fallbacks=[], relay=None,
        turn_effort="auto", turn_thinking="auto",
        turn_run_id=run_id, turn_trace_id="trace",
    )
    state.step = 1
    call = SimpleNamespace(name="operation", call_id="call-1", arguments_text="{}")
    ctx = SimpleNamespace(role="lead", provider=SimpleNamespace(id="provider"),
                          model=SimpleNamespace(model_id="model"))
    turn = SimpleNamespace(tool_calls=[call], text="", provider_context=None)
    directive = Harness()._handle_tool_round(
        "req", session, state, ctx, turn,
        history_start=0, t_prompt=0.0, routing_decision=None,
    )
    saved = store.get(run_id)
    assert saved is not None
    receipts = list(saved["tool_batches"].values())
    assert len(receipts) == 1
    checkpoints = [cp["name"] for cp in saved["checkpoints"]]
    assert checkpoints[0] == "tool_batch_admitted"
    assert observations.count("dispatch") == 1
    if cancelled:
        assert directive == _LoopDirective.RETURN
        assert receipts[0]["status"] == "admitted"
        assert "tool_batch_completed" not in checkpoints
        assert ("response", "cancelled") in observations
        assert ("recovery", "cancelled_during_tools", effect != "read") in observations
    else:
        assert directive == _LoopDirective.NONE
        assert receipts[0]["status"] == "completed"
        assert checkpoints[-1] == "tool_batch_completed"
        assert not any(isinstance(row, tuple) and row[0] == "recovery" for row in observations)

    assert store.restart_recovery_state(saved) == (
        "unknown_side_effect" if effect != "read" else "pending_restart"
    )


@pytest.mark.parametrize("cancel_before_dispatch", [True, False])
def test_cancelled_batch_snapshot_preserves_counters_and_skips_later_calls(
    monkeypatch, cancel_before_dispatch: bool,
) -> None:
    """Both early-exit paths reuse the same outcome and never dispatch call 2."""
    from threading import Event
    from kiro_byo import tool_batch_execution as module

    monkeypatch.setattr(module, "append_lifecycle", lambda *a, **kw: None)
    invocations = []

    class Harness(TurnRunnerMixin):
        def _tool_effect_class(self, *a):
            return "unknown_write"

        def _runtime_event(self, *a, **kw):
            pass

        def _execute_native_tool(self, session, call, arguments):
            invocations.append(call.call_id)
            return "interrupted", 17.0, "cancelled"

        def _append_tool_result(self, *a):
            pass

    session = SimpleNamespace(
        cancel_event=Event(), steer_event=Event(),
        native=SimpleNamespace(has_tool=lambda name: True),
    )
    if cancel_before_dispatch:
        session.cancel_event.set()
    calls = [
        SimpleNamespace(name="write", call_id=f"id-{i}", arguments_text="{}",
                        arguments=lambda: {})
        for i in (1, 2)
    ]
    result = Harness()._execute_tool_batch_impl(
        session, tool_calls=calls, round_role="lead",
        round_provider=SimpleNamespace(id="provider"),
        round_model=SimpleNamespace(model_id="model"),
        step=1, execution_guard=SimpleNamespace(), provider_rounds_total=1,
    )
    assert result.cancelled
    assert invocations == ([] if cancel_before_dispatch else ["id-1"])
    assert result.tool_ms == (0.0 if cancel_before_dispatch else 17.0)
    assert result.native_calls == (0 if cancel_before_dispatch else 1)
