"""V6 fault injection: terminal failure ownership never authorizes replay."""
from __future__ import annotations

import json
from io import StringIO
from types import SimpleNamespace

import pytest

import kiro_byo.turn_runner as turn_mod
from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.config import ModelSpec, ProviderSpec
from kiro_byo.providers.base import ChatTurn, ProviderTransportError, ToolCall

MODEL = ModelSpec('v6', 'V6', 'v6-primary', 'Primary', tier='strong')
FALLBACK = ModelSpec('v6', 'V6', 'v6-fallback', 'Fallback', tier='fast')
PROVIDER = ProviderSpec('v6', 'V6', 'openai-compatible', 'https://example.invalid/v1', '', (MODEL, FALLBACK))

@pytest.fixture
def harness(monkeypatch, tmp_path):
    monkeypatch.setenv('KIRO_BYO_HOME', str(tmp_path))
    out = StringIO()
    server = AcpServer(stdin=StringIO(), stdout=out)
    try:
        yield server, out, tmp_path
    finally:
        server._executor.shutdown(wait=True)


def _session(server, tmp_path, *, native=None):
    sess = Session('v6', str(tmp_path), MODEL.model_id, native=native)
    server._put_session(sess)
    sess.messages.append({'role': 'user', 'content': 'work'})
    return sess


def _responses(out):
    return [json.loads(line) for line in out.getvalue().splitlines()
            if line.startswith('{') and json.loads(line).get('id') == 1]


def test_terminal_transport_failure_after_one_pristine_retry_has_single_result(harness, monkeypatch):
    server, out, home = harness
    session = _session(server, home)
    calls = []
    def fail(*args, **kwargs):
        calls.append(args[1])
        raise ProviderTransportError('injected lost connection')
    monkeypatch.setattr(turn_mod, 'chat_completion_turn', fail)
    server._run_provider_tool_loop(1, 'v6', session, PROVIDER, MODEL, 0, 0.0, 0.0)
    assert calls == [MODEL.model_id, MODEL.model_id]
    assert session.messages == []
    assert server.telemetry.recent(1)[0]['outcome'] == 'error'
    assert server.telemetry.recent(1)[0]['error_category'] == 'transport'
    assert [x['event'] for x in server.runtime_trace.recent(100)].count('provider.retry') == 1
    assert len(_responses(out)) == 1 and 'result' in _responses(out)[0]


def test_pristine_fallback_is_policy_controlled_not_terminal_error_handler(harness, monkeypatch):
    server, out, home = harness
    session = _session(server, home)
    calls = []
    def inference(provider, model_id, messages, emit, **kwargs):
        calls.append(model_id)
        if model_id == MODEL.model_id:
            raise ProviderTransportError('temporary network down')
        return ChatTurn(text='fallback succeeded')
    monkeypatch.setattr(turn_mod, 'chat_completion_turn', inference)
    server._run_provider_tool_loop(
        1, 'v6', session, PROVIDER, MODEL, 0, 0.0, 0.0,
        mode_fallbacks=[(PROVIDER, FALLBACK)],
    )
    assert calls == [MODEL.model_id, MODEL.model_id, FALLBACK.model_id]
    assert session.messages[-1]['content'] == 'fallback succeeded'
    assert server.telemetry.recent(1)[0]['outcome'] == 'success'
    assert any(r['event'] == 'model.route.fallback' for r in server.runtime_trace.recent(100))
    assert len(_responses(out)) == 1


def test_visible_partial_failure_does_not_fallback_or_retry(harness, monkeypatch):
    server, out, home = harness
    session = _session(server, home)
    calls = []
    def inference(provider, model_id, messages, emit, **kwargs):
        calls.append(model_id)
        emit('already visible')
        raise ProviderTransportError('interrupted after streaming')
    monkeypatch.setattr(turn_mod, 'chat_completion_turn', inference)
    server._run_provider_tool_loop(
        1, 'v6', session, PROVIDER, MODEL, 0, 0.0, 0.0,
        mode_fallbacks=[(PROVIDER, FALLBACK)],
    )
    assert calls == [MODEL.model_id]
    assert not any(r['event'] == 'model.route.fallback' for r in server.runtime_trace.recent(100))
    assert session.messages == []
    assert server.telemetry.recent(1)[0]['outcome'] == 'error'
    assert len(_responses(out)) == 1


def test_internal_error_after_mutation_preserves_effects_and_redacts_trace_event(harness, monkeypatch):
    server, out, home = harness
    class Native:
        def specs(self):
            return (SimpleNamespace(name='write'),)
        def tool_specs(self, names=None):
            return [{'type': 'function', 'function': {'name': 'write'}}]
        def has_tool(self, name):
            return name == 'write'
        def capability_for(self, name):
            return 'fs_write'
    session = _session(server, home, native=Native())
    calls = []
    def inference(provider, model_id, messages, emit, **kwargs):
        calls.append(model_id)
        if len(calls) == 1:
            return ChatTurn(text='', tool_calls=(ToolCall('w1', 'write', '{"path":"x.txt","content":"x"}'),))
        raise RuntimeError('SENSITIVE_INJECTED_MESSAGE')
    monkeypatch.setattr(turn_mod, 'chat_completion_turn', inference)
    server._execute_native_tool = lambda session, call, args: ('Wrote x.txt', 0.0, '')
    server._run_provider_tool_loop(1, 'v6', session, PROVIDER, MODEL, 0, 0.0, 0.0)
    assert len(calls) == 2  # never replays a completed mutation
    assert any(msg.get('role') == 'tool' for msg in session.messages)
    assert any(e.get('type') == 'turn/recovery_preserved' for e in session.events)
    assert server.telemetry.recent(1)[0]['error_category'] == 'internal'
    error_events = [r for r in server.runtime_trace.recent(100) if r['event'] == 'runtime.internal_error']
    assert len(error_events) == 1
    assert 'SENSITIVE_INJECTED_MESSAGE' not in str(error_events[0])
    assert len(_responses(out)) == 1


def test_cancel_before_provider_invocation_is_terminal_without_network_call(harness, monkeypatch):
    server, out, home = harness
    session = _session(server, home)
    session.cancel_event.set()
    calls = []
    monkeypatch.setattr(turn_mod, 'chat_completion_turn', lambda *a, **kw: calls.append(1))
    server._run_provider_tool_loop(1, 'v6', session, PROVIDER, MODEL, 0, 0.0, 0.0)
    assert calls == []
    assert session.messages == []
    assert server.telemetry.recent(1)[0]['outcome'] == 'cancelled'
    assert len(_responses(out)) == 1
    assert _responses(out)[0]['result']['stopReason'] == 'cancelled'


def test_loop_termination_handlers_do_not_embed_retry_or_fallback_logic():
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    runner = ast.parse((root / 'src/kiro_byo/turn_runner.py').read_text(encoding='utf-8'))
    loop = next(node for node in ast.walk(runner)
                if isinstance(node, ast.FunctionDef) and node.name == '_run_provider_tool_loop')
    try_block = next(node for node in loop.body if isinstance(node, ast.Try))
    assert len(try_block.handlers) == 2
    methods = []
    for handler in try_block.handlers:
        calls = [node for node in ast.walk(handler)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        methods.extend(call.func.attr for call in calls)
    assert methods == [
        '_finish_provider_loop_provider_error', '_finish_provider_loop_internal_error',
    ]
    terminal_path = root / 'src/kiro_byo/provider_loop_terminal.py'
    terminal = terminal_path.read_text(encoding='utf-8')
    assert 'live_provider_failure' not in terminal
    assert 'provider.retry' not in terminal
    assert 'model.route.fallback' not in terminal
