from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from io import StringIO

import kiro_byo.turn_runner as turn_mod
from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.durable_runs import DurableRunStore
from kiro_byo.protocol import type_selection_id
from kiro_byo.providers.base import ChatTurn, ProviderTransportError
from kiro_byo.session_events import append_lifecycle, append_message, set_route_pin


MODEL = ModelSpec(
    "p", "P", "p-pro", "P Pro", tier="strong", tool_use=True,
    routing_quality=0.95, routing_speed=0.6, routing_economy=0.5,
)
ALT = ModelSpec(
    "q", "Q", "q-pro", "Q Pro", tier="strong", tool_use=True,
    routing_quality=0.90, routing_speed=0.65, routing_economy=0.55,
)
P1 = ProviderSpec("p", "P", "openai-compatible", "https://example.invalid/v1", "", (MODEL,))
P2 = ProviderSpec("q", "Q", "openai-compatible", "https://other.invalid/v1", "", (ALT,))


def _responses(output: StringIO):
    return [json.loads(line) for line in output.getvalue().splitlines() if line.strip().startswith("{")]


def test_failed_clean_turn_can_resume_same_durable_run_and_execute() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_home = os.environ.get("KIRO_BYO_HOME")
        os.environ["KIRO_BYO_HOME"] = tmp
        original = turn_mod.chat_completion_turn
        output = StringIO()
        server = AcpServer(stdin=StringIO(), stdout=output)
        config = KiroByoConfig((P1,))
        server._config = lambda: config  # type: ignore[method-assign]
        session = Session("resume-run", tmp, MODEL.model_id)
        server._put_session(session)
        calls = 0

        def fake_turn(provider, model_id, messages, emit, **kwargs):
            nonlocal calls
            calls += 1
            if calls <= 2:  # initial call + one policy retry, then fail the turn
                raise ProviderTransportError("synthetic disconnect")
            return ChatTurn(text="recovered after durable resume")

        turn_mod.chat_completion_turn = fake_turn
        try:
            server._handle_prompt_request(
                1, {"sessionId": session.session_id, "prompt": [{"type": "text", "text": "recover this task"}]},
            )
            runs = server._durable_run_store().list(session_id=session.session_id)
            assert len(runs) == 1
            run = runs[0]
            run_id = run["run_id"]
            assert run["status"] == "failed"
            assert run["recovery_state"] == "retryable"
            assert run["metadata"]["user_event_seq"] >= 0
            assert session.messages == []

            server._handle_run_resume_request(
                2, {"sessionId": session.session_id, "runId": run_id},
            )
            recovered = server._durable_run_store().get(run_id)
            assert recovered is not None
            assert recovered["status"] == "completed"
            assert recovered["recovery_state"] == "clean"
            assert recovered["resume_count"] == 1
            assert len(recovered["steps"]) == 2
            assert recovered["steps"][0]["attempts"][0]["status"] == "failed"
            assert recovered["steps"][1]["attempts"][0]["status"] == "completed"
            assert session.messages[-2]["content"] == "recover this task"
            assert session.messages[-1]["content"] == "recovered after durable resume"
            assert any(e.get("type") == "run/resume" for e in session.events)
            payloads = _responses(output)
            assert any(p.get("id") == 2 and p.get("result", {}).get("stopReason") == "end_turn" for p in payloads)
        finally:
            turn_mod.chat_completion_turn = original
            server._executor.shutdown(wait=True)
            if old_home is None:
                os.environ.pop("KIRO_BYO_HOME", None)
            else:
                os.environ["KIRO_BYO_HOME"] = old_home


def test_restart_recovery_rewinds_orphaned_read_only_turn_before_replay() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_home = os.environ.get("KIRO_BYO_HOME")
        os.environ["KIRO_BYO_HOME"] = tmp
        original = turn_mod.chat_completion_turn
        output = StringIO()
        server = AcpServer(stdin=StringIO(), stdout=output)
        config = KiroByoConfig((P1,))
        server._config = lambda: config  # type: ignore[method-assign]
        session = Session("crash-safe", tmp, MODEL.model_id)
        server._put_session(session)

        # Simulate a process that persisted the turn anchor and then disappeared
        # after adding read-only partial transcript state.
        with session.state_lock:
            history_start = len(session.messages)
            append_lifecycle(session, "turn/start", {"history_start": history_start})
            append_message(session, {"role": "user", "content": "resume after restart"})
            user_event_seq = len(session.events) - 1
            append_message(session, {"role": "assistant", "content": "orphaned partial state"})
        server._persist_session(session)
        run = server._durable_run_store().create_run(
            session.session_id, selection=MODEL.model_id,
            metadata={"history_start": history_start, "user_event_seq": user_event_seq},
        )
        step = server._durable_run_store().start_step(run["run_id"], kind="turn")
        assert step is not None
        server._durable_run_store().start_attempt(run["run_id"], step["step_id"], provider_id="p", model_id=MODEL.model_id)
        server._durable_run_store().checkpoint(
            run["run_id"], step_id=step["step_id"], name="turn_ready",
            data={"side_effect_risk": False},
        )
        server._durable_run_store().mark_recovery(run["run_id"], "pending_restart")

        def fake_turn(provider, model_id, messages, emit, **kwargs):
            # The orphaned assistant fragment must have been rewound; only the
            # replayed user message may be visible in the active transcript.
            assert not any(m.get("content") == "orphaned partial state" for m in messages)
            return ChatTurn(text="restart recovered")

        turn_mod.chat_completion_turn = fake_turn
        try:
            server._handle_run_resume_request(
                3, {"sessionId": session.session_id, "runId": run["run_id"]},
            )
            active_contents = [m.get("content") for m in session.messages]
            assert active_contents == ["resume after restart", "restart recovered"]
            saved = server._durable_run_store().get(run["run_id"])
            assert saved is not None and saved["status"] == "completed"
            # The orphaned running attempt is closed explicitly before the new attempt.
            assert saved["steps"][0]["status"] == "interrupted"
            assert saved["steps"][0]["attempts"][0]["status"] == "interrupted"
        finally:
            turn_mod.chat_completion_turn = original
            server._executor.shutdown(wait=True)
            if old_home is None:
                os.environ.pop("KIRO_BYO_HOME", None)
            else:
                os.environ["KIRO_BYO_HOME"] = old_home


def test_side_effect_checkpoint_blocks_whole_turn_replay() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = DurableRunStore(Path(tmp) / "runs")
        run = store.create_run(
            "sidefx", metadata={"history_start": 0, "user_event_seq": 2}
        )
        step = store.start_step(run["run_id"], kind="turn")
        assert step is not None
        attempt = store.start_attempt(run["run_id"], step["step_id"], provider_id="p", model_id="m")
        assert attempt is not None
        store.checkpoint(
            run["run_id"], step_id=step["step_id"], attempt_id=attempt["attempt_id"],
            name="tool_batch_admitted", data={"effects": ["unknown_write"], "side_effect_risk": True},
        )
        record = store.get(run["run_id"])
        assert record is not None
        assert store.restart_recovery_state(record) == "unknown_side_effect"
        store.mark_recovery(run["run_id"], "unknown_side_effect")
        from kiro_byo.recovery_policy import RecoveryPolicy
        decision = RecoveryPolicy.durable_run(store.get(run["run_id"]), strategy="resume")
        assert decision.safe is False
        assert "side-effect" in decision.reason


def test_replan_clears_sticky_type_pin_and_reexecutes_same_run() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_home = os.environ.get("KIRO_BYO_HOME")
        os.environ["KIRO_BYO_HOME"] = tmp
        original = turn_mod.chat_completion_turn
        output = StringIO()
        server = AcpServer(stdin=StringIO(), stdout=output)
        config = KiroByoConfig((P1, P2), mode_type="power")
        server._config = lambda: config  # type: ignore[method-assign]
        selection = type_selection_id("tiga", "power")
        session = Session("replan-run", tmp, selection, selection_kind="type")
        server._put_session(session)
        append_lifecycle(session, "turn/start", {"history_start": 0})
        append_message(session, {"role": "user", "content": "replan this"})
        user_event_seq = len(session.events) - 1
        # Mark an old sticky route so replan has something concrete to invalidate.
        set_route_pin(session, "tiga:power", "p", MODEL.model_id, reason="test")
        server._persist_session(session)
        run = server._durable_run_store().create_run(
            session.session_id, selection=selection, mode="power",
            metadata={"history_start": 0, "user_event_seq": user_event_seq},
        )
        server._durable_run_store().finish_run(
            run["run_id"], status="failed", recovery_state="replan_required"
        )

        def fake_turn(provider, model_id, messages, emit, **kwargs):
            return ChatTurn(text="replanned")

        turn_mod.chat_completion_turn = fake_turn
        try:
            server._handle_run_replan_request(
                4, {"sessionId": session.session_id, "runId": run["run_id"]}
            )
            saved = server._durable_run_store().get(run["run_id"])
            assert saved is not None and saved["status"] == "completed"
            assert saved["replan_count"] == 1
            assert saved["last_recovery_action"] == "replan"
            # Replan emits an explicit unpin before normal routing may choose/pin again.
            assert any(
                e.get("type") == "routing/unpin" and (e.get("data") or {}).get("reason") == "durable_replan"
                for e in session.events
            )
            assert session.messages[-1]["content"] == "replanned"
        finally:
            turn_mod.chat_completion_turn = original
            server._executor.shutdown(wait=True)
            if old_home is None:
                os.environ.pop("KIRO_BYO_HOME", None)
            else:
                os.environ["KIRO_BYO_HOME"] = old_home


def test_session_start_payload_surfaces_recoverable_runs_without_transcript() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_home = os.environ.get("KIRO_BYO_HOME")
        os.environ["KIRO_BYO_HOME"] = tmp
        server = AcpServer(stdin=StringIO(), stdout=StringIO())
        config = KiroByoConfig((P1,))
        session = Session("surface-recovery", tmp, MODEL.model_id)
        server._put_session(session)
        append_message(session, {"role": "user", "content": "private prompt"})
        event_seq = len(session.events) - 1
        run = server._durable_run_store().create_run(
            session.session_id, selection=MODEL.model_id,
            metadata={"history_start": 0, "user_event_seq": event_seq},
        )
        server._durable_run_store().finish_run(
            run["run_id"], status="failed", recovery_state="retryable"
        )
        payload = server._session_start_payload(session, config)
        recovery = payload["_meta"]["kiro"]["durableRecovery"]
        assert recovery["runs"][0]["runId"] == run["run_id"]
        assert recovery["runs"][0]["resume"] is True
        assert "private prompt" not in str(recovery)
        server._executor.shutdown(wait=True)
        if old_home is None:
            os.environ.pop("KIRO_BYO_HOME", None)
        else:
            os.environ["KIRO_BYO_HOME"] = old_home


def test_recovery_does_not_rewind_session_if_turn_lock_is_lost_to_race() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_home = os.environ.get("KIRO_BYO_HOME")
        os.environ["KIRO_BYO_HOME"] = tmp
        output = StringIO()
        server = AcpServer(stdin=StringIO(), stdout=output)
        config = KiroByoConfig((P1,))
        server._config = lambda: config  # type: ignore[method-assign]
        session = Session("recovery-race", tmp, MODEL.model_id)
        server._put_session(session)
        append_message(session, {"role": "user", "content": "anchor"})
        event_seq = len(session.events) - 1
        append_message(session, {"role": "assistant", "content": "must remain"})
        run = server._durable_run_store().create_run(
            session.session_id, selection=MODEL.model_id,
            metadata={"history_start": 0, "user_event_seq": event_seq},
        )
        server._durable_run_store().finish_run(
            run["run_id"], status="failed", recovery_state="retryable"
        )
        assert session.turn_lock.acquire(blocking=False)
        try:
            server._handle_run_resume_request(
                9, {"sessionId": session.session_id, "runId": run["run_id"]}
            )
        finally:
            session.turn_lock.release()
        assert [m.get("content") for m in session.messages] == ["anchor", "must remain"]
        payload = next(p for p in _responses(output) if p.get("id") == 9)
        assert payload["error"]["code"] == -32002
        server._executor.shutdown(wait=True)
        if old_home is None:
            os.environ.pop("KIRO_BYO_HOME", None)
        else:
            os.environ["KIRO_BYO_HOME"] = old_home


def test_generic_provider_failure_requires_replan_instead_of_same_route_resume() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_home = os.environ.get("KIRO_BYO_HOME")
        os.environ["KIRO_BYO_HOME"] = tmp
        server = AcpServer(stdin=StringIO(), stdout=StringIO())
        session = Session("generic-provider-failure", tmp, MODEL.model_id)
        server._put_session(session)
        run = server._durable_run_store().create_run(
            session.session_id, selection=MODEL.model_id,
            metadata={"history_start": 0, "user_event_seq": 0},
        )
        step = server._durable_run_store().start_step(run["run_id"], kind="turn")
        assert step is not None
        attempt = server._durable_run_store().start_attempt(
            run["run_id"], step["step_id"], provider_id="p", model_id=MODEL.model_id
        )
        assert attempt is not None
        session.active_run_id = run["run_id"]
        session.active_run_step_id = step["step_id"]
        session.active_run_attempt_id = attempt["attempt_id"]
        server._finish_durable_run(
            session, outcome="error", error_kind="ProviderError", error_category="provider",
            provider_id="p", model_id=MODEL.model_id, side_effect_risk=False,
        )
        saved = server._durable_run_store().get(run["run_id"])
        assert saved is not None
        assert saved["recovery_state"] == "replan_required"
        server._executor.shutdown(wait=True)
        if old_home is None:
            os.environ.pop("KIRO_BYO_HOME", None)
        else:
            os.environ["KIRO_BYO_HOME"] = old_home
