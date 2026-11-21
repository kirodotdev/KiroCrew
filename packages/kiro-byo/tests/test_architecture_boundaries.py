"""R3 characterization: static ACP routing and read-only telemetry projection."""
from __future__ import annotations

import json
from io import StringIO
from types import SimpleNamespace

import pytest

from kiro_byo.acp_dispatch import ACP_REQUEST_HANDLERS
from kiro_byo.acp_server import AcpServer
from kiro_byo.turn_runner import TurnRunnerMixin
from kiro_byo.turn_telemetry_builder import build_turn_telemetry


def test_acp_dispatch_registry_is_complete_immutable_and_not_client_controlled():
    assert len(ACP_REQUEST_HANDLERS) == 38
    assert ACP_REQUEST_HANDLERS['session/set_model'] == '_handle_session_set_model_request'
    assert ACP_REQUEST_HANDLERS['_kiro/work/bind'] == '_handle_work_bind_request'
    assert ACP_REQUEST_HANDLERS['_kiro/run/retry'] == '_handle_run_retry_request'
    # Prompt and resume require dedicated dispatch and must not be dynamically routed.
    assert 'session/prompt' not in ACP_REQUEST_HANDLERS
    assert 'session/resume' not in ACP_REQUEST_HANDLERS
    with pytest.raises(TypeError):
        ACP_REQUEST_HANDLERS['untrusted/method'] = 'run'  # type: ignore[index]


def test_real_server_dispatch_keeps_unknown_method_and_notification_semantics(tmp_path, monkeypatch):
    monkeypatch.setenv('KIRO_BYO_HOME', str(tmp_path))
    output = StringIO()
    server = AcpServer(stdin=StringIO(), stdout=output)
    try:
        server.handle({'jsonrpc':'2.0','id':1,'method':'_kiro/work/bind','params':{'sessionId':'missing','workLink':{}}})
        server.handle({'jsonrpc':'2.0','id':2,'method':'__class__','params':{}})
        before = len(output.getvalue().splitlines())
        server.handle({'jsonrpc':'2.0','method':'__class__','params':{}})
        after = len(output.getvalue().splitlines())
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        assert rows[0]['error']['code'] == -32602  # registered path, missing session
        assert rows[1]['error']['code'] == -32601  # unknown rejected
        assert before == after  # notification is never replied to
    finally:
        server._executor.shutdown(wait=True)


def _state():
    attrs = dict(
        served_provider_id='p', served_model_id='model', model=SimpleNamespace(thinking_preference='on'),
        observed_efforts={'high', 'medium'}, observed_thinking_modes={'on'},
        upstream_ms=20, tool_ms=14, mcp_wait_ms=3, tool_calls_total=2,
        native_tool_calls_total=1, tool_search_calls_total=1, active_tools_count=4,
        tool_schema_bytes=1400, stream_chunks=4, stream_chars=23,
        input_tokens=13, output_tokens=8, cached_tokens=5, cache_write_tokens=3,
        reasoning_tokens=2, cache_usage_reported=True, cache_write_usage_reported=True,
        reasoning_usage_reported=True, transport_retries_total=1, fallbacks_total=0,
        turn_run_id='run-1', turn_trace_id='turn-1',
    )
    return SimpleNamespace(**attrs)


def test_telemetry_builder_preserves_all_turn_fields_and_is_side_effect_free():
    state = _state()
    session = SimpleNamespace(
        session_id='crew-1', model='selected', agent=SimpleNamespace(agent_id='kirocrew-lite'),
        cancel_source='user', selection_kind='type', selection_source='manual', selection_requested='smart',
    )
    result = build_turn_telemetry(
        session, state, outcome='cancelled', completed_at=100.5,
        total_ms=2500.0, first_ms=-1.0, error_kind='interrupted',
        error_category='user', error_site='provider',
        reasoning_summary=TurnRunnerMixin._reasoning_summary,
    )
    assert (result.timestamp, result.started_at, result.completed_at) == (100.5, 98.0, 100.5)
    assert (result.session_id, result.requested_model, result.served_model) == ('crew-1', 'selected', 'model')
    assert result.is_background_role is True
    assert result.reasoning_tokens == 2 and result.cache_write_tokens == 3
    assert (result.run_id, result.turn_id) == ('run-1', 'turn-1')
    assert result.cancel_source == 'user' and result.outcome == 'cancelled'
    assert result.effort == 'mixed'
    assert result.route_signals is None and result.route_diagnostics is None
    assert result.thinking_preference == 'on'
    assert state.tool_calls_total == 2  # input was not mutated


def test_telemetry_builder_preserves_route_candidate_and_signal_clamping():
    state = _state()
    session = SimpleNamespace(session_id='crew-1', model='selected')
    candidate = SimpleNamespace(
        provider=SimpleNamespace(id='p'),
        model=SimpleNamespace(model_id='model', thinking_preference='off'),
        score=.83, runtime_confidence='high',
    )
    route = SimpleNamespace(
        candidates=(candidate,), pack='tiga', policy='fast',
        signals=SimpleNamespace(task_profile='coding', complexity=1.4, risk=-.2,
                                parallelizability=.378941),
    )
    result = build_turn_telemetry(
        session, state, outcome='success', completed_at=25.0,
        total_ms=1500, first_ms=500, routing_decision=route,
        reasoning_summary=TurnRunnerMixin._reasoning_summary,
    )
    assert result.routing_score == .83
    assert result.routing_confidence == 'high'
    assert result.thinking_preference == 'off'
    assert (result.mode_pack, result.mode_type, result.task_profile) == ('tiga', 'fast', 'coding')
    assert result.route_signals == {'complexity': 1.0, 'parallelizability': .3789, 'risk': 0.0}
    assert result.started_at == 23.5


def test_acp_capability_audit_fails_closed_if_dispatch_table_points_to_missing_handler(tmp_path):
    from pathlib import Path
    from scripts.acp_capability_audit import _dispatch_names

    root = Path(__file__).resolve().parents[1] / 'src' / 'kiro_byo'
    (tmp_path / 'acp_server.py').write_text((root / 'acp_server.py').read_text(encoding='utf-8'), encoding='utf-8')
    original = (root / 'acp_dispatch.py').read_text(encoding='utf-8')
    damaged = original.replace("'_kiro/work/bind': '_handle_work_bind_request'", "'_kiro/work/bind': '_missing_handler'")
    (tmp_path / 'acp_dispatch.py').write_text(damaged, encoding='utf-8')
    assert _dispatch_names(tmp_path / 'acp_server.py') == set()


def test_one_orphan_run_reconciler_and_type_selection_transition():
    """A new session cannot invent a separate Run recovery or Type state machine."""
    import inspect

    start = inspect.getsource(AcpServer._handle_session_new_request)
    select = inspect.getsource(AcpServer._set_model)
    assert 'self._reconcile_restored_durable_runs(session, session_id)' in start
    assert 'run_store.pending(' not in start
    assert select.count('session.selection_kind = "type"') == 1
    assert select.count('session.effort_user_set = False') == 2
