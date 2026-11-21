"""0.2.5 health / independent ledgers / real Type route regressions."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
import time
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

import kiro_byo.turn_runner as runner
from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.durable_runs import DurableRunStore
from kiro_byo.providers.base import ChatTurn
from kiro_byo.runtime_trace import RuntimeTraceStore
from kiro_byo.telemetry import TurnTelemetry, TurnTelemetryStore
from kiro_byo.protocol import type_selection_id

ROOT = Path(__file__).resolve().parents[1]


def _sample(outcome='success'):
    return TurnTelemetry(
        timestamp=time.time(), session_id='new-byo-session', outcome=outcome,
        requested_model=type_selection_id('tiga', 'multi'), provider_id='p',
        served_model='model', effort='', total_ms=20, first_visible_ms=5,
        upstream_ms=15, tool_ms=0, mcp_wait_ms=0, tool_calls=0,
        native_calls=0, tool_search_calls=0, active_tools=0,
        tool_schema_bytes=0, stream_chunks=1, stream_chars=10,
    )


def _app_routes():
    """Load the real KiroCrew App routes in their documented synthetic package."""
    pkg_name = '_kiro_byo_records_regression'
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(ROOT)]
    backend = types.ModuleType(pkg_name + '.backend')
    backend.__path__ = [str(ROOT / 'backend')]
    crew = types.ModuleType('kiro_crew')
    apps = types.ModuleType('kiro_crew.apps')
    registry = types.ModuleType('kiro_crew.apps.route_registry')
    registry.AppRoute = lambda *a, **kw: (a, kw)
    names = {pkg_name: pkg, pkg_name + '.backend': backend,
             'kiro_crew': crew, 'kiro_crew.apps': apps,
             'kiro_crew.apps.route_registry': registry}
    with patch.dict(sys.modules, names):
        spec = importlib.util.spec_from_file_location(pkg_name + '.backend.routes', ROOT / 'backend' / 'routes.py')
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        try:
            spec.loader.exec_module(mod)
            yield mod
        finally:
            sys.modules.pop(spec.name, None)
            for key in list(sys.modules):
                if key.startswith(pkg_name + '.'):
                    sys.modules.pop(key, None)


class _Request:
    def __init__(self, **query):
        self.query = query


def test_clear_request_endpoint_preserves_true_route_traces_and_durable_unknown_runs(tmp_path, monkeypatch):
    for routes in _app_routes():
        monkeypatch.setattr(routes, '_ensure_app_state', lambda ctx: tmp_path)
        with routes.use_app_home(tmp_path):
            telemetry = routes.TurnTelemetryStore()
            traces = routes.RuntimeTraceStore()
            durable = routes.DurableRunStore()
            telemetry.record(_sample('error'))
            traces.record('model.route.selected', session_id='previous', data={'policy': 'multi'})
            active = durable.create_run('pending')
            risky = durable.create_run('risk')
            durable.checkpoint(risky['run_id'], name='tool_batch_admitted', data={'side_effect_risk': True})
            before = {active['run_id']: durable.get(active['run_id']), risky['run_id']: durable.get(risky['run_id'])}
            assert durable.restart_recovery_state(before[risky['run_id']]) == 'unknown_side_effect'
        cleared = asyncio.run(routes.clear_diagnostics(_Request(), None))
        payload = json.loads(cleared.body)
        assert payload['removed'] == 1
        assert payload['removed_trace'] == 0
        with routes.use_app_home(tmp_path):
            assert routes.TurnTelemetryStore().recent(5) == []
            assert [e['event'] for e in routes.RuntimeTraceStore().recent(100)] == ['model.route.selected']
            for run_id, original in before.items():
                assert routes.DurableRunStore().get(run_id) == original
        response = asyncio.run(routes.get_runtime_trace(_Request(limit='50', event='model.route.selected'), None))
        assert len(json.loads(response.body)['events']) == 1


def test_filtered_routes_are_not_crowded_out_by_other_acp_events(tmp_path, monkeypatch):
    monkeypatch.setenv('KIRO_BYO_RUNTIME_TRACE', 'metadata')
    trace = RuntimeTraceStore(tmp_path / 'events.jsonl', max_records=1000)
    trace.record('model.route.selected', session_id='type-session', turn_id='turn-a', run_id='run-a', data={'policy': 'multi'})
    for n in range(240):
        trace.record('acp.outbound', session_id='other', data={'n': n})
    assert not any(x['event'] == 'model.route.selected' for x in trace.recent(100))
    matching = trace.recent(100, event='model.route.selected')
    assert len(matching) == 1
    assert matching[0]['session_id'] == 'type-session'
    assert matching[0]['turn_id'] == 'turn-a'
    assert matching[0]['run_id'] == 'run-a'
    assert trace.recent(100, event='model.route.selected', session_id='other') == []


def test_new_multi_type_turn_after_request_clear_records_real_route_and_request(tmp_path, monkeypatch):
    monkeypatch.setenv('KIRO_BYO_HOME', str(tmp_path))
    monkeypatch.setenv('KIRO_BYO_RUNTIME_TRACE', 'metadata')
    # Clear previous request history before starting a fresh session.
    TurnTelemetryStore().record(_sample('error'))
    TurnTelemetryStore().clear()
    assert TurnTelemetryStore().recent(5) == []
    model = ModelSpec('p', 'P', 'model', 'Model', routing_tiers=('fast', 'strong'), tool_use=True)
    config = KiroByoConfig((ProviderSpec('p', 'P', 'openai-compatible', 'https://example.invalid/v1', '', (model,)),),
                           default_model='Model', mode_pack='tiga', mode_type='multi')
    output = StringIO()
    server = AcpServer(stdin=StringIO(), stdout=output)
    session = Session('fresh-multi-session', str(tmp_path), type_selection_id('tiga', 'multi'))
    server._put_session(session)
    calls = []

    def fake_provider(provider, model_id, messages, emit, **kwargs):
        calls.append(model_id)
        emit('模拟成功')
        return ChatTurn(text='模拟成功')

    with patch.object(AcpServer, '_config', staticmethod(lambda: config)), patch.object(runner, 'chat_completion_turn', fake_provider):
        try:
            server._handle_prompt_request(1, {'sessionId': session.session_id,
                                              'prompt': [{'type': 'text', 'text': '简短回复你好'}]})
        finally:
            server._executor.shutdown(wait=True)
    assert calls, 'provider inference did not run'
    rows = TurnTelemetryStore().recent(10)
    assert len(rows) == 1
    assert rows[0]['outcome'] == 'success'
    assert rows[0]['mode_type'] == 'multi'
    events = RuntimeTraceStore().recent(100, event='model.route.selected')
    assert len(events) == 1
    assert events[0]['session_id'] == session.session_id
    assert events[0]['data']['policy'] == 'multi'
    assert events[0]['data']['resolved_model'] == 'model'
    assert events[0]['run_id'] == rows[0]['run_id']
    assert events[0]['turn_id'] == rows[0]['turn_id']
