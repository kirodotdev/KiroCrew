from __future__ import annotations

import os
import tempfile
import threading
from io import StringIO
from types import SimpleNamespace

import kiro_byo.turn_runner as turn_mod
from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.mode_router import build_execution_plan, route_model
from kiro_byo.providers.base import ChatTurn, ToolCall


LEAD = ModelSpec(
    "p", "P", "lead-pro", "Lead Pro", tier="strong", tool_use=True,
    routing_quality=0.98, routing_speed=0.35, routing_economy=0.25,
)
FAST1 = ModelSpec(
    "p", "P", "fast-a", "Fast A", tier="fast", tool_use=True,
    routing_quality=0.55, routing_speed=0.95, routing_economy=0.95,
)
FAST2 = ModelSpec(
    "q", "Q", "fast-b", "Fast B", tier="fast", tool_use=True,
    routing_quality=0.58, routing_speed=0.90, routing_economy=0.90,
)
P1 = ProviderSpec("p", "P", "openai-compatible", "https://example.invalid/v1", "", (LEAD, FAST1))
P2 = ProviderSpec("q", "Q", "openai-compatible", "https://other.invalid/v1", "", (FAST2,))


class FakeNative:
    def specs(self):
        return (SimpleNamespace(name="read"),)

    def tool_specs(self, names=None):
        return [{"type": "function", "function": {"name": "read"}}]

    def has_tool(self, name):
        return name == "read"

    def capability_for(self, name):
        return "fs_read" if name == "read" else "unknown"


def test_unified_runtime_executes_glitter_parallel_budget_and_hands_back() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_home = os.environ.get("KIRO_BYO_HOME")
        os.environ["KIRO_BYO_HOME"] = tmp
        original = turn_mod.chat_completion_turn
        server = AcpServer(stdin=StringIO(), stdout=StringIO())
        server._execute_native_tool = lambda session, call, args: ("seed-data", 1.0, "")
        barrier = threading.Barrier(4)
        child_models: list[str] = []
        lead_calls = 0
        lock = threading.Lock()

        def fake_turn(provider, model_id, messages, emit, **kwargs):
            nonlocal lead_calls
            if model_id == "lead-pro":
                with lock:
                    lead_calls += 1
                    index = lead_calls
                if index == 1:
                    return ChatTurn(text="", tool_calls=(ToolCall("seed", "read", '{"path":"seed"}'),))
                return ChatTurn(text="final synthesis")
            with lock:
                child_models.append(model_id)
            barrier.wait(timeout=1.0)
            return ChatTurn(text=f"findings from {model_id}")

        turn_mod.chat_completion_turn = fake_turn
        try:
            config = KiroByoConfig((P1, P2), mode_type="glitter")
            prompt = (
                "全面读取并比较整个仓库里 auth、provider、session、runtime、tools 多个目录的所有实现，"
                "同时检查每个模块的边界、异常处理、测试覆盖和相互依赖，找出全部问题"
            )
            decision = route_model(
                config, pack="tiga", policy="glitter", prompt=prompt, history=[],
                has_image=False, tools_required=True, tools_available=True,
            )
            assert decision is not None
            plan = build_execution_plan(decision)
            assert len(plan.workers) == 4

            session = Session("s", tmp, "kiro-byo-type:tiga:glitter", native=FakeNative())
            session.messages.append({"role": "user", "content": prompt})
            server._put_session(session)
            server._begin_agent_plan(session, plan)
            first = server._relay_teammate(
                decision, decision.provider, decision.model, config=config, signals=decision.signals
            )
            assert first is not None
            server._run_provider_tool_loop(
                1, "s", session, decision.provider, decision.model, 0, 0.0, 0.0,
                [], None, relay=(first[0], first[1], ""),
                reasoning_profile=decision.execution_intent.reasoning,
                routing_signals=decision.signals, routing_config=config,
                routing_decision=decision, execution_plan=plan,
            )

            assert set(child_models) == {"fast-a", "fast-b"}
            workers = session.agent_registry.tasks_for_role(
                next(task.plan_id for task in session.agent_registry.list() if task.spec.role == "worker"),
                "worker",
            )
            assert len(workers) == 4
            assert all(task.status == "completed" for task in workers)
            assert all(task.context_messages for task in workers)
            assert session.messages[-1]["content"] == "final synthesis"
            handbacks = [m for m in session.messages if "[Subagent findings:" in str(m.get("content"))]
            assert len(handbacks) == 4
            # Child transcripts remain private: only their structured hand-backs
            # are merged into the parent session.
            assert not any(
                m.get("role") == "assistant" and str(m.get("content", "")).startswith("findings from")
                for m in session.messages
            )
        finally:
            turn_mod.chat_completion_turn = original
            server._shutdown_agent_runtime(session) if 'session' in locals() else None
            server._executor.shutdown(wait=True)
            if old_home is None:
                os.environ.pop("KIRO_BYO_HOME", None)
            else:
                os.environ["KIRO_BYO_HOME"] = old_home


def test_agent_resume_control_plane_reuses_stable_task_and_pinned_model() -> None:
    import json
    from kiro_byo.agent_runtime import AgentResult, AgentSpec

    with tempfile.TemporaryDirectory() as tmp:
        old_home = os.environ.get("KIRO_BYO_HOME")
        os.environ["KIRO_BYO_HOME"] = tmp
        original = turn_mod.chat_completion_turn
        output = StringIO()
        server = AcpServer(stdin=StringIO(), stdout=output)
        config = KiroByoConfig((P1, P2), mode_type="glitter")
        server._config = lambda: config  # type: ignore[method-assign]
        session = Session("resume-s", tmp, "kiro-byo-type:tiga:glitter", native=FakeNative())
        server._put_session(session)
        plan_id, _ = session.agent_registry.begin_plan(
            session.session_id,
            (AgentSpec("worker", "inspect", name="worker-1", tool_policy="read_only", sticky_model=True),),
        )
        task = session.agent_registry.task_for_role(plan_id, "worker")
        assert task is not None
        session.agent_registry.attach_context(task.task_id, ({"role": "user", "content": "seed"},))
        session.agent_registry.pin_model(task.task_id, "p", "fast-a")
        session.agent_registry.transition(
            task.task_id, "completed",
            result=AgentResult(status="completed", summary="first", provider_id="p", model_id="fast-a"),
        )

        seen_messages: list[list[dict]] = []

        def fake_turn(provider, model_id, messages, emit, **kwargs):
            assert model_id == "fast-a"
            seen_messages.append(messages)
            return ChatTurn(text="continued finding")

        turn_mod.chat_completion_turn = fake_turn
        try:
            server._handle_agent_resume_request(
                77,
                {"sessionId": session.session_id, "taskId": task.task_id, "message": "check edge case"},
            )
            payload = json.loads(output.getvalue().strip().splitlines()[-1])
            assert payload["id"] == 77
            assert payload["result"]["result"]["summary"] == "continued finding"
            current = session.agent_registry.get(task.task_id)
            assert current is not None
            assert current.task_id == task.task_id
            assert current.generation == 1
            assert current.status == "completed"
            assert current.provider_id == "p" and current.model_id == "fast-a"
            assert any("check edge case" in str(msg.get("content", "")) for msg in seen_messages[-1])
        finally:
            turn_mod.chat_completion_turn = original
            server._shutdown_agent_runtime(session)
            server._executor.shutdown(wait=True)
            if old_home is None:
                os.environ.pop("KIRO_BYO_HOME", None)
            else:
                os.environ["KIRO_BYO_HOME"] = old_home


def test_read_only_subagent_retries_one_transient_transport_failure() -> None:
    from kiro_byo.providers.base import ProviderTransportError
    with tempfile.TemporaryDirectory() as tmp:
        old_home = os.environ.get("KIRO_BYO_HOME")
        os.environ["KIRO_BYO_HOME"] = tmp
        original = turn_mod.chat_completion_turn
        server = AcpServer(stdin=StringIO(), stdout=StringIO())
        calls = 0
        try:
            config = KiroByoConfig((P1, P2), mode_type="glitter")
            server._config = lambda: config  # type: ignore[method-assign]
            session = Session("sub-retry", tmp, "kiro-byo-type:tiga:glitter", native=FakeNative())
            server._put_session(session)
            from kiro_byo.agent_runtime import AgentSpec
            plan_id, _ = session.agent_registry.begin_plan(
                session.session_id,
                (AgentSpec("worker", "inspect safely", name="worker-1", tool_policy="read_only", sticky_model=True),),
            )
            task = session.agent_registry.task_for_role(plan_id, "worker")
            assert task is not None
            session.agent_registry.attach_context(task.task_id, ({"role": "user", "content": "inspect"},))
            session.agent_registry.pin_model(task.task_id, "p", "fast-a")

            def fake_turn(provider, model_id, messages, emit, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise ProviderTransportError("temporary EOF")
                return ChatTurn(text="child recovered")

            turn_mod.chat_completion_turn = fake_turn
            result = server._run_isolated_subagent(
                session, task, P1, FAST1,
                reasoning_profile=None,
                reasoning_signals=SimpleNamespace(complexity=0.5, code_task=False, tools_required=True, has_image=False),
                runtime_mode_context="",
            )
            assert calls == 2
            assert result.status == "completed"
            assert result.summary == "child recovered"
            assert result.usage["transport_retries"] == 1
            assert any(e.get("event") == "agent.subagent.retry" for e in server.runtime_trace.recent(50))
        finally:
            turn_mod.chat_completion_turn = original
            server._shutdown_agent_runtime(session) if 'session' in locals() else None
            server._executor.shutdown(wait=True)
            if old_home is None:
                os.environ.pop("KIRO_BYO_HOME", None)
            else:
                os.environ["KIRO_BYO_HOME"] = old_home
